"""claude transcript usage.

claude writes one API response as several records, one per content block, and
every one of them repeats the response's full ``usage``. Summing records counts
a response once per block, so responses are keyed by ``message.id`` (then
``requestId``, then the record ``uuid``) and the last record for a key wins.
Subagents write their own ``<session>/subagents/agent-*.jsonl`` files, and the
sibling ``.meta.json`` names the agent.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Optional

from . import buckets
from .timestamps import parse_timestamp

HARNESS = "claude"
SYNTHETIC_MODEL = "<synthetic>"
_META_LIMIT = 64 * 1024
_AGENT_NAME_LIMIT = 200


def _read_small_json(path: Path) -> Optional[dict]:
    """A small regular JSON file, read descriptor-first: never through a symlink,
    never blocking on a FIFO, never more than :data:`_META_LIMIT` bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _META_LIMIT:
            return None
        data = os.read(fd, _META_LIMIT + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def agent_for_file(path: Path) -> Optional[str]:
    """The subagent a file belongs to, or ``None`` for a main-session file."""
    path = Path(path)
    if path.parent.name != "subagents" or not path.name.startswith("agent-"):
        return None
    meta = _read_small_json(path.with_name(path.stem + ".meta.json")) or {}
    name = meta.get("agentType")
    if isinstance(name, str) and name.strip():
        return name.strip()[:_AGENT_NAME_LIMIT]
    return path.stem[len("agent-"):][:_AGENT_NAME_LIMIT] or "subagent"


class Adapter:
    harness = HARNESS

    def start_file(self, path: Path) -> dict:
        return {"agent": agent_for_file(path)}

    def events(self, record: dict, state: dict) -> list:
        if record.get("type") != "assistant":
            return []
        message = record.get("message")
        if not isinstance(message, dict):
            return []
        model = message.get("model")
        if not isinstance(model, str) or not model or model == SYNTHETIC_MODEL:
            return []
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return []
        details = usage.get("output_tokens_details")
        thinking = details.get("thinking_tokens") if isinstance(details, dict) else 0
        counters = buckets.make(
            input=usage.get("input_tokens"),
            cache_create=usage.get("cache_creation_input_tokens"),
            cache_read=usage.get("cache_read_input_tokens"),
            output=usage.get("output_tokens"),
            thinking=thinking,
        )
        if counters is None or buckets.is_zero(counters):
            return []
        key = next(
            (v for v in (message.get("id"), record.get("requestId"), record.get("uuid"))
             if isinstance(v, str) and v),
            None,
        )
        if key is None:
            return []
        return [buckets.Event(
            key=("claude", key), harness=HARNESS, model=model, agent=state["agent"],
            at=parse_timestamp(record.get("timestamp")), counters=counters,
        )]
