"""`lmer-signal ask`/`wait`/`close` record an accepted answer in the run (#674)."""
import subprocess

import pytest

from ask_channel import signal_cli
from tests.conftest import strip_lmer_env
from tests.test_lmer_signal_cli import Rig


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


@pytest.fixture
def work_calls(monkeypatch, tmp_path):
    """A session with a work repo, and a `work` that records its argv."""
    monkeypatch.setenv(signal_cli.WORK_REPO_PATH_ENV, str(tmp_path / "work"))
    monkeypatch.setattr(signal_cli.shutil, "which", lambda name: f"/fake/bin/{name}")
    calls = []
    outcome = {"rc": 0, "stderr": "", "raise": None}

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if outcome["raise"]:
            raise outcome["raise"]
        return subprocess.CompletedProcess(argv, outcome["rc"], stdout="", stderr=outcome["stderr"])

    monkeypatch.setattr(signal_cli.subprocess, "run", fake_run)
    return calls, outcome


def test_an_answered_ask_records_the_answer_by_its_id(rig, work_calls, capsys):
    calls, _ = work_calls
    thread = rig.answer(1, "prep-release", when_waiting=True)
    assert signal_cli.main(["ask", "which branch?", "--timeout", "30", "--interval", "0.1"]) == 0
    thread.join()
    assert capsys.readouterr().out == "prep-release\n"
    ((argv, kwargs),) = calls
    assert argv == ["/fake/bin/work", "decision", "--ref", "sess-1/000002"]
    assert kwargs["timeout"] == signal_cli.RECORD_DECISION_TIMEOUT


def test_wait_and_close_record_too(rig, work_calls, capsys):
    calls, _ = work_calls
    assert signal_cli.main(["ask", "q?", "--no-wait"]) == 0
    rig.answer(1, "yes").join()
    assert signal_cli.main(["wait", "1"]) == 0
    assert signal_cli.main(["close", "1"]) == 0
    assert [argv[-1] for argv, _ in calls] == ["sess-1/000002", "sess-1/000002"]


def test_a_failed_record_is_one_stderr_line_and_keeps_exit_zero(rig, work_calls, capsys):
    _calls, outcome = work_calls
    outcome.update(rc=1, stderr="Resolving run...\n❌ No run context\n")
    assert signal_cli.main(["ask", "q?", "--no-wait"]) == 0
    rig.answer(1, "yes").join()
    assert signal_cli.main(["wait", "1"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip().splitlines()[-1] == "yes"
    assert "answer not recorded in the run: ❌ No run context" in captured.err
    assert "record it with `work decision --ref sess-1/000002`" in captured.err


def test_a_hung_work_cli_does_not_fail_the_wait(rig, work_calls, capsys):
    _calls, outcome = work_calls
    outcome["raise"] = subprocess.TimeoutExpired(["work"], 60)
    assert signal_cli.main(["ask", "q?", "--no-wait"]) == 0
    rig.answer(1, "yes").join()
    assert signal_cli.main(["wait", "1"]) == 0
    assert "answer not recorded in the run" in capsys.readouterr().err


def test_an_unexpected_error_does_not_fail_the_wait(rig, work_calls, capsys):
    _calls, outcome = work_calls
    outcome["raise"] = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad byte")
    assert signal_cli.main(["ask", "q?", "--no-wait"]) == 0
    rig.answer(1, "yes").join()
    assert signal_cli.main(["wait", "1"]) == 0
    assert "answer not recorded in the run" in capsys.readouterr().err


def test_no_work_repo_means_no_record(rig, work_calls, monkeypatch, capsys):
    calls, _ = work_calls
    monkeypatch.delenv(signal_cli.WORK_REPO_PATH_ENV)
    assert signal_cli.main(["ask", "q?", "--no-wait"]) == 0
    rig.answer(1, "yes").join()
    assert signal_cli.main(["wait", "1"]) == 0
    assert calls == []


def test_a_timeout_records_nothing(rig, work_calls):
    calls, _ = work_calls
    assert signal_cli.main(["ask", "anyone?", "--timeout", "0.3", "--interval", "0.1"]) == 2
    assert calls == []
