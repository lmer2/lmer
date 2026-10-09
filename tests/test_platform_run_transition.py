"""The key transition of slug-keyed entries for schema-2 groups (spec §6.1, astra R3).

A daemon upgraded onto an index written before uids existed holds entries keyed
``host/project/<slug>`` whose groups hold schema-2 records. The first state build
binds each one to its record when that is provable, or marks it unresolved —
never "the newest record in the group" — and the carry is durable: a crash after
any store resumes the rest at the next build without moving a store twice.
"""

import os
import socket

import pytest
import yaml

from lmer_platform import (
    api, checkin, meta, projects, references, registry, relations, run_transition, runs,
    store,
)
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env

HOST, PROJECT = "gitlab.example.com", "acme/widgets"
SLUG = "chat"
UID_A = "a" * 32
UID_B = "b" * 32
SLUG_KEY = runs.run_key(HOST, PROJECT, SLUG)
KEY_A = runs.run_key(HOST, PROJECT, UID_A)
KEY_B = runs.run_key(HOST, PROJECT, UID_B)
#: A record written before execution uids: a timestamp session id.
LEGACY_SESSION = "20260824-165230-2-31245"
SESSION = "s-20260801T000000-abcd"
THREAD = "https://example.slack.com/archives/C0123ABCDEF/p1712345678000900"


@pytest.fixture
def config(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    config = cfg.load()
    config.mirror_path.mkdir(parents=True, exist_ok=True)
    (config.mirror_path / "_plexus.yaml").write_text(
        yaml.safe_dump({
            "schema": 2, "name": "example-plexus",
            "projects": {"widgets": {"repo": "ssh://git@gitlab.example.com/acme/widgets.git"}},
        }),
        encoding="utf-8",
    )
    return config


def _record(config, dir_name, started_at, *, slug=SLUG, uid=None, name=None, launcher=None):
    path = config.mirror_path / "widgets" / "_runs" / slug / dir_name
    path.mkdir(parents=True, exist_ok=True)
    session = {"started_at": started_at}
    if uid:
        session["uid"] = uid
    if launcher:
        session["launcher"] = launcher
    state = {"schema": 2 if uid else 1, "slug": slug, "status": "in-progress",
             "goal": f"record {dir_name}", "session": session}
    if name:
        state["name"] = name
    (path / "state.yaml").write_text(yaml.safe_dump(state), encoding="utf-8")
    return path


def _uid_record(config, uid, started_at, **kwargs):
    return _record(config, uid, started_at, uid=uid, **kwargs)


def _persist_index(*entries):
    """An index as a pre-A2 daemon left it: slug keys, no uid."""
    payload = {}
    for entry in entries:
        payload[runs.run_key(entry["host"], entry["project"], entry.get("uid") or entry["slug"])] = {
            "source": "spawned", "first_seen": "2026-07-01T00:00:00Z",
            "last_seen": "2026-07-01T00:00:00Z", **entry,
        }
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"runs": payload})


def _slug_entry(**extra):
    return {"host": HOST, "project": PROJECT, "slug": SLUG, "taskdef": "chat", **extra}


def _index():
    return runs.load_index()


def _index_bytes():
    return store.snapshot_path(runs.RUNS_FILE).read_bytes()


def _rows(payload):
    return [row for row in payload["runs"] if row.get("slug") == SLUG]


# --- unresolved --------------------------------------------------------------


def test_two_records_in_one_unchanged_group_stay_unresolved_with_both_candidates(config):
    _uid_record(config, UID_A, "2026-01-01T00:00:00+00:00", name="first")
    _uid_record(config, UID_B, "2026-02-01T00:00:00+00:00")
    _persist_index(_slug_entry())

    payload = api.build_state(config)

    entry = _index()[SLUG_KEY]
    assert entry.get("uid") is None
    assert entry["unresolved"] == {"candidates": [
        {"uid": UID_A, "started_at": "2026-01-01T00:00:00+00:00", "name": "first"},
        {"uid": UID_B, "started_at": "2026-02-01T00:00:00+00:00", "name": None},
    ]}
    (row,) = _rows(payload)
    # Resolved to no record at all — the newest would be a guess.
    assert row["state"] == "dormant" and row["rel_path"] is None
    assert row["uid"] is None
    assert row["readopt"]["reason"] == runs.READOPT_BY_UID
    assert [c["uid"] for c in row["readopt"]["candidates"]] == [UID_A, UID_B]


