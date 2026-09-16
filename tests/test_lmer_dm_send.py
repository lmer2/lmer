"""The host side of `lmer-signal dm` (issue #441).

Where the operator's allow list, the recipient's resolution and the actual
delivery meet. The order matters as much as the outcome: a message cannot be
unsent, and this is the message somebody chose not to say in public, so
everything refusable without a transport is refused before one is touched.
"""
from __future__ import annotations

import pytest

from lmer_cli import thread_access
from lmer_cli.dm_access import SLACK_ALLOW_LIST_ENV, MATRIX_ALLOW_LIST_ENV
from lmer_cli.threads import ThreadError
from slack_chat.client import SlackError


def env(allowed="", **extra):
    base = {
        SLACK_ALLOW_LIST_ENV: allowed,
        MATRIX_ALLOW_LIST_ENV: allowed,
        "SLACK_BOT_TOKEN": "xoxb-test",
        "LMER_MATRIX_HOMESERVER": "https://example.org",
        "LMER_MATRIX_USER_ID": "@lmer:example.org",
        "LMER_MATRIX_ACCESS_TOKEN": "token",
    }
    base.update(extra)
    return base


class FakeSlack:
    def __init__(self, *, scopes=("chat:write",), users=None, open_error=None):
        self.scopes = list(scopes) if scopes is not None else None
        self.users = users if users is not None else []
        self.open_error = open_error
        self.calls = []
        self.posted = []

    def workspace(self):
        self.calls.append("workspace")
        return {"host": "example.slack.com", "scopes": self.scopes}

    def find_user(self, name):
        self.calls.append(f"find_user:{name}")
        if isinstance(self.users, Exception):
            raise self.users
        return [item for item in self.users
                if name.lstrip("@").casefold() in (item["name"].casefold(),)]

    def open_dm(self, user_id):
        self.calls.append(f"open_dm:{user_id}")
        if self.open_error is not None:
            raise self.open_error
        return "D" + user_id

    def post_root(self, channel, text):
        self.calls.append(f"post_root:{channel}")
        self.posted.append((channel, text))
        return {"channel": channel, "ts": "1700000000.123456"}


class FakeMatrix:
    def __init__(self, *, existing=None, encrypted=False):
        self.existing = existing
        self.encrypted = encrypted
        self.calls = []
        self.sent = []

    def direct_room(self, user_id):
        self.calls.append(f"direct_room:{user_id}")
        return self.existing

    def create_direct_room(self, user_id):
        self.calls.append(f"create:{user_id}")
        return "!fresh:example.org"

    def room_is_encrypted(self, room):
        self.calls.append(f"encrypted:{room}")
        return self.encrypted

    def send_message(self, room, content):
        self.calls.append(f"send:{room}")
        self.sent.append((room, content))
        return "$event"


class TestTheListIsConsultedFirst:
    def test_no_list_refuses_before_any_transport_call(self):
        """An empty list costs no API call. Asserted on `calls`, not on
        `posted`: `conversations.open` mutates, and a run permitted to message
        nobody was still opening IMs and enumerating the directory through the
        404 and the ambiguity refusal (MR !315, review round 1)."""
        slack = FakeSlack()
        with pytest.raises(ThreadError) as caught:
            thread_access.send_dm("U0123ABCD", "the credential", env=env(""), client=slack)
        assert SLACK_ALLOW_LIST_ENV in str(caught.value)
        assert slack.calls == []

    def test_somebody_the_list_cannot_rescue_is_refused_before_the_im_is_opened(self):
        """A non-empty list still resolves first — comparing needs the id — but
        the mutating call waits until the answer could still be yes."""
        slack = FakeSlack()
        with pytest.raises(ThreadError):
            thread_access.send_dm("U0123ABCD", "hi", env=env("U9999ZZZZ"), client=slack)
        assert not any(call.startswith("open_dm") for call in slack.calls)

    def test_somebody_not_on_the_list_is_refused_and_nothing_is_posted(self):
        slack = FakeSlack()
        with pytest.raises(ThreadError) as caught:
            thread_access.send_dm("U0123ABCD", "hi", env=env("U9999ZZZZ"), client=slack)
        assert "U0123ABCD" in str(caught.value)
        assert slack.posted == []

    def test_empty_text_is_refused_before_the_list_is_even_read(self):
        slack = FakeSlack()
        with pytest.raises(ThreadError) as caught:
            thread_access.send_dm("U0123ABCD", "   ", env=env("U0123ABCD"), client=slack)
        assert "needs text" in str(caught.value)
        assert slack.calls == []


