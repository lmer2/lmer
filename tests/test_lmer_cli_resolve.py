#!/usr/bin/env python3
"""Tests for lmer_cli.resolve module"""

import os
import pytest
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

from lmer_cli.resolve import (
    ResolveError,
    _git,
    get_remote_url,
    is_likely_url,
    normalize_repo_url,
)


class TestGitCommand:
    """Test git command execution wrapper"""

    def test_git_command_success(self):
        """Test successful git command execution"""
        rc, output = _git("--version")

        assert rc == 0
        assert "git version" in output.lower()

    def test_git_command_failure(self):
        """Test failed git command returns non-zero"""
        with tempfile.TemporaryDirectory() as tmpdir:
            rc, output = _git("rev-parse", "--git-dir", cwd=Path(tmpdir))

            assert rc != 0

    def test_git_command_with_cwd(self):
        """Test git command with working directory"""
        # Use current repo as test subject
        cwd = Path.cwd()
        rc, output = _git("rev-parse", "--show-toplevel", cwd=cwd)

        # Should succeed if we're in a git repo
        if rc == 0:
            assert output  # Should have some output

    def test_git_not_found(self):
        """Test handling when git is not in PATH"""
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            rc, output = _git("--version")

            assert rc == 127
            assert "git not found" in output


class TestGetRemoteUrl:
    """Test git remote URL retrieval"""

    def test_get_remote_url_in_non_git_dir(self):
        """Test returns None for non-git directory"""
        with tempfile.TemporaryDirectory() as tmpdir:
            url, remotes = get_remote_url(Path(tmpdir))

            assert url is None
            assert remotes == []

    def test_get_remote_url_in_git_repo(self):
        """Test retrieves remote URL from git repository"""
        # Test in current repo (if it is one)
        cwd = Path.cwd()
        rc, _ = _git("rev-parse", "--git-dir", cwd=cwd)

        if rc == 0:  # We're in a git repo
            url, remotes = get_remote_url(cwd)

            # Should have at least one remote or be empty
            assert isinstance(remotes, list)
            if remotes:
                assert isinstance(url, str) or url is None

    def test_get_remote_url_specific_remote(self):
        """Test requesting specific remote name"""
        # Test in current repo (if it has origin)
        cwd = Path.cwd()
        rc, _ = _git("rev-parse", "--git-dir", cwd=cwd)

        if rc == 0:  # We're in a git repo
            url, remotes = get_remote_url(cwd, "origin")

            if "origin" in remotes:
                assert url is not None
            else:
                assert url is None

    def test_get_remote_url_invalid_remote(self):
        """Test requesting non-existent remote"""
        cwd = Path.cwd()
        rc, _ = _git("rev-parse", "--git-dir", cwd=cwd)

        if rc == 0:  # We're in a git repo
            url, remotes = get_remote_url(cwd, "nonexistent-remote-12345")

            assert url is None
            assert isinstance(remotes, list)


class TestIsLikelyUrl:
    """Test URL pattern detection"""

    def test_is_likely_url_https(self):
        """Test HTTPS URLs detected"""
        assert is_likely_url("https://github.com/user/repo") is True

    def test_is_likely_url_http(self):
        """Test HTTP URLs detected"""
        assert is_likely_url("http://github.com/user/repo") is True

    def test_is_likely_url_file_protocol(self):
        """Test file:/// protocol detected"""
        assert is_likely_url("file:///home/user/repo") is True

    def test_is_likely_url_ssh_protocol(self):
        """Test ssh:// protocol detected"""
        assert is_likely_url("ssh://git@github.com/user/repo") is True

    def test_is_likely_url_ssh_shorthand(self):
        """Test SSH shorthand format detected"""
        assert is_likely_url("git@github.com:user/repo") is True

    def test_is_likely_url_local_path(self):
        """Test local paths not detected as URLs"""
        assert is_likely_url("/home/user/repo") is False
        assert is_likely_url("./local/repo") is False
        assert is_likely_url("../parent/repo") is False

    def test_is_likely_url_relative_path(self):
        """Test relative paths not detected as URLs"""
        assert is_likely_url("src/main") is False

    def test_is_likely_url_empty_string(self):
        """Test empty string not detected as URL"""
        assert is_likely_url("") is False


