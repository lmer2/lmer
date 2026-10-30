"""Transient state contention answers a retryable 503, never an opaque 500.

``POST /api/runs/unmute`` once answered 500 and succeeded on a retry. The cause
was not recorded; the two candidates are a busy ``projects.sqlite3`` (the
request binding reads it before the route runs) and a failed state write in the
mute store. Both are pinned here: each fails once, the client is told to retry,
the mute state is as it was, and the retry succeeds.

A busy database past the binding is still a 503, but not one that promises a
retry is safe: the handler may have applied part of a request that is not
idempotent (a spawn, a project create).
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api, mute, projects, runs, store
from lmer_platform import config as cfg
from lmer_platform import project_context as scope
from tests.conftest import strip_lmer_env
from tests.test_platform_projects import fleet  # noqa: F401

SECRET = "test-secret-value"
HOST, PROJECT, SLUG = "gitlab.example.com", "group/project", "chat-1"
RUN = (HOST, PROJECT, SLUG)
BODY = {"host": HOST, "project": PROJECT, "slug": SLUG}


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def muted(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    runs.track(*RUN, source="spawned", taskdef="chat")
    mute.set_run(*RUN, muted=True)
    return mute._path().read_bytes()


def auth():
    return {"Authorization": f"Bearer {SECRET}"}


def fail_once(monkeypatch, target, name, error):
    original = getattr(target, name)
    calls = []

    def flaky(*args, **kwargs):
        calls.append(name)
        if len(calls) == 1:
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, flaky)
    return calls


def locked_error(tmp_path):
    """A real ``database is locked``, carrying the code sqlite3 sets on one."""
    path = tmp_path / "busy.sqlite3"
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("CREATE TABLE t (x)")
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            sqlite3.connect(path, timeout=0).execute("INSERT INTO t VALUES (1)")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
    return caught.value


def assert_retryable(reply):
    assert reply.status_code == 503, reply.text
    assert reply.headers["retry-after"] == api.RETRY_AFTER_SECONDS
    detail = reply.json()["detail"]
    assert detail["code"] == "state_busy" and detail["retryable"] is True
    assert "retry" in detail["message"]


def test_a_failed_mute_write_is_a_retryable_503(muted, monkeypatch):
    client = TestClient(api.create_app(
        cfg.load(), SECRET, state_builder=lambda config, force_pull=False: {},
    ))
    calls = fail_once(monkeypatch, mute, "_save", store.StoreError("injected: disk said no"))

    reply = client.post("/api/runs/unmute", json=BODY, headers=auth())
    assert_retryable(reply)
    assert "disk said no" in reply.json()["detail"]["message"]
    assert mute._path().read_bytes() == muted, "the failed unmute changed the mute state"
    assert mute.is_muted(RUN)

    reply = client.post("/api/runs/unmute", json=BODY, headers=auth())
    assert reply.status_code == 200, reply.text
    assert reply.json()["changed"] is True
    assert not mute.is_muted(RUN)
    assert calls == ["_save", "_save"]


def test_a_busy_project_database_in_the_binding_is_a_retryable_503(
    fleet, tmp_path, monkeypatch  # noqa: F811
):
    projects.migrate()
    with scope.bound(projects.GENERAL):
        runs.track(*RUN, source="spawned", taskdef="chat")
    secret = cfg.ensure_secret(fleet)
    client = TestClient(
        api.create_app(fleet, secret, state_builder=lambda *_a, **_k: {}),
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert client.post("/api/runs/mute", json=BODY).status_code == 200
    before = mute._path().read_bytes()
    calls = fail_once(monkeypatch, projects, "run_home", locked_error(tmp_path))

    reply = client.post("/api/runs/unmute", json=BODY)
    assert_retryable(reply)
    assert "nothing was applied" in reply.json()["detail"]["message"]
    assert calls == ["run_home"], "the failure must come from the request binding"
    assert mute._path().read_bytes() == before
    assert mute.is_muted(RUN)

    reply = client.post("/api/runs/unmute", json=BODY)
    assert reply.status_code == 200, reply.text
    assert not mute.is_muted(RUN)


def test_a_busy_database_inside_a_handler_does_not_promise_a_safe_retry(
    fleet, tmp_path, monkeypatch  # noqa: F811
):
    """Past the binding the handler may have done part of the work."""
    projects.migrate()
    secret = cfg.ensure_secret(fleet)
    client = TestClient(
        api.create_app(fleet, secret, state_builder=lambda *_a, **_k: {}),
        headers={"Authorization": f"Bearer {secret}"},
    )
    calls = fail_once(monkeypatch, projects, "create", locked_error(tmp_path))

    reply = client.post("/api/projects", json={"name": "Alpha"})
    assert reply.status_code == 503, reply.text
    assert reply.headers["retry-after"] == api.RETRY_AFTER_SECONDS
    detail = reply.json()["detail"]
    assert detail["code"] == "state_busy" and detail["retryable"] is False
    assert "may have partly applied" in detail["message"]
    assert "only if it is idempotent" in detail["message"]
    assert calls == ["create"]


def test_another_sqlite_failure_is_still_a_500(fleet, monkeypatch):  # noqa: F811
    """Only contention is transient; a broken database must not ask for retries."""
    projects.migrate()
    secret = cfg.ensure_secret(fleet)
    client = TestClient(
        api.create_app(fleet, secret, state_builder=lambda *_a, **_k: {}),
        headers={"Authorization": f"Bearer {secret}"},
        raise_server_exceptions=False,
    )
    calls = fail_once(
        monkeypatch, projects, "run_home", sqlite3.OperationalError("no such table: homes")
    )

    assert client.post("/api/runs/unmute", json=BODY).status_code == 500
    assert calls == ["run_home"]
