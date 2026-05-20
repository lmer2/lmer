#!/usr/bin/env python3
"""Tests for lmer_cli.gitlab_pipeline module"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from lmer_cli.gitlab_pipeline import (
    ANSI_PATTERN,
    STATUS_ICONS,
    GitLabPipelineClient,
    GitLabPipelineError,
    MRPipelineNotFoundError,
    PipelineNotFoundError,
    ProjectNotFoundError,
    TokenNotFoundError,
    TraceNotFoundError,
    _sanitize_hostname,
    format_job_status,
    get_token,
    resolve_pipeline_id,
    show_status,
    show_trace,
    strip_ansi,
    watch_pipeline,
)


class TestSanitizeHostname:
    """Test _sanitize_hostname function"""

    def test_dots_replaced(self):
        """Test dots are replaced with underscores"""
        assert _sanitize_hostname("git.example.com") == "git_example_com"

    def test_hyphens_replaced(self):
        """Test hyphens are replaced with underscores"""
        assert _sanitize_hostname("my-gitlab.example.com") == "my_gitlab_example_com"

    def test_lowercase(self):
        """Test hostname is lowercased"""
        assert _sanitize_hostname("Git.Example.COM") == "git_example_com"

    def test_simple_hostname(self):
        """Test simple hostname without dots"""
        assert _sanitize_hostname("localhost") == "localhost"


class TestConstants:
    """Test module constants"""

    def test_status_icons_all_defined(self):
        """Test all expected status icons are defined"""
        expected = ["success", "failed", "running", "pending", "skipped", "canceled", "manual"]
        for status in expected:
            assert status in STATUS_ICONS


class TestExceptions:
    """Test custom exception classes"""

    def test_token_not_found_error(self):
        """Test TokenNotFoundError message"""
        error = TokenNotFoundError("GITLAB_TOKEN")
        assert "GITLAB_TOKEN" in str(error)
        assert error.env_var == "GITLAB_TOKEN"

    def test_project_not_found_error(self):
        """Test ProjectNotFoundError message"""
        error = ProjectNotFoundError("myorg/myrepo")
        assert "myorg/myrepo" in str(error)
        assert error.project == "myorg/myrepo"

    def test_pipeline_not_found_error(self):
        """Test PipelineNotFoundError message"""
        error = PipelineNotFoundError(123)
        assert "123" in str(error)
        assert error.pipeline_id == 123

    def test_mr_pipeline_not_found_error(self):
        """Test MRPipelineNotFoundError message"""
        error = MRPipelineNotFoundError(42)
        assert "42" in str(error)
        assert error.mr_id == 42

    def test_trace_not_found_error(self):
        """Test TraceNotFoundError message"""
        error = TraceNotFoundError(789)
        assert "789" in str(error)
        assert error.job_id == 789

    def test_exceptions_inherit_from_base(self):
        """Test all exceptions inherit from GitLabPipelineError"""
        assert issubclass(TokenNotFoundError, GitLabPipelineError)
        assert issubclass(ProjectNotFoundError, GitLabPipelineError)
        assert issubclass(PipelineNotFoundError, GitLabPipelineError)
        assert issubclass(MRPipelineNotFoundError, GitLabPipelineError)
        assert issubclass(TraceNotFoundError, GitLabPipelineError)


class TestGetToken:
    """Test get_token function"""

    def test_get_token_with_sanitized_hostname(self):
        """Test getting token using sanitized hostname env var"""
        with patch.dict(os.environ, {"GITLAB_TOKEN_git_example_com": "test-token"}, clear=True):
            token = get_token("git.example.com")
            assert token == "test-token"

    def test_get_token_with_hyphenated_hostname(self):
        """Test token lookup with hyphens in hostname"""
        with patch.dict(os.environ, {"GITLAB_TOKEN_my_gitlab_example_com": "tok"}, clear=True):
            token = get_token("my-gitlab.example.com")
            assert token == "tok"

    def test_get_token_fallback_to_gitlab_token(self):
        """Test fallback to GITLAB_TOKEN for unknown hosts"""
        with patch.dict(os.environ, {"GITLAB_TOKEN": "fallback-token"}, clear=True):
            token = get_token("unknown.gitlab.com")
            assert token == "fallback-token"

    def test_get_token_fallback_when_specific_missing(self):
        """Test fallback to GITLAB_TOKEN when specific token missing"""
        with patch.dict(os.environ, {"GITLAB_TOKEN": "fallback-token"}, clear=True):
            token = get_token("gitlab.example.com")
            assert token == "fallback-token"

    def test_get_token_raises_when_not_found(self):
        """Test TokenNotFoundError when no token available"""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(TokenNotFoundError) as exc_info:
                get_token("gitlab.example.com")
            assert "GITLAB_TOKEN_gitlab_example_com" in str(exc_info.value)


class TestStripAnsi:
    """Test strip_ansi function"""

    def test_strip_ansi_removes_color_codes(self):
        """Test removing ANSI color codes"""
        text = "\x1b[32mgreen\x1b[0m normal"
        result = strip_ansi(text)
        assert result == "green normal"

    def test_strip_ansi_removes_multiple_codes(self):
        """Test removing multiple ANSI codes"""
        text = "\x1b[1m\x1b[31mbold red\x1b[0m"
        result = strip_ansi(text)
        assert result == "bold red"

    def test_strip_ansi_handles_plain_text(self):
        """Test plain text passes through unchanged"""
        text = "plain text without codes"
        result = strip_ansi(text)
        assert result == text

    def test_strip_ansi_handles_empty_string(self):
        """Test empty string handling"""
        assert strip_ansi("") == ""

    def test_strip_ansi_removes_clear_line_codes(self):
        """Test removing clear line codes"""
        text = "[0Kcleared line"
        result = strip_ansi(text)
        assert result == "cleared line"


class TestFormatJobStatus:
    """Test format_job_status function"""

    def test_format_success_job(self):
        """Test formatting successful job"""
        job = {"name": "test-job", "status": "success", "stage": "test"}
        result = format_job_status(job)
        assert "✅" in result
        assert "test-job" in result
        assert "success" in result
        assert "test" in result

    def test_format_failed_job(self):
        """Test formatting failed job"""
        job = {"name": "build", "status": "failed", "stage": "build"}
        result = format_job_status(job)
        assert "❌" in result
        assert "build" in result
        assert "failed" in result

    def test_format_running_job(self):
        """Test formatting running job"""
        job = {"name": "deploy", "status": "running", "stage": "deploy"}
        result = format_job_status(job)
        assert "🔄" in result
        assert "running" in result

    def test_format_unknown_status(self):
        """Test formatting job with unknown status"""
        job = {"name": "mystery", "status": "unknown-status", "stage": "test"}
        result = format_job_status(job)
        assert "❓" in result

    def test_format_job_without_stage(self):
        """Test formatting job without stage field"""
        job = {"name": "test", "status": "success"}
        result = format_job_status(job)
        assert "test" in result
        assert "success" in result


class TestGitLabPipelineClient:
    """Test GitLabPipelineClient class"""

    def test_client_initialization(self):
        """Test client initialization"""
        client = GitLabPipelineClient("gitlab.example.com", "test-token")
        assert client.host == "gitlab.example.com"
        assert client.token == "test-token"
        assert client.base_url == "https://gitlab.example.com/api/v4"

    @patch("urllib.request.urlopen")
    def test_get_project_id_success(self, mock_urlopen):
        """Test successful project ID lookup"""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"id": 123}).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        client = GitLabPipelineClient("gitlab.example.com", "test-token")
        project_id = client.get_project_id("myorg/myrepo")
        assert project_id == 123

    @patch("urllib.request.urlopen")
    def test_get_project_id_not_found(self, mock_urlopen):
        """Test project ID lookup when project not found"""
        from urllib.error import HTTPError

        mock_urlopen.side_effect = HTTPError(
            "url", 404, "Not Found", {}, None
        )

        client = GitLabPipelineClient("gitlab.example.com", "test-token")
        project_id = client.get_project_id("nonexistent/project")
        assert project_id is None

    @patch("urllib.request.urlopen")
    def test_get_pipeline_success(self, mock_urlopen):
        """Test successful pipeline fetch"""
        pipeline_data = {"id": 456, "status": "success", "web_url": "http://example.com"}
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps(pipeline_data).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        client = GitLabPipelineClient("gitlab.example.com", "test-token")
        pipeline = client.get_pipeline(123, 456)
        assert pipeline["id"] == 456
        assert pipeline["status"] == "success"

    @patch("urllib.request.urlopen")
    def test_get_jobs_success(self, mock_urlopen):
        """Test successful jobs fetch"""
        jobs_data = [
            {"id": 1, "name": "job1", "status": "success"},
            {"id": 2, "name": "job2", "status": "running"},
        ]
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps(jobs_data).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        client = GitLabPipelineClient("gitlab.example.com", "test-token")
        jobs = client.get_jobs(123, 456)
        assert len(jobs) == 2
        assert jobs[0]["name"] == "job1"

    @patch("urllib.request.urlopen")
    def test_get_job_trace_success(self, mock_urlopen):
        """Test successful job trace fetch"""
        trace_text = "Step 1: Building...\nStep 2: Testing...\nDone!"
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = trace_text.encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        client = GitLabPipelineClient("gitlab.example.com", "test-token")
        trace = client.get_job_trace(123, 789)
        assert "Building" in trace
        assert "Testing" in trace

    @patch("urllib.request.urlopen")
    def test_get_mr_pipeline_success(self, mock_urlopen):
        """Test successful MR pipeline fetch"""
        mr_data = {
            "id": 42,
            "head_pipeline": {"id": 789, "status": "running"},
        }
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps(mr_data).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        client = GitLabPipelineClient("gitlab.example.com", "test-token")
        pipeline = client.get_mr_pipeline(123, 42)
        assert pipeline["id"] == 789


class TestResolvePipelineId:
    """Test resolve_pipeline_id function"""

    def test_resolve_direct_pipeline_id(self):
        """Test resolving direct pipeline ID (not MR)"""
        client = MagicMock()
        output = MagicMock()

        result = resolve_pipeline_id(client, 123, 456, is_mr=False, output=output)
        assert result == 456
        client.get_mr_pipeline.assert_not_called()

    def test_resolve_mr_pipeline_id(self):
        """Test resolving pipeline ID from MR"""
        client = MagicMock()
        client.get_mr_pipeline.return_value = {"id": 789}
        output = MagicMock()

        result = resolve_pipeline_id(client, 123, 42, is_mr=True, output=output)
        assert result == 789
        client.get_mr_pipeline.assert_called_once_with(123, 42)

    def test_resolve_mr_pipeline_not_found(self):
        """Test MRPipelineNotFoundError when no pipeline for MR"""
        client = MagicMock()
        client.get_mr_pipeline.return_value = None
        output = MagicMock()

        with pytest.raises(MRPipelineNotFoundError) as exc_info:
            resolve_pipeline_id(client, 123, 42, is_mr=True, output=output)
        assert exc_info.value.mr_id == 42


class TestShowStatus:
    """Test show_status function"""

    def test_show_status_success(self):
        """Test showing pipeline status"""
        client = MagicMock()
        client.get_pipeline.return_value = {
            "id": 456,
            "status": "success",
            "web_url": "http://example.com/pipeline/456",
        }
        client.get_jobs.return_value = [
            {"id": 1, "name": "lint", "status": "success", "stage": "lint"},
            {"id": 2, "name": "test", "status": "success", "stage": "test"},
        ]

        output_lines = []
        result = show_status(client, 123, 456, output=output_lines.append)

        assert result["status"] == "success"
        assert any("success" in line for line in output_lines)
        assert any("lint" in line for line in output_lines)

    def test_show_status_pipeline_not_found(self):
        """Test PipelineNotFoundError when pipeline missing"""
        client = MagicMock()
        client.get_pipeline.return_value = None

        with pytest.raises(PipelineNotFoundError) as exc_info:
            show_status(client, 123, 456)
        assert exc_info.value.pipeline_id == 456


class TestShowTrace:
    """Test show_trace function"""

    def test_show_trace_success(self):
        """Test showing job trace"""
        client = MagicMock()
        client.get_job.return_value = {
            "id": 789,
            "name": "test-job",
            "status": "failed",
            "stage": "test",
            "failure_reason": "script_failure",
        }
        client.get_job_trace.return_value = "Running tests...\nTest failed!"

        output_lines = []
        result = show_trace(client, 123, 789, output=output_lines.append)

        assert "Test failed" in result
        assert any("test-job" in line for line in output_lines)
        assert any("failure_reason" in line or "script_failure" in line for line in output_lines)

    def test_show_trace_not_found(self):
        """Test TraceNotFoundError when trace missing"""
        client = MagicMock()
        client.get_job.return_value = {"id": 789, "name": "test", "status": "failed"}
        client.get_job_trace.return_value = None

        with pytest.raises(TraceNotFoundError) as exc_info:
            show_trace(client, 123, 789)
        assert exc_info.value.job_id == 789


class TestWatchPipeline:
    """Test watch_pipeline function"""

    def test_watch_pipeline_immediate_success(self):
        """Test watching pipeline that's already successful"""
        client = MagicMock()
        client.get_pipeline.return_value = {"id": 456, "status": "success"}
        client.get_jobs.return_value = [
            {"id": 1, "name": "test", "status": "success", "stage": "test"}
        ]

        output_lines = []
        result = watch_pipeline(
            client, 123, 456, output=output_lines.append, poll_interval=0
        )

        assert result == "success"
        assert any("finished" in line.lower() for line in output_lines)

    def test_watch_pipeline_shows_failed_traces(self):
        """Test that watch_pipeline shows traces for failed jobs"""
        client = MagicMock()
        client.get_pipeline.return_value = {"id": 456, "status": "failed"}
        client.get_jobs.return_value = [
            {"id": 1, "name": "passing", "status": "success", "stage": "test"},
            {"id": 2, "name": "failing", "status": "failed", "stage": "test"},
        ]
        client.get_job_trace.return_value = "Error: something went wrong"

        output_lines = []
        result = watch_pipeline(
            client, 123, 456, output=output_lines.append, poll_interval=0
        )

        assert result == "failed"
        assert any("FAILED JOB" in line for line in output_lines)
        client.get_job_trace.assert_called_once_with(123, 2)

    def test_watch_pipeline_not_found(self):
        """Test PipelineNotFoundError when pipeline missing"""
        client = MagicMock()
        client.get_pipeline.return_value = None

        with pytest.raises(PipelineNotFoundError):
            watch_pipeline(client, 123, 456, poll_interval=0)
