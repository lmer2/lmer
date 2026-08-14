"""Tests for the chat view's transcript adapter (issue #141, slice M2 / T18).

Fixture-driven on purpose. Spec D6 accepted that the harness transcript format is
**not a stable public contract**, and the mitigation it named was captured
fixtures — so ``tests/fixtures/transcripts/`` holds real-shaped Claude Code
records (usage blocks, thinking signatures, attachment rows, a torn final line
and all) rather than a tidy invention. A format change is then a test failure with
a diff, instead of an empty chat view on someone's phone.

Four properties carry the weight, because each fails silently and looks like
something else:

- **One bad line must not empty the conversation.** The fixture ends with a
  non-JSON line and a truncated one, which is what a crash mid-append leaves.
- **A failed tool has to be visible.** It is the interesting case, and it lives in
  a *different record* from the call it belongs to.
- **The cursor cannot skip or repeat.** It is the whole basis of polling, and a
  bug there presents as a chat view that drops turns.
- **Nothing leaked.** A real transcript on this codebase recorded a URL carrying
  the platform's own shared secret; the adapter is the last place that can catch
  it before it reaches a browser.

Since T22 the module also has a *write* path: a spawn mounts a host directory in
as the harness's projects dir, so the transcript now outlives the container and is
scrubbed and locked down when the session ends. Its tests are at the bottom, and
carry one property of their own — **a half-rewritten transcript is worse than an
unscrubbed one**, so the rewrite is atomic and structure-preserving.
"""

import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lmer_platform import api, registry, runs, spawn, store, transcripts
from lmer_platform import config as cfg
from tests.conftest import denied_read, strip_lmer_env

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "transcripts"
SESSION_FIXTURE = FIXTURES / "claude-session.jsonl"
FOLLOWUP_FIXTURE = FIXTURES / "claude-followup.jsonl"

SECRET = "test-secret-value"

#: A pid nothing can be running under, so an entry reads as crashed. Same value
#: the rest of the platform tests use.
DEAD_PID = 2**22

RUN = {"host": "gitlab.example.com", "project": "agents/global", "slug": "develop-t18"}


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


@pytest.fixture(autouse=True)
def _isolated_transcript_root(tmp_path, monkeypatch):
    """Never let a test fall through to the developer's own ~/.claude/projects.

    Autouse because the default root is ``Path.home()``-based, and a test that
    forgot would read (and assert against) whatever real conversations happen to
    be on the machine. Same spirit as ``conftest._isolate_platform_state``.
    """
    root = tmp_path / "harness-transcripts"
    root.mkdir()
    monkeypatch.setattr(transcripts, "TRANSCRIPT_ROOT", str(root))
    return root


def records(path=SESSION_FIXTURE):
    """The fixture's parseable records, for testing the normaliser alone."""
    parsed = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except ValueError:
            continue
    return parsed


def plant_session(session_id, *, fixture=SESSION_FIXTURE, run=RUN, pid=DEAD_PID):
    """Register a session and give it that fixture as its transcript.

    The transcript lands in the platform's own per-session directory, one level
    down — the layout a mounted harness projects dir has, since the harness keeps
    a subdirectory per workspace.
    """
    registry.register(session_id, pid=pid, run=dict(run) if run else None)
    directory = transcripts.session_transcript_dir(session_id) / "-workspace"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{fixture.stem}.jsonl"
    target.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def plant_log(session_id, data=b"hello"):
    """Give a session a PTY log, which is what makes it exist to the API."""
    path = spawn.log_path_for(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# --- the normaliser, alone --------------------------------------------------

def test_real_shaped_transcript_normalises():
    """Roles, order and text, from records captured off a real session."""
    messages = transcripts.normalise_records(records())
    shape = [(m.role, m.kind) for m in messages]

    assert shape == [
        ("user", "said"),        # the /start command, unwrapped
        ("user", "injected"),    # stop-hook feedback: machinery, not a person
        ("assistant", "said"),   # "I'll run the suite."
        ("assistant", "said"),   # the Bash call
        ("assistant", "said"),   # the failed Edit
        ("system", "notice"),    # /usage
        ("user", "said"),        # the operator's next prompt
        ("assistant", "said"),   # text plus a still-running tool
    ]
    assert messages[0].text == "start\n/start"
    assert messages[2].text == "I'll run the suite."
    assert messages[0].at == "2026-07-20T09:00:01.000Z"


def test_thinking_blocks_are_not_served():
    """The model's private reasoning is not the readable summary (and is long)."""
    messages = transcripts.normalise_records(records())
    for message in messages:
        assert "should check the layout" not in message.text
        assert "signature" not in message.text


def test_noise_records_are_dropped():
    """A transcript is mostly bookkeeping; none of it is a chat message."""
    messages = transcripts.normalise_records(records())
    assert not [m for m in messages if "deferred_tools_delta" in m.text]
    # turn_duration and file-history rows carry no prose and produce nothing.
    assert len(messages) == 8


def test_sidechain_records_are_skipped():
    """A subagent's conversation is not the operator's; its Agent call is."""
    messages = transcripts.normalise_records(records())
    assert not [m for m in messages if "reviewer sidechain" in m.text]


def test_tool_calls_are_collapsed_to_a_name_and_a_hint():
    messages = transcripts.normalise_records(records())
    calls = [tool for message in messages for tool in message.tools]
    assert [tool.name for tool in calls] == ["Bash", "Edit", "Bash"]
    assert calls[0].detail == "python -m pytest tests/ -q"
    assert calls[1].detail == "/workspace/src/lmer_platform/registry.py"
    # Nothing of the payload itself: old_string/new_string never leave the host.
    assert "old_string" not in json.dumps([tool.to_dict() for tool in calls])


def test_a_failed_tool_is_visible_with_its_reason():
    """The failure is in a later record than the call, and has to find its way back."""
    messages = transcripts.normalise_records(records())
    failed = [
        tool for message in messages for tool in message.tools
        if tool.status == "failed"
    ]
    assert len(failed) == 1
    assert failed[0].name == "Edit"
    assert failed[0].error == "String to replace not found in file."
    # The wrapper tag the harness puts around it is not shown.
    assert "tool_use_error" not in failed[0].error


def test_a_succeeded_tool_reads_as_ok():
    messages = transcripts.normalise_records(records())
    ok = [t for m in messages for t in m.tools if t.name == "Bash" and t.status == "ok"]
    assert len(ok) == 1


def test_a_tool_with_no_result_yet_reads_as_pending():
    """What the session is doing right now is often the answer being looked for."""
    messages = transcripts.normalise_records(records())
    pending = [t for m in messages for t in m.tools if t.status == "pending"]
    assert [t.detail for t in pending] == ["git -C /work fetch --prune"]


def test_a_tool_result_alone_is_not_a_message():
    """The harness feeding the model is not a turn; the outcome is on the call."""
    messages = transcripts.normalise_records(records())
    for message in messages:
        assert "12 passed in 0.8s" not in message.text


def test_unparseable_and_truncated_lines_are_skipped_not_fatal():
    """A crash mid-append leaves a torn final line. It must cost one line."""
    raw = SESSION_FIXTURE.read_text(encoding="utf-8").splitlines()
    assert raw[-1].startswith('{"type":"assistant"')  # the truncated one
    assert "not json at all" in raw[-2]

    messages = transcripts.normalise_records(records())
    assert len(messages) == 8


def test_a_transcript_of_only_garbage_is_empty_not_an_error():
    assert transcripts.normalise_records([{"nonsense": True}, {}, {"type": "x"}]) == []


@pytest.mark.parametrize(
    "record",
    [
        {"type": "user"},                                     # no message at all
        {"type": "user", "message": "not a mapping"},
        {"type": "user", "message": {"role": "tool"}},         # a role we don't render
        {"type": "user", "message": {"role": "user", "content": 42}},
        {"type": "system", "subtype": "turn_duration"},        # carries no prose
        {"type": "system", "content": "   "},
        {"type": "file-history-delta", "trackingPath": "/tmp/x"},
    ],
)
def test_a_record_that_is_not_a_turn_produces_nothing(record):
    assert transcripts.normalise_records([record]) == []


def test_a_record_shape_that_raises_costs_that_record_only():
    """The format is not a contract (spec D6), so a surprise must be survivable."""
    good = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "fine"}]},
    }
    messages = transcripts.normalise_records([None, good, ["not a record"]])
    assert [m.text for m in messages] == ["fine"]


def test_a_tool_result_carrying_blocks_rather_than_a_string_is_read():
    """A real shape: an image or multi-part result arrives as a block list."""
    records_in = [
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": {"command": "false"}},
            ]},
        },
        {
            "type": "user",
            "message": {"role": "user", "content": [
                {"tool_use_id": "t1", "type": "tool_result", "is_error": True,
                 "content": [{"type": "text", "text": "Exit code 1"}]},
            ]},
        },
    ]
    tool = transcripts.normalise_records(records_in)[0].tools[0]
    assert tool.status == "failed"
    assert tool.error == "Exit code 1"


def test_a_block_list_with_junk_in_it_is_tolerated():
    record = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            "a bare string where a block should be",
            {"type": "text", "text": "the real answer"},
            {"type": "something_new_next_release"},
        ]},
    }
    assert transcripts.normalise_records([record])[0].text == "the real answer"


@pytest.mark.parametrize(
    "result_content",
    ["", "   \n\n", None, {"unexpected": "shape"}],
)
def test_a_failure_with_no_readable_reason_still_shows_as_failed(result_content):
    """"Something failed" is worth saying even when the reason is unquotable."""
    records_in = [
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": "raw string"},
            ]},
        },
        {
            "type": "user",
            "message": {"role": "user", "content": [
                {"tool_use_id": "t1", "type": "tool_result", "is_error": True,
                 "content": result_content},
            ]},
        },
    ]
    tool = transcripts.normalise_records(records_in)[0].tools[0]
    assert tool.status == "failed"
    assert tool.error is None
    # A tool whose input is not a mapping still has a name to show.
    assert tool.detail is None


def test_an_orphan_tool_result_is_dropped():
    """Its call is not in this file, so there is no name and no message for it."""
    record = {
        "type": "user",
        "message": {"role": "user", "content": [
            {"tool_use_id": "toolu_from_another_session", "type": "tool_result",
             "content": "output", "is_error": True},
        ]},
    }
    assert transcripts.normalise_records([record]) == []


def test_a_system_record_that_is_only_wrapper_tags_produces_nothing():
    record = {"type": "system", "subtype": "local_command",
              "content": "<command-args></command-args>"}
    assert transcripts.normalise_records([record]) == []


def test_a_tool_with_no_recognisable_input_still_shows_its_name():
    """A blank chip beats a missing one; a table of per-tool keys would rot."""
    record = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t", "name": "TodoWrite", "input": {"todos": []}},
        ]},
    }
    tool = transcripts.normalise_records([record])[0].tools[0]
    assert tool.name == "TodoWrite"
    assert tool.detail is None


def test_long_text_is_trimmed_and_says_so():
    record = {
        "type": "assistant",
        "timestamp": "2026-07-20T09:00:00.000Z",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "x" * (transcripts.TEXT_LIMIT + 500)},
        ]},
    }
    message = transcripts.normalise_records([record])[0]
    assert message.truncated is True
    assert len(message.text) <= transcripts.TEXT_LIMIT


# --- a watch firing is nobody talking (T99) ----------------------------------
#
# The live incident, verbatim: the operator armed uber lmer's digest watch (T89),
# it fired, and the uber lmer chat drew this as a message the operator had typed.
#
# Two things are wrong with that and only one of them is cosmetic. The markup is
# ugly (and double-escaped — note `&amp;gt;`, the harness escaping text that was
# already escaped), but the identity is the failure: nobody said it. Which is not
# something the view can work out, because the record the harness writes carries
# the operator's own role and no `isMeta` to put it behind the internals toggle —
# so the classification is the server's, and these tests are on that.

#: The injected turn as Claude Code 2.1.220 writes it: a plain string of markup,
#: role 'user', ``origin.kind`` and ``promptSource`` the only fields that say a
#: keyboard was not involved. Pinned as one string because it is a captured
#: artifact, in the spirit of the fixture files (spec D6).
MONITOR_INJECTION = (
    "<task-notification>\n"
    "<task-id>b63lu8hv2</task-id>\n"
    '<summary>Monitor event: "lmer pending digest count &amp;gt; 0"</summary>\n'
    "<event>fleet digests pending: 1</event>\n"
    "</task-notification>"
)


def monitor_record(text=MONITOR_INJECTION, **extra):
    """One injected monitor turn, shaped like the captured one."""
    record = {
        "type": "user",
        "timestamp": "2026-07-29T08:15:03.000Z",
        "origin": {"kind": "task-notification"},
        "promptSource": "system",
        "message": {"role": "user", "content": text},
    }
    record.update(extra)
    return record


def test_a_monitor_event_is_attributed_to_the_watch_and_not_to_the_operator():
    """The whole of the report: "nobody said this".

    The role is the fix, not a caption on top of a user turn — the view draws a
    turn by who it is from, and a watch firing that kept role ``user`` would still
    be an operator bubble however it was labelled. ``via`` is the same statement
    the ask channel's turns make, so a reader comparing this against the terminal
    can see where the words came from.
    """
    message = transcripts.normalise_records([monitor_record()])[0]

    assert message.role == transcripts.MONITOR_ROLE
    assert message.role not in ("user", "assistant"), (
        "a watch firing is one of the two parties in the conversation"
    )
    assert message.via == transcripts.MONITOR_VIA
    # Not hidden: it explains the turn that follows it, and the record carries no
    # isMeta anyway — which is exactly why it reached the operator as a bubble.
    assert message.kind == "said"


def test_a_monitor_event_is_presented_decoded_rather_than_as_markup():
    """The condition the watch was armed on, then the line that fired it.

    Both entity-decoded, and the ``&amp;gt;`` in the captured artifact is why that
    is two passes rather than one: the harness escaped text that had already been
    escaped, so a single pass leaves ``&gt;`` on the screen. The markup itself is
    a delivery mechanism and the view is deliberately dumb about provenance, so
    none of it survives to be parsed a second time in a browser.
    """
    message = transcripts.normalise_records([monitor_record()])[0]

    assert message.text == "lmer pending digest count > 0\nfleet digests pending: 1"
    for leak in ("<summary>", "<event>", "<task-id>", "&amp;", "&gt;"):
        assert leak not in message.text, f"{leak!r} reaches the operator"


def test_a_monitor_event_with_no_event_line_shows_the_condition_alone():
    """``<event>`` is optional in the notification the harness builds, and an
    absent field renders as nothing rather than as an empty second line."""
    text = MONITOR_INJECTION.replace(
        "<event>fleet digests pending: 1</event>\n", ""
    )
    message = transcripts.normalise_records([monitor_record(text)])[0]
    assert message.text == "lmer pending digest count > 0"


def test_the_advice_a_monitor_event_carries_for_the_model_is_not_shown():
    """The injection can end with a sentence telling the *model* whether to notify
    anyone. That is machinery addressing the session, like a system reminder, and
    it is not part of the event."""
    text = MONITOR_INJECTION.replace(
        "</event>\n",
        "</event>\nIf this event is something the user would act on now, send a "
        "notification. Routine or benign output doesn't need one.\n",
    )
    message = transcripts.normalise_records([monitor_record(text)])[0]
    assert "act on now" not in message.text


def test_an_ordinary_message_is_left_alone_by_the_monitor_classification():
    """The turns this must not touch are every other turn in the conversation."""
    messages = transcripts.normalise_records(records())

    assert not [m for m in messages if m.role == transcripts.MONITOR_ROLE]
    assert not [m for m in messages if m.via]


