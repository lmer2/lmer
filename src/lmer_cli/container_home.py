"""
Container home directory management.

This module handles setup and management of a persistent container home directory
that preserves SSH keys, configs, shell history, and other user data across
container sessions. The container-home directory is stored at:
- Global install: ~/.lmer/container-home/
- Dev repo: <repo_root>/container-home/
"""

import shutil
from pathlib import Path
from typing import Optional

from .log import info, success


def get_container_home_path(repo_root: Path) -> Path:
    """
    Get the path to the container-home directory.

    Args:
        repo_root: Path to the lmer repository root

    Returns:
        Path to container-home directory
    """
    return repo_root / "container-home"


def ensure_container_home(repo_root: Path) -> Path:
    """
    Ensure container-home directory exists and is set up.

    Creates the directory structure if it doesn't exist, and copies
    initial configuration from the host user's home directory.

    Args:
        repo_root: Path to the lmer repository root

    Returns:
        Path to the container-home directory
    """
    container_home = get_container_home_path(repo_root)

    if container_home.exists():
        # Ensure critical subdirectories exist even on subsequent runs
        # (.config/mise is needed so the bind-mount doesn't shadow
        #  the build-time mise config without a place for entrypoint to recreate it)
        (container_home / ".config" / "mise").mkdir(parents=True, exist_ok=True)
        return container_home

    info("First run - setting up container home...")

    # Create directory structure
    ssh_path = container_home / ".ssh"
    ssh_path.mkdir(parents=True, exist_ok=True)
    ssh_path.chmod(0o700)  # SSH requires restrictive permissions
    (container_home / ".config" / "mise").mkdir(parents=True, exist_ok=True)
    (container_home / ".local" / "share").mkdir(parents=True, exist_ok=True)

    host_home = Path.home()

    # Copy SSH config if exists (but not private keys by default)
    ssh_dir = host_home / ".ssh"
    if ssh_dir.exists():
        info("Copying SSH config...")
        for config_file in ["config", "known_hosts"]:
            src = ssh_dir / config_file
            if src.exists():
                shutil.copy2(src, ssh_path / config_file)

        # Set proper permissions on copied files
        for ssh_file in ssh_path.iterdir():
            if ssh_file.is_file():
                ssh_file.chmod(0o600)

        info("Note: SSH private keys not copied automatically for security.")
        info("To use SSH in container, either:")
        info("  1. Copy specific keys: cp ~/.ssh/id_rsa* container-home/.ssh/")
        info("  2. Use SSH agent forwarding (recommended)")

    # Copy git config
    gitconfig = host_home / ".gitconfig"
    if gitconfig.exists():
        info("Copying git config...")
        shutil.copy2(gitconfig, container_home / ".gitconfig")

    # Create shell history file
    (container_home / ".bash_history").touch()

    success("✅ Container home setup complete!")
    info("")
    info("Directory structure:")
    info(f"  {container_home}/")
    info("  ├── .ssh/          (SSH config)")
    info("  ├── .config/       (App configs)")
    info("  ├── .local/share/  (App data)")
    info("  ├── .bash_history  (Shell history)")
    info("  └── .gitconfig     (Git config)")
    info("")

    return container_home
