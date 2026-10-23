"""Durable ownership and communications for the existing assistant conversation."""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import os
import sqlite3
import uuid

from . import registry, store, project_context, projects
from .store import StoreError


ROTATION_TEXT = (
    "Assistant rotated. Messages above are kept for your reference. The new assistant "
    "received a handover; it does not have this conversation verbatim in its current context."
)


def _history_dir(session_id=None):
    owner = projects.session_home(session_id) if session_id else None
    with project_context.bound(owner or project_context.current()):
        return project_context.assistant_dir()


@contextmanager
def _database(session_id=None):
    directory = _history_dir(session_id)
    path = directory / "assistant-history.sqlite3"
    try:
        store.ensure_state_dir(directory)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS generations (
                    session TEXT PRIMARY KEY,
                    conversation TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    started_at TEXT,
                    previous_session TEXT,
                    uploads INTEGER NOT NULL,
                    transcript TEXT,
                    partial INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    position INTEGER PRIMARY KEY AUTOINCREMENT,
                    session TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(session, message_id)
                );
                CREATE INDEX IF NOT EXISTS records_session ON records(session, position);
                CREATE TABLE IF NOT EXISTS credentials (
                    session TEXT PRIMARY KEY,
                    fingerprint TEXT UNIQUE
                );
            """)
            yield db
    except (OSError, sqlite3.Error) as exc:
        raise StoreError(f"cannot access assistant history ({exc})") from exc


def generation(session_id: str | None) -> dict | None:
    """Return durable membership, without inventing links from run identities."""
    if not session_id or not (_history_dir(session_id) / "assistant-history.sqlite3").is_file():
        return None
    try:
        registry.session_path(session_id)
    except registry.RegistryError:
        return None
    with _database(session_id) as db:
        row = db.execute("SELECT * FROM generations WHERE session = ?", (session_id,)).fetchone()
    return dict(row) if row else None



def register(session_id: str, *, number: int, started_at: str | None,
             previous: str | None = None, entry: dict | None = None, partial: bool = False) -> dict:
    """Publish a generation and its predecessor in one transaction."""
    registry.session_path(session_id)
    entry = entry or {}
    with _database() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM generations WHERE session = ?", (session_id,)).fetchone()
        if existing:
            return dict(existing)
        parent = db.execute("SELECT * FROM generations WHERE session = ?", (previous,)).fetchone()
        if previous and parent is None:
            raise StoreError("the previous assistant generation is unavailable")
        conversation = parent["conversation"] if parent else uuid.uuid4().hex
        number = max(number, parent["generation"] + 1) if parent else number
        pointer = entry.get("transcript")
        db.execute(
            "INSERT INTO generations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, conversation, number, started_at, previous,
             bool((entry.get("uploads") or {}).get("path")),
             json.dumps(pointer) if isinstance(pointer, dict) else None,
             int(partial or (parent["partial"] if parent else number > 1))),
        )
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('head', ?)", (session_id,))
        if partial:
            db.execute("INSERT INTO metadata VALUES (?, 'pending')", (f"migration:{session_id}",))
    return generation(session_id)


def latest() -> dict | None:
    """Return the last explicitly published generation, including stopped ones."""
    if not (_history_dir() / "assistant-history.sqlite3").is_file():
        return None
    with _database() as db:
        row = db.execute(
            "SELECT generations.* FROM generations JOIN metadata "
            "ON generations.session = metadata.value WHERE metadata.key = 'head'"
        ).fetchone()
    return dict(row) if row else None


def bind_credential(session_id: str, credential: str | None) -> None:
    """Record a generation's immutable identity, including an unattributed launch."""
    fingerprint = hashlib.sha256(credential.encode()).hexdigest() if credential else None
    with _database(session_id) as db:
        db.execute("INSERT OR IGNORE INTO credentials VALUES (?, ?)", (session_id, fingerprint))



def credential_owner(credential: str) -> dict | None:
    """Resolve the credential captured at authentication, even after rotation."""
    if not (_history_dir() / "assistant-history.sqlite3").is_file():
        return None
    fingerprint = hashlib.sha256(credential.encode()).hexdigest()
    with _database() as db:
        row = db.execute(
            "SELECT generations.* FROM generations JOIN credentials USING (session) "
            "WHERE credentials.fingerprint = ?", (fingerprint,),
        ).fetchone()
    return dict(row) if row else None


