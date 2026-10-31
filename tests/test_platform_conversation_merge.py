"""Tests for merging a live session's ask channel into its conversation (T67).

The gap being closed, stated once: an answer given through the ask channel never
touches the PTY and is never handed to the harness as input — it is a file the
platform writes into a bind-mounted directory the session polls. So the harness
records nothing about it, and until this merge the operator could answer a
question and find no trace of their own words in the conversation view. The
operator, from live testing: *"answering lmer's question in the operator chat,
doesn't show my answer in the conversation chat … for coherence it'd ideally still
show up as input i did"*.

Five properties carry this, and each one fails quietly in a different direction:

- **The answer is yours, and shown as yours.** Not paraphrased, not rendered as
  markdown, not attributed to the agent.
- **Nothing is written into the transcript.** The merge is a read-path join; the
  transcript is the harness's artifact and the one durable record of a session,
  and forging operator turns into it would make that record partly fiction.
- **A session with no channel reads exactly as it did before.** The assistant's
  own session (T31) has no run and no channel at all, and every session spawned
  before the channel existed is in the same position.
- **The credential scrub reaches the merged turns.** An operator can paste a
  secret into an answer, and this is the last place before a browser.
- **The order is deterministic.** Two clocks with different precision meet here,
  so equal timestamps must not decide anything by luck.

Ordering is asserted against captured transcript timestamps rather than against
"now": the fixture's turns are stamped 09:00:01 … 09:00:14, and the channel
entries are restamped to land at chosen points inside that.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ask_channel import protocol
from lmer_cli import comms as bus
from lmer_platform import api, ask, assistant_history, registry, store, transcripts, uploads
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env
from tests.test_platform_transcripts import (
    DEAD_PID,
    FOLLOWUP_FIXTURE,
    RUN,
    plant_log,
    plant_session,
)

SECRET = "test-secret-value"

WEB = Path(__file__).resolve().parent.parent / "web"
CHAT = WEB / "src" / "components" / "Chat.vue"

#: Where the fixture transcript's own turns sit, so a test can say "between the
#: failed Edit and /usage" without restating the fixture.
BEFORE_EDIT = "2026-07-20T09:00:06Z"
AT_THE_EDIT = "2026-07-20T09:00:08Z"
AFTER_THE_EDIT = "2026-07-20T09:00:09Z"
AFTER_THE_ANSWER = "2026-07-20T09:00:10Z"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", cfg.ENV_BIND_ADDRESS, cfg.ENV_BIND_PORT):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture(autouse=True)
def _isolated_transcript_root(tmp_path, monkeypatch):
    """Never fall through to the developer's own ``~/.claude/projects``.

    Autouse for the reason ``test_platform_transcripts`` gives: the default root is
    ``Path.home()``-based, so a test that forgot would assert against whatever real
    conversations happen to be on this machine.
    """
    root = tmp_path / "harness-transcripts"
    root.mkdir()
    monkeypatch.setattr(transcripts, "TRANSCRIPT_ROOT", str(root))
    return root


def _restamp(path, **fields):
    """Rewrite the time fields of one channel record already on disk.

    The records are *posted* through :mod:`ask_channel.protocol` so they are the
    real format — nonce, schema, padded id and all — and only then restamped,
    because the protocol stamps the moment it wrote and these tests need an entry
    that lands at a known point inside a captured transcript. Rewriting the field
    is the smaller lie than reimplementing the file format here.
    """
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(fields)
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def plant_question(
    session_id,
    text="which branch?",
    *,
    at=AFTER_THE_EDIT,
    answer=None,
    answered_at=None,
    options=(),
):
    """Put one question — and optionally its answer — on a session's channel."""
    directory = ask.prepare_ask_dir(session_id)
    entry = protocol.post_question(directory, text, list(options))
    _restamp(directory / f"{entry.id}{protocol.QUESTION_SUFFIX}", at=at)
    if answer is not None:
        # At the protocol layer, not ask.answer_question: these fixtures register
        # dead sessions (the merge only needs a transcript to read), and the
        # platform's write path now refuses a dead session's question (T94). The
        # record being planted is "an answer already exists on this channel" —
        # the ordering T94 deliberately keeps legal, since answers survive the
        # session that received them.
        protocol.write_answer(directory, entry, answer)
        _restamp(
            directory / f"{entry.id}{protocol.ANSWER_SUFFIX}",
            answered_at=answered_at or at,
        )
    return entry.id


def plant_note(session_id, text="ran the suite, all green"):
    return protocol.post_note(ask.prepare_ask_dir(session_id), text)


def said(page):
    """``(role, text)`` for every turn on a page, in the order it renders."""
    return [(message.role, message.text) for message in page.messages]


# --- the answer is yours, and shown as yours ---------------------------------

def test_an_answer_you_gave_appears_as_your_own_turn(platform_root):
    """The whole point: your words are in the conversation, attributed to you.

    ``role='user'`` is not cosmetic — it is what puts the turn in the view's
    verbatim branch, so an answer containing asterisks is shown as typed rather
    than reinterpreted as markup.
    """
    plant_session("s-merge")
    plant_question("s-merge", answer="prep-release")

    page = transcripts.read_messages("s-merge", limit=100)
    yours = [message for message in page.messages if message.text == "prep-release"]

    assert len(yours) == 1, "the answer the operator gave is not in the conversation"
    assert yours[0].role == "user"
    assert yours[0].kind == "said", "an answer is not machinery talking to the model"
    assert yours[0].via == transcripts.ASK_CHANNEL_VIA
    # No transcript file holds it, and pretending one does would be the lie.
    assert yours[0].origin is None


def test_the_question_is_shown_above_the_answer_it_explains(platform_root):
    """A bubble of your own saying "prep-release" means nothing on its own.

    The question is the agent's own words — it ran ``lmer-ask`` — so it is an
    assistant turn, and it is not a second copy of anything: ``lmer-ask`` is
    documented to take its text from a file or stdin, so the transcript usually
    holds only a bounded tool-hint line for the command that posted it.
    """
    plant_session("s-pair")
    plant_question("s-pair", "which branch?", answer="prep-release")

    page = transcripts.read_messages("s-pair", limit=100)
    turns = said(page)

    assert ("assistant", "which branch?") in turns
    assert turns.index(("assistant", "which branch?")) < turns.index(
        ("user", "prep-release")
    ), "the answer renders above the question it answers"
    assert [
        message.via for message in page.messages if message.text == "which branch?"
    ] == [transcripts.ASK_CHANNEL_VIA]


def test_an_unanswered_question_is_still_part_of_the_conversation(platform_root):
    """A question that timed out is exactly what a later reader needs to see.

    Dropping it would leave the transcript's own ``lmer-ask`` tool line
    unexplained, which is the state an operator is trying to understand when they
    scroll back at all.
    """
    plant_session("s-open")
    plant_question("s-open", "delete the branch?")

    turns = said(transcripts.read_messages("s-open", limit=100))
    assert ("assistant", "delete the branch?") in turns


def test_a_progress_note_is_not_a_conversation_turn(platform_root):
    """A note wants no reply, so it is not half of an exchange to read back.

    It is progress, and the operator channel is where it surfaces. Merging notes
    too would put the agent's status pings in the middle of a conversation that
    already carries the agent's own account of the same work.
    """
    plant_session("s-note")
    plant_note("s-note", "ran the suite, all green")

    turns = said(transcripts.read_messages("s-note", limit=100))
    assert not [text for _, text in turns if "all green" in text]


def test_the_options_a_question_offered_are_not_merged(platform_root):
    """What the operator chose is shown verbatim; the roads not taken are not.

    The ask panel is where a pending question's choices belong. Pasting them into
    the conversation would put text in an agent turn that the agent never wrote as
    prose.
    """
    plant_session("s-options")
    plant_question(
        "s-options", "which branch?", options=("main", "prep-release"), answer="main"
    )

    page = transcripts.read_messages("s-options", limit=100)
    assert [
        message.text for message in page.messages
        if message.via == transcripts.ASK_CHANNEL_VIA
    ] == ["which branch?", "main"]


# --- nothing is written back --------------------------------------------------

def test_a_merged_read_does_not_touch_the_transcript_file(platform_root):
    """The transcript is the harness's artifact and the one durable record.

    Writing synthetic operator turns into it would leave no later reader able to
    tell which turns the session actually received from which ones the platform
    added — and it is the file ``scrub_transcript`` rewrites, so a forged turn
    would also become the thing served as "what the harness wrote".
    """
    target = plant_session("s-readonly")
    plant_question("s-readonly", answer="prep-release")

    before = target.read_bytes()
    before_stat = target.stat()
    names_before = sorted(path.name for path in target.parent.iterdir())

    page = transcripts.read_messages("s-readonly", limit=100)
    assert ("user", "prep-release") in said(page), "nothing was merged, so prove nothing"

    assert target.read_bytes() == before, "the merge wrote into the transcript"
    assert target.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert sorted(path.name for path in target.parent.iterdir()) == names_before


