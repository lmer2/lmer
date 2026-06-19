"""Failing pytest suite for SlackClient (TDD red phase).

Tests are designed to fail until src/slack_chat/client.py is implemented.
All HTTP is mocked via monkeypatch on requests.Session.request — no real
network calls are made.

Collection succeeds even before the module exists; each test fails with
ImportError (or assertion failure) until the implementation lands.
"""

import json
import os
import time
import pytest
from unittest.mock import MagicMock, call

# Deferred import — collected regardless; fails at runtime until impl lands.
try:
    from slack_chat.client import SlackClient, SlackError
    _IMPORT_OK = True
except ImportError:
    _IMPORT_OK = False
    SlackClient = None  # type: ignore[assignment,misc]
    SlackError = None   # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_impl():
    """Skip-with-fail if the module is not yet implemented."""
    if not _IMPORT_OK:
        pytest.fail(
            "slack_chat.client is not yet implemented — "
            "this is expected (TDD red phase)."
        )


BOT_TOKEN = "xoxb-test-token-12345"
SLACK_API_BASE = "https://slack.com/api"


def _make_response(payload: dict, status_code: int = 200, headers: dict = None):
    """Build a mock requests.Response from a JSON payload dict."""
    import requests as _req
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        http_err = _req.HTTPError(response=resp)
        resp.raise_for_status.side_effect = http_err
    return resp


# ---------------------------------------------------------------------------
# 1. Token read from SLACK_BOT_TOKEN env var
# ---------------------------------------------------------------------------

class TestTokenFromEnv:
    def test_token_read_from_env(self, monkeypatch):
        """SlackClient() without explicit token reads SLACK_BOT_TOKEN."""
        _require_impl()
        monkeypatch.setenv("SLACK_BOT_TOKEN", BOT_TOKEN)
        client = SlackClient()
        assert client.bot_token == BOT_TOKEN

    def test_explicit_token_takes_precedence(self, monkeypatch):
        """Explicit token kwarg wins over env var."""
        _require_impl()
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")
        client = SlackClient(bot_token="xoxb-explicit")
        assert client.bot_token == "xoxb-explicit"

    def test_no_token_raises(self, monkeypatch):
        """Missing token and no env var -> raises at construction time."""
        _require_impl()
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        with pytest.raises((SlackError, ValueError, TypeError)):
            SlackClient()


# ---------------------------------------------------------------------------
# 2. chat.postMessage payload shape and return value
# ---------------------------------------------------------------------------

class TestPostMessage:
    def test_post_message_payload_shape(self, monkeypatch):
        """post_message sends correct JSON body to chat.postMessage."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        posted_bodies = []

        def fake_request(method, url, **kwargs):
            assert method.upper() == "POST"
            assert "chat.postMessage" in url
            body = kwargs.get("json") or json.loads(kwargs.get("data", "{}"))
            posted_bodies.append(body)
            return _make_response({
                "ok": True,
                "ts": "1700000001.000100",
                "channel": "C12345",
            })

        monkeypatch.setattr(client.session, "request", fake_request)

        ts = client.post_message(
            channel="C12345",
            thread_ts="1700000000.123456",
            text="Hello, world!",
        )

        assert ts == "1700000001.000100"
        assert len(posted_bodies) == 1
        body = posted_bodies[0]
        assert body["channel"] == "C12345"
        assert body["thread_ts"] == "1700000000.123456"
        assert body["text"] == "Hello, world!"

    def test_post_message_returns_ts(self, monkeypatch):
        """post_message returns the ts field from the Slack response."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        monkeypatch.setattr(
            client.session,
            "request",
            lambda *a, **kw: _make_response({
                "ok": True,
                "ts": "1700000099.000001",
                "channel": "C99999",
            }),
        )

        ts = client.post_message("C99999", "1700000000.000001", "test")
        assert ts == "1700000099.000001"

    def test_post_message_missing_ts_raises_slack_error(self, monkeypatch):
        """An ok response with no 'ts' raises SlackError, not a bare KeyError.

        A Slack response with ``ok: true`` but a missing field would otherwise
        escape the CLI's ``except SlackError`` and die with a traceback.
        """
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        monkeypatch.setattr(
            client.session,
            "request",
            lambda *a, **kw: _make_response({"ok": True, "channel": "C99999"}),
        )

        with pytest.raises(SlackError):
            client.post_message("C99999", "1700000000.000001", "test")


