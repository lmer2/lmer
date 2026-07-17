"""Tests for the git-lfs skip fallback in container cloning.

The lmer container ships ``git`` but not ``git-lfs``. A target repo that tracks
files via LFS (``filter=lfs`` in ``.gitattributes``) would otherwise abort
checkout with ``git-lfs: command not found`` (``fatal: the remote end hung up``,
exit 128). When git-lfs is absent, the clone command must disable the LFS
smudge/process filters so checkout degrades to pointer files instead of failing.
The disabling config must precede the ``clone`` subcommand so it applies to the
implicit checkout.

The offending ``filter.lfs.*`` config is *global* inside the container (the
host's ``~/.gitconfig`` is mounted in, with ``required = true``), so the ``-c``
flags only protect the clone itself. The same settings must therefore also be
persisted into the cloned repo's local config right after the clone, covering
the later branch/ref checkout, the MR auto-checkout, and in-session git use.
"""

from pathlib import Path

from lmer_cli.container import clone_and_exec
from lmer_cli.container.clone_and_exec import (
    _clone_cmd,
    _lfs_safe_git,
    clone_secondary_mr,
    ensure_clone,
)

REPO_URL = "https://host/org/repo.git"


def _record_check_calls(monkeypatch) -> list[list[str]]:
    """Replace clone_and_exec.check_call with a recorder; return the log."""
    calls: list[list[str]] = []
    monkeypatch.setattr(clone_and_exec, "check_call", lambda cmd: calls.append(cmd))
    return calls


def _lfs_config_calls(calls: list[list[str]], repo_dir: Path) -> list[list[str]]:
    """The ``git -C <repo_dir> config filter.lfs.*`` calls in *calls*."""
    return [
        c
        for c in calls
        if c[:4] == ["git", "-C", str(repo_dir), "config"]
        and c[4].startswith("filter.lfs.")
    ]


class TestCloneCmdLfsSkip:
    def test_disables_lfs_filters_when_git_lfs_missing(self, monkeypatch):
        monkeypatch.setattr(clone_and_exec.shutil, "which", lambda name: None)
        cmd = _clone_cmd(REPO_URL, Path("/workspace"))

        # Filters are disabled and marked non-required so a missing git-lfs is
        # not fatal.
        assert "filter.lfs.process=" in cmd
        assert "filter.lfs.smudge=" in cmd
        assert "filter.lfs.required=false" in cmd
        # `-c` config must come before `clone` (applies to the checkout).
        assert cmd.index("-c") < cmd.index("clone")
        # Positional args are still last and untouched.
        assert cmd[-2:] == [REPO_URL, "/workspace"]

    def test_plain_clone_when_git_lfs_present(self, monkeypatch):
        monkeypatch.setattr(
            clone_and_exec.shutil, "which", lambda name: "/usr/bin/git-lfs"
        )
        cmd = _clone_cmd(REPO_URL, Path("/workspace"))

        assert cmd == ["git", "clone", REPO_URL, "/workspace"]
        assert "lfs" not in " ".join(cmd)


class TestLfsBypassWarning:
    """The bypass must not be silent (review on !126): without git-lfs,
    LFS-tracked files check out as pointer text and tests fail confusingly —
    one stderr line names the cause, once per process."""

    def test_warns_once_when_git_lfs_missing(self, monkeypatch, capsys):
        monkeypatch.setattr(clone_and_exec.shutil, "which", lambda name: None)
        monkeypatch.setattr(clone_and_exec, "_lfs_bypass_warned", False)

        _lfs_safe_git("-C", "/workspace", "checkout", "mr-5")
        _clone_cmd(REPO_URL, Path("/workspace"))

        err = capsys.readouterr().err
        assert err.count("git-lfs not installed") == 1
        assert "pointer files" in err

    def test_no_warning_when_git_lfs_present(self, monkeypatch, capsys):
        monkeypatch.setattr(
            clone_and_exec.shutil, "which", lambda name: "/usr/bin/git-lfs"
        )
        monkeypatch.setattr(clone_and_exec, "_lfs_bypass_warned", False)

        _lfs_safe_git("-C", "/workspace", "checkout", "mr-5")

        assert "git-lfs" not in capsys.readouterr().err