class TestSlackDelivery:
    def test_a_listed_user_id_receives_the_message(self):
        slack = FakeSlack()
        result = thread_access.send_dm(
            "U0123ABCD", "the credential", env=env("U0123ABCD"), client=slack)
        assert slack.posted == [("DU0123ABCD", "the credential")]
        conversation = result["conversation"]
        assert conversation["recipient"] == "U0123ABCD"
        assert conversation["id"] == "DU0123ABCD"
        assert conversation["permalink"].startswith("https://example.slack.com/archives/DU0123ABCD/")

    def test_a_username_resolves_when_the_app_carries_the_scope(self):
        slack = FakeSlack(scopes=("chat:write", "users:read"),
                          users=[{"id": "U0123ABCD", "name": "vegu"}])
        thread_access.send_dm("vegu", "hi", env=env("U0123ABCD"), client=slack)
        assert slack.posted == [("DU0123ABCD", "hi")]

    def test_a_username_is_refused_by_name_when_the_scope_is_known_absent(self):
        """Refused here rather than by a call that would fail anyway. The scope
        is named so an operator can see what it would take; they are not told to
        grant it."""
        slack = FakeSlack(scopes=("chat:write",))
        with pytest.raises(ThreadError) as caught:
            thread_access.send_dm("vegu", "hi", env=env("vegu"), client=slack)
        assert "users:read" in str(caught.value)
        assert not any(call.startswith("find_user") for call in slack.calls)

    def test_unreadable_scopes_let_slack_answer_for_itself(self):
        """A stripped header is not knowing rather than evidence of absence —
        the reading the membership refusal already takes — so the lookup is
        attempted and Slack's own missing_scope is the authority."""
        slack = FakeSlack(scopes=None, users=SlackError("nope", code="missing_scope"))
        with pytest.raises(ThreadError) as caught:
            thread_access.send_dm("vegu", "hi", env=env("vegu"), client=slack)
        assert "users:read" in str(caught.value)
        assert any(call.startswith("find_user") for call in slack.calls)

    def test_a_username_nobody_holds_is_refused(self):
        slack = FakeSlack(scopes=("users:read",), users=[])
        with pytest.raises(ThreadError) as caught:
            thread_access.send_dm("ghost", "hi", env=env("ghost"), client=slack)
        assert "no Slack user is called ghost" in str(caught.value)

    def test_a_username_two_people_answer_to_is_refused_naming_both(self):
        """The one case where guessing is worst: the wrong choice delivers a
        private message to a stranger, and nothing can take it back."""
        class Ambiguous(FakeSlack):
            def find_user(self, name):
                self.calls.append(f"find_user:{name}")
                return [{"id": "U1", "name": "sam"}, {"id": "U2", "name": "sam"}]

        slack = Ambiguous(scopes=("users:read",))
        with pytest.raises(ThreadError) as caught:
            thread_access.send_dm("sam", "hi", env=env("sam"), client=slack)
        assert "U1" in str(caught.value) and "U2" in str(caught.value)
        assert slack.posted == []

    def test_a_refusal_to_open_carries_slack_s_code(self):
        slack = FakeSlack(open_error=SlackError("no", code="cannot_dm_bot"))
        with pytest.raises(ThreadError) as caught:
            thread_access.send_dm("U0123ABCD", "hi", env=env("U0123ABCD"), client=slack)
        assert "cannot_dm_bot" in str(caught.value)

    def test_a_display_name_cannot_satisfy_an_entry_no_handle_holds(self):
        """A display name is self-set and not unique. Any member could set
        theirs to an operator's entry and collect somebody else's secret, so
        only what Slack confirms about the account — its id and its handle —
        is compared (MR !315, review round 1)."""
        class DisplayNameMatch(FakeSlack):
            """`find_user` matches profile.display_name as well as the handle,
            so a lookup for "stefan" can land on an account whose handle is
            nothing of the sort."""

            def find_user(self, name):
                self.calls.append(f"find_user:{name}")
                return [{"id": "UIMPOSTOR", "name": "impostor"}]

        slack = DisplayNameMatch(scopes=("users:read",))
        with pytest.raises(ThreadError) as caught:
            thread_access.send_dm("stefan", "hi", env=env("stefan"), client=slack)
        assert SLACK_ALLOW_LIST_ENV in str(caught.value)
        assert slack.posted == []
        # And the refusal calls them what the run typed, not the id it resolved.
        assert "stefan" in str(caught.value)

    def test_the_handle_slack_holds_still_satisfies_the_list(self):
        """The legitimate case the rule above must not break."""
        slack = FakeSlack(scopes=("users:read",),
                          users=[{"id": "U0123ABCD", "name": "vegu"}])
        thread_access.send_dm("vegu", "hi", env=env("vegu"), client=slack)
        assert slack.posted == [("DU0123ABCD", "hi")]

    def test_the_allow_list_matches_the_resolved_id_too(self):
        """An operator who listed the id has authorised the person, however the
        run spelled them."""
        slack = FakeSlack(scopes=("users:read",), users=[{"id": "U0123ABCD", "name": "vegu"}])
        thread_access.send_dm("vegu", "hi", env=env("U0123ABCD"), client=slack)
        assert slack.posted == [("DU0123ABCD", "hi")]

    @pytest.mark.parametrize("listed, admitted", [
        ("U0123ABCD", False), ("u0123abcd", True),
    ])
    def test_resolved_handle_cannot_satisfy_another_users_id_entry(self, listed, admitted):
        slack = FakeSlack(
            scopes=("chat:write", "users:read"),
            users=[{"id": "UOTHER999", "name": "u0123abcd"}],
        )

        if admitted:
            thread_access.send_dm("u0123abcd", "hi", env=env(listed), client=slack)
            assert slack.posted == [("DUOTHER999", "hi")]
        else:
            with pytest.raises(ThreadError, match=SLACK_ALLOW_LIST_ENV):
                thread_access.send_dm("u0123abcd", "hi", env=env(listed), client=slack)
            assert slack.posted == []
            assert not any(call.startswith("open_dm") for call in slack.calls)


