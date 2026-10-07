"""Both listener transports through durable ownership, HTTP admission and replies.

Transport clients are fakes; no Slack workspace or Matrix homeserver is contacted.
"""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
import pytest

from lmer_cli import assistant_mentions, dm_parents, thread_access, thread_posts
from lmer_platform import api, assistant, config, connectors, projects, project_requests, project_threads, runs, spawn
from lmer_platform import project_context as scope
from slack_chat import registry as thread_registry
from slack_chat.client import SlackError
from tests.test_platform_projects import fleet  # noqa: F401
from tests.test_assistant_mentions import Slack, Matrix, body, SLACK_USER, MATRIX_USER
from slack_chat.permalink import slack_thread_link
from matrix_chat.permalink import matrix_to_link
from lmer_platform import lifecycle, registry
from slack_chat import listener as slack_listener
from matrix_chat import listener as matrix_listener
from tests.connector_fakes import SlackRig, MatrixRig
from tests.timing_budgets import DRAIN


@pytest.fixture
def transport_host(fleet, tmp_path, monkeypatch):
    projects.migrate()
    for name, value in {
        assistant_mentions.ENABLED_ENV: "true", "SLACK_BOT_TOKEN": "xoxb-fixture",
        "LMER_SLACK_DM_ALLOWED_USERS": SLACK_USER,
        "LMER_MATRIX_HOMESERVER": "https://example.org", "LMER_MATRIX_USER_ID": "@bot:example.org",
        "LMER_MATRIX_ACCESS_TOKEN": "fixture-token", "LMER_MATRIX_ALLOWED_USERS": MATRIX_USER,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(dm_parents, "REGISTRY_DIR", str(tmp_path / "dms"))
    monkeypatch.setattr(thread_posts, "REGISTRY_DIR", str(tmp_path / "posts"))
    monkeypatch.setattr(thread_registry, "REGISTRY_DIR", str(tmp_path / "threads"))
    transports = {"slack": Slack(), "matrix": Matrix()}
    monkeypatch.setattr(thread_access, "_slack_client", lambda _token: transports["slack"])
    monkeypatch.setattr(thread_access, "_matrix_client", lambda _account: transports["matrix"])
    component = connectors.ThreadConnectors(slack_client=lambda _token: transports["slack"],
                                            matrix_client=lambda _account: transports["matrix"])
    monkeypatch.setattr(connectors, "_CONNECTORS", component)
    app = api.create_app(fleet, config.ensure_secret(fleet), state_builder=lambda *_a, **_k: {"runs": []})
    with TestClient(app, headers={"Authorization": f"Bearer {config.ensure_secret(fleet)}"}) as client:
        yield client, transports, component


def _post(client, value):
    result = client.post("/api/assistant/requests", json=value)
    assert result.status_code == 200, result.text
    return result.json()


def _link(request):
    event = request["event"]
    if request["transport"] == "slack":
        return slack_thread_link("example.slack.com", request["conversation"], event.get("thread_ts") or event["ts"])
    relation = event["content"].get("m.relates_to") or {}
    return matrix_to_link(request["conversation"], relation.get("event_id") or event["event_id"])


def _next(request, *, text=None, suffix="next"):
    event = dict(request["event"])
    if request["transport"] == "slack":
        event["ts"] = "1700000005.000001" if suffix == "next" else "1700000006.000001"
        if text is not None:
            event["text"] = text
    else:
        event["event_id"] = f"${suffix}"
        event["content"] = {**event["content"], **({"body": text} if text is not None else {})}
    return {**request, "event": event}


@pytest.mark.parametrize("owner", [None, "Alpha"])
@pytest.mark.parametrize("text,bot_identity,named,delivered", [
    ("<@UBOT1234> Beta hello", "UBOT1234", True, "hello"),
    ("  <@UBOT1234>: bEtA hello", "UBOT1234", True, "hello"),
    ("<@UOTHER123> Beta please discuss this with <@UBOT1234>", "UBOT1234", False, None),
    ("Please discuss Beta with <@UBOT1234>", "UBOT1234", False, None),
    ("<@UBOT1234> Beta hello", None, False, None),
])
def test_slack_prefix_requires_the_authenticated_bot_identity(
    transport_host, monkeypatch, owner, text, bot_identity, named, delivered,
):
    client, transports, _component = transport_host
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    monkeypatch.setattr(transports["slack"], "auth_test", lambda: bot_identity)
    request = _next(body("slack", threaded=True), text=text)
    request["event"]["bot_user_id"] = "UOTHER123"
    if owner:
        project_threads.choose_unbound(_link(request), alpha["id"])
    result = _post(client, request)
    row = project_requests.get(result["request_id"])
    if owner and named:
        assert row["state"] == "choice"
        assert row["project_id"] is None
        assert "does not own this thread" in transports["slack"].posts[-1][2]
    else:
        assert row["state"] == "queued"
        assert row["project_id"] == (alpha["id"] if owner else beta["id"] if named else "general")
        assert row["payload"]["message"]["text"] == (delivered if named else text)
    assert row["payload"]["message"]["original_text"] == text
    assert _post(client, request)["status"] == "duplicate"
    assert len(project_requests.listing()) == 1
    if owner:
        assert {item["project_id"] for item in project_threads.ownership(_link(request))} == {alpha["id"]}


def test_slack_identity_failure_remains_retryable_without_routing(transport_host, monkeypatch):
    client, transports, _component = transport_host
    beta = projects.create("Beta")
    request = _next(body("slack"), text="<@UBOT> Beta hello")
    def unavailable():
        raise SlackError("Slack auth.test response missing 'user_id' field")
    with monkeypatch.context() as patch:
        patch.setattr(transports["slack"], "auth_test", unavailable)
        response = client.post("/api/assistant/requests", json=request)
    assert response.status_code == 502, response.text
    assert "could not verify the Slack bot identity" in response.text
    assert project_requests.listing() == []
    assert transports["slack"].posts == []
    assert _post(client, request)["workspace_id"] == beta["id"]
    assert _post(client, request)["status"] == "duplicate"


def test_matrix_other_user_prefix_stays_ordinary_text(transport_host):
    client, _transports, _component = transport_host
    projects.create("Beta")
    text = "@other:example.org Beta discuss this with @bot:example.org"
    request = _next(body("matrix"), text=text)
    request["event"]["content"]["formatted_body"] = (
        '<a href="https://matrix.to/#/@other:example.org">@other:example.org</a> Beta discuss this with '
        '<a href="https://matrix.to/#/@bot:example.org">@bot:example.org</a>'
    )
    result = _post(client, request)
    assert result["workspace_id"] == "general"
    assert project_requests.get(result["request_id"])["payload"]["message"]["text"] == text
    assert _post(client, request)["status"] == "duplicate"


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_bound_threads_use_own_project_and_reply_only_to_source(transport_host, fleet, transport):
    client, transports, component = transport_host
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    projects.reconcile(fleet)
    request = body(transport, threaded=True)
    alpha_session = projects.status(alpha["id"])["session_id"]
    component.adopt_launch_targets(alpha_session, [_link(request)], by="operator")
    accepted = _post(client, request)
    assert accepted["workspace_id"] == alpha["id"]
    with scope.bound(beta["id"]):
        assert assistant.take_pending() == []
    notes = client.post("/api/assistant/pending", params={"workspace_id": alpha["id"]}).json()["pending"]
    assert len(notes) == 1
    assert notes[0]["data"]["request_id"] == accepted["request_id"]
    with scope.bound(alpha["id"]):
        token = config.active_assistant_credential()
    reply = client.post(f"/api/assistant/requests/{accepted['request_id']}/reply", json={"text": "source answer"},
                        headers={"Authorization": f"Bearer {token}"})
    assert reply.status_code == 200, reply.text
    assert transports[transport].posts[-1] == (request["conversation"], notes[0]["data"]["root"], "source answer")
    again = client.post(f"/api/assistant/requests/{accepted['request_id']}/reply", json={"text": "duplicate"},
                        headers={"Authorization": f"Bearer {token}"})
    assert again.json()["duplicate"] is True
    assert len(transports[transport].posts) == 1
    assert _post(client, request)["status"] == "duplicate"
    assert client.post(f"/api/assistant/requests/{accepted['request_id']}/reply",
                       json={"text": "wrong", "workspace_id": beta["id"]}).status_code == 409


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_named_stopped_destination_is_deduplicated_without_starting_it(transport_host, transport):
    client, transports, _component = transport_host
    alpha = projects.create("Alpha")
    projects.stop(alpha["id"])
    request = _next(body(transport, threaded=True), text="alpha hi")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _post(client, request), range(2)))
    first = next(row for row in results if row["status"] == "accepted")
    assert len(transports[transport].posts) == 1
    with scope.bound("general"):
        assert assistant.take_pending() == []
    assert first["workspace_id"] == alpha["id"]
    assert first["lifecycle"] == "stopped"
    assert _post(client, request)["status"] == "duplicate"
    assert projects.status(alpha["id"])["pending"] == 1
    projects.migrate()
    assert projects.status(alpha["id"])["desired"] == "stopped"
    assert project_requests.get(first["request_id"])["project_id"] == alpha["id"]


