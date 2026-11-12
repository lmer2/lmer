"""Connector delivery health reaches the chat only when it lasts.

A connector retrying through a transient error used to post a "retrying" and
a "recovered" note into the conversation every time; these pin the grace
window, the pairing, and the cooldown that replaced that.
"""
from __future__ import annotations

import pytest

from lmer_cli import connector_lifecycle
from tests.connector_fakes import SlackRig, isolated_connectors  # noqa: F401
from tests.timing_budgets import DRAIN


class Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now


@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(connector_lifecycle.time, "time", clock)
    made = SlackRig(tmp_path)
    connector = made.connector
    connector._ensure_ready()
    monkeypatch.setattr(connector, "_backoff", lambda: None)
    try:
        yield made, connector, clock
    finally:
        connector.stop(timeout=DRAIN, announce=False)


def _health_notes(made):
    return [row for row in made.log.read_since(0)[0]
            if row.kind == "note" and row.meta.get("notification_class") == "connector_health"]


def _run(connector, clock, outcomes, *, step_seconds):
    """Drive the inbound loop through *outcomes* (``True`` succeeds), *step_seconds* apart."""
    queue = list(outcomes)

    def step():
        clock.now += step_seconds
        ok = queue.pop(0)
        if not queue:
            connector._stop.set()
        if not ok:
            raise RuntimeError("transient")

    connector._loop("inbound", step)
    connector._stop.clear()


def _incident(failures):
    return [False] * failures + [True]


def _states(made):
    return [row.meta[connector_lifecycle.HEALTH_STATE_KEY] for row in _health_notes(made)]


def test_a_failure_that_clears_inside_the_grace_window_posts_nothing(rig):
    made, connector, clock = rig
    _run(connector, clock, _incident(5), step_seconds=10)
    assert _health_notes(made) == []
    assert connector._delivery_health["inbound"]["failures"] == 0


def test_a_failure_past_the_grace_window_posts_one_pair(rig):
    made, connector, clock = rig
    _run(connector, clock, _incident(6), step_seconds=15)
    assert _states(made) == ["failing", "recovered"]
    assert all(row.meta["direction"] == "inbound" for row in _health_notes(made))


def test_the_grace_window_counts_from_the_first_failure_across_a_restart(rig, tmp_path):
    made, connector, clock = rig
    _run(connector, clock, [False] * 3, step_seconds=15)
    since = connector._delivery_health["inbound"]["since"]
    assert _health_notes(made) == []
    assert connector.stop(timeout=DRAIN, announce=False)
    restarted = type(connector)(made.config, slack=made.slack, bus_client=made.bus)
    restarted._ensure_ready()
    restarted._backoff = lambda: None
    try:
        assert restarted._delivery_health["inbound"]["since"] == since
        _run(restarted, clock, _incident(2), step_seconds=15)
        assert _states(made) == ["failing", "recovered"]
    finally:
        restarted.stop(timeout=DRAIN, announce=False)


def test_a_flapping_connector_reads_as_one_incident_inside_the_cooldown(rig):
    made, connector, clock = rig
    _run(connector, clock, _incident(6), step_seconds=15)
    assert len(_health_notes(made)) == 2
    # Another long incident inside the cooldown stays out of the chat…
    _run(connector, clock, _incident(6), step_seconds=15)
    assert len(_health_notes(made)) == 2
    # …and one after it is noted again.
    clock.now += connector_lifecycle.HEALTH_COOLDOWN_SECONDS
    _run(connector, clock, _incident(6), step_seconds=15)
    assert len(_health_notes(made)) == 4


def test_an_outage_that_outlasts_the_cooldown_is_still_noted(rig):
    made, connector, clock = rig
    _run(connector, clock, _incident(6), step_seconds=15)
    # Failing for longer than the cooldown: the note comes once it lapses,
    # not never.
    _run(connector, clock, _incident(7), step_seconds=45)
    assert _states(made) == ["failing", "recovered", "failing", "recovered"]


def test_giving_up_does_not_wait_for_the_grace_window(rig, monkeypatch):
    made, connector, clock = rig
    warned = []
    monkeypatch.setattr(connector, "_warn", warned.append)
    calls = {"n": 0}

    def step():
        calls["n"] += 1
        raise RuntimeError("down")

    connector._loop("inbound", step)
    assert calls["n"] == connector_lifecycle.FAILURE_LIMIT
    assert warned, "a connector that gives up says so"
    assert _health_notes(made) == []


def test_a_failing_note_the_bus_refused_is_tried_again_and_never_answered_alone(rig, monkeypatch):
    made, connector, clock = rig
    post = connector.bus.post
    refusing = {"on": True}

    def flaky_post(kind, text, **kwargs):
        if refusing["on"]:
            raise RuntimeError("bus down")
        return post(kind, text, **kwargs)

    monkeypatch.setattr(connector.bus, "post", flaky_post)
    _run(connector, clock, [False] * 5, step_seconds=15)
    assert not connector._delivery_health["inbound"].get("noted")
    refusing["on"] = False
    _run(connector, clock, _incident(1), step_seconds=15)
    assert _states(made) == ["failing", "recovered"]


def test_a_recovered_note_the_bus_refused_is_tried_again_on_the_next_success(rig, monkeypatch):
    made, connector, clock = rig
    post = connector.bus.post
    refusing = {"on": False}

    def flaky_post(kind, text, **kwargs):
        if refusing["on"]:
            raise RuntimeError("bus down")
        return post(kind, text, **kwargs)

    monkeypatch.setattr(connector.bus, "post", flaky_post)
    _run(connector, clock, [False] * 5, step_seconds=15)
    assert _states(made) == ["failing"]
    refusing["on"] = True
    _run(connector, clock, [True], step_seconds=15)
    health = connector._delivery_health["inbound"]
    assert health.get("noted") and not health.get("quiet_until"), \
        "an unanswered failing note keeps its incident open, with no cooldown yet"
    refusing["on"] = False
    _run(connector, clock, [True], step_seconds=15)
    assert _states(made) == ["failing", "recovered"]
    assert not connector._delivery_health["inbound"].get("noted")
    _run(connector, clock, [True], step_seconds=15)
    assert _states(made) == ["failing", "recovered"]


def test_the_real_backoff_notes_a_failure_before_the_connector_gives_up(rig, monkeypatch):
    # Every other test here stubs the backoff. With instant failures, the
    # schedule and FAILURE_LIMIT leave one failure past the grace window; if
    # either constant moves so that none is, a give-up would post nothing.
    made, connector, clock = rig
    monkeypatch.delattr(connector, "_backoff")
    wait = connector._stop.wait

    def advance(timeout=None):
        clock.now += timeout or 0
        return wait(0)

    monkeypatch.setattr(connector._stop, "wait", advance)
    began = clock.now
    calls = {"n": 0}

    def step():
        calls["n"] += 1
        raise RuntimeError("down")

    connector._loop("inbound", step)
    assert calls["n"] == connector_lifecycle.FAILURE_LIMIT
    assert _states(made) == ["failing"]
    assert clock.now - began >= connector_lifecycle.HEALTH_GRACE_SECONDS
