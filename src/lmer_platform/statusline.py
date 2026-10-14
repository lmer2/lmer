"""The harness's own status line, mirrored into the platform (issue #248).

A session publishes what its own status line shows — context left, usage
windows spent, effort — as a JSON document into a directory the platform
bind-mounted for it. This module is the host's end of that: read on the
platform's schedule, off the host side of the mount. Format, rationale and the
bounds enforced here: ``docs/SESSION-STATUS.md``.

Reads are best-effort in the way :mod:`lmer_platform.store` and
:mod:`lmer_platform.registry` are: an absent, unreadable, oversized, torn,
undateable or wrongly-shaped document is *no status*, logged at most once and
never raised. This value rides on the fleet payload — the one request the whole
app polls.
"""

from __future__ import annotations

import json
import logging
import math
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from lmer_cli.util import get_bool_env

from . import registry, store
from .store import TS_FORMAT, age_seconds, clamp_text, logs_dir

logger = logging.getLogger("lmer_platform.statusline")

__all__ = [
    "CONTAINER_STATUS_DIR", "STATUS_DIR_ENV", "STATUS_FILENAME", "ENABLED_ENV", "enabled",
    "SESSION_DIR_SUFFIX", "SCHEMA_VERSION", "DIR_MODE",
    "MAX_DOCUMENT_BYTES", "MAX_LIMITS",
    "MAX_NAME_CHARS", "MAX_STAMP_CHARS",
    "session_status_dir", "status_file_for", "prepare_status_dir",
    "discard_status_dir", "read_document", "for_session", "by_session",
]

#: Where the platform mounts this session's status directory *inside* the
#: container. A dedicated directory rather than one under ``~/.lmer``, for the
#: reason :data:`ask_channel.protocol.CONTAINER_ASK_DIR` gives: that path is
#: itself a mount, and nesting bind mounts makes the result depend on order.
CONTAINER_STATUS_DIR = "/home/developer/.lmer-status"

#: The variable that tells a session where to publish. Set only when the mount
#: was actually prepared, exactly as ``LMER_ASK_DIR`` is: a value without the
#: matching mount points the writer at a container-private directory.
STATUS_DIR_ENV = "LMER_STATUS_DIR"

#: The daemon's fleet-wide off switch (issue #507). Documented in
#: docs/LMER-CLI.md beside ``LMER_STATUS_DIR``.
ENABLED_ENV = "LMER_PLATFORM_STATUS_STRIP"


def enabled() -> bool:
    """Whether sessions get a status directory and the platform serves strips."""
    return get_bool_env(ENABLED_ENV, default=True)


#: The document's name inside that directory. Spelled again in
#: ``hooks/statusline.py`` — that renderer is stdlib-only by constraint, so the
#: two halves cannot share an import and a round-trip test binds them instead.
STATUS_FILENAME = "status.json"

#: Suffix of the per-session directory on the host, beside the PTY log, the
#: transcript directory and the ask channel in ``logs/``. Derived from the
#: session id, so resolving it needs no recorded state.
SESSION_DIR_SUFFIX = ".status"

#: Owner-only, like the rest of the platform's state tree.
DIR_MODE = store.STATE_DIR_MODE

#: The document format this build speaks (``docs/SESSION-STATUS.md``).
SCHEMA_VERSION = 1

#: Read cap. The document is ~200 bytes; anything approaching this is not one.
MAX_DOCUMENT_BYTES = 16 * 1024

#: How many usage windows are served. claude publishes two; the cap keeps an
#: odd document from lengthening every row of the fleet payload.
MAX_LIMITS = 8

#: Character caps for the strings a harness chooses the vocabulary of.
MAX_NAME_CHARS = 32

#: Cap on ``updated`` before it reaches a date parser: an unbounded string from
#: a container must not reach one.
MAX_STAMP_CHARS = 64


def session_status_dir(session_id: str) -> Path:
    """Host directory holding one session's status document."""
    return logs_dir() / f"{_validated(session_id)}{SESSION_DIR_SUFFIX}"


