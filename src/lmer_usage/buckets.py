"""The normalised usage counters every adapter emits.

Four disjoint token buckets (``input``, ``cache_create``, ``cache_read``,
``output``) plus ``thinking``, which is a subset of ``output`` and never added to
it, and ``requests``, the number of deduplicated API responses. There is no
single "total tokens": the buckets are reported as they are.
"""

from __future__ import annotations

from typing import Mapping, Optional

TOKEN_BUCKETS = ("input", "cache_create", "cache_read", "output")
BUCKETS = TOKEN_BUCKETS + ("thinking",)
COUNTERS = ("requests",) + BUCKETS

#: Every count is a JSON-safe integer: non-negative and below 2^53.
MAX_COUNT = 2**53 - 1


def count(value: object) -> Optional[int]:
    """``value`` as a valid count, ``0`` for absent, ``None`` when invalid.

    ``bool`` is an ``int`` subclass and is refused, as is a float, a negative
    number or anything at or above 2^53.
    """
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_COUNT:
        return None
    return value


def zero() -> dict:
    return {name: 0 for name in COUNTERS}


def add(into: dict, other: Mapping) -> dict:
    """Add ``other``'s counters into ``into``, saturating at :data:`MAX_COUNT`."""
    for name in COUNTERS:
        into[name] = min(MAX_COUNT, into.get(name, 0) + int(other.get(name, 0)))
    return into


def is_zero(counters: Mapping) -> bool:
    return not any(counters.get(name, 0) for name in TOKEN_BUCKETS)


def valid(counters: object) -> bool:
    """Whether ``counters`` is a mapping of valid counts, thinking within output."""
    if not isinstance(counters, Mapping):
        return False
    for name in COUNTERS:
        # A stored null is not an absent count: a record states every counter.
        if name in counters and (counters[name] is None or count(counters[name]) is None):
            return False
    return (count(counters.get("thinking")) or 0) <= (count(counters.get("output")) or 0)


def make(*, input: object = 0, cache_create: object = 0, cache_read: object = 0,
         output: object = 0, thinking: object = 0, requests: int = 1) -> Optional[dict]:
    """One response's counters, or ``None`` when any count is invalid.

    ``thinking`` is clamped to ``output``: it is a subset, and a harness that
    reports more thinking than output has misreported one of the two, not spent
    extra tokens.
    """
    values = {
        "input": count(input), "cache_create": count(cache_create),
        "cache_read": count(cache_read), "output": count(output),
        "thinking": count(thinking), "requests": count(requests),
    }
    if any(v is None for v in values.values()):
        return None
    values["thinking"] = min(values["thinking"], values["output"])
    return values


class Event:
    """One deduplicated API response, as an adapter reports it.

    ``key`` identifies the response across every file of a session, so a resume
    or a fork that copies history into a new file is counted once. ``at`` is the
    response's own timestamp (aware UTC), or ``None`` when the record had none.
    """

    __slots__ = ("key", "harness", "model", "agent", "at", "counters", "reported_cost")

    def __init__(self, key, harness, model, agent, at, counters, reported_cost=None):
        self.key = key
        self.harness = harness
        self.model = model
        self.agent = agent
        self.at = at
        self.counters = counters
        self.reported_cost = reported_cost

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Event({self.key!r}, {self.harness}, {self.model}, {self.agent}, {self.counters})"
