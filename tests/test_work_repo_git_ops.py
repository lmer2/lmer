#!/usr/bin/env python3
"""Tests for work_repo.git_ops module"""

import os
import pytest
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

from work_repo.git_ops import (
    UNTRACKED_REPORT_CAP,
    commit_napkin_if_subdir,
    commit_work_changes,
    commit_work_path,
    push_napkin_if_separate,
    report_uncommitted_work_items,
    run_dir_push_status,
    run_git_command,
)

# The Stop-hook guard's trigger-2 gatherer, which run_dir_push_status
# deliberately mirrors (hooks import no project code) — imported here only
# so the drift-guard tests can hold both copies to the same fixtures.
from hooks.run_state_guard import gather_run_dir_status


class TestRunGitCommand:
    """Test run_git_command function"""

    def test_run_git_command_success(self):
        """Test successful git command"""
        with tempfile.TemporaryDirectory() as tmpdir:
            rc, output = run_git_command(["--version"], Path(tmpdir), check=False)
            assert rc == 0
            assert "git version" in output.lower()

    def test_run_git_command_failure(self):
        """Test failed git command"""
        with tempfile.TemporaryDirectory() as tmpdir:
            rc, output = run_git_command(["invalid-command"], Path(tmpdir), check=False)
            assert rc != 0


