"""The thread routes, the fleet fold and the assistant case (#412 T2, #429).

`GET`/`POST`/`DELETE /api/sessions/{id}/threads` over a real app with a real
component behind it, fake connector processes underneath. What is pinned is the
surface an operator meets: what a connect returns, what each refusal *says* (the
variable that is missing, the run that holds the thread), that the fleet payload
carries the connections the icons render from, and that a write on the
assistant's own session also moves its standing launch setting.
"""
from __future__ import annotations

import base64
import os

import pytest
from fastapi.testclient import TestClient

from lmer_platform import (
    api, assistant, connectors, registry, runs, session_threads, spawn, store,
)
from lmer_platform import config as cfg
from slack_chat import registry as thread_registry
from tests.conftest import strip_lmer_env

SECRET = "test-secret-value"
SLACK_LINK = "https://example.slack.com/archives/C0123ABCDEF/p1712345678000900"
OTHER_LINK = "https://example.slack.com/archives/C0123ABCDEF/p1712345678000901"
MATRIX_LINK = "https://matrix.to/#/%21room%3Aexample.org/%24event"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    monkeypatch.setattr(thread_registry, "REGISTRY_DIR", str(tmp_path / "threads"))
    return root


class FakeProcess:
    def __init__(self, argv, env):
        self.argv, self.env = list(argv), dict(env)
        self.pid = os.getpid()
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def component(platform_root, monkeypatch):
    built = connectors.ThreadConnectors(
        runner=lambda argv, env, log_path=None: FakeProcess(argv, env),
        environ={
            "SLACK_BOT_TOKEN": "xoxb-test",
            "LMER_MATRIX_HOMESERVER": "https://matrix.example.org",
            "LMER_MATRIX_USER_ID": "@lmer:example.org",
            "LMER_MATRIX_ACCESS_TOKEN": "secret-access-token",
        },
        matrix_client=lambda account: _FakeMatrix(),
        slack_client=lambda token: _FakeSlack(),
        clock=lambda: 0.0,
    )
    connectors.register_connectors(built)
    yield built
    connectors.register_connectors(None)


class _FakeSlack:
    """Slack reporting a root: for a root link, its own ts."""

    def get_replies(self, channel, ts, oldest=None):
        return [{"ts": ts}]


class _RefusingSlack:
    """Slack failing the resolution's read. *code* is Slack's own ``error``
    field — an answer — and ``None`` is a transport failure, which is not one."""

    def __init__(self, code):
        self.code = code

    def get_replies(self, channel, ts, oldest=None):
        from slack_chat.client import SlackError

        raise SlackError(
            f"Slack API error: {self.code}" if self.code
            else "Slack API rate limited after 3 retries (HTTP 429)",
            code=self.code,
        )


class _FakeMatrix:
    encrypted = True

    def resolve_room(self, room):
        return "!room:example.org"

    def room_is_encrypted(self, room_id):
        return self.encrypted


@pytest.fixture
def client(platform_root, component):
    return TestClient(api.create_app(cfg.load(), SECRET))


def auth():
    raw = f":{SECRET}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def live_session(session_id="s-1", *, kind="worker", slug="develop-412"):
    registry.register(
        session_id,
        kind=kind,
        pid=os.getpid(),
        run={"host": "gitlab.example.com", "project": "agents/global", "slug": slug},
        task={"taskdef": "develop", "target": "issue-412"},
        control={
            "host": "127.0.0.1",
            "port": 8123,
            "token_ref": str(spawn.token_file_for(session_id)),
        },
        started_at="2026-09-05T09:00:00Z",
    )
    spawn.token_file_for(session_id).write_text("control-token", encoding="utf-8")
    return session_id


