"""Activity on the bus drives automatic time (issue #604, task T7, goal G3).

Records go in through the real hub's ``_ingest``, as journaled bus records,
into an :class:`ActivityConsumer` with an injected clock; the tests then run
the thread's pass with ``tick`` and sweep with an explicit time, so nothing
waits on the wall clock. The tracker, the projects database and the connector
bindings are real, in an isolated platform dir.

What must hold: a mapped human message starts or switches its project, idle
time stops at the last activity under each project's grace, and one Slack
message is decided at its first copy from every session bound to its thread,
whenever the other copies arrive: the thread's owner wins, and owners or bound
sessions from two projects credit nobody (D10). Everything that is not a
listed human's message starts nothing. The runs of the credited project's
sessions reach the timer and its entry (issue #648). The daemon registers the consumer
before the hub starts and stops every part with itself.
"""
import logging
import os
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from lmer_cli import comms as bus
from lmer_cli.threads import parse_thread
from lmer_platform import (
    comms, connectors, daemon, meta, project_context, project_threads, projects, registry, runs, store,
    timesheet_store,
    timesheet_writer, timetrack, timetrack_activity, timetrack_live,
)
from lmer_platform import config as cfg
from lmer_platform.people import CREDENTIAL_OPERATOR, META_CREDENTIAL, Registry
from lmer_platform.timetrack import REFRESHED, STARTED, STOPPED, SWITCHED, Tracker
from lmer_platform.timetrack_activity import ActivityConsumer, activity_key
from lmer_timesheet import SessionRef, parse_entry
from matrix_chat.client import MatrixError
from matrix_chat.permalink import thread_ref
from tests.conftest import strip_lmer_env
from tests.test_platform_projects import fleet  # noqa: F401
from tests.test_platform_timetrack import Clock

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
PERSON = "alex"
SLACK_ID = "U0ALEX"
STRANGER_ID = "U0STRANGER"
ALPHA_THREAD = "https://example.slack.com/archives/C0123ABCD/p1700000000000100"
BETA_THREAD = "https://example.slack.com/archives/C0456EFGH/p1700000500000100"
MATRIX_ID = "@alex:example.org"
MATRIX_T = "https://matrix.to/#/!shared:example.org/$t-root"
MATRIX_U = "https://matrix.to/#/!alpha:example.org/$u-root"
MATRIX_T_ALIAS = "https://matrix.to/#/#shared:example.org/$t-root"
MATRIX_ENV = {"LMER_MATRIX_HOMESERVER": "https://matrix.example.org", "LMER_MATRIX_USER_ID": "@lmer:example.org",
              "LMER_MATRIX_ACCESS_TOKEN": "secret-access-token"}
PEOPLE = [{"key": PERSON, "name": "Alex", "slack": [SLACK_ID], "matrix": ["@alex:example.org"]}]


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def platform(fleet):
    projects.migrate()
    return replace(fleet, people=list(PEOPLE), default_person=PERSON)


@pytest.fixture
def tracker(platform, clock):
    tracker = Tracker(platform, now=clock)
    timetrack.register_tracker(tracker)
    yield tracker
    timetrack.register_tracker(None)
    timesheet_writer.register_writer(None)


@pytest.fixture
def activity(platform, tracker, clock):
    consumer = ActivityConsumer(platform, tracker, Registry.from_config(platform), now=clock)
    yield consumer
    consumer.stop()


@pytest.fixture
def hub(activity):
    hub = comms.CommsHub()
    hub.add_consumer(activity)
    return hub


@pytest.fixture(autouse=True)
def _unregister():
    yield
    connectors.register_connectors(None)
    timetrack_activity.register_activity(None)


@pytest.fixture
def alpha(platform):
    return projects.create("Alpha")["id"]


@pytest.fixture
def beta(platform):
    return projects.create("Beta")["id"]


def _session(session, home, *, run=None):
    projects.bind_session(session, home)
    if run is not None:
        registry.register(session, kind="worker", pid=os.getpid(), run=run)
    return session


def _bind(bindings, *, matrix=None, environ=MATRIX_ENV, clock=time.monotonic):
    """Record connector bindings ``{session: [permalink]}`` as the daemon persists them.

    *matrix* is the homeserver client the component resolves a room alias
    through; without one only ``!room`` ids are bound."""
    records = {}
    for session, links in bindings.items():
        records[session] = []
        for link in links:
            ref = parse_thread(link)
            records[session].append({"id": ref.id, "channel": ref.channel, "permalink": ref.permalink,
                                     "label": ref.label, "key": dict(ref.key), "by": "test",
                                     "origin": "test"})
    store.write_json(connectors.threads_path(), {"schema": connectors.SCHEMA, "sessions": records})
    if matrix is None:
        connectors.register_connectors(connectors.ThreadConnectors())
    else:
        connectors.register_connectors(connectors.ThreadConnectors(
            environ=environ, matrix_client=lambda account: matrix, clock=clock))


_SEQ = {}


def _record(session, channel, *, at, kind="message", author=None, author_id=None, ref=None,
            meta=None):
    seq = _SEQ[session] = _SEQ.get(session, 0) + 1
    return bus.Message(
        id=bus.message_id(session, seq), seq=seq, session=session,
        at=at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        source=bus.Source(channel, author=author, ref=ref, author_id=author_id),
        kind=kind, text="hello", meta=meta or {},
    ).to_dict()


def _slack(session, thread, *, at, ts, author_id=SLACK_ID, kind="message"):
    key = parse_thread(thread).key
    return _record(session, bus.CHANNEL_SLACK, at=at, kind=kind, author="Alex",
                   author_id=author_id, ref=f"{key['channel']}:{key['thread_ts']}:{ts}")


def _matrix(session, thread, *, at, event, tagged=True):
    """A human's Matrix message in *thread*, its ref as the session's
    connector posts it; ``tagged=False`` is a connector older than the tag."""
    key = parse_thread(thread).key
    return _record(session, bus.CHANNEL_MATRIX, at=at, author=MATRIX_ID, author_id=MATRIX_ID,
                   ref=thread_ref(key["room"], key["event"], event) if tagged else event)


def _ingest(hub, session, message):
    assert hub._ingest(session, message) is True


