"""``timesheet.d/`` is a known work-repo path, never a host bucket.

The schema reads a dotted top-level directory as a host bucket: the unnamed
work of a repo on that host (SCHEMA.md "Host buckets"). ``timesheet.d/`` has a
dot and is the time log of record (docs/TIME-TRACKING.md), so the validator
has to know the name: nothing below it is a project, whatever it holds.

The fixtures are written with :mod:`lmer_timesheet` itself, so the tree is the
one the timesheet writer and a sync process really produce.
"""
import json
from datetime import datetime, timezone

import pytest

from lmer_schema.checks import walk
from lmer_schema.checks.runner import validate
from lmer_schema import registry
from lmer_timesheet import (
    HANDLERS_DIR,
    TIMESHEET_DIR,
    Entry,
    LedgerLine,
    Trigger,
    entry_hash,
    entry_path,
    ledger_path,
    new_entry_id,
)

HANDLER = """\
name: tracker
label: Example tracker
fields:
  - key: task_id
    label: Tracker task
    required: true
    pattern: "[a-z0-9]{6,12}"
    link: https://tracker.example.com/t/{value}
sync: true
"""


def _entry(person, project, start):
    return Entry(
        id=new_entry_id(at=start), person=person, project=project, handler="tracker",
        fields={"task_id": "abc123"}, client="acme", title="Checkout triage",
        start=start, duration=1800, trigger=Trigger(start="activity", stop="idle"),
    )


def _timesheet(root):
    """A ``timesheet.d/`` holding entries, a handler definition and a ledger."""
    start = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
    written = []
    for person, project in (("alice", "webshop"), ("bob", "docs-site")):
        entry = _entry(person, project, start)
        path = root / entry_path(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(entry.to_yaml(), encoding="utf-8")
        written.append((entry, path))
    handlers = root / HANDLERS_DIR
    handlers.mkdir(parents=True)
    (handlers / "tracker.yaml").write_text(HANDLER, encoding="utf-8")
    entry, path = written[0]
    line = LedgerLine(
        id=entry.id, hash=entry_hash(path.read_bytes()), status="synced", ref="9876",
        link="https://tracker.example.com/entries/9876",
        at=datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc),
    )
    ledger = root / ledger_path("alice", "tracker")
    ledger.parent.mkdir(parents=True)
    ledger.write_text(line.to_json() + "\n", encoding="utf-8")
    hand = root / TIMESHEET_DIR / "bob" / "2026" / "10" / "20261027T120000Z--docs-site--hand-1.yaml"
    hand.write_text(
        "id: hand-1\nperson: bob\nproject: docs-site\n"
        "start: 2026-10-27T14:00:00+02:00\nduration: 900\n",
        encoding="utf-8",
    )


@pytest.fixture
def schema_root(tmp_path):
    root = tmp_path / "plexus"
    root.mkdir()
    (root / "_lmer.yaml").write_text(
        "schema: 2\nname: example\nprojects:\n  widgets:\n    description: d\n",
        encoding="utf-8",
    )
    (root / "widgets").mkdir()
    _timesheet(root)
    return root


@pytest.fixture
def legacy_root(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    _timesheet(root)
    return root


def _findings(root):
    report = validate(root)
    return report, [(finding.code, finding.path) for finding in report.findings]


def test_the_name_is_registered_and_is_not_a_host():
    assert TIMESHEET_DIR in registry.WORK_REPO_DATA_DIRS
    assert not registry.is_host_bucket_name(TIMESHEET_DIR)
    assert registry.is_host_bucket_name("git.example.com"), "a real host is still a bucket"


def test_a_timesheet_tree_validates_clean(schema_root):
    report, findings = _findings(schema_root)

    assert findings == []
    assert report.exit_code == 0
    assert (schema_root / TIMESHEET_DIR / "alice" / ".sync" / "tracker.jsonl").is_file()


@pytest.mark.parametrize("planted", ["alice/info", "runs", "bob/2026/memory"])
@pytest.mark.parametrize("form", ["schema", "legacy"])
def test_nothing_below_it_is_a_project_whatever_it_holds(form, planted, request):
    """Before ``timesheet.d`` was known, a directory below it that passes the
    §1 marker test (a hand-made ``info/`` notes directory, say) made the tree
    a host bucket: ``tree.host-bucket`` in schema form, a legacy project home
    in a legacy tree, and its contents checked as a project's."""
    root = request.getfixturevalue(f"{form}_root")
    (root / TIMESHEET_DIR / planted).mkdir(parents=True)

    _report, findings = _findings(root)

    assert [item for item in findings if item[1].startswith(TIMESHEET_DIR)] == []
    assert [project.rel for project in walk.project_dirs(root)
            if project.rel.startswith(TIMESHEET_DIR)] == []


def test_the_ledger_line_in_the_fixture_is_the_contract(schema_root):
    """The fixture is the real thing, not a stand-in: its ledger line parses
    back as D20's line and its hash is the entry file's bytes."""
    ledger = schema_root / ledger_path("alice", "tracker")
    line = json.loads(ledger.read_text(encoding="utf-8"))
    entries = sorted((schema_root / TIMESHEET_DIR / "alice").rglob("*.yaml"))

    assert line["status"] == "synced"
    assert line["hash"] == entry_hash(entries[0].read_bytes())
