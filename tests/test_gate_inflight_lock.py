"""Tests for the gate-in-flight marker (src/lmer_cli/gate_lock.py, issue #201)."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lmer_cli import gate_lock

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _lock_dir(tmp_path, monkeypatch):
    """Every test gets its own marker directory."""
    directory = tmp_path / "locks"
    monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(directory))
    monkeypatch.delenv(gate_lock.GUARD_ENV, raising=False)
    return directory


def _write_marker(directory, pid, gate="gate-check", started_at=None, raw=None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pid}.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
        return path
    payload = {"pid": pid, "gate": gate}
    if started_at is not None:
        payload["started_at"] = started_at
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---- parse_marker ---------------------------------------------------------------


class TestParseMarker:
    def test_full_marker(self):
        marker = gate_lock.parse_marker('{"pid": 42, "gate": "gate-push", "started_at": 10.5}')
        assert marker == {"pid": 42, "gate": "gate-push", "started_at": 10.5}

    def test_pid_as_string_is_accepted(self):
        assert gate_lock.parse_marker('{"pid": "42"}')["pid"] == 42

    def test_missing_gate_gets_a_neutral_label(self):
        assert gate_lock.parse_marker('{"pid": 7}')["gate"] == "a gate"

    def test_unreadable_started_at_is_none_not_a_rejection(self):
        # The pid is the fact that matters; decoration must not throw the
        # marker away, or a torn timestamp would silently disable deferral.
        marker = gate_lock.parse_marker('{"pid": 7, "started_at": "soon"}')
        assert marker["pid"] == 7
        assert marker["started_at"] is None

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "not json",
            '{"pid": 0}',
            '{"pid": -3}',
            '{"pid": null}',
            '{"gate": "gate-check"}',
            '{"pid": "abc"}',
            "[1, 2, 3]",
            '"a string"',
            '{"pid": 12',  # torn write
        ],
    )
    def test_unusable_markers_are_none(self, text):
        assert gate_lock.parse_marker(text) is None


# ---- marker_is_live -------------------------------------------------------------


class TestMarkerIsLive:
    def test_live_when_pid_alive_and_fresh(self):
        marker = {"pid": 1, "started_at": 100.0}
        assert gate_lock.marker_is_live(marker, now=200.0, pid_alive=True) is True

    def test_dead_pid_is_never_live(self):
        marker = {"pid": 1, "started_at": 100.0}
        assert gate_lock.marker_is_live(marker, now=101.0, pid_alive=False) is False

    def test_expired_marker_is_not_live_even_with_a_live_pid(self):
        marker = {"pid": 1, "started_at": 0.0}
        now = gate_lock.STALE_AFTER_SECONDS + 1
        assert gate_lock.marker_is_live(marker, now=now, pid_alive=True) is False

    def test_unknown_start_time_falls_back_to_pid_liveness(self):
        marker = {"pid": 1, "started_at": None}
        assert gate_lock.marker_is_live(marker, now=1e9, pid_alive=True) is True

    def test_cap_is_far_longer_than_a_real_gate(self):
        # The full suite is ~14 minutes and a slow runner beats that; a cap
        # that could expire mid-gate reintroduces #201 as a flake.
        assert gate_lock.STALE_AFTER_SECONDS >= 2 * 60 * 60
        marker = {"pid": 1, "started_at": 0.0}
        assert gate_lock.marker_is_live(marker, now=45 * 60, pid_alive=True) is True


# ---- formatting -----------------------------------------------------------------


class TestDescribe:
    @pytest.mark.parametrize(
        "seconds,expected",
        [(0, "0s"), (9.7, "9s"), (60, "1m00s"), (192, "3m12s"), (3600, "1h00m"), (7500, "2h05m")],
    )
    def test_format_age(self, seconds, expected):
        assert gate_lock.format_age(seconds) == expected

    @pytest.mark.parametrize("seconds", [None, -1])
    def test_format_age_unknown(self, seconds):
        assert gate_lock.format_age(seconds) == ""

    def test_describe_names_gate_pid_and_age(self):
        text = gate_lock.describe_marker(
            {"pid": 99, "gate": "gate-check", "started_at": 1000.0}, now=1192.0
        )
        assert "gate-check" in text
        assert "99" in text
        assert "3m12s" in text

    def test_describe_without_marker(self):
        assert gate_lock.describe_marker(None) == "a gate"


# ---- read_markers / active_gate -------------------------------------------------


class TestActiveGate:
    def test_no_lock_dir_is_idle(self, _lock_dir):
        assert not _lock_dir.exists()
        assert gate_lock.read_markers() == []
        assert gate_lock.active_gate() is None
        assert gate_lock.describe_active_gate() is None

    def test_live_marker_from_another_process_is_seen(self, _lock_dir):
        _write_marker(_lock_dir, os.getppid(), started_at=time.time())
        marker = gate_lock.active_gate()
        assert marker is not None
        assert marker["pid"] == os.getppid()

    def test_own_marker_never_defers_the_holder(self, _lock_dir):
        # `work verify` holds a marker while its own receipt machinery runs.
        _write_marker(_lock_dir, os.getpid(), started_at=time.time())
        assert gate_lock.active_gate() is None
        assert gate_lock.active_gate(exclude_self=False) is not None

    def test_dead_marker_is_ignored_and_pruned(self, _lock_dir):
        dead = _spawn_and_reap()
        path = _write_marker(_lock_dir, dead, started_at=time.time())
        assert gate_lock.active_gate() is None
        assert not path.exists()

    def test_expired_marker_is_pruned(self, _lock_dir):
        path = _write_marker(
            _lock_dir,
            os.getppid(),
            started_at=time.time() - gate_lock.STALE_AFTER_SECONDS - 60,
        )
        assert gate_lock.active_gate() is None
        assert not path.exists()

    def test_corrupt_marker_is_skipped_but_not_pruned(self, _lock_dir):
        # A torn write from a gate starting right now parses on the next read.
        path = _write_marker(_lock_dir, 4242, raw='{"pid": 42')
        assert gate_lock.active_gate() is None
        assert path.exists()

    def test_longest_running_gate_wins(self, _lock_dir):
        now = time.time()
        _write_marker(_lock_dir, os.getppid(), gate="gate-push", started_at=now - 10)
        _write_marker(_lock_dir, 1, gate="gate-check", started_at=now - 600)
        assert gate_lock.active_gate()["gate"] == "gate-check"

    def test_unreadable_lock_dir_reads_as_idle(self, tmp_path, monkeypatch):
        # Fail open: the pre-#201 race is survivable, a wedged session is not.
        monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(tmp_path / "nope" / "deeper"))
        assert gate_lock.active_gate() is None

    @pytest.mark.parametrize("value", ["0", "false", "no", "NO"])
    def test_kill_switch_makes_consumers_stand_down(self, _lock_dir, monkeypatch, value):
        _write_marker(_lock_dir, os.getppid(), started_at=time.time())
        monkeypatch.setenv(gate_lock.GUARD_ENV, value)
        assert gate_lock.active_gate() is None
        # ...but the markers are still there to be read: the switch turns off
        # the consumers, not the recording.
        assert gate_lock.read_markers()

    @pytest.mark.parametrize("value", ["", "1", "true", "yes", "garbage"])
    def test_guard_defaults_to_enabled(self, _lock_dir, monkeypatch, value):
        _write_marker(_lock_dir, os.getppid(), started_at=time.time())
        monkeypatch.setenv(gate_lock.GUARD_ENV, value)
        assert gate_lock.active_gate() is not None


def _spawn_and_reap():
    """A pid that is definitely dead (spawned, waited on, reaped)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# ---- hold_gate_lock -------------------------------------------------------------


