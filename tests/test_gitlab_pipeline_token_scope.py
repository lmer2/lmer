"""`gitlab_pipeline.get_token` honors the generic token's issuing host (#161).

The pipeline client kept its own unscoped `GITLAB_TOKEN` fallback, so a PAT
issued for the work-repo host was sent as PRIVATE-TOKEN to whatever host the
caller named. It now delegates to the shared lookup, which applies the same
rule as everywhere else: per-host entries first, generic token only for
LMER_GITLAB_TOKEN_HOST (defaulting to the LMER_WORK_REPO host), one stderr
notice per refusal. Refused-because-issued-elsewhere reads as "no token" here,
which is this module's existing TokenNotFoundError outcome.
"""

import pytest

from lmer_cli import tokens
from lmer_cli.gitlab_pipeline import TokenNotFoundError, get_token
from tests.conftest import strip_lmer_env

# Built by concatenation so no secret-shaped literal assignment exists here.
SAMPLE = "glpat-" + "N" * 20

_TOKEN_ENV = (
    "GITLAB_TOKEN",
    "GITLAB_TOKEN_git_example_com",
    "GITLAB_TOKEN_gitlab_other_com",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for name in _TOKEN_ENV:
        monkeypatch.delenv(name, raising=False)
    # Notices dedupe process-wide; a leftover entry would silence the line a
    # test below asserts on.
    tokens._warned.clear()
    yield
    tokens._warned.clear()


class TestPerHostLookup:
    """Unchanged: the host-specific entry wins and is never scoped away."""

    def test_sanitized_hostname_entry(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN_git_example_com", SAMPLE)
        assert get_token("git.example.com") == SAMPLE

    def test_per_host_entry_beats_a_generic_token_for_another_host(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", "generic-value")
        monkeypatch.setenv("LMER_GITLAB_TOKEN_HOST", "git.example.com")
        monkeypatch.setenv("GITLAB_TOKEN_gitlab_other_com", SAMPLE)
        assert get_token("gitlab.other.com") == SAMPLE


class TestGenericFallbackScope:
    def test_issuing_host_match_returns_the_token(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", SAMPLE)
        monkeypatch.setenv("LMER_GITLAB_TOKEN_HOST", "git.example.com")
        assert get_token("git.example.com") == SAMPLE

    def test_work_repo_host_is_the_default_issuing_host(self, monkeypatch):
        monkeypatch.setenv("GITLAB_TOKEN", SAMPLE)
        monkeypatch.setenv("LMER_WORK_REPO", "https://git.example.com/agents/work.git")
        assert get_token("git.example.com") == SAMPLE

    def test_mismatch_raises_and_names_the_per_host_variable(self, monkeypatch, capsys):
        monkeypatch.setenv("GITLAB_TOKEN", SAMPLE)
        monkeypatch.setenv("LMER_GITLAB_TOKEN_HOST", "git.example.com")
        with pytest.raises(TokenNotFoundError) as exc_info:
            get_token("gitlab.other.com")
        assert "GITLAB_TOKEN_gitlab_other_com" in str(exc_info.value)
        err = capsys.readouterr().err
        assert "GITLAB_TOKEN not used for gitlab.other.com" in err
        assert "issued for git.example.com" in err
        assert SAMPLE not in err

    def test_unknown_issuing_host_raises(self, monkeypatch, capsys):
        monkeypatch.setenv("GITLAB_TOKEN", SAMPLE)
        with pytest.raises(TokenNotFoundError):
            get_token("git.example.com")
        err = capsys.readouterr().err
        assert "issuing host unknown" in err
        assert SAMPLE not in err

    def test_no_token_at_all_raises_without_a_notice(self, capsys):
        # "No token" is a normal outcome, not a refusal — nothing to explain.
        with pytest.raises(TokenNotFoundError):
            get_token("git.example.com")
        assert capsys.readouterr().err == ""
