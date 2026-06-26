"""Tests for the Slack reply-routing Stop hook (hooks/slack_reply_guard.py)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hooks.slack_reply_guard import (
    MIN_SUBSTANTIVE_CHARS,
    _is_slack_post,
    evaluate,
    iter_messages,
    unposted_reply_chars,
)

REPO_ROOT = Path(__file__).parent.parent
HOOK = REPO_ROOT / "hooks" / "slack_reply_guard.py"

LONG = "x" * (MIN_SUBSTANTIVE_CHARS + 10)


# ---- transcript event builders ------------------------------------------------

def _assistant_text(text):
    return {"type": "assistant", "message": {"role": "assistant",
                                             "content": [{"type": "text", "text": text}]}}


def _assistant_thinking(text):
    return {"type": "assistant", "message": {"role": "assistant",
                                             "content": [{"type": "thinking", "thinking": text}]}}


def _assistant_bash(command, tool_id=None):
    block = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
    if tool_id is not None:
        block["id"] = tool_id
    return {"type": "assistant", "message": {"role": "assistant", "content": [block]}}


def _user_text(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tool_result(text, tool_use_id=None, is_error=False):
    block = {"type": "tool_result", "content": text, "is_error": is_error}
    if tool_use_id is not None:
        block["tool_use_id"] = tool_use_id
    return {"type": "user", "toolUseResult": {"stdout": text},
            "message": {"role": "user", "content": [block]}}


# ---- _is_slack_post -----------------------------------------------------------

class TestIsSlackPost:
    @pytest.mark.parametrize("cmd", [
        "lmer-slack post --message-file /tmp/r.md",
        "lmer-slack post 'hi there'",
        "cat /tmp/r.md | lmer-slack post --stdin",
        "echo hi && lmer-slack post --stdin",
        "/usr/local/bin/lmer-slack post --stdin",
    ])
    def test_matches_real_posts(self, cmd):
        assert _is_slack_post(cmd) is True

    @pytest.mark.parametrize("cmd", [
        "lmer-slack history",
        "lmer-slack watch --out /tmp/t.jsonl",
        "lmer-slack poll",
        'grep "lmer-slack post" notes.md',
        "echo done",
        "",
    ])
    def test_ignores_non_posts(self, cmd):
        assert _is_slack_post(cmd) is False


# ---- unposted_reply_chars -----------------------------------------------------

class TestUnpostedReplyChars:
    def test_prose_without_post_accumulates(self):
        events = [_user_text("question?"), _assistant_text(LONG)]
        assert unposted_reply_chars(events) == len(LONG)

    def test_post_resets_to_zero(self):
        events = [_user_text("q?"), _assistant_text(LONG),
                  _assistant_bash("lmer-slack post --message-file /tmp/r.md")]
        assert unposted_reply_chars(events) == 0

    def test_successful_post_with_result_resets(self):
        events = [
            _user_text("q?"),
            _assistant_text(LONG),
            _assistant_bash("lmer-slack post --stdin", tool_id="t1"),
            _tool_result("ok", tool_use_id="t1", is_error=False),
        ]
        assert unposted_reply_chars(events) == 0

    def test_failed_post_does_not_reset(self):
        # post command issued but exited non-zero -> reply never delivered
        events = [
            _user_text("q?"),
            _assistant_text(LONG),
            _assistant_bash("lmer-slack post --stdin", tool_id="t1"),
            _tool_result("Exit code 1\nSlackError: ...", tool_use_id="t1", is_error=True),
        ]
        assert unposted_reply_chars(events) == len(LONG)

    def test_failed_then_successful_post_resets(self):
        events = [
            _user_text("q?"),
            _assistant_text(LONG),
            _assistant_bash("lmer-slack post --stdin", tool_id="t1"),
            _tool_result("Exit code 1", tool_use_id="t1", is_error=True),
            _assistant_bash("lmer-slack post --stdin", tool_id="t2"),  # retry
            _tool_result("ok", tool_use_id="t2", is_error=False),
        ]
        assert unposted_reply_chars(events) == 0

    def test_text_after_last_post_counts(self):
        # answer posted, then a fresh question answered only in prose
        events = [
            _assistant_bash("lmer-slack post --stdin"),
            _user_text("next question?"),
            _assistant_text(LONG),
        ]
        assert unposted_reply_chars(events) == len(LONG)

    def test_ack_then_work_then_result_post_is_clean(self):
        events = [
            _user_text("please rebuild"),
            _assistant_text("on it — rebuilding now"),
            _assistant_bash("lmer-slack post --stdin"),   # ack
            _assistant_text("running the indexer, this narration is terminal-only"),
            _assistant_bash("make reindex"),
            _assistant_text("done, summarizing"),
            _assistant_bash("lmer-slack post --message-file /tmp/result.md"),  # result
        ]
        assert unposted_reply_chars(events) == 0

    def test_work_without_final_post_trips(self):
        events = [
            _assistant_bash("lmer-slack post --stdin"),   # ack
            _assistant_text(LONG),                          # work narration, no follow-up post
        ]
        assert unposted_reply_chars(events) == len(LONG)

    def test_thinking_is_ignored(self):
        events = [_user_text("q?"), _assistant_thinking(LONG)]
        assert unposted_reply_chars(events) == 0

    def test_tool_results_do_not_count(self):
        events = [_user_text("q?"), _tool_result(LONG)]
        assert unposted_reply_chars(events) == 0

    def test_malformed_events_skipped(self):
        events = [None, "junk", {"type": "assistant"}, _assistant_text(LONG)]
        assert unposted_reply_chars(events) == len(LONG)


# ---- evaluate -----------------------------------------------------------------

class TestEvaluate:
    def test_returns_reason_above_threshold(self):
        reason = evaluate([_assistant_text(LONG)])
        assert reason is not None
        assert "lmer-slack post" in reason

    def test_none_below_threshold(self):
        assert evaluate([_assistant_text("ok")]) is None

    def test_none_when_posted(self):
        events = [_assistant_text(LONG), _assistant_bash("lmer-slack post --stdin")]
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
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
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
            _assistant_text(LONG), _assistant_bash("lmer-slack post --stdin")])
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
