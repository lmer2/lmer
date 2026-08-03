"""Tests for lmer_cli.supervisor."""
from __future__ import annotations

import errno
import os
import socket
import stat
import termios
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from lmer_cli import supervisor


# ---------------------------------------------------------------------------
# OutputBuffer
# ---------------------------------------------------------------------------


class TestOutputBuffer:
    def test_appends_and_reads_from_zero(self):
        buf = supervisor.OutputBuffer(limit=1024)
        buf.append(b"hello ")
        buf.append(b"world")
        data, cursor, dropped = buf.read_since(0)
        assert data == b"hello world"
        assert cursor == 11
        assert dropped == 0

    def test_partial_read_via_cursor(self):
        buf = supervisor.OutputBuffer(limit=1024)
        buf.append(b"abcdef")
        data, cursor, dropped = buf.read_since(3)
        assert data == b"def"
        assert cursor == 6
        assert dropped == 0

    def test_empty_when_caught_up(self):
        buf = supervisor.OutputBuffer(limit=1024)
        buf.append(b"abc")
        data, cursor, dropped = buf.read_since(3)
        assert data == b""
        assert cursor == 3
        assert dropped == 0

    def test_eviction_reports_dropped_bytes(self):
        buf = supervisor.OutputBuffer(limit=10)
        buf.append(b"0123456789")  # exactly at limit
        buf.append(b"abcde")  # forces eviction of 5 bytes
        # Reader was at 0, but oldest available is now 5
        data, cursor, dropped = buf.read_since(0)
        assert dropped == 5
        assert cursor == 15
        assert data.endswith(b"abcde")
        assert len(data) == 10

    def test_blocks_for_new_data_then_returns(self):
        buf = supervisor.OutputBuffer(limit=1024)

        def producer():
            time.sleep(0.05)
            buf.append(b"late")

        threading.Thread(target=producer, daemon=True).start()
        start = time.monotonic()
        data, cursor, _ = buf.read_since(0, timeout=2.0)
        elapsed = time.monotonic() - start
        assert data == b"late"
        assert cursor == 4
        assert elapsed < 1.0

    def test_timeout_expires_with_no_data(self):
        buf = supervisor.OutputBuffer(limit=1024)
        start = time.monotonic()
        data, cursor, _ = buf.read_since(0, timeout=0.1)
        elapsed = time.monotonic() - start
        assert data == b""
        assert cursor == 0
        assert elapsed >= 0.1


# ---------------------------------------------------------------------------
# The idle clock (T95)
# ---------------------------------------------------------------------------
#
# The fact only this process can know: the host cannot tell a harness that is
# working from one that finished and is sitting at its prompt, because run state
# moves when a session *ends* (spec D24). So the supervisor times the gap since the
# last byte the wrapped process produced, and /healthz reports it.
#
# The clock is injected rather than slept against — this suite runs on a one-CPU
# box, where a timing assertion is a flake — and it is monotonic, so an NTP step
# cannot produce a negative idle.


