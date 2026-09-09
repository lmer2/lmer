"""The session bus the supervisor keeps (issue #407, T1).

What is pinned here is the contract connectors and the agent's CLI ship
against: the record shape, the per-session ``seq`` cursor, first-answer-wins
arbitration, dedupe on ``(channel, ref)``, the PTY relay with its source tag,
the terminal line reconstruction, and the one-release ask-directory
projection. ``docs/COMMS.md`` describes the same contract in prose.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import threading
import time

import pytest

from ask_channel import protocol
from lmer_cli import comms, supervisor
from lmer_cli.comms import (
    ACCEPTED_DUPLICATE, ACCEPTED_LATE, ACCEPTED_NEW, CHANNEL_AGENT,
    CHANNEL_PLATFORM, CHANNEL_SLACK, CHANNEL_TERMINAL, CHANNEL_UBER,
    DELIVERY_CLI, DELIVERY_PTY, KIND_ANSWER, KIND_CLOSE, KIND_COMMAND,
    KIND_MESSAGE, KIND_MILESTONE, KIND_NOTE, KIND_QUESTION, CommsError,
    Message, MessageLog, Source, TerminalTracker,
)
from tests.conftest import strip_lmer_env

AUTH = {"Authorization": "Bearer test-token"}

#: This checkout, for the one test that reads a document.
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def log(tmp_path):
    return MessageLog("sess-1", str(tmp_path / "messages.jsonl"))


def agent(kind, text, **kw):
    return dict(kind=kind, text=text, source=Source(CHANNEL_AGENT), **kw)


def human(channel, author, ref, kind, text, **kw):
    return dict(kind=kind, text=text, source=Source(channel, author=author, ref=ref), **kw)


# ---------------------------------------------------------------------------
# Records and ids
# ---------------------------------------------------------------------------


class TestRecords:
    def test_seq_is_a_dense_per_session_cursor_and_id_names_the_session(self, log):
        first, _ = log.append(**agent(KIND_NOTE, "one"))
        second, _ = log.append(**agent(KIND_NOTE, "two"))
        assert (first.seq, second.seq) == (1, 2)
        assert first.id == "sess-1/000001"
        assert log.seq == 2

    def test_record_round_trips_through_its_dict(self, log):
        message, _ = log.append(
            **agent(KIND_QUESTION, "which branch?", options=["main", "prep-release"])
        )
        again = Message.from_dict(json.loads(json.dumps(message.to_dict())))
        assert again == message
        assert again.to_dict()["schema"] == comms.SCHEMA_VERSION

    @pytest.mark.parametrize("ref, expected", [
        ("42", 42), ("000042", 42), ("sess-1/000042", 42), (42, 42),
        ("other/000042", None), ("0", None), ("abc", None), (None, None), (True, None),
    ])
    def test_a_reply_may_name_a_record_three_ways(self, ref, expected):
        assert comms.parse_message_ref(ref, "sess-1") == expected

    def test_source_label_is_channel_then_author(self):
        assert Source(CHANNEL_SLACK, author="vegu").label == "slack · vegu"
        assert Source(CHANNEL_UBER).label == "uber lmer"
        assert Source("future-thing").label == "future-thing"

    def test_source_channel_is_validated_not_sanitized(self):
        with pytest.raises(CommsError):
            Source.from_dict({"channel": "Slack Thread"})
        with pytest.raises(CommsError):
            Source.from_dict({"channel": "slack", "author": "x" * 201})


# ---------------------------------------------------------------------------
# Validation and caps
# ---------------------------------------------------------------------------


class TestValidation:
    def test_text_caps_are_per_kind(self, log):
        with pytest.raises(CommsError, match="1000 limit for a milestone"):
            log.append(**agent(KIND_MILESTONE, "m" * 1001))
        log.append(**agent(KIND_NOTE, "n" * 1001))  # a note is allowed 4096

    def test_the_agent_may_not_answer_or_command(self, log):
        with pytest.raises(CommsError, match="agent may post"):
            log.append(**agent(KIND_ANSWER, "x", in_reply_to="1"))
        with pytest.raises(CommsError, match="agent may post"):
            log.append(**agent(KIND_COMMAND, "/followup"))

    def test_only_platform_and_uber_lmer_may_command(self, log):
        with pytest.raises(CommsError, match="command may only come from"):
            log.append(**human(CHANNEL_SLACK, "vegu", "t1", KIND_COMMAND, "/followup"))
        message, accepted = log.append(**human(CHANNEL_UBER, None, "r1", KIND_COMMAND, "/followup"))
        assert accepted == ACCEPTED_NEW and message.kind == KIND_COMMAND

    def test_open_question_cap_counts_only_open_ones(self, log):
        for index in range(comms.MAX_OPEN_QUESTIONS):
            log.append(**agent(KIND_QUESTION, f"q{index}"))
        with pytest.raises(CommsError, match="still open"):
            log.append(**agent(KIND_QUESTION, "one too many"))
        log.append(**agent(KIND_CLOSE, "moved on", in_reply_to="1"))
        log.append(**agent(KIND_QUESTION, "fits again"))

    def test_agent_entry_cap_ignores_inbound_traffic(self, log, monkeypatch):
        monkeypatch.setattr(comms, "MAX_AGENT_ENTRIES", 2)
        log.append(**agent(KIND_NOTE, "a"))
        log.append(**human(CHANNEL_SLACK, "vegu", "t1", KIND_MESSAGE, "hi"))
        log.append(**agent(KIND_NOTE, "b"))
        with pytest.raises(CommsError, match="looping"):
            log.append(**agent(KIND_NOTE, "c"))
        log.append(**human(CHANNEL_SLACK, "vegu", "t2", KIND_MESSAGE, "still fine"))

    def test_reply_to_an_unknown_record_is_refused(self, log):
        with pytest.raises(CommsError, match="names no question"):
            log.append(**human(CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "x", in_reply_to="7"))
        log.append(**agent(KIND_NOTE, "n"))
        with pytest.raises(CommsError, match="names no question"):
            log.append(**human(CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "x", in_reply_to="1"))
        with pytest.raises(CommsError, match="names no record"):
            log.append(**human(CHANNEL_SLACK, "vegu", "t2", KIND_MESSAGE, "x", in_reply_to="9"))

    def test_caller_meta_cannot_forge_arbitration_fields(self, log):
        message, _ = log.append(
            **agent(KIND_NOTE, "n", meta={"late": True, "delivered": "cli", "custom": 1})
        )
        assert message.meta == {"custom": 1}


# ---------------------------------------------------------------------------
# Questions: first answer wins
# ---------------------------------------------------------------------------


class TestQuestions:
    def test_first_answer_wins_and_later_ones_are_late(self, log):
        question, _ = log.append(**agent(KIND_QUESTION, "which branch?"))
        first, accepted = log.append(
            **human(CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "prep-release", in_reply_to="1")
        )
        assert accepted == ACCEPTED_NEW
        assert first.meta == {"delivered": DELIVERY_PTY}
        second, accepted = log.append(
            **human("matrix", "@alice:example.org", "e1", KIND_ANSWER, "main", in_reply_to="1")
        )
        assert accepted == ACCEPTED_LATE
        assert second.meta == {"late": True, "answered_by": first.id, "delivered": DELIVERY_PTY}
        (state,) = log.questions()
        assert (state.state, state.answered_by) == (comms.STATE_ANSWERED, first.id)

    def test_an_option_index_names_the_option_and_free_text_is_left_alone(self, log):
        """#428: the index is how a door that rendered a numbered list says which
        option was picked. A number in the *text* is not read as one — a
        question offering `3 5 10` answered `1` would otherwise arrive as `3`."""
        log.append(**agent(KIND_QUESTION, "which branch?", options=["red", "blue", "green"]))
        picked, _ = log.append(**human(
            CHANNEL_PLATFORM, "operator", "p1", KIND_ANSWER, "", in_reply_to="1", option=2,
        ))
        assert (picked.text, picked.meta["option"]) == ("blue", 2)
        assert picked.options == (), "an answer carries no options of its own"
        typed, _ = log.append(**human(
            CHANNEL_PLATFORM, "operator", "p2", KIND_ANSWER, "2", in_reply_to="1",
        ))
        assert typed.text == "2" and "option" not in typed.meta

    @pytest.mark.parametrize("text, stored", [
        ("blue", "blue"), ("BLUE", "blue"), (" Blue ", "blue"),
        ("2", "2"), ("2.", "2."), ("7", "7"), ("later", "later"),
    ])
    def test_an_option_named_by_its_own_text_is_stored_canonically(self, log, text, stored):
        log.append(**agent(KIND_QUESTION, "which branch?", options=["red", "blue", "green"]))
        answer, _ = log.append(
            **human(CHANNEL_PLATFORM, "operator", "p1", KIND_ANSWER, text, in_reply_to="1")
        )
        assert answer.text == stored

    @pytest.mark.parametrize("option", [0, -1, 4, 99])
    def test_an_index_the_question_does_not_offer_is_refused_with_the_count(self, log, option):
        log.append(**agent(KIND_QUESTION, "which branch?", options=["red", "blue", "green"]))
        with pytest.raises(CommsError, match="not one of the 3"):
            log.append(**human(
                CHANNEL_PLATFORM, "operator", "p1", KIND_ANSWER, "", in_reply_to="1",
                option=option,
            ))

    def test_an_index_against_an_option_less_question_is_refused(self, log):
        log.append(**agent(KIND_QUESTION, "words please?"))
        with pytest.raises(CommsError, match="not one of the 0"):
            log.append(**human(
                CHANNEL_PLATFORM, "operator", "p1", KIND_ANSWER, "", in_reply_to="1", option=1,
            ))

    @pytest.mark.parametrize("option", [True, "2", 2.0])
    def test_an_index_must_be_a_whole_number(self, log, option):
        """`True` is an `int` in Python and would silently mean the first option."""
        log.append(**agent(KIND_QUESTION, "which branch?", options=["red", "blue"]))
        with pytest.raises(CommsError, match="whole number"):
            log.append(**human(
                CHANNEL_PLATFORM, "operator", "p1", KIND_ANSWER, "x", in_reply_to="1",
                option=option,
            ))

    def test_only_an_answer_carries_an_option(self, log):
        with pytest.raises(CommsError, match="this is a note"):
            log.append(**agent(KIND_NOTE, "working", option=1))

    def test_an_index_only_answer_needs_no_words_but_a_plain_one_still_does(self, log):
        log.append(**agent(KIND_QUESTION, "which branch?", options=["red", "blue"]))
        with pytest.raises(CommsError, match="text is empty"):
            log.append(**human(CHANNEL_PLATFORM, "operator", "p1", KIND_ANSWER, "  ",
                               in_reply_to="1"))

    def test_an_index_and_words_together_are_refused_rather_than_one_winning(self, log):
        """Round 2, major: `body = offered[picked - 1]` overwrote unconditionally,
        so `--option 2` beside "prep-release, but rebase first" delivered
        `prep-release` and dropped the instruction with no trace."""
        log.append(**agent(KIND_QUESTION, "which branch?", options=["red", "blue"]))
        with pytest.raises(CommsError, match="not both"):
            log.append(**human(
                CHANNEL_PLATFORM, "operator", "p1", KIND_ANSWER,
                "red, but rebase onto it first", in_reply_to="1", option=1,
            ))
        # Whitespace is not words: an index-only answer may still be padded.
        padded, _ = log.append(**human(
            CHANNEL_PLATFORM, "operator", "p2", KIND_ANSWER, "  ", in_reply_to="1", option=1,
        ))
        assert padded.text == "red"
        # Words that name the same option are a redundancy, not a contradiction:
        # the ask-dir answer file carries both, because its reader needs the text.
        agreeing, _ = log.append(**human(
            CHANNEL_PLATFORM, "operator", "p3", KIND_ANSWER, "RED", in_reply_to="1", option=1,
        ))
        assert (agreeing.text, agreeing.meta["option"]) == ("red", 1)
        with pytest.raises(CommsError, match="not both"):
            log.append(**human(
                CHANNEL_PLATFORM, "operator", "p4", KIND_ANSWER, "blue",
                in_reply_to="1", option=1,
            ))

    def test_a_late_answer_resolves_its_index_too(self, log):
        log.append(**agent(KIND_QUESTION, "which branch?", options=["red", "blue"]))
        log.append(**human(CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "red", in_reply_to="1"))
        late, accepted = log.append(**human(
            "matrix", "@a:x", "e1", KIND_ANSWER, "", in_reply_to="1", option=2,
        ))
        assert accepted == ACCEPTED_LATE
        assert (late.text, late.meta["option"], late.meta["late"]) == ("blue", 2, True)

    def test_the_option_index_is_stamped_by_the_supervisor_not_the_poster(self, log):
        log.append(**agent(KIND_QUESTION, "which branch?", options=["red", "blue"]))
        answer, _ = log.append(**human(
            CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "red", in_reply_to="1",
            meta={"option": 2},
        ))
        assert "option" not in answer.meta

    def test_every_key_the_supervisor_stamps_is_refused_from_a_caller(self, log):
        """Driven off `RESERVED_META_KEYS` rather than a literal, so a key added
        to it is covered here without anyone remembering to — which is what §1's
        row promises. An unreserved key is still kept as sent."""
        assert comms.RESERVED_META_KEYS, "the set is what this test is about"
        forged = {key: "forged" for key in comms.RESERVED_META_KEYS}
        log.append(**agent(KIND_QUESTION, "which branch?", options=["red", "blue"]))
        answer, _ = log.append(**human(
            CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "red", in_reply_to="1",
            meta={**forged, "fidelity": "reconstructed"},
        ))
        kept = set(answer.meta) & set(comms.RESERVED_META_KEYS)
        assert kept == {"delivered"}, "only the one the supervisor stamps on this record"
        assert answer.meta["delivered"] == DELIVERY_PTY, "its own value, not the caller's"
        assert answer.meta["fidelity"] == "reconstructed"

    def test_the_reserved_keys_are_the_ones_the_document_names(self):
        """Round 3 nit: driving the test above off `RESERVED_META_KEYS` made it
        self-updating but cut its tie to §1's row, which the old literal was
        accidentally holding. This is that tie, both ways — the tuple and the
        prose can no longer drift apart in silence."""
        row = next(
            line for line in (REPO / "docs" / "COMMS.md").read_text().splitlines()
            if line.startswith("| `meta` |")
        )
        sets, _, rest = row.partition("The supervisor sets")
        sentence = re.sub(r"\([^)]*\)", "", rest.split("Every key in that list")[0])
        documented = {name.split(":")[0].strip() for name in re.findall(r"`([^`]+)`", sentence)}
        assert documented == set(comms.RESERVED_META_KEYS)
        assert "fidelity" in row, "and the row still says an unreserved key is kept"

    def test_close_ends_the_question_and_a_second_close_is_refused(self, log):
        log.append(**agent(KIND_QUESTION, "q"))
        close, _ = log.append(**agent(KIND_CLOSE, "", in_reply_to="1"))
        (state,) = log.questions()
        assert (state.state, state.closed_by) == (comms.STATE_CLOSED, close.id)
        with pytest.raises(CommsError, match="already closed"):
            log.append(**agent(KIND_CLOSE, "", in_reply_to="1"))
        _, accepted = log.append(
            **human(CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "too late", in_reply_to="1")
        )
        assert accepted == ACCEPTED_LATE

    def test_an_attached_waiter_takes_the_answer_instead_of_the_pty(self, log):
        question, _ = log.append(**agent(KIND_QUESTION, "q"))
        got = {}

        def wait():
            got["result"] = log.read_since(
                question.seq, timeout=5.0, kinds=[KIND_ANSWER], waiter=question.id
            )

        thread = threading.Thread(target=wait)
        thread.start()
        deadline = time.monotonic() + 2.0
        while question.id not in log._waiters and time.monotonic() < deadline:
            time.sleep(0.01)
        answer, _ = log.append(
            **human(CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "yes", in_reply_to="1")
        )
        thread.join(timeout=5.0)
        assert answer.meta == {"delivered": DELIVERY_CLI}
        messages, cursor = got["result"]
        assert [m.id for m in messages] == [answer.id] and cursor == 2
        assert question.id not in log._waiters

    def test_a_claim_covers_the_gap_between_the_ask_and_the_first_poll(self, log):
        """The window the CLI cannot close from its side: the ask returns, the
        CLI checks for an existing answer, and only then polls. An answer in
        there was stamped for the PTY *and* returned on stdout — the agent got
        it twice, once as a turn it never asked for."""
        question, _ = log.append(**agent(KIND_QUESTION, "branch?"))
        log.claim(question.id, ttl=60.0)
        answer, _ = log.append(
            **human(CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "prep", in_reply_to="1")
        )
        assert answer.meta["delivered"] == DELIVERY_CLI
        # and the CLI's poll takes delivery, so nothing types it in later
        messages, _ = log.read_since(question.seq, kinds=[KIND_ANSWER], waiter=question.id)
        assert [m.id for m in messages] == [answer.id]
        assert log.lapsed_answers() == []

    def test_an_answer_the_waiting_cli_never_read_is_typed_in_when_its_claim_lapses(self, log):
        """The other half of §7: a `lmer-signal ask` killed mid-poll (a harness
        command hitting its own timeout is the ordinary case) used to leave the
        answer marked delivered to a process that no longer exists."""
        question, _ = log.append(**agent(KIND_QUESTION, "branch?"))
        log.claim(question.id, ttl=0.05)
        answer, _ = log.append(
            **human(CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "prep", in_reply_to="1")
        )
        assert answer.meta["delivered"] == DELIVERY_CLI
        assert log.lapsed_answers() == []  # still claimed
        time.sleep(0.1)
        assert [m.id for m in log.lapsed_answers()] == [answer.id]
        assert log.lapsed_answers() == [], "relayed once, not on every tick"

    def test_releasing_a_claim_hands_the_next_answer_back_to_the_harness(self, log):
        question, _ = log.append(**agent(KIND_QUESTION, "branch?"))
        log.claim(question.id, ttl=60.0)
        log.release(question.id)
        answer, _ = log.append(
            **human(CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "prep", in_reply_to="1")
        )
        assert answer.meta["delivered"] == DELIVERY_PTY

    def test_duplicate_ref_returns_the_stored_record_and_appends_nothing(self, log):
        log.append(**agent(KIND_QUESTION, "q"))
        first, _ = log.append(**human(CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "yes", in_reply_to="1"))
        again, accepted = log.append(
            **human(CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "yes", in_reply_to="1")
        )
        assert accepted == ACCEPTED_DUPLICATE and again is first and log.seq == 2
        # Same ref on another channel is a different message.
        _, accepted = log.append(**human("matrix", "a", "t1", KIND_MESSAGE, "hello"))
        assert accepted == ACCEPTED_NEW

    def test_questions_view_can_be_narrowed_to_open(self, log):
        log.append(**agent(KIND_QUESTION, "a"))
        log.append(**agent(KIND_QUESTION, "b"))
        log.append(**human(CHANNEL_SLACK, "v", "t", KIND_ANSWER, "x", in_reply_to="2"))
        assert [s.question.seq for s in log.questions(open_only=True)] == [1]
        assert log.question("000002").state == comms.STATE_ANSWERED
        assert log.question("9") is None


# ---------------------------------------------------------------------------
# Reading, acks, persistence
# ---------------------------------------------------------------------------


class TestReading:
    def test_read_since_is_exclusive_and_filters_by_kind(self, log):
        log.append(**agent(KIND_NOTE, "n1"))
        log.append(**agent(KIND_MILESTONE, "m1"))
        log.append(**agent(KIND_NOTE, "n2"))
        messages, cursor = log.read_since(1)
        assert [m.text for m in messages] == ["m1", "n2"] and cursor == 3
        messages, cursor = log.read_since(0, kinds=[KIND_MILESTONE])
        assert [m.text for m in messages] == ["m1"] and cursor == 3

    def test_read_since_blocks_until_the_next_record(self, log):
        result = {}

        def read():
            result["value"] = log.read_since(0, timeout=5.0)

        thread = threading.Thread(target=read)
        thread.start()
        time.sleep(0.05)
        log.append(**agent(KIND_NOTE, "arrived"))
        thread.join(timeout=5.0)
        messages, cursor = result["value"]
        assert [m.text for m in messages] == ["arrived"] and cursor == 1

    def test_read_since_times_out_empty(self, log):
        started = time.monotonic()
        messages, cursor = log.read_since(0, timeout=0.05)
        assert messages == [] and cursor == 0
        assert time.monotonic() - started < 2.0

    def test_acks_keep_a_high_water_mark_per_subscriber(self, log):
        log.ack("platform", 3)
        log.ack("platform", 2)
        log.ack("slack", 5)
        assert log.subscribers() == {"platform": 3, "slack": 5}
        with pytest.raises(CommsError):
            log.ack("", 1)
        with pytest.raises(CommsError):
            log.ack("platform", -1)

    def test_the_file_is_the_record_and_reloads_with_the_fold(self, tmp_path):
        path = tmp_path / "messages.jsonl"
        first = MessageLog("sess-1", str(path))
        first.append(**agent(KIND_QUESTION, "q"))
        first.append(**human(CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "yes", in_reply_to="1"))
        first.append(**agent(KIND_NOTE, "later"))
        first.close()
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3 and json.loads(lines[0])["seq"] == 1
        assert oct(path.stat().st_mode & 0o777) == "0o600"

        # A torn tail: the last line is half-written.
        path.write_text("\n".join(lines[:2] + [lines[2][:20]]) + "\n", encoding="utf-8")
        second = MessageLog("sess-1", str(path))
        assert second.seq == 2
        assert second.question("1").state == comms.STATE_ANSWERED
        _, accepted = second.append(**human(CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "yes", in_reply_to="1"))
        assert accepted == ACCEPTED_DUPLICATE
        message, _ = second.append(**agent(KIND_NOTE, "continues"))
        assert message.seq == 3
        assert json.loads(path.read_text().splitlines()[-1])["seq"] == 3

    def test_no_path_keeps_the_bus_in_memory(self):
        log = MessageLog("sess-1", None)
        log.append(**agent(KIND_NOTE, "n"))
        assert log.health()["log"] is None and log.health()["seq"] == 1

    def test_listeners_run_after_publication_and_cannot_break_the_bus(self, log, capsys):
        seen = []
        log.add_listener(lambda m, a: seen.append((m.seq, a)))
        log.add_listener(lambda m, a: 1 / 0)
        log.append(**agent(KIND_NOTE, "n"))
        assert seen == [(1, ACCEPTED_NEW)]
        assert "comms listener failed" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Relay text
# ---------------------------------------------------------------------------


class TestRelayPayload:
    def test_inbound_message_is_tagged_and_the_tag_takes_column_one(self, log):
        message, _ = log.append(**human(CHANNEL_SLACK, "vegu", "t1", KIND_MESSAGE, "!206 was merged"))
        text, tagged = comms.relay_payload(message, None)
        assert (text, tagged) == ("[slack · vegu] !206 was merged", True)

    def test_answer_carries_the_question_head(self, log):
        question, _ = log.append(**agent(KIND_QUESTION, "target branch?\nmore detail"))
        answer, _ = log.append(**human(CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "main", in_reply_to="1"))
        text, _ = comms.relay_payload(answer, question)
        assert text == "[slack · vegu] (answer to #1 'target branch?') main"
        late, _ = log.append(**human("matrix", "@a:x", "e1", KIND_ANSWER, "prep", in_reply_to="1"))
        text, _ = comms.relay_payload(late, question)
        assert text == (
            f"[matrix · @a:x] (late answer to #1 'target branch?', already answered by {answer.id}) prep"
        )

    def test_a_long_question_head_is_clipped(self, log):
        question, _ = log.append(**agent(KIND_QUESTION, "q" * 200))
        answer, _ = log.append(**human(CHANNEL_SLACK, "v", "t", KIND_ANSWER, "a", in_reply_to="1"))
        label = comms.relay_label(answer, question)
        assert "…" in label and len(label) < 140

    def test_command_is_untagged(self, log):
        message, _ = log.append(**human(CHANNEL_UBER, None, "r1", KIND_COMMAND, "/followup"))
        assert comms.relay_payload(message, None) == ("/followup", False)


# ---------------------------------------------------------------------------
# Terminal tracker
# ---------------------------------------------------------------------------


class TestTerminalTracker:
    def test_plain_typing_with_backspace_and_enter(self):
        tracker = TerminalTracker()
        assert tracker.feed(b"hel\x7flo wor") == []
        assert tracker.feed(b"ld\r") == ["helo world"]

    def test_escape_sequences_are_dropped_and_split_reads_survive(self):
        tracker = TerminalTracker()
        assert tracker.feed(b"ab\x1b[") == []
        assert tracker.feed(b"Dc\r") == ["abc"]

    def test_first_column_escapes_are_the_human_driving_the_tui(self):
        tracker = TerminalTracker()
        assert tracker.feed(b"/quit\r!ls\r#note\r ok\r") == ["ok"]

    def test_ctrl_c_and_ctrl_u_clear_the_line(self):
        tracker = TerminalTracker()
        assert tracker.feed(b"abandon\x03kept\r") == ["kept"]
        assert tracker.feed(b"abandon\x15kept too\r") == ["kept too"]

    def test_bracketed_paste_is_taken_verbatim_with_newlines(self):
        tracker = TerminalTracker()
        assert tracker.feed(b"\x1b[200~line one\rline two\x1b[201~\r") == ["line one\nline two"]

    def test_utf8_backspace_removes_one_character(self):
        tracker = TerminalTracker()
        assert tracker.feed("héllo\x7f\x7f\r".encode("utf-8")) == ["hél"]

    def test_blank_lines_emit_nothing(self):
        assert TerminalTracker().feed(b"\r\r   \r") == []


# ---------------------------------------------------------------------------
# FastAPI routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("manual_start,start_command,held,start_prompt", [
    (False, "/start", True, ""), (True, "/start", False, ""), (False, "", False, ""),
    (False, "/start", True, "seed direction"),
])
def test_launcher_submits_start_before_accepted_inbound_records(tmp_path, monkeypatch, manual_start, start_command, held, start_prompt):
    from fastapi.testclient import TestClient
    import os

    log = MessageLog("startup", str(tmp_path / "messages.jsonl"))
    submitted = []
    observed = {}
    monkeypatch.setattr(supervisor, "_open_comms_log", lambda: log)
    monkeypatch.setattr(supervisor, "_open_ask_projection", lambda log: None)
    monkeypatch.setattr(supervisor, "_resolve_fastapi_port", lambda *args: 8701)
    monkeypatch.setattr(supervisor, "_make_submit", lambda *args, **kwargs: (
        lambda text: (submitted.append(text) or len(text.encode()) + 1, supervisor.SUBMIT_TEXT_READ)
    ))
    monkeypatch.setenv("LMER_COMMS_DRAIN_GRACE", "0")
    monkeypatch.setattr(supervisor, "_inject_start_prompt", lambda write, text, *args: submitted.append(text))

    def serve(app, *args):
        client = TestClient(app)
        observed["client"] = client
        response = client.post("/messages", json={
            "kind": "message", "text": "earlier", "source": {"channel": "slack"},
        }, headers={"Authorization": "Bearer startup-control"})
        observed["status"] = response.status_code
        observed["accepted"] = [record.text for record in log.read_since(0)[0]]
        observed["typed_before_start"] = list(submitted)
        return None, lambda: None

    monkeypatch.setattr(supervisor, "_start_fastapi_server", serve)
    start_thread = supervisor._start_auto_start_thread

    def start_and_join(*args, **kwargs):
        thread = start_thread(*args, **kwargs)
        thread.join(timeout=3)
        observed["startup_finished"] = not thread.is_alive()
        response = observed["client"].post("/messages", json={
            "kind": "message", "text": "later", "source": {"channel": "matrix"},
        }, headers={"Authorization": "Bearer startup-control"})
        observed["later_status"] = response.status_code
        observed["typed_after_start"] = list(submitted)
        return thread

    monkeypatch.setattr(supervisor, "_start_auto_start_thread", start_and_join)
    options = {
        "fastapi": True, "manual_start": manual_start, "host": "127.0.0.1",
        "token": "startup-control", "port_range": (8700, 8799),
        "auto_start_delay": 0, "auto_start_nudge_delay": 0,
        "auto_start_ready_marker": b"", "auto_start_ready_timeout": 0,
        "auto_start_settle_delay": 0, "winsize_recheck_delay": 0,
        "start_command": start_command, "start_prompt": start_prompt, "start_prompt_delay": 0,
    }
    with open(os.devnull, "rb") as stdin, open(os.devnull, "wb") as stdout:
        assert supervisor.run_supervisor(["true"], options, stdin_fd=stdin.fileno(), stdout_fd=stdout.fileno()) == 0
    assert observed["status"] == 200 and observed["accepted"] == ["earlier"]
    assert observed["typed_before_start"] == ([] if held else ["[slack] earlier"])
    if not manual_start:
        assert observed["startup_finished"] and observed["later_status"] == 200
        expected = (["/start"] if held else []) + ([start_prompt] if start_prompt else []) + ["[slack] earlier", "[matrix] later"]
        assert observed["typed_after_start"] == expected
        assert submitted == expected


def test_bus_acceptance_continues_while_startup_queue_is_flushing(tmp_path):
    from fastapi.testclient import TestClient
    import threading

    log = MessageLog("startup", str(tmp_path / "messages.jsonl"))
    writing, release, accepted = threading.Event(), threading.Event(), threading.Event()
    submitted = []

    def submit(text):
        if text == "[slack] first":
            writing.set()
            assert release.wait(3)
        submitted.append(text)
        return len(text) + 1, supervisor.SUBMIT_TEXT_READ

    relay = supervisor._InboundRelay(log, submit, "claude", startup_pending=True)
    app = supervisor._build_fastapi_app(
        supervisor.OutputBuffer(), lambda data: len(data), "test-token",
        comms=log, submit=submit, inbound_relay=relay,
    )
    client = TestClient(app)
    def post(text):
        return client.post("/messages", json={
            "kind": "message", "text": text, "source": {"channel": "slack"},
        }, headers=AUTH)
    assert post("first").status_code == 200
    log.add_listener(lambda message, verdict: accepted.set() if message.text == "second" else None)
    flush = threading.Thread(target=relay.startup_submitted)
    later = threading.Thread(target=lambda: post("second"))
    flush.start()
    try:
        assert writing.wait(2)
        later.start()
        assert accepted.wait(2), "bus acceptance waited for the PTY write"
        assert [record.text for record in log.read_since(0)[0]] == ["first", "second"]
        assert submitted == []
    finally:
        release.set()
        flush.join(timeout=3)
        if later.ident is not None:
            later.join(timeout=3)
    assert not flush.is_alive() and not later.is_alive()
    assert submitted == ["[slack] first", "[slack] second"]


def test_startup_hold_accepts_all_inbound_kinds_and_flushes_in_bus_order(tmp_path):
    from fastapi.testclient import TestClient

    log = MessageLog("startup", str(tmp_path / "messages.jsonl"))
    submitted = []
    submit = lambda text: (submitted.append(text) or len(text) + 1, supervisor.SUBMIT_TEXT_READ)
    relay = supervisor._InboundRelay(log, submit, "claude", startup_pending=True)
    app = supervisor._build_fastapi_app(
        supervisor.OutputBuffer(), lambda data: len(data), "test-token",
        comms=log, submit=submit, inbound_relay=relay,
    )
    client = TestClient(app)
    question, _ = log.append(kind="question", text="which branch?", source=Source("agent"))
    payloads = [
        {"kind": "message", "text": "earlier", "source": {"channel": "slack", "ref": "m1"}},
        {"kind": "answer", "text": "feature", "source": {"channel": "matrix"}, "in_reply_to": question.id},
        {"kind": "command", "text": "/followup", "source": {"channel": "platform"}},
    ]
    for payload in payloads:
        response = client.post("/messages", json=payload, headers=AUTH)
        assert response.status_code == 200
        assert response.json()["relay"] == {"queued": True, "bytes_written": 0}
    assert submitted == []
    assert len(client.get("/messages", headers=AUTH).json()["messages"]) == 4
    assert client.post("/messages", json=payloads[0], headers=AUTH).json()["relay"] is None
    relay.startup_submitted()
    assert submitted[0] == "[slack] earlier"
    assert "feature" in submitted[1]
    assert submitted[2] == "/followup"
    relay.startup_submitted()
    assert len(submitted) == 3
    response = client.post("/messages", json={
        "kind": "message", "text": "later", "source": {"channel": "slack"},
    }, headers=AUTH)
    assert response.status_code == 200 and submitted[-1] == "[slack] later"
    assert response.json()["relay"]["bytes_written"] > 0


class TestRoutes:
    def _build(self, tmp_path, comms_log=True):
        buf = supervisor.OutputBuffer(limit=1024)
        sink: list[bytes] = []

        def write_input(data: bytes) -> int:
            sink.append(bytes(data))
            return len(data)

        log = MessageLog("sess-1", str(tmp_path / "messages.jsonl")) if comms_log else None
        app = supervisor._build_fastapi_app(buf, write_input, "test-token", comms=log)
        from fastapi.testclient import TestClient
        return TestClient(app), log, sink

    def _build_two_identities(self, tmp_path):
        """A plane built the way `run_supervisor` builds one: the control
        credential verified by its hash, the session holding a token of its own."""
        import hashlib

        buf = supervisor.OutputBuffer(limit=1024)
        sink: list[bytes] = []

        def write_input(data: bytes) -> int:
            sink.append(bytes(data))
            return len(data)

        log = MessageLog("sess-1", str(tmp_path / "messages.jsonl"))
        control = "control-token-value"
        app = supervisor._build_fastapi_app(
            buf, write_input, "", comms=log,
            agent_token="agent-token-value",
            token_hash=hashlib.sha256(control.encode("utf-8")).hexdigest(),
        )
        from fastapi.testclient import TestClient
        return TestClient(app), log, sink, {"Authorization": f"Bearer {control}"}, \
            {"Authorization": "Bearer agent-token-value"}

    def test_the_session_may_not_post_as_a_human_on_another_channel(self, tmp_path):
        """§3.2: the source is stamped by the party that vouches for it. With one
        shared token a session could put `[slack · vegu] approved, ship it` in
        front of every other channel and the fleet assistant."""
        client, log, _, control, agent = self._build_two_identities(tmp_path)
        forged = client.post("/messages", json={
            "kind": "message", "text": "approved, ship it",
            "source": {"channel": "slack", "author": "vegu"},
        }, headers=agent)
        assert forged.status_code == 403
        assert "may only be the agent's own" in forged.json()["detail"]
        assert client.post("/messages", json={
            "kind": "command", "text": "/followup", "source": {"channel": "platform"},
        }, headers=agent).status_code == 403
        assert client.post("/messages", json={
            "kind": "note", "text": "mine", "source": {"channel": "agent", "author": "someone"},
        }, headers=agent).status_code == 403
        assert [m.text for m in log.read_since(0)[0]] == []

    def test_the_session_posts_as_the_agent_and_the_holder_of_the_control_token_speaks_for_a_channel(self, tmp_path):
        client, log, _, control, agent = self._build_two_identities(tmp_path)
        mine = client.post("/messages", json={"kind": "note", "text": "working"}, headers=agent)
        assert mine.status_code == 200
        assert mine.json()["message"]["source"]["channel"] == "agent"
        theirs = client.post("/messages", json={
            "kind": "message", "text": "hello", "source": {"channel": "slack", "author": "vegu"},
        }, headers=control)
        assert theirs.status_code == 200
        assert theirs.json()["message"]["source"] == {"channel": "slack", "author": "vegu"}

    def test_the_control_token_itself_still_authenticates_when_the_host_sent_one(self, tmp_path):
        """An older host CLI hands the token down rather than its hash; the plane
        it builds has no separate session credential and behaves as before."""
        client, log, _ = self._build(tmp_path)
        assert client.post("/messages", json={
            "kind": "message", "text": "hi", "source": {"channel": "platform", "author": "operator"},
        }, headers=AUTH).status_code == 200

    def test_a_wrong_credential_is_refused_by_both_shapes(self, tmp_path):
        client, _, _, control, agent = self._build_two_identities(tmp_path)
        assert client.get("/messages", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert client.get("/messages", headers=control).status_code == 200
        assert client.get("/messages", headers=agent).status_code == 200

    def test_the_session_may_not_ack_for_a_subscriber(self, tmp_path):
        """An ack ends the drain grace over records the daemon may not hold."""
        client, log, _, control, agent = self._build_two_identities(tmp_path)
        log.append(kind="note", text="x", source=Source("agent"))
        assert client.post("/messages/ack", json={"subscriber": "platform", "seq": 1},
                           headers=agent).status_code == 403
        assert client.post("/messages/ack", json={"subscriber": "platform", "seq": 1},
                           headers=control).status_code == 200

    def test_routes_answer_503_without_a_bus(self, tmp_path):
        client, _, _ = self._build(tmp_path, comms_log=False)
        assert client.get("/messages", headers=AUTH).status_code == 503
        assert client.post("/messages", json={"kind": "note", "text": "x"}, headers=AUTH).status_code == 503
        assert client.get("/healthz", headers=AUTH).json()["comms"] is None

    def test_routes_require_the_bearer_token(self, tmp_path):
        client, _, _ = self._build(tmp_path)
        assert client.get("/messages").status_code == 401
        assert client.post("/messages", json={"kind": "note", "text": "x"}).status_code == 401
        assert client.post("/messages/ack", json={"subscriber": "p", "seq": 1}).status_code == 401
        assert client.get("/messages/questions").status_code == 401

    def test_agent_post_defaults_to_the_agent_channel_and_is_not_relayed(self, tmp_path):
        client, _, sink = self._build(tmp_path)
        resp = client.post("/messages", json={"kind": "note", "text": "working"}, headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted_as"] == ACCEPTED_NEW
        assert body["message"]["source"] == {"channel": CHANNEL_AGENT}
        assert body["relay"] is None and sink == []

    def test_inbound_message_is_typed_once_with_its_tag_and_a_receipt(self, tmp_path):
        client, _, sink = self._build(tmp_path)
        resp = client.post("/messages", json={
            "kind": "message", "text": "!206 was merged",
            "source": {"channel": "slack", "author": "vegu", "ref": "1725.1"},
        }, headers=AUTH)
        body = resp.json()
        typed = "[slack · vegu] !206 was merged"
        assert body["relay"]["tagged"] is True
        assert body["relay"]["payload_sha256"] == hashlib.sha256(typed.encode()).hexdigest()
        assert body["relay"]["payload_length"] == len(typed.encode())
        assert body["relay"]["submit_text"] in (
            supervisor.SUBMIT_TEXT_READ, supervisor.SUBMIT_TEXT_UNREAD, supervisor.SUBMIT_TEXT_UNKNOWN,
        )
        # The text (framed as a paste for the claude default, or bare) then the
        # Enter as its own write — exactly the /input contract.
        assert sink[-1] == b"\r"
        assert typed.encode() in b"".join(sink)
        assert b"".join(sink).count(typed.encode()) == 1

    def test_duplicate_post_is_not_typed_again(self, tmp_path):
        client, _, sink = self._build(tmp_path)
        payload = {"kind": "message", "text": "hi", "source": {"channel": "slack", "ref": "t1"}}
        first = client.post("/messages", json=payload, headers=AUTH).json()
        writes = len(sink)
        second = client.post("/messages", json=payload, headers=AUTH).json()
        assert second["accepted_as"] == ACCEPTED_DUPLICATE
        assert second["message"]["id"] == first["message"]["id"]
        assert second["relay"] is None and len(sink) == writes

    def test_command_is_typed_untagged(self, tmp_path):
        client, _, sink = self._build(tmp_path)
        resp = client.post("/messages", json={
            "kind": "command", "text": "/followup", "source": {"channel": "uber-lmer"},
        }, headers=AUTH)
        assert resp.status_code == 200 and resp.json()["relay"]["tagged"] is False
        assert b"/followup" in b"".join(sink)
        assert b"[" not in b"".join(sink).replace(b"\x1b[", b"")

    def test_command_from_a_human_channel_is_refused(self, tmp_path):
        client, _, sink = self._build(tmp_path)
        resp = client.post("/messages", json={
            "kind": "command", "text": "/followup", "source": {"channel": "slack"},
        }, headers=AUTH)
        assert resp.status_code == 400 and sink == []

    def test_question_answer_flow_over_http(self, tmp_path):
        client, log, sink = self._build(tmp_path)
        q = client.post("/messages", json={
            "kind": "question", "text": "branch?", "options": ["main", "prep"],
        }, headers=AUTH).json()["message"]
        assert client.get("/messages/questions?open=true", headers=AUTH).json()["questions"][0]["state"] == "open"

        first = client.post("/messages", json={
            "kind": "answer", "text": "prep", "in_reply_to": q["id"],
            "source": {"channel": "slack", "author": "vegu", "ref": "t1"},
        }, headers=AUTH).json()
        assert first["accepted_as"] == ACCEPTED_NEW
        assert first["message"]["meta"]["delivered"] == DELIVERY_PTY
        assert b"(answer to #1 'branch?') prep" in b"".join(sink)

        late = client.post("/messages", json={
            "kind": "answer", "text": "main", "in_reply_to": "1",
            "source": {"channel": "matrix", "author": "@a:x", "ref": "e1"},
        }, headers=AUTH).json()
        assert late["accepted_as"] == ACCEPTED_LATE
        assert b"late answer to #1" in b"".join(sink)

        page = client.get("/messages?since=0", headers=AUTH).json()
        assert page["cursor"] == 3 and page["schema"] == comms.SCHEMA_VERSION
        assert [m["kind"] for m in page["messages"]] == ["question", "answer", "answer"]
        page = client.get("/messages?since=1&kinds=answer", headers=AUTH).json()
        assert len(page["messages"]) == 2

    def test_a_waiting_cli_takes_the_answer_and_nothing_is_typed(self, tmp_path):
        client, log, sink = self._build(tmp_path)
        q = client.post("/messages", json={"kind": "question", "text": "go?"}, headers=AUTH).json()["message"]
        result = {}

        def wait():
            result["page"] = client.get(
                f"/messages?since={q['seq']}&timeout=5&kinds=answer&waiter={q['seq']}", headers=AUTH
            ).json()

        thread = threading.Thread(target=wait)
        thread.start()
        deadline = time.monotonic() + 2.0
        while q["id"] not in log._waiters and time.monotonic() < deadline:
            time.sleep(0.01)
        answer = client.post("/messages", json={
            "kind": "answer", "text": "yes", "in_reply_to": q["id"],
            "source": {"channel": "platform", "author": "operator"},
        }, headers=AUTH).json()
        thread.join(timeout=5.0)
        assert answer["message"]["meta"]["delivered"] == DELIVERY_CLI
        assert answer["relay"] is None and sink == []
        assert [m["text"] for m in result["page"]["messages"]] == ["yes"]

    def test_an_option_index_from_a_control_credential_reaches_the_waiter_resolved(
        self, tmp_path
    ):
        """#428: `lmer-ctl answer <id> <qid> --option 2` is what the operator's
        `2` becomes, and the waiting `lmer-signal ask` prints the option."""
        client, log, sink, control, agent_header = self._build_two_identities(tmp_path)
        question = client.post("/messages", json={
            "kind": "question", "text": "which branch?", "options": ["red", "blue", "green"],
        }, headers=agent_header).json()["message"]
        result = {}

        def wait():
            result["page"] = client.get(
                f"/messages?since={question['seq']}&timeout=5&kinds=answer"
                f"&waiter={question['seq']}", headers=agent_header,
            ).json()

        thread = threading.Thread(target=wait)
        thread.start()
        deadline = time.monotonic() + 2.0
        while question["id"] not in log._waiters and time.monotonic() < deadline:
            time.sleep(0.01)
        posted = client.post("/messages", json={
            "kind": "answer", "option": 2, "in_reply_to": question["id"],
            "source": {"channel": "platform", "author": "operator"},
        }, headers=control).json()
        thread.join(timeout=5.0)
        assert posted["message"]["text"] == "blue"
        assert posted["message"]["meta"]["option"] == 2
        assert posted["message"]["meta"]["delivered"] == DELIVERY_CLI
        assert [m["text"] for m in result["page"]["messages"]] == ["blue"]
        assert sink == [], "delivered to the waiter, so nothing is typed in"

    def test_an_out_of_range_index_is_a_400_naming_the_count(self, tmp_path):
        client, _, _ = self._build(tmp_path)
        client.post("/messages", json={
            "kind": "question", "text": "which branch?", "options": ["red", "blue"],
        }, headers=AUTH)
        refused = client.post("/messages", json={
            "kind": "answer", "option": 5, "in_reply_to": "1",
            "source": {"channel": "platform"},
        }, headers=AUTH)
        assert refused.status_code == 400
        assert "not one of the 2" in refused.json()["detail"]

    @pytest.mark.parametrize("value", [True, False, "2", 1.0, 1.7, "abc"])
    def test_the_route_refuses_a_non_integer_index_instead_of_coercing_it(
        self, tmp_path, value
    ):
        """Round 2, major: pydantic's lax mode turned `true` into `1`, so the
        route answered with the FIRST option and `_clean_option`'s bool guard
        never ran. `strict=True` is the only layer that still has the raw value —
        a test calling `append` directly passes either way."""
        client, log, _ = self._build(tmp_path)
        client.post("/messages", json={
            "kind": "question", "text": "which branch?", "options": ["main", "prep"],
        }, headers=AUTH)
        refused = client.post("/messages", json={
            "kind": "answer", "option": value, "in_reply_to": "1",
            "source": {"channel": "platform"},
        }, headers=AUTH)
        assert refused.status_code == 422
        assert [m.kind for m in log.read_since(0)[0]] == [KIND_QUESTION]

    def test_the_route_still_takes_a_whole_number_index(self, tmp_path):
        client, _, _ = self._build(tmp_path)
        client.post("/messages", json={
            "kind": "question", "text": "which branch?", "options": ["main", "prep"],
        }, headers=AUTH)
        posted = client.post("/messages", json={
            "kind": "answer", "option": 2, "in_reply_to": "1",
            "source": {"channel": "platform"},
        }, headers=AUTH)
        assert posted.status_code == 200
        assert posted.json()["message"]["text"] == "prep"

    def test_the_route_refuses_an_index_beside_words(self, tmp_path):
        client, _, _ = self._build(tmp_path)
        client.post("/messages", json={
            "kind": "question", "text": "which branch?", "options": ["main", "prep"],
        }, headers=AUTH)
        refused = client.post("/messages", json={
            "kind": "answer", "text": "prep, but rebase first", "option": 2,
            "in_reply_to": "1", "source": {"channel": "platform"},
        }, headers=AUTH)
        assert refused.status_code == 400
        assert "not both" in refused.json()["detail"]

    def test_the_receipt_covers_what_the_caller_sent_not_what_was_stored(self, tmp_path):
        """#197's receipt answers "did my bytes reach the session?", so it is
        over the received body. Resolving an index rewrites `text`, and hashing
        the stored record made `CommsHub.post` journal `receipt_match: false`
        against deliveries that had in fact succeeded."""
        client, _, _ = self._build(tmp_path)
        client.post("/messages", json={
            "kind": "question", "text": "which branch?", "options": ["red", "blue"],
        }, headers=AUTH)
        posted = client.post("/messages", json={
            "kind": "answer", "text": "", "option": 2, "in_reply_to": "1",
            "source": {"channel": "platform", "author": "operator"},
        }, headers=AUTH).json()
        assert posted["message"]["text"] == "blue"
        assert posted["receipt"] == {
            "payload_sha256": hashlib.sha256(b"").hexdigest(), "payload_length": 0,
        }
        plain = client.post("/messages", json={
            "kind": "message", "text": "hello", "source": {"channel": "platform"},
        }, headers=AUTH).json()
        assert plain["receipt"]["payload_sha256"] == hashlib.sha256(b"hello").hexdigest()

    def test_waiter_must_name_a_question(self, tmp_path):
        client, _, _ = self._build(tmp_path)
        assert client.get("/messages?waiter=7", headers=AUTH).status_code == 400

    def test_bad_records_are_400_with_the_reason(self, tmp_path):
        client, _, _ = self._build(tmp_path)
        resp = client.post("/messages", json={"kind": "note", "text": ""}, headers=AUTH)
        assert resp.status_code == 400 and "empty" in resp.json()["detail"]
        resp = client.post("/messages", json={"kind": "answer", "text": "x", "in_reply_to": "5",
                                              "source": {"channel": "slack"}}, headers=AUTH)
        assert resp.status_code == 400

    def test_ack_and_healthz(self, tmp_path):
        client, _, _ = self._build(tmp_path)
        client.post("/messages", json={"kind": "note", "text": "n"}, headers=AUTH)
        resp = client.post("/messages/ack", json={"subscriber": "platform", "seq": 1}, headers=AUTH)
        assert resp.json() == {"subscribers": {"platform": 1}}
        health = client.get("/healthz", headers=AUTH).json()["comms"]
        assert health["schema"] == comms.SCHEMA_VERSION and health["seq"] == 1
        assert health["subscribers"] == {"platform": 1}
        assert health["log"].endswith("messages.jsonl")
        # Which session this is: a connector and a thread servicer both ask
        # here rather than being told by a launch argument (#427).
        assert health["session"] == "sess-1"

    def test_raw_keystrokes_reach_the_terminal_tracker_with_their_author(self, tmp_path):
        buf = supervisor.OutputBuffer(limit=1024)
        seen = []
        app = supervisor._build_fastapi_app(
            buf, lambda data: len(data), "test-token",
            on_raw_input=lambda data, author: seen.append((bytes(data), author)),
        )
        from fastapi.testclient import TestClient
        client = TestClient(app)
        client.post("/input", json={"data": "h", "author": "vegu"}, headers=AUTH)
        client.post("/input", json={"data": "x", "append_newline": True}, headers=AUTH)
        assert seen == [(b"h", "vegu")]


# ---------------------------------------------------------------------------
# Wiring helpers and the ask-dir projection
# ---------------------------------------------------------------------------


class TestWiring:
    def test_open_comms_log_uses_the_mounted_directory_and_exports_the_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMER_SESSION_ID", "sess-9")
        # The call below exports the path into this process's environment for
        # the hooks; recorded here so it is restored rather than left for every
        # later test in the session (the signal guard reads it).
        monkeypatch.delenv(comms.COMMS_LOG_ENV, raising=False)
        log = supervisor._open_comms_log(str(tmp_path))
        assert log.session == "sess-9"
        assert log.path == str(tmp_path / comms.COMMS_LOG_NAME)
        assert os.environ[comms.COMMS_LOG_ENV] == log.path
        log.close()

    def test_open_comms_log_without_a_mount_stays_in_memory(self, tmp_path, monkeypatch):
        monkeypatch.delenv(comms.COMMS_LOG_ENV, raising=False)
        log = supervisor._open_comms_log(str(tmp_path / "absent"))
        assert log.path is None and log.session.startswith("local-")
        assert comms.COMMS_LOG_ENV not in os.environ

    def test_projection_is_only_opened_for_a_mounted_channel(self, tmp_path, monkeypatch):
        log = MessageLog("sess-1", None)
        monkeypatch.setenv(protocol.ASK_DIR_ENV, str(tmp_path / "absent"))
        assert supervisor._open_ask_projection(log) is None
        channel = tmp_path / "ask"
        channel.mkdir()
        monkeypatch.setenv(protocol.ASK_DIR_ENV, str(channel))
        assert supervisor._open_ask_projection(log) is not None

    def test_terminal_mirror_kill_switch(self, monkeypatch):
        monkeypatch.delenv(comms.TERMINAL_MIRROR_ENV, raising=False)
        assert comms.terminal_mirror_enabled() is True
        assert comms.terminal_mirror_enabled({comms.TERMINAL_MIRROR_ENV: "0"}) is False
        assert comms.terminal_mirror_enabled({comms.TERMINAL_MIRROR_ENV: "yes"}) is True


class TestAskDirProjection:
    @pytest.fixture
    def channel(self, tmp_path):
        directory = tmp_path / "ask"
        directory.mkdir()
        return directory

    @pytest.fixture
    def projected(self, channel, tmp_path):
        log = MessageLog("sess-1", str(tmp_path / "messages.jsonl"))
        projection = comms.AskDirProjection(str(channel), log)
        log.add_listener(projection.mirror)
        return log, projection

    def test_bus_records_appear_as_the_old_file_shapes_under_their_seq(self, projected, channel):
        log, _ = projected
        log.append(**agent(KIND_NOTE, "progress"))
        log.append(**agent(KIND_QUESTION, "branch?", options=["main"]))
        log.append(**agent(KIND_MILESTONE, "pushed MR"))
        log.append(**human(CHANNEL_SLACK, "vegu", "t1", KIND_MESSAGE, "hi"))  # not an entry
        names = sorted(os.listdir(channel))
        assert names == ["000001.note.json", "000002.question.json", "000003.signal.json"]
        entries = protocol.read_entries(channel)
        assert [(e.id, e.kind, e.text) for e in entries] == [
            ("000001", "note", "progress"), ("000002", "question", "branch?"),
        ]
        assert entries[1].options == ("main",)
        (signal,) = protocol.read_signals(channel)
        assert (signal.id, signal.text) == ("000003", "pushed MR")

    def test_an_accepted_answer_is_mirrored_with_its_receipt_and_a_close_too(self, projected, channel):
        log, _ = projected
        log.append(**agent(KIND_QUESTION, "a?"))
        log.append(**agent(KIND_QUESTION, "b?"))
        log.append(**human(CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "yes", in_reply_to="1"))
        log.append(**human("matrix", "@a:x", "e1", KIND_ANSWER, "late", in_reply_to="1"))
        log.append(**agent(KIND_CLOSE, "moved on", in_reply_to="2"))
        first, second = protocol.read_entries(channel)
        assert first.answered and first.answer.text == "yes" and first.answer.source == "slack:vegu"
        assert first.answer_read, "delivery is guaranteed by the bus, so old readers see it read"
        assert second.closed and second.closure.reason == "moved on" and not second.answered

    def test_an_old_daemon_s_answer_file_is_lifted_onto_the_bus_once(self, projected, channel):
        log, projection = projected
        question, _ = log.append(**agent(KIND_QUESTION, "branch?"))
        (entry,) = protocol.read_entries(channel)
        protocol.write_answer(channel, entry, "prep-release")
        lifted = projection.lift_foreign_records()
        assert len(lifted) == 1
        answer, accepted = lifted[0]
        assert accepted == ACCEPTED_NEW
        assert answer.source == Source(CHANNEL_PLATFORM, author="operator", ref="ask-dir:000001.answer.json")
        assert answer.in_reply_to == question.id and answer.meta["delivered"] == DELIVERY_PTY
        assert log.question("1").state == comms.STATE_ANSWERED
        assert projection.lift_foreign_records() == []
        # The mirror does not re-write the answer it just lifted, but it does
        # file the receipt: the bus delivered it, so the old daemon must not
        # see "answered, unread".
        assert sorted(os.listdir(channel)) == [
            "000001.answer.json", "000001.question.json", "000001.read.json",
        ]
        assert protocol.read_entries(channel)[0].answer_read

    def test_an_answer_file_naming_an_option_is_lifted_resolved(self, projected, channel):
        """#428: the lift goes through `append`, so an answer filed with an
        `option` resolves like one posted on the bus — and one carrying a bare
        number is lifted as the words it is."""
        log, projection = projected
        log.append(**agent(KIND_QUESTION, "which branch?", options=["red", "blue", "green"]))
        log.append(**agent(KIND_QUESTION, "and which host?", options=["a", "b"]))
        first, second = protocol.read_entries(channel)
        # What the platform's file leg writes: the option resolved to its text,
        # because this file's reader may be a session with no bus, plus the
        # index so the lift can say the choice was named rather than typed.
        protocol.write_answer(channel, first, "blue", option=2)
        protocol.write_answer(channel, second, "2")
        lifted = projection.lift_foreign_records()
        assert [(m.text, m.meta.get("option")) for m, _ in lifted] == [("blue", 2), ("2", None)]

    def test_the_projection_s_own_answer_files_are_never_lifted(self, projected, channel):
        log, projection = projected
        log.append(**agent(KIND_QUESTION, "q"))
        log.append(**human(CHANNEL_SLACK, "vegu", "t1", KIND_ANSWER, "yes", in_reply_to="1"))
        assert projection.lift_foreign_records() == []
        assert log.seq == 2

    def test_stray_files_are_ignored(self, projected, channel):
        log, projection = projected
        (channel / "notes.txt").write_text("hello")
        (channel / "000007.answer.json").write_text("{}")
        assert projection.lift_foreign_records() == []


# ---------------------------------------------------------------------------
# Issue #421: a session with a bus whose in-tree CLI predates it writes only
# into the ask directory. The projection lifts what it finds there, so the
# daemon sees an ordinary bus session and needs no rule of its own.
# ---------------------------------------------------------------------------


class TestOldPathRecordsAreLifted:
    @pytest.fixture
    def projected(self, tmp_path):
        channel = tmp_path / "ask"
        channel.mkdir()
        log = MessageLog("sess-1", str(tmp_path / "messages.jsonl"))
        projection = comms.AskDirProjection(str(channel), log)
        log.add_listener(projection.mirror)
        return log, projection, channel

    def test_every_kind_the_old_cli_files_reaches_the_bus_as_the_session_s_own(
        self, projected
    ):
        log, projection, channel = projected
        protocol.post_note(channel, "cloning")
        protocol.post_signal(channel, "opened MR !294")
        protocol.post_question(channel, "which branch?", ["prep-release"])

        lifted = projection.lift_foreign_records()
        assert [(m.kind, m.text) for m, _ in lifted] == [
            (KIND_NOTE, "cloning"),
            (KIND_MILESTONE, "opened MR !294"),
            (KIND_QUESTION, "which branch?"),
        ]
        assert [m.source.channel for m, _ in lifted] == [CHANNEL_AGENT] * 3
        assert [m.source.ref for m, _ in lifted] == [
            "ask-dir:000001.note.json",
            "ask-dir:000002.signal.json",
            "ask-dir:000003.question.json",
        ]
        assert lifted[2][0].options == ("prep-release",)

    def test_a_pre_bus_asker_reads_the_option_its_question_was_answered_with(
        self, projected
    ):
        """#428 review, finding 3: the direction the doc paragraph was about. The
        CLI that asked predates the bus and polls its own file, so what has to
        carry the option's text is that file — the platform's leg resolves before
        it writes (`ask.answer_question`), and the lift then says the choice was
        named rather than typed."""
        log, projection, channel = projected
        question = protocol.post_question(channel, "which branch?", ["main", "prep-release"])
        (lifted_question, _), = projection.lift_foreign_records()

        protocol.write_answer(channel, question, "prep-release", option=2)
        ((answer, accepted),) = projection.lift_foreign_records()

        assert protocol.answer_for(channel, question).text == "prep-release"
        assert accepted == ACCEPTED_NEW
        assert (answer.text, answer.meta["option"]) == ("prep-release", 2)
        assert answer.in_reply_to == lifted_question.id

    def test_a_foreign_writer_s_bad_index_costs_the_provenance_not_the_answer(
        self, projected
    ):
        """Round 2, minor: `append` range-checked the provenance index and raised,
        and `_lifted` had already taken the name — so the session read its answer
        while the bus showed the question open for ever, on one stderr line.
        Nothing in the tree writes a bad index; the ask directory exists to
        absorb writers that are not in the tree."""
        log, projection, channel = projected
        question = protocol.post_question(channel, "which branch?", ["main", "prep"])
        (lifted_question, _), = projection.lift_foreign_records()
        # Straight to disk: `write_answer` now refuses this index, and the point
        # is a file some other writer produced.
        path = channel / f"{question.id}{protocol.ANSWER_SUFFIX}"
        path.write_text(json.dumps({
            "schema": protocol.SCHEMA_VERSION, "kind": "answer",
            "question_id": question.id, "nonce": question.nonce, "text": "prep",
            "answered_at": protocol.utc_now_iso(), "source": "operator", "option": 5,
        }))

        ((answer, accepted),) = projection.lift_foreign_records()
        assert accepted == ACCEPTED_NEW
        assert answer.text == "prep", "the file's text is the answer"
        assert "option" not in answer.meta, "the unusable index is dropped"
        assert answer.in_reply_to == lifted_question.id
        assert log.question(lifted_question.id).state == comms.STATE_ANSWERED

    def test_no_index_in_a_foreign_file_can_strand_the_answer(self, projected):
        """Round 3, minor: `_provenance_option` dropped an out-of-range index but
        not an IN-range one contradicting the file's text, which `append` refuses
        for the other reason — same permanent strand, since `_lifted` holds the
        name before the append is tried."""
        log, projection, channel = projected
        question = protocol.post_question(channel, "which branch?", ["main", "prep-release"])
        (lifted_question, _), = projection.lift_foreign_records()
        path = channel / f"{question.id}{protocol.ANSWER_SUFFIX}"
        # In range, but the words name the other option — the second way `append`
        # refuses an index, and the one the helper did not cover.
        path.write_text(json.dumps({
            "schema": protocol.SCHEMA_VERSION, "kind": "answer",
            "question_id": question.id, "nonce": question.nonce, "text": "main",
            "answered_at": protocol.utc_now_iso(), "source": "operator", "option": 2,
        }))

        ((answer, accepted),) = projection.lift_foreign_records()
        assert accepted == ACCEPTED_NEW
        assert answer.text == "main", "the file's text is the answer"
        assert "option" not in answer.meta
        assert log.question(lifted_question.id).state == comms.STATE_ANSWERED

    def test_a_lifted_record_is_not_written_back_into_the_directory_it_came_from(
        self, projected
    ):
        log, projection, channel = projected
        # One inbound record first, so the lift's seq is 2 while the file it came
        # from is 000001. Without the gap an echo lands on the file already there
        # and FileExistsError hides it, and this test passes either way.
        log.append(**human(CHANNEL_UBER, None, "u1", KIND_MESSAGE, "how is it going?"))
        protocol.post_signal(channel, "opened MR !294")
        ((lifted, _),) = projection.lift_foreign_records()
        assert lifted.seq == 2
        assert sorted(os.listdir(channel)) == ["000001.signal.json"]

    def test_the_same_file_is_never_lifted_twice_even_by_a_new_projection(
        self, projected, tmp_path
    ):
        log, projection, channel = projected
        protocol.post_signal(channel, "opened MR !294")
        assert len(projection.lift_foreign_records()) == 1
        assert projection.lift_foreign_records() == []

        # A second supervisor over the same mount (§3.1) rebuilds from the log:
        # the ref is what tells it this file is already on the bus.
        reloaded = MessageLog("sess-1", str(tmp_path / "messages.jsonl"))
        again = comms.AskDirProjection(str(channel), reloaded)
        assert again.lift_foreign_records() == []
        assert reloaded.seq == 1

    def test_a_projection_rebuilt_over_its_own_files_lifts_none_of_them(
        self, projected, tmp_path
    ):
        log, projection, channel = projected
        log.append(**agent(KIND_NOTE, "progress"))
        log.append(**agent(KIND_MILESTONE, "pushed MR"))
        log.append(**agent(KIND_QUESTION, "branch?"))
        assert len(os.listdir(channel)) == 3

        reloaded = MessageLog("sess-1", str(tmp_path / "messages.jsonl"))
        rebuilt = comms.AskDirProjection(str(channel), reloaded)
        assert rebuilt.lift_foreign_records() == []
        assert reloaded.seq == 3

    def test_a_name_the_other_writer_took_first_is_still_lifted(self, projected):
        """The mirror's write raises and the record inside stays theirs.

        Reachable in the population this serves: a self-dev session's CLI is
        whatever the checkout holds when it runs, so one that updates mid-run
        files its early records through the directory and its later ones on the
        bus, and the bus's first milestone lands on the name the CLI took.
        """
        log, projection, channel = projected
        protocol.post_signal(channel, "opened MR !294")     # 000001.signal.json
        log.append(**agent(KIND_MILESTONE, "pushed the branch"))  # same name, refused
        assert sorted(os.listdir(channel)) == ["000001.signal.json"]

        lifted = projection.lift_foreign_records()
        assert [(m.kind, m.text) for m, _ in lifted] == [
            (KIND_MILESTONE, "opened MR !294")
        ], "the milestone in the file the mirror could not write"

    def test_a_name_the_other_writer_took_first_is_lifted_after_a_restart_too(
        self, projected, tmp_path
    ):
        """Same name **and** the same words, which is the case a comparison of
        names and texts cannot tell apart. The stamp can: the file is the
        session CLI's, and its note has never been on the bus."""
        log, projection, channel = projected
        protocol.post_note(channel, "cloning")          # 000001.note.json
        log.append(**agent(KIND_NOTE, "cloning"))       # same name, same words, refused

        reloaded = MessageLog("sess-1", str(tmp_path / "messages.jsonl"))
        rebuilt = comms.AskDirProjection(str(channel), reloaded)
        lifted = rebuilt.lift_foreign_records()
        assert [(m.kind, m.text) for m, _ in lifted] == [(KIND_NOTE, "cloning")]
        assert lifted[0][0].source.ref == "ask-dir:000001.note.json"

    def test_a_question_is_lifted_before_the_answer_filed_under_its_stem(
        self, projected
    ):
        log, projection, channel = projected
        question = protocol.post_question(channel, "which branch?")
        protocol.write_answer(channel, question, "prep-release")

        lifted = projection.lift_foreign_records()
        assert [(m.kind, m.text) for m, _ in lifted] == [
            (KIND_QUESTION, "which branch?"),
            (KIND_ANSWER, "prep-release"),
        ]
        answer = lifted[1][0]
        assert answer.in_reply_to == lifted[0][0].id
        assert answer.source.channel == CHANNEL_PLATFORM
        assert log.question(lifted[0][0].id).state == comms.STATE_ANSWERED

    def test_the_answer_to_a_lifted_question_files_no_bus_receipt(self, projected):
        """The old CLI polling the file is the reader, and it files its own
        receipt when it takes the answer — a receipt here would say it was
        delivered before it was."""
        log, projection, channel = projected
        question = protocol.post_question(channel, "which branch?")
        protocol.write_answer(channel, question, "prep-release")
        projection.lift_foreign_records()
        assert sorted(os.listdir(channel)) == [
            "000001.answer.json", "000001.question.json"
        ]

    def test_an_answer_posted_on_the_bus_lands_under_the_stem_the_cli_polls(
        self, projected
    ):
        """Uber lmer answering the lifted question on the bus. Its seq is the
        bus's, and the waiting CLI is polling the ask-dir stem."""
        log, projection, channel = projected
        question = protocol.post_question(channel, "which branch?", ["prep-release"])
        (lifted, _), = projection.lift_foreign_records()
        log.append(**human(CHANNEL_UBER, None, "u1", KIND_ANSWER, "prep-release",
                           in_reply_to=lifted.id))

        entry = protocol.read_entry(channel, question.id)
        assert entry.answered and entry.answer.text == "prep-release"
        assert entry.answer.nonce == question.nonce, "the file's own nonce, not the bus's"

    def test_a_close_filed_by_the_old_cli_closes_the_lifted_question(self, projected):
        log, projection, channel = projected
        question = protocol.post_question(channel, "which branch?")
        protocol.close_question(channel, question, reason="stopped waiting")
        lifted = projection.lift_foreign_records()
        assert [m.kind for m, _ in lifted] == [KIND_QUESTION, KIND_CLOSE]
        assert log.question(lifted[0][0].id).state == comms.STATE_CLOSED

    def _plane(self, tmp_path, name="messages.jsonl"):
        buf = supervisor.OutputBuffer(limit=1024)
        log = MessageLog("sess-1", str(tmp_path / name))
        app = supervisor._build_fastapi_app(buf, lambda data: len(data), "test-token", comms=log)
        from fastapi.testclient import TestClient

        return TestClient(app), log

    def _post_ref(self, client, ref):
        return client.post("/messages", json={
            "kind": "message", "text": "anything",
            "source": {"channel": "slack", "author": "someone", "ref": ref},
        }, headers=AUTH)

    def test_a_posted_record_may_not_claim_to_have_come_out_of_this_directory(
        self, tmp_path
    ):
        """`source.ref` is a caller's field, and this one prefix is the
        projection's own record of what it lifted. A posted one would make it
        skip that file for good: the session's question never reaches the bus,
        silently, on every start."""
        client, log = self._plane(tmp_path)
        for ref in (
            "ask-dir:000001.question.json",
            "  ask-dir:000001.question.json  ",  # the log stores what `from_dict` strips
            "ask-dir:nodot",                     # the message promises the prefix, not a shape
            "ask-dir:",
        ):
            refused = self._post_ref(client, ref)
            assert refused.status_code == 400, ref
            assert "ask-dir:" in refused.json()["detail"]
        assert log.seq == 0, "nothing durable, so nothing to repeat on every start"

        allowed = self._post_ref(client, "1725.1")
        assert allowed.status_code == 200
        assert log.read_since(0)[0][0].source.ref == "1725.1"

    def test_the_projection_s_own_refs_survive_the_reservation_and_a_reload(
        self, tmp_path
    ):
        """The regression the guard's placement is chosen for: `Source.from_dict`
        is the *read* path too, so a check inside it would strip the projection's
        refs when the log is rebuilt — and every one of them would be lifted
        again as a record nobody sent."""
        channel = tmp_path / "ask"
        channel.mkdir()
        client, log = self._plane(tmp_path)
        projection = comms.AskDirProjection(str(channel), log)
        log.add_listener(projection.mirror)

        protocol.post_question(channel, "which branch?")
        ((lifted, _),) = projection.lift_foreign_records()
        assert lifted.source.ref == "ask-dir:000001.question.json"

        reloaded = MessageLog("sess-1", str(tmp_path / "messages.jsonl"))
        assert [m.source.ref for m in reloaded.read_since(0)[0]] == [
            "ask-dir:000001.question.json"
        ], "read back through Source.from_dict, which the guard must not touch"
        rebuilt = comms.AskDirProjection(str(channel), reloaded)
        assert rebuilt.lift_foreign_records() == []

        # And the reservation is still a reservation on the same plane.
        assert self._post_ref(client, "ask-dir:000002.question.json").status_code == 400

    def test_a_close_with_no_reason_stays_a_close_with_no_reason(self, projected):
        """The bus allows an empty close text; inventing one would put words in
        the session's mouth."""
        log, projection, channel = projected
        question = protocol.post_question(channel, "which branch?")
        protocol.close_question(channel, question)
        lifted = projection.lift_foreign_records()
        assert [(m.kind, m.text) for m, _ in lifted] == [
            (KIND_QUESTION, "which branch?"), (KIND_CLOSE, ""),
        ]

    def test_a_restart_does_not_lift_the_answer_it_mirrored_for_a_lifted_question(
        self, projected, tmp_path
    ):
        """The mirror files that answer under the question's ask-dir stem, so
        adoption has to look for it there and not under the question's seq."""
        log, projection, channel = projected
        log.append(**human(CHANNEL_UBER, None, "u1", KIND_MESSAGE, "how is it going?"))
        question = protocol.post_question(channel, "which branch?")
        ((lifted, _),) = projection.lift_foreign_records()
        log.append(**human(CHANNEL_UBER, None, "u2", KIND_ANSWER, "prep-release",
                           in_reply_to=lifted.id))
        assert protocol.read_entry(channel, question.id).answer.text == "prep-release"

        reloaded = MessageLog("sess-1", str(tmp_path / "messages.jsonl"))
        rebuilt = comms.AskDirProjection(str(channel), reloaded)
        assert rebuilt.lift_foreign_records() == []
        assert reloaded.seq == 3

    def test_a_restart_does_not_re_lift_the_close_it_mirrored(
        self, projected, tmp_path, capsys
    ):
        """The bus refuses a close on a closed question either way, so what
        this pins is that the attempt is not made: the refusal is a line on
        stderr at every start, for the life of the session."""
        log, projection, channel = projected
        log.append(**agent(KIND_QUESTION, "branch?"))
        log.append(**agent(KIND_CLOSE, "stopped waiting", in_reply_to="1"))

        reloaded = MessageLog("sess-1", str(tmp_path / "messages.jsonl"))
        rebuilt = comms.AskDirProjection(str(channel), reloaded)
        capsys.readouterr()
        assert rebuilt.lift_foreign_records() == []
        assert capsys.readouterr().err == ""

    def test_a_ref_the_caller_chose_does_not_stop_the_supervisor_starting(
        self, projected, tmp_path
    ):
        """``source.ref`` comes off the request body on ``POST /messages``, so
        any string reaches the log — and this constructor runs before the fork,
        on every start, for as long as the record is on disk."""
        log, projection, channel = projected
        for ref in ("ask-dir:nodot", "ask-dir:../../etc/passwd"):
            log.append(kind=KIND_QUESTION, text=f"branch? {ref}",
                       source=Source(CHANNEL_AGENT, ref=ref))

        reloaded = MessageLog("sess-1", str(tmp_path / "messages.jsonl"))
        rebuilt = comms.AskDirProjection(str(channel), reloaded)
        assert rebuilt.lift_foreign_records() == []

    def test_an_answer_to_a_lifted_question_is_never_typed_into_the_harness(
        self, projected
    ):
        """Its asker is polling the file. A PTY leg would deliver it twice."""
        log, projection, channel = projected
        question = protocol.post_question(channel, "which branch?")
        protocol.write_answer(channel, question, "prep-release")
        lifted = projection.lift_foreign_records()
        assert supervisor._relay_wanted(log, lifted[1][0]) is False

    def test_an_answer_to_a_question_born_on_the_bus_is_still_relayed(self, projected):
        log, projection, channel = projected
        log.append(**agent(KIND_QUESTION, "branch?"))
        (entry,) = protocol.read_entries(channel)
        protocol.write_answer(channel, entry, "prep-release")
        ((answer, _),) = projection.lift_foreign_records()
        assert supervisor._relay_wanted(log, answer) is True