class TestTheRoutes:
    def test_connect_list_disconnect(self, client):
        live_session()

        created = client.post(
            "/api/sessions/s-1/threads", json={"permalink": SLACK_LINK}, headers=auth(),
        )
        assert created.status_code == 201
        record = created.json()["thread"]
        assert record["channel"] == "slack" and record["running"] is True
        assert record["by"] == "operator" and record["origin"] == "live"

        listed = client.get("/api/sessions/s-1/threads", headers=auth())
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["threads"]] == [record["id"]]

        gone = client.delete(
            f"/api/sessions/s-1/threads/{record['id']}", headers=auth(),
        )
        assert gone.status_code == 200
        assert gone.json()["disconnected"]["id"] == record["id"]
        assert client.get("/api/sessions/s-1/threads", headers=auth()).json()["threads"] == []

    def test_a_link_that_is_not_a_thread_is_a_400(self, client):
        live_session()
        reply = client.post(
            "/api/sessions/s-1/threads",
            json={"permalink": "https://example.org/nope"},
            headers=auth(),
        )
        assert reply.status_code == 400
        assert "permalink" in reply.json()["detail"]

    def test_a_missing_credential_names_its_variable(self, client, component):
        live_session()
        component._environ = {}
        reply = client.post(
            "/api/sessions/s-1/threads", json={"permalink": SLACK_LINK}, headers=auth(),
        )
        assert reply.status_code == 400
        assert "SLACK_BOT_TOKEN" in reply.json()["detail"]

    def test_an_encrypted_room_is_named(self, client):
        live_session()
        reply = client.post(
            "/api/sessions/s-1/threads", json={"permalink": MATRIX_LINK}, headers=auth(),
        )
        assert reply.status_code == 400
        assert "!room:example.org" in reply.json()["detail"]

    def test_a_thread_another_run_holds_names_that_run(self, client):
        live_session("s-1", slug="develop-412")
        live_session("s-2", slug="review-99")
        runs.track("gitlab.example.com", "agents/global", "develop-412", source="spawned")
        client.post("/api/sessions/s-1/threads", json={"permalink": SLACK_LINK}, headers=auth())

        reply = client.post(
            "/api/sessions/s-2/threads", json={"permalink": SLACK_LINK}, headers=auth(),
        )

        assert reply.status_code == 409
        assert "develop-412" in reply.json()["detail"]

    def test_the_same_thread_twice_is_a_409(self, client):
        live_session()
        client.post("/api/sessions/s-1/threads", json={"permalink": SLACK_LINK}, headers=auth())
        again = client.post(
            "/api/sessions/s-1/threads", json={"permalink": SLACK_LINK}, headers=auth(),
        )
        assert again.status_code == 409

    def test_a_session_that_is_not_running_is_a_409(self, client):
        registry.register(
            "s-dead", kind="worker", pid=2 ** 22 - 1, started_at="2026-09-05T09:00:00Z",
        )
        reply = client.post(
            "/api/sessions/s-dead/threads", json={"permalink": SLACK_LINK}, headers=auth(),
        )
        assert reply.status_code == 409

    def test_an_unknown_session_is_a_404(self, client):
        reply = client.get("/api/sessions/s-nope/threads", headers=auth())
        assert reply.status_code == 404

    def test_disconnecting_something_that_is_not_connected_is_a_404(self, client):
        live_session()
        reply = client.delete(
            "/api/sessions/s-1/threads/slack:deadbeefdeadbeef", headers=auth(),
        )
        assert reply.status_code == 404


