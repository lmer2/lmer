"""The Slack connector: one thread mirrored onto one session's bus (issue #407).

A separable component. It is given a supervisor address and token, a Slack
thread and a bot token, and it does two things until told to stop:

- **outbound** — long-polls the supervisor's ``GET /messages`` and posts every
  record that did not come from Slack into the thread, rendered for a human
  (a question with numbered options, an answer from another channel with who
  gave it, a note, a milestone), then acks its position;
- **inbound** — polls the thread's replies with the bot token and posts every
  new human message onto the bus as ``source {channel: slack, author, ref:
  channel:thread_ts:ts}`` — an ``answer`` to the oldest open question when the
  session has one (a reply that is exactly an option's number or text is that option), else a
  ``message``. Files attached to a message are copied into the session's
  upload store when one is mounted, and the record names them.

Today the host ``lmer`` process hosts it on a thread whenever a Slack thread
is a target (:class:`lmer_cli.targets.SlackThreadTargets`); the same
component runs standalone as ``lmer-slack-connector`` against
``LMER_SUPERVISOR_URL`` — a sidecar in a session Pod, later. It knows
nothing about the CLI, the platform daemon or the container: the Slack token
stays in the process that runs it.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
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
from lmer_cli.thread_requests import MAX_READ_LIMIT
from lmer_cli.util import get_bool_env

from .client import CONNECTION_NOTICE_BLOCK_PREFIX, SlackClient, SlackError
from .permalink import slack_thread_link

logger = logging.getLogger(__name__)

__all__ = [
    "ConnectorConfig", "SlackConnector", "render_outbound", "option_for",
    "post_replay_notice", "replay_stamp",
    "DEFAULT_POLL_SECONDS", "POLL_SECONDS_ENV", "SUBSCRIBER", "CHANNEL", "main",
    "ATTACH_ACK", "DETACH_NOTE", "ATTACH_ACK_ENV", "ATTACH_FROM_NOW_ENV",
    "ATTACH_SINCE_ENV",
]

CHANNEL = "slack"
SUBSCRIBER = "slack"

POLL_SECONDS_ENV = "LMER_SLACK_POLL_SECONDS"
DEFAULT_POLL_SECONDS = 3.0

#: Whether this connector announces its own attachment in the thread. Off for a
#: session a listener spawned: the listener announces that thread itself, at once
#: and with more to say (which preset the session got, how long the first reply
#: may take), and a thread must not get two announcements for one attachment.
#: It gates the **ack** only — the note below is what tells a thread that nobody
#: is listening any more, and that is true however the attachment was made.
ATTACH_ACK_ENV = "LMER_CHAT_ATTACH_ACK"

#: Whether a fresh attachment starts at the *current* bus position rather than at
#: the beginning. Set by the platform daemon for a connection made while the
#: session is already running: without it such a connector posts the run's whole
#: journal into the thread, one message per record. A connection that exists for
#: the session's whole life leaves it off — the thread is meant to have seen all
#: of it. A connector resuming after its own death ignores this either way and
#: continues from the position the supervisor has it acked at.
ATTACH_FROM_NOW_ENV = "LMER_CHAT_ATTACH_FROM_NOW"

#: Where this attachment had read up to, according to whoever remembers it for
#: the connector. The supervisor's own record of a subscriber's position is
#: fresher and wins where it exists — but a clean stop acks ``final``, which
#: *removes* that name, so after a graceful restart this is all that is left of
#: it. Without it a connection that exists for the session's whole life falls
#: back to the beginning and re-posts the whole journal into its thread.
ATTACH_SINCE_ENV = "LMER_CHAT_ATTACH_SINCE"

#: The two lines a connector says for itself. Deliberately short, deliberately
#: about listening rather than about a session: what a human in the thread needs
#: is whether anything is reading what they write. The matrix connector imports
#: these rather than writing its own, so the two transports cannot drift.
ATTACH_ACK = "🔗 lmer is listening in this thread."
DETACH_NOTE = "🔌 lmer has stopped listening in this thread."

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
        if meta.get("fidelity") == "reconstructed":
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
    """A history timestamp in UTC, or "time unknown" for an unusable value.

    Every unusable value answers "time unknown" rather than raising: the stamp
    comes from the transport — a homeserver's ``origin_server_ts`` arrives over
    federation — and ``nan``, an infinity or a year outside ``datetime``'s range
    would otherwise end the connector's inbound thread, which nothing restarts.
    """
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


class SlackConnector:
    """Two loops on two threads, one thread in Slack. See the module docstring."""

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

    def start(self) -> None:
        """Run the two loops once the bus answers, on a thread of its own.

        The host CLI starts this connector *before* it launches the container
        (``targets.py``), so for the first seconds — minutes, on a cold image —
        there is nothing listening on the supervisor's port. Dialing from
        ``start`` would hand both loops a :class:`NoChannel` they read as "the
        session is over" and end the connector before the session begins.
        """
        with self._threads_lock:
            if self._threads:
                return
            thread = threading.Thread(
                target=self._run_when_ready, name="lmer-slack-connector-connect", daemon=True,
            )
            self._threads.append(thread)
        thread.start()

    def _run_when_ready(self) -> None:
        if not self.bus.wait_ready(budget=self.config.connect_budget, stop=self._stop):
            if not self._stop.is_set():
                self._warn(
                    f"the supervisor at {self.config.supervisor_url} never came up "
                    f"within {self.config.connect_budget:.0f}s — this Slack thread "
                    "will not hear the session"
                )
            return
        self._seed_cursor()
        # Once the bus answers and before the loops: "connected" has to mean the
        # session can actually be heard, and a connector that announced itself
        # from ``start`` would say so through a whole cold start in which nothing
        # was listening.
        self._announce_attached()
        started = []
        for name, target in (("outbound", self._outbound_loop), ("inbound", self._inbound_loop)):
            thread = threading.Thread(target=target, name=f"lmer-slack-connector-{name}", daemon=True)
            thread.start()
            started.append(thread)
        with self._threads_lock:
            self._threads.extend(started)

    def _warn(self, message: str) -> None:
        """Loud on both sides: the host `lmer` process configures no logging, so
        a connector that gives up would otherwise do it in silence."""
        logger.warning("slack_connector_gave_up reason=%s", message)
        if self._notify is not None:
            self._notify(message)

    def _seed_cursor(self) -> None:
        """Where in the session's bus this attachment starts reading.

        Two facts, both from the supervisor rather than guessed here.

        **Its own acked position wins.** ``/healthz`` reports every subscriber's
        high-water mark, so a connector that died resumes exactly where it
        stopped. It is the freshest answer and the only one that needs nobody
        else to have been watching.

        **Then whatever the caller remembers** (:data:`ATTACH_SINCE_ENV`). A
        clean stop acks ``final``, which *removes* the subscriber name — so the
        answer above is gone precisely when the connector was shut down
        politely, which is what a graceful daemon restart does to every
        connector it holds. Something that outlives the process has to hold the
        position, or a connection that exists for the session's whole life comes
        back at the beginning and re-posts the whole journal.

        **A fresh attach — one nobody remembers — starts where its caller says.** Zero — the whole journal
        — is right for a connection that exists for the session's whole life: the
        thread is meant to have seen all of it, and under #407 the connector and
        the session started together anyway. It is wrong for one made *during* a
        session, which would post hours of records into the thread one message at
        a time; :data:`ATTACH_FROM_NOW_ENV` is how the caller that knows the
        difference says so, and the current ``seq`` is where such an attach
        starts. Either way the session receives a notice describing the thread's
        history, which it can read on demand.
        """
        try:
            health = self.bus.health()
        except bus.BusError as exc:
            logger.warning("slack_connector_seed_failed error=%s", exc)
            return
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
        """Tell the thread a session is listening — unless something else did.

        A post that fails is logged and forgotten: a thread that never got the
        line still works, and refusing to serve it over a missing courtesy would
        be the worse trade.
        """
        if not self.config.attach_ack or self._announced:
            return
        try:
            self._mirrored(
                self.slack.post_message(self.config.channel, self.config.thread_ts, ATTACH_ACK,
                                        connection_notice=True)
            )
        except SlackError as exc:
            logger.warning("slack_connector_ack_failed error=%s", exc)
            return
        self._announced = True

    def _announce_detached(self) -> None:
        """The other half, and unconditional where the ack was made: a disconnect,
        a session that ended and a daemon shutting down are the same fact to the
        humans in the thread — nobody is reading it any more."""
        if not self._announced:
            return
        self._announced = False
        try:
            self._mirrored(
                self.slack.post_message(self.config.channel, self.config.thread_ts, DETACH_NOTE,
                                        connection_notice=True)
            )
        except SlackError as exc:
            logger.warning("slack_connector_note_failed error=%s", exc)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._announce_detached()
        if self._bus is not None:
            # "Not waiting for me": without it the supervisor's drain grace is
            # spent in full on a subscriber that has already gone.
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

    # -- outbound: bus → Slack -------------------------------------------------

    def _outbound_loop(self) -> None:
        questions: dict = {}
        while not self._stop.is_set():
            try:
                records, cursor = self.bus.messages(self._bus_cursor, timeout=BUS_POLL_SECONDS)
            except bus.NoChannel as exc:
                logger.info("slack_connector_bus_gone reason=%s", exc)
                # The session is over, and that is terminal for both loops — so
                # end the *process*, not just this thread. It used to live on a
                # thread of an `lmer` process that was itself exiting; as a
                # detached process (issue #412) a connector whose loops have both
                # given up would otherwise spin forever, and its live pid keeps
                # the thread claimed against every future session.
                self._stop.set()
                break
            except bus.BusError as exc:
                logger.warning("slack_connector_bus_error error=%s", exc)
                self._backoff()
                continue
            self.failures = 0
            delivered = self._bus_cursor
            for record in records:
                if record.get("kind") == "question":
                    questions[record.get("id")] = record
                text = render_outbound(record, questions)
                if text is not None:
                    try:
                        self._mirrored(
                            self.slack.post_message(
                                self.config.channel, self.config.thread_ts, text,
                            )
                        )
                    except SlackError as exc:
                        # The cursor stops here: one 500 from Slack used to drop
                        # the record for good — an answer to a question the
                        # thread had asked, posted nowhere, with nobody told.
                        # The records are immutable and a re-post is cheap, so
                        # the next poll reads this one again.
                        logger.warning(
                            "slack_connector_post_failed seq=%s error=%s — holding the cursor",
                            record.get("seq"), exc,
                        )
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

    # -- inbound: Slack → bus --------------------------------------------------

    def _inbound_loop(self) -> None:
        first = True
        while not self._stop.is_set():
            try:
                self._poll_thread(replay=first)
                first = False
                self.failures = 0
            except SlackError as exc:
                logger.warning("slack_connector_read_failed error=%s", exc)
                self._backoff()
                continue
            except bus.NoChannel as exc:
                logger.info("slack_connector_bus_gone reason=%s", exc)
                # The session is over, and that is terminal for both loops — so
                # end the *process*, not just this thread. It used to live on a
                # thread of an `lmer` process that was itself exiting; as a
                # detached process (issue #412) a connector whose loops have both
                # given up would otherwise spin forever, and its live pid keeps
                # the thread claimed against every future session.
                self._stop.set()
                break
            except bus.BusError as exc:
                logger.warning("slack_connector_bus_error error=%s", exc)
                self._backoff()
                continue
            self._stop.wait(self.config.poll_seconds)

    @property
    def thread_label(self) -> str:
        """The thread identity shown in an attach notice."""
        return f"the slack thread {self.config.channel}:{self.config.thread_ts}"

    def _poll_thread(self, *, replay: bool = False) -> int:
        """Read the thread once; post what is new. Returns how many were posted —
        attach posts one notice regardless of the backlog's length."""
        if self._bot_user_id is None:
            self._bot_user_id = self.slack.auth_test()
        messages = self.slack.get_replies(self.config.channel, self.config.thread_ts, oldest=self._cursor_ts)
        if replay:
            snapshot = sorted(
                (message for message in messages if message.get("ts") and not (
                    message.get("user") == self._bot_user_id and any(
                        isinstance(block, dict)
                        and str(block.get("block_id", "")).startswith(CONNECTION_NOTICE_BLOCK_PREFIX)
                        for block in (message.get("blocks") or [])
                    )
                )),
                key=lambda message: message["ts"],
            )
            posted = int(self._post_replay(snapshot)) if snapshot else 0
        else:
            fresh = [
                message for message in messages
                if (self._cursor_ts is None or message.get("ts", "") > self._cursor_ts)
                and not message.get("subtype")
            ]
            fresh.sort(key=lambda message: message.get("ts", ""))
            inbound = []
            for message in fresh:
                if self._is_human(message):
                    inbound.append((message, None))
                elif message.get("user") == self._bot_user_id:
                    verdict, poster = self._posters.decide(message)
                    if verdict == thread_posts.INGEST:
                        inbound.append((message, poster))
            posted = 0
            for message, poster in self._posters.due() + inbound:
                self._post_inbound(message, poster)
                posted += 1
        latest = max((message.get("ts", "") for message in messages if message.get("ts")), default=None)
        if latest and (self._cursor_ts is None or latest > self._cursor_ts):
            self._cursor_ts = latest
        return posted

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

    def _post_inbound(self, message: dict, poster=None) -> dict:
        body, attachments = self._inbound_body(message)
        text = _with_references(body, attachments)
        if not text:
            return {}
        author = self._author(message, poster)
        ts = message.get("ts")
        source = {
            "channel": CHANNEL, "author": author,
            "ref": f"{self.config.channel}:{self.config.thread_ts}:{ts}" if ts else None,
        }
        kind = "message"
        in_reply_to = None
        oldest = self._oldest_open_question()
        if oldest is not None:
            bound = answer_binding(
                text, oldest, first_since_question=self._first_since(oldest),
            )
            if bound is not None:
                kind, in_reply_to, text = "answer", oldest.get("id"), bound
        result = self.bus.post(
            kind, text, source=source, in_reply_to=in_reply_to,
            attachments=[{k: v for k, v in item.items() if k != "reference"} for item in attachments],
        )
        if oldest is not None:
            self._answered_freely.add(oldest["id"])
        return result

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
    return 0


def _stop_on_sigterm(connector) -> None:
    """Turn SIGTERM into an ordinary stop, in the process's main thread."""
    def handler(_signum, _frame):
        connector._stop.set()

    with contextlib.suppress(ValueError):  # not the main thread: nothing to install
        signal.signal(signal.SIGTERM, handler)


if __name__ == "__main__":
    sys.exit(main())
