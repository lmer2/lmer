"""Tests for the platform HTTP control plane (issue #141, slice M1 / T6).

Auth is the bulk of it: every route is gated, both schemes work, and a browser
gets prompted rather than a bare 401. Plus the guarantees the payload has to make
— no secret in a response, no credentialed URL, and stale sessions preserved so
crashed runs survive into the attention list.
"""

import base64

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api
from lmer_platform import config as cfg
from lmer_platform import registry, store
from tests.conftest import strip_lmer_env

SECRET = "test-secret-value"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", cfg.ENV_BIND_ADDRESS, cfg.ENV_BIND_PORT):
        monkeypatch.delenv(name, raising=False)


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
    """A canned payload so routing tests need no work repo."""
    calls = []

    def builder(config, *, force_pull=False):
        calls.append({"force_pull": force_pull})
        return {
            "schema": 1,
            "generated_at": "2026-07-26T12:00:00Z",
            "config": {"base_url": config.base_url},
            "mirror": {"present": True, "healthy": True},
            "runs": [],
            "attention": [],
            "counts": {},
            "totals": {"runs": 0, "live": 0, "attention": 0},
        }

    builder.calls = calls
    return builder


@pytest.fixture
def client(config, fake_state):
    return TestClient(api.create_app(config, SECRET, state_builder=fake_state))


def basic_header(username="", password=SECRET):
    raw = f"{username}:{password}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def bearer_header(token=SECRET):
    return {"Authorization": f"Bearer {token}"}


# --- construction -----------------------------------------------------------

def test_refuses_to_serve_without_a_secret(config, fake_state):
    with pytest.raises(ValueError, match="without a shared secret"):
        api.create_app(config, "", state_builder=fake_state)


# --- auth -------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/api/health", "/api/state"])
def test_get_routes_require_auth(client, path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", ["/api/rescan", "/api/prune"])
def test_post_routes_require_auth(client, path):
    assert client.post(path).status_code == 401


def test_unauthenticated_response_prompts_a_browser(client):
    """A phone hitting the daemon should get a credential prompt, not a wall."""
    response = client.get("/")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == f'Basic realm="{api.REALM}"'


def test_bearer_token_is_accepted(client):
    assert client.get("/api/health", headers=bearer_header()).status_code == 200


def test_basic_auth_is_accepted_with_any_username(client):
    assert client.get("/api/health", headers=basic_header("anyone")).status_code == 200


def test_basic_auth_accepts_secret_as_username(client):
    """Some clients put the token in the username field with no password."""
    raw = base64.b64encode(f"{SECRET}:".encode("utf-8")).decode("ascii")
    response = client.get("/api/health", headers={"Authorization": f"Basic {raw}"})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Bearer wrong-secret"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic not-base64!!"},
        {"Authorization": "Basic " + base64.b64encode(b"u:wrong").decode("ascii")},
        {"Authorization": "Digest whatever"},
        {"Authorization": SECRET},  # no scheme
    ],
)
def test_bad_credentials_are_rejected(client, header):
    assert client.get("/api/health", headers=header).status_code == 401


def test_non_utf8_basic_credentials_are_rejected(client):
    raw = base64.b64encode(b"\xff\xfe:\xff").decode("ascii")
    assert client.get(
        "/api/health", headers={"Authorization": f"Basic {raw}"}
    ).status_code == 401


def test_auth_failures_are_logged(client, caplog):
    client.get("/api/health", headers=bearer_header("nope"))
    assert any("platform_auth_rejected" in r.message for r in caplog.records)


@pytest.mark.parametrize("header_for", [
    lambda bad: basic_header("operator", bad),   # the password field
    lambda bad: basic_header(bad, ""),           # token-as-username clients
])
def test_a_non_ascii_credential_is_a_401_and_not_a_500(client, caplog, header_for):
    """One typo'd character must not turn the door into a server error.

    ``secrets.compare_digest`` refuses two ``str`` arguments unless both are
    ASCII-only, and ``_presented_secret`` hands it whatever the header decoded
    to — so a browser credential prompt filled in with an accented character
    used to raise ``TypeError`` off the dependency path. That answers 500: no
    ``WWW-Authenticate``, so the browser never re-prompts, and no
    ``platform_auth_rejected``, so the attempt is invisible in the daemon's own
    record of refused auth. All three consequences are asserted, not just the
    status.

    Basic in both its shapes, and Basic only: a header *value* is ASCII on the
    wire, so a non-ASCII bearer token cannot be sent at all — base64 is what
    carries these bytes to the comparison, which is exactly the browser
    credential prompt this reaches the daemon through.
    """
    response = client.get("/api/health", headers=header_for("pässwort"))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == f'Basic realm="{api.REALM}"'
    assert any("platform_auth_rejected" in r.message for r in caplog.records)


