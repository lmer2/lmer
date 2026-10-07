"""The context-fullness reminder: the platform recommends a rotation (#446).

The daemon holds a threshold the assistant cannot hold for itself, reads the
figure its harness publishes, and types one line. Everything about it is a
boundary, so this suite is mostly boundaries — and in both directions, because a
reminder that never fires and one that fires every tick are the same bug from the
chat window.

What is pinned, in the order it matters:

- **The issue's own acceptance**: threshold 70 with a reading of 71 produces one
  line; 69 produces none; 0 produces none at any reading.
- **Absent is not zero.** A harness that publishes no context figure is reported
  once and never alerted on, and a reading too old to date is treated the same way
  rather than as a low one.
- **The rate limit is a window, not a one-shot**, and falling back below the
  threshold ends the stay — so the *next* crossing is announced immediately.
- **Nothing here rotates anything.** The reminder recommends; the taskdef carries
  the rule; the assistant decides.
- **Nothing about it can cost the daemon its tick**: a raising send is absorbed
  and the diff still comes back.

Time moves by passing ``now`` and by writing stamps; nothing here sleeps.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lmer_platform import assistant, detect, rotation, store
from lmer_platform.config import PlatformConfig
from lmer_platform.session_io import SessionIOError

THRESHOLD = 70


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


def iso(**delta):
    """An ISO-Z stamp *delta* in the past."""
    when = datetime.now(timezone.utc) - timedelta(**delta)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def decide(percent, *, threshold=THRESHOLD, age=5.0, alerted_at=None, now=None):
    return rotation.due(
        threshold=threshold,
        percent=percent,
        reading_age=age,
        alerted_at=alerted_at,
        now=now,
    )


# --- the conditions -----------------------------------------------------------

def test_a_reading_past_the_threshold_is_due():
    """The issue's first acceptance line: threshold 70, reading 71."""
    alert = decide(71)
    assert alert is not None
    assert (alert.percent, alert.threshold) == (71, 70)
    assert alert.repeat is False


def test_a_reading_under_the_threshold_is_not_due():
    """And its second: 69 against the same threshold says nothing."""
    assert decide(69) is None


def test_a_reading_exactly_at_the_threshold_is_due():
    """The operator set the number they want to hear about, so reaching it is
    reaching it. The acceptance leaves 70 unstated and this is the reading that
    keeps 69/71 from being the only two numbers anyone ever thought about."""
    assert decide(70) is not None


def test_a_threshold_of_zero_is_the_off_switch():
    for percent in (0, 50, 99, 100):
        assert decide(percent, threshold=0) is None


def test_a_negative_threshold_cannot_turn_the_reminder_on():
    assert decide(100, threshold=-10) is None


def test_an_unpublished_reading_is_never_an_alert():
    """`None` is "this harness publishes no context figure", which is a fact
    about the host rather than a context window at 0 percent."""
    assert decide(None) is None
    assert decide(None, threshold=1) is None


def test_a_reading_too_old_to_date_is_treated_as_no_reading():
    """A wedged publisher goes on reporting what it last wrote. That number may
    even be true — the window only grows — but nothing dates it, and ending a
    context window is not a decision to take on undated evidence."""
    assert decide(95, age=rotation.MAX_READING_AGE_SECONDS + 1) is None
    assert decide(95, age=rotation.MAX_READING_AGE_SECONDS) is not None


def test_a_reading_of_unknown_age_still_alerts():
    """An age the platform could not compute is the statusline's own `None`. It
    proceeds, for the reason the nudge proceeds on an unknown idle reading: the
    hosts least able to notice must not be the ones where the safety net is off."""
    assert decide(95, age=None) is not None


# --- the window ---------------------------------------------------------------

def test_a_recent_reminder_holds_the_next_one():
    assert decide(90, alerted_at=iso(minutes=5)) is None


def test_a_reminder_an_interval_old_is_repeated():
    alert = decide(90, alerted_at=iso(minutes=20))
    assert alert is not None
    assert alert.repeat is True, "the log line has to be able to say so"


