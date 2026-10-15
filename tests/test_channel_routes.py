"""Slack channel / Matrix room → project mention routing (issue #546).

Transport clients are fakes; no Slack workspace or Matrix homeserver is contacted.
"""
import zlib

import pytest

from lmer_platform import channel_routes, config, ctl, project_requests, project_threads, projects
from lmer_platform import project_context as scope
from slack_chat.client import SlackClient, SlackError
from tests.test_assistant_mentions import ROOM, body
from tests.test_platform_project_transports import _link, _next, _post, transport_host  # noqa: F401
from tests.test_platform_projects import fleet  # noqa: F401

CHANNEL = "C0123ABCD"
PREFIX = {"slack": "<@UBOT>", "matrix": "@bot:example.org"}
CONVERSATION = {"slack": CHANNEL, "matrix": ROOM}


def _map(client, project, *channels):
    response = client.put(f"/api/projects/{project['id']}/channel-routes", json={"channels": list(channels)})
    assert response.status_code == 200, response.text
    return response.json()["routes"]


def _mention(transport, text, *, suffix="next"):
    request = _next(body(transport, threaded=False), text=f"{PREFIX[transport]} {text}", suffix=suffix)
    if transport == "slack":
        request["event"]["ts"] = f"1700{zlib.crc32(suffix.encode()) % 1000000:06d}.000001"
    return request


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_a_mapped_channel_routes_like_the_project_name_prefix(transport_host, transport):
    client, _transports, _component = transport_host
    alpha = projects.create("Example Alpha")
    _map(client, alpha, CONVERSATION[transport])
    mapped = _post(client, _mention(transport, "hi", suffix="mapped"))
    prefixed = _post(client, _mention(transport, "example alpha hi", suffix="prefixed"))
    assert mapped["status"] == prefixed["status"] == "accepted"
    assert mapped["workspace_id"] == prefixed["workspace_id"] == alpha["id"]
    rows = [project_requests.get(result["request_id"]) for result in (mapped, prefixed)]
    assert [row["state"] for row in rows] == ["queued", "queued"]
    assert [row["payload"]["message"]["text"] for row in rows] == ["hi", "hi"]
    assert projects.status(alpha["id"])["pending"] == 2
    assert projects.status("general")["pending"] == 0


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_an_explicit_prefix_naming_another_project_still_wins(transport_host, transport):
    client, _transports, _component = transport_host
    alpha, beta = [projects.create(name) for name in ("Example Alpha", "Example Beta")]
    _map(client, alpha, CONVERSATION[transport])
    result = _post(client, _mention(transport, "example beta hi"))
    assert result["workspace_id"] == beta["id"]
    assert project_requests.get(result["request_id"])["payload"]["message"]["text"] == "hi"


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_thread_ownership_still_wins_over_the_channel(transport_host, transport):
    client, _transports, _component = transport_host
    alpha, beta = [projects.create(name) for name in ("Example Alpha", "Example Beta")]
    _map(client, alpha, CONVERSATION[transport])
    request = _next(body(transport, threaded=True), text=f"{PREFIX[transport]} hi")
    project_threads.choose_unbound(_link(request), beta["id"])
    assert _post(client, request)["workspace_id"] == beta["id"]


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_unmapped_channels_still_default_to_general(transport_host, transport):
    client, _transports, _component = transport_host
    alpha = projects.create("Example Alpha")
    _map(client, alpha, "C9999ZZZZ" if transport == "slack" else "!elsewhere:example.org")
    assert _post(client, _mention(transport, "hi"))["workspace_id"] == "general"


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_remapping_and_removing_take_effect_without_a_restart(transport_host, transport):
    client, _transports, _component = transport_host
    alpha, beta = [projects.create(name) for name in ("Example Alpha", "Example Beta")]
    _map(client, alpha, CONVERSATION[transport])
    assert _post(client, _mention(transport, "one", suffix="one"))["workspace_id"] == alpha["id"]
    _map(client, alpha)
    _map(client, beta, CONVERSATION[transport])
    assert _post(client, _mention(transport, "two", suffix="two"))["workspace_id"] == beta["id"]
    response = client.delete("/api/channel-routes", params={"channel": CONVERSATION[transport]})
    assert response.status_code == 200, response.text
    assert response.json()["routes"] == []
    assert _post(client, _mention(transport, "three", suffix="three"))["workspace_id"] == "general"


