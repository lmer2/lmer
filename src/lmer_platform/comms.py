"""The daemon's end of the unified communications channel (issue #407).

Every live session with a bus (``/healthz`` carries ``comms``) gets one
subscriber thread here, long-polling the supervisor's ``GET /messages`` and
appending each record to one **fleet journal** with a daemon-wide cursor. That
journal is what the daemon re-serves: the per-session chat pane, the fleet
feed a platform-side client subscribes to, and the assistant's digests all
read it. The daemon reads no file a container wrote — every record arrives
over the control plane, and the supervisor's drain grace keeps that plane up
after the harness exits until this hub has acked the last record.

The hub is process-wide state, registered by the daemon at start-up
(:func:`register_hub`) the way the assistant supervisor is, because the API
app is built separately and its routes need to reach it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from lmer_cli import comms as bus_comms
from lmer_cli import runref

from . import registry, session_io, teardown
from .store import (
    StoreError, ensure_state_dir, mutating, platform_dir,
    read_json, utc_now_iso, write_json,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SUBSCRIBER", "COMMS_DIRNAME", "JOURNAL_FILE", "POSITIONS_FILE",
    "CONNECTOR_POSITIONS_FILE", "POLL_SECONDS", "RECONCILE_SECONDS",
    "DIGEST_SECONDS", "CHANNEL_PLATFORM", "CHANNEL_UBER",
    "KIND_COMMAND", "KIND_MILESTONE", "KIND_QUESTION", "KIND_ANSWER",
    "COMMAND_CALLERS", "FleetEvent", "Journal", "CommsHub", "CommsError",
    "NoBus", "register_hub", "get_hub", "ensure_hub", "comms_dir", "journal_path",
]

#: How this daemon acks itself to every supervisor.
SUBSCRIBER = bus_comms.PLATFORM_SUBSCRIBER

COMMS_DIRNAME = "comms"
JOURNAL_FILE = "journal.jsonl"
POSITIONS_FILE = "positions.json"
CONNECTOR_POSITIONS_FILE = "connectors.json"

#: The supervisor caps a long-poll at 30 s; this leaves the round trip inside
#: the control-plane read budget the client adds on top.
POLL_SECONDS = 25.0

#: How often the hub compares the registry against its subscribers.
RECONCILE_SECONDS = 5.0

#: How often non-milestone traffic is coalesced into one digest per session
#: for the assistant (spec decision 8: everything, batched).
DIGEST_SECONDS = 20.0

#: Bytes the journal may reach before it is rotated (one previous generation is
#: kept). It is a cache of records the supervisors own — the session logs are
#: the record — and an unbounded file is also an unbounded scan: a cursor older
#: than the memory window re-reads it, and two UI-polled routes do exactly that.
MAX_JOURNAL_BYTES = 32 * 1024 * 1024

#: Records the chat pane merges for one session. The pane shows a conversation,
#: not an archive, and the number bounds what a `since=0` read can cost.
PANE_LIMIT = 2000

#: Records kept in memory for the feed's fast path; older cursors are served
#: from the journal file.
MEMORY_EVENTS = 5000

#: Longest a subscriber sleeps between attempts after a transport failure.
BACKOFF_MAX = 30.0

CHANNEL_PLATFORM = "platform"
CHANNEL_UBER = "uber-lmer"

KIND_MESSAGE = "message"
KIND_MILESTONE = "milestone"
KIND_QUESTION = "question"
KIND_ANSWER = "answer"
KIND_COMMAND = "command"

#: Callers whose posts may be harness commands (spec §5.2): the operator and
#: the assistant, never a connector — the channels a human speaks through
#: carry prose.
COMMAND_CALLERS = frozenset({"operator", "assistant"})


class CommsError(RuntimeError):
    """A comms operation the daemon refuses or cannot complete."""

    status = 500


class NoBus(CommsError):
    """The session has no bus: a pre-#407 image, or no control plane at all."""

    status = 409


def comms_dir() -> Path:
    return platform_dir() / COMMS_DIRNAME


def journal_path() -> Path:
    return comms_dir() / JOURNAL_FILE


