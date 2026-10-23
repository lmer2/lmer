"""`work housekeep` end-to-end acceptance (plan task 46, #371)."""
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.conftest import strip_lmer_env
from tests.schema2_fixtures import DEFAULT_PROJECT, RecordSpec, build_schema2_plexus, uid_for
from work_repo import housekeep, run_state

MANIFEST = (
    "schema: 2\nname: example\nprojects:\n  widgets:\n"
    "    repo: git.example.test/acme/widgets\n"
    "  gadgets:\n"
    "    repo: git.example.test/acme/gadgets\n"
    "housekeeping:\n"
    "  napkin:\n    archive_after: 365d\n"
    "  plans:\n    archive_after: 30d\n"
    "  runs:\n    archive_after: 30d\n"
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    strip_lmer_env(monkeypatch)


def _git(root, *args, env=None):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(root), check=True, capture_output=True, text=True, env=env,
    )


def _build(tmp_path):
    tree = build_schema2_plexus(tmp_path / "plexus")
    (tree.root / "_lmer.yaml").write_text(MANIFEST)
    # 1. over-TTL napkin note
    note = tree.root / "_napkin/acme/topic/note.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Note\n")
    # 2. done plan past its TTL
    plans = tree.project_dir() / "_plans"
    plans.mkdir(parents=True)
    (plans / "done.md").write_text("# Done\n")
    (plans / "PLANS.md").write_text("# Plans\n\n- [Done](done.md) — done\n")
    # 3. complete signed-out session past the runs TTL, with a nested bundle
    tree.add(DEFAULT_PROJECT, RecordSpec(
        uid_for(1), "chat", "2025-01-01T10:00:00Z", "2025-01-01T10:05:00Z",
        status="complete", stop_reason="complete", signed_out=True))
    rdir = tree.record_dir(DEFAULT_PROJECT, uid_for(1))
    bundle = rdir / "masterplan/mp-a"
    bundle.mkdir(parents=True)
    (bundle / "spec.md").write_text("spec\n")
    (rdir / "spec.md").symlink_to("masterplan/mp-a/spec.md")
    # 4. dangling index entry
    instance = tree.root / "_agents/memory"
    instance.mkdir(parents=True)
    (instance / "MEMORY.md").write_text(
        "# Memory\n\n- [Gone](gone.md) — gone\n"
    )
    # 5. a second REGISTERED project (so the instance scope is not planned
    #    per project) and a host bucket
    (tree.root / "gadgets/_runs").mkdir(parents=True)
    (tree.root / "git.example.test/unnamed/widget/_runs").mkdir(parents=True)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    _git(tree.root, "init", "-q", "-b", "main")
    _git(tree.root, "remote", "add", "origin", str(origin))
    _git(tree.root, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_DATE": "2025-01-01T00:00:00Z",
           "GIT_COMMITTER_DATE": "2025-01-01T00:00:00Z"}
    _git(tree.root, "commit", "-qm", "seed", env=env)
    _git(tree.root, "push", "-qu", "origin", "main")
    return tree, rdir


def _args(**over):
    base = dict(apply=False, area=None, project=None, archive_stale=False)
    base.update(over)
    return SimpleNamespace(**base)


def test_the_dry_run_lists_the_moves_and_leaves_head_unchanged(tmp_path, monkeypatch, capsys):
    tree, _ = _build(tmp_path)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
    head = _git(tree.root, "rev-parse", "HEAD").stdout
    assert housekeep.cmd_housekeep(_args()) == 0
    out = capsys.readouterr().out
    assert "move:" in out and "tombstone:" in out and "info:" in out
    assert "work adopt git.example.test/unnamed/widget" in out
    assert _git(tree.root, "rev-parse", "HEAD").stdout == head
    assert (tree.root / "_napkin/acme/topic/note.md").exists()


