"""Tests for the tracked-run index (issue #141, slice M1 / T7).

The property that matters most: a fresh orchestrator tracks nothing, so the fleet
view is empty no matter how full the shared work repo is. Everything else follows
— entries survive session exit, only an explicit forget removes them, and a
passing session cannot widen the scope on its own.
"""

import json

import pytest

from lmer_platform import runs, store
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


# --- the headline property --------------------------------------------------

def test_fresh_orchestrator_tracks_nothing(platform_root):
    assert runs.list_tracked() == []
    assert runs.load_index() == {}


def test_tracking_is_never_implicit(platform_root):
    """A session for an untracked run must not widen the view's scope."""
    assert runs.note_session("gitlab.example.com", "agents/global", "some-run", "s-1") is None
    assert runs.list_tracked() == []


# --- keys -------------------------------------------------------------------

def test_run_key_shape(platform_root):
    assert runs.run_key("gitlab.example.com", "agents/global", "develop-1") == (
        "gitlab.example.com/agents/global/develop-1"
    )


@pytest.mark.parametrize("args", [
    ("", "agents/global", "s"),
    ("h", "", "s"),
    ("h", "p", ""),
    (None, "p", "s"),
    ("h", "p", "   "),
])
def test_run_key_rejects_empty_parts(platform_root, args):
    with pytest.raises(runs.RunIndexError):
        runs.run_key(*args)


# --- track ------------------------------------------------------------------

def test_track_records_a_run(platform_root):
    entry = runs.track(
        "gitlab.example.com", "agents/global", "develop-issue-141",
        taskdef="develop", target="issue-141", repo="git@gitlab.example.com:agents/global.git",
        session_id="s-1",
    )
    assert entry.source == "spawned"
    assert entry.key == "gitlab.example.com/agents/global/develop-issue-141"
    assert entry.first_seen and entry.last_seen

    listed = runs.list_tracked()
    assert len(listed) == 1
    assert listed[0].taskdef == "develop"
    assert listed[0].last_session_id == "s-1"


def test_track_rejects_unknown_source(platform_root):
    with pytest.raises(runs.RunIndexError, match="invalid source"):
        runs.track("h", "p", "s", source="inferred")


def test_retracking_preserves_first_seen_and_source(platform_root):
    first = runs.track("h", "p", "s", source="spawned", taskdef="develop")
    again = runs.track("h", "p", "s", source="adopted", taskdef="review")

    assert again.first_seen == first.first_seen
    assert again.source == "spawned", "a spawned run does not become adopted"
    assert again.taskdef == "review", "newer metadata does win"


def test_adopted_run_keeps_its_source(platform_root):
    runs.track("h", "p", "s", source="adopted")
    assert runs.get_tracked("h", "p", "s").source == "adopted"


def test_track_preserves_metadata_when_not_supplied(platform_root):
    runs.track("h", "p", "s", taskdef="develop", target="t", repo="r")
    runs.track("h", "p", "s", session_id="s-2")

    entry = runs.get_tracked("h", "p", "s")
    assert (entry.taskdef, entry.target, entry.repo) == ("develop", "t", "r")
    assert entry.last_session_id == "s-2"


def test_note_session_updates_a_tracked_run(platform_root):
    runs.track("h", "p", "s")
    assert runs.note_session("h", "p", "s", "s-9").last_session_id == "s-9"


def test_get_tracked_absent_is_none(platform_root):
    assert runs.get_tracked("h", "p", "nope") is None


# --- ordering ---------------------------------------------------------------

def test_list_tracked_is_most_recent_first(platform_root):
    runs.track("h", "p", "old")
    runs.track("h", "p", "new")
    index = runs.load_index()
    index["h/p/old"]["last_seen"] = "2026-01-01T00:00:00Z"
    index["h/p/new"]["last_seen"] = "2026-07-26T00:00:00Z"
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"runs": index})

    assert [e.slug for e in runs.list_tracked()] == ["new", "old"]