class TestHoldGateLock:
    def test_marker_exists_during_and_is_gone_after(self, _lock_dir):
        with gate_lock.hold_gate_lock("gate-check"):
            markers = gate_lock.read_markers()
            assert [m["pid"] for m in markers] == [os.getpid()]
            assert markers[0]["gate"] == "gate-check"
        assert gate_lock.read_markers() == []

    def test_marker_is_removed_when_the_body_raises(self, _lock_dir):
        # A marker outliving its gate would defer every work-repo write until
        # the pid died — worse than the race it prevents.
        with pytest.raises(RuntimeError):
            with gate_lock.hold_gate_lock("gate-check"):
                raise RuntimeError("gate blew up")
        assert list(_lock_dir.glob("*.json")) == []

    def test_marker_is_removed_on_systemexit(self, _lock_dir):
        with pytest.raises(SystemExit):
            with gate_lock.hold_gate_lock("gate-push"):
                raise SystemExit(1)
        assert list(_lock_dir.glob("*.json")) == []

    def test_lock_dir_is_created_on_demand(self, _lock_dir):
        assert not _lock_dir.exists()
        with gate_lock.hold_gate_lock("gate-check"):
            assert _lock_dir.is_dir()

    def test_unwritable_lock_dir_never_breaks_the_gate(self, tmp_path, monkeypatch):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(blocker / "locks"))
        ran = False
        with gate_lock.hold_gate_lock("gate-check"):
            ran = True
        assert ran  # no exception escaped — receipts' fail-soft contract

    def test_lock_dir_env_is_read_at_call_time(self, tmp_path, monkeypatch):
        first = tmp_path / "first"
        second = tmp_path / "second"
        monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(first))
        assert gate_lock.lock_dir() == first
        monkeypatch.setenv(gate_lock.LOCK_DIR_ENV, str(second))
        assert gate_lock.lock_dir() == second

    def test_default_lock_dir_when_unset(self, monkeypatch):
        monkeypatch.delenv(gate_lock.LOCK_DIR_ENV, raising=False)
        assert gate_lock.lock_dir() == Path(gate_lock.DEFAULT_LOCK_DIR)


# ---- env plumbing ---------------------------------------------------------------


class TestContainerEnvPassthrough:
    """Both vars must reach INSIDE the container — the gates, the work CLI and
    the Stop hook all run there, and a host value that stopped at the boundary
    would silently leave the deferral on (env-vars.md rule 4)."""

    @pytest.mark.parametrize(
        "name", [gate_lock.GUARD_ENV, gate_lock.LOCK_DIR_ENV]
    )
    def test_cli_env_dict_declares_the_var(self, name):
        source = (REPO_ROOT / "src" / "lmer_cli" / "cli.py").read_text(encoding="utf-8")
        assert f'"{name}": os.environ.get("{name}")' in source

    @pytest.mark.parametrize(
        "name", [gate_lock.GUARD_ENV, gate_lock.LOCK_DIR_ENV]
    )
    def test_documented_in_lmer_cli_docs(self, name):
        source = (REPO_ROOT / "docs" / "LMER-CLI.md").read_text(encoding="utf-8")
        assert f"`{name}`" in source

    def test_names_carry_the_lmer_prefix(self):
        assert gate_lock.GUARD_ENV.startswith("LMER_")
        assert gate_lock.LOCK_DIR_ENV.startswith("LMER_")
