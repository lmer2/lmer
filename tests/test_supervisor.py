"""Tests for lmer_cli.supervisor."""
from __future__ import annotations

import os
import socket
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
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_defaults(self, monkeypatch):
        for k in (
            "LMER_FASTAPI", "LMER_MANUAL_START", "LMER_FASTAPI_PORT_RANGE",
            "LMER_FASTAPI_HOST", "LMER_FASTAPI_TOKEN", "LMER_AUTO_START_DELAY",
            "LMER_WINSIZE_RECHECK_DELAY",
        ):
            monkeypatch.delenv(k, raising=False)
        opts = supervisor._resolve_options(self._ns())
        assert opts["fastapi"] is False
        assert opts["manual_start"] is False
        assert opts["port_range"] == supervisor.DEFAULT_PORT_RANGE
        assert opts["host"] == supervisor.DEFAULT_FASTAPI_HOST
        assert opts["token"] == ""
        assert opts["auto_start_delay"] == supervisor.DEFAULT_AUTO_START_DELAY

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
            "--", "claude", "--foo",
        ])
        assert ns.fastapi is True
        assert ns.manual_start is True
        assert ns.fastapi_port_range == "9000-9099"
        assert ns.fastapi_host == "0.0.0.0"
        assert ns.fastapi_token == "tok"
        assert ns.auto_start_delay == 0.5
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
            "winsize_recheck_delay": 0,
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
        # Use cat which echoes whatever we send (including the auto /start).
        # Don't close stdin immediately — give the timer time to fire.
        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()

        def feeder():
            time.sleep(0.4)
            try:
                os.close(stdin_w)
            except OSError:
                pass
        threading.Thread(target=feeder, daemon=True).start()

        opts = {
            "fastapi": False,
            "manual_start": False,
            "port_range": (8700, 8799),
            "host": "127.0.0.1",
            "token": "",
            "auto_start_delay": 0.1,
            "winsize_recheck_delay": 0,
        }
        rc = supervisor.run_supervisor(
            ["cat"], opts, stdin_fd=stdin_r, stdout_fd=stdout_w,
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
        assert rc == 0
        assert b"/start" in b"".join(chunks)

    def test_exit_code_propagated(self):
        rc, _ = self._run(["sh", "-c", "exit 42"], b"")
        assert rc == 42