class TestOpeningAThread:
    """``POST …/threads {channel}`` — the same route, one step further (#429).

    The thread does not exist yet, so what is pinned is that the root message
    lands in the named channel saying which run this is, that the connection is
    recorded against the thread that now exists, and that everything refusable
    is refused *before* a message reaches anybody's channel.
    """

    @pytest.fixture(autouse=True)
    def _slack(self, monkeypatch):
        from lmer_cli import thread_access
        from tests.test_lmer_thread_access import FakeSlack

        client = FakeSlack(messages=[])
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        return client

    def test_the_root_names_the_run_and_the_thread_is_connected(self, client, _slack):
        live_session()

        created = client.post(
            "/api/sessions/s-1/threads", json={"channel": "#a-channel"}, headers=auth(),
        )

        assert created.status_code == 201
        record = created.json()["thread"]
        assert record["channel"] == "slack" and record["running"] is True
        assert record["permalink"] == (
            "https://20c.slack.com/archives/C0123ABCDEF/p1712345678000900"
        )
        channel, root = _slack.opened[0]
        assert channel == "#a-channel"
        # The fleet-view label, then the key that finds the run anywhere. No
        # control-UI link: this daemon's bind is loopback, and a 127.0.0.1 link
        # in somebody's channel is not a link.
        assert root == "develop-412\ngitlab.example.com/agents/global/develop-412"
        listed = client.get("/api/sessions/s-1/threads", headers=auth()).json()
        assert [item["id"] for item in listed["threads"]] == [record["id"]]

    def test_a_platform_opened_root_is_stamped_as_the_platform(self, client, _slack, tmp_path):
        """#427's attribution on #429's root. The operator opened this thread for
        the run, so the run should read the root as the platform addressing it —
        not as an own-echo to skip, which is what an unstamped root becomes."""
        from lmer_cli import thread_posts

        thread_posts.REGISTRY_DIR = str(tmp_path / "thread-posts")
        try:
            live_session()
            client.post(
                "/api/sessions/s-1/threads", json={"channel": "#a-channel"},
                headers=auth(),
            )
            stamped = thread_posts.lookup("C0123ABCDEF", "1712345678.000900")
        finally:
            thread_posts.REGISTRY_DIR = None

        assert stamped == thread_posts.Poster.for_platform("operator")
        assert stamped.author == "platform · operator"

    def test_a_title_replaces_the_first_line_and_nothing_else(self, client, _slack):
        live_session()
        client.post(
            "/api/sessions/s-1/threads",
            json={"channel": "#a-channel", "title": "the release cut"},
            headers=auth(),
        )
        assert _slack.opened[0][1] == (
            "the release cut\ngitlab.example.com/agents/global/develop-412"
        )

    def test_a_channel_the_bot_is_not_in_is_refused_by_name(self, client, monkeypatch):
        from lmer_cli import thread_access
        from tests.test_lmer_thread_access import FakeSlack

        monkeypatch.setattr(
            thread_access, "_slack_client",
            lambda token: FakeSlack(fail="not_in_channel", code="not_in_channel"),
        )
        live_session()

        reply = client.post(
            "/api/sessions/s-1/threads", json={"channel": "#a-channel"}, headers=auth(),
        )

        assert reply.status_code == 403
        assert "invite it" in reply.json()["detail"]

    def test_a_body_naming_both_a_thread_and_a_channel_is_refused(self, client, _slack):
        live_session()
        reply = client.post(
            "/api/sessions/s-1/threads",
            json={"channel": "#a-channel", "permalink": SLACK_LINK},
            headers=auth(),
        )
        assert reply.status_code == 400
        assert _slack.opened == []

    def test_a_session_with_no_control_plane_is_refused_before_the_post(
        self, client, _slack
    ):
        """The order the failures decide: a root message cannot be unposted, so
        what can be refused without asking Slack anything is refused first."""
        registry.register(
            "s-dead", kind="worker", pid=2 ** 22 - 1, started_at="2026-09-05T09:00:00Z",
        )
        reply = client.post(
            "/api/sessions/s-dead/threads", json={"channel": "#a-channel"},
            headers=auth(),
        )
        assert reply.status_code == 409
        assert _slack.opened == []

    def test_a_channel_that_names_no_transport_is_refused(self, client, _slack):
        live_session()
        reply = client.post(
            "/api/sessions/s-1/threads", json={"channel": "not/a/channel"},
            headers=auth(),
        )
        assert reply.status_code == 400
        assert _slack.opened == []


