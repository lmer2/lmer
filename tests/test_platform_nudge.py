"""The digest nudge: the platform says a spool is waiting (issue #317).

The push-side backstop to a pull-side watch. Everything about it is a boundary,
so this suite is mostly boundaries — in both directions, because a nudge that
only fires late is as useless as one that fires always, and a nudge that fires
at a working assistant is noise it has to read.

What is pinned, in the order it matters:

- **All five conditions gate it** — interval, liveness, count, accumulation age,
  and the session's own state (idle, and having drawn a byte) — each asserted from
  both sides.
- **The rate limit is a window, not a one-shot.** A spool still unread one
  interval after a nudge is nudged again, because the platform cannot know its
  line was registered as a submit. A drain ends the accumulation.
- **An unknowable idle reading proceeds**, and the sentence does not then claim
  the session was quiet — while a session known never to have drawn a byte is
  refused, because that case is knowable rather than unknowable.
- **The sentence carries no newline**, says who is talking, and names the route.
- **Nothing about it can cost the daemon its tick**: a raising send is absorbed and
  the diff still comes back. Whether it leaves a mark follows *delivery*, not the
  raise — a refused send leaves none and earns its retry, while one that raised
  after the bytes were typed is bounded like a clean send.

Time moves by writing stamps and by passing ``now``; nothing here sleeps.
"""

import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lmer_platform import assistant, detect, nudge, store
from lmer_platform.config import PlatformConfig
from lmer_platform.session_io import ControlPlaneError, SessionIOError

AFTER = 180
THRESHOLD = 1


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


def iso(**delta):
    """An ISO-Z stamp *delta* in the past."""
    when = datetime.now(timezone.utc) - timedelta(**delta)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def state(*, pending=1, oldest=None, nudged_at=None):
    """A spool of *pending* notes whose oldest carries *oldest*."""
    notes = tuple(
        assistant.PendingNote(
            at=(oldest if index == 0 and oldest else iso(seconds=1)),
            kind="event",
            note=f"note {index}",
        )
        for index in range(pending)
    )
    return assistant.AssistantState(
        pending=notes,
        pending_since=(oldest if oldest else iso(seconds=1)),
        nudged_at=nudged_at,
    )


def decide(state_, *, running=True, idle=600.0, produced=True, after=AFTER,
           threshold=THRESHOLD):
    return nudge.due(
        state_,
        running=running,
        idle_seconds=idle,
        produced_output=produced,
        after_seconds=after,
        pending_threshold=threshold,
    )


# --- the five conditions ------------------------------------------------------

def test_a_spool_that_has_waited_past_the_interval_is_due():
    result = decide(state(oldest=iso(minutes=10)))

    assert result is not None
    assert result.count == 1
    assert result.waited_seconds >= 600
    assert result.repeat is False


def test_a_spool_inside_the_interval_is_not_due():
    assert decide(state(oldest=iso(seconds=AFTER - 30))) is None


def test_an_empty_spool_is_never_due():
    assert decide(state(pending=0)) is None


def test_a_spool_under_the_threshold_is_not_due():
    assert decide(state(pending=2, oldest=iso(minutes=10)), threshold=3) is None
    assert decide(state(pending=3, oldest=iso(minutes=10)), threshold=3) is not None


def test_an_interval_of_zero_is_the_off_switch():
    assert decide(state(oldest=iso(hours=5)), after=0) is None


def test_a_negative_interval_cannot_turn_the_nudge_on():
    """Defence in depth beside config's own floor: whatever reaches this must not
    read as 'every tick'."""
    assert decide(state(oldest=iso(hours=5)), after=-60) is None


def test_nothing_is_due_when_no_assistant_is_running():
    assert decide(state(oldest=iso(hours=1)), running=False) is None


def test_a_working_assistant_is_not_nudged():
    """The condition that makes the drain guard free: an assistant that is doing
    anything at all — including draining — is not idle."""
    assert decide(state(oldest=iso(hours=1)), idle=5.0) is None


def test_an_idle_reading_exactly_at_the_boundary_is_idle_enough():
    assert decide(state(oldest=iso(minutes=10)), idle=float(AFTER)) is not None


def test_an_unknowable_idle_reading_proceeds():
    """The judgment call, pinned: blocking on ``None`` would switch the backstop
    off on exactly the hosts least able to notice (an older image, a control
    plane that did not answer)."""
    result = decide(state(oldest=iso(minutes=10)), idle=None)

    assert result is not None
    assert result.idle is None, "the reading is carried so the log can explain it"


def test_a_threshold_below_one_still_needs_a_digest():
    assert decide(state(pending=0), threshold=0) is None


# --- the clock, and what makes the rate limit a window ------------------------

def test_a_previous_nudge_restarts_the_clock():
    """One nudge per interval, not one per digest and not one per accumulation:
    the mark is a stamp on the clock, so a fresh nudge cannot follow itself."""
    just_nudged = state(oldest=iso(hours=2), nudged_at=iso(seconds=30))

    assert decide(just_nudged) is None


def test_a_spool_still_unread_an_interval_after_a_nudge_is_nudged_again():
    """The lost-submit case, which is why this is a window. ``send_input`` proves
    the bytes arrived and cannot prove the TUI registered the Enter."""
    stale = state(oldest=iso(hours=2), nudged_at=iso(minutes=10))

    result = decide(stale)

    assert result is not None
    assert result.repeat is True, "a retry is worth saying in the log"


def test_the_clock_runs_from_the_oldest_digest_not_the_newest():
    """A busy fleet keeps adding digests; the wait belongs to the first one."""
    old = assistant.PendingNote(at=iso(minutes=20), kind="event", note="old")
    fresh = assistant.PendingNote(at=iso(seconds=2), kind="event", note="fresh")
    result = decide(assistant.AssistantState(pending=(old, fresh)))

    assert result is not None
    assert result.waited_seconds >= 1200


def test_a_spool_with_no_readable_stamp_says_nothing():
    """A hand-edited or ancient file: a sentence claiming a wait this cannot
    measure is worse than leaving it to the assistant's own watch."""
    broken = assistant.AssistantState(
        pending=(assistant.PendingNote(at="not a date", kind="event", note="x"),)
    )

    assert decide(broken) is None


def test_a_future_stamp_does_not_produce_a_nudge():
    ahead = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    assert decide(state(oldest=ahead)) is None


def test_now_is_injectable_so_the_boundary_needs_no_sleep():
    at = "2026-08-19T09:33:00Z"
    now = datetime(2026, 8, 19, 9, 35, 0, tzinfo=timezone.utc)

    assert nudge.due(
        state(oldest=at), running=True, idle_seconds=600.0,
        after_seconds=AFTER, pending_threshold=1, now=now,
    ) is None
    assert nudge.due(
        state(oldest=at), running=True, idle_seconds=600.0,
        after_seconds=AFTER, pending_threshold=1,
        now=now + timedelta(minutes=2),
    ) is not None


# --- the sentence -------------------------------------------------------------

def test_the_prompt_is_one_paragraph_with_no_newline():
    """A correctness property, not formatting: this is typed into a TUI that
    submits on Enter, so an embedded newline would send half a message."""
    text = nudge.prompt(nudge.Nudge(count=3, waited_seconds=600, idle=600, repeat=False))

    assert "\n" not in text
    assert "\r" not in text
    assert len(text) <= nudge.MAX_PROMPT_CHARS


def test_the_prompt_says_who_is_talking_and_what_to_do():
    text = nudge.prompt(nudge.Nudge(count=3, waited_seconds=600, idle=600, repeat=False))

    assert text.startswith("[lmer platform] ")
    assert "not the operator" in text
    assert "not a new task" in text
    assert nudge.PROMPT_ROUTE in text


