"""Read-time pricing from an operator-supplied table (``LMER_PLATFORM_USAGE_PRICES``).

No default prices ship: without a table every cost is null. Records never store
cost; the daemon prices their rows here, and every shown cost names the table
that produced it by ``sha256:`` of the file bytes. Prices are per million tokens
in the four :data:`lmer_usage.buckets.TOKEN_BUCKETS`; ``thinking`` is part of
``output`` and is never priced on its own.
"""

from __future__ import annotations

import fnmatch
import hashlib
import math
from typing import Iterable, Mapping, Optional

import yaml

from . import buckets

ENV = "LMER_PLATFORM_USAGE_PRICES"
_PER = 1_000_000


class PriceTableError(ValueError):
    """The price table file exists but is not a valid table."""


class PriceTable:
    __slots__ = ("currency", "models", "hash")

    def __init__(self, currency: str, models: Mapping, hash: str):
        self.currency = currency
        self.models = dict(models)
        self.hash = hash

    def price_for(self, model: object) -> Optional[dict]:
        """The per-million prices for ``model``: exact id, else the longest matching glob."""
        if not isinstance(model, str):
            return None
        if model in self.models:
            return dict(self.models[model])
        matches = [p for p in self.models if fnmatch.fnmatchcase(model, p)]
        if not matches:
            return None
        # Sorting first makes an equal-length tie deterministic.
        return dict(self.models[max(sorted(matches), key=len)])


def _price(where: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PriceTableError(f"{where}: {value!r} is not a number")
    if value < 0:
        raise PriceTableError(f"{where}: {value!r} is negative")
    return float(value)


def _parse(path: str, data: object) -> tuple:
    if not isinstance(data, Mapping):
        raise PriceTableError(f"{path}: price table is not a mapping")
    currency = data.get("currency")
    if not isinstance(currency, str) or not currency:
        raise PriceTableError(f"{path}: currency is missing or not a string")
    models = data.get("models")
    if not isinstance(models, Mapping):
        raise PriceTableError(f"{path}: models is missing or not a mapping")
    parsed = {}
    for model, entry in models.items():
        if not isinstance(model, str) or not model:
            raise PriceTableError(f"{path}: model id {model!r} is not a string")
        if not isinstance(entry, Mapping):
            raise PriceTableError(f"{path}: models[{model!r}] is not a mapping of bucket prices")
        for name in entry:
            if name not in buckets.TOKEN_BUCKETS:
                raise PriceTableError(
                    f"{path}: models[{model!r}]: unknown bucket {name!r}"
                    f" (expected {', '.join(buckets.TOKEN_BUCKETS)})"
                )
        parsed[model] = {
            name: _price(f"{path}: models[{model!r}].{name}", entry.get(name, 0))
            for name in buckets.TOKEN_BUCKETS
        }
    return currency, parsed


def load_table(path: Optional[str]) -> Optional[PriceTable]:
    """The table at ``path``, or ``None`` when ``path`` is empty or the file is absent.

    Raises :class:`PriceTableError` when the file exists but is malformed.
    """
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PriceTableError(f"{path}: cannot read price table: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise PriceTableError(f"{path}: price table is not valid YAML: {exc}") from exc
    currency, models = _parse(path, data)
    return PriceTable(currency, models, "sha256:" + hashlib.sha256(raw).hexdigest())


def cost(table: Optional[PriceTable], rows: Iterable[Mapping]) -> dict:
    """The cost of usage ``rows`` (``record["models"]`` shaped) under ``table``.

    ``total`` sums only the covered rows; ``partial`` says some row was not
    covered, and it is ``None`` when rows exist but none is covered. Any cost a
    row or record carries is ignored.
    """
    if table is None:
        return {"currency": None, "total": None, "partial": False, "table_hash": None,
                "unpriced_models": []}
    total = 0.0
    covered = 0
    seen = 0
    unpriced = []
    for row in rows:
        if row.get("status", "collected") == "not_collected":
            continue
        seen += 1
        model = row.get("model")
        price = table.price_for(model)
        if price is None:
            if model not in unpriced:
                unpriced.append(model)
            continue
        covered += 1
        total += sum(int(row.get(name, 0)) * price[name] for name in buckets.TOKEN_BUCKETS) / _PER
    return {
        "currency": table.currency,
        "total": round(total, 6) if covered or not seen else None,
        "partial": bool(unpriced),
        "table_hash": table.hash,
        "unpriced_models": unpriced,
    }


def unpriced_weight(counters: Mapping) -> int:
    """A rank for runs whose cost cannot be computed.

    Cache reads are left out: they dominate the counts and would misrank runs.
    """
    return sum(int(counters.get(name, 0)) for name in ("output", "cache_create", "input"))
