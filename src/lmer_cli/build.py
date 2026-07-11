"""
Container image provisioning for LMER CLI.

Handles auto-building (developer mode) or pulling from a registry
(installed mode) the container image when it doesn't exist locally.
"""

import importlib.metadata
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .log import error, success

DEFAULT_IMAGE = "lmer"
DEFAULT_REGISTRY = "ghcr.io/lmer2/lmer"


def image_exists(runtime: str, image: str) -> bool:
    """Check if a container image exists locally."""
    try:
        result = subprocess.run(
            [runtime, "image", "inspect", image],
            capture_output=True,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def delete_image(runtime: str, image: str) -> bool:
    """Delete a container image if it exists."""
    if not image_exists(runtime, image):
        return True  # Already gone
    try:
        result = subprocess.run(
            [runtime, "rmi", "-f", image],
            capture_output=True,
            timeout=60,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def checkout_commit(repo_root: Path | None) -> str | None:
    """Short commit of a git checkout, with a ``-dirty`` suffix when the
    tree has uncommitted changes. None when not a checkout (installed mode)
    or git is unavailable."""
    if repo_root is None or not (repo_root / ".git").exists():
        return None
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=10,
        )
        if rev.returncode != 0:
            return None
        commit = rev.stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=10,
        )
        if status.returncode == 0 and status.stdout.strip():
            commit += "-dirty"
        return commit
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _build_provenance(repo_root: Path) -> str:
    """Human-readable provenance string baked into the image (BUILD_INFO)."""
    commit = checkout_commit(repo_root) or "unknown"
    branch = "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    built = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{commit} ({branch}) built={built}"


def build_image_local(runtime: str, image: str, repo_root: Path, pull: bool = True, update_claude: bool = False) -> int:
    """Build container image from local repo checkout (developer mode)."""
    import time

    uid = os.getuid()
    gid = os.getgid()
    # --cache-from only works with Docker; Podman caches layers automatically
    cache_args = ["--cache-from", image] if Path(runtime).name == "docker" else []
    claude_args = ["--build-arg", f"CLAUDE_CACHE_BUST={int(time.time())}"] if update_claude else []
    cmd = [
        runtime, "build",
        *cache_args,
        *(["--pull"] if pull else []),
        "--build-arg", f"BUILD_UID={uid}",
        "--build-arg", f"BUILD_GID={gid}",
        "--build-arg", f"LMER_BUILD_COMMIT={_build_provenance(repo_root)}",
        *claude_args,
        "-t", image,
        "-f", "Containerfile",
        ".",
    ]
    success(f"Building image {image} from local repo...")
    return subprocess.call(cmd, cwd=str(repo_root))


def resolve_image_tag(repo_root: Path | None) -> str | None:
    """Derive a versioned image name based on install method.

    Resolution order:
    1. Developer mode (git checkout): use git short SHA
    2. Git install (PEP 610 direct_url.json): use commit SHA
    3. PyPI/registry install: use package version
    4. Returns None if version cannot be determined
    """
    # 1. Developer mode: git short SHA from repo checkout
    if repo_root is not None and (repo_root / ".git").exists():
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return f"{DEFAULT_IMAGE}:{result.stdout.strip()}"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    # 2. Git install: PEP 610 direct_url.json has commit SHA
    try:
        dist = importlib.metadata.distribution("lmer")
        raw = dist.read_text("direct_url.json")
        if raw:
            data = json.loads(raw)
            commit = data.get("vcs_info", {}).get("commit_id", "")
            if commit:
                return f"{DEFAULT_IMAGE}:{commit[:7]}"
    except Exception:
        pass

    # 3. Version from package metadata (PyPI install)
    try:
        version = importlib.metadata.version("lmer")
        return f"{DEFAULT_IMAGE}:{version}"
    except Exception:
        pass

    # No fallback — caller must handle None
    return None