def test_the_prompt_counts_and_pluralises():
    one = nudge.prompt(nudge.Nudge(count=1, waited_seconds=61, idle=600, repeat=False))
    many = nudge.prompt(nudge.Nudge(count=9, waited_seconds=600, idle=600, repeat=False))

    assert "1 digest has been waiting" in one
    assert "1 minute " in one
    assert "9 digests have been waiting" in many
    assert "10 minutes" in many


def test_the_prompt_never_claims_a_quiet_session_it_did_not_measure():
    """States a claim at the strength it was measured: on a host with no idle
    reading the nudge still goes out, but it does not assert the session was
    quiet."""
    measured = nudge.prompt(
        nudge.Nudge(count=1, waited_seconds=600, idle=600, repeat=False)
    )
    unknown = nudge.prompt(
        nudge.Nudge(count=1, waited_seconds=600, idle=None, repeat=False)
    )

    assert "has been quiet" in measured
    assert "has been quiet" not in unknown


def test_the_prompt_never_says_a_wait_of_zero_minutes():
    text = nudge.prompt(nudge.Nudge(count=1, waited_seconds=1, idle=600, repeat=False))

    assert "0 minute" not in text
    assert "1 minute" in text


def test_the_prompt_carries_no_digest_text():
    """The spool stays the one bounded, scrubbed source: what a nudge pushes is
    that something is waiting, never what."""
    text = nudge.prompt(nudge.Nudge(count=2, waited_seconds=600, idle=600, repeat=True))

    assert "note" not in text.lower().replace("nothing", "")


# --- the module writes nothing ------------------------------------------------

def test_deciding_writes_no_state(platform_root):
    """Every boundary above is testable without a container because the decision
    is a function; the mark, the event and the send belong to the caller."""
    assistant.notify("first")
    before = store.read_json(assistant.state_path())

    decide(assistant.read_state())

    assert store.read_json(assistant.state_path()) == before


# --- the detector's stage -----------------------------------------------------

class _Sender:
    """A stand-in for ``session_io.send_input``, recording what it was given."""

    def __init__(self, *, raises=None, reply=None):
        self.calls = []
        self._raises = raises
        self._reply = reply or {"bytes_written": 1, "submit_confirmed": True}
        self.lock = threading.Lock()

    def __call__(self, session_id, data, *, append_newline=False):
        with self.lock:
            self.calls.append((session_id, data, append_newline))
        if self._raises is not None:
            raise self._raises
        return self._reply


@pytest.fixture
def detector(platform_root, monkeypatch):
    """A detector whose fleet read, spool and session are all stubbed out.

    The tick's other four stages are exercised in
    ``tests/test_platform_detection.py``; what these drive is the fifth.
    """
    def _build(*, running=True, idle=600.0, produced=True, reading=True,
               sender=None, after=AFTER, threshold=1):
        monkeypatch.setattr(
            detect.assistant, "status",
            lambda: assistant.AssistantStatus(
                running=running,
                session_id="assistant-1" if running else None,
                pid=1234 if running else None,
                started_at=iso(hours=1),
                generation=1,
                stale=False,
                tracked=True,
                pending=len(assistant.read_state().pending),
                handoff=None,
                nudged_at=assistant.read_state().nudged_at,
            ),
        )
        monkeypatch.setattr(
            detect, "session_output_state",
            lambda session_id: None if reading is None else {
                "idle_seconds": idle, "produced": produced,
            },
        )
        monkeypatch.setattr(
            detect, "nudge_settings",
            lambda: {
                "after_seconds": _Setting(after),
                "pending_threshold": _Setting(threshold),
            },
        )
        instance = detect.Detector(
            PlatformConfig(),
            state_reader=lambda config: {"runs": []},
            sender=sender or _Sender(),
        )
        return instance

    return _build


class _Setting:
    def __init__(self, value):
        self.value = value


def test_a_due_nudge_is_typed_into_the_assistants_session(detector, platform_root):
    sender = _Sender()
    instance = detector(sender=sender)
    assistant.notify("a question opened")
    _age_spool(minutes=10)

    assert instance.nudge_once() is not None

    assert len(sender.calls) == 1
    session_id, data, append_newline = sender.calls[0]
    assert session_id == "assistant-1"
    assert data.startswith("[lmer platform] ")
    assert append_newline is True, (
        "without Enter the reminder sits unsent in the agent's input box"
    )
    assert instance.nudged == 1


def test_the_nudge_is_marked_so_the_next_tick_is_quiet(detector, platform_root):
    sender = _Sender()
    instance = detector(sender=sender)
    assistant.notify("a question opened")
    _age_spool(minutes=10)

    assert instance.nudge_once() is not None
    assert assistant.read_state().nudged_at is not None
    assert instance.nudge_once() is None, "one per window, not one per tick"
    assert len(sender.calls) == 1


def test_a_drain_between_ticks_re_arms_the_next_accumulation(
    detector, platform_root
):
    sender = _Sender()
    instance = detector(sender=sender)
    assistant.notify("first")
    _age_spool(minutes=10)
    instance.nudge_once()

    assistant.take_pending()
    assistant.notify("second")
    # A distinctly different start, which is what a real second accumulation has:
    # it begins after the drain, so its stamp cannot equal the first one's.
    _age_spool(minutes=20)

    assert instance.nudge_once() is not None
    assert len(sender.calls) == 2


def test_a_refused_send_is_absorbed_and_leaves_no_mark(detector, platform_root):
    """The tick must survive an unreachable container, and a mark written for a
    nudge that never went out would silence the retry."""
    sender = _Sender(raises=detect.SessionIOError("control plane unreachable"))
    instance = detector(sender=sender)
    assistant.notify("a question opened")
    _age_spool(minutes=10)

    assert instance.nudge_once() is None
    assert assistant.read_state().nudged_at is None
    assert instance.failures == 1
    assert instance.nudged == 0


def test_a_refused_send_does_not_cost_the_tick_its_diff(detector, platform_root):
    sender = _Sender(raises=RuntimeError("boom"))
    instance = detector(sender=sender)
    assistant.notify("a question opened")
    _age_spool(minutes=10)

    assert instance.tick_once() == []
    assert instance.tick_once() == []
    assert instance.failures >= 1


def test_the_stage_runs_off_the_tick(detector, platform_root):
    sender = _Sender()
    instance = detector(sender=sender)
    assistant.notify("a question opened")
    _age_spool(minutes=10)

    instance.tick_once()

    assert len(sender.calls) == 1, "the nudge is a stage, not a separate loop"


def test_nothing_is_typed_when_the_nudge_is_switched_off(detector, platform_root):
    sender = _Sender()
    instance = detector(sender=sender, after=0)
    assistant.notify("a question opened")
    _age_spool(hours=3)

    assert instance.nudge_once() is None
    assert sender.calls == []


def test_nothing_is_typed_when_no_assistant_is_running(detector, platform_root):
    sender = _Sender()
    instance = detector(sender=sender, running=False)
    assistant.notify("a question opened")
    _age_spool(hours=3)

    assert instance.nudge_once() is None
    assert sender.calls == []


def test_nothing_is_typed_while_the_assistant_is_working(detector, platform_root):
    sender = _Sender()
    instance = detector(sender=sender, idle=4.0)
    assistant.notify("a question opened")
    _age_spool(hours=3)

    assert instance.nudge_once() is None
    assert sender.calls == []


def test_an_unknowable_idle_reading_still_nudges(detector, platform_root):
    sender = _Sender()
    instance = detector(sender=sender, idle=None)
    assistant.notify("a question opened")
    _age_spool(minutes=10)

    assert instance.nudge_once() is not None
    assert "has been quiet" not in sender.calls[0][1]


