"""Tests for the release-run resume brief (masterplan release-flow §3):
decide()/format_brief() carrying the release record's derived position —
leg + next step, recorded version/merge SHAs, the receipt set so far, and
the single-flight claim state — with the record passed in by the caller
(decide() stays IO-free) and non-release briefs byte-identical."""
import pytest

from work_repo import release_run, run_state
from tests.conftest import strip_lmer_env

SHA_BUMP = "a" * 40
SHA_MERGE = "b" * 40


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


def _state(**overrides):
    base = run_state.seed_state("release", "release", "")
    base.update(overrides)
    return base


def _release(**overrides):
    """A release record mid-leg-2: tagged, first two pushes receipted."""
    release = release_run.seed_release()
    release.update(
        version="0.5.0",
        bump_mr_merge_sha=SHA_BUMP,
        release_mr_merge_sha=SHA_MERGE,
        tag={"name": "v0.5.0", "sha": SHA_MERGE, "created": "2026-07-27T10:00:00Z"},
        receipts={
            "github-main-push": {"recorded": "2026-07-27T10:01:00Z"},
            "github-tag-push": {"recorded": "2026-07-27T10:02:00Z"},
        },
    )
    release.update(overrides)
    return release


class TestDecideRelease:
    def test_release_none_by_default(self):
        # Non-release runs never pass a record — additive key reads None.
        d = run_state.decide(_state(), [], "s-1")
        assert d["release"] is None

    def test_non_release_decision_unchanged_by_none(self):
        # release=None is the non-release path: the decision dict is
        # identical to one made without the parameter at all.
        state = _state(phase="interview", goal="align")
        assert run_state.decide(state, [], "s-1") == run_state.decide(
            state, [], "s-1", release=None
        )

    def test_fresh_seed_derives_leg1(self):
        # An absent release.yaml is a fresh release run — the caller passes
        # the seed and the position still derives (nothing recorded yet).
        d = run_state.decide(_state(), [], "s-1", release=release_run.seed_release())
        assert d["release"]["leg"] == "leg1"
        assert d["release"]["next_step"] == "leg1-bump"
        assert d["release"]["version"] is None
        assert d["release"]["receipts"] == []

    def test_mid_leg2_position_and_identity_carried(self):
        d = run_state.decide(_state(), [], "s-1", release=_release())
        rel = d["release"]
        assert rel["leg"] == "leg2"
        assert rel["next_step"] == "leg2-poll-actions"
        assert rel["version"] == "0.5.0"
        assert rel["bump_mr_merge_sha"] == SHA_BUMP
        assert rel["release_mr_merge_sha"] == SHA_MERGE
        assert rel["tag"] == "v0.5.0"

    def test_receipts_listed_in_ladder_order(self):
        release = _release(receipts={
            "gitlab-tag-push": {"recorded": "x"},
            "github-main-push": {"recorded": "x"},
        })
        d = run_state.decide(_state(), [], "s-1", release=release)
        assert d["release"]["receipts"] == ["github-main-push", "gitlab-tag-push"]

    def test_gate_position(self):
        release = _release(release_mr_merge_sha=None, tag=None, receipts={})
        d = run_state.decide(_state(), [], "s-1", release=release)
        assert d["release"]["leg"] == "gate"
        assert d["release"]["next_step"] == "gate-await-release-merge"

    def test_inconsistent_record_warns_instead_of_raising(self):
        # derive_leg's hard stop (hand-edited record) must not break the
        # session-start hook: raw fields survive, position reads unknown,
        # and the stop text lands in warnings.
        release = _release(tag={"name": "v0.9.9", "sha": SHA_MERGE})
        d = run_state.decide(_state(), [], "s-1", release=release)
        rel = d["release"]
        assert rel["leg"] is None
        assert rel["next_step"] is None
        assert rel["version"] == "0.5.0"
        assert rel["tag"] == "v0.9.9"
        assert "HARD STOP" in rel["error"]
        assert any("release record inconsistent" in w for w in d["warnings"])

    def test_claim_verdict_carried_alongside_release(self):
        state = _state(claim={"session_id": "s-1",
                              "claimed_at": "2026-07-27T11:59:00Z"})
        d = run_state.decide(state, [], "s-1", now="2026-07-27T12:00:00Z",
                             release=_release())
        assert d["claim"]["verdict"] == run_state.CLAIM_OURS
        assert d["release"]["next_step"] == "leg2-poll-actions"


class TestFormatBriefRelease:
    def _brief(self, state=None, release=None, session="s-1", now=None):
        d = run_state.decide(state or _state(), [], session, now=now,
                             release=release)
        return run_state.format_brief(d)

    def test_position_line_renders(self):
        text = self._brief(release=_release())
        assert "Release: 0.5.0 — leg2 (next: leg2-poll-actions)" in text

    def test_identity_and_receipts_render(self):
        text = self._brief(release=_release())
        assert f"bump-MR merge SHA     {SHA_BUMP}" in text
        assert f"release-MR merge SHA  {SHA_MERGE}" in text
        assert "tag                   v0.5.0" in text
        assert "receipts              github-main-push, github-tag-push" in text

    def test_fresh_seed_renders_dashes(self):
        text = self._brief(release=release_run.seed_release())
        assert "Release: ? — leg1 (next: leg1-bump)" in text
        assert "bump-MR merge SHA     —" in text
        assert "receipts              —" in text

    def test_claim_unclaimed(self):
        text = self._brief(release=_release())
        assert "claim                 unclaimed" in text

    def test_claim_ours(self):
        state = _state(claim={"session_id": "s-1",
                              "claimed_at": "2026-07-27T11:59:00Z"})
        text = self._brief(state=state, release=_release(),
                           now="2026-07-27T12:00:00Z")
        assert "claim                 ours (session s-1)" in text

    def test_claim_foreign_live(self):
        state = _state(claim={"session_id": "s-other",
                              "claimed_at": "2026-07-27T11:30:00Z"})
        text = self._brief(state=state, release=_release(),
                           now="2026-07-27T12:00:00Z")
        assert "claim                 foreign (LIVE) — session s-other, 30 min ago" in text

    def test_claim_foreign_stale(self):
        state = _state(claim={"session_id": "s-other",
                              "claimed_at": "2026-07-27T01:00:00Z"})
        text = self._brief(state=state, release=_release(),
                           now="2026-07-27T12:00:00Z")
        assert "claim                 foreign (stale) — session s-other" in text

    def test_inconsistent_record_renders_hard_stop(self):
        release = _release(tag={"name": "v0.9.9", "sha": SHA_MERGE})
        text = self._brief(release=release)
        assert "Release: 0.5.0 — record inconsistent (hard stop; see warning below)" in text
        assert "release record inconsistent" in text

    def test_non_release_brief_byte_identical(self):
        # The block keys off decision["release"] alone: a non-release run's
        # brief is byte-for-byte the text of a decision without the key.
        state = _state(phase="interview", stop_reason="question",
                       goal="align", estimate={"sessions": 2, "time": None})
        events = [{"ts": "2026-07-27T10:00:00Z", "session": "s-0",
                   "type": "phase", "note": "interview"}]
        d = run_state.decide(state, events, "s-1", sessions_used=1)
        with_key = run_state.format_brief(d)
        legacy = dict(d)
        del legacy["release"]
        assert with_key == run_state.format_brief(legacy)
        assert "Release:" not in with_key
