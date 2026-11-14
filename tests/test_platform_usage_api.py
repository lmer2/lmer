"""The live usage view's answers and routes (#315)."""

from __future__ import annotations

import copy
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from lmer_platform import api, project_api, store, usage_api, usage_live
from lmer_platform import config as cfg
from lmer_usage import buckets, prices, record
from lmer_usage.buckets import Event
from lmer_usage.summarise import summarise
from tests.conftest import strip_lmer_env

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
SECRET = "test-secret-value"
KEY = ("git.example.com", "org/repo", "masterplan-issue-1")
FIXTURE = Path(__file__).parent / "fixtures" / "usage" / "claude" / "session-a"


def _reading(minutes_ago, percent, *, session="s-1", resets_at="2026-11-05T14:00:00Z", updated=None):
    at = NOW - timedelta(minutes=minutes_ago)
    return {"at": at, "session": session, "percent": float(percent), "resets_at": resets_at,
            "updated": updated or at.isoformat()}


class FakeLive:
    """The collector's read surface, without threads or transcripts."""

    def __init__(self, sessions, samples=None):
        self._sessions = sessions
        self._samples = samples or {}
        self.last_tick_at = NOW
        self.last_error = None

    def sessions(self):
        return list(self._sessions)

    def session_usage(self, sid, *, now=None, burn_minutes=15):
        entry = self._sessions.get(sid)
        if entry is None:
            return None
        since = (now or NOW) - timedelta(minutes=burn_minutes)
        totals, burn, rows, burn_rows = buckets.zero(), buckets.zero(), {}, {}
        for event in entry["events"]:
            buckets.add(totals, event.counters)
            buckets.add(rows.setdefault((event.harness, event.model, None), buckets.zero()), event.counters)
            if event.at >= since:
                buckets.add(burn, event.counters)
                buckets.add(burn_rows.setdefault((event.harness, event.model), buckets.zero()), event.counters)
        return {
            "session": sid, "run": entry["run"], "harness": "claude", "operator": "alice",
            "live": entry.get("live", True), "status": entry.get("status", "collected"), "reason": None,
            "collected_at": NOW.isoformat(), "totals": totals, "burn": burn,
            "models": [{"harness": h, "model": m, "agent": a, "status": "collected", **c}
                       for (h, m, a), c in rows.items()],
            "not_collected": [], "started_at": None, "ended_at": None, "active_seconds": 0,
            "harness_reported_cost": {},
            "burn_models": [{"harness": h, "model": m, "status": "collected", **c}
                            for (h, m), c in burn_rows.items()],
        }

    def session_events(self, sid):
        return list(self._sessions[sid]["events"])

    def sessions_by_run(self):
        out = {}
        for sid, entry in self._sessions.items():
            if entry["run"] is not None:
                out.setdefault(entry["run"], []).append(sid)
        return out

    def samples(self):
        return dict(self._samples)


def _event(key, minutes_ago, model="claude-opus-5-5", **counters):
    return Event(key=key, harness="claude", model=model, agent=None,
                 at=NOW - timedelta(minutes=minutes_ago), counters=buckets.make(**counters))


def _table(tmp_path, models):
    path = tmp_path / "prices.yaml"
    path.write_text(yaml.safe_dump({"currency": "USD", "models": models}))
    return prices.load_table(str(path))


