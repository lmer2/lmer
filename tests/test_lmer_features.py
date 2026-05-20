#!/usr/bin/env python3
"""Test lmer script features for .env and .venv handling"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests._lmer_runtime import requires_container, requires_lmer_venv


@requires_container
@requires_lmer_venv
class TestLmerEnvFeatures:
    """Test lmer environment and venv handling"""

    def test_env_file_detection(self):
        """Test that lmer detects and loads .env files"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a .env file
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("TEST_VAR=test_value\nANOTHER_VAR=another_value\n")

            # Create a simple test script that checks environment
            test_script = Path(tmpdir) / "test_env.sh"
            test_script.write_text("""#!/bin/bash
echo "TEST_VAR=$TEST_VAR"
echo "ANOTHER_VAR=$ANOTHER_VAR"
""")
            test_script.chmod(0o755)

            # Run lmer in exec mode to check if env vars are loaded
            result = subprocess.run(
                ["bash", "-c", f"cd {tmpdir} && {Path(__file__).parent.parent}/lmer --no-task --exec 'echo TEST_VAR=$TEST_VAR'"],
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": os.environ.get("PATH", "")}
            )

            # Check that .env was detected
            combined_output = result.stdout + result.stderr
            assert "Found .env file" in combined_output or "Loaded" in combined_output and "variables from .env" in combined_output, \
                f"lmer did not detect .env file. Output: {result.stdout}\nError: {result.stderr}"

    def test_env_file_not_loaded_when_absent(self):
        """Test that lmer doesn't error when no .env file exists"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Run lmer without .env file
            result = subprocess.run(
                ["bash", "-c", f"cd {tmpdir} && {Path(__file__).parent.parent}/lmer --no-task --exec 'echo test'"],
                capture_output=True,
                text=True
            )

            # Should not show .env loading message (but will show "not found" message)
            combined_output = result.stdout + result.stderr
            assert ".env file not found" in combined_output or result.returncode == 0, \
                f"lmer incorrectly reported .env file. Output: {result.stdout}\nError: {result.stderr}"



class TestLmerScriptValidation:
    """Validate lmer script syntax and structure"""

    def test_lmer_script_exists(self):
        """Test that lmer script exists and is executable"""
        lmer_path = Path(__file__).parent.parent / "lmer"
        assert lmer_path.exists(), "lmer script not found"
        assert os.access(lmer_path, os.X_OK), "lmer script is not executable"

    def test_lmer_script_bash_syntax(self):
        """Test that lmer script has valid bash syntax"""
        lmer_path = Path(__file__).parent.parent / "lmer"
        result = subprocess.run(
            ["bash", "-n", str(lmer_path)],
            capture_output=True,
            text=True
        )
        assert result.returncode == 0, f"lmer script has bash syntax errors: {result.stderr}"

    def test_env_file_args_in_cli(self):
        """Test that .env file handling is implemented in lmer"""
        cli_path = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"
        assert cli_path.exists(), "cli.py not found"

        cli_content = cli_path.read_text()

        # Check that .env file loading is implemented
        assert "dotenv" in cli_content or ".env" in cli_content, ".env handling not found in CLI"
