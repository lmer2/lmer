"""Tests for GitLab token injection in lmer CLI."""

import os
from unittest.mock import patch

from lmer_cli.tokens import (
    _get_gitlab_token,
    _convert_ssh_to_https_if_token_available,
    _inject_gitlab_token_if_available,
    _prefer_ssh,
    _sanitize_hostname,
)


class TestSanitizeHostname:
    """Tests for _sanitize_hostname function."""

    def test_dots_to_underscores(self):
        """Test dots are replaced with underscores."""
        assert _sanitize_hostname("git.example.com") == "git_example_com"

    def test_hyphens_to_underscores(self):
        """Test hyphens are replaced with underscores."""
        assert _sanitize_hostname("my-gitlab.example.com") == "my_gitlab_example_com"

    def test_lowercase(self):
        """Test hostname is lowercased."""
        assert _sanitize_hostname("Git.Example.COM") == "git_example_com"


class TestGetGitlabToken:
    """Tests for _get_gitlab_token function."""

    def test_host_specific_token(self):
        """Should return GITLAB_TOKEN_{sanitized_host} for the given host."""
        with patch.dict(os.environ, {"GITLAB_TOKEN_git_example_com": "token-example"}, clear=True):
            token = _get_gitlab_token("git.example.com")
            assert token == "token-example"

    def test_host_specific_token_alternate_host(self):
        """Should return host-specific token for another host."""
        with patch.dict(os.environ, {"GITLAB_TOKEN_gitlab_myorg_com": "token-myorg"}, clear=True):
            token = _get_gitlab_token("gitlab.myorg.com")
            assert token == "token-myorg"

    def test_fallback_to_gitlab_token(self):
        """Should fall back to GITLAB_TOKEN when host-specific not found."""
        env = {"GITLAB_TOKEN": "generic-token"}
        with patch.dict(os.environ, env, clear=True):
            token = _get_gitlab_token("unknown.host.com")
            assert token == "generic-token"

    def test_no_token_available(self):
        """Should return None when no token is available."""
        with patch.dict(os.environ, {}, clear=True):
            token = _get_gitlab_token("git.example.com")
            assert token is None


class TestGetGitlabTokenWorkRepo:
    """Tests for _get_gitlab_token with for_work_repo=True."""

    def test_worklog_token_takes_highest_priority(self):
        """GITLAB_TOKEN_worklog should take priority over host-specific tokens."""
        env = {
            "GITLAB_TOKEN_worklog": "worklog-token",
            "GITLAB_TOKEN_git_example_com": "host-token",
            "GITLAB_TOKEN": "generic-token",
        }
        with patch.dict(os.environ, env, clear=True):
            token = _get_gitlab_token("git.example.com", for_work_repo=True)
            assert token == "worklog-token"

    def test_falls_back_to_host_specific_if_no_worklog(self):
        """Should fall back to host-specific token if no worklog token."""
        env = {
            "GITLAB_TOKEN_git_example_com": "host-token",
        }
        with patch.dict(os.environ, env, clear=True):
            token = _get_gitlab_token("git.example.com", for_work_repo=True)
            assert token == "host-token"

    def test_worklog_not_used_when_for_work_repo_false(self):
        """Should not use worklog token when for_work_repo=False."""
        env = {
            "GITLAB_TOKEN_worklog": "worklog-token",
            "GITLAB_TOKEN_git_example_com": "host-token",
        }
        with patch.dict(os.environ, env, clear=True):
            token = _get_gitlab_token("git.example.com", for_work_repo=False)
            assert token == "host-token"


