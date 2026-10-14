"""Tests for `gate-check --baseline` — reusing CI for the initial baseline (#452)."""

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import urllib.parse
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest
import yaml

from lmer_cli import ci_baseline, gate_cache, gates
from lmer_cli.ci_baseline import GitLabEvidence, SmokeResult
from lmer_cli.gates import (
    BASELINE_CI_FAILED,
    BASELINE_FALLBACK,
    BASELINE_GATE,
    BASELINE_LOCAL_FAILED,
    BASELINE_REUSED,
    CI_BASELINE_SCOPE,
    BaselineOutcome,
    CheckResult,
    CheckStatus,
    GateSystem,
)
from lmer_cli.gitlab_pipeline import GitLabPipelineClient, TokenNotFoundError
from tests.conftest import strip_lmer_env
from tests.test_ci_baseline import CI_YAML, PINS, FakeClient, config_routes
from tests.test_lmer_env_file import REPO_URL, _run_main

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_RUN_SMOKE = ci_baseline.run_smoke
HOST = "gitlab.example.com"
PROJECT = "group/project"
PROJECT_ID = 414
DEVELOPER_TOKEN = "developer-run-token-582"
EVIDENCE_TOKEN = "maintainer-read-api-token-582"
EVIDENCE_ENV = ci_baseline.evidence_token_env(HOST)
MAPPING_YAML = (
    "ci_baseline:\n"
    "  provider: gitlab\n"
    "  jobs:\n"
    "    pre-commit:\n"
    "      covers: lint\n"
    f"      definition: \"{PINS['pre-commit']}\"\n"
    "    pytest:\n"
    "      covers: tests\n"
    f"      definition: \"{PINS['pytest']}\"\n"
    "  smoke:\n"
    "    timeout: 60\n"
)
CHEAP_CHECKS = (
    "check_git_status", "check_staged_files", "check_branch", "check_secrets",
    "check_permissions", "check_deliverable_formats", "check_changelog",
    "check_documentation", "check_code_quality", "check_precommit",
)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A clean checkout of group/project with a work-repo coverage mapping."""
    strip_lmer_env(monkeypatch)
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
    _git(root, "init", "-q", "-b", "feature/x")
    _git(root, "-c", "user.email=t@example.com", "-c", "user.name=t",
         "add", "tests/test_x.py")
    _git(root, "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "-q", "-m", "init")
    _git(root, "remote", "add", "origin", f"https://{HOST}/{PROJECT}.git")

    work = tmp_path / "work"
    info = work / HOST / PROJECT / "info"
    info.mkdir(parents=True)
    (info / "gate-check.yaml").write_text(MAPPING_YAML)
    monkeypatch.setenv("LMER_WORK_REPO_PATH", str(work))
    monkeypatch.setenv("LMER_REPO_HOST", HOST)
    monkeypatch.setenv("LMER_REPO_PROJECT", PROJECT)
    monkeypatch.setenv("LMER_REPO_URL", f"https://{HOST}/{PROJECT}.git")
    monkeypatch.setenv("LMER_GATE_CI_BASELINE", "1")
    monkeypatch.setenv(gate_cache.CACHE_DIR_ENV, str(tmp_path / "gate-cache"))
    monkeypatch.delenv(gate_cache.DISABLE_ENV, raising=False)
    return root


def _sha(root):
    return _git(root, "rev-parse", "HEAD")


def _job(job_id, name, status, sha, pipeline_id=10, allow_failure=False):
    return {
        "id": job_id, "name": name, "status": status,
        "allow_failure": allow_failure,
        "finished_at": f"2026-10-10T05:00:{job_id:02d}Z",
        "web_url": f"https://{HOST}/{PROJECT}/-/jobs/{job_id}",
        "pipeline": {"id": pipeline_id, "sha": sha, "project_id": PROJECT_ID},
    }


def _client(sha, jobs, pipeline_status="success", pipeline_sha=None, ci_yaml=CI_YAML):
    pipeline_sha = pipeline_sha or sha
    return FakeClient({
        f"/projects/{urllib.parse.quote(PROJECT, safe='')}": {
            "id": PROJECT_ID, "path_with_namespace": PROJECT},
        f"/projects/{PROJECT_ID}/pipelines": [{
            "id": 10, "sha": pipeline_sha, "status": pipeline_status,
            "project_id": PROJECT_ID, "source": "merge_request_event",
            "ref": "refs/merge-requests/1/head",
            "web_url": f"https://{HOST}/{PROJECT}/-/pipelines/10"}],
        f"/projects/{PROJECT_ID}/pipelines/10/jobs": jobs,
        **config_routes([10], ci_yaml),
    })


class Harness:
    """A GateSystem whose forge, smoke check and cheap checks are scripted."""

    def __init__(self, root, monkeypatch, client=None,
                 smoke=SmokeResult(ci_baseline.SMOKE_PASS, 1.0, "collected: 1 test", "python3"),
                 cheap_status=CheckStatus.PASSED, real_checks=()):
        self.gate = GateSystem()
        self.gate.project_root = root
        self.client = client
        self.source_calls = 0
        self.smoke_calls = []
        self.suite_calls = 0
        self.commit_gate_calls = []

        def source(_host):
            self.source_calls += 1
            return GitLabEvidence(self.client)

        def run_smoke(*args, **kwargs):
            self.smoke_calls.append((args, kwargs))
            return smoke

        def suite():
            self.suite_calls += 1
            raise AssertionError("the local suite must not run on a reused baseline")

        monkeypatch.setattr(self.gate, "_baseline_source", source)
        monkeypatch.setattr(ci_baseline, "run_smoke", run_smoke)
        monkeypatch.setattr(self.gate, "check_tests", suite)
        for name in CHEAP_CHECKS:
            if name in real_checks:
                continue
            monkeypatch.setattr(
                self.gate, name,
                lambda name=name: CheckResult(name=name, status=cheap_status))

        real_commit_gate = self.gate.run_commit_gate

        def commit_gate(*args, **kwargs):
            self.commit_gate_calls.append(kwargs)
            return real_commit_gate(*args, **kwargs)

        monkeypatch.setattr(self.gate, "run_commit_gate", commit_gate)

    def run(self):
        return self.gate.run_baseline_gate()


def passing_jobs(sha):
    return [_job(1, "pre-commit", "success", sha), _job(2, "pytest", "success", sha)]


class TestOutcomes:
    def test_passed_ci_reuses_the_baseline_without_the_suite(self, repo, monkeypatch):
        harness = Harness(repo, monkeypatch, _client(_sha(repo), passing_jobs(_sha(repo))))
        outcome = harness.run()
        assert outcome.kind == BASELINE_REUSED
        assert outcome.exit_code == 0
        assert harness.suite_calls == 0
        assert len(harness.smoke_calls) == 1
        tests = [r for r in harness.gate.results if r.name == "Python Tests"]
        assert len(tests) == 1
        assert tests[0].scope == CI_BASELINE_SCOPE
        assert "not run locally" in tests[0].message
        assert {r.name for r in harness.gate.results} >= set(CHEAP_CHECKS)

    def test_red_pipeline_from_unrelated_publish_still_reuses(self, repo, monkeypatch):
        sha = _sha(repo)
        jobs = passing_jobs(sha) + [_job(3, "publish-container", "failed", sha)]
        harness = Harness(repo, monkeypatch, _client(sha, jobs, pipeline_status="failed"))
        assert harness.run().kind == BASELINE_REUSED

    @pytest.mark.parametrize("jobs", [
        lambda sha: [_job(1, "pre-commit", "failed", sha), _job(2, "pytest", "success", sha)],
        lambda sha: [_job(1, "pre-commit", "success", sha), _job(2, "pytest", "failed", sha)],
        lambda sha: [_job(1, "pre-commit", "success", sha),
                     _job(2, "pytest", "failed", sha, allow_failure=True)],
    ])
    def test_failed_lint_or_tests_stop_without_local_rerun(self, repo, monkeypatch, jobs):
        harness = Harness(repo, monkeypatch, _client(_sha(repo), jobs(_sha(repo))))
        outcome = harness.run()
        assert outcome.kind == BASELINE_CI_FAILED
        assert outcome.exit_code == ci_baseline.EXIT_CI_FAILED
        assert harness.suite_calls == 0
        assert harness.commit_gate_calls == []
        assert harness.smoke_calls == []

    def test_retry_that_passed_reuses_and_records_both_attempts(self, repo, monkeypatch):
        sha = _sha(repo)
        jobs = [_job(1, "pre-commit", "success", sha), _job(2, "pytest", "failed", sha),
                _job(3, "pytest", "success", sha)]
        harness = Harness(repo, monkeypatch, _client(sha, jobs))
        outcome = harness.run()
        assert outcome.kind == BASELINE_REUSED
        pytest_job = next(j for j in outcome.payload()["evidence"]["jobs"]
                          if j["name"] == "pytest")
        assert [a["status"] for a in pytest_job["attempts"]] == ["failed", "success"]
        assert pytest_job["deciding_attempt"]["job_id"] == 3

    @pytest.mark.parametrize("jobs", [
        lambda sha: [],
        lambda sha: [_job(1, "pre-commit", "success", sha)],
        lambda sha: [_job(1, "pre-commit", "success", sha), _job(2, "pytest", "running", sha)],
        lambda sha: [_job(1, "pre-commit", "success", sha), _job(2, "pytest", "canceled", sha)],
        lambda sha: [_job(1, "pre-commit", "success", sha), _job(2, "pytest", "skipped", sha)],
        lambda sha: [_job(1, "pre-commit", "manual", sha), _job(2, "pytest", "success", sha)],
        lambda sha: [_job(1, "pre-commit", "success", sha), _job(2, "pytest 1/2", "success", sha)],
    ])
    def test_unverified_evidence_falls_back_to_the_full_gate(self, repo, monkeypatch, jobs):
        harness = Harness(repo, monkeypatch, _client(_sha(repo), jobs(_sha(repo))))
        outcome = harness.run()
        assert outcome.kind == BASELINE_FALLBACK
        assert harness.commit_gate_calls == []
        assert harness.smoke_calls == []
        assert outcome.evidence is not None

    def test_pipeline_at_another_sha_falls_back(self, repo, monkeypatch):
        other = "f" * 40
        harness = Harness(repo, monkeypatch,
                          _client(_sha(repo), passing_jobs(other), pipeline_sha=other))
        outcome = harness.run()
        assert outcome.kind == BASELINE_FALLBACK
        assert "no CI pipeline" in outcome.reason

    def test_api_failure_falls_back(self, repo, monkeypatch):
        client = _client(_sha(repo), passing_jobs(_sha(repo)))
        client.failures[f"/projects/{PROJECT_ID}/pipelines"] = (502, "")
        outcome = Harness(repo, monkeypatch, client).run()
        assert outcome.kind == BASELINE_FALLBACK
        assert "502" in outcome.reason

    def test_an_error_in_the_evaluation_falls_back(self, repo, monkeypatch):
        harness = Harness(repo, monkeypatch)

        def boom(_host):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(harness.gate, "_baseline_source", boom)
        outcome = harness.run()
        assert outcome.kind == BASELINE_FALLBACK
        assert "baseline evaluation failed" in outcome.reason

    def test_missing_token_falls_back(self, repo, monkeypatch):
        harness = Harness(repo, monkeypatch)

        def no_token(_host):
            raise TokenNotFoundError("GITLAB_TOKEN_gitlab_example_com")

        monkeypatch.setattr(harness.gate, "_baseline_source", no_token)
        outcome = harness.run()
        assert outcome.kind == BASELINE_FALLBACK
        assert "no GitLab API access" in outcome.reason


class RoleForge(FakeClient):
    """GitLab's rule for the pipeline-variables read: 403 below Maintainer."""

    def __init__(self, host, token, routes, maintainers):
        super().__init__(routes)
        self.host, self.token, self.maintainers = host, token, maintainers

    def request(self, endpoint, accept="application/json"):
        if endpoint.split("?")[0].endswith("/variables") and self.token not in self.maintainers:
            self.requests.append(endpoint)
            return 403, '{"message":"403 Forbidden"}'
        return super().request(endpoint, accept)


