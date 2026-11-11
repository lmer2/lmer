"""The tracker core: one timer per person and the D4 rules (issue #604, task T5).

Every test runs on an injected clock in an isolated platform dir with a real
projects database (the ``fleet`` fixture) and a fixture work-repo mirror that
defines one handler, ``tracker-x``, with a required, patterned field. The
properties that matter: start touches only live state and stop queues one
complete entry (D8), slivers stay local (D11), manual overrides automatic
under the pause window (D4), required fields refuse or prompt (D19), defaults
and recents follow D12, idle timers close at their last activity (D1, D7), and
activity decided out of order never starts or switches time before what ran,
and the runs a timer covered reach its entry (issue #648).
"""
import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from lmer_cli import comms as bus
from lmer_platform import config as cfg
from lmer_platform import projects, runs, store, timesheet_store, timesheet_writer, timetrack
from lmer_platform.people import CREDENTIAL_OPERATOR, META_CREDENTIAL, Registry
from lmer_platform.timetrack import (
    AUTO, KEPT, MANUAL, NOTED, PROMPTED, REFRESHED, REFUSED, STARTED, STOPPED, SWITCHED,
    ADOPTED, REMEMBERED, SPLIT, TAKEN_OVER, UPDATED, Timer, Tracker, decide_activity,
    decide_manual_start,
    decide_run, is_idle, is_paused,
)
from lmer_platform.timetrack_activity import ActivityConsumer
from lmer_platform.timetrack_runs import RunFacts
from lmer_timesheet import SessionRef, entry_path, parse_entry
from tests.test_platform_projects import fleet  # noqa: F401

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
PERSON = "alex"


def _run(name, *issues, title=None, project="acme/widgets"):
    """The facts of run *name*, as the activity consumer hands them over."""
    key = f"git.example.com/{project}/{name}"
    return RunFacts(ref=SessionRef(run=key), key=key, title=name if title is None else title,
                    issues=tuple(issues))


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)
        return self.now


@pytest.fixture
def clock():
    return Clock(T0)


@pytest.fixture
def mirror(tmp_path):
    root = tmp_path / "mirror"
    handlers = root / "timesheet.d" / ".handlers"
    handlers.mkdir(parents=True)
    (handlers / "tracker-x.yaml").write_text(
        "name: tracker-x\nlabel: Tracker X\nsync: true\n"
        "fields:\n  - key: task_id\n    label: Tracker X task\n    required: true\n"
        "    pattern: '[a-z0-9]+'\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def platform(fleet, mirror):
    projects.migrate()
    return replace(fleet, work_repo_mirror=str(mirror))


@pytest.fixture
def tracker(platform, clock):
    tracker = Tracker(platform, now=clock)
    timetrack.register_tracker(tracker)
    yield tracker
    timetrack.register_tracker(None)
    timesheet_writer.register_writer(None)


def _project(name, **settings):
    row = projects.create(name)
    if settings:
        row = projects.update(row["id"], {"settings": settings})
    return row["id"]


@pytest.fixture
def webshop(platform):
    return _project("Webshop", time_handler="tracker-x", time_clients=["acme", "globex"])


@pytest.fixture
def docs(platform):
    return _project("Docs Site")


def _manual(tracker, workspace, at, **values):
    return tracker.start(PERSON, workspace, at=at, trigger="manual", **values)


def _entries():
    return [parse_entry(row.entry_yaml)[0] for row in timesheet_store.pending()]


def _timer_rows():
    with timesheet_store.database() as db:
        return db.execute("SELECT person, workspace_id FROM timers").fetchall()


# --- the D4 rules as pure functions -----------------------------------------

def _timer(workspace, mode, *, start=T0, last=T0):
    return Timer(person=PERSON, workspace_id=workspace, project_key=workspace, start=start,
                 last_activity=last, mode=mode, start_trigger="activity")


@pytest.mark.parametrize("timer, paused, expected", [
    (None, False, STARTED),
    (None, True, NOTED),
    (_timer("a", AUTO), False, REFRESHED),
    (_timer("b", AUTO), False, SWITCHED),
    (_timer("a", MANUAL), False, NOTED),
    (_timer("b", MANUAL), False, NOTED),
])
def test_decide_activity(timer, paused, expected):
    assert decide_activity(timer, "a", paused=paused) == expected


@pytest.mark.parametrize("timer", [None, _timer("a", AUTO), _timer("b", AUTO), _timer("a", MANUAL)])
def test_decide_activity_with_automatic_tracking_off_only_notes(timer):
    assert decide_activity(timer, "a", paused=False, automatic=False) == NOTED


@pytest.mark.parametrize("timer, expected", [
    (None, NOTED),
    (_timer("a", AUTO), REFRESHED),
    (_timer("b", AUTO), NOTED),
    (_timer("b", MANUAL), NOTED),
])
def test_a_late_activity_never_starts_or_switches(timer, expected):
    assert decide_activity(timer, "a", paused=False, late=True) == expected


def test_decide_manual_start_and_the_window_and_idle_rules():
    assert decide_manual_start(None, "a") == STARTED
    assert decide_manual_start(_timer("a", MANUAL), "a") == REFRESHED
    assert decide_manual_start(_timer("a", AUTO), "a") == SWITCHED
    assert decide_manual_start(_timer("b", MANUAL), "a") == SWITCHED
    assert is_paused(T0 + timedelta(hours=4), T0)
    assert not is_paused(T0, T0)
    assert not is_paused(None, T0)
    assert is_idle(_timer("a", AUTO), T0 + timedelta(minutes=15), 15)
    assert not is_idle(_timer("a", AUTO), T0 + timedelta(minutes=14), 15)
    assert not is_idle(_timer("a", MANUAL), T0 + timedelta(days=3), 15)


# --- start and stop ---------------------------------------------------------

def test_start_writes_no_outbox_row(tracker, docs, webshop, clock):
    assert _manual(tracker, docs, T0).outcome == STARTED
    assert tracker.note_activity("sam", docs, at=T0).outcome == STARTED
    assert timesheet_store.pending() == []
    assert tracker.kept_entries(PERSON) == []
    running = tracker.status(PERSON)["running"]
    assert running["workspace_id"] == docs
    assert running["project_key"] == "docs-site"
    assert running["mode"] == MANUAL


def test_stop_enqueues_one_complete_entry_with_every_field(tracker, webshop, clock):
    started = _manual(tracker, webshop, T0, client="acme", title="Checkout triage",
                      fields={"task_id": "86c1x2y3z"})
    assert started.outcome == STARTED, started
    stopped = tracker.stop(PERSON, webshop, at=T0 + timedelta(minutes=30), trigger="manual")
    assert stopped.outcome == STOPPED
    [row] = timesheet_store.pending()
    entry, problems = parse_entry(row.entry_yaml)
    assert problems == []
    assert entry == stopped.entry
    assert row.seq == stopped.seq
    assert row.path == str(entry_path(entry))
    assert entry.to_dict() == {
        "id": entry.id, "person": PERSON, "project": "webshop", "handler": "tracker-x",
        "fields": {"task_id": "86c1x2y3z"}, "client": "acme", "title": "Checkout triage",
        "start": "2026-10-01T09:00:00Z", "end": "2026-10-01T09:30:00Z", "duration": 1800,
        "trigger": {"start": "manual", "stop": "manual"}, "source": "lmer",
    }
    assert _timer_rows() == []
    assert tracker.status(PERSON)["running"] is None


def test_a_sliver_is_kept_locally_not_enqueued(tracker, docs, clock):
    _manual(tracker, docs, T0, title="Quick look")
    stopped = tracker.stop(PERSON, None, at=T0 + timedelta(seconds=30), trigger="manual")
    assert stopped.outcome == KEPT
    assert stopped.seq is None
    assert "60-second minimum" in stopped.kept
    assert timesheet_store.pending() == []
    [kept] = tracker.kept_entries(PERSON)
    assert kept["id"] == stopped.entry.id
    assert kept["workspace_id"] == docs
    assert (kept["project"], kept["title"], kept["duration"]) == ("docs-site", "Quick look", 30)
    assert kept["trigger"] == {"start": "manual", "stop": "manual"}
    assert kept["start"] == "2026-10-01T09:00:00Z" and kept["end"] == "2026-10-01T09:00:30Z"
    assert "minimum" in kept["reason"]


def test_the_minimum_length_is_read_fresh(tracker, docs, clock, monkeypatch):
    monkeypatch.setenv(cfg.ENV_TIME_MIN_ENTRY_SECONDS, "600")
    _manual(tracker, docs, T0)
    assert tracker.stop(PERSON, None, at=T0 + timedelta(minutes=5), trigger="manual").outcome == KEPT
    monkeypatch.setenv(cfg.ENV_TIME_MIN_ENTRY_SECONDS, "60")
    _manual(tracker, docs, T0 + timedelta(minutes=6))
    assert tracker.stop(PERSON, None, at=T0 + timedelta(minutes=11), trigger="manual").outcome == STOPPED


@pytest.fixture
def pause_hours(monkeypatch):
    """A four-hour pause after a manual stop; the default is none."""
    monkeypatch.setenv(cfg.ENV_TIME_PAUSE_HOURS, "4")


def test_by_default_a_manual_stop_opens_no_pause(tracker, docs, clock):
    tracker.note_activity(PERSON, docs, at=T0)
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=20), trigger="manual")
    assert tracker.status(PERSON)["pause"] is None
    with timesheet_store.database() as db:
        assert db.execute("SELECT COUNT(*) FROM pauses").fetchone()[0] == 0
    started = tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=21))
    assert (started.outcome, started.timer.mode) == (STARTED, AUTO)


def test_stop_with_nothing_running_still_opens_the_pause(tracker, docs, clock, pause_hours):
    result = tracker.stop(PERSON, None, at=T0, trigger="manual")
    assert result.outcome == NOTED and result.reasons == ("no timer is running",)
    assert tracker.status(PERSON)["pause"]["until"] == "2026-10-01T13:00:00Z"
    assert tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=1)).outcome == NOTED


def test_stop_naming_another_project_is_refused(tracker, docs, webshop, clock):
    _manual(tracker, docs, T0)
    refused = tracker.stop(PERSON, webshop, at=T0 + timedelta(hours=1), trigger="manual")
    assert refused.outcome == REFUSED
    assert "docs-site" in refused.reasons[0]
    assert tracker.status(PERSON)["running"]["workspace_id"] == docs
    assert tracker.status(PERSON)["pause"] is None


def test_one_timer_per_person(tracker, docs, webshop, clock):
    _manual(tracker, docs, T0)
    switched = _manual(tracker, webshop, T0 + timedelta(minutes=20), fields={"task_id": "abc"})
    assert switched.outcome == SWITCHED
    assert switched.timer.workspace_id == webshop
    [entry] = _entries()
    assert (entry.project, entry.duration) == ("docs-site", 1200)
    assert (entry.trigger.start, entry.trigger.stop) == ("manual", "switch")
    tracker.note_activity("sam", docs, at=T0)
    assert sorted((row["person"], row["workspace_id"]) for row in _timer_rows()) == [
        (PERSON, webshop), ("sam", docs)]


def test_manual_start_on_the_running_manual_project_fills_in_place_and_splits_on_a_change(
        tracker, docs, clock):
    """D24 (#665): a manual start on its running manual project edits like PATCH."""
    _manual(tracker, docs, T0)
    filled = _manual(tracker, docs, T0 + timedelta(minutes=2), title="First")
    assert (filled.outcome, filled.timer.start) == (REFRESHED, T0)
    again = _manual(tracker, docs, T0 + timedelta(minutes=5), title="Renamed")
    assert (again.outcome, again.timer.start) == (SPLIT, T0 + timedelta(minutes=5))
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="manual")
    assert [(entry.title, entry.duration) for entry in _entries()] == [("First", 300), ("Renamed", 300)]


def test_a_manual_start_takes_over_an_automatic_timer(tracker, docs, clock):
    tracker.note_activity(PERSON, docs, at=T0, runs=(_run("docs-run"),))
    taken = _manual(tracker, docs, T0 + timedelta(minutes=10), title="Writing")
    assert taken.outcome == SWITCHED
    assert taken.timer.mode == MANUAL
    [entry] = _entries()
    assert (entry.trigger.start, entry.trigger.stop, entry.duration) == ("activity", "switch", 600)


# --- manual overrides automatic (D4) ----------------------------------------

def test_manual_is_immune_to_idle_and_to_activity_elsewhere(tracker, docs, webshop, clock):
    _manual(tracker, docs, T0)
    assert tracker.close_idle(clock.advance(hours=6)) == []
    noted = tracker.note_activity(PERSON, webshop, at=clock.now)
    assert noted.outcome == NOTED
    assert "manual timer" in noted.reasons[0]
    running = tracker.status(PERSON)["running"]
    assert (running["workspace_id"], running["mode"]) == (docs, MANUAL)
    assert running["elapsed_seconds"] == 6 * 3600
    assert timesheet_store.pending() == []


def test_pause_after_manual_stop_notes_a_late_reply(tracker, docs, clock, pause_hours):
    tracker.note_activity(PERSON, docs, at=T0)
    stop_at = T0 + timedelta(minutes=20)
    tracker.stop(PERSON, None, at=stop_at, trigger="manual")
    late = tracker.note_activity(PERSON, docs, at=stop_at + timedelta(minutes=1))
    assert late.outcome == NOTED
    assert "paused until 2026-10-01T13:20:00Z" in late.reasons[0]
    # A message sent before the stop but processed after it is noted as well.
    assert tracker.note_activity(PERSON, docs, at=stop_at - timedelta(seconds=5)).outcome == NOTED
    assert _timer_rows() == []
    pause = tracker.status(PERSON)["pause"]
    assert pause["until"] == "2026-10-01T13:20:00Z"


def test_pause_is_lifted_by_a_manual_start(tracker, docs, webshop, clock, pause_hours):
    tracker.stop(PERSON, None, at=T0, trigger="manual")
    assert tracker.status(PERSON)["pause"] is not None
    _manual(tracker, docs, T0 + timedelta(minutes=5))
    assert tracker.status(PERSON)["pause"] is None
    with timesheet_store.database() as db:
        assert db.execute("SELECT COUNT(*) FROM pauses").fetchone()[0] == 0


def test_pause_is_lifted_when_the_window_expires(tracker, docs, clock, monkeypatch):
    monkeypatch.setenv(cfg.ENV_TIME_PAUSE_HOURS, "2")
    tracker.stop(PERSON, None, at=T0, trigger="manual")
    assert tracker.note_activity(PERSON, docs, at=T0 + timedelta(hours=1, minutes=59)).outcome == NOTED
    started = tracker.note_activity(PERSON, docs, at=T0 + timedelta(hours=2))
    assert started.outcome == STARTED
    assert started.timer.mode == AUTO


