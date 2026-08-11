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
