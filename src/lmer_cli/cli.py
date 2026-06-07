"""
LMER CLI main entry point.

This module implements the command-line interface for lmerpy, which orchestrates
running containerized development environments. It handles:
- Argument parsing for repository URLs, branches, and execution modes
- Container runtime detection (Docker/Podman)
- Volume mount configuration for workspaces, repos, and user configs
- SSH agent forwarding for authenticated git operations
- Command execution within containers

The CLI supports both interactive Claude Code sessions and arbitrary command execution.
"""

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
import re
from typing import Mapping
from dotenv import dotenv_values

from . import resolve
from .container_home import ensure_container_home
from .log import error, info, success, warning
from .mounts import (
    build_checkout_mount,
    build_container_home_mounts,
    build_external_taskdef_mounts,
    build_global_mount,
    build_host_repo_ro_mount,
    build_lmer_docs_mount,
    build_user_mounts,
    build_workspace_mount,
    build_service_mode_mounts,
)
from .build import DEFAULT_IMAGE, ensure_image, build_image, resolve_image_tag
from .runtime import base_run_args, detect_runtime, env_args, lmer_state_dir, repo_root_path
from .service import ServiceError, resolve_container, inspect_container_workdir
from .tokens import (
    _get_gitlab_token,
    _prefer_ssh,
    _convert_ssh_to_https_if_token_available,
    _inject_gitlab_token_if_available,
)
from .util import resolve_human_identity

# Default pool for general port passthrough (--ports / --port-pool). Kept
# distinct from the FastAPI range (8700-8799) so both features can be used in
# the same session without colliding on default flags.
DEFAULT_PORT_POOL = "8800-8899"


def _resolve_requested_ports(
    cli_count: int | None,
    cli_pool: str | None,
    env: Mapping[str, str],
) -> tuple[int, str]:
    """Resolve the requested port count and pool from CLI args + env vars.

    CLI values take precedence over the ``LMER_PORT_COUNT`` / ``LMER_PORT_POOL``
    environment variables. Returns ``(count, pool_spec)`` where ``count`` is 0
    when port passthrough is off. Raises :class:`ValueError` if the count is
    non-numeric or negative.
    """
    if cli_count is not None:
        count = cli_count
    else:
        raw = (env.get("LMER_PORT_COUNT") or "").strip()
        if not raw:
            count = 0
        else:
            try:
                count = int(raw)
            except ValueError:
                raise ValueError(f"LMER_PORT_COUNT must be an integer, got {raw!r}")
    if count < 0:
        raise ValueError(f"port count must be >= 0, got {count}")
    pool = cli_pool or env.get("LMER_PORT_POOL") or DEFAULT_PORT_POOL
    return count, pool


def _publish_loopback_ports(run: list[str], ports: list[int]) -> None:
    """Append container ``-p`` args publishing each port on 127.0.0.1.

    The same port number is used inside and outside the container, and the
    bind is loopback-only so the mapping is reachable from the host but not
    network-exposed. Shared by the FastAPI endpoint and the general
    port-passthrough publishing sites.
    """
    for port in ports:
        run += ["-p", f"127.0.0.1:{port}:{port}"]


