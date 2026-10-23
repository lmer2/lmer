"""Reuse passing CI on the exact starting commit as the initial baseline (#452).

`gate-check --baseline` asks one question before a development run touches
the tree: did trusted CI already run the required lint and test jobs on this
exact commit, and what did they say? This module answers it from the forge's
own records and nothing else. The orchestration — when to ask, what to run
afterwards, what to record — lives in `lmer_cli.gates`.

Only three verdicts exist, and only one of them lets the local suite be
skipped:

- PASSED: every required job's latest attempt succeeded.
- FAILED: a required job's latest attempt failed (or an unfinished retry sits
  on top of a failure). A known failure, not missing evidence.
- UNVERIFIED: anything else. The caller runs the normal local gate.

The decision reads forge API facts and the operator's coverage mapping, never
job logs. A pass counts only when the job that passed is the job the operator
audited: its resolved definition at this commit must match the mapping's pin,
and its pipeline must come from a push or merge request with no pipeline
variables. Otherwise the job is unverified.
"""

import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import subprocess
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import yaml

from lmer_cli.tokens import EVIDENCE_TOKEN_PREFIX, _per_host_env, _sanitize_hostname

PASSED = "passed"
FAILED = "failed"
UNVERIFIED = "unverified"

# gate-check's exit status when CI already failed a required job at this
# commit. Distinct from 1 (a local check failed) so the taskdef can route it to
# the baseline-failure question without parsing output.
EXIT_CI_FAILED = 3

PROVIDER_GITLAB = "gitlab"
COVERS = ("lint", "tests")

DEFAULT_SMOKE_TIMEOUT = 300
SMOKE_PASS = "pass"
SMOKE_FAIL = "fail"
SMOKE_TIMEOUT = "timeout"

# The whole API read, every page included. Past it the evidence is unread, not
# absent, and the gate falls back.
EVIDENCE_BUDGET_S = 120

PER_PAGE = 100
# A commit with more pipelines or jobs than this is not one whose evidence we
# can read completely, and an incomplete read is not a verdict.
MAX_PAGES = 5

# Pipeline sources whose jobs run the commit's own CI configuration as written.
# Web, API, schedule and trigger pipelines can carry variables this cannot see
# in full (schedule variables are not pipeline variables), and child pipelines
# run another configuration.
TRUSTED_SOURCES = ("push", "merge_request_event")

DOCS_HINT = "see docs/GATE-CI-BASELINE.md"
RUN_CREDENTIAL = "the run token"

PIN_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
# Top-level keys that can change what every job runs without touching the job
# itself (workflow: through rules:variables).
INHERITED_KEYS = ("variables", "default", "image", "services",
                  "before_script", "after_script", "cache", "workflow")

JOB_SUCCESS = "success"
JOB_FAILED = "failed"
# Every other GitLab job status (created, pending, running, canceled, skipped,
# manual, scheduled, waiting_for_resource, preparing, ...) proves nothing about
# whether the job's checks pass.


@dataclass
class CoverageMapping:
    """The operator's declaration of which CI jobs make up the baseline."""
    provider: str
    jobs: Dict[str, str]
    smoke_timeout: int = DEFAULT_SMOKE_TIMEOUT
    tools: List[str] = field(default_factory=list)
    # The audited definition digest per job; a job without one never passes.
    pins: Dict[str, str] = field(default_factory=dict)


@dataclass
class Attempt:
    job_id: int
    pipeline_id: int
    status: str
    allow_failure: bool
    finished_at: Optional[str]
    web_url: Optional[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "pipeline_id": self.pipeline_id,
            "status": self.status,
            "allow_failure": self.allow_failure,
            "finished_at": self.finished_at,
            "web_url": self.web_url,
        }


