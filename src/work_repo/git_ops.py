"""Git operations for work repository."""

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .run_state import run_rel_path_candidates
from .utils import sanitize_task_target


def run_git_command(cmd: list[str], cwd: Path, check: bool = True) -> tuple[int, str]:
    """
    Run a git command and return exit code and output.

    Args:
        cmd: Git command and arguments
        cwd: Working directory for the command
        check: If True, raise exception on non-zero exit

    Returns:
        Tuple of (exit_code, output)
    """
    try:
        result = subprocess.run(
            ["git"] + cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=check,
        )
        if result.returncode == 0:
            return result.returncode, result.stdout
        # Git reports errors on stderr; include it so a failure message is
        # never an empty string after the colon.
        return result.returncode, (result.stdout + result.stderr).strip()
    except subprocess.CalledProcessError as e:
        return e.returncode, ((e.stdout or "") + (e.stderr or "")).strip()


PUSH_RETRIES = 3

# Cap on how many stray entries ``report_uncommitted_work_items`` lists before
# collapsing the rest into a "... and N more" line, so a large dirty tree can
# never flood ``work commit``'s output.
UNTRACKED_REPORT_CAP = 10


def _has_tracked_files(work_repo_path: Path, rel_path: str) -> bool:
    """True when git tracks anything under rel_path — even if it is gone
    from disk (pending deletions still need staging)."""
    rc, output = run_git_command(
        ["ls-files", "--", rel_path], work_repo_path, check=False
    )
    return rc == 0 and bool(output.strip())


def commit_work_path(target_path, commit_message: Optional[str] = None) -> int:
    """
    Sync one or more paths in the work repo: add -> commit -> rebase -> push.

    Stages only the given path(s) (relative to the work repo root) with ``git
    add -A`` so additions, modifications, *and* deletions under them are
    captured, leaving unrelated pending changes elsewhere in the work repo
    untouched. If staging produces no changes, returns 0 without committing —
    the no-change check is scoped to the given paths too, so an unrelated
    dirty file (e.g. a per-session ``log.yaml``) does not trigger a spurious
    empty commit.

    Ordering matters: the local commit happens FIRST, then the remote is
    integrated with ``pull --rebase``, then push retries with a rebase
    between attempts. The previous fetch/pull-first sequence failed whenever
    the tree was dirty (``git pull`` refuses) and then lost the race to a
    busy remote — the work repo has many concurrent writers by design.

    Args:
        target_path: Path (or list of paths) within the work repo to stage,
            relative to its root (e.g. ``github.com/owner/repo/review/pr-123``).
            Paths that neither exist on disk nor hold tracked files are
            skipped; a tracked path missing from disk is KEPT so that
            deletions — e.g. the old name after a run-dir rename — get
            staged and the move lands as a move, not a duplicate.
        commit_message: Optional commit message (defaults to auto-generated
            from the first path).

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    work_repo_path = Path(os.environ.get("LMER_WORK_REPO_PATH", "/work"))

    if not work_repo_path.exists():
        print(f"❌ Work repository not found at {work_repo_path}", file=sys.stderr)
        return 1

    all_paths = [target_path] if isinstance(target_path, str) else list(target_path)
    paths = [
        p
        for p in all_paths
        if (work_repo_path / p).exists() or _has_tracked_files(work_repo_path, p)
    ]
    if not paths:
        print("✅ No existing paths to commit in work repository")
        return 0

    # 1. Stage first — committing before any pull keeps the tree clean for
    # the rebase below (a dirty tree makes `git pull` refuse outright).
    print(f"➕ Staging {', '.join(paths)} in work repository...")
    rc, output = run_git_command(["add", "-A", "--", *paths], work_repo_path, check=False)
    if rc != 0:
        print(f"❌ git add failed (work repo): {output}", file=sys.stderr)
        return rc

    rc, status_output = run_git_command(
        ["status", "--porcelain", "--", *paths], work_repo_path, check=False
    )
    if not status_output.strip():
        print("✅ No changes to commit in work repository")
        return 0

    # 2. Commit
    if commit_message is None:
        commit_message = f"Update work repo: {paths[0]}"
    print(f"💾 Committing changes to work repository...")
    rc, output = run_git_command(["commit", "-m", commit_message], work_repo_path, check=False)
    if rc != 0:
        print(f"❌ git commit failed (work repo): {output}", file=sys.stderr)
        return rc

    # 3. Integrate the remote and push, rebasing between attempts — many
    # sessions and host-side tools push here concurrently.
    print(f"📤 Pushing changes to work repository...")
    run_git_command(["fetch"], work_repo_path, check=False)
    rc, output = run_git_command(["pull", "--rebase"], work_repo_path, check=False)
    if rc != 0:
        print(f"⚠️  git pull --rebase warning (work repo): {output}", file=sys.stderr)

    for attempt in range(1, PUSH_RETRIES + 1):
        rc, output = run_git_command(["push"], work_repo_path, check=False)
        if rc == 0:
            print("✅ Successfully committed and pushed changes to work repository")
            return 0
        print(
            f"⚠️  git push rejected (attempt {attempt}/{PUSH_RETRIES}): {output}",
            file=sys.stderr,
        )
        if attempt < PUSH_RETRIES:
            rrc, routput = run_git_command(["pull", "--rebase"], work_repo_path, check=False)
            if rrc != 0:
                print(f"⚠️  git pull --rebase warning (work repo): {routput}", file=sys.stderr)

    print(f"❌ git push failed after {PUSH_RETRIES} attempts (work repo): {output}", file=sys.stderr)
    return rc


def commit_work_changes(commit_message: Optional[str] = None) -> int:
    """
    Commit and push the current task-target directory in the work repo.

    Builds the path ``{host}/{project}/{task_type}/{task_target}`` from the
    environment and delegates to :func:`commit_work_path`.

    Args:
        commit_message: Optional commit message (defaults to auto-generated)

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    repo_host = os.environ.get("LMER_REPO_HOST")
    repo_project = os.environ.get("LMER_REPO_PROJECT")
    task_type = os.environ.get("LMER_TASK", "default")
    task_target = os.environ.get("LMER_TASK_TARGET", "default")

    if not repo_host or not repo_project:
        print("❌ LMER_REPO_HOST and LMER_REPO_PROJECT must be set", file=sys.stderr)
        return 1

    # Sanitize task_target to match directory structure
    safe_task_target = sanitize_task_target(task_target) if task_target else "default"

    # Legacy task-target dir: log/report output predating the run-dir
    # unification (issue #87 D4) may still sit here — keep staging it.
    target_path = f"{repo_host}/{repo_project}/{task_type}/{safe_task_target}"

    # The run dir itself: the resolved (possibly renamed) dir plus the
    # bare-slug dir, so a rename whose own push failed still gets its old
    # path's deletions staged (commit_work_path skips clean paths).
    paths = [target_path]
    for rel in run_rel_path_candidates():
        if rel not in paths:
            paths.append(rel)

    return commit_work_path(paths, commit_message)


