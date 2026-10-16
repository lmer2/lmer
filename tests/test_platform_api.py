"""Tests for the platform HTTP control plane (issue #141, slice M1 / T6).

Auth is the bulk of it: every route is gated, both schemes work, and a browser
gets prompted rather than a bare 401. Plus the guarantees the payload has to make
— no secret in a response, no credentialed URL, and stale sessions preserved so
crashed runs survive into the attention list.
"""

import asyncio
import base64
import json
import os

import pytest
import yaml
from fastapi.testclient import TestClient

from lmer_platform import api, connectors
from lmer_platform import config as cfg
from lmer_platform import registry, spawn, store
from tests.conftest import strip_lmer_env

# The taskdef-tier fixtures, imported rather than copied — the same trade
# ``tests.test_platform_run_files`` makes with the panel parser. Planting a
# taskdef and blinding the host's own candidates are the arrangement T73's tests
# established, and a second copy would drift from the one the search is
# described by.
from tests.test_platform_addrun import blind_the_host, plant_taskdefs
from starlette.datastructures import State

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


def test_state_names_the_live_sessions_container(config, platform_root):
    """#444: what the launching lmer reported at create reaches the session
    block as ``container_name``; the id stays host-side."""
    registry.register(
        "s-20261009T152516-ff2b", pid=os.getpid(),
        run={"host": "gitlab.example.com", "project": "agents/global", "slug": "chat-mr-389"},
        task={"taskdef": "chat", "target": "mr-389"},
    )
    facts = spawn.ports_file_for("s-20261009T152516-ff2b")
    facts.parent.mkdir(parents=True, exist_ok=True)
    facts.write_text(json.dumps(
        {"container": {"id": "9f3ca1b2c3d4e5", "name": "lmer-chat-mr-389-ff2b"}}
    ))

    app = api.create_app(config, SECRET, state_builder=api.build_state)
    payload = TestClient(app).get("/api/state", headers=bearer_header()).json()

    session = payload["runs"][0]["session"]
    assert session["container_name"] == "lmer-chat-mr-389-ff2b"
    assert "container_id" not in session
    assert "9f3ca1b2c3d4e5" not in json.dumps(payload)
    entry = registry.read_session("s-20261009T152516-ff2b")
    assert (entry["container_id"], entry["container_name"]) == (
        "9f3ca1b2c3d4e5", "lmer-chat-mr-389-ff2b",
    )


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
    _plant_mirror_run(config, "someone-elses-run", project="colleague/home",
                      stop_reason="question")

    payload = api.build_state(config)

    assert payload["totals"] == {"runs": 0, "live": 0, "attention": 0}
    assert payload["runs"] == []
    assert "hint" in payload, "an empty view should explain why it is empty"


def test_tracked_run_appears_with_its_state(platform_root):
    from lmer_platform import runs as run_index

    config = cfg.load()
    _plant_mirror_run(config, "mine", stop_reason="question")
    _plant_mirror_run(config, "theirs", project="colleague/home", stop_reason="question")
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
    _plant_mirror_run(config, "theirs", project="colleague/home")
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


# --- one directory, one identity key (issue #353, D8) ------------------------
#
# A project with ``remotes:`` is reachable at more than one URL and the CONTAINER
# places its record across the registry's UNION, so a launch through the
# secondary writes under the same short name as one through the primary. The
# picker compares the mirror WALK's key — which is the project's ``repo:`` — with
# the tracked index's, so an identity taken from the launch URL verbatim gave one
# directory two keys and the picker offered a run this orchestrator had spawned
# itself. Adopting it, which is what the picker invites, wrote a SECOND index
# entry for that one directory: two rows, two titles, two check-in marks.
#
# ``spawn.derive_run_identity`` folds the launch URL to the project's primary
# remote, so the two halves agree by construction. Both surfaces are tested from
# the identity the platform itself derives rather than a hand-written pair — the
# ``lmer platform runs --candidates`` twin in ``lmer_platform.daemon`` reads the
# same walk and the same index, so it is fixed by the same fold.

REGISTERED_PRIMARY = "gitlab.example.com/acme/widgets"
REGISTERED_SECONDARY = "github.com/acme/widgets"
SECONDARY_URL = "https://github.com/acme/widgets.git"
WIDGETS_TARGET = "https://gitlab.example.com/acme/widgets/-/work_items/42"
WIDGETS_SESSION_ID = "20260824-165230-2-31245"


def _register_widgets(config):
    """A mirror whose manifest knows ``widgets`` at both of its remotes (D8)."""
    config.mirror_path.mkdir(parents=True, exist_ok=True)
    (config.mirror_path / "_lmer.yaml").write_text(
        yaml.safe_dump({
            "schema": 1,
            "name": "example-plexus",
            "projects": {"widgets": {
                "repo": REGISTERED_PRIMARY, "remotes": [REGISTERED_SECONDARY],
            }},
        }),
        encoding="utf-8",
    )


def _plant_registered_run(config, slug):
    """One schema-form session record under the project's SHORT NAME (D7/D22) —
    where the container puts it whichever remote the launch used."""
    path = config.mirror_path / "widgets" / "_runs" / slug / WIDGETS_SESSION_ID
    path.mkdir(parents=True, exist_ok=True)
    (path / "state.yaml").write_text(
        f"schema: 1\nslug: {slug}\nstatus: in-progress\n", encoding="utf-8"
    )
    return path


def _identity_of_a_launch_through_the_mirror(config):
    """The ``(host, project)`` a spawn through the SECONDARY remote tracks under.

    Read out of the platform's own prediction rather than spelled out here: a
    hand-written pair would let the picker agree with a key no spawn derives.
    """
    from lmer_platform import spawn

    request = spawn.SpawnRequest(
        taskdef="review", target=WIDGETS_TARGET, repo_url=SECONDARY_URL
    )
    host, project, _slug = spawn.derive_run_identity(request, SECONDARY_URL, config)
    return host, project


def test_a_run_spawned_through_a_secondary_remote_is_not_offered_for_adoption(
    client, platform_root, config
):
    """The picker lists what is NOT tracked, so a run this orchestrator started
    itself appearing there is an invitation to adopt a run it already has."""
    from lmer_platform import runs as run_index

    _register_widgets(config)
    _plant_registered_run(config, "review-mr-1")
    host, project = _identity_of_a_launch_through_the_mirror(config)
    run_index.track(host, project, "review-mr-1")

    payload = client.get("/api/runs/candidates", headers=bearer_header()).json()
    by_slug = {c["slug"]: c for c in payload["candidates"]}

    assert set(by_slug) == {"review-mr-1"}, "one directory, one candidate"
    assert by_slug["review-mr-1"]["tracked"] is True


def test_a_run_adopted_under_a_secondary_spelling_is_not_offered_again(
    client, platform_root, config
):
    """The row the fold cannot reach, and used to be re-offered forever
    (!269 review).

    `POST /api/runs/adopt` takes host and project verbatim with no registry
    validation, and the secondary is the URL an operator actually cloned from
    — so it is the spelling they would type. The walk keys every directory on
    the PRIMARY, so comparing raw pairs read that as "untracked" and invited a
    second row for a directory that already had one: the defect item 15 exists
    to close, reappearing one level up.
    """
    from lmer_platform import runs as run_index

    _register_widgets(config)
    _plant_registered_run(config, "review-mr-1")
    secondary_host, secondary_project = REGISTERED_SECONDARY.split("/", 1)
    run_index.track(secondary_host, secondary_project, "review-mr-1",
                    source="adopted")

    payload = client.get("/api/runs/candidates", headers=bearer_header()).json()
    by_slug = {c["slug"]: c for c in payload["candidates"]}

    assert by_slug["review-mr-1"]["tracked"] is True, (
        "a directory already tracked under one of its own remotes was offered "
        "for adoption again"
    )


def test_a_genuinely_untracked_run_is_still_offered(
    client, platform_root, config
):
    """The converse, so folding both sides of the membership test is not a
    widening: a directory nothing tracks is still a candidate."""
    _register_widgets(config)
    _plant_registered_run(config, "review-mr-2")

    payload = client.get("/api/runs/candidates", headers=bearer_header()).json()
    by_slug = {c["slug"]: c for c in payload["candidates"]}

    assert by_slug["review-mr-2"]["tracked"] is False


