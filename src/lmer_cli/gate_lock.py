"""
Gate-in-flight markers: "is a long gate running right now?" (issue #201).

Three mechanisms that are each correct alone used to be structurally in each
other's way, and none of them knew the others existed:

1. the Stop hook (``hooks/run_state_guard.py`` trigger 2) refuses to let a
   session stop while its run dir is unpushed, and mandates ``work commit``;
2. the test suite's ``/work`` leak guard (``tests/conftest.py``) fails when the
   operational work repo changed during a run;
3. gate receipts (``work_repo.run_state.emit_gate_event``) append to the run
   dir, so **every** gate run leaves it dirty.

A ~14-minute suite makes background gating the normal pattern, so ending a turn
inside that window fired the Stop hook, the mandated ``work commit`` swept
tracked run-dir files into a commit, and the running suite watched them vanish
underneath it. Self-sustaining, too: the receipt dirties the run dir, which arms
the nudge, which lands a commit inside the next gate's window.

This module is the coordination point. A gate command holds a marker for its
whole run (:func:`hold_gate_lock`); everything that would write the work repo
asks whether one is live (:func:`active_gate`) and defers instead. What each
consumer does with the answer lives with the consumer:

- ``work_repo.git_ops.commit_work_path`` — defers the commit+push, records a
  ``commit_deferred`` event, and returns 0;
- ``work resume --json`` — exposes ``gate_in_flight`` so the Stop hook can
  suppress its push nudge without importing project code;
- the gate scripts and ``work verify`` — the producers.

Liveness is a **fact taken from the operating system**, not a prediction about
how long a gate "should" take: a marker counts only while its pid is alive, and
a dead one is pruned on the next read. The age cap exists solely as a pid-reuse
backstop and is deliberately far longer than any real gate (see
:data:`STALE_AFTER_SECONDS`) — a cap that could expire mid-gate would
reintroduce the very bug this module fixes, at the moment it is most expensive,
and it would read as a flake.

Everything here fails soft. Writing a marker can never change a gate's exit code
(same contract as receipt emission), and an unreadable/corrupt/absent lock dir
reads as "no gate in flight" — the pre-#201 behavior, which is a race, never a
wedged session.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .util import get_bool_env

#: Where markers live. ``/tmp`` is deliberate: the marker describes a *process*
#: on this machine and must not outlive a reboot, and every consumer (gates,
#: the work CLI, the test suite) runs in the same container as the gate.
#:
#: Rebindable, and the test suite's session fixture does exactly that — the env
#: override below is not enough on its own, because a test that isolates its
#: environment with ``patch.dict(os.environ, …, clear=True)`` drops the variable
#: and lands back here, where a live `gate-check` marker would defer the very
#: commit path it is asserting on. Same reason ``lmer_platform.store.PLATFORM_DIR``
#: is patched rather than pointed at by an env var.
DEFAULT_LOCK_DIR = "/tmp/lmer-gate-inflight"

#: Override for the marker directory. Read at call time, never at import, so a
#: test (or the suite's own isolation fixture) can point it somewhere else after
#: this module is already imported.
LOCK_DIR_ENV = "LMER_GATE_LOCK_DIR"

#: Kill switch for the *consumers* of this module (``get_bool_env`` semantics;
#: unset or truthy enables). Markers are written either way — a switch that
#: stopped recording them would make the next diagnosis harder, and writing one
#: costs nothing.
GUARD_ENV = "LMER_GATE_INFLIGHT_GUARD"

#: Pid-reuse backstop, NOT a gate timeout. Pid liveness is what actually retires
#: a marker; this only stops a recycled pid from resurrecting a marker whose
#: writer died without cleaning up. Six hours is far past any credible gate (the
#: full suite is ~14 minutes) precisely so it can never expire under a running
#: one.
STALE_AFTER_SECONDS = 6 * 60 * 60

#: Marker filename suffix — one file per holding process, named by pid.
MARKER_SUFFIX = ".json"


# ---------------------------------------------------------------------------
# Pure logic — unit-testable seams. No env reads, no filesystem; every input is
# injected by the caller (same shape as hooks/run_state_guard.py).
# ---------------------------------------------------------------------------


def parse_marker(text: str) -> dict | None:
    """
    Normalize one marker file's contents, or None when it is unusable.

    A marker is only meaningful if it names a pid, so anything without a
    positive integer ``pid`` is discarded — as is a torn write (a marker is
    written by one small ``write``, but a reader can still catch a truncated
    file) or a JSON document that is not an object. ``gate`` and ``started_at``
    are best-effort decoration for the human-facing message; a marker missing
    them still counts as live.
    """
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        pid = int(parsed.get("pid"))
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    marker: dict = {"pid": pid}
    gate = parsed.get("gate")
    marker["gate"] = gate if isinstance(gate, str) and gate.strip() else "a gate"
    try:
        marker["started_at"] = float(parsed.get("started_at"))
    except (TypeError, ValueError):
        marker["started_at"] = None
    return marker


def marker_is_live(
    marker: dict,
    now: float,
    pid_alive: bool,
    stale_after: float = STALE_AFTER_SECONDS,
) -> bool:
    """
    Decide whether a parsed *marker* still describes a running gate.

    Both conditions must hold: the writing process is alive, and the marker is
    not older than *stale_after*. The age half is only the pid-reuse backstop
    (see :data:`STALE_AFTER_SECONDS`); a marker with no readable ``started_at``
    is judged on pid liveness alone rather than being thrown away, because the
    pid is the fact that matters.
    """
    if not pid_alive:
        return False
    started = marker.get("started_at")
    if started is None:
        return True
    return (now - started) <= stale_after


def format_age(seconds: float | None) -> str:
    """Render an elapsed time for the deferral message ("3m12s"); "" when unknown."""
    if seconds is None or seconds < 0:
        return ""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def describe_marker(marker: dict | None, now: float | None = None) -> str:
    """
    One-line human description of a live marker, for deferral messages.

    Names the gate, its pid, and how long it has been running — the three facts
    a reader needs to decide whether to wait or to go looking for a stuck
    process.
    """
    if not marker:
        return "a gate"
    gate = marker.get("gate") or "a gate"
    text = f"{gate} (pid {marker.get('pid')})"
    started = marker.get("started_at")
    if started is not None:
        age = format_age((now if now is not None else time.time()) - started)
        if age:
            text += f", running for {age}"
    return text


# ---------------------------------------------------------------------------
# Impure helpers — every one fails open by treating errors as "no gate".
# ---------------------------------------------------------------------------


def lock_dir() -> Path:
    """The marker directory, honoring :data:`LOCK_DIR_ENV` at call time."""
    return Path(os.environ.get(LOCK_DIR_ENV, "").strip() or DEFAULT_LOCK_DIR)


def guard_enabled() -> bool:
    """True unless :data:`GUARD_ENV` is explicitly falsy (default: enabled)."""
    return get_bool_env(GUARD_ENV, default=True)


def _pid_alive(pid: int) -> bool:
    """
    Whether *pid* is a live process, via signal 0.

    ``PermissionError`` means the process exists but belongs to another user —
    still alive, and still a reason to defer. Anything else (no such process, a
    pid the platform rejects) reads as dead.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _marker_path(pid: int, directory: Path | None = None) -> Path:
    return (directory or lock_dir()) / f"{pid}{MARKER_SUFFIX}"


