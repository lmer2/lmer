"""Tests for `work plan check` (issue #90 — checkable plan gates).

The pure lint rules are covered in tests/test_work_repo_plan_index.py;
these tests cover the CLI/IO shell: run-context handling, file discovery,
report rendering, exit codes, and the read-only contract.
"""
import json
from unittest.mock import patch

import pytest

from work_repo import cli as work_cli
from work_repo import run_state
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def run_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
    monkeypatch.setenv("LMER_TASK", "develop")
    monkeypatch.setenv("LMER_TASK_TARGET", "https://git.example.com/org/repo/-/issues/123")
    monkeypatch.setenv("LMER_SESSION_ID", "s-plan-1")
    rdir = tmp_path / "git.example.com" / "org/repo" / "runs" / "develop-issue-123"
    rdir.mkdir(parents=True)
    run_state.write_state(rdir, run_state.seed_state("develop-issue-123", "develop", "t"))
    return rdir


def _main(argv):
    with patch.object(work_cli.sys, "argv", ["work"] + argv):
        return work_cli.main()


def _write_index(rdir, index):
    (rdir / "plan.index.json").write_text(json.dumps(index), encoding="utf-8")


def _task(task_id, **overrides):
    base = {
        "id": task_id,
        "description": f"task {task_id}",
        "files": [],
        "deps": [],
        "verify_commands": ["gate-check"],
        "session_scope": "one",
    }
    base.update(overrides)
    return base


class TestPlanCheckCleanExits:
    def test_no_run_context_exits_zero(self, capsys):
        assert _main(["plan", "check"]) == 0
        assert "no run context" in capsys.readouterr().out.lower()

    def test_no_plan_index_exits_zero(self, run_env, capsys):
        assert _main(["plan", "check"]) == 0
        assert "No plan index" in capsys.readouterr().out

    def test_bare_plan_prints_help_exits_one(self, run_env, capsys):
        assert _main(["plan"]) == 1


class TestPlanCheckVerdicts:
    def test_green_index_exits_zero(self, run_env, capsys):
        _write_index(run_env, {"schema": 1, "tasks": [
            _task("T1", files=["src/a.py"]),
            _task("T2", files=["src/b.py"], deps=["T1"]),
        ]})
        assert _main(["plan", "check"]) == 0
        out = capsys.readouterr().out
        assert "2 task(s)" in out
        assert "✅ plan check green" in out

    def test_errors_exit_one_and_are_reported(self, run_env, capsys):
        _write_index(run_env, {"schema": 1, "tasks": [
            _task("T1", files=["src/a.py"]),
            _task("T2", files=["src/a.py"]),
        ]})
        assert _main(["plan", "check"]) == 1
        out = capsys.readouterr().out
        assert "❌" in out and "share write-scope" in out
        assert "plan check failed: 1 error(s)" in out

    def test_warnings_alone_stay_green(self, run_env, capsys):
        _write_index(run_env, {"schema": 1, "tasks": [
            _task("T1", verify_commands=[]),
        ]})
        assert _main(["plan", "check"]) == 0
        out = capsys.readouterr().out
        assert "⚠️" in out and "non-blocking" in out

    def test_invalid_json_exits_one(self, run_env, capsys):
        (run_env / "plan.index.json").write_text("{nope", encoding="utf-8")
        assert _main(["plan", "check"]) == 1
        assert "not valid JSON" in capsys.readouterr().out


class TestPlanCheckSiblings:
    def test_plan_md_drift_warns(self, run_env, capsys):
        _write_index(run_env, {"schema": 1, "tasks": [_task("T1")]})
        (run_env / "plan.md").write_text("- [ ] a\n- [ ] b\n", encoding="utf-8")
        assert _main(["plan", "check"]) == 0
        assert "may have drifted" in capsys.readouterr().out

    def test_goals_md_refs_warn(self, run_env, capsys):
        _write_index(run_env, {"schema": 1, "tasks": [_task("T1", goals=["G9"])]})
        (run_env / "goals.md").write_text("## G1: ship\n", encoding="utf-8")
        assert _main(["plan", "check"]) == 0
        assert "G9" in capsys.readouterr().out

    def test_no_goals_md_skips_goal_rule(self, run_env, capsys):
        _write_index(run_env, {"schema": 1, "tasks": [_task("T1", goals=["G9"])]})
        assert _main(["plan", "check"]) == 0
        assert "G9" not in capsys.readouterr().out


class TestPlanCheckReadOnly:
    def test_never_writes_events_or_pushes(self, run_env):
        _write_index(run_env, {"schema": 1, "tasks": [_task("T1")]})
        events_before = run_state.read_events(run_env, last_n=0)
        with patch("work_repo.cli.commit_work_path") as push:
            assert _main(["plan", "check"]) == 0
        push.assert_not_called()
        assert run_state.read_events(run_env, last_n=0) == events_before