def test_the_notice_says_the_nudge_is_on_and_at_what_numbers(detector):
    instance = detector()

    assert "nudge" in instance.notice.lower()
    assert str(AFTER // 60) in instance.notice


def test_the_notice_says_so_when_the_nudge_is_off(detector):
    """A disabled feature and a broken one look identical from a chat window."""
    instance = detector(after=0)

    assert "OFF" in instance.notice


def _age_spool(**delta):
    """Backdate the accumulation, which is how this suite moves time.

    Both the notes and ``pending_since``: the age the decision reads comes from
    the stamp, and the notes are only its fallback — a helper that moved one and
    not the other would test neither honestly.
    """
    payload = store.read_json(assistant.state_path()) or {}
    for note in payload.get("pending") or []:
        note["at"] = iso(**delta)
    if payload.get("pending"):
        payload["pending_since"] = iso(**delta)
    store.write_json(assistant.state_path(), payload)


def test_both_tick_paths_reach_the_stage(detector, platform_root):
    """The baseline-establishing tick nudges too: a spool inherited across a
    daemon restart is the one that has waited longest, and the silent-first-tick
    rule is about not re-announcing conditions, not about staying quiet on it."""
    sender = _Sender()
    instance = detector(sender=sender)
    assistant.notify("a question opened")
    _age_spool(minutes=10)

    instance.tick_once()          # establishes the baseline
    assert len(sender.calls) == 1

    assistant.take_pending()
    assistant.notify("another")
    _age_spool(minutes=20)
    instance.tick_once()          # the ordinary path

    assert len(sender.calls) == 2


def test_a_nudge_is_recorded_in_platform_history(detector, platform_root):
    """The platform typed into a session on its own initiative, so "who did that,
    and on what numbers" has to be answerable from the same history that records a
    wind-down."""
    instance = detector(sender=_Sender(), idle=None)
    assistant.notify("a question opened")
    _age_spool(minutes=10)

    instance.nudge_once()

    events = [
        line for line in store.events_path().read_text().splitlines()
        if detect.ASSISTANT_NUDGED in line
    ]
    assert len(events) == 1
    data = json.loads(events[0])["data"]
    assert data["session"] == "assistant-1"
    assert data["pending"] == 1
    assert data["waited_seconds"] >= 600
    assert data["idle_seconds"] is None, (
        "null is a value here: it says the platform typed into a session whose "
        "idleness it could not measure"
    )
    assert data["repeat"] is False
    assert data["marked"] is True


# --- the prose and the code that types it (issue #317) ------------------------

def test_the_taskdef_tells_the_assistant_the_nudge_exists():
    """The orchestrate taskdef used to say, in bold, that nothing ever arrives in
    this session. An assistant reading that and then receiving a line takes it for
    the operator talking — so the prose and the sender ship together, and this is
    what stops them drifting apart."""
    text = (
        Path(__file__).resolve().parents[1]
        / "taskdef" / "orchestrate" / "instructions.txt"
    ).read_text()

    assert "[lmer platform]" in text, "the assistant has to be able to attribute it"
    assert nudge.PROMPT_ROUTE in text
    assert "not the operator" in text
    assert "digest arrives in this session, ever" in text, (
        "the digests themselves are still never pushed, and that has to stay said"
    )


def test_the_env_vars_are_documented_where_every_other_one_is():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "LMER-CLI.md").read_text()

    for name in ("LMER_PLATFORM_NUDGE_AFTER_SECONDS",
                 "LMER_PLATFORM_NUDGE_PENDING_THRESHOLD"):
        assert name in doc


# --- the window's edges (review of !234) ---------------------------------------
#
# Three findings, all about a reminder that went out while the thing bounding the
# next one did not. The bound is what these pin, in the two directions that
# matter: it must hold when the durable mark cannot be written, and it must NOT
# outlive the accumulation it was about.

def test_a_send_whose_mark_cannot_be_written_still_bounds_the_window(
    detector, platform_root, monkeypatch
):
    """`mark_nudged` returns None whenever the state write fails, by design. The
    rate limit's only memory used to be that write, so an unwritable
    `assistant.json` typed a reminder into a live terminal every tick — measured
    at five sends in five consecutive ticks, while the same outage stops the
    assistant draining the spool at all."""
    monkeypatch.setattr(detect.assistant, "mark_nudged", lambda *_a, **_k: None)
    sender = _Sender()
    instance = detector(sender=sender)
    assistant.notify("a question opened")
    _age_spool(minutes=10)

    for _ in range(5):
        instance.nudge_once()

    assert len(sender.calls) == 1
    assert assistant.read_state().nudged_at is None, (
        "the durable mark genuinely did not land; the bound is the process's own"
    )

    # One send in five ticks is also what a bound that suppressed the accumulation
    # *forever* would look like, so the window has to be shown re-arming while the
    # write still fails. Time moves by rewriting the stamp, which is this suite's
    # method throughout — here the in-memory one, since that is the live bound.
    seq, _ = instance._nudged
    instance._nudged = (seq, iso(minutes=10))

    assert instance.nudge_once() is not None
    assert len(sender.calls) == 2, "the window re-arms; it is not a one-shot"


def test_a_send_that_raised_after_delivering_bounds_the_window(
    detector, platform_root
):
    """`send_input` raises `ControlPlaneError` on a receipt mismatch *after* the
    plane has typed the payload — its own message says "the payload WAS typed into
    the session". Repeating it every tick is the same unbounded loop as an
    unwritable mark, so what decides is whether the bytes arrived, not whether the
    call raised."""
    sender = _Sender(
        raises=ControlPlaneError("acknowledged different bytes", delivered=True)
    )
    instance = detector(sender=sender)
    assistant.notify("a question opened")
    _age_spool(minutes=10)

    for _ in range(5):
        instance.nudge_once()

    assert len(sender.calls) == 1
    assert assistant.read_state().nudged_at is not None, (
        "the bytes are in the session, so the window is marked durably too"
    )
    assert instance.failures == 1, "and the raise is still absorbed and counted"


def test_a_refused_send_is_retried_because_nothing_was_typed(
    detector, platform_root
):
    """The other half of the same branch: a refusal earns its retry. Without the
    distinction, bounding the delivered case would have silenced this one."""
    sender = _Sender(raises=ControlPlaneError("session refused the input (503)"))
    instance = detector(sender=sender)
    assistant.notify("a question opened")
    _age_spool(minutes=10)

    for _ in range(4):
        instance.nudge_once()

    assert len(sender.calls) == 4
    assert assistant.read_state().nudged_at is None


def test_a_delivered_but_failed_send_says_so_in_the_event(detector, platform_root):
    """A reader counting nudges has to be able to see the one case where "a
    reminder was typed" and "the call failed" are both true."""
    instance = detector(
        sender=_Sender(raises=ControlPlaneError("mismatch", delivered=True))
    )
    assistant.notify("a question opened")
    _age_spool(minutes=10)

    instance.nudge_once()

    data = json.loads([
        line for line in store.events_path().read_text().splitlines()
        if detect.ASSISTANT_NUDGED in line
    ][0])["data"]
    assert "ControlPlaneError" in data["send_error"]
    assert data["marked"] is True
    assert instance.nudged == 1


def test_the_in_memory_bound_does_not_outlive_its_accumulation(
    detector, platform_root, monkeypatch
):
    """A bound remembered per process must describe exactly one accumulation, and
    `pending_since` cannot identify one: a drain and a fresh digest inside a single
    second give two accumulations the same stamp.

    What mis-keying actually costs is small and was measured after an earlier
    version of this docstring overstated it — a delay of the send's own duration
    and a wrong `repeat` flag, not a lost window, because `_window` caps with
    `min(accumulation_age, since_nudge)` and the memory stamp never materially
    precedes the new accumulation. So this pins exactness, not a rescue.
    """
    monkeypatch.setattr(detect.assistant, "mark_nudged", lambda *_a, **_k: None)
    sender = _Sender()
    instance = detector(sender=sender)
    assistant.notify("first")
    _age_spool(minutes=10)
    assert instance.nudge_once() is not None
    first_since = assistant.read_state().pending_since

    assistant.take_pending()
    assistant.notify("second")
    _age_spool(minutes=10)
    payload = store.read_json(assistant.state_path())
    # Both accumulations dated to the same second — reachable when a drain and a
    # fresh digest land inside one, and the case a timestamp key cannot tell
    # apart. The sequence can.
    payload["pending_since"] = first_since
    store.write_json(assistant.state_path(), payload)
    assert assistant.read_state().pending_seq == 2

    assert instance.nudge_once() is not None, (
        "a new accumulation is owed its own nudge even when its stamp collides "
        "with the remembered one"
    )
    assert len(sender.calls) == 2


