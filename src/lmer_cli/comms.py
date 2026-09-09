"""The session's communications bus, kept by the supervisor (issue #407).

Every communication between a session and the parties that talk to it — the
agent's notes, milestones and questions; messages and answers from the
platform, Slack, matrix or a human at the terminal — is one immutable record
in a per-session log. The supervisor is the only writer. It serves the log
over its control plane (``supervisor.py`` owns the routes), relays every
inbound record into the harness once, and arbitrates questions: the first
answer to reach it wins.

The record shape is the contract connectors ship against and is documented
in ``docs/COMMS.md``; ``SCHEMA_VERSION`` moves only when a field changes
meaning. Question state is never a field on a record — it is a fold over the
log (question → open, first answer → answered, close → closed), which is what
keeps records immutable and makes "replay since" a plain read.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence

from .util import get_bool_env

__all__ = [
    "SCHEMA_VERSION", "COMMS_LOG_NAME", "COMMS_LOG_ENV", "TERMINAL_MIRROR_ENV",
    "DRAIN_GRACE_ENV", "DEFAULT_DRAIN_GRACE", "drain_grace",
    "KIND_MESSAGE", "KIND_NOTE", "KIND_MILESTONE", "KIND_QUESTION",
    "KIND_ANSWER", "KIND_CLOSE", "KIND_COMMAND", "KNOWN_KINDS", "AGENT_KINDS",
    "RELAYED_KINDS", "CHANNEL_AGENT", "CHANNEL_TERMINAL", "CHANNEL_PLATFORM",
    "CHANNEL_UBER", "CHANNEL_SLACK", "CHANNEL_MATRIX", "CHANNEL_LABELS",
    "CHANNEL_SUPERVISOR", "EVENT_SESSION_END_REQUESTED", "PLATFORM_SUBSCRIBER",
    "COMMAND_CHANNELS", "MAX_TEXT_CHARS", "MAX_OPTIONS", "MAX_OPTION_CHARS",
    "MAX_OPEN_QUESTIONS", "MAX_AGENT_ENTRIES", "ACCEPTED_NEW",
    "ACCEPTED_DUPLICATE", "ACCEPTED_LATE", "DELIVERY_CLI", "DELIVERY_PTY",
    "STATE_OPEN", "STATE_ANSWERED", "STATE_CLOSED", "CommsError", "Source",
    "Message", "QuestionState", "MessageLog", "TerminalTracker",
    "AskDirProjection", "message_id", "parse_message_ref", "option_index",
    "option_conflict", "RESERVED_META_KEYS", "relay_payload", "relay_label",
    "utc_now_iso",
]

SCHEMA_VERSION = 1

#: The ``meta`` keys the supervisor stamps itself. A caller's own copy of one is
#: dropped rather than stored, and ``docs/COMMS.md`` §1 lists exactly these.
RESERVED_META_KEYS = (
    "late", "answered_by", "delivered", "session_origin", "option",
)

#: File name inside the mounted session directory
#: (:data:`lmer_cli.supervisor.CONTAINER_SESSION_LOG_DIR`). A cross-version
#: interface like ``session.log``: the platform reads it from the host side.
COMMS_LOG_NAME = "messages.jsonl"

#: The daemon's authenticated delivery acknowledgment identity.
PLATFORM_SUBSCRIBER = "platform"

#: Exported to the wrapped harness before fork, so stdlib-only hooks can read
#: the log without knowing the mount point. Absent when nothing is mounted.
COMMS_LOG_ENV = "LMER_COMMS_LOG"

#: Kill switch for the terminal line reconstruction (§3.4 of the spec).
TERMINAL_MIRROR_ENV = "LMER_COMMS_TERMINAL_MIRROR"

#: How long the supervisor keeps its control plane up after the harness
#: exits, so a subscriber can fetch and ack the last records over HTTP. The
#: daemon receives these records over HTTP, never from a container-written file.
DRAIN_GRACE_ENV = "LMER_COMMS_DRAIN_GRACE"
DEFAULT_DRAIN_GRACE = 5.0
DRAIN_GRACE_MAX = 60.0

KIND_MESSAGE = "message"
KIND_NOTE = "note"
KIND_MILESTONE = "milestone"
KIND_QUESTION = "question"
KIND_ANSWER = "answer"
KIND_CLOSE = "close"
KIND_COMMAND = "command"

KNOWN_KINDS = frozenset({
    KIND_MESSAGE, KIND_NOTE, KIND_MILESTONE, KIND_QUESTION, KIND_ANSWER,
    KIND_CLOSE, KIND_COMMAND,
})

#: What the agent's own CLI may post. An agent never answers itself and never
#: issues a harness command through the bus.
AGENT_KINDS = frozenset({KIND_MESSAGE, KIND_NOTE, KIND_MILESTONE, KIND_QUESTION, KIND_CLOSE})

#: Inbound kinds that are typed into the harness. Notes, milestones, questions
#: and closes come from the agent and have no PTY leg.
RELAYED_KINDS = frozenset({KIND_MESSAGE, KIND_ANSWER, KIND_COMMAND})

CHANNEL_AGENT = "agent"
CHANNEL_TERMINAL = "terminal"
CHANNEL_PLATFORM = "platform"
CHANNEL_UBER = "uber-lmer"
CHANNEL_SLACK = "slack"
CHANNEL_MATRIX = "matrix"
CHANNEL_SUPERVISOR = "supervisor"

# Structured lifecycle acknowledgment; consumers never infer it from prose.
EVENT_SESSION_END_REQUESTED = "session_end_requested"

#: How a channel reads inside a source tag. A channel not listed here is shown
#: by its identifier.
CHANNEL_LABELS = {
    CHANNEL_AGENT: "agent",
    CHANNEL_TERMINAL: "terminal",
    CHANNEL_PLATFORM: "platform",
    CHANNEL_UBER: "uber lmer",
    CHANNEL_SLACK: "slack",
    CHANNEL_MATRIX: "matrix",
}

#: The only sources that may post a ``command`` record. The daemon enforces
#: this by caller; the bus enforces it by channel so a misbehaving client
#: cannot route a slash command through a human's channel.
COMMAND_CHANNELS = frozenset({CHANNEL_PLATFORM, CHANNEL_UBER})

#: Per-kind ceilings on ``text``; the ask channel's numbers where one existed.
MAX_TEXT_CHARS = {
    KIND_QUESTION: 8192,
    KIND_NOTE: 4096,
    KIND_MILESTONE: 1000,
    KIND_MESSAGE: 16384,
    KIND_ANSWER: 16384,
    KIND_CLOSE: 500,
    KIND_COMMAND: 4096,
}
MAX_OPTIONS = 12
MAX_OPTION_CHARS = 200
MAX_OPEN_QUESTIONS = 32

#: Agent-posted records per session. Inbound traffic is not counted: the cap
#: exists to make a looping agent hit a visible refusal.
MAX_AGENT_ENTRIES = 500

MAX_AUTHOR_CHARS = 200
MAX_REF_CHARS = 200
MAX_ATTACHMENTS = 16

ACCEPTED_NEW = "new"
ACCEPTED_DUPLICATE = "duplicate"
ACCEPTED_LATE = "late"

#: Bridge poll boundaries while bounding recovery when a waiting CLI dies.
CLAIM_GRACE_SECONDS = 15.0
MAX_CLAIM_SECONDS = 120.0

#: ``meta.delivered`` on an answer: handed to a CLI blocked in a wait, or
#: typed into the PTY because nothing was waiting.
DELIVERY_CLI = "cli"
DELIVERY_PTY = "pty"

STATE_OPEN = "open"
STATE_ANSWERED = "answered"
STATE_CLOSED = "closed"

#: Text a late or PTY-delivered answer is introduced with: the question's first
#: line, so the answer reads in context, clipped to this many characters.
QUESTION_HEAD_CHARS = 80

_ID_WIDTH = 6
_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_SEQ_RE = re.compile(r"^[0-9]{1,12}$")


class CommsError(ValueError):
    """A record the bus refuses: bad shape, over a cap, or naming nothing."""


def utc_now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def message_id(session: str, seq: int) -> str:
    return f"{session}/{seq:0{_ID_WIDTH}d}"


def parse_message_ref(ref: object, session: str) -> Optional[int]:
    """The sequence number a caller means by ``42``, ``000042`` or a full id.

    ``None`` when *ref* names nothing in this session — including a full id
    from another session, which must not resolve to a local record that happens
    to share the number.
    """
    if isinstance(ref, int) and not isinstance(ref, bool):
        return ref if ref > 0 else None
    if not isinstance(ref, str):
        return None
    text = ref.strip()
    if "/" in text:
        head, _, tail = text.rpartition("/")
        if head != session:
            return None
        text = tail
    if not _SEQ_RE.match(text):
        return None
    value = int(text)
    return value if value > 0 else None


@dataclass(frozen=True)
class Source:
    """Who a record came from: a channel and, when known, a person and the
    channel's own id for the message."""

    channel: str
    author: Optional[str] = None
    ref: Optional[str] = None

    @property
    def label(self) -> str:
        name = CHANNEL_LABELS.get(self.channel, self.channel)
        return f"{name} · {self.author}" if self.author else name

    def to_dict(self) -> dict:
        out: dict = {"channel": self.channel}
        if self.author:
            out["author"] = self.author
        if self.ref:
            out["ref"] = self.ref
        return out

    @classmethod
    def from_dict(cls, raw: object, *, default_channel: Optional[str] = None) -> "Source":
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise CommsError("source must be an object")
        channel = raw.get("channel", default_channel)
        if not isinstance(channel, str) or not _CHANNEL_RE.match(channel):
            raise CommsError(
                "source.channel must be a short lowercase identifier "
                "(letters, digits, dashes)"
            )
        author = _optional_text(raw.get("author"), MAX_AUTHOR_CHARS, "source.author")
        ref = _optional_text(raw.get("ref"), MAX_REF_CHARS, "source.ref")
        return cls(channel=channel, author=author, ref=ref)


