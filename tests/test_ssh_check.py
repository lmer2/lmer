#!/usr/bin/env python3
"""Tests for SSH setup checking in lmer_cli.cli"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from lmer_cli.cli import _check_ssh_setup


class TestCheckSshSetup:
    """Test SSH setup checking"""

    def test_no_warning_when_ssh_agent_enabled(self, capsys):
        """Test no warning is shown when SSH agent is enabled"""
        with tempfile.TemporaryDirectory() as tmpdir:
            container_home = Path(tmpdir)
            _check_ssh_setup(container_home, ssh_agent_enabled=True)
            captured = capsys.readouterr()
            assert "SSH not configured" not in captured.out

    def test_success_when_keys_in_container_home(self, capsys):
        """Test success message when SSH keys exist in container-home"""
        with tempfile.TemporaryDirectory() as tmpdir:
            container_home = Path(tmpdir)
            ssh_dir = container_home / ".ssh"
            ssh_dir.mkdir()
            (ssh_dir / "id_ed25519").touch()

            _check_ssh_setup(container_home, ssh_agent_enabled=False)
            captured = capsys.readouterr()
            assert "SSH keys found in container-home" in captured.out
            assert "SSH not configured" not in captured.out

    def test_success_with_rsa_key(self, capsys):
        """Test success message with RSA key"""
        with tempfile.TemporaryDirectory() as tmpdir:
            container_home = Path(tmpdir)
            ssh_dir = container_home / ".ssh"
            ssh_dir.mkdir()
            (ssh_dir / "id_rsa").touch()

            _check_ssh_setup(container_home, ssh_agent_enabled=False)
            captured = capsys.readouterr()
            assert "SSH keys found in container-home" in captured.out

    def test_success_with_ecdsa_key(self, capsys):
        """Test success message with ECDSA key"""
        with tempfile.TemporaryDirectory() as tmpdir:
            container_home = Path(tmpdir)
            ssh_dir = container_home / ".ssh"
            ssh_dir.mkdir()
            (ssh_dir / "id_ecdsa").touch()

            _check_ssh_setup(container_home, ssh_agent_enabled=False)
            captured = capsys.readouterr()
            assert "SSH keys found in container-home" in captured.out

    def test_warning_when_no_ssh_configured(self, capsys):
        """Test warning is shown when no SSH is configured"""
        with tempfile.TemporaryDirectory() as tmpdir:
            container_home = Path(tmpdir)
            # Mock Path.home() to return a temp dir without .ssh
            with patch.object(Path, "home", return_value=Path(tmpdir) / "fake_home"):
                _check_ssh_setup(container_home, ssh_agent_enabled=False)

            captured = capsys.readouterr()
            assert "SSH not configured" in captured.out
            assert "Option 1:" in captured.out
            assert "Option 2:" in captured.out
            assert "─" * 72 in captured.out  # Check for framing

    def test_warning_shows_copy_hint_when_host_has_keys(self, capsys):
        """Test warning shows copy command when host has SSH keys"""
        with tempfile.TemporaryDirectory() as tmpdir:
            container_home = Path(tmpdir) / "container-home"
            container_home.mkdir()

            # Create fake home with SSH keys
            fake_home = Path(tmpdir) / "fake_home"
            fake_home.mkdir()
            fake_ssh = fake_home / ".ssh"
            fake_ssh.mkdir()
            (fake_ssh / "id_ed25519").touch()

            with patch.object(Path, "home", return_value=fake_home):
                _check_ssh_setup(container_home, ssh_agent_enabled=False)

            captured = capsys.readouterr()
            assert "SSH not configured" in captured.out
            assert "cp -r ~/.ssh ~/.lmer/container-home/" in captured.out
            assert "chmod 700 ~/.lmer/container-home/.ssh" in captured.out
            assert "chmod 600 ~/.lmer/container-home/.ssh/id_*" in captured.out

    def test_warning_shows_keygen_hint_when_no_host_keys(self, capsys):
        """Test warning shows ssh-keygen hint when host has no keys"""
        with tempfile.TemporaryDirectory() as tmpdir:
            container_home = Path(tmpdir) / "container-home"
            container_home.mkdir()

            # Create fake home without SSH keys
            fake_home = Path(tmpdir) / "fake_home"
            fake_home.mkdir()

            with patch.object(Path, "home", return_value=fake_home):
                _check_ssh_setup(container_home, ssh_agent_enabled=False)

            captured = capsys.readouterr()
            assert "SSH not configured" in captured.out
            assert "ssh-keygen -t ed25519" in captured.out

    def test_no_warning_when_ssh_dir_exists_but_empty(self, capsys):
        """Test warning is shown when .ssh dir exists but has no keys"""
        with tempfile.TemporaryDirectory() as tmpdir:
            container_home = Path(tmpdir)
            ssh_dir = container_home / ".ssh"
            ssh_dir.mkdir()
            # .ssh exists but no key files

            with patch.object(Path, "home", return_value=Path(tmpdir) / "fake_home"):
                _check_ssh_setup(container_home, ssh_agent_enabled=False)

            captured = capsys.readouterr()
            assert "SSH not configured" in captured.out
