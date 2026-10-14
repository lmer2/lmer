"""Peer grants across real platform auth, supervisor bus and native delivery."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from lmer_cli import codex_delivery, comms as bus, supervisor
from lmer_platform import api, assistant, assistant_history, comms, config, connectors, ctl
from lmer_platform import projects, registry, session_io, spawn, store
from lmer_platform import project_context as scope
from tests.test_platform_projects import fleet  # noqa: F401
from tests.test_project_review_requests import credential
from tests.test_platform_project_transports import transport_host, _link, _post  # noqa: F401
from tests.test_assistant_mentions import body as mention_body
from tests.conftest import strip_lmer_env
from lmer_platform import project_requests
from hooks.start import render_taskdef_template


@pytest.fixture
def host(fleet, tmp_path, monkeypatch):
    projects.migrate()
    alpha, beta = [projects.create(name) for name in ("Example Alpha", "Example Beta")]
    projects.reconcile(fleet)
    hub = comms.CommsHub()
    monkeypatch.setattr(comms, "_HUB", hub)
    monkeypatch.setattr(connectors, "_CONNECTORS", None)
    planes = {}

    def attach(project):
        session = projects.status(project["id"])["session_id"]
        native = codex_delivery.CodexDelivery(["codex"], notify=Mock())
        native.directory = tmp_path / session
        native.directory.mkdir(exist_ok=True)
        native.outbox = native.directory / "delivery.json"
        native._connect = Mock()
        native._disconnect = Mock()
        thread = {"id": session, "parentThreadId": None, "threadSource": "user",
                  "model": "gpt-6-astra", "reasoningEffort": "high", "turns": []}

        def request(method, params):
            if method == "thread/loaded/list":
                return {"data": [session]}
            if method == "thread/read":
                return {"thread": thread}
            assert method == "turn/start"
            return {"turn": {"id": "turn-1"}}

        native._request = Mock(side_effect=request)
        log = bus.MessageLog(session, str(tmp_path / f"{session}.jsonl"))
        terminal = Mock(side_effect=AssertionError("peer message used a terminal fallback"))
        token = spawn.token_file_for(session).read_text().strip()
        app = supervisor._build_fastapi_app(supervisor.OutputBuffer(), terminal, token,
                                           comms=log, codex_delivery=native)
        plane = {"http": TestClient(app), "native": native, "log": log, "session": session, "thread": thread}
        planes[session] = plane
        registry.update(session, control={"host": session, "port": 8001, "token_ref": str(spawn.token_file_for(session))})
        return plane

    first, second = attach(alpha), attach(beta)

    def request(method, url, **kwargs):
        host_name = url.split("//", 1)[1].split(":", 1)[0]
        return planes[host_name]["http"].request(method, "/" + url.split(":8001/", 1)[1],
                                                json=kwargs.get("json"), headers=kwargs.get("headers"))

    monkeypatch.setattr(session_io.requests, "request", request)
    http = TestClient(api.create_app(fleet, config.ensure_secret(fleet)))
    yield http, alpha, beta, first, second, attach
    http.close()
    for plane in planes.values():
        plane["http"].close()
    hub.stop()


def submitted(native, ident=None):
    """The destination adapter's receipt once its relay worker has settled it."""
    for _ in range(500):
        receipts = [r for r in native.snapshot()["receipts"] if ident in (None, r["delivery_id"])]
        if receipts and receipts[-1]["state"] != "pending":
            return receipts[-1]
        threading.Event().wait(.01)
    raise AssertionError("the destination never settled the relay")


def send(host, text="Requested coordination", ref="task-1", **extra):
    http, alpha, _beta, _first, second, _attach = host
    return http.post(f"/api/sessions/{second['session']}/comms", headers=credential(alpha["id"]),
                     json={"kind": "message", "text": text, "ref": ref, **extra})


