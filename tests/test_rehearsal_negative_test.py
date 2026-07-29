"""Tests for the rehearsal rig's G2 negative test driver
(Ctl/rehearsal/negative-test.sh).

Only the offline paths are exercised — this suite runs in sandboxes with
no rig env vars and no network: the case matrix, the production-target
guard inherited from lib.sh, the R14 skip-clean behavior, the pending
evidence skeleton, the --verify-evidence pending/populated/malformed
branches, and the fail-before-publish job assertion against recorded API
fixtures (tests/fixtures/rehearsal/negative-jobs-*.json — invented test
data, clearly labeled, never evidence). Everything is driven through
subprocess so the script is tested exactly as invoked.
"""
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REHEARSAL_DIR = REPO_ROOT / "Ctl" / "rehearsal"
NEGATIVE_TEST = REHEARSAL_DIR / "negative-test.sh"
PENDING_EVIDENCE = REPO_ROOT / "docs" / "rehearsal" / "evidence-negative-test.md"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "rehearsal"

JOBS_PASS = FIXTURES / "negative-jobs-pass.json"
JOBS_PUBLISHED = FIXTURES / "negative-jobs-published.json"
JOBS_WRONG_FAILURE = FIXTURES / "negative-jobs-wrong-failure.json"

# The case matrix (scenario name = negative-<case>).
CASES = ("unsigned-tag", "wrong-signer", "not-main-head")
SCENARIOS = tuple(f"negative-{case}" for case in CASES)

# The rehearsal-only credential variables R14 skip-clean keys on. The
# TestPyPI token is deliberately absent: no rig script reads it (the
# tokenless JSON API does all TestPyPI verification), so it must not gate.
RIG_ENV_VARS = (
    "LMER_REHEARSAL_GITHUB_TOKEN",
    "LMER_REHEARSAL_SIGNING_KEY",
)

MAIN_HEAD = "a" * 40
OTHER_SHA = "b" * 40

JOBS_OK_SUMMARY = (
    "Verify tag signature and main head=failure | "
    "Verify tag matches pyproject version=skipped | "
    "Run CI checks=skipped | "
    "Build wheel and sdist=skipped | "
    "Publish to PyPI=skipped | "
    "Create GitHub Release=skipped"
)


def clean_env(**extra):
    """Environment with every LMER_* var stripped (no rig, no production
    credentials), plus any explicit additions."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LMER_")}
    env.update(extra)
    return env


def run_negative(*args, env=None, **extra_env):
    """Run negative-test.sh with a clean environment; --env-file /dev/null
    so a developer's real rig.env can never leak into a test."""
    return subprocess.run(
        [str(NEGATIVE_TEST), "--env-file", "/dev/null", *args],
        capture_output=True,
        text=True,
        env=env if env is not None else clean_env(**extra_env),
        cwd=REPO_ROOT,
    )


def populated_evidence_text(overrides=None):
    """A populated (status: complete) evidence document in the agreed
    format — prose header + one fenced yaml block. overrides maps full
    yaml keys (e.g. 'negative-unsigned-tag.recorded_conclusion') to new
    values, or to None to drop the line entirely."""
    fields = {
        "status": "complete",
        "rig_repo": "bot/lmer-rehearsal",
        "rig_project": "lmer-rehearsal",
        "main_head_sha": MAIN_HEAD,
        "derive_check": "pass",
        "recorded_at": "2026-07-27T00:00:00Z",
    }
    for index, scenario in enumerate(SCENARIOS):
        sha = OTHER_SHA if scenario == "negative-not-main-head" else MAIN_HEAD
        run_id = str(101 + index)
        fields.update(
            {
                f"{scenario}.tag": f"v0.0.0-neg-{scenario}-20260727T000000Z",
                f"{scenario}.tag_sha": sha,
                f"{scenario}.workflow_run_id": run_id,
                f"{scenario}.workflow_run_url": (
                    f"https://github.com/bot/lmer-rehearsal/actions/runs/{run_id}"
                ),
                f"{scenario}.expected_conclusion": "failure",
                f"{scenario}.recorded_conclusion": "failure",
                f"{scenario}.failed_job": "verify-tag-signature",
                f"{scenario}.jobs": JOBS_OK_SUMMARY,
                f"{scenario}.testpypi_files_before": "(none)",
                f"{scenario}.testpypi_files_after": "(none)",
                f"{scenario}.published": "false",
                f"{scenario}.github_release": "absent",
            }
        )
    fields.update(overrides or {})
    lines = [
        "# Rehearsal evidence: G2 negative tag-verification tests",
        "",
        "Test-constructed populated evidence (unit test data).",
        "",
        "```yaml",
    ]
    lines += [f"{key}: {value}" for key, value in fields.items() if value is not None]
    lines.append("```")
    return "\n".join(lines) + "\n"


