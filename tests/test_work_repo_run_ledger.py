"""Tests for the per-task execution ledger (issue #89): kernel functions in
run_state.py, the `work ledger` CLI verbs, and the resume-brief line."""
import json
from unittest.mock import patch

import pytest

from work_repo import cli as work_cli
from work_repo import run_state
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def run_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
    monkeypatch.setenv("LMER_TASK", "develop")
    monkeypatch.setenv("LMER_TASK_TARGET", "https://git.example.com/org/repo/-/issues/123")
    monkeypatch.setenv("LMER_SESSION_ID", "s-ledger-1")
    return tmp_path / "git.example.com" / "org/repo" / "runs" / "develop-issue-123"


def _main(argv):
    with patch.object(work_cli.sys, "argv", ["work"] + argv):
        return work_cli.main()


def _task_events(rdir):
    return [e for e in run_state.read_events(rdir, last_n=0) if e["type"] == "task"]


class TestLoadLedger:
    def test_absent_returns_none(self, tmp_path):
        assert run_state.load_ledger(tmp_path) is None

    def test_reads_written_ledger(self, tmp_path):
        run_state.write_ledger(tmp_path, {"schema": 1, "tasks": {"T1": {"status": "done"}}})
        ledger = run_state.load_ledger(tmp_path)
        assert ledger["tasks"]["T1"]["status"] == "done"

    def test_missing_tasks_normalized_to_empty(self, tmp_path):
        (tmp_path / "ledger.yaml").write_text("schema: 1\n", encoding="utf-8")
        assert run_state.load_ledger(tmp_path)["tasks"] == {}

    def test_corrupt_yaml_backed_up(self, tmp_path):
        (tmp_path / "ledger.yaml").write_text("tasks: [unclosed", encoding="utf-8")
        with pytest.raises(run_state.RunStateError, match="backed up"):
            run_state.load_ledger(tmp_path)
        assert not (tmp_path / "ledger.yaml").exists()
        assert list(tmp_path.glob("ledger.yaml.bad-*"))

    def test_non_mapping_backed_up(self, tmp_path):
        (tmp_path / "ledger.yaml").write_text("- a\n- b\n", encoding="utf-8")
        with pytest.raises(run_state.RunStateError, match="not a mapping"):
            run_state.load_ledger(tmp_path)
        assert list(tmp_path.glob("ledger.yaml.bad-*"))

    def test_non_mapping_tasks_backed_up(self, tmp_path):
        (tmp_path / "ledger.yaml").write_text("schema: 1\ntasks: [a]\n", encoding="utf-8")
        with pytest.raises(run_state.RunStateError, match="tasks field"):
            run_state.load_ledger(tmp_path)

    def test_newer_schema_read_only_refusal_leaves_file(self, tmp_path):
        (tmp_path / "ledger.yaml").write_text("schema: 99\ntasks: {}\n", encoding="utf-8")
        with pytest.raises(run_state.RunStateError, match="read-only refusal"):
            run_state.load_ledger(tmp_path)
        assert (tmp_path / "ledger.yaml").exists()

    def test_bad_schema_type_backed_up(self, tmp_path):
        (tmp_path / "ledger.yaml").write_text("schema: true\ntasks: {}\n", encoding="utf-8")
        with pytest.raises(run_state.RunStateError, match="schema field"):
            run_state.load_ledger(tmp_path)


class TestWriteLedger:
    def test_atomic_no_tmp_left(self, tmp_path):
        run_state.write_ledger(tmp_path, {"schema": 1, "tasks": {}})
        assert (tmp_path / "ledger.yaml").exists()
        assert not list(tmp_path.glob(".ledger.yaml.*.tmp"))  # temp carries pid+thread

    def test_preserves_insertion_order(self, tmp_path):
        run_state.write_ledger(
            tmp_path,
            {"schema": 1, "tasks": {"T2": {"status": "done"}, "T1": {"status": "pending"}}},
        )
        text = (tmp_path / "ledger.yaml").read_text(encoding="utf-8")
        assert text.index("T2") < text.index("T1")


