"""slack_chat — Slack thread fetch and parse utilities.

Foundational re-exports only.  Client and CLI imports are intentionally
excluded to avoid import cycles and keep this slice dependency-free.
"""

from .permalink import is_slack_thread_url, parse_slack_permalink

__all__ = [
    "parse_slack_permalink",
    "is_slack_thread_url",
]
