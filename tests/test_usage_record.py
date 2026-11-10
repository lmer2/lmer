"""The version-1 usage record: built from a summary, validated before it is summed."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lmer_usage import buckets, record
from lmer_usage.summarise import summarise

SESSION = Path(__file__).parent / "fixtures" / "usage" / "claude" / "session-a"
KEY = {"host": "git.example.com", "project": "group/repo", "slug": "masterplan-issue-1",
       "form": "schema2", "schema": 2, "session": "20261105-101500-abcd", "uid": "20261105-101500-abcd"}
NOW = datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc)


def _built(**kw):
    summary = summarise(sorted(SESSION.rglob("*.jsonl")))
    args = dict(key=KEY, summary=summary, status="ended", final=True, updated_at=NOW,
                taskdef="masterplan", harness="claude", model="opus")
    args.update(kw)
    return record.build(**args)


def test_a_built_record_is_valid():
    data = _built()
    assert record.validate(data) == []
    assert data["version"] == 1 and data["final"] is True and data["partial"] is False
    assert data["started_at"] == "2026-11-05T10:00:00Z"
    assert data["key"]["form"] == "schema2"


def test_later_fields_are_declared_up_front():
    child = {"harness": "codex", "model": "gpt-5.5-codex", "agent": "reviewer",
             "child_session": "20261105-101600-ef01", "status": "collected", **buckets.make(output=40)}
    absent = {"harness": "claude", "model": None, "agent": "implementer",
              "child_session": "20261105-101700-ef02", "status": "not_collected",
              "reason": "child output carried no usage", **buckets.zero()}
    data = _built(partial=True, status="crashed", final=False, extra_rows=[child, absent])
    assert record.validate(data) == []
    assert data["totals"]["output"] == 122 + 40


def test_extra_days_join_the_days_split():
    child = {"harness": "codex", "model": "gpt-5.5-codex", "agent": "reviewer",
             "child_session": "20261105-101600-ef01", "status": "collected", **buckets.make(output=40)}
    plain = _built()
    data = _built(extra_rows=[child], extra_days={"2026-11-06": buckets.make(output=40),
                                                  "2026-11-07": buckets.zero()})
    assert record.validate(data) == []
    assert data["days"]["2026-11-06"]["output"] == plain["days"]["2026-11-06"]["output"] + 40
    assert data["days"]["2026-11-05"] == plain["days"]["2026-11-05"] and list(data["days"]) == sorted(data["days"])
    assert data["child_days"] == {"2026-11-06": buckets.make(output=40), "2026-11-07": buckets.zero()}
    assert record.own_days(data)["2026-11-06"] == plain["days"]["2026-11-06"]
    assert plain["child_days"] == {} and record.own_days(plain) == plain["days"]


def test_a_record_without_child_days_still_validates():
    data = _built()
    del data["child_days"]
    assert record.validate(data) == [] and record.own_days(data) == data["days"]


def test_own_days_reads_an_unvalidated_record():
    assert record.own_days({"days": {"2026-11-06": "x", "2026-11-07": {"output": 5}},
                            "child_days": {"2026-11-07": {"output": 9}}}) == {"2026-11-07": buckets.zero()}
    assert record.own_days({"days": [], "child_days": 3}) == {}


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d.update(version=2), "unknown record version"),
    (lambda d: d["totals"].update(output=10**6), "totals do not equal"),
    (lambda d: d["totals"].update(output=-1), "totals invalid"),
    (lambda d: d["key"].update(session="../other"), "key.session"),
    (lambda d: d["key"].update(form="schema9"), "key.form"),
    (lambda d: d.update(status="finished"), "status"),
    (lambda d: d.update(final="yes"), "final is not a boolean"),
    (lambda d: d["days"].update({"2026-13": buckets.zero()}), "days["),
    (lambda d: d["days"]["2026-11-05"].update(output=10**9), "exceeds totals"),
    (lambda d: d["models"].append({"status": "not_collected", **buckets.make(output=1)}), "carries counts"),
    (lambda d: d.update(harness_reported_cost=[{"currency": "USD", "total": -1}]), "harness_reported_cost"),
    (lambda d: d["totals"].update(thinking=None), "totals invalid"),
    (lambda d: d["models"][0].update(output=None), "models[0] invalid"),
    (lambda d: d["days"]["2026-11-05"].update(thinking=None), "days["),
    (lambda d: d["days"].update({"2026-99-99": buckets.zero()}), "days['2026-99-99'] invalid"),
    (lambda d: d.update(totals={"output": None}, days={"2026-11-05": {"output": 1}}), "totals invalid"),
    (lambda d: d.update(child_days=[]), "child_days is not a mapping"),
    (lambda d: d.update(child_days={"2026-11-05": {"output": 10**9}}), "child_days['2026-11-05'] exceeds days"),
    (lambda d: d.update(child_days={"2026-11-30": buckets.make(output=1)}), "exceeds days"),
    (lambda d: d.update(child_days={"2026-11-05": {"output": -1}}), "child_days['2026-11-05'] invalid"),
])
def test_tampered_records_are_rejected(mutate, needle):
    data = copy.deepcopy(_built())
    mutate(data)
    errors = record.validate(data)
    assert any(needle in e for e in errors), errors


def test_non_mapping_is_rejected():
    assert record.validate(["not", "a", "record"]) == ["record is not a mapping"]


def test_key_must_match_the_path_it_was_found_at():
    data = _built()
    where = dict(host=KEY["host"], project=KEY["project"], slug=KEY["slug"], session=KEY["session"])
    assert record.key_matches_path(data, **where)
    assert not record.key_matches_path(data, **{**where, "slug": "someone-elses-run"})


@pytest.mark.parametrize("session", ["", "../x", "a/b", ".hidden", "x" * 200])
def test_unsafe_session_ids_cannot_name_a_file(session):
    with pytest.raises(ValueError):
        record.file_name(session)


def test_cost_is_never_stored():
    assert "cost" not in _built() and "cost_estimate" not in _built()