class TestSetLedgerTask:
    def test_creates_ledger_and_appends_task_event(self, tmp_path):
        row = run_state.set_ledger_task(
            tmp_path, "T2", "done", title="lmer wiring", commit="4a1f9c2", receipt="t2-tests"
        )
        assert row["status"] == "done"
        assert row["updated"]
        ledger = run_state.load_ledger(tmp_path)
        assert ledger["schema"] == run_state.LEDGER_SCHEMA_VERSION
        assert ledger["tasks"]["T2"]["commit"] == "4a1f9c2"
        events = _task_events(tmp_path)
        assert len(events) == 1
        assert events[0]["note"] == "T2: done"
        assert events[0]["data"] == {"task": "T2", "status": "done",
                                     "commit": "4a1f9c2", "receipt": "t2-tests"}

    def test_partial_update_preserves_existing_fields(self, tmp_path):
        run_state.set_ledger_task(tmp_path, "T1", "in-progress", title="kernel")
        run_state.set_ledger_task(tmp_path, "T1", "done", commit="abc1234")
        row = run_state.load_ledger(tmp_path)["tasks"]["T1"]
        assert row["title"] == "kernel"
        assert row["status"] == "done"
        assert row["commit"] == "abc1234"

    def test_invalid_status_raises_and_writes_nothing(self, tmp_path):
        with pytest.raises(run_state.RunStateError, match="invalid task status"):
            run_state.set_ledger_task(tmp_path, "T1", "finished")
        assert run_state.load_ledger(tmp_path) is None
        assert _task_events(tmp_path) == []

    def test_event_omits_absent_optional_fields(self, tmp_path):
        run_state.set_ledger_task(tmp_path, "T1", "pending")
        assert _task_events(tmp_path)[0]["data"] == {"task": "T1", "status": "pending"}

    def test_title_and_note_are_redacted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_state, "redact_secrets", lambda s: "<redacted>")
        run_state.set_ledger_task(
            tmp_path, "T1", "done", title="secret title", commit="abc1234",
            receipt="t1", note="secret note",
        )
        row = run_state.load_ledger(tmp_path)["tasks"]["T1"]
        assert row["title"] == "<redacted>"
        assert row["note"] == "<redacted>"
        # Identifier fields pass through untouched.
        assert row["commit"] == "abc1234"
        assert row["receipt"] == "t1"


class TestSummarizeLedger:
    def test_none_and_empty(self):
        assert run_state.summarize_ledger(None) is None
        assert run_state.summarize_ledger({"schema": 1, "tasks": {}}) is None
        assert run_state.summarize_ledger({"schema": 1}) is None

    def test_counts_in_flight_and_last_commit(self):
        ledger = {"schema": 1, "tasks": {
            "T1": {"status": "done", "commit": "aaa1111", "updated": "2026-07-05T01:00:00Z"},
            "T2": {"status": "done", "commit": "bbb2222", "updated": "2026-07-05T02:00:00Z"},
            "T3": {"status": "in-progress"},
            "T4": {"status": "pending"},
        }}
        summary = run_state.summarize_ledger(ledger)
        assert summary["total"] == 4
        assert summary["done"] == 2
        assert summary["in_flight"] == ["T3"]
        assert summary["last_commit"] == "bbb2222"
        assert summary["counts"]["pending"] == 1

    def test_tolerates_malformed_rows(self):
        ledger = {"schema": 1, "tasks": {
            "T1": "not-a-dict",
            "T2": {"status": "weird"},
            "T3": {"status": ["not", "hashable"]},
            "T4": {"status": {"nor": "this"}},
            "T5": {"status": None},
        }}
        summary = run_state.summarize_ledger(ledger)
        assert summary["total"] == 5
        assert summary["done"] == 0
        assert summary["in_flight"] == []
        # A hand-mangled but parseable ledger must never break the brief.
        state = run_state.seed_state("develop-x", "develop", "x")
        brief = run_state.format_brief(run_state.decide(state, [], "s1", ledger=ledger))
        assert "Ledger: 0/5 done" in brief


