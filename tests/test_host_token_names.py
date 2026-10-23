"""Per-host token variable names are one-to-one with hosts (#544).

The old suffix turned dots and hyphens alike into ``_``, so ``git.example.com``
and ``git-example.com`` both read ``GITLAB_TOKEN_git_example_com``: a token set
for one host was sent to its lookalike. Hyphens now become ``__``; a valid
hostname holds no ``_`` and no empty label, so the mapping is injective, and
dot-only hosts keep the names they always had.

A hyphenated host's old name is refused rather than read — it is the dotted
host's name now — with one stderr notice naming the variable to rename it to.

The rule lives in lmer_cli.tokens; clone_and_exec.py runs standalone in the
container and keeps its own copy, so every assertion here runs against both,
and against each surface that builds or reads a per-host name.
"""

import os

import pytest

from github_reviewer import cli as gh_cli
from gitlab_reviewer.client import GitLabClient, GitLabError
from lmer_cli import ci_baseline, gates, gitlab_pipeline, tokens
from lmer_cli.container import clone_and_exec
from lmer_cli.gitlab_pipeline import TokenNotFoundError
from tests.conftest import strip_lmer_env

# Built by concatenation so no secret-shaped literal assignment exists here.
DOTTED_VALUE = "dotted-host-" + "N" * 12
HYPHENATED_VALUE = "hyphen-host-" + "M" * 12

DOTTED = "git.example.com"
HYPHENATED = "git-example.com"

#: host -> expected suffix. Hyphen, dot, mixed and uppercase forms.
SUFFIXES = {
    "git.example.com": "git_example_com",
    "git-example.com": "git__example_com",
    "Git-Example.COM": "git__example_com",
    "my-gitlab.example.com": "my__gitlab_example_com",
    "my.gitlab-example.com": "my_gitlab__example_com",
    "a-b.c-d.example": "a__b_c__d_example",
    "gitlab.example.invalid": "gitlab_example_invalid",
    "localhost": "localhost",
}

_PREFIXES = ("GITLAB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")