def test_a_turn_that_merely_mentions_the_markup_is_still_a_turn():
    """Anchored at the start of the message, not searched for inside it.

    An agent quoting an injection in a report, or an operator asking about one,
    is talking *about* an event — and the second case is not hypothetical, since
    quoting the markup into a chat is how this was reported.
    """
    quoted = f"why does this render as me?\n\n{MONITOR_INJECTION}"
    message = transcripts.normalise_records([monitor_record(quoted)])[0]

    assert message.role == "user"
    assert message.via is None


def test_a_pasted_monitor_event_stays_the_operators_own_words():
    """The second anchor, and the one that has to be there.

    An injection's markup is text; an operator can type it, and the text alone
    cannot tell that apart from the harness having written it. So the record has
    to say a keyboard was not involved — ``origin.kind``/``promptSource`` on the
    build this was captured from — before the shape is consulted at all. Without
    that check, quoting the incident in the chat would attribute the operator's
    own message to the watch, which is the reported bug with the parties swapped.
    """
    typed = monitor_record()
    typed.pop("origin")
    typed.pop("promptSource")

    message = transcripts.normalise_records([typed])[0]
    assert message.role == "user"
    assert message.via is None


def test_the_monitor_classification_survives_the_origin_field_alone():
    """Either marker is enough: they are two spellings of the same fact, and the
    format is not a contract (spec D6), so needing both would make the fix hostage
    to a release that renames one."""
    for keep in ("origin", "promptSource"):
        record = monitor_record()
        for field in ("origin", "promptSource"):
            if field != keep:
                record.pop(field)
        message = transcripts.normalise_records([record])[0]
        assert message.role == transcripts.MONITOR_ROLE, (
            f"{keep!r} alone no longer identifies an injected turn"
        )


def test_a_monitor_event_is_scrubbed_like_every_other_string():
    """It goes through ``_present`` with everything else, which is the point of
    there being one chokepoint: a watch armed on a URL with a credential in it is
    an ordinary way for one to be written."""
    text = MONITOR_INJECTION.replace(
        "fleet digests pending: 1",
        "curl https://x:hunter2hunter2@example.com/ said 1",
    )
    message = transcripts.normalise_records([monitor_record(text)])[0]
    assert "hunter2hunter2" not in message.text


# --- the harness's other injections (#242) ------------------------------------
#
# A watch firing is one thing the harness injects; a background command exiting
# and a subagent stopping are the others, and they arrive in the same role, with
# the same `origin`/`promptSource` anchors and no `isMeta`. The classification
# read `isMeta` alone for everything that was not a monitor event, so every
# finished background task rendered as a block of task ids and output-file paths
# the operator had apparently typed — the reported bug, one route over from the
# one the section above closed.

#: A background task notification as Claude Code 2.1.228 writes it, from a live
#: transcript on this codebase (ids and paths scrubbed). Not a monitor event: the
#: summary is a different sentence, which is the whole reason the text shape
#: cannot be what decides whether a turn is the operator's.
BACKGROUND_TASK_NOTIFICATION = (
    "<task-notification>\n"
    "<task-id>t-000000001</task-id>\n"
    "<tool-use-id>toolu_00000000000000000000</tool-use-id>\n"
    "<output-file>/tmp/agent-output/t-000000001.output</output-file>\n"
    "<status>failed</status>\n"
    '<summary>Background command "Run gate checks on clean state" failed with '
    "exit code 1</summary>\n"
    "</task-notification>"
)


def injected_record(text=BACKGROUND_TASK_NOTIFICATION, **extra):
    """One harness-injected turn that is not a monitor event.

    The monitor's own builder, deliberately: the two records are identical down
    to the field, and only what the injection *says* differs.
    """
    return monitor_record(text, **extra)


def test_a_finished_background_task_is_not_a_turn_the_operator_typed():
    """The report: the harness put it there, so it is not the operator's words.

    ``injected`` rather than the monitor's re-attribution — it is machinery
    addressing the session, which is what that kind already means, and the view
    keeps it behind the internals toggle instead of drawing a bubble for it.
    """
    message = transcripts.normalise_records([injected_record()])[0]

    assert message.kind == "injected"
    assert message.kind != "said", "the harness's own turn is drawn as the operator"
    assert message.role == "user", (
        "the role is the record's; only a monitor event is re-attributed"
    )
    assert message.via is None


def test_an_injected_turn_needs_no_isMeta_to_be_injected():
    """``isMeta`` was the only thing consulted, and a task notification carries
    none — so the marker that says a keyboard was not involved has to be enough on
    its own, either spelling of it."""
    for keep in ("origin", "promptSource"):
        record = injected_record()
        assert "isMeta" not in record
        for field in ("origin", "promptSource"):
            if field != keep:
                record.pop(field)
        message = transcripts.normalise_records([record])[0]
        assert message.kind == "injected", f"{keep!r} alone no longer classifies"


def test_a_turn_the_operator_typed_is_untouched_by_the_injected_classification():
    """The other half, and the one a wrong fix breaks silently: a real typed turn
    carries the harness's markers too — ``promptSource: typed``, ``origin.kind:
    human`` — and those are not what :func:`_injected_by_harness` reads."""
    typed = injected_record(
        "the gate failed on the first item, look at that one first",
        origin={"kind": "human"},
        promptSource="typed",
    )
    message = transcripts.normalise_records([typed])[0]

    assert (message.role, message.kind) == ("user", "said")


#: The same records off a live Claude Code 2.1.228: two task notifications (a
#: background command that failed, a subagent that stopped), one turn the operator
#: typed, and assistant turns around them. Ids, paths and prose are scrubbed; the
#: keys, their order and the version string are the ones the harness wrote.
TASK_NOTIFICATION_FIXTURE = FIXTURES / "claude-task-notification.jsonl"


def test_the_captured_notifications_classify_as_the_hand_built_ones_claim(platform_root):
    """Read end to end, for the reason spec D6 gives: the hand-built records above
    pin the fix to this author's model of the format, and it was that model being
    a turn short — ``isMeta`` on everything the harness injects — that produced the
    bug. A release that changes the anchors is then a diff here."""
    plant_session("s-notified", fixture=TASK_NOTIFICATION_FIXTURE)
    page = transcripts.read_messages("s-notified", limit=100)

    assert [(m.role, m.kind) for m in page.messages] == [
        ("assistant", "said"),
        ("user", "injected"),
        ("user", "injected"),
        ("user", "said"),
        ("assistant", "said"),
    ]
    # Nothing the harness wrote for the model is drawn as something a party said.
    said = [m for m in page.messages if m.kind == "said" and m.role == "user"]
    assert [m.text for m in said] == [
        "the gate failed on the first item, look at that one first"
    ]
    for message in said:
        assert "task-notification" not in message.text
        assert "toolu_" not in message.text


# --- a message typed into a busy session (#275) -------------------------------
#
# Type while the session is mid-turn and Claude Code queues the message — and
# writes no `user` record for it at all. Three rows go down instead: the
# `queue-operation` enqueue, its `remove`, and an `attachment` of type
# `queued_command` at the point the model actually received the text. The
# normaliser dropped all three, so the chat's pending bubble — which settles only
# on a matching user turn (#254) — never settled, and one more stuck bubble piled
# up per mid-turn message. Shapes below are the ones a live transcript carried.
#
# That queue carries the harness's own task notifications too, told apart by the
# attachment's `origin`: a typed message says `{"kind": "human"}`, machinery
# carries no `origin` key at all (the captured fixture below; a null would fail
# the same check). So the tests come in pairs — the message has to arrive, and the
# machinery must not arrive wearing the operator's role.

QUEUE_SESSION = "11111111-2222-3333-4444-555555555555"


def queue_operation(text, operation="enqueue", at="2026-08-11T10:00:01.000Z"):
    """One row of the queue's bookkeeping. ``dequeue`` carries no ``content``."""
    record = {
        "type": "queue-operation",
        "operation": operation,
        "timestamp": at,
        "sessionId": QUEUE_SESSION,
    }
    if operation != "dequeue":
        record["content"] = text
    return record


#: What the harness pushes through the same queue for its own purposes, and the
#: shape a live transcript carried it in: no origin at all and a commandMode of
#: its own, wrapping kilobytes of task ids that ``_strip_wrappers`` only takes the
#: outer tag off. Pinned as a captured artifact, like ``MONITOR_INJECTION``.
TASK_NOTIFICATION_PROMPT = (
    "<task-notification>\n"
    "<task-id>b63lu8hv2</task-id>\n"
    "<tool-use-id>toolu_01AAAABBBBCCCCDDDD</tool-use-id>\n"
    "<summary>Agent stopped</summary>\n"
    "</task-notification>"
)


def queued_delivery(text, at="2026-08-11T10:00:04.000Z", uuid="q-1", **attachment):
    """The attachment row: the queued message arriving where the model got it.

    ``origin`` defaults to the harness's own marker for a keyboard, which is what
    a typed message carries and what makes this a turn at all.
    """
    payload = {
        "type": "queued_command",
        "prompt": text,
        "commandMode": "prompt",
        "origin": {"kind": "human"},
        "timestamp": at,
    }
    payload.update(attachment)
    return {
        "type": "attachment",
        "timestamp": at,
        "isSidechain": False,
        "uuid": uuid,
        "attachment": payload,
    }


def spoken(text, at="2026-08-11T10:00:00.000Z"):
    """An assistant turn, for placing a queued message among other records."""
    return {
        "type": "assistant",
        "timestamp": at,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def test_a_message_queued_mid_turn_is_one_turn_where_it_was_delivered():
    """The report, whole: three rows for one message, and exactly one turn out.

    The attachment is the delivery — the enqueue and the remove are the queue
    talking to itself and carry the same string — so it is the row that becomes
    the turn, and it stays where the file put it, which is where the model
    received it.
    """
    messages = transcripts.normalise_records([
        spoken("working on it"),
        queue_operation("ship it when the suite is green"),
        queue_operation("ship it when the suite is green", operation="remove"),
        queued_delivery("ship it when the suite is green"),
        spoken("understood", at="2026-08-11T10:00:09.000Z"),
    ])

    assert [(m.role, m.kind, m.text) for m in messages] == [
        ("assistant", "said", "working on it"),
        ("user", "said", "ship it when the suite is green"),
        ("assistant", "said", "understood"),
    ]
    assert messages[1].at == "2026-08-11T10:00:04.000Z"


def test_three_messages_queued_in_one_turn_are_three_turns_in_order():
    """The acceptance case: an operator typing while the agent works gets every
    message back, once each, in the order the model received them."""
    records_in = [spoken("thinking")]
    for index, text in enumerate(("first", "second", "third"), start=1):
        records_in.append(queue_operation(text))
        records_in.append(queue_operation(text, operation="remove"))
        records_in.append(
            queued_delivery(text, at=f"2026-08-11T10:00:0{index}.000Z", uuid=f"q-{index}")
        )

    messages = transcripts.normalise_records(records_in)

    assert [m.text for m in messages if m.role == "user"] == ["first", "second", "third"]
    assert len(messages) == 4


def test_the_queues_own_bookkeeping_is_not_a_turn():
    """Without the delivery there is nothing to show: an enqueue is a message the
    model has not been handed yet, a remove and a dequeue are the queue draining.
    Emitting any of them would double the message the attachment carries."""
    assert transcripts.normalise_records([
        queue_operation("ship it"),
        queue_operation("ship it", operation="remove"),
        queue_operation("ship it", operation="dequeue"),
    ]) == []


def test_an_attachment_that_is_not_a_delivered_message_produces_nothing():
    """The record family stays dropped; one type of it is a turn."""
    other = queued_delivery("ship it")
    other["attachment"]["type"] = "deferred_tools_delta"

    assert transcripts.normalise_records([other]) == []
    assert transcripts.normalise_records([{"type": "attachment", "uuid": "a"}]) == []
    assert transcripts.normalise_records(
        [{"type": "attachment", "attachment": "not a mapping"}]
    ) == []


@pytest.mark.parametrize("prompt", [None, "", "   ", 42, {"text": "ship it"}])
def test_a_delivered_message_with_no_readable_prompt_produces_nothing(prompt):
    """Another program writes this file (spec D6), so nothing about the shape is
    assumed: a delivery with nothing readable in it renders as nothing."""
    record = queued_delivery("ship it")
    record["attachment"]["prompt"] = prompt

    assert transcripts.normalise_records([record]) == []


def test_a_task_notification_pushed_through_the_queue_is_not_the_operator():
    """The queue is not the operator's alone.

    The harness delivers its own task notifications the same way, with no
    ``origin`` at all and a ``commandMode`` of their own — the shape the captured
    fixture carries, which is not the ``origin: null`` this test asserted while it
    was hand-built. Surfacing one would put kilobytes of task ids and tool-use ids
    in the chat as a message the operator had typed — the same failure the monitor
    classification exists to prevent, arriving by a different route.
    """
    record = queued_delivery(TASK_NOTIFICATION_PROMPT)
    record["attachment"].pop("origin")
    record["attachment"]["commandMode"] = "task-notification"

    assert transcripts.normalise_records([record]) == []


def test_a_queued_message_needs_the_harness_to_say_a_keyboard_wrote_it():
    """The marker is required, not its absence tolerated.

    A blocklist of machinery ``commandMode``s would let the next internal kind a
    release invents through, drawn as the operator's own words; requiring the
    positive human marker fails the other way, and that failure is a bubble that
    does not settle — today's behavior, and nothing worse.
    """
    typed = queued_delivery("ship it")
    assert transcripts.normalise_records([typed])[0].text == "ship it"

    for origin in (None, {}, {"kind": "task-notification"}, "human", ["human"]):
        record = queued_delivery("ship it", origin=origin)
        assert transcripts.normalise_records([record]) == [], (
            f"origin {origin!r} passed as something the operator typed"
        )

    missing = queued_delivery("ship it")
    missing["attachment"].pop("origin")
    assert transcripts.normalise_records([missing]) == []


def test_a_queued_message_is_scrubbed_like_every_other_string():
    """It is operator input like anything else typed into the chat, so it goes
    through ``_present`` with the rest — this must not become the one path where a
    pasted credential reaches a browser."""
    message = transcripts.normalise_records([
        queued_delivery("try curl http://x:s3cr3t-value-here@127.0.0.1:8620/ again"),
    ])[0]

    assert "s3cr3t-value-here" not in message.text
    assert "curl http://127.0.0.1:8620/ again" in message.text


def test_a_queued_message_is_trimmed_at_the_same_ceiling():
    """The cap is the module's, not the record type's."""
    message = transcripts.normalise_records([
        queued_delivery("x" * (transcripts.TEXT_LIMIT + 500)),
    ])[0]

    assert message.truncated is True
    assert len(message.text) <= transcripts.TEXT_LIMIT


def test_a_queued_message_from_a_sidechain_is_still_skipped():
    """The sidechain gate is above this branch and stays there: a subagent's own
    queued input is not the operator's conversation."""
    record = queued_delivery("ship it")
    record["isSidechain"] = True

    assert transcripts.normalise_records([record]) == []


#: The same three rows, off a live Claude Code 2.1.228 instead of built here: an
#: operator message typed mid-turn (enqueue, delivery attachment, remove), one of
#: the harness's own task notifications through the same queue, and ordinary
#: assistant turns around both. Ids, branch and prose are scrubbed; every key,
#: their order, and the version string are the ones the harness wrote.
QUEUED_FIXTURE = FIXTURES / "claude-queued-command.jsonl"


def test_the_captured_queue_shapes_normalise_as_the_hand_built_ones_claim(platform_root):
    """Every queued-message test above builds its own records, which pins the fix
    to the author's model of the format rather than to the format.

    That model being wrong is what produced #275: a record family nobody had
    looked at, dropped because the normaliser's idea of a transcript did not
    include it. Spec D6 accepted that the shape is not a contract and named
    captured fixtures as the mitigation, so this reads a real one end to end — a
    release that changes the shape is then a diff here rather than a pending
    bubble that never settles on someone's phone.

    The capture already corrected one hand-built detail: the harness's own
    delivery carries no ``origin`` key at all, where these tests had been passing
    ``origin: null``. Both fail the same check, which is why the fix needed no
    change — but only the capture could say so.
    """
    plant_session("s-queued", fixture=QUEUED_FIXTURE)
    page = transcripts.read_messages("s-queued", limit=100)

    assert [(m.role, m.kind, m.text) for m in page.messages] == [
        ("assistant", "said", "Running the suite now."),
        ("assistant", "said", "The first case passes."),
        ("user", "said", "Queued while you were working: check the second case too."),
        ("assistant", "said", "The second case passes too."),
    ]
    # Position is delivery order and the timestamp is when it was typed: the
    # attachment carries the enqueue's own time, which is *earlier* than the
    # assistant turn the file puts before it. Both are as captured, and the view
    # orders by file position for exactly this reason.
    assert page.messages[2].at == "2026-07-20T09:00:05.000Z"
    assert page.messages[2].at < page.messages[1].at

    # The machinery delivery and the queue's bookkeeping produced none of these.
    for message in page.messages:
        assert "task-notification" not in message.text
        assert "toolu_" not in message.text
    assert len([m for m in page.messages if m.role == "user"]) == 1


# --- credential scrubbing ---------------------------------------------------
#
# Not defensive theatre: the transcript of the session that wrote this module
# recorded a browser tool navigating to
# http://x:<the platform's shared secret>@127.0.0.1:8620/ — a live credential the
# agent had put in a URL and the harness had written down.

def test_credentials_in_a_url_are_scrubbed_out_of_message_text():
    messages = transcripts.normalise_records(records())
    mirror = [m for m in messages if "gitlab.example.com" in m.text]
    assert len(mirror) == 1
    assert "glpat-" not in mirror[0].text
    assert "oauth2" not in mirror[0].text
    assert "https://gitlab.example.com/agents/work.git" in mirror[0].text


@pytest.mark.parametrize(
    "raw, gone",
    [
        ("browse http://x:s3cr3t-value-here@127.0.0.1:8620/", "s3cr3t-value-here"),
        ('curl -H "Authorization: Bearer abc123def456ghi" u', "abc123def456ghi"),
        ("lmer chat . --fastapi-token abc123def456ghi", "abc123def456ghi"),
        ("export LMER_FASTAPI_TOKEN=abc123def456ghi", "abc123def456ghi"),
        ("clone with glpat-AAAABBBBCCCCDDDDEEEEFFFF now", "glpat-AAAA"),
    ],
)
def test_credential_shapes_are_scrubbed_from_a_tool_detail(raw, gone):
    """Tool inputs are whole command lines, so this is where credentials show up."""
    record = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": raw}},
        ]},
    }
    detail = transcripts.normalise_records([record])[0].tools[0].detail
    assert gone not in detail