def remember(state) -> dict | None:
    """Migrate only the incumbent named by lifecycle state, never a guessed run."""
    if not state.session_id:
        return latest()
    known = generation(state.session_id)
    if not known:
        parent = latest()
        known = register(
            state.session_id, number=state.generation, started_at=state.started_at,
            previous=parent["session"] if parent else None,
            entry=registry.read_session(state.session_id), partial=True,
        )
    key = f"migration:{state.session_id}"
    with _database() as db:
        if not db.execute("SELECT 1 FROM metadata WHERE key = ?", (key,)).fetchone():
            return known
    # deferred: cycle — lmer_platform.comms imports this module at top (via projects); only a pending migration marker needs it
    from . import comms

    cursor = 0
    while True:
        records, following = comms.ensure_hub().feed(cursor, session=state.session_id, limit=500)
        for event in records:
            retain(state.session_id, event.message)
        if not records or following <= cursor:
            break
        cursor = following
    with _database() as db:
        db.execute("DELETE FROM metadata WHERE key = ?", (key,))
    return known


def retain(session_id: str, message: dict) -> None:
    """Only already-public communication records enter this archive."""
    # deferred: cycle — lmer_platform.transcripts imports this module at top (via lmer_platform.projects)
    from .transcripts import _scrub_decoded

    registry.session_path(session_id)
    if not message.get("text") and message.get("kind") != "close":
        return
    identifier = message.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise StoreError("assistant communication has no stable message id")
    payload = json.dumps(_scrub_decoded(message), ensure_ascii=False)
    with _database(session_id) as db:
        db.execute(
            "INSERT OR IGNORE INTO records (session, message_id, payload) VALUES (?, ?, ?)",
            (session_id, identifier, payload),
        )



def events(session_id: str) -> list:
    """Read one generation for the optional working-transcript normalizer."""
    # deferred: cycle — lmer_platform.comms and .transcripts import this module at top (via lmer_platform.projects)
    from .comms import FleetEvent
    from .transcripts import MAX_MESSAGES_PER_SOURCE

    if not (_history_dir(session_id) / "assistant-history.sqlite3").is_file():
        return []
    with _database(session_id) as db:
        rows = db.execute(
            "SELECT position, payload FROM records WHERE session = ? ORDER BY position LIMIT ?",
            (session_id, MAX_MESSAGES_PER_SOURCE),
        ).fetchall()
    return [FleetEvent(row["position"], session_id, json.loads(row["payload"])) for row in rows]



def transcript_limit_note(session_id: str) -> str | None:
    """Explain the optional normalizer's ceiling, with a paged recovery route."""
    # deferred: cycle — lmer_platform.transcripts imports this module at top (via lmer_platform.projects)
    from .transcripts import MAX_MESSAGES_PER_SOURCE

    if not (_history_dir(session_id) / "assistant-history.sqlite3").is_file():
        return None
    with _database(session_id) as db:
        more = db.execute(
            "SELECT 1 FROM records WHERE session = ? ORDER BY position LIMIT 1 OFFSET ?",
            (session_id, MAX_MESSAGES_PER_SOURCE),
        ).fetchone()
    if more:
        return ("This working transcript view reached its retained-conversation read limit. "
                "Later public messages remain available in bounded pages at "
                f"/api/assistant/history/{session_id}?view=conversation.")
    return None



def reference(session_id: str | None) -> dict | None:
    """Describe authenticated lookup routes without reading the conversation."""
    item = generation(session_id)
    if item is None:
        return None
    return {
        "session": item["session"], "generation": item["generation"],
        "started_at": item["started_at"], "conversation": item["conversation"],
        "previous_session": item["previous_session"],
        "messages_url": f"/api/sessions/{item['session']}/messages",
        "history_url": f"/api/assistant/history/{item['session']}",
        "partial": bool(item["partial"]),
    }



class HistoryError(ValueError):
    """The requested generation or page anchor is unavailable."""


def _notice(session_id: str, item: dict, kind: str, text: str):
    # deferred: cycle — lmer_platform.transcripts imports this module at top (via lmer_platform.projects)
    from .transcripts import Message

    return Message(role="platform", kind="said", text=text, at=item["started_at"],
                   via="comms", comms_kind=kind, id=f"history:{session_id}:{kind}")



