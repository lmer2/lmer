"""Thread ownership outlives connectors; worker bindings follow their run home."""
import json

from lmer_cli.threads import parse_thread
from . import projects, runs
from . import connectors


def key(permalink):
    ref = parse_thread(permalink)
    return json.dumps([ref.channel, *ref.registry_key()], separators=(",", ":"))


def remember(session_id, permalink):
    if not projects.enabled():
        return
    project_id = projects.session_home(session_id)
    if not project_id:
        return
    parts = runs.run_for_session(session_id)
    run_key = runs.run_key(*parts) if parts else None
    owner = f"run:{run_key}" if run_key else f"project:{project_id}"
    with projects.database() as db:
        db.execute("INSERT OR REPLACE INTO thread_owners VALUES (?,?,?,?)",
                   (key(permalink), owner, project_id, run_key))


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