def test_a_non_ascii_secret_still_opens_the_door(config, fake_state):
    """The fix is an encoding, not a filter, and this is the difference.

    A rule refusing non-ASCII *input* would answer 401 for the test above while
    also making this secret permanently unusable — the same request resolved
    into a different wrong answer. Encoding is total: every credential gets
    compared, and the right one still opens the door.
    """
    secret = "sehr-gehéimes-wört"
    client = TestClient(api.create_app(config, secret, state_builder=fake_state))

    assert client.get(
        "/api/health", headers=basic_header("op", secret)
    ).status_code == 200
    assert client.get(
        "/api/health", headers=basic_header("op", "wrong")
    ).status_code == 401


# --- routes -----------------------------------------------------------------

def test_route_list_lives_at_api(client):
    """`/` belongs to the UI (T11); the plain-text route list moved to /api."""
    body = client.get("/api", headers=bearer_header()).text
    assert "/api/state" in body
    assert "/api/health" in body


def test_health_is_cheap_and_does_not_pull(client, fake_state):
    response = client.get("/api/health", headers=bearer_header())
    payload = response.json()

    assert payload["ok"] is True
    assert payload["bind"].startswith("http://127.0.0.1:")
    assert set(payload["mirror"]) == {"present", "healthy", "last_pull_at"}
    assert fake_state.calls == [], "health must not build the full state"


def test_state_returns_the_inventory(client, fake_state):
    payload = client.get("/api/state", headers=bearer_header()).json()
    assert payload["totals"] == {"runs": 0, "live": 0, "attention": 0}
    assert fake_state.calls == [{"force_pull": False}]


def test_rescan_forces_a_pull(client, fake_state):
    client.post("/api/rescan", headers=bearer_header())
    assert fake_state.calls == [{"force_pull": True}]


def test_prune_removes_dead_sessions_only_when_asked(client, platform_root):
    registry.register("s-live", pid=__import__("os").getpid())
    registry.register("s-dead", pid=2**22)

    payload = client.post("/api/prune", headers=bearer_header()).json()
    assert payload == {"removed": ["s-dead"], "count": 1}
    assert registry.read_session("s-live") is not None


def test_state_does_not_prune(client, platform_root, fake_state):
    """A stale entry is how a crashed run stays visible."""
    registry.register("s-dead", pid=2**22)
    client.get("/api/state", headers=bearer_header())
    assert registry.read_session("s-dead") is not None


# --- payload safety ---------------------------------------------------------

def test_secret_never_appears_in_any_response(config, platform_root):
    app = api.create_app(config, SECRET, state_builder=api.build_state)
    client = TestClient(app)

    for method, path in [("get", "/"), ("get", "/api/health"),
                         ("get", "/api/state"), ("post", "/api/rescan"),
                         ("post", "/api/prune")]:
        response = getattr(client, method)(path, headers=bearer_header())
        assert SECRET not in response.text, f"{path} leaked the secret"


def test_config_summary_scrubs_credentials_from_work_repo_url(platform_root):
    config = cfg.load({
        "work_repo_url": "https://oauth2:leaky@git.example.com/agents/work.git"
    })
    app = api.create_app(config, SECRET, state_builder=api.build_state)
    response = TestClient(app).get("/api/state", headers=bearer_header())

    assert "leaky" not in response.text
    assert response.json()["config"]["work_repo_url"] is None or (
        "leaky" not in response.json()["config"]["work_repo_url"]
    )


def test_config_summary_omits_secret_path_and_exposes_caps(client):
    summary = client.get("/api/state", headers=bearer_header()).json()["config"]
    assert "secret" not in " ".join(summary).lower()
    assert summary["base_url"].startswith("http://")


# --- build_state (integration, no network) ----------------------------------

def test_build_state_reports_unconfigured_work_repo(platform_root):
    payload = api.build_state(cfg.load())
    assert payload["mirror"]["present"] is False
    assert "no work repo configured" in payload["mirror"]["last_error"]
    assert payload["totals"]["runs"] == 0