def test_adopting_a_candidate_cannot_double_a_run_the_platform_spawned(
    client, platform_root, config
):
    """The damage the picker's offer leads to: one directory, two index entries,
    each with its own title and check-in marks and neither aware of the other."""
    from lmer_platform import runs as run_index

    _register_widgets(config)
    _plant_registered_run(config, "review-mr-1")
    host, project = _identity_of_a_launch_through_the_mirror(config)
    run_index.track(host, project, "review-mr-1")

    payload = client.get("/api/runs/candidates", headers=bearer_header()).json()
    offered = payload["candidates"][0]
    client.post("/api/runs/adopt", headers=bearer_header(), json={
        "host": offered["host"], "project": offered["project"],
        "slug": offered["slug"],
    })

    tracked = run_index.list_tracked()
    assert len(tracked) == 1, (
        "one run directory became two rows in the fleet view: "
        f"{[t.key for t in tracked]}"
    )
    # Re-tracking preserves the first sighting (``runs.track``), so the surviving
    # row is still the spawn's — adopting a run one already has is a no-op, which
    # is what makes the picker's offer harmless even when it is made.
    assert tracked[0].source == "spawned"


def test_adopt_then_forget_over_the_api(config, platform_root, monkeypatch):
    """Needs the real state builder, so the tracked count actually reflects it."""
    monkeypatch.setattr(connectors, "_CONNECTORS", None)
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
    assert "target or a repo_url" in response.json()["detail"]


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


def test_the_answer_route_passes_a_repo_url_to_the_request(client, answered):
    """`repo_url_required`: an adopted plain-project run is answered with the
    transport URL the client now sends."""
    response = client.post(
        "/api/runs/answer",
        headers=bearer_header(),
        json={**ANSWER_BODY, "repo_url": "https://gitlab.example.com/example/repo.git"},
    )
    assert response.status_code == 202
    assert answered.calls[0].repo_url == "https://gitlab.example.com/example/repo.git"


def test_answer_confirmation_is_passed_as_the_exact_source_uid(client, answered):
    source_uid = "a" * 32
    response = client.post(
        "/api/runs/answer",
        headers=bearer_header(),
        json={**ANSWER_BODY, "allow_unsigned_source": source_uid},
    )
    assert response.status_code == 202
    assert answered.calls[0].allow_unsigned_source == source_uid


def test_answer_unsigned_refusal_keeps_structured_provenance(
    client, platform_root, monkeypatch
):
    from lmer_platform import resume as resume_mod

    source = {
        "uid": "a" * 32,
        "operator": "alice",
        "hostname": "build.example",
        "started_at": "2026-09-17T12:00:00Z",
        "last_activity": "2026-09-17T12:05:00Z",
    }

    def refuse(config, request):
        raise resume_mod.UnsignedSessionSource("source has no sign-out", source)

    monkeypatch.setattr(api, "answer_run", refuse)
    response = client.post(
        "/api/runs/answer", headers=bearer_header(), json=ANSWER_BODY
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "unsigned_session",
        "message": "source has no sign-out",
        "source": source,
        "decisions": ["continue_fresh", "cancel"],
    }


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


#: A pid nothing can be running under, so the session reads as crashed and the
#: follower ends instead of polling. Same value the rest of the platform tests use.
DEAD_PID = 2**22


class _StubSocket:
    """The tty endpoint's client, cut down to what the handler touches.

    ``close`` raises whatever *failure* is, which is how a client that went away
    is reproduced without a socket to hang up: nothing else about the connection
    is different, and the handler reaches the close either way.
    """

    def __init__(self, session_id, ticket, failure=None):
        self.state = State()
        self.path_params = {"session_id": session_id}
        self.query_params = {"ticket": ticket}
        self.failure = failure
        self.sent = []
        self.closes = 0

    async def accept(self):
        return None

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive(self):
        return {"type": "websocket.disconnect"}

    async def close(self, code=1000, reason=""):
        self.closes += 1
        if self.failure is not None:
            raise self.failure


def open_tty_with(client, socket_factory):
    """Run the tty handler itself against a stub socket.

    The route rather than a copy of its last two lines: what #247 is about is
    whether anything escapes *the endpoint*, and the ticket is redeemed against
    the same app's store so the handler runs the whole way through.
    """
    endpoint = next(
        route.endpoint for route in client.app.routes
        if getattr(route, "path", None) == "/api/sessions/{session_id}/tty"
    )
    minted = client.post("/api/sessions/s-1/tty-ticket", headers=bearer_header())
    assert minted.status_code == 200, minted.text
    socket = socket_factory(minted.json()["ticket"])
    asyncio.run(endpoint(socket))
    return socket


def test_a_client_that_left_before_the_close_is_not_an_error(client, platform_root):
    """Every browser tab closed on a terminal logged an ASGI traceback (#247).

    starlette turns the hung-up transport's ``OSError`` — uvicorn's
    ``ClientDisconnected`` — into ``WebSocketDisconnect(1006)`` on a socket its
    own state still calls connected, so the ``RuntimeError`` guard, which is the
    already-closed state, never saw the common case.
    """
    from starlette.websockets import WebSocketDisconnect

    registry.register("s-1", pid=DEAD_PID)
    socket = open_tty_with(
        client, lambda ticket: _StubSocket("s-1", ticket, WebSocketDisconnect(1006))
    )

    assert socket.closes == 1, "the handler must still try to close"
    assert socket.sent[0]["type"] == "open", "the socket was served before it left"


def test_closing_an_already_closed_socket_stays_quiet(client, platform_root):
    """The state that was handled, still handled: starlette raises this one when
    a close frame follows a close frame."""
    registry.register("s-1", pid=DEAD_PID)
    failure = RuntimeError('Cannot call "send" once a close message has been sent.')
    socket = open_tty_with(client, lambda ticket: _StubSocket("s-1", ticket, failure))

    assert socket.closes == 1


def test_a_close_that_fails_for_another_reason_is_not_swallowed():
    """Two named exceptions, not a bare ``except``: a socket failing some other
    way is a bug in this daemon, and suppressing it would only hide the next one."""
    socket = _StubSocket("s-1", "unused", ValueError("nothing to do with the peer"))

    with pytest.raises(ValueError):
        asyncio.run(api._close_quietly(socket))


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


# --- service slots (issue #245) ----------------------------------------------

# ``slot_host`` and its presets come from tests/conftest.py — one fake for
# every module, with the real resolve_container's keyword-only signature.

def _declare_slots(entries):
    store.write_json(cfg.config_path(), {"slots": entries})


def test_build_state_carries_a_row_per_declared_slot(platform_root, slot_host):
    _declare_slots([
        {"name": "webapp", "preset": "webapp_dev", "description": "Web app dev"},
        {"name": "plain", "preset": "no_service"},
    ])

    payload = api.build_state(cfg.load())

    assert [row["name"] for row in payload["slots"]] == ["webapp", "plain"]
    assert payload["slots"][0] == {
        "name": "webapp", "preset": "webapp_dev", "description": "Web app dev",
        # The version an editor sends back as If-Match (#256); pinned in
        # tests/test_platform_slots_api.py rather than spelled out here.
        "etag": payload["slots"][0]["etag"],
        "applicable_projects": [], "needs_project_assignment": True,
        "service": "webapp-web", "service_group": None, "members": [],
        "state": "free", "reason": None,
        "occupant": None, "occupants": [], "service_occupants": [],
    }
    assert payload["slots"][1]["state"] == "misconfigured"
    assert "sets no service" in payload["slots"][1]["reason"]


def test_build_state_has_an_empty_slots_list_when_none_are_declared(
    platform_root, slot_host
):
    """A host that declares none is the ordinary case, not a missing key."""
    assert api.build_state(cfg.load())["slots"] == []