class TestCommitWorkChanges:
    """Test commit_work_changes function"""

    def test_commit_work_changes_missing_env_vars(self):
        """Test commit when env vars are missing"""
        with patch.dict(os.environ, {}, clear=True):
            result = commit_work_changes()
            assert result == 1

    def test_commit_work_changes_repo_not_found(self):
        """Test commit when work repo doesn't exist"""
        env_vars = {
            "LMER_WORK_REPO_PATH": "/nonexistent/path",
            "LMER_REPO_HOST": "github.com",
            "LMER_REPO_PROJECT": "owner/repo",
            "LMER_TASK": "review",
            "LMER_TASK_TARGET": "pr-123",
        }

        with patch.dict(os.environ, env_vars):
            result = commit_work_changes()
            assert result == 1

    def test_commit_work_changes_no_changes(self):
        """Test commit when there are no changes"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Initialize git repo
            subprocess.run(["git", "init"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmpdir, check=True, capture_output=True)

            # Create directory structure
            target_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "review" / "pr-123"
            target_dir.mkdir(parents=True)

            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                result = commit_work_changes()
                # Should return 0 (no changes to commit)
                assert result == 0

    def test_commit_work_changes_with_changes(self):
        """Test commit when there are changes"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Initialize git repo
            subprocess.run(["git", "init"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmpdir, check=True, capture_output=True)

            # Create initial commit
            (Path(tmpdir) / "README.md").write_text("Initial")
            subprocess.run(["git", "add", "README.md"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmpdir, check=True, capture_output=True)

            # Create directory structure and file
            target_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "review" / "pr-123"
            target_dir.mkdir(parents=True)
            (target_dir / "log.yaml").write_text("- message: test\n")

            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                # Mock push to avoid needing remote
                with patch("work_repo.git_ops.run_git_command") as mock_git:
                    def git_side_effect(cmd, cwd, check=False):
                        if cmd == ["fetch"]:
                            return (0, "")
                        elif cmd == ["pull"]:
                            return (0, "")
                        elif cmd == ["add", "-A", "--", "github.com/owner/repo/review/pr-123"]:
                            return (0, "")
                        elif cmd == ["status", "--porcelain", "--", "github.com/owner/repo/review/pr-123"]:
                            return (0, "M  github.com/owner/repo/review/pr-123/log.yaml\n")
                        elif cmd == ["commit", "-m", "Update work repo: github.com/owner/repo/review/pr-123"]:
                            return (0, "")
                        elif cmd == ["push"]:
                            return (0, "")
                        return (0, "")

                    mock_git.side_effect = git_side_effect
                    result = commit_work_changes()
                    assert result == 0

    def test_commit_work_path_repo_not_found(self):
        """commit_work_path returns 1 when the work repo path doesn't exist."""
        with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": "/nonexistent/path"}):
            assert commit_work_path("some/rel/path") == 1

    def test_commit_work_path_stages_given_path(self):
        """commit_work_path stages exactly the path it is given, with -A."""
        rel = "git.example.com/group/proj/memory"
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / rel).mkdir(parents=True)  # only existing paths are staged
            with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": tmpdir}):
                with patch("work_repo.git_ops.run_git_command") as mock_git:
                    def git_side_effect(cmd, cwd, check=False):
                        if cmd == ["status", "--porcelain", "--", rel]:
                            return (0, "M  git.example.com/group/proj/memory/fact.md\n")
                        return (0, "")

                    mock_git.side_effect = git_side_effect
                    result = commit_work_path(rel)
                    assert result == 0
                    add_calls = [
                        call for call in mock_git.call_args_list
                        if call[0][0] == ["add", "-A", "--", rel]
                    ]
                    assert len(add_calls) == 1

    def test_commit_work_path_no_change_in_target_ignores_other_dirty_files(self):
        """Unchanged target + an unrelated dirty file → return 0, no commit.

        Regression test: the no-change check must be scoped to target_path so a
        dirty per-session log.yaml elsewhere can't trigger a spurious empty
        commit (which would fail and return non-zero).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            subprocess.run(["git", "init"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmpdir, check=True, capture_output=True)

            mem_dir = tmp / "git.example.com" / "grp" / "proj" / "memory"
            mem_dir.mkdir(parents=True)
            (mem_dir / "fact.md").write_text("stable fact\n")
            other = tmp / "git.example.com" / "grp" / "proj" / "develop" / "issue-1"
            other.mkdir(parents=True)
            (other / "log.yaml").write_text("- m: a\n")
            subprocess.run(["git", "add", "."], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=tmpdir, check=True, capture_output=True)

            # Memory unchanged; an unrelated file (log.yaml) is dirty.
            (other / "log.yaml").write_text("- m: a\n- m: b\n")

            with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": tmpdir}):
                result = commit_work_path("git.example.com/grp/proj/memory")
            assert result == 0
            # No new commit should have been created.
            rc, out = run_git_command(["log", "--oneline"], tmp, check=False)
            assert out.strip().count("\n") == 0  # exactly one commit (init)

    def test_commit_work_changes_custom_message(self):
        """Test commit with custom message"""
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmpdir, check=True, capture_output=True)

            target_dir = Path(tmpdir) / "github.com" / "owner" / "repo" / "review" / "pr-123"
            target_dir.mkdir(parents=True)
            (target_dir / "log.yaml").write_text("- message: test\n")

            env_vars = {
                "LMER_WORK_REPO_PATH": tmpdir,
                "LMER_REPO_HOST": "github.com",
                "LMER_REPO_PROJECT": "owner/repo",
                "LMER_TASK": "review",
                "LMER_TASK_TARGET": "pr-123",
            }

            with patch.dict(os.environ, env_vars):
                with patch("work_repo.git_ops.run_git_command") as mock_git:
                    def git_side_effect(cmd, cwd, check=False):
                        if cmd == ["commit", "-m", "Custom message"]:
                            return (0, "")
                        elif cmd == ["status", "--porcelain", "--", "github.com/owner/repo/review/pr-123"]:
                            return (0, "M  github.com/owner/repo/review/pr-123/log.yaml\n")
                        return (0, "")

                    mock_git.side_effect = git_side_effect
                    result = commit_work_changes("Custom message")
                    # Verify custom message was used
                    commit_calls = [call for call in mock_git.call_args_list if call[0][0] == ["commit", "-m", "Custom message"]]
                    assert len(commit_calls) > 0


class TestCommitWorkPathResilience:
    """Commit-first ordering, stderr surfacing, and push retry (the session-end
    push race observed 2026-07-05: dirty tree -> pull refused -> non-FF push,
    all with empty error messages)."""

    def test_failure_output_includes_stderr(self, tmp_path):
        # Not a git repo: git prints the error to stderr, which must surface.
        rc, output = run_git_command(["status"], tmp_path, check=False)
        assert rc != 0
        assert output.strip(), "failure output must not be empty"
        assert "not a git repository" in output.lower()

    def test_commit_happens_before_pull_and_push_retries_with_rebase(self, tmp_path):
        rel = "git.example.com/grp/proj/runs/develop-x"
        (tmp_path / rel).mkdir(parents=True)
        calls = []

        def side_effect(cmd, cwd, check=False):
            calls.append(cmd)
            if cmd[0] == "status":
                return (0, f"M  {rel}/state.yaml\n")
            if cmd == ["push"]:
                # First push rejected (non-FF), second succeeds.
                pushes = [c for c in calls if c == ["push"]]
                if len(pushes) == 1:
                    return (1, "! [rejected] main -> main (fetch first)")
                return (0, "")
            return (0, "")

        with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": str(tmp_path)}):
            with patch("work_repo.git_ops.run_git_command", side_effect=side_effect):
                assert commit_work_path(rel) == 0

        commit_i = calls.index(["commit", "-m", f"Update work repo: {rel}"])
        first_pull_i = next(i for i, c in enumerate(calls) if c == ["pull", "--rebase"])
        first_push_i = calls.index(["push"])
        assert commit_i < first_pull_i < first_push_i, calls
        # A rebase happens between the rejected and the retried push.
        push_indices = [i for i, c in enumerate(calls) if c == ["push"]]
        assert len(push_indices) == 2
        rebases_between = [
            c for c in calls[push_indices[0] + 1:push_indices[1]] if c == ["pull", "--rebase"]
        ]
        assert rebases_between, "must rebase between push attempts"

    def test_push_gives_up_after_retries(self, tmp_path):
        rel = "git.example.com/grp/proj/memory"
        (tmp_path / rel).mkdir(parents=True)

        def side_effect(cmd, cwd, check=False):
            if cmd[0] == "status":
                return (0, f"M  {rel}/fact.md\n")
            if cmd == ["push"]:
                return (1, "! [rejected]")
            return (0, "")

        with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": str(tmp_path)}):
            with patch("work_repo.git_ops.run_git_command", side_effect=side_effect) as mock_git:
                assert commit_work_path(rel) == 1
                assert [c for c in (call[0][0] for call in mock_git.call_args_list)
                        if c == ["push"]].count(["push"]) == 3

    def test_multi_path_staging_skips_missing(self, tmp_path):
        existing = "git.example.com/grp/proj/develop/issue-1"
        missing = "git.example.com/grp/proj/runs/develop-issue-1"
        (tmp_path / existing).mkdir(parents=True)

        def side_effect(cmd, cwd, check=False):
            if cmd[0] == "status":
                return (0, "M  something\n")
            return (0, "")

        with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": str(tmp_path)}):
            with patch("work_repo.git_ops.run_git_command", side_effect=side_effect) as mock_git:
                assert commit_work_path([existing, missing]) == 0
                add_calls = [call[0][0] for call in mock_git.call_args_list
                             if call[0][0][:3] == ["add", "-A", "--"]]
                assert add_calls == [["add", "-A", "--", existing]]

    def test_tracked_but_deleted_path_is_kept_for_staging(self, tmp_path):
        # A run-dir rename leaves the old path gone from disk but its files
        # still tracked — staging it is what commits the move as a move.
        existing = "git.example.com/grp/proj/runs/develop-issue-1--lifecycle"
        deleted = "git.example.com/grp/proj/runs/develop-issue-1"
        (tmp_path / existing).mkdir(parents=True)

        def side_effect(cmd, cwd, check=False):
            if cmd[0] == "ls-files":
                return (0, f"{deleted}/state.yaml\n")
            if cmd[0] == "status":
                return (0, "R  something\n")
            return (0, "")

        with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": str(tmp_path)}):
            with patch("work_repo.git_ops.run_git_command", side_effect=side_effect) as mock_git:
                assert commit_work_path([existing, deleted]) == 0
                add_calls = [call[0][0] for call in mock_git.call_args_list
                             if call[0][0][:3] == ["add", "-A", "--"]]
                assert add_calls == [["add", "-A", "--", existing, deleted]]

    def test_has_tracked_files_against_real_repo(self, tmp_path):
        import subprocess

        from work_repo.git_ops import _has_tracked_files

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        tracked = tmp_path / "runs" / "old-run"
        tracked.mkdir(parents=True)
        (tracked / "state.yaml").write_text("schema: 1\n", encoding="utf-8")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"],
            check=True, env=env,
        )
        import shutil

        shutil.rmtree(tracked)
        assert _has_tracked_files(tmp_path, "runs/old-run") is True
        assert _has_tracked_files(tmp_path, "runs/never-existed") is False

    def test_all_paths_missing_is_clean_noop(self, tmp_path):
        with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": str(tmp_path)}):
            with patch(
                "work_repo.git_ops.run_git_command", return_value=(0, "")
            ) as mock_git:
                assert commit_work_path(["nope/a", "nope/b"]) == 0
                # Missing paths get a read-only tracked-files probe (they may
                # hold pending deletions) — but nothing is staged or committed.
                assert all(
                    call.args[0][0] == "ls-files"
                    for call in mock_git.call_args_list
                )

    def test_commit_work_changes_includes_runs_dir(self, tmp_path):
        env_vars = {
            "LMER_WORK_REPO_PATH": str(tmp_path),
            "LMER_REPO_HOST": "git.example.com",
            "LMER_REPO_PROJECT": "grp/proj",
            "LMER_TASK": "develop",
            "LMER_TASK_TARGET": "https://git.example.com/grp/proj/-/issues/9",
        }
        task_dir = tmp_path / "git.example.com/grp/proj/develop/issue-9"
        runs_dir = tmp_path / "git.example.com/grp/proj/runs/develop-issue-9"
        task_dir.mkdir(parents=True)
        runs_dir.mkdir(parents=True)

        def side_effect(cmd, cwd, check=False):
            if cmd[0] == "status":
                return (0, "M  x\n")
            return (0, "")

        with patch.dict(os.environ, env_vars):
            with patch("work_repo.git_ops.run_git_command", side_effect=side_effect) as mock_git:
                assert commit_work_changes() == 0
                add_calls = [call[0][0] for call in mock_git.call_args_list
                             if call[0][0][:3] == ["add", "-A", "--"]]
                assert add_calls == [[
                    "add", "-A", "--",
                    "git.example.com/grp/proj/develop/issue-9",
                    "git.example.com/grp/proj/runs/develop-issue-9",
                ]]

    def test_commit_work_changes_stages_task_runs_and_specs_in_order(self, tmp_path):
        """MR !140 staging gap: entries written by `work specs-index
        --rebuild` / the masterplan sync have no push of their own — the
        batching `work commit` must stage specs/ too (skipped when the
        dir doesn't exist, free when clean).

        Distinct name from the !126 test below on purpose: they shared one
        method name until the review on !154, and Python silently kept only
        the later definition — dropping this stricter argv/order assertion.
        """
        env_vars = {
            "LMER_WORK_REPO_PATH": str(tmp_path),
            "LMER_REPO_HOST": "git.example.com",
            "LMER_REPO_PROJECT": "grp/proj",
            "LMER_TASK": "develop",
            "LMER_TASK_TARGET": "issue-9",
        }
        task_dir = tmp_path / "git.example.com/grp/proj/develop/issue-9"
        runs_dir = tmp_path / "git.example.com/grp/proj/runs/develop-issue-9"
        specs_dir = tmp_path / "git.example.com/grp/proj/specs"
        for d in (task_dir, runs_dir, specs_dir):
            d.mkdir(parents=True)

        def side_effect(cmd, cwd, check=False):
            if cmd[0] == "status":
                return (0, "M  x\n")
            return (0, "")

        with patch.dict(os.environ, env_vars):
            with patch("work_repo.git_ops.run_git_command", side_effect=side_effect) as mock_git:
                assert commit_work_changes() == 0
                add_calls = [call[0][0] for call in mock_git.call_args_list
                             if call[0][0][:3] == ["add", "-A", "--"]]
                assert add_calls == [[
                    "add", "-A", "--",
                    "git.example.com/grp/proj/develop/issue-9",
                    "git.example.com/grp/proj/runs/develop-issue-9",
                    "git.example.com/grp/proj/specs",
                ]]


    def test_commit_work_changes_includes_specs_index(self, tmp_path):
        """The specs index is staged when it exists (review on !126): entries
        created by the masterplan sync, `specs-index --rebuild`, and the
        freeze-rename re-point must reach the remote via `work commit`, not
        only via `work artifact`."""
        env_vars = {
            "LMER_WORK_REPO_PATH": str(tmp_path),
            "LMER_REPO_HOST": "git.example.com",
            "LMER_REPO_PROJECT": "grp/proj",
            "LMER_TASK": "develop",
            "LMER_TASK_TARGET": "https://git.example.com/grp/proj/-/issues/9",
        }
        task_dir = tmp_path / "git.example.com/grp/proj/develop/issue-9"
        specs_dir = tmp_path / "git.example.com/grp/proj/specs"
        task_dir.mkdir(parents=True)
        specs_dir.mkdir(parents=True)

        def side_effect(cmd, cwd, check=False):
            if cmd[0] == "status":
                return (0, "M  x\n")
            return (0, "")

        with patch.dict(os.environ, env_vars):
            with patch("work_repo.git_ops.run_git_command", side_effect=side_effect) as mock_git:
                assert commit_work_changes() == 0
                add_calls = [call[0][0] for call in mock_git.call_args_list
                             if call[0][0][:3] == ["add", "-A", "--"]]
                assert len(add_calls) == 1
                assert "git.example.com/grp/proj/specs" in add_calls[0]


class TestReportUncommittedWorkItems:
    """report_uncommitted_work_items: the issue #85 stray-file reminder.

    `work commit` stages only the run dir, so a file added elsewhere in the
    work repo is left behind. This repo-wide, fail-soft reminder flags it.
    """

    @staticmethod
    def _init_repo(path):
        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True, capture_output=True)

    @staticmethod
    def _commit_all(path, message="init"):
        subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", message], cwd=path, check=True, capture_output=True)

    def test_clean_tree_reports_nothing(self, tmp_path, capsys):
        """A clean work repo prints nothing and returns 0 (no noise)."""
        self._init_repo(tmp_path)
        (tmp_path / "README.md").write_text("x\n")
        self._commit_all(tmp_path)
        with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": str(tmp_path)}):
            assert report_uncommitted_work_items() == 0
        assert capsys.readouterr().out == ""

    def test_missing_repo_is_silent_zero(self, tmp_path, capsys):
        """No work repo on disk → silent 0 (fail-soft)."""
        with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": str(tmp_path / "nope")}):
            assert report_uncommitted_work_items() == 0
        assert capsys.readouterr().out == ""

    def test_untracked_and_modified_are_listed(self, tmp_path, capsys):
        """Both a new untracked file and a modified tracked file are flagged."""
        self._init_repo(tmp_path)
        info = tmp_path / "git.example.com" / "grp" / "proj" / "info"
        info.mkdir(parents=True)
        tracked = info / "existing.md"
        tracked.write_text("v1\n")
        self._commit_all(tmp_path)

        # A stray new info file + an edit to a tracked one — both left behind
        # by a run-dir-scoped `work commit`.
        (info / "new.md").write_text("new\n")
        tracked.write_text("v2\n")

        with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": str(tmp_path)}):
            assert report_uncommitted_work_items() == 2
        out = capsys.readouterr().out
        assert "2 uncommitted item(s)" in out
        assert "not staged by `work commit`" in out
        assert "info/new.md" in out
        assert "info/existing.md" in out

    def test_list_is_capped_with_overflow_note(self, tmp_path, capsys):
        """More than the cap of stray items → cap lines + a '... and N more'."""
        self._init_repo(tmp_path)
        overflow = 3
        total = UNTRACKED_REPORT_CAP + overflow
        for i in range(total):
            (tmp_path / f"stray_{i:02d}.txt").write_text("x\n")

        with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": str(tmp_path)}):
            assert report_uncommitted_work_items() == total
        out = capsys.readouterr().out
        assert f"{total} uncommitted item(s)" in out
        assert f"... and {overflow} more" in out
        # Exactly the cap number of untracked-entry lines are printed.
        item_lines = [ln for ln in out.splitlines() if ln.strip().startswith("??")]
        assert len(item_lines) == UNTRACKED_REPORT_CAP

    def test_git_error_is_silent_zero(self, tmp_path, capsys):
        """A dir that is not a git repo → git status fails → silent 0."""
        with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": str(tmp_path)}):
            assert report_uncommitted_work_items() == 0
        assert capsys.readouterr().out == ""

    def test_exception_is_swallowed(self, tmp_path):
        """Any unexpected error is swallowed to 0 — a reminder is never a gate."""
        with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": str(tmp_path)}):
            with patch(
                "work_repo.git_ops.run_git_command",
                side_effect=RuntimeError("boom"),
            ):
                assert report_uncommitted_work_items() == 0

    def test_new_untracked_subtree_lists_files_individually(self, tmp_path, capsys):
        """A brand-new untracked directory is enumerated per file, not collapsed
        into one entry — the literal issue #85 shape (a new, untracked info dir).

        Plain `git status --porcelain` would emit a single `?? .../info/` line
        and undercount; `--untracked-files=all` names each file.
        """
        self._init_repo(tmp_path)
        (tmp_path / "README.md").write_text("x\n")
        self._commit_all(tmp_path)
        # An entirely-untracked new directory holding two files.
        info = tmp_path / "git.example.com" / "grp" / "proj" / "info"
        info.mkdir(parents=True)
        (info / "new.md").write_text("a\n")
        (info / "another.md").write_text("b\n")

        with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": str(tmp_path)}):
            assert report_uncommitted_work_items() == 2
        out = capsys.readouterr().out
        assert "2 uncommitted item(s)" in out
        assert "info/new.md" in out
        assert "info/another.md" in out
        # The directory must NOT appear as a single collapsed entry.
        assert not any(
            ln.strip() == "?? git.example.com/grp/proj/info/"
            for ln in out.splitlines()
        )

    def test_exact_cap_has_no_overflow_note(self, tmp_path, capsys):
        """Exactly UNTRACKED_REPORT_CAP items → all listed, no '... and N more'
        (the off-by-one boundary of the `remaining > 0` guard)."""
        self._init_repo(tmp_path)
        for i in range(UNTRACKED_REPORT_CAP):
            (tmp_path / f"stray_{i:02d}.txt").write_text("x\n")

        with patch.dict(os.environ, {"LMER_WORK_REPO_PATH": str(tmp_path)}):
            assert report_uncommitted_work_items() == UNTRACKED_REPORT_CAP
        out = capsys.readouterr().out
        assert f"{UNTRACKED_REPORT_CAP} uncommitted item(s)" in out
        assert "more" not in out  # no overflow tail exactly at the cap
        item_lines = [ln for ln in out.splitlines() if ln.strip().startswith("??")]
        assert len(item_lines) == UNTRACKED_REPORT_CAP


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd),
         "-c", "user.name=test", "-c", "user.email=test@example.com",
         *args],
        check=True, capture_output=True,
    )


def _clone_with_run_dir(tmp_path):
    """Bare origin + clone holding a committed-and-pushed run dir — the same
    harness shape as the Stop-hook guard tests whose trigger-2 predicate
    run_dir_push_status mirrors."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True,
    )
    clone = tmp_path / "workrepo"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)],
        check=True, capture_output=True,
    )
    run_dir = clone / "git.example.com" / "org/repo" / "runs" / "develop-issue-1"
    run_dir.mkdir(parents=True)
    (run_dir / "notes.md").write_text("artifact\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-q", "-m", "run dir")
    _git(clone, "push", "-q", "-u", "origin", "main")
    return clone, run_dir