def test_build_state_includes_sessions_without_run_dirs(platform_root):
    """A just-spawned session must be visible before its first work commit."""
    import os

    registry.register(
        "s-1", pid=os.getpid(),
        run={"host": "gitlab.example.com", "project": "agents/global", "slug": "develop-1"},
        task={"taskdef": "develop", "target": "issue-141"},
    )
    payload = api.build_state(cfg.load())

    assert payload["totals"]["runs"] == 1
    assert payload["runs"][0]["slug"] == "develop-1"
    assert payload["runs"][0]["state"] == "running"


def test_build_state_surfaces_crashed_sessions_in_attention(platform_root):
    registry.register(
        "s-dead", pid=2**22,
        run={"host": "gitlab.example.com", "project": "agents/global", "slug": "develop-2"},
    )
    payload = api.build_state(cfg.load())

    assert payload["totals"]["attention"] == 1
    assert payload["attention"][0]["attention"]["reason"] == "crashed"


def test_build_state_shape_is_stable(platform_root):
    payload = api.build_state(cfg.load())
    assert set(payload) >= {
        "schema", "generated_at", "config", "mirror", "runs", "attention",
        "counts", "totals", "tracked",
    }


# --- scoping (D25) ----------------------------------------------------------
#
# The regression these lock in: the fleet view once enumerated the whole shared
# work repo and reported other devs' blocked runs as needing this operator's
# input.

