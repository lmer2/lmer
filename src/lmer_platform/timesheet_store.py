"""The time tracker's local state: one SQLite file, ``timesheet.sqlite3``.

Its first table is the timesheet writer's **outbox** (spec D14): a finished
entry is committed here before anything touches git, and its row leaves only
once the push that carries it has landed. ``seq`` is never reused, so
"oldest first" is the order entries were finished in. The tracker's own tables
(:mod:`lmer_platform.timetrack`) share the file, so a stop can finish its
timer and queue its entry in one transaction. See docs/TIME-TRACKING.md.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from typing import Optional

from lmer_cli.container.clone_and_exec import _scrub_credentials
from lmer_timesheet import Entry, entry_path

# timesheet_writer imports this module back: import modules, not names, so
# either one can be imported first.
from . import store, timesheet_writer

__all__ = [
    "DB_FILE", "STATE_QUEUED", "STATE_FAILED", "OutboxRow", "database",
    "enqueue", "enqueue_in", "notify_writer", "pending", "mark_delivered",
    "mark_failed", "outbox_status",
]

DB_FILE = "timesheet.sqlite3"
#: Waiting for its first delivery attempt.
STATE_QUEUED = "queued"
#: An attempt failed; the row is still queued and ``last_error`` says why.
STATE_FAILED = "failed"

#: Every table this store owns, each ``CREATE ... IF NOT EXISTS``, run on
#: every open: a table added here exists the next time any caller connects.
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS outbox (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id TEXT NOT NULL UNIQUE,
        entry_yaml TEXT NOT NULL,
        path TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        enqueued_at TEXT NOT NULL,
        last_attempt_at TEXT
    )""",
    # The tracker's tables (lmer_platform.timetrack). Timestamps are
    # fixed-width UTC ISO strings, so they compare and sort as text.
    """CREATE TABLE IF NOT EXISTS timers (
        person TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        project_key TEXT NOT NULL,
        start TEXT NOT NULL,
        last_activity TEXT NOT NULL,
        mode TEXT NOT NULL,
        start_trigger TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS pauses (
        person TEXT PRIMARY KEY,
        until TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS auto_off (
        person TEXT PRIMARY KEY,
        since TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS recents (
        person TEXT NOT NULL,
        project_key TEXT NOT NULL,
        kind TEXT NOT NULL,
        value TEXT NOT NULL,
        last_used TEXT NOT NULL,
        PRIMARY KEY (person, project_key, kind, value)
    )""",
    """CREATE TABLE IF NOT EXISTS kept (
        id TEXT PRIMARY KEY,
        person TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        project_key TEXT NOT NULL,
        handler TEXT NOT NULL,
        fields TEXT NOT NULL DEFAULT '{}',
        client TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        start TEXT NOT NULL,
        end TEXT NOT NULL,
        duration INTEGER NOT NULL,
        trigger_start TEXT NOT NULL,
        trigger_stop TEXT NOT NULL,
        reason TEXT NOT NULL,
        at TEXT NOT NULL,
        sessions TEXT NOT NULL DEFAULT '[]'
    )""",
    """CREATE TABLE IF NOT EXISTS prompts (
        person TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        project_key TEXT NOT NULL,
        missing TEXT NOT NULL,
        at TEXT NOT NULL,
        PRIMARY KEY (person, workspace_id)
    )""",
    # Issue #665: field values and prompts per work key, an entry's first
    # issue or else its run's key; '' is a project's run-less prompt.
    """CREATE TABLE IF NOT EXISTS key_values (
        person TEXT NOT NULL,
        handler TEXT NOT NULL,
        work_key TEXT NOT NULL,
        field TEXT NOT NULL,
        value TEXT NOT NULL,
        last_used TEXT NOT NULL,
        PRIMARY KEY (person, handler, work_key, field)
    )""",
    """CREATE TABLE IF NOT EXISTS key_prompts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        person TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        project_key TEXT NOT NULL,
        work_key TEXT NOT NULL DEFAULT '',
        run TEXT NOT NULL DEFAULT '',
        issues TEXT NOT NULL DEFAULT '[]',
        missing TEXT NOT NULL,
        at TEXT NOT NULL,
        UNIQUE (person, workspace_id, work_key)
    )""",
)

#: ``(table, column, declaration)`` added since a table first shipped, for a
#: file whose ``CREATE`` ran before the column existed.
_ADDED_COLUMNS = (
    ("kept", "sessions", "TEXT NOT NULL DEFAULT '[]'"),
    ("kept", "identity", "TEXT NOT NULL DEFAULT ''"),
    ("kept", "issues", "TEXT NOT NULL DEFAULT '[]'"),
)