@pytest.mark.parametrize("transport", ["slack", "matrix"])
@pytest.mark.parametrize("text,named,delivered", [
    ("example alpha hi", True, "hi"),
    ("ExAmPlE AlPhA hi", True, "hi"),
    ("hi", False, "hi"),
    ("example alphabet hi", False, "example alphabet hi"),
    ("example alpha-beta hi", False, "example alpha-beta hi"),
    ("ask example alpha hi", False, "ask example alpha hi"),
])
def test_project_name_or_general_admission_and_source_reply(transport_host, fleet, transport, text, named, delivered):
    client, transports, component = transport_host
    alpha = projects.create("Example Alpha")
    projects.request_start("general")
    projects.reconcile(fleet)
    prefix = "<@UBOT>" if transport == "slack" else "@bot:example.org"
    request = _next(body(transport, threaded=True), text=f"{prefix} {text}")
    selected = alpha["id"] if named else "general"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: _post(client, request), range(2)))
    accepted = next(row for row in outcomes if row["status"] == "accepted")
    assert sorted(row["status"] for row in outcomes) == ["accepted", "duplicate"]
    assert accepted["workspace_id"] == selected
    assert transports[transport].posts == []
    assert component.sessions() == {}
    with scope.bound(alpha["id"] if not named else "general"):
        assert assistant.take_pending() == []
    projects.rotate(selected, fleet)
    projects.migrate()
    notes = client.post("/api/assistant/pending", params={"workspace_id": selected}).json()["pending"]
    assert len(notes) == 1
    data = notes[0]["data"]
    assert data["request_id"] == accepted["request_id"]
    assert data["message"]["text"] == delivered
    assert data["message"]["original_text"] == f"{prefix} {text}"
    assert data["permalink"] == _link(request)
    with scope.bound(selected):
        credential = config.active_assistant_credential()
    path = f"/api/assistant/requests/{accepted['request_id']}/reply"
    headers = {"Authorization": f"Bearer {credential}"}
    response = client.post(path, headers=headers, json={"text": "source answer"})
    assert response.status_code == 200, response.text
    assert transports[transport].posts == [(request["conversation"], data["root"], "source answer")]
    assert client.post(path, headers=headers, json={"text": "source answer"}).json()["duplicate"]
    assert _post(client, request)["status"] == "duplicate"
    assert len(transports[transport].posts) == 1


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_names_do_not_guess_ambiguous_or_overwrite_owned_threads(transport_host, transport):
    client, transports, _component = transport_host
    alpha, beta = [projects.create(name) for name in ("Example Alpha", "Example Beta")]
    request = _next(body(transport, threaded=True), text="hi")
    project_threads.choose_unbound(_link(request), alpha["id"])
    assert _post(client, request)["workspace_id"] == alpha["id"]
    conflict = _next(request, text="example beta hi", suffix="conflict")
    result = _post(client, conflict)
    assert result["status"] == "project_required"
    assert "does not own this thread" in transports[transport].posts[-1][2]
    assert {row["project_id"] for row in project_threads.ownership(_link(request))} == {alpha["id"]}
    projects.create("Example")
    ambiguous = _next(body(transport), text="Example Alpha hi", suffix="ambiguous")
    if transport == "slack":
        ambiguous["event"]["ts"] = "1700000007.000001"
    result = _post(client, ambiguous)
    assert result["status"] == "project_required"
    assert "more than one project name" in transports[transport].posts[-1][2]
    assert projects.status(beta["id"])["pending"] == 0
    assert projects.status("general")["pending"] == 0
    assert project_threads.ownership(_link(ambiguous)) == []


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_archived_name_is_reported_at_source_without_general_fallback(transport_host, transport):
    client, transports, _component = transport_host
    alpha = projects.create("Example Alpha")
    projects.stop(alpha["id"])
    projects.archive(alpha["id"], {"runs": []})
    request = _next(body(transport, threaded=True), text="example alpha hi")
    result = _post(client, request)
    assert result["status"] == "project_unavailable"
    assert result["workspace_id"] == alpha["id"]
    assert "Example Alpha is archived" in transports[transport].posts[0][2]
    assert _post(client, request)["status"] == "duplicate"
    assert len(transports[transport].posts) == 1
    assert projects.status("general")["pending"] == 0
    assert projects.status(alpha["id"])["pending"] == 0
    assert projects.status(alpha["id"])["desired"] == "stopped"