def _decide(activity):
    """One pass of the consumer's thread, at the clock's current time."""
    return activity.tick()


def _timer():
    with timesheet_store.database() as db:
        return timetrack._read_timer(db, PERSON)


def _entries():
    return [parse_entry(row.entry_yaml)[0] for row in timesheet_store.pending()]


def _say(hub, activity, clock, session, thread, *, at, ts):
    clock.now = at
    _ingest(hub, session, _slack(session, thread, at=at, ts=ts))
    decisions = _decide(activity)
    assert len(decisions) == 1, decisions
    return decisions[0]


# --- start, switch, idle ---------------------------------------------------

def test_a_mapped_slack_message_in_a_bound_thread_starts_its_project_at_once(
        hub, activity, clock, alpha):
    session = _session("s-alpha1", alpha)
    _bind({session: [ALPHA_THREAD]})
    _ingest(hub, session, _slack(session, ALPHA_THREAD, at=T0, ts="1700000100.000200"))

    [decision] = _decide(activity)
    assert decision.sessions == (session,)
    assert (decision.workspace_id, decision.result.outcome) == (alpha, STARTED)
    timer = _timer()
    assert (timer.workspace_id, timer.start, timer.mode) == (alpha, T0, timetrack.AUTO)
    assert timesheet_store.pending() == []


def test_a_message_in_another_project_switches(hub, activity, clock, alpha, beta):
    one, two = _session("s-alpha1", alpha), _session("s-beta1", beta)
    _bind({one: [ALPHA_THREAD], two: [BETA_THREAD]})
    _say(hub, activity, clock, one, ALPHA_THREAD, at=T0, ts="1700000100.000200")

    later = T0 + timedelta(minutes=10)
    decision = _say(hub, activity, clock, two, BETA_THREAD, at=later, ts="1700000600.000200")

    assert (decision.workspace_id, decision.result.outcome) == (beta, SWITCHED)
    assert _timer().workspace_id == beta
    [entry] = _entries()
    assert (entry.project, entry.start, entry.end) == (projects.project_key(alpha), T0, later)
    assert (entry.trigger.start, entry.trigger.stop) == ("activity", "switch")


def test_idleness_stops_at_the_last_activity_under_each_projects_grace(
        hub, activity, clock, platform, alpha, beta):
    projects.update(alpha, {"settings": {"time_idle_grace_minutes": 5}})
    one, two = _session("s-alpha1", alpha), _session("s-beta1", beta)
    _bind({one: [ALPHA_THREAD], two: [BETA_THREAD]})

    _say(hub, activity, clock, one, ALPHA_THREAD, at=T0, ts="1700000100.000200")
    last = T0 + timedelta(minutes=2)
    assert _say(hub, activity, clock, one, ALPHA_THREAD, at=last,
                ts="1700000200.000200").result.outcome == REFRESHED
    assert activity.sweep(T0 + timedelta(minutes=6)) == []
    [stopped] = activity.sweep(T0 + timedelta(minutes=7))
    assert stopped.outcome == STOPPED
    assert (stopped.entry.end, stopped.entry.trigger.stop) == (last, "idle")
    assert _timer() is None

    start = T0 + timedelta(minutes=30)
    _say(hub, activity, clock, two, BETA_THREAD, at=start, ts="1700000700.000200")
    last = start + timedelta(minutes=2)
    _say(hub, activity, clock, two, BETA_THREAD, at=last, ts="1700000800.000200")
    assert activity.sweep(last + timedelta(minutes=14)) == [], "the platform's 15 minutes apply to Beta"
    [stopped] = activity.sweep(last + timedelta(minutes=15))
    assert (stopped.entry.project, stopped.entry.end) == (projects.project_key(beta), last)
    assert [entry.end for entry in _entries()] == [T0 + timedelta(minutes=2), last]


def test_a_single_message_is_a_sliver_kept_locally(hub, activity, clock, tracker, alpha):
    session = _session("s-alpha1", alpha)
    _bind({session: [ALPHA_THREAD]})
    _say(hub, activity, clock, session, ALPHA_THREAD, at=T0, ts="1700000100.000200")

    [result] = activity.sweep(T0 + timedelta(minutes=15))
    assert result.outcome == timetrack.KEPT
    assert timesheet_store.pending() == []
    [kept] = tracker.kept_entries(PERSON)
    assert kept["workspace_id"] == alpha


# --- one message, many sessions (D10) ---------------------------------------

def test_the_owner_wins_when_its_sessions_copy_arrives_second(hub, activity, clock, alpha, beta):
    bound = _session("s-alpha1", alpha)
    unbound = _session("s-beta1", beta)
    _bind({bound: [ALPHA_THREAD]})
    ts = "1700000100.000200"
    _ingest(hub, unbound, _slack(unbound, ALPHA_THREAD, at=T0, ts=ts))
    clock.advance(seconds=6)
    [decision] = _decide(activity)
    assert decision.sessions == (bound,), "the bound sessions decide, not the copies that arrived"
    assert (decision.workspace_id, decision.result.outcome) == (alpha, STARTED)

    for late in (bound, _session("s-alpha2", alpha)):
        _ingest(hub, late, _slack(late, ALPHA_THREAD, at=T0, ts=ts))
        assert _decide(activity) == [], "a copy after the decision counts again"
    assert _timer().workspace_id == alpha


def test_a_message_reaching_two_projects_sessions_without_an_owner_starts_nothing(
        hub, activity, clock, alpha, beta):
    # Copies reach the sessions' subscribers independently: B's comes 6 s
    # after A's, after the thread has already decided.
    one, two = _session("s-alpha1", alpha), _session("s-beta1", beta)
    _bind({})
    ts = "1700000100.000200"
    _ingest(hub, one, _slack(one, ALPHA_THREAD, at=T0, ts=ts))
    clock.advance(seconds=5)
    [decision] = _decide(activity)
    assert decision.workspace_id is None and decision.result is None
    assert decision.reason == "no session is bound to that thread"

    clock.advance(seconds=1)
    _ingest(hub, two, _slack(two, ALPHA_THREAD, at=T0, ts=ts))
    assert _decide(activity) == [], "the late copy decided again"
    assert _timer() is None


