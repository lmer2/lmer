"""The session verbs in the work CLI (issue #353 D22).

The session is the unit of record: in schema form one launch owns
`<project>/_runs/<slug>/<session-id>[--<name>]/` and `_runs/<slug>/` only
groups them, so three questions the CLI used to answer for a whole run now
have to be answered for one launch — and a legacy tree, which keeps one
shared run dir per target, has to keep answering them exactly as it does
today. Four behaviours, one class each:

- **`work sessions` answers "what is running" in both forms.** The rows come
  from `run_state.list_sessions` (id, slug, name, status, phase,
  created/updated, the `orphaned` verdict); this module only pins what the
  verb does with them — `--running` is in-progress AND not orphaned, an
  orphan is listed without the flag and marked, an unreadable record is
  listed rather than dropped, and a legacy run dir is a row with no session
  id because several sessions interleave in it.
- **`work name` names the LAUNCH.** Its uniqueness scan is the whole
  project's records, not the naming run's siblings — in schema form a
  record's siblings are the other launches of its own group, so a sibling
  scan would let two runs of one project hold one name. The conflict tests
  put the holder in ANOTHER group on purpose: a sibling-only scan passes
  them all.
- **A resume continues one session, and the lineage routes the rest.** One
  `work session-start --session <id>` is the whole handshake; the later
  invocations of that launch carry only their own `LMER_SESSION_ID` and
  still land in the same dir. An id that cannot name a directory is refused
  by the kernel and surfaced here as a message — never a traceback, never a
  record at the grouping level D22 keeps empty.
- **An `--answer` relaunch addresses the session that asked.** The answer
  belongs to the question, and the question was asked by one launch; the
  relaunch is a new container with a new harness id, so without this it
  would open a fresh record, apply the answer there and leave the real
  question standing. The tests assert the group gains NO third directory.

Plus the refusal itself: with an unreadable manifest a mutating verb now
says so instead of blaming unset environment variables — and a broken
REGISTRY, which still resolves a base (the host bucket, D9), is never
mistaken for either.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from lmer_cli.container import clone_and_exec
from tests.conftest import strip_lmer_env
from tests.schema2_fixtures import RecordSpec, build_schema2_plexus, make_env, stamp, uid_for
from tests.plexus_fixtures import (
    PRIMARY_REPO,
    PROJECT,
    SESSION_IDS,
    SLUG,
    build_legacy_plexus,
    build_schema_plexus,
)
from work_repo import cli as work_cli
from work_repo import run_state, utils

#: The harness id a relaunch (a resume, an answer) arrives with — never one
#: of the fixture tree's own two launches.
SID_RESUMED = "20260825-090000-3-33333"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """No ambient run context, no announcement and no synthesized id carried
    between tests."""
    strip_lmer_env(monkeypatch)
    utils._ANNOUNCED_MANIFEST_PROBLEMS.clear()
    run_state._reset_synthesized_session_id()


@pytest.fixture(autouse=True)
def _tmp_answer_markers(monkeypatch, tmp_path):
    """Keep the LMER_ANSWER consume-once markers out of the real /tmp — a
    marker left there by one run would make the next one skip the apply."""
    markers = tmp_path / "markers"
    markers.mkdir(exist_ok=True)
    monkeypatch.setattr(work_cli, "ANSWER_MARKER_DIR", str(markers))


def _main(argv):
    with patch.object(work_cli.sys, "argv", ["work"] + argv):
        return work_cli.main()


def _set_context(monkeypatch, root, *, host=None, project=None, session=None):
    """Point the resolver at *root* with the launch context a session has."""
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(root))
    monkeypatch.setenv("LMER_TASK", "develop")
    monkeypatch.setenv("LMER_TASK_TARGET", f"https://{PRIMARY_REPO}/-/issues/12")
    for name, value in (
        ("LMER_REPO_HOST", host),
        ("LMER_REPO_PROJECT", project),
        ("LMER_SESSION_ID", session),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _launch(monkeypatch, tree, session=None):
    """The launch context for the fixture tree's registered project."""
    host, path = PRIMARY_REPO.split("/", 1)
    _set_context(monkeypatch, tree.root, host=host, project=path, session=session)


@pytest.fixture
def schema_tree(tmp_path):
    return build_schema_plexus(tmp_path / "plexus-schema")


@pytest.fixture
def legacy_tree(tmp_path):
    return build_legacy_plexus(tmp_path / "plexus-legacy")


def _bare_schema_tree(monkeypatch, tmp_path, session=None, name="plexus-bare"):
    """A schema tree whose registered project has no run records yet."""
    root = tmp_path / name
    root.mkdir()
    (root / "_lmer.yaml").write_text(
        f"schema: 1\nname: example\nprojects:\n  {PROJECT}:\n"
        f"    repo: {PRIMARY_REPO}\n",
        encoding="utf-8",
    )
    host, path = PRIMARY_REPO.split("/", 1)
    _set_context(monkeypatch, root, host=host, project=path, session=session)
    return root


def _write_record(rdir, slug=SLUG, **fields):
    """A minimal run record — enough for the resolvers and the listing."""
    state = {
        "schema": 1,
        "slug": slug,
        "name": None,
        "taskdef": "develop",
        "target": "",
        "status": "in-progress",
        "phase": None,
        "stop_reason": None,
        "open_question": None,
        "goal": None,
        "owner": None,
        "created": "2026-08-25T08:00:00Z",
        "updated": "2026-08-25T08:00:00Z",
    }
    state.update(fields)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / run_state.STATE_FILE).write_text(yaml.safe_dump(state), encoding="utf-8")
    return rdir


def _state_of(rdir):
    return yaml.safe_load((rdir / run_state.STATE_FILE).read_text(encoding="utf-8"))


def _event_types(rdir):
    return [event["type"] for event in run_state.read_events(rdir, last_n=0)]


def _dirs_in(path):
    return sorted(child.name for child in path.iterdir() if child.is_dir())


# ---------------------------------------------------------------------------
# `work sessions`
# ---------------------------------------------------------------------------