def test_a_mixed_group_stays_unresolved(config):
    """'Exactly one schema-2 record' does not say which execution the old row meant."""
    _record(config, LEGACY_SESSION, "2026-01-01T00:00:00+00:00")
    _uid_record(config, UID_A, "2026-02-01T00:00:00+00:00")
    _persist_index(_slug_entry())

    payload = api.build_state(config)

    entry = _index()[SLUG_KEY]
    assert entry.get("uid") is None
    assert [c["uid"] for c in entry["unresolved"]["candidates"]] == [UID_A]
    (row,) = _rows(payload)
    assert row["readopt"]["reason"] == runs.READOPT_BY_UID
    assert row["rel_path"] is None


def test_a_launcher_match_binds_one_record_of_two(config):
    registry.register(SESSION, pid=4242, run={"host": HOST, "project": PROJECT, "slug": SLUG},
                      started_at="2026-02-01T00:00:00Z")
    _uid_record(config, UID_A, "2026-01-01T00:00:00+00:00",
                launcher={"pid": 4242, "hostname": socket.gethostname(),
                          "started_at": "2026-01-01T00:00:00+00:00"})
    _uid_record(config, UID_B, "2026-02-01T00:00:05+00:00",
                launcher={"pid": 4242, "hostname": socket.gethostname(),
                          "started_at": "2026-02-01T00:00:05+00:00"})
    _persist_index(_slug_entry(last_session_id=SESSION))

    summary = run_transition.reconcile(config)

    # UID_A shares the pid but launched long before the session registered: a
    # recycled pid, not this execution.
    assert summary["bound"] == [SLUG_KEY]
    assert set(_index()) == {KEY_B}
    assert registry.read_session(SESSION)["run"]["uid"] == UID_B


def test_a_registry_entry_that_contradicts_the_only_record_leaves_it_unresolved(config):
    registry.register(SESSION, pid=4242, run={"host": HOST, "project": PROJECT, "slug": SLUG},
                      started_at="2026-02-01T00:00:00Z")
    _uid_record(config, UID_A, "2026-01-01T00:00:00+00:00",
                launcher={"pid": 999, "hostname": socket.gethostname(),
                          "started_at": "2026-01-01T00:00:00+00:00"})
    _persist_index(_slug_entry(last_session_id=SESSION))

    run_transition.reconcile(config)

    assert set(_index()) == {SLUG_KEY}
    assert [c["uid"] for c in _index()[SLUG_KEY]["unresolved"]["candidates"]] == [UID_A]


def test_launcher_verdict_proves_nothing_without_a_registry_entry():
    entry = runs.TrackedRun(host=HOST, project=PROJECT, slug=SLUG)
    state = {"session": {"launcher": {"pid": 1, "hostname": socket.gethostname()}}}
    assert run_transition.launcher_verdict(state, None, entry) is None
    other_run = {"pid": 1, "run": {"host": HOST, "project": PROJECT, "slug": "other"}}
    assert run_transition.launcher_verdict(state, other_run, entry) is None
    ours = {"pid": 1, "run": {"host": HOST, "project": PROJECT, "slug": SLUG}}
    assert run_transition.launcher_verdict({"session": {}}, ours, entry) is None
    assert run_transition.launcher_verdict(state, {**ours, "pid": 2}, entry) is False


def _launched(started_at, pid=42):
    return {"session": {"launcher": {"pid": pid, "hostname": socket.gethostname(),
                                     "started_at": started_at}}}


_REGISTERED = {"pid": 42, "started_at": "2026-02-01T00:00:00Z",
               "run": {"host": HOST, "project": PROJECT, "slug": SLUG}}


