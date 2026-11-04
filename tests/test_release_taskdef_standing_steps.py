"""Tests for the release taskdef's standing privacy and CI steps (#651).

The leak scan, the pre-push guard and the GitHub CI candidate used to be
restated in every release brief. This module pins them as taskdef steps:

  - Phase 0.5 validates the project tier's `leak_scan` block through
    `lmer-release-guard check-config`, and never falls back to a run-dir copy
  - the gate runs the early leak scan after recording its phase and before
    arming the watch; a finding is an operator-only stop
  - leg 2 runs the gating rescan (2a) and the CI candidate (2b) before the
    tag, and the guard before each GitHub push
  - the red-CI rule: one flaky rerun, a test-only fix MR without waiting,
    an operator-only ask otherwise, abort as superseded only once the fix
    merged; the risk-based code-scanning check; dismissals need the operator

Rendering conventions mirror tests/test_release_taskdef.py: builtin tier
pinned to this checkout, LMER_* env stripped per test.
"""
from pathlib import Path

import pytest

from hooks.start import render_taskdef_template
from tests.conftest import strip_lmer_env

REPO_TASKDEF = Path(__file__).parent.parent / "taskdef"
INSTRUCTIONS = REPO_TASKDEF / "release" / "instructions.txt"
STEP_2A = "### Step 2a — `leg2-leak-scan` — the gating leak scan at the merge SHA"
STEP_2B = "### Step 2b — `leg2-ci-candidate` — GitHub CI on the exact merge SHA"
STEP_3 = "### Step 3 — `leg2-create-tag`"
STEP_4 = "### Step 4 — `leg2-push-github-main`"


@pytest.fixture(autouse=True)
def _clean_lmer_env(monkeypatch):
    strip_lmer_env(monkeypatch)


@pytest.fixture(autouse=True)
def _repo_builtin_root(monkeypatch):
    monkeypatch.setattr("hooks.start.builtin_taskdef_root", lambda: REPO_TASKDEF)
    monkeypatch.setenv("LMER_TASKDEF_ROOT", str(REPO_TASKDEF))


@pytest.fixture(scope="module")
def out():
    with pytest.MonkeyPatch.context() as mp:
        strip_lmer_env(mp)
        mp.setattr("hooks.start.builtin_taskdef_root", lambda: REPO_TASKDEF)
        mp.setenv("LMER_TASKDEF_ROOT", str(REPO_TASKDEF))
        return render_taskdef_template(INSTRUCTIONS, {
            "work_mode": "finish", "run_state_brief": "", "instructions_file": str(INSTRUCTIONS),
        })


def _squash(text):
    return " ".join(text.split())


def _between(text, start, end):
    return text[text.index(start):text.index(end)]


def _gate(out):
    return _between(out, "## Gate — human merge", "## Leg 2 — ship")


def _leg2(out):
    return _between(out, "## Leg 2 — ship", "## Resuming an in-progress release")


class TestPhase05:
    def test_block_is_validated_by_the_consuming_tool(self, out):
        phase = _squash(_between(out, "## Phase 0.5", "## Phase 0.75"))
        assert "leak_scan:" in phase and "public_mirror:" in phase
        assert "`lmer-release-guard check-config --config <resolved task.yaml>`" in phase
        assert "non-zero exit → HARD STOP" in phase
        assert "never fall back to a copy of the scan tool from an old run directory" in phase

    def test_missing_block_policy_lives_in_the_tool_only(self, out):
        """Q1 is a one-line switch in the tool; the taskdef must not restate it."""
        phase = _squash(_between(out, "## Phase 0.5", "## Phase 0.75"))
        assert "do not decide it here" in phase