@pytest.fixture(autouse=True)
def isolated_token_env(monkeypatch):
    strip_lmer_env(monkeypatch)
    for key in list(os.environ):
        if key.startswith(_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    # Both copies dedupe notices process-wide; a leftover entry would silence
    # the very line a test asserts on.
    tokens._warned.clear()
    clone_and_exec._warned.clear()
    yield
    tokens._warned.clear()
    clone_and_exec._warned.clear()


def _pipeline_lookup(host):
    try:
        return gitlab_pipeline.get_token(host)
    except TokenNotFoundError:
        return None


#: (variable prefix, reader) for every place a per-host variable is read.
READERS = [
    pytest.param("GITLAB_TOKEN_", tokens._get_gitlab_token, id="tokens"),
    pytest.param("GITLAB_TOKEN_", clone_and_exec._get_gitlab_token, id="container"),
    pytest.param("GITLAB_TOKEN_", _pipeline_lookup, id="gitlab-pipeline"),
    pytest.param("GITLAB_TOKEN_", GitLabClient._resolve_token, id="gitlab-review"),
    pytest.param("GH_TOKEN_", gh_cli._resolve_token, id="github-review"),
    pytest.param(ci_baseline.EVIDENCE_TOKEN_PREFIX,
                 lambda host: ci_baseline.evidence_token(host) or None, id="ci-baseline"),
]


class TestOneMapping:
    @pytest.mark.parametrize("host, suffix", SUFFIXES.items())
    def test_both_copies_map_every_host_alike(self, host, suffix):
        assert tokens._sanitize_hostname(host) == suffix
        assert clone_and_exec._sanitize_hostname(host) == suffix
        assert (clone_and_exec._legacy_sanitize_hostname(host)
                == tokens._legacy_sanitize_hostname(host))

    def test_importers_use_the_shared_helper(self):
        assert gitlab_pipeline._sanitize_hostname is tokens._sanitize_hostname
        assert ci_baseline._sanitize_hostname is tokens._sanitize_hostname
        assert gh_cli._per_host_env is tokens._per_host_env

    def test_no_two_hosts_share_a_suffix(self):
        hosts = {host.lower() for host in SUFFIXES}
        assert len({tokens._sanitize_hostname(host) for host in hosts}) == len(hosts)

    def test_dot_only_hosts_keep_their_names(self):
        for host in ("gitlab.example.invalid", "git.example.com", "github.com"):
            assert tokens._sanitize_hostname(host) == tokens._legacy_sanitize_hostname(host)

    @pytest.mark.parametrize("host, suffix", SUFFIXES.items())
    def test_every_surface_names_the_same_variable(self, host, suffix):
        with pytest.raises(TokenNotFoundError) as exc_info:
            gitlab_pipeline.get_token(host)
        assert exc_info.value.env_var == f"GITLAB_TOKEN_{suffix}"
        with pytest.raises(GitLabError, match=f"Set GITLAB_TOKEN_{suffix} "):
            GitLabClient(host=host)
        assert ci_baseline.evidence_token_env(host) == f"LMER_CI_BASELINE_TOKEN_{suffix}"


class TestCollidingHosts:
    """git.example.com and git-example.com: one variable each, never crossed."""

    def test_distinct_variable_names(self):
        assert (tokens._sanitize_hostname(DOTTED)
                != tokens._sanitize_hostname(HYPHENATED))
        assert (ci_baseline.evidence_token_env(DOTTED)
                != ci_baseline.evidence_token_env(HYPHENATED))

    @pytest.mark.parametrize("prefix, lookup", READERS)
    def test_each_host_reads_only_its_own(self, prefix, lookup, monkeypatch):
        monkeypatch.setenv(prefix + "git_example_com", DOTTED_VALUE)
        monkeypatch.setenv(prefix + "git__example_com", HYPHENATED_VALUE)
        assert lookup(DOTTED) == DOTTED_VALUE
        assert lookup(HYPHENATED) == HYPHENATED_VALUE

    @pytest.mark.parametrize("prefix, lookup", READERS)
    def test_dotted_hosts_value_never_reaches_the_lookalike(
            self, prefix, lookup, monkeypatch, capsys):
        """The #544 leak: the dotted host's variable was the lookalike's too."""
        monkeypatch.setenv(prefix + "git_example_com", DOTTED_VALUE)
        assert lookup(HYPHENATED) is None
        assert lookup(DOTTED) == DOTTED_VALUE
        assert DOTTED_VALUE not in capsys.readouterr().err

    @pytest.mark.parametrize("prefix, lookup", READERS)
    def test_hyphenated_hosts_value_never_reaches_the_dotted_host(
            self, prefix, lookup, monkeypatch):
        monkeypatch.setenv(prefix + "git__example_com", HYPHENATED_VALUE)
        assert lookup(DOTTED) is None
        assert lookup(HYPHENATED) == HYPHENATED_VALUE


class TestOldNameRefused:
    @pytest.mark.parametrize("prefix, lookup", READERS)
    def test_old_name_alone_is_refused_with_a_rename_hint(
            self, prefix, lookup, monkeypatch, capsys):
        monkeypatch.setenv(prefix + "my_gitlab_example_com", HYPHENATED_VALUE)
        assert lookup("my-gitlab.example.com") is None
        assert capsys.readouterr().err == (
            f"⚠️  {prefix}my_gitlab_example_com not used for my-gitlab.example.com: "
            "that name now means my.gitlab.example.com only — if it holds the "
            f"token for my-gitlab.example.com, rename it to "
            f"{prefix}my__gitlab_example_com\n")

    @pytest.mark.parametrize("prefix, lookup", READERS)
    def test_notice_is_printed_once(self, prefix, lookup, monkeypatch, capsys):
        monkeypatch.setenv(prefix + "git_example_com", DOTTED_VALUE)
        lookup(HYPHENATED)
        lookup(HYPHENATED)
        assert capsys.readouterr().err.count("not used for git-example.com") == 1

    @pytest.mark.parametrize("prefix, lookup", READERS)
    def test_new_name_wins_silently(self, prefix, lookup, monkeypatch, capsys):
        monkeypatch.setenv(prefix + "git_example_com", DOTTED_VALUE)
        monkeypatch.setenv(prefix + "git__example_com", HYPHENATED_VALUE)
        assert lookup(HYPHENATED) == HYPHENATED_VALUE
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("prefix, lookup", READERS)
    def test_dot_only_host_reads_its_unchanged_name_silently(
            self, prefix, lookup, monkeypatch, capsys):
        monkeypatch.setenv(prefix + "gitlab_example_invalid", DOTTED_VALUE)
        assert lookup("gitlab.example.invalid") == DOTTED_VALUE
        assert capsys.readouterr().err == ""

    def test_refusal_does_not_widen_to_the_generic_token(self, monkeypatch):
        """Failing closed on the old name still leaves the #161 scope intact."""
        monkeypatch.setenv("GITLAB_TOKEN_git_example_com", DOTTED_VALUE)
        monkeypatch.setenv("GITLAB_TOKEN", HYPHENATED_VALUE)
        monkeypatch.setenv("LMER_GITLAB_TOKEN_HOST", DOTTED)
        assert tokens._get_gitlab_token(HYPHENATED) is None
        assert clone_and_exec._get_gitlab_token(HYPHENATED) is None

    def test_both_copies_print_the_same_text(self, monkeypatch, capsys):
        monkeypatch.setenv("GITLAB_TOKEN_git_example_com", DOTTED_VALUE)
        tokens._get_gitlab_token(HYPHENATED)
        host_line = capsys.readouterr().err
        clone_and_exec._get_gitlab_token(HYPHENATED)
        assert capsys.readouterr().err == host_line
        assert "GITLAB_TOKEN_git__example_com" in host_line


class TestGateEvidenceToken:
    """gate-check reads the CI-baseline evidence token through the same rule."""

    @pytest.fixture
    def clients(self, monkeypatch):
        made = []

        class Client:
            def __init__(self, host, value):
                made.append((host, value))

        monkeypatch.setattr(gates, "GitLabPipelineClient", Client)
        monkeypatch.setenv("GITLAB_TOKEN_git__example_com", HYPHENATED_VALUE)
        return made

    def test_old_evidence_name_is_refused(self, clients, monkeypatch, capsys):
        monkeypatch.setenv("LMER_CI_BASELINE_TOKEN_git_example_com", DOTTED_VALUE)
        source = gates.GateSystem()._baseline_source(HYPHENATED)
        assert not source.uses_evidence_token
        assert clients == [(HYPHENATED, HYPHENATED_VALUE)]
        assert "rename it to LMER_CI_BASELINE_TOKEN_git__example_com" in (
            capsys.readouterr().err)

    def test_new_evidence_name_is_used(self, clients, monkeypatch):
        monkeypatch.setenv("LMER_CI_BASELINE_TOKEN_git__example_com", DOTTED_VALUE)
        source = gates.GateSystem()._baseline_source(HYPHENATED)
        assert source.uses_evidence_token
        assert source.credential == "LMER_CI_BASELINE_TOKEN_git__example_com"
        assert clients == [(HYPHENATED, DOTTED_VALUE)]
