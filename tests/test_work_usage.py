"""The run's own usage record: ``work usage`` and the final write (#647)."""

from __future__ import annotations

import re
import shutil
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from lmer_cli import gate_lock
from lmer_cli.container import clone_and_exec
from lmer_usage import buckets, record
from tests.conftest import OPERATIONAL_WORK_REPO, strip_lmer_env
from tests.schema2_fixtures import build_schema2_plexus, start_session
from tests.timing_budgets import SETTLE, XDIST_GROUP
from work_repo import cli as work_cli
from work_repo import run_state, transactions, usage, utils

FIXTURE = Path(__file__).parent / "fixtures" / "usage" / "claude" / "session-a"
LEGACY_SESSION = "20261105-101500-1-1111"


@pytest.fixture
def transcripts(tmp_path, monkeypatch):
    """This session's transcripts: a copy of the claude fixture, and nothing of
    the real container's own."""
    home = tmp_path / "claude-projects" / "-workspace"
    shutil.copytree(FIXTURE, home / "session-a")
    monkeypatch.setattr(usage, "session_dirs", lambda: [tmp_path / "claude-projects"])
    return home / "session-a"


@pytest.fixture
def no_gate(monkeypatch):
    monkeypatch.setattr(usage.gate_lock, "active_gate", lambda *a, **k: None)


def _legacy(monkeypatch, tmp_path, session=LEGACY_SESSION):
    strip_lmer_env(monkeypatch)
    for key, value in {
        "LMER_WORK_REPO_PATH": str(tmp_path / "work"),
        "LMER_REPO_HOST": "git.example.com",
        "LMER_REPO_PROJECT": "org/repo",
        "LMER_TASK": "develop",
        "LMER_TASK_TARGET": "https://git.example.com/org/repo/-/issues/123",
        "LMER_SESSION_ID": session,
    }.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "work").mkdir(exist_ok=True)
    utils._ANNOUNCED_MANIFEST_PROBLEMS.clear()
    rdir, _ = run_state.ensure_run()
    return rdir


def _schema1(monkeypatch, tmp_path, session=LEGACY_SESSION):
    strip_lmer_env(monkeypatch)
    root = tmp_path / "work1"
    root.mkdir(exist_ok=True)
    (root / "_lmer.yaml").write_text(
        "schema: 1\nname: example\nprojects:\n  widgets:\n    repo: git.example.test/acme/widgets\n"
    )
    for key, value in {
        "LMER_WORK_REPO_PATH": str(root),
        "LMER_REPO_HOST": "git.example.test",
        "LMER_REPO_PROJECT": "acme/widgets",
        "LMER_REPO_URL": "https://git.example.test/acme/widgets",
        "LMER_TASK": "develop",
        "LMER_TASK_TARGET": "https://git.example.test/acme/widgets/-/issues/7",
        "LMER_SESSION_ID": session,
    }.items():
        monkeypatch.setenv(key, value)
    utils._ANNOUNCED_MANIFEST_PROBLEMS.clear()
    rdir, _ = run_state.ensure_run()
    return rdir


def _schema2(monkeypatch, tmp_path):
    tree = build_schema2_plexus(tmp_path / "work2")
    rdir, _ = start_session(monkeypatch, tree.root, task="chat")
    return rdir


FORMS = {"legacy": _legacy, "schema1": _schema1, "schema2": _schema2}


