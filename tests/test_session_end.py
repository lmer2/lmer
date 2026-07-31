"""``lmer-end-session``: the generic way an agent ends its own session.

Why this exists at all is the interesting part. The mechanism used to live only
inside ``lmer-slack end-session``, so the only agents that could end their own
session were ones with a Slack thread — and the ``chat`` taskdef's instructions
were the only place it was written down. That became a real hole when the
orchestrator grew a **wind down** verb (spec §7.5 / D22): wind-down asks the agent
to land its work and end the session, and a ``develop`` run had no generic way to
comply. The operator got a button whose effect depended on the agent guessing.

So the properties under test are mostly about *not being Slack*, and about the
exit-status contract, which is what makes a clean ending distinguishable from a
crash.
"""

import os
import signal

import pytest

from ask_channel import protocol as ask_protocol
from lmer_cli import session_end, supervisor
from lmer_platform import lifecycle
from slack_chat import cli as slack_cli
from tests.conftest import denied_access, strip_lmer_env


@pytest.fixture(autouse=True)
def _no_inherited_supervisor(monkeypatch):
    """Never read a real supervisor PID — or a real ask channel — out of the
    ambient environment (issue #93)."""
    strip_lmer_env(monkeypatch)
    monkeypatch.delenv(session_end.SUPERVISOR_PID_ENV, raising=False)