class TestARunActingOnItsOwnThreads:
    """What the servicer hands a run when it opens or connects a thread itself
    (issue #429) — the daemon's side of ``lmer-signal thread``.

    The same three functions the route calls, bound to one session, so what is
    pinned here is the binding: whose session it acts on, what the record says
    made the connection, and that a run addresses its threads by the permalink
    it holds rather than by an id it has never seen.
    """

    @pytest.fixture(autouse=True)
    def _slack(self, monkeypatch):
        from lmer_cli import thread_access
        from tests.test_lmer_thread_access import FakeSlack

        client = FakeSlack(messages=[])
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        return client

    def test_a_run_opens_a_thread_lets_it_go_and_takes_it_back(self, component):
        live_session()
        own = session_threads.SessionThreads("s-1")

        opened = own.open("#a-channel", text="watching the release")["thread"]
        assert opened["by"] == "agent" and opened["running"] is True
        assert [record["id"] for record in component.records("s-1")] == [opened["id"]]

        own.disconnect(opened["permalink"])
        assert component.records("s-1") == []

        # Reconnecting is the same verb, by the permalink the run kept.
        again = own.connect(opened["permalink"])["thread"]
        assert again["id"] == opened["id"] and again["origin"] == "live"

    def test_a_thread_this_run_does_not_hold_is_a_404_naming_the_link(self, component):
        live_session()
        with pytest.raises(connectors.ThreadError) as caught:
            session_threads.SessionThreads("s-1").disconnect(SLACK_LINK)
        assert caught.value.status == 404
        assert SLACK_LINK in str(caught.value)

    def test_connecting_a_channel_the_bot_is_not_in_is_refused(self, component):
        """Review finding on !299, round 2: `thread connect` is a door this MR
        adds, and it reached the same uninvited channels the scope guard closes
        for `open` — starting a standing mirror there rather than posting once.

        No scope heuristic needed: the resolution's own `conversations.replies`
        has already been told `not_in_channel`, and that is evidence rather than
        a failure to ask.
        """
        component._slack_client = lambda token: _RefusingSlack("not_in_channel")
        live_session()

        with pytest.raises(connectors.ThreadError) as caught:
            session_threads.SessionThreads("s-1").connect(SLACK_LINK)

        assert caught.value.status == 403
        assert "not_in_channel" in str(caught.value)
        assert "invite it" in str(caught.value)
        assert component.records("s-1") == []

    def test_a_transport_that_could_not_be_asked_still_connects(self, component):
        """The other half of that split, and the reason it is a split: a 429 or a
        network failure is not Slack saying no, and a directory lookup must not
        be what decides whether a thread can be connected."""
        component._slack_client = lambda token: _RefusingSlack(None)
        live_session()

        record = session_threads.SessionThreads("s-1").connect(SLACK_LINK)["thread"]

        assert record["running"] is True
        assert [item["id"] for item in component.records("s-1")] == [record["id"]]

    def test_opening_in_a_private_channel_without_the_read_scope_still_records(
        self, component, monkeypatch,
    ):
        """Round 3's second major, end to end: the documented scope set makes
        `groups:history` optional, so the root can post and the read back answer
        `missing_scope`. Refusing there would leave the root message in a human's
        private channel with no record to disconnect — round 1's finding through
        a door round 1 did not cover."""
        from lmer_cli import thread_access
        from tests.test_lmer_thread_access import FakeSlack

        class ReadRefused(FakeSlack):
            """One client behind both paths, as the real one is."""

            def get_replies(self, channel, ts, oldest=None):
                from slack_chat.client import SlackError

                raise SlackError("Slack API error: missing_scope", code="missing_scope")

        opener = ReadRefused(scopes=["chat:write"])
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: opener)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        component._slack_client = lambda token: opener
        live_session()

        record = session_threads.open_for(
            "s-1", "#a-private-channel", by="agent",
        )

        assert opener.opened[0][0] == "#a-private-channel"
        assert [item["id"] for item in component.records("s-1")] == [record["id"]]

    def test_a_thread_another_live_run_holds_is_refused_to_the_run_too(self, component):
        live_session("s-1", slug="develop-429")
        live_session("s-2", slug="review-99")
        session_threads.SessionThreads("s-2").connect(SLACK_LINK)

        with pytest.raises(connectors.ThreadError) as caught:
            session_threads.SessionThreads("s-1").connect(SLACK_LINK)

        assert caught.value.status == 409
        assert "review-99" in str(caught.value)


class TestTheFleetPayload:
    def test_a_row_carries_its_connections(self, client, component):
        live_session()
        runs.track(
            "gitlab.example.com", "agents/global", "develop-412",
            source="spawned", session_id="s-1", taskdef="develop", target="issue-412",
        )
        component.connect("s-1", SLACK_LINK, by="operator")

        payload = client.get("/api/state", headers=auth()).json()
        rows = [row for row in payload["runs"] if row["slug"] == "develop-412"]

        assert rows and [thread["permalink"] for thread in rows[0]["threads"]] == [SLACK_LINK]

    def test_a_row_with_no_session_has_an_empty_list_rather_than_nothing(self, client):
        runs.track("gitlab.example.com", "agents/global", "dormant-1", source="adopted")
        payload = client.get("/api/state", headers=auth()).json()
        rows = [row for row in payload["runs"] if row["slug"] == "dormant-1"]
        assert rows and rows[0]["threads"] == []


