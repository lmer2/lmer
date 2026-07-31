"""The assistant's HTTP surface (issue #141, T60; spec §8).

:mod:`lmer_platform.assistant` shipped with the whole lifecycle — start, stop,
rotate, the handoff hand-forward, the digest spool — and nothing in
:mod:`lmer_platform.api` imported it, so all of it was callable only in-process.
The half that made it a bug rather than a gap: the ``orchestrate`` taskdef tells a
starting assistant to *ask the platform* for the handover note its predecessor
left, and there was no route to ask. This file is about the door rather than the
room; tests/test_platform_assistant.py owns the lifecycle itself.

What is worth pinning here:

- **The route list.** The taskdef makes ``GET /api`` the authority on what this
  build serves, and tells the assistant to say plainly that it was not briefed
  when a route is absent. A route missing from that list is therefore invisible
  even once it works, so the index entry is checked mechanically — every served
  ``/api/assistant`` route has to appear there, including ones a later slice adds.
- **The two stop verbs.** ``POST /api/sessions/{id}/exit`` refuses a
  ``kind="assistant"`` session, because killing the process is the easy half and
  the pointer, ``stopped_at`` and ``stop_reason`` belong to the module that owns
  the state file. The fleet view lists the assistant as a row like any other, so
  the wrong verb is one tap away — the pair is checked together.
- **A read stays a read.** ``ensure_running`` sits in the same module as
  ``status``, and a UI polls the status route; wiring the idempotent starter to a
  GET would make opening a page cost a session slot.
- **The refusals keep their own status** (409 for a second assistant, 429 at the
  cap, 503 for a host that cannot see the taskdef), because that is the whole
  reason the code rides on the exception.

Sessions are started for real against the stub standing in for ``lmer`` that
tests/test_platform_assistant.py uses, so the spawn, the registry entry and the
signal ladder are genuinely exercised. The stub-lifetime trap comes with them: a
stub that exits cleanly has its registry entry reaped by the watcher thread, so
every test that asserts on a *running* assistant sets ``FAKE_LMER_SLEEP`` and
kills the process in a ``finally``.
"""

import contextlib
import os
import re

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api, assistant, registry, store
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env

SECRET = "test-secret-value"

#: Every route this slice adds, as ``(method, path)``. The auth sweep and the
#: index guard both walk it, and the index guard additionally re-derives the list
#: from the app — so a route added later without an entry in ``GET /api`` fails
#: there rather than being silently unreachable for the assistant.
ASSISTANT_ROUTES = (
    ("GET", "/api/assistant"),
    ("POST", "/api/assistant/start"),
    ("POST", "/api/assistant/stop"),
    ("POST", "/api/assistant/rotate"),
    ("GET", "/api/assistant/handoff"),
    ("POST", "/api/assistant/handoff"),
    ("GET", "/api/assistant/instructions"),
    ("POST", "/api/assistant/instructions"),
    ("POST", "/api/assistant/pending"),
)