def test_a_message_in_a_thread_bound_to_two_projects_starts_nothing_at_the_first_copy(
        hub, activity, clock, alpha, beta):
    one, two = _session("s-alpha1", alpha), _session("s-beta1", beta)
    _bind({one: [ALPHA_THREAD], two: [ALPHA_THREAD]})
    _ingest(hub, one, _slack(one, ALPHA_THREAD, at=T0, ts="1700000100.000200"))
    [decision] = _decide(activity)
    assert decision.sessions == (one, two)
    assert decision.result is None and decision.reason.startswith("ambiguous: thread owners")
    assert _timer() is None


def test_an_ambiguous_message_does_not_switch_a_running_timer(hub, activity, clock, alpha, beta):
    one, two = _session("s-alpha1", alpha), _session("s-beta1", beta)
    _bind({one: [ALPHA_THREAD, BETA_THREAD], two: [BETA_THREAD]})
    _say(hub, activity, clock, one, ALPHA_THREAD, at=T0, ts="1700000100.000200")

    later = T0 + timedelta(minutes=3)
    clock.now = later
    _ingest(hub, two, _slack(two, BETA_THREAD, at=later, ts="1700000600.000200"))
    [decision] = _decide(activity)

    assert decision.result is None
    timer = _timer()
    assert (timer.workspace_id, timer.last_activity) == (alpha, T0)


def test_sessions_bound_to_one_thread_from_two_projects_are_ambiguous(
        hub, activity, clock, alpha, beta):
    one, two = _session("s-alpha1", alpha), _session("s-beta1", beta)
    _bind({one: [ALPHA_THREAD], two: [ALPHA_THREAD]})
    ts = "1700000100.000200"
    for session in (one, two):
        _ingest(hub, session, _slack(session, ALPHA_THREAD, at=T0, ts=ts))
    [decision] = _decide(activity)
    assert decision.result is None and "thread owners" in decision.reason


def test_a_binding_to_another_thread_does_not_lend_its_owner(hub, activity, clock, alpha, beta):
    session = _session("s-beta1", beta)
    _bind({session: [ALPHA_THREAD]})
    project_threads.choose_unbound(ALPHA_THREAD, alpha)
    decision = _say(hub, activity, clock, session, BETA_THREAD, at=T0, ts="1700000600.000200")
    assert decision.workspace_id is None, "the unbound thread borrowed an owner"
    assert decision.reason == "no session is bound to that thread"
    assert _timer() is None


def test_bindings_that_cannot_be_read_start_nothing(hub, activity, clock, alpha, monkeypatch, caplog):
    session = _session("s-alpha1", alpha)
    _bind({session: [ALPHA_THREAD]})

    class Broken:
        def sessions(self):
            raise OSError("threads.json is unreadable")

    monkeypatch.setattr(connectors, "ensure_connectors", lambda: Broken())
    with caplog.at_level(logging.WARNING, logger="lmer_platform.timetrack_activity"):
        _ingest(hub, session, _slack(session, ALPHA_THREAD, at=T0, ts="1700000100.000200"))
        [decision] = _decide(activity)
    assert decision.result is None
    assert "threads.json is unreadable" in decision.reason
    assert "platform_time_activity_unresolved" in caplog.text
    assert _timer() is None


@pytest.mark.parametrize("first", ["alpha", "beta"])
def test_a_matrix_message_is_decided_from_its_thread_whichever_copy_comes_first(
        hub, activity, clock, alpha, beta, first):
    # A is also bound to U, which Alpha owns; a message in T must not borrow
    # U's owner, whichever session's copy is first.
    one, two = _session("s-alpha1", alpha), _session("s-beta1", beta)
    _bind({one: [MATRIX_T, MATRIX_U], two: [MATRIX_T]})
    project_threads.choose_unbound(MATRIX_U, alpha)
    order = (one, two) if first == "alpha" else (two, one)
    _ingest(hub, order[0], _matrix(order[0], MATRIX_T, at=T0, event="$m1"))
    [decision] = _decide(activity)
    assert decision.sessions == (one, two)
    assert decision.result is None
    assert decision.reason == f"ambiguous: thread owners {', '.join(sorted((alpha, beta)))}"
    _ingest(hub, order[1], _matrix(order[1], MATRIX_T, at=T0, event="$m1"))
    assert _decide(activity) == []
    assert _timer() is None


def test_a_matrix_threads_owner_wins_when_another_sessions_copy_arrives_first(
        hub, activity, clock, alpha, beta):
    bound, other = _session("s-alpha1", alpha), _session("s-beta1", beta)
    _bind({bound: [MATRIX_T], other: [MATRIX_U]})
    project_threads.choose_unbound(MATRIX_T, alpha)
    clock.now = T0
    _ingest(hub, other, _matrix(other, MATRIX_T, at=T0, event="$m1"))
    [decision] = _decide(activity)
    assert decision.sessions == (bound,), "the recording session's own threads decided"
    assert (decision.workspace_id, decision.result.outcome) == (alpha, STARTED)


def test_an_untagged_matrix_ref_is_ambiguous_when_its_session_has_two_threads(
        hub, activity, clock, alpha, beta):
    one, two = _session("s-alpha1", alpha), _session("s-beta1", beta)
    _bind({one: [MATRIX_T, MATRIX_U], two: [MATRIX_T]})
    project_threads.choose_unbound(MATRIX_U, alpha)
    _ingest(hub, one, _matrix(one, MATRIX_T, at=T0, event="$m1", tagged=False))
    [decision] = _decide(activity)
    assert decision.result is None
    assert decision.reason == "the message names no thread, and the session that recorded it has 2"
    assert _timer() is None


def test_an_untagged_matrix_ref_from_a_session_with_one_thread_counts(hub, activity, clock, alpha):
    session = _session("s-alpha1", alpha)
    _bind({session: [MATRIX_T]})
    clock.now = T0
    _ingest(hub, session, _matrix(session, MATRIX_T, at=T0, event="$m1", tagged=False))
    [decision] = _decide(activity)
    assert (decision.workspace_id, decision.result.outcome) == (alpha, STARTED)


