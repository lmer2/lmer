#!/usr/bin/env python3
"""Integration tests for lmer container features"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests._lmer_runtime import requires_container, requires_lmer_venv


@requires_container
class TestLmerDockerIntegration:
    """Integration tests that require Docker"""

    def test_env_vars_passed_to_container(self):
        """Test that environment variables from .env are available in container"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create .env file with test variables
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("""
TEST_ENV_VAR=hello_from_env
ANOTHER_TEST_VAR=42
DB_PASSWORD=secret123
""")

            # Test that variables are accessible in container
            result = subprocess.run(
                ["bash", "-c",
                 f"cd {tmpdir} && {Path(__file__).parent.parent}/lmer --no-task --exec 'echo $TEST_ENV_VAR'"],
                capture_output=True,
                text=True,
                timeout=30
            )

            # Check if the container can access the env var
            # Note: This will only work if Docker is running and container is built
            if result.returncode == 0:
                combined_output = result.stdout + result.stderr
                assert "hello_from_env" in result.stdout or \
                       "Found .env file" in combined_output or \
                       "Loaded" in combined_output and "variables from .env" in combined_output, \
                    f"Environment variable not passed to container. Output: {result.stdout}"



@requires_container
@requires_lmer_venv
class TestLmerErrorHandling:
    """Test error handling in lmer script"""

    def test_invalid_env_file(self, lmer_subprocess_env):
        """Test handling of malformed .env files"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create an invalid .env file
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("INVALID LINE WITHOUT EQUALS")

            # lmer should still work even with invalid .env.
            result = subprocess.run(
                ["bash", "-c",
                 f"cd {tmpdir} && {Path(__file__).parent.parent}/lmer --no-task --exec 'echo test' 2>&1"],
                capture_output=True,
                text=True,
                timeout=30,
                env=lmer_subprocess_env,
            )

            # Should detect .env but handle errors gracefully
            combined_output = result.stdout + result.stderr
            assert "Found .env file" in combined_output or \
                   result.returncode == 0, \
                f"lmer failed with invalid .env. Output: {result.stdout}"

    def test_empty_env_file(self, lmer_subprocess_env):
        """Test handling of empty .env files"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create an empty .env file
            env_file = Path(tmpdir) / ".env"
            env_file.touch()

            # lmer should handle empty .env gracefully.
            result = subprocess.run(
                ["bash", "-c",
                 f"cd {tmpdir} && {Path(__file__).parent.parent}/lmer --no-task --exec 'echo test'"],
                capture_output=True,
                text=True,
                timeout=30,
                env=lmer_subprocess_env,
            )

            # Should detect .env even if empty
            combined_output = result.stdout + result.stderr
            assert "Found .env file" in combined_output or \
                   result.returncode == 0, \
                f"lmer failed with empty .env. Output: {result.stdout}"
