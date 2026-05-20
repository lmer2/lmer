"""Tests for repository URL parsing functions."""

from lmer_cli.cli import _parse_repo_url
from lmer_cli.container.clone_and_exec import _parse_gitlab_mr_url


class TestParseRepoUrl:
    """Tests for _parse_repo_url function."""

    def test_https_url(self):
        """Should extract host and project from HTTPS URL."""
        host, project = _parse_repo_url("https://gitlab.example.com/agents/global.git")
        assert host == "gitlab.example.com"
        assert project == "agents/global"

    def test_https_url_without_git_suffix(self):
        """Should handle HTTPS URL without .git suffix."""
        host, project = _parse_repo_url("https://github.com/owner/repo")
        assert host == "github.com"
        assert project == "owner/repo"

    def test_ssh_url(self):
        """Should extract host and project from SSH URL."""
        host, project = _parse_repo_url("git@gitlab.example.com:agents/global.git")
        assert host == "gitlab.example.com"
        assert project == "agents/global"

    def test_ssh_url_without_git_suffix(self):
        """Should handle SSH URL without .git suffix."""
        host, project = _parse_repo_url("git@github.com:owner/repo")
        assert host == "github.com"
        assert project == "owner/repo"

    def test_https_url_with_oauth_credentials(self):
        """Should strip OAuth credentials from HTTPS URL host."""
        host, project = _parse_repo_url(
            "https://oauth2:glpat-abc123@gitlab.example.com/agents/global.git"
        )
        assert host == "gitlab.example.com"
        assert project == "agents/global"

    def test_https_url_with_user_password(self):
        """Should strip user:password credentials from HTTPS URL host."""
        host, project = _parse_repo_url(
            "https://user:password@gitlab.example.com/group/project.git"
        )
        assert host == "gitlab.example.com"
        assert project == "group/project"

    def test_https_url_with_token_in_user(self):
        """Should strip token-as-username from HTTPS URL host."""
        host, project = _parse_repo_url(
            "https://glpat-token123@gitlab.com/org/repo.git"
        )
        assert host == "gitlab.com"
        assert project == "org/repo"

    def test_nested_groups(self):
        """Should handle nested GitLab groups."""
        host, project = _parse_repo_url(
            "https://gitlab.com/group/subgroup/project.git"
        )
        assert host == "gitlab.com"
        assert project == "group/subgroup/project"

    def test_empty_url(self):
        """Should return None, None for empty URL."""
        host, project = _parse_repo_url("")
        assert host is None
        assert project is None

    def test_none_url(self):
        """Should return None, None for None URL."""
        host, project = _parse_repo_url(None)
        assert host is None
        assert project is None

    def test_invalid_url(self):
        """Should return None, None for invalid URL."""
        host, project = _parse_repo_url("not-a-url")
        assert host is None
        assert project is None

    def test_url_with_only_host(self):
        """Should return None, None for URL with no path."""
        host, project = _parse_repo_url("https://gitlab.com/")
        assert host is None
        assert project is None

    def test_https_url_with_port(self):
        """Should handle HTTPS URL with explicit port."""
        host, project = _parse_repo_url(
            "https://gitlab.example.com:8443/group/project.git"
        )
        assert host == "gitlab.example.com"
        assert project == "group/project"

    def test_https_url_with_port_and_credentials(self):
        """Should strip credentials and handle port correctly."""
        host, project = _parse_repo_url(
            "https://oauth2:token@gitlab.example.com:8443/group/project.git"
        )
        assert host == "gitlab.example.com"
        assert project == "group/project"


class TestParseGitlabMrUrl:
    """Tests for _parse_gitlab_mr_url function."""

    def test_standard_mr_url(self):
        """Should parse a standard GitLab MR URL."""
        host, project, mr_id = _parse_gitlab_mr_url(
            "https://gitlab.example.com/group/project/-/merge_requests/123"
        )
        assert host == "gitlab.example.com"
        assert project == "group/project"
        assert mr_id == "123"

    def test_mr_url_with_oauth_credentials(self):
        """Should strip OAuth credentials from MR URL host."""
        host, project, mr_id = _parse_gitlab_mr_url(
            "https://oauth2:glpat-abc123@gitlab.example.com/agents/global/-/merge_requests/42"
        )
        assert host == "gitlab.example.com"
        assert project == "agents/global"
        assert mr_id == "42"

    def test_mr_url_with_user_password(self):
        """Should strip user:password credentials from MR URL host."""
        host, project, mr_id = _parse_gitlab_mr_url(
            "https://user:pass@gitlab.example.com/org/repo/-/merge_requests/99"
        )
        assert host == "gitlab.example.com"
        assert project == "org/repo"
        assert mr_id == "99"

    def test_mr_url_with_port(self):
        """Should handle MR URL with explicit port."""
        host, project, mr_id = _parse_gitlab_mr_url(
            "https://gitlab.example.com:8443/group/project/-/merge_requests/55"
        )
        assert host == "gitlab.example.com"
        assert project == "group/project"
        assert mr_id == "55"

    def test_non_mr_url(self):
        """Should return None tuple for non-MR URLs."""
        result = _parse_gitlab_mr_url("https://gitlab.example.com/group/project")
        assert result == (None, None, None)

    def test_empty_url(self):
        """Should return None tuple for empty URL."""
        result = _parse_gitlab_mr_url("")
        assert result == (None, None, None)
