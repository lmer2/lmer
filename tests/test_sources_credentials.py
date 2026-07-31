"""Tests for credential derivation and redaction in sources.py.

Derivation (spec N2/N3) borrows the work-repo URL's auth for a declared
same-host source — three branches: HTTPS userinfo copied over, SSH-form
conversion, and the anonymous marker for credential-less work repos (the
caller uses it to drive the loud declared-source failure path). Redaction
follows the clone_and_exec._scrub_credentials pattern extended to scp-form
URLs; no helper output may ever carry a derived token value.
"""

from lmer_cli.container.sources import (
    ANONYMOUS_MODES,
    DERIVED_ANONYMOUS,
    DERIVED_ANONYMOUS_OTHER_PORT,
    DERIVED_HTTPS_USERINFO,
    DERIVED_SSH,
    derive_clone_url,
    env_matches_declared,
    scrub_credentials,
)

# Fake token value (glpat- shape, not a real secret).
FAKE_TOKEN = "glpat-FAKEtoken1234567890"
DECLARED = "https://git.example.com/agents/taskdefs.git"
WORK_HTTPS_TOKENIZED = f"https://oauth2:{FAKE_TOKEN}@git.example.com/20c/worklog.git"
WORK_HTTPS_ANON = "https://git.example.com/20c/worklog.git"
WORK_SCP = "git@git.example.com:20c/worklog.git"
WORK_SSH_SCHEME = "ssh://git@git.example.com/20c/worklog.git"


class TestHttpsUserinfoBranch:
    def test_userinfo_copied_onto_declared_url(self):
        url, mode = derive_clone_url(DECLARED, WORK_HTTPS_TOKENIZED)
        assert mode == DERIVED_HTTPS_USERINFO
        assert url == (
            f"https://oauth2:{FAKE_TOKEN}@git.example.com/agents/taskdefs.git"
        )

    def test_basic_userinfo_copied(self):
        work = "https://user:hunter2@git.example.com/20c/worklog.git"
        url, mode = derive_clone_url(DECLARED, work)
        assert mode == DERIVED_HTTPS_USERINFO
        assert url.startswith("https://user:hunter2@git.example.com/")

    def test_scp_declared_rebuilt_on_https_scheme(self):
        # The credential only applies over HTTPS, so an scp-form declared
        # spelling is rebuilt on the work repo's scheme.
        url, mode = derive_clone_url(
            "git@git.example.com:agents/taskdefs.git", WORK_HTTPS_TOKENIZED
        )
        assert mode == DERIVED_HTTPS_USERINFO
        assert url == (
            f"https://oauth2:{FAKE_TOKEN}@git.example.com/agents/taskdefs.git"
        )


class TestSshBranch:
    def test_scp_work_repo_converts_declared_to_scp(self):
        url, mode = derive_clone_url(DECLARED, WORK_SCP)
        assert mode == DERIVED_SSH
        assert url == "git@git.example.com:agents/taskdefs.git"

    def test_ssh_scheme_work_repo_converts_declared_to_ssh_scheme(self):
        url, mode = derive_clone_url(DECLARED, WORK_SSH_SCHEME)
        assert mode == DERIVED_SSH
        assert url == "ssh://git@git.example.com/agents/taskdefs.git"

    def test_scp_declared_with_scp_work_repo_passes_through_form(self):
        url, mode = derive_clone_url(
            "git@git.example.com:agents/taskdefs.git", WORK_SCP
        )
        assert mode == DERIVED_SSH
        assert url == "git@git.example.com:agents/taskdefs.git"