@pytest.mark.parametrize("quote", ["", "> <@user:example.org> old request\n\n", "> ordinary quote\n\n"])
def test_matrix_display_mention_and_reply_fallback_preserve_source(transport_host, quote):
    client, _transports, _component = transport_host
    alpha = projects.create("Example Alpha")
    original = quote + "Helper Bot: EXAMPLE ALPHA hi"
    request = _next(body("matrix", threaded=True), text=original)
    request["event"]["content"].update(format="org.matrix.custom.html", formatted_body=(
        '<mx-reply>quoted old request</mx-reply><a href="https://matrix.to/#/%40bot%3Aexample.org">Helper Bot</a>: EXAMPLE ALPHA hi'
    ))
    result = _post(client, request)
    ordinary_quote = quote.startswith("> ordinary")
    assert result["workspace_id"] == ("general" if ordinary_quote else alpha["id"])
    row = project_requests.get(result["request_id"])
    assert row["payload"]["message"]["text"] == (original if ordinary_quote else "hi")
    assert row["payload"]["root"] == "$root"
    assert row["payload"]["message"]["original_text"] == original


@pytest.mark.parametrize("transport", ["slack", "matrix"])
@pytest.mark.parametrize("known_parent", [False, True])
def test_dm_reply_keeps_parent_scope_without_name_routing_or_general_default(transport_host, fleet, transport, known_parent):
    client, _transports, _component = transport_host
    alpha = projects.create("Example Alpha")
    session = "assistant-old"
    if known_parent:
        projects.reconcile(fleet)
        session = projects.status(alpha["id"])["session_id"]
    request = _next(body(transport, threaded=True, dm=True, mention=False), text="General hi")
    dm_parents.remember({"conversation": {"permalink": _link(request),
                                         "recipient": SLACK_USER if transport == "slack" else MATRIX_USER}}, session)
    if known_parent:
        projects.rotate(alpha["id"], fleet)
    result = _post(client, request)
    assert projects.status("general")["pending"] == 0
    if known_parent:
        assert result["workspace_id"] == alpha["id"]
        row = project_requests.get(result["request_id"])
        assert row["payload"]["message"]["text"] == "General hi"
        assert row["payload"]["parent"]["session"] == session
    else:
        assert result["status"] == "project_required"
        assert projects.status(alpha["id"])["pending"] == 0


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_named_project_does_not_bypass_sender_authorization(transport_host, transport, monkeypatch):
    client, transports, _component = transport_host
    alpha = projects.create("Example Alpha")
    variable = "LMER_SLACK_DM_ALLOWED_USERS" if transport == "slack" else "LMER_MATRIX_ALLOWED_USERS"
    monkeypatch.setenv(variable, "UOTHER999" if transport == "slack" else "@other:example.org")
    result = _post(client, _next(body(transport), text="example alpha hi"))
    assert result == {"status": "ignored", "reason": "denied"}
    assert project_requests.listing() == []
    assert projects.status(alpha["id"])["pending"] == 0
    assert projects.status("general")["pending"] == 0
    assert transports[transport].posts == []


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_conflicting_ownership_requires_explicit_choice(transport_host, fleet, transport):
    client, _transports, component = transport_host
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    projects.reconcile(fleet)
    request = body(transport, threaded=True)
    project_threads.remember(projects.status(alpha["id"])["session_id"], _link(request))
    project_threads.remember(projects.status(beta["id"])["session_id"], _link(request))
    first = _post(client, request)
    assert first["status"] == "project_required"
    selected = _post(client, _next(request, text=f"project {first['request_id']} {beta['id']}"))
    assert selected["workspace_id"] == beta["id"]
    assert projects.status(alpha["id"])["pending"] == 0
    assert projects.status(beta["id"])["pending"] == 1
    assert component.sessions() == {}


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_worker_connector_preserved_and_moved_delayed_request_refused(transport_host, fleet, transport):
    client, _transports, component = transport_host
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    worker = spawn.spawn_session(fleet, spawn.SpawnRequest("chat", "task", repo_url="https://forge.test/repository",
                                                          workspace_id=alpha["id"]))
    request = body(transport, threaded=True)
    component.adopt_launch_targets(worker.session_id, [_link(request)], by="operator")
    before = component.records(worker.session_id)
    accepted = _post(client, request)
    assert accepted["workspace_id"] == alpha["id"]
    assert component.records(worker.session_id) == before
    assert component.live_holder(thread_access.parse_thread(_link(request))) == worker.session_id
    lifecycle.exit_session(worker.session_id)
    assert spawn.wait_for_exit_recorded(worker.session_id)
    projects.move("forge.test/repository/chat-task", beta["id"], expected_project=alpha["id"])
    with scope.bound(alpha["id"]):
        notes = assistant.take_pending()
    assert any(note["kind"] == "request_unavailable" for note in notes)
    assert project_requests.get(accepted["request_id"])["state"] == "invalid"
    with scope.bound(beta["id"]):
        assert all(note["kind"] != "conversation_request" for note in assistant.take_pending())
    assert _post(client, request)["status"] == "duplicate"


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_rotation_and_daemon_recreation_deliver_once_to_original_project(transport_host, fleet, transport):
    client, _transports, component = transport_host
    alpha = projects.create("Alpha")
    projects.reconcile(fleet)
    request = body(transport, threaded=True)
    component.adopt_launch_targets(projects.status(alpha["id"])["session_id"], [_link(request)], by="operator")
    accepted = _post(client, request)
    projects.rotate(alpha["id"], fleet)
    projects.migrate()
    replacement = api.create_app(fleet, config.ensure_secret(fleet), state_builder=lambda *_a, **_k: {})
    with TestClient(replacement, headers={"Authorization": f"Bearer {config.ensure_secret(fleet)}"}) as restarted:
        notes = restarted.post("/api/assistant/pending", params={"workspace_id": alpha["id"]}).json()["pending"]
        assert [note["data"]["request_id"] for note in notes] == [accepted["request_id"]]
        assert restarted.post("/api/assistant/pending", params={"workspace_id": alpha["id"]}).json()["pending"] == []
    assert project_requests.get(accepted["request_id"])["generation"] == projects.status(alpha["id"])["session_id"]


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_delivered_request_recovers_with_action_receipt_without_repeating_spawn(transport_host, fleet, transport):
    client, _transports, component = transport_host
    alpha = projects.create("Alpha")
    projects.reconcile(fleet)
    request = body(transport, threaded=True)
    component.adopt_launch_targets(projects.status(alpha["id"])["session_id"], [_link(request)], by="operator")
    accepted = _post(client, request)
    identity = accepted["request_id"]
    headers = {"X-Lmer-Workspace-Id": alpha["id"], "X-Lmer-Request-Id": identity}
    client.post("/api/assistant/pending", params={"workspace_id": alpha["id"]})
    action = {"taskdef": "chat", "target": "once", "no_repo": True}
    launched = client.post("/api/sessions", headers=headers, json=action)
    assert launched.status_code == 201, launched.text
    first = launched.json()["session_id"]
    projects.rotate(alpha["id"], fleet)
    notes = client.post("/api/assistant/pending", params={"workspace_id": alpha["id"]}).json()["pending"]
    assert len(notes) == 1
    assert notes[0]["data"]["request_id"] == identity
    assert notes[0]["data"]["actions"][0]["state"] == "complete"
    again = client.post("/api/sessions", headers=headers, json=action)
    assert again.status_code in (200, 201), again.text
    assert again.json()["session_id"] == first
    assert len([row for row in registry.list_sessions() if row.get("kind") != "assistant"]) == 1
    assert _post(client, request)["status"] == "duplicate"


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_unknown_reply_outcome_is_visible_and_never_automatically_repeated(transport_host, fleet, transport, monkeypatch):
    client, transports, component = transport_host
    alpha = projects.create("Alpha")
    projects.reconcile(fleet)
    request = body(transport, threaded=True)
    component.adopt_launch_targets(projects.status(alpha["id"])["session_id"], [_link(request)], by="operator")
    accepted = _post(client, request)
    client.post("/api/assistant/pending", params={"workspace_id": alpha["id"]})
    remote = transports[transport]
    method = "post_message" if transport == "slack" else "send_thread_text"
    actual = getattr(remote, method)
    def lost_response(*args, **kwargs):
        actual(*args, **kwargs)
        raise OSError("connection lost after remote acceptance")
    monkeypatch.setattr(remote, method, lost_response)
    path = f"/api/assistant/requests/{accepted['request_id']}/reply"
    result = client.post(path, json={"text": "only once", "workspace_id": alpha["id"]})
    assert result.status_code == 502, result.text
    projects.rotate(alpha["id"], fleet)
    assert client.post(path, json={"text": "only once", "workspace_id": alpha["id"]}).status_code == 409
    assert len(remote.posts) == 1
    assert project_requests.get(accepted["request_id"])["state"] == "reply_uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["slack", "matrix"])
