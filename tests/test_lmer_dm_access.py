"""Who a run may direct-message (issue #441).

Each transport's host-side list governs both incoming and outgoing DMs. An
unset or empty list means *nobody* — no fallback to a public thread, no prompt,
a refusal that names the transport's variable.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lmer_cli.dm_access import (
    SLACK_ALLOW_LIST_ENV, MATRIX_ALLOW_LIST_ENV, allowed_recipients,
    check_recipient, recipient_channel, slack_user_id,
)
from lmer_cli.threads import CHANNEL_MATRIX, CHANNEL_SLACK, ThreadError


def env(value, channel=CHANNEL_SLACK):
    name = MATRIX_ALLOW_LIST_ENV if channel == CHANNEL_MATRIX else SLACK_ALLOW_LIST_ENV
    return {name: value}


class TestTheList:
    def test_removed_outbound_variable_is_read_nowhere(self):
        old_name = "LMER_DM_" + "ALLOWED_RECIPIENTS"
        source = Path(__file__).resolve().parents[1] / "src"
        assert not [
            path for path in source.rglob("*.py")
            if old_name in path.read_text(encoding="utf-8")
        ]

    def test_transport_lists_are_host_side_only(self):
        cli = Path(__file__).resolve().parents[1] / "src/lmer_cli/cli.py"
        source = cli.read_text(encoding="utf-8")
        for name in (SLACK_ALLOW_LIST_ENV, MATRIX_ALLOW_LIST_ENV):
            assert f'"{name}": None' in source

    @pytest.mark.parametrize("raw, expected", [
        ("U1,U2", ["U1", "U2"]),
        (" U1 , U2 ", ["U1", "U2"]),
        ("U1,,U2,", ["U1", "U2"]),
        ("", []),
        ("   ", []),
        (",,,", []),
    ])
    def test_the_list_is_read_as_the_operator_wrote_it(self, raw, expected):
        assert allowed_recipients("U1", env(raw)) == expected

    def test_an_unset_variable_is_an_empty_list(self):
        assert allowed_recipients("U1", {}) == []
        assert allowed_recipients("@a:example.org", {}) == []

    def test_matrix_reads_only_its_own_list(self):
        both = {SLACK_ALLOW_LIST_ENV: "U1", MATRIX_ALLOW_LIST_ENV: "@a:example.org"}
        assert allowed_recipients("U1", both) == ["U1"]
        assert allowed_recipients("@a:example.org", both) == ["@a:example.org"]


class TestWhichTransport:
    @pytest.mark.parametrize("recipient, channel", [
        ("U0123ABC", CHANNEL_SLACK),
        ("tester", CHANNEL_SLACK),
        ("@someone:example.org", CHANNEL_MATRIX),
        ("@someone:example.org:8448", CHANNEL_MATRIX),
    ])
    def test_the_server_half_tells_the_two_apart(self, recipient, channel):
        """The same reading `parse_channel` uses for a room: a matrix id carries
        a `:server` and a Slack id or handle cannot."""
        assert recipient_channel(recipient) == channel

    @pytest.mark.parametrize("value, is_id", [
        ("U0123ABCD", True), ("W0123ABCD", True),
        ("U0123ABC", False), ("u0123abcd", False),
        ("walter", False), ("user", False), ("@walter", False),
    ])
    def test_slack_id_shape_is_case_sensitive_and_nine_characters(self, value, is_id):
        assert slack_user_id(value) is is_id


class TestTheGate:
    def test_no_list_refuses_everyone_and_names_the_variable(self):
        for missing in ({}, env(""), env("   ")):
            with pytest.raises(ThreadError) as caught:
                check_recipient("U0123ABC", env=missing)
            assert SLACK_ALLOW_LIST_ENV in str(caught.value)
            assert caught.value.status == 403
            # The refusal has to close the door rather than point at another one:
            # the incident behind #441 was a secret posted to a public thread
            # because the private route was unavailable.
            assert "Nothing was sent" in str(caught.value)

    def test_somebody_not_on_the_list_is_refused_by_name(self):
        with pytest.raises(ThreadError) as caught:
            check_recipient("U0123ABC", env=env("U9999ZZZ"))
        assert "U0123ABC" in str(caught.value)
        assert SLACK_ALLOW_LIST_ENV in str(caught.value)
        assert caught.value.status == 403

    def test_somebody_on_the_list_is_allowed(self):
        check_recipient("U0123ABCD", env=env("U9999ZZZZ,U0123ABCD"))

    def test_each_transport_list_cannot_authorise_the_other(self):
        with pytest.raises(ThreadError, match="LMER_MATRIX_ALLOWED_USERS"):
            check_recipient("@a:example.org", env=env("@a:example.org"))
        with pytest.raises(ThreadError, match="LMER_SLACK_DM_ALLOWED_USERS"):
            check_recipient("U1", env=env("U1", CHANNEL_MATRIX))

    def test_matrix_unset_refuses_and_names_its_list(self):
        with pytest.raises(ThreadError, match="LMER_MATRIX_ALLOWED_USERS"):
            check_recipient("@a:example.org", env={})

    @pytest.mark.parametrize("listed, confirmed", [
        ("tester", "tester"), ("@TESTER", "tester"),
        ("walter", "walter"), ("user", "user"),
        ("u0123abcd", "u0123abcd"),
    ])
    def test_a_slack_handle_entry_matches_only_a_confirmed_handle(self, listed, confirmed):
        check_recipient("UOTHER999", spellings=(confirmed,), env=env(listed))
        with pytest.raises(ThreadError):
            check_recipient("UOTHER999", env=env(listed))

    @pytest.mark.parametrize("listed", ["u0123abcd", "@U0123ABCD", "U0123ABCDx"])
    def test_an_id_entry_needs_exact_case_and_shape(self, listed):
        with pytest.raises(ThreadError):
            check_recipient("U0123ABCD", env=env(listed))

    def test_an_id_entry_cannot_match_another_users_confirmed_handle(self):
        with pytest.raises(ThreadError):
            check_recipient(
                "UOTHER999", spellings=("u0123abcd",), env=env("U0123ABCD")
            )
        check_recipient("UOTHER999", spellings=("u0123abcd",), env=env("u0123abcd"))

    @pytest.mark.parametrize("listed, asked", [
        ("@someone:example.org", "@SomeOne:example.org"),
        ("@SomeOne:example.org", "@someone:example.org"),
    ])
    def test_a_matrix_entry_matches_exactly_and_nothing_else(self, listed, asked):
        """Two MXIDs differing only in case are two different people, so a
        case-insensitive match here could deliver to whoever registered the other
        spelling. Refusing is the safe direction and the one taken."""
        with pytest.raises(ThreadError):
            check_recipient(asked, env=env(listed, CHANNEL_MATRIX))

    def test_a_resolved_spelling_also_matches_the_list(self):
        """The transport confirmed both this ID and its handle."""
        check_recipient("U0123ABCD", spellings=("tester",), env=env("U0123ABCD"))
        check_recipient("U0123ABCD", spellings=("tester",), env=env("tester"))

    def test_a_resolved_spelling_does_not_invent_permission(self):
        with pytest.raises(ThreadError):
            check_recipient("U0123ABCD", spellings=("tester",), env=env("U9999ZZZZ"))
