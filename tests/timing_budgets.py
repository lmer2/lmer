"""Scaled wall-clock budgets for tests that wait on real processes.

Poll intervals and product-default assertions stay unscaled; see docs/GATE-FASTPATH.md.
"""
from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest

#: The gate runner supplies its worker count; explicit values take precedence.
WAIT_SCALE_ENV = "LMER_TESTS_WAIT_SCALE"

#: Requires --dist loadgroup (set by ci/gate-tests.py).
XDIST_GROUP = "process_spawning"

#: Reject tightened bounds and excessively long waits from mistyped factors.
SCALE_LIMITS = (1.0, 32.0)


def _scale() -> float:
    raw = os.environ.get(WAIT_SCALE_ENV, "").strip()
    if not raw:
        return 1.0
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not SCALE_LIMITS[0] <= value <= SCALE_LIMITS[1]:
        raise RuntimeError(
            f"{WAIT_SCALE_ENV}={raw!r} is not a wait-scale factor between "
            f"{SCALE_LIMITS[0]} and {SCALE_LIMITS[1]}. Unset it for a serial "
            "run; the gate runner sets it to its worker count."
        )
    return value


#: Read once so environment isolation cannot give budgets different factors.
SCALE = _scale()


def stretch(seconds: float) -> float:
    """Scale a base duration for the current run."""
    return round(seconds * SCALE, 3)


def outlast(budget: float) -> float:
    """Derive a stall that outlives the given budget at every scale."""
    return round(budget * 3.0, 3)


# Wait ceilings in base seconds; successful waits return early.

#: A socket, a bus or a supervisor becoming reachable.
LINK = stretch(30.0)
#: Default wait for an observable child state.
SETTLE = stretch(120.0)
#: A child reacting to a signal, a reload or a closed lease and exiting.
SHUTDOWN = stretch(60.0)
#: Must exceed the product's 45s drain for drain_connectors to assert completion.
DRAIN = stretch(60.0)
#: An in-process thread finishing, or an event the test posted being picked up.
JOIN = stretch(90.0)
#: A CLI subprocess run to completion: a recovery CLI, or a `work` verb in a
#: scratch clone (a release claim fetches, commits, pushes and re-fetches).
CLI = stretch(90.0)
#: A Git hook or child parked at a barrier until the test releases it; must
#: outlive every wait the test performs before the release.
BARRIER = outlast(SETTLE)
#: Interleaving probe in test_connector_ingestion.py.
SCRIPT = stretch(360.0)
#: The contained orphan probe can perform three SETTLE waits in sequence.
PROBE = outlast(SETTLE)
#: Deliberately expired by the probe-kill test, so kept small.
PROBE_KILL = stretch(10.0)
#: The probe supervisor's own reaping deadline after it has sent SIGKILL.
REAP = stretch(5.0)
#: Delivering a 257-message backlog through a real child.
BACKLOG = stretch(900.0)
#: Deliberately expired by tests of connection failure.
CONNECT_EXPIRED = stretch(0.3)

# Fixture child budgets.

#: Exceeding this drain exits the fixture child without re-exec.
CHILD_DRAIN = stretch(30.0)
#: Match terminate_wait's headroom so SIGKILL cannot race drain completion.
STOP = CHILD_DRAIN + stretch(2.0)
#: Small because drain-expiry tests wait it out on every run.
CHILD_DRAIN_EXPIRED = stretch(5.0)
#: ``LMER_COMMS_CONNECT_BUDGET`` inside a fixture child.
CHILD_CONNECT = stretch(30.0)
#: What a test asserts an expired drain finished inside, from the outside.
DRAIN_DEADLINE = outlast(CHILD_DRAIN_EXPIRED)
#: How long a reload may take to reach the child's ``stop()``.
RELOAD_LATENCY = stretch(10.0)
#: Outlast the test's reaction window but expire before the held-open drain.
FIXTURE_RELEASE = outlast(SHUTDOWN)

#: Allow a signalled child to write its faulthandler dump before rendering failure.
DUMP_SECONDS = stretch(2.0)

# Supervisor teardown budgets.

#: Assertion bound from shutdown request to child exit.
TEARDOWN_BOUND = stretch(2.0)
#: Handoff delay, also used as a duration assertion's lower bound.
HANDOFF = stretch(0.2)
#: Must exceed TEARDOWN_BOUND so the assertion detects waiting out the handoff.
LATE_HANDOFF = outlast(TEARDOWN_BOUND)
#: How quickly a delivery wait must give up when the bus is busy.
BUSY_BOUND = stretch(0.2)
#: A spawned child that must outlive whatever the test does to the supervisor.
CHILD_ALIVE = outlast(TEARDOWN_BOUND)
#: Observation window for absence of activity; must allow scheduling under load.
QUIET = stretch(0.5)
#: Keep a short-lived child alive long enough for its driver to act.
SHORT_CHILD = stretch(0.3)

# Poll intervals stay unscaled.

#: Poll interval for the wait helpers.
POLL_SECONDS = 0.02
#: Poll interval a fixture child is given for its module monitor.
CHILD_POLL_SECONDS = 0.05


def child_bounds() -> dict:
    """Environment entries giving fixture children the parent's scaled budgets."""
    return {
        "CONNECTOR_FIXTURE_DRAIN": str(CHILD_DRAIN),
        "CONNECTOR_FIXTURE_POLL": str(CHILD_POLL_SECONDS),
        "CONNECTOR_FIXTURE_SETTLE": str(SETTLE),
        "LMER_COMMS_CONNECT_BUDGET": str(CHILD_CONNECT),
    }


def wait_until(predicate, *, budget: float, poll: float = POLL_SECONDS) -> bool:
    """Poll ``predicate`` until it holds or ``budget`` runs out."""
    deadline = time.monotonic() + budget
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def child_evidence(harness, *extra) -> str:
    """Render child pids, exit states and logs, explicitly naming empty or missing logs."""
    lines = []
    for process in getattr(harness, "children", []):
        code = process.poll()
        state = "running" if code is None else f"exited {code}"
        lines.append(f"[child pid={process.pid}] {state}")
    seen = set()
    for path in [*getattr(harness, "log_paths", []), *extra]:
        path = Path(path)
        if path in seen:
            continue
        seen.add(path)
        if not path.exists():
            lines.append(f"--- {path}: never written")
            continue
        text = path.read_text(errors="replace")
        lines.append(f"--- {path}" + ("\n" + text if text else ": empty"))
    return "\n".join(lines) or "no child, no log"


def wait_for(predicate, harness, budget: float = SETTLE):
    """Wait for a child state or fail with logs and SIGUSR1 faulthandler dumps."""
    if wait_until(predicate, budget=budget):
        return
    for process in harness.children:
        if process.poll() is None:
            process.send_signal(signal.SIGUSR1)
    time.sleep(DUMP_SECONDS)
    pytest.fail(f"child condition timed out after {budget}s\n" + child_evidence(harness))