# --- forget -----------------------------------------------------------------

def test_forget_removes_a_run(platform_root):
    runs.track("h", "p", "s")
    assert runs.forget("h", "p", "s") is True
    assert runs.list_tracked() == []


def test_forget_untracked_is_false(platform_root):
    assert runs.forget("h", "p", "s") is False


def test_forget_leaves_other_runs_alone(platform_root):
    runs.track("h", "p", "keep")
    runs.track("h", "p", "drop")
    runs.forget("h", "p", "drop")
    assert [e.slug for e in runs.list_tracked()] == ["keep"]


# --- durability -------------------------------------------------------------

def test_entries_survive_across_reads(platform_root):
    runs.track("h", "p", "s", taskdef="develop")
    # A fresh read is what the daemon does on every state build.
    assert runs.get_tracked("h", "p", "s").taskdef == "develop"


def test_index_is_human_readable(platform_root):
    """D2's point: repairable in an editor when the orchestrator is wedged."""
    runs.track("gitlab.example.com", "agents/global", "develop-1")
    payload = json.loads(store.snapshot_path(runs.RUNS_FILE).read_text(encoding="utf-8"))
    assert "gitlab.example.com/agents/global/develop-1" in payload["runs"]
    assert payload["schema"] == store.SCHEMA_VERSION


def test_corrupt_index_reads_as_empty_and_is_logged(platform_root, caplog):
    """A corrupt index must not be mistaken for "the daemon lost my runs"."""
    path = store.snapshot_path(runs.RUNS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert runs.list_tracked() == []
    assert any("platform_run_index_unreadable" in r.message for r in caplog.records)
    assert len(list(path.parent.glob("runs.json.bad-*"))) == 1


def test_malformed_entry_is_skipped_not_fatal(platform_root, caplog):
    runs.track("h", "p", "good")
    index = runs.load_index()
    index["h/p/bad"] = {"host": "h", "slug": "bad"}  # no project
    index["h/p/worse"] = "not even a mapping"
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"runs": index})

    assert [e.slug for e in runs.list_tracked()] == ["good"]
    assert any("platform_tracked_run_malformed" in r.message for r in caplog.records)


def test_index_without_runs_key_reads_as_empty(platform_root):
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"something": "else"})
    assert runs.list_tracked() == []


def test_unknown_source_in_file_degrades_to_adopted(platform_root):
    """Never silently claim the platform spawned something it may not have."""
    runs.track("h", "p", "s")
    index = runs.load_index()
    index["h/p/s"]["source"] = "telepathy"
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"runs": index})

    assert runs.get_tracked("h", "p", "s").source == "adopted"


def test_to_dict_shape(platform_root):
    entry = runs.track("h", "p", "s", taskdef="develop", target="t")
    payload = entry.to_dict()
    assert set(payload) >= {
        "host", "project", "slug", "source", "first_seen", "last_seen",
        "last_session_id", "taskdef", "target", "repo", "note", "key",
    }


def test_an_entry_carries_identity_and_never_a_composed_run_directory(platform_root):
    """The last of the composed-path family (T90, finished in T96).

    This index records the slug a run was keyed under. The container renames a
    named run's directory to ``runs/<slug>--<name>`` — most real runs are named —
    so ``runs/<slug>`` is a directory that does not exist, and every reader of the
    old ``rel_path`` quoted one: the tracked listing, the adopt message, the
    dormant fleet row and the run header in the detail view.

    A directory is *found*, by content (``workrepo.resolve_run_dir``), and only
    what found it may name one. What is stored here is identity, so identity is
    all an entry offers.
    """
    entry = runs.track("gitlab.example.com", "agents/global", "develop-issue-141")

    assert not hasattr(entry, "rel_path"), (
        "TrackedRun composes a run directory again — for a named run that path "
        "names nothing, and the entry has no way to know the name"
    )
    payload = entry.to_dict()
    assert payload["key"] == "gitlab.example.com/agents/global/develop-issue-141"
    for name, value in payload.items():
        assert "runs/" not in str(value), (
            f"{name} carries a composed run directory ({value!r}); the index knows "
            "the slug, not the directory it lives in"
        )