def test_the_stage_runs_even_when_the_fleet_read_fails(detector, platform_root):
    """The third exit from `tick_once`, and the one that matters most: a host
    whose work-repo mirror cannot be read is exactly where a spool sits unread and
    nobody notices. The stage needs nothing from that read."""
    sender = _Sender()
    instance = detector(sender=sender)
    instance._read_state = lambda config: (_ for _ in ()).throw(
        RuntimeError("mirror unreadable")
    )
    assistant.notify("a question opened")
    _age_spool(minutes=10)

    assert instance.tick_once() == []
    assert instance.tick_once() == []

    assert len(sender.calls) == 1, "reached once, and rate-limited like any other"
    assert instance.failures == 2, "the scan failure is still absorbed and counted"


# --- what the sentence may claim (review of !234) ------------------------------

def test_the_reported_wait_is_the_accumulation_not_the_last_nudge():
    """The repeat exists because the first line may not have landed, so it is
    exactly where the assistant must not be told a two-hour backlog is three
    minutes old. The cap belongs to the rate limit only."""
    state_ = assistant.AssistantState(
        pending=tuple(
            assistant.PendingNote(at=iso(hours=2), kind="event", note=f"n{i}")
            for i in range(4)
        ),
        pending_since=iso(hours=2),
        nudged_at=iso(seconds=200),
    )

    result = decide(state_)

    assert result is not None and result.repeat is True
    assert result.waited_seconds >= 7200
    assert "120 minutes" in nudge.prompt(result)


def test_a_loud_fleet_cannot_pin_the_clock_below_the_interval():
    """The ages of *retained* notes drift younger as the bound evicts older ones,
    so a spool taking a digest every few seconds never aged past the interval —
    total failure in the case where the spool matters most. The accumulation's own
    stamp does not move."""
    notes = tuple(
        assistant.PendingNote(at=iso(seconds=3 * i), kind="event", note=f"n{i}")
        for i in range(assistant.MAX_PENDING, 0, -1)
    )
    evicting = assistant.AssistantState(pending=notes, pending_since=iso(minutes=30))

    assert max(store.age_seconds(n.at) for n in evicting.pending) < AFTER, (
        "the premise: no retained note is as old as the interval"
    )
    assert decide(evicting) is not None


def test_an_inherited_accumulation_keeps_its_age_when_the_next_digest_arrives(
    platform_root
):
    """The upgrade case, and it goes through `notify()` on purpose: evaluating
    `due()` on a hand-built stampless state passes either way, which is why the
    fallback test below did not catch this.

    A spool inherited from a build with no stamp was re-dated to `now` by the next
    arriving digest, so an overdue accumulation stopped being due for a full
    interval — #317's own failure, on this feature's deploy.
    """
    store.write_json(assistant.state_path(), {
        "pending": [
            {"at": iso(minutes=30), "kind": "event", "note": "a question opened"},
        ],
    })
    legacy = assistant.read_state()
    assert legacy.pending_since is None and legacy.pending_seq == 0
    assert decide(legacy) is not None, "the premise: it is overdue before the digest"

    assistant.notify("second digest")

    upgraded = assistant.read_state()
    assert upgraded.pending_since == legacy.pending[0].at, (
        "the oldest retained note is adopted, not now"
    )
    assert upgraded.pending_seq == 0, (
        "and it is the same accumulation, so the sequence does not move"
    )
    result = decide(upgraded)
    assert result is not None and result.waited_seconds >= 1800


def test_a_genuine_new_accumulation_is_still_dated_now(platform_root):
    """The other side of the same predicate: after a drain, the next digest starts
    a new accumulation and must not inherit the drained one's age."""
    store.write_json(assistant.state_path(), {
        "pending": [
            {"at": iso(hours=3), "kind": "event", "note": "old"},
        ],
    })
    assistant.take_pending()

    assistant.notify("fresh")

    fresh = assistant.read_state()
    assert store.age_seconds(fresh.pending_since) < 5
    assert fresh.pending_seq == 1
    assert decide(fresh) is None


def test_a_state_with_no_accumulation_stamp_falls_back_to_the_notes():
    """A state written before the stamp existed, or hand-edited. The oldest note
    owns the wait, exactly as before."""
    old_shape = assistant.AssistantState(
        pending=(
            assistant.PendingNote(at=iso(minutes=20), kind="event", note="old"),
            assistant.PendingNote(at=iso(seconds=2), kind="event", note="fresh"),
        ),
    )

    result = decide(old_shape)

    assert result is not None
    assert result.waited_seconds >= 1200


# --- a session that has not drawn a byte (review of !234) ----------------------

def test_a_session_that_has_never_produced_output_is_not_nudged():
    """`idle_seconds` is None until the first byte, and `session_activity`
    collapses that into the same None as "no answer". A rotate carrying an aged
    spool would type into a PTY nobody is reading — and mark it, buying another
    interval of silence at the moment a new incarnation was meant to hear."""
    assert decide(state(oldest=iso(minutes=10)), produced=False) is None
    assert decide(state(oldest=iso(minutes=10)), produced=True) is not None
    assert decide(state(oldest=iso(minutes=10)), produced=None) is not None, (
        "genuinely unknowable still proceeds — that decision is unchanged"
    )


def test_the_never_drawn_session_leaves_no_mark(detector, platform_root):
    sender = _Sender()
    instance = detector(sender=sender, idle=None, produced=False)
    assistant.notify("a question opened")
    _age_spool(minutes=10)

    assert instance.nudge_once() is None
    assert sender.calls == []
    assert assistant.read_state().nudged_at is None, (
        "nothing was typed, so nothing may be suppressed"
    )


def test_session_output_state_separates_the_two_nones(monkeypatch):
    """The distinction exists at the source — /healthz reports `cursor`, the
    offset past the last byte ever written — so it is read rather than inferred."""
    from lmer_platform import session_io

    def health(payload):
        monkeypatch.setattr(session_io, "quiet_health", lambda session_id: payload)
        return session_io.session_output_state("s-1")

    assert health(None) is None, "no answer stays no answer"
    assert health({"cursor": 0, "idle_seconds": None})["produced"] is False
    assert health({"cursor": 4096, "idle_seconds": 12.0}) == {
        "idle_seconds": 12.0, "produced": True,
    }
    assert health({"idle_seconds": 12.0})["produced"] is None, (
        "an older build with no cursor is unknowable, not un-drawn"
    )


# --- the threshold's ceiling and the tables (review of !234) --------------------

def test_the_threshold_ceiling_is_the_spools_own_bound():
    """A threshold above the spool's capacity can never be met, which would be a
    second, undocumented off-switch beside the interval's 0. The ceiling is copied
    into config.py because assistant.py imports it, so this is the pin that stops
    the copy drifting."""
    from lmer_platform import config as cfg

    assert cfg.MAX_NUDGE_PENDING_THRESHOLD == assistant.MAX_PENDING
    assert cfg.INT_SETTINGS["nudge_pending_threshold"].maximum == assistant.MAX_PENDING
    with pytest.raises(cfg.ConfigError) as excinfo:
        cfg.validate_int_setting(
            "nudge_pending_threshold", assistant.MAX_PENDING + 1
        )
    assert "nudge_after_seconds=0" in str(excinfo.value), (
        "the refusal has to name where the off-switch actually is"
    )
    assert cfg.validate_int_setting(
        "nudge_pending_threshold", assistant.MAX_PENDING
    ) == assistant.MAX_PENDING