def write_populated(tmp_path, overrides=None):
    path = tmp_path / "evidence-negative-test.md"
    path.write_text(populated_evidence_text(overrides))
    return path


def verify(path):
    return run_negative("--verify-evidence", str(path))


class TestSyntax:
    def test_bash_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(NEGATIVE_TEST)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_script_executable(self):
        assert os.access(NEGATIVE_TEST, os.X_OK), "negative-test.sh must be executable"


class TestArgParsing:
    def test_no_mode_is_a_usage_error(self):
        result = run_negative()
        assert result.returncode == 2
        assert "usage:" in result.stderr

    def test_unknown_flag_is_a_usage_error(self):
        result = run_negative("--bogus")
        assert result.returncode == 2
        assert "usage:" in result.stderr

    def test_modes_are_mutually_exclusive(self):
        result = run_negative("--all", "--verify-evidence", "x.md")
        assert result.returncode == 2
        assert "mutually exclusive" in result.stderr

    def test_flag_missing_value_is_a_usage_error(self):
        result = subprocess.run(
            [str(NEGATIVE_TEST), "--case"],
            capture_output=True,
            text=True,
            env=clean_env(),
            cwd=REPO_ROOT,
        )
        assert result.returncode == 2
        assert "usage:" in result.stderr

    def test_help_exits_zero_and_documents_modes(self):
        result = run_negative("--help")
        assert result.returncode == 0
        for flag in (
            "--all",
            "--case",
            "--verify-evidence",
            "--write-pending",
            "--assert-jobs",
            "--dry-run",
        ):
            assert flag in result.stdout


class TestCaseMatrix:
    """The three-case matrix: every valid case is accepted, anything else
    is rejected loudly with the full matrix listed."""

    def test_unknown_case_is_rejected_and_lists_matrix(self):
        result = run_negative("--case", "totally-made-up")
        assert result.returncode == 2
        for case in CASES:
            assert case in result.stderr

    @pytest.mark.parametrize("case", CASES)
    def test_valid_cases_accepted(self, case):
        # Without rig env vars a valid case reaches the R14 skip-clean
        # (exit 0) instead of the arg-parsing error (exit 2).
        result = run_negative("--case", case)
        assert result.returncode == 0, result.stderr
        assert "SKIP-CLEAN" in result.stdout

    def test_dry_run_all_documents_every_case(self):
        result = run_negative("--all", "--dry-run")
        assert result.returncode == 0, result.stderr
        assert "no network calls" in result.stdout
        for case in CASES:
            assert case in result.stdout
        assert "verify-tag-signature" in result.stdout

    def test_dry_run_single_case_plans_only_that_case(self):
        result = run_negative("--case", "wrong-signer", "--dry-run")
        assert result.returncode == 0, result.stderr
        assert "wrong-signer" in result.stdout
        assert "unsigned-tag" not in result.stdout


class TestProductionTargetGuard:
    """The guard from lib.sh is inherited: production targets are refused
    offline, in every mode, before skip-clean and before any network."""

    def test_rejects_production_repo(self):
        result = run_negative("--all", "--repo", "lmer2/lmer")
        assert result.returncode != 0
        assert "REFUSED" in result.stderr
        assert "SKIP-CLEAN" not in result.stdout

    def test_rejects_any_repo_named_lmer(self):
        result = run_negative("--all", "--repo", "somebody/lmer")
        assert result.returncode != 0
        assert "REFUSED" in result.stderr

    def test_rejects_production_project(self):
        result = run_negative(
            "--all", "--repo", "bot/lmer-rehearsal", "--project", "lmer"
        )
        assert result.returncode != 0
        assert "REFUSED" in result.stderr

    def test_rejects_production_credentials_even_in_offline_modes(self):
        result = run_negative(
            "--verify-evidence",
            str(PENDING_EVIDENCE),
            LMER_RELEASE_SIGNING_KEY="/release-signing-key",
        )
        assert result.returncode != 0
        assert "REFUSED" in result.stderr

    def test_rejects_rehearsal_key_at_production_mount(self):
        result = run_negative(
            "--all",
            "--repo",
            "bot/lmer-rehearsal",
            LMER_REHEARSAL_SIGNING_KEY="/release-signing-key",
        )
        assert result.returncode != 0
        assert "REFUSED" in result.stderr


