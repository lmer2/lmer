"""Redaction at the work-CLI write boundaries (issue #124 B1/B2/B3/B5/B6).

`work log` and `state set --question` already redact agent-typed free text
before it lands in the shared work repo; these cover the writers that did
not — goal/seed goals, event notes and payloads, critical-error objects,
free-form phases, and the commit subject that becomes git history.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from work_repo import cli as work_cli
from work_repo import run_state
from tests.conftest import strip_lmer_env

# A value `redact_secrets` catches on its prefix alone, so these tests never
# depend on the environment carrying a matching variable.
SAMPLE = "glpat-" + "A" * 20

REDACTED = "***REDACTED***"

# A credential shaped like an ordinary name: all lowercase, no punctuation, so
# kebab-case normalization leaves it whole and only redaction can remove it.
# Assembled here so no scanner reads this file as carrying a real value.
LOWERCASE_VALUE = "correct" + "horsebattery"
VALUE_ENV = "PROBE_DB_PASSWORD"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture(autouse=True)
def _tmp_goal_dir(monkeypatch, tmp_path):
    """Keep the `work goal` fallback file out of the real /tmp — a suite run
    must never write where a live session's goal lives (issue #277)."""
    monkeypatch.setattr(work_cli, "GOAL_FILE_DIR", str(tmp_path / "goals"))
    (tmp_path / "goals").mkdir(exist_ok=True)


@pytest.fixture
def run_env(monkeypatch, tmp_path):
    """Full env trio + task context; returns the session's run dir."""
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
    monkeypatch.setenv("LMER_TASK", "develop")
    monkeypatch.setenv("LMER_TASK_TARGET", "https://git.example.com/org/repo/-/issues/123")
    monkeypatch.setenv("LMER_SESSION_ID", "s-redact-1")
    return tmp_path / "git.example.com" / "org/repo" / "runs" / "develop-issue-123"


def _main(argv):
    with patch.object(work_cli.sys, "argv", ["work"] + argv):
        return work_cli.main()


def _events(rdir: Path) -> list[dict]:
    return run_state.read_events(rdir, last_n=0)


def _committed_text(rdir: Path) -> str:
    """Everything the run dir would push: state plus the raw event log."""
    text = (rdir / run_state.STATE_FILE).read_text(encoding="utf-8")
    events = rdir / run_state.EVENTS_FILE
    if events.is_file():
        text += events.read_text(encoding="utf-8")
    return text


class TestGoalRedaction:
    """B1a — `work goal` writes state.goal, a goal_set note, and an echo."""

    def test_goal_redacted_in_state_event_and_echo(self, run_env, capsys):
        assert _main(["goal", f"rotate {SAMPLE} out of the runner"]) == 0
        state = run_state.load_state(run_env)
        assert REDACTED in state["goal"]
        notes = [e["note"] for e in _events(run_env) if e["type"] == "goal_set"]
        assert notes and all(REDACTED in note for note in notes)
        assert SAMPLE not in _committed_text(run_env)
        assert SAMPLE not in capsys.readouterr().out

    def test_estimate_fields_are_structured_and_untouched(self, run_env):
        assert _main([
            "goal", f"rotate {SAMPLE}",
            "--estimate-sessions", "2", "--estimate-time", "4h",
        ]) == 0
        state = run_state.load_state(run_env)
        assert state["estimate"] == {"sessions": 2, "time": "4h"}


class TestSeedGoalRedaction:
    """B1b — `work seed --goal` seeds a run for another slug."""

    def test_seed_goal_redacted_in_state_and_event(self, run_env, tmp_path):
        assert _main([
            "seed", "develop", "gate-receipts",
            "--goal", f"reuse {SAMPLE} for the runner",
        ]) == 0
        rdir = tmp_path / "git.example.com" / "org/repo" / "runs" / "develop-gate-receipts"
        state = run_state.load_state(rdir)
        assert REDACTED in state["goal"]
        notes = [e["note"] for e in _events(rdir) if e["type"] == "goal_set"]
        assert notes == [f"reuse {REDACTED} for the runner"]
        assert SAMPLE not in _committed_text(rdir)