class TestMatrixDelivery:
    def test_unset_matrix_list_refuses_before_transport(self):
        matrix = FakeMatrix()
        with pytest.raises(ThreadError, match="LMER_MATRIX_ALLOWED_USERS"):
            thread_access.send_dm(
                "@someone:example.org", "hi",
                env=env("@someone:example.org", LMER_MATRIX_ALLOWED_USERS=""), client=matrix,
            )
        assert matrix.calls == []

    def test_matrix_case_variant_is_not_authorised(self):
        matrix = FakeMatrix()
        with pytest.raises(ThreadError, match="LMER_MATRIX_ALLOWED_USERS"):
            thread_access.send_dm(
                "@SomeOne:example.org", "hi", env=env("@someone:example.org"), client=matrix,
            )
        assert matrix.sent == []

    def test_a_listed_user_gets_a_fresh_room_when_there_is_none(self):
        matrix = FakeMatrix(existing=None)
        result = thread_access.send_dm(
            "@someone:example.org", "hi", env=env("@someone:example.org"), client=matrix)
        assert matrix.sent == [("!fresh:example.org", {"msgtype": "m.text", "body": "hi"})]
        assert result["conversation"]["id"] == "!fresh:example.org"
        assert result["conversation"]["permalink"].startswith("https://matrix.to/#/")

    def test_an_existing_room_is_reused_rather_than_duplicated(self):
        matrix = FakeMatrix(existing="!dm:example.org")
        thread_access.send_dm(
            "@someone:example.org", "hi", env=env("@someone:example.org"), client=matrix)
        assert not any(call.startswith("create") for call in matrix.calls)
        assert matrix.sent[0][0] == "!dm:example.org"

    def test_an_encrypted_room_is_refused_before_anything_is_sent(self):
        """Delivery into a room this account holds no keys for looks like
        success and arrives unreadable."""
        matrix = FakeMatrix(existing="!dm:example.org", encrypted=True)
        with pytest.raises(ThreadError) as caught:
            thread_access.send_dm(
                "@someone:example.org", "hi", env=env("@someone:example.org"), client=matrix)
        assert "encrypted" in str(caught.value)
        assert matrix.sent == []

    def test_a_malformed_matrix_id_is_refused_before_a_room_is_made(self):
        matrix = FakeMatrix()
        with pytest.raises(ThreadError) as caught:
            thread_access.send_dm("@bad:", "hi", env=env("@bad:"), client=matrix)
        assert "matrix user id" in str(caught.value)
        assert matrix.calls == []


