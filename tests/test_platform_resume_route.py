"""The ways an operator can actually reach a resume (issue #141, slice M2 / T41).

:mod:`lmer_platform.resume` was implemented, tested and unreachable: no route, no
subcommand, nothing in the UI. Its refusals were the point of the module — a
missing repo URL is *asked for* rather than invented — and an operator could not
trigger one. This file is about the door rather than the room: what
tests/test_platform_resume.py proves about the decision, these tests prove about
the three surfaces that expose it.

- **The route** (``POST /api/runs/resume``): gated like every other, the status
  taken off the exception, and — the one place this route differs from its
  siblings — an error body of ``{code, message}``, because two of the refusals are
  requests for one more field rather than failures. A client that had to match on
  an English sentence to tell those apart would break the first time the sentence
  was improved.
- **The subcommand** (``lmer platform resume``): the same verb without a browser,
  plus the translation of those two codes into the flags that satisfy them — the
  refusals are worded for the API, and "Supply repo_url" is not something a shell
  user can type.
- **The UI**. There is no JS test runner here (see tests/test_platform_web_app.py),
  so the component is checked at source level, and what is worth checking there is
  the seam between the two languages: the code strings it branches on have to be
  the codes the platform actually emits, or the field an operator is being asked
  for never appears. The api.js half is *executed* instead — under Node, against a
  scripted ``fetch`` — because the branch this slice added to ``request`` fails
  invisibly: a ``{code, message}`` body taken down the old path reaches an alert as
  ``[object Object]``.

``lmer platform spawn --agents`` is verified here too. It is the sibling gap in the
same class — the web spawn dialog offers the fan-out roster and the CLI did not, so
the two disagreed about what a spawn can carry — and this slice's file scope put
its test here rather than in tests/test_platform_daemon.py, where the rest of that
verb's tests live.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api
from lmer_platform import config as cfg
from lmer_platform import daemon, registry, resume as resume_mod, spawn, store
from tests.conftest import node_binary, strip_lmer_env

SECRET = "test-secret-value"
WEB = Path(__file__).resolve().parent.parent / "web"

HOST = "gitlab.example.com"
PROJECT = "agents/global"
SLUG = "develop-issue-141"
TASKDEF = "develop"
TARGET = "https://gitlab.example.com/agents/global/-/work_items/141"
RUN_REF = f"{HOST}/{PROJECT}/{SLUG}"
REL_PATH = f"{HOST}/{PROJECT}/runs/{SLUG}"

RESUME_BODY = {"host": HOST, "project": PROJECT, "slug": SLUG}


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in ("LMER_WORK_REPO", "LMER_REPO_URL",
                 cfg.ENV_BIND_ADDRESS, cfg.ENV_BIND_PORT):
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
    """A canned fleet payload so routing tests need no work repo."""
    def builder(config, *, force_pull=False):
        return {"schema": 1, "runs": [], "attention": [], "counts": {},
                "totals": {"runs": 0, "live": 0, "attention": 0}}

    return builder


@pytest.fixture
def client(config, fake_state):
    return TestClient(api.create_app(config, SECRET, state_builder=fake_state))


def bearer_header(token=SECRET):
    return {"Authorization": f"Bearer {token}"}


def spawn_result(platform_root, session_id="s-resume", **overrides):
    payload = {
        "session_id": session_id,
        "pid": 4242,
        "log_path": platform_root / "logs" / f"{session_id}.log",
        "host": HOST,
        "project": PROJECT,
        "slug": SLUG,
        # The argv a resume launches carries --prompt=<direction>, which is why
        # ResumeResult.to_dict does not publish it. A stub that omitted it could not
        # prove that.
        "command": ["lmer", "develop", "target", "--prompt=do the thing"],
        "control_port": 8711,
    }
    payload.update(overrides)
    return spawn.SpawnResult(**payload)


def resume_result(platform_root, request, *, continued=True, started_slug=None,
                  **session_overrides):
    return resume_mod.ResumeResult(
        host=request.host,
        project=request.project,
        slug=request.slug,
        taskdef=request.taskdef or TASKDEF,
        target=TARGET,
        continued=continued,
        started_slug=started_slug or request.slug,
        session=spawn_result(platform_root, **session_overrides),
    )


@pytest.fixture
def resumed(monkeypatch, platform_root):
    """Capture what the route hands lmer_platform.resume, without a spawn."""
    calls = []

    def fake_resume(config, request):
        calls.append(request)
        return resume_result(platform_root, request)

    fake_resume.calls = calls
    monkeypatch.setattr(api, "resume_run", fake_resume)
    return fake_resume


def refuse_with(monkeypatch, target, exc):
    """Point *target*'s ``resume_run`` at a refusal."""
    def refuse(config, request):
        raise exc

    monkeypatch.setattr(target, "resume_run", refuse)