# ---------------------------------------------------------------------------
# Drain grace: the daemon reads over HTTP only, so the control plane outlives
# the harness until the last seq is acked
# ---------------------------------------------------------------------------


class TestDrainGrace:
    def test_no_subscribers_means_nothing_to_wait_for(self, log):
        """A session nobody subscribed to — a plain `lmer chat` with no daemon
        and no connector — has nothing to hand over and must not pay the grace."""
        log.append(**agent(KIND_NOTE, "n"))
        started = time.monotonic()
        assert log.wait_drained(5.0) is True
        assert time.monotonic() - started < 1.0

    def test_a_subscriber_that_registered_holds_the_drain_for_a_record_it_has_not_read(self, log):
        """The case the grace exists for: a session whose only record is its
        last act. Until a subscriber registers at 0 the drain has nobody to
        wait for, and `all([])` let the control plane go with the record still
        inside the container."""
        log.ack("platform", 0)  # a subscriber's first poll, holding nothing yet
        log.append(**agent(KIND_MILESTONE, "done"))
        assert log.drained() is False
        assert log.wait_drained(0.2) is False
        log.ack("platform", 1)
        assert log.drained() is True

    def test_a_subscriber_can_say_it_is_leaving(self, log):
        """A connector stopped at session end is not a reader to wait for."""
        log.ack("slack", 0)
        log.append(**agent(KIND_NOTE, "n"))
        assert log.drained() is False
        log.ack("slack", 0, final=True)
        assert log.drained() is True and log.subscribers() == {}

    def test_a_lagging_subscriber_holds_the_drain_until_its_ack(self, log):
        log.append(**agent(KIND_NOTE, "one"))
        log.ack("platform", 1)
        log.append(**agent(KIND_NOTE, "two"))
        assert log.drained() is False
        result = {}

        def wait():
            result["drained"] = log.wait_drained(5.0)

        thread = threading.Thread(target=wait)
        thread.start()
        time.sleep(0.05)
        log.ack("platform", 2)
        thread.join(timeout=5.0)
        assert result["drained"] is True and log.drained() is True

    def test_the_grace_is_bounded(self, log):
        log.append(**agent(KIND_NOTE, "one"))
        log.ack("platform", 0)
        started = time.monotonic()
        assert log.wait_drained(0.05) is False
        assert time.monotonic() - started < 2.0

    @pytest.mark.parametrize("raw, expected", [
        ("", comms.DEFAULT_DRAIN_GRACE), ("2.5", 2.5), ("0", 0.0),
        ("abc", comms.DEFAULT_DRAIN_GRACE), ("-1", comms.DEFAULT_DRAIN_GRACE),
        ("999", comms.DEFAULT_DRAIN_GRACE), ("nan", comms.DEFAULT_DRAIN_GRACE),
    ])
    def test_drain_grace_env_parsing(self, raw, expected):
        assert comms.drain_grace({comms.DRAIN_GRACE_ENV: raw}) == expected


