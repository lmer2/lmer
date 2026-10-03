"""Behavioral connector regressions; original source journals remain available."""
import threading
import time

import pytest

from lmer_cli import thread_posts
from tests.connector_fakes import MatrixRig, ROOM, SlackRig, isolated_connectors
from tests.timing_budgets import DRAIN, JOIN

@pytest.mark.parametrize("channel", ["slack", "matrix"])
def test_real_startup_retries_plain_inbound_exception(channel, tmp_path, monkeypatch):
    rig = (SlackRig if channel == "slack" else MatrixRig)(tmp_path)
    connector = rig.connector
    remote = rig.slack if channel == "slack" else rig.matrix
    method = "get_replies" if channel == "slack" else "sync"
    original = getattr(remote, method)
    calls = []
    next_poll = threading.Event()
    def fail_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("injected plain inbound failure")
        next_poll.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(remote, method, fail_once)
    monkeypatch.setattr(connector, "_backoff", lambda *args: None)
    read = rig.bus.messages
    def short_read(cursor, **kwargs):
        time.sleep(0.002)
        return read(cursor, timeout=0)
    monkeypatch.setattr(rig.bus, "messages", short_read)
    workers = []
    try:
        connector.start()
        retried = next_poll.wait(JOIN)
        assert calls, "the real startup did not reach the injected transport"
        assert retried, f"real startup stopped polling after {len(calls)} transport call(s)"
    finally:
        with connector._threads_lock:
            workers = list(connector._threads)
        connector.stop(timeout=DRAIN)
        for worker in workers:
            worker.join(timeout=JOIN)
        assert all(not worker.is_alive() for worker in workers)
        rig.bus._session._http.close()
        rig.log.close()



@pytest.mark.parametrize("channel", ["slack", "matrix"])
@pytest.mark.parametrize("deferred", [False, True])
def test_real_startup_validates_identity_before_restoring_cursor(channel, deferred, tmp_path, monkeypatch):
    from lmer_cli.connector_delivery import checkpoint_path
    factory = SlackRig if channel == "slack" else MatrixRig
    rig = factory(tmp_path)
    old = rig.connector
    old._ensure_ready()
    old._bus_cursor = 42
    old._state.save()
    path = checkpoint_path(old._lease.key, old._state.session)
    old.stop(timeout=DRAIN, announce=False)
    cls = type(old)
    remote_kw = {"slack": rig.slack} if channel == "slack" else {"matrix": rig.matrix}
    fresh = cls(old.config, bus_client=rig.bus, **remote_kw)
    fresh._bus_poll_seconds = .02
    original = rig.bus.health
    allow = threading.Event()
    attempted = threading.Event()
    def health():
        data = original()
        data["subscribers"] = {fresh.subscriber: 40}
        if deferred and not allow.is_set():
            data.pop("session", None)
            attempted.set()
        return data
    monkeypatch.setattr(rig.bus, "health", health)
    before = path.read_bytes()
    try:
        fresh.start()
        if deferred:
            assert attempted.wait(JOIN)
            time.sleep(.05)
            assert path.read_bytes() == before
            assert fresh._state.session is None
            allow.set()
        deadline = time.monotonic() + JOIN
        while not fresh._connected and time.monotonic() < deadline:
            time.sleep(.01)
        assert fresh._connected
        assert fresh._bus_cursor >= 42
        assert fresh._state.session == rig.log.session
    finally:
        allow.set()
        assert fresh.stop(timeout=DRAIN, announce=False)
        rig.bus._session._http.close()
        rig.log.close()


@pytest.mark.parametrize("channel", ["slack", "matrix"])
def test_other_supervisor_checkpoint_is_preserved_without_import(channel, tmp_path):
    from lmer_cli.connector_delivery import checkpoint_path
    factory = SlackRig if channel == "slack" else MatrixRig
    rig = factory(tmp_path)
    first = rig.connector
    first._ensure_ready()
    first._bus_cursor = 42
    first._state.save()
    path = checkpoint_path(first._lease.key, first._state.session)
    first.stop(timeout=DRAIN, announce=False)
    before = path.read_bytes()
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = factory(other_dir)
    # Give the actual second supervisor an independent identity.
    other.log.session = "different-supervisor"
    try:
        other.connector._ensure_ready()
        assert other.connector._state.session == "different-supervisor"
        assert other.connector._bus_cursor == 0
        assert path.read_bytes() == before
    finally:
        other.connector.stop(timeout=DRAIN, announce=False)
        for item in (rig, other):
            item.bus._session._http.close()
            item.log.close()
