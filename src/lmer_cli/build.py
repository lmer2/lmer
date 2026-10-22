"""
Container image provisioning for LMER CLI.

Handles auto-building (developer mode) or pulling from a registry
(installed mode) the container image when it doesn't exist locally.
"""

import hashlib
import importlib.metadata
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .harness import get_harness
from .keep_id import image_id as resolve_image_id, prewarm
from .log import error, success
from .runtime import ambient_container_user, is_root_container_user

DEFAULT_IMAGE = "lmer"
DEFAULT_REGISTRY = "ghcr.io/lmer2/lmer"

# Files whose content is baked into the image AND consumed at runtime without
# a developer-mode bind mount shadowing them (build_global_mount covers
# bin/src/hooks/Ctl/libexec and friends under /Agents/global, and find_runner
# prefers the mounted libexec runner over the baked /home/developer copy).
# The developer-mode image tag is a hash of exactly this set (issue #351):
# a commit that touches none of these reuses the existing image and its
# already-chowned keep-id layer instead of paying podman's whole-rootfs
# remap again.
DEV_IMAGE_BUILD_INPUTS = (
    "Containerfile",
    "pyproject.toml",
    "uv.lock",
    ".mise.toml",
    ".mcp.json",
    "Ctl/container/entrypoint.sh",
    "Ctl/container/container-limits.sh",
    "agent-files/codex/requirements.toml",
)


def dev_image_input_hash(repo_root: Path) -> str:
    """Content hash of the declared developer-mode build inputs.

    Deterministic for absent/unreadable entries (hashed as a marker) so a
    checkout genuinely missing one resolves to a different, stable tag
    rather than an error. A directory entry is walked file-by-file — a bare
    absent-marker for it would contribute a constant and never move the
    tag, silently reusing a stale image when the directory's contents
    change (MR !249 review).
    """
    h = hashlib.sha256()

    def add(rel: str, path: Path) -> None:
        h.update(rel.encode())
        h.update(b"\0")
        try:
            if path.is_dir():
                for sub in sorted(p for p in path.rglob("*") if p.is_file()):
                    add(f"{rel}/{sub.relative_to(path)}", sub)
            else:
                h.update(path.read_bytes())
        except OSError:
            h.update(b"<absent>")
        h.update(b"\0")

    for rel in DEV_IMAGE_BUILD_INPUTS:
        add(rel, repo_root / rel)
    return h.hexdigest()[:12]


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


def build_image_local(
    runtime: str,
    image: str,
    repo_root: Path,
    pull: bool = True,
    update_claude: bool = False,
    update_harnesses: list[str] | None = None,
) -> int:
    """Build container image from local repo checkout (developer mode).

    ``update_harnesses`` names harness install layers whose Docker cache
    should be busted (each maps to its registry ``cache_bust_arg``);
    ``update_claude`` is the legacy spelling for ``["claude"]``.
    """
    uid = os.getuid()
    gid = os.getgid()
    # --cache-from only works with Docker; Podman caches layers automatically
    cache_args = ["--cache-from", image] if Path(runtime).name == "docker" else []
    bust = set(update_harnesses or [])
    if update_claude:
        bust.add("claude")
    harness_args: list[str] = []
    for name in sorted(bust):
        harness_args += ["--build-arg", f"{get_harness(name).cache_bust_arg}={int(time.time())}"]
    cmd = [
        runtime, "build",
        *cache_args,
        *(["--pull"] if pull else []),
        "--build-arg", f"BUILD_UID={uid}",
        "--build-arg", f"BUILD_GID={gid}",
        "--build-arg", f"LMER_BUILD_COMMIT={_build_provenance(repo_root)}",
        *harness_args,
        "-t", image,
        "-f", "Containerfile",
        ".",
    ]
    success(f"Building image {image} from local repo...")
    return subprocess.call(cmd, cwd=str(repo_root))


def resolve_image_tag(repo_root: Path | None) -> str | None:
    """Derive a versioned image name based on install method.

    Resolution order:
    1. Developer mode (git checkout with a Containerfile): hash of the
       declared build inputs (DEV_IMAGE_BUILD_INPUTS) — NOT the commit, so
       source-only commits keep reusing the existing image and its warmed
       keep-id layer (issue #351)
    2. Developer mode without a Containerfile: git short SHA (legacy)
    3. Git install (PEP 610 direct_url.json): use commit SHA
    4. PyPI/registry install: use package version
    5. Returns None if version cannot be determined
    """
    # 1. Developer mode with a buildable checkout: input-hash tag. Gated on
    # the Containerfile because that is also ensure_image's condition for
    # building locally — a .git dir alone doesn't make the hash meaningful.
    if (
        repo_root is not None
        and (repo_root / ".git").exists()
        and (repo_root / "Containerfile").exists()
    ):
        return f"{DEFAULT_IMAGE}:dev-{dev_image_input_hash(repo_root)}"

    # 2. Developer mode without a Containerfile: git short SHA
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

    # 3. Git install: PEP 610 direct_url.json has commit SHA
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

    # 4. Version from package metadata (PyPI install)
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