class TestRunNameRedaction:
    """`work name` / `work seed --name` write state.name, a run_named note,
    and — at the freeze — the run *directory* name in the shared repo."""

    def test_name_redacted_in_state_and_event(self, run_env, monkeypatch):
        monkeypatch.setenv(VALUE_ENV, LOWERCASE_VALUE)
        assert _main(["name", f"fix-{LOWERCASE_VALUE}-leak"]) == 0
        state = run_state.load_state(run_env)
        assert state["name"] == "fix-redacted-leak"
        notes = [e["note"] for e in _events(run_env) if e["type"] == "run_named"]
        assert notes == ["fix-redacted-leak"]
        assert LOWERCASE_VALUE not in _committed_text(run_env)

    def test_freeze_rename_gives_a_clean_directory_name(self, run_env, monkeypatch):
        monkeypatch.setenv(VALUE_ENV, LOWERCASE_VALUE)
        assert _main(["name", f"fix-{LOWERCASE_VALUE}"]) == 0
        state = run_state.load_state(run_env)
        renamed, previous = run_state.freeze_run_dir(run_env, state)
        assert renamed.name == "develop-issue-123--fix-redacted"
        assert previous == "develop-issue-123"

    def test_seed_name_redacted_in_state_and_event(self, run_env, tmp_path, monkeypatch):
        monkeypatch.setenv(VALUE_ENV, LOWERCASE_VALUE)
        assert _main([
            "seed", "develop", "gate-receipts", "--name", f"{LOWERCASE_VALUE}-run",
        ]) == 0
        rdir = tmp_path / "git.example.com" / "org/repo" / "runs" / "develop-gate-receipts"
        state = run_state.load_state(rdir)
        assert state["name"] == "redacted-run"
        notes = [e["note"] for e in _events(rdir) if e["type"] == "run_named"]
        assert notes == ["redacted-run"]
        assert LOWERCASE_VALUE not in _committed_text(rdir)

    def test_echoes_do_not_print_the_value(self, run_env, monkeypatch, capsys):
        monkeypatch.setenv(VALUE_ENV, LOWERCASE_VALUE)
        # Both the "Normalized to:" note and the confirmation echo the name.
        assert _main(["name", f"fix {LOWERCASE_VALUE}"]) == 0
        out = capsys.readouterr()
        assert "Run named: fix-redacted" in out.out
        assert LOWERCASE_VALUE not in out.out + out.err


class TestEventRedaction:
    """B2 — `work event` note and --data payload."""

    def test_note_redacted(self, run_env):
        assert _main(["event", "review_posted", "--note", f"used {SAMPLE}"]) == 0
        events = [e for e in _events(run_env) if e["type"] == "review_posted"]
        assert events[0]["note"] == f"used {REDACTED}"
        assert SAMPLE not in _committed_text(run_env)

    def test_data_redacted_at_depth_with_keys_redacted_and_scalars_intact(self, run_env):
        payload = {
            "outer": {
                "argv": ["curl", f"--header=auth: {SAMPLE}"],
                "attempts": 3,
                "clean": True,
                "note": None,
            },
            "top": f"also {SAMPLE}",
            SAMPLE: 1,
        }
        assert _main(["event", "probe", "--data", json.dumps(payload)]) == 0
        data = [e for e in _events(run_env) if e["type"] == "probe"][0]["data"]
        # Keys are agent-typed too — a secret used as a key is redacted, the
        # rest of the structure is untouched.
        assert set(data) == {"outer", "top", REDACTED}
        assert set(data["outer"]) == {"argv", "attempts", "clean", "note"}
        assert data[REDACTED] == 1
        assert data["outer"]["argv"] == ["curl", f"--header=auth: {REDACTED}"]
        assert data["top"] == f"also {REDACTED}"
        # Non-strings carry nothing to redact and keep their types.
        assert data["outer"]["attempts"] == 3
        assert data["outer"]["clean"] is True
        assert data["outer"]["note"] is None
        assert SAMPLE not in _committed_text(run_env)

    def test_data_list_at_top_level_is_redacted(self, run_env):
        assert _main(["event", "probe", "--data", json.dumps([f"a {SAMPLE}", 7])]) == 0
        data = [e for e in _events(run_env) if e["type"] == "probe"][0]["data"]
        assert data == [f"a {REDACTED}", 7]


