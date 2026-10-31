"""`LMER_WORK_REPO_REF` — point a session's work-repo clone at a branch (#613).

The work repo's manifest decides the writer contract (including whether a
schema-2 uid is minted), so the branch checkout has to happen before the
manifest is read. Publication already follows the current branch's upstream,
which is why only a branch — never a tag or SHA — is accepted.
"""

import io
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lmer_cli.container import clone_and_exec
from tests.conftest import strip_lmer_env


def _git(*args, cwd=None):
    result = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    return path


@pytest.fixture
def upstream(tmp_path):
    """A work repo with `main` (no manifest) and `feature/ref` (a manifest)."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    (repo / "README.md").write_text("main\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "main", cwd=repo)

    _git("switch", "-c", "feature/ref", cwd=repo)
    (repo / "_lmer.yaml").write_text("schema: 2\nname: example\n")
    _git("add", "_lmer.yaml", cwd=repo)
    _git("commit", "-m", "manifest on branch", cwd=repo)
    _git("tag", "v1", cwd=repo)
    _git("switch", "main", cwd=repo)
    return repo


def _clone(upstream, destination):
    _git("clone", str(upstream), str(destination), cwd=destination.parent)


class TestCheckoutWorkRepoBranch:
    def test_fresh_clone_is_switched_to_the_branch_and_tracks_it(
        self, upstream, tmp_path, home
    ):
        work = tmp_path / "work"
        _clone(upstream, work)

        rc = clone_and_exec._checkout_work_repo_branch(work, "feature/ref")

        assert rc == 0
        assert _git("symbolic-ref", "--short", "HEAD", cwd=work) == "feature/ref"
        assert _git(
            "rev-parse", "--abbrev-ref", "feature/ref@{upstream}", cwd=work
        ) == "origin/feature/ref"
        assert (work / "_lmer.yaml").is_file()

    def test_clone_on_the_default_branch_is_switched(self, upstream, tmp_path, home):
        work = tmp_path / "work"
        _clone(upstream, work)
        assert _git("symbolic-ref", "--short", "HEAD", cwd=work) == "main"

        rc = clone_and_exec._checkout_work_repo_branch(work, "feature/ref")

        assert rc == 0
        assert _git("symbolic-ref", "--short", "HEAD", cwd=work) == "feature/ref"

    def test_existing_local_branch_is_fast_forwarded_and_gets_its_upstream(
        self, upstream, tmp_path, home
    ):
        work = tmp_path / "work"
        _clone(upstream, work)
        # A local branch of that name at an old commit, with no upstream —
        # the re-entry shape the fresh-clone path never produces.
        _git("branch", "feature/ref", "main", cwd=work)
        stale = _git("rev-parse", "feature/ref", cwd=work)
        assert stale != _git("rev-parse", "origin/feature/ref", cwd=work)

        rc = clone_and_exec._checkout_work_repo_branch(work, "feature/ref")

        assert rc == 0
        assert _git("symbolic-ref", "--short", "HEAD", cwd=work) == "feature/ref"
        assert _git("rev-parse", "HEAD", cwd=work) == _git(
            "rev-parse", "origin/feature/ref", cwd=work
        )
        assert _git(
            "rev-parse", "--abbrev-ref", "feature/ref@{upstream}", cwd=work
        ) == "origin/feature/ref"
        # The branch's current manifest is what startup would now read.
        assert (work / "_lmer.yaml").is_file()

    def test_existing_local_branch_with_the_wrong_upstream_is_repointed(
        self, upstream, tmp_path, home
    ):
        work = tmp_path / "work"
        _clone(upstream, work)
        _git("branch", "--track", "feature/ref", "origin/main", cwd=work)
        assert _git(
            "rev-parse", "--abbrev-ref", "feature/ref@{upstream}", cwd=work
        ) == "origin/main"

        rc = clone_and_exec._checkout_work_repo_branch(work, "feature/ref")

        assert rc == 0
        assert _git(
            "rev-parse", "--abbrev-ref", "feature/ref@{upstream}", cwd=work
        ) == "origin/feature/ref"

    def test_a_diverged_local_branch_is_refused_not_reset(
        self, upstream, tmp_path, home, capsys
    ):
        work = tmp_path / "work"
        _clone(upstream, work)
        _git("switch", "-c", "feature/ref", "main", cwd=work)
        _git(
            "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "--allow-empty", "-m", "local", cwd=work,
        )
        local_head = _git("rev-parse", "HEAD", cwd=work)

        rc = clone_and_exec._checkout_work_repo_branch(work, "feature/ref")

        assert rc != 0
        assert _git("rev-parse", "HEAD", cwd=work) == local_head
        assert "fast-forward" in capsys.readouterr().err

    def test_a_tag_is_refused_without_moving_head(
        self, upstream, tmp_path, home, capsys
    ):
        work = tmp_path / "work"
        _clone(upstream, work)

        rc = clone_and_exec._checkout_work_repo_branch(work, "v1")

        assert rc == 2
        assert _git("symbolic-ref", "--short", "HEAD", cwd=work) == "main"
        err = capsys.readouterr().err
        assert "LMER_WORK_REPO_REF" in err
        assert "not a branch" in err
        assert "v1" in err

    def test_an_unknown_name_is_refused(self, upstream, tmp_path, home, capsys):
        work = tmp_path / "work"
        _clone(upstream, work)

        rc = clone_and_exec._checkout_work_repo_branch(work, "does-not-exist")

        assert rc == 2
        assert "not a branch" in capsys.readouterr().err


class TestMainOrdering:
    def _run_main(self, upstream, tmp_path, monkeypatch, ref):
        work = tmp_path / "work"

        def fake_ensure_clone(workspace, repo_url, branch, ref_, **kwargs):
            if Path(workspace) == work:
                _clone(upstream, work)
            return None

        observed = []

        def spy_mint(*, fresh=False):
            observed.append((fresh, (work / "_lmer.yaml").exists()))

        for var in (
            "LMER_SERVICE_MODE", "LMER_REPO_URL", "LMER_CHECKOUT_BRANCH",
            "LMER_CHECKOUT_REF", "GITLAB_MR_ID", "LMER_SECONDARY_TARGETS",
            "LMER_NAPKIN_REPO", "LMER_TASKDEF_REPO", "LMER_TASKDEF_REF",
            "LMER_TRUST_MISE", "LMER_REPO_HOST", "LMER_REPO_PROJECT",
            "LMER_TASK", "LMER_TASK_TARGET", "LMER_LAUNCH_UID",
            "LMER_SESSION_ID",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("LMER_NO_REPO", "1")
        monkeypatch.setenv("LMER_WORK_REPO", str(upstream))
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
        monkeypatch.setenv("LMER_WORK_REPO_REF", ref)
        monkeypatch.setattr(sys, "stdin", io.StringIO())  # headless

        monkeypatch.setattr(clone_and_exec, "ensure_clone", fake_ensure_clone)
        monkeypatch.setattr(clone_and_exec, "mint_session_id", spy_mint)
        monkeypatch.setattr(clone_and_exec, "clone_aux_repos", MagicMock())
        monkeypatch.setattr(clone_and_exec, "setup_napkin_and_links", MagicMock())
        monkeypatch.setattr(clone_and_exec, "ensure_work_repo_directory", MagicMock())
        monkeypatch.setattr(clone_and_exec, "provision_documentation", MagicMock())
        monkeypatch.setattr(clone_and_exec, "_trust_mise_config", MagicMock())
        monkeypatch.setattr(
            "work_repo.run_state._source_context", lambda *a, **k: None
        )
        monkeypatch.setattr(clone_and_exec.os, "execv", MagicMock())

        rc = clone_and_exec.main(["--", "true"])
        return rc, work, observed

    def test_the_branch_manifest_is_read_at_startup(
        self, upstream, tmp_path, monkeypatch, home
    ):
        rc, work, observed = self._run_main(
            upstream, tmp_path, monkeypatch, "feature/ref"
        )

        assert rc == 0
        assert _git("symbolic-ref", "--short", "HEAD", cwd=work) == "feature/ref"
        # The schema-2 fresh-mint ran, and the manifest was already the
        # branch's when it did: the checkout precedes the manifest read.
        assert (True, True) in observed

    def test_a_non_branch_ref_stops_before_the_manifest_is_read(
        self, upstream, tmp_path, monkeypatch, home, capsys
    ):
        rc, work, observed = self._run_main(upstream, tmp_path, monkeypatch, "v1")

        assert rc == 2
        assert (True, True) not in observed
        assert _git("symbolic-ref", "--short", "HEAD", cwd=work) == "main"
        assert "not a branch" in capsys.readouterr().err


def test_cli_declares_the_work_repo_ref_passthrough():
    source = (
        Path(__file__).resolve().parents[1] / "src/lmer_cli/cli.py"
    ).read_text()
    assert re.search(
        r"""["']LMER_WORK_REPO_REF["']\s*:\s*os\.environ\.get\(\s*["']LMER_WORK_REPO_REF["']\s*\)""",
        source,
    ), "LMER_WORK_REPO_REF must be in the container-env passthrough dict"
