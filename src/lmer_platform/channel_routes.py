"""Slack channel / Matrix room → project routing for bot mentions. See docs/PROJECTS.md."""
import re
from urllib.parse import urlsplit

from lmer_cli import thread_access
from lmer_cli.threads import ThreadError
from matrix_chat.permalink import _fragment_parts, is_matrix_room
from . import projects

_SLACK_ID = re.compile(r"[CG][A-Z0-9]{6,}")
_SLACK_DM = re.compile(r"D[A-Z0-9]{6,}")
_SLACK_NAME = re.compile(r"#?([a-z0-9][a-z0-9._-]{0,79})")
MAX_ROUTES = 64


def _refuse(message, status=400):
    raise projects.ProjectError(message, code="channel_route_invalid", status=status)


def _slack_link(spec):
    parts = urlsplit(spec)
    host = parts.hostname or ""
    if host != "slack.com" and not host.endswith(".slack.com"):
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if "archives" in segments and segments.index("archives") + 1 < len(segments):
        return segments[segments.index("archives") + 1]
    if parts.hostname == "app.slack.com" and len(segments) >= 3 and segments[0] == "client":
        return segments[2]
    return None


def _matrix_link(spec):
    parts = _fragment_parts(spec)
    return parts[0] if parts and len(parts) <= 2 and is_matrix_room(parts[0]) else None


def _slack_channel(spec, env):
    if _SLACK_DM.fullmatch(spec):
        _refuse("Direct messages cannot be mapped to a project; map a channel instead.")
    if _SLACK_ID.fullmatch(spec):
        return spec, spec
    name = _SLACK_NAME.fullmatch(spec)
    if not name:
        return None
    client = thread_access._slack_client(thread_access.slack_token(env))
    try:
        found = client.find_channels(name[1])
    except Exception as exc:
        if getattr(exc, "code", None) == "missing_scope":
            _refuse(f"The Slack token cannot list channels to resolve #{name[1]}; "
                    "give the channel ID or a link to the channel instead.")
        raise ThreadError(f"Slack could not resolve #{name[1]}: {exc}", status=502) from exc
    if len(found) != 1:
        _refuse(f"No Slack channel named #{name[1]} is visible to the bot." if not found else
                f"More than one Slack channel is named #{name[1]}; give its ID instead.")
    return found[0], f"#{name[1]}"


def resolve(spec, env=None):
    """``(transport, conversation, label)`` for one channel, room, alias or link."""
    if not isinstance(spec, str) or not spec.strip() or len(spec) > 500:
        _refuse("A mention channel needs a Slack channel or Matrix room of 1–500 characters.")
    spec = spec.strip()
    if spec.startswith(("http://", "https://")):
        channel = _slack_link(spec)
        if channel:
            if not _SLACK_ID.fullmatch(channel):
                _refuse("Map a Slack channel link; direct messages and other links name no channel."
                        if _SLACK_DM.fullmatch(channel) else f"{spec} names no Slack channel.")
            return "slack", channel, channel
        spec = _matrix_link(spec) or _refuse(f"{spec} is not a Slack channel or Matrix room link.")
    if is_matrix_room(spec) and spec.startswith("!"):
        return "matrix", spec, spec
    if is_matrix_room(spec):
        client = thread_access._matrix_client(thread_access.matrix_account(env))
        try:
            return "matrix", client.resolve_room(spec), spec
        except Exception as exc:
            _refuse(f"The Matrix alias {spec} does not resolve to a room: {exc}")
    found = _slack_channel(spec, env)
    if found:
        return ("slack", *found)
    _refuse(f"{spec} is not a Slack channel ID, #name or link, or a Matrix room ID, alias or link.")


def _row(row, names):
    project = names.get(row["project_id"]) or {}
    return {"transport": row["transport"], "conversation": row["conversation"], "label": row["label"],
            "project_id": row["project_id"], "project_name": project.get("name"),
            "archived": bool(project.get("archived"))}


def listing(workspace_id=None):
    """Every mapping, or one project's, with whether its project is archived (and so disabled)."""
    if not projects.enabled():
        return []
    names = {project["id"]: project for project in projects.listing()}
    with projects.database() as db:
        rows = db.execute("SELECT * FROM channel_routes ORDER BY rowid").fetchall()
    return [_row(row, names) for row in rows if workspace_id is None or row["project_id"] == workspace_id]


def project_for(transport, conversation):
    with projects.database() as db:
        row = db.execute("SELECT project_id FROM channel_routes WHERE transport=? AND conversation=?",
                         (transport, conversation)).fetchone()
    return row[0] if row else None


def _known(spec, workspace_id=None):
    """The stored route *spec* names, by label or conversation, before any lookup.

    Re-resolving a stored ``#name`` would follow a rename to another channel.
    With *workspace_id* only that project's rows count, so another project's
    stale label cannot shadow a live lookup.
    """
    if not isinstance(spec, str):
        return None
    with projects.database() as db:
        rows = db.execute("SELECT * FROM channel_routes WHERE label=? OR conversation=?",
                          (spec.strip(), spec.strip())).fetchall()
    rows = [row for row in rows if workspace_id is None or row["project_id"] == workspace_id]
    return (rows[0]["transport"], rows[0]["conversation"], rows[0]["label"]) if len(rows) == 1 else None


def replace(workspace_id, specs, env=None):
    """Make *specs* exactly *workspace_id*'s mapped channels; never takes one from another project."""
    if not isinstance(specs, list) or len(specs) > MAX_ROUTES:
        _refuse(f"Mention channels must be a list of at most {MAX_ROUTES} channels or rooms.")
    resolved = {}
    for spec in specs:
        transport, conversation, label = _known(spec, workspace_id) or resolve(spec, env)
        resolved.setdefault((transport, conversation), label)
    with projects.operation():
        projects.get(workspace_id, active=True)
        with projects.database() as db:
            for (transport, conversation), label in resolved.items():
                held = db.execute("SELECT p.id,p.name FROM channel_routes r JOIN projects p ON p.id=r.project_id "
                                  "WHERE r.transport=? AND r.conversation=?", (transport, conversation)).fetchone()
                if held and held["id"] != workspace_id:
                    _refuse(f"{label} is already mapped to project {held['name']}; remove it there first.", 409)
            db.execute("DELETE FROM channel_routes WHERE project_id=?", (workspace_id,))
            db.executemany("INSERT INTO channel_routes (transport,conversation,project_id,label) VALUES (?,?,?,?)",
                           [(transport, conversation, workspace_id, label)
                            for (transport, conversation), label in resolved.items()])
    return listing(workspace_id)


def remove(spec, env=None):
    """Drop one mapping whatever project holds it, archived ones included."""
    transport, conversation, label = _known(spec) or resolve(spec, env)
    with projects.operation(), projects.database() as db:
        removed = db.execute("DELETE FROM channel_routes WHERE transport=? AND conversation=?",
                             (transport, conversation)).rowcount
    if not removed:
        _refuse(f"{label} is not mapped to any project.", 404)
    return listing()
