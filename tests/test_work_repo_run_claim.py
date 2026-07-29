"""Tests for the single-flight release-claim primitives (work_repo.run_state).

docs/RUN-STATE.md §7: the claim is a dedicated `claim` block
({session_id, claimed_at}) in the release run's state.yaml — NOT the
session-scoped `owner` field — valid only while the run is in-progress,
scoped to project + release taskdef. A live foreign claim is a HARD refusal
(never a warning), the holder refreshes by re-claiming (idempotent), and a
claim past RELEASE_CLAIM_STALE_MINUTES is taken over automatically and
loudly. decide()'s warn-only STALE_CLAIM_MINUTES semantics for non-release
runs stay byte-identical.

The core proof is the two-clone race: one bare remote, two clones, two
claim attempts — the local primitives here composed with
git_ops.claim_push_once exactly as the CLI will. Exactly one claimant wins;
the loser's re-evaluation against the remote head gets a structured refusal.
"""
import subprocess
from pathlib import Path

import pytest

from work_repo import run_state
from work_repo.git_ops import CLAIM_PUSH_LOST_RACE, CLAIM_PUSH_WON, claim_push_once
from tests.conftest import strip_lmer_env


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """Strip LMER_* env vars so the host's real env can't leak in."""
    strip_lmer_env(monkeypatch)


NOW = "2026-07-26T12:00:00Z"
LIVE_AT = "2026-07-26T11:30:00Z"    # 30 min before NOW — live
STALE_AT = "2026-07-26T09:00:00Z"   # 180 min before NOW — stale


def _state(**overrides):
    base = run_state.seed_state("release", "release", "")
    base.update(overrides)
    return base


def _claim(session="s-other", at=LIVE_AT):
    return {"session_id": session, "claimed_at": at}


def _claim_events(rdir):
    return [e for e in run_state.read_events(rdir, last_n=0) if e["type"] == "claim"]


class TestClaimStatus:
    """Pure verdicts — no fs, no env; decide() and the CLI both call this."""

    def test_no_state_is_unclaimed(self):
        v = run_state.claim_status(None, "s-1", now=NOW)
        assert v == {"verdict": run_state.CLAIM_UNCLAIMED, "holder": None,
                     "claimed_at": None, "age_minutes": None}

    def test_no_claim_block_is_unclaimed(self):
        v = run_state.claim_status(_state(), "s-1", now=NOW)
        assert v["verdict"] == run_state.CLAIM_UNCLAIMED

    def test_block_without_session_id_is_unclaimed(self):
        state = _state(claim={"claimed_at": LIVE_AT})
        v = run_state.claim_status(state, "s-1", now=NOW)
        assert v["verdict"] == run_state.CLAIM_UNCLAIMED

    def test_own_claim_is_ours(self):
        state = _state(claim=_claim("s-1"))
        v = run_state.claim_status(state, "s-1", now=NOW)
        assert v["verdict"] == run_state.CLAIM_OURS
        assert v["holder"] == "s-1"
        assert v["age_minutes"] == 30.0

    def test_fresh_foreign_claim_is_live(self):
        state = _state(claim=_claim("s-other", LIVE_AT))
        v = run_state.claim_status(state, "s-1", now=NOW)
        assert v["verdict"] == run_state.CLAIM_FOREIGN_LIVE
        assert v["holder"] == "s-other"
        assert v["claimed_at"] == LIVE_AT

    def test_old_foreign_claim_is_stale(self):
        state = _state(claim=_claim("s-other", STALE_AT))
        v = run_state.claim_status(state, "s-1", now=NOW)
        assert v["verdict"] == run_state.CLAIM_FOREIGN_STALE
        assert v["age_minutes"] == 180.0

    def test_exactly_at_threshold_is_stale(self):
        # live is strictly `age < RELEASE_CLAIM_STALE_MINUTES` — mirrors the
        # advisory owner branch's boundary.
        at = "2026-07-26T10:00:00Z"  # exactly 120 min before NOW
        state = _state(claim=_claim("s-other", at))
        v = run_state.claim_status(state, "s-1", now=NOW)
        assert v["verdict"] == run_state.CLAIM_FOREIGN_STALE

    def test_unparseable_claimed_at_is_stale(self):
        # Takeover territory, never refusal: a claim whose age can't be
        # judged must not strand the scheduled relaunch.
        state = _state(claim=_claim("s-other", "not-a-timestamp"))
        v = run_state.claim_status(state, "s-1", now=NOW)
        assert v["verdict"] == run_state.CLAIM_FOREIGN_STALE
        assert v["age_minutes"] is None

    def test_far_future_claimed_at_is_stale(self):
        # A holder with a fast clock (+6 h) that then crashed must not read
        # live until its skew elapses — the threshold bounds skew in BOTH
        # directions (abs(age) < threshold).
        state = _state(claim=_claim("s-other", "2026-07-26T18:00:00Z"))
        v = run_state.claim_status(state, "s-1", now=NOW)
        assert v["verdict"] == run_state.CLAIM_FOREIGN_STALE
        assert v["age_minutes"] == -360.0

    def test_slightly_future_claimed_at_is_live(self):
        # Small negative skew (holder clock a minute ahead) must NOT flip a
        # genuinely live holder into instant-takeover territory.
        state = _state(claim=_claim("s-other", "2026-07-26T12:01:00Z"))
        v = run_state.claim_status(state, "s-1", now=NOW)
        assert v["verdict"] == run_state.CLAIM_FOREIGN_LIVE

    @pytest.mark.parametrize("status", ["complete", "archived"])
    def test_claim_on_finished_run_reads_unclaimed(self, status):
        # Completing or aborting the run releases the lock with no separate
        # CAS write (§7) — but the block's metadata is still reported.
        state = _state(status=status, claim=_claim("s-other", LIVE_AT))
        v = run_state.claim_status(state, "s-1", now=NOW)
        assert v["verdict"] == run_state.CLAIM_UNCLAIMED
        assert v["holder"] == "s-other"