class _FakeClock:
    """A monotonic clock a test advances by hand. Seconds, arbitrary origin."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestIdleClock:
    def test_nothing_produced_yet_reports_no_idleness_at_all(self):
        """``None``, not zero: zero says the harness just wrote something.

        It is also the same answer an image too old to have this feature gives the
        platform, which is what makes one absent case on the read side instead of
        two.
        """
        buf = supervisor.OutputBuffer(limit=1024, clock=_FakeClock())
        assert buf.idle_seconds is None

    def test_the_clock_starts_at_the_first_byte_and_counts_from_the_last(self):
        clock = _FakeClock()
        buf = supervisor.OutputBuffer(limit=1024, clock=clock)

        buf.append(b"first")
        assert buf.idle_seconds == 0.0
        clock.advance(90)
        assert buf.idle_seconds == 90.0

        # A second chunk resets it: idleness is measured from the *last* output.
        buf.append(b"second")
        assert buf.idle_seconds == 0.0
        clock.advance(1320)
        assert buf.idle_seconds == 1320.0

    def test_reading_the_buffer_is_not_activity(self):
        """The mixed-fleet guard, from the side that has to hold it.

        The platform's re-attach drain polls ``/output`` every twenty seconds for
        the life of a detached session (:class:`lmer_platform.reattach
        .ControlDrain`), and the host-side fallback must never be able to make a
        quiet session look busy. It cannot, structurally: the reader's path is
        ``read_since``, and only ``append`` touches the clock. This is the test
        that says so, because the property is invisible in either function.
        """
        clock = _FakeClock()
        buf = supervisor.OutputBuffer(limit=1024, clock=clock)
        buf.append(b"the harness said this once")
        clock.advance(600)

        for _ in range(3):
            buf.read_since(0)
            buf.read_since(buf.end_offset)
            clock.advance(0)

        assert buf.idle_seconds == 600.0, (
            "polling the output buffer moved the idle clock, so a session nobody "
            "is watching would look busy for as long as the drain kept reading it"
        )

    def test_an_empty_append_is_not_activity(self):
        """``append(b"")`` returns early, and it must return early *first*.

        The forwarding loop treats an empty read as EOF and breaks, so this is a
        direct call's problem rather than a production path — but a clock that
        moved on nothing would still be recording an event that did not happen.
        """
        clock = _FakeClock()
        buf = supervisor.OutputBuffer(limit=1024, clock=clock)
        buf.append(b"")
        assert buf.idle_seconds is None

    def test_a_clock_that_stepped_backwards_never_reports_a_negative_idle(self):
        clock = _FakeClock()
        buf = supervisor.OutputBuffer(limit=1024, clock=clock)
        buf.append(b"out")
        clock.advance(-5)
        assert buf.idle_seconds == 0.0

    def test_the_report_is_null_in_both_spellings_when_nothing_is_known(self):
        assert supervisor._activity_report(None) == {
            "last_output_at": None, "idle_seconds": None,
        }

    def test_the_report_dates_the_last_output_by_subtracting_the_idle(self):
        """One reading, two spellings, and the wall clock applied exactly once.

        ``now`` is passed so the ISO is exact here; in the route it is read at
        answer time, which is what keeps the timestamp from being frozen before an
        NTP correction the way an append-time one would be.
        """
        now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
        assert supervisor._activity_report(1320.0, now=now) == {
            "last_output_at": "2026-07-28T11:38:00Z",
            "idle_seconds": 1320.0,
        }

    def test_the_idle_reading_is_rounded_to_something_a_person_reads(self):
        report = supervisor._activity_report(12.345678)
        assert report["idle_seconds"] == 12.3


# ---------------------------------------------------------------------------
# Port range parsing / picking
# ---------------------------------------------------------------------------


class TestPortRange:
    def test_parse_valid(self):
        assert supervisor._parse_port_range("8700-8799") == (8700, 8799)

    def test_parse_single_value_rejected(self):
        with pytest.raises(ValueError):
            supervisor._parse_port_range("8700")

    def test_parse_inverted_rejected(self):
        with pytest.raises(ValueError):
            supervisor._parse_port_range("8800-8700")

    def test_pick_port_in_range(self):
        port = supervisor._pick_port((9000, 9100), "127.0.0.1")
        assert 9000 <= port <= 9100

    def test_pick_port_raises_when_only_port_is_busy(self):
        # Single-port range whose only port is held → must raise
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", 0))
            held_port = held.getsockname()[1]
            with pytest.raises(RuntimeError):
                supervisor._pick_port((held_port, held_port), "127.0.0.1")

    def test_pick_port_skips_busy_and_returns_another(self):
        # Hold one port inside a multi-port range; picker must return a
        # different port within the same range.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", 0))
            held_port = held.getsockname()[1]
            # Build a range that includes the held port plus neighbors. Use
            # held_port +/- 1 as bounds, then verify we get something other
            # than held_port back.
            low = max(1024, held_port - 5)
            high = held_port + 5
            for _ in range(20):
                picked = supervisor._pick_port((low, high), "127.0.0.1")
                assert low <= picked <= high
                if picked != held_port:
                    return
            pytest.fail("never returned a port other than the busy one")

    def test_pick_ports_returns_distinct_ports_in_range(self):
        ports = supervisor._pick_ports((9000, 9100), "127.0.0.1", 5)
        assert len(ports) == 5
        assert len(set(ports)) == 5
        assert all(9000 <= p <= 9100 for p in ports)

    def test_pick_ports_rejects_non_positive_count(self):
        with pytest.raises(ValueError):
            supervisor._pick_ports((9000, 9100), "127.0.0.1", 0)

    def test_pick_ports_raises_when_pool_too_small(self):
        # Single-port pool cannot satisfy a request for two ports.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", 0))
            free_port = held.getsockname()[1]
        # free_port is released; a request for 2 from a 1-wide pool must raise.
        with pytest.raises(RuntimeError):
            supervisor._pick_ports((free_port, free_port), "127.0.0.1", 2)


# ---------------------------------------------------------------------------
# Options resolution
# ---------------------------------------------------------------------------


class TestResolveOptions:
    def _ns(self, **overrides):
        import argparse
        defaults = dict(
            fastapi=False,
            manual_start=False,
            fastapi_port_range=None,
            fastapi_host=None,
            fastapi_token=None,
            auto_start_delay=None,
            auto_start_nudge_delay=None,
            auto_start_ready_timeout=None,
            start_prompt_delay=None,
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_defaults(self, monkeypatch):
        for k in (
            "LMER_FASTAPI", "LMER_MANUAL_START", "LMER_FASTAPI_PORT_RANGE",
            "LMER_FASTAPI_HOST", "LMER_FASTAPI_TOKEN", "LMER_AUTO_START_DELAY",
            "LMER_AUTO_START_NUDGE_DELAY", "LMER_AUTO_START_READY_TIMEOUT",
            "LMER_AUTO_START_SETTLE_DELAY", "LMER_AUTO_START_READY_MARKER",
            "LMER_WINSIZE_RECHECK_DELAY", "LMER_START_PROMPT",
            "LMER_START_PROMPT_DELAY",
        ):
            monkeypatch.delenv(k, raising=False)
        opts = supervisor._resolve_options(self._ns())
        assert opts["fastapi"] is False
        assert opts["manual_start"] is False
        assert opts["port_range"] == supervisor.DEFAULT_PORT_RANGE
        assert opts["host"] == supervisor.DEFAULT_FASTAPI_HOST
        assert opts["token"] == ""
        assert opts["auto_start_delay"] == supervisor.DEFAULT_AUTO_START_DELAY
        assert opts["auto_start_nudge_delay"] == supervisor.DEFAULT_AUTO_START_NUDGE_DELAY
        assert opts["auto_start_ready_timeout"] == supervisor.DEFAULT_AUTO_START_READY_TIMEOUT
        assert opts["auto_start_ready_marker"] == supervisor.DEFAULT_AUTO_START_READY_MARKER
        assert opts["auto_start_settle_delay"] == supervisor.DEFAULT_AUTO_START_SETTLE_DELAY
        assert opts["start_prompt"] == ""
        assert opts["start_prompt_delay"] == supervisor.DEFAULT_START_PROMPT_DELAY

    def test_start_prompt_from_env(self, monkeypatch):
        monkeypatch.setenv("LMER_START_PROMPT", "research X online first")
        opts = supervisor._resolve_options(self._ns())
        assert opts["start_prompt"] == "research X online first"

    def test_start_prompt_blank_env_is_empty(self, monkeypatch):
        # Explicit empty string is treated the same as unset: no follow-up.
        monkeypatch.setenv("LMER_START_PROMPT", "")
        opts = supervisor._resolve_options(self._ns())
        assert opts["start_prompt"] == ""

    def test_nudge_delay_from_env(self, monkeypatch):
        monkeypatch.delenv("LMER_AUTO_START_NUDGE_DELAY", raising=False)
        monkeypatch.setenv("LMER_AUTO_START_NUDGE_DELAY", "0.25")
        opts = supervisor._resolve_options(self._ns())
        assert opts["auto_start_nudge_delay"] == 0.25

    def test_nudge_delay_cli_overrides_env(self, monkeypatch):
        monkeypatch.setenv("LMER_AUTO_START_NUDGE_DELAY", "0.25")
        opts = supervisor._resolve_options(self._ns(auto_start_nudge_delay=0.75))
        assert opts["auto_start_nudge_delay"] == 0.75

    def test_ready_timeout_from_env(self, monkeypatch):
        monkeypatch.setenv("LMER_AUTO_START_READY_TIMEOUT", "7.5")
        opts = supervisor._resolve_options(self._ns())
        assert opts["auto_start_ready_timeout"] == 7.5

    def test_ready_timeout_cli_overrides_env(self, monkeypatch):
        monkeypatch.setenv("LMER_AUTO_START_READY_TIMEOUT", "3")
        opts = supervisor._resolve_options(self._ns(auto_start_ready_timeout=12.0))
        assert opts["auto_start_ready_timeout"] == 12.0

    def test_settle_delay_from_env(self, monkeypatch):
        monkeypatch.setenv("LMER_AUTO_START_SETTLE_DELAY", "0.1")
        opts = supervisor._resolve_options(self._ns())
        assert opts["auto_start_settle_delay"] == 0.1

    def test_start_prompt_delay_from_env(self, monkeypatch):
        monkeypatch.setenv("LMER_START_PROMPT_DELAY", "3.5")
        opts = supervisor._resolve_options(self._ns())
        assert opts["start_prompt_delay"] == 3.5

    def test_start_prompt_delay_cli_overrides_env(self, monkeypatch):
        monkeypatch.setenv("LMER_START_PROMPT_DELAY", "3.5")
        opts = supervisor._resolve_options(self._ns(start_prompt_delay=0.75))
        assert opts["start_prompt_delay"] == 0.75

    def test_start_prompt_delay_zero_from_env(self, monkeypatch):
        # Explicit 0 must be honored (restores the old near-immediate behavior),
        # not treated as unset/falling back to the default.
        monkeypatch.setenv("LMER_START_PROMPT_DELAY", "0")
        opts = supervisor._resolve_options(self._ns())
        assert opts["start_prompt_delay"] == 0.0

    def test_ready_marker_from_env(self, monkeypatch):
        # Override with a unicode string so we verify UTF-8 encoding too.
        monkeypatch.setenv("LMER_AUTO_START_READY_MARKER", "READY→")
        opts = supervisor._resolve_options(self._ns())
        assert opts["auto_start_ready_marker"] == "READY→".encode("utf-8")

    def test_ready_marker_empty_env_disables_gating(self, monkeypatch):
        # Empty string is a sentinel for "no marker" — _wait_for_ready_marker
        # short-circuits on falsy marker.
        monkeypatch.setenv("LMER_AUTO_START_READY_MARKER", "")
        opts = supervisor._resolve_options(self._ns())
        assert opts["auto_start_ready_marker"] == b""

    def test_malformed_ready_marker_falls_back_to_profile(self, monkeypatch, capsys):
        """Review on !154: the marker decode moved from `.encode("utf-8")`
        (never raises) to decode_escape_bytes, which raises UnicodeDecodeError
        on a lone trailing backslash — that must not kill the supervisor at
        startup, it must degrade to the harness default."""
        monkeypatch.setenv("LMER_AUTO_START_READY_MARKER", "ready\\")
        opts = supervisor._resolve_options(self._ns())
        assert opts["auto_start_ready_marker"] == supervisor.DEFAULT_AUTO_START_READY_MARKER
        err = capsys.readouterr().err
        assert "LMER_AUTO_START_READY_MARKER" in err
        assert "cannot decode" in err

    def test_malformed_quit_sequence_falls_back_to_profile(self, monkeypatch, capsys):
        # Same shared decode, same hazard: LMER_QUIT_SEQUENCE must degrade too.
        monkeypatch.setenv("LMER_QUIT_SEQUENCE", "\\x03|/quit\\")
        opts = supervisor._resolve_options(self._ns())
        assert opts["quit_sequence"] == supervisor._resolve_harness_profile().quit_sequence
        assert "LMER_QUIT_SEQUENCE" in capsys.readouterr().err

    def test_env_enables_fastapi(self, monkeypatch):
        monkeypatch.setenv("LMER_FASTAPI", "1")
        monkeypatch.setenv("LMER_MANUAL_START", "true")
        monkeypatch.setenv("LMER_FASTAPI_PORT_RANGE", "9000-9099")
        monkeypatch.setenv("LMER_FASTAPI_HOST", "0.0.0.0")
        monkeypatch.setenv("LMER_FASTAPI_TOKEN", "deadbeef")
        opts = supervisor._resolve_options(self._ns())
        assert opts["fastapi"] is True
        assert opts["manual_start"] is True
        assert opts["port_range"] == (9000, 9099)
        assert opts["host"] == "0.0.0.0"
        assert opts["token"] == "deadbeef"

    def test_cli_overrides_env(self, monkeypatch):
        monkeypatch.setenv("LMER_FASTAPI_HOST", "0.0.0.0")
        opts = supervisor._resolve_options(self._ns(fastapi_host="1.2.3.4"))
        assert opts["host"] == "1.2.3.4"


class TestContainerEnvPassthrough:
    def test_cli_env_dict_declares_auto_start_delay(self):
        """Guard: LMER_AUTO_START_DELAY must be in cli.py's container env dict.

        The supervisor reads this var inside the container, so a host-set value
        only takes effect if cli.py forwards it explicitly.
        """
        import re
        from pathlib import Path
        cli_py = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"
        source = cli_py.read_text()
        pattern = re.compile(
            r"""["']LMER_AUTO_START_DELAY["']\s*:\s*os\.environ\.get\(\s*["']LMER_AUTO_START_DELAY["']\s*\)"""
        )
        assert pattern.search(source), \
            "LMER_AUTO_START_DELAY entry missing from cli.py container env dict"

    def test_cli_env_dict_declares_auto_start_nudge_delay(self):
        """Guard: LMER_AUTO_START_NUDGE_DELAY must be in cli.py's container env dict.

        The supervisor reads this var inside the container, so a host-set value
        only takes effect if cli.py forwards it explicitly.
        """
        import re
        from pathlib import Path
        cli_py = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"
        source = cli_py.read_text()
        pattern = re.compile(
            r"""["']LMER_AUTO_START_NUDGE_DELAY["']\s*:\s*os\.environ\.get\(\s*["']LMER_AUTO_START_NUDGE_DELAY["']\s*\)"""
        )
        assert pattern.search(source), \
            "LMER_AUTO_START_NUDGE_DELAY entry missing from cli.py container env dict"

    def test_cli_env_dict_declares_auto_start_ready_timeout(self):
        """Guard: LMER_AUTO_START_READY_TIMEOUT must be in cli.py's container env dict."""
        import re
        from pathlib import Path
        cli_py = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"
        source = cli_py.read_text()
        pattern = re.compile(
            r"""["']LMER_AUTO_START_READY_TIMEOUT["']\s*:\s*os\.environ\.get\(\s*["']LMER_AUTO_START_READY_TIMEOUT["']\s*\)"""
        )
        assert pattern.search(source), \
            "LMER_AUTO_START_READY_TIMEOUT entry missing from cli.py container env dict"

    def test_cli_env_dict_declares_auto_start_ready_marker(self):
        """Guard: LMER_AUTO_START_READY_MARKER must be in cli.py's container env dict."""
        import re
        from pathlib import Path
        cli_py = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"
        source = cli_py.read_text()
        pattern = re.compile(
            r"""["']LMER_AUTO_START_READY_MARKER["']\s*:\s*os\.environ\.get\(\s*["']LMER_AUTO_START_READY_MARKER["']\s*\)"""
        )
        assert pattern.search(source), \
            "LMER_AUTO_START_READY_MARKER entry missing from cli.py container env dict"

    def test_cli_env_dict_declares_start_prompt(self):
        """Guard: LMER_START_PROMPT must be in cli.py's container env dict.

        Unlike the other supervisor vars it is sourced from the --prompt CLI
        arg (ns.prompt), not os.environ — so the guard matches that pattern.
        """
        import re
        from pathlib import Path
        cli_py = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"
        source = cli_py.read_text()
        pattern = re.compile(
            r"""["']LMER_START_PROMPT["']\s*:\s*ns\.prompt"""
        )
        assert pattern.search(source), \
            "LMER_START_PROMPT entry missing from cli.py container env dict"

    def test_cli_env_dict_declares_start_prompt_delay(self):
        """Guard: LMER_START_PROMPT_DELAY must be in cli.py's container env dict.

        The supervisor reads this var inside the container, so a host-set value
        only takes effect if cli.py forwards it explicitly.
        """
        import re
        from pathlib import Path
        cli_py = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"
        source = cli_py.read_text()
        pattern = re.compile(
            r"""["']LMER_START_PROMPT_DELAY["']\s*:\s*os\.environ\.get\(\s*["']LMER_START_PROMPT_DELAY["']\s*\)"""
        )
        assert pattern.search(source), \
            "LMER_START_PROMPT_DELAY entry missing from cli.py container env dict"

    def test_cli_env_dict_declares_auto_start_settle_delay(self):
        """Guard: LMER_AUTO_START_SETTLE_DELAY must be in cli.py's container env dict.

        The supervisor reads this var inside the container, so a host-set value
        only takes effect if cli.py forwards it explicitly.
        """
        import re
        from pathlib import Path
        cli_py = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"
        source = cli_py.read_text()
        pattern = re.compile(
            r"""["']LMER_AUTO_START_SETTLE_DELAY["']\s*:\s*os\.environ\.get\(\s*["']LMER_AUTO_START_SETTLE_DELAY["']\s*\)"""
        )
        assert pattern.search(source), \
            "LMER_AUTO_START_SETTLE_DELAY entry missing from cli.py container env dict"

    def test_cli_env_dict_declares_winsize_recheck_delay(self):
        """Guard: LMER_WINSIZE_RECHECK_DELAY must be in cli.py's container env dict.

        The supervisor reads this var inside the container, so a host-set value
        only takes effect if cli.py forwards it explicitly.
        """
        import re
        from pathlib import Path
        cli_py = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"
        source = cli_py.read_text()
        pattern = re.compile(
            r"""["']LMER_WINSIZE_RECHECK_DELAY["']\s*:\s*os\.environ\.get\(\s*["']LMER_WINSIZE_RECHECK_DELAY["']\s*\)"""
        )
        assert pattern.search(source), \
            "LMER_WINSIZE_RECHECK_DELAY entry missing from cli.py container env dict"

    def test_cli_env_dict_declares_start_command(self):
        """Guard: LMER_START_COMMAND must be in cli.py's container env dict.

        HARNESSES.md promises every supervisor-profile field has an env
        override that works without a release; the in-container supervisor
        only sees a host-exported value if cli.py forwards it explicitly.
        """
        import re
        from pathlib import Path
        cli_py = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"
        source = cli_py.read_text()
        pattern = re.compile(
            r"""["']LMER_START_COMMAND["']\s*:\s*os\.environ\.get\(\s*["']LMER_START_COMMAND["']\s*\)"""
        )
        assert pattern.search(source), \
            "LMER_START_COMMAND entry missing from cli.py container env dict"

    def test_cli_env_dict_declares_quit_sequence(self):
        """Guard: LMER_QUIT_SEQUENCE must be in cli.py's container env dict.

        HARNESSES.md promises every supervisor-profile field has an env
        override that works without a release; the in-container supervisor
        only sees a host-exported value if cli.py forwards it explicitly.
        """
        import re
        from pathlib import Path
        cli_py = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"
        source = cli_py.read_text()
        pattern = re.compile(
            r"""["']LMER_QUIT_SEQUENCE["']\s*:\s*os\.environ\.get\(\s*["']LMER_QUIT_SEQUENCE["']\s*\)"""
        )
        assert pattern.search(source), \
            "LMER_QUIT_SEQUENCE entry missing from cli.py container env dict"


