"""``lmer-ctl`` — the assistant's client for the platform API (T102, spec §8.2).

The CLI was specified, dropped as "just a wrapper over the REST API", and asked
for again to take the composing of those requests off the assistant. That history
is what these tests are shaped by: a wrapper's only failure mode is *disagreeing
with the thing it wraps*, so almost everything here is a claim about equivalence
rather than behaviour.

What is pinned, and why each one is a way this could rot:

- **Every verb is one documented route.** The table in
  :data:`ROUTE_TABLE` is the command surface, and it is re-derived against the
  app's own routes — a typo'd path would otherwise be a 404 the assistant reports
  to the operator as a broken fleet, which is the exact failure the ``orchestrate``
  taskdef warns about.
- **The two answer verbs stay two.** ``answer`` writes into a live session's
  channel and ``runs answer`` starts a container. The API index calls this the one
  mistake that starts a container nobody asked for; a CLI that merged them would
  make it a typo away.
- **No logic of its own.** An omitted flag is absent from the body rather than
  ``null`` (several routes distinguish those), a refusal is passed through with the
  daemon's status rather than interpreted, and the run-key split is the only
  parsing there is.
- **The credential is a header and nothing else.** No flag accepts it, it is never
  formatted into a URL or a query string, and a reply that echoed it is scrubbed.
  This process runs inside the session whose terminal is written to disk and
  served to a browser.
- **Configuration is the pair the host already writes.** Same two variables the
  ``orchestrate`` taskdef names; absent, the refusal names them and never prints
  the value of either.

The end-to-end tests drive the real FastAPI app through
:func:`lmer_platform.ctl.main`'s transport seam, so the daemon's own statuses —
the 429 at the cap, the 410 for a channel whose session is gone, the 404 for a
session that never existed — are the ones the CLI is seen to pass through.
"""

import argparse
import json
import os
import re
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from ask_channel import protocol
from lmer_platform import api, ask, assistant, ctl, registry, store
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env

SECRET = "test-secret-value"

#: The base URL a ``TestClient`` answers on. Set in the environment like the real
#: one, so every end-to-end test goes through :func:`ctl.resolve_endpoint`.
BASE_URL = "http://testserver"

RUN_KEY = "gitlab.example.com/agents/global/develop-141"
RUN_FIELDS = {
    "host": "gitlab.example.com",
    "project": "agents/global",
    "slug": "develop-141",
}
OTHER_KEY = "gitlab.example.com/agents/global/review-mr-163"
OTHER_FIELDS = {
    "host": "gitlab.example.com",
    "project": "agents/global",
    "slug": "review-mr-163",
}


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def config(platform_root):
    return cfg.load()


@pytest.fixture
def fake_state():
    """A canned fleet view, so a routing test needs no work repo."""

    def builder(config, *, force_pull=False):
        return {
            "schema": 1,
            "generated_at": "2026-07-29T12:00:00Z",
            "runs": [],
            "attention": [],
            "totals": {"runs": 0, "live": 0, "attention": 0},
        }

    return builder


@pytest.fixture
def client(config, fake_state):
    return Local(TestClient(api.create_app(config, SECRET, state_builder=fake_state)))


@pytest.fixture
def configured(monkeypatch):
    """The environment the host writes into the assistant's container."""
    monkeypatch.setenv(ctl.ENV_PLATFORM_URL, BASE_URL)
    monkeypatch.setenv(ctl.ENV_PLATFORM_CREDENTIAL, SECRET)


@dataclass
class Reply:
    """The two attributes :func:`ctl._emit` reads off a response."""

    status_code: int = 200
    text: str = "{}"


class Local:
    """The real app as a transport for :func:`ctl.main`.

    Wrapped rather than handed over directly for one reason: ``TestClient`` warns
    about a *timeout* argument, since an in-process ASGI call has no socket to
    time one out on. That the flag is forwarded at all is :class:`Recorder`'s
    claim to make.
    """

    def __init__(self, client):
        self.client = client
        self.app = client.app

    def request(self, method, url, *, timeout=None, **kwargs):
        return self.client.request(method, url, **kwargs)


class Recorder:
    """A transport that records the request and answers a canned reply.

    For the claims about *what was sent* — a real reply cannot show that the
    credential was in a header rather than the query string.
    """

    def __init__(self, reply=None):
        self.reply = reply or Reply()
        self.calls = []

    def request(self, method, url, *, params=None, json=None, headers=None,
                timeout=None):
        self.calls.append({
            "method": method, "url": url, "params": params, "json": json,
            "headers": headers, "timeout": timeout,
        })
        return self.reply


