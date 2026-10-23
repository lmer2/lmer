"""A chat thread named by a permalink, parsed once for everyone (issue #414).

Both hosts that can act on a thread need the same reading of a permalink: the
platform daemon, which keeps thread *connections* and runs their connectors
(:mod:`lmer_platform.connectors`), and the host ``lmer`` process, which serves a
session's read/post requests for threads it is not connected to
(:mod:`lmer_cli.thread_access`). The parse decides identity — two spellings of
one thread must produce one :class:`ThreadRef` — so it lives in one place and
the platform re-exports it.

``docs/COMMS.md`` describes what a thread is to a session; this module is only
the reading of the link.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from matrix_chat.permalink import (
    is_matrix_room, is_matrix_thread_url, matrix_to_link, parse_matrix_permalink,
)
from slack_chat.permalink import (
    is_slack_thread_url, parse_slack_permalink, slack_thread_link,
)

from .comms import CHANNEL_MATRIX, CHANNEL_SLACK

__all__ = [
    "CHANNEL_SLACK", "CHANNEL_MATRIX", "MAX_THREAD_TARGETS", "MAX_PERMALINK_CHARS",
    "MAX_CHANNEL_CHARS", "MAX_RECIPIENT_CHARS", "ThreadError", "ThreadRef",
    "ChannelRef", "parse_thread", "parse_channel", "validate_thread_targets",
]

#: How many threads one launch list may carry.
MAX_THREAD_TARGETS = 16

MAX_PERMALINK_CHARS = 4096

#: How long the name of a channel to open a thread in may be. Over a Slack
#: channel name (80) and over a matrix room id or alias (255 bytes), and far
#: short of a permalink, which is the mistake this bound is here to catch.
MAX_CHANNEL_CHARS = 256

#: How long the name of a person to direct-message may be (issue #441). Over a
#: Slack user id (11) and username (21) and over an MXID (255 bytes), and short
#: enough that a pasted permalink is refused rather than sent to a transport.
MAX_RECIPIENT_CHARS = 256


class ThreadError(RuntimeError):
    """A thread request that cannot be honoured.

    Carries the status a route answers with, so the refusal is written once —
    where the reason is known — instead of being re-derived from the message at
    each caller (the platform routes and ``lmer-ctl`` both use it).
    """

    def __init__(self, message: str, status: int = 400, *, transient: bool = False,
                 retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.status = status
        #: The transport said "not now" (rate limit, 5xx, no answer), not "no".
        self.transient = transient
        #: Seconds the rate-limiting server asked for, when it named any.
        self.retry_after = retry_after


def validate_thread_targets(value: object) -> list[str]:
    """Return a list of permalinks, or raise ValueError before a launch changes state."""
    if not isinstance(value, list):
        raise ValueError("must be a list of Slack or matrix thread permalinks")
    if len(value) > MAX_THREAD_TARGETS:
        raise ValueError(f"must contain at most {MAX_THREAD_TARGETS} thread permalinks")
    for target in value:
        if not isinstance(target, str) or not target or len(target) > MAX_PERMALINK_CHARS:
            raise ValueError(
                "each thread permalink must be non-empty text, at most "
                f"{MAX_PERMALINK_CHARS} characters"
            )
        if any(char.isspace() or ord(char) < 32 for char in target):
            raise ValueError("thread permalinks must not contain whitespace or control characters")
        try:
            parsed = urlsplit(target)
            credential = parsed.username is not None or parsed.password is not None
        except ValueError:
            raise ValueError("invalid thread permalink") from None
        if credential or not (is_slack_thread_url(target) or is_matrix_thread_url(target)):
            raise ValueError("expected a Slack or matrix thread permalink without embedded credentials")
        if is_slack_thread_url(target):
            segments = [part for part in parsed.path.split("/") if part]
            timestamps = parse_qs(parsed.query, keep_blank_values=True).get("thread_ts", [])
            if (
                len(segments) > 2 and not re.fullmatch(r"p[0-9]{7,}", segments[2])
                or any(not re.fullmatch(r"[0-9]+\.[0-9]{6}", ts) for ts in timestamps)
            ):
                raise ValueError("Slack thread permalinks must contain valid numeric timestamps")
    return list(value)


@dataclass(frozen=True)
class ThreadRef:
    """One thread, parsed."""

    channel: str
    key: dict
    permalink: str
    label: str

    @property
    def key_text(self) -> str:
        if self.channel == CHANNEL_SLACK:
            return f"{self.key['channel']}:{self.key['thread_ts']}"
        return f"{self.key['room']}:{self.key['event']}"

    @property
    def id(self) -> str:
        """A derived id, so the same thread is always the same connection.

        Derived rather than minted: a duplicate is then a dict lookup, a
        ``DELETE`` has something to address, and a daemon restart needs no
        allocation table.

        It is also, exactly, the connector's own subscriber name: the same digest
        function over the same key, because the connector is handed
        :attr:`canonical` — the thread as *resolved* — rather than the link that
        named it. So one ``/healthz`` read answers "how far has this connection
        got" for every connection on a session, by id, with nothing to map
        between. Two earlier attempts at this sentence were wrong in the same
        way, each after a resolution was added on one side only (a matrix room
        alias, then a Slack link naming a message inside a thread), which is why
        the connector is now given the resolved thread instead of the raw link.
        """
        digest = hashlib.sha256(self.key_text.encode("utf-8")).hexdigest()[:16]
        return f"{self.channel}:{digest}"

    @property
    def canonical(self) -> str:
        """A permalink naming the thread this connection was *resolved* to.

        What the connector is given, rather than the link the operator pasted.
        The two differ exactly where the resolution did something — a matrix room
        alias, a Slack link naming a message inside a thread — and handing the raw
        link on would undo the resolution twice over: the connector would poll a
        message instead of its thread (``conversations.replies`` returns only the
        message named when it has no replies of its own), and it would hash its
        subscriber name from a key this side does not use, so the record and the
        position the supervisor holds for it would stop being the same string.

        Falls back to the link as given when it cannot be composed — there is
        nothing better to say, and the resolution is still in ``key``.
        """
        if self.channel == CHANNEL_MATRIX:
            return matrix_to_link(self.key["room"], self.key["event"])
        host = urlsplit(self.permalink).hostname
        if not host:
            return self.permalink
        return slack_thread_link(host, self.key["channel"], self.key["thread_ts"])

    def registry_key(self) -> tuple:
        if self.channel == CHANNEL_SLACK:
            return self.key["channel"], self.key["thread_ts"]
        return self.key["room"], self.key["event"]

    def rekeyed(self, key: dict) -> "ThreadRef":
        """The same link, keyed by what its transport says the thread is."""
        return ThreadRef(
            channel=self.channel, key=dict(key), permalink=self.permalink, label=self.label,
        )


def _slack_label(permalink: str, channel: str) -> str:
    """``<workspace> · <channel>``, read out of the permalink.

    Out of the link and not out of Slack: naming the channel would need
    ``conversations.info`` and therefore a token scope the operator's app may
    not have, which would turn a cosmetic hover into a way for a connect to
    fail. What the link carries is true without asking anyone.
    """
    host = (urlsplit(permalink).hostname or "").split(".")[0]
    return f"{host} · {channel}" if host else channel


def parse_thread(permalink: object, *, resolve_room=None) -> ThreadRef:
    """The thread a permalink names, or :class:`ThreadError`.

    *resolve_room* turns a matrix room alias into a room id; without it an alias
    is kept as given. Resolution matters because the id is what the connection
    is keyed by: the same room reached through an alias and through its id must
    be one connection, not two.
    """
    try:
        validate_thread_targets([permalink])
    except ValueError as exc:
        raise ThreadError(str(exc), status=400) from None
    link = str(permalink)
    if is_slack_thread_url(link):
        channel, thread_ts = parse_slack_permalink(link)
        return ThreadRef(
            channel=CHANNEL_SLACK,
            key={"channel": channel, "thread_ts": thread_ts},
            permalink=link,
            label=_slack_label(link, channel),
        )
    thread = parse_matrix_permalink(link)
    room = thread.room
    if resolve_room is not None:
        room = resolve_room(thread.room)
    return ThreadRef(
        channel=CHANNEL_MATRIX,
        key={"room": room, "event": thread.event_id},
        permalink=link,
        label=thread.room,
    )


@dataclass(frozen=True)
class ChannelRef:
    """A place to open a thread in: a Slack channel, or a matrix room.

    No ``label`` beside :attr:`target`, unlike :class:`ThreadRef`: a channel is
    named by the operator and shown back as they wrote it, and the thread that
    comes out of opening one gets its own label from the permalink.
    """

    channel: str
    #: What the transport is asked for — a Slack channel id or ``#name``, a
    #: matrix room id or alias. Passed through as the operator wrote it, so the
    #: refusal for a channel the bot cannot see is the transport's own.
    target: str


def parse_channel(spec: object) -> ChannelRef:
    """The channel or room *spec* names, or :class:`ThreadError`.

    One field for two transports, told apart by the ``:server`` half a matrix
    room id or alias carries and a Slack channel does not. Neither side is
    resolved here: whether the bot can see the channel is a question for the
    transport, and asking it needs a credential this module never has.
    """
    if not isinstance(spec, str) or not spec.strip():
        raise ThreadError(
            "name a channel to open the thread in — a Slack channel id or "
            "#name, or a matrix room id or alias",
            status=400,
        )
    text = spec.strip()
    if len(text) > MAX_CHANNEL_CHARS:
        raise ThreadError(
            f"that channel name is {len(text)} characters; the limit is "
            f"{MAX_CHANNEL_CHARS}",
            status=400,
        )
    if any(char.isspace() or ord(char) < 32 for char in text):
        raise ThreadError(
            "a channel is named without whitespace or control characters", status=400,
        )
    # A matrix room id or alias carries a ``:server`` half and a Slack channel
    # does not, which is the whole of how one field serves two transports.
    if is_matrix_room(text):
        return ChannelRef(channel=CHANNEL_MATRIX, target=text)
    if is_slack_thread_url(text) or is_matrix_thread_url(text):
        raise ThreadError(
            f"{text} is a thread permalink, not a channel — connect that thread "
            "instead of opening a new one",
            status=400,
        )
    if ":" in text or "/" in text:
        raise ThreadError(
            f"{text!r} is neither a Slack channel (an id, or #name) nor a matrix "
            "room (an id or alias, both ending in :server)",
            status=400,
        )
    return ChannelRef(channel=CHANNEL_SLACK, target=text)
