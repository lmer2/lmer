"""Host-side registry of live lmer sessions attached to Slack threads.

Why this exists
---------------
The Slack listener (:mod:`slack_chat.listener`) dedups the sessions it spawns
via its in-memory :class:`~slack_chat.sessions.SessionManager`, but it has no
knowledge of an ``lmer chat`` session started by other means — e.g. a developer
running ``lmer chat <thread-permalink>`` directly from a shell. Without that
knowledge a later @-mention in the same thread sails past the in-memory dedup
and the listener spawns a *second* lmer into a thread that already has one, so
two agents talk over each other (issue #74).

Both listener-spawned and manually-invoked sessions are host-side ``lmer chat``
processes that go through :func:`lmer_cli.cli.main`. Each such process records
its attachment here when it launches and clears it on exit (see the
``on_session_start`` / ``on_session_end`` hooks on
:class:`lmer_cli.targets.SlackThreadTargets`); the listener consults the
registry before spawning. Because every session and the listener run on the
same host — the listener launches a container per session as a sibling process,
and a manual invocation needs the same ``lmer`` CLI and container runtime — a
shared on-disk registry under the lmer state dir is a reliable cross-process
signal.

Liveness
--------
Each entry records the PID of the host-side ``lmer`` process, which lives for
the whole session (it blocks in ``subprocess.call`` running the container). An
entry whose PID is no longer alive is **stale** — left behind by an unclean
death (SIGKILL from the reaper's escalation ladder, a crash, a host reboot)
that skipped the normal deregister — and :func:`is_thread_connected` reports it
as not-connected, so a crashed session never permanently blocks a thread.

Reads never mutate the registry. A stale (or corrupt) entry is *not* deleted on
read; it is reclaimed when the next session for the thread calls
:func:`register`, which atomically overwrites the file. Keeping ``register``
(overwrite) and ``deregister`` (delete) the only writers avoids a race in which
an opportunistic read-path unlink — which removes the file *by path*, not the
specific content it just read — could delete a **live** entry a concurrent
``register`` had written in place of the stale one. That would fail unsafe by
letting the listener spawn the duplicate this module exists to prevent (unlike
the PID-reuse edge above, which fails safe). The only cost is that an entry for
a thread that is never reconnected lingers as a tiny, harmless file.

PID reuse is the one accepted edge: if the recorded PID is dead but the OS has
since recycled it to an unrelated process, :func:`is_thread_connected` reports a
false "connected" and the listener declines to spawn. That is the *safe* failure
(no duplicate agent), it is transient (a real new session for the thread
overwrites the entry), and it is rare in practice, so the registry deliberately
trades it for staying dependency-free and simple.

All writes and reads are best-effort: a registry error must never break the
session it only annotates, nor wrongly block the listener — failures fall back
to the in-memory dedup that existed before this module.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from lmer_cli.runtime import lmer_state_dir

logger = logging.getLogger("lmer_slack.registry")

#: Overridable directory for registry entries (mirrors the patchable
#: ``slack_chat.cli.CURSOR_DIR`` convention). When ``None`` the directory is
#: derived from the lmer state dir at call time, so tests can point it at a
#: temp dir via ``monkeypatch.setattr(registry, "REGISTRY_DIR", str(tmp))``.
REGISTRY_DIR: str | None = None


def _registry_dir() -> Path:
    """Return the directory holding registry entries."""
    if REGISTRY_DIR is not None:
        return Path(REGISTRY_DIR)
    return lmer_state_dir() / "slack-sessions"


def _entry_path(channel: str, thread_ts: str) -> Path:
    """Return the path of the registry file for one ``(channel, thread_ts)``.

    ``channel`` and ``thread_ts`` come from
    :func:`slack_chat.permalink.parse_slack_permalink` (e.g. ``"C0123ABC"`` and
    ``"1700000000.123456"``), which can't contain a path separator — but the
    separators are stripped defensively so a crafted value can never escape the
    registry directory.
    """
    safe = f"{channel}-{thread_ts}".replace("/", "_").replace(os.sep, "_")
    return _registry_dir() / f"{safe}.json"


def _pid_alive(pid: int) -> bool:
    """Whether *pid* names a process that currently exists.

    Uses the POSIX ``kill(pid, 0)`` existence probe. A ``PermissionError`` means
    the process exists but is owned by another user; that is treated as alive so
    the listener fails safe (it won't spawn a duplicate over a session it merely
    can't signal).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def register(
    channel: str | None,
    thread_ts: str | None,
    *,
    permalink: str | None = None,
    pid: int | None = None,
) -> None:
    """Record that an lmer session is attached to ``(channel, thread_ts)``.

    *pid* defaults to the calling process — the host-side ``lmer`` process,
    whose lifetime is the session's. The write is atomic (temp file + rename)
    so a concurrent reader never sees a half-written entry, and best-effort: any
    failure is logged and swallowed so a registry problem can't break the
    session it only annotates.
    """
    if not channel or not thread_ts:
        return
    if pid is None:
        pid = os.getpid()
    entry = {"pid": pid, "channel": channel, "thread_ts": thread_ts}
    if permalink:
        entry["permalink"] = permalink
    try:
        directory = _registry_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = _entry_path(channel, thread_ts)
        # Per-PID temp name so two processes racing on the same thread (which
        # should not happen, but might pathologically) can't clobber each
        # other's temp file mid-write; rename is atomic on the same filesystem.
        tmp = path.parent / f"{path.name}.{pid}.tmp"
        tmp.write_text(json.dumps(entry))
        tmp.replace(path)
        logger.debug(
            "slack_session_registered channel=%s thread_ts=%s pid=%s",
            channel,
            thread_ts,
            pid,
        )
    except OSError as exc:
        logger.warning(
            "slack_session_register_failed channel=%s thread_ts=%s error=%s",
            channel,
            thread_ts,
            exc,
        )