class TestRunDirPushStatus:
    """The issue #100 phase-boundary predicate — trigger-2 semantics."""

    def test_clean_and_pushed(self, tmp_path):
        _, run_dir = _clone_with_run_dir(tmp_path)
        assert run_dir_push_status(run_dir) == (False, False)

    def test_dirty_run_dir(self, tmp_path):
        _, run_dir = _clone_with_run_dir(tmp_path)
        (run_dir / "scratch.md").write_text("uncommitted\n")
        assert run_dir_push_status(run_dir) == (True, False)

    def test_unpushed_commit(self, tmp_path):
        clone, run_dir = _clone_with_run_dir(tmp_path)
        (run_dir / "notes.md").write_text("amended\n")
        _git(clone, "add", ".")
        _git(clone, "commit", "-q", "-m", "local only")  # no push
        assert run_dir_push_status(run_dir) == (False, True)

    def test_dirt_outside_run_dir_does_not_count(self, tmp_path):
        clone, run_dir = _clone_with_run_dir(tmp_path)
        (clone / "elsewhere.md").write_text("other project\n")
        assert run_dir_push_status(run_dir) == (False, False)

    def test_missing_dir_fails_open(self, tmp_path):
        assert run_dir_push_status(tmp_path / "absent") == (False, False)

    def test_non_git_dir_fails_open(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "junk.md").write_text("dirty-looking but ungoverned\n")
        assert run_dir_push_status(plain) == (False, False)

    def test_no_upstream_reads_as_pushed(self, tmp_path):
        # Local-only repo: ahead-of-upstream is unknowable — fail open, but
        # the dirty half still works independently.
        repo = tmp_path / "local"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        (repo / "notes.md").write_text("artifact\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "local only")
        assert run_dir_push_status(repo) == (False, False)
        (repo / "scratch.md").write_text("uncommitted\n")
        assert run_dir_push_status(repo) == (True, False)


class TestRunDirPushStatusMirrorsHook:
    """Drift guard for the deliberate mirror: run_dir_push_status duplicates
    the Stop-hook guard's trigger-2 gatherer (hooks/run_state_guard.py's
    gather_run_dir_status) rather than importing it, since hooks import no
    project code. Run both copies against the same fixture repos so they
    cannot silently diverge."""

    def _both(self, run_dir):
        """Assert the hook and git_ops copies agree; return the verdict."""
        hook_verdict = gather_run_dir_status(str(run_dir))
        ops_verdict = run_dir_push_status(run_dir)
        assert hook_verdict == ops_verdict, (
            "hooks.run_state_guard.gather_run_dir_status and "
            "work_repo.git_ops.run_dir_push_status diverged on the same "
            f"fixture: hook={hook_verdict} git_ops={ops_verdict}"
        )
        return ops_verdict

    def test_clean_and_pushed_agree(self, tmp_path):
        _, run_dir = _clone_with_run_dir(tmp_path)
        assert self._both(run_dir) == (False, False)

    def test_dirty_run_dir_agree(self, tmp_path):
        _, run_dir = _clone_with_run_dir(tmp_path)
        (run_dir / "scratch.md").write_text("uncommitted\n")
        assert self._both(run_dir) == (True, False)

    def test_unpushed_commit_agree(self, tmp_path):
        clone, run_dir = _clone_with_run_dir(tmp_path)
        (run_dir / "notes.md").write_text("amended\n")
        _git(clone, "add", ".")
        _git(clone, "commit", "-q", "-m", "local only")  # no push
        assert self._both(run_dir) == (False, True)

    def test_non_git_dir_fails_open_in_both(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "junk.md").write_text("dirty-looking but ungoverned\n")
        assert self._both(plain) == (False, False)


def _git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)


