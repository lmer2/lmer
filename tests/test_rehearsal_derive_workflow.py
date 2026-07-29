"""Tests for the rehearsal rig's workflow deriver
(Ctl/rehearsal/derive-workflow.py).

Entirely hermetic: the transform and the drift guard are exercised against
checked-in fixture workflows (tests/fixtures/rehearsal/), plus mutations of
the good fixture written to tmp_path; --check is additionally pointed at
the live production workflow so drift in this repo fails the suite. No
network. Everything is driven through subprocess so the script is tested
exactly as invoked (stand-up.sh runs it the same way).
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DERIVER = REPO_ROOT / "Ctl" / "rehearsal" / "derive-workflow.py"
PRODUCTION_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "rehearsal"
GOOD = FIXTURES / "release-with-verify.yml"
BAD = FIXTURES / "release-missing-verify.yml"

TESTPYPI_LEGACY = "https://test.pypi.org/legacy/"


def clean_env(**extra):
    """Environment with every LMER_* var stripped, so a developer's real
    rig.env values can never leak into a test."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LMER_")}
    env.update(extra)
    return env


def run_deriver(*args, env=None, **extra_env):
    return subprocess.run(
        [sys.executable, str(DERIVER), *args],
        capture_output=True,
        text=True,
        env=env if env is not None else clean_env(**extra_env),
        cwd=REPO_ROOT,
    )


def mutated_fixture(tmp_path, old, new):
    """The good fixture with one textual mutation, written to tmp_path."""
    text = GOOD.read_text()
    assert old in text, f"mutation target {old!r} not in fixture"
    path = tmp_path / "release.yml"
    path.write_text(text.replace(old, new))
    return path


