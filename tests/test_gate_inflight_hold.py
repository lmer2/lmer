"""The gate commands hold the in-flight marker for their whole run (#201).

Source-level for the wiring — running a real `gate-check` here would cost the
~14-minute suite this issue is about — plus a cross-process test proving the
marker one process holds is actually seen by another, which is the property the
deferral rests on.
"""
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from lmer_cli import gate_lock

REPO_ROOT = Path(__file__).parent.parent
GATES = {
    "gate-check": "gate-check",
    "gate-commit": "gate-commit",
    "gate-push": "gate-push",
}


def _source(script):
    return (REPO_ROOT / "bin" / script).read_text(encoding="utf-8")


class TestGateScriptsHoldTheMarker:
    @pytest.mark.parametrize("script", sorted(GATES))
    def test_imports_hold_gate_lock(self, script):
        assert "from lmer_cli.gate_lock import hold_gate_lock" in _source(script)

    @pytest.mark.parametrize("script", sorted(GATES))
    def test_import_is_fail_soft(self, script):
        """No lock problem may ever change a gate's exit code — the same
        contract receipt emission already has."""
        source = _source(script)
        match = re.search(
            r"try:\n\s+from lmer_cli\.gate_lock import hold_gate_lock\n\s*except ImportError:",
            source,
        )
        assert match, f"{script} must degrade to a no-op when the import fails"
        assert "@contextmanager" in source

    @pytest.mark.parametrize("script,label", sorted(GATES.items()))
    def test_wraps_its_run_with_the_right_label(self, script, label):
        assert f'with hold_gate_lock("{label}")' in _source(script)

    @pytest.mark.parametrize("script", sorted(GATES))
    def test_fallback_contextmanager_is_a_working_no_op(self, script):
        """The `except ImportError` fallback must yield, not merely exist: a
        broken fallback would take every gate down with it."""
        source = _source(script)
        # Anchor on the gate_lock import specifically — the scripts carry an
        # earlier fail-soft `except ImportError` for receipt emission too.
        anchor = source.index("from lmer_cli.gate_lock import hold_gate_lock")
        start = source.index("except ImportError:", anchor)
        block = source[start : source.index("\n\n\n", start)]
        namespace = {}
        exec(textwrap.dedent(block[block.index("\n") + 1 :]), namespace)  # noqa: S102
        ran = False
        with namespace["hold_gate_lock"]("gate-check"):
            ran = True
        assert ran


class TestMarkerCrossesProcesses:
    def test_a_holding_child_is_visible_to_the_parent(self, tmp_path, monkeypatch):
        lock_dir = tmp_path / "locks"
        monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(lock_dir))
        monkeypatch.delenv(gate_lock.GUARD_ENV, raising=False)
        script = textwrap.dedent(
            """
            import sys, time
            sys.path.insert(0, sys.argv[1])
            from lmer_cli.gate_lock import hold_gate_lock
            with hold_gate_lock("gate-check"):
                print("held", flush=True)
                time.sleep(30)
            """
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(REPO_ROOT / "src")],
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, gate_lock.LOCK_DIR_ENV: str(lock_dir)},
        )
        try:
            assert child.stdout.readline().strip() == "held"
            marker = gate_lock.active_gate()
            assert marker is not None
            assert marker["pid"] == child.pid
            assert marker["gate"] == "gate-check"
        finally:
            child.terminate()
            child.wait(timeout=30)

    def test_a_killed_holder_stops_deferring_anything(self, tmp_path, monkeypatch):
        """Liveness comes from the OS, not from the marker's own promises: a
        gate killed without cleanup must not wedge every later commit."""
        lock_dir = tmp_path / "locks"
        monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(lock_dir))
        script = textwrap.dedent(
            """
            import sys, time
            sys.path.insert(0, sys.argv[1])
            from lmer_cli.gate_lock import hold_gate_lock
            with hold_gate_lock("gate-check"):
                print("held", flush=True)
                time.sleep(30)
            """
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(REPO_ROOT / "src")],
            stdout=subprocess.PIPE,
            text=True,
            env={**os.environ, gate_lock.LOCK_DIR_ENV: str(lock_dir)},
        )
        assert child.stdout.readline().strip() == "held"
        child.kill()
        child.wait(timeout=30)
        # SIGKILL leaves the marker file behind; the pid check retires it.
        assert list(lock_dir.glob("*.json"))
        assert gate_lock.active_gate() is None
        assert list(lock_dir.glob("*.json")) == []
