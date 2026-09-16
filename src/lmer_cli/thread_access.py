"""Reading and posting accessible chat threads on demand (#414, #432).

A read or post leaves existing connections and their cursors unchanged,
including when the run addresses its own connected thread. It can also *make* one —
:func:`open_thread` posts a root message in a named channel, which the platform
then connects to the run (issue #429). This module is that work, on the host,
where the tokens are.

Two callers, one implementation:

- :class:`ThreadServicer` answers a session's requests
  (:mod:`lmer_cli.thread_requests`), hosted by the platform daemon for a session
  it spawned and by the host ``lmer`` process otherwise;
- the daemon's own ``/api/comms/thread`` routes call :func:`read_thread` and
  :func:`post_thread` directly for the UI and ``lmer-ctl``.

The attribution a thread sees is composed here, from what the *host* knows —
the run's fleet-view title, or the credential a platform caller presented —
never from text the session supplied, for the reason the bus stamps
``source.channel`` rather than reading it out of the body.
"""
from __future__ import annotations

import logging
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import requests

from ask_channel import bus
from matrix_chat.permalink import matrix_to_link
from slack_chat.permalink import slack_thread_link

from . import dm_access
from .thread_posts import Poster
from .threads import (
    CHANNEL_MATRIX, CHANNEL_SLACK, ChannelRef, ThreadError, ThreadRef,
    parse_channel, parse_thread,
)
from . import thread_posts
from . import thread_requests as _requests

logger = logging.getLogger(__name__)

__all__ = [
    "UploadStore", "ThreadServicer", "resolve_thread", "read_thread",
    "post_thread", "open_thread", "send_dm", "render_record", "stage_upload",
    "read_from_store", "stored_name_of",
    "slack_token", "matrix_account", "DAEMON_ENVIRONMENT", "CLI_ENVIRONMENT",
]

#: How a credential refusal names the environment the operator has to fix. The
#: two hosts are started by different people in different places, and "set it
#: where the daemon is started" is wrong advice for a shell session.
DAEMON_ENVIRONMENT = "the daemon's environment"
CLI_ENVIRONMENT = "this lmer process's environment"

#: What one post carries. Slack's own limit is 40 000 characters; a thread reply
#: is read on a phone, and the connectors already hold themselves to this. A post
#: over it is sent clipped and **says so** — see :func:`_clip`.
MAX_POST_CHARS = 3500

#: Bytes of one thread file copied into the upload store. A read of a thread is
#: a convenience, not a mirror of somebody's file share.
MAX_FILE_BYTES = 25 * 1024 * 1024

#: The supervisor caps a long-poll at 30 s; the connectors use the same figure.
POLL_SECONDS = 25.0

#: The scope a username lookup needs. Named in the refusal so an operator can
#: see what would be required, without being told to grant it.
SLACK_USERS_READ = "users:read"

BACKOFF_MAX = 30.0

#: Slack error codes that mean the bot cannot see the conversation, and what to
#: do about each. Matched against :attr:`slack_chat.client.SlackError.code`;
#: anything else is passed through as Slack said it.
#:
#: Two vocabularies, deliberately in one table: ``not_in_channel`` and
#: ``is_archived`` are what a *post* answers, while ``no_permission`` and
#: ``access_denied`` are what ``conversations.replies`` documents for the same
#: fact — "make sure your app is a member of the conversation", in Slack's own
#: words. Both sets are here because either call can be the one that fails and
#: the advice is the same (issue #429 review, round 3, where Slack's error
#: tables for the two methods were read side by side).
_SLACK_HINTS = {
    "channel_not_found": "the host's bot token cannot see that channel — invite the bot to it",
    "not_in_channel": "the host's bot is not in that channel — invite it",
    "no_permission": "the host's bot is not a member of that conversation — invite it",
    "access_denied": "the host's bot may not see that conversation — invite it to the channel",
    "missing_scope": "the host's bot token lacks a scope this needs",
    "thread_not_found": "that thread no longer exists",
    "is_archived": "that channel is archived",
    # Direct messages (#441): both mean "this person cannot be reached this
    # way", which is an operator's problem rather than a transient one.
    "cannot_dm_bot": "that user is another app, and Slack does not deliver direct messages to apps",
    "user_not_found": "no user in this workspace has that id",
    "user_disabled": "that account is deactivated",
    "im_disabled": "direct messages are disabled in this workspace",
}

#: Of those, the answers that make a **connect** refuse (issue #429 round 3):
#: the ones that say the conversation is none of this token's business, which is
#: what must not become a connector mirroring a run into somebody's channel.
#: Every other code — including ``missing_scope``, a token that may be a member
#: of a thread it is not scoped to *read* — leaves the connection to be made and
#: to fail visibly in its own log, because refusing after ``open`` has posted a
#: root message loses the record an operator would use to disconnect it.
#:
#: **Listed rather than derived from :data:`_SLACK_HINTS`.** Deriving it read as
#: the safe direction and is not: this set is consulted by ``open_for``'s connect
#: too, where over-refusing costs an *orphaned root message* rather than a
#: retry, and ``conversations.replies`` documents transient codes
#: (``service_unavailable``, ``internal_error``, ``fatal_error``) that a
#: maintainer might give hint text to for entirely good reasons — under a
#: derivation each of those becomes a refusal, and an orphaned root, by
#: accident. So membership is stated here and nowhere else, and a code added to
#: the hint table above changes messages only (!299 review, round 4).
_SLACK_UNSEEABLE = frozenset({
    "not_in_channel",
    "no_permission",
    "access_denied",
    "channel_not_found",
    "thread_not_found",
    "is_archived",
})

#: And of one whole read. Both bounds exist because the files are posted by
#: whoever is in the thread rather than by anyone here, and on the platform route
#: the process fetching them serves the whole fleet: `--limit 200` over a
#: file-heavy thread would otherwise stage 200 times the per-file bound.
MAX_READ_BYTES = 100 * 1024 * 1024

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class UploadStore:
    """The session's upload store: where files land, and what the session calls it."""

    host_dir: str
    container_dir: str

    @classmethod
    def of(cls, host_dir: Optional[str], container_dir: Optional[str]) -> Optional["UploadStore"]:
        if not host_dir:
            return None
        return cls(host_dir=str(host_dir), container_dir=str(container_dir or host_dir))


# ---------------------------------------------------------------------------
# Credentials — on the host, never in the container
# ---------------------------------------------------------------------------


def slack_token(env: Optional[dict] = None, *, where: str = DAEMON_ENVIRONMENT) -> str:
    token = ((env if env is not None else os.environ).get("SLACK_BOT_TOKEN") or "").strip()
    if not token:
        raise ThreadError(
            f"SLACK_BOT_TOKEN is not set in {where}, and reading or posting in a "
            "Slack thread needs it — set it where that process is started (it "
            "never enters a container)",
            status=400,
        )
    return token


