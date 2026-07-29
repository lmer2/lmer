"""Tests for the rehearsal rig's leg-2 dry-run driver
(Ctl/rehearsal/run-leg2.sh).

Only the offline paths are exercised — this suite runs in sandboxes with
no rig env vars and no network: the production-target guard inherited
from lib.sh, the R14 skip-clean behavior of --full, the pending evidence
skeleton, the --verify-evidence pending/populated/malformed branches
(ordering receipts main-before-tag and GitHub-green-before-GitLab,
attestation presence, idempotency branch records, which-run-uploaded
consistency), and the idempotency-ladder derivation against recorded
state fixtures (tests/fixtures/rehearsal/leg2-state-*.json — invented
test data, clearly labeled, never evidence). Everything is driven through
subprocess so the script is tested exactly as invoked.
"""
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REHEARSAL_DIR = REPO_ROOT / "Ctl" / "rehearsal"
RUN_LEG2 = REHEARSAL_DIR / "run-leg2.sh"
PENDING_EVIDENCE = REPO_ROOT / "docs" / "rehearsal" / "evidence-leg2.md"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "rehearsal"

STATE_GREEN = FIXTURES / "leg2-state-green.json"
STATE_RED = FIXTURES / "leg2-state-red.json"
STATE_STALE_REFS = FIXTURES / "leg2-state-stale-refs.json"
STATE_NO_RUN = FIXTURES / "leg2-state-no-run.json"

# The rehearsal-only credential variables R14 skip-clean keys on. The
# TestPyPI token is deliberately absent: no rig script reads it (the
# tokenless JSON API does all TestPyPI verification), so it must not gate.
RIG_ENV_VARS = (
    "LMER_REHEARSAL_GITHUB_TOKEN",
    "LMER_REHEARSAL_SIGNING_KEY",
)

MERGE_SHA = "a" * 40
VERSION = "0.0.1753500000"
TAG = f"v{VERSION}"
FILES = (
    f"lmer_rehearsal-{VERSION}-py3-none-any.whl,"
    f"lmer_rehearsal-{VERSION}.tar.gz"
)


def clean_env(**extra):
    """Environment with every LMER_* var stripped (no rig, no production
    credentials), plus any explicit additions."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LMER_")}
    env.update(extra)
    return env


def run_leg2(*args, env=None, **extra_env):
    """Run run-leg2.sh with a clean environment; --env-file /dev/null so a
    developer's real rig.env can never leak into a test."""
    return subprocess.run(
        [str(RUN_LEG2), "--env-file", "/dev/null", *args],
        capture_output=True,
        text=True,
        env=env if env is not None else clean_env(**extra_env),
        cwd=REPO_ROOT,
    )


def populated_evidence_text(overrides=None):
    """A populated (status: complete) leg-2 evidence document in the
    agreed format — prose header + one fenced yaml block. overrides maps
    yaml keys to new values, or to None to drop the line entirely."""
    fields = {
        "status": "complete",
        "rig_repo": "bot/lmer-rehearsal",
        "rig_project": "lmer-rehearsal",
        "derive_check": "pass",
        "recorded_at": "2026-07-27T00:30:00Z",
        "merge_sha": MERGE_SHA,
        "version": VERSION,
        "tag": TAG,
        "tag_sha": MERGE_SHA,
        "tag_signature": "verified",
        "main_pushed_at": "2026-07-27T00:00:10Z",
        "tag_pushed_at": "2026-07-27T00:00:20Z",
        "workflow_run_id": "42",
        "workflow_run_url": "https://github.com/bot/lmer-rehearsal/actions/runs/42",
        "expected_conclusion": "success",
        "recorded_conclusion": "success",
        "actions_green_at": "2026-07-27T00:05:00Z",
        "testpypi_files_before": "(none)",
        "testpypi_files": FILES,
        "attestations": "present",
        "published": "true",
        "github_release": "present",
        "uploaded_by_run_id": "42",
        "gitlab_tag_push": "skipped-in-rig",
        "gitlab_tag_push_reason": (
            "rig topology has no GitLab-side mirror (test fixture value)"
        ),
        "idempotency_green.refs_current": "true",
        "idempotency_green.run_conclusion": "success",
        "idempotency_green.action": "skip",
        "idempotency_green.new_runs": "none",
        "idempotency_red.refs_current": "true",
        "idempotency_red.initial_conclusion": "cancelled",
        "idempotency_red.redispatch": "api-rerun",
        "idempotency_red.final_conclusion": "success",
        "idempotency_red.uploaded": "false",
        "idempotency_red.testpypi_files_after": FILES,
    }
    fields.update(overrides or {})
    lines = [
        "# Rehearsal evidence: leg-2 dry run",
        "",
        "Test-constructed populated evidence (unit test data).",
        "",
        "```yaml",
    ]
    lines += [f"{key}: {value}" for key, value in fields.items() if value is not None]
    lines.append("```")
    return "\n".join(lines) + "\n"