class TestFormatLedger:
    def test_line_full_form(self):
        summary = {"total": 7, "done": 4, "counts": {}, "in_flight": ["T3a"],
                   "last_commit": "4a1f9c2"}
        assert run_state.format_ledger_line(summary) == (
            "Ledger: 4/7 done, in-flight: T3a, last commit 4a1f9c2"
        )

    def test_line_minimal_form(self):
        summary = {"total": 2, "done": 0, "counts": {}, "in_flight": [], "last_commit": None}
        assert run_state.format_ledger_line(summary) == "Ledger: 0/2 done"
        assert run_state.format_ledger_line(None) is None

    def test_table_no_ledger(self):
        assert run_state.format_ledger(None) == "No ledger"
        assert run_state.format_ledger({"schema": 1, "tasks": {}}) == "No ledger"

    def test_table_tolerates_malformed_rows(self):
        ledger = {"schema": 1, "tasks": {
            "T1": {"status": None},
            "T2": "not-a-dict",
            "T3": {"status": "done", "commit": "abc1234"},
        }}
        table = run_state.format_ledger(ledger)
        assert "T1" in table and "?" in table
        assert "commit=abc1234" in table

    def test_table_rows(self, tmp_path):
        run_state.set_ledger_task(
            tmp_path, "T2", "done", title="lmer wiring", commit="4a1f9c2", note="22 tests"
        )
        run_state.set_ledger_task(tmp_path, "T3a", "in-progress")
        table = run_state.format_ledger(run_state.load_ledger(tmp_path))
        assert "Ledger: 1/2 done, in-flight: T3a, last commit 4a1f9c2" in table
        assert "commit=4a1f9c2" in table
        assert "lmer wiring" in table
        assert "— 22 tests" in table


class TestDecideAndBrief:
    def test_decision_carries_full_ledger_and_brief_renders_line(self):
        state = run_state.seed_state("develop-x", "develop", "x")
        ledger = {"schema": 1, "tasks": {
            "T1": {"status": "done", "commit": "abc1234", "updated": "2026-07-05T01:00:00Z"},
            "T2": {"status": "in-progress"},
        }}
        decision = run_state.decide(state, [], "s1", ledger=ledger)
        assert decision["ledger"] == ledger
        brief = run_state.format_brief(decision)
        assert "Ledger: 1/2 done, in-flight: T2, last commit abc1234" in brief

    def test_brief_omits_line_without_ledger(self):
        state = run_state.seed_state("develop-x", "develop", "x")
        brief = run_state.format_brief(run_state.decide(state, [], "s1"))
        assert "Ledger:" not in brief


class TestCliLedgerShow:
    def test_no_context_exits_zero(self, capsys):
        assert _main(["ledger"]) == 0
        assert "no run context" in capsys.readouterr().out.lower()

    def test_no_ledger_prints_and_exits_zero(self, run_env, capsys):
        assert _main(["ledger"]) == 0
        assert "No ledger" in capsys.readouterr().out

    def test_shows_table(self, run_env, capsys):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            _main(["ledger", "set", "T1", "--status", "done", "--commit", "abc1234"])
        assert _main(["ledger"]) == 0
        out = capsys.readouterr().out
        assert "Ledger: 1/1 done" in out
        assert "commit=abc1234" in out

    def test_corrupt_ledger_warns_and_exits_one(self, run_env, capsys):
        run_env.mkdir(parents=True)
        (run_env / "ledger.yaml").write_text("tasks: [unclosed", encoding="utf-8")
        assert _main(["ledger"]) == 1
        assert "backed up" in capsys.readouterr().err


