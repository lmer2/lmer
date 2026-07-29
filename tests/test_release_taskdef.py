"""Tests for the built-in `release` taskdef skeleton.

The `release` taskdef drives the two-leg release flow (leg 1: version bump +
changelog MR on GitLab; gate: human merges the release MR; leg 2: signed tag,
GitHub push + Actions publish, GitLab tag last). This module covers the parts
the skeleton itself owns:

  - the rendered spine: overview, single-flight claim consumption, the leg /
    gate / resume sections in order, and the include wiring to the shared
    `release-*.jinja2` partials (whose bodies are authored separately — no
    assertions on partial content here)
  - the parameterization contract: the four per-repo parameters resolve from
    a `task.yaml` manifest through the taskdef tier precedence, the built-in
    tier's manifest deliberately carries no `release:` mapping (it declares
    session needs only), and a missing/ambiguous parameter is a documented
    hard stop — never a default
  - the HARD RULES block: never force-push, never delete/re-point a tag,
    never re-sign, no fallback push path, and no taskdef-side lock (the
    single-flight lock is the run-state layer's atomic claim, only consumed
    here)
  - the cross-cutting seams of the assembled document (G1 evidence): every
    `work release` verb/field/receipt the taskdef cites exists verbatim in
    the kernel surface (no field invented in one place and read in
    another), the single-flight claim is consumed through `work release
    claim` and its exit code (never check-then-create), every hard stop
    names a human decision, the parameterization surface is complete for a
    second adopter with nothing adopter-specific hardcoded outside the
    documented ctl bootstrap pin, and a relaunch at any point of leg 2
    derives exactly one kernel step the document names

Rendering conventions mirror tests/test_taskdef_render_matrix.py: builtin
tier pinned to this checkout, LMER_* env stripped per test.
"""
import itertools
import re
from pathlib import Path

import pytest
import yaml

from hooks.start import render_taskdef_template
from tests.conftest import strip_lmer_env
from work_repo import release_run

REPO_TASKDEF = Path(__file__).parent.parent / "taskdef"
RELEASE_DIR = REPO_TASKDEF / "release"
INSTRUCTIONS = RELEASE_DIR / "instructions.txt"

WORK_MODES = ("finish", "phasic")
# provider name -> LMER_REPO_HOST value that selects it
PROVIDERS = {"gitlab": "git.example.com", "github": "github.com"}

