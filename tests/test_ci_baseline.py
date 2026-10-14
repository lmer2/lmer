"""Tests for lmer_cli.ci_baseline — CI evidence for the initial baseline (#452)."""

import http.client
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
from urllib.error import HTTPError

import pytest

from lmer_cli import ci_baseline
from lmer_cli.ci_baseline import (
    FAILED,
    PASSED,
    SMOKE_FAIL,
    SMOKE_PASS,
    SMOKE_TIMEOUT,
    UNVERIFIED,
    Attempt,
    CoverageMapping,
    GitLabEvidence,
    collect,
    current_pins,
    definition_digest,
    job_definition,
    judge,
    judge_job,
    load_ci_config,
    parse_mapping,
    run_smoke,
)
from lmer_cli.gitlab_pipeline import GitLabPipelineClient

SHA = "0752f6f9104756e7213cba5a74e1540168cba23a"
OTHER_SHA = "f220a0721e4b0f6a0e3c5d0e9a5c0b1d2e3f4a5b"
PROJECT = "group/project"
PROJECT_ID = 414

CI_YAML = """\
variables:
  UV_CACHE_DIR: .uv
.setup:
  before_script:
    - pip install uv
pre-commit:
  stage: lint
  extends: .setup
  script:
    - uv run pre-commit run --all-files
pytest:
  stage: test
  before_script:
    - !reference [.setup, before_script]
  script:
    - uv run pytest tests/
publish:
  stage: deploy
  script:
    - ./publish.sh
"""
WORKFLOW_NARROWING = """\
workflow:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
      variables:
        PYTEST_ADDOPTS: "-m 'not slow'"
    - when: always
"""


def pins_for(ci_yaml=CI_YAML, names=("pre-commit", "pytest")):
    config = load_ci_config(ci_yaml)
    return {name: definition_digest(job_definition(config, name)) for name in names}


PINS = pins_for()
MAPPING = CoverageMapping(provider="gitlab",
                          jobs={"pre-commit": "lint", "pytest": "tests"},
                          pins=PINS)


def job(job_id, name, status, pipeline_id=1, sha=SHA, allow_failure=False,
        finished_at="2026-10-10T05:38:54Z"):
    return {
        "id": job_id,
        "name": name,
        "status": status,
        "allow_failure": allow_failure,
        "finished_at": finished_at,
        "web_url": f"https://gitlab.example.com/{PROJECT}/-/jobs/{job_id}",
        "pipeline": {"id": pipeline_id, "sha": sha, "project_id": PROJECT_ID},
    }


def pipeline(pipeline_id, sha=SHA, status="success", project_id=PROJECT_ID,
             source="merge_request_event"):
    return {
        "id": pipeline_id,
        "sha": sha,
        "status": status,
        "source": source,
        "ref": "refs/merge-requests/1/head",
        "project_id": project_id,
        "web_url": f"https://gitlab.example.com/{PROJECT}/-/pipelines/{pipeline_id}",
    }


class FakeClient:
    """Answers GitLab REST paths from a route table; paginates lists."""

    def __init__(self, routes, failures=None):
        self.routes = routes
        self.failures = failures or {}
        self.requests = []

    def request(self, endpoint, accept="application/json"):
        self.requests.append(endpoint)
        path, _, query = endpoint.partition("?")
        if path in self.failures:
            return self.failures[path]
        if path.endswith("/trace"):
            raise AssertionError("job logs are never read")
        if path not in self.routes:
            return 404, json.dumps({"message": "404 Not found"})
        data = self.routes[path]
        if isinstance(data, list):
            params = urllib.parse.parse_qs(query)
            page = int(params.get("page", ["1"])[0])
            per_page = int(params.get("per_page", ["20"])[0])
            data = data[(page - 1) * per_page:page * per_page]
        return 200, json.dumps(data)


def config_routes(pipeline_ids, ci_yaml=CI_YAML, variables=None, project_id=PROJECT_ID):
    """The CI lint answer and every pipeline's variables (none by default)."""
    table = {f"/projects/{project_id}/ci/lint": {
        "valid": True, "errors": [], "warnings": [], "merged_yaml": ci_yaml}}
    for pipeline_id in pipeline_ids:
        table[f"/projects/{project_id}/pipelines/{pipeline_id}/variables"] = (
            (variables or {}).get(pipeline_id, []))
    return table


def routes(pipelines, jobs_by_pipeline, project_path=PROJECT, ci_yaml=CI_YAML,
           variables=None):
    table = {
        f"/projects/{urllib.parse.quote(PROJECT, safe='')}": {
            "id": PROJECT_ID, "path_with_namespace": project_path},
        f"/projects/{PROJECT_ID}/pipelines": pipelines,
    }
    for pipeline_id, jobs in jobs_by_pipeline.items():
        table[f"/projects/{PROJECT_ID}/pipelines/{pipeline_id}/jobs"] = jobs
    table.update(config_routes(
        [p["id"] for p in pipelines if isinstance(p, dict) and "id" in p],
        ci_yaml, variables))
    return table


def gather(client, mapping=MAPPING):
    return collect(mapping, GitLabEvidence(client), "gitlab.example.com",
                   PROJECT, SHA)


