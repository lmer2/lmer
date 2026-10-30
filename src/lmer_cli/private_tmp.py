"""
Owner-only state under a shared, world-writable ``/tmp`` (issue #262).

Several of lmer's files live at names every local user can predict — the
gate-in-flight markers, the gate caches, the gate-check log — and a name in
``/tmp`` that anyone can predict is a name anyone can claim first: pre-create
the directory and own what is written into it, or plant a symlink and redirect
the write. These helpers are the one place that knows how to refuse both, so
every such file gets the same answer.

The directory is the real defence (:func:`owned_dir`): once it is a real
directory owned by this uid with no group/other bits, nobody but this uid and
root can add a name to it. The no-follow opens are the cheap second one, for
the window in which a directory that used to be somebody else's still holds
their names.

Every helper fails by returning None or raising ``OSError``; none of them
decides what a refusal means — the gates treat one as a cache miss or "no gate
in flight", never as an error.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional

#: Mode a private directory is created with, and tightened to.
PRIVATE_DIR_MODE = 0o700

#: Mode a private file is created with, and tightened to.
PRIVATE_FILE_MODE = 0o600


def owned_dir(
    directory: Path, create: bool = False, mode: int = PRIVATE_DIR_MODE
) -> Optional[Path]:
    """*directory* when this uid can trust it, else None.

    Refused: a symlink (never followed to wherever it points), anything that
    is not a directory, and a directory whose owner is not ``geteuid()``. A
    directory already there with looser bits than *mode* is tightened rather
    than adopted as found: ``mkdir`` does nothing to a directory that exists,
    so without that the hardening would apply only where it is not needed
    (:func:`lmer_platform.store.ensure_state_dir` makes the same correction
    for the same reason). Only bits outside *mode* trigger the ``chmod``, so
    nothing is ever widened.

    With *create*, a missing directory is made with *mode*. Ownership is still
    checked after the ``mkdir``: losing the creation race to another user
    leaves their directory in place, and ``exist_ok`` would not say so.
    """
    try:
        if directory.is_symlink():
            return None
        if not directory.exists():
            if not create:
                return None
            directory.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.mkdir(directory, mode)
            except FileExistsError:
                pass
            else:
                # Ours, so exactly *mode*: a umask can only have narrowed it,
                # and a narrower one could leave the owner unable to write.
                os.chmod(directory, mode)
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            return None
        if stat.S_IMODE(info.st_mode) & ~mode:
            os.chmod(directory, mode)
        return directory
    except OSError:
        return None


def open_no_follow(path: str, flags: int) -> int:
    """``open`` opener that refuses a symlink at the final component."""
    return os.open(path, flags | os.O_NOFOLLOW)


def open_private(path: Path, flags: int, mode: int = PRIVATE_FILE_MODE) -> int:
    """``os.open`` a file this uid owns outright; a descriptor or ``OSError``.

    The final component is never followed (``O_NOFOLLOW``) and a FIFO cannot
    hang the open (``O_NONBLOCK``). What was opened must be a regular file
    owned by ``geteuid()`` — a name created by someone else is refused before
    anything is written to it, which is why ``O_TRUNC`` is applied only after
    that check rather than by the open itself. A file found with looser bits
    than *mode* is tightened.
    """
    truncate = bool(flags & os.O_TRUNC)
    descriptor = os.open(
        str(path),
        (flags & ~os.O_TRUNC) | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        mode,
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{path} is not a regular file")
        if info.st_uid != os.geteuid():
            raise PermissionError(f"{path} is owned by uid {info.st_uid}")
        if stat.S_IMODE(info.st_mode) & ~mode:
            os.fchmod(descriptor, mode)
        if truncate:
            os.ftruncate(descriptor, 0)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def write_private(path: Path, text: str, append: bool = False,
                  errors: str = "strict") -> None:
    """Write (or append) *text* as UTF-8 through :func:`open_private`."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    with os.fdopen(open_private(path, flags), "w", encoding="utf-8",
                   errors=errors) as handle:
        handle.write(text)


def read_owned(path: Path) -> tuple[str, os.stat_result]:
    """Read a regular file this uid owns, as UTF-8, with its ``fstat``.

    The same refusals as :func:`open_private`, for the same reason: a
    directory made by an older build under a loose umask can still hold names
    another user planted before it was tightened, and their contents are not
    this uid's to believe. Raises ``OSError`` (``PermissionError`` for a
    foreign owner), which every caller treats as an absent file.
    """
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                         | os.O_CLOEXEC)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{path} is not a regular file")
        if info.st_uid != os.geteuid():
            raise PermissionError(f"{path} is owned by uid {info.st_uid}")
        return handle.read(), info


def read_no_follow(path: Path) -> str:
    """The text of :func:`read_owned`."""
    return read_owned(path)[0]
