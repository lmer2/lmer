"""lmer-slack console script.

Subcommands
-----------
history   Fetch the thread, print messages, advance cursor to latest ts.
post      Post a message, print the returned ts.
poll      Block-poll the thread until a new non-bot message arrives or
          --timeout seconds elapse.
watch     Long-poll continuously, emitting every new non-bot message as a
          JSON line to stdout (and optionally appending it to a file).
          Designed to be driven by a file/stream monitor instead of the
          agent blocking in poll itself.

Channel and thread_ts resolution order (first wins):
  1. --permalink <url>  (parsed via parse_slack_permalink)
  2. LMER_SLACK_CHANNEL / LMER_SLACK_THREAD_TS environment variables

Cursor file
-----------
Stored at CURSOR_DIR/<channel>-<thread_ts>.cursor (module-level CURSOR_DIR
so tests can patch it).  --since <ts> overrides the cursor for a single run
without touching the file.

Exit codes
----------
0                     success
1                     SlackError or other runtime error
POLL_TIMEOUT_EXIT_CODE  poll timed out without finding a new human message
                        (must not equal 1 so callers can distinguish)
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path
from typing import List, Optional

from .permalink import parse_slack_permalink, is_slack_thread_url
from .client import SlackClient, SlackError

# ---------------------------------------------------------------------------
# Module-level constants (patchable in tests)
# ---------------------------------------------------------------------------

#: Default directory for cursor files.  Tests patch this via patch.object.
CURSOR_DIR: str = "/tmp/lmer-slack"

#: Exit code returned when poll times out without finding a new human message.
#: Deliberately NOT 1 (generic error) so callers can distinguish.
POLL_TIMEOUT_EXIT_CODE: int = 2

#: Upper bound (seconds) for watch's exponential backoff after transient
#: Slack errors, so a sustained outage doesn't hammer the API every interval.
WATCH_ERROR_BACKOFF_MAX: float = 60.0


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------

def _cursor_path(channel: str, thread_ts: str) -> Path:
    """Return the Path object for the cursor file."""
    return Path(CURSOR_DIR) / f"{channel}-{thread_ts}.cursor"


def _read_cursor(channel: str, thread_ts: str) -> Optional[str]:
    """Read the cursor ts from disk; return None if absent or empty."""
    p = _cursor_path(channel, thread_ts)
    try:
        val = p.read_text().strip()
        return val if val else None
    except (FileNotFoundError, OSError):
        return None


def _write_cursor(channel: str, thread_ts: str, ts: str) -> None:
    """Write the cursor ts to disk, creating parent directories as needed."""
    p = _cursor_path(channel, thread_ts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(ts)


def _filter_newer_than(messages: List[dict], oldest: Optional[str]) -> List[dict]:
    """Return only messages strictly newer than *oldest*.

    conversations.replies always includes the thread's parent message
    regardless of the ``oldest`` parameter, so an oldest/cursor bound must be
    re-applied client-side or the parent (and any boundary message) is
    re-surfaced on every fetch.  When *oldest* is None all messages pass.
    """
    if oldest is None:
        return list(messages)
    return [msg for msg in messages if msg.get("ts", "") > oldest]


def _human_messages(
    messages: List[dict],
    oldest: Optional[str],
    thread_ts: str,
    bot_user_id: str,
) -> List[dict]:
    """Filter *messages* down to genuinely new human replies.

    A message qualifies only if it is strictly newer than *oldest*, is not the
    thread parent (``ts == thread_ts``), is not authored by this bot, and is
    not bot/webhook-authored at all — anything carrying ``bot_id`` or lacking a
    ``user`` field is never a human reply.  Shared by ``poll`` and ``watch``.
    """
    return [
        msg for msg in _filter_newer_than(messages, oldest)
        if msg.get("ts") != thread_ts
        and not msg.get("bot_id")
        and msg.get("user")
        and msg.get("user") != bot_user_id
    ]


def _latest_ts(messages: List[dict]) -> Optional[str]:
    """Return the maximum ``ts`` across *messages*, or None if there are none.

    Slack ts values are zero-padded ``seconds.micros`` strings that sort
    correctly lexically.  Shared by ``poll`` and ``watch`` for advancing the
    cursor; each caller decides whether/when to persist it.
    """
    all_ts = [msg["ts"] for msg in messages if "ts" in msg]
    return max(all_ts) if all_ts else None


def _sleep_within_deadline(seconds: float, deadline: Optional[float]) -> bool:
    """Sleep up to *seconds*, capped to the time remaining before *deadline*.

    Returns False if the deadline has already passed (the caller should stop),
    True otherwise.  *deadline* is a ``time.monotonic()`` value, or None for no
    deadline (always sleeps the full *seconds* and returns True).
    """
    if deadline is None:
        time.sleep(seconds)
        return True
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    time.sleep(min(seconds, remaining))
    return True


def _watch_line(msg: dict) -> str:
    """Render a message as a single-line JSON event for watch output.

    JSON (not ``[ts] text``) so multi-line message text stays on one line —
    each emitted line is one event for a downstream stream/file monitor.
    """
    return json.dumps({
        "ts": msg.get("ts", ""),
        "user": msg.get("user", ""),
        "text": msg.get("text", ""),
    })


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def create_parser() -> argparse.ArgumentParser:
    """Create and return the argument parser for lmer-slack."""
    parser = argparse.ArgumentParser(
        prog="lmer-slack",
        description="Slack thread CLI for lmer: fetch history, post messages, poll for replies.",
    )

    # Optional global flag: derive channel + thread_ts from a Slack permalink.
    parser.add_argument(
        "--permalink",
        metavar="URL",
        help=(
            "Slack thread permalink.  Derives channel and thread_ts; overrides "
            "LMER_SLACK_CHANNEL / LMER_SLACK_THREAD_TS."
        ),
    )

    sub = parser.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")

    # --- history ---
    hist = sub.add_parser(
        "history",
        help="Fetch the thread and print messages.",
    )
    hist.add_argument(
        "--since",
        metavar="TS",
        help="Fetch only messages newer than this timestamp (overrides cursor file).",
    )

    # --- post ---
    post_p = sub.add_parser(
        "post",
        help="Post a message to the thread.",
    )
    post_p.add_argument(
        "text",
        nargs="?",
        help=(
            "Message text. Passed on the command line it is subject to the "
            "shell's quoting and expansion — backticks, $, quotes etc. in the "
            "body can be silently mangled. For free-form text (especially "
            "Slack inline-code backticks) prefer --message-file or --stdin, "
            "which read the body verbatim. '-' is an alias for --stdin."
        ),
    )
    post_src = post_p.add_mutually_exclusive_group()
    post_src.add_argument(
        "--message-file",
        metavar="PATH",
        help="Read the message body verbatim from PATH (no shell expansion).",
    )
    post_src.add_argument(
        "--stdin",
        action="store_true",
        help="Read the message body verbatim from stdin (no shell expansion).",
    )

    # --- poll ---
    poll_p = sub.add_parser(
        "poll",
        help="Long-poll the thread until a new non-bot message arrives.",
    )
    poll_p.add_argument(
        "--interval",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="Seconds between each poll (default: 5).",
    )
    poll_p.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="Total seconds before giving up (default: 300).",
    )
    poll_p.add_argument(
        "--since",
        metavar="TS",
        help="Start cursor at this timestamp (overrides cursor file).",
    )

    # --- watch ---
    watch_p = sub.add_parser(
        "watch",
        help=(
            "Continuously long-poll the thread, streaming every new non-bot "
            "message as a JSON line (for a file/stream monitor)."
        ),
    )
    watch_p.add_argument(
        "--out",
        metavar="FILE",
        help=(
            "Also append each emitted JSON line to FILE (created if needed). "
            "stdout always carries the same stream."
        ),
    )
    watch_p.add_argument(
        "--interval",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="Seconds between each poll (default: 5).",
    )
    watch_p.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help=(
            "Stop after this many wall-clock seconds.  0 (the default) runs "
            "indefinitely until the process is stopped."
        ),
    )
    watch_p.add_argument(
        "--since",
        metavar="TS",
        help="Start cursor at this timestamp (overrides cursor file).",
    )

    return parser


# ---------------------------------------------------------------------------
# Channel / thread_ts resolution
# ---------------------------------------------------------------------------

def _resolve_channel_thread(args: argparse.Namespace):
    """Return (channel, thread_ts) from --permalink or env vars.

    Raises SystemExit(1) if neither source provides both values.
    """
    if args.permalink:
        try:
            channel, thread_ts = parse_slack_permalink(args.permalink)
            return channel, thread_ts
        except ValueError as exc:
            print(f"Error parsing permalink: {exc}", file=sys.stderr)
            sys.exit(1)

    channel = os.environ.get("LMER_SLACK_CHANNEL", "").strip()
    thread_ts = os.environ.get("LMER_SLACK_THREAD_TS", "").strip()

    if not channel or not thread_ts:
        print(
            "Error: channel and thread_ts are required.  "
            "Set LMER_SLACK_CHANNEL and LMER_SLACK_THREAD_TS, "
            "or use --permalink <url>.",
            file=sys.stderr,
        )
        sys.exit(1)

    return channel, thread_ts


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_history(args: argparse.Namespace, client: SlackClient) -> int:
    """Handle the 'history' subcommand."""
    channel, thread_ts = _resolve_channel_thread(args)

    # Determine oldest ts: --since > cursor file > None
    since: Optional[str] = getattr(args, "since", None)
    if since:
        oldest = since
    else:
        oldest = _read_cursor(channel, thread_ts)

    messages = client.get_replies(channel, thread_ts, oldest=oldest)

    if not messages:
        return 0

    # On cursored runs only print messages newer than the cursor — Slack
    # re-includes the thread parent on every fetch regardless of `oldest`.
    for msg in _filter_newer_than(messages, oldest):
        text = msg.get("text", "")
        ts = msg.get("ts", "")
        print(f"[{ts}] {text}")

    # Advance cursor to the latest ts seen, but never move it backwards —
    # a cursored fetch that returns only the re-included parent must not
    # regress the cursor to the parent's ts.
    all_ts = [msg["ts"] for msg in messages if "ts" in msg]
    if all_ts:
        latest_ts = max(all_ts)
        if oldest is None or latest_ts > oldest:
            _write_cursor(channel, thread_ts, latest_ts)

    return 0


def _resolve_post_text(args: argparse.Namespace) -> str:
    """Return the message body for 'post' from exactly one source.

    Sources, all mutually exclusive: the positional ``text`` argument,
    ``--message-file PATH``, ``--stdin``, or a positional ``-`` (alias for
    --stdin).  --message-file/--stdin read the body verbatim so it never goes
    through shell quoting/expansion — the only safe way to post free-form text
    containing backticks, ``$``, or quotes.  A trailing newline is stripped.

    Raises SystemExit(1) if zero or more than one source is given, or the
    --message-file cannot be read.
    """
    use_stdin = bool(getattr(args, "stdin", False)) or args.text == "-"
    msg_file = getattr(args, "message_file", None)
    positional = args.text is not None and args.text != "-"

    if sum([positional, msg_file is not None, use_stdin]) != 1:
        print(
            "Error: provide the message exactly once — as the positional "
            "argument, --message-file PATH, or --stdin ('-').",
            file=sys.stderr,
        )
        sys.exit(1)

    if msg_file is not None:
        try:
            body = Path(msg_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error reading --message-file: {exc}", file=sys.stderr)
            sys.exit(1)
        return body.rstrip("\n")

    if use_stdin:
        return sys.stdin.read().rstrip("\n")

    return args.text


def _cmd_post(args: argparse.Namespace, client: SlackClient) -> int:
    """Handle the 'post' subcommand."""
    channel, thread_ts = _resolve_channel_thread(args)

    text = _resolve_post_text(args)
    new_ts = client.post_message(channel, thread_ts, text)
    print(new_ts)

    return 0


def _cmd_poll(args: argparse.Namespace, client: SlackClient) -> int:
    """Handle the 'poll' subcommand.

    Fetches the thread repeatedly until a new non-bot message is found (exit 0)
    or the timeout elapses (exit POLL_TIMEOUT_EXIT_CODE).

    Bot messages are identified by comparing the message ``user`` field against
    the user ID returned by ``auth_test()``.

    A message only counts as *new* if it is strictly newer than the cursor and
    is not the thread parent: conversations.replies always returns the parent
    regardless of ``oldest``, and in the lmer chat flow the parent is the
    human's original message, so the bot-author filter alone would match it
    on the very first iteration and poll would return immediately.

    The timeout is a wall-clock bound (monotonic deadline), so slow network
    round-trips count against it.  At least one fetch always happens, and the
    sleep between fetches is capped to the time remaining.
    """
    channel, thread_ts = _resolve_channel_thread(args)

    # Determine starting cursor: --since > cursor file > None
    since: Optional[str] = getattr(args, "since", None)
    if since:
        current_oldest = since
    else:
        current_oldest = _read_cursor(channel, thread_ts)

    # Get the bot's own user ID to filter self-messages.
    bot_user_id = client.auth_test()

    interval = float(args.interval)
    timeout = float(args.timeout)

    deadline = time.monotonic() + timeout

    while True:
        messages = client.get_replies(channel, thread_ts, oldest=current_oldest)

        # Keep only genuinely new human messages (see _human_messages).
        human_messages = _human_messages(
            messages, current_oldest, thread_ts, bot_user_id
        )

        # Latest ts across all fetched messages (bot's and parent included).
        latest_ts = _latest_ts(messages)

        if human_messages:
            for msg in human_messages:
                text = msg.get("text", "")
                ts = msg.get("ts", "")
                print(f"[{ts}] {text}")

            if latest_ts is not None:
                _write_cursor(channel, thread_ts, latest_ts)
            return 0

        # No human messages yet.  Advance the internal cursor (never
        # backwards) to avoid re-fetching the same messages next iteration.
        if latest_ts is not None and (
            current_oldest is None or latest_ts > current_oldest
        ):
            current_oldest = latest_ts

        if not _sleep_within_deadline(interval, deadline):
            # Timeout elapsed without finding a new human message.
            return POLL_TIMEOUT_EXIT_CODE


def _cmd_watch(args: argparse.Namespace, client: SlackClient) -> int:
    """Handle the 'watch' subcommand.

    Long-polls the thread continuously and emits every genuinely new human
    message as a single JSON line to stdout (and, with --out, appends the same
    line to a file).  Unlike poll it does not stop on the first message: it is
    meant to be the command behind a stream/file monitor so the agent reacts to
    new messages as events instead of blocking in poll itself.

    The cursor advances (never backwards) past everything fetched and is
    persisted on each advance, so a restarted watcher resumes from where it
    left off rather than re-emitting the whole thread.

    Because it is meant to run indefinitely behind a monitor, a transient
    Slack error (network blip, 5xx, 429-exhaustion) during a fetch must not
    kill the stream: it is logged to stderr and retried with exponential
    backoff (capped at WATCH_ERROR_BACKOFF_MAX), so the watcher recovers when
    Slack does.  The pre-loop auth_test() is deliberately left to fail fast —
    a bad/revoked token is not transient and should not be retried forever.

    With --timeout 0 (the default) it runs until the process is stopped;
    a positive --timeout sets a monotonic wall-clock deadline (mainly for
    bounded/testable runs).
    """
    channel, thread_ts = _resolve_channel_thread(args)

    # Determine starting cursor: --since > cursor file > None
    since: Optional[str] = getattr(args, "since", None)
    current_oldest = since if since else _read_cursor(channel, thread_ts)

    # Get the bot's own user ID to filter self-messages.  A failure here
    # (e.g. invalid_auth) is not transient — let it propagate and fail fast.
    bot_user_id = client.auth_test()

    interval = float(args.interval)
    timeout = float(args.timeout)
    # timeout <= 0 means run indefinitely (no deadline).
    deadline = None if timeout <= 0 else time.monotonic() + timeout

    out_path = Path(args.out) if getattr(args, "out", None) else None
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)

    error_backoff = interval  # grows on consecutive transient fetch errors

    while True:
        try:
            messages = client.get_replies(
                channel, thread_ts, oldest=current_oldest
            )
        except SlackError as exc:
            # Transient blip — log and retry rather than terminating the
            # long-lived stream the /monitor loop depends on.
            print(
                f"lmer-slack watch: transient Slack error, retrying: {exc}",
                file=sys.stderr,
                flush=True,
            )
            if not _sleep_within_deadline(error_backoff, deadline):
                return 0
            error_backoff = min(error_backoff * 2, WATCH_ERROR_BACKOFF_MAX)
            continue

        error_backoff = interval  # reset after a successful fetch

        human_messages = _human_messages(
            messages, current_oldest, thread_ts, bot_user_id
        )
        if human_messages:
            lines = [_watch_line(msg) for msg in human_messages]
            for line in lines:
                print(line, flush=True)
            if out_path is not None:
                with out_path.open("a", encoding="utf-8") as fh:
                    fh.write("".join(line + "\n" for line in lines))

        # Advance the cursor (never backwards) past everything fetched and
        # persist it, so a restarted watcher resumes instead of re-emitting.
        latest_ts = _latest_ts(messages)
        if latest_ts is not None and (
            current_oldest is None or latest_ts > current_oldest
        ):
            current_oldest = latest_ts
            _write_cursor(channel, thread_ts, latest_ts)

        if not _sleep_within_deadline(interval, deadline):
            return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    """Parse arguments and dispatch to the appropriate subcommand handler.

    Returns an int exit code.  Callers should pass it to sys.exit().
    """
    parser = create_parser()
    args = parser.parse_args(argv)

    if not args.subcommand:
        parser.print_usage(sys.stderr)
        return 1

    # Reject unknown subcommands (argparse already handles this, but belt-and-
    # suspenders for subparsers that might not exit).
    known = {"history", "post", "poll", "watch"}
    if args.subcommand not in known:
        print(f"Unknown subcommand: {args.subcommand!r}", file=sys.stderr)
        return 1

    try:
        client = SlackClient()
    except SlackError as exc:
        print(f"Slack error: {exc}", file=sys.stderr)
        return 1

    try:
        if args.subcommand == "history":
            return _cmd_history(args, client)
        elif args.subcommand == "post":
            return _cmd_post(args, client)
        elif args.subcommand == "poll":
            return _cmd_poll(args, client)
        elif args.subcommand == "watch":
            return _cmd_watch(args, client)
    except SlackError as exc:
        print(f"Slack error: {exc}", file=sys.stderr)
        return 1

    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
