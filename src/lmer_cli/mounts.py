"""
Container volume mount configuration.

This module handles building Docker/Podman volume mount arguments for various
components including workspaces, global configurations, repositories, and user
home directory files. It handles differences between Docker and Podman, including
SELinux labeling requirements for Podman.
"""

import os
import sys
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional, Tuple

from lmer_cli.harness import get_harness
from lmer_cli.runtime import _is_selinux_enforcing

EXTERNAL_TASKDEF_MOUNT_BASE = "/Agents/taskdefs"

# Path inside the container where uv looks for its cache by default
# (HOME=/home/developer + XDG default of $HOME/.cache/uv).
CONTAINER_UV_CACHE_DIR = "/home/developer/.cache/uv"

# Path inside the container where the persistent git clone cache is mounted.
# The container clone script (clone_and_exec.py) receives it via
# LMER_CLONE_CACHE_PATH and keeps one bare mirror per repo under it.
CONTAINER_CLONE_CACHE_DIR = "/clone-cache"


def selinux_opt(runtime: str) -> str:
    """
    Return SELinux volume label suffix when needed.

    Both Docker and Podman require ,z suffix for SELinux private labeling
    to allow container access to mounted volumes on SELinux-enabled systems.

    Args:
        runtime: Container runtime name ('docker' or 'podman')

    Returns:
        ',z' if SELinux is enforcing, empty string otherwise
    """
    return ",z" if _is_selinux_enforcing() else ""


def build_workspace_mount(
    runtime: str,
    workspace_volume: Optional[str],
    workspace_bind: Optional[Path],
) -> List[str]:
    """
    Build volume mount arguments for /workspace directory.

    Creates mount for the container's working directory. Prioritizes bind mount
    over named volume, falls back to tmpfs for ephemeral workspaces.

    Args:
        runtime: Container runtime ('docker' or 'podman')
        workspace_volume: Optional named volume name
        workspace_bind: Optional host path to bind mount

    Returns:
        List of Docker/Podman arguments for workspace mount
    """
    args: List[str] = []
    se = selinux_opt(runtime)
    if workspace_bind:
        args += ["-v", f"{workspace_bind}:/workspace:rw{se}"]
    elif workspace_volume:
        args += ["-v", f"{workspace_volume}:/workspace:rw{se}"]
    else:
        if runtime == "docker":
            args += ["--mount", "type=tmpfs,destination=/workspace"]
        else:
            args += ["--tmpfs", "/workspace"]
    return args


def build_global_mount(runtime: str, repo_root: Path) -> List[str]:
    """
    Build volume mounts for the global rules repository.

    Mounts specific directories from the lmer repository into the container
    at /Agents/global. Excludes .venv to allow container to use its own
    Python environment built during image creation.

    Args:
        runtime: Container runtime ('docker' or 'podman')
        repo_root: Path to the lmer repository root

    Returns:
        List of Docker/Podman arguments for global mounts
    """
    se = selinux_opt(runtime)
    args: List[str] = []

    # Directories needed by the container (excludes .venv, cache, build artifacts)
    # Read-write directories
    rw_dirs = ["bin", "src", "hooks", "Ctl", "libexec"]
    # Read-only directories
    ro_dirs = [".claude", "agent-files", "rules", "taskdef"]
    # Individual files
    ro_files = ["AGENTS.md"]
    rw_files = [".env"]

    for dir_name in rw_dirs:
        dir_path = repo_root / dir_name
        if dir_path.exists():
            args += ["-v", f"{dir_path}:/Agents/global/{dir_name}:rw{se}"]

    for dir_name in ro_dirs:
        dir_path = repo_root / dir_name
        if dir_path.exists():
            args += ["-v", f"{dir_path}:/Agents/global/{dir_name}:ro{se}"]

    for file_name in ro_files:
        file_path = repo_root / file_name
        if file_path.exists():
            args += ["-v", f"{file_path}:/Agents/global/{file_name}:ro{se}"]

    for file_name in rw_files:
        file_path = repo_root / file_name
        if file_path.exists():
            args += ["-v", f"{file_path}:/Agents/global/{file_name}:rw{se}"]

    return args


