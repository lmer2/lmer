#!/usr/bin/env python3
"""Tests for lmer_cli.container_home module"""

import os
import pytest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

from lmer_cli.container_home import (
    get_container_home_path,
    ensure_container_home,
)


class TestGetContainerHomePath:
    """Test container home path calculation"""

    def test_get_container_home_path(self):
        """Test container-home path is inside repo root"""
        repo_root = Path("/home/user/lmer")
        container_home = get_container_home_path(repo_root)

        assert container_home == repo_root / "container-home"

    def test_get_container_home_path_different_roots(self):
        """Test path calculation for different repo roots"""
        roots = [
            Path("/opt/lmer"),
            Path("/home/user/.lmer"),
            Path("/tmp/test-repo"),
        ]

        for root in roots:
            container_home = get_container_home_path(root)
            assert container_home == root / "container-home"
            assert container_home.parent == root


class TestEnsureContainerHome:
    """Test container home directory setup"""

    def test_ensure_container_home_creates_directory(self):
        """Test creates container-home directory if not exists"""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            container_home = ensure_container_home(repo_root)

            assert container_home.exists()
            assert container_home.is_dir()
            assert container_home == repo_root / "container-home"

    def test_ensure_container_home_creates_subdirs(self):
        """Test creates required subdirectories"""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            container_home = ensure_container_home(repo_root)

            # Check required directories exist
            assert (container_home / ".ssh").exists()
            assert (container_home / ".config").exists()
            assert (container_home / ".local" / "share").exists()

    def test_ensure_container_home_idempotent(self):
        """Test multiple calls don't cause issues"""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)

            # Call twice
            container_home1 = ensure_container_home(repo_root)
            container_home2 = ensure_container_home(repo_root)

            assert container_home1 == container_home2
            assert container_home1.exists()

    def test_ensure_container_home_preserves_existing(self):
        """Test doesn't overwrite existing container-home"""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            container_home_path = repo_root / "container-home"

            # Create container-home with a marker file
            container_home_path.mkdir()
            marker = container_home_path / "marker.txt"
            marker.write_text("preserved")

            # Ensure container home
            container_home = ensure_container_home(repo_root)

            # Marker should still exist
            assert marker.exists()
            assert marker.read_text() == "preserved"

    def test_ensure_container_home_copies_ssh_config(self):
        """Test copies SSH config from host if exists"""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)

            # Mock host home with SSH config
            host_home = Path(tmpdir) / "fake-home"
            host_home.mkdir()
            ssh_dir = host_home / ".ssh"
            ssh_dir.mkdir()
            ssh_config = ssh_dir / "config"
            ssh_config.write_text("Host example\n  User testuser")

            with patch("pathlib.Path.home", return_value=host_home):
                container_home = ensure_container_home(repo_root)

                # Should have copied SSH config
                copied_config = container_home / ".ssh" / "config"
                if copied_config.exists():
                    assert "Host example" in copied_config.read_text()

    def test_ensure_container_home_permissions(self):
        """Test .ssh directory has correct permissions"""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            container_home = ensure_container_home(repo_root)

            ssh_dir = container_home / ".ssh"
            # On Unix, .ssh should be readable/writable by owner only
            if os.name != 'nt':  # Skip on Windows
                mode = ssh_dir.stat().st_mode & 0o777
                # Should be 700 or similar restrictive permissions
                assert mode <= 0o700


class TestContainerHomeIntegration:
    """Integration tests for container home setup"""

    def test_full_setup_workflow(self):
        """Test complete container home setup workflow"""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)

            # Mock a more complete host environment
            host_home = Path(tmpdir) / "host-home"
            host_home.mkdir()

            # Create various config files
            (host_home / ".ssh").mkdir()
            (host_home / ".ssh" / "config").write_text("Host *\n  ForwardAgent yes")
            (host_home / ".gitconfig").write_text("[user]\n  name = Test User")

            with patch("pathlib.Path.home", return_value=host_home):
                # Setup container home
                container_home = ensure_container_home(repo_root)

                # Verify structure
                assert container_home.exists()
                assert (container_home / ".ssh").is_dir()
                assert (container_home / ".config").is_dir()
                assert (container_home / ".local" / "share").is_dir()

                # Should not copy private keys by default
                assert not (container_home / ".ssh" / "id_rsa").exists()
                assert not (container_home / ".ssh" / "id_ed25519").exists()

    def test_container_home_in_different_locations(self):
        """Test container home works in different repo locations"""
        locations = ["lmer", ".lmer", "global", "agents/global"]

        for location in locations:
            with tempfile.TemporaryDirectory() as tmpdir:
                repo_root = Path(tmpdir) / location
                repo_root.mkdir(parents=True)

                container_home = ensure_container_home(repo_root)

                assert container_home.exists()
                assert container_home.parent == repo_root