class TestIsClaimedByOther:
    """The hard-refusal predicate: True ONLY for a live foreign claim."""

    def test_true_for_live_foreign_claim(self):
        state = _state(claim=_claim("s-other", LIVE_AT))
        assert run_state.is_claimed_by_other(state, "s-1", now=NOW) is True

    def test_false_for_own_claim(self):
        state = _state(claim=_claim("s-1"))
        assert run_state.is_claimed_by_other(state, "s-1", now=NOW) is False

    def test_false_for_stale_foreign_claim(self):
        state = _state(claim=_claim("s-other", STALE_AT))
        assert run_state.is_claimed_by_other(state, "s-1", now=NOW) is False

    def test_false_without_claim(self):
        assert run_state.is_claimed_by_other(_state(), "s-1", now=NOW) is False

    def test_false_on_finished_run(self):
        state = _state(status="complete", claim=_claim("s-other", LIVE_AT))
        assert run_state.is_claimed_by_other(state, "s-1", now=NOW) is False


class TestClaimRun:
    def test_fresh_claim_lands_on_disk(self, tmp_path):
        rdir = tmp_path / "r"
        state = run_state.claim_run(rdir, _state(), session="s-1", now=NOW)
        assert state["claim"] == {"session_id": "s-1", "claimed_at": NOW}
        assert run_state.load_state(rdir)["claim"] == {
            "session_id": "s-1", "claimed_at": NOW
        }
        (event,) = _claim_events(rdir)
        assert event["data"]["action"] == "claim"
        assert event["data"]["session_id"] == "s-1"

    def test_reclaim_by_holder_refreshes_claimed_at(self, tmp_path):
        rdir = tmp_path / "r"
        state = run_state.claim_run(rdir, _state(), session="s-1", now=LIVE_AT)
        state = run_state.claim_run(rdir, state, session="s-1", now=NOW)
        assert state["claim"]["claimed_at"] == NOW
        assert run_state.load_state(rdir)["claim"]["claimed_at"] == NOW
        events = _claim_events(rdir)
        assert [e["data"]["action"] for e in events] == ["claim", "refresh"]

    def test_live_foreign_claim_hard_refuses(self, tmp_path):
        rdir = tmp_path / "r"
        state = _state(phase="leg2", claim=_claim("s-other", LIVE_AT))
        run_state.write_state(rdir, state)
        with pytest.raises(run_state.ClaimRefusedError) as exc:
            run_state.claim_run(rdir, state, session="s-1", now=NOW)
        # Structured refusal: the loser's pointer (§7) — the CLI adds the
        # web URL via git_ops.web_url_for.
        assert exc.value.pointer == {
            "slug": "release",
            "run_dir": str(rdir),
            "holder": "s-other",
            "claimed_at": LIVE_AT,
            "age_minutes": 30.0,
            "status": "in-progress",
            "phase": "leg2",
        }
        # Nothing was written: the foreign claim is intact, no claim event.
        assert run_state.load_state(rdir)["claim"] == _claim("s-other", LIVE_AT)
        assert _claim_events(rdir) == []

    def test_stale_foreign_claim_taken_over_loudly(self, tmp_path):
        rdir = tmp_path / "r"
        state = _state(claim=_claim("s-dead", STALE_AT))
        run_state.write_state(rdir, state)
        state = run_state.claim_run(rdir, state, session="s-2", now=NOW)
        assert state["claim"] == {"session_id": "s-2", "claimed_at": NOW}
        (event,) = _claim_events(rdir)
        assert event["data"]["action"] == "takeover"
        assert event["data"]["displaced_session"] == "s-dead"
        assert event["data"]["displaced_claimed_at"] == STALE_AT
        assert event["data"]["displaced_age_minutes"] == 180.0
        assert "s-dead" in event["note"]

    def test_foreign_claim_on_finished_run_is_claimable(self, tmp_path):
        # A claim block on a complete run reads as unclaimed (§7) — the
        # lock was released by completion, no CAS write needed.
        rdir = tmp_path / "r"
        state = _state(status="complete", claim=_claim("s-other", LIVE_AT))
        run_state.write_state(rdir, state)
        state = run_state.claim_run(rdir, state, session="s-1", now=NOW)
        assert state["claim"]["session_id"] == "s-1"


