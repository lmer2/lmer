#!/usr/bin/env python3
"""Tests for lmer_cli.mounts module"""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile

from lmer_cli.cli import (
    _announce_user_credential_mounts,
    conflicting_mount_destinations,
    parse_args,
    parse_dir_mount_specs,
    parse_file_mount_specs,
)
from lmer_cli.harness import HARNESSES
from lmer_cli.user_harnesses import load_user_harnesses
from lmer_cli.mounts import (
    CONTAINER_MOUNT_STAGING_DIR,
    _stage_token as _real_stage_token,
    DirMountSpec,
    FileMountSpec,
    credential_mount_links,
    format_mount_links,
    plan_credential_mounts,
    selinux_opt,
    _is_selinux_enforcing,
    build_workspace_mount,
    build_global_mount,
    build_lmer_docs_mount,
    build_host_repo_ro_mount,
    build_host_uv_cache_mount,
    build_user_mounts,
    build_checkout_mount,
    build_dir_mounts,
    build_file_mounts,
    build_release_signing_key_mount,
    build_service_mode_mounts,
    resolve_host_uv_cache_dir,
    CONTAINER_UV_CACHE_DIR,
    CONTAINER_RELEASE_SIGNING_KEY_PATH,
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

    @staticmethod
    def _no_selinuxfs(tmp_path):
        """Point the sysfs probe at a path that does not exist.

        Every ``getenforce`` case below is about the *fallback*, so selinuxfs has
        to be absent for the test to be about what it says it is. Patched rather
        than assumed: whether ``/sys/fs/selinux/enforce`` exists is a property of
        the host running the suite, and a test that reads differently on Fedora
        than on Debian is the failure mode this whole class exists to avoid.
        """
        return patch(
            "lmer_cli.runtime.SELINUX_ENFORCE_PATH", tmp_path / "no-selinuxfs"
        )

    def test_is_selinux_enforcing_reads_the_kernel_first(self, tmp_path):
        """selinuxfs answers without a subprocess — the container has no getenforce."""
        enforce = tmp_path / "enforce"
        enforce.write_text("1", encoding="utf-8")
        _is_selinux_enforcing.cache_clear()
        with patch("lmer_cli.runtime.SELINUX_ENFORCE_PATH", enforce):
            with patch(
                "lmer_cli.runtime.subprocess.run", side_effect=FileNotFoundError
            ):
                assert _is_selinux_enforcing() is True
        _is_selinux_enforcing.cache_clear()

    def test_is_selinux_enforcing_reads_permissive_from_the_kernel(self, tmp_path):
        """``0`` is permissive, and it must not fall through to the shell-out:
        a host with selinuxfs mounted has already given the answer."""
        enforce = tmp_path / "enforce"
        enforce.write_text("0\n", encoding="utf-8")
        _is_selinux_enforcing.cache_clear()
        with patch("lmer_cli.runtime.SELINUX_ENFORCE_PATH", enforce):
            with patch("lmer_cli.runtime.subprocess.run") as run:
                assert _is_selinux_enforcing() is False
                run.assert_not_called()
        _is_selinux_enforcing.cache_clear()

    def test_is_selinux_enforcing_returns_true_when_enforcing(self, tmp_path):
        """Test SELinux detection when getenforce returns Enforcing"""
        _is_selinux_enforcing.cache_clear()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Enforcing\n"
        with self._no_selinuxfs(tmp_path):
            with patch("lmer_cli.runtime.subprocess.run", return_value=mock_result):
                _is_selinux_enforcing.cache_clear()
                assert _is_selinux_enforcing() is True
        _is_selinux_enforcing.cache_clear()

    def test_is_selinux_enforcing_returns_false_when_permissive(self, tmp_path):
        """Test SELinux detection when getenforce returns Permissive"""
        _is_selinux_enforcing.cache_clear()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Permissive\n"
        with self._no_selinuxfs(tmp_path):
            with patch("lmer_cli.runtime.subprocess.run", return_value=mock_result):
                _is_selinux_enforcing.cache_clear()
                assert _is_selinux_enforcing() is False
        _is_selinux_enforcing.cache_clear()

    def test_is_selinux_enforcing_returns_false_when_disabled(self, tmp_path):
        """Test SELinux detection when getenforce returns Disabled"""
        _is_selinux_enforcing.cache_clear()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Disabled\n"
        with self._no_selinuxfs(tmp_path):
            with patch("lmer_cli.runtime.subprocess.run", return_value=mock_result):
                _is_selinux_enforcing.cache_clear()
                assert _is_selinux_enforcing() is False
        _is_selinux_enforcing.cache_clear()

    def test_is_selinux_enforcing_returns_false_when_command_not_found(self, tmp_path):
        """Test SELinux detection when getenforce command is not available"""
        _is_selinux_enforcing.cache_clear()
        with self._no_selinuxfs(tmp_path):
            with patch(
                "lmer_cli.runtime.subprocess.run", side_effect=FileNotFoundError
            ):
                _is_selinux_enforcing.cache_clear()
                assert _is_selinux_enforcing() is False
        _is_selinux_enforcing.cache_clear()


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


class TestStagedCredentialMounts:
    """User-harness credential mounts below the container home are staged (#290).

    A manifest may name a container path whose parents the image does not ship
    (``~/.local/share/opencode/auth.json``). Binding straight there makes the
    runtime create ``~/.local/share/opencode`` root-owned before any container
    process exists, and opencode's own startup ``mkdir`` of ``repos/`` beside it
    then fails with EACCES — the container is ``developer`` with
    no-new-privileges, so nothing inside can fix the ownership. So the bind
    lands under the staging directory and the declared path is delivered by the
    entrypoint as a symlink, from the pairs surfaced here.
    """

    DECLARED = "/home/developer/.local/share/acme/auth.json"
    TOKEN = "deadbeef"

    @pytest.fixture(autouse=True)
    def _pinned_stage_token(self, monkeypatch):
        """The per-launch token is random by design; pin it so the assertions
        can name the whole staged path instead of matching a prefix."""
        monkeypatch.setattr("lmer_cli.mounts._stage_token", lambda: self.TOKEN)
        # The exact-args assertions below would otherwise depend on whether
        # the launching host handed this container an ssh-agent socket (#328).
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    @property
    def stage(self):
        return f"{CONTAINER_MOUNT_STAGING_DIR}/creds/{self.TOKEN}"

    def _user_harness(self, tmp_path, monkeypatch, container_path=None):
        """A drop-in harness with one existing credential file."""
        home = tmp_path / "home"
        (home / ".acme").mkdir(parents=True)
        (home / ".acme" / "auth.json").write_text("{}")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        root = tmp_path / "harnesses"
        directory = root / "acme"
        directory.mkdir(parents=True)
        manifest = {
            "schema": 1,
            "binary": "acme",
            "credential_mounts": [{
                "host_path": ".acme/auth.json",
                "container_path": container_path or self.DECLARED,
            }],
        }
        (directory / "harness.json").write_text(json.dumps(manifest))
        (directory / "runner.sh").write_text("#!/bin/bash\nexit 0\n")
        return load_user_harnesses(root)["acme"]

    def test_user_credentials_bind_in_the_staging_area(self, tmp_path, monkeypatch):
        acme = self._user_harness(tmp_path, monkeypatch)
        to_mount, skipped = plan_credential_mounts(acme)
        assert skipped == []
        assert [m.staged for m in to_mount] == [f"{self.stage}/0"]
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            args, _ = build_user_mounts("docker", acme, plan=(to_mount, skipped))
        assert [a for a in args if a != "-v"] == [
            f"{tmp_path / 'home' / '.acme' / 'auth.json'}:{self.stage}/0:rw"
        ]
        assert self.DECLARED not in " ".join(args), (
            "the declared path is where the entrypoint puts the symlink"
        )

    def test_the_declared_path_is_surfaced_as_a_link_pair(self, tmp_path, monkeypatch):
        acme = self._user_harness(tmp_path, monkeypatch)
        to_mount, _ = plan_credential_mounts(acme)
        assert credential_mount_links(to_mount) == [
            (self.DECLARED, f"{self.stage}/0")
        ]

    def test_each_user_credential_gets_its_own_stage(self, tmp_path, monkeypatch):
        """The index is over the planned user mounts, so two credentials never
        share a destination — the runtime would bind the second over the first."""
        home = tmp_path / "home"
        (home / ".acme").mkdir(parents=True)
        for leaf in ("auth.json", "models.json"):
            (home / ".acme" / leaf).write_text("{}")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        root = tmp_path / "harnesses"
        directory = root / "acme"
        directory.mkdir(parents=True)
        (directory / "harness.json").write_text(json.dumps({
            "schema": 1,
            "binary": "acme",
            "credential_mounts": [
                {"host_path": ".acme/auth.json", "container_path": self.DECLARED},
                {
                    "host_path": ".acme/models.json",
                    "container_path": "/home/developer/.acme/models.json",
                },
            ],
        }))
        (directory / "runner.sh").write_text("#!/bin/bash\nexit 0\n")
        acme = load_user_harnesses(root)["acme"]
        to_mount, _ = plan_credential_mounts(acme)
        staged = [m.staged for m in to_mount]
        assert staged == [f"{self.stage}/0", f"{self.stage}/1"]
        assert len(set(staged)) == len(staged)

    def test_built_in_credentials_are_not_staged(self, tmp_path, monkeypatch):
        """The image ships ~/.claude developer-owned, so there is no root-owned
        parent to avoid — and a symlink hop would only add a way to lose the
        credential."""
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / ".credentials.json").write_text("{}")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        to_mount, _ = plan_credential_mounts(HARNESSES["claude"])
        assert to_mount, "the fixture credential file must be planned"
        assert all(m.staged is None for m in to_mount)
        assert credential_mount_links(to_mount) == []
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            args, _ = build_user_mounts(
                "docker", HARNESSES["claude"], plan=(to_mount, [])
            )
        assert any(
            a.endswith("/home/developer/.claude/.credentials.json:rw") for a in args
        ), args

    @pytest.mark.parametrize("declared", [
        "/etc/acme/auth.json",
        "/opt/acme/auth.json",
        # Component-wise, not a string prefix — a different directory.
        "/home/developer2/acme/auth.json",
        # The home itself is not strictly below itself.
        "/home/developer",
    ])
    def test_a_declared_path_outside_the_home_binds_directly(
        self, tmp_path, monkeypatch, declared
    ):
        acme = self._user_harness(tmp_path, monkeypatch, container_path=declared)
        to_mount, skipped = plan_credential_mounts(acme)
        assert skipped == []
        assert [m.staged for m in to_mount] == [None]
        assert credential_mount_links(to_mount) == [], (
            "nothing to link: the mount is already where the manifest asked for it"
        )
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            args, _ = build_user_mounts("docker", acme, plan=(to_mount, skipped))
        assert [a for a in args if a != "-v"] == [
            f"{tmp_path / 'home' / '.acme' / 'auth.json'}:{declared}:rw"
        ]

    def test_a_declared_path_below_the_home_still_stages(self, tmp_path, monkeypatch):
        """The control for the boundary above: one component deeper than the
        home is the case staging exists for."""
        acme = self._user_harness(
            tmp_path, monkeypatch, container_path="/home/developer/.acme/auth.json"
        )
        to_mount, _ = plan_credential_mounts(acme)
        assert [m.staged for m in to_mount] == [f"{self.stage}/0"]

    def test_the_stage_is_this_launch_s_own(self, tmp_path, monkeypatch):
        """Two plans must not name one staged path.

        A nested ``lmer`` (a ``--service`` session carries the runtime socket)
        would otherwise stage its own ``creds/0`` while the inherited pair from
        the outer launch still named that path — linking the OUTER harness's
        declared path onto the INNER harness's credential file, and first-wins
        would drop the inner harness's own pair.
        """
        # The real generator, not the pinned one — the point is that two calls
        # differ.
        monkeypatch.setattr("lmer_cli.mounts._stage_token", _real_stage_token)
        acme = self._user_harness(tmp_path, monkeypatch)
        first, _ = plan_credential_mounts(acme)
        second, _ = plan_credential_mounts(acme)
        assert first[0].staged != second[0].staged
        for planned in (first[0], second[0]):
            assert planned.staged.startswith(f"{CONTAINER_MOUNT_STAGING_DIR}/creds/")
            assert planned.staged.endswith("/0"), "the index is still per plan"

    def test_the_announce_names_the_path_the_operator_declared(
        self, tmp_path, monkeypatch, capsys
    ):
        """Staging is an implementation detail; the 🔑 line has to stay
        recognisable as the manifest entry it is warning about."""
        acme = self._user_harness(tmp_path, monkeypatch)
        to_mount, _ = plan_credential_mounts(acme)
        monkeypatch.delenv("LMER_VERBOSE", raising=False)
        _announce_user_credential_mounts(to_mount)
        out = capsys.readouterr().out
        assert "🔑 User harness acme: mounting ~/.acme/auth.json (rw)" in out
        assert CONTAINER_MOUNT_STAGING_DIR not in out


