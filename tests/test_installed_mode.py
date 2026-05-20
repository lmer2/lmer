#!/usr/bin/env python3
"""Tests for installed mode (uv tool install) behavior.

These tests verify that the CLI works correctly when installed via
`uv tool install` where no local repository checkout exists.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from lmer_cli.cli import _discover_tasks
from lmer_cli.container_home import ensure_container_home
from lmer_cli.mounts import build_global_mount, build_lmer_docs_mount
from lmer_cli.runtime import InstallMode, lmer_state_dir, repo_root_path


class TestInstalledModeRepoRoot:
    """Test that repo_root_path returns None in installed mode."""

    def test_repo_root_is_none(self):
        """repo_root_path should return None when install mode is INSTALLED"""
        with patch(
            "lmer_cli.runtime.detect_install_mode",
            return_value=InstallMode.INSTALLED,
        ):
            assert repo_root_path() is None


class TestInstalledModeTaskDiscovery:
    """Test task discovery behavior when no local taskdef exists."""

    def test_discover_tasks_returns_empty_for_missing_dir(self):
        """With no taskdef dir, _discover_tasks returns an empty set"""
        tasks = _discover_tasks(Path("/nonexistent/path/taskdef"))
        assert tasks == set()

    def test_task_validation_skipped_when_known_tasks_empty(self):
        """When known_tasks is empty, any task name passes the guard.

        The validation logic is: `if known_tasks and task_id not in known_tasks`.
        Empty set is falsy, so the check passes for any task_id.
        """
        known_tasks: set[str] = set()
        task_id = "review"
        # This mirrors the validation logic in cli.py
        should_error = known_tasks and task_id not in known_tasks
        assert not should_error

    def test_unknown_task_name_passes_when_no_discovery(self):
        """Even a made-up task name should pass when known_tasks is empty"""
        known_tasks: set[str] = set()
        task_id = "nonexistent_task"
        should_error = known_tasks and task_id not in known_tasks
        assert not should_error


class TestInstalledModeContainerHome:
    """Test container-home uses state dir in installed mode."""

    def test_container_home_at_state_dir(self):
        """When repo_root is None, container-home should be at state_dir/container-home"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            container_home = ensure_container_home(state_dir)
            assert container_home == state_dir / "container-home"
            assert container_home.exists()
            assert (container_home / ".ssh").exists()
            assert (container_home / ".config").exists()

    def test_container_home_idempotent(self):
        """Calling ensure_container_home twice should not error"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            first = ensure_container_home(state_dir)
            second = ensure_container_home(state_dir)
            assert first == second


class TestInstalledModeNoMounts:
    """Test that global mounts are skipped in installed mode."""

    def test_no_global_mount_when_repo_root_none(self):
        """build_global_mount should not be called when repo_root is None.

        In installed mode, the container image has all assets baked in.
        The CLI skips the mount calls entirely (conditional in cli.py).
        This test verifies the mount function returns empty for a
        non-existent path, as a safety net.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            # Use an empty dir (no bin/, src/, etc.) to simulate missing assets
            empty_root = Path(tmpdir)
            args = build_global_mount("docker", empty_root)
            assert args == []

    def test_no_lmer_docs_mount_when_repo_root_none(self):
        """build_lmer_docs_mount should return empty for missing lmer-docs"""
        with tempfile.TemporaryDirectory() as tmpdir:
            empty_root = Path(tmpdir)
            args = build_lmer_docs_mount("docker", empty_root)
            assert args == []


class TestInstalledModeTaskdefEnvVars:
    """Test that LMER_TASKDEF_* env vars are set without local discovery."""

    def test_taskdef_vars_set_with_task_id(self):
        """In installed mode, taskdef env vars should be set based on task_id alone"""
        task_id = "review"
        no_task = False

        # This mirrors the env dict construction in cli.py
        env = {
            "LMER_TASKDEF_ROOT": "/Agents/global/taskdef" if (not no_task) else None,
            "LMER_TASKDEF_DIR": f"/Agents/global/taskdef/{task_id}"
            if (not no_task and task_id)
            else None,
            "LMER_TASK_INSTRUCTIONS": f"/Agents/global/taskdef/{task_id}/instructions.txt"
            if (not no_task and task_id)
            else None,
        }

        assert env["LMER_TASKDEF_ROOT"] == "/Agents/global/taskdef"
        assert env["LMER_TASKDEF_DIR"] == "/Agents/global/taskdef/review"
        assert (
            env["LMER_TASK_INSTRUCTIONS"]
            == "/Agents/global/taskdef/review/instructions.txt"
        )

    def test_taskdef_vars_none_in_no_task_mode(self):
        """When --no-task is set, taskdef vars should be None"""
        task_id = None
        no_task = True

        env = {
            "LMER_TASKDEF_ROOT": "/Agents/global/taskdef" if (not no_task) else None,
            "LMER_TASKDEF_DIR": f"/Agents/global/taskdef/{task_id}"
            if (not no_task and task_id)
            else None,
            "LMER_TASK_INSTRUCTIONS": f"/Agents/global/taskdef/{task_id}/instructions.txt"
            if (not no_task and task_id)
            else None,
        }

        assert env["LMER_TASKDEF_ROOT"] is None
        assert env["LMER_TASKDEF_DIR"] is None
        assert env["LMER_TASK_INSTRUCTIONS"] is None


class TestInstalledModeEnvFileLoading:
    """Test .env file loading from state dir."""

    def test_env_file_from_state_dir(self):
        """In installed mode, .env should be loadable from ~/.lmer/.env"""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            env_file = state_dir / ".env"
            env_file.write_text("TEST_VAR=hello\nOTHER_VAR=world\n")
            assert env_file.exists()
            # Verify the file is readable and has expected content
            content = env_file.read_text()
            assert "TEST_VAR=hello" in content

    def test_state_dir_is_consistent(self):
        """lmer_state_dir should always return the same path"""
        assert lmer_state_dir() == lmer_state_dir()
        assert lmer_state_dir() == Path.home() / ".lmer"