def test_automatic_tracking_off_notes_activity_until_it_is_turned_on(tracker, docs, webshop, clock):
    tracker.note_activity(PERSON, docs, at=T0)
    clock.now = T0 + timedelta(minutes=2)
    assert tracker.set_automatic(PERSON, False).outcome == UPDATED
    assert tracker.status(PERSON)["automatic"] == {"on": False, "off_since": "2026-10-01T09:02:00Z"}

    # The running automatic timer is no longer refreshed, so it ends at its last activity.
    noted = tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=5))
    assert noted.outcome == NOTED
    assert noted.reasons == ("automatic tracking is off since 2026-10-01T09:02:00Z",)
    [closed] = tracker.close_idle(T0 + timedelta(minutes=16))
    assert (closed.entry.end, closed.entry.trigger.stop) == (T0, "idle")
    # Nothing starts or prompts, the restart of the daemon included: the flag is stored.
    restarted = Tracker(tracker.config, now=clock)
    assert restarted.note_activity(PERSON, webshop, at=T0 + timedelta(minutes=20)).outcome == NOTED
    assert restarted.status(PERSON)["prompts"] == []
    assert restarted.status(PERSON)["automatic"]["on"] is False

    # Turning it off again keeps the first time it went off.
    clock.now = T0 + timedelta(minutes=30)
    restarted.set_automatic(PERSON, False)
    assert restarted.status(PERSON)["automatic"]["off_since"] == "2026-10-01T09:02:00Z"

    # Manual time still works while it is off, and turning it on lets activity start time again.
    assert _manual(restarted, docs, T0 + timedelta(minutes=30)).outcome == STARTED
    assert restarted.stop(PERSON, None, at=T0 + timedelta(minutes=40), trigger="manual").outcome == STOPPED
    assert restarted.status(PERSON)["automatic"]["on"] is False, "a manual start or stop leaves it off"
    restarted.set_automatic(PERSON, True)
    assert restarted.status(PERSON)["automatic"] == {"on": True, "off_since": None}
    started = restarted.note_activity(PERSON, docs, at=T0 + timedelta(minutes=41))
    assert (started.outcome, started.timer.mode) == (STARTED, AUTO)


def _auto_timer_then_off(tracker, docs, clock):
    tracker.note_activity(PERSON, docs, at=T0)
    tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=3))
    clock.now = T0 + timedelta(minutes=4)
    tracker.set_automatic(PERSON, False)


def test_turning_automatic_tracking_on_within_the_grace_ends_the_stale_timer_at_its_last_activity(
        tracker, docs, webshop, clock):
    _auto_timer_then_off(tracker, docs, clock)
    clock.now = T0 + timedelta(minutes=10)
    turned_on = tracker.set_automatic(PERSON, True)
    assert (turned_on.outcome, turned_on.timer) == (UPDATED, None)
    [entry] = _entries()
    assert (entry.end, entry.trigger.stop) == (T0 + timedelta(minutes=3), "idle")
    # The next message starts fresh time; it neither refreshes nor switches the old timer.
    started = tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=12))
    assert (started.outcome, started.timer.start) == (STARTED, T0 + timedelta(minutes=12))
    assert len(_entries()) == 1


def test_turning_automatic_tracking_on_when_it_was_never_off_keeps_the_live_timer(tracker, docs, clock):
    tracker.note_activity(PERSON, docs, at=T0)
    clock.now = T0 + timedelta(minutes=5)
    kept = tracker.set_automatic(PERSON, True)
    assert (kept.timer.mode, kept.timer.start, kept.entry) == (AUTO, T0, None)
    assert tracker.status(PERSON)["running"]["mode"] == AUTO
    assert _entries() == []


def test_turning_automatic_tracking_on_leaves_a_manual_timer_running(tracker, docs, clock):
    tracker.set_automatic(PERSON, False)
    _manual(tracker, docs, T0)
    clock.now = T0 + timedelta(minutes=10)
    assert tracker.set_automatic(PERSON, True).timer.mode == MANUAL
    assert _entries() == []


@pytest.mark.parametrize("end_it", ["manual start", "manual stop"])
def test_ending_a_stale_automatic_timer_while_tracking_is_off_records_no_off_time(
        tracker, docs, webshop, clock, end_it):
    _auto_timer_then_off(tracker, docs, clock)
    at = T0 + timedelta(minutes=10)
    if end_it == "manual start":
        assert _manual(tracker, webshop, at, fields={"task_id": "abc"}).outcome == SWITCHED
    else:
        assert tracker.stop(PERSON, None, at=at, trigger="manual").outcome == STOPPED
    [entry] = _entries()
    assert entry.end == T0 + timedelta(minutes=3)


def test_automatic_tracking_off_is_per_person(tracker, docs, clock):
    tracker.set_automatic("sam", False)
    assert tracker.note_activity(PERSON, docs, at=T0).outcome == STARTED
    assert tracker.note_activity("sam", docs, at=T0).outcome == NOTED


def test_a_non_manual_stop_opens_no_pause(tracker, docs, clock):
    tracker.note_activity(PERSON, docs, at=T0)
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=5), trigger="switch")
    assert tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=6)).outcome == STARTED


# --- automatic start, switch and refresh ------------------------------------

def test_activity_starts_refreshes_and_switches(tracker, docs, clock):
    ops = _project("Ops", time_clients=["acme"])
    assert tracker.note_activity(PERSON, docs, at=T0).outcome == STARTED
    refreshed = tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=10))
    assert refreshed.outcome == REFRESHED
    assert refreshed.timer.last_activity == T0 + timedelta(minutes=10)
    assert refreshed.timer.start == T0
    switched = tracker.note_activity(PERSON, ops, at=T0 + timedelta(minutes=12), runs=(_run("ops-run"),))
    assert switched.outcome == SWITCHED
    assert (switched.timer.workspace_id, switched.timer.start_trigger) == (ops, "activity")
    [entry] = _entries()
    assert (entry.project, entry.duration) == ("docs-site", 720)
    assert (entry.trigger.start, entry.trigger.stop) == ("activity", "switch")
    assert switched.entry == entry


def test_a_switch_from_a_stale_timer_ends_it_at_its_last_activity(tracker, docs, clock):
    ops = _project("Ops")
    tracker.note_activity(PERSON, docs, at=T0)
    tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=5))
    later = tracker.note_activity(PERSON, ops, at=T0 + timedelta(hours=1))
    assert later.outcome == STARTED
    [entry] = _entries()
    assert entry.end == T0 + timedelta(minutes=5)
    assert entry.trigger.stop == "idle"


# --- activity decided out of order ------------------------------------------
# Sessions deliver independently, so an older record can be decided after a
# newer one. It must never move time backwards: it is only noted.

def test_a_late_activity_from_another_project_is_only_noted(tracker, docs, clock):
    ops = _project("Ops")
    first, last = T0 + timedelta(seconds=3), T0 + timedelta(minutes=5)
    assert tracker.note_activity(PERSON, ops, at=first).outcome == STARTED
    late = tracker.note_activity(PERSON, docs, at=T0)
    assert late.outcome == NOTED
    assert late.reasons == ("an activity at 2026-10-01T09:00:00Z is older than the last one,"
                            " at 2026-10-01T09:00:03Z",)
    assert (late.timer.workspace_id, late.entry) == (ops, None)
    assert tracker.note_activity(PERSON, ops, at=last).outcome == REFRESHED
    assert timesheet_store.pending() == [] and tracker.kept_entries(PERSON) == []
    [closed] = tracker.close_idle(last + timedelta(minutes=15))
    assert (closed.entry.project, closed.entry.start, closed.entry.end) == ("ops", first, last)
    assert [entry.project for entry in _entries()] == ["ops"]
    assert tracker.kept_entries(PERSON) == []


def test_a_late_activity_on_the_running_project_still_refreshes(tracker, docs, clock):
    tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=5))
    late = tracker.note_activity(PERSON, docs, at=T0)
    assert late.outcome == REFRESHED
    assert late.timer.last_activity == T0 + timedelta(minutes=5)


def test_a_late_activity_never_reopens_time_before_the_last_stop(tracker, docs, clock):
    ops = _project("Ops")
    tracker.note_activity(PERSON, ops, at=T0)
    last = T0 + timedelta(minutes=5)
    tracker.note_activity(PERSON, ops, at=last)
    [closed] = tracker.close_idle(last + timedelta(minutes=15))
    assert closed.entry.end == last
    for workspace in (docs, ops):
        late = tracker.note_activity(PERSON, workspace, at=last - timedelta(minutes=1))
        assert late.outcome == NOTED, late
        assert "older than the last one, at 2026-10-01T09:05:00Z" in late.reasons[0]
    assert _timer_rows() == []
    assert tracker.note_activity(PERSON, docs, at=last).outcome == STARTED
    assert [entry.project for entry in _entries()] == ["ops"]
    assert tracker.kept_entries(PERSON) == []


def test_the_last_stop_outlives_the_prune_and_a_restart(platform, docs, clock, monkeypatch):
    monkeypatch.setenv(cfg.ENV_TIME_TITLE_MAX_AGE_DAYS, "1")
    stop = T0 + timedelta(hours=1)
    first = Tracker(platform, now=clock)
    assert first.start(PERSON, docs, at=T0, trigger="manual", title="release notes").outcome == STARTED
    clock.now = stop
    assert first.stop(PERSON, None, at=stop, trigger="manual").outcome == STOPPED
    clock.now = T0 + timedelta(days=2)
    restarted = Tracker(platform, now=clock)
    assert restarted.close_idle(clock.now) == []
    with timesheet_store.database() as db:
        kinds = [row["kind"] for row in db.execute("SELECT kind FROM recents WHERE person=?", (PERSON,))]
    late = restarted.note_activity(PERSON, docs, at=stop - timedelta(minutes=5))
    assert late.outcome == NOTED, late
    assert _timer_rows() == []
    assert kinds == ["stop"], "the prune kept the title, or dropped the stop"
    assert [entry.end for entry in _entries()] == [stop]


def test_a_late_activity_never_ends_the_running_timer_for_a_prompt(tracker, docs, webshop, clock):
    tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=5))
    late = tracker.note_activity(PERSON, webshop, at=T0 + timedelta(minutes=2))
    assert late.outcome == NOTED
    assert late.entry is None
    assert tracker.status(PERSON)["running"]["workspace_id"] == docs
    assert tracker.status(PERSON)["prompts"] == []
    assert timesheet_store.pending() == [] and tracker.kept_entries(PERSON) == []


def test_two_sessions_delivering_out_of_order_credit_only_the_newer_project(
        tracker, platform, docs, clock):
    ops = _project("Ops")
    platform = replace(platform, people=[{"key": PERSON, "name": "Alex"}], default_person=PERSON)
    consumer = ActivityConsumer(platform, tracker, Registry.from_config(platform), now=clock)
    projects.bind_session("s-docs", docs)
    projects.bind_session("s-ops", ops)
    seqs = {}

    def deliver(session, at):
        seq = seqs[session] = seqs.get(session, 0) + 1
        message = bus.Message(
            id=bus.message_id(session, seq), seq=seq, session=session,
            at=at.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            source=bus.Source(bus.CHANNEL_PLATFORM, author="operator"), kind="message", text="hello",
            meta={META_CREDENTIAL: CREDENTIAL_OPERATOR},
        ).to_dict()
        consumer(SimpleNamespace(session=session, message=message))
        [decision] = consumer.tick()
        return decision

    first, last = T0 + timedelta(seconds=3), T0 + timedelta(minutes=5)
    try:
        clock.now = first
        assert deliver("s-ops", first).result.outcome == STARTED
        # s-docs's subscriber was backing off: its older record arrives now.
        clock.advance(seconds=30)
        late = deliver("s-docs", T0)
        assert (late.workspace_id, late.result.outcome) == (docs, NOTED)
        clock.now = last
        assert deliver("s-ops", last).result.outcome == REFRESHED
        [closed] = consumer.sweep(last + timedelta(minutes=15))
    finally:
        consumer.stop()
    assert (closed.entry.project, closed.entry.start, closed.entry.end) == ("ops", first, last)
    assert [entry.project for entry in _entries()] == ["ops"]
    assert tracker.kept_entries(PERSON) == []


# --- required handler fields (D19) ------------------------------------------

def test_manual_start_is_refused_without_required_fields(tracker, webshop, clock):
    refused = _manual(tracker, webshop, T0)
    assert refused.outcome == REFUSED
    assert refused.missing == ("task_id",)
    assert refused.reasons == ("task_id: required (Tracker X task)",)
    bad = _manual(tracker, webshop, T0, fields={"task_id": "NOT VALID"})
    assert bad.outcome == REFUSED
    assert "does not match" in bad.reasons[0]
    assert _timer_rows() == []
    ok = _manual(tracker, webshop, T0, fields={"task_id": "abc", "unknown": "x"})
    assert ok.outcome == STARTED
    assert ok.timer.fields == {"task_id": "abc"}
    assert any("unknown" in warning for warning in ok.warnings)


def test_automatic_start_reuses_the_last_value_per_field(tracker, webshop, clock):
    _manual(tracker, webshop, T0, fields={"task_id": "abc123"}, title="Checkout bug triage")
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=30), trigger="manual")
    started = tracker.note_activity(PERSON, webshop, at=T0 + timedelta(hours=5))
    assert started.outcome == STARTED, started
    assert started.timer.fields == {"task_id": "abc123"}
    assert started.timer.title == "Checkout bug triage"
    assert started.timer.client == "acme"


def test_automatic_start_without_a_value_prompts_and_a_value_clears_it(tracker, webshop, docs, clock):
    prompted = tracker.note_activity(PERSON, webshop, at=T0)
    assert prompted.outcome == PROMPTED
    assert prompted.missing == ("task_id",)
    assert _timer_rows() == []
    [prompt] = tracker.status(PERSON)["prompts"]
    assert prompt["workspace_id"] == webshop
    assert prompt["message"] == "Webshop needs a Tracker X task to track time"
    assert prompt["missing"] == [{"key": "task_id", "label": "Tracker X task", "per_issue": False}]
    # Another project's manual start leaves the prompt standing.
    _manual(tracker, docs, T0 + timedelta(minutes=1))
    assert len(tracker.status(PERSON)["prompts"]) == 1
    _manual(tracker, webshop, T0 + timedelta(minutes=2), fields={"task_id": "abc"})
    assert tracker.status(PERSON)["prompts"] == []


