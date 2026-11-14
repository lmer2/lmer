"""Record the MR notes a review session writes, on its run (#401 F3).

The platform's check-in digest leaves a posted review out while its author has
not moved, and a comment on the MR is the author moving — unless the reviewer
wrote it. The review taskdef writes after ``review_posted`` ("verified in <sha>"
replies), reviewer and author are often the same bot account, and the session's
own clock cannot stand in for "the reviewer's last write" (its last event is
often its teardown, days later). So the tool that writes the note says so: one
``review_note`` event per note, carrying the note id, which the digest excludes
by id (:mod:`lmer_platform.mr_activity`).

Only in a review session (``LMER_TASK=review``, the same test the resolve policy
uses): a fix author's replies are exactly the movement the digest looks for.

**Never fails the write it follows.** A ``work`` CLI that is missing, refuses
(no active run) or hangs is a warning on stderr. The unrecorded note then reads
as its author's, which keeps the run named — the fail-open side.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Optional

__all__ = ["REVIEW_NOTE_EVENT", "RECORD_TIMEOUT_SECONDS", "note_of", "record"]

#: The run-dir event type; its ``data.note_id`` is a GitLab note id.
REVIEW_NOTE_EVENT = "review_note"

#: The ``work`` CLI appends one line; this only bounds a wedged one.
RECORD_TIMEOUT_SECONDS = 30


def note_of(created: Any) -> Optional[dict]:
    """The note in a create answer: the note itself, or a new discussion's first."""
    if not isinstance(created, dict):
        return None
    notes = created.get("notes")
    if isinstance(notes, list):
        created = notes[0] if notes and isinstance(notes[0], dict) else {}
    note_id = created.get("id")
    if isinstance(note_id, bool) or not isinstance(note_id, int):
        return None
    return created


def record(host: str, project: str, mr_id: Any, created: Any) -> None:
    """Record the note *created* on MR *mr_id* as this review run's own write."""
    if os.environ.get("LMER_TASK") != "review":
        return
    note = note_of(created)
    if note is None:
        return
    data = {"mr": f"{host}/{project}!{mr_id}", "note_id": note["id"]}
    if isinstance(note.get("created_at"), str):
        data["created_at"] = note["created_at"]
    argv = ["work", "event", REVIEW_NOTE_EVENT, "--data", json.dumps(data)]
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=RECORD_TIMEOUT_SECONDS,
            check=False,
        )
        problem = None if result.returncode == 0 else (
            (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        )
    except (OSError, subprocess.SubprocessError) as exc:
        problem = f"{type(exc).__name__}: {exc}"
    if problem:
        print(
            f"Warning: note {note['id']} was posted but not recorded on the run "
            f"({problem}). The check-in digest will read it as the author's "
            "comment and keep naming this review until its next round.",
            file=sys.stderr,
        )
