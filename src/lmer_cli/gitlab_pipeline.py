"""
GitLab Pipeline Monitor - Watch and debug CI/CD pipelines.

This module provides a GitLab API client for monitoring pipelines and jobs,
as well as helper functions for formatting output and watching pipeline status.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

# Pre-compiled regex for stripping ANSI escape codes
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[mK]|\[0K")

# Status icons for job display
STATUS_ICONS = {
    "success": "✅",
    "failed": "❌",
    "running": "🔄",
    "pending": "⏳",
    "skipped": "⏭️",
    "canceled": "🚫",
    "manual": "👆",
}

DEFAULT_POLL_INTERVAL = 10


def _sanitize_hostname(host: str) -> str:
    """Sanitize a hostname into a valid environment variable suffix.

    Converts the hostname to lowercase and replaces dots/hyphens with underscores.
    Example: 'git.example.com' -> 'git_example_com'
    """
    return re.sub(r"[.\-]", "_", host.lower())


class GitLabPipelineError(Exception):
    """Base exception for GitLab pipeline operations."""

    pass


class TokenNotFoundError(GitLabPipelineError):
    """Raised when no API token is found."""

    def __init__(self, env_var: str):
        self.env_var = env_var
        super().__init__(f"No token found. Set {env_var} or GITLAB_TOKEN")


class ProjectNotFoundError(GitLabPipelineError):
    """Raised when a project cannot be found."""

    def __init__(self, project: str):
        self.project = project
        super().__init__(f"Could not find project {project}")


class PipelineNotFoundError(GitLabPipelineError):
    """Raised when a pipeline cannot be found."""

    def __init__(self, pipeline_id: int):
        self.pipeline_id = pipeline_id
        super().__init__(f"Could not fetch pipeline {pipeline_id}")


class MRPipelineNotFoundError(GitLabPipelineError):
    """Raised when no pipeline is found for a merge request."""

    def __init__(self, mr_id: int):
        self.mr_id = mr_id
        super().__init__(f"No pipeline found for MR {mr_id}")


class TraceNotFoundError(GitLabPipelineError):
    """Raised when a job trace cannot be fetched."""

    def __init__(self, job_id: int):
        self.job_id = job_id
        super().__init__(f"Could not fetch trace for job {job_id}")


class UnexpectedStatusError(GitLabPipelineError):
    """Raised when the pipeline status does not match --expect-status."""

    def __init__(self, status: Optional[str], expected: str):
        self.status = status
        self.expected = expected
        super().__init__(
            f"Pipeline status {status!r} does not match expected {expected!r}"
        )


class GitLabPipelineClient:
    """GitLab Pipeline API client."""

    def __init__(self, host: str, token: str):
        self.host = host
        self.token = token
        self.base_url = f"https://{host}/api/v4"

    def _request(
        self, endpoint: str, accept: str = "application/json"
    ) -> tuple[int, str]:
        """Make API request and return status code and response body."""
        url = f"{self.base_url}{endpoint}"
        req = urllib.request.Request(url)
        req.add_header("PRIVATE-TOKEN", self.token)
        req.add_header("Accept", accept)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")
        except urllib.error.URLError as e:
            return 0, str(e.reason)

    def _get_json(self, endpoint: str) -> Optional[dict[str, Any]]:
        """Make API request and return parsed JSON."""
        status, body = self._request(endpoint)
        if status == 200:
            return json.loads(body)
        return None

    def get_project_id(self, project: str) -> Optional[int]:
        """Get project ID from path."""
        encoded = urllib.parse.quote(project, safe="")
        data = self._get_json(f"/projects/{encoded}")
        return data.get("id") if data else None

    def get_pipeline(self, project_id: int, pipeline_id: int) -> Optional[dict]:
        """Get pipeline details."""
        return self._get_json(f"/projects/{project_id}/pipelines/{pipeline_id}")

    def get_mr_pipeline(self, project_id: int, mr_id: int) -> Optional[dict]:
        """Get pipeline for a merge request."""
        mr = self._get_json(f"/projects/{project_id}/merge_requests/{mr_id}")
        if mr and "head_pipeline" in mr:
            return mr["head_pipeline"]
        return None

    def get_jobs(self, project_id: int, pipeline_id: int) -> list[dict]:
        """Get jobs for a pipeline."""
        data = self._get_json(f"/projects/{project_id}/pipelines/{pipeline_id}/jobs")
        return data if data else []

    def get_job_trace(self, project_id: int, job_id: int) -> Optional[str]:
        """Get job trace/log. Requires Accept: text/plain header."""
        status, body = self._request(
            f"/projects/{project_id}/jobs/{job_id}/trace", accept="text/plain"
        )
        if status == 200:
            return body
        return None

    def get_job(self, project_id: int, job_id: int) -> Optional[dict]:
        """Get job details."""
        return self._get_json(f"/projects/{project_id}/jobs/{job_id}")


def get_token(host: str) -> str:
    """Get API token for host.

    Derives the environment variable name from the sanitized hostname.
    For example, 'git.example.com' checks GITLAB_TOKEN_git_example_com,
    then falls back to GITLAB_TOKEN.

    Args:
        host: GitLab host (e.g., 'gitlab.example.com')

    Returns:
        API token string

    Raises:
        TokenNotFoundError: If no token is found in environment
    """
    suffix = _sanitize_hostname(host)
    token = (
        os.environ.get(f"GITLAB_TOKEN_{suffix}")
        or os.environ.get("GITLAB_TOKEN")
    )
    if not token:
        raise TokenNotFoundError(f"GITLAB_TOKEN_{suffix}")
    return token


def strip_ansi(text: str) -> str:
    """Strip ANSI escape codes from text."""
    return ANSI_PATTERN.sub("", text)


def format_job_status(job: dict) -> str:
    """Format job status with icon."""
    status = job.get("status", "unknown")
    icon = STATUS_ICONS.get(status, "❓")
    return f"{icon} {job['name']:20} {status:12} {job.get('stage', '')}"


def watch_pipeline(
    client: GitLabPipelineClient,
    project_id: int,
    pipeline_id: int,
    output: Callable[[str], None] = print,
    poll_interval: int = DEFAULT_POLL_INTERVAL,
) -> str:
    """Watch pipeline until complete, show failed job traces.

    Args:
        client: GitLab API client
        project_id: Project ID
        pipeline_id: Pipeline ID to watch
        output: Function to call with output lines (default: print)
        poll_interval: Seconds between status checks (default: 10)

    Returns:
        Final pipeline status

    Raises:
        PipelineNotFoundError: If pipeline cannot be fetched
    """
    output(f"Watching pipeline {pipeline_id}...")
    last_status = None

    while True:
        pipeline = client.get_pipeline(project_id, pipeline_id)
        if not pipeline:
            raise PipelineNotFoundError(pipeline_id)

        status = pipeline.get("status")
        if status != last_status:
            output(f"\nPipeline status: {status}")
            jobs = client.get_jobs(project_id, pipeline_id)
            for job in sorted(
                jobs, key=lambda j: (j.get("stage", ""), j.get("name", ""))
            ):
                output(f"  {format_job_status(job)}")
            last_status = status

        if status in ("success", "failed", "canceled", "skipped"):
            output(f"\nPipeline finished: {status}")

            # Show traces for failed jobs
            if status == "failed":
                jobs = client.get_jobs(project_id, pipeline_id)
                failed = [j for j in jobs if j.get("status") == "failed"]
                for job in failed:
                    output(f"\n{'='*60}")
                    output(f"FAILED JOB: {job['name']} (id: {job['id']})")
                    output(f"{'='*60}")
                    trace = client.get_job_trace(project_id, job["id"])
                    if trace:
                        # Show last 50 lines, strip ANSI
                        lines = strip_ansi(trace).strip().split("\n")
                        output("\n".join(lines[-50:]))
                    else:
                        output("(Could not fetch trace)")
            return status

        time.sleep(poll_interval)


def show_trace(
    client: GitLabPipelineClient,
    project_id: int,
    job_id: int,
    output: Callable[[str], None] = print,
) -> str:
    """Show trace for a specific job.

    Args:
        client: GitLab API client
        project_id: Project ID
        job_id: Job ID
        output: Function to call with output lines (default: print)

    Returns:
        The job trace content

    Raises:
        TraceNotFoundError: If trace cannot be fetched
    """
    job = client.get_job(project_id, job_id)
    if job:
        output(f"Job: {job['name']} ({job['status']})")
        output(f"Stage: {job.get('stage', 'N/A')}")
        if job.get("failure_reason"):
            output(f"Failure reason: {job['failure_reason']}")
        output("=" * 60)

    trace = client.get_job_trace(project_id, job_id)
    if trace:
        cleaned = strip_ansi(trace)
        output(cleaned)
        return cleaned
    else:
        raise TraceNotFoundError(job_id)


def show_status(
    client: GitLabPipelineClient,
    project_id: int,
    pipeline_id: int,
    output: Callable[[str], None] = print,
) -> dict:
    """Show current pipeline status.

    Args:
        client: GitLab API client
        project_id: Project ID
        pipeline_id: Pipeline ID
        output: Function to call with output lines (default: print)

    Returns:
        Pipeline data dict

    Raises:
        PipelineNotFoundError: If pipeline cannot be fetched
    """
    pipeline = client.get_pipeline(project_id, pipeline_id)
    if not pipeline:
        raise PipelineNotFoundError(pipeline_id)

    output(f"Pipeline {pipeline_id}: {pipeline.get('status')}")
    output(f"URL: {pipeline.get('web_url', 'N/A')}")

    jobs = client.get_jobs(project_id, pipeline_id)
    if jobs:
        output("\nJobs:")
        for job in sorted(
            jobs, key=lambda j: (j.get("stage", ""), j.get("name", ""))
        ):
            output(f"  {format_job_status(job)}")

    return pipeline


def check_expected_status(status: Optional[str], expected: Optional[str]) -> None:
    """Enforce the --expect-status contract on a resolved pipeline status.

    A no-op when no expectation was given or when the status matches;
    otherwise raises UnexpectedStatusError (a GitLabPipelineError, so the
    CLI exits non-zero). This is what makes
    `work verify ... -- gitlab-pipeline ... --expect-status success` a real
    receipt: a red or still-running pipeline can never exit 0.

    Args:
        status: Resolved pipeline status (plain status mode or the final
            status from watch mode)
        expected: Expected status (e.g. 'success'), or None to skip

    Raises:
        UnexpectedStatusError: If expected is set and status differs
    """
    if expected is not None and status != expected:
        raise UnexpectedStatusError(status, expected)


def resolve_pipeline_id(
    client: GitLabPipelineClient,
    project_id: int,
    id_value: int,
    is_mr: bool = False,
    output: Callable[[str], None] = print,
) -> int:
    """Resolve a pipeline ID, handling MR lookups if needed.

    Args:
        client: GitLab API client
        project_id: Project ID
        id_value: Pipeline ID or MR ID
        is_mr: If True, treat id_value as an MR ID
        output: Function to call with output lines (default: print)

    Returns:
        Pipeline ID

    Raises:
        MRPipelineNotFoundError: If no pipeline found for MR
    """
    if is_mr:
        pipeline = client.get_mr_pipeline(project_id, id_value)
        if not pipeline:
            raise MRPipelineNotFoundError(id_value)
        pipeline_id = pipeline["id"]
        output(f"MR {id_value} -> Pipeline {pipeline_id}")
        return pipeline_id
    return id_value
