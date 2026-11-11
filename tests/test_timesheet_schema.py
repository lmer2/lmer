"""Tests for ``lmer_timesheet`` — the ``timesheet.d/`` schema (spec D15, D19, D20).

Covers the entry round trip and D15 key order, minimal hand-written entries,
the optional ``sessions`` run references (issue #648), the optional ``issues``
list, the ``edit`` trigger, ``per_issue`` fields and the issue map (issue #665),
duration derivation, the path layout and its slug guard, ULID ordering,
handler definitions from a fixture work repo, required-field and pattern
validation, ledger parsing with broken lines, and sync status including a
hash mismatch. Every time is explicit; nothing reads the wall clock.
"""

import json
import os
import re
import subprocess
import sys
import textwrap
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import pytest
import yaml

import lmer_timesheet as ts
from lmer_cli import runref
from lmer_platform import runs, timetrack_entries
from lmer_timesheet import entry as entry_module
from lmer_timesheet import handlers as handlers_module
from lmer_timesheet.entry import ENTRY_KEYS

UTC = timezone.utc
START = datetime(2026, 10, 27, 9, 2, tzinfo=UTC)
END = datetime(2026, 10, 27, 9, 40, tzinfo=UTC)
UID = "981ec036134f4a8580927ddca71658a9"
RUN_V2 = f"git.example.com/acme/widgets/{UID}"
RUN_V1 = "git.example.com/acme/widgets/develop-issue-12"
ISSUE = "https://git.example.com/acme/widgets/-/issues/12"
MR = "https://git.example.com/acme/widgets/-/merge_requests/7"

FIXTURE_HANDLER = textwrap.dedent(
    """\
    name: tracker-x
    label: Tracker X
    sync: true
    fields:
      - key: task_id
        label: Tracker X task
        required: true
        pattern: '[0-9a-z]{9}'
        hint: Paste the task id from Tracker X
        link: https://tracker-x.example/t/{value}
      - key: note
        label: Note
    """
)


def _entry(**overrides):
    values = dict(
        id="01JB0000000000000000000000",
        person="alex",
        project="webshop",
        handler="tracker-x",
        fields={"task_id": "86c1x2y3z"},
        client="acme",
        title="Checkout bug triage",
        start=START,
        end=END,
        trigger=ts.Trigger(start="activity", stop="switch"),
    )
    values.update(overrides)
    return ts.Entry(**values)


# ---------------------------------------------------------------------------
# entries
# ---------------------------------------------------------------------------


def test_round_trip_keeps_every_field():
    entry = _entry()
    parsed, problems = ts.parse_entry(entry.to_yaml())
    assert problems == []
    assert parsed == entry
    assert parsed.duration == 38 * 60
    assert parsed.source == "lmer"


def test_to_yaml_writes_d15_keys_in_order_and_utc():
    local = timezone(timedelta(hours=2))
    entry = _entry(start=START.astimezone(local), end=END.astimezone(local))
    text = entry.to_yaml()
    # An entry with no sessions or issues leaves those keys out; every other is written.
    assert list(yaml.safe_load(text)) == list(ENTRY_KEYS[:-2])
    assert ENTRY_KEYS == (
        "id", "person", "project", "handler", "fields", "client", "title",
        "start", "end", "duration", "trigger", "source", "sessions", "issues",
    )
    with_both = _entry(sessions=[ts.SessionRef(run=RUN_V2, uid=UID)], issues=[ISSUE]).to_yaml()
    assert list(yaml.safe_load(with_both)) == list(ENTRY_KEYS)
    with_issues = _entry(issues=[ISSUE]).to_yaml()
    assert list(yaml.safe_load(with_issues)) == [*ENTRY_KEYS[:-2], "issues"]
    assert "start: '2026-10-27T09:02:00Z'" in text
    assert "end: '2026-10-27T09:40:00Z'" in text


def test_numeric_field_value_survives_as_a_string():
    entry = _entry(fields={"task_id": "12345"})
    parsed, problems = ts.parse_entry(entry.to_yaml())
    assert problems == []
    assert parsed.fields == {"task_id": "12345"}


def test_minimal_hand_written_entry_gets_defaults():
    text = textwrap.dedent(
        """\
        id: lunch-planning-1
        person: alex
        project: docs-site
        start: 2026-10-27T13:00:00+02:00
        duration: 1800
        """
    )
    entry, problems = ts.parse_entry(text)
    assert problems == []
    assert entry.handler == "timesheet"
    assert entry.source == "hand"
    assert entry.trigger == ts.Trigger(start="hand", stop="hand")
    assert entry.fields == {}
    assert entry.client == ""
    assert entry.title == ""
    assert entry.start == datetime(2026, 10, 27, 11, 0, tzinfo=UTC)
    assert entry.end == datetime(2026, 10, 27, 11, 30, tzinfo=UTC)


def test_duration_derived_from_start_and_end():
    text = textwrap.dedent(
        """\
        id: x1
        person: alex
        project: docs-site
        start: '2026-10-27T09:40:00Z'
        end: '2026-10-27T10:05:30Z'
        """
    )
    entry, problems = ts.parse_entry(text)
    assert problems == []
    assert entry.duration == 25 * 60 + 30


def test_constructor_derives_the_missing_side():
    by_duration = _entry(end=None, duration=600)
    assert by_duration.end == START + timedelta(seconds=600)
    with pytest.raises(ValueError):
        _entry(end=None, duration=None)


def test_constructor_measures_across_a_dst_change_in_utc():
    # New York springs forward at 02:00 on 8 March 2026: 00:30-03:30 local is 2 hours.
    new_york = ZoneInfo("America/New_York")
    start = datetime(2026, 3, 8, 0, 30, tzinfo=new_york)
    by_end = _entry(start=start, end=datetime(2026, 3, 8, 3, 30, tzinfo=new_york))
    assert by_end.duration == 7200
    data = by_end.to_dict()
    assert (data["start"], data["end"]) == ("2026-03-08T05:30:00Z", "2026-03-08T07:30:00Z")
    by_duration = _entry(start=start, end=None, duration=10800)
    assert ts.format_timestamp(by_duration.end) == "2026-03-08T08:30:00Z"


def test_inconsistent_duration_is_reported_not_fatal():
    text = _entry().to_yaml().replace("duration: 2280", "duration: 60")
    entry, problems = ts.parse_entry(text)
    assert entry is not None
    assert entry.duration == 60
    assert problems == ["duration: 60 does not match start and end (2280)"]