@dataclass(frozen=True)
class FleetEvent:
    """One record as the fleet sees it: the daemon's cursor, the session it
    came from and what the registry knew about that session at the time."""

    cursor: int
    session: str
    message: dict
    run: Optional[dict] = None
    session_kind: Optional[str] = None
    title: Optional[str] = None
    at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict:
        return {
            "cursor": self.cursor,
            "at": self.at,
            "session": self.session,
            "session_kind": self.session_kind,
            "run": self.run,
            "title": self.title,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, raw: object) -> Optional["FleetEvent"]:
        if not isinstance(raw, dict):
            return None
        cursor = raw.get("cursor")
        session = raw.get("session")
        message = raw.get("message")
        if not isinstance(cursor, int) or isinstance(cursor, bool):
            return None
        if not isinstance(session, str) or not isinstance(message, dict):
            return None
        run = raw.get("run")
        return cls(
            cursor=cursor,
            session=session,
            message=message,
            run=run if isinstance(run, dict) else None,
            session_kind=raw.get("session_kind") if isinstance(raw.get("session_kind"), str) else None,
            title=raw.get("title") if isinstance(raw.get("title"), str) else None,
            at=raw.get("at") if isinstance(raw.get("at"), str) else "",
        )


class Journal:
    """The fleet journal: append-only JSONL with a monotonic cursor.

    Dedupes on ``message.id`` so a subscriber that re-reads after a crash (its
    position is written after the line) cannot double a record. The last
    :data:`MEMORY_EVENTS` are kept in memory for the feed; a cursor older than
    that is served by scanning the file.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path if path is not None else journal_path()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._events: deque = deque(maxlen=MEMORY_EVENTS)
        self._ids: set = set()
        self._cursor = 0
        #: session → {question id: state}, folded as records arrive. Re-deriving
        #: it per call read the whole journal once per session, on a route the
        #: web UI polls.
        self._questions: dict = {}
        self._load()

    @property
    def _checkpoint_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".checkpoint.json")

    def _disk_events(self) -> Iterable[FleetEvent]:
        """Read both retained generations in cursor order."""
        previous = self.path.with_suffix(self.path.suffix + ".1")
        for path in (previous, self.path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            event = FleetEvent.from_dict(json.loads(line))
                        except ValueError:
                            continue
                        if event is not None:
                            yield event
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning("platform_comms_journal_unreadable path=%s error=%s", path, exc)

    def _load(self) -> None:
        checkpoint = read_json(self._checkpoint_path)
        folded_through = 0
        if isinstance(checkpoint, dict):
            folded_through = int(checkpoint.get("cursor", 0))
            self._cursor = folded_through
            self._questions = checkpoint.get("questions", {})
        last = 0
        for event in self._disk_events():
            if event.cursor <= last:
                continue
            last = event.cursor
            self._cursor = max(self._cursor, event.cursor)
            self._events.append(event)
            if event.cursor > folded_through:
                self._fold(event)
            message_id = event.message.get("id")
            if isinstance(message_id, str):
                self._ids.add(message_id)

    @property
    def cursor(self) -> int:
        with self._lock:
            return self._cursor

    def append(
        self,
        session: str,
        message: dict,
        *,
        run: Optional[dict] = None,
        session_kind: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Optional[FleetEvent]:
        """Journal one record. ``None`` when its id is already there."""
        message_id = message.get("id")
        with self._cond:
            if isinstance(message_id, str) and message_id in self._ids:
                return None
            event = FleetEvent(
                cursor=self._cursor + 1, session=session, message=dict(message),
                run=run, session_kind=session_kind, title=title,
            )
            line = json.dumps(event.to_dict(), ensure_ascii=False) + "\n"
            try:
                ensure_state_dir(self.path.parent)
                fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, line.encode("utf-8"))
                finally:
                    os.close(fd)
            except OSError as exc:
                raise StoreError(f"cannot append to the comms journal {self.path} ({exc})")
            self._cursor = event.cursor
            self._events.append(event)
            self._fold(event)
            if isinstance(message_id, str):
                self._ids.add(message_id)
            self._rotate_if_large()
            self._cond.notify_all()
            return event

    def _fold(self, event: "FleetEvent") -> None:
        """Caller holds the lock. One record's effect on the question fold."""
        message = event.message
        kind = message.get("kind")
        if kind == KIND_QUESTION:
            self._questions.setdefault(event.session, {})[message.get("id")] = {
                "question": message, "state": "open", "answered_by": None, "closed_by": None,
            }
            return
        if kind not in (KIND_ANSWER, "close"):
            return
        target = self._questions.get(event.session, {}).get(message.get("in_reply_to"))
        if target is None or target["state"] != "open":
            return
        if kind == KIND_ANSWER and not (message.get("meta") or {}).get("late"):
            target["state"] = "answered"
            target["answered_by"] = message.get("id")
        elif kind == "close":
            target["state"] = "closed"
            target["closed_by"] = message.get("id")

    def _rotate_if_large(self) -> None:
        """Caller holds the lock. Checkpoint the fold before retiring history."""
        try:
            if self.path.stat().st_size < MAX_JOURNAL_BYTES:
                return
            write_json(self._checkpoint_path, {
                "cursor": self._cursor, "questions": self._questions,
            })
            self.path.replace(self.path.with_suffix(self.path.suffix + ".1"))
        except (OSError, StoreError) as exc:
            logger.warning("platform_comms_journal_rotate_failed path=%s error=%s", self.path, exc)
            return
        self._ids = {
            event.message["id"] for event in self._disk_events()
            if isinstance(event.message.get("id"), str)
        }
        logger.info("platform_comms_journal_rotated path=%s cursor=%s", self.path, self._cursor)

    def read_since(
        self,
        cursor: int,
        *,
        timeout: float = 0.0,
        session: Optional[str] = None,
        limit: int = 500,
    ) -> tuple[list, int]:
        """Events past *cursor*, blocking up to *timeout* for the next one.

        Returns ``(events, next_cursor)``; ``next_cursor`` is the last cursor
        the caller has now seen (or *cursor* itself when nothing matched), so a
        loop that passes it back neither skips nor repeats.
        """
        deadline = time.monotonic() + timeout if timeout > 0 else None
        with self._cond:
            while True:
                events = self._collect(cursor, session, limit)
                if events or deadline is None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
            if not events:
                # Under the lock: `_cursor` moves on every append, and a reader
                # that took it after leaving could report a position past what
                # it was handed.
                return [], max(cursor, self._cursor if session is None else cursor)
        return events, events[-1].cursor

    def _collect(self, cursor: int, session: Optional[str], limit: int) -> list:
        """Caller holds the lock."""
        if not self._events or cursor < self._events[0].cursor - 1:
            events = self._scan_file(cursor, session, limit)
        else:
            events = [
                event for event in self._events
                if event.cursor > cursor and (session is None or event.session == session)
            ][:limit]
        return events

    def _scan_file(self, cursor: int, session: Optional[str], limit: int) -> list:
        found: list = []
        for event in self._disk_events():
            if event.cursor <= cursor:
                continue
            if session is not None and event.session != session:
                continue
            found.append(event)
            cursor = event.cursor
            if len(found) >= limit:
                break
        return found

    def question_states(self, session: str) -> dict:
        """One session's fold: ``{question id: state}``, ``state`` in
        ``open``/``answered``/``closed``. Kept as records arrive."""
        with self._lock:
            return {qid: dict(state) for qid, state in self._questions.get(session, {}).items()}

    def sessions(self) -> list:
        """Every session the fold knows a question for."""
        with self._lock:
            return sorted(self._questions)