def _apply_port_passthrough(
    ns: argparse.Namespace, env: dict, run: list[str]
) -> int | None:
    """Resolve, allocate, and publish general port-passthrough ports.

    Allocates the requested number of free ports from the pool on the host
    (before container start, so parallel sessions get disjoint ports),
    publishes each to 127.0.0.1, and exports the list to the container via
    ``LMER_PORTS`` so Claude can bind services to ports reachable from the host.
    CLI flags win over the ``LMER_PORT_COUNT``/``LMER_PORT_POOL`` env vars.

    Mutates ``env`` and ``run`` in place. Returns ``None`` on success
    (including when no ports are requested) or a process exit code on a fatal
    error, so the caller can ``return`` it directly.
    """
    try:
        port_count, port_pool_spec = _resolve_requested_ports(
            ns.ports, ns.port_pool, os.environ
        )
    except ValueError as exc:
        error(f"❌ {exc}")
        return 2
    if port_count <= 0:
        return None

    from .supervisor import _parse_port_range, _pick_ports

    try:
        port_pool = _parse_port_range(port_pool_spec)
    except ValueError as exc:
        error(f"❌ Invalid port pool {port_pool_spec!r}: {exc}")
        return 2
    try:
        picked_ports = _pick_ports(port_pool, "127.0.0.1", port_count)
    except RuntimeError as exc:
        error(f"❌ {exc}")
        return 2

    env["LMER_PORTS"] = ",".join(str(p) for p in picked_ports)
    _publish_loopback_ports(run, picked_ports)
    published = ", ".join(str(p) for p in picked_ports)
    info(f"🔌 Publishing {len(picked_ports)} port(s) on 127.0.0.1: {published}")
    info("   (exposed inside the container via LMER_PORTS; bind services to 0.0.0.0)")
    return None


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """
    Parse command-line arguments for lmerpy.

    Args:
        argv: List of command-line arguments to parse

    Returns:
        Tuple of (parsed namespace, remaining args after known args)
        Remaining args are used for commands after --exec
    """
    # Handle -- separator manually: everything after -- goes to rest
    # This is needed because argparse's parse_known_args doesn't stop
    # consuming positional arguments at --
    rest: list[str] = []
    if "--" in argv:
        idx = argv.index("--")
        rest = argv[idx + 1:]
        argv = argv[:idx]

    parser = argparse.ArgumentParser(prog="lmerpy", add_help=True)
    # Task is optional when --no-task is provided; otherwise required
    parser.add_argument("task", nargs="?", help="Task type (e.g., chat, review, develop, modernize)")
    parser.add_argument("target", nargs="*", help="Repository URL/path or PR/MR/issue URL. First target is primary (sets env vars), additional targets are cloned but don't override env vars.")
    parser.add_argument("--workspace-volume", dest="workspace_volume")
    parser.add_argument("--workspace-bind", dest="workspace_bind")
    parser.add_argument("--branch", dest="branch")
    parser.add_argument("--ref", dest="ref")
    parser.add_argument("--remote", dest="remote", help="Git remote name to use (required when local repo has multiple remotes)")
    parser.add_argument("--exec", dest="exec_mode", action="store_true", help="Run an arbitrary command in the container")
    parser.add_argument("--no-clone", dest="no_clone", action="store_true", help="Skip git clone, just run command (requires --exec)")
    parser.add_argument("--service", dest="service", help="Docker service/container name to exec into (service mode)")
    parser.add_argument("--checkout", dest="checkout", help="Path to existing local source checkout (mounted as /workspace)")
    parser.add_argument("--user", dest="user", help="Container user (e.g., developer or 0:0)")
    parser.add_argument("--match-uid", dest="match_uid", action="store_true", help="Run container as your host UID:GID (fixes SSH agent permissions)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--show-env", dest="show_env", action="store_true", help="Display LMER environment variable configuration on startup")

    # Use parse_known_args so we can capture the command after --exec
    parser.add_argument("--no-task", dest="no_task", action="store_true", help="Run without selecting a task (exec mode only)")
    parser.add_argument("--skip-build", dest="skip_build", action="store_true", help="Do not auto-build or pull the container image if missing")
    # Supervisor / FastAPI controls (consumed inside the container by lmer-supervisor)
    parser.add_argument("--fastapi", dest="fastapi", action="store_true", help="Expose a FastAPI endpoint to read/write the claude process (POST /input, GET /output)")
    parser.add_argument("--manual-start", dest="manual_start", action="store_true", help="Do not auto-inject /start into claude on launch")
    parser.add_argument("--prompt", dest="prompt", help="Follow-up prompt injected immediately after the auto-/start (e.g. --prompt='research X online first'). Ignored under --manual-start since nothing is auto-injected then.")
    parser.add_argument("--no-supervisor", dest="no_supervisor", action="store_true", help="Bypass lmer-supervisor and exec claude directly (debug aid for rendering issues)")
    parser.add_argument("--fastapi-port-range", dest="fastapi_port_range", help="Port range LOW-HIGH the FastAPI endpoint may bind to (default 8700-8799)")
    parser.add_argument("--fastapi-host", dest="fastapi_host", help="Host for the FastAPI endpoint to bind (default 127.0.0.1)")
    parser.add_argument("--fastapi-token", dest="fastapi_token", help="Bearer token for the FastAPI endpoint (auto-generated if omitted)")
    # General port passthrough: allocate N free ports from a pool and publish
    # them so a service Claude runs inside the container is reachable on the host.
    parser.add_argument("--ports", dest="ports", type=int, help="Number of host ports to allocate from --port-pool and publish to the container (env: LMER_PORT_COUNT)")
    parser.add_argument("--port-pool", dest="port_pool", help=f"Port pool LOW-HIGH to allocate --ports from (default {DEFAULT_PORT_POOL}; env: LMER_PORT_POOL)")

    ns, extra = parser.parse_known_args(argv)
    # Combine any extra unknown args with the -- separated rest
    rest = extra + rest
    return ns, rest


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


def _parse_repo_url(repo_url: str) -> tuple[str | None, str | None]:
    """
    Parse a repository URL and extract host and project path.

    Supports both SSH and HTTPS formats:
    - SSH: git@gitlab.example.com:group/project -> (gitlab.example.com, group/project)
    - HTTPS: https://gitlab.example.com/group/project -> (gitlab.example.com, group/project)
    - GitHub SSH: git@github.com:owner/repo -> (github.com, owner/repo)
    - GitHub HTTPS: https://github.com/owner/repo -> (github.com, owner/repo)

    Args:
        repo_url: Repository URL in SSH or HTTPS format

    Returns:
        Tuple of (host, project_path) or (None, None) if parsing fails
    """
    if not repo_url:
        return None, None

    # Handle SSH format: git@host:path
    if repo_url.startswith("git@"):
        # Format: git@host:path
        parts = repo_url.split(":", 1)
        if len(parts) != 2:
            return None, None
        host = parts[0].replace("git@", "")
        project_path = parts[1].rstrip(".git")
        return host, project_path

    # Handle HTTPS format: https://host/path
    try:
        parsed = urlparse(repo_url)
    except Exception:
        return None, None

    if not parsed.netloc:
        return None, None

    host = parsed.hostname
    if not host:
        return None, None
    # Extract project path from URL path
    path = parsed.path.strip("/")
    # Remove .git suffix if present
    path = re.sub(r"\.git$", "", path)

    if not path:
        return None, None

    return host, path


