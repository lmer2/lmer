"""Tests for general port passthrough (--ports / --port-pool, issue #57) and for
the host-side resolution of the --fastapi endpoint's port (issue #141).

Covers two layers:
1. The pure resolvers in cli.py — ``_resolve_requested_ports`` (CLI args win over
   the LMER_PORT_COUNT / LMER_PORT_POOL env vars, with the documented default
   pool and validation behavior) and ``_resolve_fastapi_host_port`` (a preset
   LMER_FASTAPI_PORT wins over picking one, because whoever set it has already
   told someone else where the session will answer).
2. Source-level guards that the CLI declares the flags and that the allocated
   ports are exported to the container via LMER_PORTS. The publish/allocate
   logic in main() is built inline, so (like the LMER_REASONING_EFFORT guard)
   a source check catches accidental removal without re-testing podman wiring.
"""
import argparse
import json
import re
import socket
from pathlib import Path

import pytest

from lmer_cli.cli import (
    _record_published_ports,
    DEFAULT_FASTAPI_PORT_RANGE,
    DEFAULT_PORT_BIND,
    DEFAULT_PORT_POOL,
    _apply_port_passthrough,
    _publish_host_ports,
    _resolve_fastapi_host_port,
    _resolve_port_bind,
    _resolve_requested_ports,
)

CLI_PY = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"


class TestResolveRequestedPorts:
    def test_default_pool_constant(self):
        # Kept distinct from the FastAPI range (8700-8799) to avoid collisions.
        assert DEFAULT_PORT_POOL == "8800-8899"

    def test_off_when_nothing_requested(self):
        count, pool = _resolve_requested_ports(None, None, {})
        assert count == 0
        assert pool == DEFAULT_PORT_POOL

    def test_cli_count_and_pool_take_precedence_over_env(self):
        count, pool = _resolve_requested_ports(
            3, "9000-9100", {"LMER_PORT_COUNT": "7", "LMER_PORT_POOL": "1-2"}
        )
        assert count == 3
        assert pool == "9000-9100"

    def test_env_fallback_for_count_and_pool(self):
        count, pool = _resolve_requested_ports(
            None, None, {"LMER_PORT_COUNT": "2", "LMER_PORT_POOL": "9500-9600"}
        )
        assert count == 2
        assert pool == "9500-9600"

    def test_env_count_with_default_pool(self):
        count, pool = _resolve_requested_ports(None, None, {"LMER_PORT_COUNT": "4"})
        assert count == 4
        assert pool == DEFAULT_PORT_POOL

    def test_blank_env_count_is_off(self):
        count, _ = _resolve_requested_ports(None, None, {"LMER_PORT_COUNT": "  "})
        assert count == 0

    def test_cli_count_zero_overrides_env(self):
        # An explicit --ports 0 turns the feature off even if the env requests some.
        count, _ = _resolve_requested_ports(0, None, {"LMER_PORT_COUNT": "5"})
        assert count == 0

    def test_non_numeric_env_count_raises(self):
        with pytest.raises(ValueError):
            _resolve_requested_ports(None, None, {"LMER_PORT_COUNT": "abc"})

    def test_negative_count_raises(self):
        with pytest.raises(ValueError):
            _resolve_requested_ports(-1, None, {})


class TestResolvePortBind:
    def test_default_bind_constant(self):
        # Loopback by default so published ports are not network-exposed.
        assert DEFAULT_PORT_BIND == "127.0.0.1"

    def test_default_when_nothing_set(self):
        assert _resolve_port_bind(None, {}) == DEFAULT_PORT_BIND

    def test_cli_wins_over_env(self):
        assert (
            _resolve_port_bind("0.0.0.0", {"LMER_PORT_BIND": "10.0.0.1"})
            == "0.0.0.0"
        )

    def test_env_fallback(self):
        assert _resolve_port_bind(None, {"LMER_PORT_BIND": "0.0.0.0"}) == "0.0.0.0"

    def test_blank_env_falls_through_to_default(self):
        # An exported but empty LMER_PORT_BIND should not bind to "".
        assert _resolve_port_bind(None, {"LMER_PORT_BIND": "   "}) == DEFAULT_PORT_BIND


