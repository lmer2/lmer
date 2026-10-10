"""Tests for repository URL parsing functions."""

import os

import pytest

from lmer_cli.cli import _derive_repo_url_from_task_target, _parse_repo_url
from lmer_cli.container.clone_and_exec import (
    _derive_repo_url_from_task_target as _derive_repo_url_container,
    _parse_gitlab_mr_url,
    sanitize_task_target as _container_sanitize_task_target,
)
from work_repo.utils import sanitize_task_target as _utils_sanitize_task_target

#: A token in the shape a host token has, and the variable the host-specific
#: lookup reads it from.
HOST_TOKEN = "glpat-not-a-real-token-000"
TOKEN_ENV = "GITLAB_TOKEN_gitlab_example_com"

#: #391: an id-less list page. ``LMER_REPO_PROJECT`` read ``group/project/-/issues``.
LIST_PAGE = "https://gitlab.example.com/group/project/-/issues"
#: #523: a document inside the repository. Derived nothing, so the document
#: URL itself was handed to the clone.
DOCUMENT_URL = "https://gitlab.example.com/group/project/-/blob/main/docs/guide.md"


@pytest.fixture
def no_host_tokens(monkeypatch):
    """No token for any host: the derivation answers in SSH shape.

    Ambient ``GITLAB_TOKEN*`` is not an ``LMER_`` name, so nothing strips it
    for a developer machine that has one — a test about the token-less
    spelling has to say so.
    """
    for name in list(os.environ):
        if name.startswith(("GITLAB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def host_token(monkeypatch, no_host_tokens):
    """Exactly one host token, for gitlab.example.com."""
    monkeypatch.setenv(TOKEN_ENV, HOST_TOKEN)
    return HOST_TOKEN


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


class TestParseRepoUrlOfAResourceUrl:
    """``_parse_repo_url`` parses ``repository_of(url) or url`` (spec §2).

    So ``LMER_REPO_PROJECT`` — the run directory, the memory dir, the log path
    — never contains a ``/-/`` route (#391) or a ``/pull/N`` tail, whichever
    forge URL the launch was given. The repository-URL cases above are
    unchanged: a repository is its own answer.
    """

    @pytest.mark.parametrize("url", [
        LIST_PAGE,
        f"{LIST_PAGE}/",
        f"{LIST_PAGE}/?label_name=bug",
        "https://gitlab.example.com/group/project/-/merge_requests",
        "https://gitlab.example.com/group/project/-/work_items",
    ])
    def test_a_list_page_parses_to_the_project(self, url):
        assert _parse_repo_url(url) == ("gitlab.example.com", "group/project")

    @pytest.mark.parametrize("url", [
        DOCUMENT_URL,
        "https://gitlab.example.com/group/project/-/tree/main",
        "https://gitlab.example.com/group/project/-/wikis/home",
        "https://gitlab.example.com/group/project/-/commits/main",
    ])
    def test_a_document_url_parses_to_the_project(self, url):
        assert _parse_repo_url(url) == ("gitlab.example.com", "group/project")

    def test_a_work_item_url_parses_to_the_project(self):
        assert _parse_repo_url(
            "https://gitlab.example.com/group/project/-/issues/141"
        ) == ("gitlab.example.com", "group/project")

    @pytest.mark.parametrize("url", [
        "https://github.com/owner/repo/pull/9",
        "https://github.com/owner/repo/issues",
        "https://github.com/owner/repo/blob/main/README.md",
        "https://github.com/owner/repo/tree/main/docs",
    ])
    def test_a_github_route_parses_to_owner_repo(self, url):
        assert _parse_repo_url(url) == ("github.com", "owner/repo")

    def test_nested_groups_port_and_credentials_in_a_resource_url(self):
        assert _parse_repo_url(
            f"https://oauth2:{HOST_TOKEN}@gitlab.example.com:8443/group/subgroup/project/-/issues/12"
        ) == ("gitlab.example.com", "group/subgroup/project")

    def test_the_verify_line(self):
        """The two calls the task's verify step makes, pinned here as well."""
        assert _parse_repo_url("https://gitlab.example.com/g/p/-/issues") == (
            "gitlab.example.com", "g/p",
        )
        assert _parse_repo_url("https://gitlab.example.com/g/p/-/blob/main/x.md") == (
            "gitlab.example.com", "g/p",
        )


# Project names whose last character is one of `.`, `g`, `i`, `t`. The SSH branch
# used to remove the `.git` suffix with rstrip(".git"), which strips a character
# *class* and so ate those too: `group/project` came back as `group/projec`, and
# LMER_REPO_PROJECT — the run directory, the memory dir, the log path — was filed
# under the mangled name. The shapes are ordinary rather than contrived:
# hyphenated tool-style names end in `-bot`, and a project named after a domain
# ends in `.net`, so it is not a corner case.
#
# Both spellings are checked for every name because only the SSH branch was
# broken: the HTTPS branch removes the suffix with a regex. A test that passed on
# both would prove nothing about either.
GIT_CHAR_TAILED_PROJECTS = [
    "group/project",
    "group/example-doc-bot",
    "group/example.net",
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


@pytest.mark.parametrize(
    "derive",
    [
        pytest.param(_derive_repo_url_from_task_target, id="cli"),
        pytest.param(_derive_repo_url_container, id="container"),
    ],
)
class TestDeriveRepoUrlFromReferenceUrls:
    """A list page (#391) and a document URL (#523) derive the repository they
    belong to, in today's two shapes, from both copies of the helper.

    The shape is decided as before: a GitLab ``/-/`` URL answers in HTTPS with
    the host token when there is one and in SSH form otherwise; the GitHub
    shape is always SSH and never looks a token up.
    """

    def test_list_page_without_a_token(self, derive, no_host_tokens):
        assert derive(LIST_PAGE) == "git@gitlab.example.com:group/project"

    def test_document_url_without_a_token(self, derive, no_host_tokens):
        assert derive(DOCUMENT_URL) == "git@gitlab.example.com:group/project"

    def test_list_page_with_a_host_token(self, derive, host_token):
        assert derive(LIST_PAGE) == (
            f"https://oauth2:{host_token}@gitlab.example.com/group/project.git"
        )

    def test_document_url_with_a_host_token(self, derive, host_token):
        assert derive(DOCUMENT_URL) == (
            f"https://oauth2:{host_token}@gitlab.example.com/group/project.git"
        )

    def test_a_document_url_on_a_custom_port_keeps_it(self, derive, host_token):
        """A non-default HTTPS port survives the credential insert."""
        url = "https://gitlab.example.com:8443/group/project/-/blob/main/notes.md"
        assert derive(url) == (
            f"https://oauth2:{host_token}@gitlab.example.com:8443/group/project.git"
        )

    def test_a_reference_derives_what_the_work_item_does(self, derive, host_token):
        base = "https://gitlab.example.com/group/project"
        assert derive(f"{base}/-/blob/main/docs/guide.md") == derive(f"{base}/-/issues/141")
        assert derive(f"{base}/-/issues") == derive(f"{base}/-/issues/141")

    def test_github_document_url(self, derive, no_host_tokens):
        assert derive("https://github.com/owner/repo/blob/main/README.md") == (
            "git@github.com:owner/repo"
        )

    def test_github_shape_never_looks_a_token_up(self, derive, monkeypatch, no_host_tokens):
        """Today's GitHub answer, kept: ``_get_gitlab_token`` would read
        ``GH_TOKEN`` for github.com and refuse a generic ``GITLAB_TOKEN`` with
        a notice — neither of which this helper has ever done."""
        monkeypatch.setenv("GH_TOKEN", "ghp-not-a-real-token")
        monkeypatch.setenv("GITLAB_TOKEN_github_com", "glpat-not-for-github")
        assert derive("https://github.com/owner/repo/pull/9") == "git@github.com:owner/repo"
        assert derive("https://github.com/owner/repo/tree/main") == "git@github.com:owner/repo"

    @pytest.mark.parametrize("url", [
        "https://gitlab.example.com/group/project",
        "https://gitlab.example.com/group/project.git",
        "git@gitlab.example.com:group/project.git",
        "ssh://git@gitlab.example.com/group/project.git",
        "https://github.com/owner/repo",
    ])
    def test_a_repository_url_still_derives_nothing(self, derive, url, host_token):
        """The fallback contract: spawn, clone_secondary_mr and
        _resolve_clone_url use the target itself as the clone URL."""
        assert derive(url) is None

    @pytest.mark.parametrize("url", [
        "https://gitlab.example.com/group/project/-/pipelines/1691",
        "https://docs.example.com/guide/getting-started",
        "https://myworkspace.slack.com/archives/C0123ABCDEF/p1700000000123456",
        "feature/some-branch",
        "",
    ])
    def test_nothing_else_derives(self, derive, url, host_token):
        assert derive(url) is None

    def test_the_derived_url_parses_to_what_parse_repo_url_reads_from_the_target(
        self, derive, host_token
    ):
        """One identity: the clone URL and ``LMER_REPO_PROJECT`` agree."""
        for url in (LIST_PAGE, DOCUMENT_URL):
            assert _parse_repo_url(derive(url)) == _parse_repo_url(url)


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
