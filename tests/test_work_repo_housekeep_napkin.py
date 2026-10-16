"""`work housekeep` napkin area (plan task 44, #371 part 5)."""
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.conftest import strip_lmer_env
from tests.schema2_fixtures import DEFAULT_PROJECT, build_schema2_plexus
from work_repo import housekeep

MANIFEST = (
    "schema: 2\nname: example\nprojects:\n  widgets:\n"
    "    repo: git.example.test/acme/widgets\n"
    "housekeeping:\n  napkin:\n    archive_after: 365d\n"
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    strip_lmer_env(monkeypatch)


def _git(root, *args, env=None):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(root), check=True, capture_output=True, env=env,
    )


def _build(tmp_path, *, commit_date="2025-01-01T00:00:00Z"):
    tree = build_schema2_plexus(tmp_path / "plexus")
    (tree.root / "_lmer.yaml").write_text(MANIFEST)
    note = tree.root / "_napkin/acme/topic/note.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Note\n")
    _git(tree.root, "init", "-q")
    _git(tree.root, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_DATE": commit_date,
           "GIT_COMMITTER_DATE": commit_date}
    _git(tree.root, "commit", "-qm", "napkin", env=env)
    return tree


def _plan(tree):
    policy = housekeep.resolve_policy(tree.root)
    entries = housekeep.project_dirs(tree.root, DEFAULT_PROJECT)
    return housekeep.build_plan(
        tree.root, entries, policy, ["napkin"],
        now=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )


def test_an_over_ttl_note_moves_to_its_mirror_path(tmp_path):
    tree = _build(tmp_path)
    moves = [a for a in _plan(tree).actions if a.kind == "move"]
    assert len(moves) == 1
    assert moves[0].dst == "_archive/_napkin/acme/topic/note.md"


def test_a_recent_note_stays(tmp_path):
    tree = _build(tmp_path, commit_date="2026-12-20T00:00:00Z")
    assert [a for a in _plan(tree).actions if a.kind == "move"] == []


def test_an_untracked_note_stays(tmp_path):
    tree = _build(tmp_path)
    (tree.root / "_napkin/acme/topic/new.md").write_text("# New\n")
    moves = [a for a in _plan(tree).actions if a.kind == "move"]
    assert [m for m in moves if m.src.endswith("new.md")] == []


def test_null_disables_the_area(tmp_path):
    tree = _build(tmp_path)
    (tree.root / "_lmer.yaml").write_text(
        MANIFEST.replace("archive_after: 365d", "archive_after: null")
    )
    assert [a for a in _plan(tree).actions if a.kind == "move"] == []


def test_the_bare_napkin_dir_is_report_only(tmp_path):
    tree = _build(tmp_path)
    bare = tree.root / "napkin/acme/topic"
    bare.mkdir(parents=True)
    (bare / "note.md").write_text("# Bare note\n")
    infos = [a for a in _plan(tree).actions if a.kind == "info"]
    assert any("bare napkin/" in (a.detail or "") for a in infos)


def test_a_second_run_is_a_noop(tmp_path):
    tree = _build(tmp_path)
    plan = _plan(tree)
    moves = [a for a in plan.actions if a.kind == "move"]
    for action in moves:
        dst = tree.root / action.dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        (tree.root / action.src).rename(dst)
    assert [a for a in _plan(tree).actions if a.kind == "move"] == []