def _plant_mirror_run(config, slug, *, host="gitlab.example.com", project="agents/global",
                      stop_reason=None, recorded_slug=None):
    path = config.mirror_path / host / project / "runs" / slug
    path.mkdir(parents=True, exist_ok=True)
    lines = ["schema: 1", "status: in-progress"]
    if recorded_slug:
        # A named run: the directory is <slug>--<name> and the state file keeps the
        # slug, which is the run's identity everywhere else (issue #87 D1).
        lines.append(f"slug: {recorded_slug}")
    if stop_reason:
        lines.append(f"stop_reason: {stop_reason}")
    (path / "state.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_untracked_mirror_runs_are_invisible(platform_root):
    """A colleague's blocked run must never show up as needing your input."""
    config = cfg.load()
    _plant_mirror_run(config, "someone-elses-run", project="grizz/home",
                      stop_reason="question")

    payload = api.build_state(config)

    assert payload["totals"] == {"runs": 0, "live": 0, "attention": 0}
    assert payload["runs"] == []
    assert "hint" in payload, "an empty view should explain why it is empty"


def test_tracked_run_appears_with_its_state(platform_root):
    from lmer_platform import runs as run_index

    config = cfg.load()
    _plant_mirror_run(config, "mine", stop_reason="question")
    _plant_mirror_run(config, "theirs", project="grizz/home", stop_reason="question")
    run_index.track("gitlab.example.com", "agents/global", "mine", taskdef="develop")

    payload = api.build_state(config)

    assert payload["totals"]["runs"] == 1
    assert payload["runs"][0]["slug"] == "mine"
    assert payload["runs"][0]["state"] == "waiting_on_you"
    assert payload["totals"]["attention"] == 1
    assert "hint" not in payload


def test_tracked_run_missing_from_mirror_still_shows(platform_root):
    """Tracked but not yet pushed: a row beats an unexplained absence."""
    from lmer_platform import runs as run_index

    run_index.track("gitlab.example.com", "agents/global", "not-pushed-yet",
                    taskdef="develop", target="issue-141")
    payload = api.build_state(cfg.load())

    assert payload["totals"]["runs"] == 1
    assert payload["runs"][0]["slug"] == "not-pushed-yet"
    assert payload["runs"][0]["taskdef"] == "develop"


def test_tracked_block_reports_the_index(platform_root):
    from lmer_platform import runs as run_index

    run_index.track("gitlab.example.com", "agents/global", "a", source="adopted")
    payload = api.build_state(cfg.load())

    assert payload["tracked"]["count"] == 1
    assert payload["tracked"]["runs"][0]["source"] == "adopted"


def test_candidates_lists_everyone_and_flags_tracked(client, platform_root, config):
    from lmer_platform import runs as run_index

    _plant_mirror_run(config, "mine")
    _plant_mirror_run(config, "theirs", project="grizz/home")
    run_index.track("gitlab.example.com", "agents/global", "mine")

    payload = client.get("/api/runs/candidates", headers=bearer_header()).json()
    by_slug = {c["slug"]: c for c in payload["candidates"]}

    assert set(by_slug) == {"mine", "theirs"}
    assert by_slug["mine"]["tracked"] is True
    assert by_slug["theirs"]["tracked"] is False
    assert "other people" in payload["note"]


def test_candidates_flag_a_tracked_named_run_as_tracked(client, platform_root, config):
    """The picker flags by ``(host, project, slug)`` and a named run's directory is
    not its slug (T90), so keying candidates on the directory name offered a run
    this orchestrator already tracks — and the UI shows untracked candidates only,
    so adopting it again was the obvious thing to do."""
    from lmer_platform import runs as run_index

    _plant_mirror_run(config, "mine--nice-name", recorded_slug="mine")
    run_index.track("gitlab.example.com", "agents/global", "mine")

    payload = client.get("/api/runs/candidates", headers=bearer_header()).json()
    by_slug = {c["slug"]: c for c in payload["candidates"]}

    assert set(by_slug) == {"mine"}, "one directory, one candidate"
    assert by_slug["mine"]["tracked"] is True
    assert by_slug["mine"]["rel_path"].endswith("runs/mine--nice-name"), (
        "the address is still the directory an operator can open"
    )


def test_adopt_then_forget_over_the_api(config, platform_root):
    """Needs the real state builder, so the tracked count actually reflects it."""
    real_client = TestClient(api.create_app(config, SECRET, state_builder=api.build_state))
    body = {"host": "gitlab.example.com", "project": "agents/global", "slug": "adopted-run"}

    adopted = real_client.post(
        "/api/runs/adopt", headers=bearer_header(), json=body
    ).json()
    assert adopted["tracked"]["source"] == "adopted"

    state = real_client.get("/api/state", headers=bearer_header()).json()
    assert state["tracked"]["count"] == 1

    forgotten = real_client.post(
        "/api/runs/forget", headers=bearer_header(), json=body
    ).json()
    assert forgotten["forgotten"] is True
    after = real_client.get("/api/state", headers=bearer_header()).json()
    assert after["tracked"]["count"] == 0


def test_adopt_rejects_incomplete_bodies(client, platform_root):
    response = client.post(
        "/api/runs/adopt", headers=bearer_header(), json={"host": "h"}
    )
    assert response.status_code == 400


def test_forget_untracked_reports_false(client, platform_root):
    response = client.post(
        "/api/runs/forget", headers=bearer_header(),
        json={"host": "h", "project": "p", "slug": "s"},
    )
    assert response.json() == {"forgotten": False}


# --- spawning over the API (T8) ---------------------------------------------

def test_spawn_endpoint_creates_a_session(client, platform_root, monkeypatch):
    from lmer_platform import spawn as spawn_mod

    captured = {}

    def fake_spawn(config, request, kind="worker"):
        captured["request"] = request
        return spawn_mod.SpawnResult(
            session_id="s-1", pid=4242,
            log_path=platform_root / "logs" / "s-1.log",
            host="gitlab.example.com", project="agents/global", slug="develop-1",
            command=["lmer", "develop", "t"],
        )

    monkeypatch.setattr(api, "spawn_session", fake_spawn)
    response = client.post(
        "/api/sessions", headers=bearer_header(),
        json={"taskdef": "develop", "target": "t", "ports": 2},
    )

    assert response.status_code == 201
    assert response.json()["session_id"] == "s-1"
    assert response.json()["run"]["slug"] == "develop-1"
    assert captured["request"].ports == 2


def test_spawn_at_capacity_returns_429(client, platform_root, monkeypatch):
    from lmer_platform import spawn as spawn_mod

    def at_cap(config, request, kind="worker"):
        raise spawn_mod.CapacityError("concurrency cap reached: 4/4 sessions")

    monkeypatch.setattr(api, "spawn_session", at_cap)
    response = client.post(
        "/api/sessions", headers=bearer_header(),
        json={"taskdef": "develop", "target": "t"},
    )
    assert response.status_code == 429
    assert "4/4" in response.json()["detail"]


def test_spawning_a_run_that_already_has_a_session_returns_409(
    client, platform_root, monkeypatch
):
    """This route derives a run identity like the answer and resume routes do.

    It carried no live-run check of its own, so the same taskdef and target were all
    a caller needed to start a second session for one run — the hole the invariant in
    ``spawn_session`` closes. 409 and not 400: the request is well formed and works
    once that session stops.
    """
    from lmer_platform import spawn as spawn_mod

    def already_live(config, request, kind="worker"):
        raise spawn_mod.RunAlreadyLive(
            "gitlab.example.com/agents/global/develop-1 already has a live session "
            "(s-7, pid 4242)"
        )

    monkeypatch.setattr(api, "spawn_session", already_live)
    response = client.post(
        "/api/sessions", headers=bearer_header(),
        json={"taskdef": "develop", "target": "t"},
    )
    assert response.status_code == 409
    assert "s-7" in response.json()["detail"], (
        "the caller has to be told which session holds the run"
    )


def test_spawn_with_a_bad_request_returns_400(client, platform_root):
    response = client.post(
        "/api/sessions", headers=bearer_header(), json={"taskdef": "develop"}
    )
    assert response.status_code == 400
    assert "target is required" in response.json()["detail"]


def test_spawn_requires_auth(client):
    assert client.post("/api/sessions", json={}).status_code == 401


@pytest.mark.parametrize("path", ["/api/runs/candidates"])
def test_run_index_routes_require_auth(client, path):
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", ["/api/runs/adopt", "/api/runs/forget"])
def test_run_index_post_routes_require_auth(client, path):
    assert client.post(path, json={}).status_code == 401


# --- answering a question-blocked run (T19) ---------------------------------
#
# The refusals and the argv live in tests/test_platform_answer.py; what belongs
# here is the HTTP surface — the route is gated like every other one, the statuses
# on lmer_platform.answer's exceptions actually reach the client, and the answer
# text does not come back in the reply.

ANSWER_BODY = {
    "host": "gitlab.example.com",
    "project": "agents/global",
    "slug": "develop-1",
    "answer": "yes, start empty",
}


@pytest.fixture
def answered(monkeypatch, platform_root):
    """Capture what the route hands lmer_platform.answer, without a spawn."""
    from lmer_platform import answer as answer_mod
    from lmer_platform import spawn as spawn_mod

    calls = []

    def fake_answer(config, request):
        calls.append(request)
        return answer_mod.AnswerResult(
            host=request.host,
            project=request.project,
            slug=request.slug,
            question="Should the queue survive a restart?",
            session=spawn_mod.SpawnResult(
                session_id="s-answer", pid=4243,
                log_path=platform_root / "logs" / "s-answer.log",
                host=request.host, project=request.project, slug=request.slug,
                command=["lmer", "develop", "t", f"--answer={request.answer}"],
                control_port=8711,
            ),
        )

    fake_answer.calls = calls
    monkeypatch.setattr(api, "answer_run", fake_answer)
    return fake_answer


def test_answering_a_run_starts_a_session(client, answered):
    response = client.post(
        "/api/runs/answer", headers=bearer_header(), json=ANSWER_BODY
    )

    assert response.status_code == 202, (
        "nothing is recorded yet when this returns — the session applies the "
        "answer at its own start"
    )
    payload = response.json()
    assert payload["session"]["session_id"] == "s-answer"
    assert payload["run"]["slug"] == "develop-1"
    assert answered.calls[0].answer == "yes, start empty"


def test_the_answer_does_not_come_back_in_the_reply(client, answered):
    """The spawn's argv carries --answer=<text>, so the reply must not carry it."""
    response = client.post(
        "/api/runs/answer", headers=bearer_header(), json=ANSWER_BODY
    )
    assert ANSWER_BODY["answer"] not in response.text
    assert "command" not in response.text


def test_answer_refusals_keep_their_status(client, platform_root, monkeypatch):
    from lmer_platform import answer as answer_mod

    for error, status in (
        (answer_mod.AnswerError("answer is empty"), 400),
        (answer_mod.RunNotTracked("not tracked"), 404),
        (answer_mod.NotAnswerable("already has a live session"), 409),
    ):
        def refuse(config, request, exc=error):
            raise exc

        monkeypatch.setattr(api, "answer_run", refuse)
        response = client.post(
            "/api/runs/answer", headers=bearer_header(), json=ANSWER_BODY
        )
        assert response.status_code == status, str(error)
        assert str(error) in response.json()["detail"]


def test_answering_at_capacity_returns_429(client, platform_root, monkeypatch):
    from lmer_platform import spawn as spawn_mod

    def at_cap(config, request):
        raise spawn_mod.CapacityError("concurrency cap reached: 4/4 sessions")

    monkeypatch.setattr(api, "answer_run", at_cap)
    response = client.post(
        "/api/runs/answer", headers=bearer_header(), json=ANSWER_BODY
    )
    assert response.status_code == 429
    assert "4/4" in response.json()["detail"]


def test_an_answer_the_spawn_says_is_already_live_is_a_409(
    client, platform_root, monkeypatch
):
    """The same 409 ``NotAnswerable`` gives this route, for the residual case.

    The answer path checks the run's recorded identity before it reads the mirror;
    the invariant in ``spawn_session`` checks the identity the spawn is about to
    register. When the second one is what fires, the operator must still be told the
    answer was not delivered and which session has the run — not handed a 400.
    """
    from lmer_platform import spawn as spawn_mod

    def already_live(config, request):
        raise spawn_mod.RunAlreadyLive(
            "gitlab.example.com/agents/global/develop-1 already has a live session "
            "(s-7, pid 4242)"
        )

    monkeypatch.setattr(api, "answer_run", already_live)
    response = client.post(
        "/api/runs/answer", headers=bearer_header(), json=ANSWER_BODY
    )
    assert response.status_code == 409
    assert "s-7" in response.json()["detail"]


def test_an_unspawnable_answer_is_a_400(client, platform_root, monkeypatch):
    from lmer_platform import spawn as spawn_mod

    def unspawnable(config, request):
        raise spawn_mod.SpawnError("cannot find the `lmer` executable on PATH")

    monkeypatch.setattr(api, "answer_run", unspawnable)
    response = client.post(
        "/api/runs/answer", headers=bearer_header(), json=ANSWER_BODY
    )
    assert response.status_code == 400
    assert "lmer" in response.json()["detail"]


def test_answer_requires_auth(client, answered):
    assert client.post("/api/runs/answer", json=ANSWER_BODY).status_code == 401
    assert answered.calls == [], "an unauthenticated request must not reach the spawn"


def test_answering_an_untracked_run_over_the_api_is_a_404(config, platform_root):
    """End to end through the real module: nothing is tracked, so nothing to answer."""
    real_client = TestClient(
        api.create_app(config, SECRET, state_builder=api.build_state)
    )
    response = real_client.post(
        "/api/runs/answer", headers=bearer_header(), json=ANSWER_BODY
    )
    assert response.status_code == 404
    assert "not tracked" in response.json()["detail"]


def test_route_list_mentions_answering(client):
    body = client.get("/api", headers=bearer_header()).text
    assert "/api/runs/answer" in body


# --- one session's terminal (T16) -------------------------------------------
#
# The behaviour lives in tests/test_platform_session_io.py; what belongs here is
# the API surface — that the new routes are gated like every other one, and that
# the route list still tells an operator they exist.

def test_session_log_route_requires_auth(client):
    assert client.get("/api/sessions/s-1/log").status_code == 401


@pytest.mark.parametrize("path", [
    "/api/sessions/s-1/input",
    "/api/sessions/s-1/tty-ticket",
])
def test_session_io_post_routes_require_auth(client, path):
    assert client.post(path, json={"data": "x"}).status_code == 401


def test_the_tty_socket_takes_no_shared_secret(client, platform_root):
    """It is ticket-only, so an authenticated handshake must still be refused."""
    from starlette.websockets import WebSocketDisconnect

    registry.register("s-1", pid=__import__("os").getpid())
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/sessions/s-1/tty", headers=bearer_header()
        ):
            pass