def test_the_window_is_the_constant_it_documents():
    """Pinned against the number, not against a re-derivation of it: the docs and
    the taskdef both quote fifteen minutes."""
    assert rotation.REMINDER_INTERVAL_SECONDS == 900
    assert decide(90, alerted_at=iso(seconds=899)) is None
    assert decide(90, alerted_at=iso(seconds=901)) is not None


def test_an_unreadable_stamp_costs_one_repeat_not_every_future_one():
    assert decide(90, alerted_at="not a stamp") is not None


def test_a_future_stamp_does_not_silence_the_reminder():
    """Clock correction must not be able to turn this off indefinitely."""
    assert decide(90, alerted_at=iso(minutes=-60)) is not None


def test_deciding_writes_nothing(platform_root):
    """The mark, the event and the send are the caller's, which is what lets every
    boundary above be tested without a container or a daemon."""
    decide(99)
    decide(99, alerted_at=iso(minutes=30))
    assert not platform_root.exists()


# --- the sentence -------------------------------------------------------------

def test_the_prompt_is_one_paragraph_with_no_newline():
    text = rotation.prompt(decide(80))
    assert "\n" not in text
    assert len(text) <= rotation.MAX_PROMPT_CHARS


def test_the_prompt_says_who_is_talking_and_carries_both_numbers():
    text = rotation.prompt(decide(81))
    assert text.startswith("[lmer platform]")
    assert "81 percent" in text
    assert "70 percent" in text
    assert "not the operator" in text


def test_the_prompt_says_nothing_has_been_rotated():
    """The line most likely to be misread as "you have been rotated" or as the
    operator asking for one now. Both readings are answered in the text."""
    text = rotation.prompt(decide(90))
    assert "nothing has been rotated" in text
    assert "next clean breakpoint" in text


def test_the_prompt_names_the_breakpoint_rule_it_expects():
    text = rotation.prompt(decide(90))
    for clause in ("no question of yours open", "no digest taken",
                   "half-finished brief", "mid-decision"):
        assert clause in text


def test_a_repeat_reads_the_same_as_a_first_reminder():
    """An assistant that never registered the first one must not be handed a
    sentence written as though it had."""
    first = rotation.prompt(decide(90))
    again = rotation.prompt(decide(90, alerted_at=iso(minutes=30)))
    assert first == again


# --- the detector's stage -----------------------------------------------------

class _Sender:
    """A stand-in for ``session_io.send_input``, recording what it was given."""

    def __init__(self, *, raises=None):
        self.calls = []
        self._raises = raises

    def __call__(self, session_id, data, *, append_newline=False, **kwargs):
        self.calls.append(
            (session_id, data, append_newline, kwargs.get("delivery_id"))
        )
        if self._raises is not None:
            raise self._raises
        return {"bytes_written": 1, "submit_confirmed": True}


class _Setting:
    def __init__(self, value):
        self.value = value


@pytest.fixture
def detector(platform_root, monkeypatch):
    """A detector whose fleet read, assistant and status line are stubbed out.

    ``percent`` of ``None`` is the harness that publishes no context block at
    all, which is a different state from a document carrying one — and the
    difference is what the unreadable notice turns on.
    """
    def _build(*, running=True, percent=80, age=5.0, session_id="assistant-1",
               threshold=THRESHOLD, sender=None, generation=3, started="1h",
               document=True, reading_age=None):
        published = None
        if running and document:
            published = {"harness": "claude", "updated": iso(seconds=5),
                         "age_seconds": age}
            if percent is not None:
                published["context"] = {"percent": percent}
                if reading_age is not None:
                    # A codex status line dates its own reading, apart from the
                    # document that carries it.
                    published["context"]["age_seconds"] = reading_age
        monkeypatch.setattr(
            detect.assistant, "status",
            lambda: assistant.AssistantStatus(
                running=running,
                session_id=session_id if running else None,
                pid=1234 if running else None,
                started_at=(
                    iso(hours=1) if started == "1h" else iso(seconds=30)
                ),
                generation=generation,
                stale=False,
                tracked=True,
                pending=0,
                handoff=None,
                statusline=published,
            ),
        )
        monkeypatch.setattr(
            detect, "rotation_settings",
            lambda: {"context_threshold": _Setting(threshold)},
        )
        return detect.Detector(
            PlatformConfig(),
            state_reader=lambda config, **kwargs: {"runs": []},
            sender=sender or _Sender(),
        )

    return _build