def write_populated(tmp_path, overrides=None):
    path = tmp_path / "evidence-leg2.md"
    path.write_text(populated_evidence_text(overrides))
    return path


def verify(path):
    return run_leg2("--verify-evidence", str(path))


class TestSyntax:
    def test_bash_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(RUN_LEG2)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    def test_script_executable(self):
        assert os.access(RUN_LEG2, os.X_OK), "run-leg2.sh must be executable"


class TestArgParsing:
    def test_no_mode_is_a_usage_error(self):
        result = run_leg2()
        assert result.returncode == 2
        assert "usage:" in result.stderr

    def test_unknown_flag_is_a_usage_error(self):
        result = run_leg2("--bogus")
        assert result.returncode == 2
        assert "usage:" in result.stderr

    def test_modes_are_mutually_exclusive(self):
        result = run_leg2("--full", "--verify-evidence", "x.md")
        assert result.returncode == 2
        assert "mutually exclusive" in result.stderr

    def test_flag_missing_value_is_a_usage_error(self):
        result = subprocess.run(
            [str(RUN_LEG2), "--verify-evidence"],
            capture_output=True,
            text=True,
            env=clean_env(),
            cwd=REPO_ROOT,
        )
        assert result.returncode == 2
        assert "usage:" in result.stderr

    def test_help_exits_zero_and_documents_modes(self):
        result = run_leg2("--help")
        assert result.returncode == 0
        for flag in (
            "--full",
            "--verify-evidence",
            "--write-pending",
            "--derive-action",
            "--dry-run",
        ):
            assert flag in result.stdout


class TestProductionTargetGuard:
    """The guard from lib.sh is inherited: production targets are refused
    offline, in every mode, before skip-clean and before any network."""

    def test_rejects_production_repo(self):
        result = run_leg2("--full", "--repo", "lmer2/lmer")
        assert result.returncode != 0
        assert "REFUSED" in result.stderr
        assert "SKIP-CLEAN" not in result.stdout

    def test_rejects_any_repo_named_lmer(self):
        result = run_leg2("--full", "--repo", "somebody/lmer")
        assert result.returncode != 0
        assert "REFUSED" in result.stderr

    def test_rejects_production_project(self):
        result = run_leg2(
            "--full", "--repo", "bot/lmer-rehearsal", "--project", "lmer"
        )
        assert result.returncode != 0
        assert "REFUSED" in result.stderr

    def test_rejects_production_credentials_even_in_offline_modes(self):
        result = run_leg2(
            "--verify-evidence",
            str(PENDING_EVIDENCE),
            LMER_RELEASE_SIGNING_KEY="/release-signing-key",
        )
        assert result.returncode != 0
        assert "REFUSED" in result.stderr

    def test_rejects_rehearsal_key_at_production_mount(self):
        result = run_leg2(
            "--full",
            "--repo",
            "bot/lmer-rehearsal",
            LMER_REHEARSAL_SIGNING_KEY="/release-signing-key",
        )
        assert result.returncode != 0
        assert "REFUSED" in result.stderr


