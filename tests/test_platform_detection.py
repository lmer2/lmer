"""The daemon's detection tick feeds the assistant's spool (issue #141, T69; §8.3).

Spec §8.3 splits one job in two: the daemon detects, mechanically, and the
assistant is *notified* rather than subscribed. The seam
(:func:`lmer_platform.assistant.notify`) has existed since T29 with no caller in
``src/`` at all, so ``POST /api/assistant/pending`` honestly answered with an
empty list and the assistant was never told anything. This is that wiring.

The diff is what is tested hardest, because it is what keeps a notification
channel from being a firehose. Notifying every tick for a condition that has not
changed would spool a digest a minute for one unanswered question, which is
precisely the context-window flooding §8.3 exists to avoid. So:

- the same condition on consecutive ticks is one notification;
- a condition that clears and comes back is a second one — it is a second event;
- two runs in the same condition are two entries, each naming its own run;
- a *new* question on a run already waiting is a new event, which is why a
  condition's identity includes when it started;
- and a re-rendered note for an unchanged condition is not an event at all.

Four guards ride alongside it, each an acceptance criterion rather than a nicety:
detection never depends on an assistant being alive (the UI's badge must not
depend on an LLM), it never forces a work-repo pull (that would defeat the
``work_repo_pull_interval`` throttle), a failure is absorbed and its repeats are
demoted rather than logged at full volume forever, and only ``lmer platform run``
grows the thread.

Nothing here waits on wall-clock time: the interval and the sleep are constructor
parameters and :meth:`lmer_platform.detect.Detector.tick_once` is driven by hand,
because this suite runs in a one-CPU container where a timing assertion is a
flake. The thread itself gets one smoke test and no assertions about how often it
ran.
"""

import json
import logging
import os
import sys
import threading
from types import SimpleNamespace

import pytest

from ask_channel import protocol as ask_protocol
from lmer_platform import (
    ask,
    assistant,
    checkin,
    daemon,
    detect,
    inventory,
    registry,
    spawn,
    store,
)
from lmer_platform import config as cfg
from lmer_platform.workrepo import resolve_run_dir
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", cfg.ENV_BIND_ADDRESS, cfg.ENV_BIND_PORT):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def config(platform_root):
    return cfg.load()


@pytest.fixture
def detected(caplog):
    """Capture this module's own log at DEBUG.

    The demoted repeats are a ``DEBUG`` line by design, so the dedupe cannot be
    asserted at the default level — and asserting it is the only way to tell
    "quiet" from "not running".
    """
    caplog.set_level(logging.DEBUG, logger="lmer_platform.detect")
    return caplog


class Spool:
    """Stands in for :func:`lmer_platform.assistant.notify`.

    Returns ``False`` — "no assistant is live" — because that is the case the
    seam's contract is about: a digest spools either way, and a caller that
    treated the answer as a delivery receipt would drop everything said while the
    operator had the chat closed.
    """

    def __init__(self, error=None) -> None:
        self.calls = []
        self.error = error

    def __call__(self, note, *, kind="event", data=None):
        if self.error is not None:
            raise self.error
        self.calls.append({"note": note, "kind": kind, "data": data})
        return False

    @property
    def notes(self):
        return [call["note"] for call in self.calls]

    @property
    def kinds(self):
        return [call["kind"] for call in self.calls]


def row(
    slug,
    *,
    reason=None,
    note=None,
    since=None,
    state="dormant",
    updated=None,
    label=None,
    host="gitlab.example.com",
    project="agents/global",
):
    """One fleet row, shaped exactly as :meth:`inventory.RunView.to_dict` builds it.

    Hand-built rather than driven through ``build_inventory`` because what is
    under test is the diff, not the derivation — ``tests/test_platform_inventory.py``
    owns the mapping from run state to attention reason, and a test that went
    through both would fail for either reason.
    """
    attention = None
    if reason is not None:
        attention = {"reason": reason, "note": note, "url": None, "since": since,
                     "priority": 1}
    return {
        "host": host, "project": project, "slug": slug,
        "label": label or slug, "state": state, "updated": updated,
        "attention": attention,
    }


def fleet(*rows):
    """A ``build_state`` payload carrying *rows*."""
    return {
        "runs": list(rows),
        "attention": [r for r in rows if r.get("attention")],
        "counts": {},
        "totals": {"runs": len(rows), "live": 0, "attention": 0},
    }


def reading(*payloads):
    """A state reader that answers *payloads* in order, then repeats the last.

    Raises whatever it is handed instead of a payload, which is how a tick that
    fails mid-sequence is expressed without a second seam.
    """
    remaining = list(payloads)

    def read(config, **kwargs):
        current = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(current, BaseException):
            raise current
        return current

    return read


def detector(config, *payloads, spool=None, **kwargs):
    """A detector reading *payloads* and notifying into *spool*."""
    return detect.Detector(
        config,
        state_reader=reading(*payloads),
        notifier=spool if spool is not None else Spool(),
        **kwargs,
    )


def loud_failures(caplog):
    """The ``ERROR``-level "this is a new cause" lines, not the demoted repeats.

    The trailing space is what separates ``platform_detection_failed`` from
    ``platform_detection_failed_again``, which shares its prefix.
    """
    return [
        record for record in caplog.records
        if f"{record.message} ".startswith("platform_detection_failed ")
        and record.levelno >= logging.ERROR
    ]


QUESTION = "which approach do you want?"


# --- the diff, which is the load-bearing part --------------------------------

def test_the_first_tick_is_a_baseline_and_notifies_nothing(config, detected):
    """A daemon restart must not re-announce a fleet's standing state.

    §8.3's events are transitions — a question *opened*, a task *finished* — and
    the spool is bounded, so replaying every condition that was already true would
    evict the digests that describe what actually changed. The standing list is
    what ``GET /api/state`` answers, and the ``orchestrate`` taskdef sends a
    starting assistant to read it.
    """
    spool = Spool()
    tick = detector(config, fleet(row("develop-141", reason="question",
                                      note=QUESTION, state="waiting_on_you")),
                    spool=spool)

    assert tick.tick_once() == []
    assert spool.calls == [], "the baseline tick must tell the assistant nothing"
    assert any("platform_detection_baseline" in r.message for r in detected.records), (
        "a silent tick has to say why it was silent"
    )


def test_a_standing_condition_is_notified_once(config):
    """The firehose this exists to avoid: one question, one digest, not one a tick."""
    waiting = fleet(row("develop-141", reason="question", note=QUESTION,
                        state="waiting_on_you", since="2026-07-28T10:00:00Z"))
    spool = Spool()
    tick = detector(config, fleet(), waiting, spool=spool)

    tick.tick_once()                      # baseline: nothing is waiting
    first = tick.tick_once()              # the question opens
    second = tick.tick_once()             # and is still open
    third = tick.tick_once()

    assert len(first) == 1
    assert second == [] and third == []
    assert len(spool.calls) == 1, "an unanswered question must not re-notify"


def test_a_condition_that_clears_and_returns_is_a_second_event(config):
    """Asked, answered, asked again — the second ask is not a duplicate.

    Suppressing it would hide the run that most needs a human: the one that has
    now been round twice.
    """
    waiting = fleet(row("develop-141", reason="question", note=QUESTION,
                        state="waiting_on_you", since="2026-07-28T10:00:00Z"))
    answered = fleet(row("develop-141", state="running"))
    spool = Spool()
    tick = detector(config, fleet(), waiting, answered, waiting, spool=spool)

    tick.tick_once()
    tick.tick_once()
    tick.tick_once()
    again = tick.tick_once()

    assert len(again) == 1
    assert len(spool.calls) == 2


