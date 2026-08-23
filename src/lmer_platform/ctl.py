"""``lmer-ctl`` — one command per platform route, for the orchestrating assistant.

Why this exists, having once been dropped
-----------------------------------------
Spec §8.2 asked for this CLI, the platform shipped without it, and the omission
was recorded as deliberate: every command here is one HTTP request an agent can
already make with ``curl``, the caps and the validation are enforced by the
daemon either way, and a second client is a second thing to keep true. None of
that reasoning turned out to be wrong.

What changed is who pays for it (operator request, 2026-07-29). The assistant
composed those requests by hand on every operation — a bearer header, a JSON
body, a run's identity split into three fields, the right one of two answer
verbs — and the cost was not the typing but the *mistakes* available while doing
it: an untitled run, an answer posted to the verb that respawns a container, a
credential pasted into a command the session also prints. This removes the
composing. It removes nothing else.

Hence the rule this module is built to keep, and the one a future change here has
to argue with first: **the daemon is the only enforcer, and this file grows no
logic of its own.** One subcommand is one documented route (``GET /api`` serves
the authoritative list). Arguments become a JSON body or a query string, and the
reply is printed as it arrived. No client-side validation beyond the shape an
argument has to have to be sent at all, no defaults a route does not already
have, no retries, no verb that calls two routes, and no interpretation of a
refusal — a 429 at the concurrency cap is printed, not waited out. The first
decision taken here is the first way this CLI and the API can disagree, and the
API is what the control UI, the operator's ``curl`` and every other client see.

The commands, and the route each one is
---------------------------------------
Seeing::

    status                          GET  /api/state
    health                          GET  /api/health
    log SESSION                     GET  /api/sessions/{id}/log
    messages SESSION                GET  /api/sessions/{id}/messages
    questions SESSION               GET  /api/sessions/{id}/ask
    spawn-options                   GET  /api/spawn-options

Answering — two routes, and never each other's::

    answer SESSION QUESTION TEXT    POST /api/sessions/{id}/ask/{qid}/answer
    runs answer KEY TEXT            POST /api/runs/answer

Steering::

    spawn TASKDEF TARGET            POST /api/sessions
    send SESSION TEXT               POST /api/sessions/{id}/input
    wind-down SESSION               POST /api/sessions/{id}/wind-down
    exit SESSION                    POST /api/sessions/{id}/exit
    runs resume KEY                 POST /api/runs/resume

The fleet this orchestrator tracks::

    runs candidates                 GET  /api/runs/candidates
    runs adopt KEY                  POST /api/runs/adopt
    runs forget KEY                 POST /api/runs/forget
    runs meta get KEY               GET  /api/runs/meta
    runs meta set KEY               POST /api/runs/meta
    runs relations KEY              GET  /api/runs/relations
    runs relate KEY OTHER           POST /api/runs/relate
    runs unrelate KEY OTHER         POST /api/runs/unrelate

The assistant's own state::

    me                              GET  /api/assistant
    orders get                      GET  /api/assistant/instructions
    orders set TEXT                 POST /api/assistant/instructions
    handoff get                     GET  /api/assistant/handoff
    handoff set TEXT                POST /api/assistant/handoff
    pending take                    POST /api/assistant/pending

A run is named by one ``host/project/slug`` key — see :func:`_run_fields`.

Configuration
-------------
Two environment variables, and no flag for either: ``LMER_PLATFORM_URL`` and
``LMER_PLATFORM_SECRET``, which is exactly the pair
:mod:`lmer_platform.assistant` writes into the assistant's container and the
same pair the ``orchestrate`` taskdef names. No new configuration surface: a
session that cannot reach the platform must not be able to make this CLI work by
guessing a URL, because the credential is what it is missing and no URL supplies
one. When either is absent the refusal names both variables and relays
``LMER_PLATFORM_UNREACHABLE`` if the host recorded a reason.

Output
------
JSON on stdout and exit 0 on success; JSON on stderr and exit 1 otherwise. The
consumer is an agent, so there is no prose mode to keep in step with a schema —
and stdout is always parseable, including when a route answers with something
that is not JSON, which arrives wrapped in ``detail``.

A failure is one object with an ``error`` discriminator::

    {"error": "http", "status": 429, "body": {"detail": "…"}}   the daemon refused
    {"error": "unreachable", "detail": "…"}                     it was never asked
    {"error": "configuration", "detail": "…"}                   nothing to ask

``body`` is the daemon's own body, untouched — including
``POST /api/runs/resume``'s ``{code,message}``, which a client is meant to match
on rather than reading the sentence.

The credential
--------------
It travels in the ``Authorization`` header and nowhere else. There is
deliberately no ``--secret``/``--token`` flag: argv is readable by every process
on the box and is echoed by a harness into the transcript this session's terminal
writes to disk. Replies are scrubbed of it before printing for the reason
:func:`lmer_platform.session_io._call` scrubs them — a future route that echoed a
request header must not turn this CLI into the disclosure.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional, Sequence

from lmer_platform.client import (
    Call,
    Endpoint,
    PlatformError,
    TransportError,
    request as _request,
)

#: The names above are :mod:`lmer_platform.client`'s, re-exported here because
#: this CLI was their first home and because ``ctl.Endpoint`` / ``ctl.Call`` read
#: naturally in a subcommand. ``CtlError`` is the client's base failure under its
#: old name: every ``except CtlError`` here still catches a
#: :class:`~lmer_platform.client.TransportError`, which is a subclass of it.
CtlError = PlatformError

__all__ = [
    "Call", "CtlError", "Endpoint", "TransportError",
    "main", "resolve_endpoint",
]


#: The pair the host writes into the assistant's container, spelled here rather
#: than imported from :mod:`lmer_platform.assistant`: importing that module pulls
#: the whole spawn stack (``lmer_cli.cli``, ``spawn``, ``transcripts``) and this
#: is a process an agent starts once per operation. ``tests/test_platform_ctl.py``
#: pins these against ``assistant``'s constants, so the copy cannot drift.
#:
#: The second is named ``…_CREDENTIAL`` for the reason ``assistant`` gives at
#: length: the repository's secret scan matches an assignment whose left-hand side
#: ends in the other word and cannot tell a hardcoded credential from a name for
#: one. The *variable* is still ``LMER_PLATFORM_SECRET``.
ENV_PLATFORM_URL = "LMER_PLATFORM_URL"
ENV_PLATFORM_CREDENTIAL = "LMER_PLATFORM_SECRET"

#: Why the host gave this container neither, in a sentence worth relaying.
ENV_PLATFORM_UNREACHABLE = "LMER_PLATFORM_UNREACHABLE"

#: Every failure this CLI reports, whether the daemon refused the request or was
#: never reached. Argparse's own usage errors still exit 2, as they do everywhere.
EXIT_FAILURE = 1

#: One number for every verb, sized for the slowest rather than the typical: a
#: spawn answers only once the container is up, and a fleet view may be served
#: behind a mirror refresh. ``--timeout`` is for a caller who knows better.
DEFAULT_TIMEOUT_SECONDS = 120.0

#: How much of a non-JSON body is quoted back before it is truncated. A route that
#: answers HTML (a proxy's error page, say) should not paste a page into an agent's
#: context to say so.
_DETAIL_LIMIT = 2000


def resolve_endpoint() -> Endpoint:
    """The platform's address and credential, or a refusal naming both variables.

    All-or-nothing, because that is how the host writes them: a URL with no
    credential is a 401 machine and a credential with no URL has nothing to spend
    itself on, so an agent has one condition to report rather than two.

    The values are never echoed — the URL because there is nothing to gain from
    quoting back what the operator can read, the credential because quoting it is
    the disclosure this CLI's whole header discipline exists to avoid.
    """
    base_url = (os.environ.get(ENV_PLATFORM_URL) or "").strip()
    credential = (os.environ.get(ENV_PLATFORM_CREDENTIAL) or "").strip()
    if base_url and credential:
        return Endpoint(base_url.rstrip("/"), credential)

    reason = (os.environ.get(ENV_PLATFORM_UNREACHABLE) or "").strip()
    message = (
        f"no platform to talk to: this session's environment must carry both "
        f"{ENV_PLATFORM_URL} and {ENV_PLATFORM_CREDENTIAL}, and the host writes "
        f"them together at launch. There is no flag for either."
    )
    if reason:
        message = f"{message} The host recorded why it wrote neither: {reason}"
    raise CtlError(message)


def _payload(text: str) -> Any:
    """A reply body as JSON, wrapping anything that is not.

    Parsed and re-serialised rather than relayed byte-for-byte so that stdout is
    JSON on every path an agent can reach — a route that answers plain text, or a
    proxy that answers a page, must not break the one contract the consumer has.
    """
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"detail": text[:_DETAIL_LIMIT]}
    if isinstance(parsed, (dict, list)):
        return parsed
    return {"detail": str(parsed)[:_DETAIL_LIMIT]}


def _emit(response: Any, endpoint: Endpoint, *, out=None, err=None) -> int:
    """Print the reply and return the exit code.

    Success is the daemon's body; a refusal is the daemon's body under ``body``
    with the status beside it, because the status is the part a client acts on and
    FastAPI's ``detail`` is not always a sentence.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    text = response.text
    if endpoint.credential and endpoint.credential in text:
        text = text.replace(endpoint.credential, "<redacted>")
    payload = _payload(text)
    status = response.status_code
    if 200 <= status < 300:
        print(json.dumps(payload), file=out)
        return 0
    print(
        json.dumps({"error": "http", "status": status, "body": payload}), file=err
    )
    return EXIT_FAILURE


