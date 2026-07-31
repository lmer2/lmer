"""Tests for the container-side ``lmer-ask`` CLI (issue #141, slice M2 / T23).

The CLI is the whole contract an agent sees (spec D27), so what is asserted here
is what a shell script can rely on: which stream carries the answer, what the
exit codes mean, and — the one that decides whether this feature helps or hurts —
that a session with no channel fails immediately instead of blocking on a reply
that can never arrive.
"""

import threading

import pytest

from ask_channel import cli, protocol
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def channel(tmp_path, monkeypatch):
    directory = tmp_path / "ask"
    directory.mkdir()
    monkeypatch.setenv(protocol.ASK_DIR_ENV, str(directory))
    return directory


def run(argv):
    return cli.main(argv)


# --- posting -----------------------------------------------------------------

def test_asking_without_waiting_prints_the_id(channel, capsys):
    assert run(["ask", "--no-wait", "which branch?"]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed == "000001"
    assert protocol.read_entry(channel, printed).text == "which branch?"


def test_options_are_recorded_with_the_question(channel, capsys):
    run(["ask", "--no-wait", "rebase or merge?", "--option", "rebase",
         "--option", "merge"])
    entry = protocol.read_entry(channel, capsys.readouterr().out.strip())
    assert entry.options == ("rebase", "merge")


def test_a_note_expects_no_reply(channel, capsys):
    assert run(["note", "cloning, back in a minute"]) == 0
    entry = protocol.read_entry(channel, capsys.readouterr().out.strip())
    assert entry.kind == protocol.KIND_NOTE


def test_text_from_a_file_is_verbatim(channel, tmp_path, capsys):
    """The reason the flag exists: a question about a command has backticks in it."""
    body = tmp_path / "q.md"
    body.write_text("run `git status`? $HOME is odd\n", encoding="utf-8")
    assert run(["ask", "--no-wait", "--message-file", str(body)]) == 0
    entry = protocol.read_entry(channel, capsys.readouterr().out.strip())
    assert entry.text == "run `git status`? $HOME is odd"


def test_text_from_stdin_is_verbatim(channel, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", _FakeStdin("`backticks` and $vars\n"))
    assert run(["ask", "--no-wait", "-"]) == 0
    entry = protocol.read_entry(channel, capsys.readouterr().out.strip())
    assert entry.text == "`backticks` and $vars"


class _FakeStdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


@pytest.mark.parametrize("argv", [
    ["ask"],
    ["ask", "here", "--message-file", "/tmp/x"],
    ["note"],
])
def test_ambiguous_or_missing_text_is_an_error(channel, argv, capsys):
    assert run(argv) == 1
    assert "exactly once" in capsys.readouterr().err


def test_an_oversized_question_is_an_error_not_a_post(channel, capsys):
    assert run(["ask", "--no-wait", "x" * (protocol.MAX_QUESTION_CHARS + 1)]) == 1
    assert "over the" in capsys.readouterr().err
    assert protocol.read_entries(channel) == []


# --- waiting -----------------------------------------------------------------

def test_the_answer_goes_to_stdout_alone(channel, capsys):
    """`answer=$(lmer-ask ask …)` has to work, so stdout carries nothing else."""
    question = protocol.post_question(channel, "planted")
    protocol.write_answer(channel, question, "the reply")
    assert run(["wait", question.id, "--timeout", "5", "--interval", "0.01"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "the reply\n"
    assert "the reply" not in captured.err


def test_ask_blocks_until_the_operator_replies(channel, capsys):
    """The real loop against a real directory, answered from another thread."""
    answered = threading.Event()

    def answer_soon():
        for _ in range(500):
            open_questions = protocol.open_questions(channel)
            if open_questions:
                protocol.write_answer(channel, open_questions[0], "go ahead")
                answered.set()
                return
            threading.Event().wait(0.01)

    helper = threading.Thread(target=answer_soon, daemon=True)
    helper.start()
    code = run(["ask", "ready?", "--timeout", "10", "--interval", "0.01"])
    helper.join(timeout=5)

    assert answered.is_set(), "the helper never saw the question"
    assert code == 0
    assert capsys.readouterr().out == "go ahead\n"


def test_a_timeout_is_its_own_exit_code_and_leaves_the_question_open(channel, capsys):
    code = run(["ask", "anyone there?", "--timeout", "0.05", "--interval", "0.01"])
    assert code == cli.TIMEOUT_EXIT_CODE
    assert code != 1, "a quiet operator is not an error"
    err = capsys.readouterr().err
    assert "lmer-ask wait 000001" in err, "the agent needs the id to resume"
    assert len(protocol.open_questions(channel)) == 1


def test_the_timeout_message_teaches_the_whole_loop(channel, capsys):
    """The resume line alone did not hold. An agent had it in front of it, went on
    to other work, never waited again, and recorded "unanswered" over an answer
    that was on the channel — so the message carries the loop, the rule about the
    final check, and the watch suggestion for a session that goes idle."""
    assert run(["ask", "anyone there?", "--timeout", "0.05",
                "--interval", "0.01"]) == cli.TIMEOUT_EXIT_CODE
    err = capsys.readouterr().err

    assert "lmer-ask wait 000001" in err
    assert "Answers arrive while you work" in err, "the re-arm loop is missing"
    assert "re-run" in err
    assert "HARD RULE" in err, "the final check is stated as a rule or not at all"
    assert "before you record any stop reason" in err
    assert "one final time" in err
    assert "watch" in err, "an idle agent needs to be told to arm something"


def test_a_timeout_files_no_read_receipt(channel):
    """Nothing was delivered, so nothing may claim it was: the backstop at session
    end has to still see this question as unanswered rather than as read."""
    run(["ask", "anyone there?", "--timeout", "0.05", "--interval", "0.01"])
    assert not (channel / f"000001{protocol.READ_SUFFIX}").exists()


def test_the_default_wait_ends_before_a_harness_would_kill_it():
    """Read, never waited for (a 1-CPU box has no business timing 90 seconds).

    The exit-2 contract only exists if *this* process is the one that gives up:
    killed at the harness's own command timeout, the agent gets a tool error
    instead of "still open, resume with `lmer-ask wait <id>`" and has no reason to
    believe the question survived. Hence a default under the floor, with headroom
    for interpreter start, one overshooting poll, and a slow link.

    Two-sided on purpose: a default so short that no operator could ever answer in
    time would satisfy the first assertion and be useless.
    """
    assert cli.DEFAULT_TIMEOUT < cli.HARNESS_COMMAND_TIMEOUT_FLOOR
    headroom = cli.HARNESS_COMMAND_TIMEOUT_FLOOR - cli.DEFAULT_TIMEOUT
    assert headroom >= 30, (
        f"only {headroom:g}s of headroom under a "
        f"{cli.HARNESS_COMMAND_TIMEOUT_FLOOR:g}s harness timeout"
    )
    assert cli.DEFAULT_TIMEOUT >= 60, (
        "a wait nobody could answer in time is not a wait"
    )


def test_the_wait_flags_quote_the_default_they_will_use(capsys):
    """The help is what an agent reads; a stale number there is a lie about both."""
    with pytest.raises(SystemExit):
        run(["wait", "--help"])
    # Whitespace-normalised: argparse wraps the help text at the terminal width.
    text = " ".join(capsys.readouterr().out.split())
    assert f"default {cli.DEFAULT_TIMEOUT:g}" in text
    assert f"default {cli.DEFAULT_INTERVAL:g}" in text


@pytest.mark.parametrize("flags", [
    ["--interval", "0"], ["--interval", "-1"], ["--timeout", "-5"],
])
def test_a_wait_that_would_spin_is_refused(channel, flags, capsys):
    """`--interval 0` is a busy loop, not a faster answer; and it must not post."""
    assert run(["ask", "anyone?", *flags]) == 1
    assert protocol.read_entries(channel) == [], (
        "the question must not be left on the channel with nobody waiting"
    )
    err = capsys.readouterr().err
    assert "--interval" in err or "--timeout" in err


def test_a_bad_wait_flag_does_not_stop_a_plain_post(channel, capsys):
    """--no-wait never sleeps, so the wait flags are not its business."""
    assert run(["ask", "--no-wait", "anyone?", "--interval", "0"]) == 0


def test_waiting_on_an_unknown_question_is_an_error(channel, capsys):
    assert run(["wait", "000042"]) == 1
    assert "no question" in capsys.readouterr().err


def test_waiting_on_a_note_is_an_error(channel, capsys):
    note = protocol.post_note(channel, "just saying")
    assert run(["wait", note.id]) == 1
    assert "notes are not answered" in capsys.readouterr().err


def test_an_answer_belonging_to_another_question_is_refused(channel, capsys):
    """Never silently accepted: exit 1, and the agent stays blocked, on purpose."""
    question = protocol.post_question(channel, "which one?")
    (channel / f"{question.id}{protocol.ANSWER_SUFFIX}").write_text(
        '{"question_id": "000099", "text": "not for you"}', encoding="utf-8"
    )
    assert run(["wait", question.id, "--timeout", "1", "--interval", "0.01"]) == 1
    err = capsys.readouterr().err
    assert "000099" in err


def test_a_traversing_question_id_is_refused(channel, capsys):
    """Refused as an id, before it is ever joined to the channel's path."""
    assert run(["wait", "../../../etc/passwd"]) == 1
    assert "invalid entry id" in capsys.readouterr().err


# --- closing ------------------------------------------------------------------
#
# What `close` is for: a question outlives the wait that posted it, and until it is
# answered the operator is offered a reply box for it. Closing says "I stopped
# waiting" so nobody types into that box for nothing.

def test_close_marks_the_question_and_keeps_it_on_the_channel(channel, capsys):
    """Marked, never deleted: the question is a record of what the agent asked."""
    question = protocol.post_question(channel, "which branch?")
    assert run(["close", question.id]) == 0
    entry = protocol.read_entry(channel, question.id)
    assert entry is not None, "closing must not remove the question"
    assert entry.text == "which branch?"
    assert entry.closed is True
    assert (channel / f"{question.id}{protocol.QUESTION_SUFFIX}").exists()


def test_a_closed_question_is_no_longer_open(channel, capsys):
    question = protocol.post_question(channel, "anyone there?")
    assert run(["close", question.id]) == 0
    capsys.readouterr()
    assert protocol.open_questions(channel) == []
    assert run(["list", "--open"]) == 0
    assert capsys.readouterr().out == ""


def test_close_records_the_reason_it_was_given(channel):
    question = protocol.post_question(channel, "rebase or merge?")
    assert run(["close", question.id, "--reason", "timed out, rebased"]) == 0
    closure = protocol.read_entry(channel, question.id).closure
    assert closure.reason == "timed out, rebased"
    assert closure.question_id == question.id
    assert closure.nonce == question.nonce, "the pairing check needs the nonce"


def test_an_oversized_reason_does_not_close_the_question(channel, capsys):
    question = protocol.post_question(channel, "ok?")
    long_reason = "x" * (protocol.MAX_REASON_CHARS + 1)
    assert run(["close", question.id, "--reason", long_reason]) == 1
    assert "over the" in capsys.readouterr().err
    assert protocol.read_entry(channel, question.id).closed is False


def test_closing_twice_is_not_an_error(channel, capsys):
    """This is what an exit path calls; a second close is done, not a failure."""
    question = protocol.post_question(channel, "anyone there?")
    assert run(["close", question.id]) == 0
    assert run(["close", question.id]) == 0
    assert "already closed" in capsys.readouterr().err


def test_an_answer_that_beat_the_close_is_handed_over_not_discarded(channel, capsys):
    """The race decision: the operator's work wins, and reaches the agent.

    Nothing is filed, so no reader ever has to choose — and the answer lands on
    stdout, where the agent's `$(…)` was already looking.
    """
    question = protocol.post_question(channel, "ship it?")
    protocol.write_answer(channel, question, "yes, ship it")
    assert run(["close", question.id]) == 0
    captured = capsys.readouterr()
    assert captured.out == "yes, ship it\n"
    assert "answered before it could be closed" in captured.err
    assert not (channel / f"{question.id}{protocol.CLOSED_SUFFIX}").exists()
    assert protocol.answer_for(channel, question).text == "yes, ship it"


def test_an_answer_that_lost_the_race_still_reads_as_the_answer(channel, capsys):
    """Both records on disk — the window `close` cannot close by checking first.

    Written by hand because nothing in the CLI produces this pair: it takes an
    answer landing between the read and the link inside `close`.
    """
    question = protocol.post_question(channel, "ship it?")
    protocol.close_question(channel, question)
    protocol.write_answer(channel, question, "yes")
    entry = protocol.read_entry(channel, question.id)
    assert entry.answered and entry.closed
    assert protocol.is_answerable(entry) is False
    assert run(["list"]) == 0
    assert f"[{question.id}] answered (unread):" in capsys.readouterr().out


def test_waiting_on_a_closed_question_says_so_instead_of_blocking(channel, capsys):
    """Exit 1, not the timeout code: the timeout code promises it is still open."""
    question = protocol.post_question(channel, "anyone there?")
    run(["close", question.id])
    capsys.readouterr()
    assert run(["wait", question.id, "--timeout", "0.05", "--interval", "0.01"]) == 1
    assert "was closed" in capsys.readouterr().err


def test_waiting_on_a_closed_question_that_was_answered_returns_the_answer(
    channel, capsys,
):
    question = protocol.post_question(channel, "ship it?")
    protocol.close_question(channel, question)
    protocol.write_answer(channel, question, "yes")
    assert run(["wait", question.id, "--timeout", "0.05", "--interval", "0.01"]) == 0
    assert capsys.readouterr().out == "yes\n"


def test_closing_an_unknown_question_is_an_error(channel, capsys):
    assert run(["close", "000042"]) == 1
    assert "no question" in capsys.readouterr().err


def test_closing_a_note_is_an_error(channel, capsys):
    note = protocol.post_note(channel, "just saying")
    assert run(["close", note.id]) == 1
    assert "notes are not answered" in capsys.readouterr().err


def test_a_traversing_id_cannot_be_closed(channel, capsys):
    assert run(["close", "../../../etc/passwd"]) == 1
    assert "invalid entry id" in capsys.readouterr().err


def test_a_question_with_no_close_record_is_open(channel):
    """Absent means answerable, and has to keep meaning that: a session on an
    older image writes no close record at all, ever."""
    question = protocol.post_question(channel, "which branch?")
    assert not (channel / f"{question.id}{protocol.CLOSED_SUFFIX}").exists()
    entry = protocol.read_entry(channel, question.id)
    assert entry.closed is False
    assert protocol.is_answerable(entry) is True
    assert [open_q.id for open_q in protocol.open_questions(channel)] == [question.id]


def test_a_stale_close_record_does_not_close_a_fresh_question(channel):
    """Same id, different question — the nonce is what tells them apart."""
    import json

    question = protocol.post_question(channel, "the new question")
    (channel / f"{question.id}{protocol.CLOSED_SUFFIX}").write_text(
        json.dumps({
            "question_id": question.id,
            "nonce": "some-earlier-questions-nonce",
            "closed_at": "2026-07-01T00:00:00Z",
        }),
        encoding="utf-8",
    )
    assert protocol.read_entry(channel, question.id).closed is False


def test_an_unreadable_close_record_leaves_the_question_answerable(channel):
    """Fails open, unlike a torn answer: a close only ever takes a reply away."""
    question = protocol.post_question(channel, "still open?")
    (channel / f"{question.id}{protocol.CLOSED_SUFFIX}").write_text(
        "{ torn", encoding="utf-8"
    )
    assert protocol.read_entry(channel, question.id).closed is False
    assert [entry.id for entry in protocol.open_questions(channel)] == [question.id]


def test_a_closed_question_frees_its_slot_under_the_open_cap(channel):
    """The cap counts what an operator could still reply to, not what was asked."""
    for index in range(protocol.MAX_OPEN_QUESTIONS):
        protocol.post_question(channel, f"question {index}")
    with pytest.raises(protocol.AskError, match="which is the limit"):
        protocol.post_question(channel, "one too many")
    assert run(["close", "000001"]) == 0
    assert protocol.post_question(channel, "room again").id == "000033"


def test_the_timeout_message_offers_the_close_verb(channel, capsys):
    """Where an agent finds out the verb exists: the moment it needs it."""
    assert run(["ask", "anyone there?", "--timeout", "0.05",
                "--interval", "0.01"]) == cli.TIMEOUT_EXIT_CODE
    assert "lmer-ask close 000001" in capsys.readouterr().err


# --- no channel ---------------------------------------------------------------

def test_a_session_that_is_not_orchestrated_fails_immediately(monkeypatch, capsys):
    """The failure mode this whole feature is judged on: refuse, never hang."""
    monkeypatch.delenv(protocol.ASK_DIR_ENV, raising=False)
    assert run(["ask", "anyone?", "--timeout", "600"]) == cli.NO_CHANNEL_EXIT_CODE
    err = capsys.readouterr().err
    assert "not started by the lmer orchestrator" in err


def test_an_unmounted_channel_fails_the_same_way(tmp_path, monkeypatch, capsys):
    """The variable is set but the mount is not there — same answer, own words."""
    monkeypatch.setenv(protocol.ASK_DIR_ENV, str(tmp_path / "not-mounted"))
    assert run(["ask", "anyone?", "--timeout", "600"]) == cli.NO_CHANNEL_EXIT_CODE
    assert "mount is missing" in capsys.readouterr().err


def test_the_no_channel_code_is_distinct_from_every_other_outcome():
    """A script branches on these, so they must not collide."""
    assert len({0, 1, cli.TIMEOUT_EXIT_CODE, cli.NO_CHANNEL_EXIT_CODE}) == 4


def test_an_explicit_dir_works_without_the_env_var(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(protocol.ASK_DIR_ENV, raising=False)
    directory = tmp_path / "ask"
    directory.mkdir()
    assert run(["--dir", str(directory), "note", "hello"]) == 0
    assert protocol.read_entries(directory)[0].text == "hello"


# --- listing -----------------------------------------------------------------

def test_list_shows_open_answered_and_notes(channel, capsys):
    open_q = protocol.post_question(channel, "still open?")
    protocol.post_note(channel, "a note")
    done = protocol.post_question(channel, "settled?")
    protocol.write_answer(channel, done, "yes")

    assert run(["list"]) == 0
    out = capsys.readouterr().out
    assert f"[{open_q.id}] open: still open?" in out
    assert "note: a note" in out
    # Nobody has been handed this answer yet, and that is said in the leading
    # position where a scanning agent finds it.
    assert f"[{done.id}] answered (unread): settled?" in out
    assert "answer: yes" in out


def test_list_shows_a_closed_question_as_closed(channel, capsys):
    question = protocol.post_question(channel, "gone stale?")
    run(["close", question.id, "--reason", "decided it myself"])
    capsys.readouterr()

    assert run(["list"]) == 0
    out = capsys.readouterr().out
    assert f"[{question.id}] closed: gone stale?" in out
    assert "closed because: decided it myself" in out


def test_list_open_only(channel, capsys):
    protocol.post_note(channel, "a note")
    protocol.post_question(channel, "still open?")
    assert run(["list", "--open"]) == 0
    out = capsys.readouterr().out
    assert "a note" not in out
    assert "still open?" in out


def test_list_marks_an_answer_that_was_read(channel, capsys):
    """The other half of the distinction: once the agent has been handed the
    answer, the line stops shouting about it."""
    question = protocol.post_question(channel, "settled?")
    protocol.write_answer(channel, question, "yes")
    assert run(["wait", question.id, "--timeout", "1", "--interval", "0.01"]) == 0
    capsys.readouterr()

    assert run(["list"]) == 0
    out = capsys.readouterr().out
    assert f"[{question.id}] answered: settled?" in out
    assert "unread" not in out


def test_list_json_carries_the_read_flag(channel, capsys):
    """A machine reading the channel gets the same distinction as the prose."""
    import json

    question = protocol.post_question(channel, "settled?")
    protocol.write_answer(channel, question, "yes")
    assert run(["list", "--json"]) == 0
    record = json.loads(capsys.readouterr().out.splitlines()[0])
    assert record["answered"] is True
    assert record["answer_read"] is False
    assert record["receipt"] is None


def test_list_json_is_one_object_per_line(channel, capsys):
    import json

    protocol.post_question(channel, "one", ["a"])
    protocol.post_note(channel, "two")
    assert run(["list", "--json"]) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [record["kind"] for record in records] == ["question", "note"]
    assert records[0]["options"] == ["a"]


# --- read receipts ------------------------------------------------------------
#
# Which verbs record a read is a decision, not an accident: a receipt means this
# answer's text was put in front of the agent as the answer to this question. The
# verbs whose whole output is that text mark it; the survey verb does not, because
# it is where unread-ness is reported and a survey that consumed receipts would
# erase the state it exists to show.

def test_waiting_records_that_the_answer_was_read(channel, capsys):
    question = protocol.post_question(channel, "which branch?")
    protocol.write_answer(channel, question, "prep-release")

    assert run(["wait", question.id, "--timeout", "1", "--interval", "0.01"]) == 0

    assert capsys.readouterr().out == "prep-release\n"
    receipt = protocol.receipt_for(channel, question)
    assert receipt is not None, "the answer was printed and nothing recorded it"
    assert receipt.via == "wait"
    assert protocol.unread_answers(channel) == []


def test_asking_and_being_answered_records_the_read(channel, capsys):
    """`ask` is post-then-wait, so it delivers exactly as `wait` does.

    Answered from another thread, as in the blocking test above: `ask` allocates
    the id itself, so there is no answer to plant before the command runs.
    """
    def answer_soon():
        for _ in range(500):
            open_questions = protocol.open_questions(channel)
            if open_questions:
                protocol.write_answer(channel, open_questions[0], "go ahead")
                return
            threading.Event().wait(0.01)

    helper = threading.Thread(target=answer_soon, daemon=True)
    helper.start()
    code = run(["ask", "ready?", "--timeout", "10", "--interval", "0.01"])
    helper.join(timeout=5)

    assert code == 0
    assert capsys.readouterr().out == "go ahead\n"
    assert protocol.unread_answers(channel) == [], (
        "the answer was printed and nothing recorded the read"
    )


def test_close_records_the_read_of_an_answer_that_beat_it(channel, capsys):
    """It hands the answer over on stdout exactly as `wait` would, so it is the
    same delivery — and the session must not then be refused an end over it."""
    question = protocol.post_question(channel, "ship it?")
    protocol.write_answer(channel, question, "yes, ship it")

    assert run(["close", question.id]) == 0

    assert capsys.readouterr().out == "yes, ship it\n"
    assert protocol.receipt_for(channel, question).via == "close"
    assert protocol.unread_answers(channel) == []


def test_listing_does_not_consume_the_receipt(channel, capsys):
    """The decision, pinned. `list` is a survey: an agent that runs it for a status
    line must not thereby clear the signal that catches a stranded answer — and it
    is the verb that *displays* unread-ness, which it could not do if it cleared
    it."""
    question = protocol.post_question(channel, "settled?")
    protocol.write_answer(channel, question, "yes")

    assert run(["list"]) == 0
    assert run(["list", "--json"]) == 0
    assert run(["list", "--open"]) == 0
    capsys.readouterr()

    assert not (channel / f"{question.id}{protocol.READ_SUFFIX}").exists()
    assert [entry.id for entry in protocol.unread_answers(channel)] == [question.id]


def test_a_receipt_that_cannot_be_written_does_not_fail_the_delivery(
    channel, capsys, monkeypatch,
):
    """The answer is already on stdout; turning that into an error would lose it.

    A lost receipt costs one redundant delivery at session end, which is the
    direction that cannot strand a reply.
    """
    question = protocol.post_question(channel, "which branch?")
    protocol.write_answer(channel, question, "prep-release")

    def refuse(*args, **kwargs):
        raise protocol.AskError("cannot write 000001.read.json (disk is full)")

    monkeypatch.setattr(protocol, "mark_answer_read", refuse)

    assert run(["wait", question.id, "--timeout", "1", "--interval", "0.01"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "prep-release\n"
    assert "recording that you read it failed" in captured.err


# --- the parser ---------------------------------------------------------------

def test_no_subcommand_prints_usage(capsys):
    assert run([]) == 1
    assert "usage" in capsys.readouterr().err.lower()


def test_help_names_every_verb(capsys):
    with pytest.raises(SystemExit):
        run(["--help"])
    out = capsys.readouterr().out
    for verb in ("ask", "note", "wait", "close", "list"):
        assert verb in out
