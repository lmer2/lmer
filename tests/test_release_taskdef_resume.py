"""Tests for the release resume contract (`release-resume.jinja2`).

The resume body is a shared partial included by both the release
instructions ("Resuming an in-progress release") and the follow-up-session
document `taskdef/release/followup.txt`. Two halves are covered:

  - kernel: every run-state combination derives to exactly ONE frozen next
    step (`work_repo.release_run.derive_leg`/`next_step`) — the resume
    promise "exactly one next action" is a property of recorded state
    alone, including out-of-order receipt subsets and odd hand-built
    states (first gap in the ladder wins).
  - document: the rendered decision table cites every frozen next_step
    name verbatim, once each, in ladder order; the abandoned-release path
    releases the run claim (`work release unclaim`) instead of stalling it
    forever; the burned-version runbook repairs a spent PyPI version by
    yanking plus a new patch release, never by deleting or re-pointing a
    tag; and `followup.txt` resolves and renders through the taskdef tier
    search.

Rendering conventions mirror tests/test_release_taskdef.py: builtin tier
pinned to this checkout, LMER_* env stripped per test.
"""
import itertools
from pathlib import Path

import pytest

from hooks.followup import find_followup_file, read_and_display_followup
from hooks.start import render_taskdef_template
from tests.conftest import strip_lmer_env
from work_repo import release_run

REPO_TASKDEF = Path(__file__).parent.parent / "taskdef"
INSTRUCTIONS = REPO_TASKDEF / "release" / "instructions.txt"
FOLLOWUP = REPO_TASKDEF / "release" / "followup.txt"
RESUME_PARTIAL = REPO_TASKDEF / "release-resume.jinja2"

SHA_BUMP = "a" * 40
SHA_MERGE = "b" * 40

# The frozen leg-2 receipt ladder, in spec order (a prefix of full receipts
# maps to the step that records the first missing one).
RECEIPT_LADDER = (
    ("github-main-push", "leg2-push-github-main"),
    ("github-tag-push", "leg2-push-github-tag"),
    ("actions-run", "leg2-poll-actions"),
    ("pypi", "leg2-record-pypi"),
    ("gitlab-tag-push", "leg2-push-gitlab-tag"),
    ("dep-refresh", "leg2-dep-refresh"),
)


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    """Strip LMER_* env vars so each test builds its own config."""
    strip_lmer_env(monkeypatch)


@pytest.fixture(autouse=True)
def _repo_builtin_root(monkeypatch):
    """Pin the builtin tier to this checkout's taskdef/ — the tests must
    exercise the templates under development, not a container mount."""
    monkeypatch.setattr(
        "hooks.start.builtin_taskdef_root", lambda: REPO_TASKDEF
    )
    monkeypatch.setenv("LMER_TASKDEF_ROOT", str(REPO_TASKDEF))


def _state(version="0.5.0", bump=SHA_BUMP, merge=SHA_MERGE, tag=True,
           receipts=()):
    """Hand-build a release record (consistent tag: name/SHA match the
    recorded version/merge SHA — derive_leg hard-stops on anything else)."""
    release = release_run.seed_release()
    release["version"] = version
    release["bump_mr_merge_sha"] = bump
    release["release_mr_merge_sha"] = merge
    if tag:
        release["tag"] = {"name": f"v{version}", "sha": merge}
    release["receipts"] = {name: {"recorded": "t"} for name in receipts}
    return release


def _render(template_file, work_mode="finish"):
    return render_taskdef_template(
        template_file,
        {
            "work_mode": work_mode,
            "run_state_brief": "",
            "instructions_file": str(INSTRUCTIONS),
            "followup_file": str(template_file),
        },
    )


def _assert_fully_rendered(out):
    assert out.strip()
    assert "{%" not in out and "{{" not in out


def _squash(text):
    """Collapse whitespace so prose assertions survive line wrapping."""
    return " ".join(text.split())


def _resume_section(out):
    """The resume H2 the instructions own, up to the next H2."""
    start = out.index("## Resuming an in-progress release")
    end = out.index("## Closing out the run", start)
    return out[start:end]


