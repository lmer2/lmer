"""codex rollout usage.

codex records ``event_msg`` / ``token_count`` snapshots whose
``info.total_token_usage`` is cumulative for the thread. Usage is the delta
between consecutive snapshots, attributed to the model of the latest
``turn_context``. Each delta is keyed by its (previous, current) cumulative
pair, so history that a fork or resume copies into another file of the same
session dedupes like claude's message ids.

- A snapshot that does not increase is ignored, so repeats are harmless.
- A snapshot lower than the last one starts a new segment, and its value is a
  baseline rather than a delta.
- A forked rollout (``session_meta`` names the thread it forked from) starts
  from the parent's cumulative total, so its first snapshot is a baseline too.

``cached_input_tokens`` and ``cache_write_input_tokens`` are treated as subsets
of ``input_tokens`` and ``reasoning_output_tokens`` as a subset of
``output_tokens`` (tests/fixtures/codex-usage/README.md).
"""

from __future__ import annotations

from pathlib import Path

from . import buckets
from .timestamps import parse_timestamp

HARNESS = "codex"
_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
           "output_tokens", "reasoning_output_tokens")
_FORK_KEYS = ("forked_from_id", "forkedFromId", "forked_from")


def _snapshot(record: dict):
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    total = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(total, dict):
        return None
    values = tuple(buckets.count(total.get(name)) for name in _FIELDS)
    return None if any(v is None for v in values) else values


def _counters(delta: tuple):
    inp, cached, cache_write, out, reasoning = delta
    return buckets.make(
        input=max(0, inp - cached - cache_write),
        cache_create=cache_write,
        cache_read=cached,
        output=out,
        thinking=reasoning,
    )


class Adapter:
    harness = HARNESS

    def start_file(self, path: Path) -> dict:
        return {"model": None, "previous": None, "forked": False}

    def events(self, record: dict, state: dict) -> list:
        kind = record.get("type")
        payload = record.get("payload")
        if kind == "session_meta" and isinstance(payload, dict):
            state["forked"] = any(payload.get(name) for name in _FORK_KEYS)
            return []
        if kind == "turn_context" and isinstance(payload, dict):
            model = payload.get("model")
            if isinstance(model, str) and model:
                state["model"] = model
            return []
        if kind != "event_msg":
            return []
        current = _snapshot(record)
        if current is None:
            return []
        previous = state["previous"]
        if previous is None:
            state["previous"] = current
            if state["forked"]:
                return []
            previous = (0,) * len(_FIELDS)
        else:
            if any(c < p for c, p in zip(current, previous)):
                state["previous"] = current
                return []
            if current == previous:
                return []
            state["previous"] = current
        counters = _counters(tuple(c - p for c, p in zip(current, previous)))
        if counters is None or buckets.is_zero(counters):
            return []
        return [buckets.Event(
            key=("codex", previous, current), harness=HARNESS,
            model=state["model"] or "unknown", agent=None,
            at=parse_timestamp(record.get("timestamp")), counters=counters,
        )]
