"""Tests for the leg-2 (ship) body of the `release` taskdef.

`taskdef/release-leg2.jinja2` is included by the release spine under its
"## Leg 2 — ship" heading. This module covers what the body owns:

  - the idempotent step ladder keyed on the recorded merge SHA: record the
    merge SHA with the version re-read at that SHA, preconditions as
    receipts, the SSH-signed tag at exactly the merge SHA, GitHub `main`
    then the GitHub tag, the Actions poll, the GitLab tag last
  - every hard stop: version mismatch at the merge SHA, GitHub `main`
    divergence (documented remediation, never a force-push), a tag pointing
    anywhere but the recorded SHA (never re-point/re-sign/delete), a red
    Actions run (fail loudly with the run URL), a rejected push (credential
    requirement, no fallback path)
  - convergence on re-entry: skip rules for current refs and green runs,
    and workflow re-dispatch via the API instead of re-tagging
  - the consumption contract: gate-push with the frozen `--tag`/`--remote`
    flags, the provisioned signing key, the frozen `work release record`
    verbs — never home-grown replacements
  - command hygiene: no rendered tag or push command carries a force flag

Rendering conventions mirror tests/test_release_taskdef.py: builtin tier
pinned to this checkout, LMER_* env stripped per test.
"""
import re
from pathlib import Path

import pytest

from hooks.start import render_taskdef_template
from tests.conftest import strip_lmer_env
from tests.test_release_askpass import ASKPASS

REPO_TASKDEF = Path(__file__).parent.parent / "taskdef"
INSTRUCTIONS = REPO_TASKDEF / "release" / "instructions.txt"
LEG2_PARTIAL = REPO_TASKDEF / "release-leg2.jinja2"
ASKPASS_CONTAINER_PATH = f"/Agents/global/bin/{ASKPASS.name}"