def test_launcher_verdict_matches_only_within_the_clock_skew_both_ways():
    entry = runs.TrackedRun(host=HOST, project=PROJECT, slug=SLUG)
    verdict = run_transition.launcher_verdict

    assert verdict(_launched("2026-02-01T00:00:05+00:00"), _REGISTERED, entry) is True
    assert verdict(_launched("2026-01-31T23:59:30+00:00"), _REGISTERED, entry) is True
    # A later launch that drew the same recycled pid is another execution.
    assert verdict(_launched("2026-03-03T00:00:00+00:00"), _REGISTERED, entry) is False
    assert verdict(_launched("2026-01-01T00:00:00+00:00"), _REGISTERED, entry) is False


@pytest.mark.parametrize("launched, registered", [
    (None, "2026-02-01T00:00:00Z"),
    ("garbage", "2026-02-01T00:00:00Z"),
    ("2026-02-01T00:00:05+00:00", None),
    ("2026-02-01T00:00:05+00:00", "garbage"),
    (None, None),
])
def test_launcher_verdict_never_matches_on_pid_and_hostname_alone(launched, registered):
    entry = runs.TrackedRun(host=HOST, project=PROJECT, slug=SLUG)
    state = _launched(launched)
    if launched is None:
        del state["session"]["launcher"]["started_at"]
    session = {**_REGISTERED, "started_at": registered}

    assert run_transition.launcher_verdict(state, session, entry) is None


def test_a_later_launch_with_a_recycled_pid_is_not_bound_as_the_only_record(config):
    registry.register(SESSION, pid=42, run={"host": HOST, "project": PROJECT, "slug": SLUG},
                      started_at="2026-02-01T00:00:00Z")
    _uid_record(config, UID_A, "2026-03-03T00:00:00+00:00",
                launcher={"pid": 42, "hostname": socket.gethostname(),
                          "started_at": "2026-03-03T00:00:00+00:00"})
    _persist_index(_slug_entry(last_session_id=SESSION))

    run_transition.reconcile(config)

    assert set(_index()) == {SLUG_KEY}
    assert [c["uid"] for c in _index()[SLUG_KEY]["unresolved"]["candidates"]] == [UID_A]
    assert registry.read_session(SESSION)["run"].get("uid") is None


# --- bound ---------------------------------------------------------------------


@pytest.fixture
def home(config):
    projects.migrate()
    return projects.create("Alpha")["id"]


def test_one_record_is_bound_with_everything_carried(config, home):
    _uid_record(config, UID_A, "2026-01-01T00:00:00+00:00")
    _persist_index(_slug_entry(), {"host": HOST, "project": PROJECT, "slug": "other"})
    meta.write(HOST, PROJECT, SLUG, title="the chat", description="about widgets")
    relations.relate((HOST, PROJECT, SLUG), (HOST, PROJECT, "other"))
    checkin.record_check(HOST, PROJECT, SLUG)
    references.add(HOST, PROJECT, SLUG, THREAD, title="the design thread")
    projects.bind_run(SLUG_KEY, home)
    with projects.database() as db:
        db.execute("INSERT INTO thread_owners VALUES (?,?,?,?)",
                   ("t-1", f"run:{SLUG_KEY}", home, SLUG_KEY))
    marks_before = checkin.read_marks()[SLUG_KEY]

    summary = run_transition.reconcile(config)

    assert summary["bound"] == [SLUG_KEY]
    index = _index()
    assert SLUG_KEY not in index
    assert index[KEY_A]["uid"] == UID_A and index[KEY_A]["slug"] == SLUG
    assert index[KEY_A]["aliases"] == [SLUG_KEY]
    assert "transition" not in index[KEY_A] and "conflict" not in index[KEY_A]
    assert meta.read(HOST, PROJECT, UID_A).title == "the chat"
    assert SLUG_KEY not in meta.load_all()
    (pair,) = relations.load_all().values()
    assert sorted(ref["slug"] for ref in pair["runs"]) == sorted([UID_A, "other"])
    assert checkin.read_marks()[KEY_A] == marks_before
    assert SLUG_KEY not in checkin.read_marks()
    assert [r["title"] for r in references.load_all()[KEY_A]] == ["the design thread"]
    assert projects.run_home(KEY_A)["project_id"] == home
    assert projects.run_home(SLUG_KEY) is None
    with projects.database() as db:
        owners = [tuple(row) for row in db.execute("SELECT thread,owner,run_key FROM thread_owners")]
    assert owners == [("t-1", f"run:{KEY_A}", KEY_A)]
    # A stale client reference still resolves, through the alias.
    stale = runs.get_tracked(HOST, PROJECT, SLUG)
    assert stale is not None and stale.uid == UID_A and stale.key == KEY_A