class Directory:
    """A homeserver's room directory: an alias resolves while it is listed,
    and otherwise fails with *failure* (by default, no answer)."""

    def __init__(self, rooms, failure=None):
        self.rooms = dict(rooms)
        self.failure = failure or MatrixError("cannot reach the homeserver", transient=True)
        self.asked = []

    def resolve_room(self, room):
        self.asked.append(room)
        if room not in self.rooms:
            raise self.failure
        return self.rooms[room]


NOT_FOUND = MatrixError("GET /directory/room → 404 M_NOT_FOUND", status=404, errcode="M_NOT_FOUND")
MATRIX_V = "https://matrix.to/#/!healthy:example.org/$v-root"


def _from_the_connector(session, *, at, event, thread=("!shared:example.org", "$t-root")):
    """A message as a connector posts it: tagged with the room id it resolved,
    by default that of a connector launched on MATRIX_T_ALIAS."""
    return _record(session, bus.CHANNEL_MATRIX, at=at, author=MATRIX_ID, author_id=MATRIX_ID,
                   ref=thread_ref(*thread, event))


def test_a_matrix_binding_kept_under_its_alias_matches_the_room_id_the_connector_tags(
        hub, activity, clock, alpha, beta):
    bound, other = _session("s-alpha1", alpha), _session("s-beta1", beta)
    directory = Directory({"#shared:example.org": "!shared:example.org"})
    _bind({bound: [MATRIX_T_ALIAS], other: [MATRIX_U]}, matrix=directory)
    assert connectors.get_connectors().records(bound)[0]["key"]["room"] == "#shared:example.org"
    clock.now = T0
    _ingest(hub, other, _from_the_connector(other, at=T0, event="$m1"))
    [decision] = _decide(activity)
    assert decision.sessions == (bound,)
    assert (decision.workspace_id, decision.result.outcome) == (alpha, STARTED)

    clock.now = T0 + timedelta(minutes=1)
    _ingest(hub, bound, _from_the_connector(bound, at=clock.now, event="$m2"))
    [decision] = _decide(activity)
    assert decision.result.outcome == REFRESHED
    assert directory.asked == ["#shared:example.org"], "a resolution is asked once"


def test_a_matrix_binding_whose_alias_cannot_be_resolved_for_now_makes_its_threads_message_ambiguous(
        hub, activity, clock, alpha, beta):
    bound, other = _session("s-alpha1", alpha), _session("s-beta1", beta)
    ticks = [0.0]
    directory = Directory({})
    _bind({bound: [MATRIX_T_ALIAS], other: [MATRIX_U]}, matrix=directory, clock=lambda: ticks[0])
    clock.now = T0
    _ingest(hub, other, _from_the_connector(other, at=T0, event="$m1"))
    [decision] = _decide(activity)
    assert decision.result is None and decision.sessions == (bound,)
    assert decision.reason == \
        "a Matrix room a session is bound to could not be resolved, so the thread could be its"
    assert _timer() is None

    directory.rooms["#shared:example.org"] = "!shared:example.org"
    ticks[0] = connectors.BACKOFF_MAX
    clock.now = T0 + timedelta(minutes=1)
    _ingest(hub, other, _from_the_connector(other, at=clock.now, event="$m2"))
    [decision] = _decide(activity)
    assert (decision.workspace_id, decision.result.outcome) == (alpha, STARTED), \
        "a failed resolution is asked again once its window passes"
    assert directory.asked == ["#shared:example.org"] * 2


def test_an_unresolved_alias_on_the_matched_threads_root_is_ambiguous(hub, activity, clock, alpha, beta):
    aliased, direct = _session("s-alpha1", alpha), _session("s-beta1", beta)
    directory = Directory({})
    _bind({aliased: [MATRIX_T_ALIAS], direct: [MATRIX_T]}, matrix=directory)
    _ingest(hub, direct, _from_the_connector(direct, at=T0, event="$m1"))
    [decision] = _decide(activity)
    assert decision.result is None and decision.sessions == (aliased,)
    assert directory.asked == ["#shared:example.org"]


@pytest.mark.parametrize("failure", ["no answer", "no such room", "no account"])
def test_a_dead_alias_in_one_session_never_blocks_another_sessions_thread(
        hub, activity, clock, alpha, beta, failure):
    dead, healthy = _session("s-alpha1", alpha), _session("s-beta1", beta)
    directory = Directory({}, failure=NOT_FOUND if failure == "no such room" else None)
    _bind({dead: [MATRIX_T_ALIAS], healthy: [MATRIX_V]}, matrix=directory,
          environ={} if failure == "no account" else MATRIX_ENV)
    clock.now = T0
    _ingest(hub, healthy, _from_the_connector(healthy, at=T0, event="$m1",
                                              thread=("!healthy:example.org", "$v-root")))
    [decision] = _decide(activity)
    assert decision.sessions == (healthy,)
    assert (decision.workspace_id, decision.result.outcome) == (beta, STARTED)
    assert directory.asked == [], "an alias on another root is never looked up"


@pytest.mark.parametrize("failure", ["no such room", "no account"])
def test_an_alias_that_definitively_names_no_room_matches_nothing(
        hub, activity, clock, alpha, beta, failure):
    dead, other = _session("s-alpha1", alpha), _session("s-beta1", beta)
    directory = Directory({}, failure=NOT_FOUND)
    _bind({dead: [MATRIX_T_ALIAS], other: [MATRIX_U]}, matrix=directory,
          environ={} if failure == "no account" else MATRIX_ENV)
    _ingest(hub, other, _from_the_connector(other, at=T0, event="$m1"))
    [decision] = _decide(activity)
    assert decision.result is None
    assert decision.reason == "no session is bound to that thread"
    assert directory.asked == ([] if failure == "no account" else ["#shared:example.org"])