@pytest.mark.parametrize("transport", ["slack", "matrix"])
def test_an_archived_target_refuses_in_the_thread_without_general_fallback(transport_host, transport):
    client, transports, _component = transport_host
    alpha = projects.create("Example Alpha")
    _map(client, alpha, CONVERSATION[transport])
    projects.stop(alpha["id"])
    projects.archive(alpha["id"], {"runs": []})
    routes = client.get("/api/channel-routes").json()["routes"]
    assert [(route["project_id"], route["archived"]) for route in routes] == [(alpha["id"], True)]
    request = _mention(transport, "hi")
    result = _post(client, request)
    assert result["status"] == "project_unavailable"
    assert result["workspace_id"] == alpha["id"]
    notice = transports[transport].posts[0][2]
    assert "mapped to project Example Alpha, which is archived" in notice
    assert _post(client, request)["status"] == "duplicate"
    assert len(transports[transport].posts) == 1
    assert projects.status("general")["pending"] == 0
    assert client.put(f"/api/projects/{alpha['id']}/channel-routes", json={"channels": []}).status_code == 409
    assert client.delete("/api/channel-routes", params={"channel": CONVERSATION[transport]}).status_code == 200
    assert _post(client, _mention(transport, "again", suffix="again"))["workspace_id"] == "general"


def test_a_channel_maps_to_at_most_one_project(transport_host):
    client, _transports, _component = transport_host
    alpha, beta = [projects.create(name) for name in ("Example Alpha", "Example Beta")]
    _map(client, alpha, CHANNEL, ROOM)
    response = client.put(f"/api/projects/{beta['id']}/channel-routes", json={"channels": [CHANNEL]})
    assert response.status_code == 409
    assert "already mapped to project Example Alpha" in response.json()["detail"]["message"]
    assert {(route["conversation"], route["project_id"]) for route in client.get("/api/channel-routes").json()["routes"]} == {
        (CHANNEL, alpha["id"]), (ROOM, alpha["id"])}
    assert _map(client, alpha, ROOM, ROOM) == [{
        "transport": "matrix", "conversation": ROOM, "label": ROOM, "project_id": alpha["id"],
        "project_name": "Example Alpha", "archived": False}]


def test_only_an_operator_may_change_routes_and_any_caller_may_read_them(transport_host, fleet):
    client, _transports, _component = transport_host
    alpha = projects.create("Example Alpha")
    projects.reconcile(fleet)
    _map(client, alpha, CHANNEL)
    with scope.bound(alpha["id"]):
        headers = {"Authorization": f"Bearer {config.active_assistant_credential()}"}
    assert client.get("/api/channel-routes", headers=headers).json()["routes"][0]["project_id"] == alpha["id"]
    response = client.put(f"/api/projects/{alpha['id']}/channel-routes", headers=headers, json={"channels": []})
    assert response.status_code == 403
    response = client.delete("/api/channel-routes", headers=headers, params={"channel": CHANNEL})
    assert response.status_code == 403
    assert len(channel_routes.listing()) == 1


