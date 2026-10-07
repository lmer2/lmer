"""Tests for the Slack reply-routing Stop hook (hooks/slack_reply_guard.py)."""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from hooks.slack_reply_guard import (
    MIN_SUBSTANTIVE_CHARS,
    current_turn,
    evaluate,
    iter_messages,
    owed_reply_chars,
)

from lmer_cli.routing_receipts import CONTEXT_ENV, KEY, delivered, record

REPO_ROOT = Path(__file__).parent.parent
HOOK = REPO_ROOT / "hooks" / "slack_reply_guard.py"

LONG = "x" * (MIN_SUBSTANTIVE_CHARS + 10)


@pytest.fixture(autouse=True)
def isolate_routing(routing_call):
    pass


# ---- transcript event builders ------------------------------------------------

def _assistant_text(text):
    return {"type": "assistant", "sessionId": "claude-test", "message": {"role": "assistant",
                                             "content": [{"type": "text", "text": text}]}}


def _assistant_thinking(text):
    return {"type": "assistant", "message": {"role": "assistant",
                                             "content": [{"type": "thinking", "thinking": text}]}}


def _assistant_bash(command, tool_id=None):
    block = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
    if tool_id is not None:
        block["id"] = tool_id
    return {"type": "assistant", "sessionId": "claude-test", "message": {"role": "assistant", "content": [block]}}


def _user_text(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tool_result(text, tool_use_id=None, is_error=False):
    block = {"type": "tool_result", "content": text, "is_error": is_error}
    if tool_use_id is not None:
        block["tool_use_id"] = tool_use_id
    return {"type": "user", "toolUseResult": {"stdout": text},
            "message": {"role": "user", "content": [block]}}


def _hook_feedback(text="Stop hook feedback: Reply-routing check: …"):
    """The harness's re-prompt after a blocked stop — a user record, `isMeta`."""
    return {"type": "user", "isMeta": True, "message": {"role": "user", "content": text}}


def _task_notification(text="<task-notification>Monitor event</task-notification>"):
    """A watch firing, delivered as an injected user turn."""
    return {"type": "user", "promptSource": "system",
            "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content": text}}


def _receipt(kind="post", tool_id="t1"):
    context = os.environ.get(CONTEXT_ENV)
    os.environ[CONTEXT_ENV] = json.dumps({"session": "claude-test", "tool": tool_id})
    try:
        record(kind, "session:1")
    finally:
        if context is None:
            os.environ.pop(CONTEXT_ENV, None)
        else:
            os.environ[CONTEXT_ENV] = context
    return json.dumps({KEY: {"version": 1, "kind": kind, "id": "session:1"}})


def _routed(command, kind="post", tool_id=None):
    tool_id = tool_id or uuid.uuid4().hex
    return [_assistant_bash(command, tool_id=tool_id),
            _tool_result(_receipt(kind, tool_id), tool_use_id=tool_id)]


@pytest.mark.parametrize("command", [
    "lmer-signal ask --help",
    "printf '%s\\n' 'example: lmer-signal ask question'",
    "lmer-signal dm --help",
    "lmer-slack post --help",
])
def test_command_text_is_not_delivery(command):
    events = [_user_text("question?"), _assistant_bash(command, tool_id="help"),
              _tool_result("CLI documentation", tool_use_id="help"), _assistant_text(LONG)]
    assert evaluate(events) is not None


@pytest.mark.parametrize("output", [
    "ok", "000001", 'null', '[]', '{"lmer_routing_receipt": null}',
    json.dumps({KEY: {"version": True, "kind": "post", "id": "1"}}),
    json.dumps({KEY: {"version": 1, "kind": "note", "id": "1"}}),
    json.dumps({KEY: {"version": 1, "kind": [], "id": "1"}}),
    json.dumps({KEY: {"version": 1, "kind": "ask", "id": ""}}),
    json.dumps({KEY: {"version": 1, "kind": "post", "id": "genuine-old-id"}}),
])
def test_tool_output_is_not_delivery_evidence(output):
    assert not delivered(output)
    assert evaluate([_assistant_bash("cat receipt.json", tool_id="read"),
                     _tool_result(output, tool_use_id="read"), _assistant_text(LONG)])


