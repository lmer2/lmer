"""Tests for the pure URL normalizer in lmer_cli.container.sources.

The normalizer is the comparison side of the sources.yaml resolution matrix
(spec: canonical-source-config): a NEW pure function — no credential
injection, not token-conditional (the tokens.py converter is reference
material only, never reused). It strips userinfo, strips trailing `.git`
and trailing slash, lowercases the host (path case preserved), drops the
default ports :443/:22, and folds scp-form and ssh:// spellings into the
same `host/path` form as https.
"""

from lmer_cli.container.sources import (
    normalize_repo_url,
    repo_url_host,
    scrub_credentials,
    url_has_embedded_credential,
)

CANONICAL = "git.example.com/agents/taskdefs"


class TestNormalizeRepoUrl:
    def test_plain_https(self):
        assert normalize_repo_url("https://git.example.com/agents/taskdefs") == CANONICAL

    def test_credentialed_url_stripped(self):
        # Fake token value (glpat- shape, not a real secret).
        url = "https://oauth2:glpat-FAKEtoken123@git.example.com/agents/taskdefs.git"
        assert normalize_repo_url(url) == CANONICAL

    def test_basic_userinfo_stripped(self):
        url = "https://user:hunter2@git.example.com/agents/taskdefs"
        assert normalize_repo_url(url) == CANONICAL

    def test_git_suffix_stripped(self):
        assert normalize_repo_url("https://git.example.com/agents/taskdefs.git") == CANONICAL

    def test_trailing_slash_stripped(self):
        assert normalize_repo_url("https://git.example.com/agents/taskdefs/") == CANONICAL

    def test_git_suffix_and_trailing_slash(self):
        url = "https://git.example.com/agents/taskdefs.git/"
        assert normalize_repo_url(url) == CANONICAL

    def test_scp_form(self):
        assert normalize_repo_url("git@git.example.com:agents/taskdefs.git") == CANONICAL

    def test_ssh_scheme_form(self):
        assert normalize_repo_url("ssh://git@git.example.com/agents/taskdefs.git") == CANONICAL

    def test_mixed_case_host_lowered_path_case_preserved(self):
        url = "https://Git.Example.COM/Agents/TaskDefs"
        assert normalize_repo_url(url) == "git.example.com/Agents/TaskDefs"

    def test_default_https_port_dropped(self):
        assert normalize_repo_url("https://git.example.com:443/agents/taskdefs") == CANONICAL

    def test_default_ssh_port_dropped(self):
        url = "ssh://git@git.example.com:22/agents/taskdefs.git"
        assert normalize_repo_url(url) == CANONICAL

    def test_non_default_port_kept(self):
        url = "https://git.example.com:8443/agents/taskdefs"
        assert normalize_repo_url(url) == "git.example.com:8443/agents/taskdefs"

    def test_all_spellings_of_one_repo_normalize_equal(self):
        # The core resolution-matrix property: https, scp, and ssh://
        # spellings of one repo (credentialed or not, .git or not) all land
        # on the same comparison form.
        spellings = [
            "https://git.example.com/agents/taskdefs",
            "https://git.example.com/agents/taskdefs.git",
            "https://git.example.com/agents/taskdefs.git/",
            "https://oauth2:glpat-FAKEtoken123@git.example.com/agents/taskdefs.git",
            "https://Git.Example.Com:443/agents/taskdefs",
            "git@git.example.com:agents/taskdefs.git",
            "git@git.example.com:agents/taskdefs",
            "git.example.com:agents/taskdefs.git",
            "git.example.com:agents/taskdefs",
            "ssh://git@git.example.com/agents/taskdefs.git",
            "ssh://git@git.example.com:22/agents/taskdefs",
        ]
        forms = {normalize_repo_url(url) for url in spellings}
        assert forms == {CANONICAL}

    def test_pure_no_credential_injection(self):
        # A clean URL comes back clean — the normalizer never adds auth.
        assert "@" not in normalize_repo_url("https://git.example.com/a/b.git")