def status_file_for(session_id: str) -> Path:
    """Host path of one session's status document."""
    return session_status_dir(session_id) / STATUS_FILENAME


def _validated(session_id: str) -> str:
    """Reject an id that could not name a session before it reaches a path.

    Borrowed from the registry rather than written again, as
    :mod:`lmer_platform.ask` and :mod:`lmer_platform.transcripts` do: two
    notions of a legal session id is how ``..`` eventually gets past one.
    """
    registry.session_path(session_id)
    return session_id


def prepare_status_dir(session_id: str) -> Optional[Path]:
    """Create the session's status directory 0700. ``None`` when that failed.

    Created with the mode and then chmod'ed, for the reason
    :func:`lmer_platform.ask.prepare_ask_dir` gives. Fail-soft: a session
    without the directory publishes nothing and shows no strip, which is every
    session that ran before this feature existed. ``None`` too when the
    feature is switched off (:func:`enabled`).
    """
    if not enabled():
        return None
    try:
        directory = session_status_dir(session_id)
    except registry.RegistryError as exc:
        logger.warning("platform_status_dir_rejected id=%s error=%s", session_id, exc)
        return None
    try:
        store.ensure_state_dir(directory.parent)
        directory.mkdir(mode=DIR_MODE, exist_ok=True)
        directory.chmod(DIR_MODE)
    except OSError as exc:
        logger.warning(
            "platform_status_dir_unusable id=%s path=%s error=%s — the session "
            "runs without a status strip", session_id, directory, exc,
        )
        return None
    return directory


def discard_status_dir(session_id: str) -> None:
    """Remove a finished session's status directory. Never raises.

    The document describes a session that is rendering, so it has no readers
    once the session is gone — the reasoning that retires the ports file on a
    clean exit, and unlike the two logs, which *are* the scrollback.
    """
    try:
        directory = session_status_dir(session_id)
    except registry.RegistryError:
        return
    try:
        (directory / STATUS_FILENAME).unlink()
    except OSError:
        pass
    try:
        directory.rmdir()
    except OSError:
        pass


def _clamped_name(value: object) -> Optional[str]:
    """A non-empty string a harness chose, cut to :data:`MAX_NAME_CHARS`."""
    if not isinstance(value, str) or not value.strip():
        return None
    return clamp_text(value.strip(), MAX_NAME_CHARS)


def _percent(value: object) -> Optional[int]:
    """*value* as a percentage clamped to 0–100, or ``None``.

    Clamped rather than rejected because a spend limit can legitimately report
    past 100 and a bar cannot draw it. ``None`` for anything that is not a
    finite number — "not reported", never zero.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        rounded = round(value)
    except (ValueError, OverflowError):  # NaN and infinity
        return None
    return max(0, min(100, int(rounded)))


def _resets_at(value: object) -> Optional[str]:
    """A window's reset time as ISO-8601 Z, from the epoch seconds it arrives as.

    ``None`` for anything unusable, including a non-positive epoch: rendering
    1970 would read as a window that reset long ago, the opposite of what an
    absent field means.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    try:
        moment = datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None
    return moment.strftime(TS_FORMAT)


