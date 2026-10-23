"""Durable delivery of worker and human events to the current run owner."""
from collections import defaultdict
from dataclasses import replace
import hashlib
import json

from . import assistant, mute, projects, spool_merge, store


def enqueue(session, message, run=None, session_kind=None):
    if not projects.enabled():
        return
    meta = message.get("meta") or {}
    if ((message.get("source") or {}).get("channel") == "uber-lmer" or meta.get("session_origin") is True
            or meta.get("notification_class") == "connector_health" or session_kind == "assistant"):
        return
    if mute.suppressed(session, message, run):
        return
    data = {"session": session, **{key: (run or {}).get(key) for key in ("host", "project", "slug")}}
    # Only a run that has a uid carries one, so a schema-1 payload keeps its shape.
    if (run or {}).get("uid"):
        data["uid"] = run["uid"]
    run_key = projects.key_of(data)
    if run_key:
        data["run_key"] = run_key
    owner = projects.destination(data)
    if not owner:
        return
    identity = hashlib.sha256(json.dumps([session, message.get("id") or message], sort_keys=True).encode()).hexdigest()
    payload = assistant._scrubbed_data({"message": message, "data": data})
    with projects.database() as db:
        db.execute("INSERT OR IGNORE INTO event_queue (id,session,project_id,payload) VALUES (?,?,?,?)",
                   (identity, session, owner, json.dumps(payload)))


def flush():
    with projects.operation(), assistant._LOCK:
        with projects.database() as db:
            queued = [dict(row) for row in db.execute("SELECT * FROM event_queue WHERE done=0 ORDER BY rowid")]
        groups = defaultdict(list)
        for row in queued:
            payload = json.loads(row["payload"])
            owner = projects.destination(payload["data"]) or row["project_id"]
            groups[(owner, row["session"])].append((row["id"], payload))
        with projects.database() as db:
            for (owner, session), records in groups.items():
                row = db.execute("SELECT assistant_state FROM projects WHERE id=?", (owner,)).fetchone()
                state = assistant.AssistantState.from_dict(json.loads(row[0]), memo_spool=owner)
                data = {**records[-1][1]["data"], "workspace_id": owner, "count": len(records)}
                lines = []
                for _identity, payload in records[-8:]:
                    message = payload["message"]
                    who = (message.get("source") or {}).get("channel", "?")
                    lines.append(f"[{who}] {message.get('kind')}: {str(message.get('text') or '')[:300]}")
                routine = spool_merge.routine(payload["message"] for _identity, payload in records)
                at = store.utc_now_iso()
                pending = spool_merge.add_comms(list(state.pending), at=at, session=session,
                                                lines=lines, routine=routine, data=data)
                updated = replace(state, pending=tuple(pending),
                                  pending_since=state.pending_since or at,
                                  pending_seq=state.pending_seq + (not state.pending))
                db.execute("UPDATE projects SET assistant_state=? WHERE id=?", (json.dumps(updated.to_dict()), owner))
                db.executemany("UPDATE event_queue SET done=1 WHERE id=?", [(identity,) for identity, _ in records])
        return len(groups)