@pytest.fixture
def adopted_run(config):
    """A tracked run in the mirror with **no repo URL** — the adopted shape.

    Planted as bytes rather than written through ``run_state``, for the reason
    tests/test_platform_resume.py plants them that way: the mirror is a read surface
    for the platform, and this is the fixture behind the one test here that runs the
    real module rather than a stub.
    """
    from lmer_platform import runs
    from work_repo import run_state

    slug = run_state.derive_slug(TASKDEF, TARGET)
    assert slug == SLUG, "the fixture and the route body have to name one run"
    path = config.mirror_path / HOST / PROJECT / "runs" / slug
    path.mkdir(parents=True, exist_ok=True)
    (path / "state.yaml").write_text(
        "schema: 1\nstatus: \"in-progress\"\nstop_reason: \"yield\"\n"
        f"slug: {json.dumps(slug)}\ntaskdef: {json.dumps(TASKDEF)}\n"
        f"target: {json.dumps(TARGET)}\n",
        encoding="utf-8",
    )
    return runs.track(
        HOST, PROJECT, slug, source="adopted", taskdef=TASKDEF, target=TARGET
    )


# --- POST /api/runs/resume ----------------------------------------------------

def test_resume_requires_auth(client, resumed):
    """Starting a container is not something an unauthenticated peer may do."""
    assert client.post("/api/runs/resume", json=RESUME_BODY).status_code == 401
    assert resumed.calls == []


def test_the_route_is_listed_in_the_api_index(client):
    """The plain-text index is how an operator with a terminal finds the verbs."""
    body = client.get("/api", headers=bearer_header()).text
    assert "POST /api/runs/resume" in body
    assert "sibling run" in body, (
        "the entry has to say that naming a taskdef starts a different run"
    )


def test_resuming_a_run_starts_a_session(client, resumed):
    response = client.post(
        "/api/runs/resume", headers=bearer_header(), json=RESUME_BODY
    )

    assert response.status_code == 202, (
        "nothing is recorded when this returns — the session claims the run and "
        "prints its own resume brief a moment later"
    )
    payload = response.json()
    assert payload["run"] == {"host": HOST, "project": PROJECT, "slug": SLUG}
    assert payload["started"]["slug"] == SLUG
    assert payload["continued"] is True
    assert payload["session"]["session_id"] == "s-resume"
    request = resumed.calls[0]
    assert (request.host, request.project, request.slug) == (HOST, PROJECT, SLUG)


def test_an_overridden_taskdef_reports_the_sibling_it_started(
    client, monkeypatch, platform_root
):
    """The reply says which run the session went to, and the route must not flatten
    that: an operator told their run is running when a sibling is has been lied to."""
    def fake_resume(config, request):
        return resume_result(
            platform_root, request, continued=False, started_slug="review-issue-141"
        )

    monkeypatch.setattr(api, "resume_run", fake_resume)
    payload = client.post(
        "/api/runs/resume",
        headers=bearer_header(),
        json={**RESUME_BODY, "taskdef": "review"},
    ).json()

    assert payload["continued"] is False
    assert payload["started"]["slug"] == "review-issue-141"
    assert payload["run"]["slug"] == SLUG
    assert "untouched" in payload["note"]