class _Positions:
    """Per-session high-water marks, persisted after each journal line."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._marks: dict = {}
        try:
            data = read_json(path)
        except StoreError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
            for session_id, seq in data["sessions"].items():
                if isinstance(seq, int) and not isinstance(seq, bool):
                    self._marks[session_id] = seq

    def get(self, session_id: str) -> int:
        with self._lock:
            return self._marks.get(session_id, 0)

    def set(self, session_id: str, seq: int) -> None:
        with self._lock:
            if seq <= self._marks.get(session_id, 0):
                return
            self._marks[session_id] = seq
            snapshot = dict(self._marks)
        try:
            with mutating(self.path):
                write_json(self.path, {"sessions": snapshot})
        except StoreError as exc:
            logger.warning("platform_comms_positions_unwritable path=%s error=%s", self.path, exc)


class _Subscriber(threading.Thread):
    """One session's long-poll loop."""

    def __init__(self, hub: "CommsHub", session_id: str) -> None:
        super().__init__(name=f"lmer-comms-{session_id}", daemon=True)
        self.hub = hub
        self.session_id = session_id
        self.stop_event = threading.Event()
        self.failures = 0
        self.registered = False

    def run(self) -> None:
        hub = self.hub
        session_id = self.session_id
        while not self.stop_event.is_set():
            since = hub.positions.get(session_id)
            try:
                endpoint = session_io.control_endpoint(session_id)
                reply = session_io._get(
                    endpoint, "/messages",
                    params={"since": since, "timeout": POLL_SECONDS},
                    timeout=(session_io.CONTROL_TIMEOUT_SECONDS, POLL_SECONDS + session_io.CONTROL_TIMEOUT_SECONDS),
                )
            except session_io.ControlUnavailable:
                # The session is gone from the registry, or its process is: this
                # subscription has nothing left to read. The drain grace covered
                # the tail while the control plane was still answering.
                break
            except session_io.SessionIOError as exc:
                self.failures += 1
                logger.debug("platform_comms_poll_failed session=%s error=%s", session_id, exc)
                if not registry.is_live(registry.read_session(session_id)):
                    break
                self.stop_event.wait(min(BACKOFF_MAX, 0.5 * (2 ** min(self.failures, 6))))
                continue
            if reply.status in (503, 404):
                # The bus vanished under us — a supervisor restart onto an older
                # image is the only known way; stop rather than spin.
                break
            if not reply.ok:
                self.failures += 1
                self.stop_event.wait(min(BACKOFF_MAX, 0.5 * (2 ** min(self.failures, 6))))
                continue
            self.failures = 0
            timing = reply.payload.get("terminal_mirror")
            if isinstance(timing, dict):
                from .config import terminal_settings

                seconds = terminal_settings()["confirm_seconds"].value
                if timing.get("window_seconds") != seconds:
                    try:
                        changed = session_io._post(
                            endpoint, "/messages/terminal-mirror",
                            {"window_seconds": seconds},
                        )
                        if not changed.ok:
                            logger.warning(
                                "platform_terminal_timing_failed session=%s status=%s",
                                session_id, changed.status,
                            )
                    except session_io.SessionIOError as exc:
                        logger.warning(
                            "platform_terminal_timing_failed session=%s error=%s", session_id, exc,
                        )
            if not self.registered:
                # Registering, not acking a record: until a subscriber is known
                # to the supervisor its drain has nobody to wait for, and a
                # session whose last act is its only record loses it.
                try:
                    session_io._post(
                        endpoint, "/messages/ack",
                        {"subscriber": SUBSCRIBER, "seq": since, "final": False},
                    )
                    self.registered = True
                except session_io.SessionIOError:
                    pass
            messages = reply.payload.get("messages") or []
            cursor = reply.payload.get("cursor")
            last = since
            held_all = True
            for message in messages:
                if not isinstance(message, dict):
                    continue
                if not hub._ingest(session_id, message):
                    held_all = False
                    break
                seq = message.get("seq")
                if isinstance(seq, int) and seq > last:
                    last = seq
            if held_all and isinstance(cursor, int) and not isinstance(cursor, bool) and cursor > last:
                last = cursor
            if not held_all:
                # A journal write that failed is a record this daemon does not
                # have; acking it would tell the supervisor otherwise.
                self.failures += 1
                self.stop_event.wait(min(BACKOFF_MAX, 0.5 * (2 ** min(self.failures, 6))))
            if last > since:
                hub.positions.set(session_id, last)
                try:
                    session_io._post(endpoint, "/messages/ack", {"subscriber": SUBSCRIBER, "seq": last})
                except session_io.SessionIOError:
                    pass
        hub._subscriber_ended(session_id)