def test_a_merged_read_does_not_touch_the_channel_either(platform_root):
    """Read-only in both directions: the channel is the container's to write.

    Nobody rewrites anybody else's file on that channel (``ask_channel.protocol``
    says so, and it is what makes a directory shared across a bind mount workable
    without coordination). A read path that touched it would be the first
    exception.
    """
    plant_session("s-channel-readonly")
    plant_question("s-channel-readonly", answer="prep-release")
    directory = ask.session_ask_dir("s-channel-readonly")
    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(directory.iterdir())
    }

    transcripts.read_messages("s-channel-readonly", limit=100)

    assert {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(directory.iterdir())
    } == before


# --- a session with no channel is untouched ----------------------------------

def test_a_session_with_no_channel_reads_exactly_as_before(platform_root):
    """T31 pointed the assistant drawer at a session with no run and no channel.

    Every session spawned before the channel existed is in the same position, and
    so is every run that never asked anything — which is almost all of them. The
    merge has to contribute nothing there, not merely nothing visible: the list is
    handed straight back, so no clock is computed and no ordering decision is made
    on a conversation that has only one source.
    """
    plant_session("s-plain", run=None)
    assert not ask.session_ask_dir("s-plain").exists()

    page = transcripts.read_messages("s-plain", limit=100)
    assert page.total == 8, "the transcript's own turn count changed"
    assert all(message.via is None for message in page.messages)
    assert [message.seq for message in page.messages] == list(range(8))

    # The shortcut itself, since "nothing visible changed" is the weaker claim.
    messages = list(page.messages)
    assert transcripts._interleave(messages, []) is messages


def test_an_empty_channel_contributes_nothing(platform_root):
    """A directory exists as soon as a session is spawned; most stay empty."""
    plant_session("s-empty-channel")
    before = transcripts.read_messages("s-empty-channel", limit=100).to_dict()

    ask.prepare_ask_dir("s-empty-channel")

    assert transcripts.read_messages("s-empty-channel", limit=100).to_dict() == before


def test_a_session_whose_transcript_never_mounted_still_shows_the_exchange(
    platform_root,
):
    """The channel is the whole conversation a session like this has.

    Common today: codex and pi keep their session files somewhere this adapter does
    not read, so the transcript resolves to nothing while the ask channel is fully
    there. An empty view would report the exchange as never having happened.
    """
    registry.register("s-no-transcript", pid=DEAD_PID, run=dict(RUN))
    plant_question("s-no-transcript", "which branch?", answer="prep-release")

    page = transcripts.read_messages("s-no-transcript", limit=100)
    assert said(page) == [("assistant", "which branch?"), ("user", "prep-release")]
    assert page.sources == ()


def test_an_unreadable_channel_costs_the_questions_and_nothing_else(
    platform_root, monkeypatch, caplog
):
    """Tolerance is the contract here as everywhere else in this module.

    The conversation is the transcript's; a channel directory that cannot be read
    must not be able to empty it.
    """
    plant_session("s-broken-channel")

    def boom(session_id):
        raise OSError("channel is gone")

    monkeypatch.setattr(ask, "read_entries", boom)

    page = transcripts.read_messages("s-broken-channel", limit=100)
    assert page.total == 8
    assert "platform_transcript_ask_unreadable" in caplog.text


# --- ordering -----------------------------------------------------------------

def test_the_exchange_lands_where_it_happened_in_the_transcript(platform_root):
    """Appending it would put an answer given mid-run at the end of the run.

    The fixture's turns are stamped 09:00:01 … 09:00:14; this exchange happened at
    :09 and :10, which is after the failed Edit (:08) and before ``/usage`` (:11).
    """
    plant_session("s-order")
    plant_question(
        "s-order",
        "which branch?",
        at=AFTER_THE_EDIT,
        answer="prep-release",
        answered_at=AFTER_THE_ANSWER,
    )

    turns = said(transcripts.read_messages("s-order", limit=100))
    positions = [
        turns.index(("assistant", "which branch?")),
        turns.index(("user", "prep-release")),
    ]
    assert positions == [5, 6]
    assert turns[4][0] == "assistant"          # the failed Edit, at :08
    assert turns[7] == ("system", "/usage\nusage")


def test_an_equal_timestamp_breaks_toward_the_transcript(platform_root):
    """Two clocks, two precisions: milliseconds against whole seconds.

    A channel entry stamped :08 collides with a transcript record stamped
    :08.000Z, and the tie cannot be left to luck — a merge that reordered itself
    between two polls would renumber history under a client's cursor. The
    transcript wins because the channel record is *caused by* the tool call the
    transcript is recording: the agent ran ``lmer-ask``, and that call is the
    transcript turn.
    """
    plant_session("s-tie")
    plant_question("s-tie", "which branch?", at=AT_THE_EDIT, answer="prep-release")

    first = transcripts.read_messages("s-tie", limit=100)
    turns = said(first)
    # The Edit turn is the fixture's :08; the question shares the instant.
    assert turns.index(("assistant", "which branch?")) == 5
    assert first.messages[4].tools[0].name == "Edit"

    # And it is stable: the same read twice cannot renumber anything.
    again = transcripts.read_messages("s-tie", limit=100)
    assert again.to_dict() == first.to_dict()


