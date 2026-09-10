"""Slack thread transport for the shared connector lifecycle.

See docs/COMMS.md and docs/CONNECTOR-RECOVERY.md for delivery and recovery.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import html
import logging
import os
import re
import signal
import shlex
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, Optional

import requests

from ask_channel import bus
from lmer_cli import thread_posts
from lmer_cli.connector_lifecycle import ConnectorLifecycle
from lmer_cli.connector_ownership import ThreadKey
from lmer_cli.thread_requests import MAX_READ_LIMIT
from lmer_cli.util import get_bool_env

from .client import CONNECTION_NOTICE_BLOCK_PREFIX, SlackClient, SlackError
from .permalink import slack_thread_link

logger = logging.getLogger(__name__)

__all__ = [
    "ConnectorConfig", "SlackConnector", "render_outbound", "option_for",
    "post_replay_notice", "replay_stamp", "attachment_notice",
    "DEFAULT_POLL_SECONDS", "POLL_SECONDS_ENV", "SUBSCRIBER", "CHANNEL", "main",
    "ATTACH_ACK", "DETACH_NOTE", "ATTACH_ACK_ENV", "ATTACH_FROM_NOW_ENV",
    "ATTACH_SINCE_ENV",
]

CHANNEL = "slack"
SUBSCRIBER = "slack"

POLL_SECONDS_ENV = "LMER_SLACK_POLL_SECONDS"
DEFAULT_POLL_SECONDS = 3.0

#: Enable attachment and matching detach notices. Listener-owned threads disable
#: these because the listener announces their lifecycle.
ATTACH_ACK_ENV = "LMER_CHAT_ATTACH_ACK"

#: Start a new attachment at the current bus position; saved checkpoints win.
ATTACH_FROM_NOW_ENV = "LMER_CHAT_ATTACH_FROM_NOW"

#: Initial cursor when neither a durable checkpoint nor a subscriber ack exists.
ATTACH_SINCE_ENV = "LMER_CHAT_ATTACH_SINCE"

#: The two lines a connector says for itself. Deliberately short, deliberately
#: about listening rather than about a session: what a human in the thread needs
#: is whether anything is reading what they write. The matrix connector imports
#: these rather than writing its own, so the two transports cannot drift.
ATTACH_ACK = "🔗 lmer is listening in this thread."
DETACH_NOTE = "🔌 lmer has stopped listening in this thread."


def attachment_notice(launch_settings: Optional[dict], *, slack_markup: bool = True) -> str:
    """Format launch labels for Slack markup or a Matrix plain-text body."""
    if launch_settings is None:
        return ATTACH_ACK

    def label(key, unset):
        value = launch_settings.get(key)
        if not isinstance(value, str) or not value.strip():
            return unset
        text = " ".join(value.split())[:160]
        return html.escape(text, quote=False) if slack_markup else text

    return ATTACH_ACK + f" Preset: {label('preset', 'none')}; model: {label('model', 'harness default')}."


#: The supervisor caps a long-poll at 30 s.
BUS_POLL_SECONDS = 25.0

#: Slack's own limit is 40 000 characters; a thread reply is read on a phone.
MAX_SLACK_CHARS = 3500

_OPTION_NUMBER_RE = re.compile(r"^\s*(\d{1,2})[.)]?\s*$")


@dataclass(frozen=True)
class ConnectorConfig:
    supervisor_url: str
    supervisor_token: str = field(repr=False)
    channel: str
    thread_ts: str
    bot_token: str = field(repr=False)
    poll_seconds: float = DEFAULT_POLL_SECONDS
    #: See :data:`ATTACH_ACK_ENV`.
    attach_ack: bool = True
    #: See :data:`ATTACH_FROM_NOW_ENV`.
    attach_from_now: bool = False
    #: See :data:`ATTACH_SINCE_ENV`.
    attach_since: Optional[int] = None
    #: Where the session's upload store is, on this side (the host directory
    #: bound into the container), and what the container calls it — both needed
    #: to copy a Slack file in and name the path the agent will read.
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
        channel: Optional[str] = None,
        thread_ts: Optional[str] = None,
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
        channel = channel or (environ.get("LMER_SLACK_CHANNEL") or "").strip()
        thread_ts = thread_ts or (environ.get("LMER_SLACK_THREAD_TS") or "").strip()
        if not channel or not thread_ts:
            raise ValueError(
                "the connector needs a Slack thread: LMER_SLACK_CHANNEL and "
                "LMER_SLACK_THREAD_TS, or a permalink"
            )
        bot_token = (environ.get("SLACK_BOT_TOKEN") or "").strip()
        if not bot_token:
            raise ValueError("SLACK_BOT_TOKEN is required to run the Slack connector")
        raw = (environ.get(POLL_SECONDS_ENV) or "").strip()
        poll = DEFAULT_POLL_SECONDS
        if raw:
            try:
                poll = max(1.0, float(raw))
            except ValueError:
                poll = DEFAULT_POLL_SECONDS
        return cls(
            supervisor_url=supervisor_url, supervisor_token=supervisor_token,
            channel=channel, thread_ts=thread_ts, bot_token=bot_token,
            poll_seconds=poll, uploads_dir=uploads_dir,
            container_uploads_dir=container_uploads_dir,
            connect_budget=bus.connect_budget(environ),
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
        )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_LABELS = {"uber-lmer": "uber lmer"}


def _int_env(raw: object) -> Optional[int]:
    """A non-negative integer from an environment value, or ``None``."""
    text = (raw or "").strip() if isinstance(raw, str) else ""
    return int(text) if text.isdigit() else None


def _who(source: dict) -> str:
    name = _LABELS.get(source.get("channel"), source.get("channel") or "?")
    return f"{name} · {source['author']}" if source.get("author") else name


def _head(text: str, limit: int = 80) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def render_outbound(
    record: dict, questions: Optional[dict] = None, *, own_channel: str = CHANNEL,
) -> Optional[str]:
    """The thread text for one bus record, or ``None`` when the record came from
    the thread this render is for (§6 rule 2: nothing goes back where it came
    from).

    *questions* maps question id → record, for the answer lines. *own_channel*
    is whose thread is being rendered: the matrix connector renders with the
    same code and must skip matrix's records, not Slack's.
    """
    source = record.get("source") or {}
    if source.get("channel") == own_channel:
        return None
    kind = record.get("kind")
    text = str(record.get("text") or "")
    meta = record.get("meta") or {}
    agent = source.get("channel") in (None, "agent")
    if kind == "question":
        lines = [f"❓ {text}"]
        options = record.get("options") or []
        for index, option in enumerate(options, start=1):
            lines.append(f"{index}. {option}")
        lines.append(
            "_reply in this thread with a number or your own words — the first "
            "answer from any channel wins_" if options
            else "_reply in this thread — the first answer from any channel wins_"
        )
        body = "\n".join(lines)
    elif kind == "answer":
        asked = (questions or {}).get(record.get("in_reply_to")) or {}
        about = f" to “{_head(asked.get('text', ''))}”" if asked else ""
        if meta.get("late"):
            body = f"↩️ late answer{about} from {_who(source)}: {text} _(already answered)_"
        else:
            body = f"✅ answered{about} from {_who(source)}: {text}"
    elif kind == "close":
        body = "⏹ the session stopped waiting for an answer" + (f" — {text}" if text else "")
    elif kind == "milestone":
        body = f"🏁 {text}"
    elif kind == "note":
        body = f"📝 {text}"
    elif kind == "command":
        body = f"⌨️ [{_who(source)}] {text}"
    elif kind == "message":
        label = _who(source)
        if meta.get("fidelity") in ("reconstructed", "confirmed"):
            label += " · typed"
        body = text if agent else f"💬 [{label}] {text}"
        # Where it went, when it went somewhere else (issue #414): the record is
        # on this session's bus because the run said it, but it was posted into a
        # thread this session is not connected to, and a reader here would
        # otherwise take it for something said to them.
        if agent and meta.get("thread"):
            body = f"↗️ posted into {meta['thread']}:\n{body}"
    else:
        body = f"[{kind}] {text}" if agent else f"[{_who(source)}] {kind}: {text}"
    for item in record.get("attachments") or []:
        name = item.get("name") if isinstance(item, dict) else None
        if name:
            body += f"\n📎 {name}"
    if len(body) > MAX_SLACK_CHARS:
        body = body[: MAX_SLACK_CHARS - 1] + "…"
    return body


def answer_binding(
    text: str,
    question: dict,
    *,
    replies_to_question: bool = False,
    first_since_question: bool = False,
) -> Optional[str]:
    """Bind only deliberate answers; return None for ordinary conversation.

    See docs/COMMS.md for reply relations, options and the first free-text reply.
    """
    options = list(question.get("options") or [])
    if replies_to_question:
        return option_for(text, options) or text
    picked = option_for(text, options)
    if picked is not None:
        return picked
    if not options and first_since_question:
        return text
    return None


def replay_stamp(epoch_seconds: Optional[float]) -> str:
    """A history timestamp in UTC, or "time unknown" for unusable remote values."""
    try:
        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, OverflowError, OSError, ValueError):
        return "time unknown"


def _epoch(ts: object) -> Optional[float]:
    """A Slack ``ts`` as seconds, or ``None`` when it is not a number."""
    try:
        return float(ts)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _with_references(text: str, staged: Iterable[Mapping]) -> str:
    """*text* with each staged file's ``[lmer upload]`` reference after it."""
    for item in staged:
        reference = item["reference"]
        text = f"{text}\n\n{reference}" if text else reference
    return text.strip()