class TestEvidenceToken:
    """#582: a Developer run token plus a Maintainer read_api evidence token."""

    @pytest.fixture
    def forge(self, repo, monkeypatch):
        sha = _sha(repo)
        routes = _client(sha, passing_jobs(sha)).routes
        self.maintainers = {EVIDENCE_TOKEN}
        self.clients = []

        def client(host, token):
            made = RoleForge(host, token, routes, self.maintainers)
            self.clients.append(made)
            return made

        monkeypatch.setattr(gates, "GitLabPipelineClient", client)
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        monkeypatch.setenv("GITLAB_TOKEN_gitlab_example_com", DEVELOPER_TOKEN)
        harness = Harness(repo, monkeypatch)
        monkeypatch.delattr(harness.gate, "_baseline_source")
        return harness

    def test_developer_run_token_falls_back_with_the_specific_reason(self, forge, capsys):
        outcome = forge.run()
        assert outcome.kind == BASELINE_FALLBACK
        assert outcome.reason == (
            "cannot read pipeline variables: GitLab refused the run token (403); "
            f"it needs the Maintainer role, or set {EVIDENCE_ENV} to a Maintainer "
            "read_api token (see docs/GATE-CI-BASELINE.md)")
        out = capsys.readouterr().out
        assert "Reading CI evidence with the run token" in out
        assert f"CI baseline not used: {outcome.reason}" in out

    def test_evidence_token_reuses_with_a_developer_run_token(self, forge, monkeypatch, capsys):
        monkeypatch.setenv(EVIDENCE_ENV, EVIDENCE_TOKEN)
        outcome = forge.run()
        assert outcome.kind == BASELINE_REUSED, outcome.reason
        assert [(client.host, client.token) for client in self.clients] == [
            (HOST, EVIDENCE_TOKEN)]
        receipt = forge.gate.baseline_receipt(outcome)
        assert receipt["ci_baseline"]["evidence"]["credential"] == EVIDENCE_ENV
        assert EVIDENCE_TOKEN not in json.dumps(receipt)
        out = capsys.readouterr().out
        assert f"Reading CI evidence with {EVIDENCE_ENV}" in out
        assert EVIDENCE_TOKEN not in out

    def test_evidence_token_below_maintainer_is_named(self, forge, monkeypatch):
        self.maintainers.clear()
        monkeypatch.setenv(EVIDENCE_ENV, EVIDENCE_TOKEN)
        outcome = forge.run()
        assert outcome.kind == BASELINE_FALLBACK
        assert outcome.reason == (
            f"cannot read pipeline variables: GitLab refused {EVIDENCE_ENV} (403); "
            "it needs the Maintainer role (see docs/GATE-CI-BASELINE.md)")

    def test_print_pins_reads_with_the_evidence_token(self, forge, repo, monkeypatch, capsys):
        monkeypatch.setenv(EVIDENCE_ENV, EVIDENCE_TOKEN)
        monkeypatch.chdir(repo)
        module = _load_gate_check()
        monkeypatch.setattr("sys.argv", ["gate-check", "--print-pins"])
        assert module.main() == 0
        assert [client.token for client in self.clients] == [EVIDENCE_TOKEN]
        assert f'definition: "{PINS["pytest"]}"' in capsys.readouterr().out

    def test_evidence_token_needs_no_run_token(self, forge, monkeypatch):
        monkeypatch.delenv("GITLAB_TOKEN_gitlab_example_com")
        monkeypatch.setenv(EVIDENCE_ENV, EVIDENCE_TOKEN)
        assert forge.run().kind == BASELINE_REUSED

    @pytest.mark.parametrize("run_token, evidence_token, credential", [
        ("glpat-SECRETMARK\rX", None, "the run token"),
        (DEVELOPER_TOKEN, "glpat-SECRETMARK\nX", EVIDENCE_ENV),
        (DEVELOPER_TOKEN, "glpat-SECRETMARK\u200b", EVIDENCE_ENV),
    ])
    def test_a_token_that_is_not_header_text_is_never_shown(
            self, forge, monkeypatch, capsys, run_token, evidence_token, credential):
        """http.client quotes a token it cannot send in its exception (#582)."""
        monkeypatch.setattr(gates, "GitLabPipelineClient", GitLabPipelineClient)
        monkeypatch.setenv("GITLAB_TOKEN_gitlab_example_com", run_token)
        if evidence_token:
            monkeypatch.setenv(EVIDENCE_ENV, evidence_token)
        outcome = forge.run()
        assert outcome.kind == BASELINE_FALLBACK
        assert outcome.reason == (f"no GitLab API access: {credential} is not a valid token "
                                  "(control or non-latin-1 characters)")
        shown = capsys.readouterr().out + json.dumps(forge.gate.baseline_receipt(outcome))
        assert "SECRETMARK" not in shown

    @pytest.mark.parametrize("name, value", [
        (EVIDENCE_ENV, "   "),
        ("LMER_CI_BASELINE_TOKEN_git_other_example_com", EVIDENCE_TOKEN),
    ])
    def test_blank_or_other_host_token_leaves_the_run_token(self, forge, monkeypatch,
                                                           name, value):
        monkeypatch.setenv(name, value)
        outcome = forge.run()
        assert outcome.kind == BASELINE_FALLBACK
        assert [client.token for client in self.clients] == [DEVELOPER_TOKEN]
        assert "refused the run token" in outcome.reason