def test_the_transcript_keeps_its_file_order_whatever_its_timestamps_say(
    platform_root, tmp_path
):
    """A merge may interleave into a sequence; it may not reorder it.

    Harness timestamps are not guaranteed monotonic — a resumed session or a
    record written from a queue can date one turn before the turn above it — and a
    plain timestamp sort would move it, renumbering history for every cursor in
    flight. The clamp is what makes the file the authority on its own order.
    """
    registry.register("s-backwards", pid=DEAD_PID, run=dict(RUN))
    directory = transcripts.session_transcript_dir("s-backwards") / "-workspace"
    directory.mkdir(parents=True)
    rows = [
        {"type": "user", "timestamp": "2026-07-20T09:00:20.000Z",
         "message": {"role": "user", "content": "first, late-dated"}},
        {"type": "assistant", "timestamp": "2026-07-20T09:00:02.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "second, early-dated"},
         ]}},
    ]
    (directory / "backwards.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    plant_question("s-backwards", "third?", at="2026-07-20T09:00:30Z", answer="yes")

    turns = said(transcripts.read_messages("s-backwards", limit=100))
    assert turns == [
        ("user", "first, late-dated"),
        ("assistant", "second, early-dated"),
        ("assistant", "third?"),
        ("user", "yes"),
    ]


def test_an_answer_never_renders_above_its_own_question(platform_root):
    """The clamp again, on the channel side, where a stamp can simply be missing.

    An answer with no ``answered_at`` inherits its question's instant rather than
    floating to the top of the conversation, and an answer somehow dated *before*
    its question is held to the question's time.
    """
    plant_session("s-clamped")
    plant_question(
        "s-clamped",
        "which branch?",
        at=AFTER_THE_EDIT,
        answer="prep-release",
        answered_at=BEFORE_EDIT,
    )

    turns = said(transcripts.read_messages("s-clamped", limit=100))
    assert turns.index(("assistant", "which branch?")) < turns.index(
        ("user", "prep-release")
    )


def test_an_undated_channel_record_is_ordered_by_its_own_sequence(platform_root):
    """Corruption path, but it must not be a random one.

    ``at`` is always written by the protocol, so a record without one has been
    damaged. The channel's own sequence order is then what it is placed by — never
    a guess at a time it might have had.
    """
    plant_session("s-undated")
    plant_question("s-undated", "first?", at=AFTER_THE_EDIT, answer="yes")
    plant_question("s-undated", "second?", at="", answer="also yes")

    channel = [
        message.text for message in transcripts.read_messages("s-undated", limit=100).messages
        if message.via == transcripts.ASK_CHANNEL_VIA
    ]
    assert channel == ["first?", "yes", "second?", "also yes"]


def test_two_sessions_of_one_run_keep_their_channels_apart(platform_root):
    """A channel belongs to one container, so it merges into one transcript.

    Interleaving the earlier session's questions into the later session's turns
    would show an answer in a conversation that never received it — and the
    session boundary is what keeps the run's overall order from becoming a global
    timestamp sort.
    """
    plant_session("s-20260720T090000-aaaa")
    plant_session("s-20260720T113000-bbbb", fixture=FOLLOWUP_FIXTURE)
    # Dated inside the *first* session's window on purpose: the session grouping,
    # not the timestamp, has to decide which transcript it lands in.
    plant_question("s-20260720T113000-bbbb", "which branch?", at=AFTER_THE_EDIT,
                   answer="prep-release")

    turns = said(transcripts.read_messages("s-20260720T113000-bbbb", limit=100))
    assert turns.index(("assistant", "which branch?")) >= 8, (
        "the second session's question was merged into the first session's turns"
    )


def test_the_cursor_still_neither_skips_nor_repeats_across_a_merge(platform_root):
    """Paging is the whole basis of the poll, and the merge renumbers a session."""
    plant_session("s-paged")
    plant_question("s-paged", "which branch?", answer="prep-release")

    seen = []
    cursor = 0
    while True:
        page = transcripts.read_messages("s-paged", since=cursor, limit=3)
        if not page.messages:
            break
        seen.extend(message.seq for message in page.messages)
        cursor = page.cursor

    assert seen == list(range(10)), "a page skipped or repeated a turn"
    assert transcripts.read_messages("s-paged", limit=100).total == 10


# --- the credential scrub reaches the merged turns ---------------------------

def test_a_credential_pasted_into_an_answer_is_masked(platform_root):
    """An operator answering from a phone can paste a token into the box.

    Merged turns are served by the same path as transcript turns, so they go
    through the same chokepoint — that is what makes the scrub a property of the
    module rather than of whichever caller remembered.
    """
    leak = "glpat-" + "a" * 24
    plant_session("s-leak")
    plant_question(
        "s-leak",
        f"push with --fastapi-token {leak}?",
        answer=f"yes, use {leak}",
    )

    payload = json.dumps(transcripts.read_messages("s-leak", limit=100).to_dict())
    assert leak not in payload, "a credential in an answer reached the browser"
    assert "<redacted>" in payload


def test_the_platforms_own_secret_is_masked_in_an_answer(platform_root):
    """The credential that spawns containers, matched by value rather than shape.

    Nothing shaped can find it: it has no prefix, no delimiter and no giveaway, so
    an operator quoting it back in an answer is only caught because the value is
    known.
    """
    secret = cfg.ensure_secret(cfg.load())
    plant_session("s-secret")
    plant_question("s-secret", "what is the token?", answer=f"it is {secret}")

    page = transcripts.read_messages("s-secret", limit=100)
    payload = json.dumps(page.to_dict())
    assert secret not in payload
    assert "<redacted>" in payload


def test_the_channel_file_itself_is_left_as_written(platform_root):
    """Stated so the story stays consistent, not because it is comfortable.

    A transcript is rewritten when its session ends (``scrub_transcript``) because
    it is a file the platform mounted out and now owns. An answer file is not: the
    container it is mounted into was *handed* that answer, so there is nothing left
    to withhold from it, and the record sits 0600 in a 0700 directory. The scrub's
    job here is the one it has everywhere in this module — what reaches a browser.
    """
    leak = "glpat-" + "b" * 24
    plant_session("s-ondisk")
    question_id = plant_question("s-ondisk", "which token?", answer=f"use {leak}")

    answer_file = (
        ask.session_ask_dir("s-ondisk") / f"{question_id}{protocol.ANSWER_SUFFIX}"
    )
    assert leak in answer_file.read_text(encoding="utf-8")

    # The write-path scrub runs over the session's transcripts and stops there.
    assert transcripts.scrub_session_transcripts("s-ondisk") == 1
    assert leak in answer_file.read_text(encoding="utf-8")

    # And the read path masks it anyway, which is the promise that matters.
    page = transcripts.read_messages("s-ondisk", limit=100)
    assert leak not in json.dumps(page.to_dict())


def test_a_very_long_answer_is_trimmed_like_any_other_turn(platform_root):
    """The channel allows more characters than one turn is served with.

    ``MAX_ANSWER_CHARS`` is twice ``TEXT_LIMIT``, so a merged answer has to pass
    through the same truncation as everything else — and keep the same end, since
    what a person types last is what they concluded.
    """
    assert protocol.MAX_ANSWER_CHARS > transcripts.TEXT_LIMIT
    body = "x" * (transcripts.TEXT_LIMIT + 200) + " the actual decision"
    plant_session("s-long")
    plant_question("s-long", "which branch?", answer=body)

    merged = [
        message for message in transcripts.read_messages("s-long", limit=100).messages
        if message.role == "user" and message.via == transcripts.ASK_CHANNEL_VIA
    ]
    assert len(merged) == 1
    assert merged[0].truncated
    assert len(merged[0].text) <= transcripts.TEXT_LIMIT
    assert merged[0].text.endswith("the actual decision")


# --- the route that serves it -------------------------------------------------

def test_the_messages_route_serves_the_merged_turns(platform_root):
    """Wiring check: ``api.py`` gets this for free, and must actually get it.

    The merge lives in :mod:`lmer_platform.transcripts` precisely so the route
    keeps one call and one exception mapping.
    """
    plant_session("s-20260720T090000-cccc")
    plant_log("s-20260720T090000-cccc")
    plant_question("s-20260720T090000-cccc", "which branch?", answer="prep-release")

    client = TestClient(
        api.create_app(
            cfg.load(), SECRET, state_builder=lambda config, force_pull=False: {}
        )
    )
    reply = client.get(
        "/api/sessions/s-20260720T090000-cccc/messages?limit=100",
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    assert reply.status_code == 200
    payload = reply.json()
    merged = [
        message for message in payload["messages"]
        if message["via"] == transcripts.ASK_CHANNEL_VIA
    ]
    assert [(message["role"], message["text"]) for message in merged] == [
        ("assistant", "which branch?"), ("user", "prep-release"),
    ]


# --- the view's half ----------------------------------------------------------

def _chat():
    return CHAT.read_text(encoding="utf-8")


def test_a_merged_answer_is_not_run_through_the_markdown_renderer():
    """The user asked for "something i sent", and that is a verbatim bubble.

    The property is bought by attribution rather than by a special case: a merged
    answer carries ``role: 'user'``, so it falls into the branch that was already
    there for everything the operator sent. A second branch keyed on ``via`` would
    be a second place for the renderer to creep into.
    """
    text = _chat()
    branch = re.search(
        r'<p\s+v-if="([^"]+)"\s+class="text-body-medium said plain"',
        text,
        re.S,
    )
    assert branch and "message.role === 'user'" in branch.group(1), (
        "the operator's own turns no longer key the verbatim branch on the role"
    )
    assert text.count("said plain") == 2, (
        "a rendering branch was added or removed; the merged answer must reuse the "
        "one that already shows a sent message verbatim"
    )
    assert re.search(r'<Markdown\s+v-else-if="message\.text"', text), (
        "the rendered half is no longer the fallthrough, so a turn could reach it "
        "by some route other than not being the operator's"
    )
    assert text.count("message.via") == 1, (
        "`via` is consulted somewhere besides the header — where a turn came from "
        "must not decide how its words are rendered"
    )


def test_a_merged_turn_says_where_it_came_from():
    """Neither half of the exchange is passed off as transcript content.

    The answer is genuinely the operator's and the question is genuinely the
    agent's, but the harness wrote down neither — so the header says so, and an
    operator comparing the chat against the terminal is not left wondering why two
    turns are missing from the one and present in the other.
    """
    text = _chat()
    assert "message.via === 'ask'" in text, "a merged turn is indistinguishable"
    assert "ask channel" in text


def test_the_server_and_the_view_agree_on_the_marker():
    """One spelling of the marker, checked across the seam rather than assumed."""
    assert transcripts.ASK_CHANNEL_VIA == "ask"
    assert f"message.via === '{transcripts.ASK_CHANNEL_VIA}'" in _chat()


# --- the bus (issue #407): the conversation, every channel included ------------
#
# The ask channel is a projection of the bus for one release; when the daemon's
# journal holds a session's records, they are the conversation and the projection
# is not read. Records are planted straight into the journal the way the hub's
# subscriber lands them.

from lmer_platform import comms as platform_comms  # noqa: E402


def plant_record(session_id, seq, kind, text, *, channel="agent", author=None, ref=None,
                 in_reply_to=None, at=AFTER_THE_EDIT, meta=None, options=()):
    record = {
        "schema": 1, "id": f"{session_id}/{seq:06d}", "seq": seq, "session": session_id,
        "at": at, "source": {"channel": channel}, "kind": kind, "text": text,
        "in_reply_to": in_reply_to, "options": list(options), "attachments": [],
        "meta": dict(meta or {}),
    }
    if author:
        record["source"]["author"] = author
    if ref:
        record["source"]["ref"] = ref
    platform_comms.ensure_hub().journal.append(session_id, record)
    return record


def plant_rows(session_id, rows):
    """A session whose transcript is exactly *rows* (harness-shaped records)."""
    registry.register(session_id, pid=DEAD_PID, run=dict(RUN))
    directory = transcripts.session_transcript_dir(session_id) / "-workspace"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "rows.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def user_row(text, at="2026-07-20T09:00:02.000Z"):
    return {"type": "user", "timestamp": at, "message": {"role": "user", "content": text}}


def assistant_row(text, at="2026-07-20T09:00:03.000Z"):
    return {"type": "assistant", "timestamp": at,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def bus_turns(page):
    return [
        (m.role, m.text, m.comms_kind, (m.source or {}).get("channel"))
        for m in page.messages if m.via == transcripts.COMMS_VIA
    ]


def test_bus_records_are_the_conversation_with_their_sources(platform_root):
    session_id = "s-bus-merge"
    plant_rows(session_id, [assistant_row("looking at the branch")])
    plant_record(session_id, 1, "note", "running the suite")
    plant_record(session_id, 2, "question", "which branch?", options=["main", "prep"])
    plant_record(session_id, 3, "answer", "prep", channel="slack", author="tester", ref="t1",
                 in_reply_to=f"{session_id}/000002", meta={"delivered": "pty"})
    plant_record(session_id, 4, "message", "thanks — go ahead", channel="matrix", author="@alice:x", ref="e1")
    plant_record(session_id, 5, "milestone", "pushed MR !300")

    page = transcripts.read_messages(session_id, limit=100)
    assert said(page)[0] == ("assistant", "looking at the branch")
    assert bus_turns(page) == [
        ("assistant", "running the suite", "note", "agent"),
        ("assistant", "which branch?", "question", "agent"),
        ("user", "prep", "answer", "slack"),
        ("user", "thanks — go ahead", None, "matrix"),
        ("assistant", "pushed MR !300", "milestone", "agent"),
    ]
    answer = [m for m in page.messages if m.text == "prep"][0]
    assert answer.source == {"channel": "slack", "author": "tester", "ref": "t1"}
    assert answer.to_dict()["source"]["author"] == "tester"
    assert answer.origin is None


def test_the_harness_copy_of_a_relayed_record_is_hidden(platform_root):
    """The supervisor typed ``[slack · tester] hello there`` into the harness, which
    wrote it down as a user turn. The bus record already says who said hello."""
    session_id = "s-bus-relay"
    plant_rows(session_id, [
        user_row("[slack · tester] hello there", at="2026-07-20T09:00:02.000Z"),
        assistant_row("hi tester", at="2026-07-20T09:00:03.000Z"),
        user_row("[slack · tester] hello   there", at="2026-07-20T09:00:04.000Z"),
        user_row("an unrelated turn typed at the terminal", at="2026-07-20T09:00:05.000Z"),
    ])
    plant_record(session_id, 1, "message", "hello there", channel="slack", author="tester", ref="t1",
                 at="2026-07-20T09:00:01Z")
    plant_record(session_id, 2, "message", "hello there", channel="slack", author="tester", ref="t2",
                 at="2026-07-20T09:00:04Z")
    page = transcripts.read_messages(session_id, limit=100)
    harness_copies = [m for m in page.messages if m.via is None and m.text.startswith("[slack")]
    assert harness_copies == [], "the harness's copy of a relayed record is still shown"
    assert [(m.role, m.text) for m in page.messages if m.via is None] == [
        ("assistant", "hi tester"),
        ("user", "an unrelated turn typed at the terminal"),
    ]
    assert said(page).count(("user", "hello there")) == 2


def test_the_harness_copy_of_a_relayed_record_the_scrub_rewrites_is_hidden(platform_root):
    session_id = "s-bus-relay-scrubbed"
    record = plant_record(session_id, 1, "message", "try glab --token abcdef123456 now",
                          channel="slack", author="tester", ref="t1", at="2026-07-20T09:00:01Z")
    line, _tagged = bus.relay_payload(bus.Message.from_dict(record))
    plant_rows(session_id, [user_row(line)])
    page = transcripts.read_messages(session_id, limit=100)
    assert [(m.via, m.text) for m in page.messages] == [
        (transcripts.COMMS_VIA, "try glab --token <redacted> now")]


def _reference(session_id, purpose=None):
    """The line the writer delivers, its file named from the purpose as the writer names it."""
    slug = uploads._purpose_slug(purpose)
    name = uploads.stored_name(f"private-{slug}" if slug else "private", uploads.KNOWN_TYPES["txt"],
                               now=datetime(2026, 10, 30, 12, tzinfo=timezone.utc))
    return uploads.private_reference_line(uploads.StoredPrivate(
        session_id=session_id, name=name,
        container_path=f"{uploads.CONTAINER_UPLOADS_DIR}/{uploads.PRIVATE_DIRNAME}/{session_id}/{name}",
        purpose=purpose,
    ))


#: Purposes the scrub rewrites inside the delivered line: an option-shaped word eats
#: the text after it, and a token prefix redacts the slug in the file name.
SCRUBBED_PURPOSES = [
    None,
    "GitLab deploy token",
    "for --deploy-token",
    "glab --token",
    "use --password",
    "GitLab glpat deploy token staging",
    "sk-proj key for eval harness",
]


def _private_record(session_id, seq, purpose, *, marked=True):
    """The operator's Send Privately record and the line the harness wrote for it."""
    reference = _reference(session_id, purpose)
    record = plant_record(session_id, seq, "message", reference, channel="platform", author="operator",
                          at="2026-07-20T09:00:01Z",
                          meta={"private": {"purpose": purpose}} if marked else None)
    line, tagged = bus.relay_payload(bus.Message.from_dict(record))
    assert tagged
    return record, line


def _raw_private_lines(page):
    return [m for m in page.messages if "[lmer private]" in m.text and not m.private]


@pytest.mark.parametrize("purpose", SCRUBBED_PURPOSES)
def test_a_private_send_with_its_record_in_the_window_is_one_entry(platform_root, purpose):
    session_id = "s-private-normal"
    _record, line = _private_record(session_id, 1, purpose)
    plant_rows(session_id, [user_row(line)])
    page = transcripts.read_messages(session_id, limit=100)
    private = [m for m in page.messages if m.private]
    assert [(m.via, m.private) for m in private] == [(transcripts.COMMS_VIA, {"purpose": purpose})]
    assert _raw_private_lines(page) == []
    assert len(page.messages) == 1


@pytest.mark.parametrize("purpose", SCRUBBED_PURPOSES)
def test_a_private_send_outside_the_window_is_a_private_entry_whatever_its_purpose(
        platform_root, monkeypatch, purpose):
    session_id = "s-private-window-scrub"
    monkeypatch.setattr(platform_comms, "PANE_LIMIT", 1)
    plant_record(session_id, 0, "note", "one")
    _record, line = _private_record(session_id, 1, purpose)
    plant_rows(session_id, [user_row(line)])
    page = transcripts.read_messages(session_id, limit=100)
    assert [m.text for m in page.messages if m.via == transcripts.COMMS_VIA] == ["one"]
    [echo] = [m for m in page.messages if m.via is None]
    assert echo.private == {"purpose": purpose}
    assert _raw_private_lines(page) == []


@pytest.mark.parametrize("purpose", SCRUBBED_PURPOSES)
@pytest.mark.parametrize("ended", [False, True], ids=["live", "ended"])
def test_a_private_send_with_no_bus_is_a_private_entry_whatever_its_purpose(platform_root, purpose, ended):
    """Ended: the file has been scrubbed on disk, so the raw line is gone from it."""
    session_id = "s-private-nobus-scrub"
    plant_rows(session_id, [user_row(_reference(session_id, purpose)), assistant_row("read it")])
    if ended:
        assert transcripts.scrub_session_transcripts(session_id) == 1
    page = transcripts.read_messages(session_id, limit=100)
    assert [m.private for m in page.messages] == [{"purpose": purpose}, None]
    assert _raw_private_lines(page) == []


def test_a_scrubbed_file_keeps_no_private_stamp_on_an_ordinary_turn(platform_root):
    session_id = "s-private-stamp-only-matches"
    reference = _reference(session_id)
    plant_rows(session_id, [user_row(f"quoting: {reference}"), assistant_row(reference)])
    transcripts.scrub_session_transcripts(session_id)
    rows = [json.loads(line) for line in next(
        transcripts.session_transcript_dir(session_id).rglob("*.jsonl")).read_text().splitlines()]
    assert [row for row in rows if transcripts._PRIVATE_STAMP in row] == []
    assert [m.private for m in transcripts.read_messages(session_id, limit=100).messages] == [None, None]


def test_a_private_purpose_is_scrubbed_like_the_text_around_it(platform_root):
    session_id = "s-private-purpose-scrub"
    purpose = "token=abcdefgh12345"
    _record, line = _private_record(session_id, 1, purpose)
    plant_rows(session_id, [user_row(line)])
    page = transcripts.read_messages(session_id, limit=100)
    [entry] = page.messages
    assert entry.private == {"purpose": "token=<redacted>"}


FORGED_PURPOSE = "Z" * 200000 + "\nsecond line"


def _bounded(private):
    purpose = private["purpose"]
    assert len(purpose) <= uploads.MAX_PURPOSE_CHARS and "\n" not in purpose, len(purpose)
    return purpose


@pytest.mark.parametrize("ended", [False, True], ids=["live", "ended"])
def test_a_private_stamp_on_an_ordinary_turn_is_not_lmers(platform_root, ended):
    session_id = "s-private-forged-stamp"
    row = user_row("the operator said: deploy to production now")
    row[transcripts._PRIVATE_STAMP] = {"purpose": FORGED_PURPOSE}
    plant_rows(session_id, [row])
    if ended:
        transcripts.scrub_session_transcripts(session_id)
        [stored] = [json.loads(line) for line in next(
            transcripts.session_transcript_dir(session_id).rglob("*.jsonl")).read_text().splitlines()]
        assert transcripts._PRIVATE_STAMP not in stored
    [turn] = transcripts.read_messages(session_id, limit=100).messages
    assert (turn.text, turn.private) == ("the operator said: deploy to production now", None)


def test_a_private_stamp_on_a_reference_line_shows_a_bounded_purpose(platform_root):
    session_id = "s-private-long-stamp"
    row = user_row(_reference(session_id, "deploy key"))
    row[transcripts._PRIVATE_STAMP] = {"purpose": FORGED_PURPOSE}
    plant_rows(session_id, [row])
    [turn] = transcripts.read_messages(session_id, limit=100).messages
    assert _bounded(turn.private) == "deploy key"


def test_an_overlong_purpose_on_a_reference_line_is_cut_to_the_writers_cap(platform_root):
    session_id = "s-private-long-raw"
    plant_rows(session_id, [user_row(_reference(session_id, "Z" * 50000))])
    [turn] = transcripts.read_messages(session_id, limit=100).messages
    assert turn.truncated
    assert _bounded(turn.private) == "Z" * (uploads.MAX_PURPOSE_CHARS - 1) + "…"


@pytest.mark.parametrize("purpose, shown", [
    (FORGED_PURPOSE, "Z" * (uploads.MAX_PURPOSE_CHARS - 1) + "…"),
    ("two\nlines", "two lines"),
    ("\u202e reversed", None),
    ("   ", None),
], ids=["overlong", "two-lines", "unprintable", "blank"])
def test_a_records_private_marker_is_held_to_the_writers_rule(platform_root, purpose, shown):
    session_id = "s-private-long-marker"
    plant_record(session_id, 1, "message", "anything", channel="platform", author="operator",
                 meta={"private": {"purpose": purpose}})
    plant_rows(session_id, [])
    [turn] = transcripts.read_messages(session_id, limit=100).messages
    assert turn.private == {"purpose": shown}


@pytest.mark.parametrize("purpose", SCRUBBED_PURPOSES)
def test_an_archived_older_private_send_is_a_private_entry(platform_root, purpose):
    """Archived before ``meta.private`` existed: the archive stores the record scrubbed,
    so it is marked from the raw text on the way in."""
    session_id = "s-private-archive"
    registry.register(session_id, pid=DEAD_PID, run=dict(RUN))
    assistant_history.register(session_id, number=1, started_at=None)
    record, line = _private_record(session_id, 1, purpose, marked=False)
    assistant_history.retain(session_id, record)
    page = assistant_history.page(session_id, since=0)
    private = [row for row in page["messages"] if row.get("private")]
    assert [row["private"] for row in private] == [{"purpose": purpose}]
    assert [row for row in page["messages"] if "[lmer private]" in row["text"] and not row.get("private")] == []

    plant_rows(session_id, [user_row(line)])
    working = transcripts.read_messages(session_id, limit=100)
    assert [(m.via, m.private) for m in working.messages] == [(transcripts.COMMS_VIA, {"purpose": purpose})]


def test_a_private_send_past_the_bus_window_is_still_a_private_entry(platform_root, monkeypatch):
    """A worker's read is its first PANE_LIMIT records, so a later private send has
    no bus turn: the harness's copy of the relayed line is all the pane holds."""
    session_id = "s-private-window"
    reference = _reference(session_id, "GitLab deploy token")
    plant_rows(session_id, [user_row(f"[platform · operator] {reference}")])
    monkeypatch.setattr(platform_comms, "PANE_LIMIT", 2)
    plant_record(session_id, 1, "note", "one")
    plant_record(session_id, 2, "note", "two")
    plant_record(session_id, 3, "message", reference, channel="platform", author="operator",
                 meta={"private": {"purpose": "GitLab deploy token"}})

    page = transcripts.read_messages(session_id, limit=100)
    assert [m.text for m in page.messages if m.via == transcripts.COMMS_VIA] == ["one", "two"], (
        "the private record is inside the read window, so this exercises nothing")
    [echo] = [m for m in page.messages if m.via is None]
    assert echo.to_dict()["private"] == {"purpose": "GitLab deploy token"}
    assert [m for m in page.messages if "[lmer private]" in m.text and not m.private] == []


def test_a_private_send_typed_with_no_bus_is_a_private_entry(platform_root):
    session_id = "s-private-nobus"
    plant_rows(session_id, [user_row(_reference(session_id))])
    page = transcripts.read_messages(session_id, limit=100)
    assert [m for m in page.messages if m.via == transcripts.COMMS_VIA] == []
    [echo] = page.messages
    assert echo.to_dict()["private"] == {"purpose": None}


def test_only_the_operators_exact_reference_line_is_a_private_echo(platform_root):
    session_id = "s-private-lookalike"
    reference = _reference(session_id)
    plant_rows(session_id, [
        user_row(f"[slack · someone] {reference}"),
        user_row(f"quoting: {reference}"),
        user_row(f"[platform · operator] {reference} Also, here is the password."),
        assistant_row(reference),
    ])
    page = transcripts.read_messages(session_id, limit=100)
    assert len(page.messages) == 4
    assert [m.private for m in page.messages] == [None] * 4


def test_the_new_assistant_tag_is_claimed(platform_root):
    """A current worker types ``[Ratchet] …``; the bus record must not show twice."""
    session_id = "s-bus-ratchet"
    plant_rows(session_id, [
        user_row("[Ratchet] the review is posted", at="2026-07-20T09:00:02.000Z"),
    ])
    plant_record(session_id, 1, "message", "the review is posted", channel="uber-lmer",
                 at="2026-07-20T09:00:01Z")
    page = transcripts.read_messages(session_id, limit=100)
    harness_copies = [m for m in page.messages if m.via is None and m.text.startswith("[Ratchet")]
    assert harness_copies == [], "the harness's copy of the relayed record is still shown"
    assert said(page).count(("user", "the review is posted")) == 1


def test_a_legacy_assistant_tag_is_still_claimed(platform_root):
    """A container started before the rename still types ``[uber lmer] …`` (#581)."""
    session_id = "s-bus-legacy"
    plant_rows(session_id, [
        user_row("[uber lmer] the review is posted", at="2026-07-20T09:00:02.000Z"),
    ])
    plant_record(session_id, 1, "message", "the review is posted", channel="uber-lmer",
                 at="2026-07-20T09:00:01Z")
    page = transcripts.read_messages(session_id, limit=100)
    harness_copies = [m for m in page.messages if m.via is None and m.text.startswith("[uber lmer")]
    assert harness_copies == [], "the legacy tag's harness copy is still shown"
    assert said(page).count(("user", "the review is posted")) == 1


def test_a_config_json_only_name_is_claimed(platform_root):
    """The host's resolved name (config.assistant_name), not just its env, decides
    which relay line is the harness's copy (review of !403)."""
    session_id = "s-bus-sprocket"
    plant_rows(session_id, [
        user_row("[Sprocket] the review is posted", at="2026-07-20T09:00:02.000Z"),
    ])
    plant_record(session_id, 1, "message", "the review is posted", channel="uber-lmer",
                 at="2026-07-20T09:00:01Z")
    page = transcripts.read_messages(session_id, limit=100, assistant_name="Sprocket")
    harness_copies = [m for m in page.messages if m.via is None and m.text.startswith("[Sprocket")]
    assert harness_copies == [], "a config-only name left the harness copy visible"
    assert said(page).count(("user", "the review is posted")) == 1


def test_a_renamed_assistant_still_claims_the_legacy_tag(platform_root):
    session_id = "s-bus-renamed-legacy"
    plant_rows(session_id, [
        user_row("[uber lmer] the review is posted", at="2026-07-20T09:00:02.000Z"),
    ])
    plant_record(session_id, 1, "message", "the review is posted", channel="uber-lmer",
                 at="2026-07-20T09:00:01Z")
    page = transcripts.read_messages(session_id, limit=100, assistant_name="Sprocket")
    harness_copies = [m for m in page.messages if m.via is None and m.text.startswith("[uber lmer")]
    assert harness_copies == [], "the legacy alias was dropped once the name changed"


def test_a_late_answer_is_labelled_and_a_command_is_the_platforms(platform_root):
    session_id = "s-bus-late"
    registry.register(session_id, pid=DEAD_PID, run=dict(RUN))
    plant_record(session_id, 1, "question", "branch?")
    plant_record(session_id, 2, "answer", "prep", channel="slack", author="tester", in_reply_to=f"{session_id}/000001")
    plant_record(session_id, 3, "answer", "main", channel="matrix", author="@a:x",
                 in_reply_to=f"{session_id}/000001", meta={"late": True, "answered_by": f"{session_id}/000002"})
    plant_record(session_id, 4, "command", "/followup", channel="uber-lmer")
    plant_record(session_id, 5, "close", "", in_reply_to=f"{session_id}/000001")
    page = transcripts.read_messages(session_id, limit=100)
    assert [(m.role, m.comms_kind, m.text) for m in page.messages] == [
        ("assistant", "question", "branch?"),
        ("user", "answer", "prep"),
        ("user", "late answer", "main"),
        (transcripts.PLATFORM_ROLE, "command", "/followup"),
        ("assistant", "close", "stopped waiting for an answer"),
    ]


def test_the_ask_channel_is_read_only_when_the_bus_has_nothing(platform_root):
    """An older image writes the ask channel and nothing else; a current one
    projects the bus into it, and reading both would double every question."""
    session_id = "s-bus-fallback"
    plant_session(session_id)
    plant_question(session_id, "old-image question?", answer="yes")
    page = transcripts.read_messages(session_id, limit=100)
    assert ("assistant", "old-image question?") in said(page)
    assert all(m.via in (None, transcripts.ASK_CHANNEL_VIA) for m in page.messages)

    plant_record(session_id, 1, "question", "old-image question?")
    page = transcripts.read_messages(session_id, limit=100)
    assert said(page).count(("assistant", "old-image question?")) == 1
    assert [m.via for m in page.messages if m.text == "old-image question?"] == [transcripts.COMMS_VIA]


def test_the_messages_route_carries_the_source(platform_root):
    session_id = "s-20260720T090000-dddd"
    plant_session(session_id)
    plant_log(session_id)
    plant_record(session_id, 1, "message", "from the room", channel="matrix", author="@a:x", ref="e1")
    client = TestClient(
        api.create_app(cfg.load(), SECRET, state_builder=lambda config, force_pull=False: {})
    )
    body = client.get(
        f"/api/sessions/{session_id}/messages?limit=100",
        headers={"Authorization": f"Bearer {SECRET}"},
    ).json()
    (turn,) = [m for m in body["messages"] if m["via"] == transcripts.COMMS_VIA]
    assert turn["source"] == {"channel": "matrix", "author": "@a:x", "ref": "e1"}
    assert turn["comms_kind"] is None and turn["role"] == "user"


def test_a_reconstructed_terminal_line_is_marked_all_the_way_to_the_view(platform_root):
    """§3.4 accepts keystroke reconstruction as a prediction on one condition:
    that the humans reading the mirror can see which lines are one. The marker
    stops being a marker if it ends at the daemon."""
    session_id = "s-20260720T090000-eeee"
    plant_session(session_id)
    plant_log(session_id)
    plant_record(session_id, 1, "message", "deploy it", channel="terminal",
                 meta={"fidelity": "reconstructed"})
    client = TestClient(
        api.create_app(cfg.load(), SECRET, state_builder=lambda config, force_pull=False: {})
    )
    body = client.get(
        f"/api/sessions/{session_id}/messages?limit=100",
        headers={"Authorization": f"Bearer {SECRET}"},
    ).json()
    (turn,) = [m for m in body["messages"] if m["via"] == transcripts.COMMS_VIA]
    assert turn["fidelity"] == "reconstructed"
    text = _chat()
    assert "message.fidelity === 'reconstructed'" in text, "the view must show it"
    assert "· typed" in text


def test_a_post_into_another_thread_says_so_all_the_way_to_the_view(platform_root):
    """The words are on this session's conversation because the run said them,
    but they were addressed to a thread it is not connected to (#414). Without
    the marker the operator reading the pane is being addressed by a message that
    was never for them."""
    session_id = "s-20260720T090000-ffff"
    plant_session(session_id)
    plant_log(session_id)
    plant_record(
        session_id, 1, "message", "the review is posted", channel="agent",
        meta={"thread": "https://example.slack.com/archives/C0123ABCDEF/p1712345678000900"},
    )
    client = TestClient(
        api.create_app(cfg.load(), SECRET, state_builder=lambda config, force_pull=False: {})
    )
    body = client.get(
        f"/api/sessions/{session_id}/messages?limit=100",
        headers={"Authorization": f"Bearer {SECRET}"},
    ).json()
    (turn,) = [m for m in body["messages"] if m["via"] == transcripts.COMMS_VIA]
    assert turn["thread"].endswith("p1712345678000900")
    text = _chat()
    assert 'v-if="threadLink(message)"' in text, "the view must show it"
    assert "posted into a thread" in text


def test_a_thread_link_the_container_chose_never_becomes_an_href(platform_root):
    """`meta.thread` is the one field on a record that becomes an `href`, and the
    path to it takes an unschema'd `meta` on the session's own token. So a session
    that never posts into a thread could otherwise choose where a link in the
    operator's browser points, under a caption saying the platform put it there.

    Dropped where the turn is built, so no consumer inherits the problem, and the
    marker still renders — as text — for a record that carries one."""
    session_id = "s-20260720T090000-aaab"
    plant_session(session_id)
    plant_log(session_id)
    for seq, hostile in enumerate(
        ["javascript:alert(1)", "https://evil.example/phish", "data:text/html,x"], start=1
    ):
        plant_record(
            session_id, seq, "message", f"claim {seq}", channel="agent",
            meta={"thread": hostile},
        )
    client = TestClient(
        api.create_app(cfg.load(), SECRET, state_builder=lambda config, force_pull=False: {})
    )
    body = client.get(
        f"/api/sessions/{session_id}/messages?limit=100",
        headers={"Authorization": f"Bearer {SECRET}"},
    ).json()
    threads = [
        m["thread"] for m in body["messages"] if m["via"] == transcripts.COMMS_VIA
    ]
    assert threads == [None, None, None]
    # And the last hop says so too: the anchor is bound to the checked value, not
    # to the record's own field, with a plain-text fallback beside it.
    text = _chat()
    assert ':href="threadLink(message)"' in text
    assert ":href=\"message.thread\"" not in text
    assert "protocol === 'https:'" in text


def test_the_view_names_the_source_and_the_kind():
    text = _chat()
    assert "sourceLabel(source)" in text and "message.comms_kind" in text
    assert "source.channel === 'uber-lmer' ? assistantName.value" in text
    api_js = (CHAT.parent.parent / "api.js").read_text(encoding="utf-8")
    send = api_js[api_js.index("export function sendSessionInput"):]
    send = send[:send.index("\n}\n")]
    assert "/comms" in send and "kind: 'message'" in send
    assert send.index("append_newline: false") < send.index("/comms"), (
        "the keystroke form must keep the raw input route"
    )


# --- the agent's own post is one message, not two ------------------------------
#
# Found by hands-on test on 2026-09-05: every uber lmer post was drawn twice in
# the chat pane. The agent writes its reply as prose — which the harness records
# — and then posts the same words with `lmer-signal post`, which the bus records.
# Both reached the page, so the pane drew the same sentence twice with the same
# timestamp. The suppression that already hid the harness's copy of an *inbound*
# record only looked at `user` turns, so nothing covered the outbound direction.
#
# The bus record is the one kept, for the reason `_merged_channel` keeps it over
# the ask projection: it is what every other channel saw, and it alone carries
# the source and kind the pane labels the turn with.


def _messages_route(session_id):
    """The page as the pane actually receives it, over the route it polls."""
    client = TestClient(
        api.create_app(cfg.load(), SECRET, state_builder=lambda config, force_pull=False: {})
    )
    return client.get(
        f"/api/sessions/{session_id}/messages?limit=100",
        headers={"Authorization": f"Bearer {SECRET}"},
    ).json()["messages"]


def test_a_worker_post_the_harness_also_wrote_is_one_turn(platform_root):
    session_id = "s-echo-worker"
    plant_rows(session_id, [assistant_row("the suite is green, pushing now")])
    plant_record(session_id, 1, "message", "the suite is green, pushing now",
                 channel="agent", at="2026-07-20T09:00:03Z")

    turns = [(m["role"], m["text"], m["comms_kind"]) for m in _messages_route(session_id)]
    assert turns == [("assistant", "the suite is green, pushing now", "message")], (
        "the agent's post and its own prose copy are one message"
    )


def test_the_assistants_own_post_is_one_turn(platform_root):
    """uber lmer's records carry `session_origin` and the channel it was launched
    with, so they never took the inbound path the old suppression looked at."""
    session_id = "s-echo-assistant"
    plant_rows(session_id, [assistant_row("three running, one waiting on you")])
    plant_record(session_id, 1, "message", "three running, one waiting on you",
                 channel="uber-lmer", at="2026-07-20T09:00:03Z",
                 meta={"session_origin": True})

    turns = [(m["role"], m["text"], m["source"]) for m in _messages_route(session_id)]
    assert turns == [
        ("assistant", "three running, one waiting on you", {"channel": "uber-lmer"}),
    ]


def test_every_outbound_kind_is_hidden_once_and_only_where_it_matches(platform_root):
    """Each record consumes one copy: prose the bus never carried stays, and the
    second of two identical replies is still a second reply."""
    session_id = "s-echo-kinds"
    plant_rows(session_id, [
        assistant_row("looking at the branch", at="2026-07-20T09:00:02.000Z"),
        assistant_row("which target branch?", at="2026-07-20T09:00:03.000Z"),
        assistant_row("on it", at="2026-07-20T09:00:04.000Z"),
        assistant_row("on it", at="2026-07-20T09:00:05.000Z"),
        assistant_row("pushed MR !300", at="2026-07-20T09:00:06.000Z"),
    ])
    plant_record(session_id, 1, "question", "which target branch?", at="2026-07-20T09:00:03Z")
    plant_record(session_id, 2, "message", "on it", at="2026-07-20T09:00:04Z")
    plant_record(session_id, 3, "milestone", "pushed MR !300", at="2026-07-20T09:00:06Z")

    assert said(transcripts.read_messages(session_id, limit=100)) == [
        ("assistant", "looking at the branch"),
        ("assistant", "which target branch?"),
        ("assistant", "on it"),
        ("assistant", "on it"),
        ("assistant", "pushed MR !300"),
    ]
    kinds = [m.comms_kind for m in transcripts.read_messages(session_id, limit=100).messages]
    assert kinds == [None, "question", "message", None, "milestone"], (
        "one prose copy per record, and the unmatched turn keeps its own place"
    )


def test_a_close_the_harness_never_wrote_consumes_nothing(platform_root):
    """`close` renders words the record does not carry, so it must not be able to
    swallow an assistant turn that happens to say them."""
    session_id = "s-echo-close"
    plant_rows(session_id, [assistant_row("stopped waiting for an answer")])
    plant_record(session_id, 1, "question", "branch?", at="2026-07-20T09:00:02Z")
    plant_record(session_id, 2, "close", "", in_reply_to=f"{session_id}/000001",
                 at="2026-07-20T09:00:04Z")

    assert said(transcripts.read_messages(session_id, limit=100)).count(
        ("assistant", "stopped waiting for an answer")
    ) == 2


def test_a_turn_that_also_called_a_tool_keeps_the_call_and_loses_the_words(platform_root):
    """The commonest shape of the bug: the harness records the reply and the
    `lmer-signal post` that sends it as one turn. Dropping it would lose the only
    record of the call, so the turn stays and only the duplicated words go."""
    session_id = "s-echo-tools"
    registry.register(session_id, pid=DEAD_PID, run=dict(RUN))
    directory = transcripts.session_transcript_dir(session_id) / "-workspace"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "rows.jsonl").write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-07-20T09:00:03.000Z",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "running the suite"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest"}},
        ]},
    }) + "\n", encoding="utf-8")
    plant_record(session_id, 1, "note", "running the suite", at="2026-07-20T09:00:03Z")

    page = transcripts.read_messages(session_id, limit=100)
    assert [m.name for m in page.messages[0].tools] == ["Bash"]
    assert page.messages[0].text == ""
    assert said(page).count(("assistant", "running the suite")) == 1


