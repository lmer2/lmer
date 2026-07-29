"""Tests for the leg-1 (prep) body of the `release` taskdef.

`taskdef/release-leg1.jinja2` is included by the release spine under its
"## Leg 1 — prep (GitLab side)" heading. This module asserts the contract
the leg-1 body owns:

  - the ctl install comes from its canonical git.20c.com repository at a
    PINNED ref (a full commit SHA, never a branch or HEAD)
  - the dry run validates the pinned ref against the repo's changelog mode
    BEFORE anything is committed (dry-run precedes `gate-commit`)
  - both changelog mechanisms are covered: the legacy `CHANGELOG.yaml`
    roll and `changelog.d/` fragments
  - the bump MR targets `prep-release`, never `main`, and the standing
    `prep-release` -> `main` MR becomes the release MR once the bump lands
  - leg-1 state (version, bump-MR merge SHA) is recorded through the
    `work` CLI's release record verbs — the runstate subsystem is the
    single writer

Rendering conventions mirror tests/test_release_taskdef.py: builtin tier
pinned to this checkout, LMER_* env stripped per test.
"""
import re
from pathlib import Path

import pytest

from hooks.start import render_taskdef_template
from tests.conftest import strip_lmer_env

REPO_TASKDEF = Path(__file__).parent.parent / "taskdef"
INSTRUCTIONS = REPO_TASKDEF / "release" / "instructions.txt"
LEG1_PARTIAL = REPO_TASKDEF / "release-leg1.jinja2"

LEG1_HEADING = "## Leg 1 — prep (GitLab side)"
NEXT_HEADING = "## Gate — human merge"


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


def _leg1(out=None):
    """The rendered leg-1 section: spine heading up to the gate heading."""
    out = out if out is not None else _render()
    return out[out.index(LEG1_HEADING) : out.index(NEXT_HEADING)]


def _squash(text):
    """Collapse whitespace so prose assertions survive line wrapping."""
    return " ".join(text.split())


class TestLeg1Rendering:
    """The partial renders inside the spine and stays below H2."""

    def test_renders_fully_inside_the_spine(self):
        leg1 = _leg1()
        assert "{%" not in leg1 and "{{" not in leg1
        # The placeholder comment is gone — a real body rendered.
        assert len(leg1.strip()) > len(LEG1_HEADING)

    def test_partial_owns_no_h2_headings(self):
        """The spine owns the H2; the body starts at H3 or plain prose, so
        the document outline stays the spine's."""
        source = LEG1_PARTIAL.read_text()
        for line in source.splitlines():
            assert not re.match(r"^##\s", line), line
        # And the rendered section contains exactly one H2: the spine's own.
        h2s = re.findall(r"^## .+$", _leg1(), flags=re.MULTILINE)
        assert h2s == [LEG1_HEADING]

    def test_phase_recorded(self):
        assert "work state set --phase=leg1-prep" in _leg1()


class TestPinnedRefInstall:
    """ctl installs from git.20c.com at a pinned commit — never floating."""

    def test_install_uses_the_pinned_ref(self):
        leg1 = _leg1()
        assert (
            'uv tool install "git+https://git.20c.com/20c/ctl'
            "@${CTL_PINNED_REF}\"" in leg1
        )

    def test_pin_is_a_full_commit_sha(self):
        """A branch name or short ref is not a pin."""
        match = re.search(r"^CTL_PINNED_REF=(\S+)$", _leg1(), re.MULTILINE)
        assert match, "CTL_PINNED_REF assignment missing"
        assert re.fullmatch(r"[0-9a-f]{40}", match.group(1)), match.group(1)

    def test_pin_changes_are_taskdef_edits_not_session_decisions(self):
        leg1 = _squash(_leg1())
        assert "reviewed edit to this taskdef" in leg1
        assert "never a session-time decision" in leg1

    def test_no_fallback_install_path(self):
        leg1 = _squash(_leg1())
        assert "never a branch, never HEAD" in leg1
        assert (
            "Do NOT fall back to PyPI, a floating branch, a different ref"
            in leg1
        )


class TestDryRunBeforeCommit:
    """The pinned ref is validated against the repo's changelog mode before
    anything is committed."""

    def test_dry_run_precedes_gate_commit(self):
        leg1 = _leg1()
        assert leg1.index("dry-run mode FIRST") < leg1.index("`gate-commit`")

    def test_install_precedes_dry_run_precedes_real_run(self):
        leg1 = _leg1()
        order = [
            "### 2. Install ctl at the pinned ref",
            "### 3. Dry-run before committing anything",
            "### 4. Run the bump + changelog roll",
            "### 5. Commit, then record the version — BEFORE anything is "
            "pushed",
            "### 6. Push the branch",
            "### 7. Open the bump MR targeting `prep-release` — then log "
            "its URL",
        ]
        positions = [leg1.index(step) for step in order]
        assert positions == sorted(positions)

    def test_dry_run_validates_the_changelog_mode(self):
        leg1 = _squash(_leg1())
        assert "no writes, no commits" in leg1
        # Wrong-mechanism or failing dry run → hard stop, pin is human-owned.
        assert (
            "any mechanism other than the configured one" in leg1
        )
        assert "the pinned ref does not handle this repository" in leg1
        assert "HARD STOP" in leg1
        assert "Do NOT bump the pin yourself" in leg1

    def test_dry_run_detects_an_already_landed_bump(self):
        """Spec §7: an aborted release leaves the bump on prep-release; the
        next run's dry run detects it and skips the bump."""
        leg1 = _squash(_leg1())
        assert "version already bumped" in leg1
        assert "changelog already rolled" in leg1
        assert "do NOT bump again" in leg1

    def test_commit_goes_through_the_gate(self):
        leg1 = _squash(_leg1())
        assert "`gate-commit`" in leg1
        assert "never raw `git commit`" in leg1