class TestSetWinsize:
    """Cover the strict/forgiving split on the TIOCSWINSZ helper."""

    def _non_tty_fd(self, tmp_path) -> int:
        """An fd whose TIOCSWINSZ fails for real (ENOTTY), no PTY teardown race."""
        regular = tmp_path / "not-a-tty"
        regular.write_bytes(b"")
        return os.open(str(regular), os.O_RDWR)

    def test_applies_geometry_to_a_pty(self):
        master, slave = os.openpty()
        try:
            supervisor._set_winsize(master, 44, 132)
            assert supervisor._get_winsize(slave) == (44, 132)
        finally:
            os.close(master)
            os.close(slave)

    def test_default_swallows_ioctl_failure(self, tmp_path):
        # Pins the host-TTY callers' contract: the SIGWINCH handler and the
        # post-launch recheck timer race a PTY that may already be gone and must
        # not raise into a signal handler or a timer thread.
        fd = self._non_tty_fd(tmp_path)
        try:
            supervisor._set_winsize(fd, 24, 80)
        finally:
            os.close(fd)

    def test_strict_propagates_ioctl_failure(self, tmp_path):
        # The /resize caller owes the client an answer, so it opts into the raise.
        fd = self._non_tty_fd(tmp_path)
        try:
            with pytest.raises(OSError):
                supervisor._set_winsize(fd, 24, 80, strict=True)
        finally:
            os.close(fd)


class TestPreconfigurePtyForInjection:
    """Cover the cooked-mode race fix: ICRNL/ECHO/ICANON cleared pre-fork."""

    def _new_pty(self) -> tuple[int, int]:
        master, slave = os.openpty()
        # Sanity: a fresh PTY starts with the flags we intend to clear ON.
        attrs = termios.tcgetattr(slave)
        assert attrs[0] & termios.ICRNL
        assert attrs[3] & termios.ECHO
        assert attrs[3] & termios.ICANON
        return master, slave

    def test_clears_input_and_local_flags(self):
        master, slave = self._new_pty()
        try:
            supervisor._preconfigure_pty_for_injection(slave)
            attrs = termios.tcgetattr(slave)
            # Input flags: CR↔NL translation off so injected \r survives.
            assert not (attrs[0] & termios.ICRNL)
            assert not (attrs[0] & termios.INLCR)
            assert not (attrs[0] & termios.IGNCR)
            # Local flags: echo off so injection doesn't render on the host TTY;
            # ICANON off so the kernel doesn't line-buffer the injection.
            assert not (attrs[3] & termios.ECHO)
            assert not (attrs[3] & termios.ICANON)
        finally:
            os.close(master)
            os.close(slave)

    def test_cr_survives_after_preconfigure(self):
        """End-to-end: a CR written to master reaches slave as CR (not LF)."""
        master, slave = self._new_pty()
        try:
            supervisor._preconfigure_pty_for_injection(slave)
            os.write(master, b"/start\r")
            data = os.read(slave, 64)
            # Without the pre-config, ICRNL would have turned this into b"/start\n".
            assert data == b"/start\r"
        finally:
            os.close(master)
            os.close(slave)

    def test_no_master_side_echo_after_preconfigure(self):
        """Bytes written to master must not echo back through master."""
        import select

        master, slave = self._new_pty()
        try:
            supervisor._preconfigure_pty_for_injection(slave)
            os.write(master, b"/start\r")
            # Drain the slave-bound delivery so any echo would be the only thing
            # left for master to read.
            os.read(slave, 64)
            rlist, _, _ = select.select([master], [], [], 0.05)
            assert master not in rlist, \
                "ECHO leaked: master saw a readable echo of the injection"
        finally:
            os.close(master)
            os.close(slave)

    def test_swallows_termios_error_on_non_tty(self, tmp_path):
        """Helper must not crash if fd is not a TTY (e.g., a regular file)."""
        regular = tmp_path / "not-a-tty"
        regular.write_bytes(b"")
        fd = os.open(str(regular), os.O_RDWR)
        try:
            # Should not raise even though fd is not a terminal.
            supervisor._preconfigure_pty_for_injection(fd)
        finally:
            os.close(fd)


class TestWaitForReadyMarker:
    """Cover the prompt-ready gating that defers /start until claude is ready.

    Claude Code v2.1.119 changed Enter routing so an open modal/dialog (theme
    picker, IDE detect, permission prompt, etc.) consumes the submit CR rather
    than also submitting input-box text. Until claude finishes its startup
    chain and renders the input prompt glyph, our `/start\\r` injection sits
    typed but unsubmitted. The helper waits for that glyph before we inject.
    """

    def test_returns_true_when_marker_present(self):
        buf = supervisor.OutputBuffer(limit=1024)
        buf.append(b"banner\n\xe2\x9d\xaf ")  # "❯ " — the prompt glyph
        assert supervisor._wait_for_ready_marker(buf, b"\xe2\x9d\xaf", 1.0) is True

    def test_returns_true_when_marker_arrives_late(self):
        buf = supervisor.OutputBuffer(limit=1024)

        def producer():
            time.sleep(0.05)
            buf.append(b"some startup output ")
            time.sleep(0.05)
            buf.append(b"\xe2\x9d\xaf ")

        threading.Thread(target=producer, daemon=True).start()
        start = time.monotonic()
        ok = supervisor._wait_for_ready_marker(buf, b"\xe2\x9d\xaf", 2.0)
        elapsed = time.monotonic() - start
        assert ok is True
        assert elapsed < 1.0

    def test_returns_false_on_timeout(self):
        buf = supervisor.OutputBuffer(limit=1024)
        buf.append(b"no marker here at all")
        start = time.monotonic()
        ok = supervisor._wait_for_ready_marker(buf, b"\xe2\x9d\xaf", 0.15)
        elapsed = time.monotonic() - start
        assert ok is False
        assert elapsed >= 0.15

    def test_detects_marker_spanning_chunk_boundary(self):
        """Marker straddling two chunks must still match — the wait helper
        keeps a small tail of previously-seen bytes specifically for this."""
        buf = supervisor.OutputBuffer(limit=1024)

        def producer():
            # ``❯`` is three bytes (0xE2, 0x9D, 0xAF). Split the marker across
            # two appends so the first read returns only its prefix.
            buf.append(b"prefix\xe2\x9d")
            time.sleep(0.05)
            buf.append(b"\xaf rest")

        threading.Thread(target=producer, daemon=True).start()
        ok = supervisor._wait_for_ready_marker(buf, b"\xe2\x9d\xaf", 2.0)
        assert ok is True

    def test_zero_timeout_skips_wait(self):
        """timeout<=0 disables marker gating — return immediately as True so
        callers can opt out of marker-based readiness by configuration."""
        buf = supervisor.OutputBuffer(limit=1024)
        assert supervisor._wait_for_ready_marker(buf, b"\xe2\x9d\xaf", 0) is True
        assert supervisor._wait_for_ready_marker(buf, b"\xe2\x9d\xaf", -1.0) is True

    def test_empty_marker_skips_wait(self):
        buf = supervisor.OutputBuffer(limit=1024)
        assert supervisor._wait_for_ready_marker(buf, b"", 1.0) is True

    def test_cancel_event_breaks_out_promptly(self):
        """Cancel during a long marker wait must return False quickly, not
        block for the full timeout. The helper polls on a bounded cadence
        specifically so shutdown propagates within ~poll seconds."""
        buf = supervisor.OutputBuffer(limit=1024)
        cancel = threading.Event()

        def trip():
            time.sleep(0.05)
            cancel.set()

        threading.Thread(target=trip, daemon=True).start()
        start = time.monotonic()
        ok = supervisor._wait_for_ready_marker(
            buf, b"\xe2\x9d\xaf", 5.0, cancel=cancel
        )
        elapsed = time.monotonic() - start
        assert ok is False
        # Generous upper bound: poll interval is 0.2s, trip fires at +0.05s,
        # so worst case we wake on the next poll boundary around +0.25s.
        assert elapsed < 1.0