def test_a_recent_value_clears_a_prompt(tracker, webshop, clock):
    tracker.note_activity(PERSON, webshop, at=T0)
    assert tracker.status(PERSON)["prompts"]
    with timesheet_store.database() as db:
        timetrack._remember(db, PERSON, "webshop", at=T0, cutoff=timetrack._stamp(T0 - timedelta(days=1)),
                            fields={"task_id": "abc"})
    assert tracker.status(PERSON)["prompts"] == []


def test_a_switch_into_a_prompting_project_ends_the_running_one(tracker, docs, webshop, clock):
    tracker.note_activity(PERSON, docs, at=T0)
    moved = tracker.note_activity(PERSON, webshop, at=T0 + timedelta(minutes=10))
    assert moved.outcome == PROMPTED
    assert moved.entry.project == "docs-site"
    assert moved.entry.trigger.stop == "switch"
    assert _timer_rows() == []


# --- a configured handler that is not defined -------------------------------
# Never tracked under ``timesheet`` instead: that handler's sync process would
# never see the entries.

def test_a_manual_start_under_an_undefined_handler_is_refused(tracker, docs, clock):
    odd = _project("Odd", time_handler="tracker-y")
    _manual(tracker, docs, T0)
    refused = _manual(tracker, odd, T0 + timedelta(minutes=5), title="Work")
    assert refused.outcome == REFUSED
    assert refused.reasons == ("handler 'tracker-y' is not defined in the work repo;"
                               " time on this project does not start until it is",)
    assert tracker.status(PERSON)["running"]["workspace_id"] == docs
    assert timesheet_store.pending() == []


def test_a_manual_start_under_an_unusable_handler_is_refused(tracker, mirror, clock):
    (mirror / "timesheet.d" / ".handlers" / "broken.yaml").write_text("name: broken\nfields: nope\n")
    odd = _project("Odd", time_handler="broken")
    refused = _manual(tracker, odd, T0)
    assert refused.outcome == REFUSED
    assert "handler 'broken' is unusable" in refused.reasons[0]
    assert _timer_rows() == []


def test_an_automatic_start_under_an_undefined_handler_prompts(tracker, docs, clock):
    odd = _project("Odd", time_handler="tracker-y")
    prompted = tracker.note_activity(PERSON, odd, at=T0)
    assert prompted.outcome == PROMPTED
    assert prompted.reasons == ("Odd needs a usable definition of handler 'tracker-y' to track time",)
    assert _timer_rows() == []
    [prompt] = tracker.status(PERSON)["prompts"]
    assert prompt["workspace_id"] == odd
    assert prompt["message"] == "Odd needs a usable definition of handler 'tracker-y' to track time"
    # A value remembered for the project does not clear it: no field is missing.
    _manual(tracker, docs, T0 + timedelta(minutes=1))
    assert len(tracker.status(PERSON)["prompts"]) == 1


def test_a_switch_into_an_undefined_handler_ends_the_running_timer(tracker, docs, clock):
    odd = _project("Odd", time_handler="tracker-y")
    tracker.note_activity(PERSON, docs, at=T0)
    moved = tracker.note_activity(PERSON, odd, at=T0 + timedelta(minutes=10))
    assert moved.outcome == PROMPTED
    assert (moved.entry.project, moved.entry.trigger.stop, moved.entry.duration) == ("docs-site", "switch", 600)
    assert _timer_rows() == []


def test_once_the_handler_is_defined_the_next_start_works(tracker, mirror, clock):
    odd = _project("Odd", time_handler="tracker-y")
    assert tracker.note_activity(PERSON, odd, at=T0).outcome == PROMPTED
    (mirror / "timesheet.d" / ".handlers" / "tracker-y.yaml").write_text(
        "name: tracker-y\nlabel: Tracker Y\nsync: true\nfields: []\n", encoding="utf-8")
    started = tracker.note_activity(PERSON, odd, at=T0 + timedelta(minutes=1))
    assert started.outcome == STARTED, started
    assert started.timer.handler == "tracker-y"
    assert tracker.status(PERSON)["prompts"] == []
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=11), trigger="manual")
    again = _manual(tracker, odd, T0 + timedelta(minutes=12))
    assert (again.outcome, again.timer.handler) == (STARTED, "tracker-y")
    assert [entry.handler for entry in _entries()] == ["tracker-y"]


def test_a_project_without_a_configured_handler_tracks_under_timesheet(tracker, docs, clock):
    started = tracker.note_activity(PERSON, docs, at=T0)
    assert (started.outcome, started.timer.handler, started.warnings) == (STARTED, "timesheet", ())
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=5), trigger="manual")
    manual = _manual(tracker, docs, T0 + timedelta(minutes=6))
    assert (manual.outcome, manual.timer.handler) == (STARTED, "timesheet")
    assert [w["kind"] for w in tracker.status(PERSON)["warnings"]] == ["no_client"]


def test_a_running_timer_whose_handler_disappears_keeps_it(tracker, webshop, mirror, clock):
    _manual(tracker, webshop, T0, fields={"task_id": "abc"})
    (mirror / "timesheet.d" / ".handlers" / "tracker-x.yaml").unlink()
    status = tracker.status(PERSON)
    assert status["running"]["handler"] == "tracker-x"
    [warning] = status["warnings"]
    assert warning["kind"] == "handler" and "'tracker-x' is not defined" in warning["message"]
    assert tracker.update_running(PERSON, title="Still going").outcome == UPDATED
    refused = tracker.update_running(PERSON, fields={"task_id": "def"})
    assert refused.outcome == REFUSED
    assert "'tracker-x' is not defined" in refused.reasons[0]
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="manual")
    [entry] = _entries()
    assert (entry.handler, entry.fields, entry.title) == ("tracker-x", {"task_id": "abc"}, "Still going")


# --- client labels, titles and recents (D12) --------------------------------

def test_default_client_and_title_fallbacks(tracker, docs, clock):
    ops = _project("Ops", time_clients=["acme", "globex"])
    first = tracker.note_activity(PERSON, ops, at=T0, runs=(_run("deploy-run"),))
    assert (first.timer.client, first.timer.title) == ("acme", "deploy-run")
    tracker.update_running(PERSON, title="Deploy review")
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="switch")
    # With no run, the most recent title; a run's own title wins over it (D22).
    second = tracker.note_activity(PERSON, ops, at=T0 + timedelta(minutes=20))
    assert (second.timer.title, second.timer.title_source) == ("Deploy review", "recent")
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=30), trigger="switch")
    bare = tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=40))
    assert (bare.timer.client, bare.timer.title) == ("", "")
    kinds = [warning["kind"] for warning in tracker.status(PERSON)["warnings"]]
    assert kinds == ["no_client"]


def test_a_new_client_label_is_added_to_the_project(tracker, webshop, clock):
    started = _manual(tracker, webshop, T0, client="initech", fields={"task_id": "abc"})
    assert started.timer.client == "initech"
    assert projects.get(webshop)["settings"]["time_clients"] == ["acme", "globex", "initech"]
    updated = tracker.update_running(PERSON, client="GLOBEX")
    assert updated.outcome == UPDATED
    assert updated.timer.client == "globex"
    assert projects.get(webshop)["settings"]["time_clients"] == ["acme", "globex", "initech"]
    refused = tracker.update_running(PERSON, client="x" * 40)
    assert refused.outcome == REFUSED
    assert "client label" in refused.reasons[0]


def test_recents_are_ordered_by_last_use_and_age_out(tracker, docs, clock):
    for minute, title in ((0, "Old"), (10, "Newer"), (20, "Newest")):
        _manual(tracker, docs, T0 + timedelta(minutes=minute), title=title)
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=25), trigger="manual")
    assert tracker.recents(PERSON, docs)["titles"] == ["Newest", "Newer", "Old"]
    clock.advance(days=31)
    assert tracker.recents(PERSON, docs)["titles"] == []
    started = tracker.note_activity(PERSON, docs, at=clock.now, runs=(_run("the-run"),))
    assert started.timer.title == "the-run"
    tracker.stop(PERSON, None, at=clock.now, trigger="switch")
    tracker.close_idle(clock.now)
    with timesheet_store.database() as db:
        kept = [row["value"] for row in db.execute("SELECT value FROM recents WHERE kind='title'")]
    assert kept == ["the-run"]


def test_kept_rows_and_prompts_age_out(tracker, docs, webshop, clock):
    _manual(tracker, docs, T0)
    tracker.stop(PERSON, None, at=T0 + timedelta(seconds=5), trigger="switch")
    tracker.note_activity(PERSON, webshop, at=T0 + timedelta(minutes=1))
    assert tracker.kept_entries(PERSON) and tracker.status(PERSON)["prompts"]
    tracker.close_idle(clock.advance(days=31))
    assert tracker.kept_entries(PERSON) == []
    assert tracker.status(PERSON)["prompts"] == []
    with timesheet_store.database() as db:
        assert db.execute("SELECT COUNT(*) FROM kept").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] == 0


# --- values edited while running (D8) ---------------------------------------

def test_values_edited_while_running_land_in_the_entry(tracker, webshop, clock):
    # Before the minimum entry length an edit stays in place (D24, #665).
    _manual(tracker, webshop, T0, title="Start title", fields={"task_id": "first"})
    clock.advance(seconds=30)
    updated = tracker.update_running(PERSON, title="Better title", client="globex",
                                     fields={"task_id": "second"})
    assert updated.outcome == UPDATED
    cleared = tracker.update_running(PERSON, fields={"task_id": ""})
    assert cleared.outcome == REFUSED and cleared.missing == ("task_id",)
    assert timesheet_store.pending() == []
    tracker.stop(PERSON, webshop, at=T0 + timedelta(minutes=40), trigger="manual")
    [entry] = _entries()
    assert (entry.title, entry.client, entry.fields) == ("Better title", "globex", {"task_id": "second"})
    assert entry.duration == 2400


def test_update_with_nothing_running_is_refused(tracker, clock):
    assert tracker.update_running(PERSON, title="x").outcome == REFUSED


# --- idle close and restart (D1, D7) ----------------------------------------

def test_idle_close_stamps_the_last_activity(tracker, docs, clock):
    tracker.note_activity(PERSON, docs, at=T0)
    tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=10))
    assert tracker.close_idle(T0 + timedelta(minutes=24)) == []
    [closed] = tracker.close_idle(T0 + timedelta(minutes=25))
    assert closed.outcome == STOPPED
    assert closed.entry.end == T0 + timedelta(minutes=10)
    assert (closed.entry.trigger.start, closed.entry.trigger.stop) == ("activity", "idle")
    assert _timer_rows() == []


def test_restart_closes_a_timer_left_open_at_its_last_activity(platform, docs, clock):
    before = Tracker(platform, now=clock)
    before.note_activity(PERSON, docs, at=T0)
    before.note_activity(PERSON, docs, at=T0 + timedelta(minutes=10))
    after = Tracker(platform, now=Clock(T0 + timedelta(hours=3)))
    [closed] = after.close_idle()
    assert closed.entry.end == T0 + timedelta(minutes=10)
    assert closed.entry.duration == 600
    [entry] = _entries()
    assert entry.trigger.stop == "idle"


def test_per_project_grace_is_honoured(tracker, docs, clock):
    slow = _project("Slow", time_idle_grace_minutes=60)
    tracker.note_activity(PERSON, slow, at=T0)
    tracker.note_activity("sam", docs, at=T0)
    tracker.note_activity(PERSON, slow, at=T0 + timedelta(minutes=2))
    tracker.note_activity("sam", docs, at=T0 + timedelta(minutes=2))
    [closed] = tracker.close_idle(T0 + timedelta(minutes=30))
    assert closed.entry.person == "sam"
    assert tracker.status(PERSON)["running"]["idle_at"] == "2026-10-01T10:02:00Z"
    [closed] = tracker.close_idle(T0 + timedelta(minutes=62))
    assert (closed.entry.person, closed.entry.project) == (PERSON, "slow")


def test_platform_grace_is_read_fresh(tracker, docs, clock, monkeypatch):
    monkeypatch.setenv(cfg.ENV_TIME_IDLE_GRACE_MINUTES, "5")
    tracker.note_activity(PERSON, docs, at=T0)
    tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=2))
    [closed] = tracker.close_idle(T0 + timedelta(minutes=7))
    assert closed.entry.end == T0 + timedelta(minutes=2)
    assert closed.outcome == STOPPED


# --- nothing reaches git -----------------------------------------------------

def _tree(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(root.rglob("*")) if path.is_file()}


def test_start_and_stop_touch_neither_the_clone_nor_the_mirror(tracker, webshop, docs, mirror, clock):
    before = _tree(mirror)
    _manual(tracker, webshop, T0, fields={"task_id": "abc"})
    tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=1))
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=30), trigger="manual")
    tracker.note_activity(PERSON, docs, at=T0 + timedelta(hours=5))
    tracker.note_activity(PERSON, docs, at=T0 + timedelta(hours=5, minutes=10))
    tracker.close_idle(T0 + timedelta(hours=6))
    assert len(timesheet_store.pending()) == 2
    assert not timesheet_writer.clone_path().exists()
    assert _tree(mirror) == before
    assert sorted(path.name for path in store.platform_dir().iterdir()
                  if path.name.startswith("timesheet")) == [timesheet_store.DB_FILE]


def test_a_stop_wakes_a_registered_writer(tracker, docs, clock):
    woken = []

    class Writer:
        def notify(self):
            woken.append(True)

    timesheet_writer.register_writer(Writer())
    _manual(tracker, docs, T0)
    assert woken == []
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=5), trigger="manual")
    assert woken == [True]


# --- status, the module calls and concurrency -------------------------------

def test_status_reports_warnings_the_outbox_and_the_writer_refusal(tracker, platform, clock):
    odd = _project("Odd")
    tracker.note_activity(PERSON, odd, at=T0, runs=(_run("odd-run"),))
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="switch")
    [row] = timesheet_store.pending()
    timesheet_store.mark_failed(row.seq, "push refused after 3 attempts: protected branch")
    clock.advance(minutes=20)
    tracker.note_activity(PERSON, odd, at=clock.now)
    projects.update(odd, {"settings": {"time_handler": "tracker-y"}})
    status = tracker.status(PERSON)
    assert status["person"] == PERSON
    assert status["enabled"] is True and status["problems"] == []
    running = status["running"]
    assert running["project_name"] == "Odd"
    assert running["handler"] == "timesheet"
    assert running["title"] == "odd-run"
    assert running["start"] == "2026-10-01T09:20:00Z"
    assert running["elapsed_seconds"] == 0
    kinds = sorted(warning["kind"] for warning in status["warnings"])
    assert kinds == ["handler", "no_client"]
    assert status["outbox"]["depth"] == 1
    assert "protected branch" in status["writer_refusal"]
    assert status["pause"] is None
    json.dumps(status)