def test_the_optional_fields_reach_the_resume_path(client, resumed):
    """All three are the point of the request: the override, and the two remedies."""
    client.post(
        "/api/runs/resume",
        headers=bearer_header(),
        json={
            **RESUME_BODY,
            "taskdef": "review",
            "repo_url": "https://gitlab.example.com/agents/global",
            "direction": "review the migration only",
        },
    )
    request = resumed.calls[0]

    assert request.taskdef == "review"
    assert request.repo_url == "https://gitlab.example.com/agents/global"
    assert request.direction == "review the migration only"


def test_an_absent_option_stays_absent_rather_than_becoming_blank(client, resumed):
    """``None`` is "I did not say" and the module treats it as such.

    Coercing to ``""`` would work by accident today — blanks normalise to ``None``
    — and would silently turn a non-string into an empty one, which is how a caller
    that sent ``{"taskdef": 5}`` gets a refusal about something else entirely.
    """
    client.post("/api/runs/resume", headers=bearer_header(), json=RESUME_BODY)
    request = resumed.calls[0]

    assert (request.taskdef, request.repo_url, request.direction) == (None, None, None)


def test_a_non_string_option_keeps_its_own_refusal(client, platform_root):
    """Which is what the un-coerced field buys, so it is checked through the route."""
    response = client.post(
        "/api/runs/resume", headers=bearer_header(), json={**RESUME_BODY, "taskdef": 5}
    )
    assert response.status_code == 400
    assert "must be a string" in response.json()["detail"]["message"]


def test_the_direction_does_not_come_back_in_the_reply(client, resumed):
    """The spawn's argv carries --prompt=<direction>, so the reply must not."""
    response = client.post(
        "/api/runs/resume",
        headers=bearer_header(),
        json={**RESUME_BODY, "direction": "do the thing"},
    )
    assert response.status_code == 202
    assert "do the thing" not in response.text
    assert "command" not in response.text


def test_the_reply_carries_the_spawn_s_warning_key(client, resumed):
    """The same key ``POST /api/sessions`` publishes, so a client shows it once."""
    payload = client.post(
        "/api/runs/resume", headers=bearer_header(), json=RESUME_BODY
    ).json()
    assert "warning" in payload, (
        "a client that renders a spawn's warning must not have to know that this "
        "route is the one where the key does not exist"
    )


def test_a_warning_from_the_spawn_reaches_the_caller(client, monkeypatch,
                                                     platform_root):
    """Unreachable today (see ResumeResult.to_dict) and wired anyway: the field
    belongs to the spawn, and a resume that lost the run's identity must not read
    as a clean success."""
    def fake_resume(config, request):
        return resume_result(
            platform_root, request, warning="this run has no identity (…)"
        )

    monkeypatch.setattr(api, "resume_run", fake_resume)
    payload = client.post(
        "/api/runs/resume", headers=bearer_header(), json=RESUME_BODY
    ).json()
    assert payload["warning"] == "this run has no identity (…)"


# --- the refusals, as an HTTP client sees them --------------------------------

@pytest.mark.parametrize("error,status,code", [
    (resume_mod.ResumeError, 400, "resume_refused"),
    (resume_mod.RunNotTracked, 404, "run_not_tracked"),
    (resume_mod.RepoUrlRequired, 400, "repo_url_required"),
    (resume_mod.DirectionRequired, 400, "direction_required"),
    (resume_mod.NotResumable, 409, "not_resumable"),
    (resume_mod.RunIsLive, 409, "live_session"),
    (resume_mod.QuestionOpen, 409, "question_open"),
])
def test_every_refusal_reaches_the_client_with_its_status_and_code(
    client, monkeypatch, error, status, code
):
    """The status rides on the exception so a refusal added later is not a 500, and
    the code rides in the body so a client can act on it without reading English."""
    refuse_with(monkeypatch, api, error("because of a reason"))
    response = client.post(
        "/api/runs/resume", headers=bearer_header(), json=RESUME_BODY
    )

    assert response.status_code == status
    assert response.json()["detail"] == {
        "code": code, "message": "because of a reason",
    }