class TestInjectAutoStart:
    def test_sends_start_then_nudges(self):
        sink: list[bytes] = []
        supervisor._inject_auto_start(
            lambda data: (sink.append(data), len(data))[1],
            nudge_count=3,
            nudge_delay=0,
        )
        # /start\r once, followed by exactly one bare \r per nudge.
        assert sink == [b"/start\r", b"\r", b"\r", b"\r"]

    def test_no_nudges_when_count_zero(self):
        sink: list[bytes] = []
        supervisor._inject_auto_start(
            lambda data: (sink.append(data), len(data))[1],
            nudge_count=0,
            nudge_delay=0,
        )
        assert sink == [b"/start\r"]

    def test_oserror_from_write_is_suppressed(self):
        def boom(_data: bytes) -> int:
            raise OSError("PTY closed")

        # A closed PTY mid-injection must not propagate out of the timer thread.
        supervisor._inject_auto_start(boom, nudge_count=2, nudge_delay=0)

    def test_negative_nudge_delay_never_sleeps(self, monkeypatch):
        # Documented contract: a negative delay is clamped — time.sleep must
        # never be called with a negative value (which would raise ValueError),
        # while all nudges are still sent.
        slept: list[float] = []
        monkeypatch.setattr(supervisor.time, "sleep", lambda s: slept.append(s))
        sink: list[bytes] = []
        supervisor._inject_auto_start(
            lambda data: (sink.append(data), len(data))[1],
            nudge_count=3,
            nudge_delay=-1.0,
        )
        assert slept == []
        assert sink == [b"/start\r", b"\r", b"\r", b"\r"]


class TestInjectStartPrompt:
    def test_sends_prompt_with_trailing_cr(self):
        sink: list[bytes] = []
        supervisor._inject_start_prompt(
            lambda data: (sink.append(data), len(data))[1],
            "research X online first",
        )
        # Single payload: text + CR (Enter in claude's raw-mode TUI).
        assert sink == ["research X online first\r".encode("utf-8")]

    def test_does_not_double_terminate_cr(self):
        sink: list[bytes] = []
        supervisor._inject_start_prompt(
            lambda data: (sink.append(data), len(data))[1],
            "do the thing\r",
        )
        assert sink == [b"do the thing\r"]

    def test_a_trailing_lf_still_gets_a_real_enter(self):
        """The caller's newline is kept — it is what they asked for — and the
        submit CR goes behind it, because LF is not Enter in raw mode."""
        sink: list[bytes] = []
        supervisor._inject_start_prompt(
            lambda data: (sink.append(data), len(data))[1],
            "do the thing\n",
        )
        assert sink == [b"do the thing\n\r"]

    def test_empty_prompt_is_noop(self):
        sink: list[bytes] = []
        supervisor._inject_start_prompt(
            lambda data: (sink.append(data), len(data))[1],
            "",
        )
        assert sink == []

    def test_encodes_unicode_as_utf8(self):
        sink: list[bytes] = []
        supervisor._inject_start_prompt(
            lambda data: (sink.append(data), len(data))[1],
            "café ☕",
        )
        assert sink == ["café ☕\r".encode("utf-8")]

    def test_sends_prompt_then_nudges(self):
        # Mirrors _inject_auto_start: the prompt's submit CR can be swallowed
        # during a startup re-render, so bare-CR nudges re-submit it.
        sink: list[bytes] = []
        supervisor._inject_start_prompt(
            lambda data: (sink.append(data), len(data))[1],
            "hello",
            nudge_count=3,
            nudge_delay=0,
        )
        assert sink == [b"hello\r", b"\r", b"\r", b"\r"]

    def test_no_nudges_when_count_zero(self):
        sink: list[bytes] = []
        supervisor._inject_start_prompt(
            lambda data: (sink.append(data), len(data))[1],
            "hello",
            nudge_count=0,
            nudge_delay=0,
        )
        assert sink == [b"hello\r"]

    def test_negative_nudge_delay_never_sleeps(self, monkeypatch):
        # A negative delay must be clamped — time.sleep is never called with a
        # negative value (which would raise ValueError) while nudges still send.
        slept: list[float] = []
        monkeypatch.setattr(supervisor.time, "sleep", lambda s: slept.append(s))
        sink: list[bytes] = []
        supervisor._inject_start_prompt(
            lambda data: (sink.append(data), len(data))[1],
            "hello",
            nudge_count=2,
            nudge_delay=-1.0,
        )
        assert slept == []
        assert sink == [b"hello\r", b"\r", b"\r"]

    def test_empty_prompt_is_noop_even_with_nudges(self):
        sink: list[bytes] = []
        supervisor._inject_start_prompt(
            lambda data: (sink.append(data), len(data))[1],
            "",
            nudge_count=3,
            nudge_delay=0,
        )
        assert sink == []

    def test_oserror_from_write_is_suppressed(self):
        def boom(_data: bytes) -> int:
            raise OSError("PTY closed")

        # A closed PTY mid-injection must not propagate out of the timer thread.
        supervisor._inject_start_prompt(boom, "anything", nudge_count=2)


class TestInjectShutdownChord:
    """The self-shutdown quit chord: Ctrl-C (\\x03) twice with a gap."""

    def test_sends_ctrl_c_twice(self):
        sink: list[bytes] = []
        supervisor._inject_shutdown_chord(
            lambda data: (sink.append(data), len(data))[1], gap=0
        )
        assert sink == [b"\x03", b"\x03"]

    def test_sleeps_the_gap_between_presses(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr(supervisor.time, "sleep", lambda s: slept.append(s))
        sink: list[bytes] = []
        supervisor._inject_shutdown_chord(
            lambda data: (sink.append(data), len(data))[1], gap=0.5
        )
        assert slept == [0.5]
        assert sink == [b"\x03", b"\x03"]

    def test_non_positive_gap_never_sleeps(self, monkeypatch):
        # A negative/zero gap must not call time.sleep (negative would raise).
        slept: list[float] = []
        monkeypatch.setattr(supervisor.time, "sleep", lambda s: slept.append(s))
        sink: list[bytes] = []
        supervisor._inject_shutdown_chord(
            lambda data: (sink.append(data), len(data))[1], gap=-1.0
        )
        assert slept == []
        assert sink == [b"\x03", b"\x03"]

    def test_oserror_from_write_is_suppressed(self):
        def boom(_data: bytes) -> int:
            raise OSError("PTY closed")

        # A closed PTY mid-chord must not propagate out of the daemon thread.
        supervisor._inject_shutdown_chord(boom, gap=0)


class TestChildAlive:
    """_child_alive probes via kill(pid, 0) and never reaps."""

    def test_true_for_self(self):
        assert supervisor._child_alive(os.getpid()) is True

    def test_false_when_process_lookup_error(self, monkeypatch):
        def _no_such(_pid, _sig):
            raise ProcessLookupError

        monkeypatch.setattr(supervisor.os, "kill", _no_such)
        assert supervisor._child_alive(424242) is False

    def test_true_when_permission_error(self, monkeypatch):
        def _eperm(_pid, _sig):
            raise PermissionError

        monkeypatch.setattr(supervisor.os, "kill", _eperm)
        # Exists but unsignalable — fail safe to "alive" rather than escalate.
        assert supervisor._child_alive(1) is True


class TestWaitChildExit:
    def test_returns_true_when_already_gone(self, monkeypatch):
        monkeypatch.setattr(supervisor, "_child_alive", lambda _pid: False)
        assert supervisor._wait_child_exit(123, timeout=5.0) is True

    def test_returns_false_on_timeout(self, monkeypatch):
        clock = {"now": 0.0}
        monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock["now"])
        monkeypatch.setattr(
            supervisor.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + s)
        )
        monkeypatch.setattr(supervisor, "_child_alive", lambda _pid: True)
        assert supervisor._wait_child_exit(123, timeout=1.0) is False

    def test_returns_true_when_child_exits_partway(self, monkeypatch):
        clock = {"now": 0.0}
        calls = {"n": 0}
        monkeypatch.setattr(supervisor.time, "monotonic", lambda: clock["now"])
        monkeypatch.setattr(
            supervisor.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + s)
        )

        def _alive(_pid):
            calls["n"] += 1
            return calls["n"] < 3  # alive for two probes, then gone

        monkeypatch.setattr(supervisor, "_child_alive", _alive)
        assert supervisor._wait_child_exit(123, timeout=10.0) is True


class TestSelfShutdown:
    """_self_shutdown escalates quit-chord -> SIGTERM -> SIGKILL until exit."""

    def test_chord_only_when_child_exits_promptly(self, monkeypatch):
        chords: list[bool] = []
        monkeypatch.setattr(
            supervisor, "_inject_shutdown_chord", lambda w, g, s: chords.append(True)
        )
        monkeypatch.setattr(supervisor, "_wait_child_exit", lambda pid, t: True)
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(
            supervisor.os, "kill", lambda pid, sig: killed.append((pid, sig))
        )

        supervisor._self_shutdown(lambda d: len(d), 4321)

        assert chords == [True]
        assert killed == []  # chord worked; no escalation

    def test_escalates_to_sigterm_then_sigkill(self, monkeypatch):
        monkeypatch.setattr(supervisor, "_inject_shutdown_chord", lambda w, g, s: None)
        # Child never exits on its own -> both waits time out.
        monkeypatch.setattr(supervisor, "_wait_child_exit", lambda pid, t: False)
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(
            supervisor.os, "kill", lambda pid, sig: killed.append((pid, sig))
        )

        supervisor._self_shutdown(lambda d: len(d), 4321)

        assert killed == [
            (4321, supervisor.signal.SIGTERM),
            (4321, supervisor.signal.SIGKILL),
        ]

    def test_escalates_to_sigterm_only_when_that_works(self, monkeypatch):
        monkeypatch.setattr(supervisor, "_inject_shutdown_chord", lambda w, g, s: None)
        waits = iter([False, True])  # chord fails, child dies after SIGTERM
        monkeypatch.setattr(supervisor, "_wait_child_exit", lambda pid, t: next(waits))
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(
            supervisor.os, "kill", lambda pid, sig: killed.append((pid, sig))
        )

        supervisor._self_shutdown(lambda d: len(d), 4321)

        assert killed == [(4321, supervisor.signal.SIGTERM)]


