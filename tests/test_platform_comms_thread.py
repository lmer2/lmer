"""The daemon's own two thread verbs (issue #414).

``GET``/``POST /api/comms/thread`` do the same work the servicer does for a
session, in the daemon, with the daemon's credentials — for the UI and for uber
lmer through ``lmer-ctl thread``. What is pinned here is the attribution rule:
the prefix a thread sees comes from the caller's **credential**, the way
``source.channel`` does, and never from anything in the body.
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from lmer_cli import thread_access, thread_posts
from lmer_platform import api, store
from lmer_platform import config as cfg
from tests.conftest import strip_lmer_env
from tests.test_lmer_thread_access import SLACK_LINK, FakeSlack

SECRET = "test-secret-value"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    root = tmp_path / "platform"
    monkeypatch.setattr(store, "PLATFORM_DIR", str(root))
    return root


@pytest.fixture
def client(platform_root, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    return TestClient(api.create_app(cfg.load(), SECRET))


@pytest.fixture(autouse=True)
def registry_dir(tmp_path, monkeypatch):
    """Where a post records who made it, so a connector relays it (#427)."""
    monkeypatch.setattr(thread_posts, "REGISTRY_DIR", str(tmp_path / "thread-posts"))


@pytest.fixture
def slack(monkeypatch):
    client = FakeSlack(messages=[
        {"ts": "1712345678.000900", "user": "U1", "text": "what did we decide?"},
    ])
    monkeypatch.setattr(thread_access, "_slack_client", lambda token: client)
    return client


def auth(secret=SECRET):
    raw = f":{secret}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


class TestReading:
    def test_a_thread_reads_with_the_daemons_own_credentials(self, client, slack):
        body = client.get(
            "/api/comms/thread", params={"permalink": SLACK_LINK}, headers=auth(),
        ).json()
        assert body["thread"]["channel"] == "slack"
        assert [record["text"] for record in body["messages"]] == ["what did we decide?"]

    def test_a_link_that_names_no_thread_is_a_400(self, client, slack):
        refused = client.get(
            "/api/comms/thread", params={"permalink": "https://example.org/nope"}, headers=auth(),
        )
        assert refused.status_code == 400

    def test_a_channel_the_bot_cannot_see_keeps_its_own_status(self, client, monkeypatch):
        monkeypatch.setattr(
            thread_access, "_slack_client",
            lambda token: FakeSlack(
                fail="Slack API error: channel_not_found", code="channel_not_found"
            ),
        )
        refused = client.get(
            "/api/comms/thread", params={"permalink": SLACK_LINK}, headers=auth(),
        )
        assert refused.status_code == 403
        assert "invite the bot" in refused.json()["detail"]

    def test_without_a_token_the_refusal_names_the_daemons_environment(
        self, platform_root, monkeypatch
    ):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        client = TestClient(api.create_app(cfg.load(), SECRET))
        refused = client.get(
            "/api/comms/thread", params={"permalink": SLACK_LINK}, headers=auth(),
        )
        assert refused.status_code == 400
        assert "SLACK_BOT_TOKEN" in refused.json()["detail"]
        assert "daemon" in refused.json()["detail"]


class TestPosting:
    def test_the_operator_posts_as_the_operator(self, client, slack):
        posted = client.post(
            "/api/comms/thread",
            json={"permalink": SLACK_LINK, "text": "merged as 7ce6360"},
            headers=auth(),
        )
        assert posted.status_code == 200, posted.text
        assert slack.posted[0][2] == "[operator] merged as 7ce6360"

    def test_the_assistant_posts_as_uber_lmer(self, client, slack):
        minted = cfg.mint_assistant_credential()
        client.post(
            "/api/comms/thread",
            json={"permalink": SLACK_LINK, "text": "the review is posted"},
            headers=bearer(minted),
        )
        assert slack.posted[0][2] == "[uber lmer] the review is posted"

    def test_the_body_cannot_choose_who_the_thread_thinks_wrote_it(self, client, slack):
        client.post(
            "/api/comms/thread",
            json={"permalink": SLACK_LINK, "text": "ship it", "author": "vegu", "label": "vegu"},
            headers=auth(),
        )
        assert slack.posted[0][2] == "[operator] ship it"

    @pytest.mark.parametrize("headers, label", [
        (auth(), "operator"),
        (None, "uber lmer"),
    ])
    def test_the_platform_s_own_post_reaches_a_connected_run(self, client, slack, headers, label):
        """The credential names it for the humans in the thread AND for the
        connector watching it, so a post from the UI is not skipped as an echo
        (#427)."""
        if headers is None:
            headers = bearer(cfg.mint_assistant_credential())
        posted = client.post(
            "/api/comms/thread", json={"permalink": SLACK_LINK, "text": "have a look"},
            headers=headers,
        )
        ref = posted.json()["posted"]["ref"]
        assert thread_posts.lookup("C0123ABCDEF", "1712345679.000100",
        ) == thread_posts.Poster.for_platform(label)

    def test_an_empty_post_is_refused(self, client, slack):
        refused = client.post(
            "/api/comms/thread", json={"permalink": SLACK_LINK, "text": "  "}, headers=auth(),
        )
        assert refused.status_code == 400
        assert slack.posted == []