def test_a_missing_repo_url_is_a_refusal_the_ui_can_act_on(adopted_run, client):
    """The designed behaviour, end to end: an operator is *asked* for the URL.

    Nothing is stubbed here — a real adopted run, in a real mirror, through the real
    module — because what is being checked is that the module's own refusal reaches
    a client intact, code included, rather than a re-worded copy of it. An adopted
    run knows its host, project and slug but not where its code is cloned from, and
    the platform will not invent that: whatever it is given becomes the run's
    repository of record.
    """
    response = client.post(
        "/api/runs/resume", headers=bearer_header(), json=RESUME_BODY
    )

    assert response.status_code == 400, (
        "the same request will never succeed — nothing the platform can wait for "
        "supplies this, so it is not a 409"
    )
    detail = response.json()["detail"]
    assert detail["code"] == "repo_url_required"
    assert "Supply repo_url" in detail["message"]
    assert registry.list_sessions(live_only=False) == [], (
        "and nothing was started: a container spent on a run the platform cannot "
        "file is the harm the refusal exists to avoid"
    )


def test_an_untracked_run_is_refused_by_the_route_itself(client, platform_root):
    """The whole path with nothing stubbed: scope is the local index (spec D25)."""
    response = client.post(
        "/api/runs/resume", headers=bearer_header(), json=RESUME_BODY
    )

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "run_not_tracked"
    assert "adopt it first" in detail["message"]
    assert registry.list_sessions(live_only=False) == []


def test_resuming_at_capacity_returns_429(client, monkeypatch):
    """Continuing a run is not a reason to exceed the cap — it is a spawn."""
    refuse_with(monkeypatch, api, spawn.CapacityError("concurrency cap reached: 4/4"))
    response = client.post(
        "/api/runs/resume", headers=bearer_header(), json=RESUME_BODY
    )

    assert response.status_code == 429
    assert "4/4" in response.json()["detail"]


def test_a_run_the_spawn_says_is_already_live_is_a_409(client, monkeypatch):
    """``RunIsLive``'s residual case, and it must not arrive as a 400.

    The route's own refusal above carries the ``live_session`` code; this one comes
    from the invariant in ``spawn_session``, which matches the identity the spawn is
    about to register and so sees a session the recorded slug did not lead to. A
    sentence rather than {code, message}, exactly as the cap answers — which the
    client reads as a failure rather than as a request for another field, and that
    is the right reading.
    """
    refuse_with(monkeypatch, api, spawn.RunAlreadyLive(
        "gitlab.example.com/agents/global/develop-1 already has a live session (s-9)"
    ))
    response = client.post(
        "/api/runs/resume", headers=bearer_header(), json=RESUME_BODY
    )

    assert response.status_code == 409
    assert "s-9" in response.json()["detail"]


def test_an_unspawnable_resume_is_a_400(client, monkeypatch):
    refuse_with(monkeypatch, api, spawn.SpawnError("cannot find `lmer` on PATH"))
    response = client.post(
        "/api/runs/resume", headers=bearer_header(), json=RESUME_BODY
    )

    assert response.status_code == 400
    assert "lmer" in response.json()["detail"]


# --- lmer platform resume ----------------------------------------------------

@pytest.fixture
def cli_resumed(monkeypatch, platform_root):
    """Capture what the subcommand hands lmer_platform.resume."""
    calls = []

    def fake_resume(config, request):
        calls.append(request)
        return resume_result(platform_root, request)

    fake_resume.calls = calls
    monkeypatch.setattr(daemon, "resume_run", fake_resume)
    return fake_resume