def test_a_bound_row_shows_its_own_record(config):
    _uid_record(config, UID_A, "2026-01-01T00:00:00+00:00")
    _persist_index(_slug_entry())

    payload = api.build_state(config)

    (row,) = _rows(payload)
    assert row["uid"] == UID_A
    assert row["rel_path"] == f"widgets/_runs/{SLUG}/{UID_A}"
    assert row["readopt"] is None


def test_note_session_through_the_alias_keeps_the_uid_key(config):
    _uid_record(config, UID_A, "2026-01-01T00:00:00+00:00")
    _persist_index(_slug_entry())
    run_transition.reconcile(config)

    runs.note_session(HOST, PROJECT, SLUG, "s-later")

    assert set(_index()) == {KEY_A}
    assert _index()[KEY_A]["last_session_id"] == "s-later"


def test_an_upgrade_with_a_live_old_style_registry_entry_ends_with_one_joined_row(config):
    registry.register(SESSION, pid=os.getpid(),
                      run={"host": HOST, "project": PROJECT, "slug": SLUG},
                      started_at="2026-08-01T00:00:00Z")
    _uid_record(config, UID_A, "2026-08-01T00:00:02+00:00",
                launcher={"pid": os.getpid(), "hostname": socket.gethostname(),
                          "started_at": "2026-08-01T00:00:02+00:00"})
    _persist_index(_slug_entry(last_session_id=SESSION))

    payload = api.build_state(config)

    rows = _rows(payload)
    assert len(rows) == 1, rows
    (row,) = rows
    assert row["uid"] == UID_A
    assert row["rel_path"] == f"widgets/_runs/{SLUG}/{UID_A}"
    assert row["session"]["id"] == SESSION
    assert registry.read_session(SESSION)["run"] == {
        "host": HOST, "project": PROJECT, "slug": SLUG, "uid": UID_A,
    }


# --- durable and resumable -------------------------------------------------------

_FAULT_POINTS = {
    "meta": (meta, "rekey"),
    "relations": (relations, "rekey"),
    "checkin": (checkin, "rekey"),
    "references": (references, "rekey"),
    "projects": (projects, "rekey"),
    "registry": (run_transition, "_canonicalize"),
}


def _count_calls(monkeypatch, *, fail_once=None):
    """Wrap every store's mover; *fail_once* raises on that store's first call."""
    calls = {name: {"tried": 0, "moved": 0} for name in _FAULT_POINTS}
    for name, (module, attr) in _FAULT_POINTS.items():
        original = getattr(module, attr)

        def wrapped(*args, _name=name, _original=original, **kwargs):
            calls[_name]["tried"] += 1
            if _name == fail_once and calls[_name]["tried"] == 1:
                raise RuntimeError(f"injected fault in {_name}")
            result = _original(*args, **kwargs)
            calls[_name]["moved"] += 1
            return result

        monkeypatch.setattr(module, attr, wrapped)
    return calls