# ---------------------------------------------------------------------------
# 3. conversations.replies pagination following next_cursor
# ---------------------------------------------------------------------------

class TestGetRepliesPagination:
    def test_single_page_no_cursor(self, monkeypatch):
        """get_replies returns all messages when there is only one page."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        messages_page1 = [
            {"type": "message", "ts": "1700000001.000001", "text": "msg1"},
            {"type": "message", "ts": "1700000001.000002", "text": "msg2"},
        ]

        monkeypatch.setattr(
            client.session,
            "request",
            lambda *a, **kw: _make_response({
                "ok": True,
                "messages": messages_page1,
                "response_metadata": {"next_cursor": ""},
            }),
        )

        result = client.get_replies("C12345", "1700000001.000001")
        assert result == messages_page1

    def test_pagination_follows_next_cursor(self, monkeypatch):
        """get_replies follows response_metadata.next_cursor across pages."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        page1_msgs = [
            {"type": "message", "ts": "1700000001.000001", "text": "msg1"},
        ]
        page2_msgs = [
            {"type": "message", "ts": "1700000001.000002", "text": "msg2"},
            {"type": "message", "ts": "1700000001.000003", "text": "msg3"},
        ]

        call_params = []

        def fake_request(method, url, **kwargs):
            params = kwargs.get("params", {})
            call_params.append(dict(params) if params else {})
            cursor = (params or {}).get("cursor", "")
            if not cursor:
                return _make_response({
                    "ok": True,
                    "messages": page1_msgs,
                    "response_metadata": {"next_cursor": "cursor_abc123"},
                })
            else:
                return _make_response({
                    "ok": True,
                    "messages": page2_msgs,
                    "response_metadata": {"next_cursor": ""},
                })

        monkeypatch.setattr(client.session, "request", fake_request)

        result = client.get_replies("C12345", "1700000001.000001")

        # Flat list of all messages across pages
        assert result == page1_msgs + page2_msgs

        # Exactly two requests made
        assert len(call_params) == 2
        # Second call carries the cursor from page 1
        assert call_params[1].get("cursor") == "cursor_abc123"

    def test_pagination_three_pages(self, monkeypatch):
        """get_replies follows cursor across three pages, returning flat list."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        pages = [
            {"ok": True, "messages": [{"ts": "1.1", "text": "a"}],
             "response_metadata": {"next_cursor": "cur1"}},
            {"ok": True, "messages": [{"ts": "1.2", "text": "b"}],
             "response_metadata": {"next_cursor": "cur2"}},
            {"ok": True, "messages": [{"ts": "1.3", "text": "c"}],
             "response_metadata": {"next_cursor": ""}},
        ]
        page_iter = iter(pages)

        monkeypatch.setattr(
            client.session,
            "request",
            lambda *a, **kw: _make_response(next(page_iter)),
        )

        result = client.get_replies("C12345", "1700000001.000001")
        assert len(result) == 3
        assert [m["text"] for m in result] == ["a", "b", "c"]

    def test_get_replies_passes_channel_and_thread_ts(self, monkeypatch):
        """get_replies passes channel and ts params to conversations.replies."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        captured_params = {}

        def fake_request(method, url, **kwargs):
            assert "conversations.replies" in url
            params = kwargs.get("params") or {}
            captured_params.update(params)
            return _make_response({
                "ok": True,
                "messages": [],
                "response_metadata": {"next_cursor": ""},
            })

        monkeypatch.setattr(client.session, "request", fake_request)

        client.get_replies("CABC123", "1700000000.123456")

        assert captured_params.get("channel") == "CABC123"
        assert captured_params.get("ts") == "1700000000.123456"