class TestUserlessScpForm:
    """Git's scp syntax is ``[<user>@]<host>:<path>`` — the user is optional.

    Review finding (wave 1): the parser took its scp branch only when it
    found userinfo, so the userless spelling fell through to the "all path"
    reading and ``host:org/repo.git`` normalized to ``host:org/repo`` while
    ``git@host:org/repo.git`` normalized to ``host/org/repo`` — two keys for
    one repo, which is the D8 property the registry lookup rests on. The
    userless form is not exotic: a checkout cloned through an ssh_config
    ``Host`` alias reports it from ``git remote get-url`` for life.

    The rule is git's own — no scheme and a ``:`` before any ``/`` — with
    two scheme-less shapes deliberately kept OUT of the scp branch, both
    pinned below: a bare-digit segment after the colon is a port (the
    normalizer's own canonical spelling of a ported repo), and the
    ambiguous ``a:b/c@d`` shape stays hostless (callers refuse it).
    """

    USERLESS = "git.example.com:agents/taskdefs.git"

    def test_userless_scp_form_normalizes_like_https(self):
        assert normalize_repo_url(self.USERLESS) == CANONICAL

    def test_userless_and_user_qualified_scp_forms_are_one_repo(self):
        # The exact reproduction from the review.
        assert normalize_repo_url("git.example.com:acme/widgets.git") == \
            normalize_repo_url("git@git.example.com:acme/widgets.git") == \
            "git.example.com/acme/widgets"

    def test_userless_scp_form_has_a_host(self):
        # So the trust rule can compare it, rather than reporting host ''.
        assert repo_url_host(self.USERLESS) == "git.example.com"
        assert repo_url_host("Git.Example.COM:agents/taskdefs") == "git.example.com"

    def test_userless_scp_form_carries_no_credential(self):
        assert url_has_embedded_credential(self.USERLESS) is False

    def test_absolute_path_after_the_colon(self):
        # `host:/srv/git/x.git` is scp-form with an absolute path (git
        # accepts it); the leading separator is dropped like a scheme URL's.
        assert normalize_repo_url("git.example.com:/srv/git/x.git") == "git.example.com/srv/git/x"
        assert repo_url_host("git.example.com:/srv/git/x.git") == "git.example.com"

    def test_ported_canonical_form_is_a_port_not_an_scp_path(self):
        # `host:2222/path` is what normalize_repo_url itself emits for a
        # non-default port, so it must re-normalize to itself — under git's
        # raw rule it would read as host `host`, path `2222/path`, and a
        # manifest carrying the canonical ported key would silently miss.
        ported = "git.example.com:2222/agents/taskdefs"
        assert normalize_repo_url(ported) == ported
        assert normalize_repo_url(ported) == \
            normalize_repo_url("ssh://git@git.example.com:2222/agents/taskdefs")
        # ...and stays the opaque reading a bare `host/path` gets: no host.
        assert repo_url_host(ported) == ""

    def test_bare_digits_to_the_end_are_a_port_too(self):
        assert normalize_repo_url("git.example.com:2222") == "git.example.com:2222"
        assert repo_url_host("git.example.com:2222") == ""

    def test_ported_host_with_at_in_path_keeps_its_prior_reading(self):
        # The shape _userinfo_boundary's digit guard already reads as a
        # ported host; the scp rule agrees with it rather than re-deciding.
        assert normalize_repo_url("host:8080/org/repo@v1.git") == "host:8080/org/repo@v1"

    def test_user_qualified_form_is_the_way_to_spell_an_all_digit_segment(self):
        # The residual ambiguity, documented on _parse_repo_url: a top-level
        # path segment that really is all digits needs the user (or a
        # scheme) to be read as a path.
        assert normalize_repo_url("git@git.example.com:2024/reports") == \
            "git.example.com/2024/reports"
        assert normalize_repo_url("git.example.com:2024/reports") == \
            "git.example.com:2024/reports"

    def test_ambiguous_shape_stays_hostless(self):
        # TestAmbiguousSchemeLessShape's guarantee, restated against the new
        # branch: `a:b/c@d` has a `:` before any `/` but is NOT taken as
        # userless scp — neither reading is reported as a host, and the
        # string is left opaque exactly as before.
        assert repo_url_host("git.example.com:org/re@po.git") == ""
        assert normalize_repo_url("git.example.com:org/re@po.git") == "git.example.com:org/re@po"

    def test_bare_paths_and_canonical_forms_are_untouched(self):
        # (The leading separator has always been stripped with the trailing one.)
        assert normalize_repo_url("/srv/git/repo.git") == "srv/git/repo"
        assert normalize_repo_url(CANONICAL) == CANONICAL
        assert repo_url_host(CANONICAL) == ""
        # A `/` before the first `:` is not the scp shape.
        assert normalize_repo_url("srv/git:repo") == "srv/git:repo"

    def test_scheme_urls_are_unchanged_by_the_scp_rule(self):
        # The scp branch is scheme-less only; these are the scheme-form
        # readings pinned above, with a digit segment and a colon in the
        # path for good measure.
        assert normalize_repo_url("https://git.example.com/2222/x.git") == "git.example.com/2222/x"
        assert normalize_repo_url("https://git.example.com/a:b/c") == "git.example.com/a:b/c"
        assert normalize_repo_url("https://git.example.com:8443/agents/taskdefs") == \
            "git.example.com:8443/agents/taskdefs"