def test_directory_and_bounded_orders_do_not_grant_context_or_controls(host, monkeypatch):
    http, alpha, beta, _first, second, _attach = host
    rows = [{"channel": channel, "permalink": link, "running": running, "key": {"secret": "hidden"}, "detail": "private"}
            for channel, link, running in (("slack", "https://example.slack.com/archives/C1/p123", True),
                                          ("matrix", "https://matrix.to/#/!room:example/$root", False))]
    monkeypatch.setattr(assistant, "_connected_threads", lambda session: rows if session == second["session"] else [])
    with scope.bound(beta["id"]):
        assistant.set_instructions("Relevant style. " * 200)
        assistant.set_handoff("Private handover")
    headers = credential(alpha["id"])
    result = http.get("/api/assistant/peers", headers=headers)
    assert result.status_code == 200, result.text
    peer = next(row for row in result.json()["peers"] if row["project_id"] == beta["id"])
    assert peer == {"project_id": beta["id"], "project_name": "Example Beta", "archived": False,
                    "session_id": second["session"], "generation": 1, "lifecycle": "running", "available": True,
                    "threads": [{key: row[key] for key in ("channel", "permalink", "running")} for row in rows]}
    route = f"/api/assistant/peers/{beta['id']}/instructions"
    assert http.get(route, headers=headers).status_code == 422
    assert http.get(route, headers=headers, params={"purpose": "Requested styling", "limit": 4001}).status_code == 422
    read = http.get(route, headers=headers, params={"purpose": "Requested styling", "offset": 4, "limit": 30})
    assert read.status_code == 200, read.text
    assert read.json()["instructions"] == ("Relevant style. " * 200)[4:34]
    assert read.json()["truncated"] and read.json()["source"]["project_id"] == beta["id"]
    assert "Private handover" not in result.text + read.text
    assert [row["id"] for row in http.get("/api/projects", headers=headers).json()["projects"]] == [alpha["id"]]
    for route in ("/api/assistant/handoff", "/api/assistant/instructions", "/api/state"):
        assert http.get(route, headers=headers, params={"workspace_id": beta["id"]}).status_code == 409
    for suffix in ("comms", "messages", "log", "ask"):
        assert http.get(f"/api/sessions/{second['session']}/{suffix}", headers=headers).status_code == 409
    assert http.post("/api/project-relay", headers=headers, json={"destination_id": beta["id"], "text": "No grant"}).status_code == 403


def test_orders_read_scrubs_the_source_projects_credential_and_keeps_memory_private(host):
    http, alpha, beta, _first, _second, _attach = host
    with scope.bound(beta["id"]):
        secret = config.active_assistant_credential()
        state = assistant.read_state().to_dict()
        state["instructions"] = "Style summary " + secret + "\nPRIVATE_TOKEN=fixture-credential-value"
        projects.save_assistant_state(state)
    response = http.get(f"/api/assistant/peers/{beta['id']}/instructions", headers=credential(alpha["id"]),
                        params={"purpose": "Requested style reference"})
    assert response.status_code == 200, response.text
    assert secret not in response.text and "fixture-credential-value" not in response.text
    assert response.json()["instructions"].startswith("Style summary")


def test_peer_send_and_reply_have_native_bus_history_and_durable_provenance(host):
    http, alpha, beta, first, second, _attach = host
    result = send(host, generation=1)
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["outcome"] == "recorded"
    assert payload["relay"] == {"queued": True, "bytes_written": 0}
    record = payload["message"]
    assert submitted(second["native"], record["id"])["state"] == "accepted"
    assert record["source"]["channel"] == "peer-ul"
    assert record["meta"]["peer"]["project_id"] == alpha["id"]
    assert record["meta"]["peer"]["session"] == first["session"]
    assert record["meta"]["peer"]["destination_session"] == second["session"]
    native_call = next(call for call in second["native"]._request.call_args_list if call.args[0] == "turn/start")
    text = native_call.args[1]["input"][0]["text"]
    assert f'[peer UL message · "Example Alpha" · session {first["session"]} · generation 1' in text
    assert f"reply: lmer-ctl send {first['session']}" in text
    assert "reply with your own UL credential" in text and text.endswith("Requested coordination")
    assert "lmer-ctl orders get" in text and "No additional operator credential" in text
    assert "original receipt snapshot is uncertain" in text
    with scope.bound(beta["id"]):
        retained = assistant_history.page(second["session"], since=0)["messages"]
    shown = next(row for row in retained if row.get("peer"))
    assert shown["peer"] == record["meta"]["peer"]
    assert shown["comms_kind"] == "peer UL message"
    with scope.bound(beta["id"]):
        assert assistant_history.events(second["session"])[0].message["meta"]["peer"] == record["meta"]["peer"]
    reply = http.post(f"/api/sessions/{first['session']}/comms", headers=credential(beta["id"]),
                      json={"text": "Requested answer", "ref": "reply-1"})
    assert reply.status_code == 200, reply.text
    assert reply.json()["peer"]["project_id"] == beta["id"]
    assert first["log"].get(1).source.channel == "peer-ul"
    attempts = [event for event in store.read_events() if event["type"] == "comms_post"]
    assert len(attempts) == 2 and all(row["data"]["receipt_match"] for row in attempts)


