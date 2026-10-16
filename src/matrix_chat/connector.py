"""Matrix thread transport for the shared connector lifecycle.

See docs/COMMS.md and docs/CONNECTOR-RECOVERY.md for delivery and recovery.
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
from lmer_cli.comms import off_thread
from lmer_cli.connector_delivery import RecoveryRequired
from lmer_cli.connector_lifecycle import ConnectorLifecycle
from lmer_cli.connector_ownership import ThreadKey
from lmer_cli.thread_access import UploadStore, outbound_file, withheld_note
from lmer_cli.util import get_bool_env
from matrix_bridge.scrub import scrub_text
from slack_chat.connector import (
    ATTACH_ACK, ATTACH_ACK_ENV, ATTACH_FROM_NOW_ENV, ATTACH_SINCE_ENV, DETACH_NOTE,
    _int_env, _with_references, answer_binding, attachment_notice, option_for,
    post_replay_notice, render_outbound as _render_slack, replay_stamp,
)

from .client import MatrixAccount, MatrixClient, MatrixError
from .permalink import matrix_to_link, parse_matrix_permalink
from lmer_cli.assistant_mentions import route_connected_mention

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
    """Thread text, or ``None`` for quiet notes and matrix's own records.

    The same rendering the Slack connector uses (numbered options, who answered
    from where, kind glyphs) keyed on *this* channel — a Slack record belongs in
    a matrix thread, and only matrix's own does not — scrubbed the way the fleet
    bridge scrubs everything it posts. A ``close`` renders as ``None`` here too:
    it stays on the bus, the platform and the transcript (issue #579).
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


class MatrixConnector(ConnectorLifecycle):
    _transport_fields = ("_seen", "_next_batch", "_last_event_id", "_question_events")
    _detach_note = DETACH_NOTE
    _bus_poll_seconds = BUS_POLL_SECONDS

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
        self._init_lifecycle()

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

    def connect(self):
        room_id = self.matrix.resolve_room(self.config.room)
        self._acquire(ThreadKey(CHANNEL, room_id, self.config.thread_event))
        try:
            try:
                self.matrix.join(room_id)
            except MatrixError as exc:
                logger.info("matrix_connector_join_skipped room=%s error=%s", room_id, exc)
            if self.matrix.room_is_encrypted(room_id):
                raise MatrixError(f"room {room_id} is end-to-end encrypted; this connector requires a plaintext room")
        except BaseException:
            self._lease.close()
            self._lease = self._state = None
            raise
        self.room_id = room_id
        return room_id


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


    # -- outbound: bus → thread --------------------------------------------------


    def _mirror_content(self) -> dict:
        """Identify our own echoes and attribute live posts for other runs."""
        return thread_posts.matrix_content(
            thread_posts.Poster.for_session(self._posters.session)
        )

    def _post_outbound(self, record, questions):
        self._ensure_ready()
        text = render_outbound(record, questions)
        if text is None:
            return True
        def remember(receipt):
            self._seen.add(receipt)
            self._posters.wrote(receipt)
            self._last_event_id = receipt
            if record.get("kind") == "question" and record.get("id"):
                self._question_events[record["id"]] = receipt
        self._deliver_part(record, "text", {"text": text},
                           lambda: self.matrix.send_thread_text(
                               self.room_id, self.config.thread_event, text,
                               last_event_id=self._last_event_id, extra=self._mirror_content()), remember)
        if (record.get("meta") or {}).get("thread"):
            return True  # its files went to the thread it names
        for index, item in enumerate(record.get("attachments") or []):
            if isinstance(item, dict):
                self._post_attachment(item, record=record, index=index)
        return True

    def _post_attachment(self, item, *, record=None, index=0):
        self._ensure_ready()
        source = record if record is not None else {"attachment": item}
        part = f"file:{index}"
        name = scrub_text(str(item.get("name") or "file"))
        def remember(receipt):
            self._seen.add(receipt)
            self._posters.wrote(receipt)
            self._last_event_id = receipt
        with self._state.outbound:
            pending = self._state.begin(source)
            existing = pending["parts"].get(part)
            if existing:
                payload = existing["payload"]
            else:
                note = None
                media_type = str(item.get("media_type") or "application/octet-stream")
                if not self.config.authenticated_media:
                    note = withheld_note(name, f"set {AUTHENTICATED_MEDIA_ENV}=1 once the homeserver requires authentication for media")
                else:
                    prepared = outbound_file(
                        item, UploadStore.of(self.config.uploads_dir, self.config.container_uploads_dir),
                    )
                    if prepared.withheld is not None:
                        note = prepared.note
                    else:
                        clean = prepared.data
                if note is not None:
                    payload = {"text": note, "index": index}
                else:
                    identity = {"index": index, "name": name, "sha256": hashlib.sha256(clean).hexdigest(),
                                "size": len(clean), "media_type": media_type}
                    with self._state.lock:
                        upload = pending.setdefault("uploads", {}).get(part)
                        if upload and upload["identity"] != identity:
                            raise RecoveryRequired("pending attachment bytes changed")
                    if upload is None:
                        mxc = self.matrix.upload(clean, content_type=media_type, filename=name)
                        upload = {"identity": identity, "mxc": mxc}
                        with self._state.lock:
                            pending["uploads"][part] = upload
                            self._state.save()
                    payload = {**identity, "mxc": upload["mxc"]}
            def send():
                if "text" in payload:
                    return self.matrix.send_thread_text(
                        self.room_id, self.config.thread_event, payload["text"],
                        last_event_id=self._last_event_id, msgtype="m.notice")
                return self.matrix.send_thread_file(
                    self.room_id, self.config.thread_event, mxc=payload["mxc"], name=payload["name"],
                    media_type=payload["media_type"], size=payload["size"], last_event_id=self._last_event_id,
                    extra=self._mirror_content())
            self._deliver_part(source, part, payload, send, remember)
            if record is None:
                self._state.finish(lambda: None)

    def _note(self, text, *, connection_notice=False):
        self._ensure_ready()
        self._notice(text, "note")

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

    def _inbound_loop(self):
        self._loop("inbound", self._inbound_once)

    @property
    def thread_label(self) -> str:
        """The thread identity shown in an attach notice."""
        return f"the matrix thread {self.config.thread_event} in {self.room_id}"

    def replay(self) -> int:
        """Announce the full thread snapshot and take the token for live events."""
        self._ensure_ready()
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
        with self._state.lock:
            for event in events:
                if isinstance(event.get("event_id"), str):
                    self._seen.add(event["event_id"])
            self._next_batch = initial.get("next_batch")
            self._state.save()
        return posted

    def sync_once(self):
        self._ensure_ready()
        resumed = self._resume_pending_inbound()
        body = self.matrix.sync(since=self._next_batch, filter_body=self._room_filter(), timeout_ms=SYNC_TIMEOUT_MS)
        rooms = ((body.get("rooms") or {}).get("join") or {})
        events = (((rooms.get(self.room_id) or {}).get("timeline") or {}).get("events") or [])
        try:
            with self._state.lock:
                inbound = []
                for event in events:
                    if event.get("type") == "m.room.encrypted":
                        logger.warning("matrix_connector_encrypted_event room=%s", self.room_id)
                        continue
                    if resolve_thread_root(event) != self.config.thread_event:
                        continue
                    relay, poster = self._inbound(event)
                    if relay:
                        inbound.append((event, poster))
                ready = self._posters.due() + inbound
                self._state.save()
            posted = resumed
            for event, poster in ready:
                if self._post_inbound(event, poster):
                    posted += 1
                with self._state.lock:
                    self._posters.ack(event)
                    self._state.save()
            with self._state.lock:
                self._next_batch = body.get("next_batch") or self._next_batch
                self._state.save()
            return posted
        finally:
            self._posters.release()

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
            with self._state.lock:
                self._last_event_id = newest
                self._state.save()
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

    def _post_inbound(self, event, poster=None):
        event_id = event.get("event_id")
        if poster is None and event.get("sender") != self.config.user_id:
            if route_connected_mention(CHANNEL, self.room_id, event, bot_id=self.config.user_id):
                with self._state.lock:
                    self._accept_inbound(event_id)
                    self._state.save()
                return True
        def prepare():
            content = event.get("content") if isinstance(event.get("content"), dict) else {}
            body, attachments = self._inbound_body(event)
            text = _with_references(body, attachments)
            if not text:
                return None
            if not event_id:
                raise ValueError("Matrix input has no event identity")
            source = {"channel": CHANNEL, "author": self._author(event, poster), "ref": event_id}
            kind, in_reply_to = "message", None
            states = [state for state in self.bus.questions() if not off_thread(state["question"])]
            related = next((state["question"] for state in states
                            if self._replies_to(content, state["question"])), None)
            oldest = min((state["question"] for state in states if state["state"] == "open"),
                         key=lambda question: question["seq"], default=None)
            question = related or oldest
            if question is not None:
                bound = answer_binding(text, question, replies_to_question=related is not None,
                                       first_since_question=self._first_since(question))
                if bound is not None:
                    kind, in_reply_to, text = "answer", question["id"], bound
            return {"question": question["id"] if question else None,
                    "human_receipt": poster is None and event.get("sender") != self.config.user_id,
                    "request": {"kind": kind, "text": text, "source": source, "in_reply_to": in_reply_to,
                                "attachments": [{k: v for k, v in item.items() if k != "reference"} for item in attachments]}}
        return bool(self._post_prepared(event_id, prepare, lambda: self._accept_inbound(event_id)))

    def _accept_inbound(self, ref):
        self._seen.add(ref)
        self._last_event_id = ref

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

    def _prepare(self):
        if self.room_id is None:
            self.connect()
        else:
            self._acquire(ThreadKey(CHANNEL, self.room_id, self.config.thread_event))

    def _attachment_notice(self):
        return attachment_notice(self._launch_settings, slack_markup=False)

    def _send_notice(self, text):
        return self.matrix.send_thread_text(self.room_id, self.config.thread_event, text,
                                           last_event_id=self._last_event_id, msgtype="m.notice",
                                           extra={"net.20c.lmer.connection": True})

    def _remember_notice(self, receipt):
        self._seen.add(receipt)
        self._posters.wrote(receipt)

    def _send_record(self, record):
        self._post_outbound(record, self._questions)

    def _send_receipt(self, ref, transaction):
        self.matrix.send_reaction(self.room_id, ref, "📥", transaction=transaction)

    def _inbound_once(self):
        if not self._replayed:
            self.replay()
            with self._state.lock:
                self._replayed = True
                self._state.save()
        self.sync_once()


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
    return connector.exit_code


def _stop_on_sigterm(connector) -> None:
    """Record SIGTERM for the main monitor."""
    def handler(_signum, _frame):
        connector._signal_stop = True

    with contextlib.suppress(ValueError):  # not the main thread: nothing to install
        signal.signal(signal.SIGTERM, handler)


if __name__ == "__main__":
    sys.exit(main())
