#!/usr/bin/env python3
"""Tests for work_repo.utils module"""

import pytest

from work_repo.utils import sanitize_task_target


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