def test_a_burst_asks_a_failing_directory_once_per_window(hub, activity, clock, alpha, beta):
    sessions = [_session("s-alpha1", alpha), _session("s-beta1", beta), _session("s-beta2", beta)]
    other = _session("s-alpha2", alpha)
    ticks = [0.0]
    directory = Directory({}, failure=MatrixError("GET /directory/room → 429", status=429,
                                                  transient=True, retry_after=5.0))
    _bind({**{session: [MATRIX_T_ALIAS] for session in sessions}, other: [MATRIX_U]},
          matrix=directory, clock=lambda: ticks[0])
    for n in range(5):
        _ingest(hub, other, _from_the_connector(other, at=T0 + timedelta(seconds=n), event=f"$m{n}"))
    decisions = _decide(activity)
    assert [d.sessions for d in decisions] == [tuple(sessions)] * 5
    assert directory.asked == ["#shared:example.org"], "one request for three bindings and five messages"

    ticks[0] = 4.9
    _ingest(hub, other, _from_the_connector(other, at=T0 + timedelta(seconds=6), event="$m6"))
    _decide(activity)
    assert len(directory.asked) == 1, "within the server's retry_after"
    ticks[0] = 5.0
    _ingest(hub, other, _from_the_connector(other, at=T0 + timedelta(seconds=7), event="$m7"))
    _decide(activity)
    assert len(directory.asked) == 2


def test_the_stopping_drain_asks_no_homeserver(hub, activity, clock, alpha, beta):
    bound, other = _session("s-alpha1", alpha), _session("s-beta1", beta)
    directory = Directory({"#shared:example.org": "!shared:example.org"})
    _bind({bound: [MATRIX_T_ALIAS], other: [MATRIX_U]}, matrix=directory)
    _ingest(hub, other, _from_the_connector(other, at=T0, event="$m1"))
    activity.stop()
    assert directory.asked == []
    assert _timer() is None


def test_a_tagged_and_an_untagged_copy_of_one_matrix_event_are_one_activity(
        hub, activity, clock, alpha):
    one, two = _session("s-alpha1", alpha), _session("s-alpha2", alpha)
    _bind({one: [MATRIX_T], two: [MATRIX_T]})
    tagged, bare = _matrix(one, MATRIX_T, at=T0, event="$m1"), _matrix(two, MATRIX_T, at=T0, event="$m1", tagged=False)
    assert activity_key(tagged) == activity_key(bare) == ("ref", bus.CHANNEL_MATRIX, "$m1")
    clock.now = T0
    _ingest(hub, one, tagged)
    _ingest(hub, two, bare)
    assert len(_decide(activity)) == 1


def test_activity_key_is_the_channel_ref_else_the_record_id():
    ref = _record("s-x", bus.CHANNEL_SLACK, at=T0, ref="C1:1.0:2.0")
    assert activity_key(ref) == ("ref", bus.CHANNEL_SLACK, "C1:1.0:2.0")
    web = _record("s-x", bus.CHANNEL_PLATFORM, at=T0, author="operator")
    assert activity_key(web) == ("id", web["id"])


# --- what is not activity ----------------------------------------------------

def test_web_chat_counts_for_the_default_person_in_the_sessions_home(hub, activity, clock, alpha):
    session = _session("s-alpha1", alpha)
    _ingest(hub, session, _record(session, bus.CHANNEL_PLATFORM, at=T0, author="operator",
                                  meta={META_CREDENTIAL: CREDENTIAL_OPERATOR}))
    [decision] = _decide(activity)
    assert (decision.person, decision.workspace_id, decision.result.outcome) == (PERSON, alpha, STARTED)


def test_agents_assistants_announcements_and_strangers_start_nothing(hub, activity, clock, alpha):
    session = _session("s-alpha1", alpha)
    _bind({session: [ALPHA_THREAD]})
    records = [
        _record(session, bus.CHANNEL_AGENT, at=T0, author="agent"),
        _record(session, bus.CHANNEL_SUPERVISOR, at=T0, kind=bus.KIND_NOTE),
        _record(session, bus.CHANNEL_UBER, at=T0, author="Assistant"),
        _record(session, bus.CHANNEL_PEER_UL, at=T0, author="peer"),
        _record(session, bus.CHANNEL_PLATFORM, at=T0, author="operator",
                meta={META_CREDENTIAL: CREDENTIAL_OPERATOR, "notification_class": "connector_health"}),
        _record(session, bus.CHANNEL_PLATFORM, at=T0, author="operator", ref="thread-reference",
                meta={META_CREDENTIAL: CREDENTIAL_OPERATOR}),
        # The assistant's own reply, read back from the thread: no stable id.
        _slack(session, ALPHA_THREAD, at=T0, ts="1700000100.000300", author_id=None),
        _slack(session, ALPHA_THREAD, at=T0, ts="1700000100.000400", kind=bus.KIND_QUESTION),
        _slack(session, ALPHA_THREAD, at=T0, ts="1700000100.000500", author_id=STRANGER_ID),
    ]
    for message in records:
        _ingest(hub, session, message)

    assert _decide(activity) == []
    assert _timer() is None
    assert activity.unmapped_identities() == [f"slack:{STRANGER_ID}"]


def test_unmapped_identities_are_bounded_and_newest_first(activity, monkeypatch):
    monkeypatch.setattr(timetrack_activity, "MAX_UNMAPPED", 2)
    for name in ("U1", "U2", "U3", "U2"):
        message = _record("s-x", bus.CHANNEL_SLACK, at=T0, author_id=name, ref="C1:1.0:2.0")
        activity(comms.FleetEvent(cursor=1, session="s-x", message=message))
    assert activity.unmapped_identities() == ["slack:U2", "slack:U3"]


def test_a_record_the_consumer_cannot_read_is_dropped_without_raising(activity):
    broken = _slack("s-x", ALPHA_THREAD, at=T0, ts="1.0")
    broken["at"] = "yesterday"
    activity(comms.FleetEvent(cursor=1, session="s-x", message=broken))
    activity(SimpleNamespace(session="s-x", message=None))
    assert _decide(activity) == []


def test_a_run_name_is_never_the_title(hub, activity, clock, alpha):
    """D22: a run names an entry through its identity, never as a fallback title."""
    session = _session("s-alpha1", alpha,
                       run={"host": "forge.test", "project": "team/app", "slug": "fix-login"})
    _bind({session: [ALPHA_THREAD]})
    _say(hub, activity, clock, session, ALPHA_THREAD, at=T0, ts="1700000100.000200")

    assert _timer().title != "fix-login"


UID_A = "981ec036134f4a8580927ddca71658a9"


