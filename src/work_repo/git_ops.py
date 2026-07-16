"""Git operations for work repository."""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit

from .run_state import run_rel_path_candidates
from .utils import sanitize_task_target

PUSH_RETRIES = 3

# Cap on how many stray entries ``report_uncommitted_work_items`` lists before
# collapsing the rest into a "... and N more" line, so a large dirty tree can
# never flood ``work commit``'s output.
UNTRACKED_REPORT_CAP = 10


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


def _web_base_from_remote(remote: str) -> Optional[str]:
    """Web base URL (``https://host/project``) for a git remote URL.

    Any userinfo in the remote — the work repo's remote is typically
    tokenized, e.g. ``https://oauth2:TOKEN@git.example.com/agents/work.git``
    — is STRIPPED: only the hostname (plus port) and project path survive,
    so a derived URL can never leak the token. ssh remotes, both the
    ``ssh://git@host/path.git`` scheme form and the scp-style
    ``git@host:path.git``, normalize to https. Returns None when no
    host/project can be derived (e.g. a local-path remote).
    """
    if "://" in remote:
        parts = urlsplit(remote)
        host = parts.hostname
        if not host:
            return None
        if parts.port:
            host = f"{host}:{parts.port}"
        project = parts.path.strip("/")
    else:
        match = re.match(r"^(?:[^@/]+@)?([^:/]+):(.+)$", remote)
        if match is None:
            return None
        host, project = match.group(1), match.group(2).strip("/")
    if project.endswith(".git"):
        project = project[: -len(".git")]
    if not project:
        return None
    return f"https://{host}/{project}"


def web_url_for(path) -> Optional[str]:
    """GitLab web URL for a path inside the work-repo checkout, or None.

    Maps an absolute path under ``LMER_WORK_REPO_PATH`` (default ``/work``)
    to ``https://<host>/<project>/-/blob/<branch>/<relpath>`` for files and
    ``/-/tree/<branch>/<relpath>`` for directories — the clickable form a
    human on a phone can actually open, instead of a container path. The
    web base comes from the checkout's ``origin`` remote with credentials
    stripped (:func:`_web_base_from_remote`); the branch is the current
    branch, falling back to ``main`` when detection fails (detached HEAD).

    Fail-soft by contract: any problem — no work repo, no origin remote, a
    remote with no host, a path outside or missing from the checkout —
    returns None and callers print the plain path as before. Never raises.
    """
    try:
        work_repo_path = Path(
            os.environ.get("LMER_WORK_REPO_PATH", "/work")
        ).resolve()
        target = Path(path).resolve()
        if not target.exists():
            return None
        if target != work_repo_path and work_repo_path not in target.parents:
            return None
        rc, remote = run_git_command(
            ["remote", "get-url", "origin"], work_repo_path, check=False
        )
        if rc != 0 or not remote.strip():
            return None
        base = _web_base_from_remote(remote.strip())
        if base is None:
            return None
        # symbolic-ref (not rev-parse) so an unborn initial branch still
        # names itself; a detached HEAD fails and falls back to main.
        rc, branch = run_git_command(
            ["symbolic-ref", "--short", "HEAD"], work_repo_path, check=False
        )
        branch = branch.strip() if rc == 0 else ""
        branch_q = quote(branch or "main", safe="/")
        if target == work_repo_path:
            return f"{base}/-/tree/{branch_q}"
        kind = "tree" if target.is_dir() else "blob"
        rel = quote(target.relative_to(work_repo_path).as_posix(), safe="/")
        return f"{base}/-/{kind}/{branch_q}/{rel}"
    except Exception:
        return None


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
    rc = _push_with_rebase_retries(work_repo_path, "work repo")
    if rc == 0:
        print("✅ Successfully committed and pushed changes to work repository")
    return rc


def _push_with_rebase_retries(repo_path: Path, label: str) -> int:
    """Integrate the remote and push, rebasing between attempts.

    The commit must already exist locally (commit-FIRST ordering — a dirty
    tree makes ``git pull`` refuse outright). Shared by the work-repo and
    separate-napkin push paths; both repos have concurrent writers by design,
    so a rejected push is retried up to :data:`PUSH_RETRIES` times with a
    ``pull --rebase`` between attempts.

    Returns 0 on success, the failing git exit code otherwise.
    """
    run_git_command(["fetch"], repo_path, check=False)
    rc, output = run_git_command(["pull", "--rebase"], repo_path, check=False)
    if rc != 0:
        print(f"⚠️  git pull --rebase warning ({label}): {output}", file=sys.stderr)

    for attempt in range(1, PUSH_RETRIES + 1):
        rc, output = run_git_command(["push"], repo_path, check=False)
        if rc == 0:
            return 0
        print(
            f"⚠️  git push rejected (attempt {attempt}/{PUSH_RETRIES}): {output}",
            file=sys.stderr,
        )
        if attempt < PUSH_RETRIES:
            rrc, routput = run_git_command(["pull", "--rebase"], repo_path, check=False)
            if rrc != 0:
                print(f"⚠️  git pull --rebase warning ({label}): {routput}", file=sys.stderr)

    print(f"❌ git push failed after {PUSH_RETRIES} attempts ({label}): {output}", file=sys.stderr)
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


