"""Human activity on the bus drives automatic time (issue #604, spec D5, D10).

:class:`ActivityConsumer` is both a :meth:`lmer_platform.comms.CommsHub.add_consumer`
callable and a daemon component. The hub calls it inside ``_ingest`` on a
subscriber's thread, so that call only classifies a record and queues it; the
component's own thread, woken at once, resolves the project, calls
:meth:`lmer_platform.timetrack.Tracker.note_activity`, and runs the idle sweep
(:meth:`~lmer_platform.timetrack.Tracker.close_idle`) every
``time_sweep_seconds`` and once at start-up (D1, D7).

- **Activity (D5)** is a ``message`` or ``answer`` on ``slack``, ``matrix`` or
  ``platform`` whose author the person registry maps. Its time is the record's
  own ``at``. A human the registry does not list is remembered for status
  (:meth:`ActivityConsumer.unmapped_identities`) and starts nothing.
- **One message, many sessions (D10).** Records sharing a key, ``(channel,
  source.ref)`` or else the record id, are one activity, decided at its first
  copy; a later copy is dropped. Each session's subscriber delivers on its own
  schedule, so the decision never depends on which copies have arrived.
- **Which project.** A Slack or Matrix message's thread is found in the
  connector bindings, and so is every session bound to it: the thread's
  owner (:func:`lmer_platform.project_threads.ownership`) wins; with no owner,
  the bound sessions' home. More than one project is ambiguous, and so is a
  thread no session is bound to: nothing starts or switches. A Matrix binding
  kept under an ``#alias`` is matched by the room id the alias resolves to
  (kept until the daemon restarts). One that cannot be resolved for now makes
  the message ambiguous only if its thread root is the matched thread's, or
  nothing matched; an alias that names no room matches nothing. A Matrix ref
  from a connector started before the thread tag names no thread, so it is
  ambiguous unless the session that recorded it has one Matrix thread. A
  web-chat record reaches one session and counts for its home.
- **Which runs (issue #648).** The credited sessions are those the decision
  weighed whose home is the credited project. Each one's run
  (:func:`session_refs`) goes to the tracker, and the entry the timer becomes
  names it. A session with no run the platform knows contributes nothing.
- **Which run the activity is for (issue #665, D21).** Only a credited
  session that is not the assistant and has a run the platform tracks names
  a candidate run (:func:`run_candidates`); the tracker picks the entry's run
  among them (:func:`lmer_platform.timetrack.select_run`). Ratchet has no
  run, so its messages are run-less and extend the running entry (D23).

See docs/TIME-TRACKING.md.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from lmer_cli.comms import CHANNEL_MATRIX, CHANNEL_PLATFORM, CHANNEL_SLACK, KIND_ANSWER, KIND_MESSAGE
from lmer_cli.threads import ThreadError
from lmer_timesheet import SessionRef
from matrix_chat.permalink import split_thread_ref, thread_tag

from . import connectors, project_threads, projects, registry, runs, timetrack_runs
from .config import TIME_INT_SETTINGS, PlatformConfig, time_settings
from .people import Registry
from .timetrack import Result, Tracker

logger = logging.getLogger("lmer_platform.timetrack_activity")

__all__ = [
    "MAX_RECENT_KEYS", "MAX_UNMAPPED", "ActivityConsumer", "Decision",
    "activity_key", "session_refs", "run_candidates", "register_activity", "get_activity",
]

#: How many decided keys are remembered, so a late copy of a message is not
#: counted a second time.
MAX_RECENT_KEYS = 4096
MAX_UNMAPPED = 100
STOP_TIMEOUT_SECONDS = 10.0
_UNKNOWN = object()

_KINDS = (KIND_MESSAGE, KIND_ANSWER)
_CHANNELS = (CHANNEL_SLACK, CHANNEL_MATRIX, CHANNEL_PLATFORM)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def activity_key(message: dict) -> Optional[tuple]:
    """The key one human message has in every session that records it (D10)."""
    source = message.get("source") or {}
    ref = source.get("ref")
    if isinstance(ref, str) and ref:
        if source.get("channel") == CHANNEL_MATRIX:
            ref = split_thread_ref(ref)[1]
        return ("ref", source.get("channel"), ref)
    identifier = message.get("id")
    if isinstance(identifier, str) and identifier:
        return ("id", identifier)
    return None


def _record_time(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


@dataclass(frozen=True)
class _Pending:
    person: str
    channel: str
    ref: Optional[str]
    at: datetime
    session: str


@dataclass(frozen=True)
class Decision:
    """What one activity came to: the project credited and the tracker's
    :class:`~lmer_platform.timetrack.Result`, or why no project was.

    ``sessions`` are those the decision weighed: every session bound to the
    message's thread, or the one session a web-chat record reached.
    ``refs`` are the runs of those homed in the credited project, as the
    tracker was given them.
    """

    key: tuple
    person: str
    at: datetime
    sessions: tuple
    workspace_id: Optional[str] = None
    result: Optional[Result] = None
    reason: Optional[str] = None
    refs: tuple = ()
    #: The candidate runs (:class:`~lmer_platform.timetrack_runs.RunFacts`).
    runs: tuple = ()


class ActivityConsumer:
    """Classifies hub records, settles their keys and drives the tracker.

    *now* is injectable, and :meth:`tick`, :meth:`process` and :meth:`sweep`
    are what the thread runs, so a test drives every decision synchronously
    without waiting on the wall clock.
    """

    def __init__(self, config: PlatformConfig, tracker: Tracker, registry: Registry, *,
                 now: Callable[[], datetime] = _utc_now) -> None:
        self.config = config
        self.tracker = tracker
        self.registry = registry
        self._now = now
        self._lock = threading.Lock()
        self._pending: OrderedDict = OrderedDict()
        self._decided: OrderedDict = OrderedDict()
        self._unmapped: OrderedDict = OrderedDict()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._next_sweep: Optional[datetime] = None

    # -- the hub's side --------------------------------------------------------

    def __call__(self, event) -> None:
        """Queue *event* (a :class:`lmer_platform.comms.FleetEvent`) if it is activity."""
        try:
            self._accept(event)
        except Exception as exc:  # the hub's ingest must never fail on time tracking
            logger.warning("platform_time_activity_classify_failed session=%s error=%s",
                           getattr(event, "session", None), exc)

    def _accept(self, event) -> None:
        message = event.message
        if not isinstance(message, dict) or message.get("kind") not in _KINDS:
            return
        source = message.get("source") or {}
        channel = source.get("channel")
        if channel not in _CHANNELS:
            return
        person = self.registry.person_for(message)
        if person is None:
            identity = self.registry.unmapped(message)
            if identity:
                self._remember_unmapped(identity)
            return
        at = _record_time(message.get("at"))
        key = activity_key(message)
        if at is None or key is None:
            logger.info("platform_time_activity_unusable session=%s id=%s",
                        event.session, message.get("id"))
            return
        ref = source.get("ref") if isinstance(source.get("ref"), str) else None
        with self._lock:
            if key in self._decided or key in self._pending:
                return
            self._pending[key] = _Pending(person, channel, ref, at, event.session)
        self._wake.set()

    def _remember_unmapped(self, identity: str) -> None:
        with self._lock:
            self._unmapped.pop(identity, None)
            self._unmapped[identity] = True
            while len(self._unmapped) > MAX_UNMAPPED:
                self._unmapped.popitem(last=False)

    def unmapped_identities(self) -> list:
        """``channel:id`` of each human seen whom no listed person owns, newest first.

        Read through the current registry, which a settings write swaps in.
        """
        registry = self.registry
        with self._lock:
            return [identity for identity in reversed(self._unmapped) if not registry.lists(identity)]

    # -- decisions -------------------------------------------------------------

    def process(self) -> list:
        """Decide every queued key; one :class:`Decision` each, in the order
        of the records' own times."""
        with self._lock:
            taken = list(self._pending.items())
            self._pending.clear()
            for key, _pending in taken:
                self._decided[key] = True
            while len(self._decided) > MAX_RECENT_KEYS:
                self._decided.popitem(last=False)
        taken.sort(key=lambda item: item[1].at)
        return [self._decide(key, pending) for key, pending in taken]

    def _decide(self, key: tuple, pending: _Pending) -> Decision:
        base = dict(key=key, person=pending.person, at=pending.at)
        try:
            workspace_id, session, sessions, reason = self._resolve(pending)
        except Exception as exc:
            logger.warning("platform_time_activity_unresolved person=%s session=%s error=%s",
                           pending.person, pending.session, exc)
            return Decision(**base, sessions=(pending.session,),
                            reason=f"the project could not be resolved: {exc}")
        base["sessions"] = sessions
        if workspace_id is None:
            logger.info("platform_time_activity_skipped person=%s session=%s reason=%s",
                        pending.person, pending.session, reason)
            return Decision(**base, reason=reason)
        try:
            credited = _credited(sessions, workspace_id)
            # An assistant's run is never the entry's (D21, D28): no reference either.
            refs = session_refs([session for session in credited if not _is_assistant(session)])
        except Exception as exc:  # a reference is a link, never a reason to skip time
            logger.info("platform_time_session_refs_skipped person=%s workspace=%s error=%s",
                        pending.person, workspace_id, exc)
            credited, refs = (), ()
        try:
            candidates = run_candidates(credited)
        except Exception as exc:  # a run is a label, never a reason to skip time
            logger.info("platform_time_run_candidates_skipped person=%s workspace=%s error=%s",
                        pending.person, workspace_id, exc)
            candidates = ()
        try:
            result = self.tracker.note_activity(pending.person, workspace_id, at=pending.at,
                                                runs=candidates, sessions=refs)
        except Exception as exc:
            logger.warning("platform_time_activity_failed person=%s workspace=%s error=%s",
                           pending.person, workspace_id, exc)
            return Decision(**base, workspace_id=workspace_id, refs=refs, runs=candidates,
                            reason=str(exc))
        logger.info("platform_time_activity person=%s workspace=%s outcome=%s",
                    pending.person, workspace_id, result.outcome)
        return Decision(**base, workspace_id=workspace_id, refs=refs, runs=candidates,
                        result=result)

    def _resolve(self, pending: _Pending) -> tuple:
        """``(workspace_id, session, sessions, None)``, or ``(None, None, sessions, reason)`` (D10)."""
        if not projects.enabled():
            return None, None, (pending.session,), "projects are not enabled"
        if pending.channel == CHANNEL_PLATFORM:
            home = projects.session_home(pending.session)
            if not home:
                return None, None, (pending.session,), "the session that recorded it belongs to no project"
            return home, pending.session, (pending.session,), None
        component = connectors.ensure_connectors()
        bindings = component.sessions()
        # Stopping, the drain asks no homeserver: a lookup could outlast the stop.
        ask = not self._stop.is_set()
        threads, named, unresolved = _threads(
            bindings, pending, lambda room: component.matrix_room_id(room, ask=ask))
        if unresolved:
            return None, None, unresolved, \
                "a Matrix room a session is bound to could not be resolved, so the thread could be its"
        if not named and len(threads) > 1:
            return None, None, (pending.session,), \
                f"the message names no thread, and the session that recorded it has {len(threads)}"
        bound = tuple(session for session, records in bindings.items()
                      if any(_thread_of(record) in threads for record in records))
        if not bound:
            return None, None, (), "no session is bound to that thread"
        homes: dict = {}
        for session in bound:
            home = projects.session_home(session)
            if home:
                homes.setdefault(home, session)
        owners: dict = {}
        for permalink in threads.values():
            for row in project_threads.ownership(permalink):
                if row.get("project_id"):
                    owners.setdefault(row["project_id"], homes.get(row["project_id"]))
        candidates = owners or homes
        if len(candidates) == 1:
            (workspace_id, session), = candidates.items()
            return workspace_id, session, bound, None
        if not candidates:
            return None, None, bound, "no session bound to that thread belongs to a project"
        whose = "thread owners" if owners else "session homes"
        return None, None, bound, f"ambiguous: {whose} {', '.join(sorted(candidates))}"

    # -- the idle sweep --------------------------------------------------------

    def sweep(self, now: Optional[datetime] = None) -> list:
        """Close idle automatic timers at *now*; the tracker's results, or ``[]``."""
        try:
            results = self.tracker.close_idle(now if now is not None else self._now())
        except Exception as exc:
            logger.warning("platform_time_sweep_failed error=%s", exc)
            return []
        for result in results:
            entry = result.entry
            logger.info("platform_time_idle_stop person=%s project=%s outcome=%s",
                        entry.person if entry else None, entry.project if entry else None,
                        result.outcome)
        return results

    def tick(self, now: Optional[datetime] = None) -> list:
        """One pass of the thread: decide what is queued, sweep when the interval is up."""
        now = now if now is not None else self._now()
        decisions = self.process()
        if self._next_sweep is None or now >= self._next_sweep:
            self.sweep(now)
            self._next_sweep = now + timedelta(seconds=_sweep_seconds())
        return decisions

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Sweep once (a timer left open across a restart closes at its last
        activity, D7), then run the thread."""
        if self._thread is not None:
            return
        self._stop.clear()
        now = self._now()
        self.sweep(now)
        self._next_sweep = now + timedelta(seconds=_sweep_seconds())
        self._thread = threading.Thread(target=self._run_forever, name="lmer-time-activity", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """End the thread, then decide whatever is still queued rather than drop it."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=STOP_TIMEOUT_SECONDS)
            self._thread = None
        try:
            self.process()
        except Exception as exc:
            logger.warning("platform_time_activity_drain_failed error=%s", exc)

    def _run_forever(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.tick()
            except Exception as exc:  # the component must outlive one bad pass
                logger.warning("platform_time_activity_tick_failed error=%s", exc)
            self._wake.wait(self._wait_seconds())

    def _wait_seconds(self) -> float:
        with self._lock:
            if self._pending:
                return 0.0
        if self._next_sweep is None:
            return float(_sweep_seconds())
        return max(0.0, (self._next_sweep - self._now()).total_seconds())


def _thread_of(record: dict) -> tuple:
    """A binding's thread, as its transport keys it."""
    return record.get("channel"), tuple(sorted((record.get("key") or {}).items()))


def _threads(bindings: dict, pending: _Pending, room_id: Callable[[str], Optional[str]]) -> tuple:
    """``({thread: permalink}, named, unresolved)`` for the threads the message
    could be from, whether its ref named the thread, and the sessions with a
    Matrix binding that could be the thread but whose room could not be resolved.

    A Slack ref names its thread (``channel:thread_ts:ts``) and a Matrix ref
    its thread's tag (``tag:event_id``), so either is found in every session's
    bindings, whichever session's copy came first. The connector tags the
    room id it resolved, so a binding kept under an ``#alias`` is compared by
    the id *room_id* resolves it to. A thread is its root event, so an alias
    is resolved only when no ``!id`` binding matched or its root is the
    matched one; one that cannot be resolved for now is ``unresolved``, and
    one that names no room matches nothing. A bare Matrix event id, from a
    connector started before the tag, names no thread: the candidates are the
    recording session's own Matrix threads.
    """
    def key(record):
        return record.get("key") or {}

    everyone = [record for records in bindings.values() for record in records]
    parts = pending.ref.split(":") if pending.ref else []
    if pending.channel == CHANNEL_SLACK and len(parts) >= 2:
        wanted = (parts[0], parts[1])
        records = [record for record in everyone if record.get("channel") == CHANNEL_SLACK
                   and (key(record).get("channel"), key(record).get("thread_ts")) == wanted]
    elif pending.channel == CHANNEL_MATRIX and pending.ref and split_thread_ref(pending.ref)[0]:
        tag = split_thread_ref(pending.ref)[0]
        matrix = [(session, record) for session, records in bindings.items() for record in records
                  if record.get("channel") == CHANNEL_MATRIX and isinstance(key(record).get("room"), str)
                  and isinstance(key(record).get("event"), str)]
        records = [record for _session, record in matrix if key(record)["room"].startswith("!")
                   and thread_tag(key(record)["room"], key(record)["event"]) == tag]
        roots = {key(record)["event"] for record in records}
        rooms, unknown = {}, []
        for session, record in matrix:
            room, event = key(record)["room"], key(record)["event"]
            if room.startswith("!") or (roots and event not in roots):
                continue
            if room not in rooms:
                try:
                    rooms[room] = room_id(room)
                except ThreadError:
                    rooms[room] = _UNKNOWN
            if rooms[room] is _UNKNOWN:
                unknown.append((session, event))
            elif rooms[room] is not None and thread_tag(rooms[room], event) == tag:
                records.append(record)
        roots = {key(record)["event"] for record in records}
        unresolved = dict.fromkeys(session for session, event in unknown if not roots or event in roots)
        return _permalinks(records), True, tuple(unresolved)
    else:
        records = [record for record in bindings.get(pending.session, ())
                   if record.get("channel") == pending.channel]
        return _permalinks(records), False, ()
    return _permalinks(records), True, ()


def _permalinks(records: list) -> dict:
    return {_thread_of(record): record["permalink"] for record in records
            if isinstance(record.get("permalink"), str)}


def _credited(sessions: tuple, workspace_id: str) -> tuple:
    """The *sessions* homed in *workspace_id*: a thread's owner may be a
    project none of the bound sessions belongs to, and then none is credited."""
    return tuple(session for session in sessions if projects.session_home(session) == workspace_id)


def session_refs(sessions) -> tuple:
    """The :class:`~lmer_timesheet.SessionRef` of each session's run, in order,
    once each. A session whose run the platform does not know, or cannot
    read, is left out: a reference is a link, never a reason to skip time."""
    refs = []
    for session in sessions:
        try:
            run = runs.run_of_session(session)
            parts = runs.key_parts_lenient(run) if run else None
            if parts is None:
                continue
            ref = _session_ref(runs.run_key(*parts), run.get("uid") or "")
        except Exception as exc:
            logger.info("platform_time_session_ref_skipped session=%s error=%s", session, exc)
            continue
        if ref not in refs:
            refs.append(ref)
    return tuple(refs)


def _session_ref(run: str, uid: str) -> SessionRef:
    """The reference, or the uid alone when the key is one an entry cannot
    hold (a host with a port): the uid still names the record."""
    try:
        return SessionRef(run=run, uid=uid)
    except ValueError:
        if not uid:
            raise
        return SessionRef(uid=uid)


def _is_assistant(session: str) -> bool:
    try:
        entry = registry.read_session(session)
    except registry.RegistryError:
        return False
    return isinstance(entry, dict) and entry.get("kind") == registry.ASSISTANT_KIND


def run_candidates(sessions) -> tuple:
    """The runs one activity may be for (D21): each non-assistant session's
    tracked run, once each. A session whose run is unknown names none."""
    found: list = []
    for session in sessions:
        if _is_assistant(session):
            continue
        facts = timetrack_runs.facts_for_session(session)
        if facts is None or any(timetrack_runs.same_run(facts.ref, seen.ref) for seen in found):
            continue
        found.append(facts)
    return tuple(found)


#: The errors :func:`_sweep_seconds` has warned about: it runs on every tick.
_SWEEP_WARNED: set = set()


def _sweep_seconds() -> int:
    try:
        return max(1, int(time_settings()["sweep_seconds"].value))
    except Exception as exc:
        if str(exc) not in _SWEEP_WARNED:
            _SWEEP_WARNED.add(str(exc))
            logger.warning("platform_time_sweep_settings_unreadable error=%s (said once per error)", exc)
        return TIME_INT_SETTINGS["time_sweep_seconds"].default


_ACTIVITY: Optional[ActivityConsumer] = None
_ACTIVITY_LOCK = threading.Lock()


def register_activity(activity: Optional[ActivityConsumer]) -> None:
    global _ACTIVITY
    with _ACTIVITY_LOCK:
        _ACTIVITY = activity


def get_activity() -> Optional[ActivityConsumer]:
    with _ACTIVITY_LOCK:
        return _ACTIVITY