class TestSessionsListsEveryLaunch:
    """One row per record, in start order — the operator's answer to "what is
    running", built on `run_state.list_sessions` and nothing re-derived."""

    def test_a_row_per_launch_in_start_order(self, monkeypatch, schema_tree, capsys):
        _launch(monkeypatch, schema_tree, session=SID_RESUMED)
        assert _main(["sessions"]) == 0
        out = capsys.readouterr().out
        body = out.splitlines()[1:]
        assert [line.split()[1] for line in body] == list(SESSION_IDS)
        assert all(line.split()[0] == SLUG for line in body)

    def test_the_row_carries_what_the_operator_decides_on(
        self, monkeypatch, schema_tree, capsys
    ):
        _launch(monkeypatch, schema_tree, session=SID_RESUMED)
        _main(["sessions"])
        first, second = capsys.readouterr().out.splitlines()[1:]
        assert "complete" in first
        assert "phase=implement" in first
        assert "created=2026-08-24T10:15:00Z" in first
        assert "updated=2026-08-24T10:41:00Z" in first
        assert "name=second-look" in second
        assert "in-progress" in second

    def test_an_orphan_is_listed_and_marked(self, monkeypatch, schema_tree, capsys):
        """In-progress with no live owner: resumable, never auto-ended, and
        never hidden — a crashed session unlisted reads as a finished one."""
        _launch(monkeypatch, schema_tree, session=SID_RESUMED)
        _main(["sessions"])
        out = capsys.readouterr().out
        assert "1 orphaned" in out.splitlines()[0]
        assert "orphaned" in out.splitlines()[2]

    def test_running_is_in_progress_with_something_live_in_it(
        self, monkeypatch, schema_tree, capsys
    ):
        _launch(monkeypatch, schema_tree, session=SID_RESUMED)
        assert _main(["sessions", "--running"]) == 0
        # The fixture's in-progress launch holds no owner claim — orphaned,
        # so nothing is running even though a record is open.
        assert capsys.readouterr().out.strip() == "No sessions running"

        live = schema_tree.session_dirs[1]
        state = _state_of(live)
        state["owner"] = {
            "session_id": SESSION_IDS[1],
            "claimed_at": run_state.utc_now_iso(),
        }
        run_state.write_state(live, state)
        assert _main(["sessions", "--running"]) == 0
        out = capsys.readouterr().out
        assert out.splitlines()[0] == "Running sessions: 1"
        assert SESSION_IDS[1] in out
        assert SESSION_IDS[0] not in out  # complete is not running

    def test_an_unreadable_record_is_listed_without_a_verdict(
        self, monkeypatch, tmp_path, capsys
    ):
        root = _bare_schema_tree(monkeypatch, tmp_path, session=SID_RESUMED)
        broken = run_state.runs_base() / SLUG / SESSION_IDS[0]
        broken.mkdir(parents=True)
        (broken / run_state.STATE_FILE).write_text("{[bad", encoding="utf-8")
        assert _main(["sessions"]) == 0
        out = capsys.readouterr().out
        assert SESSION_IDS[0] in out
        assert "state unreadable" in out
        assert root.exists()
        # No verdict is not a verdict of "fine": it is never `--running`.
        _main(["sessions", "--running"])
        assert capsys.readouterr().out.strip() == "No sessions running"

    def test_json_carries_the_rows_the_form_and_the_address(
        self, monkeypatch, schema_tree, capsys
    ):
        _launch(monkeypatch, schema_tree, session=SID_RESUMED)
        assert _main(["sessions", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["form"] == "schema"
        assert payload["count"] == 2
        first, second = payload["sessions"]
        assert first["id"] == SESSION_IDS[0]
        assert first["slug"] == SLUG
        assert first["status"] == "complete"
        assert first["path"] == str(schema_tree.session_dirs[0])
        # Run-area relative, the spelling the movers report: a bare session
        # id names a launch without saying which run it belongs to.
        assert second["address"] == f"{SLUG}/{schema_tree.session_dirs[1].name}"
        assert second["orphaned"] is True

    def test_a_state_json_cannot_represent_is_an_unreadable_record(
        self, monkeypatch, tmp_path, capsys
    ):
        """`--json` never exits 1 over one record — the contract this verb
        already publishes for a record it cannot read.

        An unquoted ISO date in a hand-edited `state.yaml` is a YAML-native
        `datetime`, which `json.dumps` refuses; any of `created`, `updated`,
        `frozen` or `owner.claimed_at` will do it. A record whose state
        cannot be REPRESENTED is an unreadable record, so it lists with None
        fields like any other — and the readable rows keep their own values,
        which a blanket coercion over the whole document would not leave
        true. Both halves of the verb are driven here so the text and the
        JSON listing can never disagree about which records exist.
        """
        _bare_schema_tree(monkeypatch, tmp_path, session=SID_RESUMED)
        base = run_state.runs_base()
        _write_record(base / SLUG / SESSION_IDS[0], phase="implement")
        native = base / SLUG / SESSION_IDS[1]
        native.mkdir(parents=True)
        (native / run_state.STATE_FILE).write_text(
            f"schema: 1\nslug: {SLUG}\nstatus: in-progress\n"
            "created: 2026-08-25T08:00:00Z\nupdated: 2026-08-25T08:00:00Z\n",
            encoding="utf-8",
        )
        assert _main(["sessions"]) == 0
        out = capsys.readouterr().out
        assert SESSION_IDS[0] in out
        assert SESSION_IDS[1] in out

        assert _main(["sessions", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["count"] == 2
        good, unrepresentable = payload["sessions"]
        assert good["phase"] == "implement"
        assert good["created"] == "2026-08-25T08:00:00Z"
        assert unrepresentable["id"] == SESSION_IDS[1]
        assert unrepresentable["address"] == f"{SLUG}/{SESSION_IDS[1]}"
        assert unrepresentable["status"] is None
        assert unrepresentable["created"] is None
        assert unrepresentable["orphaned"] is None

    def test_it_is_read_only(self, monkeypatch, schema_tree):
        _launch(monkeypatch, schema_tree, session=SID_RESUMED)
        group = schema_tree.runs_dir / SLUG
        before = sorted(path.name for path in group.iterdir())
        assert _main(["sessions"]) == 0
        assert sorted(path.name for path in group.iterdir()) == before

    def test_no_run_context_is_not_a_failure(self, monkeypatch, tmp_path, capsys):
        _set_context(monkeypatch, tmp_path)
        assert _main(["sessions"]) == 0
        out = capsys.readouterr().out
        assert "no run context" in out.lower()
        assert "LMER_REPO_HOST" in out

    def test_no_run_context_still_answers_in_json(self, monkeypatch, tmp_path, capsys):
        _set_context(monkeypatch, tmp_path)
        assert _main(["sessions", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["sessions"] == []
        assert payload["count"] == 0
        assert "LMER_REPO_HOST" in payload["detail"]

    def test_an_empty_run_area_is_a_normal_state(self, monkeypatch, tmp_path, capsys):
        _bare_schema_tree(monkeypatch, tmp_path, session=SID_RESUMED)
        assert _main(["sessions"]) == 0
        assert capsys.readouterr().out.strip() == "No sessions recorded"


class TestALegacyTreeAnswersTheSameQuestion:
    """One verb, both forms. A legacy run dir is not one launch's record —
    several sessions interleave in it — so it lists with no session id."""

    def test_it_lists_the_run_dir_with_no_session_id(
        self, monkeypatch, legacy_tree, capsys
    ):
        _launch(monkeypatch, legacy_tree, session="s-legacy-1")
        assert _main(["sessions"]) == 0
        out = capsys.readouterr().out
        row = out.splitlines()[1].split()
        assert row[0] == SLUG
        assert row[1] == "-"
        assert "in-progress" in out

    def test_the_json_form_says_which_shape_it_read(
        self, monkeypatch, legacy_tree, capsys
    ):
        _launch(monkeypatch, legacy_tree, session="s-legacy-1")
        _main(["sessions", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["form"] == "legacy"
        (row,) = payload["sessions"]
        assert row["id"] is None
        assert row["dir_name"] == SLUG
        assert row["address"] == SLUG


# ---------------------------------------------------------------------------
# `work sessions` on a schema-2 work repo
# ---------------------------------------------------------------------------

#: A clock the header-bucket test hands to `list_sessions`, so the 14-day
#: active/stale split is decided by the test and not by the day it runs.
FIXED_NOW = "2026-09-20T00:00:00Z"

#: The five derived states, one record each, in start order.
STATE_WORDS = ("closed", "waiting on question", "ended, not closed", "stale", "active")


def _one_record_per_state(active_updated=stamp(15), stale_updated=stamp(1)):
    """Five schema-2 records that render the five `work sessions` states.

    `stale_updated` is 19 days before `FIXED_NOW` and `active_updated` five
    days before it; a caller listing under the real clock passes an
    `active_updated` of now.
    """
    return [
        RecordSpec(uid=uid_for(1), slug="chat", started_at=stamp(1), updated=stamp(1, 12),
                   status="complete"),
        RecordSpec(uid=uid_for(2), slug="chat", started_at=stamp(2), updated=stamp(2, 12),
                   stop_reason="question"),
        RecordSpec(uid=uid_for(3), slug="chat", started_at=stamp(3), updated=stamp(3, 12),
                   signed_out=True),
        RecordSpec(uid=uid_for(4), slug="chat", started_at=stamp(4), updated=stale_updated,
                   signed_out=False),
        RecordSpec(uid=uid_for(5), slug="chat", started_at=stamp(5), updated=active_updated,
                   signed_out=False),
    ]


def _state_tail(line):
    """The `state=<word>` tail of one text row."""
    return line.rsplit("state=", 1)[1]


class TestSchemaTwoRowsCarryADerivedState:
    """A schema-2 record has no `owner` verdict, so the listing used to print
    `⚠️  state unreadable` on every readable row. Each row now ends with the
    state `list_sessions` derives from the record and its own clock, and the
    header counts those states instead of the schema-1 running/orphaned pair;
    a record whose state really cannot be read keeps its marker."""

    @staticmethod
    def _tree(tmp_path, monkeypatch, specs):
        tree = build_schema2_plexus(tmp_path / "plexus2", {"widgets": specs})
        make_env(monkeypatch, tree.root, "widgets")
        return tree

    def test_every_readable_row_ends_with_a_state_tail(self, monkeypatch, tmp_path, capsys):
        self._tree(tmp_path, monkeypatch,
                   _one_record_per_state(active_updated=run_state.utc_now_iso()))
        assert _main(["sessions"]) == 0
        out = capsys.readouterr().out
        assert "state unreadable" not in out
        header, *rows = out.splitlines()
        assert [_state_tail(line) for line in rows] == list(STATE_WORDS)
        assert header == (
            "Sessions: 5 (1 active, 1 stale, 1 ended not closed, "
            "1 waiting on question, 1 closed)"
        )

    def test_a_corrupt_state_file_still_prints_state_unreadable(
        self, monkeypatch, tmp_path, capsys
    ):
        self._tree(tmp_path, monkeypatch, [
            RecordSpec(uid=uid_for(1), slug="chat", started_at=stamp(1), updated=stamp(1, 12),
                       signed_out=True),
            RecordSpec(uid=uid_for(2), slug="chat", started_at=stamp(2), updated=stamp(2, 12),
                       readable=False),
        ])
        assert _main(["sessions"]) == 0
        header, *rows = capsys.readouterr().out.splitlines()
        readable = next(line for line in rows if uid_for(1) in line)
        corrupt = next(line for line in rows if uid_for(2) in line)
        assert readable.endswith("state=ended, not closed")
        assert corrupt.endswith("⚠️  state unreadable")
        assert "state=" not in corrupt
        assert header == "Sessions: 2 (1 ended not closed)"

    def test_header_buckets_count_the_states_derived_with_a_fixed_now(
        self, monkeypatch, tmp_path
    ):
        self._tree(tmp_path, monkeypatch, _one_record_per_state())
        rows = run_state.list_sessions(run_state.runs_base(), now=FIXED_NOW)
        assert [row["state"] for row in rows] == list(STATE_WORDS)
        assert [row["stop_reason"] for row in rows] == [None, "question", None, None, None]
        text = work_cli._format_sessions(rows, running_only=False)
        assert text.splitlines()[0] == (
            "Sessions: 5 (1 active, 1 stale, 1 ended not closed, "
            "1 waiting on question, 1 closed)"
        )

    def test_only_non_zero_buckets_are_listed(self, monkeypatch, tmp_path):
        self._tree(tmp_path, monkeypatch, [
            RecordSpec(uid=uid_for(1), slug="chat", started_at=stamp(1), updated=stamp(1, 12),
                       status="complete"),
            RecordSpec(uid=uid_for(2), slug="chat", started_at=stamp(2), updated=stamp(2, 12),
                       status="archived"),
        ])
        rows = run_state.list_sessions(run_state.runs_base(), now=FIXED_NOW)
        text = work_cli._format_sessions(rows, running_only=False)
        assert text.splitlines()[0] == "Sessions: 2 (2 closed)"

    def test_json_rows_carry_stop_reason_and_state_equal_to_the_text_row(
        self, monkeypatch, tmp_path, capsys
    ):
        self._tree(tmp_path, monkeypatch,
                   _one_record_per_state(active_updated=run_state.utc_now_iso()))
        assert _main(["sessions"]) == 0
        text_rows = capsys.readouterr().out.splitlines()[1:]
        by_id = {line.split()[1]: _state_tail(line) for line in text_rows}
        assert _main(["sessions", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["count"] == 5
        for entry in payload["sessions"]:
            assert entry["state"] == by_id[entry["id"]]
        stop_reasons = {entry["id"]: entry["stop_reason"] for entry in payload["sessions"]}
        assert stop_reasons[uid_for(2)] == "question"
        assert stop_reasons[uid_for(1)] is None

    def test_an_unrepresentable_row_clears_state_and_stop_reason_too(
        self, monkeypatch, tmp_path, capsys
    ):
        tree = self._tree(tmp_path, monkeypatch, [
            RecordSpec(uid=uid_for(1), slug="chat", started_at=stamp(1), updated=stamp(1, 12),
                       stop_reason="question"),
        ])
        native = tree.record_dir("widgets", uid_for(1)) / run_state.STATE_FILE
        native.write_text(
            native.read_text(encoding="utf-8").replace(f"updated: '{stamp(1, 12)}'",
                                                        f"updated: {stamp(1, 12)}"),
            encoding="utf-8",
        )
        assert _main(["sessions", "--json"]) == 0
        (entry,) = json.loads(capsys.readouterr().out)["sessions"]
        assert entry["id"] == uid_for(1)
        assert entry["state"] is None
        assert entry["stop_reason"] is None

    def test_running_is_still_in_progress_with_no_sign_out(
        self, monkeypatch, tmp_path, capsys
    ):
        self._tree(tmp_path, monkeypatch,
                   _one_record_per_state(active_updated=run_state.utc_now_iso()))
        assert _main(["sessions", "--running"]) == 0
        header, *rows = capsys.readouterr().out.splitlines()
        assert header == "Running sessions: 2"
        assert sorted(line.split()[1] for line in rows) == sorted([uid_for(4), uid_for(5)])
        assert sorted(_state_tail(line) for line in rows) == ["active", "stale"]

    def test_a_schema_one_listing_is_unchanged(self, monkeypatch, schema_tree, capsys):
        _launch(monkeypatch, schema_tree, session=SID_RESUMED)
        assert _main(["sessions"]) == 0
        out = capsys.readouterr().out
        assert "state=" not in out
        assert out.splitlines()[0] == "Sessions: 2 (0 running, 1 orphaned)"
        assert _main(["sessions", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert all("state" not in entry for entry in payload["sessions"])
        assert [entry["stop_reason"] for entry in payload["sessions"]] == ["complete", None]


# ---------------------------------------------------------------------------
# `work name`
# ---------------------------------------------------------------------------


class TestNameNamesTheLaunch:
    """Names are unique per PROJECT, and in schema form they name a session.

    Every conflict below is planted in ANOTHER group on purpose: the scan
    this replaced looked at the naming run's siblings, which in schema form
    are the other launches of its own group — so a sibling-only scan passes
    each of these tests while two runs of the project hold one name.
    """

    @pytest.fixture
    def group(self, monkeypatch, tmp_path):
        _bare_schema_tree(monkeypatch, tmp_path, session=SESSION_IDS[0])
        base = run_state.runs_base()
        _write_record(base / SLUG / SESSION_IDS[0])
        _write_record(base / SLUG / SESSION_IDS[1])
        return base

    def test_it_names_this_launchs_record_only(self, group, capsys):
        assert _main(["name", "second-look"]) == 0
        assert _state_of(group / SLUG / SESSION_IDS[0])["name"] == "second-look"
        assert _state_of(group / SLUG / SESSION_IDS[1])["name"] is None
        assert "Run named: second-look" in capsys.readouterr().out

    def test_the_group_itself_is_never_written(self, group):
        _main(["name", "second-look"])
        assert _dirs_in(group / SLUG) == sorted(SESSION_IDS)
        assert not (group / SLUG / run_state.STATE_FILE).exists()

    def test_a_name_held_by_another_group_is_refused(self, group, capsys):
        _write_record(
            group / "develop-issue-99" / SID_RESUMED,
            slug="develop-issue-99",
            name="second-look",
        )
        assert _main(["name", "second-look"]) == 1
        err = capsys.readouterr().err
        assert "already held" in err
        # Quoted run-area relative: a bare session id would not say which run.
        assert f"develop-issue-99/{SID_RESUMED}" in err
        assert _state_of(group / SLUG / SESSION_IDS[0])["name"] is None

    def test_a_name_shadowing_another_runs_slug_is_refused(self, group, capsys):
        _write_record(
            group / "develop-issue-99" / SID_RESUMED, slug="develop-issue-99"
        )
        assert _main(["name", "develop-issue-99"]) == 1
        assert "already held" in capsys.readouterr().err

    def test_a_sibling_launch_still_conflicts(self, group, capsys):
        run_state.write_state(
            group / SLUG / SESSION_IDS[1],
            dict(_state_of(group / SLUG / SESSION_IDS[1]), name="second-look"),
        )
        assert _main(["name", "second-look"]) == 1
        assert f"{SLUG}/{SESSION_IDS[1]}" in capsys.readouterr().err

    def test_a_directory_addressing_the_name_is_refused(self, group, capsys):
        """A stateless leftover occupies an address exactly as a run does —
        the answer `run_state.slug_available` gives."""
        (group / "second-look").mkdir()
        assert _main(["name", "second-look"]) == 1
        assert "already held by run 'second-look'" in capsys.readouterr().err

    def test_naming_this_runs_own_slug_is_still_allowed(self, group):
        assert _main(["name", SLUG]) == 0
        assert _state_of(group / SLUG / SESSION_IDS[0])["name"] == SLUG

    def test_a_legacy_conflict_reads_exactly_as_before(
        self, monkeypatch, legacy_tree, capsys
    ):
        _launch(monkeypatch, legacy_tree, session="s-legacy-1")
        base = run_state.runs_base()
        _write_record(base / "develop-issue-99", slug="develop-issue-99",
                      name="second-look")
        assert _main(["name", "second-look"]) == 1
        err = capsys.readouterr().err
        assert "already held by run 'develop-issue-99'" in err  # bare dir name

    def test_a_legacy_run_is_still_named_in_place(
        self, monkeypatch, legacy_tree, capsys
    ):
        _launch(monkeypatch, legacy_tree, session="s-legacy-1")
        assert _main(["name", "still-fine"]) == 0
        assert _state_of(legacy_tree.session_dirs[0])["name"] == "still-fine"
        assert "Run named: still-fine" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Resume: one launch, and the lineage that routes the rest of it
# ---------------------------------------------------------------------------


class TestASessionIsContinuedByName:
    """`--session <id>` is the whole resume handshake (T21's contract): one
    call per launch, and the record's `sessions:` lineage routes every later
    process — no second environment variable."""

    @pytest.fixture
    def group(self, monkeypatch, tmp_path):
        _bare_schema_tree(monkeypatch, tmp_path, session=SID_RESUMED)
        base = run_state.runs_base()
        _write_record(base / SLUG / SESSION_IDS[0], goal="the first launch")
        _write_record(base / SLUG / SESSION_IDS[1], goal="the second launch")
        return base

    def test_session_start_continues_the_named_record(self, group):
        assert _main(["session-start", "--session", SESSION_IDS[0]]) == 0
        continued = group / SLUG / SESSION_IDS[0]
        assert "session_start" in _event_types(continued)
        assert _state_of(continued)["owner"]["session_id"] == SID_RESUMED
        # No launch of its own: the relaunch joined a record, it did not open one.
        assert _dirs_in(group / SLUG) == sorted(SESSION_IDS)

    def test_the_new_harness_id_chains_into_the_lineage(self, group):
        _main(["session-start", "--session", SESSION_IDS[1]])
        state = _state_of(group / SLUG / SESSION_IDS[1])
        assert state[run_state.SESSIONS_FIELD] == [SID_RESUMED]

    def test_the_lineage_routes_the_rest_of_the_launch(self, group):
        """The handshake happens ONCE: every later `work` invocation is a new
        process that knows only its own LMER_SESSION_ID and still lands here."""
        _main(["session-start", "--session", SESSION_IDS[0]])
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["state", "set", "--phase=implement"]) == 0
        assert _state_of(group / SLUG / SESSION_IDS[0])["phase"] == "implement"
        assert _state_of(group / SLUG / SESSION_IDS[1])["phase"] is None
        assert _dirs_in(group / SLUG) == sorted(SESSION_IDS)

    def test_resume_briefs_the_named_session(self, group, capsys):
        assert _main(["resume", "--session", SESSION_IDS[1]]) == 0
        out = capsys.readouterr().out
        assert "the second launch" in out
        assert "the first launch" not in out

    def test_resume_json_quotes_the_named_session_dir(self, group, capsys):
        assert _main(["resume", "--json", "--session", SESSION_IDS[0]]) == 0
        decision = json.loads(capsys.readouterr().out)
        assert decision["run_dir"] == str(group / SLUG / SESSION_IDS[0])

    def test_an_out_of_form_id_is_passed_through_not_re_validated(self, group):
        """The CLI keeps no grammar of its own: the `--<name>` half of a
        session dir is unconstrained and a launcher's out-of-form id is used
        verbatim, so a second rule here would refuse ids that are on disk."""
        assert _main(["session-start", "--session", "s-cli-1"]) == 0
        assert (group / SLUG / "s-cli-1" / run_state.STATE_FILE).exists()

    @pytest.mark.parametrize("bad", [".", "..", "", "a/b", "2026*", "sid?"])
    def test_an_unusable_id_is_a_clean_refusal(self, group, capsys, bad):
        assert _main(["session-start", "--session", bad]) == 0  # hook-facing
        assert _main(["resume", "--session", bad]) == 0  # never non-zero
        out = capsys.readouterr().out
        assert "usable directory name" in out
        # Nothing at the grouping level D22 keeps empty, and no launch nobody
        # named: the value was never joined and never globbed.
        assert not (group / SLUG / run_state.STATE_FILE).exists()
        assert _dirs_in(group / SLUG) == sorted(SESSION_IDS)

    def test_the_refusal_is_the_kernels_own(self, group, capsys, monkeypatch):
        """Surfaced, not re-derived: whatever `run_state` refuses, the verb
        reports — it never grows a second copy of the rule."""
        def refuse(*_args, **_kwargs):
            raise run_state.RunStateError("kernel says no")

        monkeypatch.setattr(run_state, "run_dir", refuse)
        assert _main(["session-start", "--session", SESSION_IDS[0]]) == 0
        assert _main(["resume", "--session", SESSION_IDS[0]]) == 0
        out = capsys.readouterr().out
        assert out.count("kernel says no") == 2

    def test_a_legacy_tree_is_untouched_by_the_flag(
        self, monkeypatch, legacy_tree, capsys
    ):
        """One shared run dir per target: there is no launch to name, so the
        flag changes nothing and no lineage is recorded."""
        _launch(monkeypatch, legacy_tree, session="s-legacy-2")
        assert _main(["session-start", "--session", "whatever"]) == 0
        rdir = legacy_tree.session_dirs[0]
        state = _state_of(rdir)
        assert state["owner"]["session_id"] == "s-legacy-2"
        assert run_state.SESSIONS_FIELD not in state
        assert _dirs_in(run_state.runs_base()) == [SLUG]


# ---------------------------------------------------------------------------
# The answer relaunch
# ---------------------------------------------------------------------------


class TestTheAnswerRelaunchAddressesTheSessionThatAsked:
    """An answer belongs to the question it was given for, and the question
    was asked by one launch. Without this the relaunch — a new container with
    a new harness id — opens a fresh record, applies the answer to it and
    leaves the real question standing where nobody sees it answered."""

    @pytest.fixture
    def asked(self, monkeypatch, tmp_path):
        """Two launches of one run; the FIRST is stopped on a question."""
        _bare_schema_tree(monkeypatch, tmp_path, session=SID_RESUMED)
        base = run_state.runs_base()
        _write_record(
            base / SLUG / SESSION_IDS[0],
            stop_reason="question",
            open_question="new target or continue?",
        )
        _write_record(base / SLUG / SESSION_IDS[1], status="complete")
        return base

    @pytest.fixture
    def asked_after_container_start(self, asked, monkeypatch):
        """The same two launches, plus the EMPTY session dir the container
        entrypoint leaves behind — the shape every real relaunch arrives in.

        `clone_and_exec.ensure_work_repo_directory()` runs once at container
        start, before the harness and therefore before any `work` verb, and
        in schema form it creates this launch's `_runs/<slug>/<session-id>/`
        empty (D22, issue #353). Driven here exactly as `main()` drives it,
        from the same environment, because a reroute verified only in a
        world container start does not produce is a reroute verified in a
        shape it is never invoked in.
        """
        clone_and_exec.ensure_work_repo_directory(
            Path(os.environ["LMER_WORK_REPO_PATH"]),
            os.environ["LMER_REPO_HOST"],
            os.environ["LMER_REPO_PROJECT"],
            os.environ["LMER_TASK"],
            os.environ["LMER_TASK_TARGET"],
        )
        opened = asked / SLUG / SID_RESUMED
        assert opened.is_dir()  # the address exists...
        assert run_state._state_path(opened) is None  # ...and holds no record
        return asked

    def test_the_answer_lands_in_the_record_that_asked(self, asked, monkeypatch):
        monkeypatch.setenv("LMER_ANSWER", "continue this run")
        assert _main(["session-start"]) == 0
        asker = asked / SLUG / SESSION_IDS[0]
        assert _state_of(asker)["stop_reason"] is None
        assert _state_of(asker)["open_question"] is None
        assert "question_answered" in _event_types(asker)

    def test_no_third_directory_appears(self, asked, monkeypatch):
        monkeypatch.setenv("LMER_ANSWER", "continue this run")
        _main(["session-start"])
        assert _dirs_in(asked / SLUG) == sorted(SESSION_IDS)

    def test_the_relaunch_then_writes_in_that_record(self, asked, monkeypatch):
        monkeypatch.setenv("LMER_ANSWER", "continue this run")
        _main(["session-start"])
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["state", "set", "--phase=implement"]) == 0
        assert _state_of(asked / SLUG / SESSION_IDS[0])["phase"] == "implement"
        assert _dirs_in(asked / SLUG) == sorted(SESSION_IDS)

    def test_the_brief_leads_with_the_answered_pair(self, asked, monkeypatch, capsys):
        monkeypatch.setenv("LMER_ANSWER", "continue this run")
        _main(["session-start"])
        assert "continue this run" in capsys.readouterr().out

    def test_the_newest_asker_wins(self, asked, monkeypatch):
        """Several launches of one run stopped on a question resolve by the
        same newest-session rule the kernel and the fleet view use."""
        run_state.write_state(
            asked / SLUG / SESSION_IDS[1],
            dict(
                _state_of(asked / SLUG / SESSION_IDS[1]),
                status="in-progress",
                stop_reason="question",
            ),
        )
        monkeypatch.setenv("LMER_ANSWER", "continue this run")
        _main(["session-start"])
        assert _state_of(asked / SLUG / SESSION_IDS[1])["stop_reason"] is None
        assert _state_of(asked / SLUG / SESSION_IDS[0])["stop_reason"] == "question"

    def test_no_answer_reroutes_nothing(self, asked):
        """Narrow on purpose: a launch with nothing to apply is its own
        launch, and opens its own record beside the one that asked."""
        assert _main(["session-start"]) == 0
        assert _dirs_in(asked / SLUG) == sorted([*SESSION_IDS, SID_RESUMED])
        assert _state_of(asked / SLUG / SESSION_IDS[0])["stop_reason"] == "question"

    def test_a_launch_that_already_has_a_record_is_left_where_it_is(
        self, asked, monkeypatch
    ):
        _main(["session-start"])  # this launch opens its own dir
        monkeypatch.setenv("LMER_ANSWER", "continue this run")
        assert _main(["session-start"]) == 0
        assert _state_of(asked / SLUG / SESSION_IDS[0])["stop_reason"] == "question"
        assert _dirs_in(asked / SLUG) == sorted([*SESSION_IDS, SID_RESUMED])

    def test_the_dir_container_start_pre_creates_is_not_a_record(
        self, asked_after_container_start, monkeypatch
    ):
        """Emptiness, not existence: `hooks/start.py` invokes `work
        session-start` with no `--session`, so this IS the production path
        for an `--answer` relaunch, and an existence test makes it a no-op
        on every real container."""
        monkeypatch.setenv("LMER_ANSWER", "continue this run")
        assert _main(["session-start"]) == 0
        asker = asked_after_container_start / SLUG / SESSION_IDS[0]
        assert _state_of(asker)["stop_reason"] is None
        assert _state_of(asker)["open_question"] is None
        assert "question_answered" in _event_types(asker)

    def test_no_record_is_opened_at_the_pre_created_address(
        self, asked_after_container_start, monkeypatch
    ):
        """The failure this prevents is silent — exit 0 and a normal-looking
        brief for a brand-new empty record — so what the fleet view lists
        afterwards is the assertion that matters."""
        monkeypatch.setenv("LMER_ANSWER", "continue this run")
        _main(["session-start"])
        opened = asked_after_container_start / SLUG / SID_RESUMED
        assert run_state._state_path(opened) is None
        assert [row["id"] for row in run_state.list_sessions()] == list(SESSION_IDS)

    def test_the_rest_of_the_launch_follows_the_lineage_not_the_empty_dir(
        self, asked_after_container_start, monkeypatch
    ):
        """The pre-created dir keeps its name for the whole container, so a
        reroute that later invocations do not follow would answer the
        question and then write everything else next door."""
        monkeypatch.setenv("LMER_ANSWER", "continue this run")
        _main(["session-start"])
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["state", "set", "--phase=implement"]) == 0
        asked = asked_after_container_start
        assert _state_of(asked / SLUG / SESSION_IDS[0])["phase"] == "implement"
        assert run_state._state_path(asked / SLUG / SID_RESUMED) is None

    def test_a_relaunch_with_no_answer_still_opens_its_own_record(
        self, asked_after_container_start
    ):
        """The other half of the same guard: with nothing to apply the
        pre-created dir is this launch's own record, and the question the
        other launch asked is not touched."""
        assert _main(["session-start"]) == 0
        asked = asked_after_container_start
        assert _state_of(asked / SLUG / SID_RESUMED)["slug"] == SLUG
        assert _state_of(asked / SLUG / SESSION_IDS[0])["stop_reason"] == "question"

    def test_an_answer_this_container_already_applied_reroutes_nothing(
        self, asked, monkeypatch
    ):
        monkeypatch.setenv("LMER_ANSWER", "continue this run")
        work_cli._answer_marker_path("continue this run").touch()
        assert _main(["session-start"]) == 0
        assert _state_of(asked / SLUG / SESSION_IDS[0])["stop_reason"] == "question"
        assert _dirs_in(asked / SLUG) == sorted([*SESSION_IDS, SID_RESUMED])

    def test_an_explicit_session_beats_the_answer_route(self, asked, monkeypatch):
        monkeypatch.setenv("LMER_ANSWER", "continue this run")
        assert _main(["session-start", "--session", SESSION_IDS[1]]) == 0
        assert _state_of(asked / SLUG / SESSION_IDS[0])["stop_reason"] == "question"
        assert run_state.SESSIONS_FIELD in _state_of(asked / SLUG / SESSION_IDS[1])

    def test_only_this_runs_launches_are_candidates(self, asked, monkeypatch):
        """A question asked by a DIFFERENT run is not this launch's business."""
        for rdir in (asked / SLUG).iterdir():
            state = _state_of(rdir)
            state["stop_reason"] = None
            run_state.write_state(rdir, state)
        _write_record(
            asked / "develop-issue-99" / SESSION_IDS[0],
            slug="develop-issue-99",
            stop_reason="question",
        )
        monkeypatch.setenv("LMER_ANSWER", "continue this run")
        _main(["session-start"])
        other = asked / "develop-issue-99" / SESSION_IDS[0]
        assert _state_of(other)["stop_reason"] == "question"
        assert (asked / SLUG / SID_RESUMED).is_dir()

    def test_the_legacy_answer_path_is_byte_identical(
        self, monkeypatch, legacy_tree
    ):
        """One shared run dir per target IS the dir that asked — the reroute
        has nothing to do and must not invent something to do."""
        _launch(monkeypatch, legacy_tree, session="s-legacy-2")
        rdir = legacy_tree.session_dirs[0]
        state = _state_of(rdir)
        state.update(stop_reason="question", open_question="which branch?")
        run_state.write_state(rdir, state)
        monkeypatch.setenv("LMER_ANSWER", "the release branch")
        assert _main(["session-start"]) == 0
        assert _state_of(rdir)["stop_reason"] is None
        assert "question_answered" in _event_types(rdir)
        assert _dirs_in(run_state.runs_base()) == [SLUG]


# ---------------------------------------------------------------------------
# The refusal names the cause it actually has
# ---------------------------------------------------------------------------


def _unreadable_manifest_tree(monkeypatch, tmp_path, body, *, context=True):
    """A schema tree whose manifest this build cannot use."""
    root = tmp_path / "plexus-broken"
    root.mkdir()
    (root / "_lmer.yaml").write_text(body, encoding="utf-8")
    host, path = PRIMARY_REPO.split("/", 1)
    if context:
        _set_context(monkeypatch, root, host=host, project=path, session=SID_RESUMED)
    else:
        _set_context(monkeypatch, root, session=SID_RESUMED)
    return root


#: The document itself is unusable — nothing resolves (MANIFEST_PROBLEM).
_UNUSABLE_MANIFEST = "schema: 3\nname: example\nprojects: {}\n"
#: The document parsed; only its identity layer breaks a rule. The base
#: still resolves — to the {host}/{project} host bucket (REGISTRY_PROBLEM).
_BROKEN_REGISTRY = (
    "schema: 1\nname: example\nprojects:\n  alpha:\n"
    f"    remotes:\n      - {PRIMARY_REPO}\n"
)


class TestTheRefusalNamesTheCause:
    """A mutating verb refuses when nothing resolves a work-repo path. Since
    D20 there are two ways for that to happen, and reporting the second as
    the first sends the fix at the environment instead of at the manifest."""

    def test_an_unreadable_manifest_is_named_as_the_cause(
        self, monkeypatch, tmp_path, capsys
    ):
        root = _unreadable_manifest_tree(monkeypatch, tmp_path, _UNUSABLE_MANIFEST)
        assert _main(["state", "set", "--phase=implement"]) == 1
        err = capsys.readouterr().err
        assert str(root / "_lmer.yaml") in err.split("No run context:")[1]
        assert "LMER_REPO_HOST and LMER_REPO_PROJECT must be set" not in err

    @pytest.mark.parametrize(
        "argv",
        [
            ["state", "set", "--phase=implement"],
            ["event", "note"],
            ["answer", "yes"],
            ["seed", "develop", "https://example.com/x/-/issues/1"],
            ["report", "--file", "r.md"],
        ],
    )
    def test_every_refusing_verb_says_the_same_thing(
        self, monkeypatch, tmp_path, capsys, argv
    ):
        root = _unreadable_manifest_tree(monkeypatch, tmp_path, _UNUSABLE_MANIFEST)
        assert _main(argv) == 1
        err = capsys.readouterr().err
        assert "No run context:" in err
        assert str(root / "_lmer.yaml") in err
        assert "LMER_REPO_HOST and LMER_REPO_PROJECT must be set" not in err

    def test_unset_context_still_blames_the_environment(
        self, monkeypatch, tmp_path, capsys
    ):
        _set_context(monkeypatch, tmp_path)
        assert _main(["state", "set", "--phase=implement"]) == 1
        assert "LMER_REPO_HOST and LMER_REPO_PROJECT must be set" in (
            capsys.readouterr().err
        )

    def test_a_broken_registry_never_refuses(self, monkeypatch, tmp_path):
        """A REGISTRY problem still resolves a base — the {host}/{project}
        host bucket, D9's holding place for work nobody has named. Refusing
        on `problem` alone would take the whole work repo down over one
        hand-edited entry."""
        root = _unreadable_manifest_tree(monkeypatch, tmp_path, _BROKEN_REGISTRY)
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["state", "set", "--phase=implement"]) == 0
        bucket = root / PRIMARY_REPO / "_runs"
        assert [row["slug"] for row in run_state.list_sessions(base=bucket)] == [SLUG]

    def test_a_broken_registry_is_never_blamed_for_unset_context(
        self, monkeypatch, tmp_path, capsys
    ):
        """The two coincide — an unregistered launch with no host/project
        resolves nothing either — and only the MANIFEST kind explains a
        missing base. Reading `problem` alone points the operator at a
        registry entry that is not why this verb refused."""
        _unreadable_manifest_tree(
            monkeypatch, tmp_path, _BROKEN_REGISTRY, context=False
        )
        assert _main(["state", "set", "--phase=implement"]) == 1
        err = capsys.readouterr().err.split("No run context:")[1]
        assert "LMER_REPO_HOST and LMER_REPO_PROJECT must be set" in err
        assert "remotes:" not in err

    def test_the_read_only_listing_reports_it_and_exits_zero(
        self, monkeypatch, tmp_path, capsys
    ):
        root = _unreadable_manifest_tree(monkeypatch, tmp_path, _UNUSABLE_MANIFEST)
        assert _main(["sessions", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["form"] == "schema"
        assert payload["sessions"] == []
        assert str(root / "_lmer.yaml") in payload["detail"]

    def test_the_hook_facing_start_names_it_where_the_reader_looks(
        self, monkeypatch, tmp_path, capsys
    ):
        """`work session-start` is read through a pipe, not a terminal.

        `hooks/start.py` runs it with `capture_output=True` and injects its
        STDOUT into the agent's context; the stderr the manifest announcement
        goes to is captured and dropped. So this one line is the reader's
        only channel, and on a schema-form tree with an unusable manifest
        blaming unset environment variables is exactly the misdiagnosis
        `_no_run_context_reason()` exists to prevent — with nothing to
        correct it afterwards. Scoped to this site: the other read-only
        verbs print to a stderr that reaches a terminal.
        """
        root = _unreadable_manifest_tree(monkeypatch, tmp_path, _UNUSABLE_MANIFEST)
        assert _main(["session-start"]) == 0
        out = capsys.readouterr().out
        assert str(root / "_lmer.yaml") in out
        assert "LMER_REPO_HOST" not in out

    def test_the_hook_facing_start_still_blames_unset_context_when_it_is(
        self, monkeypatch, tmp_path, capsys
    ):
        """The cause it actually has, both ways round — a tree with no
        manifest and no launch context is still an environment problem."""
        _set_context(monkeypatch, tmp_path)
        assert _main(["session-start"]) == 0
        out = capsys.readouterr().out
        assert "LMER_REPO_HOST and LMER_REPO_PROJECT must be set" in out
        assert "run state skipped" in out


class TestTheVerbIsWiredIn:
    """Parser plumbing: the flags exist, and the read-only verbs declare no
    write intent (the journal excuses a scope, so a read-only verb naming one
    would hand the leak guard an alibi it has no use for)."""

    def test_sessions_parses_its_flags(self):
        args = work_cli.create_parser().parse_args(["sessions", "--running", "--json"])
        assert (args.command, args.running, args.as_json) == ("sessions", True, True)

    def test_the_session_flag_is_on_both_resume_verbs(self):
        parser = work_cli.create_parser()
        assert parser.parse_args(["resume", "--session", "x"]).session == "x"
        assert parser.parse_args(["session-start", "--session", "x"]).session == "x"

    def test_the_defaults_are_the_launchs_own_record(self):
        parser = work_cli.create_parser()
        assert parser.parse_args(["resume"]).session is None
        assert parser.parse_args(["session-start"]).session is None

    def test_sessions_declares_no_write_intent(self):
        args = work_cli.create_parser().parse_args(["sessions"])
        assert work_cli._write_intent_rel_paths(args) is None