def test_status_flags_a_required_field_missing_from_the_running_timer(tracker, platform, clock):
    ops = _project("Ops")
    tracker.note_activity(PERSON, ops, at=T0)
    projects.update(ops, {"settings": {"time_handler": "tracker-x"}})
    with timesheet_store.database() as db:
        timer = timetrack._read_timer(db, PERSON)
        timetrack._write_timer(db, replace(timer, handler="tracker-x"))
    [warning] = [w for w in tracker.status(PERSON)["warnings"] if w["kind"] == "missing_field"]
    assert warning["field"] == "task_id"
    assert warning["message"] == "Ops needs a Tracker X task to track time"


def test_module_calls_use_the_registered_tracker(tracker, docs, clock):
    started = timetrack.start_tracking_time(PERSON, docs, at=T0, trigger="manual", title="Via module")
    assert started.outcome == STARTED
    stopped = timetrack.stop_tracking_time(PERSON, docs, at=T0 + timedelta(minutes=5), trigger="manual")
    assert stopped.outcome == STOPPED
    assert stopped.to_dict()["entry"]["title"] == "Via module"
    timetrack.register_tracker(None)
    missing = timetrack.start_tracking_time(PERSON, docs, at=T0, trigger="manual")
    assert (missing.outcome, missing.reasons) == (REFUSED, ("time tracking is not running",))
    assert timetrack.stop_tracking_time(PERSON, docs, at=T0, trigger="manual").outcome == REFUSED


def test_bad_calls_are_refused_or_raise(tracker, docs, platform, clock):
    assert _manual(tracker, "no-such-project", T0).outcome == REFUSED
    assert tracker.start("../escape", docs, at=T0, trigger="manual").outcome == REFUSED
    assert _manual(tracker, docs, T0, title="x" * 201).outcome == REFUSED
    with pytest.raises(ValueError):
        tracker.start(PERSON, docs, at=T0, trigger="hand")
    with pytest.raises(ValueError):
        tracker.stop(PERSON, docs, at=T0, trigger="activity")
    with pytest.raises(ValueError):
        tracker.start(PERSON, docs, at=T0.replace(tzinfo=None), trigger="manual")
    disabled = Tracker(replace(platform, time_problems=("people[0] is broken",)), now=clock)
    refused = disabled.start(PERSON, docs, at=T0, trigger="manual")
    assert refused.outcome == REFUSED and "people[0] is broken" in refused.reasons[0]


def test_concurrent_activity_starts_one_timer(tracker, docs, clock):
    ops = _project("Ops")
    barrier = threading.Barrier(8)
    results = []

    def worker(n):
        barrier.wait(timeout=10)
        results.append(tracker.note_activity(PERSON, docs if n % 2 else ops,
                                             at=T0 + timedelta(seconds=n)))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(results) == 8
    assert [r.outcome for r in results].count(STARTED) == 1
    assert len(_timer_rows()) == 1
    switches = [r for r in results if r.outcome == SWITCHED]
    assert len(timesheet_store.pending()) + len(tracker.kept_entries(PERSON)) == len(switches)


# --- the runs an entry covered (issue #648) ----------------------------------

def _ref(n, *, uid=True):
    segment = f"{n:032x}" if uid else f"develop-issue-{n}"
    return SessionRef(run=f"git.example.com/acme/widgets/{segment}", uid=segment if uid else "")


def test_activity_gathers_runs_and_a_switch_hands_only_the_new_ones_on(tracker, docs, clock):
    ops = _project("Ops")
    a, b, c = _ref(1), _ref(2, uid=False), _ref(3)
    tracker.note_activity(PERSON, docs, at=T0, sessions=(a,))
    refreshed = tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=5), sessions=(b, a))
    assert refreshed.timer.sessions == (a, b)
    assert tracker.status(PERSON)["running"]["sessions"] == [a.to_dict(), b.to_dict()]
    switched = tracker.note_activity(PERSON, ops, at=T0 + timedelta(minutes=12), sessions=(c,))
    assert switched.outcome == SWITCHED
    assert switched.timer.sessions == (c,)
    [entry] = _entries()
    assert entry.sessions == [a, b]
    assert switched.entry.sessions == [a, b]


def test_a_noted_activity_on_another_project_adds_no_run(tracker, docs, clock):
    ops = _project("Ops")
    _manual(tracker, docs, T0, sessions=(_ref(1),))
    noted = tracker.note_activity(PERSON, ops, at=T0 + timedelta(minutes=1), sessions=(_ref(2),))
    assert noted.outcome == NOTED
    assert noted.timer.sessions == (_ref(1),)
    assert tracker.status(PERSON)["running"]["sessions"] == [_ref(1).to_dict()]


def test_activity_on_a_manual_timers_own_project_names_its_run_and_changes_nothing_else(
        tracker, docs, clock):
    started = _manual(tracker, docs, T0 + timedelta(minutes=2)).timer
    before = tracker.note_activity(PERSON, docs, at=T0, sessions=(_ref(9),))
    assert before.timer.sessions == ()
    worked = tracker.note_activity(PERSON, docs, at=T0 + timedelta(hours=2), sessions=(_ref(1),))
    assert worked.outcome == NOTED
    assert worked.timer == replace(started, sessions=(_ref(1),))
    stopped = tracker.stop(PERSON, None, at=T0 + timedelta(hours=2, minutes=5), trigger="manual")
    [entry] = _entries()
    assert entry.sessions == [_ref(1)]
    assert stopped.entry.sessions == [_ref(1)]
    assert (entry.start, entry.trigger.start) == (T0 + timedelta(minutes=2), "manual")


def test_a_manual_start_on_its_own_timer_adds_its_run(tracker, docs, clock):
    _manual(tracker, docs, T0, sessions=(_ref(1),))
    again = _manual(tracker, docs, T0 + timedelta(minutes=3), sessions=(_ref(2),))
    assert again.outcome == REFRESHED
    assert again.timer.sessions == (_ref(1), _ref(2))
    stopped = tracker.stop(PERSON, None, at=T0 + timedelta(minutes=9), trigger="manual")
    assert parse_entry(timesheet_store.pending()[0].entry_yaml)[0].sessions == [_ref(1), _ref(2)]
    assert stopped.entry.sessions == [_ref(1), _ref(2)]


def test_an_entry_with_no_runs_writes_no_sessions_key(tracker, docs, clock):
    _manual(tracker, docs, T0)
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=5), trigger="manual")
    [row] = timesheet_store.pending()
    assert "sessions" not in row.entry_yaml


def test_a_timer_gathers_at_most_the_cap_keeping_the_earliest(tracker, docs, clock):
    many = tuple(_ref(n) for n in range(timetrack.MAX_TIMER_SESSIONS + 5))
    started = tracker.note_activity(PERSON, docs, at=T0, sessions=many[:3])
    assert started.timer.sessions == many[:3]
    refreshed = tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=1), sessions=many)
    assert refreshed.timer.sessions == many[:timetrack.MAX_TIMER_SESSIONS]


def test_a_kept_sliver_keeps_its_runs(tracker, docs, clock):
    _manual(tracker, docs, T0, sessions=(_ref(1),))
    tracker.stop(PERSON, None, at=T0 + timedelta(seconds=30), trigger="manual")
    [kept] = tracker.kept_entries(PERSON)
    assert kept["sessions"] == [_ref(1).to_dict()]


def test_an_older_kept_table_gains_the_sessions_column(platform, docs, clock):
    # The kept table as !439 first created it, before #648 added the column.
    with timesheet_store.database() as db:
        db.execute("DROP TABLE kept")
        db.execute(
            "CREATE TABLE kept (id TEXT PRIMARY KEY, person TEXT NOT NULL,"
            " workspace_id TEXT NOT NULL, project_key TEXT NOT NULL, handler TEXT NOT NULL,"
            " fields TEXT NOT NULL DEFAULT '{}', client TEXT NOT NULL DEFAULT '',"
            " title TEXT NOT NULL DEFAULT '', start TEXT NOT NULL, end TEXT NOT NULL,"
            " duration INTEGER NOT NULL, trigger_start TEXT NOT NULL, trigger_stop TEXT NOT NULL,"
            " reason TEXT NOT NULL, at TEXT NOT NULL)")
        db.execute(
            "INSERT INTO kept VALUES ('old', ?, ?, 'docs-site', 'timesheet', '{}', '', '',"
            " ?, ?, 30, 'manual', 'manual', 'short', ?)",
            (PERSON, docs, T0.isoformat(timespec="microseconds"),
             (T0 + timedelta(seconds=30)).isoformat(timespec="microseconds"),
             T0.isoformat(timespec="microseconds")))
    tracker = Tracker(platform, now=clock)
    [old] = tracker.kept_entries(PERSON)
    assert old["sessions"] == []
    _manual(tracker, docs, T0 + timedelta(minutes=1), sessions=(_ref(1),))
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=1, seconds=20), trigger="manual")
    assert [row["sessions"] for row in tracker.kept_entries(PERSON)] == [[_ref(1).to_dict()], []]


def test_a_stored_reference_that_no_longer_validates_is_dropped(tracker, docs, clock):
    tracker.note_activity(PERSON, docs, at=T0, sessions=(_ref(1),))
    with timesheet_store.database() as db:
        meta = json.loads(db.execute("SELECT metadata FROM timers").fetchone()[0])
        meta["sessions"].append({"run": "not a key"})
        meta["sessions"].append("garbage")
        db.execute("UPDATE timers SET metadata=?", (json.dumps(meta),))
    assert tracker.status(PERSON)["running"]["sessions"] == [_ref(1).to_dict()]


# --- identity, issues, fields and prompts per work key (issue #665) ------

ISSUE_12 = "https://git.example.com/acme/widgets/-/issues/12"
ISSUE_13 = "https://git.example.com/acme/widgets/-/issues/13"
ISSUE_14 = "https://git.example.com/acme/widgets/-/issues/14"
MR_7 = "https://git.example.com/acme/widgets/-/merge_requests/7"


@pytest.fixture
def widgets(platform, mirror):
    """A project whose handler's ``task_id`` follows the issue."""
    (mirror / "timesheet.d" / ".handlers" / "tracker-p.yaml").write_text(
        "name: tracker-p\nlabel: Tracker P\nfields:\n  - key: task_id\n    label: Task\n"
        "    required: true\n    pattern: '[a-z0-9]+'\n    per_issue: true\n",
        encoding="utf-8",
    )
    return _project("Widgets", time_handler="tracker-p", time_clients=["acme"])


def _issue_map(mirror, text):
    path = mirror / "timesheet.d" / ".handlers" / "tracker-p" / "issues.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_start_from_a_run_takes_its_identity_issues_and_title(tracker, docs):
    run = _run("fix-12", ISSUE_12, MR_7, title="Checkout bug")
    started = tracker.note_activity(PERSON, docs, at=T0, runs=(run,), sessions=(run.ref,))
    timer = started.timer
    assert started.outcome == STARTED
    assert (timer.identity, timer.issues, timer.work_key) == (run.ref, (ISSUE_12, MR_7), ISSUE_12)
    assert (timer.title, timer.title_source) == ("Checkout bug", "run")
    assert tracker.status(PERSON)["running"]["identity"] == run.ref.to_dict()
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="manual")
    chore = _run("chore")
    again = tracker.note_activity(PERSON, docs, at=T0 + timedelta(hours=1), runs=(chore,))
    assert (again.timer.issues, again.timer.work_key) == ((), chore.key)


def test_a_run_less_start_has_no_identity_and_never_a_run_name(tracker, docs):
    started = tracker.note_activity(PERSON, docs, at=T0)
    assert (started.timer.identity, started.timer.title, started.timer.title_source) == (None, "", "")
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="switch")
    two = tracker.note_activity(PERSON, docs, at=T0 + timedelta(hours=1),
                                runs=(_run("a"), _run("b")))
    assert two.outcome == STARTED and two.timer.identity is None


def test_an_other_project_switch_takes_the_new_run_and_only_its_sessions(tracker, docs):
    ops = _project("Ops")
    first, second = _run("r"), _run("s")
    tracker.note_activity(PERSON, docs, at=T0, runs=(first,), sessions=(first.ref,))
    switched = tracker.note_activity(PERSON, ops, at=T0 + timedelta(minutes=10), runs=(second,),
                                     sessions=(second.ref,))
    assert switched.outcome == SWITCHED
    assert (switched.timer.identity, switched.timer.sessions) == (second.ref, (second.ref,))
    [entry] = _entries()
    assert entry.sessions == [first.ref]


def test_the_title_follows_the_run_and_is_read_again_at_the_end(platform, clock, docs):
    titles = {"r": "First"}
    tracker = Tracker(platform, now=clock, run_facts=lambda ref: _run("r", title=titles["r"]))
    tracker.note_activity(PERSON, docs, at=T0, runs=(_run("r", title="First"),))
    followed = tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=5),
                                     runs=(_run("r", title="Second"),))
    assert (followed.outcome, followed.timer.title) == (REFRESHED, "Second")
    assert _entries() == []
    titles["r"] = "Third"
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="manual")
    [entry] = _entries()
    assert entry.title == "Third"


def test_a_title_a_person_set_never_follows_the_run(platform, clock, docs):
    tracker = Tracker(platform, now=clock, run_facts=lambda ref: _run("r", title="Renamed"))
    started = tracker.start(PERSON, docs, at=T0, trigger="activity", title="Mine",
                            runs=(_run("r", title="First"),))
    assert (started.timer.title, started.timer.title_source) == ("Mine", "person")
    tracker.note_activity(PERSON, docs, at=T0 + timedelta(minutes=5),
                          runs=(_run("r", title="Renamed"),))
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="manual")
    [entry] = _entries()
    assert entry.title == "Mine"


def test_field_values_follow_the_work_key_before_the_map(tracker, widgets, mirror):
    _issue_map(mirror, f"{ISSUE_12}:\n  task_id: frommap\n")
    run = _run("fix-12", ISSUE_12)
    started = tracker.note_activity(PERSON, widgets, at=T0, runs=(run,))
    assert started.outcome == STARTED and started.timer.fields == {"task_id": "frommap"}
    tracker.update_running(PERSON, fields={"task_id": "mine"})
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="manual")
    again = tracker.note_activity(PERSON, widgets, at=T0 + timedelta(hours=1), runs=(run,))
    assert again.timer.fields == {"task_id": "mine"}
    tracker.stop(PERSON, None, at=T0 + timedelta(hours=1, minutes=10), trigger="manual")
    # per_issue: another issue never borrows the project's last value.
    other = tracker.note_activity(PERSON, widgets, at=T0 + timedelta(hours=2),
                                  runs=(_run("fix-13", ISSUE_13),))
    assert other.outcome == PROMPTED and other.missing == ("task_id",)


