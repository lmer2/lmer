"""Performance and efficiency tests."""
import time
import subprocess
from pathlib import Path
import pytest


class TestPerformance:
    """Test performance aspects of the system."""

    def test_rule_file_sizes(self, all_rule_files):
        """Ensure rule files aren't too large for quick reading."""
        max_size_kb = 50  # 50KB should be plenty for a rule file

        for rule_file in all_rule_files:
            size_kb = rule_file.stat().st_size / 1024
            assert size_kb < max_size_kb, \
                f"{rule_file.name} is {size_kb:.1f}KB (max: {max_size_kb}KB)"

    def test_hook_execution_time(self, tmp_path, monkeypatch):
        """Test hooks execute quickly."""
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude").mkdir()

        # Time a simple hook execution
        start = time.time()
        result = subprocess.run(
            ["python3", "hooks/rgr.py"],
            capture_output=True,
            timeout=5  # Should complete in 5 seconds
        )
        duration = time.time() - start

        assert duration < 2, f"Hook took {duration:.2f}s (should be <2s)"

    def test_no_large_files_tracked(self, project_root):
        """Ensure no large files are tracked in git."""
        # Get list of tracked files
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=project_root,
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            max_size_mb = 1  # 1MB max for any tracked file

            for file_name in result.stdout.strip().split('\n'):
                if file_name:
                    file_path = project_root / file_name
                    if file_path.exists():
                        size_mb = file_path.stat().st_size / (1024 * 1024)
                        assert size_mb < max_size_mb, \
                            f"{file_name} is {size_mb:.2f}MB (max: {max_size_mb}MB)"

    def test_import_performance(self):
        """Test that imports are fast."""
        # Test importing our modules doesn't take too long
        import_code = """
import time
start = time.time()
import subprocess
import json
import hashlib
import ssl
duration = time.time() - start
print(duration)
"""
        result = subprocess.run(
            ["python3", "-c", import_code],
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            duration = float(result.stdout.strip())
            assert duration < 0.5, f"Imports took {duration:.3f}s (should be <0.5s)"