class TestTheAssistant:
    def test_connecting_the_assistant_edits_its_launch_setting(self, client):
        live_session("s-uber", kind="assistant")

        client.post(
            "/api/sessions/s-uber/threads", json={"permalink": SLACK_LINK}, headers=auth(),
        )

        stored = cfg.assistant_settings()["threads"].stored
        assert stored == [SLACK_LINK]

    def test_disconnecting_it_takes_the_thread_out_again(self, client):
        live_session("s-uber", kind="assistant")
        created = client.post(
            "/api/sessions/s-uber/threads", json={"permalink": SLACK_LINK}, headers=auth(),
        ).json()["thread"]

        client.delete(f"/api/sessions/s-uber/threads/{created['id']}", headers=auth())

        assert cfg.assistant_settings()["threads"].stored == []

    def test_a_worker_never_touches_the_setting(self, client):
        live_session("s-1")
        client.post("/api/sessions/s-1/threads", json={"permalink": SLACK_LINK}, headers=auth())
        assert cfg.assistant_settings()["threads"].stored is None

    def test_a_connect_that_would_make_the_launch_list_unusable_is_refused(self, client):
        """Review finding, measured: ``update_stored`` *tolerates* an unusable
        launch setting rather than raising, so a 17th link lands in config.json
        and the whole list then resolves to ``None`` — uber lmer rotating back
        connected to nothing, a moment after the pane showed sixteen icons."""
        live_session("s-uber", kind="assistant")
        cfg.update_stored({"assistant_threads": [
            f"https://example.slack.com/archives/C0123ABCDEF/p17123456780{i:05d}"
            for i in range(16)
        ]})

        refused = client.post(
            "/api/sessions/s-uber/threads", json={"permalink": SLACK_LINK}, headers=auth(),
        )

        assert refused.status_code == 409
        assert "at most 16" in refused.json()["detail"]
        # And nothing was recorded or started on the way to refusing.
        assert client.get("/api/sessions/s-uber/threads", headers=auth()).json()["threads"] == []
        assert len(cfg.assistant_settings()["threads"].stored) == 16

    def test_opening_a_thread_for_a_full_launch_list_posts_nothing(
        self, client, monkeypatch,
    ):
        """Review finding on !299, measured: the standing list's bound is
        refusable without asking any transport, but was only reached by the
        connect *after* the root message had landed — so the root sat in the
        channel with nothing connected, in the one case (uber lmer) this feature
        is aimed at."""
        from lmer_cli import thread_access
        from tests.test_lmer_thread_access import FakeSlack

        slack = FakeSlack(messages=[])
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: slack)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        live_session("s-uber", kind="assistant")
        cfg.update_stored({"assistant_threads": [
            f"https://example.slack.com/archives/C0123ABCDEF/p17123456780{i:05d}"
            for i in range(16)
        ]})

        refused = client.post(
            "/api/sessions/s-uber/threads", json={"channel": "#a-channel"},
            headers=auth(),
        )

        assert refused.status_code == 409
        assert "at most 16" in refused.json()["detail"]
        assert slack.opened == []
        assert client.get("/api/sessions/s-uber/threads", headers=auth()).json()["threads"] == []

    def test_a_worker_is_not_bounded_by_the_assistants_list(self, client):
        live_session("s-1")
        cfg.update_stored({"assistant_threads": [
            f"https://example.slack.com/archives/C0123ABCDEF/p17123456780{i:05d}"
            for i in range(16)
        ]})
        created = client.post(
            "/api/sessions/s-1/threads", json={"permalink": SLACK_LINK}, headers=auth(),
        )
        assert created.status_code == 201

    def test_the_status_carries_the_live_connections_not_the_launched_list(self, client):
        """``settings.threads`` is what the incarnation was started with; these
        are what it is connected to now, and the pane header renders these."""
        live_session("s-uber", kind="assistant")
        client.post(
            "/api/sessions/s-uber/threads", json={"permalink": OTHER_LINK}, headers=auth(),
        )

        status = assistant.status().to_dict()

        assert [thread["permalink"] for thread in status["threads"]] == [OTHER_LINK]
        assert status["settings"]["threads"] == []

    def test_a_standing_thread_a_worker_holds_shows_as_not_running(
        self, client, component,
    ):
        """uber lmer comes up without the thread rather than not at all, so the
        pane has to be able to say which thread and whose (issue #413)."""
        live_session("s-1", slug="develop-412")
        runs.track("gitlab.example.com", "agents/global", "develop-412", source="spawned")
        client.post("/api/sessions/s-1/threads", json={"permalink": SLACK_LINK}, headers=auth())
        live_session("s-uber", kind="assistant")
        component.adopt_launch_targets("s-uber", [SLACK_LINK], by="operator")

        component.reconcile()
        threads = assistant.status().to_dict()["threads"]

        assert [thread["permalink"] for thread in threads] == [SLACK_LINK]
        assert threads[0]["running"] is False
        assert "develop-412" in threads[0]["detail"]


