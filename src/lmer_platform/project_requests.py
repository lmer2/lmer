"""Durable, single-destination Slack and Matrix requests and reply receipts."""
from dataclasses import replace
from contextvars import ContextVar
import hashlib
import json
import re

from lmer_cli import thread_access
from lmer_cli.threads import ThreadError
from . import assistant, channel_routes, project_context as scope, project_threads, projects, store
from .transcripts import scrub_credentials
from . import assistant_conversation

CHOICE = re.compile(r"\bproject\s+([a-f0-9]{64})\s+([a-z0-9][a-z0-9-]{0,63})\s*$")
_ACTION = ContextVar("request_action", default=None)
OWNERSHIP_CHANGED = (
    "The request's thread ownership changed. Take pending to release it safely, "
    "or use the operator's Release blocked request control. Keep completed action receipts; "
    "ask for a new request in the source thread instead of repeating completed work."
)


def preflight():
    action = _ACTION.get()
    if action is not None and action["effect"] is None:
        with projects.database() as db:
            db.execute("UPDATE request_actions SET state='prepared' WHERE id=?", (action["id"],))
        action["effect"] = False


def effect():
    from . import project_api

    action = _ACTION.get()
    if action is not None:
        project_api.recheck()
        with projects.database() as db:
            db.execute("UPDATE request_actions SET state='sending' WHERE id=?", (action["id"],))
        action["effect"] = True


def refused_before_effect():
    action = _ACTION.get()
    if action is not None:
        with projects.database() as db:
            db.execute("UPDATE request_actions SET state='refused' WHERE id=?", (action["id"],))
        action["effect"] = False


def active(workspace_id):
    with projects.database() as db:
        row = db.execute("SELECT request_id FROM request_context WHERE project_id=?", (workspace_id,)).fetchone()
        if not row:
            row = db.execute("SELECT id FROM requests WHERE project_id=? AND state IN "
                             "('queued','delivered','reply_sending','reply_uncertain') "
                             "AND id NOT IN (SELECT request_id FROM request_parking) ORDER BY rowid LIMIT 1",
                             (workspace_id,)).fetchone()
            if row:
                db.execute("INSERT INTO request_context VALUES (?,?)", (workspace_id, row[0]))
        return row[0] if row else None


def _decode(row):
    if row is None:
        return None
    result = dict(row)
    for field in ("payload", "ownership"):
        result[field] = json.loads(result[field])
    return result


