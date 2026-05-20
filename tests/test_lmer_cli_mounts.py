#!/usr/bin/env python3
"""Tests for lmer_cli.mounts module"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile

from lmer_cli.mounts import (
    selinux_opt,
    _is_selinux_enforcing,
    build_workspace_mount,
    build_global_mount,
    build_lmer_docs_mount,
    build_host_repo_ro_mount,
    build_user_mounts,
    build_checkout_mount,
    build_service_mode_mounts,
)


class TestSelinuxOpt:
    """Test SELinux option generation"""

    def test_selinux_opt_when_selinux_enforcing(self):
        """Test both runtimes get ,z suffix when SELinux is enforcing"""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=True):
            # Clear the lru_cache to ensure our mock is used
            _is_selinux_enforcing.cache_clear()
            assert selinux_opt("podman") == ",z"
            assert selinux_opt("docker") == ",z"

    def test_selinux_opt_when_selinux_not_enforcing(self):
        """Test both runtimes get empty suffix when SELinux is not enforcing"""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            assert selinux_opt("podman") == ""
            assert selinux_opt("docker") == ""

    def test_is_selinux_enforcing_returns_true_when_enforcing(self):
        """Test SELinux detection when getenforce returns Enforcing"""
        _is_selinux_enforcing.cache_clear()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Enforcing\n"
        with patch("lmer_cli.runtime.subprocess.run", return_value=mock_result):
            _is_selinux_enforcing.cache_clear()
            assert _is_selinux_enforcing() is True

    def test_is_selinux_enforcing_returns_false_when_permissive(self):
        """Test SELinux detection when getenforce returns Permissive"""
        _is_selinux_enforcing.cache_clear()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Permissive\n"
        with patch("lmer_cli.runtime.subprocess.run", return_value=mock_result):
            _is_selinux_enforcing.cache_clear()
            assert _is_selinux_enforcing() is False

    def test_is_selinux_enforcing_returns_false_when_disabled(self):
        """Test SELinux detection when getenforce returns Disabled"""
        _is_selinux_enforcing.cache_clear()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Disabled\n"
        with patch("lmer_cli.runtime.subprocess.run", return_value=mock_result):
            _is_selinux_enforcing.cache_clear()
            assert _is_selinux_enforcing() is False

    def test_is_selinux_enforcing_returns_false_when_command_not_found(self):
        """Test SELinux detection when getenforce command is not available"""
        _is_selinux_enforcing.cache_clear()
        with patch("lmer_cli.runtime.subprocess.run", side_effect=FileNotFoundError):
            _is_selinux_enforcing.cache_clear()
            assert _is_selinux_enforcing() is False


class TestBuildWorkspaceMount:
    """Test workspace mount configuration"""

    def test_workspace_bind_mount_without_selinux(self):
        """Test bind mount without SELinux"""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            bind_path = Path("/tmp/workspace")
            args = build_workspace_mount("docker", None, bind_path)
            assert args == ["-v", f"{bind_path}:/workspace:rw"]

    def test_workspace_bind_mount_with_selinux(self):
        """Test bind mount with SELinux enforcing"""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=True):
            _is_selinux_enforcing.cache_clear()
            bind_path = Path("/tmp/workspace")
            args = build_workspace_mount("docker", None, bind_path)
            assert args == ["-v", f"{bind_path}:/workspace:rw,z"]

    def test_workspace_named_volume_without_selinux(self):
        """Test named volume without SELinux"""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            args = build_workspace_mount("docker", "my-workspace", None)
            assert args == ["-v", "my-workspace:/workspace:rw"]

    def test_workspace_named_volume_with_selinux(self):
        """Test named volume with SELinux enforcing"""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=True):
            _is_selinux_enforcing.cache_clear()
            args = build_workspace_mount("podman", "my-workspace", None)
            assert args == ["-v", "my-workspace:/workspace:rw,z"]

    def test_workspace_bind_preferred_over_volume(self):
        """Test bind mount takes priority over named volume"""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            bind_path = Path("/tmp/workspace")
            args = build_workspace_mount("docker", "my-workspace", bind_path)
            assert f"{bind_path}:/workspace:rw" in args[1]
            assert "my-workspace" not in args[1]

    def test_workspace_tmpfs_fallback_docker(self):
        """Test tmpfs fallback for Docker when no mount specified"""
        args = build_workspace_mount("docker", None, None)

        assert "--mount" in args
        assert "type=tmpfs,destination=/workspace" in args

    def test_workspace_tmpfs_fallback_podman(self):
        """Test tmpfs fallback for Podman when no mount specified"""
        args = build_workspace_mount("podman", None, None)

        assert "--tmpfs" in args
        assert "/workspace" in args


class TestBuildGlobalMount:
    """Test global repository mount configuration"""

    def _create_repo_dirs(self, repo_path: Path):
        """Helper to create subdirectories that build_global_mount expects."""
        for d in ["bin", "src", "hooks", "Ctl", "libexec"]:
            (repo_path / d).mkdir(parents=True, exist_ok=True)
        for d in [".claude", "rules", "taskdef"]:
            (repo_path / d).mkdir(parents=True, exist_ok=True)
        (repo_path / "AGENTS.md").write_text("")

    def test_global_mount_lmer_directory_without_selinux(self):
        """Test mounting ~/.lmer subdirectories as global without SELinux"""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            with tempfile.TemporaryDirectory() as tmpdir:
                lmer_path = Path(tmpdir) / ".lmer"
                lmer_path.mkdir()
                self._create_repo_dirs(lmer_path)

                args = build_global_mount("docker", lmer_path)

                # Should mount individual subdirectories
                assert len(args) > 0, "Expected mount arguments"
                # Check rw dirs are mounted
                assert "-v" in args
                assert any(f"{lmer_path}/bin:/Agents/global/bin:rw" in a for a in args)
                assert any(f"{lmer_path}/src:/Agents/global/src:rw" in a for a in args)
                # Check ro dirs are mounted
                assert any(f"{lmer_path}/rules:/Agents/global/rules:ro" in a for a in args)
                # Check no ,z suffix
                assert not any(",z" in a for a in args)

    def test_global_mount_lmer_directory_with_selinux(self):
        """Test mounting ~/.lmer subdirectories as global with SELinux"""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=True):
            _is_selinux_enforcing.cache_clear()
            with tempfile.TemporaryDirectory() as tmpdir:
                lmer_path = Path(tmpdir) / ".lmer"
                lmer_path.mkdir()
                self._create_repo_dirs(lmer_path)

                args = build_global_mount("docker", lmer_path)

                assert len(args) > 0, "Expected mount arguments"
                # All mounts should have ,z suffix
                mount_values = [a for a in args if a != "-v"]
                assert all(",z" in v for v in mount_values)

    def test_global_mount_podman_dev_repo_with_selinux(self):
        """Test mounting dev repo subdirectories with Podman and SELinux"""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=True):
            _is_selinux_enforcing.cache_clear()
            with tempfile.TemporaryDirectory() as tmpdir:
                repo_path = Path(tmpdir) / "global"
                repo_path.mkdir()
                self._create_repo_dirs(repo_path)

                args = build_global_mount("podman", repo_path)

                assert len(args) > 0, "Expected mount arguments"
                mount_values = [a for a in args if a != "-v"]
                assert all(",z" in v for v in mount_values)

    def test_global_mount_docker_dev_repo_without_selinux(self):
        """Test mounting dev repo subdirectories with Docker without SELinux"""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            with tempfile.TemporaryDirectory() as tmpdir:
                agents_path = Path(tmpdir) / "Agents"
                agents_path.mkdir()
                repo_path = agents_path / "global"
                repo_path.mkdir()
                self._create_repo_dirs(repo_path)

                args = build_global_mount("docker", repo_path)

                # Should mount individual subdirectories
                assert len(args) > 0, "Expected mount arguments"
                assert any(f"{repo_path}/src:/Agents/global/src:rw" in a for a in args)
                assert not any(",z" in a for a in args)

    def test_global_mount_docker_dev_repo_with_selinux(self):
        """Test mounting dev repo subdirectories with Docker and SELinux"""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=True):
            _is_selinux_enforcing.cache_clear()
            with tempfile.TemporaryDirectory() as tmpdir:
                agents_path = Path(tmpdir) / "Agents"
                agents_path.mkdir()
                repo_path = agents_path / "global"
                repo_path.mkdir()
                self._create_repo_dirs(repo_path)

                args = build_global_mount("docker", repo_path)

                assert len(args) > 0, "Expected mount arguments"
                mount_values = [a for a in args if a != "-v"]
                assert all(",z" in v for v in mount_values)


class TestBuildLmerDocsMount:
    """Test lmer-docs mount configuration"""

    def test_lmer_docs_mount_exists_without_selinux(self):
        """Test mounting lmer-docs when directory exists without SELinux"""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            with tempfile.TemporaryDirectory() as tmpdir:
                repo_path = Path(tmpdir) / "global"
                repo_path.mkdir()
                docs_path = repo_path / "lmer-docs"
                docs_path.mkdir()

                args = build_lmer_docs_mount("docker", repo_path)

                assert "-v" in args
                assert "lmer-docs" in args[1]
                assert "/Agents/global/lmer-docs:ro" in args[1]
                assert ",z" not in args[1]

    def test_lmer_docs_mount_not_exists(self):
        """Test no mount when lmer-docs doesn't exist"""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir) / "global"
            repo_path.mkdir()

            args = build_lmer_docs_mount("docker", repo_path)

            assert args == []

    def test_lmer_docs_mount_with_selinux(self):
        """Test lmer-docs mount with SELinux enforcing"""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=True):
            _is_selinux_enforcing.cache_clear()
            with tempfile.TemporaryDirectory() as tmpdir:
                repo_path = Path(tmpdir) / "global"
                repo_path.mkdir()
                docs_path = repo_path / "lmer-docs"
                docs_path.mkdir()

                args = build_lmer_docs_mount("docker", repo_path)

                assert ",z" in args[1]


