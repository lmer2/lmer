"""Tests for the release rehearsal rig scripts (Ctl/rehearsal).

Only the offline paths are exercised — this suite runs in sandboxes with
no rig env vars and no network: the production-target guard, the R14
skip-clean behavior, argument parsing, and the evidence writer/parser
round trip. Everything is driven through subprocess so the scripts are
tested exactly as invoked.
"""
import os
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REHEARSAL_DIR = REPO_ROOT / "Ctl" / "rehearsal"
STAND_UP = REHEARSAL_DIR / "stand-up.sh"
LIB = REHEARSAL_DIR / "lib.sh"

# The rehearsal-only credential variables R14 skip-clean keys on. The
# TestPyPI token is deliberately absent: no rig script reads it (the
# tokenless JSON API does all TestPyPI verification), so it must not gate.
RIG_ENV_VARS = (
    "LMER_REHEARSAL_GITHUB_TOKEN",
    "LMER_REHEARSAL_SIGNING_KEY",
)


def clean_env(**extra):
    """Environment with every LMER_* var stripped (no rig, no production
    credentials), plus any explicit additions."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LMER_")}
    env.update(extra)
    return env


def run_standup(*args, env=None, **extra_env):
    """Run stand-up.sh with a clean environment; --env-file /dev/null so a
    developer's real rig.env can never leak into a test."""
    return subprocess.run(
        [str(STAND_UP), "--env-file", "/dev/null", *args],
        capture_output=True,
        text=True,
        env=env if env is not None else clean_env(**extra_env),
        cwd=REPO_ROOT,
    )


def run_lib_fn(snippet, env=None, **extra_env):
    """Source lib.sh and run a snippet in the same shell."""
    return subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(LIB))}; {snippet}"],
        capture_output=True,
        text=True,
        env=env if env is not None else clean_env(**extra_env),
        cwd=REPO_ROOT,
    )


class TestSyntax:
    """Both scripts must parse (bash -n) — the cheapest offline gate."""

    @pytest.mark.parametrize("script", [STAND_UP, LIB])
    def test_bash_syntax(self, script):
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_scripts_executable(self):
        assert os.access(STAND_UP, os.X_OK), "stand-up.sh must be executable"
        assert os.access(LIB, os.X_OK), "lib.sh must be executable"


class TestCheckMode:
    """Source-level invariants of do_check (the live paths need a rig)."""

    def test_testpypi_visibility_is_informational_not_gating(self):
        """The TestPyPI project materializes on the FIRST trusted-publisher
        upload, and the negative tests publish nothing — so `--check` must
        not count its absence as a rig failure, or the documented
        '--check exits 0' prerequisite is unsatisfiable on a fresh rig."""
        text = STAND_UP.read_text()
        do_check = text[text.index("do_check()"):text.index("do_teardown()")]
        assert 'verify_step "TestPyPI' not in do_check
        assert "rehearsal_testpypi_project_exists" in do_check  # still probed
        assert "not a rig failure" in do_check