MINIMAL = {
    "id": "a",
    "person": "alex",
    "project": "p",
    "start": "2026-10-27T09:00:00Z",
    "duration": 5,
}


def _hand(**changes):
    """MINIMAL with *changes* applied as YAML text; a ``None`` value drops the key."""
    data = {**MINIMAL, **changes}
    return yaml.safe_dump({k: v for k, v in data.items() if v is not None})


@pytest.mark.parametrize(
    "text, expected",
    [
        (_hand(id=None), "id: required"),
        (_hand(person=None), "person: required"),
        (_hand(project=None), "project: required"),
        (_hand(start=None), "start: required"),
        (_hand(duration=None), "end or duration: required"),
        (_hand(start="2026-10-27T09:00:00"), "has no UTC offset"),
        (_hand(start=date(2026, 10, 27)), "is a date"),
        (_hand(start="yesterday"), "is not an ISO 8601 timestamp"),
        (_hand(duration=None, end="2026-10-27T08:00:00Z"), "end: before start"),
        (_hand(duration=-1), "must not be negative"),
        (_hand(duration=1.5), "must be a whole number of seconds"),
        (_hand(person="runs"), "person: 'runs'"),
        (_hand(project="../x"), "project: '../x'"),
        (_hand(project="web--shop"), "project: 'web--shop'"),
        (_hand(fields=["task_id"]), "fields: must be a mapping"),
        ("- not\n- a mapping\n", "not a mapping"),
        ("id: [unclosed\n", "not valid YAML"),
    ],
)
def test_unusable_entries_return_none_with_the_reason(text, expected):
    entry, problems = ts.parse_entry(text)
    assert entry is None
    assert any(expected in problem for problem in problems), problems


def test_soft_problems_are_reported_with_the_entry():
    text = textwrap.dedent(
        """\
        id: a
        person: alex
        project: p
        start: '2026-10-27T09:00:00Z'
        duration: 5
        duraton: 6
        fields: {task_id: 12345}
        trigger: {start: manual, stop: lunch}
        source: robot
        """
    )
    entry, problems = ts.parse_entry(text)
    assert entry is not None
    assert entry.fields == {"task_id": "12345"}
    assert entry.trigger == ts.Trigger(start="manual", stop="lunch")
    assert problems == [
        "duraton: unknown key",
        "fields.task_id: must be a string (quote it)",
        "trigger.stop: 'lunch' is not one of ('manual', 'activity', 'idle', 'switch', 'hand', 'edit')",
        "source: 'robot' is not one of ('lmer', 'hand')",
    ]


def _alias_bomb(target: str, *, levels: int = 7, width: int = 9) -> str:
    """A small entry whose *target* line expands to width**(levels+1) items."""
    lines = ["anchors:", f"  l0: &l0 [{', '.join(['x'] * width)}]"]
    for level in range(1, levels + 1):
        refs = ", ".join([f"*l{level - 1}"] * width)
        lines.append(f"  l{level}: &l{level} [{refs}]")
    return "\n".join(lines + [*_MINIMAL_LINES, target.replace("BOMB", f"*l{levels}")]) + "\n"


def _merge_bomb(*, levels: int = 7, width: int = 9) -> str:
    """A small entry whose merge keys make PyYAML build width**levels key pairs."""
    lines = ["anchors:", "  m0: &m0 {k: x}"]
    for level in range(1, levels + 1):
        refs = ", ".join([f"*m{level - 1}"] * width)
        lines.append(f"  m{level}: &m{level} {{<<: [{refs}]}}")
    return "\n".join(lines + [*_MINIMAL_LINES, f"fields: {{<<: *m{levels}}}"]) + "\n"


_MINIMAL_LINES = [
    "id: a", "person: alex", "project: p", "start: '2026-10-27T09:00:00Z'", "duration: 5",
]


@pytest.mark.parametrize(
    "text",
    [_alias_bomb("client: BOMB"), _alias_bomb("fields: {task_id: BOMB}"), _merge_bomb(),
     "id: &a x\nperson: *a\n"],
    ids=["alias-bomb-client", "alias-bomb-field", "merge-key-bomb", "plain-alias"],
)
def test_yaml_aliases_are_refused_before_they_expand(text):
    assert len(text) < 1000
    began = time.monotonic()
    entry, problems = ts.parse_entry(text)
    assert time.monotonic() - began < 1.0
    assert entry is None
    assert len(problems) == 1 and problems[0].startswith("not valid YAML: aliases are not allowed")
    assert len(problems[0]) < 300, problems[0][:300]


@pytest.mark.parametrize(
    "target, expected",
    [
        ("fields: {task_id: [a, b], note: kept}", "fields.task_id: must be a string, not a list"),
        ("client: {a: b}", "client: must be a string, not a dict"),
        ("title: [a, b]", "title: must be a string, not a list"),
        ("trigger: {start: [manual]}", "trigger.start: a list is not one of"),
        ("source: [lmer]", "source: a list is not one of"),
        ("handler: [timesheet]", "handler: a list is not a handler name"),
        ("end: [a, b]", "end: a list is not an ISO 8601 timestamp"),
    ],
)
def test_non_scalar_values_are_named_by_type_and_dropped(target, expected):
    text = "\n".join([*_MINIMAL_LINES, target]) + "\n"
    entry, problems = ts.parse_entry(text)
    assert any(problem.startswith(expected) for problem in problems), problems
    assert not any("[" in problem or "{" in problem for problem in problems), problems
    if entry is not None:
        assert entry.client == "" and entry.title == ""
        assert entry.trigger == ts.Trigger(start="hand", stop="hand")
        assert entry.source == "hand"
        assert entry.fields == ({"note": "kept"} if "fields" in target else {})