def test_two_runs_in_the_same_condition_are_two_entries_naming_each_run(config):
    """One digest per run, and each has to say which run it is about.

    The assistant's next move is an API call, so the identity in the digest is the
    ``<host>/<project>/<slug>`` the run routes take — not the label, which two runs
    can share.
    """
    spool = Spool()
    tick = detector(
        config,
        fleet(),
        fleet(
            row("develop-141", reason="question", note=QUESTION,
                state="waiting_on_you", since="2026-07-28T10:00:00Z", label="shared"),
            row("review-141", reason="question", note="ship it?",
                state="waiting_on_you", since="2026-07-28T10:01:00Z", label="shared"),
        ),
        spool=spool,
    )

    tick.tick_once()
    fresh = tick.tick_once()

    assert len(fresh) == 2
    assert sorted(call["data"]["slug"] for call in spool.calls) == [
        "develop-141", "review-141"
    ]
    assert all("gitlab.example.com/agents/global/" in note for note in spool.notes)
    assert len(set(spool.notes)) == 2, "two events must not read as one"


def test_a_second_question_on_a_waiting_run_is_a_new_event(config):
    """Same run, same reason, different question — the reason alone is not identity.

    ``since`` comes off the attention record (the question's own timestamp for a
    live ask), so keying on ``(run, reason)`` would swallow every question after
    the first for as long as the run stayed on the attention list.
    """
    spool = Spool()
    tick = detector(
        config,
        fleet(),
        fleet(row("develop-141", reason="live_question", note=QUESTION,
                  state="running", since="2026-07-28T10:00:00Z")),
        fleet(row("develop-141", reason="live_question", note="and this one?",
                  state="running", since="2026-07-28T10:05:00Z")),
        spool=spool,
    )

    tick.tick_once()
    tick.tick_once()
    later = tick.tick_once()

    assert len(later) == 1
    assert "and this one?" in spool.notes[1]


def test_a_re_rendered_note_for_an_unchanged_condition_is_not_an_event(config):
    """The note is a rendering, not the condition.

    "…(+2 more)" moves on its own as sibling questions come and go, and the
    current text is always one fleet read away.
    """
    spool = Spool()
    tick = detector(
        config,
        fleet(),
        fleet(row("develop-141", reason="live_question", note=f"{QUESTION} (+1 more)",
                  state="running", since="2026-07-28T10:00:00Z")),
        fleet(row("develop-141", reason="live_question", note=f"{QUESTION} (+2 more)",
                  state="running", since="2026-07-28T10:00:00Z")),
        spool=spool,
    )

    tick.tick_once()
    tick.tick_once()

    assert tick.tick_once() == []
    assert len(spool.calls) == 1


# --- what counts as material -------------------------------------------------

def test_a_finished_run_is_material_and_routine_states_are_not(config):
    """§8.3 lists "a task finished", and a finished run needs nobody.

    So it cannot come off the attention axis — :func:`inventory._derive` gives a
    complete run no attention record at all — and is read off the state axis
    instead, using that axis's own word. A run merely running, dormant or parked
    is the routine traffic that must stay out of a context window.
    """
    spool = Spool()
    tick = detector(
        config,
        fleet(row("develop-141", state="running")),
        fleet(row("develop-141", state="parked")),
        fleet(row("develop-141", state="complete", updated="2026-07-28T11:00:00Z")),
        spool=spool,
    )

    tick.tick_once()
    assert tick.tick_once() == [], "parking a run is not something to wake anyone for"
    finished = tick.tick_once()

    assert [signal.kind for signal in finished] == ["complete"]
    assert spool.kinds == ["complete"]
    assert "is now complete" in spool.notes[0]


def test_every_attention_reason_is_material_without_being_listed_here(config):
    """The policy is inventory's attention axis, not a copy of it.

    A reason exists on that axis precisely because a person has to act, so each
    one is material by construction — including ``cap_reached`` and
    ``slot_contention``, which nothing raises yet. Detection picks up a reason
    added there later without being touched, which is the whole point of not
    inventing a vocabulary here.
    """
    from lmer_platform.inventory import ATTENTION_REASONS

    spool = Spool()
    tick = detector(
        config,
        fleet(),
        fleet(*[
            row(f"run-{reason}", reason=reason, note=f"{reason} happened",
                since="2026-07-28T10:00:00Z")
            for reason in ATTENTION_REASONS
        ]),
        spool=spool,
    )

    tick.tick_once()
    tick.tick_once()

    assert spool.kinds == list(ATTENTION_REASONS)


def test_the_digest_carries_what_the_assistant_needs_to_act(config):
    spool = Spool()
    tick = detector(
        config,
        fleet(),
        fleet(row("develop-141", reason="question", note=QUESTION, label="issue 141",
                  state="waiting_on_you", since="2026-07-28T10:00:00Z")),
        spool=spool,
    )

    tick.tick_once()
    tick.tick_once()
    call = spool.calls[0]

    assert call["note"] == (
        "gitlab.example.com/agents/global/develop-141 needs you — question: "
        f"{QUESTION}"
    )
    assert call["kind"] == "question"
    assert call["data"] == {
        "host": "gitlab.example.com",
        "project": "agents/global",
        "slug": "develop-141",
        "label": "issue 141",
        "kind": "question",
        "state": "waiting_on_you",
        "since": "2026-07-28T10:00:00Z",
    }


def test_a_row_with_no_run_identity_yet_is_skipped(config):
    """A session spawned seconds ago has no ``<host>/<project>/<slug>``.

    :func:`inventory._view_from_session` fills those with an em dash, and a digest
    naming a run the assistant cannot address is a wake-up it can do nothing with.
    It will have an identity by its first ``work commit``.
    """
    spool = Spool()
    tick = detector(
        config,
        fleet(),
        fleet({"host": "—", "project": "—", "slug": "s-1", "label": "s-1",
               "state": "crashed", "updated": None,
               "attention": {"reason": "crashed", "note": "gone", "since": None}}),
        spool=spool,
    )

    tick.tick_once()
    assert tick.tick_once() == []
    assert spool.calls == []


# --- (1) it must work with no assistant running -------------------------------

def test_a_digest_spools_when_no_assistant_is_running(config, platform_root):
    """The seam's own contract, honored rather than second-guessed.

    :func:`assistant.notify` spools and answers whether one was live; it is not a
    delivery receipt. Nothing here reads the assistant, starts one, or waits for
    one — the attention list is computed the same way whether an LLM is alive or
    not, which is what keeps the UI's badge off an assistant's critical path.
    """
    tick = detect.Detector(
        config,
        state_reader=reading(
            fleet(),
            fleet(row("develop-141", reason="question", note=QUESTION,
                      state="waiting_on_you", since="2026-07-28T10:00:00Z")),
        ),
    )

    tick.tick_once()
    assert len(tick.tick_once()) == 1
    assert assistant.status().running is False, "detection must not start one"
    assert registry.list_sessions(live_only=True) == []

    pending = assistant.take_pending()
    assert [note["kind"] for note in pending] == ["question"]
    assert QUESTION in pending[0]["note"]
    assert tick.notified == 1


def test_a_long_question_is_truncated_rather_than_refused(config, platform_root):
    """``notify`` refuses a note over ``MAX_NOTE_CHARS``, and questions are prose.

    Truncating gives a shortened digest; not truncating loses the notification
    entirely for exactly the runs whose questions were long enough to need care.
    """
    tick = detect.Detector(
        config,
        state_reader=reading(
            fleet(),
            fleet(row("develop-141", reason="question", note="x" * 5000,
                      state="waiting_on_you", since="2026-07-28T10:00:00Z")),
        ),
    )

    tick.tick_once()
    tick.tick_once()

    pending = assistant.take_pending()
    assert len(pending) == 1, "a long question must still reach the spool"
    assert len(pending[0]["note"]) <= assistant.MAX_NOTE_CHARS
    assert pending[0]["note"].endswith("…")


# --- (3) it must not turn a pull into a per-tick cost -------------------------

def test_the_tick_reads_the_fleet_view_without_forcing_a_pull(config, monkeypatch):
    """``force_pull`` is the ``rescan`` path, and forcing here would defeat the
    ``work_repo_pull_interval`` throttle — a ``git fetch`` every tick for the life
    of the daemon. Reading through the same ``build_state`` the UI polls also means
    the badge and the digest can never disagree about what needs a human."""
    calls = []

    def spy(config, **kwargs):
        calls.append(kwargs)
        return fleet()

    monkeypatch.setattr(detect, "build_state", spy)
    detect.Detector(config, notifier=Spool()).tick_once()

    assert calls == [{}], "the throttled path takes no arguments; forcing takes one"