class TestResumeDerivationLadder:
    """Each run-state combination maps to a single unambiguous next step —
    the kernel property the decision table documents."""

    @pytest.mark.parametrize(
        "release, leg, step",
        [
            (None, "leg1", "leg1-bump"),
            ({}, "leg1", "leg1-bump"),
            (_state(version=None, bump=None, merge=None, tag=False),
             "leg1", "leg1-bump"),
            (_state(bump=None, merge=None, tag=False),
             "leg1", "leg1-record-bump-merge"),
            (_state(merge=None, tag=False), "gate",
             "gate-await-release-merge"),
            (_state(tag=False), "leg2", "leg2-create-tag"),
            (_state(), "leg2", "leg2-push-github-main"),
            (_state(receipts=("github-main-push",)),
             "leg2", "leg2-push-github-tag"),
            (_state(receipts=("github-main-push", "github-tag-push")),
             "leg2", "leg2-poll-actions"),
            (_state(receipts=("github-main-push", "github-tag-push",
                              "actions-run")),
             "leg2", "leg2-record-pypi"),
            (_state(receipts=("github-main-push", "github-tag-push",
                              "actions-run", "pypi")),
             "leg2", "leg2-push-gitlab-tag"),
            (_state(receipts=release_run.RECEIPT_NAMES),
             "complete", "complete"),
        ],
        ids=lambda value: value if isinstance(value, str) else None,
    )
    def test_ladder_state_derives_leg_and_single_step(
        self, release, leg, step
    ):
        derived = release_run.derive_leg(release)
        assert derived["leg"] == leg
        assert derived["next_step"] == step
        assert release_run.next_step(release) == step

    def test_every_receipt_subset_derives_exactly_one_step(self):
        """All 2^6 receipt subsets (identity fields complete) land on the
        step for the FIRST missing receipt in ladder order — out-of-order
        receipts never skip ahead."""
        for bits in itertools.product((False, True), repeat=len(RECEIPT_LADDER)):
            recorded = tuple(
                name for (name, _), present in zip(RECEIPT_LADDER, bits)
                if present
            )
            expected = "complete"
            for name, step in RECEIPT_LADDER:
                if name not in recorded:
                    expected = step
                    break
            derived = release_run.next_step(_state(receipts=recorded))
            assert derived == expected, recorded
            assert derived in release_run.STEPS

    def test_out_of_order_receipts_do_not_skip_the_ladder(self):
        """A gitlab-tag-push receipt without an actions-run receipt derives
        to leg2-poll-actions — never past the gap."""
        release = _state(
            receipts=("github-main-push", "github-tag-push",
                      "gitlab-tag-push")
        )
        assert release_run.next_step(release) == "leg2-poll-actions"

    def test_identity_gaps_win_over_later_records(self):
        """Odd hand-built states (later fields present, earlier ones
        missing) still land on the first gap — one answer, never two."""
        # Bump SHA without a version → the version gap wins.
        release = _state(version=None, merge=None, tag=False)
        assert release_run.next_step(release) == "leg1-bump"
        # Merge SHA without a bump SHA → the bump gap wins.
        release = _state(bump=None, tag=False)
        assert release_run.next_step(release) == "leg1-record-bump-merge"
        # Receipts without a tag → the tag gap wins (receipts ignored).
        release = _state(tag=False, receipts=("github-main-push",))
        assert release_run.next_step(release) == "leg2-create-tag"

    def test_inconsistent_recorded_tag_is_a_hard_stop_not_a_row(self):
        """A hand-edited record (tag SHA vs merge SHA disagreement) derives
        to NO step — the table only covers states the writers can produce."""
        release = _state()
        release["tag"] = {"name": "v0.5.0", "sha": "c" * 40}
        with pytest.raises(release_run.ReleaseRunError, match="re-point"):
            release_run.derive_leg(release)


class TestResumeDecisionTable:
    """The rendered decision table — keyed on the frozen next_step names."""

    def test_partial_is_shared_by_instructions_and_followup(self):
        directive = "{% include 'release-resume.jinja2' %}"
        assert INSTRUCTIONS.read_text().count(directive) == 1
        assert FOLLOWUP.read_text().count(directive) == 1

    def test_table_cites_every_frozen_step_once_in_ladder_order(self):
        """Each of the ten frozen next_step names appears in exactly one
        table row, in ladder order — one row, one step, no ambiguity."""
        section = _resume_section(_render(INSTRUCTIONS))
        rows = [
            line for line in section.splitlines()
            if line.startswith("|") and "`" in line
        ]
        assert len(rows) == len(release_run.STEPS)
        positions = []
        for step in release_run.STEPS:
            token = f"`{step}`"
            hits = [i for i, row in enumerate(rows) if token in row]
            assert len(hits) == 1, step
            positions.append(hits[0])
        assert positions == sorted(positions)

    def test_first_gap_wins_is_stated(self):
        section = _squash(_resume_section(_render(INSTRUCTIONS)))
        assert "stops at the FIRST missing record" in section
        assert "first gap wins" in section
        # The documented convergence example matches the kernel's behavior.
        assert "derives to `leg2-poll-actions`, not past it" in section

    def test_status_command_is_the_entry_point(self):
        section = _squash(_resume_section(_render(INSTRUCTIONS)))
        assert "`work release status`" in section
        assert "--json" in section
        # State decides the step; remotes only perform it.
        assert "never to decide what the step is" in section

    def test_resume_never_starts_over(self):
        section = _squash(_resume_section(_render(INSTRUCTIONS)))
        assert "NEVER starts over" in section
        assert "single source of truth" in section
        assert "exactly ONE next action" in section

    def test_reentry_idempotency_rules(self):
        section = _squash(_resume_section(_render(INSTRUCTIONS)))
        assert "never re-point, never re-sign" in section
        assert "refs already current → skip the push" in section
        assert "nothing is lost by a dead watcher" in section

    def test_waiting_rows_are_context_aware(self):
        """The gate and bump-merge rows split by session kind: a full
        release session re-arms/waits, a follow-up session exits
        immediately (followup.txt's scheduled-relaunch contract) — the two
        documents sharing this partial must not contradict each other."""
        section = _squash(_resume_section(_render(INSTRUCTIONS)))
        assert (
            "still open → in a full release session re-arm the watch and "
            "idle with pushed state; in a follow-up session exit "
            "immediately — the scheduled relaunch IS the re-arm" in section
        )
        assert (
            "still open → in a full release session wait for the merge; in "
            "a follow-up session exit immediately — the scheduled relaunch "
            "picks the merge up" in section
        )

    def test_leg1_bump_row_adopts_existing_branch_or_mr(self):
        """The crash-window hardening: a remote prep-<X.Y.Z> branch or open
        bump MR left by a session that died between push and record is
        adopted, never re-created or force-pushed over."""
        section = _squash(_resume_section(_render(INSTRUCTIONS)))
        assert (
            "an existing remote `prep-<X.Y.Z>` branch or open bump MR "
            "matching the dry-run's target version is ADOPTED" in section
        )
        assert "never re-created, never force-pushed" in section