def events(platform_root):
    path = platform_root / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_a_reading_past_the_threshold_is_typed_into_the_session(
    detector, platform_root
):
    sender = _Sender()
    instance = detector(percent=71, sender=sender)

    assert instance.rotation_once() is not None

    assert len(sender.calls) == 1
    session_id, data, append_newline, _ = sender.calls[0]
    assert session_id == "assistant-1"
    assert data.startswith("[lmer platform] ")
    assert "71 percent" in data
    assert append_newline is True, (
        "without Enter the reminder sits unsent in the agent's input box"
    )
    assert instance.recommended == 1


def test_a_reading_under_the_threshold_types_nothing(detector, platform_root):
    sender = _Sender()
    instance = detector(percent=69, sender=sender)

    assert instance.rotation_once() is None
    assert sender.calls == []


def test_a_threshold_of_zero_types_nothing_at_any_reading(
    detector, platform_root
):
    sender = _Sender()
    instance = detector(percent=99, threshold=0, sender=sender)

    assert instance.rotation_once() is None
    assert sender.calls == []
    assert events(platform_root) == [], (
        "an off feature records nothing, including the unreadable notice"
    )


def test_nothing_is_typed_when_no_assistant_is_running(detector, platform_root):
    sender = _Sender()
    instance = detector(running=False, sender=sender)

    assert instance.rotation_once() is None
    assert sender.calls == []


def test_one_reminder_per_window_not_one_per_tick(detector, platform_root):
    sender = _Sender()
    instance = detector(percent=90, sender=sender)

    assert instance.rotation_once() is not None
    assert instance.rotation_once() is None
    assert len(sender.calls) == 1


def test_falling_back_below_the_threshold_re_arms_the_next_crossing(
    detector, platform_root, monkeypatch
):
    """One line per *crossing*: an assistant that compacted its window back down
    and filled it again is in a new state, and waiting out the window there would
    hold the reminder for the one case it was asked for."""
    sender = _Sender()
    instance = detector(percent=90, sender=sender)
    assert instance.rotation_once() is not None

    below = detector(percent=20, sender=sender)
    below._rotation = instance._rotation
    assert below.rotation_once() is None
    assert below._rotation is None

    again = detector(percent=91, sender=sender)
    again._rotation = below._rotation
    assert again.rotation_once() is not None
    assert len(sender.calls) == 2


def test_a_successor_is_never_alerted_on_its_predecessors_reading(
    detector, platform_root
):
    """The stamp is keyed on the session it was typed into, so a rotation does
    not inherit a window — and the status line it reads belongs to the new
    session id, which is a directory that does not exist yet."""
    sender = _Sender()
    instance = detector(percent=95, sender=sender)
    assert instance.rotation_once() is not None

    successor = detector(percent=95, session_id="assistant-2", sender=sender)
    successor._rotation = instance._rotation
    assert successor.rotation_once() is not None, (
        "a fresh incarnation above the threshold is a fresh crossing"
    )


def test_a_harness_publishing_no_context_figure_is_reported_once(
    detector, platform_root, caplog
):
    sender = _Sender()
    instance = detector(percent=None, sender=sender)

    assert instance.rotation_once() is None
    assert instance.rotation_once() is None
    assert sender.calls == []

    notices = [
        event for event in events(platform_root)
        if event.get("type") == detect.ASSISTANT_ROTATION_UNREADABLE
    ]
    assert len(notices) == 1, "once, not once per tick"
    assert notices[0]["data"]["threshold"] == THRESHOLD
    assert notices[0]["data"]["status_strip_off"] is False