@pytest.mark.parametrize("spec,expected", [
    ("C0123ABCD", ("slack", "C0123ABCD", "C0123ABCD")),
    (" G0123ABCD ", ("slack", "G0123ABCD", "G0123ABCD")),
    ("https://example.slack.com/archives/C0123ABCD", ("slack", "C0123ABCD", "C0123ABCD")),
    ("https://example.slack.com/archives/C0123ABCD/p1700000000000000", ("slack", "C0123ABCD", "C0123ABCD")),
    ("https://app.slack.com/client/T0123ABCD/C0123ABCD", ("slack", "C0123ABCD", "C0123ABCD")),
    ("!room:example.org", ("matrix", "!room:example.org", "!room:example.org")),
    ("https://matrix.to/#/!room:example.org/$event?via=example.org", ("matrix", "!room:example.org", "!room:example.org")),
    ("https://matrix.to/#/%21room%3Aexample.org", ("matrix", "!room:example.org", "!room:example.org")),
])
def test_ids_and_links_resolve_without_a_transport_call(spec, expected, monkeypatch):
    monkeypatch.setattr(channel_routes.thread_access, "_slack_client", lambda _token: pytest.fail("no Slack call"))
    monkeypatch.setattr(channel_routes.thread_access, "_matrix_client", lambda _account: pytest.fail("no Matrix call"))
    assert channel_routes.resolve(spec, env={}) == expected


@pytest.mark.parametrize("spec,message", [
    ("D0123ABCD", "Direct messages cannot be mapped"),
    ("https://example.slack.com/archives/D0123ABCD/p1700000000000000", "direct messages"),
    ("https://example.com/somewhere", "not a Slack channel or Matrix room link"),
    ("", "1–500 characters"),
    ("not a channel!", "is not a Slack channel ID"),
])
def test_unmappable_specs_are_refused(spec, message):
    with pytest.raises(projects.ProjectError, match=message):
        channel_routes.resolve(spec, env={})


class _Lister:
    def __init__(self, found=(), error=None):
        self.found, self.error, self.asked = list(found), error, []

    def find_channels(self, name):
        self.asked.append(name)
        if self.error:
            raise self.error
        return self.found


@pytest.mark.parametrize("found,outcome", [
    (["C0123ABCD"], ("slack", "C0123ABCD", "#i-example")),
    ([], "No Slack channel named #i-example"),
    (["C0123ABCD", "C0456ABCD"], "More than one Slack channel"),
])
def test_a_slack_name_resolves_through_the_channel_list(monkeypatch, found, outcome):
    lister = _Lister(found)
    monkeypatch.setattr(channel_routes.thread_access, "_slack_client", lambda _token: lister)
    env = {"SLACK_BOT_TOKEN": "xoxb-fixture"}
    if isinstance(outcome, tuple):
        assert channel_routes.resolve("#i-example", env=env) == outcome
    else:
        with pytest.raises(projects.ProjectError, match=outcome):
            channel_routes.resolve("#i-example", env=env)
    assert lister.asked == ["i-example"]


def test_a_slack_name_without_list_scope_asks_for_the_id(monkeypatch):
    lister = _Lister(error=SlackError("missing_scope", code="missing_scope"))
    monkeypatch.setattr(channel_routes.thread_access, "_slack_client", lambda _token: lister)
    with pytest.raises(projects.ProjectError, match="give the channel ID or a link"):
        channel_routes.resolve("#i-example", env={"SLACK_BOT_TOKEN": "xoxb-fixture"})


def test_a_matrix_alias_resolves_to_its_room(monkeypatch):
    class Directory:
        def resolve_room(self, alias):
            assert alias == "#example:example.org"
            return "!resolved:example.org"
    monkeypatch.setattr(channel_routes.thread_access, "_matrix_client", lambda _account: Directory())
    env = {"LMER_MATRIX_HOMESERVER": "https://example.org", "LMER_MATRIX_USER_ID": "@bot:example.org",
           "LMER_MATRIX_ACCESS_TOKEN": "fixture-token"}
    for spec in ("#example:example.org", "https://matrix.to/#/%23example%3Aexample.org"):
        assert channel_routes.resolve(spec, env=env) == ("matrix", "!resolved:example.org", "#example:example.org")