class TestCommitNapkinIfSubdir:
    def test_no_napkin_path_is_noop(self):
        with patch.dict(os.environ, {}, clear=True):
            assert commit_napkin_if_subdir() == 0

    def test_separate_repo_is_noop(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        napkin = tmp_path / "napkin"
        _git_init(napkin)
        env = {"LMER_NAPKIN_PATH": str(napkin), "LMER_WORK_REPO_PATH": str(work)}
        with patch.dict(os.environ, env, clear=True):
            with patch("work_repo.git_ops.run_git_command") as mock_git:
                assert commit_napkin_if_subdir() == 0
                mock_git.assert_not_called()

    def test_nested_git_repo_under_work_is_noop(self, tmp_path):
        work = tmp_path / "work"
        napkin = work / "napkin"
        _git_init(napkin)  # its own repo — unreachable via the work repo's index
        env = {"LMER_NAPKIN_PATH": str(napkin), "LMER_WORK_REPO_PATH": str(work)}
        with patch.dict(os.environ, env, clear=True):
            with patch("work_repo.git_ops.run_git_command") as mock_git:
                assert commit_napkin_if_subdir() == 0
                mock_git.assert_not_called()

    def test_missing_napkin_dir_is_noop(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        env = {"LMER_NAPKIN_PATH": str(work / "napkin"), "LMER_WORK_REPO_PATH": str(work)}
        with patch.dict(os.environ, env, clear=True):
            with patch("work_repo.git_ops.run_git_command") as mock_git:
                assert commit_napkin_if_subdir() == 0
                mock_git.assert_not_called()

    def test_subdir_notes_are_staged_committed_and_pushed(self, tmp_path):
        """Regression: notes under {work_repo}/napkin/ must be captured by
        work commit — commit_work_changes stages only the task-target and
        run-dir paths, so without this pass subdir-mode notes were silently
        lost when the ephemeral container exited."""
        work = tmp_path / "work"
        napkin = work / "napkin"
        (napkin / "org-a").mkdir(parents=True)
        (napkin / "org-a" / "note.md").write_text("finding")
        env = {"LMER_NAPKIN_PATH": str(napkin), "LMER_WORK_REPO_PATH": str(work)}
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "work_repo.git_ops.run_git_command",
                return_value=(0, "A  napkin/org-a/note.md"),
            ) as mock_git:
                assert commit_napkin_if_subdir("msg") == 0
        calls = [c.args[0] for c in mock_git.call_args_list]
        assert ["add", "-A", "--", "napkin"] in calls
        assert ["commit", "-m", "msg"] in calls
        assert ["push"] in calls
        # Everything runs against the work repo, not the napkin path.
        assert all(c.args[1] == Path(str(work)) for c in mock_git.call_args_list)

    def test_default_commit_message(self, tmp_path):
        work = tmp_path / "work"
        napkin = work / "napkin"
        napkin.mkdir(parents=True)
        (napkin / "note.md").write_text("x")
        env = {"LMER_NAPKIN_PATH": str(napkin), "LMER_WORK_REPO_PATH": str(work)}
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "work_repo.git_ops.run_git_command",
                return_value=(0, "A  napkin/note.md"),
            ) as mock_git:
                assert commit_napkin_if_subdir() == 0
        calls = [c.args[0] for c in mock_git.call_args_list]
        assert ["commit", "-m", "Update napkin notes"] in calls


