"""Track acknowledged shutdowns whose launcher outlives the control plane.

The supervisor's structured bus record starts eligibility; a wind-down prompt
alone does not. Detection reports the condition and never signals a process.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from lmer_cli import comms

from . import registry, session_io
from .store import StoreError

logger = logging.getLogger(__name__)


def _time(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else None


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def record_request(session_id: str, message: dict) -> bool:
    """Bind an acknowledgment to its authenticated subscriber session.

    The message's session is the container's work identity, not the registry id.
    """
    source = message.get("source") or {}
    meta = message.get("meta") or {}
    if (message.get("kind") != comms.KIND_NOTE
            or not isinstance(source, dict)
            or source.get("channel") != comms.CHANNEL_SUPERVISOR
            or not isinstance(meta, dict)
            or meta.get("event") != comms.EVENT_SESSION_END_REQUESTED
            or _time(message.get("at")) is None):
        return False
    # Its own field: replaying this record must not overwrite a detector's
    # concurrently recorded outage or reset its timer.
    return bool(registry.update(session_id, teardown_requested_at=message["at"]))


def _reachable(session_id: str) -> Optional[bool]:
    try:
        endpoint = session_io.control_endpoint(session_id)
    except (session_io.SessionNotFound, session_io.ControlUnavailable):
        return None
    try:
        session_io._get(endpoint, "/healthz", timeout=session_io.ACTIVITY_TIMEOUT_SECONDS)
    except session_io.ControlPlaneError:
        return False
    # Even an HTTP error proves the server answered. Authentication errors and
    # missing local credentials are not evidence of a refused connection.
    return True


def sweep(grace_seconds: int, *, now: Optional[datetime] = None) -> list[str]:
    """Sample ending sessions; record newly stuck ones for the fleet and digest."""
    now = now or datetime.now(timezone.utc)
    newly_stuck = []
    for entry in registry.list_sessions(live_only=True):
        if _time(entry.get("teardown_requested_at")) is None:
            continue
        session_id = entry["id"]
        previous = entry.get("teardown")
        previous = previous if isinstance(previous, dict) else {}
        try:
            reachable = _reachable(session_id)
            current = {}
            if reachable is False:
                since = _time(previous.get("unreachable_since")) or now
                if since > now:
                    since = now
                current["unreachable_since"] = _stamp(since)
                if (now - since).total_seconds() >= grace_seconds:
                    current["stuck_at"] = previous.get("stuck_at") or _stamp(now)
                    current["grace_seconds"] = grace_seconds
            # A successful response clears the interval. An unavailable probe
            # cannot establish continuous failure either, so it clears it too.
            if current != previous:
                updated = registry.update(session_id, teardown=current)
                if updated and current.get("stuck_at") and not previous.get("stuck_at"):
                    newly_stuck.append(session_id)
                    logger.warning("platform_stuck_teardown session=%s grace_seconds=%s",
                                   session_id, grace_seconds)
        except StoreError as exc:
            logger.warning("platform_teardown_record_failed session=%s error=%s", session_id, exc)
    return newly_stuck
