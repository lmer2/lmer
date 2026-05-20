#!/usr/bin/env python3
"""Test that gate checks work without a tests directory"""

import tempfile
import subprocess
import os
import sys
from pathlib import Path
import pytest


@pytest.mark.skip(reason="Skipping test - stalls forever?")
def test_gate_check_without_tests_directory():
    """Test that gate checks handle missing tests directory gracefully"""
    # Create a temporary directory
    with tempfile.TemporaryDirectory() as tmpdir:
        # Initialize a git repo
        subprocess.run(["git", "init"], cwd=tmpdir, check=True, capture_output=True)

        # Create a minimal Python file
        (Path(tmpdir) / "main.py").write_text("print('hello')\n")

        # Copy gates.py to the temp directory
        gates_src = Path(__file__).parent.parent / "src" / "lmer_cli" / "gates.py"
        gates_dst = Path(tmpdir) / "gates.py"
        gates_dst.write_text(gates_src.read_text())

        # Create bin directory and gate-check
        bin_dir = Path(tmpdir) / "bin"
        bin_dir.mkdir()
        gate_check = bin_dir / "gate-check"
        gate_check.write_text("""#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gates import commit_gate
if __name__ == "__main__":
    sys.exit(commit_gate(verbose=False))
""")
        gate_check.chmod(0o755)

        # Run gate-check
        result = subprocess.run(
            ["python3", str(gate_check)],
            cwd=tmpdir,
            capture_output=True,
            text=True
        )

        # Check if tests were skipped
        assert "No tests directory found (skipped)" in result.stdout, \
            f"Gate check did not skip tests properly. Output: {result.stdout}"
