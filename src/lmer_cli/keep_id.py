"""Podman's ``--userns=keep-id`` UID remap: pre-warming it, and remembering it.

Rootless podman materializes a re-owned copy of an image's whole rootfs
(``storage-chown-by-maps``) the first time that image runs under the keep-id
mapping — 7–12 minutes for lmer's 4.3 GB / 86.5k-inode image (issue #351).
The copy is cached per (image top layer, mapping), so the cost is one-time
per image; the problem is only ever *when* it is paid.

Two jobs here:

* :func:`prewarm` pays it right after a build or pull, where a human is
  already watching a progress-shaped command, instead of at the first spawn.
* the marker functions record that an image has been remapped, so a launch
  can tell a cold image from a warm one *across processes*. That is the gap
  ``ImageReadiness.materialized`` cannot close (issue #384): it answers "did
  THIS call create the image", which is false for the slowest path there is —
  ``lmer build`` followed by a separate spawn.
"""

import os
import subprocess
from pathlib import Path

from .log import success, warning
from .runtime import lmer_state_dir
from .util import get_bool_env

#: Ceiling on the pre-warm container, seconds. The remap itself legitimately
#: runs for minutes, so this is a wedge guard, not a deadline; override with
#: ``LMER_PREWARM_TIMEOUT``.
DEFAULT_PREWARM_TIMEOUT = 3600


def _prewarm_timeout() -> int:
    raw = os.environ.get("LMER_PREWARM_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_PREWARM_TIMEOUT
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        warning(
            f"⚠️  LMER_PREWARM_TIMEOUT must be a positive number of seconds "
            f"(got {raw!r}); using {DEFAULT_PREWARM_TIMEOUT}"
        )
        return DEFAULT_PREWARM_TIMEOUT
    return value


def image_id(runtime: str, image: str) -> str | None:
    """The image's content ID, or None when it cannot be read.

    The marker keys off this rather than the tag: a rebuild reuses the tag but
    produces a new ID, and its rootfs has never been remapped.
    """
    try:
        result = subprocess.run(
            [runtime, "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def remap_marker_path(image_id: str) -> Path:
    """Where the "this image has been remapped" marker for *image_id* lives."""
    return lmer_state_dir() / "remapped" / image_id.replace("/", "_")


def remap_recorded(image_id: str | None) -> bool:
    """Whether this user has already paid the keep-id remap for *image_id*.

    Unknown ID answers False: the notice it gates is cheap to over-print and
    expensive to omit (#384 — a missing one reads as a hang). The one false
    negative is a ``podman system reset``, which wipes the store but not these
    markers; the next build re-warms and rewrites them.
    """
    if not image_id:
        return False
    try:
        return remap_marker_path(image_id).exists()
    except OSError:
        # An unsearchable or otherwise unreadable state dir (mode 000, an ACL,
        # a root-owned ~/.lmer/remapped) makes Path.exists() raise rather than
        # answer — EACCES is not in the set pathlib swallows. This cache is
        # advisory; letting it abort the launch would turn a hint into a
        # prerequisite (!309 adversarial review, F3). Unknown answers False,
        # which prints the notice — the same conservative direction as an
        # unknown ID above.
        return False


def record_remap(image_id: str | None) -> None:
    """Record that *image_id*'s rootfs has been remapped. Best effort."""
    if not image_id:
        return
    path = remap_marker_path(image_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError as e:
        warning(f"⚠️  could not record the keep-id remap of {image_id[:19]}: {e}")


def prewarm(runtime: str, image: str, *, container_root: bool = False) -> bool:
    """Pay the keep-id remap for *image* now (issue #351, napkin Part 3).

    Returns True when the image is warm afterwards. Best effort throughout: a
    failure leaves the remap to happen lazily at first spawn, which is the
    behaviour before this function existed.
    """
    # Same predicate base_run_args gates the flag on (runtime.keep_id_applies):
    # warming a mapping the launch will not use is minutes spent for nothing.
    if runtime != "podman" or container_root:
        return False
    if get_bool_env("LMER_NO_PREWARM"):
        return False

    # Run against the resolved ID, not the tag: a concurrent rebuild that
    # retags between the inspect and the run would otherwise warm the new
    # image and record the old one, leaving a retained image certified warm
    # that nothing ever remapped (!309 adversarial review, F6). With an ID
    # the two cannot disagree. Falling back to the tag when the inspect
    # failed costs nothing: record_remap(None) is already a no-op, so such a
    # run warms the image without claiming it did.
    ident = image_id(runtime, image)
    target = ident or image
    success(
        f"Pre-warming the keep-id UID remap for {image} "
        "(several minutes on a new image; it is paid here instead of at the "
        "first session start)..."
    )
    # Deliberately minimal, and each omission is load-bearing:
    #   --entrypoint  the image's entrypoint installs mise tools over the
    #                 network and rewrites the container home; the remap needs
    #                 none of it. It also clears the image CMD.
    #   --network     nothing runs, so nothing needs a network namespace set up.
    #   no --cpus/--memory/--pids-limit: those are what makes crun abort when
    #                 the cgroup controllers are not delegated to the user
    #                 slice (runtime._resource_limit_args). A throwaway `true`
    #                 needs no bound, so the whole delegation question is
    #                 sidestepped rather than re-handled here.
    cmd = [
        runtime, "run", "--rm",
        "--userns=keep-id",
        "--network=none",
        "--entrypoint", "/usr/bin/true",
        target,
    ]
    timeout = _prewarm_timeout()
    try:
        rc = subprocess.call(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        warning(
            f"⚠️  UID remap pre-warm timed out after {timeout}s; "
            "the first session start will pay it instead"
        )
        return False
    except OSError as e:
        warning(
            f"⚠️  UID remap pre-warm failed to start ({e}); "
            "the first session start will pay it instead"
        )
        return False
    if rc != 0:
        warning(
            f"⚠️  UID remap pre-warm exited with code {rc}; "
            "the first session start will pay it instead"
        )
        return False

    record_remap(ident)
    success("Pre-warm complete — first session start will not wait on the remap")
    return True
