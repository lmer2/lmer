"""lmer-ask console script: the session's end of the operator channel.

Subcommands
-----------
ask    Post a question and wait for the answer. Prints the answer on stdout.
       ``--option`` (repeatable) offers choices; the operator may ignore them.
note   Post a progress note. Expects no reply.
wait   Wait for the answer to a question already posted (what ``ask --no-wait``
       and a timed-out ``ask`` leave you with).
close  Stop waiting for an answer. The question stays on the channel as a record
       and the operator's reply box for it goes away.
list   Print the channel: what was asked, what was answered (and whether the
       answer was ever read), what is still open.

The CLI is the whole contract (spec D27). Anything that can run a command can
use this — there is no harness-specific tool, no SDK, and no schema beyond
"text, and optionally a list of strings".

Exit codes
----------
0                        the thing was posted, the answer arrived, or the
                         question is closed (``close`` never reports a failure
                         for a question that is already done with)
1                        a real error (bad arguments, unwritable channel, an
                         answer that does not belong to the question)
TIMEOUT_EXIT_CODE (2)    waited and no answer yet. **Not** an error: the
                         question is still open and ``lmer-ask wait <id>``
                         resumes the wait.
NO_CHANNEL_EXIT_CODE (3) this session has no channel — it was not started by
                         the orchestrator, or the mount is missing. Distinct
                         from 1 so a script can fall back to asking in its
                         ordinary output instead of retrying.

Why a wait can be interrupted safely
------------------------------------
Posting and waiting are separate acts on separate files, so a harness that kills
this process at its own tool timeout loses nothing: the question is already on
the channel and the operator can still answer it.

Reading an answer is recorded
-----------------------------
The verbs that hand an answer's text to the agent file a read receipt beside it
(:func:`ask_channel.protocol.mark_answer_read` holds the rule about which ones do
and why ``list`` does not). That is what makes "the operator answered and nobody
ever looked" a state the tooling can see: ``list`` marks such an answer unread,
and ``lmer-end-session`` refuses once to end a session that has one — the failure
that motivated the receipt was an agent timing out a wait, working for an hour,
and recording "unanswered" over a reply that was on the channel the whole time.

Better than surviving that kill, though, is not being killed: a harness that
times out the command reports its own failure to the agent, and what the agent
then has is a tool error rather than :data:`TIMEOUT_EXIT_CODE` and the sentence
telling it to resume with ``lmer-ask wait <id>``. So the default wait is sized to
end *before* the harness ends it — see :data:`DEFAULT_TIMEOUT`. The useful loop is
"wait, report, wait again", not one process blocked all afternoon.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from . import protocol
from .protocol import AskError, ChannelUnavailable

#: Waited, nothing yet. Deliberately not 1: "no answer yet" is the normal
#: outcome of a bounded wait, and a caller has to be able to tell it from a
#: channel that is broken.
TIMEOUT_EXIT_CODE = 2

#: No channel at all — see the module docstring.
NO_CHANNEL_EXIT_CODE = 3

#: The tightest command timeout a harness in this fleet applies by default:
#: Claude Code's Bash tool kills a command at 120 s unless the model passes its
#: own (up to ten minutes), and neither codex's nor pi's shell tool advertises
#: anything more generous. A fact about where this CLI runs, not a setting — it
#: lives here so the next person to raise the default has to argue with it.
HARNESS_COMMAND_TIMEOUT_FLOOR = 120.0

#: Default seconds a wait blocks before reporting back — under the floor above
#: with room to spare.
#:
#: The 30 s of headroom is not decoration. The harness starts counting before this
#: process does (interpreter start, imports, resolving the channel), the last poll
#: can overshoot the deadline by up to one :data:`DEFAULT_INTERVAL`, and a phone on
#: a slow link is not part of that budget at all. Being killed at the harness's
#: timeout costs the agent the one thing this exit code exists for: the difference
#: between "nobody has answered yet, resume with ``lmer-ask wait <id>``" and a tool
#: error that reads like the channel is broken.
#:
#: An agent that wants to block longer says so — ``--timeout`` with the harness's
#: own timeout raised to match — which is a decision, not a default.
DEFAULT_TIMEOUT = 90.0

#: Seconds between reads of the channel. A person is typing the answer, so this
#: is set by what feels immediate, not by what is cheap.
DEFAULT_INTERVAL = 2.0


def create_parser() -> argparse.ArgumentParser:
    """Create and return the argument parser for lmer-ask."""
    parser = argparse.ArgumentParser(
        prog="lmer-ask",
        description=(
            "Ask the operator who started this session a question, and read "
            "their answer. Use this instead of asking in the terminal: an "
            "orchestrated session's terminal has nobody watching it."
        ),
    )
    parser.add_argument(
        "--dir",
        metavar="PATH",
        help=(
            f"Channel directory. Defaults to ${protocol.ASK_DIR_ENV}, which the "
            "orchestrator sets when it starts a session."
        ),
    )

    sub = parser.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")

    ask_p = sub.add_parser(
        "ask",
        help="Post a question and wait for the answer (prints it on stdout).",
    )
    _add_text_source(ask_p, "question")
    ask_p.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="TEXT",
        dest="options",
        help=(
            "Offer a choice (repeatable). A hint, not a menu — the operator can "
            "answer with anything, so handle an answer that matches none of them."
        ),
    )
    ask_p.add_argument(
        "--no-wait",
        action="store_true",
        help="Post the question, print its id, and exit 0 without waiting.",
    )
    _add_wait_flags(ask_p)

    note_p = sub.add_parser(
        "note", help="Post a progress note to the operator. No reply expected."
    )
    _add_text_source(note_p, "note")

    wait_p = sub.add_parser(
        "wait", help="Wait for the answer to a question already posted."
    )
    wait_p.add_argument("id", help="Question id, as printed when it was posted.")
    _add_wait_flags(wait_p)

    close_p = sub.add_parser(
        "close",
        help=(
            "Stop waiting for an answer. The question stays as a record; the "
            "operator is no longer offered a reply box for it."
        ),
    )
    close_p.add_argument("id", help="Question id, as printed when it was posted.")
    close_p.add_argument(
        "--reason",
        metavar="TEXT",
        default="",
        help=(
            "One clause on why, shown beside the question (e.g. 'timed out, took "
            "the safe branch'). Optional."
        ),
    )

    list_p = sub.add_parser(
        "list", help="Print this session's channel: questions, notes, answers."
    )
    list_p.add_argument(
        "--open",
        action="store_true",
        dest="open_only",
        help="Only questions that are still waiting for an answer.",
    )
    list_p.add_argument(
        "--json",
        action="store_true",
        help="One JSON object per line instead of prose.",
    )

    return parser


def _add_text_source(parser: argparse.ArgumentParser, label: str) -> None:
    """The three ways to supply a body, mirroring ``lmer-slack post``.

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