def test_an_occupied_row_names_the_run_holding_it(platform_root, slot_host):
    import os

    _declare_slots([{"name": "webapp", "preset": "webapp_dev"}])
    registry.register(
        "s-holder", pid=os.getpid(), slot="webapp",
        run={"host": "gitlab.example.com", "project": "agents/global",
             "slug": "develop-1"},
    )

    row = api.build_state(cfg.load())["slots"][0]

    assert row["state"] == "occupied"
    assert row["occupant"] == {
        "session_id": "s-holder", "host": "gitlab.example.com",
        "project": "agents/global", "slug": "develop-1",
    }


def test_a_crashed_session_does_not_hold_a_slot_in_the_payload(
    platform_root, slot_host
):
    """The payload reads sessions with ``live_only=False`` so crashed runs stay
    in the attention list — a dead entry must still not hold a slot."""
    _declare_slots([{"name": "webapp", "preset": "webapp_dev"}])
    registry.register(
        "s-dead", pid=2**22, slot="webapp",
        run={"host": "gitlab.example.com", "project": "agents/global",
             "slug": "develop-2"},
    )

    payload = api.build_state(cfg.load())

    assert payload["slots"][0]["state"] == "free"
    # …and the crash is still visible, which is why the list is unfiltered.
    assert payload["totals"]["runs"] == 1


def test_the_spawn_route_passes_the_slot_through(client, platform_root, monkeypatch):
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
        json={"taskdef": "develop", "target": "t", "slot": "webapp"},
    )

    assert response.status_code == 201
    assert captured["request"].slot == "webapp"


def test_spawning_into_an_occupied_slot_returns_409(
    client, platform_root, monkeypatch
):
    """Well formed, and it works unchanged once the holder stops."""
    from lmer_platform import spawn as spawn_mod

    def occupied(config, request, kind="worker"):
        raise spawn_mod.SlotOccupied(
            "service slot 'webapp' is already held by session s-7"
        )

    monkeypatch.setattr(api, "spawn_session", occupied)
    response = client.post(
        "/api/sessions", headers=bearer_header(),
        json={"taskdef": "develop", "target": "t", "slot": "webapp"},
    )

    assert response.status_code == 409
    assert "s-7" in response.json()["detail"]