# Shared partials the spine includes; their bodies are authored in parallel,
# so only existence and include wiring are asserted — never their content.
LEG_PARTIALS = (
    "release-leg1.jinja2",
    "release-gate.jinja2",
    "release-leg2.jinja2",
    "release-resume.jinja2",
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


def _assert_fully_rendered(out):
    assert out.strip()
    assert "{%" not in out and "{{" not in out


def _squash(text):
    """Collapse whitespace so prose assertions survive line wrapping."""
    return " ".join(text.split())


class TestReleaseSpine:
    """The rendered document skeleton the release taskdef owns."""

    @pytest.mark.parametrize("provider", sorted(PROVIDERS))
    @pytest.mark.parametrize("work_mode", WORK_MODES)
    def test_renders_builtin_only_with_zero_config(
        self, work_mode, provider, monkeypatch, capsys
    ):
        """Like chat, release must render out of the box: no work repo, no
        external paths — the built-in root resolves as schema 1."""
        monkeypatch.setenv("LMER_REPO_HOST", PROVIDERS[provider])
        out = _render(work_mode)
        _assert_fully_rendered(out)
        assert out.startswith("# Release")
        banner = capsys.readouterr().out
        assert f"taskdef source: {REPO_TASKDEF} (schema 1)" in banner

    def test_spine_sections_render_in_order(self):
        out = _render()
        spine = [
            "## The release spine",
            "## Phase 0.25: Single-flight claim",
            "## Phase 0.5: Resolve the release parameters",
            "## Leg 1 — prep (GitLab side)",
            "## Gate — human merge",
            "## Leg 2 — ship",
            "## Resuming an in-progress release",
            "## Closing out the run",
            "## HARD RULES",
        ]
        positions = [out.index(heading) for heading in spine]
        assert positions == sorted(positions)

    def test_spine_states_the_two_legs_and_the_gate(self):
        out = _squash(_render())
        # Leg 1: bump + changelog MR against prep-release on GitLab.
        assert "prep-release" in out
        # Gate: the human merge IS the approval.
        assert "release approval" in out
        # Leg 2 ordering: GitHub main, GitHub tag, Actions green, GitLab tag.
        assert "push GitHub `main` first, then the tag" in out
        assert "only after GitHub is green" in out
        # Resume contract: relaunch derives the leg from run state.
        assert "re-derives the current leg from run state" in out

    def test_leg_partials_exist_and_are_included_once_each_in_order(self):
        """The skeleton wires the four shared partials; their bodies land in
        parallel, so assert the include directives in the raw template (the
        spine's own content), never the partial content."""
        source = INSTRUCTIONS.read_text()
        positions = []
        for partial in LEG_PARTIALS:
            assert (REPO_TASKDEF / partial).exists(), partial
            directive = "{% include '" + partial + "' %}"
            assert source.count(directive) == 1, partial
            positions.append(source.index(directive))
        assert positions == sorted(positions)

    def test_run_state_partial_included(self):
        source = INSTRUCTIONS.read_text()
        assert "{% include 'run-state.jinja2' ignore missing %}" in source

    def test_missing_repo_url_is_a_documented_stop(self):
        """No project checkout → the document says stop, not improvise."""
        out = _squash(_render())
        assert "No project repository is checked out" in out
        assert "Do not improvise a checkout" in out

    def test_repo_url_renders_when_set(self, monkeypatch):
        monkeypatch.setenv(
            "LMER_REPO_URL", "https://git.example.com/agents/global"
        )
        out = _render()
        assert (
            "The project under release is "
            "`https://git.example.com/agents/global`." in out
        )
        assert "No project repository is checked out" not in out


class TestReleaseParameterization:
    """Per-repo parameters come from `task.yaml` via tier precedence; the
    built-in manifest carries no `release:` mapping and a gap is a hard
    stop, never a default."""

    def test_builtin_tier_manifest_carries_no_release_mapping(self):
        """The built-in manifest exists (needs declarations only — see
        tests/test_release_taskdef_manifest.py) but the deliberate absence
        of a `release:` mapping IS the no-defaults property: the parameter
        contract keys on the first tier whose task.yaml CONTAINS a
        `release:` mapping, so an un-onboarded repository still has nothing
        to fall back to."""
        manifest = RELEASE_DIR / "task.yaml"
        assert manifest.exists()
        data = yaml.safe_load(manifest.read_text())
        assert isinstance(data, dict)
        assert "release" not in data

    def test_names_all_four_parameters(self):
        out = _render()
        for key in ("github_target", "tag_prefix", "signing_key", "changelog"):
            assert key in out, key

    def test_documents_manifest_resolution_precedence(self):
        out = _render()
        assert "task.yaml" in out
        assert "first match wins" in out
        assert (
            "<work repo>/<host>/<project>/taskdef/release/task.yaml" in out
        )
        assert "deliberately ships NO `release:` mapping" in out
        # One file wins whole — no cross-tier merging ambiguity.
        assert "Do not merge values across tiers" in out

    def test_changelog_mechanism_is_a_closed_enum(self):
        out = _render()
        assert 'exactly "fragments" or "legacy"' in out
        assert "anything other than exactly `fragments` or `legacy`" in out

    def test_missing_parameter_is_a_recorded_hard_stop(self):
        out = _render()
        assert "HARD STOP" in out
        assert "--stop-reason=critical_error" in out
        assert "release parameters missing" in out

    def test_forbids_defaulting_and_inference(self):
        out = _squash(_render())
        assert "NEVER default a parameter" in out
        assert "do not derive `github_target` from git remotes" in out
        assert "do not guess `tag_prefix` from existing tags" in out
        assert "do not infer the changelog mechanism" in out
        # Env vars and project-info prose do not substitute for the manifest.
        assert "Environment variables do not override the manifest" in out
        assert "does NOT substitute for the four release parameters" in out

    def test_signing_key_is_a_reference_never_material(self):
        out = _render()
        assert "NEVER key material" in out
        assert "signing key REFERENCE only" in out


class TestReleaseHardRules:
    """The HARD RULES block instructions.txt itself owns."""

    def _hard_rules(self):
        out = _render()
        return out[out.index("## HARD RULES") :]

    def test_never_force_push(self):
        rules = self._hard_rules()
        assert "NEVER force-push any ref on any remote" in rules
        assert "never an automatic force-push" in rules

    def test_never_delete_or_repoint_a_tag(self):
        rules = self._hard_rules()
        assert "NEVER delete or re-point an existing tag" in rules

    def test_never_resign_a_tag(self):
        assert "NEVER re-sign an existing tag" in self._hard_rules()

    def test_no_fallback_push_path(self):
        rules = self._hard_rules()
        assert "NO fallback push path" in rules
        assert "naming the credential requirement" in rules

    def test_no_taskdef_side_lock(self):
        """The single-flight lock is the runstate subsystem's atomic `work`
        claim — the taskdef consumes it, never re-implements it."""
        rules = _squash(self._hard_rules())
        assert "NO taskdef-side lock" in rules
        assert "atomic run claim" in rules
        assert "never check-then-create" in rules
        # And the consuming side, up in the spine:
        out = _squash(_render())
        assert "Do NOT implement any lock of your own" in out
        assert "this taskdef only consumes its verdict" in out

    def test_tag_name_from_pyproject_at_tagged_commit(self):
        rules = _squash(self._hard_rules())
        assert "derived from `pyproject.toml` at the tagged commit" in rules
        assert "never from an earlier leg's memory" in rules

    def test_push_ordering_is_fixed(self):
        assert "Push ordering is fixed" in self._hard_rules()

    def test_no_self_reference_commit_tags(self):
        assert "Co-Authored-By" in self._hard_rules()

    def test_every_hard_rule_line_is_marked(self):
        """Each bullet in the HARD RULES block carries the HARD RULE tag —
        the block stays greppable as rules are added."""
        rules = self._hard_rules()
        bullets = re.findall(r"^- (.+)$", rules, flags=re.MULTILINE)
        assert bullets
        for bullet in bullets:
            assert bullet.startswith("HARD RULE:"), bullet


class TestCrossCuttingRecordSeam:
    """The leg-1 → leg-2 → resume seam: every `work release` verb, field,
    and receipt the assembled document cites exists verbatim in the kernel
    surface (src/work_repo/release_run.py + the frozen CLI verb table) —
    no field is written in one place and read under another name."""

    def test_cited_subverbs_are_the_frozen_verb_family(self):
        out = _render()
        subverbs = set(re.findall(r"work release ([a-z][a-z-]*)", out))
        assert subverbs == {"claim", "unclaim", "status", "record", "abort"}

    def test_cited_record_fields_match_the_kernel(self):
        out = _render()
        fields = set(re.findall(r"work release record ([a-z-]+)", out))
        assert fields == {"version", "bump-sha", "merge-sha", "tag", "receipt"}

    def test_cited_receipts_are_exactly_the_kernel_receipt_names(self):
        """Every receipt the taskdef records exists in RECEIPT_NAMES, and
        every kernel receipt has a recording site — the resume derivation
        consumes exactly what leg 2 writes."""
        out = _render()
        cited = set(re.findall(r"work release record receipt ([a-z-]+)", out))
        assert cited == set(release_run.RECEIPT_NAMES)

    def test_url_required_receipts_carry_url_in_the_document(self):
        """actions-run and pypi require --url in the kernel; the document
        must cite them with it, never as bare checkmarks."""
        out = _squash(_render())
        for name in release_run.URL_REQUIRED_RECEIPTS:
            assert f"work release record receipt {name} --url" in out, name

    def test_leg2_step_names_are_kernel_steps_or_the_documented_bridge(self):
        """Every `leg2-*` name the document cites is either a frozen
        derived step or one of the two bridge phase names, and the bridge
        is declared: neither ever appears as a derived next step."""
        out = _render()
        cited = set(re.findall(r"`(leg2-[a-z-]+)`", out))
        kernel = {s for s in release_run.STEPS if s.startswith("leg2")}
        bridge = {"leg2-record-merge", "leg2-preconditions"}
        assert kernel <= cited
        assert cited - kernel == bridge
        squashed = _squash(out)
        assert "never appear as a derived next step" in squashed
        assert "folds them into" in squashed


class TestCrossCuttingClaimConsumption:
    """Single-flight is only ever consumed: the claim verb and its exit
    code, in both the launch path and the resume path — no check-then-create
    language anywhere outside a prohibition."""

    def test_claim_verb_is_the_launch_gate(self):
        out = _squash(_render())
        assert "run `work release claim`" in out
        assert "A non-zero exit means REFUSE to proceed" in out
        assert "Never proceed unlocked" in out

    def test_resume_path_retakes_the_same_claim(self):
        out = _squash(_render())
        assert "Re-entry holds the same single-flight lock" in out
        assert "idempotent refresh or a loud takeover of a stale claim" in out
        assert "resumes this SAME run, never a second release" in out

    def test_check_then_create_appears_only_as_prohibition(self):
        out = _render()
        hits = [m.start() for m in re.finditer(r"check-then-create", out)]
        assert hits  # the prohibition itself must be stated
        for pos in hits:
            context = out[max(0, pos - 300) : pos + 300].lower()
            assert "never" in context or "loses" in context, out[pos : pos + 80]

    def test_no_lock_artifact_commands(self):
        """No rendered command builds a lock: no flock, no O_EXCL-style
        marker files, no lock refs."""
        out = _render()
        for needle in ("flock", "mkdir /tmp/release", "refs/locks"):
            assert needle not in out, needle


class TestCrossCuttingHumanDecisions:
    """Every hard stop names a human decision — never an automatic
    remediation."""

    HUMAN_PHRASES = (
        # Phase 0.5: missing/ambiguous parameter → owner onboards.
        "the repository owner's decision",
        # Leg 1: pinned-ref install/dry-run failures → human moves the pin.
        "The pin is human-owned",
        "a human revalidates and moves the pin",
        # Leg 2 step 1: version mismatch at the merge SHA.
        "a human decision is required, not a repair attempt",
        # Leg 2 step 2: GitHub main divergence.
        "The remediation path is a human one",
        # Leg 2 step 3: tag pointing elsewhere → burned-version runbook.
        "the human's repair path",
        # Leg 2 step 5: Actions still red after the one re-dispatch.
        "the human's call",
        # Gate: the human merge IS the approval.
        "IS the release approval",
    )

    def test_every_hard_stop_names_a_human_decision(self):
        out = _squash(_render())
        for phrase in self.HUMAN_PHRASES:
            assert phrase in out, phrase

    def test_the_only_automatic_remediation_is_the_actions_redispatch(self):
        out = _squash(_render())
        assert "the only automatic remediation this leg attempts" in out
        assert "never an automatic force-push" in out


class TestCrossCuttingParameterization:
    """The surface a second adopter needs is complete, consumed where the
    spec says, and nothing adopter-specific is hardcoded outside the
    documented ctl bootstrap pin."""

    PARAMS = ("github_target", "tag_prefix", "signing_key", "changelog")

    def test_each_parameter_has_a_consumption_site(self):
        """Beyond being named in the manifest shape (Phase 0.5), each
        parameter is consumed somewhere as `release.<key>`."""
        out = _render()
        for param in self.PARAMS:
            assert f"release.{param}" in out, param

    def test_github_target_is_the_only_mirror_url_source(self):
        out = _squash(_render())
        assert "git remote add github <release.github_target>" in out

    def test_no_adopter_url_outside_the_ctl_bootstrap_pin(self):
        """The pinned ctl install (spec §6 bootstrap: git-pinned until ctl
        ships) is the ONE sanctioned concrete URL; nothing else in the
        rendered document names a host or repo an adopter would change."""
        out = _render()
        start = out.index("### 2. Install ctl at the pinned ref")
        end = out.index("### 3.", start)
        remainder = out[:start] + out[end:]
        for needle in ("git.20c.com", "github.com/", "lmer2"):
            assert needle not in remainder, needle

    def test_tag_name_derives_from_the_tag_prefix_parameter(self):
        out = _squash(_render())
        assert (
            "The tag name is `release.tag_prefix` + the version re-read in "
            "step 1 at the merge SHA" in out
        )
        # ...but the prefix is pinned: leg 2 must not oversell arbitrary
        # prefixes the kernel would refuse (see the cross-check below).
        assert 'Phase 0.5 pins `tag_prefix` to exactly `"v"`' in out


class TestTagPrefixKernelCrossCheck:
    """CROSS-CHECK: binds the Phase 0.5 tag_prefix pin to the kernel's
    record_tag contract so neither side can drift alone. The kernel
    (src/work_repo/release_run.py) hard-codes tag names as v<version>;
    Phase 0.5 therefore hard-stops on any `tag_prefix` other than exactly
    "v". If the kernel ever learns another prefix (a reviewed kernel
    change), this test MUST be updated together with the Phase 0.5
    resolution rule and leg 2's step-3 wording — that forced simultaneous
    update is this test's whole purpose."""

    def test_instructions_pin_tag_prefix_to_v_as_a_hard_stop(self):
        out = _squash(_render())
        assert (
            "`tag_prefix` set to anything other than exactly `v` → HARD "
            "STOP naming the offending value, same recording contract" in out
        )
        assert "The release kernel derives tag names as `v<version>`" in out
        assert (
            "a reviewed kernel change, not a session decision" in out
        )

    def test_kernel_accepts_exactly_v_version_and_nothing_else(self, tmp_path):
        """The real kernel functions, not a rendering assertion: record_tag
        accepts exactly f"v{version}" — the contract the Phase 0.5 pin is
        honest about. A non-"v" prefix would sign a tag the recorder then
        refuses, wedging the release."""
        release_run.record_version(tmp_path, "0.5.0")
        release_run.record_bump_merge(tmp_path, "a" * 40)
        release_run.record_release_merge(
            tmp_path, "b" * 40, version_at_sha="0.5.0"
        )
        # Any other prefix is refused by the kernel...
        with pytest.raises(release_run.ReleaseRunError, match="v<version>"):
            release_run.record_tag(tmp_path, "rel-0.5.0", "b" * 40)
        # ...and exactly v<version> is accepted.
        recorded = release_run.record_tag(tmp_path, "v0.5.0", "b" * 40)
        assert recorded["tag"]["name"] == "v0.5.0"


class TestCrossCuttingLeg2Idempotency:
    """A relaunch at ANY point of leg 2 lands on exactly one kernel-derived
    step, and the assembled document names that step verbatim — the ladder
    tolerates re-entry everywhere (G1 evidence)."""

    @staticmethod
    def _leg2_state(receipts=(), tag=True):
        release = release_run.seed_release()
        release["version"] = "0.5.0"
        release["bump_mr_merge_sha"] = "a" * 40
        release["release_mr_merge_sha"] = "b" * 40
        if tag:
            release["tag"] = {"name": "v0.5.0", "sha": "b" * 40}
        release["receipts"] = {name: {"recorded": "t"} for name in receipts}
        return release

    def test_every_leg2_relaunch_point_derives_one_documented_step(self):
        """Merge recorded but no tag, plus all 2^5 receipt subsets: every
        state derives to a single step, and the document carries that step
        by its frozen name."""
        out = _render()
        states = [self._leg2_state(tag=False)]
        for bits in itertools.product((False, True), repeat=5):
            recorded = tuple(
                name
                for name, present in zip(release_run.RECEIPT_NAMES, bits)
                if present
            )
            states.append(self._leg2_state(receipts=recorded))
        for release in states:
            derived = release_run.derive_leg(release)
            assert derived["leg"] in ("leg2", "complete")
            step = derived["next_step"]
            assert step in release_run.STEPS
            assert f"`{step}`" in out, step

    def test_document_states_reentry_tolerance(self):
        out = _squash(_render())
        assert "Leg 2 may be entered any number of times" in out
        assert (
            "every step compares its target to the recorded merge SHA "
            "before acting" in out
        )
        # Re-entry never re-does identity work: skips, re-records, and the
        # one sanctioned re-dispatch.
        assert "the mismatch check runs every time" in out
        assert "never re-point, never re-sign" in out