def build_lmer_docs_mount(runtime: str, repo_root: Path) -> List[str]:
    """
    Build volume mount for lmer-docs directory.

    Mounts the lmer-docs directory into the container at /Agents/global/lmer-docs.

    Args:
        runtime: Container runtime ('docker' or 'podman')
        repo_root: Path to the lmer repository root

    Returns:
        List of Docker/Podman arguments for lmer-docs mount
    """
    se = selinux_opt(runtime)
    lmer_docs = repo_root / "lmer-docs"
    if lmer_docs.exists():
        return ["-v", f"{lmer_docs}:/Agents/global/lmer-docs:ro{se}"]
    return []


def build_host_repo_ro_mount(runtime: str, host_repo_path: Path) -> List[str]:
    """
    Build read-only mount for local repository on host.

    When the target is a local git repository, mounts it read-only into
    the container for reference during cloning.

    Args:
        runtime: Container runtime ('docker' or 'podman')
        host_repo_path: Path to local repository on host

    Returns:
        List of Docker/Podman arguments for host repo mount
    """
    se = selinux_opt(runtime)
    return ["-v", f"{host_repo_path}:/host-repo:ro{se}"]


def build_container_home_mounts(runtime: str, container_home: Path) -> List[str]:
    """
    Build mounts for persistent container-home directory.

    Mounts persistent directories from container-home for SSH keys, configs,
    shell history, and app data that should persist across container sessions.

    Args:
        runtime: Container runtime ('docker' or 'podman')
        container_home: Path to container-home directory on host

    Returns:
        List of Docker/Podman arguments for container-home mounts
    """
    args: List[str] = []
    se = selinux_opt(runtime)

    # Mount SSH directory for git/ssh operations
    ssh_dir = container_home / ".ssh"
    if ssh_dir.exists():
        args += ["-v", f"{ssh_dir}:/home/developer/.ssh:rw{se}"]

    # Mount config directory for various app configs
    config_dir = container_home / ".config"
    if config_dir.exists():
        args += ["-v", f"{config_dir}:/home/developer/.config:rw{se}"]

    # Mount bash history for persistence
    bash_history = container_home / ".bash_history"
    if bash_history.exists():
        args += ["-v", f"{bash_history}:/home/developer/.bash_history:rw{se}"]

    # Mount gitconfig if it exists (read-write so git can add safe.directory)
    gitconfig = container_home / ".gitconfig"
    if gitconfig.exists():
        args += ["-v", f"{gitconfig}:/home/developer/.gitconfig:rw{se}"]

    # Mount only specific .local subdirectories to preserve image-installed
    # binaries in .local/bin/ and .local/share/claude/ (claude CLI)
    for subdir in ["share/mise", "state"]:
        local_sub = container_home / ".local" / subdir
        if local_sub.exists():
            args += ["-v", f"{local_sub}:/home/developer/.local/{subdir}:rw{se}"]

    return args


def build_user_mounts(runtime: str, harness=None, extra_harnesses=()) -> Tuple[List[str], bool]:
    """
    Build mounts for user configuration files and SSH agent.

    Mounts the active harness's credential files (per its registry entry —
    individual files rather than whole config directories, which avoids
    ownership issues with other subdirectories) and the SSH authentication
    socket from the host user's home directory into the container.

    Args:
        runtime: Container runtime ('docker' or 'podman')
        harness: Harness registry entry whose credential files to mount;
            defaults to claude (the historical behavior)
        extra_harnesses: Additional harness registry entries whose credential
            files to mount as well — the harnesses implied by the ``--agents``
            fan-out selection, so a child routed to a non-session harness can
            authenticate (issue #131). Duplicate credential entries are
            mounted once; missing host files are skipped as usual.

    Returns:
        Tuple of (mount arguments, ssh_agent_enabled flag)
    """
    if harness is None:
        harness = get_harness("claude")

    args: List[str] = []
    se = selinux_opt(runtime)
    home = Path.home()
    ssh_agent_enabled = False

    seen = set()
    for entry in (harness, *extra_harnesses):
        for cred in entry.credential_mounts:
            if cred in seen:
                continue
            seen.add(cred)
            host_file = home / cred.host_path
            if host_file.exists():
                args += ["-v", f"{host_file}:{cred.container_path}:{cred.mode}{se}"]

    # SSH agent
    ssh_sock = os.environ.get("SSH_AUTH_SOCK")
    if ssh_sock:
        args += ["-v", f"{ssh_sock}:/ssh-agent:ro{se}", "-e", "SSH_AUTH_SOCK=/ssh-agent"]
        ssh_agent_enabled = True
    return args, ssh_agent_enabled