@pytest.mark.parametrize(
    "text, expected",
    [
        (_hand(duration=10**20), "duration: too large"),
        (_hand(start="9999-12-31T23:00:00Z", duration=7200), "duration: too large"),
        (_hand(start="9999-12-31T23:00:00-05:00"), "out of range"),
        (_hand(start="0001-01-01T00:00:00+05:00"), "out of range"),
        ("id: a\nperson: alex\nproject: p\nstart: 9999-12-31T23:00:00-05:00\nduration: 5\n",
         "out of range"),
        ("id: a\nperson: alex\nproject: p\nstart: 2026-13-45T00:00:00Z\nduration: 5\n",
         "not valid YAML"),
        ("id: a\nfields: {n: " + "1" * 5000 + "}\n", "not valid YAML"),
        ("id: " + "[" * 5000 + "]" * 5000 + "\n", "not valid YAML"),
    ],
    ids=["huge-duration", "end-past-9999", "start-past-9999", "start-before-1",
         "yaml-timestamp-past-9999", "yaml-timestamp-month-13", "huge-int", "deep-nesting"],
)
def test_unrepresentable_values_are_problems_not_exceptions(text, expected):
    entry, problems = ts.parse_entry(text)
    assert entry is None
    assert any(expected in problem for problem in problems), problems
    assert all(len(problem) < 300 for problem in problems), [p[:300] for p in problems]


def test_one_hostile_entry_file_does_not_stop_its_neighbours(tmp_path):
    good = _entry()
    directory = tmp_path / "timesheet.d" / "alex" / "2026" / "10"
    directory.mkdir(parents=True)
    (directory / "20261027T090000Z--p--bomb.yaml").write_text(_alias_bomb("client: BOMB"))
    (directory / "20261027T090000Z--p--merge.yaml").write_text(_merge_bomb())
    (directory / "20261027T090000Z--p--long.yaml").write_text(_hand(duration=10**20))
    (directory / "20261027T090000Z--p--deep.yaml").write_text("id: " + "[" * 5000 + "]" * 5000)
    (directory / ts.entry_path(good).name).write_text(good.to_yaml())

    began = time.monotonic()
    parsed = {}
    for name in sorted(os.listdir(directory)):
        path = f"timesheet.d/alex/2026/10/{name}"
        parsed[name] = ts.parse_entry((directory / name).read_bytes(), path=path)
    assert time.monotonic() - began < 5.0

    assert parsed[ts.entry_path(good).name] == (good, [])
    assert all(len(problem) < 300 for _entry_, problems in parsed.values() for problem in problems)


def test_hostile_files_reach_the_platform_entries_view_as_problem_rows(tmp_path):
    good = _entry(handler="timesheet", fields={})
    directory = tmp_path / ts.entry_path(good).parent
    directory.mkdir(parents=True)
    (directory / "20261027T090000Z--p--bomb.yaml").write_text(_alias_bomb("title: BOMB"))
    (directory / "20261027T090000Z--p--merge.yaml").write_text(_merge_bomb())
    (directory / "20261027T090000Z--p--long.yaml").write_text(_hand(duration=10**20))
    (directory / ts.entry_path(good).name).write_text(good.to_yaml())

    handlers = {ts.TIMESHEET.name: ts.TIMESHEET}
    ledgers = timetrack_entries._Ledgers(tmp_path, "alex")
    began = time.monotonic()
    rows = [
        timetrack_entries._file_row(path, raw, "mirror", handlers, ledgers, {})
        for path, raw in timetrack_entries._mirror_files(tmp_path, "alex")
    ]
    assert time.monotonic() - began < 5.0
    problems = {PurePosixPath(row["path"]).name: row["problems"] for row in rows}
    assert problems.pop(ts.entry_path(good).name) == []
    expected = {
        "20261027T090000Z--p--bomb.yaml": "not valid YAML: aliases are not allowed",
        "20261027T090000Z--p--merge.yaml": "not valid YAML: aliases are not allowed",
        "20261027T090000Z--p--long.yaml": "duration: too large",
    }
    assert sorted(problems) == sorted(expected)
    for name, prefix in expected.items():
        assert len(problems[name]) == 1 and problems[name][0].startswith(prefix), problems[name]


def test_parse_entry_accepts_bytes():
    entry = _entry()
    parsed, problems = ts.parse_entry(entry.to_yaml().encode("utf-8"))
    assert problems == [] and parsed == entry
    parsed, problems = ts.parse_entry(b"\xff\xfe")
    assert parsed is None and problems[0].startswith("not UTF-8")


def test_path_is_checked_against_person_and_month():
    entry = _entry()
    good = ts.entry_path(entry)
    assert ts.parse_entry(entry.to_yaml(), path=good)[1] == []
    wrong = PurePosixPath("timesheet.d/sam/2026/11/x.yaml")
    _, problems = ts.parse_entry(entry.to_yaml(), path=wrong)
    assert problems == [
        "person: 'alex' but the file is under 'sam'",
        "start: UTC month is 2026/10 but the file is under 2026/11",
    ]


# ---------------------------------------------------------------------------
# sessions (issue #648)
# ---------------------------------------------------------------------------


def test_sessions_round_trip_in_order():
    refs = [ts.SessionRef(run=RUN_V2, uid=UID), ts.SessionRef(run=RUN_V1)]
    entry = _entry(sessions=refs)
    text = entry.to_yaml()
    assert yaml.safe_load(text)["sessions"] == [{"run": RUN_V2, "uid": UID}, {"run": RUN_V1}]
    parsed, problems = ts.parse_entry(text)
    assert problems == []
    assert parsed == entry
    assert parsed.sessions == refs


def test_an_entry_without_sessions_parses_to_none():
    entry, problems = ts.parse_entry(_entry().to_yaml())
    assert problems == []
    assert entry.sessions == []
    assert "sessions" not in _entry().to_dict()


def test_a_uid_alone_is_a_reference():
    text = _entry().to_yaml() + f"sessions:\n- uid: {UID}\n"
    entry, problems = ts.parse_entry(text)
    assert problems == []
    assert entry.sessions == [ts.SessionRef(uid=UID)]


def test_unusable_session_items_are_dropped_with_a_problem_and_the_entry_kept():
    text = _entry().to_yaml() + textwrap.dedent(
        f"""\
        sessions:
        - run: {RUN_V2}
          uid: {UID}
          note: extra
        - just-a-string
        - run: not-a-key
        - uid: ABC
        - run: 12
        - {{}}
        - run: {RUN_V2}
          uid: {UID}
        - run: {RUN_V1}
        """
    )
    entry, problems = ts.parse_entry(text)
    assert entry is not None
    assert entry.sessions == [ts.SessionRef(run=RUN_V2, uid=UID), ts.SessionRef(run=RUN_V1)]
    assert problems == [
        "sessions[0].note: unknown key",
        "sessions[1]: must be a mapping with run or uid, not 'just-a-string'",
        "sessions[2]: run 'not-a-key' is not a host/project/run key",
        "sessions[3]: uid 'ABC' is not 32 lowercase hex",
        "sessions[4].run: must be a string, not 12",
        "sessions[4]: a session reference needs a run or a uid",
        "sessions[5]: a session reference needs a run or a uid",
        "sessions[6]: a repeat of an earlier item",
    ]