def test_the_credited_sessions_runs_reach_the_entry(hub, activity, clock, alpha, beta):
    # Schema 2 (a uid), schema 1 (a slug), a session with no run, and one bound
    # to the thread but homed in no project: only the first two are Alpha's runs.
    v2 = _session("s-alpha1", alpha, run={"host": "forge.test", "project": "team/app",
                                          "slug": "fix-login", "uid": UID_A})
    v1 = _session("s-alpha2", alpha, run={"host": "forge.test", "project": "team/app",
                                          "slug": "develop-issue-7"})
    runless = _session("s-alpha3", alpha)
    stray = "s-stray"
    registry.register(stray, kind="worker", pid=os.getpid(),
                      run={"host": "forge.test", "project": "team/other", "slug": "stray"})
    _bind({v2: [ALPHA_THREAD], v1: [ALPHA_THREAD], runless: [ALPHA_THREAD], stray: [ALPHA_THREAD]})

    decision = _say(hub, activity, clock, v2, ALPHA_THREAD, at=T0, ts="1700000100.000200")

    assert decision.sessions == (v2, v1, runless, stray)
    assert decision.refs == (SessionRef(run=f"forge.test/team/app/{UID_A}", uid=UID_A),
                             SessionRef(run="forge.test/team/app/develop-issue-7"))
    assert _timer().sessions == decision.refs
    two = _session("s-beta1", beta)
    _bind({v2: [ALPHA_THREAD], two: [BETA_THREAD]})
    switched = _say(hub, activity, clock, two, BETA_THREAD, at=T0 + timedelta(minutes=4),
                    ts="1700000600.000200")
    assert switched.refs == () and _timer().sessions == ()
    [entry] = _entries()
    assert entry.sessions == list(decision.refs)


def _stray(session, slug):
    """A session with a run, homed in no project."""
    registry.register(session, kind="worker", pid=os.getpid(),
                      run={"host": "forge.test", "project": "team/other", "slug": slug})
    return session


def test_a_thread_owner_is_credited_only_its_own_sessions_runs(hub, activity, clock, alpha):
    # A session homed in another project would make the thread ambiguous (its
    # project owns it too), so the session the owner must not take a run from
    # is one homed nowhere.
    mine = _session("s-alpha1", alpha, run={"host": "forge.test", "project": "team/app", "slug": "alpha-work"})
    stray = _stray("s-stray", "stray-work")
    _bind({mine: [ALPHA_THREAD], stray: [ALPHA_THREAD]})
    project_threads.choose_unbound(ALPHA_THREAD, alpha)
    decision = _say(hub, activity, clock, stray, ALPHA_THREAD, at=T0, ts="1700000100.000200")
    assert (decision.workspace_id, decision.sessions) == (alpha, (mine, stray))
    assert decision.refs == (SessionRef(run="forge.test/team/app/alpha-work"),)


def test_a_thread_owner_with_no_session_of_its_own_is_credited_no_run(hub, activity, clock, alpha):
    stray = _stray("s-stray", "stray-work")
    _bind({stray: [ALPHA_THREAD]})
    project_threads.choose_unbound(ALPHA_THREAD, alpha)
    decision = _say(hub, activity, clock, stray, ALPHA_THREAD, at=T0, ts="1700000100.000200")
    assert decision.workspace_id == alpha and decision.result.outcome == STARTED
    assert decision.refs == ()


def test_a_run_key_an_entry_cannot_hold_keeps_its_uid():
    ref = timetrack_activity._session_ref(f"forge.test:8443/team/app/{UID_A}", UID_A)
    assert ref == SessionRef(uid=UID_A)


def test_web_chat_credits_its_own_sessions_run(hub, activity, clock, alpha):
    session = _session("s-alpha1", alpha, run={"host": "forge.test", "project": "team/app",
                                               "slug": "fix-login", "uid": UID_A})
    _ingest(hub, session, _record(session, bus.CHANNEL_PLATFORM, at=T0, author="operator",
                                  meta={META_CREDENTIAL: CREDENTIAL_OPERATOR}))
    [decision] = _decide(activity)
    assert decision.refs == (SessionRef(run=f"forge.test/team/app/{UID_A}", uid=UID_A),)


def test_a_run_that_cannot_be_referenced_costs_its_reference_not_the_time(
        hub, activity, clock, alpha, caplog):
    session = _session("s-alpha1", alpha, run={"host": "forge.test", "project": "team/app",
                                               "slug": "fix login"})
    _bind({session: [ALPHA_THREAD]})
    with caplog.at_level(logging.INFO, logger="lmer_platform.timetrack_activity"):
        decision = _say(hub, activity, clock, session, ALPHA_THREAD, at=T0, ts="1700000100.000200")
    assert decision.result.outcome == STARTED
    assert decision.refs == ()
    assert "platform_time_session_ref_skipped session=s-alpha1" in caplog.text


# --- the component's own thread ---------------------------------------------

def _until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        threading.Event().wait(0.02)
    return predicate()


def test_start_sweeps_once_and_the_thread_decides_at_once(platform, tracker, clock, alpha):
    tracker.note_activity(PERSON, alpha, at=T0 - timedelta(hours=2))
    tracker.note_activity(PERSON, alpha, at=T0 - timedelta(hours=1, minutes=58))
    consumer = ActivityConsumer(platform, tracker, Registry.from_config(platform), now=clock)
    hub = comms.CommsHub()
    hub.add_consumer(consumer)
    session = _session("s-alpha1", alpha)
    _bind({session: [ALPHA_THREAD]})
    try:
        consumer.start()
        [entry] = _entries()
        assert entry.end == T0 - timedelta(hours=1, minutes=58), \
            "a timer left open closes at its last activity"

        _ingest(hub, session, _slack(session, ALPHA_THREAD, at=T0, ts="1700000100.000200"))
        assert _until(lambda: _timer() is not None), "the thread never decided the record"
        assert _timer().start == T0
    finally:
        consumer.stop()
    assert consumer._thread is None


def test_stop_decides_what_is_still_queued(hub, activity, clock, alpha):
    session = _session("s-alpha1", alpha)
    _bind({session: [ALPHA_THREAD]})
    _ingest(hub, session, _slack(session, ALPHA_THREAD, at=T0, ts="1700000100.000200"))
    activity.stop()
    assert _timer().workspace_id == alpha


