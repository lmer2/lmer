"""The live usage view's answers (#315), computed on read.

Everything here is a pure function of the collector's state
(:class:`lmer_platform.usage_live.UsageLive`), the run-dir records read from the
mirror, and the operator's price table. Cost is computed here, at read time,
and never taken from a record.

**Headroom** readings come from container-written status documents, so a group's
projection fits the *median* percent across its sessions per sample time, and one
session cannot steer it. The fit is a least-squares line over the last
``usage_projection_minutes``. It needs at least :data:`MIN_READINGS` readings
spanning :data:`MIN_SPAN_MINUTES`, and a change in ``resets_at`` (the window rolled
over) starts a new fit. It extrapolates the harness's own percent and never
converts tokens to quota.
"""

from __future__ import annotations

import logging
import os
import statistics
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from lmer_usage import buckets, prices, record
from lmer_usage.summarise import open_regular

from . import runs

logger = logging.getLogger(__name__)

WINDOWS = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)}
BURN_MINUTES = 15
MIN_READINGS = 3
MIN_SPAN_MINUTES = 10
STALE_AFTER = timedelta(minutes=30)
TOP_RUNS = 10

_TABLE_CACHE: dict = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _parse(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def price_table(config) -> tuple:
    """``(table or None, problem or None)``, re-read when the file changes."""
    path = getattr(config, "usage_prices_path", None)
    if not path:
        return None, None
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        stamp = None
    cached = _TABLE_CACHE.get(path)
    if cached and cached[0] == stamp:
        return cached[1], cached[2]
    try:
        table, problem = prices.load_table(path), None
        if table is None:
            problem = f"price table {path} not found"
    except prices.PriceTableError as exc:
        table, problem = None, str(exc)
        logger.warning("platform_usage_prices_unusable error=%s", exc)
    _TABLE_CACHE[path] = (stamp, table, problem)
    return table, problem


def _merge_rows(rows: Iterable[dict]) -> list:
    merged: dict = {}
    for row in rows:
        if row.get("status", "collected") != "collected":
            continue
        key = (row.get("harness"), row.get("model"), row.get("agent"))
        buckets.add(merged.setdefault(key, buckets.zero()), row)
    return [{"harness": h, "model": m, "agent": a, "status": "collected", **c}
            for (h, m, a), c in merged.items()]


def _cost(table, rows: list) -> Optional[dict]:
    return prices.cost(table, rows) if table is not None else None


def _rank_value(entry: dict, priced: bool) -> float:
    if priced:
        return float((entry.get("burn_cost") or {}).get("total") or 0.0)
    return float(prices.unpriced_weight(entry["burn"]))


# -- per run (the /api/state row) ------------------------------------------


def run_live(live, key: tuple, *, table=None, now: Optional[datetime] = None,
             index: Optional[dict] = None) -> Optional[dict]:
    """The compact ``usage`` object of one run's row, or ``None`` when this host
    knows no session of it. ``index`` is ``live.sessions_by_run()``, taken once
    per request by a caller that asks for many runs."""
    if live is None:
        return None
    now = now or _now()
    index = live.sessions_by_run() if index is None else index
    sessions = [(sid, u) for sid, u in ((sid, live.session_usage(sid, now=now, burn_minutes=BURN_MINUTES))
                                        for sid in index.get(key, ())) if u and u["run"] == key]
    if not sessions:
        return None
    totals, burn, rows, burn_rows = buckets.zero(), buckets.zero(), [], []
    statuses = set()
    collected_at = None
    for sid, usage in sessions:
        buckets.add(totals, usage["totals"])
        buckets.add(burn, usage["burn"])
        rows.extend(usage["models"])
        if table is not None:
            burn_rows.extend(usage["burn_models"])
        statuses.add(usage["status"])
        stamp = _parse(usage["collected_at"])
        if stamp and (collected_at is None or stamp > collected_at):
            collected_at = stamp
    if statuses == {"collected"}:
        status = "collected"
    elif "collected" in statuses or "partial" in statuses:
        status = "partial"
    else:
        status = "not_collected"
    out = {
        "totals": totals,
        "burn": burn,
        "models": sorted({r["model"] for r in rows if r.get("model")}),
        "collected_at": _iso(collected_at),
        "status": status,
        "sessions": len(sessions),
    }
    if table is not None:
        out["cost"] = _cost(table, _merge_rows(rows))
        # What "most expensive now" ranks by, the same measure as the fleet's.
        out["burn_cost"] = _cost(table, _merge_rows(burn_rows))
    return out


def fold_state(payload: dict, live, *, table=None, now: Optional[datetime] = None) -> None:
    """Put ``usage`` on each fleet row, in place. ``None`` where nothing is known."""
    index = live.sessions_by_run() if live is not None else {}
    for row in (payload.get("runs") or []) + (payload.get("attention") or []):
        if not isinstance(row, dict):
            continue
        key = runs.key_parts_lenient(row)
        row["usage"] = run_live(live, key, table=table, now=now, index=index) if live is not None else None


# -- the fleet (GET /api/usage) --------------------------------------------


def _window_rows(live, sid: str, since: datetime) -> list:
    events = live.session_events(sid)
    rows: dict = {}
    for event in events:
        if event.at is None or event.at < since:
            continue
        key = (event.harness, event.model)
        buckets.add(rows.setdefault(key, buckets.zero()), event.counters)
    return [{"harness": h, "model": m, **c} for (h, m), c in rows.items()]


def fleet(live, *, window: str = "1h", table=None, projection_minutes: int = 30,
          now: Optional[datetime] = None, include=None) -> dict:
    """Fleet usage over ``window``. ``include(run key)``, when given, scopes every
    per-run figure and total to the runs it accepts; a session of no run is then
    left out too. Headroom names no run and is the account's either way."""
    now = now or _now()
    span = WINDOWS.get(window)
    if span is None:
        raise ValueError(f"window must be one of {', '.join(WINDOWS)}")
    since = now - span
    by_harness, by_model = {}, {}
    per_run: dict = {}
    collected = not_collected = 0
    if live is not None:
        for sid in live.sessions():
            usage = live.session_usage(sid, now=now, burn_minutes=BURN_MINUTES)
            if usage is None:
                continue
            if include is not None and (usage["run"] is None or not include(usage["run"])):
                continue
            if usage["status"] == "not_collected":
                not_collected += 1
            else:
                collected += 1
            for row in _window_rows(live, sid, since):
                buckets.add(by_harness.setdefault(row["harness"], buckets.zero()), row)
                buckets.add(by_model.setdefault((row["harness"], row["model"]), buckets.zero()), row)
            key = usage["run"]
            if key is None:
                continue
            entry = per_run.setdefault(key, {"burn": buckets.zero(), "rows": [], "live": False})
            buckets.add(entry["burn"], usage["burn"])
            entry["live"] = entry["live"] or usage["live"]
            entry["rows"].extend(usage["burn_models"])
    priced = table is not None and all(
        not prices.cost(table, entry["rows"])["partial"] for entry in per_run.values()
    )
    ranked = []
    for (host, project, slug), entry in per_run.items():
        if buckets.is_zero(entry["burn"]):
            continue
        item = {"host": host, "project": project, "slug": slug, "live": entry["live"],
                "burn": entry["burn"], "unpriced_weight": prices.unpriced_weight(entry["burn"])}
        if table is not None:
            item["burn_cost"] = prices.cost(table, entry["rows"])
        ranked.append(item)
    ranked.sort(key=lambda item: _rank_value(item, priced), reverse=True)
    totals = buckets.zero()
    for counters in by_harness.values():
        buckets.add(totals, counters)
    window_rows = [{"harness": h, "model": m, "status": "collected", **c} for (h, m), c in by_model.items()]
    return {
        "window": window,
        "generated_at": _iso(now),
        "totals": totals,
        "by_harness": [{"harness": h, **c} for h, c in sorted(by_harness.items(), key=lambda i: str(i[0]))],
        "by_model": [{"harness": h, "model": m, **c} for (h, m), c in sorted(by_model.items(), key=lambda i: str(i[0]))],
        "cost": _cost(table, window_rows),
        "ranking": "cost" if priced else "unpriced_token_weight",
        "top_runs": ranked[:TOP_RUNS],
        "sessions": {"collected": collected, "not_collected": not_collected},
        "headroom": headroom(live.samples() if live is not None else {}, now=now,
                             projection_minutes=projection_minutes),
        "collector": {
            "last_tick_at": _iso(getattr(live, "last_tick_at", None)),
            "last_error": getattr(live, "last_error", None),
        } if live is not None else None,
    }


# -- headroom --------------------------------------------------------------


def project(readings: list, *, now: datetime, minutes: int) -> dict:
    """The fill rate of one group's window and when it would be full.

    ``readings`` are ``{"at", "session", "percent", "resets_at", "updated"}``,
    oldest first. Only the readings that share the freshest one's ``resets_at``
    count: an earlier reset belongs to a window that is over.
    """
    if not readings:
        return {"rate_pct_per_hour": None, "projected_full_at": None, "fills_before_reset": False,
                "readings": 0, "reason": "no readings"}
    current_reset = readings[-1].get("resets_at")
    since = now - timedelta(minutes=minutes)
    by_time: dict = {}
    for reading in readings:
        at = _parse(reading.get("at"))
        if at is None or at < since or reading.get("resets_at") != current_reset:
            continue
        by_time.setdefault(at, []).append(float(reading["percent"]))
    points = sorted((at, statistics.median(values)) for at, values in by_time.items())
    out = {"rate_pct_per_hour": None, "projected_full_at": None, "fills_before_reset": False,
           "readings": len(points), "reason": None}
    span = (points[-1][0] - points[0][0]).total_seconds() / 60 if len(points) > 1 else 0
    if len(points) < MIN_READINGS or span < MIN_SPAN_MINUTES:
        out["reason"] = "too few readings"
        return out
    origin = points[0][0]
    xs = [(at - origin).total_seconds() / 3600 for at, _ in points]
    ys = [value for _, value in points]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    rate = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator if denominator else 0.0
    out["rate_pct_per_hour"] = round(rate, 3)
    latest = ys[-1]
    full_at = None
    if latest >= 100:
        full_at = points[-1][0]
    elif rate > 0:
        full_at = points[-1][0] + timedelta(hours=(100 - latest) / rate)
    if full_at is not None:
        out["projected_full_at"] = _iso(full_at)
        reset = _parse(current_reset)
        out["fills_before_reset"] = bool(reset and full_at < reset)
    return out


def headroom(samples: dict, *, now: datetime, projection_minutes: int) -> list:
    """One entry per (harness, operator, window), an approximation of the account."""
    entries = []
    for (harness, operator, window), readings in sorted(samples.items(), key=lambda i: tuple(map(str, i[0]))):
        if not readings:
            continue
        freshest = readings[-1]
        updated = _parse(freshest.get("updated")) or _parse(freshest.get("at"))
        reset = _parse(freshest.get("resets_at"))
        age = (now - updated).total_seconds() if updated else None
        entries.append({
            "harness": harness,
            "operator": operator,
            "window": window,
            "approximate": True,
            "percent": freshest.get("percent"),
            "resets_at": freshest.get("resets_at"),
            "age_seconds": int(age) if age is not None else None,
            "stale": (age is None or age > STALE_AFTER.total_seconds()) or bool(reset and reset <= now),
            **project(readings, now=now, minutes=projection_minutes),
        })
    return entries


# -- one run in detail (GET /api/runs/usage) -------------------------------


def record_sessions(records: Iterable) -> dict:
    """``platform session -> record session`` from valid ``(sid, data)`` records.

    The live collector keys sessions by the platform's ``s-…`` id, and records
    by ``LMER_SESSION_ID``; neither is derived from the other. A record names
    the platform session that wrote it, which is the only join between them.
    When one platform session wrote several records, the newest one wins.
    """
    newest: dict = {}
    for sid, data in records:
        platform = data.get("platform_session")
        if isinstance(platform, str) and platform:
            stamp = str(data.get("updated_at") or "")
            if platform not in newest or stamp >= newest[platform][0]:
                newest[platform] = (stamp, sid)
    return {platform: sid for platform, (_, sid) in newest.items()}


def run_detail(live, key: tuple, records: list, *, slug: str, table=None,
               now: Optional[datetime] = None) -> dict:
    """Per-session usage of one run, the run-dir record beside the live numbers.

    ``records`` are ``(session id, parsed record or None, forge url or None)``
    from the run dir. A record that fails validation is listed as invalid and
    never summed. The "record is behind" note appears only for a record that is
    not final and whose session is no longer live: a running session's record is
    always behind, and that is not news.
    """
    now = now or _now()
    by_session: dict = {}
    valid: list = []
    invalid = 0
    for sid, data, url in records:
        errors = record.validate(data) if data is not None else ["unreadable"]
        if not errors and not record.key_matches_path(data, host=key[0], project=key[1], slug=slug,
                                                       session=sid):
            errors = ["key does not match its path"]
        if errors:
            invalid += 1
            by_session.setdefault(sid, {})["record"] = {"valid": False, "errors": errors[:5], "url": url}
            continue
        valid.append((sid, data))
        by_session.setdefault(sid, {})["record"] = {
            "valid": True, "url": url, "updated_at": data.get("updated_at"), "final": data.get("final"),
            "partial": data.get("partial"), "status": data.get("status"), "totals": data.get("totals"),
            "models": data.get("models"), "started_at": data.get("started_at"), "ended_at": data.get("ended_at"),
            "active_seconds": data.get("active_seconds"), "harness": data.get("harness"),
            "harness_reported_cost": data.get("harness_reported_cost"),
            "cost": _cost(table, data.get("models") or []),
        }
    if live is not None:
        joined = record_sessions(valid)
        for sid in live.sessions_by_run().get(key, ()):
            usage = live.session_usage(sid, now=now, burn_minutes=BURN_MINUTES)
            if usage and usage["run"] == key:
                usage = dict(usage)
                usage.pop("burn_models", None)
                usage["cost"] = _cost(table, usage["models"])
                usage["platform_session"] = sid
                by_session.setdefault(joined.get(sid, sid), {})["live"] = usage
    sessions = []
    for sid, entry in sorted(by_session.items()):
        rec, cur = entry.get("record"), entry.get("live")
        note = None
        if rec and rec.get("valid") and rec.get("final") is False and cur and not cur.get("live"):
            # The live view reads the session's own transcripts, not its
            # children's, so it is compared with the record's own share.
            own = dict(rec["totals"])
            for row in rec.get("models") or []:
                if row.get("child_session") and row.get("status", "collected") == "collected":
                    own = {n: int(own.get(n, 0)) - int(row.get(n, 0)) for n in buckets.COUNTERS}
            delta = {n: int(cur["totals"].get(n, 0)) - int(own.get(n, 0)) for n in buckets.COUNTERS}
            if any(v > 0 for v in delta.values()):
                note = {"behind": delta, "unpriced_weight_behind": prices.unpriced_weight(delta)}
        sessions.append({"session": sid, "record": rec, "live": cur, "record_behind": note})
    return {"run": {"host": key[0], "project": key[1], "slug": slug}, "sessions": sessions,
            "invalid_records": invalid, "priced": table is not None,
            "table_hash": getattr(table, "hash", None)}


#: A record is a few KiB; anything far larger is not one.
MAX_RECORD_BYTES = 1024 * 1024
MAX_RECORDS = 200


def read_records(run_dir, url_for=None) -> list:
    """``(session id, parsed record or None, url)`` for each ``ai-usage/*.yaml``.

    The mirror is a checkout of a repo every container can push to, so the
    directory and each file are opened refusing symlinks, FIFOs and oversized
    files, and parsed with ``yaml.safe_load``.
    """
    # deferred: import cost — PyYAML loads only for the routes that read records
    import yaml

    directory = run_dir / record.DIR_NAME
    if directory.is_symlink() or not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.yaml"))[:MAX_RECORDS]:
        sid = path.stem
        if not record.valid_session(sid):
            continue
        data = None
        try:
            with open_regular(path) as handle:
                raw = handle.read(MAX_RECORD_BYTES + 1)
            if len(raw) <= MAX_RECORD_BYTES:
                parsed = yaml.safe_load(raw)
                data = parsed if isinstance(parsed, dict) else None
        except (OSError, yaml.YAMLError, UnicodeError):
            data = None
        out.append((sid, data, url_for(f"{record.DIR_NAME}/{path.name}") if url_for else None))
    return out
