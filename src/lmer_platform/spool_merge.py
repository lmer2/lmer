"""Projects-mode spool merging (#584).

A project's spool has no numeric cap, so the two digest classes that repeat on a
quiet fleet are merged as they arrive: a ``stale_runs`` check-in absorbs the
earlier ones, and a routine ``session_comms`` batch absorbs its session's earlier
routine note. Everything else, questions and milestones above all, is appended
untouched. The
operator-facing rules are in ``docs/PLATFORM-QUICKSTART.md``, "Projects mode:
merged digests".
"""

from __future__ import annotations

from dataclasses import replace

from lmer_cli.comms import KIND_NOTE

from . import checkin
from .assistant import MAX_NOTE_CHARS, PendingNote

__all__ = ["COMMS_KIND", "ROUTINE_KINDS", "add_check_in", "add_comms", "routine"]

COMMS_KIND = "session_comms"

#: The record kinds a merge may absorb. An allow-list: a milestone, a close, a
#: relayed command or answer, a missing kind and any kind added later all keep
#: their batch's note. Connector-health notices never get this far:
#: ``project_events.enqueue`` filters them out before the spool.
ROUTINE_KINDS = frozenset({KIND_NOTE})


def routine(messages) -> bool:
    """Whether a comms batch of *messages* holds only mergeable records."""
    # A container wrote the record, so its kind may not even be hashable.
    return all(isinstance(message.get("kind"), str) and message["kind"] in ROUTINE_KINDS
               for message in messages)


def add_check_in(pending: list, entry: PendingNote) -> list:
    """*pending* with *entry* added, earlier check-ins folded into it if it is one."""
    earlier = [note for note in pending if note.kind == checkin.STALE_DIGEST_KIND]
    if entry.kind != checkin.STALE_DIGEST_KIND or not earlier:
        return [*pending, entry]
    runs = {}
    for note in (entry, *reversed(earlier)):
        for run in (note.data or {}).get("runs") or ():
            if isinstance(run, dict):
                runs.setdefault((run.get("host"), run.get("project"), run.get("slug")), run)
    data = entry.data or {}
    text, merged = checkin.digest(
        [_stale_run(run) for run in runs.values()],
        window=data.get("window_seconds") or 0, caveat=data.get("caveat"),
    )
    merged.update({key: value for key, value in data.items() if key not in merged})
    merged["check_ins"] = 1 + sum((note.data or {}).get("check_ins", 1) for note in earlier)
    kept = [note for note in pending if note.kind != checkin.STALE_DIGEST_KIND]
    return [*kept, replace(entry, note=text, data=merged)]


def add_comms(pending: list, *, at: str, session: str, lines: list, routine: bool,
              data: dict) -> list:
    """*pending* with one session's comms batch added.

    A :func:`routine` batch absorbs that session's earlier routine note; any
    other batch is never merged either way.
    """
    earlier = [] if not routine else [
        note for note in pending
        if note.kind == COMMS_KIND and (note.data or {}).get("session") == session
        and (note.data or {}).get("routine") is True
    ]
    count = int(data.get("count") or 0) + sum(int(note.data.get("count") or 0) for note in earlier)
    shown = f" ({count} records, latest {len(lines)} shown)" if earlier else ""
    entry = PendingNote(
        at=at, kind=COMMS_KIND,
        note=(f"Session {session}{shown}: " + " | ".join(lines))[:MAX_NOTE_CHARS],
        data={**data, "count": count, "routine": routine},
    )
    return [*(note for note in pending if note not in earlier), entry]


def _stale_run(run: dict) -> checkin.StaleRun:
    return checkin.StaleRun(
        host=str(run.get("host") or ""), project=str(run.get("project") or ""),
        slug=str(run.get("slug") or ""), label=str(run.get("label") or ""),
        state=str(run.get("state") or ""), since=run.get("since"),
        age_seconds=run.get("age_seconds"),
    )