def passing(pipeline_id=10, **kwargs):
    """A commit whose required jobs both passed in one pipeline."""
    return FakeClient(routes([pipeline(pipeline_id, **kwargs)],
                             {pipeline_id: [job(1, "pre-commit", "success", pipeline_id),
                                            job(2, "pytest", "success", pipeline_id)]}))


class TestParseMapping:
    def test_absent_mapping_is_unusable(self):
        mapping, reason = parse_mapping(None)
        assert mapping is None
        assert "no ci_baseline" in reason

    def test_valid_mapping(self):
        mapping, reason = parse_mapping({
            "provider": "gitlab",
            "jobs": {"pre-commit": "lint", "pytest": "tests"},
            "smoke": {"timeout": 120, "tools": ["git", "jq"]},
        })
        assert reason == ""
        assert mapping.jobs == {"pre-commit": "lint", "pytest": "tests"}
        assert mapping.pins == {}
        assert mapping.smoke_timeout == 120
        assert mapping.tools == ["git", "jq"]

    def test_pinned_jobs(self):
        mapping, reason = parse_mapping({
            "provider": "gitlab",
            "jobs": {"pre-commit": {"covers": "lint", "definition": PINS["pre-commit"]},
                     "pytest": {"covers": "tests", "definition": PINS["pytest"]}},
        })
        assert reason == ""
        assert mapping.jobs == {"pre-commit": "lint", "pytest": "tests"}
        assert mapping.pins == PINS

    def test_smoke_defaults(self):
        mapping, _ = parse_mapping({"provider": "gitlab", "jobs": {"pytest": "tests"}})
        assert mapping.smoke_timeout == ci_baseline.DEFAULT_SMOKE_TIMEOUT
        assert mapping.tools == []

    @pytest.mark.parametrize("raw, fragment", [
        ("pytest", "not a mapping"),
        ({"provider": "github", "jobs": {"pytest": "tests"}}, "not supported"),
        ({"jobs": {"pytest": "tests"}}, "not supported"),
        ({"provider": "gitlab"}, "must map"),
        ({"provider": "gitlab", "jobs": {}}, "must map"),
        ({"provider": "gitlab", "jobs": {"pytest": "everything"}}, "covers"),
        ({"provider": "gitlab", "jobs": {"pre-commit": "lint"}}, "no job covering tests"),
        ({"provider": "gitlab", "jobs": {"pytest": "tests"}, "smoke": {"timeout": 0}}, "timeout"),
        ({"provider": "gitlab", "jobs": {"pytest": "tests"}, "smoke": {"timeout": True}}, "timeout"),
        ({"provider": "gitlab", "jobs": {"pytest": "tests"}, "smoke": {"tools": "git"}}, "tools"),
        ({"provider": "gitlab", "jobs": {"pytest": "tests"}, "smoke": []}, "smoke"),
        ({"provider": "gitlab", "jobs": {"pytest": {"covers": "tests", "definition": "abc"}}},
         "64 hex"),
        ({"provider": "gitlab", "jobs": {"pytest": {"covers": "tests",
                                                     "definition": "sha256:" + "A" * 64}}},
         "64 hex"),
        ({"provider": "gitlab", "jobs": {"pytest": {"covers": "tests", "pin": "x"}}},
         "unknown keys"),
        ({"provider": "gitlab", "jobs": {"pytest": {"definition": PINS["pytest"]}}}, "covers"),
    ])
    def test_invalid_mappings_are_refused(self, raw, fragment):
        mapping, reason = parse_mapping(raw)
        assert mapping is None
        assert fragment in reason


def attempt(job_id, status, allow_failure=False):
    return Attempt(job_id=job_id, pipeline_id=1, status=status,
                   allow_failure=allow_failure, finished_at=None, web_url=None)


class TestJudgeJob:
    def test_no_attempt_is_unverified(self):
        assert judge_job("pytest", "tests", []).verdict == UNVERIFIED

    def test_success(self):
        assert judge_job("pytest", "tests", [attempt(5, "success")]).verdict == PASSED

    def test_failure(self):
        assert judge_job("pytest", "tests", [attempt(5, "failed")]).verdict == FAILED

    def test_allowed_failure_is_still_a_failure(self):
        evidence = judge_job("pytest", "tests", [attempt(5, "failed", allow_failure=True)])
        assert evidence.verdict == FAILED
        assert "allowed to fail" in evidence.reason

    @pytest.mark.parametrize("status", [
        "created", "pending", "running", "canceled", "skipped", "manual",
        "scheduled", "waiting_for_resource", "preparing", "something-new",
    ])
    def test_nonterminal_or_inconclusive_is_unverified(self, status):
        assert judge_job("pytest", "tests", [attempt(5, status)]).verdict == UNVERIFIED

    def test_retry_that_passed_decides_and_both_attempts_are_kept(self):
        evidence = judge_job("pytest", "tests",
                             [attempt(9, "success"), attempt(5, "failed")])
        assert evidence.verdict == PASSED
        assert [a.job_id for a in evidence.attempts] == [5, 9]
        assert evidence.deciding.job_id == 9

    def test_newer_failure_beats_older_pass(self):
        evidence = judge_job("pytest", "tests",
                             [attempt(5, "success"), attempt(9, "failed")])
        assert evidence.verdict == FAILED
        assert evidence.deciding.job_id == 9

    @pytest.mark.parametrize("status", ["running", "canceled", "pending", "skipped", "manual"])
    def test_unfinished_retry_does_not_erase_a_known_failure(self, status):
        evidence = judge_job("pytest", "tests",
                             [attempt(5, "failed"), attempt(9, status)])
        assert evidence.verdict == FAILED
        assert "job 5 failed" in evidence.reason

    @pytest.mark.parametrize("status", ["running", "canceled", "pending"])
    def test_inconclusive_attempt_after_a_passed_retry_is_unverified_not_failed(self, status):
        """[failed, success, pending]: CI already overturned the failure, so
        reporting it as a CI failure would stop the run on stale evidence."""
        evidence = judge_job("pytest", "tests", [attempt(5, "failed"), attempt(7, "success"),
                                                 attempt(9, status)])
        assert evidence.verdict == UNVERIFIED

    def test_failure_after_a_pass_then_inconclusive_is_failed(self):
        evidence = judge_job("pytest", "tests", [attempt(5, "success"), attempt(7, "failed"),
                                                 attempt(9, "canceled")])
        assert evidence.verdict == FAILED
        assert "job 7 failed" in evidence.reason

    def test_unfinished_retry_over_a_pass_is_unverified(self):
        evidence = judge_job("pytest", "tests",
                             [attempt(5, "success"), attempt(9, "running")])
        assert evidence.verdict == UNVERIFIED


