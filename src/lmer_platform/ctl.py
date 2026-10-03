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
    send SESSION TEXT               POST /api/sessions/{id}/comms
    peers list                      GET  /api/assistant/peers
    peers orders PROJECT --purpose TEXT   GET /api/assistant/peers/{id}/instructions
    peers receipt REF               GET  /api/assistant/peers/receipts/{ref}
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
    rotate                          POST /api/assistant/handoff, then
                                    POST /api/assistant/rotate

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
import re
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from lmer_cli import routing_receipts, runref

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

#: ``rotate``'s help, shown in both places argparse shows help (issue #446).
_ROTATE_HELP = (
    "Rotate YOURSELF: write the handover, then replace this incarnation with a "
    "fresh context window (POST /api/assistant/handoff, then "
    "POST /api/assistant/rotate). THIS CALL DOES NOT RETURN — the daemon ends "
    "this container to start the successor, so a dropped connection is what "
    "success looks like from here and the receipt is the next incarnation's "
    "generation on `me`. Rotate at a breakpoint: no question of yours open, no "
    "digest taken and not yet acted on, no half-finished brief."
)

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
    parts = runref.split(key)
    if parts is None:
        raise CtlError(
            f"{key!r} is not a run key — a run is named host/project/slug, as "
            "the fleet view names it (gitlab.example.com/group/project/develop-141)"
        )
    host, project, slug = parts
    return {"host": host, "project": project, "slug": slug}


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
        "title", "description", "targets",
    )))
    if args.references:
        body["references"] = [_reference_of(spec) for spec in args.references]
    return Call("POST", "/api/sessions", body=body)


def _reference_of(spec: str) -> dict:
    """``<permalink>[|<title>]`` — the one flag spelling that carries both.

    A pipe because a permalink cannot contain one and a title can contain
    anything else; split at the first, so a title with a pipe in it survives.
    """
    permalink, _, title = spec.partition("|")
    reference = {"permalink": permalink.strip()}
    if title.strip():
        reference["title"] = title.strip()
    return reference


def _call_send(args) -> Call:
    """Send on the bus; the server assigns worker or peer UL provenance."""
    text = _text(args.text)
    if args.no_newline:
        return Call(
            "POST",
            f"/api/sessions/{args.session}/input",
            body={"data": text, "append_newline": False},
        )
    if re.fullmatch(r"/[A-Za-z][\w-]*", text):
        raise CtlError(
            f"{text!r} looks like a harness command. To run it: "
            f"lmer-ctl command {args.session} {text!r}. "
            "To discuss it as prose, use lmer-ctl send with a sentence around it."
        )
    return Call(
        "POST",
        f"/api/sessions/{args.session}/comms",
        body={"kind": "message", "text": text, **_given(args, ("ref", "generation"))},
    )


def _call_peers(args) -> Call:
    if args.peer_action == "list":
        return Call("GET", "/api/assistant/peers")
    if args.peer_action == "orders":
        return Call("GET", f"/api/assistant/peers/{args.project}/instructions",
                    params=_given(args, ("purpose", "offset", "limit")))
    return Call("GET", f"/api/assistant/peers/receipts/{args.ref}")


def _call_command(args) -> Call:
    return Call(
        "POST",
        f"/api/sessions/{args.session}/comms",
        body={"kind": "command", "text": _text(args.text)},
    )


def _call_comms(args) -> Call:
    return Call(
        "GET",
        f"/api/sessions/{args.session}/comms",
        params=_given(args, ("since", "timeout")),
    )


