"""`work housekeep` — the frame and its registration (plan task 39)."""
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.conftest import strip_lmer_env
from tests.plexus_fixtures import build_legacy_plexus, build_schema_plexus
from tests.schema2_fixtures import uid_for
from work_repo import cli, housekeep, run_state


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    strip_lmer_env(monkeypatch)


def _args(**over):
    base = dict(apply=False, area=None, project=None, archive_stale=False)
    base.update(over)
    return SimpleNamespace(**base)


def _manifest_with(root, housekeeping):
    (root / "_plexus.yaml").write_text(
        "schema: 2\nname: example\nprojects:\n  lmer:\n"
        "    repo: gitlab.example.com/group/project\n"
        f"housekeeping:\n{housekeeping}"
    )


class TestPolicy:
    def test_defaults_when_the_manifest_says_nothing(self, tmp_path):
        tree = build_schema_plexus(tmp_path / "plexus")
        policy = housekeep.resolve_policy(tree.root)
        assert policy.archive_after == housekeep.DEFAULT_ARCHIVE_AFTER
        assert policy.errors == () and policy.legacy is False

    def test_a_duration_null_and_an_override(self, tmp_path):
        tree = build_schema_plexus(tmp_path / "plexus")
        _manifest_with(tree.root, "  napkin:\n    archive_after: 30d\n  plans:\n    archive_after: null\n")
        policy = housekeep.resolve_policy(tree.root)
        assert policy.archive_after["napkin"] == 30
        assert policy.archive_after["plans"] is None
        assert policy.errors == ()

    def test_an_invalid_value_refuses_only_its_area(self, tmp_path):
        tree = build_schema_plexus(tmp_path / "plexus")
        _manifest_with(tree.root, "  napkin:\n    archive_after: 30\n")
        policy = housekeep.resolve_policy(tree.root)
        assert any("manifest.housekeeping-policy-invalid" in e for e in policy.errors)
        assert policy.refused == ("napkin",)
        # A different selected area still plans.
        called = []
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setitem(
            housekeep.PLANNERS, "plans",
            lambda plan, **kw: called.append(kw["policy"]),
        )
        try:
            housekeep.build_plan(tree.root, housekeep.project_dirs(tree.root), policy, ["plans", "napkin"])
        finally:
            monkeypatch.undo()
        assert called  # the non-refused area ran

    def test_a_legacy_tree_gets_defaults_and_the_info_line(self, tmp_path, capsys, monkeypatch):
        tree = build_legacy_plexus(tmp_path / "plexus-legacy")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
        policy = housekeep.resolve_policy(tree.root)
        assert policy.legacy is True
        assert housekeep.cmd_housekeep(_args()) == 0
        assert "not configurable" in capsys.readouterr().out