def test_resume_verb_passes_the_identity_and_every_flag_through(cli_resumed):
    assert daemon.main([
        "resume", RUN_REF,
        "--taskdef", "review",
        "--repo", "https://gitlab.example.com/agents/global",
        "--prompt", "only look at the migration",
    ]) == 0
    request = cli_resumed.calls[0]

    assert (request.host, request.project, request.slug) == (HOST, PROJECT, SLUG)
    assert request.taskdef == "review"
    assert request.repo_url == "https://gitlab.example.com/agents/global"
    assert request.direction == "only look at the migration"


def test_resume_verb_needs_no_flags_at_all(cli_resumed):
    """The one-tap continue, in a shell: the run's recorded taskdef is the default."""
    assert daemon.main(["resume", RUN_REF]) == 0
    request = cli_resumed.calls[0]
    assert (request.taskdef, request.repo_url, request.direction) == (None, None, None)


def test_resume_verb_reports_the_session_it_started(cli_resumed, capsys):
    daemon.main(["resume", RUN_REF])
    out = capsys.readouterr().out

    assert f"resumed {REL_PATH}" in out
    assert "s-resume (pid 4242)" in out
    assert "resume brief" in out, "the note explains why the row has not changed yet"


def test_resume_verb_says_when_it_started_a_sibling_instead(monkeypatch, capsys,
                                                            platform_root):
    """"resumed" would be a lie: the session went to another run entirely."""
    def fake_resume(config, request):
        return resume_result(
            platform_root, request, continued=False, started_slug="review-issue-141"
        )

    monkeypatch.setattr(daemon, "resume_run", fake_resume)
    daemon.main(["resume", RUN_REF, "--taskdef", "review"])
    out = capsys.readouterr().out

    assert f"started {HOST}/{PROJECT}/runs/review-issue-141" in out
    assert f"resumed {REL_PATH}" not in out
    assert "untouched" in out


def test_resume_verb_never_prints_the_direction(cli_resumed, capsys):
    """It is the operator's content: not logged, not evented, not echoed."""
    daemon.main(["resume", RUN_REF, "--prompt", "the staging password is hunter2"])
    captured = capsys.readouterr()
    assert "hunter2" not in captured.out + captured.err


@pytest.mark.parametrize("error,flag", [
    (resume_mod.RepoUrlRequired, "--repo"),
    (resume_mod.DirectionRequired, "--prompt"),
])
def test_resume_verb_translates_a_missing_field_into_its_own_flag(
    monkeypatch, capsys, platform_root, error, flag
):
    """The refusals name an API field ("Supply repo_url"), which no shell user can
    type. The code is what makes translating it possible without rewording it."""
    refuse_with(monkeypatch, daemon, error("Supply repo_url with the resume"))
    assert daemon.main(["resume", RUN_REF]) == 2
    err = capsys.readouterr().err

    assert "Supply repo_url with the resume" in err, "the daemon's own words survive"
    assert flag in err


def test_a_refusal_with_no_flag_to_offer_prints_only_the_refusal(
    monkeypatch, capsys, platform_root
):
    """Every refusal already names the way through; only two of them name a field."""
    refuse_with(monkeypatch, daemon, resume_mod.QuestionOpen("answer it instead"))
    assert daemon.main(["resume", RUN_REF]) == 2
    err = capsys.readouterr().err

    assert "answer it instead" in err
    assert "--repo" not in err and "--prompt" not in err


def test_resume_verb_exit_codes_distinguish_capacity_from_a_refusal(
    monkeypatch, capsys, platform_root
):
    """1 is "try again later", 2 is "change the request" — as with ``spawn``."""
    refuse_with(monkeypatch, daemon, spawn.CapacityError("cap reached: 4/4"))
    assert daemon.main(["resume", RUN_REF]) == 1
    assert "cap reached" in capsys.readouterr().err

    refuse_with(monkeypatch, daemon, spawn.SpawnError("cannot find lmer"))
    assert daemon.main(["resume", RUN_REF]) == 2

    refuse_with(monkeypatch, daemon, resume_mod.NotResumable("not in the mirror"))
    assert daemon.main(["resume", RUN_REF]) == 2