class TestSkipClean:
    """R14: --full SKIP-CLEANs (exit 0, clear notice) when the rig env
    vars are absent — the rig walk cannot and does not run in a sandbox."""

    def test_full_skip_cleans_without_rig_env(self):
        result = run_leg2("--full")
        assert result.returncode == 0, result.stderr
        assert "SKIP-CLEAN" in result.stdout

    def test_skip_clean_names_every_missing_var(self):
        result = run_leg2("--full")
        assert result.returncode == 0
        for var in RIG_ENV_VARS:
            assert var in result.stdout

    def test_skip_clean_points_at_rig_env_example(self):
        result = run_leg2("--full")
        assert "rig.env.example" in result.stdout

    def test_partial_env_lists_only_missing_vars(self):
        result = run_leg2("--full", LMER_REHEARSAL_GITHUB_TOKEN="dummy")
        assert result.returncode == 0
        assert "SKIP-CLEAN" in result.stdout
        assert "LMER_REHEARSAL_GITHUB_TOKEN" not in result.stdout
        assert "LMER_REHEARSAL_SIGNING_KEY" in result.stdout
        # The optional TestPyPI token gates nothing (nothing reads it).
        assert "LMER_REHEARSAL_TESTPYPI_TOKEN" not in result.stdout


class TestDryRun:
    def test_dry_run_prints_the_sequence_without_network(self):
        result = run_leg2("--full", "--dry-run")
        assert result.returncode == 0, result.stderr
        assert "no network calls" in result.stdout
        # The spec's leg-2 sequence, in the plan.
        assert "main FIRST, then the tag" in result.stdout
        assert "attestations" in result.stdout
        assert "skipped-in-rig" in result.stdout
        assert "re-entry A" in result.stdout
        assert "re-entry B" in result.stdout


class TestDeriveAction:
    """--derive-action against the state fixtures: the idempotency ladder
    as pure, offline decision logic (the same function the live
    re-entries call)."""

    def test_refs_current_green_derives_skip(self):
        result = run_leg2("--derive-action", str(STATE_GREEN))
        assert result.returncode == 0, result.stderr
        assert "action: skip" in result.stdout

    def test_refs_current_red_derives_redispatch(self):
        result = run_leg2("--derive-action", str(STATE_RED))
        assert result.returncode == 0, result.stderr
        assert "action: redispatch" in result.stdout

    def test_stale_refs_derive_push(self):
        result = run_leg2("--derive-action", str(STATE_STALE_REFS))
        assert result.returncode == 0, result.stderr
        assert "action: push-refs" in result.stdout

    def test_refs_current_no_run_derives_wait(self):
        result = run_leg2("--derive-action", str(STATE_NO_RUN))
        assert result.returncode == 0, result.stderr
        assert "action: wait-for-run" in result.stdout

    def test_missing_file_fails(self):
        result = run_leg2("--derive-action", "/nonexistent/state.json")
        assert result.returncode != 0


