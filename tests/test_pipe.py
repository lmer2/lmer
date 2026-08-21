"""Tests for the ``lmer-pipe`` helper CLI."""
from __future__ import annotations

import argparse
import io
import socket
import threading
import time
from contextlib import contextmanager

import pytest
import requests
import uvicorn

from lmer_cli import pipe, supervisor
from tests.conftest import strip_lmer_env


# ---------------------------------------------------------------------------
# CI runners route 127.0.0.1 traffic through Privoxy unless NO_PROXY is set.
# Every test in this module hits a localhost uvicorn instance, so disable
# proxy autodetection for the duration of the test session.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_proxies(monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


# ---------------------------------------------------------------------------
# Live test server: runs the supervisor's FastAPI app on a real port so the
# requests-based talk client can hit it end-to-end.
# ---------------------------------------------------------------------------


@contextmanager
def _live_app(token: str = "test-token"):
    output = supervisor.OutputBuffer(limit=1024)
    sink: list[bytes] = []

    def write_input(data: bytes) -> int:
        sink.append(data)
        return len(data)

    app = supervisor._build_fastapi_app(output, write_input, token)

    # Pick a real free port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for the server to be ready (uvicorn sets ``server.started`` once
    # the socket is bound and accepting). 5 seconds is generous; on a healthy
    # machine this is well under 200 ms.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not server.started:
        time.sleep(0.02)
    assert server.started, "uvicorn test server failed to start"

    try:
        yield port, output, sink
    finally:
        server.should_exit = True
        thread.join(timeout=3.0)


# ---------------------------------------------------------------------------
# Settings resolution
# ---------------------------------------------------------------------------


class TestResolveSettings:
    def _ns(self, **overrides):
        import argparse
        defaults = dict(host=None, port=None, url=None, token=None, timeout=10.0, json=False)
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_url_overrides_host_and_port(self, monkeypatch):
        monkeypatch.setenv("LMER_FASTAPI_PORT", "9999")
        url = pipe._resolve_base_url(self._ns(url="http://example.test:1/foo"))
        assert url == "http://example.test:1/foo"

    def test_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("LMER_FASTAPI_PORT", "4242")
        monkeypatch.delenv("LMER_FASTAPI_HOST", raising=False)
        monkeypatch.delenv("LMER_FASTAPI_URL", raising=False)
        url = pipe._resolve_base_url(self._ns())
        assert url == "http://127.0.0.1:4242"

    def test_missing_port_raises(self, monkeypatch):
        for k in ("LMER_FASTAPI_PORT", "LMER_FASTAPI_URL"):
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(pipe.PipeError):
            pipe._resolve_base_url(self._ns())

    def test_token_from_env(self, monkeypatch):
        monkeypatch.setenv("LMER_FASTAPI_TOKEN", "from-env")
        assert pipe._resolve_token(self._ns()) == "from-env"

    def test_token_flag_overrides_env(self, monkeypatch):
        monkeypatch.setenv("LMER_FASTAPI_TOKEN", "from-env")
        assert pipe._resolve_token(self._ns(token="from-flag")) == "from-flag"

    def test_missing_token_raises(self, monkeypatch):
        monkeypatch.delenv("LMER_FASTAPI_TOKEN", raising=False)
        with pytest.raises(pipe.PipeError):
            pipe._resolve_token(self._ns())