@pytest.mark.parametrize("extra", [{"source": {"project_id": "other"}}, {"meta": {"human": True}},
                                   {"author": "operator"}, {"kind": "command"}, {"kind": "answer"},
                                   {"in_reply_to": "question"}, {"attachments": []}])
def test_forged_source_and_non_message_actions_are_refused(host, extra):
    result = send(host, **extra)
    assert result.status_code == 403, result.text
    assert host[4]["log"].seq == 0


def test_foreign_worker_controls_and_non_ul_credentials_stay_closed(host, fleet):
    http, alpha, beta, _first, second, _attach = host
    worker = spawn.spawn_session(fleet, spawn.SpawnRequest("chat", "example", no_repo=True, workspace_id=beta["id"]))
    headers = credential(alpha["id"])
    rejected = http.post(f"/api/sessions/{worker.session_id}/comms", headers=headers, json={"text": "No", "ref": "foreign"})
    assert rejected.status_code == 403, rejected.text
    for suffix, body in (("input", {"data": "No"}), ("exit", {}), ("wind-down", {}), ("tty-ticket", {})):
        assert http.post(f"/api/sessions/{second['session']}/{suffix}", headers=headers, json=body).status_code == 409
    assert send(host, workspace_id=beta["id"]).status_code == 409
    worker_headers = {"Authorization": f"Bearer {spawn.token_file_for(worker.session_id).read_text().strip()}"}
    assert http.get("/api/assistant/peers", headers=worker_headers).status_code == 401
    connector = config.mint_connector_credential("example", "slack")
    assert http.get("/api/assistant/peers", headers={"Authorization": f"Bearer {connector}"}).status_code == 403
    assert http.post(f"/api/sessions/{second['session']}/comms", headers={"Authorization": f"Bearer {connector}"},
                     json={"text": "No", "ref": "connector", "generation": 1}).status_code == 403
    reserved = config.mint_connector_credential("reserved", "peer-ul")
    assert http.post(f"/api/sessions/{second['session']}/comms", headers={"Authorization": f"Bearer {reserved}"},
                     json={"text": "No forged peer role"}).status_code == 403
    assert second["log"].seq == 0


def test_invalid_peer_requests_never_admit_and_receipts_stay_project_scoped(host):
    http, alpha, beta, _first, second, _attach = host
    for invalid in ({"text": ""}, {"text": "x" * 16385}, {"ref": None}, {"ref": "spaces are invalid"},
                    {"generation": True}, {"generation": 0}):
        response = send(host, **invalid)
        assert response.status_code == 400, response.text
    assert second["log"].seq == 0
    empty_purpose = http.get(f"/api/assistant/peers/{beta['id']}/instructions", headers=credential(alpha["id"]),
                             params={"purpose": "  "})
    assert empty_purpose.status_code == 400
    assert send(host).status_code == 200
    assert http.get("/api/assistant/peers/receipts/task-1", headers=credential(beta["id"])).status_code == 404
    assert http.get("/api/assistant/peers/receipts/task-1", headers=credential(beta["id"]),
                    params={"workspace_id": alpha["id"]}).status_code == 409


def test_duplicate_concurrency_restart_and_sender_rotation_never_resend(host, fleet, monkeypatch):
    http, alpha, _beta, _first, second, _attach = host
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: send(host), range(2)))
    assert all(row.status_code == 200 for row in responses), [row.text for row in responses]
    assert second["log"].seq == 1
    assert sum(call.args[0] == "turn/start" for call in second["native"]._request.call_args_list) == 1
    assert send(host, text="Different text").status_code == 409
    monkeypatch.setattr(comms, "_HUB", comms.CommsHub())
    projects.migrate()
    projects.rotate(alpha["id"], fleet)
    repeated = send(host)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["replayed_receipt"] is True
    assert repeated.json()["peer"]["generation"] == 1
    receipt = http.get("/api/assistant/peers/receipts/task-1", headers=credential(alpha["id"]))
    assert receipt.status_code == 200 and receipt.json()["state"] == "recorded"
    assert second["log"].seq == 1


