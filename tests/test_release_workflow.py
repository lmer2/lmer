"""Publish-gate invariants for .github/workflows/release.yml.

The release workflow's security posture rests on a handful of structural
facts: verify-tag-signature runs first and everything else hangs off it,
the trust anchor comes from an admin-controlled Actions variable (never
repo content — a tag push runs the workflow from the tag's own tree), the
main-HEAD comparison is sourced from the GitHub API, and the PyPI publish
action is SHA-pinned at or above the attestations-by-default floor with
skip-existing for idempotent re-runs. These tests assert those invariants
directly against the parsed workflow so a future edit that weakens the
gate fails CI instead of shipping.
"""

import os
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
GATE_SCRIPT = REPO_ROOT / ".github" / "scripts" / "verify-tag-signature.sh"
GATE_SCRIPT_REL = ".github/scripts/verify-tag-signature.sh"
REUSE_GATE_SCRIPT = REPO_ROOT / ".github" / "scripts" / "gate-version-reuse.py"

GATE_JOB = "verify-tag-signature"
PUBLISH_ACTION = "pypa/gh-action-pypi-publish"
SIGNERS_VAR_EXPR = "${{ vars.RELEASE_ALLOWED_SIGNERS }}"

# v1.12.0 is where gh-action-pypi-publish started emitting PEP 740
# attestations by default; the pin must never fall below it.
ATTESTATIONS_FLOOR = (1, 12, 0)


@pytest.fixture(scope="module")
def workflow_text():
    return WORKFLOW.read_text()


@pytest.fixture(scope="module")
def workflow(workflow_text):
    """The workflow parsed once for the whole module."""
    return yaml.safe_load(workflow_text)


@pytest.fixture(scope="module")
def jobs(workflow):
    return workflow["jobs"]


@pytest.fixture(scope="module")
def publish_step(jobs):
    """The pypa/gh-action-pypi-publish step of the publish-pypi job."""
    steps = [
        step
        for step in jobs["publish-pypi"].get("steps", [])
        if str(step.get("uses", "")).startswith(f"{PUBLISH_ACTION}@")
    ]
    assert len(steps) == 1, (
        f"expected exactly one {PUBLISH_ACTION} step in publish-pypi, "
        f"found {len(steps)}"
    )
    return steps[0]


def _needs(job):
    """A job's needs as a list (GitHub accepts a bare string or a list)."""
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return [needs]
    return list(needs)


def _steps(jobs):
    """All (job_name, step) pairs; reusable-workflow jobs have no steps."""
    for name, job in jobs.items():
        for step in job.get("steps", []):
            yield name, step


class TestGateJobOrdering:
    def test_gate_is_the_first_job(self, jobs):
        assert list(jobs)[0] == GATE_JOB

    def test_gate_has_no_needs(self, jobs):
        assert "needs" not in jobs[GATE_JOB]

    def test_verify_version_exists_and_needs_gate(self, jobs):
        assert "verify-version" in jobs
        assert _needs(jobs["verify-version"]) == [GATE_JOB]

    def test_every_other_job_transitively_needs_gate(self, jobs):
        """Walk each job's needs graph; all roads must lead to the gate."""
        for name, job in jobs.items():
            if name == GATE_JOB:
                continue
            seen = set()
            frontier = _needs(job)
            assert frontier, f"job '{name}' has no needs; it can run before the gate"
            while frontier:
                dep = frontier.pop()
                if dep in seen:
                    continue
                seen.add(dep)
                assert dep in jobs, f"job '{name}' needs unknown job '{dep}'"
                frontier.extend(_needs(jobs[dep]))
            assert GATE_JOB in seen, (
                f"job '{name}' does not transitively depend on '{GATE_JOB}'"
            )


