"""Tests for the `work release` claim verbs (RUN-STATE.md §7).

The single-flight release claim, wired into the single-writer work CLI:
`work release claim` composes the kernel claim (run_state.claim_run) with
the claim-by-push CAS (git_ops.claim_push_once) — exit 0 means the claim is
held (fresh win, holder refresh, or loud stale takeover); NON-ZERO means a
live foreign claim holds the run (the loser's pointer prints — this is the
exit code the release taskdef's refusal keys on) or the claim could not be
established (fail closed). `claim-status` is read-only and always exits 0;
`unclaim` releases our claim (foreign needs --force; no claim is a no-op
success).

Unit tests patch the CLI's git plumbing seams so the CAS loop runs against
a plain tmp-dir work repo; the integration tests use a real bare origin
plus two clones — the two-simultaneous-launches topology — to prove the
end-to-end arbitration (mirroring tests/test_work_repo_claim_push.py).
"""
import json
import subprocess
from contextlib import ExitStack
from unittest.mock import patch

import pytest
import yaml

from work_repo import cli as work_cli
from work_repo import release_run, run_state
from work_repo.git_ops import (
    CLAIM_PUSH_ERROR,
    CLAIM_PUSH_LOST_RACE,
    CLAIM_PUSH_WON,
)
from tests.conftest import strip_lmer_env


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
    monkeypatch.setenv("LMER_SESSION_ID", "s-claim-1")
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
        patch("work_repo.cli._git_head", return_value="pre-claim-sha"))
    drop = stack.enter_context(patch("work_repo.cli._drop_claim_commit"))
    push = stack.enter_context(
        patch("work_repo.cli.claim_push_once",
              side_effect=list(push_outcomes or [(CLAIM_PUSH_WON, "")])))
    return push, drop


def _snapshot(runs):
    """Every file under runs/, keyed by relative path — the tree a push
    publishes and a fetch installs."""
    return {
        str(p.relative_to(runs)): p.read_bytes()
        for p in sorted(runs.rglob("*")) if p.is_file()
    }


def _run_dir_names(snapshot):
    """The run dirs a snapshot contains — the tracked set a head carries."""
    return {rel.split("/", 1)[0] for rel in snapshot}


def _restore(runs, snapshot, tracked=None):
    """What `git reset --hard <pre_head>` leaves on disk.

    Reset restores the TRACKED tree and does not touch untracked paths — so
    a dir the verb created but never staged survives the drop. `tracked` is
    the set of runs/-relative dir names the claim commit staged; everything
    outside it stays exactly where it is. `None` means the whole tree is
    being replaced, which is the fetch case (a head another session pushed).
    """
    def _staged(rel):
        return tracked is None or any(
            rel == name or rel.startswith(f"{name}/") for name in tracked)

    for path in sorted(runs.rglob("*"), reverse=True):  # children before dirs
        rel = str(path.relative_to(runs))
        if path.is_file():
            if _staged(rel):
                path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    for rel, blob in snapshot.items():
        path = runs / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)


def _seed_claimed(run_env, session="s-holder", claimed_at=None,
                  status="in-progress"):
    """A run whose state already carries a claim block (the lock object)."""
    state = run_state.seed_state("release-main", "release", "main")
    state["status"] = status
    state["phase"] = "watch"
    state["claim"] = {
        "session_id": session,
        "claimed_at": claimed_at or run_state.utc_now_iso(),
    }
    run_state.write_state(run_env, state)
    return state


def _claim_events(run_env):
    return [e for e in run_state.read_events(run_env, last_n=0)
            if e["type"] == "claim"]


