"""pi session usage.

Follows pi's own aggregation (``core/usage-totals.js``). Spend is carried by:
- assistant messages, under ``responseModel`` (falling back to ``model``);
- ``usage`` entries such as ``cache_warm``;
- tool results and ``compaction`` / ``branch_summary`` entries that carry a
  ``usage`` of their own; these have no model of their own and are reported
  under :data:`SUMMARY_MODEL`.

A pi session is a tree. Every branch counts, because tokens spent on an
abandoned branch were still spent. Entries are keyed by ``responseId`` when the
provider gave one and by the entry ``id`` otherwise. pi's own ``cost.total`` is
kept as the harness-reported cost and never mixed into lmer's estimate.
"""

from __future__ import annotations

from pathlib import Path

from . import buckets
from .timestamps import parse_timestamp

HARNESS = "pi"
SUMMARY_MODEL = "pi:tools-and-summaries"
#: pi prices from its model catalogue, in US dollars.
REPORTED_CURRENCY = "USD"


def _counters(usage: dict):
    return buckets.make(
        input=usage.get("input"),
        cache_create=usage.get("cacheWrite"),
        cache_read=usage.get("cacheRead"),
        output=usage.get("output"),
        thinking=usage.get("reasoning"),
    )


def _reported(usage: dict):
    cost = usage.get("cost")
    total = cost.get("total") if isinstance(cost, dict) else None
    if isinstance(total, bool) or not isinstance(total, (int, float)) or total < 0:
        return None
    return (REPORTED_CURRENCY, float(total))


class Adapter:
    harness = HARNESS

    def start_file(self, path: Path) -> dict:
        return {}

    def events(self, record: dict, state: dict) -> list:
        kind = record.get("type")
        model = None
        usage = None
        key = None
        if kind == "message":
            message = record.get("message")
            if not isinstance(message, dict):
                return []
            role = message.get("role")
            usage = message.get("usage")
            if role == "assistant":
                model = message.get("responseModel") or message.get("model")
                key = message.get("responseId")
            elif role == "toolResult":
                model = SUMMARY_MODEL
            else:
                return []
        elif kind == "usage":
            model = record.get("model")
            usage = record.get("usage")
        elif kind in ("compaction", "branch_summary"):
            model = SUMMARY_MODEL
            usage = record.get("usage")
        else:
            return []
        if not isinstance(usage, dict) or not isinstance(model, str) or not model:
            return []
        counters = _counters(usage)
        if counters is None or buckets.is_zero(counters):
            return []
        key = key if isinstance(key, str) and key else record.get("id")
        if not isinstance(key, str) or not key:
            return []
        return [buckets.Event(
            key=("pi", key), harness=HARNESS, model=model, agent=None,
            at=parse_timestamp(record.get("timestamp")), counters=counters,
            reported_cost=_reported(usage),
        )]
