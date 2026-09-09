"""Chat-thread connections the platform owns and can change while a run is up
(issue #412).

A connection is a **record** — one session, one thread — and a connector is a
**process the daemon runs and reconciles against those records**. Connecting
writes a record; disconnecting drops one; a daemon restart reattaches, because
the records are the state and reconcile is the only thing that acts on them.

The connectors themselves are the same ``lmer-slack-connector`` /
``lmer-matrix-connector`` entrypoints the CLI hosts in-process, given a
supervisor address, that session's control token, a thread and the channel's
credentials. Those credentials come from the daemon's own environment and stay
there: a connector process is the daemon's child on the host, never anything a
container can read.

``docs/COMMS.md`` §9 has the operator-facing shape — the routes, the refusals
and what a thread is told.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from lmer_cli import thread_access
from lmer_cli.threads import ThreadError, ThreadRef, parse_thread

from slack_chat import registry as thread_registry
from slack_chat.connector import ATTACH_FROM_NOW_ENV, ATTACH_SINCE_ENV

from . import registry, session_io, uploads
from .comms import comms_dir
from .store import (
    StoreError, ensure_state_dir, logs_dir, mutating, read_json, utc_now_iso,
    write_json,
)

logger = logging.getLogger(__name__)

__all__ = [
    "THREADS_FILE", "SCHEMA", "CHANNEL_SLACK", "CHANNEL_MATRIX",
    "RECONCILE_SECONDS", "STOP_GRACE_SECONDS", "BACKOFF_MAX", "MAX_PER_SESSION",
    "ThreadError", "ThreadRef", "ThreadConnectors", "parse_thread",
    "threads_path", "register_connectors", "get_connectors", "ensure_connectors",
]

THREADS_FILE = "threads.json"
SCHEMA = 1

CHANNEL_SLACK = "slack"
CHANNEL_MATRIX = "matrix"

#: The hub's cadence, for the same reason: it is the interval at which the
#: registry is already re-read, so a connection follows its session's liveness
#: within one tick of everything else the daemon notices.
RECONCILE_SECONDS = 5.0

#: How long a connector gets to post its "disconnected" line and unwind before
#: it is killed.
STOP_GRACE_SECONDS = 5.0

BACKOFF_MAX = 30.0

#: What this daemon writes into a thread-registry entry it owns, so a later
#: incarnation can tell its own orphan from somebody else's CLI session.
OWNER = "platform"

#: The same bound :func:`lmer_cli.threads.validate_thread_targets` puts on a
#: launch list. A session's connections and its launch targets are the same
#: thing here, so one number governs both.
MAX_PER_SESSION = 16

_MODULES = {
    CHANNEL_SLACK: "slack_chat.connector",
    CHANNEL_MATRIX: "matrix_chat.connector",
}

#: Thread context the CLI sets for a container. A connector process is given its
#: thread by ``--permalink``, so these are cleared from its environment rather
#: than left to disagree with the flag.
_THREAD_CONTEXT_ENV = (
    "LMER_SLACK_CHANNEL", "LMER_SLACK_THREAD_TS", "LMER_SLACK_PERMALINK",
    "LMER_MATRIX_ROOM_ID", "LMER_MATRIX_THREAD_EVENT", "LMER_MATRIX_PERMALINK",
    "LMER_FASTAPI_PORT",
)


def threads_path() -> Path:
    return comms_dir() / THREADS_FILE


@dataclass
class _Process:
    popen: object
    ref: ThreadRef
    since: str


class ThreadConnectors:
    """The records, the processes, and the loop that keeps them in step.

    *runner* builds a process from ``(argv, env, log_path)``; *matrix_client* and
    *slack_client* build the clients the two transports are asked for a thread's
    real identity. All three are injected so the reconcile logic can be exercised
    without a Slack workspace, a homeserver or a subprocess.
    """

    def __init__(
        self,
        *,
        path: Optional[Path] = None,
        runner: Optional[Callable] = None,
        matrix_client: Optional[Callable] = None,
        slack_client: Optional[Callable] = None,
        environ: Optional[dict] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = path if path is not None else threads_path()
        self._runner = runner if runner is not None else _spawn_process
        self._matrix_client = matrix_client if matrix_client is not None else _matrix_client
        self._slack_client = slack_client if slack_client is not None else _slack_client
        self._environ = os.environ if environ is None else environ
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict = {}
        self._procs: dict = {}
        self._attempts: dict = {}
        self._next_attempt: dict = {}
        self._exits: dict = {}
        self._detail: dict = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._load()

    # -- state -----------------------------------------------------------------

    def _load(self) -> None:
        try:
            data = read_json(self.path)
        except StoreError as exc:
            logger.warning("platform_threads_unreadable path=%s error=%s", self.path, exc)
            return
        sessions = (data or {}).get("sessions")
        if not isinstance(sessions, dict):
            return
        for session_id, records in sessions.items():
            if not isinstance(session_id, str) or not isinstance(records, list):
                continue
            kept = [record for record in records if _is_record(record)]
            if kept:
                self._sessions[session_id] = kept

    def _save(self) -> None:
        snapshot = {session: list(records) for session, records in self._sessions.items()}
        try:
            ensure_state_dir(self.path.parent)
            with mutating(self.path):
                write_json(self.path, {"schema": SCHEMA, "sessions": snapshot})
        except StoreError as exc:
            logger.warning("platform_threads_unwritable path=%s error=%s", self.path, exc)

    def records(self, session_id: str) -> list:
        with self._lock:
            return [dict(record) for record in self._sessions.get(session_id, ())]

    def sessions(self) -> dict:
        with self._lock:
            return {
                session: [dict(record) for record in records]
                for session, records in self._sessions.items()
            }

    def drop_session(self, session_id: str) -> int:
        """Forget everything recorded for *session_id*. Returns how many went."""
        with self._lock:
            records = self._sessions.pop(session_id, [])
            if records:
                self._save()
        for record in records:
            self._stop_process((session_id, record["id"]))
            self._forget((session_id, record["id"]))
        return len(records)

    # -- connect / disconnect --------------------------------------------------

    def connect(self, session_id: str, permalink: object, *, by: str) -> dict:
        """Attach *session_id* to the thread *permalink* names.

        Every refusal happens before anything is written or started, so a
        refused connect leaves the fleet exactly as it was.
        """
        endpoint = self._require_control_plane(session_id)
        ref = self.prepare(permalink)
        with self._lock:
            records = self._sessions.get(session_id, [])
            if any(record["id"] == ref.id for record in records):
                raise ThreadError(
                    f"this run is already connected to that thread ({ref.id}) — "
                    "disconnect it first if you meant to reconnect",
                    status=409,
                )
            self._refuse_if_full(records)
            self._refuse_if_held_elsewhere(session_id, ref)
            record = _record(ref, by=by, origin="live")
            self._sessions.setdefault(session_id, []).append(record)
            self._save()
        self._clear_backoff((session_id, ref.id))
        self._ensure((session_id, ref.id), record, endpoint=endpoint)
        logger.info(
            "platform_thread_connected session=%s thread=%s channel=%s by=%s",
            session_id, ref.id, ref.channel, by,
        )
        return self._with_runtime(session_id, record)

    def disconnect(self, session_id: str, thread_id: str) -> dict:
        with self._lock:
            records = self._sessions.get(session_id, [])
            for index, record in enumerate(records):
                if record["id"] == thread_id:
                    records.pop(index)
                    if not records:
                        self._sessions.pop(session_id, None)
                    self._save()
                    break
            else:
                raise ThreadError(
                    f"this run has no connection {thread_id!r}", status=404,
                )
        self._stop_process((session_id, record["id"]))
        self._forget((session_id, record["id"]))
        logger.info(
            "platform_thread_disconnected session=%s thread=%s", session_id, record["id"],
        )
        return record

    def require_room(self, session_id: str):
        """A control plane, and room for one more connection. Returns the endpoint.

        What :meth:`connect` checks first — and what *opening* a thread checks
        **before it posts a root message** (issue #429), because a refusal after
        the post leaves a thread in somebody's channel that nothing is listening
        to and nobody asked for.
        """
        endpoint = self._require_control_plane(session_id)
        with self._lock:
            self._refuse_if_full(self._sessions.get(session_id, []))
        return endpoint

    def _refuse_if_full(self, records: list) -> None:
        """Caller holds the lock, where the count and the append are one step."""
        if len(records) >= MAX_PER_SESSION:
            raise ThreadError(
                f"a run may hold at most {MAX_PER_SESSION} threads; this one "
                f"already has {len(records)}",
                status=409,
            )

    def adopt_launch_targets(self, session_id: str, targets, *, by: str) -> list:
        """Record a platform spawn's thread targets as connections.

        The spawn has already happened, so this refuses nothing: a target whose
        credentials are missing or whose room is encrypted becomes a record whose
        connector will not start, and says so through the runtime block, rather
        than a session that fails to launch over a thread.
        """
        recorded = []
        for target in targets or ():
            try:
                ref = self.prepare(target, checks=False)
            except ThreadError as exc:
                logger.warning(
                    "platform_thread_launch_target_unusable session=%s error=%s",
                    session_id, exc,
                )
                continue
            with self._lock:
                records = self._sessions.setdefault(session_id, [])
                if any(record["id"] == ref.id for record in records):
                    continue
                record = _record(ref, by=by, origin="launch")
                records.append(record)
                self._save()
            recorded.append(record)
        return recorded

    def refuse_held_targets(self, targets, *, besides: Optional[str] = None) -> None:
        """Refuse a launch naming a thread another live session already holds.

        The launch-time half of :meth:`_refuse_if_held_elsewhere`, called before a
        spawn has created anything, so a refused launch leaves no session, no
        record, no connector and no line in the thread (issue #413).

        Two holders are checked — a live session's record, and a holder outside
        this platform — and a platform-owned connector whose session is *not*
        live deliberately is not: that one is an orphan winding down, which every
        rotation passes through, and :meth:`_reclaim` deals with it.
        docs/COMMS.md §9 has both, and what *besides* (the session this launch
        replaces) keeps a rotation from refusing itself over.
        """
        for target in targets or ():
            try:
                ref = self.prepare(target, checks=False)
            except ThreadError:
                # Not this check's to refuse: an unusable target is dropped with
                # a warning by adopt_launch_targets, not made fatal to a launch.
                continue
            with self._lock:
                holder = self._live_holder(ref, besides=besides)
            if holder is not None:
                raise ThreadError(
                    f"{ref.permalink} is connected to {_run_label(holder)} — a "
                    "thread has one live session at a time. Disconnect it there, "
                    "or launch this one without that thread",
                    status=409,
                )
            outside = thread_registry.thread_holder(
                *ref.registry_key(), kind=ref.channel
            )
            if outside is not None and outside.get("owner") != OWNER:
                raise ThreadError(
                    f"{ref.permalink} already has an lmer session attached outside "
                    f"this platform (host process {outside.get('pid')}) — stop it, "
                    "or disconnect the thread there, then launch this one",
                    status=409,
                )

    def prepare(self, permalink: object, *, checks: bool = True) -> ThreadRef:
        """Parse *permalink* and check this host can actually serve it.

        *checks* off parses and nothing else — no credential refusal, no alias
        resolution, no encrypted-room probe. That is :meth:`adopt_launch_targets`,
        which records what a spawn has already done rather than deciding whether
        it may happen.
        """
        ref = parse_thread(permalink)
        if not checks:
            return self._resolved(ref)
        if ref.channel == CHANNEL_SLACK:
            self._require_slack_credentials()
            # Deciding, not recording: a `not_in_channel` from the resolution's
            # own `conversations.replies` is Slack saying this token cannot see
            # the thread, and connecting anyway starts a connector mirroring the
            # run into a channel nobody invited the bot to (issue #429 review,
            # round 2). `slack_thread` splits that from "could not be asked".
            return self._slack_rooted(ref, refuse_refusals=True)
        client = self._matrix_client(self._require_matrix_credentials())
        return thread_access.matrix_resolved(ref, client)

    def _resolved(self, ref: ThreadRef) -> ThreadRef:
        """*ref* keyed by the thread its transport says it is, not by the link.

        Every guard here is a dict lookup on :attr:`ThreadRef.id` — the duplicate
        409, the one-live-session-per-thread 409, the reconcile's process table —
        so two spellings of one thread producing two ids means two connectors on
        it, and every reply twice. Both transports have such a pair, and each is
        settled by asking the transport rather than by another rule about link
        shapes:

        - **matrix**, a room given as an **alias** rather than an id: the
          homeserver's directory answers. A room id needs no call at all, which
          is what most permalinks carry.
        - **Slack**, a link to a message *inside* a thread with no ``thread_ts``
          on it: it parses as its own root. ``conversations.replies`` takes "a
          message in the thread" and every threaded message carries ``thread_ts``,
          the parent's — so Slack names the root and the link does not have to.

        Best-effort in both directions: a transport that cannot be asked leaves
        the ref as the link spelled it, because a directory lookup must not be
        what makes a spawn fail.
        """
        if ref.channel == CHANNEL_SLACK:
            return self._slack_rooted(ref)
        if not ref.key["room"].startswith("#"):
            return ref
        try:
            room_id = self._matrix_client(self._require_matrix_credentials()).resolve_room(
                ref.key["room"]
            )
        except Exception as exc:
            logger.warning(
                "platform_thread_alias_unresolved room=%s error=%s", ref.key["room"], exc,
            )
            return ref
        return ThreadRef(
            channel=ref.channel,
            key={"room": room_id, "event": ref.key["event"]},
            permalink=ref.permalink,
            label=ref.label,
        )

    def _slack_rooted(self, ref: ThreadRef, *, refuse_refusals: bool = False) -> ThreadRef:
        """*ref* keyed by the thread root Slack reports for it.

        *refuse_refusals* is :func:`lmer_cli.thread_access.slack_thread`'s, and
        reaches only :meth:`prepare`'s deciding path — a client that cannot even
        be built stays best-effort here whichever way it is set, because that is
        this host's own failure and not Slack's answer.
        """
        try:
            client = self._slack_client((self._environ.get("SLACK_BOT_TOKEN") or "").strip())
        except Exception as exc:
            logger.warning("platform_thread_root_unresolved error=%s", exc)
            return ref
        return thread_access.slack_rooted(ref, client, refuse_refusals=refuse_refusals)

    def drop_run(self, identity: tuple) -> int:
        """Forget the connections of every session belonging to *identity*.

        Forgetting a run drops what this orchestrator knew about it, and a thread
        it was mirrored into is part of that. The ordinary case is covered by the
        prune — a finished session leaves the registry and its records go with it
        — but a *crashed* session's entry is deliberately kept as the crash
        signal, so its records would outlive the run that was forgotten.
        """
        from . import runs

        dropped = 0
        for session_id in list(self.sessions()):
            try:
                if runs.run_for_session(session_id) != identity:
                    continue
            except Exception:
                continue
            dropped += self.drop_session(session_id)
        return dropped

    def _require_control_plane(self, session_id: str):
        try:
            return session_io.control_endpoint(session_id)
        except session_io.ControlUnavailable as exc:
            raise ThreadError(str(exc), status=409) from None
        except session_io.SessionIOError as exc:
            raise ThreadError(str(exc), status=getattr(exc, "status", 409)) from None

    def _require_slack_credentials(self) -> None:
        thread_access.slack_token(self._environ)

    def _require_matrix_credentials(self):
        return thread_access.matrix_account(self._environ)

    def _live_holder(self, ref: ThreadRef, *, besides: Optional[str] = None) -> Optional[str]:
        """The live session whose records claim *ref*, or ``None``.

        The records rather than the thread registry: a record is what this daemon
        decided, and it is true from the moment a connection is made rather than
        from the moment its connector gets as far as registering.
        """
        for other, records in self._sessions.items():
            if other == besides:
                continue
            if not any(record["id"] == ref.id for record in records):
                continue
            if registry.is_live(registry.read_session(other)):
                return other
        return None

    def _refuse_if_held_elsewhere(self, session_id: str, ref: ThreadRef) -> None:
        """A thread maps to one live session (operator decision, 2026-09-05).

        Two runs answering into one thread makes a human's reply ambiguous —
        neither the binding rule nor first-answer-wins spans sessions — so the
        rule the Slack listener already enforces for spawning holds for
        connecting too. The refusal names the holder, because the operator's next
        move is to disconnect it there.
        """
        holder_session = self._live_holder(ref, besides=session_id)
        if holder_session is not None:
            raise ThreadError(
                f"that thread is connected to {_run_label(holder_session)} — "
                "disconnect it there first, then connect it here",
                status=409,
            )
        holder = thread_registry.thread_holder(*ref.registry_key(), kind=ref.channel)
        if holder is None:
            return
        if holder.get("owner") == OWNER:
            # This daemon's own connector, for a session the scan above did not
            # find live — the seconds between a session dying and the reconcile
            # noticing, or an orphan of a previous incarnation. Telling the
            # operator to go and stop it would be wrong on both counts; what
            # helps is knowing it is on its way out.
            raise ThreadError(
                "that thread is still held by a connector this daemon is winding "
                f"down (host process {holder.get('pid')}) — try again in a moment",
                status=409,
            )
        raise ThreadError(
            "that thread already has an lmer session attached outside this "
            f"platform (host process {holder.get('pid')}) — stop it, or "
            "disconnect the thread there, then connect it here",
            status=409,
        )

    # -- reading ---------------------------------------------------------------

    def listing(self, session_id: str) -> list:
        """One session's connections, each with what its connector is doing."""
        return [self._with_runtime(session_id, record) for record in self.records(session_id)]

    def _with_runtime(self, session_id: str, record: dict) -> dict:
        key = (session_id, record["id"])
        with self._lock:
            process = self._procs.get(key)
            running = process is not None and process.popen.poll() is None
            view = dict(record)
            view["running"] = running
            view["since"] = process.since if running else None
            view["last_exit"] = self._exits.get(key)
            view["detail"] = self._detail.get(key)
        return view

    # -- the loop --------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._reconcile_forever, name="lmer-thread-connectors", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        for key in list(self._procs):
            self._stop_process(key)

    def _reconcile_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile()
            except Exception as exc:  # the loop must outlive one bad tick
                logger.warning("platform_thread_reconcile_failed error=%s", exc)
            self._stop.wait(RECONCILE_SECONDS)

    def reconcile(self) -> None:
        """Make the running processes equal the records of live sessions."""
        entries = {
            entry.get("id"): entry
            for entry in registry.list_sessions(live_only=False)
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        }
        self._prune(entries)
        live = {
            session_id for session_id, entry in entries.items() if registry.is_live(entry)
        }
        with self._lock:
            wanted = {
                (session_id, record["id"]): record
                for session_id, records in self._sessions.items()
                if session_id in live
                for record in records
            }
            running = list(self._procs)
        for key in running:
            if key not in wanted:
                self._stop_process(key)
        for key, record in wanted.items():
            self._ensure(key, record)
        self._persist_cursors(live)

    def _persist_cursors(self, live: set) -> None:
        """Remember how far each attachment has read, where a stop cannot erase it.

        The supervisor holds every subscriber's position, which is the freshest
        answer — but a connector's *clean* stop acks ``final``, and that **removes
        the name** (``MessageLog.ack``). So a graceful daemon restart, which
        SIGTERMs every connector and lets each one stop cleanly, erased exactly
        the fact the next start needed: a launch-origin connection then fell back
        to the beginning and re-posted the session's whole journal into the
        thread. Measured on !290 review iteration 2, and reproduced here before
        this was written.

        A subscriber's name is its connection's id — the same digest of the same
        key — so one ``/healthz`` per session answers for every connection on it,
        and the record is where the answer survives the process that produced it.
        Only sessions that have connections are asked, and the file is written
        once per pass and only when something moved.
        """
        moved = False
        for session_id in sorted(live):
            with self._lock:
                records = list(self._sessions.get(session_id, ()))
            if not records:
                continue
            health = session_io._quiet_health(session_id)
            comms = (health or {}).get("comms")
            subscribers = comms.get("subscribers") if isinstance(comms, dict) else None
            if not isinstance(subscribers, dict):
                continue
            with self._lock:
                for record in self._sessions.get(session_id, ()):
                    seq = subscribers.get(record["id"])
                    if not isinstance(seq, int) or isinstance(seq, bool):
                        continue
                    if seq > record.get("cursor", -1):
                        record["cursor"] = seq
                        moved = True
        if moved:
            with self._lock:
                self._save()

    def _prune(self, entries: dict) -> None:
        """Drop records for sessions this host no longer knows about.

        Which is also how "forgetting a run drops its records" is honoured
        without the forget path having to know this module exists: a forgotten
        run's session is either already gone from the registry or goes when it
        ends, and its records go with the next tick.
        """
        if not entries:
            # A listing that came back empty is more likely a read that failed
            # than a host that has forgotten every session it ever ran, and the
            # records are a few hundred bytes: the next tick that sees anything
            # at all prunes them.
            return
        with self._lock:
            gone = [session for session in self._sessions if session not in entries]
            if not gone:
                return
            for session in gone:
                self._sessions.pop(session, None)
            self._save()
        for session in gone:
            for key in [k for k in list(self._exits) + list(self._detail) if k[0] == session]:
                self._forget(key)
            logger.info("platform_thread_records_pruned session=%s", session)

    # -- processes -------------------------------------------------------------

    def _ensure(self, key: tuple, record: dict, *, endpoint=None) -> None:
        with self._lock:
            process = self._procs.get(key)
            if process is not None:
                code = process.popen.poll()
                if code is None:
                    return
                self._procs.pop(key, None)
                self._exits[key] = code
                self._penalise(key)
                logger.info(
                    "platform_thread_connector_exited session=%s thread=%s code=%s",
                    key[0], key[1], code,
                )
                # Its claim on the thread goes with it. The pid is dead, so the
                # entry already reads as free — but leaving it makes the next
                # start reason about a pid nobody owns, which is the shape the
                # reclaim above exists to be careful about.
                thread_registry.deregister(
                    *process.ref.registry_key(),
                    kind=process.ref.channel,
                    pid=getattr(process.popen, "pid", None),
                )
            if self._clock() < self._next_attempt.get(key, 0.0):
                return
            # Inside the lock: a connect calls this too, and outside it a
            # reconcile tick landing between the poll and the spawn would start a
            # second connector for one connection. The spawn is a fork, and every
            # slow step (the stop's wait, the whole of a connector's life) is
            # outside.
            self._start(key, record, endpoint=endpoint)

    def _start(self, key: tuple, record: dict, *, endpoint=None) -> None:
        session_id, thread_id = key
        if endpoint is None:
            try:
                endpoint = session_io.control_endpoint(session_id)
            except session_io.SessionIOError as exc:
                self._fail(key, str(exc))
                return
        ref = _ref_of(record)
        held = self._reclaim(ref, key)
        if held is not None:
            self._fail(key, held)
            return
        try:
            argv = self._argv(record, session_id)
            popen = self._runner(
                argv,
                self._environment(endpoint, session_id, record),
                self._log_path(record),
            )
        except Exception as exc:
            self._fail(key, f"the connector process would not start: {exc}")
            return
        with self._lock:
            self._procs[key] = _Process(popen=popen, ref=ref, since=utc_now_iso())
            self._detail.pop(key, None)
            self._attempts.pop(key, None)
            self._next_attempt.pop(key, None)
            # Including the last exit code: a connection that died once and came
            # back reported `running: true` beside it for the rest of the
            # daemon's life, because only `connect()` ever cleared it.
            self._exits.pop(key, None)
        thread_registry.register(
            *ref.registry_key(),
            permalink=ref.permalink,
            pid=getattr(popen, "pid", None),
            kind=ref.channel,
            owner=OWNER,
        )
        logger.info(
            "platform_thread_connector_started session=%s thread=%s pid=%s",
            session_id, thread_id, getattr(popen, "pid", None),
        )

    def _argv(self, record: dict, session_id: str) -> list:
        ref = _ref_of(record)
        argv = [
            sys.executable, "-m", _MODULES[record["channel"]],
            # The thread as resolved, not as pasted: see ``ThreadRef.canonical``.
            "--permalink", ref.canonical,
        ]
        store = self._upload_store(session_id)
        if store is not None:
            argv += [
                "--uploads-dir", str(store),
                "--container-uploads-dir", uploads.CONTAINER_UPLOADS_DIR,
            ]
        return argv

    def _log_path(self, record: dict) -> Path:
        """Where one connector's own output lands, beside the session logs."""
        name = str(record["id"]).replace(":", "-").replace("/", "-")
        return logs_dir() / f"connector-{name}.log"

    def _upload_store(self, session_id: str) -> Optional[Path]:
        try:
            return uploads.mounted_store(session_id, registry.read_session(session_id))
        except Exception:
            # A session with no store still gets a connector: what it loses is
            # files from the thread, not the conversation.
            return None

    def _environment(self, endpoint, session_id: str, record: dict) -> dict:
        env = dict(self._environ)
        for name in _THREAD_CONTEXT_ENV:
            env.pop(name, None)
        # Which kind of attachment this is, told to the one process that has to
        # act on it: a connection made *during* a session mirrors the
        # conversation from now on, while a launch target is meant to have seen
        # the whole of it. Without this a live connect posts the run's whole
        # journal into the thread, one message per record.
        env[ATTACH_FROM_NOW_ENV] = "1" if record.get("origin") == "live" else "0"
        # Where this attachment had read up to, if it has ever read anything. The
        # supervisor's own answer is fresher and wins when it is there; this is
        # what is left after a clean stop has taken that answer away.
        cursor = record.get("cursor")
        if isinstance(cursor, int) and not isinstance(cursor, bool):
            env[ATTACH_SINCE_ENV] = str(cursor)
        env["LMER_SUPERVISOR_URL"] = f"http://{endpoint.host}:{endpoint.port}"
        # In the environment and not in argv: argv is readable from /proc by
        # every account on the host, and this is the credential that opens the
        # session's control plane.
        env["LMER_FASTAPI_TOKEN"] = endpoint.token
        return env

    def _forget(self, key: tuple) -> None:
        """Drop every runtime fact about a connection that no longer exists.

        These are keyed per (session, thread) and nothing else prunes them, so a
        host that has run many sessions would otherwise keep one entry each,
        forever, for connections it has already forgotten.
        """
        with self._lock:
            self._exits.pop(key, None)
            self._detail.pop(key, None)
            self._attempts.pop(key, None)
            self._next_attempt.pop(key, None)

    def _stop_process(self, key: tuple) -> None:
        with self._lock:
            process = self._procs.pop(key, None)
            self._attempts.pop(key, None)
            self._next_attempt.pop(key, None)
        if process is None:
            return
        popen = process.popen
        try:
            if popen.poll() is None:
                # SIGTERM rather than a kill: it is what gives the connector the
                # seconds it needs to post its "disconnected" line in the thread.
                popen.terminate()
                try:
                    popen.wait(timeout=STOP_GRACE_SECONDS)
                except Exception:
                    popen.kill()
                    popen.wait(timeout=STOP_GRACE_SECONDS)
        except Exception as exc:
            logger.warning(
                "platform_thread_connector_stop_failed session=%s thread=%s error=%s",
                key[0], key[1], exc,
            )
        thread_registry.deregister(
            *process.ref.registry_key(),
            kind=process.ref.channel,
            pid=getattr(popen, "pid", None),
        )

    def _reclaim(self, ref: ThreadRef, key: tuple) -> Optional[str]:
        """Clear the way for a connector for *ref*. ``None`` when the way is clear.

        A connector is a detached process (``start_new_session``), so it outlives
        a daemon that dies by signal. A restart would otherwise start a *second*
        one for every reattached connection — both holding valid control tokens,
        both with their own cursor, so every record posted twice and every human
        reply injected twice. Reconcile never reaches :meth:`connect`'s holder
        check, and ``thread_registry.register`` overwrites by contract, so
        nothing else would notice.

        A pid is not enough to act on. The registry documents pid reuse as an
        accepted edge because its only consumer *reads* — reporting a thread busy
        when it is free is the safe direction. Signalling on the same evidence is
        not safe, so the pid is checked against what the process actually is
        (:meth:`_is_our_connector`) before anything is sent to it; an entry that
        does not match is a stale record over a recycled pid, and the thread is
        free.

        Nothing is waited for: the answer is "not yet", the backoff brings the
        next attempt round, and the reason is on the record meanwhile — waiting
        would hold this component's lock for seconds per orphan on the one pass
        that has the most of them.
        """
        holder = thread_registry.thread_holder(*ref.registry_key(), kind=ref.channel)
        if holder is None:
            return None
        pid = holder.get("pid")
        if holder.get("owner") != OWNER or not isinstance(pid, int):
            return (
                f"that thread is held by an lmer session this daemon did not "
                f"start (host process {pid}); connect it here once that one has "
                "let it go"
            )
        running = self._connector_holding(pid, ref, except_key=key)
        if running is not None and registry.is_live(registry.read_session(running[0])):
            # Not an orphan but a live session's connector. Signalling it is
            # what made two registrations trade the connector back and forth
            # until one session ended (issue #413).
            return (
                f"that thread is connected to {_run_label(running[0])} — a thread "
                "has one live session at a time, and this one attaches when that "
                "session lets it go"
            )
        ours = self._is_our_connector(pid, ref)
        if ours is None:
            # Unreadable is not the same answer as "not ours", and treating it as
            # one starts a second connector on the thread — the duplicate this
            # whole path exists to prevent. Nothing is signalled and nothing is
            # started: the claim stands until something can be established about
            # the process holding it.
            return (
                f"a live process holds the thread (host process {pid}) and this "
                "daemon cannot read what it is; nothing will be started on that "
                "thread until it goes"
            )
        if not ours:
            logger.info(
                "platform_thread_stale_claim thread=%s pid=%s — the entry names a "
                "connector, the process is not one", ref.id, pid,
            )
            return None
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            logger.warning("platform_thread_orphan_unkillable pid=%s error=%s", pid, exc)
            return None
        logger.info("platform_thread_orphan_reclaimed thread=%s pid=%s", ref.id, pid)
        return (
            f"a connector this daemon started earlier still holds the thread "
            f"(host process {pid}); it has been asked to stop"
        )

    def _connector_holding(
        self, pid: int, ref: ThreadRef, *, except_key: tuple
    ) -> Optional[tuple]:
        """The connection whose running connector is *pid* **on *ref*'s thread**.

        This daemon's own process table, so the answer is what it started rather
        than what a registry entry claims — and a pid it is not running is a pid
        it may still have to reclaim.

        The thread is half the question, not decoration: a pid alone would answer
        for a connector on a *different* thread that inherited the pid from a
        stale claim, and this stands in front of :meth:`_is_our_connector`, which
        is the check for exactly that.
        """
        with self._lock:
            for key, process in self._procs.items():
                if key == except_key or process.ref.id != ref.id:
                    continue
                if getattr(process.popen, "pid", None) != pid:
                    continue
                return key if process.popen.poll() is None else None
        return None

    def _is_our_connector(self, pid: int, ref: ThreadRef) -> Optional[bool]:
        """Whether *pid* really is a connector process for *ref*'s thread.

        Three answers, not two, and the third is the one that matters: ``True``
        is a match, ``False`` is a process that is demonstrably something else —
        a recycled pid — and ``None`` is **not knowing**, where the argv cannot
        be read at all. Collapsing ``None`` into ``False`` reads an absence of
        evidence as evidence, and the cost of that mistake is the duplicate
        connector the caller is trying to avoid.

        Read off the process rather than believed from the registry entry that
        named it: the argv this module built carries the module name and the
        thread's canonical permalink, so a match is evidence.
        """
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
        except FileNotFoundError:
            # The pid is gone between the liveness read and this one — a race
            # this side loses harmlessly: no process, no holder, no duplicate.
            return False
        except OSError:
            return None
        return _MODULES[ref.channel] in argv and ref.canonical in argv

    def _fail(self, key: tuple, detail: str) -> None:
        with self._lock:
            self._detail[key] = detail
            self._penalise(key)
        logger.warning(
            "platform_thread_connector_unavailable session=%s thread=%s reason=%s",
            key[0], key[1], detail,
        )

    def _penalise(self, key: tuple) -> None:
        attempts = self._attempts.get(key, 0) + 1
        self._attempts[key] = attempts
        self._next_attempt[key] = self._clock() + min(
            BACKOFF_MAX, 0.5 * (2 ** min(attempts, 6))
        )

    def _clear_backoff(self, key: tuple) -> None:
        with self._lock:
            self._attempts.pop(key, None)
            self._next_attempt.pop(key, None)
            self._detail.pop(key, None)
            self._exits.pop(key, None)


def _is_record(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("id"), str)
        and value.get("channel") in _MODULES
        and isinstance(value.get("permalink"), str)
        and isinstance(value.get("key"), dict)
    )


def _record(ref: ThreadRef, *, by: str, origin: str) -> dict:
    """The persisted shape, in one place.

    It is a schema — what :func:`_is_record` validates and what the routes and
    the fleet payload serialise — and it was written out longhand at both the
    places that create one, so a field added to one and not the other reads back
    as valid and is missing something. :func:`_ref_of` is its inverse.
    """
    return {
        "id": ref.id,
        "channel": ref.channel,
        "permalink": ref.permalink,
        "label": ref.label,
        "key": dict(ref.key),
        "connected_at": utc_now_iso(),
        "by": by,
        "origin": origin,
    }


def _ref_of(record: dict) -> ThreadRef:
    return ThreadRef(
        channel=record["channel"],
        key=dict(record["key"]),
        permalink=record["permalink"],
        label=record.get("label") or record["permalink"],
    )


def _run_label(session_id: str) -> str:
    """How a refusal names the run holding a thread.

    The operator's next move is to go and disconnect it there, so the name has
    to be the one the fleet list shows: the run's title when it has one, and
    always the ``host/project/slug`` that finds it.
    """
    from lmer_cli import runref

    from . import meta, runs

    identity = runs.run_for_session(session_id)
    if identity is None:
        return f"session {session_id}"
    key = runref.compose(*identity)
    try:
        title = meta.read(*identity).title
    except Exception:
        title = None
    return f"{title} ({key})" if title else key


def _spawn_process(argv: list, env: dict, log_path: Optional[Path] = None):
    """The connector, as the daemon's own child.

    ``sys.executable -m`` rather than the ``lmer-slack-connector`` console
    script: the daemon already imports these packages, so its own interpreter is
    the one environment guaranteed to have them, and a console script hidden by
    a PATH difference would surface as "the thread went quiet" long after the
    cause.

    Its output goes to a file rather than to ``/dev/null``. ``last_exit`` and
    ``detail`` cover a process that died and one that would not start; the case
    they cannot reach is a connector that is *running* and cannot post — a Slack
    ``missing_scope``, a bus error, a refused ack — which is the "connected but
    silent" state the runtime block exists for, and the only place it says so is
    its own stderr.
    """
    stream = subprocess.DEVNULL
    if log_path is not None:
        try:
            ensure_state_dir(log_path.parent)
            stream = open(log_path, "ab")  # noqa: SIM115 - owned by the child
        except OSError as exc:
            logger.warning("platform_thread_log_unwritable path=%s error=%s", log_path, exc)
            stream = subprocess.DEVNULL
    try:
        return subprocess.Popen(  # noqa: S603 - argv is built here, never from a body
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        if stream is not subprocess.DEVNULL:
            stream.close()


def _slack_client(token: str):
    from slack_chat.client import SlackClient

    return SlackClient(token)


def _matrix_client(account):
    from matrix_chat.client import MatrixClient

    return MatrixClient(account)


_LOCK = threading.Lock()
_CONNECTORS: Optional[ThreadConnectors] = None


def register_connectors(connectors: Optional[ThreadConnectors]) -> None:
    global _CONNECTORS
    with _LOCK:
        _CONNECTORS = connectors


def get_connectors() -> Optional[ThreadConnectors]:
    """The registered component, or ``None`` — for a caller with somewhere else
    to go (the spawn's bookkeeping, which must not fail a running session)."""
    with _LOCK:
        return _CONNECTORS


def ensure_connectors() -> ThreadConnectors:
    """The registered component, or a passive one registered now.

    The daemon registers a started component before it serves; an app built
    without one (tests, an embedding caller) still needs the records and the
    refusals, so the routes get one whose reconcile thread is simply never
    started — the same bargain :func:`lmer_platform.comms.ensure_hub` makes.
    """
    global _CONNECTORS
    with _LOCK:
        if _CONNECTORS is None:
            _CONNECTORS = ThreadConnectors()
        return _CONNECTORS
