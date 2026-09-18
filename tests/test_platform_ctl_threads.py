"""``lmer-ctl threads`` (issue #412 T5, #429).

A mirror of the routes and nothing else — each verb builds one call and
does no thinking of its own, which is this CLI's rule. Driven through the real
FastAPI app so the paths and the bodies are checked against what actually serves
them, not against a copy of them written here.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from lmer_platform import api, connectors, ctl, registry, spawn, store
from lmer_platform import config as cfg
from slack_chat import registry as thread_registry
from tests.conftest import strip_lmer_env

SECRET = "test-secret-value"
SLACK_LINK = "https://example.slack.com/archives/C0123ABCDEF/p1712345678000900"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


class FakeProcess:
    def __init__(self, argv, env):
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
def platform(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    monkeypatch.setattr(thread_registry, "REGISTRY_DIR", str(tmp_path / "threads"))
    component = connectors.ThreadConnectors(
        runner=lambda argv, env, log_path=None, *, pass_fds=(): FakeProcess(argv, env),
        environ={"SLACK_BOT_TOKEN": "xoxb-test"},
        slack_client=lambda token: _FakeSlack(),
        clock=lambda: 0.0,
    )
    connectors.register_connectors(component)
    monkeypatch.setenv("LMER_PLATFORM_URL", "http://testserver")
    monkeypatch.setenv("LMER_PLATFORM_SECRET", SECRET)
    registry.register(
        "s-1", kind="worker", pid=os.getpid(),
        run={"host": "gitlab.example.com", "project": "agents/global", "slug": "develop-412"},
        control={
            "host": "127.0.0.1", "port": 8123,
            "token_ref": str(spawn.token_file_for("s-1")),
        },
        started_at="2026-09-05T09:00:00Z",
    )
    spawn.token_file_for("s-1").write_text("control-token", encoding="utf-8")
    client = TestClient(api.create_app(cfg.load(), SECRET))
    yield client
    client.close()
    component.stop()
    connectors.register_connectors(None)


class _FakeSlack:
    """Slack reporting a root: for a root link, its own ts."""

    def get_replies(self, channel, ts, oldest=None):
        return [{"ts": ts}]


def run(client, capsys, argv):
    code = ctl.main(argv, transport=_Transport(client))
    out = capsys.readouterr()
    return code, out


class _Transport:
    """``requests``' signature over the app under test."""

    def __init__(self, client):
        self.client = client

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        return self.client.request(
            method, url, params=params, json=json, headers=headers,
        )


def auth():
    raw = f":{SECRET}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


class TestTheVerbs:
    def test_connect_list_disconnect_round_trip(self, platform, capsys):
        code, out = run(platform, capsys, ["threads", "connect", "s-1", SLACK_LINK])
        assert code == 0, out.err
        thread = json.loads(out.out)["thread"]
        assert thread["channel"] == "slack"

        code, out = run(platform, capsys, ["threads", "list", "s-1"])
        assert code == 0
        assert [item["id"] for item in json.loads(out.out)["threads"]] == [thread["id"]]

        code, out = run(platform, capsys, ["threads", "disconnect", "s-1", thread["id"]])
        assert code == 0
        assert json.loads(out.out)["disconnected"]["id"] == thread["id"]

    def test_open_reaches_the_daemon_and_answers_with_the_new_thread(
        self, platform, capsys, monkeypatch,
    ):
        from lmer_cli import thread_access
        from tests.test_lmer_thread_access import FakeSlack

        slack = FakeSlack(messages=[])
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: slack)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

        code, out = run(platform, capsys, ["threads", "open", "s-1", "#a-channel"])

        assert code == 0, out.err
        thread = json.loads(out.out)["thread"]
        assert thread["permalink"] == (
            "https://20c.slack.com/archives/C0123ABCDEF/p1712345678000900"
        )
        assert slack.opened[0][0] == "#a-channel"

    def test_a_refusal_reaches_the_operator_with_its_reason(self, platform, capsys):
        code, out = run(
            platform, capsys, ["threads", "connect", "s-1", "https://example.org/no"],
        )
        assert code != 0
        assert "permalink" in (out.err + out.out)

    def test_the_group_without_a_verb_says_so(self, platform, capsys):
        code, out = run(platform, capsys, ["threads"])
        assert code != 0
        assert "name a command" in out.err


class TestTheCalls:
    """Each verb is one call and nothing else — this CLI's whole contract."""

    def test_the_routes_are_what_is_spelled(self):
        parser = ctl.create_parser()

        listed = parser.parse_args(["threads", "list", "s-1"])
        assert listed.call(listed) == ctl.Call("GET", "/api/sessions/s-1/threads")

        connected = parser.parse_args(["threads", "connect", "s-1", SLACK_LINK])
        assert connected.call(connected) == ctl.Call(
            "POST", "/api/sessions/s-1/threads", body={"permalink": SLACK_LINK},
        )

        gone = parser.parse_args(["threads", "disconnect", "s-1", "slack:abc"])
        assert gone.call(gone) == ctl.Call("DELETE", "/api/sessions/s-1/threads/slack:abc")

    def test_open_is_the_same_route_with_a_channel_instead_of_a_link(self):
        """The one body that names a place to make a thread rather than one that
        exists (issue #429). A title only when the operator gave one — the run's
        own is what the daemon uses otherwise."""
        parser = ctl.create_parser()

        opened = parser.parse_args(["threads", "open", "s-1", "#a-channel"])
        assert opened.call(opened) == ctl.Call(
            "POST", "/api/sessions/s-1/threads", body={"channel": "#a-channel"},
        )

        titled = parser.parse_args(
            ["threads", "open", "s-1", "#a-channel", "--title", "the release cut"],
        )
        assert titled.call(titled) == ctl.Call(
            "POST", "/api/sessions/s-1/threads",
            body={"channel": "#a-channel", "title": "the release cut"},
        )


