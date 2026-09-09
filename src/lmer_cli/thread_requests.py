"""Thread requests: a session asking the host side to act on a thread (#414).

Reading and posting a thread the session is not connected to, and — since issue
#429 — opening, connecting and disconnecting its own.

An RPC to the host, deliberately not the message bus. ``docs/COMMS.md`` §12 has
the shape and the reasons; the two properties this queue owes its callers are:

- **A request is handed to one servicer, once**, and nothing retries it — a post
  delivered twice is two messages in a stranger's thread.
- **"Nobody can do this" is answered, not waited out** — a session with no host
  side behind it is told so at once rather than blocking for a timeout.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from . import threads as _threads

__all__ = [
    "OP_READ", "OP_POST", "OP_OPEN", "OP_CONNECT", "OP_DISCONNECT", "OPS",
    "CONNECTION_OPS", "STATE_PENDING", "STATE_CLAIMED", "STATE_DONE",
    "SERVICER_TTL", "MAX_IN_FLIGHT", "RESULT_TTL", "MAX_READ_LIMIT",
    "DEFAULT_READ_LIMIT", "MAX_CHANNEL_CHARS", "ThreadRequestError",
    "ThreadRequest", "ThreadRequests",
]

OP_READ = "read"
OP_POST = "post"

#: The three a run asks about its *own* threads (issue #429): open a thread in a
#: channel and connect it, connect one that exists, disconnect one. They reach
#: the same queue as ``read``/``post`` because the reason is the same — the
#: credentials are on the host — but the work is the platform's rather than a
#: transport call, so a host side that keeps no connections refuses them by name.
OP_OPEN = "open"
OP_CONNECT = "connect"
OP_DISCONNECT = "disconnect"
CONNECTION_OPS = (OP_OPEN, OP_CONNECT, OP_DISCONNECT)
OPS = (OP_READ, OP_POST, *CONNECTION_OPS)

STATE_PENDING = "pending"
STATE_CLAIMED = "claimed"
STATE_DONE = "done"

#: How recently a servicer must have shown itself for one to count as attached.
#: Comfortably over three long-poll periods; see :meth:`ThreadRequests.servicer_attached`
#: for what else counts as showing itself.
SERVICER_TTL = 90.0

#: Requests one session may have outstanding. A looping agent meets a refusal it
#: can read rather than a queue that grows with the session.
MAX_IN_FLIGHT = 8

#: How long a finished request is kept for an asker that has not collected it,
#: and how long an unanswered one is left with the servicer that took it.
RESULT_TTL = 300.0

DEFAULT_READ_LIMIT = 20
MAX_READ_LIMIT = 200

#: The bus's own caps, applied here too. The permalink's bound is the parser's
#: own (:mod:`lmer_cli.threads`), not a second number that could diverge from it.
MAX_PERMALINK_CHARS = _threads.MAX_PERMALINK_CHARS
MAX_TEXT_CHARS = 16384
MAX_ATTACHMENTS = 16

#: An attachment's name is text the session chose that a thread will show, so it
#: is bounded and cleaned like every other such field.
MAX_NAME_CHARS = 200

#: A channel to open a thread in. The parser's own bound
#: (:mod:`lmer_cli.threads`), not a second number that could diverge from it.
MAX_CHANNEL_CHARS = _threads.MAX_CHANNEL_CHARS

#: A media type rides in an HTTP header and in a matrix event's ``info``; it is
#: the shape RFC 6838 allows and nothing else.
_MEDIA_TYPE_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]{1,127}/[A-Za-z0-9!#$&^_.+-]{1,127}$")

#: Finished requests kept at once for askers that never collected them:
#: :data:`RESULT_TTL` bounds their age, this bounds their number. The sweep runs
#: when the queue is touched, so what is held between two touches is this plus
#: whatever is in flight.
MAX_RETAINED_RESULTS = 16
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ThreadRequestError(RuntimeError):
    """A request the facility refuses, carrying the status the route answers."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ThreadRequest:
    id: str
    seq: int
    op: str
    permalink: str
    channel: str = ""
    limit: int = DEFAULT_READ_LIMIT
    text: str = ""
    attachments: list = field(default_factory=list)
    state: str = STATE_PENDING
    created: float = field(default_factory=time.monotonic)
    result: Optional[dict] = None

    def to_dict(self, *, include_result: bool = True) -> dict:
        payload = {
            "id": self.id,
            "seq": self.seq,
            "op": self.op,
            "permalink": self.permalink,
            "channel": self.channel,
            "limit": self.limit,
            "text": self.text,
            "attachments": list(self.attachments),
            "state": self.state,
        }
        if include_result:
            payload["result"] = self.result
        return payload


def _gone(request_id: str) -> str:
    """Why an id names nothing. 410 and not 404, which this control plane reads
    as "this supervisor predates the facility"."""
    return (
        f"thread request {request_id!r} is gone — a result is dropped once the "
        "asker collects it, and an uncollected one ages out"
    )