class TestCheck:
    """--check: the drift guard (exit 0 shape ok / nonzero + loud)."""

    def test_good_fixture_passes(self):
        result = run_deriver("--check", str(GOOD))
        assert result.returncode == 0, result.stderr
        assert "drift guard OK" in result.stdout

    def test_live_production_workflow_passes(self):
        # The guard is additionally pointed at the real workflow: if
        # .github/workflows/release.yml drifts from the expected shape,
        # this repo's suite fails, not just the rig.
        result = run_deriver("--check", str(PRODUCTION_WORKFLOW))
        assert result.returncode == 0, result.stderr

    def test_default_path_is_the_production_workflow(self):
        # stand-up.sh invokes `derive-workflow.py --check` with no
        # argument; that must check the production workflow.
        result = run_deriver("--check")
        assert result.returncode == 0, result.stderr
        assert ".github/workflows/release.yml" in result.stdout

    def test_missing_verify_fixture_fails_loudly(self):
        result = run_deriver("--check", str(BAD))
        assert result.returncode != 0
        assert "DRIFT GUARD FAILED" in result.stderr
        assert "verify-tag-signature" in result.stderr

    def test_missing_verify_fixture_reports_every_gap(self):
        result = run_deriver("--check", str(BAD))
        assert "first job is 'build'" in result.stderr
        assert "skip-existing" in result.stderr

    def test_fails_when_verify_job_is_not_first(self, tmp_path):
        text = GOOD.read_text()
        jobs_at = text.index("jobs:\n")
        verify_at = text.index("  verify-tag-signature:")
        build_at = text.index("  build:")
        publish_at = text.index("  publish-pypi:")
        reordered = (
            text[:jobs_at]
            + "jobs:\n"
            + text[build_at:publish_at]
            + text[verify_at:build_at]
            + text[publish_at:]
        )
        path = tmp_path / "release.yml"
        path.write_text(reordered)
        result = run_deriver("--check", str(path))
        assert result.returncode != 0
        assert "first job is 'build'" in result.stderr

    def test_fails_when_signers_come_from_secrets(self, tmp_path):
        path = mutated_fixture(
            tmp_path,
            "${{ vars.RELEASE_ALLOWED_SIGNERS }}",
            "${{ secrets.RELEASE_ALLOWED_SIGNERS }}",
        )
        result = run_deriver("--check", str(path))
        assert result.returncode != 0
        assert "RELEASE_ALLOWED_SIGNERS" in result.stderr
        assert "vars.RELEASE_ALLOWED_SIGNERS" in result.stderr

    def test_fails_when_signers_env_is_absent(self, tmp_path):
        path = mutated_fixture(
            tmp_path,
            "RELEASE_ALLOWED_SIGNERS: ${{ vars.RELEASE_ALLOWED_SIGNERS }}",
            "OTHER_VAR: ${{ vars.OTHER_VAR }}",
        )
        result = run_deriver("--check", str(path))
        assert result.returncode != 0
        assert "RELEASE_ALLOWED_SIGNERS" in result.stderr

    def test_fails_without_main_head_api_assertion(self, tmp_path):
        path = mutated_fixture(tmp_path, "commits/heads/main", "commits/tags")
        result = run_deriver("--check", str(path))
        assert result.returncode != 0
        assert "main HEAD" in result.stderr

    def test_fails_without_skip_existing(self, tmp_path):
        path = mutated_fixture(
            tmp_path, "skip-existing: true", "verbose: true"
        )
        result = run_deriver("--check", str(path))
        assert result.returncode != 0
        assert "skip-existing" in result.stderr

    def test_fails_when_skip_existing_is_false(self, tmp_path):
        path = mutated_fixture(
            tmp_path, "skip-existing: true", "skip-existing: false"
        )
        result = run_deriver("--check", str(path))
        assert result.returncode != 0
        assert "skip-existing" in result.stderr

    def test_fails_when_gate_checkout_not_pinned_to_main(self, tmp_path):
        path = mutated_fixture(tmp_path, "ref: main", "ref: ${{ github.ref }}")
        result = run_deriver("--check", str(path))
        assert result.returncode != 0
        assert "pinned to ref: main" in result.stderr

    def test_fails_without_version_reuse_gate(self, tmp_path):
        path = mutated_fixture(
            tmp_path,
            "PYPI_PROJECT_URL: https://pypi.org/project/lmer/",
            "OTHER_URL: https://example.com/",
        )
        result = run_deriver("--check", str(path))
        assert result.returncode != 0
        assert "version-reuse gate" in result.stderr

    def test_fails_when_publish_job_has_no_checkout(self, tmp_path):
        """The publish job holds id-token: write and runs the reuse-gate
        script; without a checkout the script cannot come from `main` at
        all, so the shape is wrong before any pin question."""
        text = GOOD.read_text()
        publish_checkout = """      - uses: actions/checkout@v6
        with:
          ref: main
          sparse-checkout: .github/scripts

"""
        assert publish_checkout in text
        path = tmp_path / "no-publish-checkout.yml"
        path.write_text(text.replace(publish_checkout, ""))
        result = run_deriver("--check", str(path))
        assert result.returncode != 0
        assert "publish job has no checkout" in result.stderr

    def test_fails_when_publish_checkout_follows_the_dist_download(self, tmp_path):
        """actions/checkout cleans the workspace it lands in — checking out
        after the download would delete the distributions being published."""
        text = GOOD.read_text()
        checkout = """      - uses: actions/checkout@v6
        with:
          ref: main
          sparse-checkout: .github/scripts

"""
        download = """      - name: Download dist artifact
        uses: actions/download-artifact@v4
        with:
          name: dist
          path: dist/

"""
        assert checkout + download in text
        path = tmp_path / "swapped.yml"
        path.write_text(text.replace(checkout + download, download + checkout))
        result = run_deriver("--check", str(path))
        assert result.returncode != 0
        assert "checks out after downloading" in result.stderr

    def test_plain_check_dry_runs_the_transform(self, tmp_path):
        # A semantically-identical reformat keeps the YAML shape checks
        # green but breaks the text-level transform — plain --check must
        # catch it (a green check has to imply --emit will succeed).
        path = mutated_fixture(
            tmp_path,
            "with:\n          skip-existing: true",
            "with: {skip-existing: true}",
        )
        result = run_deriver("--check", str(path))
        assert result.returncode != 0
        assert "StopIteration" not in result.stderr
        assert "Traceback" not in result.stderr
        assert "DRIFT GUARD FAILED" in result.stderr

    def test_green_check_reports_the_dry_run(self):
        result = run_deriver("--check", str(GOOD))
        assert result.returncode == 0, result.stderr
        assert "transform dry-run OK" in result.stdout

    def test_fails_when_publish_environment_is_not_pypi(self, tmp_path):
        path = mutated_fixture(tmp_path, "name: pypi", "name: production")
        result = run_deriver("--check", str(path))
        assert result.returncode != 0
        assert "environment.name" in result.stderr

    def test_fails_on_missing_file(self):
        result = run_deriver("--check", "/nonexistent/release.yml")
        assert result.returncode != 0
        assert "cannot read" in result.stderr

    def test_fails_on_invalid_yaml(self, tmp_path):
        path = tmp_path / "release.yml"
        path.write_text("jobs: [unclosed\n")
        result = run_deriver("--check", str(path))
        assert result.returncode != 0
        assert "not valid YAML" in result.stderr


