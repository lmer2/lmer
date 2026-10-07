"""Opening a 1:1 conversation on each transport (issue #441).

The transport half of `lmer-signal dm`: Slack's `conversations.open` and the
username lookup behind it, and matrix's find-the-DM-or-make-one. All HTTP is
faked; nothing here reaches a network.
"""
from __future__ import annotations

import pytest

from matrix_chat.client import MatrixAccount, MatrixClient, MatrixError
from slack_chat.client import SlackClient, SlackError


# --- Slack -----------------------------------------------------------------


class FakeSlackSession:
    """Answers Slack Web API calls from a scripted table."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append((endpoint, kwargs.get("json") or kwargs.get("params") or {}))
        payload = self.replies[endpoint]
        if callable(payload):
            payload = payload(len(self.calls))

        class Response:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                return None

            def json(self):
                return payload

        return Response()


def slack_client(replies):
    client = SlackClient(bot_token="xoxb-test")
    client.session = FakeSlackSession(replies)
    return client


class TestSlackOpensADirectMessage:
    def test_a_user_id_opens_the_conversation_and_names_it(self):
        client = slack_client({"conversations.open": {"ok": True, "channel": {"id": "D0123"}}})
        assert client.open_dm("U0123ABC") == "D0123"
        endpoint, body = client.session.calls[0]
        assert endpoint == "conversations.open"
        assert body == {"users": "U0123ABC"}

    def test_a_refusal_carries_slack_s_own_code(self):
        """`cannot_dm_bot` and `user_not_found` are different problems for the
        operator, so the code survives rather than becoming one flat failure."""
        client = slack_client({"conversations.open": {"ok": False, "error": "cannot_dm_bot"}})
        with pytest.raises(SlackError) as caught:
            client.open_dm("U0123ABC")
        assert caught.value.code == "cannot_dm_bot"

    def test_a_response_without_a_channel_is_refused(self):
        client = slack_client({"conversations.open": {"ok": True, "channel": {}}})
        with pytest.raises(SlackError):
            client.open_dm("U0123ABC")


class TestSlackResolvesAUsername:
    def _members(self, members, cursor=""):
        return {"ok": True, "members": members, "response_metadata": {"next_cursor": cursor}}

    def test_a_handle_resolves_to_one_user(self):
        client = slack_client({"users.list": self._members([
            {"id": "U1", "name": "tester", "profile": {"display_name": "Example"}},
            {"id": "U2", "name": "someone", "profile": {"display_name": "Someone"}},
        ])})
        assert client.find_user("tester") == [{"id": "U1", "name": "tester"}]

    def test_a_display_name_resolves_too_and_ignores_case_and_at(self):
        client = slack_client({"users.list": self._members([
            {"id": "U1", "name": "tester", "profile": {"display_name": "Example"}},
        ])})
        assert client.find_user("@EXAMPLE") == [{"id": "U1", "name": "tester"}]

    def test_two_people_with_one_name_both_come_back(self):
        """The caller has to be able to refuse ambiguity; it cannot if the
        lookup picks one."""
        client = slack_client({"users.list": self._members([
            {"id": "U1", "name": "sam", "profile": {}},
            {"id": "U2", "name": "other", "profile": {"display_name": "sam"}},
        ])})
        assert [item["id"] for item in client.find_user("sam")] == ["U1", "U2"]

    def test_nobody_is_an_empty_list_not_an_error(self):
        client = slack_client({"users.list": self._members([{"id": "U1", "name": "tester", "profile": {}}])})
        assert client.find_user("nobody") == []

    def test_a_deactivated_account_is_not_a_match(self):
        client = slack_client({"users.list": self._members([
            {"id": "U1", "name": "tester", "profile": {}, "deleted": True},
        ])})
        assert client.find_user("tester") == []

    def test_every_page_is_read(self):
        pages = [
            self._members([{"id": "U1", "name": "first", "profile": {}}], cursor="next"),
            self._members([{"id": "U2", "name": "tester", "profile": {}}]),
        ]
        client = slack_client({"users.list": lambda call: pages[call - 1]})
        assert client.find_user("tester") == [{"id": "U2", "name": "tester"}]

    def test_a_missing_scope_is_slack_s_own_error(self):
        client = slack_client({"users.list": {"ok": False, "error": "missing_scope"}})
        with pytest.raises(SlackError) as caught:
            client.find_user("tester")
        assert caught.value.code == "missing_scope"


# --- matrix ----------------------------------------------------------------


class FakeMatrixSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, **kwargs):
        path = url.split("/_matrix", 1)[1]
        self.calls.append((method, "/_matrix" + path, kwargs.get("json")))
        status, payload = self.routes(method, "/_matrix" + path)

        class Response:
            status_code = status
            content = b"{}"

            def json(self):
                return payload

            @property
            def text(self):
                return ""

        return Response()


def matrix_client(routes):
    account = MatrixAccount(
        homeserver="https://example.org", user_id="@lmer:example.org", access_token="t")
    return MatrixClient(account, session=FakeMatrixSession(routes))


class TestMatrixFindsOrMakesADirectRoom:
    def test_an_existing_dm_is_reused(self):
        def routes(method, path):
            if "account_data/m.direct" in path:
                return 200, {"@someone:example.org": ["!dm:example.org"]}
            if path.endswith("/joined_rooms"):
                return 200, {"joined_rooms": ["!dm:example.org", "!other:example.org"]}
            if "m.room.member" in path:
                return 200, {"membership": "join"}
            raise AssertionError(path)

        client = matrix_client(routes)
        assert client.direct_room("@someone:example.org") == "!dm:example.org"

    @pytest.mark.parametrize("membership, readable", [
        ("leave", True), ("ban", True), (None, False),
    ])
    def test_a_room_the_recipient_is_no_longer_in_is_not_reused(self, membership, readable):
        """m.direct saying the bot is joined is not evidence the recipient is:
        a direct room may carry other members and the entry outlives somebody
        leaving, so sending would deliver to whoever remains and report it as
        delivery to the person named (MR !315, review round 1)."""
        def routes(method, path):
            if "account_data/m.direct" in path:
                return 200, {"@someone:example.org": ["!dm:example.org"]}
            if path.endswith("/joined_rooms"):
                return 200, {"joined_rooms": ["!dm:example.org"]}
            if "m.room.member" in path:
                return (200, {"membership": membership}) if readable else (403, {"errcode": "M_FORBIDDEN"})
            raise AssertionError(path)

        assert matrix_client(routes).direct_room("@someone:example.org") is None

    def test_an_invited_recipient_counts_as_present(self):
        """They have not accepted yet, but the room is theirs and the message
        appears when they do — making a second one would not help."""
        def routes(method, path):
            if "account_data/m.direct" in path:
                return 200, {"@someone:example.org": ["!dm:example.org"]}
            if path.endswith("/joined_rooms"):
                return 200, {"joined_rooms": ["!dm:example.org"]}
            if "m.room.member" in path:
                return 200, {"membership": "invite"}
            raise AssertionError(path)

        assert matrix_client(routes).direct_room("@someone:example.org") == "!dm:example.org"

    def test_a_room_this_account_has_left_does_not_count(self):
        """The id would still address a room; nothing sent there would arrive."""
        def routes(method, path):
            if "account_data/m.direct" in path:
                return 200, {"@someone:example.org": ["!gone:example.org"]}
            if path.endswith("/joined_rooms"):
                return 200, {"joined_rooms": ["!other:example.org"]}
            raise AssertionError(path)

        assert matrix_client(routes).direct_room("@someone:example.org") is None

    def test_no_m_direct_at_all_is_no_room_not_a_failure(self):
        def routes(method, path):
            if "account_data/m.direct" in path:
                return 404, {"errcode": "M_NOT_FOUND"}
            raise AssertionError(path)

        assert matrix_client(routes).direct_room("@someone:example.org") is None

    def test_somebody_with_no_entry_is_no_room(self):
        def routes(method, path):
            if "account_data/m.direct" in path:
                return 200, {"@other:example.org": ["!dm:example.org"]}
            raise AssertionError(path)

        assert matrix_client(routes).direct_room("@someone:example.org") is None

    def test_a_new_room_is_created_as_a_direct_chat(self):
        seen = {}

        def routes(method, path):
            if path.endswith("/createRoom"):
                return 200, {"room_id": "!fresh:example.org"}
            if "account_data/m.direct" in path:
                return (200, {}) if method == "PUT" else (404, {"errcode": "M_NOT_FOUND"})
            raise AssertionError(path)

        client = matrix_client(routes)
        assert client.create_direct_room("@someone:example.org") == "!fresh:example.org"
        _method, _path, body = client._session.calls[0]
        seen.update(body)
        assert seen["is_direct"] is True
        assert seen["invite"] == ["@someone:example.org"]
        assert seen["preset"] == "trusted_private_chat"

    def test_a_create_that_names_no_room_is_refused(self):
        def routes(method, path):
            return 200, {}

        with pytest.raises(MatrixError):
            matrix_client(routes).create_direct_room("@someone:example.org")

    def test_a_created_room_is_filed_in_m_direct(self):
        """The spec puts this on the inviting client, not the homeserver, which
        is why a DM the person opens is findable and one the bot opens is not
        unless it writes the entry itself."""
        written = {}

        def routes(method, path):
            if path.endswith("/createRoom"):
                return 200, {"room_id": "!fresh:example.org"}
            if "account_data/m.direct" in path:
                if method == "PUT":
                    return 200, {}
                return 404, {"errcode": "M_NOT_FOUND"}
            raise AssertionError(path)

        client = matrix_client(routes)
        client.create_direct_room("@someone:example.org")
        puts = [call for call in client._session.calls if call[0] == "PUT"]
        assert len(puts) == 1
        written.update(puts[0][2])
        assert written == {"@someone:example.org": ["!fresh:example.org"]}

    def test_an_existing_m_direct_is_extended_not_replaced(self):
        def routes(method, path):
            if path.endswith("/createRoom"):
                return 200, {"room_id": "!fresh:example.org"}
            if "account_data/m.direct" in path:
                if method == "PUT":
                    return 200, {}
                return 200, {"@other:example.org": ["!kept:example.org"]}
            raise AssertionError(path)

        client = matrix_client(routes)
        client.create_direct_room("@someone:example.org")
        body = [call for call in client._session.calls if call[0] == "PUT"][0][2]
        assert body["@other:example.org"] == ["!kept:example.org"]
        assert body["@someone:example.org"] == ["!fresh:example.org"]

    def test_a_failed_m_direct_write_does_not_cost_the_room(self):
        """Reuse is the casualty, not the message: the room exists and the
        caller is about to write to it."""
        def routes(method, path):
            if path.endswith("/createRoom"):
                return 200, {"room_id": "!fresh:example.org"}
            if "account_data/m.direct" in path:
                return (500, {}) if method == "PUT" else (404, {"errcode": "M_NOT_FOUND"})
            raise AssertionError(path)

        assert matrix_client(routes).create_direct_room("@someone:example.org") == "!fresh:example.org"

    def test_two_consecutive_sends_reuse_one_room(self):
        """The case the previous round could not have caught, because the test
        pre-supplied the room: create, file, then find it again."""
        state = {"direct": None, "rooms": []}

        def routes(method, path):
            if path.endswith("/createRoom"):
                state["rooms"].append("!fresh:example.org")
                return 200, {"room_id": "!fresh:example.org"}
            if "account_data/m.direct" in path:
                if method == "PUT":
                    return 200, {}
                if state["direct"] is None:
                    return 404, {"errcode": "M_NOT_FOUND"}
                return 200, state["direct"]
            if path.endswith("/joined_rooms"):
                return 200, {"joined_rooms": list(state["rooms"])}
            if "m.room.member" in path:
                return 200, {"membership": "join"}
            raise AssertionError(path)

        client = matrix_client(routes)
        assert client.direct_room("@someone:example.org") is None
        first = client.create_direct_room("@someone:example.org")
        # what the PUT stored is what the next lookup reads
        state["direct"] = [call for call in client._session.calls if call[0] == "PUT"][0][2]
        assert client.direct_room("@someone:example.org") == first
        assert len(state["rooms"]) == 1
