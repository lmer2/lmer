"""Failing (TDD red-phase) tests for the slack_chat.permalink module.

These tests must fail until Task 2 (permalink.impl) lands in
src/slack_chat/permalink.py.

Coverage:
- parse_slack_permalink: canonical /archives/<CHANNEL>/p<TS> form
- parse_slack_permalink: ?thread_ts= query-string override form
- p<TS> -> dotted-ts conversion including the 6-fractional-digit split
- is_slack_thread_url: true on valid Slack thread permalinks
- is_slack_thread_url: false on non-Slack, non-archive, and plain repo URLs
- spec §6 half-match contract: host matches slack.com but archive path malformed
  -> ValueError from parse_slack_permalink; is_slack_thread_url returns False
"""

import pytest

from slack_chat.permalink import is_slack_thread_url, parse_slack_permalink

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CANONICAL = "https://myworkspace.slack.com/archives/C0123ABCDEF/p1700000000123456"
THREAD_QS = (
    "https://myworkspace.slack.com/archives/C0123ABCDEF"
    "/p1700000000000000?thread_ts=1700000000.123456&cid=C0123ABCDEF"
)


# ---------------------------------------------------------------------------
# parse_slack_permalink: canonical form
# ---------------------------------------------------------------------------


class TestParseSlackPermalinkCanonical:
    """parse_slack_permalink on the canonical /archives/<CHANNEL>/p<TS> form."""

    def test_returns_tuple(self):
        result = parse_slack_permalink(CANONICAL)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_channel_extracted(self):
        channel, _ = parse_slack_permalink(CANONICAL)
        assert channel == "C0123ABCDEF"

    def test_thread_ts_dotted(self):
        _, thread_ts = parse_slack_permalink(CANONICAL)
        assert thread_ts == "1700000000.123456"

    def test_fractional_part_is_6_digits(self):
        """The fractional part must be exactly the last 6 digits of p<TS>."""
        _, thread_ts = parse_slack_permalink(CANONICAL)
        integer_part, frac_part = thread_ts.split(".")
        assert len(frac_part) == 6

    def test_integer_part_correct(self):
        _, thread_ts = parse_slack_permalink(CANONICAL)
        integer_part, _ = thread_ts.split(".")
        assert integer_part == "1700000000"

    def test_different_channel_id(self):
        url = "https://acme.slack.com/archives/GABCDE12345/p1600000001654321"
        channel, thread_ts = parse_slack_permalink(url)
        assert channel == "GABCDE12345"
        assert thread_ts == "1600000001.654321"

    def test_workspace_subdomain_ignored(self):
        """The workspace subdomain does not leak into channel or thread_ts."""
        channel, thread_ts = parse_slack_permalink(CANONICAL)
        assert "myworkspace" not in channel
        assert "myworkspace" not in thread_ts


# ---------------------------------------------------------------------------
# parse_slack_permalink: ?thread_ts= query-string form
# ---------------------------------------------------------------------------


class TestParseSlackPermalinkQueryString:
    """parse_slack_permalink on the ?thread_ts= query-string form."""

    def test_thread_ts_from_query_string(self):
        """When ?thread_ts= is present it takes precedence over p<TS>."""
        _, thread_ts = parse_slack_permalink(THREAD_QS)
        assert thread_ts == "1700000000.123456"

    def test_channel_still_from_path(self):
        channel, _ = parse_slack_permalink(THREAD_QS)
        assert channel == "C0123ABCDEF"

    def test_thread_ts_already_dotted_passes_through(self):
        """A dotted ?thread_ts= value is returned as-is."""
        url = (
            "https://team.slack.com/archives/C999/p1234567890000000"
            "?thread_ts=1234567890.999999"
        )
        _, thread_ts = parse_slack_permalink(url)
        assert thread_ts == "1234567890.999999"


# ---------------------------------------------------------------------------
# p<TS> -> dotted-ts conversion
# ---------------------------------------------------------------------------


class TestPTsConversion:
    """Verify the 6-fractional-digit split for various p<TS> values."""

    @pytest.mark.parametrize(
        "url, expected_ts",
        [
            (
                "https://x.slack.com/archives/C1/p1700000000123456",
                "1700000000.123456",
            ),
            (
                "https://x.slack.com/archives/C1/p1234567890000000",
                "1234567890.000000",
            ),
            (
                "https://x.slack.com/archives/C1/p0000000001999999",
                "0000000001.999999",
            ),
            (
                "https://x.slack.com/archives/C1/p1609459200000000",
                "1609459200.000000",
            ),
        ],
    )
    def test_split_produces_correct_dotted_ts(self, url, expected_ts):
        _, thread_ts = parse_slack_permalink(url)
        assert thread_ts == expected_ts

    def test_fractional_always_6_digits(self):
        """Verify len(frac) == 6 for all parametrized cases."""
        urls = [
            "https://x.slack.com/archives/C1/p1700000000000001",
            "https://x.slack.com/archives/C1/p1700000000100000",
            "https://x.slack.com/archives/C1/p1700000000010000",
        ]
        for url in urls:
            _, ts = parse_slack_permalink(url)
            _, frac = ts.split(".")
            assert len(frac) == 6, f"Expected 6-digit frac for {url}, got {frac!r}"


# ---------------------------------------------------------------------------
# is_slack_thread_url: true on valid permalinks
# ---------------------------------------------------------------------------


