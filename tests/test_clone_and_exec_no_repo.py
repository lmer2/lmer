"""Tests for repo-less sessions (LMER_NO_REPO=1) in clone_and_exec.

A Slack thread permalink as the sole `lmer chat` target
starts a session without a repository. The host CLI signals this to the
container via LMER_NO_REPO=1; clone_and_exec must skip the workspace clone
instead of failing on the missing LMER_REPO_URL.
"""

import os
from pathlib import Path
from unittest.mock import patch

from lmer_cli.container import clone_and_exec


_WORK_REPO_URL = "https://github.com/example/work-repo.git"


def _base_env(home):
    # HOME must be a scratch dir: main() runs setup_napkin_and_links, whose
    # link_into_home() rmtrees an existing real $HOME/work before symlinking.
    # With the real HOME this deleted /home/runner/work (the checkout itself)
    # on GitHub-hosted runners (#125). LMER_WORK_REPO_PATH likewise: without
    # it, main() defaults to /work and mkdir-p's a real /work/napkin.
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LMER_WORK_REPO": _WORK_REPO_URL,
        "LMER_WORK_REPO_PATH": str(home / "work-repo"),
    }


def _run_main(env, argv=None):
    """Run clone_and_exec.main() with clone/exec side effects mocked out.

    Returns (rc, ensure_clone_calls, execv_calls).
    """
    clone_calls = []
    execv_calls = []

    def fake_ensure_clone(workspace, repo_url, branch, ref, **kwargs):
        clone_calls.append((Path(workspace), repo_url))

    def fake_execv(path, args):
        execv_calls.append((path, args))

    def fake_dispatch_runner(runner):
        # The runner is now dispatched as a child process (dispatch_runner)
        # rather than execv'd; record it in the same shape for assertions.
        execv_calls.append((runner, [runner]))
        return 0

    with patch.dict(os.environ, env, clear=True):
        with patch.object(clone_and_exec, "ensure_clone", fake_ensure_clone), \
             patch.object(clone_and_exec, "ensure_work_repo_directory"), \
             patch.object(clone_and_exec, "provision_documentation"), \
             patch.object(clone_and_exec, "find_runner", return_value="/bin/true"), \
             patch.object(clone_and_exec, "dispatch_runner", fake_dispatch_runner), \
             patch.object(clone_and_exec.os, "execv", fake_execv):
            rc = clone_and_exec.main(argv if argv is not None else ["--", "claude-runner"])

    return rc, clone_calls, execv_calls


class TestNoRepoMode:
    def test_missing_repo_url_without_no_repo_is_fatal(self, tmp_path):
        """Without LMER_NO_REPO (and not service mode), a missing
        LMER_REPO_URL must still exit 2 — existing behavior unchanged."""
        rc, clone_calls, _ = _run_main(_base_env(tmp_path))
        assert rc == 2
        assert clone_calls == []

    def test_no_repo_mode_skips_workspace_clone(self, tmp_path):
        """LMER_NO_REPO=1 with no LMER_REPO_URL must not error and must not
        clone anything into /workspace."""
        rc, clone_calls, execv_calls = _run_main({**_base_env(tmp_path), "LMER_NO_REPO": "1"})
        assert rc == 0
        workspace_clones = [c for c in clone_calls if c[0] == Path("/workspace")]
        assert workspace_clones == [], (
            f"No workspace clone expected in no-repo mode; got {workspace_clones}"
        )
        assert execv_calls, "claude-runner dispatch must still happen"

    def test_no_repo_mode_still_clones_work_repo(self, tmp_path):
        """The work repository clone is unaffected by no-repo mode."""
        rc, clone_calls, _ = _run_main({**_base_env(tmp_path), "LMER_NO_REPO": "1"})
        assert rc == 0
        work_clones = [c for c in clone_calls if c[1] == _WORK_REPO_URL]
        assert len(work_clones) == 1, (
            f"Expected exactly one work-repo clone; got {clone_calls}"
        )

    def test_repo_url_present_wins_over_no_repo_flag(self, tmp_path):
        """Defensive: if both LMER_REPO_URL and LMER_NO_REPO are set, the
        repository is cloned normally."""
        env = {
            **_base_env(tmp_path),
            "LMER_NO_REPO": "1",
            "LMER_REPO_URL": "https://github.com/example/project.git",
        }
        rc, clone_calls, _ = _run_main(env)
        assert rc == 0
        workspace_clones = [c for c in clone_calls if c[0] == Path("/workspace")]
        assert workspace_clones == [
            (Path("/workspace"), "https://github.com/example/project.git")
        ]