class TestCliLedgerSet:
    def test_no_context_exits_one(self, capsys):
        assert _main(["ledger", "set", "T1", "--status", "done"]) == 1
        err = capsys.readouterr().err
        assert "run context" in err.lower()
        # No done-without-commit warning for a mutation that never happened.
        assert "NO --commit" not in err

    def test_requires_task_id(self, run_env, capsys):
        assert _main(["ledger", "set", "--status", "done"]) == 1
        assert "task id" in capsys.readouterr().err.lower()

    def test_requires_status(self, run_env, capsys):
        assert _main(["ledger", "set", "T1"]) == 1
        assert "--status" in capsys.readouterr().err

    def test_invalid_status_rejected_by_parser(self, run_env):
        with pytest.raises(SystemExit):
            _main(["ledger", "set", "T1", "--status", "finished"])

    def test_writes_row_event_and_pushes(self, run_env):
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            rc = _main(["ledger", "set", "T2", "--status", "done",
                        "--title", "lmer wiring", "--commit", "4a1f9c2",
                        "--receipt", "t2-tests", "--note", "22 tests"])
        assert rc == 0
        row = run_state.load_ledger(run_env)["tasks"]["T2"]
        assert row == {"title": "lmer wiring", "status": "done", "commit": "4a1f9c2",
                       "receipt": "t2-tests", "note": "22 tests", "updated": row["updated"]}
        assert _task_events(run_env)[0]["session"] == "s-ledger-1"
        push.assert_called_once_with(
            ["git.example.com/org/repo/runs/develop-issue-123"],
            "run-state: develop-issue-123 ledger T2=done",
        )

    def test_auto_seeds_missing_run(self, run_env):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["ledger", "set", "T1", "--status", "in-progress"]) == 0
        assert run_state.load_state(run_env) is not None
        events = run_state.read_events(run_env, last_n=0)
        assert events[0]["type"] == "run_seeded"

    def test_done_without_commit_warns_but_succeeds(self, run_env, capsys):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["ledger", "set", "T1", "--status", "done"]) == 0
        err = capsys.readouterr().err
        assert "NO --commit" in err
        assert run_state.load_ledger(run_env)["tasks"]["T1"]["status"] == "done"

    def test_done_with_commit_does_not_warn(self, run_env, capsys):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["ledger", "set", "T1", "--status", "done",
                          "--commit", "abc1234"]) == 0
        assert "NO --commit" not in capsys.readouterr().err

    def test_push_failure_is_nonfatal(self, run_env, capsys):
        with patch("work_repo.cli.commit_work_path", return_value=1):
            assert _main(["ledger", "set", "T1", "--status", "pending"]) == 0
        assert "saved locally" in capsys.readouterr().out


class TestResumeLedger:
    def test_json_carries_full_ledger(self, run_env, capsys):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            _main(["state", "set", "--phase=execution"])
            _main(["ledger", "set", "T1", "--status", "done", "--commit", "abc1234"])
        capsys.readouterr()
        assert _main(["resume", "--json"]) == 0
        decision = json.loads(capsys.readouterr().out)
        assert decision["ledger"]["tasks"]["T1"]["commit"] == "abc1234"

    def test_brief_shows_ledger_line(self, run_env, capsys):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            _main(["ledger", "set", "T1", "--status", "done", "--commit", "abc1234"])
            _main(["ledger", "set", "T2", "--status", "in-progress"])
        capsys.readouterr()
        assert _main(["resume"]) == 0
        assert "Ledger: 1/2 done, in-flight: T2, last commit abc1234" in capsys.readouterr().out

    def test_corrupt_ledger_does_not_break_resume(self, run_env, capsys):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            _main(["state", "set", "--phase=execution"])
        run_dirs = list(run_env.parent.glob("develop-issue-123*"))
        (run_dirs[0] / "ledger.yaml").write_text("tasks: [unclosed", encoding="utf-8")
        capsys.readouterr()
        assert _main(["resume"]) == 0
        captured = capsys.readouterr()
        assert "Run:" in captured.out
        assert "Ledger:" not in captured.out
        assert "ledger unreadable" in captured.err


class TestArtifactReservedNames:
    def test_ledger_yaml_is_reserved(self, run_env, tmp_path, capsys):
        source = tmp_path / "src.md"
        source.write_text("x", encoding="utf-8")
        assert _main(["artifact", "ledger.yaml", "--file", str(source)]) == 1
        assert "reserved" in capsys.readouterr().err.lower()