# ---------------------------------------------------------------------------
# End-to-end against a live server
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_send_writes_to_endpoint(self, monkeypatch, capsys):
        with _live_app() as (port, _output, sink):
            monkeypatch.setenv("LMER_FASTAPI_PORT", str(port))
            monkeypatch.setenv("LMER_FASTAPI_TOKEN", "test-token")
            rc = pipe.main(["send", "/start", "--quiet"])
        assert rc == 0
        # Claude does not execute a slash command delivered as a bracketed paste
        # (#210), so control-plane commands remain keystrokes. Enter is still its
        # own one-and-only write: a second blind CR could fire another dialog.
        assert [bytes(part) for part in sink] == [b"/start", b"\r"], (
            f"the submit must be one CR, written on its own: {sink}"
        )

    def test_send_no_newline(self, monkeypatch):
        with _live_app() as (port, _output, sink):
            monkeypatch.setenv("LMER_FASTAPI_PORT", str(port))
            monkeypatch.setenv("LMER_FASTAPI_TOKEN", "test-token")
            rc = pipe.main(["send", "/start", "--no-newline", "--quiet"])
        assert rc == 0
        assert sink == [b"/start"]

    def test_read_outputs_buffer(self, monkeypatch, capsys):
        with _live_app() as (port, output, _sink):
            output.append(b"line one\nline two\n")
            monkeypatch.setenv("LMER_FASTAPI_PORT", str(port))
            monkeypatch.setenv("LMER_FASTAPI_TOKEN", "test-token")
            rc = pipe.main(["read"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out == "line one\nline two\n"

    def test_read_since_cursor(self, monkeypatch, capsys):
        with _live_app() as (port, output, _sink):
            output.append(b"abcdef")
            monkeypatch.setenv("LMER_FASTAPI_PORT", str(port))
            monkeypatch.setenv("LMER_FASTAPI_TOKEN", "test-token")
            rc = pipe.main(["read", "--since", "3"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out == "def"

    def test_health_ok(self, monkeypatch, capsys):
        with _live_app() as (port, output, _sink):
            output.append(b"hello")
            monkeypatch.setenv("LMER_FASTAPI_PORT", str(port))
            monkeypatch.setenv("LMER_FASTAPI_TOKEN", "test-token")
            rc = pipe.main(["health"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "ok=True" in captured.out
        assert "cursor=5" in captured.out

    def test_wrong_token_returns_nonzero(self, monkeypatch, capsys):
        with _live_app() as (port, _output, _sink):
            monkeypatch.setenv("LMER_FASTAPI_PORT", str(port))
            monkeypatch.setenv("LMER_FASTAPI_TOKEN", "wrong-token")
            rc = pipe.main(["health"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "HTTP 401" in captured.err

    def test_missing_token_exits_2(self, monkeypatch, capsys):
        with _live_app() as (port, _output, _sink):
            monkeypatch.setenv("LMER_FASTAPI_PORT", str(port))
            monkeypatch.delenv("LMER_FASTAPI_TOKEN", raising=False)
            rc = pipe.main(["health"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "no token configured" in captured.err

    def test_read_wait_outlasts_default_timeout(self, monkeypatch, capsys):
        # --wait > --timeout used to raise ReadTimeout because the client
        # capped the request before the server's long-poll budget. cmd_read
        # now extends the HTTP timeout to cover the wait, so the call returns
        # data successfully when the buffer is fed mid-poll.
        with _live_app() as (port, output, _sink):
            monkeypatch.setenv("LMER_FASTAPI_PORT", str(port))
            monkeypatch.setenv("LMER_FASTAPI_TOKEN", "test-token")

            def feed_after_delay():
                time.sleep(0.2)
                output.append(b"delayed-payload")

            threading.Thread(target=feed_after_delay, daemon=True).start()
            rc = pipe.main(["read", "--wait", "5", "--timeout", "1"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out == "delayed-payload"


# ---------------------------------------------------------------------------
# cmd_follow: long-poll loop, --from-end probe, error classification
# ---------------------------------------------------------------------------


def _follow_args(**overrides):
    defaults = dict(
        host=None,
        port=None,
        url=None,
        token=None,
        timeout=10.0,
        json=False,
        since=0,
        from_end=False,
        wait=15.0,
        retry=2.0,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _guard_follow_requests(monkeypatch) -> threading.Event:
    """Make a background cmd_follow loop stoppable.

    cmd_follow retries ConnectionError forever by design, so once the live
    server is torn down the loop never exits on its own. Wrap requests.get
    so that after the returned event is set, the next call raises an
    exception the retry handler does NOT catch, breaking the loop.
    """
    stop = threading.Event()
    real_get = pipe.requests.get

    def guarded_get(*args, **kwargs):
        if stop.is_set():
            raise RuntimeError("follow stopped by test")
        return real_get(*args, **kwargs)

    monkeypatch.setattr(pipe.requests, "get", guarded_get)
    return stop


class TestCmdFollow:
    def test_streams_from_live_endpoint(self, monkeypatch, capsys):
        # Start follow in a background thread, append twice, then stop the
        # server to break the loop. Both chunks should reach stdout.
        with _live_app() as (port, output, _sink):
            monkeypatch.setenv("LMER_FASTAPI_PORT", str(port))
            monkeypatch.setenv("LMER_FASTAPI_TOKEN", "test-token")
            output.append(b"first ")

            args = _follow_args(wait=0.2, retry=0.05, timeout=1.0)
            stop = _guard_follow_requests(monkeypatch)

            def runner():
                try:
                    pipe.cmd_follow(args)
                except BaseException:
                    pass

            t = threading.Thread(target=runner, daemon=True)
            t.start()
            time.sleep(0.3)
            output.append(b"second")
            time.sleep(0.5)
        # _live_app exiting tears down uvicorn; the follow loop retries
        # ConnectionError forever by design, so it must be stopped
        # explicitly — a leaked daemon thread spams stderr for the rest of
        # the pytest run and can abort interpreter shutdown
        # (_enter_buffered_busy).
        stop.set()
        t.join(timeout=2.0)
        assert not t.is_alive()
        captured = capsys.readouterr()
        assert "first " in captured.out
        assert "second" in captured.out

    def test_from_end_skips_backlog(self, monkeypatch, capsys):
        with _live_app() as (port, output, _sink):
            monkeypatch.setenv("LMER_FASTAPI_PORT", str(port))
            monkeypatch.setenv("LMER_FASTAPI_TOKEN", "test-token")
            output.append(b"backlog-bytes")

            args = _follow_args(from_end=True, wait=0.2, retry=0.05, timeout=1.0)
            stop = _guard_follow_requests(monkeypatch)

            def runner():
                try:
                    pipe.cmd_follow(args)
                except BaseException:
                    pass

            t = threading.Thread(target=runner, daemon=True)
            t.start()
            time.sleep(0.3)
            output.append(b"after-probe")
            time.sleep(0.5)
        stop.set()
        t.join(timeout=2.0)
        assert not t.is_alive()
        captured = capsys.readouterr()
        # The pre-existing 13 bytes ("backlog-bytes") must be skipped; only
        # data appended after the /healthz probe should print.
        assert "backlog-bytes" not in captured.out
        assert "after-probe" in captured.out

    def test_http_error_propagates_does_not_retry_forever(
        self, monkeypatch, capsys
    ):
        # Wrong token => /output returns 401. The previous behavior was to
        # swallow it under the broad RequestException retry; now HTTPError
        # propagates so main() exits 1.
        with _live_app() as (port, _output, _sink):
            monkeypatch.setenv("LMER_FASTAPI_PORT", str(port))
            monkeypatch.setenv("LMER_FASTAPI_TOKEN", "wrong-token")
            rc = pipe.main([
                "follow",
                "--wait", "0.1",
                "--retry", "0.05",
                "--timeout", "1",
            ])
        captured = capsys.readouterr()
        assert rc == 1
        assert "HTTP 401" in captured.err

    def test_from_end_http_error_propagates(self, monkeypatch, capsys):
        # The --from-end probe was already outside the retry try/except. Lock
        # that in: a 401 on the probe must surface as exit 1, not loop.
        with _live_app() as (port, _output, _sink):
            monkeypatch.setenv("LMER_FASTAPI_PORT", str(port))
            monkeypatch.setenv("LMER_FASTAPI_TOKEN", "wrong-token")
            rc = pipe.main([
                "follow",
                "--from-end",
                "--wait", "0.1",
                "--retry", "0.05",
                "--timeout", "1",
            ])
        captured = capsys.readouterr()
        assert rc == 1
        assert "HTTP 401" in captured.err

    def test_connection_error_is_retried(self, monkeypatch, capsys):
        # Drive cmd_follow against a port that nobody is listening on so each
        # request raises ConnectionError. Verify the loop sleeps and retries
        # rather than crashing, and that we can stop it after a few iterations.
        # We do this by monkeypatching time.sleep to count calls and raise on
        # the 3rd one.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            dead_port = s.getsockname()[1]
        # Socket is closed now; nothing listens on dead_port.

        monkeypatch.setenv("LMER_FASTAPI_PORT", str(dead_port))
        monkeypatch.setenv("LMER_FASTAPI_TOKEN", "test-token")

        sleep_calls = {"n": 0}

        class _Stop(Exception):
            pass

        real_sleep = time.sleep

        def fake_sleep(seconds):
            sleep_calls["n"] += 1
            if sleep_calls["n"] >= 3:
                raise _Stop
            real_sleep(0)

        monkeypatch.setattr(pipe.time, "sleep", fake_sleep)

        with pytest.raises(_Stop):
            pipe.cmd_follow(_follow_args(wait=0.0, retry=0.01, timeout=0.2))
        captured = capsys.readouterr()
        assert sleep_calls["n"] >= 3
        assert "follow: connection error" in captured.err