class TestPortMismatchWithholdsTheCredential:
    """Iteration-5 review finding: the trust key is hostname-only, so "same
    host" now admits any port — a registry on 5050, an app on 8080. Only the
    https-userinfo branch carries the declared authority through, so it is
    the one place a declaration could aim `oauth2:<token>` at a service the
    work-repo clone never authenticated against. The credential is withheld
    there and the declared URL is attempted anonymously.
    """

    OTHER_PORT = "https://git.example.com:9999/attacker/x.git"

    def test_declared_other_port_gets_no_credential(self):
        url, mode = derive_clone_url(self.OTHER_PORT, WORK_HTTPS_TOKENIZED)
        assert mode == DERIVED_ANONYMOUS_OTHER_PORT
        assert url == self.OTHER_PORT
        assert FAKE_TOKEN not in url

    def test_withheld_mode_counts_as_anonymous(self):
        # Consumers test membership in ANONYMOUS_MODES; a bare
        # `== DERIVED_ANONYMOUS` check would report this clone as credentialed.
        assert DERIVED_ANONYMOUS_OTHER_PORT in ANONYMOUS_MODES
        assert DERIVED_ANONYMOUS in ANONYMOUS_MODES

    def test_explicit_default_port_still_matches(self):
        # :443 spelled out is the same authority as no port at all.
        url, mode = derive_clone_url(
            "https://git.example.com:443/agents/taskdefs.git", WORK_HTTPS_TOKENIZED
        )
        assert mode == DERIVED_HTTPS_USERINFO
        assert f"oauth2:{FAKE_TOKEN}@" in url

    def test_plain_http_declaration_gets_no_credential(self):
        # http's effective port is 80, not the work repo's 443 — and sending
        # basic auth in the clear would be the worse half of this bug.
        url, mode = derive_clone_url(
            "http://git.example.com/agents/taskdefs.git", WORK_HTTPS_TOKENIZED
        )
        assert mode == DERIVED_ANONYMOUS_OTHER_PORT
        assert FAKE_TOKEN not in url

    def test_ported_work_repo_matches_its_own_port(self):
        work = f"https://oauth2:{FAKE_TOKEN}@git.example.com:8443/20c/worklog.git"
        url, mode = derive_clone_url(
            "https://git.example.com:8443/agents/taskdefs.git", work
        )
        assert mode == DERIVED_HTTPS_USERINFO
        assert url == f"https://oauth2:{FAKE_TOKEN}@git.example.com:8443/agents/taskdefs.git"

    def test_ported_work_repo_declines_the_default_port(self):
        work = f"https://oauth2:{FAKE_TOKEN}@git.example.com:8443/20c/worklog.git"
        url, mode = derive_clone_url(DECLARED, work)
        assert mode == DERIVED_ANONYMOUS_OTHER_PORT
        assert url == DECLARED

    def test_scp_declaration_inherits_the_work_repo_port(self):
        # An scp-form declaration names no port, so the work repo's authority
        # is the only one either side asserts — it must survive the rebuild.
        work = f"https://oauth2:{FAKE_TOKEN}@git.example.com:8443/20c/worklog.git"
        url, mode = derive_clone_url("git@git.example.com:agents/taskdefs.git", work)
        assert mode == DERIVED_HTTPS_USERINFO
        assert url == f"https://oauth2:{FAKE_TOKEN}@git.example.com:8443/agents/taskdefs.git"

    def test_ssh_branch_is_unaffected(self):
        # DERIVED_SSH rebuilds on the work repo's port and discards the
        # declared one, so it never carried this exposure.
        url, mode = derive_clone_url(
            self.OTHER_PORT, "ssh://git@git.example.com:2222/20c/worklog.git"
        )
        assert mode == DERIVED_SSH
        assert url == "ssh://git@git.example.com:2222/attacker/x.git"


class TestEnvMatchesDeclared:
    """The row-2 predicate: "is this env value the credentialed form of this
    declaration?" — shared by the container matrix and host --show-env.
    """

    WORK_SSH_2222 = "ssh://git@git.example.com:2222/org/work.git"
    DECLARED_HTTPS = "https://git.example.com/agents/taskdefs.git"

    def test_direct_normalized_match(self):
        assert env_matches_declared(
            f"https://oauth2:{FAKE_TOKEN}@git.example.com/agents/taskdefs.git",
            self.DECLARED_HTTPS,
            WORK_HTTPS_TOKENIZED,
        )

    def test_derived_form_matches_across_a_custom_ssh_port(self):
        # The reproduced defect: the env var holds the working ssh://…:2222
        # form, the declaration the clean https URL. normalize_repo_url keeps
        # the port (deliberately), so only the derived-URL comparison folds it.
        assert env_matches_declared(
            "ssh://git@git.example.com:2222/agents/taskdefs.git",
            self.DECLARED_HTTPS,
            self.WORK_SSH_2222,
        )

    def test_a_different_repo_is_still_a_mismatch(self):
        assert not env_matches_declared(
            "ssh://git@git.example.com:2222/agents/other.git",
            self.DECLARED_HTTPS,
            self.WORK_SSH_2222,
        )

    def test_a_different_port_is_still_a_mismatch(self):
        # Only the port the derivation itself produces is folded; an
        # unrelated port still means "possibly a different repo".
        assert not env_matches_declared(
            "https://git.example.com:9999/agents/taskdefs.git",
            self.DECLARED_HTTPS,
            self.WORK_SSH_2222,
        )

    def test_without_a_work_repo_url_only_the_direct_comparison_runs(self):
        assert env_matches_declared(
            "git@git.example.com:agents/taskdefs.git", self.DECLARED_HTTPS
        )
        assert not env_matches_declared(
            "ssh://git@git.example.com:2222/agents/taskdefs.git", self.DECLARED_HTTPS
        )