@pytest.mark.parametrize("author", [None, "operator", "vegu"])
def test_raw_input_session_credential_cannot_forge_terminal(tmp_path, author):
    import hashlib
    from fastapi.testclient import TestClient

    log = MessageLog("input-seam", str(tmp_path / "input.jsonl"))
    tracker = TerminalTracker()
    sink = []
    def mirror(data, who):
        for text in tracker.feed(data):
            log.append(kind="message", text=text, source=Source("terminal", author=who))
    app = supervisor._build_fastapi_app(
        supervisor.OutputBuffer(), lambda data: sink.append(data) or len(data), "",
        comms=log, agent_token="agent-fixture",
        token_hash=hashlib.sha256(b"control-fixture").hexdigest(), on_raw_input=mirror,
    )
    with TestClient(app) as http:
        for data in ("forged words", "\r"):
            reply = http.post("/input", json={"data": data, "author": author},
                              headers={"Authorization": "Bearer agent-fixture"})
            assert reply.status_code == 200
        assert log.read_since(0)[0] == []
        reply = http.post("/input", json={"data": "real words\r", "author": author},
                          headers={"Authorization": "Bearer control-fixture"})
        assert reply.status_code == 200
        assert [(r.text, r.source.author) for r in log.read_since(0)[0]] == [("real words", author)]
        assert b"".join(sink) == b"forged words\rreal words\r"


