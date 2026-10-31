"""Ask every running assistant to rotate itself (issue #646).

The platform asks and the assistant decides when: each one gets a bus record
telling it to hand over and run ``lmer-ctl rotate`` at its next breakpoint, the
same self-rotation it would choose on its own. Nothing here stops a session,
because a clean handover is worth more than a fast one.

The record is posted on the session's bus with ``ref=rotate-request:<id>``, so
the assistant can find it in ``lmer-signal list``, and the request id the text
cites names one platform action across every project it reached.
"""
from __future__ import annotations

from contextlib import nullcontext
import logging
import secrets
from typing import Optional

from lmer_cli import comms as bus_comms

from . import assistant, comms, projects
from . import project_context as scope
from .session_io import SessionIOError
from .store import append_event

logger = logging.getLogger(__name__)

MAX_REASON_CHARS = 500

#: Outcomes per target, in the reply and the event.
REQUESTED = "requested"
NOT_RUNNING = "not_running"
NOT_REACHED = "not_reached"
FAILED = "failed"


class RotateRequestError(ValueError):
    """A request the caller has to change before it can be sent."""


def validated_reason(reason: object) -> Optional[str]:
    """The operator's reason as one line, or ``None`` when none was given."""
    try:
        return bus_comms.one_line_label(reason, MAX_REASON_CHARS, "a rotation reason")
    except bus_comms.CommsError as exc:
        raise RotateRequestError(str(exc)) from None


def request_text(request_id: str, reason: Optional[str]) -> str:
    """What each assistant reads, exactly."""
    because = f" Reason given: {reason}" if reason else " No reason was given."
    return (
        f"Rotation request {request_id}: the operator asks every running assistant to "
        "rotate itself." + because + " This is not urgent and nothing will stop your "
        "session. At your next breakpoint, write a handover for your next generation "
        "that names this request and its reason, then run lmer-ctl rotate with it "
        "(--handoff-file FILE). Your project's settings apply to the new generation."
    )


def targets() -> list[dict]:
    """Every assistant a request would address, running or not."""
    if not projects.enabled():
        status = assistant.status().to_dict()
        return [_target(None, None, status, "running" if status.get("running") else "stopped")]
    found = []
    for project in projects.listing(archived=False):
        status = projects.status(project["id"])
        found.append(_target(project["id"], project["name"], status, status.get("lifecycle")))
    return found


def _target(workspace_id, name, status: dict, lifecycle) -> dict:
    return {
        "workspace_id": workspace_id,
        "project_name": name,
        "session_id": status.get("session_id"),
        "generation": status.get("generation"),
        "lifecycle": lifecycle,
        "running": bool(status.get("running")) and lifecycle == "running",
    }


def request_all(reason: object = None) -> dict:
    """Post one rotation request to every running assistant; never stop one."""
    reason = validated_reason(reason)
    request_id = secrets.token_hex(4)
    text = request_text(request_id, reason)
    hub = comms.ensure_hub()
    results = []
    for target in targets():
        result = {**target, "outcome": NOT_RUNNING, "message_id": None, "detail": None}
        if target["running"] and target["session_id"]:
            result.update(_post(hub, target, request_id, text))
        results.append(result)
    append_event(
        "assistant_rotate_all_requested",
        note=request_id,
        data={
            "request": request_id,
            # Whether a reason was given, not what: it is in each bus record, and
            # the event log is the thing people paste around (wind-down's rule).
            "reasoned": reason is not None,
            "targets": [
                {key: item[key] for key in ("workspace_id", "session_id", "generation", "outcome", "message_id")}
                for item in results
            ],
        },
    )
    return {"request": request_id, "reason": reason, "text": text, "targets": results}


def _post(hub, target: dict, request_id: str, text: str) -> dict:
    try:
        with scope.bound(target["workspace_id"]) if target["workspace_id"] else nullcontext():
            payload = hub.post(
                target["session_id"],
                kind=bus_comms.KIND_MESSAGE,
                text=text,
                channel="platform",
                author="operator",
                ref=f"rotate-request:{request_id}",
            )
    except comms.NoBus:
        return {"outcome": NOT_REACHED, "detail": "this session has no message bus"}
    except (comms.CommsError, SessionIOError) as exc:
        logger.warning(
            "platform_rotate_request_failed session=%s request=%s error=%s",
            target["session_id"], request_id, exc,
        )
        # The full error is in the log; the modal names what failed.
        detail = "its session did not answer" if isinstance(exc, SessionIOError) else str(exc)
        return {"outcome": FAILED, "detail": detail}
    return {"outcome": REQUESTED, "message_id": (payload.get("message") or {}).get("id")}