def test_the_sweep_runs_every_time_sweep_seconds(activity, clock, monkeypatch, caplog):
    monkeypatch.setenv(cfg.ENV_TIME_SWEEP_SECONDS, "7")
    monkeypatch.setattr(timetrack_activity, "_SWEEP_WARNED", set())
    with caplog.at_level(logging.WARNING, logger="lmer_platform.timetrack_activity"):
        activity.tick(T0)
    assert activity._next_sweep == T0 + timedelta(seconds=7)
    assert "platform_time_sweep_settings_unreadable" not in caplog.text


def test_an_unreadable_sweep_interval_warns_once_per_error_and_idle_timers_still_close(
        activity, tracker, clock, alpha, monkeypatch, caplog):
    monkeypatch.setattr(timetrack_activity, "_SWEEP_WARNED", set())
    errors = iter(["first"] * 3 + ["second"] * 10)

    def unreadable():
        raise ValueError(next(errors))

    monkeypatch.setattr(timetrack_activity, "time_settings", unreadable)
    tracker.note_activity(PERSON, alpha, at=T0 - timedelta(minutes=5))
    tracker.note_activity(PERSON, alpha, at=T0)
    default = cfg.TIME_INT_SETTINGS["time_sweep_seconds"].default
    with caplog.at_level(logging.WARNING, logger="lmer_platform.timetrack_activity"):
        at = T0
        for _ in range(5):
            at += timedelta(seconds=default)
            activity.tick(at)
            assert activity._next_sweep == at + timedelta(seconds=default)
        clock.now = at + timedelta(minutes=20)
        activity.tick(clock.now)
    warned = [record.getMessage() for record in caplog.records
              if "platform_time_sweep_settings_unreadable" in record.getMessage()]
    assert len(warned) == 2 and "first" in warned[0] and "second" in warned[1], warned
    [entry] = _entries()
    assert (entry.end, entry.trigger.stop) == (T0, "idle"), "the sweep closes an idle timer regardless"


# --- daemon wiring -----------------------------------------------------------

@pytest.fixture
def daemon_root(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", cfg.ENV_BIND_ADDRESS, cfg.ENV_BIND_PORT):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    store.write_json(cfg.config_path(), {"people": list(PEOPLE), "default_person": PERSON})
    noop = SimpleNamespace(stop=lambda: None)
    for name in ("_supervise_assistant", "_watch_for_attention", "_run_thread_connectors",
                 "_serve_thread_requests"):
        monkeypatch.setattr(daemon, name, lambda *args: noop)
    yield root
    timetrack.register_tracker(None)
    timesheet_writer.register_writer(None)
    comms.register_hub(None)


def _fakes(monkeypatch, calls, *, activity_fails=False):
    class Hub:
        def __init__(self):
            self.consumers = []

        def add_consumer(self, consumer):
            self.consumers.append(consumer)
            calls.append(("hub.add_consumer", consumer))

        def start(self):
            # The time unit stays on the hub for the daemon's life; what counts
            # is the activity consumer it forwards to as the hub starts.
            calls.append(("hub.start", tuple(getattr(consumer, "activity", consumer)
                                             for consumer in self.consumers)))

        def stop(self):
            calls.append(("hub.stop",))

    class Writer:
        def __init__(self, config):
            calls.append(("writer.new",))

        def start(self):
            calls.append(("writer.start",))

        def stop(self):
            calls.append(("writer.stop",))

    class Activity:
        def __init__(self, config, tracker, people):
            assert isinstance(tracker, Tracker) and isinstance(people, Registry)
            calls.append(("activity.new",))

        def __call__(self, event):
            pass

        def start(self):
            if activity_fails:
                raise RuntimeError("activity could not start")
            calls.append(("activity.start",))

        def stop(self):
            calls.append(("activity.stop",))

    monkeypatch.setattr(daemon, "CommsHub", Hub)
    monkeypatch.setattr(timetrack_live, "TimesheetWriter", Writer)
    monkeypatch.setattr(timetrack_live, "ActivityConsumer", Activity)
    return Activity


def _serve(monkeypatch, calls):
    def run(app, **kwargs):
        calls.append(("serve", timetrack.get_tracker(), timesheet_writer.get_writer(),
                      timetrack_activity.get_activity()))

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=run))


def _names(calls):
    return [call[0] for call in calls]


def test_the_daemon_registers_the_consumer_before_the_hub_starts_and_stops_it_all(
        daemon_root, monkeypatch):
    calls = []
    activity_class = _fakes(monkeypatch, calls)
    _serve(monkeypatch, calls)

    assert daemon.main(["run"]) == 0

    names = _names(calls)
    assert names[:4] == ["writer.new", "writer.start", "activity.new", "activity.start"]
    started = next(call for call in calls if call[0] == "hub.start")
    assert any(isinstance(consumer, activity_class) for consumer in started[1]), \
        "the activity consumer was not on the hub when it started"
    serve = next(call for call in calls if call[0] == "serve")
    assert isinstance(serve[1], Tracker) and serve[2] is not None and isinstance(serve[3], activity_class)
    assert names[names.index("serve"):] == ["serve", "hub.stop", "activity.stop", "writer.stop"]
    assert timetrack.get_tracker() is None
    assert timesheet_writer.get_writer() is None
    assert timetrack_activity.get_activity() is None


def test_tracking_disabled_starts_nothing_and_says_why(daemon_root, monkeypatch, caplog):
    store.write_json(cfg.config_path(), {"people": "everyone", "default_person": PERSON})
    calls = []
    _fakes(monkeypatch, calls)
    _serve(monkeypatch, calls)

    with caplog.at_level(logging.WARNING, logger="lmer_platform.timetrack_live"):
        assert daemon.main(["run"]) == 0

    assert not any(name.startswith(("writer.", "activity.")) for name in _names(calls))
    assert next(call for call in calls if call[0] == "hub.start")[1][1:] == (None,)
    assert next(call for call in calls if call[0] == "serve")[1:] == (None, None, None)
    [warning] = [record for record in caplog.records if record.name == "lmer_platform.timetrack_live"
                 and "platform_time_tracking_disabled" in record.getMessage()]
    assert "people" in warning.getMessage()


