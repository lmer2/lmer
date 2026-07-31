"""Tests for repository URL parsing functions."""

import pytest

from lmer_cli.cli import _derive_repo_url_from_task_target, _parse_repo_url
from lmer_cli.container.clone_and_exec import (
    _derive_repo_url_from_task_target as _derive_repo_url_container,
    _parse_gitlab_mr_url,
    sanitize_task_target as _container_sanitize_task_target,
)
from work_repo.utils import sanitize_task_target as _utils_sanitize_task_target


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


# Project names whose last character is one of `.`, `g`, `i`, `t`. The SSH branch
# used to remove the `.git` suffix with rstrip(".git"), which strips a character
# *class* and so ate those too: `group/project` came back as `group/projec`, and
# LMER_REPO_PROJECT — the run directory, the memory dir, the log path — was filed
# under the mangled name. Real projects in the work repo look like this
# (`docs/lmer-doc-bot`, `openpipes/openpipes.net`), so it is not a corner case.
#
# Both spellings are checked for every name because only the SSH branch was
# broken: the HTTPS branch removes the suffix with a regex. A test that passed on
# both would prove nothing about either.
GIT_CHAR_TAILED_PROJECTS = [
    "group/project",
    "docs/lmer-doc-bot",
    "openpipes/openpipes.net",
    "group/tooling",
]


@pytest.mark.parametrize("project", GIT_CHAR_TAILED_PROJECTS)
class TestProjectNamesEndingInGitCharacters:
    """A `.git` suffix is removed as a suffix, never as a set of characters."""

    def test_ssh_url(self, project):
        """The broken spelling: git@host:group/project kept only `group/projec`."""
        assert _parse_repo_url(f"git@gitlab.example.com:{project}") == (
            "gitlab.example.com", project,
        )

    def test_ssh_url_with_git_suffix(self, project):
        """And with the suffix actually present, which is the common spelling."""
        assert _parse_repo_url(f"git@gitlab.example.com:{project}.git") == (
            "gitlab.example.com", project,
        )

    def test_https_url(self, project):
        assert _parse_repo_url(f"https://gitlab.example.com/{project}") == (
            "gitlab.example.com", project,
        )

    def test_https_url_with_git_suffix(self, project):
        assert _parse_repo_url(f"https://gitlab.example.com/{project}.git") == (
            "gitlab.example.com", project,
        )

    def test_both_spellings_name_the_same_project(self, project):
        """The identity must not depend on which spelling reached the parser.

        ``lmer`` picks the spelling by whether it holds a token for the host
        (:func:`lmer_cli.cli._derive_repo_url_from_task_target` answers in SSH
        shape without one), so a project whose run directory changed name with the
        token state is the same bug seen from the outside.
        """
        assert _parse_repo_url(f"git@gitlab.example.com:{project}.git") == (
            _parse_repo_url(f"https://gitlab.example.com/{project}.git")
        )


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


# Both copies of the URL-derivation helper (host-side cli.py and the
# container clone_and_exec.py) must treat the newer GitLab "work_items"
# issue URL form identically to "/-/issues/" (issue #72).
@pytest.mark.parametrize(
    "derive",
    [
        pytest.param(_derive_repo_url_from_task_target, id="cli"),
        pytest.param(_derive_repo_url_container, id="container"),
    ],
)
class TestDeriveRepoUrlFromWorkItems:
    """work_items URLs derive a base repo URL just like /-/issues/ URLs."""

    def test_work_items_url_is_derivable(self, derive):
        # Before #72 the 'work_items' indicator was missing, so this returned
        # None and the raw work_items URL was wrongly used as a clone URL.
        url = "https://gitlab.example.com/group/project/-/work_items/70"
        assert derive(url) is not None

    def test_work_items_matches_issues(self, derive):
        """A work_items URL derives the same repo URL as the issues URL."""
        base = "https://gitlab.example.com/group/project"
        assert derive(f"{base}/-/work_items/70") == derive(f"{base}/-/issues/70")

    def test_nested_group_work_items(self, derive):
        """The full project path before /-/ is preserved for nested groups."""
        url = "https://gitlab.example.com/group/subgroup/project/-/work_items/12"
        derived = derive(url)
        assert derived is not None
        assert "group/subgroup/project" in derived

    def test_repo_named_work_items_is_not_derivable(self, derive):
        """A bare repo whose name merely contains 'work_items' is not a target.

        The 'work_items/' indicator carries a trailing slash so it only matches
        a real .../-/work_items/<id> resource path, mirroring the 'issues/'
        convention. A bare repo URL like .../group/work_items (or
        .../work_items_tracker) must derive None, not be misread as a resource
        link. Bare 'work_items' would have matched these via substring.
        """
        assert derive("https://gitlab.example.com/group/work_items") is None
        assert derive("https://gitlab.example.com/group/work_items_tracker") is None


# Both copies of sanitize_task_target (work_repo.utils and the container
# clone_and_exec) must normalize work_items URLs to the issue-<id> form.
@pytest.mark.parametrize(
    "sanitize",
    [
        pytest.param(_utils_sanitize_task_target, id="work_repo.utils"),
        pytest.param(_container_sanitize_task_target, id="container"),
    ],
)
class TestSanitizeWorkItemsParity:
    """work_items URLs normalize to issue-<id> in both sanitize copies."""

    def test_work_items_url(self, sanitize):
        url = "https://gitlab.example.com/group/project/-/work_items/70"
        assert sanitize(url) == "issue-70"

    def test_work_items_matches_issues(self, sanitize):
        base = "https://gitlab.example.com/group/project"
        assert sanitize(f"{base}/-/work_items/70") == sanitize(f"{base}/-/issues/70")