def test_the_scrub_stays_linear_on_a_long_string(tmp_path):
    """A record legitimately carries 50 KB in one string, and this must not crawl.

    Run in a subprocess with a timeout on purpose: the cost being guarded against
    lives inside a single ``re.sub`` call, so a regression cannot be caught by
    timing the call afterwards — nothing would come back to time. Before the
    patterns were made linear, each of these inputs took minutes (and 300 KB took
    the better part of an hour) because an unbounded quantifier ahead of the
    credential word rescanned the run from every position in it. The only symptom
    would have been a chat request that never returned.
    """
    probe = tmp_path / "scrub-probe.py"
    probe.write_text(
        "from lmer_platform.transcripts import _scrub\n"
        "for text in ('a' * 50000, '-' * 50000, 'QUJDREVGR0g=' * 4000):\n"
        "    _scrub(text)\n",
        encoding="utf-8",
    )
    subprocess.run(
        [sys.executable, str(probe)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        timeout=30,
        check=True,
        capture_output=True,
    )


def test_ordinary_prose_is_left_alone():
    """Over-redaction is the cheap direction, but not at the cost of readability."""
    record = {
        "type": "user",
        "message": {"role": "user", "content": "the secret is in config.json, no token needed"},
    }
    message = transcripts.normalise_records([record])[0]
    assert message.text == "the secret is in config.json, no token needed"


# --- locating a session's transcript ----------------------------------------

def test_the_transcript_root_resolution_order(monkeypatch, tmp_path):
    """Module attribute, then environment, then the harness's own default."""
    monkeypatch.setattr(transcripts, "TRANSCRIPT_ROOT", str(tmp_path / "patched"))
    monkeypatch.setenv(transcripts.ENV_TRANSCRIPT_ROOT, str(tmp_path / "env"))
    assert transcripts.transcript_root() == tmp_path / "patched"

    monkeypatch.setattr(transcripts, "TRANSCRIPT_ROOT", None)
    assert transcripts.transcript_root() == tmp_path / "env"

    monkeypatch.delenv(transcripts.ENV_TRANSCRIPT_ROOT)
    assert transcripts.transcript_root() == Path.home() / ".claude" / "projects"


@pytest.mark.parametrize(
    "pointer",
    [
        {"harness": "claude"},                    # names nothing
        {"path": "   "},
        {"path": None, "dir": None},
    ],
)
def test_a_pointer_that_names_nothing_falls_back_to_the_derived_directory(
    platform_root, pointer
):
    plant_session("s-vague")
    registry.update("s-vague", transcript=pointer)
    assert transcripts.read_messages("s-vague").total == 8


def test_a_pointer_to_a_file_that_is_gone_reads_as_no_transcript(
    platform_root, _isolated_transcript_root
):
    """The pointer resolved and was allowed; the file simply is not there."""
    registry.register("s-stale", pid=DEAD_PID, run=dict(RUN))
    registry.update(
        "s-stale",
        transcript={"path": str(_isolated_transcript_root / "vanished.jsonl")},
    )
    page = transcripts.read_messages("s-stale")
    assert page.total == 0
    assert page.note == transcripts.NO_TRANSCRIPT_NOTE


def test_a_symlink_loop_in_a_pointer_is_refused_rather_than_raising(
    platform_root, _isolated_transcript_root
):
    """``Path.resolve`` raises on ELOOP, and a chat view must not 500 over it."""
    left = _isolated_transcript_root / "left.jsonl"
    right = _isolated_transcript_root / "right.jsonl"
    left.symlink_to(right)
    right.symlink_to(left)
    registry.register("s-loop", pid=DEAD_PID, run=dict(RUN))
    registry.update("s-loop", transcript={"path": str(left)})
    assert transcripts.read_messages("s-loop").total == 0


def test_missing_transcript_reads_as_empty_with_a_note(platform_root):
    """The common case today, and it must not read as "the run said nothing"."""
    registry.register("s-none", pid=DEAD_PID, run=dict(RUN))
    page = transcripts.read_messages("s-none")
    assert page.messages == ()
    assert page.total == 0
    assert page.sources == ()
    assert page.note == transcripts.NO_TRANSCRIPT_NOTE


def test_an_unknown_session_reads_as_empty_rather_than_raising(platform_root):
    """No entry, no directory. A transcript is history: absent is an answer."""
    page = transcripts.read_messages("s-never-existed")
    assert page.messages == ()
    assert page.note == transcripts.NO_TRANSCRIPT_NOTE


def test_the_per_session_directory_is_found_without_any_recorded_pointer(platform_root):
    """The resolution a spawn only has to mount, with no new state to record."""
    plant_session("s-one")
    page = transcripts.read_messages("s-one")
    assert page.total == 8
    assert [source.session for source in page.sources] == ["s-one"]
    assert [source.id for source in page.sources] == ["claude-session"]


def test_a_recorded_pointer_wins_over_the_derived_directory(
    platform_root, _isolated_transcript_root
):
    """A session that said where its transcript is knows better than a convention."""
    pointed = _isolated_transcript_root / "-workspace" / "pointed.jsonl"
    pointed.parent.mkdir(parents=True)
    pointed.write_text(FOLLOWUP_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    plant_session("s-two")  # also has a per-session directory
    registry.update("s-two", transcript={"harness": "claude", "path": str(pointed)})

    page = transcripts.read_messages("s-two")
    assert [source.id for source in page.sources] == ["pointed"]
    assert page.total == 2


def test_a_pointer_to_a_directory_is_read(platform_root, _isolated_transcript_root):
    nested = _isolated_transcript_root / "-workspace"
    nested.mkdir()
    (nested / "a.jsonl").write_text(
        FOLLOWUP_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    registry.register("s-dir", pid=DEAD_PID, run=dict(RUN))
    registry.update("s-dir", transcript={"dir": str(_isolated_transcript_root)})
    page = transcripts.read_messages("s-dir")
    assert page.total == 2


@pytest.mark.parametrize("field", ["path", "dir"])
def test_a_pointer_outside_the_transcript_roots_is_refused(
    platform_root, tmp_path, field, caplog
):
    """Registry entries are hand-editable, and this module answers an HTTP route.

    Following an arbitrary path out of one would make a tampered entry a file-read
    primitive on the daemon's behalf — the same refusal ``registry.token_path``
    makes of ``control.token_ref``.
    """
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    leak = outside / "secrets.jsonl"
    leak.write_text(FOLLOWUP_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    registry.register("s-evil", pid=DEAD_PID, run=dict(RUN))
    registry.update(
        "s-evil",
        transcript={field: str(leak if field == "path" else outside)},
    )
    page = transcripts.read_messages("s-evil")
    assert page.total == 0
    assert page.note == transcripts.NO_TRANSCRIPT_NOTE
    assert "transcript_pointer_refused" in caplog.text


def test_traversal_in_a_pointer_is_refused_after_resolution(platform_root, tmp_path):
    """`..` inside an allowed root still has to land inside it."""
    outside = tmp_path / "outside.jsonl"
    outside.write_text(FOLLOWUP_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    registry.register("s-dots", pid=DEAD_PID, run=dict(RUN))
    escape = transcripts.transcript_root() / ".." / "outside.jsonl"
    registry.update("s-dots", transcript={"path": str(escape)})
    assert transcripts.read_messages("s-dots").total == 0


@pytest.mark.parametrize("bad", ["../../etc/passwd", "..", "a/b", ".hidden", ""])
def test_a_session_id_that_could_name_a_path_is_refused(platform_root, bad):
    """One notion of a legal session id, borrowed from the registry."""
    with pytest.raises(transcripts.SessionNotFound):
        transcripts.read_messages(bad)
    with pytest.raises(transcripts.SessionNotFound):
        transcripts.session_transcript_dir(bad)


# --- spanning the sessions of one run ---------------------------------------

def test_two_sessions_of_one_run_read_as_one_conversation(platform_root):
    """Answering a question respawns the run; the operator reads one thread.

    The join is the ``run`` block on each registry entry — a crashed predecessor
    keeps its entry alongside its replacement, which is exactly this state.
    """
    plant_session("s-20260720T090000-aaaa")
    plant_session("s-20260720T113000-bbbb", fixture=FOLLOWUP_FIXTURE)

    page = transcripts.read_messages("s-20260720T113000-bbbb", limit=100)
    assert [source.session for source in page.sources] == [
        "s-20260720T090000-aaaa", "s-20260720T113000-bbbb",
    ]
    assert page.total == 10
    # Chronological, and the sequence numbers say so.
    assert page.messages[0].text == "start\n/start"
    assert page.messages[-1].text == "Understood — retargeting the MR at prep-release."
    assert [m.seq for m in page.messages] == list(range(10))
    # Each message says which harness session it came from, so the view can mark
    # the seam without the server having to render it.
    assert page.messages[0].origin == "claude-session"
    assert page.messages[-1].origin == "claude-followup"


def test_either_session_of_a_run_returns_the_same_conversation(platform_root):
    """The chat is a property of the run, not of whichever session was asked for."""
    plant_session("s-20260720T090000-aaaa")
    plant_session("s-20260720T113000-bbbb", fixture=FOLLOWUP_FIXTURE)
    first = transcripts.read_messages("s-20260720T090000-aaaa", limit=100)
    second = transcripts.read_messages("s-20260720T113000-bbbb", limit=100)
    assert first.to_dict() == second.to_dict()


def test_a_cleanly_exited_session_is_joined_through_the_tracked_index(platform_root):
    """A clean exit removes the registry entry; the index is what remembers.

    This is also the limit worth knowing: the index holds *one* id per run, so a
    run with several clean sessions can only offer the most recent.
    """
    plant_session("s-20260720T090000-aaaa")
    runs.track(RUN["host"], RUN["project"], RUN["slug"], session_id="s-20260720T090000-aaaa")
    registry.remove("s-20260720T090000-aaaa", force=True)

    page = transcripts.read_messages("s-20260720T090000-aaaa")
    assert page.total == 8
    assert transcripts.sessions_for_run("s-20260720T090000-aaaa") == [
        "s-20260720T090000-aaaa"
    ]


def test_a_session_with_no_run_identity_is_its_own_conversation(platform_root):
    """A spawn whose repo URL could not be parsed still has a transcript to show."""
    plant_session("s-lonely", run=None)
    plant_session("s-other")
    page = transcripts.read_messages("s-lonely")
    assert [source.session for source in page.sources] == ["s-lonely"]


def test_sessions_of_a_different_run_are_not_mixed_in(platform_root):
    """Showing another run's conversation is the failure D25 exists to prevent.

    Both halves of the join are exercised: a registry entry for the other run, and
    a tracked-index entry for it. Either leaking through would put a colleague's
    conversation in this operator's chat.
    """
    other = {"host": "gitlab.example.com", "project": "agents/global", "slug": "other"}
    plant_session("s-mine")
    plant_session("s-theirs", fixture=FOLLOWUP_FIXTURE, run=other)
    runs.track(other["host"], other["project"], other["slug"], session_id="s-theirs")
    runs.track(RUN["host"], RUN["project"], RUN["slug"], session_id="s-mine")

    page = transcripts.read_messages("s-mine")
    assert [source.session for source in page.sources] == ["s-mine"]
    assert transcripts.sessions_for_run("s-mine") == ["s-mine"]


# --- paging -----------------------------------------------------------------

def test_the_cursor_advances_and_does_not_duplicate(platform_root):
    plant_session("s-page")
    first = transcripts.read_messages("s-page", since=0, limit=3)
    assert [m.seq for m in first.messages] == [0, 1, 2]
    assert first.cursor == 3

    second = transcripts.read_messages("s-page", since=first.cursor, limit=3)
    assert [m.seq for m in second.messages] == [3, 4, 5]
    assert second.cursor == 6

    tail = transcripts.read_messages("s-page", since=second.cursor, limit=100)
    assert [m.seq for m in tail.messages] == [6, 7]
    assert tail.cursor == 8

    exhausted = transcripts.read_messages("s-page", since=tail.cursor)
    assert exhausted.messages == ()
    assert exhausted.cursor == 8


def test_polling_past_the_end_picks_up_only_what_was_appended(platform_root):
    target = plant_session("s-live")
    page = transcripts.read_messages("s-live", limit=100)
    cursor = page.cursor

    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "assistant",
            "timestamp": "2026-07-20T09:05:00.000Z",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "and one more thing"},
            ]},
        }) + "\n")

    fresh = transcripts.read_messages("s-live", since=cursor)
    assert [m.text for m in fresh.messages] == ["and one more thing"]
    assert fresh.cursor == cursor + 1


def test_a_negative_since_reads_the_tail(platform_root):
    """A cold open wants the end of a long conversation, not its beginning."""
    plant_session("s-tail")
    page = transcripts.read_messages("s-tail", since=-2)
    assert page.start == 6
    assert [m.seq for m in page.messages] == [6, 7]
    assert page.cursor == 8


def test_a_since_past_the_end_is_clamped(platform_root):
    plant_session("s-clamp")
    page = transcripts.read_messages("s-clamp", since=10_000)
    assert page.messages == ()
    assert page.cursor == 8


