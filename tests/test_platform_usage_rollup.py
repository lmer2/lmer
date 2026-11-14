"""Usage per project and per period from run-dir records (#647)."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from lmer_platform import api, store, usage_rollup, workrepo
from lmer_platform import config as cfg
from lmer_usage import buckets, prices, record
from lmer_usage.buckets import Event
from lmer_usage.summarise import summarise
from tests.conftest import strip_lmer_env

HOST, PROJECT = "git.example.com", "org/repo"
FIXTURE = Path(__file__).parent / "fixtures" / "usage" / "claude" / "session-a"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_cache():
    usage_rollup._CACHE.clear()
    yield
    usage_rollup._CACHE.clear()


def _record(session, slug, *, taskdef="masterplan", project=PROJECT, platform_session=None):
    summary = summarise(sorted(FIXTURE.rglob("*.jsonl")))
    key = {"host": HOST, "project": project, "slug": slug, "form": "legacy", "schema": None,
           "session": session, "uid": None}
    return record.build(key=key, summary=summary, status="ended", final=True, updated_at=NOW,
                        taskdef=taskdef, platform_session=platform_session)


#: A record is filed under the launch's LMER_SESSION_ID; the live view knows the
#: same session by the platform's own id, which the record names.
LAUNCH, PLATFORM = "20261105-101500-1-1111", "s-20261105101500-a1b2"


def _run(tmp_path, slug, records, *, project=PROJECT):
    path = tmp_path / slug
    (path / "ai-usage").mkdir(parents=True)
    for session, data in records.items():
        text = data if isinstance(data, str) else yaml.safe_dump(data)
        (path / "ai-usage" / f"{session}.yaml").write_text(text)
    return SimpleNamespace(host=HOST, project=project, slug=slug, uid=None, path=path)


class TestPeriods:
    @pytest.mark.parametrize("day,period,expected", [
        ("2026-11-05", "day", "2026-11-05"),
        ("2026-11-05", "week", "2026-W45"),
        ("2026-01-01", "week", "2026-W01"),
        ("2027-01-01", "week", "2026-W53"),
        ("2026-11-05", "month", "2026-11"),
    ])
    def test_period_of(self, day, period, expected):
        assert usage_rollup.period_of(day, period) == expected

    def test_daily_rollup_sums_the_days_split(self, tmp_path):
        refs = [_run(tmp_path, "run-a", {"s-a1": _record("s-a1", "run-a")}),
                _run(tmp_path, "run-b", {"s-b1": _record("s-b1", "run-b")})]
        out = usage_rollup.rollup(refs, host=HOST, project=PROJECT, period="day")
        assert [p["period"] for p in out["periods"]] == ["2026-11-05", "2026-11-06"]
        assert out["periods"][0]["totals"]["input"] == 2 * 21
        assert out["periods"][1]["totals"]["input"] == 2 * 5
        assert out["records"] == 2 and out["invalid_records"] == 0
        assert out["periods"][0]["by_taskdef"][0]["taskdef"] == "masterplan"
        assert {r["slug"] for r in out["periods"][0]["top_runs"]} == {"run-a", "run-b"}

    def test_monthly_rollup_merges_days(self, tmp_path):
        refs = [_run(tmp_path, "run-a", {"s-a1": _record("s-a1", "run-a")})]
        out = usage_rollup.rollup(refs, host=HOST, project=PROJECT, period="month")
        assert len(out["periods"]) == 1
        assert out["periods"][0]["totals"]["requests"] == 7

    def test_from_and_to_bound_the_days(self, tmp_path):
        refs = [_run(tmp_path, "run-a", {"s-a1": _record("s-a1", "run-a")})]
        out = usage_rollup.rollup(refs, host=HOST, project=PROJECT, since="2026-11-06", until="2026-11-06")
        assert [p["period"] for p in out["periods"]] == ["2026-11-06"]

    @pytest.mark.parametrize("kw", [{"period": "year"}, {"since": "yesterday"}])
    def test_bad_arguments_are_refused(self, kw, tmp_path):
        with pytest.raises(ValueError):
            usage_rollup.rollup([], host=HOST, project=PROJECT, **kw)

    def test_other_projects_are_left_out(self, tmp_path):
        refs = [_run(tmp_path, "run-x", {"s-x1": _record("s-x1", "run-x", project="org/other")},
                     project="org/other")]
        assert usage_rollup.rollup(refs, host=HOST, project=PROJECT)["records"] == 0


class TestChildren:
    def test_children_are_in_the_periods_and_the_model_breakdown(self, tmp_path):
        summary = summarise(sorted(FIXTURE.rglob("*.jsonl")))
        key = {"host": HOST, "project": PROJECT, "slug": "run-a", "form": "legacy", "schema": None,
               "session": "s-a1", "uid": None}
        child = {"harness": "codex", "model": "gpt-5.5-codex", "agent": "reviewer", "child_session": "c" * 32,
                 "status": "collected", **buckets.make(output=1000, requests=1)}
        data = record.build(key=key, summary=summary, status="ended", final=True, updated_at=NOW,
                            extra_rows=[child], extra_days={"2026-11-06": buckets.make(output=1000, requests=1)})
        out = usage_rollup.rollup([_run(tmp_path, "run-a", {"s-a1": data})], host=HOST, project=PROJECT,
                                  period="month")
        [month] = out["periods"]
        assert month["totals"] == data["totals"]
        assert any(row["harness"] == "codex" and row["output"] > 0 for row in month["by_harness"])


class TestModelShare:
    def test_each_counter_follows_its_own_daily_split(self, tmp_path):
        """A one-model record's breakdown is exact: cache reads stay on the day
        they were read, not moved by the output share."""
        data = _record("s-a1", "run-a")
        day1, day2 = buckets.make(cache_read=1_000_000, output=10), buckets.make(output=1000)
        totals = buckets.add(buckets.add(buckets.zero(), day1), day2)
        [row] = [r for r in data["models"] if r.get("status", "collected") == "collected"][:1]
        data["models"] = [{**row, **totals}]
        data.update(totals=dict(totals), days={"2026-11-05": day1, "2026-11-06": day2})
        assert record.validate(data) == []
        out = usage_rollup.rollup([_run(tmp_path, "run-a", {"s-a1": data})], host=HOST, project=PROJECT)
        first, second = (p["by_model"][0] for p in out["periods"])
        assert (first["cache_read"], first["output"]) == (1_000_000, 10)
        assert (second["cache_read"], second["output"]) == (0, 1000)


class TestEveryLaunch:
    def test_every_launch_of_a_schema2_slug_is_summed(self, tmp_path):
        """The run listing points at the newest launch; the rollup reads them all."""
        group = tmp_path / HOST / "org" / "repo" / "_runs" / "run-a"
        launches = ("20261105-101500-1-1111", "20261106-101500-1-2222")
        for sid in launches:
            (group / sid / "ai-usage").mkdir(parents=True)
            (group / sid / "state.yaml").write_text("slug: run-a\n")
            (group / sid / "ai-usage" / f"{sid}.yaml").write_text(yaml.safe_dump(_record(sid, "run-a")))
        newest = workrepo.RunDirRef(host=HOST, project=PROJECT, slug="run-a", path=group / launches[-1],
                                    dir_name=launches[-1], address=f"{HOST}/org/repo/_runs/run-a/{launches[-1]}")
        out = usage_rollup.rollup([newest], host=HOST, project=PROJECT, period="month")
        one = _record(launches[0], "run-a")["totals"]
        assert out["records"] == 2 and out["invalid_records"] == 0
        assert out["periods"][0]["totals"]["output"] == 2 * one["output"]
        assert [r.path.name for r in workrepo.every_launch(newest)] == list(launches)


class TestValidation:
    def test_a_record_claiming_another_run_is_skipped_and_counted(self, tmp_path):
        refs = [_run(tmp_path, "run-a", {"s-a1": _record("s-a1", "someone-else")})]
        out = usage_rollup.rollup(refs, host=HOST, project=PROJECT)
        assert out["records"] == 0 and out["invalid_records"] == 1 and out["periods"] == []

    def test_a_record_under_another_session_name_is_skipped(self, tmp_path):
        refs = [_run(tmp_path, "run-a", {"s-renamed": _record("s-a1", "run-a")})]
        assert usage_rollup.rollup(refs, host=HOST, project=PROJECT)["invalid_records"] == 1

    def test_out_of_bounds_and_unknown_versions_are_skipped(self, tmp_path):
        huge = copy.deepcopy(_record("s-a2", "run-a"))
        huge["days"]["2026-11-05"]["output"] = 2**60
        future = dict(_record("s-a3", "run-a"), version=9)
        refs = [_run(tmp_path, "run-a", {"s-a1": _record("s-a1", "run-a"), "s-a2": huge,
                                         "s-a3": future, "s-a4": "{not: [yaml"})]
        out = usage_rollup.rollup(refs, host=HOST, project=PROJECT)
        assert out["records"] == 1 and out["invalid_records"] == 3


class TestCost:
    def test_cost_is_priced_at_read_time_and_never_read_from_a_record(self, tmp_path):
        data = dict(_record("s-a1", "run-a"), cost={"total": 999})
        refs = [_run(tmp_path, "run-a", {"s-a1": data})]
        table_path = tmp_path / "prices.yaml"
        table_path.write_text(yaml.safe_dump({"currency": "USD", "models": {"claude-*": {"output": 1_000_000}}}))
        table = prices.load_table(str(table_path))
        out = usage_rollup.rollup(refs, host=HOST, project=PROJECT, period="month", table=table)
        cost = out["periods"][0]["cost"]
        assert cost["currency"] == "USD" and cost["table_hash"] == table.hash
        assert cost["total"] == pytest.approx(122, rel=0.05)

    def test_without_a_table_cost_is_null(self, tmp_path):
        refs = [_run(tmp_path, "run-a", {"s-a1": _record("s-a1", "run-a")})]
        assert usage_rollup.rollup(refs, host=HOST, project=PROJECT)["periods"][0]["cost"] is None


class TestCache:
    def test_results_are_cached_on_the_mirror_sha(self, tmp_path, monkeypatch):
        refs = [_run(tmp_path, "run-a", {"s-a1": _record("s-a1", "run-a")})]
        calls = []
        real = usage_rollup.collect
        monkeypatch.setattr(usage_rollup, "collect", lambda *a, **k: calls.append(1) or real(*a, **k))
        usage_rollup.rollup(refs, host=HOST, project=PROJECT, mirror_sha="abc")
        usage_rollup.rollup(refs, host=HOST, project=PROJECT, mirror_sha="abc")
        assert len(calls) == 1
        usage_rollup.rollup(refs, host=HOST, project=PROJECT, mirror_sha="def")
        assert len(calls) == 2


class FakeLive:
    def __init__(self, sessions):
        self._sessions = sessions

    def sessions(self):
        return list(self._sessions)

    def session_usage(self, sid, **kw):
        return {"run": self._sessions[sid]["run"]}

    def session_events(self, sid):
        return self._sessions[sid]["events"]


def _event(key, at, output):
    return Event(key=key, harness="claude", model="claude-opus-5-5", agent=None, at=at,
                 counters=buckets.make(output=output))


class TestProvisional:
    def test_live_numbers_ahead_of_a_record_are_provisional(self, tmp_path):
        data = _record(LAUNCH, "run-a", platform_session=PLATFORM)
        refs = [_run(tmp_path, "run-a", {LAUNCH: data})]
        recorded = data["days"]["2026-11-06"]["output"]
        live = FakeLive({PLATFORM: {"run": (HOST, PROJECT, "run-a"),
                                  "events": [_event("e", datetime(2026, 11, 6, 1, tzinfo=timezone.utc),
                                                    recorded + 40)]}})
        out = usage_rollup.rollup(refs, host=HOST, project=PROJECT, live=live)
        by_period = {p["period"]: p for p in out["periods"]}
        assert by_period["2026-11-06"]["provisional"]["output"] == 40
        assert by_period["2026-11-05"]["provisional"] is None

    def test_children_in_the_record_do_not_hide_the_parents_unrecorded_usage(self, tmp_path):
        """The live view reads the parent's own transcripts; the record's days
        include its children. Only the parent's own share is compared."""
        summary = summarise(sorted(FIXTURE.rglob("*.jsonl")))
        key = {"host": HOST, "project": PROJECT, "slug": "run-a", "form": "legacy", "schema": None,
               "session": LAUNCH, "uid": None}
        child = {"harness": "codex", "model": "gpt-5.5-codex", "agent": "reviewer", "child_session": "c" * 32,
                 "status": "collected", **buckets.make(output=1000, requests=1)}
        data = record.build(key=key, summary=summary, status="running", final=False, updated_at=NOW,
                            platform_session=PLATFORM, extra_rows=[child],
                            extra_days={"2026-11-06": buckets.make(output=1000, requests=1)})
        own = summary.days["2026-11-06"]["output"]
        live = FakeLive({PLATFORM: {"run": (HOST, PROJECT, "run-a"),
                                  "events": [_event("e", datetime(2026, 11, 6, 1, tzinfo=timezone.utc),
                                                    own + 20)]}})
        out = usage_rollup.rollup([_run(tmp_path, "run-a", {LAUNCH: data})], host=HOST, project=PROJECT,
                                  live=live)
        by_period = {p["period"]: p for p in out["periods"]}
        assert by_period["2026-11-06"]["provisional"]["output"] == 20
        assert by_period["2026-11-06"]["totals"]["output"] == own + 1000

    def test_a_live_session_with_no_record_yet_is_wholly_provisional(self, tmp_path):
        live = FakeLive({"s-new1": {"run": (HOST, PROJECT, "run-n"),
                                    "events": [_event("e", datetime(2026, 11, 7, 1, tzinfo=timezone.utc), 9)]}})
        out = usage_rollup.rollup([], host=HOST, project=PROJECT, live=live)
        assert out["periods"] == [{"period": "2026-11-07", "totals": buckets.zero(), "by_model": [],
                                   "by_harness": [], "by_taskdef": [], "top_runs": [], "cost": None,
                                   "provisional": {**buckets.zero(), "output": 9, "requests": 1}}]

    def test_the_cached_result_is_not_mutated_by_the_overlay(self, tmp_path):
        refs = [_run(tmp_path, "run-a", {"s-a1": _record("s-a1", "run-a")})]
        live = FakeLive({"s-n2": {"run": (HOST, PROJECT, "run-n"),
                                  "events": [_event("e", datetime(2026, 11, 9, tzinfo=timezone.utc), 5)]}})
        usage_rollup.rollup(refs, host=HOST, project=PROJECT, live=live, mirror_sha="s1")
        again = usage_rollup.rollup(refs, host=HOST, project=PROJECT, mirror_sha="s1")
        assert [p["period"] for p in again["periods"]] == ["2026-11-05", "2026-11-06"]