def test_receipt_must_belong_to_a_call_in_this_turn():
    old = _routed("lmer-signal post hello", tool_id="previous")
    events = [*old, _user_text("new question"), _assistant_text(LONG),
              _tool_result(_receipt(), tool_use_id="previous")]
    assert evaluate(events) is not None


def test_a_late_confirmation_for_an_earlier_call_cannot_cover_the_new_prompt(routing_call):
    events = [_user_text("first"), _assistant_bash("send", tool_id="earlier"),
              _user_text("second"), _assistant_text(LONG)]
    routing_call("earlier")
    record("post", "provider-ref")
    assert evaluate(events) is not None


def test_delivery_is_known_even_when_the_tool_output_was_redirected():
    call, _result = _routed("lmer-signal post reply >/tmp/saved.json 2>&1")
    assert evaluate([call, _assistant_text(LONG)]) is None


# ---- owed_reply_chars ---------------------------------------------------------

class TestOwedReplyChars:
    def test_prose_without_post_accumulates(self):
        events = [_user_text("question?"), _assistant_text(LONG)]
        assert owed_reply_chars(events) == len(LONG)

    def test_receipted_post_resets_to_zero(self):
        events = [_user_text("q?"), _assistant_text(LONG),
                  *_routed("lmer-slack post --message-file /tmp/r.md")]
        assert owed_reply_chars(events) == 0

    def test_successful_post_with_result_resets(self):
        events = [
            _user_text("q?"),
            _assistant_text(LONG),
            _assistant_bash("lmer-slack post --stdin", tool_id="t1"),
            _tool_result(_receipt(), tool_use_id="t1", is_error=False),
        ]
        assert owed_reply_chars(events) == 0

    def test_failed_post_does_not_reset(self):
        # post command issued but exited non-zero -> reply never delivered
        events = [
            _user_text("q?"),
            _assistant_text(LONG),
            _assistant_bash("lmer-slack post --stdin", tool_id="t1"),
            _tool_result("Exit code 1\nSlackError: ...", tool_use_id="t1", is_error=True),
        ]
        assert owed_reply_chars(events) == len(LONG)

    def test_failed_then_successful_post_resets(self):
        events = [
            _user_text("q?"),
            _assistant_text(LONG),
            _assistant_bash("lmer-slack post --stdin", tool_id="t1"),
            _tool_result("Exit code 1", tool_use_id="t1", is_error=True),
            _assistant_bash("lmer-slack post --stdin", tool_id="t2"),  # retry
            _tool_result(_receipt(tool_id="t2"), tool_use_id="t2", is_error=False),
        ]
        assert owed_reply_chars(events) == 0

    def test_text_after_last_post_counts(self):
        # answer posted, then a fresh question answered only in prose
        events = [
            *_routed("lmer-slack post --stdin"),
            _user_text("next question?"),
            _assistant_text(LONG),
        ]
        assert owed_reply_chars(events) == len(LONG)

    def test_ack_then_work_then_result_post_is_clean(self):
        events = [
            _user_text("please rebuild"),
            _assistant_text("on it — rebuilding now"),
            *_routed("lmer-slack post --stdin"),   # ack
            _assistant_text("running the indexer, this narration is terminal-only"),
            _assistant_bash("make reindex"),
            _assistant_text("done, summarizing"),
            *_routed("lmer-slack post --message-file /tmp/result.md"),  # result
        ]
        assert owed_reply_chars(events) == 0

    def test_narration_after_a_post_in_the_same_turn_is_routed(self):
        """#462 point 2: the turn posted, so a closing line after it is not an
        unrouted reply — the old whole-transcript count blocked exactly this."""
        events = [
            _user_text("please rebuild"),
            *_routed("lmer-slack post --stdin"),   # ack
            _assistant_text(LONG),                          # closing narration
        ]
        assert owed_reply_chars(events) == 0

    def test_narration_in_a_later_turn_is_not_covered_by_an_earlier_post(self):
        events = [
            *_routed("lmer-slack post --stdin"),   # last turn's post
            _user_text("and now?"),                       # a new turn
            _assistant_text(LONG),                          # answered in prose only
        ]
        assert owed_reply_chars(events) == len(LONG)

    def test_thinking_is_ignored(self):
        events = [_user_text("q?"), _assistant_thinking(LONG)]
        assert owed_reply_chars(events) == 0

    def test_tool_results_do_not_count(self):
        events = [_user_text("q?"), _tool_result(LONG)]
        assert owed_reply_chars(events) == 0

    def test_malformed_events_skipped(self):
        events = [None, "junk", {"type": "assistant"}, _assistant_text(LONG)]
        assert owed_reply_chars(events) == len(LONG)