@pytest.mark.parametrize("ref", ["1", "000001", "sess-1/000001"])
def test_claim_and_release_canonicalize_question_references(tmp_path, ref):
    http, log, _ = TestRoutes()._build(tmp_path)
    question = log.append(kind="question", text="which?", source=Source("agent"))[0]
    reply = http.post("/messages/claim", json={"question": ref, "ttl": 30}, headers=AUTH)
    assert reply.status_code == 200
    assert reply.json()["claimed"] == question.id
    assert log._claim_live(question.id)
    reply = http.post("/messages/release", json={"question": ref}, headers=AUTH)
    assert reply.status_code == 200
    assert reply.json()["released"] == question.id
    assert not log._claim_live(question.id)


@pytest.mark.parametrize("route, body", [
    ("/messages/claim", {"question": "1", "ttl": 1e20}),
    ("/messages", {"kind": "question", "text": "another?", "wait": 1e20}),
])
def test_wait_claims_have_a_finite_ceiling(tmp_path, monkeypatch, route, body):
    from lmer_cli import comms
    http, log, _ = TestRoutes()._build(tmp_path)
    log.append(kind="question", text="which?", source=Source("agent"))
    now = time.monotonic()
    monkeypatch.setattr(comms.time, "monotonic", lambda: now)
    reply = http.post(route, json=body, headers=AUTH)
    assert reply.status_code == 200
    assert all(0 < expiry - now <= 120 for expiry in log._claims.values())