@dataclass(frozen=True)
class ImageReadiness:
    """Outcome of :func:`ensure_image`.

    ``materialized`` records that THIS call created the local image (built or
    pulled it). ``image_id`` is the image's content ID, which is what the
    keep-id remap marker is keyed on (``lmer_cli.keep_id``) — a rebuild reuses
    the tag but not the ID, so it is correctly cold again.

    ``materialized`` deliberately no longer gates the launch notice: it answers
    "did this call create the image", which is false for the slowest path there
    is — ``lmer build`` followed by a separate spawn (issue #384).
    """

    available: bool
    materialized: bool = False
    image_id: str | None = None


def ensure_image(
    runtime: str,
    image: str,
    repo_root: Path | None,
    skip_build: bool = False,
    container_root: bool = False,
) -> ImageReadiness:
    """
    Ensure the container image exists, building or pulling if necessary.

    Strategy:
    1. If image already exists locally, return immediately.
    2. If auto-build is disabled, error and return unavailable.
    3. If repo checkout has a Containerfile, build locally (developer mode).
    4. Pull from registry: ``LMER_REGISTRY`` if set (and non-empty),
       otherwise fall back to ``DEFAULT_REGISTRY``.

    Returns an :class:`ImageReadiness`: ``available`` says the image can be
    run, ``materialized`` says this call is what put it there, ``image_id``
    identifies it for the keep-id remap marker.

    ``container_root`` says the launch will run as container root, which skips
    keep-id entirely — there is then no remap to pre-warm.
    """
    if image_exists(runtime, image):
        return ImageReadiness(available=True, image_id=resolve_image_id(runtime, image))

    if skip_build or os.environ.get("LMER_NO_AUTO_BUILD", "").lower() in ("1", "true", "yes"):
        error(f"Image {image} not found and auto-build is disabled")
        return ImageReadiness(available=False)

    can_build_locally = repo_root is not None and (repo_root / "Containerfile").exists()

    if can_build_locally:
        rc = build_image_local(runtime, image, repo_root)
        if rc != 0:
            error(f"Image build failed with exit code {rc}")
            return ImageReadiness(available=False)
        success("Image built successfully")
        prewarm(runtime, image, container_root=container_root)
        return ImageReadiness(available=True, materialized=True,
                              image_id=resolve_image_id(runtime, image))

    # Pull from registry (defaults to the project's GHCR; LMER_REGISTRY overrides).
    # Treat empty-string the same as unset so a stray `LMER_REGISTRY=` in a .env
    # file doesn't bypass the default and produce a misleading downstream error.
    registry = os.environ.get("LMER_REGISTRY") or DEFAULT_REGISTRY
    rc = pull_image(runtime, image, registry)
    if rc == 0:
        success("Image pulled successfully")
        prewarm(runtime, image, container_root=container_root)
        return ImageReadiness(available=True, materialized=True,
                              image_id=resolve_image_id(runtime, image))

    error(f"Pull from {registry} failed (expected tag: {image}).")
    error("If the registry is unreachable or the tag is missing, build locally instead:")
    error("    git clone https://github.com/lmer2/lmer /tmp/lmer-src")
    error("    lmer build --local /tmp/lmer-src")
    error("Or override the registry with LMER_REGISTRY=<host>/<path>.")
    return ImageReadiness(available=False)


def build_image(
    runtime: str,
    image: str,
    repo_root: Path | None,
    force: bool = False,
    pull: bool = True,
    update_claude: bool = False,
    update_harnesses: list[str] | None = None,
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
            (legacy spelling for ``update_harnesses=["claude"]``)
        update_harnesses: Harness names whose install-layer cache to bust

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
        rc = build_image_local(
            runtime, image, repo_root, pull=pull,
            update_claude=update_claude, update_harnesses=update_harnesses,
        )
        if rc != 0:
            error(f"Image build failed with exit code {rc}")
            return False
        success("Image built successfully")
        # The whole point of `lmer build`: pay the keep-id remap here, not at
        # the first spawn (issue #351, napkin Part 3). This command has no
        # --root-userns flag, so the env toggle is the only way it can know the
        # host has opted out of keep-id entirely (!309 review).
        prewarm(
            runtime, image,
            container_root=is_root_container_user(ambient_container_user()),
        )
        return True

    error("No Containerfile found in repo root")
    return False
