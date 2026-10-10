"""`work housekeep` index area (plan task 43, #371 part 4)."""
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import strip_lmer_env
from tests.schema2_fixtures import DEFAULT_PROJECT, build_schema2_plexus
from work_repo import housekeep, memory


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    strip_lmer_env(monkeypatch)


def _build(tmp_path):
    tree = build_schema2_plexus(tmp_path / "plexus")
    instance = tree.root / "_agents" / "memory"
    project = tree.project_dir() / "memory"
    for scope in (instance, project):
        scope.mkdir(parents=True, exist_ok=True)
    # Instance scope: one dangling entry, one with an archive mirror.
    (instance / "MEMORY.md").write_text(
        "# Instance memory\n\n- [Gone](gone.md) — gone\n- [Moved](moved.md) — moved\n"
    )
    (tree.root / "_archive/_agents/memory").mkdir(parents=True)
    (tree.root / "_archive/_agents/memory/moved.md").write_text("archived\n")
    # Project scope: a dangling entry.
    (project / "MEMORY.md").write_text(
        "# Project memory\n\n- [Gone](gone.md) — gone\n"
    )
    # PLANS.md: one dangling, one archived.
    plans = tree.project_dir() / "_plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "PLANS.md").write_text(
        "# Plans\n\n- [Dangling](dangling.md) — done\n- [Retired](retired.md) — done\n"
    )
    (tree.project_dir() / "_archive/_plans").mkdir(parents=True)
    (tree.project_dir() / "_archive/_plans/retired.md").write_text("plan\n")
    (tree.root / "_memory_retirements.json").write_text('{"keep": true}\n')
    return tree


def _plan(tree):
    policy = housekeep.resolve_policy(tree.root)
    entries = housekeep.project_dirs(tree.root, DEFAULT_PROJECT)
    return housekeep.build_plan(
        tree.root, entries, policy, ["index"],
        now=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )


def _apply(tree, plan):
    with patch("work_repo.housekeep._run_git", lambda args, cwd: (0, "")):
        return housekeep._apply_changes(tree.root, plan)


def test_dangling_memory_entries_are_tombstoned_and_archived_ones_repointed(tmp_path):
    tree = _build(tmp_path)
    plan = _plan(tree)
    tombstones = [a for a in plan.actions if a.kind == "tombstone"]
    assert len(tombstones) == 5  # 3 tombstones + 2 re-points
    _apply(tree, plan)
    instance = (tree.root / "_agents/memory/MEMORY.md").read_text()
    assert "archived 2027-01-01" in instance
    # The moved entry is re-pointed, not tombstoned.
    assert "_archive/_agents/memory/moved.md" in instance
    project = (tree.project_dir() / "memory/MEMORY.md").read_text()
    assert "archived 2027-01-01" in project
    plans = (tree.project_dir() / "_plans/PLANS.md").read_text()
    assert "archived 2027-01-01" in plans
    assert "_archive/_plans/retired.md" in plans


def test_every_rewritten_line_is_a_non_entry(tmp_path):
    tree = _build(tmp_path)
    _apply(tree, _plan(tree))
    for index in (
        tree.root / "_agents/memory/MEMORY.md",
        tree.project_dir() / "memory/MEMORY.md",
        tree.project_dir() / "_plans/PLANS.md",
    ):
        for line in index.read_text().splitlines():
            assert memory._entry_targets(line) == []


def test_non_entry_lines_stay_byte_identical(tmp_path):
    tree = _build(tmp_path)
    _apply(tree, _plan(tree))
    instance = (tree.root / "_agents/memory/MEMORY.md").read_text()
    assert instance.startswith("# Instance memory\n\n")


def test_memory_retirements_file_is_unchanged(tmp_path):
    tree = _build(tmp_path)
    path = tree.root / "_memory_retirements.json"
    before = path.read_bytes()
    _apply(tree, _plan(tree))
    assert path.read_bytes() == before


def test_a_second_run_is_a_noop(tmp_path):
    tree = _build(tmp_path)
    _apply(tree, _plan(tree))
    assert [a for a in _plan(tree).actions if a.kind == "tombstone"] == []


def test_no_entry_without_file_finding_afterwards(tmp_path):
    import subprocess
    import sys

    tree = _build(tmp_path)
    _apply(tree, _plan(tree))
    proc = subprocess.run(
        [sys.executable, "-m", "lmer_schema.cli", "validate", str(tree.root), "--json"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
    doc = json.loads(proc.stdout)
    codes = {f.get("code") for f in doc.get("findings", [])}
    assert "index.entry-without-file" not in codes