def _updated(value: object) -> Optional[str]:
    """A document's ``updated`` as :data:`lmer_platform.store.TS_FORMAT`, or None.

    Both spellings ``docs/SESSION-STATUS.md`` accepts — the format's own and
    general ISO-8601, which is how a drop-in's
    ``datetime.now(timezone.utc).isoformat()`` will spell it — normalised to the
    strict form so the wire sees one shape. A naive stamp is read as UTC rather
    than discarded over a missing suffix.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if len(text) > MAX_STAMP_CHARS:
        return None
    try:
        moment = datetime.strptime(text, TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(TS_FORMAT)


def _limits(value: object) -> list:
    """The usage windows of a document, validated and bounded.

    An entry keeps its ``key`` and its own ``label`` — the harness's vocabulary,
    not this module's. Deduplicated on ``key``, first occurrence winning: that
    key is what a client draws the list by.
    """
    if not isinstance(value, list):
        return []
    windows = []
    seen = set()
    for entry in value[:MAX_LIMITS]:
        if not isinstance(entry, dict):
            continue
        key = _clamped_name(entry.get("key"))
        percent = _percent(entry.get("percent"))
        if key is None or percent is None or key in seen:
            continue
        seen.add(key)
        window = {
            "key": key,
            "label": _clamped_name(entry.get("label")) or key,
            "percent": percent,
        }
        resets_at = _resets_at(entry.get("resets_at"))
        if resets_at is not None:
            window["resets_at"] = resets_at
        windows.append(window)
    return windows


def _read_bytes(path: Path) -> Optional[bytes]:
    """The document's bytes, or None when there is nothing safe to read.

    Everything is asked of the open descriptor rather than the path: what the
    size cap measured and what the read consumed have to be one inode, the
    directory is container-writable (``O_NOFOLLOW``), and a FIFO at this name
    would report ``st_size == 0`` and then stall the fleet payload's own read
    forever (``O_NONBLOCK`` plus ``S_ISREG``). Borrowed wholesale from
    :func:`lmer_platform.uploads.read_upload`, same threat model.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        # ELOOP, EACCES, ENOTDIR all say the same thing here: nothing to serve.
        logger.warning(
            "platform_status_document_unopenable path=%s error=%s", path, exc
        )
        return None
    try:
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                logger.warning(
                    "platform_status_document_not_a_file path=%s mode=%o",
                    path, info.st_mode,
                )
                return None
            if info.st_size > MAX_DOCUMENT_BYTES:
                logger.warning("platform_status_document_oversized path=%s", path)
                return None
            # One byte past the cap, so a file that grew between the fstat and
            # the read is refused on the same rule rather than on neither.
            raw = handle.read(MAX_DOCUMENT_BYTES + 1)
    except OSError as exc:
        logger.warning(
            "platform_status_document_unreadable path=%s error=%s", path, exc
        )
        return None
    if len(raw) > MAX_DOCUMENT_BYTES:
        logger.warning("platform_status_document_oversized path=%s", path)
        return None
    return raw


def read_document(path: Path, *, now=None) -> Optional[dict]:
    """One status document, validated for serving. ``None`` when there is none.

    ``None`` covers every way this can fail — absent, not a regular file, too
    big, torn mid-write, not JSON, undateable, a schema from the future — since
    a client asks "is there a status for this session" by testing the object.

    Shaped as closely to the document as validation allows, so the next harness
    to publish one needs no reader change: blocks keep their names and nesting,
    and the only additions are ``age_seconds`` and each window's ISO-8601 reset.
    """
    raw = _read_bytes(path)
    if raw is None:
        return None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        # A torn read of a file being replaced looks exactly like this, and the
        # next poll gets the whole thing — so it is not worth a warning.
        return None
    if not isinstance(document, dict):
        return None
    schema = document.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema > SCHEMA_VERSION:
        logger.warning(
            "platform_status_document_unsupported path=%s schema=%r", path, schema
        )
        return None

    updated = _updated(document.get("updated"))
    if updated is None:
        # Refused rather than served undated: an undated reading is
        # indistinguishable on screen from one taken three seconds ago.
        logger.warning(
            "platform_status_document_undated path=%s updated=%.64r",
            path, document.get("updated"),
        )
        return None
    served: dict = {
        "harness": _clamped_name(document.get("harness")),
        "updated": updated,
        "age_seconds": age_seconds(updated, now=now),
    }
    if served['harness'] == 'codex':
        usage = _codex(document.get('codex'), now=now)
        if usage is not None:
            served['codex'] = usage
            context = document.get('context')
            observed = _dated(context, 'rollout/token_count', now=now)
            if observed and context.get('thread_id') == usage['thread_id']:
                percent = _percent(context.get('percent'))
                if percent is not None:
                    served['context'] = {**observed, 'percent': percent,
                                         'scope': 'thread', 'thread_id': usage['thread_id']}
        return served
    context = document.get("context")
    if isinstance(context, dict):
        percent = _percent(context.get("percent"))
        if percent is not None:
            served["context"] = {"percent": percent}
    limits = _limits(document.get("limits"))
    if limits:
        served["limits"] = limits
    effort = document.get("effort")
    if isinstance(effort, dict):
        level = _clamped_name(effort.get("level"))
        if level is not None:
            served["effort"] = {"level": level}
    return served