def test_apply_makes_one_real_commit_and_the_tree_validates(tmp_path, monkeypatch):
    """A REAL `--apply` commit, then `lmer-schema validate` exits 0."""
    import sys

    tree, rdir = _build(tmp_path)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
    # CI has no gitconfig; give the work-repo commit an explicit identity.
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(name, "test")
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(name, "test@example.com")
    before = _git(tree.root, "rev-list", "--count", "HEAD").stdout.strip()
    assert housekeep.cmd_housekeep(_args(apply=True)) == 0
    after = _git(tree.root, "rev-list", "--count", "HEAD").stdout.strip()
    assert int(after) == int(before) + 1
    message = _git(tree.root, "log", "-1", "--format=%B").stdout
    assert message.startswith("housekeep: ")
    assert "_archive/_napkin/acme/topic/note.md" in message
    assert (tree.root / "_archive/_napkin/acme/topic/note.md").is_file()
    assert (tree.project_dir() / "_archive/_plans/done.md").is_file()
    archived = tree.project_dir() / "_archive/_runs/chat" / uid_for(1)
    assert archived.is_dir()
    assert (archived / "masterplan/spec.md").is_file()
    assert not (rdir / "masterplan/spec.md").exists()
    proc = subprocess.run(
        [sys.executable, "-m", "lmer_schema.cli", "validate", str(tree.root)],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_instance_scope_is_planned_once_with_two_projects(tmp_path, monkeypatch):
    tree, _ = _build(tmp_path)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
    policy = housekeep.resolve_policy(tree.root)
    entries = housekeep.project_dirs(tree.root)
    assert len([e for e in entries if e.kind == "registered"]) == 2
    plan = housekeep.build_plan(tree.root, entries, policy, list(housekeep.AREAS))
    napkin_moves = [a for a in plan.actions if a.kind == "move" and a.area == "napkin"]
    assert len(napkin_moves) == 1
    instance_tombstones = [
        a for a in plan.actions
        if a.kind == "tombstone" and a.src == "_agents/memory/MEMORY.md"
    ]
    assert len(instance_tombstones) == 1


def test_a_second_apply_makes_no_commit(tmp_path, monkeypatch):
    tree, _ = _build(tmp_path)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
    with patch("work_repo.housekeep.git_ops.commit_work_path", return_value=0):
        housekeep.cmd_housekeep(_args(apply=True))
    with patch("work_repo.housekeep.git_ops.commit_work_path") as commit:
        assert housekeep.cmd_housekeep(_args(apply=True)) == 0
    commit.assert_not_called()


def test_writers_never_touch_a_moved_record(tmp_path, monkeypatch):
    tree, _ = _build(tmp_path)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))

    def boom(*args, **kwargs):
        raise AssertionError("a writer touched the moved record")

    for name in ("write_state", "append_event", "freeze_run_dir"):
        monkeypatch.setattr(run_state, name, boom)
    with patch("work_repo.housekeep.git_ops.commit_work_path", return_value=0):
        assert housekeep.cmd_housekeep(_args(apply=True)) == 0


def test_in_session_each_move_is_one_event_in_the_callers_record(tmp_path, monkeypatch):
    tree, _ = _build(tmp_path)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
    uid = uid_for(9)
    caller = tree.project_dir() / "_runs/chat" / uid
    caller.mkdir(parents=True)
    (caller / "state.yaml").write_text(
        f"schema: 2\nslug: chat\nstatus: in-progress\nsession:\n  uid: {uid}\n"
    )
    monkeypatch.setenv("LMER_SESSION_ID", uid)
    monkeypatch.setattr(housekeep, "_caller_record", lambda: caller)
    with patch("work_repo.housekeep.git_ops.commit_work_path", return_value=0) as commit:
        assert housekeep.cmd_housekeep(_args(apply=True)) == 0
    events = run_state.read_events(caller, last_n=0)
    assert events and all(e["type"] == "housekeep_move" for e in events)
    assert any(p.endswith("events.jsonl") for p in commit.call_args[0][0])


def test_a_legacy_tree_moves_a_complete_run(tmp_path, monkeypatch, capsys):
    from tests.plexus_fixtures import build_legacy_plexus
    import yaml

    tree = build_legacy_plexus(tmp_path / "plexus-legacy")
    rdir = tree.session_dirs[0]
    state = run_state._read_sibling_state(rdir)
    state["status"] = "complete"
    state["updated"] = "2025-01-01T00:00:00Z"
    run_state._state_path(rdir).write_text(yaml.safe_dump(state, sort_keys=False))
    _git(tree.root, "init", "-q")
    _git(tree.root, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_DATE": "2025-01-01T00:00:00Z",
           "GIT_COMMITTER_DATE": "2025-01-01T00:00:00Z"}
    _git(tree.root, "commit", "-qm", "seed", env=env)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
    assert housekeep.cmd_housekeep(_args()) == 0
    assert "not configurable" in capsys.readouterr().out
    plan = housekeep.build_plan(
        tree.root, housekeep.project_dirs(tree.root),
        housekeep.resolve_policy(tree.root), ["runs"],
        now=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    moves = [a for a in plan.actions if a.kind == "move"]
    assert moves and "/runs/archive/" in moves[0].dst