class TestEmit:
    """--emit: check, transform, self-verify, derived YAML on stdout."""

    def emit(self, path=GOOD, **extra_env):
        result = run_deriver("--emit", str(path), **extra_env)
        assert result.returncode == 0, result.stderr
        return result.stdout, yaml.safe_load(result.stdout)

    def publish(self, doc):
        job = doc["jobs"]["publish-pypi"]
        (step,) = [
            s
            for s in job["steps"]
            if str(s.get("uses", "")).startswith("pypa/gh-action-pypi-publish")
        ]
        return job, step

    def test_publish_step_gains_testpypi_repository_url(self):
        _, doc = self.emit()
        _, step = self.publish(doc)
        assert step["with"]["repository-url"] == TESTPYPI_LEGACY

    def test_skip_existing_survives(self):
        _, doc = self.emit()
        _, step = self.publish(doc)
        assert step["with"]["skip-existing"] is True

    def test_publish_action_pin_survives(self):
        text, _ = self.emit()
        assert (
            "pypa/gh-action-pypi-publish@76f52bc884231f62b9a034ebfe128415bbaabdfc"
            in text
        )

    def test_environment_renamed_with_testpypi_url(self):
        _, doc = self.emit()
        job, _ = self.publish(doc)
        assert job["environment"]["name"] == "testpypi"
        assert (
            job["environment"]["url"]
            == "https://test.pypi.org/project/lmer-rehearsal/"
        )

    def test_project_name_swapped_in_github_release_job(self):
        _, doc = self.emit()
        job = doc["jobs"]["github-release"]
        assert job["name"] == "Create lmer-rehearsal GitHub Release"
        (step,) = [s for s in job["steps"] if "with" in s]
        assert step["with"]["body"].startswith("lmer-rehearsal release")

    def test_no_production_pypi_reference_survives(self):
        text, _ = self.emit()
        assert "pypi.org" not in text.replace("test.pypi.org", "")

    def test_verify_job_passes_through_verbatim(self):
        # Everything outside the enumerated transform is untouched —
        # including comments (the point of a text-level transform).
        text, doc = self.emit()
        source = yaml.safe_load(GOOD.read_text())
        assert doc["jobs"]["verify-tag-signature"] == (
            source["jobs"]["verify-tag-signature"]
        )
        assert "# Pinned to an exact commit SHA" in text

    def test_respects_rehearsal_project_and_environment_env(self):
        _, doc = self.emit(
            LMER_REHEARSAL_PROJECT="lmer-rig-test",
            LMER_REHEARSAL_ENVIRONMENT="rigpypi",
        )
        job, _ = self.publish(doc)
        assert job["environment"]["name"] == "rigpypi"
        assert (
            job["environment"]["url"]
            == "https://test.pypi.org/project/lmer-rig-test/"
        )

    def test_derived_output_still_passes_check(self, tmp_path):
        text, _ = self.emit()
        # The derived workflow keeps the production shape (only the
        # publish target changed), except environment.name — assert the
        # other guard clauses hold by re-running --check after restoring
        # the environment identity.
        restored = text.replace("name: testpypi", "name: pypi").replace(
            # Covers both the environment url and the reuse gate's
            # PYPI_PROJECT_URL env var (same string by contract).
            "https://test.pypi.org/project/lmer-rehearsal/",
            "https://pypi.org/project/lmer/",
        )
        path = tmp_path / "derived.yml"
        path.write_text(restored)
        result = run_deriver("--check", str(path))
        assert result.returncode == 0, result.stderr

    def test_stdout_is_only_the_workflow(self):
        result = run_deriver("--emit", str(GOOD))
        assert result.returncode == 0
        # The guard's OK notice goes to stderr, never into the emitted
        # workflow.
        assert result.stdout.lstrip().startswith("#")
        assert "drift guard OK" not in result.stdout
        assert "drift guard OK" in result.stderr

    def test_emit_from_live_production_workflow(self):
        text, doc = self.emit(PRODUCTION_WORKFLOW)
        assert "test.pypi.org/legacy/" in text
        _, step = self.publish(doc)
        assert step["with"]["repository-url"] == TESTPYPI_LEGACY

    def test_emit_refuses_bad_shape(self):
        result = run_deriver("--emit", str(BAD))
        assert result.returncode != 0
        assert "DRIFT GUARD FAILED" in result.stderr
        assert result.stdout == ""

    def test_emit_refuses_production_project_name(self):
        # Same offline production-target guard as lib.sh: the rehearsal
        # project must be unmistakably non-production.
        result = run_deriver("--emit", str(GOOD), LMER_REHEARSAL_PROJECT="lmer")
        assert result.returncode != 0
        assert "REFUSED" in result.stderr
        assert result.stdout == ""

    def test_emit_refuses_production_environment_name(self):
        result = run_deriver(
            "--emit", str(GOOD), LMER_REHEARSAL_ENVIRONMENT="pypi"
        )
        assert result.returncode != 0
        assert "REFUSED" in result.stderr