def test_the_limit_is_clamped_to_the_ceiling(platform_root):
    plant_session("s-limit")
    page = transcripts.read_messages("s-limit", limit=10**9)
    assert len(page.messages) <= transcripts.MAX_MESSAGE_LIMIT


def test_the_per_file_ceiling_is_reported_rather_than_hidden(platform_root, monkeypatch):
    """Showing a prefix of a conversation without saying so is the bad outcome."""
    monkeypatch.setattr(transcripts, "MAX_MESSAGES_PER_SOURCE", 3)
    plant_session("s-capped")
    page = transcripts.read_messages("s-capped")
    assert page.total == 3
    assert page.sources[0].capped is True


def test_the_number_of_transcript_files_read_is_bounded(platform_root, monkeypatch):
    """One request must not be able to walk an unbounded number of files."""
    monkeypatch.setattr(transcripts, "MAX_SOURCES", 1)
    plant_session("s-20260720T090000-aaaa")
    plant_session("s-20260720T113000-bbbb", fixture=FOLLOWUP_FIXTURE)
    page = transcripts.read_messages("s-20260720T113000-bbbb", limit=100)
    assert len(page.sources) == 1


# --- reads that fail --------------------------------------------------------

def test_an_unreadable_transcript_file_is_skipped_not_fatal(platform_root):
    """A transcript written by another uid inside the container reads as absent."""
    target = plant_session("s-locked")
    target.chmod(0o000)
    try:
        # Injected rather than left to the mode bit: root is exempt from the
        # permission check, so this file opens fine for CI's pytest job.
        with denied_read(target):
            page = transcripts.read_messages("s-locked")
    finally:
        target.chmod(0o644)
    assert page.messages == ()
    # The file was found, so this is "nothing readable in it", not "no transcript" —
    # two different things to tell an operator.
    assert page.sources[0].messages == 0
    assert page.note == transcripts.EMPTY_TRANSCRIPT_NOTE


def test_a_just_started_session_says_so_rather_than_nothing(platform_root):
    """The harness writes its mode rows before anything is said. Normal, not broken."""
    registry.register("s-fresh", pid=DEAD_PID, run=dict(RUN))
    directory = transcripts.session_transcript_dir("s-fresh") / "-workspace"
    directory.mkdir(parents=True)
    (directory / "brand-new.jsonl").write_text(
        '{"type":"mode","mode":"normal","sessionId":"x"}\n', encoding="utf-8"
    )
    page = transcripts.read_messages("s-fresh")
    assert page.total == 0
    assert page.note == transcripts.EMPTY_TRANSCRIPT_NOTE


def test_a_filesystem_error_listing_transcripts_is_not_fatal(
    platform_root, monkeypatch
):
    """A stale handle or an I/O error mid-walk must not 500 the chat view.

    Injected rather than provoked: ``Path.rglob`` swallows a permission error and
    yields nothing, so the only way to exercise the tolerance this module promises
    is to raise the error it exists for.
    """
    plant_session("s-io")

    def exploding_rglob(self, pattern):
        raise OSError("stale file handle")

    monkeypatch.setattr(Path, "rglob", exploding_rglob)
    page = transcripts.read_messages("s-io")
    assert page.messages == ()
    assert page.note == transcripts.NO_TRANSCRIPT_NOTE


class _FailingHandle:
    """A file handle that raises partway through. Delegates everything else."""

    def __init__(self, handle, after):
        self._handle = handle
        self._after = after
        self._calls = 0

    def readline(self, *args):
        self._calls += 1
        if self._calls > self._after:
            raise OSError("input/output error")
        return self._handle.readline(*args)

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)


def test_a_read_error_partway_through_a_transcript_is_not_fatal(
    platform_root, monkeypatch
):
    """An I/O error mid-file loses the rest of that file, not the whole view.

    Injected: a read that fails halfway is not something a test can provoke from
    the filesystem, and the tolerance this module promises has to be exercised
    rather than assumed.
    """
    plant_session("s-20260720T090000-aaaa")
    plant_session("s-20260720T113000-bbbb", fixture=FOLLOWUP_FIXTURE)

    real_open = Path.open

    def flaky_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        # Only the transcripts: the registry is read through the same call.
        return _FailingHandle(handle, 2) if self.suffix == ".jsonl" else handle

    monkeypatch.setattr(Path, "open", flaky_open)
    page = transcripts.read_messages("s-20260720T113000-bbbb", limit=100)
    # Both files were opened and both gave up early; neither raised.
    assert len(page.sources) == 2
    assert 0 < page.total < 10


def test_a_root_that_cannot_be_resolved_does_not_break_containment(
    platform_root, _isolated_transcript_root, monkeypatch
):
    """One unusable root must not stop the other from allowing a pointer.

    The per-session root is checked first, so this makes *that* one unresolvable —
    a symlinked platform log directory pointing at itself — and asserts the
    configured harness root still admits the pointer.
    """
    logs = store.logs_dir()
    logs.parent.mkdir(parents=True, exist_ok=True)
    logs.symlink_to(logs.parent / "logs-loop")
    (logs.parent / "logs-loop").symlink_to(logs)

    pointed = _isolated_transcript_root / "pointed.jsonl"
    pointed.write_text(FOLLOWUP_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    registry.register("s-roots", pid=DEAD_PID, run=dict(RUN))
    registry.update("s-roots", transcript={"path": str(pointed)})

    assert transcripts.read_messages("s-roots").total == 2


def test_blank_lines_in_a_transcript_are_skipped(platform_root):
    target = plant_session("s-blank")
    with target.open("a", encoding="utf-8") as handle:
        handle.write("\n\n   \n")
    assert transcripts.read_messages("s-blank", limit=100).total == 8


def test_a_pathologically_long_line_is_bounded_and_reported(platform_root, caplog):
    """A binary file misnamed .jsonl presents as one enormous "line".

    Reading it whole would put it in the daemon's heap, so the read is chunked —
    and the file is named in the log, because otherwise the only symptom is a chat
    view quietly missing part of itself.
    """
    target = plant_session("s-huge")
    with target.open("a", encoding="utf-8") as handle:
        handle.write("{" + "a" * (transcripts._MAX_LINE_BYTES + 2048) + "\n")
        handle.write(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "still here"},
            ]},
        }) + "\n")
    page = transcripts.read_messages("s-huge", limit=100)
    assert page.messages[-1].text == "still here"
    assert "transcript_line_oversized" in caplog.text
    # Once per file, not once per chunk of it.
    assert caplog.text.count("transcript_line_oversized") == 1


# --- the HTTP route ---------------------------------------------------------

@pytest.fixture
def config(platform_root):
    return cfg.load()


@pytest.fixture
def client(config):
    from fastapi.testclient import TestClient

    return TestClient(
        api.create_app(config, SECRET, state_builder=lambda config, force_pull=False: {})
    )


def bearer_header(token=SECRET):
    return {"Authorization": f"Bearer {token}"}


def test_messages_route_requires_auth(client):
    assert client.get("/api/sessions/s-1/messages").status_code == 401


def test_messages_route_serves_the_normalised_conversation(client, platform_root):
    plant_session("s-http")
    plant_log("s-http")
    response = client.get(
        "/api/sessions/s-http/messages?limit=100", headers=bearer_header()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["session"] == "s-http"
    assert body["live"] is False
    assert body["total"] == 8
    assert body["cursor"] == 8
    assert body["messages"][0]["role"] == "user"
    assert body["sessions"][0]["id"] == "claude-session"


def test_messages_route_serves_a_monitor_event_as_the_watch(client, platform_root):
    """End to end for T99: the role is what the browser keys on, and the payload
    is where it could be dropped."""
    session = "s-watch"
    plant_log(session)
    registry.register(session, pid=DEAD_PID, run=dict(RUN))
    directory = transcripts.session_transcript_dir(session) / "-workspace"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "watch.jsonl").write_text(
        json.dumps(monitor_record()) + "\n", encoding="utf-8"
    )

    response = client.get(
        f"/api/sessions/{session}/messages", headers=bearer_header()
    )
    assert response.status_code == 200
    served = response.json()["messages"]
    assert [(m["role"], m["via"]) for m in served] == [
        (transcripts.MONITOR_ROLE, transcripts.MONITOR_VIA)
    ]
    assert served[0]["text"] == (
        "lmer pending digest count > 0\nfleet digests pending: 1"
    )


def test_messages_route_returns_no_transcript_internals(client, platform_root):
    """A presentation endpoint. None of the transcript's own machinery ships."""
    plant_session("s-clean")
    plant_log("s-clean")
    body = client.get(
        "/api/sessions/s-clean/messages?limit=100", headers=bearer_header()
    ).text
    for leaked in (
        "cache_read_input_tokens", "requestId", "signature", "toolUseResult",
        "old_string", "parentUuid", "glpat-",
    ):
        assert leaked not in body, f"{leaked} reached the wire"
    # Nor the host filesystem layout the transcript was read from.
    assert str(store.platform_dir()) not in body


def test_messages_route_answers_a_session_with_no_transcript(client, platform_root):
    """A log but no transcript — every session on this host today."""
    plant_log("s-logonly")
    response = client.get("/api/sessions/s-logonly/messages", headers=bearer_header())
    assert response.status_code == 200
    body = response.json()
    assert body["messages"] == []
    assert body["note"] == transcripts.NO_TRANSCRIPT_NOTE


def test_messages_route_404s_for_a_session_that_never_existed(client, platform_root):
    """Same answer the log route gives: the session, not the transcript, is missing."""
    response = client.get("/api/sessions/s-nothing/messages", headers=bearer_header())
    assert response.status_code == 404


@pytest.mark.parametrize("bad", ["..", ".hidden", "a%2Fb"])
def test_messages_route_refuses_a_traversal_session_id(client, platform_root, bad):
    response = client.get(f"/api/sessions/{bad}/messages", headers=bearer_header())
    assert response.status_code == 404
    assert "/etc" not in response.text


def test_messages_route_rejects_an_out_of_range_limit(client, platform_root):
    plant_log("s-lim")
    assert client.get(
        "/api/sessions/s-lim/messages?limit=0", headers=bearer_header()
    ).status_code == 422
    assert client.get(
        f"/api/sessions/s-lim/messages?limit={transcripts.MAX_MESSAGE_LIMIT + 1}",
        headers=bearer_header(),
    ).status_code == 422


def test_the_route_list_mentions_the_messages_route(client):
    body = client.get("/api", headers=bearer_header()).text
    assert "/api/sessions/{id}/messages" in body


# --- the chat view's source -------------------------------------------------
#
# Source-level guards, in the spirit of test_platform_web_app.py: there is no JS
# test runner here, so the invariants that would otherwise only fail on a phone
# are asserted against the markup.

WEB = Path(__file__).resolve().parent.parent / "web"
CHAT = WEB / "src" / "components" / "Chat.vue"


def test_the_chat_component_exists_and_is_mounted():
    assert CHAT.is_file()
    detail = (WEB / "src" / "components" / "RunDetail.vue").read_text(encoding="utf-8")
    assert "import Chat from './Chat.vue'" in detail
    assert "<Chat" in detail


def test_the_chat_view_reads_the_transcript_and_writes_the_control_plane():
    """The two halves are different routes with different latencies (spec §10.4)."""
    text = CHAT.read_text(encoding="utf-8")
    assert "fetchSessionMessages" in text
    assert "sendSessionInput" in text
    api_js = (WEB / "src" / "api.js").read_text(encoding="utf-8")
    assert "/messages?" in api_js
    assert "/input`" in api_js


def test_a_sent_message_is_pending_until_the_transcript_has_it():
    """A slow transcript is not a failed send, and must not be shown as one.

    Since issue #254 the pending bubble carries one caption ("sending…", inside
    the grace window) and none after it — the message is assumed delivered and
    the old "not caught up"/"not confirmed" rungs are deliberately gone. The
    detailed contract lives in test_platform_web_chat.py; this guard only keeps
    the pending layer itself present.
    """
    text = CHAT.read_text(encoding="utf-8")
    assert "pending" in text
    assert "sending…" in text
    # The two retired caption strings, verbatim — prose in comments may still
    # say "not confirmed" about the control-plane reply, which is a fact.
    assert "the transcript has not caught up" not in text
    assert "may not have received" not in text


def test_the_composer_has_the_three_states_the_spec_names():
    """send for a live session, answer for a blocked run, nothing for history."""
    text = CHAT.read_text(encoding="utf-8")
    assert "composerMode" in text
    for mode in ("'answer'", "'closed'", "'send'"):
        assert mode in text
    # A question-blocked run gets an explanation, not a box that goes nowhere.
    assert "nothing to type into" in text


def test_the_chat_view_uses_theme_colours_and_m3_typography():
    text = CHAT.read_text(encoding="utf-8")
    assert not [
        line for line in text.splitlines()
        if "#" in line and ("color" in line.lower() or "background" in line.lower())
        and "rgb(var(--v-theme" not in line
    ], "colours come from the theme, never a hex literal"
    # text-caption and text-h6 do not exist in Vuetify 4.
    assert "text-caption" not in text
    assert "text-h6" not in text


def test_the_chat_view_imports_icons_individually():
    """A barrel import of @mdi/js pulls every icon path into the bundle."""
    text = CHAT.read_text(encoding="utf-8")
    assert "from '@mdi/js'" in text
    assert "import * as" not in text


# --- scrubbing the file a finished session left behind (T22) ------------------
#
# The read path masked credentials on their way to a browser, which was enough
# while the transcript died with the container. Now a spawn mounts a host
# directory in, so the file stays — and gets the same scrub applied to it once
# the session's process is gone.

#: A transcript in the shape trouble arrives in: every credential form the
#: patterns claim to catch, plus prose that must survive, plus the torn final
#: line a crash mid-append leaves (carrying a credential of its own).
LEAKY_TRANSCRIPT = "\n".join([
    json.dumps({
        "type": "user",
        "timestamp": "2026-07-26T09:00:00.000Z",
        "message": {
            "role": "user",
            "content": (
                "clone https://oauth2:glpat-AAAABBBBCCCCDDDDEEEEFFFF"
                "@gitlab.example.com/agents/work.git and run the suite"
            ),
        },
    }),
    json.dumps({
        "type": "assistant",
        "timestamp": "2026-07-26T09:00:05.000Z",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "the suite passed"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {
                "command": "lmer chat . --fastapi-token s3cr3t-value-here",
            }},
        ]},
    }),
    json.dumps({
        "type": "system",
        "content": 'curl -H "Authorization: Bearer s3cr3t-value-here" http://localhost',
    }),
    json.dumps({
        "type": "user",
        "message": {"role": "user", "content": "export LMER_FASTAPI_TOKEN=s3cr3t-value-here"},
    }),
    '{"type":"assistant","message":{"role":"assistant","content":"torn line with '
    'glpat-AAAABBBBCCCCDDDDEEEEFFFF in it',
]) + "\n"

#: Every literal the scrub must remove from :data:`LEAKY_TRANSCRIPT`.
LEAKED = ("s3cr3t-value-here", "glpat-AAAA", "oauth2:")


def plant_transcript(session_id, text=LEAKY_TRANSCRIPT, name="session.jsonl"):
    """Write *text* into the platform's own transcript dir for *session_id*."""
    directory = transcripts.session_transcript_dir(session_id) / "-workspace"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text(text, encoding="utf-8")
    return target


def temp_files(directory):
    """Every leftover scrub temp under *directory* (there must never be one)."""
    return sorted(p.name for p in directory.rglob(".*.tmp"))


def scrub_temp_path(target, ident=None):
    """The temp path a scrub of *target* derives, on this thread or *ident*'s.

    In one place because three tests need to plant a file at exactly the path the
    writer will open — a test that guesses the shape wrong stops testing what it
    says it tests, silently. The shape itself is pinned by
    :func:`test_the_scrub_temp_name_carries_the_writers_process_and_thread`.
    """
    ident = threading.get_ident() if ident is None else ident
    return target.with_name(f".{target.name}.{os.getpid()}.{ident}.tmp")


