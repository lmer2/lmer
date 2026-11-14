"""Releasing or handing off a mention's thread claim, through the HTTP route."""
import json
from pathlib import Path

import pytest

from lmer_platform import config, ctl, project_requests, project_threads, projects, store
from tests.conftest import strip_lmer_env
from hooks.start import render_taskdef_template
from tests.test_platform_projects import fleet  # noqa: F401
from tests.test_platform_project_transports import transport_host, _link, _next, _post  # noqa: F401
from tests.test_project_review_requests import credential
from tests.test_assistant_mentions import body


def take(client, headers):
    response = client.post("/api/assistant/pending", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["pending"]


def release(client, headers, link, **fields):
    return client.post("/api/threads/release", headers=headers, json={"permalink": link, **fields})


def general_request(host, fleet, transport):
    """The issue's case: an unmapped mention lands on General, which answers it."""
    client, _transports, _component = host
    billing = projects.create("Billing")
    projects.request_start(projects.GENERAL)
    projects.reconcile(fleet)
    request = body(transport, threaded=True)
    admitted = _post(client, request)
    assert admitted["workspace_id"] == projects.GENERAL
    headers = credential(projects.GENERAL)
    assert take(client, headers)[0]["data"]["request_id"] == admitted["request_id"]
    return billing, request, admitted["request_id"], headers


def finish(client, headers, identity):
    """Answer and take pending, so later hand-made ownership changes meet no bound request."""
    assert client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Done"}).status_code == 200
    assert take(client, headers) == []


def detail(response):
    return response.json()["detail"]["message"]


def owners(link):
    return [(row["owner"], row["project_id"]) for row in project_threads.ownership(link)]


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_general_hands_an_answered_thread_to_billing_whose_spawn_then_succeeds(transport_host, fleet, transport):
    client, _transports, _component = transport_host
    billing, request, identity, headers = general_request(transport_host, fleet, transport)
    link = _link(request)
    refused = client.post("/api/sessions", headers=credential(billing["id"]), json={
        "taskdef": "chat", "target": "triage", "repo_url": "https://forge.test/example/repository",
        "targets": [link]})
    assert refused.status_code in (400, 409), refused.text
    replied = client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Forwarding"})
    assert replied.status_code == 200, replied.text

    handed = release(client, headers, link, to="billing", reason="Billing owns this request")
    assert handed.status_code == 200, handed.text
    result = handed.json()
    assert result["to"] == billing["id"]
    assert result["previous"] == {"request_id": identity, "state": "replied", "closed": False}
    assert owners(link) == [("choice", billing["id"])]
    receipts = [row for row in project_requests.listing() if row["id"] == identity][0]["actions"]
    assert receipts[-1] == {"method": "POST", "path": "/api/threads/release", "state": "complete", "error": None}

    peer_headers = credential(billing["id"])
    delivered = take(client, peer_headers)
    data = delivered[0]["data"]
    assert data["request_id"] == result["request_id"] != identity
    assert data["handoff"]["from_project"] == projects.GENERAL
    assert data["handoff"]["reason"] == "Billing owns this request"
    assert data["actions"] == []
    assert "handed this thread to you" in data["instruction"]
    spawned = client.post("/api/sessions", headers=peer_headers, json={
        "taskdef": "chat", "target": "triage", "repo_url": "https://forge.test/example/repository",
        "targets": [link]})
    assert spawned.status_code == 201, spawned.text

    events = [event for event in store.read_events() if event["type"] == "thread_released"]
    assert [(event["data"]["workspace_id"], event["data"]["to"], event["data"]["by"]) for event in events] == [
        (projects.GENERAL, billing["id"], "assistant")]


def test_a_repeated_handoff_returns_the_same_request(transport_host, fleet):
    client, _transports, _component = transport_host
    billing, request, _identity, headers = general_request(transport_host, fleet, "slack")
    first = release(client, headers, _link(request), to=billing["id"])
    again = release(client, headers, _link(request), to="BILLING")
    assert first.status_code == again.status_code == 200, again.text
    assert again.json() == {"permalink": _link(request), "workspace_id": projects.GENERAL, "to": billing["id"],
                            "released": [], "request_id": first.json()["request_id"], "duplicate": True}
    assert owners(_link(request)) == [("choice", billing["id"])]


def test_an_assistant_repeating_a_handoff_after_replying_gets_the_same_request(transport_host, fleet):
    client, _transports, _component = transport_host
    billing, request, identity, headers = general_request(transport_host, fleet, "slack")
    assert client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Over to Billing"}).status_code == 200
    first = release(client, headers, _link(request), to="Billing")
    again = release(client, headers, _link(request), to="Billing")
    assert first.status_code == again.status_code == 200, again.text
    assert again.json()["duplicate"] is True
    assert again.json()["request_id"] == first.json()["request_id"]
    plain = release(client, headers, _link(request))
    assert plain.status_code == 200, plain.text
    assert plain.json() == {"permalink": _link(request), "workspace_id": projects.GENERAL, "released": [],
                            "to": None, "request_id": None, "duplicate": True}


def test_a_thread_handed_back_and_on_again_gets_a_new_request(transport_host, fleet):
    client, _transports, _component = transport_host
    billing, request, identity, headers = general_request(transport_host, fleet, "slack")
    link = _link(request)
    first = release(client, headers, link, to="Billing").json()["request_id"]
    peer = credential(billing["id"])
    assert take(client, peer)[0]["data"]["request_id"] == first
    back = release(client, peer, link, to="General")
    assert back.status_code == 200, back.text
    assert owners(link) == [("choice", projects.GENERAL)]
    assert [note["data"]["request_id"] for note in take(client, headers)] == [back.json()["request_id"]]
    finish(client, headers, back.json()["request_id"])
    again = release(client, headers, link, to="Billing")
    assert again.status_code == 200, again.text
    assert "duplicate" not in again.json()
    assert again.json()["request_id"] not in (first, back.json()["request_id"])
    assert owners(link) == [("choice", billing["id"])]


def test_a_project_without_a_claim_cannot_see_or_repeat_anothers_handoff(transport_host, fleet):
    client, _transports, _component = transport_host
    billing = projects.create("Billing")
    third = projects.create("Third")
    projects.reconcile(fleet)
    link = _link(body("slack", threaded=True))
    project_threads.choose_unbound(link, projects.GENERAL)
    handed = client.post("/api/threads/release", json={"permalink": link, "to": "Billing", "reason": "theirs",
                                                        "workspace_id": projects.GENERAL})
    assert handed.status_code == 200, handed.text
    refused = client.post("/api/threads/release", json={"permalink": link, "to": "Billing", "reason": "probe",
                                                         "workspace_id": third["id"]})
    assert refused.status_code == 403, refused.text
    assert handed.json()["request_id"] not in refused.text
    assert owners(link) == [("choice", billing["id"])]


def test_a_connector_credential_cannot_release(transport_host, fleet):
    client, _transports, _component = transport_host
    _billing, request, _identity, _headers = general_request(transport_host, fleet, "slack")
    connector = config.mint_connector_credential("example", "slack")
    refused = client.post("/api/threads/release", headers={"Authorization": f"Bearer {connector}"},
                          json={"permalink": _link(request), "to": "Billing", "workspace_id": projects.GENERAL})
    assert refused.status_code == 403, refused.text
    assert owners(_link(request)) == [("choice", projects.GENERAL)]


def test_a_failure_before_commit_changes_nothing_and_the_retry_succeeds(transport_host, fleet, monkeypatch):
    client, _transports, _component = transport_host
    billing, request, identity, headers = general_request(transport_host, fleet, "slack")
    queue = project_requests._queue

    def broken(*args, **kwargs):
        raise RuntimeError("queue write failed")

    monkeypatch.setattr(project_requests, "_queue", broken)
    with pytest.raises(RuntimeError):
        release(client, headers, _link(request), to="Billing")
    assert owners(_link(request)) == [("choice", projects.GENERAL)]
    assert project_requests.get(identity)["state"] == "delivered"
    assert project_requests.active(projects.GENERAL) == identity
    monkeypatch.setattr(project_requests, "_queue", queue)
    retried = release(client, headers, _link(request), to="Billing")
    assert retried.status_code == 200, retried.text
    assert owners(_link(request)) == [("choice", billing["id"])]


def test_a_delivered_request_is_closed_and_its_binding_cleared(transport_host, fleet):
    client, _transports, _component = transport_host
    billing, request, identity, headers = general_request(transport_host, fleet, "slack")
    assert project_requests.active(projects.GENERAL) == identity
    handed = release(client, headers, _link(request), to="Billing")
    assert handed.status_code == 200, handed.text
    assert handed.json()["previous"] == {"request_id": identity, "state": "invalid", "closed": True}
    assert project_requests.get(identity)["error"] == "Handed off to project Billing."
    assert project_requests.active(projects.GENERAL) is None


@pytest.mark.parametrize("notice", ["sending", "uncertain"])
def test_an_uncertain_source_notice_alone_does_not_keep_the_request_bound(transport_host, fleet, notice):
    client, _transports, _component = transport_host
    billing, request, identity, headers = general_request(transport_host, fleet, "slack")
    with projects.database() as db:
        db.execute("UPDATE requests SET prompt_state=? WHERE id=?", (notice, identity))
    handed = release(client, headers, _link(request), to="Billing")
    assert handed.status_code == 200, handed.text
    assert handed.json()["previous"] == {"request_id": identity, "state": "invalid", "closed": True}
    assert project_requests.get(identity)["prompt_state"] == notice
    assert project_requests.active(projects.GENERAL) is None
    assert "uncertain_actions" not in take(client, credential(billing["id"]))[0]["data"]["handoff"]
    assert take(client, headers) == []
    fresh = _post(client, body("matrix", threaded=True))
    assert [note["data"]["request_id"] for note in take(client, headers)] == [fresh["request_id"]]


def test_every_open_request_on_the_thread_is_closed_not_only_the_newest(transport_host, fleet):
    client, _transports, _component = transport_host
    _billing, request, bound, headers = general_request(transport_host, fleet, "slack")
    queued = _post(client, _next(request))["request_id"]
    assert queued != bound and project_requests.active(projects.GENERAL) == bound
    with projects.database() as db:
        db.execute("UPDATE requests SET prompt_state='uncertain' WHERE id=?", (queued,))
    handed = release(client, headers, _link(request), to="Billing")
    assert handed.status_code == 200, handed.text
    assert handed.json()["previous"] == {"request_id": queued, "state": "invalid", "closed": True,
                                         "also_closed": [bound]}
    assert [project_requests.get(identity)["state"] for identity in (bound, queued)] == ["invalid", "invalid"]
    assert project_requests.active(projects.GENERAL) is None
    assert take(client, headers) == []


def test_an_uncertain_request_keeps_its_receipts_and_does_not_block_the_handoff(transport_host, fleet):
    client, _transports, _component = transport_host
    billing, request, identity, headers = general_request(transport_host, fleet, "slack")
    with projects.database() as db:
        db.execute("INSERT INTO request_actions (id,request_id,method,path,state,error) VALUES "
                   "('uncertain-spawn',?,'POST','/api/sessions','uncertain','connection reset')", (identity,))
    handed = release(client, headers, _link(request), to="Billing")
    assert handed.status_code == 200, handed.text
    previous = handed.json()["previous"]
    assert (previous["request_id"], previous["state"], previous["closed"]) == (identity, "delivered", False)
    states = [row["state"] for row in project_requests.listing() if row["id"] == identity][0:1]
    assert states == ["delivered"]
    kept = [row for row in project_requests.listing() if row["id"] == identity][0]["actions"]
    assert {"method": "POST", "path": "/api/sessions", "state": "uncertain", "error": "connection reset"} in kept
    assert "requests park" in handed.json()["previous"]["instruction"]
    delivered = take(client, credential(billing["id"]))[0]["data"]
    assert delivered["actions"] == []
    assert delivered["handoff"]["uncertain_actions"] == [
        {"request_id": identity, "method": "POST", "path": "/api/sessions", "state": "uncertain"}]
    assert "check with the operator before repeating" in delivered["instruction"]
    stuck = client.post(f"/api/assistant/requests/{identity}/reply", headers=headers, json={"text": "Still bound"})
    assert stuck.status_code == 409, stuck.text
    listed = [row for row in project_requests.listing() if row["id"] == identity][0]
    assert listed["parkable"] is True
    parked = client.post(f"/api/assistant/requests/{identity}/park", json={"workspace_id": projects.GENERAL,
                                                                           "reason": "Spawn outcome unknown; handed off"})
    assert parked.status_code == 200, parked.text
    assert project_requests.active(projects.GENERAL) is None
    after = [row for row in project_requests.listing() if row["id"] == identity][0]
    assert (after["state"], after["parkable"]) == ("delivered", False)
    assert {"method": "POST", "path": "/api/sessions", "state": "uncertain", "error": "connection reset"} in after["actions"]
    unrelated = client.post("/api/threads/release", headers=headers, json={"permalink": _link(body("matrix", threaded=True))})
    assert unrelated.status_code == 403, unrelated.text
    projects.rotate(projects.GENERAL, fleet)
    project_requests.recover(projects.GENERAL)
    assert project_requests.get(identity)["state"] == "delivered"
    assert project_requests.active(projects.GENERAL) is None


def test_a_requeued_uncertain_request_can_be_parked_after_the_handoff(transport_host, fleet):
    client, _transports, _component = transport_host
    billing, request, identity, _headers = general_request(transport_host, fleet, "slack")
    with projects.database() as db:
        db.execute("INSERT INTO request_actions (id,request_id,method,path,state,error) VALUES "
                   "('uncertain-spawn',?,'POST','/api/sessions','uncertain','connection reset')", (identity,))
    projects.rotate(projects.GENERAL, fleet)
    project_requests.recover(projects.GENERAL)
    assert project_requests.get(identity)["state"] == "queued"
    handed = client.post("/api/threads/release", json={"permalink": _link(request), "to": "Billing",
                                                        "reason": "Belongs to Billing", "workspace_id": projects.GENERAL})
    assert handed.status_code == 200, handed.text
    assert "requests park" in handed.json()["previous"]["instruction"]
    parked = client.post(f"/api/assistant/requests/{identity}/park", json={"workspace_id": projects.GENERAL,
                                                                           "reason": "Spawn outcome unknown"})
    assert parked.status_code == 200, parked.text
    headers = credential(projects.GENERAL)
    assert take(client, headers) == []
    fresh = _post(client, body("matrix", threaded=True))
    assert [note["data"]["request_id"] for note in take(client, headers)] == [fresh["request_id"]]
    assert project_requests.get(identity)["state"] == "queued"
    assert take(client, credential(billing["id"]))[0]["data"]["handoff"]["from_request"] == identity


def test_a_plain_release_without_any_claim_or_earlier_release_is_refused(transport_host, fleet):
    client, _transports, _component = transport_host
    billing, request, identity, headers = general_request(transport_host, fleet, "slack")
    finish(client, headers, identity)
    with projects.database() as db:
        db.execute("DELETE FROM thread_owners")
    refused = release(client, headers, _link(request))
    assert refused.status_code == 403, refused.text


def test_park_still_refuses_a_delivered_request_without_uncertain_effects(transport_host, fleet):
    client, _transports, _component = transport_host
    _billing, _request, identity, _headers = general_request(transport_host, fleet, "slack")
    refused = client.post(f"/api/assistant/requests/{identity}/park", json={"workspace_id": projects.GENERAL,
                                                                            "reason": "Nothing uncertain"})
    assert refused.status_code == 409, refused.text
    assert project_requests.active(projects.GENERAL) == identity


def test_a_plain_release_leaves_the_thread_unowned(transport_host, fleet):
    client, _transports, _component = transport_host
    _billing, request, identity, headers = general_request(transport_host, fleet, "matrix")
    finish(client, headers, identity)
    with projects.database() as db:
        db.execute("INSERT INTO thread_owners VALUES (?,?,?,?)",
                   (project_threads.key(_link(request)), "run:ended-run", projects.GENERAL, "ended-run"))
    released = release(client, headers, _link(request))
    assert released.status_code == 200, released.text
    assert sorted(released.json()["released"]) == ["choice", "run:ended-run"]
    assert released.json()["request_id"] is None
    assert owners(_link(request)) == []


def test_a_live_session_on_the_thread_refuses_and_names_the_disconnect(transport_host, fleet):
    client, _transports, component = transport_host
    billing, request, _identity, headers = general_request(transport_host, fleet, "slack")
    link = _link(request)
    worker = client.post("/api/sessions", headers=headers, json={
        "taskdef": "chat", "target": "general-run", "repo_url": "https://forge.test/example/repository",
        "targets": [link]})
    assert worker.status_code == 201, worker.text
    session = worker.json()["session_id"]
    thread = component.records(session)[0]["id"]
    before = owners(link)
    refused = release(client, headers, link, to="Billing")
    assert refused.status_code == 409, refused.text
    assert f"Session {session} holds this thread live" in detail(refused)
    assert f"lmer-ctl threads disconnect {session} {thread}" in detail(refused)
    assert owners(link) == before


def test_another_projects_assistant_cannot_release_the_claim(transport_host, fleet):
    client, _transports, _component = transport_host
    billing, request, _identity, _headers = general_request(transport_host, fleet, "slack")
    refused = release(client, credential(billing["id"]), _link(request), to="General")
    assert refused.status_code == 403, refused.text
    assert owners(_link(request)) == [("choice", projects.GENERAL)]


def test_conflicting_owners_refuse(transport_host, fleet):
    client, _transports, _component = transport_host
    billing, request, identity, headers = general_request(transport_host, fleet, "slack")
    finish(client, headers, identity)
    with projects.database() as db:
        db.execute("INSERT INTO thread_owners VALUES (?,?,?,NULL)",
                   (project_threads.key(_link(request)), f"project:{billing['id']}", billing["id"]))
    refused = release(client, headers, _link(request))
    assert refused.status_code == 409, refused.text
    assert "conflicting ownership" in detail(refused)


def test_a_standing_settings_thread_refuses(transport_host, fleet):
    client, _transports, _component = transport_host
    _billing, request, identity, headers = general_request(transport_host, fleet, "slack")
    finish(client, headers, identity)
    with projects.database() as db:
        db.execute("UPDATE projects SET settings=? WHERE id=?",
                   (json.dumps({"threads": [_link(request)]}), projects.GENERAL))
    refused = release(client, headers, _link(request))
    assert refused.status_code == 409, refused.text
    assert "standing thread list" in detail(refused)


def test_an_assistant_bound_to_another_request_must_finish_it_first(transport_host, fleet):
    client, _transports, _component = transport_host
    _billing, _request, _identity, headers = general_request(transport_host, fleet, "slack")
    other = body("matrix", threaded=True)
    project_threads.choose_unbound(_link(other), projects.GENERAL)
    refused = release(client, headers, _link(other))
    assert refused.status_code == 409, refused.text
    assert "Finish the active conversation request" in detail(refused)


@pytest.mark.parametrize("to,status", [("Nowhere", 404), ("General", 400)])
def test_an_unknown_or_own_target_is_refused(transport_host, fleet, to, status):
    client, _transports, _component = transport_host
    _billing, request, _identity, headers = general_request(transport_host, fleet, "slack")
    refused = release(client, headers, _link(request), to=to)
    assert refused.status_code == status, refused.text
    assert owners(_link(request)) == [("choice", projects.GENERAL)]


def test_the_operator_needs_a_reason_and_can_hand_off_a_pure_thread_claim(transport_host, fleet):
    client, _transports, _component = transport_host
    billing = projects.create("Billing")
    projects.request_start(projects.GENERAL)
    projects.reconcile(fleet)
    link = _link(body("slack", threaded=True))
    project_threads.choose_unbound(link, projects.GENERAL)
    scoped = {"workspace_id": projects.GENERAL}
    missing = client.post("/api/threads/release", json={"permalink": link, "to": "Billing", **scoped})
    assert missing.status_code == 400, missing.text
    handed = client.post("/api/threads/release", json={"permalink": link, "to": "Billing",
                                                        "reason": "Operator: belongs to Billing", **scoped})
    assert handed.status_code == 200, handed.text
    assert handed.json()["previous"] is None
    data = take(client, credential(billing["id"]))[0]["data"]
    assert data["permalink"] == link
    assert data["handoff"]["by"] == "operator"
    assert "lmer-ctl thread read" in data["message"]["text"]


def test_the_ctl_verb_builds_the_route_call():
    parser = ctl.create_parser()
    args = parser.parse_args(["threads", "release", "https://example.slack.com/archives/C1/p1",
                              "--to", "billing", "--reason", "not ours"])
    call = args.call(args)
    assert (call.method, call.path, call.body) == ("POST", "/api/threads/release", {
        "permalink": "https://example.slack.com/archives/C1/p1", "to": "billing", "reason": "not ours"})
    bare = parser.parse_args(["threads", "release", "https://example.slack.com/archives/C1/p1"])
    assert bare.call(bare).body == {"permalink": "https://example.slack.com/archives/C1/p1"}


@pytest.mark.parametrize("workspace", [True, False])
def test_the_assistant_is_told_when_to_hand_a_thread_on_only_with_projects(monkeypatch, workspace):
    strip_lmer_env(monkeypatch)
    root = Path(__file__).resolve().parents[1] / "taskdef"
    monkeypatch.setenv("LMER_TASKDEF_ROOT", str(root))
    monkeypatch.setenv("LMER_PLATFORM_URL", "http://platform.test")
    if workspace:
        monkeypatch.setenv("LMER_WORKSPACE_ID", "example-alpha")
        monkeypatch.setenv("LMER_WORKSPACE_NAME", "Example Alpha")
    prose = " ".join(render_taskdef_template(root / "orchestrate" / "instructions.txt",
                                             extra_context={"instructions_file": "/x"}).split())
    verb = "lmer-ctl threads release <permalink> --to <project> --reason <why>"
    assert (verb in prose) is workspace
    if workspace:
        args = ctl.create_parser().parse_args(["threads", "release", "https://example.slack.com/archives/C1/p1",
                                               "--to", "billing", "--reason", "theirs"])
        assert args.call(args).body["to"] == "billing"
