"""Which harness wrote one transcript record, decided by the record's shape.

The transcript view (``lmer_platform.transcripts``) and the usage adapters share
this one rule, so a record the view labels pi is the record the usage counter
reads as pi. Dispatch is per record rather than per file, because a fallback
source cannot vouch for what wrote a file.
"""

from __future__ import annotations

from typing import Optional

#: Record ``type`` values each harness writes. Disjoint, which is what makes the
#: dispatch per record rather than per file. Each set names the harness's
#: *bookkeeping* types as well as its conversation ones, so a session that has so
#: far written only its header is still labelled by what wrote it, and a type
#: this build has never seen stays unrecognised instead of being handed to
#: whichever adapter guessed first.
#: claude's ``attachment`` is a turn when it delivers a queued command;
#: ``queue-operation`` never is, but it is still claude's bookkeeping.
CLAUDE_RECORD_TYPES = frozenset({
    "user", "assistant", "system", "attachment", "queue-operation",
})
#: pi's session-manager vocabulary. ``usage``, ``compaction`` and
#: ``branch_summary`` carry model spend of their own.
PI_RECORD_TYPES = frozenset({
    "session", "model_change", "thinking_level_change", "message",
    "usage", "compaction", "branch_summary", "custom", "custom_message",
    "label", "session_info", "context_edit",
})
CODEX_RECORD_TYPES = frozenset({
    "session_meta", "response_item", "event_msg", "turn_context", "world_state",
})

#: The canonical "lmer transcript format" (#296) is claimed by its namespace
#: rather than by its known types, so a record from a later version still
#: belongs to this format.
LMER_TYPE_PREFIX = "lmer."
LMER_RECORD_TYPES = frozenset({"lmer.meta", "lmer.message", "lmer.tool_update"})
LMER_HARNESS = "lmer"


def harness_of_record(record: dict) -> Optional[str]:
    """Which harness's format one record is in, or ``None`` for a shape nobody
    here recognises: a fourth harness, or a file that is not a transcript."""
    kind = record.get("type")
    if kind in CLAUDE_RECORD_TYPES:
        return "claude"
    if kind in PI_RECORD_TYPES:
        return "pi"
    if kind in CODEX_RECORD_TYPES:
        return "codex"
    if isinstance(kind, str) and kind.startswith(LMER_TYPE_PREFIX):
        return LMER_HARNESS
    return None