class TestPreferSsh:
    """Tests for _prefer_ssh function and REPO_AUTH_PREFER_SSH behavior."""

    def test_prefer_ssh_when_set_to_1(self):
        """Should return True when REPO_AUTH_PREFER_SSH=1."""
        with patch.dict(os.environ, {"REPO_AUTH_PREFER_SSH": "1"}, clear=True):
            assert _prefer_ssh() is True

    def test_prefer_ssh_when_set_to_true(self):
        """Should return True when REPO_AUTH_PREFER_SSH=true."""
        with patch.dict(os.environ, {"REPO_AUTH_PREFER_SSH": "true"}, clear=True):
            assert _prefer_ssh() is True

    def test_prefer_ssh_when_set_to_yes(self):
        """Should return True when REPO_AUTH_PREFER_SSH=yes."""
        with patch.dict(os.environ, {"REPO_AUTH_PREFER_SSH": "yes"}, clear=True):
            assert _prefer_ssh() is True

    def test_prefer_ssh_case_insensitive(self):
        """Should be case insensitive."""
        with patch.dict(os.environ, {"REPO_AUTH_PREFER_SSH": "TRUE"}, clear=True):
            assert _prefer_ssh() is True

    def test_prefer_ssh_when_not_set(self):
        """Should return False when not set."""
        with patch.dict(os.environ, {}, clear=True):
            assert _prefer_ssh() is False

    def test_prefer_ssh_when_set_to_0(self):
        """Should return False when REPO_AUTH_PREFER_SSH=0."""
        with patch.dict(os.environ, {"REPO_AUTH_PREFER_SSH": "0"}, clear=True):
            assert _prefer_ssh() is False

    def test_convert_ssh_skips_token_when_prefer_ssh(self):
        """Should skip token conversion when REPO_AUTH_PREFER_SSH is set."""
        env = {
            "REPO_AUTH_PREFER_SSH": "1",
            "GITLAB_TOKEN_git_example_com": "mytoken",
        }
        with patch.dict(os.environ, env, clear=True):
            result = _convert_ssh_to_https_if_token_available("git@git.example.com:group/project")
            assert result == "git@git.example.com:group/project"  # SSH preserved

    def test_convert_ssh_uses_token_when_prefer_ssh_not_set(self):
        """Should use token when REPO_AUTH_PREFER_SSH is not set."""
        env = {
            "GITLAB_TOKEN_git_example_com": "mytoken",
        }
        with patch.dict(os.environ, env, clear=True):
            result = _convert_ssh_to_https_if_token_available("git@git.example.com:group/project")
            assert result == "https://oauth2:mytoken@git.example.com/group/project.git"


class TestConvertSshToHttpsWorkRepo:
    """Tests for _convert_ssh_to_https_if_token_available with for_work_repo=True."""

    def test_uses_worklog_token_for_work_repo(self):
        """Should use worklog token when for_work_repo=True."""
        env = {
            "GITLAB_TOKEN_worklog": "worklog-token",
            "GITLAB_TOKEN_git_example_com": "host-token",
        }
        with patch.dict(os.environ, env, clear=True):
            result = _convert_ssh_to_https_if_token_available(
                "git@git.example.com:agents/work", for_work_repo=True
            )
            assert result == "https://oauth2:worklog-token@git.example.com/agents/work.git"

    def test_uses_host_token_when_not_work_repo(self):
        """Should use host-specific token when for_work_repo=False."""
        env = {
            "GITLAB_TOKEN_worklog": "worklog-token",
            "GITLAB_TOKEN_git_example_com": "host-token",
        }
        with patch.dict(os.environ, env, clear=True):
            result = _convert_ssh_to_https_if_token_available(
                "git@git.example.com:agents/work", for_work_repo=False
            )
            assert result == "https://oauth2:host-token@git.example.com/agents/work.git"


