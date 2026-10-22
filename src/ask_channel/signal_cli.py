"""lmer-signal console script: the agent's one command to the world (issue #407).

Verbs
-----
::

    lmer-signal "<what happened>"        a milestone — the historical spelling, kept
    lmer-signal milestone "<text>"       the same, explicit
    lmer-signal note "<text>"            progress, no reply wanted
    lmer-signal post "<text>"            a message to the humans in the conversation
    lmer-signal read <permalink>         read any accessible thread, including this session's own
    lmer-signal dm <recipient> "<text>"  one private message; allow-listed, body never journalled
    lmer-signal post --thread <link> …   post once into a thread
    lmer-signal thread open <channel>    open a thread in a channel and connect to it
    lmer-signal thread connect <link>    connect this session to a thread that exists
    lmer-signal thread disconnect <link> stop mirroring one of its threads
    lmer-signal ask "<question>" ...     blocks until someone, anywhere, answers
    lmer-signal wait <id>                resume waiting after a timeout
    lmer-signal close <id>               stop waiting; the question is withdrawn everywhere
    lmer-signal list [--open] [--json]   the conversation so far

Every verb posts one record on the session bus the supervisor keeps
(:mod:`lmer_cli.comms`) and the supervisor fans it out to every connected
channel — the platform, Slack, matrix, a terminal. The agent does not know or
care who is listening; message kind rides along for rendering, never routing.

Exit codes — the ask channel's contract, unchanged
--------------------------------------------------
0   posted / answered / closed / listed
1   a real error (bad arguments, the supervisor refused the record)
2   ``ask``/``wait``: waited and no answer yet. Not an error: the question is
    still open and ``lmer-signal wait <id>`` resumes the wait.
3   no channel: this session has no supervisor control plane, or the
    supervisor does not answer. Say what you need in ordinary output instead.

Ids are printed as the record's sequence number, zero-padded as the ask
channel always printed them, and accepted back in any spelling (``42``,
``000042``, or the full id).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import mimetypes
import os
import shutil
import sys
import time
from pathlib import Path
from typing import List, Optional

from lmer_cli import thread_requests as _threads
from lmer_cli.comms import CHANNEL_UBER, META_AUTHORITY, META_OPERATOR_ONLY
from lmer_cli.routing_receipts import emit as routing_receipt

from . import bus
from .args import (
    DEFAULT_INTERVAL, DEFAULT_TIMEOUT, NO_CHANNEL_EXIT_CODE, TIMEOUT_EXIT_CODE,
    ArgsError, add_text_source, add_wait_flags, check_wait_flags, resolve_text,
)

PROG = "lmer-signal"

#: Seconds of slack on a claim beyond the wait itself, so it survives the gap
#: between two long-polls. The supervisor uses the same figure.
CLAIM_GRACE = 15.0

KIND_MESSAGE = "message"
KIND_NOTE = "note"
KIND_MILESTONE = "milestone"
KIND_QUESTION = "question"
KIND_ANSWER = "answer"
KIND_CLOSE = "close"

#: The verbs. A first argument that is not one of these is a milestone's text.
VERBS = (
    "milestone", "note", "post", "read", "dm", "thread", "ask", "wait", "close",
    "list", "end-session",
)

#: How long a thread verb waits for the host side to answer. Comfortably over a
#: Slack read with pagination, under the tightest harness command timeout.
THREAD_TIMEOUT = 90.0

#: Where a session's upload store is mounted, when it has one (#246). Read from
#: the environment the platform (or the host CLI) sets; ``--attach`` needs it.
UPLOADS_DIR_ENV = "LMER_UPLOADS_DIR"

#: How the record's id is printed — the ask channel's width, so a caller that
#: captured ``000001`` yesterday captures ``000002`` today.
ID_WIDTH = 6


def format_id(record: dict) -> str:
    return f"{int(record['seq']):0{ID_WIDTH}d}"


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Communicate with everyone connected to this session — the platform, "
            "Slack, matrix, a terminal — through the one channel the supervisor "
            "keeps. Bare text is a milestone; the verbs below are the rest."
        ),
    )
    sub = parser.add_subparsers(dest="verb", metavar="VERB")

    milestone_p = sub.add_parser(
        "milestone",
        help="Report that reportable work happened — an MR pushed, a review finished, the task done.",
    )
    add_text_source(milestone_p, "milestone")

    note_p = sub.add_parser("note", help="Post a progress note. No reply expected.")
    add_text_source(note_p, "note")
    _add_attach(note_p)

    post_p = sub.add_parser("post", help="Post a message to the humans in this session's conversation.")
    add_text_source(post_p, "message")
    _add_attach(post_p)
    post_p.add_argument(
        "--thread", metavar="PERMALINK",
        help=(
            "Post into the Slack or matrix thread this permalink names — a thread "
            "this session is not connected to. The post is attributed to this run "
            "and also recorded on this session's own conversation."
        ),
    )
    _add_thread_timeout(post_p)

    read_p = sub.add_parser(
        "read",
        help="Read a Slack or matrix thread by permalink, without connecting to it.",
    )
    read_p.add_argument("permalink", help="Thread permalink.")
    read_p.add_argument(
        "--limit", type=int, default=_threads.DEFAULT_READ_LIMIT, metavar="N",
        help=f"How many of the thread's most recent messages to print (default {_threads.DEFAULT_READ_LIMIT}).",
    )
    read_p.add_argument("--json", action="store_true", help="One JSON record per line instead of prose.")
    _add_thread_timeout(read_p)

    dm_p = sub.add_parser(
        "dm",
        help=(
            "Send one private message to a person. Allow-listed by the operator; "
            "the body is never recorded on this session's conversation."
        ),
    )
    dm_p.add_argument(
        "recipient",
        help="Slack user id or username, or a matrix user id (@name:server).",
    )
    add_text_source(dm_p, "message")
    _add_thread_timeout(dm_p)

    thread_p = sub.add_parser(
        "thread",
        help=(
            "This session's own chat threads: open one in a channel, connect one, "
            "disconnect one. The host side does the work — the credentials are there."
        ),
    )
    thread_sub = thread_p.add_subparsers(dest="thread_verb", metavar="VERB")
    open_p = thread_sub.add_parser(
        "open",
        help=(
            "Open a new thread in a Slack channel or matrix room and connect this "
            "session to it. Prints the thread's permalink."
        ),
    )
    open_p.add_argument(
        "channel",
        help="Slack channel id or #name, or matrix room id or alias. The bot must already be in it.",
    )
    open_p.add_argument(
        "text", nargs="?",
        help=(
            "First line of the root message, replacing this run's title. The run's "
            "key and the control-UI link are added either way."
        ),
    )
    _add_thread_timeout(open_p)
    connect_p = thread_sub.add_parser(
        "connect",
        help="Connect this session to a thread that already exists, by permalink.",
    )
    connect_p.add_argument("permalink", help="Thread permalink.")
    _add_thread_timeout(connect_p)
    disconnect_p = thread_sub.add_parser(
        "disconnect",
        help="Stop mirroring one of this session's threads. The thread is told.",
    )
    disconnect_p.add_argument("permalink", help="Thread permalink, as it was connected.")
    _add_thread_timeout(disconnect_p)

    ask_p = sub.add_parser(
        "ask", help="Post a question and wait for the first answer from any channel (printed on stdout)."
    )
    add_text_source(ask_p, "question")
    ask_p.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="TEXT",
        dest="options",
        help=(
            "Offer a choice (repeatable). A hint, not a menu — anyone may answer "
            "with anything, so handle an answer that matches none of them."
        ),
    )
    ask_p.add_argument(
        "--no-wait", action="store_true", help="Post the question, print its id, and exit 0 without waiting."
    )
    ask_p.add_argument(
        "--operator-only", action="store_true", dest="operator_only",
        help=(
            "A gate only a human may pass (spec approval, a merge, a new repository). "
            "The bus refuses an answer to it from any assistant, whatever authority it holds."
        ),
    )
    _add_attach(ask_p)
    add_wait_flags(ask_p)

    wait_p = sub.add_parser("wait", help="Wait for the answer to a question already posted.")
    wait_p.add_argument("id", help="Question id, as printed when it was posted.")
    add_wait_flags(wait_p)

    close_p = sub.add_parser(
        "close",
        help="Stop waiting for an answer. The question stays as a record; every channel withdraws its reply box.",
    )
    close_p.add_argument("id", help="Question id, as printed when it was posted.")
    close_p.add_argument(
        "--reason", metavar="TEXT", default="",
        help="One clause on why, shown beside the question (e.g. 'timed out, took the safe branch').",
    )

    end_p = sub.add_parser(
        "end-session",
        help="Say goodbye and end this session, freeing its slot at once.",
    )
    end_p.add_argument(
        "text", nargs="?",
        help="An optional goodbye, posted to the conversation before the session ends.",
    )
    end_source = end_p.add_mutually_exclusive_group()
    end_source.add_argument("--message-file", metavar="PATH", help="Read the goodbye verbatim from PATH.")
    end_source.add_argument("--stdin", action="store_true", help="Read the goodbye verbatim from stdin.")

    list_p = sub.add_parser("list", help="Print this session's conversation: every record, every channel.")
    list_p.add_argument("--open", action="store_true", dest="open_only", help="Only questions still waiting for an answer.")
    list_p.add_argument("--json", action="store_true", help="One JSON record per line instead of prose.")
    list_p.add_argument("--since", type=int, default=0, metavar="SEQ", help="Only records after this sequence number.")

    return parser


def _add_attach(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--attach",
        action="append",
        default=[],
        metavar="PATH",
        dest="attachments",
        help=(
            "Attach a file (repeatable). Copied into this session's upload store; "
            "connected Slack and matrix threads get the file itself, or a "
            "'withheld' note saying why (over 25 MiB, credential-shaped, the Slack "
            "app lacking files:write, matrix media not asserted authenticated). "
            "Refused when the session has no store."
        ),
    )


def _add_thread_timeout(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout", type=float, default=THREAD_TIMEOUT, metavar="SECONDS",
        help=f"How long to wait for the host side to answer (default {THREAD_TIMEOUT:.0f}).",
    )


def _client() -> bus.BusClient:
    return bus.BusClient(bus.resolve_endpoint())


class _LazyClient:
    """A :class:`bus.BusClient` resolved on first use.

    A verb validates its own flags before it posts anything, and a bad flag is
    the caller's mistake (exit 1) whatever the channel's state is. Resolving
    the endpoint up front turned every such mistake in a session without a bus
    into "no channel" (exit 3), and left ``end-session`` — whose real work is
    the shutdown, not the goodbye — unable to run at all.
    """

    def __init__(self) -> None:
        self._client: Optional[bus.BusClient] = None

    def __getattr__(self, name: str):
        if self._client is None:
            self._client = _client()
        return getattr(self._client, name)


def _stage_attachments(paths: List[str]) -> list:
    """Copy each file into the upload store and describe it for the record."""
    if not paths:
        return []
    store = (os.environ.get(UPLOADS_DIR_ENV) or "").strip()
    if not store or not os.path.isdir(store):
        raise ArgsError(
            "--attach needs an upload store and this session has none "
            f"({UPLOADS_DIR_ENV} unset or not mounted) — describe the file in the text instead"
        )
    staged = []
    for raw in paths:
        source = Path(raw)
        if not source.is_file():
            raise ArgsError(f"--attach {raw!r}: not a file")
        target = Path(store) / f"{time.strftime('%Y%m%d-%H%M%S')}-{source.name}"
        counter = 1
        while target.exists():
            target = Path(store) / f"{time.strftime('%Y%m%d-%H%M%S')}-{counter}-{source.name}"
            counter += 1
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
        media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        staged.append({
            "name": source.name,
            "bytes": target.stat().st_size,
            "media_type": media_type,
            "path": str(target),
        })
    return staged


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------


def _cmd_milestone(args: argparse.Namespace, client: bus.BusClient) -> int:
    result = client.post(KIND_MILESTONE, resolve_text(args))
    print(format_id(result["message"]))
    return 0


def _cmd_note(args: argparse.Namespace, client: bus.BusClient) -> int:
    result = client.post(KIND_NOTE, resolve_text(args), attachments=_stage_attachments(args.attachments))
    print(format_id(result["message"]))
    return 0


def _cmd_post(args: argparse.Namespace, client: bus.BusClient) -> int:
    text = resolve_text(args)
    staged = _stage_attachments(args.attachments)
    if getattr(args, "thread", None):
        return _post_into_thread(args, client, text, staged)
    result = client.post(KIND_MESSAGE, text, attachments=staged)
    routing_receipt("post", result["message"].get("id"))
    print(format_id(result["message"]))
    return 0


def _post_into_thread(
    args: argparse.Namespace, client: bus.BusClient, text: str, staged: list
) -> int:
    """Post once into a thread this session is not connected to, then record it.

    In that order: a record of a post that never landed is a record nobody can
    act on, and the delivery is the part that can fail.

    Which makes the *second* step the dangerous one — it can fail after the
    message is already in a thread this run does not own, and an agent told
    "failed" retries. So the receipt is printed before the record is attempted,
    and a failure there says what landed and that it must not be sent again.
    """
    delivered = _thread_work(
        client, args, _threads.OP_POST, args.thread, text=text, attachments=staged,
    )
    thread = delivered.get("thread") or {}
    where = thread.get("label") or args.thread
    posted = delivered.get("posted") or {}
    routing_receipt("post", posted.get("ref"))
    if posted.get("clipped"):
        print(
            f"posted into {where}, clipped to what one message carries — the "
            "record on this session's conversation has the whole text",
            file=sys.stderr,
        )
    else:
        print(f"posted into {where}", file=sys.stderr)
    try:
        result = client.post(
            KIND_MESSAGE, text, attachments=staged,
            meta={"thread": thread.get("permalink") or args.thread},
        )
    except (bus.NoChannel, bus.BusError) as exc:
        raise ArgsError(
            f"the message IS in {where} — do not send it again. What failed is "
            f"the record of it on this session's own conversation ({exc}), so the "
            "pane and any connected thread will not show that it went there"
        ) from None
    print(format_id(result["message"]))
    return 0


def _cmd_read(args: argparse.Namespace, client: bus.BusClient) -> int:
    from lmer_cli.thread_access import render_record

    result = _thread_work(client, args, _threads.OP_READ, args.permalink, limit=args.limit)
    messages = result.get("messages") or []
    for record in messages:
        print(json.dumps(record, ensure_ascii=False) if args.json else render_record(record))
    if not messages:
        thread = result.get("thread") or {}
        print(
            f"{PROG}: {thread.get('label') or args.permalink} has no messages to show",
            file=sys.stderr,
        )
    return 0


def _cmd_dm(args: argparse.Namespace, client: bus.BusClient) -> int:
    """One private message to one person (#441).

    Delivery first, then the record — ``_post_into_thread``'s order and its
    reason. What differs is that the record carries no body: that record fans
    out to every thread this session is connected to, which is exactly where a
    DM must not appear. docs/COMMS.md §12 has the rest.
    """
    text = resolve_text(args)
    delivered = _thread_work(
        client, args, _threads.OP_DM, "",
        on_timeout=_dm_is_indeterminate(args), recipient=args.recipient, text=text,
    )
    conversation = delivered.get("conversation") or {}
    who = conversation.get("recipient") or args.recipient
    where = conversation.get("channel") or ""
    posted = delivered.get("posted") or {}
    routing_receipt("dm", posted.get("ref"))
    if posted.get("clipped"):
        print(
            f"sent to {who}, clipped to what one message carries — and the whole "
            "text is nowhere else, so send the rest yourself if it mattered",
            file=sys.stderr,
        )
    else:
        print(f"sent to {who}", file=sys.stderr)
    try:
        client.post(
            KIND_NOTE,
            f"🔒 direct message sent to {who}" + (f" ({where})" if where else "")
            + f" — {len(text)} characters, body not recorded",
        )
    except (bus.NoChannel, bus.BusError) as exc:
        raise ArgsError(
            f"the message IS with {who} — do not send it again. What failed is "
            f"the note of it on this session's own conversation ({exc}), so "
            "nothing here will show that a direct message went out"
        ) from None
    if conversation.get("permalink"):
        print(conversation["permalink"])
    return 0


def _dm_is_indeterminate(args: argparse.Namespace) -> str:
    """What a timed-out ``dm`` says instead of "it failed".

    ``open``'s reason exactly (:func:`_open_is_indeterminate`): the request is
    already claimed, so the host may finish after this process stops waiting,
    and an agent told "failed" sends somebody a second copy of something
    private. There is no `connect`-shaped way out here — a conversation cannot
    be re-opened to find out — so the answer is the operator.
    """
    return (
        f"INDETERMINATE, not failed: the message may already be with "
        f"{args.recipient}, or nothing may have happened. Do NOT run this again "
        "— a second attempt sends them a second copy. Ask the operator, who can "
        "see whether it arrived"
    )


def _cmd_thread(args: argparse.Namespace, client: bus.BusClient) -> int:
    """Open, connect or disconnect one of this session's own threads (#429).

    The permalink goes to **stdout alone**, as ``ask``'s answer does, so a shell
    can capture the thread this just made; what happened goes to stderr.
    """
    if not args.thread_verb:
        raise ArgsError("say what to do with a thread: open, connect or disconnect")
    if args.thread_verb == "open":
        result = _thread_work(
            client, args, _threads.OP_OPEN, "",
            on_timeout=_open_is_indeterminate(args),
            channel=args.channel, text=args.text or "",
        )
    elif args.thread_verb == "connect":
        result = _thread_work(client, args, _threads.OP_CONNECT, args.permalink)
    else:
        result = _thread_work(client, args, _threads.OP_DISCONNECT, args.permalink)
    thread = result.get("thread") or {}
    where = thread.get("label") or thread.get("permalink") or "the thread"
    said = {
        "open": f"opened a thread in {where} and connected this session to it",
        "connect": f"connected this session to {where}",
        "disconnect": f"disconnected {where}; nothing flows either way now",
    }[args.thread_verb]
    print(f"{said}. The thread is told, and the run's threads show it.", file=sys.stderr)
    if thread.get("permalink"):
        print(thread["permalink"])
    return 0


def _open_is_indeterminate(args: argparse.Namespace) -> str:
    """What a timed-out ``open`` says instead of "it failed" (!299's review).

    The deadline was written for ``read`` and ``post``, where giving up costs a
    repeated message at worst. ``open`` creates, and its request is already
    claimed — so the host may finish after this process stops waiting, and an
    agent told "failed" retries a root message that cannot be unposted. Hence
    the way out being named too: ``thread connect`` answers either way, because
    a thread this run already holds comes back as the duplicate 409.
    """
    return (
        f"INDETERMINATE, not failed: the thread may already exist in "
        f"{args.channel} with this run connected to it, or nothing may have "
        "happened. Do NOT run this again — a second attempt posts a second root "
        "message in somebody's channel. Find out first: the root names this run, "
        "so look in the channel, or ask the operator, who sees this run's threads "
        "in the control UI. If it is there, `lmer-signal thread connect "
        "<permalink>` either connects it or answers that this run already holds "
        "it, and both of those are the answer"
    )


def _thread_work(
    client: bus.BusClient, args: argparse.Namespace, op: str, permalink: str,
    on_timeout: str = "", **fields
) -> dict:
    """Queue one thread request and block for the host side's answer.

    The wait is a chain of long-polls, like every other wait here. A servicer
    that took the request and died leaves it unanswered, which is why this has a
    deadline at all: the request was claimed, so nothing will retry it.

    *on_timeout* replaces that deadline's message, for an op where "it may be
    gone" is the wrong reading — see :func:`_open_is_indeterminate`.
    """
    queued = client.thread_request(op, permalink, **fields)
    request_id = (queued.get("request") or {}).get("id")
    if not request_id:
        raise bus.BusError("the supervisor accepted the request without naming it")
    deadline = time.monotonic() + max(0.0, args.timeout)
    while True:
        remaining = deadline - time.monotonic()
        state = client.thread_result(request_id, timeout=max(0.0, remaining))
        result = (state.get("request") or {}).get("result")
        if result is not None:
            break
        if remaining <= 0:
            raise ArgsError(
                f"the host side has not answered in {args.timeout:.0f}s — "
                + (
                    on_timeout if on_timeout
                    else "it may be gone; nothing retries a request it has "
                         "already taken"
                )
            )
    if not result.get("ok"):
        raise ArgsError(str(result.get("error") or "the host side could not do it"))
    return result


def _cmd_ask(args: argparse.Namespace, client: bus.BusClient) -> int:
    # Before posting, not after: a question asked and then refused for a bad
    # flag would sit on every channel with nobody waiting on it.
    if not args.no_wait:
        check_wait_flags(args)
    result = client.post(
        KIND_QUESTION, resolve_text(args), options=args.options,
        attachments=_stage_attachments(args.attachments),
        meta={META_OPERATOR_ONLY: True} if args.operator_only else None,
        # The claim starts with the question, not with the first poll: an
        # answer landing in between used to be typed into the harness as well
        # as returned here.
        wait=None if args.no_wait else _claim_ttl(args),
    )
    question = result["message"]
    routing_receipt("ask", question.get("id"))
    if args.no_wait:
        print(format_id(question))
        return 0
    print(f"asked (question {format_id(question)}); waiting for the first answer from any channel…", file=sys.stderr)
    return _await(client, question, args)


def _claim_ttl(args: argparse.Namespace) -> float:
    """How long the supervisor holds the answer for this process. A wait
    without a deadline still leases: the claim is refreshed by every poll, so
    what this bounds is how long an answer waits for a CLI that died."""
    timeout = getattr(args, "timeout", DEFAULT_TIMEOUT) or 0.0
    return (timeout if timeout > 0 else bus.MAX_POLL_SECONDS) + CLAIM_GRACE


def _cmd_wait(args: argparse.Namespace, client: bus.BusClient) -> int:
    check_wait_flags(args)
    state = _require_question(client, args.id)
    question = state["question"]
    if state["state"] == "closed":
        # Not TIMEOUT_EXIT_CODE: that code promises the question is still open
        # and a later wait will find the answer, and after a close neither is
        # true — the supervisor files any reply as late.
        raise ArgsError(
            f"question {format_id(question)} was closed, so nobody can answer it now — "
            "ask again if you still need to know"
        )
    client.claim(question["id"], _claim_ttl(args))
    return _await(client, question, args)


def _await(client: bus.BusClient, question: dict, args: argparse.Namespace) -> int:
    """Block for the accepted answer and print it, or report the timeout.

    The answer goes to **stdout alone** so it can be captured; everything else
    this command says goes to stderr. The wait is a chain of long-polls with
    ``waiter=<question>``, which is how the supervisor knows to hand the answer
    to this process rather than type it into the harness.
    """
    answer = None
    deadline = None if args.timeout <= 0 else time.monotonic() + args.timeout
    cursor = int(question["seq"])
    try:
        answer = _find_answer(client, question)
        while answer is None:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            poll = bus.MAX_POLL_SECONDS if remaining is None else min(bus.MAX_POLL_SECONDS, remaining)
            messages, cursor = client.messages(
                cursor, timeout=max(poll, args.interval if poll > 0 else 0.0),
                kinds=[KIND_ANSWER], waiter=question["id"],
            )
            for record in messages:
                if _answers(record, question):
                    answer = record
                    break
    finally:
        with contextlib.suppress(bus.BusError):
            client.release(question["id"])
    if answer is None:
        qid = format_id(question)
        # Four lines on every timeout, because the resume line alone was not
        # enough: an agent that had it quoted in front of it still went on to
        # other work, never waited again, and recorded "unanswered" while the
        # reply sat on the channel. The loop, the hard rule and the watch
        # suggestion are what that agent was missing.
        print(
            f"no answer to question {qid} yet. It is still open — resume the wait with: "
            f"lmer-signal wait {qid} (or, if you have decided without an answer: "
            f"lmer-signal close {qid}).\n"
            "Answers arrive while you work: if you go on to other tasks, re-run that "
            "wait between them.\n"
            "HARD RULE: before you conclude nobody has answered — and before you record "
            f"any stop reason or end this session — run lmer-signal wait {qid} (or "
            "lmer-signal list --open) one final time.\n"
            "If you have a monitor or watch tool, arm it to re-run that wait so an idle "
            "session still hears the answer.",
            file=sys.stderr,
        )
        return TIMEOUT_EXIT_CODE
    print(answer["text"])
    print(f"answered from {_who(answer)}", file=sys.stderr)
    return 0


def _who(record: dict) -> str:
    """The record's source, with the authority the platform stamped (#594)."""
    source = record.get("source") or {}
    who = source.get("channel", "?")
    if source.get("author"):
        who = f"{who} · {source['author']}"
    authority = (record.get("meta") or {}).get(META_AUTHORITY)
    # Only the assistant's channel is ever stamped; elsewhere the key is a
    # sender's own meta that an older supervisor kept.
    if authority and source.get("channel") == CHANNEL_UBER:
        who = f"{who} · authority={authority}"
    return who


def _answers(record: dict, question: dict) -> bool:
    return (
        record.get("kind") == KIND_ANSWER
        and record.get("in_reply_to") == question["id"]
        and not (record.get("meta") or {}).get("late")
    )


def _find_answer(client: bus.BusClient, question: dict) -> Optional[dict]:
    """The accepted answer if one already exists (an answer before the wait
    began must return immediately rather than cost a poll)."""
    messages, _cursor = client.messages(int(question["seq"]), kinds=[KIND_ANSWER], waiter=question["id"])
    for record in messages:
        if _answers(record, question):
            return record
    return None


def _require_question(client: bus.BusClient, ref: str) -> dict:
    state = client.question(ref)
    if state is None:
        raise ArgsError(f"no question {ref!r} on this session")
    return state


def _cmd_close(args: argparse.Namespace, client: bus.BusClient) -> int:
    """Stop waiting for an answer to a question this session asked.

    Exit 0 in every outcome that is not a broken channel, because this is the
    verb an exit path calls: a question already closed is done, and one that
    was answered a moment before the close is *also* done — that answer is
    printed on stdout exactly as ``wait`` would print it, so the reply is
    handed to the caller instead of being closed over.
    """
    state = _require_question(client, args.id)
    question = state["question"]
    qid = format_id(question)
    if state["state"] == "closed":
        print(f"question {qid} was already closed", file=sys.stderr)
        return 0
    if state["state"] == "answered":
        try:
            answer = _find_answer(client, question)
        finally:
            with contextlib.suppress(bus.BusError):
                client.release(question["id"])
        print(f"question {qid} was answered before it could be closed — the answer is on stdout", file=sys.stderr)
        if answer is not None:
            print(answer["text"])
        return 0
    client.post(KIND_CLOSE, args.reason or "", in_reply_to=question["id"])
    print(f"closed question {qid}; nobody can reply to it now", file=sys.stderr)
    return 0


def _cmd_list(args: argparse.Namespace, client: bus.BusClient) -> int:
    if args.open_only:
        states = client.questions(open_only=True)
        for state in states:
            question = state["question"]
            if args.json:
                print(json.dumps(question, ensure_ascii=False))
            else:
                _print_record(question, "open")
        return 0
    messages, _cursor = client.messages(max(0, args.since))
    fold = {state["question"]["id"]: state for state in client.questions()}
    for record in messages:
        if args.json:
            print(json.dumps(record, ensure_ascii=False))
            continue
        state = fold.get(record["id"], {}).get("state") if record.get("kind") == KIND_QUESTION else None
        _print_record(record, state)
    return 0


def _print_record(record: dict, state: Optional[str]) -> None:
    head = f"[{format_id(record)}] {record.get('kind')} from {_who(record)}"
    if state:
        head += f" ({state})"
    meta = record.get("meta") or {}
    if meta.get(META_OPERATOR_ONLY):
        head += " (operator-only)"
    if meta.get("late"):
        head += " (late)"
    reply_to = record.get("in_reply_to")
    if reply_to:
        head += f" → #{reply_to.rpartition('/')[2].lstrip('0') or '0'}"
    print(f"{head}: {record.get('text', '')}")
    if record.get("options"):
        print(f"    options: {', '.join(record['options'])}")


def _cmd_end_session(args: argparse.Namespace, client: bus.BusClient) -> int:
    """Post an optional goodbye, then end the session.

    The goodbye is best effort and the shutdown is not: freeing the slot is
    what the verb is for, so a conversation nobody is listening to must not
    keep a session alive to its idle timeout.
    """
    from lmer_cli.session_end import request_session_end

    if args.text is not None or args.message_file or args.stdin:
        try:
            client.post(KIND_MESSAGE, resolve_text(args))
        except (bus.NoChannel, bus.BusError) as exc:
            print(f"{PROG}: could not post the goodbye: {exc}", file=sys.stderr)
    result = request_session_end()
    if not result.ok:
        print(f"{PROG}: {result.message}", file=sys.stderr)
        return result.code
    print(result.message.capitalize())
    return 0


_COMMANDS = {
    "milestone": _cmd_milestone,
    "note": _cmd_note,
    "post": _cmd_post,
    "read": _cmd_read,
    "dm": _cmd_dm,
    "thread": _cmd_thread,
    "ask": _cmd_ask,
    "wait": _cmd_wait,
    "close": _cmd_close,
    "list": _cmd_list,
    "end-session": _cmd_end_session,
}


def normalize_argv(argv: List[str]) -> List[str]:
    """Turn the bare-text milestone form into ``milestone <text>``.

    The first argument decides: a known verb, ``-h``/``--help``, or the
    ``--message-file``/``--stdin``/``-`` text sources leave the argv alone
    (those are ``milestone``'s own flags and get the verb prepended); anything
    else is milestone text.
    """
    if not argv:
        return argv
    first = argv[0]
    if first in VERBS or first in ("-h", "--help"):
        return list(argv)
    return ["milestone", *argv]


def main(argv: Optional[List[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = create_parser()
    args = parser.parse_args(normalize_argv(raw))
    if not args.verb:
        parser.print_usage(sys.stderr)
        return 1
    handler = _COMMANDS[args.verb]
    try:
        return handler(args, _LazyClient())
    except bus.NoChannel as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return NO_CHANNEL_EXIT_CODE
    except (ArgsError, bus.BusError) as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            f"{PROG}: interrupted. Any question already posted is still open; "
            "`lmer-signal list --open` shows it.",
            file=sys.stderr,
        )
        return TIMEOUT_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
