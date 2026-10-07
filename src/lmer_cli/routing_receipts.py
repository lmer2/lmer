"""Producer-owned delivery evidence for a harness session and tool invocation."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import shlex
import sys
import tempfile

KEY = "lmer_routing_receipt"
CONTEXT_ENV = "LMER_ROUTING_CALL"
KINDS = frozenset({"post", "ask", "dm"})
logger = logging.getLogger(__name__)


def receipt(kind: str, identifier: str) -> dict:
    if kind in KINDS and isinstance(identifier, str) and identifier:
        return {KEY: {"version": 1, "kind": kind, "id": identifier}}
    return {}


def _path(session: str, tool: str) -> Path:
    namespace = json.dumps([os.environ.get("LMER_SESSION_ID", ""), session, tool])
    key = hashlib.sha256(namespace.encode()).hexdigest()
    return Path(tempfile.gettempdir()) / "lmer-routing" / f"{key}.json"


def record(kind: str, identifier: str) -> None:
    value = receipt(kind, identifier)
    if not value:
        return
    try:
        context = json.loads(os.environ.get(CONTEXT_ENV, "null"))
        if not isinstance(context, dict):
            return
        session, tool = context.get("session"), context.get("tool")
        if not all(isinstance(item, str) and item for item in (session, tool)):
            return
        path = _path(session, tool)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        value.update(session=session, tool=tool)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                json.dump(value, handle)
                handle.flush()
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError) as exc:
        # Delivery already succeeded; a local write failure must not invite a resend.
        logger.warning("routing_evidence_not_saved error=%s", exc)


def emit(kind: str, identifier: str) -> None:
    record(kind, identifier)
    value = receipt(kind, identifier)
    if value:
        print(json.dumps(value), file=sys.stderr)


def delivered(tool: str, session: str | None = None) -> bool:
    if not all(isinstance(item, str) and item for item in (session, tool)):
        return False
    try:
        value = json.loads(_path(session, tool).read_text())
        if not isinstance(value, dict) or value.get("session") != session or value.get("tool") != tool:
            return False
        evidence = value.get(KEY)
        return (
            isinstance(evidence, dict)
            and type(evidence.get("version")) is int and evidence["version"] == 1
            and isinstance(evidence.get("kind"), str) and evidence["kind"] in KINDS
            and isinstance(evidence.get("id"), str) and bool(evidence["id"])
        )
    except (OSError, ValueError, TypeError):
        return False


def prepare_bash(payload: dict) -> dict | None:
    """Give each Bash child its own identity, including parallel/background calls."""
    inputs = payload.get("tool_input")
    session, tool = payload.get("session_id"), payload.get("tool_use_id")
    if (payload.get("tool_name") != "Bash" or not isinstance(inputs, dict)
            or not isinstance(inputs.get("command"), str)
            or not all(isinstance(item, str) and item for item in (session, tool))):
        return None
    context = shlex.quote(json.dumps({"session": session, "tool": tool}))
    command = f"export {CONTEXT_ENV}={context}\n" + inputs["command"]
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "updatedInput": {**inputs, "command": command},
    }}