def _add_wait_flags(parser: argparse.ArgumentParser) -> None:
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


def _resolve_text(args: argparse.Namespace) -> str:
    """Return the body from exactly one of the three sources."""
    use_stdin = bool(getattr(args, "stdin", False)) or args.text == "-"
    message_file = getattr(args, "message_file", None)
    positional = args.text is not None and args.text != "-"

    if sum([positional, message_file is not None, use_stdin]) != 1:
        raise AskError(
            "provide the text exactly once — as the positional argument, "
            "--message-file PATH, or --stdin ('-')"
        )
    if message_file is not None:
        try:
            body = Path(message_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise AskError(f"cannot read --message-file: {exc}")
        return body.rstrip("\n")
    if use_stdin:
        return sys.stdin.read().rstrip("\n")
    return args.text


def _check_wait_flags(args: argparse.Namespace) -> None:
    """Refuse wait settings that would spin instead of waiting.

    ``--interval 0`` looks like "check as fast as possible" and is a busy loop
    against the filesystem for the whole timeout — inside a container, with a
    person at the other end who will take seconds at best. A negative interval is
    a ``ValueError`` out of ``time.sleep``, and a negative timeout would silently
    mean "wait forever", which is the opposite of what someone typing a negative
    number wants.
    """
    if args.interval <= 0:
        raise AskError("--interval must be greater than 0 seconds")
    if args.timeout < 0:
        raise AskError("--timeout cannot be negative (0 waits indefinitely)")


def _cmd_ask(args: argparse.Namespace, directory: Path) -> int:
    # Before posting, not after: a question asked and then refused for a bad
    # flag would sit on the operator's channel with nobody waiting on it.
    if not args.no_wait:
        _check_wait_flags(args)
    entry = protocol.post_question(directory, _resolve_text(args), args.options)
    if args.no_wait:
        # The id on stdout, alone, so `id=$(lmer-ask ask --no-wait …)` works.
        print(entry.id)
        return 0
    print(f"asked the operator (question {entry.id}); waiting…", file=sys.stderr)
    return _await(directory, entry, args)


def _cmd_wait(args: argparse.Namespace, directory: Path) -> int:
    _check_wait_flags(args)
    entry = protocol.read_entry(directory, args.id)
    if entry is None:
        raise AskError(f"no question {args.id!r} on this channel")
    if entry.kind != protocol.KIND_QUESTION:
        raise AskError(f"entry {args.id} is a note — notes are not answered")
    if entry.answer is None and entry.closed:
        # Not TIMEOUT_EXIT_CODE: that code promises the question is still open and
        # a later wait will find the answer, and after a close neither is true —
        # the platform refuses to file a reply to it. An answered-and-closed
        # question falls through, because the answer is there to be printed.
        raise AskError(
            f"question {args.id} was closed, so nobody can answer it now — ask "
            "again if you still need to know"
        )
    return _await(directory, entry, args)


def _await(directory: Path, entry, args: argparse.Namespace) -> int:
    """Block for the answer and print it, or report the timeout.

    The answer goes to **stdout alone** so it can be captured; everything else
    this command says goes to stderr.
    """
    answer = protocol.wait_for_answer(
        directory, entry, timeout=args.timeout, interval=args.interval
    )
    if answer is None:
        # Four lines on every timeout, because the resume line alone was not
        # enough: an agent that had it quoted in front of it still went on to
        # other work, never waited again, and recorded "unanswered" while the
        # operator's reply sat on the channel. The loop, the hard rule and the
        # watch suggestion are what that agent was missing.
        print(
            f"no answer to question {entry.id} yet. It is still open — resume "
            f"the wait with: lmer-ask wait {entry.id} (or, if you have decided "
            f"without the operator: lmer-ask close {entry.id}).\n"
            "Answers arrive while you work: if you go on to other tasks, re-run "
            "that wait between them.\n"
            f"HARD RULE: before you conclude the operator has not answered — and "
            f"before you record any stop reason or end this session — run "
            f"lmer-ask wait {entry.id} (or lmer-ask list) one final time.\n"
            "If you have a monitor or watch tool, arm it to re-run that wait so "
            "an idle session still hears the answer.",
            file=sys.stderr,
        )
        return TIMEOUT_EXIT_CODE
    print(answer.text)
    _mark_read(directory, entry, "wait")
    return 0


def _mark_read(directory: Path, entry, via: str) -> None:
    """Record that the answer just printed reached the agent.

    Never fatal. The answer is already on stdout, so a channel that cannot take
    the receipt must not turn a delivered answer into a failure — the cost of a
    lost receipt is one redundant delivery by ``lmer-end-session`` later, which is
    the direction that cannot strand a reply.
    """
    try:
        protocol.mark_answer_read(directory, entry, via=via)
    except AskError as exc:
        print(
            f"lmer-ask: the answer is above, but recording that you read it "
            f"failed ({exc})",
            file=sys.stderr,
        )


def _cmd_close(args: argparse.Namespace, directory: Path) -> int:
    """Stop waiting for an answer to a question this session asked.

    Exit 0 in every outcome that is not a broken channel, because this is the verb
    an exit path calls: a question already closed is done, and a question the
    operator answered a second before the close is *also* done — that answer is
    printed on stdout exactly as ``wait`` would print it, so the reply is handed to
    the caller instead of being closed over. Nothing the operator typed is ever
    discarded (``ask_channel.protocol``, "An answer that raced a close wins").
    """
    entry = protocol.read_entry(directory, args.id)
    if entry is None:
        raise AskError(f"no question {args.id!r} on this channel")
    if entry.kind != protocol.KIND_QUESTION:
        raise AskError(f"entry {args.id} is a note — notes are not answered")
    if entry.closed:
        print(f"question {entry.id} was already closed", file=sys.stderr)
        return 0
    try:
        answer = protocol.close_question(directory, entry, reason=args.reason)
    except FileExistsError:
        # Closed between the read above and the link: same outcome, and a race
        # against oneself is not worth an exit code.
        print(f"question {entry.id} was already closed", file=sys.stderr)
        return 0
    if answer is not None:
        print(
            f"question {entry.id} was answered before it could be closed — the "
            "answer is on stdout",
            file=sys.stderr,
        )
        print(answer.text)
        _mark_read(directory, entry, "close")
        return 0
    print(f"closed question {entry.id}; the operator can no longer reply to it",
          file=sys.stderr)
    return 0


def _cmd_note(args: argparse.Namespace, directory: Path) -> int:
    entry = protocol.post_note(directory, _resolve_text(args))
    print(entry.id)
    return 0


def _cmd_list(args: argparse.Namespace, directory: Path) -> int:
    entries = protocol.read_entries(directory)
    if args.open_only:
        entries = [entry for entry in entries if protocol.is_answerable(entry)]
    for entry in entries:
        if args.json:
            print(json.dumps(entry.to_dict(), ensure_ascii=False))
            continue
        print(f"[{entry.id}] {_state_word(entry)}: {entry.text}")
        if entry.options:
            print(f"    options: {', '.join(entry.options)}")
        if entry.answer:
            print(f"    answer: {entry.answer.text}")
        if entry.closure and entry.closure.reason:
            print(f"    closed because: {entry.closure.reason}")
        if entry.problem:
            print(f"    problem: {entry.problem}")
    return 0


def _state_word(entry) -> str:
    """The state this entry's line leads with.

    ``answered`` outranks ``closed`` because an answer that raced a close is still
    an answer — the same order every other reader of this channel uses.

    ``answered (unread)`` is the one an agent has to act on: the operator replied
    and the reply has never been handed to anybody, so it is said in the leading
    position where a scan finds it. Listing does not clear it
    (:func:`ask_channel.protocol.mark_answer_read`).
    """
    if entry.kind != protocol.KIND_QUESTION:
        return "note"
    if entry.answered:
        return "answered" if entry.answer_read else "answered (unread)"
    return "closed" if entry.closed else "open"


_COMMANDS = {
    "ask": _cmd_ask,
    "note": _cmd_note,
    "wait": _cmd_wait,
    "close": _cmd_close,
    "list": _cmd_list,
}


def main(argv: Optional[List[str]] = None) -> int:
    """Parse arguments and dispatch. Returns the exit code; callers exit on it."""
    parser = create_parser()
    args = parser.parse_args(argv)

    if not args.subcommand:
        parser.print_usage(sys.stderr)
        return 1

    handler = _COMMANDS.get(args.subcommand)
    if handler is None:  # pragma: no cover - argparse rejects unknown verbs
        print(f"Unknown subcommand: {args.subcommand!r}", file=sys.stderr)
        return 1

    try:
        directory = protocol.resolve_channel_dir(args.dir)
    except ChannelUnavailable as exc:
        print(f"lmer-ask: {exc}", file=sys.stderr)
        return NO_CHANNEL_EXIT_CODE

    try:
        return handler(args, directory)
    except ChannelUnavailable as exc:  # pragma: no cover - resolved above
        print(f"lmer-ask: {exc}", file=sys.stderr)
        return NO_CHANNEL_EXIT_CODE
    except AskError as exc:
        print(f"lmer-ask: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # A killed wait is not a lost question — say so, because the agent's next
        # move (wait again) depends on knowing that.
        print(
            "lmer-ask: interrupted. Any question already posted is still open; "
            "`lmer-ask list --open` shows it.",
            file=sys.stderr,
        )
        return TIMEOUT_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