class TestProjection:
    def test_too_few_readings_is_null_with_a_reason(self):
        result = usage_api.project([_reading(5, 40), _reading(1, 42)], now=NOW, minutes=30)
        assert result["rate_pct_per_hour"] is None and result["reason"] == "too few readings"

    def test_readings_spanning_under_ten_minutes_are_too_few(self):
        readings = [_reading(9, 40), _reading(5, 41), _reading(1, 42)]
        assert usage_api.project(readings, now=NOW, minutes=30)["reason"] == "too few readings"

    def test_a_steady_rise_projects_a_fill_time(self):
        # 10 points per hour, 50% now: full in 5 hours, after a reset 2 hours away.
        readings = [_reading(m, 50 - m / 6) for m in (24, 18, 12, 6, 0)]
        result = usage_api.project(readings, now=NOW, minutes=30)
        assert result["rate_pct_per_hour"] == pytest.approx(10.0, rel=1e-3)
        assert result["projected_full_at"].startswith("2026-11-05T17:00")
        assert result["fills_before_reset"] is False

    def test_a_steep_rise_fills_before_reset(self):
        readings = [_reading(m, 70 + (24 - m)) for m in (24, 18, 12, 6, 0)]
        result = usage_api.project(readings, now=NOW, minutes=30)
        assert result["fills_before_reset"] is True

    def test_an_already_full_window_is_full_now(self):
        readings = [_reading(m, 100) for m in (24, 12, 0)]
        result = usage_api.project(readings, now=NOW, minutes=30)
        assert result["projected_full_at"].startswith("2026-11-05T12:00")
        assert result["fills_before_reset"] is True

    def test_a_reset_rollover_starts_a_new_fit(self):
        old = [_reading(m, 90 + (24 - m) / 10, resets_at="2026-11-05T11:55:00Z") for m in (24, 18, 12)]
        new = [_reading(m, 1, resets_at="2026-11-05T16:55:00Z") for m in (4, 1)]
        result = usage_api.project(old + new, now=NOW, minutes=30)
        assert result["readings"] == 2 and result["reason"] == "too few readings"

    def test_the_fit_uses_the_median_so_one_session_cannot_steer_it(self):
        honest = [_reading(m, 30, session=s) for m in (24, 12, 0) for s in ("s-1", "s-2")]
        liar = [_reading(m, 99 if m == 0 else 0, session="s-3") for m in (24, 12, 0)]
        result = usage_api.project(sorted(honest + liar, key=lambda r: r["at"]), now=NOW, minutes=30)
        assert result["rate_pct_per_hour"] == pytest.approx(0.0)
        assert result["projected_full_at"] is None

    def test_readings_older_than_the_window_are_ignored(self):
        readings = [_reading(m, 10 + (60 - m)) for m in (60, 50, 40)] + [_reading(m, 50) for m in (20, 10, 0)]
        assert usage_api.project(readings, now=NOW, minutes=30)["rate_pct_per_hour"] == pytest.approx(0.0)


class TestHeadroom:
    def test_entries_per_group_are_labelled_approximate(self):
        samples = {("claude", "alice", "five_hour"): [_reading(m, 40 + (24 - m) / 6) for m in (24, 12, 0)]}
        entry = usage_api.headroom(samples, now=NOW, projection_minutes=30)[0]
        assert (entry["harness"], entry["operator"], entry["window"], entry["approximate"]) == (
            "claude", "alice", "five_hour", True,
        )
        assert entry["stale"] is False and entry["percent"] == pytest.approx(44.0)

    def test_an_old_reading_is_stale(self):
        samples = {("claude", "alice", "five_hour"): [_reading(45, 40)]}
        assert usage_api.headroom(samples, now=NOW, projection_minutes=30)[0]["stale"] is True

    def test_a_passed_reset_is_stale(self):
        samples = {("claude", "alice", "five_hour"): [_reading(1, 40, resets_at="2026-11-05T11:00:00Z")]}
        assert usage_api.headroom(samples, now=NOW, projection_minutes=30)[0]["stale"] is True


def _fleet_live():
    return FakeLive({
        "s-big": {"run": KEY, "events": [_event("a", 5, output=500, input=100),
                                         _event("b", 120, output=10_000)]},
        "s-small": {"run": ("git.example.com", "org/repo", "other"),
                    "events": [_event("c", 3, model="claude-haiku-5-5", output=50, cache_read=10**7)]},
        "s-none": {"run": None, "events": [], "status": "not_collected"},
    })