@pytest.mark.parametrize("failing", list(_FAULT_POINTS))
def test_a_fault_after_each_store_resumes_at_the_next_build(config, monkeypatch, failing):
    _uid_record(config, UID_A, "2026-01-01T00:00:00+00:00")
    _persist_index(_slug_entry())
    meta.write(HOST, PROJECT, SLUG, title="the chat")
    calls = _count_calls(monkeypatch, fail_once=failing)

    api.build_state(config)

    transition = _index()[KEY_A]["transition"]
    assert transition["state"] == "pending"
    order = list(runs.TRANSITION_STORES)
    failed_at = order.index(failing)
    for store_name in order[:failed_at]:
        assert transition["stores"][store_name] == "done", store_name
    for store_name in order[failed_at:]:
        assert transition["stores"][store_name] == "pending", store_name
    # The alias already resolves: the entry moved with the record of the transition.
    assert runs.get_tracked(HOST, PROJECT, SLUG).uid == UID_A

    api.build_state(config)

    assert "transition" not in _index()[KEY_A]
    assert meta.read(HOST, PROJECT, UID_A).title == "the chat"
    for name, count in calls.items():
        assert count["moved"] == 1, (name, count)
    assert calls[failing]["tried"] == 2


def test_a_second_build_is_a_no_op(config, monkeypatch):
    _uid_record(config, UID_A, "2026-01-01T00:00:00+00:00")
    _persist_index(_slug_entry())
    api.build_state(config)
    before = _index_bytes()
    calls = _count_calls(monkeypatch)
    group_reads = []
    monkeypatch.setattr(run_transition.workrepo, "group_records",
                        lambda *a, **k: group_reads.append(a) or [])

    api.build_state(config)

    assert _index_bytes() == before
    assert all(count["tried"] == 0 for count in calls.values())
    assert group_reads == [], "a bound index must not walk the mirror"


def test_an_unresolved_entry_is_not_rewritten_by_the_next_build(config):
    _uid_record(config, UID_A, "2026-01-01T00:00:00+00:00")
    _uid_record(config, UID_B, "2026-02-01T00:00:00+00:00")
    _persist_index(_slug_entry())
    api.build_state(config)
    before = _index_bytes()

    api.build_state(config)

    assert _index_bytes() == before


# --- collisions and schema 1 ----------------------------------------------------


def test_a_differing_value_collision_keeps_both_values_and_marks_the_entry(config):
    _uid_record(config, UID_A, "2026-01-01T00:00:00+00:00")
    # The record was adopted by uid too, and described differently there.
    _persist_index(_slug_entry(), {"host": HOST, "project": PROJECT, "slug": SLUG, "uid": UID_A})
    meta.write(HOST, PROJECT, SLUG, title="the old title")
    meta.write(HOST, PROJECT, UID_A, title="the new title")

    payload = api.build_state(config)

    stored = meta.load_all()[KEY_A]
    assert stored["title"] == {"old": "the old title", "new": "the new title"}
    entry = _index()[KEY_A]
    assert entry["conflict"] == {"stores": ["meta"]}
    assert "transition" not in entry
    (row,) = _rows(payload)
    assert row["readopt"]["reason"] == "re-adopt by uid: conflicting meta state"


def test_one_real_schema_one_record_is_unchanged(config):
    legacy = _record(config, LEGACY_SESSION, "2026-01-01T00:00:00+00:00")
    _persist_index(_slug_entry())
    meta.write(HOST, PROJECT, SLUG, title="the chat")
    before = _index_bytes()

    payload = api.build_state(config)

    assert _index_bytes() == before
    assert meta.read(HOST, PROJECT, SLUG).title == "the chat"
    (row,) = _rows(payload)
    assert row["uid"] is None and row["readopt"] is None
    assert row["rel_path"] == f"widgets/_runs/{SLUG}/{legacy.name}"


# --- the operator's bind -------------------------------------------------------


def test_bind_adopts_an_unresolved_entry_by_uid(config):
    _uid_record(config, UID_A, "2026-01-01T00:00:00+00:00")
    _uid_record(config, UID_B, "2026-02-01T00:00:00+00:00")
    _persist_index(_slug_entry())
    api.build_state(config)

    bound = run_transition.bind(config, HOST, PROJECT, SLUG, UID_A)

    assert bound.key == KEY_A
    assert set(_index()) == {KEY_A}
    assert "unresolved" not in _index()[KEY_A]
    # Idempotent through the alias; a different uid is refused.
    assert run_transition.bind(config, HOST, PROJECT, SLUG, UID_A).key == KEY_A
    with pytest.raises(runs.RunIndexError):
        run_transition.bind(config, HOST, PROJECT, SLUG, UID_B)