def test_a_map_correction_is_picked_up_by_the_next_entry(tracker, widgets, mirror):
    _issue_map(mirror, f"{ISSUE_12}:\n  task_id: t12\n")
    assert _say(tracker, widgets, T0, R).timer.fields == {"task_id": "t12"}
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="manual")
    _issue_map(mirror, f"{ISSUE_12}:\n  task_id: fixed12\n")
    again = _say(tracker, widgets, T0 + timedelta(hours=1), R)
    assert (again.outcome, again.timer.fields) == (STARTED, {"task_id": "fixed12"})


def test_a_person_set_value_carried_to_the_next_entry_stays_fresh(tracker, widgets, mirror):
    _issue_map(mirror, f"{ISSUE_12}:\n  task_id: t12\n")

    def last_used():
        with timesheet_store.database() as db:
            return db.execute("SELECT value, last_used FROM key_values WHERE work_key=?",
                              (ISSUE_12,)).fetchone()

    _manual(tracker, widgets, T0, fields={"task_id": "mine"}, runs=(R,))
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="manual")
    first = last_used()
    assert _say(tracker, widgets, T0 + timedelta(hours=1), R).timer.fields == {"task_id": "mine"}
    tracker.stop(PERSON, None, at=T0 + timedelta(hours=1, minutes=10), trigger="manual")
    later = last_used()
    assert later["value"] == "mine" and later["last_used"] > first["last_used"]


def test_a_carried_person_value_survives_an_expiry_sweep_during_its_entry(tracker, widgets, mirror):
    _issue_map(mirror, f"{ISSUE_12}:\n  task_id: t12\n")
    _manual(tracker, widgets, T0, fields={"task_id": "mine"}, runs=(R,))
    stopped = T0 + timedelta(minutes=10)
    tracker.stop(PERSON, None, at=stopped, trigger="manual")
    start = stopped + timedelta(days=30) - timedelta(minutes=5)
    assert _say(tracker, widgets, start, R).timer.fields == {"task_id": "mine"}
    sweep = start + timedelta(minutes=6)
    _say(tracker, widgets, sweep, R)
    tracker.close_idle(sweep)
    tracker.stop(PERSON, None, at=sweep + timedelta(minutes=4), trigger="manual")
    again = _say(tracker, widgets, sweep + timedelta(hours=1), R)
    assert again.timer.fields == {"task_id": "mine"}


def _key_memory(work_key):
    with timesheet_store.database() as db:
        row = db.execute("SELECT value FROM key_values WHERE work_key=?", (work_key,)).fetchone()
    return row["value"] if row else None


def _prompt_in(tracker, workspace, work_key):
    [prompt_id] = [item["id"] for item in tracker.status(PERSON)["prompts"]
                   if item["work_key"] == work_key and item["workspace_id"] == workspace]
    return prompt_id


def test_filling_a_timer_from_its_run_s_prompt_remembers_nothing_for_its_issue(
        tracker, widgets, mirror):
    gadgets = _project("Gadgets", time_handler="tracker-p", time_clients=["acme"])
    run = _run("r")
    assert _say(tracker, widgets, T0, run).outcome == PROMPTED
    assert _say(tracker, gadgets, T0 + timedelta(minutes=1), run).outcome == PROMPTED
    _manual(tracker, widgets, T0 + timedelta(minutes=2), fields={"task_id": "x"}, runs=(_run("other"),))
    switched = _say(tracker, widgets, T0 + timedelta(minutes=20), _run("r", ISSUE_13))
    assert (switched.timer.work_key, switched.timer.unassigned) == (ISSUE_13, ("task_id",))
    tracker.answer_prompt(PERSON, _prompt_in(tracker, gadgets, run.key),
                          at=T0 + timedelta(minutes=21), fields={"task_id": "runmemory"})
    filled = tracker.answer_prompt(PERSON, _prompt_in(tracker, widgets, run.key),
                                   at=T0 + timedelta(minutes=22))
    assert (filled.outcome, filled.timer.fields) == (UPDATED, {"task_id": "runmemory"})
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=30), trigger="manual")
    assert _key_memory(ISSUE_13) is None
    _issue_map(mirror, f"{ISSUE_13}:\n  task_id: correctedmap\n")
    again = _say(tracker, widgets, T0 + timedelta(hours=1), _run("r", ISSUE_13))
    assert again.timer.fields == {"task_id": "correctedmap"}


def test_a_carried_value_never_overwrites_a_newer_answer_for_its_key(tracker, widgets):
    gadgets = _project("Gadgets", time_handler="tracker-p", time_clients=["acme"])
    run = _run("fix-13", ISSUE_13)
    assert _say(tracker, gadgets, T0, run).outcome == PROMPTED
    _manual(tracker, widgets, T0 + timedelta(minutes=1), fields={"task_id": "old"}, runs=(run,))
    assert _say(tracker, widgets, T0 + timedelta(minutes=20), _run("other")).outcome == SWITCHED
    back = _say(tracker, widgets, T0 + timedelta(minutes=40), run)
    assert (back.timer.fields, back.timer.sources) == ({"task_id": "old"}, {"field:task_id": "key"})
    answered = tracker.answer_prompt(PERSON, _prompt_in(tracker, gadgets, ISSUE_13),
                                     at=T0 + timedelta(minutes=41), fields={"task_id": "new"})
    assert answered.outcome == REMEMBERED
    assert _key_memory(ISSUE_13) == "new"
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=50), trigger="manual")
    assert _key_memory(ISSUE_13) == "new"


UID = "981ec036134f4a8580927ddca71658a9"
SLUG_KEY = "git.example.com/acme/widgets/fix-thing"
UID_KEY = f"git.example.com/acme/widgets/{UID}"
SLUG_RUN = RunFacts(ref=SessionRef(run=SLUG_KEY), key=SLUG_KEY, title="Fix thing")
UID_RUN = RunFacts(ref=SessionRef(run=UID_KEY, uid=UID), key=UID_KEY, title="Fix thing")


def _move_to_uid_key():
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"runs": {UID_KEY: {
        "host": "git.example.com", "project": "acme/widgets", "slug": "fix-thing", "uid": UID,
        "aliases": [SLUG_KEY]}}})


def test_a_carried_value_never_overwrites_a_newer_answer_under_a_run_alias(tracker, widgets):
    gadgets = _project("Gadgets", time_handler="tracker-p", time_clients=["acme"])
    assert _say(tracker, gadgets, T0, SLUG_RUN).outcome == PROMPTED
    _manual(tracker, widgets, T0 + timedelta(minutes=1), fields={"task_id": "old"}, runs=(SLUG_RUN,))
    assert _say(tracker, widgets, T0 + timedelta(minutes=20), _run("other")).outcome == SWITCHED
    _move_to_uid_key()
    back = _say(tracker, widgets, T0 + timedelta(minutes=40), UID_RUN)
    assert (back.timer.work_key, back.timer.fields, back.timer.sources) == (
        UID_KEY, {"task_id": "old"}, {"field:task_id": "key"})
    answered = tracker.answer_prompt(PERSON, _prompt_in(tracker, gadgets, SLUG_KEY),
                                     at=T0 + timedelta(minutes=41), fields={"task_id": "new"})
    assert answered.outcome == REMEMBERED
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=50), trigger="manual")
    again = _say(tracker, widgets, T0 + timedelta(hours=2), UID_RUN)
    assert again.timer.fields == {"task_id": "new"}


def test_a_value_carried_from_a_run_alias_stays_fresh_while_in_use(tracker, widgets):
    with timesheet_store.database() as db:
        for key, value, minute in ((UID_KEY, "stale", 20), (SLUG_KEY, "mine", 21)):
            db.execute("INSERT INTO key_values (person, handler, work_key, field, value, last_used)"
                       " VALUES (?,?,?,?,?,?)", (PERSON, "tracker-p", key, "task_id", value,
                                                 timetrack._stamp(T0 + timedelta(minutes=minute))))
    _move_to_uid_key()
    carried = _say(tracker, widgets, T0 + timedelta(minutes=30), UID_RUN)
    assert (carried.timer.fields, carried.timer.sources) == ({"task_id": "mine"}, {"field:task_id": "key"})
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=50), trigger="manual")
    sweep = T0 + timedelta(days=30, minutes=30)
    tracker.close_idle(sweep)
    again = _say(tracker, widgets, sweep + timedelta(minutes=1), UID_RUN)
    assert (again.outcome, again.timer.fields) == (STARTED, {"task_id": "mine"})


def test_a_project_value_is_not_remembered_for_a_run_key(tracker, webshop):
    first, second = _run("first"), _run("second")
    _manual(tracker, webshop, T0, fields={"task_id": "p"})
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="manual")
    assert _say(tracker, webshop, T0 + timedelta(hours=1), first).timer.fields == {"task_id": "p"}
    tracker.stop(PERSON, None, at=T0 + timedelta(hours=1, minutes=10), trigger="manual")
    _manual(tracker, webshop, T0 + timedelta(hours=2), fields={"task_id": "q"}, runs=(second,))
    tracker.stop(PERSON, None, at=T0 + timedelta(hours=2, minutes=10), trigger="manual")
    again = _say(tracker, webshop, T0 + timedelta(hours=3), first)
    assert again.timer.fields == {"task_id": "q"}


def test_a_run_key_value_is_found_after_the_run_moves_to_its_uid_key(tracker, widgets):
    uid = "981ec036134f4a8580927ddca71658a9"
    slug_key = "git.example.com/acme/widgets/fix-thing"
    uid_key = f"git.example.com/acme/widgets/{uid}"
    before = RunFacts(ref=SessionRef(run=slug_key), key=slug_key, title="Fix thing")
    tracker.start(PERSON, widgets, at=T0, trigger="activity", fields={"task_id": "abc"},
                  runs=(before,))
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="manual")
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"runs": {uid_key: {
        "host": "git.example.com", "project": "acme/widgets", "slug": "fix-thing", "uid": uid,
        "aliases": [slug_key]}}})
    after = RunFacts(ref=SessionRef(run=uid_key, uid=uid), key=uid_key, title="Fix thing")
    started = tracker.note_activity(PERSON, widgets, at=T0 + timedelta(hours=1), runs=(after,))
    assert started.outcome == STARTED, started
    assert started.timer.fields == {"task_id": "abc"}


def test_prompts_are_per_work_key_and_keep_their_id(tracker, widgets):
    fix13, fix14 = _run("fix-13", ISSUE_13), _run("fix-14", ISSUE_14)
    prompted = tracker.note_activity(PERSON, widgets, at=T0, runs=(fix13,))
    assert prompted.reasons == ("Widgets #13 needs a Task to track time",)
    [prompt] = tracker.status(PERSON)["prompts"]
    assert (prompt["work_key"], prompt["issues"], prompt["label"]) == (ISSUE_13, [ISSUE_13], "#13")
    assert prompt["run"] == fix13.ref.to_dict() and isinstance(prompt["id"], int)
    tracker.note_activity(PERSON, widgets, at=T0 + timedelta(minutes=1), runs=(fix14,))
    tracker.note_activity(PERSON, widgets, at=T0 + timedelta(minutes=2), runs=(fix13,))
    by_key = {item["work_key"]: item["id"] for item in tracker.status(PERSON)["prompts"]}
    assert set(by_key) == {ISSUE_13, ISSUE_14} and by_key[ISSUE_13] == prompt["id"]
    started = tracker.start(PERSON, widgets, at=T0 + timedelta(minutes=3), trigger="activity",
                            fields={"task_id": "t13"}, runs=(fix13,))
    assert started.outcome == STARTED
    assert [item["work_key"] for item in tracker.status(PERSON)["prompts"]] == [ISSUE_14]


def test_a_run_with_no_issue_is_prompted_by_its_title(tracker, widgets):
    prompted = tracker.note_activity(PERSON, widgets, at=T0, runs=(_run("chore", title="Chore run"),))
    assert prompted.reasons == ("Widgets Chore run needs a Task to track time",)
    [prompt] = tracker.status(PERSON)["prompts"]
    assert (prompt["label"], prompt["issues"]) == ("Chore run", [])


def test_a_manual_start_clears_the_project_prompt_and_its_own_key_only(tracker, platform, mirror):
    (mirror / "timesheet.d" / ".handlers" / "tracker-q.yaml").write_text(
        "name: tracker-q\nfields:\n  - key: task_id\n    label: Task\n    required: true\n"
        "    per_issue: true\n  - key: code\n    label: Code\n    required: true\n",
        encoding="utf-8",
    )
    gadgets = _project("Gadgets", time_handler="tracker-q")
    tracker.note_activity(PERSON, gadgets, at=T0)
    tracker.note_activity(PERSON, gadgets, at=T0 + timedelta(minutes=1), runs=(_run("fix-12", ISSUE_12),))
    tracker.note_activity(PERSON, gadgets, at=T0 + timedelta(minutes=2), runs=(_run("fix-13", ISSUE_13),))
    keys = sorted(item["work_key"] for item in tracker.status(PERSON)["prompts"])
    assert keys == ["", ISSUE_12, ISSUE_13]
    _manual(tracker, gadgets, T0 + timedelta(minutes=3), fields={"task_id": "abc", "code": "c1"},
            runs=(_run("fix-12", ISSUE_12),))
    assert [item["work_key"] for item in tracker.status(PERSON)["prompts"]] == [ISSUE_13]


def test_a_run_less_start_under_per_issue_runs_unassigned(tracker, widgets):
    started = tracker.note_activity(PERSON, widgets, at=T0)
    assert started.outcome == STARTED
    assert (started.timer.fields, started.timer.unassigned) == ({}, ("task_id",))
    status = tracker.status(PERSON)
    assert status["running"]["unassigned"] == ["task_id"]
    assert [w["field"] for w in status["warnings"] if w["kind"] == "missing_field"] == ["task_id"]
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="manual")
    [entry] = _entries()
    assert entry.fields == {}


def test_entries_carry_issues_and_kept_slivers_their_identity(tracker, docs):
    run = _run("fix-12", ISSUE_12, MR_7)
    tracker.note_activity(PERSON, docs, at=T0, runs=(run,))
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="manual")
    [entry] = _entries()
    assert entry.issues == [ISSUE_12, MR_7]
    tracker.note_activity(PERSON, docs, at=T0 + timedelta(hours=1), runs=(run,))
    tracker.stop(PERSON, None, at=T0 + timedelta(hours=1, seconds=5), trigger="manual")
    [kept] = tracker.kept_entries(PERSON)
    assert (kept["identity"], kept["issues"]) == (run.ref.to_dict(), [ISSUE_12, MR_7])