class TestFleet:
    def test_window_totals_count_responses_by_their_own_time(self):
        reply = usage_api.fleet(_fleet_live(), window="1h", now=NOW)
        assert reply["totals"]["output"] == 550
        reply = usage_api.fleet(_fleet_live(), window="24h", now=NOW)
        assert reply["totals"]["output"] == 10_550

    def test_unpriced_ranking_leaves_cache_reads_out(self):
        reply = usage_api.fleet(_fleet_live(), now=NOW)
        assert reply["ranking"] == "unpriced_token_weight"
        assert [r["slug"] for r in reply["top_runs"]] == ["masterplan-issue-1", "other"]
        assert reply["cost"] is None

    def test_a_full_price_table_ranks_by_cost(self, tmp_path):
        table = _table(tmp_path, {"claude-opus-5-5": {"output": 1}, "claude-haiku-5-5": {"cache_read": 10}})
        reply = usage_api.fleet(_fleet_live(), table=table, now=NOW)
        assert reply["ranking"] == "cost"
        assert [r["slug"] for r in reply["top_runs"]] == ["other", "masterplan-issue-1"]
        assert reply["cost"]["table_hash"] == table.hash

    def test_a_partial_table_falls_back_to_token_weight(self, tmp_path):
        table = _table(tmp_path, {"claude-opus-5-5": {"output": 1}})
        reply = usage_api.fleet(_fleet_live(), table=table, now=NOW)
        assert reply["ranking"] == "unpriced_token_weight"
        assert reply["cost"]["partial"] is True

    def test_collected_and_not_collected_are_counted(self):
        assert usage_api.fleet(_fleet_live(), now=NOW)["sessions"] == {"collected": 2, "not_collected": 1}

    def test_an_unknown_window_is_refused(self):
        with pytest.raises(ValueError):
            usage_api.fleet(_fleet_live(), window="5y", now=NOW)

    def test_no_collector_is_an_empty_view_not_an_error(self):
        reply = usage_api.fleet(None, now=NOW)
        assert reply["totals"] == buckets.zero() and reply["top_runs"] == [] and reply["collector"] is None


class TestStateFold:
    def test_rows_carry_their_runs_usage(self, tmp_path):
        payload = {"runs": [{"host": KEY[0], "project": KEY[1], "slug": KEY[2]},
                            {"host": "h", "project": "p", "slug": "unknown"}], "attention": []}
        usage_api.fold_state(payload, _fleet_live(), now=NOW)
        usage = payload["runs"][0]["usage"]
        assert usage["totals"]["output"] == 10_500 and usage["burn"]["output"] == 500
        assert usage["status"] == "collected" and "cost" not in usage and "burn_cost" not in usage
        assert payload["runs"][1]["usage"] is None

    def test_cost_appears_only_with_a_table(self, tmp_path):
        table = _table(tmp_path, {"claude-opus-5-5": {"output": 2}})
        payload = {"runs": [{"host": KEY[0], "project": KEY[1], "slug": KEY[2]}]}
        usage_api.fold_state(payload, _fleet_live(), table=table, now=NOW)
        assert payload["runs"][0]["usage"]["cost"]["total"] == pytest.approx(0.021)

    def test_the_burn_is_priced_on_its_own_for_most_expensive_now(self, tmp_path):
        table = _table(tmp_path, {"claude-opus-5-5": {"output": 2}})
        payload = {"runs": [{"host": KEY[0], "project": KEY[1], "slug": KEY[2]}]}
        usage_api.fold_state(payload, _fleet_live(), table=table, now=NOW)
        burn_cost = payload["runs"][0]["usage"]["burn_cost"]
        assert burn_cost["total"] == pytest.approx(0.001) and burn_cost["table_hash"] == table.hash

    def test_a_uid_keyed_row_matches_its_sessions(self):
        uid = "a" * 32
        live = FakeLive({"s-1": {"run": (KEY[0], KEY[1], uid), "events": [_event("k", 1, output=7)]}})
        payload = {"runs": [{"host": KEY[0], "project": KEY[1], "slug": KEY[2], "uid": uid}]}
        usage_api.fold_state(payload, live, now=NOW)
        assert payload["runs"][0]["usage"]["totals"]["output"] == 7


