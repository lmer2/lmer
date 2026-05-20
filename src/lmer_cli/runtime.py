"""
Container runtime detection and configuration.

This module handles:
- Detecting available container runtime (Docker or Podman)
- Building base container run arguments with resource limits
- Constructing environment variable arguments
- Determining repository root path and install mode
- TTY configuration for interactive sessions
"""

import shutil
import subprocess
import sys
import tomllib
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import List


class InstallMode(Enum):
    """How the lmer CLI was installed and is being run."""

    DEVELOPER = "developer"  # Running from a git checkout
    INSTALLED = "installed"  # Installed via uv tool install (no local repo)


class RuntimeErrorDetect(Exception):
    """Exception raised when no container runtime is found."""
    pass


_LMER_STATE_DIR = Path.home() / ".lmer"


@lru_cache(maxsize=1)
def _is_selinux_enforcing() -> bool:
    """
    Check if SELinux is enabled and enforcing.

    Returns:
        True if SELinux is enforcing, False otherwise
    """
    try:
        result = subprocess.run(
            ["getenforce"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "enforcing"
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def detect_runtime() -> str:
    """
    Detect available container runtime on the system.

    Checks PATH for docker and podman binaries, in that order.

    Returns:
        'docker' or 'podman'

    Raises:
        RuntimeErrorDetect: If neither Docker nor Podman is found
    """
    if shutil.which("docker"):
        return "docker"
    if shutil.which("podman"):
        return "podman"
    raise RuntimeErrorDetect("Neither Docker nor Podman found in PATH")


def tty_flags() -> List[str]:
    """
    Determine TTY flags for container.

    Returns:
        ['-it'] if stdin is a TTY, empty list otherwise
    """
    # mimic lmer: use -it if stdin is a tty
    if sys.stdin.isatty():
        return ["-it"]
    return []


def _is_lmer_pyproject(path: Path) -> bool:
    """Check if a pyproject.toml belongs to lmer."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return data.get("project", {}).get("name") in ("lmer", "lmer-cli")
    except (OSError, tomllib.TOMLDecodeError):
        return False


def detect_install_mode() -> InstallMode:
    """
    Detect whether lmer is running from a git checkout or an installed package.

    Developer mode: __file__ is inside a directory tree containing a
    pyproject.toml with project.name == "lmer" (or legacy "lmer-cli").

    Installed mode: No such pyproject.toml found (e.g. uv tool install
    places the package in an isolated venv).

    Returns:
        InstallMode.DEVELOPER or InstallMode.INSTALLED
    """
    p = Path(__file__).resolve()
    for parent in [p] + list(p.parents):
        pyproject = parent / "pyproject.toml"
        if pyproject.exists() and _is_lmer_pyproject(pyproject):
            return InstallMode.DEVELOPER
    return InstallMode.INSTALLED


def repo_root_path() -> Path | None:
    """
    Find the root path of the lmer repository.

    Searches up the directory tree from this file for a pyproject.toml
    belonging to lmer.

    Returns:
        Path to repository root in developer mode, None in installed mode.
    """
    if detect_install_mode() == InstallMode.INSTALLED:
        return None
    p = Path(__file__).resolve()
    for parent in [p] + list(p.parents):
        pyproject = parent / "pyproject.toml"
        if pyproject.exists() and _is_lmer_pyproject(pyproject):
            return parent
    return None


def lmer_state_dir() -> Path:
    """
    Get the directory for LMER runtime state (~/.lmer/).

    Used for container-home, .env, and other persistent data
    in both developer and installed modes.

    Returns:
        Path to ~/.lmer/
    """
    return _LMER_STATE_DIR


def base_run_args(runtime: str, exec_mode: bool, user: str) -> List[str]:
    """
    Build base container run arguments.

    Includes:
    - Runtime and run command
    - TTY flags
    - Resource limits (CPU, memory, PIDs)
    - Security options
    - User and working directory

    Args:
        runtime: Container runtime ('docker' or 'podman')
        exec_mode: Whether running in exec mode
        user: Container user specification (e.g., 'developer' or '1000:1000')

    Returns:
        List of base Docker/Podman arguments
    """
    args: List[str] = [runtime, "run", "--rm"]
    args += tty_flags()
    # Resource limits and security
    args += ["--cpus", "1", "--memory", "2g", "--pids-limit", "512", "--security-opt", "no-new-privileges"]
    # Disable SELinux labeling to allow SSH agent socket access
    # Container processes (container_t) cannot connect to user sockets (unconfined_t) by default
    if _is_selinux_enforcing():
        args += ["--security-opt", "label=disable"]
    # Podman-specific: maintain user ID mapping for SSH agent and file permissions
    if runtime == "podman":
        args += ["--userns=keep-id"]
    # User and workdir
    args += ["--user", user, "-w", "/workspace"]
    return args


def env_args(env: dict) -> List[str]:
    """
    Build environment variable arguments for container.

    Args:
        env: Dictionary of environment variables (None values are skipped)

    Returns:
        List of -e arguments for Docker/Podman
    """
    args: List[str] = []
    for k, v in env.items():
        if v is None:
            continue
        args += ["-e", f"{k}={v}"]
    return args
