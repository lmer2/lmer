"""`work housekeep` plans area (plan task 42, #371 part 3)."""
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.conftest import strip_lmer_env
from tests.schema2_fixtures import DEFAULT_PROJECT, build_schema2_plexus
from work_repo import housekeep, run_state

MANIFEST = (
    "schema: 2\nname: example\nprojects:\n  widgets:\n"
    "    repo: git.example.test/acme/widgets\n"
    "housekeeping:\n  plans:\n    archive_after: 30d\n"
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    strip_lmer_env(monkeypatch)


def _git(root, *args, env=None):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(root), check=True, capture_output=True, env=env,
    )


def _build(tmp_path, *, line="done", commit_date="2026-01-01T00:00:00Z",
           plan="done-plan.md", body="# Done plan\n"):
    tree = build_schema2_plexus(tmp_path / "plexus")
    (tree.root / "_plexus.yaml").write_text(MANIFEST)
    plans = tree.project_dir() / "_plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / plan).write_text(body)
    (plans / "PLANS.md").write_text(
        f"# Plans\n\nSome prose.\n\n- [Done plan]({plan}) — {line}\n"
    )
    _git(tree.root, "init", "-q")
    _git(tree.root, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_DATE": commit_date,
           "GIT_COMMITTER_DATE": commit_date}
    _git(tree.root, "commit", "-qm", "plans", env=env)
    return tree


def _plan(tree, *, now=None):
    policy = housekeep.resolve_policy(tree.root)
    entries = housekeep.project_dirs(tree.root, DEFAULT_PROJECT)
    return housekeep.build_plan(
        tree.root, entries, policy, ["plans"],
        now=now or datetime(2027, 1, 1, tzinfo=timezone.utc),
    )


def _apply(tree, plan):
    def fake_git(args, cwd):
        if args[0] == "mv":
            (cwd / args[1]).rename(cwd / args[2])
        return 0, ""

    with patch("work_repo.housekeep._run_git", fake_git):
        return housekeep._apply_changes(tree.root, plan)


def test_a_done_plan_past_the_ttl_moves_and_its_line_is_repointed(tmp_path):
    tree = _build(tmp_path)
    plan = _plan(tree)
    moves = [a for a in plan.actions if a.kind == "move"]
    assert len(moves) == 1 and moves[0].dst.endswith("_archive/_plans/done-plan.md")
    tombstones = [a for a in plan.actions if a.kind == "tombstone"]
    assert len(tombstones) == 1
    touched, lines, failures = _apply(tree, plan)
    assert failures == []
    assert (tree.project_dir() / "_archive/_plans/done-plan.md").is_file()
    index = (tree.project_dir() / "_plans/PLANS.md").read_text()
    assert "../_archive/_plans/done-plan.md" in index


def test_a_not_done_plan_stays(tmp_path):
    tree = _build(tmp_path, line="in progress")
    assert [a for a in _plan(tree).actions if a.kind == "move"] == []


def test_a_done_plan_within_the_ttl_stays(tmp_path):
    # Within 30 days of the plan's now (2027-01-01).
    tree = _build(tmp_path, commit_date="2026-12-20T00:00:00Z")
    assert [a for a in _plan(tree).actions if a.kind == "move"] == []


@pytest.mark.parametrize("line", [
    "done in 2024, kept for reference",
    "50% complete",
    "2/3 done",
])
def test_prose_or_a_residue_done_word_is_not_archived(tmp_path, line):
    tree = _build(tmp_path, line=line)
    actions = _plan(tree).actions
    assert [a for a in actions if a.kind == "move"] == []
    assert any("ambiguous" in (a.detail or "") for a in actions)


def test_null_disables_the_area(tmp_path):
    tree = _build(tmp_path)
    (tree.root / "_plexus.yaml").write_text(
        MANIFEST.replace("archive_after: 30d", "archive_after: null")
    )
    assert [a for a in _plan(tree).actions if a.kind == "move"] == []


def test_headings_and_other_lines_stay_byte_identical(tmp_path):
    tree = _build(tmp_path)
    plan = _plan(tree)
    _apply(tree, plan)
    index = (tree.project_dir() / "_plans/PLANS.md").read_text()
    assert index == (
        "# Plans\n\nSome prose.\n\n"
        "- [Done plan](../_archive/_plans/done-plan.md) — done\n"
    )


def test_a_second_run_is_a_noop(tmp_path):
    tree = _build(tmp_path)
    plan = _plan(tree)
    _apply(tree, plan)
    assert [a for a in _plan(tree).actions if a.kind == "move"] == []