def test_resume_verb_rejects_a_run_reference_it_cannot_split(platform_root, capsys):
    assert daemon.main(["resume", "not-a-ref"]) == 2
    assert "expected <host>/<project>/<slug>" in capsys.readouterr().err


# --- lmer platform spawn --agents --------------------------------------------

def test_spawn_verb_passes_agents_as_a_typed_field(platform_root, monkeypatch):
    """The UI's spawn dialog offers the fan-out roster; the CLI now carries it too.

    A typed field and pointedly not ``extra_args``: the platform emits ``--agents``
    itself and records what it emitted in the session's registry entry, so a second
    spelling later in argv would not collide with the platform's — argparse is
    last-wins, so it would beat it, and the entry would then name a roster the
    session never got (spawn._RESERVED_ARGS refuses exactly that).
    """
    seen = {}

    def capture(config, request):
        seen["request"] = request
        return spawn_result(platform_root, session_id="s-1")

    monkeypatch.setattr(daemon, "spawn_session", capture)
    assert daemon.main([
        "spawn", "develop", "https://example.com/x", "--agents", "sol,fable",
    ]) == 0
    request = seen["request"]

    assert request.agents == "sol,fable"
    assert spawn.AGENTS_FLAG not in request.extra_args
    assert not [arg for arg in request.extra_args if "sol" in arg]
    # And it survives the validation the platform runs before spawning.
    assert request.validate().agents == "sol,fable"


def test_spawn_verb_without_agents_says_nothing_about_them(platform_root,
                                                           monkeypatch):
    """Absent is the ordinary case, and it must not arrive as an empty selection —
    ``lmer`` refuses one that names nobody rather than treating it as a no-op."""
    seen = {}

    def capture(config, request):
        seen["request"] = request
        return spawn_result(platform_root, session_id="s-1")

    monkeypatch.setattr(daemon, "spawn_session", capture)
    daemon.main(["spawn", "develop", "https://example.com/x"])
    assert seen["request"].agents is None


# --- the UI's half of the door -----------------------------------------------
#
# Source-level, for the reason tests/test_platform_web_app.py gives: there is no JS
# test runner in this repo. What is worth pinning here is the seam between the two
# languages — a component that branched on a code the platform does not emit would
# never show the operator the field they are being asked for, and nothing else in
# the tree would notice.

RUN_DETAIL = WEB / "src" / "components" / "RunDetail.vue"
API_CLIENT = WEB / "src" / "api.js"


#: One Node run that exercises the api.js client against a scripted ``fetch``:
#: what it puts on the wire, and what it makes of the two error-body shapes. The
#: assertions are in Python, so the JS half only observes and reports.
_API_PROBE = """
const calls = []
globalThis.fetch = async (path, options) => {
  calls.push({ path, method: options.method, body: options.body })
  const scripted = globalThis.__reply
  if (scripted.raw) throw new Error('network down')
  return { ok: scripted.ok, status: scripted.status,
           json: async () => { if (!scripted.body) throw new Error('not json')
                               return scripted.body } }
}
const api = await import(%s)
const run = { host: 'gitlab.example.com', project: 'agents/global', slug: 'develop-1' }
const seen = {}

globalThis.__reply = { ok: true, status: 202, body: { continued: true } }
seen.reply = await api.resumeRun(run, { taskdef: ' review ', repoUrl: '   ',
                                        direction: '  seed the session  ' })
seen.sent = calls[0]

async function refusal(reply, call) {
  globalThis.__reply = reply
  try {
    await call()
    return { threw: false }
  } catch (exc) {
    return { threw: true, message: exc.message, code: exc.code ?? null,
             status: exc.status }
  }
}

seen.coded = await refusal(
  { ok: false, status: 400,
    body: { detail: { code: 'repo_url_required', message: 'Supply repo_url' } } },
  () => api.resumeRun(run),
)
seen.sentence = await refusal(
  { ok: false, status: 429, body: { detail: 'concurrency cap reached: 4/4' } },
  () => api.answerRun(run, 'yes'),
)
seen.unreadable = await refusal(
  { ok: false, status: 401, body: null }, () => api.resumeRun(run),
)
console.log(JSON.stringify(seen))
"""


