"""Fleet reads look projects up once, not once per row (#561).

``project_api.annotate`` stamped every fleet row with its project by asking the
database again for each one, and ``projects.inventory_sessions`` did the same for
every retired assistant generation — under the lock ``GET /api/assistant`` waits
on. These tests pin the replacement: the same answers, from one snapshot, with a
connection count that does not grow with the rows.
"""

import sqlite3
import threading

import pytest

from lmer_platform import assistant_history, project_api, projects, registry, runs, store
from lmer_platform import project_context as scope

HOST, PROJECT = "git.example.com", "group/widgets"


@pytest.fixture
def platform(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    projects.migrate()
    return tmp_path


def _key(slug):
    return runs.run_key(*runs.key_parts({"host": HOST, "project": PROJECT, "slug": slug}))


def _row(slug):
    return {"host": HOST, "project": PROJECT, "slug": slug, "state": "in-progress", "live": False}


def _fixture_payload(count):
    """Homed runs in two projects, session-only rows, and rows nobody owns."""
    beta = projects.create("Beta")["id"]
    archived = projects.create("Gamma")["id"]
    rows, sessions = [], []
    for number in range(count):
        slug = f"run-{number}"
        home = (projects.GENERAL, beta, archived, None)[number % 4]
        if home:
            projects.bind_run(_key(slug), home)
        rows.append(_row(slug))
        session_id = f"20261111-000000-{number}-row"
        if number % 3 == 0:
            projects.bind_session(session_id, beta)
        sessions.append({"id": session_id, "live": False})
    with projects.database() as db:
        db.execute("UPDATE projects SET archived=1 WHERE id=?", (archived,))
    return beta, {
        "runs": rows, "sessions": sessions, "attention": rows[:3],
        "tracked": {"runs": rows, "count": len(rows)},
        "relations": [{"a": _key("run-0"), "b": _key("run-4")}, {"a": _key("run-1"), "b": _key("run-5")}],
        "totals": {}, "counts": {},
    }


def _reference_annotate(payload, workspace_id=None):
    """The annotation as it was computed before #561: per row, per helper."""
    result = dict(payload)
    for collection in ("runs", "sessions", "attention"):
        rows = []
        for original in result[collection]:
            row = dict(original)
            key = projects.key_of(row)
            home = projects.run_home(key) if key else None
            session = row.get("session")
            session_id = session.get("id") if isinstance(session, dict) else row.get("id") or session
            owner = home["project_id"] if home else projects.session_home(session_id)
            row["workspace_id"] = owner
            row["home_revision"] = home["revision"] if home else None
            row["confirm_project"] = bool(home and home["reconfirm"])
            if owner:
                project = projects.get(owner)
                row["workspace_name"], row["workspace_color"] = project["name"], project["color"]
            if workspace_id is None or owner == workspace_id:
                rows.append(row)
        result[collection] = rows
    if workspace_id is not None:
        if isinstance(result.get("tracked"), dict):
            tracked = _reference_annotate({"runs": result["tracked"]["runs"], "sessions": [], "attention": []},
                                          workspace_id)["runs"]
            result["tracked"] = {"runs": tracked, "count": len(tracked)}
        if "relations" in result:
            result["relations"] = [edge for edge in result["relations"] if all(
                (projects.run_home(edge.get(end)) or {}).get("project_id") == workspace_id
                for end in ("a", "b"))]
        if "totals" in result:
            result["totals"] = {"runs": len(result["runs"]),
                                "live": sum(bool(row.get("live")) for row in result["runs"]),
                                "attention": len(result["attention"])}
        if "counts" in result:
            counts = {}
            for row in result["runs"]:
                counts[row.get("state")] = counts.get(row.get("state"), 0) + 1
            result["counts"] = counts
    return result


def test_annotations_match_the_per_row_helpers(platform):
    beta, payload = _fixture_payload(12)
    for workspace_id in (None, projects.GENERAL, beta):
        assert project_api.annotate(payload, workspace_id=workspace_id) == \
            _reference_annotate(payload, workspace_id), workspace_id


def test_annotate_never_mutates_the_payload_it_was_given(platform):
    _, payload = _fixture_payload(6)
    before = repr(payload)
    project_api.annotate(payload, workspace_id=projects.GENERAL)
    assert repr(payload) == before


def _count_connections(monkeypatch):
    opened = []
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: opened.append(1) or real(*a, **k))
    return opened