def test_bind_refuses_a_uid_the_mirror_does_not_hold(config):
    _uid_record(config, UID_A, "2026-01-01T00:00:00+00:00")
    _persist_index(_slug_entry())

    with pytest.raises(runs.RunIndexError):
        run_transition.bind(config, HOST, PROJECT, SLUG, UID_B)
    with pytest.raises(runs.RunIndexError):
        run_transition.bind(config, HOST, PROJECT, SLUG, "not-a-uid")
    assert set(_index()) == {SLUG_KEY}


# --- a crash between a store's write and mark_transition (review !387, D4) ------


def _plant_conflict(store_name):
    """A uid entry beside the slug entry, both holding *store_name* state that
    differs, so the carry keeps two values."""
    if store_name == "meta":
        meta.write(HOST, PROJECT, SLUG, title="the old title")
        meta.write(HOST, PROJECT, UID_A, title="the new title")
    elif store_name == "references":
        references.add(HOST, PROJECT, SLUG, THREAD, title="the old title")
        references.add(HOST, PROJECT, UID_A, THREAD, title="the new title")
    elif store_name == "relations":
        relations.relate((HOST, PROJECT, SLUG), (HOST, PROJECT, "other"))
        relations.relate((HOST, PROJECT, UID_A), (HOST, PROJECT, "other"))
        entries = relations.load_all()
        for (pair, payload), kind in zip(sorted(entries.items()), ("old", "new")):
            payload["kind"] = kind
        relations._save(entries)