# ---- evaluate -----------------------------------------------------------------

class TestEvaluate:
    def test_returns_reason_above_threshold(self):
        reason = evaluate([_assistant_text(LONG)])
        assert reason is not None
        assert "lmer-slack post" in reason

    def test_none_below_threshold(self):
        assert evaluate([_assistant_text("ok")]) is None

    def test_none_when_posted(self):
        events = [_assistant_text(LONG), *_routed("lmer-slack post --stdin")]
        assert evaluate(events) is None


# ---- iter_messages ------------------------------------------------------------

class TestIterMessages:
    def test_parses_and_skips_bad_lines(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text(
            json.dumps(_assistant_text("hi")) + "\n"
            + "not json\n"
            + "\n"
            + json.dumps(_user_text("q")) + "\n"
        )
        events = iter_messages(str(p))
        assert len(events) == 2
        assert events[0]["type"] == "assistant"
        assert events[1]["type"] == "user"


# ---- main() via subprocess ----------------------------------------------------

def _run_hook(payload, env_extra):
    env = os.environ.copy()
    env.pop("LMER_SLACK_CHANNEL", None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=30,
    )


def _write_transcript(tmp_path, events):
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return str(p)


class TestMainSubprocess:
    def test_blocks_when_reply_unposted_in_slack_mode(self, tmp_path):
        transcript = _write_transcript(tmp_path, [_user_text("q?"), _assistant_text(LONG)])
        r = _run_hook({"transcript_path": transcript},
                      {"LMER_SLACK_CHANNEL": "C123"})
        assert r.returncode == 0
        out = json.loads(r.stdout)
        assert out["decision"] == "block"
        assert "lmer-slack post" in out["reason"]

    def test_noop_outside_slack_mode(self, tmp_path):
        transcript = _write_transcript(tmp_path, [_user_text("q?"), _assistant_text(LONG)])
        r = _run_hook({"transcript_path": transcript}, {})
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_noop_when_stop_hook_active(self, tmp_path):
        transcript = _write_transcript(tmp_path, [_user_text("q?"), _assistant_text(LONG)])
        r = _run_hook({"transcript_path": transcript, "stop_hook_active": True},
                      {"LMER_SLACK_CHANNEL": "C123"})
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_noop_when_reply_posted(self, tmp_path):
        transcript = _write_transcript(tmp_path, [
            _assistant_text(LONG), *_routed("lmer-slack post --stdin")])
        r = _run_hook({"transcript_path": transcript}, {"LMER_SLACK_CHANNEL": "C123"})
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_blocks_when_post_failed(self, tmp_path):
        transcript = _write_transcript(tmp_path, [
            _user_text("q?"),
            _assistant_text(LONG),
            _assistant_bash("lmer-slack post --stdin", tool_id="t1"),
            _tool_result("Exit code 1\nSlackError", tool_use_id="t1", is_error=True),
        ])
        r = _run_hook({"transcript_path": transcript}, {"LMER_SLACK_CHANNEL": "C123"})
        assert r.returncode == 0
        assert json.loads(r.stdout)["decision"] == "block"

    def test_noop_missing_transcript_path(self):
        r = _run_hook({}, {"LMER_SLACK_CHANNEL": "C123"})
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_fails_open_on_unreadable_transcript(self, tmp_path):
        r = _run_hook({"transcript_path": str(tmp_path / "nope.jsonl")},
                      {"LMER_SLACK_CHANNEL": "C123"})
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_fails_open_on_malformed_stdin(self):
        env = os.environ.copy()
        env["LMER_SLACK_CHANNEL"] = "C123"
        r = subprocess.run([sys.executable, str(HOOK)], input="not json{",
                           capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))
        assert r.returncode == 0
        assert r.stdout.strip() == ""


# ---- settings.json wiring (drift guard) ---------------------------------------