# --- (4) a detection failure must never take the daemon down ------------------

def test_a_failing_scan_is_absorbed_and_the_detector_keeps_ticking(config, detected):
    """The fleet view is what an operator needs when something is broken, and it
    does not depend on this thread — so a mirror that cannot be read costs a tick,
    never the daemon."""
    tick = detector(config, RuntimeError("mirror is a smoking hole"))

    assert tick.tick_once() == []
    assert tick.tick_once() == [], "and it comes back for the next one"
    assert tick.failures == 2
    assert any(
        "platform_detection_failed" in r.message and r.levelno >= logging.ERROR
        for r in detected.records
    )


def test_a_failed_tick_leaves_the_baseline_intact(config):
    """Otherwise a flaky mirror is a firehose arriving through the error path.

    A tick that dropped its baseline would make the next successful one see every
    standing condition as new, and one that reset it to "nothing observed" would
    re-notify the lot.
    """
    waiting = row("develop-141", reason="question", note=QUESTION,
                  state="waiting_on_you", since="2026-07-28T10:00:00Z")
    second = row("review-141", reason="yield", note="phase boundary",
                 state="yielded", since="2026-07-28T10:30:00Z")
    spool = Spool()
    tick = detector(
        config,
        fleet(waiting),
        OSError("mirror unreadable"),
        fleet(waiting, second),
        spool=spool,
    )

    tick.tick_once()                      # baseline: develop-141 already waiting
    tick.tick_once()                      # the read fails
    fresh = tick.tick_once()              # and recovers, with one new condition

    assert [signal.slug for signal in fresh] == ["review-141"]
    assert len(spool.calls) == 1, "only what is new since the last good tick"


def test_a_persistent_failure_stops_logging_at_full_volume(config, detected):
    """A cause that fails every tick is an ``ERROR`` a minute otherwise, which is
    how a log becomes unreadable at the moment it matters. The first occurrence is
    loud, identical repeats are demoted, and the recovery is logged so the silence
    is bounded at both ends."""
    boom = RuntimeError("same cause every time")
    tick = detector(config, boom, boom, boom, fleet())

    tick.tick_once()
    tick.tick_once()
    tick.tick_once()

    loud = loud_failures(detected)
    quiet = [r for r in detected.records
             if "platform_detection_failed_again" in r.message]
    assert len(loud) == 1, "one line per cause, not one per tick"
    assert len(quiet) == 2

    tick.tick_once()
    assert any("platform_detection_recovered" in r.message for r in detected.records)


def test_a_persistent_notify_failure_is_also_logged_only_once(config, detected):
    """The dedupe is per stage, and this is why: the scan succeeds every tick here,
    so a shared "last failure" that any success cleared would put the unwritable
    spool back at full volume on every single tick."""
    spool = Spool(error=RuntimeError("spool is unwritable"))
    tick = detector(
        config,
        fleet(),
        fleet(row("a", reason="question", note="q", since="1")),
        fleet(row("a", reason="question", note="q", since="1"),
              row("b", reason="question", note="q", since="2")),
        fleet(row("a", reason="question", note="q", since="1"),
              row("b", reason="question", note="q", since="2"),
              row("c", reason="question", note="q", since="3")),
        spool=spool,
    )

    tick.tick_once()
    tick.tick_once()
    tick.tick_once()
    tick.tick_once()

    loud = loud_failures(detected)
    assert len(loud) == 1
    assert tick.failures == 3
    assert tick.notified == 0


def test_a_refused_digest_is_not_retried_on_the_next_tick(config):
    """``notify`` is best-effort by its own contract — "a failed state write costs
    one notification, not the detection that produced it". A caller that retried
    would re-deliver the whole standing list every tick for as long as the spool
    was unwritable."""
    waiting = fleet(row("develop-141", reason="question", note=QUESTION,
                        state="waiting_on_you", since="2026-07-28T10:00:00Z"))
    spool = Spool(error=RuntimeError("spool is unwritable"))
    tick = detector(config, fleet(), waiting, spool=spool)

    tick.tick_once()
    assert len(tick.tick_once()) == 1
    assert tick.failures == 1

    tick.tick_once()
    assert tick.failures == 1, "the condition is not new any more; it is not re-sent"


# --- reconciling endings nothing watched --------------------------------------
#
# The second job on the tick, and not detection: a session's registry entry is
# removed by the ``_watch`` thread that waited on its process, and that thread dies
# with the daemon (or with ``lmer platform spawn``, which exits at once). The entry
# it leaves behind is exactly how ``inventory._derive`` recognises a crash, so a
# session that finished perfectly well reads ``crashed`` forever.
#
# What every test here turns on is *what authorises the removal*: the run's own
# committed state saying it finished. Nothing else does, because a stale entry is
# the only evidence a real crash leaves.

#: A pid nothing can be running under, so an entry reads as dead. Same value the
#: rest of the platform tests use.
DEAD_PID = 2**22

HOST = "gitlab.example.com"
PROJECT = "agents/global"

#: A credential shape ``transcripts._scrub`` masks — see
#: ``tests/test_platform_transcripts.py``, which owns the vocabulary.
LEAKED_TOKEN = "glpat-AAAABBBBCCCCDDDDEEEEFFFF"


def stale_entry(slug="develop-141", *, session_id="s-unwatched", pid=DEAD_PID):
    """Register a session entry the way a daemon that then died would leave it."""
    registry.register(
        session_id,
        pid=pid,
        run={"host": HOST, "project": PROJECT, "slug": slug},
        task={"taskdef": "develop", "target": "issue-141"},
    )
    return session_id


def plant_committed_run(config, slug="develop-141", *, status="complete"):
    """Write one run dir into the mirror the way a pushed run appears there.

    Bytes rather than ``run_state.write_state``, as in ``test_platform_resume.py``:
    the mirror is a *read* surface for the platform, and planting the file is the
    only way a test can be sure nothing in the sweep wrote to it.
    """
    path = config.mirror_path / HOST / PROJECT / "runs" / slug
    path.mkdir(parents=True, exist_ok=True)
    (path / "state.yaml").write_text(
        "\n".join([
            "schema: 1",
            f"status: {json.dumps(status)}",
            f"slug: {json.dumps(slug)}",
            'taskdef: "develop"',
            'target: "issue-141"',
        ]) + "\n",
        encoding="utf-8",
    )
    return path


def fleet_rows(config, slug="develop-141"):
    """What the fleet view shows, from the state actually on disk.

    Through :func:`inventory.build_inventory` rather than by asserting on the
    registry, because "reads as crashed" is the thing that was wrong and the entry
    is only the input to it. ``activity={}`` keeps the rows off the network.
    """
    ref = resolve_run_dir(config, HOST, PROJECT, slug)
    inv = inventory.build_inventory(
        [ref] if ref is not None else [],
        registry.list_sessions(live_only=False),
        titles={}, activity={},
    )
    return inv.runs


def fleet_reading(config, slug="develop-141"):
    """The one row for *slug*, when there is exactly one."""
    (run,) = fleet_rows(config, slug)
    return run


def sweep_events():
    return [
        event for event in store.read_events()
        if event.get("type") == detect.SESSION_ENDED_UNWATCHED
    ]


@pytest.mark.parametrize("status", ["complete", "archived"])
def test_a_stale_entry_whose_run_finished_is_cleared(config, platform_root, status):
    """The removal itself, plus the cleanup ``_watch`` never got to do.

    A finished run's row already reads ``complete`` while the entry sits there —
    ``_derive`` prefers the run's committed status — so what this asserts is
    everything else the missing reaper left: a dead session hanging off a finished
    run, a control token for a container that is gone, and the ports file.

    Both terminal statuses, because the sweep takes that vocabulary from
    ``inventory`` rather than keeping a list of its own: one that lacked ``archived``
    would leave archived runs uncleared forever, which is this bug wearing a new hat.
    """
    session_id = stale_entry()
    plant_committed_run(config, status=status)
    ports = spawn.ports_file_for(session_id)
    ports.parent.mkdir(parents=True, exist_ok=True)
    ports.write_text("{}", encoding="utf-8")
    spawn.token_file_for(session_id).write_text("session-control-token", "utf-8")

    assert fleet_reading(config).session is not None

    assert detect.sweep_finished_sessions(config) == [session_id]

    assert registry.read_session(session_id) is None
    row = fleet_reading(config)
    assert row.state == "complete" and row.attention is None
    assert row.session is None, "a finished run still carries a dead session"
    assert not spawn.token_file_for(session_id).exists(), (
        "a credential for a container that is gone was left on disk"
    )
    assert not ports.exists(), (
        "the ports file has no readers once the session is gone"
    )