def test_values_are_remembered_for_the_work_key_only(tracker, widgets):
    run = _run("fix-12", ISSUE_12, ISSUE_13)
    tracker.start(PERSON, widgets, at=T0, trigger="activity", fields={"task_id": "t12"}, runs=(run,))
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="manual")
    with timesheet_store.database() as db:
        rows = [tuple(row) for row in db.execute(
            "SELECT handler, work_key, field, value FROM key_values")]
    assert rows == [("tracker-p", ISSUE_12, "task_id", "t12")]


def test_a_timer_row_from_before_665_still_reads_and_finishes(tracker, docs):
    with timesheet_store.database() as db:
        db.execute(
            "INSERT INTO timers (person, workspace_id, project_key, start, last_activity, mode,"
            " start_trigger, metadata) VALUES (?,?,?,?,?,?,?,?)",
            (PERSON, docs, "docs-site", "2026-10-01T09:00:00.000000+00:00",
             "2026-10-01T09:00:00.000000+00:00", MANUAL, "manual",
             json.dumps({"handler": "timesheet", "client": "", "title": "Old", "fields": {},
                         "run_name": "old-run", "sessions": []})),
        )
    running = tracker.status(PERSON)["running"]
    assert (running["identity"], running["issues"], running["title_source"]) == (None, [], "")
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="manual")
    [entry] = _entries()
    assert (entry.title, entry.issues) == ("Old", [])


# --- the same-project run table, late, D23, auto-off (issue #665, D22) -------

R, S = _run("fix-12", ISSUE_12, title="Fix 12"), _run("fix-13", ISSUE_13, title="Fix 13")


@pytest.mark.parametrize("identity, run, late, young, fits, expected", [
    (R.ref, R, False, False, True, REFRESHED),
    (R.ref, S, False, True, True, TAKEN_OVER),
    (R.ref, S, False, True, False, SWITCHED),
    (R.ref, S, False, False, True, SWITCHED),
    (None, R, False, False, True, ADOPTED),
    (None, R, False, True, False, SWITCHED),
    (R.ref, None, False, False, True, REFRESHED),
    (None, None, False, False, True, REFRESHED),
    (R.ref, S, True, True, True, REFRESHED),
    (None, R, True, False, True, REFRESHED),
])
def test_decide_run(identity, run, late, young, fits, expected):
    timer = replace(_timer("a", AUTO), identity=identity)
    assert decide_run(timer, run, late=late, young=young, values_fit=fits) == expected


def _say(tracker, workspace, at, *runs):
    return tracker.note_activity(PERSON, workspace, at=at, runs=runs,
                                 sessions=tuple(run.ref for run in runs))


def test_a_run_switch_in_the_project_splits(tracker, docs):
    _say(tracker, docs, T0, R)
    switched = _say(tracker, docs, T0 + timedelta(minutes=10), S)
    assert switched.outcome == SWITCHED
    [entry] = _entries()
    assert (entry.trigger.stop, entry.issues, entry.duration) == ("switch", [ISSUE_12], 600)
    timer = switched.timer
    assert (timer.start_trigger, timer.identity, timer.sessions) == ("activity", S.ref, (S.ref,))


def test_a_young_timer_takes_the_new_run_over_in_place(tracker, docs):
    _say(tracker, docs, T0, R)
    with timesheet_store.database() as db:
        row = db.execute("SELECT metadata FROM timers").fetchone()
        meta = {**json.loads(row["metadata"]), "title": "Mine", "title_source": "person"}
        db.execute("UPDATE timers SET metadata=?", (json.dumps(meta),))
    taken = _say(tracker, docs, T0 + timedelta(seconds=30), S)
    assert taken.outcome == TAKEN_OVER
    assert (taken.timer.identity, taken.timer.issues, taken.timer.title) == (S.ref, (ISSUE_13,), "Mine")
    assert taken.timer.start == T0 and _entries() == []
    assert taken.timer.sessions == (R.ref, S.ref)


def test_a_young_take_over_takes_the_run_title(tracker, docs):
    _say(tracker, docs, T0, R)
    taken = _say(tracker, docs, T0 + timedelta(seconds=30), S)
    assert (taken.outcome, taken.timer.title, taken.timer.title_source) == (TAKEN_OVER, "Fix 13", "run")


def test_a_young_take_over_without_a_value_switches_and_prompts(tracker, widgets, mirror):
    _issue_map(mirror, f"{ISSUE_12}:\n  task_id: t12\n")
    _say(tracker, widgets, T0, R)
    switched = _say(tracker, widgets, T0 + timedelta(seconds=30), S)
    assert switched.outcome == PROMPTED and switched.missing == ("task_id",)
    assert tracker.status(PERSON)["running"] is None
    [kept] = tracker.kept_entries(PERSON)
    assert kept["trigger"]["stop"] == "switch"


def test_a_run_less_timer_adopts_the_first_run(tracker, widgets, mirror):
    _issue_map(mirror, f"{ISSUE_12}:\n  task_id: t12\n")
    started = _say(tracker, widgets, T0)
    assert started.timer.unassigned == ("task_id",)
    adopted = _say(tracker, widgets, T0 + timedelta(minutes=3), R)
    assert adopted.outcome == ADOPTED
    timer = adopted.timer
    assert (timer.identity, timer.issues, timer.title, timer.title_source) == (R.ref, (ISSUE_12,), "Fix 12", "run")
    assert (timer.fields, timer.unassigned, timer.start) == ({"task_id": "t12"}, (), T0)
    assert _entries() == []


def test_adopting_a_run_that_would_change_a_value_splits(tracker, webshop, mirror):
    _manual(tracker, webshop, T0 - timedelta(hours=2), fields={"task_id": "old"})
    tracker.stop(PERSON, None, at=T0 - timedelta(hours=1), trigger="manual")
    _issue_map(mirror, "")
    tracker.start(PERSON, webshop, at=T0 - timedelta(minutes=30), trigger="activity",
                  fields={"task_id": "new"}, runs=(R,))
    tracker.stop(PERSON, None, at=T0 - timedelta(minutes=20), trigger="manual")
    started = _say(tracker, webshop, T0)
    assert started.timer.fields == {"task_id": "new"}
    tracker.update_running(PERSON, fields={"task_id": "other"})
    switched = _say(tracker, webshop, T0 + timedelta(minutes=5), R)
    assert switched.outcome == SWITCHED
    assert switched.timer.fields == {"task_id": "new"} and switched.timer.identity == R.ref


def test_adopting_a_run_with_no_value_splits_and_prompts(tracker, widgets):
    _say(tracker, widgets, T0)
    prompted = _say(tracker, widgets, T0 + timedelta(minutes=3), S)
    assert prompted.outcome == PROMPTED
    [entry] = _entries()
    assert (entry.trigger.stop, entry.fields) == ("switch", {})
    assert [item["work_key"] for item in tracker.status(PERSON)["prompts"]] == [ISSUE_13]


def test_run_less_activity_extends_the_running_entry(tracker, docs):
    _say(tracker, docs, T0, R)
    refreshed = _say(tracker, docs, T0 + timedelta(minutes=5))
    assert refreshed.outcome == REFRESHED
    assert (refreshed.timer.identity, refreshed.timer.title) == (R.ref, "Fix 12")
    ops = _project("Ops")
    switched = _say(tracker, ops, T0 + timedelta(minutes=6))
    assert switched.outcome == SWITCHED and switched.timer.identity is None


def test_late_activity_from_another_run_never_splits(tracker, docs):
    _say(tracker, docs, T0, R)
    _say(tracker, docs, T0 + timedelta(minutes=10), R)
    late = _say(tracker, docs, T0 + timedelta(minutes=5), S)
    assert late.outcome == REFRESHED
    assert late.timer.identity == R.ref and late.timer.sessions == (R.ref, S.ref)
    assert late.timer.last_activity == T0 + timedelta(minutes=10)
    assert _entries() == []


def test_a_key_transition_is_the_same_run(tracker, docs):
    uid = "981ec036134f4a8580927ddca71658a9"
    slug_key = "git.example.com/acme/widgets/fix-thing"
    uid_key = f"git.example.com/acme/widgets/{uid}"
    before = RunFacts(ref=SessionRef(run=slug_key), key=slug_key, title="Fix thing")
    _say(tracker, docs, T0, before)
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"runs": {uid_key: {
        "host": "git.example.com", "project": "acme/widgets", "slug": "fix-thing", "uid": uid,
        "aliases": [slug_key]}}})
    after = RunFacts(ref=SessionRef(run=uid_key, uid=uid), key=uid_key, title="Fix thing")
    for at in (T0 + timedelta(seconds=30), T0 + timedelta(minutes=10)):
        result = _say(tracker, docs, at, after)
        assert result.outcome == REFRESHED and result.timer.identity == before.ref
    assert _entries() == []


def test_several_runs_including_the_running_one_refresh(tracker, docs):
    _say(tracker, docs, T0, R)
    result = _say(tracker, docs, T0 + timedelta(minutes=10), S, R)
    assert result.outcome == REFRESHED and result.timer.identity == R.ref
    assert _entries() == []


def test_automatic_tracking_off_or_paused_freezes_the_timer_but_follows_the_title(
        tracker, docs, monkeypatch):
    _say(tracker, docs, T0, R)
    tracker.set_automatic(PERSON, False)
    noted = _say(tracker, docs, T0 + timedelta(minutes=10), S)
    assert noted.outcome == NOTED and noted.timer.identity == R.ref
    retitled = _say(tracker, docs, T0 + timedelta(minutes=11), _run("fix-12", ISSUE_12, title="Renamed"))
    assert retitled.outcome == NOTED
    assert (retitled.timer.title, retitled.timer.last_activity) == ("Renamed", T0)
    assert _entries() == []


def test_the_walkthrough_steps_one_to_four(tracker, platform, mirror, clock):
    (mirror / "timesheet.d" / ".handlers" / "clickup.yaml").write_text(
        "name: clickup\nfields:\n  - key: task_id\n    label: ClickUp task\n    required: true\n"
        "    per_issue: true\n", encoding="utf-8")
    path = mirror / "timesheet.d" / ".handlers" / "clickup" / "issues.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(f"{ISSUE_12}:\n  task_id: 86c1x2y3z\n", encoding="utf-8")
    widgets = _project("widgets", time_handler="clickup", time_idle_grace_minutes=60)
    r12 = _run("r12", ISSUE_12, title="Fix checkout rounding")
    r13 = _run("r13", ISSUE_13)
    at = lambda hh, mm: T0.replace(hour=hh, minute=mm)  # noqa: E731
    one = _say(tracker, widgets, at(9, 0))
    assert (one.outcome, one.timer.identity, one.timer.unassigned) == (STARTED, None, ("task_id",))
    two = _say(tracker, widgets, at(9, 3), r12)
    assert two.outcome == ADOPTED
    assert (two.timer.title, two.timer.issues, two.timer.fields) == (
        "Fix checkout rounding", (ISSUE_12,), {"task_id": "86c1x2y3z"})
    three = _say(tracker, widgets, at(9, 40))
    assert three.outcome == REFRESHED and three.timer.identity == r12.ref
    four = _say(tracker, widgets, at(9, 55), r13)
    assert four.outcome == PROMPTED
    assert four.reasons == ("widgets #13 needs a ClickUp task to track time",)
    [entry] = _entries()
    assert (entry.start, entry.end, entry.trigger.stop) == (at(9, 0), at(9, 55), "switch")
    assert (entry.issues, entry.fields) == ([ISSUE_12], {"task_id": "86c1x2y3z"})


# --- D27: manual timers follow runs; answering a prompt (issue #665) --------

def _prompt_id(tracker, work_key):
    [prompt_id] = [item["id"] for item in tracker.status(PERSON)["prompts"]
                   if item["work_key"] == work_key]
    return prompt_id


def test_a_manual_timer_splits_on_a_run_switch_and_stays_manual(tracker, docs):
    ops = _project("Ops")
    _manual(tracker, docs, T0, runs=(R,))
    switched = _say(tracker, docs, T0 + timedelta(minutes=10), S)
    assert switched.outcome == SWITCHED
    assert (switched.timer.mode, switched.timer.start_trigger, switched.timer.identity) == (
        MANUAL, "activity", S.ref)
    [entry] = _entries()
    assert (entry.trigger.start, entry.trigger.stop, entry.duration) == ("manual", "switch", 600)
    assert _say(tracker, ops, T0 + timedelta(minutes=11), R).outcome == NOTED


def test_a_manual_timer_with_no_identity_adopts_the_first_run(tracker, docs):
    _manual(tracker, docs, T0)
    adopted = _say(tracker, docs, T0 + timedelta(minutes=10), R)
    assert (adopted.outcome, adopted.timer.mode, adopted.timer.identity) == (ADOPTED, MANUAL, R.ref)
    assert _entries() == []


def test_a_manual_split_to_a_run_with_no_value_runs_unassigned_and_prompts(tracker, widgets, mirror):
    _issue_map(mirror, f"{ISSUE_12}:\n  task_id: t12\n")
    _manual(tracker, widgets, T0, fields={"task_id": "t12"}, runs=(R,))
    switched = _say(tracker, widgets, T0 + timedelta(minutes=10), S)
    assert switched.outcome == SWITCHED and switched.timer.mode == MANUAL
    assert (switched.timer.fields, switched.timer.unassigned) == ({}, ("task_id",))
    prompt_id = _prompt_id(tracker, ISSUE_13)
    filled = tracker.answer_prompt(PERSON, prompt_id, at=T0 + timedelta(minutes=12),
                                   fields={"task_id": "t13"})
    assert filled.outcome == UPDATED
    assert (filled.timer.fields, filled.timer.unassigned, filled.timer.start) == (
        {"task_id": "t13"}, (), T0 + timedelta(minutes=10))
    assert len(_entries()) == 1 and tracker.status(PERSON)["prompts"] == []


def test_run_less_activity_leaves_a_manual_timer_and_auto_off_still_follows(tracker, docs):
    _manual(tracker, docs, T0, runs=(R,))
    assert _say(tracker, docs, T0 + timedelta(minutes=5)).timer.identity == R.ref
    tracker.set_automatic(PERSON, False)
    switched = _say(tracker, docs, T0 + timedelta(minutes=10), S)
    assert switched.outcome == SWITCHED and switched.timer.identity == S.ref