# --- what a turn is, and nothing of it is lost (issue #481) --------------------
#
# Shaped after a recorded uber lmer session (1,816 records, 2026-09-18): the
# harness writes prose, tool calls and thinking as separate records, and the
# read path served every one of its 132 assistant text blocks. These pin the
# rules that made that so, for both record shapes the harness has been seen to
# write, so a regression shows up as a test rather than as a screenshot.


def tool_row(command, at, text=None):
    """An assistant record with a tool call — and, optionally, prose in the
    same record, the combined shape a harness may write."""
    content = []
    if text:
        content.append({"type": "text", "text": text})
    content.append({
        "type": "tool_use", "id": f"t-{at}", "name": "Bash",
        "input": {"command": command},
    })
    return {"type": "assistant", "timestamp": at,
            "message": {"role": "assistant", "content": content}}


def test_prose_a_tool_call_more_prose_and_a_post_are_served_once_each(platform_root):
    session_id = "s-481-turn"
    plant_rows(session_id, [
        assistant_row("Saved the note to memory.", at="2026-07-20T09:00:02.000Z"),
        tool_row("sed -i s/a/b/ memory.md", "2026-07-20T09:00:03.000Z"),
        assistant_row("The 657 developer pushed round 2; I turn the reviewer now.",
                      at="2026-07-20T09:00:04.000Z"),
        tool_row('lmer-signal post "Round 2 is up; reviewer turned."', "2026-07-20T09:00:05.000Z"),
    ])
    plant_record(session_id, 1, "message", "Round 2 is up; reviewer turned.",
                 channel="uber-lmer", at="2026-07-20T09:00:05Z", meta={"session_origin": True})

    page = transcripts.read_messages(session_id, limit=100)
    assert [m.text for m in page.messages if m.text] == [
        "Saved the note to memory.",
        "The 657 developer pushed round 2; I turn the reviewer now.",
        "Round 2 is up; reviewer turned.",
    ]
    assert [m.via for m in page.messages if m.text] == [None, None, transcripts.COMMS_VIA]
    # Both calls are kept: the post's own tool row is the pane's to hide.
    assert [[t.name for t in m.tools] for m in page.messages if m.tools] == [["Bash"], ["Bash"]]