def test_route_list_mentions_the_terminal_routes(client):
    body = client.get("/api", headers=bearer_header()).text
    assert "/api/sessions/{id}/log" in body
    assert "/api/sessions/{id}/tty-ticket" in body
    assert "ticket" in body


# --- serving the SPA (T11) --------------------------------------------------

@pytest.fixture
def built_ui(tmp_path, monkeypatch):
    """A stand-in for a built bundle."""
    from lmer_platform import api as api_mod

    dist = tmp_path / "web" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>lmer platform ui</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log('app')", encoding="utf-8")
    (dist / "assets" / "index.css").write_text("body{}", encoding="utf-8")
    monkeypatch.setattr(api_mod, "dist_dir", lambda: dist)
    return dist


@pytest.fixture
def unbuilt_ui(monkeypatch):
    from lmer_platform import api as api_mod

    monkeypatch.setattr(api_mod, "dist_dir", lambda: None)


def test_root_serves_the_built_ui(client, built_ui):
    response = client.get("/", headers=bearer_header())
    assert response.status_code == 200
    assert "lmer platform ui" in response.text


def test_built_ui_is_not_cached(client, built_ui):
    """Asset filenames are stable across builds, so a cached index goes stale."""
    response = client.get("/", headers=bearer_header())
    assert response.headers["cache-control"] == "no-store"