def _add_columns(db) -> None:
    """Add any of :data:`_ADDED_COLUMNS` the file lacks.

    Run under ``BEGIN IMMEDIATE``, so two connections opening an old file at
    once cannot both add the same column.
    """
    for table, column, declaration in _ADDED_COLUMNS:
        present = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _migrate_prompts(db) -> None:
    """Move any ``prompts`` row (one per project, before #665) into
    ``key_prompts`` as that project's run-less prompt.

    ``prompts`` itself stays, so an older daemon can still open the file.
    """
    db.execute(
        "INSERT OR IGNORE INTO key_prompts (person, workspace_id, project_key, missing, at)"
        " SELECT person, workspace_id, project_key, missing, at FROM prompts"
    )
    db.execute("DELETE FROM prompts")


@dataclass(frozen=True)
class OutboxRow:
    """One undelivered entry. ``path`` is relative to the work-repo root."""

    seq: int
    entry_id: str
    entry_yaml: str
    path: str
    state: str
    attempts: int
    last_error: Optional[str]
    enqueued_at: str
    last_attempt_at: Optional[str]


@contextmanager
def database():
    path = store.platform_dir() / DB_FILE
    store.ensure_state_dir(path.parent)
    os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        db.row_factory = sqlite3.Row
        db.executescript(";\n".join(_SCHEMA) + ";")
        db.execute("BEGIN IMMEDIATE")
        _add_columns(db)
        _migrate_prompts(db)
        yield db


def enqueue(entry: Entry) -> int:
    """Queue *entry* for the writer and return its ``seq``.

    Queuing an entry id that is still queued with the same content returns the
    existing row, so a caller retrying after a crash cannot write it twice.
    Raises ``ValueError`` when the entry cannot be placed (see
    :func:`lmer_timesheet.entry_path`) or its id is queued with other content.
    """
    with database() as db:
        seq = enqueue_in(db, entry)
    notify_writer()
    return seq


def enqueue_in(db: sqlite3.Connection, entry: Entry) -> int:
    """:func:`enqueue` inside the caller's open :func:`database` transaction.

    The caller calls :func:`notify_writer` once that transaction has
    committed; a writer woken earlier would not see the row.
    """
    path = str(entry_path(entry))
    text = entry.to_yaml()
    row = db.execute(
        "SELECT seq, entry_yaml FROM outbox WHERE entry_id=?", (entry.id,)
    ).fetchone()
    if row is not None:
        if row["entry_yaml"] != text:
            raise ValueError(f"entry {entry.id} is already queued with other content")
        return row["seq"]
    return db.execute(
        "INSERT INTO outbox (entry_id, entry_yaml, path, state, enqueued_at)"
        " VALUES (?,?,?,?,?)",
        (entry.id, text, path, STATE_QUEUED, store.utc_now_iso()),
    ).lastrowid


def notify_writer() -> None:
    """Wake the registered writer, if there is one, to drain the outbox now."""
    writer = timesheet_writer.get_writer()
    if writer is not None:
        writer.notify()


def pending() -> list[OutboxRow]:
    """Every undelivered row, oldest first."""
    with database() as db:
        return [OutboxRow(**dict(row)) for row in db.execute("SELECT * FROM outbox ORDER BY seq")]


def mark_delivered(seq: int) -> None:
    with database() as db:
        db.execute("DELETE FROM outbox WHERE seq=?", (seq,))


def mark_failed(seq: int, reason: str) -> None:
    """Record a failed attempt on *seq*; the row stays queued."""
    with database() as db:
        db.execute(
            "UPDATE outbox SET state=?, attempts=attempts+1, last_error=?, last_attempt_at=?"
            " WHERE seq=?",
            (STATE_FAILED, _scrub_credentials(reason), store.utc_now_iso(), seq),
        )


def outbox_status() -> dict:
    """Queue depth and the oldest row's failure, for the time status endpoint.

    The oldest row is the one every later row waits behind, so its error is
    the one that explains the queue.
    """
    with database() as db:
        depth = db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        failed = db.execute(
            "SELECT COUNT(*) FROM outbox WHERE state=?", (STATE_FAILED,)
        ).fetchone()[0]
        head = db.execute(
            "SELECT seq, attempts, last_error, last_attempt_at, enqueued_at"
            " FROM outbox ORDER BY seq LIMIT 1"
        ).fetchone()
    return {
        "depth": depth,
        "failed": failed,
        "oldest_enqueued_at": head["enqueued_at"] if head else None,
        "attempts": head["attempts"] if head else 0,
        "last_error": head["last_error"] if head else None,
        "last_attempt_at": head["last_attempt_at"] if head else None,
    }