@pytest.mark.parametrize("value, expected", [
    ("a-string", "sessions: must be a list, not 'a-string'"),
    ("{run: x}", "sessions: must be a list, not a dict"),
])
def test_sessions_that_are_not_a_list_are_a_problem_not_fatal(value, expected):
    entry, problems = ts.parse_entry(_entry().to_yaml() + f"sessions: {value}\n")
    assert entry is not None and entry.sessions == []
    assert problems == [expected]


def test_null_sessions_mean_none():
    entry, problems = ts.parse_entry(_entry().to_yaml() + "sessions:\n")
    assert problems == [] and entry.sessions == []


@pytest.mark.parametrize("values", [{}, {"run": "x"}, {"uid": "0" * 31}])
def test_session_ref_refuses_what_it_cannot_reference(values):
    with pytest.raises(ValueError):
        ts.SessionRef(**values)


def test_a_parser_without_the_key_still_reads_the_entry(monkeypatch):
    # The reader a sync process built against !439 runs: sessions is unknown to it.
    monkeypatch.setattr(entry_module, "ENTRY_KEYS", tuple(k for k in ENTRY_KEYS if k != "sessions"))
    monkeypatch.setattr(entry_module, "_parse_sessions", lambda raw, problems: [])
    text = _entry(sessions=[ts.SessionRef(run=RUN_V2, uid=UID)]).to_yaml()
    entry, problems = ts.parse_entry(text)
    assert entry is not None and entry.id == "01JB0000000000000000000000"
    assert problems == ["sessions: unknown key"]


# ---------------------------------------------------------------------------
# issues and the edit trigger (issue #665)
# ---------------------------------------------------------------------------


def test_issues_round_trip_after_sessions():
    entry = _entry(sessions=[ts.SessionRef(run=RUN_V2)], issues=[ISSUE, MR])
    text = entry.to_yaml()
    assert yaml.safe_load(text)["issues"] == [ISSUE, MR]
    parsed, problems = ts.parse_entry(text)
    assert problems == []
    assert parsed == entry


def test_an_entry_without_issues_writes_no_key_and_parses_to_none():
    assert "issues" not in _entry().to_dict()
    entry, problems = ts.parse_entry(_entry().to_yaml())
    assert problems == [] and entry.issues == []
    entry, problems = ts.parse_entry(_entry().to_yaml() + "issues:\n")
    assert problems == [] and entry.issues == []


def test_issues_are_normalised_on_read():
    text = _entry().to_yaml() + (
        "issues:\n"
        "- HTTPS://Git.Example.com/acme/widgets/-/work_items/12/?x=1#note_5\n"
        f"- {MR}\n"
    )
    entry, problems = ts.parse_entry(text)
    assert problems == []
    assert entry.issues == [ISSUE, MR]


def test_bad_issue_items_are_soft_problems_and_dropped():
    text = _entry().to_yaml() + textwrap.dedent(
        f"""\
        issues:
        - {ISSUE}
        - 12
        - not a url
        - ftp://git.example.com/acme/widgets/-/issues/3
        - {ISSUE}/
        - https://user:secret@git.example.com/acme/widgets/-/issues/4
        - {MR}
        """
    )
    entry, problems = ts.parse_entry(text)
    assert entry is not None
    assert entry.issues == [ISSUE, MR]
    assert problems == [
        "issues[1]: 12 is not an issue URL",
        "issues[2]: 'not a url' is not an issue URL",
        "issues[3]: 'ftp://git.example.com/acme/widgets/-/issues/3' is not an http(s) issue URL",
        "issues[4]: a repeat of an earlier item",
        "issues[5]: an issue URL must not carry credentials",
    ]
    assert all("secret" not in problem for problem in problems)


@pytest.mark.parametrize("value, expected", [
    ("a-string", "issues: must be a list, not 'a-string'"),
    ("{a: b}", "issues: must be a list, not a dict"),
])
def test_issues_that_are_not_a_list_are_a_problem_not_fatal(value, expected):
    entry, problems = ts.parse_entry(_entry().to_yaml() + f"issues: {value}\n")
    assert entry is not None and entry.issues == []
    assert problems == [expected]


def test_a_huge_issue_item_makes_a_short_problem():
    entry, problems = ts.parse_entry(_entry().to_yaml() + "issues:\n- " + "x" * 5000 + "\n")
    assert entry is not None and entry.issues == []
    assert len(problems) == 1 and len(problems[0]) < 300


@pytest.mark.parametrize("side", ["start", "stop"])
def test_the_edit_trigger_parses_clean(side):
    sides = {"start": "activity", "stop": "switch", side: "edit"}
    entry = _entry(trigger=ts.Trigger(**sides))
    parsed, problems = ts.parse_entry(entry.to_yaml())
    assert problems == [] and parsed.trigger == ts.Trigger(**sides)


def test_a_parser_without_issues_or_edit_still_reads_the_entry(monkeypatch):
    # The reader a sync process built against #648 runs: issues and edit are unknown to it.
    monkeypatch.setattr(entry_module, "ENTRY_KEYS", tuple(k for k in ENTRY_KEYS if k != "issues"))
    monkeypatch.setattr(entry_module, "TRIGGERS", tuple(t for t in ts.TRIGGERS if t != "edit"))
    monkeypatch.setattr(entry_module, "_parse_issues", lambda raw, problems: [])
    text = _entry(issues=[ISSUE], trigger=ts.Trigger(start="edit", stop="edit")).to_yaml()
    entry, problems = ts.parse_entry(text)
    assert entry is not None and entry.trigger == ts.Trigger(start="edit", stop="edit")
    assert problems == [
        "issues: unknown key",
        "trigger.start: 'edit' is not one of ('manual', 'activity', 'idle', 'switch', 'hand')",
        "trigger.stop: 'edit' is not one of ('manual', 'activity', 'idle', 'switch', 'hand')",
    ]