def test_assets_are_served(client, built_ui):
    response = client.get("/assets/app.js", headers=bearer_header())
    assert response.status_code == 200
    assert "console.log" in response.text


def test_root_explains_how_to_build_when_unbuilt(client, unbuilt_ui):
    response = client.get("/", headers=bearer_header())
    assert response.status_code == 200
    assert "lmer platform setup-ui" in response.text
    assert "/api/state" in response.text


def test_unbuilt_assets_are_404_not_a_traceback(client, unbuilt_ui):
    assert client.get("/assets/app.js", headers=bearer_header()).status_code == 404


def test_ui_requires_the_secret(client, built_ui):
    """An unauthenticated visit must not leak even a shell of the UI."""
    response = client.get("/")
    assert response.status_code == 401
    assert "lmer platform ui" not in response.text
    assert response.headers["www-authenticate"] == f'Basic realm="{api.REALM}"'


def test_assets_require_the_secret(client, built_ui):
    assert client.get("/assets/app.js").status_code == 401


@pytest.mark.parametrize("path", [
    "../index.html",
    "../../../../etc/passwd",
    "nested/../../secret",
])
def test_asset_traversal_is_refused(client, built_ui, path):
    """The daemon is reachable from a phone on a LAN; traversal here is fatal."""
    response = client.get(f"/assets/{path}", headers=bearer_header())
    assert response.status_code in (404, 400), response.text
    assert "passwd" not in response.text