class FlushRecorder(io.StringIO):
    """stdout that remembers what had been written at every flush."""

    def __init__(self):
        super().__init__()
        self.flushed = []

    def flush(self):
        self.flushed.append(self.getvalue())
        super().flush()


class TestDecisionIsFlushed:
    """#582: a redirected gate-check is block-buffered, so an unflushed
    decision line only appears when the hour-long full gate exits."""

    def test_fallback_reason_is_flushed_when_decided(self, repo, monkeypatch):
        stdout = FlushRecorder()
        monkeypatch.setattr(sys, "stdout", stdout)
        outcome = Harness(repo, monkeypatch, _client(_sha(repo), [])).run()
        assert outcome.kind == BASELINE_FALLBACK
        assert any(f"CI baseline not used: {outcome.reason}" in seen
                   and "Running the full local gate instead." in seen
                   for seen in stdout.flushed)

    def test_evidence_is_flushed_before_the_smoke_check(self, repo, monkeypatch):
        stdout = FlushRecorder()
        monkeypatch.setattr(sys, "stdout", stdout)
        harness = Harness(repo, monkeypatch, _client(_sha(repo), passing_jobs(_sha(repo))))
        seen_at_smoke = []

        def run_smoke(*_args, **_kwargs):
            seen_at_smoke.append(stdout.flushed[-1] if stdout.flushed else "")
            return SmokeResult(ci_baseline.SMOKE_PASS, 1.0, "collected: 1 test", "python3")

        monkeypatch.setattr(ci_baseline, "run_smoke", run_smoke)
        assert harness.run().kind == BASELINE_REUSED
        assert "Reading CI evidence with the run token" in seen_at_smoke[0]
        assert f"CI evidence for {PROJECT} @ {_sha(repo)}: passed" in seen_at_smoke[0]
        assert "pytest [tests]: passed" in seen_at_smoke[0]

    def test_ci_failure_is_flushed_when_decided(self, repo, monkeypatch):
        stdout = FlushRecorder()
        monkeypatch.setattr(sys, "stdout", stdout)
        sha = _sha(repo)
        jobs = [_job(1, "pre-commit", "success", sha), _job(2, "pytest", "failed", sha)]
        assert Harness(repo, monkeypatch, _client(sha, jobs)).run().kind == BASELINE_CI_FAILED
        assert "the local suite was not run" in stdout.flushed[-1]


