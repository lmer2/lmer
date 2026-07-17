"""Tests for run-state verbs in the work CLI."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

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
    monkeypatch.setenv("LMER_SESSION_ID", "s-cli-1")
    return tmp_path / "git.example.com" / "org/repo" / "runs" / "develop-issue-123"


def _main(argv):
    with patch.object(work_cli.sys, "argv", ["work"] + argv):
        return work_cli.main()


class TestStateShow:
    def test_no_context_exits_zero(self, capsys):
        assert _main(["state"]) == 0
        assert "no run context" in capsys.readouterr().out.lower()

    def test_shows_state(self, run_env, capsys):
        run_state.write_state(run_env, run_state.seed_state("develop-issue-123", "develop", "t"))
        assert _main(["state"]) == 0
        assert "develop-issue-123" in capsys.readouterr().out


class TestStateSet:
    def test_no_context_exits_one(self, capsys):
        assert _main(["state", "set", "--phase=interview"]) == 1

    def test_set_phase_seeds_appends_and_pushes(self, run_env):
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            assert _main(["state", "set", "--phase=interview"]) == 0
        state = run_state.load_state(run_env)
        assert state["phase"] == "interview"
        assert state["status"] == "in-progress"  # auto-seeded
        events = run_state.read_events(run_env, last_n=0)
        assert any(e["type"] == "run_seeded" for e in events)
        assert any(e["type"] == "phase" and e["note"] == "interview" for e in events)
        push.assert_called_once_with(
            ["git.example.com/org/repo/runs/develop-issue-123"],
            "run-state: develop-issue-123 phase=interview",
        )

    def test_set_stop_reason_no_push(self, run_env):
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            assert _main(["state", "set", "--stop-reason=question"]) == 0
        assert run_state.load_state(run_env)["stop_reason"] == "question"
        events = run_state.read_events(run_env, last_n=0)
        assert any(e["type"] == "state_changed" for e in events)
        push.assert_not_called()

    def test_set_status_complete_pushes(self, run_env):
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            assert _main(["state", "set", "--status=complete", "--stop-reason=complete"]) == 0
        state = run_state.load_state(run_env)
        assert state["status"] == "complete"
        assert state["stop_reason"] == "complete"
        push.assert_called_once()

    def test_stop_reason_none_clears(self, run_env):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            _main(["state", "set", "--stop-reason=question"])
            assert _main(["state", "set", "--stop-reason=none"]) == 0
        assert run_state.load_state(run_env)["stop_reason"] is None

    def test_critical_error_requires_payload(self, run_env, capsys):
        assert _main(["state", "set", "--stop-reason=critical_error"]) == 1
        assert "critical-error" in capsys.readouterr().err.lower()

    def test_critical_error_with_payload(self, run_env):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            rc = _main(["state", "set", "--stop-reason=critical_error",
                        "--critical-error", '{"summary": "boom", "detail": "trace"}'])
        assert rc == 0
        assert run_state.load_state(run_env)["critical_error"]["summary"] == "boom"

    def test_push_failure_is_nonfatal(self, run_env, capsys):
        with patch("work_repo.cli.commit_work_path", return_value=1):
            assert _main(["state", "set", "--phase=execution"]) == 0
        assert "warning" in capsys.readouterr().out.lower()

    def test_no_fields_errors(self, run_env, capsys):
        assert _main(["state", "set"]) == 1

    def test_same_phase_resubmission_is_noop(self, run_env):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            _main(["state", "set", "--phase=interview"])
            before = run_state.load_state(run_env)["updated"]
            n_events = len(run_state.read_events(run_env, last_n=0))
            assert _main(["state", "set", "--phase=interview"]) == 0
        assert run_state.load_state(run_env)["updated"] == before
        assert len(run_state.read_events(run_env, last_n=0)) == n_events

    def test_critical_error_must_be_object(self, run_env):
        assert _main(["state", "set", "--stop-reason=critical_error",
                      "--critical-error", '"oops"']) == 1
        assert _main(["state", "set", "--stop-reason=critical_error",
                      "--critical-error", '[1, 2]']) == 1

    def test_question_stored_and_event_carries_it(self, run_env):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            rc = _main(["state", "set", "--stop-reason=question",
                        "--question", "sqlite or postgres?"])
        assert rc == 0
        state = run_state.load_state(run_env)
        assert state["stop_reason"] == "question"
        assert state["open_question"] == "sqlite or postgres?"
        events = run_state.read_events(run_env, last_n=0)
        changed = [e for e in events if e["type"] == "state_changed"]
        assert changed[-1]["data"]["open_question"] == "sqlite or postgres?"

    def test_question_alone_valid_after_question_stop(self, run_env):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            _main(["state", "set", "--stop-reason=question"])
            assert _main(["state", "set", "--question", "which branch?"]) == 0
        assert run_state.load_state(run_env)["open_question"] == "which branch?"

    def test_question_without_question_stop_reason_errors(self, run_env, capsys):
        assert _main(["state", "set", "--question", "orphaned?"]) == 1
        assert "--stop-reason=question" in capsys.readouterr().err
        assert _main(["state", "set", "--stop-reason=yield",
                      "--question", "mismatched?"]) == 1
        assert "--stop-reason=question" in capsys.readouterr().err

    def test_clearing_stop_reason_clears_question(self, run_env):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            _main(["state", "set", "--stop-reason=question",
                   "--question", "sqlite or postgres?"])
            assert _main(["state", "set", "--stop-reason=none"]) == 0
        state = run_state.load_state(run_env)
        assert state["stop_reason"] is None
        assert state["open_question"] is None

    def test_other_stop_reason_clears_question(self, run_env):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            _main(["state", "set", "--stop-reason=question",
                   "--question", "sqlite or postgres?"])
            assert _main(["state", "set", "--stop-reason=yield"]) == 0
        assert run_state.load_state(run_env)["open_question"] is None

    def test_bare_question_stop_clears_previous_question(self, run_env):
        # A NEW question-stop without text must not resurface the previous
        # stop's question as if it were the current blocker.
        with patch("work_repo.cli.commit_work_path", return_value=0):
            _main(["state", "set", "--stop-reason=question",
                   "--question", "sqlite or postgres?"])
            assert _main(["state", "set", "--stop-reason=question"]) == 0
        state = run_state.load_state(run_env)
        assert state["stop_reason"] == "question"
        assert state["open_question"] is None

    def test_completion_event_carries_actuals(self, run_env):
        # Issue #99: two sessions ran, then the run completes — the
        # state_changed event carries the machine-computed actuals.
        _main(["session-start"])
        with patch("work_repo.cli.commit_work_path", return_value=0):
            _main(["session-end"])
        _main(["session-start"])
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["state", "set", "--status=complete",
                          "--stop-reason=complete"]) == 0
        events = run_state.read_events(run_env, last_n=0)
        first_start = next(e for e in events if e["type"] == "session_start")
        changed = [e for e in events if e["type"] == "state_changed"][-1]
        actuals = changed["data"]["actuals"]
        assert actuals["sessions_used"] == 2
        assert actuals["first_session_at"] == first_start["ts"]
        assert actuals["completed_at"].endswith("Z")

    def test_completion_actuals_fail_soft_to_null(self, run_env, capsys, monkeypatch):
        # An events read problem must never block completion — the actuals
        # degrade to null (the completion stamp itself is still recorded).
        run_state.write_state(run_env, run_state.seed_state("develop-issue-123", "develop", "t"))
        captured = {}
        real_append = run_state.append_event

        def failing_read(rdir, last_n=5):
            raise OSError("events unreadable")

        def spying_append(rdir, event_type, note=None, data=None):
            captured[event_type] = data
            return real_append(rdir, event_type, note=note, data=data)

        monkeypatch.setattr(work_cli.run_state, "read_events", failing_read)
        monkeypatch.setattr(work_cli.run_state, "append_event", spying_append)
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["state", "set", "--status=complete",
                          "--stop-reason=complete"]) == 0
        actuals = captured["state_changed"]["actuals"]
        assert actuals["sessions_used"] is None
        assert actuals["first_session_at"] is None
        assert actuals["completed_at"].endswith("Z")

    def test_non_completion_event_carries_no_actuals(self, run_env):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["state", "set", "--stop-reason=yield"]) == 0
        changed = [e for e in run_state.read_events(run_env, last_n=0)
                   if e["type"] == "state_changed"][-1]
        assert "actuals" not in changed["data"]


def _ask_question(run_env, question="sqlite or postgres?"):
    """Record a question stop on the run, the way a session would (#97)."""
    with patch("work_repo.cli.commit_work_path", return_value=0):
        assert _main(["state", "set", "--stop-reason=question",
                      "--question", question]) == 0


class TestAnswer:
    """`work answer` — resume-on-answer (issue #98)."""

    def test_answer_clears_question_appends_event_and_pushes(self, run_env):
        _ask_question(run_env)
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            assert _main(["answer", "postgres"]) == 0
        state = run_state.load_state(run_env)
        assert state["open_question"] is None
        assert state["stop_reason"] is None
        assert state["status"] == "in-progress"
        event = run_state.read_events(run_env, last_n=0)[-1]
        assert event["type"] == "question_answered"
        assert event["data"] == {"question": "sqlite or postgres?",
                                 "answer": "postgres"}
        push.assert_called_once_with(
            ["git.example.com/org/repo/runs/develop-issue-123"],
            "run-state: develop-issue-123 question answered",
        )

    def test_completed_run_keeps_status(self, run_env):
        _ask_question(run_env)
        with patch("work_repo.cli.commit_work_path", return_value=0):
            # A question can outlive completion (the #97 completed-run ask
            # path); answering must not silently reopen the run.
            state = run_state.load_state(run_env)
            state["status"] = "complete"
            run_state.write_state(run_env, state)
            assert _main(["answer", "archive it"]) == 0
        assert run_state.load_state(run_env)["status"] == "complete"

    def test_no_open_question_errors(self, run_env, capsys):
        with patch("work_repo.cli.commit_work_path", return_value=0):
            _main(["state", "set", "--phase=spec"])  # run exists, no question
        assert _main(["answer", "unasked"]) == 1
        assert "No open question" in capsys.readouterr().err

    def test_no_run_at_all_errors(self, run_env, capsys):
        assert _main(["answer", "nothing there"]) == 1
        assert "No open question" in capsys.readouterr().err
        assert run_state.load_state(run_env) is None  # never auto-seeds

    def test_no_context_exits_one(self, capsys):
        assert _main(["answer", "postgres"]) == 1
        assert "No run context" in capsys.readouterr().err

    def test_empty_answer_errors(self, run_env, capsys):
        _ask_question(run_env)
        assert _main(["answer", "   "]) == 1
        assert "non-empty" in capsys.readouterr().err
        assert run_state.load_state(run_env)["open_question"] == "sqlite or postgres?"

    def test_push_failure_is_nonfatal(self, run_env, capsys):
        _ask_question(run_env)
        with patch("work_repo.cli.commit_work_path", return_value=1):
            assert _main(["answer", "postgres"]) == 0
        assert "push failed" in capsys.readouterr().out


class TestEvent:
    def test_appends_event(self, run_env):
        assert _main(["event", "review_posted", "--note", "MR 456: iteration 2"]) == 0
        events = run_state.read_events(run_env, last_n=0)
        assert events[-1]["type"] == "review_posted"
        assert events[-1]["note"] == "MR 456: iteration 2"

    def test_data_json(self, run_env):
        assert _main(["event", "custom", "--data", '{"k": 1}']) == 0
        assert run_state.read_events(run_env, last_n=0)[-1]["data"] == {"k": 1}

    def test_bad_data_json_errors(self, run_env, capsys):
        assert _main(["event", "custom", "--data", "{not json"]) == 1

    def test_no_context_exits_one(self):
        assert _main(["event", "x"]) == 1

    def test_event_auto_seeds_missing_run(self, run_env):
        assert _main(["event", "x"]) == 0
        assert run_state.load_state(run_env) is not None
        events = run_state.read_events(run_env, last_n=0)
        assert events[0]["type"] == "run_seeded"


def _verify_events(run_env):
    return [e for e in run_state.read_events(run_env, last_n=0) if e["type"] == "verify"]


class TestVerify:
    def test_no_context_exits_one_and_never_runs_command(self, tmp_path, capsys):
        marker = tmp_path / "ran.txt"
        assert _main(["verify", "tests", "--", "touch", str(marker)]) == 1
        assert not marker.exists()
        assert "run context" in capsys.readouterr().err.lower()

    def test_pass_receipt_shape(self, run_env, capsys):
        assert _main(["verify", "tests", "--", "echo", "1397 passed in 41.8s"]) == 0
        event = _verify_events(run_env)[-1]
        assert event["note"] == "tests: pass"
        data = event["data"]
        assert data["name"] == "tests"
        assert data["argv"] == ["echo", "1397 passed in 41.8s"]
        assert data["exit_code"] == 0
        assert data["duration_s"] >= 0
        assert data["summary_line"] == "1397 passed in 41.8s"
        assert data["output_tail_sha256"] == hashlib.sha256(
            b"1397 passed in 41.8s\n").hexdigest()

    def test_mirrors_nonzero_exit_code(self, run_env):
        assert _main(["verify", "tests", "--", "sh", "-c", "exit 4"]) == 4
        event = _verify_events(run_env)[-1]
        assert event["note"] == "tests: exit 4"
        assert event["data"]["exit_code"] == 4

    def test_streams_output_and_reports_on_stderr(self, run_env, capsys):
        assert _main(["verify", "ok", "--", "echo", "hello"]) == 0
        captured = capsys.readouterr()
        assert "hello" in captured.out
        assert "Verify receipt recorded: ok: pass" in captured.err

    def test_stderr_merged_into_tail(self, run_env):
        assert _main(["verify", "warn", "--", "sh", "-c", "echo boom >&2"]) == 0
        assert _verify_events(run_env)[-1]["data"]["summary_line"] == "boom"

    def test_tail_bounded_to_last_64k(self, run_env):
        script = "import sys; sys.stdout.write('a' * 100000 + 'END\\n')"
        assert _main(["verify", "big", "--", sys.executable, "-c", script]) == 0
        expected_tail = ("a" * 100000 + "END\n").encode()[-work_cli.VERIFY_TAIL_BYTES:]
        data = _verify_events(run_env)[-1]["data"]
        assert data["output_tail_sha256"] == hashlib.sha256(expected_tail).hexdigest()

    def test_command_not_found_exits_127(self, run_env, capsys):
        assert _main(["verify", "x", "--", "definitely-not-a-command-xyz"]) == 127
        data = _verify_events(run_env)[-1]["data"]
        assert data["exit_code"] == 127
        assert "summary_line" not in data  # no output — nothing fabricated
        assert data["output_tail_sha256"] == hashlib.sha256(b"").hexdigest()
        assert "could not start" in capsys.readouterr().err

    def test_requires_command(self, run_env, capsys):
        assert _main(["verify", "tests"]) == 1
        assert _main(["verify", "tests", "--"]) == 1
        assert not _verify_events(run_env)

    def test_requires_separator(self, run_env, capsys):
        """A forgotten name must not silently become the command:
        `work verify -- pytest tests/` parses as name='pytest' with no
        leading `--` left in the remainder — refuse it."""
        marker_free_cmd = ["verify", "--", "echo", "hi"]  # name swallows "echo"
        assert _main(marker_free_cmd) == 1
        assert _main(["verify", "tests", "echo", "hi"]) == 1  # separator missing
        assert not _verify_events(run_env)
        assert "`--` separator" in capsys.readouterr().err

    def test_signal_killed_maps_to_shell_convention(self, run_env):
        assert _main(["verify", "sig", "--", "sh", "-c", "kill -9 $$"]) == 137
        event = _verify_events(run_env)[-1]
        assert event["note"] == "sig: exit 137"
        assert event["data"]["exit_code"] == 137

    def test_broken_pipe_keeps_receipt_and_exit_code(self, run_env, monkeypatch):
        """A downstream consumer closing early (`… | head`) must not kill
        the receipt or the exit-code mirror — echoing stops, hashing goes on."""
        class _BrokenBuffer:
            def write(self, chunk):
                raise BrokenPipeError
            def flush(self):
                pass

        class _FakeStdout:
            buffer = _BrokenBuffer()

        monkeypatch.setattr(work_cli.sys, "stdout", _FakeStdout())
        assert _main(["verify", "piped", "--", "sh", "-c", "echo out; echo more"]) == 0
        event = _verify_events(run_env)[-1]
        assert event["note"] == "piped: pass"
        assert event["data"]["exit_code"] == 0
        # The tail kept accumulating after the echo died.
        assert event["data"]["summary_line"] == "more"
        assert event["data"]["output_tail_sha256"] == hashlib.sha256(
            b"out\nmore\n").hexdigest()

    def test_requires_nonempty_name(self, run_env, capsys):
        assert _main(["verify", "   ", "--", "true"]) == 1
        assert not _verify_events(run_env)

    def test_summary_line_is_redacted(self, run_env, monkeypatch):
        monkeypatch.setattr(work_cli, "redact_secrets", lambda s: "<redacted>")
        assert _main(["verify", "t", "--", "echo", "token=hunter2"]) == 0
        assert _verify_events(run_env)[-1]["data"]["summary_line"] == "<redacted>"

    def test_receipt_failure_warns_but_mirrors_exit_code(self, run_env, capsys, monkeypatch):
        # Seed first: only the RECEIPT append may fail, not the auto-seed.
        run_state.write_state(run_env, run_state.seed_state("develop-issue-123", "develop", "t"))
        monkeypatch.setattr(
            work_cli.run_state, "append_event",
            lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")),
        )
        assert _main(["verify", "t", "--", "echo", "fine"]) == 0
        assert "NOT recorded" in capsys.readouterr().err

    def test_auto_seeds_missing_run(self, run_env):
        assert _main(["verify", "t", "--", "true"]) == 0
        assert run_state.load_state(run_env) is not None
        events = run_state.read_events(run_env, last_n=0)
        assert events[0]["type"] == "run_seeded"


def _seed_sibling(runs_base, slug, name, state_file="state.yaml"):
    """Create a sibling run dir holding `name` (bypassing the single writer
    so legacy state.yml siblings can be fabricated)."""
    sibling = runs_base / slug
    sibling.mkdir(parents=True)
    state = run_state.seed_state(slug, "develop", "t")
    state["name"] = name
    (sibling / state_file).write_text(yaml.safe_dump(state, sort_keys=False))
    return sibling


class TestName:
    def test_set_name_writes_state_and_event(self, run_env, capsys):
        assert _main(["name", "auth-refactor"]) == 0
        state = run_state.load_state(run_env)
        assert state["name"] == "auth-refactor"
        events = run_state.read_events(run_env, last_n=0)
        assert any(e["type"] == "run_named" and e["note"] == "auth-refactor" for e in events)
        assert "auth-refactor" in capsys.readouterr().out

    def test_normalizes_to_kebab_case(self, run_env, capsys):
        assert _main(["name", "My Fancy_Run!! (v2)"]) == 0
        assert run_state.load_state(run_env)["name"] == "my-fancy-run-v2"
        assert "my-fancy-run-v2" in capsys.readouterr().out

    def test_collapses_and_trims_dashes(self, run_env):
        assert _main(["name", "--foo--_bar  baz--"]) == 0
        assert run_state.load_state(run_env)["name"] == "foo-bar-baz"

    def test_empty_after_normalization_errors(self, run_env, capsys):
        assert _main(["name", "!!!"]) == 1
        assert "normalization" in capsys.readouterr().err.lower()
        assert run_state.load_state(run_env) is None  # nothing written

    def test_rename_overwrites_and_logs_again(self, run_env):
        assert _main(["name", "first-name"]) == 0
        assert _main(["name", "second-name"]) == 0
        assert run_state.load_state(run_env)["name"] == "second-name"
        events = run_state.read_events(run_env, last_n=0)
        named = [e["note"] for e in events if e["type"] == "run_named"]
        assert named == ["first-name", "second-name"]

    def test_own_name_resubmission_is_noop(self, run_env, capsys):
        _main(["name", "steady-name"])
        before = run_state.load_state(run_env)["updated"]
        n_events = len(run_state.read_events(run_env, last_n=0))
        assert _main(["name", "steady-name"]) == 0
        assert "unchanged" in capsys.readouterr().out.lower()
        assert run_state.load_state(run_env)["updated"] == before
        assert len(run_state.read_events(run_env, last_n=0)) == n_events

    def test_rejects_name_held_by_sibling(self, run_env, capsys):
        _seed_sibling(run_env.parent, "develop-issue-99", "taken-name")
        assert _main(["name", "taken-name"]) == 1
        assert "develop-issue-99" in capsys.readouterr().err
        state = run_state.load_state(run_env)
        assert state is None or state.get("name") is None

    def test_rejects_name_matching_sibling_slug(self, run_env, capsys):
        _seed_sibling(run_env.parent, "review-mr-9", "unrelated-name")
        assert _main(["name", "review-mr-9"]) == 1
        assert "review-mr-9" in capsys.readouterr().err
        state = run_state.load_state(run_env)
        assert state is None or state.get("name") is None

    def test_archive_is_reserved(self, run_env, capsys):
        assert _main(["name", "archive"]) == 1
        assert "reserved" in capsys.readouterr().err.lower()
        state = run_state.load_state(run_env)
        assert state is None or state.get("name") is None

    def test_own_slug_as_name_is_allowed(self, run_env):
        # The run's own slug is not a conflict — name==slug is redundant but
        # harmless, and stays valid if name-as-directory lands.
        assert _main(["name", "develop-issue-123"]) == 0
        assert run_state.load_state(run_env)["name"] == "develop-issue-123"

    def test_legacy_state_yml_sibling_counts_as_taken(self, run_env, capsys):
        _seed_sibling(run_env.parent, "develop-issue-7", "legacy-name",
                      state_file="state.yml")
        assert _main(["name", "legacy-name"]) == 1
        assert "develop-issue-7" in capsys.readouterr().err

    def test_corrupt_sibling_is_skipped(self, run_env):
        sibling = run_env.parent / "develop-issue-broken"
        sibling.mkdir(parents=True)
        (sibling / "state.yaml").write_text("{ not: valid: yaml [")
        assert _main(["name", "fresh-name"]) == 0
        assert run_state.load_state(run_env)["name"] == "fresh-name"
        # The corrupt sibling was only read, never backed up or repaired.
        assert (sibling / "state.yaml").exists()

    def test_archived_runs_do_not_hold_names(self, run_env):
        _seed_sibling(run_env.parent / "archive", "develop-issue-old", "archived-name")
        assert _main(["name", "archived-name"]) == 0
        assert run_state.load_state(run_env)["name"] == "archived-name"

    def test_set_auto_seeds_missing_run(self, run_env):
        assert _main(["name", "seeded-run"]) == 0
        events = run_state.read_events(run_env, last_n=0)
        assert events[0]["type"] == "run_seeded"
        assert run_state.load_state(run_env)["status"] == "in-progress"

    def test_bare_displays_name(self, run_env, capsys):
        _main(["name", "shown-name"])
        capsys.readouterr()
        assert _main(["name"]) == 0
        assert "shown-name" in capsys.readouterr().out

    def test_bare_without_name(self, run_env, capsys):
        run_state.write_state(run_env, run_state.seed_state("develop-issue-123", "develop", "t"))
        assert _main(["name"]) == 0
        assert "no name set" in capsys.readouterr().out.lower()

    def test_bare_without_run_state(self, run_env, capsys):
        assert _main(["name"]) == 0
        assert "no name set" in capsys.readouterr().out.lower()

    def test_bare_no_context_exits_zero(self, capsys):
        assert _main(["name"]) == 0
        assert "no run context" in capsys.readouterr().out.lower()

    def test_set_no_context_exits_one(self, capsys):
        assert _main(["name", "some-name"]) == 1
        assert "no run context" in capsys.readouterr().err.lower()


class TestResume:
    def test_no_context(self, capsys):
        assert _main(["resume"]) == 0
        assert "no run context" in capsys.readouterr().out.lower()

    def test_no_run_yet(self, run_env, capsys):
        assert _main(["resume"]) == 0
        assert "fresh run" in capsys.readouterr().out.lower()

    def test_existing_run_brief(self, run_env, capsys):
        run_state.write_state(run_env, run_state.seed_state("develop-issue-123", "develop", "t"))
        run_state.append_event(run_env, "phase", note="interview")
        assert _main(["resume"]) == 0
        out = capsys.readouterr().out
        assert "develop-issue-123" in out
        assert "interview" in out

    def test_json_mode(self, run_env, capsys):
        run_state.write_state(run_env, run_state.seed_state("develop-issue-123", "develop", "t"))
        assert _main(["resume", "--json"]) == 0
        decision = json.loads(capsys.readouterr().out)
        assert decision["kind"] == "run"
        assert decision["slug"] == "develop-issue-123"

    def test_json_mode_exposes_name(self, run_env, capsys):
        state = run_state.seed_state("develop-issue-123", "develop", "t")
        state["name"] = "auth-refactor"
        run_state.write_state(run_env, state)
        assert _main(["resume", "--json"]) == 0
        decision = json.loads(capsys.readouterr().out)
        assert decision["name"] == "auth-refactor"

    def test_json_mode_fresh_run(self, run_env, capsys):
        assert _main(["resume", "--json"]) == 0
        decision = json.loads(capsys.readouterr().out)
        assert decision["kind"] == "none"

    def test_corrupt_state_degrades(self, run_env, capsys):
        run_env.mkdir(parents=True)
        (run_env / "state.yml").write_text("{ not: valid: yaml [")
        assert _main(["resume"]) == 0
        assert "unreadable" in capsys.readouterr().out.lower()


class TestArtifact:
    def test_copies_registers_and_logs(self, run_env, tmp_path, capsys):
        src = tmp_path / "summary.md"
        src.write_text("# Agreed approach\ndo the thing\n")
        assert _main(["artifact", "spec.md", "--file", str(src)]) == 0
        assert (run_env / "spec.md").read_text().startswith("# Agreed approach")
        state = run_state.load_state(run_env)
        assert state["artifacts"]["spec"] == "spec.md"
        events = run_state.read_events(run_env, last_n=0)
        assert any(e["type"] == "artifact_written" and e["note"] == "spec.md" for e in events)

    def test_redacts_secrets(self, run_env, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_API_TOKEN", "supersecretvalue123")
        src = tmp_path / "r.md"
        src.write_text("token is supersecretvalue123 ok")
        assert _main(["artifact", "retro.md", "--file", str(src)]) == 0
        assert "supersecretvalue123" not in (run_env / "retro.md").read_text()

    def test_rejects_path_traversal_name(self, run_env, tmp_path, capsys):
        src = tmp_path / "x.md"
        src.write_text("content here")
        assert _main(["artifact", "../evil.md", "--file", str(src)]) == 1

    def test_missing_source_errors(self, run_env):
        assert _main(["artifact", "spec.md", "--file", "/nonexistent/x.md"]) == 1

    def test_no_context_exits_one(self, tmp_path):
        src = tmp_path / "x.md"
        src.write_text("content")
        assert _main(["artifact", "spec.md", "--file", str(src)]) == 1

    def test_rejects_reserved_and_empty_names(self, run_env, tmp_path):
        src = tmp_path / "x.md"
        src.write_text("content")
        for bad in (
            "state.yaml",
            "state.yml",
            "events.jsonl",
            "state.yaml.bad-20260703",
            "state.yml.bad-20260703",
            "state.yml.migrated",
            "",
        ):
            assert _main(["artifact", bad, "--file", str(src)]) == 1, bad

    def test_reserved_name_does_not_clobber_events(self, run_env, tmp_path):
        run_state.append_event(run_env, "precious")
        src = tmp_path / "x.md"
        src.write_text("payload")
        assert _main(["artifact", "events.jsonl", "--file", str(src)]) == 1
        events = run_state.read_events(run_env, last_n=0)
        assert [e["type"] for e in events] == ["precious"]


class TestSessionStart:
    def test_no_context_soft_exit(self, capsys):
        assert _main(["session-start"]) == 0
        assert "no run context" in capsys.readouterr().out.lower()

    def test_seeds_claims_and_prints_brief(self, run_env, capsys):
        assert _main(["session-start"]) == 0
        state = run_state.load_state(run_env)
        assert state["slug"] == "develop-issue-123"
        assert state["owner"]["session_id"] == "s-cli-1"
        assert state["owner"]["claimed_at"].endswith("Z")
        events = run_state.read_events(run_env, last_n=0)
        assert [e["type"] for e in events] == ["run_seeded", "session_start"]
        assert "develop-issue-123" in capsys.readouterr().out

    def test_existing_run_not_reseeded(self, run_env, capsys):
        _main(["session-start"])
        _main(["session-start"])
        events = run_state.read_events(run_env, last_n=0)
        assert [e["type"] for e in events].count("run_seeded") == 1
        assert [e["type"] for e in events].count("session_start") == 2

    def test_foreign_claim_warned_then_taken(self, run_env, capsys, monkeypatch):
        _main(["session-start"])
        monkeypatch.setenv("LMER_SESSION_ID", "s-cli-2")
        assert _main(["session-start"]) == 0
        out = capsys.readouterr().out
        assert "claim" in out.lower()  # warning surfaced in the brief
        assert run_state.load_state(run_env)["owner"]["session_id"] == "s-cli-2"

    def test_corrupt_state_recovers_with_fresh_seed(self, run_env, capsys):
        run_env.mkdir(parents=True)
        (run_env / "state.yml").write_text("{ not: valid: yaml [")
        assert _main(["session-start"]) == 0
        assert "recovered" in capsys.readouterr().out.lower()
        assert run_state.load_state(run_env)["slug"] == "develop-issue-123"
        assert list(run_env.glob("state.yml.bad-*"))

    def test_completed_run_reported_not_reseeded(self, run_env, capsys):
        state = run_state.seed_state("develop-issue-123", "develop", "t")
        state["status"] = "complete"
        run_state.write_state(run_env, state)
        assert _main(["session-start"]) == 0
        out = capsys.readouterr().out
        assert "complete" in out
        # No seed (autouse fixture strips LMER_*): the ask-or-stop line.
        assert "COMPLETED RUN" in out
        assert 'work state set --stop-reason=question --question "<text>"' in out
        assert run_state.load_state(run_env)["status"] == "complete"

    def test_completed_run_with_seed_prints_seed_line(self, run_env, capsys, monkeypatch):
        state = run_state.seed_state("develop-issue-123", "develop", "t")
        state["status"] = "complete"
        run_state.write_state(run_env, state)
        monkeypatch.setenv("LMER_START_PROMPT", "pick up issue 97 next")
        assert _main(["session-start"]) == 0
        out = capsys.readouterr().out
        assert "COMPLETED RUN" in out
        assert "Seed provided (LMER_START_PROMPT): pick up issue 97 next" in out
        assert "work state set --status=in-progress --stop-reason=none" in out

    def test_lmer_answer_applied_and_brief_leads_with_pair(self, run_env, capsys, monkeypatch):
        # Issue #98: a pushed answer (LMER_ANSWER, from `lmer --answer`) is
        # applied before the brief prints, and the brief leads with the pair.
        _ask_question(run_env)
        capsys.readouterr()  # drain the setup command's output
        monkeypatch.setenv("LMER_ANSWER", "postgres")
        assert _main(["session-start"]) == 0
        out = capsys.readouterr().out
        lines = out.splitlines()
        assert lines[0].startswith("✅ ANSWERED QUESTION")
        assert lines[1] == "Q: sqlite or postgres?"
        assert lines[2] == "A: postgres"
        assert "record the follow-up goal/phase" in lines[3]
        assert "OPEN QUESTION" not in out  # the stale block it just resolved
        state = run_state.load_state(run_env)
        assert state["open_question"] is None
        assert state["stop_reason"] is None
        assert state["owner"]["session_id"] == "s-cli-1"  # still claimed
        events = [e["type"] for e in run_state.read_events(run_env, last_n=0)]
        assert "question_answered" in events
        assert events[-1] == "session_start"  # answer applied BEFORE deciding

    def test_lmer_answer_keeps_completed_status(self, run_env, capsys, monkeypatch):
        _ask_question(run_env)
        state = run_state.load_state(run_env)
        state["status"] = "complete"
        run_state.write_state(run_env, state)
        monkeypatch.setenv("LMER_ANSWER", "reopen with the new target")
        assert _main(["session-start"]) == 0
        out = capsys.readouterr().out
        assert "ANSWERED QUESTION" in out
        assert "COMPLETED RUN" in out  # the #96 directive still governs reopening
        assert run_state.load_state(run_env)["status"] == "complete"

    def test_lmer_answer_ignored_without_open_question(self, run_env, capsys, monkeypatch):
        monkeypatch.setenv("LMER_ANSWER", "answer to nothing")
        assert _main(["session-start"]) == 0
        out = capsys.readouterr().out
        assert "ANSWERED QUESTION" not in out
        events = [e["type"] for e in run_state.read_events(run_env, last_n=0)]
        assert "question_answered" not in events

    def test_lmer_answer_ignored_when_stop_reason_moved_on(self, run_env, capsys, monkeypatch):
        _ask_question(run_env)
        with patch("work_repo.cli.commit_work_path", return_value=0):
            _main(["state", "set", "--stop-reason=yield"])  # question resolved in-session
        monkeypatch.setenv("LMER_ANSWER", "stale answer")
        assert _main(["session-start"]) == 0
        assert "ANSWERED QUESTION" not in capsys.readouterr().out
    def test_archived_run_gets_direction_contract(self, run_env, capsys):
        # `archived` counts as finished: the slug still resolves until the
        # external cleaner moves the dir, so it must not silently resume.
        state = run_state.seed_state("develop-issue-123", "develop", "t")
        state["status"] = "archived"
        run_state.write_state(run_env, state)
        assert _main(["session-start"]) == 0
        out = capsys.readouterr().out
        assert "COMPLETED RUN" in out
        assert "work state set --stop-reason=question" in out
        assert run_state.load_state(run_env)["status"] == "archived"

    def test_newer_schema_refusal_is_not_reseeded_over(self, run_env, capsys):
        # The read-only refusal leaves the file intact — session-start must
        # NOT mistake it for the backed-up-corrupt case and write a schema-1
        # seed over a newer build's run (mixed-build fleets share work repos).
        state = run_state.seed_state("develop-issue-123", "develop", "t")
        state["schema"] = run_state.SCHEMA_VERSION + 1
        state["name"] = "kept-by-refusal"
        run_state.write_state(run_env, state)
        assert _main(["session-start"]) == 0  # still fail-soft for the hook
        out = capsys.readouterr().out
        assert "read-only refusal" in out
        assert "recovered" not in out.lower()  # nothing was backed up
        on_disk = yaml.safe_load((run_env / "state.yaml").read_text())
        assert on_disk["schema"] == run_state.SCHEMA_VERSION + 1
        assert on_disk["name"] == "kept-by-refusal"
        assert on_disk.get("owner") is None  # not claimed either
        assert not (run_env / "events.jsonl").exists()  # no session_start event


class TestSessionEnd:
    def test_no_context_soft_exit(self):
        assert _main(["session-end"]) == 0

    def test_no_run_soft_exit(self, run_env):
        assert _main(["session-end"]) == 0

    def test_clears_own_claim_logs_and_pushes(self, run_env):
        _main(["session-start"])
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            assert _main(["session-end"]) == 0
        state = run_state.load_state(run_env)
        assert state["owner"] is None
        events = run_state.read_events(run_env, last_n=0)
        assert events[-1]["type"] == "session_end"
        push.assert_called_once_with(
            ["git.example.com/org/repo/runs/develop-issue-123"],
            "run-state: session end develop-issue-123",
        )

    def test_foreign_claim_left_alone(self, run_env, monkeypatch):
        _main(["session-start"])
        monkeypatch.setenv("LMER_SESSION_ID", "s-cli-other")
        with patch("work_repo.cli.commit_work_path", return_value=0):
            _main(["session-end"])
        assert run_state.load_state(run_env)["owner"]["session_id"] == "s-cli-1"

    def test_push_failure_soft(self, run_env, capsys):
        _main(["session-start"])
        with patch("work_repo.cli.commit_work_path", return_value=1):
            assert _main(["session-end"]) == 0


class TestGoalKernelIntegration:
    def test_goal_lands_in_state(self, run_env):
        assert _main(["goal", "align on auth approach"]) == 0
        state = run_state.load_state(run_env)
        assert state["goal"] == "align on auth approach"
        events = run_state.read_events(run_env, last_n=0)
        assert any(e["type"] == "goal_set" for e in events)

    def test_goal_without_context_still_works(self, capsys):
        # Legacy behavior must be untouched when no run context exists.
        assert _main(["goal", "just a goal"]) == 0
        assert "Goal set" in capsys.readouterr().out

    def test_goal_display_unchanged(self, run_env, capsys):
        _main(["goal", "the goal text"])
        capsys.readouterr()
        assert _main(["goal"]) == 0
        assert "the goal text" in capsys.readouterr().out


class TestGoalEstimate:
    """`work goal --estimate-*` — session estimation (issue #99)."""

    def test_estimate_lands_in_state_and_event(self, run_env, capsys):
        assert _main(["goal", "fix the auth bug",
                      "--estimate-sessions", "2", "--estimate-time", "3h"]) == 0
        state = run_state.load_state(run_env)
        assert state["goal"] == "fix the auth bug"
        assert state["estimate"] == {"sessions": 2, "time": "3h"}
        event = [e for e in run_state.read_events(run_env, last_n=0)
                 if e["type"] == "goal_set"][-1]
        assert event["note"] == "fix the auth bug"
        assert event["data"] == {"estimate": {"sessions": 2, "time": "3h"}}
        assert "Estimate: ~2 sessions / 3h" in capsys.readouterr().out

    def test_sessions_only(self, run_env):
        assert _main(["goal", "small fix", "--estimate-sessions", "1"]) == 0
        assert run_state.load_state(run_env)["estimate"] == {
            "sessions": 1, "time": None}

    def test_time_only(self, run_env):
        assert _main(["goal", "medium fix", "--estimate-time", "2d"]) == 0
        assert run_state.load_state(run_env)["estimate"] == {
            "sessions": None, "time": "2d"}

    def test_goal_without_estimate_unchanged(self, run_env):
        # No flags: no estimate recorded, and the goal_set event stays
        # byte-for-byte what it was before #99 (no data payload).
        assert _main(["goal", "plain goal"]) == 0
        assert run_state.load_state(run_env)["estimate"] is None
        event = [e for e in run_state.read_events(run_env, last_n=0)
                 if e["type"] == "goal_set"][-1]
        assert "data" not in event

    def test_estimate_flags_require_description(self, run_env, capsys):
        assert _main(["goal", "--estimate-sessions", "2"]) == 1
        assert "goal description" in capsys.readouterr().err
        assert run_state.load_state(run_env) is None  # nothing written

    def test_estimate_sessions_must_be_positive(self, run_env, capsys):
        assert _main(["goal", "g", "--estimate-sessions", "0"]) == 1
        assert "positive" in capsys.readouterr().err
        assert run_state.load_state(run_env) is None

    def test_estimate_time_must_be_nonempty(self, run_env, capsys):
        assert _main(["goal", "g", "--estimate-time", "  "]) == 1
        assert "non-empty" in capsys.readouterr().err
        assert run_state.load_state(run_env) is None

    def test_brief_shows_estimate_with_used_count(self, run_env, capsys):
        _main(["goal", "fix the auth bug",
               "--estimate-sessions", "3", "--estimate-time", "4h"])
        run_state.append_event(run_env, "session_start")
        run_state.append_event(run_env, "session_end")
        run_state.append_event(run_env, "session_start")
        capsys.readouterr()
        assert _main(["resume"]) == 0
        assert "Estimate: ~3 sessions / 4h — used: 2 sessions" in capsys.readouterr().out

    def test_resume_json_carries_estimate_and_sessions_used(self, run_env, capsys):
        _main(["goal", "fix it", "--estimate-sessions", "2"])
        run_state.append_event(run_env, "session_start")
        capsys.readouterr()
        assert _main(["resume", "--json"]) == 0
        decision = json.loads(capsys.readouterr().out)
        assert decision["estimate"] == {"sessions": 2, "time": None}
        assert decision["sessions_used"] == 1

    def test_session_start_brief_counts_prior_sessions(self, run_env, capsys):
        _main(["goal", "fix it", "--estimate-sessions", "2"])
        _main(["session-start"])
        capsys.readouterr()
        # The second session's brief counts the first (its own session_start
        # lands after the decide — "used so far").
        assert _main(["session-start"]) == 0
        assert "Estimate: ~2 sessions — used: 1 session" in capsys.readouterr().out


class TestBinWorkExitCodes:
    """bin/work must propagate the CLI's exit code (it historically always exited 0)."""

    def test_failure_exit_code_propagates(self, run_env, tmp_path):
        env = dict(os.environ)
        result = subprocess.run(
            [str(Path(__file__).parent.parent / "bin" / "work"),
             "artifact", "spec.md", "--file", "/nonexistent/x.md"],
            env={**env, "LMER_PYTHON": sys.executable},
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 1, result.stderr

    def test_success_exit_code_propagates(self, run_env):
        env = dict(os.environ)
        result = subprocess.run(
            [str(Path(__file__).parent.parent / "bin" / "work"), "resume"],
            env={**env, "LMER_PYTHON": sys.executable},
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr


class TestArtifactProactivePush:
    def test_artifact_pushes_run_dir(self, run_env, tmp_path):
        src = tmp_path / "spec-src.md"
        src.write_text("# spec content here")
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            assert _main(["artifact", "spec.md", "--file", str(src)]) == 0
        push.assert_called_once_with(
            ["git.example.com/org/repo/runs/develop-issue-123"],
            "run-state: develop-issue-123 artifact spec.md",
        )

    def test_artifact_push_failure_soft(self, run_env, tmp_path, capsys):
        src = tmp_path / "spec-src.md"
        src.write_text("# spec content here")
        with patch("work_repo.cli.commit_work_path", return_value=1):
            assert _main(["artifact", "spec.md", "--file", str(src)]) == 0
        assert "saved locally" in capsys.readouterr().out


def _make_bundle(run_env, slug="develop-run-naming", files=("spec.md",)):
    """Fabricate a masterplan bundle dir with the given artifact files."""
    bundle = run_env / "masterplan" / slug
    bundle.mkdir(parents=True)
    for fname in files:
        (bundle / fname).write_text(f"# {fname} content\n")
    return bundle


class TestMasterplanArtifactSync:
    """Spec §6: masterplan artifact links self-maintained by the work layer."""

    def test_commit_creates_links_before_push(self, run_env):
        run_state.write_state(run_env, run_state.seed_state("develop-issue-123", "develop", "t"))
        _make_bundle(run_env, files=("spec.md", "plan.md"))
        linked_at_push = {}

        def fake_commit(message=None):
            linked_at_push["spec"] = (run_env / "spec.md").is_symlink()
            return 0

        with patch("work_repo.cli.commit_work_changes", side_effect=fake_commit) as push:
            assert _main(["commit"]) == 0
        push.assert_called_once_with(None)
        assert linked_at_push["spec"]  # sync ran BEFORE staging/push
        assert (run_env / "spec.md").is_symlink()
        assert os.readlink(run_env / "spec.md") == "masterplan/develop-run-naming/spec.md"
        assert (run_env / "plan.md").is_symlink()

    def test_commit_links_present_artifacts_only(self, run_env):
        run_state.write_state(run_env, run_state.seed_state("develop-issue-123", "develop", "t"))
        _make_bundle(run_env, files=("spec.md",))
        with patch("work_repo.cli.commit_work_changes", return_value=0):
            assert _main(["commit"]) == 0
        assert (run_env / "spec.md").is_symlink()
        for absent in ("goals.md", "plan.md", "plan.html", "retro.md"):
            assert not (run_env / absent).exists(), absent

    def test_session_end_creates_links_and_registers(self, run_env):
        _main(["session-start"])
        _make_bundle(run_env)
        with patch("work_repo.cli.commit_work_path", return_value=0) as push:
            assert _main(["session-end"]) == 0
        push.assert_called_once()
        assert (run_env / "spec.md").is_symlink()
        state = run_state.load_state(run_env)
        assert state["artifacts"]["spec"] == "spec.md"
        assert state["owner"] is None  # session-end semantics untouched

    def test_artifact_sync_links_and_registers(self, run_env, capsys):
        run_state.write_state(run_env, run_state.seed_state("develop-issue-123", "develop", "t"))
        _make_bundle(run_env, files=("spec.md", "retro.md"))
        assert _main(["artifact", "--sync"]) == 0
        out = capsys.readouterr().out
        assert "spec.md" in out and "retro.md" in out
        assert (run_env / "spec.md").is_symlink()
        assert (run_env / "retro.md").is_symlink()
        state = run_state.load_state(run_env)
        assert state["artifacts"]["spec"] == "spec.md"
        assert state["artifacts"]["retro"] == "retro.md"

    def test_artifact_sync_without_bundle_reports_nothing(self, run_env, capsys):
        run_state.write_state(run_env, run_state.seed_state("develop-issue-123", "develop", "t"))
        assert _main(["artifact", "--sync"]) == 0
        assert "no masterplan artifacts" in capsys.readouterr().out.lower()

    def test_artifact_sync_without_run_dir_soft_exit(self, run_env, capsys):
        # Env set but run dir never created — still exits 0.
        assert _main(["artifact", "--sync"]) == 0

    def test_artifact_sync_no_context_soft_exit(self, capsys):
        assert _main(["artifact", "--sync"]) == 0
        assert "no run context" in capsys.readouterr().out.lower()

    def test_artifact_sync_rejects_name_and_file(self, run_env, tmp_path, capsys):
        src = tmp_path / "x.md"
        src.write_text("content")
        assert _main(["artifact", "spec.md", "--sync"]) == 1
        assert _main(["artifact", "--sync", "--file", str(src)]) == 1
        err = capsys.readouterr().err
        assert "--sync" in err

    def test_bare_artifact_still_errors(self, run_env, capsys):
        assert _main(["artifact"]) == 1
        assert "name" in capsys.readouterr().err.lower()

    def test_artifact_name_without_file_errors(self, run_env, capsys):
        assert _main(["artifact", "spec.md"]) == 1
        assert "--file" in capsys.readouterr().err

    def test_commit_fail_soft_without_run_dir(self, run_env):
        # Run dir missing entirely — commit proceeds untouched.
        with patch("work_repo.cli.commit_work_changes", return_value=0) as push:
            assert _main(["commit"]) == 0
        push.assert_called_once_with(None)

    def test_commit_fail_soft_when_sync_raises(self, run_env, capsys):
        run_state.write_state(run_env, run_state.seed_state("develop-issue-123", "develop", "t"))
        _make_bundle(run_env)
        with patch("work_repo.run_state.sync_masterplan_artifacts",
                   side_effect=RuntimeError("boom")), \
             patch("work_repo.cli.commit_work_changes", return_value=0) as push:
            assert _main(["commit"]) == 0
        push.assert_called_once_with(None)
        assert "sync skipped" in capsys.readouterr().out.lower()

    def test_session_end_fail_soft_without_bundle(self, run_env):
        _main(["session-start"])
        with patch("work_repo.cli.commit_work_path", return_value=0):
            assert _main(["session-end"]) == 0
        assert not (run_env / "spec.md").exists()

    def test_session_end_fail_soft_without_run_dir(self, run_env):
        # Env set but run dir never created — same soft exit as before.
        assert _main(["session-end"]) == 0