class TestStateSetRedaction:
    """B3/B6 — the critical-error object and the free-form phase."""

    def test_critical_error_object_redacted(self, run_env):
        payload = {"summary": "gate failed", "detail": f"ran curl -H 'auth: {SAMPLE}'"}
        assert _main([
            "state", "set", "--stop-reason=critical_error",
            "--critical-error", json.dumps(payload),
        ]) == 0
        state = run_state.load_state(run_env)
        assert state["critical_error"]["summary"] == "gate failed"
        assert state["critical_error"]["detail"] == f"ran curl -H 'auth: {REDACTED}'"
        assert SAMPLE not in _committed_text(run_env)

    def test_phase_redacted_in_state_event_and_push_detail(self, run_env):
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            assert _main(["state", "set", f"--phase=execute-{SAMPLE}"]) == 0
        state = run_state.load_state(run_env)
        assert state["phase"] == f"execute-{REDACTED}"
        notes = [e["note"] for e in _events(run_env) if e["type"] == "phase"]
        assert notes == [f"execute-{REDACTED}"]
        # The durability push turns the phase into a commit subject too.
        assert SAMPLE not in push.call_args[0][1]
        assert SAMPLE not in _committed_text(run_env)


class TestCommitMessageRedaction:
    """B5 — `work commit -m` becomes a commit subject in the shared repo."""

    @pytest.fixture
    def work_repo(self, monkeypatch, tmp_path):
        """A real work repo with something staged for the run's task dir."""
        repo = tmp_path / "work"
        repo.mkdir()
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "test@example.com"],
            ["git", "config", "user.name", "Test User"],
        ):
            subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
        task_dir = repo / "git.example.com" / "org/repo" / "develop" / "issue-123"
        task_dir.mkdir(parents=True)
        (task_dir / "log.yaml").write_text("- message: test\n", encoding="utf-8")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(repo))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASK_TARGET", "https://git.example.com/org/repo/-/issues/123")
        monkeypatch.setenv("LMER_SESSION_ID", "s-redact-2")
        return repo

    def test_git_history_carries_the_redacted_subject(self, work_repo):
        # No remote here: stub the push leg so only the local commit runs.
        with patch("work_repo.git_ops._push_with_rebase_retries", return_value=0):
            assert _main(["commit", "-m", f"wip: exported {SAMPLE}"]) == 0
        log = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=work_repo, check=True, capture_output=True, text=True,
        ).stdout
        assert REDACTED in log
        assert SAMPLE not in log

    def test_napkin_commits_get_the_same_redacted_message(self, run_env):
        with patch("work_repo.cli._sync_masterplan_links", return_value=[]), \
             patch("work_repo.cli.commit_work_changes", return_value=0) as commit, \
             patch("work_repo.cli.commit_napkin_if_subdir", return_value=0) as napkin, \
             patch("work_repo.cli.push_napkin_if_separate", return_value=0) as push, \
             patch("work_repo.cli.report_uncommitted_work_items", return_value=0):
            assert work_cli.cmd_commit(f"wip: exported {SAMPLE}") == 0
        expected = f"wip: exported {REDACTED}"
        commit.assert_called_once_with(expected)
        napkin.assert_called_once_with(expected)
        push.assert_called_once_with(expected)

    def test_no_message_still_commits(self, run_env):
        with patch("work_repo.cli._sync_masterplan_links", return_value=[]), \
             patch("work_repo.cli.commit_work_changes", return_value=0) as commit, \
             patch("work_repo.cli.commit_napkin_if_subdir", return_value=0), \
             patch("work_repo.cli.push_napkin_if_separate", return_value=0), \
             patch("work_repo.cli.report_uncommitted_work_items", return_value=0):
            assert work_cli.cmd_commit(None) == 0
        commit.assert_called_once_with(None)


class TestRedactJsonValues:
    """The shared recursive helper behind B2 and B3."""

    def test_leaves_structure_and_scalars_alone(self):
        obj = {"k": [1, 2.5, True, None, {"deep": f"x {SAMPLE}"}]}
        out = work_cli._redact_json_values(obj)
        assert out == {"k": [1, 2.5, True, None, {"deep": f"x {REDACTED}"}]}
        assert isinstance(out["k"][0], int) and isinstance(out["k"][1], float)

    def test_string_keys_are_redacted_at_depth(self):
        obj = {"outer": {f"header {SAMPLE}": "v"}}
        assert work_cli._redact_json_values(obj) == {
            "outer": {f"header {REDACTED}": "v"}
        }

    def test_non_string_keys_survive(self):
        # json.loads never makes these, but in-process callers can.
        assert work_cli._redact_json_values({7: "a", None: "b"}) == {7: "a", None: "b"}

    def test_none_payload_stays_none(self):
        assert work_cli._redact_json_values(None) is None