def _read(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


class TestPlacement:
    @pytest.mark.parametrize("form", sorted(FORMS))
    def test_checkpoint_writes_one_file_per_session_in_the_run_dir(
        self, form, monkeypatch, tmp_path, transcripts, no_gate,
    ):
        rdir = FORMS[form](monkeypatch, tmp_path)
        path = usage.checkpoint()
        session = run_state.current_session_id()
        assert path == rdir / "ai-usage" / f"{session}.yaml"
        data = _read(path)
        assert record.validate(data) == []
        assert data["key"]["form"] == form
        assert data["key"]["session"] == session
        assert data["key"]["uid"] == (session if form == "schema2" else None)
        assert data["key"]["slug"] == run_state.load_state(rdir)["slug"]
        assert (data["status"], data["final"]) == ("running", False)
        assert data["totals"]["requests"] == 7

    def test_lineage_joined_sessions_never_overwrite_each_other(
        self, monkeypatch, tmp_path, transcripts, no_gate,
    ):
        rdir = _schema1(monkeypatch, tmp_path)
        first = usage.checkpoint()
        # A later launch joined to the same record dir, with fewer transcripts.
        monkeypatch.setenv("LMER_SESSION_ID", "20261105-120000-1-2222")
        monkeypatch.setattr(usage.run_state, "run_dir", lambda *a, **k: rdir)
        (transcripts / "main.jsonl").unlink()
        second = usage.checkpoint()
        assert first != second and first.parent == second.parent
        assert _read(first)["totals"]["requests"] == 7
        assert _read(second)["totals"]["requests"] < 7

    def test_no_record_dir_is_created_without_a_state(self, monkeypatch, tmp_path, transcripts, no_gate):
        rdir = _legacy(monkeypatch, tmp_path)
        (rdir / "state.yaml").unlink()
        assert usage.checkpoint() is None
        assert not (rdir / "ai-usage").exists()

    def test_an_unsafe_session_id_writes_nothing(self, monkeypatch, tmp_path, transcripts, no_gate):
        rdir = _legacy(monkeypatch, tmp_path)
        monkeypatch.setenv("LMER_SESSION_ID", "../escape")
        assert usage.checkpoint() is None
        assert not (rdir / "ai-usage").exists()


class TestCheckpoint:
    def test_unchanged_counters_write_nothing(self, monkeypatch, tmp_path, transcripts, no_gate):
        _legacy(monkeypatch, tmp_path)
        path = usage.checkpoint()
        before = path.read_bytes()
        assert usage.checkpoint() is None
        assert path.read_bytes() == before

    def test_changed_counters_rewrite(self, monkeypatch, tmp_path, transcripts, no_gate):
        _legacy(monkeypatch, tmp_path)
        (transcripts / "subagents" / "agent-def456.jsonl").unlink()
        path = usage.checkpoint()
        assert _read(path)["totals"]["requests"] == 6
        shutil.copy(FIXTURE / "subagents" / "agent-def456.jsonl", transcripts / "subagents")
        assert usage.checkpoint() == path
        assert _read(path)["totals"]["requests"] == 7

    def test_a_running_gate_suppresses_the_checkpoint(self, monkeypatch, tmp_path, transcripts):
        rdir = _legacy(monkeypatch, tmp_path)
        monkeypatch.setattr(usage.gate_lock, "active_gate", lambda *a, **k: {"pid": 1})
        assert usage.checkpoint() is None
        assert not (rdir / "ai-usage").exists()

    def test_a_final_record_is_not_reopened_by_a_checkpoint(self, monkeypatch, tmp_path, transcripts, no_gate):
        _legacy(monkeypatch, tmp_path)
        path = usage.finalise(0)
        (transcripts / "main.jsonl").unlink()
        assert usage.checkpoint() is None
        assert _read(path)["final"] is True

    def test_the_interval_limits_commit_refreshes(self, monkeypatch, tmp_path, transcripts, no_gate):
        _legacy(monkeypatch, tmp_path)
        (transcripts / "subagents" / "agent-def456.jsonl").unlink()
        now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
        path = usage.checkpoint(respect_interval=True, now=now)
        shutil.copy(FIXTURE / "subagents" / "agent-def456.jsonl", transcripts / "subagents")
        assert usage.checkpoint(respect_interval=True, now=now + timedelta(seconds=60)) is None
        later = now + timedelta(seconds=usage.DEFAULT_CHECKPOINT_SECONDS + 1)
        assert usage.checkpoint(respect_interval=True, now=later) == path

    def test_fewer_counts_never_overwrite_a_record(self, monkeypatch, tmp_path, transcripts, no_gate):
        _legacy(monkeypatch, tmp_path)
        path = usage.checkpoint()
        before = path.read_bytes()
        (transcripts / "main.jsonl").unlink()
        assert usage.checkpoint() is None
        assert path.read_bytes() == before

    def test_nothing_to_record_writes_no_first_record(self, monkeypatch, tmp_path, no_gate):
        rdir = _legacy(monkeypatch, tmp_path)
        monkeypatch.setattr(usage, "session_dirs", lambda: [])
        assert usage.checkpoint() is None
        assert not (rdir / "ai-usage").exists()

    def test_cli_checkpoint_commits_only_the_record_file(self, monkeypatch, tmp_path, transcripts, no_gate):
        rdir = _legacy(monkeypatch, tmp_path)
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            with patch.object(work_cli.sys, "argv", ["work", "usage", "--checkpoint"]):
                assert work_cli.main() == 0
        rel = (rdir / "ai-usage" / f"{LEGACY_SESSION}.yaml").relative_to(tmp_path / "work").as_posix()
        assert push.call_args.args[0] == [rel]

    def test_cli_checkpoint_with_nothing_new_commits_nothing(self, monkeypatch, tmp_path, transcripts, no_gate):
        _legacy(monkeypatch, tmp_path)
        usage.checkpoint()
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            with patch.object(work_cli.sys, "argv", ["work", "usage", "--checkpoint"]):
                assert work_cli.main() == 0
        push.assert_not_called()

    def test_work_commit_refreshes_under_the_interval(self, monkeypatch, tmp_path, transcripts, no_gate):
        rdir = _legacy(monkeypatch, tmp_path)
        with patch("work_repo.cli.commit_work_changes", return_value=0), \
                patch("work_repo.cli.commit_napkin_if_subdir", return_value=0), \
                patch("work_repo.cli.push_napkin_if_separate", return_value=0), \
                patch("work_repo.cli.report_uncommitted_work_items"):
            assert work_cli.cmd_commit("msg") == 0
        assert (rdir / "ai-usage" / f"{LEGACY_SESSION}.yaml").is_file()


class TestFinal:
    def test_session_end_writes_final_before_its_push(self, monkeypatch, tmp_path, transcripts):
        rdir = _legacy(monkeypatch, tmp_path)
        # The final write ignores a running gate, like session-end's own push.
        monkeypatch.setattr(usage.gate_lock, "active_gate", lambda *a, **k: {"pid": 1})
        path = rdir / "ai-usage" / f"{LEGACY_SESSION}.yaml"
        seen = {}

        def push(rels, *a, **k):
            seen["exists_at_push"] = path.is_file()
            return 0

        with patch("work_repo.cli.commit_work_path", side_effect=push):
            assert work_cli.cmd_session_end() == 0
        assert seen["exists_at_push"]
        data = _read(path)
        assert (data["final"], data["status"], data["partial"]) == (True, "ended", False)

    def test_session_end_writes_its_state_inside_the_checkout_lock(self, monkeypatch, tmp_path, transcripts):
        """load, usage, write and push are one locked sequence: a second
        session-end (the hook and the runner's backstop) cannot land between."""
        _legacy(monkeypatch, tmp_path)
        original, held = run_state.write_state, []

        def write_state(*args, **kwargs):
            held.append(bool(getattr(transactions._local, "held", set())))
            return original(*args, **kwargs)

        monkeypatch.setattr(run_state, "write_state", write_state)
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert work_cli.cmd_session_end() == 0
        assert held and all(held)

    def test_exit_code_marks_crashed_and_ended_never_overwrites_it(self, monkeypatch, tmp_path, transcripts):
        _legacy(monkeypatch, tmp_path)
        path = usage.finalise(None)
        assert _read(path)["status"] == "ended"
        usage.finalise(3)
        assert _read(path)["status"] == "crashed"
        usage.finalise(0)
        assert _read(path)["status"] == "crashed"

    def test_cli_session_end_passes_the_exit_code(self, monkeypatch, tmp_path, transcripts):
        rdir = _legacy(monkeypatch, tmp_path)
        with patch("work_repo.cli.commit_work_path", return_value=0):
            with patch.object(work_cli.sys, "argv", ["work", "session-end", "--exit-code", "137"]):
                assert work_cli.main() == 0
        assert _read(rdir / "ai-usage" / f"{LEGACY_SESSION}.yaml")["status"] == "crashed"

    def test_a_slow_final_pass_writes_the_last_checkpoint_as_partial(
        self, monkeypatch, tmp_path, transcripts, no_gate,
    ):
        _legacy(monkeypatch, tmp_path)
        checkpointed = usage.checkpoint()
        before = _read(checkpointed)

        never = threading.Event()
        path = usage.finalise(0, summarise_fn=lambda files: never.wait(5), timeout=0.2)
        data = _read(path)
        assert (data["final"], data["partial"]) == (True, True)
        assert data["totals"] == before["totals"]
        assert record.validate(data) == []


    def test_a_partial_final_keeps_the_reported_cost_and_uncounted_files(
        self, monkeypatch, tmp_path, transcripts, no_gate,
    ):
        _legacy(monkeypatch, tmp_path)
        checkpointed = usage.checkpoint()
        before = _read(checkpointed)
        before["harness_reported_cost"] = [{"currency": "USD", "total": 1.25}]
        before["models"].append({"harness": None, "model": None, "agent": None,
                                 "status": "not_collected", "reason": "unreadable", **buckets.zero()})
        assert record.validate(before) == []
        checkpointed.write_text(yaml.safe_dump(before))
        never = threading.Event()

        data = _read(usage.finalise(0, summarise_fn=lambda files: never.wait(5), timeout=0.2))
        assert (data["final"], data["partial"]) == (True, True)
        assert data["harness_reported_cost"] == [{"currency": "USD", "total": 1.25}]
        assert [r["reason"] for r in data["models"] if r["status"] == "not_collected"] == ["unreadable"]
        assert record.validate(data) == []

    def test_a_partial_final_keeps_the_childrens_own_days(self, monkeypatch, tmp_path, transcripts, no_gate):
        _legacy(monkeypatch, tmp_path)
        child = {"harness": "codex", "model": "gpt-5.5-codex", "agent": "reviewer",
                 "child_session": "c" * 32, "status": "collected", **buckets.make(output=1000)}
        day = "2026-11-06"
        monkeypatch.setattr(usage, "EXTRA_ROW_PROVIDERS",
                            [lambda session: ([child], {day: buckets.make(output=1000)})])
        before = _read(usage.checkpoint())
        assert before["child_days"] == {day: buckets.make(output=1000)}
        never = threading.Event()

        data = _read(usage.finalise(0, summarise_fn=lambda files: never.wait(5), timeout=0.2))
        assert (data["final"], data["partial"]) == (True, True)
        assert (data["days"], data["child_days"], data["totals"]) == (
            before["days"], before["child_days"], before["totals"])
        assert record.validate(data) == []

    def test_the_final_deadline_covers_the_children(self, monkeypatch, tmp_path, transcripts, no_gate):
        _legacy(monkeypatch, tmp_path)
        usage.checkpoint()
        never = threading.Event()

        def slow_children(session):
            never.wait(5)
            return [], {}

        monkeypatch.setattr(usage, "EXTRA_ROW_PROVIDERS", [slow_children])
        data = _read(usage.finalise(0, timeout=0.2))
        assert (data["final"], data["partial"]) == (True, True)

    def test_a_final_pass_that_counts_less_keeps_the_record_as_partial(
        self, monkeypatch, tmp_path, transcripts, no_gate,
    ):
        _legacy(monkeypatch, tmp_path)
        before = _read(usage.checkpoint())
        (transcripts / "main.jsonl").unlink()
        data = _read(usage.finalise(0))
        assert (data["final"], data["status"], data["partial"]) == (True, "ended", True)
        assert data["totals"] == before["totals"]
        assert record.validate(data) == []


class TestOperationalRepoGuard:
    @pytest.mark.parametrize("operational", [True, False])
    def test_the_suite_has_no_usage_context_in_the_operational_work_repo(self, operational, monkeypatch, tmp_path):
        """conftest's guard: an in-process `work commit` carrying the session's
        own env once wrote a zero record over the running session's real one."""
        rdir = OPERATIONAL_WORK_REPO / "runs" / "some-run" if operational else tmp_path / "some-run"
        monkeypatch.setattr(usage.run_state, "run_dir", lambda: rdir)
        monkeypatch.setattr(usage.run_state, "load_state", lambda path: {"taskdef": "develop"})
        monkeypatch.setattr(usage.run_state, "current_session_id", lambda: LEGACY_SESSION)
        assert (usage._context() is None) is operational


class TestNeverBlocks:
    def test_a_usage_error_does_not_fail_session_end(self, monkeypatch, tmp_path, transcripts):
        _legacy(monkeypatch, tmp_path)
        monkeypatch.setattr(usage, "session_files", lambda *a: 1 / 0)
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            assert work_cli.cmd_session_end(2) == 0
        push.assert_called_once()

    def test_a_usage_error_does_not_fail_work_commit(self, monkeypatch, tmp_path, transcripts, no_gate):
        _legacy(monkeypatch, tmp_path)
        monkeypatch.setattr(usage, "build", lambda *a, **k: 1 / 0)
        with patch("work_repo.cli.commit_work_changes", return_value=0), \
                patch("work_repo.cli.commit_napkin_if_subdir", return_value=0), \
                patch("work_repo.cli.push_napkin_if_separate", return_value=0), \
                patch("work_repo.cli.report_uncommitted_work_items"):
            assert work_cli.cmd_commit("msg") == 0


class TestOptOut:
    @pytest.mark.parametrize("env", [{"LMER_USAGE": "0"}, {"LMER_USAGE_CHILD": "1"}])
    def test_opted_out_and_fan_out_children_write_nothing(self, env, monkeypatch, tmp_path, transcripts, no_gate):
        rdir = _legacy(monkeypatch, tmp_path)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        assert usage.checkpoint() is None
        assert usage.finalise(0) is None
        assert not (rdir / "ai-usage").exists()


class TestShow:
    def test_plain_usage_prints_and_writes_nothing(self, monkeypatch, tmp_path, transcripts, capsys):
        rdir = _legacy(monkeypatch, tmp_path)
        with patch.object(work_cli.sys, "argv", ["work", "usage"]):
            assert work_cli.main() == 0
        assert '"requests": 7' in capsys.readouterr().out
        assert not (rdir / "ai-usage").exists()


class TestLinked:
    @pytest.mark.parametrize("target,expected", [
        ("https://gitlab.example.com/group/project/-/issues/315", {"kind": "issue", "ref": "group/project#315"}),
        ("https://gitlab.example.com/group/project/-/merge_requests/12", {"kind": "mr", "ref": "group/project!12"}),
        ("https://github.com/o/r/pull/9", {"kind": "mr", "ref": "o/r!9"}),
        (".", {"kind": None, "ref": None}),
        (None, {"kind": None, "ref": None}),
    ])
    def test_linked_work_item(self, target, expected):
        assert usage._linked(target) == expected


# The runner side (#647): clone_and_exec.dispatch_runner hands the runner's exit
# code to session-end and runs the checkpoint timer beside the runner.

CLI_PY = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"

#: A runner that waits until *path* exists (bounded), then exits with *rc*, so a
#: test waits on the event it asserts rather than a fixed sleep.
WAIT_FOR_FILE = (
    "import os, sys, time\n"
    "path, deadline, rc = sys.argv[1], time.monotonic() + float(sys.argv[2]), int(sys.argv[3])\n"
    "while not os.path.exists(path) and time.monotonic() < deadline:\n"
    "    time.sleep(0.05)\n"
    "raise SystemExit(rc)\n"
)


def _wait_for_runner(path: Path, rc: int = 0) -> list:
    return [sys.executable, "-c", WAIT_FOR_FILE, str(path), str(SETTLE), str(rc)]


def _sleep_runner(seconds: float) -> list:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _checkpoint_threads() -> list:
    return [t for t in threading.enumerate()
            if t.name == clone_and_exec.USAGE_CHECKPOINT_THREAD_NAME and t.is_alive()]


@pytest.fixture
def runner_env(monkeypatch, tmp_path):
    """dispatch_runner with a 1 s timer, a fake `work` that logs its argv, no
    gate, and no session-end, signal-handler or session-id side effects."""
    strip_lmer_env(monkeypatch)
    monkeypatch.setenv("LMER_USAGE_CHECKPOINT_SECONDS", "1")
    log = tmp_path / "work-calls.log"
    work = tmp_path / "work"
    work.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    work.chmod(0o755)
    monkeypatch.setattr(clone_and_exec.shutil, "which", lambda name: str(work) if name == "work" else None)
    monkeypatch.setattr(gate_lock, "active_gate", lambda *a, **k: None)
    monkeypatch.setattr(clone_and_exec, "mint_session_id", lambda *a, **k: None)
    monkeypatch.setattr(clone_and_exec, "_forward_signals", lambda proc: None)
    ended = []
    monkeypatch.setattr(clone_and_exec, "run_state_session_end", lambda *a, **k: ended.append(a))
    return {"log": log, "work": work, "ended": ended}


class TestRunnerExitCode:
    def test_exit_code_is_passed_to_session_end(self, monkeypatch):
        calls = []
        monkeypatch.setattr(clone_and_exec.shutil, "which", lambda name: "/usr/bin/work")
        monkeypatch.setattr(clone_and_exec.subprocess, "call", lambda cmd, **kw: calls.append(cmd) or 0)
        clone_and_exec.run_state_session_end(3)
        assert calls == [["/usr/bin/work", "session-end", "--exit-code", "3"]]

    def test_no_exit_code_keeps_the_plain_session_end(self, monkeypatch):
        calls = []
        monkeypatch.setattr(clone_and_exec.shutil, "which", lambda name: "/usr/bin/work")
        monkeypatch.setattr(clone_and_exec.subprocess, "call", lambda cmd, **kw: calls.append(cmd) or 0)
        clone_and_exec.run_state_session_end()
        assert calls == [["/usr/bin/work", "session-end"]]

    def test_dispatch_runner_passes_the_runners_real_exit_code(self, runner_env, monkeypatch):
        monkeypatch.setenv("LMER_USAGE", "0")
        assert clone_and_exec.dispatch_runner([sys.executable, "-c", "raise SystemExit(5)"]) == 5
        assert runner_env["ended"] == [(5,)]


@pytest.mark.xdist_group(XDIST_GROUP)
class TestCheckpointTimer:
    def test_timer_runs_the_checkpoint_while_the_runner_runs(self, runner_env):
        log = runner_env["log"]
        assert clone_and_exec.dispatch_runner(_wait_for_runner(log)) == 0
        assert log.is_file(), "no checkpoint ran within the runner's budget"
        assert log.read_text().splitlines()[0] == "usage --checkpoint"

    def test_timer_skips_a_tick_while_a_gate_runs(self, runner_env, monkeypatch, tmp_path):
        consulted = tmp_path / "gate-consulted"

        def live_gate(*a, **k):
            consulted.touch()
            return {"pid": 1, "label": "gate-check"}

        monkeypatch.setattr(gate_lock, "active_gate", live_gate)
        assert clone_and_exec.dispatch_runner(_wait_for_runner(consulted)) == 0
        assert consulted.is_file(), "the timer never ticked"
        assert not runner_env["log"].exists()

    @pytest.mark.parametrize("opt_out", ["usage_off", "no_work_cli"])
    def test_timer_does_not_start_when_opted_out(self, opt_out, runner_env, monkeypatch):
        consulted = []
        monkeypatch.setattr(gate_lock, "active_gate", lambda *a, **k: consulted.append(1))
        if opt_out == "usage_off":
            monkeypatch.setenv("LMER_USAGE", "0")
        else:
            monkeypatch.setattr(clone_and_exec.shutil, "which", lambda name: None)
        assert clone_and_exec.dispatch_runner(_sleep_runner(2.5)) == 0
        assert consulted == []
        assert not runner_env["log"].exists()

    def test_timer_thread_is_stopped_when_dispatch_runner_returns(self, runner_env):
        log = runner_env["log"]
        assert clone_and_exec.dispatch_runner(_wait_for_runner(log)) == 0
        assert log.is_file(), "the timer never ticked"
        # A dead thread cannot tick again.
        assert _checkpoint_threads() == []

    def test_timer_stop_is_prompt_not_an_interval(self, runner_env, monkeypatch):
        monkeypatch.setenv("LMER_USAGE_CHECKPOINT_SECONDS", "3600")
        stop = clone_and_exec._start_usage_checkpoints()
        assert len(_checkpoint_threads()) == 1
        stop()
        assert _checkpoint_threads() == []

    def test_timer_failure_does_not_change_the_runners_exit_code(
        self, runner_env, monkeypatch, tmp_path, capsys,
    ):
        raised = tmp_path / "raised"

        def broken(*a, **k):
            raised.touch()
            raise RuntimeError("checkpoint exploded")

        monkeypatch.setattr(clone_and_exec, "_usage_checkpoint", broken)
        assert clone_and_exec.dispatch_runner(_wait_for_runner(raised, rc=5)) == 5
        assert raised.is_file(), "the timer never ticked"
        assert runner_env["ended"] == [(5,)]
        assert "⚠️  usage checkpoint failed (continuing): checkpoint exploded" in capsys.readouterr().err

    @pytest.mark.parametrize("raw", ["", "soon", "0", "-5"])
    def test_timer_interval_falls_back_to_the_default(self, raw, monkeypatch):
        monkeypatch.setenv("LMER_USAGE_CHECKPOINT_SECONDS", raw)
        assert clone_and_exec._usage_checkpoint_interval() == usage.DEFAULT_CHECKPOINT_SECONDS

    def test_timer_constants_mirror_work_repo_usage(self):
        assert clone_and_exec.USAGE_ENABLED_ENV == usage.ENABLED_ENV
        assert clone_and_exec.USAGE_CHECKPOINT_ENV == usage.CHECKPOINT_ENV
        assert clone_and_exec.USAGE_DEFAULT_CHECKPOINT_SECONDS == usage.DEFAULT_CHECKPOINT_SECONDS


@pytest.mark.parametrize("var", ["LMER_USAGE", "LMER_USAGE_CHECKPOINT_SECONDS", "LMER_USAGE_IDLE_SECONDS"])
def test_usage_settings_passthrough_into_the_container(var):
    """Read in the container by the timer and the work CLI, so a host-side
    value must cross the boundary (cli.py's env dict)."""
    pattern = rf"""["']{var}["']\s*:\s*os\.environ\.get\(\s*["']{var}["']\s*\)"""
    assert re.search(pattern, CLI_PY.read_text()), f"{var} entry missing from cli.py env dict"


class TestCheckpointRace:
    def test_a_final_write_during_a_checkpoint_wins(self, monkeypatch, tmp_path, transcripts, no_gate):
        _legacy(monkeypatch, tmp_path)
        real = usage.summarise

        def summarise_then_finalise(files):
            result = real(files)
            usage.finalise(0)  # the runner exited while this checkpoint was summarising
            return result

        assert usage.checkpoint(summarise_fn=summarise_then_finalise) is None
        data = _read(tmp_path / "work" / "git.example.com" / "org" / "repo" / "runs" / "develop-issue-123"
                     / "ai-usage" / f"{LEGACY_SESSION}.yaml")
        assert (data["final"], data["status"]) == (True, "ended")

    def test_the_lock_never_lands_in_the_run_dir(self, monkeypatch, tmp_path, transcripts, no_gate):
        rdir = _legacy(monkeypatch, tmp_path)
        usage.checkpoint()
        assert sorted(p.name for p in (rdir / "ai-usage").iterdir()) == [f"{LEGACY_SESSION}.yaml"]