def test_a_predecessor_s_stale_entry_stops_being_a_crashed_row(config, platform_root):
    """The standing lie, and the one no committed status ever corrected.

    A resumed run (spec §5.4) has two sessions, and the finished one's entry loses
    the run key to its live replacement — which lands it in
    ``inventory._view_from_session``, where there is no run state to prefer and a
    dead entry reads ``crashed`` with a ``crashed`` record on the attention axis. So
    a session that ended normally shows as a crash, in the list of things a human has
    to deal with, for as long as the entry exists.
    """
    finished = stale_entry(session_id="s-finished")
    stale_entry(session_id="s-live", pid=os.getpid())
    plant_committed_run(config)

    crashed = [row for row in fleet_rows(config) if row.state == "crashed"]
    assert [row.session["id"] for row in crashed] == [finished]
    assert crashed[0].attention.reason == "crashed"

    assert detect.sweep_finished_sessions(config) == [finished]

    assert [row.state for row in fleet_rows(config)] == ["running"], (
        "the crashed row outlived the session that was never reaped"
    )
    assert registry.read_session("s-live") is not None, (
        "the live session's own entry must not be swept with its predecessor's"
    )


def test_a_stale_entry_whose_run_is_unfinished_keeps_reading_as_crashed(
    config, platform_root
):
    """The shape that really is a crash, and the entry is its only evidence.

    A dead pid with a run that never said it finished is a session that died
    mid-flight. Clearing that would erase the one record that anything went wrong,
    which is why the sweep asks the run and not the clock.
    """
    session_id = stale_entry()
    plant_committed_run(config, status="in-progress")

    assert detect.sweep_finished_sessions(config) == []

    assert registry.read_session(session_id) is not None
    row = fleet_reading(config)
    assert row.state == "crashed" and row.attention.reason == "crashed"
    assert sweep_events() == [], "a reconciliation that did not happen was recorded"


def test_a_run_that_has_not_reached_the_mirror_yet_is_left_alone(config, platform_root):
    """Run state lands git-eventually, and "not yet" is not "not finished".

    So the entry survives this tick and is swept on a later one, once the run's last
    commit has been pushed and pulled — eventually-honest rather than guessing from
    the absence of a file.
    """
    session_id = stale_entry()

    assert detect.sweep_finished_sessions(config) == []
    assert registry.read_session(session_id) is not None

    plant_committed_run(config)
    assert detect.sweep_finished_sessions(config) == [session_id]


def test_a_live_session_is_never_swept(config, platform_root):
    """A run may mark itself complete while its session is still winding down.

    Removing that entry would drop a live session out of the fleet view and out of
    the concurrency cap that counts it — and there is a reaper for its ending.
    """
    session_id = stale_entry(pid=os.getpid())
    plant_committed_run(config)

    assert detect.sweep_finished_sessions(config) == []
    assert registry.read_session(session_id) is not None


def test_a_session_with_no_run_identity_is_left_alone(config, platform_root):
    """Spawned, nothing committed, then died: there is no run state to consult.

    Nothing could authorise the removal, and the entry is all there is to say the
    session existed at all.
    """
    registry.register("s-nameless", pid=DEAD_PID)
    plant_committed_run(config)

    assert detect.sweep_finished_sessions(config) == []
    assert registry.read_session("s-nameless") is not None


def test_an_unreadable_run_state_is_not_a_finished_run(config, platform_root):
    """A corrupt ``state.yaml`` is the ``unreadable`` case, not consent to remove."""
    session_id = stale_entry()
    path = plant_committed_run(config)
    (path / "state.yaml").write_text("schema: 1\nstatus: [not, a, string\n", "utf-8")

    assert detect.sweep_finished_sessions(config) == []
    assert registry.read_session(session_id) is not None


def test_the_reconciled_ending_never_claims_a_clean_exit(config, platform_root):
    """The one fact no version of this can recover, said rather than invented.

    A sibling event name rather than ``session_exited`` with empty fields: that event
    means "a watcher saw this process exit with this code", and every consumer of it
    stays correct precisely because this is not one. A ``clean`` here would be a
    guess, and a ``clean: false`` would score every reconciled clean ending as a
    crash.
    """
    session_id = stale_entry()
    plant_committed_run(config)

    detect.sweep_finished_sessions(config)

    (event,) = sweep_events()
    assert event["note"] == session_id
    assert event["data"] == {
        "session": session_id,
        "run": {"host": HOST, "project": PROJECT, "slug": "develop-141"},
        "run_status": "complete",
        "exit_code": None,
    }
    assert "clean" not in event["data"], "a clean exit nobody observed"
    # The literal, not the constant: this is a name in an append-only log that
    # consumers match on, and what must hold is that it is not ``spawn``'s.
    assert detect.SESSION_ENDED_UNWATCHED == "session_ended_unwatched"
    assert [e["type"] for e in store.read_events()] == ["session_ended_unwatched"], (
        "the sweep's ending must not arrive as a session_exited"
    )


def test_the_sweep_scrubs_the_transcript_the_dead_daemon_never_did(
    config, platform_root
):
    """``_watch`` scrubs on its way out, and for these sessions it never ran.

    So the credential shapes a harness echoed into its transcript have been sitting
    in that file since the daemon that would have masked them died. Idempotent, which
    is what makes it safe to do from here: a second pass matches nothing.
    """
    from lmer_platform import transcripts

    session_id = stale_entry()
    plant_committed_run(config)
    directory = transcripts.session_transcript_dir(session_id) / "-workspace"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "session.jsonl"
    target.write_text(
        json.dumps({
            "type": "user",
            "message": {"role": "user", "content": f"use {LEAKED_TOKEN} to push"},
        }) + "\n",
        encoding="utf-8",
    )

    detect.sweep_finished_sessions(config)

    text = target.read_text(encoding="utf-8")
    assert LEAKED_TOKEN not in text
    assert "to push" in text, "the record itself must survive the masking"


def test_the_sweep_runs_before_the_fleet_read_on_the_same_tick(config, platform_root):
    """Order, not layout: a run that finished unwatched arrives at ``complete`` now.

    The fleet read below the sweep is what the diff sees, so an entry still present
    when it happens makes this tick's event ``crashed`` and the next tick's
    ``complete`` — two digests for one ending, the first of them wrong.
    """
    session_id = stale_entry()
    plant_committed_run(config)
    seen = []

    def read(config, **kwargs):
        seen.append([e["id"] for e in registry.list_sessions(live_only=False)])
        return fleet()

    tick = detect.Detector(config, state_reader=read, notifier=Spool())
    tick.tick_once()

    assert seen == [[]], "the fleet was read while the stale entry was still there"
    assert tick.reconciled == 1
    assert registry.read_session(session_id) is None