class CommsHub:
    """Subscribes to every session bus, keeps the fleet journal, serves it."""

    def __init__(self, *, journal: Optional[Journal] = None, positions_path: Optional[Path] = None) -> None:
        self.journal = journal if journal is not None else Journal()
        self.positions = _Positions(positions_path or comms_dir() / POSITIONS_FILE)
        self._subscribers: dict = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._supports: dict = {}
        self._consumers: list = []
        self._connector_positions = _Positions(comms_dir() / CONNECTOR_POSITIONS_FILE)
        self._pending_digest: dict = {}
        self._last_digest_flush = time.monotonic()

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._reconcile_forever, name="lmer-comms-hub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            subscribers = list(self._subscribers.values())
            # Dropped now rather than when each thread unwinds its long-poll:
            # a stopped hub handles nobody, and the ask-dir reader must not
            # keep skipping a session on the strength of a thread that is
            # merely waiting to exit.
            self._subscribers.clear()
        for subscriber in subscribers:
            subscriber.stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _reconcile_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile()
            except Exception as exc:  # the hub must outlive one bad tick
                logger.warning("platform_comms_reconcile_failed error=%s", exc)
            if time.monotonic() - self._last_digest_flush >= DIGEST_SECONDS:
                self.flush_digests()
            self._stop.wait(RECONCILE_SECONDS)

    def reconcile(self) -> None:
        """Start a subscriber for every live session with a bus that lacks one."""
        for entry in registry.list_sessions(live_only=True):
            session_id = entry.get("id") if isinstance(entry, dict) else None
            if not isinstance(session_id, str) or not session_id:
                continue
            with self._lock:
                if session_id in self._subscribers:
                    continue
            if not self.supports(session_id):
                continue
            self.subscribe(session_id)

    def subscribe(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._subscribers or self._stop.is_set():
                return
            subscriber = _Subscriber(self, session_id)
            self._subscribers[session_id] = subscriber
        subscriber.start()

    def _subscriber_ended(self, session_id: str) -> None:
        with self._lock:
            self._subscribers.pop(session_id, None)
        self._supports.pop(session_id, None)

    # -- capability ------------------------------------------------------------

    def supports(self, session_id: str) -> bool:
        """Whether *session_id*'s supervisor keeps a bus (``/healthz`` carries
        ``comms``). Memoized per session; a ``False`` is re-probed every
        reconcile because the answer only turns true once."""
        cached = self._supports.get(session_id)
        if cached is True:
            return True
        health = session_io.quiet_health(session_id)
        if health is None:
            return False
        supported = isinstance(health.get("comms"), dict)
        if supported:
            self._supports[session_id] = True
        return supported

    def question_for_ask_entry(
        self, session_id: str, entry_id: str, *, mirrored: bool
    ) -> Optional[str]:
        """The id of the bus question that *is* ask-directory entry *entry_id*,
        or ``None`` when the bus holds no such question.

        **Never by arithmetic.** An entry id and a ``seq`` are different counters
        (``lmer_cli.comms.AskDirProjection``), so "the question at ``seq`` == the
        entry id" binds an answer to whatever record happens to carry that
        number — a different question, or one already answered. Two things make
        a bus question this entry, and neither is guessed at:

        * it was **lifted** out of that file, and carries the file's name in
          ``source.ref``;
        * it was **mirrored** into that file, which the projection stamps and
          files under the record's own ``seq`` — so *mirrored* is the caller's
          reading of that stamp, and without it the file belongs to the session's
          own CLI whatever number it carries.

        ``None`` sends the answer down the ask-directory path, which is where the
        question is.
        """
        states = self.journal.question_states(session_id)
        for qid, state in states.items():
            record = state.get("question") or {}
            ref = (record.get("source") or {}).get("ref")
            if bus_comms.ask_dir_ref_stem(ref) == entry_id:
                return qid
        if not mirrored or f"{session_id}/{entry_id}" not in states:
            return None
        return f"{session_id}/{entry_id}"

    def handles(self, session_id: str) -> bool:
        """Whether this hub is the reader for *session_id*'s milestones — the
        ask-dir reader in :mod:`lmer_platform.detect` skips such sessions."""
        with self._lock:
            return session_id in self._subscribers

    # -- ingest ----------------------------------------------------------------

    def add_consumer(self, consumer: Callable[[FleetEvent], None]) -> None:
        self._consumers.append(consumer)

    def _ingest(self, session_id: str, message: dict) -> bool:
        """Journal one record. ``False`` means it is *not* held — the caller must
        not advance its position past it, and must not ack it to the supervisor,
        which would release the drain hold over a record nobody has.
        """
        entry = registry.read_session(session_id)
        run = None
        kind = None
        if isinstance(entry, dict):
            run = entry.get("run") or None
            kind = entry.get("kind")
        try:
            # Before the journal/ack: a transient registry write failure must
            # leave this lifecycle fact eligible for the subscriber's retry.
            teardown.record_request(session_id, message)
            from . import assistant_history

            archived = assistant_history.generation(session_id)
            if kind == "assistant" and archived is None:
                from . import assistant

                archived = assistant.history_for(session_id)
            if kind == "assistant" or archived:
                assistant_history.retain(session_id, message)
            event = self.journal.append(session_id, message, run=run, session_kind=kind)
        except StoreError as exc:
            logger.warning("platform_comms_journal_failed session=%s error=%s", session_id, exc)
            return False
        if event is None:
            return True  # already journaled: held, nothing to hand on
        for consumer in list(self._consumers):
            try:
                consumer(event)
            except Exception as exc:
                logger.warning("platform_comms_consumer_failed error=%s", exc)
        return True

    # -- reads -----------------------------------------------------------------

    def feed(self, since: int, *, timeout: float = 0.0, session: Optional[str] = None, limit: int = 500) -> tuple[list, int]:
        return self.journal.read_since(since, timeout=timeout, session=session, limit=limit)

    def questions(self, *, open_only: bool = False) -> list:
        """The question fold across the live sessions this hub has journaled.

        Live only: a question from a session that has since exited can never be
        answered, and an ask dock showing it forever is not a fold "across live
        sessions" (§5.2) but a list of everything that was ever asked.
        """
        found: list = []
        for session_id in self.journal.sessions():
            if not registry.is_live(registry.read_session(session_id)):
                continue
            for state in self.journal.question_states(session_id).values():
                if open_only and state["state"] != "open":
                    continue
                found.append({"session": session_id, **state})
        return found

    def ack_connector(self, connector: str, cursor: int) -> None:
        self._connector_positions.set(connector, cursor)

    def connectors(self) -> dict:
        with self._connector_positions._lock:
            return dict(self._connector_positions._marks)

    # -- writes ----------------------------------------------------------------

    def post(
        self,
        session_id: str,
        *,
        kind: str,
        text: str,
        channel: str,
        author: Optional[str] = None,
        ref: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        option: Optional[int] = None,
        options: Iterable[str] = (),
        attachments: Iterable[dict] = (),
        meta: Optional[dict] = None,
    ) -> dict:
        """Post one record on *session_id*'s bus and journal it at once.

        The subscriber would journal it on its next poll anyway; journaling here
        too makes the record visible to the feed before the caller's reply
        returns (the journal dedupes on id).
        """
        endpoint = session_io.control_endpoint(session_id)
        body = {
            "kind": kind,
            "text": text,
            "source": {k: v for k, v in (("channel", channel), ("author", author), ("ref", ref)) if v},
            "in_reply_to": in_reply_to,
            "option": option,
            "options": list(options),
            "attachments": [dict(item) for item in attachments],
            "meta": dict(meta or {}),
        }
        # Hashed before the wire, like `session_io.send_input`: the durable
        # record of the attempt (#197, spec §5.2) is what answers "did my
        # message reach the session?", and the chat composer, `lmer-ctl send`
        # and the ask answer all come through here now.
        payload_bytes = text.encode("utf-8")
        sent_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        reply = None
        error: Optional[str] = None
        try:
            try:
                reply = session_io._post(endpoint, "/messages", body)
            except session_io.SessionIOError as exc:
                error = str(exc)[:400]
                raise
            if reply.status in (503, 404):
                error = "no message bus"
                raise NoBus(
                    f"session {session_id} has no message bus (its image predates the unified channel)"
                )
            if not reply.ok:
                error = reply.detail
                failure = CommsError(f"session {session_id}'s bus refused the record ({reply.detail})")
                failure.status = 400 if reply.status == 400 else 502
                raise failure
        finally:
            record = (reply.payload.get("message") or {}) if reply is not None else {}
            relay = (reply.payload.get("relay") or {}) if reply is not None else {}
            receipt = (reply.payload.get("receipt") or {}).get("payload_sha256") if reply else None
            session_io._record_attempt(
                "comms_post", session_id,
                endpoint=endpoint, reply=reply, error=error,
                kind=kind, channel=channel, length=len(payload_bytes),
                payload_hmac=session_io._payload_hmac(endpoint, payload_bytes),
                # A verdict, never the receipt itself, for the same reason
                # `session_input` keeps one: the hash of a message written
                # durably would undo the HMAC above.
                receipt_match=(None if receipt is None else receipt == sent_sha256),
                submit_text=relay.get("submit_text"),
                message_id=record.get("id"),
                accepted_as=(reply.payload.get("accepted_as") if reply is not None else None),
            )
        payload = reply.payload
        message = payload.get("message")
        if isinstance(message, dict):
            self._ingest(session_id, message)
        return payload

    # -- assistant digests -----------------------------------------------------

    def queue_digest(self, event: FleetEvent) -> None:
        """Remember a non-milestone record for the next batched digest."""
        with self._lock:
            self._pending_digest.setdefault(event.session, []).append(event)

    def flush_digests(self, notify: Optional[Callable] = None) -> int:
        """Coalesce queued records into one digest per session. Returns how many
        digests were spooled."""
        from . import assistant

        with self._lock:
            pending, self._pending_digest = self._pending_digest, {}
            self._last_digest_flush = time.monotonic()
        spool = notify or assistant.notify
        count = 0
        for session_id, events in pending.items():
            lines = []
            for event in events[-8:]:
                message = event.message
                source = message.get("source") or {}
                who = source.get("channel", "?")
                if source.get("author"):
                    who = f"{who} · {source['author']}"
                text = str(message.get("text", "")).splitlines()[0] if message.get("text") else ""
                lines.append(f"[{who}] {message.get('kind')}: {text[:120]}")
            run = events[-1].run or {}
            subject = None
            if run.get("host") and run.get("project") and run.get("slug"):
                subject = runref.compose(run["host"], run["project"], run["slug"])
            note = f"{subject or 'session ' + session_id} comms ({len(events)}): " + " | ".join(lines)
            try:
                spool(note[:assistant.MAX_NOTE_CHARS], kind="session_comms", data={
                    "session": session_id,
                    "count": len(events),
                    "first_cursor": events[0].cursor,
                    "last_cursor": events[-1].cursor,
                    **{k: run.get(k) for k in ("host", "project", "slug")},
                })
                count += 1
            except Exception as exc:
                logger.warning("platform_comms_digest_failed session=%s error=%s", session_id, exc)
        return count


# ---------------------------------------------------------------------------
# Process-wide hub, the way the assistant supervisor is published
# ---------------------------------------------------------------------------

_HUB: Optional[CommsHub] = None
_HUB_LOCK = threading.Lock()


def register_hub(hub: Optional[CommsHub]) -> None:
    global _HUB
    with _HUB_LOCK:
        _HUB = hub


def get_hub() -> Optional[CommsHub]:
    with _HUB_LOCK:
        return _HUB


def ensure_hub() -> CommsHub:
    """The registered hub, or a passive one registered now.

    A daemon registers a started hub before it serves; an app built without one
    (tests, an embedding caller) still needs the journal and the post path, so
    the routes get a hub whose reconcile thread is simply never started.
    """
    global _HUB
    with _HUB_LOCK:
        if _HUB is None:
            _HUB = CommsHub()
        return _HUB
