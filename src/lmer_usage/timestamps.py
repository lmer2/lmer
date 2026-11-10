"""Record timestamps as aware UTC datetimes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def parse_timestamp(value: object) -> Optional[datetime]:
    """An ISO-8601 string or epoch milliseconds as aware UTC, else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