class TestJudge:
    def test_all_required_passed(self):
        verdict, _, jobs = judge(MAPPING, [job(1, "pre-commit", "success"),
                                           job(2, "pytest", "success")])
        assert verdict == PASSED
        assert {j.name for j in jobs} == {"pre-commit", "pytest"}

    def test_unrelated_publish_failure_does_not_count(self):
        verdict, _, _ = judge(MAPPING, [
            job(1, "pre-commit", "success"),
            job(2, "pytest", "success"),
            job(3, "build-container", "failed"),
            job(4, "publish-container", "failed"),
        ])
        assert verdict == PASSED

    def test_failed_lint_fails(self):
        verdict, reason, _ = judge(MAPPING, [job(1, "pre-commit", "failed"),
                                             job(2, "pytest", "success")])
        assert verdict == FAILED
        assert "pre-commit" in reason

    def test_failure_outranks_unverified(self):
        verdict, _, _ = judge(MAPPING, [job(1, "pre-commit", "failed")])
        assert verdict == FAILED

    def test_missing_required_job_is_unverified(self):
        verdict, reason, _ = judge(MAPPING, [job(1, "pre-commit", "success")])
        assert verdict == UNVERIFIED
        assert "pytest" in reason

    def test_parallel_split_names_do_not_stand_in_for_the_declared_job(self):
        verdict, _, _ = judge(MAPPING, [job(1, "pre-commit", "success"),
                                        job(2, "pytest 1/2", "success"),
                                        job(3, "pytest 2/2", "success")])
        assert verdict == UNVERIFIED

    def test_skipped_required_job_is_unverified(self):
        verdict, _, _ = judge(MAPPING, [job(1, "pre-commit", "success"),
                                        job(2, "pytest", "skipped")])
        assert verdict == UNVERIFIED