def commit_napkin_if_subdir(commit_message: Optional[str] = None) -> int:
    """
    Commit and push the napkin subdir when napkin lives *inside* the work repo.

    The inverse of :func:`push_napkin_if_separate`: in subdir mode
    ``LMER_NAPKIN_PATH`` resolves under ``LMER_WORK_REPO_PATH``, but the
    work-repo commit (:func:`commit_work_changes`) stages only the task-target
    and run-dir paths — napkin sits at the work-repo root, so without this
    step notes written there are never committed and are silently lost when
    an ephemeral container exits. Stages and pushes just the napkin subdir
    via :func:`commit_work_path`.

    Returns 0 when there is nothing to do or the commit succeeds; the failing
    git exit code otherwise. Callers treat any failure as a warning — napkin
    capture must never block the worklog commit.
    """
    napkin_path_str = os.environ.get("LMER_NAPKIN_PATH")
    if not napkin_path_str:
        return 0
    napkin_path = Path(napkin_path_str).resolve()
    work_repo_path = Path(os.environ.get("LMER_WORK_REPO_PATH", "/work")).resolve()

    # Subdir mode only: strictly under the work repo and not a git repo of
    # its own (a nested repo would be unreachable via the work repo's index).
    if work_repo_path not in napkin_path.parents:
        return 0
    if (napkin_path / ".git").exists():
        return 0
    if not napkin_path.exists():
        return 0  # no notes were ever written

    rel_path = napkin_path.relative_to(work_repo_path)
    return commit_work_path([str(rel_path)], commit_message or "Update napkin notes")


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


def push_napkin_if_separate(commit_message: Optional[str] = None) -> int:
    """
    Commit and push the napkin repo when it is a *separate* git repo.

    Napkin is pushed only when ``LMER_NAPKIN_PATH`` resolves to a git repo that
    lives *outside* ``LMER_WORK_REPO_PATH``. In subdir mode (napkin under the
    work repo) the work-repo commit already captures it, so this is a no-op.

    Uses the same commit-FIRST ordering as :func:`commit_work_path` — stage
    and commit locally, then integrate the remote with ``pull --rebase`` and
    push with rebase-between-attempt retries (a shared team napkin repo has
    concurrent writers by design; a plain ``git pull`` refuses on a dirty
    tree and a single unretried push loses races). Returns 0 when there is
    nothing to do or the push succeeds; the failing git exit code otherwise.
    Callers treat any failure as a warning — napkin push must never block
    the worklog commit.
    """
    napkin_path_str = os.environ.get("LMER_NAPKIN_PATH")
    if not napkin_path_str:
        return 0
    napkin_path = Path(napkin_path_str).resolve()
    work_repo_path = Path(os.environ.get("LMER_WORK_REPO_PATH", "/work")).resolve()

    # Separate-repo mode only: a git repo that is not the work repo or under it.
    if not (napkin_path / ".git").exists():
        return 0
    if napkin_path == work_repo_path or work_repo_path in napkin_path.parents:
        return 0

    # 1. Stage first — committing before any pull keeps the tree clean for
    # the rebase below (a dirty tree makes `git pull` refuse outright).
    print("➕ Staging all changes in napkin repository...")
    rc, output = run_git_command(["add", "-A"], napkin_path, check=False)
    if rc != 0:
        print(f"⚠️  git add failed (napkin): {output}", file=sys.stderr)
        return rc

    rc, status_output = run_git_command(["status", "--porcelain"], napkin_path, check=False)
    if not status_output.strip():
        print("✅ No changes to commit in napkin repository")
        return 0

    # 2. Commit
    if commit_message is None:
        commit_message = "Update napkin notes"
    print("💾 Committing changes to napkin repository...")
    rc, output = run_git_command(["commit", "-m", commit_message], napkin_path, check=False)
    if rc != 0:
        print(f"⚠️  git commit failed (napkin): {output}", file=sys.stderr)
        return rc

    # 3. Integrate the remote and push, rebasing between attempts.
    print("📤 Pushing changes to napkin repository...")
    rc = _push_with_rebase_retries(napkin_path, "napkin")
    if rc == 0:
        print("✅ Successfully committed and pushed changes to napkin repository")
    return rc