class TestPreconditions:
    def test_flag_off_falls_back_without_asking_the_forge(self, repo, monkeypatch):
        monkeypatch.setenv("LMER_GATE_CI_BASELINE", "0")
        harness = Harness(repo, monkeypatch, _client(_sha(repo), passing_jobs(_sha(repo))))
        outcome = harness.run()
        assert outcome.kind == BASELINE_FALLBACK
        assert "LMER_GATE_CI_BASELINE" in outcome.reason
        assert harness.source_calls == 0

    def test_flag_unset_falls_back(self, repo, monkeypatch):
        monkeypatch.delenv("LMER_GATE_CI_BASELINE")
        harness = Harness(repo, monkeypatch, _client(_sha(repo), passing_jobs(_sha(repo))))
        assert harness.run().kind == BASELINE_FALLBACK
        assert harness.source_calls == 0

    def test_no_mapping_falls_back(self, repo, monkeypatch, tmp_path):
        (tmp_path / "work" / HOST / PROJECT / "info" / "gate-check.yaml").unlink()
        harness = Harness(repo, monkeypatch, _client(_sha(repo), passing_jobs(_sha(repo))))
        outcome = harness.run()
        assert outcome.kind == BASELINE_FALLBACK
        assert "no ci_baseline" in outcome.reason
        assert harness.source_calls == 0

    def test_repo_local_mapping_is_ignored(self, repo, monkeypatch, tmp_path):
        (tmp_path / "work" / HOST / PROJECT / "info" / "gate-check.yaml").unlink()
        (repo / ".lmer").mkdir()
        (repo / ".lmer" / "gate-check.yaml").write_text(MAPPING_YAML)
        _git(repo, "add", ".lmer/gate-check.yaml")
        _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t",
             "commit", "-q", "-m", "repo declares its own coverage")
        harness = Harness(repo, monkeypatch, _client(_sha(repo), passing_jobs(_sha(repo))))
        outcome = harness.run()
        assert outcome.kind == BASELINE_FALLBACK
        assert harness.source_calls == 0

    def test_modified_file_falls_back(self, repo, monkeypatch):
        (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert 0\n")
        harness = Harness(repo, monkeypatch, _client(_sha(repo), passing_jobs(_sha(repo))))
        outcome = harness.run()
        assert outcome.kind == BASELINE_FALLBACK
        assert "not clean" in outcome.reason
        assert harness.source_calls == 0

    def test_untracked_file_falls_back(self, repo, monkeypatch):
        (repo / "tests" / "test_new.py").write_text("def test_new():\n    pass\n")
        harness = Harness(repo, monkeypatch, _client(_sha(repo), passing_jobs(_sha(repo))))
        assert harness.run().kind == BASELINE_FALLBACK

    def test_origin_of_another_project_falls_back(self, repo, monkeypatch):
        _git(repo, "remote", "set-url", "origin", f"https://{HOST}/someone/fork.git")
        harness = Harness(repo, monkeypatch, _client(_sha(repo), passing_jobs(_sha(repo))))
        outcome = harness.run()
        assert outcome.kind == BASELINE_FALLBACK
        assert "not the repository" in outcome.reason
        assert harness.source_calls == 0

    def test_project_runner_falls_back(self, repo, monkeypatch, tmp_path):
        runner = tmp_path / "work" / HOST / PROJECT / "info" / "gate-check-run-tests.sh"
        runner.write_text("#!/bin/sh\nexit 0\n")
        runner.chmod(0o755)
        harness = Harness(repo, monkeypatch, _client(_sha(repo), passing_jobs(_sha(repo))))
        outcome = harness.run()
        assert outcome.kind == BASELINE_FALLBACK
        assert "own runner" in outcome.reason


class TestSmoke:
    def test_smoke_timeout_falls_back(self, repo, monkeypatch):
        harness = Harness(repo, monkeypatch, _client(_sha(repo), passing_jobs(_sha(repo))),
                          smoke=SmokeResult(ci_baseline.SMOKE_TIMEOUT, 60.0, "too slow"))
        outcome = harness.run()
        assert outcome.kind == BASELINE_FALLBACK
        assert outcome.payload()["smoke"]["outcome"] == ci_baseline.SMOKE_TIMEOUT
        assert harness.commit_gate_calls == []

    def test_smoke_failure_is_an_environment_failure(self, repo, monkeypatch):
        harness = Harness(repo, monkeypatch, _client(_sha(repo), passing_jobs(_sha(repo))),
                          smoke=SmokeResult(ci_baseline.SMOKE_FAIL, 2.0, "no pytest"))
        outcome = harness.run()
        assert outcome.kind == BASELINE_LOCAL_FAILED
        assert outcome.exit_code == 1
        assert "smoke check failed" in outcome.reason
        assert harness.commit_gate_calls == []

    def test_smoke_is_bounded_by_the_mapping_timeout(self, repo, monkeypatch):
        harness = Harness(repo, monkeypatch, _client(_sha(repo), passing_jobs(_sha(repo))))
        harness.run()
        args, _ = harness.smoke_calls[0]
        _candidates, collect_argv, _tools, timeout = args[:4]
        assert timeout == 60
        assert "--collect-only" in collect_argv

    def test_real_smoke_collects_the_checkout(self, repo, monkeypatch):
        """The unscripted smoke check against a real checkout and interpreter,
        followed by the real git-status check: collection must leave no
        __pycache__ or .pytest_cache behind in a repo that does not ignore
        them, or the baseline fails its own clean-tree check."""
        harness = Harness(repo, monkeypatch, _client(_sha(repo), passing_jobs(_sha(repo))),
                          real_checks=("check_git_status",))
        monkeypatch.setattr(ci_baseline, "run_smoke", REAL_RUN_SMOKE)
        venv = repo / ".venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").symlink_to(sys.executable)
        (repo / ".git" / "info" / "exclude").write_text(".venv/\n")
        outcome = harness.run()
        assert outcome.smoke is not None
        assert outcome.smoke.outcome == ci_baseline.SMOKE_PASS, outcome.smoke.detail
        assert outcome.kind == BASELINE_REUSED, outcome.reason
        assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""