def test_a_manual_timer_keeps_the_late_and_young_rules(tracker, docs, webshop, mirror):
    _manual(tracker, docs, T0, runs=(R,))
    _say(tracker, docs, T0 + timedelta(minutes=10), R)
    late = _say(tracker, docs, T0 + timedelta(minutes=5), S)
    assert late.timer.identity == R.ref and _entries() == []
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=20), trigger="manual")
    _manual(tracker, docs, T0 + timedelta(minutes=30), runs=(R,))
    taken = _say(tracker, docs, T0 + timedelta(minutes=30, seconds=20), S)
    assert (taken.outcome, taken.timer.mode, taken.timer.identity) == (TAKEN_OVER, MANUAL, S.ref)
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=40), trigger="manual")
    _issue_map(mirror, "")
    tracker.start(PERSON, webshop, at=T0 + timedelta(minutes=50), trigger="activity",
                  fields={"task_id": "keyed"}, runs=(R,))
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=55), trigger="manual")
    _manual(tracker, webshop, T0 + timedelta(hours=1), fields={"task_id": "other"})
    switched = _say(tracker, webshop, T0 + timedelta(hours=1, minutes=5), R)
    assert (switched.outcome, switched.timer.mode, switched.timer.fields) == (
        SWITCHED, MANUAL, {"task_id": "keyed"})


def test_answering_with_nothing_running_starts_an_automatic_timer(tracker, widgets):
    _say(tracker, widgets, T0, S)
    answered = tracker.answer_prompt(PERSON, _prompt_id(tracker, ISSUE_13),
                                     at=T0 + timedelta(minutes=3), fields={"task_id": "t13"})
    timer = answered.timer
    assert answered.outcome == STARTED
    assert (timer.mode, timer.start_trigger, timer.identity, timer.issues) == (
        AUTO, "manual", S.ref, (ISSUE_13,))
    assert (timer.title, timer.fields) == ("Fix 13", {"task_id": "t13"})
    assert tracker.status(PERSON)["prompts"] == []
    with timesheet_store.database() as db:
        assert db.execute("SELECT value FROM key_values WHERE work_key=?", (ISSUE_13,)).fetchone()[0] == "t13"
    assert tracker.close_idle(T0 + timedelta(hours=1))[0].entry.trigger.stop == "idle"


def test_answering_while_automatic_tracking_is_off_or_paused_starts_a_manual_timer(
        tracker, widgets, pause_hours):
    _say(tracker, widgets, T0, S)
    _say(tracker, widgets, T0 + timedelta(minutes=1), _run("fix-14", ISSUE_14))
    tracker.set_automatic(PERSON, False)
    answered = tracker.answer_prompt(PERSON, _prompt_id(tracker, ISSUE_13),
                                     at=T0 + timedelta(minutes=3), fields={"task_id": "t13"})
    assert (answered.outcome, answered.timer.mode) == (STARTED, MANUAL)
    tracker.set_automatic(PERSON, True)
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="manual")
    assert tracker.status(PERSON)["pause"] is not None
    paused = tracker.answer_prompt(PERSON, _prompt_id(tracker, ISSUE_14),
                                   at=T0 + timedelta(minutes=12), fields={"task_id": "t14"})
    assert (paused.outcome, paused.timer.mode) == (STARTED, MANUAL)
    assert tracker.status(PERSON)["pause"] is None


def test_a_bad_answer_or_someone_elses_prompt_is_refused(tracker, widgets):
    _say(tracker, widgets, T0, S)
    prompt_id = _prompt_id(tracker, ISSUE_13)
    bad = tracker.answer_prompt(PERSON, prompt_id, at=T0, fields={"task_id": "NOT VALID"})
    assert bad.outcome == REFUSED and bad.missing == ("task_id",)
    assert tracker.status(PERSON)["prompts"]
    other = tracker.answer_prompt("sam", prompt_id, at=T0, fields={"task_id": "t13"})
    assert other.outcome == REFUSED


def test_answering_never_ends_a_manual_timer(tracker, widgets, docs, mirror):
    _say(tracker, widgets, T0, S)
    prompt_id = _prompt_id(tracker, ISSUE_13)
    _manual(tracker, docs, T0 + timedelta(minutes=1))
    kept = tracker.answer_prompt(PERSON, prompt_id, at=T0 + timedelta(minutes=2),
                                 fields={"task_id": "t13"})
    assert kept.outcome == REMEMBERED and kept.timer.workspace_id == docs
    assert _entries() == [] and tracker.status(PERSON)["prompts"] == []
    tracker.stop(PERSON, None, at=T0 + timedelta(minutes=10), trigger="manual")
    resumed = _say(tracker, widgets, T0 + timedelta(minutes=11), S)
    assert resumed.outcome == STARTED and resumed.timer.fields == {"task_id": "t13"}


def test_answering_inside_a_manual_run_sequence_keeps_the_running_manual_timer(
        tracker, widgets, mirror, pause_hours):
    _issue_map(mirror, f"{ISSUE_12}:\n  task_id: t12\n")
    _manual(tracker, widgets, T0, fields={"task_id": "t12"}, runs=(R,))
    _say(tracker, widgets, T0 + timedelta(minutes=10), S)
    prompt_id = _prompt_id(tracker, ISSUE_13)
    _say(tracker, widgets, T0 + timedelta(minutes=20), R)
    tracker.set_automatic(PERSON, False)
    kept = tracker.answer_prompt(PERSON, prompt_id, at=T0 + timedelta(minutes=25),
                                 fields={"task_id": "t13"})
    assert kept.outcome == REMEMBERED
    assert (kept.timer.identity, kept.timer.start) == (R.ref, T0 + timedelta(minutes=20))
    assert len(_entries()) == 2


def test_answering_ends_an_automatic_timer_under_another_key(tracker, widgets, mirror):
    _issue_map(mirror, f"{ISSUE_12}:\n  task_id: t12\n")
    _say(tracker, widgets, T0 - timedelta(minutes=30), S)
    prompt_id = _prompt_id(tracker, ISSUE_13)
    _say(tracker, widgets, T0, R)
    answered = tracker.answer_prompt(PERSON, prompt_id, at=T0 + timedelta(minutes=5),
                                     fields={"task_id": "t13"})
    assert answered.outcome == SWITCHED
    assert (answered.timer.identity, answered.timer.title) == (S.ref, "Fix 13")
    assert answered.entry.trigger.stop == "switch" and answered.entry.issues == [ISSUE_12]


def test_a_manual_timer_saves_what_it_has_seen_so_a_late_record_never_splits(tracker, docs):
    _manual(tracker, docs, T0, runs=(R,))
    _say(tracker, docs, T0 + timedelta(minutes=5), R)
    _say(tracker, docs, T0 + timedelta(hours=1), R)
    late = _say(tracker, docs, T0 + timedelta(minutes=30), S)
    assert (late.timer.identity, late.timer.start) == (R.ref, T0) and _entries() == []
    tracker.note_activity(PERSON, docs, at=T0 + timedelta(hours=2))
    late = _say(tracker, docs, T0 + timedelta(hours=1, minutes=30), S)
    assert late.timer.identity == R.ref and _entries() == []


def test_adopting_or_taking_over_a_run_clears_its_prompt(tracker, widgets, mirror):
    _say(tracker, widgets, T0, S)
    assert [item["work_key"] for item in tracker.status(PERSON)["prompts"]] == [ISSUE_13]
    _issue_map(mirror, f"{ISSUE_13}:\n  task_id: t13\n")
    tracker.note_activity(PERSON, widgets, at=T0 + timedelta(minutes=1))
    adopted = _say(tracker, widgets, T0 + timedelta(minutes=2), S)
    assert (adopted.outcome, adopted.timer.fields) == (ADOPTED, {"task_id": "t13"})
    assert tracker.status(PERSON)["prompts"] == []


def test_a_prompt_under_the_run_s_old_key_is_cleared_by_its_new_key(tracker, widgets):
    uid = "981ec036134f4a8580927ddca71658a9"
    slug_key = "git.example.com/acme/widgets/fix-thing"
    uid_key = f"git.example.com/acme/widgets/{uid}"
    before = RunFacts(ref=SessionRef(run=slug_key), key=slug_key, title="Fix thing")
    assert tracker.note_activity(PERSON, widgets, at=T0, runs=(before,)).outcome == PROMPTED
    store.write_json(store.snapshot_path(runs.RUNS_FILE), {"runs": {uid_key: {
        "host": "git.example.com", "project": "acme/widgets", "slug": "fix-thing", "uid": uid,
        "aliases": [slug_key]}}})
    after = RunFacts(ref=SessionRef(run=uid_key, uid=uid), key=uid_key, title="Fix thing")
    started = tracker.start(PERSON, widgets, at=T0 + timedelta(minutes=1), trigger="activity",
                            fields={"task_id": "abc"}, runs=(after,))
    assert started.outcome == STARTED
    assert tracker.status(PERSON)["prompts"] == []


def test_a_stale_prompt_for_the_running_key_never_ends_its_timer(tracker, widgets, mirror):
    _issue_map(mirror, f"{ISSUE_12}:\n  task_id: t12\n")
    _say(tracker, widgets, T0, R)
    with timesheet_store.database() as db:
        tracker._prompt(db, PERSON, tracker._project(widgets), R,
                        [{"key": "task_id", "label": "Task", "per_issue": True}], T0)
    stale = tracker.answer_prompt(PERSON, _prompt_id(tracker, ISSUE_12), at=T0 + timedelta(minutes=5),
                                  fields={"task_id": "other"})
    assert stale.outcome == REFUSED and "edit it instead" in stale.reasons[0]
    assert (stale.timer.start, stale.timer.fields) == (T0, {"task_id": "t12"})
    assert _entries() == [] and tracker.status(PERSON)["prompts"] == []
    with timesheet_store.database() as db:
        assert db.execute("SELECT * FROM key_values WHERE value='other'").fetchall() == []


def test_a_run_s_prompt_is_cleared_when_the_run_starts_under_an_issue(tracker, widgets, mirror):
    bare = _run("fix-x")
    assert _say(tracker, widgets, T0, bare).outcome == PROMPTED
    assert _prompt_id(tracker, bare.key)
    _issue_map(mirror, f"{ISSUE_13}:\n  task_id: t13\n")
    started = _say(tracker, widgets, T0 + timedelta(minutes=1), _run("fix-x", ISSUE_13))
    assert (started.outcome, started.timer.work_key) == (STARTED, ISSUE_13)
    assert tracker.status(PERSON)["prompts"] == []


def test_an_answer_to_a_run_s_prompt_never_splits_that_run_keyed_to_an_issue(tracker, widgets, mirror):
    bare, issued = _run("fix-x"), _run("fix-x", ISSUE_13)
    _issue_map(mirror, f"{ISSUE_13}:\n  task_id: t13\n")
    _say(tracker, widgets, T0, issued)
    with timesheet_store.database() as db:
        tracker._prompt(db, PERSON, tracker._project(widgets), bare,
                        [{"key": "task_id", "label": "Task", "per_issue": True}], T0)
    stale = tracker.answer_prompt(PERSON, _prompt_id(tracker, bare.key), at=T0 + timedelta(minutes=5),
                                  fields={"task_id": "x1"})
    assert stale.outcome == REFUSED
    assert (stale.timer.start, stale.timer.issues) == (T0, (ISSUE_13,))
    assert tracker.status(PERSON)["running"]["issues"] == [ISSUE_13]
    assert _entries() == [] and tracker.status(PERSON)["prompts"] == []


def _issue_moved_under_a_manual_timer(tracker, widgets, mirror):
    """Run R prompted under #12, a manual timer follows R once it moves to #13:
    the prompts for #12 and #13 are both open (round 3 of !456)."""
    _issue_map(mirror, "")
    assert _say(tracker, widgets, T0, _run("fix-r", ISSUE_12)).outcome == PROMPTED
    _manual(tracker, widgets, T0 + timedelta(minutes=1), fields={"task_id": "t14"},
            runs=(_run("fix-14", ISSUE_14),))
    _issue_map(mirror, f"{ISSUE_12}:\n  task_id: t12\n")
    moved = _say(tracker, widgets, T0 + timedelta(minutes=10), _run("fix-r", ISSUE_13))
    assert (moved.outcome, moved.timer.mode, moved.timer.unassigned) == (SWITCHED, MANUAL, ("task_id",))
    return _prompt_id(tracker, ISSUE_12), _prompt_id(tracker, ISSUE_13)


def test_a_prompt_under_another_issue_of_the_run_never_fills_its_timer(tracker, widgets, mirror):
    p12, p13 = _issue_moved_under_a_manual_timer(tracker, widgets, mirror)
    kept = tracker.answer_prompt(PERSON, p12, at=T0 + timedelta(minutes=12), fields={})
    assert kept.outcome == REMEMBERED
    assert (kept.timer.fields.get("task_id"), kept.timer.unassigned) == (None, ("task_id",))
    assert [item["id"] for item in tracker.status(PERSON)["prompts"]] == [p13]


def test_filling_a_timer_clears_its_prompt_and_leaves_the_run_s_other_issues(tracker, widgets, mirror):
    p12, p13 = _issue_moved_under_a_manual_timer(tracker, widgets, mirror)
    filled = tracker.answer_prompt(PERSON, p13, at=T0 + timedelta(minutes=12), fields={"task_id": "t13"})
    assert (filled.outcome, filled.timer.fields, filled.timer.unassigned) == (UPDATED, {"task_id": "t13"}, ())
    assert [item["id"] for item in tracker.status(PERSON)["prompts"]] == [p12]
    kept = tracker.answer_prompt(PERSON, p12, at=T0 + timedelta(minutes=13), fields={"task_id": "x12"})
    assert (kept.outcome, kept.timer.fields) == (REMEMBERED, {"task_id": "t13"})
    with timesheet_store.database() as db:
        assert db.execute("SELECT value FROM key_values WHERE work_key=?",
                          (ISSUE_12,)).fetchone()["value"] == "x12"


def test_a_run_key_answer_that_fills_the_timer_clears_its_issue_s_prompt(tracker, widgets, mirror):
    _issue_map(mirror, "")
    bare = _run("fix-r")
    assert _say(tracker, widgets, T0, bare).outcome == PROMPTED
    _manual(tracker, widgets, T0 + timedelta(minutes=1), fields={"task_id": "t14"},
            runs=(_run("fix-14", ISSUE_14),))
    moved = _say(tracker, widgets, T0 + timedelta(minutes=10), _run("fix-r", ISSUE_13))
    assert moved.timer.unassigned == ("task_id",)
    assert _prompt_id(tracker, ISSUE_13)
    filled = tracker.answer_prompt(PERSON, _prompt_id(tracker, bare.key), at=T0 + timedelta(minutes=12),
                                   fields={"task_id": "t13"})
    assert (filled.outcome, filled.timer.unassigned) == (UPDATED, ())
    assert tracker.status(PERSON)["prompts"] == []