async def test_real_listener_handlers_route_mentions_even_with_existing_worker_thread(transport_host, transport, monkeypatch):
    client, _transports, _component = transport_host
    alpha = projects.create("Alpha")
    request = body(transport, threaded=True)
    project_threads.choose_unbound(_link(request), alpha["id"])
    calls = []
    def forward(channel, conversation, event):
        calls.append(_post(client, {"transport": channel, "conversation": conversation, "event": event}))
        return calls[-1]
    monkeypatch.setattr(assistant_mentions, "forward", forward)
    manager = MagicMock()
    manager.touch.return_value = True
    manager.spawn = AsyncMock()
    if transport == "slack":
        monkeypatch.setattr(slack_listener, "MENTIONS", assistant_mentions.Settings(True))
        monkeypatch.setattr(slack_listener, "session_manager", manager)
        monkeypatch.setattr(slack_listener, "is_thread_connected", lambda *_args: True)
        await slack_listener.handle_mention(request["event"], AsyncMock())
        await slack_listener.handle_mention(request["event"], AsyncMock())
    else:
        monkeypatch.setattr(matrix_listener, "is_thread_connected", lambda *_args, **_kwargs: True)
        listener = matrix_listener.Listener(matrix_listener.ListenerConfig(), manager)
        await listener.handle(request["conversation"], request["event"])
        await listener.handle(request["conversation"], request["event"])
    assert [item["status"] for item in calls] == ["accepted", "duplicate"]
    assert calls[0]["workspace_id"] == alpha["id"]
    manager.spawn.assert_not_called()
    with scope.bound(alpha["id"]):
        assert len(assistant.take_pending()) == 1


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_real_worker_connector_routes_bot_mentions_but_keeps_normal_answers(transport_host, transport, tmp_path, monkeypatch):
    client, _transports, _component = transport_host
    alpha = projects.create("Alpha")
    rig = (SlackRig if transport == "slack" else MatrixRig)(tmp_path)
    component = rig.connector
    if transport == "matrix":
        monkeypatch.setenv("LMER_MATRIX_USER_ID", rig.config.user_id)
    received = []
    def forward(channel, conversation, event):
        received.append(_post(client, {"transport": channel, "conversation": conversation, "event": event}))
        return received[-1]
    monkeypatch.setattr(assistant_mentions, "forward", forward)
    try:
        component._inbound_once()
        question = rig.agent("question", "Worker question")
        component._outbound_once()
        if transport == "slack":
            mention = {"type": "app_mention", "user": SLACK_USER, "text": "<@UBOT> supervise this",
                       "ts": "1700009999.000001", "thread_ts": rig.config.thread_ts}
            request = {"transport": transport, "conversation": rig.config.channel, "event": mention}
        else:
            mention = rig.matrix.human_says("supervise this", sender=MATRIX_USER,
                                           **{"m.mentions": {"user_ids": [rig.config.user_id]}})
            request = {"transport": transport, "conversation": component.room_id, "event": mention}
        project_threads.choose_unbound(_link(request), alpha["id"])
        component._post_inbound(mention)
        assert received[0]["workspace_id"] == alpha["id"]
        assert rig.log.question(question.id).open
        if transport == "slack":
            normal = {"user": SLACK_USER, "text": "Worker answer", "ts": "1700009999.000002"}
        else:
            normal = rig.matrix.human_says("Worker answer", sender=MATRIX_USER)
        component._post_inbound(normal)
        assert not rig.log.question(question.id).open
        assert len(received) == 1
        records = rig.log.read_since(0)[0]
        assert not any("supervise this" in record.text for record in records)
        assert any(record.text == "Worker answer" for record in records)
    finally:
        assert component.stop(timeout=DRAIN, announce=False)
        rig.bus._session._http.close()
        rig.log.close()


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_uncertain_action_stays_visible_and_blocks_archive_after_source_reply(transport_host, fleet, transport, monkeypatch):
    client, _transports, component = transport_host
    alpha, beta = [projects.create(name) for name in ("Alpha", "Beta")]
    projects.reconcile(fleet)
    request = body(transport, threaded=True)
    component.adopt_launch_targets(projects.status(alpha["id"])["session_id"], [_link(request)], by="operator")
    identity = _post(client, request)["request_id"]
    client.post("/api/assistant/pending", params={"workspace_id": alpha["id"]})
    real_notify = assistant.notify
    def interrupted(*args, **kwargs):
        real_notify(*args, **kwargs)
        raise OSError("lost result after accepted action")
    with monkeypatch.context() as patch:
        patch.setattr(assistant, "notify", interrupted)
        with pytest.raises(OSError, match="lost result"):
            client.post("/api/project-relay", headers={"X-Lmer-Request-Id": identity},
                        json={"workspace_id": alpha["id"], "destination_id": beta["id"], "text": "explicit relay"})
    response = client.post(f"/api/assistant/requests/{identity}/reply",
                           json={"workspace_id": alpha["id"], "text": "Action outcome needs inspection"})
    assert response.status_code == 200, response.text
    visible = client.get("/api/assistant/requests", params={"workspace_id": alpha["id"]}).json()["requests"]
    row = next(row for row in visible if row["id"] == identity)
    assert row["state"] == "replied"
    assert row["actions"][0]["state"] == "uncertain"
    assert "lost result" in row["actions"][0]["error"]
    assert client.post(f"/api/projects/{alpha['id']}/archive").status_code == 409
    assert not projects.get(alpha["id"])["archived"]