@pytest.mark.parametrize("detail", [
    "unknown service slot 'wbeapp'",
    "service slot 'webapp' is not usable: preset 'typo' is not defined on this host",
])
def test_an_unknown_or_unusable_slot_returns_400(
    client, platform_root, monkeypatch, detail
):
    """400 rather than 409: no amount of waiting fixes either one."""
    from lmer_platform import spawn as spawn_mod

    def refused(config, request, kind="worker"):
        raise spawn_mod.SpawnError(detail)

    monkeypatch.setattr(api, "spawn_session", refused)
    response = client.post(
        "/api/sessions", headers=bearer_header(),
        json={"taskdef": "develop", "target": "t", "slot": "webapp"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == detail


def test_the_payload_names_every_holder_of_a_contended_slot(
    platform_root, slot_host
):
    """The spawn gate's race is disclosed, not prevented — so the surface built
    to answer "who has my dev service" has to report both holders rather than
    name one and look settled."""
    import os

    _declare_slots([{"name": "webapp", "preset": "webapp_dev"}])
    for session_id, slug in (("s-one", "develop-1"), ("s-two", "develop-2")):
        registry.register(
            session_id, pid=os.getpid(), slot="webapp",
            run={"host": "gitlab.example.com", "project": "acme/app",
                 "slug": slug},
        )

    row = api.build_state(cfg.load())["slots"][0]

    assert row["state"] == "occupied"
    assert [h["session_id"] for h in row["occupants"]] == ["s-one", "s-two"]
    # The single-holder key stays, so an older client still reads something true.
    assert row["occupant"]["session_id"] == "s-one"


def test_two_slots_on_one_service_are_not_both_offered_in_the_payload(
    platform_root, slot_host
):
    """Name-keyed exclusion let every slot resolving to one service read free."""
    _declare_slots([
        {"name": "a", "preset": "webapp_dev"},
        {"name": "b", "preset": "webapp_alt"},
    ])

    rows = {row["name"]: row for row in api.build_state(cfg.load())["slots"]}

    assert rows["a"]["state"] == "free"
    assert rows["b"]["state"] == "misconfigured"
    assert "already bound by slot 'a'" in rows["b"]["reason"]


# --- the fleet row carries its check-in facts (issue #244) --------------------
#
# The operator's half of the check-in work: the daemon tells the orchestrator
# which runs have gone unchecked, and the same staleness rides on the row so the
# fleet view can show it without anyone asking the orchestrator. What matters
# here is that the row and the digest cannot disagree — same window, same
# eligibility, one computation in lmer_platform.checkin — and that a fleet poll
# stays a read.

def test_every_row_carries_a_check_in_block(platform_root):
    import os

    registry.register(
        "s-1", pid=os.getpid(),
        run={"host": "gitlab.example.com", "project": "agents/global",
             "slug": "develop-1"},
    )
    payload = api.build_state(cfg.load())

    facts = payload["runs"][0]["checkin"]
    assert set(facts) == {"checked_at", "since", "age_seconds", "stale"}
    assert facts["checked_at"] is None, "nothing has read this run"
    assert facts["stale"] is False, "a run the platform has just seen is not stale"


def test_the_row_reports_a_check_the_orchestrator_made(platform_root):
    import os

    from lmer_platform import checkin

    run = ("gitlab.example.com", "agents/global", "develop-1")
    registry.register(
        "s-1", pid=os.getpid(),
        run={"host": run[0], "project": run[1], "slug": run[2]},
    )
    checkin.record_check(*run)

    facts = api.build_state(cfg.load())["runs"][0]["checkin"]
    assert facts["checked_at"], "the row does not report what the daemon recorded"
    assert facts["stale"] is False


def test_a_fleet_poll_records_no_check_in(platform_root):
    """Reading the list is not reading a run — and a browser polling it must not
    keep the whole fleet looking fresh."""
    import os

    from lmer_platform import checkin

    registry.register(
        "s-1", pid=os.getpid(),
        run={"host": "gitlab.example.com", "project": "agents/global",
             "slug": "develop-1"},
    )
    api.build_state(cfg.load())
    api.build_state(cfg.load())
    assert checkin.read_marks() == {}, "build_state wrote check-in state"


# --- the work repo's taskdef tiers, in both tree shapes (issue #353) ---------
#
# The daemon reads the operator's own taskdefs off its mirror (T73) by resolving
# the project dir and joining the taskdef tier onto it. The work repo holds two
# shapes at once (D16/D20), so "the project dir" has two answers: the registered
# SHORT NAME the ``_lmer.yaml`` registry binds this repo to (D7/D8), and — for
# a repo nobody has named yet, or a tree with no manifest at all — the
# ``{host}/{project}`` host bucket that has always held it (D9). Both are
# offered, schema name first, exactly as the container's own resolver
# (``lmer_cli.container.taskdefs.work_repo_taskdef_dirs``) searches them: a menu
# that named a tier the container will not read is a spawn that fails.
#
# The properties that must survive the second shape, in the order they matter:
# the containment re-check that stops a crafted repo URL aiming the search
# outside the mirror; no pull on a request path; a present-but-broken manifest
# failing CLOSED (D20) rather than quietly demoting the search to the
# pre-migration bucket; and nothing ever derived from a repo name (D9).

REPO_URL = "https://gitlab.example.com/acme/widgets.git"
#: What that URL parses to, which is the host bucket an unnamed repo's work
#: lives in — and the short name the fixture registry binds it to.
BUCKET = ("gitlab.example.com", "acme/widgets")
SHORT_NAME = "widgets"


@pytest.fixture
def mirror(platform_root):
    """This daemon's work-repo mirror, planted rather than cloned.

    Resolved through :attr:`PlatformConfig.mirror_path` so it keeps pointing at
    the directory the daemon actually reads; ``platform_root`` is what keeps it
    inside tmp_path.
    """
    root = cfg.load().mirror_path
    root.mkdir(parents=True)
    return root


def _plant_manifest(mirror, projects, *, name="example-plexus"):
    """A ``_lmer.yaml`` at the mirror root — the registry IS the identity (D8)."""
    (mirror / "_lmer.yaml").write_text(
        yaml.safe_dump({"schema": 1, "name": name, "projects": projects}),
        encoding="utf-8",
    )


def _tiers(config, **kwargs):
    return [str(path) for path in api._work_repo_taskdef_dirs(config, **kwargs)]


def test_the_project_taskdef_tier_is_found_by_registry_lookup(config, mirror):
    """Schema form: the project dir is the registered short name, and nothing
    about the repo URL says which name that is — only the registry does (D7/D8)."""
    _plant_manifest(mirror, {SHORT_NAME: {"repo": REPO_URL}})
    plant_taskdefs(mirror / SHORT_NAME / "_taskdef", "develop")

    assert _tiers(config, repo_url=REPO_URL) == [str(mirror / SHORT_NAME / "_taskdef")]


def test_the_schema_tier_is_offered_before_the_bucket_it_migrates_from(config, mirror):
    """A tree mid-migration holds both, and co-existence is real rather than
    promised (D16): both tiers are searched, the schema-owned name first."""
    _plant_manifest(mirror, {SHORT_NAME: {"repo": REPO_URL}})
    plant_taskdefs(mirror / SHORT_NAME / "_taskdef", "develop")
    plant_taskdefs(mirror.joinpath(*BUCKET) / "taskdef", "older")

    assert _tiers(config, repo_url=REPO_URL) == [
        str(mirror / SHORT_NAME / "_taskdef"),
        str(mirror.joinpath(*BUCKET) / "taskdef"),
    ]


def test_a_project_that_holds_the_session_area_still_resolves_positionally(
    config, mirror
):
    """The host bucket is not a transitional shape — it is the permanent holding
    form for a repo nobody has named yet (D9) — and its project may already be
    writing session records. The dir is still found by ``{host}/{project}``, and
    its taskdef tier is read under both names."""
    project = mirror.joinpath(*BUCKET)
    (project / "_runs" / "develop-1" / "20260824-165230-2-31245").mkdir(parents=True)
    plant_taskdefs(project / "_taskdef", "develop")

    assert _tiers(config, repo_url=REPO_URL) == [str(project / "_taskdef")]


def test_a_project_holding_both_record_areas_resolves_the_same(config, mirror):
    """With a legacy ``runs/`` beside the new ``_runs/`` as well: which record
    areas a project holds is not how its taskdef tier is found."""
    project = mirror.joinpath(*BUCKET)
    (project / "runs" / "develop-1").mkdir(parents=True)
    (project / "_runs" / "develop-1" / "20260824-165230-2-31245").mkdir(parents=True)
    plant_taskdefs(project / "taskdef", "develop")

    assert _tiers(config, repo_url=REPO_URL) == [str(project / "taskdef")]


def test_a_crafted_repo_url_cannot_aim_the_taskdef_search_outside_the_mirror(
    config, mirror
):
    """``host``/``project`` come out of a repo URL a caller supplied, and the
    parser hands back whatever path that URL had — ``..`` segments included.
    Without the re-check, such a URL would aim the taskdef search at a directory
    outside the mirror and the menu would report what it found there."""
    outside = plant_taskdefs(mirror.parent / "outside" / "taskdef", "not-yours")
    escaping = "https://gitlab.example.com/../../outside"

    assert api._mirror_project_dir(mirror, BUCKET[0], "../../outside") is None
    assert _tiers(config, repo_url=escaping) == []
    assert outside.is_dir(), "the directory the URL aimed at is really there"


def test_a_symlinked_project_dir_is_refused_by_the_same_re_check(config, mirror):
    """The check is after resolution, so a project dir that is a symlink out of
    the mirror is refused as well as one spelled with ``..``."""
    outside = plant_taskdefs(mirror.parent / "elsewhere" / "taskdef", "not-yours")
    link = mirror.joinpath(*BUCKET)
    link.parent.mkdir(parents=True)
    link.symlink_to(outside.parent, target_is_directory=True)

    assert api._mirror_project_dir(mirror, *BUCKET) is None
    assert _tiers(config, repo_url=REPO_URL) == []


def test_an_unusable_manifest_drops_the_project_tiers_not_the_global_one(
    config, mirror
):
    """Fail CLOSED on a manifest that is present but broken (D20): a
    one-character typo must never quietly point the search back at the
    pre-migration bucket, which is the silent demotion the second form exists to
    make impossible. The global tier is not a project claim, so it stays."""
    (mirror / "_lmer.yaml").write_text("projects: [", encoding="utf-8")
    plant_taskdefs(mirror / SHORT_NAME / "_taskdef", "schema-tier")
    plant_taskdefs(mirror.joinpath(*BUCKET) / "taskdef", "bucket-tier")
    plant_taskdefs(mirror / "_taskdef", "global-tier")

    assert _tiers(config, repo_url=REPO_URL) == [str(mirror / "_taskdef")]


def test_an_unregistered_repo_gets_no_invented_short_name(config, mirror):
    """Names are never derived (D9). A directory named after the repo is not the
    project's dir until a human says so in the manifest — until then the work
    lives in the bucket, visibly unnamed."""
    _plant_manifest(
        mirror, {"other": {"repo": "https://gitlab.example.com/acme/other"}}
    )
    plant_taskdefs(mirror / SHORT_NAME / "_taskdef", "derived")
    plant_taskdefs(mirror.joinpath(*BUCKET) / "taskdef", "the-bucket")

    assert _tiers(config, repo_url=REPO_URL) == [
        str(mirror.joinpath(*BUCKET) / "taskdef")
    ]


def test_the_global_tier_reads_the_schema_name_before_the_bare_one(config, mirror):
    """``_taskdef/`` is the schema-owned name (D6/§2); the bare one is
    grandfathered until the tree migrates, so both are read, in that order."""
    plant_taskdefs(mirror / "_taskdef", "schema")
    plant_taskdefs(mirror / "taskdef", "legacy")

    assert _tiers(config) == [str(mirror / "_taskdef"), str(mirror / "taskdef")]


def test_the_tiers_agree_with_the_container_in_schema_form(config, mirror, monkeypatch):
    """Not a second opinion about the work repo's layout: the tiers and their
    order are asserted against the container's own resolver, which is the code
    that settles every id this menu offers — in the second shape too."""
    from lmer_cli.container import taskdefs as container_taskdefs

    _plant_manifest(mirror, {SHORT_NAME: {"repo": REPO_URL}})
    plant_taskdefs(mirror / SHORT_NAME / "_taskdef", "develop")
    plant_taskdefs(mirror.joinpath(*BUCKET) / "taskdef", "older")
    plant_taskdefs(mirror / "_taskdef", "global")
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(mirror))
    monkeypatch.setenv("LMER_REPO_URL", REPO_URL)
    monkeypatch.setenv("LMER_REPO_HOST", BUCKET[0])
    monkeypatch.setenv("LMER_REPO_PROJECT", BUCKET[1])

    ours = api._work_repo_taskdef_dirs(config, repo_url=REPO_URL)

    assert [path.resolve() for path in ours] == [
        path.resolve() for path in container_taskdefs.work_repo_taskdef_dirs()
    ]


def test_the_schema_form_tier_reaches_the_spawn_form(config, mirror, tmp_path,
                                                     monkeypatch):
    """End to end: what the operator is offered on the run form is what the
    registry resolved, and the route stays advisory."""
    _plant_manifest(mirror, {SHORT_NAME: {"repo": REPO_URL}})
    plant_taskdefs(mirror / SHORT_NAME / "_taskdef", "develop")
    blind_the_host(monkeypatch, tmp_path)

    options = api.discover_spawn_options(config, repo_url=REPO_URL)

    assert options["taskdefs"] == ["develop"]
    assert options["advisory"] is True


def test_reading_the_taskdef_tiers_never_pulls_the_mirror(config, mirror, tmp_path,
                                                          monkeypatch):
    """The mirror is read exactly as it stands: a form's select is no reason to
    put a ``git fetch`` on a request path, in either shape."""
    pulls = []
    monkeypatch.setattr(api, "pull", lambda *a, **k: pulls.append(a))
    _plant_manifest(mirror, {SHORT_NAME: {"repo": REPO_URL}})
    plant_taskdefs(mirror / SHORT_NAME / "_taskdef", "develop")
    blind_the_host(monkeypatch, tmp_path)

    assert api.discover_spawn_options(config, repo_url=REPO_URL)["taskdefs"] == [
        "develop"
    ]
    assert pulls == []


@pytest.fixture
def fresh_model_cache(monkeypatch):
    """Each test starts from an empty provider-model cache."""
    monkeypatch.setattr(api, "_model_discovery_cache", {})


def test_spawn_options_lists_discovered_provider_models(
    config, tmp_path, monkeypatch, fresh_model_cache
):
    """The spawn form can offer a model that actually exists (issue #490).

    A provider the host can query contributes its live model ids; a provider
    whose key is missing or whose endpoint is dead costs only its own row, not
    the answer.
    """
    from lmer_cli import pi_providers as pp

    providers_file = tmp_path / "pi-providers.json"
    providers_file.write_text(json.dumps({
        "skynet": {"baseUrl": "https://skynet.example/v1", "keyEnv": "SKYNET_KEY"},
        "dead": {"baseUrl": "https://dead.example/v1", "keyEnv": "DEAD_KEY"},
    }), encoding="utf-8")
    monkeypatch.setenv(pp.PROVIDERS_FILE_ENV, str(providers_file))
    monkeypatch.setenv("SKYNET_KEY", "sk-secret")
    monkeypatch.delenv("DEAD_KEY", raising=False)

    def fake_discover(provider, key=None, **kwargs):
        if provider.name == "dead":
            raise RuntimeError("connection refused")
        return [{"id": "deepseek-v4-flash"}]

    monkeypatch.setattr(pp, "discover_models", fake_discover)

    options = api.discover_spawn_options(config, repo_url=REPO_URL)
    assert options["models"] == {"skynet": ["deepseek-v4-flash"]}


def _two_providers(tmp_path, monkeypatch):
    from lmer_cli import pi_providers as pp

    providers_file = tmp_path / "pi-providers.json"
    providers_file.write_text(json.dumps({
        "skynet": {"baseUrl": "https://skynet.example/v1", "apiKey": "local"},
        "lan": {"baseUrl": "http://10.255.255.1:8080/v1", "apiKey": "local"},
    }), encoding="utf-8")
    monkeypatch.setenv(pp.PROVIDERS_FILE_ENV, str(providers_file))
    return pp, providers_file


def test_spawn_options_model_discovery_is_bounded(
    tmp_path, monkeypatch, fresh_model_cache
):
    """An unreachable provider must not hold the spawn form for its full
    timeout: providers are probed in parallel under one
    deadline, and a provider still pending at the deadline costs its row."""
    import threading
    import time

    pp, _ = _two_providers(tmp_path, monkeypatch)
    release = threading.Event()
    seen: dict = {}

    def fake_discover(provider, key=None, **kwargs):
        seen[provider.name] = kwargs
        if provider.name == "lan":
            release.wait(10)  # an unroutable host, stalled in connect
            return [{"id": "never"}]
        return [{"id": "deepseek-v4-flash"}]

    monkeypatch.setattr(pp, "discover_models", fake_discover)
    monkeypatch.setattr(api, "_MODEL_DISCOVERY_DEADLINE", 0.3)
    try:
        started = time.monotonic()
        models = api._discovered_models()
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert models == {"skynet": ["deepseek-v4-flash"]}
    assert elapsed < 2.0
    # The form needs ids only, and each probe's own timeout is short.
    assert seen["skynet"]["context_windows"] is False
    assert seen["skynet"]["timeout"] <= api._MODEL_DISCOVERY_TIMEOUT


def test_spawn_options_model_discovery_is_cached(
    tmp_path, monkeypatch, fresh_model_cache
):
    """Repeated form loads within the TTL do not re-probe; a changed provider
    file does."""
    pp, providers_file = _two_providers(tmp_path, monkeypatch)
    calls: list = []

    def fake_discover(provider, key=None, **kwargs):
        calls.append(provider.name)
        return [{"id": f"{provider.name}-model"}]

    monkeypatch.setattr(pp, "discover_models", fake_discover)
    first = api._discovered_models()
    second = api._discovered_models()
    assert first == second == {
        "skynet": ["skynet-model"], "lan": ["lan-model"],
    }
    assert sorted(calls) == ["lan", "skynet"]

    providers_file.write_text(json.dumps({
        "skynet": {"baseUrl": "https://skynet.example/v1", "apiKey": "local"},
    }), encoding="utf-8")
    assert api._discovered_models() == {"skynet": ["skynet-model"]}
    assert sorted(calls) == ["lan", "skynet", "skynet"]

    # Past the TTL the answer is refreshed.
    monkeypatch.setattr(api, "_MODEL_DISCOVERY_TTL", 0.0)
    api._discovered_models()
    assert calls.count("skynet") == 3


# --- fleet-wide relations (the constellation map's edges) --------------------
#
# The map draws the whole fleet at once, so its edges ride this payload rather
# than costing a relation request per node per poll — the same trade the open
# questions and the statusline already make.

def _register_run(session, slug, *, host="h.example.com", project="p/q"):
    import os

    registry.register(
        session, pid=os.getpid(),
        run={"host": host, "project": project, "slug": slug},
    )
    return (host, project, slug)


def test_fleet_payload_carries_related_pairs_once(platform_root):
    """Both endpoints tracked, so the edge has two nodes to attach to."""
    from lmer_platform import relations, runs

    first = _register_run("s-a", "run-a")
    second = _register_run("s-b", "run-b")
    runs.track(*first, source="spawned")
    runs.track(*second, source="spawned")
    relations.relate(first, second)

    payload = api.build_state(cfg.load())

    assert payload["relations"] == [
        {"a": runs.run_key(*first), "b": runs.run_key(*second)}
    ], "the pair should appear exactly once, keyed the way runKey() composes"


def test_fleet_payload_drops_an_edge_whose_run_is_not_on_it(platform_root):
    """An endpoint with no row is an edge attached to nothing.

    Dropping it here is what lets the client join edges to nodes without
    checking. The relation itself is not lost — it is still shown, and named as
    untracked, on the run's own detail view.
    """
    from lmer_platform import relations, runs

    present = _register_run("s-a", "run-a")
    runs.track(*present, source="spawned")
    absent = ("h.example.com", "p/q", "never-adopted")
    relations.relate(present, absent)

    payload = api.build_state(cfg.load())

    assert payload["relations"] == [], (
        "an edge survived to a run that has no node on this payload"
    )
    assert relations.list_for(*present), (
        "the relation itself should still exist — only the fleet edge is dropped"
    )


def test_fleet_payload_always_carries_a_relations_list(platform_root):
    """Empty is the answer for a fleet with no relations, not a missing key.

    A client that has to branch on whether the daemon sent the field would get
    it wrong the first time somebody related two runs.
    """
    payload = api.build_state(cfg.load())

    assert payload["relations"] == []


def test_fleet_payload_keeps_an_edge_on_a_run_that_wants_the_operator(platform_root):
    """The map's most important nodes must not be the ones that lose their edges.

    ``attention`` holds separate copies of rows that are also in ``runs``, so the
    tracked-key set is built from ``runs`` alone. If that ever stopped being true,
    every edge touching a run needing a human would silently vanish — from
    exactly the view that exists to surface those runs.
    """
    from lmer_platform import relations, runs

    live = _register_run("s-live", "run-live")
    dead = ("h.example.com", "p/q", "run-dead")
    registry.register("s-dead", pid=2**22, run={
        "host": dead[0], "project": dead[1], "slug": dead[2],
    })
    runs.track(*live, source="spawned")
    runs.track(*dead, source="spawned")
    relations.relate(live, dead)

    payload = api.build_state(cfg.load())

    assert payload["attention"], "expected the crashed run to need a human"
    first, second = sorted((runs.run_key(*live), runs.run_key(*dead)))
    assert payload["relations"] == [{"a": first, "b": second}], (
        "an edge touching a run that wants the operator was dropped"
    )


# --- the key segment (spec §6.1, A2.fleet) -----------------------------------
#
# A schema-2 run is tracked under its execution uid, and build_state resolves
# its record by that uid — never "the newest record in the slug's group", which
# for the older of two launches is somebody else's record.

UID_OLDER = "a" * 32
UID_NEWER = "b" * 32
UID_PROJECT = ("gitlab.example.com", "acme/widgets")


def _plant_uid_group(config):
    config.mirror_path.mkdir(parents=True, exist_ok=True)
    (config.mirror_path / "_lmer.yaml").write_text(
        yaml.safe_dump({
            "schema": 2, "name": "example-plexus",
            "projects": {"widgets": {"repo": "ssh://git@gitlab.example.com/acme/widgets.git"}},
        }),
        encoding="utf-8",
    )
    for uid, started, goal in (
        (UID_OLDER, "2026-01-01T00:00:00+00:00", "the older chat"),
        (UID_NEWER, "2026-02-01T00:00:00+00:00", "the newer chat"),
    ):
        path = config.mirror_path / "widgets" / "_runs" / "chat" / uid
        path.mkdir(parents=True, exist_ok=True)
        (path / "state.yaml").write_text(
            yaml.safe_dump({
                "schema": 2, "slug": "chat", "status": "in-progress", "goal": goal,
                "session": {"started_at": started},
            }),
            encoding="utf-8",
        )


def test_state_shows_a_uid_tracked_entry_with_its_own_record_and_uid(
    config, platform_root, fake_state
):
    from lmer_platform import runs as run_index

    _plant_uid_group(config)
    run_index.track(*UID_PROJECT, "chat", uid=UID_OLDER, taskdef="chat")
    real_client = TestClient(api.create_app(config, SECRET, state_builder=api.build_state))

    payload = real_client.get("/api/state", headers=bearer_header()).json()

    assert payload["totals"]["runs"] == 1
    row = payload["runs"][0]
    assert row["uid"] == UID_OLDER
    assert row["slug"] == "chat"
    assert row["goal"] == "the older chat", "resolved to the group's newest record"
    assert row["rel_path"] == f"widgets/_runs/chat/{UID_OLDER}"


def test_two_uid_tracked_launches_of_one_group_are_two_rows(platform_root):
    from lmer_platform import checkin, relations, runs as run_index

    config = cfg.load()
    _plant_uid_group(config)
    run_index.track(*UID_PROJECT, "chat", uid=UID_OLDER)
    run_index.track(*UID_PROJECT, "chat", uid=UID_NEWER)
    checkin.record_check(*UID_PROJECT, UID_OLDER)
    relations.relate((*UID_PROJECT, UID_OLDER), (*UID_PROJECT, UID_NEWER))

    payload = api.build_state(config)

    rows = {row["uid"]: row for row in payload["runs"]}
    assert set(rows) == {UID_OLDER, UID_NEWER}
    assert rows[UID_OLDER]["goal"] == "the older chat"
    assert rows[UID_NEWER]["goal"] == "the newer chat"
    assert rows[UID_OLDER]["checkin"]["checked_at"]
    assert rows[UID_NEWER]["checkin"]["checked_at"] is None
    older, newer = (run_index.run_key(*UID_PROJECT, uid) for uid in (UID_OLDER, UID_NEWER))
    assert payload["relations"] == [{"a": older, "b": newer}]


def test_the_references_fold_reads_a_flat_uid_row(platform_root, monkeypatch):
    from lmer_platform import references, runs as run_index

    run_index.track(*UID_PROJECT, "chat", uid=UID_OLDER)
    references.add(
        *UID_PROJECT, UID_OLDER,
        "https://example.slack.com/archives/C0123ABCDEF/p1712345678000900",
        title="the design thread",
    )
    payload = {"runs": [
        {"host": UID_PROJECT[0], "project": UID_PROJECT[1], "slug": "chat", "uid": UID_OLDER},
        {"host": UID_PROJECT[0], "project": UID_PROJECT[1], "slug": "chat", "uid": UID_NEWER},
    ]}

    api._fold_references(payload)

    assert [r["title"] for r in payload["runs"][0]["references"]] == ["the design thread"]
    assert payload["runs"][1]["references"] == []


# --- every run-keyed route by uid (spec §6.1, §6.4, A2.routes) -----------------
#
# A schema-2 launch is addressed by its execution uid, so each route that names
# a run takes ``uid`` beside ``{host, project, slug}`` — on the body for a
# write, on the query string for a read — and acts on the uid-keyed record.

UID_HOST, UID_PROJ = UID_PROJECT


def _uid_body(uid, **extra):
    return {"host": UID_HOST, "project": UID_PROJ, "uid": uid, **extra}


def test_adopt_by_uid_binds_an_unresolved_entry(config, platform_root, client):
    """The operator's answer to a "re-adopt by uid" row: the slug-keyed entry
    the transition could not bind is re-keyed to the chosen record, and what
    the operator wrote about the run moves with it."""
    from lmer_platform import meta, runs as run_index

    _plant_uid_group(config)
    run_index.track(*UID_PROJECT, "chat", taskdef="chat")
    meta.write(*UID_PROJECT, "chat", title="the chat")
    api.build_state(config)
    slug_key = run_index.run_key(*UID_PROJECT, "chat")
    assert run_index.load_index()[slug_key]["unresolved"], "precondition: two records"

    response = client.post(
        "/api/runs/adopt", headers=bearer_header(), json=_uid_body(UID_OLDER)
    )

    assert response.status_code == 200, response.text
    tracked = response.json()["tracked"]
    uid_key = run_index.run_key(*UID_PROJECT, UID_OLDER)
    assert tracked["key"] == uid_key
    assert tracked["uid"] == UID_OLDER and tracked["slug"] == "chat"
    assert tracked["source"] == "spawned", "binding is not a new adoption"
    index = run_index.load_index()
    assert set(index) == {uid_key}
    assert "unresolved" not in index[uid_key]
    assert index[uid_key]["aliases"] == [slug_key]
    assert meta.read(*UID_PROJECT, UID_OLDER).title == "the chat"


def test_re_adopting_by_uid_clears_a_conflict_once_its_pair_is_written(
    config, platform_root, client
):
    """What clears a conflict mark (review !387, discussion 5): writing the
    conflicted field replaces the pair, and re-adopting by uid then drops the
    mark. Re-adopting while the pair is still there keeps it."""
    from lmer_platform import meta, run_transition, runs as run_index

    _plant_uid_group(config)
    run_index.track(*UID_PROJECT, "chat")
    run_index.track(*UID_PROJECT, "chat", uid=UID_OLDER)
    meta.write(*UID_PROJECT, "chat", title="the slug title")
    meta.write(*UID_PROJECT, UID_OLDER, title="the uid title")
    run_transition.bind(config, *UID_PROJECT, "chat", UID_OLDER)
    uid_key = run_index.run_key(*UID_PROJECT, UID_OLDER)
    assert run_index.load_index()[uid_key]["conflict"] == {"stores": ["meta"]}

    kept = client.post("/api/runs/adopt", headers=bearer_header(), json=_uid_body(UID_OLDER))
    assert kept.status_code == 200, kept.text
    assert run_index.load_index()[uid_key]["conflict"] == {"stores": ["meta"]}

    client.post("/api/runs/meta", headers=bearer_header(),
                json=_uid_body(UID_OLDER, title="the chosen title"))
    cleared = client.post("/api/runs/adopt", headers=bearer_header(), json=_uid_body(UID_OLDER))

    assert cleared.status_code == 200, cleared.text
    assert "conflict" not in run_index.load_index()[uid_key]
    assert run_index.readopt_marks() == {}


def test_adopt_by_uid_tracks_a_record_with_its_slug_read_from_the_mirror(
    config, platform_root, client
):
    from lmer_platform import runs as run_index

    _plant_uid_group(config)

    response = client.post(
        "/api/runs/adopt", headers=bearer_header(),
        json=_uid_body(UID_NEWER, note="picked by uid"),
    )

    assert response.status_code == 200, response.text
    tracked = response.json()["tracked"]
    assert tracked["slug"] == "chat", "the group label comes from the record"
    assert tracked["uid"] == UID_NEWER and tracked["source"] == "adopted"
    assert tracked["note"] == "picked by uid"
    assert set(run_index.load_index()) == {run_index.run_key(*UID_PROJECT, UID_NEWER)}


def test_adopt_by_uid_beside_a_bound_launch_tracks_a_second_row(
    config, platform_root, client
):
    """A slug that already resolves to one bound launch is not a reason to
    re-key that launch: the other uid is a row of its own."""
    from lmer_platform import runs as run_index

    _plant_uid_group(config)
    run_index.track(*UID_PROJECT, "chat", uid=UID_OLDER)

    client.post("/api/runs/adopt", headers=bearer_header(), json=_uid_body(UID_NEWER))

    assert set(run_index.load_index()) == {
        run_index.run_key(*UID_PROJECT, UID_OLDER),
        run_index.run_key(*UID_PROJECT, UID_NEWER),
    }


def test_adopt_by_a_uid_with_no_record_and_no_slug_is_refused(config, platform_root, client):
    from lmer_platform import runs as run_index

    response = client.post(
        "/api/runs/adopt", headers=bearer_header(), json=_uid_body("c" * 32)
    )

    assert response.status_code == 400
    assert "c" * 32 in response.json()["detail"]
    assert run_index.load_index() == {}


def test_a_malformed_uid_is_refused_rather_than_read_as_the_slug(client, platform_root):
    """``key_parts`` would fall back to the slug, addressing the group's
    slug-keyed entry — a different run than the caller named."""
    from lmer_platform import runs as run_index

    run_index.track(*UID_PROJECT, "chat")
    for method, path, kwargs in (
        ("post", "/api/runs/adopt", {"json": {**_uid_body("NOT-HEX"), "slug": "chat"}}),
        ("post", "/api/runs/forget", {"json": {**_uid_body("NOT-HEX"), "slug": "chat"}}),
        ("get", "/api/runs/meta", {"params": {**_uid_body("NOT-HEX"), "slug": "chat"}}),
    ):
        response = getattr(client, method)(path, headers=bearer_header(), **kwargs)
        assert response.status_code == 400, (path, response.text)
        assert "uid" in response.json()["detail"]
    assert run_index.get_tracked(*UID_PROJECT, "chat") is not None


def test_forget_by_uid_forgets_that_launch_and_drops_its_connections(
    client, platform_root, monkeypatch
):
    from lmer_platform import runs as run_index

    dropped = []

    class _Connectors:
        def drop_run(self, identity):
            dropped.append(identity)
            return 0

    monkeypatch.setattr(connectors, "ensure_connectors", lambda: _Connectors())
    run_index.track(*UID_PROJECT, "chat", uid=UID_OLDER)
    run_index.track(*UID_PROJECT, "chat", uid=UID_NEWER)

    response = client.post(
        "/api/runs/forget", headers=bearer_header(), json=_uid_body(UID_OLDER)
    )

    assert response.json() == {"forgotten": True}
    assert set(run_index.load_index()) == {run_index.run_key(*UID_PROJECT, UID_NEWER)}
    # The key segment, which is what ``runs.run_for_session`` answers with.
    assert dropped == [(UID_HOST, UID_PROJ, UID_OLDER)]


def test_meta_by_uid_writes_and_reads_the_launch_and_echoes_the_uid(client, platform_root):
    from lmer_platform import meta, runs as run_index

    run_index.track(*UID_PROJECT, "chat", uid=UID_OLDER)
    run_index.track(*UID_PROJECT, "chat", uid=UID_NEWER)

    written = client.post(
        "/api/runs/meta", headers=bearer_header(),
        json=_uid_body(UID_OLDER, title="the older chat"),
    )
    read = client.get(
        "/api/runs/meta", headers=bearer_header(), params=_uid_body(UID_OLDER)
    )

    assert written.status_code == 200, written.text
    assert read.json()["meta"]["title"] == "the older chat"
    assert read.json()["run"]["uid"] == UID_OLDER
    assert meta.read(*UID_PROJECT, UID_NEWER).title == "", "the other launch is untouched"


def test_a_uid_echo_carries_the_entrys_slug_not_the_callers(client, platform_root):
    """The uid decides which run a request is about, so a ``slug`` sent beside
    it that names another group is not echoed back as if it were this run's."""
    from lmer_platform import runs as run_index

    run_index.track(*UID_PROJECT, "chat", uid=UID_OLDER)

    tracked = client.get(
        "/api/runs/meta", headers=bearer_header(),
        params=_uid_body(UID_OLDER, slug="other"),
    ).json()
    untracked = client.get(
        "/api/runs/meta", headers=bearer_header(),
        params=_uid_body(UID_NEWER, slug="other"),
    ).json()

    assert tracked["run"] == {"host": UID_HOST, "project": UID_PROJ, "slug": "chat",
                              "uid": UID_OLDER}
    assert untracked["run"] == {"host": UID_HOST, "project": UID_PROJ, "slug": None,
                                "uid": UID_NEWER}


def test_meta_echo_of_a_slug_request_keeps_its_shape(client, platform_root):
    """Schema-1 replies are byte-identical: no ``uid`` key appears."""
    read = client.get(
        "/api/runs/meta", headers=bearer_header(),
        params={"host": UID_HOST, "project": UID_PROJ, "slug": "chat"},
    )

    assert read.json()["run"] == {"host": UID_HOST, "project": UID_PROJ, "slug": "chat"}


def test_files_by_uid_lists_that_launch_not_the_newest(config, client, platform_root):
    _plant_uid_group(config)

    reply = client.get(
        "/api/runs/files", headers=bearer_header(), params=_uid_body(UID_OLDER)
    ).json()

    assert reply["present"] is True
    assert reply["rel_path"] == f"widgets/_runs/chat/{UID_OLDER}"
    assert reply["run"]["uid"] == UID_OLDER


def test_a_fleet_row_naming_slug_and_uid_acts_on_the_uid_key_not_the_slug_entry(
    config, client, platform_root
):
    """A schema-2 fleet row carries both ``slug`` (the group label) and ``uid``;
    a schema-1 ``chat`` entry beside it must not be the one meta, files or
    forget reach (review !387, discussion 2)."""
    from lmer_platform import meta, runs as run_index

    _plant_uid_group(config)
    run_index.track(*UID_PROJECT, "chat")
    run_index.track(*UID_PROJECT, "chat", uid=UID_OLDER)
    meta.write(*UID_PROJECT, "chat", title="the slug run")
    row = _uid_body(UID_OLDER, slug="chat")

    written = client.post(
        "/api/runs/meta", headers=bearer_header(), json={**row, "title": "the older chat"}
    )
    files = client.get("/api/runs/files", headers=bearer_header(), params=row).json()

    assert written.status_code == 200, written.text
    assert meta.read(*UID_PROJECT, UID_OLDER).title == "the older chat"
    assert meta.read(*UID_PROJECT, "chat").title == "the slug run"

    forgotten = client.post("/api/runs/forget", headers=bearer_header(), json=row).json()

    assert files["rel_path"] == f"widgets/_runs/chat/{UID_OLDER}"
    assert forgotten == {"forgotten": True}
    assert set(run_index.load_index()) == {run_index.run_key(*UID_PROJECT, "chat")}
    assert meta.read(*UID_PROJECT, UID_OLDER).title == "", "forget dropped the uid's title"


def test_relations_by_uid_relate_list_and_unrelate_two_launches(client, platform_root):
    from lmer_platform import relations, runs as run_index

    run_index.track(*UID_PROJECT, "chat", uid=UID_OLDER)
    run_index.track(*UID_PROJECT, "chat", uid=UID_NEWER)
    body = _uid_body(UID_OLDER, related=_uid_body(UID_NEWER))

    related = client.post("/api/runs/relate", headers=bearer_header(), json=body)
    listed = client.get(
        "/api/runs/relations", headers=bearer_header(), params=_uid_body(UID_NEWER)
    ).json()

    assert related.status_code == 200, related.text
    assert related.json()["created"] is True
    assert related.json()["related"]["uid"] == UID_NEWER
    assert relations.list_for(*UID_PROJECT, UID_OLDER)[0].key == run_index.run_key(
        *UID_PROJECT, UID_NEWER
    )
    assert [entry["key"] for entry in listed["relations"]] == [
        run_index.run_key(*UID_PROJECT, UID_OLDER)
    ]
    assert listed["relations"][0]["tracked"] is True

    removed = client.post("/api/runs/unrelate", headers=bearer_header(), json=body)

    assert removed.json()["removed"] is True
    assert relations.load_all() == {}


def test_candidates_carry_the_record_uid_and_match_a_uid_keyed_entry(
    config, client, platform_root
):
    from lmer_platform import runs as run_index

    _plant_uid_group(config)
    solo = "c" * 32
    path = config.mirror_path / "widgets" / "_runs" / "solo" / solo
    path.mkdir(parents=True)
    (path / "state.yaml").write_text(
        yaml.safe_dump({"schema": 2, "slug": "solo", "status": "in-progress",
                        "session": {"started_at": "2026-03-01T00:00:00+00:00"}}),
        encoding="utf-8",
    )

    before = client.get("/api/runs/candidates", headers=bearer_header()).json()
    run_index.track(*UID_PROJECT, "solo", uid=solo)
    after = client.get("/api/runs/candidates", headers=bearer_header()).json()

    (row,) = [c for c in before["candidates"] if c["slug"] == "solo"]
    assert row["uid"] == solo
    assert row["tracked"] is False
    (row,) = [c for c in after["candidates"] if c["slug"] == "solo"]
    assert row["tracked"] is True, "a uid-keyed entry tracks its own record"


def test_a_legacy_candidate_carries_no_uid(client, platform_root, config):
    _plant_mirror_run(config, "mine")

    (row,) = client.get("/api/runs/candidates", headers=bearer_header()).json()["candidates"]

    assert row["uid"] is None


def test_move_composes_the_key_from_the_uid(client, platform_root, monkeypatch):
    from lmer_platform import projects, runs as run_index

    moved = []
    monkeypatch.setattr(
        projects, "move", lambda key, *_a, **_k: moved.append(key) or {"ok": True}
    )

    client.post(
        "/api/runs/move", headers=bearer_header(),
        json={**_uid_body(UID_OLDER, slug="chat"), "destination_id": None},
    )

    assert moved == [run_index.run_key(*UID_PROJECT, UID_OLDER)]


def test_an_assistant_read_by_uid_checks_in_that_launch(config, client, platform_root):
    """``_note_check`` stamps the key segment, so the check lands on the row
    the assistant looked at and not on the slug the group shares."""
    from lmer_platform import checkin, runs as run_index

    _plant_uid_group(config)
    run_index.track(*UID_PROJECT, "chat", uid=UID_OLDER)

    reply = client.get(
        "/api/runs/files", headers=bearer_header(cfg.mint_assistant_credential()),
        params=_uid_body(UID_OLDER, slug="chat"),
    )

    assert reply.status_code == 200, reply.text
    marks = checkin.read_marks()
    assert run_index.run_key(*UID_PROJECT, UID_OLDER) in marks
    assert run_index.run_key(*UID_PROJECT, "chat") not in marks


def test_an_assistant_look_through_a_stale_slug_stamps_the_bound_entry(
    config, client, platform_root
):
    """A slug reference that reaches a bound entry through its alias must not
    re-create the slug-keyed mark the transition moved."""
    from lmer_platform import checkin, run_transition, runs as run_index

    _plant_uid_group(config)
    run_index.track(*UID_PROJECT, "chat")
    run_transition.bind(config, *UID_PROJECT, "chat", UID_NEWER)

    reply = client.get(
        "/api/runs/files", headers=bearer_header(cfg.mint_assistant_credential()),
        params={"host": UID_HOST, "project": UID_PROJ, "slug": "chat"},
    )

    assert reply.status_code == 200, reply.text
    marks = checkin.read_marks()
    assert run_index.run_key(*UID_PROJECT, UID_NEWER) in marks
    assert run_index.run_key(*UID_PROJECT, "chat") not in marks


def test_adopt_by_uid_refuses_a_slug_naming_another_group(config, platform_root, client):
    """The body's slug picks which slug-keyed entry is bound, so it must be the
    record's own group: binding ``other`` to a ``chat`` launch would carry the
    other run's meta, relations and home onto a record they never described."""
    from lmer_platform import meta, runs as run_index

    _plant_uid_group(config)
    run_index.track(*UID_PROJECT, "other", taskdef="other")
    meta.write(*UID_PROJECT, "other", title="the other run")
    before = run_index.load_index()

    response = client.post(
        "/api/runs/adopt", headers=bearer_header(),
        json=_uid_body(UID_OLDER, slug="other"),
    )

    assert response.status_code == 400, response.text
    assert "other" in response.json()["detail"]
    assert run_index.load_index() == before, "nothing moved"
    assert meta.read(*UID_PROJECT, "other").title == "the other run"


def test_adopt_by_uid_binds_an_entry_naming_the_uid_as_a_candidate(
    config, platform_root, client
):
    """The unresolved mark is the transition's own word that this entry may be
    that record, whatever label the record now sits under."""
    from lmer_platform import runs as run_index

    _plant_uid_group(config)
    entry = run_index.track(*UID_PROJECT, "renamed")
    run_index.mark_unresolved(entry.key, [{"uid": UID_OLDER, "started_at": None,
                                           "name": None}])

    response = client.post(
        "/api/runs/adopt", headers=bearer_header(),
        json=_uid_body(UID_OLDER, slug="renamed"),
    )

    assert response.status_code == 200, response.text
    assert set(run_index.load_index()) == {run_index.run_key(*UID_PROJECT, UID_OLDER)}


def test_adopt_by_uid_is_refused_on_a_legacy_mirror(config, platform_root, client):
    """A schema-1 work repo has no execution records, so a uid there addresses
    nothing the container will ever write."""
    from lmer_platform import runs as run_index

    _plant_mirror_run(config, "chat")

    response = client.post(
        "/api/runs/adopt", headers=bearer_header(),
        json=_uid_body("c" * 32, slug="chat"),
    )

    assert response.status_code == 400, response.text
    assert "schema" in response.json()["detail"]
    assert run_index.load_index() == {}


def test_a_stale_slug_reads_the_bound_record_not_the_groups_newest(
    config, platform_root, client
):
    """Once ``chat`` is bound to the OLDER launch, a client still holding the
    slug reference reads that launch — the slug alone would resolve to the
    group's newest record, which is another launch's."""
    from lmer_platform import meta, run_transition, runs as run_index

    _plant_uid_group(config)
    run_index.track(*UID_PROJECT, "chat")
    run_transition.bind(config, *UID_PROJECT, "chat", UID_OLDER)
    meta.write(*UID_PROJECT, UID_OLDER, title="the older chat")
    by_slug = {"host": UID_HOST, "project": UID_PROJ, "slug": "chat"}

    files = client.get("/api/runs/files", headers=bearer_header(), params=by_slug)
    read = client.get("/api/runs/meta", headers=bearer_header(), params=by_slug)

    assert files.json()["rel_path"] == f"widgets/_runs/chat/{UID_OLDER}"
    assert read.json()["meta"]["title"] == "the older chat"
    assert read.json()["run"] == by_slug, "echoed as sent"

    client.post("/api/runs/meta", headers=bearer_header(),
                json={**by_slug, "title": "renamed"})

    assert meta.read(*UID_PROJECT, UID_OLDER).title == "renamed"
    assert "chat" not in {key.rsplit("/", 1)[-1] for key in meta.load_all()}