def test_keys_helper(platform_root):
    runs.track("h", "p", "a")
    runs.track("h", "p", "b")
    assert runs.keys(runs.list_tracked()) == {("h", "p", "a"), ("h", "p", "b")}


# --- the session-unit record (issue #353) -----------------------------------
#
# In schema form a launch owns ``<project>/_runs/<slug>/<session-id>/`` and one
# slug groups several independent launches (D22). The index is unmoved by it:
# the key is identity, the session id is an address hint, and a directory is
# found rather than composed. These pin that, because "nothing changes" is only
# a decision if something asserts it.

#: A session id in the schema-form grammar — which is now also a directory name
#: in the work repo (D22), and still just an id here.
SESSION_ID = "20260824-165230-2-31245"


def test_run_key_shape_is_unchanged_by_the_session_record(platform_root):
    """``host/project/slug``: the key in runs.json, meta, relations and the
    check-in marks. A session id never enters it."""
    entry = runs.track("gitlab.example.com", "acme/widgets", "develop-issue-353",
                       session_id=SESSION_ID)

    assert entry.key == "gitlab.example.com/acme/widgets/develop-issue-353"
    assert SESSION_ID not in entry.key


def test_an_entry_never_carries_a_session_directory(platform_root):
    """The composed-path family, one shape later. ``runs/<slug>`` was wrong for
    every named run; ``_runs/<slug>/<session-id>`` is a directory this index
    cannot even guess at, since which session is current is the mirror's answer.
    """
    entry = runs.track("gitlab.example.com", "acme/widgets", "develop-issue-353",
                       session_id=SESSION_ID)

    for name, value in entry.to_dict().items():
        assert "_runs/" not in str(value), (
            f"{name} carries a composed session directory ({value!r})"
        )


def test_a_schema_form_session_joins_its_run_through_the_tracked_index(platform_root):
    """The second source is the one that matters here: a clean exit removes the
    registry entry, and the session id is what the fleet view has left."""
    runs.track("gitlab.example.com", "acme/widgets", "develop-issue-353")
    runs.note_session("gitlab.example.com", "acme/widgets", "develop-issue-353",
                      SESSION_ID)

    assert runs.run_for_session(SESSION_ID) == (
        "gitlab.example.com", "acme/widgets", "develop-issue-353"
    )


def test_a_named_session_dir_still_joins_its_run(platform_root):
    """``work name`` renames the session DIR to ``<session-id>--<name>``; the id
    the session is registered under does not change, and a lookup with the dir
    name is simply not a session this host knows — tolerantly, not fatally."""
    runs.track("h", "p", "s")
    runs.note_session("h", "p", "s", SESSION_ID)

    assert runs.run_for_session(SESSION_ID) == ("h", "p", "s")
    assert runs.run_for_session(f"{SESSION_ID}--the-name") is None


def test_an_entry_written_by_a_build_that_stores_more_still_loads(platform_root):
    """Forward tolerance, for the same reason ``from_dict`` is tolerant at all:
    an index a newer build wrote must not empty this one's fleet view."""
    entry = runs.TrackedRun.from_dict("h/p/s", {
        "host": "h", "project": "p", "slug": "s",
        "last_session_id": SESSION_ID,
        "session_dir": f"p/_runs/s/{SESSION_ID}",
        "sessions": [SESSION_ID],
    })

    assert entry is not None
    assert entry.key == "h/p/s"
    assert entry.last_session_id == SESSION_ID
    assert not hasattr(entry, "session_dir"), "and the address is still not stored"


# --- uid identity (A2.key, spec §6.1) ---------------------------------------

#: A schema-2 execution uid: 32 lowercase hex.
UID_A = "0123456789abcdef0123456789abcdef"
UID_B = "fedcba9876543210fedcba9876543210"