def read_markers(prune: bool = True) -> list[dict]:
    """
    Every live marker, oldest first; dead ones are pruned as a side effect.

    Pruning here rather than in a separate sweeper keeps the lock dir bounded
    without anyone having to remember to clean it: the readers run constantly
    (every work-repo write, every stop), and a marker is only ever removed once
    its writing process is provably gone. An unreadable directory or file is
    skipped — "no gate in flight" is the safe answer, since it restores the
    pre-#201 behavior instead of wedging a session.
    """
    directory = lock_dir()
    try:
        entries = sorted(directory.glob(f"*{MARKER_SUFFIX}"))
    except OSError:
        return []
    now = time.time()
    live: list[dict] = []
    for entry in entries:
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        marker = parse_marker(text)
        if marker is None:
            # Unparseable and therefore unattributable: a torn write from a
            # gate starting right now would parse on the next read, so it is
            # skipped, never pruned.
            continue
        marker["path"] = str(entry)
        if marker_is_live(marker, now, _pid_alive(marker["pid"])):
            live.append(marker)
            continue
        if prune:
            try:
                entry.unlink()
            except OSError:
                pass
    live.sort(key=lambda m: (m.get("started_at") is None, m.get("started_at") or 0))
    return live


def active_gate(exclude_self: bool = True) -> dict | None:
    """
    The longest-running gate currently in flight, or None.

    Returns None when the kill switch is off, so every consumer opts out
    together. *exclude_self* drops a marker written by the calling process — a
    process must never defer on its own gate (``work verify -- <cmd>`` holds a
    marker while its own receipt machinery runs).
    """
    if not guard_enabled():
        return None
    mypid = os.getpid()
    for marker in read_markers():
        if exclude_self and marker.get("pid") == mypid:
            continue
        return marker
    return None


def describe_active_gate() -> str | None:
    """:func:`describe_marker` of :func:`active_gate`, or None when idle."""
    marker = active_gate()
    return describe_marker(marker) if marker else None


@contextmanager
def hold_gate_lock(label: str):
    """
    Hold a marker naming this process as *label* for the duration of the block.

    Written on entry and removed in ``finally`` — including when the gate fails
    or raises, since a marker that outlived its gate would defer every work-repo
    write until its pid died. Every filesystem step is swallowed: no lock
    problem may change a gate's exit code (the receipt contract, applied here
    too), and a marker that could not be written only restores the pre-#201
    race.
    """
    pid = os.getpid()
    directory = lock_dir()
    path = _marker_path(pid, directory)
    written = False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"pid": pid, "gate": label, "started_at": time.time()},
            ensure_ascii=False,
        )
        path.write_text(payload + "\n", encoding="utf-8")
        written = True
    except (OSError, ValueError, TypeError):
        written = False
    try:
        yield
    finally:
        if written:
            try:
                path.unlink()
            except OSError:
                pass
