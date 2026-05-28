#!/usr/bin/env python3
"""Tests for work_repo.utils module"""

from pathlib import Path

import pytest

from work_repo.utils import (
    project_info_dir,
    sanitize_task_target,
    task_info_dir,
    task_target_dir,
)

# Env vars the info-dir helpers read; cleared before each helper test so the
# host environment can't leak real LMER_* values into assertions.
_INFO_DIR_ENV_VARS = (
    "LMER_WORK_REPO_PATH",
    "LMER_REPO_HOST",
    "LMER_REPO_PROJECT",
    "LMER_TASK",
    "LMER_TASK_TARGET",
)


class TestSanitizeTaskTarget:
    """Test sanitize_task_target function"""

    @pytest.mark.parametrize("input_value", ["", None])
    def test_empty_or_none_returns_default(self, input_value):
        """Test empty string or None returns default"""
        assert sanitize_task_target(input_value) == "default"

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://gitlab.example.com/group/project/-/merge_requests/756", "mr-756"),
            ("https://gitlab.com/group/project/-/merge_requests/123?view=diff", "mr-123"),
            ("https://gitlab.com/group/project/-/merge_requests/456", "mr-456"),
        ],
    )
    def test_gitlab_mr_urls(self, url, expected):
        """Test GitLab merge request URLs"""
        assert sanitize_task_target(url) == expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://gitlab.com/group/project/-/issues/456", "issue-456"),
            ("https://gitlab.com/group/project/-/issues/789?view=details", "issue-789"),
        ],
    )
    def test_gitlab_issue_urls(self, url, expected):
        """Test GitLab issue URLs"""
        assert sanitize_task_target(url) == expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://github.com/owner/repo/pull/123", "pr-123"),
            ("https://github.com/owner/repo/pull/456?tab=files", "pr-456"),
        ],
    )
    def test_github_pr_urls(self, url, expected):
        """Test GitHub pull request URLs"""
        assert sanitize_task_target(url) == expected

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://github.com/owner/repo/issues/789", "issue-789"),
            ("https://github.com/owner/repo/issues/101", "issue-101"),
        ],
    )
    def test_github_issue_urls(self, url, expected):
        """Test GitHub issue URLs"""
        assert sanitize_task_target(url) == expected

    @pytest.mark.parametrize(
        "input_value,expected",
        [
            ("feature/new-feature", "feature-new-feature"),
            ("feature/new@feature#123", "feature-new-feature-123"),
            ("bugfix/fix-123", "bugfix-fix-123"),
        ],
    )
    def test_branch_names(self, input_value, expected):
        """Test branch name sanitization"""
        assert sanitize_task_target(input_value) == expected

    @pytest.mark.parametrize(
        "sha",
        [
            "abc123def456",
            "a1b2c3d4e5f6",
            "1234567890abcdef",
        ],
    )
    def test_commit_shas(self, sha):
        """Test commit SHA handling"""
        assert sanitize_task_target(sha) == sha

    @pytest.mark.parametrize(
        "input_value",
        [
            "test---multiple----dashes",
            "test--double--dashes",
            "---leading-dashes",
            "trailing-dashes---",
        ],
    )
    def test_multiple_dashes_collapsed(self, input_value):
        """Test multiple dashes are collapsed"""
        result = sanitize_task_target(input_value)
        assert "--" not in result

    @pytest.mark.parametrize(
        "input_value",
        [
            "-test-",
            "--test--",
            "-leading",
            "trailing-",
        ],
    )
    def test_leading_trailing_dashes_removed(self, input_value):
        """Test leading and trailing dashes are removed"""
        result = sanitize_task_target(input_value)
        assert not result.startswith("-")
        assert not result.endswith("-")

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("http://example.com/path/to/resource", "resource"),
            ("https://example.com/path/to/resource", "resource"),
            ("http://example.com/path/to/resource?query=value", "resource"),
            ("https://example.com/path/to/resource#fragment", "resource"),
        ],
    )
    def test_url_fallback(self, url, expected):
        """Test URL fallback behavior for unrecognized URLs"""
        assert sanitize_task_target(url) == expected


