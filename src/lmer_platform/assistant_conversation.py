"""Platform-only conversation records; never sent to session connectors."""
from __future__ import annotations

import threading
import uuid
import logging

from . import comms
from .store import utc_now_iso
from .project_context import assistant_dir

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_JOURNAL: comms.Journal | None = None
DIGEST_KINDS = frozenset({"question", "live_question", "crashed", "complete"})


def _journal() -> comms.Journal:
    global _JOURNAL
    path = (assistant_dir() / "comms" / "assistant-conversation.jsonl").resolve()
    with _LOCK:
        if _JOURNAL is None or _JOURNAL.path != path:
            _JOURNAL = comms.Journal(path)
        return _JOURNAL


#: The run fields a digest's payload may carry, kept so the conversation can
#: name and open the run without parsing the digest's prose.
RUN_FIELDS = ("host", "project", "slug", "label")
MAX_RUN_FIELD_CHARS = 200


def digest_run(data: object) -> dict | None:
    """``host/project/slug`` (+ ``label``) off a digest payload, or ``None``.

    Only the label is shortened: a cut identity field would name a run that
    does not exist, so an over-long one drops the link instead.
    """
    if not isinstance(data, dict):
        return None
    run = {
        field: data[field] for field in RUN_FIELDS
        if isinstance(data.get(field), str) and data[field]
    }
    if not all(field in run for field in RUN_FIELDS[:3]):
        return None
    if any(len(run[field]) > MAX_RUN_FIELD_CHARS for field in RUN_FIELDS[:3]):
        return None
    if "label" in run:
        run["label"] = run["label"][:MAX_RUN_FIELD_CHARS]
    return run


def record(text: str, *, kind: str, thread: str | None = None,
           session_id: str | None = None, data: dict | None = None) -> None:
    from .transcripts import scrub_credentials
    from . import assistant, assistant_history

    with _LOCK:
        if session_id is None:
            state = assistant.read_state()
            latest = assistant_history.latest()
            session_id = state.session_id or (latest["session"] if latest else None)
        message = {
            "id": f"assistant:{uuid.uuid4().hex}",
            "at": utc_now_iso(),
            "kind": "message" if thread else "digest",
            "text": scrub_credentials(text),
            "source": {"channel": "agent" if thread else "platform"},
            "meta": {"thread": thread, "digest_kind": kind},
        }
        run = None if thread else digest_run(data)
        if run:
            message["meta"]["run"] = run
        if session_id:
            assistant_history.retain(session_id, message)
        _journal().append(session_id or "assistant", message, session_kind="assistant")


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
