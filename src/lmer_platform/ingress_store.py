"""Transport inbox committed before remote acknowledgement. See docs/PROJECTS.md."""
from contextlib import closing, contextmanager
import hashlib
import json
import os
import sqlite3

from . import store


@contextmanager
def database():
    path = store.platform_dir() / "ingress.sqlite3"
    store.ensure_state_dir(path.parent)
    os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE IF NOT EXISTS inbox (
                id TEXT PRIMARY KEY, transport TEXT NOT NULL, conversation TEXT NOT NULL,
                event TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS cursors (account TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        db.execute("BEGIN IMMEDIATE")
        yield db


def _insert(db, transport, conversation, event):
    event_id = event.get("event_id") if transport == "matrix" else event.get("ts")
    identity = hashlib.sha256(json.dumps([transport, conversation, event_id or event], sort_keys=True).encode()).hexdigest()
    db.execute("INSERT OR IGNORE INTO inbox (id,transport,conversation,event) VALUES (?,?,?,?)",
               (identity, transport, conversation, json.dumps(event)))
    return identity


def receive(transport, conversation, event):
    with database() as db:
        return _insert(db, transport, conversation, event)


def slack_event(request):
    event = (request.payload or {}).get("event")
    if request.type != "events_api" or not isinstance(event, dict):
        return False
    if event.get("type") != "app_mention" and not (event.get("type") == "message" and event.get("channel_type") == "im"):
        return False
    receive("slack", event.get("channel"), event)
    return True


def pending(transport):
    with database() as db:
        return [{**dict(row), "event": json.loads(row["event"])} for row in db.execute(
            "SELECT * FROM inbox WHERE transport=? AND event IS NOT NULL ORDER BY rowid", (transport,))]


def finish(identity):
    with database() as db:
        db.execute("UPDATE inbox SET event=NULL,error=NULL WHERE id=?", (identity,))


def failed(identity, reason):
    with database() as db:
        db.execute("UPDATE inbox SET error=? WHERE id=?", (reason, identity))


def cursor(account):
    with database() as db:
        row = db.execute("SELECT value FROM cursors WHERE account=?", (account,)).fetchone()
        return json.loads(row[0]) if row else {"since": None, "rooms": []}


def matrix_batch(account, since, rooms, events):
    with database() as db:
        for conversation, event in events:
            _insert(db, "matrix", conversation, event)
        db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?)",
                   (account, json.dumps({"since": since, "rooms": sorted(rooms)})))