def _checked_attachment(item: object) -> dict:
    """One attachment, reduced to the four fields the host side reads.

    A whitelist rather than a cleaning pass: everything kept here leaves this
    process — into a thread the run does not own, or onto a host filesystem — and
    a key copied through is a key with no bound and no reader.
    """
    if not isinstance(item, dict):
        raise ThreadRequestError("each attachment is an object naming a file")
    cleaned: dict = {}
    name = item.get("name")
    if name is not None:
        if not isinstance(name, str):
            raise ThreadRequestError("an attachment's name must be text")
        cleaned["name"] = " ".join(_CONTROL_CHARS.sub("", name).split())[:MAX_NAME_CHARS]
    path = item.get("path")
    if path is not None:
        if not isinstance(path, str) or not path.strip():
            raise ThreadRequestError("an attachment's path must be text")
        if len(path) > MAX_PERMALINK_CHARS:
            raise ThreadRequestError(
                f"an attachment's path is {len(path)} characters; the limit is "
                f"{MAX_PERMALINK_CHARS}"
            )
        # A NUL makes ``os.open`` raise ValueError rather than OSError, which is
        # not what the host side's refusal path catches — so it is refused here,
        # where a refusal is what this returns.
        if any(ord(char) < 32 for char in path):
            raise ThreadRequestError("an attachment's path carries a control character")
        cleaned["path"] = path
    media_type = item.get("media_type")
    if media_type is not None:
        if not isinstance(media_type, str) or not _MEDIA_TYPE_RE.match(media_type.strip()):
            raise ThreadRequestError(f"{media_type!r} is not a media type (type/subtype)")
        cleaned["media_type"] = media_type.strip()
    size = item.get("bytes")
    if size is not None:
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ThreadRequestError("an attachment's bytes must be a non-negative integer")
        cleaned["bytes"] = size
    return cleaned


def _checked_channel(value: object) -> str:
    """Bounds only, for :data:`_checked_permalink`'s reason: which channels exist
    is a question for the transport, and this process cannot ask one."""
    if not isinstance(value, str) or not value.strip():
        raise ThreadRequestError(
            "opening a thread needs a channel — a Slack channel id or #name, or "
            "a matrix room id or alias"
        )
    channel = value.strip()
    if len(channel) > MAX_CHANNEL_CHARS:
        raise ThreadRequestError(
            f"the channel is {len(channel)} characters; the limit is {MAX_CHANNEL_CHARS}"
        )
    if any(char.isspace() or ord(char) < 32 for char in channel):
        raise ThreadRequestError("a channel carries no whitespace or control characters")
    return channel


def _checked_permalink(value: object) -> str:
    """Bounds only. Which links name a thread is the servicer's question — it
    has the transports — and the supervisor holds no credential to ask with."""
    if not isinstance(value, str) or not value.strip():
        raise ThreadRequestError("a thread request needs a permalink")
    permalink = value.strip()
    if len(permalink) > MAX_PERMALINK_CHARS:
        raise ThreadRequestError(
            f"the permalink is {len(permalink)} characters; the limit is {MAX_PERMALINK_CHARS}"
        )
    if any(char.isspace() or ord(char) < 32 for char in permalink):
        raise ThreadRequestError("a permalink carries no whitespace or control characters")
    return permalink