def _derive_repo_url_from_task_target(target: str) -> str | None:
    """
    Best-effort derivation of a base repository URL from a task target URL
    such as PR/MR/issue links.

    For GitLab hosts with available API tokens, returns HTTPS URL with token auth.
    Otherwise returns SSH-format URL for git cloning.

    Supports:
    - GitHub: https://github.com/owner/repo/pull/123 -> git@github.com:owner/repo
    - GitLab: https://gitlab.com/group/project/-/merge_requests/123 -> https://oauth2:TOKEN@gitlab.com/group/project (if token available)
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
                # Use HTTPS with token authentication
                return f"https://oauth2:{token}@{host}/{project_path}.git"
            # Fall back to SSH
            return f"git@{host}:{project_path}"

    # GitHub and simple URLs: owner/repo/pull/123 or owner/repo/issues/123
    owner = path_parts[0]
    repo = path_parts[1]

    # Strip trailing .git if present in repo segment from some URLs
    repo = re.sub(r"\.git$", "", repo)

    return f"git@{host}:{owner}/{repo}"


def _get_taskdef_paths(repo_root: Path | None) -> list[Path]:
    """
    Get ordered list of taskdef directories to search.

    Checks:
    1. Built-in taskdef dir (repo_root/taskdef if available)
    2. LMER_TASKDEF_PATHS env var (colon-separated list of additional paths)

    Returns paths in search order (first match wins).
    """
    paths: list[Path] = []

    # Built-in taskdef dir first
    if repo_root is not None:
        builtin = repo_root / "taskdef"
        if builtin.exists() and builtin.is_dir():
            paths.append(builtin)

    # Additional paths from env var
    paths += _get_external_taskdef_paths()

    return paths


def _get_external_taskdef_paths() -> list[Path]:
    """
    Parse LMER_TASKDEF_PATHS into a list of existing directory Paths.
    """
    paths: list[Path] = []
    extra_paths = os.environ.get("LMER_TASKDEF_PATHS", "")
    if extra_paths:
        for p in extra_paths.split(":"):
            p = p.strip()
            if p:
                path = Path(p)
                if path.exists() and path.is_dir():
                    paths.append(path)
    return paths


def _discover_tasks(taskdef_root: Path) -> set[str]:
    """
    Discover available tasks from a single taskdef directory.

    A task is any subdirectory of taskdef_root containing an instructions.txt file.
    """
    tasks: set[str] = set()
    if not taskdef_root.exists() or not taskdef_root.is_dir():
        return tasks
    for child in taskdef_root.iterdir():
        if child.is_dir() and (child / "instructions.txt").exists():
            tasks.add(child.name)
    return tasks


def _discover_all_tasks(taskdef_paths: list[Path]) -> set[str]:
    """
    Discover available tasks from all taskdef directories.
    """
    tasks: set[str] = set()
    for path in taskdef_paths:
        tasks |= _discover_tasks(path)
    return tasks


def _resolve_taskdef_dir(task_id: str, taskdef_paths: list[Path]) -> Path | None:
    """
    Find the taskdef directory for a given task ID.

    Searches paths in order, returning the first match.
    """
    for path in taskdef_paths:
        candidate = path / task_id
        if candidate.is_dir() and (candidate / "instructions.txt").exists():
            return candidate
    return None


def _check_ssh_setup(container_home: Path, ssh_agent_enabled: bool) -> None:
    """
    Check SSH configuration and warn users if git operations may fail.

    Checks for:
    1. SSH agent forwarding (preferred method)
    2. SSH keys in container-home/.ssh/
    3. SSH keys in ~/.ssh/ that could be copied

    Args:
        container_home: Path to container-home directory
        ssh_agent_enabled: Whether SSH agent forwarding is enabled
    """
    if ssh_agent_enabled:
        # SSH agent is available, no warning needed
        return

    # Check for SSH keys in container-home
    container_ssh_dir = container_home / ".ssh"
    has_container_keys = False
    if container_ssh_dir.exists():
        for key_file in ["id_rsa", "id_ed25519", "id_ecdsa"]:
            if (container_ssh_dir / key_file).exists():
                has_container_keys = True
                break

    if has_container_keys:
        # Keys exist in container-home, should work
        success("✅ SSH keys found in container-home/.ssh/")
        return

    # No SSH agent and no keys in container-home - warn the user
    home_ssh_dir = Path.home() / ".ssh"
    has_host_keys = False
    if home_ssh_dir.exists():
        for key_file in ["id_rsa", "id_ed25519", "id_ecdsa"]:
            if (home_ssh_dir / key_file).exists():
                has_host_keys = True
                break

    warning("")
    warning("─" * 72)
    warning("⚠️  SSH not configured - git operations requiring authentication may fail")
    warning("─" * 72)
    warning("")
    warning("  Option 1: Use SSH agent (recommended)")
    warning("    eval $(ssh-agent)")
    warning("    ssh-add ~/.ssh/id_ed25519")
    warning("")
    warning("  Option 2: Copy SSH keys to container-home")
    if has_host_keys:
        warning("    cp -r ~/.ssh ~/.lmer/container-home/")
        warning("    chmod 700 ~/.lmer/container-home/.ssh")
        warning("    chmod 600 ~/.lmer/container-home/.ssh/id_*")
    else:
        warning("    (No SSH keys found - generate one first:")
        warning("      ssh-keygen -t ed25519")
        warning("    Then add the public key to your Git hosting service)")
    warning("")
    warning("─" * 72)
    warning("")


def _redact_env_value(name: str, value: str) -> str:
    """Redact sensitive env var values, showing only a hint."""
    if re.search(r"TOKEN|KEY|SECRET|PASSWORD|CREDENTIALS", name, re.IGNORECASE):
        if len(value) <= 4:
            return "***"
        return value[:4] + "***"
    # Strip embedded credentials from URLs
    if "://" in value and "@" in value:
        from urllib.parse import urlparse, urlunparse
        try:
            parsed = urlparse(value)
            if parsed.password or parsed.username:
                cleaned = parsed._replace(
                    netloc=parsed.hostname + (f":{parsed.port}" if parsed.port else "")
                )
                return urlunparse(cleaned)
        except Exception:
            pass
    return value


def _display_env_config_cli(
    host_lmer_vars: set[str],
    env_file_sources: dict[str, str],
) -> None:
    """Display LMER environment variables with their sources and exit.

    Shows all LMER_* vars from the host environment and .env files in a
    formatted table.
    """
    lmer_vars: dict[str, str] = {}
    for key, value in sorted(os.environ.items()):
        if key.startswith("LMER_"):
            lmer_vars[key] = value

    if not lmer_vars:
        info("No LMER_* environment variables set.")
        return

    name_width = max(len(k) for k in lmer_vars)
    rows = []
    for name, value in lmer_vars.items():
        redacted = _redact_env_value(name, value)
        if name in env_file_sources:
            source = env_file_sources[name]
        elif name in host_lmer_vars:
            source = "environment"
        else:
            source = "environment"
        rows.append((name, redacted, source))

    value_width = max(len(r[1]) for r in rows)

    print("---")
    print("⚙️  LMER Environment Configuration:\n")
    print(f"  {'Variable':<{name_width}}  {'Value':<{value_width}}  Source")
    print(f"  {'─' * name_width}  {'─' * value_width}  {'─' * 20}")
    for name, value, source in rows:
        print(f"  {name:<{name_width}}  {value:<{value_width}}  {source}")
    print("---")


def _handle_build(argv: list[str]) -> int:
    """
    Handle the 'lmer build' subcommand.

    Args:
        argv: Arguments after 'build' (e.g., ['--no-pull', '--force'])

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    import argparse

    parser = argparse.ArgumentParser(prog="lmer build", description="Build the container image")
    parser.add_argument("--no-pull", action="store_true", help="Don't pass --pull to docker build (skip refreshing base image layers)")
    parser.add_argument("--force", action="store_true", help="Delete existing image before building")
    parser.add_argument("--local", metavar="PATH", type=Path, help="Path to local repo checkout to build from (useful when installed via pip/uv)")
    parser.add_argument("--update-claude", action="store_true", help="Force re-install of Claude Code CLI (bust Docker cache for that layer)")
    args = parser.parse_args(argv)

    try:
        runtime = detect_runtime()
    except Exception as e:
        error(f"❌ {e}")
        return 2

    repo_root = repo_root_path()
    build_root = args.local or repo_root
    # Resolve image tag from the install method (not the --local checkout)
    # so the built image matches what 'lmer chat' will look for.
    image = os.environ.get("LMER_IMAGE") or resolve_image_tag(repo_root)
    if not image:
        error("Could not determine image version. Set LMER_IMAGE or fix your installation.")
        return 2

    success(f"Building image {image}...")
    if build_image(runtime, image, build_root, force=args.force, pull=not args.no_pull, update_claude=args.update_claude):
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    """
    Main entry point for the lmerpy CLI.

    Orchestrates the complete workflow:
    1. Parse arguments and validate configuration
    2. Resolve repository URL (from args or git origin)
    3. Detect container runtime (Docker/Podman)
    4. Build container run arguments with mounts and environment
    5. Execute container with either Claude Code or custom command

    Args:
        argv: Command-line arguments, defaults to sys.argv[1:]

    Returns:
        Exit code (0 for success, non-zero for errors)
    """
    argv = argv if argv is not None else sys.argv[1:]

    # Handle build subcommand early (before normal arg parsing)
    if argv and argv[0] == "build":
        return _handle_build(argv[1:])
    if argv and argv[0] == "rebuild":
        error("'lmer rebuild' has been renamed to 'lmer build'. Please use 'lmer build' instead.")
        return 1

    ns, rest = parse_args(argv)

    if ns.verbose:
        os.environ["LMER_VERBOSE"] = "1"

    # Validate mutually exclusive options
    if ns.branch and ns.ref:
        error("Cannot specify both --branch and --ref")
        return 2
    if ns.workspace_volume and ns.workspace_bind:
        error("Cannot specify both --workspace-volume and --workspace-bind")
        return 2
    if ns.no_clone and not ns.exec_mode:
        error("--no-clone requires --exec mode")
        return 2
    if ns.user and ns.match_uid:
        error("Cannot specify both --user and --match-uid")
        return 2
    if ns.no_task and not ns.exec_mode:
        error("--no-task requires --exec mode")
        return 2
    if not ns.no_task and not ns.task and not ns.show_env:
        error("Task type is required unless --no-task is specified")
        return 2
    if ns.service and not ns.checkout:
        error("--service requires --checkout (path to local source checkout)")
        return 2

    # Determine container user
    if ns.match_uid:
        uid = os.getuid()
        gid = os.getgid()
        container_user = f"{uid}:{gid}"
    elif ns.user:
        container_user = ns.user
    else:
        container_user = os.environ.get("LMER_CONTAINER_USER", "developer")

    # Discover tasks from filesystem and validate provided task
    repo_root = repo_root_path()  # None in installed mode
    state_dir = lmer_state_dir()  # Always ~/.lmer/

    taskdef_paths = _get_taskdef_paths(repo_root)
    if taskdef_paths:
        known_tasks = _discover_all_tasks(taskdef_paths)
    else:
        # Installed mode with no local or configured taskdef directories.
        # Accept any task name; the container validates via baked-in taskdef/.
        known_tasks = set()

    # Snapshot host LMER_* env vars before .env loading (for source tracking)
    host_lmer_vars = {k for k in os.environ if k.startswith("LMER_")}

    # Load .env files early so GitLab tokens are available for URL derivation
    # Check state dir (~/.lmer/) and current working directory
    # Later entries take priority, so load in reverse order (highest priority first)
    # since we use a first-wins pattern (if key not in os.environ).
    # Also track sources for --show-env.
    cwd_env_file = Path.cwd() / ".env"
    early_env_file_sources: dict[str, str] = {}
    env_file_candidates = [
        ("working directory", cwd_env_file),
        ("lmer state dir", state_dir / ".env"),
    ]
    for location, env_file in env_file_candidates:
        if env_file.exists():
            env_vars = dotenv_values(dotenv_path=str(env_file))
            for key, value in env_vars.items():
                if key not in os.environ and value is not None:
                    os.environ[key] = value
                    early_env_file_sources[key] = f".env ({location})"

    # Handle --show-env: display env config table
    if ns.show_env:
        _display_env_config_cli(host_lmer_vars, early_env_file_sources)
        # Without a task, just show env and exit
        if not ns.task and not ns.no_task:
            return 0

    task_id = ns.task if not ns.no_task else None
    # Handle multiple targets: first is primary, rest are secondary
    # ns.target is always a list with nargs="*" (possibly empty)
    targets: list[str] = ns.target if ns.target else []
    primary_target: str | None = targets[0] if targets else None
    secondary_targets: list[str] = targets[1:] if len(targets) > 1 else []

    # Resolve taskdef directory for the selected task
    resolved_taskdef_dir: Path | None = None
    if not ns.no_task:
        # Host-side known_tasks is advisory: work-repo taskdefs (project-scoped
        # and globally-scoped) live inside the container at /work/... and are
        # not visible here. If the task isn't in the host's known set, warn but
        # continue — the container's start hook is authoritative. The container
        # may still fail to find the task (e.g. for a genuine typo); that error
        # surfaces after start-up.
        if task_id and known_tasks and task_id not in known_tasks:
            warning(
                "Task '" + str(task_id) + "' not found in host-side taskdef directories ("
                + ", ".join(sorted(known_tasks))
                + "). Will attempt to resolve it via the container's work-repo taskdefs; "
                + "if not found there either, the task will fail to start."
            )
        if task_id and taskdef_paths:
            resolved_taskdef_dir = _resolve_taskdef_dir(task_id, taskdef_paths)
        # Announce selected task early
        if primary_target:
            if secondary_targets:
                success(f"✅ Selected task: {task_id} (primary target: {primary_target}, secondary targets: {', '.join(secondary_targets)})")
            else:
                success(f"✅ Selected task: {task_id} (target: {primary_target})")
        else:
            success(f"✅ Selected task: {task_id}")
        if resolved_taskdef_dir:
            info(f"📂 Task definition: {resolved_taskdef_dir}")
    else:
        success("✅ Selected no-task (exec mode)")

    # Service mode: resolve checkout path early
    service_mode = bool(ns.service)
    checkout_path: Path | None = None
    if ns.checkout:
        checkout_path = Path(ns.checkout).resolve()
        if not checkout_path.exists():
            error(f"❌ --checkout path does not exist: {checkout_path}")
            return 2
        if not checkout_path.is_dir():
            error(f"❌ --checkout path is not a directory: {checkout_path}")
            return 2

    # Skip repo resolution when --no-clone or --no-task
    # Note: --checkout skips cloning but still resolves repo URL for MR/git metadata
    skip_repo_resolve = bool(ns.no_clone or ns.no_task)
    repo_url: str | None = None
    host_repo_path = None
    if not skip_repo_resolve:
        cwd = Path.cwd()

        # If task provides a PR/MR/issue URL, try to derive base repo URL
        # Only use primary target for repo resolution
        derived_repo_url = None
        if primary_target and isinstance(primary_target, str):
            derived_repo_url = _derive_repo_url_from_task_target(primary_target)

        try:
            target_to_resolve = derived_repo_url or (primary_target or "")
            # With --checkout, target is optional (may not have a repo URL)
            if checkout_path and not target_to_resolve:
                # No target and using checkout — resolve from the checkout's git remote
                repo_url, host_repo_path = resolve.normalize_repo_url("", checkout_path, ns.remote)
            else:
                repo_url, host_repo_path = resolve.normalize_repo_url(
                    target_to_resolve, cwd, ns.remote
                )
            # Inject GitLab token into repo URL if available (for HTTPS or SSH URLs)
            if repo_url:
                repo_url = _inject_gitlab_token_if_available(repo_url)
        except resolve.ResolveError as e:
            if checkout_path:
                # With --checkout, repo URL resolution failure is non-fatal
                warning(f"⚠️  Could not resolve repo URL: {e}")
            else:
                error(f"❌ {e}")
                return 2

    try:
        runtime = detect_runtime()
    except Exception as e:
        error(f"❌ {e}")
        return 2

    # Resolve image name and ensure it exists (auto-build or pull if missing)
    image = os.environ.get("LMER_IMAGE") or resolve_image_tag(repo_root)
    if not image:
        error("Could not determine image version. Set LMER_IMAGE or fix your installation.")
        return 1
    if not ensure_image(runtime, image, repo_root, skip_build=ns.skip_build):
        return 1

    # Build docker/podman run args
    exec_mode = bool(ns.exec_mode)
    run: list[str] = []
    run += base_run_args(runtime, exec_mode, container_user)

    # In developer mode, mount local repo dirs into the container to override
    # baked-in assets (for live development). In installed mode, the container
    # image already has everything at /Agents/global/.
    if repo_root is not None:
        run += build_global_mount(runtime, repo_root)
        run += build_lmer_docs_mount(runtime, repo_root)

    # Ensure container-home exists and mount it
    container_home_base = repo_root if repo_root is not None else state_dir
    container_home = ensure_container_home(container_home_base)
    run += build_container_home_mounts(runtime, container_home)

    # Build user mounts and check for SSH agent
    user_mounts, ssh_agent_enabled = build_user_mounts(runtime)
    run += user_mounts
    if ssh_agent_enabled:
        success("✅ SSH agent forwarding enabled")

    # Check SSH setup and warn if not configured
    _check_ssh_setup(container_home, ssh_agent_enabled)

    # Workspace mount removed - using /workspace directory from image instead
    # This avoids root:root ownership issues with Docker/Podman mounts
    # run += build_workspace_mount(
    #     runtime,
    #     ns.workspace_volume,
    #     Path(ns.workspace_bind).resolve() if ns.workspace_bind else None,
    # )
    if host_repo_path is not None:
        run += build_host_repo_ro_mount(runtime, host_repo_path)

    # Service mode: resolve container and add mounts
    service_container_id: str | None = None
    service_workdir: str | None = None
    if service_mode:
        try:
            service_container_id = resolve_container(runtime, ns.service)
            service_workdir = inspect_container_workdir(runtime, service_container_id)
        except ServiceError as e:
            error(f"❌ {e}")
            return 2
        run += build_service_mode_mounts(runtime, checkout_path)  # type: ignore[arg-type]
    elif checkout_path:
        # --checkout without --service: just mount the checkout as workspace
        # (no Docker socket needed since we're not doing container exec)
        run += build_checkout_mount(runtime, checkout_path)

    # Mount external taskdef directories into the container and build
    # container-side LMER_TASKDEF_PATHS so start.py can find instructions.
    container_taskdef_paths: list[str] = []
    external_taskdef_host_paths = _get_external_taskdef_paths()
    if external_taskdef_host_paths:
        taskdef_mount_args, container_taskdef_paths = build_external_taskdef_mounts(
            runtime, external_taskdef_host_paths
        )
        run += taskdef_mount_args
        info(f"📂 External taskdef paths: {', '.join(str(p) for p in external_taskdef_host_paths)}")

    # Parse GitLab MR URL if primary_target is a GitLab merge request URL
    # Only primary target sets GitLab env vars
    gitlab_host = None
    gitlab_project = None
    gitlab_mr_id = None
    if primary_target and isinstance(primary_target, str):
        parsed_host, parsed_project, parsed_mr_id = _parse_gitlab_mr_url(primary_target)
        if parsed_host and parsed_project and parsed_mr_id:
            gitlab_host = parsed_host
            gitlab_project = parsed_project
            gitlab_mr_id = parsed_mr_id

    # Parse repository URL to extract host and project for work repo directory structure
    repo_host = None
    repo_project = None
    if repo_url:
        repo_host, repo_project = _parse_repo_url(repo_url)

    # Get work repo URL, convert to HTTPS if token available
    work_repo_url_original = os.environ.get("LMER_WORK_REPO", "")
    if not work_repo_url_original:
        error("❌ LMER_WORK_REPO is required but not set. Configure it in your .env file.")
        return 2
    work_repo_url = _convert_ssh_to_https_if_token_available(work_repo_url_original, for_work_repo=True)

    # Debug: show token lookup for work repo
    if work_repo_url_original.startswith("git@"):
        work_host = work_repo_url_original.split("@")[1].split(":")[0]
        if _prefer_ssh():
            info(f"🔑 REPO_AUTH_PREFER_SSH set (using SSH for work repo)")
        else:
            work_token = _get_gitlab_token(work_host, for_work_repo=True)
            if work_token:
                success(f"🔑 Found GitLab token for {work_host} (using HTTPS auth for work repo)")
            else:
                info(f"🔑 No GitLab token found for {work_host} (work repo will use SSH)")
        info(f"📦 Work repo URL: {work_repo_url[:50]}..." if len(work_repo_url) > 50 else f"📦 Work repo URL: {work_repo_url}")

    # Remap resolved_taskdef_dir to container paths when it lives in an
    # external taskdef directory (host path won't exist inside the container).
    container_taskdef_root: str | None = None
    container_taskdef_dir: str | None = None
    container_task_instructions: str | None = None
    if resolved_taskdef_dir and not ns.no_task:
        # Check if the resolved dir is under one of the external host paths
        # and remap to the corresponding container mount point.
        remapped = False
        for host_path, cpath in zip(external_taskdef_host_paths, container_taskdef_paths):
            try:
                rel = resolved_taskdef_dir.relative_to(host_path)
                container_taskdef_dir = f"{cpath}/{rel}"
                container_taskdef_root = cpath
                container_task_instructions = f"{container_taskdef_dir}/instructions.txt"
                remapped = True
                break
            except ValueError:
                continue
        if not remapped:
            # Built-in taskdef or developer-mode path already inside container
            container_taskdef_root = str(resolved_taskdef_dir.parent)
            container_taskdef_dir = str(resolved_taskdef_dir)
            container_task_instructions = str(resolved_taskdef_dir / "instructions.txt")
    elif not ns.no_task:
        container_taskdef_root = "/Agents/global/taskdef"
        container_taskdef_dir = f"/Agents/global/taskdef/{task_id}" if task_id else None
        container_task_instructions = f"/Agents/global/taskdef/{task_id}/instructions.txt" if task_id else None

    env = {
        "HOME": "/home/developer",
        "CLAUDE_CODE_ENTRYPOINT": os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli"),
        "LMER_GLOBAL_DIR": os.environ.get("LMER_GLOBAL_DIR", "/home/developer/.lmer"),
        "LMER_DANGER_ZONE": os.environ.get("LMER_DANGER_ZONE"),
        "LMER_REASONING_EFFORT": os.environ.get("LMER_REASONING_EFFORT"),
        "LMER_QUICK_GATE_COMMIT": os.environ.get("LMER_QUICK_GATE_COMMIT"),
        "LMER_PERSIST_AGENT_MEMORY": os.environ.get("LMER_PERSIST_AGENT_MEMORY"),
        "LMER_HUMAN_IDENTITY": resolve_human_identity(),
        # Optional git identity overrides for commits made inside the container.
        # When set, entrypoint.sh exports them as GIT_AUTHOR_*/GIT_COMMITTER_*
        # (session-scoped; the mounted ~/.gitconfig is left untouched). Either
        # may be set independently; the unset half falls back to gitconfig.
        "LMER_GIT_USER_NAME": os.environ.get("LMER_GIT_USER_NAME"),
        "LMER_GIT_USER_EMAIL": os.environ.get("LMER_GIT_USER_EMAIL"),
        "LMER_REPO_URL": repo_url,
        "LMER_WORK_REPO": work_repo_url,
        "LMER_WORK_REPO_PATH": os.environ.get("LMER_WORK_REPO_PATH", "/work"),
        # Task routing env
        "LMER_TASK": task_id if not ns.no_task else None,
        "LMER_TASK_TARGET": primary_target if not ns.no_task else None,
        "LMER_SECONDARY_TARGETS": ",".join(secondary_targets) if secondary_targets else None,
        "LMER_CORE_TASKS": ",".join(sorted(known_tasks)) if (known_tasks and not ns.no_task) else None,
        # Container paths for task definitions (remapped from host paths).
        "LMER_TASKDEF_ROOT": container_taskdef_root,
        "LMER_TASKDEF_DIR": container_taskdef_dir,
        "LMER_TASK_INSTRUCTIONS": container_task_instructions,
        # External taskdef search paths (container-side) for Jinja2 includes
        "LMER_TASKDEF_PATHS": ":".join(container_taskdef_paths) if container_taskdef_paths else None,
        "LMER_CHECKOUT_BRANCH": ns.branch if ns.branch else None,
        "LMER_CHECKOUT_REF": ns.ref if ns.ref else None,
        "LMER_GIT_REMOTE": ns.remote if ns.remote else None,
        # GitLab MR environment variables (set when task_target is a GitLab MR URL)
        "GITLAB_HOST": gitlab_host,
        "GITLAB_PROJECT": gitlab_project,
        "GITLAB_MR_ID": gitlab_mr_id,
        "GITLAB_REVIEW_FILE": "review.json",
        # Repository parsing for work repo directory structure
        "LMER_REPO_HOST": repo_host,
        "LMER_REPO_PROJECT": repo_project,
        # Service mode environment variables
        "LMER_SERVICE_MODE": "1" if service_mode else None,
        "LMER_SERVICE_CONTAINER": service_container_id,
        "LMER_SERVICE_NAME": ns.service if service_mode else None,
        "LMER_SERVICE_WORKDIR": service_workdir,
        # Supervisor / FastAPI controls (consumed inside the container by lmer-supervisor)
        "LMER_FASTAPI": "1" if ns.fastapi else None,
        "LMER_MANUAL_START": "1" if ns.manual_start else None,
        # Follow-up prompt the in-container supervisor injects immediately
        # after the auto-/start. Sourced from --prompt; no-op under
        # --manual-start (the supervisor only injects it as part of auto-start).
        "LMER_START_PROMPT": ns.prompt if ns.prompt else None,
        "LMER_DISABLE_SUPERVISOR": "1" if ns.no_supervisor else None,
        # Forward the initial auto-/start delay so a host-set value reaches
        # the supervisor running inside the container.
        "LMER_AUTO_START_DELAY": os.environ.get("LMER_AUTO_START_DELAY"),
        # Forward the auto-/start CR-nudge delay so a host-set value reaches
        # the supervisor running inside the container.
        "LMER_AUTO_START_NUDGE_DELAY": os.environ.get("LMER_AUTO_START_NUDGE_DELAY"),
        # Forward the prompt-ready marker wait timeout (default 15s in-container).
        "LMER_AUTO_START_READY_TIMEOUT": os.environ.get("LMER_AUTO_START_READY_TIMEOUT"),
        # Forward the prompt-ready marker bytes (UTF-8) so the in-container
        # supervisor can be re-tuned without a release if claude changes the
        # input-prompt glyph.
        "LMER_AUTO_START_READY_MARKER": os.environ.get("LMER_AUTO_START_READY_MARKER"),
        "LMER_FASTAPI_PORT_RANGE": ns.fastapi_port_range,
        # When --fastapi is on we publish the port range to the host, which
        # only works if the container-side bind is 0.0.0.0. The host CLI
        # publishes to 127.0.0.1 on the host, so it is not network-exposed.
        "LMER_FASTAPI_HOST": ns.fastapi_host or ("0.0.0.0" if ns.fastapi else None),
        "LMER_FASTAPI_TOKEN": ns.fastapi_token,
    }

    # Merge all variables from .env file into container env dict
    # Check state dir (~/.lmer/) and cwd for .env files
    # Working directory takes highest priority — load in reverse order
    # (highest priority first) since we use a first-wins pattern (if key not in env).
    cwd_env_file = Path.cwd() / ".env"

    env_files_to_load = []

    # Load from cwd first (highest priority)
    if cwd_env_file.exists():
        env_files_to_load.append(("working directory", cwd_env_file))

    # Load from state dir (~/.lmer/)
    state_env_file = state_dir / ".env"
    if state_env_file.exists():
        already_listed = any(f == state_env_file for _, f in env_files_to_load)
        if not already_listed and state_env_file != cwd_env_file:
            env_files_to_load.append(("lmer state dir", state_env_file))

    # Track which keys came from .env files (not hardcoded in env dict above)
    env_file_keys: set[str] = set()

    if env_files_to_load:
        for location, env_file in env_files_to_load:
            success(f"✅ Found .env file at {location}: {env_file}")
            dotenv_vars = dotenv_values(dotenv_path=str(env_file))
            success(f"📋 Loaded {len(dotenv_vars)} variables from .env file in {location}")
            # Merge .env variables into env dict
            # - Hardcoded env values (set above) take precedence over .env
            # - Later .env files (cwd) override earlier ones (repo root)
            merged_count = 0
            for key, value in dotenv_vars.items():
                if value is None:
                    continue
                # Allow override if key came from a previous .env file
                # but not if it was hardcoded in the env dict
                if key not in env or key in env_file_keys:
                    env[key] = value
                    env_file_keys.add(key)
                    merged_count += 1
            success(f"✅ Merged {merged_count} variables from {location} .env into container environment")
    else:
        searched = [str(cwd_env_file), str(state_env_file)]
        success(f"⚠️  .env file not found at: {' or '.join(searched)}")

    # Publish the FastAPI port so the endpoint is reachable from the host.
    # We pick one free port from the configured range on the host before the
    # container starts and publish only that port (rather than the whole
    # range) so multiple `lmer ... --fastapi` sessions can coexist on the
    # same host with default flags. The chosen port is passed into the
    # container via LMER_FASTAPI_PORT so the supervisor inside binds to it.
    if ns.fastapi:
        from .supervisor import _parse_port_range, _pick_port
        port_range_spec = ns.fastapi_port_range or "8700-8799"
        port_range = _parse_port_range(port_range_spec)
        host_port = _pick_port(port_range, "127.0.0.1")
        env["LMER_FASTAPI_PORT"] = str(host_port)
        _publish_loopback_ports(run, [host_port])
        info(f"🛰  FastAPI endpoint will be published on http://127.0.0.1:{host_port}")

    # General port passthrough (--ports / --port-pool): allocate N free ports
    # and publish them so a service Claude runs inside is reachable on the host.
    port_rc = _apply_port_passthrough(ns, env, run)
    if port_rc is not None:
        return port_rc

    run += env_args(env)

    # Image name was resolved earlier (before ensure_image)
    run += [image]

    # Container command
    # Service mode and checkout mode still use clone_and_exec.py for work repo
    # and MR branch handling — only --no-clone/--no-task skip it entirely.
    use_clone_script = not (ns.no_clone or ns.no_task)
    if not use_clone_script:
        # Skip clone script entirely, run command directly
        cmd_tokens = rest if rest else ["bash"]
        run += cmd_tokens
    else:
        # Call the container clone+exec script from mounted repo
        clone_script = "/Agents/global/src/lmer_cli/container/clone_and_exec.py"

        if exec_mode:
            # Remaining tokens after --exec are the command to run; if empty, default to bash
            cmd_tokens = rest if rest else ["bash"]
            run += [
                "python3",
                clone_script,
                "--",
                *cmd_tokens,
            ]
        else:
            # Default to Claude runner, forward any residual args to runner
            run += [
                "python3",
                clone_script,
                "--",
                "claude-runner",
            ]

    info("Running: " + shlex.join(run))
    try:
        return subprocess.call(run)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