class TestStartAutoStartThread:
    """Cover the auto-start daemon's sequencing, especially the configurable
    gap before the follow-up prompt (issue #65)."""

    class _FakeCancel:
        """Stand-in for the cancel Event: records wait() durations, never set."""

        def __init__(self):
            self.waits: list[float] = []

        def wait(self, timeout):
            self.waits.append(timeout)
            return False

        def is_set(self):
            return False

    def _options(self, **overrides):
        opts = dict(
            auto_start_delay=0.0,
            auto_start_nudge_delay=0.0,
            auto_start_ready_marker=b"",  # empty → marker wait short-circuits
            auto_start_ready_timeout=0.0,
            auto_start_settle_delay=0.0,
            start_prompt="hello",
            start_prompt_delay=0.0,
        )
        opts.update(overrides)
        return opts

    def test_injects_start_then_prompt_in_order(self):
        sink: list[bytes] = []
        write = lambda data: (sink.append(bytes(data)), len(data))[1]
        cancel = self._FakeCancel()
        thread = supervisor._start_auto_start_thread(
            output=None, write=write, options=self._options(), cancel=cancel
        )
        thread.join(timeout=2)
        assert not thread.is_alive()
        # /start submitted first, then the follow-up prompt.
        assert sink[0] == b"/start\r"
        assert b"hello\r" in sink
        assert sink.index(b"/start\r") < sink.index(b"hello\r")

    def test_waits_start_prompt_delay_before_prompt(self):
        # The gap before the prompt must come from start_prompt_delay, not the
        # nudge delay — a distinctive value proves which knob gates it.
        sink: list[bytes] = []
        write = lambda data: (sink.append(bytes(data)), len(data))[1]
        cancel = self._FakeCancel()
        thread = supervisor._start_auto_start_thread(
            output=None,
            write=write,
            options=self._options(start_prompt_delay=0.123),
            cancel=cancel,
        )
        thread.join(timeout=2)
        assert 0.123 in cancel.waits
        assert b"hello\r" in sink

    def test_no_prompt_injection_when_unset(self):
        sink: list[bytes] = []
        write = lambda data: (sink.append(bytes(data)), len(data))[1]
        cancel = self._FakeCancel()
        thread = supervisor._start_auto_start_thread(
            output=None,
            write=write,
            options=self._options(start_prompt="", start_prompt_delay=5.0),
            cancel=cancel,
        )
        thread.join(timeout=2)
        # /start still injected, but no prompt and no prompt-delay wait.
        assert sink[0] == b"/start\r"
        assert all(b"hello" not in chunk for chunk in sink)
        assert 5.0 not in cancel.waits


class TestSessionLog:
    """The session's own log — the copy the host cannot take away (#150).

    Three of these are the contract the platform's read path depends on rather
    than merely nice properties: nothing is written when nothing was mounted (a
    plain ``lmer`` run on a laptop must be untouched), the directory is never
    created here (its existence is what the platform asks with), and a log that
    cannot be appended to is *removed* rather than left frozen — because the file
    is what tells the reader "this is the record", and a truncated file making that
    claim is worse than no file at all.
    """

    def test_nothing_is_written_when_nothing_is_mounted(self, tmp_path):
        missing = tmp_path / "not-mounted"
        assert supervisor.SessionLog.open_if_mounted(str(missing)) is None
        assert not missing.exists(), "the mount point is a question, not ours to create"

    def test_a_file_is_a_mount_point_this_declines(self, tmp_path):
        """A path that is not a directory is not a mount; it is not written into."""
        decoy = tmp_path / "decoy"
        decoy.write_bytes(b"")
        assert supervisor.SessionLog.open_if_mounted(str(decoy)) is None

    def test_a_mounted_directory_is_opened_before_anything_is_written(self, tmp_path):
        log = supervisor.SessionLog.open_if_mounted(str(tmp_path))
        try:
            assert log is not None
            assert log.path == str(tmp_path / supervisor.SESSION_LOG_NAME)
            assert os.path.exists(log.path), "opened at startup, not at the first byte"
        finally:
            log.close()

    def test_the_log_is_owner_only(self, tmp_path):
        """It holds every byte the session drew, in a bind-mounted directory."""
        log = supervisor.SessionLog.open_if_mounted(str(tmp_path))
        try:
            mode = stat.S_IMODE(os.stat(log.path).st_mode)
            assert mode == supervisor.SESSION_LOG_MODE, f"got {oct(mode)}"
        finally:
            log.close()

    def test_bytes_are_readable_before_the_log_is_closed(self, tmp_path):
        """The reader is another process tailing a running session."""
        log = supervisor.SessionLog.open_if_mounted(str(tmp_path))
        try:
            log.write(b"first")
            assert (tmp_path / supervisor.SESSION_LOG_NAME).read_bytes() == b"first"
            log.write(b"second")
            assert (
                tmp_path / supervisor.SESSION_LOG_NAME
            ).read_bytes() == b"firstsecond"
        finally:
            log.close()

    def test_an_existing_log_is_appended_to(self, tmp_path):
        (tmp_path / supervisor.SESSION_LOG_NAME).write_bytes(b"earlier\n")
        log = supervisor.SessionLog.open_if_mounted(str(tmp_path))
        try:
            log.write(b"later")
        finally:
            log.close()
        assert (
            tmp_path / supervisor.SESSION_LOG_NAME
        ).read_bytes() == b"earlier\nlater"

    def test_a_write_that_cannot_land_removes_the_log(self, tmp_path, monkeypatch):
        """The file's claim is withdrawn, so the reader falls back to the host tee."""
        log = supervisor.SessionLog.open_if_mounted(str(tmp_path))
        log.write(b"recorded")
        real_write = os.write

        def refuse(fd, data):
            # Scoped to this log's fd: everything else in the process (pytest's
            # own capture included) still has a working os.write.
            if fd == log._fd:
                raise OSError(errno.ENOSPC, "no space left on device")
            return real_write(fd, data)

        monkeypatch.setattr(supervisor.os, "write", refuse)
        log.write(b"lost")

        assert not (tmp_path / supervisor.SESSION_LOG_NAME).exists()

    def test_writing_to_an_abandoned_log_is_a_no_op(self, tmp_path, monkeypatch):
        """A session must not die of a second failure to log."""
        log = supervisor.SessionLog.open_if_mounted(str(tmp_path))
        monkeypatch.setattr(
            supervisor.os, "write", lambda *a: (_ for _ in ()).throw(OSError("gone"))
        )
        log.write(b"one")
        log.write(b"two")  # no exception, and nothing reopened
        log.close()
        assert not (tmp_path / supervisor.SESSION_LOG_NAME).exists()

    def test_closing_keeps_the_file(self, tmp_path):
        """The session ended; its log is exactly what somebody now wants to read."""
        log = supervisor.SessionLog.open_if_mounted(str(tmp_path))
        log.write(b"history")
        log.close()
        log.close()  # idempotent
        assert (tmp_path / supervisor.SESSION_LOG_NAME).read_bytes() == b"history"


class TestWriteAll:
    """A message reaches the session whole, or the write raises (issue 194).

    The failure this closes is the quietest one in the delivery path: ``os.write``
    may write less than it was given, and the *last* byte of an ``/input`` payload
    is the submit CR — so a short write is not a slow PTY, it is a partial message
    typed into the box and never sent, under a 200 with a byte count over it. The
    view on the other end had no way to know (:mod:`tests.test_platform_web_chat`
    holds that half).
    """

    def test_a_short_write_is_followed_up_until_the_payload_is_gone(self, monkeypatch):
        chunks = []
        real_write = supervisor.os.write
        read_fd, write_fd = os.pipe()

        def stingy(fd, data):
            # The kernel's prerogative, made deterministic: at most 4 bytes a call.
            # Scoped to this pipe, because ``supervisor.os`` *is* the os module —
            # a process-wide truncation would silently clip pytest's own capture
            # writes and the test that noticed would be some other one (same
            # discipline as the log's fd-scoped fake above).
            if fd != write_fd:
                return real_write(fd, data)
            head = bytes(data[:4])
            chunks.append(head)
            return real_write(fd, head)

        monkeypatch.setattr(supervisor.os, "write", stingy)
        try:
            payload = b"a message long enough to be split, and its Enter\r"
            written = supervisor._write_all(write_fd, payload)
        finally:
            monkeypatch.undo()
            os.close(write_fd)
            landed = os.read(read_fd, 4096)
            os.close(read_fd)

        assert landed == payload, (
            "the session received a truncated message; the tail carries the submit"
        )
        assert written == len(payload), (
            f"the reply would report {written} of {len(payload)} bytes as written"
        )
        assert len(chunks) > 1, "the fixture no longer forces a short write"

    def test_a_write_that_moves_nothing_raises_instead_of_spinning(self, monkeypatch):
        """A 0 the kernel should never answer must not become a wedged session: the
        write lock is held here, so a spin would take every other writer with it.

        The fake counts its own calls and gives up after a few, so losing the guard
        surfaces as a failure rather than as a hung suite — there is no
        ``pytest-timeout`` in this project, so an unbounded loop here would stall
        CI instead of turning it red. Scoped to a sentinel fd for the same reason
        the fake above is.
        """
        real_write = supervisor.os.write
        sentinel = 987654
        calls = []

        def stuck(fd, data):
            if fd != sentinel:
                return real_write(fd, data)
            calls.append(len(data))
            assert len(calls) < 4, "_write_all is spinning on a zero-length write"
            return 0

        monkeypatch.setattr(supervisor.os, "write", stuck)
        with pytest.raises(OSError) as caught:
            supervisor._write_all(sentinel, b"xyz")
        assert calls == [3], f"the guard did not stop at the first zero: {calls}"
        # The diagnostic has to name how much landed, not how much was left: a
        # message that died 43 bytes into 48 is a different repair from one that
        # never started, and this string is what reaches the operator through
        # ``/input``'s 500.
        assert "0 of 3 bytes" in str(caught.value), str(caught.value)


    def test_a_failure_part_way_through_says_how_much_landed(self, monkeypatch):
        """The one fact nobody downstream can recover for themselves.

        A write that dies between iterations — the child exiting under us — leaves
        the front of the message typed in the session's input box. "wrote 43 of 48"
        is the difference between that and a message the session never saw, and it
        is what ``/input``'s 500 detail carries to the operator.
        """
        real_write = supervisor.os.write
        sentinel = 987655
        seen = []

        def dies_after_the_first_chunk(fd, data):
            if fd != sentinel:
                return real_write(fd, data)
            if seen:
                raise OSError(errno.EIO, "the child is gone")
            seen.append(True)
            return 4

        monkeypatch.setattr(supervisor.os, "write", dies_after_the_first_chunk)
        with pytest.raises(OSError) as caught:
            supervisor._write_all(sentinel, b"a message and its Enter\r")
        assert "wrote 4 of 24 bytes" in str(caught.value), str(caught.value)

    def test_the_control_plane_write_goes_through_the_loop(self):
        """The wiring, not just the helper.

        ``TestWriteAll`` above exercises ``_write_all`` directly and the ``/input``
        route tests write into their own sink, so both halves can pass while the
        supervisor's real writer calls ``os.write`` once — which is the bug. So the
        call site itself is pinned: every writer into the child (``/input``, the
        auto-start injection, the host TTY relay) goes through ``write_to_child``,
        and ``write_to_child`` has to go through the loop.
        """
        import inspect

        source = inspect.getsource(supervisor.run_supervisor)
        assert "_write_all(master_fd, data)" in source, (
            "write_to_child no longer writes through _write_all, so a short write "
            "silently truncates whatever an operator typed"
        )

    def test_the_session_log_shares_the_loop(self):
        """One implementation of "write all of it", not two.

        The two had already diverged: only one refused a zero-length write, so the
        hole this closes on the input path stayed open on the log path. A second
        copy is how that happens again.
        """
        import inspect

        source = inspect.getsource(supervisor.SessionLog.write)
        assert "_write_all(" in source, (
            "SessionLog.write hand-rolls the loop again"
        )
        assert "os.write(" not in source, (
            "SessionLog.write still calls os.write directly"
        )