class TestSkipClean:
    """R14: the rig-touching modes SKIP-CLEAN (exit 0, clear notice) when
    the rig env vars are absent — --all in a sandbox does nothing."""

    @pytest.mark.parametrize(
        "mode_args", [["--all"], ["--case", "unsigned-tag"]]
    )
    def test_rig_modes_skip_clean_without_rig_env(self, mode_args):
        result = run_negative(*mode_args)
        assert result.returncode == 0, result.stderr
        assert "SKIP-CLEAN" in result.stdout

    def test_skip_clean_names_every_missing_var(self):
        result = run_negative("--all")
        assert result.returncode == 0
        for var in RIG_ENV_VARS:
            assert var in result.stdout

    def test_skip_clean_points_at_rig_env_example(self):
        result = run_negative("--all")
        assert "rig.env.example" in result.stdout

    def test_partial_env_lists_only_missing_vars(self):
        result = run_negative("--all", LMER_REHEARSAL_GITHUB_TOKEN="dummy")
        assert result.returncode == 0
        assert "SKIP-CLEAN" in result.stdout
        assert "LMER_REHEARSAL_GITHUB_TOKEN" not in result.stdout
        assert "LMER_REHEARSAL_SIGNING_KEY" in result.stdout
        # The optional TestPyPI token gates nothing (nothing reads it).
        assert "LMER_REHEARSAL_TESTPYPI_TOKEN" not in result.stdout


class TestPendingEvidence:
    """The checked-in evidence file is an explicitly pending skeleton:
    no fabricated data, loud PENDING verify branch, format pinned to the
    script's own writer."""

    def test_checked_in_file_exists_and_is_pending(self):
        text = PENDING_EVIDENCE.read_text()
        assert "status: pending" in text
        assert "PENDING" in text
        assert "negative-test.sh --all" in text

    def test_checked_in_file_carries_no_fake_data(self):
        # Every yaml value is TBD except the status marker and the
        # expected conclusions (design expectations, not recordings).
        text = PENDING_EVIDENCE.read_text()
        in_yaml = False
        for line in text.splitlines():
            if line.strip() == "```yaml":
                in_yaml = True
                continue
            if line.strip() == "```":
                in_yaml = False
                continue
            if not in_yaml or not line.strip():
                continue
            key, _, value = line.partition(":")
            value = value.strip()
            if key == "status":
                assert value == "pending"
            elif key.endswith(".expected_conclusion"):
                assert value == "failure"
            else:
                assert value == "TBD", f"pending skeleton has data in {key!r}"

    def test_checked_in_file_covers_all_scenarios(self):
        text = PENDING_EVIDENCE.read_text()
        for scenario in SCENARIOS:
            assert f"{scenario}.tag:" in text
            assert f"{scenario}.jobs:" in text

    def test_checked_in_file_matches_write_pending_output(self, tmp_path):
        # The skeleton format is pinned to the script's writer — drift
        # between the doc and the script fails here.
        regen = tmp_path / "regen.md"
        result = run_negative("--write-pending", str(regen))
        assert result.returncode == 0, result.stderr
        assert regen.read_text() == PENDING_EVIDENCE.read_text()

    def test_verify_pending_exits_zero_with_loud_notice(self):
        result = verify(PENDING_EVIDENCE)
        assert result.returncode == 0, result.stderr
        assert "PENDING — negative test has not run yet" in result.stdout

    def test_write_pending_refuses_to_clobber_recorded_evidence(self, tmp_path):
        path = write_populated(tmp_path)
        result = run_negative("--write-pending", str(path))
        assert result.returncode != 0
        assert "refusing" in result.stderr
        assert "status: complete" in path.read_text()


class TestAssertJobs:
    """--assert-jobs against the recorded API fixtures: the offline
    fail-before-publish job-table check."""

    def test_pass_fixture(self):
        result = run_negative("--assert-jobs", str(JOBS_PASS))
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout
        assert "Verify tag signature and main head=failure" in result.stdout

    def test_published_fixture_fails(self):
        result = run_negative("--assert-jobs", str(JOBS_PUBLISHED))
        assert result.returncode != 0
        assert "Publish to PyPI" in result.stderr

    def test_wrong_failing_job_fixture_fails(self):
        result = run_negative("--assert-jobs", str(JOBS_WRONG_FAILURE))
        assert result.returncode != 0
        assert "verify-tag-signature" in result.stderr

    def test_missing_file_fails(self):
        result = run_negative("--assert-jobs", "/nonexistent/jobs.json")
        assert result.returncode != 0