def test_the_notice_names_the_status_strip_switch_when_it_is_off(
    detector, platform_root, caplog, monkeypatch
):
    """#507: the switch removes the document the threshold reads, so the
    notice must not blame a harness for publishing nothing."""
    from lmer_platform import statusline

    monkeypatch.setenv(statusline.ENABLED_ENV, "0")
    instance = detector(percent=None)
    with caplog.at_level("WARNING"):
        assert instance.rotation_once() is None
    notices = [
        event for event in events(platform_root)
        if event.get("type") == detect.ASSISTANT_ROTATION_UNREADABLE
    ]
    assert notices[0]["data"]["status_strip_off"] is True
    assert statusline.ENABLED_ENV in caplog.text


def test_a_figure_that_starts_arriving_re_arms_the_notice(
    detector, platform_root
):
    """A publisher that was fixed — or a harness swapped in — must not stay
    silenced for the life of the incarnation by a notice about the old one."""
    instance = detector(percent=None)
    assert instance.rotation_once() is None

    reading = detector(percent=30)
    reading._context_unreadable = instance._context_unreadable
    assert reading.rotation_once() is None
    assert reading._context_unreadable is None


def test_a_stale_document_types_nothing(detector, platform_root):
    sender = _Sender()
    instance = detector(
        percent=99, age=rotation.MAX_READING_AGE_SECONDS + 60, sender=sender
    )

    assert instance.rotation_once() is None
    assert sender.calls == []


def test_a_reminder_is_recorded_in_platform_history(detector, platform_root):
    instance = detector(percent=88, generation=7)
    instance.rotation_once()

    recorded = [
        event for event in events(platform_root)
        if event.get("type") == detect.ASSISTANT_ROTATION_RECOMMENDED
    ]
    assert len(recorded) == 1
    data = recorded[0]["data"]
    assert data["session"] == "assistant-1"
    assert data["generation"] == 7
    assert (data["percent"], data["threshold"]) == (88, THRESHOLD)
    assert data["repeat"] is False
    assert data["delivery_id"].startswith("rotation-")


def test_the_event_records_the_age_the_decision_was_taken_on(
    detector, platform_root
):
    """An audit record of a recommendation has to name the evidence that produced
    it. On codex the document's age and the reading's are two clocks, and the
    decision runs on the second — so recording the first describes a freshness
    nothing was measured against."""
    instance = detector(percent=88, age=2.0, reading_age=500.0)
    instance.rotation_once()

    data = [
        event for event in events(platform_root)
        if event.get("type") == detect.ASSISTANT_ROTATION_RECOMMENDED
    ][0]["data"]
    assert data["reading_age_seconds"] == 500.0, (
        "the event reported the wrapper document's age, not the reading's"
    )


def test_a_refused_send_leaves_no_mark_and_earns_its_retry(
    detector, platform_root
):
    sender = _Sender(raises=SessionIOError("control plane refused"))
    instance = detector(percent=90, sender=sender)

    assert instance.rotation_once() is None
    assert instance._rotation is None
    assert instance.recommended == 0
    assert instance.failures == 1, "absorbed, and counted as a health fact"


def test_a_send_that_raised_after_delivering_is_still_bounded(
    detector, platform_root
):
    """Repeating a reminder that was typed is the failure the bound exists to
    prevent, so delivery is the condition rather than a clean return."""
    error = SessionIOError("acknowledged different bytes")
    error.delivered = True
    instance = detector(percent=90, sender=_Sender(raises=error))

    assert instance.rotation_once() is not None
    assert instance._rotation is not None