def _call_feed(args) -> Call:
    return Call("GET", "/api/comms/feed", params=_given(args, ("since", "timeout", "session")))


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
    if args.option is None and args.text is None:
        raise CtlError(
            "an answer needs the option's text or --option N, and this has neither"
        )
    if args.option is not None and args.text is not None:
        # The bus refuses the pair too. Caught here so the words are still in
        # front of the operator rather than in a 400 that does not quote them.
        raise CtlError(
            "an answer names its option by index or in its text, not both — "
            f"drop one of --option {args.option} and {args.text!r}"
        )
    body: dict = {}
    if args.text is not None:
        body["answer"] = _text(args.text)
    if args.option is not None:
        body["option"] = args.option
    return Call(
        "POST",
        f"/api/sessions/{args.session}/ask/{args.question}/answer",
        body=body,
    )


def _call_thread_read(args) -> Call:
    return Call(
        "GET",
        "/api/comms/thread",
        params={"permalink": _text(args.permalink), **_given(args, ("limit",))},
    )


def _call_thread_post(args) -> Call:
    return Call(
        "POST",
        "/api/comms/thread",
        body={"permalink": _text(args.permalink), "text": _text(args.text)},
    )


def _call_threads_list(args) -> Call:
    return Call("GET", f"/api/sessions/{args.session}/threads")


def _call_threads_connect(args) -> Call:
    return Call(
        "POST",
        f"/api/sessions/{args.session}/threads",
        body={"permalink": _text(args.permalink)},
    )


def _call_threads_open(args) -> Call:
    return Call(
        "POST",
        f"/api/sessions/{args.session}/threads",
        body={"channel": _text(args.channel), **_given(args, ("title",))},
    )


def _call_threads_references(args) -> Call:
    return Call("GET", f"/api/sessions/{args.session}/references")


def _call_threads_reference(args) -> Call:
    return Call(
        "POST",
        f"/api/sessions/{args.session}/references",
        body={"permalink": _text(args.permalink), **_given(args, ("title",))},
    )


def _call_threads_unreference(args) -> Call:
    return Call("DELETE", f"/api/sessions/{args.session}/references/{args.reference}")


def _call_threads_disconnect(args) -> Call:
    return Call("DELETE", f"/api/sessions/{args.session}/threads/{args.thread}")


def _call_threads_recover(args) -> Call:
    return Call("POST", f"/api/sessions/{args.session}/threads/{args.thread}/recover", body={
        "action": args.action, "operation": args.operation, "remote_ref": args.remote_ref,
        "acknowledge_duplicate": args.acknowledge_duplicate, "reason": args.reason,
    })


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
        body={**_run_fields(args.key), "answer": _text(args.text),
              **_given(args, ("slot", "home_revision", "confirm_project"))},
    )


