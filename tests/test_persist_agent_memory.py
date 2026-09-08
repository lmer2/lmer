"""Tests for LMER_PERSIST_AGENT_MEMORY (agent memory persistence to work repo).

Covers three layers:
1. Python (cli.py): the env var is declared in the host→container env dict.
   Verified by a source-level sanity check (the env dict is built inline in
   main(), so we guard against accidental removal).
2. Python (work_repo.memory): restore_memory / persist_memory copy behavior,
   the LMER_PERSIST_AGENT_MEMORY enable gate, and the memory-dir override.
3. Bash (claude-runner.sh): when the var is enabled, the runner invokes
   `work memory restore` before launching claude; when disabled it does not.
"""

import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from work_repo import memory
from tests._claude_runner_harness import run_claude_runner, skip_if_npm_claude_present


CLI_PY = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"


@pytest.fixture(autouse=True)
def memory_git_preflight(monkeypatch):
    """Copy/merge tests stub Git; push_safety exercises the real preflight."""
    monkeypatch.setattr(memory, "check_commit_ready", lambda *args: 0)


# ── Layer 1: cli.py env-dict guard ──────────────────────────────────────────


def test_cli_env_dict_declares_persist_agent_memory():
    """Guard against accidental removal of LMER_PERSIST_AGENT_MEMORY from cli.py.

    The container-env dict in main() is built inline; a source-level check
    catches drift (removal from the host→container passthrough) without
    re-testing the surrounding logic.
    """
    source = CLI_PY.read_text()
    pattern = re.compile(
        r"""["']LMER_PERSIST_AGENT_MEMORY["']\s*:\s*"""
        r"""os\.environ\.get\(\s*["']LMER_PERSIST_AGENT_MEMORY["']\s*\)"""
    )
    assert pattern.search(source), (
        "LMER_PERSIST_AGENT_MEMORY entry missing from cli.py env dict"
    )


# ── Layer 2: work_repo.memory ───────────────────────────────────────────────


@pytest.fixture
def memory_env(monkeypatch, tmp_path):
    """Configure a work repo + agent memory dir via env vars and return paths."""
    work_repo = tmp_path / "work"
    agent_dir = tmp_path / "agent-memory"
    work_repo.mkdir()
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_repo))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "group/proj")
    monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", str(agent_dir))
    monkeypatch.delenv("LMER_PERSIST_AGENT_MEMORY", raising=False)
    project_memory = work_repo / "git.example.com" / "group" / "proj" / "memory"
    return {
        "work_repo": work_repo,
        "agent_dir": agent_dir,
        "project_memory": project_memory,
    }


def test_agent_memory_dir_honors_override(monkeypatch):
    monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", "/custom/mem")
    assert memory.agent_memory_dir() == Path("/custom/mem")


def test_agent_memory_dir_default_from_home(monkeypatch):
    monkeypatch.delenv("LMER_AGENT_MEMORY_DIR", raising=False)
    monkeypatch.setenv("HOME", "/home/someone")
    assert memory.agent_memory_dir() == Path(
        "/home/someone/.claude/projects/-workspace/memory"
    )


def test_restore_noop_when_disabled(memory_env):
    """restore_memory does nothing (and copies nothing) when the var is off."""
    (memory_env["project_memory"]).mkdir(parents=True)
    (memory_env["project_memory"] / "fact.md").write_text("remembered\n")

    assert memory.restore_memory() == 0
    assert not memory_env["agent_dir"].exists()


def test_restore_copies_memory_when_enabled(memory_env, monkeypatch):
    monkeypatch.setenv("LMER_PERSIST_AGENT_MEMORY", "1")
    pm = memory_env["project_memory"]
    pm.mkdir(parents=True)
    (pm / "MEMORY.md").write_text("- [fact](fact.md)\n")
    (pm / "fact.md").write_text("a durable fact\n")

    assert memory.restore_memory() == 0
    restored = memory_env["agent_dir"]
    assert (restored / "MEMORY.md").read_text() == "- [fact](fact.md)\n"
    assert (restored / "fact.md").read_text() == "a durable fact\n"


def test_restore_fresh_start_when_no_saved_memory(memory_env, monkeypatch, capsys):
    monkeypatch.setenv("LMER_PERSIST_AGENT_MEMORY", "1")
    assert memory.restore_memory() == 0
    assert "starting fresh" in capsys.readouterr().out
    # The store dir is still created (empty): session context promises the
    # agent an existing path — a missing one invites an improvised memory/
    # dir inside the project workspace.
    assert memory_env["agent_dir"].is_dir()
    assert not any(memory_env["agent_dir"].iterdir())


