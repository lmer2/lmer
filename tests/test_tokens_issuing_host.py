"""The generic GITLAB_TOKEN is scoped to its issuing host (issue #161).

Before this, the last step of the token lookup handed the generic PAT to any
host that asked, so a GitLab credential was baked into `github.com` clone URLs
and sent as HTTP Basic auth to a third party. The rule asserted here: the
generic token applies only to the host named by LMER_GITLAB_TOKEN_HOST, which
defaults to the work-repo host from LMER_WORK_REPO; anything else is refused
with one stderr notice and falls through to an anonymous clone.

The container copy in clone_and_exec.py must behave identically — its docstring
mandates staying in sync — so the matrix runs against both implementations.
"""

import re
from pathlib import Path

import pytest

from lmer_cli import tokens
from lmer_cli.container import clone_and_exec
from lmer_cli.tokens import _get_gitlab_token, _inject_gitlab_token_if_available
from lmer_cli.container.clone_and_exec import _get_gitlab_token as container_get
from tests.conftest import strip_lmer_env


#: Provider tokens the lookup consults; cleared so a developer's real shell
#: environment cannot satisfy (or defeat) any assertion here.
_TOKEN_ENV = (
    "GITLAB_TOKEN",
    "GITLAB_TOKEN_worklog",
    "GITLAB_TOKEN_git_example_com",
    "GITLAB_TOKEN_github_com",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)

_STUB = "glpat-notarealcredential"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in _TOKEN_ENV:
        monkeypatch.delenv(name, raising=False)
    # Both copies dedupe notices process-wide; a leftover entry would silence
    # the very line the next test asserts on.
    tokens._warned.clear()
    clone_and_exec._warned.clear()
    yield
    tokens._warned.clear()
    clone_and_exec._warned.clear()


#: Both implementations of the lookup, exercised over the same matrix.
lookups = pytest.mark.parametrize("lookup", [_get_gitlab_token, container_get],
                                  ids=["host", "container"])