@pytest.mark.parametrize("raw, expected", [
    (ISSUE, ISSUE),
    (ISSUE + "/", ISSUE),
    (ISSUE + "?foo=bar", ISSUE),
    (ISSUE + "#note_1", ISSUE),
    (ISSUE + "//?a=b#c", ISSUE),
    ("  " + ISSUE + "  ", ISSUE),
    ("HTTPS://GIT.Example.COM/acme/widgets/-/issues/12", ISSUE),
    ("https://git.example.com/acme/widgets/-/work_items/12", ISSUE),
    ("https://git.example.com/acme/widgets/-/work_items/12/", ISSUE),
    (MR, MR),
    (MR + "/diffs", MR + "/diffs"),
    ("https://github.com/acme/widgets/pull/9/", "https://github.com/acme/widgets/pull/9"),
    ("http://tracker.example/t/86c1x2y3z", "http://tracker.example/t/86c1x2y3z"),
    # Only the host is folded: a path is case-sensitive.
    ("https://git.example.com/Acme/Widgets/-/issues/12", "https://git.example.com/Acme/Widgets/-/issues/12"),
])
def test_normalise_issue_url(raw, expected):
    assert ts.normalise_issue_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "  ", "issue-12", "#12", "git.example.com/acme/widgets/-/issues/12",
    "ftp://git.example.com/x/-/issues/1", "mailto:a@example.com", "https://", "https://example.com",
    "https://example.com/", "https://a b.example/x", "https://user@git.example.com/x/-/issues/1",
    "https://[::1/x", None, 12, ["x"],
])
def test_normalise_issue_url_refuses_what_is_not_an_issue_url(raw):
    with pytest.raises(ValueError):
        ts.normalise_issue_url(raw)


@pytest.mark.parametrize("url, label", [
    ("https://git.example.com/acme/widgets/-/issues/665", "#665"),
    ("https://git.example.com/acme/widgets/-/work_items/665", "#665"),
    ("https://git.example.com/acme/widgets/-/merge_requests/439", "!439"),
    ("https://github.com/acme/widgets/issues/12", "#12"),
    ("https://github.com/acme/widgets/pull/9", "#9"),
    ("https://tracker.example/t/86c1x2y3z", "https://tracker.example/t/86c1x2y3z"),
])
def test_issue_label(url, label):
    assert ts.issue_label(url) == label


def test_the_run_key_grammar_is_lmers_and_the_webs():
    """``lmer_timesheet`` spells the run key and uid itself, since it imports no
    lmer module. If lmer widened its grammar alone, it would compose keys that
    ``SessionRef`` rejects, and entries would stop naming runs with nothing
    failing anywhere."""
    assert entry_module._RUN_KEY_RE.pattern == rf"{runref.SEGMENT}(?:/{runref.SEGMENT}){{2,}}"
    assert rf"^{entry_module._UID_RE.pattern}\Z" == runs.UID_RE.pattern

    script = (Path(__file__).resolve().parent.parent / "web" / "src" / "runref.js").read_text(
        encoding="utf-8")
    run_ref = re.search(r"^export const RUN_REF_RE = /\^lmer:\\/\\/run\\/(.+)\$/$", script, re.M)
    uid = re.search(r"^const UID_RE = /\^(.+)\$/$", script, re.M)
    assert run_ref and uid, "runref.js no longer spells RUN_REF_RE or UID_RE on one line"
    assert run_ref.group(1).replace("\\/", "/") == entry_module._RUN_KEY_RE.pattern
    assert uid.group(1) == entry_module._UID_RE.pattern


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------


def test_entry_path_layout_uses_the_utc_month_of_start():
    # 00:30 on 1 November at +02:00 is still October in UTC.
    local = timezone(timedelta(hours=2))
    start = datetime(2026, 11, 1, 0, 30, tzinfo=local)
    entry = _entry(start=start, end=start + timedelta(minutes=5))
    assert ts.entry_path(entry) == PurePosixPath(
        "timesheet.d/alex/2026/10/20261031T223000Z--webshop--01JB0000000000000000000000.yaml"
    )


@pytest.mark.parametrize(
    "override",
    [
        {"person": "../alex"},
        {"person": "Alex"},
        {"person": "a/b"},
        {"person": "runs"},
        {"person": "info"},
        {"person": "memory"},
        {"person": "specs"},
        {"person": "_lmer"},
        {"person": ".sync"},
        {"person": ""},
        {"project": "../../etc"},
        {"project": "web/shop"},
        {"project": "web--shop"},
        {"id": "../x"},
        {"id": "a/b"},
        {"id": ".hidden"},
    ],
)
def test_entry_path_refuses_keys_that_escape_their_directory(override):
    with pytest.raises(ValueError):
        ts.entry_path(_entry(**override))


def test_slug_rules():
    assert ts.is_slug("docs-site")
    assert ts.is_slug("a.b_c-1")
    assert not ts.is_slug("-lead")
    assert not ts.is_slug(None)
    assert ts.is_person_key("alex")
    assert not ts.is_person_key("runs")
    assert ts.is_project_key("docs-site")
    assert ts.is_project_key("web-")
    assert not ts.is_project_key("web--shop")
    assert not ts.is_project_key("../x")
    assert not ts.is_project_key(None)


def test_entry_file_name_splits_back_into_its_project_and_id():
    # The project is everything up to the first "--" after the start, so an id
    # may contain "--" but a project key may not.
    name_re = re.compile(
        r"^(?P<start>\d{8}T\d{6}Z)--(?P<project>[^/]+?)--(?P<id>[A-Za-z0-9][A-Za-z0-9._-]*)\.yaml$"
    )
    for project, entry_id in [("web", "a--b"), ("web-", "x"), ("a.b_c-1", "01JB--2")]:
        name = ts.entry_path(_entry(project=project, id=entry_id)).name
        match = name_re.match(name)
        assert (match["project"], match["id"]) == (project, entry_id), name


# ---------------------------------------------------------------------------
# ids
# ---------------------------------------------------------------------------


def test_new_entry_id_is_a_ulid():
    value = ts.new_entry_id(at=START)
    assert len(value) == 26
    assert set(value) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    assert ts.is_entry_id(value)


def test_new_entry_ids_sort_by_time():
    times = [START + timedelta(milliseconds=n) for n in (0, 1, 2, 1000, 86_400_000)]
    ids = [ts.new_entry_id(at=moment) for moment in times]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_new_entry_ids_in_one_millisecond_differ():
    ids = {ts.new_entry_id(at=START) for _ in range(50)}
    assert len(ids) == 50
    assert len({value[:10] for value in ids}) == 1


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


