"""MR review regressions through real authentication, admission and child launches."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
from fastapi import HTTPException
import pytest

from lmer_platform import api, assistant, config, lifecycle, project_requests, projects, spawn, store
from lmer_platform import project_context as scope
from tests.test_platform_projects import fleet  # noqa: F401
from tests.test_platform_project_transports import transport_host, _link, _next, _post  # noqa: F401
from tests.test_assistant_mentions import body
from lmer_cli import thread_access
from lmer_cli.threads import ThreadError
from lmer_platform import project_threads, ctl
from tests.test_platform_ctl import Local


def delivered(host, fleet, transport="slack"):
    client, _transports, component = host
    project = projects.create("Alpha")
    projects.reconcile(fleet)
    request = body(transport, threaded=True)
    component.adopt_launch_targets(projects.status(project["id"])["session_id"], [_link(request)], by="operator")
    accepted = _post(client, request)
    headers = credential(project["id"])
    taken = client.post("/api/assistant/pending", headers=headers)
    assert taken.status_code == 200, taken.text
    assert taken.json()["pending"][0]["data"]["request_id"] == accepted["request_id"]
    return project, request, accepted["request_id"], headers


def credential(project_id):
    with scope.bound(project_id):
        secret = config.active_assistant_credential()
    return {"Authorization": f"Bearer {secret}"}


def actions(identity):
    return next(row["actions"] for row in project_requests.listing() if row["id"] == identity)


@pytest.mark.parametrize("operation", ["context", "relay"])
def test_cross_project_sharing_requires_operator_authority(transport_host, fleet, operation):
    client, _transports, _component = transport_host
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    projects.reconcile(fleet)
    headers = credential(alpha["id"])
    if operation == "context":
        denied = client.get("/api/project-context", headers=headers, params={"source_workspace_id": beta["id"]})
        assert denied.status_code == 403, denied.text
        allowed = client.get("/api/project-context", params={"workspace_id": alpha["id"], "source_workspace_id": beta["id"]})
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["source"] == {"id": beta["id"], "name": "Beta"}
    else:
        payload = {"destination_id": beta["id"], "text": "Operator explicitly requested this excerpt"}
        denied = client.post("/api/project-relay", headers=headers, json=payload)
        assert denied.status_code == 403, denied.text
        with scope.bound(beta["id"]):
            assert assistant.take_pending() == []
        allowed = client.post("/api/project-relay", json={**payload, "workspace_id": alpha["id"]})
        assert allowed.status_code == 200, allowed.text
        with scope.bound(beta["id"]):
            note = assistant.take_pending()[0]
        assert note["data"]["source_workspace_id"] == alpha["id"]
        assert note["data"]["source_name"] == "Alpha"


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_omitted_request_id_cannot_repeat_launch_after_rotation(transport_host, fleet, transport):
    client, _transports, _component = transport_host
    project, _request, identity, headers = delivered(transport_host, fleet, transport)
    payload = {"taskdef": "chat", "target": "once"}
    first = client.post("/api/sessions", headers=headers, json=payload)
    assert first.status_code == 201, first.text
    lifecycle.exit_session(first.json()["session_id"])
    assert spawn.wait_for_exit_recorded(first.json()["session_id"])
    projects.rotate(project["id"], fleet)
    headers = credential(project["id"])
    notes = client.post("/api/assistant/pending", headers=headers).json()["pending"]
    assert notes[0]["data"]["request_id"] == identity
    repeated = client.post("/api/sessions", headers=headers, json=payload)
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["session_id"] == first.json()["session_id"]
    assert [row["state"] for row in actions(identity)] == ["complete"]
    launches = [event for event in store.read_events() if event["type"] == "session_spawned" and event["data"]["target"] == "once"]
    assert [event["data"]["session"] for event in launches] == [first.json()["session_id"]]


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_generic_reply_is_source_bound_and_durable_without_request_id(transport_host, fleet, transport):
    client, transports, _component = transport_host
    project, request, identity, headers = delivered(transport_host, fleet, transport)
    data = {"permalink": _link(request), "text": "Only once at the source"}
    first = client.post("/api/comms/thread", headers=headers, json=data)
    assert first.status_code == 200, first.text
    projects.rotate(project["id"], fleet)
    repeated = client.post("/api/comms/thread", headers=credential(project["id"]), json=data)
    assert repeated.status_code == 200, repeated.text
    assert len(transports[transport].posts) == 1
    assert project_requests.get(identity)["state"] == "replied"


def test_pending_serializes_request_context_and_assistant_mutations(transport_host, fleet):
    client, _transports, _component = transport_host
    project, request, identity, headers = delivered(transport_host, fleet)
    second = _post(client, _next(request))["request_id"]
    assert client.post("/api/assistant/pending", headers=headers).json()["pending"] == []
    changed = client.post("/api/assistant/instructions", headers=headers, json={"instructions": "Request orders"})
    assert changed.status_code == 200, changed.text
    assert actions(identity)[0]["state"] == "complete"
    assert project_requests.get(second)["state"] == "queued"
    denied = client.post("/api/assistant/instructions", headers={**headers, "X-Lmer-Request-Id": second},
                         json={"instructions": "Not this request"})
    assert denied.status_code == 409
    answered = client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Done"})
    assert answered.status_code == 200, answered.text
    next_notes = client.post("/api/assistant/pending", headers=headers).json()["pending"]
    assert [note["data"]["request_id"] for note in next_notes] == [second]
    assert project_requests.get(second)["project_id"] == project["id"]


@pytest.mark.parametrize("status", [400, 409, 429])
def test_proven_pre_spawn_refusals_remain_retryable_and_do_not_poison_archive(transport_host, fleet, status):
    client, _transports, _component = transport_host
    project, _request, identity, headers = delivered(transport_host, fleet)
    headers = {**headers, "X-Lmer-Request-Id": identity}
    payload = {"taskdef": "chat", "target": "retry", "repo_url": "https://forge.test/repository"}
    occupant = None
    if status == 400:
        payload["taskdef"] = ""
    else:
        config.update_stored({"max_concurrent_sessions": 2 if status == 409 else 1})
        occupant = spawn.spawn_session(fleet, spawn.SpawnRequest("chat", "retry" if status == 409 else "occupant",
                                                                repo_url="https://forge.test/repository",
                                                                workspace_id=project["id"]))
    refused = client.post("/api/sessions", headers=headers, json=payload)
    assert refused.status_code == status, refused.text
    assert actions(identity)[0]["state"] == "refused"
    if occupant:
        lifecycle.exit_session(occupant.session_id)
        assert spawn.wait_for_exit_recorded(occupant.session_id)
    repeated = client.post("/api/sessions", headers=headers, json=payload)
    assert repeated.status_code == (400 if status == 400 else 201), repeated.text
    if status == 400:
        assert client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Invalid request"}).status_code == 200
        assert client.post(f"/api/projects/{project['id']}/archive").status_code == 200


@pytest.mark.parametrize("status", [400, 403, 409, 429, 503])
def test_post_commit_errors_remain_uncertain_regardless_of_http_status(transport_host, fleet, monkeypatch, status):
    client, _transports, _component = transport_host
    project, _request, identity, headers = delivered(transport_host, fleet)
    headers = {**headers, "X-Lmer-Request-Id": identity}
    original = api.spawn_session
    launched = []
    def lost_response(*args, **kwargs):
        launched.append(original(*args, **kwargs))
        raise HTTPException(status_code=status, detail="Response lost after launch")
    monkeypatch.setattr(api, "spawn_session", lost_response)
    payload = {"taskdef": "chat", "target": "lost-response"}
    first = client.post("/api/sessions", headers=headers, json=payload)
    assert first.status_code == status, first.text
    assert actions(identity)[0]["state"] == "uncertain"
    again = client.post("/api/sessions", headers=headers, json=payload)
    assert again.status_code == 409
    assert len(launched) == 1
    assert client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Inspect the launch"}).status_code == 200
    assert client.post(f"/api/projects/{project['id']}/archive").status_code == 409


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_reply_lookup_failure_keeps_request_retryable(transport_host, fleet, monkeypatch, transport):
    client, transports, _component = transport_host
    _project, _request, identity, headers = delivered(transport_host, fleet, transport)
    original = thread_access.resolve_thread
    def unavailable(*_args, **_kwargs):
        raise ThreadError("Thread lookup unavailable before posting", status=503)
    monkeypatch.setattr(thread_access, "resolve_thread", unavailable)
    failed = client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Answer"})
    assert failed.status_code == 503, failed.text
    assert project_requests.get(identity)["state"] == "delivered"
    assert transports[transport].posts == []
    monkeypatch.setattr(thread_access, "resolve_thread", original)
    reply = client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Answer"})
    assert reply.status_code == 200, reply.text
    assert len(transports[transport].posts) == 1


def test_concurrent_omitted_id_mutations_share_one_receipt(transport_host, fleet):
    client, _transports, _component = transport_host
    _project, _request, identity, headers = delivered(transport_host, fleet)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: client.post("/api/sessions", headers=headers,
                                                      json={"taskdef": "chat", "target": "concurrent"}), range(2)))
    assert [reply.status_code for reply in replies] == [201, 201], [reply.text for reply in replies]
    assert replies[0].json()["session_id"] == replies[1].json()["session_id"]
    assert [row["state"] for row in actions(identity)] == ["complete"]


def test_legacy_payload_receipt_survives_context_recovery(transport_host, fleet):
    client, _transports, _component = transport_host
    project, _request, identity, headers = delivered(transport_host, fleet)
    payload = {"taskdef": "chat", "target": "legacy", "workspace_id": project["id"], "request_id": identity}
    first = client.post("/api/sessions", headers=headers, json=payload)
    assert first.status_code == 201, first.text
    old_identity = hashlib.sha256(json.dumps([identity, "POST", "/api/sessions", payload], sort_keys=True).encode()).hexdigest()
    with projects.database() as db:
        db.execute("UPDATE request_actions SET id=? WHERE request_id=?", (old_identity, identity))
    projects.rotate(project["id"], fleet)
    headers = credential(project["id"])
    before_take = client.post("/api/sessions", headers=headers, json={"taskdef": "chat", "target": "different"})
    assert before_take.status_code == 409, before_take.text
    assert client.post("/api/assistant/pending", headers=headers).status_code == 200
    repeated = client.post("/api/sessions", headers=headers, json={"taskdef": "chat", "target": "legacy"})
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["session_id"] == first.json()["session_id"]


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_active_request_refuses_other_owned_thread_and_terminal_bypass(transport_host, fleet, transport):
    client, transports, _component = transport_host
    project, request, identity, headers = delivered(transport_host, fleet, transport)
    other = body(transport)
    project_threads.choose_unbound(_link(other), project["id"])
    wrong = client.post("/api/comms/thread", headers=headers, json={"permalink": _link(other), "text": "Wrong source"})
    assert wrong.status_code == 409, wrong.text
    assert transports[transport].posts == []
    session = projects.status(project["id"])["session_id"]
    ticket = client.post(f"/api/sessions/{session}/tty-ticket", headers=headers)
    assert ticket.status_code == 409, ticket.text
    assert actions(identity) == []


def test_forbidden_operator_mutation_has_no_action_effect(transport_host, fleet):
    client, _transports, _component = transport_host
    project, _request, identity, headers = delivered(transport_host, fleet)
    denied = client.patch(f"/api/projects/{project['id']}", headers=headers, json={"name": "Unauthorized"})
    assert denied.status_code == 403, denied.text
    assert actions(identity) == []
    assert projects.get(project["id"])["name"] == "Alpha"


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_operator_parks_uncertain_reply_without_resend_and_releases_after_rotation(transport_host, fleet, monkeypatch, capsys, transport):
    client, transports, _component = transport_host
    project, request, identity, headers = delivered(transport_host, fleet, transport)
    remote = transports[transport]
    method = "post_message" if transport == "slack" else "send_thread_text"
    original = getattr(remote, method)
    def response_lost(*args, **kwargs):
        original(*args, **kwargs)
        raise ThreadError("Fixture response lost after post", status=502)
    monkeypatch.setattr(remote, method, response_lost)
    failed = client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Uncertain answer"})
    assert failed.status_code == 502, failed.text
    before = project_requests.get(identity)
    assert before["state"] == "reply_uncertain"
    monkeypatch.setattr(remote, method, original)
    second = _post(client, _next(request, text="Independent work"))["request_id"]
    assert client.post("/api/assistant/pending", headers=headers).json()["pending"] == []
    assert client.post("/api/assistant/instructions", headers=headers, json={"instructions": "Independent"}).status_code == 409
    projects.rotate(project["id"], fleet)
    headers = credential(project["id"])
    assert client.post("/api/assistant/pending", headers=headers).json()["pending"] == []
    url = f"/api/assistant/requests/{identity}/park"
    payload = {"workspace_id": project["id"], "reason": "Leave delivery unknown; continue independent work"}
    assert client.post(url, headers=headers, json=payload).status_code == 403
    assert client.post(url, headers={"Authorization": "Bearer invalid"}, json=payload).status_code == 401
    assert client.post(url, json={**payload, "workspace_id": "general"}).status_code == 409
    assert client.post(url, json={**payload, "reason": ""}).status_code == 400
    assert project_requests.active(project["id"]) == identity
    connect = sqlite3.connect
    class InterruptedParking(sqlite3.Connection):
        def execute(self, sql, *args):
            result = super().execute(sql, *args)
            if sql.startswith("DELETE FROM request_context"):
                raise sqlite3.OperationalError("Fixture parking transaction interrupted")
            return result
    with monkeypatch.context() as interrupted:
        interrupted.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, factory=InterruptedParking, **kw))
        with pytest.raises(sqlite3.OperationalError, match="transaction interrupted"):
            project_requests.park(identity, payload["reason"], workspace_id=project["id"])
    with projects.database() as db:
        assert db.execute("SELECT * FROM request_parking").fetchall() == []
    assert project_requests.active(project["id"]) == identity
    parked = client.post(url, json=payload)
    assert parked.status_code == 200, parked.text
    assert parked.json()["parking"]["by"] == "operator"
    assert parked.json()["parking"]["reason"] == payload["reason"]
    assert parked.json()["parking"]["at"]
    monkeypatch.setenv(ctl.ENV_PLATFORM_URL, "http://testserver")
    monkeypatch.setenv(ctl.ENV_PLATFORM_CREDENTIAL, config.ensure_secret(fleet))
    assert ctl.main(["--workspace-id", project["id"], "requests", "park", identity,
                     "Do not overwrite provenance"], transport=Local(client)) == 0
    assert json.loads(capsys.readouterr().out) == parked.json()
    assert project_requests.get(identity) == before
    projects.migrate()
    projects.rotate(project["id"], fleet)
    headers = credential(project["id"])
    notes = client.post("/api/assistant/pending", headers=headers).json()["pending"]
    assert [note["data"]["request_id"] for note in notes] == [second]
    assert client.post(url, json=payload).json() == parked.json()
    assert project_requests.active(project["id"]) == second
    changed = client.post("/api/assistant/instructions", headers=headers, json={"instructions": "Independent"})
    assert changed.status_code == 200, changed.text
    assert actions(second)[0]["state"] == "complete"
    assert actions(identity) == []
    answered = client.post(f"/api/assistant/requests/{second}/reply", headers=headers, json={"text": "Independent answer"})
    assert answered.status_code == 200, answered.text
    assert client.post("/api/assistant/pending", headers=headers).json()["pending"] == []
    assert project_requests.active(project["id"]) is None
    assert client.post("/api/assistant/instructions", headers=headers, json={"instructions": "Unbound independent work"}).status_code == 200
    assert client.post(f"/api/assistant/requests/{identity}/reply", json={"text": "Never resend"}).status_code == 409
    assert _post(client, request)["status"] == "duplicate"
    assert [post[2] for post in remote.posts] == ["Uncertain answer", "Independent answer"]
    assert client.post(f"/api/projects/{project['id']}/archive").status_code == 409
    listed = client.get("/api/assistant/requests", params={"workspace_id": project["id"]}).json()["requests"]
    retained = next(row for row in listed if row["id"] == identity)
    assert retained["state"] == "reply_uncertain" and retained["error"] == before["error"]
    assert retained["parking"] == parked.json()["parking"]
    assert retained["conversation"] == request["conversation"]
    assert client.post(f"/api/assistant/requests/{second}/park", json=payload).status_code == 409


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_operator_can_park_interrupted_reply_without_claiming_delivery(transport_host, fleet, monkeypatch, transport):
    client, transports, _component = transport_host
    project, _request, identity, _headers = delivered(transport_host, fleet, transport)
    remote = transports[transport]
    method = "post_message" if transport == "slack" else "send_thread_text"
    original = getattr(remote, method)
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise SystemExit("Fixture interruption after remote post")
    monkeypatch.setattr(remote, method, interrupted)
    with pytest.raises(SystemExit, match="interruption"):
        project_requests.reply(identity, "Unconfirmed", workspace_id=project["id"],
                               session_id=projects.status(project["id"])["session_id"])
    assert project_requests.get(identity)["state"] == "reply_sending"
    projects.rotate(project["id"], fleet)
    reply = client.post(f"/api/assistant/requests/{identity}/park", json={"reason": "Preserve interrupted delivery"})
    assert reply.status_code == 200, reply.text
    assert reply.json()["state"] == "reply_sending"
    assert project_requests.active(project["id"]) is None
    assert client.post(f"/api/assistant/requests/{identity}/reply", json={"text": "Never repeat"}).status_code == 409
    assert len(remote.posts) == 1
    assert client.post(f"/api/projects/{project['id']}/archive").status_code == 409


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_parking_retains_original_project_after_source_worker_moves(transport_host, fleet, monkeypatch, transport):
    client, transports, component = transport_host
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    projects.reconcile(fleet)
    worker = spawn.spawn_session(fleet, spawn.SpawnRequest("chat", "park-move", repo_url="https://forge.test/repository",
                                                         workspace_id=alpha["id"]))
    request = body(transport, threaded=True)
    component.adopt_launch_targets(worker.session_id, [_link(request)], by="operator")
    identity = _post(client, request)["request_id"]
    headers = credential(alpha["id"])
    assert client.post("/api/assistant/pending", headers=headers).json()["pending"][0]["data"]["request_id"] == identity
    remote = transports[transport]
    method = "post_message" if transport == "slack" else "send_thread_text"
    original = getattr(remote, method)
    def response_lost(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("Fixture reply response lost")
    monkeypatch.setattr(remote, method, response_lost)
    assert client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Uncertain"}).status_code == 502
    before = project_requests.get(identity)
    lifecycle.exit_session(worker.session_id)
    assert spawn.wait_for_exit_recorded(worker.session_id)
    key = "forge.test/repository/chat-park-move"
    projects.move(key, beta["id"], expected_project=alpha["id"])
    url = f"/api/assistant/requests/{identity}/park"
    assert client.post(url, json={"workspace_id": beta["id"], "reason": "Wrong original project"}).status_code == 409
    parked = client.post(url, json={"workspace_id": alpha["id"], "reason": "Retain unknown outcome; continue Alpha"})
    assert parked.status_code == 200, parked.text
    assert project_requests.get(identity) == before
    assert project_requests.active(alpha["id"]) is None
    assert projects.run_home(key)["project_id"] == beta["id"]
    assert client.post("/api/assistant/instructions", headers=headers, json={"instructions": "Independent Alpha work"}).status_code == 200
    assert client.post(f"/api/assistant/requests/{identity}/reply", json={"workspace_id": alpha["id"], "text": "Never redirect"}).status_code == 409
    assert len(remote.posts) == 1
    assert client.post(f"/api/projects/{alpha['id']}/archive").status_code == 409