class TestIsSlackThreadUrlTrue:
    """is_slack_thread_url should return True for recognizable Slack thread URLs."""

    def test_canonical_permalink(self):
        assert is_slack_thread_url(CANONICAL) is True

    def test_permalink_with_thread_qs(self):
        assert is_slack_thread_url(THREAD_QS) is True

    def test_different_workspace(self):
        url = "https://anotherteam.slack.com/archives/CABC123/p1700000001000000"
        assert is_slack_thread_url(url) is True

    def test_channel_starting_with_g(self):
        url = "https://ws.slack.com/archives/G0001ABCDE/p1700000000999999"
        assert is_slack_thread_url(url) is True

    def test_uppercase_channel(self):
        url = "https://ws.slack.com/archives/CABCDEFGHIJ/p1700000000123000"
        assert is_slack_thread_url(url) is True


# ---------------------------------------------------------------------------
# is_slack_thread_url: false on non-Slack, non-archive, repo URLs
# ---------------------------------------------------------------------------


class TestIsSlackThreadUrlFalse:
    """is_slack_thread_url must return False for non-Slack / non-archive strings."""

    def test_github_repo_url(self):
        assert is_slack_thread_url("https://github.com/owner/repo") is False

    def test_gitlab_repo_url(self):
        assert is_slack_thread_url("https://gitlab.com/group/project.git") is False

    def test_gitlab_mr_url(self):
        assert (
            is_slack_thread_url(
                "https://gitlab.example.com/group/project/-/merge_requests/42"
            )
            is False
        )

    def test_plain_string_not_url(self):
        assert is_slack_thread_url("not-a-url") is False

    def test_empty_string(self):
        assert is_slack_thread_url("") is False

    def test_none_value(self):
        assert is_slack_thread_url(None) is False

    def test_slack_base_url_no_archives(self):
        """slack.com host but no /archives/ path is NOT a thread URL."""
        assert is_slack_thread_url("https://myworkspace.slack.com/") is False

    def test_slack_non_archive_path(self):
        """slack.com host but a different path section (e.g. /messages/) is False."""
        assert (
            is_slack_thread_url(
                "https://myworkspace.slack.com/messages/C0123ABCDEF"
            )
            is False
        )

    def test_ssh_git_url(self):
        assert is_slack_thread_url("git@github.com:owner/repo.git") is False

    def test_slack_url_without_p_token(self):
        """Archives path present but no p<TS> token -> not a valid thread URL."""
        assert (
            is_slack_thread_url(
                "https://ws.slack.com/archives/C0123ABCDEF"
            )
            is False
        )


# ---------------------------------------------------------------------------
# Spec §6 half-match contract:
#   host matches *.slack.com AND path starts with /archives/ BUT is malformed
#   -> parse_slack_permalink raises ValueError
#   -> is_slack_thread_url returns False (not an error)
# ---------------------------------------------------------------------------


class TestHalfMatchContract:
    """Spec §6: half-matching permalinks raise ValueError from parse, False from is_."""

    def test_is_slack_thread_url_returns_false_for_half_match(self):
        """is_slack_thread_url must return False, not raise."""
        malformed = "https://ws.slack.com/archives/"
        result = is_slack_thread_url(malformed)
        assert result is False

    def test_is_slack_thread_url_returns_false_archives_no_channel(self):
        """Host matches, /archives/ present, but no channel segment -> False."""
        assert is_slack_thread_url("https://ws.slack.com/archives/") is False

    def test_is_slack_thread_url_returns_false_channel_no_ts(self):
        """Host matches, channel present, but no p<TS> segment -> False."""
        assert (
            is_slack_thread_url(
                "https://ws.slack.com/archives/C0123ABCDEF"
            )
            is False
        )

    def test_parse_raises_valueerror_for_malformed_archive_path(self):
        """slack.com host + /archives/ prefix but missing channel -> ValueError."""
        malformed = "https://ws.slack.com/archives/"
        with pytest.raises(ValueError):
            parse_slack_permalink(malformed)

    def test_parse_raises_valueerror_for_channel_only(self):
        """slack.com host + channel present but no p<TS> token -> ValueError."""
        malformed = "https://ws.slack.com/archives/C0123ABCDEF"
        with pytest.raises(ValueError):
            parse_slack_permalink(malformed)

    def test_parse_raises_valueerror_for_bad_ts_token(self):
        """p<TS> token shorter than 7 digits (not enough to split) -> ValueError."""
        malformed = "https://ws.slack.com/archives/C0123ABCDEF/p12345"
        with pytest.raises(ValueError):
            parse_slack_permalink(malformed)

    def test_parse_valueerror_message_is_informative(self):
        """The ValueError message should mention the URL or the problem."""
        malformed = "https://ws.slack.com/archives/"
        with pytest.raises(ValueError, match=r"(?i)(malformed|invalid|archive|channel|slack)"):
            parse_slack_permalink(malformed)

    def test_is_does_not_raise_for_non_slack_url(self):
        """is_slack_thread_url must never raise, even for weird inputs."""
        for val in [
            "ftp://ws.slack.com/archives/C1/p12345",
            "https://notslack.com/archives/C1/p1700000000123456",
            "garbage",
            "",
            None,
            123,
        ]:
            # Must not raise — just return False
            result = is_slack_thread_url(val)
            assert result is False
