"""The assistant's memory store — platform-local, one per host (issue #325).

One host directory mounted into every assistant incarnation, so the harness's
agent memory outlives the container it was written in. Design, deploy shape and
what the reported counts do and do not mean:
``docs/PLATFORM-QUICKSTART.md`` ("uber lmer's memory") and ``docs/HARNESSES.md``
note 8.

Two properties are load-bearing and easy to undo from here:

* The bind lands on :data:`CONTAINER_STAGED_DIR`, never on the harness's declared
  path — that path is inside the session's transcript mount, and a runtime
  creates a destination's missing parents root-owned before any container
  process runs (the #293/#290 EACCES). :func:`memory_links` hands the entrypoint
  the pair it symlinks as ``developer`` instead.
* Nothing here deletes or trims a memory file. :func:`observe` reports.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from lmer_cli.harness import HARNESSES
from lmer_cli.mounts import CONTAINER_MOUNT_STAGING_DIR
# The variable this feature closes off must be the one work_repo.memory gates on,
# or "platform-local only" is a comment rather than a guarantee.
from work_repo.memory import PERSIST_ENV_VAR

from . import store
from .store import append_event, platform_dir

logger = logging.getLogger("lmer_platform.memory")

__all__ = [
    "PERSIST_ENV_VAR", "CONTAINER_STAGED_DIR", "DIR_MODE",
    "WARN_BYTES", "WARN_FILES", "MEASURE_ENTRY_CAP",
    "Measurement", "memory_dir", "prepare_memory_dir", "harness_memory_dirs",
    "memory_links", "mount_flags", "measure", "observe",
]

#: One store per host, shared by every incarnation — the point of the feature.
DIRNAME = "assistant-memory"

#: Owner-only, like the rest of the platform's state tree.
DIR_MODE = store.STATE_DIR_MODE

#: Where the store is bound inside the container (staged, not declared).
CONTAINER_STAGED_DIR = f"{CONTAINER_MOUNT_STAGING_DIR}/assistant/memory"

#: Size and file count the store is *warned* about, never capped at.
WARN_BYTES = 256 * 1024
WARN_FILES = 50

#: Directory entries :func:`measure` looks at before giving up — every entry, not
#: only the files it counts: an empty directory or a dangling link costs the same
#: syscall while contributing nothing, so a cap on files found bounds nothing.
MEASURE_ENTRY_CAP = 5000


@dataclass(frozen=True)
class Measurement:
    """What the store holds right now, as an outside observer can see it."""

    files: int
    bytes: int
    #: Set only when the walk left entries unlooked-at, so "exact" and "at least
    #: this much" stay different answers.
    truncated: bool = False

    @property
    def large(self) -> bool:
        return self.files > WARN_FILES or self.bytes > WARN_BYTES

    def to_dict(self) -> dict:
        return {
            "files": self.files,
            "bytes": self.bytes,
            "truncated": self.truncated,
            "large": self.large,
        }


def memory_dir() -> Path:
    """The host store: ``<platform state>/assistant-memory``."""
    return platform_dir() / DIRNAME


def prepare_memory_dir() -> Optional[Path]:
    """Create the store owner-only and return it, or ``None`` if unusable.

    Fail-soft: the assistant still starts, and loses only durability. The
    fallback is thinner than it looks — with the work-repo route closed for this
    session no ``work memory restore`` runs either, and that restore was what
    created the harness's memory directory unconditionally.

    Usable is checked, not assumed: ``ensure_state_dir`` only clears bits
    *outside* ``STATE_DIR_MODE``, so a pre-existing narrow store comes back
    untouched, and mounting one the harness cannot write produces exactly the
    "nothing ever accumulates" symptom this feature exists to end.
    """
    directory = memory_dir()
    try:
        # Through the store so every level takes DIR_MODE; mkdir(mode=) is
        # leaf-only (the T93 pitfall under logs/).
        store.ensure_state_dir(directory)
    except OSError as exc:
        logger.warning(
            "platform_assistant_memory_dir_unusable path=%s error=%s — this "
            "incarnation keeps its memory inside the container, so nothing it "
            "saves will reach the next one",
            directory, exc,
        )
        return None
    # The container user is this uid (keep-id / BUILD_UID), so the host-side
    # answer is the container-side one.
    if not os.access(directory, os.W_OK | os.X_OK):
        try:
            mode = oct(directory.stat().st_mode & 0o7777)
        except OSError:
            mode = "unknown"
        logger.warning(
            "platform_assistant_memory_store_unwritable path=%s mode=%s — the "
            "store exists but this user cannot write it, so the harness would "
            "find a directory it cannot save into; not mounted, and this "
            "incarnation's memory dies with it",
            directory, mode,
        )
        return None
    return directory


def harness_memory_dirs() -> dict:
    """``{harness name: the container path it keeps agent memory in}``.

    Built-ins that declare a *native* memory feature — claude alone today. Read
    from the declarations so a harness that grows one needs no change here; that
    is about the plumbing, not about what a codex or pi session knows (note 8).
    """
    return {
        name: harness.memory_dir
        for name, harness in HARNESSES.items()
        if harness.memory_dir
    }


def memory_links() -> list:
    """``[(declared, staged)]`` for the entrypoint's linker.

    Every declaring harness points at the same staged mount, whichever harness
    the session turns out to run — the policy the transcript mounts follow.
    """
    return [
        (declared, CONTAINER_STAGED_DIR)
        for declared in harness_memory_dirs().values()
    ]


def mount_flags(directory: Path) -> list:
    """``lmer`` flags binding *directory* in as the staged memory store.

    ``rw`` is the feature: a store the harness cannot write never accumulates.
    """
    return ["--mount-dir", f"{directory}:{CONTAINER_STAGED_DIR}:rw"]


def measure(directory: Optional[Path] = None) -> Measurement:
    """Count the files in the store and add up their sizes.

    Accumulation, not liveness: a store that already holds files keeps reporting
    them whether or not anything still reads the container-side path. An absent
    or unreadable store measures as empty.

    Symlinks are never followed — the store is rw-mounted into a container an
    agent drives, so a link out of it is something that side can plant.
    """
    root = memory_dir() if directory is None else directory
    files = 0
    total = 0
    looked_at = 0
    pending: list = [root]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if looked_at >= MEASURE_ENTRY_CAP:
                # Whatever is left here or on `pending` is work never done.
                return Measurement(files=files, bytes=total, truncated=True)
            looked_at += 1
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            files += 1
    return Measurement(files=files, bytes=total, truncated=False)


def observe(directory: Optional[Path] = None) -> Measurement:
    """:func:`measure`, plus a warning and an event when the store is large.

    The event outlives the log line, which is only seen by whoever is watching.
    Nothing is trimmed: what to keep is the assistant's call.
    """
    measurement = measure(directory)
    if measurement.large:
        logger.warning(
            "platform_assistant_memory_large files=%s bytes=%s — the store is "
            "read through its index every incarnation, so this is context spent "
            "before the first turn; nothing was removed",
            measurement.files, measurement.bytes,
        )
        append_event(
            "assistant_memory_large",
            note=f"{measurement.files} files, {measurement.bytes} bytes",
            data=measurement.to_dict(),
        )
    return measurement