@pytest.mark.parametrize("lost", ["platform", "native"])
def test_uncertain_delivery_is_retained_and_never_replayed(host, fleet, monkeypatch, lost):
    http, alpha, _beta, _first, second, _attach = host
    if lost == "platform":
        original = session_io._post

        def lose(*args, **kwargs):
            original(*args, **kwargs)
            raise session_io.ControlPlaneError("response lost after bus acceptance")

        monkeypatch.setattr(session_io, "_post", lose)
    else:
        original = second["native"]._request.side_effect

        def lose(method, params):
            if method == "turn/start":
                raise TimeoutError("response lost after native submission")
            return original(method, params)

        second["native"]._request.side_effect = lose
    first = send(host)
    if lost == "native":
        # The destination's adapter owns the outcome: it escalates, never resends.
        assert first.status_code == 200 and first.json()["outcome"] == "recorded", first.text
        assert submitted(second["native"])["state"] == "uncertain"
    else:
        assert first.status_code == 409, first.text
    projects.rotate(alpha["id"], fleet)
    repeated = send(host)
    assert repeated.status_code == first.status_code, repeated.text
    assert second["log"].seq == 1
    assert sum(call.args[0] == "turn/start" for call in second["native"]._request.call_args_list) == 1
    inspected = http.get("/api/assistant/peers/receipts/task-1", headers=credential(alpha["id"]))
    assert inspected.status_code == 200 and inspected.json()["result"]


def test_stale_stopped_archived_and_unavailable_peers_are_truthful(host, fleet):
    http, alpha, beta, _first, second, attach = host
    assert send(host, generation=2).status_code == 409
    control = registry.read_session(second["session"])["control"]
    registry.update(second["session"], control=None)
    unavailable = http.get("/api/assistant/peers", headers=credential(alpha["id"])).json()
    assert not next(row for row in unavailable["peers"] if row["project_id"] == beta["id"])["available"]
    assert send(host).json()["detail"]["code"] == "peer_unavailable"
    registry.update(second["session"], control=control)
    projects.rotate(beta["id"], fleet)
    current = attach(beta)
    assert send(host).json()["detail"]["code"] == "peer_session_changed"
    assert current["log"].seq == 0
    selected = http.post(f"/api/sessions/{current['session']}/comms", headers=credential(alpha["id"]),
                         json={"text": "Explicit new destination", "ref": "new-generation", "generation": 2})
    assert selected.status_code == 200, selected.text
    assert selected.json()["peer"]["destination_generation"] == 2
    projects.stop(beta["id"])
    listing = http.get("/api/assistant/peers", headers=credential(alpha["id"])).json()
    stopped = next(row for row in listing["peers"] if row["project_id"] == beta["id"])
    assert stopped["session_id"] is None and stopped["generation"] is None and not stopped["available"]
    assert stopped["threads"] == []
    projects.archive(beta["id"], {"runs": []})
    listing = http.get("/api/assistant/peers", headers=credential(alpha["id"])).json()
    archived = next(row for row in listing["peers"] if row["project_id"] == beta["id"])
    assert archived["archived"] and not archived["available"]
    assert send(host).status_code == 409


def test_waiting_project_has_no_advertised_live_destination(host):
    http, alpha, _beta, _first, _second, _attach = host
    queued = projects.create("Example Waiting")
    directory = http.get("/api/assistant/peers", headers=credential(alpha["id"])).json()
    row = next(row for row in directory["peers"] if row["project_id"] == queued["id"])
    assert row["lifecycle"] == "waiting" and row["session_id"] is None and not row["available"]