class TestWorkRepoHostDefault:
    """With no explicit setting, the work-repo host is the issuing host."""

    @lookups
    def test_https_work_repo_host_gets_the_token(self, lookup, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        monkeypatch.setenv("LMER_WORK_REPO", "https://git.example.com/agents/work.git")
        assert lookup("git.example.com") == _STUB

    @lookups
    def test_ssh_work_repo_host_gets_the_token(self, lookup, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        monkeypatch.setenv("LMER_WORK_REPO", "git@git.example.com:agents/work.git")
        assert lookup("git.example.com") == _STUB

    @lookups
    def test_credentialed_https_work_repo_host_gets_the_token(self, lookup, monkeypatch):
        # The form LMER_WORK_REPO takes inside the container: the host CLI has
        # already injected the work-repo credential into the URL.
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        monkeypatch.setenv(
            "LMER_WORK_REPO", f"https://oauth2:{_STUB}@git.example.com/agents/work.git"
        )
        assert lookup("git.example.com") == _STUB

    @lookups
    def test_host_match_is_case_insensitive(self, lookup, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        monkeypatch.setenv("LMER_WORK_REPO", "https://Git.Example.COM/agents/work.git")
        assert lookup("git.example.com") == _STUB

    @lookups
    def test_third_party_host_is_refused(self, lookup, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        monkeypatch.setenv("LMER_WORK_REPO", "https://git.example.com/agents/work.git")
        assert lookup("github.com") is None

    @lookups
    def test_unparseable_work_repo_is_unknown(self, lookup, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        monkeypatch.setenv("LMER_WORK_REPO", "/srv/local/work")
        assert lookup("git.example.com") is None


class TestExplicitIssuingHost:
    """LMER_GITLAB_TOKEN_HOST names the issuing host directly."""

    @lookups
    def test_match_returns_the_token(self, lookup, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        monkeypatch.setenv("LMER_GITLAB_TOKEN_HOST", "git.example.com")
        assert lookup("git.example.com") == _STUB

    @lookups
    def test_mismatch_returns_none(self, lookup, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        monkeypatch.setenv("LMER_GITLAB_TOKEN_HOST", "git.example.com")
        assert lookup("gitlab.other.com") is None

    @lookups
    def test_explicit_setting_beats_the_work_repo_default(self, lookup, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        monkeypatch.setenv("LMER_GITLAB_TOKEN_HOST", "gitlab.other.com")
        monkeypatch.setenv("LMER_WORK_REPO", "https://git.example.com/agents/work.git")
        assert lookup("gitlab.other.com") == _STUB
        assert lookup("git.example.com") is None

    @lookups
    def test_case_insensitive(self, lookup, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        monkeypatch.setenv("LMER_GITLAB_TOKEN_HOST", "GIT.EXAMPLE.COM")
        assert lookup("git.example.com") == _STUB


class TestUnknownIssuingHost:
    """Neither signal available: the generic fallback is off, not permissive."""

    @lookups
    def test_returns_none(self, lookup, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        assert lookup("git.example.com") is None

    @lookups
    def test_more_specific_lookups_still_win(self, lookup, monkeypatch):
        # Scoping is the LAST step only: a per-host entry for a third-party
        # host is an explicit operator decision and is honored as before.
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        monkeypatch.setenv("GITLAB_TOKEN_git_example_com", "per-host-value")
        assert lookup("git.example.com") == "per-host-value"

    @lookups
    def test_github_family_fallback_still_applies(self, lookup, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        monkeypatch.setenv("GH_TOKEN", "gh-value")
        assert lookup("github.com") == "gh-value"


class TestNotices:
    """One clear stderr line per refusal, printed at most once per process."""

    @lookups
    def test_unknown_issuing_host_notice(self, lookup, monkeypatch, capsys):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        assert lookup("github.com") is None
        err = capsys.readouterr().err
        assert "GITLAB_TOKEN not used for github.com" in err
        assert "issuing host unknown" in err
        assert "LMER_GITLAB_TOKEN_HOST" in err
        assert "GITLAB_TOKEN_github_com" in err
        assert _STUB not in err

    @lookups
    def test_mismatch_notice_names_both_hosts(self, lookup, monkeypatch, capsys):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        monkeypatch.setenv("LMER_GITLAB_TOKEN_HOST", "git.example.com")
        assert lookup("github.com") is None
        err = capsys.readouterr().err
        assert "GITLAB_TOKEN not used for github.com" in err
        assert "issued for git.example.com" in err
        assert _STUB not in err

    @lookups
    def test_notice_is_printed_once_per_process(self, lookup, monkeypatch, capsys):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        lookup("github.com")
        lookup("github.com")
        err = capsys.readouterr().err
        assert err.count("GITLAB_TOKEN not used for github.com") == 1

    @lookups
    def test_no_notice_when_no_generic_token(self, lookup, monkeypatch, capsys):
        # "No token" (anonymous clone) is a normal outcome, not a refusal.
        assert lookup("github.com") is None
        assert capsys.readouterr().err == ""

    @lookups
    def test_no_notice_when_the_token_applies(self, lookup, monkeypatch, capsys):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        monkeypatch.setenv("LMER_GITLAB_TOKEN_HOST", "git.example.com")
        assert lookup("git.example.com") == _STUB
        assert capsys.readouterr().err == ""

    def test_both_copies_print_the_same_text(self, monkeypatch, capsys):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        _get_gitlab_token("github.com")
        host_line = capsys.readouterr().err
        clone_and_exec._warned.clear()
        container_get("github.com")
        assert capsys.readouterr().err == host_line


class TestUrlInjection:
    """The URL helpers are where the credential would have leaked."""

    def test_github_url_stays_uncredentialed(self, monkeypatch, capsys):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        monkeypatch.setenv("LMER_WORK_REPO", "https://git.example.com/agents/work.git")
        url = "https://github.com/org/repo"
        assert _inject_gitlab_token_if_available(url) == url
        assert _STUB not in _inject_gitlab_token_if_available(url)
        assert "GITLAB_TOKEN not used for github.com" in capsys.readouterr().err

    def test_work_repo_host_url_is_still_credentialed(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        monkeypatch.setenv("LMER_WORK_REPO", "git@git.example.com:agents/work.git")
        assert _inject_gitlab_token_if_available("https://git.example.com/org/repo") == (
            f"https://oauth2:{_STUB}@git.example.com/org/repo.git"
        )

    def test_per_host_entry_credentials_a_third_party_host(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", _STUB)
        monkeypatch.setenv("LMER_WORK_REPO", "https://git.example.com/agents/work.git")
        monkeypatch.setenv("GITLAB_TOKEN_github_com", "per-host-value")
        assert _inject_gitlab_token_if_available("https://github.com/org/repo") == (
            "https://oauth2:per-host-value@github.com/org/repo.git"
        )


class TestContainerEnvPassthrough:
    """Guard: LMER_GITLAB_TOKEN_HOST must reach the container.

    The container runs its own copy of the lookup, so without this entry an
    explicit issuing host would apply on the host side only and every
    in-container clone would see "issuing host unknown".
    """

    def test_cli_env_dict_declares_gitlab_token_host(self):
        cli_py = Path(__file__).parent.parent / "src" / "lmer_cli" / "cli.py"
        source = cli_py.read_text()
        pattern = re.compile(
            r"""["']LMER_GITLAB_TOKEN_HOST["']\s*:\s*os\.environ\.get\(\s*"""
            r"""["']LMER_GITLAB_TOKEN_HOST["']\s*\)"""
        )
        assert pattern.search(source), \
            "LMER_GITLAB_TOKEN_HOST entry missing from cli.py container env dict"