class TestCollect:
    def test_passed_with_red_pipeline_from_unrelated_publish(self):
        client = FakeClient(routes(
            [pipeline(10, status="failed")],
            {10: [job(1, "pre-commit", "success", 10),
                  job(2, "pytest", "success", 10),
                  job(3, "publish-container", "failed", 10)]}))
        evidence = gather(client)
        assert evidence.verdict == PASSED
        assert evidence.project_id == PROJECT_ID
        assert evidence.sha == SHA
        assert [p["id"] for p in evidence.pipelines] == [10]
        assert evidence.completed_at == "2026-10-10T05:38:54Z"

    def test_green_pipeline_with_failed_allowed_test_job_fails(self):
        client = FakeClient(routes(
            [pipeline(10, status="success")],
            {10: [job(1, "pre-commit", "success", 10),
                  job(2, "pytest", "failed", 10, allow_failure=True)]}))
        assert gather(client).verdict == FAILED

    def test_no_pipeline_at_commit_is_unverified(self):
        evidence = gather(FakeClient(routes([], {})))
        assert evidence.verdict == UNVERIFIED
        assert "no CI pipeline" in evidence.reason

    def test_pipeline_for_another_sha_is_ignored(self):
        client = FakeClient(routes(
            [pipeline(10, sha=OTHER_SHA)],
            {10: [job(1, "pre-commit", "success", 10, sha=OTHER_SHA),
                  job(2, "pytest", "success", 10, sha=OTHER_SHA)]}))
        assert gather(client).verdict == UNVERIFIED

    def test_pipeline_from_another_project_is_ignored(self):
        client = FakeClient(routes(
            [pipeline(10, project_id=999)],
            {10: [job(1, "pre-commit", "success", 10),
                  job(2, "pytest", "success", 10)]}))
        assert gather(client).verdict == UNVERIFIED

    def test_project_resolving_elsewhere_is_unverified(self):
        client = FakeClient(routes([pipeline(10)], {}, project_path="other/project"))
        evidence = gather(client)
        assert evidence.verdict == UNVERIFIED
        assert "other/project" in evidence.reason

    @pytest.mark.parametrize("status", [0, 401, 403, 500, 502])
    def test_api_failure_is_unverified(self, status):
        client = FakeClient(routes([pipeline(10)], {}),
                            failures={f"/projects/{PROJECT_ID}/pipelines": (status, "")})
        evidence = gather(client)
        assert evidence.verdict == UNVERIFIED
        assert "answered" in evidence.reason

    def test_unreadable_json_is_unverified(self):
        client = FakeClient(routes([pipeline(10)], {}),
                            failures={f"/projects/{PROJECT_ID}/pipelines": (200, "<html>")})
        assert gather(client).verdict == UNVERIFIED

    def test_unexpected_shape_is_unverified(self):
        client = FakeClient(routes([{"sha": SHA, "project_id": PROJECT_ID}], {}))
        evidence = gather(client)
        assert evidence.verdict == UNVERIFIED
        assert "unexpected" in evidence.reason

    def test_job_record_without_an_id_is_unverified(self):
        broken = job(2, "pytest", "success", 10)
        del broken["id"]
        client = FakeClient(routes([pipeline(10)], {10: [job(1, "pre-commit", "success", 10), broken]}))
        evidence = gather(client)
        assert evidence.verdict == UNVERIFIED
        assert "unexpected" in evidence.reason

    def test_non_mapping_pipeline_record_is_unverified(self):
        client = FakeClient(routes(["not-a-pipeline"], {}))
        assert gather(client).verdict == UNVERIFIED

    @pytest.mark.parametrize("exc", [
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        TimeoutError("The read operation timed out"),
        ConnectionResetError(104, "Connection reset by peer"),
        http.client.IncompleteRead(b"partial"),
    ])
    def test_transport_errors_after_the_request_are_unverified(self, exc):
        class Exploding(FakeClient):
            def request(self, endpoint, accept="application/json"):
                raise exc

        evidence = gather(Exploding(routes([pipeline(10)], {})))
        assert evidence.verdict == UNVERIFIED

    def test_overall_deadline_makes_evidence_unverified(self):
        now = [0.0]

        class Slow(FakeClient):
            def request(self, endpoint, accept="application/json"):
                now[0] += 50
                return super().request(endpoint, accept)

        client = Slow(routes([pipeline(10)], {10: [job(1, "pre-commit", "success", 10),
                                                    job(2, "pytest", "success", 10)]}))
        evidence = collect(MAPPING, GitLabEvidence(client, budget_s=60, clock=lambda: now[0]),
                           "gitlab.example.com", PROJECT, SHA)
        assert evidence.verdict == UNVERIFIED
        assert "within 60s" in evidence.reason

    def test_too_many_records_is_unverified(self, monkeypatch):
        monkeypatch.setattr(ci_baseline, "PER_PAGE", 1)
        monkeypatch.setattr(ci_baseline, "MAX_PAGES", 2)
        client = FakeClient(routes([pipeline(10), pipeline(11), pipeline(12)], {}))
        evidence = gather(client)
        assert evidence.verdict == UNVERIFIED
        assert "more than" in evidence.reason

    def test_attempts_across_pipelines_latest_decides(self):
        client = FakeClient(routes(
            [pipeline(11), pipeline(10)],
            {10: [job(1, "pre-commit", "success", 10), job(2, "pytest", "failed", 10)],
             11: [job(5, "pre-commit", "success", 11), job(6, "pytest", "success", 11)]}))
        evidence = gather(client)
        assert evidence.verdict == PASSED
        pytest_job = next(j for j in evidence.jobs if j.name == "pytest")
        assert [a.job_id for a in pytest_job.attempts] == [2, 6]
        assert [a.status for a in pytest_job.attempts] == ["failed", "success"]

    def test_jobs_are_requested_with_retries_included(self):
        client = FakeClient(routes([pipeline(10)], {10: []}))
        gather(client)
        assert any("include_retried=true" in r for r in client.requests)

    def test_evidence_payload_carries_provenance(self):
        client = FakeClient(routes(
            [pipeline(10)],
            {10: [job(1, "pre-commit", "success", 10), job(2, "pytest", "success", 10)]}))
        payload = gather(client).as_dict()
        assert payload["project"] == PROJECT
        assert payload["project_id"] == PROJECT_ID
        assert payload["sha"] == SHA
        assert payload["pipelines"][0]["web_url"].endswith("/pipelines/10")
        names = {j["name"]: j for j in payload["jobs"]}
        assert names["pytest"]["deciding_attempt"]["job_id"] == 2
        assert names["pytest"]["deciding_attempt"]["web_url"].endswith("/jobs/2")
        assert names["pytest"]["covers"] == "tests"


