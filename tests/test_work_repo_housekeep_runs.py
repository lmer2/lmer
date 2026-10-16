"""`work housekeep` runs area (plan task 40, #371 part 2)."""
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import strip_lmer_env
from tests.plexus_fixtures import build_legacy_plexus
from tests.schema2_fixtures import (
    DEFAULT_PROJECT,
    RecordSpec,
    build_schema2_plexus,
    stamp,
    uid_for,
)
from work_repo import housekeep, run_state


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    strip_lmer_env(monkeypatch)


def _plan(tree, project=DEFAULT_PROJECT, *, archive_stale=False):
    policy = housekeep.resolve_policy(tree.root)
    entries = housekeep.project_dirs(tree.root, project)
    return housekeep.build_plan(
        tree.root, entries, policy, ["runs"], archive_stale=archive_stale
    )


def _mark_seeded(tree, uid, seeded_by="b" * 32):
    import yaml

    path = tree.record_dir(DEFAULT_PROJECT, uid) / "state.yaml"
    state = yaml.safe_load(path.read_text())
    state["session"]["seeded_by"] = seeded_by
    path.write_text(yaml.safe_dump(state, sort_keys=False))


class TestSchemaTwoLiveness:
    def test_a_stopped_record_past_the_ttl_is_moved(self, tmp_path):
        tree = build_schema2_plexus(tmp_path / "plexus")
        uid = uid_for(1)
        tree.add(DEFAULT_PROJECT, RecordSpec(
            uid, "chat", stamp(1, month=1), stamp(1, month=1),
            status="complete", stop_reason="complete", signed_out=True))
        moves = [a for a in _plan(tree).actions if a.kind == "move"]
        assert len(moves) == 1
        assert moves[0].dst.endswith(f"_archive/_runs/chat/{uid}")

    def test_a_stopped_record_within_the_ttl_stays(self, tmp_path):
        tree = build_schema2_plexus(tmp_path / "plexus")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tree.add(DEFAULT_PROJECT, RecordSpec(
            uid_for(1), "chat", now, now, status="complete", stop_reason="complete",
            signed_out=True))
        assert [a for a in _plan(tree).actions if a.kind == "move"] == []

    def test_a_waiting_on_question_record_is_never_moved(self, tmp_path):
        tree = build_schema2_plexus(tmp_path / "plexus")
        tree.add(DEFAULT_PROJECT, RecordSpec(
            uid_for(1), "chat", stamp(1, month=1), stamp(1, month=1),
            status="in-progress", stop_reason="question", signed_out=False))
        assert [a for a in _plan(tree).actions if a.kind == "move"] == []

    def test_a_signed_out_waiting_on_question_record_follows_the_ttl(self, tmp_path):
        tree = build_schema2_plexus(tmp_path / "plexus")
        tree.add(DEFAULT_PROJECT, RecordSpec(
            uid_for(1), "chat", stamp(1, month=1), stamp(1, month=1),
            status="in-progress", stop_reason="question", signed_out=True))
        assert [a for a in _plan(tree).actions if a.kind == "move"]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tree.add(DEFAULT_PROJECT, RecordSpec(
            uid_for(2), "chat", now, now,
            status="in-progress", stop_reason="question", signed_out=True))
        moves = [a for a in _plan(tree).actions if a.kind == "move"]
        assert len(moves) == 1  # only the past-TTL one

    def test_a_complete_record_not_signed_out_is_never_moved_but_reported(self, tmp_path):
        tree = build_schema2_plexus(tmp_path / "plexus")
        tree.add(DEFAULT_PROJECT, RecordSpec(
            uid_for(1), "chat", stamp(1, month=1), stamp(1, month=1),
            status="complete", stop_reason="complete", signed_out=False))
        actions = _plan(tree).actions
        assert [a for a in actions if a.kind == "move"] == []
        assert any("possibly orphaned" in (a.detail or "") for a in actions)

    def test_an_unreadable_record_is_never_moved_even_with_archive_stale(self, tmp_path):
        tree = build_schema2_plexus(tmp_path / "plexus")
        tree.add(DEFAULT_PROJECT, RecordSpec(
            uid_for(1), "chat", stamp(1, month=1), stamp(1, month=1),
            status="complete", signed_out=True, readable=False))
        assert [a for a in _plan(tree, archive_stale=True).actions if a.kind == "move"] == []

    def test_a_torn_events_file_is_never_moved(self, tmp_path):
        tree = build_schema2_plexus(tmp_path / "plexus")
        uid = uid_for(1)
        tree.add(DEFAULT_PROJECT, RecordSpec(
            uid, "chat", stamp(1, month=1), stamp(1, month=1),
            status="complete", signed_out=True))
        (tree.record_dir(DEFAULT_PROJECT, uid) / "events.jsonl").write_text("{torn\n")
        assert [a for a in _plan(tree, archive_stale=True).actions if a.kind == "move"] == []

    def test_a_seed_past_the_ttl_is_moved(self, tmp_path):
        tree = build_schema2_plexus(tmp_path / "plexus")
        uid = uid_for(1)
        tree.add(DEFAULT_PROJECT, RecordSpec(
            uid, "chat", stamp(1, month=1), stamp(1, month=1), signed_out=None))
        _mark_seeded(tree, uid)
        moves = [a for a in _plan(tree).actions if a.kind == "move"]
        assert len(moves) == 1

    def test_a_seed_within_the_ttl_stays(self, tmp_path):
        tree = build_schema2_plexus(tmp_path / "plexus")
        uid = uid_for(1)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tree.add(DEFAULT_PROJECT, RecordSpec(uid, "chat", now, now, signed_out=None))
        _mark_seeded(tree, uid)
        assert [a for a in _plan(tree).actions if a.kind == "move"] == []

    def test_archive_stale_moves_a_stale_unsigned_record(self, tmp_path):
        tree = build_schema2_plexus(tmp_path / "plexus")
        tree.add(DEFAULT_PROJECT, RecordSpec(
            uid_for(1), "chat", stamp(1, month=1), stamp(1, month=1), signed_out=False))
        assert [a for a in _plan(tree).actions if a.kind == "move"] == []
        assert [a for a in _plan(tree, archive_stale=True).actions if a.kind == "move"]

    def test_archive_stale_is_refused_under_noninteractive(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        tree = build_schema2_plexus(tmp_path / "plexus")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
        monkeypatch.setenv("LMER_NONINTERACTIVE", "1")
        rc = housekeep.cmd_housekeep(SimpleNamespace(
            apply=False, area="runs", project=None, archive_stale=True))
        assert rc == 2

    def test_the_callers_own_record_is_never_a_candidate(self, tmp_path, monkeypatch):
        tree = build_schema2_plexus(tmp_path / "plexus")
        uid = uid_for(1)
        tree.add(DEFAULT_PROJECT, RecordSpec(
            uid, "chat", stamp(1, month=1), stamp(1, month=1),
            status="complete", signed_out=True))
        own = tree.record_dir(DEFAULT_PROJECT, uid)
        monkeypatch.setattr(run_state, "run_dir", lambda: own)
        assert [a for a in _plan(tree).actions if a.kind == "move"] == []

    def test_a_link_under_a_moved_dir_is_retargeted(self, tmp_path):
        tree = build_schema2_plexus(tmp_path / "plexus")
        uid = uid_for(1)
        tree.add(DEFAULT_PROJECT, RecordSpec(
            uid, "chat", stamp(1, month=1), stamp(1, month=1),
            status="complete", signed_out=True))
        rdir = tree.record_dir(DEFAULT_PROJECT, uid)
        specs = tree.project_dir() / "_specs"
        specs.mkdir()
        link = specs / "2026-01-01-chat--spec.md"
        link.symlink_to(os.path.relpath(rdir / "spec.md", specs))
        retargets = [a for a in _plan(tree).actions if a.kind == "retarget"]
        assert len(retargets) == 1
        assert retargets[0].src.endswith("_specs/2026-01-01-chat--spec.md")
        # `dst` is the new prefix; the applier keeps the file's sub-path.
        assert retargets[0].dst.endswith(f"_archive/_runs/chat/{uid}")
        assert retargets[0].detail.endswith(f"_runs/chat/{uid}")


class TestSecondRunIsANoop:
    def test_after_a_move_the_planner_finds_nothing(self, tmp_path):
        tree = build_schema2_plexus(tmp_path / "plexus")
        uid = uid_for(1)
        tree.add(DEFAULT_PROJECT, RecordSpec(
            uid, "chat", stamp(1, month=1), stamp(1, month=1),
            status="complete", signed_out=True))
        rdir = tree.record_dir(DEFAULT_PROJECT, uid)
        dest = tree.project_dir() / "_archive/_runs/chat" / uid
        dest.parent.mkdir(parents=True)
        rdir.rename(dest)
        assert [a for a in _plan(tree).actions if a.kind == "move"] == []


class TestNewOrphanSweep:
    def test_an_old_orphan_is_swept_and_a_fresh_one_kept(self, tmp_path):
        tree = build_schema2_plexus(tmp_path / "plexus")
        runs = tree.project_dir() / "_runs"
        old = runs / ".new-old-tmp"
        fresh = runs / ".new-fresh-tmp"
        old.mkdir()
        fresh.mkdir()
        old_ts = time.time() - housekeep.NEW_ORPHAN_AGE_SECONDS - 3600
        os.utime(old, (old_ts, old_ts))
        deletes = {a.src for a in _plan(tree).actions if a.kind == "delete"}
        assert any(a.endswith(".new-old-tmp") for a in deletes)
        assert not any(a.endswith(".new-fresh-tmp") for a in deletes)


class TestNoWritesToMovedRecords:
    def test_the_write_helpers_never_touch_a_moved_record(self, tmp_path, monkeypatch):
        tree = build_schema2_plexus(tmp_path / "plexus")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
        uid = uid_for(1)
        tree.add(DEFAULT_PROJECT, RecordSpec(
            uid, "chat", stamp(1, month=1), stamp(1, month=1),
            status="complete", signed_out=True))
        moved = tree.record_dir(DEFAULT_PROJECT, uid)

        def boom(*args, **kwargs):
            raise AssertionError("a writer touched the moved record")

        for name in ("write_state", "append_event", "freeze_run_dir"):
            monkeypatch.setattr(run_state, name, boom)
        # A stub planner emits the move; apply performs it without a writer.
        monkeypatch.setitem(
            housekeep.PLANNERS, "runs",
            lambda plan, **kw: plan.add(housekeep.Action(
                "move", "runs", moved.relative_to(tree.root).as_posix(),
                (tree.project_dir() / "_archive/_runs/chat" / uid).relative_to(tree.root).as_posix(),
                "90d")),
        )
        from unittest.mock import patch

        def fake_git(args, cwd):
            if args[0] == "mv":
                (cwd / args[1]).rename(cwd / args[2])
            return 0, ""

        monkeypatch.setattr(housekeep, "_run_git", fake_git)
        with patch("work_repo.housekeep.git_ops.commit_work_path", return_value=0):
            assert housekeep.cmd_housekeep(
                SimpleNamespace(apply=True, area="runs", project="widgets",
                                archive_stale=False)) == 0


class TestLegacyRuns:
    def test_a_live_owner_is_untouched(self, tmp_path):
        import yaml

        tree = build_legacy_plexus(tmp_path / "plexus-legacy")
        rdir = tree.session_dirs[0]
        state = run_state._read_sibling_state(rdir)
        state["status"] = "in-progress"
        # Fresh relative to the plan's now (2027-01-01), so `_is_orphaned`
        # is False and the record is not a candidate.
        state["owner"] = {
            "session_id": "20260824-101500-1-11111",
            "claimed_at": "2027-01-01T00:00:00Z",
        }
        run_state._state_path(rdir).write_text(yaml.safe_dump(state, sort_keys=False))
        plan = housekeep.build_plan(
            tree.root, housekeep.project_dirs(tree.root),
            housekeep.resolve_policy(tree.root), ["runs"],
            now=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
        assert [a for a in plan.actions if a.kind == "move"] == []

    def test_a_complete_run_goes_to_runs_archive(self, tmp_path):
        import yaml

        tree = build_legacy_plexus(tmp_path / "plexus-legacy")
        rdir = tree.session_dirs[0]
        state = run_state._read_sibling_state(rdir)
        state["status"] = "complete"
        state["updated"] = stamp(1, month=1)
        # Write directly: write_state bumps `updated` to now.
        run_state._state_path(rdir).write_text(yaml.safe_dump(state, sort_keys=False))
        policy = housekeep.resolve_policy(tree.root)
        entries = housekeep.project_dirs(tree.root)
        # The legacy fixture's events are fixed in 2026-08; look from 2027 so
        # the record is past the 90-day runs TTL.
        plan = housekeep.build_plan(
            tree.root, entries, policy, ["runs"],
            now=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
        moves = [a for a in plan.actions if a.kind == "move"]
        assert moves and "/runs/archive/" in moves[0].dst

    def test_runs_null_disables_the_area(self, tmp_path):
        tree = build_schema2_plexus(tmp_path / "plexus")
        tree.add(DEFAULT_PROJECT, RecordSpec(
            uid_for(1), "chat", stamp(1, month=1), stamp(1, month=1),
            status="complete", signed_out=True))
        (tree.root / "_lmer.yaml").write_text(
            "schema: 2\nname: example\nprojects:\n  widgets:\n"
            "    repo: git.example.test/acme/widgets\n"
            "housekeeping:\n  runs:\n    archive_after: null\n"
        )
        assert [a for a in _plan(tree).actions if a.kind == "move"] == []