def post_replay_notice(
    bus_client, *, channel: str, label: str, permalink: str,
    count: int, oldest: str, newest: str, ref: str,
) -> bool:
    """Announce history without binding an answer or spending its free-text slot."""
    if not count:
        return False
    label = " ".join(label.split())
    plural = "" if count == 1 else "s"
    limit = min(count, MAX_READ_LIMIT)
    offer = f"Read up to {limit} with: " if count > limit else "Read them with: "
    text = (
        f"[replay notice] {label} has {count} earlier message{plural} "
        f"({oldest} .. {newest}). {offer}"
        f"lmer-signal read {shlex.quote(permalink)} --limit {limit}"
    )
    bus_client.post("message", text, source={"channel": channel, "ref": ref})
    return True


def option_for(text: str, options: Iterable[str]) -> Optional[str]:
    """The option a reply picks — by number or by exact text — or ``None``."""
    choices = list(options or [])
    if not choices:
        return None
    wanted = (text or "").strip().casefold()
    for option in choices:
        if option.strip().casefold() == wanted:
            return option
    match = _OPTION_NUMBER_RE.match(text or "")
    if match:
        index = int(match.group(1))
        if 1 <= index <= len(choices):
            return choices[index - 1]
    return None


# ---------------------------------------------------------------------------
# The connector
# ---------------------------------------------------------------------------


