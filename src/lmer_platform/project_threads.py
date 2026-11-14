"""Thread ownership outlives connectors; worker bindings follow their run home.

A worker binding is keyed by its run twice — ``owner = run:<key>`` and
``run_key`` — and the key is the tracked index's (uid for a schema-2 run, slug
otherwise; :func:`lmer_platform.runs.key_parts`). When a run's key changes,
:func:`lmer_platform.projects.rekey` moves both in the same transaction as the
run's home, so a binding never points at a home that has moved on.
"""
import json

from lmer_cli.threads import parse_thread
from . import projects, registry, runs
from . import connectors


def key(permalink):
    ref = parse_thread(permalink)
    return json.dumps([ref.channel, *ref.registry_key()], separators=(",", ":"))


def session_owner(session_id):
    """The ownership record an attachment to this session contributes."""
    project_id = projects.session_home(session_id)
    if not project_id:
        return None
    parts = runs.run_for_session(session_id)
    run_key = runs.run_key(*parts) if parts else None
    home = projects.run_home(run_key) if run_key else None
    return {"owner": f"run:{run_key}" if run_key else f"project:{project_id}",
            "project_id": home["project_id"] if home else project_id, "run_key": run_key,
            "revision": home["revision"] if home else None}


def remember(session_id, permalink):
    if not projects.enabled():
        return
    owner = session_owner(session_id)
    if not owner:
        return
    with projects.database() as db:
        db.execute("INSERT OR REPLACE INTO thread_owners VALUES (?,?,?,?)",
                   (key(permalink), owner["owner"], owner["project_id"], owner["run_key"]))


def observe_connectors():
    component = connectors.ensure_connectors()
    for session, records in component.sessions().items():
        for record in records:
            remember(session, record["permalink"])
    for project in projects.listing():
        for permalink in project["settings"].get("threads") or []:
            with projects.database() as db:
                db.execute("INSERT OR IGNORE INTO thread_owners VALUES (?,?,?,NULL)",
                           (key(permalink), f"project:{project['id']}", project["id"]))


def ownership(permalink, parent=None):
    observe_connectors()
    with projects.database() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM thread_owners WHERE thread=? ORDER BY owner", (key(permalink),))]
    found = []
    for row in rows:
        home = projects.run_home(row["run_key"]) if row["run_key"] else None
        found.append({"owner": row["owner"], "project_id": home["project_id"] if home else row["project_id"],
                      "run_key": row["run_key"], "revision": home["revision"] if home else None})
    if parent and parent.get("session"):
        owner = projects.session_home(parent["session"])
        if owner:
            found.append({"owner": f"parent:{parent['session']}", "project_id": owner, "run_key": None, "revision": None})
    return sorted(found, key=lambda row: row["owner"])


def choose_unbound(permalink, project_id):
    with projects.database() as db:
        db.execute("INSERT OR IGNORE INTO thread_owners VALUES (?,?,?,NULL)", (key(permalink), "choice", project_id))


def authorize(permalink, project_id):
    owners = {row["project_id"] for row in ownership(permalink)}
    if owners and owners != {project_id}:
        raise projects.ProjectError("This thread belongs to another project or has conflicting ownership.", status=409)


def releasable(permalink, project_id):
    """The owners *project_id* may drop on a thread, refusing anything that would steal it.

    A connector record or a standing settings thread re-seeds its owner row on the
    next :func:`ownership` read, so either has to go before the claim can.
    """
    thread = key(permalink)
    for session, records in connectors.ensure_connectors().sessions().items():
        record = next((record for record in records if key(record["permalink"]) == thread), None)
        if record:
            held = ("holds this thread live" if registry.is_live(registry.read_session(session))
                    else "still records a connection to this thread")
            raise projects.ProjectError(
                f"Session {session} {held}; a release never takes a thread from a session. "
                f"Disconnect it first: lmer-ctl threads disconnect {session} {record['id']}", status=409)
    project = projects.get(project_id)
    if any(key(link) == thread for link in project["settings"].get("threads") or []):
        raise projects.ProjectError(
            f"This thread is in project {project['name']}'s standing thread list; "
            "remove it from the project's settings first.", status=409)
    rows = ownership(permalink)
    if {row["project_id"] for row in rows} - {project_id}:
        raise projects.ProjectError("This thread belongs to another project or has conflicting ownership.", status=409)
    return [row["owner"] for row in rows]


def drop(db, permalink, owners, to=None):
    """Delete *owners* on the thread inside the caller's transaction; *to* becomes its choice."""
    thread = key(permalink)
    db.executemany("DELETE FROM thread_owners WHERE thread=? AND owner=?", [(thread, owner) for owner in owners])
    if to:
        db.execute("INSERT OR REPLACE INTO thread_owners VALUES (?,?,?,NULL)", (thread, "choice", to))