class TestProductionTargetGuard:
    """The hard guard refuses production targets fully offline, before any
    network call, in every mode including --dry-run."""

    def test_rejects_production_repo(self):
        result = run_standup("--dry-run", "--repo", "lmer2/lmer")
        assert result.returncode != 0
        assert "REFUSED" in result.stderr
        assert "lmer2/lmer" in result.stderr

    def test_rejects_any_repo_named_lmer(self):
        result = run_standup("--dry-run", "--repo", "somebody/lmer")
        assert result.returncode != 0
        assert "REFUSED" in result.stderr

    def test_rejects_production_repo_case_insensitively(self):
        result = run_standup("--dry-run", "--repo", "Lmer2/LMER")
        assert result.returncode != 0
        assert "REFUSED" in result.stderr

    def test_rejects_production_project(self):
        result = run_standup(
            "--dry-run", "--repo", "bot/lmer-rehearsal", "--project", "lmer"
        )
        assert result.returncode != 0
        assert "REFUSED" in result.stderr

    def test_guard_runs_before_skip_clean(self):
        # Even with no rig env vars (which normally SKIP-CLEANs), a
        # production target must be refused — the guard runs first.
        result = run_standup("--check", "--repo", "lmer2/lmer")
        assert result.returncode != 0
        assert "REFUSED" in result.stderr
        assert "SKIP-CLEAN" not in result.stdout

    def test_rejects_production_signing_key_in_env(self):
        result = run_standup(
            "--dry-run",
            "--repo",
            "bot/lmer-rehearsal",
            LMER_RELEASE_SIGNING_KEY="/release-signing-key",
        )
        assert result.returncode != 0
        assert "REFUSED" in result.stderr
        assert "LMER_RELEASE_SIGNING_KEY" in result.stderr

    def test_rejects_production_pat_in_env(self):
        result = run_standup(
            "--dry-run",
            "--repo",
            "bot/lmer-rehearsal",
            LMER_RELEASE_GITHUB_TOKEN="ghp_production",
        )
        assert result.returncode != 0
        assert "REFUSED" in result.stderr

    def test_rejects_rehearsal_key_at_production_mount(self):
        result = run_standup(
            "--dry-run",
            "--repo",
            "bot/lmer-rehearsal",
            LMER_REHEARSAL_SIGNING_KEY="/release-signing-key",
        )
        assert result.returncode != 0
        assert "REFUSED" in result.stderr

    def test_rejects_production_pypi_url(self):
        result = run_lib_fn("rehearsal_guard_url https://pypi.org/project/lmer/")
        assert result.returncode != 0
        assert "REFUSED" in result.stderr

    def test_rejects_upload_pypi_url(self):
        result = run_lib_fn("rehearsal_guard_url https://upload.pypi.org/legacy/")
        assert result.returncode != 0

    def test_allows_testpypi_url(self):
        result = run_lib_fn("rehearsal_guard_url https://test.pypi.org/legacy/")
        assert result.returncode == 0, result.stderr

    def test_allows_rehearsal_targets(self):
        result = run_standup(
            "--dry-run",
            "--repo",
            "bot/lmer-rehearsal",
            "--project",
            "lmer-rehearsal",
        )
        assert result.returncode == 0, result.stderr

    def test_dry_run_announces_no_network(self):
        result = run_standup("--dry-run", "--repo", "bot/lmer-rehearsal")
        assert result.returncode == 0
        assert "no network calls" in result.stdout


class TestSkipClean:
    """R14: every rig-touching mode SKIP-CLEANS (exit 0, clear notice)
    when the rig env vars are absent, so the rig verifies in a sandbox."""

    @pytest.mark.parametrize("mode_args", [["--check"], [], ["--teardown"]])
    def test_modes_skip_clean_without_rig_env(self, mode_args):
        result = run_standup(*mode_args)
        assert result.returncode == 0, result.stderr
        assert "SKIP-CLEAN" in result.stdout

    def test_skip_clean_names_every_missing_var(self):
        result = run_standup("--check")
        assert result.returncode == 0
        for var in RIG_ENV_VARS:
            assert var in result.stdout
        # The optional TestPyPI token gates nothing (nothing reads it).
        assert "LMER_REHEARSAL_TESTPYPI_TOKEN" not in result.stdout

    def test_skip_clean_points_at_rig_env_example(self):
        result = run_standup("--check")
        assert "rig.env.example" in result.stdout

    def test_partial_env_lists_only_missing_vars(self):
        result = run_standup("--check", LMER_REHEARSAL_GITHUB_TOKEN="dummy")
        assert result.returncode == 0
        assert "SKIP-CLEAN" in result.stdout
        assert "LMER_REHEARSAL_GITHUB_TOKEN" not in result.stdout
        assert "LMER_REHEARSAL_SIGNING_KEY" in result.stdout


