#!/usr/bin/env python3
"""
Stop hook: Slack reply-routing guard.

In a Slack-bridged chat session (``LMER_SLACK_CHANNEL`` is set) the
conversation with the human happens in a Slack thread, driven through the
``lmer-slack`` CLI. The terminal is a *separate* channel: it carries the
agent's own working output and is used to debug the integration. Crucially,
in the steady state it is **unattended** — so a reply the agent composes as
ordinary assistant text lands in the terminal and reaches *no one*. The human
sees silence on a question they asked, and there is nobody at the terminal to
notice and nudge.

Field reports on MR !86 showed this happening repeatedly, and recurring even
after the model acknowledged the miss — model self-correction was not enough.
This hook is the programmatic backstop.

It fires when the agent yields (the Claude Code ``Stop`` event). If the agent
emitted substantive visible text *after* its most recent ``lmer-slack post``
call — i.e. a reply that went to the terminal but never reached Slack — it
blocks the stop and re-prompts the agent to post the reply via
``lmer-slack post``. Because the terminal backstop does not exist in the
steady state, a non-blocking reminder (which only prints to the terminal)
would be useless here: the guard must actually drive the reply to Slack, so it
re-prompts rather than merely warning.

Scoping the "no post" check to *everything since the last post* (rather than
just the final message) means the legitimate patterns do not trip:
acknowledge-then-work, periodic progress notes, and post-then-yield all leave
a ``lmer-slack post`` after the last substantive text. Only a turn that
produced a real reply and posted nothing trips — which is exactly the bug.

A post counts as a post only if it *succeeded*: ``lmer-slack post`` exits
non-zero on a ``SlackError`` (network blip, 5xx, revoked token), and a reply
that failed to send never reached Slack either — so a failed post does not
reset the counter, and the guard still re-prompts. (Such a failure is also
surfaced to the agent as a non-zero Bash result, so it is less silent than the
routing miss this guard primarily targets.)

The hook is a no-op outside Slack mode, honours ``stop_hook_active`` so it
nudges at most once per yield (no loops), and fails open: any error reading
the payload or transcript lets the agent stop normally. A guard that broke
the agent would be worse than the bug it prevents.
"""
from __future__ import annotations

import json
import os
import re
import sys

# Minimum length (whitespace-stripped) of visible assistant text accumulated
# since the last successful `lmer-slack post` before we treat it as a real,
# unposted reply. This is a deliberate trade-off point, not a precise target:
# the floor exists to avoid false positives on brief terminal-only lines that
# legitimately follow a post — short working narration ("running tests now")
# and post-confirmation notes ("done — posted to the thread", ~28 chars) — which
# a near-zero threshold would flag. The cost is that the very shortest genuine
# one-line replies (e.g. "Yes, that's right." at ~18 chars) fall below the floor
# and are intentionally NOT caught; the guard targets medium-and-longer dropped
# replies. Trivial acks ("ok", "done", an emoji) are well below it.
MIN_SUBSTANTIVE_CHARS = 40

# Matches an `lmer-slack post` invocation inside a shell command. The leading
# boundary class (start-of-string or a shell separator / path slash) keeps a
# quoted mention like `grep "lmer-slack post"` from counting as an actual
# post. `history`, `watch`, and `poll` do not match — only `post` resets the
# counter.
_SLACK_POST_RE = re.compile(r"(?:^|[\s;&|()/])lmer-slack\s+post(?:\s|$)")


def _is_slack_post(command: str) -> bool:
    """Return True if ``command`` invokes ``lmer-slack post``."""
    return bool(command) and bool(_SLACK_POST_RE.search(command))


def _command_of(block: dict) -> str | None:
    """Extract the shell command from a Bash ``tool_use`` block, if any."""
    if block.get("name") != "Bash":
        return None
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    return command if isinstance(command, str) else None


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


def _tool_result_errors(events: list[dict]) -> dict:
    """
    Map ``tool_use_id`` -> ``is_error`` (bool) across all tool results in the
    transcript.

    A Bash ``tool_result`` carries ``is_error: True`` when the command exited
    non-zero. Used to tell a delivered ``lmer-slack post`` from a failed one.
    """
    errors: dict = {}
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "user":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if tool_use_id is not None:
                    errors[tool_use_id] = bool(block.get("is_error"))
    return errors


def unposted_reply_chars(events: list[dict]) -> int:
    """
    Count visible assistant text accumulated since the most recent *successful*
    ``lmer-slack post``.

    Walks events in order. Assistant ``text`` blocks add their stripped length;
    a Bash ``tool_use`` that runs ``lmer-slack post`` resets the count to zero —
    but only if its tool result did not error (a failed post did not deliver,
    so it must not silence the guard). ``thinking`` blocks and tool results are
    ignored for the text tally. The returned value is the amount of reply text
    that the human never saw on Slack.
    """
    errors = _tool_result_errors(events)
    chars = 0
    for event in events:
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
                command = _command_of(block)
                if command and _is_slack_post(command):
                    # A post we cannot correlate to a result (missing id) is
                    # assumed delivered — fail toward not nagging.
                    if errors.get(block.get("id")) is not True:
                        chars = 0
    return chars


def build_reason() -> str:
    """The message fed back to the agent when the guard blocks a stop."""
    return (
        "Slack reply-routing check: this turn produced visible reply text but "
        "made no `lmer-slack post` call. In a Slack-bridged session the human "
        "is reached ONLY through the Slack thread — the terminal is a separate, "
        "normally-unattended channel, so that reply went into the void and the "
        "human did not see it. Post your reply now with `lmer-slack post` "
        "(use `--message-file PATH` or `--stdin` for any body containing "
        "backticks, `$`, or quotes). If you genuinely owed the human no reply "
        "this turn (internal/working output only), simply stop again."
    )


def evaluate(events: list[dict], min_chars: int = MIN_SUBSTANTIVE_CHARS) -> str | None:
    """
    Return the re-prompt reason if the transcript shows an unposted reply,
    otherwise ``None``.
    """
    if unposted_reply_chars(events) >= min_chars:
        return build_reason()
    return None


def main(argv: list[str] | None = None) -> int:
    """
    Stop-hook entrypoint. Reads the hook payload from stdin and, in Slack
    mode, blocks the stop with a re-prompt when a reply was left unposted.

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

    # No-op outside Slack-bridged sessions.
    if not os.environ.get("LMER_SLACK_CHANNEL", "").strip():
        return 0

    transcript_path = payload.get("transcript_path")
    if not transcript_path or not isinstance(transcript_path, str):
        return 0

    try:
        events = iter_messages(transcript_path)
    except Exception:
        return 0

    reason = evaluate(events)
    if reason:
        json.dump({"decision": "block", "reason": reason}, sys.stdout)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