def test_restore_creates_dir_even_without_repo_identity(memory_env, monkeypatch, capsys):
    # The "already exists" promise is gated solely on the persistence var,
    # so the guarantee must hold on the missing-env no-op branch too.
    monkeypatch.setenv("LMER_PERSIST_AGENT_MEMORY", "1")
    monkeypatch.delenv("LMER_REPO_HOST", raising=False)
    monkeypatch.delenv("LMER_REPO_PROJECT", raising=False)
    assert memory.restore_memory() == 0
    assert "LMER_REPO_HOST/LMER_REPO_PROJECT not set" in capsys.readouterr().err
    assert memory_env["agent_dir"].is_dir()


def test_restore_mirrors_deletions(memory_env, monkeypatch):
    """Restore removes agent-dir files that are no longer in the work repo."""
    monkeypatch.setenv("LMER_PERSIST_AGENT_MEMORY", "1")
    # Work repo only has one fact.
    pm = memory_env["project_memory"]
    pm.mkdir(parents=True)
    (pm / "keep.md").write_text("keep me\n")
    # Agent dir has a stale fact that was deleted upstream.
    agent = memory_env["agent_dir"]
    agent.mkdir(parents=True)
    (agent / "keep.md").write_text("old\n")
    (agent / "stale.md").write_text("should be removed\n")

    assert memory.restore_memory() == 0
    assert (agent / "keep.md").read_text() == "keep me\n"
    assert not (agent / "stale.md").exists()


def test_persist_propagates_a_memory_this_session_deleted(memory_env, monkeypatch):
    """Persist removes work-repo files that the agent has deleted.

    The honest delete: this session RESTORED the file, so the restore
    manifest carries it, and its absence from the memory dir is a decision
    rather than a session that never saw it (issue #369).
    """
    monkeypatch.setenv("LMER_PERSIST_AGENT_MEMORY", "1")
    # Work repo already has two facts from a previous session.
    pm = memory_env["project_memory"]
    pm.mkdir(parents=True)
    (pm / "keep.md").write_text("old\n")
    (pm / "wrong.md").write_text("a memory that turned out wrong\n")

    assert memory.restore_memory() == 0

    # Agent deleted the wrong one this session.
    agent = memory_env["agent_dir"]
    (agent / "wrong.md").unlink()
    (agent / "keep.md").write_text("updated\n")

    with patch("work_repo.memory.commit_work_path", return_value=0):
        assert memory.persist_memory() == 0

    assert (pm / "keep.md").read_text() == "updated\n"
    assert not (pm / "wrong.md").exists()


def test_persist_without_a_restore_deletes_nothing(memory_env, monkeypatch):
    """No restore manifest degrades to "delete nothing" (issue #369).

    An older container, a restore that never ran, a hand-made memory dir: a
    session with no record of what it restored cannot tell "the agent deleted
    this" from "the agent never saw it", so it deletes neither.
    """
    monkeypatch.setenv("LMER_PERSIST_AGENT_MEMORY", "1")
    pm = memory_env["project_memory"]
    pm.mkdir(parents=True)
    (pm / "someone-elses.md").write_text("another session's memory\n")
    agent = memory_env["agent_dir"]
    agent.mkdir(parents=True)
    (agent / "mine.md").write_text("this session's memory\n")

    with patch("work_repo.memory.commit_work_path", return_value=0):
        assert memory.persist_memory() == 0

    assert (pm / "someone-elses.md").exists()
    assert (pm / "mine.md").exists()


def test_two_sessions_do_not_delete_each_others_memory(tmp_path, monkeypatch):
    """Issue #369, on the legacy one-scope tree: A persists, then B — which
    holds an older view and never saw A's file — persists its own work. Both
    memories survive, and both index lines with them.

    Each session gets its own memory dir (and so its own restore manifest),
    which is what a container is.
    """
    work_repo = tmp_path / "work"
    work_repo.mkdir()
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work_repo))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "group/proj")
    monkeypatch.setenv("LMER_PERSIST_AGENT_MEMORY", "1")
    pm = work_repo / "git.example.com" / "group" / "proj" / "memory"
    pm.mkdir(parents=True)
    (pm / "shared.md").write_text("a fact both sessions restored\n")
    (pm / "MEMORY.md").write_text("- [Shared](shared.md) — a fact.\n")

    def session(name):
        agent_dir = tmp_path / name / "memory"
        agent_dir.mkdir(parents=True)
        monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", str(agent_dir))
        return agent_dir

    def persist():
        with patch("work_repo.memory.commit_work_path", return_value=0):
            return memory.persist_memory()

    session_b = session("session-b")
    assert memory.restore_memory() == 0

    session_a = session("session-a")
    assert memory.restore_memory() == 0
    (session_a / "a.md").write_text("A's memory\n")
    (session_a / "MEMORY.md").write_text(
        (session_a / "MEMORY.md").read_text() + "- [A](a.md) — A's fact.\n"
    )
    assert persist() == 0

    monkeypatch.setenv("LMER_AGENT_MEMORY_DIR", str(session_b))
    (session_b / "b.md").write_text("B's memory\n")
    (session_b / "MEMORY.md").write_text(
        (session_b / "MEMORY.md").read_text() + "- [B](b.md) — B's fact.\n"
    )
    assert persist() == 0

    assert (pm / "a.md").read_text() == "A's memory\n"
    assert (pm / "b.md").read_text() == "B's memory\n"
    assert (pm / "shared.md").exists()
    index = (pm / "MEMORY.md").read_text()
    assert "(a.md)" in index and "(b.md)" in index and "(shared.md)" in index