class TestArgParsing:
    def test_unknown_flag_is_a_usage_error(self):
        result = run_standup("--bogus")
        assert result.returncode == 2
        assert "usage:" in result.stderr

    def test_flag_missing_value_is_a_usage_error(self):
        result = subprocess.run(
            [str(STAND_UP), "--repo"],
            capture_output=True,
            text=True,
            env=clean_env(),
            cwd=REPO_ROOT,
        )
        assert result.returncode == 2
        assert "usage:" in result.stderr

    def test_help_exits_zero_and_documents_modes(self):
        result = run_standup("--help")
        assert result.returncode == 0
        for flag in ("--check", "--teardown", "--dry-run", "--repo", "--env-file"):
            assert flag in result.stdout

    def test_lib_without_args_is_a_usage_error(self):
        result = subprocess.run(
            [str(LIB)], capture_output=True, text=True, env=clean_env()
        )
        assert result.returncode == 2
        assert "usage:" in result.stderr

    def test_lib_help(self):
        result = subprocess.run(
            [str(LIB), "--help"], capture_output=True, text=True, env=clean_env()
        )
        assert result.returncode == 0
        assert "--verify-evidence" in result.stdout


class TestEvidence:
    """Writer/parser/verifier round trip — entirely offline."""

    BASE_FIELDS = {
        "scenario": "negative-unsigned-tag",
        "rig_repo": "bot/lmer-rehearsal",
        "rig_project": "lmer-rehearsal",
        "tag": "v0.0.0-rc1",
        "tag_sha": "a" * 40,
        "workflow_run_id": "12345",
        "workflow_run_url": "https://github.com/bot/lmer-rehearsal/actions/runs/12345",
        "expected_conclusion": "failure",
        "recorded_conclusion": "failure",
        "failed_job": "verify-tag-signature",
        "published": "false",
        "derive_check": "pass",
    }

    def write_evidence(self, tmp_path, **overrides):
        fields = dict(self.BASE_FIELDS)
        fields.update(overrides)
        args = " ".join(
            f"{key}={shlex.quote(value)}"
            for key, value in fields.items()
            if value is not None
        )
        result = run_lib_fn(
            f"rehearsal_evidence_write {args}",
            REHEARSAL_EVIDENCE_DIR=str(tmp_path),
        )
        assert result.returncode == 0, result.stderr
        path = Path(result.stdout.strip())
        assert path.is_file()
        return path

    def verify(self, path):
        return subprocess.run(
            [str(LIB), "--verify-evidence", str(path)],
            capture_output=True,
            text=True,
            env=clean_env(),
        )

    def test_negative_round_trip(self, tmp_path):
        path = self.write_evidence(tmp_path)
        assert path.name.endswith("-negative-unsigned-tag.md")
        result = self.verify(path)
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_leg2_round_trip(self, tmp_path):
        path = self.write_evidence(
            tmp_path,
            scenario="leg2-dry-run",
            expected_conclusion="success",
            recorded_conclusion="success",
            failed_job=None,
            published="true",
        )
        result = self.verify(path)
        assert result.returncode == 0, result.stderr

    def test_parser_reads_fields_and_strips_comments(self, tmp_path):
        path = self.write_evidence(tmp_path)
        # Add a trailing comment the way the README's example does.
        text = path.read_text()
        text = text.replace(
            "expected_conclusion: failure",
            "expected_conclusion: failure      # negatives expect failure",
        )
        path.write_text(text)
        result = run_lib_fn(
            f"rehearsal_evidence_get {shlex.quote(str(path))} expected_conclusion"
        )
        assert result.stdout.strip() == "failure"
        result = run_lib_fn(f"rehearsal_evidence_get {shlex.quote(str(path))} tag")
        assert result.stdout.strip() == "v0.0.0-rc1"

    def test_writer_stamps_recorded_at(self, tmp_path):
        path = self.write_evidence(tmp_path)
        result = run_lib_fn(
            f"rehearsal_evidence_get {shlex.quote(str(path))} recorded_at"
        )
        import re

        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", result.stdout.strip()
        )

    def test_writer_rejects_unknown_field(self, tmp_path):
        result = run_lib_fn(
            "rehearsal_evidence_write scenario=leg2-dry-run bogus=1",
            REHEARSAL_EVIDENCE_DIR=str(tmp_path),
        )
        assert result.returncode != 0

    def test_verify_fails_on_conclusion_mismatch(self, tmp_path):
        path = self.write_evidence(tmp_path, recorded_conclusion="success")
        result = self.verify(path)
        assert result.returncode != 0
        assert "does not match" in result.stderr

    def test_verify_fails_when_failed_job_is_publish(self, tmp_path):
        path = self.write_evidence(tmp_path, failed_job="publish-pypi")
        result = self.verify(path)
        assert result.returncode != 0
        assert "precede" in result.stderr

    def test_verify_fails_when_failed_job_is_post_publish(self, tmp_path):
        path = self.write_evidence(tmp_path, failed_job="github-release")
        result = self.verify(path)
        assert result.returncode != 0

    def test_verify_fails_on_derive_check_fail(self, tmp_path):
        path = self.write_evidence(tmp_path, derive_check="fail")
        result = self.verify(path)
        assert result.returncode != 0
        assert "derive_check" in result.stderr

    def test_verify_fails_on_missing_required_field(self, tmp_path):
        path = self.write_evidence(tmp_path)
        text = "\n".join(
            line
            for line in path.read_text().splitlines()
            if not line.startswith("tag_sha:")
        )
        path.write_text(text + "\n")
        result = self.verify(path)
        assert result.returncode != 0
        assert "tag_sha" in result.stderr

    def test_verify_fails_on_malformed_sha(self, tmp_path):
        path = self.write_evidence(tmp_path, tag_sha="not-a-sha")
        result = self.verify(path)
        assert result.returncode != 0
        assert "hex" in result.stderr

    def test_verify_fails_on_unknown_scenario(self, tmp_path):
        path = self.write_evidence(tmp_path, scenario="totally-made-up")
        result = self.verify(path)
        assert result.returncode != 0
        assert "scenario" in result.stderr

    def test_verify_fails_when_negative_published_true(self, tmp_path):
        path = self.write_evidence(tmp_path, published="true")
        result = self.verify(path)
        assert result.returncode != 0
        assert "published" in result.stderr

    def test_verify_fails_on_missing_file(self):
        result = self.verify(Path("/nonexistent/evidence.md"))
        assert result.returncode != 0