class TestPushNapkinIfSeparate:
    def test_no_napkin_path_is_noop(self):
        with patch.dict(os.environ, {}, clear=True):
            assert push_napkin_if_separate() == 0

    def test_non_git_napkin_is_noop(self, tmp_path):
        napkin = tmp_path / "napkin"
        napkin.mkdir()
        env = {"LMER_NAPKIN_PATH": str(napkin), "LMER_WORK_REPO_PATH": str(tmp_path / "work")}
        with patch.dict(os.environ, env, clear=True):
            with patch("work_repo.git_ops.run_git_command") as mock_git:
                assert push_napkin_if_separate() == 0
                mock_git.assert_not_called()

    def test_subdir_napkin_is_noop(self, tmp_path):
        work = tmp_path / "work"
        napkin = work / "napkin"
        _git_init(napkin)  # even with a .git, being under work means skip
        env = {"LMER_NAPKIN_PATH": str(napkin), "LMER_WORK_REPO_PATH": str(work)}
        with patch.dict(os.environ, env, clear=True):
            with patch("work_repo.git_ops.run_git_command") as mock_git:
                assert push_napkin_if_separate() == 0
                mock_git.assert_not_called()

    def test_separate_repo_runs_full_flow(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        napkin = tmp_path / "napkin"
        _git_init(napkin)
        env = {"LMER_NAPKIN_PATH": str(napkin), "LMER_WORK_REPO_PATH": str(work)}
        with patch.dict(os.environ, env, clear=True):
            with patch("work_repo.git_ops.run_git_command", return_value=(0, " M note.md")) as mock_git:
                rc = push_napkin_if_separate("msg")
        assert rc == 0
        commands = [c.args[0][0] for c in mock_git.call_args_list]
        assert "push" in commands
        assert "commit" in commands

    def test_separate_repo_no_changes_skips_push(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        napkin = tmp_path / "napkin"
        _git_init(napkin)
        env = {"LMER_NAPKIN_PATH": str(napkin), "LMER_WORK_REPO_PATH": str(work)}

        def fake_git(cmd, cwd, check=False):
            if cmd[0] == "status":
                return (0, "")  # clean
            return (0, "")

        with patch.dict(os.environ, env, clear=True):
            with patch("work_repo.git_ops.run_git_command", side_effect=fake_git) as mock_git:
                rc = push_napkin_if_separate()
        assert rc == 0
        commands = [c.args[0][0] for c in mock_git.call_args_list]
        assert "push" not in commands

    def test_push_failure_returns_nonzero(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        napkin = tmp_path / "napkin"
        _git_init(napkin)
        env = {"LMER_NAPKIN_PATH": str(napkin), "LMER_WORK_REPO_PATH": str(work)}

        def fake_git(cmd, cwd, check=False):
            if cmd[0] == "status":
                return (0, " M note.md")
            if cmd[0] == "push":
                return (1, "rejected")
            return (0, "")

        with patch.dict(os.environ, env, clear=True):
            with patch("work_repo.git_ops.run_git_command", side_effect=fake_git):
                rc = push_napkin_if_separate()
        assert rc == 1

    def test_commit_first_then_rebase_and_push_retry(self, tmp_path):
        """Dirty tree + moved remote: the napkin flow must commit FIRST (a
        plain pull refuses on a dirty tree), integrate with pull --rebase,
        and retry a rejected push with a rebase in between — same ordering
        as commit_work_path, because a shared napkin repo has concurrent
        writers by design."""
        work = tmp_path / "work"
        work.mkdir()
        napkin = tmp_path / "napkin"
        _git_init(napkin)
        env = {"LMER_NAPKIN_PATH": str(napkin), "LMER_WORK_REPO_PATH": str(work)}
        calls = []

        def fake_git(cmd, cwd, check=False):
            calls.append(cmd)
            if cmd[0] == "status":
                return (0, " M note.md")
            if cmd == ["push"]:
                # First push rejected (non-FF), second succeeds.
                if [c for c in calls if c == ["push"]] == [["push"]]:
                    return (1, "! [rejected] main -> main (fetch first)")
                return (0, "")
            return (0, "")

        with patch.dict(os.environ, env, clear=True):
            with patch("work_repo.git_ops.run_git_command", side_effect=fake_git):
                assert push_napkin_if_separate("msg") == 0

        commit_i = calls.index(["commit", "-m", "msg"])
        first_rebase_i = next(i for i, c in enumerate(calls) if c == ["pull", "--rebase"])
        assert commit_i < first_rebase_i, calls
        # No plain (non-rebase) pull anywhere — it would refuse on dirty trees.
        assert ["pull"] not in calls
        push_indices = [i for i, c in enumerate(calls) if c == ["push"]]
        assert len(push_indices) == 2
        rebases_between = [
            c for c in calls[push_indices[0] + 1:push_indices[1]]
            if c == ["pull", "--rebase"]
        ]
        assert rebases_between, "must rebase between push attempts"
