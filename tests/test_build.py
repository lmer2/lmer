"""Tests for lmer_cli.build — container image provisioning."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

from lmer_cli.build import (
    DEFAULT_IMAGE,
    DEV_IMAGE_BUILD_INPUTS,
    build_image,
    build_image_local,
    delete_image,
    ensure_image,
    image_exists,
    pull_image,
    resolve_image_tag,
)


def _make_dev_checkout(root: Path) -> None:
    """Lay out a minimal developer checkout: .git plus every declared build input."""
    (root / ".git").mkdir()
    for rel in DEV_IMAGE_BUILD_INPUTS:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"content of {rel}\n")


class TestImageExists:
    """Test image existence detection."""

    def test_returns_true_when_image_found(self):
        with patch("lmer_cli.build.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert image_exists("docker", "myimage:latest") is True
            mock_run.assert_called_once_with(
                ["docker", "image", "inspect", "myimage:latest"],
                capture_output=True,
                timeout=30,
            )

    def test_returns_false_when_image_not_found(self):
        with patch("lmer_cli.build.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert image_exists("docker", "myimage:latest") is False

    def test_returns_false_on_timeout(self):
        with patch("lmer_cli.build.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=30)
            assert image_exists("docker", "myimage:latest") is False

    def test_uses_correct_runtime(self):
        with patch("lmer_cli.build.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            image_exists("podman", "myimage:latest")
            assert mock_run.call_args[0][0][0] == "podman"


class TestBuildImageLocal:
    """Test local image building."""

    def test_passes_correct_build_args(self, tmp_path):
        with patch("lmer_cli.build.subprocess.call", return_value=0) as mock_call, \
             patch("lmer_cli.build.os.getuid", return_value=1000), \
             patch("lmer_cli.build.os.getgid", return_value=1000):
            build_image_local("docker", "myimage:fips", tmp_path)
            cmd = mock_call.call_args[0][0]
            assert cmd[0] == "docker"
            assert cmd[1] == "build"
            assert "--build-arg" in cmd
            assert "BUILD_UID=1000" in cmd
            assert "BUILD_GID=1000" in cmd
            assert "-t" in cmd
            assert "myimage:fips" in cmd
            assert "-f" in cmd
            assert "Containerfile" in cmd

    def test_uses_repo_root_as_cwd(self, tmp_path):
        with patch("lmer_cli.build.subprocess.call", return_value=0) as mock_call, \
             patch("lmer_cli.build.os.getuid", return_value=1000), \
             patch("lmer_cli.build.os.getgid", return_value=1000):
            build_image_local("docker", "myimage:fips", tmp_path)
            assert mock_call.call_args[1]["cwd"] == str(tmp_path)

    def test_returns_exit_code(self, tmp_path):
        with patch("lmer_cli.build.subprocess.call", return_value=1) as mock_call, \
             patch("lmer_cli.build.os.getuid", return_value=1000), \
             patch("lmer_cli.build.os.getgid", return_value=1000):
            assert build_image_local("docker", "myimage:fips", tmp_path) == 1

    def test_includes_pull_flag_by_default(self, tmp_path):
        with patch("lmer_cli.build.subprocess.call", return_value=0) as mock_call, \
             patch("lmer_cli.build.os.getuid", return_value=1000), \
             patch("lmer_cli.build.os.getgid", return_value=1000):
            build_image_local("docker", "myimage:v1", tmp_path)
            cmd = mock_call.call_args[0][0]
            assert "--pull" in cmd

    def test_omits_pull_flag_when_disabled(self, tmp_path):
        with patch("lmer_cli.build.subprocess.call", return_value=0) as mock_call, \
             patch("lmer_cli.build.os.getuid", return_value=1000), \
             patch("lmer_cli.build.os.getgid", return_value=1000):
            build_image_local("docker", "myimage:v1", tmp_path, pull=False)
            cmd = mock_call.call_args[0][0]
            assert "--pull" not in cmd


class TestPullImage:
    """Test image pulling from registry."""

    def test_requires_registry(self):
        """Should return error when no registry provided."""
        result = pull_image("docker", "lmer")
        assert result == 1

    def test_custom_registry(self):
        with patch("lmer_cli.build.subprocess.call", return_value=0) as mock_call:
            pull_image("docker", "myimage:fips", registry="my.registry.com/group/project")
            calls = mock_call.call_args_list
            assert calls[0][0][0] == ["docker", "pull", "my.registry.com/group/project:fips"]

    def test_tags_after_pull(self):
        with patch("lmer_cli.build.subprocess.call", return_value=0) as mock_call:
            pull_image("docker", "myimage:fips", registry="my.registry.com/repo")
            calls = mock_call.call_args_list
            assert len(calls) == 2
            assert calls[1][0][0] == [
                "docker", "tag",
                "my.registry.com/repo:fips",
                "myimage:fips",
            ]

    def test_skips_tag_when_names_match(self):
        """When image name matches registry image, no tag command needed."""
        with patch("lmer_cli.build.subprocess.call", return_value=0) as mock_call:
            pull_image("docker", "my.registry.com/repo:latest", registry="my.registry.com/repo")
            calls = mock_call.call_args_list
            assert len(calls) == 1

    def test_returns_failure_on_pull_error(self):
        with patch("lmer_cli.build.subprocess.call", return_value=1):
            assert pull_image("docker", "myimage:fips", registry="my.registry.com/repo") == 1

    def test_pulls_with_tag_from_image_name(self):
        """When image has a tag, pull that tag from registry instead of :latest."""
        with patch("lmer_cli.build.subprocess.call", return_value=0) as mock_call:
            pull_image("docker", "lmer:abc1234", registry="my.registry.com/repo")
            calls = mock_call.call_args_list
            assert calls[0][0][0] == ["docker", "pull", "my.registry.com/repo:abc1234"]

    def test_registry_port_not_confused_with_tag(self):
        """Registry port in image name should not be mistaken for a tag."""
        with patch("lmer_cli.build.subprocess.call", return_value=0) as mock_call:
            pull_image("docker", "registry.example.com:5000/lmer", registry="my.registry.com/repo")
            calls = mock_call.call_args_list
            assert calls[0][0][0] == ["docker", "pull", "my.registry.com/repo:latest"]


class TestResolveImageTag:
    """Test image tag resolution based on install method."""

    def test_developer_mode_uses_git_sha(self, tmp_path):
        """Legacy path: a checkout without a Containerfile tags by git SHA."""
        (tmp_path / ".git").mkdir()
        mock_result = MagicMock(returncode=0, stdout="a1b2c3d\n")
        with patch("lmer_cli.build.subprocess.run", return_value=mock_result):
            result = resolve_image_tag(tmp_path)
        assert result == f"{DEFAULT_IMAGE}:a1b2c3d"

    def test_developer_mode_with_containerfile_uses_input_hash(self, tmp_path):
        """A buildable checkout tags by input hash, never touching git."""
        _make_dev_checkout(tmp_path)
        with patch("lmer_cli.build.subprocess.run", side_effect=AssertionError("git must not be called")):
            result = resolve_image_tag(tmp_path)
        assert result is not None
        prefix = f"{DEFAULT_IMAGE}:dev-"
        assert result.startswith(prefix)
        digest = result[len(prefix):]
        assert len(digest) == 12
        int(digest, 16)

    def test_input_hash_stable_across_unrelated_source_edit(self, tmp_path):
        """Editing non-input source (a new commit) must not change the tag —
        that stability is what lets the warmed keep-id layer be reused."""
        _make_dev_checkout(tmp_path)
        before = resolve_image_tag(tmp_path)
        src = tmp_path / "src" / "lmer_cli"
        src.mkdir(parents=True)
        (src / "cli.py").write_text("changed = True\n")
        assert resolve_image_tag(tmp_path) == before

    def test_input_hash_changes_when_input_changes(self, tmp_path):
        _make_dev_checkout(tmp_path)
        before = resolve_image_tag(tmp_path)
        (tmp_path / "uv.lock").write_text("a dependency moved\n")
        assert resolve_image_tag(tmp_path) != before

    def test_input_hash_deterministic_when_input_absent(self, tmp_path):
        """Two checkouts missing the same input resolve to the same tag."""
        a = tmp_path / "a"
        b = tmp_path / "b"
        for root in (a, b):
            root.mkdir()
            _make_dev_checkout(root)
            (root / ".mcp.json").unlink()
        assert resolve_image_tag(a) == resolve_image_tag(b)

    def test_input_hash_directory_entry_tracks_contents(self, tmp_path):
        """MR !249 review (build.py:57): a directory entry must contribute its
        contents, not a constant — otherwise extending the input list with a
        directory silently stops moving the tag and stale images get reused."""
        root = tmp_path / "dir"
        root.mkdir()
        _make_dev_checkout(root)
        (root / ".mcp.json").unlink()
        d = root / ".mcp.json"
        d.mkdir()
        (d / "a.json").write_text("one")
        tag_v1 = resolve_image_tag(root)

        absent = tmp_path / "absent"
        absent.mkdir()
        _make_dev_checkout(absent)
        (absent / ".mcp.json").unlink()
        assert tag_v1 != resolve_image_tag(absent)

        (d / "a.json").write_text("two")
        tag_v2 = resolve_image_tag(root)
        assert tag_v2 != tag_v1

        (d / "b.json").write_text("added")
        tag_v3 = resolve_image_tag(root)
        assert tag_v3 not in (tag_v1, tag_v2)


class TestDevImageInputCoverage:
    """MR !249 review (build.py:31): the input list's completeness invariant —
    every file baked into the image and read at runtime unshadowed is either
    hashed or dev-mounted — is mechanical for the Containerfile's explicit
    COPY/ADD sources, so pin it. (`COPY . .`'s unmounted remainder was
    audited by hand in the review; explicit copies are the drift surface.)"""

    def test_every_explicit_copy_source_is_hashed_or_mounted(self, tmp_path):
        import shlex

        from lmer_cli.mounts import build_global_mount

        containerfile = Path(__file__).parent.parent / "Containerfile"
        sources = []
        for line in containerfile.read_text().splitlines():
            stripped = line.strip()
            if not stripped.startswith(("COPY ", "ADD ")):
                continue
            tokens = [t for t in shlex.split(stripped)[1:] if not t.startswith("--")]
            sources += [t for t in tokens[:-1] if t != "."]
        assert sources, "no explicit COPY/ADD sources parsed — Containerfile moved?"

        # Materialize each source in a scratch repo and ask build_global_mount
        # itself which paths dev mode mounts, so this cannot drift from the
        # real mount list.
        for src in sources:
            path = tmp_path / src
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
        mounted_hosts = [
            a.split(":", 1)[0]
            for a in build_global_mount("docker", tmp_path)
            if a != "-v"
        ]
        mounted_rels = {str(Path(m).relative_to(tmp_path)) for m in mounted_hosts}

        uncovered = [
            src
            for src in sources
            if src not in DEV_IMAGE_BUILD_INPUTS
            and src not in mounted_rels
            and src.split("/", 1)[0] not in mounted_rels
        ]
        assert not uncovered, (
            f"Containerfile COPY/ADD source(s) neither hashed into "
            f"DEV_IMAGE_BUILD_INPUTS nor shadowed by a build_global_mount "
            f"path: {uncovered} — a dev-mode session would run a stale baked "
            f"copy until an unrelated input moves the image tag (issue #351)"
        )

    def test_pypi_install_uses_version(self):
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = None
        with patch("lmer_cli.build.importlib.metadata.distribution", return_value=mock_dist), \
             patch("lmer_cli.build.importlib.metadata.version", return_value="0.1.0"):
            result = resolve_image_tag(None)
        assert result == f"{DEFAULT_IMAGE}:0.1.0"

    def test_git_install_uses_pep610_commit(self):
        """Git install resolves to lmer:{commit_sha[:7]} from direct_url.json."""
        direct_url = json.dumps({
            "url": "https://gitlab.example.com/agents/global.git",
            "vcs_info": {"vcs": "git", "commit_id": "abc123def456789"},
        })
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = direct_url
        with patch("lmer_cli.build.importlib.metadata.distribution", return_value=mock_dist):
            result = resolve_image_tag(None)
        assert result == f"{DEFAULT_IMAGE}:abc123d"

    def test_returns_none_when_all_detection_fails(self):
        with patch("lmer_cli.build.importlib.metadata.distribution", side_effect=Exception("no dist")), \
             patch("lmer_cli.build.importlib.metadata.version", side_effect=Exception("no version")):
            result = resolve_image_tag(None)
        assert result is None

    def test_git_failure_falls_through_to_metadata(self, tmp_path):
        """When git rev-parse fails, falls through to metadata resolution."""
        (tmp_path / ".git").mkdir()
        mock_result = MagicMock(returncode=128, stdout="")
        with patch("lmer_cli.build.subprocess.run", return_value=mock_result), \
             patch("lmer_cli.build.importlib.metadata.distribution", side_effect=Exception), \
             patch("lmer_cli.build.importlib.metadata.version", return_value="1.2.3"):
            result = resolve_image_tag(tmp_path)
        assert result == f"{DEFAULT_IMAGE}:1.2.3"

    def test_no_git_dir_skips_git_check(self, tmp_path):
        """When repo_root has no .git dir, skips git and uses metadata."""
        with patch("lmer_cli.build.importlib.metadata.distribution", side_effect=Exception), \
             patch("lmer_cli.build.importlib.metadata.version", return_value="2.0.0"):
            result = resolve_image_tag(tmp_path)
        assert result == f"{DEFAULT_IMAGE}:2.0.0"


class TestEnsureImage:
    """Test the ensure_image orchestrator."""

    def test_returns_available_when_image_already_exists(self):
        with patch("lmer_cli.build.image_exists", return_value=True) as mock_exists:
            assert ensure_image("docker", "myimage:fips", None).available is True
            mock_exists.assert_called_once()

    def test_does_not_build_or_pull_when_image_exists(self):
        with patch("lmer_cli.build.image_exists", return_value=True), \
             patch("lmer_cli.build.build_image_local") as mock_build, \
             patch("lmer_cli.build.pull_image") as mock_pull:
            ensure_image("docker", "myimage:fips", Path("/repo"))
            mock_build.assert_not_called()
            mock_pull.assert_not_called()

    def test_builds_locally_in_developer_mode(self, tmp_path):
        (tmp_path / "Containerfile").touch()
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.build_image_local", return_value=0) as mock_build:
            assert ensure_image("docker", "myimage:fips", tmp_path).available is True
            mock_build.assert_called_once_with("docker", "myimage:fips", tmp_path)

    def test_pulls_from_registry_when_no_containerfile(self, tmp_path):
        """When repo_root exists but has no Containerfile, pull from registry."""
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.build_image_local") as mock_build, \
             patch("lmer_cli.build.pull_image", return_value=0) as mock_pull, \
             patch.dict("os.environ", {"LMER_REGISTRY": "my.registry.com/repo"}):
            assert ensure_image("docker", "myimage:fips", tmp_path).available is True
            mock_build.assert_not_called()
            mock_pull.assert_called_once()

    def test_pulls_from_registry_in_installed_mode(self):
        """When repo_root is None (uv tool install), pull from registry."""
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.pull_image", return_value=0) as mock_pull, \
             patch.dict("os.environ", {"LMER_REGISTRY": "my.registry.com/repo"}):
            assert ensure_image("docker", "myimage:fips", None).available is True
            mock_pull.assert_called_once()

    def test_pull_uses_custom_registry(self):
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.pull_image", return_value=0) as mock_pull, \
             patch.dict("os.environ", {"LMER_REGISTRY": "custom.reg.io/foo/bar"}):
            assert ensure_image("docker", "myimage:fips", None).available is True
            mock_pull.assert_called_once_with("docker", "myimage:fips", "custom.reg.io/foo/bar")

    def test_returns_false_when_pull_fails(self):
        """When registry pull fails, return False."""
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.pull_image", return_value=1), \
             patch.dict("os.environ", {"LMER_REGISTRY": "my.registry.com/repo"}):
            assert ensure_image("docker", "myimage:fips", None).available is False

    def test_pull_uses_default_registry_when_lmer_registry_unset(self):
        """Without LMER_REGISTRY, fall back to the project's default GHCR registry."""
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.pull_image", return_value=0) as mock_pull, \
             patch.dict("os.environ", {}, clear=True):
            assert ensure_image("docker", "myimage:fips", None).available is True
            mock_pull.assert_called_once_with("docker", "myimage:fips", "ghcr.io/lmer2/lmer")

    def test_respects_skip_build_flag(self):
        with patch("lmer_cli.build.image_exists", return_value=False):
            assert ensure_image("docker", "myimage:fips", Path("/repo"), skip_build=True).available is False

    def test_respects_lmer_no_auto_build_env(self):
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch.dict("os.environ", {"LMER_NO_AUTO_BUILD": "1"}):
            assert ensure_image("docker", "myimage:fips", Path("/repo")).available is False

    def test_returns_false_on_build_failure(self, tmp_path):
        (tmp_path / "Containerfile").touch()
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.build_image_local", return_value=1):
            assert ensure_image("docker", "myimage:fips", tmp_path).available is False

    def test_returns_false_on_pull_failure(self):
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.pull_image", return_value=1), \
             patch.dict("os.environ", {"LMER_REGISTRY": "my.registry.com/repo"}):
            assert ensure_image("docker", "myimage:fips", None).available is False

    # --- materialized: did THIS call create the local image? (issue #351)
    # It is what gates the cold-keep-id-remap launch notice, so each arm of
    # ensure_image has to report it honestly.

    def test_existing_image_is_not_materialized(self):
        with patch("lmer_cli.build.image_exists", return_value=True):
            assert ensure_image("podman", "myimage:fips", None).materialized is False

    def test_local_build_materializes(self, tmp_path):
        (tmp_path / "Containerfile").touch()
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.build_image_local", return_value=0):
            assert ensure_image("podman", "myimage:fips", tmp_path).materialized is True

    def test_registry_pull_materializes(self):
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.pull_image", return_value=0), \
             patch.dict("os.environ", {"LMER_REGISTRY": "my.registry.com/repo"}):
            assert ensure_image("podman", "myimage:fips", None).materialized is True

    def test_failed_build_does_not_materialize(self, tmp_path):
        (tmp_path / "Containerfile").touch()
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.build_image_local", return_value=1):
            result = ensure_image("podman", "myimage:fips", tmp_path)
            assert (result.available, result.materialized) == (False, False)


