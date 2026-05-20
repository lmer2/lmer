"""
Container entrypoint for repository cloning and command execution.

This script runs inside the container to:
1. Clone the target repository into /workspace
2. Checkout specified branch or ref
3. Clone secondary MRs (if any) into subdirectories
4. Execute either Claude Code runner or arbitrary commands

It is invoked by the host CLI with repository configuration passed via
environment variables (LMER_REPO_URL, LMER_CHECKOUT_BRANCH, LMER_CHECKOUT_REF).

Note: This script runs standalone (not as part of lmer_cli package) so it
must not import from lmer_cli or work_repo modules.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def run(cmd: list[str]) -> int:
    """
    Execute a command and return its exit code.

    Args:
        cmd: Command and arguments to execute

    Returns:
        Exit code from subprocess.call
    """
    return subprocess.call(cmd)


def check_call(cmd: list[str]) -> None:
    """
    Execute a command and raise exception if it fails.

    Args:
        cmd: Command and arguments to execute

    Raises:
        subprocess.CalledProcessError: If command returns non-zero exit code
    """
    subprocess.check_call(cmd)


def ensure_clone(workspace: Path, repo_url: str, branch: Optional[str], ref: Optional[str]) -> None:
    """
    Clone repository into workspace if not already present.

    If workspace already contains a git repository, skip cloning.
    Otherwise, clone the repo and checkout the specified branch or ref.

    Args:
        workspace: Path to workspace directory
        repo_url: Git repository URL to clone
        branch: Optional branch name to checkout
        ref: Optional git ref (tag/commit) to checkout

    Raises:
        subprocess.CalledProcessError: If git commands fail
    """
    workspace.mkdir(parents=True, exist_ok=True)
    if (workspace / ".git").exists():
        return

    clone_cmd = ["git", "clone", repo_url, str(workspace)]
    check_call(clone_cmd)

    # Configure safe.directory to avoid ownership complaints
    try:
        check_call(["git", "config", "--global", "--add", "safe.directory", str(workspace)])
    except Exception:
        pass

    if ref:
        check_call(["git", "-C", str(workspace), "fetch", "--all", "--tags"])
        check_call(["git", "-C", str(workspace), "checkout", "--detach", ref])
    elif branch:
        # try switch/checkout
        rc = run(["git", "-C", str(workspace), "switch", branch])
        if rc != 0:
            check_call(["git", "-C", str(workspace), "checkout", branch])


def find_runner() -> str:
    """
    Locate Claude Code runner script in the container.

    Searches standard installation locations for claude-runner.sh.

    Returns:
        Path to runner script, or 'claude' as fallback
    """
    # Prefer global install location inside container
    candidates = [
        "/home/developer/.lmer/libexec/claude-runner.sh",
        "/Agents/global/libexec/claude-runner.sh",
        "/home/developer/claude-runner.sh",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    # Fallback to plain claude on PATH
    return "claude"


def sanitize_task_target(task_target: str) -> str:
    """
    Sanitize task target for use in filesystem paths.

    Handles URLs (MR/PR/issue links), branch names, commit SHAs.
    """
    if not task_target:
        return "default"

    # If it's a URL, try to extract meaningful parts
    if task_target.startswith("http://") or task_target.startswith("https://"):
        # GitLab MR format: .../-/merge_requests/123
        if "/-/merge_requests/" in task_target.lower():
            parts = task_target.split("/-/merge_requests/")
            if len(parts) > 1:
                mr_id = parts[-1].split("/")[0].split("?")[0]
                return f"mr-{mr_id}"
        # GitLab issue format: .../-/issues/456
        if "/-/issues/" in task_target.lower():
            parts = task_target.split("/-/issues/")
            if len(parts) > 1:
                issue_id = parts[-1].split("/")[0].split("?")[0]
                return f"issue-{issue_id}"
        # GitHub PR format: .../pull/123
        if "/pull/" in task_target.lower():
            parts = task_target.split("/pull/")
            if len(parts) > 1:
                pr_id = parts[-1].split("/")[0].split("?")[0]
                return f"pr-{pr_id}"
        # GitHub issue format: .../issues/456
        if "/issues/" in task_target.lower() and "/pull/" not in task_target.lower():
            parts = task_target.split("/issues/")
            if len(parts) > 1:
                issue_id = parts[-1].split("/")[0].split("?")[0]
                return f"issue-{issue_id}"
        # Fallback: use the last meaningful segment of the URL
        path_parts = [p for p in task_target.split("/") if p and p not in ("http:", "https:", "")]
        if path_parts:
            last_part = path_parts[-1].split("?")[0].split("#")[0]
            return last_part.replace(":", "-")

    # For branch names, commit SHAs, etc., sanitize but preserve structure
    sanitized = "".join(c if c.isalnum() or c in "-_." else "-" for c in task_target)
    while "--" in sanitized:
        sanitized = sanitized.replace("--", "-")
    sanitized = sanitized.strip("-")

    return sanitized if sanitized else "default"


def _get_gitlab_token(host: str) -> str | None:
    """
    Look up an API token for ``host`` from environment variables.

    Despite the name, supports both GitLab and GitHub hosts. Lookup:
      1. ``GITLAB_TOKEN_{sanitized_host}`` — host-specific (provider-agnostic
         in practice; the prefix is historical)
      2. For github.com / *.github.com / *.ghe.com: ``GH_TOKEN``, then
         ``GITHUB_TOKEN``
      3. ``GITLAB_TOKEN`` — generic fallback

    NOTE: standalone copy of lmer_cli.tokens._get_gitlab_token (this module
    runs inside the container without the lmer_cli import path). The work-repo
    branch (``LMER_WORK_REPO_TOKEN``/``GITLAB_TOKEN_worklog``) is intentionally
    omitted — the host CLI has already injected the work-repo token into
    ``LMER_WORK_REPO`` before the container starts. Keep this in sync with
    tokens.py for the host-shared behavior.
    """
    suffix = re.sub(r"[.\-]", "_", host.lower())
    token = os.environ.get(f"GITLAB_TOKEN_{suffix}")
    if token:
        return token

    h = host.lower()
    if h == "github.com" or h.endswith(".github.com") or h.endswith(".ghe.com"):
        for var in ("GH_TOKEN", "GITHUB_TOKEN"):
            token = os.environ.get(var)
            if token:
                return token

    return os.environ.get("GITLAB_TOKEN")


def _derive_repo_url_from_task_target(target: str) -> str | None:
    """
    Best-effort derivation of a base repository URL from a task target URL
    such as PR/MR/issue links.

    For GitLab hosts with available API tokens, returns HTTPS URL with token auth.
    Otherwise returns SSH-format URL for git cloning.

    Supports:
    - GitHub: https://github.com/owner/repo/pull/123 -> git@github.com:owner/repo
    - GitLab: https://gitlab.com/group/project/-/merge_requests/123 -> https://oauth2:TOKEN@gitlab.com/group/project.git (if token available)
    - GitLab: https://gitlab.example.com/group/subgroup/project/-/issues/456 -> git@gitlab.example.com:group/subgroup/project (if no token)
    """
    try:
        parsed = urlparse(target)
    except Exception:
        return None

    if not parsed.scheme or not parsed.netloc or not parsed.path:
        return None

    host = parsed.hostname
    if not host:
        return None

    path_parts = [p for p in parsed.path.split('/') if p]
    if len(path_parts) < 2:
        return None

    # Heuristics: only attempt derive when a known resource path is present
    lowered = '/'.join(path_parts).lower()
    indicators = (
        'pull/', 'pulls/', 'merge_requests', 'issues/', 'compare/', 'commits/', 'commit/'
    )
    if not any(tok in lowered for tok in indicators):
        return None

    # GitLab URLs use /-/ separator: group/project/-/merge_requests/123
    # Find the /-/ separator and extract everything before it
    if '/-/' in parsed.path:
        # Split on /-/ and take everything before it
        project_path = parsed.path.split('/-/')[0].strip('/')
        if project_path:
            # Check if we have a GitLab token for this host
            token = _get_gitlab_token(host)
            if token:
                return f"https://oauth2:{token}@{host}/{project_path}.git"
            return f"git@{host}:{project_path}"

    # GitHub and simple URLs: owner/repo/pull/123 or owner/repo/issues/123
    owner = path_parts[0]
    repo = path_parts[1]

    # Strip trailing .git if present in repo segment from some URLs
    repo = re.sub(r"\.git$", "", repo)

    return f"git@{host}:{owner}/{repo}"


def _parse_gitlab_mr_url(target: str) -> tuple[str | None, str | None, str | None]:
    """
    Parse a GitLab merge request URL and extract host, project, and MR ID.

    Args:
        target: GitLab MR URL (e.g., https://gitlab.example.com/group/project/-/merge_requests/756)

    Returns:
        Tuple of (host, project, mr_id) or (None, None, None) if not a GitLab MR URL
    """
    try:
        parsed = urlparse(target)
    except Exception:
        return None, None, None

    if not parsed.scheme or not parsed.netloc or not parsed.path:
        return None, None, None

    # Check if this is a GitLab merge request URL
    # GitLab URLs use /-/ separator: group/project/-/merge_requests/123
    if '/-/merge_requests/' not in parsed.path.lower():
        return None, None, None

    # Extract host (hostname only, strip any credentials)
    host = parsed.hostname
    if not host:
        return None, None, None

    # Extract project path (everything before /-/)
    if '/-/' in parsed.path:
        project_path = parsed.path.split('/-/')[0].strip('/')
        if not project_path:
            return None, None, None
    else:
        return None, None, None

    # Extract MR ID (number after merge_requests/)
    path_after_separator = parsed.path.split('/-/')[1]
    # Look for merge_requests/ followed by a number
    match = re.search(r'merge_requests/(\d+)', path_after_separator, re.IGNORECASE)
    if not match:
        return None, None, None

    mr_id = match.group(1)

    return host, project_path, mr_id


def clone_secondary_mr(target: str, workspace: Path) -> None:
    """
    Clone a secondary MR into a subdirectory of the workspace.

    Args:
        target: MR/PR URL or repository URL
        workspace: Path to workspace directory

    Raises:
        subprocess.CalledProcessError: If git commands fail
    """
    # Derive repo URL from target
    repo_url = _derive_repo_url_from_task_target(target)
    if not repo_url:
        # If we can't derive, assume it's already a repo URL
        repo_url = target

    # Parse GitLab MR URL to get MR ID for directory naming
    gitlab_host, gitlab_project, mr_id = _parse_gitlab_mr_url(target)

    # Determine subdirectory name
    if mr_id:
        subdir_name = f"mr-{mr_id}"
    else:
        # For non-MR URLs, use sanitized target name
        subdir_name = sanitize_task_target(target)

    target_dir = workspace / subdir_name
    target_dir.mkdir(parents=True, exist_ok=True)

    # Skip if already cloned
    if (target_dir / ".git").exists():
        print(f"✅ Secondary MR already cloned at {target_dir}", file=sys.stderr)
        return

    # Clone the repository
    print(f"📦 Cloning secondary MR into {target_dir}...", file=sys.stderr)
    clone_cmd = ["git", "clone", repo_url, str(target_dir)]
    check_call(clone_cmd)

    # Configure safe.directory
    try:
        check_call(["git", "config", "--global", "--add", "safe.directory", str(target_dir)])
    except Exception:
        pass

    # For GitLab MRs, try to fetch and checkout the MR branch
    if gitlab_host and gitlab_project and mr_id:
        print(f"🔍 Attempting to fetch secondary MR {mr_id} branch from {gitlab_host}/{gitlab_project}...", file=sys.stderr)
        try:
            check_call(["git", "-C", str(target_dir), "fetch", "origin",
                        f"merge-requests/{mr_id}/head:mr-{mr_id}"])
            check_call(["git", "-C", str(target_dir), "checkout", f"mr-{mr_id}"])
            print(f"✅ Checked out secondary MR {mr_id} branch (mr-{mr_id}) in {target_dir}", file=sys.stderr)
        except subprocess.CalledProcessError as e:
            print(f"⚠️  Failed to checkout secondary MR {mr_id} branch: {e}", file=sys.stderr)
            print(f"   Repository cloned at HEAD in {target_dir}. You may need to manually checkout the source branch.", file=sys.stderr)


def ensure_work_repo_directory(work_repo_path: Path, host: str | None, project: str | None, task_type: str | None, task_target: str | None) -> None:
    """
    Ensure directory structure exists in work repo: {host}/{project}/{task_type}/{task_target}
    Also creates info directories at:
    - {host}/{project}/info/ (global project info)
    - {host}/{project}/{task_type}/info/ (project+task specific info)

    Args:
        work_repo_path: Path to the work repository
        host: Git service host (e.g., gitlab.example.com, github.com)
        project: Project path (e.g., group/project)
        task_type: Task type (e.g., review, modernize, develop, etc.)
        task_target: Task target (e.g., merge request URL, branch name, etc.)

    Raises:
        OSError: If directory creation fails
    """
    if not host or not project:
        # Skip directory creation if we can't parse the repo URL
        return

    # Build project directory path: {host}/{project}
    project_dir_parts = [host, project]
    project_dir = work_repo_path
    for part in project_dir_parts:
        project_dir = project_dir / part
        project_dir.mkdir(parents=True, exist_ok=True)

    # Create global project info directory at {host}/{project}/info/
    project_info_dir = project_dir / "info"
    project_info_dir.mkdir(parents=True, exist_ok=True)

    # Build task directory path: {host}/{project}/{task_type}/{task_target}
    safe_task_type = task_type if task_type else "default"
    safe_task_target = sanitize_task_target(task_target) if task_target else "default"

    # Build task type directory path: {host}/{project}/{task_type}
    task_type_dir_parts = [host, project, safe_task_type]
    task_type_dir = work_repo_path
    for part in task_type_dir_parts:
        task_type_dir = task_type_dir / part
        task_type_dir.mkdir(parents=True, exist_ok=True)

    # Create task-specific info directory at {host}/{project}/{task_type}/info/
    task_info_dir = task_type_dir / "info"
    task_info_dir.mkdir(parents=True, exist_ok=True)

    # Create directory structure for task target
    dir_parts = [host, project, safe_task_type, safe_task_target]

    target_dir = work_repo_path
    for part in dir_parts:
        target_dir = target_dir / part
        target_dir.mkdir(parents=True, exist_ok=True)


def _is_self_dev_workspace(workspace: Path) -> bool:
    """
    Detect whether the workspace IS the lmer repository itself.

    In self-development mode, the workspace is a checkout of lmer rather than
    a target project. Provisioning lmer's own docs into a lmer checkout would
    overwrite/shadow the real source files and pollute .git/info/exclude with
    entries for files that the developer needs to be able to commit.

    Detection mirrors libexec/claude-runner.sh and lmer_cli.runtime._is_lmer_pyproject:
    read pyproject.toml and check whether the project name is "lmer" or "lmer-cli".
    A standalone copy is necessary because this module runs as a standalone script
    inside the container and cannot import from the lmer_cli package (see header).
    The LMER_SELF_DEV env var is set later by claude-runner.sh and is not yet
    available when this script runs.

    Args:
        workspace: Path to the workspace to inspect

    Returns:
        True if workspace is a lmer source checkout, False otherwise.
    """
    pyproject = workspace / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef,import-not-found]
        except ImportError:
            return False
    try:
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return False
    name = data.get("project", {}).get("name")
    return name in ("lmer", "lmer-cli")


def provision_documentation(
    workspace: Path,
    work_repo_path: Path,
    global_path: Path,
) -> list[str]:
    """
    Provision AGENTS.md and rules/ files into the workspace when missing.

    For each documentation file, the hierarchy is (least to most important):
    1. lmer global defaults (global_path, e.g. /Agents/global/)
    2. Work repo project info ({host}/{project}/info/)
    3. Project repo's own files (already in workspace) — highest priority, skipped

    Provisioned files are added to .git/info/exclude so they don't appear as
    untracked changes, and a .lmer-provisioned-docs marker file is written
    listing which files were provisioned.

    Skipped entirely when the workspace IS the lmer repository (self-dev mode):
    in that case the repo's own files are authoritative and we must not touch
    .git/info/exclude or copy stray files from /Agents/global into the dev
    checkout.

    Args:
        workspace: Path to the target project workspace
        work_repo_path: Path to the work repository
        global_path: Path to the lmer global directory (e.g. /Agents/global)

    Returns:
        List of file paths (relative to workspace) that were provisioned
    """
    if _is_self_dev_workspace(workspace):
        print(
            "📄 Self-development mode: skipping documentation provisioning "
            "(workspace is the lmer repo)",
            file=sys.stderr,
        )
        return []

    repo_host = os.environ.get("LMER_REPO_HOST")
    repo_project = os.environ.get("LMER_REPO_PROJECT")

    # Discover which rule files lmer provides
    doc_files = ["AGENTS.md"]
    global_rules_dir = global_path / "rules"
    if global_rules_dir.is_dir():
        for rule_file in sorted(global_rules_dir.glob("*.md")):
            doc_files.append(f"rules/{rule_file.name}")

    provisioned: list[str] = []
    sources: dict[str, str] = {}  # doc_file -> "work-repo" or "lmer-defaults"

    for doc_file in doc_files:
        target = workspace / doc_file
        if target.exists():
            continue  # Project has its own — highest priority

        # Try work repo first (middle priority)
        source = None
        if repo_host and repo_project:
            work_source = work_repo_path / repo_host / repo_project / "info" / doc_file
            if work_source.exists():
                source = work_source
                sources[doc_file] = "work-repo"

        # Fall back to lmer global (lowest priority)
        if source is None:
            global_source = global_path / doc_file
            if global_source.exists():
                source = global_source
                sources[doc_file] = "lmer-defaults"

        if source:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(target))
            provisioned.append(doc_file)

    if provisioned:
        # Add provisioned files to .git/info/exclude so git ignores them
        exclude_file = workspace / ".git" / "info" / "exclude"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_file.read_text() if exclude_file.exists() else ""
        existing_lines = set(existing.splitlines())
        additions: list[str] = []
        if "# lmer provisioned documentation" not in existing_lines:
            additions.append("# lmer provisioned documentation")
        for doc_file in provisioned:
            if doc_file not in existing_lines:
                additions.append(doc_file)
        marker_name = ".lmer-provisioned-docs"
        if marker_name not in existing_lines:
            additions.append(marker_name)
        if additions:
            with open(exclude_file, "a") as f:
                f.write("\n" + "\n".join(additions) + "\n")

        # Write marker file listing provisioned files
        marker = workspace / ".lmer-provisioned-docs"
        with open(marker, "w") as f:
            for doc_file in provisioned:
                f.write(f"{doc_file}\n")

        print(f"📄 Provisioned {len(provisioned)} documentation file(s):", file=sys.stderr)
        for doc_file in provisioned:
            src_label = sources.get(doc_file, "unknown")
            print(f"   • {doc_file} ({src_label})", file=sys.stderr)

    return provisioned


def main(argv: list[str] | None = None) -> int:
    """
    Container entrypoint main function.

    Workflow:
    1. Read repository URL from LMER_REPO_URL environment variable
    2. Clone repository into /workspace with specified branch/ref
    3. Clone work repository into /work
    4. Create directory structure in work repo: {host}/{project}/{task_type}/{task_target}
    5. Execute either claude-runner or custom command via bash

    Args:
        argv: Command-line arguments (expects '--' followed by command tokens)

    Returns:
        Exit code (0 for success, 2 for missing env vars, 1 for git errors)
    """
    argv = argv if argv is not None else sys.argv[1:]
    # Expect a '--' delimiter then command tokens
    if "--" in argv:
        idx = argv.index("--")
        cmd_tokens = argv[idx + 1 :]
    else:
        cmd_tokens = ["claude-runner"]

    service_mode = os.environ.get("LMER_SERVICE_MODE") == "1"

    repo_url = os.environ.get("LMER_REPO_URL")
    if not repo_url and not service_mode:
        print("❌ LMER_REPO_URL is not set in environment", file=sys.stderr)
        return 2

    work_repo_url = os.environ.get("LMER_WORK_REPO", "")
    if not work_repo_url:
        print("❌ LMER_WORK_REPO is not set in environment", file=sys.stderr)
        return 2

    branch = os.environ.get("LMER_CHECKOUT_BRANCH")
    ref = os.environ.get("LMER_CHECKOUT_REF")

    ws = Path("/workspace").resolve()

    if service_mode:
        # Service mode: /workspace is bind-mounted from --checkout.
        # Skip cloning, but configure git safe.directory and handle branch/MR checkout.
        service_name = os.environ.get("LMER_SERVICE_NAME", "unknown")
        print(f"📦 Service mode: using local checkout at /workspace (service: {service_name})", file=sys.stderr)
        try:
            check_call(["git", "config", "--global", "--add", "safe.directory", str(ws)])
        except Exception:
            pass
        # Checkout branch or ref if specified
        if ref:
            try:
                check_call(["git", "-C", str(ws), "fetch", "--all", "--tags"])
                check_call(["git", "-C", str(ws), "checkout", "--detach", ref])
            except subprocess.CalledProcessError as e:
                print(f"⚠️  Failed to checkout ref {ref}: {e}", file=sys.stderr)
        elif branch:
            rc = run(["git", "-C", str(ws), "switch", branch])
            if rc != 0:
                rc = run(["git", "-C", str(ws), "checkout", branch])
                if rc != 0:
                    print(f"⚠️  Failed to checkout branch {branch}", file=sys.stderr)
    else:
        # Normal mode: clone the repository
        if not repo_url:
            print("❌ LMER_REPO_URL is not set in environment", file=sys.stderr)
            return 2
        try:
            ensure_clone(ws, repo_url, branch, ref)
        except subprocess.CalledProcessError as e:
            print(f"❌ git operation failed: {e}", file=sys.stderr)
            return e.returncode or 1

    # Trust workspace mise config if present and opt-in via LMER_TRUST_MISE.
    # Gated behind an env var because auto-trusting .mise.toml from cloned repos
    # could allow untrusted repos to install arbitrary tools or run hooks.
    mise_toml = ws / ".mise.toml"
    if mise_toml.exists():
        if os.environ.get("LMER_TRUST_MISE", "").lower() in ("1", "true", "yes"):
            try:
                run(["mise", "trust", str(mise_toml)])
            except Exception:
                pass
        else:
            print(f"⚠️  Workspace has .mise.toml but LMER_TRUST_MISE is not set", file=sys.stderr)
            print(f"   Set LMER_TRUST_MISE=1 in your .env to auto-trust workspace mise configs", file=sys.stderr)

    # For GitLab MRs, checkout the MR source branch if no explicit branch/ref was given.
    # Skip in service mode — the user's checkout is already set up and remote auth
    # may not be available inside the container. Claude handles branch setup via
    # task instructions (Phase -1).
    git_remote = os.environ.get("LMER_GIT_REMOTE", "origin")
    gitlab_mr_id = os.environ.get("GITLAB_MR_ID")
    if gitlab_mr_id and not branch and not ref and not service_mode:
        print(f"🔍 Detected GitLab MR {gitlab_mr_id}, attempting to fetch and checkout MR branch (remote: {git_remote})...", file=sys.stderr)
        try:
            check_call(["git", "-C", str(ws), "fetch", git_remote,
                        f"merge-requests/{gitlab_mr_id}/head:mr-{gitlab_mr_id}"])
            check_call(["git", "-C", str(ws), "checkout", f"mr-{gitlab_mr_id}"])
            print(f"✅ Checked out MR {gitlab_mr_id} branch (mr-{gitlab_mr_id})", file=sys.stderr)
        except subprocess.CalledProcessError as e:
            print(f"⚠️  Failed to checkout MR {gitlab_mr_id} branch: {e}", file=sys.stderr)
            print(f"   Repository remains at default branch. You may need to manually checkout the source branch.", file=sys.stderr)
    elif gitlab_mr_id and service_mode:
        print(f"📋 Service mode: skipping MR branch auto-checkout (MR {gitlab_mr_id})", file=sys.stderr)
        print(f"   Claude will handle branch setup via task instructions", file=sys.stderr)

    # Clone work repository
    work_repo_path = Path(os.environ.get("LMER_WORK_REPO_PATH", "/work")).resolve()
    try:
        ensure_clone(work_repo_path, work_repo_url, None, None)
    except subprocess.CalledProcessError as e:
        print(f"❌ work repo clone failed: {e}", file=sys.stderr)
        return e.returncode or 1

    # Clone secondary MRs if any
    secondary_targets_str = os.environ.get("LMER_SECONDARY_TARGETS")
    if secondary_targets_str:
        secondary_targets = [t.strip() for t in secondary_targets_str.split(",") if t.strip()]
        for secondary_target in secondary_targets:
            try:
                clone_secondary_mr(secondary_target, ws)
            except subprocess.CalledProcessError as e:
                print(f"⚠️  Failed to clone secondary MR {secondary_target}: {e}", file=sys.stderr)
                # Don't fail the entire operation if secondary MR clone fails
            except Exception as e:
                print(f"⚠️  Error processing secondary MR {secondary_target}: {e}", file=sys.stderr)
                # Don't fail the entire operation if secondary MR processing fails

    # Create directory structure in work repo
    repo_host = os.environ.get("LMER_REPO_HOST")
    repo_project = os.environ.get("LMER_REPO_PROJECT")
    task_type = os.environ.get("LMER_TASK")
    task_target = os.environ.get("LMER_TASK_TARGET")
    try:
        ensure_work_repo_directory(work_repo_path, repo_host, repo_project, task_type, task_target)
    except OSError as e:
        print(f"⚠️  Failed to create work repo directory structure: {e}", file=sys.stderr)
        # Don't fail the entire operation if directory creation fails

    # Provision missing documentation (AGENTS.md, rules/) from work repo or lmer
    global_path = Path("/Agents/global")
    if (ws / ".git").exists() and global_path.exists():
        try:
            provision_documentation(ws, work_repo_path, global_path)
        except Exception as e:
            print(f"⚠️  Failed to provision documentation: {e}", file=sys.stderr)

    # Dispatch
    if len(cmd_tokens) == 1 and cmd_tokens[0] == "claude-runner":
        runner = find_runner()
        os.execv(runner, [runner])
        return 0

    # Otherwise, treat tokens as a command to exec via bash -lc
    cmd_str = " ".join(shlex.quote(t) for t in cmd_tokens)
    os.execv("/bin/bash", ["/bin/bash", "-lc", cmd_str])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