def test_asset_symlink_escaping_the_bundle_is_refused(client, built_ui, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (built_ui / "assets" / "link.txt").symlink_to(outside)

    response = client.get("/assets/link.txt", headers=bearer_header())
    assert response.status_code == 404
    assert "secret" not in response.text


def test_missing_asset_is_404(client, built_ui):
    assert client.get("/assets/nope.js", headers=bearer_header()).status_code == 404


def test_api_index_still_lists_routes(client, built_ui):
    """The UI takes over `/`, so the plain-text route list moves to /api."""
    body = client.get("/api", headers=bearer_header()).text
    assert "/api/state" in body
    assert "/api/sessions" in body


# --- compression (T20) ------------------------------------------------------

def test_ui_bundle_is_served_compressed(client, built_ui):
    """~550 kB of Vuetify CSS is a phone's cold load; ~130 kB gzipped is not."""
    big = "x" * 4000
    (built_ui / "assets" / "big.css").write_text(big, encoding="utf-8")

    response = client.get(
        "/assets/big.css",
        headers={**bearer_header(), "Accept-Encoding": "gzip"},
    )
    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "gzip"
    assert response.text == big, "compression must be transparent to the content"


def test_small_responses_are_not_compressed(client):
    """Below the threshold the CPU is not worth the handful of bytes."""
    response = client.get(
        "/api/health", headers={**bearer_header(), "Accept-Encoding": "gzip"}
    )
    assert response.status_code == 200
    assert "content-encoding" not in response.headers


def test_a_client_that_does_not_ask_gets_plain_bytes(client, built_ui):
    response = client.get(
        "/", headers={**bearer_header(), "Accept-Encoding": "identity"}
    )
    assert response.status_code == 200
    assert "content-encoding" not in response.headers


def test_asset_symlink_loop_is_a_404_not_a_500(client, built_ui):
    """On 3.12 a symlink loop makes resolve() raise RuntimeError, not OSError.

    Catching only OSError turned a traversal attempt into a 500 — found by a
    mutation test against the identical pattern in transcripts.py.
    """
    loop = built_ui / "assets" / "loop"
    loop.symlink_to(loop)

    response = client.get("/assets/loop", headers=bearer_header())
    assert response.status_code == 404