class TestPendingEvidence:
    """The checked-in evidence file is an explicitly pending skeleton:
    no fabricated data, loud PENDING verify branch, format pinned to the
    script's own writer."""

    def test_checked_in_file_exists_and_is_pending(self):
        text = PENDING_EVIDENCE.read_text()
        assert "status: pending" in text
        assert "PENDING" in text
        assert "run-leg2.sh --full" in text

    def test_checked_in_file_carries_no_fake_data(self):
        # Every yaml value is TBD except the status marker and the
        # expected conclusion (a design expectation, not a recording).
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
            elif key == "expected_conclusion":
                assert value == "success"
            else:
                assert value == "TBD", f"pending skeleton has data in {key!r}"

    def test_checked_in_file_covers_both_idempotency_branches(self):
        text = PENDING_EVIDENCE.read_text()
        for key in (
            "idempotency_green.action:",
            "idempotency_green.new_runs:",
            "idempotency_red.redispatch:",
            "idempotency_red.uploaded:",
            "uploaded_by_run_id:",
            "gitlab_tag_push:",
            "attestations:",
        ):
            assert key in text

    def test_checked_in_file_matches_write_pending_output(self, tmp_path):
        # The skeleton format is pinned to the script's writer — drift
        # between the doc and the script fails here.
        regen = tmp_path / "regen.md"
        result = run_leg2("--write-pending", str(regen))
        assert result.returncode == 0, result.stderr
        assert regen.read_text() == PENDING_EVIDENCE.read_text()

    def test_verify_pending_exits_zero_with_loud_notice(self):
        result = verify(PENDING_EVIDENCE)
        assert result.returncode == 0, result.stderr
        assert "PENDING — leg-2 dry run has not run yet" in result.stdout

    def test_write_pending_refuses_to_clobber_recorded_evidence(self, tmp_path):
        path = write_populated(tmp_path)
        result = run_leg2("--write-pending", str(path))
        assert result.returncode != 0
        assert "refusing" in result.stderr
        assert "status: complete" in path.read_text()


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
        path = write_populated(tmp_path, {"workflow_run_url": "TBD"})
        result = verify(path)
        assert result.returncode != 0
        assert "TBD" in result.stderr

    def test_missing_field_fails(self, tmp_path):
        path = write_populated(tmp_path, {"idempotency_red.redispatch": None})
        result = verify(path)
        assert result.returncode != 0
        assert "idempotency_red.redispatch" in result.stderr

    def test_run_not_success_fails(self, tmp_path):
        path = write_populated(tmp_path, {"recorded_conclusion": "failure"})
        result = verify(path)
        assert result.returncode != 0
        assert "recorded_conclusion" in result.stderr

    def test_tag_not_at_merge_sha_fails(self, tmp_path):
        path = write_populated(tmp_path, {"tag_sha": "b" * 40})
        result = verify(path)
        assert result.returncode != 0
        assert "merge_sha" in result.stderr

    def test_tag_name_not_v_version_fails(self, tmp_path):
        path = write_populated(tmp_path, {"tag": "v9.9.9"})
        result = verify(path)
        assert result.returncode != 0
        assert "v<version>" in result.stderr

    def test_unverified_signature_fails(self, tmp_path):
        path = write_populated(tmp_path, {"tag_signature": "unverified"})
        result = verify(path)
        assert result.returncode != 0
        assert "tag_signature" in result.stderr

    def test_tag_pushed_before_main_fails(self, tmp_path):
        # The load-bearing ordering: main must land before the tag
        # triggers the workflow.
        path = write_populated(
            tmp_path, {"main_pushed_at": "2026-07-27T00:00:30Z"}
        )
        result = verify(path)
        assert result.returncode != 0
        assert "ordering violated" in result.stderr
        assert "main" in result.stderr

    def test_green_before_tag_push_fails(self, tmp_path):
        path = write_populated(
            tmp_path, {"actions_green_at": "2026-07-27T00:00:15Z"}
        )
        result = verify(path)
        assert result.returncode != 0
        assert "ordering violated" in result.stderr

    def test_missing_attestations_fails(self, tmp_path):
        path = write_populated(tmp_path, {"attestations": "absent"})
        result = verify(path)
        assert result.returncode != 0
        assert "attestations" in result.stderr

    def test_no_files_on_testpypi_fails(self, tmp_path):
        path = write_populated(tmp_path, {"testpypi_files": "(none)"})
        result = verify(path)
        assert result.returncode != 0
        assert "testpypi_files" in result.stderr

    def test_preexisting_listing_fails(self, tmp_path):
        # A non-empty before listing makes which-run-uploaded
        # unattributable.
        path = write_populated(tmp_path, {"testpypi_files_before": FILES})
        result = verify(path)
        assert result.returncode != 0
        assert "testpypi_files_before" in result.stderr

    def test_missing_github_release_fails(self, tmp_path):
        path = write_populated(tmp_path, {"github_release": "absent"})
        result = verify(path)
        assert result.returncode != 0
        assert "github_release" in result.stderr

    def test_published_false_fails(self, tmp_path):
        path = write_populated(tmp_path, {"published": "false"})
        result = verify(path)
        assert result.returncode != 0
        assert "published" in result.stderr

    def test_uploader_not_the_green_run_fails(self, tmp_path):
        # Which-run-uploaded consistency (the skip-existing drift caveat):
        # the recorded uploader must be the recorded green run.
        path = write_populated(tmp_path, {"uploaded_by_run_id": "43"})
        result = verify(path)
        assert result.returncode != 0
        assert "uploaded_by_run_id" in result.stderr

    def test_gitlab_skip_without_reason_fails(self, tmp_path):
        path = write_populated(tmp_path, {"gitlab_tag_push_reason": None})
        result = verify(path)
        assert result.returncode != 0
        assert "gitlab_tag_push_reason" in result.stderr

    def test_gitlab_unknown_state_fails(self, tmp_path):
        path = write_populated(tmp_path, {"gitlab_tag_push": "maybe"})
        result = verify(path)
        assert result.returncode != 0
        assert "gitlab_tag_push" in result.stderr

    def test_gitlab_pushed_without_timestamp_fails(self, tmp_path):
        # The hypothetical rig-with-GitLab branch: a recorded push must
        # carry its timestamp so GitHub-green-before-GitLab is checkable.
        path = write_populated(tmp_path, {"gitlab_tag_push": "pushed"})
        result = verify(path)
        assert result.returncode != 0
        assert "gitlab_tag_pushed_at" in result.stderr

    def test_gitlab_pushed_before_green_fails(self, tmp_path):
        path = write_populated(
            tmp_path,
            {
                "gitlab_tag_push": "pushed",
                "gitlab_tag_pushed_at": "2026-07-27T00:01:00Z",
            },
        )
        result = verify(path)
        assert result.returncode != 0
        assert "ordering violated" in result.stderr
        assert "GitLab" in result.stderr

    def test_gitlab_pushed_after_green_passes(self, tmp_path):
        path = write_populated(
            tmp_path,
            {
                "gitlab_tag_push": "pushed",
                "gitlab_tag_pushed_at": "2026-07-27T00:06:00Z",
            },
        )
        result = verify(path)
        assert result.returncode == 0, result.stderr

    def test_green_branch_wrong_action_fails(self, tmp_path):
        path = write_populated(
            tmp_path, {"idempotency_green.action": "redispatch"}
        )
        result = verify(path)
        assert result.returncode != 0
        assert "idempotency_green.action" in result.stderr

    def test_green_branch_new_run_fails(self, tmp_path):
        path = write_populated(tmp_path, {"idempotency_green.new_runs": "1"})
        result = verify(path)
        assert result.returncode != 0
        assert "idempotency_green.new_runs" in result.stderr

    def test_green_branch_refs_stale_fails(self, tmp_path):
        path = write_populated(
            tmp_path, {"idempotency_green.refs_current": "false"}
        )
        result = verify(path)
        assert result.returncode != 0
        assert "idempotency_green.refs_current" in result.stderr

    def test_red_branch_initial_success_fails(self, tmp_path):
        # A "red" branch whose initial conclusion was green never
        # exercised the re-dispatch.
        path = write_populated(
            tmp_path, {"idempotency_red.initial_conclusion": "success"}
        )
        result = verify(path)
        assert result.returncode != 0
        assert "idempotency_red.initial_conclusion" in result.stderr

    def test_red_branch_retag_redispatch_fails(self, tmp_path):
        path = write_populated(tmp_path, {"idempotency_red.redispatch": "re-tag"})
        result = verify(path)
        assert result.returncode != 0
        assert "idempotency_red.redispatch" in result.stderr

    def test_red_branch_did_not_converge_fails(self, tmp_path):
        path = write_populated(
            tmp_path, {"idempotency_red.final_conclusion": "failure"}
        )
        result = verify(path)
        assert result.returncode != 0
        assert "idempotency_red.final_conclusion" in result.stderr

    def test_red_branch_uploaded_true_fails(self, tmp_path):
        path = write_populated(tmp_path, {"idempotency_red.uploaded": "true"})
        result = verify(path)
        assert result.returncode != 0
        assert "skip-existing" in result.stderr

    def test_red_branch_listing_changed_fails(self, tmp_path):
        path = write_populated(
            tmp_path,
            {"idempotency_red.testpypi_files_after": FILES + ",extra.whl"},
        )
        result = verify(path)
        assert result.returncode != 0
        assert "listing" in result.stderr

    def test_malformed_sha_fails(self, tmp_path):
        path = write_populated(
            tmp_path, {"merge_sha": "not-a-sha", "tag_sha": "not-a-sha"}
        )
        result = verify(path)
        assert result.returncode != 0
        assert "hex" in result.stderr

    def test_malformed_timestamp_fails(self, tmp_path):
        path = write_populated(tmp_path, {"main_pushed_at": "yesterday"})
        result = verify(path)
        assert result.returncode != 0
        assert "ISO-8601" in result.stderr

    def test_derive_check_fail_fails(self, tmp_path):
        path = write_populated(tmp_path, {"derive_check": "fail"})
        result = verify(path)
        assert result.returncode != 0
        assert "derive_check" in result.stderr

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