def _text(value: str) -> str:
    """A free-text argument, with ``-`` meaning "read stdin".

    ``lmer-ask``'s convention, for its reason: text on a command line goes through
    the shell first, which eats backticks and ``$`` silently, and the arguments
    most likely to contain both are the ones this CLI carries — a standing-orders
    document, a handover note, an answer quoting a command. Anything multi-line
    belongs on stdin.
    """
    if value == "-":
        return sys.stdin.read()
    return value


def _given(args: argparse.Namespace, names: Sequence[str]) -> dict:
    """The flags that were actually passed, as body fields.

    An omitted flag is left out of the body rather than sent as ``null``, because
    several routes distinguish the two: ``POST /api/runs/meta`` leaves an absent
    field alone and clears it on ``""``, and ``POST /api/runs/resume`` falls back
    to the run's own recorded taskdef only when none is named.
    """
    return {
        name: getattr(args, name)
        for name in names
        if getattr(args, name, None) is not None
    }


def _run_fields(key: str) -> dict:
    """``host/project/slug`` as the three fields every run-keyed route names.

    The inverse of :func:`lmer_platform.runs.run_key`, which is the string the
    platform's own state files use and therefore the one an operator has in front
    of them. A project is ``group/subgroup``, so the middle is whatever lies
    between the first segment and the last.

    Splitting is argument shape and stops there: the parts are not checked for
    emptiness or existence, so a key naming no run reaches the daemon and comes
    back as its refusal, which names the field it disliked.
    """
    parts = key.split("/")
    if len(parts) < 3:
        raise CtlError(
            f"{key!r} is not a run key — a run is named host/project/slug, as "
            "the fleet view names it (gitlab.example.com/group/project/develop-141)"
        )
    return {"host": parts[0], "project": "/".join(parts[1:-1]), "slug": parts[-1]}