def test_a_failing_sweep_is_absorbed_and_the_diff_still_runs(config, detected):
    """Its own stage: the fleet view does not depend on the sweep, and neither does
    the diff. A mirror the sweep cannot read must cost neither."""
    spool = Spool()
    tick = detector(
        config,
        fleet(),
        fleet(row("develop-141", reason="question", note=QUESTION, since="1")),
        spool=spool,
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(detect, "sweep_finished_sessions", _explode)

        tick.tick_once()
        fresh = tick.tick_once()

    assert [signal.slug for signal in fresh] == ["develop-141"]
    assert len(spool.calls) == 1
    assert tick.failures == 2 and tick.reconciled == 0
    assert [record.message for record in loud_failures(detected)].__len__() == 1, (
        "one loud line per cause per stage, as everywhere else here"
    )
    assert any("stage=sweep" in record.message for record in detected.records)


def _explode(config):
    raise OSError("the mirror is a smoking hole")


# --- milestones a session signalled (T122) ------------------------------------
#
# The third job on the tick, and the only one where the platform is *told* rather
# than inferring: an agent runs ``lmer-signal "pushed MR !167"`` and the record is
# a file on its own ask channel. What every test here turns on is delivery exactly
# once — the mark is on disk, so a daemon restart is not a second announcement —
# and that a milestone reaches the orchestrator without appearing on the
# operator's attention axis.

def live_session(session_id="s-signaller", *, slug="develop-141", run=True):
    """Register a live session the way a spawn leaves it."""
    extra = {"run": {"host": HOST, "project": PROJECT, "slug": slug}} if run else {}
    registry.register(
        session_id,
        pid=os.getpid(),
        task={"taskdef": "develop", "target": "issue-141"},
        **extra,
    )
    return session_id


def signal_on(session_id, text):
    """File one signal on *session_id*'s channel, as ``lmer-signal`` would."""
    directory = ask.prepare_ask_dir(session_id)
    return ask_protocol.post_signal(directory, text)


def signal_events():
    return [
        event for event in store.read_events()
        if event.get("type") == detect.SESSION_SIGNALLED
    ]


def marks():
    """The high-water marks as they are on disk."""
    stored = store.read_json(store.snapshot_path(detect.SIGNAL_MARKS_FILE)) or {}
    sessions = stored.get("sessions") or {}
    return {name: record.get("seq") for name, record in sessions.items()}


def test_a_signal_is_delivered_on_the_very_first_tick(config, platform_root):
    """No silent baseline for a signal, unlike a standing condition.

    The first tick deliberately announces no *conditions* — they are all one
    ``GET /api/state`` away — but a signal is nowhere else at all: no fleet read
    derives it and no run dir carries it. A milestone filed while the daemon was
    restarting is therefore delivered when it comes back, which is the case the
    tool exists for.
    """
    session_id = live_session()
    signal_on(session_id, "pushed MR !167 for review")
    spool = Spool()
    tick = detector(config, fleet(), spool=spool)

    assert tick.tick_once() == [], "a milestone is not one of the diff's conditions"

    assert tick.signalled == 1
    (call,) = spool.calls
    assert call["kind"] == detect.SIGNAL_DIGEST_KIND
    assert call["note"] == (
        f"{HOST}/{PROJECT}/develop-141 signalled: pushed MR !167 for review"
    )
    assert call["data"]["session"] == session_id
    assert call["data"]["text"] == "pushed MR !167 for review"
    assert call["data"]["slug"] == "develop-141"


def test_the_signal_also_lands_in_the_platform_s_own_history(config, platform_root):
    """The copy that does not depend on a spool being writable.

    Nested ``run``, like every neighbour in ``events.jsonl``, and the session id on
    the note so a grep for one session finds its milestones.
    """
    session_id = live_session()
    signal = signal_on(session_id, "the review is finished")

    detector(config, fleet()).tick_once()

    (event,) = signal_events()
    assert event["note"] == session_id
    assert event["data"] == {
        "session": session_id,
        "signal": signal.id,
        "run": {"host": HOST, "project": PROJECT, "slug": "develop-141"},
        "text": "the review is finished",
        "at": signal.at,
    }
    # The literal, not the constant: this is a name in an append-only log that
    # consumers match on.
    assert detect.SESSION_SIGNALLED == "session_signalled"


def test_one_signal_is_ingested_once_however_many_ticks_run(config, platform_root):
    """The record is on the channel for the life of the session, so a tick that
    re-read it would spool the same milestone a minute."""
    live_session()
    signal_on("s-signaller", "pushed MR !167")
    spool = Spool()
    tick = detector(config, fleet(), spool=spool)

    tick.tick_once()
    tick.tick_once()
    tick.tick_once()

    assert len(spool.calls) == 1
    assert len(signal_events()) == 1
    assert tick.signalled == 1


def test_a_restarted_daemon_does_not_re_announce_what_it_already_took(
    config, platform_root
):
    """The mark is a file, not a field on the detector.

    In-memory dedupe would make every daemon restart a fresh announcement of every
    milestone still sitting on a live session's channel — the firehose §8.3 exists
    to prevent, arriving through the restart path.
    """
    live_session()
    signal_on("s-signaller", "pushed MR !167")
    detector(config, fleet()).tick_once()

    spool = Spool()
    detector(config, fleet(), spool=spool).tick_once()

    assert spool.calls == []
    assert marks() == {"s-signaller": 1}


def test_the_next_milestone_after_an_ingested_one_still_arrives(config, platform_root):
    """The mark is a high-water mark, not a "this session has signalled" flag."""
    live_session()
    signal_on("s-signaller", "pushed MR !167")
    spool = Spool()
    tick = detector(config, fleet(), spool=spool)
    tick.tick_once()

    signal_on("s-signaller", "and the review is finished")
    tick.tick_once()

    assert [call["data"]["text"] for call in spool.calls] == [
        "pushed MR !167", "and the review is finished",
    ]
    assert marks() == {"s-signaller": 2}


def test_a_milestone_is_not_a_reason_for_the_operator_to_act(config, platform_root):
    """Its own digest class, standing beside the attention axis rather than in it.

    Every member of ``ATTENTION_REASONS`` becomes a digest class automatically and
    every one of them puts a run on the operator's badge. A milestone needs the
    orchestrator, so it must not be one of them.
    """
    live_session()
    signal_on("s-signaller", "done with the current task")
    spool = Spool()
    detector(config, fleet(), spool=spool).tick_once()

    assert detect.SIGNAL_DIGEST_KIND not in inventory.ATTENTION_REASONS
    assert detect.SIGNAL_DIGEST_KIND not in detect.MATERIAL_STATES
    assert spool.kinds == [detect.SIGNAL_DIGEST_KIND]
    assert all(row.attention is None for row in fleet_rows(config)), (
        "signalling put the run on the operator's attention axis"
    )


def test_a_dead_session_s_channel_is_left_alone(config, platform_root):
    """Only live sessions are read: a milestone from a session nothing can act on
    any more would arrive as news the orchestrator cannot route."""
    session_id = stale_entry(session_id="s-dead")
    signal_on(session_id, "pushed MR !167")
    spool = Spool()

    detector(config, fleet(), spool=spool).tick_once()

    assert spool.calls == []
    assert signal_events() == []


def test_a_channel_no_tracked_session_owns_is_never_opened(config, platform_root):
    """The registry is what is iterated, so a leftover directory is not a source.

    A channel dir outlives its session (it is beside the PTY log), and ingesting
    from one would re-announce the milestones of a session that has been gone for
    days.
    """
    signal_on("s-forgotten", "pushed MR !167")
    spool = Spool()

    detector(config, fleet(), spool=spool).tick_once()

    assert spool.calls == []
    assert marks() == {}


def test_a_session_with_no_run_yet_signals_under_its_session_id(config, platform_root):
    """Nothing has been committed, so there is no run triple to lead with — and
    the milestone is still worth delivering: the session routes take an id."""
    live_session(session_id="s-fresh", run=False)
    signal_on("s-fresh", "pushed MR !167 before the first commit")
    spool = Spool()

    detector(config, fleet(), spool=spool).tick_once()

    (call,) = spool.calls
    assert call["note"] == (
        "session s-fresh signalled: pushed MR !167 before the first commit"
    )
    assert call["data"]["host"] is None
    assert signal_events()[0]["data"]["run"] is None


def test_a_credential_shape_is_masked_before_either_sink(config, platform_root):
    """Scrub before bound, at the boundary, once (T92/T93's rule).

    The text is session-authored prose crossing out of a container's mount into two
    files the daemon keeps: ``notify`` would have masked its own note and nothing
    would have masked what went into history.
    """
    live_session()
    signal_on("s-signaller", f"pushed with {LEAKED_TOKEN} in the remote")
    spool = Spool()

    detector(config, fleet(), spool=spool).tick_once()

    (call,) = spool.calls
    assert LEAKED_TOKEN not in call["note"]
    assert LEAKED_TOKEN not in json.dumps(call["data"])
    assert LEAKED_TOKEN not in json.dumps(signal_events()[0])
    assert "in the remote" in call["note"], "the milestone itself must survive"


def test_a_signal_fits_the_spool_by_construction_and_is_clamped_regardless(
    config, platform_root, monkeypatch
):
    """Two bounds, and the channel's is the tighter one on purpose.

    ``notify`` *refuses* an over-long note, so a digest built from a signal the
    channel accepted must never be able to reach it: the first assertion is what
    makes that structural. The clamp stays behind it because the digest is a
    sentence *around* the text and the scrub in front of it can lengthen what it
    masks — proven with the spool's bound lowered, since with the real numbers this
    branch cannot be reached.
    """
    assert ask_protocol.MAX_SIGNAL_CHARS < assistant.MAX_NOTE_CHARS

    live_session()
    signal_on("s-signaller", "x" * ask_protocol.MAX_SIGNAL_CHARS)
    monkeypatch.setattr(assistant, "MAX_NOTE_CHARS", 80)
    spool = Spool()

    detector(config, fleet(), spool=spool).tick_once()

    (call,) = spool.calls
    assert len(call["note"]) <= 80
    assert call["note"].endswith("…")


def test_one_unreadable_channel_costs_only_that_session(
    config, platform_root, detected
):
    """Containment on the sweep's own terms: quietly, at debug, per session.

    A container that filled its mount with junk must not be able to stop the tick
    for the fleet, and the operator has no action to take about it.
    """
    live_session(session_id="s-broken")
    live_session(session_id="s-fine", slug="review-141")
    signal_on("s-broken", "pushed MR !167")
    signal_on("s-fine", "the review is finished")
    real = ask_protocol.read_signals

    def refuse_one(directory, **kwargs):
        if directory.name.startswith("s-broken"):
            raise ask_protocol.AskError("this channel cannot be read")
        return real(directory, **kwargs)

    spool = Spool()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(detect.ask_protocol, "read_signals", refuse_one)
        tick = detector(config, fleet(), spool=spool)
        tick.tick_once()

    assert [call["data"]["session"] for call in spool.calls] == ["s-fine"]
    assert tick.failures == 0, "one bad channel is not a failed stage"
    assert any(
        "platform_signal_channel_unreadable" in record.message
        and record.levelno == logging.DEBUG
        for record in detected.records
    )


def test_a_record_that_says_nothing_is_skipped_and_the_rest_arrives(
    config, platform_root, detected
):
    """A torn file and a blank one, each costing that one entry.

    Written as bytes, because what is under test is a reader meeting what a crash
    or a hand edit leaves — ``post_signal`` refuses both.
    """
    session_id = live_session()
    directory = ask.prepare_ask_dir(session_id)
    (directory / "000001.signal.json").write_text('{"kind": "sig', encoding="utf-8")
    (directory / "000002.signal.json").write_text(
        json.dumps({"kind": "signal", "text": "   ", "at": "x"}), encoding="utf-8"
    )
    signal_on(session_id, "pushed MR !167")
    spool = Spool()

    detector(config, fleet(), spool=spool).tick_once()

    assert [call["data"]["text"] for call in spool.calls] == ["pushed MR !167"]
    assert any(
        "platform_signal_unusable" in record.message for record in detected.records
    )


def test_a_failing_ingestion_is_absorbed_and_the_diff_still_runs(config, detected):
    """Its own stage, like the sweep: neither the fleet view nor the diff depends
    on a session's channel being readable."""
    spool = Spool()
    tick = detector(
        config,
        fleet(),
        fleet(row("develop-141", reason="question", note=QUESTION, since="1")),
        spool=spool,
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(detect, "new_signals", _explode_reading)

        tick.tick_once()
        fresh = tick.tick_once()

    assert [signal.slug for signal in fresh] == ["develop-141"]
    assert len(spool.calls) == 1, "the diff's own digest still went out"
    assert tick.failures == 2 and tick.signalled == 0
    assert any("stage=signals" in record.message for record in detected.records)


def test_a_refused_digest_leaves_the_event_and_does_not_come_back(
    config, platform_root
):
    """``notify`` is best-effort by its own contract, and history is the copy that
    is not: retrying would re-deliver the milestone on every tick for as long as
    the spool was unwritable."""
    live_session()
    signal_on("s-signaller", "pushed MR !167")
    spool = Spool(error=RuntimeError("spool is unwritable"))
    tick = detector(config, fleet(), spool=spool)

    tick.tick_once()
    assert tick.failures == 1
    assert len(signal_events()) == 1
    assert marks() == {"s-signaller": 1}

    tick.tick_once()
    assert tick.failures == 1, "the signal is not new any more; it is not re-sent"
    assert len(signal_events()) == 1


def test_marks_that_cannot_be_read_fail_open(config, platform_root, detected):
    """A corrupt or hand-edited marks file must not swallow a milestone.

    Failing open costs a re-delivery the assistant can recognise as one — the
    digest carries the same signal id — while failing closed would silence "the
    review is finished" for the life of the session, which is the one thing this
    tool exists to deliver. Loud, because what an operator has to know is that the
    dedupe is not being kept for now.
    """
    live_session()
    signal_on("s-signaller", "pushed MR !167")
    store.snapshot_path(detect.SIGNAL_MARKS_FILE).write_text(
        "this is not json", encoding="utf-8"
    )
    spool = Spool()
    tick = detector(config, fleet(), spool=spool)

    tick.tick_once()

    assert len(spool.calls) == 1
    assert tick.failures == 0, "an unusable marks file is not a failed stage"
    assert any(
        "platform_signal_marks_unreadable" in record.message
        and record.levelno >= logging.WARNING
        for record in detected.records
    )


def test_marks_for_sessions_that_are_gone_are_dropped(config, platform_root):
    """One integer per session that ever signalled, for the life of the host, is a
    file that only grows — and a session id is never reused."""
    live_session(session_id="s-first")
    signal_on("s-first", "pushed MR !167")
    tick = detector(config, fleet())
    tick.tick_once()
    assert marks() == {"s-first": 1}

    registry.remove("s-first", force=True)
    live_session(session_id="s-second", slug="review-141")
    signal_on("s-second", "the review is finished")
    tick.tick_once()

    assert marks() == {"s-second": 1}


def _explode_reading():
    raise OSError("the channel is a smoking hole")


# --- the loop ----------------------------------------------------------------

def test_the_loop_ticks_until_it_is_stopped(config):
    """One smoke test, and the only assertion is that the thread ends.

    How often it ticked is a timing question this container cannot answer, so the
    loop's decisions are tested through :meth:`tick_once` and what is left here is
    "a real thread runs it, and ``stop`` gets it back".
    """
    ticked = threading.Event()

    def read(config, **kwargs):
        ticked.set()
        return fleet()

    tick = detect.Detector(
        config, interval=0.01, state_reader=read, notifier=Spool()
    )
    thread = tick.start()
    try:
        assert ticked.wait(30), "the thread never read the fleet view"
    finally:
        tick.stop()
    thread.join(30)

    assert not thread.is_alive()
    assert tick.stopped is True


def test_stopping_cuts_short_the_wait_rather_than_sitting_it_out(config):
    """The default sleep is a wait on the stop flag, so a shutdown does not have
    to sit out a full interval before the thread returns."""
    tick = detect.Detector(config, interval=600.0, state_reader=lambda c: fleet())
    tick.stop()
    tick.run()  # returns immediately, or this test times out


# --- (5) only `lmer platform run` grows the thread ----------------------------

class FakeDetector:
    """Records the wiring instead of starting a thread."""

    made = []

    def __init__(self, config, **kwargs):
        self.config = config
        self.started = False
        self.stopped_at_end = False
        FakeDetector.made.append(self)

    @property
    def notice(self):
        return "🔔 Detection every 30s (fake)"

    def start(self):
        self.started = True

    def stop(self):
        self.stopped_at_end = True


@pytest.fixture
def fake_detector(monkeypatch):
    FakeDetector.made = []
    monkeypatch.setattr(daemon, "Detector", FakeDetector)
    return FakeDetector


@pytest.fixture
def no_assistant(monkeypatch):
    """`run` also auto-starts the assistant (T63), which is a real spawn."""
    class NoSupervisor:
        def stop(self):
            pass

    monkeypatch.setattr(daemon, "_supervise_assistant", lambda config: NoSupervisor())


def serve_nothing(monkeypatch, on_serve=None):
    class FakeUvicorn:
        @staticmethod
        def run(app, **kwargs):
            if on_serve is not None:
                on_serve(kwargs)

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)


def test_run_starts_detection_before_serving_and_stops_it_after(
    platform_root, fake_detector, no_assistant, monkeypatch, capsys
):
    """Started before uvicorn accepts anything, and stopped when it returns.

    A detector left running past the server would keep fetching a mirror and
    spooling digests for a daemon nobody can reach — the same reason
    ``supervisor.stop()`` sits there.
    """
    at_serve = {}
    serve_nothing(monkeypatch, lambda _kwargs: at_serve.update(
        started=fake_detector.made[0].started,
        stopped=fake_detector.made[0].stopped_at_end,
    ))

    assert daemon.main(["run"]) == 0

    assert len(fake_detector.made) == 1, "one detector per daemon, not one per tick"
    assert at_serve == {"started": True, "stopped": False}
    assert fake_detector.made[0].stopped_at_end is True
    assert "Detection every" in capsys.readouterr().out


def _spawn_result():
    from lmer_platform.spawn import SpawnResult

    return SpawnResult(
        session_id="s-1", pid=4242, log_path="/logs/s-1.log",
        host="gitlab.example.com", project="agents/global", slug="develop-1",
        command=["lmer", "develop", "t"],
    )


@pytest.mark.parametrize("argv", [
    ["status"], ["status", "--json"], ["rescan"], ["runs"], ["runs", "--candidates"],
    ["secret"], ["adopt", "gitlab.example.com/agents/global/x"],
    ["forget", "gitlab.example.com/agents/global/x"],
    ["spawn", "develop", "https://example.com/x"], ["setup-ui"],
])
def test_no_other_subcommand_starts_a_detection_thread(
    platform_root, monkeypatch, argv
):
    """Same rule as the assistant auto-start: every other verb is diagnostic or
    one-shot, often run several times over, and one that quietly grew a thread
    pulling a work repo behind the answer would be the command an operator cannot
    use while working out what is wrong."""
    monkeypatch.setattr(
        daemon, "Detector",
        lambda config, **kwargs: pytest.fail(
            f"`lmer platform {' '.join(argv)}` started detection"
        ),
    )
    # Neither of these may reach the real thing: one spawns a container, the other
    # downloads a Node toolchain.
    monkeypatch.setattr(daemon, "spawn_session", lambda c, r: _spawn_result())
    monkeypatch.setattr(daemon, "setup_ui", lambda force_node=False: platform_root)

    daemon.main(argv)


# --- the fourth stage: runs nobody has looked at (issue #244) ------------------
#
# Everything above needs something to *happen*. This stage is the one that fires
# when nothing does, so what it must not become is the firehose the diff exists
# to avoid: one digest for the whole quiet fleet, and not again until another
# window has passed. lmer_platform.checkin owns the staleness rules and their
# tests; these pin the wiring — that the tick runs the pass, that the digest
# reaches the same spool through the same seam, that the window is read fresh
# from configuration, and that a failure here costs this stage and nothing else.

def stale_fleet(*slugs, seen_hours_ago=4):
    """A fleet of *slugs* the platform saw ``seen_hours_ago`` hours ago."""
    from datetime import datetime, timedelta, timezone

    payload = fleet(*[row(slug) for slug in slugs])
    when = (datetime.now(timezone.utc) - timedelta(hours=seen_hours_ago))
    checkin.observe(payload)
    marks = checkin.read_marks()
    for ref in marks:
        marks[ref] = {"first_seen": when.strftime("%Y-%m-%dT%H:%M:%SZ")}
    store.write_json(checkin.marks_path(), {"runs": marks})
    return payload


def checkin_digests(spool):
    return [
        call for call in spool.calls
        if call["kind"] == checkin.STALE_DIGEST_KIND
    ]


def test_a_tick_tells_the_assistant_which_runs_nobody_has_checked(config):
    payload = stale_fleet("review-mr-202", "develop-issue-236")
    spool = Spool()
    tick = detector(config, payload, spool=spool)

    tick.tick_once()

    digests = checkin_digests(spool)
    assert len(digests) == 1, "one digest names them all, never one per run"
    assert "2 runs have gone unchecked" in digests[0]["note"]
    assert "review-mr-202" in digests[0]["note"]
    assert digests[0]["data"]["count"] == 2
    assert tick.stale_reported == 2


def test_the_same_stale_run_is_not_announced_every_tick(config):
    payload = stale_fleet("review-mr-202")
    spool = Spool()
    tick = detector(config, payload, spool=spool)

    tick.tick_once()
    tick.tick_once()
    tick.tick_once()

    assert len(checkin_digests(spool)) == 1, (
        "a stale run costs one digest per window, not one per 30s tick"
    )


def test_a_quiet_fleet_that_was_just_seen_says_nothing(config):
    """The baseline: a run first seen on this tick is not stale."""
    spool = Spool()
    detector(config, fleet(row("develop-issue-1")), spool=spool).tick_once()
    assert checkin_digests(spool) == []


def test_the_window_is_read_fresh_from_configuration(config, monkeypatch):
    """An operator widening the window means the next sweep, not the next boot."""
    payload = stale_fleet("review-mr-202", seen_hours_ago=2)
    monkeypatch.setenv(cfg.ENV_CHECKIN_WINDOW, "10800")  # three hours
    spool = Spool()
    tick = detector(config, payload, spool=spool)

    tick.tick_once()
    assert checkin_digests(spool) == [], "two hours is inside a three-hour window"

    monkeypatch.setenv(cfg.ENV_CHECKIN_WINDOW, "3600")
    tick.tick_once()
    assert len(checkin_digests(spool)) == 1


def test_a_window_of_zero_turns_the_stage_off_entirely(config, monkeypatch):
    payload = stale_fleet("review-mr-202")
    monkeypatch.setenv(cfg.ENV_CHECKIN_WINDOW, "0")
    spool = Spool()
    tick = detector(config, payload, spool=spool)

    tick.tick_once()

    assert checkin_digests(spool) == []
    assert tick.stale_reported == 0


def test_a_refused_digest_is_retried_rather_than_swallowed(config):
    """Deliver-then-mark: nothing reached the spool, so nothing was announced."""
    payload = stale_fleet("review-mr-202")
    failing = Spool(error=RuntimeError("spool is unwritable"))
    tick = detector(config, payload, spool=failing)

    tick.tick_once()
    assert tick.stale_reported == 0

    working = Spool()
    again = detector(config, payload, spool=working)
    again.tick_once()
    assert len(checkin_digests(working)) == 1


def test_a_failing_check_in_pass_does_not_cost_the_diff(config, monkeypatch, detected):
    """Its own stage, like the sweep: one broken pass must not stop detection."""
    def explode(*_args, **_kwargs):
        raise OSError("marks file is unreadable")

    monkeypatch.setattr(checkin, "observe", explode)
    spool = Spool()
    payload = fleet(row("develop-issue-1", reason="question", note=QUESTION))
    tick = detector(config, fleet(), payload, spool=spool)

    tick.tick_once()   # baseline
    fresh = tick.tick_once()

    assert [signal.kind for signal in fresh] == ["question"]
    assert any(
        "stage=checkin" in record.message for record in detected.records
    ), "the absorbed failure has to name its own stage"


def test_the_startup_notice_says_the_window(config):
    notice = detect.Detector(config, notifier=Spool()).notice
    assert "60m" in notice and "looked at" in notice


def test_the_startup_notice_says_when_check_ins_are_off(config, monkeypatch):
    monkeypatch.setenv(cfg.ENV_CHECKIN_WINDOW, "0")
    notice = detect.Detector(config, notifier=Spool()).notice
    assert "OFF" in notice


# --- what the first review round found (iteration 1) --------------------------
#
# Three properties the check-in stage claimed and did not have. Each one is
# silent when it breaks — a spool quietly full of one digest class, a feature
# that cannot register a single read, a file that only grows — so each gets a
# test that fails loudly instead.

def test_a_marks_write_that_fails_costs_a_repeat_per_window_not_per_tick(
    config, monkeypatch
):
    """The eviction bug: at a 30s tick an unwritable checkins.json produced 120
    digests an hour against a spool bounded at 50, so every question, crash and
    completion digest was evicted by this one within half an hour."""
    payload = stale_fleet("review-mr-202")
    monkeypatch.setattr(checkin, "record_announced", _raises_store_error)
    spool = Spool()
    tick = detector(config, payload, spool=spool)

    for _ in range(6):
        tick.tick_once()

    assert len(checkin_digests(spool)) == 1, (
        "the digest was re-spooled every tick because the *record* of it could "
        "not be written"
    )


def test_the_in_memory_announcement_is_dropped_once_the_file_writes(config):
    """It is a fallback for one process, not a second store.

    Seeded directly with an announcement a window old, which is the state a
    daemon is in after an unwritable marks file and a window of waiting — the
    next digest lands, the file takes it, and the fallback has to go with it.
    """
    payload = stale_fleet("review-mr-202")
    tick = detector(config, payload, spool=Spool())
    tick._announced["gitlab.example.com/agents/global/review-mr-202"] = _hours_ago(4)

    tick.tick_once()

    assert not tick._announced, "a successful write has to retire the fallback"
    marks = checkin.read_marks()["gitlab.example.com/agents/global/review-mr-202"]
    assert marks.get("announced_at"), "and the record has to be on disk instead"


def _running_assistant():
    """The one fact ``_unattributed_caveat`` reads off the status."""
    return SimpleNamespace(running=True)


def _raises_store_error(*_args, **_kwargs):
    raise store.StoreError("checkins.json is unwritable")


def _hours_ago(hours):
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_an_assistant_on_the_shared_secret_is_told_why_this_repeats(
    config, monkeypatch, detected
):
    """The state of the first deploy that ships this: the running assistant was
    adopted across the restart, so it holds the operator's shared secret and no
    read it makes can ever register."""
    payload = stale_fleet("review-mr-202")
    monkeypatch.setattr(assistant, "status", _running_assistant)
    assert cfg.active_assistant_credential() is None, "precondition"
    spool = Spool()
    tick = detector(config, payload, spool=spool)

    tick.tick_once()

    digest = checkin_digests(spool)[0]
    assert "shared secret" in digest["note"], (
        "the digest that will not stop has to say why it will not stop"
    )
    assert "rotate" in digest["note"].lower()
    assert digest["data"]["caveat"], "the caveat is machine-readable too"
    assert any(
        "platform_checkin_unattributed" in record.message
        for record in detected.records
    ), "and the operator reading logs rather than the chat gets one line"


def test_an_attributable_assistant_gets_no_caveat(config, monkeypatch):
    payload = stale_fleet("review-mr-202")
    cfg.mint_assistant_credential()
    monkeypatch.setattr(assistant, "status", _running_assistant)
    spool = Spool()
    detector(config, payload, spool=spool).tick_once()

    digest = checkin_digests(spool)[0]
    assert "shared secret" not in digest["note"]
    assert "caveat" not in digest["data"]


def test_no_assistant_at_all_is_not_a_caveat(config):
    """Nothing to attribute — and whoever starts one next mints a credential."""
    payload = stale_fleet("review-mr-202")
    spool = Spool()
    detector(config, payload, spool=spool).tick_once()
    assert "shared secret" not in checkin_digests(spool)[0]["note"]


def test_a_disabled_window_still_prunes_the_marks_file(config, monkeypatch):
    """observe() is the only pruner, and _note_check keeps stamping whatever the
    window says — so an early return above it left the file growing forever on a
    host with the feature switched off."""
    monkeypatch.setenv(cfg.ENV_CHECKIN_WINDOW, "0")
    both = fleet(row("kept"), row("forgotten"))
    tick = detector(config, both, fleet(row("kept")), spool=Spool())

    tick.tick_once()
    assert len(checkin.read_marks()) == 2

    tick.tick_once()
    assert list(checkin.read_marks()) == [
        "gitlab.example.com/agents/global/kept"
    ], "a run that left the fleet kept its entry forever"

# --- a halted session reaches the assistant (#243) ----------------------------
#
# The reason itself is inventory's (tests/test_platform_inventory.py owns which
# silence means what). What is under test here is the half that is this module's:
# the assistant is told, once, and in one line that names the run and what
# stopped it — the requirement the issue states as "the assistant receives a
# digest naming the run and the error class".


STALL_NOTE = (
    "no output for 14m; the harness recorded a provider refusal: billing_error"
)


def test_a_halted_session_reaches_the_assistant_naming_run_and_cause(config):
    """One line, the run first and the cause in it.

    The run reference leads because the assistant's next move is an API call and
    that triple is what every run route takes; the cause rides in the note
    because an assistant that has to open a terminal to find out what broke is
    the situation this issue exists to end.
    """
    spool = Spool()
    tick = detector(
        config,
        fleet(),
        fleet(row("develop-243", reason="stalled", note=STALL_NOTE,
                  state="running", label="issue 243",
                  since="2026-08-05T16:32:00Z")),
        spool=spool,
    )

    tick.tick_once()
    tick.tick_once()

    assert len(spool.calls) == 1
    call = spool.calls[0]
    assert call["kind"] == "stalled"
    assert "gitlab.example.com/agents/global/develop-243" in call["note"]
    assert "billing_error" in call["note"]
    assert call["data"]["slug"] == "develop-243"
    assert call["data"]["state"] == "running", (
        "the row stays running: the container is up, which is what makes it worth "
        "flagging rather than merely broken"
    )


def test_a_halt_that_persists_is_announced_once(config):
    """The failure this replaces was silence; the failure it must not become is a
    digest a minute for one halted run.

    ``since`` is the moment it went quiet and inventory floors it to the minute,
    so a condition that persists keeps producing the same identity however many
    ticks it survives.
    """
    spool = Spool()
    stalled = fleet(row("develop-243", reason="stalled", note=STALL_NOTE,
                        state="running", since="2026-08-05T16:32:00Z"))
    tick = detector(config, fleet(), stalled, stalled, stalled, spool=spool)

    for _ in range(4):
        tick.tick_once()

    assert len(spool.calls) == 1, (
        f"one halt spooled {len(spool.calls)} digests: {spool.notes}"
    )


def test_a_session_that_halts_again_is_a_second_event(config):
    """Recovered and halted again is two events, not a duplicate of one.

    The second halt has its own ``since`` — the session produced output in
    between, which is what moved it — and swallowing that would hide exactly the
    case where an operator restored credits and the run stopped a second time.
    """
    spool = Spool()
    tick = detector(
        config,
        fleet(),
        fleet(row("develop-243", reason="stalled", note=STALL_NOTE,
                  state="running", since="2026-08-05T16:32:00Z")),
        fleet(row("develop-243", state="running")),
        fleet(row("develop-243", reason="stalled", note=STALL_NOTE,
                  state="running", since="2026-08-05T18:10:00Z")),
        spool=spool,
    )

    for _ in range(4):
        tick.tick_once()

    assert len(spool.calls) == 2


def test_the_backstops_digest_does_not_claim_a_diagnosis(config):
    """The three paths carry different strengths of claim, and the weakest one
    has to survive the trip to the assistant saying so — otherwise an hour-late
    "we do not know why" reads as a confident finding."""
    spool = Spool()
    note = "no output for 61m; nothing in the transcript says why (flagged on silence alone)"
    tick = detector(
        config,
        fleet(),
        fleet(row("develop-243", reason="stalled", note=note, state="running",
                  since="2026-08-05T16:32:00Z")),
        spool=spool,
    )

    tick.tick_once()
    tick.tick_once()

    assert "silence alone" in spool.calls[0]["note"]
