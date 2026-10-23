"""Durable workspace ownership and admission. See docs/PROJECTS.md."""
from __future__ import annotations

from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import replace
from functools import wraps
import fcntl
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid

from lmer_cli.comms import SUPERVISOR_AUTHORITY_LEVELS

from . import project_context as scope, registry, store
from . import assistant_history, runs

logger = logging.getLogger(__name__)
GENERAL = scope.GENERAL
#: Project settings that are not assistant launch settings: they never reach an
#: assistant start (issue #594, :mod:`lmer_platform.authority`).
WORKER_SETTING_KEYS = ("supervisor_authority",)
COLORS = (
    "Red", "Orange", "Amber", "Yellow", "Lime", "Green", "Emerald", "Teal",
    "Cyan", "Sky blue", "Blue", "Indigo", "Violet", "Purple", "Magenta",
    "Pink", "Rose", "Brown", "Slate", "Gray",
)
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_LOCK = threading.RLock()
_LOCAL = threading.local()
_ADMISSION = ContextVar("assistant_admission", default=None)


class ProjectError(ValueError):
    def __init__(self, message: str, *, code="project_invalid", status=400):
        self.code, self.status = code, status
        super().__init__(message)


def path():
    return store.platform_dir() / "projects.sqlite3"


def enabled():
    if not path().is_file():
        return False
    with closing(sqlite3.connect(path().absolute().as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone():
            return False
        return bool(db.execute("SELECT 1 FROM metadata WHERE key='migrated'").fetchone())


def serialized(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not enabled():
            return function(*args, **kwargs)
        with operation():
            return function(*args, **kwargs)
    return wrapped


@contextmanager
def admitting(workspace_id):
    token = _ADMISSION.set(workspace_id)
    try:
        yield
    finally:
        _ADMISSION.reset(token)


def require_admission(workspace_id):
    if _ADMISSION.get() != workspace_id:
        raise ProjectError("Start the project assistant through its durable admission queue.", status=409)


def run_operation(function):
    @wraps(function)
    def wrapped(config, request):
        # Normalise before reading any field. This decorator consumes
        # workspace_id/home_revision/confirm_project directly, and the request's
        # own validate() — which runs inside the wrapped function — is too late
        # for them. Idempotent, so the inner call is a no-op.
        request = request.validate()
        if not enabled():
            return function(config, request)

        with operation():
            # The request's own fields are the body it was built from, so a
            # request that carries a uid binds to the uid home and one that does
            # not (or predates the field) to the slug home, by key_parts' rule.
            key = runs.run_key(*runs.key_parts(vars(request)))
            home = run_home(key)
            chosen = request.workspace_id or scope.current() or (home or {}).get("project_id")
            get(chosen, active=True)
            check_home(key, chosen, request.home_revision)
            if home and home["reconfirm"] and (not request.confirm_project or request.home_revision != home["revision"]):
                raise ProjectError("This run moved. Confirm its current project before continuing.",
                                   code="project_reconfirmation_required", status=409)
            with scope.bound(chosen):
                result = function(config, request)
            if home:
                with database() as db:
                    db.execute("UPDATE homes SET reconfirm=0 WHERE run_key=?", (key,))
            return result
    return wrapped


def session_operation(function):
    @wraps(function)
    def wrapped(session_id, *args, **kwargs):
        if not enabled():
            return function(session_id, *args, **kwargs)
        with operation():
            owner = session_home(session_id)
            if scope.current() and scope.current() != owner:
                raise ProjectError("This session belongs to another project.", status=409)
            get(owner, active=True)
            with scope.bound(owner):
                return function(session_id, *args, **kwargs)
    return wrapped


@contextmanager
def operation(*, force=False):
    """Serialize admission, moves and control across daemon and host CLI writers."""
    if not force and not enabled():
        yield
        return
    with _LOCK:
        depth = getattr(_LOCAL, "depth", 0)
        fd = None
        if not depth:
            directory = store.platform_dir()
            store.ensure_state_dir(directory)
            fd = os.open(directory / "project-operations.lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        _LOCAL.depth = depth + 1
        try:
            yield
        finally:
            _LOCAL.depth = depth
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)


@contextmanager
def database():
    store.ensure_state_dir(path().parent)
    fd = os.open(path(), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path(), timeout=30)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT UNIQUE NOT NULL,
                color TEXT NOT NULL, settings TEXT NOT NULL DEFAULT '{}',
                repositories TEXT NOT NULL DEFAULT '[]', archived INTEGER NOT NULL DEFAULT 0,
                desired TEXT NOT NULL DEFAULT 'stopped', status TEXT NOT NULL DEFAULT 'stopped',
                error TEXT, retries INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                reserved INTEGER NOT NULL DEFAULT 0, assistant_state TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS starts (
                ticket INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT UNIQUE NOT NULL REFERENCES projects(id)
            );
            CREATE TABLE IF NOT EXISTS homes (
                run_key TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                revision INTEGER NOT NULL DEFAULT 1, reconfirm INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), run_key TEXT
            );
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS event_queue (
                id TEXT PRIMARY KEY, session TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id),
                payload TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS thread_owners (
                thread TEXT NOT NULL, owner TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id),
                run_key TEXT, PRIMARY KEY(thread, owner)
            );
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, transport TEXT NOT NULL, conversation TEXT NOT NULL, root TEXT NOT NULL,
                sender TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, project_id TEXT,
                ownership TEXT NOT NULL, prompt_state TEXT, error TEXT, generation TEXT, reply TEXT
            );
            CREATE TABLE IF NOT EXISTS request_actions (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(id),
                method TEXT NOT NULL, path TEXT NOT NULL, state TEXT NOT NULL,
                result TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS request_context (
                project_id TEXT PRIMARY KEY REFERENCES projects(id),
                request_id TEXT NOT NULL REFERENCES requests(id)
            );
            CREATE TABLE IF NOT EXISTS request_parking (
                request_id TEXT PRIMARY KEY REFERENCES requests(id),
                at TEXT NOT NULL, by TEXT NOT NULL, reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS peer_receipts (
                project_id TEXT NOT NULL REFERENCES projects(id), ref TEXT NOT NULL,
                fingerprint TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
                PRIMARY KEY(project_id, ref)
            );
            CREATE TABLE IF NOT EXISTS channel_routes (
                transport TEXT NOT NULL, conversation TEXT NOT NULL,
                project_id TEXT NOT NULL REFERENCES projects(id), label TEXT NOT NULL,
                PRIMARY KEY(transport, conversation)
            );
        """)
        if "run_key" not in {row[1] for row in db.execute("PRAGMA table_info(sessions)")}:
            db.execute("ALTER TABLE sessions ADD COLUMN run_key TEXT")
        if "launch" not in {row[1] for row in db.execute("PRAGMA table_info(homes)")}:
            db.execute("ALTER TABLE homes ADD COLUMN launch TEXT")
        db.execute("BEGIN IMMEDIATE")
        yield db


def migrate():
    """Adopt legacy identities once, without moving mounts or changing services."""
    with operation(force=True), database() as db:
        if db.execute("SELECT 1 FROM metadata WHERE key='migrated'").fetchone():
            return
        legacy = store.read_json(store.snapshot_path("assistant.json")) or {}
        live = [s for s in registry.list_sessions(live_only=True) if s.get("kind") == "assistant"]
        desired = "running" if live else "stopped"
        db.execute(
            "INSERT OR IGNORE INTO projects (id,name,name_key,color,desired,status,reserved,assistant_state) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (GENERAL, "General", "general", "Blue", desired,
             "running" if live else "stopped", bool(live), json.dumps(legacy)),
        )
        configured = store.read_json(store.snapshot_path("config.json")) or {}
        db.execute("UPDATE projects SET settings=? WHERE id=?",
                   (json.dumps({"threads": configured.get("assistant_threads") or []}), GENERAL))
        for run in runs.list_tracked():
            db.execute("INSERT OR IGNORE INTO homes (run_key,project_id) VALUES (?,?)", (run.key, GENERAL))
            if run.last_session_id:
                db.execute("INSERT OR IGNORE INTO sessions VALUES (?,?,?)", (run.last_session_id, GENERAL, run.key))
        for run in runs.list_tracked():
            entry = registry.read_session(run.last_session_id) if run.last_session_id else None
            if entry:
                task = entry.get("task") or {}
                db.execute("UPDATE homes SET launch=? WHERE run_key=?",
                           (json.dumps({"slot": entry.get("slot"), "preset": task.get("preset")}), run.key))
        for entry in registry.list_sessions():
            parts = runs.run_key_of_entry(entry)
            db.execute("INSERT OR IGNORE INTO sessions VALUES (?,?,?)",
                       (entry["id"], GENERAL, runs.run_key(*parts) if parts else None))
        history = store.platform_dir() / "assistant-history.sqlite3"
        if history.is_file():
            with closing(sqlite3.connect(history)) as old:
                # The history writer creates its file before its tables
                # (assistant_history._database), so a file with no
                # ``generations`` yet holds no session to carry.
                known = old.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='generations'"
                ).fetchone()
                for (session,) in old.execute("SELECT session FROM generations") if known else ():
                    db.execute("INSERT OR IGNORE INTO sessions VALUES (?,?,NULL)", (session, GENERAL))
        for event in store.read_events():
            if event.get("type") != "session_spawned":
                continue
            data = event.get("data") or {}
            key = key_of(data.get("run"))
            if data.get("session") and key:
                db.execute("INSERT OR IGNORE INTO sessions VALUES (?,?,?)", (data["session"], GENERAL, key))
        db.execute("INSERT INTO metadata VALUES ('migrated','1')")


def _decode(row):
    if row is None:
        return None
    value = dict(row)
    for key in ("settings", "repositories", "assistant_state"):
        value[key] = json.loads(value[key])
    value["archived"] = bool(value["archived"])
    value["reserved"] = bool(value["reserved"])
    value.pop("name_key", None)
    return value


def get(workspace_id, *, active=False):
    if not workspace_id:
        raise ProjectError("Choose a project before starting new work.", code="project_required")
    with database() as db:
        row = _decode(db.execute("SELECT * FROM projects WHERE id=?", (workspace_id,)).fetchone())
    if row is None:
        raise ProjectError("This project does not exist.", code="project_not_found", status=404)
    if active and row["archived"]:
        raise ProjectError("This project is archived; restore it first.", code="project_archived", status=409)
    return row


def listing(*, archived=True):
    if not enabled():
        return []
    with database() as db:
        return [_decode(row) for row in db.execute(
            "SELECT * FROM projects" + ("" if archived else " WHERE archived=0") + " ORDER BY name_key"
        )]


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120:
        raise ProjectError("A project needs a name of 1–120 characters.")
    return value.strip()


def validate_assignments(value):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ProjectError("Applicable projects must be a list of project identities.")
    for identifier in value:
        get(identifier)
    return list(dict.fromkeys(value))


def create(name):
    name = _name(name)
    with operation(), database() as db:
        identifier = uuid.uuid4().hex
        try:
            db.execute("INSERT INTO projects (id,name,name_key,color,desired,status) VALUES (?,?,?,?,?,?)",
                       (identifier, name, name.casefold(), COLORS[db.execute(
                           "SELECT count(*) FROM projects").fetchone()[0] % len(COLORS)], "running", "waiting"))
        except sqlite3.IntegrityError as exc:
            raise ProjectError("A project with that name already exists.", code="name_taken", status=409) from exc
        db.execute("INSERT INTO starts (project_id) VALUES (?)", (identifier,))
    return get(identifier)


def _color(value):
    if value in COLORS:
        return value
    if isinstance(value, str) and _HEX_COLOR.fullmatch(value):
        return value.lower()
    raise ProjectError("Choose one of the 20 named project colors or a #rrggbb hex color.")


@serialized
def update(workspace_id, values):
    # deferred: cycle — lmer_platform.config imports this module at top (via lmer_platform.uploads)
    from .config import ASSISTANT_SETTING_KEYS, validate_assistant_override

    allowed = {"name", "color", "settings", "repositories"}
    if set(values) - allowed:
        raise ProjectError("Unknown project setting.")
    current = get(workspace_id, active=True)
    name = _name(values.get("name", current["name"]))
    color = _color(values.get("color", current["color"]))
    settings = values.get("settings", current["settings"])
    if not isinstance(settings, dict) or set(settings) - set(ASSISTANT_SETTING_KEYS) - set(WORKER_SETTING_KEYS):
        raise ProjectError("Invalid assistant overrides.")
    for key, value in settings.items():
        if value is None:
            continue
        if key == "supervisor_authority":
            if value not in SUPERVISOR_AUTHORITY_LEVELS:
                raise ProjectError(
                    f"supervisor_authority must be one of {', '.join(SUPERVISOR_AUTHORITY_LEVELS)}."
                )
        else:
            validate_assistant_override(key, value)
    repositories = values.get("repositories", current["repositories"])
    if not isinstance(repositories, list) or any(not isinstance(item, str) or not item.strip() for item in repositories):
        raise ProjectError("Repositories must be a list of repository URLs.")
    with operation(), database() as db:
        try:
            db.execute("UPDATE projects SET name=?,name_key=?,color=?,settings=?,repositories=? WHERE id=?",
                       (name, name.casefold(), color, json.dumps(settings), json.dumps(repositories), workspace_id))
        except sqlite3.IntegrityError as exc:
            raise ProjectError("A project with that name already exists.", code="name_taken", status=409) from exc
    return get(workspace_id)


def assistant_state():
    return get(scope.current() or GENERAL)["assistant_state"]


def save_assistant_state(value):
    with database() as db:
        db.execute("UPDATE projects SET assistant_state=? WHERE id=?",
                   (json.dumps(value), scope.current() or GENERAL))


def session_home(session_id):
    if not enabled() or not session_id:
        return None

    entry = registry.read_session(session_id)
    key = runs.run_key_of_entry(entry) if entry else None
    if key:
        home = run_home(runs.run_key(*key))
        if home:
            return home["project_id"]
    with database() as db:
        row = db.execute("SELECT project_id FROM sessions WHERE session=?", (session_id,)).fetchone()
    return row[0] if row else None



def session_run(session_id):
    if not enabled() or not session_id:
        return None
    with database() as db:
        row = db.execute("SELECT run_key FROM sessions WHERE session=?", (session_id,)).fetchone()
    return row[0] if row else None


def bind_session(session_id, workspace_id, run_key=None):
    get(workspace_id, active=True)
    with database() as db:
        db.execute("INSERT INTO sessions VALUES (?,?,?)", (session_id, workspace_id, run_key))


def run_home(key):
    if not enabled():
        return None
    with database() as db:
        row = db.execute("SELECT * FROM homes WHERE run_key=?", (key,)).fetchone()
    return dict(row) if row else None


def bind_run(key, workspace_id):
    get(workspace_id, active=True)
    with database() as db:
        row = db.execute("SELECT project_id FROM homes WHERE run_key=?", (key,)).fetchone()
        if row and row[0] != workspace_id:
            raise ProjectError("This run belongs to another project.", code="project_changed", status=409)
        db.execute("INSERT OR IGNORE INTO homes (run_key,project_id) VALUES (?,?)", (key, workspace_id))


def remember_launch(key, slot, preset):
    with database() as db:
        db.execute("UPDATE homes SET launch=? WHERE run_key=?",
                   (json.dumps({"slot": slot, "preset": preset}), key))


def resume_selection(key, selected_slot=None):
    if not enabled():
        return {"slot": selected_slot} if selected_slot is not None else {}
    home = run_home(key)
    remembered = json.loads(home["launch"]) if home and home.get("launch") else {}
    slot = selected_slot if selected_slot is not None else remembered.get("slot")
    if slot is not None:
        return {"slot": slot}
    return {"preset": remembered.get("preset")}


def check_home(key, workspace_id, revision=None):
    home = run_home(key)
    if home and (home["project_id"] != workspace_id or revision is not None and home["revision"] != revision):
        raise ProjectError("The run's project changed. Reopen it in its home project.",
                           code="project_changed", status=409)
    return home


def key_of(data):
    """The run key a body, fleet row, event or registry block names, or ``None``.

    Composed through :func:`lmer_platform.runs.key_parts`, so a mapping that
    carries a uid binds to the uid key and one that does not to the slug key —
    the same rule the tracked index keys by. ``None`` rather than a refusal for a
    mapping that names no run: every caller here is asking "is this about a run",
    and most of what reaches them is not.
    """
    try:
        return runs.run_key(*runs.key_parts(data))
    except runs.RunIndexError:
        return None


def destination(data):
    """Resolve a recorded run/session, without repository or UI inference."""
    data = data or {}
    key = key_of(data)
    if key:
        home = run_home(key)
        if home:
            return home["project_id"]
    return session_home(data.get("session")) or data.get("workspace_id") or scope.current()


def _live_worker(key):
    """The live registry entry for *key*, or ``None``.

    A run with a container behind it is never reassigned or moved: the worker
    holds the old project's admission, and a move under it would split the
    session from its home.
    """
    for entry in registry.list_sessions(live_only=True):
        parts = runs.run_key_of_entry(entry)
        if parts and runs.run_key(*parts) == key:
            return entry
    return None


def _assign(key, destination_id):
    """Give a genuinely unowned run a home in an explicitly chosen project.

    Called only when ``homes`` has no row for *key*. A recorded home, even one
    in an archived project, is a transfer (see :func:`move`), never an adoption:
    this function must not be reachable for a run whose ownership is known.
    The caller has validated *destination_id* active and holds the operation
    lock. Ownership comes from the named destination alone.
    """
    # deferred: cycle — lmer_platform.assistant imports this module at top
    from . import assistant

    if _live_worker(key):
        raise ProjectError("Stop the worker before assigning this run.", code="run_active", status=409)
    with database() as db:
        db.execute("INSERT INTO homes (run_key,project_id) VALUES (?,?)", (key, destination_id))
        db.execute("UPDATE sessions SET project_id=? WHERE run_key=?", (destination_id, key))
        db.execute("UPDATE event_queue SET project_id=? WHERE done=0 AND (session IN "
                   "(SELECT session FROM sessions WHERE run_key=?) OR json_extract(payload,'$.data.run_key')=?)",
                   (destination_id, key, key))
        row = db.execute("SELECT assistant_state FROM projects WHERE id=?", (destination_id,)).fetchone()
        state = assistant.AssistantState.from_dict(json.loads(row[0]), memo_spool=destination_id)
        notice = assistant.PendingNote(
            at=store.utc_now_iso(), kind="run_assigned",
            note=f"Run {key} was assigned to this project.",
            data={"run_key": key, "workspace_id": destination_id, "home_revision": 1},
        )
        updated = replace(state, pending=(*state.pending, notice),
                          pending_since=state.pending_since or notice.at,
                          pending_seq=state.pending_seq + (not state.pending))
        db.execute("UPDATE projects SET assistant_state=? WHERE id=?", (json.dumps(updated.to_dict()), destination_id))
    logger.info("platform_run_assigned key=%s destination=%s", key, destination_id)
    return run_home(key)


def move(key, destination_id, *, expected_project, expected_revision=None):
    # deferred: cycle — lmer_platform.assistant imports this module at top
    from . import assistant

    with operation(), assistant._LOCK:
        get(destination_id, active=True)
        home = check_home(key, expected_project, expected_revision)
        if not home:
            # An unowned run is assigned, not moved: there is no source to
            # transfer from. ``expected_project`` is the destination because the
            # request layer made the destination the request's only scope.
            if expected_project != destination_id:
                raise ProjectError("An unassigned run can only be assigned to the project named "
                                   "as its destination.", status=409)
            return _assign(key, destination_id)
        if home["project_id"] == destination_id:
            return home
        if _live_worker(key):
            raise ProjectError("Stop the worker before moving this run.", code="run_active", status=409)
        with database() as db:
            states = {}
            for project_id in (expected_project, destination_id):
                row = db.execute("SELECT assistant_state FROM projects WHERE id=?", (project_id,)).fetchone()
                states[project_id] = assistant.AssistantState.from_dict(json.loads(row[0]), memo_spool=project_id)
            source, target = states[expected_project], states[destination_id]
            moved, retained = [], []
            for note in source.pending:
                data = note.data or {}
                session_run = db.execute("SELECT run_key FROM sessions WHERE session=?", (data.get("session"),)).fetchone()
                belongs = (key_of(data) == key or data.get("run_key") == key
                           or session_run and session_run[0] == key)
                (moved if belongs else retained).append(note)
            notice = assistant.PendingNote(
                at=store.utc_now_iso(), kind="run_moved", note=f"Run {key} moved here from project {expected_project}.",
                data={"run_key": key, "source_project": expected_project,
                      "workspace_id": destination_id, "home_revision": home["revision"] + 1},
            )
            updates = {
                expected_project: replace(source, pending=tuple(retained),
                                          **({} if retained else assistant.ended_episode(source))),
                destination_id: replace(target, pending=(*target.pending, *moved, notice),
                                        pending_since=target.pending_since or notice.at,
                                        pending_seq=target.pending_seq + (not target.pending)),
            }
            for project_id, state in updates.items():
                db.execute("UPDATE projects SET assistant_state=? WHERE id=?", (json.dumps(state.to_dict()), project_id))
            db.execute("UPDATE homes SET project_id=?,revision=revision+1,reconfirm=1 WHERE run_key=?",
                       (destination_id, key))
            db.execute("UPDATE sessions SET project_id=? WHERE run_key=?", (destination_id, key))
            db.execute("UPDATE event_queue SET project_id=? WHERE done=0 AND (session IN "
                       "(SELECT session FROM sessions WHERE run_key=?) OR json_extract(payload,'$.data.run_key')=?)",
                       (destination_id, key, key))
        return run_home(key)


def _present(row, *, skip):
    """A row's columns as :func:`runs.merge_values` reads them: NULL is "lacks"."""
    return {name: value for name, value in dict(row).items() if name not in skip and value is not None}


def _carry(db, table, where, old_args, new_args, old, new, *, rename):
    """Move one row of *table* to its new key, or merge it into the row already there.

    *old* and *new* are the two rows' non-key columns (:func:`_present`); *new*
    is ``None`` when nothing holds the new key, and the old row is then renamed
    in place with *rename*. Otherwise the new row gains only the columns it
    lacked, and the old row is removed only when nothing differed — a differing
    column leaves both rows standing, which is how the lossless policy keeps
    both values in a table whose key admits one row per run. A stamp column
    (``revision``, :data:`runs.BOOKKEEPING_STAMPS`) is not a difference: the
    new row takes the later one. Returns ``(moved, conflict)``.
    """
    if new is None:
        columns = ",".join(f"{name}=?" for name in rename)
        db.execute(f"UPDATE {table} SET {columns} WHERE {where}", (*rename.values(), *old_args))
        return (True, False)
    merged, conflict = runs.merge_values(old, new)
    carried = [
        name for name in merged
        if name not in new
        or (name in runs.BOOKKEEPING_STAMPS and merged[name] != new[name])
    ]
    for name in carried:
        db.execute(f"UPDATE {table} SET {name}=? WHERE {where}", (merged[name], *new_args))
    if not conflict:
        db.execute(f"DELETE FROM {table} WHERE {where}", old_args)
    return (bool(carried) or not conflict, conflict)


def rekey(old_key, new_key):
    """Carry a run's project ownership from *old_key* to *new_key* (spec §6.1).

    The transition that binds a slug-keyed run to its uid calls this for the
    ``projects`` and ``threads`` stores. It moves the ``homes`` row, the
    ``sessions`` rows' ``run_key`` and the durable thread ownership
    (``thread_owners.owner = run:<key>`` and ``thread_owners.run_key``) in one
    ``database()`` transaction under :func:`operation`, so a crash leaves either
    the old ownership or the new, never a home under one key with its sessions
    and threads under the other.

    Returns ``{"moved": bool, "conflict": bool}``. ``moved`` says whether this
    call changed any row; ``conflict`` whether a row under *new_key* holds a
    value that differs from the one under *old_key*. Both are ``False`` for a
    repeat call after a clean move, so the call is idempotent.

    Collisions follow :func:`runs.merge_values`. A column the new row lacks
    (NULL) is carried over; an equal one is a no-op and the old row is removed;
    a differing one is never dropped. For a home, a differing column (a
    different project) means the two keys disagree
    about who owns the run, so the old home stays in place with its sessions
    and thread owners still pointing at it: moving them would cross a project
    boundary that only :func:`move`, with its live-worker check and notices,
    may cross. The new home still gains what it lacked. A differing thread
    owner keeps both rows the same way. A conflict is reported again on every
    repeat until an operator resolves it.
    """
    if not enabled() or old_key == new_key:
        return {"moved": False, "conflict": False}
    moved = conflict = False
    with operation(), database() as db:
        old = db.execute("SELECT * FROM homes WHERE run_key=?", (old_key,)).fetchone()
        if old:
            new = db.execute("SELECT * FROM homes WHERE run_key=?", (new_key,)).fetchone()
            moved, conflict = _carry(db, "homes", "run_key=?", (old_key,), (new_key,),
                                     _present(old, skip={"run_key"}),
                                     _present(new, skip={"run_key"}) if new else None,
                                     rename={"run_key": new_key})
        if not conflict:
            moved = bool(db.execute("UPDATE sessions SET run_key=? WHERE run_key=?",
                                    (new_key, old_key)).rowcount) or moved
            old_owner = f"run:{old_key}"
            for row in db.execute("SELECT * FROM thread_owners WHERE owner=? OR run_key=?",
                                  (old_owner, old_key)).fetchall():
                owner = f"run:{new_key}" if row["owner"] == old_owner else row["owner"]
                rekeyed = {**dict(row), "run_key": new_key if row["run_key"] == old_key else row["run_key"]}
                # A row whose owner does not name the run (``project:<id>``) keeps
                # its primary key, so it cannot collide with anything but itself.
                target = None if owner == row["owner"] else db.execute(
                    "SELECT * FROM thread_owners WHERE thread=? AND owner=?", (row["thread"], owner)).fetchone()
                changed, clash = _carry(db, "thread_owners", "thread=? AND owner=?",
                                        (row["thread"], row["owner"]), (row["thread"], owner),
                                        _present(rekeyed, skip={"thread", "owner"}),
                                        _present(target, skip={"thread", "owner"}) if target else None,
                                        rename={"owner": owner, "run_key": rekeyed["run_key"]})
                moved, conflict = moved or changed, conflict or clash
    if moved or conflict:
        logger.info("platform_run_rekeyed old=%s new=%s moved=%s conflict=%s", old_key, new_key, moved, conflict)
    return {"moved": moved, "conflict": conflict}


def archive(workspace_id, inventory):
    with operation():
        project = get(workspace_id, active=True)
        if project["status"] in ("waiting", "starting", "rotating", "stopping") or project["assistant_state"].get("pending"):
            raise ProjectError("Resolve pending assistant work before archiving.", status=409)
        with database() as db:
            pending = db.execute("SELECT 1 FROM requests WHERE project_id=? AND state IN "
                                 "('choice','queued','delivered','reply_sending','reply_uncertain') LIMIT 1",
                                 (workspace_id,)).fetchone()
            events = db.execute("SELECT 1 FROM event_queue WHERE project_id=? AND done=0 LIMIT 1", (workspace_id,)).fetchone()
            actions = db.execute("SELECT 1 FROM request_actions a JOIN requests r ON r.id=a.request_id "
                                 "WHERE r.project_id=? AND a.state IN ('sending','uncertain') LIMIT 1",
                                 (workspace_id,)).fetchone()
        if pending or events or actions:
            raise ProjectError("Resolve pending requests and events before archiving.", status=409)
        rows = {}
        for item in inventory.get("runs", []):
            key = key_of(item)
            if key:
                rows[key] = item
        for run in runs.list_tracked():
            home = run_home(run.key)
            if home and home["project_id"] == workspace_id:
                row = rows.get(run.key)
                if not row or row.get("state") != "complete" or row.get("attention"):
                    raise ProjectError("Finish the project's runs and questions before archiving.", status=409)
        for entry in registry.list_sessions(live_only=True):
            if entry.get("kind") != "assistant" and session_home(entry["id"]) == workspace_id:
                raise ProjectError("Stop the project's workers before archiving.", status=409)
        stopped = stop(workspace_id)
        if stopped["running"]:
            raise ProjectError("The assistant is still stopping.", status=409)
        with database() as db:
            db.execute("UPDATE projects SET archived=1 WHERE id=?", (workspace_id,))
    return get(workspace_id)


def restore(workspace_id):
    get(workspace_id)
    with operation(), database() as db:
        db.execute("UPDATE projects SET archived=0,desired='stopped',status='stopped' WHERE id=? AND archived=1", (workspace_id,))
    return get(workspace_id)


def limit():
    # deferred: cycle — lmer_platform.config imports this module at top (via lmer_platform.uploads)
    from .config import config_path

    value = (store.read_json(config_path()) or {}).get("max_live_assistants", 4)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else 4


def worker_limit(default):
    # deferred: cycle — lmer_platform.config imports this module at top (via lmer_platform.uploads)
    from .config import config_path

    value = (store.read_json(config_path()) or {}).get("max_concurrent_sessions", default)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else default


def request_start(workspace_id):
    get(workspace_id, active=True)
    with operation(), database() as db:
        row = db.execute("SELECT desired,status FROM projects WHERE id=?", (workspace_id,)).fetchone()
        if row[0] == "running" and row[1] in ("running", "starting", "waiting", "rotating"):
            return
        db.execute("UPDATE projects SET desired='running',status='waiting',error=NULL,retries=0,retry_at=0 WHERE id=?",
                   (workspace_id,))
        db.execute("INSERT OR IGNORE INTO starts (project_id) VALUES (?)", (workspace_id,))


def status(workspace_id):
    # deferred: cycle — lmer_platform.assistant imports this module at top
    from . import assistant

    project = get(workspace_id)
    with scope.bound(workspace_id):
        result = assistant.status().to_dict()
    with database() as db:
        queue = [row[0] for row in db.execute("SELECT project_id FROM starts ORDER BY ticket")]
    result.update(workspace_id=workspace_id, project_name=project["name"], color=project["color"],
                  desired=project["desired"], lifecycle=project["status"], error=project["error"],
                  queue_position=queue.index(workspace_id) + 1 if workspace_id in queue else None)
    if project["status"] == "running" and not result["running"]:
        result.update(lifecycle="failed", error=project["error"] or "Assistant exited unexpectedly.")
    return result


def inventory_sessions(sessions):
    """Omit retired assistant generations from the fleet, retaining their records."""
    if not enabled():
        return sessions
    owners = {row["id"]: row for row in listing()}
    visible = []
    for entry in sessions:
        if entry.get("kind") == registry.ASSISTANT_KIND and not entry.get("live"):
            project = owners.get(session_home(entry["id"]))
            if project:
                current = project["assistant_state"].get("session_id")
                if not current:
                    with scope.bound(project["id"]):
                        latest = assistant_history.latest()
                    current = latest["session"] if latest else None
                if current == entry["id"] and project["status"] in ("starting", "rotating"):
                    continue
                if current and current != entry["id"]:
                    previous = assistant_history.generation(entry["id"])
                    head = assistant_history.generation(current)
                    if (previous and head and previous["conversation"] == head["conversation"]
                            and previous["generation"] < head["generation"]):
                        continue
        visible.append(entry)
    return visible


def stop(workspace_id):
    # deferred: cycle — lmer_platform.assistant imports this module at top
    from . import assistant

    get(workspace_id)
    with operation(), scope.bound(workspace_id), admitting(workspace_id):
        with database() as db:
            db.execute("DELETE FROM starts WHERE project_id=?", (workspace_id,))
            db.execute("UPDATE projects SET desired='stopped',status='stopping',error=NULL WHERE id=?", (workspace_id,))
        assistant.stop()
        live = assistant.status().running
        with database() as db:
            db.execute("UPDATE projects SET status=?,reserved=? WHERE id=?",
                       ("stopping" if live else "stopped", live, workspace_id))
    return status(workspace_id)


def rotate(workspace_id, config, **kwargs):
    # deferred: cycle — lmer_platform.assistant and lmer_platform.project_requests import this module at top
    from . import assistant, project_requests

    get(workspace_id, active=True)
    with operation(), scope.bound(workspace_id):
        if not assistant.status().running:
            request_start(workspace_id)
            reconcile(config)
            return status(workspace_id)
        with database() as db:
            db.execute("UPDATE projects SET status='rotating',desired='running',reserved=1 WHERE id=?", (workspace_id,))
        try:
            with admitting(workspace_id):
                assistant.rotate(config, **kwargs)
        except Exception as exc:
            _failed(workspace_id, str(exc), retry=False)
            raise
        with database() as db:
            db.execute("UPDATE projects SET status='running',error=NULL WHERE id=?", (workspace_id,))
        project_requests.recover(workspace_id)
    return status(workspace_id)


def _failed(workspace_id, error, *, retry):
    # deferred: cycle — lmer_platform.transcripts imports this module at top
    from .transcripts import scrub_credentials

    with database() as db:
        row = db.execute("SELECT retries FROM projects WHERE id=?", (workspace_id,)).fetchone()
        attempts = row[0] + 1
        retry = retry and attempts < 3
        db.execute("UPDATE projects SET status=?,error=?,retries=?,retry_at=?,reserved=0 WHERE id=?",
                   ("waiting" if retry else "failed", scrub_credentials(error), attempts,
                    time.time() + min(30, 2 ** attempts), workspace_id))
        if not retry:
            db.execute("DELETE FROM starts WHERE project_id=?", (workspace_id,))


def reconcile(config):
    """Reconcile one fleet pass; reservations include starts and rotations."""
    # deferred: cycle — lmer_platform.assistant and lmer_platform.project_requests import this module at top
    from . import assistant, project_requests

    with operation():
        for project in listing(archived=False):
            workspace_id = project["id"]
            with scope.bound(workspace_id):
                live = assistant._live_assistant()
            if live:
                if project["desired"] == "stopped":
                    stop(workspace_id)
                    continue
                with database() as db:
                    db.execute("UPDATE projects SET status=?,reserved=1 WHERE id=?",
                               ("running" if project["desired"] == "running" else "stopping", workspace_id))
                    db.execute("DELETE FROM starts WHERE project_id=?", (workspace_id,))
                    age = store.age_seconds(project["assistant_state"].get("started_at"))
                    if age is not None and age >= assistant.SETTLED_SECONDS:
                        db.execute("UPDATE projects SET retries=0,retry_at=0 WHERE id=?", (workspace_id,))
                project_requests.recover(workspace_id)
            elif project["status"] in ("running", "starting", "rotating"):
                if project["desired"] == "running":
                    with database() as db:
                        db.execute("INSERT OR IGNORE INTO starts (project_id) VALUES (?)", (workspace_id,))
                    _failed(workspace_id, "Assistant exited unexpectedly.", retry=True)
            elif project["desired"] == "stopped":
                with database() as db:
                    db.execute("UPDATE projects SET status='stopped',reserved=0 WHERE id=?", (workspace_id,))
        while True:
            with database() as db:
                used = db.execute("SELECT count(*) FROM projects WHERE reserved=1").fetchone()[0]
                row = db.execute("SELECT p.* FROM starts s JOIN projects p ON p.id=s.project_id "
                                 "WHERE p.desired='running' AND p.archived=0 ORDER BY s.ticket LIMIT 1").fetchone()
                if used >= limit() or row is None or row["retry_at"] > time.time():
                    return
                workspace_id = row["id"]
                db.execute("UPDATE projects SET status='starting',reserved=1 WHERE id=?", (workspace_id,))
            try:
                with scope.bound(workspace_id), admitting(workspace_id):
                    assistant.start(config)
            except Exception as exc:
                _failed(workspace_id, str(exc), retry=bool(row["retries"]))
                continue
            with database() as db:
                db.execute("UPDATE projects SET status='running',error=NULL WHERE id=?", (workspace_id,))
                db.execute("DELETE FROM starts WHERE project_id=?", (workspace_id,))
            project_requests.recover(workspace_id)


class Supervisor:
    def __init__(self, config):
        self.config = config
        self._stop = threading.Event()
        self._thread = None

    def first_start(self):
        # deferred: cycle — lmer_platform.assistant imports this module at top, and lmer_platform.config does via uploads
        from . import assistant, config

        incumbents = {row["id"] for row in listing() if status(row["id"])["running"]}
        reconcile(self.config)
        notices = []
        for row in listing(archived=False):
            if row["desired"] == "stopped":
                notices.append(f"{row['name']}: assistant intentionally stopped.")
                continue
            with scope.bound(row["id"]):
                current = assistant.status()
                if not current.running:
                    notices.append(
                        f"{row['name']} ({row['id']}): Assistant NOT running: {row['error'] or row['status']}. "
                        f"Use POST /api/assistant/start with workspace_id={row['id']} to request a start."
                    )
                    continue
                destination = config.container_base_url(self.config)
                report = assistant.SupervisionReport(
                    running=current.running, session_id=current.session_id, generation=current.generation,
                    adopted=row["id"] in incumbents, reachable=destination.reachable,
                    reason=destination.reason, error=row["error"] or row["status"],
                    slots=self.config.max_concurrent_sessions,
                )
                notices.append(f"{row['name']} ({row['id']}): {report.notice}")
        return "\n".join(notices)

    def start(self):
        self._thread = threading.Thread(target=self.run, name="project-assistants", daemon=True)
        self._thread.start()
        return self._thread

    def run(self):
        while not self._stop.is_set():
            try:
                reconcile(self.config)
            except Exception:
                logger.exception("project_assistant_reconcile_failed")
            self._stop.wait(2)

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join()