class TestVerifyPopulated:
    """--verify-evidence on a populated file: the full offline re-check,
    failing loudly on any inconsistency."""

    def test_consistent_evidence_passes(self, tmp_path):
        result = verify(write_populated(tmp_path))
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_missing_file_fails(self):
        result = verify(Path("/nonexistent/evidence.md"))
        assert result.returncode != 0

    def test_missing_status_field_fails(self, tmp_path):
        path = write_populated(tmp_path, {"status": None})
        result = verify(path)
        assert result.returncode != 0
        assert "status" in result.stderr

    def test_unknown_status_fails(self, tmp_path):
        path = write_populated(tmp_path, {"status": "done"})
        result = verify(path)
        assert result.returncode != 0
        assert "status" in result.stderr

    def test_leftover_tbd_in_complete_file_fails(self, tmp_path):
        path = write_populated(
            tmp_path, {"negative-wrong-signer.workflow_run_url": "TBD"}
        )
        result = verify(path)
        assert result.returncode != 0
        assert "TBD" in result.stderr

    def test_run_not_failure_fails(self, tmp_path):
        path = write_populated(
            tmp_path, {"negative-unsigned-tag.recorded_conclusion": "success"}
        )
        result = verify(path)
        assert result.returncode != 0
        assert "recorded_conclusion" in result.stderr

    def test_wrong_failed_job_fails(self, tmp_path):
        path = write_populated(tmp_path, {"negative-unsigned-tag.failed_job": "build"})
        result = verify(path)
        assert result.returncode != 0
        assert "verify-tag-signature" in result.stderr

    def test_downstream_job_not_skipped_fails(self, tmp_path):
        bad_jobs = JOBS_OK_SUMMARY.replace(
            "Publish to PyPI=skipped", "Publish to PyPI=success"
        )
        path = write_populated(tmp_path, {"negative-wrong-signer.jobs": bad_jobs})
        result = verify(path)
        assert result.returncode != 0
        assert "Publish to PyPI" in result.stderr

    def test_testpypi_gained_files_fails(self, tmp_path):
        path = write_populated(
            tmp_path,
            {
                "negative-not-main-head.testpypi_files_after": (
                    "lmer_rehearsal-0.0.0-py3-none-any.whl"
                )
            },
        )
        result = verify(path)
        assert result.returncode != 0
        assert "TestPyPI" in result.stderr

    def test_github_release_present_fails(self, tmp_path):
        path = write_populated(
            tmp_path, {"negative-unsigned-tag.github_release": "present"}
        )
        result = verify(path)
        assert result.returncode != 0
        assert "github_release" in result.stderr

    def test_published_true_fails(self, tmp_path):
        path = write_populated(tmp_path, {"negative-wrong-signer.published": "true"})
        result = verify(path)
        assert result.returncode != 0
        assert "published" in result.stderr

    def test_not_main_head_tag_at_main_head_fails(self, tmp_path):
        path = write_populated(
            tmp_path, {"negative-not-main-head.tag_sha": MAIN_HEAD}
        )
        result = verify(path)
        assert result.returncode != 0
        assert "main_head_sha" in result.stderr

    def test_malformed_sha_fails(self, tmp_path):
        path = write_populated(tmp_path, {"negative-unsigned-tag.tag_sha": "not-a-sha"})
        result = verify(path)
        assert result.returncode != 0
        assert "hex" in result.stderr

    def test_derive_check_fail_fails(self, tmp_path):
        path = write_populated(tmp_path, {"derive_check": "fail"})
        result = verify(path)
        assert result.returncode != 0
        assert "derive_check" in result.stderr

    def test_missing_case_field_fails(self, tmp_path):
        path = write_populated(tmp_path, {"negative-wrong-signer.tag": None})
        result = verify(path)
        assert result.returncode != 0
        assert "negative-wrong-signer.tag" in result.stderr

    def test_production_repo_in_evidence_fails(self, tmp_path):
        # Production-target guard inheritance from lib.sh: evidence that
        # claims a production target is inconsistent by definition.
        path = write_populated(tmp_path, {"rig_repo": "lmer2/lmer"})
        result = verify(path)
        assert result.returncode != 0
        assert "REFUSED" in result.stderr

    def test_production_project_in_evidence_fails(self, tmp_path):
        path = write_populated(tmp_path, {"rig_project": "lmer"})
        result = verify(path)
        assert result.returncode != 0
        assert "REFUSED" in result.stderr