def test_an_interval_has_no_ceiling_because_a_long_one_is_only_slow():
    from lmer_platform import config as cfg

    assert cfg.INT_SETTINGS["nudge_after_seconds"].maximum is None
    assert cfg.INT_SETTINGS["checkin_window_seconds"].maximum is None
    assert cfg.validate_int_setting("nudge_after_seconds", 86400) == 86400


def test_the_routes_accept_exactly_the_table_the_validator_indexes():
    """The one copy the generalisation left behind. A name accepted by the route
    but absent from the table reaches `validate_int_setting`, which indexes it —
    a KeyError where the route translates only ConfigError, i.e. a 500."""
    from lmer_platform import api
    from lmer_platform import config as cfg

    assert api._INT_SETTING_KEYS == tuple(sorted(cfg.INT_SETTINGS))


def test_every_integer_setting_names_its_unit():
    """The shared warn message says "the default of 3600 seconds"; before the unit
    was carried it said "3600" where the per-setting text had said "3600s"."""
    from lmer_platform import config as cfg

    for field, rule in cfg.INT_SETTINGS.items():
        assert rule.unit, field
        assert rule.singular_unit, field
        assert (rule.maximum is None) == (rule.ceiling_reason is None), field


def test_a_one_digest_default_warning_is_singular(caplog):
    from lmer_platform import config as cfg

    assert cfg._int_setting_value(0, field="nudge_pending_threshold") == 1
    assert "using the default of 1 digest instead" in caplog.text
    assert "1 digests" not in caplog.text


@pytest.mark.parametrize("corrupt", ["yesterday", "not a date", "soon"])
def test_an_unusable_stored_mark_does_not_defeat_the_in_memory_bound(
    detector, platform_root, monkeypatch, corrupt
):
    """`_window` ignores a stamp it cannot parse, so a stored mark that is garbage
    is no bound at all — and a bare lexical compare called it *newer* than the
    process's own ("not a date" sorts after a year), which would have left the
    window unbounded on a hand-edited file whose next write also fails.

    The failing write is half the subject, not scenery: with `mark_nudged`
    succeeding, the first nudge overwrites the corrupt value with a real stamp and
    every later tick compares something parseable — measured at 1 send with the
    fold reverted as well as with it, i.e. a test that would pass with the fix
    dropped.
    """
    monkeypatch.setattr(detect.assistant, "mark_nudged", lambda *_a, **_k: None)
    sender = _Sender()
    instance = detector(sender=sender)
    assistant.notify("a question opened")
    _age_spool(minutes=10)
    payload = store.read_json(assistant.state_path())
    payload["nudged_at"] = corrupt
    store.write_json(assistant.state_path(), payload)

    for _ in range(4):
        instance.nudge_once()

    assert len(sender.calls) == 1


def test_a_future_stored_mark_is_clamped_once_then_nudges_after_the_window(
    detector, platform_root
):
    """A valid future stamp otherwise wins the fold and suppresses every repeat
    until the skewed wall-clock time arrives. The correction must be durable:
    deriving ``now`` on every tick restarts the window forever, and a restarted
    daemon would otherwise keep the original future value.
    """
    sender = _Sender()
    instance = detector(sender=sender)
    assistant.notify("a question opened")
    _age_spool(minutes=10)
    payload = store.read_json(assistant.state_path())
    payload["nudged_at"] = iso(minutes=-10)
    store.write_json(assistant.state_path(), payload)

    instance.nudge_once()

    assert sender.calls == []
    corrected = assistant.read_state().nudged_at
    assert corrected is not None
    assert 0 <= store.age_seconds(corrected) < 5

    # Model the next tick after one complete interval, and model the daemon
    # restart that used to bypass the detector's in-memory fold entirely.
    payload = store.read_json(assistant.state_path())
    payload["nudged_at"] = iso(minutes=10)
    store.write_json(assistant.state_path(), payload)
    restarted = detector(sender=sender)

    restarted.nudge_once()

    assert len(sender.calls) == 1


def test_an_unwritable_future_mark_fails_open_to_one_nudge(
    detector, platform_root, monkeypatch
):
    """A failed correction cannot leave the future value as a permanent bound."""
    sender = _Sender()
    instance = detector(sender=sender)
    assistant.notify("a question opened")
    _age_spool(minutes=10)
    payload = store.read_json(assistant.state_path())
    payload["nudged_at"] = iso(minutes=-10)
    store.write_json(assistant.state_path(), payload)
    monkeypatch.setattr(assistant, "_write_state", lambda _state: False)

    instance.nudge_once()

    assert len(sender.calls) == 1


# Codex uses the same daemon tick, independently of the idle fallback.
def test_codex_wake_during_conversation_coalesces_and_ack_survives_restart(detector, monkeypatch):
    from unittest.mock import Mock
    assistant.notify('question waiting')
    assistant._write_state(replace(assistant.read_state(), session_id='assistant-1'))
    sender = Mock(return_value={'transport': 'codex-app-server', 'state': 'accepted', 'submit_confirmed': False})
    instance = detector(sender=sender, idle=0)
    native = {'runtime': {'model': 'gpt-6-astra', 'effort': 'high'}, 'receipts': []}
    health = Mock(return_value={'codex_delivery': native})
    monkeypatch.setattr(detect, 'quiet_health', health)
    instance._nudge_stage()
    assert sender.call_count == 1
    ident = sender.call_args.kwargs['delivery_id']
    assert assistant.read_state().codex_wake is None
    assert len(assistant.read_state().pending) == 1
    native['receipts'] = [{'delivery_id': ident, **sender.return_value}]
    assistant.notify('another digest')
    instance._nudge_stage()
    assert sender.call_count == 1
    native['receipts'][0].update(state='confirmed', submit_confirmed=True)
    instance._nudge_stage()
    assert assistant.read_state().codex_wake == ident
    assert len(assistant.read_state().pending) == 2
    before = health.call_count
    detector(sender=sender, idle=0)._nudge_stage()
    assert health.call_count == before and sender.call_count == 1
    assistant.take_pending()
    assistant.notify('new accumulation')
    instance._nudge_stage()
    assert sender.call_count == 2 and sender.call_args.kwargs['delivery_id'] != ident


def test_codex_wake_lost_response_uses_same_identity_and_original_text(detector, monkeypatch):
    from unittest.mock import Mock
    assistant.notify('pending')
    sender = Mock(side_effect=[SessionIOError('response lost'), {
        'transport': 'codex-app-server', 'state': 'accepted', 'submit_confirmed': False,
    }])
    instance = detector(sender=sender, idle=0)
    monkeypatch.setattr(detect, 'quiet_health', lambda _: {'codex_delivery': {'runtime': {'model': 'astra'}, 'receipts': []}})
    instance._nudge_stage()
    assistant.notify('burst changes count')
    instance._nudge_stage()
    assert sender.call_args_list[0] == sender.call_args_list[1]
    assert len(assistant.read_state().pending) == 2
    assert assistant.read_state().codex_wake is None


@pytest.mark.parametrize('health', [None, {}, {'codex_delivery': {'runtime': None}}])
def test_codex_wake_never_uses_legacy_or_unready_transport(detector, monkeypatch, health):
    from unittest.mock import Mock
    assistant.notify('pending')
    sender = Mock()
    instance = detector(sender=sender, idle=0)
    monkeypatch.setattr(detect, 'quiet_health', lambda _: health)
    instance._nudge_stage()
    sender.assert_not_called()


