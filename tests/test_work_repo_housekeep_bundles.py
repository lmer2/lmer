"""`work housekeep` bundles and buckets areas (plan task 45, #371 part 6)."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.conftest import strip_lmer_env
from tests.schema2_fixtures import DEFAULT_PROJECT, RecordSpec, build_schema2_plexus, uid_for
from work_repo import housekeep, run_state



@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    strip_lmer_env(monkeypatch)


def _stopped_record(tree, uid):
    tree.add(DEFAULT_PROJECT, RecordSpec(
        uid, "chat", "2026-01-01T10:00:00Z", "2026-01-01T10:05:00Z",
        status="complete", stop_reason="complete", signed_out=True))
    return tree.record_dir(DEFAULT_PROJECT, uid)


def _bundle(rdir, slug="mp-a", name="spec.md"):
    bundle = rdir / "masterplan" / slug
    bundle.mkdir(parents=True)
    (bundle / name).write_text("spec\n")
    return bundle


def _plan(tree, areas=("bundles", "buckets")):
    policy = housekeep.resolve_policy(tree.root)
    entries = housekeep.project_dirs(tree.root)
    return housekeep.build_plan(tree.root, entries, policy, list(areas))


def _apply(tree, plan):
    def fake_git(args, cwd):
        if args[0] == "mv":
            (cwd / args[1]).rename(cwd / args[2])
        return 0, ""

    with patch("work_repo.housekeep._run_git", fake_git):
        return housekeep._apply_changes(tree.root, plan)


def test_a_single_nested_bundle_is_flattened_and_links_retargeted(tmp_path):
    tree = build_schema2_plexus(tmp_path / "plexus")
    rdir = _stopped_record(tree, uid_for(1))
    _bundle(rdir)
    (rdir / "spec.md").symlink_to("masterplan/mp-a/spec.md")
    specs = tree.project_dir() / "_specs"
    specs.mkdir()
    link = specs / "2026-01-01-chat--spec.md"
    link.symlink_to(os.path.relpath(rdir / "masterplan/mp-a/spec.md", specs))
    plan = _plan(tree)
    moves = [a for a in plan.actions if a.kind == "move"]
    assert len(moves) == 1
    _apply(tree, plan)
    assert (rdir / "masterplan/spec.md").is_file()
    assert not (rdir / "masterplan/mp-a").exists()
    assert os.readlink(rdir / "spec.md") == "masterplan/spec.md"
    assert link.resolve() == (rdir / "masterplan/spec.md").resolve()


def test_two_bundles_are_reported_and_untouched(tmp_path):
    tree = build_schema2_plexus(tmp_path / "plexus")
    rdir = _stopped_record(tree, uid_for(1))
    _bundle(rdir, "mp-a")
    _bundle(rdir, "mp-b")
    before = sorted(p.relative_to(rdir).as_posix() for p in rdir.rglob("*"))
    plan = _plan(tree)
    assert [a for a in plan.actions if a.kind == "move"] == []
    assert any("nested bundles" in (a.detail or "") for a in plan.actions)
    assert sorted(p.relative_to(rdir).as_posix() for p in rdir.rglob("*")) == before


def test_a_live_records_bundle_is_untouched(tmp_path):
    tree = build_schema2_plexus(tmp_path / "plexus")
    tree.add(DEFAULT_PROJECT, RecordSpec(
        uid_for(1), "chat", "2026-01-01T10:00:00Z", "2026-01-01T10:05:00Z",
        status="in-progress", signed_out=False))
    rdir = tree.record_dir(DEFAULT_PROJECT, uid_for(1))
    _bundle(rdir)
    plan = _plan(tree)
    assert [a for a in plan.actions if a.kind == "move"] == []
    assert any("live" in (a.detail or "") for a in plan.actions)


def test_a_bucket_is_listed_with_its_adopt_line(tmp_path):
    tree = build_schema2_plexus(tmp_path / "plexus")
    bucket = tree.root / "git.example.test/unnamed/widget"
    (bucket / "_runs").mkdir(parents=True)
    infos = [a for a in _plan(tree, ("buckets",)).actions if a.kind == "info"]
    assert any(a.detail.startswith("work adopt git.example.test/unnamed/widget") for a in infos)


def test_a_failed_move_skips_the_rmdir(tmp_path):
    tree = build_schema2_plexus(tmp_path / "plexus")
    rdir = _stopped_record(tree, uid_for(1))
    bundle = _bundle(rdir)
    plan = _plan(tree)
    with patch("work_repo.housekeep._run_git", lambda args, cwd: (1, "boom")):
        _touched, _lines, failures = housekeep._apply_changes(tree.root, plan)
    assert failures
    assert bundle.is_dir() and (bundle / "spec.md").is_file()


def test_a_bundle_with_a_subdir_is_not_half_flattened(tmp_path):
    tree = build_schema2_plexus(tmp_path / "plexus")
    rdir = _stopped_record(tree, uid_for(1))
    bundle = _bundle(rdir)
    (bundle / "extra").mkdir()
    (bundle / "extra" / "keep.md").write_text("keep\n")
    plan = _plan(tree)
    assert [a for a in plan.actions if a.kind == "move"] == []
    assert any("subdirs" in (a.detail or "") for a in plan.actions)
    _apply(tree, plan)
    assert (bundle / "spec.md").is_file()
    assert (bundle / "extra/keep.md").is_file()


def test_a_destination_collision_leaves_the_bundle_untouched(tmp_path):
    tree = build_schema2_plexus(tmp_path / "plexus")
    rdir = _stopped_record(tree, uid_for(1))
    bundle = _bundle(rdir)
    (bundle / "notes.md").write_text("bundle notes\n")
    (rdir / "masterplan/notes.md").write_text("flat notes\n")
    plan = _plan(tree)
    assert [a for a in plan.actions if a.kind == "move"] == []
    assert any("collision" in (a.detail or "") for a in plan.actions)


def test_masterplan_that_is_already_a_bundle_is_not_flattened(tmp_path):
    tree = build_schema2_plexus(tmp_path / "plexus")
    rdir = _stopped_record(tree, uid_for(1))
    mp = rdir / "masterplan"
    (mp / "old").mkdir(parents=True)
    (mp / "spec.md").write_text("spec\n")
    (mp / "old" / "plan.md").write_text("plan\n")
    plan = _plan(tree)
    assert [a for a in plan.actions if a.kind == "move"] == []


def test_a_flat_bundle_with_subdirs_is_not_flattened(tmp_path):
    tree = build_schema2_plexus(tmp_path / "plexus")
    rdir = _stopped_record(tree, uid_for(1))
    mp = rdir / "masterplan"
    (mp / "reports").mkdir(parents=True)
    (mp / "spec.md").write_text("spec\n")
    (mp / "reports" / "x.md").write_text("x\n")
    plan = _plan(tree)
    assert [a for a in plan.actions if a.kind == "move"] == []


def test_a_second_run_is_a_noop(tmp_path):
    tree = build_schema2_plexus(tmp_path / "plexus")
    rdir = _stopped_record(tree, uid_for(1))
    _bundle(rdir)
    _apply(tree, _plan(tree))
    assert [a for a in _plan(tree).actions if a.kind == "move"] == []