class TestConvertSshToHttps:
    """Tests for _convert_ssh_to_https_if_token_available function."""

    def test_converts_ssh_to_https_with_token(self):
        """Should convert SSH URL to HTTPS with token when available."""
        env = {"GITLAB_TOKEN_git_example_com": "mytoken"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("REPO_AUTH_PREFER_SSH", None)
            result = _convert_ssh_to_https_if_token_available("git@git.example.com:group/project")
            assert result == "https://oauth2:mytoken@git.example.com/group/project.git"

    def test_keeps_ssh_without_token(self):
        """Should keep SSH URL when no token available."""
        with patch.dict(os.environ, {}, clear=True):
            result = _convert_ssh_to_https_if_token_available("git@git.example.com:group/project")
            assert result == "git@git.example.com:group/project"

    def test_passes_through_non_ssh_urls(self):
        """Should pass through non-SSH URLs unchanged."""
        result = _convert_ssh_to_https_if_token_available("https://github.com/org/repo")
        assert result == "https://github.com/org/repo"

    def test_handles_empty_input(self):
        """Should handle empty string input."""
        result = _convert_ssh_to_https_if_token_available("")
        assert result == ""

    def test_adds_git_suffix(self):
        """Should add .git suffix if missing."""
        with patch.dict(os.environ, {"GITLAB_TOKEN": "tok"}, clear=True):
            result = _convert_ssh_to_https_if_token_available("git@gitlab.com:group/project")
            assert result.endswith(".git")

    def test_preserves_existing_git_suffix(self):
        """Should not double .git suffix."""
        with patch.dict(os.environ, {"GITLAB_TOKEN": "tok"}, clear=True):
            result = _convert_ssh_to_https_if_token_available("git@gitlab.com:group/project.git")
            assert result == "https://oauth2:tok@gitlab.com/group/project.git"


class TestInjectGitlabToken:
    """Tests for _inject_gitlab_token_if_available function."""

    def test_injects_token_into_https_url(self):
        """Should inject token into plain HTTPS URL."""
        with patch.dict(os.environ, {"GITLAB_TOKEN_git_example_com": "mytoken"}, clear=False):
            result = _inject_gitlab_token_if_available("https://git.example.com/group/project.git")
            assert result == "https://oauth2:mytoken@git.example.com/group/project.git"

    def test_converts_ssh_to_https_with_token(self):
        """Should convert SSH URL to HTTPS with token."""
        env = {"GITLAB_TOKEN_git_example_com": "mytoken"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("REPO_AUTH_PREFER_SSH", None)
            result = _inject_gitlab_token_if_available("git@git.example.com:group/project")
            assert result == "https://oauth2:mytoken@git.example.com/group/project.git"

    def test_keeps_url_if_no_token(self):
        """Should keep URL unchanged when no token available."""
        with patch.dict(os.environ, {}, clear=True):
            result = _inject_gitlab_token_if_available("https://git.example.com/group/project.git")
            assert result == "https://git.example.com/group/project.git"

    def test_keeps_url_if_credentials_present(self):
        """Should not override existing credentials in URL."""
        with patch.dict(os.environ, {"GITLAB_TOKEN": "newtoken"}, clear=True):
            result = _inject_gitlab_token_if_available("https://user:pass@git.example.com/group/project.git")
            assert result == "https://user:pass@git.example.com/group/project.git"

    def test_passes_through_http_urls(self):
        """Should pass through non-HTTPS URLs unchanged (except SSH)."""
        result = _inject_gitlab_token_if_available("http://github.com/org/repo")
        assert result == "http://github.com/org/repo"

    def test_passes_through_file_urls(self):
        """Should pass through file:// URLs unchanged."""
        result = _inject_gitlab_token_if_available("file:///path/to/repo")
        assert result == "file:///path/to/repo"

    def test_handles_empty_input(self):
        """Should handle empty string input."""
        result = _inject_gitlab_token_if_available("")
        assert result == ""

    def test_adds_git_suffix_to_https(self):
        """Should add .git suffix if missing from HTTPS URL."""
        with patch.dict(os.environ, {"GITLAB_TOKEN": "tok"}, clear=True):
            result = _inject_gitlab_token_if_available("https://gitlab.com/group/project")
            assert result == "https://oauth2:tok@gitlab.com/group/project.git"

    def test_works_with_github_no_token(self):
        """Should keep GitHub URLs unchanged when no token for that host."""
        with patch.dict(os.environ, {"GITLAB_TOKEN": "gitlab-only"}, clear=True):
            # GITLAB_TOKEN would apply to github.com too since it's a fallback
            result = _inject_gitlab_token_if_available("https://github.com/org/repo")
            # Token gets injected since GITLAB_TOKEN is a fallback for all hosts
            assert "oauth2:gitlab-only" in result

    def test_nested_groups(self):
        """Should handle GitLab nested groups correctly."""
        with patch.dict(os.environ, {"GITLAB_TOKEN": "tok"}, clear=True):
            result = _inject_gitlab_token_if_available("https://gitlab.com/group/subgroup/project.git")
            assert result == "https://oauth2:tok@gitlab.com/group/subgroup/project.git"