def test_persist_noop_when_disabled(memory_env):
    """persist_memory does nothing when the var is off, even with memory present."""
    memory_env["agent_dir"].mkdir(parents=True)
    (memory_env["agent_dir"] / "fact.md").write_text("x\n")

    assert memory.persist_memory() == 0
    assert not memory_env["project_memory"].exists()


def test_persist_no_memory_to_save(memory_env, monkeypatch):
    monkeypatch.setenv("LMER_PERSIST_AGENT_MEMORY", "1")
    # No agent memory dir / files at all.
    with patch("work_repo.memory.commit_work_path", return_value=0) as commit:
        assert memory.persist_memory() == 0
    assert commit.call_args.args[0] == []


def test_persist_copies_and_commits(memory_env, monkeypatch):
    monkeypatch.setenv("LMER_PERSIST_AGENT_MEMORY", "1")
    agent = memory_env["agent_dir"]
    agent.mkdir(parents=True)
    (agent / "MEMORY.md").write_text("- [fact](fact.md)\n")
    (agent / "fact.md").write_text("a durable fact\n")

    with patch("work_repo.memory.commit_work_path", return_value=0) as mock_commit:
        assert memory.persist_memory() == 0

    # Files were copied into the per-project work-repo memory dir.
    pm = memory_env["project_memory"]
    assert (pm / "MEMORY.md").read_text() == "- [fact](fact.md)\n"
    assert (pm / "fact.md").read_text() == "a durable fact\n"

    # And the correct work-repo path was committed/pushed.
    mock_commit.assert_called_once()
    rel_path = mock_commit.call_args[0][0]
    assert rel_path == "git.example.com/group/proj/memory"


def test_persist_custom_message(memory_env, monkeypatch):
    monkeypatch.setenv("LMER_PERSIST_AGENT_MEMORY", "1")
    agent = memory_env["agent_dir"]
    agent.mkdir(parents=True)
    (agent / "fact.md").write_text("x\n")

    with patch("work_repo.memory.commit_work_path", return_value=0) as mock_commit:
        memory.persist_memory("custom msg")

    assert mock_commit.call_args[0][1] == "custom msg"


def test_persist_missing_env_returns_error(memory_env, monkeypatch):
    monkeypatch.setenv("LMER_PERSIST_AGENT_MEMORY", "1")
    monkeypatch.delenv("LMER_REPO_HOST", raising=False)
    monkeypatch.delenv("LMER_REPO_PROJECT", raising=False)
    memory_env["agent_dir"].mkdir(parents=True)
    (memory_env["agent_dir"] / "fact.md").write_text("x\n")

    assert memory.persist_memory() == 1


# ── Layer 3: claude-runner.sh restore invocation ────────────────────────────


def _run_claude_runner_with_work(tmp_path, persist_value=None):
    env = {} if persist_value is None else {"LMER_PERSIST_AGENT_MEMORY": persist_value}
    result = run_claude_runner(tmp_path, env, stub_work=True)
    return result.output, result.work_calls


@skip_if_npm_claude_present
class TestClaudeRunnerMemoryRestore:
    """Verify claude-runner.sh restores agent memory when the var is enabled."""

    @pytest.mark.parametrize("value", ["1", "true", "yes"])
    def test_restore_invoked_when_enabled(self, tmp_path, value):
        _output, calls = _run_claude_runner_with_work(tmp_path, persist_value=value)
        assert "memory restore" in calls

    def test_restore_not_invoked_when_disabled(self, tmp_path):
        _output, calls = _run_claude_runner_with_work(tmp_path, persist_value="0")
        assert "memory restore" not in calls

    def test_restore_not_invoked_when_unset(self, tmp_path):
        _output, calls = _run_claude_runner_with_work(tmp_path, persist_value=None)
        assert "memory restore" not in calls