def test_bus_refusal_can_retry_same_reference_without_terminal_fallback(host):
    _http, _alpha, _beta, _first, second, _attach = host
    original = second["http"]
    token = spawn.token_file_for(second["session"]).read_text().strip()
    terminal = Mock(side_effect=AssertionError("bus refusal fell back to terminal"))
    with TestClient(supervisor._build_fastapi_app(supervisor.OutputBuffer(), terminal, token)) as no_bus:
        second["http"] = no_bus
        refused = send(host)
    second["http"] = original
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["outcome"] == "refused"
    accepted = send(host)
    assert accepted.status_code == 200, accepted.text
    assert second["log"].seq == 1


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_peer_send_inherits_active_request_and_safe_refusal_can_retry_after_rotation(host, transport_host, fleet, transport):
    http, alpha, _beta, first, second, _attach = host
    transport_http, transports, component = transport_host
    request = mention_body(transport, threaded=True)
    component.adopt_launch_targets(first["session"], [_link(request)], by="operator")
    accepted = _post(transport_http, request)
    taken = http.post("/api/assistant/pending", headers=credential(alpha["id"]))
    assert taken.status_code == 200, taken.text
    assert taken.json()["pending"][0]["data"]["request_id"] == accepted["request_id"]
    original = second["http"]
    token = spawn.token_file_for(second["session"]).read_text().strip()
    terminal = Mock(side_effect=AssertionError("peer send fell back to terminal"))
    with TestClient(supervisor._build_fastapi_app(supervisor.OutputBuffer(), terminal, token)) as no_bus:
        second["http"] = no_bus
        refused = send(host)
    second["http"] = original
    assert refused.status_code == 409, refused.text
    actions = project_requests.listing(alpha["id"])[0]["actions"]
    assert [row["state"] for row in actions] == ["refused"]
    result = send(host)
    assert result.status_code == 200, result.text
    assert [row["state"] for row in project_requests.listing(alpha["id"])[0]["actions"]] == ["complete"]
    projects.rotate(alpha["id"], fleet)
    pending = http.post("/api/assistant/pending", headers=credential(alpha["id"]))
    assert pending.status_code == 200, pending.text
    repeated = send(host)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["message"]["id"] == result.json()["message"]["id"]
    assert second["log"].seq == 1
    assert transports[transport].posts == []


def test_process_loss_after_bus_acceptance_retains_sending_receipt(host, monkeypatch):
    _http, _alpha, _beta, _first, second, _attach = host
    original = session_io._post

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("process interrupted before result persistence")

    with monkeypatch.context() as patch:
        patch.setattr(session_io, "_post", crash)
        with pytest.raises(RuntimeError, match="process interrupted"):
            send(host)
    projects.migrate()
    monkeypatch.setattr(comms, "_HUB", comms.CommsHub())
    repeated = send(host)
    assert repeated.status_code == 409, repeated.text
    assert repeated.json()["detail"]["outcome"] == "uncertain"
    assert second["log"].seq == 1


def test_an_unreachable_destination_retries_the_recorded_send_itself(host):
    """Not "refused": the destination adapter resubmits it, so a peer resend would arrive twice."""
    second = host[4]
    second["native"]._connect.side_effect = codex_delivery.CodexDeliveryError("fixture native destination unavailable")
    first = send(host)
    assert first.status_code == 200 and first.json()["outcome"] == "recorded", first.text
    assert submitted(second["native"])["state"] == "retrying"
    repeated = send(host)
    assert repeated.status_code == 200 and repeated.json()["replayed_receipt"]
    assert second["log"].seq == 1
    assert not any(call.args[0] == "turn/start" for call in second["native"]._request.call_args_list)


def test_cli_peer_commands_use_real_authorization_and_send_path(host, monkeypatch, capsys):
    http, alpha, beta, _first, second, _attach = host
    monkeypatch.setenv(ctl.ENV_PLATFORM_URL, "http://testserver")
    with scope.bound(alpha["id"]):
        monkeypatch.setenv(ctl.ENV_PLATFORM_CREDENTIAL, config.active_assistant_credential())

    assert ctl.main(["peers", "list"], transport=http) == 0
    assert second["session"] in capsys.readouterr().out
    assert ctl.main(["peers", "orders", beta["id"], "--purpose", "Human requested matching style", "--limit", "20"], transport=http) == 0
    assert json.loads(capsys.readouterr().out)["source"]["project_id"] == beta["id"]
    assert ctl.main(["send", second["session"], "Requested via CLI", "--ref", "cli-1", "--generation", "1"], transport=http) == 0
    assert json.loads(capsys.readouterr().out)["message"]["source"]["channel"] == "peer-ul"
    assert ctl.main(["peers", "receipt", "cli-1"], transport=http) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "recorded"