def run(argv, transport, capsys):
    """``(exit_code, stdout, stderr)`` for one invocation."""
    code = ctl.main(list(argv), transport=transport)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def ok(argv, transport, capsys):
    """Run *argv*, require success, return the JSON it printed on stdout."""
    code, out, err = run(argv, transport, capsys)
    assert code == 0, err
    assert err == "", "a successful call printed to stderr"
    return json.loads(out)


def refused(argv, transport, capsys):
    """Run *argv*, require a nonzero exit, return the JSON it printed on stderr."""
    code, out, err = run(argv, transport, capsys)
    assert code == ctl.EXIT_FAILURE
    assert out == "", "a failure printed to stdout, where a caller reads results"
    return json.loads(err)


def build(*argv):
    """The :class:`ctl.Call` an argv produces, with no HTTP anywhere near it."""
    args = ctl.create_parser().parse_args(list(argv))
    return args.call(args)


def registered_session(session_id="s-20260729-aaaa", *, pid=None):
    """A session in the registry, with no control plane behind it.

    Registered here rather than borrowed from ``tests/test_platform_ask.py``: the
    channel tests need a *reader* on the other side of the mount and wire up a fake
    control plane for it, and nothing in this file does — what is being tested is
    which route a verb calls, so the daemon's answer only has to be the daemon's.
    A dead ``pid`` is how the 410 side is reached.
    """
    registry.register(
        session_id,
        kind="worker",
        pid=os.getpid() if pid is None else pid,
        run={"host": "gitlab.example.com", "project": "agents/global",
             "slug": "develop-141"},
        task={"taskdef": "develop", "target": "issue-141"},
        log_path=str(store.logs_dir() / f"{session_id}.log"),
        started_at="2026-07-29T09:00:00Z",
    )
    log = store.logs_dir() / f"{session_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"")
    return session_id


# --- configuration: the pair the host writes ---------------------------------

def test_the_variables_are_the_ones_the_host_actually_writes():
    """Spelled in ``ctl`` rather than imported, to keep the module's imports off
    the spawn stack — so the copy is pinned here instead."""
    assert ctl.ENV_PLATFORM_URL == assistant.ENV_PLATFORM_URL
    assert ctl.ENV_PLATFORM_CREDENTIAL == assistant.ENV_PLATFORM_CREDENTIAL
    assert ctl.ENV_PLATFORM_UNREACHABLE == assistant.ENV_PLATFORM_UNREACHABLE


@pytest.mark.parametrize("present", [(), (ctl.ENV_PLATFORM_URL,),
                                     (ctl.ENV_PLATFORM_CREDENTIAL,)])
def test_a_half_configured_session_is_refused_by_name(
    monkeypatch, capsys, present
):
    """All-or-nothing, because that is how the host writes them: a URL with no
    credential is a 401 machine. The refusal has to name both, since the agent
    reading it has to tell the operator which variable to set."""
    for name in present:
        monkeypatch.setenv(name, "set-to-something")

    payload = refused(["status"], Recorder(), capsys)

    assert payload["error"] == "configuration"
    assert ctl.ENV_PLATFORM_URL in payload["detail"]
    assert ctl.ENV_PLATFORM_CREDENTIAL in payload["detail"]


def test_the_refusal_relays_the_reason_the_host_recorded(monkeypatch, capsys):
    """``LMER_PLATFORM_UNREACHABLE`` is the one sentence the assistant can give
    the operator instead of "it does not work"."""
    monkeypatch.setenv(
        ctl.ENV_PLATFORM_UNREACHABLE,
        "bound to 127.0.0.1 and the runtime is docker",
    )
    payload = refused(["status"], Recorder(), capsys)
    assert "bound to 127.0.0.1 and the runtime is docker" in payload["detail"]


def test_the_refusal_never_echoes_the_credential(monkeypatch, capsys):
    """The half-configured case is the tempting place to print what *was* found."""
    monkeypatch.setenv(ctl.ENV_PLATFORM_CREDENTIAL, SECRET)
    code, out, err = run(["status"], Recorder(), capsys)
    assert code == ctl.EXIT_FAILURE
    assert SECRET not in err + out