def test_scrubbing_removes_the_credential_shapes_and_keeps_the_conversation(platform_root):
    target = plant_transcript("s-scrub")
    assert transcripts.scrub_session_transcripts("s-scrub") == 1

    text = target.read_text(encoding="utf-8")
    for leaked in LEAKED:
        assert leaked not in text, f"{leaked} survived the scrub"
    # The point of scrubbing rather than deleting: the conversation is still there.
    assert "the suite passed" in text
    assert "gitlab.example.com/agents/work.git" in text
    assert "run the suite" in text


def test_a_transcript_in_a_harness_subdirectory_is_scrubbed_too(platform_root):
    """A spawn mounts one subdirectory per harness now (#280), so a transcript
    sits a level deeper than it used to. The scrub walks the same recursive
    discovery the read path does, and depth must not be what decides whether a
    credential is removed from disk."""
    directory = transcripts.session_transcript_dir("s-sub") / "pi" / "-workspace"
    directory.mkdir(parents=True)
    target = directory / "session.jsonl"
    target.write_text(LEAKY_TRANSCRIPT, encoding="utf-8")

    assert transcripts.scrub_session_transcripts("s-sub") == 1
    text = target.read_text(encoding="utf-8")
    for leaked in LEAKED:
        assert leaked not in text, f"{leaked} survived the scrub"
    assert "the suite passed" in text, "scrubbed, not emptied"


def test_a_scrubbed_transcript_is_still_valid_jsonl(platform_root):
    """The chat view reads this file next. Valid lines must stay parseable."""
    target = plant_transcript("s-valid")
    transcripts.scrub_session_transcripts("s-valid")

    lines = [l for l in target.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 5
    for line in lines[:4]:
        assert isinstance(json.loads(line), dict)
    # The torn line stays torn — it cannot be re-serialised, and dropping it
    # would destroy the evidence of the crash that produced it.
    with pytest.raises(ValueError):
        json.loads(lines[4])
    assert "glpat-AAAA" not in lines[4], "a torn line is where a secret hides best"


def test_the_read_path_still_serves_the_same_conversation_after_a_scrub(platform_root):
    """Scrub then read: the chat view must not notice, beyond the masking."""
    registry.register("s-after", pid=DEAD_PID, run=dict(RUN))
    plant_transcript("s-after")
    before = transcripts.read_messages("s-after", limit=100)
    transcripts.scrub_session_transcripts("s-after")
    after = transcripts.read_messages("s-after", limit=100)
    assert after.to_dict() == before.to_dict()
    assert after.total == 4


def test_a_scrub_does_not_rewrite_the_records_structure(platform_root):
    """Values are scrubbed, never the raw line — the difference is not cosmetic.

    Some patterns end at the first quote and some do not, so a substitution run
    over a whole JSON line can match past a closing quote and swallow the next
    field: ``{"url":"https://host","who":"a@b"}`` collapses to
    ``{"url":"https://b"}`` under the URL-userinfo pattern. Still valid JSON,
    silently missing a field — which is why the scrub decodes first.
    """
    record = {"type": "system", "content": "see https://host", "note": "a@b"}
    target = plant_transcript("s-shape", text=json.dumps(record, separators=(",", ":")) + "\n")

    transcripts.scrub_session_transcripts("s-shape")
    assert json.loads(target.read_text(encoding="utf-8")) == record


def test_a_scrubbed_transcript_is_left_owner_only(platform_root):
    """The container's umask writes 0644; what stays on the host does not."""
    target = plant_transcript("s-mode")
    target.chmod(0o644)
    transcripts.scrub_session_transcripts("s-mode")
    assert stat.S_IMODE(target.stat().st_mode) == transcripts.TRANSCRIPT_FILE_MODE


def test_the_atomic_write_leaves_no_temp_file(platform_root):
    """A leftover temp would be an unscrubbed copy of what was just scrubbed."""
    target = plant_transcript("s-tmp")
    transcripts.scrub_session_transcripts("s-tmp")
    assert temp_files(transcripts.session_transcript_dir("s-tmp")) == []
    assert target.is_file()


def test_a_stale_temp_from_a_crashed_scrub_does_not_block_the_next_one(platform_root):
    """O_EXCL would make one crash leave the file unscrubbable forever."""
    target = plant_transcript("s-stale-tmp")
    stale = scrub_temp_path(target)
    stale.write_text("half a scrub", encoding="utf-8")

    assert transcripts.scrub_transcript(target) is True
    assert "s3cr3t-value-here" not in target.read_text(encoding="utf-8")
    assert not stale.exists()


def test_every_transcript_of_a_session_is_scrubbed(platform_root):
    """The harness writes one file per session, and a run can respawn into more."""
    first = plant_transcript("s-many", name="one.jsonl")
    second = plant_transcript("s-many", name="two.jsonl")
    assert transcripts.scrub_session_transcripts("s-many") == 2
    for target in (first, second):
        assert "s3cr3t-value-here" not in target.read_text(encoding="utf-8")


def test_scrubbing_a_session_with_no_transcript_is_not_fatal(platform_root):
    """The normal case for a session whose container never wrote one."""
    assert transcripts.scrub_session_transcripts("s-nothing-here") == 0


def test_scrubbing_an_unreadable_transcript_is_not_fatal(platform_root, caplog):
    """A file written by another uid inside the container reads as absent."""
    target = plant_transcript("s-locked-scrub")
    target.chmod(0o000)
    try:
        # Injected rather than left to the mode bit — see the read-path twin.
        with denied_read(target):
            assert transcripts.scrub_session_transcripts("s-locked-scrub") == 0
    finally:
        target.chmod(0o644)
    assert "transcript_scrub_unreadable" in caplog.text
    # Untouched rather than truncated: an unscrubbable transcript is still one
    # the chat view should show (and the read path masks it again on the way out).
    assert "s3cr3t-value-here" in target.read_text(encoding="utf-8")


def test_a_write_failure_leaves_the_original_intact_and_no_temp(
    platform_root, monkeypatch, caplog
):
    """A partially rewritten transcript is worse than an unscrubbed one.

    Injected: the write half of an atomic replace cannot be made to fail from the
    filesystem without also breaking the read, and this is the property the
    temp-file-plus-rename exists for.
    """
    target = plant_transcript("s-wfail")
    before = target.read_text(encoding="utf-8")

    def no_replace(*_args, **_kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(transcripts.os, "replace", no_replace)
    assert transcripts.scrub_transcript(target) is False
    assert target.read_text(encoding="utf-8") == before
    assert temp_files(transcripts.session_transcript_dir("s-wfail")) == []
    assert "transcript_scrub_failed" in caplog.text


def test_scrubbing_never_follows_a_pointer_out_of_the_platform_s_own_directory(
    platform_root, _isolated_transcript_root
):
    """This function *mutates* what it reads, so it only touches what it owns.

    ``locate_sources`` will follow a recorded pointer into the invoking user's own
    ``~/.claude/projects``; rewriting a developer's real transcripts in place is
    not something a session ending may do.
    """
    outside = _isolated_transcript_root / "-workspace" / "mine.jsonl"
    outside.parent.mkdir(parents=True)
    outside.write_text(LEAKY_TRANSCRIPT, encoding="utf-8")
    registry.register("s-pointer", pid=DEAD_PID, run=dict(RUN))
    registry.update("s-pointer", transcript={"path": str(outside)})

    assert transcripts.scrub_session_transcripts("s-pointer") == 0
    assert "s3cr3t-value-here" in outside.read_text(encoding="utf-8")


def test_a_symlink_in_the_transcript_directory_is_refused(platform_root, tmp_path, caplog):
    """The directory is mounted read-write into a container, so a link in it
    could have been planted by the session being observed — and following one
    would copy an unrelated file into this session's chat view."""
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text(LEAKY_TRANSCRIPT, encoding="utf-8")
    directory = transcripts.session_transcript_dir("s-link") / "-workspace"
    directory.mkdir(parents=True)
    (directory / "planted.jsonl").symlink_to(outside)

    assert transcripts.scrub_session_transcripts("s-link") == 0
    assert "transcript_link_refused" in caplog.text
    assert "s3cr3t-value-here" in outside.read_text(encoding="utf-8")


def test_a_planted_symlink_is_not_read_back_as_this_session_s_transcript(
    platform_root, tmp_path, caplog,
):
    """The same link, on the *read* path: ``GET /api/sessions/{id}/messages``
    must not serve another run's conversation because the observed session
    dropped a link into the directory mounted into it. Transcripts are the
    artifact most likely to carry another run's credentials."""
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text(LEAKY_TRANSCRIPT, encoding="utf-8")
    registry.register("s-link-read", pid=DEAD_PID, run=dict(RUN))
    directory = transcripts.session_transcript_dir("s-link-read") / "-workspace"
    directory.mkdir(parents=True)
    (directory / "planted.jsonl").symlink_to(outside)

    page = transcripts.read_messages("s-link-read")

    assert page.sources == ()
    assert page.messages == ()
    assert "transcript_link_refused" in caplog.text


@pytest.mark.parametrize("bad", ["../../etc/passwd", "..", "a/b", ".hidden", ""])
def test_scrubbing_refuses_a_session_id_that_could_name_a_path(platform_root, bad):
    """One notion of a legal session id, the same one every entry point uses."""
    with pytest.raises(transcripts.SessionNotFound):
        transcripts.scrub_session_transcripts(bad)


def test_scrubbing_a_transcript_twice_is_a_no_op(platform_root):
    """A second exit event, or a re-run of the cleanup, must not corrupt anything."""
    target = plant_transcript("s-twice")
    transcripts.scrub_session_transcripts("s-twice")
    once = target.read_text(encoding="utf-8")
    transcripts.scrub_session_transcripts("s-twice")
    assert target.read_text(encoding="utf-8") == once


def test_a_pathologically_long_line_survives_the_scrub(platform_root):
    """A line too long to parse is scrubbed as text, not dropped."""
    target = plant_transcript(
        "s-long",
        text=(
            "{" + "a" * (transcripts._MAX_LINE_BYTES + 2048)
            + " token=s3cr3t-value-here\n"
        ),
    )
    assert transcripts.scrub_transcript(target) is True
    text = target.read_text(encoding="utf-8")
    assert len(text) > transcripts._MAX_LINE_BYTES
    assert "s3cr3t-value-here" not in text


def test_the_container_side_mount_destination_is_the_harness_projects_dir():
    """What a spawn mounts and what this module reads are one fact.

    The container's HOME is pinned by the host CLI's env dict; the layout under
    it is claude's own, and it is the same constant the reader's default root is
    built from.
    """
    assert transcripts.CONTAINER_TRANSCRIPT_DIR == "/home/developer/.claude/projects"
    assert transcripts.CONTAINER_TRANSCRIPT_DIR.endswith(str(transcripts._DEFAULT_ROOT))


# --- two writers, one transcript (T59) ---------------------------------------
#
# The atomic replace above named its temp for the *process*, which was enough
# while every writer was its own process. The daemon serves sync handlers from a
# threadpool, so one process is many writers — and two of them deriving one temp
# path do not merely lose a write. Both truncate it, one renames it onto the
# transcript, and the loser is then holding an open fd on the destination inode:
# its bytes land on top of the file just published, and only then does its own
# rename fail ENOENT. The result is a torn transcript produced by the mechanism
# that exists to prevent one, published as a success.
#
# Byte-identical to the bug T58 fixed in ``store.write_json``, in the file where
# a tear costs more: this is the credential-scrubbed artifact, and this pass is
# the last thing to touch it before it outlives its container.

#: Copies of :data:`LEAKY_TRANSCRIPT` in a transcript too big to sit in one
#: buffer. The writer wraps the temp fd in a text wrapper, so a *small*
#: transcript reaches the filesystem in a single flush at close and a shared temp
#: path looks deceptively atomic. This many is ~14 KB of output — more than one
#: flush, which is what lets a second writer's ``O_TRUNC`` land in the *middle*
#: of the first writer's file instead of before its first byte.
BULKY_COPIES = 20

BULKY_TRANSCRIPT = LEAKY_TRANSCRIPT * BULKY_COPIES

#: Lines and decoded records in :data:`BULKY_TRANSCRIPT`: five lines per copy, of
#: which four are records — the fifth is the torn one, scrubbed as raw text.
BULKY_LINES = 5 * BULKY_COPIES
BULKY_RECORDS = 4 * BULKY_COPIES


def test_the_scrub_temp_name_carries_the_writers_process_and_thread(
    platform_root, monkeypatch
):
    """Two writers must never derive one temp path, and a pid is only half of it.

    The pid stays. It is what keeps two *processes* apart, and swapping the pair
    for a random suffix (``ask_channel.protocol``'s shape) would drop a property
    to gain nothing.
    """
    target = plant_transcript("s-tmp-name")
    real_open = os.open
    opened = []

    def capture(path, *args, **kwargs):
        opened.append(Path(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(transcripts.os, "open", capture)
    assert transcripts.scrub_transcript(target) is True

    # Every path, unfiltered: filtering on the shape being asserted would let a
    # temp named wrongly disappear from the list instead of failing a check.
    assert len(opened) == 1, opened
    tmp = opened[0]
    assert tmp.parent == target.parent, "a rename out of the directory is a copy"
    assert tmp.name.startswith("."), "a visible temp reads as a transcript"
    assert tmp.name.endswith(".tmp"), "what keeps it out of _jsonl_files"
    parts = tmp.name.split(".")
    assert str(os.getpid()) in parts, "two processes must still not collide"
    assert str(threading.get_ident()) in parts, "nor two threads of one process"
    assert tmp == scrub_temp_path(target), (
        "the path the other tests plant at has drifted from the one the writer opens"
    )


def test_two_threads_scrubbing_one_transcript_both_succeed(
    platform_root, monkeypatch, caplog
):
    """Both writers must land, and what survives must be one of them, whole.

    The barrier holds both threads with their temp written and closed and neither
    renamed — the interleaving in question — rather than trusting a one-CPU
    scheduler to produce it. A shared temp path shows up here as the loser
    renaming a name the winner has already taken: ENOENT, logged as a failed
    scrub, and returned to the caller as ``False`` for a transcript it never
    scrubbed.
    """
    target = plant_transcript("s-race", text=BULKY_TRANSCRIPT)
    real_replace = os.replace
    both_temps_written = threading.Barrier(2, timeout=30)

    def replace_once_both_temps_exist(source, destination):
        both_temps_written.wait()
        return real_replace(source, destination)

    monkeypatch.setattr(transcripts.os, "replace", replace_once_both_temps_exist)
    results, errors = [], []

    def scrub():
        try:
            results.append(transcripts.scrub_transcript(target))
        except BaseException as exc:  # pragma: no cover - a failure prints the reason
            errors.append(exc)

    threads = [threading.Thread(target=scrub, name=f"scrub-{i}") for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert not [thread for thread in threads if thread.is_alive()], "a writer hung"
    assert not errors, errors
    assert results == [True, True], f"a writer reported failure: {caplog.text}"
    assert "transcript_scrub_failed" not in caplog.text

    text = target.read_text(encoding="utf-8")
    for leaked in LEAKED:
        assert leaked not in text, f"{leaked} survived the race"
    assert "\x00" not in text, "a hole is what one writer truncating another leaves"
    assert len([line for line in text.splitlines() if line.strip()]) == BULKY_LINES
    assert temp_files(transcripts.session_transcript_dir("s-race")) == []


def test_a_concurrent_reader_never_sees_a_half_scrubbed_transcript(platform_root):
    """The property temp-plus-rename exists to provide, under a real race.

    Three threads re-scrub one transcript while a reader hammers it. The scrub is
    byte-idempotent (``test_scrubbing_a_transcript_twice_is_a_no_op``), so once
    the first pass is done every state a reader may see is identical to every
    other, and *any* difference is a state no writer meant to publish. The
    distinct inodes count the publications the reader actually caught: a reader
    that never saw the file change would prove nothing.
    """
    target = plant_transcript("s-reader", text=BULKY_TRANSCRIPT)
    assert transcripts.scrub_transcript(target) is True
    expected = target.read_bytes()
    for leaked in LEAKED:
        assert leaked.encode() not in expected

    writers, rounds = 3, 5
    start = threading.Barrier(writers + 1, timeout=30)
    stop = threading.Event()
    errors, torn, refused, inodes = [], [], [], []
    reads = 0

    def scrub(index):
        try:
            start.wait()
            for _ in range(rounds):
                if transcripts.scrub_transcript(target) is not True:
                    refused.append(index)
        except BaseException as exc:  # pragma: no cover - a failure prints the reason
            errors.append(exc)

    def read():
        nonlocal reads
        try:
            start.wait()
            while not stop.is_set():
                reads += 1
                try:
                    with target.open("rb") as handle:
                        inode = os.fstat(handle.fileno()).st_ino
                        raw = handle.read()
                except FileNotFoundError:
                    torn.append(b"<the transcript was not there>")
                    continue
                if not inodes or inodes[-1] != inode:
                    inodes.append(inode)
                if raw != expected:
                    torn.append(raw)
        except BaseException as exc:  # pragma: no cover - a failure prints the reason
            errors.append(exc)

    threads = [
        threading.Thread(target=scrub, args=(index,), name=f"scrub-{index}")
        for index in range(writers)
    ]
    reader = threading.Thread(target=read, name="reader")
    reader.start()
    for thread in threads:
        thread.start()
    try:
        for thread in threads:
            thread.join(timeout=180)
    finally:
        stop.set()
        reader.join(timeout=60)

    assert not errors, errors
    assert reads, "the reader never got a look in"
    assert len(inodes) > 1, f"the reader never caught a rewrite ({reads} reads)"
    # The torn reads first, the writers' own verdict second: a writer reporting
    # failure is the symptom, and what the reader saw is the property.
    assert not torn, (
        f"the reader saw {len(torn)} half-scrubbed transcript(s) in {reads} reads; "
        f"the first was {len(torn[0])} bytes against {len(expected)} expected: "
        f"{torn[0][:120]!r}"
    )
    assert not refused, f"{len(refused)} scrub(s) reported failure (writers {refused})"
    assert target.read_bytes() == expected
    assert temp_files(transcripts.session_transcript_dir("s-reader")) == []


def test_a_writer_held_mid_file_cannot_have_its_temp_truncated(
    platform_root, monkeypatch
):
    """The mechanism itself, forced rather than sampled — and what a tear costs.

    Three handoffs, in this order: the first writer stops partway through with a
    buffer already on the filesystem; the second opens its temp (nothing more) and
    is held there; the first finishes and publishes while the second's fd is still
    open. The order is the whole point — a truncation that lands *before* the
    first writer's first byte costs nothing, and a scheduler will happily produce
    that one instead. On one temp path, the second's ``O_TRUNC`` lands in the
    middle of the first writer's file, so what the first publishes is a transcript
    with a hole where its opening was, and it returns ``True`` on it.

    The unscrubbed check is here because this is the credential scrubber and a
    torn publication must not be a route back to raw material. It cannot be, and
    the reason is worth writing down rather than assuming: every byte either
    writer emits has already been through :func:`transcripts._scrub`, so what a
    tear destroys is the transcript's completeness, not its scrubbing.
    """
    reference = plant_transcript("s-handoff-ref", text=BULKY_TRANSCRIPT)
    assert transcripts.scrub_transcript(reference) is True
    expected = reference.read_bytes()
    target = plant_transcript("s-handoff", text=BULKY_TRANSCRIPT)

    # Three quarters of the way in: past the writer's first flush (measured, the
    # text wrapper reaches the filesystem around record 46 of 80) with records
    # still to come, so a truncation here lands mid-file. Not left on trust — the
    # `flushed` assertion below fails the test if there was nothing on disk yet.
    hold_after = (BULKY_RECORDS * 3) // 4
    real_open, real_scrub_decoded = os.open, transcripts._scrub_decoded
    first_is_mid_file = threading.Event()
    second_holds_its_temp = threading.Event()
    published = threading.Event()
    temps, flushed, decoded, nested = {}, [], {"first": 0}, set()

    def gated_open(path, *args, **kwargs):
        # The scrub's only os.open is its temp (it reads through Path.open), and
        # the gate is keyed on the thread name, so nothing else is caught here.
        name = threading.current_thread().name
        if name == "second":
            # Not before the other writer has bytes on disk to lose.
            first_is_mid_file.wait(timeout=60)
        fd = real_open(path, *args, **kwargs)
        temps[name] = Path(path)
        if name == "second":
            # Its O_TRUNC has landed by now; hold the fd open across the other
            # writer's rename, which is the whole scenario.
            second_holds_its_temp.set()
            published.wait(timeout=60)
        return fd

    def gated_scrub_decoded(value):
        ident = threading.get_ident()
        if ident in nested:  # the real one recurses through this same name
            return real_scrub_decoded(value)
        nested.add(ident)
        try:
            if threading.current_thread().name == "first":
                decoded["first"] += 1
                if decoded["first"] == hold_after:
                    flushed.append(temps["first"].stat().st_size)
                    first_is_mid_file.set()
                    second_holds_its_temp.wait(timeout=60)
            return real_scrub_decoded(value)
        finally:
            nested.discard(ident)

    monkeypatch.setattr(transcripts.os, "open", gated_open)
    monkeypatch.setattr(transcripts, "_scrub_decoded", gated_scrub_decoded)
    results, errors = {}, []

    def scrub(name):
        try:
            results[name] = transcripts.scrub_transcript(target)
        except BaseException as exc:  # pragma: no cover - a failure prints the reason
            errors.append(exc)

    first = threading.Thread(target=scrub, args=("first",), name="first")
    second = threading.Thread(target=scrub, args=("second",), name="second")
    first.start()
    second.start()
    first.join(timeout=120)

    assert not first.is_alive(), "the first writer never published"
    assert not errors, errors
    assert second.is_alive(), "the second writer was supposed to still be holding on"
    # Read here, not at the end: this is the moment a chat-view poll would have
    # landed in, with one writer's fd still open on a file that has been renamed.
    mid_race = target.read_bytes()
    published.set()
    second.join(timeout=120)

    assert not errors, errors
    assert flushed and flushed[0] > 0, (
        "the first writer had nothing on disk yet, so this proved nothing"
    )
    assert b"\x00" not in mid_race, "a hole is what a second writer's O_TRUNC leaves"
    assert mid_race == expected, "what was published was not what the writer wrote"
    for leaked in LEAKED:
        assert leaked.encode() not in mid_race, f"{leaked} came back in a torn write"
    # The writers' own verdict last: a False here is the symptom of the tear
    # above, and reading it first would hide what was published.
    assert results == {"first": True, "second": True}
    assert target.read_bytes() == expected
    assert temp_files(transcripts.session_transcript_dir("s-handoff")) == []


def test_a_stale_temp_from_another_thread_is_neither_reused_nor_removed(platform_root):
    """The cost of naming the thread, stated rather than left to be discovered.

    A temp whose name carries another thread's id may belong to a *live* writer,
    so it is never opened ``O_TRUNC`` and never unlinked — which means a leftover
    from a crashed scrub now outlives the next scrub instead of being reused by
    it. That is the safe direction, and what it leaves behind is already-scrubbed
    content in a 0600 file inside a 0700 directory, invisible to
    :func:`transcripts._jsonl_files` and gone when the session's log directory is.
    """
    target = plant_transcript("s-other-thread-tmp")
    sibling = scrub_temp_path(target, ident=threading.get_ident() + 1)
    sibling.write_text("half a scrub", encoding="utf-8")

    assert transcripts.scrub_transcript(target) is True
    assert "s3cr3t-value-here" not in target.read_text(encoding="utf-8")
    assert sibling.read_text(encoding="utf-8") == "half a scrub", (
        "a temp that could belong to a live writer must not be touched"
    )
    # And it is still not a transcript, so the session's next scrub ignores it.
    assert transcripts.scrub_session_transcripts("s-other-thread-tmp") == 1


# --- the platform's own shared secret (T30) ----------------------------------
#
# Every other pattern in this module catches a credential because of what
# SURROUNDS it — a scheme, a header, an option name, a variable name. That is
# not enough for this one. The orchestrating assistant now holds the platform's
# shared secret in its environment (lmer_platform.assistant), and a shared
# secret has no prefix and no giveaway: an agent that runs `env`, or pastes it
# into a query string, or simply quotes it in a sentence, leaves a
# container-spawning credential in a file that outlives its container and is
# served to a browser. So this one is matched by VALUE.

#: A transcript that leaks the secret in the four shapes nothing shaped catches.
def leaky_secret_transcript(secret):
    return "\n".join([
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": f"the platform answered; its key is {secret}"},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {
                    "command": f"env | grep PLATFORM   # {secret}",
                }},
            ]},
        }),
        json.dumps({
            "type": "user",
            "message": {"role": "user", "content": f"LMER_PLATFORM_SECRET={secret}"},
        }),
        json.dumps({
            "type": "system",
            "content": f"curl '$LMER_PLATFORM_URL/api/state?token={secret}'",
        }),
    ]) + "\n"