def test_codex_wake_ack_cannot_mark_a_new_accumulation(platform_root):
    assistant.notify('first')
    assistant._write_state(replace(assistant.read_state(), session_id='assistant-1'))
    sequence = assistant.read_state().pending_seq
    assistant.take_pending()
    assistant.notify('second')
    assert not assistant.mark_codex_wake('assistant-1', sequence, 'old')
    assert assistant.read_state().codex_wake is None
    assert len(assistant.read_state().pending) == 1


@pytest.fixture
def native_wake_adapter(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from lmer_cli.codex_delivery import CodexDelivery

    adapter = CodexDelivery(['codex'], notify=Mock())
    adapter.outbox = tmp_path / 'delivery.json'
    adapter._connect = Mock()
    adapter._disconnect = Mock()
    thread = {'id': 't', 'threadSource': 'user', 'model': 'gpt-6-astra',
              'reasoningEffort': 'high', 'turns': [{'id': 'turn', 'status': 'inProgress', 'items': []}]}
    starts = []

    def request(method, params):
        if method == 'thread/loaded/list':
            return {'data': ['t']}
        if method == 'thread/read':
            return {'thread': thread}
        assert method == 'turn/start'
        starts.append(params)
        return {'turn': {'id': 'turn'}}

    adapter._request = request
    adapter._thread()
    monkeypatch.setattr(detect, 'quiet_health', lambda _: {'codex_delivery': adapter.snapshot()})
    return adapter, thread, starts


def test_codex_idle_fallback_response_loss_and_restart_do_not_replay(detector, native_wake_adapter):
    import hashlib

    adapter, thread, starts = native_wake_adapter
    assistant.notify('pending question')
    _age_spool(minutes=10)
    state = assistant.read_state()
    ident = 'digest-' + hashlib.sha256(f'assistant-1:{state.pending_seq}'.encode()).hexdigest()[:32]
    assistant._write_state(replace(state, session_id='assistant-1', codex_wake=ident))

    def sender(session, text, **kwargs):
        adapter.deliver(text, client_id=kwargs.get('delivery_id'))
        raise SessionIOError('HTTP response lost after native acceptance')

    instance = detector(sender=sender)
    instance._nudge_stage()
    assert len(starts) == 1 and starts[0]['clientUserMessageId'].startswith('digest-idle-')
    assistant.notify('burst changes count')
    _age_spool(minutes=20)
    detector(sender=sender)._nudge_stage()
    assert len(starts) == 1
    assert assistant.read_state().nudged_at is None
    thread['turns'][0]['items'].append({'type': 'userMessage', 'clientId': starts[0]['clientUserMessageId'],
                                      'content': starts[0]['input']})
    adapter.refresh()
    detector(sender=sender)._nudge_stage()
    assert len(starts) == 1 and assistant.read_state().nudged_at is not None
    assert len(assistant.read_state().pending) == 2
    detector(sender=sender)._nudge_stage()
    assert len(starts) == 1


def test_codex_wake_recovers_only_proven_unsent_input_across_restart(detector, native_wake_adapter):
    adapter, thread, starts = native_wake_adapter
    assistant.notify('pending question')
    assistant._write_state(replace(assistant.read_state(), session_id='assistant-1'))

    def sender(session, text, **kwargs):
        return adapter.deliver(text, client_id=kwargs['delivery_id'])

    adapter._connect.side_effect = OSError('connection unavailable before send')
    detector(sender=sender, idle=0)._nudge_stage()
    assert starts == []
    adapter._connect.side_effect = None
    detector(sender=sender, idle=0)._nudge_stage()
    assert len(starts) == 1
    detector(sender=sender, idle=0)._nudge_stage()
    assert len(starts) == 1 and assistant.read_state().codex_wake is None
    thread['turns'][0]['items'].append({'type': 'userMessage', 'clientId': starts[0]['clientUserMessageId'],
                                      'content': starts[0]['input']})
    adapter.refresh()
    detector(sender=sender, idle=0)._nudge_stage()
    assert assistant.read_state().codex_wake == starts[0]['clientUserMessageId']
    assert len(starts) == 1 and len(assistant.read_state().pending) == 1


@pytest.mark.parametrize('idle', [0, 600])
def test_codex_uncertain_wake_is_blocked_across_restart(detector, native_wake_adapter, idle):
    import hashlib

    adapter, _, starts = native_wake_adapter
    assistant.notify('pending question')
    _age_spool(minutes=10)
    state = assistant.read_state()
    ident = 'digest-' + hashlib.sha256(f'assistant-1:{state.pending_seq}'.encode()).hexdigest()[:32]
    assistant._write_state(replace(state, session_id='assistant-1', codex_wake=ident if idle else None))
    request = adapter._request

    def lost_response(method, params):
        result = request(method, params)
        if method == 'turn/start':
            raise TimeoutError('native response lost')
        return result

    adapter._request = lost_response

    def sender(session, text, **kwargs):
        return adapter.deliver(text, client_id=kwargs.get('delivery_id'))

    for _ in range(3):
        detector(sender=sender, idle=idle)._nudge_stage()
    assert len(starts) == 1
    assert adapter.snapshot()['receipts'][0]['state'] == 'uncertain'
    assert assistant.read_state().nudged_at is None


@pytest.mark.parametrize('health', [None, {}, {'codex_delivery': {'runtime': None}}])
def test_known_codex_idle_fallback_holds_when_native_health_is_unavailable(detector, monkeypatch, health):
    from unittest.mock import Mock

    sender = Mock()
    instance = detector(sender=sender)
    assistant.notify('pending question')
    _age_spool(minutes=10)
    status = replace(assistant.status(), settings={'harness': 'codex'})
    monkeypatch.setattr(assistant, 'status', lambda: status)
    monkeypatch.setattr(detect, 'quiet_health', lambda _: health)
    instance._nudge_stage()
    sender.assert_not_called()
    assert assistant.read_state().nudged_at is None


@pytest.mark.parametrize('failure', ['accepted', 'http-loss', 'uncertain', 'rejected', 'unsent'])
def test_codex_overdue_idle_spool_has_one_outstanding_wake(detector, native_wake_adapter, failure):
    from lmer_cli.codex_delivery import CodexDeliveryError

    adapter, _, starts = native_wake_adapter
    assistant.notify('overdue question')
    _age_spool(minutes=20)
    assistant._write_state(replace(assistant.read_state(), session_id='assistant-1'))
    request = adapter._request

    def rpc(method, params):
        result = request(method, params)
        if method == 'turn/start':
            if failure == 'uncertain':
                raise TimeoutError('primary native response lost')
            if failure == 'rejected':
                raise CodexDeliveryError('primary native rejection')
        return result

    adapter._request = rpc
    if failure == 'unsent':
        adapter._connect.side_effect = OSError('primary connection unavailable')
    sends = []

    def sender(session, text, **kwargs):
        sends.append((text, kwargs))
        receipt = adapter.deliver(text, client_id=kwargs['delivery_id'])
        if failure == 'http-loss':
            raise SessionIOError('primary HTTP response lost')
        return receipt

    detector(sender=sender)._nudge_stage()
    assert len(starts) == (0 if failure == 'unsent' else 1)
    assert len(sends) == 1
    adapter._connect.side_effect = None
    assistant.notify('another digest in the same accumulation')
    detector(sender=sender)._nudge_stage()
    detector(sender=sender)._nudge_stage()
    assert len(starts) == 1
    assert all(s[1]['delivery_id'] == sends[0][1]['delivery_id'] for s in sends)
    assert len(assistant.read_state().pending) == 2
    assert assistant.read_state().codex_wake is None
    assert assistant.read_state().nudged_at is None


def test_codex_confirmed_primary_starts_interval_before_later_idle_reminder(detector, native_wake_adapter, monkeypatch):
    adapter, thread, starts = native_wake_adapter
    assistant.notify('overdue question')
    _age_spool(minutes=20)
    assistant._write_state(replace(assistant.read_state(), session_id='assistant-1'))

    def sender(session, text, **kwargs):
        return adapter.deliver(text, client_id=kwargs['delivery_id'])

    detector(sender=sender)._nudge_stage()
    assert len(starts) == 1
    thread['turns'][0]['items'].append({'type': 'userMessage', 'clientId': starts[0]['clientUserMessageId'],
                                      'content': starts[0]['input']})
    adapter.refresh()
    detector(sender=sender)._nudge_stage()
    state = assistant.read_state()
    assert state.codex_wake == starts[0]['clientUserMessageId'] and state.nudged_at
    detector(sender=sender)._nudge_stage()
    assert len(starts) == 1
    later = datetime.now(timezone.utc) + timedelta(seconds=AFTER + 1)
    age = nudge.age_seconds
    monkeypatch.setattr(nudge, 'age_seconds', lambda stamp, now=None: age(stamp, now=later))
    detector(sender=sender)._nudge_stage()
    assert len(starts) == 2 and starts[1]['clientUserMessageId'].startswith('digest-idle-')
    assert starts[0]['input'] != starts[1]['input']
    detector(sender=sender)._nudge_stage()
    assert len(starts) == 2 and len(assistant.read_state().pending) == 1


@pytest.mark.parametrize('failed_mark', ['primary', 'idle'])
@pytest.mark.parametrize('outcome', ['accepted', 'uncertain', 'rejected', 'unsent'])
def test_codex_mark_recovery_cannot_replace_outstanding_idle_identity(
    detector, native_wake_adapter, monkeypatch, failed_mark, outcome,
):
    from lmer_cli.codex_delivery import CodexDeliveryError

    adapter, thread, starts = native_wake_adapter
    clock = [datetime.now(timezone.utc)]
    stamp = lambda: clock[0].strftime('%Y-%m-%dT%H:%M:%SZ')
    age = store.age_seconds
    for module in (assistant, detect, nudge):
        monkeypatch.setattr(module, 'age_seconds', lambda value, now=None: age(value, now=clock[0]))
    for module in (assistant, detect):
        monkeypatch.setattr(module, 'utc_now_iso', stamp)
    assistant.notify('pending original')
    _age_spool(minutes=20)
    assistant._write_state(replace(assistant.read_state(), session_id='assistant-1'))
    sends = []

    def sender(session, text, **kwargs):
        sends.append((text, kwargs['delivery_id']))
        return adapter.deliver(text, client_id=kwargs['delivery_id'])

    def confirm_latest():
        thread['turns'][0]['items'].append({
            'type': 'userMessage', 'clientId': starts[-1]['clientUserMessageId'],
            'content': starts[-1]['input'],
        })
        adapter.refresh()

    instance = detector(sender=sender)
    instance._nudge_stage()
    confirm_latest()
    if failed_mark == 'idle':
        instance._nudge_stage()
        clock[0] += timedelta(seconds=AFTER + 1)
        instance._nudge_stage()
        confirm_latest()
    confirmed_count = len(starts)
    before_mark = assistant.read_state()
    writer = assistant._write_state
    monkeypatch.setattr(assistant, '_write_state', lambda state: False)
    instance._nudge_stage()
    assert assistant.read_state() == before_mark
    clock[0] += timedelta(seconds=AFTER + 1)
    instance._nudge_stage()
    assert len(starts) == confirmed_count, 'failed durable mark must not authorize another idle identity'
    detector(sender=sender)._nudge_stage()
    assert len(starts) == confirmed_count

    monkeypatch.setattr(assistant, '_write_state', writer)
    instance = detector(sender=sender)
    instance._nudge_stage()
    recovered = assistant.read_state()
    assert recovered.nudged_at == stamp()
    assert recovered.codex_wake == starts[0]['clientUserMessageId']
    assert len(starts) == confirmed_count
    clock[0] += timedelta(seconds=AFTER - 1)
    instance._nudge_stage()
    assert len(starts) == confirmed_count

    request = adapter._request

    def rpc(method, params):
        result = request(method, params)
        if method == 'turn/start':
            if outcome == 'uncertain':
                raise TimeoutError('idle native response lost')
            if outcome == 'rejected':
                raise CodexDeliveryError('idle native rejection')
        return result

    adapter._request = rpc
    if outcome == 'unsent':
        adapter._connect.side_effect = OSError('idle connection unavailable before submission')
    clock[0] += timedelta(seconds=2)
    instance._nudge_stage()
    original = sends[-1]
    assert original[1].startswith('digest-idle-')
    assert original[1] not in {p['clientUserMessageId'] for p in starts[:confirmed_count]}
    assert len(starts) == confirmed_count + (outcome != 'unsent')
    adapter._connect.side_effect = None
    assistant.notify('burst after pending idle')
    for _ in range(3):
        clock[0] += timedelta(seconds=AFTER + 1)
        detector(sender=sender)._nudge_stage()
    assert len(starts) == confirmed_count + 1
    assert (starts[-1]['input'][0]['text'], starts[-1]['clientUserMessageId']) == original
    receipt = next(r for r in adapter.snapshot()['receipts'] if r['delivery_id'] == original[1])
    assert receipt['state'] == ('accepted' if outcome == 'unsent' else outcome)
    assert not receipt['submit_confirmed'] and not receipt['retryable']
    assert assistant.read_state().nudged_at == recovered.nudged_at
    assert [item.note for item in assistant.read_state().pending] == ['pending original', 'burst after pending idle']


# --- back-off and the one operator signal (issue #584) -----------------------

class _Deliver:
    """A stand-in for ``stall.deliver``: every thread takes the post unless told."""

    def __init__(self, *, fail=False):
        self.calls = []
        self._fail = fail

    def __call__(self, text, threads):
        self.calls.append((text, list(threads)))
        if self._fail:
            return [], [{"thread": t, "error": "slack said no"} for t in threads]
        return list(threads), []


@pytest.fixture
def stalling(detector, monkeypatch):
    """A detector whose stall signal goes to one standing thread, recorded."""
    def _build(*, threads=("https://example.slack.com/archives/C1/p1",),
               reason="Login expired", deliver=None, **kwargs):
        posted = deliver or _Deliver()
        monkeypatch.setattr(detect.stall, "deliver", posted)
        monkeypatch.setattr(detect.stall, "terminal_reason", lambda session_id: reason)
        monkeypatch.setattr(detect.assistant, "_stored_threads", lambda: list(threads))
        return detector(**kwargs), posted

    return _build


def _age_nudge(instance, **delta):
    """Move the last nudge one window into the past, durably and in memory."""
    payload = store.read_json(assistant.state_path()) or {}
    if payload.get("nudged_at"):
        payload["nudged_at"] = iso(**delta)
        store.write_json(assistant.state_path(), payload)
    if instance._nudged is not None:
        instance._nudged = (instance._nudged[0], iso(**delta))


def _ignored_windows(instance, count):
    for _ in range(count):
        instance.nudge_once()
        _age_nudge(instance, minutes=10)


def _as_session(monkeypatch, session_id):
    real = detect.assistant.status
    monkeypatch.setattr(detect.assistant, "status",
                        lambda: replace(real(), session_id=session_id))


def test_three_unanswered_nudges_end_typing_and_tell_the_operator_once(
    stalling, platform_root
):
    sender = _Sender()
    instance, posted = stalling(sender=sender)
    assistant.notify("a question opened")
    _age_spool(minutes=30)

    _ignored_windows(instance, nudge.MAX_UNANSWERED_NUDGES)
    assert len(sender.calls) == nudge.MAX_UNANSWERED_NUDGES
    assert posted.calls == []

    _ignored_windows(instance, 5)

    assert len(sender.calls) == nudge.MAX_UNANSWERED_NUDGES, "no more typing"
    assert len(posted.calls) == 1, "once per episode, not once per interval"
    text, threads = posted.calls[0]
    assert threads == ["https://example.slack.com/archives/C1/p1"]
    assert "Its terminal shows `Login expired`." in text
    assert "3 reminders" in text
    assert assistant.read_state().stalled_at is not None
    assert instance.stalled == 1


def test_the_third_nudge_is_only_unanswered_after_its_own_window(
    stalling, platform_root
):
    instance, posted = stalling()
    assistant.notify("a question opened")
    _age_spool(minutes=30)
    _ignored_windows(instance, nudge.MAX_UNANSWERED_NUDGES - 1)

    instance.nudge_once()
    instance.nudge_once()

    assert posted.calls == []


def test_a_working_assistant_is_not_reported(stalling, platform_root):
    instance, posted = stalling()
    assistant.notify("a question opened")
    _age_spool(minutes=30)
    _ignored_windows(instance, nudge.MAX_UNANSWERED_NUDGES)

    busy, posted = stalling(idle=5.0)
    busy.nudge_once()

    assert posted.calls == []


def test_a_take_ends_the_episode(stalling, platform_root):
    sender = _Sender()
    instance, posted = stalling(sender=sender)
    assistant.notify("a question opened")
    _age_spool(minutes=30)
    _ignored_windows(instance, nudge.MAX_UNANSWERED_NUDGES + 1)
    assert len(posted.calls) == 1

    assistant.take_pending()
    cleared = assistant.read_state()
    assert (cleared.nudges, cleared.nudge_session, cleared.stalled_at) == (0, None, None)
    assistant.notify("a new backlog")
    _age_spool(minutes=40)
    _ignored_windows(instance, nudge.MAX_UNANSWERED_NUDGES + 1)

    assert len(sender.calls) == 2 * nudge.MAX_UNANSWERED_NUDGES
    assert len(posted.calls) == 2


def test_a_new_incarnation_starts_a_new_episode(stalling, platform_root, monkeypatch):
    sender = _Sender()
    instance, posted = stalling(sender=sender)
    assistant.notify("a question opened")
    _age_spool(minutes=30)
    _ignored_windows(instance, nudge.MAX_UNANSWERED_NUDGES + 1)
    assert len(posted.calls) == 1

    _as_session(monkeypatch, "assistant-2")
    instance.nudge_once()

    assert len(sender.calls) == nudge.MAX_UNANSWERED_NUDGES + 1
    assert sender.calls[-1][0] == "assistant-2"
    state_ = assistant.read_state()
    assert (state_.nudges, state_.nudge_session, state_.stalled_at) == (1, "assistant-2", None)


def test_an_unwritable_state_file_cannot_turn_the_signal_into_a_flood(
    stalling, platform_root, monkeypatch
):
    monkeypatch.setattr(detect.assistant, "mark_nudged", lambda *_a, **_k: None)
    monkeypatch.setattr(detect.assistant, "mark_stalled", lambda *_a, **_k: None)
    sender = _Sender()
    instance, posted = stalling(sender=sender)
    assistant.notify("a question opened")
    _age_spool(minutes=30)

    _ignored_windows(instance, 10)

    assert len(sender.calls) == nudge.MAX_UNANSWERED_NUDGES
    assert len(posted.calls) == 1


def test_a_signal_no_thread_took_is_retried_a_window_later(stalling, platform_root):
    instance, posted = stalling(deliver=_Deliver(fail=True))
    assistant.notify("a question opened")
    _age_spool(minutes=30)
    _ignored_windows(instance, nudge.MAX_UNANSWERED_NUDGES)

    instance.nudge_once()
    instance.nudge_once()
    assert len(posted.calls) == 1, "a refused post waits a window, not a tick"
    assert assistant.read_state().stalled_at is None

    _age_nudge(instance, minutes=10)
    instance.nudge_once()
    assert len(posted.calls) == 2


def test_with_no_standing_thread_the_event_is_the_signal(stalling, platform_root):
    instance, posted = stalling(threads=(), reason=None)
    assistant.notify("a question opened")
    _age_spool(minutes=30)

    _ignored_windows(instance, nudge.MAX_UNANSWERED_NUDGES + 3)

    assert [threads for _text, threads in posted.calls] == [[]]
    events = [
        json.loads(line)["data"] for line in store.events_path().read_text().splitlines()
        if detect.ASSISTANT_STALLED in line
    ]
    assert len(events) == 1
    assert events[0]["signalled"] is True
    assert events[0]["threads"] == [] and events[0]["reason"] is None
    assert events[0]["nudges"] == nudge.MAX_UNANSWERED_NUDGES


def test_the_episode_survives_a_reread_of_the_state_file(platform_root):
    assistant.notify("a question opened")
    assistant.mark_nudged("assistant-1")
    assistant.mark_nudged("assistant-1")
    assistant.mark_stalled("assistant-1")

    state_ = assistant.read_state()

    assert state_.episode_nudges("assistant-1") == 2
    assert state_.episode_stalled("assistant-1")
    assert state_.episode_nudges("assistant-2") == 0
    assert not state_.episode_stalled("assistant-2")


def test_due_reports_the_back_off_and_then_nothing():
    spool = replace(state(oldest=iso(minutes=30)), nudges=3, nudge_session="s1")

    assert nudge.due(spool, running=True, idle_seconds=600, after_seconds=AFTER,
                     pending_threshold=1, session_id="s1").backed_off
    assert not nudge.due(spool, running=True, idle_seconds=600, after_seconds=AFTER,
                         pending_threshold=1, session_id="s2").backed_off
    stalled = replace(spool, stalled_at=iso(minutes=1))
    assert nudge.due(stalled, running=True, idle_seconds=600, after_seconds=AFTER,
                     pending_threshold=1, session_id="s1") is None


class _TakingDeliver(_Deliver):
    """The operator re-authenticates and the assistant takes while the post is out."""

    def __call__(self, text, threads):
        assistant.take_pending()
        assistant.notify("a new backlog")
        return super().__call__(text, threads)


def test_a_take_during_the_stall_post_does_not_silence_the_next_backlog(
    stalling, platform_root
):
    sender = _Sender()
    instance, posted = stalling(sender=sender, deliver=_TakingDeliver())
    assistant.notify("a question opened")
    _age_spool(minutes=30)
    _ignored_windows(instance, nudge.MAX_UNANSWERED_NUDGES + 1)
    assert len(posted.calls) == 1
    assert assistant.read_state().stalled_at is None

    _age_spool(minutes=40)
    _ignored_windows(instance, nudge.MAX_UNANSWERED_NUDGES + 1)

    assert len(sender.calls) == 2 * nudge.MAX_UNANSWERED_NUDGES
    assert len(posted.calls) == 2


def test_a_take_during_the_nudge_send_does_not_carry_the_count_over(
    stalling, platform_root
):
    class _TakingSender(_Sender):
        def __call__(self, session_id, data, *, append_newline=False):
            assistant.take_pending()
            assistant.notify("a new backlog")
            return super().__call__(session_id, data, append_newline=append_newline)

    instance, _posted = stalling(sender=_TakingSender())
    assistant.notify("a question opened")
    _age_spool(minutes=30)

    instance.nudge_once()

    state_ = assistant.read_state()
    assert (state_.nudges, state_.nudge_session) == (0, None)


@pytest.mark.parametrize("failing", ["mark_nudged", "mark_stalled"])
def test_a_raising_state_store_cannot_turn_the_signal_into_a_flood(
    stalling, platform_root, monkeypatch, failing
):
    """The projects store raises sqlite errors where the file store returns False."""
    def refuse(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(detect.assistant, failing, refuse)
    sender = _Sender()
    instance, posted = stalling(sender=sender)
    assistant.notify("a question opened")
    _age_spool(minutes=30)

    _ignored_windows(instance, 10)

    assert len(sender.calls) == nudge.MAX_UNANSWERED_NUDGES
    assert len(posted.calls) == 1