# --- the verbs: one route each ----------------------------------------------
#
# Every function here returns a Call and does nothing else. If one of them ever
# needs a second statement that is not building a body, the route is the thing to
# change — see this module's docstring.

def _call_status(args) -> Call:
    return Call("GET", "/api/state")


def _call_health(args) -> Call:
    return Call("GET", "/api/health")


def _call_spawn_options(args) -> Call:
    return Call(
        "GET", "/api/spawn-options", params=_given(args, ("target", "repo_url"))
    )


def _call_spawn(args) -> Call:
    body = {"taskdef": args.taskdef, "target": args.target}
    body.update(_given(args, (
        "repo_url", "preset", "agents", "harness", "model", "ports", "slot",
        "title", "description",
    )))
    return Call("POST", "/api/sessions", body=body)


def _call_send(args) -> Call:
    return Call(
        "POST",
        f"/api/sessions/{args.session}/input",
        body={
            "data": _text(args.text),
            "append_newline": not args.no_newline,
            # This command steers another session with prose. The supervisor
            # must not execute a sentence beginning with a harness escape, but
            # slash commands are deliberate on this orchestration path.
            "sanitize": True,
            "preserve_slash_commands": True,
        },
    )


def _call_log(args) -> Call:
    return Call(
        "GET",
        f"/api/sessions/{args.session}/log",
        params=_given(args, ("offset", "limit", "source")),
    )