class TestPlanAndApply:
    def _planner(self, monkeypatch, action):
        def planner(plan, *, root, project, policy, **_kwargs):
            plan.add(action)
        monkeypatch.setitem(housekeep.PLANNERS, "plans", planner)

    def test_a_dry_run_changes_nothing(self, tmp_path, monkeypatch, capsys):
        tree = build_schema_plexus(tmp_path / "plexus")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
        src = tree.root / "lmer/_plans/a.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("plan\n")
        before = {p: p.read_bytes() for p in tree.root.rglob("*") if p.is_file()}
        self._planner(monkeypatch, housekeep.Action(
            "move", "plans", "lmer/_plans/a.md", "lmer/_archive/_plans/a.md", "30d"))
        with patch("work_repo.housekeep.git_ops.commit_work_path") as commit:
            assert housekeep.cmd_housekeep(_args()) == 0
        commit.assert_not_called()
        assert src.exists()
        assert {p: p.read_bytes() for p in tree.root.rglob("*") if p.is_file()} == before
        assert "move" in capsys.readouterr().out

    def test_a_failed_move_exits_nonzero(self, tmp_path, monkeypatch):
        tree = build_schema_plexus(tmp_path / "plexus")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
        self._planner(monkeypatch, housekeep.Action(
            "move", "plans", "lmer/_plans/missing.md", "lmer/_archive/_plans/missing.md", "30d"))
        monkeypatch.setattr(housekeep, "_run_git", lambda args, cwd: (1, "boom"))
        with patch("work_repo.housekeep.git_ops.commit_work_path") as commit:
            assert housekeep.cmd_housekeep(_args(apply=True, project="lmer")) == 1
        commit.assert_not_called()

    def test_an_unregistered_action_kind_is_a_hard_failure(self, tmp_path, monkeypatch):
        tree = build_schema_plexus(tmp_path / "plexus")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
        self._planner(monkeypatch, housekeep.Action("tombstone", "plans", "lmer/_plans/a.md"))
        with patch("work_repo.housekeep.git_ops.commit_work_path") as commit:
            assert housekeep.cmd_housekeep(_args(apply=True, project="lmer")) == 1
        commit.assert_not_called()

    def test_in_session_each_move_is_one_event_in_the_callers_record(
        self, tmp_path, monkeypatch
    ):
        tree = build_schema_plexus(tmp_path / "plexus")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
        uid = uid_for(9)
        caller = tree.project_dir / "_runs" / "chat" / uid
        caller.mkdir(parents=True)
        (caller / "state.yaml").write_text(
            f"schema: 2\nslug: chat\nstatus: in-progress\nsession:\n  uid: {uid}\n"
        )
        monkeypatch.setenv("LMER_SESSION_ID", uid)
        monkeypatch.setattr(housekeep, "_caller_record", lambda: caller)
        src = tree.root / "lmer/_plans/a.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("plan\n")
        self._planner(monkeypatch, housekeep.Action(
            "move", "plans", "lmer/_plans/a.md", "lmer/_archive/_plans/a.md", "30d"))

        def fake_git(args, cwd):
            if args[0] == "mv":
                (cwd / args[1]).rename(cwd / args[2])
            return 0, ""

        monkeypatch.setattr(housekeep, "_run_git", fake_git)
        with patch("work_repo.housekeep.git_ops.commit_work_path", return_value=0) as commit:
            assert housekeep.cmd_housekeep(_args(apply=True, project="lmer")) == 0
        events = run_state.read_events(caller, last_n=0)
        assert [e["type"] for e in events] == ["housekeep_move"]
        assert any(p.endswith("events.jsonl") for p in commit.call_args[0][0])

    def test_apply_makes_one_commit_with_the_move_lines(self, tmp_path, monkeypatch):
        tree = build_schema_plexus(tmp_path / "plexus")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
        src = tree.root / "lmer/_plans/a.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("plan\n")
        self._planner(monkeypatch, housekeep.Action(
            "move", "plans", "lmer/_plans/a.md", "lmer/_archive/_plans/a.md", "30d"))

        def fake_git(args, cwd):
            if args[0] == "mv":
                (cwd / args[1]).rename(cwd / args[2])
            return 0, ""

        monkeypatch.setattr(housekeep, "_run_git", fake_git)
        with patch("work_repo.housekeep.git_ops.commit_work_path", return_value=0) as commit:
            assert housekeep.cmd_housekeep(_args(apply=True, project="lmer")) == 0
        commit.assert_called_once()
        paths, message = commit.call_args[0][:2]
        assert "lmer/_plans/a.md" in paths and "lmer/_archive/_plans/a.md" in paths
        assert "lmer/_plans/a.md -> lmer/_archive/_plans/a.md" in message

    def test_a_findings_only_run_makes_no_commit(self, tmp_path, monkeypatch, capsys):
        tree = build_schema_plexus(tmp_path / "plexus")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
        self._planner(monkeypatch, housekeep.Action("info", "plans", "lmer", detail="nothing"))
        with patch("work_repo.housekeep.git_ops.commit_work_path") as commit:
            assert housekeep.cmd_housekeep(_args(apply=True)) == 0
        commit.assert_not_called()


class TestRegistration:
    def test_dry_run_journals_nothing_and_apply_has_a_project_scope(self, tmp_path, monkeypatch):
        tree = build_schema_plexus(tmp_path / "plexus")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
        assert cli._write_intent_rel_paths(
            SimpleNamespace(command="housekeep", apply=False)) is None
        rels = cli._write_intent_rel_paths(
            SimpleNamespace(command="housekeep", apply=True, project=None))
        assert rels is not None
        assert "lmer" in rels  # a project dir, not just a root-level area

    def test_a_schema_two_tree_without_a_session_is_not_refused_by_the_uid_gate(
        self, tmp_path, monkeypatch, capsys
    ):
        tree = build_schema_plexus(tmp_path / "plexus")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
        monkeypatch.setattr(sys, "argv", ["work", "housekeep", "--area", "buckets"])
        assert cli.main() == 0
        assert "Session write refused" not in capsys.readouterr().err

    def test_help_lists_every_area(self):
        parser = cli.create_parser()
        import argparse

        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        choices = sub.choices["housekeep"]._actions
        area = next(a for a in choices if a.dest == "area")
        assert tuple(area.choices) == housekeep.AREAS