@dataclass
class JobEvidence:
    name: str
    covers: str
    verdict: str
    reason: str
    attempts: List[Attempt] = field(default_factory=list)
    definition: Optional[str] = None
    pinned: Optional[str] = None
    pipeline_source: Optional[str] = None
    confirmed: bool = False

    @property
    def deciding(self) -> Optional[Attempt]:
        return self.attempts[-1] if self.attempts else None

    def as_dict(self) -> Dict[str, Any]:
        deciding = self.deciding
        data: Dict[str, Any] = {
            "name": self.name,
            "covers": self.covers,
            "verdict": self.verdict,
            "reason": self.reason,
            "deciding_attempt": deciding.as_dict() if deciding else None,
            "attempts": [attempt.as_dict() for attempt in self.attempts],
        }
        for key in ("definition", "pinned", "pipeline_source"):
            if getattr(self, key) is not None:
                data[key] = getattr(self, key)
        return data


@dataclass
class Evidence:
    verdict: str
    reason: str
    provider: Optional[str] = None
    host: Optional[str] = None
    project: Optional[str] = None
    project_id: Optional[int] = None
    sha: Optional[str] = None
    pipelines: List[Dict[str, Any]] = field(default_factory=list)
    jobs: List[JobEvidence] = field(default_factory=list)
    credential: Optional[str] = None

    @property
    def completed_at(self) -> Optional[str]:
        """When the last deciding job finished (ISO 8601 sorts as text)."""
        times = [job.deciding.finished_at for job in self.jobs
                 if job.deciding and job.deciding.finished_at]
        return max(times) if times else None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "provider": self.provider,
            "host": self.host,
            "project": self.project,
            "project_id": self.project_id,
            "sha": self.sha,
            "credential": self.credential,
            "completed_at": self.completed_at,
            "pipelines": list(self.pipelines),
            "jobs": [job.as_dict() for job in self.jobs],
        }


class EvidenceUnavailable(Exception):
    """The forge could not be asked, or its answer could not be read whole."""


