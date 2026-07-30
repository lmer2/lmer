#!/usr/bin/env python3
"""Tests for git_ops.claim_push_once — the claim-by-push CAS leg.

The single-flight release claim (docs/RUN-STATE.md §7) needs a push path
that does NOT rebase-and-retry: _push_with_rebase_retries reacts to a
rejected push by rebasing the local commit onto the new remote head and
pushing again, which would silently stack two competing claim commits.
claim_push_once instead issues one plain push and reports a non-fast-forward
rejection as a distinguishable lost-race outcome, leaving the bounded
re-fetch/re-evaluate loop to the caller.

The core proof uses a real bare remote plus two clones: of two competing
claim pushes exactly one wins, and the loser reports lost-race without
clobbering the winner's commit or looping.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import work_repo.git_ops as git_ops
from work_repo.git_ops import (
    CLAIM_PUSH_ERROR,
    CLAIM_PUSH_LOST_RACE,
    CLAIM_PUSH_WON,
    claim_push_once,
)


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd),
         "-c", "user.name=test", "-c", "user.email=test@example.com",
         *args],
        check=True, capture_output=True,
    )


def _rev(cwd, ref="HEAD"):
    result = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", ref],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _log_subjects(cwd, ref="HEAD"):
    result = subprocess.run(
        ["git", "-C", str(cwd), "log", "--format=%s", ref],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.splitlines()


def _bare_remote_with_two_clones(tmp_path):
    """Bare origin seeded with a base commit, plus two fresh clones — the
    two-simultaneous-launches topology: each launch has its own per-container
    clone of the work repo (RUN-STATE.md §7)."""
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
    run_dir = Path("git.example.com/grp/proj/runs/release-v1")
    (seed / run_dir).mkdir(parents=True)
    (seed / run_dir / "state.yaml").write_text("schema: 1\nstatus: in-progress\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "seed run state")
    _git(seed, "push", "-q", "-u", "origin", "main")

    clones = []
    for name in ("clone-a", "clone-b"):
        clone = tmp_path / name
        subprocess.run(
            ["git", "clone", "-q", str(origin), str(clone)],
            check=True, capture_output=True,
        )
        clones.append(clone)
    return origin, clones[0], clones[1], run_dir


def _write_claim_commit(clone, run_dir, session_id):
    """Each competing launch writes its claim into its OWN clone — both see
    the same remote head, so both commits share the same parent (the race)."""
    state = clone / run_dir / "state.yaml"
    state.write_text(
        "schema: 1\nstatus: in-progress\n"
        f"claim:\n  session_id: {session_id}\n  claimed_at: 2026-07-26T00:00:00Z\n"
    )
    _git(clone, "add", ".")
    _git(clone, "commit", "-q", "-m", f"claim release run ({session_id})")


class TestCompetingClaims:
    """Two clones, two claim commits off the same base: exactly one wins."""

    def test_exactly_one_of_two_claims_wins(self, tmp_path):
        origin, clone_a, clone_b, run_dir = _bare_remote_with_two_clones(tmp_path)
        _write_claim_commit(clone_a, run_dir, "session-a")
        _write_claim_commit(clone_b, run_dir, "session-b")

        outcome_a, _ = claim_push_once(clone_a)
        outcome_b, detail_b = claim_push_once(clone_b)

        assert outcome_a == CLAIM_PUSH_WON
        assert outcome_b == CLAIM_PUSH_LOST_RACE, detail_b
        # The remote holds the winner's claim and ONLY the winner's — the
        # loser's commit never landed, stacked, or merged.
        subjects = _log_subjects(origin, "main")
        assert "claim release run (session-a)" in subjects
        assert not any("session-b" in s for s in subjects)
        assert _rev(origin, "main") == _rev(clone_a, "HEAD")

    def test_loser_makes_one_push_and_never_rebases(self, tmp_path):
        """The lost-race path must not loop or integrate: one fetch, ONE push,
        no pull --rebase, no second attempt — the rebase-retry behavior that
        would merge two claims stays confined to _push_with_rebase_retries."""
        origin, clone_a, clone_b, run_dir = _bare_remote_with_two_clones(tmp_path)
        _write_claim_commit(clone_a, run_dir, "session-a")
        _write_claim_commit(clone_b, run_dir, "session-b")
        assert claim_push_once(clone_a)[0] == CLAIM_PUSH_WON

        loser_head_before = _rev(clone_b, "HEAD")
        with patch(
            "work_repo.git_ops.run_git_command",
            side_effect=git_ops.run_git_command,
        ) as spy:
            outcome, _ = claim_push_once(clone_b)
        assert outcome == CLAIM_PUSH_LOST_RACE

        commands = [call.args[0] for call in spy.call_args_list]
        assert commands == [["fetch"], ["push"]]
        assert ["pull", "--rebase"] not in commands
        # Local history untouched: the loser's claim commit still sits on its
        # original parent, never rebased onto the winner's head.
        assert _rev(clone_b, "HEAD") == loser_head_before
        assert _rev(clone_b, "HEAD~1") == _rev(origin, "main~1")

    def test_uncontested_claim_wins_and_lands(self, tmp_path):
        origin, clone_a, _, run_dir = _bare_remote_with_two_clones(tmp_path)
        _write_claim_commit(clone_a, run_dir, "session-a")
        outcome, _ = claim_push_once(clone_a)
        assert outcome == CLAIM_PUSH_WON
        assert _rev(origin, "main") == _rev(clone_a, "HEAD")


class TestFailClosed:
    """Transport/auth problems are ERROR, never lost-race — a release must
    fail closed rather than treat a dead remote as an arbitration verdict."""

    def test_unreachable_remote_is_error(self, tmp_path, capsys):
        _, clone_a, _, run_dir = _bare_remote_with_two_clones(tmp_path)
        _write_claim_commit(clone_a, run_dir, "session-a")
        _git(clone_a, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
        outcome, detail = claim_push_once(clone_a)
        assert outcome == CLAIM_PUSH_ERROR
        assert detail.strip()  # a failure detail is never empty
        assert "git fetch failed" in capsys.readouterr().err

    def test_push_transport_failure_is_error_not_lost_race(self, tmp_path, capsys):
        """Fetch succeeds but the push dies on transport/auth: no rejection
        marker in the output → ERROR (mocked — real git fails the fetch
        first, so the classification branch needs a synthetic push failure)."""
        def fake_git(cmd, cwd, check=False):
            if cmd == ["fetch"]:
                return (0, "")
            if cmd == ["push"]:
                return (128, "fatal: unable to access 'https://host/repo.git/': 403")
            return (0, "")

        with patch("work_repo.git_ops.run_git_command", side_effect=fake_git):
            outcome, detail = claim_push_once(tmp_path)
        assert outcome == CLAIM_PUSH_ERROR
        assert "403" in detail
        assert "git push failed" in capsys.readouterr().err

    def test_non_fast_forward_output_is_lost_race(self, tmp_path):
        """The rejection shapes git actually prints classify as lost-race."""
        rejections = [
            "! [rejected]        main -> main (fetch first)",
            "! [rejected]        main -> main (non-fast-forward)",
        ]
        for rejection in rejections:
            def fake_git(cmd, cwd, check=False, _out=rejection):
                if cmd == ["push"]:
                    return (1, _out)
                return (0, "")

            with patch("work_repo.git_ops.run_git_command", side_effect=fake_git):
                outcome, detail = claim_push_once(tmp_path)
            assert outcome == CLAIM_PUSH_LOST_RACE, rejection
            assert detail == rejection

    def test_fetch_precedes_the_single_push(self, tmp_path):
        """Order is fetch → push, one of each: the fetch proves the remote is
        reachable before the push verdict is trusted."""
        commands = []

        def fake_git(cmd, cwd, check=False):
            commands.append(cmd)
            return (0, "")

        with patch("work_repo.git_ops.run_git_command", side_effect=fake_git):
            outcome, _ = claim_push_once(tmp_path)
        assert outcome == CLAIM_PUSH_WON
        assert commands == [["fetch"], ["push"]]