class TestMountLinkFormatting:
    """The LMER_MOUNT_LINKS value: what the entrypoint linker parses."""

    def test_pairs_are_colon_joined_and_comma_separated(self):
        assert format_mount_links("", [("/a/b", "/staged/0")]) == "/a/b:/staged/0"
        assert format_mount_links(
            "", [("/a/b", "/staged/0"), ("/c/d", "/staged/1")]
        ) == "/a/b:/staged/0,/c/d:/staged/1"

    def test_inherited_pairs_come_first_and_are_kept(self):
        """The platform stages the session directories and sets the variable on
        the child ``lmer``; this launch's credential pairs are added to that, not
        substituted for it."""
        assert format_mount_links(
            "/sessions/declared:/staged/sessions/acme", [("/a/b", "/staged/creds/0")]
        ) == "/sessions/declared:/staged/sessions/acme,/a/b:/staged/creds/0"

    def test_nothing_to_link_is_blank_not_absent(self):
        assert format_mount_links("", []) == ""

    def test_blank_entries_are_dropped(self):
        """A stray comma must not reach the linker as an empty pair."""
        assert format_mount_links(",/a/b:/staged/0,", []) == "/a/b:/staged/0"


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


class TestBuildDirMounts:
    """Mount-arg construction for explicit per-directory mounts (--mount-dir)."""

    def test_single_ro_mount_arg_shape(self):
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            args = build_dir_mounts(
                "docker",
                [DirMountSpec(Path("/home/user/data"), "/home/developer/data", "ro")],
            )
        assert args == ["-v", "/home/user/data:/home/developer/data:ro"]

    def test_rw_mode_preserved(self):
        """The transcript mount depends on this: the harness writes into it."""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            args = build_dir_mounts(
                "docker",
                [DirMountSpec(Path("/tmp/t"), "/home/developer/.claude/projects", "rw")],
            )
        assert args == ["-v", "/tmp/t:/home/developer/.claude/projects:rw"]

    def test_selinux_suffix_when_enforcing(self):
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=True):
            _is_selinux_enforcing.cache_clear()
            args = build_dir_mounts("podman", [DirMountSpec(Path("/tmp/a"), "/etc/a", "rw")])
        assert args[-1].endswith(":rw,z")

    def test_multiple_specs_in_order(self):
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            args = build_dir_mounts(
                "docker",
                [
                    DirMountSpec(Path("/tmp/a"), "/etc/a", "ro"),
                    DirMountSpec(Path("/tmp/b"), "/etc/b", "rw"),
                ],
            )
        assert args == ["-v", "/tmp/a:/etc/a:ro", "-v", "/tmp/b:/etc/b:rw"]

    def test_empty_specs_no_args(self):
        assert build_dir_mounts("docker", []) == []

    def test_file_and_dir_mounts_produce_the_same_arg_shape(self):
        """One grammar, one arg shape — the two builders share a body for it."""
        with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
            _is_selinux_enforcing.cache_clear()
            files = build_file_mounts("docker", [FileMountSpec(Path("/tmp/x"), "/etc/x", "rw")])
            dirs = build_dir_mounts("docker", [DirMountSpec(Path("/tmp/x"), "/etc/x", "rw")])
        assert files == dirs