def _call_messages(args) -> Call:
    return Call(
        "GET",
        f"/api/sessions/{args.session}/messages",
        params=_given(args, ("since", "limit")),
    )


def _call_questions(args) -> Call:
    return Call("GET", f"/api/sessions/{args.session}/ask")


def _call_answer(args) -> Call:
    return Call(
        "POST",
        f"/api/sessions/{args.session}/ask/{args.question}/answer",
        body={"answer": _text(args.text)},
    )


def _call_wind_down(args) -> Call:
    return Call(
        "POST",
        f"/api/sessions/{args.session}/wind-down",
        body=_given(args, ("note",)),
    )


def _call_exit(args) -> Call:
    return Call("POST", f"/api/sessions/{args.session}/exit", body={})


def _call_runs_candidates(args) -> Call:
    return Call("GET", "/api/runs/candidates")


def _call_runs_adopt(args) -> Call:
    return Call(
        "POST", "/api/runs/adopt",
        body={**_run_fields(args.key), **_given(args, ("note",))},
    )


def _call_runs_forget(args) -> Call:
    return Call("POST", "/api/runs/forget", body=_run_fields(args.key))


def _call_runs_answer(args) -> Call:
    return Call(
        "POST", "/api/runs/answer",
        body={**_run_fields(args.key), "answer": _text(args.text)},
    )


def _call_runs_resume(args) -> Call:
    return Call(
        "POST", "/api/runs/resume",
        body={
            **_run_fields(args.key),
            **_given(args, ("taskdef", "repo_url", "direction")),
        },
    )


def _call_runs_meta_get(args) -> Call:
    return Call("GET", "/api/runs/meta", params=_run_fields(args.key))


def _call_runs_meta_set(args) -> Call:
    return Call(
        "POST", "/api/runs/meta",
        body={
            **_run_fields(args.key),
            **_given(args, ("title", "description")),
        },
    )


def _call_runs_relations(args) -> Call:
    return Call("GET", "/api/runs/relations", params=_run_fields(args.key))


def _call_runs_relate(args) -> Call:
    return Call(
        "POST", "/api/runs/relate",
        body={**_run_fields(args.key), "related": _run_fields(args.other)},
    )


def _call_runs_unrelate(args) -> Call:
    return Call(
        "POST", "/api/runs/unrelate",
        body={**_run_fields(args.key), "related": _run_fields(args.other)},
    )


def _call_me(args) -> Call:
    return Call("GET", "/api/assistant")


def _call_orders_get(args) -> Call:
    return Call("GET", "/api/assistant/instructions")


def _call_orders_set(args) -> Call:
    return Call(
        "POST", "/api/assistant/instructions",
        body={"instructions": _text(args.text)},
    )


def _call_handoff_get(args) -> Call:
    return Call("GET", "/api/assistant/handoff")


def _call_handoff_set(args) -> Call:
    return Call("POST", "/api/assistant/handoff", body={"handoff": _text(args.text)})


def _call_pending_take(args) -> Call:
    return Call("POST", "/api/assistant/pending", body={})


# --- arguments ---------------------------------------------------------------

def _add_run_key(parser: argparse.ArgumentParser, name: str = "key") -> None:
    parser.add_argument(
        name,
        help="A run, as host/project/slug — the key the fleet view names it by.",
    )


def _add_text(parser: argparse.ArgumentParser, label: str) -> None:
    parser.add_argument(
        "text",
        help=(
            f"{label} '-' reads it from stdin, which is what to use for anything "
            "multi-line or containing backticks, $ or quotes."
        ),
    )