class TestSettingsWiring:
    def test_pretool_hook_provides_the_bash_invocation_identity(self):
        settings = json.loads((REPO_ROOT / "agent-files" / "claude" / "settings.json").read_text())
        assert any(group.get("matcher") == "Bash" and any(
            "slack_reply_guard.py" in hook.get("command", "") for hook in group.get("hooks", [])
        ) for group in settings['hooks'].get('PreToolUse', []))

    def test_stop_hook_registered_in_settings(self):
        settings = json.loads(
            (REPO_ROOT / "agent-files" / "claude" / "settings.json").read_text())
        stop_hooks = settings.get("hooks", {}).get("Stop", [])
        commands = [
            h.get("command", "")
            for group in stop_hooks for h in group.get("hooks", [])
        ]
        assert any("slack_reply_guard.py" in c for c in commands), (
            "Stop hook for slack_reply_guard.py missing from settings.json")


# ---------------------------------------------------------------------------
# #407: the post that resets the counter is now `lmer-signal post`, with the
# `lmer-slack post` shim still counting for one release
# ---------------------------------------------------------------------------


class TestSignalPost:
    @pytest.mark.parametrize("cmd", [
        "lmer-signal post --message-file /tmp/r.md",
        "lmer-signal post 'hi there'",
        "cat /tmp/r.md | lmer-signal post --stdin",
        "lmer-slack post --stdin",
        # #488: the human-routing verbs beside `post`.
        'lmer-signal ask "which branch?" --option main',
        "lmer-signal dm U123 --message-file /tmp/private.md",
    ])
    def test_routing_verbs_route(self, cmd):
        assert owed_reply_chars([_assistant_text(LONG), *_routed(cmd)]) == 0

    @pytest.mark.parametrize("cmd", [
        'lmer-signal "pushed MR"',
        'lmer-signal note "working"',
        "lmer-signal list",
        "lmer-signal wait 000004",
        'lmer-slack ask "which?"',
        'grep "lmer-signal post" notes.md',
    ])
    def test_other_verbs_do_not(self, cmd):
        assert owed_reply_chars([_assistant_text(LONG), _assistant_bash(cmd)]) == len(LONG)

    def test_the_reason_names_the_new_verb(self):
        reason = evaluate([_assistant_text(LONG)])
        assert "lmer-signal post" in reason

    def test_the_reason_asks_for_an_empty_stop_rather_than_a_filler_line(self):
        """#488: the old wording ("simply stop again") produced 46 "Nothing owed."
        lines in one recorded uber lmer session, each drawn by the pane as speech."""
        reason = evaluate([_assistant_text(LONG)])
        assert "EMPTY response" in reason
        assert "filler" in reason


# ---------------------------------------------------------------------------
# #488 / #462 point 2: the check is scoped to the turn a Stop ends.
# ---------------------------------------------------------------------------