class TestLfsSafeGit:
    """Process-scoped `-c` flags for git operations on bind-mounted checkouts
    (service mode / --checkout), where repo-local persistence must never be
    written but checkouts still have to survive a missing git-lfs."""

    def test_carries_skip_flags_when_git_lfs_missing(self, monkeypatch):
        monkeypatch.setattr(clone_and_exec.shutil, "which", lambda name: None)
        cmd = _lfs_safe_git("-C", "/workspace", "checkout", "mr-5")

        # Flags precede the git args so they apply as global options.
        assert cmd[:2] == ["git", "-c"]
        assert "filter.lfs.smudge=" in cmd
        assert "filter.lfs.process=" in cmd
        assert "filter.lfs.required=false" in cmd
        assert cmd[-4:] == ["-C", "/workspace", "checkout", "mr-5"]

    def test_plain_git_when_git_lfs_present(self, monkeypatch):
        monkeypatch.setattr(
            clone_and_exec.shutil, "which", lambda name: "/usr/bin/git-lfs"
        )
        assert _lfs_safe_git("-C", "/workspace", "checkout", "mr-5") == [
            "git", "-C", "/workspace", "checkout", "mr-5"
        ]


class TestEnsureCloneLfsSkip:
    """Pin ensure_clone to _clone_cmd and to the repo-local persistence.

    A regression re-inlining a plain ``git clone`` (or dropping the local
    config write) would otherwise pass the suite, since the tests above
    exercise _clone_cmd in isolation only.
    """

    def test_clone_goes_through_clone_cmd(self, monkeypatch, tmp_path):
        monkeypatch.setattr(clone_and_exec.shutil, "which", lambda name: None)
        calls = _record_check_calls(monkeypatch)
        ws = tmp_path / "ws"

        ensure_clone(ws, REPO_URL, None, None)

        assert calls[0] == _clone_cmd(REPO_URL, ws)
        assert "clone" in calls[0]

    def test_persists_lfs_skip_into_repo_local_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(clone_and_exec.shutil, "which", lambda name: None)
        calls = _record_check_calls(monkeypatch)
        ws = tmp_path / "ws"

        ensure_clone(ws, REPO_URL, None, None)

        # The global (mounted) filter.lfs.* config would break every later
        # checkout in the repo; the repo-local override must cover them all.
        assert _lfs_config_calls(calls, ws) == [
            ["git", "-C", str(ws), "config", "filter.lfs.smudge", ""],
            ["git", "-C", str(ws), "config", "filter.lfs.process", ""],
            ["git", "-C", str(ws), "config", "filter.lfs.required", "false"],
        ]

    def test_no_local_config_when_git_lfs_present(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            clone_and_exec.shutil, "which", lambda name: "/usr/bin/git-lfs"
        )
        calls = _record_check_calls(monkeypatch)
        ws = tmp_path / "ws"

        ensure_clone(ws, REPO_URL, None, None)

        assert calls[0] == ["git", "clone", REPO_URL, str(ws)]
        assert _lfs_config_calls(calls, ws) == []


class TestCloneSecondaryMrLfsSkip:
    def test_clone_goes_through_clone_cmd_and_persists(self, monkeypatch, tmp_path):
        monkeypatch.setattr(clone_and_exec.shutil, "which", lambda name: None)
        calls = _record_check_calls(monkeypatch)

        # A plain repo URL is used as-is (no MR-id derivation), keeping the
        # test hermetic — no token lookup from the test environment.
        clone_secondary_mr(REPO_URL, tmp_path)
        target_dir = tmp_path / "repo.git"

        assert calls[0] == _clone_cmd(REPO_URL, target_dir)
        assert _lfs_config_calls(calls, target_dir) == [
            ["git", "-C", str(target_dir), "config", "filter.lfs.smudge", ""],
            ["git", "-C", str(target_dir), "config", "filter.lfs.process", ""],
            ["git", "-C", str(target_dir), "config", "filter.lfs.required", "false"],
        ]