class SlackConnector(ConnectorLifecycle):
    """Two loops on two threads, one thread in Slack. See the module docstring."""

    _transport_fields = ("_cursor_ts", "_accepted_inbound")
    _detach_note = DETACH_NOTE
    _bus_poll_seconds = BUS_POLL_SECONDS

    def __init__(
        self,
        config: ConnectorConfig,
        *,
        slack: Optional[SlackClient] = None,
        bus_client: Optional[bus.BusClient] = None,
        clock: Callable[[], float] = time.monotonic,
        notify: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.config = config
        self.subscriber = SUBSCRIBER + ":" + hashlib.sha256(
            f"{config.channel}:{config.thread_ts}".encode("utf-8")
        ).hexdigest()[:16]
        self._slack = slack
        self._bus = bus_client
        self._clock = clock
        self._notify = notify
        self._stop = threading.Event()
        self._threads_lock = threading.Lock()
        self._threads: list = []
        self._names: dict = {}
        self._bot_user_id: Optional[str] = None
        self._cursor_ts: Optional[str] = None
        self._bus_cursor = 0
        self._launch_settings: Optional[dict] = None
        #: questions whose one free-text chance has been used (`_first_since`)
        self._answered_freely: set = set()
        #: Whether the thread has been told this connector is listening — and so
        #: whether it is owed the line saying it has stopped.
        self._announced = False
        #: Who wrote the app-authored messages in this thread (#427).
        self._posters = thread_posts.Attribution(
            poster_of=lambda message: thread_posts.poster_from_slack(
                self.config.channel, message,
            ),
            ref_of=lambda message: message.get("ts"),
            session_of=self._own_session,
            clock=clock,
            warn=lambda note: logger.warning(
                "slack_connector_unattributed_post note=%s", note,
            ),
        )
        self.failures = 0
        self._accepted_inbound = set()
        self._init_lifecycle()

    # -- clients, resolved lazily so construction never touches the network --

    @property
    def slack(self) -> SlackClient:
        if self._slack is None:
            self._slack = SlackClient(self.config.bot_token)
        return self._slack

    @property
    def bus(self) -> bus.BusClient:
        if self._bus is None:
            self._bus = bus.BusClient(
                bus.Endpoint(url=self.config.supervisor_url, token=self.config.supervisor_token)
            )
        return self._bus

    # -- lifecycle -------------------------------------------------------------


    def _warn(self, message: str) -> None:
        """Loud on both sides: the host `lmer` process configures no logging, so
        a connector that gives up would otherwise do it in silence."""
        logger.warning("slack_connector_gave_up reason=%s", message)
        if self._notify is not None:
            self._notify(message)


    # -- outbound: bus → Slack -------------------------------------------------


    # -- inbound: Slack → bus --------------------------------------------------

    def _inbound_loop(self):
        self._loop("inbound", self._inbound_once, interval=self.config.poll_seconds)

    @property
    def thread_label(self) -> str:
        """The thread identity shown in an attach notice."""
        return f"the slack thread {self.config.channel}:{self.config.thread_ts}"

    def _poll_thread(self, *, replay=False):
        self._ensure_ready()
        resumed = self._resume_pending_inbound()
        if self._bot_user_id is None:
            self._bot_user_id = self.slack.auth_test()
        messages = self.slack.get_replies(self.config.channel, self.config.thread_ts,
                                         oldest=None if replay else self._cursor_ts)
        try:
            if replay:
                snapshot = sorted(
                    (message for message in messages if message.get("ts") and not (
                        message.get("user") == self._bot_user_id and any(
                            isinstance(block, dict) and str(block.get("block_id", "")).startswith(CONNECTION_NOTICE_BLOCK_PREFIX)
                            for block in (message.get("blocks") or [])
                        )
                    )), key=lambda message: message["ts"],
                )
                posted = resumed + (int(self._post_replay(snapshot)) if snapshot else 0)
            else:
                with self._state.lock:
                    fresh = sorted((message for message in messages
                                    if (self._cursor_ts is None or message.get("ts", "") > self._cursor_ts)
                                    and message.get("ts") not in self._accepted_inbound
                                    and not message.get("subtype")), key=lambda message: message.get("ts", ""))
                    inbound = []
                    for message in fresh:
                        if self._is_human(message):
                            inbound.append((message, None))
                        elif message.get("user") == self._bot_user_id:
                            verdict, poster = self._posters.decide(message)
                            if verdict == thread_posts.INGEST:
                                inbound.append((message, poster))
                    ready = self._posters.due() + inbound
                    self._state.save()
                posted = resumed
                for message, poster in ready:
                    self._post_inbound(message, poster)
                    with self._state.lock:
                        self._posters.ack(message)
                        self._state.save()
                    posted += 1
            with self._state.lock:
                latest = max((message.get("ts", "") for message in messages if message.get("ts")), default=None)
                if latest and (self._cursor_ts is None or latest > self._cursor_ts):
                    self._cursor_ts = latest
                self._accepted_inbound = {ref for ref in self._accepted_inbound
                                          if self._cursor_ts is None or ref > self._cursor_ts}
                self._state.save()
            return posted
        finally:
            self._posters.release()

    def _post_replay(self, messages: list) -> bool:
        permalink = slack_thread_link(
            self.slack.workspace()["host"], self.config.channel, self.config.thread_ts,
        )
        return post_replay_notice(
            self.bus, channel=CHANNEL, label=self.thread_label, permalink=permalink,
            count=len(messages), oldest=replay_stamp(_epoch(messages[0]["ts"])),
            newest=replay_stamp(_epoch(messages[-1]["ts"])),
            ref=f"{self.config.channel}:{self.config.thread_ts}:{messages[-1]['ts']}",
        )

    def _inbound_body(self, message: dict) -> tuple:
        """One message's own text, and the files it brought into the store."""
        return (
            str(message.get("text") or "").strip(),
            self._stage_files(message.get("files") or []),
        )

    def _is_human(self, message: dict) -> bool:
        return bool(
            not message.get("bot_id")
            and message.get("user")
            and message.get("user") != self._bot_user_id
        )

    def _author(self, message: dict, poster) -> Optional[str]:
        """The host-stamped author of a live message, or its Slack display name."""
        if poster is not None:
            return poster.author
        return self._display_name(message.get("user"))

    def _mirrored(self, ts: object) -> None:
        """Identify our own echoes and attribute live posts for other runs."""
        self._posters.wrote(ts)
        poster = thread_posts.Poster.for_session(self._posters.session)
        if poster is not None and isinstance(ts, str) and ts:
            thread_posts.record(self.config.channel, ts, poster)

    def _own_session(self) -> Optional[str]:
        """This session's id, as its own supervisor gives it (#427)."""
        try:
            session = self.bus.health().get("session")
        except (bus.BusError, bus.NoChannel) as exc:
            logger.debug("slack_connector_session_unknown error=%s", exc)
            return None
        return session if isinstance(session, str) and session else None

    def _post_inbound(self, message, poster=None):
        ts = message.get("ts")
        def prepare():
            body, attachments = self._inbound_body(message)
            text = _with_references(body, attachments)
            if not text:
                return None
            if not ts:
                raise ValueError("Slack input has no source timestamp")
            source = {"channel": CHANNEL, "author": self._author(message, poster),
                      "ref": f"{self.config.channel}:{self.config.thread_ts}:{ts}"}
            kind, in_reply_to = "message", None
            oldest = self._oldest_open_question()
            if oldest is not None:
                bound = answer_binding(text, oldest, first_since_question=self._first_since(oldest))
                if bound is not None:
                    kind, in_reply_to, text = "answer", oldest.get("id"), bound
            return {"question": oldest.get("id") if oldest else None,
                    "request": {"kind": kind, "text": text, "source": source, "in_reply_to": in_reply_to,
                                "attachments": [{k: v for k, v in item.items() if k != "reference"} for item in attachments]}}
        return self._post_prepared(ts, prepare, lambda: self._accept_inbound(ts)) or {}

    def _accept_inbound(self, ref):
        self._accepted_inbound.add(ref)

    def _first_since(self, question: dict) -> bool:
        """Whether no human message has been accepted since this question."""
        qid = question.get("id")
        return bool(qid and qid not in self._answered_freely)

    def _oldest_open_question(self) -> Optional[dict]:
        states = self.bus.questions(open_only=True)
        if not states:
            return None
        states.sort(key=lambda state: (state.get("question") or {}).get("seq", 0))
        return states[0].get("question")

    def _display_name(self, user_id: Optional[str]) -> Optional[str]:
        from lmer_cli.thread_access import slack_display_name

        return slack_display_name(self.slack, user_id, self._names)

    def _stage_files(self, files: list) -> list:
        """Copy a message's files into the session's upload store, if it has one.

        The fetch is the same bounded one the read path uses
        (:func:`lmer_cli.thread_access.fetch_slack_file`): the same Slack file
        was accepted through a connected thread and refused through
        ``lmer-signal read``, which is not a distinction anything intends — and
        the unbounded side was this one.
        """
        from lmer_cli.thread_access import UploadStore, fetch_slack_file, stage_upload

        store = UploadStore.of(self.config.uploads_dir, self.config.container_uploads_dir)
        if store is None or not files:
            return []
        staged = []
        for item in files:
            if not isinstance(item, dict):
                continue
            url = item.get("url_private_download") or item.get("url_private")
            name = item.get("name") or item.get("title") or "file"
            if not url:
                continue
            data = fetch_slack_file(url, self.config.bot_token)
            if data is None:
                logger.warning("slack_connector_file_failed name=%s", name)
                continue
            record = stage_upload(
                store, name, data, item.get("mimetype") or "application/octet-stream",
            )
            if record is not None:
                staged.append(record)
        return staged

    def _prepare(self):
        self._acquire(ThreadKey(CHANNEL, self.config.channel, self.config.thread_ts))

    def _attachment_notice(self):
        return attachment_notice(self._launch_settings)

    def _send_notice(self, text):
        return self.slack.post_message(self.config.channel, self.config.thread_ts, text,
                                       connection_notice=True)

    def _remember_notice(self, receipt):
        self._mirrored(receipt)

    def _send_record(self, record):
        text = render_outbound(record, self._questions)
        if text is not None:
            self._deliver_part(record, "text", {"text": text},
                               lambda: self.slack.post_message(self.config.channel, self.config.thread_ts, text),
                               self._mirrored)

    def _inbound_once(self):
        self._poll_thread(replay=not self._replayed)
        with self._state.lock:
            self._replayed = True
            self._state.save()


# ---------------------------------------------------------------------------
# Standalone entrypoint
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lmer-slack-connector",
        description=(
            "Mirror one Slack thread onto one session's bus. Reads LMER_SUPERVISOR_URL "
            "(or LMER_FASTAPI_PORT), LMER_FASTAPI_TOKEN, SLACK_BOT_TOKEN and the thread "
            "from LMER_SLACK_CHANNEL/LMER_SLACK_THREAD_TS or --permalink."
        ),
    )
    parser.add_argument("--permalink", help="Slack thread permalink; overrides the env pair.")
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
    channel = thread_ts = None
    if args.permalink:
        from .permalink import parse_slack_permalink

        channel, thread_ts = parse_slack_permalink(args.permalink)
    try:
        config = ConnectorConfig.from_env(
            channel=channel, thread_ts=thread_ts,
            uploads_dir=args.uploads_dir, container_uploads_dir=args.container_uploads_dir,
            attach_ack=args.attach_ack,
            attach_from_now=args.attach_from_now,
        )
    except (ValueError, bus.NoChannel) as exc:
        print(f"lmer-slack-connector: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    connector = SlackConnector(config)
    # SIGTERM is how the platform daemon disconnects a thread, and the note this
    # connector owes the thread is posted on the way out of ``run_forever``. The
    # default disposition would kill the process before it could say anything.
    _stop_on_sigterm(connector)
    try:
        connector.run_forever()
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