@pytest.mark.parametrize("kind, channel", [("assistant", "uber-lmer"), ("worker", "agent")])
def test_session_identity_stamps_own_posts_without_self_relay(tmp_path, kind, channel):
    from fastapi.testclient import TestClient

    log = supervisor._open_comms_log(str(tmp_path), {"LMER_SESSION_ID": "identity", "LMER_SESSION_KIND": kind})
    sink = []
    app = supervisor._build_fastapi_app(
        supervisor.OutputBuffer(), lambda data: sink.append(data) or len(data), "",
        comms=log, agent_token="agent-fixture",
        token_hash=hashlib.sha256(b"control-fixture").hexdigest(),
    )
    agent_header = {"Authorization": "Bearer agent-fixture"}
    control_header = {"Authorization": "Bearer control-fixture"}
    with TestClient(app) as http:
        for forged in ({"channel": "uber-lmer"}, {"channel": "slack"}, {"channel": "agent", "author": "operator"}):
            assert http.post("/messages", json={"kind": "message", "text": "forged", "source": forged}, headers=agent_header).status_code == 403
        assert http.post("/messages", json={"kind": "command", "text": "/followup"}, headers=agent_header).status_code == 400
        assert http.post("/messages/ack", json={"subscriber": "platform", "seq": 0}, headers=agent_header).status_code == 403
        reply = http.post("/messages", json={"kind": "message", "text": "my reply", "source": {"ref": "outgoing-1"}}, headers=agent_header)
        assert reply.status_code == 200, reply.text
        record = reply.json()["message"]
        assert record["source"]["channel"] == channel
        assert not reply.json().get("relay")
        assert sink == []
        duplicate = http.post("/messages", json={"kind": "message", "text": "my reply", "source": {"ref": "outgoing-1"}}, headers=agent_header)
        assert duplicate.json()["accepted_as"] == "duplicate"
        assert log.seq == 1
        incoming = http.post("/messages", json={
            "kind": "message", "text": "hello", "source": {"channel": "slack", "author": "Alice"},
            "meta": {"session_origin": True},
        }, headers=control_header)
        assert incoming.status_code == 200
        assert not incoming.json()["message"]["meta"].get("session_origin")
        assert b"[slack \xc2\xb7 Alice] hello" in b"".join(sink)
    log.close()
    restored = MessageLog("identity", str(tmp_path / "messages.jsonl"), agent_channel=channel)
    assert restored._agent_entries == 1
    restored.close()