class TestBuildReleaseSigningKeyMount:
    """Mount-arg construction for the release SSH signing key."""

    @pytest.fixture()
    def fake_home(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        return home

    def test_happy_path_single_ro_bind(self, fake_home):
        key = fake_home / ".ssh" / "lmer_release_key"
        key.parent.mkdir()
        key.write_text("PRIVATE KEY")
        with patch("pathlib.Path.home", return_value=fake_home):
            with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
                _is_selinux_enforcing.cache_clear()
                args, reason = build_release_signing_key_mount("docker", key)
        assert reason is None
        assert args == ["-v", f"{key}:{CONTAINER_RELEASE_SIGNING_KEY_PATH}:ro"]

    def test_selinux_label_on_podman(self, fake_home):
        key = fake_home / "release_key"
        key.write_text("PRIVATE KEY")
        with patch("pathlib.Path.home", return_value=fake_home):
            with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=True):
                _is_selinux_enforcing.cache_clear()
                args, reason = build_release_signing_key_mount("podman", key)
        assert reason is None
        assert args == ["-v", f"{key}:{CONTAINER_RELEASE_SIGNING_KEY_PATH}:ro,z"]

    def test_missing_file_refused(self, fake_home):
        with patch("pathlib.Path.home", return_value=fake_home):
            args, reason = build_release_signing_key_mount(
                "docker", fake_home / "nope"
            )
        assert args == []
        assert "does not exist" in reason

    def test_directory_refused(self, fake_home):
        key_dir = fake_home / ".ssh"
        key_dir.mkdir()
        with patch("pathlib.Path.home", return_value=fake_home):
            args, reason = build_release_signing_key_mount("docker", key_dir)
        assert args == []
        assert "not a regular file" in reason

    def test_out_of_home_symlink_refused(self, fake_home, tmp_path):
        outside = tmp_path / "outside-key"
        outside.write_text("PRIVATE KEY")
        link = fake_home / "release_key"
        link.symlink_to(outside)
        with patch("pathlib.Path.home", return_value=fake_home):
            args, reason = build_release_signing_key_mount("docker", link)
        assert args == []
        assert "outside the host home" in reason

    def test_in_home_symlink_allowed(self, fake_home):
        real = fake_home / "real_key"
        real.write_text("PRIVATE KEY")
        link = fake_home / "release_key"
        link.symlink_to(real)
        with patch("pathlib.Path.home", return_value=fake_home):
            with patch("lmer_cli.mounts._is_selinux_enforcing", return_value=False):
                _is_selinux_enforcing.cache_clear()
                args, reason = build_release_signing_key_mount("docker", link)
        assert reason is None
        assert args == ["-v", f"{link}:{CONTAINER_RELEASE_SIGNING_KEY_PATH}:ro"]


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