class TestForgetting:
    def test_forgetting_a_run_drops_its_connections(self, client, component):
        live_session()
        runs.track(
            "gitlab.example.com", "agents/global", "develop-412",
            source="spawned", session_id="s-1",
        )
        client.post("/api/sessions/s-1/threads", json={"permalink": SLACK_LINK}, headers=auth())

        forgotten = client.post(
            "/api/runs/forget",
            json={"host": "gitlab.example.com", "project": "agents/global", "slug": "develop-412"},
            headers=auth(),
        )

        assert forgotten.status_code == 200 and forgotten.json()["forgotten"] is True
        assert component.records("s-1") == []


class TestSpawningOntoAHeldThread:
    """A launch names its threads too, and the same rule applies (issue #413).

    The refusal is the whole point of the route test: it happens before the
    spawn builds a command line, so there is no session to clean up and the
    thread is never posted to.
    """

    def test_a_target_another_live_run_holds_is_a_409(self, client, component):
        live_session("s-1", slug="develop-412")
        runs.track("gitlab.example.com", "agents/global", "develop-412", source="spawned")
        client.post("/api/sessions/s-1/threads", json={"permalink": SLACK_LINK}, headers=auth())

        reply = client.post(
            "/api/sessions",
            json={
                "taskdef": "chat",
                "target": "fleet",
                "no_repo": True,
                "targets": [SLACK_LINK],
            },
            headers=auth(),
        )

        assert reply.status_code == 409
        assert "develop-412" in reply.json()["detail"]
        assert list(component.sessions()) == ["s-1"], "the refused spawn recorded nothing"
        assert [entry["id"] for entry in registry.list_sessions(live_only=False)] == ["s-1"]

    def test_the_refusal_names_the_target_it_refused(self, client, component):
        """Every target is checked, not just the first — a launch may name up to
        sixteen and the operator has to know which one to deal with."""
        live_session("s-1", slug="develop-412")
        client.post("/api/sessions/s-1/threads", json={"permalink": OTHER_LINK}, headers=auth())

        reply = client.post(
            "/api/sessions",
            json={
                "taskdef": "chat",
                "target": "fleet",
                "no_repo": True,
                "targets": [SLACK_LINK, OTHER_LINK],
            },
            headers=auth(),
        )

        assert reply.status_code == 409
        assert OTHER_LINK in reply.json()["detail"]

    def test_a_free_thread_still_launches_and_becomes_a_connection(
        self, platform_root, component, tmp_path,
    ):
        """The other half of the rule: the check refuses a *held* thread, and a
        spawn naming a free one goes through to the launch it always did."""
        script = tmp_path / "fake-lmer"
        script.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
        script.chmod(0o755)
        live_session("s-1", slug="develop-412")
        connectors.register_connectors(component)

        result = spawn.spawn_session(
            cfg.load({"lmer_bin": str(script)}),
            spawn.SpawnRequest(
                taskdef="chat", target="fleet", no_repo=True, targets=[OTHER_LINK],
            ),
        )
        try:
            assert [
                record["permalink"] for record in component.records(result.session_id)
            ] == [OTHER_LINK]
        finally:
            os.kill(result.pid, 9)