class TestBuildImage:
    """Test the build_image orchestrator (lmer build)."""

    def test_builds_locally_with_containerfile(self, tmp_path):
        (tmp_path / "Containerfile").touch()
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.build_image_local", return_value=0) as mock_build, \
             patch("lmer_cli.build.delete_image") as mock_delete:
            assert build_image("docker", "img:v1", tmp_path) is True
            mock_build.assert_called_once()
            mock_delete.assert_not_called()

    def test_force_deletes_existing_image(self, tmp_path):
        (tmp_path / "Containerfile").touch()
        with patch("lmer_cli.build.image_exists", return_value=True), \
             patch("lmer_cli.build.delete_image", return_value=True) as mock_delete, \
             patch("lmer_cli.build.build_image_local", return_value=0):
            assert build_image("docker", "img:v1", tmp_path, force=True) is True
            mock_delete.assert_called_once_with("docker", "img:v1")

    def test_no_force_skips_delete(self, tmp_path):
        (tmp_path / "Containerfile").touch()
        with patch("lmer_cli.build.image_exists", return_value=True), \
             patch("lmer_cli.build.delete_image") as mock_delete, \
             patch("lmer_cli.build.build_image_local", return_value=0):
            assert build_image("docker", "img:v1", tmp_path) is True
            mock_delete.assert_not_called()

    def test_force_delete_failure_returns_false(self, tmp_path):
        (tmp_path / "Containerfile").touch()
        with patch("lmer_cli.build.image_exists", return_value=True), \
             patch("lmer_cli.build.delete_image", return_value=False), \
             patch("lmer_cli.build.build_image_local") as mock_build:
            assert build_image("docker", "img:v1", tmp_path, force=True) is False
            mock_build.assert_not_called()

    def test_passes_pull_true_by_default(self, tmp_path):
        (tmp_path / "Containerfile").touch()
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.build_image_local", return_value=0) as mock_build:
            build_image("docker", "img:v1", tmp_path)
            mock_build.assert_called_once_with(
                "docker", "img:v1", tmp_path, pull=True,
                update_claude=False, update_harnesses=None,
            )

    def test_passes_pull_false_through(self, tmp_path):
        (tmp_path / "Containerfile").touch()
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.build_image_local", return_value=0) as mock_build:
            build_image("docker", "img:v1", tmp_path, pull=False)
            mock_build.assert_called_once_with(
                "docker", "img:v1", tmp_path, pull=False,
                update_claude=False, update_harnesses=None,
            )

    def test_returns_false_when_no_containerfile(self, tmp_path):
        with patch("lmer_cli.build.image_exists", return_value=False):
            assert build_image("docker", "img:v1", tmp_path) is False

    def test_returns_false_on_build_failure(self, tmp_path):
        (tmp_path / "Containerfile").touch()
        with patch("lmer_cli.build.image_exists", return_value=False), \
             patch("lmer_cli.build.build_image_local", return_value=1):
            assert build_image("docker", "img:v1", tmp_path) is False