class TestTurnScope:
    def test_the_turn_starts_at_the_last_prompt_and_tool_results_do_not_start_one(self):
        events = [
            _user_text("first"), _assistant_text("a"),
            _user_text("second"), _assistant_bash("ls", tool_id="t1"),
            _tool_result("out", tool_use_id="t1"), _assistant_text("b"),
        ]
        assert current_turn(events) == events[2:]

    def test_a_transcript_with_no_prompt_is_one_turn(self):
        events = [_assistant_text("a"), _assistant_text("b")]
        assert current_turn(events) == events

    def test_an_empty_turn_owes_nothing_even_after_an_unposted_turn(self):
        """The #488 case: a wake that only worked. The previous turn's unposted
        prose used to leak into the count and force a filler line here."""
        events = [
            _user_text("q?"), _assistant_text(LONG),          # earlier turn, never posted
            _task_notification(),                             # a watch fires: new turn
            _assistant_bash("curl -sS -X POST …/api/assistant/pending", tool_id="t1"),
            _tool_result("{}", tool_use_id="t1"),
        ]
        assert owed_reply_chars(events) == 0
        assert evaluate(events) is None

    def test_a_hook_re_prompt_starts_a_turn_of_its_own(self):
        events = [
            _user_text("q?"), _assistant_text(LONG),
            _hook_feedback(),
            *_routed("lmer-signal post --stdin"),
        ]
        assert owed_reply_chars(events) == 0

    def test_a_turn_of_prose_only_is_still_blocked(self):
        events = [_task_notification(), _assistant_text(LONG)]
        assert evaluate(events) is not None

    def test_prose_then_post_is_routed(self):
        events = [_user_text("q?"), _assistant_text(LONG), *_routed("lmer-signal post --stdin")]
        assert evaluate(events) is None

    def test_post_then_prose_is_routed(self):
        events = [_user_text("q?"), *_routed("lmer-signal post --stdin"), _assistant_text(LONG)]
        assert evaluate(events) is None

    def test_a_failed_routing_call_leaves_the_turn_unrouted(self):
        events = [
            _user_text("q?"), _assistant_text(LONG),
            _assistant_bash("lmer-signal ask 'which?'", tool_id="t1"),
            _tool_result("Exit code 3\nno channel", tool_use_id="t1", is_error=True),
        ]
        assert evaluate(events) is not None

    def test_an_ask_that_timed_out_waiting_was_still_delivered(self):
        """Exit 2 is "posted, no answer yet": the question reached every channel
        and the wait is the agent's to resume. Re-prompting it to post would
        produce the duplicate the pane change removes."""
        events = [
            _user_text("q?"), _assistant_text(LONG),
            _assistant_bash("lmer-signal ask 'which branch?' --timeout 60", tool_id="t1"),
            _tool_result(
                _receipt("ask") + "\nExit code 2\nno answer yet",
                tool_use_id="t1", is_error=True,
            ),
        ]
        assert owed_reply_chars(events) == 0

    def test_a_post_that_failed_with_the_same_words_is_not_excused(self):
        events = [
            _user_text("q?"), _assistant_text(LONG),
            _assistant_bash("lmer-signal post 'see lmer-signal wait 000004'", tool_id="t1"),
            _tool_result("Exit code 1\nlmer-signal wait 000004", tool_use_id="t1", is_error=True),
        ]
        assert evaluate(events) is not None

    def test_a_message_queued_mid_turn_starts_a_turn_of_its_own(self):
        """The harness delivers a message that arrived while the model was
        working as a `queued_command` attachment between tool results. Without
        this, an earlier post in the same stretch covered a prose-only answer
        to the operator's new question."""
        events = [
            _task_notification(),
            _assistant_bash("lmer-signal post 'sitrep: three up'"),
            {"type": "attachment", "attachment": {
                "type": "queued_command", "prompt": "[platform · operator] which branch?",
                "origin": {"kind": "human"},
            }},
            _assistant_text(LONG),
        ]
        assert current_turn(events) == events[2:]
        assert evaluate(events) is not None

    def test_a_user_record_marked_as_a_tool_result_never_starts_a_turn(self):
        events = [
            _user_text("q?"), _assistant_text(LONG),
            _assistant_bash("ls", tool_id="t1"),
            {"type": "user", "toolUseResult": {"stdout": "x"}, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "x"},
                {"type": "text", "text": "an odd writer's extra block"},
            ]}},
        ]
        assert current_turn(events) == events

    def test_the_hook_lets_an_empty_turn_stop(self, tmp_path):
        transcript = _write_transcript(tmp_path, [
            _user_text("q?"), _assistant_text(LONG),
            _task_notification(),
            _assistant_bash("curl -sS -X POST …/api/assistant/pending", tool_id="t1"),
            _tool_result("{}", tool_use_id="t1"),
        ])
        r = _run_hook({"transcript_path": transcript}, {"LMER_CHAT_CHANNEL": "slack"})
        assert r.returncode == 0
        assert r.stdout.strip() == ""


class TestChatChannelTrigger:
    def _run(self, env_extra, tmp_path):
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(json.dumps(_assistant_text(LONG)) + "\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ("LMER_SLACK_CHANNEL", "LMER_CHAT_CHANNEL")}
        env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "hooks" / "slack_reply_guard.py")],
            input=json.dumps({"transcript_path": str(transcript)}),
            capture_output=True, text=True, env=env, timeout=30,
        )

    def test_a_matrix_target_arms_the_guard_too(self, tmp_path):
        result = self._run({"LMER_CHAT_CHANNEL": "matrix"}, tmp_path)
        assert result.returncode == 0 and '"decision": "block"' in result.stdout

    def test_no_chat_target_no_guard(self, tmp_path):
        result = self._run({}, tmp_path)
        assert result.returncode == 0 and result.stdout.strip() == ""
