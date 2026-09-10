"""Thread registry observation and legacy annotations. Delivery requires a connector lease."""

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


def _registry_dir(kind: str = "slack") -> Path:
    """Return the directory holding registry entries for one channel *kind*.

    ``slack`` is the historical registry; ``matrix`` threads (issue #407) keep
    their own directory beside it, same shape, so a room id and a Slack channel
    id can never collide in one namespace.
    """
    if REGISTRY_DIR is not None:
        base = Path(REGISTRY_DIR)
        return base if kind == "slack" else base / kind
    return lmer_state_dir() / f"{kind}-sessions"


def _entry_path(channel: str, thread_ts: str, kind: str = "slack") -> Path:
    """Return the path of the registry file for one ``(channel, thread_ts)``.

    ``channel`` and ``thread_ts`` come from
    :func:`slack_chat.permalink.parse_slack_permalink` (e.g. ``"C0123ABC"`` and
    ``"1700000000.123456"``) — or, for ``kind="matrix"``, a room id and a
    thread-root event id — none of which can contain a path separator; the
    separators are stripped defensively so a crafted value can never escape the
    registry directory.
    """
    safe = f"{channel}-{thread_ts}".replace("/", "_").replace(os.sep, "_")
    return _registry_dir(kind) / f"{safe}.json"


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
    kind: str = "slack",
    owner: str | None = None,
) -> None:
    """Write a legacy annotation under the metadata lock, without changing a live lease."""
    if not channel or not thread_ts:
        return
    from lmer_cli.connector_ownership import ThreadKey

    key = ThreadKey(kind, channel, thread_ts)
    try:
        with key.metadata_lock():
            if key.busy():
                return
            if pid is None:
                pid = os.getpid()
            entry = {"pid": pid, "channel": channel, "thread_ts": thread_ts}
            if permalink:
                entry["permalink"] = permalink
            if owner:
                entry["owner"] = owner
            try:
                directory = _registry_dir(kind)
                directory.mkdir(parents=True, exist_ok=True)
                path = _entry_path(channel, thread_ts, kind)
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
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("thread_registry_write_refused error=%s", exc)


def deregister(
    channel: str | None,
    thread_ts: str | None,
    *,
    kind: str = "slack",
    pid: int | None = None,
) -> None:
    """Remove only the selected legacy PID annotation, under the metadata lock."""
    if not channel or not thread_ts:
        return
    from lmer_cli.connector_ownership import ThreadKey

    key = ThreadKey(kind, channel, thread_ts)
    try:
        with key.metadata_lock():
            if key.busy():
                return
            path = _entry_path(channel, thread_ts, kind)
            try:
                try:
                    owner_pid = json.loads(path.read_text()).get("pid")
                except (ValueError, AttributeError):
                    owner_pid = None  # corrupt entry: safe to remove
                if owner_pid is not None and owner_pid != (os.getpid() if pid is None else pid):
                    logger.debug(
                        "slack_session_deregister_skipped channel=%s thread_ts=%s "
                        "owner_pid=%s (not ours)",
                        channel,
                        thread_ts,
                        owner_pid,
                    )
                    return
                path.unlink()
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
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("thread_registry_write_refused error=%s", exc)


def thread_holder(
    channel: str | None, thread_ts: str | None, *, kind: str = "slack"
) -> dict | None:
    """Observe native lease ownership first, then legacy PID annotations."""
    if not channel or not thread_ts:
        return None
    from lmer_cli.connector_ownership import ThreadKey

    try:
        current = ThreadKey(kind, channel, thread_ts).holder()
    except (OSError, ValueError, RuntimeError):
        return {"owner": "unknown", "pid": None, "channel": channel, "thread_ts": thread_ts}
    if current is not None:
        return current
    path = _entry_path(channel, thread_ts, kind)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(
            "slack_session_read_failed channel=%s thread_ts=%s error=%s",
            channel,
            thread_ts,
            exc,
        )
        return None
    try:
        entry = json.loads(raw)
        pid = int(entry.get("pid", 0))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        # Corrupt/unexpected content — treat as not connected. Left in place
        # (not unlinked); the thread's next register() overwrites it.
        return None
    return entry if _pid_alive(pid) else None


def is_thread_connected(channel: str | None, thread_ts: str | None, *, kind: str = "slack") -> bool:
    """Whether a native lease or live legacy annotation holds this thread."""
    return thread_holder(channel, thread_ts, kind=kind) is not None