@pytest.mark.parametrize("workspace_id", [None, projects.GENERAL])
def test_annotate_opens_the_same_connections_for_five_rows_and_two_hundred(platform, monkeypatch,
                                                                          workspace_id):
    counts = []
    for size in (5, 200):
        _, payload = _fixture_payload(size)
        projects.enabled()  # settle the cached answer, as a running daemon has
        opened = _count_connections(monkeypatch)
        project_api.annotate(payload, workspace_id=workspace_id)
        counts.append(len(opened))
        monkeypatch.undo()
        monkeypatch.setattr(store, "PLATFORM_DIR", str(platform / f"fresh-{size}"))
        projects.migrate()
    assert counts[0] == counts[1] <= 2


def _retired_generations(count):
    """*count* generations of one conversation in General; only the last is current."""
    previous = None
    ids = []
    with scope.bound(projects.GENERAL):
        for number in range(1, count + 1):
            session_id = f"20261110-000000-{number}-gen"
            registry.register(session_id, kind=registry.ASSISTANT_KIND, pid=3_000_000 + number,
                              started_at=f"2026-11-10T00:00:{number:02d}Z")
            projects.bind_session(session_id, projects.GENERAL)
            assistant_history.register(session_id, number=number, started_at=None, previous=previous)
            previous = session_id
            ids.append(session_id)
        projects.save_assistant_state({"session_id": ids[-1], "generation": count})
    return ids


@pytest.mark.parametrize("count", [3, 30])
def test_inventory_sessions_hides_retired_generations(platform, count):
    ids = _retired_generations(count)
    visible = [entry["id"] for entry in projects.inventory_sessions(registry.list_sessions(live_only=False))]
    assert visible == [ids[-1]]


def test_inventory_sessions_opens_the_same_connections_however_many_generations_retired(platform,
                                                                                    monkeypatch):
    counts = []
    for count in (3, 30):
        monkeypatch.setattr(store, "PLATFORM_DIR", str(platform / f"fleet-{count}"))
        projects.migrate()
        _retired_generations(count)
        sessions = registry.list_sessions(live_only=False)
        projects.enabled()
        opened = _count_connections(monkeypatch)
        projects.inventory_sessions(sessions)
        counts.append(len(opened))
        monkeypatch.undo()
    assert counts[0] == counts[1]


def test_a_snapshot_never_sees_a_move_half_done(platform):
    beta = projects.create("Beta")["id"]
    key, session_id = _key("moving"), "20261111-000000-9-moving"
    projects.bind_run(key, projects.GENERAL)
    projects.bind_session(session_id, projects.GENERAL)
    stop = threading.Event()

    moves = []

    def mover():
        flip = True
        while not stop.is_set():
            target = beta if flip else projects.GENERAL
            with projects.database() as db:
                db.execute("UPDATE homes SET project_id=? WHERE run_key=?", (target, key))
                db.execute("UPDATE sessions SET project_id=? WHERE session=?", (target, session_id))
            moves.append(target)
            flip = not flip
            # Paced, at still far above any real writer's rate. In rollback-journal
            # mode each commit briefly holds PENDING/EXCLUSIVE, and a reader backs
            # off up to 100 ms between retries: at 2 ms a snapshot took 0.4–3 s on
            # one core and once exceeded the 30 s busy timeout ("database is
            # locked"). That measures reader starvation, not consistency.
            stop.wait(0.02)

    thread = threading.Thread(target=mover)
    thread.start()
    try:
        for _ in range(40):
            snap = projects.snapshot()
            assert snap.run_home(key)["project_id"] == snap.session_home(session_id)
    finally:
        stop.set()
        thread.join(10)
    assert moves, "the mover never ran, so nothing was checked"