def test_a_post_whose_words_differ_from_the_prose_keeps_both(platform_root):
    session_id = "s-481-differs"
    plant_rows(session_id, [assistant_row("I will post the sitrep now.", at="2026-07-20T09:00:02.000Z")])
    plant_record(session_id, 1, "message", "Three runs up, one waiting on you.",
                 channel="uber-lmer", at="2026-07-20T09:00:03Z", meta={"session_origin": True})

    assert said(transcripts.read_messages(session_id, limit=100)) == [
        ("assistant", "I will post the sitrep now."),
        ("assistant", "Three runs up, one waiting on you."),
    ]


def test_prose_and_a_tool_call_in_one_record_keep_the_prose_nothing_posted(platform_root):
    """The combined record shape, beside a post of *different* words: the join
    may only blank a record whose words the bus carries."""
    session_id = "s-481-combined"
    plant_rows(session_id, [
        tool_row("sed -i s/a/b/ memory.md", "2026-07-20T09:00:03.000Z",
                 text="Noted — saved to persistent memory."),
    ])
    plant_record(session_id, 1, "message", "Three runs up, one waiting on you.",
                 channel="uber-lmer", at="2026-07-20T09:00:04Z", meta={"session_origin": True})

    page = transcripts.read_messages(session_id, limit=100)
    assert page.messages[0].text == "Noted — saved to persistent memory."
    assert [t.name for t in page.messages[0].tools] == ["Bash"]
    assert page.messages[1].text == "Three runs up, one waiting on you."