def test_two_uid_entries_sharing_one_slug_coexist(platform_root):
    """A slug is a group label; the uid is the identity. Two launches against
    one target are two entries, never one overwriting the other."""
    first = runs.track("h", "p", "chat", uid=UID_A)
    second = runs.track("h", "p", "chat", uid=UID_B)

    assert first.key == f"h/p/{UID_A}"
    assert second.key == f"h/p/{UID_B}"
    assert first.slug == second.slug == "chat"
    assert len(runs.list_tracked()) == 2


def test_get_tracked_addresses_by_uid(platform_root):
    runs.track("h", "p", "chat", uid=UID_A)

    by_uid = runs.get_tracked("h", "p", uid=UID_A)
    assert by_uid is not None and by_uid.uid == UID_A
    # No slug-keyed entry exists: a uid entry is not also addressable by slug.
    assert runs.get_tracked("h", "p", "chat") is None


def test_track_stores_the_slug_as_the_group_label(platform_root):
    entry = runs.track("h", "p", "develop-1", uid=UID_A)
    assert entry.to_dict()["uid"] == UID_A
    assert entry.to_dict()["slug"] == "develop-1"


def test_old_uidless_index_loads_unchanged(platform_root):
    """Forward/backward tolerance: an index written before uids existed keys by
    slug and reads exactly as it always did."""
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"runs": {
        "h/p/develop-1": {
            "host": "h", "project": "p", "slug": "develop-1",
            "source": "spawned", "first_seen": "2026-01-01T00:00:00Z",
        },
    }})

    entry = runs.get_tracked("h", "p", "develop-1")
    assert entry is not None and entry.uid is None
    assert entry.key == "h/p/develop-1"


def test_malformed_uid_is_dropped_not_fatal(platform_root, caplog):
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"runs": {
        "h/p/develop-1": {
            "host": "h", "project": "p", "slug": "develop-1",
            "uid": "NOT-HEX", "source": "spawned",
        },
    }})

    entry = runs.get_tracked("h", "p", "develop-1")
    assert entry is not None and entry.uid is None
    assert entry.key == "h/p/develop-1"


def test_the_uid_grammar_refuses_a_trailing_newline():
    """``$`` matches before a final newline, so ``^…$`` with ``.match`` took
    ``<uid>\\n`` for a uid — a key segment carrying a newline."""
    assert runs.UID_RE.match(UID_A)
    assert runs.UID_RE.match(UID_A + "\n") is None


def test_run_key_of_entry_reads_the_uid(platform_root):
    assert runs.run_key_of_entry(
        {"run": {"host": "h", "project": "p", "slug": "chat", "uid": UID_A}}
    ) == ("h", "p", UID_A)
    assert runs.run_key_of_entry(
        {"run": {"host": "h", "project": "p", "slug": "chat"}}
    ) == ("h", "p", "chat")
    assert runs.run_key_of_entry(None) is None


def test_key_parts_over_the_three_shapes(platform_root):
    """One helper spells "the key segment" for a fleet row, a request body and a
    registry ``run`` block (spec §6.1)."""
    fleet_row = {"host": "h", "project": "p", "slug": "chat", "uid": UID_A}
    request_body = {"host": "h", "project": "p", "slug": "chat", "uid": UID_A}
    registry_block = {"run": {"host": "h", "project": "p", "slug": "chat", "uid": UID_A}}

    assert runs.key_parts(fleet_row) == ("h", "p", UID_A)
    assert runs.key_parts(request_body) == ("h", "p", UID_A)
    assert runs.key_parts(registry_block) == ("h", "p", UID_A)
    assert runs.key_parts({"host": "h", "project": "p", "slug": "chat"}) == ("h", "p", "chat")