class TestRepoUrlHost:
    def test_https_host(self):
        assert repo_url_host("https://Git.Example.com/a/b.git") == "git.example.com"

    def test_scp_form_host(self):
        assert repo_url_host("git@git.example.com:a/b.git") == "git.example.com"

    def test_ssh_form_host(self):
        assert repo_url_host("ssh://git@git.example.com/a/b") == "git.example.com"

    def test_port_never_participates(self):
        # The trust-rule key is the hostname alone — default or not, the port
        # is dropped (review finding, iteration 4). A second port on a host
        # that already holds the credential is not the off-host routing the
        # trust rule exists to stop, and keeping it rejected the common
        # SSH-on-2222 + HTTPS-on-443 self-hosted layout.
        assert repo_url_host("https://git.example.com:443/a") == "git.example.com"
        assert repo_url_host("https://git.example.com:8443/a") == "git.example.com"
        assert repo_url_host("ssh://git@git.example.com:2222/a") == "git.example.com"

    def test_ported_ssh_and_default_https_share_a_trust_key(self):
        # The exact reproduction from the iteration-4 review: these two must
        # compare equal, or an SSH work repo on :2222 can carry no HTTPS
        # declaration at all.
        work = "ssh://git@git.example.com:2222/org/work.git"
        declared = "https://git.example.com/org/taskdefs.git"
        assert repo_url_host(work) == repo_url_host(declared) == "git.example.com"

    def test_normalizer_still_keeps_non_default_ports(self):
        # repo_url_host and normalize_repo_url answer different questions —
        # "same trust boundary?" vs "same repo?". Only the first is port-blind.
        assert normalize_repo_url("https://git.example.com:8443/a") == "git.example.com:8443/a"

    def test_credentialed_host(self):
        url = "https://oauth2:glpat-FAKEtoken123@git.example.com/a/b.git"
        assert repo_url_host(url) == "git.example.com"

    def test_bare_path_has_no_host(self):
        assert repo_url_host("/srv/git/repo.git") == ""


class TestUserinfoWithSlashInSecret:
    """A secret containing "/" must not slip past the userinfo boundary.

    Iteration-4 review finding: the authority was split on "/" before the
    userinfo "@" was located, so the credential landed in `path` and both the
    no-secrets refusal and the redactor failed open. Base64-alphabet tokens
    routinely contain "/".
    """

    SLASHED = "https://user:se/cret@git.example.com/a/b.git"
    SLASHED_SCP = "user:se/cret@git.example.com:a/b.git"

    def test_scheme_form_host_is_the_real_host(self):
        assert repo_url_host(self.SLASHED) == "git.example.com"

    def test_scheme_form_normalizes_without_the_secret(self):
        assert normalize_repo_url(self.SLASHED) == "git.example.com/a/b"

    def test_scp_form_host_is_the_real_host(self):
        assert repo_url_host(self.SLASHED_SCP) == "git.example.com"

    def test_scp_form_normalizes_without_the_secret(self):
        assert normalize_repo_url(self.SLASHED_SCP) == "git.example.com/a/b"

    def test_ported_host_with_at_in_path_is_not_read_as_userinfo(self):
        # The guard on the extension: `host:8443` is a port, not `user:secret`,
        # so an "@" later in the path must not be taken as the boundary.
        url = "https://git.example.com:8443/a/repo@v1.git"
        assert repo_url_host(url) == "git.example.com"
        assert normalize_repo_url(url) == "git.example.com:8443/a/repo@v1"