# The leg-2 step ladder, in the only order it may render in.
STEP_HEADINGS = (
    "### Step 1 — `leg2-record-merge` — record the merge SHA",
    "### Step 2 — `leg2-preconditions` — preconditions, each a receipt",
    "### Step 3 — `leg2-create-tag` — the SSH-signed tag, at exactly the "
    "merge SHA",
    "### Step 4 — `leg2-push-github-main` / `leg2-push-github-tag` — "
    "GitHub: `main` first, THEN the tag",
    "### Step 5 — `leg2-poll-actions` — the GitHub Actions release run",
    "### Step 6 — `leg2-push-gitlab-tag` — the GitLab tag, LAST",
    "### Step 7 — `leg2-dep-refresh` — open the next cycle's dependency "
    "refresh",
    "### Step 8 — receipts complete, run complete",
    "### The idempotency ladder (re-entry map)",
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


def _leg2(out=None):
    """Slice the rendered leg-2 section out of the full document.

    The spine owns the H2 heading; the partial body runs from there to the
    next spine section."""
    if out is None:
        out = _render()
    start = out.index("## Leg 2 — ship")
    end = out.index("## Resuming an in-progress release")
    assert start < end
    return out[start:end]


def _squash(text):
    """Collapse whitespace so prose assertions survive line wrapping."""
    return " ".join(text.split())


def _command_spans(text):
    """Extract every command-bearing code region: fenced blocks first, then
    inline backtick spans from the remaining prose."""
    fence_re = re.compile(r"```[^\n]*\n(.*?)```", re.S)
    fences = fence_re.findall(text)
    prose = fence_re.sub("", text)
    inline = re.findall(r"`([^`\n]+)`", prose)
    return fences + inline


class TestLeg2StepLadder:
    """The rendered ladder: presence, order, and the frozen push ordering."""

    def test_body_is_authored_and_fully_rendered(self):
        assert LEG2_PARTIAL.exists()
        out = _render()
        assert "{%" not in out and "{{" not in out
        leg2 = _leg2(out)
        # A one-line placeholder cannot satisfy the ladder.
        assert "authored by taskdef.leg2 task" not in leg2
        for heading in STEP_HEADINGS:
            assert heading in leg2, heading

    def test_steps_render_in_order(self):
        leg2 = _leg2()
        positions = [leg2.index(h) for h in STEP_HEADINGS]
        assert positions == sorted(positions)

    def test_github_main_then_tag_then_gitlab_last(self):
        """The frozen push ordering: GitHub `main`, the GitHub tag, the
        Actions run, and only then the GitLab tag."""
        leg2 = _leg2()
        order = [
            "work release record receipt github-main-push",
            "work release record receipt github-tag-push",
            "work release record receipt actions-run",
            "work release record receipt gitlab-tag-push",
        ]
        positions = [leg2.index(item) for item in order]
        assert positions == sorted(positions)
        squashed = _squash(leg2)
        assert "GitHub: `main` first, THEN the tag" in squashed
        assert "`main` must land before the trigger fires" in squashed
        assert "Only after the GitHub run is green" in squashed
        assert "Public-before-internal is deliberate" in squashed
        assert "no half-released internal state" in squashed

    def test_dep_refresh_never_blocks_the_shipped_release(self):
        """Step 7 runs after the release is out, so none of its failure
        modes may stop the run: an opted-out repository, a no-op refresh
        and a red gate all record the receipt and advance."""
        squashed = _squash(_leg2())
        assert "This step never blocks the release" in squashed
        assert "No `dep_refresh` parameter" in squashed
        assert "EMPTY means every dependency was already at the newest" in squashed
        assert "A RED gate is NOT a hard stop for the release" in squashed
        assert "Never commit around a failing gate to get the MR open" in squashed

    def test_dep_refresh_targets_prep_release_and_is_adopted_not_duplicated(self):
        """The refresh MR follows the same target rule as every other MR in
        this flow, and re-entry adopts an open one rather than opening a
        second."""
        squashed = _squash(_leg2())
        assert "open the MR targeting `prep-release` — NEVER `main`" in squashed
        assert "is ADOPTED (record its URL as the receipt), never a second one" in squashed

    def test_dep_refresh_stages_before_the_commit_gate(self):
        """`bin/gate-commit` commits the index and stages nothing, while its
        git-status check fails critically on any unstaged path — so an
        unstaged lockfile would produce a red gate that never ran the suite,
        which this step's own rule then records as "refresh failed the gate"."""
        squashed = _squash(_leg2())
        assert "stage what the refresh produced, then gate it" in squashed
        assert "`bin/gate-commit` commits the index and stages nothing" in squashed
        assert "a red gate that never ran the suite" in squashed

    def test_dep_refresh_reads_the_command_exit_code_before_the_tree(self):
        """A refresh command that dies without touching the tree leaves
        `git status --porcelain` empty; reading the tree first would record
        that as "no dependency changes" — the opposite of what happened."""
        squashed = _squash(_leg2())
        assert "NON-ZERO means the refresh did not run to completion" in squashed
        assert 'that is NOT "no dependency changes"' in squashed
        assert "Only on a zero exit, read `git status --porcelain`" in squashed

    def test_dep_refresh_checks_for_an_existing_branch_or_mr_first(self):
        """The adopt rule lives in the step body, not only in the ladder
        table 40-odd lines below it: a session resuming at
        `leg2-dep-refresh` executes these bullets in order."""
        squashed = _squash(_leg2())
        assert "Adoption, never re-creation — FIRST, before creating anything" in squashed
        assert "git ls-remote origin refs/heads/dep-refresh-<X.Y.Z>" in squashed
        assert "NEVER open a second MR and NEVER force-push over it" in squashed

    def test_dep_refresh_is_why_leg1_still_refuses_dependency_movement(self):
        """The two rules are a pair: leg 1 refuses a lockfile diff beyond
        the version line precisely because this step owns that movement."""
        squashed = _squash(_leg2())
        assert "stays exactly as it is" in squashed
        assert "this step is where dependency movement is allowed to happen" in squashed

    def test_every_step_keys_on_the_recorded_merge_sha(self):
        squashed = _squash(_leg2())
        assert (
            "Everything in this leg keys on ONE fact: the release-MR merge "
            "SHA recorded in run state" in squashed
        )
        assert (
            "verifies its target against the recorded merge SHA or it does "
            "not run" in squashed
        )
        # The recording verb, with the version bound in the same breath.
        assert (
            "work release record merge-sha <merge-sha> "
            "--version <observed-version>" in squashed
        )

    def test_version_is_reread_from_pyproject_at_the_merge_sha(self):
        squashed = _squash(_leg2())
        assert "git show <merge-sha>:pyproject.toml" in squashed
        assert "never the working tree, never leg 1's memory" in squashed
        # The tag name derives from that re-read, not from leg 1.
        assert "the version re-read in step 1 at the merge SHA" in squashed
        assert "never a name remembered from leg 1" in squashed

    def test_preconditions_are_receipts(self):
        squashed = _squash(_leg2())
        assert "work verify gitlab-main-pipeline" in squashed
        assert "work verify github-ancestry" in squashed
        assert "git merge-base --is-ancestor" in squashed
        # Pinned to the merge SHA, never a moving tip.
        assert 'never "latest on main"' in squashed
        # The pipeline receipt is not vacuous: the exit code carries the
        # verdict, so a red pipeline can never record a green receipt.
        assert "gitlab-pipeline <project> <pipeline-id> --host <hostname> " \
            "--expect-status success" in squashed
        assert "never record a green checkmark for a red pipeline" in squashed

    def test_tag_is_ssh_signed_at_exactly_the_merge_sha(self):
        leg2 = _leg2()
        assert "gpg.format=ssh" in leg2
        assert "tag -s <tag> <merge-sha>" in leg2
        assert "user.signingkey=/release-signing-key" in leg2
        assert "work release record tag <tag> --sha <merge-sha>" in leg2
        # And the created tag is verified before it is recorded.
        assert "tag -v <tag>" in leg2

    def test_actions_receipts_record_the_uploading_run(self):
        """Re-run artifact drift: the URL must name the run that uploaded,
        not the last green one — a failed-jobs-only re-run leaves
        publish-pypi untouched."""
        squashed = _squash(_leg2())
        assert (
            "work release record receipt actions-run --url "
            "<Actions run URL>" in squashed
        )
        assert (
            "work release record receipt pypi --url <PyPI release URL>"
            in squashed
        )
        assert "record the run that UPLOADED" in squashed
        assert "a later green run may have uploaded nothing" in squashed


class TestLeg2HardStops:
    """Every hard stop the body owns, with its recording contract."""

    def test_version_mismatch_at_merge_sha(self):
        squashed = _squash(_leg2())
        assert "The recorder HARD STOPS when the observed version" in squashed
        assert "a second bump or a foreign commit landed" in squashed
        assert "a human decision is required, not a repair attempt" in squashed
        assert "version mismatch at merge SHA" in squashed
        assert "--stop-reason=critical_error" in squashed

    def test_github_divergence_is_documented_never_forced(self):
        squashed = _squash(_leg2())
        assert (
            "a HARD STOP with a documented remediation path, NEVER a "
            "force-push" in squashed
        )
        assert "The remediation path is a human one" in squashed
        # A push-time non-fast-forward is the same stop, not a retry.
        assert "same contract, no retry, no force" in squashed

    def test_existing_tag_elsewhere_is_a_hard_error(self):
        squashed = _squash(_leg2())
        assert "pointing at exactly the recorded merge SHA" in squashed
        assert (
            "pointing ANYWHERE else, or unsigned, or the signature does "
            "not verify → HARD ERROR" in squashed
        )
        assert "Never re-point, never re-sign, never delete" in squashed

    def test_red_actions_run_fails_loudly_with_the_url(self):
        squashed = _squash(_leg2())
        assert "FAIL LOUDLY with the Actions run URL" in squashed
        assert "GitHub release run red" in squashed
        assert "Nothing has been published internally yet" in squashed

    def test_rejected_push_names_the_credential_requirement(self):
        squashed = _squash(_leg2())
        assert (
            "hard error naming the credential requirement — never an "
            "alternative push path" in squashed
        )


class TestLeg2Idempotency:
    """Re-entry converges: skips for done work, re-dispatch over re-tag."""

    def test_rerecording_the_merge_sha_is_safe_and_rechecked(self):
        squashed = _squash(_leg2())
        assert (
            "the mismatch check runs even on an idempotent re-record"
            in squashed
        )

    def test_current_refs_skip_the_push(self):
        squashed = _squash(_leg2())
        # Each of the three pushes carries its own skip-when-current rule.
        assert squashed.count("skip when current") >= 2
        assert "Skip when current" in squashed
        assert squashed.count("Skip the push; ensure the receipt exists") == 3

    def test_green_actions_skips_the_poll(self):
        squashed = _squash(_leg2())
        assert "Already green for that SHA → skip the poll" in squashed
        assert "Skip the poll; receipts must name the run that uploaded" in (
            squashed
        )

    def test_red_actions_redispatches_never_retags(self):
        squashed = _squash(_leg2())
        assert (
            "re-dispatch the FAILED JOBS ONLY via the API, keyed on the "
            "recorded SHA" in squashed
        )
        assert "gh run rerun <run-id> --failed" in squashed
        # `gh workflow run` can never work: release.yml has no
        # workflow_dispatch trigger — the rerun is the ONLY re-dispatch.
        assert "gh workflow run" not in squashed
        assert "NEVER re-tag to retrigger" in squashed
        assert (
            "the tag is immutable: never deleted, never re-pointed, never "
            "re-signed" in squashed
        )

    def test_redispatch_is_failed_jobs_only_and_says_why(self):
        """`release.yml` carries no `skip-existing`, so a FULL re-run of a
        run whose publish already succeeded re-attempts an upload PyPI
        holds; PyPI refuses it and the run can never converge. The ladder
        must therefore never instruct a bare `gh run rerun`."""
        leg2 = _leg2()
        squashed = _squash(leg2)
        assert "The `--failed` is not optional." in squashed
        assert "`release.yml` sets no `skip-existing`" in squashed
        # No bare re-dispatch anywhere: every `gh run rerun` carries --failed.
        for line in leg2.splitlines():
            if "gh run rerun" in line:
                assert "--failed" in line, (
                    f"bare `gh run rerun` re-runs publish-pypi: {line.strip()!r}"
                )

    def test_version_reuse_is_pypi_refusal_not_a_repository_variable(self):
        """The reuse gate is PyPI refusing a duplicate. The retired
        `RELEASE_RESUME_VERSION` override must not survive in the ladder."""
        squashed = _squash(_leg2())
        assert "RELEASE_RESUME_VERSION" not in squashed
        assert "Gate version reuse" not in squashed
        assert "Red at **Publish to PyPI**" in squashed
        assert "Recovery means cutting a new version" in squashed

    def test_the_ladder_table_maps_every_done_state(self):
        squashed = _squash(_leg2())
        assert "Leg 2 may be entered any number of times" in squashed
        assert (
            "every step compares its target to the recorded merge SHA "
            "before acting" in squashed
        )
        assert "Nothing to advance — close out the run" in squashed


class TestLeg2ConsumptionContracts:
    """Frozen surfaces are consumed, never re-implemented."""

    def test_pushes_go_through_gate_push_with_frozen_flags(self):
        leg2 = _leg2()
        assert "bin/gate-push --remote github" in leg2
        assert "bin/gate-push --tag <tag> --remote github" in leg2
        assert "bin/gate-push --tag <tag> --remote origin" in leg2
        # The allow-list grammar the grants use.
        assert "repo|refs/tags/*" in leg2

    def test_credentials_are_consumed_not_reimplemented(self):
        leg2 = _leg2()
        squashed = _squash(leg2)
        assert "Credentials are provisioned, never fetched" in squashed
        assert "/release-signing-key" in squashed
        assert "LMER_RELEASE_SIGNING_KEY" in squashed
        assert f"GIT_ASKPASS={ASKPASS_CONTAINER_PATH}" in leg2
        assert "LMER_GIT_ASKPASS_USERNAME=x-access-token" in leg2
        assert (
            'LMER_GIT_ASKPASS_PASSWORD="${LMER_RELEASE_GITHUB_TOKEN}"'
            in leg2
        )
        assert "GIT_CONFIG_COUNT" not in leg2
        assert "GIT_CONFIG_KEY_" not in leg2
        assert (
            "do NOT hunt for keys elsewhere, generate one, or re-implement "
            "provisioning" in squashed
        )

    def test_status_verb_drives_reentry(self):
        squashed = _squash(_leg2())
        assert "run `work release status` first" in squashed
        assert "the derived leg, and the single next step" in squashed

    def test_no_rendered_tag_or_push_command_carries_a_force_flag(self):
        """Command hygiene across the WHOLE rendered document: no tag or
        push invocation may carry `-f`/`--force*` or a `+ref` refspec.
        Prose may say "force-push" (to forbid it); commands may not."""
        force_flag = re.compile(r"(^|\s)(-f|--force(?:-[a-z-]+)?)(\s|$)")
        offenders = []
        for span in _command_spans(_render()):
            for line in span.splitlines():
                if not re.search(r"\btag\b|\bpush\b", line):
                    continue
                if force_flag.search(line) or "+refs" in line:
                    offenders.append(line)
        assert not offenders, offenders