def test_a_trailing_slash_on_the_url_does_not_double_up(monkeypatch, capsys):
    monkeypatch.setenv(ctl.ENV_PLATFORM_URL, BASE_URL + "/")
    monkeypatch.setenv(ctl.ENV_PLATFORM_CREDENTIAL, SECRET)
    recorder = Recorder()
    ok(["status"], recorder, capsys)
    assert recorder.calls[0]["url"] == f"{BASE_URL}/api/state"


def test_the_timeout_reaches_the_transport(configured, capsys):
    """Sized for a spawn, which answers only once the container is up — so the
    default has to be the one that travels, not one a client library supplies."""
    default = Recorder()
    ok(["status"], default, capsys)
    assert default.calls[0]["timeout"] == ctl.DEFAULT_TIMEOUT_SECONDS

    override = Recorder()
    ok(["--timeout", "5", "status"], override, capsys)
    assert override.calls[0]["timeout"] == 5.0


def test_a_transport_failure_is_reported_as_unreachable(configured, capsys):
    """Distinct from a configuration failure: this one is news about the fleet."""
    import requests

    class Dead:
        def request(self, *args, **kwargs):
            raise requests.ConnectionError("connection refused")

    payload = refused(["status"], Dead(), capsys)
    assert payload["error"] == "unreachable"
    assert BASE_URL in payload["detail"]


# --- the credential ----------------------------------------------------------

def option_strings(parser):
    """Every flag this CLI accepts, walking into the subcommand groups."""
    for action in parser._actions:
        yield from action.option_strings
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                yield from option_strings(child)


def test_no_flag_anywhere_accepts_the_credential():
    """argv is world-readable and a harness echoes a command into the transcript
    this session writes to disk, so there must be no way to pass it as an
    argument — not even one nobody uses."""
    flags = list(option_strings(ctl.create_parser()))
    assert flags, "the walk found nothing, so it is not checking anything"
    for flag in flags:
        assert not re.search(r"secret|token|credential|password", flag), flag


def test_the_credential_is_rejected_as_an_argument(configured, capsys):
    with pytest.raises(SystemExit) as exc:
        ctl.main(["--secret", SECRET, "status"], transport=Recorder())
    assert exc.value.code == 2