def test_parse_fixture_handler():
    handler, problems = ts.parse_handler(FIXTURE_HANDLER)
    assert problems == []
    assert handler.name == "tracker-x"
    assert handler.label == "Tracker X"
    assert handler.sync is True
    task = handler.get_field("task_id")
    assert task == ts.HandlerField(
        key="task_id",
        label="Tracker X task",
        required=True,
        pattern="[0-9a-z]{9}",
        hint="Paste the task id from Tracker X",
        link="https://tracker-x.example/t/{value}",
    )
    assert task.link_for("86c1x2y3z") == "https://tracker-x.example/t/86c1x2y3z"
    assert task.link_for("a/b") == "https://tracker-x.example/t/a%2Fb"
    note = handler.get_field("note")
    assert (note.required, note.pattern, note.link_for("x")) == (False, None, None)


def test_builtin_timesheet_handler():
    assert ts.TIMESHEET.name == "timesheet"
    assert ts.TIMESHEET.fields == ()
    assert ts.TIMESHEET.sync is False
    assert ts.validate_fields(ts.TIMESHEET, {}) == []


@pytest.mark.parametrize(
    "text, expected",
    [
        ("name: x\nfields:\n  - label: no key\n", "fields[0].key"),
        ("name: x\nfields:\n  - key: a\n  - key: a\n", "fields.a: defined twice"),
        ("name: x\nfields:\n  - key: a\n    pattern: '('\n", "fields.a.pattern: not a regex"),
        ("name: x\nfields:\n  - key: a\n    link: 'javascript:{value}'\n", "fields.a.link: must be an http(s)"),
        ("name: x\nfields:\n  - key: a\n    required: yes please\n", "fields.a.required"),
        ("name: x\nsync: maybe\n", "sync: must be true or false"),
        ("name: Bad Name\n", "name: 'Bad Name'"),
        ("name: x\nfields: {a: 1}\n", "fields: must be a list"),
    ],
)
def test_broken_handler_definitions_are_refused(text, expected):
    handler, problems = ts.parse_handler(text)
    assert handler is None
    assert any(expected in problem for problem in problems), problems


def test_handler_name_follows_the_file_name():
    handler, problems = ts.parse_handler("label: Y\n", path="timesheet.d/.handlers/y.yaml")
    assert problems == [] and handler.name == "y" and handler.label == "Y"
    handler, problems = ts.parse_handler("name: z\n", path="timesheet.d/.handlers/y.yaml")
    assert handler is None
    assert problems == ["name: 'z' does not match the file name 'y'"]


def test_load_handlers_from_a_work_repo(tmp_path):
    directory = tmp_path / "timesheet.d" / ".handlers"
    directory.mkdir(parents=True)
    (directory / "tracker-x.yaml").write_text(FIXTURE_HANDLER)
    (directory / "broken.yaml").write_text("name: broken\nfields: [{key: a, pattern: '('}]\n")
    (directory / "timesheet.yaml").write_text("name: timesheet\nfields: [{key: a, required: true}]\n")
    (directory / "notes.txt").write_text("ignored")

    handlers, problems = ts.load_handlers(tmp_path)

    assert sorted(handlers) == ["timesheet", "tracker-x"]
    assert handlers["timesheet"] is ts.TIMESHEET
    assert handlers["tracker-x"].get_field("task_id").required is True
    assert len(problems) == 2
    assert problems[0].startswith("timesheet.d/.handlers/broken.yaml: fields.a.pattern: not a regex")
    assert problems[1] == (
        "timesheet.d/.handlers/timesheet.yaml: cannot override the built-in timesheet handler"
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        ("name: x\nfields:\n  - key: a\n    link: 'https://['\n", "fields.a.link: must be an http(s)"),
        ("name: x\nfields:\n  - key: a\n    pattern: '" + "(" * 1000 + ")" * 1000 + "'\n",
         "fields.a.pattern: not a regex"),
        ("name: x\nfields:\n  - key: a\n    pattern: 'a{99999999999}'\n", "fields.a.pattern: not a regex"),
        ("name: [" + "[" * 5000 + "]" * 5000 + "]\n", "not valid YAML"),
        ("name: x\nsync: " + "1" * 5000 + "\n", "not valid YAML"),
    ],
    ids=["unparsable-link", "deep-pattern", "huge-repeat", "deep-nesting", "huge-int"],
)
def test_hostile_handler_definitions_are_problems_not_exceptions(text, expected):
    handler, problems = ts.parse_handler(text)
    assert handler is None
    assert any(expected in problem for problem in problems), problems
    assert all(len(problem) < 300 for problem in problems), [p[:300] for p in problems]


def test_handler_aliases_are_refused_and_containers_named_by_type():
    anchors = _alias_bomb("note: x").split("id: a")[0]
    began = time.monotonic()
    handler, problems = ts.parse_handler(anchors + "name: *l7\n")
    assert time.monotonic() - began < 1.0
    assert handler is None and problems[0].startswith("not valid YAML: aliases are not allowed")
    handler, problems = ts.parse_handler("name: [a, b]\n")
    assert handler is None and "name: a list is not a handler name" in problems
    handler, problems = ts.parse_handler("name: x\nfields:\n  - key: {a: b}\n")
    assert handler is None and problems == ["fields[0].key: a dict is not a field key"]


def test_one_hostile_handler_does_not_stop_the_others_loading(tmp_path):
    directory = tmp_path / "timesheet.d" / ".handlers"
    directory.mkdir(parents=True)
    (directory / "tracker-x.yaml").write_text(FIXTURE_HANDLER)
    (directory / "bad-link.yaml").write_text("fields:\n  - key: a\n    link: 'https://['\n")
    (directory / "bad-regex.yaml").write_text("fields:\n  - key: a\n    pattern: 'a{99999999999}'\n")
    (directory / "deep.yaml").write_text("name: " + "[" * 5000 + "]" * 5000 + "\n")
    (directory / "merge.yaml").write_text(_merge_bomb())

    began = time.monotonic()
    handlers, problems = ts.load_handlers(tmp_path)
    assert time.monotonic() - began < 5.0

    assert sorted(handlers) == ["timesheet", "tracker-x"]
    assert [problem.split(":", 1)[0] for problem in problems] == [
        "timesheet.d/.handlers/bad-link.yaml",
        "timesheet.d/.handlers/bad-regex.yaml",
        "timesheet.d/.handlers/deep.yaml",
        "timesheet.d/.handlers/merge.yaml",
    ]
    assert all(len(problem) < 300 for problem in problems), [p[:300] for p in problems]


