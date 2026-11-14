"""Authentication, request binding and health stay off the event loop (#561).

``require_secret`` is an async dependency, so the project binding it ran —
sqlite reads, and for an assistant caller a full registry scan — ran on the event
loop thread, and every request waited while it did, ``/api/health`` included.
Health itself, and authentication, needed a thread from the pool the handlers
share, so a full pool hid behind a health check that timed out.

Every test drives one app through ``with TestClient(app)``: one portal, one event
loop, which is the only arrangement in which "the loop was blocked" can show.
"""

import threading
import time

import anyio.to_thread
import pytest
from fastapi.testclient import TestClient

from lmer_platform import api, load, projects, store
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env

SECRET = "test-secret-value"
AUTH = {"Authorization": f"Bearer {SECRET}"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    strip_lmer_env(monkeypatch)
    monkeypatch.delenv("LMER_WORK_REPO", raising=False)
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    monkeypatch.setattr(load, "BUILDS", load.BuildStats())


@pytest.fixture
def config():
    return cfg.load()


@pytest.fixture
def projects_mode():
    projects.migrate()
    return projects.create("Beta")["id"]


def _payload():
    return {"schema": 1, "runs": [], "sessions": [], "attention": [], "counts": {},
            "totals": {"runs": 0, "live": 0, "attention": 0}}


def _answers_within(client, path, seconds, headers=AUTH):
    """Fetch *path* from another thread; the response, or None if it took too long."""
    result = {}
    thread = threading.Thread(target=lambda: result.update(response=client.get(path, headers=headers)))
    thread.start()
    thread.join(seconds)
    return result.get("response"), thread


def test_a_blocked_binding_does_not_stall_other_requests(config, projects_mode, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real_get = projects.get

    def slow_get(workspace_id, **kwargs):
        if workspace_id == projects.GENERAL and not release.is_set():
            entered.set()
            release.wait(20)  # a sqlite busy wait, as the binding met on the host
        return real_get(workspace_id, **kwargs)

    monkeypatch.setattr(projects, "get", slow_get)
    app = api.create_app(config, SECRET, state_builder=lambda *_a, **_k: _payload())
    with TestClient(app) as client:
        held = threading.Thread(target=lambda: client.get(
            "/api/state", headers={**AUTH, "X-Lmer-Workspace-Id": projects.GENERAL}))
        held.start()
        assert entered.wait(10)
        response, waiter = _answers_within(client, "/api/health", 5)
        release.set()
        held.join(20)
        waiter.join(20)
    assert response is not None, "/api/health waited for another request's binding"
    assert response.status_code == 200


def test_health_answers_while_every_control_thread_holds_a_blocked_binding(config, projects_mode, monkeypatch):
    """The review's N-blocked variant: health shared the control limiter, so
    CONTROL_THREADS blocked bindings left it unable to answer at all (#561)."""
    entered, release = threading.Semaphore(0), threading.Event()
    real_get = projects.get

    def slow_get(workspace_id, **kwargs):
        if workspace_id == projects.GENERAL and not release.is_set():
            entered.release()
            release.wait(30)
        return real_get(workspace_id, **kwargs)

    monkeypatch.setattr(projects, "get", slow_get)
    app = api.create_app(config, SECRET, state_builder=lambda *_a, **_k: _payload())
    held = []
    with TestClient(app) as client:
        try:
            for _ in range(api.CONTROL_THREADS):
                thread = threading.Thread(target=lambda: client.get(
                    "/api/state", headers={**AUTH, "X-Lmer-Workspace-Id": projects.GENERAL}))
                thread.start()
                held.append(thread)
            for _ in range(api.CONTROL_THREADS):
                assert entered.acquire(timeout=15), "not every control thread reached the binding"
            response, waiter = _answers_within(client, "/api/health", 5)
        finally:
            release.set()
            for thread in held:
                thread.join(30)
        waiter.join(20)
    assert response is not None, "/api/health waited behind the blocked bindings"
    assert response.status_code == 200


def test_health_still_refuses_what_its_binding_refuses(config, projects_mode):
    """Health binds on its own limiter, with the same checks as every route."""
    with TestClient(api.create_app(config, SECRET, state_builder=lambda *_a, **_k: _payload())) as client:
        response = client.get("/api/health", headers={**AUTH, "X-Lmer-Workspace-Id": "no-such-project"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "project_not_found"


def test_health_answers_while_the_handler_pool_is_full(config):
    entered, release = threading.Event(), threading.Event()

    def builder(config, *, force_pull=False):
        entered.set()
        release.wait(20)
        return _payload()

    app = api.create_app(config, SECRET, state_builder=builder)
    with TestClient(app) as client:
        limiter = client.portal.call(anyio.to_thread.current_default_thread_limiter)
        total = limiter.total_tokens
        client.portal.call(setattr, limiter, "total_tokens", 1)
        try:
            held = threading.Thread(target=lambda: client.get("/api/state", headers=AUTH))
            held.start()
            assert entered.wait(10)
            time.sleep(1.5)  # one loop-monitor sample with the pool full
            response, waiter = _answers_within(client, "/api/health", 5)
            release.set()
            held.join(20)
            waiter.join(20)
        finally:
            release.set()
            client.portal.call(setattr, limiter, "total_tokens", total)
    assert response is not None, "/api/health needed a thread from the full handler pool"
    pool = response.json()["load"]["pool"]
    assert pool["total"] == 1
    assert pool["borrowed"] == 1


def test_the_bound_project_still_reaches_the_handler(config, projects_mode):
    with TestClient(api.create_app(config, SECRET, state_builder=lambda *_a, **_k: _payload())) as client:
        for workspace_id in (projects.GENERAL, projects_mode):
            body = client.get("/api/assistant", headers={**AUTH, "X-Lmer-Workspace-Id": workspace_id}).json()
            assert body["workspace_id"] == workspace_id


@pytest.mark.parametrize("path, headers, status, code", [
    # Two destinations that disagree.
    ("/api/assistant?workspace_id=nope-a", {"X-Lmer-Workspace-Id": "general"}, 409, "project_invalid"),
    # A project that does not exist.
    ("/api/assistant", {"X-Lmer-Workspace-Id": "no-such-project"}, 404, "project_not_found"),
    # A project-scoped route with no project named.
    ("/api/assistant", {}, 400, "project_required"),
])
def test_binding_refusals_keep_their_status_and_code(config, projects_mode, path, headers, status, code):
    with TestClient(api.create_app(config, SECRET, state_builder=lambda *_a, **_k: _payload())) as client:
        response = client.get(path, headers={**AUTH, **headers})
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code


def test_authentication_refusals_are_unchanged(config):
    with TestClient(api.create_app(config, SECRET, state_builder=lambda *_a, **_k: _payload())) as client:
        refused = client.get("/api/health", headers={"Authorization": "Bearer wrong"})
        missing = client.get("/api/health")
    assert refused.status_code == missing.status_code == 401
    assert refused.headers["WWW-Authenticate"].startswith("Basic")
