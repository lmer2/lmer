"""Usage per project and per period, from the run-dir records (#647).

Sums every ``ai-usage/<session>.yaml`` in the daemon's read-only work-repo
mirror, standalone runs included, by the records' per-UTC-day split. Periods are
UTC days, ISO weeks and calendar months.

A container can push anywhere in the work repo, so every record is validated
before it is summed: shape and bounds (:func:`lmer_usage.record.validate`) and a
key that names the directory it was found in. A failure is skipped and counted
under ``invalid_records``. Cost is priced here, at read time, from the daemon's
table, and never taken from a record. Live numbers for this host's sessions are
added as ``provisional`` where they run ahead of their records.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Optional

from lmer_usage import buckets, prices, record

from . import usage_api, workrepo

PERIODS = ("day", "week", "month")
TOP_RUNS = 10
_CACHE: dict = {}
_CACHE_LIMIT = 32


def period_of(day: str, period: str) -> str:
    when = date.fromisoformat(day)
    if period == "day":
        return when.isoformat()
    if period == "week":
        year, week, _ = when.isocalendar()
        return f"{year}-W{week:02d}"
    if period == "month":
        return f"{when.year}-{when.month:02d}"
    raise ValueError(f"period must be one of {', '.join(PERIODS)}")


def _in_range(day: str, since: Optional[str], until: Optional[str]) -> bool:
    return (since is None or day >= since) and (until is None or day <= until)


def _check_day(value: Optional[str], name: str) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a date, YYYY-MM-DD") from None


def collect(refs: Iterable, host: str, project: str) -> tuple:
    """``(valid records with their run, invalid count)`` for one project.

    ``refs`` list one ref per run, at its newest launch; every launch's
    records are summed (:func:`lmer_platform.workrepo.every_launch`)."""
    valid, invalid = [], 0
    for listed in refs:
        if (listed.host, listed.project) != (host, project):
            continue
        for ref in workrepo.every_launch(listed):
            for sid, data, _ in usage_api.read_records(ref.path):
                errors = record.validate(data) if data is not None else ["unreadable"]
                if errors or not record.key_matches_path(data, host=host, project=project, slug=ref.slug,
                                                         session=sid):
                    invalid += 1
                    continue
                valid.append((ref, sid, data))
    return valid, invalid


def _day_share(data: dict, day: str, row: dict) -> dict:
    """``row``'s counters attributed to ``day``, each by that counter's own daily
    share of the record's total. Records split by day only in total, not per
    model, so this is exact for a one-model record and an apportionment above."""
    totals = data.get("totals") or {}
    day_counts = (data.get("days") or {}).get(day) or {}
    out = {}
    for name in buckets.COUNTERS:
        total = int(totals.get(name, 0))
        out[name] = int(round(int(row.get(name, 0)) * int(day_counts.get(name, 0)) / total)) if total else 0
    return out


def rollup(refs: Iterable, *, host: str, project: str, period: str = "day",
           since: Optional[str] = None, until: Optional[str] = None, table=None,
           live=None, mirror_sha: Optional[str] = None) -> dict:
    if period not in PERIODS:
        raise ValueError(f"period must be one of {', '.join(PERIODS)}")
    since, until = _check_day(since, "from"), _check_day(until, "to")
    cache_key = (mirror_sha, host, project, period, since, until, getattr(table, "hash", None))
    cached = _CACHE.get(cache_key) if mirror_sha else None
    if cached is None:
        cached = _rollup_records(refs, host=host, project=project, period=period,
                                 since=since, until=until, table=table)
        if mirror_sha:
            if len(_CACHE) >= _CACHE_LIMIT:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[cache_key] = cached
    out = {**cached, "periods": [dict(p) for p in cached["periods"]]}
    record_days = out.pop("_record_days")
    joined = out.pop("_record_sessions")
    _overlay_provisional(out, live, record_days, joined, host=host, project=project, period=period,
                         since=since, until=until)
    out["mirror_sha"] = mirror_sha
    return out


def _rollup_records(refs, *, host, project, period, since, until, table) -> dict:
    records, invalid = collect(refs, host, project)
    periods: dict = {}
    record_days: dict = {}
    for ref, sid, data in records:
        # The live overlay reads only the session's own transcripts, so it is
        # compared with the record's own days, children left out.
        record_days[sid] = record.own_days(data)
        for day, counters in (data.get("days") or {}).items():
            if not _in_range(day, since, until):
                continue
            name = period_of(day, period)
            bucket = periods.setdefault(name, {
                "period": name, "totals": buckets.zero(), "by_model": {}, "by_harness": {},
                "by_taskdef": {}, "runs": {}, "rows": [],
            })
            buckets.add(bucket["totals"], counters)
            taskdef = data.get("taskdef") or "unknown"
            buckets.add(bucket["by_taskdef"].setdefault(taskdef, buckets.zero()), counters)
            run_key = (ref.slug, ref.uid)
            buckets.add(bucket["runs"].setdefault(run_key, buckets.zero()), counters)
            for row in data.get("models") or []:
                if row.get("status", "collected") != "collected":
                    continue
                share = _day_share(data, day, row)
                buckets.add(bucket["by_model"].setdefault((row.get("harness"), row.get("model")),
                                                          buckets.zero()), share)
                buckets.add(bucket["by_harness"].setdefault(row.get("harness"), buckets.zero()), share)
                bucket["rows"].append({"harness": row.get("harness"), "model": row.get("model"),
                                       "status": "collected", **share})
    listed = []
    for name in sorted(periods):
        bucket = periods[name]
        top = sorted(bucket["runs"].items(), key=lambda item: prices.unpriced_weight(item[1]), reverse=True)
        listed.append({
            "period": name,
            "totals": bucket["totals"],
            "by_model": [{"harness": h, "model": m, **c} for (h, m), c in sorted(bucket["by_model"].items(),
                                                                               key=lambda i: tuple(map(str, i[0])))],
            "by_harness": [{"harness": h, **c} for h, c in sorted(bucket["by_harness"].items(), key=lambda i: str(i[0]))],
            "by_taskdef": [{"taskdef": t, **c} for t, c in sorted(bucket["by_taskdef"].items())],
            "top_runs": [{"slug": slug, "uid": uid, **c} for (slug, uid), c in top[:TOP_RUNS]],
            "cost": prices.cost(table, bucket["rows"]) if table is not None else None,
        })
    return {"project": f"{host}/{project}", "period": period, "from": since, "to": until,
            "periods": listed, "records": len(records), "invalid_records": invalid,
            "sessions": sorted(record_days), "priced": table is not None,
            "table_hash": getattr(table, "hash", None), "_record_days": record_days,
            "_record_sessions": usage_api.record_sessions((sid, data) for _, sid, data in records)}


def _overlay_provisional(out: dict, live, record_days: dict, joined: dict, *, host, project, period,
                         since, until) -> None:
    """Add this host's live numbers that run ahead of their records. ``joined``
    maps the live view's platform session ids to record sessions."""
    for entry in out["periods"]:
        entry["provisional"] = None
    if live is None:
        return
    provisional: dict = {}
    for sid in live.sessions():
        usage = live.session_usage(sid)
        if not usage or not usage.get("run") or tuple(usage["run"][:2]) != (host, project):
            continue
        for event in live.session_events(sid):
            if event.at is None:
                continue
            day = event.at.date().isoformat()
            if not _in_range(day, since, until):
                continue
            provisional.setdefault(sid, {}).setdefault(day, buckets.zero())
            buckets.add(provisional[sid][day], event.counters)
    extra: dict = {}
    for sid, days in provisional.items():
        recorded = record_days.get(joined.get(sid, sid)) or {}
        for day, counters in days.items():
            ahead = {n: max(0, counters[n] - int((recorded.get(day) or {}).get(n, 0))) for n in buckets.COUNTERS}
            if buckets.is_zero(ahead):
                continue
            buckets.add(extra.setdefault(period_of(day, period), buckets.zero()), ahead)
    for entry in out["periods"]:
        entry["provisional"] = extra.pop(entry["period"], None)
    for name in sorted(extra):
        out["periods"].append({"period": name, "totals": buckets.zero(), "by_model": [], "by_harness": [],
                               "by_taskdef": [], "top_runs": [], "cost": None, "provisional": extra[name]})
    out["periods"].sort(key=lambda p: p["period"])
