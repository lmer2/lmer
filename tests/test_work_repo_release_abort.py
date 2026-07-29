"""Tests for `work release abort` — the explicit aborted-run transition
(masterplan release-flow §7: the bump merged to `prep-release`, the human
declined the release MR).

Abort is a terminal `stop_reason: aborted` on a `status: complete` run,
deliberately NOT a fourth STATUSES value (the enum the external cleaner
and the completed-run resume policy key on — see abort_run's design
comment). ONE atomic state write flips status/stop_reason/claim together,
releasing the single-flight lock; release.yaml gains a terminal `aborted`
marker so derive_leg() reports nothing to advance while every recorded
field — above all the bump-MR merge SHA — survives.

Post-abort invariants (the plan's contract): a fresh claim succeeds after
an abort, a subsequent session-start reports the completed run instead of
resurrecting it, and leg 1 stays re-derivable so the next run's ctl
dry-run skips the already-done bump. CLI tests patch the git plumbing
seams exactly as tests/test_work_repo_run_claim_cli.py does.
"""
import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from work_repo import cli as work_cli
from work_repo import release_run, run_state
from work_repo.git_ops import (
    CLAIM_PUSH_ERROR,
    CLAIM_PUSH_LOST_RACE,
    CLAIM_PUSH_WON,
)
from tests.conftest import strip_lmer_env

SHA_BUMP = "a" * 40


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture
def run_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path))
    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
    monkeypatch.setenv("LMER_TASK", "release")
    monkeypatch.setenv("LMER_TASK_TARGET", "main")
    monkeypatch.setenv("LMER_SESSION_ID", "s-abort-1")
    return tmp_path / "git.example.com" / "org/repo" / "runs" / "release-main"


def _main(argv):
    with patch.object(work_cli.sys, "argv", ["work"] + argv):
        return work_cli.main()


def _cas_seams(stack, push_outcomes=None, sync=(True, "")):
    """Patch the git plumbing seams so the CAS loop runs against the plain
    tmp-dir work repo (no git). Returns (push_mock, drop_mock)."""
    stack.enter_context(
        patch("work_repo.cli._sync_remote_head", return_value=sync))
    stack.enter_context(
        patch("work_repo.cli._commit_claim_write", return_value=(True, "")))
    stack.enter_context(
        patch("work_repo.cli._git_head", return_value="pre-abort-sha"))
    drop = stack.enter_context(patch("work_repo.cli._drop_claim_commit"))
    push = stack.enter_context(
        patch("work_repo.cli.claim_push_once",
              side_effect=list(push_outcomes or [(CLAIM_PUSH_WON, "")])))
    return push, drop


def _seed_leg1_done(run_env, session="s-abort-1", claim=True):
    """The abandoned-release state: leg 1 recorded (version + bump SHA),
    run in-progress on the gate, claim held — then the human declines."""
    state = run_state.seed_state("release-main", "release", "main")
    state["phase"] = "watch"
    if claim:
        state["claim"] = {
            "session_id": session,
            "claimed_at": run_state.utc_now_iso(),
        }
    run_state.write_state(run_env, state)
    release_run.record_version(run_env, "0.5.0")
    release_run.record_bump_merge(run_env, SHA_BUMP)
    return run_state.load_state(run_env)


def _abort_events(run_env):
    return [e for e in run_state.read_events(run_env, last_n=0)
            if e["type"] == "run_aborted"]