def pull_image(runtime: str, image: str, registry: str | None = None) -> int:
    """Pull pre-built image from registry and tag as expected local name."""
    if not registry:
        error("❌ Registry URL is required for pull but not provided. Set LMER_REGISTRY.")
        return 1
    # Extract tag from the image basename to avoid confusing a registry port with a tag
    # e.g. "registry.example.com:5000/lmer" should default to "latest", not "5000/lmer"
    name_part = image.rsplit("/", 1)[-1]
    tag = name_part.split(":", 1)[1] if ":" in name_part else "latest"
    registry_image = f"{registry}:{tag}"

    success(f"Pulling image from {registry_image}...")
    rc = subprocess.call([runtime, "pull", registry_image])
    if rc != 0:
        return rc

    if registry_image != image:
        rc = subprocess.call([runtime, "tag", registry_image, image])
    return rc


def ensure_image(
    runtime: str,
    image: str,
    repo_root: Path | None,
    skip_build: bool = False,
) -> bool:
    """
    Ensure the container image exists, building or pulling if necessary.

    Strategy:
    1. If image already exists locally, return immediately.
    2. If auto-build is disabled, error and return False.
    3. If repo checkout has a Containerfile, build locally (developer mode).
    4. Pull from registry: ``LMER_REGISTRY`` if set (and non-empty),
       otherwise fall back to ``DEFAULT_REGISTRY``.

    Returns True if image is available, False if not.
    """
    if image_exists(runtime, image):
        return True

    if skip_build or os.environ.get("LMER_NO_AUTO_BUILD", "").lower() in ("1", "true", "yes"):
        error(f"Image {image} not found and auto-build is disabled")
        return False

    can_build_locally = repo_root is not None and (repo_root / "Containerfile").exists()

    if can_build_locally:
        rc = build_image_local(runtime, image, repo_root)
        if rc != 0:
            error(f"Image build failed with exit code {rc}")
            return False
        success("Image built successfully")
        return True

    # Pull from registry (defaults to the project's GHCR; LMER_REGISTRY overrides).
    # Treat empty-string the same as unset so a stray `LMER_REGISTRY=` in a .env
    # file doesn't bypass the default and produce a misleading downstream error.
    registry = os.environ.get("LMER_REGISTRY") or DEFAULT_REGISTRY
    rc = pull_image(runtime, image, registry)
    if rc == 0:
        success("Image pulled successfully")
        return True

    error(f"Pull from {registry} failed (expected tag: {image}).")
    error("If the registry is unreachable or the tag is missing, build locally instead:")
    error("    git clone https://github.com/lmer2/lmer /tmp/lmer-src")
    error("    lmer build --local /tmp/lmer-src")
    error("Or override the registry with LMER_REGISTRY=<host>/<path>.")
    return False


def build_image(
    runtime: str,
    image: str,
    repo_root: Path | None,
    force: bool = False,
    pull: bool = True,
    update_claude: bool = False,
) -> bool:
    """
    Build the container image from a local Containerfile.

    Args:
        runtime: Container runtime (docker/podman)
        image: Image name to build
        repo_root: Path to repo checkout (None in installed mode)
        force: If True, delete existing image before building
        pull: If True, pass --pull to docker build (refresh base image layers)
        update_claude: If True, bust cache for the Claude Code install layer

    Returns True if build succeeded, False otherwise.
    """
    if force and image_exists(runtime, image):
        success(f"Removing existing image {image}...")
        if not delete_image(runtime, image):
            error(f"Failed to remove existing image {image}")
            return False
        success("Image removed")

    can_build_locally = repo_root is not None and (repo_root / "Containerfile").exists()

    if can_build_locally:
        rc = build_image_local(runtime, image, repo_root, pull=pull, update_claude=update_claude)
        if rc != 0:
            error(f"Image build failed with exit code {rc}")
            return False
        success("Image built successfully")
        return True

    error("No Containerfile found in repo root")
    return False