@pytest.fixture(scope="module")
def api_client_probe():
    """Run the real api.js against a scripted ``fetch`` and report what it did.

    Executed rather than read, because the branch this slice added to ``request``
    is the one place a mistake is invisible in the source: a ``{code, message}``
    body rendered by the old code path reads as ``[object Object]`` in an alert,
    which no grep for a string would notice. Module-scoped — one Node start.

    Missing Node skips, unless the host says it has one
    (:func:`tests.conftest.require_node_toolchain`): a guard that can be satisfied
    by not running is the bug that helper exists to catch.
    """
    import subprocess

    from tests.conftest import require_node_toolchain

    node = node_binary()
    if not node:
        require_node_toolchain("no Node available (run `lmer platform setup-ui`)")
    result = subprocess.run(
        [node, "--input-type=module", "-e", _API_PROBE % json.dumps(str(API_CLIENT))],
        cwd=str(WEB), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"the api.js probe failed:\n{result.stdout}\n{result.stderr}"
    )
    return json.loads(result.stdout)


def test_the_ui_reaches_the_resume_route_through_the_api_client():
    """Every other view posts through api.js; this one does not get to be special."""
    client = API_CLIENT.read_text(encoding="utf-8")
    assert "export function resumeRun(" in client
    assert "request('api/runs/resume'" in client, (
        "a relative path, like every other call here — the UI is served by the "
        "daemon it talks to and may sit behind a proxy on a subpath"
    )
    assert "'/api/runs/resume'" not in client

    detail = RUN_DETAIL.read_text(encoding="utf-8")
    assert "resumeRun" in detail
    assert "from '../api.js'" in detail


def test_the_client_sends_only_what_the_operator_filled_in(api_client_probe):
    """Executed, not read. A blank box is "I did not say", so it is left out — and
    the whitespace comes off, because a taskdef is a directory name in the
    container and " review " is not one."""
    sent = api_client_probe["sent"]

    assert sent["path"] == "api/runs/resume"
    assert sent["method"] == "POST"
    assert json.loads(sent["body"]) == {
        "host": "gitlab.example.com",
        "project": "agents/global",
        "slug": "develop-1",
        "taskdef": "review",
        "direction": "seed the session",
    }, "a blank repo URL must not be sent as an empty string"
    assert api_client_probe["reply"] == {"continued": True}


def test_the_client_unwraps_a_coded_refusal_without_breaking_the_others(
    api_client_probe
):
    """The failure this catches is silent in the source: a ``{code, message}`` body
    handled by the old branch reaches an alert as ``[object Object]``, and the
    operator is shown that instead of the sentence telling them what to supply."""
    coded = api_client_probe["coded"]
    assert coded["threw"] is True
    assert coded["message"] == "Supply repo_url", "the daemon's prose is what shows"
    assert coded["code"] == "repo_url_required"
    assert coded["status"] == 400

    # Every other route answers with a sentence, and none of them grew a code.
    sentence = api_client_probe["sentence"]
    assert sentence["message"] == "concurrency cap reached: 4/4"
    assert (sentence["code"], sentence["status"]) == (None, 429)

    # A 401 challenge page is not JSON at all; the status is all there is to say.
    unreadable = api_client_probe["unreadable"]
    assert unreadable["message"] == "HTTP 401"
    assert unreadable["code"] is None


def test_the_api_client_carries_the_refusal_code_to_the_caller():
    """Without this the two "one more field" refusals are indistinguishable prose."""
    client = API_CLIENT.read_text(encoding="utf-8")
    assert "error.code = code" in client
    assert "body?.detail?.message" in client, (
        "the message is what gets shown; a {code, message} body must not be "
        "rendered as [object Object]"
    )