def _count(value):
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2 ** 53 - 1:
        return value
    return None


def _dated(value, source, *, now):
    if not isinstance(value, dict) or value.get('source') != source:
        return None
    updated = _updated(value.get('updated'))
    if updated is None:
        return None
    return {'updated': updated, 'age_seconds': age_seconds(updated, now=now), 'source': source}


def _codex(value, *, now):
    if not isinstance(value, dict):
        return None
    ident = value.get('thread_id')
    if not isinstance(ident, str) or not ident or len(ident) > 128:
        return None
    result = {'thread_id': ident, 'version': _clamped_name(value.get('version'))}
    tokens = value.get('tokens')
    observed = _dated(tokens, 'rollout/token_count', now=now)
    if observed and tokens.get('thread_id') == ident:
        observed.update(scope='thread', thread_id=ident)
        fields = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                  'output_tokens', 'reasoning_output_tokens', 'total_tokens')
        for key in ('last', 'total'):
            counters = tokens.get(key)
            if isinstance(counters, dict):
                observed[key] = {name: counters[name] for name in fields
                                 if _count(counters.get(name)) is not None}
        capacity = _count(tokens.get('context_window'))
        if capacity:
            observed['context_window'] = capacity
        if observed.get('last') or observed.get('total') or capacity:
            result['tokens'] = observed
    account = value.get('account')
    observed = _dated(account, 'account/rateLimits/read', now=now)
    if observed:
        observed.update(scope='account', limits=[])
        if 'main_bucket' in account:
            observed['main_bucket'] = _clamped_name(account.get('main_bucket'))
        windows = account.get('limits')
        seen = set()
        for window in (windows[:MAX_LIMITS] if isinstance(windows, list) else []):
            if not isinstance(window, dict):
                continue
            key = _clamped_name(window.get('key'))
            percent = window.get('percent')
            if (not key or key in seen or isinstance(percent, bool)
                    or not isinstance(percent, (int, float)) or not 0 <= percent <= 2 ** 31 - 1
                    or not math.isfinite(percent)):
                continue
            seen.add(key)
            reset = _resets_at(window.get('resets_at'))
            observed['limits'].append({
                'key': key, 'bucket': _clamped_name(window.get('bucket')),
                'label': _clamped_name(window.get('label')) or key,
                'percent': percent, 'minutes': _count(window.get('minutes')),
                'resets_at': reset,
                'reset_passed': reset is not None and age_seconds(reset, now=now) >= 0,
            })
            period = _clamped_name(window.get('period_label'))
            if period:
                observed['limits'][-1]['period_label'] = period
        result['account'] = observed
    runtime = value.get('runtime')
    observed = _dated(runtime, 'thread/read', now=now)
    if observed:
        observed.update(model=_clamped_name(runtime.get('model')),
                        effort=_clamped_name(runtime.get('effort')))
        result['runtime'] = observed
    return result


def for_session(session_id: str, *, now=None) -> Optional[dict]:
    """The status one session last published, or ``None``."""
    if not enabled():
        return None
    try:
        path = status_file_for(session_id)
    except registry.RegistryError:
        return None
    return read_document(path, now=now)


def by_session(sessions: Iterable[dict], *, now=None) -> dict:
    """Map **live** session ids to their published status, for the fleet view.

    Live only: a document outliving its session describes nobody. Passed the
    session list rather than reading the registry itself, so the fleet path pays
    for one registry read — :func:`lmer_platform.ask.pending_by_session` is the
    same bargain.
    """
    statuses: dict = {}
    if not enabled():
        return statuses
    for entry in sessions:
        if not isinstance(entry, dict) or not entry.get("live"):
            continue
        session_id = entry.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        status = for_session(session_id, now=now)
        if status is not None:
            statuses[session_id] = status
    return statuses
