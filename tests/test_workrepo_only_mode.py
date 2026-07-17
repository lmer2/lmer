"""Work-repo-only mode integration tests (docs/TASKDEFS.md).

The minimal deployment is one work repo and nothing else: no
``LMER_TASKDEF_REPO``, no ``LMER_NAPKIN_REPO``. The work repo alone serves
both taskdefs (``{work_repo}/taskdef/``, schema-2 bodies extending the
builtin base across tiers) and napkin (``{work_repo}/napkin/``, captured by
``work commit`` via the subdir-mode path).

These are integration tests over real git repositories; the unit-level
napkin subdir mechanics live in ``tests/test_work_repo_git_ops.py``
(``TestCommitNapkinIfSubdir``) and the taskdef tier-resolution units in
``tests/test_work_repo_taskdefs.py`` — this module proves the two work
together end-to-end in work-repo-only mode.
"""
import subprocess
from pathlib import Path

import pytest

from hooks.start import find_taskdef_file, render_taskdef_template
from work_repo.cli import cmd_commit
from tests.conftest import strip_lmer_env

REPO_TASKDEF = Path(__file__).parent.parent / "taskdef"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture(autouse=True)
def _repo_builtin_root(monkeypatch):
    monkeypatch.setattr(
        "hooks.start.builtin_taskdef_root", lambda: REPO_TASKDEF
    )
    monkeypatch.setenv("LMER_TASKDEF_ROOT", str(REPO_TASKDEF))


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


@pytest.fixture
def work_repo(tmp_path):
    """A real work repo cloned from a bare remote, ready to push."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(["init", "--bare", "--initial-branch=main"], remote)
    work = tmp_path / "work"
    _git(["clone", str(remote), str(work)], tmp_path)
    _git(["config", "user.email", "t@example.com"], work)
    _git(["config", "user.name", "T"], work)
    (work / "README.md").write_text("work repo\n")
    _git(["add", "README.md"], work)
    _git(["commit", "-m", "init"], work)
    _git(["push", "-u", "origin", "main"], work)
    return work, remote


class TestWorkRepoOnlyTaskdefs:
    """Taskdefs served from {work_repo}/taskdef/ with no other source."""

    def _seed_taskdef_tier(self, work):
        tier = work / "taskdef"
        (tier / "wodemo").mkdir(parents=True)
        (tier / "taskdef.yaml").write_text("schema: 2\n")
        (tier / "wodemo" / "instructions.txt").write_text(
            "{% extends 'base-task.jinja2' %}\n"
            "{% block intro %}# Work-repo-only Demo{% endblock %}\n"
            "{% block task_phases %}## Phase 1: WO work{% endblock %}\n"
        )
        return tier

    def test_resolution_and_render_across_tiers(
        self, work_repo, monkeypatch, capsys
    ):
        """The body resolves from the work-repo global tier and extends the
        builtin base across tiers — the whole point of work-repo-only
        mode."""
        work, _ = work_repo
        tier = self._seed_taskdef_tier(work)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        monkeypatch.setenv("LMER_TASK", "wodemo")
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")

        resolved = find_taskdef_file("instructions.txt")
        assert resolved == tier / "wodemo" / "instructions.txt"

        out = render_taskdef_template(
            resolved, {"work_mode": "finish", "run_state_brief": ""}
        )
        assert out.startswith("# Work-repo-only Demo")
        assert "## Phase 1: WO work" in out
        assert "## Phase -1: Branch Setup" in out  # inherited builtin spine
        banner = capsys.readouterr().out
        assert f"taskdef source: {tier} (schema 2)" in banner
        assert (
            f"taskdef source (base-task.jinja2): {REPO_TASKDEF} (schema 1)"
            in banner
        )


class TestWorkRepoOnlyNapkin:
    """Napkin at {work_repo}/napkin/ is captured by `work commit` in subdir
    mode — no separate napkin repo configured."""

    def test_napkin_subdir_note_lands_on_remote(self, work_repo, monkeypatch):
        work, remote = work_repo
        napkin = work / "napkin"
        (napkin / "org-a").mkdir(parents=True)
        (napkin / "org-a" / "finding.md").write_text("a durable note\n")

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        monkeypatch.setenv("LMER_NAPKIN_PATH", str(napkin))
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "grp/proj")
        monkeypatch.setenv("LMER_TASK", "develop")
        monkeypatch.setenv("LMER_TASK_TARGET", "issue-1")

        assert cmd_commit("capture napkin") == 0

        remote_files = _git(
            ["ls-tree", "-r", "HEAD", "--name-only"], remote
        ).stdout.splitlines()
        assert "napkin/org-a/finding.md" in remote_files

    def test_full_workrepo_only_session_shape(self, work_repo, monkeypatch):
        """One work repo serves both: taskdef renders AND napkin commits —
        the minimal deployment end-to-end."""
        work, remote = work_repo
        tier = work / "taskdef"
        (tier / "wodemo").mkdir(parents=True)
        (tier / "taskdef.yaml").write_text("schema: 2\n")
        (tier / "wodemo" / "instructions.txt").write_text(
            "{% extends 'base-task.jinja2' %}\n"
            "{% block intro %}# WO Session{% endblock %}\n"
            "{% block task_phases %}## Phase 1: go{% endblock %}\n"
        )
        napkin = work / "napkin"
        napkin.mkdir()
        (napkin / "note.md").write_text("session note\n")

        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        monkeypatch.setenv("LMER_NAPKIN_PATH", str(napkin))
        monkeypatch.setenv("LMER_TASK", "wodemo")
        monkeypatch.setenv("LMER_TASK_TARGET", "issue-2")
        monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
        monkeypatch.setenv("LMER_REPO_PROJECT", "grp/proj")

        # Taskdef side: resolve + render.
        resolved = find_taskdef_file("instructions.txt")
        out = render_taskdef_template(
            resolved, {"work_mode": "finish", "run_state_brief": ""}
        )
        assert out.startswith("# WO Session")

        # Napkin side: `work commit` captures the subdir note.
        assert cmd_commit(None) == 0
        remote_files = _git(
            ["ls-tree", "-r", "HEAD", "--name-only"], remote
        ).stdout.splitlines()
        assert "napkin/note.md" in remote_files
        # The taskdef tier itself is work-repo content too — commit it the
        # same way a real deployment would and confirm nothing breaks.
        _git(["add", "taskdef"], work)
        _git(["commit", "-m", "taskdef tier"], work)
        _git(["push"], work)
        remote_files = _git(
            ["ls-tree", "-r", "HEAD", "--name-only"], remote
        ).stdout.splitlines()
        assert "taskdef/wodemo/instructions.txt" in remote_files
