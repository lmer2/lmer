"""Tests for optional napkin/taskdef clones and home symlinks in clone_and_exec."""
import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, call

from lmer_cli.container import clone_and_exec


def _init_source_repo(path: Path) -> str:
    """Create a real local git repo with one commit; return committed file name."""
    path.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    (path / "hello.txt").write_text("hi")
    subprocess.run(["git", "add", "hello.txt"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "init"],
        cwd=path, check=True, capture_output=True, env=env,
    )
    return "hello.txt"


class TestCloneAuxRepos:
    def test_clones_both_when_set(self):
        with patch.object(clone_and_exec, "ensure_clone") as mock_clone:
            clone_and_exec.clone_aux_repos(
                "https://oauth2:t@h/org/napkin.git",
                "https://oauth2:t@h/org/taskdef.git",
                "v1.2.3",
            )
        mock_clone.assert_any_call(
            Path("/napkin"), "https://oauth2:t@h/org/napkin.git", None, None,
            manage_existing=True,
        )
        mock_clone.assert_any_call(
            Path("/taskdef"), "https://oauth2:t@h/org/taskdef.git", None,
            "v1.2.3", manage_existing=True,
        )
        assert mock_clone.call_count == 2

    def test_no_clone_when_unset(self):
        with patch.object(clone_and_exec, "ensure_clone") as mock_clone:
            clone_and_exec.clone_aux_repos(None, None, None)
        mock_clone.assert_not_called()

    def test_clone_failure_is_non_fatal(self):
        with patch.object(clone_and_exec, "ensure_clone", side_effect=Exception("boom")):
            # Must not raise.
            clone_and_exec.clone_aux_repos("https://h/napkin.git", None, None)

    def test_clone_failure_scrubs_credentials_from_stderr(self, capsys):
        """A failed clone stringifies CalledProcessError, whose e.cmd carries
        the credentialed clone URL — the live token must never reach stderr."""
        token_url = "https://oauth2:sekret-token@h/napkin.git"
        err = subprocess.CalledProcessError(128, ["git", "clone", token_url, "/napkin"])
        with patch.object(clone_and_exec, "ensure_clone", side_effect=err):
            clone_and_exec.clone_aux_repos(token_url, token_url, None)
        captured = capsys.readouterr()
        assert "sekret-token" not in captured.err
        assert "napkin clone failed" in captured.err
        assert "taskdef clone failed" in captured.err


class TestEnsureClonePreexistingTarget:
    def test_populates_pre_existing_empty_target(self, tmp_path):
        """ensure_clone must clone into an already-existing empty dir.

        This mimics the image-provided writable mountpoint (/napkin, /taskdef)
        pre-created in the Containerfile: the target exists and is empty before
        the clone runs.
        """
        source = tmp_path / "source"
        committed = _init_source_repo(source)

        target = tmp_path / "napkin"
        target.mkdir()  # pre-existing empty mountpoint

        clone_and_exec.ensure_clone(target, str(source), None, None)

        assert (target / ".git").exists()
        assert (target / committed).exists()

        # Second call is a no-op: .git already present, still valid.
        clone_and_exec.ensure_clone(target, str(source), None, None)
        assert (target / ".git").exists()
        assert (target / committed).exists()


class TestLinkIntoHome:
    def test_creates_symlink(self, tmp_path):
        target = tmp_path / "repo"
        target.mkdir()
        link = tmp_path / "home" / "work"
        link.parent.mkdir()
        clone_and_exec.link_into_home(link, target)
        assert link.is_symlink()
        assert link.resolve() == target.resolve()

    def test_idempotent_when_called_twice(self, tmp_path):
        target = tmp_path / "repo"
        target.mkdir()
        link = tmp_path / "work"
        clone_and_exec.link_into_home(link, target)
        clone_and_exec.link_into_home(link, target)  # must not raise
        assert link.is_symlink()
        assert link.resolve() == target.resolve()

    def test_replaces_existing_file(self, tmp_path):
        target = tmp_path / "repo"
        target.mkdir()
        link = tmp_path / "work"
        link.write_text("stale")
        clone_and_exec.link_into_home(link, target)
        assert link.is_symlink()
        assert link.resolve() == target.resolve()

    def test_replaces_existing_symlink(self, tmp_path):
        old = tmp_path / "old"
        old.mkdir()
        target = tmp_path / "repo"
        target.mkdir()
        link = tmp_path / "work"
        link.symlink_to(old)
        clone_and_exec.link_into_home(link, target)
        assert link.resolve() == target.resolve()


class TestSetupNapkinAndLinks:
    def test_subdir_mode_creates_napkin_dir_and_links(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        napkin = work / "napkin"  # does not exist yet
        home = tmp_path / "home"
        home.mkdir()
        clone_and_exec.setup_napkin_and_links(work, napkin, napkin_is_separate=False, home=home)
        assert napkin.is_dir()
        assert (home / "work").resolve() == work.resolve()
        assert (home / "napkin").resolve() == napkin.resolve()

    def test_separate_mode_does_not_mkdir_but_links(self, tmp_path):
        work = tmp_path / "work"
        work.mkdir()
        napkin = tmp_path / "napkin"
        napkin.mkdir()  # already cloned
        home = tmp_path / "home"
        home.mkdir()
        clone_and_exec.setup_napkin_and_links(work, napkin, napkin_is_separate=True, home=home)
        assert (home / "napkin").resolve() == napkin.resolve()
        assert (home / "work").resolve() == work.resolve()