def _call_runs_resume(args) -> Call:
    return Call(
        "POST", "/api/runs/resume",
        body={
            **_run_fields(args.key),
            **_given(args, ("taskdef", "repo_url", "direction", "session_id", "slot", "home_revision", "confirm_project")),
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


def _call_rotate(args) -> list:
    """The one verb that is two calls, and the order is the point (issue #446).

    The handover is written **first**, through its own route, rather than ridden
    into the rotate body it would also fit. What that buys is the refusal case: a
    rotation the daemon declines — a thread the successor's settings ask for is
    held, say — leaves the note stored anyway, so the operator's own rotate, or
    the supervisor's respawn after a crash, is as briefed as the planned rotation
    would have been. A note that travelled in the rotate body would have gone with
    the refusal.

    Omitting the note is allowed and means "carry the recorded one forward", which
    is the route's own default; it is what a rotation *after* a separate
    ``handoff set`` looks like.
    """
    calls = []
    note = _handoff_note(args)
    if note is not None:
        calls.append(
            Call("POST", "/api/assistant/handoff", body={"handoff": note})
        )
    calls.append(Call("POST", "/api/assistant/rotate", body={}))
    return calls


def _handoff_note(args) -> Optional[str]:
    """The note a rotation carries, from whichever source named it.

    Three sources for one value because a handover is the text most likely to
    contain backticks, ``$`` and quotes: the flag, ``-`` for stdin (this CLI's
    convention, :func:`_text`), and a file, which is what an agent that composed
    the note in a scratch file already has.
    """
    if args.handoff_file is not None:
        try:
            return Path(args.handoff_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise CtlError(f"cannot read the handover note ({exc})")
    if args.handoff is None:
        return None
    return _text(args.handoff)


# --- arguments ---------------------------------------------------------------

def _add_run_key(parser: argparse.ArgumentParser, name: str = "key") -> None:
    parser.add_argument(
        name,
        help="A run, as host/project/slug — the key the fleet view names it by.",
    )


def _add_text(
    parser: argparse.ArgumentParser, label: str, *, optional: bool = False
) -> None:
    parser.add_argument(
        "text",
        nargs="?" if optional else None,
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
    parser.add_argument("--workspace-id", default=os.environ.get("LMER_WORKSPACE_ID"),
                        help="Explicit named project identity; inherited by project assistants.")
    parser.add_argument("--request-id", help="Bind an action to its originating conversation request.")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    workspaces = sub.add_parser("projects", help="Named workspaces, separate from forge repositories.")
    workspace_sub = workspaces.add_subparsers(dest="project_command")
    workspace_sub.add_parser("list").set_defaults(call=lambda _args: Call("GET", "/api/projects"))
    create = workspace_sub.add_parser("create")
    create.add_argument("name")
    create.set_defaults(call=lambda args: Call("POST", "/api/projects", body={"name": args.name}))
    context = workspace_sub.add_parser("context", help="Explicit read-only fleet excerpt with named provenance.")
    context.add_argument("source_workspace_id")
    context.set_defaults(call=lambda args: Call("GET", "/api/project-context", params={"source_workspace_id": args.source_workspace_id}))
    relay = workspace_sub.add_parser("relay", help="Relay the supplied text, with source project provenance.")
    relay.add_argument("destination_id")
    relay.add_argument("text")
    relay.set_defaults(call=lambda args: Call("POST", "/api/project-relay", body={"destination_id": args.destination_id, "text": _text(args.text)}))
    for action in ("archive", "restore"):
        command = workspace_sub.add_parser(action)
        command.add_argument("identity")
        command.set_defaults(call=lambda args, action=action: Call("POST", f"/api/projects/{args.identity}/{action}"))
    move = workspace_sub.add_parser("move-run")
    _add_run_key(move)
    move.add_argument("destination_id")
    move.add_argument("--home-revision", type=int, required=True)
    move.set_defaults(call=lambda args: Call("POST", "/api/runs/move", body={**_run_fields(args.key), "destination_id": args.destination_id, "home_revision": args.home_revision}))

    requests = sub.add_parser("requests", help="Source-bound conversation requests.")
    requests_sub = requests.add_subparsers(dest="requests_command")
    requests_list = requests_sub.add_parser("list")
    requests_list.set_defaults(call=lambda _args: Call("GET", "/api/assistant/requests"))
    requests_reply = requests_sub.add_parser("reply")
    requests_reply.add_argument("identity")
    requests_reply.add_argument("text")
    requests_reply.set_defaults(call=lambda args: Call("POST", f"/api/assistant/requests/{args.identity}/reply",
                                                      body={"text": _text(args.text)}))
    requests_park = requests_sub.add_parser("park", help="Operator-only: release independent work, retaining uncertain delivery.")
    requests_park.add_argument("identity")
    requests_park.add_argument("reason")
    requests_park.set_defaults(call=lambda args: Call("POST", f"/api/assistant/requests/{args.identity}/park",
                                                     body={"reason": _text(args.reason)}))
    requests_release = requests_sub.add_parser("release", help="Operator-only: release a blocked request with no uncertain effects.")
    requests_release.add_argument("identity")
    requests_release.add_argument("reason")
    requests_release.set_defaults(call=lambda args: Call("POST", f"/api/assistant/requests/{args.identity}/release",
                                                        body={"reason": _text(args.reason)}))

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
    spawn.add_argument(
        "--target-thread", dest="targets", action="append",
        help="Slack or matrix thread permalink to connect (repeatable).",
    )
    spawn.add_argument(
        "--reference-thread", dest="references", action="append", metavar="PERMALINK[|TITLE]",
        help=(
            "A thread the run should know about without being connected to it "
            "(repeatable). It is told once and reads or posts it with "
            "`lmer-signal read` / `post --thread`."
        ),
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
            "Send a message to a live session on its bus; the agent sees it "
            "tagged [uber lmer] for workers, or [peer UL message] for an explicitly "
            "addressed peer UL. Peer sends require --ref, remain native and never mirror to external threads "
            "(POST /api/sessions/{id}/comms)."
        ),
    )
    send.add_argument("session", help="Session id.")
    send.add_argument("--ref", help="Stable peer-send reference; reuse only for an identical retry. Required across projects.")
    send.add_argument("--generation", type=int, help="Expected peer UL generation; stale addresses are refused, never retargeted.")
    _add_text(send, "What to say.")
    send.add_argument(
        "--no-newline",
        action="store_true",
        help=(
            "Type without submitting, as raw keystrokes (POST /api/sessions/{id}/input) "
            "— not a message, nothing else sees it."
        ),
    )
    send.set_defaults(call=_call_send)

    peers = sub.add_parser("peers", help="Project UL metadata, explicit standing-order reads and native send receipts.")
    peer_verbs = peers.add_subparsers(dest="peer_action")
    peer_list = peer_verbs.add_parser("list", help="Discover peer names, current UL sessions/generations and connected thread links.")
    peer_list.set_defaults(call=_call_peers)
    peer_orders = peer_verbs.add_parser("orders", help="Read bounded peer standing orders for a human-directed task; adapt relevant material only.")
    peer_orders.add_argument("project", help="Peer project ID from peers list.")
    peer_orders.add_argument("--purpose", required=True, help="Human-requested task requiring this explicit read.")
    peer_orders.add_argument("--offset", type=int)
    peer_orders.add_argument("--limit", type=int, help="Characters to read, 1-4000 (default 2000).")
    peer_orders.set_defaults(call=_call_peers)
    peer_receipt = peer_verbs.add_parser("receipt", help="Inspect this project's original peer-send result without resending.")
    peer_receipt.add_argument("ref")
    peer_receipt.set_defaults(call=_call_peers)

    command = sub.add_parser(
        "command",
        help=(
            "Run a harness slash command in a live session, e.g. /followup "
            "(POST /api/sessions/{id}/comms with kind=command)."
        ),
    )
    command.add_argument("session", help="Session id.")
    _add_text(command, "The command, starting with /.")
    command.set_defaults(call=_call_command)

    comms = sub.add_parser(
        "comms",
        help="One session's conversation on its bus (GET /api/sessions/{id}/comms).",
    )
    comms.add_argument("session", help="Session id.")
    comms.add_argument("--since", type=int, help="Only records after this sequence number.")
    comms.add_argument("--timeout", type=float, help="Long-poll this many seconds for the next record (max 30).")
    comms.set_defaults(call=_call_comms)

    feed = sub.add_parser(
        "feed",
        help="The fleet-wide comms feed (GET /api/comms/feed).",
    )
    feed.add_argument("--since", type=int, help="Only events after this cursor.")
    feed.add_argument("--timeout", type=float, help="Long-poll this many seconds (max 30).")
    feed.add_argument("--session", help="Only this session's records.")
    feed.set_defaults(call=_call_feed)

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
    _add_text(answer, "The answer, unless --option names it.", optional=True)
    answer.add_argument(
        "--option", type=int, metavar="N",
        help=(
            "Answer by picking one of the question's options: N is 1-based into "
            "the `options` array `questions` prints, in the order it prints "
            "them. The session is handed the option's text. Not combinable with "
            "the text argument. A number typed as the answer text is never read "
            "as an option."
        ),
    )
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

    thread = sub.add_parser(
        "thread",
        help=(
            "Read or post one Slack or matrix thread by permalink, connecting "
            "nothing to it (/api/comms/thread)."
        ),
    )
    thread_sub = thread.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")
    thread_read = thread_sub.add_parser(
        "read", help="Print a thread's most recent messages.",
    )
    thread_read.add_argument("permalink", help="Thread permalink.")
    thread_read.add_argument(
        "--limit", type=int, help="How many of the most recent messages (default 20).",
    )
    thread_read.set_defaults(call=_call_thread_read)
    thread_post = thread_sub.add_parser(
        "post",
        help=(
            "Post once into a thread. The post is attributed to this credential "
            "— `uber lmer` or `operator` — not to anything in the text."
        ),
    )
    thread_post.add_argument("permalink", help="Thread permalink.")
    thread_post.add_argument("text", help="What to post; `-` reads it from stdin.")
    thread_post.set_defaults(call=_call_thread_post)

    threads = sub.add_parser(
        "threads",
        help=(
            "The chat threads a running session is mirrored into: open one, "
            "connect one, disconnect one, or list them "
            "(/api/sessions/{id}/threads)."
        ),
    )
    threads_sub = threads.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")
    threads_list = threads_sub.add_parser(
        "list", help="What this session is connected to, and whether each connector is running.",
    )
    threads_list.add_argument("session", help="Session id.")
    threads_list.set_defaults(call=_call_threads_list)
    threads_connect = threads_sub.add_parser(
        "connect",
        help=(
            "Connect this session to a Slack or matrix thread. The transport "
            "comes from the link; the daemon runs the connector."
        ),
    )
    threads_connect.add_argument("session", help="Session id.")
    threads_connect.add_argument("permalink", help="Thread permalink.")
    threads_connect.set_defaults(call=_call_threads_connect)
    threads_open = threads_sub.add_parser(
        "open",
        help=(
            "Open a new thread in a channel and connect this session to it. The "
            "bot must already be in the channel."
        ),
    )
    threads_open.add_argument("session", help="Session id.")
    threads_open.add_argument(
        "channel",
        help="Slack channel id or #name, or matrix room id or alias.",
    )
    threads_open.add_argument(
        "--title",
        help="First line of the root message, in place of the run's own title.",
    )
    threads_open.set_defaults(call=_call_threads_open)
    threads_disconnect = threads_sub.add_parser(
        "disconnect", help="Stop mirroring one thread. The thread is told.",
    )
    threads_disconnect.add_argument("session", help="Session id.")
    # The id and not the permalink: a permalink is a URL, and a URL cannot be one
    # path segment — %2F is normalised back to a separator before the route is
    # matched, so it would 404. `threads list` is where the id comes from.
    threads_disconnect.add_argument("thread", help="Thread id, as `threads list` reports it.")
    threads_disconnect.set_defaults(call=_call_threads_disconnect)
    threads_recover = threads_sub.add_parser(
        "recover", help="Inspect or recover a pending send in the host connector state.",
    )
    threads_recover.add_argument("session", help="Session id.")
    threads_recover.add_argument("thread", help="Thread id from threads list.")
    threads_recover.add_argument("action", choices=("inspect", "receipt", "retry", "clear"))
    threads_recover.add_argument("--operation", help="Exact pending operation id from inspect.")
    threads_recover.add_argument("--remote-ref", help="Receipt to verify against the remote thread.")
    threads_recover.add_argument("--acknowledge-duplicate", action="store_true",
                                 help="Required for clear: the selected send may be duplicated.")
    threads_recover.add_argument("--reason", help="Required for clear: why the marker is being removed.")
    threads_recover.set_defaults(call=_call_threads_recover)
    threads_references = threads_sub.add_parser(
        "references",
        help="The threads this run has been pointed at, connected to none of them.",
    )
    threads_references.add_argument("session", help="Session id.")
    threads_references.set_defaults(call=_call_threads_references)
    threads_reference = threads_sub.add_parser(
        "reference",
        help=(
            "Point this run at a thread: it is told once, and reads or posts it "
            "with `lmer-signal read` / `post --thread`. Nothing is connected."
        ),
    )
    threads_reference.add_argument("session", help="Session id.")
    threads_reference.add_argument("permalink", help="Thread permalink.")
    threads_reference.add_argument("--title", help="What this thread is, in one line.")
    threads_reference.set_defaults(call=_call_threads_reference)
    threads_unreference = threads_sub.add_parser(
        "unreference", help="Drop one reference. The run is not told.",
    )
    threads_unreference.add_argument("session", help="Session id.")
    threads_unreference.add_argument(
        "reference", help="Reference id, as `threads references` reports it.",
    )
    threads_unreference.set_defaults(call=_call_threads_unreference)

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

    # The only verb carrying a `description` as well as a `help`: argparse shows
    # `help` in the parent's listing and `description` in `rotate --help`, and the
    # fact that the call does not return has to reach whichever one an agent read.
    rotate = sub.add_parser(
        "rotate",
        description=_ROTATE_HELP,
        help=_ROTATE_HELP,
    )
    # Exclusive rather than one silently winning: the two name the same field,
    # and a note that was quietly ignored is discovered by the successor.
    note = rotate.add_mutually_exclusive_group()
    note.add_argument(
        "--handoff",
        help=(
            "The note your successor is told. '-' reads it from stdin, which is "
            "what to use for anything multi-line or containing backticks, $ or "
            "quotes. Omit it to carry forward the note already recorded."
        ),
    )
    note.add_argument(
        "--handoff-file",
        help="Read the note from this file instead of the command line.",
    )
    rotate.set_defaults(call=_call_rotate)

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
    answer.add_argument("--slot")
    answer.add_argument("--home-revision", type=int)
    answer.add_argument("--confirm-project", action="store_true", default=None)
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
    resume.add_argument(
        "--session", dest="session_id",
        help="One launch of the run's group: a session dir's <session-id> or its "
             "full <session-id>--<name>. Defaults to the newest launch; an id "
             "that names none of them is refused with the list rather than "
             "resolved to a neighbouring record.",
    )
    resume.add_argument("--slot")
    resume.add_argument("--home-revision", type=int)
    resume.add_argument("--confirm-project", action="store_true", default=None)
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
    """Parse, issue the verb's requests, print the last reply. Returns the exit code.

    One request for every verb but ``rotate``, which is two and says why
    (:func:`_call_rotate`). A sequence stops at the first refusal, so the reply
    printed is either the last call's or the one that declined.

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
        calls = args.call(args)
        if isinstance(calls, Call):
            calls = [calls]
        for index, call in enumerate(calls):
            headers = {}
            if args.workspace_id:
                headers["X-Lmer-Workspace-ID"] = args.workspace_id
            if args.request_id:
                headers["X-Lmer-Request-ID"] = args.request_id
            response = _request(
                endpoint, call, timeout=args.timeout, transport=transport,
                **({"headers": headers} if headers else {}),
            )
            if call.method == "POST" and call.path == "/api/comms/thread" and 200 <= response.status_code < 300:
                payload = _payload(response.text)
                posted = payload.get("posted") if isinstance(payload, dict) else None
                if isinstance(posted, dict):
                    routing_receipts.record("post", posted.get("ref"))
            # A verb made of several calls stops at the first refusal and reports
            # it, rather than pressing on with a step whose precondition the
            # daemon just declined. The last call's reply is the verb's reply.
            if index < len(calls) - 1 and not 200 <= response.status_code < 300:
                break
    except CtlError as exc:
        print(json.dumps({"error": exc.error, "detail": str(exc)}), file=sys.stderr)
        return EXIT_FAILURE
    return _emit(response, endpoint)


if __name__ == "__main__":
    sys.exit(main())
