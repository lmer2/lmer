"""Platform-only conversation records; never sent to session connectors."""
from __future__ import annotations

import threading
import uuid
import logging

from . import comms
from .store import utc_now_iso

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_JOURNAL: comms.Journal | None = None
DIGEST_KINDS = frozenset({"question", "live_question", "crashed", "complete"})


def _journal() -> comms.Journal:
    global _JOURNAL
    path = (comms.comms_dir() / "assistant-conversation.jsonl").resolve()
    with _LOCK:
        if _JOURNAL is None or _JOURNAL.path != path:
            _JOURNAL = comms.Journal(path)
        return _JOURNAL


def record(text: str, *, kind: str, thread: str | None = None) -> None:
    from .transcripts import scrub_credentials

    with _LOCK:
        _journal().append("assistant", {
            "id": f"assistant:{uuid.uuid4().hex}",
            "at": utc_now_iso(),
            "kind": "message" if thread else "digest",
            "text": scrub_credentials(text),
            "source": {"channel": "agent" if thread else "platform"},
            "meta": {"thread": thread, "digest_kind": kind},
        }, session_kind="assistant")


def events() -> list:
    with _LOCK:
        journal = _journal()
        return journal.read_since(max(0, journal.cursor - comms.PANE_LIMIT), limit=comms.PANE_LIMIT)[0]


def private_thread(ref, client) -> bool:
    from lmer_cli import dm_parents

    conversation, _root = ref.registry_key()
    if ref.channel == "slack":
        return conversation.startswith("D")
    try:
        return dm_parents.known_conversation(ref.channel, conversation) or client.is_direct_room(conversation)
    except Exception as exc:
        logger.warning("assistant_reply_privacy_unknown error=%s", exc)
        return True