def get(identity):
    with projects.database() as db:
        row = db.execute("SELECT * FROM requests WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise projects.ProjectError("This conversation request does not exist.", status=404)
    return _decode(row)


def listing(workspace_id=None):
    with projects.database() as db:
        rows = [_decode(row) for row in db.execute("SELECT * FROM requests ORDER BY rowid DESC LIMIT 200")]
        for row in rows:
            parked = db.execute("SELECT at,by,reason FROM request_parking WHERE request_id=?", (row["id"],)).fetchone()
            row["parking"] = dict(parked) if parked else None
            row["actions"] = [dict(action) for action in db.execute(
                "SELECT method,path,state,error FROM request_actions WHERE request_id=? ORDER BY rowid", (row["id"],))]
    result = []
    for row in rows:
        if workspace_id is not None and row["project_id"] != workspace_id:
            continue
        row["recovery"] = recovery(row)
        if row["recovery"]:
            row["error"] = row["recovery"]["reason"]
        result.append({key: value for key, value in row.items() if key not in ("payload", "ownership", "reply")})
    return result


def _current(row):
    payload = row["payload"]
    return project_threads.ownership(payload["permalink"], payload.get("parent"))


def park(identity, reason, *, workspace_id):
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise projects.ProjectError("Parking needs a reason of 1–2000 characters.")
    with projects.operation():
        row = get(identity)
        if row["project_id"] != workspace_id:
            raise projects.ProjectError("This request belongs to another project.", code="project_changed", status=409)
        projects.get(workspace_id, active=True)
        if row["state"] not in ("reply_sending", "reply_uncertain"):
            raise projects.ProjectError("Only an uncertain reply can be parked.", status=409)
        with projects.database() as db:
            db.execute("INSERT OR IGNORE INTO request_parking VALUES (?,?,?,?)",
                       (identity, store.utc_now_iso(), "operator", scrub_credentials(reason.strip())))
            db.execute("DELETE FROM request_context WHERE project_id=? AND request_id=?", (workspace_id, identity))
            parked = dict(db.execute("SELECT at,by,reason FROM request_parking WHERE request_id=?", (identity,)).fetchone())
    return {"request_id": identity, "workspace_id": workspace_id, "state": row["state"], "parking": parked}


def authorize(identity, workspace_id):
    row = get(identity)
    if row["project_id"] != workspace_id:
        raise projects.ProjectError("This request belongs to another project.", code="project_changed", status=409)
    if row["state"] in ("choice", "invalid", "choice_reply") or row["ownership"] != _current(row):
        with projects.database() as db:
            db.execute("UPDATE requests SET error=? WHERE id=?", (OWNERSHIP_CHANGED, identity))
        raise projects.ProjectError(OWNERSHIP_CHANGED,
                                   code="request_ownership_changed", status=409)
    projects.get(workspace_id, active=True)
    return row


def _uncertain(row):
    if row["state"] in ("reply_sending", "reply_uncertain") or row["prompt_state"] in ("sending", "uncertain"):
        return True
    with projects.database() as db:
        return db.execute("SELECT 1 FROM request_actions WHERE request_id=? AND state IN ('sending','uncertain')",
                          (row["id"],)).fetchone() is not None


def recovery(row):
    """Describe safe recovery without claiming that any reply or action happened."""
    if row["state"] not in ("queued", "delivered", "invalid"):
        return None
    if row["state"] != "invalid" and row["ownership"] == _current(row):
        return None
    uncertain = _uncertain(row)
    with projects.database() as db:
        bound = db.execute("SELECT 1 FROM request_context WHERE request_id=?", (row["id"],)).fetchone()
    released = row["state"] == "invalid" and not bound
    return {"code": "request_ownership_changed", "reason": row["error"] or OWNERSHIP_CHANGED,
            "release_allowed": not uncertain and not released, "released": released,
            "instruction": ("Request released; no reply sent. Completed action receipts are retained." if released else
                            "Inspect the uncertain send or action outcome before recovery. It will not be repeated automatically."
                            if uncertain else "Take pending or release this blocked request. No action or reply will be replayed.")}


def _release(row, reason):
    with projects.database() as db:
        db.execute("UPDATE requests SET state='invalid',error=? WHERE id=?", (reason, row["id"]))
        db.execute("DELETE FROM request_context WHERE project_id=? AND request_id=?", (row["project_id"], row["id"]))
        raw = db.execute("SELECT assistant_state FROM projects WHERE id=?", (row["project_id"],)).fetchone()
        state = assistant.AssistantState.from_dict(json.loads(raw[0]), memo_spool=row["project_id"])
        retained = tuple(note for note in state.pending if not (
            note.kind == "conversation_request" and (note.data or {}).get("request_id") == row["id"]))
        state = replace(state, pending=retained, pending_since=retained[0].at if retained else None)
        db.execute("UPDATE projects SET assistant_state=? WHERE id=?", (json.dumps(state.to_dict()), row["project_id"]))


def release(identity, reason, *, workspace_id):
    """Operator recovery for a known invalid request, preserving every receipt."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise projects.ProjectError("Release needs a reason of 1–2000 characters.")
    with projects.operation():
        row = get(identity)
        if row["project_id"] != workspace_id:
            raise projects.ProjectError("This request belongs to another project.", code="project_changed", status=409)
        decision = recovery(row)
        if decision and decision["released"]:
            return {"request_id": identity, "workspace_id": workspace_id, "state": "invalid", "released": True}
        if not decision or not decision["release_allowed"]:
            raise projects.ProjectError("Only a blocked request with no uncertain effects can be released.", status=409)
        _release(row, decision["reason"])
        store.append_event("request_released", data={"request_id": identity, "workspace_id": workspace_id,
                                                    "reason": scrub_credentials(reason.strip()), "by": "operator"})
    return {"request_id": identity, "workspace_id": workspace_id, "state": "invalid", "released": True}


def attach_spawned(component, session_id, targets):
    """Capture only the source ownership added by this request's actual spawn."""
    action = _ACTION.get()
    row = action["row"] if action and action["path"] == "/api/sessions" and action["method"] == "POST" else None
    before = _current(row) if row else None
    owner = project_threads.session_owner(session_id) if row else None
    records = component.adopt_launch_targets(session_id, targets, by="operator")
    if (not row or before != row["ownership"] or not owner or owner["project_id"] != row["project_id"]
            or not any(project_threads.key(record["permalink"]) == project_threads.key(row["payload"]["permalink"])
                       for record in records)):
        return records
    if any(item["owner"] == owner["owner"] for item in before):
        return records
    expected = sorted(before + [owner], key=lambda item: item["owner"])
    if _current(row) == expected:
        action["attachment"] = {"session_id": session_id, "before": before, "after": expected, "owner": owner}
    return records


def _completed_attachment(action):
    attachment = action.get("attachment")
    row = action["row"]
    if not attachment or project_threads.session_owner(attachment["session_id"]) != attachment["owner"]:
        return None
    state = projects.get(row["project_id"])["assistant_state"]
    if (state.get("session_id") != row["generation"] or _current(row) != attachment["after"]
            or get(row["id"])["ownership"] != attachment["before"]):
        return None
    return attachment


def _queue(db, row, project_id, ownership):
    payload = {**row["payload"], "workspace_id": project_id, "request_id": row["id"]}
    payload["instruction"] = (
        f"[{payload['source']['channel']} · {payload['sender']}] Handle this request only in project {project_id}. "
        f"Use lmer-ctl --request-id {row['id']} for actions. Reply with "
        f"lmer-ctl requests reply {row['id']} TEXT; its destination is the original thread. "
        "On an ownership refusal, take pending to release safely before unrelated work or handoff/rotation. "
        "Do not redirect or repeat a completed action; request fresh instructions in the source thread."
    )
    payload["instruction"] += " Your credential is bound to this request until it is answered and you take pending again."
    payload["actions"] = [dict(action) for action in db.execute(
        "SELECT method,path,state,result,error FROM request_actions WHERE request_id=?", (row["id"],))]
    if payload["actions"]:
        payload["instruction"] += " Existing action receipts are attached. Do not repeat completed or uncertain actions."
    raw = db.execute("SELECT assistant_state FROM projects WHERE id=?", (project_id,)).fetchone()
    state = assistant.AssistantState.from_dict(json.loads(raw[0]), memo_spool=project_id)
    note = assistant.PendingNote(at=store.utc_now_iso(), kind="conversation_request",
                                 note="A conversation request is waiting for its owning project.", data=payload)
    state = replace(state, pending=(*state.pending, note), pending_since=state.pending_since or note.at,
                    pending_seq=state.pending_seq + (not state.pending))
    db.execute("UPDATE projects SET assistant_state=? WHERE id=?", (json.dumps(state.to_dict()), project_id))
    db.execute("UPDATE requests SET state='queued',project_id=?,ownership=?,payload=? WHERE id=?",
               (project_id, json.dumps(ownership), json.dumps(payload), row["id"]))


def _reply_source(row, text, client=None):
    payload = row["payload"]
    if client is None:
        ref, client, _ = thread_access.resolve_thread(payload["permalink"])
    if row["transport"] == "slack":
        return client.post_message(row["conversation"], row["root"], text)
    return client.send_thread_text(row["conversation"], row["root"], text)


def _notice(row, text, client=None):
    with projects.database() as db:
        changed = db.execute("UPDATE requests SET prompt_state='sending' WHERE id=? AND prompt_state IS NULL",
                             (row["id"],)).rowcount
    if not changed:
        return
    try:
        _reply_source(row, text, client)
    except Exception as exc:
        with projects.database() as db:
            db.execute("UPDATE requests SET prompt_state='uncertain',error=? WHERE id=?",
                       (f"Source-thread notice outcome is unknown: {scrub_credentials(str(exc))}", row["id"]))
        raise ThreadError("The source-thread notice outcome is unknown; inspect its request record before retrying.", status=502) from exc
    with projects.database() as db:
        db.execute("UPDATE requests SET prompt_state='sent' WHERE id=?", (row["id"],))


def _named_projects(text):
    names = {project["name"].casefold(): project for project in projects.listing()}
    return [(names[text[:end.start()].casefold()], text[end.end():])
            for end in re.finditer(r"\s+|$", text) if text[:end.start()].casefold() in names]


def admit(identity, payload, client):
    with projects.operation(), assistant._LOCK:
        with projects.database() as db:
            existing = _decode(db.execute("SELECT * FROM requests WHERE id=?", (identity,)).fetchone())
        if existing:
            _pending_notice(existing, client)
            return {"status": "duplicate", "request_id": identity, "state": existing["state"],
                    "workspace_id": existing["project_id"], "error": existing["error"]}
        selection = CHOICE.search(payload["message"]["text"])
        ownership = project_threads.ownership(payload["permalink"], payload.get("parent"))
        owners = {row["project_id"] for row in ownership}
        selected = next(iter(owners)) if len(owners) == 1 else None
        row = {"id": identity, "transport": payload["source"]["channel"], "conversation": payload["conversation"],
               "root": payload["root"], "sender": payload["sender"], "payload": assistant._scrubbed_data(payload),
               "ownership": ownership}
        if selection:
            original = get(selection[1])
            if ((original["transport"], original["conversation"], original["root"], original["sender"]) !=
                    (row["transport"], row["conversation"], row["root"], row["sender"])):
                raise projects.ProjectError("Choose the project in the original request's thread, as its author.", status=409)
            if original["state"] != "choice":
                return {"status": "duplicate", "request_id": original["id"], "state": original["state"]}
            selected = selection[2]
            projects.get(selected, active=True)
            ownership = _current(original)
            if not ownership:
                project_threads.choose_unbound(original["payload"]["permalink"], selected)
                ownership = _current(original)
            with projects.database() as db:
                _insert(db, row, "choice_reply", selected)
                _queue(db, original, selected, ownership)
            return _accepted(original["id"], selected, original["payload"]["permalink"])
        text = row["payload"]["message"]["text"].lstrip()
        routed = None
        matches = _named_projects(text) if payload["entry"] == "mention" else []
        if len(matches) > 1:
            selected = None
            row["error"] = "The leading text matches more than one project name. Choose the intended project."
        elif matches:
            project, remainder = matches[0]
            if owners and project["id"] not in owners:
                selected = None
                row["error"] = "The named project does not own this thread. Use a new thread for that project."
            else:
                selected = project["id"]
                message = row["payload"]["message"]
                message.setdefault("original_text", message["text"])
                message["text"] = remainder
        elif not owners and payload["entry"] == "mention":
            routed = channel_routes.project_for(row["transport"], row["conversation"])
            selected = routed or projects.GENERAL
        state = "choice"
        if selected:
            project = projects.get(selected)
            if project["archived"]:
                state = "invalid"
                row["error"] = (f"This channel is mapped to project {project['name']}, which is archived; "
                                "restore it or remap the channel before sending a new request." if routed else
                                f"Project {project['name']} is archived; restore it before sending a new request.")
            elif not ownership:
                project_threads.choose_unbound(payload["permalink"], selected)
                ownership = _current(row)
                row["ownership"] = ownership
        with projects.database() as db:
            _insert(db, row, state, selected)
            if selected and state != "invalid":
                _queue(db, row, selected, ownership)
        if state == "invalid":
            _pending_notice(get(identity), client)
            return {"status": "project_unavailable", "request_id": identity, "workspace_id": selected,
                    "state": state, "error": row["error"]}
        if selected:
            result = _accepted(identity, selected, payload["permalink"])
            _pending_notice(get(identity), client)
            return result
        _pending_notice(get(identity), client)
        return {"status": "project_required", "request_id": identity, "state": "choice", "permalink": payload["permalink"]}


def _pending_notice(row, client):
    if row["prompt_state"] is not None:
        return
    if row["state"] == "choice":
        options = "\n".join(f"{project['name']}: {project['id']}" for project in projects.listing(archived=False))
        reason = "This thread has conflicting project ownership." if row["ownership"] else "This request has no project ownership."
        _notice(row, (row["error"] or reason) +
                "\nChoose a project for this request. Mention the bot in this thread with:\n"
                f"project {row['id']} PROJECT_ID\n{options}", client)
    elif (row["state"] == "invalid" and row["error"] and row["generation"] is None
          and row["ownership"] == _current(row)):
        _notice(row, row["error"], client)
    elif row["state"] == "queued":
        status = projects.status(row["project_id"])
        if status["lifecycle"] != "running":
            name = projects.get(row["project_id"])["name"]
            _notice(row, f"Request {row['id']} retained for {name}; its assistant is {status['lifecycle']}. "
                    "Start or retry that project's assistant in the platform to continue.", client)


def _insert(db, row, state, selected):
    db.execute("INSERT INTO requests (id,transport,conversation,root,sender,payload,state,project_id,ownership,error) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)", (row["id"], row["transport"], row["conversation"], row["root"], row["sender"],
                                               json.dumps(row["payload"]), state, selected,
                                               json.dumps(row["ownership"]), row.get("error")))


def recover(project_id):
    with projects.operation(), scope.bound(project_id), assistant._LOCK:
        session = assistant.read_state().session_id
        if not session:
            return
        with projects.database() as db:
            rows = [_decode(row) for row in db.execute(
                "SELECT * FROM requests WHERE project_id=? AND state='delivered' AND generation<>?",
                (project_id, session))]
            for row in rows:
                _queue(db, row, project_id, row["ownership"])


def action(binding, perform):
    from fastapi.encoders import jsonable_encoder
    from starlette.responses import JSONResponse, Response

    row = authorize(binding["request_id"], binding["workspace_id"])
    body = {key: value for key, value in binding["body"].items() if key not in ("request_id", "workspace_id")}
    identity = hashlib.sha256(json.dumps([row["id"], binding["method"], binding["path"], body],
                                        sort_keys=True).encode()).hexdigest()
    legacy = []
    for metadata in ({}, {"workspace_id": row["project_id"]}, {"request_id": row["id"]},
                     {"workspace_id": row["project_id"], "request_id": row["id"]}):
        legacy.append(hashlib.sha256(json.dumps([row["id"], binding["method"], binding["path"], {**body, **metadata}],
                                                sort_keys=True).encode()).hexdigest())
    with projects.database() as db:
        previous = db.execute("SELECT * FROM request_actions WHERE id IN (?,?,?,?) "
                              "ORDER BY CASE WHEN state IN ('refused','prepared') THEN 1 ELSE 0 END LIMIT 1", legacy).fetchone()
        if previous:
            if previous["state"] == "complete":
                receipt = json.loads(previous["result"])
                return JSONResponse(receipt["body"], status_code=receipt["status"])
            if previous["state"] not in ("refused", "prepared"):
                raise projects.ProjectError("This action's outcome is uncertain. Inspect it before issuing new work.",
                                            code="action_outcome_uncertain", status=409)
        if row["state"] != "delivered":
            raise projects.ProjectError("Take the request before acting on it; completed requests cannot act again.", status=409)
        db.execute("DELETE FROM request_actions WHERE id=?", (identity,))
        db.execute("INSERT INTO request_actions (id,request_id,method,path,state) VALUES (?,?,?,?,'sending')",
                   (identity, row["id"], binding["method"], binding["path"]))
    outcome = {"id": identity, "effect": None, "row": row, "method": binding["method"], "path": binding["path"]}
    token = _ACTION.set(outcome)
    try:
        result = perform()
        receipt = {"status": result.status_code if isinstance(result, Response) else binding["success_status"],
                   "body": json.loads(result.body) if isinstance(result, Response) else jsonable_encoder(result)}
    except Exception as exc:
        with projects.database() as db:
            db.execute("UPDATE request_actions SET state=?,error=? WHERE id=?",
                       ("refused" if outcome["effect"] is False else "uncertain", scrub_credentials(str(exc)), identity))
        raise
    finally:
        _ACTION.reset(token)
    attachment = _completed_attachment(outcome)
    if attachment:
        receipt["ownership_transition"] = attachment
    with projects.database() as db:
        db.execute("UPDATE request_actions SET state='complete',result=? WHERE id=?", (json.dumps(receipt), identity))
        if attachment:
            db.execute("UPDATE requests SET ownership=?,error=NULL WHERE id=?", (json.dumps(attachment["after"]), row["id"]))
    return result


def _accepted(identity, selected, permalink):
    status = projects.status(selected)
    return {"status": "accepted", "request_id": identity, "workspace_id": selected, "session": status["session_id"],
            "lifecycle": status["lifecycle"], "permalink": permalink}


def drain(state):
    project_id = scope.current()
    current = active(project_id)
    unavailable = None
    row = get(current) if current else None
    if row and row["state"] in ("queued", "delivered", "invalid"):
        decision = recovery(row)
        if decision and decision["release_allowed"]:
            _release(row, decision["reason"])
            unavailable = row["id"]
            current = None
    if current and row["state"] == "replied":
        with projects.database() as db:
            db.execute("DELETE FROM request_context WHERE project_id=?", (project_id,))
        current = None
    decisions, notes = [], []
    if unavailable:
        notes.append(_unavailable(row, decision["reason"]))
    retained = []
    for note in state.pending:
        identity = (note.data or {}).get("request_id") if note.kind == "conversation_request" else None
        if not identity:
            notes.append(note.to_dict())
            continue
        row = get(identity)
        if identity == unavailable:
            continue
        if unavailable:
            retained.append(note)
            continue
        if current and identity != current:
            retained.append(note)
            continue
        if row["state"] not in ("queued", "invalid"):
            continue
        try:
            authorize(identity, project_id)
        except projects.ProjectError as exc:
            if _uncertain(row):
                retained.append(note)
                continue
            decisions.append(("invalid", str(exc), identity))
            notes.append(_unavailable(row, str(exc)))
            if current == identity:
                current = None
        else:
            current = identity
            decisions.append(("delivered", None, identity))
            notes.append(note.to_dict())
    with projects.database() as db:
        for status, error, identity in decisions:
            db.execute("UPDATE requests SET state=?,error=?,generation=? WHERE id=?",
                       (status, error, state.session_id, identity))
        if current:
            db.execute("INSERT OR REPLACE INTO request_context VALUES (?,?)", (project_id, current))
        else:
            db.execute("DELETE FROM request_context WHERE project_id=?", (project_id,))
        cleared = replace(state, pending=tuple(retained), pending_since=retained[0].at if retained else None,
                          **assistant.ended_episode(state))
        db.execute("UPDATE projects SET assistant_state=? WHERE id=?", (json.dumps(cleared.to_dict()), project_id))
    return notes


def _unavailable(row, reason):
    with projects.database() as db:
        receipts = [dict(action) for action in db.execute(
            "SELECT method,path,state,result,error FROM request_actions WHERE request_id=?", (row["id"],))]
    return {"at": store.utc_now_iso(), "kind": "request_unavailable", "note": reason,
            "data": {"request_id": row["id"], "permalink": row["payload"]["permalink"], "workspace_id": row["project_id"],
                     "actions": receipts, "reply_sent": False, "released": True}}


def reply(identity, text, *, workspace_id, session_id):
    from . import project_api

    if not isinstance(text, str) or not text.strip() or len(text) > 20000:
        raise projects.ProjectError("A reply needs 1–20000 characters.")
    with projects.operation():
        row = authorize(identity, workspace_id)
        if row["state"] == "replied":
            return {"request_id": identity, "duplicate": True, "posted": json.loads(row["reply"])}
        if row["state"] != "delivered":
            raise projects.ProjectError("This request is not awaiting a reply, or a previous reply outcome is unknown.", status=409)
        target, client, _ = thread_access.resolve_thread(row["payload"]["permalink"])
        private = assistant_conversation.private_thread(target, client)
        project_api.recheck()
        authorize(identity, workspace_id)
        with projects.database() as db:
            db.execute("UPDATE requests SET state='reply_sending' WHERE id=?", (identity,))
        try:
            ref = _reply_source(row, text, client)
        except Exception as exc:
            with projects.database() as db:
                db.execute("UPDATE requests SET state='reply_uncertain',error=? WHERE id=?",
                           (scrub_credentials(str(exc)), identity))
            raise ThreadError("The source-thread reply outcome is unknown; it will not be sent again automatically.", status=502) from exc
        posted = {"ref": ref, "permalink": row["payload"]["permalink"]}
        with projects.database() as db:
            db.execute("UPDATE requests SET state='replied',reply=?,error=NULL WHERE id=?", (json.dumps(posted), identity))
        with scope.bound(workspace_id):
            recorded = f"🔒 direct reply sent — {len(text)} characters, body not recorded" if private else text
            assistant_conversation.record(recorded, kind="reply", thread=row["payload"]["permalink"], session_id=session_id)
        return {"request_id": identity, "posted": posted}