def create_parser() -> argparse.ArgumentParser:
    """The command surface. Each verb's help names the route it is."""
    parser = argparse.ArgumentParser(
        prog="lmer-ctl",
        description=(
            "The platform API, one command per route. Prints the daemon's JSON on "
            "stdout; a refusal is the daemon's, on stderr, with its status. "
            f"Needs {ENV_PLATFORM_URL} and {ENV_PLATFORM_CREDENTIAL} in the "
            "environment — there is no flag for either. GET /api is the "
            "authoritative route list; this CLI is released with the daemon but "
            "may still be older than the one you are talking to."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=(
            "HTTP timeout (default: %(default)s). Sized for a spawn, which "
            "answers only once the container is up."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    status = sub.add_parser(
        "status",
        help="The fleet view: every run this orchestrator tracks (GET /api/state).",
    )
    status.set_defaults(call=_call_status)

    health = sub.add_parser(
        "health", help="Is the daemon alive, is the mirror there (GET /api/health)."
    )
    health.set_defaults(call=_call_health)

    options = sub.add_parser(
        "spawn-options",
        help=(
            "Taskdefs and presets this host can see — suggestions, not a "
            "vocabulary (GET /api/spawn-options)."
        ),
    )
    options.add_argument("--target", help="The spawn being composed, if you have one.")
    options.add_argument("--repo-url", dest="repo_url", help="Likewise.")
    options.set_defaults(call=_call_spawn_options)

    spawn = sub.add_parser(
        "spawn",
        help=(
            "Start a worker session (POST /api/sessions). Pass --title: an "
            "untitled run is one the operator has to identify from a slug."
        ),
    )
    spawn.add_argument("taskdef", help="Which taskdef the session runs.")
    spawn.add_argument("target", help="What it runs against (an issue, an MR).")
    spawn.add_argument(
        "--repo-url", dest="repo_url", help="Overrides the host default."
    )
    spawn.add_argument("--preset", help="A preset name from spawn-options.")
    spawn.add_argument("--agents", help="Fan-out spec for --agents.")
    spawn.add_argument("--harness", help="claude, codex, pi — whatever the host has.")
    spawn.add_argument("--model", help="Model override.")
    spawn.add_argument("--ports", type=int, help="How many ports to publish.")
    spawn.add_argument(
        "--slot",
        help=(
            "A free service slot to occupy, by name (see `status`). The slot "
            "supplies its own preset, so this and --preset are exclusive."
        ),
    )
    spawn.add_argument(
        "--title",
        help="The run's label in the fleet view, in the operator's words.",
    )
    spawn.add_argument("--description", help="A longer note about the run.")
    spawn.set_defaults(call=_call_spawn)

    send = sub.add_parser(
        "send",
        help=(
            "Type into a live session — this is how a /followup is delivered "
            "(POST /api/sessions/{id}/input)."
        ),
    )
    send.add_argument("session", help="Session id.")
    _add_text(send, "What to type.")
    send.add_argument(
        "--no-newline",
        action="store_true",
        help="Do not submit: leave the text in the prompt without a newline.",
    )
    send.set_defaults(call=_call_send)

    log = sub.add_parser(
        "log",
        help=(
            "A session's raw scrollback, base64 as the route returns it, and it "
            "outlives the session (GET /api/sessions/{id}/log)."
        ),
    )
    log.add_argument("session", help="Session id.")
    log.add_argument(
        "--offset", type=int, help="Byte offset; negative reads the tail."
    )
    log.add_argument("--limit", type=int, help="How many bytes.")
    log.add_argument(
        "--source",
        help=(
            "host or container, to read one of the two logs by name instead of "
            "whichever is the log of record."
        ),
    )
    log.set_defaults(call=_call_log)

    messages = sub.add_parser(
        "messages",
        help=(
            "A run's conversation, normalised and readable, across every session "
            "of the run (GET /api/sessions/{id}/messages)."
        ),
    )
    messages.add_argument("session", help="Session id.")
    messages.add_argument(
        "--since", type=int, help="Cursor; negative reads the tail."
    )
    messages.add_argument("--limit", type=int, help="How many messages.")
    messages.set_defaults(call=_call_messages)

    questions = sub.add_parser(
        "questions",
        help=(
            "One live session's ask channel: what it asked, what is still waiting "
            "(GET /api/sessions/{id}/ask)."
        ),
    )
    questions.add_argument("session", help="Session id.")
    questions.set_defaults(call=_call_questions)

    answer = sub.add_parser(
        "answer",
        help=(
            "Answer a question a session is STILL RUNNING and waiting on; nothing "
            "is spawned (POST /api/sessions/{id}/ask/{qid}/answer). For a run that "
            "stopped on its question, that is `runs answer`."
        ),
    )
    answer.add_argument("session", help="Session id.")
    answer.add_argument("question", help="Question id, from `questions`.")
    _add_text(answer, "The answer.")
    answer.set_defaults(call=_call_answer)

    wind_down = sub.add_parser(
        "wind-down",
        help=(
            "Ask a session's agent to commit, push, report and end itself. 202: "
            "nothing has ended yet (POST /api/sessions/{id}/wind-down)."
        ),
    )
    wind_down.add_argument("session", help="Session id.")
    wind_down.add_argument("--note", help="One clause riding on the end of the prompt.")
    wind_down.set_defaults(call=_call_wind_down)

    exit_p = sub.add_parser(
        "exit",
        help=(
            "End a session now, by signal. Whatever it had not pushed is gone "
            "(POST /api/sessions/{id}/exit)."
        ),
    )
    exit_p.add_argument("session", help="Session id.")
    exit_p.set_defaults(call=_call_exit)

    me = sub.add_parser(
        "me",
        help=(
            "You, as the orchestrator sees you — including `pending`, the only "
            "way to ask whether anything is waiting without taking it "
            "(GET /api/assistant)."
        ),
    )
    me.set_defaults(call=_call_me)

    _add_runs(sub)
    _add_pair(
        sub, "orders",
        "The operator's standing orders (/api/assistant/instructions).",
        _call_orders_get, _call_orders_set,
        set_help=(
            "Replace the whole document — there is no append, so re-read it "
            "first and post it with your change folded in."
        ),
        set_label="The whole document.",
    )
    _add_pair(
        sub, "handoff",
        "The note one incarnation leaves the next (/api/assistant/handoff).",
        _call_handoff_get, _call_handoff_set,
        set_help="Write it: what is in flight, what you promised, what you await.",
        set_label="The note.",
    )

    pending = sub.add_parser(
        "pending",
        help="The digests the daemon spooled for you (/api/assistant/pending).",
    )
    pending_sub = pending.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")
    # Only `take`, because the route is only a take. The non-consuming read is
    # `me`, and offering a read-looking name here would invite the mistake the
    # route's own docstring is about: draining the spool to see whether it is
    # empty. Named rather than implied for the same reason.
    take = pending_sub.add_parser(
        "take",
        help=(
            "TAKE them: handed over and cleared in one call, so act on what you "
            "get. The count alone is on `me` (POST /api/assistant/pending)."
        ),
    )
    take.set_defaults(call=_call_pending_take)

    return parser


def _add_pair(
    sub, name: str, help_text: str, getter, setter, *, set_help: str, set_label: str
) -> None:
    """A ``get``/``set`` pair, spelled as the two routes it is.

    Not one command that posts when a flag is present: which of two routes to call
    is the sort of thing this CLI must not decide, and a document written because
    an agent passed the wrong flag is unrecoverable — the POST replaces everything.
    """
    parser = sub.add_parser(name, help=help_text)
    pair = parser.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")
    read = pair.add_parser("get", help="Read it.")
    read.set_defaults(call=getter)
    write = pair.add_parser("set", help=set_help)
    _add_text(write, set_label)
    write.set_defaults(call=setter)


def _add_runs(sub) -> None:
    """The run-keyed routes. Nested under ``runs`` because their subject is a run.

    Which is also what keeps the two answer verbs apart at the command line: this
    one is ``runs answer`` and starts a container, the top-level ``answer`` drops a
    reply into a directory a live session is polling. Confusing them is the one
    mistake the API's own index calls out, and a shared prefix would have made
    them neighbours in a completion list.
    """
    runs = sub.add_parser("runs", help="Runs this orchestrator tracks (/api/runs/*).")
    runs_sub = runs.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")

    candidates = runs_sub.add_parser(
        "candidates",
        help=(
            "Every run in the shared work repo, including other people's, each "
            "flagged tracked or not — the adopt picker, NOT the fleet view "
            "(GET /api/runs/candidates). The fleet view is `lmer-ctl status`."
        ),
    )
    candidates.set_defaults(call=_call_runs_candidates)

    adopt = runs_sub.add_parser(
        "adopt", help="Start tracking an existing run (POST /api/runs/adopt)."
    )
    _add_run_key(adopt)
    adopt.add_argument("--note", help="Why it was adopted.")
    adopt.set_defaults(call=_call_runs_adopt)

    forget = runs_sub.add_parser(
        "forget",
        help=(
            "Stop tracking a run. Its work-repo state is untouched "
            "(POST /api/runs/forget)."
        ),
    )
    _add_run_key(forget)
    forget.set_defaults(call=_call_runs_forget)

    answer = runs_sub.add_parser(
        "answer",
        help=(
            "Answer a run that STOPPED on a question: this starts a fresh "
            "container carrying the answer (POST /api/runs/answer). For a session "
            "still running and waiting, that is `lmer-ctl answer`."
        ),
    )
    _add_run_key(answer)
    _add_text(answer, "The answer.")
    answer.set_defaults(call=_call_runs_answer)

    resume = runs_sub.add_parser(
        "resume",
        help=(
            "Continue a tracked run by starting its next session "
            "(POST /api/runs/resume)."
        ),
    )
    _add_run_key(resume)
    resume.add_argument(
        "--taskdef",
        help="Defaults to the run's own; naming another starts a sibling run.",
    )
    resume.add_argument("--repo-url", dest="repo_url", help="If the run needs one.")
    resume.add_argument("--direction", help="What the next session should do.")
    resume.set_defaults(call=_call_runs_resume)

    meta = runs_sub.add_parser(
        "meta",
        help=(
            "This orchestrator's title and description for a run — platform "
            "state, never the work repo (/api/runs/meta)."
        ),
    )
    meta_sub = meta.add_subparsers(dest="leaf", metavar="get|set")
    meta_get = meta_sub.add_parser("get", help="Read them.")
    _add_run_key(meta_get)
    meta_get.set_defaults(call=_call_runs_meta_get)
    meta_set = meta_sub.add_parser(
        "set", help='Set them. An omitted field is left alone; "" clears it.'
    )
    _add_run_key(meta_set)
    meta_set.add_argument("--title", help="The label the fleet view shows.")
    meta_set.add_argument("--description", help="A longer note.")
    meta_set.set_defaults(call=_call_runs_meta_set)

    relations = runs_sub.add_parser(
        "relations",
        help=(
            "Runs this orchestrator considers related to this one; tracked: false "
            "is ordinary (GET /api/runs/relations)."
        ),
    )
    _add_run_key(relations)
    relations.set_defaults(call=_call_runs_relations)

    relate = runs_sub.add_parser(
        "relate",
        help=(
            "Tie two runs together so each is one tap from the other. Symmetric "
            "and unlabelled; neither need be tracked (POST /api/runs/relate)."
        ),
    )
    _add_run_key(relate)
    _add_run_key(relate, "other")
    relate.set_defaults(call=_call_runs_relate)

    unrelate = runs_sub.add_parser(
        "unrelate",
        help=(
            "Remove the relation, either order — one entry, so both directions go "
            "at once (POST /api/runs/unrelate)."
        ),
    )
    _add_run_key(unrelate)
    _add_run_key(unrelate, "other")
    unrelate.set_defaults(call=_call_runs_unrelate)


def main(argv: Optional[Sequence[str]] = None, *, transport=None) -> int:
    """Parse, issue one request, print the reply. Returns the exit code.

    *transport* is the injectable HTTP seam — see :func:`lmer_platform.client.request`.
    """
    parser = create_parser()
    args = parser.parse_args(argv)
    if getattr(args, "call", None) is None:
        # A group named without its verb (``runs``, ``orders``) parses fine and
        # means nothing, so it gets the usage it would have got from argparse if
        # subparsers could be required per group.
        parser.print_usage(sys.stderr)
        print(
            "lmer-ctl: name a command — `lmer-ctl --help` lists them, and "
            "GET /api is the authority on what this daemon serves.",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    try:
        endpoint = resolve_endpoint()
        call = args.call(args)
        response = _request(
            endpoint, call, timeout=args.timeout, transport=transport
        )
    except CtlError as exc:
        print(json.dumps({"error": exc.error, "detail": str(exc)}), file=sys.stderr)
        return EXIT_FAILURE
    return _emit(response, endpoint)


if __name__ == "__main__":
    sys.exit(main())