class TestNormalizeRepoUrl:
    """Test repository URL normalization"""

    def test_normalize_https_url(self):
        """Test HTTPS URL passes through"""
        url, path = normalize_repo_url("https://github.com/user/repo", Path.cwd(), None)

        assert url == "https://github.com/user/repo"
        assert path is None

    def test_normalize_ssh_url(self):
        """Test SSH URL passes through"""
        url, path = normalize_repo_url("git@github.com:user/repo", Path.cwd(), None)

        assert url == "git@github.com:user/repo"
        assert path is None

    def test_normalize_local_path(self):
        """Test local git repository path"""
        # Create a temporary git repo
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir) / "test-repo"
            repo_path.mkdir()

            # Initialize git repo and add a remote
            subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/test/repo.git"],
                cwd=repo_path,
                capture_output=True
            )

            url, path = normalize_repo_url(str(repo_path), Path.cwd(), None)

            # Should return the remote URL and the local path
            assert url == "https://github.com/test/repo.git"
            assert path == repo_path

    def test_normalize_non_git_local_path_raises(self):
        """Test non-git local path raises error"""
        with tempfile.TemporaryDirectory() as tmpdir:
            non_git_path = Path(tmpdir) / "not-a-repo"
            non_git_path.mkdir()

            with pytest.raises(ResolveError):
                normalize_repo_url(str(non_git_path), Path.cwd(), None)

    def test_normalize_infers_from_cwd(self):
        """Test inferring repo URL from current directory"""
        # Test in current repo
        cwd = Path.cwd()
        rc, _ = _git("rev-parse", "--git-dir", cwd=cwd)

        if rc == 0:  # We're in a git repo
            # Test with empty string to trigger inference
            try:
                url, path = normalize_repo_url("", cwd, None)

                # Should either find a URL or determine it's local
                assert url is not None or path is not None
            except ResolveError:
                # If there are no remotes, this is expected
                pass

    def test_normalize_with_specific_remote(self):
        """Test using specific remote name"""
        cwd = Path.cwd()
        rc, _ = _git("rev-parse", "--git-dir", cwd=cwd)

        if rc == 0:  # We're in a git repo
            try:
                url, path = normalize_repo_url("", cwd, "origin")

                # If origin exists, should get URL
                if url:
                    assert isinstance(url, str)
            except ResolveError:
                # If origin doesn't exist, this is expected
                pass

    def test_normalize_empty_string_non_git_raises(self):
        """Test empty string in non-git directory raises error"""
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(ResolveError):
                normalize_repo_url("", Path(tmpdir), None)


class TestRemapTaskdefToContainer:
    """_remap_taskdef_to_container — issue #80: the built-in taskdef resolved
    on the HOST used to pass its host path into the container verbatim."""

    def test_external_mount_remap(self, tmp_path):
        from lmer_cli.cli import _remap_taskdef_to_container

        host = tmp_path / "core-tasks"
        (host / "develop").mkdir(parents=True)
        root, tdir, instr = _remap_taskdef_to_container(
            host / "develop", [(host, "/Agents/taskdefs/0")], tmp_path / "repo"
        )
        assert root == "/Agents/taskdefs/0"
        assert tdir == "/Agents/taskdefs/0/develop"
        assert instr == "/Agents/taskdefs/0/develop/instructions.txt"

    def test_builtin_host_path_remaps_to_global_mount(self, tmp_path):
        from lmer_cli.cli import _remap_taskdef_to_container

        repo = tmp_path / "Agents" / "global"
        (repo / "taskdef" / "chat").mkdir(parents=True)
        root, tdir, instr = _remap_taskdef_to_container(
            repo / "taskdef" / "chat", [], repo
        )
        assert root == "/Agents/global/taskdef"
        assert tdir == "/Agents/global/taskdef/chat"
        assert instr == "/Agents/global/taskdef/chat/instructions.txt"

    def test_container_path_passes_through(self, tmp_path):
        from lmer_cli.cli import _remap_taskdef_to_container

        resolved = Path("/Agents/global/taskdef/chat")
        root, tdir, instr = _remap_taskdef_to_container(resolved, [], None)
        assert root == "/Agents/global/taskdef"
        assert tdir == "/Agents/global/taskdef/chat"
        assert instr == "/Agents/global/taskdef/chat/instructions.txt"