class TestLocalChecks:
    def test_cheap_check_failure_fails_the_baseline(self, repo, monkeypatch):
        harness = Harness(repo, monkeypatch, _client(_sha(repo), passing_jobs(_sha(repo))),
                          cheap_status=CheckStatus.FAILED)
        outcome = harness.run()
        assert outcome.kind == BASELINE_LOCAL_FAILED
        assert outcome.exit_code == 1
        assert harness.suite_calls == 0


class TestReceiptAndCache:
    def test_reused_receipt_carries_provenance(self, repo, monkeypatch):
        sha = _sha(repo)
        harness = Harness(repo, monkeypatch, _client(sha, passing_jobs(sha)))
        outcome = harness.run()
        receipt = harness.gate.baseline_receipt(outcome)
        assert receipt["test_scope"] == CI_BASELINE_SCOPE
        assert receipt["test_cache_verdict"] == "not-applicable"
        assert "full suite" not in receipt["summary"]
        payload = receipt["ci_baseline"]
        assert payload["decision"] == BASELINE_REUSED
        evidence = payload["evidence"]
        assert evidence["project"] == PROJECT
        assert evidence["project_id"] == PROJECT_ID
        assert evidence["sha"] == sha and len(sha) == 40
        assert evidence["pipelines"][0]["id"] == 10
        assert evidence["pipelines"][0]["web_url"].endswith("/pipelines/10")
        assert evidence["completed_at"] == "2026-10-10T05:00:02Z"
        jobs = {job["name"]: job for job in evidence["jobs"]}
        assert jobs["pytest"]["deciding_attempt"]["web_url"].endswith("/jobs/2")
        assert jobs["pre-commit"]["covers"] == "lint"
        assert payload["smoke"]["outcome"] == ci_baseline.SMOKE_PASS

    @pytest.mark.parametrize("kind_jobs, expected", [
        (lambda sha: [_job(1, "pre-commit", "failed", sha), _job(2, "pytest", "success", sha)],
         BASELINE_CI_FAILED),
        (lambda sha: [], BASELINE_FALLBACK),
    ])
    def test_non_reused_receipts_never_claim_a_test_scope(self, repo, monkeypatch,
                                                           kind_jobs, expected):
        harness = Harness(repo, monkeypatch, _client(_sha(repo), kind_jobs(_sha(repo))))
        outcome = harness.run()
        assert outcome.kind == expected
        receipt = harness.gate.baseline_receipt(outcome)
        assert "test_scope" not in receipt
        assert receipt["ci_baseline"]["decision"] == expected

    def test_reused_baseline_writes_no_test_cache_entry(self, repo, monkeypatch, tmp_path):
        def no_cache_write(*_args, **_kwargs):
            raise AssertionError("a CI baseline must never record a test-cache pass")

        monkeypatch.setattr(gate_cache, "record_pass", no_cache_write)
        harness = Harness(repo, monkeypatch, _client(_sha(repo), passing_jobs(_sha(repo))))
        assert harness.run().kind == BASELINE_REUSED
        cache_dir = tmp_path / "gate-cache"
        entries = list(cache_dir.glob(f"*{gate_cache.ENTRY_SUFFIX}")) if cache_dir.exists() else []
        assert entries == []

    def test_a_later_gate_still_runs_the_suite(self, repo, monkeypatch):
        harness = Harness(repo, monkeypatch, _client(_sha(repo), passing_jobs(_sha(repo))))
        assert harness.run().kind == BASELINE_REUSED

        later = GateSystem()
        later.project_root = repo
        ran = []
        for name in CHEAP_CHECKS:
            monkeypatch.setattr(later, name,
                                lambda name=name: CheckResult(name=name, status=CheckStatus.PASSED))
        monkeypatch.setattr(later, "check_tests", lambda: ran.append(True) or CheckResult(
            name="Python Tests", status=CheckStatus.PASSED))
        later.run_commit_gate()
        assert ran == [True]


