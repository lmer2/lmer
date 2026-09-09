"""The matrix connector: one thread mirrored onto one session's bus (issue #407).

The mirror of :mod:`slack_chat.connector`, over the client-server API as a
bot account:

- **outbound** — long-polls the supervisor's ``GET /messages`` and posts every
  record that did not come from matrix into the thread (an ``m.thread``
  relation on the root event), rendered for a human, then acks its position;
- **inbound** — ``/sync`` with a filter on the one room, taking the thread's
  replies from anyone but the bot and posting each onto the bus as
  ``source {channel: matrix, author: <mxid>, ref: <event_id>}`` — an
  ``answer`` to the oldest open question when the session has one (a reply
  that is exactly an option's number or text is that option), else a
  ``message``. Media events are downloaded into the session's upload store
  when one is mounted and the record names them.

Hosted on a thread in the ``lmer`` process for a matrix-thread target
(:class:`lmer_cli.targets.MatrixThreadTargets`); runnable standalone as
``lmer-matrix-connector``. The account's access token stays in the process
that runs it.

An encrypted room is refused at connect time with its own name: this
connector reads and writes plaintext events only. Encryption for it is the
follow-up :mod:`matrix_chat` names.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import logging
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ask_channel import bus
from lmer_cli import thread_posts
from lmer_cli.util import get_bool_env
from matrix_bridge.scrub import scrub_bytes, scrub_text
from slack_chat.connector import (
    ATTACH_ACK, ATTACH_ACK_ENV, ATTACH_FROM_NOW_ENV, ATTACH_SINCE_ENV, DETACH_NOTE,
    _int_env, _with_references, answer_binding, attachment_notice, option_for,
    post_replay_notice, render_outbound as _render_slack, replay_stamp,
)

from .client import MatrixAccount, MatrixClient, MatrixError
from .permalink import matrix_to_link, parse_matrix_permalink

logger = logging.getLogger(__name__)

__all__ = [
    "ConnectorConfig", "MatrixConnector", "render_outbound", "strip_reply_fallback",
    "resolve_thread_root", "CHANNEL", "SUBSCRIBER", "ATTACH_ACK", "main",
]

CHANNEL = "matrix"
SUBSCRIBER = "matrix"

HOMESERVER_ENV = "LMER_MATRIX_HOMESERVER"
USER_ID_ENV = "LMER_MATRIX_USER_ID"
ACCESS_TOKEN_ENV = "LMER_MATRIX_ACCESS_TOKEN"
AUTHENTICATED_MEDIA_ENV = "LMER_MATRIX_AUTHENTICATED_MEDIA"

BUS_POLL_SECONDS = 25.0
SYNC_TIMEOUT_MS = 25000

#: Matrix bodies are not size-capped by the spec in practice, but a thread is
#: read on a phone; the bridge's cap.
MAX_BODY_CHARS = 3000

#: The reply fallback the spec removed (MSC2781): a first line quoting the
#: replied-to sender. Stripped only when it is actually there — a body that
#: merely starts with ``>`` (``>= 3.12 is required``) is text.
_FALLBACK_FIRST_LINE = re.compile(r"^> <@[^>]+>")

_MEDIA_TYPES = frozenset({"m.image", "m.file", "m.audio", "m.video"})
_TEXT_TYPES = frozenset({"m.text", "m.notice", "m.emote", None})


@dataclass(frozen=True)
class ConnectorConfig:
    supervisor_url: str
    supervisor_token: str = field(repr=False)
    homeserver: str
    user_id: str
    access_token: str = field(repr=False)
    room: str
    thread_event: str
    authenticated_media: bool = False
    #: See :data:`slack_chat.connector.ATTACH_ACK_ENV` — one rule, both transports.
    attach_ack: bool = True
    #: See :data:`slack_chat.connector.ATTACH_FROM_NOW_ENV`.
    attach_from_now: bool = False
    #: See :data:`slack_chat.connector.ATTACH_SINCE_ENV`.
    attach_since: Optional[int] = None
    uploads_dir: Optional[str] = None
    container_uploads_dir: Optional[str] = None
    #: Seconds to wait for a supervisor that is not up yet (the host CLI starts
    #: this connector before it launches the container).
    connect_budget: float = bus.DEFAULT_CONNECT_BUDGET

    @classmethod
    def from_env(
        cls,
        env=None,
        *,
        room: Optional[str] = None,
        thread_event: Optional[str] = None,
        supervisor_url: Optional[str] = None,
        supervisor_token: Optional[str] = None,
        uploads_dir: Optional[str] = None,
        container_uploads_dir: Optional[str] = None,
        attach_ack: Optional[bool] = None,
        attach_from_now: Optional[bool] = None,
        attach_since: Optional[int] = None,
    ) -> "ConnectorConfig":
        environ = os.environ if env is None else env
        if not (supervisor_url and supervisor_token):
            endpoint = bus.resolve_endpoint(environ)
            supervisor_url = supervisor_url or endpoint.url
            supervisor_token = supervisor_token or endpoint.token
        room = room or (environ.get("LMER_MATRIX_ROOM_ID") or "").strip()
        thread_event = thread_event or (environ.get("LMER_MATRIX_THREAD_EVENT") or "").strip()
        if not room or not thread_event:
            raise ValueError(
                "the connector needs a matrix thread: LMER_MATRIX_ROOM_ID and "
                "LMER_MATRIX_THREAD_EVENT, or a permalink"
            )
        homeserver = (environ.get(HOMESERVER_ENV) or "").strip()
        user_id = (environ.get(USER_ID_ENV) or "").strip()
        token = (environ.get(ACCESS_TOKEN_ENV) or "").strip()
        missing = [name for name, value in (
            (HOMESERVER_ENV, homeserver), (USER_ID_ENV, user_id), (ACCESS_TOKEN_ENV, token),
        ) if not value]
        if missing:
            raise ValueError(f"the matrix connector needs {', '.join(missing)} in the environment")
        return cls(
            supervisor_url=supervisor_url, supervisor_token=supervisor_token,
            homeserver=homeserver, user_id=user_id, access_token=token,
            room=room, thread_event=thread_event,
            authenticated_media=get_bool_env(AUTHENTICATED_MEDIA_ENV, False, env=environ),
            attach_ack=(
                get_bool_env(ATTACH_ACK_ENV, True, env=environ)
                if attach_ack is None else attach_ack
            ),
            attach_from_now=(
                get_bool_env(ATTACH_FROM_NOW_ENV, False, env=environ)
                if attach_from_now is None else attach_from_now
            ),
            attach_since=(
                _int_env(environ.get(ATTACH_SINCE_ENV))
                if attach_since is None else attach_since
            ),
            uploads_dir=uploads_dir, container_uploads_dir=container_uploads_dir,
            connect_budget=bus.connect_budget(environ),
        )

    @property
    def account(self) -> MatrixAccount:
        return MatrixAccount(homeserver=self.homeserver, user_id=self.user_id, access_token=self.access_token)


def render_outbound(record: dict, questions: Optional[dict] = None) -> Optional[str]:
    """The thread text for one bus record, or ``None`` when it is matrix's own.

    The same rendering the Slack connector uses (numbered options, who answered
    from where, kind glyphs) keyed on *this* channel — a Slack record belongs in
    a matrix thread, and only matrix's own does not — scrubbed the way the fleet
    bridge scrubs everything it posts.
    """
    text = _render_slack(record, questions, own_channel=CHANNEL)
    if text is None:
        return None
    text = scrub_text(text)
    if len(text) > MAX_BODY_CHARS:
        text = text[: MAX_BODY_CHARS - 1] + "…"
    return text


def strip_reply_fallback(body: str) -> str:
    """Drop a quoted reply fallback — and only a real one (issue #392's rule)."""
    if not _FALLBACK_FIRST_LINE.match(body or ""):
        return body
    lines = body.splitlines()
    index = 0
    while index < len(lines) and lines[index].startswith(">"):
        index += 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    return "\n".join(lines[index:])


def resolve_thread_root(event: dict) -> str:
    """The thread an event belongs to: its ``m.thread`` root, else itself."""
    content = event.get("content") if isinstance(event.get("content"), dict) else {}
    relates = content.get("m.relates_to") if isinstance(content.get("m.relates_to"), dict) else {}
    if relates.get("rel_type") == "m.thread" and isinstance(relates.get("event_id"), str):
        return relates["event_id"]
    return str(event.get("event_id") or "")


class MatrixConnector:
    def __init__(
        self,
        config: ConnectorConfig,
        *,
        matrix: Optional[MatrixClient] = None,
        bus_client: Optional[bus.BusClient] = None,
        clock: Callable[[], float] = time.monotonic,
        notify: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.config = config
        self.subscriber = SUBSCRIBER + ":" + hashlib.sha256(
            f"{config.room}:{config.thread_event}".encode("utf-8")
        ).hexdigest()[:16]
        self._matrix = matrix
        self._bus = bus_client
        self._notify = notify
        self._stop = threading.Event()
        self._threads_lock = threading.Lock()
        self._threads: list = []
        self.room_id: Optional[str] = None
        self._seen: set = set()
        self._last_event_id: Optional[str] = None
        self._bus_cursor = 0
        self._launch_settings: Optional[dict] = None
        self._next_batch: Optional[str] = None
        self._names: dict = {}
        #: question id → the event this connector posted for it, for `_replies_to`
        self._question_events: dict = {}
        #: questions whose one free-text chance has been used
        self._answered_freely: set = set()
        #: Whether the thread has been told this connector is listening.
        self._announced = False
        #: Who wrote the events this bot account sent (#427).
        self._posters = thread_posts.Attribution(
            poster_of=lambda event: thread_posts.poster_from_matrix(event.get("content")),
            ref_of=lambda event: event.get("event_id"),
            session_of=self._own_session,
            clock=clock,
            warn=lambda note: logger.warning(
                "matrix_connector_unattributed_post note=%s", note,
            ),
        )
        self.failures = 0

    @property
    def matrix(self) -> MatrixClient:
        if self._matrix is None:
            self._matrix = MatrixClient(self.config.account)
        return self._matrix

    @property
    def bus(self) -> bus.BusClient:
        if self._bus is None:
            self._bus = bus.BusClient(
                bus.Endpoint(url=self.config.supervisor_url, token=self.config.supervisor_token)
            )
        return self._bus

    # -- lifecycle -------------------------------------------------------------

    def connect(self) -> str:
        """Resolve and join the room; refuse an encrypted one. Returns the room id."""
        room_id = self.matrix.resolve_room(self.config.room)
        try:
            self.matrix.join(room_id)
        except MatrixError as exc:
            logger.info("matrix_connector_join_skipped room=%s error=%s", room_id, exc)
        if self.matrix.room_is_encrypted(room_id):
            raise MatrixError(
                f"room {room_id} is end-to-end encrypted, and the matrix chat connector "
                "reads and writes plaintext events only — use an unencrypted room for "
                "lmer threads (encryption for this connector is a follow-up)"
            )
        self.room_id = room_id
        return room_id

    def start(self) -> None:
        """Join the room now; run the loops once the bus answers.

        The room is resolved synchronously so an encrypted one is refused where
        the caller can still say so. The *bus* is the part that is not up yet:
        the host CLI starts this connector before it launches the container, and
        both loops read :class:`bus.NoChannel` as "the session is over".
        """
        with self._threads_lock:
            if self._threads:
                return
        if self.room_id is None:
            self.connect()
        thread = threading.Thread(
            target=self._run_when_ready, name="lmer-matrix-connector-connect", daemon=True,
        )
        with self._threads_lock:
            self._threads.append(thread)
        thread.start()

    def _run_when_ready(self) -> None:
        if not self.bus.wait_ready(budget=self.config.connect_budget, stop=self._stop):
            if not self._stop.is_set():
                self._warn(
                    f"the supervisor at {self.config.supervisor_url} never came up "
                    f"within {self.config.connect_budget:.0f}s — this matrix thread "
                    "will not hear the session"
                )
            return
        self._seed_cursor()
        self._announce_attached()
        started = []
        for name, target in (("outbound", self._outbound_loop), ("inbound", self._inbound_loop)):
            thread = threading.Thread(target=target, name=f"lmer-matrix-connector-{name}", daemon=True)
            thread.start()
            started.append(thread)
        with self._threads_lock:
            self._threads.extend(started)

    def _warn(self, message: str) -> None:
        """Loud on both sides: the host `lmer` process configures no logging."""
        logger.warning("matrix_connector_gave_up reason=%s", message)
        if self._notify is not None:
            self._notify(message)

    def _own_session(self) -> Optional[str]:
        """This session's id, as its own supervisor gives it (#427)."""
        try:
            session = self.bus.health().get("session")
        except (bus.BusError, bus.NoChannel) as exc:
            logger.debug("matrix_connector_session_unknown error=%s", exc)
            return None
        return session if isinstance(session, str) and session else None

    def _seed_cursor(self) -> None:
        """Where this attachment starts reading — the Slack connector's rule.

        See :meth:`slack_chat.connector.SlackConnector._seed_cursor`: the
        supervisor's own ``/healthz`` answers both "did I read this bus before"
        and "where is it now", so a mid-life attach mirrors the conversation from
        now on rather than the whole journal.
        """
        try:
            health = self.bus.health()
        except bus.BusError as exc:
            logger.warning("matrix_connector_seed_failed error=%s", exc)
            return
        launch = health.get("launch")
        self._launch_settings = launch if isinstance(launch, dict) else None
        acked = (health.get("subscribers") or {}).get(self.subscriber)
        if isinstance(acked, int) and not isinstance(acked, bool):
            self._bus_cursor = acked
            return
        if self.config.attach_since is not None:
            self._bus_cursor = self.config.attach_since
            return
        if not self.config.attach_from_now:
            return
        seq = health.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            self._bus_cursor = seq

    def _announce_attached(self) -> None:
        """Say the session is listening — the Slack connector's rule and words."""
        if not self.config.attach_ack or self._announced:
            return
        self._note(attachment_notice(self._launch_settings, slack_markup=False), connection_notice=True)
        self._announced = True

    def _announce_detached(self) -> None:
        if not self._announced:
            return
        self._announced = False
        self._note(DETACH_NOTE, connection_notice=True)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._announce_detached()
        if self._bus is not None:
            with contextlib.suppress(Exception):
                self._bus.ack(self.subscriber, self._bus_cursor, final=True)
        for _ in range(2):  # the connect thread may append the loops as we join
            with self._threads_lock:
                threads, self._threads = list(self._threads), []
            for thread in threads:
                thread.join(timeout=timeout)

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                self._stop.wait(1.0)
        finally:
            self.stop()

    def _backoff(self) -> None:
        self.failures += 1
        self._stop.wait(min(30.0, 0.5 * (2 ** min(self.failures, 6))))

    # -- outbound: bus → thread --------------------------------------------------

    def _outbound_loop(self) -> None:
        questions: dict = {}
        while not self._stop.is_set():
            try:
                records, cursor = self.bus.messages(self._bus_cursor, timeout=BUS_POLL_SECONDS)
            except bus.NoChannel as exc:
                logger.info("matrix_connector_bus_gone reason=%s", exc)
                # The session is over, and that is terminal for both loops — so
                # end the *process*, not just this thread. It used to live on a
                # thread of an `lmer` process that was itself exiting; as a
                # detached process (issue #412) a connector whose loops have both
                # given up would otherwise spin forever, and its live pid keeps
                # the thread claimed against every future session.
                self._stop.set()
                break
            except bus.BusError as exc:
                logger.warning("matrix_connector_bus_error error=%s", exc)
                self._backoff()
                continue
            self.failures = 0
            delivered = self._bus_cursor
            for record in records:
                if record.get("kind") == "question":
                    questions[record.get("id")] = record
                if not self._post_outbound(record, questions):
                    # Held, not skipped: the next poll re-reads this record.
                    self._backoff()
                    break
                delivered = max(delivered, int(record.get("seq") or delivered))
            else:
                delivered = max(delivered, cursor)
            if delivered > self._bus_cursor:
                self._bus_cursor = delivered
                try:
                    self.bus.ack(self.subscriber, delivered)
                except bus.BusError:
                    pass

    def _mirror_content(self) -> dict:
        """Identify our own echoes and attribute live posts for other runs."""
        return thread_posts.matrix_content(
            thread_posts.Poster.for_session(self._posters.session)
        )

    def _post_outbound(self, record: dict, questions: dict) -> bool:
        """Put one record in the thread. ``False`` means it did not land and the
        caller must not move past it."""
        text = render_outbound(record, questions)
        if text is None:
            return True
        try:
            event_id = self.matrix.send_thread_text(
                self.room_id, self.config.thread_event, text,
                last_event_id=self._last_event_id, extra=self._mirror_content(),
            )
            self._seen.add(event_id)
            self._posters.wrote(event_id)
            self._last_event_id = event_id
            if record.get("kind") == "question" and record.get("id"):
                # Which event a human replies *to* when they mean to answer.
                self._question_events[record["id"]] = event_id
        except MatrixError as exc:
            logger.warning(
                "matrix_connector_post_failed seq=%s error=%s — holding the cursor",
                record.get("seq"), exc,
            )
            return False
        for item in record.get("attachments") or []:
            if isinstance(item, dict):
                self._post_attachment(item)
        return True

    def _post_attachment(self, item: dict) -> None:
        """Upload one of the agent's attachments into the thread — only under the
        operator's authenticated-media assertion, and only bytes the scrub passes."""
        name = str(item.get("name") or "file")
        if not self.config.authenticated_media:
            self._note(f"📎 {name} withheld: set {AUTHENTICATED_MEDIA_ENV}=1 once the homeserver requires authentication for media")
            return
        try:
            data = self._attachment_bytes(item)
        except Exception as exc:
            self._note(f"📎 {name} withheld: it is not a readable file in this session's upload store")
            logger.info("matrix_connector_attachment_unreadable name=%s error=%s", name, exc)
            return
        media_type = str(item.get("media_type") or "application/octet-stream")
        scrubbed = scrub_bytes(data, media_type)
        if scrubbed.refused:
            self._note(f"📎 {name} withheld: it carries something that looks like a credential")
            return
        clean = scrubbed.data
        try:
            mxc = self.matrix.upload(clean, content_type=media_type, filename=name)
            event_id = self.matrix.send_thread_file(
                self.room_id, self.config.thread_event, mxc=mxc, name=name,
                media_type=media_type, size=len(clean), last_event_id=self._last_event_id,
            )
            self._seen.add(event_id)
            self._last_event_id = event_id
        except MatrixError as exc:
            # A fixed phrase, not the exception: this line is posted into a
            # thread, and a homeserver's error carries URLs and paths with it.
            logger.warning("matrix_connector_attachment_upload_failed name=%s error=%s", name, exc)
            self._note(f"📎 {name} withheld: the homeserver would not take it")

    def _attachment_bytes(self, item: dict) -> bytes:
        """The bytes of one of the agent's attachments, read the way the host
        side reads any file the session named — on a descriptor, never by name
        (:func:`lmer_cli.thread_access.read_from_store`)."""
        from lmer_cli.thread_access import UploadStore, read_from_store

        return read_from_store(
            item, UploadStore.of(self.config.uploads_dir, self.config.container_uploads_dir)
        )

    def _note(self, text: str, *, connection_notice: bool = False) -> None:
        try:
            event_id = self.matrix.send_thread_text(
                self.room_id, self.config.thread_event, text, last_event_id=self._last_event_id, msgtype="m.notice",
                extra={"net.20c.lmer.connection": True} if connection_notice else None,
            )
            self._seen.add(event_id)
        except MatrixError as exc:
            logger.warning("matrix_connector_note_failed error=%s", exc)

    # -- inbound: thread → bus ---------------------------------------------------

    def _room_filter(self) -> dict:
        return {
            "room": {
                "rooms": [self.room_id],
                "timeline": {"limit": 50, "types": ["m.room.message", "m.room.encrypted"]},
                "state": {"lazy_load_members": True, "types": []},
                "ephemeral": {"types": []},
                "account_data": {"types": []},
            },
            "presence": {"types": []},
            "account_data": {"types": []},
        }

    def _inbound_loop(self) -> None:
        replayed = False
        while not self._stop.is_set():
            try:
                if not replayed:
                    self.replay()
                    replayed = True
                self.sync_once()
                self.failures = 0
            except MatrixError as exc:
                logger.warning("matrix_connector_sync_failed error=%s", exc)
                self._backoff()
            except bus.NoChannel as exc:
                logger.info("matrix_connector_bus_gone reason=%s", exc)
                # The session is over, and that is terminal for both loops — so
                # end the *process*, not just this thread. It used to live on a
                # thread of an `lmer` process that was itself exiting; as a
                # detached process (issue #412) a connector whose loops have both
                # given up would otherwise spin forever, and its live pid keeps
                # the thread claimed against every future session.
                self._stop.set()
                break
            except bus.BusError as exc:
                logger.warning("matrix_connector_bus_error error=%s", exc)
                self._backoff()

    @property
    def thread_label(self) -> str:
        """The thread identity shown in an attach notice."""
        return f"the matrix thread {self.config.thread_event} in {self.room_id}"

    def replay(self) -> int:
        """Announce the full thread snapshot and take the token for live events."""
        if self.room_id is None:
            self.connect()
        events = []
        try:
            events.append(self.matrix.get_event(self.room_id, self.config.thread_event))
        except MatrixError as exc:
            logger.warning("matrix_connector_root_unreadable event=%s error=%s", self.config.thread_event, exc)
        events.extend(self.matrix.thread_events(self.room_id, self.config.thread_event, limit=None))
        initial = self.matrix.sync(filter_body=self._room_filter(), timeout_ms=0)
        rooms = ((initial.get("rooms") or {}).get("join") or {})
        room = rooms.get(self.room_id) or {}
        timeline = ((room.get("timeline") or {}).get("events") or [])
        events.extend(
            event for event in timeline
            if isinstance(event, dict) and resolve_thread_root(event) == self.config.thread_event
        )
        snapshot = {
            event["event_id"]: event for event in events
            if isinstance(event, dict) and isinstance(event.get("event_id"), str)
            and event.get("type") in (None, "m.room.message")
            and not (event.get("sender") == self.config.user_id
                     and (event.get("content") or {}).get("net.20c.lmer.connection") is True)
        }
        ordered = sorted(snapshot.values(), key=lambda event: event.get("origin_server_ts", 0))
        posted = int(self._post_replay(ordered)) if ordered else 0
        for event in events:
            if isinstance(event.get("event_id"), str):
                self._seen.add(event["event_id"])
        self._next_batch = initial.get("next_batch")
        return posted

    def sync_once(self) -> int:
        body = self.matrix.sync(since=self._next_batch, filter_body=self._room_filter(), timeout_ms=SYNC_TIMEOUT_MS)
        rooms = ((body.get("rooms") or {}).get("join") or {})
        room = rooms.get(self.room_id) or {}
        events = ((room.get("timeline") or {}).get("events") or [])
        posted = 0
        fresh: list = []
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("type") == "m.room.encrypted":
                logger.warning("matrix_connector_encrypted_event room=%s — cannot read it", self.room_id)
                continue
            if resolve_thread_root(event) != self.config.thread_event:
                continue
            fresh.append(event)
        # Held first, as on the Slack side: a message held from an earlier poll
        # predates these, and the order decides which one takes a question's
        # first-free-text answer slot.
        for event, poster in self._posters.due():
            if self._post_inbound(event, poster):
                posted += 1
        for event in fresh:
            relay, poster = self._inbound(event)
            if relay and self._post_inbound(event, poster):
                posted += 1
        self._next_batch = body.get("next_batch") or self._next_batch
        return posted

    def _relayable(self, event: dict) -> bool:
        """Whether this is a message event this connector has not already had.
        Which thread it belongs to is the caller's question, not this one's."""
        if not isinstance(event, dict) or event.get("type") not in (None, "m.room.message"):
            return False
        if not isinstance(event.get("sender"), str):
            return False
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in self._seen:
            return False
        content = event.get("content") if isinstance(event.get("content"), dict) else {}
        return content.get("msgtype") in _TEXT_TYPES or content.get("msgtype") in _MEDIA_TYPES

    def _post_replay(self, events: list) -> bool:
        def stamp(event):
            millis = event.get("origin_server_ts")
            return replay_stamp(millis / 1000 if isinstance(millis, (int, float)) else None)

        newest = events[-1]["event_id"]
        posted = post_replay_notice(
            self.bus, channel=CHANNEL, label=self.thread_label,
            permalink=matrix_to_link(self.room_id, self.config.thread_event),
            count=len(events), oldest=stamp(events[0]), newest=stamp(events[-1]), ref=newest,
        )
        if posted:
            self._last_event_id = newest
        return posted

    def _inbound_body(self, event: dict) -> tuple:
        """One event's own text, and any media it brought into the store."""
        content = event.get("content") if isinstance(event.get("content"), dict) else {}
        msgtype = content.get("msgtype")
        if msgtype in _MEDIA_TYPES:
            staged = self._stage_media(content)
            if staged is None:
                return f"[{msgtype} {content.get('body') or 'attachment'} — not stored]", []
            return "", [staged]
        return strip_reply_fallback(str(content.get("body") or "")).strip(), []

    def _inbound(self, event: dict) -> tuple:
        """``(relay it, who posted it)`` — the poster is ``None`` for a human.

        The bot account is shared, so an event it sent is this connector's own
        only when nothing else claims it (docs/COMMS.md §9).
        """
        if not self._relayable(event):
            return False, None
        if event.get("sender") != self.config.user_id:
            return True, None
        verdict, poster = self._posters.decide(event)
        return verdict == thread_posts.INGEST, poster

    def _author(self, event: dict, poster) -> Optional[str]:
        """The host-stamped author of a live message, or its sender's MXID."""
        if poster is not None:
            return poster.author
        sender = event.get("sender")
        return sender if isinstance(sender, str) else None

    def _post_inbound(self, event: dict, poster=None) -> bool:
        event_id = str(event.get("event_id"))
        content = event.get("content") if isinstance(event.get("content"), dict) else {}
        body, attachments = self._inbound_body(event)
        text = _with_references(body, attachments)
        if not text:
            return False
        author = self._author(event, poster)
        source = {"channel": CHANNEL, "author": author, "ref": event_id}
        kind = "message"
        in_reply_to = None
        states = self.bus.questions()
        related = next(
            (state["question"] for state in states if self._replies_to(content, state["question"])),
            None,
        )
        oldest = min(
            (state["question"] for state in states if state["state"] == "open"),
            key=lambda question: question["seq"], default=None,
        )
        question = related or oldest
        if question is not None:
            bound = answer_binding(
                text, question, replies_to_question=related is not None,
                first_since_question=self._first_since(question),
            )
            if bound is not None:
                kind, in_reply_to, text = "answer", question["id"], bound
        self.bus.post(
            kind, text, source=source, in_reply_to=in_reply_to,
            attachments=[{k: v for k, v in item.items() if k != "reference"} for item in attachments],
        )
        if question is not None:
            self._answered_freely.add(question["id"])
        self._seen.add(event_id)
        self._last_event_id = event_id
        return True

    def _replies_to(self, content: dict, question: dict) -> bool:
        """Whether this event is a matrix reply to the event this connector
        posted for *question* — the one relation a human can use to say "this
        is my answer" rather than have it inferred."""
        posted = self._question_events.get(question.get("id"))
        if not posted:
            return False
        relates = content.get("m.relates_to") if isinstance(content.get("m.relates_to"), dict) else {}
        if relates.get("is_falling_back") is True:
            return False
        target = (relates.get("m.in_reply_to") or {}).get("event_id")
        if not target and relates.get("is_falling_back") is False:
            target = relates.get("event_id")
        return target == posted

    def _first_since(self, question: dict) -> bool:
        """Whether no human message has been accepted since this question."""
        qid = question.get("id")
        return bool(qid and qid not in self._answered_freely)

    def _stage_media(self, content: dict) -> Optional[dict]:
        """Copy one inbound media event into the session's upload store.

        Through the shared bounded helpers, for the reasons recorded on #416: the
        file is posted by whoever is in the room, so its size is not a question
        this side can ask first, and the store is bind-mounted ``rw`` into the
        container, so a name that is free when it is chosen can be a symlink to
        the host's own state by the time it is written. The Slack connector's
        inbound path already goes this way; this one was the same code with
        neither guard.
        """
        from lmer_cli.thread_access import MAX_FILE_BYTES, UploadStore, stage_upload

        store = UploadStore.of(self.config.uploads_dir, self.config.container_uploads_dir)
        mxc = content.get("url")
        if store is None or not isinstance(mxc, str):
            return None
        if isinstance(content.get("file"), dict):
            return None  # an encrypted attachment: nothing here can decrypt it
        name = str(content.get("filename") or content.get("body") or "file")
        info = content.get("info") if isinstance(content.get("info"), dict) else {}
        declared = info.get("size")
        if isinstance(declared, int) and not isinstance(declared, bool) and declared > MAX_FILE_BYTES:
            # What the event claims, before asking for the bytes.
            logger.info("matrix_connector_file_too_large name=%s bytes=%s", name, declared)
            return None
        try:
            data, media_type = self.matrix.download(mxc, max_bytes=MAX_FILE_BYTES)
        except MatrixError as exc:
            logger.warning("matrix_connector_download_failed mxc=%s error=%s", mxc, exc)
            return None
        if data is None:
            logger.info("matrix_connector_file_too_large name=%s bytes=over %s", name, MAX_FILE_BYTES)
            return None
        return stage_upload(store, name, data, str(info.get("mimetype") or media_type))


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lmer-matrix-connector",
        description=(
            "Mirror one matrix thread onto one session's bus. Reads LMER_SUPERVISOR_URL "
            "(or LMER_FASTAPI_PORT), LMER_FASTAPI_TOKEN, LMER_MATRIX_HOMESERVER, "
            "LMER_MATRIX_USER_ID, LMER_MATRIX_ACCESS_TOKEN and the thread from "
            "LMER_MATRIX_ROOM_ID/LMER_MATRIX_THREAD_EVENT or --permalink."
        ),
    )
    parser.add_argument("--permalink", help="matrix.to or client permalink to the thread; overrides the env pair.")
    parser.add_argument("--uploads-dir", help="Host directory of the session's upload store.")
    parser.add_argument("--container-uploads-dir", help="What the container calls that directory.")
    parser.add_argument(
        "--attach-ack", dest="attach_ack", default=None,
        action=argparse.BooleanOptionalAction,
        help=(
            "Post a one-line ack in the thread once attached (default on, "
            f"{ATTACH_ACK_ENV} overrides). Off for a session a listener spawned: "
            "it announces that thread itself."
        ),
    )
    parser.add_argument(
        "--attach-from-now", dest="attach_from_now", default=None,
        action=argparse.BooleanOptionalAction,
        help=(
            "Start reading the bus at its current position rather than at the "
            f"beginning ({ATTACH_FROM_NOW_ENV} overrides). For a connection made "
            "while the session is already running; a connector resuming after its "
            "own death continues from its acked position either way."
        ),
    )
    args = parser.parse_args(argv)
    room = thread_event = None
    if args.permalink:
        parsed = parse_matrix_permalink(args.permalink)
        if parsed is None:
            print(f"lmer-matrix-connector: not a matrix thread permalink: {args.permalink}", file=sys.stderr)
            return 2
        room, thread_event = parsed.room, parsed.event_id
    try:
        config = ConnectorConfig.from_env(
            room=room, thread_event=thread_event,
            uploads_dir=args.uploads_dir, container_uploads_dir=args.container_uploads_dir,
            attach_ack=args.attach_ack,
            attach_from_now=args.attach_from_now,
        )
    except (ValueError, bus.NoChannel) as exc:
        print(f"lmer-matrix-connector: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    connector = MatrixConnector(config)
    # SIGTERM is how the platform daemon disconnects a thread; the note this
    # connector owes the thread is posted on the way out of ``run_forever``.
    _stop_on_sigterm(connector)
    try:
        connector.run_forever()
    except MatrixError as exc:
        print(f"lmer-matrix-connector: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        connector.stop()
    return 0


def _stop_on_sigterm(connector) -> None:
    """Turn SIGTERM into an ordinary stop, in the process's main thread."""
    def handler(_signum, _frame):
        connector._stop.set()

    with contextlib.suppress(ValueError):  # not the main thread: nothing to install
        signal.signal(signal.SIGTERM, handler)


if __name__ == "__main__":
    sys.exit(main())