class TestUnclaimRun:
    def test_clears_own_claim(self, tmp_path):
        rdir = tmp_path / "r"
        state = run_state.claim_run(rdir, _state(), session="s-1", now=NOW)
        state = run_state.unclaim_run(rdir, state, session="s-1")
        assert state["claim"] is None
        assert run_state.load_state(rdir)["claim"] is None
        events = _claim_events(rdir)
        assert [e["data"]["action"] for e in events] == ["claim", "unclaim"]
        assert "forced" not in events[-1]["data"]

    def test_no_claim_is_idempotent_noop(self, tmp_path):
        rdir = tmp_path / "r"
        state = _state()
        run_state.write_state(rdir, state)
        updated_before = run_state.load_state(rdir)["updated"]
        result = run_state.unclaim_run(rdir, state, session="s-1")
        assert result is state
        assert run_state.load_state(rdir)["updated"] == updated_before  # no write
        assert _claim_events(rdir) == []  # no event

    def test_foreign_claim_refuses_without_force(self, tmp_path):
        rdir = tmp_path / "r"
        state = _state(claim=_claim("s-other", STALE_AT))  # even stale: never silent removal
        run_state.write_state(rdir, state)
        with pytest.raises(run_state.ClaimRefusedError) as exc:
            run_state.unclaim_run(rdir, state, session="s-1")
        assert exc.value.pointer["holder"] == "s-other"
        assert run_state.load_state(rdir)["claim"] == _claim("s-other", STALE_AT)

    def test_force_clears_foreign_claim(self, tmp_path):
        rdir = tmp_path / "r"
        state = _state(claim=_claim("s-other", LIVE_AT))
        run_state.write_state(rdir, state)
        state = run_state.unclaim_run(rdir, state, session="s-1", force=True)
        assert state["claim"] is None
        (event,) = _claim_events(rdir)
        assert event["data"] == {"action": "unclaim", "holder": "s-other",
                                 "forced": True}


class TestDecideClaim:
    """The claim branch replaces the warn-only owner branch for release runs
    (only they carry a claim block); non-release runs stay byte-identical."""

    def test_live_foreign_claim_warns_enforced(self):
        state = _state(claim=_claim("s-other", LIVE_AT))
        d = run_state.decide(state, [], "s-1", now=NOW)
        (warning,) = d["warnings"]
        assert "release claim held by session s-other" in warning
        assert "coordinate before writing" not in warning  # not the advisory text
        assert d["claim"]["verdict"] == run_state.CLAIM_FOREIGN_LIVE

    def test_stale_foreign_claim_warns_takeover(self):
        state = _state(claim=_claim("s-other", STALE_AT))
        d = run_state.decide(state, [], "s-1", now=NOW)
        (warning,) = d["warnings"]
        assert "stale release claim" in warning
        assert d["claim"]["verdict"] == run_state.CLAIM_FOREIGN_STALE

    def test_own_claim_no_warning(self):
        state = _state(claim=_claim("s-1"))
        d = run_state.decide(state, [], "s-1", now=NOW)
        assert d["warnings"] == []
        assert d["claim"]["verdict"] == run_state.CLAIM_OURS

    def test_claim_block_supersedes_owner_branch(self):
        # A release run with both a foreign owner and a foreign claim gets
        # the enforced message, not the advisory one.
        state = _state(
            owner={"session_id": "s-owner", "claimed_at": LIVE_AT},
            claim=_claim("s-other", LIVE_AT),
        )
        d = run_state.decide(state, [], "s-1", now=NOW)
        (warning,) = d["warnings"]
        assert "release claim" in warning

    def test_non_release_owner_warning_byte_identical(self):
        # No claim block → the advisory owner branch, verbatim.
        state = _state(owner={"session_id": "s-other", "claimed_at": LIVE_AT})
        d = run_state.decide(state, [], "s-1", now=NOW)
        assert d["warnings"] == [
            "run is claimed by another live session (s-other, 30 min ago) "
            "— coordinate before writing"
        ]
        assert d["claim"] is None

    def test_non_release_stale_owner_warning_byte_identical(self):
        state = _state(owner={"session_id": "s-other", "claimed_at": STALE_AT})
        d = run_state.decide(state, [], "s-1", now=NOW)
        assert d["warnings"] == [
            "stale owner claim from session s-other — "
            "the previous session likely did not close cleanly"
        ]