def test_note_session_and_forget_address_a_uid_entry(platform_root):
    runs.track("h", "p", "chat", uid=UID_A)

    assert runs.note_session("h", "p", uid=UID_A, session_id=SESSION_ID).last_session_id == SESSION_ID
    assert runs.forget("h", "p", uid=UID_A) is True
    assert runs.list_tracked() == []


def test_run_for_session_reads_the_registry_uid(platform_root):
    registry_entry = {"run": {"host": "h", "project": "p", "slug": "chat", "uid": UID_A}}
    assert runs.run_key_of_entry(registry_entry) == ("h", "p", UID_A)


def test_keys_helper_follows_the_uid(platform_root):
    runs.track("h", "p", "chat", uid=UID_A)
    runs.track("h", "p", "chat", uid=UID_B)
    assert runs.keys(runs.list_tracked()) == {("h", "p", UID_A), ("h", "p", UID_B)}


def test_lossless_collision_keeps_both_values():
    assert runs.merge_values(None, 1) == (1, False)
    assert runs.merge_values(1, None) == (1, False)
    assert runs.merge_values(1, 1) == (1, False)
    merged, conflict = runs.merge_values(1, 2)
    assert conflict is True
    assert merged == {"old": 1, "new": 2}


def test_transition_record_round_trips():
    transition = runs.Transition(old_key="h/p/chat", new_key=f"h/p/{UID_A}")
    assert transition.state == "pending"
    assert set(transition.stores) == set(runs.TRANSITION_STORES)
    assert all(v == "pending" for v in transition.stores.values())
    assert runs.Transition.from_dict(transition.to_dict()) == transition


# --- review !387 -------------------------------------------------------------


def test_run_for_session_finds_a_uid_tracked_run_whose_registry_entry_is_gone(platform_root):
    """A clean exit removes the registry entry; the tracked index's
    ``last_session_id`` still answers, with the uid as the key segment."""
    from lmer_platform import registry

    runs.track("h", "p", "chat", uid=UID_A, session_id=SESSION_ID)
    runs.track("h", "p", "chat", uid=UID_B, session_id="s-other")
    assert registry.read_session(SESSION_ID) is None, "precondition: no registry entry"

    assert runs.run_for_session(SESSION_ID) == ("h", "p", UID_A)
    assert runs.run_of_session(SESSION_ID) == {
        "host": "h", "project": "p", "slug": "chat", "uid": UID_A,
    }


def test_run_of_session_keeps_a_schema_one_run_s_shape(platform_root):
    runs.track("h", "p", "chat", session_id=SESSION_ID)

    assert runs.run_of_session(SESSION_ID) == {"host": "h", "project": "p", "slug": "chat"}


@pytest.mark.parametrize("uid", [
    UID_A.upper(), UID_A[:-1], UID_A + "\n", 42, ["x"],
])
def test_key_parts_refuses_a_malformed_uid_rather_than_keying_by_slug(uid):
    with pytest.raises(runs.RunIndexError, match="uid"):
        runs.key_parts({"host": "h", "project": "p", "slug": "chat", "uid": uid})
    with pytest.raises(runs.RunIndexError, match="uid"):
        runs.key_parts({"run": {"host": "h", "project": "p", "slug": "chat", "uid": uid}})


def test_an_absent_uid_keys_by_slug():
    for uid in (None, ""):
        assert runs.key_parts(
            {"host": "h", "project": "p", "slug": "chat", "uid": uid}
        ) == ("h", "p", "chat")


def test_flat_fields_win_over_a_nested_run_block():
    row = {"host": "h", "project": "p", "slug": "chat", "uid": UID_A,
           "run": {"host": "h", "project": "p", "slug": "chat", "uid": UID_B}}

    assert runs.key_parts(row) == ("h", "p", UID_A)
    assert runs.key_parts_lenient(row) == ("h", "p", UID_A)