class FileMountSpec(NamedTuple):
    """A single host-file → container-destination bind mount.

    Produced by ``cli.parse_file_mount_specs`` (which owns all validation);
    by the time a spec exists, ``host`` is an existing file, ``container``
    is an absolute path, and ``mode`` is ``ro`` or ``rw``.
    """

    host: Path
    container: str
    mode: str = "ro"


def build_file_mounts(runtime: str, specs: Iterable[FileMountSpec]) -> List[str]:
    """
    Build volume mounts for explicit per-file mounts (--mount-file).

    Bind-mounts each host file at its container destination, ``ro`` unless
    the spec asks for ``rw``. The file is mounted as-is — never copied — so
    credentials stay wherever the user keeps them on the host.

    Args:
        runtime: Container runtime ('docker' or 'podman')
        specs: Validated file-mount specs

    Returns:
        List of Docker/Podman arguments for the file mounts
    """
    se = selinux_opt(runtime)
    args: List[str] = []
    for spec in specs:
        args += ["-v", f"{spec.host}:{spec.container}:{spec.mode}{se}"]
    return args


def _find_container_socket(runtime: str) -> Optional[Path]:
    """Find the container runtime socket path."""
    candidates: List[str] = []
    if runtime == "podman":
        # Podman: rootless first, then rootful
        uid = os.getuid()
        candidates = [
            f"/run/user/{uid}/podman/podman.sock",
            "/var/run/podman/podman.sock",
            "/run/podman/podman.sock",
        ]
    else:
        candidates = ["/var/run/docker.sock"]

    for path_str in candidates:
        p = Path(path_str)
        if p.exists():
            return p
    return None


def build_external_taskdef_mounts(
    runtime: str,
    taskdef_paths: List[Path],
) -> Tuple[List[str], List[str]]:
    """
    Build volume mounts for external taskdef directories.

    Each host path in *taskdef_paths* is mounted read-only into the container
    under /Agents/taskdefs/<index>.  The function returns both the mount args
    and the corresponding container-side paths so callers can build
    LMER_TASKDEF_PATHS for the container environment.

    Args:
        runtime: Container runtime ('docker' or 'podman')
        taskdef_paths: Host-side paths from LMER_TASKDEF_PATHS

    Returns:
        (mount_args, container_paths) – Docker/Podman args and the
        container-side directories in the same order as *taskdef_paths*.
    """
    se = selinux_opt(runtime)
    args: List[str] = []
    container_paths: List[str] = []
    for idx, host_path in enumerate(taskdef_paths):
        if not host_path.exists() or not host_path.is_dir():
            continue
        container_dir = f"{EXTERNAL_TASKDEF_MOUNT_BASE}/{idx}"
        args += ["-v", f"{host_path}:{container_dir}:ro{se}"]
        container_paths.append(container_dir)
    return args, container_paths


def build_checkout_mount(runtime: str, checkout_path: Path) -> List[str]:
    """
    Build volume mount for a local checkout as /workspace.

    Used when --checkout is specified without --service (no Docker socket needed).

    Args:
        runtime: Container runtime ('docker' or 'podman')
        checkout_path: Absolute path to local source checkout on host

    Returns:
        List of Docker/Podman arguments for the workspace mount
    """
    se = selinux_opt(runtime)
    return ["-v", f"{checkout_path}:/workspace:rw{se}"]