def test_an_answer_that_starts_a_run_clears_the_run_s_other_prompts(tracker, widgets, mirror):
    _issue_map(mirror, "")
    assert _say(tracker, widgets, T0, _run("fix-r", ISSUE_12)).outcome == PROMPTED
    assert _say(tracker, widgets, T0 + timedelta(minutes=1), _run("fix-r", ISSUE_13)).outcome == PROMPTED
    p12, p13 = _prompt_id(tracker, ISSUE_12), _prompt_id(tracker, ISSUE_13)
    started = tracker.answer_prompt(PERSON, p13, at=T0 + timedelta(minutes=2), fields={"task_id": "t13"})
    assert (started.outcome, started.timer.mode, started.timer.work_key) == (STARTED, AUTO, ISSUE_13)
    assert tracker.status(PERSON)["prompts"] == []
    gone = tracker.answer_prompt(PERSON, p12, at=T0 + timedelta(minutes=3), fields={"task_id": "t12"})
    assert gone.outcome == REFUSED and tracker.status(PERSON)["running"]["issues"] == [ISSUE_13]


def test_answering_fills_an_unassigned_timer_from_the_resolved_values(tracker, widgets, mirror):
    _issue_map(mirror, f"{ISSUE_12}:\n  task_id: t12\n")
    _manual(tracker, widgets, T0, fields={"task_id": "t12"}, runs=(R,))
    _say(tracker, widgets, T0 + timedelta(minutes=10), S)
    prompt_id = _prompt_id(tracker, ISSUE_13)
    _issue_map(mirror, f"{ISSUE_12}:\n  task_id: t12\n{ISSUE_13}:\n  task_id: t13\n")
    filled = tracker.answer_prompt(PERSON, prompt_id, at=T0 + timedelta(minutes=12), fields={})
    assert filled.outcome == UPDATED
    assert (filled.timer.fields, filled.timer.unassigned) == ({"task_id": "t13"}, ())


def _gadgets(mirror, per_issue_required=True):
    (mirror / "timesheet.d" / ".handlers" / "tracker-q.yaml").write_text(
        "name: tracker-q\nfields:\n  - key: task_id\n    label: Task\n"
        f"    required: {'true' if per_issue_required else 'false'}\n"
        "    per_issue: true\n  - key: code\n    label: Code\n    required: true\n",
        encoding="utf-8",
    )
    return _project("Gadgets", time_handler="tracker-q")


def test_an_answer_that_starts_a_manual_timer_needs_every_required_field(tracker, platform, mirror):
    gadgets = _gadgets(mirror)
    assert tracker.note_activity(PERSON, gadgets, at=T0).missing == ("code",)
    tracker.set_automatic(PERSON, False)
    prompt_id = _prompt_id(tracker, "")
    refused = tracker.answer_prompt(PERSON, prompt_id, at=T0 + timedelta(minutes=1), fields={"code": "c1"})
    assert refused.outcome == REFUSED and refused.missing == ("task_id",)
    assert refused.reasons and tracker.status(PERSON)["prompts"]
    started = tracker.answer_prompt(PERSON, prompt_id, at=T0 + timedelta(minutes=2),
                                    fields={"code": "c1", "task_id": "t1"})
    assert (started.outcome, started.timer.mode, started.timer.unassigned) == (STARTED, MANUAL, ())


def test_a_prompt_the_idle_close_satisfies_is_no_longer_open(tracker, platform, mirror):
    gadgets = _gadgets(mirror)
    path = mirror / "timesheet.d" / ".handlers" / "tracker-q" / "issues.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{ISSUE_13}:\n  task_id: t13\n  code: c0\n", encoding="utf-8")
    assert tracker.note_activity(PERSON, gadgets, at=T0).missing == ("code",)
    prompt_id = _prompt_id(tracker, "")
    run = _run("fix-13", ISSUE_13)
    assert _say(tracker, gadgets, T0 + timedelta(minutes=1), run).outcome == STARTED
    _say(tracker, gadgets, T0 + timedelta(minutes=10), run)
    tracker.set_automatic(PERSON, False)
    refused = tracker.answer_prompt(PERSON, prompt_id, at=T0 + timedelta(minutes=40),
                                    fields={"code": "c1"})
    assert refused.outcome == REFUSED and refused.reasons == (f"prompt {prompt_id} is no longer open",)
    assert refused.entry is not None and refused.entry.trigger.stop == "idle"
    assert tracker.status(PERSON)["prompts"] == []


def test_an_answer_that_starts_an_automatic_run_less_timer_may_run_unassigned(tracker, platform, mirror):
    gadgets = _gadgets(mirror)
    tracker.note_activity(PERSON, gadgets, at=T0)
    started = tracker.answer_prompt(PERSON, _prompt_id(tracker, ""), at=T0 + timedelta(minutes=1),
                                    fields={"code": "c1"})
    assert (started.outcome, started.timer.mode, started.timer.unassigned) == (STARTED, AUTO, ("task_id",))


def test_an_optional_per_issue_field_is_never_unassigned(tracker, platform, mirror):
    gadgets = _gadgets(mirror, per_issue_required=False)
    started = tracker.start(PERSON, gadgets, at=T0, trigger="activity", fields={"code": "c1"})
    assert (started.outcome, started.timer.unassigned) == (STARTED, ())
    assert [w for w in tracker.status(PERSON)["warnings"] if w["kind"] == "missing_field"] == []


def test_answering_a_prompt_of_an_archived_project_is_refused(tracker, widgets):
    _say(tracker, widgets, T0, S)
    prompt_id = _prompt_id(tracker, ISSUE_13)
    with projects.database() as db:
        db.execute("UPDATE projects SET archived=1 WHERE id=?", (widgets,))
    refused = tracker.answer_prompt(PERSON, prompt_id, at=T0 + timedelta(minutes=1),
                                    fields={"task_id": "t13"})
    assert refused.outcome == REFUSED and "archived" in refused.reasons[0]
    assert tracker.status(PERSON)["running"] is None


def test_a_take_over_keeps_a_field_the_person_cleared(tracker, platform, mirror, clock):
    (mirror / "timesheet.d" / ".handlers" / "tracker-n.yaml").write_text(
        "name: tracker-n\nfields:\n  - key: note\n    label: Note\n", encoding="utf-8")
    notes = _project("Notes", time_handler="tracker-n")
    tracker.start(PERSON, notes, at=T0 - timedelta(hours=1), trigger="manual", fields={"note": "n1"})
    tracker.stop(PERSON, None, at=T0 - timedelta(minutes=30), trigger="manual")
    started = _say(tracker, notes, T0, R)
    assert started.timer.fields == {"note": "n1"}
    clock.now = T0 + timedelta(seconds=10)
    tracker.update_running(PERSON, fields={"note": ""})
    taken = _say(tracker, notes, T0 + timedelta(seconds=20), S)
    assert (taken.outcome, taken.timer.identity, taken.timer.fields) == (TAKEN_OVER, S.ref, {})


# --- D24: an edit after time has accrued splits (issue #665) ----------------

def test_a_retitle_at_the_minimum_splits_and_below_it_edits_in_place(tracker, docs, clock):
    _manual(tracker, docs, T0, title="First")
    clock.now = T0 + timedelta(seconds=59)
    inplace = tracker.update_running(PERSON, title="Second")
    assert (inplace.outcome, inplace.timer.title, inplace.timer.start) == (UPDATED, "Second", T0)
    clock.now = T0 + timedelta(seconds=60)
    split = tracker.update_running(PERSON, title="Third")
    assert split.outcome == SPLIT
    assert (split.entry.title, split.entry.duration, split.entry.trigger.stop) == ("Second", 60, "edit")
    assert (split.timer.title, split.timer.start, split.timer.start_trigger) == ("Third", clock.now, "edit")
    assert split.timer.mode == MANUAL


def test_filling_an_empty_value_or_repeating_one_never_splits(tracker, docs, clock):
    _manual(tracker, docs, T0)
    clock.now = T0 + timedelta(hours=1)
    assert tracker.update_running(PERSON, title="Filled").outcome == UPDATED
    assert tracker.update_running(PERSON, title="Filled").outcome == UPDATED
    assert _entries() == []


def test_a_value_equal_to_its_base_is_not_an_edit(platform, clock, docs):
    tracker = Tracker(platform, now=clock, run_facts=lambda ref: _run("r", title="Renamed"))
    _say(tracker, docs, T0, _run("r", title="First"))
    _say(tracker, docs, T0 + timedelta(minutes=5), _run("r", title="Renamed"))
    clock.now = T0 + timedelta(minutes=6)
    result = tracker.update_running(PERSON, title="First", base={"title": "First"})
    assert (result.outcome, result.timer.title, result.timer.title_source) == (UPDATED, "Renamed", "run")
    assert _entries() == []


def test_an_automatic_timer_splits_at_its_last_activity(tracker, docs, clock):
    _say(tracker, docs, T0, R)
    _say(tracker, docs, T0 + timedelta(minutes=10), R)
    clock.now = T0 + timedelta(minutes=14)
    split = tracker.update_running(PERSON, title="New")
    last = T0 + timedelta(minutes=10)
    assert split.outcome == SPLIT
    assert (split.entry.end, split.entry.title, split.entry.issues) == (last, "Fix 12", [ISSUE_12])
    timer = split.timer
    assert (timer.start, timer.last_activity, timer.start_trigger, timer.mode) == (last, last, "edit", AUTO)
    assert (timer.identity, timer.issues, timer.sessions) == (R.ref, (ISSUE_12,), (R.ref,))
    grown = _say(tracker, docs, T0 + timedelta(minutes=15), R)
    assert grown.timer.last_activity == T0 + timedelta(minutes=15) and grown.timer.start == last


def test_client_and_field_changes_split_and_clearing_splits(tracker, webshop, clock):
    _manual(tracker, webshop, T0, client="acme", fields={"task_id": "abc"})
    clock.now = T0 + timedelta(minutes=5)
    assert tracker.update_running(PERSON, client="globex").outcome == SPLIT
    clock.now = T0 + timedelta(minutes=10)
    assert tracker.update_running(PERSON, fields={"task_id": "def"}).outcome == SPLIT
    clock.now = T0 + timedelta(minutes=15)
    assert tracker.update_running(PERSON, title="Has a title").outcome == UPDATED
    clock.now = T0 + timedelta(minutes=20)
    assert tracker.update_running(PERSON, title="").outcome == SPLIT
    entries = _entries()
    assert [entry.trigger.stop for entry in entries] == ["edit", "edit", "edit"]
    assert entries[0].client == "acme" and entries[1].fields == {"task_id": "abc"}


def test_an_idle_timer_is_closed_before_an_edit(tracker, docs, clock, monkeypatch):
    _say(tracker, docs, T0, R)
    _say(tracker, docs, T0 + timedelta(minutes=10), R)
    clock.now = T0 + timedelta(hours=2)
    woken = []
    monkeypatch.setattr(timesheet_store, "notify_writer", lambda: woken.append(True))
    refused = tracker.update_running(PERSON, title="Late")
    assert refused.outcome == REFUSED and refused.entry.trigger.stop == "idle"
    assert refused.entry.end == T0 + timedelta(minutes=10) and refused.seq is not None and woken


def test_a_person_set_title_survives_a_take_over(tracker, docs, clock):
    _say(tracker, docs, T0, R)
    clock.now = T0 + timedelta(seconds=10)
    tracker.update_running(PERSON, title="Mine")
    taken = _say(tracker, docs, T0 + timedelta(seconds=20), S)
    assert (taken.outcome, taken.timer.title, taken.timer.identity) == (TAKEN_OVER, "Mine", S.ref)


def test_a_manual_start_on_its_running_project_splits_on_a_change(tracker, docs, clock):
    _manual(tracker, docs, T0, title="One")
    clock.now = T0 + timedelta(minutes=5)
    split = _manual(tracker, docs, clock.now, title="Two")
    assert split.outcome == SPLIT and split.entry.title == "One"
    clock.now = T0 + timedelta(minutes=10)
    _manual(tracker, docs, T0 + timedelta(minutes=10), client="acme")
    assert len(_entries()) == 1


def test_the_walkthrough_step_six(tracker, widgets, clock):
    r13 = _run("r13", ISSUE_13, title="Round the total")
    _say(tracker, widgets, T0, r13)
    prompt_id = _prompt_id(tracker, ISSUE_13)
    at = lambda hh, mm: T0.replace(hour=hh, minute=mm)  # noqa: E731
    tracker.answer_prompt(PERSON, prompt_id, at=at(9, 58), fields={"task_id": "t13"})
    _say(tracker, widgets, at(10, 10), r13)
    _say(tracker, widgets, at(10, 24), r13)
    clock.now = at(10, 30)
    split = tracker.update_running(PERSON, title="Rounding, take two")
    assert split.outcome == SPLIT
    [entry] = _entries()
    assert (entry.start, entry.end, entry.title) == (at(9, 58), at(10, 24), "Round the total")
    assert (entry.trigger.start, entry.trigger.stop) == ("manual", "edit")
    assert (split.timer.start, split.timer.title) == (at(10, 24), "Rounding, take two")


def test_stop_running_closes_timers_whose_tracking_ends(tracker, docs, clock):
    """Switching tracking off, or removing a person, ends their timer (#668): a
    manual one at that moment, an automatic one at its last activity."""
    _manual(tracker, docs, T0)
    assert tracker.note_activity("blair", docs, at=T0 + timedelta(minutes=5)).outcome == STARTED
    assert tracker.note_activity("blair", docs, at=T0 + timedelta(minutes=12)).outcome == REFRESHED
    off = T0 + timedelta(hours=1)

    [mine] = tracker.stop_running([PERSON], at=off)
    assert (mine.outcome, mine.entry.person, mine.entry.end, mine.entry.trigger.stop) == (
        STOPPED, PERSON, off, "manual")
    assert [row["person"] for row in _timer_rows()] == ["blair"], "only the named person's timer ends"

    [theirs] = tracker.stop_running(at=off)
    assert (theirs.entry.person, theirs.entry.end, theirs.entry.trigger.stop) == (
        "blair", T0 + timedelta(minutes=12), "idle")
    assert _timer_rows() == []
    assert sorted(entry.person for entry in _entries()) == [PERSON, "blair"]
    assert tracker.stop_running(at=off) == []
