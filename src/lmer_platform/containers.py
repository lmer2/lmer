"""Finding the platform's session containers by label (#444).

``lmer`` creates every session container detached and labelled
(:func:`lmer_cli.runtime.container_labels`), so a container outlives the process
that followed it. After a restart this is how the platform finds its sessions
again: by asking the runtime, not by trusting a pid. Only containers labelled
``launcher=platform`` are ever listed here; a plain CLI run's container is never
touched.
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from lmer_cli.container_follow import NOT_STARTED
from lmer_cli.runtime import (
    STOP_TIMEOUT_SECONDS,
    LABEL_LAUNCHER,
    LABEL_MANAGED,
    LABEL_PLATFORM_INSTANCE,
    LABEL_PLATFORM_SESSION,
    RuntimeErrorDetect,
    detect_runtime,
)

from . import registry
from .store import StoreError, platform_dir

logger = logging.getLogger("lmer_platform.containers")

__all__ = [
    "SessionContainer", "DiscoveryError", "discover", "inspect_one", "logs_since",
    "platform_instance", "remove", "remove_for_session", "stop_container",
    "stop_for_session", "sweep", "wait_for_removals",
    "REMOVE_TIMEOUT_SECONDS", "SWEEP_INTERVAL_SECONDS",
]

#: Seconds a single runtime query may take before the platform gives up on it.
QUERY_TIMEOUT_SECONDS = 60

#: Seconds one removal may take, its retry included. Its own bound, far above
#: the query timeout: deleting a large overlay on a saturated rotational disk
#: has taken seven minutes, and podman killed partway leaves the container in
#: ``removing``. The sweep clears what this still cuts short.
REMOVE_TIMEOUT_SECONDS = 30 * 60

#: How often the detector asks for a sweep of half-removed containers.
SWEEP_INTERVAL_SECONDS = 10 * 60


#: The only ``State.Status`` values that mean the container's process has
#: ended. Anything else that is not running — podman's ``stopping``, docker's
#: ``restarting``, ``paused`` or ``removing`` — is still on its way somewhere.
ENDED = ("exited", "dead")


class DiscoveryError(Exception):
    """The runtime could not be asked which containers exist."""


@dataclass(frozen=True)
class SessionContainer:
    id: str
    session_id: str
    #: Not ended — running, or in a state on the way to an exit (podman's
    #: ``stopping``). Only an ended container has an exit code.
    running: bool
    exit_code: Optional[int]
    #: When it stopped (RFC 3339), for an exited container.
    finished_at: Optional[str] = None
    #: ``False`` for one created and never started: its lmer is between create
    #: and start, or died there.
    started: bool = True
    name: Optional[str] = None


def _runtime() -> str:
    try:
        return detect_runtime()
    except RuntimeErrorDetect as exc:
        raise DiscoveryError(str(exc)) from exc


def _query(
    args: list, *, timeout: Optional[float] = None, own_session: bool = False,
) -> str:
    try:
        result = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=QUERY_TIMEOUT_SECONDS if timeout is None else timeout,
            start_new_session=own_session,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DiscoveryError(f"{' '.join(args[:3])}: {exc}") from exc
    if result.returncode != 0:
        raise DiscoveryError(
            f"{' '.join(args[:3])} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def platform_instance() -> str:
    """This platform's ``io.lmer.platform.instance`` label: its user and state
    directory, so two platforms sharing a runtime keep to their own."""
    key = f"{os.getuid()}:{platform_dir().resolve()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _platform_filter(session_id: Optional[str] = None) -> list:
    filters = [
        "--filter", f"label={LABEL_MANAGED}=1",
        "--filter", f"label={LABEL_LAUNCHER}=platform",
        "--filter", f"label={LABEL_PLATFORM_INSTANCE}={platform_instance()}",
    ]
    if session_id:
        filters += ["--filter", f"label={LABEL_PLATFORM_SESSION}={session_id}"]
    return filters


def _ids(runtime: str, session_id: Optional[str] = None) -> list:
    out = _query([runtime, "ps", "-a", "-q", "--no-trunc", *_platform_filter(session_id)])
    return [line.strip() for line in out.splitlines() if line.strip()]


def _ours(labels: dict) -> bool:
    """The filters again, on the record itself: everything downstream of
    discovery may remove the container."""
    return (
        labels.get(LABEL_MANAGED) == "1"
        and labels.get(LABEL_LAUNCHER) == "platform"
        and labels.get(LABEL_PLATFORM_INSTANCE) == platform_instance()
    )


def _parse(record: dict) -> Optional[SessionContainer]:
    # docker spells the id ``Id``, podman ``Id`` or ``ID`` depending on version.
    container_id = record.get("Id") or record.get("ID")
    labels = (record.get("Config") or {}).get("Labels") or {}
    session_id = labels.get(LABEL_PLATFORM_SESSION)
    state = record.get("State") or {}
    if not container_id or not session_id or not isinstance(state, dict):
        return None
    if not _ours(labels):
        return None
    # docker reports the name with a leading slash, podman without.
    name = str(record.get("Name") or "").lstrip("/") or None
    # "Not running, exit code 0" would otherwise read as a clean exit — for a
    # container that never ran, and for one still stopping.
    status = str(state.get("Status", "")).lower()
    if status in NOT_STARTED:
        return SessionContainer(
            id=container_id, session_id=session_id, running=False, exit_code=None,
            started=False, name=name,
        )
    ended = status in ENDED if status else not state.get("Running")
    code = state.get("ExitCode")
    return SessionContainer(
        id=container_id,
        session_id=session_id,
        running=not ended,
        exit_code=code if ended and isinstance(code, int) else None,
        finished_at=state.get("FinishedAt") if ended else None,
        name=name,
    )


def _listed(runtime: str, container_id: str) -> bool:
    out = _query([runtime, "ps", "-a", "-q", "--no-trunc", "--filter", f"id={container_id}"])
    return any(line.strip() == container_id for line in out.splitlines())


def _inspect(runtime: str, container_id: str) -> Optional[dict]:
    """One container's record, or ``None`` once the runtime has shown it is gone
    — a follower removing the container it just saw exit. Raises
    :class:`DiscoveryError` when the runtime cannot say."""
    try:
        records = json.loads(_query([runtime, "inspect", container_id]))
    except DiscoveryError:
        if _listed(runtime, container_id):
            raise
        logger.info("platform_container_vanished id=%s", container_id)
        return None
    except ValueError as exc:
        raise DiscoveryError(f"{runtime} inspect returned no JSON: {exc}") from exc
    if isinstance(records, list) and records and isinstance(records[0], dict):
        return records[0]
    return None


def inspect_one(container_id: str) -> Optional[SessionContainer]:
    """One container as it is now, or ``None`` if it is gone. Raises
    :class:`DiscoveryError` when the runtime cannot say."""
    record = _inspect(_runtime(), container_id)
    return _parse(record) if record is not None else None


#: Containers already warned about as unreadable or unremovable, so a daemon
#: run says so once per container rather than on every sweep.
_WARNED: set = set()
_WARNED_LOCK = threading.Lock()


def _warn_once(container_id: str, message: str, *args) -> None:
    with _WARNED_LOCK:
        if (message, container_id) in _WARNED:
            return
        _WARNED.add((message, container_id))
    logger.warning(message, *args)


def discover() -> list:
    """Every container this platform started: running, exited or never started.
    One that cannot be inspected is left out, logged once, and left to
    :func:`sweep`."""
    runtime = _runtime()
    found = []
    for container_id in _ids(runtime):
        try:
            record = _inspect(runtime, container_id)
        except DiscoveryError as exc:
            _warn_once(
                container_id, "platform_container_unreadable id=%s error=%s",
                container_id, exc,
            )
            continue
        parsed = _parse(record) if record is not None else None
        if parsed is not None:
            found.append(parsed)
    return found


def logs_since(container_id: str, since: str) -> Optional[bytes]:
    """What the container printed since *since*, or ``None`` if the runtime's
    log driver cannot say."""
    try:
        result = subprocess.run(
            [_runtime(), "logs", "--since", since, container_id],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=QUERY_TIMEOUT_SECONDS,
        )
    except (DiscoveryError, OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


class _Remover:
    """The daemon's one removal thread: ``rm`` jobs run one at a time, in
    order, and a key already queued or running is not queued again.

    One at a time because a removal on a slow disk saturates it, and a second
    in parallel slows every other reader rather than finishing sooner. Stops
    and kills do not wait here: they are quick and do not load the disk, and a
    container that has to stop must not wait behind an hour of removals.
    Callers never wait on either.

    A *conditional* job (the sweep's, which re-checks and may do nothing) gives
    way to an unconditional one for the same key: queued, it is replaced;
    running, the unconditional one queues behind it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._order: collections.deque = collections.deque()
        #: key -> (job, args, conditional), for every queued job.
        self._queued: dict = {}
        #: (key, conditional) of the job running now, if any.
        self._running: Optional[tuple] = None
        #: Queued and running jobs plus halts still on their way to queueing one.
        self._busy = 0
        self._idle = threading.Event()
        self._idle.set()
        self._thread: Optional[threading.Thread] = None

    def submit(self, key, job: Callable, *args, conditional: bool = False) -> bool:
        with self._lock:
            queued = self._admit(key, job, args, conditional)
            # Before the dedup answer, not after it: a worker that died with
            # jobs still queued is restarted by the next submit of any key.
            if self._order and (self._thread is None or not self._thread.is_alive()):
                thread = threading.Thread(
                    target=self._run, name="lmer-platform-container-remover", daemon=True,
                )
                try:
                    thread.start()
                except RuntimeError as exc:
                    # The jobs stay queued; the next submit tries again.
                    logger.warning("platform_container_remover_start_failed error=%s", exc)
                else:
                    self._thread = thread
            self._wake.notify()
        return queued

    def _admit(self, key, job: Callable, args: tuple, conditional: bool) -> bool:
        held = self._queued.get(key)
        if held is not None:
            if held[2] and not conditional:
                self._queued[key] = (job, args, conditional)
                return True
            return False
        if self._running is not None and self._running[0] == key:
            if conditional or not self._running[1]:
                return False
        self._queued[key] = (job, args, conditional)
        self._order.append(key)
        self._busy += 1
        self._idle.clear()
        return True

    def pending(self, key) -> bool:
        with self._lock:
            return key in self._queued or (
                self._running is not None and self._running[0] == key
            )

    def halt(self, job: Callable, *args) -> None:
        """Run *job* (a stop or kill that then queues the ``rm``) on its own
        thread, now."""
        with self._lock:
            self._busy += 1
            self._idle.clear()

        def run() -> None:
            try:
                job(*args)
            except Exception:  # noqa: BLE001 - a background stop has nobody to raise to
                logger.exception("platform_container_stop_job_failed")
            finally:
                self._done()

        try:
            threading.Thread(target=run, name="lmer-platform-container-stop", daemon=True).start()
        except RuntimeError as exc:
            self._done()
            logger.warning("platform_container_stop_job_failed error=%s", exc)

    def _done(self) -> None:
        with self._lock:
            self._busy -= 1
            if not self._busy:
                self._idle.set()

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._order:
                    self._wake.wait()
                key = self._order.popleft()
                job, args, conditional = self._queued.pop(key)
                self._running = (key, conditional)
            try:
                job(*args)
            except Exception:  # noqa: BLE001 - one bad job must not end the worker
                logger.exception("platform_container_job_failed key=%s", key)
            finally:
                with self._lock:
                    self._running = None
                self._done()

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        return self._idle.wait(timeout)


_REMOVER = _Remover()


def wait_for_removals(timeout: Optional[float] = None) -> bool:
    """Block until every queued stop and removal has run; ``False`` on
    timeout. For tests: the daemon never waits on its removals, and its
    threads do not hold up its exit."""
    return _REMOVER.wait_idle(timeout)


#: The least time worth giving a second ``rm`` attempt; less, and the first
#: one's failure stands.
_RETRY_FLOOR_SECONDS = 60


def _rm(runtime: str, container_id: str) -> None:
    """``rm --force``, tried twice inside one :data:`REMOVE_TIMEOUT_SECONDS`
    budget, so an attempt that timed out is not followed by a second as long.
    Done once the runtime no longer lists the container, whatever the error
    said about one that was already going.

    In a session of its own: a Ctrl-C or ``systemctl stop`` aimed at the daemon
    must not reach a removal halfway through, which is what leaves a container
    in ``removing``.
    """
    deadline = time.monotonic() + REMOVE_TIMEOUT_SECONDS
    for attempt in range(2):
        try:
            _query(
                [runtime, "rm", "--force", container_id],
                timeout=deadline - time.monotonic(), own_session=True,
            )
            return
        except DiscoveryError:
            try:
                listed = _listed(runtime, container_id)
            except DiscoveryError:
                listed = True
            if not listed:
                return
            if attempt or deadline - time.monotonic() < _RETRY_FLOOR_SECONDS:
                raise


def _kill(runtime: str, container_id: str) -> None:
    """``kill`` before ``rm``: podman's ``rm --force`` would honour the
    container's stop timeout, docker's ``rm`` has no ``--time``. Killing a
    stopped container fails harmlessly."""
    try:
        _query([runtime, "kill", container_id])
    except DiscoveryError:
        pass


def _rm_job(runtime: str, container_id: str) -> None:
    try:
        _rm(runtime, container_id)
    except DiscoveryError as exc:
        logger.warning("platform_container_remove_failed id=%s error=%s", container_id, exc)
        return
    logger.info("platform_container_removed id=%s", container_id)


def _halt_and_queue(runtime: str, container_id: str, stop_first: bool) -> None:
    if stop_first:
        try:
            # Plain stop: the container's own --stop-timeout gives its
            # session-end backstop room before the runtime kills it.
            _query([runtime, "stop", container_id], timeout=STOP_TIMEOUT_SECONDS + 30)
        except DiscoveryError as exc:
            # A container in ``removing`` always refuses a stop; the kill and
            # ``rm --force`` after it are what clear it.
            logger.info("platform_container_stop_failed id=%s error=%s", container_id, exc)
    _kill(runtime, container_id)
    _REMOVER.submit(container_id, _rm_job, runtime, container_id)


def _halt_session(runtime: str, filters: list, session_id: str, stop_first: bool) -> None:
    try:
        out = _query([runtime, "ps", "-a", "-q", "--no-trunc", *filters])
    except DiscoveryError as exc:
        event = "stop_failed" if stop_first else "cleanup_failed"
        logger.warning("platform_container_%s session=%s error=%s", event, session_id, exc)
        return
    for container_id in (line.strip() for line in out.splitlines()):
        if container_id:
            _halt_and_queue(runtime, container_id, stop_first)


def _queue_session(session_id: str, *, stop_first: bool) -> None:
    try:
        runtime = _runtime()
    except DiscoveryError as exc:
        event = "stop_failed" if stop_first else "cleanup_failed"
        logger.warning("platform_container_%s session=%s error=%s", event, session_id, exc)
        return
    # The filter is fixed now: the instance label comes from the state
    # directory, which may differ by the time the thread runs.
    _REMOVER.halt(_halt_session, runtime, _platform_filter(session_id), session_id, stop_first)


def remove(container_id: str) -> None:
    """Kill and remove a container, in the background. A failure is logged and
    left to :func:`sweep`."""
    try:
        runtime = _runtime()
    except DiscoveryError as exc:
        logger.warning("platform_container_remove_failed id=%s error=%s", container_id, exc)
        return
    _REMOVER.halt(_halt_and_queue, runtime, container_id, False)


def stop_container(container_id: str) -> None:
    """Stop and remove one container, gracefully and without waiting."""
    try:
        runtime = _runtime()
    except DiscoveryError as exc:
        logger.warning("platform_container_stop_failed id=%s error=%s", container_id, exc)
        return
    _REMOVER.halt(_halt_and_queue, runtime, container_id, True)


def stop_for_session(session_id: str) -> None:
    """Stop and remove a session's container after its follower was ended.

    In the background and gracefully: the stop runs at once, as long as the
    container's stop timeout allows, which is longer than an exit route should
    block; the removal then waits its turn. A failure is logged, and
    :func:`sweep` or the next daemon start finds the container again.
    """
    _queue_session(session_id, stop_first=True)


def remove_for_session(session_id: str) -> None:
    """Kill a session's container at once and remove it, in the background, for
    a launch the platform is throwing away: it never ran a session worth a
    teardown."""
    _queue_session(session_id, stop_first=False)


# --- the sweep ---------------------------------------------------------------

#: Whether the last sweep failed, so a run of failures warns once.
_SWEEP_FAILING = threading.Event()


def _sweep_failed(exc: Exception) -> None:
    if not _SWEEP_FAILING.is_set():
        _SWEEP_FAILING.set()
        logger.warning("platform_container_sweep_failed error=%s", exc)


def _protected(runtime: str) -> set:
    """Container ids the sweep must leave alone: a live session's (its launcher
    may still be running its own ``rm``, which a second would only queue
    behind), one whose exit is not recorded yet (the container may hold the
    only copy of the code), and one whose registry entry was set aside as
    corrupt (nothing says which of those it is).

    Raises :class:`StoreError` when the registry cannot be read in full: a
    session missing from the read is not a session that ended.
    """
    held = set()
    for entry in registry.list_sessions(live_only=False, strict=True):
        if not entry.get("live") and entry.get("exited"):
            continue
        container_id = entry.get("container_id")
        if isinstance(container_id, str) and container_id:
            held.add(container_id)
            continue
        session_id = entry.get("id")
        if isinstance(session_id, str):
            held.update(_ids(runtime, session_id))
    for session_id in registry.set_aside_ids():
        held.update(_ids(runtime, session_id))
    return held


def _ps(runtime: str, *filters: str, every: bool = True) -> set:
    return set(_query(
        [runtime, "ps", *(["-a"] if every else []), "-q", "--no-trunc", *filters]
    ).split())


def _half_removed(runtime: str, filters: list, container_id: str) -> bool:
    """Listed in ``removing``, or listed but unreadable and not running:
    podman's half-removed container, whose storage is gone."""
    only = ["--filter", f"id={container_id}", *filters]
    try:
        if container_id in _ps(runtime, "--filter", "status=removing", *only):
            return True
    except DiscoveryError:
        pass  # the rule below still finds it without the status filter
    if container_id not in _ps(runtime, *only):
        return False
    if container_id in _ps(runtime, "--filter", "status=running", *only, every=False):
        return False
    try:
        _inspect(runtime, container_id)
    except DiscoveryError:
        return True
    return False


def _sweep_job(runtime: str, filters: list) -> None:
    try:
        listed = _ps(runtime, *filters)
        held = _protected(runtime)
    except (DiscoveryError, StoreError) as exc:
        _sweep_failed(exc)
        return
    _SWEEP_FAILING.clear()
    for container_id in sorted(listed - held):
        if _REMOVER.pending(container_id):
            continue
        try:
            if not _half_removed(runtime, filters, container_id):
                continue
        except DiscoveryError:
            continue
        _REMOVER.submit(
            container_id, _sweep_remove, runtime, filters, container_id, conditional=True,
        )


def _sweep_remove(runtime: str, filters: list, container_id: str) -> None:
    # Again, just before acting: a queue ahead of this job can be long, and a
    # session may have claimed the container or the runtime recovered meanwhile.
    try:
        if container_id in _protected(runtime) or not _half_removed(runtime, filters, container_id):
            return
    except StoreError as exc:
        _sweep_failed(exc)
        return
    except DiscoveryError:
        return
    _kill(runtime, container_id)
    try:
        _rm(runtime, container_id)
    except DiscoveryError as exc:
        _warn_once(
            container_id, "platform_container_sweep_remove_failed id=%s error=%s",
            container_id, exc,
        )
        return
    logger.info("platform_container_swept id=%s", container_id)


def sweep() -> None:
    """Queue the removal of this platform's containers that a removal left
    half-done (:func:`_half_removed`) and no session still needs
    (:func:`_protected`). Returns at once."""
    try:
        runtime = _runtime()
    except DiscoveryError as exc:
        _sweep_failed(exc)
        return
    _REMOVER.submit(("sweep",), _sweep_job, runtime, _platform_filter())
