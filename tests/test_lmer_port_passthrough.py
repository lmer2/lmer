"""Tests for general port passthrough (--ports / --port-pool, issue #57).

Covers two layers:
1. The pure resolver ``_resolve_requested_ports`` in cli.py — CLI args win over
   the LMER_PORT_COUNT / LMER_PORT_POOL env vars, with the documented default
   pool and validation behavior.
2. Source-level guards that the CLI declares the flags and that the allocated
   ports are exported to the container via LMER_PORTS. The publish/allocate
   logic in main() is built inline, so (like the LMER_REASONING_EFFORT guard)
   a source check catches accidental removal without re-testing podman wiring.
"""
import argparse
import re
from pathlib import Path

import pytest

from lmer_cli.cli import (
    DEFAULT_PORT_POOL,
    _apply_port_passthrough,
    _publish_loopback_ports,
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


class TestPublishLoopbackPorts:
    def test_appends_publish_args_for_each_port(self):
        run: list[str] = []
        _publish_loopback_ports(run, [8842, 8857])
        assert run == [
            "-p", "127.0.0.1:8842:8842",
            "-p", "127.0.0.1:8857:8857",
        ]

    def test_empty_list_is_noop(self):
        run = ["existing"]
        _publish_loopback_ports(run, [])
        assert run == ["existing"]


class TestApplyPortPassthrough:
    @staticmethod
    def _ns(ports=None, port_pool=None):
        return argparse.Namespace(ports=ports, port_pool=port_pool)

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
        env: dict = {}
        run: list[str] = []
        rc = _apply_port_passthrough(self._ns(ports=2, port_pool="9000-9100"), env, run)
        assert rc is None
        ports = [int(p) for p in env["LMER_PORTS"].split(",")]
        assert len(ports) == 2
        assert all(9000 <= p <= 9100 for p in ports)
        # run holds a "-p 127.0.0.1:P:P" pair per allocated port.
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
        env: dict = {}
        run: list[str] = []
        rc = _apply_port_passthrough(self._ns(), env, run)
        assert rc is None
        (port,) = env["LMER_PORTS"].split(",")
        assert 9300 <= int(port) <= 9400


def test_cli_declares_port_passthrough_flags():
    """Guard against accidental removal of the --ports / --port-pool flags."""
    source = CLI_PY.read_text()
    assert re.search(r'add_argument\(\s*["\']--ports["\']', source), "--ports flag missing"
    assert re.search(r'add_argument\(\s*["\']--port-pool["\']', source), "--port-pool flag missing"


def test_cli_exports_allocated_ports_via_lmer_ports():
    """The allocated ports must be exported to the container as LMER_PORTS.

    Without this passthrough the agent inside the container has no way to learn
    which published ports it may bind services to.
    """
    source = CLI_PY.read_text()
    assert re.search(r'env\[["\']LMER_PORTS["\']\]\s*=', source), (
        "main() must export the allocated ports via env['LMER_PORTS']"
    )