def test_load_handlers_without_a_handlers_dir(tmp_path):
    assert ts.load_handlers(tmp_path) == ({"timesheet": ts.TIMESHEET}, [])


PER_ISSUE_HANDLER = textwrap.dedent(
    """\
    name: tracker-x
    fields:
      - key: task_id
        label: Tracker X task
        required: true
        pattern: '[0-9a-z]{9}'
        per_issue: true
      - key: note
        label: Note
    """
)


def test_per_issue_parses_and_defaults_to_false():
    handler, problems = ts.parse_handler(PER_ISSUE_HANDLER)
    assert problems == []
    assert handler.get_field("task_id").per_issue is True
    assert handler.get_field("note").per_issue is False
    handler, problems = ts.parse_handler(FIXTURE_HANDLER)
    assert problems == [] and handler.get_field("task_id").per_issue is False


def test_a_per_issue_that_is_not_a_bool_makes_the_handler_unusable():
    handler, problems = ts.parse_handler(
        "name: x\nfields:\n  - key: a\n    per_issue: 'yes'\n")
    assert handler is None
    assert problems == ["fields.a.per_issue: must be true or false"]


def test_a_parser_without_per_issue_keeps_the_handler(monkeypatch):
    # A sync process built against !439 reads a #665 definition.
    monkeypatch.setattr(handlers_module, "_FIELD_KEYS",
                        tuple(k for k in handlers_module._FIELD_KEYS if k != "per_issue"))
    handler, problems = ts.parse_handler(PER_ISSUE_HANDLER)
    assert handler is not None and handler.get_field("task_id").required is True
    assert problems == ["fields[0].per_issue: unknown key"]


def _handler(text=PER_ISSUE_HANDLER):
    handler, problems = ts.parse_handler(text)
    assert problems == []
    return handler


def test_issue_map_path():
    assert ts.issue_map_path("tracker-x") == PurePosixPath(
        "timesheet.d/.handlers/tracker-x/issues.yaml")
    assert ts.ISSUE_MAP_NAME == "issues.yaml"


def test_parse_a_good_issue_map_normalising_its_keys():
    text = textwrap.dedent(
        f"""\
        {ISSUE}:
          task_id: 86c1x2y3z
          note: triage
        https://git.example.com/acme/widgets/-/work_items/13/:
          task_id: 86c1x2y30
        {MR}: {{}}
        """
    )
    issues, problems = ts.parse_issue_map(text, _handler())
    assert problems == []
    assert issues == {
        ISSUE: {"task_id": "86c1x2y3z", "note": "triage"},
        "https://git.example.com/acme/widgets/-/issues/13": {"task_id": "86c1x2y30"},
        MR: {},
    }


@pytest.mark.parametrize("text, expected", [
    ("a: [", "not valid YAML"),
    ("- a\n- b\n", "not a mapping, but a list"),
    ("just text\n", "not a mapping, but 'just text'"),
    ("a: &x {task_id: y}\nb: *x\n", "not valid YAML: aliases are not allowed"),
])
def test_a_malformed_issue_map_is_one_problem_and_no_values(text, expected):
    issues, problems = ts.parse_issue_map(text, _handler())
    assert issues == {}
    assert len(problems) == 1 and problems[0].startswith(expected), problems


def test_an_empty_issue_map_is_no_values_and_no_problem():
    assert ts.parse_issue_map("", _handler()) == ({}, [])


def test_bad_issue_map_entries_are_skipped_with_a_warning():
    text = textwrap.dedent(
        f"""\
        not-a-url:
          task_id: 86c1x2y3z
        {ISSUE}:
          task_id: 86c1x2y3z
          colour: blue
          note: 7
        {ISSUE}/:
          task_id: 86c1x2y30
        {MR}: a-string
        https://git.example.com/acme/widgets/-/issues/14:
          task_id: TOO-LONG-AND-UPPER
          note: [a, b]
        """
    )
    issues, problems = ts.parse_issue_map(text, _handler())
    assert issues == {
        ISSUE: {"task_id": "86c1x2y3z", "note": "7"},
        "https://git.example.com/acme/widgets/-/issues/14": {},
    }
    assert problems == [
        "'not-a-url': 'not-a-url' is not an http(s) issue URL",
        f"{ISSUE}: 'colour': not a field of tracker-x",
        f"{ISSUE}: a repeat of an earlier key",
        f"{MR}: must be a mapping of field values, not 'a-string'",
        "https://git.example.com/acme/widgets/-/issues/14: 'task_id': "
        "'TOO-LONG-AND-UPPER' does not match [0-9a-z]{9}",
        "https://git.example.com/acme/widgets/-/issues/14: 'note': must be a string, not a list",
    ]


def test_load_issue_map(tmp_path):
    handler = _handler()
    assert ts.load_issue_map(tmp_path, handler) == ({}, [])
    directory = tmp_path / "timesheet.d" / ".handlers" / "tracker-x"
    directory.mkdir(parents=True)
    (directory / "issues.yaml").write_text(f"{ISSUE}:\n  task_id: 86c1x2y3z\n{MR}: 3\n")
    issues, problems = ts.load_issue_map(tmp_path, handler)
    assert issues == {ISSUE: {"task_id": "86c1x2y3z"}}
    assert problems == [
        f"timesheet.d/.handlers/tracker-x/issues.yaml: {MR}: "
        "must be a mapping of field values, not 3"
    ]
    (directory / "issues.yaml").write_bytes(b"\xff\xfe")
    issues, problems = ts.load_issue_map(tmp_path, handler)
    assert issues == {} and problems[0].startswith(
        "timesheet.d/.handlers/tracker-x/issues.yaml: unreadable")


def test_load_handlers_never_reads_an_issue_map_as_a_handler(tmp_path):
    directory = tmp_path / "timesheet.d" / ".handlers"
    (directory / "tracker-x").mkdir(parents=True)
    (directory / "tracker-x.yaml").write_text(PER_ISSUE_HANDLER)
    (directory / "tracker-x" / "issues.yaml").write_text(f"{ISSUE}:\n  task_id: 86c1x2y3z\n")
    handlers, problems = ts.load_handlers(tmp_path)
    assert sorted(handlers) == ["timesheet", "tracker-x"]
    assert problems == []