class TestBuildHostRepoRoMount:
    """Test host repository read-only mount"""

    def test_host_repo_mount_without_selinux(self):
        """Test host repo mount without SELinux"""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            repo_path = Path("/home/user/project")
            args = build_host_repo_ro_mount("docker", repo_path)

            assert args == ["-v", f"{repo_path}:/host-repo:ro"]

    def test_host_repo_mount_with_selinux(self):
        """Test host repo mount with SELinux enforcing"""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=True):
            _is_selinux_enforcing.cache_clear()
            repo_path = Path("/home/user/project")
            args = build_host_repo_ro_mount("docker", repo_path)

            assert args == ["-v", f"{repo_path}:/host-repo:ro,z"]


class TestBuildUserMounts:
    """Test user home directory mounts"""

    def test_user_mounts_without_ssh_agent(self):
        """Test user mounts when SSH agent not available"""
        with patch.dict(os.environ, {}, clear=True):
            args, ssh_enabled = build_user_mounts("docker")

            assert isinstance(args, list)
            assert ssh_enabled is False

    def test_user_mounts_with_ssh_agent(self):
        """Test user mounts when SSH agent available"""
        with patch.dict(os.environ, {"SSH_AUTH_SOCK": "/tmp/ssh-agent"}):
            with patch("os.path.exists", return_value=True):
                args, ssh_enabled = build_user_mounts("docker")

                assert ssh_enabled is True
                # Should include SSH agent socket mount
                assert any("SSH_AUTH_SOCK" in str(arg) for arg in args) or \
                       any("ssh" in str(arg).lower() for arg in args)

    def test_user_mounts_includes_claude_credentials(self):
        """Test user mounts include Claude credentials if they exist"""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir) / "home"
            fake_home.mkdir()
            claude_dir = fake_home / ".claude"
            claude_dir.mkdir()
            credentials_file = claude_dir / ".credentials.json"
            credentials_file.write_text('{"api_key": "test"}')

            with patch("pathlib.Path.home", return_value=fake_home):
                args, ssh_enabled = build_user_mounts("docker")

                # Should include credentials mount
                credentials_mounted = any(".credentials.json" in str(arg) for arg in args)
                assert credentials_mounted