def test_the_ui_branches_on_the_codes_the_platform_actually_emits():
    """The cross-language pin. A component matching a code the platform does not
    send never reveals the field the operator is being asked for, and the refusal
    reads as a dead end — which is the state this whole slice exists to end."""
    detail = RUN_DETAIL.read_text(encoding="utf-8")
    for cls in (resume_mod.RepoUrlRequired, resume_mod.DirectionRequired):
        assert f"'{cls.code}'" in detail, (
            f"RunDetail.vue does not know the {cls.code!r} refusal"
        )


def test_a_request_for_one_more_field_does_not_look_like_a_failure():
    """Being asked for the repository URL is the designed path, not a breakage.

    Every resume refusal now arrives with a code, so branching on "is there a code"
    would paint all of them the same and the distinction the codes exist for would
    be gone. These two are the ones that are answered by typing something.
    """
    detail = RUN_DETAIL.read_text(encoding="utf-8")
    decision = re.search(
        r"const resumeAsksForAField = computed\(\(\) => \((.*?)\)\)", detail, re.S
    )
    assert decision, "nothing tells a request for a field from a failure"
    assert "RESUME_NEEDS_REPO_URL" in decision.group(1)
    assert "RESUME_NEEDS_DIRECTION" in decision.group(1)
    assert "resumeAsksForAField ? 'warning' : 'error'" in detail, (
        "the two refusals are not rendered differently"
    )


def test_the_repo_url_field_appears_when_the_platform_asks_for_it():
    """The refusal is a request for a field, so there has to be a field."""
    detail = RUN_DETAIL.read_text(encoding="utf-8")
    assert "repoUrlAsked" in detail
    assert 'v-if="repoUrlAsked"' in detail, "the field is never revealed"
    assert "v-model=\"resumeRepoUrl\"" in detail
    assert "repoUrl: resumeRepoUrl.value" in detail, (
        "a revealed field that is not sent is worse than no field at all"
    )


def test_resuming_is_offered_only_when_there_is_nothing_running():
    """A run with a live session is one to read or wind down. The daemon refuses a
    second container for it (liveness outranks committed run state, spec D24), and
    a button that is always refused is not an affordance."""
    detail = RUN_DETAIL.read_text(encoding="utf-8")
    assert "const canResume = computed(() => !props.run.live)" in detail
    assert 'v-if="canResume"' in detail


def test_the_terminal_is_still_only_loaded_when_it_is_opened():
    """The emulator is over half the JS in the bundle; this view is the only one
    that opens one, and a phone must not fetch it to read a run's facts."""
    detail = RUN_DETAIL.read_text(encoding="utf-8")
    assert "defineAsyncComponent(() => import('./Terminal.vue'))" in detail


def test_the_resume_section_hardcodes_no_colour_and_no_swept_out_variant():
    """House style: the theme owns colour, and `outlined`/`flat` were swept out of
    every component — tonal and elevated are what this app uses.

    The hex scan runs on the comment-stripped source: an MR reference in a comment
    ("MR #164") is hex-shaped to a regex, and a colour cannot act from inside a
    comment — the same reading test_platform_lifecycle.py gives this guard.
    """
    detail = RUN_DETAIL.read_text(encoding="utf-8")
    detail = re.sub(r"<!--.*?-->", "", detail, flags=re.DOTALL)
    detail = re.sub(r"^\s*//.*$", "", detail, flags=re.MULTILINE)
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", detail)
    assert 'variant="outlined"' not in detail
    assert 'variant="flat"' not in detail


def test_the_run_detail_view_renders_no_raw_html():
    """Agent-authored prose goes through Markdown.vue, which owns the app's only
    v-html. Nothing in this view hand-rolls a second one."""
    assert "v-html" not in RUN_DETAIL.read_text(encoding="utf-8")