class TestAbortKernel:
    """run_state.abort_run — the atomic state half of the transition."""

    def test_one_write_flips_status_stop_reason_and_claim(self, run_env):
        state = _seed_leg1_done(run_env)
        run_state.abort_run(run_env, state, reason="release MR declined")
        after = run_state.load_state(run_env)
        assert after["status"] == "complete"
        assert after["stop_reason"] == "aborted"
        assert after["claim"] is None  # the lock is free, no dangling block
        event = _abort_events(run_env)[-1]
        assert event["data"]["reason"] == "release MR declined"
        assert event["data"]["cleared_claim_holder"] == "s-abort-1"

    def test_aborted_is_a_stop_reason_not_a_status(self, run_env):
        # The design decision: `status` stays inside the closed enum the
        # external cleaner and the completed-run policy key on (schema
        # stays 1); `aborted` extends the descriptive stop_reason axis.
        state = _seed_leg1_done(run_env)
        run_state.abort_run(run_env, state)
        after = run_state.load_state(run_env)
        assert after["status"] in run_state.STATUSES
        assert "aborted" not in run_state.STATUSES
        assert "aborted" in run_state.STOP_REASONS

    def test_abort_releases_the_single_flight_lock(self, run_env):
        # §7: a claim is valid only while the run is in-progress — after
        # the abort a NEW session's claim verdict reads unclaimed.
        state = _seed_leg1_done(run_env, session="s-abort-1")
        state = run_state.abort_run(run_env, state)
        verdict = run_state.claim_status(state, "s-next-run")
        assert verdict["verdict"] == run_state.CLAIM_UNCLAIMED

    def test_live_foreign_claim_refuses_without_force(self, run_env):
        # A live foreign claim means another session is mid-release RIGHT
        # NOW — a different fact from "the human declined". Same knob and
        # wording as unclaim_run; nothing is written on the refusal.
        state = _seed_leg1_done(run_env, session="s-holder")
        with pytest.raises(run_state.ClaimRefusedError) as excinfo:
            run_state.abort_run(run_env, state, session="s-other")
        assert "--force overrides" in str(excinfo.value)
        assert excinfo.value.pointer["holder"] == "s-holder"
        assert run_state.load_state(run_env)["status"] == "in-progress"

    def test_live_foreign_claim_aborts_with_force(self, run_env):
        state = _seed_leg1_done(run_env, session="s-holder")
        state = run_state.abort_run(
            run_env, state, session="s-other", force=True)
        assert state["status"] == "complete"
        assert state["stop_reason"] == "aborted"
        assert state["claim"] is None
        data = _abort_events(run_env)[-1]["data"]
        assert data["cleared_claim_holder"] == "s-holder"
        assert data["forced"] is True

    def test_stale_foreign_claim_aborts_without_force(self, run_env):
        # Staleness is the takeover case, not the refusal case: a crashed
        # or reaped holder must not need --force to clean up after.
        state = _seed_leg1_done(run_env, session="s-holder")
        state["claim"]["claimed_at"] = "2020-01-01T00:00:00Z"
        run_state.write_state(run_env, state)
        state = run_state.abort_run(run_env, state, session="s-other")
        assert state["stop_reason"] == "aborted"
        assert _abort_events(run_env)[-1]["data"]["forced"] is False

    def test_reabort_is_noop_no_write_no_event(self, run_env):
        state = _seed_leg1_done(run_env)
        state = run_state.abort_run(run_env, state)
        updated = run_state.load_state(run_env)["updated"]
        n_events = len(_abort_events(run_env))
        run_state.abort_run(run_env, state, reason="again")
        assert len(_abort_events(run_env)) == n_events
        assert run_state.load_state(run_env)["updated"] == updated

    def test_run_finished_another_way_refuses(self, run_env):
        # Aborting a run that completed normally would falsify its
        # recorded outcome.
        state = _seed_leg1_done(run_env, claim=False)
        state["status"] = "complete"
        state["stop_reason"] = "complete"
        run_state.write_state(run_env, state)
        with pytest.raises(run_state.RunStateError, match="nothing to abort"):
            run_state.abort_run(run_env, state)

    def test_clawless_run_aborts_without_holder_field(self, run_env):
        state = _seed_leg1_done(run_env, claim=False)
        run_state.abort_run(run_env, state)
        event = _abort_events(run_env)[-1]
        assert "cleared_claim_holder" not in event["data"]