class TestBuildProvenance:
    """Every image build bakes its commit (BUILD_INFO / LMER_BUILD_COMMIT) so
    sessions can answer 'what code am I running?' without symbol-grepping."""

    def test_local_build_passes_commit_build_arg(self, tmp_path):
        with patch("lmer_cli.build.subprocess.call", return_value=0) as mock_call, \
             patch("lmer_cli.build._build_provenance", return_value="abc1234 (feat/x) built=T"):
            build_image_local("docker", "myimage:abc1234", tmp_path)
            cmd = mock_call.call_args[0][0]
        assert "--build-arg" in cmd
        assert "LMER_BUILD_COMMIT=abc1234 (feat/x) built=T" in cmd

    def test_checkout_commit_none_outside_git(self, tmp_path):
        from lmer_cli.build import checkout_commit
        assert checkout_commit(tmp_path) is None
        assert checkout_commit(None) is None

    def test_checkout_commit_clean_and_dirty(self, tmp_path):
        from lmer_cli.build import checkout_commit
        import subprocess as sp
        sp.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        sp.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True, capture_output=True)
        sp.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "f.txt").write_text("x")
        sp.run(["git", "add", "f.txt"], cwd=tmp_path, check=True, capture_output=True)
        sp.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
        clean = checkout_commit(tmp_path)
        assert clean and "-dirty" not in clean
        (tmp_path / "f.txt").write_text("changed")
        assert checkout_commit(tmp_path) == f"{clean}-dirty"