def test_a_record_is_spent_on_a_turn_saying_its_own_words_and_never_a_neighbour(platform_root):
    session_id = "s-481-neighbour"
    plant_rows(session_id, [
        assistant_row("Noted — saved.", at="2026-07-20T09:00:02.000Z"),
        assistant_row("on it", at="2026-07-20T09:00:03.000Z"),
    ])
    plant_record(session_id, 1, "message", "on it", channel="uber-lmer",
                 at="2026-07-20T09:00:03Z", meta={"session_origin": True})

    page = transcripts.read_messages(session_id, limit=100)
    assert said(page) == [("assistant", "Noted — saved."), ("assistant", "on it")]
    assert [m.comms_kind for m in page.messages] == [None, "message"], (
        "the record replaced its own copy; the neighbouring prose is untouched"
    )


def test_the_pane_reads_the_conversation_from_one_route(platform_root):
    """`/messages` is the pane's only source for the conversation, so a record
    that reaches it twice is drawn twice unless the page itself dedupes."""
    text = _chat()
    assert "fetchSessionMessages" in text
    # Anchored on the fetch identifiers rather than on the substring "/comms",
    # which appears in prose about the write path and would fail a comment edit.
    others = [name for name in ("fetchSessionComms", "fetchCommsFeed") if name in text]
    assert others == [], f"the conversation grew a second source: {others}"