@pytest.fixture
def clean_info_env(monkeypatch):
    """Clear all LMER_* env vars the info-dir helpers consult."""
    for name in _INFO_DIR_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class TestInfoDirHelpers:
    """Test project_info_dir / task_info_dir / task_target_dir helpers."""

    @pytest.mark.parametrize(
        "helper", [project_info_dir, task_info_dir, task_target_dir]
    )
    def test_returns_none_when_host_and_project_unset(self, clean_info_env, helper):
        """All helpers return None when host/project env vars are absent."""
        assert helper() is None

    @pytest.mark.parametrize(
        "helper", [project_info_dir, task_info_dir, task_target_dir]
    )
    def test_returns_none_when_only_host_set(self, clean_info_env, helper):
        """Host without project is insufficient -> None."""
        clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
        assert helper() is None

    @pytest.mark.parametrize(
        "helper", [project_info_dir, task_info_dir, task_target_dir]
    )
    def test_returns_none_when_only_project_set(self, clean_info_env, helper):
        """Project without host is insufficient -> None."""
        clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        assert helper() is None

    def test_project_info_dir_path(self, clean_info_env):
        """project_info_dir assembles {work}/{host}/{project}/info."""
        clean_info_env.setenv("LMER_WORK_REPO_PATH", "/wr")
        clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
        clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        assert project_info_dir() == Path("/wr/git.example.com/group/proj/info")

    def test_task_info_dir_path(self, clean_info_env):
        """task_info_dir assembles {work}/{host}/{project}/{task}/info."""
        clean_info_env.setenv("LMER_WORK_REPO_PATH", "/wr")
        clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
        clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        clean_info_env.setenv("LMER_TASK", "develop")
        assert task_info_dir() == Path("/wr/git.example.com/group/proj/develop/info")

    def test_task_target_dir_path(self, clean_info_env):
        """task_target_dir assembles {work}/{host}/{project}/{task}/{target}."""
        clean_info_env.setenv("LMER_WORK_REPO_PATH", "/wr")
        clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
        clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        clean_info_env.setenv("LMER_TASK", "develop")
        clean_info_env.setenv("LMER_TASK_TARGET", "my-branch")
        assert task_target_dir() == Path(
            "/wr/git.example.com/group/proj/develop/my-branch"
        )

    def test_work_repo_path_defaults_to_work(self, clean_info_env):
        """LMER_WORK_REPO_PATH defaults to /work when unset."""
        clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
        clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        assert project_info_dir() == Path("/work/git.example.com/group/proj/info")

    def test_task_and_target_default_to_default(self, clean_info_env):
        """LMER_TASK and LMER_TASK_TARGET default to 'default' when unset."""
        clean_info_env.setenv("LMER_WORK_REPO_PATH", "/wr")
        clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
        clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        assert task_info_dir() == Path(
            "/wr/git.example.com/group/proj/default/info"
        )
        assert task_target_dir() == Path(
            "/wr/git.example.com/group/proj/default/default"
        )

    def test_task_target_dir_sanitizes_target(self, clean_info_env):
        """task_target_dir passes LMER_TASK_TARGET through sanitize_task_target."""
        clean_info_env.setenv("LMER_WORK_REPO_PATH", "/wr")
        clean_info_env.setenv("LMER_REPO_HOST", "git.example.com")
        clean_info_env.setenv("LMER_REPO_PROJECT", "group/proj")
        clean_info_env.setenv("LMER_TASK", "review")
        clean_info_env.setenv(
            "LMER_TASK_TARGET",
            "https://git.example.com/group/proj/-/merge_requests/756",
        )
        assert task_target_dir() == Path(
            "/wr/git.example.com/group/proj/review/mr-756"
        )