@pytest.fixture
def platform_secret(platform_root):
    """The real thing: whatever ``ensure_secret`` generated for this host."""
    return cfg.ensure_secret(cfg.load())


def test_the_configured_secret_does_not_survive_a_transcript(
    platform_root, platform_secret
):
    """The T18 requirement, against the actual configured value.

    Four shapes, none of which any credential *pattern* recognises: prose, a
    trailing shell comment, a bare ``NAME=value`` — that one the shape rules do
    catch, and it is here so a regression cannot hide behind it — and a query
    parameter.
    """
    assert len(platform_secret) >= 32, "a generated secret, not a placeholder"
    target = plant_transcript("s-secret", text=leaky_secret_transcript(platform_secret))

    assert transcripts.scrub_session_transcripts("s-secret") == 1

    text = target.read_text(encoding="utf-8")
    assert platform_secret not in text, "the platform's shared secret survived"
    # Scrubbed, not deleted: the conversation is still there to read.
    assert "the platform answered" in text
    assert "env | grep PLATFORM" in text


def test_the_read_path_masks_the_secret_before_a_browser_sees_it(
    platform_root, platform_secret
):
    """The file scrub runs when the session ends; the chat view serves it live."""
    plant_transcript("s-live", text=leaky_secret_transcript(platform_secret))
    registry.register("s-live", pid=DEAD_PID)

    page = transcripts.read_messages("s-live")

    assert page.total
    assert platform_secret not in json.dumps(page.to_dict())


def test_a_rotated_secret_is_picked_up_without_a_restart(
    platform_root, platform_secret
):
    """The resolution is memoized on the file's stat, not for the process's life.

    Without the invalidation the first value read would be masked forever and
    the current one never — which is the wrong way round.
    """
    assert transcripts._platform_secret() == platform_secret
    rotated = "rotated-secret-with-enough-length"
    cfg.load().secret_path.write_text(rotated + "\n", encoding="utf-8")

    target = plant_transcript("s-rotated", text=leaky_secret_transcript(rotated))
    assert transcripts.scrub_session_transcripts("s-rotated") == 1
    assert rotated not in target.read_text(encoding="utf-8")


def test_the_secret_is_found_where_the_operator_moved_it(
    platform_root, tmp_path, monkeypatch
):
    """``LMER_PLATFORM_SECRET_FILE`` moves the file; the scrub has to follow."""
    monkeypatch.setenv(cfg.ENV_SECRET_FILE, str(tmp_path / "elsewhere" / "sec"))
    secret = cfg.ensure_secret(cfg.load())

    target = plant_transcript("s-elsewhere", text=leaky_secret_transcript(secret))
    assert transcripts.scrub_session_transcripts("s-elsewhere") == 1
    assert secret not in target.read_text(encoding="utf-8")


def test_a_secret_short_enough_to_be_prose_is_not_struck_out_by_value(
    platform_root, caplog
):
    """Redacting a short hand-written "secret" would make transcripts unreadable.

    The name-based rule still masks it where it is *named*, which is what the
    last assertion checks: skipping the value match is not skipping the scrub.
    Ten characters, deliberately: under the value threshold and over the eight
    the ``NAME=value`` pattern needs, so both halves say something.
    """
    import logging

    caplog.set_level(logging.DEBUG, logger="lmer_platform.transcripts")
    short = "hunter2000"
    assert 8 <= len(short) < transcripts._MIN_SECRET_CHARS
    cfg.load().secret_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.load().secret_path.write_text(short + "\n", encoding="utf-8")

    assert transcripts._platform_secret() is None
    assert any(
        "platform_transcript_secret_too_short" in r.message for r in caplog.records
    )
    assert transcripts._scrub(f"{short} is not a word to redact") == (
        f"{short} is not a word to redact"
    )
    assert short not in transcripts._scrub(f"LMER_PLATFORM_SECRET={short}")


def test_a_host_with_no_secret_scrubs_exactly_as_before(platform_root):
    """Most hosts running this reader have no platform secret at all."""
    assert cfg.read_secret(cfg.load()) is None
    assert transcripts._platform_secret() is None
    assert transcripts._scrub("nothing to see") == "nothing to see"


def test_an_unreadable_secret_file_does_not_break_the_chat_view(
    platform_root, monkeypatch, caplog
):
    """A 500 on the chat view is a worse outcome than an unmasked transcript."""
    cfg.ensure_secret(cfg.load())

    def boom(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("pathlib.Path.read_text", boom)
    assert transcripts._platform_secret() is None
    assert any("platform_secret_unreadable" in r.message for r in caplog.records)


# --- which end of a long message survives ------------------------------------
#
# The operator asked: "conversation is 100% useless if its cut off at the tail, so
# if we have to trim it should be at the head". An agent's turn ends with its
# conclusion — what
# it did, what it found, what it wants next. Keeping the opening and dropping that
# leaves the preamble and throws the point away.

def test_a_long_message_keeps_its_ending_not_its_opening():
    conclusion = "THE ANSWER IS 42 and here is what I did about it."
    text = ("preamble that goes on. " * 900) + conclusion

    presented, truncated = transcripts._present(
        text, transcripts.TEXT_LIMIT, keep="tail"
    )

    assert truncated
    assert presented.endswith(conclusion), (
        "the conclusion was cut; a report without its ending is not a shorter "
        "report, it is a useless one"
    )
    assert len(presented) <= transcripts.TEXT_LIMIT


def test_a_one_line_hint_keeps_its_opening():
    """The opposite end, and why this is a parameter rather than a policy.

    A tool line is a command or a path: the beginning names it, the end is an
    argument. Keeping the tail of one shows the last flag of something unnamed.
    """
    hint = "pytest tests/test_platform_transcripts.py " + ("-k something " * 40)

    # Through _first_line, not _present: _first_line is what tool hints actually
    # go through, and asserting on _present directly would pass no matter what
    # _first_line asked for. (Found by mutation — the earlier version of this test
    # did exactly that and missed the change.)
    presented = transcripts._first_line(hint)

    assert presented.startswith("pytest tests/"), (
        f"the hint lost what identifies it: {presented[:60]!r}"
    )
    assert len(presented) <= transcripts.DETAIL_LIMIT


def test_every_message_path_keeps_the_ending():
    """Both record shapes, because only one of them being right is the bug.

    There are two places a message's prose is presented, and a reader hits
    whichever one their harness wrote. A fix applied to one is a fix that works
    for some sessions and not others.
    """
    marker = "FINAL-LINE-MARKER"
    body = ("x" * (transcripts.TEXT_LIMIT + 5000)) + marker

    # The two record shapes that reach the two separate _present calls: a system
    # record carries its prose directly, while user/assistant records nest it under
    # `message`. A reader hits whichever one their harness wrote, so fixing one is
    # fixing it for some sessions and not others.
    for record in (
        {"type": "system", "content": body},
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": body}]},
        },
    ):
        messages = transcripts.normalise_records([record])
        assert messages, f"no message produced for {record.get('type')}"
        message = messages[0]
        assert message.truncated
        assert message.text.endswith(marker), (
            f"{record.get('type')} messages still lose their ending"
        )