def test_the_pane_names_a_turn_by_the_id_that_survives_a_renumber(platform_root):
    """Source-level because the render key is in the template, where the node
    harness in `test_web_chat_merge.py` cannot reach it. Two keys for one turn is
    the duplicate this change removes, drawn by Vue rather than by `absorb`."""
    text = _chat()
    assert ''':key="message.id ?? message.seq"''' in text, (
        "a redelivered turn is recognised by its id, so the row must be keyed on "
        "the same thing the dedupe is"
    )
    # Both reads, or the row's key is not unique: a page the backward read matched
    # by position while the row is named by id renders one turn twice under one key.
    assert text.count("!known.has(message.id)") == 2, (
        "the forward and backward reads must both dedupe by id"
    )
    assert "message.seq)" not in text.split("function absorb")[1].split("\n}\n")[0], (
        "no branch of absorb may key a turn by its position"
    )


# --- a turn keeps its name when history renumbers underneath it ----------------
#
# The operator's screenshot, 2026-09-05: two bubbles for one uber lmer post, both
# carrying the `· message` chip, both labelled uber lmer, same timestamp, on a
# pane that was open and polling. The post came from a Bash heredoc, so no prose
# turn with those words existed — the harness-copy join above cannot explain it
# and does not fix it.
#
# `read_messages` numbers by position and renumbers on every read. A transcript
# row written after the record it precedes in time lands *below* the served
# cursor, everything after it shifts up one, and the client's next forward read
# hands back the record it already drew. `seq` is that position; `id` is what
# survives it.


def _rows_path(session_id):
    return transcripts.session_transcript_dir(session_id) / "-workspace" / "rows.jsonl"


def _append_row(session_id, row):
    with _rows_path(session_id).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def test_a_late_transcript_row_does_not_redeliver_the_record_above_it(platform_root):
    session_id = "s-renumber"
    plant_rows(session_id, [assistant_row("looking at the branch", at="2026-07-20T09:00:01.000Z")])
    plant_record(session_id, 1, "message", "three running", channel="uber-lmer",
                 at="2026-07-20T09:00:05Z", meta={"session_origin": True})

    first = transcripts.read_messages(session_id, since=0, limit=100)
    record = [m for m in first.messages if m.via == transcripts.COMMS_VIA][0]
    assert record.id == f"{session_id}/000001", "a bus turn is named by its record"

    # The harness flushes the turn that ran `lmer-signal`, stamped before the post.
    _append_row(session_id, assistant_row("running the suite", at="2026-07-20T09:00:03.000Z"))

    second = transcripts.read_messages(session_id, since=first.cursor, limit=100)
    assert [m.text for m in second.messages] == ["three running"], (
        "the positional cursor still hands the record back — that is the bug"
    )
    assert [m.id for m in second.messages] == [record.id], (
        "and the id is what lets the pane recognise it as one it already holds"
    )
    assert record.seq != second.messages[0].seq, "the position moved; the name did not"