def test_a_part_that_fails_to_start_leaves_the_daemon_serving_untracked(
        daemon_root, monkeypatch, caplog):
    calls = []
    _fakes(monkeypatch, calls, activity_fails=True)
    _serve(monkeypatch, calls)

    with caplog.at_level(logging.WARNING, logger="lmer_platform.timetrack_live"):
        assert daemon.main(["run"]) == 0

    names = _names(calls)
    assert names.index("writer.stop") < names.index("serve"), "the started writer was not stopped"
    assert "activity.stop" not in names
    assert next(call for call in calls if call[0] == "hub.start")[1][1:] == (None,)
    assert next(call for call in calls if call[0] == "serve")[1:] == (None, None, None)
    assert "platform_time_tracking_start_failed" in caplog.text


# --- which run an activity is for (issue #665, D21) --------------------------

ISSUE_12 = "https://forge.test/team/app/-/issues/12"
ISSUE_13 = "https://forge.test/team/app/-/issues/13"


def _worker(session, home, slug, *, target=None, uid=None, kind="worker", track=True):
    """A session in *home* whose run the platform tracks."""
    run = {"host": "forge.test", "project": "team/app", "slug": slug}
    if uid:
        run["uid"] = uid
    projects.bind_session(session, home)
    registry.register(session, kind=kind, pid=os.getpid(), run=run)
    if track:
        with project_context.bound(home):
            runs.track("forge.test", "team/app", slug, uid=uid, target=target)
    return session


def test_a_worker_session_names_one_candidate_with_its_title_and_issues(hub, activity, clock, alpha):
    session = _worker("s-alpha1", alpha, "fix-12", target=ISSUE_12)
    meta.write("forge.test", "team/app", "fix-12", title="Fix 12", issues=[ISSUE_13])
    _bind({session: [ALPHA_THREAD]})
    decision = _say(hub, activity, clock, session, ALPHA_THREAD, at=T0, ts="1700000100.000200")
    [facts] = decision.runs
    assert (facts.title, facts.issues) == ("Fix 12", (ISSUE_12, ISSUE_13))
    timer = _timer()
    assert (timer.identity, timer.title, timer.issues) == (facts.ref, "Fix 12", (ISSUE_12, ISSUE_13))


def test_an_assistant_session_names_no_run(hub, activity, clock, alpha):
    session = _worker("s-assist", alpha, "assistant-run", kind="assistant")
    _bind({session: [ALPHA_THREAD]})
    decision = _say(hub, activity, clock, session, ALPHA_THREAD, at=T0, ts="1700000100.000200")
    assert decision.runs == () and decision.refs == ()
    timer = _timer()
    assert (timer.identity, timer.title, timer.sessions) == (None, "", ())


def test_a_session_with_no_run_is_run_less(hub, activity, clock, alpha):
    session = _session("s-ratchet", alpha)
    _bind({session: [ALPHA_THREAD]})
    decision = _say(hub, activity, clock, session, ALPHA_THREAD, at=T0, ts="1700000100.000200")
    assert decision.runs == () and decision.result.outcome == STARTED
    assert _timer().identity is None


def test_two_workers_name_two_candidates_and_one_run_counts_once(hub, activity, clock, alpha):
    one = _worker("s-alpha1", alpha, "fix-12", uid=UID_A)
    two = _worker("s-alpha2", alpha, "fix-13")
    same = "s-alpha3"
    projects.bind_session(same, alpha)
    registry.register(same, kind="worker", pid=os.getpid(),
                      run={"host": "forge.test", "project": "team/app", "slug": "fix-12", "uid": UID_A})
    _bind({one: [ALPHA_THREAD], two: [ALPHA_THREAD], same: [ALPHA_THREAD]})
    decision = _say(hub, activity, clock, one, ALPHA_THREAD, at=T0, ts="1700000100.000200")
    assert [facts.title for facts in decision.runs] == ["fix-12", "fix-13"]
    assert _timer().identity is None


def test_a_session_with_an_untracked_run_names_nothing_and_still_counts(hub, activity, clock, alpha):
    session = _worker("s-alpha1", alpha, "unknown", track=False)
    _bind({session: [ALPHA_THREAD]})
    decision = _say(hub, activity, clock, session, ALPHA_THREAD, at=T0, ts="1700000100.000200")
    assert decision.runs == () and decision.result.outcome == STARTED


def test_a_session_the_registry_no_longer_lists_counts_by_its_tracked_run(hub, activity, clock, alpha):
    session = "s-gone"
    projects.bind_session(session, alpha)
    with project_context.bound(alpha):
        runs.track("forge.test", "team/app", "fix-12", target=ISSUE_12, session_id=session)
    _bind({session: [ALPHA_THREAD]})
    decision = _say(hub, activity, clock, session, ALPHA_THREAD, at=T0, ts="1700000100.000200")
    assert [facts.issues for facts in decision.runs] == [(ISSUE_12,)]


def test_building_candidates_failing_is_run_less_and_still_counts(
        hub, activity, clock, alpha, monkeypatch, caplog):
    session = _worker("s-alpha1", alpha, "fix-12")
    _bind({session: [ALPHA_THREAD]})

    def broken(sessions):
        raise RuntimeError("index")
    monkeypatch.setattr(timetrack_activity, "run_candidates", broken)
    caplog.set_level("INFO", logger="lmer_platform.timetrack_activity")
    decision = _say(hub, activity, clock, session, ALPHA_THREAD, at=T0, ts="1700000100.000200")
    assert decision.runs == () and decision.result.outcome == STARTED
    assert "platform_time_run_candidates_skipped" in caplog.text


def test_a_run_switch_in_the_project_splits_through_the_consumer(hub, activity, clock, alpha):
    first = _worker("s-alpha1", alpha, "fix-12", target=ISSUE_12)
    second = _worker("s-alpha2", alpha, "fix-13", target=ISSUE_13)
    _bind({first: [ALPHA_THREAD]})
    _say(hub, activity, clock, first, ALPHA_THREAD, at=T0, ts="1700000100.000200")
    _bind({second: [BETA_THREAD]})
    switched = _say(hub, activity, clock, second, BETA_THREAD, at=T0 + timedelta(minutes=5),
                    ts="1700000600.000200")
    assert switched.result.outcome == "switched"
    [entry] = _entries()
    assert (entry.trigger.stop, entry.issues) == ("switch", [ISSUE_12])
    assert _timer().issues == (ISSUE_13,)