def test_the_ceiling_is_generous_enough_for_an_ordinary_report():
    """1500 was ~three paragraphs, so it trimmed almost every real agent turn.

    Not an arbitrary number: tool payloads are bounded separately by DETAIL_LIMIT,
    and the chat pane bounds and scrolls each message, so the only remaining reason
    for a limit is payload size over a LAN.
    """
    assert transcripts.TEXT_LIMIT >= 4000, (
        "a ceiling this low trims ordinary prose, which is the one thing the chat "
        "view exists to show"
    )
    assert transcripts.DETAIL_LIMIT < transcripts.TEXT_LIMIT, (
        "a tool's one-line hint must stay far tighter than a message's prose"
    )


# --- last_turn: the bounded read halt detection asks for (#243) --------------
#
# One question, one file, one tail. The properties that matter here are the ones
# whose failure is silent: a read that is not actually bounded (a fleet poll then
# re-reads megabytes per stalled run), and a "nothing" that is indistinguishable
# from "nothing was said" (the caller would put a run on the attention list for
# the wrong reason, or fail to).

def turn_line(role, text, *, at="2026-08-06T20:00:00Z"):
    """One transcript record in the harness's own shape."""
    return json.dumps({
        "type": role,
        "timestamp": at,
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    })


def plant_turns(session_id, lines, *, name="session"):
    directory = transcripts.session_transcript_dir(session_id) / "-workspace"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.jsonl"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def test_last_turn_reads_the_newest_turn(platform_root):
    registry.register("s-lt1", pid=DEAD_PID, run=dict(RUN))
    plant_turns("s-lt1", [
        turn_line("user", "please do the thing"),
        turn_line("assistant", "on it"),
        turn_line("user", "and this too"),
    ])
    turn = transcripts.last_turn("s-lt1")
    assert turn is not None
    assert turn.role == "user"
    assert turn.text == "and this too"


def test_last_turn_reads_a_tail_not_the_whole_file(platform_root):
    """The bound is the reason this function exists rather than read_messages.

    A stalled run stays stalled, and the fleet view is polled from a phone — so
    this is called again for the same session every poll. Proven by planting a
    file far larger than the tail and asserting the read stops at the bound:
    the earliest turns must be unreachable, which is exactly what a seek buys.
    """
    filler = [turn_line("assistant", "x" * 4096) for _ in range(200)]
    path = plant_turns("s-lt2", [turn_line("user", "the first thing ever said")]
                       + filler + [turn_line("assistant", "the newest thing")])
    registry.register("s-lt2", pid=DEAD_PID, run=dict(RUN))
    assert path.stat().st_size > transcripts.LAST_TURN_TAIL_BYTES, (
        "the fixture is smaller than the bound, so this proves nothing"
    )

    records = transcripts._tail_records(
        path, tail_bytes=transcripts.LAST_TURN_TAIL_BYTES
    )
    texts = json.dumps(records)
    assert "the newest thing" in texts
    assert "the first thing ever said" not in texts, (
        "the whole file was read; the tail bound is not being applied"
    )
    assert transcripts.last_turn("s-lt2").text == "the newest thing"


def test_a_seeked_read_drops_its_first_partial_record(platform_root):
    """A tail almost never starts on a record boundary, and half a JSON object
    is not an error to report — it is a line to drop."""
    path = plant_turns("s-lt3", [
        turn_line("assistant", "y" * 2048),
        turn_line("user", "the last word"),
    ])
    records = transcripts._tail_records(path, tail_bytes=600)
    assert records, "everything was dropped, including the complete final record"
    assert records[-1]["message"]["content"][0]["text"] == "the last word"


def test_a_torn_final_line_does_not_lose_the_turn_before_it(platform_root):
    """The harness appends while this reads, so the newest line can be half
    written. That must cost the torn record, never the answer."""
    directory = transcripts.session_transcript_dir("s-lt4") / "-workspace"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "session.jsonl").write_text(
        turn_line("user", "answer me") + "\n" + '{"type": "assist',
        encoding="utf-8",
    )
    registry.register("s-lt4", pid=DEAD_PID, run=dict(RUN))
    turn = transcripts.last_turn("s-lt4")
    assert turn is not None and turn.text == "answer me"


def test_last_turn_prefers_the_file_being_appended_to(platform_root):
    """A session can have several transcript files; the live one is the newest
    by modification time, and that is the conversation it is in."""
    registry.register("s-lt5", pid=DEAD_PID, run=dict(RUN))
    old = plant_turns("s-lt5", [turn_line("assistant", "old business")], name="a")
    new = plant_turns("s-lt5", [turn_line("user", "current business")], name="b")
    os.utime(old, (1_600_000_000, 1_600_000_000))
    os.utime(new, (1_700_000_000, 1_700_000_000))
    assert transcripts.last_turn("s-lt5").text == "current business"


@pytest.mark.parametrize("plant", ["nothing", "garbage", "empty"])
def test_no_readable_turn_is_none_rather_than_a_guess(platform_root, plant):
    """Three ways of not knowing, all ordinary — a session with no transcript
    mounted out, a file of noise, an empty one.

    ``None`` has to mean "no opinion" here, because the caller is deciding
    whether to put a run on the attention list. Anything that turned "I cannot
    read this" into a turn would flag runs on the strength of a file this build
    does not understand.
    """
    registry.register("s-lt6", pid=DEAD_PID, run=dict(RUN))
    if plant == "garbage":
        plant_turns("s-lt6", ["not json at all", "{also: not}"])
    elif plant == "empty":
        plant_turns("s-lt6", [""])
    assert transcripts.last_turn("s-lt6") is None


def test_an_unreadable_transcript_is_none_not_an_exception(platform_root):
    """Whatever the caller is, it is on a poll path that must not raise."""
    registry.register("s-lt7", pid=DEAD_PID, run=dict(RUN))
    path = plant_turns("s-lt7", [turn_line("user", "hello")])
    with denied_read(path):
        assert transcripts.last_turn("s-lt7") is None


def test_a_session_with_no_transcript_at_all_is_none(platform_root):
    registry.register("s-lt8", pid=DEAD_PID, run=dict(RUN))
    assert transcripts.last_turn("s-lt8") is None


def test_a_tool_result_alone_is_not_the_newest_turn(platform_root):
    """The normaliser folds a result onto the call that made it, so a session
    whose last record is a tool result is still *in* its assistant turn — and
    the answer must be that turn, not nothing."""
    registry.register("s-lt9", pid=DEAD_PID, run=dict(RUN))
    directory = transcripts.session_transcript_dir("s-lt9") / "-workspace"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "session.jsonl").write_text("\n".join([
        json.dumps({
            "type": "assistant",
            "timestamp": "2026-08-06T20:00:00Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": {"command": "gate-check"}},
            ]},
        }),
        json.dumps({
            "type": "user",
            "timestamp": "2026-08-06T20:05:00Z",
            "toolUseResult": {},
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            ]},
        }),
    ]) + "\n", encoding="utf-8")
    turn = transcripts.last_turn("s-lt9")
    assert turn is not None and turn.role == "assistant"
    assert turn.tools and turn.tools[0].status == "ok"


# --- the provider's refusal, as the harness records it (#243) -----------------
#
# Captured rather than invented, like the rest of this file's fixtures: a real
# Claude Code (2.1.221) was pointed at an endpoint answering 400 with a billing
# error, and at one answering 529 until its retries were exhausted. What it wrote
# is claude-api-error.jsonl, and halt detection reads exactly these fields.
# Message ids and timestamps were normalised to the directory's synthetic
# convention afterwards (#268); shape and every other value are the capture.

API_ERROR_FIXTURE = FIXTURES / "claude-api-error.jsonl"


def test_a_recorded_refusal_carries_its_class_and_status():
    """The fields the harness commits to, and what they mean.

    ``isApiErrorMessage`` is the gate; ``error`` is the provider's own class and
    ``apiErrorStatus`` the HTTP status. Halt detection rests on this being a
    *field* rather than a reading of the sentence, so a change of shape here has
    to fail a test rather than quietly turn detection into prose-matching.
    """
    messages = transcripts.normalise_records(records(API_ERROR_FIXTURE))
    assert [(m.api_error, m.api_error_status) for m in messages] == [
        ("billing_error", 400), ("server_error", 529),
    ]
    assert all(m.role == "assistant" for m in messages), (
        "the harness writes its refusal as an assistant turn; the api_error path "
        "only trusts the marker on that role"
    )
    assert "Credit balance is too low" in messages[0].text
    assert "529 Overloaded" in messages[1].text


def test_an_ordinary_turn_carries_no_refusal_marker():
    """The other direction, which is the one that matters: nothing in an ordinary
    conversation may look like a refusal, however it is worded."""
    messages = transcripts.normalise_records(records())
    assert all(m.api_error is None for m in messages)
    assert all(m.api_error_status is None for m in messages)


def test_the_refusal_marker_crosses_to_a_client():
    page = transcripts.normalise_records(records(API_ERROR_FIXTURE))[0].to_dict()
    assert page["api_error"] == "billing_error"
    assert page["api_error_status"] == 400


@pytest.mark.parametrize("record,expected", [
    ({"isApiErrorMessage": True, "error": "billing_error", "apiErrorStatus": 400},
     (True, "billing_error", 400)),
    # Marked, but a build that records neither detail: still a refusal. The flag
    # is returned in its own right for exactly this row — deriving "was this a
    # refusal" from the detail fields made this case read as an ordinary turn
    # while three comments said otherwise (review of !205, iteration 2).
    ({"isApiErrorMessage": True}, (True, None, None)),
    # The flag is the gate — detail without it is not a refusal, because those
    # keys could mean anything in a record shape this build has never seen.
    ({"error": "billing_error", "apiErrorStatus": 400}, (False, None, None)),
    # Wrong types read as absent rather than crossing as junk, and the refusal
    # survives losing its detail.
    ({"isApiErrorMessage": True, "error": 7, "apiErrorStatus": "400"},
     (True, None, None)),
    ({"isApiErrorMessage": True, "error": "x", "apiErrorStatus": True},
     (True, "x", None)),
    ({"isApiErrorMessage": "yes"}, (False, None, None)),
])
def test_the_refusal_fields_are_read_defensively(record, expected):
    """This file is written by a program the platform does not version."""
    assert transcripts._api_error_of(record) == expected


@pytest.mark.parametrize("record,refused", [
    ({"isApiErrorMessage": True, "error": "billing_error"}, True),
    ({"isApiErrorMessage": True}, True),
    ({"error": "billing_error", "apiErrorStatus": 400}, False),
    ({}, False),
])
def test_the_refusal_flag_reaches_the_normalised_turn(record, refused):
    """One step past the adapter, which is where the gap was.

    The parametrised case above asserted the adapter's tuple and stopped there,
    so it passed while the fact never crossed onto the message and every consumer
    of it decided the opposite. Asserting the property on the object callers
    actually hold is what closes that.
    """
    message = transcripts.normalise_records([{
        "type": "assistant",
        "timestamp": "2026-08-07T01:00:00Z",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "API Error"}]},
        **record,
    }])[0]
    assert message.api_refusal is refused
    assert message.to_dict()["api_refusal"] is refused


def test_last_turn_reports_a_refusal_as_the_newest_turn(platform_root):
    """End to end: the fixture on disk, through the bounded tail read, to the
    fact halt detection asks for."""
    registry.register("s-api1", pid=DEAD_PID, run=dict(RUN))
    directory = transcripts.session_transcript_dir("s-api1") / "-workspace"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "session.jsonl").write_text(
        API_ERROR_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    turn = transcripts.last_turn("s-api1")
    assert turn is not None
    assert turn.api_error == "server_error"
    assert turn.api_error_status == 529


# --- the other two harnesses (#280) ------------------------------------------
#
# The platform spawns claude, pi and codex, and this adapter spoke one of them —
# so a pi or a codex run normalised to nothing and the chat view drew an empty
# page for a session that had said plenty.
#
# Fixtures again, captured from pi 0.84.1 and codex-cli 0.147.0 against a fake
# model endpoint, with paths neutralised and the giant instruction blobs cut to a
# placeholder. Kept for the reason spec D6 gave: three formats that are not
# contracts is three times the surface, and a format change has to fail here with
# a diff rather than on a phone.
#
# Two properties carry this section:
#
# - **The operator's turns are the operator's.** Codex writes its own injected
#   context — the environment block, the skills list, a repo's instructions — as
#   *user-role* records. Rendering any of that as something the operator said is
#   issue #242's class of bug, and it is the one thing here that is not cosmetic.
# - **A file nobody can read is not an empty conversation.** An unknown format
#   (kimi's wire log stands in for one) has to reach the operator as "on disk,
#   nothing to show", never as a silent blank page.

PI_FIXTURE = FIXTURES / "pi-session.jsonl"
PI_TOOLS_FIXTURE = FIXTURES / "pi-tools.jsonl"
CODEX_FIXTURE = FIXTURES / "codex-session.jsonl"
CODEX_TOOLS_FIXTURE = FIXTURES / "codex-tools.jsonl"
KIMI_FIXTURE = FIXTURES / "kimi-wire.jsonl"


def test_a_pi_conversation_normalises():
    """Roles, order, text and time, from records captured off a real pi session."""
    messages = transcripts.normalise_records(records(PI_FIXTURE))
    assert [(m.role, m.kind, m.text) for m in messages] == [
        ("user", "said", "say hi"),
        ("assistant", "said", "Hello from the fake model."),
    ]
    assert messages[0].at == "2026-08-14T07:52:30.805Z"


def test_pi_session_state_records_are_not_turns():
    """The header, the model change, the thinking level: session state, not talk."""
    assert transcripts.normalise_records([
        {"type": "session", "version": 3, "id": "x", "cwd": "/workspace"},
        {"type": "model_change", "id": "a", "provider": "fake", "modelId": "m"},
        {"type": "thinking_level_change", "id": "b", "thinkingLevel": "off"},
    ]) == []


def test_a_pi_tool_call_is_correlated_with_its_result():
    """pi puts the result in its own ``toolResult`` record, keyed by call id."""
    messages = transcripts.normalise_records(records(PI_TOOLS_FIXTURE))
    calls = [tool for message in messages for tool in message.tools]
    assert [(t.name, t.detail, t.status) for t in calls] == [
        ("bash", "echo transcript probe", "ok"),
    ]
    # The result record is the harness feeding the model, not a turn of its own.
    assert not [m for m in messages if "transcript probe\n" == m.text]


def test_a_failed_pi_tool_is_visible_with_its_reason():
    """``isError`` is pi's spelling of the case that matters (the captures had
    only successes, so the failing shape is asserted on its own)."""
    tool = transcripts.normalise_records([
        {"type": "message", "id": "a", "timestamp": "2026-08-14T08:22:45.208Z",
         "message": {"role": "assistant", "content": [
             {"type": "toolCall", "id": "call_1", "name": "bash",
              "arguments": {"command": "false"}},
         ]}},
        {"type": "message", "id": "b", "timestamp": "2026-08-14T08:22:45.249Z",
         "message": {"role": "toolResult", "toolCallId": "call_1",
                     "toolName": "bash", "isError": True,
                     "content": [{"type": "text", "text": "exit status 1"}]}},
    ])[0].tools[0]
    assert tool.status == "failed"
    assert tool.error == "exit status 1"


def test_a_pi_tool_reads_the_key_its_own_tools_use():
    """pi's file tools name their target ``path``, its shell one ``command``."""
    messages = transcripts.normalise_records([
        {"type": "message", "id": "a", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "c1", "name": "read",
             "arguments": {"path": "/workspace/src/lmer_platform/transcripts.py"}},
        ]}},
    ])
    assert messages[0].tools[0].detail == "/workspace/src/lmer_platform/transcripts.py"