def test_a_transcript_turn_keeps_its_id_when_a_row_lands_before_it(platform_root):
    """Numbered within its own file, which is append-only — so an id is not
    disturbed by a row arriving in another source or by the bus merge."""
    session_id = "s-stable-ids"
    plant_rows(session_id, [
        assistant_row("first", at="2026-07-20T09:00:01.000Z"),
        assistant_row("second", at="2026-07-20T09:00:09.000Z"),
    ])
    before = {m.text: m.id for m in transcripts.read_messages(session_id, limit=100).messages}

    plant_record(session_id, 1, "note", "running the suite", at="2026-07-20T09:00:04Z")
    after = {m.text: m.id for m in transcripts.read_messages(session_id, limit=100).messages}

    assert before["second"] == after["second"]
    assert all(value is not None for value in after.values()), "every turn is named"
    assert len(set(after.values())) == len(after), "and no two turns share a name"


def test_every_turn_the_route_serves_carries_an_id(platform_root):
    session_id = "s-ids-wire"
    plant_rows(session_id, [assistant_row("looking at the branch")])
    plant_question(session_id, "which branch?", answer="prep-release")

    served = _messages_route(session_id)
    assert served, "fixture produced no turns"
    assert all(m["id"] for m in served), [m for m in served if not m["id"]]
    assert len({m["id"] for m in served}) == len(served)


def test_a_copy_is_spent_on_the_turn_that_produced_the_record(platform_root):
    """Bare agent prose repeats — "on it" is ordinary `note` text — so matching
    oldest-first deleted an earlier, separate statement and left the turn that
    actually posted beside its own bus turn."""
    session_id = "s-echo-repeat"
    plant_rows(session_id, [
        assistant_row("on it", at="2026-07-20T09:00:01.000Z"),
        assistant_row("on it", at="2026-07-20T09:00:10.000Z"),
    ])
    plant_record(session_id, 1, "note", "on it", at="2026-07-20T09:00:11Z")

    kept = [(m.at, m.comms_kind) for m in transcripts.read_messages(session_id, limit=100).messages]
    assert kept == [
        ("2026-07-20T09:00:01.000Z", None),
        ("2026-07-20T09:00:11Z", "note"),
    ], "the earlier statement survives; the turn that posted is the one consumed"


def test_two_transcript_files_sharing_a_stem_get_different_names(platform_root):
    """Discovery is recursive and normatively so — a harness keeps one subdirectory
    per workspace — so `a/session-7.jsonl` and `b/session-7.jsonl` are ordinary.
    Naming a turn by the stem gave both one name, and the pane kept one of them."""
    session_id = "s-stem"
    registry.register(session_id, pid=DEAD_PID, run=dict(RUN))
    root = transcripts.session_transcript_dir(session_id)
    for folder, text in (("a", "from workspace a"), ("b", "from workspace b")):
        directory = root / folder
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "session-7.jsonl").write_text(
            json.dumps(assistant_row(text)) + "\n", encoding="utf-8"
        )

    served = transcripts.read_messages(session_id, limit=100).messages
    assert sorted(m.text for m in served) == ["from workspace a", "from workspace b"]
    assert len({m.id for m in served}) == 2, [m.id for m in served]


# --- the parts of the join a review found untested (!288 round 8) --------------


def test_a_bus_record_with_no_usable_id_is_still_named(platform_root):
    """The pane recognises a turn it already holds by `id` and by nothing else, so
    a turn the route cannot name is a turn it cannot recognise — it is kept rather
    than guessed at, and a redelivered one is then drawn twice
    (`test_web_chat_merge.py::test_a_turn_the_route_could_not_name_is_kept_...`).
    The journal's own cursor is stable across reads, so there is always a name."""
    session_id = "s-unnamed"
    registry.register(session_id, pid=DEAD_PID, run=dict(RUN))
    record = plant_record(session_id, 1, "message", "posted with no id", channel="agent")
    platform_comms.ensure_hub().journal.append(session_id, dict(record, id=None))

    served = transcripts.read_messages(session_id, limit=100).messages
    assert served, "fixture produced no turns"
    assert all(m.id for m in served), [m.to_dict() for m in served if not m.id]
    assert any(m.id.startswith(f"{session_id}:comms#") for m in served)


def test_an_undated_copy_prefers_a_dated_turn(platform_root):
    """Every inbound copy is undated by design, so this branch governs the whole
    inbound half. `_UNDATED` orders an undated turn last on both branches."""
    session_id = "s-undated"
    plant_rows(session_id, [
        dict(assistant_row("on it"), timestamp=None),
        assistant_row("on it", at="2026-07-20T09:00:05.000Z"),
    ])
    plant_record(session_id, 1, "note", "on it", at="not-a-timestamp")

    turns = transcripts.read_messages(session_id, limit=100).messages
    kept = [m.at for m in turns if m.text == "on it" and m.via is None]
    assert kept == [None], "the dated turn is taken; the undated one is left standing"
    assert [m.comms_kind for m in turns if m.via == transcripts.COMMS_VIA] == ["note"]


def test_the_tail_fallback_is_the_inbound_half_only(platform_root):
    """A dated copy must not reach the tail path: it is the one path that does not
    bound a claim by the record's time, and confining it is what makes the
    docstring's "never one after it" true by construction rather than by odds.

    Both truncation ends matter — the bus refuses over-limit text rather than
    cutting it, and both sides cut with the same `keep="tail"` — so a change at
    either end would restore the duplicate silently. This states it."""
    long = "z" * (transcripts.TEXT_LIMIT + 400)
    session_id = "s-tail-dated"
    plant_rows(session_id, [assistant_row(long, at="2026-07-20T09:00:09.000Z")])
    # A dated record whose text differs at the head but shares the kept tail.
    plant_record(session_id, 1, "note", "different start " + long,
                 at="2026-07-20T09:00:02Z")

    turns = transcripts.read_messages(session_id, limit=100).messages
    assert sum(1 for m in turns if m.via is None) == 1, (
        "a dated copy claimed a turn later than its own record through the tail path"
    )
    assert [m.comms_kind for m in turns if m.via == transcripts.COMMS_VIA] == ["note"], (
        "and the record itself is on the page, so the count above means what it says"
    )


def test_a_recorded_pointer_is_named_by_its_path_under_the_transcript_root():
    """`locate_sources` prefers a recorded pointer, which lies outside the derived
    session directory — so resolving against that directory alone named every turn
    of the *preferred* resolution by a digest."""
    root = Path(transcripts.transcript_root())
    assert transcripts._source_name("s-x", root / "-workspace" / "abc.jsonl") == (
        "-workspace/abc.jsonl"
    )
    outside = transcripts._source_name("s-x", Path("/elsewhere/abc.jsonl"))
    assert re.fullmatch(r"[0-9a-f]{16}", outside), outside


@pytest.mark.parametrize('combined', [False, True])
def test_late_post_reconciles_through_the_real_reader_and_pane(platform_root, combined):
    from tests.conftest import node_binary, require_node_toolchain
    node = node_binary()
    if node is None:
        require_node_toolchain("Node is required for the pane reconciliation regression")
    import subprocess

    session = 'late-post-pane'
    text = 'The requested review is ready for you.'
    prose = assistant_row(text)
    call = {'type': 'tool_use', 'id': 'post', 'name': 'Bash',
            'input': {'command': 'lmer-signal post --stdin'}}
    if combined:
        prose['message']['content'].append(call)
        rows = [prose]
    else:
        command = assistant_row('', at='2026-07-20T09:00:04Z')
        command['message']['content'] = [call]
        rows = [prose, command]
    plant_rows(session, rows)
    before = transcripts.read_messages(session).to_dict()
    plant_record(session, 1, 'message', text, at='2026-07-20T09:00:05Z')
    after = transcripts.read_messages(session).to_dict()
    script = r'''
      const fs = require('fs');
      const { before, after, text } = JSON.parse(fs.readFileSync(0, 'utf8'));
      const source = fs.readFileSync('web/src/components/Chat.vue', 'utf8');
      const code = source.slice(source.indexOf('function absorb('), source.indexOf('\nasync function start()'));
      const factory = new Function('fetchSessionMessages', `
        const props = { sessionId: 'late-post-pane', view: 'conversation' };
        const messages = { value: [] }, following = { value: false }, total = {}, sessions = {}, note = {}, live = {}, problem = {}, phase = { value: 'ready' };
        let cursor = 0, generation = 0, polling = false;
        const PAGE = 40, stale = () => false, settlePending = () => {}, nextTick = () => {}, stickToBottom = () => {};
        ${code}
        return { absorb, poll, messages };
      `);
      const pane = factory(async (_id, { since, limit }) => {
        const start = since < 0 ? Math.max(0, after.total + since) : Math.min(since, after.total);
        const messages = after.messages.slice(start, start + limit);
        return { ...after, messages, from: start, cursor: start + messages.length };
      });
      pane.absorb(before, 'replace');
      (async () => {
        await pane.poll();
        const rows = pane.messages.value;
        const speech = rows.filter(m => m.via === 'comms');
        if (speech.length !== 1 || speech[0].text !== text) throw Error('late post missing');
        if (rows.filter(m => m.text === text).length !== 1) throw Error('prose duplicated');
        await pane.poll();
        if (JSON.stringify(rows) !== JSON.stringify(pane.messages.value)) throw Error('repeated poll changed membership');
      })().catch(error => { console.error(error); process.exitCode = 1; });
    '''
    result = subprocess.run([node, '-e', script],
                            input=json.dumps({'before': before, 'after': after, 'text': text}),
                            text=True, capture_output=True, timeout=30, cwd=WEB.parent)
    assert result.returncode == 0, result.stdout + result.stderr