class TestResolveFastapiHostPort:
    """Where `--fastapi` gets published (issue #141).

    The platform orchestrator picks a port, writes it into the session's registry
    entry, and *then* spawns lmer. If lmer picked its own anyway, that entry would
    point at a port nothing is listening on — the failure would look like a
    working session until someone tried to drive it.
    """

    def test_default_range_constant(self):
        # Unchanged from when the range was an inline literal in main().
        assert DEFAULT_FASTAPI_PORT_RANGE == "8700-8799"

    def test_picks_from_the_default_range_when_nothing_is_set(self):
        port = _resolve_fastapi_host_port(None, {})
        assert 8700 <= port <= 8799

    def test_picks_from_the_requested_range(self):
        port = _resolve_fastapi_host_port("9000-9010", {})
        assert 9000 <= port <= 9010

    def test_preset_env_port_is_used_verbatim(self):
        assert _resolve_fastapi_host_port(None, {"LMER_FASTAPI_PORT": "8765"}) == 8765

    def test_preset_env_port_wins_over_the_range(self):
        """The range only says where to *pick*; a preset port is a commitment."""
        port = _resolve_fastapi_host_port("9000-9010", {"LMER_FASTAPI_PORT": "8765"})
        assert port == 8765

    def test_preset_env_port_is_not_second_guessed_when_busy(self):
        """Relocating a committed port silently is the failure this exists to stop.

        A busy port fails loudly when the container publishes it; a *different*
        port fails quietly, months later, in something that was reading the
        address the platform recorded.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.bind(("127.0.0.1", 0))
            busy = holder.getsockname()[1]
            resolved = _resolve_fastapi_host_port(None, {"LMER_FASTAPI_PORT": str(busy)})
        assert resolved == busy

    def test_blank_env_port_falls_through_to_picking(self):
        port = _resolve_fastapi_host_port(None, {"LMER_FASTAPI_PORT": "   "})
        assert 8700 <= port <= 8799

    def test_non_numeric_env_port_is_an_error(self):
        with pytest.raises(ValueError, match="LMER_FASTAPI_PORT must be an integer"):
            _resolve_fastapi_host_port(None, {"LMER_FASTAPI_PORT": "eight-thousand"})

    @pytest.mark.parametrize("value", ["0", "-1", "65536"])
    def test_out_of_range_env_port_is_an_error(self, value):
        with pytest.raises(ValueError, match="LMER_FASTAPI_PORT must be 1-65535"):
            _resolve_fastapi_host_port(None, {"LMER_FASTAPI_PORT": value})

    def test_unparseable_range_is_an_error(self):
        with pytest.raises(ValueError):
            _resolve_fastapi_host_port("not-a-range", {})

    def test_exhausted_range_raises(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.bind(("127.0.0.1", 0))
            busy = holder.getsockname()[1]
            with pytest.raises(RuntimeError):
                _resolve_fastapi_host_port(f"{busy}-{busy}", {})


class TestPublishHostPorts:
    def test_appends_publish_args_for_each_port(self):
        run: list[str] = []
        _publish_host_ports(run, [8842, 8857])
        assert run == [
            "-p", "127.0.0.1:8842:8842",
            "-p", "127.0.0.1:8857:8857",
        ]

    def test_empty_list_is_noop(self):
        run = ["existing"]
        _publish_host_ports(run, [])
        assert run == ["existing"]

    def test_custom_bind_used_in_publish_args(self):
        run: list[str] = []
        _publish_host_ports(run, [8842], bind="0.0.0.0")
        assert run == ["-p", "0.0.0.0:8842:8842"]


class TestApplyPortPassthrough:
    @staticmethod
    def _ns(ports=None, port_pool=None, port_bind=None):
        return argparse.Namespace(ports=ports, port_pool=port_pool, port_bind=port_bind)

    def test_noop_when_no_ports_requested(self, monkeypatch):
        monkeypatch.delenv("LMER_PORT_COUNT", raising=False)
        env: dict = {}
        run: list[str] = []
        assert _apply_port_passthrough(self._ns(), env, run) is None
        assert "LMER_PORTS" not in env
        assert run == []

    def test_allocates_publishes_and_exports(self, monkeypatch):
        monkeypatch.delenv("LMER_PORT_COUNT", raising=False)
        monkeypatch.delenv("LMER_PORT_POOL", raising=False)
        monkeypatch.delenv("LMER_PORT_BIND", raising=False)
        env: dict = {}
        run: list[str] = []
        rc = _apply_port_passthrough(self._ns(ports=2, port_pool="9000-9100"), env, run)
        assert rc is None
        ports = [int(p) for p in env["LMER_PORTS"].split(",")]
        assert len(ports) == 2
        assert all(9000 <= p <= 9100 for p in ports)
        # run holds a "-p 127.0.0.1:P:P" pair per allocated port (default bind).
        assert run.count("-p") == 2
        for p in ports:
            assert f"127.0.0.1:{p}:{p}" in run

    def test_invalid_pool_returns_error_code(self, monkeypatch):
        monkeypatch.delenv("LMER_PORT_COUNT", raising=False)
        env: dict = {}
        run: list[str] = []
        rc = _apply_port_passthrough(self._ns(ports=1, port_pool="not-a-range"), env, run)
        assert rc == 2
        assert "LMER_PORTS" not in env
        assert run == []

    def test_pool_too_small_returns_error_code(self, monkeypatch):
        monkeypatch.delenv("LMER_PORT_COUNT", raising=False)
        env: dict = {}
        run: list[str] = []
        # A one-wide pool cannot satisfy a request for two ports.
        rc = _apply_port_passthrough(self._ns(ports=2, port_pool="9200-9200"), env, run)
        assert rc == 2
        assert "LMER_PORTS" not in env

    def test_env_var_fallback_when_no_cli_flags(self, monkeypatch):
        monkeypatch.setenv("LMER_PORT_COUNT", "1")
        monkeypatch.setenv("LMER_PORT_POOL", "9300-9400")
        monkeypatch.delenv("LMER_PORT_BIND", raising=False)
        env: dict = {}
        run: list[str] = []
        rc = _apply_port_passthrough(self._ns(), env, run)
        assert rc is None
        (port,) = env["LMER_PORTS"].split(",")
        assert 9300 <= int(port) <= 9400

    def test_cli_port_bind_publishes_on_that_address(self, monkeypatch):
        monkeypatch.delenv("LMER_PORT_COUNT", raising=False)
        monkeypatch.delenv("LMER_PORT_POOL", raising=False)
        monkeypatch.delenv("LMER_PORT_BIND", raising=False)
        env: dict = {}
        run: list[str] = []
        rc = _apply_port_passthrough(
            self._ns(ports=1, port_pool="9500-9600", port_bind="0.0.0.0"), env, run
        )
        assert rc is None
        (port,) = env["LMER_PORTS"].split(",")
        assert f"0.0.0.0:{port}:{port}" in run
        # No 127.0.0.1 mapping should leak when an override is in effect.
        assert not any(s.startswith("127.0.0.1:") for s in run)

    def test_env_port_bind_used_when_cli_not_set(self, monkeypatch):
        monkeypatch.delenv("LMER_PORT_COUNT", raising=False)
        monkeypatch.delenv("LMER_PORT_POOL", raising=False)
        monkeypatch.setenv("LMER_PORT_BIND", "0.0.0.0")
        env: dict = {}
        run: list[str] = []
        rc = _apply_port_passthrough(self._ns(ports=1, port_pool="9700-9800"), env, run)
        assert rc is None
        (port,) = env["LMER_PORTS"].split(",")
        assert f"0.0.0.0:{port}:{port}" in run

    def test_cli_port_bind_wins_over_env(self, monkeypatch):
        monkeypatch.delenv("LMER_PORT_COUNT", raising=False)
        monkeypatch.delenv("LMER_PORT_POOL", raising=False)
        monkeypatch.setenv("LMER_PORT_BIND", "10.255.255.255")  # unroutable on purpose
        env: dict = {}
        run: list[str] = []
        rc = _apply_port_passthrough(
            self._ns(ports=1, port_pool="9810-9820", port_bind="127.0.0.1"), env, run
        )
        assert rc is None
        (port,) = env["LMER_PORTS"].split(",")
        assert f"127.0.0.1:{port}:{port}" in run

    def test_invalid_bind_returns_error_code(self, monkeypatch):
        # An address the host can't bind (here a label under .invalid — RFC
        # 6761 guarantees DNS resolution fails) makes every per-port bind in
        # _pick_ports raise gaierror; the inner loop swallows it as "port
        # busy" and after the pool is exhausted _pick_ports raises
        # RuntimeError, which we catch and surface as exit code 2.
        monkeypatch.delenv("LMER_PORT_COUNT", raising=False)
        monkeypatch.delenv("LMER_PORT_POOL", raising=False)
        env: dict = {}
        run: list[str] = []
        rc = _apply_port_passthrough(
            self._ns(ports=1, port_pool="9900-9910", port_bind="not.a.real.host.invalid"),
            env, run,
        )
        assert rc == 2
        assert "LMER_PORTS" not in env


def test_cli_declares_port_passthrough_flags():
    """Guard against accidental removal of the --ports / --port-pool / --port-bind flags."""
    source = CLI_PY.read_text()
    assert re.search(r'add_argument\(\s*["\']--ports["\']', source), "--ports flag missing"
    assert re.search(r'add_argument\(\s*["\']--port-pool["\']', source), "--port-pool flag missing"
    assert re.search(r'add_argument\(\s*["\']--port-bind["\']', source), "--port-bind flag missing"


def test_cli_exports_allocated_ports_via_lmer_ports():
    """The allocated ports must be exported to the container as LMER_PORTS.

    Without this passthrough the agent inside the container has no way to learn
    which published ports it may bind services to.
    """
    source = CLI_PY.read_text()
    assert re.search(r'env\[["\']LMER_PORTS["\']\]\s*=', source), (
        "main() must export the allocated ports via env['LMER_PORTS']"
    )


def test_cli_publishes_the_resolved_fastapi_port():
    """main() must publish the *resolved* port, not one it picks independently.

    Source-level like its neighbours: the block lives inline in main(), between
    the .env merge and the container invocation, and cannot be reached without a
    container runtime.
    """
    source = CLI_PY.read_text()
    assert re.search(r"_resolve_fastapi_host_port\(\s*ns\.fastapi_port_range", source), (
        "main() must resolve the FastAPI port through _resolve_fastapi_host_port "
        "so a preset LMER_FASTAPI_PORT is honored"
    )
    assert re.search(r'env\[["\']LMER_FASTAPI_PORT["\']\]\s*=\s*str\(host_port\)', source)
    assert re.search(r"_publish_host_ports\(\s*run,\s*\[host_port\]", source)


def test_cli_forwards_a_host_set_fastapi_token():
    """A spawner that minted the bearer token must have it reach the container.

    Otherwise the in-container supervisor generates its own and the spawner's
    recorded copy opens nothing. The flag still wins over the env var.
    """
    source = CLI_PY.read_text()
    assert re.search(
        r'["\']LMER_FASTAPI_TOKEN["\']\s*:\s*ns\.fastapi_token'
        r'\s*or\s*os\.environ\.get\(\s*["\']LMER_FASTAPI_TOKEN["\']\s*\)',
        source,
    ), "the env dict must fall back to $LMER_FASTAPI_TOKEN when --fastapi-token is absent"


class TestRecordPublishedPorts:
    """Port reporting for the orchestrator platform (issue #141).

    The platform spawns `lmer` but cannot know which ports it will publish — free
    ports are picked here, at launch. Without this file the mapping reaches only
    stdout, and the platform's UI can never link what a session is serving.
    """

    @staticmethod
    def _ns(ports=None, port_pool=None, port_bind=None):
        return argparse.Namespace(ports=ports, port_pool=port_pool, port_bind=port_bind)

    def test_noop_when_env_var_unset(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LMER_PLATFORM_PORTS_FILE", raising=False)
        _record_published_ports([9001], "127.0.0.1")
        assert list(tmp_path.iterdir()) == []

    def test_writes_the_mapping(self, monkeypatch, tmp_path):
        target = tmp_path / "nested" / "ports.json"
        monkeypatch.setenv("LMER_PLATFORM_PORTS_FILE", str(target))
        _record_published_ports([9001, 9002], "0.0.0.0")

        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["bind"] == "0.0.0.0"
        assert payload["ports"] == [
            {"host": 9001, "container": 9001},
            {"host": 9002, "container": 9002},
        ]

    def test_write_is_atomic(self, monkeypatch, tmp_path):
        target = tmp_path / "ports.json"
        monkeypatch.setenv("LMER_PLATFORM_PORTS_FILE", str(target))
        _record_published_ports([9001], "127.0.0.1")
        assert list(tmp_path.glob(".*.tmp")) == []

    def test_failure_is_a_warning_not_a_launch_failure(self, monkeypatch, tmp_path):
        """Failing to record a port must never fail the launch it annotates."""
        monkeypatch.setenv("LMER_PLATFORM_PORTS_FILE", str(tmp_path / "p.json"))
        monkeypatch.setattr(
            Path, "write_text",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("read-only")),
        )
        _record_published_ports([9001], "127.0.0.1")  # must not raise

    def test_passthrough_records_when_requested(self, monkeypatch, tmp_path):
        for name in ("LMER_PORT_COUNT", "LMER_PORT_POOL", "LMER_PORT_BIND"):
            monkeypatch.delenv(name, raising=False)
        target = tmp_path / "ports.json"
        monkeypatch.setenv("LMER_PLATFORM_PORTS_FILE", str(target))

        env: dict = {}
        run: list[str] = []
        assert _apply_port_passthrough(
            self._ns(ports=2, port_pool="9000-9100"), env, run
        ) is None

        recorded = json.loads(target.read_text(encoding="utf-8"))["ports"]
        exported = [int(p) for p in env["LMER_PORTS"].split(",")]
        assert [p["host"] for p in recorded] == exported