class TestParseDirMountSpecs:
    """Validation and merge semantics for --mount-dir / LMER_MOUNT_DIRS.

    The same grammar as --mount-file, so the same properties are asserted: a
    shared parser body is only worth anything if both flags are held to it.
    """

    @pytest.fixture()
    def host_dir(self, tmp_path):
        d = tmp_path / "transcripts"
        d.mkdir()
        return d

    def test_valid_single_flag_defaults_ro(self, host_dir):
        specs = parse_dir_mount_specs([f"{host_dir}:/home/developer/data"], "")
        assert specs == [DirMountSpec(host_dir, "/home/developer/data", "ro")]

    def test_rw_mode_is_honoured(self, host_dir):
        specs = parse_dir_mount_specs([f"{host_dir}:/etc/data:rw"], "")
        assert specs[0].mode == "rw"

    def test_tilde_expansion(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "data").mkdir()
        specs = parse_dir_mount_specs(["~/data:/etc/data"], "")
        assert specs[0].host == tmp_path / "data"

    def test_env_var_expansion(self, host_dir, monkeypatch):
        monkeypatch.setenv("MY_DATA", str(host_dir))
        specs = parse_dir_mount_specs(["$MY_DATA:/etc/data"], "")
        assert specs[0].host == host_dir

    def test_env_entries_come_before_flags(self, host_dir, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        specs = parse_dir_mount_specs([f"{other}:/etc/b"], f"{host_dir}:/etc/a")
        assert [s.container for s in specs] == ["/etc/a", "/etc/b"]

    def test_env_value_splits_on_comma(self, host_dir, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        specs = parse_dir_mount_specs([], f"{host_dir}:/etc/a, {other}:/etc/b:rw")
        assert [s.container for s in specs] == ["/etc/a", "/etc/b"]
        assert specs[1].mode == "rw"

    def test_last_wins_dedup_with_warning(self, host_dir, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        with patch("lmer_cli.cli.warning") as mock_warning:
            specs = parse_dir_mount_specs(
                [f"{other}:/etc/same:rw"], f"{host_dir}:/etc/same"
            )
        assert len(specs) == 1
        assert specs[0].host == other
        assert specs[0].mode == "rw"
        mock_warning.assert_called_once()

    def test_missing_host_dir_fails_fast(self, tmp_path):
        """lmer does not create it: a typo must be an error, not an empty mount."""
        with pytest.raises(ValueError, match="not an existing directory"):
            parse_dir_mount_specs([f"{tmp_path}/nope:/etc/nope"], "")

    def test_host_file_fails_fast(self, tmp_path):
        """A file where a directory was asked for is the mistake worth catching."""
        f = tmp_path / "a-file"
        f.write_text("x")
        with pytest.raises(ValueError, match="not an existing directory"):
            parse_dir_mount_specs([f"{f}:/etc/dir"], "")

    def test_relative_container_path_fails_fast(self, host_dir):
        with pytest.raises(ValueError, match="must be absolute"):
            parse_dir_mount_specs([f"{host_dir}:relative/dest"], "")

    def test_bad_mode_fails_fast(self, host_dir):
        with pytest.raises(ValueError, match="must be 'ro' or 'rw'"):
            parse_dir_mount_specs([f"{host_dir}:/etc/data:rwx"], "")

    def test_malformed_entry_fails_fast(self, host_dir):
        with pytest.raises(ValueError, match="host:container"):
            parse_dir_mount_specs([str(host_dir)], "")

    def test_errors_name_the_source_the_entry_came_from(self, tmp_path):
        """A stale .env entry has to be traceable to the .env, not to the flag."""
        with pytest.raises(ValueError, match="LMER_MOUNT_DIRS"):
            parse_dir_mount_specs([], f"{tmp_path}/nope:/etc/nope")
        with pytest.raises(ValueError, match="--mount-dir"):
            parse_dir_mount_specs([f"{tmp_path}/nope:/etc/nope"], "")

    def test_empty_inputs_yield_no_specs(self):
        assert parse_dir_mount_specs([], "") == []
        assert parse_dir_mount_specs([], " , ") == []


class TestMountDirFlagWiring:
    """The flag itself: repeatable, and it names its env var like every other."""

    def test_mount_dir_is_repeatable(self):
        ns, _rest = parse_args(
            ["chat", "repo", "--mount-dir", "/a:/x", "--mount-dir", "/b:/y"]
        )
        assert ns.mount_dir == ["/a:/x", "/b:/y"], (
            "without action=append only the last entry would reach the parser"
        )

    def test_mount_dir_defaults_to_nothing(self):
        ns, _rest = parse_args(["chat", "repo"])
        assert ns.mount_dir is None

    def test_mount_dir_does_not_capture_a_container_command(self):
        """`--` ends lmer's own argv; the platform's mount must precede it."""
        ns, rest = parse_args(["chat", "repo", "--mount-dir", "/a:/x", "--", "echo", "hi"])
        assert ns.mount_dir == ["/a:/x"]
        assert rest == ["echo", "hi"]

    def test_mount_dir_help_names_its_env_var(self, capsys):
        """The documented convention: a flag's help points at its env var."""
        with pytest.raises(SystemExit):
            parse_args(["--help"])
        help_text = capsys.readouterr().out
        assert "--mount-dir" in help_text
        assert "LMER_MOUNT_DIRS" in help_text


class TestConflictingMountDestinations:
    """A file mount and a dir mount on one destination: the runtime refuses it."""

    def test_a_shared_destination_is_reported(self, tmp_path):
        f = tmp_path / "f"
        f.write_text("x")
        d = tmp_path / "d"
        d.mkdir()
        clashes = conflicting_mount_destinations(
            parse_file_mount_specs([f"{f}:/etc/same"], ""),
            parse_dir_mount_specs([f"{d}:/etc/same:rw"], ""),
        )
        assert clashes == ["/etc/same"]

    def test_distinct_destinations_do_not_clash(self, tmp_path):
        f = tmp_path / "f"
        f.write_text("x")
        d = tmp_path / "d"
        d.mkdir()
        assert conflicting_mount_destinations(
            parse_file_mount_specs([f"{f}:/etc/a"], ""),
            parse_dir_mount_specs([f"{d}:/etc/b"], ""),
        ) == []

    def test_a_nested_destination_is_not_a_clash(self, tmp_path):
        """Only an identical destination is refused by the runtime.

        A file mounted *inside* a mounted directory is exactly what the claude
        credential mounts do beside the transcript dir, so it must stay legal.
        """
        f = tmp_path / "f"
        f.write_text("x")
        d = tmp_path / "d"
        d.mkdir()
        assert conflicting_mount_destinations(
            parse_file_mount_specs([f"{f}:/home/developer/.claude/.credentials.json"], ""),
            parse_dir_mount_specs([f"{d}:/home/developer/.claude:rw"], ""),
        ) == []

    def test_empty_inputs_never_clash(self):
        assert conflicting_mount_destinations([], []) == []