def resolve_host_uv_cache_dir() -> Path:
    """
    Resolve the host's uv cache directory using uv's own resolution rules.

    Honors `$UV_CACHE_DIR` first, then `$XDG_CACHE_HOME/uv`, falling back to the
    platform-appropriate default (`~/.cache/uv` on Linux, `~/Library/Caches/uv`
    on macOS). The returned path may not exist on disk — callers should check.

    Returns:
        Path to the host uv cache directory (existence not guaranteed)
    """
    explicit = os.environ.get("UV_CACHE_DIR")
    if explicit:
        return Path(explicit).expanduser()

    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "uv"

    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Caches" / "uv"
    return home / ".cache" / "uv"


def build_host_uv_cache_mount(runtime: str, host_cache_dir: Path) -> List[str]:
    """
    Build read-write mount for the host's uv cache directory.

    Mounts the host's uv cache at the container's default uv cache location so
    `uv` operations inside the container (e.g. installing project dependencies
    in the target repo) reuse already-downloaded packages instead of fetching
    them from PyPI. Mounted read-write so newly installed packages populate the
    shared cache and benefit subsequent sessions on either side.

    Args:
        runtime: Container runtime ('docker' or 'podman')
        host_cache_dir: Path to the uv cache directory on the host

    Returns:
        List of Docker/Podman arguments for the uv cache mount
    """
    se = selinux_opt(runtime)
    return ["-v", f"{host_cache_dir}:{CONTAINER_UV_CACHE_DIR}:rw{se}"]


def resolve_host_clone_cache_dir() -> Path:
    """
    Resolve the host directory for the persistent git clone cache.

    Honors `$LMER_CLONE_CACHE_DIR` first (with `~` expansion); an empty or
    unset value falls back to `~/.lmer/clone-cache`. The returned path may
    not exist on disk — the caller creates it before mounting.

    Returns:
        Path to the host clone-cache directory (existence not guaranteed)
    """
    explicit = os.environ.get("LMER_CLONE_CACHE_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".lmer" / "clone-cache"


def build_clone_cache_mount(runtime: str, host_cache_dir: Path) -> List[str]:
    """
    Build the read-only mount for the persistent git clone cache.

    Mounts the host cache directory at the container's fixed clone-cache
    location so the container's clone script (clone_and_exec.py) can borrow
    objects from the host-maintained bare mirrors via ``--reference
    <mirror> --dissociate``. All mirror maintenance is host-side
    (lmer_cli.clone_cache, forked at launch); the container only ever
    reads, so the mount is ``:ro`` — a session structurally cannot write a
    token into, or corrupt, the shared cache. A stale or empty cache is
    fine: the clone still talks to the real origin and fetches whatever the
    mirror lacks.

    Args:
        runtime: Container runtime ('docker' or 'podman')
        host_cache_dir: Path to the clone-cache directory on the host

    Returns:
        List of Docker/Podman arguments for the clone cache mount
    """
    se = selinux_opt(runtime)
    return ["-v", f"{host_cache_dir}:{CONTAINER_CLONE_CACHE_DIR}:ro{se}"]


def build_service_mode_mounts(runtime: str, checkout_path: Path) -> List[str]:
    """
    Build volume mounts for service mode.

    Mounts the local checkout as /workspace and the container runtime socket
    for exec access to the target service container.

    Args:
        runtime: Container runtime ('docker' or 'podman')
        checkout_path: Absolute path to local source checkout on host

    Returns:
        List of Docker/Podman arguments for service mode mounts
    """
    args = build_checkout_mount(runtime, checkout_path)

    # Mount container runtime socket for exec access
    sock = _find_container_socket(runtime)
    if sock is not None:
        # Always mount to /var/run/docker.sock inside the container so that
        # docker-cli (installed in the image) can find it regardless of which
        # runtime is on the host.
        container_sock = "/var/run/docker.sock"
        args += ["-v", f"{sock}:{container_sock}:rw"]
        # Add the socket's group so the container user can access it
        try:
            sock_gid = sock.stat().st_gid
            args += ["--group-add", str(sock_gid)]
        except OSError:
            pass

    return args