class TestClientRequest:
    """`GitLabPipelineClient.request` keeps the status `_get_json` discards."""

    def test_http_error_status_is_returned(self, monkeypatch):
        def fail(*_args, **_kwargs):
            raise HTTPError("url", 403, "Forbidden", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", fail)
        status, _body = GitLabPipelineClient("gitlab.example.com", "t").request("/projects/1")
        assert status == 403

    def test_403_makes_evidence_unverified_not_failed(self):
        client = FakeClient(routes([pipeline(10)], {}),
                            failures={f"/projects/{urllib.parse.quote(PROJECT, safe='')}": (403, "")})
        evidence = gather(client)
        assert evidence.verdict == UNVERIFIED
        assert "403" in evidence.reason


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class TestRunSmoke:
    def _write(self, tmp_path, body):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_x.py").write_text(body)

    def test_pass(self, tmp_path):
        self._write(tmp_path, "def test_a():\n    pass\n")
        result = run_smoke([sys.executable], ["-m", "pytest", "--collect-only", "-q", "tests/"],
                           [], 60, str(tmp_path), dict(os.environ))
        assert result.outcome == SMOKE_PASS, result.detail
        assert result.python == sys.executable

    def test_collection_error_is_a_failure(self, tmp_path):
        self._write(tmp_path, "import module_that_does_not_exist\n")
        result = run_smoke([sys.executable], ["-m", "pytest", "--collect-only", "-q", "tests/"],
                           [], 60, str(tmp_path), dict(os.environ))
        assert result.outcome == SMOKE_FAIL

    def test_missing_tool_is_a_failure(self, tmp_path):
        result = run_smoke([sys.executable], ["-c", "pass"],
                           ["definitely-not-a-tool-452"], 60, str(tmp_path), dict(os.environ))
        assert result.outcome == SMOKE_FAIL
        assert "definitely-not-a-tool-452" in result.detail

    def test_no_interpreter_with_pytest_is_a_failure(self, tmp_path):
        result = run_smoke(["/nonexistent/python"], ["-c", "pass"], [], 60,
                           str(tmp_path), dict(os.environ))
        assert result.outcome == SMOKE_FAIL
        assert "can import pytest" in result.detail

    def test_collection_past_the_deadline_times_out(self, tmp_path):
        result = run_smoke([sys.executable], ["-c", "import time; time.sleep(30)"], [],
                           2, str(tmp_path), dict(os.environ))
        assert result.outcome == SMOKE_TIMEOUT

    def test_budget_spent_before_collection_times_out(self, tmp_path):
        clock = FakeClock()

        def slow_clock():
            clock.now += 10
            return clock.now

        result = run_smoke([sys.executable], ["-c", "pass"], [], 5,
                           str(tmp_path), dict(os.environ), clock=slow_clock)
        assert result.outcome == SMOKE_TIMEOUT


class TestRealTransport:
    """Against real sockets: what the fakes above cannot show."""

    def _client(self, port):
        client = GitLabPipelineClient("127.0.0.1", "t")
        client.base_url = f"http://127.0.0.1:{port}/api/v4"
        return client

    def test_server_hanging_up_after_the_request_is_unverified(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        port = listener.getsockname()[1]

        def serve():
            for _ in range(3):
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                conn.recv(4096)
                conn.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            evidence = collect(MAPPING, GitLabEvidence(self._client(port)),
                               "127.0.0.1", PROJECT, SHA)
        finally:
            listener.close()
        assert evidence.verdict == UNVERIFIED
        assert "RemoteDisconnected" in evidence.reason or "Connection" in evidence.reason


def _edit(ci_yaml, old, new):
    assert old in ci_yaml
    return ci_yaml.replace(old, new)


class TestDefinition:
    def test_digest_is_stable(self):
        assert pins_for() == pins_for()
        assert all(ci_baseline.PIN_PATTERN.fullmatch(pin) for pin in PINS.values())

    def test_custom_tags_are_kept_as_data(self):
        config = load_ci_config(CI_YAML)
        assert config["pytest"]["before_script"] == [{"!reference": [".setup", "before_script"]}]

    def test_absent_job_has_no_definition(self):
        assert job_definition(load_ci_config(CI_YAML), "pytest 1/2") is None

    @pytest.mark.parametrize("old, new, moved", [
        # Narrowing the declared job's script under the same name.
        ("uv run pytest tests/\n", "uv run pytest tests/ -m 'not slow'\n", {"pytest"}),
        ("uv run pytest tests/\n", "uv run pytest tests/ --deselect tests/test_a.py\n",
         {"pytest"}),
        # A global variable reaches every job.
        ("  UV_CACHE_DIR: .uv\n", "  UV_CACHE_DIR: .uv\n  PYTEST_ADDOPTS: -k fast\n",
         {"pre-commit", "pytest"}),
        # An entry reached by extends (pre-commit) and by !reference (pytest).
        ("    - pip install uv\n", "    - pip install uv==0.1\n", {"pre-commit", "pytest"}),
        # Another job's change leaves both pins alone.
        ("    - ./publish.sh\n", "    - ./publish.sh --dry-run\n", set()),
    ])
    def test_what_moves_a_pin(self, old, new, moved):
        changed = pins_for(_edit(CI_YAML, old, new))
        assert {name for name in PINS if changed[name] != PINS[name]} == moved

    def test_default_section_moves_every_pin(self):
        changed = pins_for(CI_YAML + "default:\n  image: python:3.13\n")
        assert all(changed[name] != PINS[name] for name in PINS)

    def test_workflow_rules_variables_move_every_pin(self):
        changed = pins_for(CI_YAML + WORKFLOW_NARROWING)
        assert all(changed[name] != PINS[name] for name in PINS)


class TestConfirm:
    def test_pinned_jobs_from_a_merge_request_pipeline_pass(self):
        evidence = gather(passing())
        assert evidence.verdict == PASSED
        payload = {job["name"]: job for job in evidence.as_dict()["jobs"]}
        assert payload["pytest"]["definition"] == PINS["pytest"]
        assert payload["pytest"]["pinned"] == PINS["pytest"]
        assert payload["pytest"]["pipeline_source"] == "merge_request_event"

    def test_push_pipeline_passes(self):
        assert gather(passing(source="push")).verdict == PASSED

    def test_missing_pin_is_unverified_and_prints_the_current_definition(self):
        mapping = CoverageMapping(provider="gitlab", jobs=MAPPING.jobs,
                                  pins={"pre-commit": PINS["pre-commit"]})
        evidence = gather(passing(), mapping)
        assert evidence.verdict == UNVERIFIED
        assert evidence.reason == (f"pytest: no audited definition pin "
                                   f"(this commit's is {PINS['pytest']})")

    def test_narrowed_job_under_the_same_name_is_unverified(self):
        narrowed = _edit(CI_YAML, "uv run pytest tests/\n", "uv run pytest tests/ -m 'not slow'\n")
        client = FakeClient(routes([pipeline(10)],
                                   {10: [job(1, "pre-commit", "success", 10),
                                         job(2, "pytest", "success", 10)]},
                                   ci_yaml=narrowed))
        evidence = gather(client)
        assert evidence.verdict == UNVERIFIED
        now = pins_for(narrowed)["pytest"]
        assert evidence.reason == (f"pytest: CI definition changed since it was audited "
                                   f"(pin {PINS['pytest']}, now {now})")
        pre_commit = next(j for j in evidence.jobs if j.name == "pre-commit")
        assert pre_commit.verdict == PASSED

    def test_pipeline_wide_narrowing_through_workflow_is_unverified(self):
        # YAML variables, so the pipeline variables endpoint stays empty.
        narrowed = CI_YAML + WORKFLOW_NARROWING
        evidence = gather(FakeClient(routes([pipeline(10)],
                                            {10: [job(1, "pre-commit", "success", 10),
                                                  job(2, "pytest", "success", 10)]},
                                            ci_yaml=narrowed)))
        assert evidence.verdict == UNVERIFIED
        now = pins_for(narrowed)["pytest"]
        assert (f"pytest: CI definition changed since it was audited "
                f"(pin {PINS['pytest']}, now {now})") in evidence.reason

    def test_job_missing_from_the_configuration_is_unverified(self):
        without = CI_YAML.split("pytest:\n")[0]
        client = FakeClient(routes([pipeline(10)],
                                   {10: [job(1, "pre-commit", "success", 10),
                                         job(2, "pytest", "success", 10)]},
                                   ci_yaml=without))
        evidence = gather(client)
        assert evidence.verdict == UNVERIFIED
        assert "pytest: not defined in the CI configuration" in evidence.reason

    @pytest.mark.parametrize("source", [
        "web", "api", "schedule", "trigger", "pipeline", "parent_pipeline", None,
    ])
    def test_pass_from_an_untrusted_trigger_is_unverified(self, source):
        evidence = gather(passing(source=source))
        assert evidence.verdict == UNVERIFIED
        assert f"from {source!r}, not a push or merge request" in evidence.reason

    def test_pipeline_variables_make_the_pass_unverified_and_values_stay_out(self):
        client = FakeClient(routes(
            [pipeline(10, source="push")],
            {10: [job(1, "pre-commit", "success", 10), job(2, "pytest", "success", 10)]},
            variables={10: [{"key": "PYTEST_ADDOPTS", "value": "-k secret-value-452",
                             "variable_type": "env_var"}]}))
        evidence = gather(client)
        assert evidence.verdict == UNVERIFIED
        assert "pipeline 10 was created with variables: PYTEST_ADDOPTS" in evidence.reason
        assert "secret-value-452" not in json.dumps(evidence.as_dict())

    def test_deciding_pipeline_is_the_one_checked(self):
        """A failed attempt in a web pipeline under a pass from a merge
        request: only the deciding attempt's pipeline is confirmed."""
        client = FakeClient(routes(
            [pipeline(11), pipeline(10, source="web")],
            {10: [job(1, "pre-commit", "success", 10), job(2, "pytest", "failed", 10)],
             11: [job(5, "pre-commit", "success", 11), job(6, "pytest", "success", 11)]}))
        assert gather(client).verdict == PASSED
        assert not any("/pipelines/10/variables" in r for r in client.requests)

    @pytest.mark.parametrize("lint", [
        {"valid": False, "errors": ["jobs config should contain at least one visible job"],
         "merged_yaml": ""},
        {"valid": True},
        {"valid": True, "merged_yaml": "- not\n- a mapping\n"},
        {"valid": True, "merged_yaml": "key: [unclosed\n"},
    ])
    def test_unusable_configuration_is_unverified(self, lint):
        client = passing()
        client.routes[f"/projects/{PROJECT_ID}/ci/lint"] = lint
        assert gather(client).verdict == UNVERIFIED

    def test_lint_refused_is_unverified(self):
        client = passing()
        client.failures[f"/projects/{PROJECT_ID}/ci/lint"] = (403, "")
        evidence = gather(client)
        assert evidence.verdict == UNVERIFIED
        assert "403" in evidence.reason

    def test_variables_refused_to_the_run_token_names_the_fix(self):
        """GitLab answers the variables read 403 below Maintainer (#582)."""
        client = passing()
        client.failures[f"/projects/{PROJECT_ID}/pipelines/10/variables"] = (403, "")
        env = ci_baseline.evidence_token_env("gitlab.example.com")
        evidence = collect(MAPPING, GitLabEvidence(client, evidence_env=env),
                           "gitlab.example.com", PROJECT, SHA)
        assert evidence.verdict == UNVERIFIED
        assert evidence.reason == (
            "cannot read pipeline variables: GitLab refused the run token (403); "
            "it needs the Maintainer role, or set LMER_CI_BASELINE_TOKEN_gitlab_example_com "
            "to a Maintainer read_api token (see docs/GATE-CI-BASELINE.md)")

    def test_variables_refused_to_the_evidence_token_names_it(self):
        client = passing()
        client.failures[f"/projects/{PROJECT_ID}/pipelines/10/variables"] = (403, "")
        env = ci_baseline.evidence_token_env("gitlab.example.com")
        evidence = collect(MAPPING, GitLabEvidence(client, evidence_env=env, uses_evidence_token=True),
                           "gitlab.example.com", PROJECT, SHA)
        assert evidence.verdict == UNVERIFIED
        assert evidence.reason == (
            f"cannot read pipeline variables: GitLab refused {env} (403); "
            "it needs the Maintainer role (see docs/GATE-CI-BASELINE.md)")
        assert evidence.as_dict()["credential"] == env

    def test_variables_refused_without_a_host_still_points_at_the_evidence_token(self):
        client = passing()
        client.failures[f"/projects/{PROJECT_ID}/pipelines/10/variables"] = (403, "")
        evidence = gather(client)
        assert "or set LMER_CI_BASELINE_TOKEN_<host> to a Maintainer" in evidence.reason
        assert evidence.as_dict()["credential"] == ci_baseline.RUN_CREDENTIAL

    def test_an_evidence_token_refused_anywhere_is_named(self):
        """An expired or revoked evidence token fails the first read, not the
        variables read."""
        client = passing()
        client.failures[f"/projects/{urllib.parse.quote(PROJECT, safe='')}"] = (401, "")
        env = ci_baseline.evidence_token_env("gitlab.example.com")
        evidence = collect(MAPPING, GitLabEvidence(client, evidence_env=env, uses_evidence_token=True),
                           "gitlab.example.com", PROJECT, SHA)
        assert evidence.verdict == UNVERIFIED
        assert evidence.reason.endswith(f"answered 401 to {env}")

    def test_an_answer_that_is_not_utf8_blames_no_token(self):
        """A proxy's latin-1 error page fails the decode in GitLabPipelineClient."""
        class Latin1Page(FakeClient):
            def request(self, endpoint, accept="application/json"):
                return 502, b"<html>Passerelle d\xe9faillante</html>".decode("utf-8")

        env = ci_baseline.evidence_token_env("gitlab.example.com")
        evidence = collect(MAPPING, GitLabEvidence(Latin1Page({}), evidence_env=env,
                                                   uses_evidence_token=True),
                           "gitlab.example.com", PROJECT, SHA)
        assert evidence.verdict == UNVERIFIED
        assert evidence.reason == (
            f"GitLab API /projects/{urllib.parse.quote(PROJECT, safe='')}: answer is not UTF-8")

    @pytest.mark.parametrize("token, sendable", [
        ("glpat-AbC_123-xyz", True),
        ("glpat-secret\rX", False),
        ("glpat-secret\nX", False),
        ("glpat-secret\x00", False),
        ("glpat-secret\x85", False),
        ("glpat-secret\u200b", False),
        ("glpat-secret\u0100", False),
    ])
    def test_header_text(self, token, sendable):
        assert ci_baseline.is_header_text(token) is sendable

    def test_refused_variables_leave_no_job_claiming_a_pass(self):
        """Seen live on #582: pre-commit printed "no variables" for a pipeline
        whose variables were never read."""
        client = passing()
        client.failures[f"/projects/{PROJECT_ID}/pipelines/10/variables"] = (403, "")
        evidence = gather(client)
        assert [job.verdict for job in evidence.jobs] == [UNVERIFIED, UNVERIFIED]
        assert all(job.reason == evidence.reason for job in evidence.jobs)

    def test_a_job_confirmed_before_the_refusal_keeps_its_pass(self):
        client = FakeClient(routes(
            [pipeline(10), pipeline(11)],
            {10: [job(1, "pre-commit", "success", 10)],
             11: [job(2, "pytest", "success", 11)]}))
        client.failures[f"/projects/{PROJECT_ID}/pipelines/11/variables"] = (403, "")
        evidence = gather(client)
        assert evidence.verdict == UNVERIFIED
        verdicts = {job.name: job.verdict for job in evidence.jobs}
        assert verdicts == {"pre-commit": PASSED, "pytest": UNVERIFIED}

    @pytest.mark.parametrize("status", [401, 404, 500])
    def test_other_variables_errors_stay_generic(self, status):
        client = passing()
        client.failures[f"/projects/{PROJECT_ID}/pipelines/10/variables"] = (status, "")
        evidence = gather(client)
        assert evidence.verdict == UNVERIFIED
        assert evidence.reason == (f"GitLab API /projects/{PROJECT_ID}/pipelines/10/"
                                   f"variables answered {status}")

    def test_evidence_token_env_is_per_host(self):
        assert ci_baseline.evidence_token_env("Git.Example-Host.com") == (
            "LMER_CI_BASELINE_TOKEN_git_example_host_com")

    def test_failed_verdict_reads_no_configuration(self):
        client = FakeClient(routes([pipeline(10, source="web")],
                                   {10: [job(1, "pre-commit", "success", 10),
                                         job(2, "pytest", "failed", 10)]}))
        assert gather(client).verdict == FAILED
        assert not any("/ci/lint" in r or "/variables" in r for r in client.requests)

    def test_lint_is_asked_for_the_exact_commit(self):
        client = passing()
        gather(client)
        assert f"/projects/{PROJECT_ID}/ci/lint?content_ref={SHA}" in client.requests


class TestCurrentPins:
    def test_prints_every_declared_job(self):
        assert current_pins(MAPPING, GitLabEvidence(passing()), PROJECT, SHA) == PINS

    def test_job_absent_from_the_configuration_is_none(self):
        mapping = CoverageMapping(provider="gitlab", jobs={"pytest 1/2": "tests"})
        assert current_pins(mapping, GitLabEvidence(passing()), PROJECT, SHA) == {
            "pytest 1/2": None}


class TestDeadline:
    def test_answer_arriving_past_the_budget_is_discarded(self):
        """A request that starts at 119 s and returns at 148 s, with a 120 s
        budget: nothing read after the deadline counts."""
        now = [0.0]

        class Late(FakeClient):
            # The last request (pipeline variables) is the late one, so no
            # later request's start-of-call check can stand in for this one.
            def request(self, endpoint, accept="application/json"):
                late = endpoint.endswith("/variables")
                if late:
                    now[0] = 119
                result = super().request(endpoint, accept)
                if late:
                    now[0] = 148
                return result

        source = GitLabEvidence(Late(passing().routes), budget_s=120, clock=lambda: now[0])
        evidence = collect(MAPPING, source, "gitlab.example.com", PROJECT, SHA)
        assert evidence.verdict == UNVERIFIED
        assert "within 120s" in evidence.reason
        assert source.client.requests[-1].endswith("/pipelines/10/variables")


class TestPagination:
    def test_deciding_attempt_past_the_first_page_decides(self, monkeypatch):
        """PER_PAGE=1: the failed pytest retry (job 6) sits on the second page
        of pipeline 11's jobs and of the pipeline list, and still decides."""
        monkeypatch.setattr(ci_baseline, "PER_PAGE", 1)
        client = FakeClient(routes(
            [pipeline(10), pipeline(11)],
            {10: [job(1, "pre-commit", "success", 10), job(2, "pytest", "success", 10)],
             11: [job(5, "pre-commit", "success", 11), job(6, "pytest", "failed", 11)]}))
        evidence = gather(client)
        assert evidence.verdict == FAILED
        pytest_job = next(j for j in evidence.jobs if j.name == "pytest")
        assert pytest_job.deciding.job_id == 6
        assert f"/projects/{PROJECT_ID}/pipelines?sha={SHA}&per_page=1&page=2" in client.requests
        assert (f"/projects/{PROJECT_ID}/pipelines/11/jobs?include_retried=true"
                f"&per_page=1&page=2") in client.requests


def _alive(pid):
    """True while `pid` runs; a zombie (killed, not yet reaped) is dead."""
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


class TestSmokeProcessGroup:
    @pytest.mark.skipif(not os.path.isdir("/proc"), reason="reads /proc")
    @pytest.mark.parametrize("stage", ["probe", "collection"])
    def test_timeout_kills_what_the_child_started(self, tmp_path, stage):
        """A grandchild (what a conftest might start) must not outlive the
        timeout: `subprocess.run` alone would leave it running."""
        pidfile = tmp_path / "grandchild.pid"
        spawn = ("import subprocess, sys, time; "
                 "p = subprocess.Popen(['sleep', '120']); "
                 f"open({str(pidfile)!r}, 'w').write(str(p.pid)); "
                 "time.sleep(120)")
        candidate = tmp_path / "python"
        if stage == "probe":
            candidate.write_text(f"#!/bin/sh\nexec {sys.executable} -c \"{spawn}\"\n")
            candidate.chmod(0o755)
            candidates, argv = [str(candidate)], ["-c", "pass"]
        else:
            candidates, argv = [sys.executable], ["-c", spawn]
        result = run_smoke(candidates, argv, [], 3, str(tmp_path), dict(os.environ))
        if stage == "probe":
            # A probe timeout moves on to the next candidate; none is left.
            assert result.outcome in (SMOKE_TIMEOUT, SMOKE_FAIL)
        else:
            assert result.outcome == SMOKE_TIMEOUT
        grandchild = int(pidfile.read_text())
        deadline = time.monotonic() + 30
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(grandchild)