@dataclass(frozen=True)
class Message:
    """One record on the bus. Immutable once written; see the module docstring."""

    id: str
    seq: int
    session: str
    at: str
    source: Source
    kind: str
    text: str
    in_reply_to: Optional[str] = None
    options: tuple = ()
    attachments: tuple = ()
    meta: Mapping = field(default_factory=dict)
    schema: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "id": self.id,
            "seq": self.seq,
            "session": self.session,
            "at": self.at,
            "source": self.source.to_dict(),
            "kind": self.kind,
            "text": self.text,
            "in_reply_to": self.in_reply_to,
            "options": list(self.options),
            "attachments": [dict(item) for item in self.attachments],
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, raw: object) -> Optional["Message"]:
        """A record read back from the log, or ``None`` when the line is not
        one — a torn write, or a shape this build cannot use."""
        if not isinstance(raw, Mapping):
            return None
        try:
            seq = int(raw["seq"])
            source = Source.from_dict(raw.get("source"))
            kind = raw["kind"]
            text = raw["text"]
            session = raw["session"]
        except (KeyError, TypeError, ValueError, CommsError):
            return None
        if not isinstance(kind, str) or not isinstance(text, str) or not isinstance(session, str):
            return None
        if seq <= 0:
            return None
        options = raw.get("options") or ()
        attachments = raw.get("attachments") or ()
        meta = raw.get("meta") or {}
        in_reply_to = raw.get("in_reply_to")
        at = raw.get("at")
        schema = raw.get("schema")
        return cls(
            id=str(raw.get("id") or message_id(session, seq)),
            seq=seq,
            session=session,
            at=at if isinstance(at, str) else "",
            source=source,
            kind=kind,
            text=text,
            in_reply_to=in_reply_to if isinstance(in_reply_to, str) else None,
            options=tuple(str(item) for item in options) if isinstance(options, (list, tuple)) else (),
            attachments=tuple(dict(item) for item in attachments if isinstance(item, Mapping))
            if isinstance(attachments, (list, tuple)) else (),
            meta=dict(meta) if isinstance(meta, Mapping) else {},
            schema=schema if isinstance(schema, int) else SCHEMA_VERSION,
        )


@dataclass
class QuestionState:
    """The fold for one question."""

    question: Message
    state: str = STATE_OPEN
    answered_by: Optional[str] = None
    closed_by: Optional[str] = None

    @property
    def open(self) -> bool:
        return self.state == STATE_OPEN

    def to_dict(self) -> dict:
        return {
            "question": self.question.to_dict(),
            "state": self.state,
            "answered_by": self.answered_by,
            "closed_by": self.closed_by,
        }