class TestBuildCheckoutMount:
    """Tests for build_checkout_mount (workspace-only, no socket)."""

    def test_mounts_checkout_as_workspace(self):
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            args = build_checkout_mount("docker", Path("/home/user/myproject"))
        assert args == ["-v", "/home/user/myproject:/workspace:rw"]

    def test_does_not_mount_docker_socket(self):
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            args = build_checkout_mount("docker", Path("/home/user/myproject"))
        assert not any("docker.sock" in a for a in args)
        assert not any("--group-add" in a for a in args)


class TestBuildServiceModeMounts:
    """Tests for build_service_mode_mounts (workspace + socket)."""

    def test_mounts_checkout_and_socket(self):
        mock_sock = MagicMock()
        mock_sock.stat.return_value = MagicMock(st_gid=999)
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            with patch("lmer_cli.mounts._find_container_socket", return_value=mock_sock):
                args = build_service_mode_mounts("docker", Path("/home/user/myproject"))
        assert "-v" in args
        assert "/home/user/myproject:/workspace:rw" in args
        assert any("docker.sock" in str(a) for a in args)
        assert "--group-add" in args
        assert "999" in args

    def test_no_socket_still_mounts_checkout(self):
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            with patch("lmer_cli.mounts._find_container_socket", return_value=None):
                args = build_service_mode_mounts("docker", Path("/home/user/myproject"))
        assert "/home/user/myproject:/workspace:rw" in args
        assert not any("docker.sock" in str(a) for a in args)
