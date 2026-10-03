#!/usr/bin/env python3
"""Block substantive unposted replies in chat-bridged sessions.

Only producer records for calls in the ending turn prove routing. Empty turns
owe nothing. Hook feedback starts a new turn; the guard nudges once.
See docs/COMMS.md for the receipt and turn contracts.
"""
from __future__ import annotations

import json
import os
import sys

from lmer_cli.routing_receipts import delivered, prepare_bash

# Ignore short working acknowledgments.
MIN_SUBSTANTIVE_CHARS = 40


def iter_messages(transcript_path: str) -> list[dict]:
    """
    Parse a Claude Code transcript JSONL file into a list of event objects.

    Malformed lines are skipped rather than raising — a single bad line must
    not disable the guard for an otherwise healthy session.
    """
    events: list[dict] = []
    with open(transcript_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except (ValueError, TypeError):
                continue
    return events


def _is_prompt(event: object) -> bool:
    """A prompt or mid-turn queued input starts a turn; tool results do not."""
    if not isinstance(event, dict):
        return False
    if event.get("type") == "attachment":
        attachment = event.get("attachment")
        return isinstance(attachment, dict) and attachment.get("type") == "queued_command"
    if event.get("type") != "user" or "toolUseResult" in event:
        return False
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return any(
            isinstance(block, dict) and block.get("type") != "tool_result"
            for block in content
        )
    return False


def current_turn(events: list[dict]) -> list[dict]:
    """The events of the turn a Stop event ends: from the last prompt onwards.

    A transcript with no prompt at all is one turn.
    """
    start = 0
    for index, event in enumerate(events):
        if _is_prompt(event):
            start = index
    return events[start:]


def owed_reply_chars(events: list[dict], *, session_id: str | None = None) -> int:
    """Count visible text unless a producer delivered through this turn's calls."""
    turn = current_turn(events)
    if session_id is None:
        session_id = next((event.get("sessionId") for event in turn
                           if isinstance(event, dict) and event.get("sessionId")), None)
    chars = 0
    for event in turn:
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content.strip())
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str):
                    chars += len(text.strip())
            elif block_type == "tool_use":
                if delivered(block.get("id"), session_id):
                    return 0
    return chars


def build_reason() -> str:
    """The message fed back to the agent when the guard blocks a stop."""
    return (
        "Reply-routing check: this turn wrote visible reply text but made no "
        "confirmed `lmer-signal post` (or `ask`/`dm`, or `lmer-slack post`) delivery. In a "
        "chat-bridged session the human is reached ONLY through the conversation "
        "channel — the terminal is a separate, normally-unattended channel, so "
        "that text reached nobody. Post the reply now with `lmer-signal post` "
        "(use `--message-file PATH` or `--stdin` for a body containing "
        "backticks, `$` or quotes). If this turn genuinely owed the human "
        "nothing, stop again with an EMPTY response — do not write a filler "
        "line, the chat pane draws it as speech."
    )


def evaluate(
    events: list[dict], min_chars: int = MIN_SUBSTANTIVE_CHARS, *, session_id: str | None = None,
) -> str | None:
    """
    Return the re-prompt reason if the ending turn owes an unrouted reply,
    otherwise ``None``.
    """
    if owed_reply_chars(events, session_id=session_id) >= min_chars:
        return build_reason()
    return None


def main(argv: list[str] | None = None) -> int:
    """
    Stop-hook entrypoint. Reads the hook payload from stdin and, in a
    chat-bridged session, blocks the stop with a re-prompt when the ending turn
    owes an unrouted reply.

    Always returns 0: blocking is signalled via the JSON ``decision`` field on
    stdout, never via exit code, and every failure path falls open.
    """
    try:
        raw = sys.stdin.read()
    except Exception:
        return 0

    try:
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        return 0

    # Already continuing from a previous nudge — do not block again (no loops).
    if payload.get("stop_hook_active"):
        return 0

    # No-op outside chat-bridged sessions: a Slack thread (LMER_SLACK_CHANNEL,
    # the historical trigger) or any chat-thread target (LMER_CHAT_CHANNEL,
    # which the Slack and matrix target handlers both set since #407).
    if not (
        os.environ.get("LMER_SLACK_CHANNEL", "").strip()
        or os.environ.get("LMER_CHAT_CHANNEL", "").strip()
    ):
        return 0

    if payload.get("hook_event_name") == "PreToolUse":
        prepared = prepare_bash(payload)
        if prepared:
            json.dump(prepared, sys.stdout)
            sys.stdout.write("\n")
        return 0

    transcript_path = payload.get("transcript_path")
    if not transcript_path or not isinstance(transcript_path, str):
        return 0

    try:
        events = iter_messages(transcript_path)
    except Exception:
        return 0

    reason = evaluate(events, session_id=payload.get("session_id"))
    if reason:
        json.dump({"decision": "block", "reason": reason}, sys.stdout)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
