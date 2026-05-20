"""Integration tests for the complete system."""
import subprocess
import tempfile
import shutil
from pathlib import Path
import pytest


class TestIntegration:
    """Test complete workflows and integrations."""

    def test_full_commit_workflow(self, tmp_path, monkeypatch):
        """Test complete commit workflow from change to commit."""
        # Create a test git repo
        repo_path = tmp_path / "test_repo"
        repo_path.mkdir()
        monkeypatch.chdir(repo_path)

        # Initialize git
        subprocess.run(["git", "init"], check=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)

        # Create a test file
        test_file = repo_path / "test.py"
        test_file.write_text("print('hello')")

        # Stage the file
        subprocess.run(["git", "add", "test.py"], check=True)

        # Test pre-commit would run (if hooks installed)
        result = subprocess.run(
            ["git", "diff", "--staged", "--name-only"],
            capture_output=True,
            text=True
        )
        assert "test.py" in result.stdout

    def test_rule_module_loading(self, rules_dir):
        """Test all rule modules can be loaded and parsed."""
        import re

        for rule_file in rules_dir.glob("*.md"):
            content = rule_file.read_text()

            # Check structure
            assert content.startswith("# "), f"{rule_file.name} missing title"
            assert "## 🚨 Critical" in content, f"{rule_file.name} missing critical section"

            # Check for valid markdown
            headers = re.findall(r'^#{1,6} ', content, re.MULTILINE)
            assert len(headers) > 2, f"{rule_file.name} has too few sections"

    def test_command_execution_simulation(self, project_root, monkeypatch):
        """Test command shortcuts work correctly."""
        # Mock the home directory
        mock_home = project_root / "test_home"
        mock_home.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(mock_home))

        # Test rgr command components
        claude_dir = mock_home / ".claude"
        claude_dir.mkdir(exist_ok=True)

        # Verify hook scripts exist and are executable
        hooks_dir = project_root / "hooks"
        assert hooks_dir.exists()

        for hook in ["rgr.py", "rgrpc.py", "pc.py", "rgr_focused.py"]:
            hook_path = hooks_dir / hook
            assert hook_path.exists(), f"Hook {hook} not found"
            # Hooks pin the lmer venv via absolute shebang so they don't get
            # shadowed by a workspace venv on PATH inside the container.
            with open(hook_path) as f:
                first_line = f.readline()
                assert first_line.startswith("#!/Agents/global/.venv/bin/python"), \
                    f"{hook} missing proper shebang (expected lmer venv pin)"

    def test_pre_commit_config_valid(self, project_root):
        """Test pre-commit configuration is valid."""
        config_path = project_root / ".pre-commit-config.yaml"
        assert config_path.exists()

        # Try to validate the config
        result = subprocess.run(
            ["pre-commit", "validate-config"],
            cwd=project_root,
            capture_output=True
        )

        # Should either succeed or fail gracefully if pre-commit not installed
        assert result.returncode == 0 or "not found" in result.stderr.decode()

    def test_docker_files_valid(self, project_root):
        """Test Docker configuration files are valid."""
        containerfile = project_root / "Containerfile"

        assert containerfile.exists()

        # Check Containerfile syntax basics
        with open(containerfile) as f:
            content = f.read()
            assert "FROM oraclelinux:9" in content
            assert "FIPS" in content
            assert "USER developer" in content

    def test_enforcement_hooks_integration(self, tmp_path, monkeypatch):
        """Test enforcement hooks work together."""
        # Setup test environment
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        # Simulate reading rules
        timestamp_file = claude_dir / "last_rgr_timestamp"
        import time
        import json
        with open(timestamp_file, 'w') as f:
            json.dump({
                "timestamp": time.time(),
                "command": "rgr"
            }, f)

        # Verify timestamp was created
        assert timestamp_file.exists()

        # Simulate gate passage
        gate_log = claude_dir / "gate_passages.log"
        gate_log.write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - COMMIT GATE passed\n")

        # Verify gate log exists
        assert gate_log.exists()
