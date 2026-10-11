"""Request admission, authorized launch attachment and safe completion via HTTP."""
import json
import os
from unittest.mock import patch
from types import SimpleNamespace

import pytest

from lmer_platform import api, comms, config, lifecycle, project_requests, project_threads, projects, spawn, store
from lmer_cli import assistant_mentions, thread_access
from lmer_platform import client as platform_client, connectors, daemon
from slack_chat.client import SlackError
from tests.test_platform_projects import fleet  # noqa: F401
from tests.test_platform_project_transports import transport_host, _link, _post, _next  # noqa: F401
from tests.test_project_review_requests import credential, actions
from tests.test_assistant_mentions import body


def source_request(host, fleet, transport):
    client, _transports, _component = host
    project = projects.create("Example")
    projects.reconcile(fleet)
    request = body(transport, threaded=True)
    project_threads.choose_unbound(_link(request), project["id"])
    identity = _post(client, request)["request_id"]
    headers = credential(project["id"])
    taken = client.post("/api/assistant/pending", headers=headers)
    assert taken.status_code == 200, taken.text
    assert taken.json()["pending"][0]["data"]["request_id"] == identity
    return project, request, identity, headers


def launch(client, request, headers, target="source-review"):
    payload = {"taskdef": "chat", "target": target, "repo_url": "https://forge.test/example/repository",
               "targets": [_link(request)]}
    response = client.post("/api/sessions", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()["session_id"], payload


def stored_actions(identity):
    with projects.database() as db:
        return [dict(row) for row in db.execute("SELECT * FROM request_actions WHERE request_id=? ORDER BY rowid", (identity,))]


def move_worker(worker, destination):
    key = projects.session_run(worker)
    home = projects.run_home(key)
    lifecycle.exit_session(worker)
    assert spawn.wait_for_exit_recorded(worker)
    projects.move(key, destination, expected_project=home["project_id"])
    return key


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_source_attachment_allows_brief_reply_and_release(transport_host, fleet, monkeypatch, transport):
    client, transports, component = transport_host
    project, request, identity, headers = source_request(transport_host, fleet, transport)
    payload = {"taskdef": "chat", "target": "source-review", "repo_url": "https://forge.test/example/repository",
               "targets": [_link(request)]}
    first = client.post("/api/sessions", headers=headers, json=payload)
    assert first.status_code == 201, first.text
    worker = first.json()["session_id"]
    assert len(component.records(worker)) == 1
    assert [row["state"] for row in actions(identity)] == ["complete"]
    briefs = []

    def post(session, **message):
        briefs.append((session, message))
        return {"message": {"id": "brief", **message}}

    monkeypatch.setattr(comms, "ensure_hub", lambda: SimpleNamespace(post=post))
    brief = client.post(f"/api/sessions/{worker}/comms", headers=headers,
                        json={"kind": "message", "text": "Review the documented behavior"})
    assert brief.status_code == 200, brief.text
    assert [item[0] for item in briefs] == [worker]
    reply = client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Worker started"})
    assert reply.status_code == 200, reply.text
    assert transports[transport].posts == [(request["conversation"], project_requests.get(identity)["root"], "Worker started")]
    duplicate = client.post("/api/sessions", headers=headers, json=payload)
    assert duplicate.status_code == 201, duplicate.text
    assert duplicate.json()["session_id"] == worker
    assert client.post("/api/assistant/pending", headers={**headers, "X-Lmer-Request-Id": identity}).json()["pending"] == []
    assert project_requests.active(project["id"]) is None
    assert project_requests.get(identity)["state"] == "replied"
    assert _post(client, request)["status"] == "duplicate"
    launched = [event for event in store.read_events() if event["type"] == "session_spawned"
                and event["data"]["target"] == "source-review"]
    assert len(launched) == 1


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_rotation_retains_causal_attachment_and_completed_spawn(transport_host, fleet, transport):
    client, transports, _component = transport_host
    project, request, identity, headers = source_request(transport_host, fleet, transport)
    worker, payload = launch(client, request, headers)
    receipt = stored_actions(identity)[0]
    transition = json.loads(receipt["result"])["ownership_transition"]
    assert transition["session_id"] == worker
    assert len(transition["after"]) == len(transition["before"]) + 1
    projects.rotate(project["id"], fleet)
    denied = client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Stale generation"})
    assert denied.status_code in (401, 409), denied.text
    headers = credential(project["id"])
    pending = client.post("/api/assistant/pending", headers=headers).json()["pending"]
    assert pending[0]["data"]["request_id"] == identity
    assert pending[0]["data"]["actions"][0]["state"] == "complete"
    repeated = client.post("/api/sessions", headers=headers, json=payload)
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["session_id"] == worker
    assert stored_actions(identity) == [receipt]
    reply = client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Existing worker retained"})
    assert reply.status_code == 200, reply.text
    assert client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Do not repeat"}).json()["duplicate"]
    assert len(transports[transport].posts) == 1
    assert client.post("/api/assistant/pending", headers=headers).json()["pending"] == []
    assert project_requests.active(project["id"]) is None


@pytest.mark.parametrize("change", ["same-project", "foreign", "revision"])
@pytest.mark.parametrize("restart", [False, True])
def test_unrelated_change_is_refused_then_released_without_replay(transport_host, fleet, change, restart):
    client, transports, component = transport_host
    project, request, identity, headers = source_request(transport_host, fleet, "slack")
    worker, payload = launch(client, request, headers)
    receipt = stored_actions(identity)[0]
    if change == "same-project":
        config.update_stored({"max_concurrent_sessions": 2})
        other = spawn.spawn_session(fleet, spawn.SpawnRequest("chat", "independent", repo_url="https://forge.test/example/repository",
                                                            workspace_id=project["id"]))
        component.adopt_launch_targets(other.session_id, [_link(request)], by="operator")
    else:
        other_project = projects.create("Other")
        key = move_worker(worker, other_project["id"])
        if change == "revision":
            projects.move(key, project["id"], expected_project=other_project["id"])
    for path, data in [(f"/api/sessions/{worker}/input", {"text": "Brief"}),
                       (f"/api/assistant/requests/{identity}/reply", {"text": "Reply"}),
                       ("/api/assistant/handoff", {"handoff": "Do not write under a blocked request"})]:
        result = client.post(path, headers=headers, json=data)
        assert result.status_code == 409, result.text
    assert project_requests.get(identity)["state"] == "delivered"
    visible = client.get("/api/assistant/requests").json()["requests"]
    row = next(item for item in visible if item["id"] == identity)
    assert row["error"] and row["recovery"]["release_allowed"]
    assert row["actions"][0]["state"] == "complete"
    assert stored_actions(identity) == [receipt]
    if restart:
        projects.rotate(project["id"], fleet)
        headers = credential(project["id"])
    taken = client.post("/api/assistant/pending", headers=headers)
    assert taken.status_code == 200, taken.text
    unavailable = next(note for note in taken.json()["pending"] if note["kind"] == "request_unavailable")
    assert unavailable["data"]["request_id"] == identity
    assert unavailable["data"]["released"] and not unavailable["data"]["reply_sent"]
    assert unavailable["data"]["actions"][0]["state"] == "complete"
    assert project_requests.active(project["id"]) is None
    assert client.post("/api/assistant/pending", headers=headers).json()["pending"] == []
    handoff = client.post("/api/assistant/handoff", headers=headers, json={"handoff": "Recovery preserved the existing worker receipt"})
    assert handoff.status_code == 200, handoff.text
    assert stored_actions(identity) == [receipt], "Unrelated work was attributed to the old request"
    assert transports["slack"].posts == []
    assert _post(client, request)["status"] == "duplicate"
    assert transports["slack"].posts == []
    repeated = client.post("/api/sessions", headers={**headers, "X-Lmer-Request-Id": identity}, json=payload)
    assert repeated.status_code == 409, repeated.text


@pytest.mark.parametrize("during", ["execution", "attachment", "receipt"])
def test_unrelated_ownership_change_cannot_be_laundered_into_spawn_transition(transport_host, fleet, monkeypatch, during):
    client, _transports, component = transport_host
    project, request, identity, headers = source_request(transport_host, fleet, "slack")
    beta = projects.create("Other")
    outsider = spawn.spawn_session(fleet, spawn.SpawnRequest("chat", "outsider", no_repo=True, workspace_id=beta["id"]))
    config.update_stored({"max_concurrent_sessions": 2})

    def change():
        component.adopt_launch_targets(outsider.session_id, [_link(request)], by="operator")
        project_threads.remember(outsider.session_id, _link(request))

    if during == "execution":
        original = spawn._build_command
        def build(*args, **kwargs):
            result = original(*args, **kwargs)
            change()
            return result
        monkeypatch.setattr(spawn, "_build_command", build)
    elif during == "attachment":
        original = project_requests.attach_spawned
        def attach(*args, **kwargs):
            change()
            return original(*args, **kwargs)
        monkeypatch.setattr(project_requests, "attach_spawned", attach)
    else:
        original = api.spawn_session
        def perform(*args, **kwargs):
            result = original(*args, **kwargs)
            change()
            return result
        monkeypatch.setattr(api, "spawn_session", perform)
    result = client.post("/api/sessions", headers=headers,
                         json={"taskdef": "chat", "target": "source-review", "repo_url": "https://forge.test/example/repository",
                               "targets": [_link(request)]})
    assert result.status_code == (409 if during == "execution" else 201), result.text
    receipts = stored_actions(identity)
    assert len(receipts) == 1
    assert receipts[0]["state"] == ("refused" if during == "execution" else "complete")
    if receipts[0]["result"]:
        assert "ownership_transition" not in json.loads(receipts[0]["result"])
    assert client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Wrong owner"}).status_code == 409


def test_operator_release_is_scoped_safe_and_idempotent(transport_host, fleet):
    client, transports, _component = transport_host
    project, request, identity, headers = source_request(transport_host, fleet, "matrix")
    worker, _payload = launch(client, request, headers)
    receipt = stored_actions(identity)[0]
    path = f"/api/assistant/requests/{identity}/release"
    reason = {"reason": "Preserve completed work and allow independent instructions"}
    assert client.post(path, json=reason).status_code == 409
    beta = projects.create("Other")
    move_worker(worker, beta["id"])
    assert client.post(path, headers=headers, json=reason).status_code == 403
    assert client.post(path, json={**reason, "workspace_id": beta["id"]}).status_code == 409
    assert client.post(path, json={"reason": ""}).status_code == 400
    assert client.post(path, json=reason).json()["released"]
    assert client.post(path, json=reason).json()["released"]
    assert project_requests.active(project["id"]) is None
    assert stored_actions(identity) == [receipt]
    assert transports["matrix"].posts == []
    visible = next(row for row in client.get("/api/assistant/requests").json()["requests"] if row["id"] == identity)
    assert visible["recovery"]["released"] and not visible["recovery"]["release_allowed"]
    assert client.post("/api/assistant/handoff", headers=headers, json={"handoff": "Safe to rotate after release"}).status_code == 200


def test_pending_release_does_not_take_next_request_in_the_old_context(transport_host, fleet):
    client, _transports, component = transport_host
    project, request, identity, headers = source_request(transport_host, fleet, "slack")
    worker, _payload = launch(client, request, headers)
    receipt = stored_actions(identity)[0]
    config.update_stored({"max_concurrent_sessions": 2})
    other = spawn.spawn_session(fleet, spawn.SpawnRequest("chat", "independent", no_repo=True, workspace_id=project["id"]))
    component.adopt_launch_targets(other.session_id, [_link(request)], by="operator")
    second = _post(client, _next(request))["request_id"]
    wrong = client.post("/api/assistant/pending", headers={**headers, "X-Lmer-Request-Id": second})
    assert wrong.status_code == 409, wrong.text
    assert project_requests.get(identity)["state"] == "delivered"
    notes = client.post("/api/assistant/pending", headers=headers).json()["pending"]
    assert [note["kind"] for note in notes] == ["request_unavailable"]
    assert project_requests.get(identity)["state"] == "invalid"
    assert project_requests.get(second)["state"] == "queued"
    # Taking the next request is a separate boundary before any new action.
    notes = client.post("/api/assistant/pending", headers=headers).json()["pending"]
    assert [note["data"]["request_id"] for note in notes] == [second]
    assert stored_actions(identity) == [receipt]


@pytest.mark.parametrize("effect", ["spawn", "brief", "reply"])
def test_uncertainty_never_becomes_safe_release_or_duplicate_work(transport_host, fleet, monkeypatch, effect):
    client, transports, _component = transport_host
    project, request, identity, headers = source_request(transport_host, fleet, "slack")
    calls = []
    if effect == "spawn":
        original = api.spawn_session
        def interrupted(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(result.session_id)
            raise OSError("Fixture result lost after launch")
        with monkeypatch.context() as patch:
            patch.setattr(api, "spawn_session", interrupted)
            with pytest.raises(OSError, match="after launch"):
                launch(client, request, headers)
        worker = calls[0]
    else:
        worker, _payload = launch(client, request, headers)
        if effect == "brief":
            def interrupted(session, **message):
                calls.append((session, message))
                raise OSError("Fixture result lost after brief acceptance")
            monkeypatch.setattr(comms, "ensure_hub", lambda: SimpleNamespace(post=interrupted))
            with pytest.raises(OSError, match="brief acceptance"):
                client.post(f"/api/sessions/{worker}/comms", headers=headers, json={"text": "Brief"})
        else:
            original = transports["slack"].post_message
            def interrupted(*args, **kwargs):
                original(*args, **kwargs)
                calls.append(1)
                raise SlackError("Fixture result lost after reply acceptance")
            monkeypatch.setattr(transports["slack"], "post_message", interrupted)
            response = client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Reply"})
            assert response.status_code == 502, response.text
    receipts = stored_actions(identity)
    beta = projects.create("Other")
    move_worker(worker, beta["id"])
    for restart in (False, True):
        if restart:
            projects.rotate(project["id"], fleet)
            headers = credential(project["id"])
        for _ in range(2):
            assert client.post("/api/assistant/pending", headers={**headers, "X-Lmer-Request-Id": identity}).json()["pending"] == []
            assert project_requests.active(project["id"]) == identity
        response = client.post(f"/api/assistant/requests/{identity}/release", json={"reason": "Do not guess the outcome"})
        assert response.status_code == 409, response.text
        assert stored_actions(identity) == receipts
        visible = next(row for row in client.get("/api/assistant/requests").json()["requests"] if row["id"] == identity)
        assert not visible.get("recovery") or not visible["recovery"]["release_allowed"]
        assert len(calls) == 1
    if effect == "reply":
        assert visible["state"] == "reply_uncertain"
        assert client.post(f"/api/assistant/requests/{identity}/park", json={"reason": "Operator will inspect the existing reply"}).status_code == 200
        assert len(transports["slack"].posts) == 1
    else:
        assert any(row["state"] == "uncertain" for row in receipts)
        assert "uncertain" in visible["recovery"]["instruction"]


@pytest.mark.parametrize("action", ["brief", "reply"])
@pytest.mark.parametrize("change", ["revision", "generation"])
def test_execution_rechecks_before_brief_or_source_send(transport_host, fleet, monkeypatch, action, change):
    client, transports, _component = transport_host
    project, request, identity, headers = source_request(transport_host, fleet, "slack")
    worker, _payload = launch(client, request, headers)
    spawn_receipt = stored_actions(identity)[0]
    sends = []
    def changed():
        if change == "revision":
            beta = projects.create("Other")
            key = move_worker(worker, beta["id"])
            projects.move(key, project["id"], expected_project=beta["id"])
        else:
            projects.rotate(project["id"], fleet)
    if action == "brief":
        def hub():
            changed()
            return SimpleNamespace(post=lambda *args, **kwargs: sends.append(args))
        monkeypatch.setattr(comms, "ensure_hub", hub)
        path = f"/api/sessions/{worker}/comms"
    else:
        original = thread_access.resolve_thread
        def resolve(*args, **kwargs):
            result = original(*args, **kwargs)
            changed()
            return result
        monkeypatch.setattr(thread_access, "resolve_thread", resolve)
        path = f"/api/assistant/requests/{identity}/reply"
    result = client.post(path, headers=headers, json={"text": "Must not send"})
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == ("request_ownership_changed" if change == "revision" else "assistant_generation_changed")
    assert sends == transports["slack"].posts == []
    receipts = stored_actions(identity)
    assert receipts[0] == spawn_receipt
    if action == "brief":
        assert receipts[1]["state"] == "refused"
    else:
        assert receipts == [spawn_receipt]
    assert project_requests.get(identity)["state"] in ("delivered", "queued")


def test_generation_change_during_spawn_preparation_is_a_known_refusal(transport_host, fleet, monkeypatch):
    client, _transports, _component = transport_host
    project, request, identity, headers = source_request(transport_host, fleet, "slack")
    original = spawn._build_command
    def build(*args, **kwargs):
        command = original(*args, **kwargs)
        monkeypatch.setattr(spawn, "_build_command", original)
        projects.stop(project["id"])
        return command
    monkeypatch.setattr(spawn, "_build_command", build)
    result = client.post("/api/sessions", headers=headers, json={"taskdef": "chat", "target": "stale-generation",
                         "repo_url": "https://forge.test/example/repository", "targets": [_link(request)]})
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "assistant_generation_changed"
    assert [row["state"] for row in stored_actions(identity)] == ["refused"]
    assert not any(event["type"] == "session_spawned" and event["data"].get("target") == "stale-generation"
                   for event in store.read_events())


def test_daemon_mention_spawn_brief_reply_release(transport_host, fleet, monkeypatch):
    client, transports, _component = transport_host
    project = projects.create("Example")
    projects.reconcile(fleet)
    event = body("slack", threaded=True)
    link = _link(event)
    for name in ("_supervise_assistant", "_watch_for_attention", "_subscribe_to_comms", "_serve_thread_requests"):
        monkeypatch.setattr(daemon, name, lambda *args: SimpleNamespace(stop=lambda: None))
    monkeypatch.setattr(daemon, "reattach_all", lambda **_: [])
    monkeypatch.setattr(connectors.ThreadConnectors, "start", lambda self: None)
    monkeypatch.setattr(connectors, "_slack_client", lambda _token: transports["slack"])
    admissions, briefs = [], []
    def request(endpoint, call, **kwargs):
        assert endpoint.base_url == "http://127.0.0.1:7143"
        result = client.request(call.method, call.path, json=call.body, headers=endpoint.headers())
        assert result.status_code == 200, result.text
        admissions.append(result.json())
        return result
    monkeypatch.setattr(platform_client, "request", request)
    monkeypatch.setattr(comms, "ensure_hub", lambda: SimpleNamespace(
        post=lambda session, **message: briefs.append((session, message)) or {"message": {"id": "brief"}}))
    def serve(_app, **kwargs):
        manager = connectors.get_connectors()
        session = projects.status(project["id"])["session_id"]
        project_threads.choose_unbound(link, project["id"])
        environment = manager._environment(SimpleNamespace(host="127.0.0.1", port=8123, token="fixture-control"), session, {})
        with patch.dict(os.environ, environment, clear=True):
            first = assistant_mentions.forward("slack", event["conversation"], event["event"])
            repeated = assistant_mentions.forward("slack", event["conversation"], event["event"])
        assert len(admissions) == 2
        assert first["request_id"] == repeated["request_id"]
        assert repeated["status"] == "duplicate"
        identity = admissions[0]["request_id"]
        headers = credential(project["id"])
        pending = client.post("/api/assistant/pending", headers=headers).json()["pending"]
        assert [note["data"]["request_id"] for note in pending] == [identity]
        payload = {"taskdef": "chat", "target": "connected-review", "repo_url": "https://forge.test/example/repository", "targets": [link]}
        launched = client.post("/api/sessions", headers=headers, json=payload)
        assert launched.status_code == 201, launched.text
        worker = launched.json()["session_id"]
        assert client.post(f"/api/sessions/{worker}/comms", headers=headers, json={"text": "Review the behavior"}).status_code == 200
        assert [row[0] for row in briefs] == [worker]
        assert client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Review started"}).status_code == 200
        assert transports["slack"].posts == [(event["conversation"], event["event"]["thread_ts"], "Review started")]
        assert client.post("/api/assistant/pending", headers=headers).json()["pending"] == []
        assert project_requests.active(project["id"]) is None
        assert [row["state"] for row in stored_actions(identity)] == ["complete", "complete"]
    monkeypatch.setattr("uvicorn.run", serve)
    try:
        assert daemon.main(["run", "--bind", "127.0.0.1", "--port", "7143"]) == 0
    finally:
        if connectors.get_connectors():
            connectors.get_connectors().stop()


def test_delivered_complete_only_record_exposes_safe_recovery_without_stored_error(transport_host, fleet):
    client, transports, component = transport_host
    project, request, identity, headers = source_request(transport_host, fleet, "slack")
    response = client.post("/api/sessions", headers=headers, json={"taskdef": "chat", "target": "previous-launch",
                           "repo_url": "https://forge.test/example/repository"})
    assert response.status_code == 201, response.text
    worker = response.json()["session_id"]
    receipt = stored_actions(identity)[0]
    component.adopt_launch_targets(worker, [_link(request)], by="operator")
    original = project_requests.get(identity)
    assert original["state"] == "delivered" and original["error"] is None
    row = next(row for row in client.get("/api/assistant/requests").json()["requests"] if row["id"] == identity)
    assert row["state"] == "delivered" and row["recovery"]["code"] == "request_ownership_changed"
    assert row["recovery"]["release_allowed"] and row["error"]
    assert [action["state"] for action in row["actions"]] == ["complete"]
    response = client.post(f"/api/assistant/requests/{identity}/release", json={"reason": "Retain the completed worker"})
    assert response.status_code == 200, response.text
    assert stored_actions(identity) == [receipt]
    assert _post(client, request)["status"] == "duplicate"
    assert transports["slack"].posts == []
    assert client.post("/api/assistant/pending", headers=headers).json()["pending"] == []
    assert project_requests.active(project["id"]) is None


@pytest.mark.parametrize("identity_in", ["header", "body"])
def test_blocked_pending_take_preserves_explicit_request_identity(transport_host, fleet, identity_in):
    client, transports, _component = transport_host
    project, request, identity, headers = source_request(transport_host, fleet, "slack")
    worker, _payload = launch(client, request, headers)
    receipt = stored_actions(identity)[0]
    other = projects.create("Other")
    move_worker(worker, other["id"])
    response = client.post("/api/assistant/pending",
                           headers={**headers, **({"X-Lmer-Request-Id": identity} if identity_in == "header" else {})},
                           json={"request_id": identity} if identity_in == "body" else {})
    assert response.status_code == 200, response.text
    note = response.json()["pending"][0]
    assert note["kind"] == "request_unavailable"
    assert note["data"]["request_id"] == identity
    assert note["data"]["released"] and not note["data"]["reply_sent"]
    assert project_requests.active(project["id"]) is None
    assert stored_actions(identity) == [receipt]
    assert transports["slack"].posts == []