class TestEnsureSubmitCr:
    def test_appends_cr_when_missing(self):
        assert supervisor._ensure_submit_cr("hello") == "hello\r"

    def test_does_not_double_cr(self):
        assert supervisor._ensure_submit_cr("hello\r") == "hello\r"

    def test_an_lf_is_not_a_submit_and_gets_a_cr_behind_it(self):
        """LF in raw mode is a literal newline in the input box, not Enter, so
        treating it as "already submitted" left the text typed and unsent."""
        assert supervisor._ensure_submit_cr("hello\n") == "hello\n\r"

    def test_empty_string_gets_cr(self):
        assert supervisor._ensure_submit_cr("") == "\r"


class TestResolveFastApiPort:
    """Cover the production env→port resolution helper directly.

    The lmer host CLI pre-picks a free port and passes it via
    ``LMER_FASTAPI_PORT`` so the runtime can publish exactly that port. The
    supervisor must honor it instead of picking independently, otherwise the
    published port maps to nothing inside the container.
    """

    OPTIONS = {"port_range": (9000, 9100), "host": "127.0.0.1"}

    def test_uses_env_port_when_set(self):
        port = supervisor._resolve_fastapi_port(self.OPTIONS, {"LMER_FASTAPI_PORT": "9099"})
        assert port == 9099

    def test_falls_back_to_range_pick_when_unset(self):
        port = supervisor._resolve_fastapi_port(self.OPTIONS, {})
        assert 9000 <= port <= 9100

    def test_falls_back_to_range_pick_when_blank(self):
        port = supervisor._resolve_fastapi_port(self.OPTIONS, {"LMER_FASTAPI_PORT": "   "})
        assert 9000 <= port <= 9100

    def test_falls_back_to_range_pick_on_invalid(self):
        port = supervisor._resolve_fastapi_port(
            self.OPTIONS, {"LMER_FASTAPI_PORT": "not-a-number"}
        )
        assert 9000 <= port <= 9100

    def test_does_not_validate_env_port_against_range(self):
        # Deliberately outside (9000-9100) to confirm the helper trusts the
        # caller's pre-pick rather than re-validating it. The host CLI is the
        # authority on which port was reserved.
        port = supervisor._resolve_fastapi_port(self.OPTIONS, {"LMER_FASTAPI_PORT": "12345"})
        assert port == 12345


# ---------------------------------------------------------------------------
# Argument parsing / command splitting
# ---------------------------------------------------------------------------


class TestCli:
    def test_split_command_strips_separator(self):
        assert supervisor._split_command(["--", "claude", "-x"]) == ["claude", "-x"]

    def test_split_command_passthrough(self):
        assert supervisor._split_command(["claude", "-x"]) == ["claude", "-x"]

    def test_split_command_requires_command(self):
        with pytest.raises(SystemExit):
            supervisor._split_command([])

    def test_arg_parser_accepts_flags(self):
        parser = supervisor._build_arg_parser()
        ns = parser.parse_args([
            "--fastapi", "--manual-start",
            "--fastapi-port-range", "9000-9099",
            "--fastapi-host", "0.0.0.0",
            "--fastapi-token", "tok",
            "--auto-start-delay", "0.5",
            "--auto-start-nudge-delay", "0.2",
            "--auto-start-ready-timeout", "8.0",
            "--start-prompt-delay", "2.5",
            "--", "claude", "--foo",
        ])
        assert ns.fastapi is True
        assert ns.manual_start is True
        assert ns.fastapi_port_range == "9000-9099"
        assert ns.fastapi_host == "0.0.0.0"
        assert ns.fastapi_token == "tok"
        assert ns.auto_start_delay == 0.5
        assert ns.auto_start_nudge_delay == 0.2
        assert ns.auto_start_ready_timeout == 8.0
        assert ns.start_prompt_delay == 2.5
        assert ns.command == ["--", "claude", "--foo"]


# ---------------------------------------------------------------------------
# FastAPI app behavior
# ---------------------------------------------------------------------------