class TestRecordAbort:
    """release_run.record_abort — the terminal release.yaml half."""

    def test_marks_terminal_and_keeps_leg1_record(self, run_env):
        _seed_leg1_done(run_env)
        release_run.record_abort(run_env, reason="declined")
        release = release_run.load_release(run_env)
        assert release["aborted"]["reason"] == "declined"
        assert release["aborted"]["at"].endswith("Z")
        # The recorded bump SHA survives — the next run's ctl dry-run
        # needs it to skip the already-done bump (spec §7).
        assert release["version"] == "0.5.0"
        assert release["bump_mr_merge_sha"] == SHA_BUMP

    def test_derive_leg_reports_nothing_to_advance(self, run_env):
        _seed_leg1_done(run_env)
        release_run.record_abort(run_env)
        derived = release_run.derive_leg(release_run.load_release(run_env))
        assert derived["leg"] == release_run.LEG_ABORTED
        assert derived["next_step"] is None
        assert derived["aborted"] is True
        # Leg 1 stays re-derivable through the same derived view.
        assert derived["version"] == "0.5.0"
        assert derived["bump_merged"] is True

    def test_aborted_is_not_a_ladder_step(self):
        # The frozen decision table cites STEPS one row per step — the
        # terminal abort is deliberately not a resume row.
        assert release_run.LEG_ABORTED not in release_run.STEPS
        assert release_run.LEG_ABORTED not in release_run.LEGS

    def test_reabort_is_noop_preserving_first_record(self, run_env):
        _seed_leg1_done(run_env)
        release_run.record_abort(run_env, reason="first")
        first = release_run.load_release(run_env)["aborted"]
        release_run.record_abort(run_env, reason="second")
        assert release_run.load_release(run_env)["aborted"] == first

    def test_status_view_renders_terminal(self, run_env):
        _seed_leg1_done(run_env)
        release_run.record_abort(run_env, reason="declined")
        text = release_run.format_release_status(
            release_run.load_release(run_env))
        assert "aborted (terminal; nothing to advance)" in text
        assert f"bump-MR merge SHA     {SHA_BUMP}" in text
        assert "declined" in text

    def test_non_mapping_aborted_field_counts_as_corrupt(self, run_env):
        run_env.mkdir(parents=True)
        (run_env / "release.yaml").write_text(
            "schema: 1\naborted: definitely\n")
        with pytest.raises(run_state.RunStateError, match="aborted field"):
            release_run.load_release(run_env)
        assert list(run_env.glob("release.yaml.bad-*"))