def test_slack_client_find_channels_pages_and_matches_names_exactly():
    pages = [
        {"ok": True, "channels": [{"id": "C1", "name": "i-example"}, {"id": "C2", "name": "i-example-2"}],
         "response_metadata": {"next_cursor": "more"}},
        {"ok": True, "channels": [{"id": "C3", "name": "I-Example"}], "response_metadata": {"next_cursor": ""}},
    ]
    calls = []
    client = SlackClient(bot_token="xoxb-fixture")
    client._request = lambda method, endpoint, **kwargs: calls.append((method, endpoint, kwargs["params"])) or pages.pop(0)
    assert client.find_channels("#i-example") == ["C1", "C3"]
    assert [call[1] for call in calls] == ["conversations.list"] * 2
    assert calls[1][2]["cursor"] == "more"
    assert calls[0][2]["types"] == "public_channel,private_channel"


@pytest.mark.parametrize("argv,method,path,params,body", [
    (["projects", "channels", "list"], "GET", "/api/channel-routes", None, None),
    (["projects", "channels", "set", "abc", "C0123ABCD", "#i-example"], "PUT", "/api/projects/abc/channel-routes",
     None, {"channels": ["C0123ABCD", "#i-example"]}),
    (["projects", "channels", "set", "abc"], "PUT", "/api/projects/abc/channel-routes", None, {"channels": []}),
    (["projects", "channels", "remove", "#i-example"], "DELETE", "/api/channel-routes", {"channel": "#i-example"}, None),
])
def test_ctl_channel_verbs(argv, method, path, params, body):
    args = ctl.create_parser().parse_args(argv)
    call = args.call(args)
    assert (call.method, call.path, call.params, call.body) == (method, path, params, body)


def test_a_stored_name_keeps_its_channel_after_a_rename_and_removes_by_what_list_shows(fleet, monkeypatch):
    projects.migrate()
    alpha = projects.create("Example Alpha")
    lister = _Lister(["C0OLD1234"])
    monkeypatch.setattr(channel_routes.thread_access, "_slack_client", lambda _token: lister)
    env = {"SLACK_BOT_TOKEN": "xoxb-fixture"}
    channel_routes.replace(alpha["id"], ["#ops"], env=env)
    lister.found = ["C0NEW1234"]
    routes = channel_routes.replace(alpha["id"], ["#ops", ROOM], env=env)
    assert [(route["conversation"], route["label"]) for route in routes] == [("C0OLD1234", "#ops"), (ROOM, ROOM)]
    assert lister.asked == ["ops"]
    channel_routes.remove("#ops", env=env)
    assert [route["conversation"] for route in channel_routes.listing()] == [ROOM]
    assert lister.asked == ["ops"]


def test_another_projects_stale_name_does_not_shadow_a_live_lookup(fleet, monkeypatch):
    projects.migrate()
    alpha, beta = [projects.create(name) for name in ("Example Alpha", "Example Beta")]
    lister = _Lister(["C1OLD000"])
    monkeypatch.setattr(channel_routes.thread_access, "_slack_client", lambda _token: lister)
    env = {"SLACK_BOT_TOKEN": "xoxb-fixture"}
    channel_routes.replace(alpha["id"], ["#ops"], env=env)
    lister.found = ["C2NEW000"]
    routes = channel_routes.replace(beta["id"], ["#ops"], env=env)
    assert [(route["conversation"], route["label"]) for route in routes] == [("C2NEW000", "#ops")]
    assert lister.asked == ["ops", "ops"]


@pytest.mark.parametrize("spec,expected", [
    ("https://app.element.io/#/room/!room:example.org/$event", ("matrix", "!room:example.org", "!room:example.org")),
    ("https://chat.example.org/#/room/!room:example.org:8448", ("matrix", "!room:example.org:8448", "!room:example.org:8448")),
])
def test_element_room_links_resolve(spec, expected):
    assert channel_routes.resolve(spec, env={}) == expected


@pytest.mark.parametrize("spec", ["https://gitlab.example.com/archives/C0123ABCD", "!room:example.org/$event"])
def test_lookalikes_are_refused(spec):
    with pytest.raises(projects.ProjectError):
        channel_routes.resolve(spec, env={})