class TestOnlyTheHumansConnectionsExempt:
    """The exemption's provenance filter (MR !315, review round 1, critical).

    `thread open` takes any Slack channel string with no `:` or `/`, a bare user
    id included, and Slack resolves that to the user's IM. Without this filter a
    run could connect a DM with an unlisted person itself and then message them
    with the allow list never consulted.
    """

    def _threads(self, records):
        from lmer_platform import connectors, session_threads

        class Component:
            def records(self, session_id):
                return records

        original = connectors.ensure_connectors
        connectors.ensure_connectors = lambda: Component()
        try:
            return session_threads.SessionThreads("sess-1").conversation_ids()
        finally:
            connectors.ensure_connectors = original

    def test_a_conversation_the_run_connected_itself_is_not_established(self):
        assert self._threads([
            {"by": "agent", "key": {"channel": "DU0123ABCD", "thread_ts": "1.2"}},
        ]) == []

    def test_a_launch_target_and_an_operator_connect_are_established(self):
        assert self._threads([
            {"by": "operator", "key": {"channel": "DU0123ABCD", "thread_ts": "1.2"}},
            {"by": "someone@example.org", "key": {"room": "!dm:example.org", "event": "$e"}},
        ]) == ["DU0123ABCD", "!dm:example.org"]

    def test_the_runs_own_connections_are_dropped_from_a_mixed_list(self):
        assert self._threads([
            {"by": "agent", "key": {"channel": "DSNEAKY", "thread_ts": "1.2"}},
            {"by": "operator", "key": {"channel": "DALLOWED", "thread_ts": "1.2"}},
        ]) == ["DALLOWED"]


class TestTheEstablishedConversationExemption:
    def test_a_conversation_this_session_holds_needs_no_entry_on_the_list(self):
        """#441's own rule: replying inside a DM the human opened with this run
        is allowed regardless of the list, because the human chose that channel."""
        slack = FakeSlack()
        thread_access.send_dm(
            "U0123ABCD", "hi", env=env(""), client=slack, established=("DU0123ABCD",))
        assert slack.posted == [("DU0123ABCD", "hi")]

    def test_a_matrix_room_this_session_holds_needs_no_entry_either(self):
        matrix = FakeMatrix(existing="!dm:example.org")
        thread_access.send_dm(
            "@someone:example.org", "hi", env=env(""), client=matrix,
            established=("!dm:example.org",))
        assert matrix.sent[0][0] == "!dm:example.org"

    def test_a_conversation_this_session_does_not_hold_is_still_gated(self):
        slack = FakeSlack()
        with pytest.raises(ThreadError):
            thread_access.send_dm(
                "U0123ABCD", "hi", env=env(""), client=slack, established=("DSOMEONEELSE",))
        assert slack.posted == []