class TestGateEarlyScan:
    def test_scan_runs_after_the_phase_and_before_the_watch(self, out):
        gate = _gate(out)
        phase = gate.index("work state set --phase=gate")
        step = gate.index("2. Run the early leak scan (below)")
        watch = gate.index("--check-updates --watch --command")
        assert phase < step < watch
        assert "### The early leak scan" in gate

    def test_scan_command_and_record(self, out):
        gate = _squash(_gate(out))
        assert ("work verify leak-scan-early -- lmer-release-guard scan --config <resolved task.yaml> "
                "--base <previous tag> --candidate <bump-merge-sha> --published github/main") in gate
        assert "`work artifact leak-scan.md --file <path>`" in gate
        assert "`leak_scan.public_mirror` is `false`" in gate
        assert "never copy it into `leak-scan.md`" in gate
        assert ("a binary file's bytes are checked for held values, token shapes, project patterns "
                "and guard strings (`binary` records)") in gate

    def test_finding_is_an_operator_only_stop_and_nothing_is_rewritten(self, out):
        gate = _squash(_gate(out))
        assert "Any finding → do NOT arm the watch" in gate
        assert "`lmer-signal ask --operator-only`" in gate
        assert "Rewriting, reverting or squashing anything is the operator's decision" in gate


class TestLeg2Order:
    def test_new_steps_sit_between_preconditions_and_the_tag(self, out):
        leg2 = _leg2(out)
        positions = [leg2.index(h) for h in (
            "### Step 2 — `leg2-preconditions`", STEP_2A, STEP_2B, STEP_3, STEP_4)]
        assert positions == sorted(positions)

    def test_guard_before_each_github_push(self, out):
        leg2 = _leg2(out)
        step4 = leg2[leg2.index(STEP_4):]
        main_guard = step4.index("work verify prepush-guard-main -- lmer-release-guard prepush")
        tag_guard = step4.index("work verify prepush-guard-tag -- lmer-release-guard prepush")
        main_push = step4.index("`bin/gate-push --remote github`")
        tag_push = step4.index("`bin/gate-push --tag <tag> --remote github`")
        assert main_guard < main_push and tag_guard < tag_push
        assert "refs/heads/main" in step4[main_guard:main_push]
        step2b = _between(leg2, STEP_2B, STEP_3)
        assert step2b.index("prepush-guard-candidate") < step2b.index(
            "bin/gate-push --ref <merge-sha>:refs/heads/release-candidate/v<X.Y.Z> --remote github")


class TestLeg2LeakScan:
    def test_rescan_at_the_merge_sha(self, out):
        step = _squash(_between(_leg2(out), STEP_2A, STEP_2B))
        assert ("work verify leak-scan -- lmer-release-guard scan --config <resolved task.yaml> "
                "--base <previous tag> --candidate <merge-sha> --published github/main") in step
        assert "STOP before anything is pushed" in step
        assert "--operator-only" in step


class TestLeg2CiCandidate:
    @pytest.fixture
    def step(self, out):
        return _squash(_between(_leg2(out), STEP_2B, STEP_3))

    def test_no_tag_until_green_on_the_exact_sha(self, out, step):
        assert "No tag, local or remote, until GitHub CI has passed on exactly the recorded merge SHA" in step
        assert ("holds at least one `pull_request` run for head SHA `<merge-sha>`, and every run in it "
                "concluded `success`. An empty run list is not green") in step
        assert "--limit 100 --json databaseId,status,conclusion,headSha,workflowName" in step
        assert "work verify candidate-merge-tree" in step
        tag = _squash(_between(_leg2(out), STEP_3, STEP_4))
        assert "Only after step 2a found nothing and step 2b is green" in tag

    def test_draft_pr_is_public_and_never_merged(self, step):
        assert "gh pr create --draft --base main --head release-candidate/v<X.Y.Z>" in step
        assert "NEVER merge it" in step

    def test_wait_is_one_background_job(self, step):
        assert "ONE background job" in step and "No polling loop" in step

    def test_wait_for_the_first_run_is_bounded(self, step):
        assert "every 30 seconds for at most 15 minutes" in step
        assert "operator-only ask (CI never started)" in step
        assert "If the final list holds a run not yet completed, it watches that run too and lists again" in step

    def test_push_grant_and_divergence_go_to_the_operator(self, step):
        assert "A refusal is an operator-only ask for the grant" in step
        assert "never a force-push" in step

    def test_code_scanning_is_risk_based(self, step):
        assert "who can actually reach that code path" in step
        assert ("Any **new** alert a non-operator can reach with real impact → STOP with an operator-only "
                "ask before the tag and the `main` push") in step
        assert "every new alert operator-only or a false positive) → proceed without asking" in step
        assert "Draft a follow-up issue" in step
        assert "pre-existing alerts never stop the release" in step

    def test_code_scanning_reads_every_page(self, step):
        assert step.count("gh api --paginate") == 3
        assert "gh api \"repos" not in step

    def test_reachability_comes_from_the_project_tier(self, step):
        assert "judged against the project's documented trust boundaries" in step
        assert "No documented boundaries → every caller is a non-operator" in step
        assert "the platform's password" not in step
        assert "A 403 means the token lacks code-scanning read: an operator-only ask, never a skip" in step

    def test_dismissals_need_the_operator_and_public_safe_comments(self, out, step):
        assert "Dismissing an alert needs the operator's explicit authorization, every time" in step
        assert "`lmer-release-guard scan-text`" in step
        rules = _squash(out[out.index("## HARD RULES"):])
        assert "NEVER dismiss a code-scanning alert without the operator's explicit authorization" in rules

    def test_red_ci_rule(self, step):
        assert "re-run the failed jobs ONCE on the same SHA" in step
        assert "`gh run rerun <run-id> --failed`" in step
        assert "**Test-only fix**" in step and "Do not wait for the operator" in step
        assert "open an MR into `prep-release` — never `main`" in step
        assert "**Any fix outside test code** (a path outside the test roots): STOP with an operator-only ask" in step

    def test_test_only_is_a_path_fact(self, step):
        assert ("every path in the fix's `git diff --name-only --no-renames origin/prep-release...HEAD` "
                "(a moved file shows both of its paths) is under `tests/`, or under a test root the "
                "project info names as one") in step
        assert "check the path predicate on the commit before anything is pushed" in step
        assert "every changed path is test code" not in step

    def test_abort_only_after_the_fix_merged(self, step):
        assert "once the fix MR has MERGED, end the run with" in step
        assert 'work release abort --reason "superseded: GitHub CI red at <merge-sha>' in step
        assert "it never aborts early" in step