def report_uncommitted_work_items() -> int:
    """Flag items left uncommitted in the work repo after a ``work commit``.

    ``work commit`` stages only the run dir and legacy task-target dir (see
    :func:`commit_work_changes`), so a file added elsewhere — e.g. a new
    ``{host}/{project}/info/*.md`` — is silently left behind (issue #85). This
    runs a repo-wide ``git status --porcelain --untracked-files=all`` and prints
    one ⚠️ block naming the untracked/unstaged entries so the user notices them.
    ``--untracked-files=all`` lists files individually rather than collapsing a
    brand-new directory into a single entry, so the count is per-file and every
    stray file is named. The list is capped at :data:`UNTRACKED_REPORT_CAP`
    entries with a "... and N more" tail so a large dirty tree cannot flood the
    output. Nothing is printed when the tree is clean.

    Fail-soft by contract: this is a reminder, never a gate. It never raises
    and returns 0 on any error (or when the work repo is absent), so a caller
    can invoke it after a commit without risk to that commit's exit code.

    Returns:
        The number of uncommitted entries found (0 when clean or on error).
    """
    try:
        work_repo_path = Path(os.environ.get("LMER_WORK_REPO_PATH", "/work"))
        if not work_repo_path.exists():
            return 0
        rc, output = run_git_command(
            ["status", "--porcelain", "--untracked-files=all"],
            work_repo_path,
            check=False,
        )
        if rc != 0:
            return 0
        entries = [line for line in output.splitlines() if line.strip()]
        if not entries:
            return 0

        print(
            f"⚠️  {len(entries)} uncommitted item(s) remain in the work repo "
            "(not staged by `work commit`):"
        )
        for line in entries[:UNTRACKED_REPORT_CAP]:
            print(f"   {line}")
        remaining = len(entries) - UNTRACKED_REPORT_CAP
        if remaining > 0:
            print(f"   ... and {remaining} more")
        print(
            "   These fell outside the paths `work commit` stages — add & "
            "commit them manually if they should be kept."
        )
        return len(entries)
    except Exception:
        # Reminder only: a status hiccup must never affect the commit result.
        return 0