def test_the_credential_travels_only_in_the_header(configured, capsys):
    """A URL is echoed into ``requests``' own exception messages, and a query
    string into any proxy log between here and the daemon."""
    recorder = Recorder()
    ok(["runs", "meta", "get", RUN_KEY], recorder, capsys)
    call = recorder.calls[0]

    assert call["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert SECRET not in call["url"]
    assert SECRET not in json.dumps(call["params"])
    assert SECRET not in json.dumps(call["json"])


def test_a_reply_that_echoed_the_credential_is_scrubbed(configured, capsys):
    """No route does today. One that did would otherwise disclose it here."""
    recorder = Recorder(Reply(500, json.dumps({"detail": f"boom {SECRET}"})))
    payload = refused(["status"], recorder, capsys)
    assert SECRET not in json.dumps(payload)
    assert "<redacted>" in payload["body"]["detail"]


def test_a_reply_that_is_not_json_still_leaves_json_on_the_stream(
    configured, capsys
):
    """Something between here and the daemon can answer HTML — a proxy, a tunnel.
    The consumer parses whatever it is handed, so the wrapper is the contract."""
    recorder = Recorder(Reply(502, "<html>bad gateway</html>"))
    payload = refused(["status"], recorder, capsys)
    assert payload["status"] == 502
    assert "bad gateway" in payload["body"]["detail"]


def test_a_wrong_credential_comes_back_as_the_daemons_401(
    monkeypatch, client, capsys
):
    """Which is also what proves the header is what authenticates the request."""
    monkeypatch.setenv(ctl.ENV_PLATFORM_URL, BASE_URL)
    monkeypatch.setenv(ctl.ENV_PLATFORM_CREDENTIAL, "not-the-secret")
    payload = refused(["status"], client, capsys)
    assert payload == {"error": "http", "status": 401,
                       "body": {"detail": payload["body"]["detail"]}}


# --- the command surface: one verb, one route --------------------------------

#: ``(argv, method, path, params, body)`` for every verb this CLI has. The whole
#: command surface is here on purpose: this is the table the taskdef teaches and
#: the one a reviewer reads to check that nothing composite crept in.
ROUTE_TABLE = [
    (["status"], "GET", "/api/state", None, None),
    (["health"], "GET", "/api/health", None, None),
    (["spawn-options"], "GET", "/api/spawn-options", {}, None),
    (["spawn-options", "--target", "issue-141", "--repo-url", "git@x:y.git"],
     "GET", "/api/spawn-options",
     {"target": "issue-141", "repo_url": "git@x:y.git"}, None),
    (["spawn", "develop", "issue-141", "--title", "auth rate-limit fix"],
     "POST", "/api/sessions", None,
     {"taskdef": "develop", "target": "issue-141",
      "title": "auth rate-limit fix"}),
    (["spawn", "review", "mr-163", "--preset", "sol", "--agents", "2",
      "--model", "opus", "--harness", "codex", "--ports", "2",
      "--repo-url", "git@x:y.git", "--description", "the follow-up review"],
     "POST", "/api/sessions", None,
     {"taskdef": "review", "target": "mr-163", "preset": "sol", "agents": "2",
      "model": "opus", "harness": "codex", "ports": 2,
      "repo_url": "git@x:y.git", "description": "the follow-up review"}),
    (["send", "s-1", "/followup rebase please"], "POST",
     "/api/sessions/s-1/input", None,
     {"data": "/followup rebase please", "append_newline": True}),
    (["send", "s-1", "half a thought", "--no-newline"], "POST",
     "/api/sessions/s-1/input", None,
     {"data": "half a thought", "append_newline": False}),
    (["log", "s-1"], "GET", "/api/sessions/s-1/log", {}, None),
    (["log", "s-1", "--offset", "-4096", "--limit", "4096",
      "--source", "host"],
     "GET", "/api/sessions/s-1/log",
     {"offset": -4096, "limit": 4096, "source": "host"}, None),
    (["messages", "s-1", "--since", "-20"], "GET",
     "/api/sessions/s-1/messages", {"since": -20}, None),
    (["questions", "s-1"], "GET", "/api/sessions/s-1/ask", None, None),
    (["answer", "s-1", "q-7", "prep-release"], "POST",
     "/api/sessions/s-1/ask/q-7/answer", None, {"answer": "prep-release"}),
    (["wind-down", "s-1"], "POST", "/api/sessions/s-1/wind-down", None, {}),
    (["wind-down", "s-1", "--note", "skip the MR"], "POST",
     "/api/sessions/s-1/wind-down", None, {"note": "skip the MR"}),
    (["exit", "s-1"], "POST", "/api/sessions/s-1/exit", None, {}),
    (["me"], "GET", "/api/assistant", None, None),
    (["orders", "get"], "GET", "/api/assistant/instructions", None, None),
    (["orders", "set", "spawn reviewers with sol"], "POST",
     "/api/assistant/instructions", None,
     {"instructions": "spawn reviewers with sol"}),
    (["handoff", "get"], "GET", "/api/assistant/handoff", None, None),
    (["handoff", "set", "two runs in flight"], "POST",
     "/api/assistant/handoff", None, {"handoff": "two runs in flight"}),
    (["pending", "take"], "POST", "/api/assistant/pending", None, {}),
    (["runs", "candidates"], "GET", "/api/runs/candidates", None, None),
    (["runs", "adopt", RUN_KEY], "POST", "/api/runs/adopt", None, RUN_FIELDS),
    (["runs", "adopt", RUN_KEY, "--note", "the operator's own run"], "POST",
     "/api/runs/adopt", None, {**RUN_FIELDS, "note": "the operator's own run"}),
    (["runs", "forget", RUN_KEY], "POST", "/api/runs/forget", None, RUN_FIELDS),
    (["runs", "answer", RUN_KEY, "yes, rebase"], "POST", "/api/runs/answer",
     None, {**RUN_FIELDS, "answer": "yes, rebase"}),
    (["runs", "resume", RUN_KEY, "--taskdef", "review", "--direction", "go on"],
     "POST", "/api/runs/resume", None,
     {**RUN_FIELDS, "taskdef": "review", "direction": "go on"}),
    (["runs", "meta", "get", RUN_KEY], "GET", "/api/runs/meta",
     RUN_FIELDS, None),
    (["runs", "meta", "set", RUN_KEY, "--title", "auth fix"], "POST",
     "/api/runs/meta", None, {**RUN_FIELDS, "title": "auth fix"}),
    (["runs", "relations", RUN_KEY], "GET", "/api/runs/relations",
     RUN_FIELDS, None),
    (["runs", "relate", RUN_KEY, OTHER_KEY], "POST", "/api/runs/relate", None,
     {**RUN_FIELDS, "related": OTHER_FIELDS}),
    (["runs", "unrelate", RUN_KEY, OTHER_KEY], "POST", "/api/runs/unrelate",
     None, {**RUN_FIELDS, "related": OTHER_FIELDS}),
]


@pytest.mark.parametrize("argv, method, path, params, body", ROUTE_TABLE)
def test_a_verb_is_one_route_and_the_arguments_it_was_given(
    argv, method, path, params, body
):
    call = build(*argv)
    assert (call.method, call.path) == (method, path)
    assert call.params == params
    assert call.body == body


def test_every_route_this_cli_names_is_served_by_the_app(client):
    """A wrapper's worst failure: a path that 404s, which the assistant reports
    to the operator as a broken fleet rather than as a stale client."""
    served = [
        route.path for route in client.app.routes if getattr(route, "path", None)
    ]
    patterns = [
        (path, re.compile(
            "^" + "[^/]+".join(
                re.escape(part) for part in re.split(r"\{[^}]+\}", path)
            ) + "$"
        ))
        for path in served
    ]
    for argv, _method, _path, _params, _body in ROUTE_TABLE:
        # The path the CLI builds, not the one the table declares: the table is
        # pinned against the CLI above, and this has to be pinned against the app.
        built = build(*argv).path
        assert any(pattern.match(built) for _served, pattern in patterns), (
            f"{' '.join(argv)} names {built}, which this build does not serve"
        )


def test_the_two_answer_verbs_are_two_routes():
    """The API index calls confusing these the one mistake that starts a
    container nobody asked for, so the CLI must not put them one flag apart."""
    live = build("answer", "s-1", "q-7", "yes")
    stopped = build("runs", "answer", RUN_KEY, "yes")
    assert live.path == "/api/sessions/s-1/ask/q-7/answer"
    assert stopped.path == "/api/runs/answer"
    assert live.path != stopped.path


def test_an_omitted_flag_is_absent_from_the_body_not_null():
    """``POST /api/runs/meta`` leaves an absent field alone and clears it on
    ``""``, so a ``null`` sent for a flag nobody passed would be a different
    request."""
    body = build("runs", "meta", "set", RUN_KEY, "--title", "auth fix").body
    assert "description" not in body
    assert body["title"] == "auth fix"


def test_a_run_key_keeps_a_project_that_contains_a_slash():
    """A project is ``group/subgroup``: the middle is everything between the
    first segment and the last, which is what makes the key the fleet view's own
    string rather than a shape only this CLI accepts."""
    body = build("runs", "forget", "git.example.com/a/b/c/the-slug").body
    assert body == {"host": "git.example.com", "project": "a/b/c",
                    "slug": "the-slug"}


def test_a_key_that_cannot_name_a_run_is_refused_before_any_request(
    configured, capsys
):
    """Argument shape, not validation: three parts is what it takes to build the
    body at all. Whether the run exists is the daemon's answer."""
    recorder = Recorder()
    payload = refused(["runs", "forget", "just-a-slug"], recorder, capsys)
    assert payload["error"] == "configuration"
    assert "host/project/slug" in payload["detail"]
    assert recorder.calls == [], "a malformed key still reached the network"


def test_a_group_named_without_its_verb_prints_usage(configured, capsys):
    code, out, err = run(["runs"], Recorder(), capsys)
    assert code == ctl.EXIT_FAILURE
    assert out == ""
    assert "usage" in err.lower()


# --- end to end against the real app ----------------------------------------

def test_the_fleet_view_is_printed_as_the_daemon_sent_it(
    configured, client, capsys
):
    payload = ok(["status"], client, capsys)
    assert payload["totals"] == {"runs": 0, "live": 0, "attention": 0}


def test_spawn_sends_the_flags_it_was_given(
    configured, client, platform_root, monkeypatch, capsys
):
    from lmer_platform import spawn as spawn_mod

    captured = {}

    def fake_spawn(config, request, kind="worker"):
        captured["request"] = request
        return spawn_mod.SpawnResult(
            session_id="s-1", pid=4242,
            log_path=platform_root / "logs" / "s-1.log",
            host="gitlab.example.com", project="agents/global",
            slug="develop-141", command=["lmer", "develop", "issue-141"],
        )

    monkeypatch.setattr(api, "spawn_session", fake_spawn)
    payload = ok(
        ["spawn", "develop", "issue-141", "--title", "auth rate-limit fix",
         "--preset", "sol"],
        client, capsys,
    )

    assert payload["session_id"] == "s-1"
    assert captured["request"].title == "auth rate-limit fix"
    assert captured["request"].preset == "sol"
    assert captured["request"].description is None


def test_the_concurrency_cap_is_passed_through_with_its_numbers(
    configured, client, platform_root, monkeypatch, capsys
):
    """A 429 is an answer the assistant relays, so the numbers have to survive
    the trip — and nothing here may retry it."""
    from lmer_platform import spawn as spawn_mod

    def at_cap(config, request, kind="worker"):
        raise spawn_mod.CapacityError("concurrency cap reached: 4/4 sessions")

    monkeypatch.setattr(api, "spawn_session", at_cap)
    payload = refused(["spawn", "develop", "issue-141"], client, capsys)

    assert payload["error"] == "http"
    assert payload["status"] == 429
    assert "4/4" in payload["body"]["detail"]


def test_a_sessions_open_questions_are_read_over_its_own_route(
    configured, client, platform_root, capsys
):
    """``questions`` is how the id that ``answer`` needs is found, so the pair is
    only usable if this one comes back with it."""
    session_id = registered_session()
    question = protocol.post_question(
        ask.prepare_ask_dir(session_id), "which branch?"
    )

    payload = ok(["questions", session_id], client, capsys)

    assert [entry["id"] for entry in payload["entries"]] == [question.id]


def test_a_channel_whose_session_is_gone_passes_the_410_through(
    configured, client, platform_root, capsys
):
    """The daemon's own permanent-failure status, unchanged: the refusal points
    at resuming the run, and inventing a code here would hide that."""
    session_id = registered_session(pid=999999999)
    question = protocol.post_question(
        ask.prepare_ask_dir(session_id), "which branch?"
    )

    payload = refused(
        ["answer", session_id, question.id, "prep-release"], client, capsys
    )

    assert payload["status"] == 410
    assert "resume" in payload["body"]["detail"].lower()


def test_typing_into_a_session_that_never_existed_passes_the_404_through(
    configured, client, platform_root, capsys
):
    payload = refused(["send", "s-nope", "/followup"], client, capsys)
    assert payload["status"] == 404


def test_a_run_key_round_trips_through_adopt_and_meta(
    configured, client, platform_root, capsys
):
    """One key in, three fields out, twice — once in a body and once in a query
    string, which are the two shapes the run-keyed routes take."""
    adopted = ok(["runs", "adopt", RUN_KEY], client, capsys)
    assert adopted["tracked"]["project"] == "agents/global"

    ok(["runs", "meta", "set", RUN_KEY, "--title", "auth rate-limit fix"],
       client, capsys)
    read = ok(["runs", "meta", "get", RUN_KEY], client, capsys)
    assert read["meta"]["title"] == "auth rate-limit fix"
    assert read["run"] == RUN_FIELDS


def test_relating_two_runs_sends_the_nested_run_object(
    configured, client, platform_root, capsys
):
    related = ok(["runs", "relate", RUN_KEY, OTHER_KEY], client, capsys)
    assert related["created"] is True

    listed = ok(["runs", "relations", RUN_KEY], client, capsys)
    assert [entry["slug"] for entry in listed["relations"]] == ["review-mr-163"]


def test_taking_the_spool_is_the_post_the_route_wants(
    configured, client, platform_root, capsys
):
    """A body-less POST still has to arrive as one — an empty spool is a 200 and
    ``[]``, which is most calls."""
    payload = ok(["pending", "take"], client, capsys)
    assert payload == {"pending": [], "count": 0}


def test_standing_orders_can_be_written_from_stdin(
    configured, client, platform_root, monkeypatch, capsys
):
    """The document is multi-line and the shell eats backticks, so ``-`` is the
    form the taskdef points at."""
    import io

    document = "always spawn reviewers with `sol`\nnever wind a session down\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(document))

    stored = ok(["orders", "set", "-"], client, capsys)
    assert stored["instructions"] == document.strip()

    read = ok(["orders", "get"], client, capsys)
    assert read["instructions"] == document.strip()