class TestChangelogModes:
    """Both mechanisms are spelled out; the resolved parameter selects."""

    def test_legacy_mode_is_the_changelog_yaml_roll(self):
        leg1 = _squash(_leg1())
        assert "`legacy` — ctl rolls `CHANGELOG.yaml`" in leg1
        assert "lmer's first release under this flow runs in this mode" in leg1

    def test_fragments_mode_is_changelog_d(self):
        leg1 = _squash(_leg1())
        assert "`fragments`" in leg1
        assert "`changelog.d/` fragment files" in leg1

    def test_mode_comes_from_the_resolved_parameter(self):
        """The mechanism is the Phase 0.5 `release.changelog` value — the
        leg consumes it, never re-infers it from the tree."""
        assert "`release.changelog` parameter" in _squash(_leg1())


class TestBumpMRTarget:
    """The bump MR targets prep-release; the release MR is the standing
    prep-release -> main MR once the bump lands."""

    def test_branches_off_prep_release_never_main(self):
        leg1 = _squash(_leg1())
        assert "git switch -c release-bump origin/prep-release" in leg1
        assert "never off `main`" in leg1

    def test_mr_targets_prep_release_never_main(self):
        leg1 = _squash(_leg1())
        assert "targeting `prep-release` — NEVER targeting `main`" in leg1

    def test_standing_mr_becomes_the_release_mr(self):
        leg1 = _squash(_leg1())
        assert (
            "the standing `prep-release` → `main` MR IS the release MR"
            in leg1
        )


class TestRunStateRecording:
    """Leg-1 state goes through the frozen `work release record` verbs —
    the runstate subsystem is the single writer."""

    def test_records_version_and_bump_sha_via_work_cli(self):
        leg1 = _leg1()
        assert "work release record version <X.Y.Z>" in leg1
        assert "work release record bump-sha <sha>" in leg1

    def test_version_is_recorded_without_the_tag_prefix(self):
        leg1 = _squash(_leg1())
        assert "NO `v` prefix" in leg1

    def test_branch_and_mr_refs_are_logged_for_relaunch(self):
        leg1 = _squash(_leg1())
        assert "`work log` the bump branch name" in leg1
        assert "`work log` the bump-MR URL" in leg1

    def test_version_is_recorded_before_the_push(self):
        """The crash-window fix: a session dying between push and record
        would leave no version → resume re-runs the bump → non-fast-forward
        push against the existing remote branch. Recording first closes the
        window."""
        leg1 = _leg1()
        record = leg1.index("work release record version <X.Y.Z>")
        push = leg1.index("git push -u origin prep-<X.Y.Z>")
        assert record < push
        squashed = _squash(leg1)
        assert "before the branch leaves this machine" in squashed
        assert "never rolls a second bump" in squashed

    def test_existing_remote_branch_or_mr_is_adopted_never_recreated(self):
        """The resume-hardening half: a remote prep-<X.Y.Z> branch or open
        bump MR from a session that died between push and record is adopted
        — never re-created, never force-pushed."""
        leg1 = _squash(_leg1())
        assert "Adoption, never re-creation" in leg1
        assert "git ls-remote origin refs/heads/prep-<X.Y.Z>" in leg1
        assert "ADOPT it" in leg1
        assert "verify it carries exactly the bump" in leg1
        assert (
            "NEVER re-create the branch and NEVER force-push over it" in leg1
        )

    def test_no_second_writer(self):
        leg1 = _squash(_leg1())
        assert "never edit state files by hand" in leg1
        assert "never invent a parallel record of your own" in leg1

    def test_relaunch_keys_on_recorded_fields_not_memory(self):
        leg1 = _squash(_leg1())
        assert "from these two recorded fields alone" in leg1
        assert "never from an earlier session's memory" in leg1


class TestRunTakesItsVersionBearingAddress:
    """Recording the version is also where the RUN gets an address of its
    own, which is what makes the next release of the repository a new run
    rather than a permanent claim refusal."""

    def test_recording_the_version_moves_the_run_dir(self):
        leg1 = _squash(_leg1())
        assert "moves the run dir to `release-<repo>-v<X.Y.Z>`" in leg1
        assert "frees the seed address for the next release" in leg1

    def test_sessions_are_told_to_cite_the_new_path(self):
        """A URL to the seed address 404s after the move — the taskdef says
        so rather than leaving dead links in reports and MR comments."""
        leg1 = _squash(_leg1())
        assert "Cite that path" in leg1
        assert "404s" in leg1

    def test_the_move_is_not_presented_as_a_resume_hazard(self):
        """Resolution is by content, so nothing else about the run changes
        — an agent must not "helpfully" re-seed or re-point anything."""
        leg1 = _squash(_leg1())
        assert "the run resolves by content" in leg1
        assert "a relaunch still find it" in leg1
