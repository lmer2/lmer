"""Tests for the release taskdef's gate body (`release-gate.jinja2`).

The gate is the human-merge pause between leg 1 (bump MR) and leg 2 (ship):
the run arms the reviewer CLI's `--check-updates --watch` primitive on the
release MR and idles with pushed run state. This module covers the contract
the gate body owns:

  - the watch is armed on the release MR via
    `gitlab-review ... --check-updates --watch --command "/followup"`
  - run state is recorded and pushed (`work state set` + `work commit`)
    BEFORE the idle begins — an unarmed-by-state watcher is invisible
  - the human merge IS the release approval; the body never instructs the
    agent to merge (or approve) the release MR itself
  - watch is best-effort while resume is the contract: a merge nothing
    observed is picked up at the next launch, and the scheduled relaunch is
    declarative (documented in docs/RELEASE-FLOW.md — no cron here)
  - no polling loop may hold the gate open on session memory instead of
    run state

Rendering conventions mirror tests/test_release_taskdef.py: builtin tier
pinned to this checkout, LMER_* env stripped per test.
"""
from pathlib import Path

import pytest

from hooks.start import render_taskdef_template
from tests.conftest import strip_lmer_env

REPO_TASKDEF = Path(__file__).parent.parent / "taskdef"
INSTRUCTIONS = REPO_TASKDEF / "release" / "instructions.txt"

WATCH_COMMAND = (
    "gitlab-review <project> <release-mr-iid> --host <hostname> "
    '--check-updates --watch --command "/followup"'
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


def _render(work_mode="finish"):
    return render_taskdef_template(
        INSTRUCTIONS,
        {
            "work_mode": work_mode,
            "run_state_brief": "",
            "instructions_file": str(INSTRUCTIONS),
        },
    )


def _gate_section(out=None):
    """The rendered gate body: everything between the spine's gate heading
    and the leg-2 heading."""
    out = out if out is not None else _render()
    return out[out.index("## Gate — human merge") : out.index("## Leg 2 — ship")]


def _squash(text):
    """Collapse whitespace so prose assertions survive line wrapping."""
    return " ".join(text.split())


class TestGateArmsWatch:
    """The gate arms `--check-updates --watch` on the release MR."""

    def test_renders_cleanly_inside_the_spine(self):
        out = _render()
        assert "{%" not in out and "{{" not in out
        gate = _gate_section(out)
        assert gate.strip()
        # The body starts below the spine's H2 and never emits its own H2.
        body = gate[len("## Gate — human merge") :]
        assert "\n## " not in body

    def test_watch_command_is_the_reviewer_cli_primitive(self):
        gate = _squash(_gate_section())
        assert WATCH_COMMAND in gate

    def test_watch_targets_the_release_mr(self):
        gate = _squash(_gate_section())
        assert "Arm the watch on the release MR" in gate
        # The release MR is the standing prep-release → main MR from leg 1.
        assert "`prep-release` → `main` MR is the release MR" in gate

    def test_merge_detection_verifies_merged_state(self):
        """The watch fires on any MR activity — the gate confirms the MR is
        actually merged before entering leg 2."""
        gate = _squash(_gate_section())
        assert "confirm the MR state is actually `merged`" in gate
        assert "FIRST action is recording the release MR's merge SHA" in gate

    def test_wake_reclaims_before_any_mutating_action(self):
        """A long idle can outlive the claim's stale threshold and a
        scheduled relaunch may have taken over — on wake, BOTH sessions see
        the merge, so the gate re-arbitrates via `work release claim`
        before leg 2 touches anything (single-flight holds at the action
        point, not across the idle)."""
        gate = _squash(_gate_section())
        assert "Re-run `work release claim` BEFORE any other action" in gate
        assert "race the tag creation" in gate
        # The re-claim precedes the leg-2 handoff in the merge-detection list.
        raw = _gate_section()
        assert raw.index("Re-run `work release claim`") < raw.index(
            "Proceed to Leg 2")


class TestGateStateDiscipline:
    """State is recorded and pushed before the idle, and re-read — never
    remembered — across the gate."""

    def test_state_is_pushed_before_idling(self):
        gate = _gate_section()
        phase = gate.index("work state set --phase=gate")
        commit = gate.index("work commit")
        watch = gate.index("--check-updates --watch")
        assert phase < commit < watch
        squashed = _squash(gate)
        assert "Record the transition BEFORE idling" in squashed
        assert "not armed until the pushed run state says so" in squashed

    def test_state_is_pushed_at_every_transition(self):
        gate = _squash(_gate_section())
        assert "Push state at EVERY transition" in gate
        assert "Unpushed state does not exist to the next session" in gate

    def test_no_polling_loop_on_session_memory(self):
        gate = _squash(_gate_section())
        assert (
            "NEVER hold the gate open with a polling loop that re-derives "
            "progress from session memory" in gate
        )
        # Every wake re-reads run state through the work CLI.
        assert "reads run state (`work release status`) before acting" in gate

    def test_gate_entry_requires_leg1_recorded_in_run_state(self):
        gate = _squash(_gate_section())
        assert (
            "Leg 1 is complete only when run state carries the release "
            "version and the bump-MR merge SHA" in gate
        )


class TestGateApproval:
    """The human merge is the approval; the agent never merges."""

    def test_human_merge_is_the_release_approval(self):
        gate = _squash(_gate_section())
        assert (
            "The human merging the protected-`main` release MR on the "
            "canonical GitLab repository IS the release approval" in gate
        )
        assert "There is no other approval artifact" in gate

    def test_never_instructs_the_agent_to_merge(self):
        gate = _squash(_gate_section())
        assert "NEVER merge the release MR yourself" in gate
        assert "never accept or approve it on the human's behalf" in gate
        # Every imperative around merging in the gate body is the
        # prohibition or the human's act — no "merge the MR" instruction
        # addressed to the agent survives outside those.
        for line in _gate_section().splitlines():
            lowered = line.lower()
            if lowered.strip().startswith(("- merge", "merge ")):
                pytest.fail(f"imperative merge instruction: {line!r}")

    def test_idle_means_no_other_work(self):
        gate = _squash(_gate_section())
        assert "Do no other work while the gate is open" in gate
        assert "the run advances only on the human's merge" in gate


class TestGateResumeContract:
    """Watch is best-effort; resume — driven by run state — is the
    contract."""

    def test_best_effort_watch_resume_contract(self):
        gate = _squash(_gate_section())
        assert "Watch is best-effort; resume is the contract" in gate
        assert (
            "Run state, not this session, is what holds the release open"
            in gate
        )
        assert (
            "A merge that happens while nothing is watching is picked up "
            "at the next launch" in gate
        )

    def test_scheduled_relaunch_is_declarative_and_documented_elsewhere(self):
        """This repo has no cron facility — the periodic relaunch is
        declared and documented by the docs subsystem, not built here."""
        gate = _squash(_gate_section())
        assert "scheduled declaratively, outside this repository" in gate
        assert "lmer ships no cron facility" in gate
        assert "`docs/RELEASE-FLOW.md`" in gate
        assert "Manual relaunch works identically" in gate

    def test_missing_watch_primitive_ends_the_session_not_a_hand_rolled_loop(
        self,
    ):
        gate = _squash(_gate_section())
        assert "do NOT substitute a hand-rolled watcher" in gate
        assert "end the session — the scheduled relaunch carries the gate" in gate
