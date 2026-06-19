"""Slack permalink parsing utilities.

Public API
----------
parse_slack_permalink(url) -> (channel: str, thread_ts: str)
    Extract channel and thread_ts from a Slack thread permalink.
    Raises ValueError for a URL that matches the slack.com host and
    /archives/ path prefix but has a malformed structure (spec §6).

is_slack_thread_url(s) -> bool
    Return True iff *s* is a recognisable Slack thread permalink.
    Never raises; returns False for any non-matching or invalid input.
"""

from urllib.parse import parse_qs, urlparse

# A Slack message timestamp encoded in a path token looks like:
#   p<16-digit-integer>   e.g. p1700000000123456
# The last 6 digits are the fractional (microsecond) part.
_MIN_TS_DIGITS = 7  # need at least 1 integer digit + 6 fractional digits


def _pts_to_dotted(pts: str) -> str:
    """Convert a p<TS> path token to a dotted timestamp string.

    Parameters
    ----------
    pts:
        The raw token starting with 'p', e.g. 'p1700000000123456'.

    Returns
    -------
    str
        Dotted form, e.g. '1700000000.123456'.

    Raises
    ------
    ValueError
        If the token does not start with 'p' or has fewer than 7 digits.
    """
    if not pts.startswith("p"):
        raise ValueError(f"Expected p<TS> token, got: {pts!r}")
    digits = pts[1:]
    if len(digits) < _MIN_TS_DIGITS:
        raise ValueError(
            f"p<TS> token has too few digits to split into integer+fractional "
            f"parts (need >= {_MIN_TS_DIGITS}, got {len(digits)}): {pts!r}"
        )
    integer_part = digits[:-6]
    frac_part = digits[-6:]
    return f"{integer_part}.{frac_part}"


def _is_slack_host(parsed) -> bool:
    """Return True iff the parsed URL has a *.slack.com host over https."""
    return (
        parsed.scheme == "https"
        and parsed.netloc.endswith(".slack.com")
    )


def parse_slack_permalink(url: str):
    """Parse a Slack thread permalink and return (channel, thread_ts).

    Supports two forms:

    1. Canonical path form::

        https://<workspace>.slack.com/archives/<CHANNEL>/p<TS>

    2. Query-string form (used by some Slack clients)::

        https://<workspace>.slack.com/archives/<CHANNEL>/p<TS>?thread_ts=<ts>

    When ``?thread_ts=`` is present it takes precedence over the p<TS>
    conversion (spec §6 honours the query-string as the authoritative ts).

    Parameters
    ----------
    url:
        A Slack thread permalink string.

    Returns
    -------
    (channel, thread_ts):
        *channel* is the Slack channel ID (e.g. ``C0123ABCDEF``).
        *thread_ts* is the dotted timestamp (e.g. ``1700000000.123456``).

    Raises
    ------
    ValueError
        If the URL matches the slack.com host but has a malformed archive path
        (missing channel, missing p<TS> token, or a p<TS> token too short).
    """
    parsed = urlparse(url)

    if not _is_slack_host(parsed):
        raise ValueError(
            f"Not a Slack URL (expected https://*.slack.com host): {url!r}"
        )

    # Path should be /archives/<CHANNEL>/p<TS>
    path = parsed.path  # e.g. "/archives/C0123ABCDEF/p1700000000123456"
    segments = [s for s in path.split("/") if s]
    # segments[0] == "archives", segments[1] == channel, segments[2] == p<TS>

    if not segments or segments[0] != "archives":
        raise ValueError(
            f"Malformed Slack archive URL — expected path starting with "
            f"/archives/<CHANNEL>/p<TS>: {url!r}"
        )

    if len(segments) < 2 or not segments[1]:
        raise ValueError(
            f"Malformed Slack archive URL — missing channel segment in path: {url!r}"
        )

    channel = segments[1]

    # Check for ?thread_ts= query param first
    qs = parse_qs(parsed.query)
    if "thread_ts" in qs:
        thread_ts = qs["thread_ts"][0]
        # The value may already be in dotted form; return it as-is.
        return (channel, thread_ts)

    # Fall back to p<TS> path segment
    if len(segments) < 3:
        raise ValueError(
            f"Malformed Slack archive URL — missing p<TS> segment and no "
            f"?thread_ts= query parameter: {url!r}"
        )

    pts_token = segments[2]
    thread_ts = _pts_to_dotted(pts_token)
    return (channel, thread_ts)


def is_slack_thread_url(s) -> bool:
    """Return True iff *s* is a recognisable Slack thread permalink.

    This function never raises. Any input that is not a string, is empty,
    or does not match the full Slack thread permalink pattern returns False.

    The main CLI uses this to decide whether a positional target is a Slack
    thread (routed to the Slack integration) or a normal repo/MR target.

    Spec §6 contract
    ----------------
    * A URL with a *.slack.com host AND /archives/ path prefix BUT a
      malformed structure returns **False** here (and would raise ValueError
      in :func:`parse_slack_permalink`).
    * Non-Slack or non-archive URLs also return False without error.
    """
    if not isinstance(s, str) or not s:
        return False

    try:
        parsed = urlparse(s)
    except Exception:
        return False

    if not _is_slack_host(parsed):
        return False

    # Must have /archives/<CHANNEL>/p<TS> structure
    path = parsed.path
    segments = [seg for seg in path.split("/") if seg]

    if not segments or segments[0] != "archives":
        return False

    if len(segments) < 2 or not segments[1]:
        return False

    channel = segments[1]  # noqa: F841  (present for clarity)

    # Either a ?thread_ts= query param or a p<TS> path segment is required.
    qs = parse_qs(parsed.query)
    if "thread_ts" in qs:
        return True

    # Need a p<TS> token with at least 7 digits.
    if len(segments) < 3:
        return False

    pts_token = segments[2]
    if not pts_token.startswith("p"):
        return False

    digits = pts_token[1:]
    if len(digits) < _MIN_TS_DIGITS:
        return False

    return True