@pytest.mark.parametrize("reachable", [True, False])
def test_peer_instructions_render_only_with_platform_access(monkeypatch, reachable):
    strip_lmer_env(monkeypatch)
    root = Path(__file__).resolve().parents[1] / "taskdef"
    monkeypatch.setenv("LMER_TASKDEF_ROOT", str(root))
    if reachable:
        monkeypatch.setenv("LMER_PLATFORM_URL", "http://platform.test")
        monkeypatch.setenv("LMER_WORKSPACE_ID", "example-alpha")
        monkeypatch.setenv("LMER_WORKSPACE_NAME", "Example Alpha")
    rendered = render_taskdef_template(root / "orchestrate" / "instructions.txt", extra_context={"instructions_file": "/x"})
    prose = " ".join(rendered.split())
    assert ("lmer-ctl peers list" in prose) is reachable
    if reachable:
        for contract in ("does not confer human or supervising-operator authority", "never redirected",
                         "never mirrored into connected external threads", "is not an operator credential"):
            assert contract in prose
        assert "opens the whole API" not in prose
        workspace = prose.split("## Your project", 1)[1].split("Taking pending", 1)[0]
        for contract in ("generic", "routes require an operator credential", "lmer-ctl orders get",
                         "lmer-ctl peers orders PROJECT_ID", "lmer-ctl send PEER_SESSION",
                         "no additional operator credential", "Do not send a normal brief"):
            assert contract in workspace
        assert "original result snapshot" in prose and "later evidence of arrival" in prose


def test_authorized_standing_order_exchange_uses_own_ul_credentials_after_uncertain_receipt(host, monkeypatch, capsys):
    http, alpha, beta, first, second, _attach = host
    with scope.bound(beta["id"]):
        assistant.set_instructions("Requested working brief: run the focused checks before publishing.")
    monkeypatch.setenv(ctl.ENV_PLATFORM_URL, "http://testserver")
    with scope.bound(alpha["id"]):
        monkeypatch.setenv(ctl.ENV_PLATFORM_CREDENTIAL, config.active_assistant_credential())
    assert ctl.main(["peers", "orders", beta["id"], "--purpose", "Operator requested work transfer"], transport=http) == 0
    excerpt = json.loads(capsys.readouterr().out)
    assert excerpt["source"]["project_id"] == beta["id"]
    assert "focused checks" in excerpt["instructions"]

    original = session_io._post

    def lost_response(*args, **kwargs):
        original(*args, **kwargs)
        raise session_io.ControlPlaneError("response lost after native arrival")

    with monkeypatch.context() as delivery:
        delivery.setattr(session_io, "_post", lost_response)
        request = send(host, text="The operator requested this work transfer; please share the relevant standing instructions.", ref="transfer-1")
    assert request.status_code == 409, request.text
    received = next(call.args[1]["input"][0]["text"] for call in second["native"]._request.call_args_list
                    if call.args[0] == "turn/start")
    assert "reply with your own UL credential" in received and "lmer-ctl orders get" in received
    assert f"reply: lmer-ctl send {first['session']}" in received
    assert second["log"].seq == 1

    with scope.bound(beta["id"]):
        monkeypatch.setenv(ctl.ENV_PLATFORM_CREDENTIAL, config.active_assistant_credential())
    assert ctl.main(["orders", "get"], transport=http) == 0
    own = json.loads(capsys.readouterr().out)["instructions"]
    assert own == excerpt["instructions"]
    assert ctl.main(["send", first["session"], own, "--ref", "transfer-reply-1"], transport=http) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply["peer"]["project_id"] == beta["id"]
    assert reply["peer"]["destination_session"] == first["session"]
    returned = next(call.args[1]["input"][0]["text"] for call in first["native"]._request.call_args_list
                    if call.args[0] == "turn/start")
    assert returned.endswith(own) and '"Example Beta"' in returned
    headers = credential(beta["id"])
    assert http.get("/api/project-context", headers=headers, params={"source_id": alpha["id"]}).status_code == 403
    assert http.post("/api/project-relay", headers=headers, json={"destination_id": alpha["id"], "text": own}).status_code == 403
    snapshot = http.get("/api/assistant/peers/receipts/transfer-1", headers=credential(alpha["id"])).json()
    assert snapshot["state"] == "uncertain"
    again = send(host, text="The operator requested this work transfer; please share the relevant standing instructions.", ref="transfer-1")
    assert again.status_code == 409 and second["log"].seq == 1