class TestFastApiApp:
    def _build(self, token="test-token", **app_kwargs):
        buf = supervisor.OutputBuffer(limit=1024)
        sink: list[bytes] = []

        def write_input(data: bytes) -> int:
            sink.append(data)
            return len(data)

        app = supervisor._build_fastapi_app(buf, write_input, token, **app_kwargs)
        return app, buf, sink

    def _build_resizable(self, token="test-token", resize=None, winsize=(24, 80)):
        """Build the app with a recording resize callable and a fixed geometry.

        Returns the recorded ``(rows, cols)`` calls so a test can show the route
        reached the PTY-facing callable — or, for the rejection cases, that it
        never did.
        """
        calls: list[tuple[int, int]] = []

        def record(rows: int, cols: int) -> None:
            calls.append((rows, cols))

        app, _buf, _sink = self._build(
            token=token,
            resize=record if resize is None else resize,
            get_winsize=lambda: winsize,
        )
        return app, calls

    def _client(self, app):
        from fastapi.testclient import TestClient
        return TestClient(app)

    def test_input_writes_to_sink(self):
        app, _buf, sink = self._build()
        client = self._client(app)
        resp = client.post(
            "/input",
            json={"data": "hello"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"bytes_written": 5}
        assert sink == [b"hello"]

    def test_input_appends_newline(self):
        app, _buf, sink = self._build()
        client = self._client(app)
        resp = client.post(
            "/input",
            json={"data": "/start", "append_newline": True},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        # Text plus its submit CR, and nothing after it. CR, not LF: raw mode
        # treats \r as Enter.
        assert sink == [b"/start\r"], f"something followed the submit: {sink}"

    def test_input_does_not_double_terminate(self):
        app, _buf, sink = self._build()
        client = self._client(app)
        # Caller already ended with \r — don't append a second one.
        resp = client.post(
            "/input",
            json={"data": "/start\r", "append_newline": True},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        # The caller's own CR is not doubled inside the typed text, and nothing
        # is written after it.
        assert sink == [b"/start\r"]
        sink.clear()
        # A legacy trailing \n keeps its newline and gains a real Enter behind
        # it. LF in raw mode is a literal newline, so treating it as "already
        # submitted" meant a caller who passed "text\n" with append_newline=True
        # got NO submit at all — while the field is documented as "press Enter
        # after the text". It used to be rescued by the follow-up nudges; with
        # those gone the CR has to be in the payload. A caller who genuinely
        # wants a literal newline and no Enter sends append_newline=False, which
        # is untouched and covered below.
        resp = client.post(
            "/input",
            json={"data": "/start\n", "append_newline": True},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        assert sink == [b"/start\n\r"], (
            "a legacy trailing LF is left alone in the typed text (it is what "
            f"the caller asked for) but a real Enter must follow it: {sink}"
        )

    def test_a_message_is_never_followed_by_a_blind_enter(self, monkeypatch):
        """The failure a follow-up CR here would cause, and why the remedy moved.

        A bare CR is a no-op only against an empty input box *with no dialog on
        screen*: since Claude Code v2.1.119 a CR fires the topmost modal, which
        is the routing change the auto-start path waits for a readiness marker to
        avoid. This handler runs mid-session — the moment a tool-permission
        prompt is up, because the agent raises one while it is working and the
        operator is watching. An operator typing "no, stop" would have their own
        message followed 150ms later by an Enter that takes the prompt's default,
        with nothing in the transcript saying a CR did it.

        So: exactly one write, no timer, and the uncertainty reported instead.
        """
        app, _buf, sink = self._build()
        client = self._client(app)
        waits = []
        monkeypatch.setattr(
            supervisor.time, "sleep",
            lambda seconds: waits.append((seconds, len(sink))),
        )

        resp = client.post(
            "/input",
            json={"data": "no, stop", "append_newline": True},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200

        assert sink == [b"no, stop\r"], f"something else reached the PTY: {sink}"
        assert waits == [], (
            f"a timer fired on the input path, so something follows it: {waits}"
        )

        body = resp.json()
        assert body["bytes_written"] == len(b"no, stop\r")
        assert body["submit_confirmed"] is False, (
            "the handler must not claim a submit it cannot observe"
        )
        assert "terminal view" in body["note"], (
            "the reply has to say what the caller can do about it"
        )

    def test_a_keystroke_reply_says_nothing_about_a_submit(self):
        """``append_newline=False`` presses no Enter, so there is no submit to
        be unsure about — and the terminal's per-keystroke path must not gain a
        note it would have to filter out of every reply."""
        app, _buf, _sink = self._build()
        client = self._client(app)
        body = client.post(
            "/input",
            json={"data": "\x1b[A", "append_newline": False},
            headers={"Authorization": "Bearer test-token"},
        ).json()
        assert body == {"bytes_written": 3}

    def test_input_without_a_submit_is_written_exactly_as_given(self):
        """The opt-out, and the reason splitting the submit is safe.

        Everything above changes what happens when the caller asks for Enter. A
        caller that does not — a keystroke, a control byte, an arrow — must still
        get one write of exactly its own bytes, with nothing stripped and nothing
        appended. The terminal's key pad and every raw keystroke from the emulator
        take this path.
        """
        app, _buf, sink = self._build()
        client = self._client(app)

        for raw in ("\x03", "\x1b[A", "text with a trailing newline\n", "\t"):
            sink.clear()
            resp = client.post(
                "/input",
                json={"data": raw, "append_newline": False},
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 200
            assert sink == [raw.encode("utf-8")], (
                f"{raw!r} was altered on the no-submit path: {sink}"
            )

    def test_output_returns_buffered_data(self):
        app, buf, _sink = self._build()
        buf.append(b"banana")
        client = self._client(app)
        resp = client.get(
            "/output?cursor=0",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"] == "banana"
        assert body["cursor"] == 6
        assert body["dropped_bytes"] == 0

    def test_missing_token_rejected(self):
        app, _buf, _sink = self._build()
        client = self._client(app)
        resp = client.post("/input", json={"data": "x"})
        assert resp.status_code == 401

    def test_wrong_token_rejected(self):
        app, _buf, _sink = self._build()
        client = self._client(app)
        resp = client.get(
            "/output?cursor=0",
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401

    def test_healthz_requires_auth(self):
        app, _buf, _sink = self._build()
        client = self._client(app)
        resp = client.get("/healthz")
        assert resp.status_code == 401
        resp = client.get("/healthz", headers={"Authorization": "Bearer test-token"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_healthz_reports_geometry(self):
        app, _calls = self._build_resizable(winsize=(30, 100))
        client = self._client(app)
        resp = client.get("/healthz", headers={"Authorization": "Bearer test-token"})
        assert resp.status_code == 200
        body = resp.json()
        # The pre-existing keys stay put: lmer-pipe reads ok/cursor from here.
        assert body["ok"] is True
        assert body["cursor"] == 0
        # A browser client needs the geometry before it decides whether to resize.
        assert body["rows"] == 30
        assert body["cols"] == 100

    def test_healthz_geometry_is_null_when_unknown(self):
        # Nulls rather than missing keys so a client can tell "no geometry
        # available" from a real size without special-casing the shape.
        app, _buf, _sink = self._build()
        client = self._client(app)
        body = client.get(
            "/healthz", headers={"Authorization": "Bearer test-token"}
        ).json()
        assert body["ok"] is True
        assert body["rows"] is None
        assert body["cols"] is None

        app, _calls = self._build_resizable(winsize=None)
        body = self._client(app).get(
            "/healthz", headers={"Authorization": "Bearer test-token"}
        ).json()
        assert body["rows"] is None
        assert body["cols"] is None

    def test_healthz_reports_how_long_the_harness_has_been_quiet(self):
        """The consumer story: "idle 22m", read off the one process that knows.

        Both spellings, because they answer to different readers — the number is
        what a client acts on without owning a correct clock, the timestamp is what
        anything writing this down needs.
        """
        clock = _FakeClock()
        buf = supervisor.OutputBuffer(limit=1024, clock=clock)
        app = supervisor._build_fastapi_app(buf, lambda data: len(data), "test-token")
        client = self._client(app)

        buf.append(b"the harness drew something")
        clock.advance(1320)
        body = client.get(
            "/healthz", headers={"Authorization": "Bearer test-token"}
        ).json()

        assert body["idle_seconds"] == 1320.0
        # The pre-existing keys stay put beside it: lmer-pipe reads ok/cursor here,
        # and a re-attach reads cursor/rows/cols.
        assert body["ok"] is True
        assert body["cursor"] == len(b"the harness drew something")
        moment = datetime.strptime(
            body["last_output_at"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        gap = datetime.now(timezone.utc) - moment
        assert timedelta(seconds=1315) <= gap <= timedelta(seconds=1330), (
            f"last_output_at is {body['last_output_at']}, which is not 1320s ago"
        )

    def test_healthz_says_nothing_about_idleness_before_the_first_byte(self):
        """Nulls, not omitted keys and not zero — the geometry's own rule.

        A reader has one absent case to render as nothing, and it covers both a
        session that has produced nothing yet and a session whose image never
        reports this at all.
        """
        app, _buf, _sink = self._build()
        body = self._client(app).get(
            "/healthz", headers={"Authorization": "Bearer test-token"}
        ).json()
        assert body["last_output_at"] is None
        assert body["idle_seconds"] is None

    def test_typing_into_the_session_is_not_activity(self):
        """Output, never input — the decision this feature turns on.

        The question is whether the *harness* is doing anything. An operator (or the
        platform) typing into a session that answers nothing is precisely the idle
        case, so a POST /input that moved the clock would report the wedged session
        as the busy one.
        """
        clock = _FakeClock()
        buf = supervisor.OutputBuffer(limit=1024, clock=clock)
        written: list[bytes] = []

        def write_input(data: bytes) -> int:
            written.append(data)
            return len(data)

        client = self._client(
            supervisor._build_fastapi_app(buf, write_input, "test-token")
        )
        buf.append(b"a prompt")
        clock.advance(900)

        resp = client.post(
            "/input",
            json={"data": "an answer"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        assert written == [b"an answer"], "the input never reached the child"

        body = client.get(
            "/healthz", headers={"Authorization": "Bearer test-token"}
        ).json()
        assert body["idle_seconds"] == 900.0, (
            "typing into the session reset its idle clock, so a session that was "
            "answered and never replied reads as one that is working"
        )

    def test_healthz_survives_failing_winsize_ioctl(self):
        # A liveness probe must stay a liveness probe even if the geometry
        # lookup blows up (master closed after the child exited).
        def boom():
            raise OSError("ioctl failed: Bad file descriptor")

        app, _buf, _sink = self._build(get_winsize=boom)
        resp = self._client(app).get(
            "/healthz", headers={"Authorization": "Bearer test-token"}
        )
        assert resp.status_code == 200
        # Idleness is null beside the geometry rather than absent: nothing has been
        # produced, and this route's rule is that an unknown answers as a null key.
        assert resp.json() == {
            "ok": True, "cursor": 0, "rows": None, "cols": None,
            "last_output_at": None, "idle_seconds": None,
        }

    # ``POST /resize``: the no-host-TTY path for browser-rendered TUIs.

    def test_resize_applies_geometry(self):
        app, calls = self._build_resizable()
        client = self._client(app)
        resp = client.post(
            "/resize",
            json={"rows": 40, "cols": 120},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"rows": 40, "cols": 120}
        assert calls == [(40, 120)]

    def test_resize_accepts_the_documented_bounds(self):
        app, calls = self._build_resizable()
        client = self._client(app)
        resp = client.post(
            "/resize",
            json={
                "rows": supervisor.MAX_WINSIZE_DIMENSION,
                "cols": supervisor.MIN_WINSIZE_DIMENSION,
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        assert calls == [
            (supervisor.MAX_WINSIZE_DIMENSION, supervisor.MIN_WINSIZE_DIMENSION)
        ]

    def test_resize_requires_auth(self):
        app, calls = self._build_resizable()
        client = self._client(app)
        resp = client.post("/resize", json={"rows": 40, "cols": 120})
        assert resp.status_code == 401
        resp = client.post(
            "/resize",
            json={"rows": 40, "cols": 120},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401
        assert calls == []

    def test_resize_rejects_wedged_and_absurd_geometry(self):
        # 0 columns is not "unknown", it's a wedged terminal nothing inside the
        # container would fix — none of these may reach the ioctl.
        app, calls = self._build_resizable()
        client = self._client(app)
        bad_bodies = [
            ({"rows": 0, "cols": 80}, "rows"),
            ({"rows": 24, "cols": 0}, "cols"),
            ({"rows": -1, "cols": 80}, "rows"),
            ({"rows": 24, "cols": -80}, "cols"),
            ({"rows": supervisor.MAX_WINSIZE_DIMENSION + 1, "cols": 80}, "rows"),
            ({"rows": 24, "cols": 100000}, "cols"),
            ({"rows": 24.5, "cols": 80}, "rows"),
            ({"rows": "wide", "cols": 80}, "rows"),
            ({"rows": None, "cols": 80}, "rows"),
            ({"cols": 80}, "rows"),
        ]
        for body, field in bad_bodies:
            resp = client.post(
                "/resize",
                json=body,
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 422, f"{body} was accepted"
            # The client has to learn which dimension it got wrong.
            assert field in resp.text, f"{body} -> {resp.text}"
        assert calls == []

    def test_resize_unavailable_without_a_resize_callable(self):
        # The app is built without a resize callable by older callers (and by
        # the pre-existing tests): "nothing to resize" is a deployment fact, so
        # it must read as 503, not as a crashed handler.
        app, _buf, _sink = self._build()
        resp = self._client(app).post(
            "/resize",
            json={"rows": 40, "cols": 120},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 503
        assert "resize unavailable" in resp.json()["detail"]

    def test_resize_reports_ioctl_failure_as_http_error(self):
        # TestClient re-raises unhandled server exceptions, so this only passes
        # if the route actually catches the OSError.
        def boom(rows: int, cols: int) -> None:
            raise OSError("ioctl failed: Bad file descriptor")

        app, _calls = self._build_resizable(resize=boom)
        resp = self._client(app).post(
            "/resize",
            json={"rows": 40, "cols": 120},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 500
        assert "ioctl failed" in resp.json()["detail"]

    def test_resize_reports_a_real_ioctl_failure(self, tmp_path):
        # Wired the way run_supervisor wires it (strict=True) but pointed at a
        # non-TTY fd, so the ioctl fails for real. The client must hear about it:
        # a 200 echoing geometry that never landed would leave a browser
        # rendering at the wrong size with nothing to react to.
        regular = tmp_path / "not-a-tty"
        regular.write_bytes(b"")
        fd = os.open(str(regular), os.O_RDWR)
        try:
            app, _buf, _sink = self._build(
                resize=lambda rows, cols: supervisor._set_winsize(
                    fd, rows, cols, strict=True
                ),
                get_winsize=lambda: supervisor._get_winsize(fd),
            )
            resp = self._client(app).post(
                "/resize",
                json={"rows": 40, "cols": 120},
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 500
            assert "cannot set window size" in resp.json()["detail"]
        finally:
            os.close(fd)

    def test_supervisor_wires_a_strict_resize_closure(self):
        """run_supervisor's ``/resize`` closure must keep ``strict=True``.

        The closure is a local inside ``run_supervisor``, so there is nothing to
        import and call — mirror the cli.py env-dict check and pin the wiring in
        the source. Without ``strict``, ``_set_winsize`` swallows the ioctl error
        and the route answers 200 for a resize that never happened.
        """
        import re
        from pathlib import Path
        source = (
            Path(__file__).parent.parent / "src" / "lmer_cli" / "supervisor.py"
        ).read_text()
        pattern = re.compile(
            r"_set_winsize\(\s*master_fd,\s*rows,\s*cols,\s*strict=True\s*,?\s*\)"
        )
        assert pattern.search(source), \
            "run_supervisor's /resize closure lost strict=True"

    def test_resize_round_trip_on_a_real_pty(self):
        """Wired the way run_supervisor wires it, the geometry lands on the PTY."""
        master, slave = os.openpty()
        try:
            app, _buf, _sink = self._build(
                resize=lambda rows, cols: supervisor._set_winsize(master, rows, cols),
                get_winsize=lambda: supervisor._get_winsize(master),
            )
            client = self._client(app)
            # A freshly allocated PTY has no geometry yet; healthz reports the
            # 0x0 it really is, which is the browser client's cue to resize.
            before = client.get(
                "/healthz", headers={"Authorization": "Bearer test-token"}
            ).json()
            assert (before["rows"], before["cols"]) == (0, 0)
            resp = client.post(
                "/resize",
                json={"rows": 44, "cols": 132},
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status_code == 200
            # The slave end is what the wrapped TUI queries for its layout.
            assert supervisor._get_winsize(slave) == (44, 132)
            body = client.get(
                "/healthz", headers={"Authorization": "Bearer test-token"}
            ).json()
            assert (body["rows"], body["cols"]) == (44, 132)
        finally:
            os.close(master)
            os.close(slave)


# ---------------------------------------------------------------------------
# Integration: spawn `cat` under the supervisor and verify forwarding
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.environ.get("CI_NO_PTY") == "1", reason="PTY not available")
class TestSupervisorIntegration:
    def _run(self, argv, stdin_data: bytes, options=None):
        """Run the supervisor with two pipes faked as stdin/stdout."""
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        # Feed the input then close to send EOF
        os.write(stdin_w, stdin_data)
        os.close(stdin_w)

        opts = {
            "fastapi": False,
            "manual_start": True,  # avoid /start in tests (cat would just echo it)
            "port_range": (8700, 8799),
            "host": "127.0.0.1",
            "token": "",
            "auto_start_delay": 0.1,
            "auto_start_nudge_delay": 0.05,
            "auto_start_ready_marker": supervisor.DEFAULT_AUTO_START_READY_MARKER,
            "auto_start_ready_timeout": 0,  # skip marker wait for non-claude wrapped procs
            "auto_start_settle_delay": 0,
            "winsize_recheck_delay": 0,
            "start_prompt": "",
            "start_prompt_delay": 0,
        }
        if options:
            opts.update(options)

        rc = supervisor.run_supervisor(
            argv,
            opts,
            stdin_fd=stdin_r,
            stdout_fd=stdout_w,
        )
        os.close(stdout_w)
        chunks = []
        while True:
            try:
                chunk = os.read(stdout_r, 4096)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        os.close(stdout_r)
        os.close(stdin_r)
        return rc, b"".join(chunks)

    def test_cat_forwards_stdin_to_stdout(self):
        rc, output = self._run(["cat"], b"hello supervisor\n")
        assert rc == 0
        assert b"hello supervisor" in output

    def test_auto_start_injects_when_enabled(self):
        # Use ``head -c N`` instead of ``cat``: the auto-/start path pre-configures
        # the PTY out of cooked mode, so a ^D-on-stdin-close trick can't terminate
        # the child anymore. ``head -c`` exits on its own once it has echoed N
        # bytes, regardless of line discipline. N=10 covers ``/start\r`` plus the
        # three CR nudges.
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()

        opts = {
            "fastapi": False,
            "manual_start": False,
            "port_range": (8700, 8799),
            "host": "127.0.0.1",
            "token": "",
            "auto_start_delay": 0.1,
            "auto_start_nudge_delay": 0.05,
            # head doesn't emit claude's prompt glyph; skip marker gating so
            # the injection fires immediately after the initial delay.
            "auto_start_ready_marker": supervisor.DEFAULT_AUTO_START_READY_MARKER,
            "auto_start_ready_timeout": 0,
            "auto_start_settle_delay": 0,
            "winsize_recheck_delay": 0,
            # No follow-up prompt: only /start + nudges are injected (10 bytes).
            "start_prompt": "",
            "start_prompt_delay": 0,
        }
        rc = supervisor.run_supervisor(
            ["head", "-c", "10"], opts, stdin_fd=stdin_r, stdout_fd=stdout_w,
        )
        os.close(stdin_w)
        os.close(stdout_w)
        chunks = []
        while True:
            try:
                chunk = os.read(stdout_r, 4096)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        os.close(stdout_r)
        os.close(stdin_r)
        assert rc == 0
        assert b"/start" in b"".join(chunks)

    def test_start_prompt_injected_after_start(self):
        # Inject /start (7 bytes: ``/start\r``) + 3 CR nudges (3 bytes) +
        # the follow-up prompt ``hello\r`` (6 bytes) + 3 CR nudges for the
        # prompt (3 bytes) = 19 bytes total. ``head -c 19`` echoes exactly
        # those and exits, so the supervisor returns.
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()

        opts = {
            "fastapi": False,
            "manual_start": False,
            "port_range": (8700, 8799),
            "host": "127.0.0.1",
            "token": "",
            "auto_start_delay": 0.1,
            "auto_start_nudge_delay": 0.05,
            "auto_start_ready_marker": supervisor.DEFAULT_AUTO_START_READY_MARKER,
            "auto_start_ready_timeout": 0,
            "auto_start_settle_delay": 0,
            "winsize_recheck_delay": 0,
            "start_prompt": "hello",
            "start_prompt_delay": 0.05,
        }
        rc = supervisor.run_supervisor(
            ["head", "-c", "19"], opts, stdin_fd=stdin_r, stdout_fd=stdout_w,
        )
        os.close(stdin_w)
        os.close(stdout_w)
        chunks = []
        while True:
            try:
                chunk = os.read(stdout_r, 4096)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        os.close(stdout_r)
        os.close(stdin_r)
        out = b"".join(chunks)
        assert rc == 0
        assert b"/start" in out
        assert b"hello" in out

    def test_exit_code_propagated(self):
        rc, _ = self._run(["sh", "-c", "exit 42"], b"")
        assert rc == 42

    def test_the_wrapped_process_output_lands_in_the_session_log(
        self, tmp_path, monkeypatch
    ):
        """The point of the whole feature, exercised through a real PTY.

        The mount point is redirected rather than the writing being stubbed: what
        has to hold is that bytes coming off the PTY master reach the file, which is
        a property of the forwarding loop and not of :class:`SessionLog` alone.
        """
        mount = tmp_path / "mounted"
        mount.mkdir()
        monkeypatch.setattr(supervisor, "CONTAINER_SESSION_LOG_DIR", str(mount))

        rc, output = self._run(["sh", "-c", "echo recorded-by-the-supervisor"], b"")

        assert rc == 0
        logged = (mount / supervisor.SESSION_LOG_NAME).read_bytes()
        assert b"recorded-by-the-supervisor" in logged
        assert b"recorded-by-the-supervisor" in output, "still forwarded to stdout"

    def test_nothing_is_recorded_when_no_directory_was_mounted(
        self, tmp_path, monkeypatch
    ):
        """An unorchestrated ``lmer`` run writes no log and creates no directory."""
        mount = tmp_path / "never-mounted"
        monkeypatch.setattr(supervisor, "CONTAINER_SESSION_LOG_DIR", str(mount))

        rc, output = self._run(["sh", "-c", "echo unrecorded"], b"")

        assert rc == 0
        assert b"unrecorded" in output
        assert not mount.exists()

    def test_supervisor_pid_exported_to_child(self):
        # The supervisor publishes its own PID via LMER_SUPERVISOR_PID before
        # fork, so the wrapped process (and its subprocesses) inherit it. Here
        # the supervisor runs in this very process, so the child should see
        # this process's PID.
        os.environ.pop("LMER_SUPERVISOR_PID", None)
        try:
            rc, output = self._run(
                ["sh", "-c", "echo PID=$LMER_SUPERVISOR_PID"], b""
            )
            assert rc == 0
            assert f"PID={os.getpid()}".encode() in output
        finally:
            os.environ.pop("LMER_SUPERVISOR_PID", None)

    def test_sigusr1_triggers_clean_shutdown(self):
        # SIGUSR1 to the supervisor must quit the wrapped child and report a
        # clean exit (0). The supervisor runs in this process, so we signal
        # ourselves from a background thread once it's underway. We wrap `cat`
        # (stays alive on stdin) with manual_start=True, leaving the PTY in
        # cooked mode: the injected ^C (\x03) then reaches cat's line discipline
        # as VINTR -> SIGINT, killing it. (In production claude runs in raw mode
        # and handles the ^C chord itself; either way the supervisor reports 0.)
        import signal as _signal

        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        opts = {
            "fastapi": False,
            "manual_start": True,
            "port_range": (8700, 8799),
            "host": "127.0.0.1",
            "token": "",
            "auto_start_delay": 0,
            "auto_start_nudge_delay": 0,
            "auto_start_ready_marker": b"",
            "auto_start_ready_timeout": 0,
            "auto_start_settle_delay": 0,
            "winsize_recheck_delay": 0,
            "start_prompt": "",
            "start_prompt_delay": 0,
        }

        main_pid = os.getpid()

        def _signal_after_startup():
            time.sleep(0.3)
            os.kill(main_pid, _signal.SIGUSR1)

        signaller = threading.Thread(target=_signal_after_startup, daemon=True)
        signaller.start()

        try:
            rc = supervisor.run_supervisor(
                ["cat"], opts, stdin_fd=stdin_r, stdout_fd=stdout_w
            )
        finally:
            os.close(stdin_w)
            os.close(stdout_w)
            signaller.join(timeout=2.0)
            # Drain and close so no fd/pipe leaks regardless of outcome.
            while True:
                try:
                    if not os.read(stdout_r, 4096):
                        break
                except OSError:
                    break
            os.close(stdout_r)
            os.close(stdin_r)
            os.environ.pop("LMER_SUPERVISOR_PID", None)

        # Clean exit reported even though the child was killed by SIGINT —
        # a requested self-shutdown always looks deliberate to the orchestrator.
        assert rc == 0