def parse_mapping(raw: Any) -> Tuple[Optional[CoverageMapping], str]:
    """The coverage mapping, or None and why it cannot be used.

    Strict on purpose: a mapping this reads loosely is a mapping that can
    silently skip a suite nobody declared covered.
    """
    if raw is None:
        return None, "no ci_baseline coverage mapping in the work-repo project info"
    if not isinstance(raw, dict):
        return None, "ci_baseline mapping is not a mapping"
    provider = raw.get("provider")
    if provider != PROVIDER_GITLAB:
        return None, f"ci_baseline provider {provider!r} is not supported (only 'gitlab')"
    jobs = raw.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        return None, "ci_baseline.jobs must map CI job names to lint/tests"
    parsed_jobs: Dict[str, str] = {}
    pins: Dict[str, str] = {}
    for name, spec in jobs.items():
        if not isinstance(name, str) or not name.strip():
            return None, "ci_baseline.jobs has an empty job name"
        covers, pin = spec, None
        if isinstance(spec, dict):
            unknown = set(spec) - {"covers", "definition"}
            if unknown:
                return None, (f"ci_baseline.jobs[{name!r}] has unknown keys: "
                              f"{', '.join(sorted(map(str, unknown)))}")
            covers, pin = spec.get("covers"), spec.get("definition")
        if covers not in COVERS:
            return None, (f"ci_baseline.jobs[{name!r}] covers {covers!r}; "
                          f"expected one of {', '.join(COVERS)}")
        if pin is not None:
            if not isinstance(pin, str) or not PIN_PATTERN.fullmatch(pin):
                return None, (f"ci_baseline.jobs[{name!r}].definition must be "
                              f"'sha256:' and 64 hex digits")
            pins[name] = pin
        parsed_jobs[name] = covers
    if "tests" not in parsed_jobs.values():
        return None, "ci_baseline.jobs declares no job covering tests"
    smoke = raw.get("smoke")
    if smoke is None:
        smoke = {}
    if not isinstance(smoke, dict):
        return None, "ci_baseline.smoke is not a mapping"
    timeout = smoke.get("timeout", DEFAULT_SMOKE_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        return None, "ci_baseline.smoke.timeout must be a positive integer (seconds)"
    tools = smoke.get("tools", [])
    if not isinstance(tools, list) or not all(
            isinstance(tool, str) and tool.strip() for tool in tools):
        return None, "ci_baseline.smoke.tools must be a list of command names"
    return CoverageMapping(provider=provider, jobs=parsed_jobs,
                           smoke_timeout=timeout, tools=list(tools), pins=pins), ""


class _CIConfigLoader(yaml.SafeLoader):
    """SafeLoader that keeps GitLab's custom tags (`!reference`) as data
    instead of refusing the document."""


def _tagged(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Dict[str, Any]:
    if isinstance(node, yaml.SequenceNode):
        value: Any = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node, deep=True)
    else:
        value = loader.construct_scalar(node)
    return {f"!{suffix}": value}


_CIConfigLoader.add_multi_constructor("!", _tagged)


def load_ci_config(text: str) -> Dict[str, Any]:
    config = yaml.load(text, Loader=_CIConfigLoader)
    if not isinstance(config, dict):
        raise ValueError("CI configuration is not a mapping")
    return config


def _referenced(value: Any) -> List[str]:
    """Entries a job pulls in by `!reference [entry, key...]`."""
    found: List[str] = []
    if isinstance(value, dict):
        ref = value.get("!reference")
        if isinstance(ref, list) and ref and isinstance(ref[0], str):
            found.append(ref[0])
        for item in value.values():
            found.extend(_referenced(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_referenced(item))
    return found


def job_definition(config: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    """What job `name` runs, as its pin digests it; None when it is not defined.

    Wider than GitLab's own merge on purpose: a digest over these inputs
    changes whenever the resolved job could, without reimplementing how
    GitLab merges them.
    """
    if not isinstance(config.get(name), dict):
        return None
    entries: Dict[str, Any] = {}
    queue = [name]
    while queue:
        current = queue.pop()
        if current in entries:
            continue
        entry = config.get(current)
        entries[current] = entry
        if isinstance(entry, dict):
            extends = entry.get("extends")
            if isinstance(extends, str):
                queue.append(extends)
            elif isinstance(extends, list):
                queue.extend(item for item in extends if isinstance(item, str))
        queue.extend(_referenced(entry))
    return {
        "job": name,
        "entries": entries,
        "inherited": {key: config[key] for key in INHERITED_KEYS if key in config},
    }


def definition_digest(definition: Dict[str, Any]) -> str:
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"),
                           default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _attempt(job: Dict[str, Any]) -> Attempt:
    pipeline = job.get("pipeline") or {}
    return Attempt(
        job_id=int(job["id"]),
        pipeline_id=int(pipeline.get("id") or job.get("_pipeline_id") or 0),
        status=str(job.get("status") or "unknown"),
        allow_failure=bool(job.get("allow_failure")),
        finished_at=job.get("finished_at"),
        web_url=job.get("web_url"),
    )


def judge_job(name: str, covers: str, attempts: Sequence[Attempt]) -> JobEvidence:
    """One required job's verdict: its latest attempt decides.

    A retry that passed supersedes the failure under it. When the latest
    attempt is inconclusive (unfinished, canceled, skipped, manual), the most
    recent conclusive attempt below it says what is known: a failure stands —
    otherwise canceling a retry would turn a known failure into "no evidence"
    and send the run to a local rerun — and a pass is not current evidence, so
    the job is unverified.
    """
    ordered = sorted(attempts, key=lambda attempt: attempt.job_id)
    if not ordered:
        return JobEvidence(name, covers, UNVERIFIED,
                           f"no {name!r} job ran at this commit")
    latest = ordered[-1]
    if latest.status == JOB_SUCCESS:
        return JobEvidence(name, covers, PASSED,
                           f"latest attempt (job {latest.job_id}) succeeded",
                           list(ordered))
    if latest.status == JOB_FAILED:
        allowed = " (allowed to fail, still a failure)" if latest.allow_failure else ""
        return JobEvidence(name, covers, FAILED,
                           f"latest attempt (job {latest.job_id}) failed{allowed}",
                           list(ordered))
    conclusive = [attempt for attempt in ordered[:-1]
                  if attempt.status in (JOB_SUCCESS, JOB_FAILED)]
    if conclusive and conclusive[-1].status == JOB_FAILED:
        return JobEvidence(
            name, covers, FAILED,
            f"job {conclusive[-1].job_id} failed and the latest attempt "
            f"(job {latest.job_id}) is {latest.status}, not a pass",
            list(ordered))
    return JobEvidence(name, covers, UNVERIFIED,
                       f"latest attempt (job {latest.job_id}) is {latest.status}",
                       list(ordered))


def judge(mapping: CoverageMapping, jobs: Sequence[Dict[str, Any]]
          ) -> Tuple[str, str, List[JobEvidence]]:
    """The overall verdict over every required job. Unmapped jobs are ignored:
    a failed image publish says nothing about lint or tests."""
    by_name: Dict[str, List[Attempt]] = {name: [] for name in mapping.jobs}
    for job in jobs:
        name = job.get("name")
        if name in by_name:
            by_name[name].append(_attempt(job))
    evidence = [judge_job(name, covers, by_name[name])
                for name, covers in mapping.jobs.items()]
    failed = [job for job in evidence if job.verdict == FAILED]
    if failed:
        return FAILED, "; ".join(f"{job.name}: {job.reason}" for job in failed), evidence
    unverified = [job for job in evidence if job.verdict == UNVERIFIED]
    if unverified:
        return (UNVERIFIED,
                "; ".join(f"{job.name}: {job.reason}" for job in unverified),
                evidence)
    return PASSED, "every required job's latest attempt succeeded", evidence


def evidence_token_env(host: str) -> str:
    return EVIDENCE_TOKEN_PREFIX + _sanitize_hostname(host)


def evidence_token(host: str) -> str:
    """The evidence token set for `host`, stripped, or "" when there is none.
    A hyphenated host's pre-#544 name is refused: it is also the dotted
    lookalike's name, and this token reads CI/CD variable values."""
    return (_per_host_env(EVIDENCE_TOKEN_PREFIX, host) or "").strip()


def is_header_text(token: str) -> bool:
    """Printable latin-1 only, deliberately stricter than http.client: nothing
    that passes can raise in `putheader`, whose exception quotes the token."""
    return token.isprintable() and all(ord(char) < 256 for char in token)


class GitLabEvidence:
    """Reads one commit's pipelines, jobs and CI configuration from the GitLab
    REST API, inside one overall deadline: the per-request socket timeout alone
    would let a paginated read run for many minutes. No request starts past the
    deadline, and an answer that arrives past it is discarded."""

    def __init__(self, client: Any, budget_s: float = EVIDENCE_BUDGET_S,
                 clock: Callable[[], float] = time.monotonic,
                 evidence_env: Optional[str] = None, uses_evidence_token: bool = False):
        self.client = client
        self.clock = clock
        self.deadline = clock() + budget_s
        self.budget_s = budget_s
        self.evidence_env = evidence_env
        self.uses_evidence_token = uses_evidence_token

    @property
    def credential(self) -> str:
        return self.evidence_env if self.uses_evidence_token else RUN_CREDENTIAL

    def _call(self, endpoint: str, fetch: Callable[[], Tuple[int, Any]]) -> Tuple[int, Any]:
        if self.clock() > self.deadline:
            raise EvidenceUnavailable(
                f"CI evidence not read within {self.budget_s:g}s")
        try:
            result = fetch()
        except (OSError, http.client.HTTPException) as exc:
            raise EvidenceUnavailable(
                f"GitLab API {endpoint.split('?')[0]}: {exc!r}") from exc
        except UnicodeDecodeError:
            # Its repr would carry the whole body, often a proxy's error page.
            raise EvidenceUnavailable(
                f"GitLab API {endpoint.split('?')[0]}: answer is not UTF-8") from None
        if self.clock() > self.deadline:
            raise EvidenceUnavailable(
                f"CI evidence not read within {self.budget_s:g}s")
        return result

    def _get(self, endpoint: str, forbidden: Optional[str] = None) -> Any:
        status, body = self._call(endpoint, lambda: self.client.request(endpoint))
        if status == 403 and forbidden:
            raise EvidenceUnavailable(forbidden)
        if status != 200:
            # An expired or revoked evidence token must be recognisable as such.
            to = f" to {self.credential}" if self.uses_evidence_token else ""
            raise EvidenceUnavailable(
                f"GitLab API {endpoint.split('?')[0]} answered {status or 'no response'}{to}")
        try:
            return json.loads(body)
        except ValueError as exc:
            raise EvidenceUnavailable(f"GitLab API {endpoint.split('?')[0]}: unreadable JSON") from exc

    def _get_all(self, endpoint: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        separator = "&" if "?" in endpoint else "?"
        for page in range(1, MAX_PAGES + 1):
            batch = self._get(f"{endpoint}{separator}per_page={PER_PAGE}&page={page}")
            if not isinstance(batch, list):
                raise EvidenceUnavailable(f"GitLab API {endpoint.split('?')[0]}: expected a list")
            items.extend(batch)
            if len(batch) < PER_PAGE:
                return items
        raise EvidenceUnavailable(
            f"more than {PER_PAGE * MAX_PAGES} records at {endpoint.split('?')[0]}")

    def project(self, path: str) -> Dict[str, Any]:
        data = self._get(f"/projects/{urllib.parse.quote(path, safe='')}")
        if not isinstance(data, dict) or "id" not in data:
            raise EvidenceUnavailable(f"GitLab project {path} not readable")
        return data

    def pipelines(self, project_id: int, sha: str) -> List[Dict[str, Any]]:
        return self._get_all(f"/projects/{project_id}/pipelines?sha={sha}")

    def jobs(self, project_id: int, pipeline_id: int) -> List[Dict[str, Any]]:
        return self._get_all(
            f"/projects/{project_id}/pipelines/{pipeline_id}/jobs?include_retried=true")

    def ci_config(self, project_id: int, sha: str) -> Dict[str, Any]:
        """The CI configuration at `sha`, includes resolved, as GitLab reads it."""
        data = self._get(f"/projects/{project_id}/ci/lint?content_ref={sha}")
        if not isinstance(data, dict) or not isinstance(data.get("merged_yaml"), str):
            raise EvidenceUnavailable("GitLab CI lint returned no merged configuration")
        if data.get("valid") is not True:
            raise EvidenceUnavailable("the CI configuration at this commit is not valid")
        try:
            return load_ci_config(data["merged_yaml"])
        except (yaml.YAMLError, ValueError) as exc:
            raise EvidenceUnavailable(f"unreadable CI configuration: {exc}") from exc

    def variables_forbidden(self) -> str:
        """Why a 403 on the pipeline-variables read is a role problem with a
        known fix, not a generic API failure."""
        reason = (f"cannot read pipeline variables: GitLab refused {self.credential} "
                  f"(403); it needs the Maintainer role")
        if self.uses_evidence_token:
            return f"{reason} ({DOCS_HINT})"
        token = self.evidence_env or f"{EVIDENCE_TOKEN_PREFIX}<host>"
        return f"{reason}, or set {token} to a Maintainer read_api token ({DOCS_HINT})"

    def pipeline_variables(self, project_id: int, pipeline_id: int) -> List[Dict[str, Any]]:
        data = self._get(f"/projects/{project_id}/pipelines/{pipeline_id}/variables",
                         forbidden=self.variables_forbidden())
        if not isinstance(data, list):
            raise EvidenceUnavailable("GitLab pipeline variables: expected a list")
        return data


def confirm(mapping: CoverageMapping, source: GitLabEvidence,
            evidence: Evidence) -> None:
    """Hold each passing job to the job the operator audited. A job whose
    definition no longer matches its pin, or whose pipeline could have run it
    differently, becomes unverified, and the verdict with it."""
    if evidence.project_id is None or evidence.sha is None:
        raise EvidenceUnavailable("no project or commit to confirm the jobs against")
    config = source.ci_config(evidence.project_id, evidence.sha)
    sources = {pipeline.get("id"): pipeline.get("source")
               for pipeline in evidence.pipelines}
    variables: Dict[int, List[Dict[str, Any]]] = {}
    for job in evidence.jobs:
        definition = job_definition(config, job.name)
        job.definition = definition_digest(definition) if definition else None
        job.pinned = mapping.pins.get(job.name)
        problems = []
        if job.definition is None:
            problems.append("not defined in the CI configuration at this commit")
        elif job.pinned is None:
            problems.append(f"no audited definition pin (this commit's is {job.definition})")
        elif job.pinned != job.definition:
            problems.append(f"CI definition changed since it was audited "
                            f"(pin {job.pinned}, now {job.definition})")
        deciding = job.deciding
        if deciding is None:
            problems.append("no attempt to confirm")
            job.verdict, job.reason = UNVERIFIED, "; ".join(problems)
            continue
        job.pipeline_source = sources.get(deciding.pipeline_id)
        if job.pipeline_source not in TRUSTED_SOURCES:
            problems.append(f"passed in pipeline {deciding.pipeline_id} from "
                            f"{job.pipeline_source!r}, not a push or merge request")
        else:
            if deciding.pipeline_id not in variables:
                variables[deciding.pipeline_id] = source.pipeline_variables(
                    evidence.project_id, deciding.pipeline_id)
            # Names only: a variable's value can be a secret.
            names = sorted(str(item.get("key")) for item in variables[deciding.pipeline_id])
            if names:
                problems.append(f"pipeline {deciding.pipeline_id} was created with "
                                f"variables: {', '.join(names)}")
        if problems:
            job.verdict = UNVERIFIED
            job.reason = "; ".join(problems)
        else:
            job.confirmed = True
    unverified = [job for job in evidence.jobs if job.verdict != PASSED]
    if unverified:
        evidence.verdict = UNVERIFIED
        evidence.reason = "; ".join(f"{job.name}: {job.reason}" for job in unverified)


def current_pins(mapping: CoverageMapping, source: GitLabEvidence, project: str,
                 sha: str) -> Dict[str, Optional[str]]:
    """The definition digest of every declared job at `sha`, for re-pinning
    after an audited CI change. None for a job the configuration lacks."""
    info = source.project(project)
    config = source.ci_config(int(info["id"]), sha)
    pins: Dict[str, Optional[str]] = {}
    for name in mapping.jobs:
        definition = job_definition(config, name)
        pins[name] = definition_digest(definition) if definition else None
    return pins


def collect(mapping: CoverageMapping, source: GitLabEvidence, host: str,
            project: str, sha: str) -> Evidence:
    """Evidence for `sha` in `project`. Never raises: anything that cannot be
    read becomes UNVERIFIED with the reason."""
    evidence = Evidence(UNVERIFIED, "", provider=mapping.provider, host=host,
                        project=project, sha=sha, credential=source.credential)
    try:
        info = source.project(project)
        project_id = int(info["id"])
        evidence.project_id = project_id
        canonical = str(info.get("path_with_namespace") or "")
        if canonical.lower() != project.lower():
            evidence.reason = (f"GitLab resolved {project} to {canonical or 'an unnamed project'}")
            return evidence
        evidence.project = canonical
        all_jobs: List[Dict[str, Any]] = []
        for pipeline in source.pipelines(project_id, sha):
            # The API filters by sha already; checking again is what makes
            # "exact commit, same project" a property of the evidence rather
            # than of the query string.
            if pipeline.get("sha") != sha or int(pipeline.get("project_id", project_id)) != project_id:
                continue
            evidence.pipelines.append({
                "id": pipeline.get("id"),
                "status": pipeline.get("status"),
                "source": pipeline.get("source"),
                "ref": pipeline.get("ref"),
                "web_url": pipeline.get("web_url"),
            })
            for job in source.jobs(project_id, int(pipeline["id"])):
                job_pipeline = job.get("pipeline") or {}
                if job_pipeline.get("sha", sha) != sha:
                    continue
                job.setdefault("_pipeline_id", pipeline["id"])
                all_jobs.append(job)
    except EvidenceUnavailable as exc:
        evidence.reason = str(exc)
        return evidence
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        evidence.reason = f"unexpected GitLab API shape: {exc!r}"
        return evidence

    if not evidence.pipelines:
        evidence.reason = "no CI pipeline ran at this exact commit"
        return evidence

    try:
        verdict, reason, jobs = judge(mapping, all_jobs)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        evidence.reason = f"unexpected GitLab job record: {exc!r}"
        return evidence
    evidence.verdict, evidence.reason, evidence.jobs = verdict, reason, jobs
    if verdict != PASSED:
        return evidence
    try:
        confirm(mapping, source, evidence)
    except EvidenceUnavailable as exc:
        evidence.verdict, evidence.reason = UNVERIFIED, str(exc)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        evidence.verdict = UNVERIFIED
        evidence.reason = f"unexpected GitLab API shape: {exc!r}"
    if evidence.verdict != PASSED:
        # A confirmation cut short leaves jobs it never finished marked passed.
        for job in evidence.jobs:
            if job.verdict == PASSED and not job.confirmed:
                job.verdict, job.reason = UNVERIFIED, evidence.reason
    return evidence


@dataclass
class SmokeResult:
    outcome: str
    duration_s: float
    detail: str
    python: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "duration_s": round(self.duration_s, 1),
            "detail": self.detail,
            "python": self.python,
        }


def _run_bounded(argv: Sequence[str], timeout: float, cwd: str,
                 env: Dict[str, str], text: bool = False
                 ) -> subprocess.CompletedProcess:
    """`subprocess.run` whose timeout reaches the whole process group:
    `run` kills only its direct child, and whatever a conftest started would
    keep running into the full gate that follows a timeout."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=cwd, env=env, text=text, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        raise
    return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)


def run_smoke(candidates: Sequence[str], collect_argv: Sequence[str],
              tools: Sequence[str], timeout: int, cwd: str,
              env: Dict[str, str],
              clock: Callable[[], float] = time.monotonic) -> SmokeResult:
    """Bounded readiness check: collection imports every test module and what
    it imports, without running a test. The whole check shares one deadline."""
    start = clock()
    # Collection must not write into the checkout it is vouching for:
    # __pycache__ in a repo that does not ignore it would dirty the tree the
    # git-status check reads next.
    env = {**env, "PYTHONDONTWRITEBYTECODE": "1"}

    def remaining() -> float:
        return timeout - (clock() - start)

    missing = [tool for tool in tools if shutil.which(tool, path=env.get("PATH")) is None]
    if missing:
        return SmokeResult(SMOKE_FAIL, clock() - start,
                           "missing tools on PATH: " + ", ".join(missing))

    python = None
    for candidate in candidates:
        budget = remaining()
        if budget <= 0:
            return SmokeResult(SMOKE_TIMEOUT, clock() - start,
                               f"no interpreter confirmed within {timeout}s")
        try:
            probe = _run_bounded([candidate, "-c", "import pytest"],
                                 min(30, budget), cwd, env)
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            continue
        if probe.returncode == 0:
            python = candidate
            break
    if python is None:
        if remaining() <= 0:
            return SmokeResult(SMOKE_TIMEOUT, clock() - start,
                               f"no interpreter confirmed within {timeout}s")
        return SmokeResult(SMOKE_FAIL, clock() - start,
                           "no interpreter can import pytest: " + ", ".join(candidates))

    budget = remaining()
    if budget <= 0:
        return SmokeResult(SMOKE_TIMEOUT, clock() - start,
                           f"exceeded {timeout}s before test collection", python)
    argv = [python, *collect_argv]
    try:
        result = _run_bounded(argv, budget, cwd, env, text=True)
    except subprocess.TimeoutExpired:
        return SmokeResult(SMOKE_TIMEOUT, clock() - start,
                           f"test collection exceeded {timeout}s", python)
    except OSError as exc:
        return SmokeResult(SMOKE_FAIL, clock() - start,
                           f"could not start test collection: {exc}", python)
    lines = [line.strip(" =") for line in
             ((result.stdout or "") + (result.stderr or "")).splitlines() if line.strip()]
    tail = lines[-1] if lines else "(no output)"
    if result.returncode != 0:
        return SmokeResult(SMOKE_FAIL, clock() - start,
                           f"test collection exited {result.returncode}: {tail}", python)
    return SmokeResult(SMOKE_PASS, clock() - start, f"collected: {tail}", python)