def _load_gate_check():
    script = REPO_ROOT / "bin" / "gate-check"
    loader = SourceFileLoader("_gate_check_script", str(script))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class TestGateCheckCommand:
    @pytest.fixture
    def script(self, monkeypatch):
        module = _load_gate_check()
        self.events = []
        self.full_gate_runs = 0

        def emit(gate, outcome, **kwargs):
            self.events.append((gate, outcome, kwargs))

        def full_gate(**_kwargs):
            self.full_gate_runs += 1
            return 0

        monkeypatch.setattr(module, "emit_gate_event", emit)
        monkeypatch.setattr(module, "commit_gate", full_gate)
        return module

    def _outcome(self, monkeypatch, kind, exit_code=0):
        monkeypatch.setattr(GateSystem, "run_baseline_gate",
                            lambda self: BaselineOutcome(kind, exit_code, "why"))

    def test_without_the_flag_the_baseline_is_never_consulted(self, script, monkeypatch):
        monkeypatch.setattr("sys.argv", ["gate-check"])
        monkeypatch.setattr(GateSystem, "run_baseline_gate",
                            lambda self: pytest.fail("baseline consulted without --baseline"))
        assert script.main() == 0
        assert [event[0] for event in self.events] == ["gate-check"]

    def test_fallback_runs_the_full_gate_and_records_both(self, script, monkeypatch):
        monkeypatch.setattr("sys.argv", ["gate-check", "--baseline"])
        self._outcome(monkeypatch, BASELINE_FALLBACK)
        assert script.main() == 0
        assert self.full_gate_runs == 1
        assert [(gate, outcome) for gate, outcome, _ in self.events] == [
            (BASELINE_GATE, "fallback"), ("gate-check", "pass")]

    def test_reused_skips_the_full_gate(self, script, monkeypatch):
        monkeypatch.setattr("sys.argv", ["gate-check", "--baseline"])
        self._outcome(monkeypatch, BASELINE_REUSED)
        assert script.main() == 0
        assert self.full_gate_runs == 0
        assert [(gate, outcome) for gate, outcome, _ in self.events] == [(BASELINE_GATE, "pass")]

    def test_ci_failure_exits_3_without_the_full_gate(self, script, monkeypatch):
        monkeypatch.setattr("sys.argv", ["gate-check", "--baseline"])
        self._outcome(monkeypatch, BASELINE_CI_FAILED, ci_baseline.EXIT_CI_FAILED)
        assert script.main() == ci_baseline.EXIT_CI_FAILED
        assert self.full_gate_runs == 0
        gate, outcome, kwargs = self.events[0]
        assert (gate, outcome, kwargs["exit_code"]) == (BASELINE_GATE, "fail", 3)