def _record(session, *, final, totals_from=FIXTURE, slug=KEY[2], platform_session=None):
    summary = summarise(sorted(totals_from.rglob("*.jsonl")))
    key = {"host": KEY[0], "project": KEY[1], "slug": slug, "form": "legacy", "schema": None,
           "session": session, "uid": None}
    return record.build(key=key, summary=summary, status="unknown" if not final else "ended",
                        final=final, updated_at=NOW, platform_session=platform_session)


#: A record is filed under the launch's LMER_SESSION_ID; the live view knows the
#: same session by the platform's own id, which the record names.
LAUNCH, PLATFORM = "20261105-101500-1-1111", "s-20261105101500-a1b2"


class TestRunDetail:
    def _live_with(self, sid, *, live, output):
        return FakeLive({sid: {"run": KEY, "live": live, "events": [_event("x", 1, output=output)]}})

    def test_a_killed_session_shows_how_far_its_record_is_behind(self):
        data = _record(LAUNCH, final=False, platform_session=PLATFORM)
        detail = usage_api.run_detail(self._live_with(PLATFORM, live=False, output=10_000),
                                      KEY, [(LAUNCH, data, "https://forge/x")], slug=KEY[2], now=NOW)
        [session] = detail["sessions"]
        assert (session["session"], session["live"]["platform_session"]) == (LAUNCH, PLATFORM)
        note = session["record_behind"]
        assert note["behind"]["output"] == 10_000 - data["totals"]["output"]
        assert note["unpriced_weight_behind"] > 0

    def test_children_in_the_record_do_not_hide_how_far_it_is_behind(self):
        # The live view reads the session's own transcripts only.
        plain = _record(LAUNCH, final=False, platform_session=PLATFORM)
        child = {"harness": "codex", "model": "gpt-5.5-codex", "agent": "reviewer", "child_session": "c" * 32,
                 "status": "collected", **buckets.make(output=5_000)}
        summary = summarise(sorted(FIXTURE.rglob("*.jsonl")))
        data = record.build(key=dict(plain["key"]), summary=summary, status="unknown", final=False,
                            updated_at=NOW, extra_rows=[child], platform_session=PLATFORM)
        detail = usage_api.run_detail(self._live_with(PLATFORM, live=False, output=10_000),
                                      KEY, [(LAUNCH, data, None)], slug=KEY[2], now=NOW)
        note = detail["sessions"][0]["record_behind"]
        assert note["behind"]["output"] == 10_000 - plain["totals"]["output"]

    def test_a_running_session_is_not_news(self):
        data = _record(LAUNCH, final=False, platform_session=PLATFORM)
        detail = usage_api.run_detail(self._live_with(PLATFORM, live=True, output=10_000),
                                      KEY, [(LAUNCH, data, None)], slug=KEY[2], now=NOW)
        assert detail["sessions"][0]["record_behind"] is None

    def test_a_final_record_is_not_behind(self):
        data = _record(LAUNCH, final=True, platform_session=PLATFORM)
        detail = usage_api.run_detail(self._live_with(PLATFORM, live=False, output=10_000),
                                      KEY, [(LAUNCH, data, None)], slug=KEY[2], now=NOW)
        assert detail["sessions"][0]["record_behind"] is None
        assert detail["sessions"][0]["record"]["final"] is True

    def test_a_record_claiming_another_run_is_invalid_and_not_summed(self):
        data = _record("s-x1", final=True, slug="someone-elses-run")
        detail = usage_api.run_detail(None, KEY, [("s-x1", data, None)], slug=KEY[2], now=NOW)
        assert detail["invalid_records"] == 1
        assert detail["sessions"][0]["record"] == {"valid": False, "errors": ["key does not match its path"],
                                                   "url": None}

    def test_a_tampered_record_is_invalid(self):
        data = copy.deepcopy(_record("s-x2", final=True))
        data["totals"]["output"] = 10**9
        detail = usage_api.run_detail(None, KEY, [("s-x2", data, None)], slug=KEY[2], now=NOW)
        assert detail["invalid_records"] == 1