class TestJobOrder:
    """The verifier's pre-publish check follows the production workflow."""

    def test_job_order_parsed_from_production_workflow(self):
        result = run_lib_fn("rehearsal_release_job_order")
        assert result.returncode == 0, result.stderr
        jobs = result.stdout.split()
        assert jobs[0] == "verify-tag-signature"
        assert "publish-pypi" in jobs
        assert jobs.index("verify-version") < jobs.index("publish-pypi")
        assert jobs.index("publish-pypi") < jobs.index("github-release")


class TestRigEnvExample:
    """rig.env.example documents the full parameter surface, no secrets."""

    def test_documents_every_parameter(self):
        text = (REHEARSAL_DIR / "rig.env.example").read_text()
        for var in (
            "LMER_REHEARSAL_REPO",
            "LMER_REHEARSAL_PROJECT",
            "LMER_REHEARSAL_ENVIRONMENT",
            # Optional (nothing reads it), but still part of the documented
            # parameter surface.
            "LMER_REHEARSAL_TESTPYPI_TOKEN",
            *RIG_ENV_VARS,
        ):
            assert var in text, f"{var} missing from rig.env.example"

    def test_token_values_are_empty(self):
        for line in (REHEARSAL_DIR / "rig.env.example").read_text().splitlines():
            if line.startswith("LMER_REHEARSAL_") and "TOKEN" in line:
                _, value = line.split("=", 1)
                assert value.strip('"') == "", "example must not carry secrets"
