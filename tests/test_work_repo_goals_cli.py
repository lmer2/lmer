"""Tests for the `work goals` CLI verbs (issue #91 — frozen goal-sets).

The pure kernel rules are covered in tests/test_work_repo_goals.py; these
tests cover the CLI/IO shell: run-context handling, the freeze/amend/assess
lifecycle against real run dirs, event recording, the freeze-seam
invocation, push behavior, and exit codes.
"""
import os
from unittest.mock import patch

import pytest

from work_repo import cli as work_cli
from work_repo import goals, run_state


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("LMER_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _no_push():
    with patch("work_repo.cli.commit_work_path", return_value=0) as push:
        yield push


@pytest.fixture
def run_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
    monkeypatch.setenv("LMER_TASK", "develop")
    monkeypatch.setenv("LMER_TASK_TARGET", "https://git.example.com/org/repo/-/issues/91")
    monkeypatch.setenv("LMER_SESSION_ID", "s-goals-1")
    rdir = tmp_path / "git.example.com" / "org/repo" / "runs" / "develop-issue-91"
    rdir.mkdir(parents=True)
    run_state.write_state(rdir, run_state.seed_state("develop-issue-91", "develop", "t"))
    return rdir


GOOD_MD = (
    "topic: ship the thing\n"
    "\n"
    "## G1: Kernel lands\n"
    "signal: test\n"
    "evidence: tests/test_kernel.py\n"
    "\n"
    "## G2: Docs land\n"
    "signal: docs\n"
    "evidence: docs/FEATURE.md\n"
)

DRAFT_MD = (
    "## G1: Kernel lands\n"
    "signal: test\n"
    "\n"
    "## G2: Docs land\n"
)


def _main(argv):
    with patch.object(work_cli.sys, "argv", ["work"] + argv):
        return work_cli.main()


def _write_goals(rdir, text=GOOD_MD):
    (rdir / "goals.md").write_text(text, encoding="utf-8")


def _events(rdir, event_type=None):
    events = run_state.read_events(rdir, last_n=0)
    if event_type:
        events = [e for e in events if e.get("type") == event_type]
    return events


def _resolved_rdir():
    return run_state.run_dir()


class TestGoalsStatus:
    def test_no_run_context_exits_zero(self, capsys):
        assert _main(["goals"]) == 0
        assert "no run context" in capsys.readouterr().out.lower()

    def test_no_goals_md_exits_zero(self, run_env, capsys):
        assert _main(["goals"]) == 0
        assert "No goals.md" in capsys.readouterr().out

    def test_unfrozen_status(self, run_env, capsys):
        _write_goals(run_env)
        assert _main(["goals"]) == 0
        out = capsys.readouterr().out
        assert "2 active, 0 tombstoned" in out
        assert "Not frozen" in out

    def test_frozen_and_diverged_status(self, run_env, capsys):
        _write_goals(run_env)
        assert _main(["goals", "freeze"]) == 0
        assert _main(["goals"]) == 0
        assert "Frozen — goals.md matches" in capsys.readouterr().out
        _write_goals(run_env, GOOD_MD.replace("Kernel lands", "Kernel CHANGED"))
        assert _main(["goals"]) == 0
        assert "DIVERGED" in capsys.readouterr().out


class TestGoalsCheck:
    def test_no_run_context_exits_zero(self, capsys):
        assert _main(["goals", "check"]) == 0

    def test_missing_file_is_nothing_to_check(self, run_env, capsys):
        assert _main(["goals", "check"]) == 0
        assert "nothing to check" in capsys.readouterr().out

    def test_draft_contract_gaps_warn_but_stay_green(self, run_env, capsys):
        _write_goals(run_env, DRAFT_MD)
        assert _main(["goals", "check"]) == 0
        out = capsys.readouterr().out
        assert "⚠️" in out and "become" in out and "freeze" in out

    def test_structural_errors_exit_one(self, run_env, capsys):
        _write_goals(run_env, "## G1: a\nsignal: test\nevidence: e\n\n## G1: dup\n")
        assert _main(["goals", "check"]) == 1
        assert "goals check failed" in capsys.readouterr().out

    def test_complete_doc_is_green(self, run_env, capsys):
        _write_goals(run_env)
        assert _main(["goals", "check"]) == 0
        assert "✅ goals check green" in capsys.readouterr().out

    def test_check_rejects_note_flag(self, run_env, capsys):
        _write_goals(run_env)
        assert _main(["goals", "check", "--note", "x"]) == 1


class TestGoalsFreeze:
    def test_requires_run_context(self, capsys):
        assert _main(["goals", "freeze"]) == 1

    def test_requires_goals_md(self, run_env, capsys):
        assert _main(["goals", "freeze"]) == 1
        assert "No goals.md" in capsys.readouterr().err

    def test_draft_gaps_block_freeze(self, run_env, capsys):
        _write_goals(run_env, DRAFT_MD)
        assert _main(["goals", "freeze"]) == 1
        out = capsys.readouterr()
        assert "Cannot freeze" in out.err

    def test_freeze_records_event_artifact_and_frozen_stamp(self, run_env, capsys, _no_push):
        _write_goals(run_env)
        assert _main(["goals", "freeze", "--note", "approved in review thread"]) == 0
        assert "✅ Goals frozen: sha256:" in capsys.readouterr().out

        events = _events(run_env, "goals_frozen")
        assert len(events) == 1
        assert events[0]["note"] == "approved in review thread"
        data = events[0]["data"]
        assert data["goals_hash"] == goals.goals_hash(GOOD_MD)
        assert [g["id"] for g in data["goals"]] == ["G1", "G2"]
        assert data["goals"][0]["evidence"] == "tests/test_kernel.py"

        state = run_state.load_state(run_env)
        assert state["artifacts"]["goals"] == "goals.md"
        assert _no_push.called

    def test_freeze_renames_a_named_run_dir(self, run_env):
        state = run_state.load_state(run_env)
        state["name"] = "goal-run"
        run_state.write_state(run_env, state)
        _write_goals(run_env)
        assert _main(["goals", "freeze"]) == 0
        renamed = run_env.parent / "develop-issue-91--goal-run"
        assert renamed.is_dir() and not run_env.exists()
        assert _events(renamed, "goals_frozen")
        assert run_state.load_state(renamed)["frozen"]  # the seam fired

    def test_unnamed_run_leaves_the_seam_to_the_phase_gate(self, run_env, capsys):
        # The frozen stamp would forfeit the one-shot name-bearing rename
        # forever, and spec approval can precede the run being named.
        _write_goals(run_env)
        assert _main(["goals", "freeze"]) == 0
        assert "left to the phase gate" in capsys.readouterr().out
        assert run_state.load_state(run_env)["frozen"] is None
        assert _events(run_env, "goals_frozen")

    def test_freeze_rename_push_covers_the_old_dir_path(self, run_env, _no_push):
        state = run_state.load_state(run_env)
        state["name"] = "goal-run"
        run_state.write_state(run_env, state)
        _write_goals(run_env)
        assert _main(["goals", "freeze"]) == 0
        rels = _no_push.call_args[0][0]
        # Both the renamed dir and the pre-rename (bare-slug) dir must be
        # staged so the old path's deletions land even on a retry.
        assert any(rel.endswith("runs/develop-issue-91--goal-run") for rel in rels)
        assert any(rel.endswith("runs/develop-issue-91") for rel in rels)

    def test_freeze_redacts_secrets_from_the_event_payload(self, run_env):
        _write_goals(run_env, GOOD_MD.replace(
            "evidence: tests/test_kernel.py",
            "evidence: glpat-abc123def456ghi789jkl012 run log"))
        assert _main(["goals", "freeze"]) == 0
        data = _events(run_env, "goals_frozen")[0]["data"]
        assert "glpat-" not in data["goals"][0]["evidence"]
        assert "REDACTED" in data["goals"][0]["evidence"]

    def test_already_frozen_run_state_skips_seam_but_freezes_goals(self, run_env):
        state = run_state.load_state(run_env)
        state["frozen"] = "2026-07-06T00:00:00Z"
        run_state.write_state(run_env, state)
        _write_goals(run_env)
        assert _main(["goals", "freeze"]) == 0
        assert run_state.load_state(run_env)["frozen"] == "2026-07-06T00:00:00Z"

    def test_refreeze_is_an_error(self, run_env, capsys):
        _write_goals(run_env)
        assert _main(["goals", "freeze"]) == 0
        assert _main(["goals", "freeze"]) == 1
        assert "amend" in capsys.readouterr().err


class TestGoalsAmend:
    def test_requires_prior_freeze(self, run_env, capsys):
        _write_goals(run_env)
        assert _main(["goals", "amend"]) == 1
        assert "not frozen" in capsys.readouterr().err.lower()

    def test_unchanged_doc_is_a_noop(self, run_env, capsys):
        _write_goals(run_env)
        assert _main(["goals", "freeze"]) == 0
        assert _main(["goals", "amend"]) == 0
        assert "No changes to amend" in capsys.readouterr().out
        assert not _events(run_env, "goal_amended")

    def test_amend_records_diff_and_new_hash(self, run_env, capsys):
        _write_goals(run_env)
        assert _main(["goals", "freeze"]) == 0
        amended = GOOD_MD + "\n## G3: Lint lands\nsignal: command\nevidence: pre-commit run\n"
        _write_goals(run_env, amended)
        assert _main(["goals", "amend", "--note", "scope grew"]) == 0
        out = capsys.readouterr().out
        assert "G3: added" in out and "✅ Goals amended" in out

        events = _events(run_env, "goal_amended")
        assert len(events) == 1
        data = events[0]["data"]
        assert data["old_goals_hash"] == goals.goals_hash(GOOD_MD)
        assert data["new_goals_hash"] == goals.goals_hash(amended)
        assert data["diff"] == [{
            "id": "G3", "change": "added", "old": None,
            "new": {"text": "Lint lands", "signal": "command",
                    "evidence": "pre-commit run", "tombstone": None},
        }]

    def test_topic_seed_only_edit_amends_with_empty_diff(self, run_env, capsys):
        _write_goals(run_env)
        assert _main(["goals", "freeze"]) == 0
        _write_goals(run_env, GOOD_MD.replace("ship the thing", "ship the OTHER thing"))
        assert _main(["goals", "amend"]) == 0
        assert "topic-seed change" in capsys.readouterr().out
        data = _events(run_env, "goal_amended")[0]["data"]
        assert data["diff"] == []
        assert data["topic_seed"] == "ship the OTHER thing"

    def test_untombstoning_is_audited(self, run_env):
        tombstoned = GOOD_MD.replace(
            "## G2: Docs land\nsignal: docs\nevidence: docs/FEATURE.md\n",
            "## G2: Docs land\ntombstone_reason: descoped\n"
            "tombstone_at: 2026-07-01T00:00:00Z\n")
        _write_goals(run_env, tombstoned)
        assert _main(["goals", "freeze"]) == 0
        _write_goals(run_env)  # resurrect G2
        assert _main(["goals", "amend"]) == 0
        data = _events(run_env, "goal_amended")[0]["data"]
        assert [d["change"] for d in data["diff"]] == ["untombstoned"]

    def test_hard_deletion_blocks_amend(self, run_env, capsys):
        _write_goals(run_env)
        assert _main(["goals", "freeze"]) == 0
        _write_goals(run_env, GOOD_MD.split("\n## G2:")[0] + "\n")
        assert _main(["goals", "amend"]) == 1
        assert "tombstone" in capsys.readouterr().out

    def test_amend_chain_uses_latest_agreed_set(self, run_env):
        _write_goals(run_env)
        assert _main(["goals", "freeze"]) == 0
        v2 = GOOD_MD + "\n## G3: c\nsignal: docs\nevidence: e\n"
        _write_goals(run_env, v2)
        assert _main(["goals", "amend"]) == 0
        # Renumbering against the AMENDED max (G3) must now be rejected.
        v3 = GOOD_MD + "\n## G3: c\nsignal: docs\nevidence: e\n\n## G2: dup\n"
        _write_goals(run_env, v3)
        assert _main(["goals", "amend"]) == 1


class TestGoalsAssess:
    def _freeze(self, rdir):
        _write_goals(rdir)
        assert _main(["goals", "freeze"]) == 0

    def test_bare_prints_skeleton_read_only(self, run_env, capsys, _no_push):
        self._freeze(run_env)
        _no_push.reset_mock()
        events_before = len(_events(run_env))
        assert _main(["goals", "assess"]) == 0
        out = capsys.readouterr().out
        assert "## Goal verdicts" in out
        assert "met / partial / missed / waived" in out
        assert len(_events(run_env)) == events_before
        _no_push.assert_not_called()

    def test_bare_reports_divergence(self, run_env, capsys):
        self._freeze(run_env)
        _write_goals(run_env, GOOD_MD.replace("Docs land", "Docs changed"))
        assert _main(["goals", "assess"]) == 0
        assert "DIVERGED" in capsys.readouterr().out

    def test_never_frozen_warns_but_proceeds(self, run_env, capsys):
        _write_goals(run_env)
        assert _main(["goals", "assess"]) == 0
        assert "never frozen" in capsys.readouterr().out

    def test_bare_assess_is_gentle_recording_is_strict(self, run_env, capsys):
        # Bare form is the nudge path: missing or draft-grade goals.md is a
        # note, never a failure. The --verdict form stays strict.
        assert _main(["goals", "assess"]) == 0
        assert "nothing to assess" in capsys.readouterr().out
        assert _main(["goals", "assess", "--verdict", "G1=met:x"]) == 1

        _write_goals(run_env, DRAFT_MD)
        assert _main(["goals", "assess"]) == 0
        out = capsys.readouterr().out
        assert "fails strict validation" in out
        assert "## Goal verdicts" in out  # skeleton still printed
        assert _main(["goals", "assess", "--verdict", "G1=met:x",
                      "--verdict", "G2=met:y"]) == 1

    def test_incomplete_verdict_map_errors(self, run_env, capsys):
        self._freeze(run_env)
        assert _main(["goals", "assess", "--verdict", "G1=met:tests"]) == 1
        assert "G2" in capsys.readouterr().out
        assert not _events(run_env, "goals_assessed")

    def test_malformed_and_duplicate_flags_error(self, run_env, capsys):
        self._freeze(run_env)
        assert _main(["goals", "assess", "--verdict", "G1=met"]) == 1
        assert _main(["goals", "assess",
                      "--verdict", "G1=met:a", "--verdict", "G1=missed:b"]) == 1

    def test_recording_classifies_evidence_and_appends_event(self, run_env, capsys, _no_push):
        self._freeze(run_env)
        run_state.append_event(
            run_env, "verify", note="tests: pass",
            data={"name": "t1-tests", "exit_code": 0})
        assert _main([
            "goals", "assess",
            "--verdict", "G1=met:t1-tests receipt green",
            "--verdict", "G2=partial:reads well to me",
        ]) == 0
        out = capsys.readouterr().out
        assert "| G1 | Kernel lands | met | t1-tests receipt green |" in out
        assert "| G2 | Docs land | partial | reads well to me (prose) |" in out
        assert "✅ Goals assessed: met 1, partial 1" in out

        events = _events(run_env, "goals_assessed")
        assert len(events) == 1
        data = events[0]["data"]
        assert data["diverged"] is False
        assert data["goals_hash"] == goals.goals_hash(GOOD_MD)
        assert data["verdicts"]["G1"]["evidence_kind"] == "receipt"
        assert data["verdicts"]["G2"]["evidence_kind"] == "prose"
        assert _no_push.called

    def test_recording_marks_divergence_in_event(self, run_env):
        self._freeze(run_env)
        _write_goals(run_env, GOOD_MD.replace("Docs land", "Docs changed"))
        assert _main([
            "goals", "assess",
            "--verdict", "G1=met:a", "--verdict", "G2=missed:b",
        ]) == 0
        data = _events(run_env, "goals_assessed")[0]["data"]
        assert data["diverged"] is True
        assert data["last_agreed_hash"] == goals.goals_hash(GOOD_MD)

    def test_verdict_flag_outside_assess_errors(self, run_env):
        _write_goals(run_env)
        assert _main(["goals", "check", "--verdict", "G1=met:x"]) == 1
        assert _main(["goals", "freeze", "--verdict", "G1=met:x"]) == 1
