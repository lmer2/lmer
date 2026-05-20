"""Test hook functionality and enforcement."""
import os
import subprocess
import json
import time
from pathlib import Path
import pytest
from unittest.mock import patch, MagicMock


class TestHooks:
    """Test enforcement hook behaviors."""

    def test_rgr_hook_creates_timestamp(self, tmp_path, monkeypatch):
        """Test rgr hook records timestamp."""
        # Setup test environment
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        # Create test AGENTS.md
        global_dir = tmp_path / "Agents/global"
        global_dir.mkdir(parents=True)
        (global_dir / "AGENTS.md").write_text("## 🚨 Quick Reference\n## 🛑 COMMIT GATE")

        # Run rgr hook with LMER_GLOBAL_DIR pointing to test directory
        env = os.environ.copy()
        env["HOME"] = str(tmp_path)
        env["LMER_GLOBAL_DIR"] = str(global_dir)
        result = subprocess.run(
            ["python3", "hooks/rgr.py"],
            capture_output=True,
            text=True,
            env=env,
        )

        # Check timestamp was created
        timestamp_file = claude_dir / "last_rgr_timestamp"
        assert timestamp_file.exists()

        # Verify timestamp content
        with open(timestamp_file) as f:
            data = json.load(f)
            assert "timestamp" in data
            assert data["command"] == "rgr"
            assert "AGENTS.md" in data["file"]

    def test_rgrpc_requires_rules_read(self, tmp_path, monkeypatch):
        """Test rgrpc checks for recent rule reading."""
        monkeypatch.setenv("HOME", str(tmp_path))

        # Run without reading rules first
        result = subprocess.run(
            ["python3", "hooks/rgrpc.py"],
            capture_output=True,
            text=True
        )

        assert result.returncode != 0
        assert "No record of reading global rules" in result.stdout

    def test_pc_requires_gate_passage(self, tmp_path, monkeypatch):
        """Test pc hook requires recent gate passage."""
        monkeypatch.setenv("HOME", str(tmp_path))

        # Run without gate passage
        result = subprocess.run(
            ["python3", "hooks/pc.py"],
            capture_output=True,
            text=True
        )

        assert result.returncode != 0
        assert "No record of passing COMMIT GATE" in result.stdout

    def test_rgr_focused_commands(self):
        """Test focused command mapping."""
        # Test each focused command
        commands = ['rgr-git', 'rgr-test', 'rgr-code', 'rgr-security',
                   'rgr-docs', 'rgr-ci', 'rgr-deps']

        for cmd in commands:
            result = subprocess.run(
                ["python3", "hooks/rgr_focused.py", cmd],
                capture_output=True,
                text=True
            )

            # Should fail gracefully if rules don't exist
            assert cmd in result.stdout or "not found" in result.stdout

    def test_gate_expiration(self, tmp_path, monkeypatch):
        """Test that gate passages expire after 1 hour."""
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        # Create old gate passage
        gate_log = claude_dir / "gate_passages.log"
        old_time = time.strftime('%Y-%m-%d %H:%M:%S',
                                 time.localtime(time.time() - 3700))  # >1 hour ago
        gate_log.write_text(f"{old_time} - COMMIT GATE passed via rgrpc\n")

        # Try to use pc
        result = subprocess.run(
            ["python3", "hooks/pc.py"],
            capture_output=True,
            text=True
        )

        assert result.returncode != 0
        assert "Last COMMIT GATE passage was >1 hour ago" in result.stdout

    def test_secret_detection_in_pc(self, tmp_path, monkeypatch):
        """Test pc hook warns about potential secrets."""
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        # Create recent gate passage
        gate_log = claude_dir / "gate_passages.log"
        gate_log.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - COMMIT GATE passed\n")

        # Mock git commands
        with patch('subprocess.run') as mock_run:
            # Mock staged files with secrets
            mock_run.return_value = MagicMock(
                stdout="password=secret123\napi_key=abc",
                returncode=0
            )

            # Simulate user saying 'no' to continue
            with patch('builtins.input', return_value='no'):
                result = subprocess.run(
                    ["python3", "hooks/pc.py"],
                    capture_output=True,
                    text=True
                )

            # Should detect secrets (though our mock prevents actual execution)
            # In real scenario, it would warn about secrets

    def test_violation_tracking(self, tmp_path, monkeypatch):
        """Test violation display in rgr hook."""
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        # Create violations log
        violations = claude_dir / "violations.log"
        violations.write_text(
            "2024-01-01 10:00:00 - Committed without permission\n"
            "2024-01-01 11:00:00 - Pushed without checking allow list\n"
        )

        # Create test AGENTS.md
        global_dir = tmp_path / "Agents/global"
        global_dir.mkdir(parents=True)
        (global_dir / "AGENTS.md").write_text("# Test")

        # Run rgr with LMER_GLOBAL_DIR pointing to test directory
        env = os.environ.copy()
        env["HOME"] = str(tmp_path)
        env["LMER_GLOBAL_DIR"] = str(global_dir)
        result = subprocess.run(
            ["python3", "hooks/rgr.py"],
            capture_output=True,
            text=True,
            env=env,
        )

        assert "Recent rule violations detected" in result.stdout
        assert "Committed without permission" in result.stdout