def test_validate_fields_reports_missing_and_mismatched_values():
    handler, _ = ts.parse_handler(FIXTURE_HANDLER)
    assert ts.validate_fields(handler, {"task_id": "86c1x2y3z"}) == []
    assert ts.validate_fields(handler, {}) == ["task_id: required (Tracker X task)"]
    assert ts.validate_fields(handler, None) == ["task_id: required (Tracker X task)"]
    assert ts.validate_fields(handler, {"task_id": ""}) == ["task_id: required (Tracker X task)"]
    assert ts.validate_fields(handler, {"task_id": "86c1x2y3z-extra"}) == [
        "task_id: '86c1x2y3z-extra' does not match [0-9a-z]{9}"
    ]


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------


def _line(**overrides):
    values = dict(id="e1", hash="sha256:00", status="synced", at=END)
    values.update(overrides)
    return ts.LedgerLine(**values)


def test_ledger_path():
    assert ts.ledger_path("alex", "tracker-x") == PurePosixPath(
        "timesheet.d/alex/.sync/tracker-x.jsonl"
    )
    with pytest.raises(ValueError):
        ts.ledger_path("../alex", "tracker-x")
    with pytest.raises(ValueError):
        ts.ledger_path("alex", "../x")


def test_entry_hash_is_sha256_of_raw_bytes():
    assert ts.entry_hash(b"") == (
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_ledger_line_round_trip():
    line = _line(ref="ext-1", link="https://tracker-x.example/e/1")
    text = line.to_json()
    assert json.loads(text) == {
        "id": "e1",
        "hash": "sha256:00",
        "status": "synced",
        "ref": "ext-1",
        "link": "https://tracker-x.example/e/1",
        "at": "2026-10-27T09:40:00Z",
    }
    assert ts.parse_ledger(text + "\n") == ([line], [])


def test_parse_ledger_skips_and_reports_bad_lines():
    good = _line().to_json()
    held = _line(id="e2", status="held", reason="no identity for alex").to_json()
    text = "\n".join(
        [
            good,
            "{not json",
            "",
            '{"id": "e3", "hash": "sha256:00", "status": "lost", "at": "2026-10-27T09:40:00Z"}',
            '{"id": "e4", "hash": "sha256:00", "status": "synced"}',
            '["a list"]',
            held,
        ]
    )
    lines, problems = ts.parse_ledger(text)
    assert [line.id for line in lines] == ["e1", "e2"]
    assert lines[1].reason == "no identity for alex"
    assert [problem.split(":", 1)[0] for problem in problems] == [
        "line 2", "line 4", "line 5", "line 6",
    ]
    assert "status: 'lost'" in problems[1]
    assert "at: required string" in problems[2]


@pytest.mark.parametrize(
    "link",
    ["javascript:alert(document.cookie)", " JavaScript:alert(1)", "java\tscript:alert(1)",
     "data:text/html,x", "//evil.example/x", "https://["],
)
def test_ledger_link_must_be_http_or_https(link):
    text = _line(link=link).to_json()
    lines, problems = ts.parse_ledger(text)
    assert lines == []
    assert problems == ["line 1: link: must be an http(s) URL"]


def test_ledger_accepts_http_and_https_links():
    lines = [_line(id=f"e{n}", link=link) for n, link in
             enumerate(["https://tracker-x.example/e/1", "HTTP://tracker-x.example/e/2"])]
    assert ts.parse_ledger("\n".join(line.to_json() for line in lines)) == (lines, [])


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_ledger_splits_on_newline_only(separator):
    held = _line(status="held", reason=f"no identity{separator}for alex")
    text = held.to_json() + "\r\n" + _line(id="e2").to_json() + "\n"
    assert separator in text
    lines, problems = ts.parse_ledger(text)
    assert problems == []
    assert [line.id for line in lines] == ["e1", "e2"]
    assert lines[0].reason == f"no identity{separator}for alex"


def test_hostile_ledger_lines_are_problems_not_exceptions():
    text = "\n".join([
        "[" * 100000 + "]" * 100000,
        '{"id": "e1", "hash": "sha256:00", "status": "synced", "ref": ' + "1" * 5000 + "}",
        '{"id": "e1", "hash": "sha256:00", "status": "synced", "at": "9999-12-31T23:00:00-05:00"}',
        _line(id="e2").to_json(),
    ])
    lines, problems = ts.parse_ledger(text)
    assert [line.id for line in lines] == ["e2"]
    assert [problem.split(":", 1)[0] for problem in problems] == ["line 1", "line 2", "line 3"]
    assert "at: " in problems[2] and "out of range" in problems[2]
    assert all(len(problem) < 300 for problem in problems), [p[:300] for p in problems]


def test_sync_status():
    raw = _entry().to_yaml().encode("utf-8")
    current = ts.entry_hash(raw)
    entry = _entry()

    assert ts.sync_status(entry, [], raw=raw) == ("pending", None)

    synced = _line(id=entry.id, hash=current, link="https://tracker-x.example/e/1")
    assert ts.sync_status(entry, [synced], raw=raw) == ("synced", synced)
    assert ts.sync_status(entry.id, [synced], raw=raw) == ("synced", synced)

    held = _line(id=entry.id, hash=current, status="held", reason="no identity")
    assert ts.sync_status(entry, [synced, held], raw=raw) == ("held", held)
    assert ts.sync_status(entry, [held, synced], raw=raw) == ("synced", synced)

    other = _line(id="someone-else", hash=current)
    assert ts.sync_status(entry, [other], raw=raw) == ("pending", None)


def test_hash_mismatch_is_pending():
    entry = _entry()
    raw = entry.to_yaml().encode("utf-8")
    synced = _line(id=entry.id, hash=ts.entry_hash(raw))
    edited = _entry(title="Checkout bug fix").to_yaml().encode("utf-8")
    assert ts.sync_status(entry, [synced], raw=edited) == ("pending", synced)


# ---------------------------------------------------------------------------
# package
# ---------------------------------------------------------------------------


def test_package_imports_only_stdlib_and_yaml():
    # Baseline on `import yaml`, so PyYAML's own C-extension modules don't count.
    code = (
        "import sys, yaml\n"
        "before = {m.split('.')[0] for m in sys.modules}\n"
        "import lmer_timesheet\n"
        "after = {m.split('.')[0] for m in sys.modules}\n"
        "print('\\n'.join(sorted(after - before - set(sys.stdlib_module_names))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.split() == ["lmer_timesheet"]


def test_all_names_resolve():
    for name in ts.__all__:
        assert hasattr(ts, name), name