#: The routes that neither spawn nor signal anything, so a test may call all of
#: them in one loop without owning a process. Not "read-only" — two of them write
#: platform state; what they have in common is that no container is involved.
PROCESSLESS_ROUTES = (
    ("GET", "/api/assistant"),
    ("GET", "/api/assistant/handoff"),
    ("POST", "/api/assistant/handoff"),
    ("GET", "/api/assistant/instructions"),
    ("POST", "/api/assistant/instructions"),
    ("POST", "/api/assistant/pending"),
)


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", "LMER_REPO_URL", "LMER_PLATFORM_PORTS_FILE",
                 "FAKE_LMER_SLEEP", "FAKE_LMER_EXIT", cfg.ENV_BIND_ADDRESS,
                 cfg.ENV_BIND_PORT, cfg.ENV_CONTAINER_URL, cfg.ENV_SECRET_FILE):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def fake_lmer(tmp_path):
    """A stub standing in for `lmer`: announces its argv, then exits.

    Same shape as tests/test_platform_assistant.py's, minus the environment dump
    it does not need: what the child was *given* is that file's subject, while
    this one only needs a real process to start, be found in the registry, and be
    signalled. With ``FAKE_LMER_SLEEP`` it stays up, which is how a test keeps a
    registry entry around long enough to make an assertion about it.
    """
    script = tmp_path / "fake-lmer"
    script.write_text(
        "#!/bin/sh\n"
        'echo "fake lmer started: $*"\n'
        'if [ -n "$FAKE_LMER_SLEEP" ]; then sleep "$FAKE_LMER_SLEEP"; fi\n'
        'exit "${FAKE_LMER_EXIT:-0}"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def config(platform_root, fake_lmer):
    return cfg.load({"lmer_bin": str(fake_lmer)})


@pytest.fixture
def client(config):
    """A client over the real routes, with a stub fleet view (no work repo)."""
    return client_for(config)


@pytest.fixture
def long_lived(monkeypatch):
    """Keep the stub alive for the length of a test.

    A clean exit reaps the registry entry, and the registry is what answers "is
    an assistant running" — so without this, every assertion about a live one
    races the watcher thread.
    """
    monkeypatch.setenv("FAKE_LMER_SLEEP", "30")


def client_for(config):
    return TestClient(
        api.create_app(
            config, SECRET, state_builder=lambda config, force_pull=False: {}
        )
    )


def bearer_header(token=SECRET):
    return {"Authorization": f"Bearer {token}"}


def kill(pid):
    if isinstance(pid, int) and pid > 1:
        with contextlib.suppress(OSError):
            os.kill(pid, 9)


@contextlib.contextmanager
def running(client, **body):
    """Start the assistant over the route, and make sure it is gone by the end."""
    response = client.post(
        "/api/assistant/start", headers=bearer_header(), json=body or None
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    try:
        yield payload
    finally:
        kill(payload.get("pid"))


def index_text(client):
    return client.get("/api", headers=bearer_header()).text


def listed(index, method, path):
    """Whether *index* advertises exactly *method* on exactly *path*.

    The negative lookahead is what makes this an assertion rather than a
    coincidence: ``/api/assistant`` is a prefix of four other routes, so a plain
    substring check would report the status route as listed when only its
    children were.
    """
    return re.search(rf"\b{method}\s+{re.escape(path)}(?!\S)", index) is not None


def served_assistant_routes(client):
    """``(method, path)`` for every assistant route the app actually serves."""
    return {
        (method, route.path)
        for route in client.app.routes
        if route.path.startswith("/api/assistant")
        for method in sorted(getattr(route, "methods", ()) or ())
        if method in ("GET", "POST")
    }


# --- discoverability: the index is how the assistant learns these exist -------
#
# Not cosmetic. The ``orchestrate`` taskdef points the assistant at ``GET /api``
# as the authority on what this build serves and tells it to report an absent
# route rather than inventing one, so a working route missing from that list is
# a route the assistant will never call.

def test_every_served_assistant_route_is_in_the_api_index(client):
    """Derived from the app rather than from a list in this file.

    A hand-maintained list would go stale in exactly the direction that hurts:
    the next slice adds a route, nobody adds the index line, and the assistant
    goes on saying that part has not shipped.
    """
    index = index_text(client)
    served = served_assistant_routes(client)

    assert served == set(ASSISTANT_ROUTES), (
        "the routes this file knows about and the ones the app serves disagree"
    )
    missing = sorted(
        f"{method} {path}" for method, path in served if not listed(index, method, path)
    )
    assert missing == [], f"served but absent from GET /api: {missing}"


def test_the_index_sends_a_stop_to_the_assistants_own_verb(client):
    """The one confusion this block of the index exists to prevent."""
    index = index_text(client)
    assert "/api/sessions/{id}/exit" in index
    assert re.search(r"NEVER\s+/api/sessions/\{id\}/exit", index), (
        "the index has to say which stop verb the assistant is not"
    )


def test_the_index_says_the_drain_is_destructive(client):
    """A client that treats it as a peek loses the operator's digests."""
    assert "DESTRUCTIVE" in index_text(client)


def test_the_index_says_nothing_pushes_and_names_what_to_watch(client):
    """T89, from the authority the taskdef sends the assistant to.

    A live incarnation read the spool as a push ("the orchestrator already pushes me
    digests") and then sat idle while a finished review's digest waited to be
    evicted. The route list is the one place it goes back to, so the correction and
    the non-consuming signal to watch instead belong in it.
    """
    index = index_text(client)
    assert "Nothing pushes a" in index, "the index leaves the push question open"
    assert "non-consuming" in index, (
        "nothing points a watch at the pending count on the status"
    )
    assert "watch that instead" in index, (
        "the drain still reads as the thing to poll, which eats every digest"
    )


# --- auth ---------------------------------------------------------------------

@pytest.mark.parametrize("method, path", ASSISTANT_ROUTES)
def test_every_assistant_route_requires_auth(client, platform_root, method, path):
    response = client.request(method, path, json={"handoff": "x"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == f'Basic realm="{api.REALM}"'


def test_an_unauthenticated_caller_changes_nothing(client, platform_root):
    """Two of these are destructive and one of them starts a container."""
    assistant.notify("mr-166 has new findings")

    for method, path in ASSISTANT_ROUTES:
        response = client.request(method, path, json={"handoff": "hijacked"})
        assert response.status_code == 401, f"{method} {path}"

    state = assistant.read_state()
    assert registry.list_sessions(live_only=False) == [], (
        "an unauthenticated peer must not be able to spawn the assistant"
    )
    assert state.handoff is None
    assert [note.note for note in state.pending] == ["mr-166 has new findings"], (
        "nor to drain the spool it cannot read"
    )


# --- GET /api/assistant -------------------------------------------------------

def test_a_fresh_host_reports_no_assistant(client, platform_root):
    """Started on demand (D11), so "none" is the ordinary answer and not an error."""
    response = client.get("/api/assistant", headers=bearer_header())

    assert response.status_code == 200
    payload = response.json()
    assert payload["running"] is False
    assert payload["session_id"] is None
    assert payload["generation"] == 0
    assert payload["stale"] is False
    assert payload["pending"] == 0
    assert payload["taskdef"] == assistant.TASKDEF
    assert payload["target"] == assistant.TARGET


def test_the_status_route_starts_nothing(client, platform_root):
    """A read that started a container would make opening a page cost a slot.

    ``ensure_running`` is in the same module and is the idempotent entry point
    everything else reaches for, which makes wiring it to this route a plausible
    mistake rather than a hypothetical one.
    """
    for _ in range(3):
        assert client.get(
            "/api/assistant", headers=bearer_header()
        ).json()["running"] is False

    assert registry.list_sessions(live_only=False) == []
    assert not assistant.state_path().exists(), "a read must not write state either"


def test_a_live_assistant_is_reported_with_its_session(client, platform_root):
    """No process needed: the registry is what answers "is one running" (D11).

    Planted rather than spawned because what is being checked is the reconciliation
    the route serves, not the spawn — and a daemon that restarted and lost its
    pointer is exactly this shape.
    """
    registry.register(
        "s-orphan", kind=assistant.KIND, pid=os.getpid(),
        started_at="2026-07-27T09:00:00Z",
    )

    payload = client.get("/api/assistant", headers=bearer_header()).json()

    assert payload["running"] is True
    assert payload["session_id"] == "s-orphan"
    assert payload["tracked"] is False, "state never recorded this one"
    assert payload["started_at"] == "2026-07-27T09:00:00Z"
    assert payload["age_seconds"] is not None


def test_a_stale_pointer_is_reported_rather_than_repaired(client, platform_root):
    """A dead assistant is a fact about the host; a read that tidied it up would
    make the next post-mortem read a file that had already destroyed the evidence."""
    store.write_json(assistant.state_path(), {"session_id": "s-gone", "generation": 2})

    payload = client.get("/api/assistant", headers=bearer_header()).json()

    assert payload["running"] is False
    assert payload["stale"] is True
    assert payload["session_id"] == "s-gone"
    assert payload["generation"] == 2
    assert assistant.read_state().session_id == "s-gone"


# --- POST /api/assistant/start ------------------------------------------------

def test_starting_the_assistant_over_the_route(client, config, long_lived):
    secret = cfg.ensure_secret(config)
    response = client.post("/api/assistant/start", headers=bearer_header())
    payload = response.json()
    try:
        assert response.status_code == 200, (
            "200 rather than 202: the session is spawned and in the registry when "
            "this returns, so the body is a fact and not an acknowledgement"
        )
        assert payload["running"] is True
        assert payload["generation"] == 1
        assert payload["session_id"] and payload["pid"]
        assert payload["taskdef"] == assistant.TASKDEF

        entry = registry.read_session(payload["session_id"])
        assert entry is not None
        assert entry["kind"] == assistant.KIND
        assert secret not in response.text, (
            "the session is handed the operator's own key; the reply is not"
        )

        live = client.get("/api/assistant", headers=bearer_header()).json()
        assert live["session_id"] == payload["session_id"]
        assert live["running"] is True
    finally:
        kill(payload.get("pid"))


def test_a_start_can_carry_the_note_the_new_incarnation_is_told(client, long_lived):
    with running(client, handoff="mr-166 is waiting on review") as payload:
        assert payload["handoff"] == "mr-166 is waiting on review"
        assert client.get(
            "/api/assistant/handoff", headers=bearer_header()
        ).json()["handoff"] == "mr-166 is waiting on review"


def test_a_second_start_is_a_409_naming_the_incumbent(client, long_lived):
    """Two operators tapping "open chat" must not cost the running one its window."""
    with running(client) as payload:
        response = client.post("/api/assistant/start", headers=bearer_header())

        assert response.status_code == 409
        assert payload["session_id"] in response.json()["detail"]
        assert "rotate" in response.json()["detail"], (
            "the refusal has to name the verb for wanting a fresh window"
        )
        assert client.get(
            "/api/assistant", headers=bearer_header()
        ).json()["session_id"] == payload["session_id"]


def test_a_refused_start_does_not_rewrite_the_live_ones_environment(
    client, config, long_lived, monkeypatch
):
    """The trap in exposing ``start`` over HTTP: it is not a pure refusal.

    A start writes the assistant's 0600 env file, and the live session read that
    file at launch — so the refusal has to come *first*. The reachability answer
    is changed under the running assistant here (an operator installing docker, or
    rebinding the platform), which is what makes the bytes differ if anything
    re-derives them: a route that prepared the environment itself, or called
    ``stop`` before ``start``, fails here rather than in production.
    """
    monkeypatch.setattr(cfg, "detect_runtime", lambda: "podman")
    cfg.ensure_secret(config)

    with running(client):
        before = assistant.env_file_path().read_bytes()
        monkeypatch.setattr(cfg, "detect_runtime", lambda: "docker")

        assert client.post(
            "/api/assistant/start", headers=bearer_header()
        ).status_code == 409
        assert assistant.env_file_path().read_bytes() == before, (
            "the file the live session was given was rewritten behind it"
        )


def test_a_start_at_capacity_is_a_429_with_the_numbers(platform_root, fake_lmer):
    """The assistant counts against the global cap like any other container, so the
    refusal has to say what to free — "I cannot open the chat" needs a reason."""
    config = cfg.load({"lmer_bin": str(fake_lmer), "max_concurrent_sessions": 1})
    registry.register("s-worker", kind="worker", pid=os.getpid())

    response = client_for(config).post(
        "/api/assistant/start", headers=bearer_header()
    )

    assert response.status_code == 429
    detail = response.json()["detail"]
    assert "1/1" in detail
    assert "max_concurrent_sessions (1)" in detail
    assert assistant.status().running is False


def test_a_host_that_cannot_see_the_taskdef_is_a_503(client, tmp_path, monkeypatch):
    """Refused here rather than in a container that dies seconds later."""
    empty = tmp_path / "taskdefs"
    (empty / "chat").mkdir(parents=True)
    (empty / "chat" / "instructions.txt").write_text("hi", encoding="utf-8")
    monkeypatch.setattr(assistant, "_get_taskdef_paths", lambda _root: [empty])

    response = client.post("/api/assistant/start", headers=bearer_header())

    assert response.status_code == 503
    assert assistant.TASKDEF in response.json()["detail"]
    assert str(empty) in response.json()["detail"]
    assert registry.list_sessions(live_only=False) == []


def test_a_non_string_handoff_on_a_start_keeps_its_own_refusal(client, platform_root):
    """Uncoerced, so the caller is told what was wrong with what they sent."""
    response = client.post(
        "/api/assistant/start", headers=bearer_header(), json={"handoff": 5}
    )

    assert response.status_code == 400
    assert "handoff must be non-empty text" in response.json()["detail"]
    assert registry.list_sessions(live_only=False) == [], (
        "a refused handoff must not cost a container that is already starting"
    )


# --- POST /api/assistant/stop -------------------------------------------------

def test_stopping_the_assistant_over_the_route(client, config, long_lived):
    with running(client) as payload:
        response = client.post("/api/assistant/stop", headers=bearer_header())

        assert response.status_code == 200
        stopped = response.json()
        assert stopped["stopped"] is True
        assert stopped["reason"] == "operator"
        assert stopped["running"] is False
        assert stopped["session_id"] is None
        assert stopped["stale"] is False, "a stop must not leave a pointer behind"
        assert stopped["generation"] == 1, "generation counts incarnations"

        assert registry.read_session(payload["session_id"]) is None, (
            "a requested exit is not a crash and must not read as one"
        )
        state = assistant.read_state()
        assert state.session_id is None
        assert state.stop_reason == "operator"
        assert state.stopped_at


def test_stopping_when_nothing_runs_is_not_a_failure(client, platform_root):
    response = client.post("/api/assistant/stop", headers=bearer_header())

    assert response.status_code == 200
    assert response.json()["stopped"] is False
    assert response.json()["running"] is False


def test_a_stop_records_the_note_for_the_next_incarnation(client, platform_root):
    """Which is what lets a rotation write its note and stop in one call — and it
    has to work with nothing running, because that is the crash case."""
    response = client.post(
        "/api/assistant/stop",
        headers=bearer_header(),
        json={"reason": "rotation", "handoff": "waiting on the operator re: schema"},
    )

    assert response.status_code == 200
    assert response.json()["reason"] == "rotation"
    assert client.get(
        "/api/assistant/handoff", headers=bearer_header()
    ).json()["handoff"] == "waiting on the operator re: schema"


@pytest.mark.parametrize("reason", ["", "shutdown", None, 3])
def test_an_unknown_stop_reason_is_refused(client, platform_root, reason):
    """``rotation`` has to stay distinguishable from an operator stop, or the next
    context-pressure question is unanswerable from state alone. ``""`` is in here
    because defaulting on falsiness rather than on absence would record a caller's
    mistake as an operator stop."""
    response = client.post(
        "/api/assistant/stop", headers=bearer_header(), json={"reason": reason}
    )

    assert response.status_code == 400
    assert "invalid stop reason" in response.json()["detail"]


# --- the other stop verb, which refuses (spec §7.5) ---------------------------

def test_the_generic_exit_verb_still_refuses_the_assistant(client, config, long_lived):
    """The pair, together, because the danger is in the pair.

    ``exit`` would kill the process and skip everything that makes the ending
    legible — the pointer stays, ``stop_reason`` is never written, and the next
    start reports a stale session. So that verb refuses a ``kind="assistant"``
    session outright, and this route is the one that owns the bookkeeping. Both
    halves are asserted here: the refusal leaves the assistant *and its state*
    alone, and the stop then does the whole job.
    """
    with running(client) as payload:
        session_id = payload["session_id"]

        refused = client.post(
            f"/api/sessions/{session_id}/exit", headers=bearer_header()
        )

        assert refused.status_code == 409
        assert "assistant" in refused.json()["detail"]
        assert registry.read_session(session_id) is not None
        assert assistant.read_state().session_id == session_id, (
            "the pointer this refusal exists to protect was taken away anyway"
        )
        assert client.get(
            "/api/assistant", headers=bearer_header()
        ).json()["running"] is True

        stopped = client.post("/api/assistant/stop", headers=bearer_header()).json()

        assert stopped["stopped"] is True
        assert registry.read_session(session_id) is None
        assert assistant.read_state().stop_reason == "operator"


# --- POST /api/assistant/rotate -----------------------------------------------

def test_rotating_replaces_the_session_and_carries_the_note(client, long_lived):
    with running(client) as first:
        response = client.post(
            "/api/assistant/rotate",
            headers=bearer_header(),
            json={"handoff": "3 runs live, 1 blocked on you"},
        )
        payload = response.json()
        try:
            assert response.status_code == 200
            assert payload["running"] is True
            assert payload["session_id"] != first["session_id"]
            assert payload["generation"] == 2
            assert payload["handoff"] == "3 runs live, 1 blocked on you"
            assert registry.read_session(first["session_id"]) is None

            handoff = client.get(
                "/api/assistant/handoff", headers=bearer_header()
            ).json()
            assert handoff["handoff"] == "3 runs live, 1 blocked on you"
            assert handoff["generation"] == 2, (
                "the successor has to be able to tell whose note this is"
            )
        finally:
            kill(payload.get("pid"))


def test_rotating_when_nothing_is_running_starts_one(client, long_lived):
    """The case a rotation policy actually fires in is often an assistant that has
    already died; a 409 there would leave the operator with no chat."""
    response = client.post("/api/assistant/rotate", headers=bearer_header())
    payload = response.json()
    try:
        assert response.status_code == 200
        assert payload["running"] is True
        assert payload["generation"] == 1
    finally:
        kill(payload.get("pid"))


# --- the handoff, read and written (§8.3) -------------------------------------

def test_a_fresh_host_says_nobody_left_a_handover(client, platform_root):
    """``null`` with a reason, never a 404: the taskdef tells a starting assistant
    to say it was not briefed, and that instruction needs "there is no note" to be
    distinguishable from "there is no route"."""
    payload = client.get("/api/assistant/handoff", headers=bearer_header()).json()

    assert payload["handoff"] is None
    assert payload["handoff_at"] is None
    assert payload["generation"] == 0
    assert payload["limit"] == assistant.MAX_HANDOFF_CHARS
    assert "not briefed" in payload["note"]


def test_writing_the_handoff_answers_with_what_was_stored(client, platform_root):
    """One shape for the read and the write, so an assistant that has just written
    its note does not have to re-read to learn what landed — and what comes back is
    the stored text, stripped, rather than the bytes that were sent."""
    written = client.post(
        "/api/assistant/handoff",
        headers=bearer_header(),
        json={"handoff": "  mr-166 waiting on review  "},
    )

    assert written.status_code == 200
    payload = written.json()
    assert payload["handoff"] == "mr-166 waiting on review"
    assert payload["handoff_at"]
    assert payload["note"] is None, "there is a note now; the hint is for when there is not"

    read_back = client.get("/api/assistant/handoff", headers=bearer_header()).json()
    assert read_back == payload


def test_a_starting_assistant_can_ask_for_its_handover(client, platform_root):
    """The whole reason this slice exists.

    The ``orchestrate`` taskdef tells the assistant to read its handover before it
    says anything to the operator, and to find the route in ``GET /api``. Either
    half alone is a promise nothing keeps, so both are checked in one place.
    """
    client.post(
        "/api/assistant/handoff",
        headers=bearer_header(),
        json={"handoff": "mr-166 is waiting on review; I promised a summary"},
    )

    assert listed(index_text(client), "GET", "/api/assistant/handoff")
    payload = client.get("/api/assistant/handoff", headers=bearer_header()).json()
    assert payload["handoff"] == "mr-166 is waiting on review; I promised a summary"
    assert payload["note"] is None


def test_an_oversized_handoff_is_refused_and_nothing_is_stored(client, platform_root):
    """It is a compact summary handed to a fresh window, not a transcript."""
    client.post(
        "/api/assistant/handoff", headers=bearer_header(), json={"handoff": "keep me"}
    )

    response = client.post(
        "/api/assistant/handoff",
        headers=bearer_header(),
        json={"handoff": "x" * (assistant.MAX_HANDOFF_CHARS + 1)},
    )

    assert response.status_code == 400
    assert str(assistant.MAX_HANDOFF_CHARS) in response.json()["detail"]
    assert assistant.read_state().handoff == "keep me", (
        "a refused write must not clear what a predecessor left"
    )


@pytest.mark.parametrize("payload", [{}, {"handoff": ""}, {"handoff": "  "},
                                     {"handoff": None}, {"handoff": 7},
                                     {"handoff": ["a"]}])
def test_an_unusable_handoff_is_a_400_not_a_500(client, platform_root, payload):
    """Uncoerced on purpose: ``str()`` here would store the string ``"7"`` and
    call it a handover note."""
    response = client.post(
        "/api/assistant/handoff", headers=bearer_header(), json=payload
    )

    assert response.status_code == 400
    assert "handoff must be non-empty text" in response.json()["detail"]


# --- the operator's standing orders, read and written (T87) -------------------
#
# The handoff's sibling over HTTP, and the routes are shaped like it deliberately:
# same read/write body, same daemon-side ``limit``, same "there is none" note that
# has to be distinguishable from an absent route. What differs is that nothing
# consumes this one, so the interesting assertions are about a document that stays.

def test_a_fresh_host_says_no_standing_orders_have_been_set(client, platform_root):
    """``null`` with a reason, never a 404. The taskdef tells a starting assistant to
    fetch this and follow it, and "nobody has told me anything" has to be tellable
    apart from "this build has no such route"."""
    payload = client.get(
        "/api/assistant/instructions", headers=bearer_header()
    ).json()

    assert payload["instructions"] is None
    assert payload["instructions_at"] is None
    assert payload["limit"] == assistant.MAX_INSTRUCTIONS_CHARS
    assert "no standing instructions" in payload["note"]
    assert "ordinary state" in payload["note"], (
        "an empty document reads as a missing briefing unless it is said not to be"
    )


def test_writing_the_standing_orders_answers_with_what_was_stored(
    client, platform_root
):
    """One shape for the read and the write: an assistant that just promised the
    operator a rule can check the file holds that rule without a second call."""
    written = client.post(
        "/api/assistant/instructions",
        headers=bearer_header(),
        json={"instructions": "  always spawn reviewers with the sol preset  "},
    )

    assert written.status_code == 200
    payload = written.json()
    assert payload["instructions"] == "always spawn reviewers with the sol preset"
    assert payload["instructions_at"]
    assert payload["note"] is None

    read_back = client.get(
        "/api/assistant/instructions", headers=bearer_header()
    ).json()
    assert read_back == payload


def test_a_post_replaces_the_whole_document_rather_than_appending(
    client, platform_root
):
    """Whole-document on purpose: an appended "stop doing X" would arrive as a rule
    contradicting an earlier one, and the next incarnation would read both."""
    client.post(
        "/api/assistant/instructions",
        headers=bearer_header(),
        json={"instructions": "always use the sol preset\nnever rotate me at night"},
    )
    client.post(
        "/api/assistant/instructions",
        headers=bearer_header(),
        json={"instructions": "always use the sol preset"},
    )

    payload = client.get(
        "/api/assistant/instructions", headers=bearer_header()
    ).json()
    assert payload["instructions"] == "always use the sol preset"
    assert "never rotate me at night" not in payload["instructions"], (
        "a retired rule survived the write that dropped it"
    )


def test_the_standing_orders_outlive_the_incarnation_that_was_told_them(
    client, long_lived
):
    """Recorded whether or not one is running, and *kept* across the transition that
    replaces one — which is the entire difference from the handoff beside it."""
    client.post(
        "/api/assistant/instructions",
        headers=bearer_header(),
        json={"instructions": "always tell me before spawning anything"},
    )

    with running(client) as first:
        rotated = client.post(
            "/api/assistant/rotate",
            headers=bearer_header(),
            json={"handoff": "one run blocked on you"},
        ).json()
        try:
            assert rotated["session_id"] != first["session_id"]
            assert rotated["generation"] == 2
            assert client.get(
                "/api/assistant/instructions", headers=bearer_header()
            ).json()["instructions"] == "always tell me before spawning anything"
        finally:
            kill(rotated.get("pid"))


def test_an_oversized_document_is_refused_and_nothing_is_stored(client, platform_root):
    """Every future incarnation pays to read this one, so the bound is tighter than
    the handoff's — and a refused write must not clear what is in force."""
    client.post(
        "/api/assistant/instructions",
        headers=bearer_header(),
        json={"instructions": "always ask before spawning"},
    )

    response = client.post(
        "/api/assistant/instructions",
        headers=bearer_header(),
        json={"instructions": "x" * (assistant.MAX_INSTRUCTIONS_CHARS + 1)},
    )

    assert response.status_code == 400
    assert str(assistant.MAX_INSTRUCTIONS_CHARS) in response.json()["detail"]
    assert assistant.read_state().instructions == "always ask before spawning"


@pytest.mark.parametrize("payload", [{}, {"instructions": ""}, {"instructions": "  "},
                                     {"instructions": None}, {"instructions": 7},
                                     {"instructions": ["a rule"]}])
def test_an_unusable_document_is_a_400_not_a_500(client, platform_root, payload):
    """Uncoerced, as with the handoff, and never a way to clear the orders: an empty
    POST is a composer bug far more often than an operator with no preferences."""
    response = client.post(
        "/api/assistant/instructions", headers=bearer_header(), json=payload
    )

    assert response.status_code == 400
    assert "instructions must be non-empty text" in response.json()["detail"]


def test_a_credential_in_the_standing_orders_is_never_served_back(
    client, platform_root
):
    """The operator is dictating into a chat window, so a token will end up in here.

    Both directions are checked from this end: what a browser and the assistant are
    served, and what a state file this process did *not* write is served as — the
    file is plain and hand-editable, so "it was clean when we stored it" is not a
    property this route may assume.
    """
    written = client.post(
        "/api/assistant/instructions",
        headers=bearer_header(),
        json={"instructions": "always use Authorization: Bearer glpat-notarealtoken"},
    )
    assert "glpat-notarealtoken" not in written.text

    store.write_json(assistant.state_path(), {
        "instructions": "always use Authorization: Bearer glpat-notarealtoken",
    })
    served = client.get("/api/assistant/instructions", headers=bearer_header())
    assert "glpat-notarealtoken" not in served.text, (
        "a hand-edited state file is served verbatim"
    )
    assert "<redacted>" in served.json()["instructions"]


def test_reading_the_standing_orders_is_a_safe_read_with_no_destructive_twin(
    client, platform_root
):
    """Unlike the digest spool, this document is meant to be re-read forever — so the
    GET is the whole read surface and there is no take-and-clear beside it to
    confuse it with. A ``DELETE`` or a drain here would be a way for the operator's
    standing orders to vanish without them saying so."""
    client.post(
        "/api/assistant/instructions",
        headers=bearer_header(),
        json={"instructions": "always ask before spawning"},
    )

    for _ in range(3):
        assert client.get(
            "/api/assistant/instructions", headers=bearer_header()
        ).json()["instructions"] == "always ask before spawning"

    assert client.request(
        "DELETE", "/api/assistant/instructions", headers=bearer_header()
    ).status_code == 405
    assert assistant.read_state().instructions == "always ask before spawning"


def test_the_index_lists_the_standing_orders_as_never_consumed(client):
    """The taskdef makes ``GET /api`` the authority, and the one property an agent
    has to read off this line is that the document is not a baton."""
    index = index_text(client)
    assert listed(index, "GET", "/api/assistant/instructions")
    assert listed(index, "POST", "/api/assistant/instructions")
    assert "never consumed" in index


# --- POST /api/assistant/pending ----------------------------------------------

def test_taking_the_spooled_digests_drains_them(client, platform_root):
    """The seam §8.3 asks for, from the reading end: the daemon detects and spools,
    and the assistant takes what is waiting when it next has a turn."""
    assistant.notify("develop-issue-141 stopped on a question", kind="question")
    assistant.notify("review-mr-166 crashed", kind="crashed")

    response = client.post("/api/assistant/pending", headers=bearer_header())

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert [note["note"] for note in payload["pending"]] == [
        "develop-issue-141 stopped on a question", "review-mr-166 crashed",
    ], "oldest first, so a digest reads as a sequence of events"
    assert [note["kind"] for note in payload["pending"]] == ["question", "crashed"]

    assert client.post(
        "/api/assistant/pending", headers=bearer_header()
    ).json() == {"pending": [], "count": 0}


def test_an_empty_spool_is_a_200_not_an_error(client, platform_root):
    """Most calls return one: nothing has happened since the last drain."""
    response = client.post("/api/assistant/pending", headers=bearer_header())

    assert response.status_code == 200
    assert response.json() == {"pending": [], "count": 0}


def test_the_status_counts_the_spool_without_consuming_it(client, platform_root):
    """A UI badge must not eat the assistant's digests to draw itself."""
    assistant.notify("mr-166 has new findings")

    for _ in range(3):
        assert client.get(
            "/api/assistant", headers=bearer_header()
        ).json()["pending"] == 1

    assert client.post(
        "/api/assistant/pending", headers=bearer_header()
    ).json()["count"] == 1


def test_the_drain_is_not_a_get(client, platform_root):
    """Draining is destructive, and a safe-method spelling would let a browser
    prefetch or a double-poll eat the spool before anything read it."""
    assistant.notify("mr-166 has new findings")

    assert client.get(
        "/api/assistant/pending", headers=bearer_header()
    ).status_code == 405
    assert assistant.status().pending == 1


# --- the refusals, as an HTTP client sees them --------------------------------

@pytest.mark.parametrize("error, status", [
    (assistant.AssistantError, 400),
    (assistant.AssistantAlreadyRunning, 409),
    (assistant.AssistantCapacityError, 429),
    (assistant.TaskdefMissing, 503),
])
@pytest.mark.parametrize("verb, path", [
    ("start", "/api/assistant/start"),
    ("stop", "/api/assistant/stop"),
    ("rotate", "/api/assistant/rotate"),
    ("set_handoff", "/api/assistant/handoff"),
])
def test_every_refusal_keeps_its_own_status(
    client, platform_root, monkeypatch, error, status, verb, path
):
    """The status rides on the exception, which is the whole point of it being
    there: a refusal added to the module later arrives with its own code rather
    than falling through to a 500 with a traceback."""
    def refuse(*_args, **_kwargs):
        raise error("because of a reason")

    monkeypatch.setattr(assistant, verb, refuse)
    response = client.post(path, headers=bearer_header(), json={"handoff": "x"})

    assert response.status_code == status
    assert response.json()["detail"] == "because of a reason"


# --- payload safety -----------------------------------------------------------

def test_no_assistant_reply_carries_the_shared_secret(client, config, platform_root):
    """The assistant is the one container handed the operator's own key (T30), and
    a copy of it sits in the state directory these routes read from.

    Only the routes that neither spawn nor signal are walked here; start, stop and
    rotate answer with the same body the status route does, plus two scalars, so
    the shape is the same one being checked.
    """
    secret = cfg.ensure_secret(config)
    registry.register("s-adopted", kind=assistant.KIND, pid=os.getpid())
    assistant.notify("mr-166 has new findings")

    # One body for every route in the sweep: each writer reads its own field out of
    # it and ignores the rest, so the loop stays one loop.
    for method, path in PROCESSLESS_ROUTES:
        response = client.request(
            method, path, headers=bearer_header(),
            json={"handoff": "a note", "instructions": "always ask before spawning"},
        )
        assert response.status_code == 200, f"{method} {path}: {response.text}"
        assert secret not in response.text, f"{method} {path} leaked the secret"
        assert SECRET not in response.text, f"{method} {path} leaked the secret"