def test_a_codex_conversation_normalises():
    messages = transcripts.normalise_records(records(CODEX_FIXTURE))
    assert [(m.role, m.kind) for m in messages] == [
        ("system", "injected"),      # the harness's own skills instructions
        ("user", "injected"),        # the environment block, in the user's role
        ("user", "said"),            # what the operator actually typed
        ("assistant", "said"),
    ]
    assert messages[2].text == "say hi"
    assert messages[3].text == "Hello from the fake model."
    assert messages[3].at == "2026-08-14T07:54:56.748Z"


def test_a_codex_turn_is_not_rendered_twice():
    """``event_msg`` duplicates the conversation ``response_item`` already
    carries — reading both would draw every turn of every codex run twice."""
    messages = transcripts.normalise_records(records(CODEX_FIXTURE))
    assert [m.text for m in messages].count("Hello from the fake model.") == 1
    assert [m.text for m in messages].count("say hi") == 1


def test_a_codex_tool_call_is_correlated_by_call_id():
    """The call and its output are separate ``response_item`` records, and the
    arguments arrive as a JSON *string* rather than a mapping."""
    messages = transcripts.normalise_records(records(CODEX_TOOLS_FIXTURE))
    calls = [tool for message in messages for tool in message.tools]
    assert [(t.name, t.detail, t.status) for t in calls] == [
        ("exec_command", "echo transcript probe", "ok"),
    ]
    # The output is the model's to read, not a turn: "Chunk ID: …" never renders.
    assert not [m for m in messages if "Chunk ID" in m.text]


@pytest.mark.parametrize("arguments", ['{"cmd": ', "not json at all", None, 7, "[]"])
def test_a_codex_call_with_unreadable_arguments_still_shows_its_name(arguments):
    """A blank chip beats a missing one, and a truncated argument blob is a
    shape this reader will meet — it is written by the model."""
    tool = transcripts.normalise_records([{
        "timestamp": "2026-08-14T08:23:07.940Z",
        "type": "response_item",
        "payload": {"type": "function_call", "name": "exec_command",
                    "arguments": arguments, "call_id": "call_1"},
    }])[0].tools[0]
    assert tool.name == "exec_command"
    assert tool.detail is None
    assert tool.status == "pending"


def test_a_codex_call_variant_this_reader_has_no_adapter_for_still_shows():
    """codex 0.147.0's response items go well past the three read exactly —
    ``custom_tool_call``, ``tool_search_call``, ``web_search_call`` and more —
    and an assistant turn made only of them used to render as *nothing*. A run
    whose page says nothing is read as a run that did nothing, so the miss is
    made legible: one chip, named by the payload, correlated as usual."""
    messages = transcripts.normalise_records([
        {"timestamp": "2026-08-14T08:23:07.940Z", "type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "apply_patch",
                     "call_id": "call_9",
                     "input": {"path": "/workspace/src/lmer_platform/spawn.py"}}},
        {"timestamp": "2026-08-14T08:23:08.100Z", "type": "response_item",
         "payload": {"type": "custom_tool_call_output", "call_id": "call_9",
                     "output": "Success. Updated the following files:\nM spawn.py"}},
    ])
    assert [(m.role, m.kind, m.text) for m in messages] == [
        ("assistant", "said", ""),
    ]
    assert [(t.name, t.detail, t.status) for t in messages[0].tools] == [
        ("apply_patch", "/workspace/src/lmer_platform/spawn.py", "ok"),
    ]


def test_a_codex_call_variant_with_no_name_is_chipped_by_its_type():
    """``local_shell_call`` carries no ``name``, and "something ran" is the
    whole point — the type is a truer caption than a blank chip. No output
    record yet, so it stays pending, exactly as a function call would."""
    tool = transcripts.normalise_records([{
        "timestamp": "2026-08-14T08:23:07.940Z", "type": "response_item",
        "payload": {"type": "local_shell_call", "call_id": "call_3",
                    "action": {"command": ["bash", "-lc", "ls"]}},
    }])[0].tools[0]
    assert (tool.name, tool.detail) == ("local_shell_call", None)
    assert tool.status == "pending"


def test_a_codex_reasoning_record_is_still_dropped():
    """The one variant the generic rule must not sweep up: private model
    reasoning, dropped for the reason claude's thinking blocks are."""
    assert transcripts.normalise_records([{
        "timestamp": "2026-08-14T08:23:07.940Z", "type": "response_item",
        "payload": {"type": "reasoning", "summary": [
            {"type": "summary_text", "text": "The operator wants the login bug fixed."},
        ]},
    }]) == []


@pytest.mark.parametrize("fixture", [CODEX_FIXTURE, CODEX_TOOLS_FIXTURE])
def test_codex_injected_context_is_never_an_operator_turn(fixture):
    """The hold on this task, and the reason the classification is made here.

    Codex writes its environment block, its skills list and a repo's instructions
    in the *operator's* role, with nothing in the record to tell them from a typed
    prompt. Whatever else changes, no developer, environment or skills content may
    reach the view as something a person said.
    """
    messages = transcripts.normalise_records(records(fixture))
    spoken = [m.text for m in messages if m.kind == "said" and m.role == "user"]
    assert spoken == ["say hi"] or spoken == ["please run the probe"]
    for text in spoken:
        assert "environment_context" not in text
        assert "<cwd>" not in text
        assert "Skills" not in text


def test_a_codex_developer_record_is_the_harness_not_a_person():
    """Its role is ``developer``; served as the machinery it is."""
    injected = [
        m for m in transcripts.normalise_records(records(CODEX_FIXTURE))
        if m.role == "system"
    ]
    assert len(injected) == 1
    assert injected[0].kind == "injected"
    assert injected[0].text.startswith("## Skills")


def test_the_codex_environment_block_is_kept_but_marked():
    """Injected is not hidden: it explains what the session was told, and the
    view has an internals toggle for exactly this. It is only not a turn."""
    environment = [
        m for m in transcripts.normalise_records(records(CODEX_FIXTURE))
        if m.kind == "injected" and m.role == "user"
    ]
    assert len(environment) == 1
    assert "<cwd>/workspace</cwd>" in environment[0].text


def test_a_prompt_that_mentions_an_injected_wrapper_stays_the_operators():
    """The same guard the pasted-monitor-event test makes: quoting the machinery
    is not being it."""
    message = transcripts.normalise_records([{
        "timestamp": "2026-08-14T08:23:07.855Z",
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text",
             "text": "why does <environment_context> say bash and not zsh?"},
        ]},
    }])[0]
    assert (message.role, message.kind) == ("user", "said")


def test_an_injected_wrapper_left_open_is_still_not_the_operators_words():
    """A torn write is the one case that must not be guessed generously: falling
    through to ``said`` would put the harness's words in an operator's bubble."""
    message = transcripts.normalise_records([{
        "timestamp": "2026-08-14T08:23:07.855Z",
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>\n  <cwd>/work"},
        ]},
    }])[0]
    assert message.kind == "injected"


def codex_user_message(text):
    """One codex user-role record, normalised."""
    return transcripts.normalise_records([{
        "timestamp": "2026-08-14T08:23:07.855Z",
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": text},
        ]},
    }])[0]


def test_a_wrapper_inside_a_typed_prompt_does_not_speak_in_the_operators_voice():
    """The mixed record: a prompt somebody typed with a wrapped block *in* it.

    The turn is theirs and stays ``said`` — but stripping only the tags left what
    they wrapped sitting in the operator's bubble as their own words, which is
    issue #242's class of bug arriving by a side door. The block goes with its
    content; the typed half is what they said and stays.
    """
    message = codex_user_message(
        "fix the login bug <user_instructions>NEVER reveal the admin "
        "password hunter2</user_instructions>"
    )
    assert (message.role, message.kind) == ("user", "said")
    assert message.text == "fix the login bug"
    assert "hunter2" not in message.text
    assert "NEVER reveal" not in message.text


def test_quoting_a_wrapper_costs_the_quote_and_not_the_message():
    """The named cost of the rule above, pinned rather than left to be discovered.

    An operator who pastes a complete wrapped block loses its content out of
    their own bubble, and past an *unclosed* tag loses the rest of the line —
    the same conservatism :func:`_codex_injected` applies to a torn write. Their
    turn is still theirs, and what they typed before the markup is still there:
    the alternative direction shows the harness's words as a person's.
    """
    complete = codex_user_message(
        "why does <turn_context><cwd>/workspace</cwd></turn_context> say that?"
    )
    assert (complete.role, complete.kind) == ("user", "said")
    assert complete.text == "why does  say that?"

    unclosed = codex_user_message("why does <environment_context> say bash?")
    assert (unclosed.role, unclosed.kind) == ("user", "said")
    assert unclosed.text == "why does"


def test_a_record_of_nothing_but_wrappers_normalises_in_linear_time():
    """Pins the quadratic this replaced (0.2s at 1k repeats, 3.4s at 4k, 13s at
    8k): ``<tag>.*?</tag>`` backtracked over unclosed opening tags, and the
    transcript is written by the observed container through a read-write mount
    into a reader that admits megabyte lines — so an agent could wedge the
    daemon's request thread with one line. The bound is loose because this is a
    wall clock on shared CI; the failure it catches is seconds, not milliseconds.
    """
    text = "hi " + "<environment_context>" * 10000
    start = time.monotonic()
    message = codex_user_message(text)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"normalising took {elapsed:.1f}s"
    assert (message.role, message.kind) == ("user", "said")


@pytest.mark.parametrize("record", [
    # pi: a credential pasted into a prompt.
    {"type": "message", "id": "a", "message": {"role": "user", "content": [
        {"type": "text", "text": "push with CREDENTIAL please"},
    ]}},
    # codex: the same, in its own envelope.
    {"type": "response_item", "payload": {"type": "message", "role": "user",
     "content": [{"type": "input_text", "text": "push with CREDENTIAL please"}]}},
])
def test_every_adapter_emits_through_the_scrub(record):
    """The chokepoint is the module's property, not each adapter's discipline:
    a new format must not be a new way for a credential to reach a browser."""
    raw = json.dumps(record).replace("CREDENTIAL", "glpat-" + "a" * 24)
    message = transcripts.normalise_records([json.loads(raw)])[0]
    assert "glpat-" not in message.text
    assert "<redacted>" in message.text


def test_dispatch_is_per_record_not_per_file():
    """Not a file anyone expects on disk — the point is that nothing about the
    *file* decides which adapter reads a record, so a transcript found through an
    unexpected route (a pointer, a mounted directory named for another harness)
    still normalises."""
    messages = transcripts.normalise_records(
        records(PI_FIXTURE) + records(CODEX_FIXTURE)
    )
    assert [m.text for m in messages if m.kind == "said"] == [
        "say hi", "Hello from the fake model.",
        "say hi", "Hello from the fake model.",
    ]


def test_an_unknown_format_is_skipped_rather_than_guessed_at():
    """kimi's wire log: a fourth harness's records, none of which this build
    speaks. Every one of them is skipped, including the ones carrying a role and
    content — guessing at a shape would attribute turns to the wrong party."""
    assert transcripts.normalise_records(records(KIMI_FIXTURE)) == []


@pytest.mark.parametrize("fixture,harness", [
    (SESSION_FIXTURE, "claude"),
    (PI_FIXTURE, "pi"),
    (CODEX_FIXTURE, "codex"),
])
def test_the_source_says_which_harness_wrote_the_file(
    platform_root, fixture, harness
):
    """The file's own answer, which beats a label: a pointer's ``harness`` is
    hand-editable and the derived directory carries none at all."""
    plant_session("s-h1", fixture=fixture)
    page = transcripts.read_messages("s-h1")
    assert [source.harness for source in page.sources] == [harness]


def test_a_file_nothing_recognises_keeps_the_label_it_came_with(platform_root):
    """No evidence, so nothing to correct the default with."""
    plant_session("s-h2", fixture=KIMI_FIXTURE)
    page = transcripts.read_messages("s-h2")
    assert [source.harness for source in page.sources] == ["claude"]


@pytest.mark.parametrize("fixture", [PI_FIXTURE, CODEX_FIXTURE])
def test_a_transcript_of_another_harness_reads_wherever_it_is_found(
    platform_root, fixture
):
    """Planted in the claude-shaped per-session directory, which is where a spawn
    mounts one — the layout says nothing about the format."""
    plant_session("s-h3", fixture=fixture)
    page = transcripts.read_messages("s-h3")
    assert [m.text for m in page.messages if m.kind == "said"] == [
        "say hi", "Hello from the fake model.",
    ]
    assert page.note is None


def test_a_transcript_this_build_cannot_read_says_so(platform_root):
    """The degradation that must survive: a file on disk that normalises to
    nothing is "nothing to show yet", never a silent blank page."""
    plant_session("s-h4", fixture=KIMI_FIXTURE)
    page = transcripts.read_messages("s-h4")
    assert page.total == 0
    assert page.sources, "the file was not even read"
    assert page.note == transcripts.EMPTY_TRANSCRIPT_NOTE


def plant_at(session_id, relative, fixture):
    """Write *fixture* at *relative* inside the session's transcript directory."""
    target = transcripts.session_transcript_dir(session_id) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def test_a_mixed_transcript_layout_reads_each_message_exactly_once(platform_root):
    """The layout changed under the reader, and the old one has to keep reading.

    A spawn now mounts one subdirectory per harness under the session's transcript
    directory (#280), while a session spawned before that wrote its files straight
    into the root. Both are found by the same recursive walk, so the property that
    matters is that the walk neither doubles a file nor misses one — a doubled
    transcript reads as the run having said everything twice.
    """
    registry.register("s-mixed", pid=DEAD_PID, run=dict(RUN))
    plant_at("s-mixed", "-workspace/session.jsonl", SESSION_FIXTURE)  # pre-#280
    plant_at("s-mixed", "claude/-workspace/followup.jsonl", FOLLOWUP_FIXTURE)
    plant_at("s-mixed", "pi/-workspace/session.jsonl", PI_FIXTURE)

    page = transcripts.read_messages("s-mixed", limit=transcripts.MAX_MESSAGE_LIMIT)
    expected = [
        message.text
        for fixture in (SESSION_FIXTURE, FOLLOWUP_FIXTURE, PI_FIXTURE)
        for message in transcripts.normalise_records(records(fixture))
    ]
    assert [m.text for m in page.messages] == expected
    assert page.total == len(expected)
    # And each file is labelled by what is in it rather than by where it was
    # found: the directory it sits in is a mount destination, not evidence.
    assert [source.harness for source in page.sources] == ["claude", "claude", "pi"]


@pytest.mark.parametrize("fixture", [PI_TOOLS_FIXTURE, CODEX_TOOLS_FIXTURE])
def test_last_turn_reads_a_pi_or_codex_tail(platform_root, fixture):
    """Halt detection asks the same question of every harness now. For codex the
    tail also *ends* in bookkeeping (``token_count``, ``task_complete``), so the
    answer has to come from the last ``response_item`` rather than the last line.
    """
    registry.register("s-h5", pid=DEAD_PID, run=dict(RUN))
    directory = transcripts.session_transcript_dir("s-h5") / "-workspace"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "session.jsonl").write_text(
        fixture.read_text(encoding="utf-8"), encoding="utf-8"
    )
    turn = transcripts.last_turn("s-h5")
    assert turn is not None
    assert (turn.role, turn.text) == ("assistant", "Hello from the fake model.")
