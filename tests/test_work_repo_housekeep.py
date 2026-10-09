"""`work housekeep` — the frame and its registration (plan task 39)."""
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.conftest import strip_lmer_env
from tests.plexus_fixtures import build_legacy_plexus, build_schema_plexus
from work_repo import cli, housekeep


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

    def test_an_invalid_value_refuses_the_area(self, tmp_path):
        tree = build_schema_plexus(tmp_path / "plexus")
        _manifest_with(tree.root, "  napkin:\n    archive_after: 30\n")
        policy = housekeep.resolve_policy(tree.root)
        assert any("manifest.housekeeping-policy-invalid" in e for e in policy.errors)

    def test_a_legacy_tree_gets_defaults_and_the_info_line(self, tmp_path, capsys, monkeypatch):
        tree = build_legacy_plexus(tmp_path / "plexus-legacy")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
        policy = housekeep.resolve_policy(tree.root)
        assert policy.legacy is True
        assert housekeep.cmd_housekeep(_args()) == 0
        assert "not configurable" in capsys.readouterr().out


class TestPlanAndApply:
    def _planner(self, monkeypatch, action):
        def planner(plan, *, root, project, policy):
            plan.add(action)
        monkeypatch.setitem(housekeep.PLANNERS, "plans", planner)

    def test_a_dry_run_changes_nothing(self, tmp_path, monkeypatch, capsys):
        tree = build_schema_plexus(tmp_path / "plexus")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tree.root))
        self._planner(monkeypatch, housekeep.Action(
            "move", "plans", "lmer/_plans/a.md", "lmer/_archive/_plans/a.md", "30d"))
        with patch("work_repo.housekeep.git_ops.commit_work_path") as commit:
            assert housekeep.cmd_housekeep(_args()) == 0
        commit.assert_not_called()
        assert "move" in capsys.readouterr().out

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
    def test_dry_run_journals_nothing_and_apply_has_a_scope(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
        assert cli._write_intent_rel_paths(SimpleNamespace(command="housekeep", apply=False)) is None
        assert cli._write_intent_rel_paths(SimpleNamespace(command="housekeep", apply=True)) is not None

    def test_housekeep_is_exempt_from_the_uid_gate(self):
        import inspect

        source = inspect.getsource(cli.main)
        assert '"housekeep"' in source

    def test_help_lists_every_area(self):
        parser = cli.create_parser()
        import argparse

        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        assert "housekeep" in sub.choices