def deregister(channel: str | None, thread_ts: str | None) -> None:
    """Remove the registry entry for ``(channel, thread_ts)`` if present.

    Best-effort and idempotent: a missing entry (already gone, or never written)
    is not an error.
    """
    if not channel or not thread_ts:
        return
    try:
        _entry_path(channel, thread_ts).unlink()
        logger.debug(
            "slack_session_deregistered channel=%s thread_ts=%s",
            channel,
            thread_ts,
        )
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning(
            "slack_session_deregister_failed channel=%s thread_ts=%s error=%s",
            channel,
            thread_ts,
            exc,
        )


def is_thread_connected(channel: str | None, thread_ts: str | None) -> bool:
    """Whether a *live* lmer session is registered for ``(channel, thread_ts)``.

    Returns ``True`` only when an entry exists and its recorded PID is still
    alive. A stale entry (dead PID, left by an unclean death that skipped
    deregister) or a corrupt one reads as not connected, so a crashed session
    never permanently blocks a thread. Any read error fails open (returns
    ``False``) — the listener then falls back to its in-memory dedup rather than
    wrongly refusing to connect.

    Reads never mutate the registry: a stale/corrupt entry is left in place and
    reclaimed by the next :func:`register` for the thread (an atomic overwrite),
    rather than unlinked here. Unlinking on read removes the file *by path*,
    which could race with a concurrent ``register`` and delete the live entry it
    just wrote — failing unsafe by letting a duplicate session spawn. See the
    module docstring's Liveness section.
    """
    if not channel or not thread_ts:
        return False
    path = _entry_path(channel, thread_ts)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning(
            "slack_session_read_failed channel=%s thread_ts=%s error=%s",
            channel,
            thread_ts,
            exc,
        )
        return False
    try:
        pid = int(json.loads(raw).get("pid", 0))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        # Corrupt/unexpected content — treat as not connected. Left in place
        # (not unlinked); the thread's next register() overwrites it.
        return False
    # A live PID means a session is attached; a dead one is a stale entry that
    # the thread's next register() will overwrite. Either way, never mutate on
    # read (see the docstring for the race that read-path unlinking would open).
    return _pid_alive(pid)
