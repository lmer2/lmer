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


class TestAssistantConversation:
    @pytest.fixture(autouse=True)
    def assistant_pane(self, platform_root, tmp_path, monkeypatch):
        from lmer_platform import comms, registry, transcripts
        monkeypatch.setattr(transcripts, 'TRANSCRIPT_ROOT', str(tmp_path / 'transcripts'))
        monkeypatch.setattr(registry, 'read_session', lambda sid: {'id': sid, 'kind': 'assistant' if sid == 'assistant-1' else 'run'})
        hub = comms.CommsHub()
        monkeypatch.setattr(comms, '_HUB', hub)
        self.hub = hub

    def test_thread_reply_is_in_pane_without_broadcast(self, client, slack):
        from lmer_platform import transcripts
        response = client.post('/api/comms/thread', headers=bearer(cfg.mint_assistant_credential()),
                               json={'permalink': SLACK_LINK, 'text': 'A reply only for this conversation.'})
        assert response.status_code == 200, response.text
        assert len(slack.posted) == 1
        messages = transcripts.read_messages('assistant-1').messages
        assert [m.text for m in messages] == ['A reply only for this conversation.']
        assert messages[0].via == 'comms'
        assert messages[0].thread == SLACK_LINK
        assert self.hub.feed(0)[0] == []
        assert transcripts.read_messages('run-1').messages == ()

    def test_failed_or_operator_posts_do_not_become_assistant_replies(self, client, slack):
        from lmer_platform import transcripts
        refused = client.post('/api/comms/thread', headers=bearer(cfg.mint_assistant_credential()),
                              json={'permalink': SLACK_LINK, 'text': ''})
        assert refused.status_code == 400
        assert client.post('/api/comms/thread', headers=auth(),
                           json={'permalink': SLACK_LINK, 'text': 'Operator post'}).status_code == 200
        assert transcripts.read_messages('assistant-1').messages == ()

    def test_required_digests_survive_spool_drain_without_routine_traffic(self, monkeypatch):
        from lmer_platform import assistant, transcripts
        from lmer_platform.detect import Detector, Signal
        detector = Detector(cfg.load())
        monkeypatch.setattr(assistant, '_live_assistant', lambda: None)
        for kind in ['question', 'live_question', 'crashed', 'complete', 'checkin', 'event']:
            detector._deliver(Signal('example.org', 'example/project', 'run-1', kind, note='Needs attention'))
        assert len(assistant.read_state().pending) == 6
        assistant.take_pending()
        messages = transcripts.read_messages('assistant-1').messages
        assert len(messages) == 4
        assert all(m.via == 'comms' and m.role == 'platform' for m in messages)
        assert self.hub.feed(0)[0] == []
        assert transcripts.read_messages('run-1').messages == ()

    @pytest.mark.parametrize('transport, private', [
        ('slack', True), ('matrix', True), ('matrix', False), ('matrix', 'unknown'),
    ])
    def test_thread_privacy_is_preserved_in_the_pane(self, client, monkeypatch, tmp_path, transport, private):
        from lmer_cli import dm_parents
        from lmer_cli.threads import parse_thread
        from lmer_cli.routing_receipts import KEY
        from lmer_platform import transcripts
        from tests.test_lmer_thread_access import FakeMatrix, MATRIX_LINK
        monkeypatch.setattr(dm_parents, 'REGISTRY_DIR', str(tmp_path / 'dm-parents'))
        link = SLACK_LINK.replace('/C0123ABCDEF/', '/D0123ABCDEF/') if transport == 'slack' else MATRIX_LINK
        provider = FakeSlack() if transport == 'slack' else FakeMatrix()
        if transport == 'matrix':
            def is_direct_room(room):
                if private == 'unknown':
                    raise RuntimeError('privacy lookup failed')
                return private
            provider.is_direct_room = is_direct_room
        monkeypatch.setattr(thread_access, 'resolve_thread', lambda *args, **kw: (parse_thread(link), provider, []))
        text = 'Private conversation reply content'
        posted = client.post('/api/comms/thread', headers=bearer(cfg.mint_assistant_credential()),
                             json={'permalink': link, 'text': text})
        assert posted.status_code == 200, posted.text
        assert len(provider.posted if transport == 'slack' else provider.sent) == 1
        assert posted.json()[KEY]['id'] == posted.json()['posted']['ref']
        messages = transcripts.read_messages('assistant-1').messages
        assert len(messages) == 1
        if private:
            assert text not in messages[0].text
            assert 'body not recorded' in messages[0].text
            journal = (store.platform_dir() / 'comms' / 'assistant-conversation.jsonl').read_text()
            assert text not in journal
        else:
            assert messages[0].text == text
        assert self.hub.feed(0)[0] == []

    def test_a_posted_reply_reports_a_failed_pane_record_without_resending(self, client, slack, monkeypatch):
        from lmer_platform import assistant_conversation
        from lmer_cli.routing_receipts import KEY
        def fail(*args, **kwargs):
            raise store.StoreError('write refused')
        monkeypatch.setattr(assistant_conversation, 'record', fail)
        result = client.post('/api/comms/thread', headers=bearer(cfg.mint_assistant_credential()),
                             json={'permalink': SLACK_LINK, 'text': 'sent once'})
        assert result.status_code == 200
        assert result.json()['pane_recorded'] is False
        assert result.json()[KEY]['id'] == result.json()['posted']['ref']
        assert len(slack.posted) == 1