def test_the_stage_runs_off_the_tick_and_cannot_cost_it(
    detector, platform_root, monkeypatch
):
    sender = _Sender()
    instance = detector(percent=95, sender=sender)
    # A baseline has to exist first, or the tick takes its establishing exit —
    # which is one of the three the stage is required to run on anyway.
    instance.tick_once()
    assert len(sender.calls) == 1

    monkeypatch.setattr(
        instance, "rotation_once",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert instance.tick_once() == []
    assert instance.failures >= 1


def test_the_notice_says_the_reminder_is_on_and_at_what_number(detector):
    instance = detector(threshold=65)
    assert "65% of its context window used" in instance.notice
    assert "15m" in instance.notice


def test_the_notice_says_so_when_the_reminder_is_off(detector):
    instance = detector(threshold=0)
    assert "context-fullness reminders are OFF" in instance.notice
    assert "rotation_context_threshold is 0" in instance.notice


# --- the taskdef carries the rule (issue #446) ---------------------------------

def taskdef_text():
    return (
        Path(__file__).resolve().parents[1]
        / "taskdef" / "orchestrate" / "instructions.txt"
    ).read_text()


def test_the_taskdef_tells_the_assistant_the_reminder_exists():
    """The sender and the prose ship together, for the reason the nudge's own
    guard gives: a line the assistant cannot attribute reads as the operator
    asking for a rotation now."""
    text = taskdef_text()
    assert "[lmer platform] Your context window is at" in text
    assert "not the operator" in text
    assert "rotation` group" in text or "`rotation`" in text


def test_the_taskdef_says_what_a_breakpoint_is():
    """The issue's item 3, item by item: the platform can recommend a rotation but
    only the assistant knows whether it is in the middle of something."""
    text = taskdef_text()
    for clause in ("No question of yours is open",
                   "No digest is taken and unacted-on",
                   "No brief is half-finished",
                   "Nothing is mid-decision"):
        assert clause in text


def test_the_taskdef_says_to_tell_the_operator_before_it_goes():
    text = taskdef_text()
    assert "tell the operator in **one line**" in text
    assert "not a way out of work" in text


def test_the_taskdef_names_the_verb_and_its_one_surprise():
    text = taskdef_text()
    assert "lmer-ctl rotate" in text
    assert "does not return" in text


def test_the_taskdef_says_nothing_rotates_without_the_assistant():
    """The reading drives a recommendation and never an action, and the session
    being recommended to is the one that has to know it."""
    text = taskdef_text()
    assert "Nothing has been rotated, and nothing will be without you" in text


# --- the operator docs carry it too (issue #446) -------------------------------

def quickstart_text():
    return (
        Path(__file__).resolve().parents[1]
        / "docs" / "PLATFORM-QUICKSTART.md"
    ).read_text()


def test_the_operator_docs_carry_the_conditions_and_the_constants():
    """The two numbers a reader would otherwise have to find in the source, and
    the one sentence that keeps the feature's promise readable: it recommends."""
    text = quickstart_text()
    assert "## Context-fullness reminders" in text
    assert "rotation.context_threshold" in text
    assert f"not older than {rotation.MAX_READING_AGE_SECONDS // 60} minutes" in text
    assert f"{rotation.REMINDER_INTERVAL_SECONDS // 60} minutes" in text
    assert "Nothing here rotates anything" in text


def test_the_operator_docs_say_the_rotate_call_does_not_return():
    text = quickstart_text()
    assert "does not return" in text
    assert "Who may rotate is unchanged" in text


def test_the_env_var_is_documented_where_every_other_one_is():
    doc = (
        Path(__file__).resolve().parents[1] / "docs" / "LMER-CLI.md"
    ).read_text()
    assert "LMER_PLATFORM_ROTATION_CONTEXT_THRESHOLD" in doc


# --- review of the branch: the two cases the first cut got wrong ---------------
#
# Both are the same shape — a fact read off the wrong place — and both are only
# reachable on a codex assistant, which is why neither showed up in the
# claude-shaped fixtures above.

@pytest.fixture
def native_adapter(tmp_path, monkeypatch):
    """A **real** `CodexDelivery` with only its JSON-RPC transport stubbed.

    The adapter's own contract is what these tests are about — it keys receipts
    on the delivery id and refuses a reused id carrying different text — so a
    fake sender returning a hand-written receipt cannot exercise them.
    `starts` counts native submissions, which is the only evidence that a
    reminder was actually handed to the model.
    """
    from unittest.mock import Mock
    from lmer_cli.codex_delivery import CodexDelivery

    adapter = CodexDelivery(["codex"], notify=Mock())
    adapter.outbox = tmp_path / "delivery.json"
    adapter._connect = Mock()
    adapter._disconnect = Mock()
    thread = {"id": "t", "threadSource": "user", "model": "gpt-6-astra",
              "reasoningEffort": "high",
              "turns": [{"id": "turn", "status": "inProgress", "items": []}]}
    starts = []

    def request(method, params):
        if method == "thread/loaded/list":
            return {"data": ["t"]}
        if method == "thread/read":
            return {"thread": thread}
        assert method == "turn/start"
        starts.append(params)
        return {"turn": {"id": "turn"}}

    adapter._request = request
    adapter._thread()

    class Refusal:
        """The **retryable** rejection: the adapter could not connect, so nothing
        was submitted and it is free to replay the same id later. A failure
        *during* `turn/start` is a different animal — `retryable` is false there
        and the adapter deliberately never replays it."""

        def __enter__(self):
            adapter._connect.side_effect = OSError("backend unavailable")

        def __exit__(self, *exc):
            adapter._connect.side_effect = None
            return False

    refuse = Refusal()
    monkeypatch.setattr(
        detect, "quiet_health", lambda _: {"codex_delivery": adapter.snapshot()}
    )

    def confirm():
        """What the model doing the turn looks like from the adapter's side."""
        for params in starts:
            thread["turns"][0]["items"].append({
                "type": "userMessage",
                "clientId": params["clientUserMessageId"],
                "content": params["input"],
            })
        adapter.refresh()

    return adapter, starts, refuse, confirm


def native_sender(adapter):
    return lambda session, text, **kwargs: adapter.deliver(
        text, client_id=kwargs["delivery_id"]
    )


def test_a_refusal_then_a_moved_reading_still_reaches_the_model(
    detector, platform_root, native_adapter
):
    """Round 1's confirmed finding, reproduced through the real adapter.

    The id was derived from the window and the prose re-rendered from the current
    percentage, so a retryable rejection at 80 followed by recovery at 81 offered
    the adapter the same id carrying different text — which it refuses outright
    ("Codex delivery ID was reused with different text"). Nothing was ever
    submitted, and since no stamp advances, every later tick repeated it.

    One attempt is now one identity and one frozen prose, so the retry is a
    retry: same id, same bytes, and it lands.
    """
    adapter, starts, refuse, _ = native_adapter
    sender = native_sender(adapter)

    instance = detector(percent=80, sender=sender)
    with refuse:
        assert instance.rotation_once() is None
    assert starts == [], "nothing was submitted, which is what makes it retryable"
    held = instance._rotation_attempt
    assert held is not None, "a refused attempt has to survive for the retry"

    # The reading moves under the retry — the case that used to raise.
    moved = detector(percent=81, sender=sender)
    moved._rotation_attempt = held
    assert moved.rotation_once() is None, "accepted, and not yet confirmed"

    assert len(starts) == 1, "the recovery never submitted anything"
    assert starts[0]["clientUserMessageId"] == held[2]
    assert "80 percent" in starts[0]["input"][0]["text"], (
        "the retry rewrote the prose its id was already bound to — which is the "
        "refusal this test exists for; a reminder reports the reading that "
        "triggered it, not the one it was retried under"
    )


def test_a_recrossing_submits_again_under_a_new_identity(
    detector, platform_root, native_adapter
):
    """Round 1's second reproduction: 80 → 69 → 80 rebuilt the first id, so the
    adapter answered from the old receipt, `recommended` reached 2 and only one
    reminder had ever been submitted."""
    adapter, starts, _, confirm = native_adapter
    sender = native_sender(adapter)

    first = detector(percent=80, sender=sender)
    assert first.rotation_once() is None
    confirm()
    again = detector(percent=80, sender=sender)
    again._rotation_attempt = first._rotation_attempt
    assert again.rotation_once() is not None, "confirmation is what marks it"
    assert again.recommended == 1 and len(starts) == 1

    below = detector(percent=69, sender=sender)
    below._rotation, below._rotation_attempt = again._rotation, again._rotation_attempt
    assert below.rotation_once() is None
    assert below._rotation_attempt is None, "the crossing ended; so does the attempt"

    back = detector(percent=80, sender=sender)
    back._rotation, back._rotation_attempt = below._rotation, below._rotation_attempt
    assert back.rotation_once() is None
    assert len(starts) == 2, "the re-crossing was answered from the old receipt"
    assert starts[1]["clientUserMessageId"] != starts[0]["clientUserMessageId"]


def test_an_identity_is_never_recreated_from_scratch_after_a_restart(
    detector, platform_root, native_adapter
):
    """A restart loses the attempt's prose, so an id it could rebuild would be an
    id it cannot honour. Fresh identities are random for exactly that reason."""
    adapter, starts, _, _ = native_adapter
    sender = native_sender(adapter)

    first = detector(percent=80, sender=sender)
    assert first.rotation_once() is None
    restarted = detector(percent=80, sender=sender)
    assert restarted._rotation_attempt is None
    assert restarted.rotation_once() is None

    assert len(starts) == 2
    assert starts[1]["clientUserMessageId"] != starts[0]["clientUserMessageId"]


def test_an_accepted_reminder_is_not_marked_until_the_model_takes_it(
    detector, platform_root, native_adapter
):
    """`accepted` is a turn that was started, not one the model has taken. Marking
    there would close the window over a submission whose outcome is unknown, and
    re-sending would put the line in twice — so the attempt waits, silently."""
    adapter, starts, _, confirm = native_adapter
    sender = native_sender(adapter)

    instance = detector(percent=90, sender=sender)
    assert instance.rotation_once() is None
    assert instance.recommended == 0 and instance._rotation is None
    assert len(starts) == 1

    waiting = detector(percent=90, sender=sender)
    waiting._rotation_attempt = instance._rotation_attempt
    assert waiting.rotation_once() is None
    assert len(starts) == 1, "a submission awaiting confirmation was repeated"

    confirm()
    settled = detector(percent=90, sender=sender)
    settled._rotation_attempt = waiting._rotation_attempt
    assert settled.rotation_once() is not None
    assert settled._rotation is not None and settled._rotation_attempt is None
    assert len(starts) == 1


# --- through the production sender, not around it -----------------------------
#
# `session_io.send_input` turns a native `uncertain` receipt into a
# ControlPlaneError carrying `delivered=True` — so the receipt the first cut
# inspected never arrived, and the exception path marked an unknown submission as
# a delivered recommendation. Only the real function shows that; a fake sender
# returns a shape production never produces.

def send_through_production(monkeypatch, payload, *, status=200):
    """`session_io.send_input` with only the control plane's reply replaced."""
    from lmer_platform import session_io

    monkeypatch.setattr(
        session_io, "control_endpoint",
        lambda session_id: session_io.ControlEndpoint(
            host="127.0.0.1", port=1, token="t",
        ),
    )
    monkeypatch.setattr(
        session_io, "_call",
        lambda *args, **kwargs: session_io._ControlReply(status=status, payload=payload),
    )
    return session_io.send_input


@pytest.mark.parametrize("state", ["uncertain", "rejected"])
def test_a_native_state_short_of_accepted_is_not_a_recommendation(
    detector, platform_root, monkeypatch, state
):
    sender = send_through_production(monkeypatch, {
        "transport": "codex-app-server", "state": state,
        "error": "native response unavailable",
    })
    instance = detector(percent=90, sender=sender)
    monkeypatch.setattr(
        detect, "quiet_health",
        lambda _: {"codex_delivery": {"runtime": {"model": "astra"}, "receipts": []}},
    )

    assert instance.rotation_once() is None
    assert instance.recommended == 0
    assert instance._rotation is None, "an unknown submission closed the window"
    assert [
        event for event in events(platform_root)
        if event.get("type") == detect.ASSISTANT_ROTATION_RECOMMENDED
    ] == []


def test_the_attempt_survives_an_uncertain_submission_unchanged(
    detector, platform_root, monkeypatch
):
    """Retaining the identity is what keeps an uncertain submission from being
    replayed: the adapter answers the same id from its own receipt store."""
    sender = send_through_production(monkeypatch, {
        "transport": "codex-app-server", "state": "uncertain",
    })
    monkeypatch.setattr(
        detect, "quiet_health",
        lambda _: {"codex_delivery": {"runtime": {"model": "astra"}, "receipts": []}},
    )
    instance = detector(percent=90, sender=sender)

    assert instance.rotation_once() is None
    held = instance._rotation_attempt
    assert held is not None
    assert instance.rotation_once() is None
    assert instance._rotation_attempt == held, "the retry would arrive as a new message"


def test_the_age_bound_measures_the_reading_not_the_document():
    """A codex status line carries a dated `context` block of its own while the
    document around it is rewritten on every adapter poll — including the polls
    that re-publish a figure the rollout counter can no longer refresh. Aged
    against the wrapper, an hours-old reading recommends ending a context window
    every fifteen minutes and looks permanently fresh doing it."""
    assert detect._reading_age(
        _status_with(document_age=3.0, reading_age=4000.0), {"age_seconds": 4000.0},
    ) == 4000.0
    # A claude status line dates only the document, and there the wrapper IS the
    # reading's age.
    assert detect._reading_age(
        _status_with(document_age=12.0, reading_age=None), {"percent": 80},
    ) == 12.0


def _status_with(*, document_age, reading_age):
    context = {"percent": 90}
    if reading_age is not None:
        context["age_seconds"] = reading_age
    return assistant.AssistantStatus(
        running=True, session_id="assistant-1", pid=1, started_at=iso(hours=1),
        generation=1, stale=False, tracked=True, pending=0, handoff=None,
        statusline={"age_seconds": document_age, "context": context},
    )


def test_a_stale_codex_reading_inside_a_fresh_document_types_nothing(
    detector, platform_root
):
    sender = _Sender()
    instance = detector(
        percent=99, age=2.0,
        reading_age=rotation.MAX_READING_AGE_SECONDS + 300, sender=sender,
    )

    assert instance.rotation_once() is None
    assert sender.calls == []


def test_a_session_too_young_to_have_published_is_not_called_unreadable(
    detector, platform_root
):
    """Every incarnation has a window between its container starting and its
    harness first rendering a status line, and the tick lands in it routinely.
    "This harness publishes no context figure" is false there, and it is the
    sentence an operator uses to decide the feature cannot work on their host."""
    instance = detector(percent=None, document=False, started="new")

    assert instance.rotation_once() is None
    assert events(platform_root) == []
    assert instance._context_unreadable is None, "the claim was made and latched"


def test_a_session_old_enough_with_no_document_is_reported(
    detector, platform_root
):
    instance = detector(percent=None, document=False)

    assert instance.rotation_once() is None
    notices = [
        event for event in events(platform_root)
        if event.get("type") == detect.ASSISTANT_ROTATION_UNREADABLE
    ]
    assert len(notices) == 1
    assert notices[0]["data"]["document"] is False, (
        "the two absences — no document at all, and a document with no context "
        "block — are different faults and only the event can keep them apart"
    )