class TestHardRulesAndResume:
    def test_hard_rules(self, out):
        rules = _squash(out[out.index("## HARD RULES"):])
        assert "Nothing reaches the GitHub mirror without a `GUARD PASS`" in rules
        assert "NO tag, local or remote, until GitHub CI has passed on exactly the recorded merge SHA" in rules
        assert "nothing is rewritten without the operator's decision" in rules

    def test_resume_row_folds_the_new_steps(self, out):
        resume = _squash(out[out.index("## Resuming an in-progress release"):])
        assert "rescan for leaks at the merge SHA, prove GitHub CI green on exactly that SHA" in resume
        assert "`leg2-leak-scan` and `leg2-ci-candidate` phase names never appear as a derived next step" in resume
        ladder = _squash(_leg2(out)[_leg2(out).index("### The idempotency ladder"):])
        assert "Fix MR merged → `work release abort` as superseded" in ladder


class TestReviewHardeningText:
    def test_gate_adds_the_mirror_remote_before_fetching_it(self, out):
        gate = _gate(out)
        assert gate.index("git remote add github") < gate.index("github/main`")

    def test_exit_1_is_never_zero_hits(self, out):
        assert "Exit 1 means the scan did NOT run" in _squash(_gate(out))
        leg2 = _squash(_leg2(out))
        assert "A non-zero exit without a `GUARD FAIL` line means the guard did not run" in leg2

    def test_code_scanning_needs_an_analysis_first(self, out):
        step = _squash(_between(_leg2(out), STEP_2B, STEP_3))
        assert "An empty alert list proves nothing until an analysis exists for this candidate" in step
        assert 'Only a 404 saying code scanning is not enabled counts as "not configured"' in step

    def test_red_ci_paths_are_recorded_and_adopted(self, out):
        step = _squash(_between(_leg2(out), STEP_2B, STEP_3))
        assert 'work event ci-candidate-rerun --note "<run URL>"' in step
        assert "ONCE is per candidate, across sessions" in step
        assert "`gh run view <run-id> --json attempt`" in step
        assert "Adopt, never duplicate" in step
        assert 'work event ci-candidate-fix --note "<MR URL>"' in step
        ladder = _leg2(out)[_leg2(out).index("### The idempotency ladder"):]
        assert ladder.index("a `ci-candidate-fix` event recorded") < ladder.index("| No tag yet | Re-run")
