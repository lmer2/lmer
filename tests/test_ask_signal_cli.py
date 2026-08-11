"""Tests for the container-side ``lmer-signal`` CLI (issue #141, T122).

Its own file rather than a section of ``tests/test_ask_cli.py``, because it is its
own command with its own contract: no waiting, no ids to resume, no exit code for
"nobody answered yet". What is asserted here is what an agent's shell can rely on
— the id on stdout, an unorchestrated session refused rather than left writing
into a directory nobody reads, and the two exit codes shared with ``lmer-ask``
rather than re-invented.
"""

import pytest

from ask_channel import cli, protocol, signal_cli
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
    return signal_cli.main(argv)


class _FakeStdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


# --- filing a signal ----------------------------------------------------------

def test_signalling_files_the_record_and_prints_its_id(channel, capsys):
    assert run(["pushed MR !167 for review"]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed == "000001"
    (signal,) = protocol.read_signals(channel)
    assert (signal.id, signal.text) == (printed, "pushed MR !167 for review")


def test_the_signal_stays_out_of_the_operator_s_feed(channel):
    """The whole reason this is a second command: the operator is not the reader."""
    assert run(["the review is finished"]) == 0
    assert protocol.read_entries(channel) == []


def test_text_from_stdin_is_verbatim(channel, capsys, monkeypatch):
    """A milestone line names what was pushed, so it carries backticks."""
    monkeypatch.setattr("sys.stdin", _FakeStdin("pushed `fix/T122` — $HOME safe\n"))
    assert run(["-"]) == 0
    (signal,) = protocol.read_signals(channel)
    assert signal.text == "pushed `fix/T122` — $HOME safe"
    assert capsys.readouterr().out.strip() == signal.id


def test_text_from_a_file_is_verbatim(channel, tmp_path):
    body = tmp_path / "milestone.md"
    body.write_text("done with `T122`\n", encoding="utf-8")
    assert run(["--message-file", str(body)]) == 0
    assert protocol.read_signals(channel)[0].text == "done with `T122`"


def test_an_explicit_directory_beats_the_env_var(tmp_path, channel):
    other = tmp_path / "other"
    other.mkdir()
    assert run(["--dir", str(other), "pushed it"]) == 0
    assert len(protocol.read_signals(other)) == 1
    assert protocol.read_signals(channel) == []


# --- refusals -----------------------------------------------------------------

def test_an_unorchestrated_session_is_told_so_and_exits_three(monkeypatch, capsys):
    """No channel, and creating one would post a milestone into the void.

    Exit 3 rather than 1 for the reason ``lmer-ask`` has it: the caller's move is to
    carry on in its ordinary output, not to retry.
    """
    monkeypatch.delenv(protocol.ASK_DIR_ENV, raising=False)
    assert run(["pushed it"]) == signal_cli.NO_CHANNEL_EXIT_CODE
    assert "not started by the lmer orchestrator" in capsys.readouterr().err


def test_a_missing_mount_is_not_a_channel_either(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "never-mounted"
    monkeypatch.setenv(protocol.ASK_DIR_ENV, str(missing))
    assert run(["pushed it"]) == signal_cli.NO_CHANNEL_EXIT_CODE
    assert "does not exist" in capsys.readouterr().err
    assert not missing.exists()


def test_an_oversized_signal_is_an_error_not_a_post(channel, capsys):
    assert run(["x" * (protocol.MAX_SIGNAL_CHARS + 1)]) == 1
    assert "over the" in capsys.readouterr().err
    assert protocol.read_signals(channel) == []


@pytest.mark.parametrize("argv", [
    [],
    ["here", "--message-file", "/tmp/x"],
])
def test_ambiguous_or_missing_text_is_an_error(channel, argv, capsys):
    assert run(argv) == 1
    assert "exactly once" in capsys.readouterr().err
    assert protocol.read_signals(channel) == []


def test_the_exit_codes_are_the_ones_lmer_ask_already_taught(channel):
    """One vocabulary for both commands: an agent learns exit 3 once.

    Read off the other CLI's constant rather than restated, so renumbering it
    cannot leave the two commands disagreeing in the same session.
    """
    assert signal_cli.NO_CHANNEL_EXIT_CODE == cli.NO_CHANNEL_EXIT_CODE
    assert not hasattr(signal_cli, "TIMEOUT_EXIT_CODE"), (
        "nothing is waited for, so there is no timeout to report"
    )