# --- two-clone race: the topology the lock exists for (RUN-STATE.md §7) ---

RUN_REL = Path("git.example.com/org/proj/runs/release")


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd),
         "-c", "user.name=test", "-c", "user.email=test@example.com",
         *args],
        check=True, capture_output=True,
    )


def _bare_remote_with_two_clones(tmp_path):
    """Bare origin holding a seeded (unclaimed) release run, plus two fresh
    clones — two simultaneous launches, each with its own per-container
    clone of the work repo."""
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
    run_state.write_state(seed / RUN_REL, _state())
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "seed release run")
    _git(seed, "push", "-q", "-u", "origin", "main")

    clones = []
    for name in ("clone-a", "clone-b"):
        clone = tmp_path / name
        subprocess.run(
            ["git", "clone", "-q", str(origin), str(clone)],
            check=True, capture_output=True,
        )
        clones.append(clone)
    return clones[0], clones[1]


def _claim_and_commit(clone, session, now):
    """One launch's local claim leg: evaluate + write via claim_run, then
    commit — ready for the claim_push_once CAS leg."""
    rdir = clone / RUN_REL
    state = run_state.claim_run(rdir, run_state.load_state(rdir), session, now=now)
    _git(clone, "add", ".")
    _git(clone, "commit", "-q", "-m", f"claim release run ({session})")
    return state


class TestTwoCloneRace:
    def test_exactly_one_claimant_wins(self, tmp_path):
        clone_a, clone_b = _bare_remote_with_two_clones(tmp_path)

        # Both launches see the same (unclaimed) remote head, so both local
        # claims succeed — the race is real, and only the push arbitrates.
        _claim_and_commit(clone_a, "session-a", now="2026-07-26T12:00:00Z")
        _claim_and_commit(clone_b, "session-b", now="2026-07-26T12:00:01Z")

        outcome_a, _ = claim_push_once(clone_a)
        outcome_b, detail_b = claim_push_once(clone_b)
        assert outcome_a == CLAIM_PUSH_WON
        assert outcome_b == CLAIM_PUSH_LOST_RACE, detail_b

        # The loser's protocol step 1: re-fetch and re-evaluate against the
        # REMOTE head — never the local clone's stale view.
        _git(clone_b, "fetch", "-q")
        _git(clone_b, "reset", "-q", "--hard", "origin/main")
        rdir_b = clone_b / RUN_REL
        remote_state = run_state.load_state(rdir_b)
        assert remote_state["claim"]["session_id"] == "session-a"

        # Exactly one claimant holds the run; the loser's re-claim is a
        # structured hard refusal carrying the active-run pointer.
        with pytest.raises(run_state.ClaimRefusedError) as exc:
            run_state.claim_run(
                rdir_b, remote_state, "session-b", now="2026-07-26T12:01:00Z"
            )
        pointer = exc.value.pointer
        assert pointer["slug"] == "release"
        assert pointer["run_dir"] == str(rdir_b)
        assert pointer["holder"] == "session-a"
        assert pointer["claimed_at"] == "2026-07-26T12:00:00Z"
        assert pointer["age_minutes"] == 1.0
        assert pointer["status"] == "in-progress"

    def test_holder_relaunch_refreshes_through_same_path(self, tmp_path):
        """The winning session's relaunch (or watch-loop refresh) re-claims
        idempotently against the remote head — never refused by its own claim."""
        clone_a, clone_b = _bare_remote_with_two_clones(tmp_path)
        _claim_and_commit(clone_a, "session-a", now="2026-07-26T12:00:00Z")
        assert claim_push_once(clone_a)[0] == CLAIM_PUSH_WON

        # A fresh clone of the same session (relaunch) sees its own claim.
        _git(clone_b, "fetch", "-q")
        _git(clone_b, "reset", "-q", "--hard", "origin/main")
        rdir_b = clone_b / RUN_REL
        state = run_state.claim_run(
            rdir_b, run_state.load_state(rdir_b), "session-a",
            now="2026-07-26T12:30:00Z",
        )
        assert state["claim"]["claimed_at"] == "2026-07-26T12:30:00Z"
        _git(clone_b, "add", ".")
        _git(clone_b, "commit", "-q", "-m", "refresh claim (session-a)")
        assert claim_push_once(clone_b)[0] == CLAIM_PUSH_WON