class TestReadRecords:
    def test_records_are_read_and_links_refused(self, tmp_path):
        run_dir = tmp_path / "run"
        (run_dir / "ai-usage").mkdir(parents=True)
        (run_dir / "ai-usage" / "s-ok1.yaml").write_text(yaml.safe_dump(_record("s-ok1", final=True)))
        outside = tmp_path / "outside.yaml"
        outside.write_text(yaml.safe_dump(_record("s-link1", final=True)))
        (run_dir / "ai-usage" / "s-link1.yaml").symlink_to(outside)
        (run_dir / "ai-usage" / "s-big1.yaml").write_text("x: " + "y" * (usage_api.MAX_RECORD_BYTES + 10))
        got = {sid: data for sid, data, _ in usage_api.read_records(run_dir, lambda n: f"u/{n}")}
        assert got["s-ok1"]["key"]["session"] == "s-ok1"
        assert got["s-link1"] is None and got["s-big1"] is None

    def test_a_symlinked_record_dir_is_refused(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "ai-usage").symlink_to(real)
        assert usage_api.read_records(run_dir) == []


class TestPriceTableLoading:
    def test_no_path_means_no_table_and_no_problem(self):
        assert usage_api.price_table(cfg.PlatformConfig()) == (None, None)

    def test_a_malformed_table_is_reported_not_raised(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("currency: USD\nmodels: {m: {output: -1}}\n")
        table, problem = usage_api.price_table(cfg.PlatformConfig(usage_prices_path=str(path)))
        assert table is None and "negative" in problem

    def test_a_changed_table_is_reloaded(self, tmp_path):
        path = tmp_path / "p.yaml"
        path.write_text("currency: USD\nmodels: {m: {output: 1}}\n")
        config = cfg.PlatformConfig(usage_prices_path=str(path))
        first, _ = usage_api.price_table(config)
        path.write_text("currency: EUR\nmodels: {m: {output: 2}}\n")
        os.utime(path, ns=(1, 2 * 10**18))
        second, _ = usage_api.price_table(config)
        assert (first.currency, second.currency) == ("USD", "EUR")


@pytest.fixture
def client(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    config = cfg.load()
    state = {"runs": [{"host": KEY[0], "project": KEY[1], "slug": KEY[2]}], "attention": []}

    def builder(cfg_, **kw):
        payload = copy.deepcopy(state)
        usage_api.fold_state(payload, usage_live.current())
        return payload

    usage_live.register(_fleet_live())
    yield TestClient(api.create_app(config, SECRET, state_builder=builder))
    usage_live.register(None)


def _auth():
    return {"Authorization": f"Bearer {SECRET}"}


class TestRoutes:
    def test_fleet_route(self, client):
        reply = client.get("/api/usage?window=24h", headers=_auth())
        assert reply.status_code == 200
        body = reply.json()
        assert body["window"] == "24h" and body["enabled"] is True
        assert body["prices"] == {"configured": False, "problem": None, "table_hash": None}

    def test_fleet_route_refuses_a_bad_window(self, client):
        assert client.get("/api/usage?window=forever", headers=_auth()).status_code == 400

    def test_fleet_route_requires_auth(self, client):
        assert client.get("/api/usage").status_code == 401

    def test_run_route_refuses_a_missing_run(self, client):
        assert client.get("/api/runs/usage?host=h", headers=_auth()).status_code == 400

    def test_run_route_answers_for_a_run_absent_from_the_mirror(self, client):
        reply = client.get(f"/api/runs/usage?host={KEY[0]}&project={KEY[1]}&slug={KEY[2]}", headers=_auth())
        assert reply.status_code == 200
        body = reply.json()
        assert body["present"] is False and body["invalid_records"] == 0
        assert body["sessions"][0]["live"]["totals"]["output"] == 10_500

    def test_the_fleet_route_is_unbound_like_state(self):
        source = Path(project_api.__file__).read_text()
        assert '"/api/usage"' in source