class TestScpFormAtInPath:
    """Iteration-5 review finding: the digit guard cannot discriminate in
    scp-form, where the host/path colon is mandatory and its right-hand side
    is a path segment, never a port. The extension therefore always fired,
    misreading a userless scp URL whose PATH contains an "@" as userinfo —
    and the operator was then told to strip a credential that is not there.
    The scp discriminator is that a real boundary leaves `host:path` behind
    it, so the text after the "@" must itself carry a ":".
    """

    AT_IN_PATH = "git.example.com:org/re@po.git"

    def test_at_in_path_is_not_read_as_a_credential(self):
        assert url_has_embedded_credential(self.AT_IN_PATH) is False

    def test_at_in_path_does_not_become_the_host(self):
        # Pre-fix this returned "po.git" — the segment after the path "@".
        assert repo_url_host(self.AT_IN_PATH) != "po.git"

    def test_scp_credential_with_slashed_secret_still_detected(self):
        # The case the extension exists for: after-"@" is `host:a/b.git`,
        # which carries the mandatory scp colon, so the boundary still holds.
        url = "user:se/cret@git.example.com:a/b.git"
        assert url_has_embedded_credential(url) is True
        assert repo_url_host(url) == "git.example.com"

    def test_scheme_form_slashed_secret_unaffected_by_the_discriminator(self):
        # Scheme-form passes scp_form=False: after-"@" is `host/a/b.git`,
        # which has no colon, and requiring one there would re-open the
        # fail-open this whole extension closed.
        url = "https://user:se/cret@git.example.com/a/b.git"
        assert url_has_embedded_credential(url) is True
        assert repo_url_host(url) == "git.example.com"


class TestAmbiguousSchemeLessShape:
    """Iteration-6 review finding: the two earlier findings are one ambiguity.

    `git.example.com:org/re@po.git` (path "@") and
    `user:se/cret@git.example.com/org/x.git` (secret "/") are the same
    string shape — `a:b/c@d` — so no parser rule can pick a winner, and the
    guard that made one right made the other wrong. The parser therefore
    keeps its conservative reading (no credential invented where the more
    likely reading has none) and callers refuse the shape outright instead
    of acting on a guess; see test_sources_config.py and test_sources_cli.py.

    What must hold HERE is that the redactor stays independent of the parse,
    because it is what keeps every printed diagnostic safe under either
    reading.
    """

    SLASHED_SECRET = "user:se/cret@git.example.com/org/x.git"
    AT_IN_PATH = "git.example.com:org/re@po.git"

    def test_redaction_does_not_depend_on_the_parse(self):
        assert "se/cret" not in scrub_credentials(self.SLASHED_SECRET)
        # And on the normalized form, which is what the doctor CLI prints.
        assert "se/cret" not in scrub_credentials(
            normalize_repo_url(self.SLASHED_SECRET)
        )

    def test_neither_reading_is_reported_as_a_host(self):
        # Guessing a host is what would send a credential somewhere; an
        # ambiguous URL resolves to no host, and the trust rule cannot pass.
        assert repo_url_host(self.SLASHED_SECRET) == ""
        assert repo_url_host(self.AT_IN_PATH) == ""

    def test_no_credential_is_invented_for_the_path_at_reading(self):
        # The iteration-5 guarantee, unchanged: this URL is refused as
        # unreadable, never as "you committed a token".
        assert url_has_embedded_credential(self.AT_IN_PATH) is False