@pytest.fixture
def signalled(monkeypatch):
    """A supervisor to signal, and the record of whether it was."""
    sent = []
    monkeypatch.setenv(session_end.SUPERVISOR_PID_ENV, "4242")
    monkeypatch.setattr(session_end.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


@pytest.fixture
def channel(tmp_path, monkeypatch):
    """An ask channel of the shape the orchestrator mounts into a session."""
    directory = tmp_path / "ask"
    directory.mkdir()
    monkeypatch.setenv(ask_protocol.ASK_DIR_ENV, str(directory))
    return directory


# --- the contract with the supervisor ---------------------------------------

def test_the_pid_variable_is_the_one_the_supervisor_publishes():
    """Three modules named this string by hand; two now alias the third.

    A mismatch would not fail loudly — ``end-session`` would simply report "no
    supervisor" forever while a perfectly good one was running.
    """
    assert session_end.SUPERVISOR_PID_ENV == supervisor.SUPERVISOR_PID_ENV
    assert slack_cli.SUPERVISOR_PID_ENV == session_end.SUPERVISOR_PID_ENV


def test_it_signals_the_supervisor_with_sigusr1(monkeypatch):
    """SIGUSR1 is the supervisor's "the agent asked to leave" signal: it injects
    the harness's quit chord so the harness exits 0 and the host reaper reads a
    deliberate sign-off rather than a crash."""
    sent = []
    monkeypatch.setenv(session_end.SUPERVISOR_PID_ENV, "4242")
    monkeypatch.setattr(session_end.os, "kill", lambda pid, sig: sent.append((pid, sig)))

    result = session_end.request_session_end()

    assert result.ok
    assert sent == [(4242, signal.SIGUSR1)]


def test_it_does_not_kill_the_harness_itself(monkeypatch):
    """The whole reason this is a signal to the supervisor and not self-slaughter.

    A harness killed from inside is indistinguishable from one that died, so the
    platform would report a session the agent ended cleanly as a crash.
    """
    monkeypatch.setenv(session_end.SUPERVISOR_PID_ENV, "4242")
    targets = []
    monkeypatch.setattr(session_end.os, "kill", lambda pid, sig: targets.append((pid, sig)))

    session_end.request_session_end()

    assert targets, "nothing was signalled at all"
    for pid, sig in targets:
        assert pid == 4242, f"signalled {pid} rather than the supervisor"
        assert sig == signal.SIGUSR1, f"sent {sig!r}, which is not a request to quit"


# --- refusals, and why their codes differ ------------------------------------

@pytest.mark.parametrize("raw", ["", "   ", "not-a-pid", "0", "-1", "12.5"])
def test_an_unusable_pid_is_never_signalled(monkeypatch, raw):
    """``0`` and ``-1`` are the dangerous ones and the reason this is a test.

    ``os.kill(0, sig)`` signals the caller's own process group and
    ``os.kill(-1, sig)`` signals every process the user owns, so a malformed
    variable reaching ``os.kill`` would take out far more than this session.
    """
    monkeypatch.setenv(session_end.SUPERVISOR_PID_ENV, raw)
    monkeypatch.setattr(
        session_end.os, "kill",
        lambda pid, sig: pytest.fail(f"signalled {pid} from {raw!r}"),
    )

    result = session_end.request_session_end()

    assert result.code == session_end.NO_SUPERVISOR_EXIT_CODE
    assert session_end.SUPERVISOR_PID_ENV in result.message


def test_no_supervisor_reads_differently_from_a_failed_attempt(monkeypatch):
    """A wind-down needs to tell these apart: "this session never had a
    supervisor" is a configuration fact, "the signal failed" is a fault."""
    monkeypatch.setenv(session_end.SUPERVISOR_PID_ENV, "4242")

    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(session_end.os, "kill", gone)
    assert session_end.request_session_end().code == session_end.NO_SUPERVISOR_EXIT_CODE

    def refused(pid, sig):
        raise PermissionError("not yours")

    monkeypatch.setattr(session_end.os, "kill", refused)
    assert session_end.request_session_end().code == 1

    assert session_end.NO_SUPERVISOR_EXIT_CODE != 1, (
        "the two outcomes must not share an exit code — a caller cannot act on "
        "the difference if they do"
    )


def test_it_never_raises_at_an_agent(monkeypatch):
    """The caller is usually an agent winding down; a traceback is a worse thing
    to hand it than a sentence saying what happened."""
    monkeypatch.setenv(session_end.SUPERVISOR_PID_ENV, "4242")

    def explode(pid, sig):
        raise OSError("something unexpected")

    monkeypatch.setattr(session_end.os, "kill", explode)

    result = session_end.request_session_end()  # must not raise

    assert not result.ok
    assert result.message


# --- the CLI ----------------------------------------------------------------

def test_the_cli_reports_the_result_code(monkeypatch):
    monkeypatch.setenv(session_end.SUPERVISOR_PID_ENV, "4242")
    monkeypatch.setattr(session_end.os, "kill", lambda pid, sig: None)
    assert session_end.main([]) == 0

    monkeypatch.delenv(session_end.SUPERVISOR_PID_ENV)
    assert session_end.main([]) == session_end.NO_SUPERVISOR_EXIT_CODE


def test_a_failure_goes_to_stderr_even_when_quiet(monkeypatch, capsys):
    """``--quiet`` silences success, not failure: a wind-down that could not end
    the session must say so somewhere the operator can find it."""
    monkeypatch.delenv(session_end.SUPERVISOR_PID_ENV, raising=False)

    session_end.main(["--quiet"])

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no supervisor" in captured.err


# --- the one refusal: an answer nobody read ----------------------------------
#
# The incident: a session asked the operator something, timed out the wait, was
# told to resume it, worked for an hour, never waited again, and recorded the
# question as unanswered — while the answer was on the channel the whole time.
# Re-arming was enforced by prose, and prose did not hold. This command is the
# last thing an agent runs, so it is where a mechanical check can still catch it.

def test_an_unread_answer_refuses_the_end_and_hands_the_answer_over(
    channel, signalled, capsys,
):
    """Refusing is only half of it: the refusal *is* the delivery, so the answer's
    full text goes in it — nothing else will show the agent that reply."""
    question = ask_protocol.post_question(channel, "which branch should this target?")
    ask_protocol.write_answer(channel, question, "target prep-release, not main")

    code = session_end.main([])

    assert code == session_end.UNREAD_ANSWER_EXIT_CODE
    assert signalled == [], "the session was ended over an answer nobody had read"
    err = capsys.readouterr().err
    assert f"question {question.id}" in err
    assert "target prep-release, not main" in err
    assert "run lmer-end-session again" in err


def test_the_second_attempt_ends_the_session(channel, signalled, capsys):
    """One refusal, not a wall: the refusal delivered the answer and recorded that
    it did, so a second attempt with nothing unread proceeds. No flag, no state
    beyond the receipt."""
    question = ask_protocol.post_question(channel, "ship it?")
    ask_protocol.write_answer(channel, question, "yes")

    assert session_end.main([]) == session_end.UNREAD_ANSWER_EXIT_CODE
    assert ask_protocol.receipt_for(channel, question).via == "end-session"

    assert session_end.main([]) == 0
    assert signalled == [(4242, signal.SIGUSR1)]


def test_an_answer_the_agent_already_read_does_not_refuse(channel, signalled):
    """The ordinary ending: the agent waited, got the answer, acted, and left."""
    question = ask_protocol.post_question(channel, "ship it?")
    ask_protocol.write_answer(channel, question, "yes")
    ask_protocol.mark_answer_read(channel, question, via="wait")

    assert session_end.main([]) == 0
    assert signalled == [(4242, signal.SIGUSR1)]


def test_an_open_unanswered_question_does_not_block_the_end(channel, signalled):
    """Ending with a question outstanding is legitimate — the run-level answer flow
    picks it up after the session is gone (spec D23). Only a *reply* nobody read is
    the failure, so blocking on an unanswered question would make this refusal
    something an agent learns to work around."""
    ask_protocol.post_question(channel, "still waiting on this one")
    ask_protocol.post_note(channel, "a note nobody answers")

    assert session_end.main([]) == 0
    assert signalled == [(4242, signal.SIGUSR1)]


def test_a_channel_that_cannot_be_read_still_ends_the_session(
    tmp_path, monkeypatch, signalled, capsys,
):
    """The backstop must never wedge a shutdown. A mount that is not there is
    reported on stderr and then ignored."""
    monkeypatch.setenv(ask_protocol.ASK_DIR_ENV, str(tmp_path / "never-mounted"))

    assert session_end.main([]) == 0
    assert signalled == [(4242, signal.SIGUSR1)]
    assert "cannot check the operator channel" in capsys.readouterr().err


def test_an_unwritable_channel_still_ends_the_session(channel, signalled, capsys):
    """What a uid mismatch across the bind mount looks like from inside: the
    channel is there and this user cannot use it. An answer may be sitting in it
    unseen, which is worth a line — and not worth blocking the shutdown over,
    because the receipt could not be written either."""
    channel.chmod(0o500)
    try:
        # Injected, not enforced: uid 0 is exempt, so the mode bit alone makes
        # this test pass locally and never reach its assertion in CI.
        with denied_access(channel):
            assert session_end.main([]) == 0
    finally:
        channel.chmod(0o700)
    assert signalled == [(4242, signal.SIGUSR1)]
    assert "cannot check the operator channel" in capsys.readouterr().err


def test_a_session_with_no_channel_says_nothing_about_one(signalled, capsys):
    """An unorchestrated session has no channel by design; warning about it every
    time would train an agent to ignore the line that matters."""
    assert session_end.main([]) == 0
    assert signalled == [(4242, signal.SIGUSR1)]
    assert "operator channel" not in capsys.readouterr().err


def test_a_refusal_that_cannot_be_recorded_is_not_made(
    channel, signalled, monkeypatch, capsys,
):
    """A refusal whose receipt does not land would refuse every attempt, forever —
    the backstop wedging the shutdown it was meant to make safe. The answer is
    still delivered; only the refusal is skipped."""
    question = ask_protocol.post_question(channel, "ship it?")
    ask_protocol.write_answer(channel, question, "yes, and mind the migration")

    def refuse(*args, **kwargs):
        raise ask_protocol.AskError("cannot write the receipt (read-only channel)")

    monkeypatch.setattr(ask_protocol, "mark_answer_read", refuse)

    assert session_end.main([]) == 0
    assert signalled == [(4242, signal.SIGUSR1)]
    err = capsys.readouterr().err
    assert "yes, and mind the migration" in err, "the answer must still be delivered"
    assert "could not record" in err


def test_the_refusal_code_collides_with_nothing(channel, signalled):
    """A wind-down branches on these: "act on the answer and run me again" must not
    read as "this session could not be ended"."""
    assert len({
        0, 1, session_end.NO_SUPERVISOR_EXIT_CODE, session_end.UNREAD_ANSWER_EXIT_CODE,
    }) == 4


# --- not Slack any more ------------------------------------------------------

def test_the_slack_verb_delegates_rather_than_duplicating(monkeypatch):
    """One implementation. Two copies of a signal-sending routine is how one of
    them keeps a bug the other one fixed."""
    calls = []
    monkeypatch.setattr(
        slack_cli, "request_session_end",
        lambda: calls.append("shared") or session_end.SessionEndResult(0, "ok", pid=1),
    )

    class _Args:
        text = None
        stdin = False
        message_file = None
        permalink = None

    assert slack_cli._cmd_end_session(_Args(), client=None) == 0
    assert calls == ["shared"], "the Slack path signalled on its own"


def test_ending_a_session_needs_no_slack_configuration(monkeypatch):
    """The point of the whole change: a develop run has no thread, no token and no
    channel, and must still be able to end itself."""
    for name in (
        "SLACK_BOT_TOKEN", "LMER_SLACK_CHANNEL", "LMER_SLACK_THREAD_TS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(session_end.SUPERVISOR_PID_ENV, "4242")
    monkeypatch.setattr(session_end.os, "kill", lambda pid, sig: None)

    assert session_end.request_session_end().ok


# --- the wind-down prompt can now be acted on --------------------------------

def test_the_wind_down_prompt_names_the_command(monkeypatch):
    """A wind-down that cannot be complied with is a button that does nothing.

    The prompt used to say "however your instructions say a session ends", which
    for a develop run was nothing at all.
    """
    assert "lmer-end-session" in lifecycle.WIND_DOWN_PROMPT


def test_the_wind_down_prompt_is_still_one_line():
    """Naming a command must not have introduced a newline: the prompt is typed
    into a raw-mode TUI, where a newline mid-paragraph submits a partial
    instruction (see the lifecycle module docstring)."""
    assert "\n" not in lifecycle.WIND_DOWN_PROMPT
    assert "\r" not in lifecycle.WIND_DOWN_PROMPT


def test_the_named_command_is_actually_installed():
    """A prompt naming a command that no console script provides would be worse
    than the vague wording it replaced."""
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]

    assert "lmer-end-session" in scripts
    assert scripts["lmer-end-session"] == "lmer_cli.session_end:main"
