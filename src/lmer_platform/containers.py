"""Finding the platform's session containers by label (#444).

``lmer`` creates every session container detached and labelled
(:func:`lmer_cli.runtime.container_labels`), so a container outlives the process
that followed it. After a restart this is how the platform finds its sessions
again: by asking the runtime, not by trusting a pid. Only containers labelled
``launcher=platform`` are ever listed here; a plain CLI run's container is never
touched.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Optional

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

from .store import platform_dir

logger = logging.getLogger("lmer_platform.containers")

__all__ = [
    "SessionContainer", "DiscoveryError", "discover", "inspect_one", "logs_since",
    "platform_instance", "remove", "remove_for_session", "stop_container",
    "stop_for_session",
]

#: Seconds a single runtime query may take before the platform gives up on it.
QUERY_TIMEOUT_SECONDS = 60


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


def _query(args: list, *, timeout: float = QUERY_TIMEOUT_SECONDS) -> str:
    try:
        result = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=timeout,
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


def discover() -> list:
    """Every container this platform started: running, exited or never started.
    One that cannot be inspected is left out and logged."""
    runtime = _runtime()
    found = []
    for container_id in _ids(runtime):
        try:
            record = _inspect(runtime, container_id)
        except DiscoveryError as exc:
            logger.warning("platform_container_unreadable id=%s error=%s", container_id, exc)
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


def _remove_now(runtime: str, container_id: str) -> None:
    """``kill`` first: podman's ``rm --force`` would honour the container's stop
    timeout, docker's ``rm`` has no ``--time``. Killing a stopped container
    fails harmlessly."""
    try:
        _query([runtime, "kill", container_id])
    except DiscoveryError:
        pass
    _query([runtime, "rm", "--force", container_id])


def remove(container_id: str) -> None:
    """Remove a container at once. Best-effort: a failure only leaves it listed."""
    try:
        _remove_now(_runtime(), container_id)
    except DiscoveryError as exc:
        logger.warning("platform_container_remove_failed id=%s error=%s", container_id, exc)


def _stop_and_remove(runtime: str, container_id: str) -> None:
    try:
        # Plain stop: the container's own --stop-timeout gives its session-end
        # backstop room before the runtime kills it.
        _query([runtime, "stop", container_id], timeout=STOP_TIMEOUT_SECONDS + 30)
        _query([runtime, "rm", "--force", container_id])
    except DiscoveryError as exc:
        logger.warning("platform_container_stop_failed id=%s error=%s", container_id, exc)


def _in_background(target, *args) -> None:
    threading.Thread(
        target=target, args=args, name="lmer-platform-container-stop", daemon=True
    ).start()


def stop_container(container_id: str) -> None:
    """Stop and remove one container, gracefully and without waiting."""
    try:
        runtime = _runtime()
    except DiscoveryError as exc:
        logger.warning("platform_container_stop_failed id=%s error=%s", container_id, exc)
        return
    _in_background(_stop_and_remove, runtime, container_id)


def _stop_session(session_id: str) -> None:
    try:
        runtime = _runtime()
        container_ids = _ids(runtime, session_id)
    except DiscoveryError as exc:
        logger.warning("platform_container_stop_failed session=%s error=%s", session_id, exc)
        return
    for container_id in container_ids:
        _stop_and_remove(runtime, container_id)


def stop_for_session(session_id: str) -> None:
    """Stop and remove a session's container after its follower was ended.

    In the background and gracefully: the stop runs as long as the container's
    stop timeout allows, which is longer than an exit route should block. A
    failure is logged, and the next daemon start finds the container untracked
    and stops it again.
    """
    _in_background(_stop_session, session_id)


def remove_for_session(session_id: str) -> None:
    """Remove a session's container at once, for a launch the platform is
    throwing away: it never ran a session worth a teardown."""
    try:
        runtime = _runtime()
        for container_id in _ids(runtime, session_id):
            _remove_now(runtime, container_id)
    except DiscoveryError as exc:
        logger.warning(
            "platform_container_cleanup_failed session=%s error=%s", session_id, exc
        )
