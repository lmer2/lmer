"""Host DM-room facts and assistant-owned parent receipts, without message text."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .runtime import lmer_state_dir
from .threads import parse_thread

REGISTRY_DIR = None


def _directory() -> Path:
    return Path(REGISTRY_DIR) if REGISTRY_DIR is not None else lmer_state_dir() / "assistant-dms"


def _path(transport: str, conversation: str, root: str) -> Path:
    identity = json.dumps([transport, conversation, root], separators=(",", ":"))
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return _directory() / f"{digest}.json"


def remember_conversation(transport: str, conversation: str) -> None:
    """Record DM identity independently of permission to reply to the assistant."""
    _write(_path(transport, conversation, ""), {
        "kind": "conversation", "transport": transport, "conversation": conversation,
    })


def known_conversation(transport: str, conversation: str) -> bool:
    """Recognize a DM even when the transport's DM-directory update was lost."""
    try:
        paths = list(_directory().iterdir())
    except FileNotFoundError:
        return False
    for path in paths:
        if path.suffix != ".json":
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(payload, dict) and (payload.get("kind") == "conversation" or payload.get("session"))
                and payload.get("transport") == transport and payload.get("conversation") == conversation):
            return True
    return False


def remember(result: dict, session: str) -> None:
    """Persist the exact successfully sent DM root for an assistant session."""
    ref = parse_thread(result["conversation"]["permalink"])
    conversation, root = ref.registry_key()
    payload = {"transport": ref.channel, "conversation": conversation, "root": root,
               "session": session, "recipient": result["conversation"]["recipient"]}
    _write(_path(ref.channel, conversation, root), payload)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".dm-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def lookup(transport: str, conversation: str, root: str) -> dict | None:
    """Read a parent receipt; an unavailable receipt grants no routing rights."""
    try:
        payload = json.loads(_path(transport, conversation, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload.get("session"):
        return None
    if (payload.get("transport"), payload.get("conversation"), payload.get("root")) != (
        transport, conversation, root,
    ):
        return None
    return payload