class TestSignerMaterialProvenance:
    def test_gate_env_sources_signers_from_actions_variable(self, jobs):
        gate_steps = jobs[GATE_JOB]["steps"]
        sourced = [
            step
            for step in gate_steps
            if step.get("env", {}).get("RELEASE_ALLOWED_SIGNERS") == SIGNERS_VAR_EXPR
        ]
        assert sourced, (
            "gate job never sets RELEASE_ALLOWED_SIGNERS from "
            "vars.RELEASE_ALLOWED_SIGNERS"
        )

    def test_every_signers_env_assignment_uses_the_variable(self, jobs):
        """Anywhere the workflow injects signer material, it must be vars."""
        for name, step in _steps(jobs):
            env = step.get("env", {})
            if "RELEASE_ALLOWED_SIGNERS" in env:
                assert env["RELEASE_ALLOWED_SIGNERS"] == SIGNERS_VAR_EXPR, (
                    f"job '{name}' sets RELEASE_ALLOWED_SIGNERS from "
                    f"{env['RELEASE_ALLOWED_SIGNERS']!r}, not the admin-controlled "
                    "Actions variable"
                )

    def test_no_repo_content_path_for_signer_material(self, workflow_text):
        """No checked-in signers file may feed verification.

        A tag push runs the workflow from the tag's own tree, so any
        allowed-signers material read from the checkout is attacker-writable.
        """
        assert "allowed_signers" not in workflow_text
        assert "allowedSignersFile" not in workflow_text
        assert "secrets.RELEASE_ALLOWED_SIGNERS" not in workflow_text
        for line in workflow_text.splitlines():
            if "RELEASE_ALLOWED_SIGNERS" in line:
                assert "vars.RELEASE_ALLOWED_SIGNERS" in line, (
                    "RELEASE_ALLOWED_SIGNERS referenced outside the vars "
                    f"context: {line.strip()!r}"
                )


class TestGateScript:
    def test_gate_calls_the_script(self, jobs):
        runs = [
            str(step.get("run", "")).strip()
            for step in jobs[GATE_JOB]["steps"]
            if "run" in step
        ]
        assert GATE_SCRIPT_REL in runs

    def test_script_exists_and_is_executable(self):
        assert GATE_SCRIPT.is_file()
        assert os.access(GATE_SCRIPT, os.X_OK)

    def test_main_head_comes_from_the_github_api(self):
        """The tag-at-main-head check must not trust a checked-out ref."""
        script = GATE_SCRIPT.read_text()
        assert "/commits/heads/main" in script
        assert "GITHUB_API_URL" in script
        assert "origin/main" not in script
        assert "refs/heads/main" not in script

    def test_workflow_never_compares_against_checked_out_main(self, workflow_text):
        assert "origin/main" not in workflow_text
        assert "refs/heads/main" not in workflow_text


class TestGateCheckoutProvenance:
    """The verification code must never come from the pushed tag's tree —
    whoever can push a tag would then supply the code that verifies it."""

    def _checkouts(self, jobs):
        return [
            step
            for step in jobs[GATE_JOB]["steps"]
            if str(step.get("uses", "")).split("@")[0] == "actions/checkout"
        ]

    def test_gate_checkout_is_pinned_to_main(self, jobs):
        checkouts = self._checkouts(jobs)
        assert checkouts, "gate job has no checkout step"
        for step in checkouts:
            assert step.get("with", {}).get("ref") == "main", (
                "gate checkout must pin ref: main, never the pushed tag"
            )

    def test_gate_checkout_never_uses_github_ref(self, jobs):
        for step in self._checkouts(jobs):
            assert "github.ref" not in str(step.get("with", {}).get("ref", ""))

    def test_gate_checkout_fetches_tags_for_verification(self, jobs):
        """Pinning to main must not lose the tag object under test."""
        for step in self._checkouts(jobs):
            with_block = step.get("with", {})
            assert with_block.get("fetch-tags") is True
            assert with_block.get("fetch-depth") == 0