class TestClaim:
    def test_no_context_exits_one(self, capsys):
        # Mutating-verb contract: no run context is an error, exit 1.
        assert _main(["release", "claim"]) == 1
        assert "no run context" in capsys.readouterr().err.lower()

    def test_fresh_claim_wins_and_seeds(self, run_env, capsys):
        with ExitStack() as stack:
            push, _ = _cas_seams(stack)
            assert _main(["release", "claim"]) == 0
        state = run_state.load_state(run_env)
        assert state["claim"]["session_id"] == "s-claim-1"
        assert state["claim"]["claimed_at"].endswith("Z")
        events = run_state.read_events(run_env, last_n=0)
        assert events[0]["type"] == "run_seeded"  # auto-seeded, mutating-verb style
        assert _claim_events(run_env)[-1]["data"]["action"] == "claim"
        push.assert_called_once()
        assert "Release claim taken" in capsys.readouterr().out

    def test_win_json_shape(self, run_env, capsys):
        with ExitStack() as stack:
            _cas_seams(stack)
            assert _main(["release", "claim", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["result"] == "won"
        assert payload["action"] == "claim"
        assert payload["slug"] == "release-main"
        assert payload["session_id"] == "s-claim-1"
        assert payload["claimed_at"].endswith("Z")
        assert payload["run_dir"] == str(run_env)

    def test_holder_refresh_is_idempotent_win(self, run_env, capsys):
        # The holder keeps the claim live by re-claiming (§7): exit 0,
        # claimed_at refreshed, `claim` event records the refresh.
        _seed_claimed(run_env, session="s-claim-1",
                      claimed_at="2020-01-01T00:00:00Z")
        with ExitStack() as stack:
            _cas_seams(stack)
            assert _main(["release", "claim"]) == 0
        state = run_state.load_state(run_env)
        assert state["claim"]["session_id"] == "s-claim-1"
        assert state["claim"]["claimed_at"] != "2020-01-01T00:00:00Z"
        assert _claim_events(run_env)[-1]["data"]["action"] == "refresh"
        assert "Release claim refreshed" in capsys.readouterr().out

    def test_live_foreign_claim_lost_with_pointer(self, run_env, capsys):
        # THE refusal the release taskdef keys on: live foreign claim →
        # exit non-zero, active-run pointer printed, NOTHING written/pushed.
        _seed_claimed(run_env, session="s-holder")
        with ExitStack() as stack:
            push, _ = _cas_seams(stack)
            assert _main(["release", "claim"]) == 1
        err = capsys.readouterr().err
        assert "s-holder" in err
        assert "release-main" in err
        assert str(run_env) in err
        assert "claimed_at" in err
        push.assert_not_called()  # nothing pushed on refusal
        state = run_state.load_state(run_env)
        assert state["claim"]["session_id"] == "s-holder"  # lock intact
        assert not _claim_events(run_env)  # nothing written either

    def test_lost_pointer_json_shape(self, run_env, capsys):
        _seed_claimed(run_env, session="s-holder")
        with ExitStack() as stack:
            _cas_seams(stack)
            assert _main(["release", "claim", "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["result"] == "lost"
        assert payload["slug"] == "release-main"
        assert payload["run_dir"] == str(run_env)
        assert payload["holder"] == "s-holder"
        assert payload["claimed_at"].endswith("Z")
        assert payload["age_minutes"] is not None
        assert payload["status"] == "in-progress"
        assert payload["phase"] == "watch"
        assert "web_url" in payload  # None here — no git remote to derive from

    def test_stale_foreign_claim_taken_over_loudly(self, run_env, capsys):
        # Past RELEASE_CLAIM_STALE_MINUTES the holder crashed/was reaped:
        # the next claimant takes over automatically, naming the displaced
        # session (§7 — the unattended scheduled relaunch must get through).
        _seed_claimed(run_env, session="s-dead",
                      claimed_at="2020-01-01T00:00:00Z")
        with ExitStack() as stack:
            _cas_seams(stack)
            assert _main(["release", "claim"]) == 0
        out = capsys.readouterr().out
        assert "takeover" in out
        assert "s-dead" in out
        state = run_state.load_state(run_env)
        assert state["claim"]["session_id"] == "s-claim-1"
        event = _claim_events(run_env)[-1]
        assert event["data"]["action"] == "takeover"
        assert event["data"]["displaced_session"] == "s-dead"

    def test_takeover_json_names_displaced_session(self, run_env, capsys):
        _seed_claimed(run_env, session="s-dead",
                      claimed_at="2020-01-01T00:00:00Z")
        with ExitStack() as stack:
            _cas_seams(stack)
            assert _main(["release", "claim", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["action"] == "takeover"
        assert payload["displaced_session"] == "s-dead"

    def test_completed_run_rolls_over_to_a_fresh_run(self, run_env, capsys):
        # A finished run parked on this address is the PREVIOUS release, not
        # this one. Run identity is deterministic per (taskdef, target), so
        # refusing here meant a repository could release exactly once. The
        # claim rolls the finished run aside to its version-bearing slug and
        # claims a fresh run at the freed address, in one CAS commit.
        _seed_claimed(run_env, session="s-holder", status="complete")
        release_run.record_version(run_env, "0.5.0")
        with ExitStack() as stack:
            push, _ = _cas_seams(stack)
            assert _main(["release", "claim"]) == 0
        captured = capsys.readouterr()
        assert "rolled aside to 'release-main-v0.5.0'" in captured.err
        assert "✅ Release claim taken: release-main" in captured.out
        push.assert_called_once()

        aside = run_env.parent / "release-main-v0.5.0"
        assert run_state.load_state(aside)["status"] == "complete"  # untouched
        fresh = run_state.load_state(run_env)
        assert fresh["slug"] == "release-main"
        assert fresh["status"] == "in-progress"
        assert fresh["claim"]["session_id"] == "s-claim-1"
        assert release_run.load_release(run_env) is None  # a NEW release record

    def test_aborted_run_rolls_over_too(self, run_env, capsys):
        # The abandoned-release contract: a declined release is resumed as
        # the NEXT release run, whose ctl dry-run skips the bump already on
        # prep-release. Nothing about that needs re-claiming the dead run.
        state = _seed_claimed(run_env, session="s-holder", status="complete")
        state["stop_reason"] = "aborted"
        state["claim"] = None
        run_state.write_state(run_env, state)
        with ExitStack() as stack:
            _cas_seams(stack)
            assert _main(["release", "claim"]) == 0
        assert "rolled aside" in capsys.readouterr().err

        # No version was ever recorded — the stamped form still frees it.
        aside = [d for d in run_env.parent.iterdir() if d.name != "release-main"]
        assert len(aside) == 1
        assert not aside[0].name.startswith("release-main-v")
        assert run_state.load_state(aside[0])["stop_reason"] == "aborted"
        assert run_state.load_state(run_env)["status"] == "in-progress"

    def test_two_sessions_cannot_both_claim_across_a_roll_over(
            self, run_env, monkeypatch, capsys):
        """The property the roll-over must not weaken: the release taskdef
        reads exit 0 as "proceed", so two zero exits means two sessions
        driving one release. The loser refuses against the winner's LIVE
        claim on the successor run — not against the dead run."""
        state = _seed_claimed(run_env, session="s-holder", status="complete")
        state["stop_reason"] = "aborted"
        state["claim"] = None
        run_state.write_state(run_env, state)
        codes = []
        for session in ("s-a", "s-b"):
            monkeypatch.setenv("LMER_SESSION_ID", session)
            with ExitStack() as stack:
                _cas_seams(stack)
                codes.append(_main(["release", "claim"]))
        err = capsys.readouterr().err
        assert codes == [0, 1], f"two sessions claimed one release: {codes}"
        assert "s-a" in err  # the loser is pointed at the live holder
        assert run_state.load_state(run_env)["claim"]["session_id"] == "s-a"

    def test_two_sessions_interleaved_across_a_roll_over(
            self, run_env, monkeypatch, capsys):
        """The same property with the race actually interleaved.

        The sequential guard above lets the loser meet an already-in-progress
        successor, so it never rolls over itself. Here both sessions read the
        SAME head and both do their own local roll-over before either push
        lands — which is where `_drop_claim_commit`'s `reset --hard` has to
        unwind a dir rename PLUS a fresh seed, and where the re-synced tree
        the retry evaluates is the winner's rather than the one the loser
        built.

        The drop only unwinds what the claim commit STAGED (reset leaves
        untracked paths alone), so this is also the test that catches a
        roll-over staging half its move: the loser's aside dir would be
        untracked and would sit on top of the winner's tree afterwards.
        """
        runs = run_env.parent
        state = _seed_claimed(run_env, session="s-holder", status="complete")
        state["stop_reason"] = "aborted"
        state["claim"] = None
        run_state.write_state(run_env, state)
        head = _snapshot(runs)

        monkeypatch.setenv("LMER_SESSION_ID", "s-a")
        with ExitStack() as stack:
            _cas_seams(stack)
            assert _main(["release", "claim"]) == 0
        winner = _snapshot(runs)
        capsys.readouterr()

        # Rewind to the head session B also started from.
        _restore(runs, head)
        monkeypatch.setenv("LMER_SESSION_ID", "s-b")
        synced = []
        dropped = []
        staged = []

        # A fetch that integrates another session's head replaces TRACKED
        # content only — the run dirs either head knows about. Anything else
        # on disk is untracked and survives it, the drop included.
        tracked_heads = _run_dir_names(head) | _run_dir_names(winner)

        def _sync():
            if synced:  # the retry's fetch integrates whatever won meanwhile
                _restore(runs, winner, tracked=tracked_heads)
            synced.append(1)
            return True, ""

        def _commit(_message, extra_rels=None):
            # What `git add -A -- <rels>` puts in the claim commit, and so
            # what the drop can unwind: the run-dir candidates the resolver
            # names plus the paths the verb hands in explicitly.
            rels = run_state.run_rel_path_candidates() + list(extra_rels or [])
            staged.append({rel.split("/runs/", 1)[-1] for rel in rels})
            return True, ""

        def _drop(_pre_head):
            assert _snapshot(runs) != head, "B never rolled over on its own"
            dropped.append(_snapshot(runs))
            _restore(runs, head, tracked=staged[-1])

        with ExitStack() as stack:
            # B's aside dir has to be distinguishable from A's. Both take the
            # compact-UTC stamp, and two roll-overs landing in the same second
            # produce byte-identical dirs — which would hide an orphaned one
            # behind equality rather than proving it was never left.
            stack.enter_context(
                patch("work_repo.release_run.utc_now_iso",
                      return_value="2031-02-03T04:05:06Z"))
            stack.enter_context(
                patch("work_repo.cli._sync_remote_head", side_effect=_sync))
            stack.enter_context(
                patch("work_repo.cli._commit_claim_write", side_effect=_commit))
            stack.enter_context(
                patch("work_repo.cli._git_head", return_value="pre-claim-sha"))
            stack.enter_context(
                patch("work_repo.cli._drop_claim_commit", side_effect=_drop))
            stack.enter_context(
                patch("work_repo.cli.claim_push_once",
                      side_effect=[(CLAIM_PUSH_LOST_RACE, "non-fast-forward"),
                                   (CLAIM_PUSH_WON, "")]))
            code = _main(["release", "claim"])

        assert code == 1, "two sessions claimed one release"
        assert dropped, "the loser's claim commit was never dropped"
        assert "s-a" in capsys.readouterr().err  # pointed at the live holder
        assert run_state.load_state(run_env)["claim"]["session_id"] == "s-a"
        assert _snapshot(runs) == winner, "the loser left work behind"

    def test_roll_over_json_shape(self, run_env, capsys):
        _seed_claimed(run_env, session="s-holder", status="complete")
        release_run.record_version(run_env, "0.5.0")
        with ExitStack() as stack:
            _cas_seams(stack)
            assert _main(["release", "claim", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["result"] == "won"
        assert payload["action"] == "claim"
        assert payload["slug"] == "release-main"
        assert payload["rolled_over"] == "release-main"
        assert payload["run_dir"] == str(run_env)

    def test_a_finished_run_whose_address_cannot_be_freed_still_refuses(
            self, run_env, capsys):
        # The roll-over is what makes the "NEW run" contract true; when it
        # cannot happen (rename refused, fs error) the old refusal stands —
        # never a claim on a terminal run, which would be a lock with no
        # mutual exclusion (claim_status reads every claim on a
        # non-in-progress run as unclaimed).
        _seed_claimed(run_env, session="s-holder", status="complete")
        with ExitStack() as stack:
            push, _ = _cas_seams(stack)
            stack.enter_context(
                patch("work_repo.run_state.Path.rename",
                      side_effect=OSError("read-only fs")))
            assert _main(["release", "claim"]) == 1
        err = capsys.readouterr().err
        assert "nothing to claim" in err
        assert "no live session holds" in err  # not the live-holder refusal
        # `detail` is a whole sentence, so the run is named ALONGSIDE it —
        # printing it in front produced "run 'X' is run is complete".
        assert "is run is" not in err
        assert ("❌ run is complete — nothing to claim (run 'release-main'; "
                "no live session holds this release; nothing was written)") in err
        push.assert_not_called()
        assert not _claim_events(run_env)

    def test_no_free_aside_address_refuses_instead_of_raising(
            self, run_env, capsys):
        """`unique_release_slug` raises rather than hand back an address it
        has just found taken. The claim treats that as "address not freed" —
        the same fail-closed refusal a failed rename takes — instead of
        dying with a traceback part-way through a CAS attempt."""
        _seed_claimed(run_env, session="s-holder", status="complete")
        with ExitStack() as stack:
            push, _ = _cas_seams(stack)
            stack.enter_context(
                patch("work_repo.release_run.unique_release_slug",
                      side_effect=release_run.ReleaseRunError(
                          "no free release address for 'release-main-v0.5.0'")))
            assert _main(["release", "claim"]) == 1
        err = capsys.readouterr().err
        assert "roll-over skipped: no free release address" in err
        assert "nothing to claim" in err
        push.assert_not_called()
        assert run_state.load_state(run_env)["status"] == "complete"  # untouched

    def test_refusal_json_shape_when_the_address_cannot_be_freed(
            self, run_env, capsys):
        state = _seed_claimed(run_env, session="s-holder", status="complete")
        state["stop_reason"] = "aborted"
        state["claim"] = None
        run_state.write_state(run_env, state)
        with ExitStack() as stack:
            _cas_seams(stack)
            stack.enter_context(
                patch("work_repo.run_state.Path.rename",
                      side_effect=OSError("read-only fs")))
            assert _main(["release", "claim", "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["result"] == "not-live"
        assert payload["stop_reason"] == "aborted"
        assert "NEW run" in payload["detail"]

    def test_sync_failure_fails_closed(self, run_env, capsys):
        # An unreachable remote means no claim (§7): fail closed, nothing
        # evaluated or written.
        with ExitStack() as stack:
            push, _ = _cas_seams(stack, sync=(False, "git fetch failed: boom"))
            assert _main(["release", "claim"]) == 1
        assert "fail closed" in capsys.readouterr().err
        push.assert_not_called()
        assert run_state.load_state(run_env) is None  # never even seeded

    def test_push_error_fails_closed(self, run_env, capsys):
        with ExitStack() as stack:
            _cas_seams(stack, push_outcomes=[(CLAIM_PUSH_ERROR, "403")])
            assert _main(["release", "claim"]) == 1
        err = capsys.readouterr().err
        assert "fail closed" in err
        assert "403" in err

    def test_fail_closed_json_still_emits_json(self, run_env, capsys):
        with ExitStack() as stack:
            _cas_seams(stack, sync=(False, "git fetch failed: boom"))
            assert _main(["release", "claim", "--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["result"] == "fail-closed"
        assert "boom" in payload["detail"]

    def test_lost_race_reevaluates_and_wins(self, run_env, capsys):
        # Non-fast-forward rejection → drop the local claim commit, re-fetch,
        # re-evaluate, retry (§7 step 5). Second attempt wins.
        with ExitStack() as stack:
            push, drop = _cas_seams(stack, push_outcomes=[
                (CLAIM_PUSH_LOST_RACE, "[rejected] non-fast-forward"),
                (CLAIM_PUSH_WON, ""),
            ])
            assert _main(["release", "claim"]) == 0
        assert push.call_count == 2
        drop.assert_called_once_with("pre-claim-sha")
        assert run_state.load_state(run_env)["claim"]["session_id"] == "s-claim-1"

    def test_push_error_drops_the_local_claim_commit(self, run_env, capsys):
        """A claim commit left behind after a transport failure would be
        silently rebase-pushed onto an un-re-checked head by the next
        ordinary verb — CLAIM_PUSH_ERROR must drop it like a lost race."""
        with ExitStack() as stack:
            push, drop = _cas_seams(
                stack, push_outcomes=[(CLAIM_PUSH_ERROR, "transport down")])
            assert _main(["release", "claim"]) == 1
        drop.assert_called_once_with("pre-claim-sha")
        assert "fail closed" in capsys.readouterr().err

    def test_lost_race_bounded_then_fails_closed(self, run_env, capsys):
        # Attempts are bounded: exhausted retries never proceed unlocked.
        rejections = [(CLAIM_PUSH_LOST_RACE, "[rejected] fetch first")] * (
            work_cli.RELEASE_CLAIM_ATTEMPTS + 2)
        with ExitStack() as stack:
            push, drop = _cas_seams(stack, push_outcomes=rejections)
            assert _main(["release", "claim"]) == 1
        assert push.call_count == work_cli.RELEASE_CLAIM_ATTEMPTS
        assert drop.call_count == work_cli.RELEASE_CLAIM_ATTEMPTS
        err = capsys.readouterr().err
        assert "exhausted" in err
        assert "fail closed" in err

    def test_bare_release_prints_help_exit_one(self, run_env):
        assert _main(["release"]) == 1


class TestClaimStatus:
    def test_no_context_exits_zero(self, capsys):
        assert _main(["release", "claim-status"]) == 0
        assert "no run context" in capsys.readouterr().out.lower()

    def test_no_context_json(self, capsys):
        assert _main(["release", "claim-status", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["verdict"] is None

    def test_no_run_reads_unclaimed(self, run_env, capsys):
        assert _main(["release", "claim-status"]) == 0
        assert "unclaimed" in capsys.readouterr().out
        assert run_state.load_state(run_env) is None  # read-only: no seed

    def test_run_without_claim_reads_unclaimed(self, run_env, capsys):
        run_state.write_state(
            run_env, run_state.seed_state("release-main", "release", "main"))
        assert _main(["release", "claim-status"]) == 0
        assert "unclaimed" in capsys.readouterr().out

    def test_own_claim(self, run_env, capsys):
        _seed_claimed(run_env, session="s-claim-1")
        assert _main(["release", "claim-status"]) == 0
        out = capsys.readouterr().out
        assert "ours" in out
        assert "s-claim-1" in out

    def test_foreign_live_claim(self, run_env, capsys):
        _seed_claimed(run_env, session="s-holder")
        assert _main(["release", "claim-status"]) == 0
        out = capsys.readouterr().out
        assert "foreign-live" in out
        assert "s-holder" in out
        assert "refuses" in out

    def test_foreign_stale_claim(self, run_env, capsys):
        _seed_claimed(run_env, session="s-dead",
                      claimed_at="2020-01-01T00:00:00Z")
        assert _main(["release", "claim-status"]) == 0
        out = capsys.readouterr().out
        assert "foreign-stale" in out
        assert "takes over" in out

    def test_inactive_claim_block_on_finished_run(self, run_env, capsys):
        _seed_claimed(run_env, session="s-holder", status="complete")
        assert _main(["release", "claim-status"]) == 0
        out = capsys.readouterr().out
        assert "unclaimed" in out
        assert "inactive claim block" in out
        assert "s-holder" in out

    def test_json_shape(self, run_env, capsys):
        _seed_claimed(run_env, session="s-holder")
        assert _main(["release", "claim-status", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == run_state.CLAIM_FOREIGN_LIVE
        assert payload["holder"] == "s-holder"
        assert payload["claimed_at"].endswith("Z")
        assert payload["age_minutes"] is not None
        assert payload["slug"] == "release-main"
        assert payload["run_dir"] == str(run_env)

    def test_read_only_never_writes(self, run_env):
        _seed_claimed(run_env, session="s-holder")
        before = run_state.load_state(run_env)
        assert _main(["release", "claim-status"]) == 0
        assert run_state.load_state(run_env) == before
        assert not (run_env / "events.jsonl").exists()

    def test_unreadable_state_still_exits_zero(self, run_env, capsys):
        # Read-only convention: even a schema refusal degrades to a
        # warning, never a failure.
        state = run_state.seed_state("release-main", "release", "main")
        state["schema"] = run_state.SCHEMA_VERSION + 1
        run_state.write_state(run_env, state)
        assert _main(["release", "claim-status"]) == 0
        assert "schema" in capsys.readouterr().err


class TestUnclaim:
    def test_no_context_exits_one(self, capsys):
        assert _main(["release", "unclaim"]) == 1
        assert "no run context" in capsys.readouterr().err.lower()

    def test_no_run_is_noop_success(self, run_env, capsys):
        with ExitStack() as stack:
            push, _ = _cas_seams(stack)
            assert _main(["release", "unclaim"]) == 0
        assert "nothing to release" in capsys.readouterr().out
        push.assert_not_called()
        assert run_state.load_state(run_env) is None  # no-op: no seed either

    def test_no_claim_is_noop_success(self, run_env, capsys):
        run_state.write_state(
            run_env, run_state.seed_state("release-main", "release", "main"))
        with ExitStack() as stack:
            push, _ = _cas_seams(stack)
            assert _main(["release", "unclaim"]) == 0
        assert "nothing to release" in capsys.readouterr().out
        push.assert_not_called()

    def test_releases_own_claim(self, run_env, capsys):
        _seed_claimed(run_env, session="s-claim-1")
        with ExitStack() as stack:
            push, _ = _cas_seams(stack)
            assert _main(["release", "unclaim"]) == 0
        assert run_state.load_state(run_env)["claim"] is None
        event = _claim_events(run_env)[-1]
        assert event["data"]["action"] == "unclaim"
        push.assert_called_once()
        assert "Release claim released" in capsys.readouterr().out

    def test_foreign_claim_refuses_without_force(self, run_env, capsys):
        _seed_claimed(run_env, session="s-holder")
        with ExitStack() as stack:
            push, _ = _cas_seams(stack)
            assert _main(["release", "unclaim"]) == 1
        err = capsys.readouterr().err
        assert "s-holder" in err
        assert "force" in err.lower()
        push.assert_not_called()
        assert run_state.load_state(run_env)["claim"]["session_id"] == "s-holder"

    def test_force_releases_foreign_claim(self, run_env, capsys):
        _seed_claimed(run_env, session="s-holder")
        with ExitStack() as stack:
            _cas_seams(stack)
            assert _main(["release", "unclaim", "--force"]) == 0
        assert run_state.load_state(run_env)["claim"] is None
        event = _claim_events(run_env)[-1]
        assert event["data"]["forced"] is True
        assert "force-released" in capsys.readouterr().out

    def test_sync_failure_fails_closed(self, run_env, capsys):
        _seed_claimed(run_env, session="s-claim-1")
        with ExitStack() as stack:
            _cas_seams(stack, sync=(False, "git fetch failed: boom"))
            assert _main(["release", "unclaim"]) == 1
        assert "fail closed" in capsys.readouterr().err
        assert run_state.load_state(run_env)["claim"]["session_id"] == "s-claim-1"

    def test_push_error_fails_closed(self, run_env, capsys):
        _seed_claimed(run_env, session="s-claim-1")
        with ExitStack() as stack:
            _cas_seams(stack, push_outcomes=[(CLAIM_PUSH_ERROR, "403")])
            assert _main(["release", "unclaim"]) == 1
        assert "fail closed" in capsys.readouterr().err

    def test_lost_race_retries_then_wins(self, run_env):
        _seed_claimed(run_env, session="s-claim-1")
        with ExitStack() as stack:
            push, drop = _cas_seams(stack, push_outcomes=[
                (CLAIM_PUSH_LOST_RACE, "[rejected] non-fast-forward"),
                (CLAIM_PUSH_WON, ""),
            ])
            # The real drop is `git reset --hard` — it restores the claim
            # block the unclaim write just cleared; mimic that so the retry
            # sees a claim to release again (the integration tests prove
            # the real rollback).
            drop.side_effect = lambda pre_head: _seed_claimed(
                run_env, session="s-claim-1")
            assert _main(["release", "unclaim"]) == 0
        assert push.call_count == 2
        drop.assert_called_once_with("pre-claim-sha")
        assert run_state.load_state(run_env)["claim"] is None


# --- Integration: real bare origin + per-launch clones (the §7 topology) ---


REL_STATE = "git.example.com/org/repo/runs/release-main/state.yaml"


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd),
         "-c", "user.name=test", "-c", "user.email=test@example.com",
         *args],
        check=True, capture_output=True,
    )


def _origin_head(origin):
    result = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "main"],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _origin_state(origin):
    result = subprocess.run(
        ["git", "-C", str(origin), "show", f"main:{REL_STATE}"],
        check=True, capture_output=True, text=True,
    )
    return yaml.safe_load(result.stdout)


def _log_subjects(cwd, ref="main"):
    result = subprocess.run(
        ["git", "-C", str(cwd), "log", "--format=%s", ref],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.splitlines()


@pytest.fixture
def cas_repo(tmp_path, monkeypatch):
    """Bare origin seeded with a base commit, plus two clones — each
    simultaneous launch has its own per-container clone (RUN-STATE.md §7)."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True,
    )
    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(seed)],
        check=True, capture_output=True,
    )
    (seed / "README.md").write_text("work repo\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init work repo")
    _git(seed, "push", "-q", "-u", "origin", "main")

    clones = []
    for name in ("clone-a", "clone-b"):
        clone = tmp_path / name
        subprocess.run(
            ["git", "clone", "-q", str(origin), str(clone)],
            check=True, capture_output=True,
        )
        # The CLI's own git plumbing commits without -c overrides.
        _git(clone, "config", "user.name", "test")
        _git(clone, "config", "user.email", "test@example.com")
        clones.append(clone)

    monkeypatch.setenv("LMER_REPO_HOST", "git.example.com")
    monkeypatch.setenv("LMER_REPO_PROJECT", "org/repo")
    monkeypatch.setenv("LMER_TASK", "release")
    monkeypatch.setenv("LMER_TASK_TARGET", "main")
    return origin, clones[0], clones[1]


def _as_launch(monkeypatch, clone, session):
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(clone))
    monkeypatch.setenv("LMER_SESSION_ID", session)


class TestClaimIntegration:
    def test_uncontested_claim_lands_on_remote(self, cas_repo, monkeypatch):
        origin, clone_a, _ = cas_repo
        _as_launch(monkeypatch, clone_a, "s-a")
        assert _main(["release", "claim"]) == 0
        state = _origin_state(origin)
        assert state["claim"]["session_id"] == "s-a"
        assert state["status"] == "in-progress"

    def test_second_launch_refused_from_stale_clone(
        self, cas_repo, monkeypatch, capsys
    ):
        # clone-b was cut BEFORE a's claim landed: the refusal must come
        # from evaluating the fetched REMOTE head, never the stale local view.
        origin, clone_a, clone_b = cas_repo
        _as_launch(monkeypatch, clone_a, "s-a")
        assert _main(["release", "claim"]) == 0
        head_after_a = _origin_head(origin)

        _as_launch(monkeypatch, clone_b, "s-b")
        assert _main(["release", "claim"]) == 1
        err = capsys.readouterr().err
        assert "s-a" in err
        assert "release-main" in err
        # The loser wrote nothing to the remote — the winner's head stands.
        assert _origin_head(origin) == head_after_a
        assert _origin_state(origin)["claim"]["session_id"] == "s-a"

    def test_holder_refresh_round_trips(self, cas_repo, monkeypatch, capsys):
        origin, clone_a, _ = cas_repo
        _as_launch(monkeypatch, clone_a, "s-a")
        assert _main(["release", "claim"]) == 0
        assert _main(["release", "claim"]) == 0
        assert "refreshed" in capsys.readouterr().out
        assert _origin_state(origin)["claim"]["session_id"] == "s-a"

    def test_stale_claim_taken_over_from_other_clone(
        self, cas_repo, monkeypatch, capsys
    ):
        origin, clone_a, clone_b = cas_repo
        _as_launch(monkeypatch, clone_a, "s-dead")
        assert _main(["release", "claim"]) == 0
        # Age the claim past the threshold, as a crashed holder would leave it.
        state_path = clone_a / REL_STATE
        state = yaml.safe_load(state_path.read_text())
        state["claim"]["claimed_at"] = "2020-01-01T00:00:00Z"
        state_path.write_text(yaml.safe_dump(state, sort_keys=False))
        _git(clone_a, "add", ".")
        _git(clone_a, "commit", "-q", "-m", "age the claim")
        _git(clone_a, "push", "-q")

        _as_launch(monkeypatch, clone_b, "s-b")
        assert _main(["release", "claim"]) == 0
        out = capsys.readouterr().out
        assert "takeover" in out
        assert "s-dead" in out
        assert _origin_state(origin)["claim"]["session_id"] == "s-b"

    def test_lost_race_reevaluates_new_head_and_wins(
        self, cas_repo, monkeypatch
    ):
        # A genuine mid-window race: another writer advances the remote
        # between our fetch/evaluate and our push. The push is rejected
        # non-fast-forward, the local claim commit is dropped (never
        # rebased blind), the new head is re-evaluated, and the retry wins.
        origin, clone_a, clone_b = cas_repo
        from work_repo import git_ops
        calls = {"n": 0}
        real = git_ops.claim_push_once

        def racing_push(repo_path, label="work repo"):
            calls["n"] += 1
            if calls["n"] == 1:
                (clone_b / "unrelated.txt").write_text("other writer\n")
                _git(clone_b, "add", ".")
                _git(clone_b, "commit", "-q", "-m", "unrelated write")
                _git(clone_b, "push", "-q")
            return real(repo_path, label)

        monkeypatch.setattr(work_cli, "claim_push_once", racing_push)
        _as_launch(monkeypatch, clone_a, "s-a")
        assert _main(["release", "claim"]) == 0
        assert calls["n"] == 2  # first push lost the race, retry won
        assert _origin_state(origin)["claim"]["session_id"] == "s-a"
        subjects = _log_subjects(origin)
        # Exactly ONE claim commit landed — the lost-race commit was
        # dropped and rebuilt, never stacked or merged.
        assert subjects.count("run-state: release-main release claim") == 1
        assert "unrelated write" in subjects

    def test_claim_succeeds_with_dirty_session_start_state(
        self, cas_repo, monkeypatch
    ):
        """The re-entry path the verbs exist for: session-start writes
        owner into tracked state.yaml WITHOUT committing, and the CAS sync
        must snapshot-commit it instead of dying on `pull --rebase`'s
        dirty-tree refusal."""
        origin, clone_a, _ = cas_repo
        _as_launch(monkeypatch, clone_a, "s-a")
        assert _main(["release", "claim"]) == 0

        state_path = clone_a / REL_STATE
        state = yaml.safe_load(state_path.read_text())
        state["owner"] = {"session_id": "s-a", "claimed_at": "2026-01-01T00:00:00Z"}
        state_path.write_text(yaml.safe_dump(state, sort_keys=False))

        assert _main(["release", "claim"]) == 0  # refresh over the dirty tree
        subjects = _log_subjects(origin)
        assert "run-state: local snapshot before release CAS sync" in subjects
        assert _origin_state(origin)["claim"]["session_id"] == "s-a"

    def test_claim_survives_a_run_dir_rename(self, cas_repo, monkeypatch):
        """After runs/<slug> → runs/<slug>--<name>, the stale bare-slug
        candidate must not make `git add` exit 128 and fail every verb."""
        origin, clone_a, _ = cas_repo
        _as_launch(monkeypatch, clone_a, "s-a")
        assert _main(["release", "claim"]) == 0

        runs = clone_a / "git.example.com" / "org/repo" / "runs"
        _git(clone_a, "mv", str(runs / "release-main"),
             str(runs / "release-main--rc"))
        _git(clone_a, "commit", "-q", "-m", "rename run dir")
        _git(clone_a, "push", "-q")

        assert _main(["release", "claim"]) == 0
        assert _main(["release", "unclaim"]) == 0

    def test_lost_race_preserves_preexisting_untracked_files(
        self, cas_repo, monkeypatch
    ):
        """The lost-race rollback must never destroy files the verb did not
        write — an uncommitted retro.md predating the claim survives the
        race (the pre-sync snapshot commits it before pre_head is taken)."""
        origin, clone_a, clone_b = cas_repo
        _as_launch(monkeypatch, clone_a, "s-a")
        assert _main(["release", "claim"]) == 0
        assert _main(["release", "unclaim"]) == 0

        retro = clone_a / "git.example.com" / "org/repo" / "runs" / \
            "release-main" / "retro.md"
        retro.write_text("hard-won notes\n")

        from work_repo import git_ops
        calls = {"n": 0}
        real = git_ops.claim_push_once

        def racing_push(repo_path, label="work repo"):
            calls["n"] += 1
            if calls["n"] == 1:
                _git(clone_b, "pull", "-q")  # clone-b saw the earlier pushes
                (clone_b / "unrelated.txt").write_text("other writer\n")
                _git(clone_b, "add", ".")
                _git(clone_b, "commit", "-q", "-m", "unrelated write")
                _git(clone_b, "push", "-q")
            return real(repo_path, label)

        monkeypatch.setattr(work_cli, "claim_push_once", racing_push)
        assert _main(["release", "claim"]) == 0
        assert calls["n"] == 2
        assert retro.read_text() == "hard-won notes\n"
        assert _origin_state(origin)["claim"]["session_id"] == "s-a"

    def test_session_end_releases_our_claim(self, cas_repo, monkeypatch):
        """A cleanly-ended holder must not strand a live-looking claim that
        blocks relaunches until the stale threshold."""
        origin, clone_a, _ = cas_repo
        _as_launch(monkeypatch, clone_a, "s-a")
        assert _main(["release", "claim"]) == 0
        assert _origin_state(origin)["claim"]["session_id"] == "s-a"

        assert _main(["session-end"]) == 0
        assert _origin_state(origin)["claim"] is None

    def test_session_end_leaves_a_foreign_claim_alone(
        self, cas_repo, monkeypatch
    ):
        """session-end releases only OUR claim — a foreign holder's lock is
        never cleared by another session ending."""
        origin, clone_a, clone_b = cas_repo
        _as_launch(monkeypatch, clone_a, "s-a")
        assert _main(["release", "claim"]) == 0

        _as_launch(monkeypatch, clone_b, "s-b")
        _git(clone_b, "pull", "-q")
        assert _main(["session-end"]) == 0
        assert _origin_state(origin)["claim"]["session_id"] == "s-a"

    def test_unclaim_clears_claim_on_remote(self, cas_repo, monkeypatch):
        origin, clone_a, _ = cas_repo
        _as_launch(monkeypatch, clone_a, "s-a")
        assert _main(["release", "claim"]) == 0
        assert _main(["release", "unclaim"]) == 0
        assert _origin_state(origin)["claim"] is None