class TestAbortCLI:
    def test_no_context_exits_one(self, capsys):
        # Mutating-verb contract: no run context is an error, exit 1.
        assert _main(["release", "abort"]) == 1
        assert "no run context" in capsys.readouterr().err.lower()

    def test_no_run_recorded_exits_one(self, run_env, capsys):
        with ExitStack() as stack:
            push, _ = _cas_seams(stack)
            assert _main(["release", "abort"]) == 1
        assert "nothing to abort" in capsys.readouterr().err
        push.assert_not_called()
        assert run_state.load_state(run_env) is None  # never seeded

    def test_aborts_clears_claim_and_pushes(self, run_env, capsys):
        _seed_leg1_done(run_env)
        with ExitStack() as stack:
            push, _ = _cas_seams(stack)
            assert _main(["release", "abort", "--reason", "declined"]) == 0
        state = run_state.load_state(run_env)
        assert state["status"] == "complete"
        assert state["stop_reason"] == "aborted"
        assert state["claim"] is None
        assert release_run.load_release(run_env)["aborted"]["reason"] == "declined"
        push.assert_called_once()
        out = capsys.readouterr().out
        assert "Release run aborted: release-main" in out
        assert "claim cleared: session s-abort-1" in out
        assert "Bump commit stays on prep-release" in out
        # The abort is a terminal write and nothing more: this run keeps its
        # address until the next claim rolls it aside (version-in-slug).
        assert "rolls this run aside" in out

    def test_already_aborted_noop_success_no_push(self, run_env, capsys):
        _seed_leg1_done(run_env)
        with ExitStack() as stack:
            _cas_seams(stack)
            assert _main(["release", "abort"]) == 0
        with ExitStack() as stack:
            push, _ = _cas_seams(stack)
            assert _main(["release", "abort"]) == 0
        assert "already aborted" in capsys.readouterr().out
        push.assert_not_called()

    def test_completed_run_refused_before_any_write(self, run_env, capsys):
        state = _seed_leg1_done(run_env, claim=False)
        state["status"] = "complete"
        state["stop_reason"] = "complete"
        run_state.write_state(run_env, state)
        with ExitStack() as stack:
            push, _ = _cas_seams(stack)
            assert _main(["release", "abort"]) == 1
        assert "nothing to abort" in capsys.readouterr().err
        push.assert_not_called()
        # A refused abort never marks the release record terminal.
        assert release_run.load_release(run_env).get("aborted") is None
        assert run_state.load_state(run_env)["stop_reason"] == "complete"

    def test_live_foreign_claim_refuses_without_force(self, run_env, capsys):
        # The bypass this closes: session B is correctly refused at
        # `work release claim`, then takes the decline path and marks A's
        # in-flight release terminal, freeing A's lock remotely. The guard
        # runs BEFORE record_abort, so the release record is untouched too.
        _seed_leg1_done(run_env, session="s-holder")
        with ExitStack() as stack:
            _cas_seams(stack)
            assert _main(["release", "abort"]) == 1
        err = capsys.readouterr().err
        assert "--force" in err
        assert "s-holder" in err
        state = run_state.load_state(run_env)
        assert state["status"] == "in-progress"
        assert state["claim"]["session_id"] == "s-holder"
        assert release_run.load_release(run_env).get("aborted") is None

    def test_foreign_claim_cleared_and_named_with_force(self, run_env, capsys):
        # --force is the human runbook path (the holder is known dead);
        # the audit event names the displaced holder.
        _seed_leg1_done(run_env, session="s-holder")
        with ExitStack() as stack:
            _cas_seams(stack)
            assert _main(["release", "abort", "--force"]) == 0
        assert run_state.load_state(run_env)["claim"] is None
        assert _abort_events(run_env)[-1]["data"]["cleared_claim_holder"] == "s-holder"
        assert "claim cleared: session s-holder" in capsys.readouterr().out

    def test_sync_failure_fails_closed(self, run_env, capsys):
        _seed_leg1_done(run_env)
        with ExitStack() as stack:
            _cas_seams(stack, sync=(False, "git fetch failed: boom"))
            assert _main(["release", "abort"]) == 1
        assert "fail closed" in capsys.readouterr().err
        state = run_state.load_state(run_env)
        assert state["status"] == "in-progress"  # nothing written
        assert state["claim"]["session_id"] == "s-abort-1"

    def test_push_error_fails_closed(self, run_env, capsys):
        _seed_leg1_done(run_env)
        with ExitStack() as stack:
            _cas_seams(stack, push_outcomes=[(CLAIM_PUSH_ERROR, "403")])
            assert _main(["release", "abort"]) == 1
        err = capsys.readouterr().err
        assert "fail closed" in err
        assert "403" in err

    def test_lost_race_retries_then_wins(self, run_env):
        _seed_leg1_done(run_env)
        with ExitStack() as stack:
            push, drop = _cas_seams(stack, push_outcomes=[
                (CLAIM_PUSH_LOST_RACE, "[rejected] non-fast-forward"),
                (CLAIM_PUSH_WON, ""),
            ])
            # The real drop is `git reset --hard` — it restores the
            # pre-abort state; mimic that so the retry re-applies the
            # transition (the claim-verb integration tests prove the real
            # rollback plumbing).
            drop.side_effect = lambda pre_head: _seed_leg1_done(run_env)
            assert _main(["release", "abort"]) == 0
        assert push.call_count == 2
        drop.assert_called_once_with("pre-abort-sha")
        state = run_state.load_state(run_env)
        assert state["stop_reason"] == "aborted"
        assert state["claim"] is None