def _conversation_page(session_id: str, prefix: list, *, since: int,
                       before: str | None, limit: int) -> tuple:
    # deferred: cycle — lmer_platform.comms and .transcripts import this module at top (via lmer_platform.projects)
    from .comms import FleetEvent
    from .transcripts import _comms_turns

    offset = len(prefix)
    with _database(session_id) as db:
        count = db.execute("SELECT count(*) FROM records WHERE session = ?", (session_id,)).fetchone()[0]
        total = count + offset
        end = total
        if before is not None:
            boundary = next((i for i, row in enumerate(prefix) if row.id == before), None)
            if boundary is not None:
                end = boundary
            else:
                key = before.removeprefix(f"{session_id}:history:")
                row = db.execute("SELECT position FROM records WHERE session = ? AND message_id = ?",
                                 (session_id, key)).fetchone()
                if row is None:
                    raise HistoryError("That history anchor is no longer available; reload this generation.")
                end = offset + db.execute(
                    "SELECT count(*) FROM records WHERE session = ? AND position < ?",
                    (session_id, row[0]),
                ).fetchone()[0]
            start = max(0, end - limit)
        else:
            start = max(0, total + since) if since < 0 else min(since, total)
        stop = min(start + limit, end)
        selected = prefix[start:stop]
        body_start = max(start, offset)
        if stop > body_start:
            rows = db.execute(
                "SELECT position, payload FROM records WHERE session = ? ORDER BY position LIMIT ? OFFSET ?",
                (session_id, stop - body_start, body_start - offset),
            ).fetchall()
            events = [FleetEvent(row[0], session_id, json.loads(row[1])) for row in rows]
            turns, _copies = _comms_turns(events, session_id)
            for turn in turns:
                turn.id = f"{session_id}:history:{turn.id}"
            selected.extend(turns)
    return selected, start, stop, total



def page(session_id: str, *, since: int = -40, before: str | None = None,
         limit: int = 40, view: str = "conversation") -> dict:
    """Read a bounded page, keeping notices and message ownership explicit."""
    # deferred: cycle — lmer_platform.transcripts imports this module at top (via lmer_platform.projects)
    from . import session_io, transcripts

    item = generation(session_id)
    if item is None:
        raise HistoryError("This assistant generation is not recorded on this host.")
    limit = max(1, min(limit, transcripts.MAX_MESSAGE_LIMIT))
    prefix = []
    if item["previous_session"]:
        prefix.append(_notice(session_id, item, "rotation", ROTATION_TEXT))
    elif item["partial"]:
        prefix.append(_notice(session_id, item, "history unavailable",
                              "Earlier generations were not linked by this version. "
                              "Some earlier conversation records may be unavailable."))
    sources = []
    if view == "conversation":
        selected, start, cursor, total = _conversation_page(
            session_id, prefix, since=since, before=before, limit=limit,
        )
        if not total:
            selected = [_notice(session_id, item, "history unavailable",
                                "No retained conversation messages are available for this generation.")]
            start, cursor, total = 0, 1, 1
    else:
        messages, sources = transcripts.collect_messages(session_id)
        limited = transcript_limit_note(session_id)
        if limited:
            prefix.append(_notice(session_id, item, "conversation incomplete", limited))
        if not sources:
            prefix.append(_notice(session_id, item, "history unavailable",
                                  "This generation’s working transcript is unavailable. "
                                  "Any retained conversation records are shown below."))
        elif len(sources) >= transcripts.MAX_SOURCES or any(source.capped for source in sources):
            prefix.append(_notice(session_id, item, "history incomplete",
                                  "This generation’s transcript reached the read limit. "
                                  "The displayed exchange may be incomplete."))
        elif any(source.incomplete for source in sources):
            prefix.append(_notice(session_id, item, "history incomplete",
                                  "Some transcript records could not be read. "
                                  "The displayed exchange is incomplete."))
        for message in messages:
            message.id = f"{session_id}:history:{message.id}"
        rows = prefix + messages
        total = end = len(rows)
        if before is not None:
            end = next((index for index, row in enumerate(rows) if row.id == before), -1)
            if end < 0:
                raise HistoryError("That history anchor is no longer available; reload this generation.")
            start = max(0, end - limit)
        else:
            start = max(0, end + since) if since < 0 else min(since, end)
        selected = rows[start:min(start + limit, end)]
        cursor = start + len(selected)
    for index, row in enumerate(selected, start):
        row.seq = index
        row.session = session_id
        row.generation = item["generation"]
    return {
        "session": session_id, "history": reference(session_id),
        "live": session_io.session_is_live(session_id),
        "messages": [row.to_dict() for row in selected],
        "from": start, "cursor": cursor, "total": total,
        "sessions": [source.to_dict() for source in sources],
        "note": None if total else "No retained conversation messages are available for this generation.",
    }
