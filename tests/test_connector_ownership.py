"""Behavioral connector regressions; original source journals remain available."""
import threading
import time

import pytest

from lmer_cli import thread_posts
from tests.connector_fakes import MatrixRig, ROOM, SlackRig, isolated_connectors
from tests.timing_budgets import DRAIN, JOIN, QUIET

@pytest.mark.parametrize("channel", ["slack", "matrix"])
def test_competing_actual_startups_send_only_from_first_owner(channel, tmp_path, monkeypatch):
    factory = SlackRig if channel == "slack" else MatrixRig
    rigs = []
    for name in ("first", "second"):
        path = tmp_path / name
        path.mkdir()
        rig = factory(path)
        rig.agent("message", name + " owner delivery")
        read = rig.bus.messages
        def short_read(cursor, _read=read, **kwargs):
            time.sleep(0.005)
            return _read(cursor, timeout=0)
        monkeypatch.setattr(rig.bus, "messages", short_read)
        rigs.append(rig)
    def sent(rig):
        return rig.slack.posted if channel == "slack" else rig.matrix.sent
    try:
        rigs[0].connector.start()
        rigs[0].wait_until(lambda: bool(sent(rigs[0])), timeout=JOIN)
        try:
            rigs[1].connector.start()
        except RuntimeError:
            pass  # A synchronous ownership refusal is also valid.
        time.sleep(QUIET)
        assert sent(rigs[0]), "first startup did not reach remote delivery"
        assert sent(rigs[1]) == [], "competing startup delivered before owning the thread"
        assert all(rig.log.read_since(0)[0] for rig in rigs)
    finally:
        workers = []
        for rig in rigs:
            with rig.connector._threads_lock:
                workers.extend(rig.connector._threads)
            rig.connector.stop(timeout=DRAIN)
        for worker in workers:
            worker.join(timeout=JOIN)
        assert all(not worker.is_alive() for worker in workers)
        for rig in rigs:
            rig.bus._session._http.close()
            rig.log.close()