class TestPostAbortInvariants:
    """The plan's post-abort contract, end to end through the CLI."""

    def _abort(self, run_env, session="s-abort-1"):
        _seed_leg1_done(run_env, session=session)
        with ExitStack() as stack:
            _cas_seams(stack)
            assert _main(["release", "abort", "--reason", "declined"]) == 0

    def test_the_aborted_run_is_never_re_claimed_as_itself(
            self, run_env, monkeypatch, capsys):
        """An aborted run is TERMINAL — it is never handed back out.

        Re-claiming it would be a lock with no mutual exclusion:
        claim_status reads every claim on a non-in-progress run as
        unclaimed and claim_run never restores in-progress, so two sessions
        would both be told "claim taken" and both drive leg 1. The
        abandoned-release contract does not need that: the bump stays on
        prep-release and the NEXT release run's dry-run skips it — so the
        claim rolls this run aside to its version-bearing address and seeds
        that next run instead of dead-ending on this one."""
        self._abort(run_env)
        capsys.readouterr()
        monkeypatch.setenv("LMER_SESSION_ID", "s-next-run")
        with ExitStack() as stack:
            _cas_seams(stack)
            assert _main(["release", "claim"]) == 0
        assert "rolled aside" in capsys.readouterr().err

        aborted = run_env.parent / "release-main-v0.5.0"
        after = run_state.load_state(aborted)
        assert after["stop_reason"] == "aborted"
        assert after["claim"] is None  # never claimed, here or anywhere
        successor = run_state.load_state(run_env)
        assert successor["status"] == "in-progress"
        assert successor["claim"]["session_id"] == "s-next-run"
        assert release_run.load_release(run_env) is None  # a fresh leg 1

    def test_two_sessions_cannot_both_claim_across_one_aborted_run(
            self, run_env, monkeypatch, capsys):
        """The reported defect, stated as its own guard: with the old
        exemption in place both sessions exited 0 on the same aborted run,
        and the release taskdef reads exit 0 as "proceed". The roll-over
        must not reintroduce it — the second session meets the first's LIVE
        claim on the successor run."""
        self._abort(run_env)
        capsys.readouterr()
        codes = []
        for session in ("s-a", "s-b"):
            monkeypatch.setenv("LMER_SESSION_ID", session)
            with ExitStack() as stack:
                _cas_seams(stack)
                codes.append(_main(["release", "claim"]))
        assert codes.count(0) <= 1, (
            f"both sessions were told the claim is theirs: {codes}"
        )
        assert codes == [0, 1]
        assert run_state.load_state(run_env)["claim"]["session_id"] == "s-a"
        assert "s-a" in capsys.readouterr().err  # the loser gets the pointer

    def test_session_start_does_not_resurrect_the_run(self, run_env, capsys):
        self._abort(run_env)
        capsys.readouterr()
        assert _main(["session-start"]) == 0
        out = capsys.readouterr().out
        # The completed-run direction contract renders — never a silent
        # resume — and the release block shows the terminal position.
        assert "COMPLETED RUN" in out
        assert "Do NOT resume it" in out
        assert "aborted" in out
        after = run_state.load_state(run_env)
        assert after["status"] == "complete"
        assert after["stop_reason"] == "aborted"

    def test_decide_flags_completed_run(self, run_env):
        self._abort(run_env)
        state = run_state.load_state(run_env)
        decision = run_state.decide(
            state, [], "s-next-run",
            release=release_run.load_release(run_env),
        )
        assert decision["completed_run"] is True
        assert decision["release"]["leg"] == release_run.LEG_ABORTED
        assert decision["release"]["next_step"] is None
        assert decision["claim"] is None  # no claim block survives

    def test_bump_sha_survives_for_leg1_rederivation(self, run_env):
        self._abort(run_env)
        release = release_run.load_release(run_env)
        assert release["version"] == "0.5.0"
        assert release["bump_mr_merge_sha"] == SHA_BUMP
        derived = release_run.derive_leg(release)
        assert derived["bump_merged"] is True  # leg 1 reads as already done
        assert derived["next_step"] is None  # but nothing to advance here

    def test_status_json_reports_nothing_to_advance(self, run_env, capsys):
        self._abort(run_env)
        capsys.readouterr()
        assert _main(["release", "status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["leg"] == release_run.LEG_ABORTED
        assert payload["next_step"] is None
        assert payload["aborted"] is True
        assert payload["abort_reason"] == "declined"