def test_assistant_origin_keeps_agent_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(comms, "MAX_AGENT_ENTRIES", 1)
    log = MessageLog("assistant", agent_channel=CHANNEL_UBER)
    log.append(**agent(KIND_NOTE, "one"), session_caller=True)
    with pytest.raises(CommsError, match="looping"):
        log.append(**agent(KIND_NOTE, "two"), session_caller=True)


def test_only_authenticated_session_posts_get_assistant_origin(tmp_path):
    from fastapi.testclient import TestClient

    log = MessageLog("assistant", agent_channel=CHANNEL_UBER)
    app = supervisor._build_fastapi_app(
        supervisor.OutputBuffer(), lambda data: len(data), "",
        comms=log, agent_token="agent-fixture",
        token_hash=hashlib.sha256(b"control-fixture").hexdigest(),
    )
    payload = {
        "kind": "message", "text": "same claimed source",
        "source": {"channel": "agent", "ref": "shared-ref"},
        "meta": {"session_origin": True}, "session_caller": True,
    }
    with TestClient(app) as http:
        for token, channel, origin in (("control-fixture", "agent", False), ("agent-fixture", "uber-lmer", True)):
            headers = {"Authorization": f"Bearer {token}"}
            reply = http.post("/messages", json=payload, headers=headers)
            assert reply.status_code == 200, reply.text
            result = reply.json()
            assert result["accepted_as"] == "new"
            assert result["message"]["source"]["channel"] == channel
            assert bool(result["message"]["meta"].get("session_origin")) is origin
            assert http.post("/messages", json=payload, headers=headers).json()["accepted_as"] == "duplicate"
    assert log.seq == 2