class TestVersionReuseGate:
    """skip-existing converges re-entry silently; the reuse gate makes reuse
    an explicit, admin-authorized act (fail closed unless
    vars.RELEASE_RESUME_VERSION names the version) and divergence RED
    (foreign artifacts under the version fail before publish).

    The gate's BEHAVIOR is tested where it lives — the script itself, in
    tests/test_release_gate_version_reuse.py. What belongs here is the
    wiring: that the workflow invokes that script, hands it its seams, and
    fetches it from `main` rather than from the pushed tag."""

    def _steps(self, jobs):
        return jobs["publish-pypi"].get("steps", [])

    def _gate_index(self, jobs):
        for i, step in enumerate(self._steps(jobs)):
            if "PYPI_PROJECT_URL" in (step.get("env") or {}):
                return i
        return None

    def test_reuse_gate_exists_before_the_publish_step(self, jobs):
        steps = self._steps(jobs)
        gate_idx = self._gate_index(jobs)
        assert gate_idx is not None, "publish-pypi has no version-reuse gate"
        publish_idx = next(
            i for i, step in enumerate(steps)
            if str(step.get("uses", "")).startswith(f"{PUBLISH_ACTION}@")
        )
        assert gate_idx < publish_idx

    def test_reuse_gate_url_is_the_production_project_url(self, jobs):
        """The exact https://pypi.org/project/<name>/ shape is a transform
        contract with Ctl/rehearsal/derive-workflow.py."""
        step = self._steps(jobs)[self._gate_index(jobs)]
        assert re.fullmatch(
            r"https://pypi\.org/project/[^/]+/",
            step["env"]["PYPI_PROJECT_URL"],
        )

    def test_reuse_gate_runs_the_repo_script(self, jobs):
        """The gate is a script file with env seams, not a heredoc: a
        heredoc is unreachable by pytest, ruff and pre-commit, so its first
        execution would be a real release's publish job."""
        step = self._steps(jobs)[self._gate_index(jobs)]
        assert REUSE_GATE_SCRIPT.name in step["run"]
        assert ".github/scripts/" in step["run"]
        assert REUSE_GATE_SCRIPT.is_file(), f"{REUSE_GATE_SCRIPT} is missing"
        assert REUSE_GATE_SCRIPT.stat().st_mode & 0o111, (
            "gate script is not executable"
        )

    def test_reuse_gate_takes_the_resume_switch_from_an_actions_variable(self, jobs):
        """The one way to publish over an existing version is an
        admin-controlled Actions variable — same trust model as
        RELEASE_ALLOWED_SIGNERS: never repo content, never a secret."""
        step = self._steps(jobs)[self._gate_index(jobs)]
        value = step["env"]["RELEASE_RESUME_VERSION"]
        assert re.fullmatch(r"\$\{\{\s*vars\.RELEASE_RESUME_VERSION\s*\}\}", value)

    def test_publish_job_checks_out_main_before_downloading_dist(self, jobs):
        """The publish job holds id-token: write, so its code must come from
        `main`, never from the pushed tag (the verify job's rule). The
        checkout also has to precede the artifact download — checkout cleans
        the workspace it lands in, which would delete dist/."""
        steps = self._steps(jobs)
        checkouts = [
            i for i, step in enumerate(steps)
            if str(step.get("uses", "")).split("@")[0] == "actions/checkout"
        ]
        assert checkouts, "publish job has no checkout for the gate script"
        for i in checkouts:
            assert steps[i].get("with", {}).get("ref") == "main"
        download = next(
            i for i, step in enumerate(steps)
            if str(step.get("uses", "")).split("@")[0] == "actions/download-artifact"
        )
        assert max(checkouts) < download


class TestPublishStepPinning:
    def test_skip_existing_is_true(self, publish_step):
        assert publish_step["with"].get("skip-existing") is True

    def test_action_is_sha_pinned(self, publish_step):
        ref = publish_step["uses"].split("@", 1)[1]
        assert ref != "release/v1", "publish action uses the mutable release/v1 alias"
        assert not re.fullmatch(r"v\d+", ref), (
            f"publish action pinned to mutable major alias '{ref}'"
        )
        assert re.fullmatch(r"[0-9a-f]{40}", ref), (
            f"publish action ref {ref!r} is not a full commit SHA pin"
        )

    def test_pin_comment_meets_attestations_floor(self, workflow_text):
        """SHA pin carries a '# vX.Y.Z' comment; that version is the intent."""
        pin_lines = [
            line
            for line in workflow_text.splitlines()
            if f"{PUBLISH_ACTION}@" in line and not line.lstrip().startswith("#")
        ]
        assert len(pin_lines) == 1
        match = re.search(r"#\s*v(\d+)\.(\d+)\.(\d+)\s*$", pin_lines[0])
        assert match, (
            "publish action pin lacks the '# vX.Y.Z' version comment "
            f"(convention: SHA pin + comment): {pin_lines[0].strip()!r}"
        )
        version = tuple(int(part) for part in match.groups())
        assert version >= ATTESTATIONS_FLOOR, (
            f"publish action pinned at v{'.'.join(match.groups())}, below the "
            f"attestations-by-default floor v{'.'.join(map(str, ATTESTATIONS_FLOOR))}"
        )
