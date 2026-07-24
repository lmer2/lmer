"""Tests for lmer_cli.supervisor."""
from __future__ import annotations

import os
import socket
import termios
import threading
import time

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

    def test_does_not_double_terminate_lf(self):
        sink: list[bytes] = []
        supervisor._inject_start_prompt(
            lambda data: (sink.append(data), len(data))[1],
            "do the thing\n",
        )
        assert sink == [b"do the thing\n"]

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


class TestEnsureSubmitCr:
    def test_appends_cr_when_missing(self):
        assert supervisor._ensure_submit_cr("hello") == "hello\r"

    def test_does_not_double_cr(self):
        assert supervisor._ensure_submit_cr("hello\r") == "hello\r"

    def test_does_not_double_lf(self):
        assert supervisor._ensure_submit_cr("hello\n") == "hello\n"

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
    def _build(self, token="test-token"):
        buf = supervisor.OutputBuffer(limit=1024)
        sink: list[bytes] = []

        def write_input(data: bytes) -> int:
            sink.append(data)
            return len(data)

        app = supervisor._build_fastapi_app(buf, write_input, token)
        return app, buf, sink

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
        # CR, not LF: claude's TUI in raw mode treats \r as Enter; \n would
        # only insert a literal newline into the input box.
        assert sink == [b"/start\r"]

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
        assert sink == [b"/start\r"]
        sink.clear()
        # Same for legacy \n inputs.
        resp = client.post(
            "/input",
            json={"data": "/start\n", "append_newline": True},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 200
        assert sink == [b"/start\n"]

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
