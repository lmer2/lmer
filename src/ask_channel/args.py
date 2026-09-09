"""Argument helpers and exit codes shared by ``lmer-signal`` and its shims.

One home for the three text sources (positional, ``--message-file``,
``--stdin``), the wait flags and the exit codes, so the shims (``lmer-ask``,
``lmer-slack post``) cannot drift from the command they forward to.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


class ArgsError(ValueError):
    """A body supplied zero or several times, or an unusable wait flag."""


#: Waited, nothing yet. Deliberately not 1: "no answer yet" is the normal
#: outcome of a bounded wait, and a caller has to be able to tell it from a
#: channel that is broken.
TIMEOUT_EXIT_CODE = 2

#: No channel at all: not orchestrated, no control plane, or the supervisor
#: does not answer. Distinct from 1 so a script can fall back to asking in its
#: ordinary output instead of retrying.
NO_CHANNEL_EXIT_CODE = 3

#: The tightest command timeout a harness in this fleet applies by default:
#: Claude Code's Bash tool kills a command at 120 s unless the model passes its
#: own (up to ten minutes), and neither codex's nor pi's shell tool advertises
#: anything more generous. A fact about where this CLI runs, not a setting.
HARNESS_COMMAND_TIMEOUT_FLOOR = 120.0

#: Leave headroom below the harness timeout for startup and the last poll.
DEFAULT_TIMEOUT = 90.0

#: Seconds between reads while waiting for an answer. A long-poll on the bus
#: returns the moment an answer lands, so this only bounds one round trip.
DEFAULT_INTERVAL = 2.0


def add_text_source(parser: argparse.ArgumentParser, label: str) -> None:
    """The three ways to supply a body.

    A body on the command line goes through the shell first, which silently eats
    backticks and ``$``; a question about a command is exactly the text most
    likely to contain both. So the file and stdin forms exist and are what the
    instructions point agents at.
    """
    parser.add_argument(
        "text",
        nargs="?",
        help=(
            f"The {label}. On the command line it is subject to the shell's "
            "quoting and expansion — prefer --message-file or --stdin for "
            "anything containing backticks, $ or quotes. '-' means --stdin."
        ),
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--message-file",
        metavar="PATH",
        help="Read the body verbatim from PATH (no shell expansion).",
    )
    source.add_argument(
        "--stdin",
        action="store_true",
        help="Read the body verbatim from stdin (no shell expansion).",
    )


def resolve_text(args: argparse.Namespace) -> str:
    """Return the body from exactly one of the three sources."""
    use_stdin = bool(getattr(args, "stdin", False)) or args.text == "-"
    message_file = getattr(args, "message_file", None)
    positional = args.text is not None and args.text != "-"

    if sum([positional, message_file is not None, use_stdin]) != 1:
        raise ArgsError(
            "provide the text exactly once — as the positional argument, "
            "--message-file PATH, or --stdin ('-')"
        )
    if message_file is not None:
        try:
            body = Path(message_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ArgsError(f"cannot read --message-file: {exc}")
        return body.rstrip("\n")
    if use_stdin:
        return sys.stdin.read().rstrip("\n")
    return args.text


def add_wait_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=(
            f"Seconds to wait for the answer (default {DEFAULT_TIMEOUT:g}; 0 "
            f"waits forever). Timing out exits {TIMEOUT_EXIT_CODE} and leaves "
            "the question open."
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        metavar="SECONDS",
        help=f"Seconds between checks (default {DEFAULT_INTERVAL:g}).",
    )


def check_wait_flags(args: argparse.Namespace) -> None:
    """Refuse wait settings that would spin instead of waiting.

    ``--interval 0`` is a busy loop against the supervisor for the whole
    timeout; a negative interval raises out of ``time.sleep``; a negative
    timeout would silently mean "wait forever", the opposite of what someone
    typing a negative number wants.
    """
    if args.interval <= 0:
        raise ArgsError("--interval must be greater than 0 seconds")
    if args.timeout < 0:
        raise ArgsError("--timeout cannot be negative (0 waits indefinitely)")