class TestARefusedAttach:
    """The 409 an agent has to act on, all the way to its stdout (issue #413).

    `_emit` is generic, so nothing here is a product change — which is the
    reason to pin it: the refusal names the run holding the thread, and that
    name is only useful if it survives the relay.
    """

    def test_the_status_and_the_holder_reach_the_caller(self, platform, capsys):
        registry.register(
            "s-2", kind="worker", pid=os.getpid(),
            run={"host": "gitlab.example.com", "project": "agents/global", "slug": "review-99"},
            control={
                "host": "127.0.0.1", "port": 8123,
                "token_ref": str(spawn.token_file_for("s-2")),
            },
            started_at="2026-09-05T09:00:00Z",
        )
        spawn.token_file_for("s-2").write_text("control-token", encoding="utf-8")
        run(platform, capsys, ["threads", "connect", "s-1", SLACK_LINK])

        code, out = run(platform, capsys, ["threads", "connect", "s-2", SLACK_LINK])

        assert code != 0
        refusal = json.loads(out.err)
        assert refusal["status"] == 409
        assert "develop-412" in refusal["body"]["detail"]

class TestTheThreadVerbs:
    """``lmer-ctl thread read|post`` — one thread, by permalink, connected to
    nothing (issue #414)."""

    def test_read_and_post_round_trip_through_the_daemon(self, platform, capsys, monkeypatch):
        from lmer_cli import thread_access
        from tests.test_lmer_thread_access import FakeSlack

        slack = FakeSlack(messages=[
            {"ts": "1712345678.000900", "user": "U1", "text": "what did we decide?"},
        ])
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: slack)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

        code, out = run(platform, capsys, ["thread", "read", SLACK_LINK])
        assert code == 0, out.err
        assert [m["text"] for m in json.loads(out.out)["messages"]] == ["what did we decide?"]

        code, out = run(platform, capsys, ["thread", "post", SLACK_LINK, "merged as 7ce6360"])
        assert code == 0, out.err
        # The operator's credential, not the text, is what the thread is told.
        assert slack.posted[0][2] == "[operator] merged as 7ce6360"

    def test_the_two_routes_are_what_is_spelled(self):
        parser = ctl.create_parser()

        read = parser.parse_args(["thread", "read", SLACK_LINK, "--limit", "5"])
        assert read.call(read) == ctl.Call(
            "GET", "/api/comms/thread", params={"permalink": SLACK_LINK, "limit": 5},
        )

        posted = parser.parse_args(["thread", "post", SLACK_LINK, "done"])
        assert posted.call(posted) == ctl.Call(
            "POST", "/api/comms/thread", body={"permalink": SLACK_LINK, "text": "done"},
        )

    def test_reading_a_saved_post_response_does_not_route_a_later_turn(
        self, platform, capsys, monkeypatch, tmp_path, routing_call,
    ):
        from hooks.slack_reply_guard import evaluate, owed_reply_chars
        from lmer_cli import thread_access
        from tests.test_lmer_thread_access import FakeSlack
        from tests.test_slack_reply_guard import (
            _assistant_bash, _assistant_text, _tool_result, _user_text,
        )

        slack = FakeSlack()
        monkeypatch.setattr(thread_access, "_slack_client", lambda token: slack)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        routing_call("prior-post")
        prior = [_user_text("Send the earlier reply."),
                 _assistant_bash("lmer-ctl thread post ...", tool_id="prior-post")]
        code, output = run(platform, capsys, ["thread", "post", SLACK_LINK, "Earlier reply"])
        assert code == 0, output.err
        prior.append(_tool_result(output.out, tool_use_id="prior-post"))
        assert evaluate(prior + [_assistant_text("x" * 79)]) is None
        routing_call("read-this-turn")
        saved = tmp_path / "previous-post.json"
        saved.write_text(output.out)
        replay = subprocess.check_output(["cat", str(saved)], text=True, timeout=30)
        text = "This later answer has not been sent to the operator and must trigger the guard."
        events = prior + [_user_text("Explain the earlier send."),
                          _assistant_bash(f"cat {saved}", tool_id="read-this-turn"),
                          _tool_result(replay, tool_use_id="read-this-turn"),
                          _assistant_text(text)]

        assert len(slack.posted) == 1
        assert owed_reply_chars(events) == len(text)
        assert evaluate(events) is not None


class TestSpawnReferences:
    """`lmer-ctl spawn --reference-thread` (issue #414): the one flag spelling
    that carries a permalink and a title."""

    def test_each_flag_becomes_one_reference_object(self):
        parser = ctl.create_parser()
        args = parser.parse_args([
            "spawn", "develop", "https://git.example.com/g/p/-/issues/1",
            "--reference-thread", f"{SLACK_LINK}|the design thread",
            "--reference-thread", SLACK_LINK,
        ])
        assert args.call(args).body["references"] == [
            {"permalink": SLACK_LINK, "title": "the design thread"},
            {"permalink": SLACK_LINK},
        ]

    def test_a_title_may_contain_a_pipe(self):
        parser = ctl.create_parser()
        args = parser.parse_args([
            "spawn", "develop", "t", "--reference-thread", f"{SLACK_LINK}|a|b",
        ])
        assert args.call(args).body["references"][0]["title"] == "a|b"

    def test_a_spawn_without_the_flag_sends_no_references(self):
        parser = ctl.create_parser()
        args = parser.parse_args(["spawn", "develop", "t"])
        assert "references" not in args.call(args).body