class TestDefinitionPins:
    def test_changed_definition_falls_back_naming_the_job(self, repo, monkeypatch, capsys):
        narrowed = CI_YAML.replace("uv run pytest tests/\n", "uv run pytest tests/ -m fast\n")
        harness = Harness(repo, monkeypatch,
                          _client(_sha(repo), passing_jobs(_sha(repo)), ci_yaml=narrowed))
        outcome = harness.run()
        assert outcome.kind == BASELINE_FALLBACK
        assert outcome.reason.startswith("pytest: CI definition changed since it was audited")
        assert harness.smoke_calls == [] and harness.commit_gate_calls == []
        out = capsys.readouterr().out
        assert f"CI baseline not used: {outcome.reason}" in out
        assert "gate-check --baseline --print-pins" in out

    def test_unpinned_mapping_falls_back(self, repo, monkeypatch, tmp_path):
        info = tmp_path / "work" / HOST / PROJECT / "info" / "gate-check.yaml"
        info.write_text("ci_baseline:\n  provider: gitlab\n  jobs:\n"
                        "    pre-commit: lint\n    pytest: tests\n")
        outcome = Harness(repo, monkeypatch,
                          _client(_sha(repo), passing_jobs(_sha(repo)))).run()
        assert outcome.kind == BASELINE_FALLBACK
        assert f"no audited definition pin (this commit's is {PINS['pytest']})" in outcome.reason