@pytest.mark.parametrize("failing", ["meta", "relations", "references"])
def test_a_crash_after_a_store_wrote_its_conflict_still_marks_the_entry(
    config, monkeypatch, failing
):
    _uid_record(config, UID_A, "2026-01-01T00:00:00+00:00")
    _persist_index(_slug_entry(), {"host": HOST, "project": PROJECT, "slug": SLUG, "uid": UID_A},
                   {"host": HOST, "project": PROJECT, "slug": "other"})
    _plant_conflict(failing)
    module = {"meta": meta, "relations": relations, "references": references}[failing]
    original = module.rekey
    calls = []

    def write_then_crash(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        if len(calls) == 1:
            raise RuntimeError("daemon died after the write")
        return result

    monkeypatch.setattr(module, "rekey", write_then_crash)

    api.build_state(config)

    assert calls[0]["conflict"] is True
    assert "conflict" not in _index()[KEY_A], "precondition: the crash lost the mark"

    payload = api.build_state(config)

    assert calls[1] == {"moved": False, "conflict": True}, "the re-run reports the pair"
    entry = _index()[KEY_A]
    assert "transition" not in entry
    assert failing in entry["conflict"]["stores"]
    (row,) = [r for r in _rows(payload) if r.get("uid") == UID_A]
    assert row["readopt"]["reason"].startswith(f"{runs.READOPT_BY_UID}: conflicting")


def test_re_adopting_by_uid_clears_the_mark_once_the_pair_is_written(config):
    _uid_record(config, UID_A, "2026-01-01T00:00:00+00:00")
    _persist_index(_slug_entry(), {"host": HOST, "project": PROJECT, "slug": SLUG, "uid": UID_A})
    _plant_conflict("meta")
    api.build_state(config)
    assert _index()[KEY_A]["conflict"] == {"stores": ["meta"]}

    assert run_transition.recheck_conflict(KEY_A) == ["meta"], "the pair is still there"
    assert _index()[KEY_A]["conflict"] == {"stores": ["meta"]}

    meta.write(HOST, PROJECT, UID_A, title="the chosen title")
    assert run_transition.recheck_conflict(KEY_A) == []
    assert "conflict" not in _index()[KEY_A]
    assert meta.read(HOST, PROJECT, UID_A).title == "the chosen title"


def test_matching_titles_with_different_stamps_are_no_conflict(config):
    _uid_record(config, UID_A, "2026-01-01T00:00:00+00:00")
    _persist_index(_slug_entry(), {"host": HOST, "project": PROJECT, "slug": SLUG, "uid": UID_A})
    meta.write(HOST, PROJECT, SLUG, title="the chat")
    checkin.record_check(HOST, PROJECT, SLUG)
    meta.write(HOST, PROJECT, UID_A, title="the chat")
    checkin.record_check(HOST, PROJECT, UID_A)

    api.build_state(config)

    assert "conflict" not in _index()[KEY_A]
    assert meta.read(HOST, PROJECT, UID_A).title == "the chat"


def test_a_live_session_of_the_slug_entry_is_canonicalized_after_a_merge(config):
    """The uid entry has its own last session; the slug entry's still-live one
    must still join the uid row rather than stay a second row."""
    registry.register(SESSION, pid=os.getpid(),
                      run={"host": HOST, "project": PROJECT, "slug": SLUG},
                      started_at="2026-08-01T00:00:00Z")
    _uid_record(config, UID_A, "2026-08-01T00:00:02+00:00",
                launcher={"pid": os.getpid(), "hostname": socket.gethostname(),
                          "started_at": "2026-08-01T00:00:02+00:00"})
    _persist_index(_slug_entry(last_session_id=SESSION),
                   {"host": HOST, "project": PROJECT, "slug": SLUG, "uid": UID_A,
                    "last_session_id": "s-the-uid-entrys-own"})

    run_transition.reconcile(config)

    assert registry.read_session(SESSION)["run"]["uid"] == UID_A
    assert _index()[KEY_A]["last_session_id"] == "s-the-uid-entrys-own"


# --- reconcile's cost (review !387, observation) --------------------------------


def test_a_schema_one_group_is_not_walked_again_until_the_mirror_moves(config, monkeypatch):
    _record(config, LEGACY_SESSION, "2026-01-01T00:00:00+00:00")
    _persist_index(_slug_entry())
    head = ["sha-1"]
    monkeypatch.setattr(run_transition, "_mirror_head", lambda _config: head[0])
    monkeypatch.setattr(run_transition, "_EXAMINED", {})
    walks = []
    original = run_transition.workrepo.group_records
    monkeypatch.setattr(run_transition.workrepo, "group_records",
                        lambda *a, **k: walks.append(a) or original(*a, **k))

    run_transition.reconcile(config)
    run_transition.reconcile(config)
    assert len(walks) == 1

    head[0] = "sha-2"
    run_transition.reconcile(config)
    assert len(walks) == 2


def test_an_unresolved_group_is_re_read_only_when_the_mirror_or_registry_moves(
    config, monkeypatch
):
    _uid_record(config, UID_A, "2026-01-01T00:00:00+00:00")
    _uid_record(config, UID_B, "2026-02-01T00:00:00+00:00")
    _persist_index(_slug_entry(last_session_id=SESSION))
    monkeypatch.setattr(run_transition, "_mirror_head", lambda _config: "sha-1")
    monkeypatch.setattr(run_transition, "_EXAMINED", {})
    walks = []
    original = run_transition.workrepo.group_records
    monkeypatch.setattr(run_transition.workrepo, "group_records",
                        lambda *a, **k: walks.append(a) or original(*a, **k))

    assert run_transition.reconcile(config)["unresolved"] == [SLUG_KEY]
    run_transition.reconcile(config)
    assert len(walks) == 1

    registry.register(SESSION, pid=4242, run={"host": HOST, "project": PROJECT, "slug": SLUG},
                      started_at="2026-02-01T00:00:00Z")
    run_transition.reconcile(config)
    assert len(walks) == 2


def test_the_examined_memo_is_pruned_to_the_index(config, monkeypatch):
    """An entry no longer uid-less in the index leaves the memo on the next build."""
    run_transition._EXAMINED["h/p/gone"] = ("head", None, None, None)

    run_transition.reconcile(config)

    assert "h/p/gone" not in run_transition._EXAMINED