SECRET = "test-secret-value"


@pytest.fixture
def client(tmp_path, monkeypatch):
    strip_lmer_env(monkeypatch)
    monkeypatch.setattr(store, "PLATFORM_DIR", str(tmp_path / "platform"))
    refs = [_run(tmp_path, "run-a", {"s-a1": _record("s-a1", "run-a")})]
    monkeypatch.setattr(api, "run_dirs", lambda config: refs)
    monkeypatch.setattr(api, "mirror_status", lambda config: SimpleNamespace(head_sha="sha-1"))
    return TestClient(api.create_app(cfg.load(), SECRET, state_builder=lambda *a, **k: {}))


class TestRoute:
    def _get(self, client, query):
        return client.get(f"/api/usage/rollup?{query}", headers={"Authorization": f"Bearer {SECRET}"})

    def test_the_route_rolls_up_one_project(self, client):
        reply = self._get(client, f"project={HOST}/{PROJECT}&period=week")
        assert reply.status_code == 200
        body = reply.json()
        assert body["project"] == f"{HOST}/{PROJECT}" and body["mirror_sha"] == "sha-1"
        assert [p["period"] for p in body["periods"]] == ["2026-W45"]

    def test_from_and_to_are_the_query_names(self, client):
        body = self._get(client, f"project={HOST}/{PROJECT}&from=2026-11-06&to=2026-11-06").json()
        assert [p["period"] for p in body["periods"]] == ["2026-11-06"]

    @pytest.mark.parametrize("query", ["project=nohost", f"project={HOST}/{PROJECT}&period=year",
                                       f"project={HOST}/{PROJECT}&from=soon"])
    def test_bad_queries_are_refused(self, client, query):
        assert self._get(client, query).status_code == 400

    def test_the_route_requires_auth(self, client):
        assert client.get(f"/api/usage/rollup?project={HOST}/{PROJECT}").status_code == 401