def test_the_lenient_reader_degrades_and_never_raises():
    bad = {"host": "h", "project": "p", "slug": "chat", "uid": UID_A.upper()}
    assert runs.key_parts_lenient(bad) == ("h", "p", "chat")
    assert runs.key_parts_lenient({"run": bad}) == ("h", "p", "chat")
    assert runs.key_parts_lenient({"host": "h", "slug": "chat"}) is None
    assert runs.key_parts_lenient(None) is None
    assert runs.run_key_of_entry({"run": bad}) == ("h", "p", "chat")


def test_retracking_a_schema_one_entry_keeps_its_pre_uid_shape(platform_root, monkeypatch):
    """Byte-identical: no ``uid`` key appears in runs.json, the entry keeps its
    key order, and the reply shape is unchanged."""
    monkeypatch.setattr(runs, "utc_now_iso", lambda: "2026-07-02T00:00:00Z")
    pre_uid = {
        "host": "h", "project": "p", "slug": "chat", "source": "spawned",
        "first_seen": "2026-07-01T00:00:00Z", "last_seen": "2026-07-01T00:00:00Z",
        "last_session_id": "s-1", "taskdef": "chat", "target": None, "repo": None,
        "note": "mine",
    }
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"runs": {"h/p/chat": dict(pre_uid)}})

    tracked = runs.track("h", "p", "chat", session_id="s-2")

    stored = runs.load_index()["h/p/chat"]
    expected = {**pre_uid, "last_seen": "2026-07-02T00:00:00Z", "last_session_id": "s-2"}
    assert json.dumps(stored) == json.dumps(expected)
    assert "uid" not in tracked.to_dict()


def test_a_track_written_null_uid_is_dropped_on_retrack(platform_root):
    """An entry an earlier build of this branch wrote with ``"uid": null``
    returns to the schema-1 shape."""
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"runs": {"h/p/chat": {
        "host": "h", "project": "p", "slug": "chat", "uid": None, "source": "spawned",
    }}})

    runs.note_session("h", "p", "chat", "s-1")

    assert "uid" not in runs.load_index()["h/p/chat"]


def test_merge_values_keeps_the_later_stamp_and_reports_no_conflict():
    old = {"title": "same", "updated_at": "2026-01-02T00:00:00Z", "revision": 3}
    new = {"title": "same", "updated_at": "2026-01-01T00:00:00+00:00", "revision": 5}

    merged, conflict = runs.merge_values(old, new)

    assert conflict is False
    assert merged == {"title": "same", "updated_at": "2026-01-02T00:00:00Z", "revision": 5}


def test_merge_values_merges_one_level():
    merged, conflict = runs.merge_values({"a": {"x": 1}}, {"a": {"x": 2}})

    assert conflict is True
    assert merged == {"a": {"old": {"x": 1}, "new": {"x": 2}}}
    assert runs.holds_conflict(merged)
    assert not runs.holds_conflict({"a": {"x": 1}})


def _two_entries(old_extra, new_extra):
    old_key, new_key = "h/p/chat", f"h/p/{UID_A}"
    base = {"host": "h", "project": "p", "slug": "chat", "source": "spawned",
            "first_seen": "2026-07-01T00:00:00Z", "last_seen": "2026-07-01T00:00:00Z"}
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"runs": {
        old_key: {**base, **old_extra},
        new_key: {**base, "uid": UID_A, "source": "adopted",
                  "last_seen": "2026-07-03T00:00:00Z", **new_extra},
    }})
    return old_key, new_key


def test_a_merge_onto_a_uid_entry_keeps_a_differing_note_and_marks_it(platform_root):
    old_key, new_key = _two_entries(
        {"note": "the slug note", "last_session_id": "s-old", "taskdef": "chat"},
        {"note": "the uid note", "last_session_id": "s-new"},
    )

    runs.begin_transition("h", "p", "chat", UID_A)

    entry = runs.load_index()[new_key]
    assert entry["note"] == "the uid note"
    assert entry["carried"]["note"] == {"old": "the slug note", "new": "the uid note"}
    assert entry["carried"]["last_session_id"] == {"old": "s-old", "new": "s-new"}
    assert entry["taskdef"] == "chat", "a field the uid entry lacked is carried"
    assert entry["last_seen"] == "2026-07-03T00:00:00Z", "the later stamp is kept"
    assert entry["source"] == "adopted"
    assert entry["conflict"] == {"stores": ["index"]}
    assert runs.carried_session_ids(new_key) == ["s-new", "s-old"]


