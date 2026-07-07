#!/usr/bin/env python3
"""Tests for lmer_cli.mounts module"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile

from lmer_cli.cli import parse_file_mount_specs
from lmer_cli.mounts import (
    FileMountSpec,
    selinux_opt,
    _is_selinux_enforcing,
    build_workspace_mount,
    build_global_mount,
    build_lmer_docs_mount,
    build_host_repo_ro_mount,
    build_host_uv_cache_mount,
    build_user_mounts,
    build_checkout_mount,
    build_file_mounts,
    build_service_mode_mounts,
    resolve_host_uv_cache_dir,
    CONTAINER_UV_CACHE_DIR,
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


class TestResolveHostUvCacheDir:
    """Resolution order for the host's uv cache directory."""

    def test_explicit_uv_cache_dir_wins(self, monkeypatch, tmp_path):
        explicit = tmp_path / "custom-uv"
        monkeypatch.setenv("UV_CACHE_DIR", str(explicit))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert resolve_host_uv_cache_dir() == explicit

    def test_xdg_cache_home_when_no_explicit(self, monkeypatch, tmp_path):
        monkeypatch.delenv("UV_CACHE_DIR", raising=False)
        xdg = tmp_path / "xdg"
        monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))
        assert resolve_host_uv_cache_dir() == xdg / "uv"

    def test_linux_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv("UV_CACHE_DIR", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setattr("lmer_cli.mounts.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr("lmer_cli.mounts.sys.platform", "linux")
        assert resolve_host_uv_cache_dir() == tmp_path / ".cache" / "uv"

    def test_macos_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv("UV_CACHE_DIR", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setattr("lmer_cli.mounts.Path.home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr("lmer_cli.mounts.sys.platform", "darwin")
        assert resolve_host_uv_cache_dir() == tmp_path / "Library" / "Caches" / "uv"

    def test_expanduser_on_explicit_path(self, monkeypatch):
        monkeypatch.setenv("UV_CACHE_DIR", "~/my-uv-cache")
        resolved = resolve_host_uv_cache_dir()
        assert not str(resolved).startswith("~"), "tilde should have been expanded"


class TestBuildHostUvCacheMount:
    """Mount-arg construction for the host uv cache."""

    def test_mount_to_container_default_path(self):
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            args = build_host_uv_cache_mount("docker", Path("/home/user/.cache/uv"))
        assert args == ["-v", f"/home/user/.cache/uv:{CONTAINER_UV_CACHE_DIR}:rw"]

    def test_selinux_label_on_podman(self):
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=True):
            _is_selinux_enforcing.cache_clear()
            args = build_host_uv_cache_mount("podman", Path("/home/user/.cache/uv"))
        assert args[-1].endswith(":rw,z")


class TestBuildFileMounts:
    """Mount-arg construction for explicit per-file mounts (--mount-file)."""

    def test_single_ro_mount_arg_shape(self):
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            args = build_file_mounts(
                "docker",
                [FileMountSpec(Path("/home/user/.kube/config"), "/home/developer/.kube/config", "ro")],
            )
        assert args == [
            "-v",
            "/home/user/.kube/config:/home/developer/.kube/config:ro",
        ]

    def test_rw_mode_preserved(self):
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            args = build_file_mounts(
                "docker", [FileMountSpec(Path("/tmp/a"), "/etc/a", "rw")]
            )
        assert args[-1].endswith(":rw")

    def test_selinux_suffix_when_enforcing(self):
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=True):
            _is_selinux_enforcing.cache_clear()
            args = build_file_mounts(
                "podman", [FileMountSpec(Path("/tmp/a"), "/etc/a", "ro")]
            )
        assert args[-1].endswith(":ro,z")

    def test_multiple_specs_in_order(self):
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            args = build_file_mounts(
                "docker",
                [
                    FileMountSpec(Path("/tmp/a"), "/etc/a", "ro"),
                    FileMountSpec(Path("/tmp/b"), "/etc/b", "rw"),
                ],
            )
        assert args == [
            "-v", "/tmp/a:/etc/a:ro",
            "-v", "/tmp/b:/etc/b:rw",
        ]

    def test_empty_specs_no_args(self):
        assert build_file_mounts("docker", []) == []


class TestParseFileMountSpecs:
    """Validation and merge semantics for --mount-file / LMER_MOUNT_FILES."""

    @pytest.fixture()
    def host_file(self, tmp_path):
        f = tmp_path / "kubeconfig"
        f.write_text("apiVersion: v1")
        return f

    def test_valid_single_flag_defaults_ro(self, host_file):
        specs = parse_file_mount_specs([f"{host_file}:/home/developer/.kube/config"], "")
        assert specs == [
            FileMountSpec(host_file, "/home/developer/.kube/config", "ro")
        ]

    def test_valid_multi_with_modes(self, host_file, tmp_path):
        other = tmp_path / "token"
        other.write_text("secret")
        specs = parse_file_mount_specs(
            [f"{host_file}:/etc/kube:ro", f"{other}:/etc/token:rw"], ""
        )
        assert [s.mode for s in specs] == ["ro", "rw"]
        assert [s.container for s in specs] == ["/etc/kube", "/etc/token"]

    def test_tilde_expansion(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        f = tmp_path / "cred"
        f.write_text("x")
        specs = parse_file_mount_specs(["~/cred:/etc/cred"], "")
        assert specs[0].host == f

    def test_env_var_expansion(self, host_file, monkeypatch):
        monkeypatch.setenv("MY_KUBE", str(host_file))
        specs = parse_file_mount_specs(["$MY_KUBE:/etc/kube"], "")
        assert specs[0].host == host_file

    def test_env_entries_come_before_flags(self, host_file, tmp_path):
        other = tmp_path / "other"
        other.write_text("x")
        specs = parse_file_mount_specs(
            [f"{other}:/etc/b"], f"{host_file}:/etc/a"
        )
        assert [s.container for s in specs] == ["/etc/a", "/etc/b"]

    def test_env_value_splits_on_comma(self, host_file, tmp_path):
        other = tmp_path / "other"
        other.write_text("x")
        specs = parse_file_mount_specs(
            [], f"{host_file}:/etc/a, {other}:/etc/b:rw"
        )
        assert [s.container for s in specs] == ["/etc/a", "/etc/b"]
        assert specs[1].mode == "rw"

    def test_last_wins_dedup_with_warning(self, host_file, tmp_path):
        other = tmp_path / "other"
        other.write_text("x")
        with patch("lmer_cli.cli.warning") as mock_warning:
            specs = parse_file_mount_specs(
                [f"{other}:/etc/same:rw"], f"{host_file}:/etc/same"
            )
        assert len(specs) == 1
        assert specs[0].host == other
        assert specs[0].mode == "rw"
        mock_warning.assert_called_once()

    def test_missing_host_file_fails_fast(self, tmp_path):
        with pytest.raises(ValueError, match="not an existing file"):
            parse_file_mount_specs([f"{tmp_path}/nope:/etc/nope"], "")

    def test_host_directory_fails_fast(self, tmp_path):
        with pytest.raises(ValueError, match="not an existing file"):
            parse_file_mount_specs([f"{tmp_path}:/etc/dir"], "")

    def test_relative_container_path_fails_fast(self, host_file):
        with pytest.raises(ValueError, match="must be absolute"):
            parse_file_mount_specs([f"{host_file}:relative/dest"], "")

    def test_bad_mode_fails_fast(self, host_file):
        with pytest.raises(ValueError, match="must be 'ro' or 'rw'"):
            parse_file_mount_specs([f"{host_file}:/etc/kube:rwx"], "")

    def test_malformed_entry_fails_fast(self, host_file):
        with pytest.raises(ValueError, match="host:container"):
            parse_file_mount_specs([str(host_file)], "")

    def test_empty_inputs_yield_no_specs(self):
        assert parse_file_mount_specs([], "") == []
        assert parse_file_mount_specs([], " , ") == []