class TestAbandonedRelease:
    """Bump merged, human declines: explicit abort releases the claim."""

    def test_declined_release_releases_the_claim(self):
        section = _squash(_resume_section(_render(INSTRUCTIONS)))
        assert "DECLINES the release MR" in section
        assert "`work release unclaim`" in section
        assert "NEVER a run left idling forever" in section
        assert "A stalled claim blocks every future release" in section

    def test_bump_commit_stays_and_leg1_reentry_is_idempotent(self):
        section = _squash(_resume_section(_render(INSTRUCTIONS)))
        assert "bump commit STAYS on `prep-release`" in section
        assert "never revert it" in section
        assert "skips the bump" in section

    def test_the_successor_run_comes_from_the_claim(self):
        """An aborted run is terminal, so the resume needs a NEW run — and
        the body has to say what creates it, or a session facing a refused
        claim has no move except hand-editing state back to in-progress."""
        section = _squash(_resume_section(_render(INSTRUCTIONS)))
        assert "never hand-edit it back to `in-progress`" in section
        assert "`work release claim` is what creates it" in section
        assert "rolls that run aside" in section
        assert "seeds the successor in the same atomic commit" in section

    def test_unmerged_is_not_declined(self):
        """Still-open MR is the gate step — abandonment needs the human."""
        section = _squash(_resume_section(_render(INSTRUCTIONS)))
        assert "must be the human's explicit decision" in section


class TestBurnedVersionRunbook:
    """A spent PyPI version: yank + new patch release, never tag surgery."""

    def test_yank_plus_new_patch_never_tag_surgery(self):
        section = _squash(_resume_section(_render(INSTRUCTIONS)))
        assert "PyPI filenames are permanent" in section
        assert "SPENT even if yanked" in section
        assert "NEW patch version through this same flow" in section
        assert "NEVER by deleting a tag" in section
        assert "NEVER by re-pointing a tag" in section
        assert "NEVER by re-signing" in section
        assert "not a repair opportunity" in section


class TestFollowupDocument:
    """followup.txt resolves and renders through the taskdef tier search."""

    def test_resolves_via_tier_search(self, monkeypatch):
        monkeypatch.setenv("LMER_TASK", "release")
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(REPO_TASKDEF))
        assert find_followup_file() == FOLLOWUP

    def test_renders_through_the_followup_hook_path(
        self, monkeypatch, capsys
    ):
        """The hook's own render path (tier-searched includes, source
        banner) produces the full resume contract."""
        monkeypatch.setenv("LMER_TASK", "release")
        monkeypatch.setenv("LMER_TASKDEF_PATHS", str(REPO_TASKDEF))
        assert read_and_display_followup(FOLLOWUP) is True
        out = capsys.readouterr().out
        assert f"taskdef source: {REPO_TASKDEF} (schema 1)" in out
        assert "## Resuming an in-progress release" in out
        for step in release_run.STEPS:
            assert f"`{step}`" in out, step

    @pytest.mark.parametrize("work_mode", ("finish", "phasic"))
    def test_renders_fully_with_the_shared_partial(self, work_mode):
        out = _render(FOLLOWUP, work_mode)
        _assert_fully_rendered(out)
        assert out.startswith("# Release — follow-up session")
        assert str(FOLLOWUP) in out
        # The shared partial's decision table renders inside the followup.
        for step in release_run.STEPS:
            assert f"`{step}`" in out, step
        squashed = _squash(out)
        assert "`work release unclaim`" in squashed
        assert "NEVER by deleting a tag" in squashed
        # A no-op relaunch exits instead of idling.
        assert "exit immediately" in squashed

    def test_partial_has_no_h2_of_its_own(self):
        """instructions.txt owns the H2; the partial contributes H3s only,
        so both inclusion sites keep a coherent heading hierarchy."""
        for line in RESUME_PARTIAL.read_text().splitlines():
            assert not line.startswith("## "), line
            assert not line.startswith("# "), line