def test_a_merge_differing_only_in_stamps_and_session_is_no_conflict(platform_root):
    _old_key, new_key = _two_entries(
        {"note": "same", "last_session_id": "s-old"},
        {"note": "same", "last_session_id": "s-new"},
    )

    runs.begin_transition("h", "p", "chat", UID_A)

    entry = runs.load_index()[new_key]
    assert "conflict" not in entry
    assert "note" not in entry["carried"]


def test_what_clears_a_conflict_mark(platform_root):
    """An explicit note replaces the kept pair; the mark itself goes only when
    asked to settle with no store still conflicted."""
    _old_key, new_key = _two_entries({"note": "old"}, {"note": "new"})
    runs.begin_transition("h", "p", "chat", UID_A)
    runs.mark_transition(new_key, (), conflict=["meta"])
    assert runs.conflict_stores(new_key) == ["index", "meta"]

    assert runs.settle_conflict(new_key, ["index", "meta"]) == ["index", "meta"]
    runs.track("h", "p", "chat", uid=UID_A, note="resolved")
    assert runs.index_conflicted(new_key) is False
    assert runs.load_index()[new_key]["note"] == "resolved"

    assert runs.settle_conflict(new_key, ["meta"]) == ["meta"]
    assert runs.settle_conflict(new_key, []) == []
    assert "conflict" not in runs.load_index()[new_key]


def test_merge_values_keeps_the_earlier_origin_stamp():
    """``first_seen``/``created``/``created_at`` record a beginning: the earlier
    of two is the true one. Every other stamp keeps the later."""
    old = {"first_seen": "2026-01-01T00:00:00Z", "created_at": "2026-01-01T00:00:00Z",
           "last_seen": "2026-01-01T00:00:00Z"}
    new = {"first_seen": "2026-03-01T00:00:00Z", "created_at": "2026-03-01T00:00:00+00:00",
           "last_seen": "2026-03-01T00:00:00Z"}

    merged, conflict = runs.merge_values(old, new)

    assert conflict is False
    assert merged["first_seen"] == "2026-01-01T00:00:00Z"
    assert merged["created_at"] == "2026-01-01T00:00:00Z"
    assert merged["last_seen"] == "2026-03-01T00:00:00Z"
    assert runs.merge_values({"created": 5}, {"created": 3})[0] == {"created": 3}


def test_a_second_carry_onto_one_uid_keeps_both_kept_pairs(platform_root):
    """Two slug entries bound to one uid in turn: the second carry's pair is
    added beside the first, never written over it."""
    old_key, new_key = _two_entries(
        {"note": "first slug note", "last_session_id": "s-first"},
        {"note": "the uid note", "last_session_id": "s-new"},
    )
    runs.begin_transition("h", "p", "chat", UID_A)
    runs.mark_transition(new_key, runs.TRANSITION_STORES)
    runs.finish_transition(new_key)
    index = runs.load_index()
    index["h/p/chat-again"] = {
        "host": "h", "project": "p", "slug": "chat-again", "source": "spawned",
        "note": "second slug note", "last_session_id": "s-second",
    }
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"runs": index})

    runs.begin_transition("h", "p", "chat-again", UID_A)

    entry = runs.load_index()[new_key]
    assert entry["note"] == "the uid note"
    assert entry["carried"]["note"] == [
        {"old": "first slug note", "new": "the uid note"},
        {"old": "second slug note", "new": "the uid note"},
    ]
    assert runs.carried_session_ids(new_key) == ["s-new", "s-first", "s-second"]
    assert entry["conflict"] == {"stores": ["index"]}