class TestPrintPins:
    @pytest.fixture
    def script(self, repo, monkeypatch):
        module = _load_gate_check()
        self.events = []
        self.client = _client(_sha(repo), passing_jobs(_sha(repo)))
        monkeypatch.setattr(module, "emit_gate_event",
                            lambda *args, **kwargs: self.events.append(args))
        monkeypatch.setattr(module, "commit_gate",
                            lambda **_kwargs: pytest.fail("--print-pins ran the gate"))
        monkeypatch.setattr(GateSystem, "_baseline_source",
                            lambda _self, _host: GitLabEvidence(self.client))
        monkeypatch.chdir(repo)
        return module

    def test_prints_the_jobs_block_and_records_nothing(self, script, repo, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["gate-check", "--baseline", "--print-pins"])
        assert script.main() == 0
        out = capsys.readouterr().out
        assert f"@ {_sha(repo)}" in out
        assert (f'  "pytest":\n    covers: tests\n    definition: "{PINS["pytest"]}"'
                in out)
        assert f'definition: "{PINS["pre-commit"]}"' in out
        assert self.events == []
        assert f"/projects/{PROJECT_ID}/ci/lint?content_ref={_sha(repo)}" in self.client.requests

    def test_output_round_trips_through_the_mapping_parser(self, script, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["gate-check", "--print-pins"])
        assert script.main() == 0
        block = capsys.readouterr().out
        mapping, reason = ci_baseline.parse_mapping(
            {"provider": "gitlab", **yaml.safe_load(block)})
        assert reason == ""
        assert mapping.pins == PINS

    def test_revision_argument(self, script, repo, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["gate-check", "--print-pins", "HEAD^{commit}"])
        assert script.main() == 0
        assert f"@ {_sha(repo)}" in capsys.readouterr().out

    def test_unknown_revision_fails(self, script, monkeypatch):
        monkeypatch.setattr("sys.argv", ["gate-check", "--print-pins", "no-such-rev"])
        assert script.main() == 1

    def test_job_missing_from_the_configuration_fails(self, script, monkeypatch, capsys):
        self.client.routes[f"/projects/{PROJECT_ID}/ci/lint"]["merged_yaml"] = (
            CI_YAML.split("pytest:\n")[0])
        monkeypatch.setattr("sys.argv", ["gate-check", "--print-pins"])
        assert script.main() == 1
        assert "not defined in the CI configuration" in capsys.readouterr().out

    def test_no_mapping_fails(self, script, monkeypatch, tmp_path):
        (tmp_path / "work" / HOST / PROJECT / "info" / "gate-check.yaml").write_text("{}\n")
        monkeypatch.setattr("sys.argv", ["gate-check", "--print-pins"])
        assert script.main() == 1


def test_cli_env_dict_passes_the_resolved_toggle():
    """Guard: the container gets the host-resolved LMER_GATE_CI_BASELINE, not
    whatever the bare variable happened to say."""
    source = (REPO_ROOT / "src" / "lmer_cli" / "cli.py").read_text()
    assert re.search(r"""["']LMER_GATE_CI_BASELINE["']\s*:\s*ci_baseline\[0\]""", source)
    assert "ci_baseline = resolve_gate_ci_baseline(scope)" in source


@pytest.mark.parametrize("toggle, delivered", [("1", True), ("0", False), (None, False)])
def test_evidence_token_reaches_only_baseline_sessions(tmp_path, toggle, delivered):
    """#582: the evidence token reads as a Maintainer, CI/CD variable values
    included, so a session whose resolved baseline is off never receives it."""
    env_file = tmp_path / "deploy.env"
    env_file.write_text(f"{EVIDENCE_ENV}={EVIDENCE_TOKEN}\n"
                        f"GITLAB_TOKEN_gitlab_example_com={DEVELOPER_TOKEN}\n")
    captured: dict = {}
    _run_main(["--env-file", str(env_file), "--no-task", "--exec", "true", REPO_URL],
              env_in={"LMER_GATE_CI_BASELINE": toggle} if toggle else None,
              captured_env=captured)
    assert captured.get("GITLAB_TOKEN_gitlab_example_com") == DEVELOPER_TOKEN
    assert (captured.get(EVIDENCE_ENV) == EVIDENCE_TOKEN) is delivered
    assert (EVIDENCE_ENV in captured) is delivered