class TestAnonymousBranch:
    def test_credential_less_work_repo_returns_declared_unchanged(self):
        url, mode = derive_clone_url(DECLARED, WORK_HTTPS_ANON)
        assert url == DECLARED

    def test_anonymous_marker_is_explicit(self):
        # The marker is the caller's hook for the loud declared-source
        # failure path (never the aux-clone warn-and-continue).
        _, mode = derive_clone_url(DECLARED, WORK_HTTPS_ANON)
        assert mode == DERIVED_ANONYMOUS
        assert mode != DERIVED_HTTPS_USERINFO
        assert mode != DERIVED_SSH


class TestScrubCredentials:
    def test_scrubs_scheme_url_userinfo(self):
        text = f"git clone {WORK_HTTPS_TOKENIZED} failed"
        scrubbed = scrub_credentials(text)
        assert FAKE_TOKEN not in scrubbed
        assert "git.example.com" in scrubbed

    def test_scrubs_scp_form_credential(self):
        # scp-form credentials have no :// anchor — the extension over the
        # clone_and_exec pattern.
        text = f"fatal: could not read from oauth2:{FAKE_TOKEN}@git.example.com:a/b.git"
        scrubbed = scrub_credentials(text)
        assert FAKE_TOKEN not in scrubbed
        assert "git.example.com" in scrubbed

    def test_preserves_clean_text(self):
        text = "fatal: repository 'https://git.example.com/x.git' not found"
        assert scrub_credentials(text) == text

    def test_handles_empty(self):
        assert scrub_credentials("") == ""

    def test_derived_urls_render_scrubbed(self):
        # Any derived URL, wrapped by the redaction helper, is safe to log.
        for work in (WORK_HTTPS_TOKENIZED, WORK_SCP, WORK_SSH_SCHEME, WORK_HTTPS_ANON):
            url, _ = derive_clone_url(DECLARED, work)
            assert FAKE_TOKEN not in scrub_credentials(url)
            assert FAKE_TOKEN not in scrub_credentials(f"source taskdef: {url} (declared)")

    def test_scrubs_secret_containing_a_slash(self):
        # Iteration-4 review finding: `[^/\s]*` ended the match at the "/"
        # inside the secret, so the whole credential survived redaction. The
        # base64 alphabet has a "/", so this is an ordinary token shape.
        slashed = "glpat-FAKE/token/123"
        text = f"git clone https://oauth2:{slashed}@git.example.com/a/b.git failed"
        scrubbed = scrub_credentials(text)
        assert slashed not in scrubbed
        assert "git.example.com" in scrubbed

    def test_scrubs_scp_form_secret_containing_a_slash(self):
        slashed = "FAKE/token/123"
        text = f"fatal: could not read from user:{slashed}@git.example.com:a/b.git"
        scrubbed = scrub_credentials(text)
        assert slashed not in scrubbed
        assert "git.example.com" in scrubbed

    def test_ported_url_with_at_in_path_is_over_scrubbed_not_leaked(self):
        # Accepted cost of the slash-tolerant patterns: a URL with BOTH a
        # non-default port and an "@" in its path reads as `host:<secret>@`.
        # Redaction's rule is over-scrub rather than leak, so it is redacted
        # instead of preserved — pinned here so the mangling stays a known
        # trade-off rather than a surprise.
        text = "fatal: repository 'https://git.example.com:8443/a/repo@v1.git' not found"
        assert scrub_credentials(text) == "fatal: repository 'https://v1.git' not found"

    def test_ported_url_without_an_at_survives_intact(self):
        # The far more common ported URL is untouched — the over-scrub above
        # needs the "@" in the path as well.
        text = "fatal: repository 'https://git.example.com:8443/a/b.git' not found"
        assert scrub_credentials(text) == text

    def test_derived_token_never_appears_in_scrubbed_error_text(self):
        # The realistic leak vector: a failed clone stringifies its command,
        # carrying the derived tokenized URL.
        url, _ = derive_clone_url(DECLARED, WORK_HTTPS_TOKENIZED)
        error_text = f"Command '['git', 'clone', '{url}', '/taskdef']' returned 128"
        scrubbed = scrub_credentials(error_text)
        assert FAKE_TOKEN not in scrubbed
        assert "128" in scrubbed