class TestRigDiff:
    """--check --rig-workflow: the committed-copy half of the drift guard
    (re-derive from production, diff against the rig repo's copy)."""

    def rig_copy(self, tmp_path):
        result = run_deriver("--emit", str(GOOD))
        assert result.returncode == 0, result.stderr
        path = tmp_path / "rig-release.yml"
        path.write_text(result.stdout)
        return path

    def test_matching_rig_copy_passes(self, tmp_path):
        path = self.rig_copy(tmp_path)
        result = run_deriver("--check", "--rig-workflow", str(path), str(GOOD))
        assert result.returncode == 0, result.stderr
        assert "matches the freshly derived workflow" in result.stdout

    def test_stale_rig_copy_fails_with_diff(self, tmp_path):
        path = self.rig_copy(tmp_path)
        path.write_text(
            path.read_text().replace("skip-existing: true", "skip-existing: false")
        )
        result = run_deriver("--check", "--rig-workflow", str(path), str(GOOD))
        assert result.returncode != 0
        assert "DRIFT GUARD FAILED" in result.stderr
        # A readable unified diff, pointing back at stand-up.sh.
        assert "-          skip-existing: true" in result.stderr
        assert "+          skip-existing: false" in result.stderr
        assert "stand-up.sh" in result.stderr

    def test_rig_copy_read_from_stdin(self, tmp_path):
        path = self.rig_copy(tmp_path)
        result = subprocess.run(
            [sys.executable, str(DERIVER), "--check", "--rig-workflow", "-",
             str(GOOD)],
            input=path.read_text(),
            capture_output=True,
            text=True,
            env=clean_env(),
            cwd=REPO_ROOT,
        )
        assert result.returncode == 0, result.stderr

    def test_missing_rig_copy_fails(self):
        result = run_deriver(
            "--check", "--rig-workflow", "/nonexistent/rig.yml", str(GOOD)
        )
        assert result.returncode != 0
        assert "cannot read the rig workflow copy" in result.stderr

    def test_diff_respects_rehearsal_identity_env(self, tmp_path):
        # The diff derives with the same rig identity env the runners
        # export; a copy derived under a different project must mismatch.
        path = self.rig_copy(tmp_path)
        result = run_deriver(
            "--check", "--rig-workflow", str(path), str(GOOD),
            LMER_REHEARSAL_PROJECT="lmer-rig-other",
        )
        assert result.returncode != 0
        assert "DRIFT GUARD FAILED" in result.stderr

    def test_rig_workflow_is_check_only(self, tmp_path):
        path = self.rig_copy(tmp_path)
        result = run_deriver("--emit", "--rig-workflow", str(path), str(GOOD))
        assert result.returncode == 2
        assert "--rig-workflow only applies to --check" in result.stderr

    def test_diff_refuses_production_project_name(self, tmp_path):
        path = self.rig_copy(tmp_path)
        result = run_deriver(
            "--check", "--rig-workflow", str(path), str(GOOD),
            LMER_REHEARSAL_PROJECT="lmer",
        )
        assert result.returncode != 0
        assert "REFUSED" in result.stderr


class TestShapeErrorsNotTracebacks:
    """YAML-valid but unusually formatted workflows must fail as drift
    guard reports, never raw StopIteration tracebacks."""

    def test_empty_jobs_mapping_fails_loudly(self, tmp_path):
        path = tmp_path / "release.yml"
        path.write_text("jobs: {}\n")
        result = run_deriver("--check", str(path))
        assert result.returncode != 0
        assert "StopIteration" not in result.stderr
        assert "Traceback" not in result.stderr
        assert "empty jobs mapping" in result.stderr

    def test_flow_style_with_block_fails_loudly(self, tmp_path):
        # Semantically identical to the good fixture (--check passes), but
        # the text-level transform cannot find the skip-existing: line.
        path = mutated_fixture(
            tmp_path,
            "with:\n          skip-existing: true",
            "with: {skip-existing: true}",
        )
        result = run_deriver("--emit", str(path))
        assert result.returncode != 0
        assert "StopIteration" not in result.stderr
        assert "Traceback" not in result.stderr
        assert "DRIFT GUARD FAILED" in result.stderr
        assert "skip-existing" in result.stderr


class TestArgParsing:
    def test_requires_a_mode(self):
        result = run_deriver(str(GOOD))
        assert result.returncode == 2
        assert "usage" in result.stderr.lower()

    def test_check_and_emit_are_exclusive(self):
        result = run_deriver("--check", "--emit", str(GOOD))
        assert result.returncode == 2

    def test_help_documents_both_modes(self):
        result = run_deriver("--help")
        assert result.returncode == 0
        assert "--check" in result.stdout
        assert "--emit" in result.stdout