def matrix_account(env: Optional[dict] = None, *, where: str = DAEMON_ENVIRONMENT):
    from matrix_chat.client import MatrixAccount
    from matrix_chat.connector import ACCESS_TOKEN_ENV, HOMESERVER_ENV, USER_ID_ENV

    environ = env if env is not None else os.environ
    values = {
        name: (environ.get(name) or "").strip()
        for name in (HOMESERVER_ENV, USER_ID_ENV, ACCESS_TOKEN_ENV)
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ThreadError(
            f"{', '.join(missing)} not set in {where} — the matrix bot account to "
            "act as. Set them where that process is started (they never enter a "
            "container)",
            status=400,
        )
    return MatrixAccount(
        homeserver=values[HOMESERVER_ENV],
        user_id=values[USER_ID_ENV],
        access_token=values[ACCESS_TOKEN_ENV],
    )


# The two transports are imported inside their factories on purpose: this module
# is imported by the supervisor's own CLI path, where neither client is reachable
# and neither is needed until a request names a thread on that transport.


def _slack_client(token: str):
    from slack_chat.client import SlackClient

    return SlackClient(bot_token=token)


def _matrix_client(account):
    from matrix_chat.client import MatrixClient

    return MatrixClient(account)


# ---------------------------------------------------------------------------
# Resolution: the thread the transport says this link names
# ---------------------------------------------------------------------------


def slack_rooted(ref: ThreadRef, client, *, refuse_refusals: bool = False,
                 require_resolution: bool = False) -> ThreadRef:
    """*ref* keyed by the thread root Slack reports for it."""
    return slack_thread(
        ref, client, refuse_refusals=refuse_refusals, require_resolution=require_resolution,
    )[0]


def slack_thread(ref: ThreadRef, client, *, refuse_refusals: bool = False,
                 require_resolution: bool = False) -> tuple:
    """``(ref keyed by its root, the thread's messages)``.

    A link to a message *inside* a thread parses as its own root, and
    ``conversations.replies`` returns only that message when it has no replies of
    its own — so a read of it would show one line of a conversation. Every
    threaded message carries its parent's ``thread_ts``, which is Slack naming
    the root the link did not.

    The messages come back with the ref because the resolution has already
    fetched them: asking again for the same conversation — the ordinary case,
    where the link already named the root — spent two calls and two rate-limit
    tokens on one answer, and a 429 is paid for in ``time.sleep`` inside the
    servicer. ``None`` for the messages means the root moved, so what was fetched
    is the wrong conversation and the caller must ask again.

    *refuse_refusals* splits what the failure path used to fold into one answer,
    and only a caller **deciding** something turns it on (a live connect, issue
    #429's review round 2): **Slack said the conversation is not this token's**
    — a code in :data:`_SLACK_UNSEEABLE` — is direct evidence, and a connect
    built on it would start a connector mirroring a run into a channel nobody
    invited the bot to. **Slack could not be asked** — a network failure, an
    exhausted 429, no token — stays best-effort either way, because a directory
    lookup must not be what decides a thread's identity, and so does a code that
    only says this call would not be *served* (:data:`_SLACK_UNSEEABLE` names
    the one exclusion and why). Off by default: a *recorded* target
    (``adopt_launch_targets``) and a one-shot read or post do not decide whether
    a thread is free to attach.

    A held-thread refusal check uses *require_resolution*: an unavailable or
    empty answer cannot establish that another spelling is not already held.
    That deciding path refuses every lookup failure; ordinary reads and
    adoption keep the best-effort default.
    """
    channel, ts = ref.key["channel"], ref.key["thread_ts"]
    try:
        messages = client.get_replies(channel, ts)
    except Exception as exc:
        if require_resolution or (refuse_refusals and getattr(exc, "code", None) in _SLACK_UNSEEABLE):
            raise _slack_failure(exc, channel) from None
        logger.warning("thread_root_unresolved channel=%s ts=%s error=%s", channel, ts, exc)
        return ref, None
    first = messages[0] if messages else {}
    if not isinstance(first, dict):
        first = {}
    root = first.get("thread_ts") or first.get("ts")
    if require_resolution and (not isinstance(root, str) or not root.strip()):
        raise ThreadError(
            f"Slack did not identify the thread root for {channel}/{ts} — "
            "retry once it can be verified, or launch without that thread",
            status=502,
        )
    if not isinstance(root, str) or not root or root == ts:
        return ref, messages
    logger.info("thread_root_resolved channel=%s from=%s to=%s", channel, ts, root)
    return ref.rekeyed({"channel": channel, "thread_ts": root}), None


def _refuse_encrypted(room_id: str) -> None:
    """One phrasing for the one room lmer cannot work in, wherever it is met."""
    raise ThreadError(
        f"room {room_id} is end-to-end encrypted, and lmer reads and writes "
        "plaintext events only — use an unencrypted room (encryption is a "
        "follow-up)",
        status=400,
    )


def matrix_resolved(ref: ThreadRef, client, *, refuse_encrypted: bool = True) -> ThreadRef:
    """*ref* keyed by the room id, refusing an encrypted room by name."""
    room = ref.key["room"]
    try:
        room_id = client.resolve_room(room)
        encrypted = client.room_is_encrypted(room_id) if refuse_encrypted else False
    except ThreadError:
        raise
    except Exception as exc:
        raise ThreadError(f"the homeserver could not be asked about {room}: {exc}", status=400) from None
    if encrypted:
        _refuse_encrypted(room_id)
    return ref.rekeyed({"room": room_id, "event": ref.key["event"]})


def resolve_thread(
    permalink: object, *, env: Optional[dict] = None, where: str = DAEMON_ENVIRONMENT,
) -> tuple:
    """``(the thread a permalink names, a client that can reach it, what the
    resolution already fetched)`` — the third being Slack's messages, or ``None``
    when there are none to reuse."""
    ref = parse_thread(permalink)
    if ref.channel == CHANNEL_SLACK:
        client = _slack_client(slack_token(env, where=where))
        rooted, messages = slack_thread(ref, client)
        return rooted, client, messages
    client = _matrix_client(matrix_account(env, where=where))
    return matrix_resolved(ref, client), client, None


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def stage_upload(store: Optional[UploadStore], name: str, data: bytes, media_type: str) -> Optional[dict]:
    """Copy one file into the session's upload store and describe it.

    ``None`` when there is no store or the bytes cannot be written: what a
    session then loses is the file, not the message that mentioned it.

    Written through a descriptor with ``O_EXCL`` and ``fchmod`` rather than by
    name, because this store is bind-mounted ``rw`` into a container an agent
    drives: it can plant a symlink at the name between the create and the chmod,
    and a chmod that resolves the name again applies the mode to whatever it now
    points at. :func:`lmer_platform.uploads.store_upload` carries the measurement
    that argument comes from.
    """
    if store is None:
        return None
    safe = _UNSAFE_NAME.sub("_", name or "file")[:80] or "file"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    directory = Path(store.host_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("thread_file_unwritable name=%s error=%s", name, exc)
        return None
    attempt = 0
    while True:
        target = directory / (f"{stamp}-{safe}" if not attempt else f"{stamp}-{attempt}-{safe}")
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            attempt += 1
            if attempt > 100:
                logger.warning("thread_file_unwritable name=%s error=too many collisions", name)
                return None
            continue
        except OSError as exc:
            logger.warning("thread_file_unwritable name=%s error=%s", name, exc)
            return None
        break
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
    except OSError as exc:
        # A half-written file in a directory the agent is told to read is worse
        # than none.
        target.unlink(missing_ok=True)
        logger.warning("thread_file_unwritable name=%s error=%s", name, exc)
        return None
    media = (media_type or "application/octet-stream").split(";")[0]
    container_path = str(Path(store.container_dir) / target.name)
    return {
        "name": name or target.name,
        "bytes": len(data),
        "media_type": media,
        "path": container_path,
        "reference": f"[lmer upload] {container_path} ({media}, {len(data)} B)",
    }


def stored_name_of(item: dict, store: Optional[UploadStore]) -> Optional[str]:
    """The name inside the store that an attachment the session sent refers to.

    The session hands over a path inside the container, and this is the only join
    of that text onto a host path in the feature — so it accepts exactly one
    shape, ``<container store>/<one name>``, and answers ``None`` for everything
    else. Not a sanitiser: ``…/../../etc/shadow`` is refused rather than reduced
    to ``shadow``, which would have read a file of the store while the request
    said something quite different.

    A *name* and not a path, because the name is only half the answer: the store
    is writable from inside the container, so what is at that name is decided by
    :func:`read_from_store` on a descriptor, not here.
    """
    path = item.get("path") if isinstance(item, dict) else None
    if not isinstance(path, str) or store is None:
        return None
    prefix = store.container_dir.rstrip("/") + "/"
    if not path.startswith(prefix):
        return None
    name = path[len(prefix):]
    if not name or "/" in name or name in (".", ".."):
        return None
    return name


def read_from_store(
    item: dict, store: Optional[UploadStore], *, max_bytes: int = MAX_FILE_BYTES,
) -> bytes:
    """The bytes of one attachment the session named, or :class:`ThreadError`.

    Opened with ``O_NOFOLLOW`` and checked with ``fstat`` on the descriptor. The
    store is bind-mounted ``rw`` into the container, so a name that pointed at a
    regular file when it was listed can be a symlink to the host's own state by
    the time it is opened — the discipline and the measurement behind it are
    :func:`lmer_platform.uploads.open_stored`'s. Without it this reads whatever
    the daemon user can read and posts it into somebody else's thread.
    """
    name = stored_name_of(item, store)
    if name is None or store is None:
        raise ThreadError("not in this session's upload store", status=400)
    try:
        descriptor = os.open(
            Path(store.host_dir) / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
    except OSError as exc:
        # ELOOP (a symlink at that name), ENOENT and EACCES all say the same
        # thing to the caller: there is no file of that name to send.
        raise ThreadError(f"not readable in this session's upload store ({exc.strerror})", status=400) from None
    # On the descriptor and before anything opens a stream over it: a directory
    # that got the name raises out of ``fdopen`` itself, which is a traceback
    # rather than the refusal this owes its caller.
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ThreadError("not a regular file in this session's upload store", status=400)
        if info.st_size > max_bytes:
            raise ThreadError(
                f"{info.st_size} bytes; this posts at most {max_bytes}", status=400
            )
    except BaseException:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read(max_bytes)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _iso_from_slack_ts(ts: object) -> Optional[str]:
    try:
        seconds = float(str(ts))
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso_from_millis(value: object) -> Optional[str]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def render_record(record: dict) -> str:
    """One thread message as the session reads it: the bus's own source tag."""
    channel = record.get("channel") or "?"
    author = record.get("author")
    who = f"{channel} · {author}" if author else channel
    lines = [f"[{who}] {record.get('text') or ''}".rstrip()]
    for item in record.get("attachments") or []:
        lines.append(f"    {_attachment_line(item)}")
    return "\n".join(lines)


def _attachment_line(item: object) -> str:
    """A staged file names the path the session can open; one that was only
    described names what it is, because there was nowhere to put it."""
    if not isinstance(item, dict):
        return f"📎 {item}"
    reference = item.get("reference")
    if reference:
        return str(reference)
    size = item.get("bytes")
    detail = f"{item.get('media_type')}, {size} B" if size else str(item.get("media_type"))
    reason = item.get("reason") or "it was not copied in"
    return f"📎 {item.get('name')} ({detail}) — not copied in: {reason}"


def read_thread(
    permalink: object,
    *,
    env: Optional[dict] = None,
    limit: int = _requests.DEFAULT_READ_LIMIT,
    store: Optional[UploadStore] = None,
    where: str = DAEMON_ENVIRONMENT,
    client=None,
    ref: Optional[ThreadRef] = None,
) -> dict:
    """Read up to *limit* messages as bus-style records; pagination is transport-specific."""
    limit = max(1, min(int(limit or _requests.DEFAULT_READ_LIMIT), _requests.MAX_READ_LIMIT))
    fetched = None
    if ref is None or client is None:
        ref, client, fetched = resolve_thread(permalink, env=env, where=where)
    # One budget for the whole read, carried in a cell so each message spends
    # from what the ones before it left.
    budget = [MAX_READ_BYTES]
    if ref.channel == CHANNEL_SLACK:
        records = _read_slack(
            ref, client, limit=limit, store=store, budget=budget, fetched=fetched
        )
    else:
        records = _read_matrix(ref, client, limit=limit, store=store, budget=budget)
    return {"thread": _thread_summary(ref), "messages": records}


def _thread_summary(ref: ThreadRef) -> dict:
    return {
        "id": ref.id,
        "channel": ref.channel,
        "label": ref.label,
        "permalink": ref.canonical,
    }


def _read_slack(
    ref: ThreadRef, client, *, limit: int, store: Optional[UploadStore], budget: list,
    fetched: Optional[list] = None,
) -> list:
    channel, thread_ts = ref.key["channel"], ref.key["thread_ts"]
    messages = fetched
    if messages is None:
        try:
            messages = client.get_replies(channel, thread_ts)
        except Exception as exc:
            raise _slack_failure(exc, channel) from None
    names: dict = {}
    records = []
    for message in messages[-limit:]:
        if not isinstance(message, dict):
            continue
        ts = message.get("ts")
        records.append({
            "channel": CHANNEL_SLACK,
            "author": _slack_author(client, message, names),
            "text": str(message.get("text") or ""),
            "ref": f"{channel}:{thread_ts}:{ts}" if ts else None,
            "at": _iso_from_slack_ts(ts),
            "attachments": _stage_slack_files(
                client, message.get("files") or [], store, budget
            ),
        })
    return records


def slack_display_name(client, user_id: Optional[str], names: dict) -> Optional[str]:
    """A Slack user's display name, cached in *names*.

    Shared with :class:`slack_chat.connector.SlackConnector`, which asks the same
    question of the same endpoint with the same three fallbacks — the connector
    keeps the cache because its is per-thread and long-lived, while a read's is
    the read.
    """
    if not user_id:
        return None
    cached = names.get(user_id)
    if cached:
        return cached
    name = user_id
    try:
        data = client._request("GET", "users.info", params={"user": user_id})
        profile = (data.get("user") or {}).get("profile") or {}
        name = (
            profile.get("display_name") or profile.get("real_name")
            or (data.get("user") or {}).get("name") or user_id
        )
    except Exception:
        # Any failure here costs a name, never the message it belongs to.
        pass
    names[user_id] = name
    return name


def _slack_author(client, message: dict, names: dict) -> Optional[str]:
    user = message.get("user")
    if not user:
        return message.get("username") or message.get("bot_id") or None
    return slack_display_name(client, user, names)


def _stage_slack_files(client, files: list, store: Optional[UploadStore], budget: list) -> list:
    """Describe every file on a message, and copy it in when there is a store.

    Described either way: a read served to the platform's own routes has no
    session store behind it, and a message whose only content was a file would
    otherwise arrive empty. A description that could not become a copy says why —
    "no upload store" is one reason among several and the wrong one to show for
    the others.
    """
    described = []
    for item in files:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("title") or "file"
        media_type = (item.get("mimetype") or "application/octet-stream").split(";")[0]
        url = item.get("url_private_download") or item.get("url_private")
        record = {"name": name, "media_type": media_type, "bytes": item.get("size")}
        if store is None:
            record["reason"] = "this session has no upload store"
        elif not url:
            record["reason"] = "the thread does not say where to fetch it"
        elif budget[0] <= 0:
            record["reason"] = "this read has already copied in as much as it will"
        else:
            data = fetch_slack_file(url, client.bot_token, max_bytes=min(MAX_FILE_BYTES, budget[0]))
            if data is None:
                record["reason"] = "it could not be fetched, or is over the size this copies in"
            else:
                staged = stage_upload(store, name, data, media_type)
                if staged is None:
                    # Told apart from the fetch failing, as the matrix path does:
                    # the two send an operator to different places.
                    record["reason"] = "it could not be written to the upload store"
                else:
                    budget[0] -= len(data)
                    record = staged
        described.append(record)
    return described


def fetch_slack_file(
    url: str, token: str, *, max_bytes: int = MAX_FILE_BYTES,
) -> Optional[bytes]:
    """One Slack file, or ``None`` when it cannot be had inside the bound.

    Streamed and counted as it arrives. The unstreamed version applied the same
    bound *after* the body was resident, which is not a bound on this process at
    all: a thread the run was merely pointed at could hold a file of any size,
    and on the platform route the process reading it is the daemon that serves
    every session in the fleet.
    """
    try:
        with requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30, stream=True,
        ) as response:
            response.raise_for_status()
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                logger.info("thread_read_file_too_large bytes=%s", declared)
                return None
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                body.extend(chunk)
                if len(body) > max_bytes:
                    # A Content-Length is a claim, not a promise: the count of
                    # what actually arrived is what stops here.
                    logger.info("thread_read_file_too_large bytes=over %s", max_bytes)
                    return None
            return bytes(body)
    except requests.RequestException as exc:
        logger.warning("thread_read_file_failed error=%s", exc)
        return None


def _read_matrix(
    ref: ThreadRef, client, *, limit: int, store: Optional[UploadStore], budget: list,
) -> list:
    room, event = ref.key["room"], ref.key["event"]
    try:
        events = [client.get_event(room, event)] + list(client.thread_events(room, event, limit=limit))
    except Exception as exc:
        raise _matrix_failure(exc, room) from None
    names: dict = {}
    records = []
    for item in events[-limit:]:
        if not isinstance(item, dict):
            continue
        content = item.get("content") or {}
        sender = item.get("sender")
        if sender and sender not in names:
            try:
                names[sender] = client.display_name(sender)
            except Exception:
                names[sender] = sender
        records.append({
            "channel": CHANNEL_MATRIX,
            "author": names.get(sender) or sender,
            "text": str(content.get("body") or ""),
            "ref": item.get("event_id"),
            "at": _iso_from_millis(item.get("origin_server_ts")),
            "attachments": _stage_matrix_file(client, content, store, budget),
        })
    return records


def _stage_matrix_file(client, content: dict, store: Optional[UploadStore], budget: list) -> list:
    """The message's file, described, and copied in when there is room for it."""
    mxc = content.get("url")
    if not isinstance(mxc, str) or not mxc.startswith("mxc://"):
        return []
    info = content.get("info") if isinstance(content.get("info"), dict) else {}
    name = str(content.get("filename") or content.get("body") or "file")
    described = {
        "name": name,
        "media_type": str(info.get("mimetype") or "application/octet-stream"),
        "bytes": info.get("size"),
    }
    if store is None:
        return [{**described, "reason": "this session has no upload store"}]
    if budget[0] <= 0:
        return [{**described, "reason": "this read has already copied in as much as it will"}]
    declared = info.get("size")
    if isinstance(declared, int) and not isinstance(declared, bool) and declared > MAX_FILE_BYTES:
        # What the event claims, before asking for the bytes: a homeserver's
        # download has no bound of its own to fall back on.
        return [{**described, "reason": "it is over the size this copies in"}]
    try:
        data, media_type = client.download(mxc, max_bytes=min(MAX_FILE_BYTES, budget[0]))
    except Exception as exc:
        logger.warning("thread_read_file_failed mxc=%s error=%s", mxc, exc)
        return [{**described, "reason": "it could not be fetched"}]
    if data is None or len(data) > MAX_FILE_BYTES:
        return [{**described, "reason": "it is over the size this copies in"}]
    staged = stage_upload(store, name, data, media_type)
    if staged is None:
        return [{**described, "reason": "it could not be written to the upload store"}]
    budget[0] -= len(data)
    return [staged]


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


def post_thread(
    permalink: object,
    text: str,
    *,
    label: str,
    env: Optional[dict] = None,
    attachments: Optional[list] = None,
    store: Optional[UploadStore] = None,
    where: str = DAEMON_ENVIRONMENT,
    client=None,
    ref: Optional[ThreadRef] = None,
    poster: Optional[Poster] = None,
) -> dict:
    """Post once into a thread, attributed to *label*.

    *poster* is the machine-readable half of that attribution (#427): absent
    means nothing attributes the post, and a connector watching the thread will
    skip it as it skips its own echo.
    """
    body = (text or "").strip()
    if not body:
        raise ThreadError("a post needs text", status=400)
    if ref is None or client is None:
        ref, client, _fetched = resolve_thread(permalink, env=env, where=where)
    composed = f"[{label}] {body}" if label else body
    named = list(attachments or [])
    if ref.channel == CHANNEL_SLACK:
        posted = _post_slack(ref, client, composed, named, poster=poster)
    else:
        posted = _post_matrix(ref, client, composed, named, store=store, env=env, poster=poster)
    return {"thread": _thread_summary(ref), "posted": posted}


def _clip(text: str) -> tuple[str, bool]:
    """The text a thread will take, and whether anything was left behind.

    Reported rather than done quietly: the bus record keeps the whole message, so
    a caller told "posted" while the thread got two thirds of it has no way to
    find out — which is the shape of failure this feature's own honesty rules are
    written against.
    """
    if len(text) <= MAX_POST_CHARS:
        return text, False
    return text[: MAX_POST_CHARS - 1] + "…", True


def _post_slack(
    ref: ThreadRef, client, text: str, attachments: list, *, poster: Optional[Poster] = None,
) -> dict:
    # Named, not uploaded: the connector's own outbound rule (docs/COMMS.md §9).
    # The file is in the session's store, not in the thread's workspace.
    for item in attachments:
        name = item.get("name") if isinstance(item, dict) else None
        if name:
            text += f"\n📎 {name}"
    body, clipped = _clip(text)
    channel, thread_ts = ref.key["channel"], ref.key["thread_ts"]
    try:
        ts = client.post_message(channel, thread_ts, body)
    except Exception as exc:
        raise _slack_failure(exc, channel) from None
    if poster is not None:
        # After the post: the id being recorded is the one Slack just assigned.
        # A connector polling inside that gap holds the message (§9).
        thread_posts.record(channel, ts, poster)
    return {
        "ref": f"{channel}:{thread_ts}:{ts}",
        "at": _iso_from_slack_ts(ts),
        "clipped": clipped,
    }


def _post_matrix(
    ref: ThreadRef, client, text: str, attachments: list, *,
    store: Optional[UploadStore], env: Optional[dict], poster: Optional[Poster] = None,
) -> dict:
    from matrix_bridge.scrub import scrub_bytes, scrub_text
    from matrix_chat.connector import AUTHENTICATED_MEDIA_ENV

    from .util import get_bool_env

    room, event = ref.key["room"], ref.key["event"]
    body, clipped = _clip(scrub_text(text))
    # On the event, not in a registry: matrix hands the content back verbatim.
    stamp = thread_posts.matrix_content(poster)
    try:
        event_id = client.send_thread_text(room, event, body, extra=stamp)
    except Exception as exc:
        raise _matrix_failure(exc, room) from None
    authenticated = get_bool_env(
        AUTHENTICATED_MEDIA_ENV, False, env=env if env is not None else os.environ
    )
    withheld = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "file")
        reason = _matrix_attachment(
            client, ref, item, store=store, authenticated=authenticated,
            scrub_bytes=scrub_bytes, last_event_id=event_id, stamp=stamp,
        )
        if reason:
            withheld.append(f"📎 {name} withheld: {reason}")
    if withheld:
        with_note = "\n".join(withheld)
        try:
            client.send_thread_text(
                room, event, with_note, last_event_id=event_id, msgtype="m.notice",
                extra=stamp,
            )
        except Exception as exc:
            logger.warning("thread_post_note_failed room=%s error=%s", room, exc)
    return {"ref": event_id, "at": None, "clipped": clipped}


def _matrix_attachment(
    client, ref: ThreadRef, item: dict, *, store, authenticated: bool, scrub_bytes,
    last_event_id, stamp: Optional[dict] = None,
) -> Optional[str]:
    """Upload one attachment; return why it was withheld, or ``None``.

    Every reason is a fixed phrase: this string is posted into a thread the run
    does not own, and an exception's own text carries host paths and homeserver
    URLs into somebody else's room. The detail goes to the log.
    """
    from matrix_chat.connector import AUTHENTICATED_MEDIA_ENV

    if not authenticated:
        return (
            f"set {AUTHENTICATED_MEDIA_ENV}=1 once the homeserver requires "
            "authentication for media"
        )
    try:
        data = read_from_store(item, store)
    except ThreadError as exc:
        logger.info("thread_attachment_unreadable error=%s", exc)
        return "it is not a readable file in this session's upload store"
    media_type = str(item.get("media_type") or "application/octet-stream")
    scrubbed = scrub_bytes(data, media_type)
    if scrubbed.refused:
        return "it carries something that looks like a credential"
    name = str(item.get("name") or "file")
    try:
        mxc = client.upload(scrubbed.data, content_type=media_type, filename=name)
        client.send_thread_file(
            ref.key["room"], ref.key["event"], mxc=mxc, name=name,
            media_type=media_type, size=len(scrubbed.data), last_event_id=last_event_id,
            extra=stamp,
        )
    except Exception as exc:
        logger.warning("thread_attachment_upload_failed name=%s error=%s", name, exc)
        return "the homeserver would not take it"
    return None


# ---------------------------------------------------------------------------
# Opening a thread
# ---------------------------------------------------------------------------


def open_thread(
    channel: object,
    text: str,
    *,
    env: Optional[dict] = None,
    where: str = DAEMON_ENVIRONMENT,
    client=None,
    poster: Optional[Poster] = None,
) -> dict:
    """Post *text* as the root of a new thread in *channel*, and name the thread.

    The one write here that *creates* something, so every refusal it can make is
    made before the post: a root message in a hand-named channel cannot be taken
    back.

    No ``[label]`` prefix, unlike :func:`post_thread`: the root's own text names
    the run, composed by :mod:`lmer_platform.session_threads`, so a prefix would
    repeat the line under it. *poster* is the machine-readable half all the same
    (#427) — a root nothing attributes is a root every connector on the thread
    reads as an own-echo to skip, including the connector of the run that is
    about to attach to it.
    """
    body = (text or "").strip()
    if not body:
        raise ThreadError("a thread's root message needs text", status=400)
    ref = parse_channel(channel)
    if ref.channel == CHANNEL_SLACK:
        return _open_slack(
            ref, client or _slack_client(slack_token(env, where=where)), body,
            poster=poster,
        )
    return _open_matrix(
        ref, client or _matrix_client(matrix_account(env, where=where)), body,
        poster=poster,
    )


def send_dm(
    recipient: object,
    text: str,
    *,
    env: Optional[dict] = None,
    where: str = DAEMON_ENVIRONMENT,
    client=None,
    poster: Optional[Poster] = None,
    established=(),
) -> dict:
    """Send *text* privately to *recipient*, and name the conversation (#441).

    Refusals come as early as each can be known: an **empty** list with nothing
    established can never be talked round, so it costs no transport call, while
    a non-empty one has to resolve the recipient first because comparing against
    it needs the id.

    *established* are conversations somebody other than the run connected — see
    :meth:`lmer_platform.session_threads.SessionThreads.conversation_ids`, which
    is where that filter and its reason live. Empty when the host side keeps no
    connections, and then the list is the only door.
    """
    body = (text or "").strip()
    if not body:
        raise ThreadError("a direct message needs text", status=400)
    who = dm_access.checked_recipient(recipient)
    if not established and not dm_access.allowed_recipients(who, env):
        # Knowable without asking anyone, and the case where asking would open
        # an IM with somebody the operator forbade and let a run that may
        # message nobody enumerate the directory (MR !315, review round 1).
        dm_access.check_recipient(who, env=env)
    if dm_access.recipient_channel(who) == CHANNEL_MATRIX:
        return _dm_matrix(
            who, body, env=env, where=where, client=client, poster=poster,
            established=established,
        )
    return _dm_slack(
        who, body, env=env, where=where, client=client, poster=poster,
        established=established,
    )


def _dm_slack(
    recipient: str, text: str, *, env, where, client, poster, established,
) -> dict:
    client = client or _slack_client(slack_token(env, where=where))
    try:
        workspace = client.workspace()
    except Exception as exc:
        raise _slack_failure(exc, recipient) from None
    user_id, spellings = _resolve_slack_user(client, recipient, workspace.get("scopes"))
    # Only what Slack confirmed about this account — its id and the handle it
    # holds — never the spelling the run typed, which may be a display name the
    # recipient chose for themselves and which any member could set to match an
    # entry naming somebody else.
    allowed = dm_access.permits(user_id, spellings=spellings, env=env)
    if not allowed and not established:
        # `conversations.open` mutates, so it is not reached for a person no
        # exemption could rescue.
        dm_access.check_recipient(user_id, spellings=spellings, env=env, shown=recipient)
    conversation = _dm_conversation_id(client, user_id, recipient)
    if not allowed and conversation not in established:
        dm_access.check_recipient(user_id, spellings=spellings, env=env, shown=recipient)
    body, clipped = _clip(text)
    try:
        posted = client.post_root(conversation, body)
    except Exception as exc:
        raise _slack_failure(exc, recipient) from None
    if poster is not None:
        thread_posts.record(posted["channel"], posted["ts"], poster)
    return {
        "conversation": {
            "channel": CHANNEL_SLACK,
            "recipient": recipient,
            "id": posted["channel"],
            "permalink": slack_thread_link(workspace["host"], posted["channel"], posted["ts"]),
        },
        "posted": {
            "ref": f"{posted['channel']}:{posted['ts']}:{posted['ts']}",
            "at": _iso_from_slack_ts(posted["ts"]),
            "clipped": clipped,
        },
    }


def _dm_conversation_id(client, user_id: str, recipient: str) -> str:
    try:
        return client.open_dm(user_id)
    except Exception as exc:
        raise _slack_failure(exc, recipient) from None


def _resolve_slack_user(client, recipient: str, scopes) -> tuple:
    """``(user id, other spellings)`` for *recipient*, or a refusal.

    A Slack id needs nothing and is used as given. A username needs
    ``users:read``, so the scopes the ``auth.test`` already reported decide
    whether to look at all: known and missing means refuse here, before a call
    that would fail anyway. Scopes that could not be read are *not knowing*
    rather than evidence — the same reading `_refuse_unverifiable_membership`
    takes — so the lookup is attempted and Slack's own ``missing_scope`` answers
    it.
    """
    if dm_access.slack_user_id(recipient):
        return recipient, ()
    if scopes is not None and SLACK_USERS_READ not in scopes:
        raise ThreadError(_no_username_lookup(recipient), status=403)
    try:
        found = client.find_user(recipient)
    except Exception as exc:
        if getattr(exc, "code", None) == "missing_scope":
            raise ThreadError(_no_username_lookup(recipient), status=403) from None
        raise _slack_failure(exc, recipient) from None
    if not found:
        raise ThreadError(
            f"no Slack user is called {recipient} — name them by user id (U…) or "
            "check the spelling",
            status=404,
        )
    if len(found) > 1:
        named = ", ".join(f"{item['name']} ({item['id']})" for item in found[:5])
        raise ThreadError(
            f"{recipient} names {len(found)} people in this workspace ({named}) — "
            "say which by user id, because the wrong one cannot be unsent",
            status=409,
        )
    return found[0]["id"], (found[0].get("name") or "",)


def _no_username_lookup(recipient: str) -> str:
    return (
        f"this host's Slack app cannot look up the username {recipient}: that "
        f"needs the {SLACK_USERS_READ} scope, which it does not carry. Name the "
        "person by user id (U…) instead"
    )


def _dm_matrix(
    recipient: str, text: str, *, env, where, client, poster, established,
) -> dict:
    client = client or _matrix_client(matrix_account(env, where=where))
    try:
        room = client.direct_room(recipient)
    except Exception as exc:
        raise _matrix_failure(exc, recipient) from None
    if room is None or room not in established:
        dm_access.check_recipient(recipient, env=env)
    if room is None:
        try:
            room = client.create_direct_room(recipient)
        except Exception as exc:
            raise _matrix_failure(exc, recipient) from None
    # Before the send and not after: an encrypted room takes the message and
    # renders it unreadable to everyone, which looks like delivery and is not.
    try:
        encrypted = client.room_is_encrypted(room)
    except Exception as exc:
        raise _matrix_failure(exc, recipient) from None
    if encrypted:
        raise ThreadError(
            f"the direct room with {recipient} ({room}) is encrypted, and this "
            "host's bot account holds no keys for it — the message would arrive "
            "unreadable. Nothing was sent",
            status=409,
        )
    body, clipped = _clip(text)
    try:
        event = client.send_message(room, {"msgtype": "m.text", "body": body})
    except Exception as exc:
        raise _matrix_failure(exc, recipient) from None
    if poster is not None:
        thread_posts.record(room, event, poster)
    return {
        "conversation": {
            "channel": CHANNEL_MATRIX,
            "recipient": recipient,
            "id": room,
            "permalink": matrix_to_link(room, event),
        },
        "posted": {"ref": f"{room}:{event}", "at": None, "clipped": clipped},
    }


def _open_slack(
    ref: ChannelRef, client, text: str, *, poster: Optional[Poster] = None,
) -> dict:
    # One `auth.test` before the post, for two reasons: it is what turns the
    # reply into a permalink (asking afterwards would leave a thread nothing here
    # could name), and it carries the token's granted scopes, which decide
    # whether "the bot is in the channel" is a fact anyone can enforce.
    try:
        workspace = client.workspace()
    except Exception as exc:
        raise _slack_failure(exc, ref.target) from None
    _refuse_unverifiable_membership(workspace.get("scopes"), ref.target)
    host = workspace["host"]
    body, clipped = _clip(text)
    try:
        opened = client.post_root(ref.target, body)
    except Exception as exc:
        raise _slack_failure(exc, ref.target) from None
    if poster is not None:
        # After the post, on the id Slack just assigned — `_post_slack`'s rule
        # and its reason (a connector polling inside that gap holds the message).
        thread_posts.record(opened["channel"], opened["ts"], poster)
    thread = parse_thread(slack_thread_link(host, opened["channel"], opened["ts"]))
    return {
        "thread": _thread_summary(thread),
        "posted": {
            "ref": f"{opened['channel']}:{opened['ts']}:{opened['ts']}",
            "at": _iso_from_slack_ts(opened["ts"]),
            "clipped": clipped,
        },
    }


def _refuse_unverifiable_membership(scopes: Optional[list], channel: str) -> None:
    """Refuse to open a thread with an app that can post where it was not invited.

    On Slack lmer does not check membership itself — ``chat.postMessage``
    answering ``not_in_channel`` **is** the check — and ``chat:write.public``
    stops that error firing for every public channel in the workspace. So the one
    case #429's rule cannot be enforced in is refused rather than guessed at.
    ``None`` scopes is *not knowing* (a proxy that strips the header) and stays
    permitted, since refusing on an absence would break those installs.
    docs/COMMS.md §9 carries the rule and the operator's decision behind it.
    """
    if not scopes or "chat:write.public" not in scopes:
        return
    raise ThreadError(
        "the host's Slack app carries chat:write.public, so it can post into "
        f"channels it was never invited to — and Slack's not_in_channel, which is "
        f"how lmer knows it may not write in {channel}, then never fires. Remove "
        "that scope from the app's bot token scopes (membership goes back to "
        "being Slack's to enforce) and open the thread again, or grant "
        "channels:read and ask for the membership probe #429 left as a follow-up",
        status=403,
    )


def _open_matrix(
    ref: ChannelRef, client, text: str, *, poster: Optional[Poster] = None,
) -> dict:
    """A room's new thread root, refusing a room the bot is not in by name.

    **Membership before encryption**, which is not a preference: the encryption
    probe is a room-state read, and a homeserver answers a non-member ``403``
    for it — so asking that first turned every "invite the bot" case into the
    generic homeserver refusal and left ``joined_rooms`` unreachable (review
    finding on !299, measured against a client that fails the state read the way
    the real one does). ``joined_rooms`` is the one call here that needs no
    membership, and the two refusals an operator acts on are different errands.
    """
    from matrix_bridge.scrub import scrub_text

    try:
        room_id = client.resolve_room(ref.target)
        joined = client.joined_rooms()
    except Exception as exc:
        raise _matrix_failure(exc, ref.target) from None
    if room_id not in joined:
        raise ThreadError(
            f"the host's matrix bot is not in {room_id} — invite it to the room "
            "(lmer joins nothing by itself), then open the thread again",
            status=403,
        )
    try:
        encrypted = client.room_is_encrypted(room_id)
    except Exception as exc:
        raise _matrix_failure(exc, room_id) from None
    if encrypted:
        _refuse_encrypted(room_id)
    body, clipped = _clip(scrub_text(text))
    content = {
        **thread_posts.matrix_content(poster),
        "msgtype": "m.text",
        "body": body,
    }
    try:
        event_id = client.send_message(room_id, content)
    except Exception as exc:
        raise _matrix_failure(exc, room_id) from None
    thread = parse_thread(matrix_to_link(room_id, event_id))
    return {
        "thread": _thread_summary(thread),
        "posted": {"ref": event_id, "at": None, "clipped": clipped},
    }


# ---------------------------------------------------------------------------
# Failures the operator can act on
# ---------------------------------------------------------------------------

def _slack_failure(exc: Exception, channel: str) -> ThreadError:
    """The refusal a caller acts on, from Slack's own error code.

    From ``SlackError.code`` — the ``error`` field of an ``ok: false`` reply —
    and not from the exception's prose, which is composed differently on three
    paths and is nobody's contract. This verdict sets the status the platform
    route answers with, so "your token cannot see this channel" and "Slack is
    having a bad day" have to be told apart by something Slack said.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _SLACK_HINTS:
        return ThreadError(f"Slack refused {channel}: {code} — {_SLACK_HINTS[code]}", status=403)
    return ThreadError(f"Slack refused {channel}: {exc}", status=502)


def _matrix_failure(exc: Exception, room: str) -> ThreadError:
    status = getattr(exc, "status", None)
    if status in (401, 403):
        return ThreadError(
            f"the homeserver refused {room}: {exc} — the host's bot account may "
            "not be in that room",
            status=403,
        )
    return ThreadError(f"the homeserver refused {room}: {exc}", status=502)


# ---------------------------------------------------------------------------
# The servicer
# ---------------------------------------------------------------------------

class ThreadServicer(threading.Thread):
    """The host side of a session's thread requests.

    Given a supervisor address and its control token, it long-polls for
    requests, does the Slack or matrix call with the host's own credentials, and
    posts the answer back. One per session: a request is claimed when it is
    handed out, so two servicers on one session would each get some of the
    requests rather than both getting all of them — but a second one is still a
    mistake, because only one of them holds the run's label.

    *connections* is the host side of a run asking about its **own** threads
    (issue #429) — open one in a channel, connect one, disconnect one. The
    platform daemon has such an object because it keeps the connections and runs
    their connectors; the host ``lmer`` process does not, since its connectors
    are launch arguments, so there it is ``None`` and those three verbs are
    refused with that reason.
    """

    def __init__(
        self,
        *,
        supervisor_url: str,
        token: str,
        label: object = "",
        run_name: object = None,
        env: Optional[dict] = None,
        store: Optional[UploadStore] = None,
        where: str = DAEMON_ENVIRONMENT,
        connections=None,
        name: str = "lmer-thread-servicer",
        connect_budget: Optional[float] = None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        super().__init__(name=name, daemon=True)
        self.supervisor_url = supervisor_url
        self.token = token
        self.label = label
        #: NOT ``run``: this is a :class:`threading.Thread`, whose ``run`` is
        #: the thread's own entry point.
        self.run_name = run_name
        self.env = env
        self.store = store
        self.where = where
        self.connections = connections
        self.stop_event = threading.Event()
        self.poll_seconds = poll_seconds
        self.failures = 0
        self._connect_budget = connect_budget
        self._client = None
        self._session: Optional[str] = None

    # -- transport -------------------------------------------------------------

    @property
    def client(self):
        if self._client is None:
            self._client = bus.BusClient(bus.Endpoint(url=self.supervisor_url, token=self.token))
        return self._client

    def stop(self, timeout: Optional[float] = None) -> None:
        """Ask the loop to end. It is inside a long-poll, so the wait is that
        poll plus slack — a servicer is not killed for being mid-request."""
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout=self.poll_seconds + 5.0 if timeout is None else timeout)

    # -- the loop --------------------------------------------------------------

    def run(self) -> None:
        budget = self._connect_budget
        if budget is None:
            budget = bus.connect_budget(self.env if self.env is not None else os.environ)
        if not self.client.wait_ready(budget=budget, stop=self.stop_event):
            return
        since = 0
        while not self.stop_event.is_set():
            try:
                requests_, since = self.client.thread_claim(since, timeout=self.poll_seconds)
            except bus.NoChannel as exc:
                logger.info("thread_servicer_gone reason=%s", exc)
                return
            except bus.BusError as exc:
                logger.warning("thread_servicer_poll_failed error=%s", exc)
                self._backoff()
                continue
            self.failures = 0
            for request in requests_:
                self._answer(request)

    def _backoff(self) -> None:
        self.failures += 1
        self.stop_event.wait(min(BACKOFF_MAX, 0.5 * (2 ** min(self.failures, 6))))

    def _answer(self, request: dict) -> None:
        try:
            result = self.handle(request)
        except ThreadError as exc:
            result = {"ok": False, "error": str(exc), "status": exc.status}
        except Exception as exc:  # a servicer must outlive one bad request
            logger.warning("thread_servicer_failed op=%s error=%s", request.get("op"), exc)
            result = {"ok": False, "error": f"the host side could not do it: {exc}", "status": 500}
        try:
            self.client.thread_complete(request.get("id"), result)
        except (bus.BusError, bus.NoChannel) as exc:
            logger.warning("thread_servicer_result_lost id=%s error=%s", request.get("id"), exc)

    def handle(self, request: dict) -> dict:
        op = request.get("op")
        permalink = request.get("permalink")
        if op in _requests.CONNECTION_OPS:
            payload = self._connection_work(op, request)
        elif op == _requests.OP_READ:
            payload = read_thread(
                permalink, env=self.env, limit=request.get("limit") or _requests.DEFAULT_READ_LIMIT,
                store=self.store, where=self.where,
            )
        elif op == _requests.OP_DM:
            payload = send_dm(
                request.get("recipient"), request.get("text") or "",
                env=self.env, where=self.where, poster=self.poster(),
                established=self.established_conversations(),
            )
        elif op == _requests.OP_POST:
            payload = post_thread(
                permalink, request.get("text") or "", label=self.resolved_label(),
                env=self.env, attachments=request.get("attachments") or [],
                store=self.store, where=self.where, poster=self.poster(),
            )
        else:
            raise ThreadError(f"unknown thread operation {op!r}", status=400)
        return {"ok": True, **payload}

    def _connection_work(self, op: str, request: dict) -> dict:
        """A run's own threads: open, connect, disconnect (issue #429)."""
        if self.connections is None:
            raise ThreadError(
                "this session's threads belong to the `lmer` process that "
                "started it, which takes them as launch arguments and cannot "
                "change them while it runs — ask the operator to connect it from "
                "the platform, or start it with the thread as a target",
                status=409,
            )
        if op == _requests.OP_OPEN:
            # The root message is a post this host makes, so it carries the same
            # machine-readable attribution every other one does (#427). Composed
            # here because this is the side that knows which session it serves.
            return self.connections.open(
                request.get("channel"), text=request.get("text") or "",
                poster=self.poster(),
            )
        if op == _requests.OP_CONNECT:
            return self.connections.connect(request.get("permalink"))
        return self.connections.disconnect(request.get("permalink"))

    def established_conversations(self) -> tuple:
        """Conversation ids this session is already connected to (#441).

        The exemption's evidence: a DM the human opened with this run is an
        ordinary connected thread, and a run answering in it needs no entry on
        the operator's list, because the human chose that channel. A host side
        that keeps no connections — the shell `lmer` process, whose threads are
        launch arguments — knows of none, and the allow list is then the only
        door rather than a check that silently passed.
        """
        if self.connections is None:
            return ()
        try:
            records = self.connections.conversation_ids()
        except Exception as exc:  # a refusal here must not cost the refusal below
            logger.debug("dm_established_unknown error=%s", exc)
            return ()
        return tuple(records)

    def resolved_label(self) -> str:
        """How the thread is told who wrote this. Called per post, because a run
        gains its title after it starts."""
        label = self.label() if callable(self.label) else self.label
        return str(label or "")

    def poster(self) -> Optional[Poster]:
        """The machine-readable half of this post's attribution, or ``None``
        when this side cannot say which session it serves — in which case the
        post is deliberately unattributed rather than half-attributed."""
        return Poster.for_session(self.resolved_session(), self.resolved_run())

    def resolved_run(self) -> str:
        """The run's own name for `Poster.author`, which composes the "lmer"
        word itself — so this is the title or slug, never the `[label]` the
        thread shows, which already carries that word."""
        name = self.run_name() if callable(self.run_name) else self.run_name
        return str(name or "")

    def resolved_session(self) -> Optional[str]:
        """Which session this servicer serves, asked of that session's own
        supervisor (#427) — the same place the connector comparing against it
        asks, so the two cannot disagree. A supervisor that will not say costs
        the post its attribution, never the post."""
        if self._session is None:
            try:
                session = self.client.health().get("session")
            except (bus.BusError, bus.NoChannel) as exc:
                logger.debug("thread_servicer_session_unknown error=%s", exc)
                return None
            if isinstance(session, str) and session:
                self._session = session
        return self._session