def _optional_text(value: object, limit: int, label: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CommsError(f"{label} must be text")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > limit:
        raise CommsError(f"{label} is {len(cleaned)} characters, over the {limit} limit")
    return cleaned


def _clean_text(value: object, kind: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise CommsError("text must be a string")
    cleaned = value.strip()
    limit = MAX_TEXT_CHARS.get(kind, MAX_TEXT_CHARS[KIND_MESSAGE])
    if not cleaned and kind != KIND_CLOSE and not allow_empty:
        raise CommsError("text is empty")
    if len(cleaned) > limit:
        raise CommsError(
            f"text is {len(cleaned)} characters, over the {limit} limit for a "
            f"{kind} — say the short version and leave the detail where it is"
        )
    return cleaned


def option_index(value: object) -> Optional[int]:
    """A posted ``option`` as a 1-based index, or ``None``. Shape only: the
    range belongs to whoever holds the question's own options."""
    if value is None:
        return None
    # `bool` is an `int`, and `True` would silently mean the first option.
    if isinstance(value, bool) or not isinstance(value, int):
        raise CommsError("option must be a whole number")
    return value


def _clean_option(option: object, kind: str) -> Optional[int]:
    if option is not None and kind != KIND_ANSWER:
        raise CommsError(f"option names an answer's choice, and this is a {kind}")
    return option_index(option)


def _clean_options(options: object) -> tuple:
    if not options:
        return ()
    if not isinstance(options, (list, tuple)):
        raise CommsError("options must be a list of strings")
    cleaned = []
    for option in options:
        if not isinstance(option, str) or not option.strip():
            raise CommsError("every option must be non-empty text")
        if len(option.strip()) > MAX_OPTION_CHARS:
            raise CommsError(f"an option is over the {MAX_OPTION_CHARS} character limit")
        cleaned.append(option.strip())
    if len(cleaned) > MAX_OPTIONS:
        raise CommsError(f"{len(cleaned)} options is over the {MAX_OPTIONS} limit")
    return tuple(cleaned)


def _clean_attachments(attachments: object) -> tuple:
    if not attachments:
        return ()
    if not isinstance(attachments, (list, tuple)):
        raise CommsError("attachments must be a list of objects")
    if len(attachments) > MAX_ATTACHMENTS:
        raise CommsError(f"{len(attachments)} attachments is over the {MAX_ATTACHMENTS} limit")
    cleaned = []
    for item in attachments:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            raise CommsError("every attachment needs a string 'name'")
        cleaned.append({str(key): value for key, value in item.items()})
    return tuple(cleaned)


def _clean_meta(meta: object) -> dict:
    if meta is None:
        return {}
    if not isinstance(meta, Mapping):
        raise CommsError("meta must be an object")
    return {str(key): value for key, value in meta.items()}


def _option_named(text: str, options: Iterable[str]) -> Optional[str]:
    """The option whose own text *text* is, case-insensitively, or ``None``.

    Naming an option by its text is the one form every door shares. A number is
    NOT read as an ordinal here: only a channel that rendered a numbered list
    knows a number was offered, and it says so with the ``option`` index.
    """
    for option in options:
        if option.strip().casefold() == text.strip().casefold():
            return option
    return None


def option_conflict(text: str, option: int, options: Sequence[str]) -> Optional[str]:
    """Why *option* cannot name this answer's choice, or ``None`` if it can.

    One predicate for the three doors that have to agree: the bus raises it, the
    ask-directory lift drops an index it would raise on rather than losing the
    answer, and the platform's file leg refuses before it writes.

    Text is compared against the named option itself rather than resolved
    against the whole list, so two options colliding under ``casefold`` cannot
    turn a byte-exact pairing into a contradiction.
    """
    if not 1 <= option <= len(options):
        return f"option {option} is not one of the {len(options)} the question offers"
    body = (text or "").strip()
    if body and body.casefold() != options[option - 1].strip().casefold():
        return (
            "an answer names its option by index or in its text, not both — "
            f"drop one of option={option} and {body!r}"
        )
    return None


class MessageLog:
    """The per-session log: an append-only JSONL file plus the in-memory
    index, the question fold and the long-poll condition.

    *path* ``None`` keeps the bus in memory only — a laptop ``lmer`` run with
    nothing mounted. Otherwise the file is opened ``O_APPEND`` and every record
    is written before it is published, so a reader of the file never leads a
    reader of the API. A write failure abandons the file (and says so on
    stderr) rather than freezing the session's comms.
    """

    def __init__(
        self,
        session: str,
        path: Optional[str] = None,
        *,
        clock: Callable[[], str] = utc_now_iso,
        agent_channel: str = CHANNEL_AGENT,
    ) -> None:
        self.session = session
        self.agent_channel = agent_channel
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._messages: list[Message] = []
        self._refs: dict[tuple, Message] = {}
        self._questions: dict[str, QuestionState] = {}
        self._agent_entries = 0
        self._waiters: dict[str, int] = {}
        #: question id → monotonic expiry of a CLI's claim on its answer
        self._claims: dict[str, float] = {}
        #: question id → answer id stamped for a CLI that has not read it yet
        self._unclaimed: dict[str, str] = {}
        self._subscribers: dict[str, int] = {}
        self._acknowledged: dict[str, int] = {}
        self._listeners: list[Callable[[Message, str], None]] = []
        self._fd: Optional[int] = None
        if path is not None:
            self._open(path, truncate_to=self._load(path))

    # -- persistence ---------------------------------------------------------

    def _load(self, path: str) -> int:
        """Index the usable prefix; return where it ends, in bytes.

        The return value is what the caller truncates to. A torn write is a
        fragment with no trailing newline, so an append would glue the next
        record onto it and make *that* record unparseable too: dropping the
        tear from the index is not enough, it has to leave the file, because
        the file is what every other reader has (``hooks/signal_guard.py``
        reads it without importing any of this).
        """
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except FileNotFoundError:
            return 0
        except OSError as exc:
            sys.stderr.write(f"lmer-supervisor: cannot read the comms log {path!r}: {exc}\n")
            return 0
        offset = 0
        for chunk in raw.splitlines(keepends=True):
            line = chunk.decode("utf-8", "replace").strip()
            if not line:
                offset += len(chunk)
                continue
            try:
                record = json.loads(line)
            except ValueError:
                break
            message = Message.from_dict(record)
            if message is None or message.seq != len(self._messages) + 1:
                # A torn or out-of-order tail ends the usable prefix; nothing
                # after it can be numbered safely.
                break
            if not chunk.endswith(b"\n"):
                break  # a last line still being written is not a record yet
            self._index(message)
            offset += len(chunk)
        return offset

    def _open(self, path: str, truncate_to: Optional[int] = None) -> None:
        try:
            self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            if truncate_to is not None:
                size = os.fstat(self._fd).st_size
                if size > truncate_to:
                    os.ftruncate(self._fd, truncate_to)
        except OSError as exc:
            sys.stderr.write(
                f"lmer-supervisor: cannot open the comms log {path!r}: {exc}; "
                "this session's comms are kept in memory only\n"
            )
            self._fd = None
            self.path = None

    def _write(self, message: Message) -> None:
        if self._fd is None:
            return
        data = (json.dumps(message.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")
        view = memoryview(data)
        try:
            while view:
                count = os.write(self._fd, view)
                if count <= 0:
                    raise OSError("short write")
                view = view[count:]
        except OSError as exc:
            sys.stderr.write(
                f"lmer-supervisor: cannot write the comms log {self.path!r}: {exc}; "
                "abandoning the file, the bus continues in memory\n"
            )
            self.close()
            self.path = None

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    # -- indexing --------------------------------------------------------------

    def _index(self, message: Message) -> None:
        self._messages.append(message)
        if message.source.ref:
            self._refs.setdefault((message.source.channel, message.source.ref), message)
        if message.source.channel == CHANNEL_AGENT or message.meta.get("session_origin"):
            self._agent_entries += 1
        if message.kind == KIND_QUESTION:
            self._questions[message.id] = QuestionState(question=message)
        elif message.kind == KIND_ANSWER and message.in_reply_to in self._questions:
            state = self._questions[message.in_reply_to]
            if state.open and not message.meta.get("late"):
                state.state = STATE_ANSWERED
                state.answered_by = message.id
        elif message.kind == KIND_CLOSE and message.in_reply_to in self._questions:
            state = self._questions[message.in_reply_to]
            if state.open:
                state.state = STATE_CLOSED
                state.closed_by = message.id

    # -- writing ---------------------------------------------------------------

    def add_listener(self, listener: Callable[[Message, str], None]) -> None:
        """Called after every published record, outside the lock, with the
        record and how it was accepted. Exceptions are reported, not raised."""
        self._listeners.append(listener)

    def append(
        self,
        *,
        kind: str,
        text: str,
        source: Source,
        in_reply_to: Optional[str] = None,
        option: object = None,
        options: object = (),
        attachments: object = (),
        meta: object = None,
        session_caller: bool = False,
    ) -> tuple[Message, str]:
        """Validate, arbitrate, persist and publish one record.

        Returns the record and one of :data:`ACCEPTED_NEW`,
        :data:`ACCEPTED_DUPLICATE` (an existing record with the same
        ``(channel, ref)``; nothing appended) or :data:`ACCEPTED_LATE` (an
        answer to a question no longer open; stored and published, but never
        handed to a waiter).

        ``session_caller`` comes from authentication, never the posted body.
        Only that caller's records take the host-assigned session identity.

        *option* is a 1-based index into the answered question's ``options``,
        for a channel that showed the human a numbered list. It names the
        option; it is not parsed out of *text*, so free text is never turned
        into a choice.
        """
        if not isinstance(kind, str) or not kind:
            raise CommsError("kind is required")
        if source.channel == CHANNEL_AGENT and kind not in AGENT_KINDS:
            raise CommsError(f"the agent may post {sorted(AGENT_KINDS)}, not {kind!r}")
        if kind == KIND_COMMAND and source.channel not in COMMAND_CHANNELS:
            raise CommsError(f"a command may only come from {sorted(COMMAND_CHANNELS)}")
        picked = _clean_option(option, kind)
        # An answer that names its option by index carries no words of its own.
        body = _clean_text(text, kind, allow_empty=picked is not None)
        choices = _clean_options(options) if kind == KIND_QUESTION else ()
        files = _clean_attachments(attachments)
        extra = _clean_meta(meta)
        for reserved in RESERVED_META_KEYS:
            extra.pop(reserved, None)

        with self._cond:
            if source.ref:
                existing = self._refs.get((
                    self.agent_channel if session_caller and source.channel == CHANNEL_AGENT else source.channel,
                    source.ref,
                ))
                if existing is not None:
                    return existing, ACCEPTED_DUPLICATE

            reply_to: Optional[str] = None
            accepted = ACCEPTED_NEW
            if kind in (KIND_ANSWER, KIND_CLOSE):
                seq = parse_message_ref(in_reply_to, self.session)
                target = message_id(self.session, seq) if seq else None
                state = self._questions.get(target) if target else None
                if state is None:
                    raise CommsError(f"in_reply_to {in_reply_to!r} names no question on this session")
                reply_to = target
                if kind == KIND_ANSWER:
                    offered = state.question.options
                    if picked is not None:
                        conflict = option_conflict(body, picked, offered)
                        if conflict is not None:
                            raise CommsError(conflict)
                        body = offered[picked - 1]
                        extra["option"] = picked
                    else:
                        named = _option_named(body, offered)
                        if named is not None:
                            body = named
                    if not body:  # the postcondition of `allow_empty` above
                        raise CommsError("text is empty")
                    if state.open:
                        claimed = self._claim_live(target)
                        extra["delivered"] = DELIVERY_CLI if claimed else DELIVERY_PTY
                    else:
                        accepted = ACCEPTED_LATE
                        extra["late"] = True
                        extra["answered_by"] = state.answered_by or state.closed_by
                        extra["delivered"] = DELIVERY_PTY
                elif not state.open:
                    raise CommsError(f"question {target} is already {state.state}")
            elif in_reply_to is not None:
                seq = parse_message_ref(in_reply_to, self.session)
                if not seq or seq > len(self._messages):
                    raise CommsError(f"in_reply_to {in_reply_to!r} names no record on this session")
                reply_to = message_id(self.session, seq)

            if source.channel == CHANNEL_AGENT:
                if self._agent_entries >= MAX_AGENT_ENTRIES:
                    raise CommsError(
                        f"this session already posted {self._agent_entries} records, "
                        f"which is the limit ({MAX_AGENT_ENTRIES}) — a session posting "
                        "this much is looping"
                    )
                if kind == KIND_QUESTION:
                    open_count = sum(1 for state in self._questions.values() if state.open)
                    if open_count >= MAX_OPEN_QUESTIONS:
                        raise CommsError(
                            f"{open_count} questions are still open, which is the limit "
                            f"({MAX_OPEN_QUESTIONS}) — wait for an answer or close the "
                            "ones you stopped waiting for"
                        )

            if session_caller and source.channel == CHANNEL_AGENT and self.agent_channel != CHANNEL_AGENT:
                extra["session_origin"] = True
                source = Source(self.agent_channel, ref=source.ref)

            seq = len(self._messages) + 1
            message = Message(
                id=message_id(self.session, seq),
                seq=seq,
                session=self.session,
                at=self._clock(),
                source=source,
                kind=kind,
                text=body,
                in_reply_to=reply_to,
                options=choices,
                attachments=files,
                meta=extra,
            )
            self._write(message)
            self._index(message)
            if extra.get("delivered") == DELIVERY_CLI:
                # Delivery is a promise until the waiter has actually read it;
                # `lapsed_answers` is what keeps the promise if it never does.
                self._unclaimed[reply_to] = message.id
            self._cond.notify_all()

        for listener in list(self._listeners):
            try:
                listener(message, accepted)
            except Exception as exc:  # a projection must never take the bus down
                sys.stderr.write(f"lmer-supervisor: comms listener failed: {exc}\n")
        return message, accepted

    # -- reading ---------------------------------------------------------------

    @property
    def seq(self) -> int:
        with self._lock:
            return len(self._messages)

    def get(self, seq: int) -> Optional[Message]:
        with self._lock:
            if 1 <= seq <= len(self._messages):
                return self._messages[seq - 1]
            return None

    def question_of(self, message: Message) -> Optional[Message]:
        """The question a reply names, if any."""
        if not message.in_reply_to:
            return None
        with self._lock:
            state = self._questions.get(message.in_reply_to)
            return state.question if state else None

    def claim(self, question_id: str, *, ttl: float) -> None:
        """Hold the answer to *question_id* for a waiting CLI for *ttl* seconds.

        Taken when the question is asked, not when the first poll arrives: the
        gap between the two is real (the ask returns, the CLI checks for an
        existing answer, then polls), and an answer landing in it used to be
        typed into the harness *and* returned on stdout.
        """
        with self._cond:
            self._claims[question_id] = time.monotonic() + min(MAX_CLAIM_SECONDS, max(0.0, ttl))

    def release(self, question_id: str) -> None:
        """Give up a claim: the CLI is no longer waiting."""
        with self._cond:
            self._claims.pop(question_id, None)

    def _claim_live(self, question_id: str) -> bool:
        expiry = self._claims.get(question_id)
        if expiry is None:
            return False
        if expiry <= time.monotonic():
            self._claims.pop(question_id, None)
            return False
        return True

    def lapsed_answers(self) -> list[Message]:
        """Answers promised to a CLI that never read them, once its claim has
        lapsed. The caller types them into the harness: §7 says an answer
        reaches the agent one way or the other, and a CLI killed mid-wait —
        the ordinary end of a harness command that runs long — must not be the
        way an operator's answer disappears.
        """
        with self._cond:
            lapsed = []
            for question_id, answer_id in list(self._unclaimed.items()):
                if self._claim_live(question_id):
                    continue
                self._unclaimed.pop(question_id, None)
                seq = parse_message_ref(answer_id, self.session)
                if seq and seq <= len(self._messages):
                    lapsed.append(self._messages[seq - 1])
            return lapsed

    def read_since(
        self,
        since: int,
        *,
        timeout: float = 0.0,
        kinds: Optional[Iterable[str]] = None,
        waiter: Optional[str] = None,
    ) -> tuple[list[Message], int]:
        """Records past *since*, blocking up to *timeout* for the next one.

        *waiter* names a question the caller is blocked on: the poll refreshes
        that question's claim (so it survives the gaps between polls), and an
        answer this call returns is the CLI taking delivery of it. Returns
        ``(messages, cursor)`` where the cursor is the last sequence number the
        caller has now seen.
        """
        wanted = frozenset(kinds) if kinds else None
        deadline = time.monotonic() + timeout if timeout > 0 else None
        with self._cond:
            if waiter:
                self._waiters[waiter] = self._waiters.get(waiter, 0) + 1
                self._claims[waiter] = max(
                    self._claims.get(waiter, 0.0),
                    time.monotonic() + max(0.0, timeout) + CLAIM_GRACE_SECONDS,
                )
            try:
                while True:
                    fresh = self._messages[since:] if since < len(self._messages) else []
                    if wanted is not None:
                        fresh = [m for m in fresh if m.kind in wanted]
                    if fresh or deadline is None:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(timeout=remaining)
            finally:
                if waiter:
                    left = self._waiters.get(waiter, 0) - 1
                    if left <= 0:
                        self._waiters.pop(waiter, None)
                    else:
                        self._waiters[waiter] = left
            if waiter and self._unclaimed.get(waiter):
                # Handing the answer to the caller *is* the delivery this claim
                # promised, so nothing else has to happen for it.
                if any(m.id == self._unclaimed[waiter] for m in fresh):
                    self._unclaimed.pop(waiter, None)
                    self._claims.pop(waiter, None)
            return list(fresh), len(self._messages)

    def questions(self, *, open_only: bool = False) -> list[QuestionState]:
        with self._lock:
            states = list(self._questions.values())
        if open_only:
            states = [state for state in states if state.open]
        return states

    def question(self, ref: object) -> Optional[QuestionState]:
        seq = parse_message_ref(ref, self.session)
        if not seq:
            return None
        with self._lock:
            return self._questions.get(message_id(self.session, seq))

    def ack(self, subscriber: str, seq: int, *, final: bool = False) -> None:
        """Record how far *subscriber* has taken delivery.

        An ack of ``0`` is how a subscriber registers before it holds anything:
        the drain (:meth:`drained`) waits for the subscribers it knows about, and
        a session whose only record is its last one — a `lmer-signal "done"`
        milestone — had none, so ``all([])`` ended the wait immediately and the
        control plane went with the record still in the container.

        *final* is the subscriber leaving: stop waiting for it.
        """
        if not isinstance(subscriber, str) or not subscriber.strip():
            raise CommsError("subscriber is required")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise CommsError("seq must be a non-negative integer")
        with self._cond:
            self._acknowledged[subscriber] = max(self._acknowledged.get(subscriber, 0), seq)
            if final:
                self._subscribers.pop(subscriber, None)
            else:
                current = self._subscribers.get(subscriber, 0)
                self._subscribers[subscriber] = max(current, seq)
            self._cond.notify_all()

    def drained(self) -> bool:
        """Whether every subscriber that ever acked stands at the last seq.

        True with no subscribers at all: nobody is reading, so there is nothing
        to wait for.
        """
        with self._lock:
            last = len(self._messages)
            return all(seq >= last for seq in self._subscribers.values())

    def _wait_until(self, predicate: Callable[[], bool], timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                return False
            try:
                if predicate():
                    return True
            finally:
                self._lock.release()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # Condition.wait reacquires its lock without a deadline. A stalled
            # publisher must not defeat the timeout while that lock is held.
            time.sleep(min(.01, remaining))

    def wait_drained(self, timeout: float) -> bool:
        """Wait up to *timeout* seconds for an observed drained state."""
        return self._wait_until(
            lambda: all(seq >= len(self._messages) for seq in self._subscribers.values()), timeout,
        )

    def wait_acknowledged(self, subscriber: str, seq: int, timeout: float) -> bool:
        """Wait for a specific delivered record, including a departing subscriber's ack.

        Registration alone, no subscribers, or a different subscriber cannot
        satisfy this handoff. Only the authenticated control route accepts acks.
        """
        return self._wait_until(lambda: self._acknowledged.get(subscriber, 0) >= seq, timeout)

    def subscribers(self) -> dict:
        with self._lock:
            return dict(self._subscribers)

    def health(self) -> dict:
        with self._lock:
            return {
                "schema": SCHEMA_VERSION,
                # A connector and a thread servicer both ask here whose session
                # this is (#427): the supervisor is the party that vouches for
                # the id, so neither is told it by a launch argument.
                "session": self.session,
                "seq": len(self._messages),
                "log": self.path,
                "open_questions": sum(1 for state in self._questions.values() if state.open),
                "subscribers": dict(self._subscribers),
            }


# ---------------------------------------------------------------------------
# Relay into the harness
# ---------------------------------------------------------------------------


def relay_label(message: Message, question: Optional[Message] = None) -> str:
    """The bracketed tag an inbound record is introduced with."""
    label = message.source.label
    if message.kind != KIND_ANSWER or question is None:
        return f"[{label}]"
    head = question.text.strip().splitlines()[0] if question.text.strip() else ""
    if len(head) > QUESTION_HEAD_CHARS:
        head = head[: QUESTION_HEAD_CHARS - 1] + "…"
    if message.meta.get("late"):
        winner = message.meta.get("answered_by") or "another channel"
        return f"[{label}] (late answer to #{question.seq} {head!r}, already answered by {winner})"
    return f"[{label}] (answer to #{question.seq} {head!r})"


def relay_payload(message: Message, question: Optional[Message] = None) -> tuple[str, bool]:
    """What to type into the harness for an inbound record: ``(text, tagged)``.

    A ``command`` goes in untagged so the harness reads it as the slash command
    it is. Everything else is prefixed with its source tag, which also takes
    the first column away from the harness's command escapes.
    """
    if message.kind == KIND_COMMAND:
        return message.text, False
    return f"{relay_label(message, question)} {message.text}", True


# ---------------------------------------------------------------------------
# Terminal line reconstruction
# ---------------------------------------------------------------------------

_PASTE_START = b"\x1b[200~"
_PASTE_END = b"\x1b[201~"

#: A line whose first character is one of these is the human driving the TUI
#: (a slash command, a shell escape, a memory note), not talking to the room.
TERMINAL_SKIP_FIRST = frozenset("/!#")


class TerminalTracker:
    """Rebuild submitted lines from a raw keystroke stream.

    Best effort by construction — see the spec's §3.4 for what it cannot see
    (cursor movement, tab completion, TUI-side edits). It reads bytes as a
    terminal would: printable bytes accumulate, backspace removes the last
    character, escape sequences are dropped, bracketed paste is taken
    verbatim, Ctrl-C and Ctrl-U clear the line, and Enter emits it.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._escape = bytearray()
        self._in_paste = False

    def feed(self, data: bytes) -> list[str]:
        """Consume *data*; return the lines completed by it, in order."""
        lines: list[str] = []
        for byte in data:
            if self._escape:
                self._feed_escape(byte, lines)
                continue
            if self._in_paste:
                if byte == 0x1B:
                    self._escape.append(byte)
                else:
                    self._buffer.append(byte)
                continue
            if byte == 0x1B:
                self._escape.append(byte)
            elif byte in (0x0D, 0x0A):
                self._emit(lines)
            elif byte in (0x7F, 0x08):
                self._backspace()
            elif byte in (0x03, 0x15):
                self._buffer.clear()
            elif byte < 0x20:
                continue
            else:
                self._buffer.append(byte)
        return lines

    def _feed_escape(self, byte: int, lines: list[str]) -> None:
        self._escape.append(byte)
        if len(self._escape) == 2:
            if byte in (0x5B, 0x4F):  # '[' or 'O': a CSI / SS3 sequence follows
                return
            # ESC + one other byte (an alt-chord) — dropped; a bare Escape key
            # arriving in the same read clears the line like the TUI does.
            self._escape.clear()
            if not self._in_paste:
                self._buffer.clear()
            return
        if 0x40 <= byte <= 0x7E:
            sequence = bytes(self._escape)
            self._escape.clear()
            if sequence == _PASTE_START:
                self._in_paste = True
            elif sequence == _PASTE_END:
                self._in_paste = False
            return
        if len(self._escape) > 32:  # not a sequence after all; stop swallowing
            self._escape.clear()

    def _backspace(self) -> None:
        if not self._buffer:
            return
        # Drop one UTF-8 character: continuation bytes, then the lead byte.
        self._buffer.pop()
        while self._buffer and 0x80 <= self._buffer[-1] < 0xC0:
            self._buffer.pop()

    def _emit(self, lines: list[str]) -> None:
        # Inside a paste the terminal delivers Enter as CR; as text it is a
        # newline.
        text = (
            self._buffer.decode("utf-8", errors="replace")
            .replace("\r\n", "\n").replace("\r", "\n").strip()
        )
        self._buffer.clear()
        if not text or text[0] in TERMINAL_SKIP_FIRST:
            return
        lines.append(text)


# ---------------------------------------------------------------------------
# Ask-directory projection (one-release compatibility, spec §8.2)
# ---------------------------------------------------------------------------


#: What a lifted record's ``source.ref`` starts with.
ASK_DIR_REF_PREFIX = "ask-dir:"

#: The shape of the file name behind that prefix. Strict so that a malformed ref
#: cannot raise where it is read — :class:`AskDirProjection`'s constructor, which
#: runs before the fork, on every start (docs/COMMS.md §10).
_ASK_DIR_FILE_RE = re.compile(r"^([0-9]{1,12})\.[a-z]+\.json$")


def ask_dir_ref_file(ref: object) -> Optional[str]:
    """The ask-directory file name *ref* names, or ``None`` if it names none."""
    if not isinstance(ref, str) or not ref.startswith(ASK_DIR_REF_PREFIX):
        return None
    name = ref[len(ASK_DIR_REF_PREFIX):]
    return name if _ASK_DIR_FILE_RE.match(name) else None


def ask_dir_ref_stem(ref: object) -> Optional[str]:
    """The ask-directory entry id *ref* names, or ``None``.

    The one parser for this format: the projection and
    :mod:`lmer_platform.daemon` both read it, and two parsers for one wire
    format is how they come to disagree about what is on the log.
    """
    name = ask_dir_ref_file(ref)
    return name.split(".", 1)[0] if name else None


class AskDirProjection:
    """Mirror the bus into the ask channel's file shapes, and lift what is
    filed there by something else onto the bus.

    A record it mirrors is filed under that record's own ``seq``, and a reply
    under the entry id of the question it names — so an answer an old daemon
    writes under a ``NNNNNN`` pairs with the question filed there.

    Two writers share the directory during the compatibility window, and the
    lift is what makes the second one visible (issue #421): a session whose
    in-tree CLI predates the bus files its questions, notes, milestones,
    answers and closes here and nowhere else, so without this the daemon sees
    a bus session that never says anything.

    **An entry id is not a ``seq``.** The other writer numbers a new entry one
    past the highest id in the directory (``ask_channel.protocol._next_seq``);
    the bus numbers records, including inbound ones and lifts that file nothing.
    Neither counter leads: a session using only the bus is at ``seq`` 3 with 4
    as the next id, and one record behind a lift is at ``seq`` 2 with 2. So
    nothing here converts one into the other, in either direction.

    **Which writer filed a record is stamped, never inferred.** A file this
    projection wrote carries ``protocol.WRITER_KEY``; a record it lifted keeps
    the file's name in ``source.ref``. The two writers can otherwise produce a
    file with the same id and the same words, which is what makes a guess from
    the number and the text wrong rather than merely imprecise. The id a lifted
    question came from is also where its answer has to be filed, because that is
    the name its asker is polling.

    Nothing is lifted twice: the mark keeps this projection's own files out, and
    across a restart ``source.ref`` keeps the lifts out, because the log is
    reloaded and :meth:`MessageLog.append` refuses a repeated ``(channel, ref)``.
    """

    def __init__(self, directory: str, log: MessageLog) -> None:
        from ask_channel import protocol

        self._protocol = protocol
        self._kinds = {
            protocol.QUESTION_SUFFIX: KIND_QUESTION,
            protocol.NOTE_SUFFIX: KIND_NOTE,
            protocol.SIGNAL_SUFFIX: KIND_MILESTONE,
            protocol.ANSWER_SUFFIX: KIND_ANSWER,
            protocol.CLOSED_SUFFIX: KIND_CLOSE,
        }
        #: kind → the suffix :meth:`mirror` files it under; the inverse of
        #: ``_kinds``, so the two can never name different files for one kind.
        self._suffixes = {kind: suffix for suffix, kind in self._kinds.items()}
        self.directory = Path(directory)
        self._log = log
        self._lifted: set[str] = set()
        #: ask-dir stem → the id of the question lifted from it, and back.
        self._question_by_stem: dict[str, str] = {}
        self._stem_by_question: dict[str, str] = {}
        self._lock = threading.Lock()
        self._recover_lifts()

    # -- startup ---------------------------------------------------------------

    def _recover_lifts(self) -> None:
        """Re-learn which files this projection already lifted, from the log.

        ``_lifted`` is memory and the directory is not: a supervisor rebuilt over
        a populated mount (§3.1 — a second supervisor in one container, a resumed
        session) starts with it empty. The ref each lifted record carries says
        which file it came out of, and the question aliases are rebuilt from the
        same pass, in log order, so a lifted question is known before the answer
        that names it.

        Its own mirrored files need no recovering: they carry the writer mark,
        so :meth:`_is_projected` recognises them with no memory at all.
        """
        for message in self._log.read_since(0)[0]:
            lifted_from = ask_dir_ref_file(message.source.ref)
            if lifted_from is None:
                continue
            self._lifted.add(lifted_from)
            if message.kind == KIND_QUESTION:
                self._remember_lift(lifted_from.split(".", 1)[0], message)

    def _is_projected(self, name: str) -> bool:
        """Whether the file at *name* was filed by this projection rather than by
        the session's own CLI — read off the record, never guessed at."""
        record = self._protocol._read_json(self.directory / name)
        return (
            isinstance(record, dict)
            and record.get(self._protocol.WRITER_KEY) == self._protocol.BUS_WRITER
        )

    def _mirror_name(self, message: Message) -> Optional[str]:
        """The file :meth:`mirror` files *message* under, or ``None`` for a
        record it files nothing for.

        A reply goes under its question's entry id, which is the question's
        ``seq`` only when the question was born on the bus.
        """
        suffix = self._suffixes.get(message.kind)
        if suffix is None:
            return None
        if message.kind in self._REPLY_KINDS:
            stem, _ = self._question_stem(message)
            return None if stem is None else f"{stem}{suffix}"
        return f"{message.seq:06d}{suffix}"

    def _entry_of(self, name: str) -> Optional[tuple[str, str]]:
        """``(entry id, kind)`` for a file name this projection can classify."""
        match = _ASK_DIR_FILE_RE.match(name)
        if match is None:
            return None
        kind = self._kinds.get(name[match.end(1):])
        return (match.group(1), kind) if kind is not None else None

    def _remember_lift(self, stem: str, question: Message) -> None:
        self._question_by_stem[stem] = question.id
        self._stem_by_question[question.id] = stem

    # -- bus → files -----------------------------------------------------------

    def mirror(self, message: Message, accepted: str) -> None:
        if accepted == ACCEPTED_DUPLICATE:
            return
        protocol = self._protocol
        lifted = ask_dir_ref_file(message.source.ref) is not None
        name = self._mirror_name(message)
        if name is None:
            return
        stem = name.split(".", 1)[0]
        if message.kind == KIND_QUESTION:
            if lifted:
                return  # the file it came out of is the record
            self._write(name, {
                "schema": protocol.SCHEMA_VERSION, "id": stem, "kind": protocol.KIND_QUESTION,
                "text": message.text, "options": list(message.options), "at": message.at,
                "nonce": "",
            })
        elif message.kind == KIND_NOTE:
            if lifted:
                return
            self._write(name, {
                "schema": protocol.SCHEMA_VERSION, "id": stem, "kind": protocol.KIND_NOTE,
                "text": message.text, "at": message.at,
            })
        elif message.kind == KIND_MILESTONE:
            if lifted:
                return
            self._write(name, {
                "schema": protocol.SCHEMA_VERSION, "id": stem, "kind": protocol.KIND_SIGNAL,
                "text": message.text, "at": message.at,
            })
        elif message.kind == KIND_ANSWER and accepted == ACCEPTED_NEW:
            _, old_path = self._question_stem(message)
            if old_path and lifted:
                # An answer lifted out of the file it would be written to.
                return
            self._write(name, {
                "schema": protocol.SCHEMA_VERSION, "kind": "answer", "question_id": stem,
                "nonce": self._nonce_of(stem) if old_path else "",
                "text": message.text, "answered_at": message.at,
                "source": f"{message.source.channel}:{message.source.author or ''}".rstrip(":"),
            })
            if old_path:
                # The asker is polling the file rather than the bus, so delivery
                # is its read, and it files the receipt itself when it takes the
                # answer. One written here would say delivered before it was.
                return
            # Delivery is guaranteed by the bus (CLI or PTY), so the receipt is
            # filed with the answer; old readers then never see "unread".
            self._write(f"{stem}{protocol.READ_SUFFIX}", {
                "schema": protocol.SCHEMA_VERSION, "kind": "read", "question_id": stem,
                "nonce": "", "read_at": message.at, "via": "bus",
            })
        elif message.kind == KIND_CLOSE:
            if lifted:
                return
            self._write(name, {
                "schema": protocol.SCHEMA_VERSION, "kind": "closed", "question_id": stem,
                "nonce": "", "closed_at": message.at, "reason": message.text,
            })

    def _question_stem(self, message: Message) -> tuple[Optional[str], bool]:
        """``(stem, from the old path)`` for the question *message* replies to.

        A question this projection lifted keeps the stem it was filed under —
        the bus gave it a seq of its own, and filing the reply under *that*
        would put it where nothing is looking.
        """
        with self._lock:
            stem = self._stem_by_question.get(message.in_reply_to or "")
        if stem is not None:
            return stem, True
        qseq = parse_message_ref(message.in_reply_to, message.session)
        return (f"{qseq:06d}" if qseq is not None else None), False

    def _nonce_of(self, stem: str) -> str:
        """The nonce on the question file at *stem*, which its answer must carry
        for the old reader to accept the pairing (``ask_channel.protocol``)."""
        record = self._protocol._read_json(
            self.directory / f"{stem}{self._protocol.QUESTION_SUFFIX}"
        )
        nonce = record.get("nonce") if isinstance(record, dict) else None
        return nonce if isinstance(nonce, str) else ""

    def _write(self, name: str, payload: dict) -> None:
        """File one record, stamped as this projection's.

        The stamp goes on here rather than at each caller so that no record can
        be filed without it: an unmarked file reads as the other writer's and
        would be lifted back onto the bus as a record nobody sent.

        A name the other writer already holds simply fails — the file is theirs,
        and it is still to be lifted.
        """
        try:
            self._protocol.write_record(self.directory, name, {
                **payload, self._protocol.WRITER_KEY: self._protocol.BUS_WRITER,
            })
        except FileExistsError:
            pass
        except self._protocol.AskError as exc:
            sys.stderr.write(f"lmer-supervisor: ask-dir projection: {exc}\n")

    # -- files → bus -----------------------------------------------------------

    def lift_foreign_records(self) -> list[tuple[Message, str]]:
        """Records filed here by something other than this projection, appended
        to the bus. Returns what was appended.

        The session's own records (issue #421) go on as the session's, so the
        daemon spools a milestone and renders a note exactly as it would for a
        session using the bus directly. An answer stays ``platform``: it is the
        operator's, whichever daemon wrote it.
        """
        protocol = self._protocol
        try:
            names = os.listdir(self.directory)
        except OSError:
            return []
        lifted: list[tuple[Message, str]] = []
        for name in sorted(names, key=self._lift_order):
            entry = self._entry_of(name)
            if entry is None:
                continue
            stem, kind = entry
            with self._lock:
                if name in self._lifted:
                    continue
            if self._is_projected(name):
                continue
            record = self._read_for_lift(name, stem, kind)
            if record is None:
                continue
            with self._lock:
                self._lifted.add(name)
            try:
                message, accepted = self._log.append(
                    source=Source(**{**record.pop("source"), "ref": f"{ASK_DIR_REF_PREFIX}{name}"}),
                    **record,
                )
            except CommsError as exc:
                sys.stderr.write(f"lmer-supervisor: cannot lift {name} onto the bus: {exc}\n")
                continue
            if kind == KIND_QUESTION:
                with self._lock:
                    self._remember_lift(stem, message)
            lifted.append((message, accepted))
        return lifted

    def _provenance_option(
        self, option: Optional[int], text: str, question: str
    ) -> Optional[int]:
        """*option* if `append` would accept it beside *text*, else ``None``.

        The file already carries the resolved text — this index is only a record
        of how the choice was named — so a foreign writer's unusable one is
        dropped rather than costing the answer. Gated on the same predicate
        `append` raises from, so no value of `option` can strand a delivered
        answer: a refusal on this leg is unrecoverable, because the file's name
        is in `_lifted` before the append is attempted.
        """
        if option is None:
            return None
        state = self._log.question(question)
        offered = state.question.options if state is not None else ()
        return option if option_conflict(text, option, offered) is None else None

    #: The kinds that name a question rather than standing alone.
    _REPLY_KINDS = frozenset({KIND_ANSWER, KIND_CLOSE})

    def _lift_order(self, name: str) -> tuple:
        """Sort key: by entry id, replies last. A reply cannot go on the bus
        before the question it names, and ``answer`` sorts before ``question``
        by name.
        """
        entry = self._entry_of(name)
        if entry is None:
            return (name, 0, name)
        stem, kind = entry
        return (stem, 1 if kind in self._REPLY_KINDS else 0, name)

    def _read_for_lift(self, name: str, stem: str, kind: str) -> Optional[dict]:
        """The append arguments for the file *name*, or ``None`` if it is not a
        record this projection can put on the bus."""
        protocol = self._protocol
        if kind in self._REPLY_KINDS:
            with self._lock:
                question = self._question_by_stem.get(stem)
            if question is None:
                state = self._log.question(stem)
                if state is None:
                    return None
                question = state.question.id
        if kind == KIND_ANSWER:
            try:
                answer = protocol.load_answer(self.directory, stem)
            except protocol.AskError:
                return None
            if answer is None:
                return None
            return {
                "kind": KIND_ANSWER, "text": answer.text, "in_reply_to": question,
                "option": self._provenance_option(answer.option, answer.text, question),
                "source": {"channel": CHANNEL_PLATFORM, "author": answer.source or "operator"},
            }
        if kind == KIND_CLOSE:
            closure = protocol.load_closure(self.directory, stem)
            if closure is None:
                return None
            return {
                # A close carries no reason of its own when the CLI was not given
                # one, and the bus allows that text to be empty.
                "kind": KIND_CLOSE, "text": closure.reason or "",
                "in_reply_to": question, "source": {"channel": CHANNEL_AGENT},
                "session_caller": True,
            }
        record = protocol._read_json(self.directory / name)
        text = record.get("text") if isinstance(record, dict) else None
        if not isinstance(text, str) or not text:
            return None
        options = record.get("options") if kind == KIND_QUESTION else ()
        return {
            "kind": kind, "text": text, "options": options or (),
            "source": {"channel": CHANNEL_AGENT}, "session_caller": True,
        }


def drain_grace(env: Optional[Mapping] = None) -> float:
    """The drain grace in seconds; a malformed or out-of-range value falls
    back to the default rather than stalling or skipping the drain."""
    raw = (env if env is not None else os.environ).get(DRAIN_GRACE_ENV, "")
    if not raw.strip():
        return DEFAULT_DRAIN_GRACE
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_DRAIN_GRACE
    if value != value or not 0.0 <= value <= DRAIN_GRACE_MAX:
        return DEFAULT_DRAIN_GRACE
    return value


def terminal_mirror_enabled(env: Optional[Mapping] = None) -> bool:
    return get_bool_env(TERMINAL_MIRROR_ENV, True, env=env)
