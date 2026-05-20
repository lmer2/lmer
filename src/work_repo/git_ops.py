"""Git operations for work repository."""

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

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
        return result.returncode, result.stdout
    except subprocess.CalledProcessError as e:
        return e.returncode, e.stderr


def commit_work_changes(commit_message: Optional[str] = None) -> int:
    """
    Perform git operations: fetch -> pull -> add -> commit -> push.

    Args:
        commit_message: Optional commit message (defaults to auto-generated)

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    work_repo_path = Path(os.environ.get("LMER_WORK_REPO_PATH", "/work"))
    repo_host = os.environ.get("LMER_REPO_HOST")
    repo_project = os.environ.get("LMER_REPO_PROJECT")
    task_type = os.environ.get("LMER_TASK", "default")
    task_target = os.environ.get("LMER_TASK_TARGET", "default")

    if not repo_host or not repo_project:
        print("❌ LMER_REPO_HOST and LMER_REPO_PROJECT must be set", file=sys.stderr)
        return 1

    if not work_repo_path.exists():
        print(f"❌ Work repository not found at {work_repo_path}", file=sys.stderr)
        return 1

    # Sanitize task_target to match directory structure
    safe_task_target = sanitize_task_target(task_target) if task_target else "default"

    # Build path to add: {host}/{project}/{task_type}/{task_target}
    target_path = f"{repo_host}/{repo_project}/{task_type}/{safe_task_target}"

    # 1. Fetch
    print(f"📥 Fetching latest changes from work repository...")
    rc, output = run_git_command(["fetch"], work_repo_path, check=False)
    if rc != 0:
        print(f"⚠️  git fetch warning (work repo): {output}", file=sys.stderr)

    # 2. Pull
    print(f"📥 Pulling latest changes from work repository...")
    rc, output = run_git_command(["pull"], work_repo_path, check=False)
    if rc != 0:
        print(f"⚠️  git pull warning (work repo): {output}", file=sys.stderr)

    # 3. Add
    print(f"➕ Staging {target_path} in work repository...")
    rc, output = run_git_command(["add", target_path], work_repo_path, check=False)
    if rc != 0:
        print(f"❌ git add failed (work repo): {output}", file=sys.stderr)
        return rc

    # Check if there are changes to commit
    rc, status_output = run_git_command(["status", "--porcelain"], work_repo_path, check=False)
    if not status_output.strip():
        print("✅ No changes to commit in work repository")
        return 0

    # 4. Commit
    if commit_message is None:
        commit_message = f"Update work repo: {target_path}"
    print(f"💾 Committing changes to work repository...")
    rc, output = run_git_command(["commit", "-m", commit_message], work_repo_path, check=False)
    if rc != 0:
        print(f"❌ git commit failed (work repo): {output}", file=sys.stderr)
        return rc

    # 5. Push
    print(f"📤 Pushing changes to work repository...")
    rc, output = run_git_command(["push"], work_repo_path, check=False)
    if rc != 0:
        print(f"❌ git push failed (work repo): {output}", file=sys.stderr)
        return rc

    print("✅ Successfully committed and pushed changes to work repository")
    return 0