class ThreadRequests:
    """The queue, in memory, for the life of the supervisor."""

    def __init__(self, *, max_in_flight: int = MAX_IN_FLIGHT) -> None:
        self._cond = threading.Condition()
        self._requests: dict = {}
        self._order: list = []
        self._seq = 0
        self._servicer_seen: Optional[float] = None
        self._max_in_flight = max_in_flight

    # -- the servicer's attendance --------------------------------------------

    def servicer_seen(self) -> None:
        with self._cond:
            self._servicer_seen = time.monotonic()

    def servicer_attached(self) -> bool:
        """Whether anything on the host side is answering for this session.

        Two facts, and the second is what makes the first honest: a recent poll,
        **or** a request somebody has taken and not yet answered. A servicer
        works its claimed batch serially, so between two polls it can spend
        minutes on one thread's files — and reporting "nothing here can reach
        Slack" while it works is the one lie this fact exists to prevent. A claim
        that outlives ``RESULT_TTL`` is swept, so it cannot vouch forever.
        """
        with self._cond:
            now = time.monotonic()
            if self._servicer_seen is not None and now - self._servicer_seen <= SERVICER_TTL:
                return True
            return any(
                request.state == STATE_CLAIMED and now - request.created <= RESULT_TTL
                for request in self._requests.values()
            )

    def health(self) -> dict:
        with self._cond:
            in_flight = sum(
                1 for request in self._requests.values() if request.state != STATE_DONE
            )
        return {
            "servicer": self.servicer_attached(),
            "in_flight": in_flight,
            "seq": self._seq,
        }

    # -- the session's end -----------------------------------------------------

    def submit(
        self, op: str, permalink: str = "", *, channel: str = "",
        limit: int = DEFAULT_READ_LIMIT, text: str = "",
        attachments: Optional[list] = None,
    ) -> ThreadRequest:
        if op not in OPS:
            raise ThreadRequestError(f"unknown thread operation {op!r}")
        if op == OP_OPEN:
            # A channel *or* a permalink, never both: one names a place to make a
            # thread and the other a thread that exists, and a request carrying
            # the two says nothing about which was meant.
            channel = _checked_channel(channel)
            if permalink:
                raise ThreadRequestError(
                    "opening a thread takes a channel, not a permalink — connect "
                    "a thread that already exists instead"
                )
        else:
            permalink = _checked_permalink(permalink)
            channel = ""
        if len(text) > MAX_TEXT_CHARS:
            raise ThreadRequestError(
                f"the text is {len(text)} characters; the limit is {MAX_TEXT_CHARS}"
            )
        if len(attachments or []) > MAX_ATTACHMENTS:
            raise ThreadRequestError(
                f"at most {MAX_ATTACHMENTS} attachments, got {len(attachments or [])}"
            )
        attachments = [_checked_attachment(item) for item in (attachments or [])]
        if not self.servicer_attached():
            raise ThreadRequestError(
                "no host-side servicer is attached to this session, so nothing here "
                "can reach Slack or matrix — the credentials live on the host. Say "
                "what you need in your ordinary output instead",
                status=503,
            )
        with self._cond:
            self._sweep()
            in_flight = sum(
                1 for request in self._requests.values() if request.state != STATE_DONE
            )
            if in_flight >= self._max_in_flight:
                raise ThreadRequestError(
                    f"{in_flight} thread requests are already in flight (the limit is "
                    f"{self._max_in_flight}) — wait for one to finish",
                    status=429,
                )
            self._seq += 1
            request = ThreadRequest(
                id=uuid.uuid4().hex,
                seq=self._seq,
                op=op,
                permalink=permalink,
                channel=channel,
                limit=limit,
                text=text,
                attachments=list(attachments or []),
            )
            self._requests[request.id] = request
            self._order.append(request.id)
            self._cond.notify_all()
            return request

    def await_result(self, request_id: str, timeout: float = 0.0) -> ThreadRequest:
        """The request, blocking up to *timeout* for its result.

        A collected result is dropped — **read**, not received: a caller killed
        between this returning and its own read of the reply finds a 410. One
        loopback response wide, and an ack protocol is the alternative.
        """
        deadline = time.monotonic() + timeout if timeout > 0 else None
        with self._cond:
            while True:
                request = self._requests.get(request_id)
                if request is None:
                    raise ThreadRequestError(_gone(request_id), status=410)
                if request.state == STATE_DONE:
                    self._drop(request_id)
                    return request
                if deadline is None:
                    return request
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return request
                self._cond.wait(timeout=remaining)

    # -- the servicer's end ----------------------------------------------------

    def claim(self, since: int = 0, timeout: float = 0.0) -> tuple[list, int]:
        """Pending requests past *since*, claimed by this caller.

        Claimed, not merely read: a request handed out here is never offered
        again, because the servicer may be about to post it into a thread.
        """
        self.servicer_seen()
        deadline = time.monotonic() + timeout if timeout > 0 else None
        with self._cond:
            while True:
                self._sweep()
                fresh = [
                    self._requests[key] for key in self._order
                    if self._requests[key].state == STATE_PENDING
                    and self._requests[key].seq > since
                ]
                cursor = max([request.seq for request in fresh], default=since)
                if fresh or deadline is None:
                    for request in fresh:
                        request.state = STATE_CLAIMED
                    return fresh, cursor
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return [], since
                self._cond.wait(timeout=remaining)

    def complete(self, request_id: str, result: dict) -> ThreadRequest:
        # An answer is a servicer showing itself, exactly as a poll is: one that
        # spends two minutes on a thread of files must not be reported absent to
        # the session it is working for.
        self.servicer_seen()
        with self._cond:
            request = self._requests.get(request_id)
            if request is None:
                raise ThreadRequestError(_gone(request_id), status=410)
            request.result = dict(result)
            request.state = STATE_DONE
            request.created = time.monotonic()
            self._cond.notify_all()
            return request

    # -- housekeeping ----------------------------------------------------------

    def _sweep(self) -> None:
        """Drop what nobody is coming back for. Caller holds the lock.

        Two kinds age out, and the second is the one that matters: a request the
        servicer took and never answered — it died, or the daemon stopped
        mid-batch — is not coming back, and leaving it in flight burns one of the
        eight slots for the life of the supervisor. Its asker gave up long ago.
        """
        now = time.monotonic()
        for key in list(self._order):
            request = self._requests.get(key)
            if request is None:
                self._order.remove(key)
            elif now - request.created > RESULT_TTL:
                self._drop(key)
        # And how many finished results wait at once, not only how long each
        # waits: every `lmer-signal` killed mid-wait leaves one behind, and a
        # read's result is the thread it read.
        finished = [
            key for key in self._order
            if self._requests[key].state == STATE_DONE
        ]
        for key in finished[: max(0, len(finished) - MAX_RETAINED_RESULTS)]:
            self._drop(key)

    def _drop(self, request_id: str) -> None:
        self._requests.pop(request_id, None)
        if request_id in self._order:
            self._order.remove(request_id)