# ---------------------------------------------------------------------------
# 4. auth.test returning bot_user_id
# ---------------------------------------------------------------------------

class TestAuthTest:
    # Fixtures deliberately mirror the REAL auth.test payload shape:
    # ``user_id`` + ``bot_id`` and NO ``bot_user_id`` key — Slack never sends
    # one, and a fabricated key here previously masked a production KeyError.
    def test_auth_test_returns_user_id(self, monkeypatch):
        """auth_test() calls auth.test and returns the user_id field."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        monkeypatch.setattr(
            client.session,
            "request",
            lambda *a, **kw: _make_response({
                "ok": True,
                "url": "https://myworkspace.slack.com/",
                "team": "My Workspace",
                "user": "lmer",
                "team_id": "T12345",
                "user_id": "U12345",
                "bot_id": "B12345",
            }),
        )

        bot_user_id = client.auth_test()
        assert bot_user_id == "U12345"

    def test_auth_test_calls_correct_endpoint(self, monkeypatch):
        """auth_test() hits the auth.test endpoint."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)
        called_urls = []

        def fake_request(method, url, **kwargs):
            called_urls.append(url)
            return _make_response({
                "ok": True,
                "user_id": "U99999",
                "bot_id": "B99999",
            })

        monkeypatch.setattr(client.session, "request", fake_request)
        client.auth_test()

        assert any("auth.test" in u for u in called_urls)

    def test_auth_test_missing_user_id_raises_slack_error(self, monkeypatch):
        """An ok response with no 'user_id' raises SlackError, not KeyError."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        monkeypatch.setattr(
            client.session,
            "request",
            lambda *a, **kw: _make_response({"ok": True, "team": "My Workspace"}),
        )

        with pytest.raises(SlackError):
            client.auth_test()


# ---------------------------------------------------------------------------
# 5. {ok: false} response -> typed SlackError
# ---------------------------------------------------------------------------

class TestSlackErrorOnFalseOk:
    def test_ok_false_raises_slack_error(self, monkeypatch):
        """Any {ok: false} response raises SlackError."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        monkeypatch.setattr(
            client.session,
            "request",
            lambda *a, **kw: _make_response({
                "ok": False,
                "error": "invalid_auth",
            }),
        )

        with pytest.raises(SlackError):
            client.post_message("C12345", "1700000000.123456", "test")

    def test_slack_error_message_contains_error_code(self, monkeypatch):
        """SlackError message includes the Slack error code from response."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        monkeypatch.setattr(
            client.session,
            "request",
            lambda *a, **kw: _make_response({
                "ok": False,
                "error": "not_in_channel",
            }),
        )

        with pytest.raises(SlackError) as exc_info:
            client.post_message("C12345", "1700000000.123456", "test")

        assert "not_in_channel" in str(exc_info.value)

    def test_ok_false_in_get_replies_raises_slack_error(self, monkeypatch):
        """ok=false in conversations.replies also raises SlackError."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        monkeypatch.setattr(
            client.session,
            "request",
            lambda *a, **kw: _make_response({
                "ok": False,
                "error": "thread_not_found",
            }),
        )

        with pytest.raises(SlackError):
            client.get_replies("C12345", "1700000000.123456")

    def test_ok_false_in_auth_test_raises_slack_error(self, monkeypatch):
        """ok=false in auth.test also raises SlackError."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        monkeypatch.setattr(
            client.session,
            "request",
            lambda *a, **kw: _make_response({
                "ok": False,
                "error": "invalid_auth",
            }),
        )

        with pytest.raises(SlackError):
            client.auth_test()


# ---------------------------------------------------------------------------
# 6. HTTP 429 with Retry-After -> bounded retry then success / exhausted
# ---------------------------------------------------------------------------

class TestHttp429Retry:
    def test_429_retries_and_succeeds(self, monkeypatch):
        """HTTP 429 with Retry-After triggers retry; success on next attempt."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        import requests as req_lib

        call_count = {"n": 0}

        def fake_request(method, url, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                resp = MagicMock()
                resp.status_code = 429
                resp.headers = {"Retry-After": "1"}
                resp.json.return_value = {"ok": False, "error": "ratelimited"}
                http_err = req_lib.HTTPError(response=resp)
                resp.raise_for_status.side_effect = http_err
                return resp
            return _make_response({
                "ok": True,
                "ts": "1700000001.000200",
                "channel": "C12345",
            })

        monkeypatch.setattr(client.session, "request", fake_request)
        monkeypatch.setattr("time.sleep", lambda s: None)

        ts = client.post_message("C12345", "1700000000.123456", "retry test")
        assert ts == "1700000001.000200"
        assert call_count["n"] == 2

    def test_429_exhausted_retries_raises_slack_error(self, monkeypatch):
        """HTTP 429 that persists beyond max retries raises SlackError."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        import requests as req_lib

        def always_429(method, url, **kwargs):
            resp = MagicMock()
            resp.status_code = 429
            resp.headers = {"Retry-After": "1"}
            resp.json.return_value = {"ok": False, "error": "ratelimited"}
            http_err = req_lib.HTTPError(response=resp)
            resp.raise_for_status.side_effect = http_err
            return resp

        monkeypatch.setattr(client.session, "request", always_429)
        monkeypatch.setattr("time.sleep", lambda s: None)

        with pytest.raises(SlackError):
            client.post_message("C12345", "1700000000.123456", "exhausted retries")

    def test_429_retry_after_header_respected(self, monkeypatch):
        """Retry-After value from header is passed to sleep()."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        import requests as req_lib

        sleep_calls = []
        call_count = {"n": 0}

        def fake_request(method, url, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                resp = MagicMock()
                resp.status_code = 429
                resp.headers = {"Retry-After": "30"}
                resp.json.return_value = {"ok": False, "error": "ratelimited"}
                http_err = req_lib.HTTPError(response=resp)
                resp.raise_for_status.side_effect = http_err
                return resp
            return _make_response({
                "ok": True,
                "ts": "1700000001.000300",
                "channel": "C12345",
            })

        monkeypatch.setattr(client.session, "request", fake_request)
        monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

        client.post_message("C12345", "1700000000.123456", "retry header test")

        assert len(sleep_calls) >= 1
        assert any(s >= 30 for s in sleep_calls)

    def test_429_non_integer_retry_after_falls_back(self, monkeypatch):
        """An HTTP-date (non-integer) Retry-After must not raise ValueError —
        the retry proceeds with the fallback delay instead."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        import requests as req_lib

        sleep_calls = []
        call_count = {"n": 0}

        def fake_request(method, url, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                resp = MagicMock()
                resp.status_code = 429
                resp.headers = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
                resp.json.return_value = {"ok": False, "error": "ratelimited"}
                http_err = req_lib.HTTPError(response=resp)
                resp.raise_for_status.side_effect = http_err
                return resp
            return _make_response({
                "ok": True,
                "ts": "1700000001.000400",
                "channel": "C12345",
            })

        monkeypatch.setattr(client.session, "request", fake_request)
        monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

        ts = client.post_message("C12345", "1700000000.123456", "date header test")
        assert ts == "1700000001.000400"
        assert call_count["n"] == 2
        assert sleep_calls == [1]


# ---------------------------------------------------------------------------
# 7. Bearer auth header is set from bot_token
# ---------------------------------------------------------------------------

class TestAuthHeader:
    def test_bearer_auth_header_sent(self, monkeypatch):
        """Requests include Authorization: Bearer <token> header."""
        _require_impl()
        client = SlackClient(bot_token=BOT_TOKEN)

        auth_header = client.session.headers.get("Authorization", "")
        assert auth_header == f"Bearer {BOT_TOKEN}"
