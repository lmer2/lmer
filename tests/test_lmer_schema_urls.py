"""Tests for ``lmer_schema.urls`` — the registry's remote-URL keys (spec D21).

The module is an ADAPTER, not a normalizer: it delegates to the single
definition of remote normalization, ``lmer_cli.container.sources
.normalize_repo_url``, and applies it uniformly to a manifest entry's
``repo:``, each of its ``remotes:`` and the URL a session looks up, so all
three compare in one canonical ``host/path`` form (spec §4 "Remote
normalization", "Resolution").

Two halves:

- behaviour — the §4 spellings (scp-form, ``ssh://``, https with a credential,
  a non-default port, trailing ``.git``/``/``) fold to the canonical key, and
  the ``repo:``/``remotes:``/lookup-URL triple folds to ONE key;
- the drift guard — the module source contains NO URL parsing of its own: no
  ``re``/``urllib``, no string dissection on URL delimiters, and the normalizer
  is imported from ``lmer_cli.container.sources``. Credential stripping and
  the default-port rule live in one place; a private reimplementation here
  would be a security regression, not a refactor.
"""

import ast
from pathlib import Path

import pytest

from lmer_cli.container import sources
from lmer_schema import urls
from tests import plexus_fixtures as pf

REPO_ROOT = Path(__file__).resolve().parent.parent
URLS_PATH = REPO_ROOT / "src" / "lmer_schema" / "urls.py"

#: The fixture registry's primary remote, already in canonical form.
CANONICAL = pf.PRIMARY_REPO
HOST, PATH = CANONICAL.split("/", 1)
#: The same path with its case changed — derived from the fixture, never
#: spelled, so this file carries no locator of its own and stays correct
#: whatever PRIMARY_REPO is. `normalize` keeps the case; `lookup_key` folds it.
MIXED_CASE_PATH = PATH.title()
assert MIXED_CASE_PATH != PATH and MIXED_CASE_PATH.lower() == PATH

# Fake token value (glpat- shape, not a real secret).
TOKEN = "glpat-FAKEtoken123"

#: Every §4 spelling of the one repo. Each must normalize to CANONICAL.
SPELLINGS = {
    "https": f"https://{HOST}/{PATH}",
    "https-git-suffix": f"https://{HOST}/{PATH}.git",
    "https-trailing-slash": f"https://{HOST}/{PATH}/",
    "https-credential": f"https://oauth2:{TOKEN}@{HOST}/{PATH}.git",
    "https-default-port": f"https://{HOST}:443/{PATH}",
    "https-mixed-case-host": f"https://{HOST.upper()}/{PATH}",
    "scp": f"git@{HOST}:{PATH}.git",
    "scp-userless": f"{HOST}:{PATH}.git",
    "ssh": f"ssh://git@{HOST}/{PATH}.git",
    "ssh-default-port": f"ssh://git@{HOST}:22/{PATH}",
    "bare-canonical": CANONICAL,
}

#: The spec's own non-default-port example: a DIFFERENT key, kept as host:port.
PORTED = f"ssh://git@{HOST}:2222/{PATH}"
PORTED_KEY = f"{HOST}:2222/{PATH}"


class TestNormalize:
    @pytest.mark.parametrize("spelling", sorted(SPELLINGS), ids=sorted(SPELLINGS))
    def test_every_spelling_folds_to_the_canonical_form(self, spelling):
        assert urls.normalize(SPELLINGS[spelling]) == CANONICAL

    @pytest.mark.parametrize("spelling", sorted(SPELLINGS), ids=sorted(SPELLINGS))
    def test_is_exactly_the_sources_normalizer(self, spelling):
        # Adoption, not redefinition (D21): byte-for-byte the same answer.
        url = SPELLINGS[spelling]
        assert urls.normalize(url) == sources.normalize_repo_url(url)

    def test_scp_form(self):
        assert urls.normalize(f"git@{HOST}:{PATH}") == CANONICAL

    def test_userless_scp_form(self):
        # Git's scp syntax is `[user@]host:path`: the user is optional, and
        # an ssh_config alias checkout reports the userless form for life.
        assert urls.normalize(f"{HOST}:{PATH}") == CANONICAL
        assert urls.normalize(f"{HOST}:{PATH}") == urls.normalize(f"git@{HOST}:{PATH}")

    def test_ssh_scheme_form(self):
        assert urls.normalize(f"ssh://git@{HOST}/{PATH}") == CANONICAL

    def test_https_with_credential_is_stripped(self):
        result = urls.normalize(SPELLINGS["https-credential"])
        assert result == CANONICAL
        assert TOKEN not in result
        assert "oauth2" not in result

    def test_non_default_port_is_kept_as_a_different_key(self):
        assert urls.normalize(PORTED) == PORTED_KEY
        assert urls.normalize(PORTED) != CANONICAL

    def test_ported_key_re_normalizes_to_itself(self):
        # `host:2222/path` is the normalizer's own spelling of a ported
        # repo, so a manifest may carry it verbatim: the digits after the
        # colon are a port, not a userless scp path.
        assert urls.normalize(PORTED_KEY) == PORTED_KEY
        assert urls.normalize(PORTED_KEY) == urls.normalize(PORTED)

    def test_default_ports_are_dropped(self):
        assert urls.normalize(f"https://{HOST}:443/{PATH}") == CANONICAL
        assert urls.normalize(f"ssh://git@{HOST}:22/{PATH}") == CANONICAL

    def test_trailing_git_and_slash_are_stripped(self):
        assert urls.normalize(f"https://{HOST}/{PATH}.git/") == CANONICAL

    def test_path_case_is_preserved_as_written(self):
        # §4: the canonical SPELLING keeps path case (host is lowered) — the
        # comparison side is lookup_key's job.
        assert urls.normalize(f"https://{HOST.upper()}/{MIXED_CASE_PATH}") == \
            f"{HOST}/{MIXED_CASE_PATH}"

    def test_empty_and_none_are_empty(self):
        assert urls.normalize("") == ""
        assert urls.normalize(None) == ""


class TestLookupKey:
    @pytest.mark.parametrize("spelling", sorted(SPELLINGS), ids=sorted(SPELLINGS))
    def test_every_spelling_shares_one_lookup_key(self, spelling):
        assert urls.lookup_key(SPELLINGS[spelling]) == urls.lookup_key(CANONICAL)

    def test_is_the_canonical_form_for_a_lower_case_path(self):
        assert urls.lookup_key(SPELLINGS["scp"]) == CANONICAL

    def test_path_case_compares_insensitively(self):
        # §4: path case preserved as written, COMPARED case-insensitively
        # (GitHub and GitLab both route paths case-insensitively).
        assert urls.lookup_key(f"https://{HOST}/{MIXED_CASE_PATH}") == urls.lookup_key(CANONICAL)
        assert urls.normalize(f"https://{HOST}/{MIXED_CASE_PATH}") != CANONICAL

    def test_schema_2_preserves_path_case_in_the_identity_key(self):
        upper = f"https://{HOST}/{MIXED_CASE_PATH}"

        assert urls.lookup_key(upper, schema=2) == f"{HOST}/{MIXED_CASE_PATH}"
        assert urls.lookup_key(upper, schema=2) != urls.lookup_key(CANONICAL, schema=2)

    def test_non_default_port_stays_a_different_key(self):
        assert urls.lookup_key(PORTED) == PORTED_KEY
        assert urls.lookup_key(PORTED) != urls.lookup_key(CANONICAL)

    def test_credential_never_reaches_the_key(self):
        assert TOKEN not in urls.lookup_key(SPELLINGS["https-credential"])

    def test_userless_scp_form_shares_the_scp_key(self):
        assert urls.lookup_key(SPELLINGS["scp-userless"]) == urls.lookup_key(SPELLINGS["scp"])
        assert urls.lookup_key(SPELLINGS["scp-userless"]) == CANONICAL


class TestEntryKeys:
    def test_repo_first_then_remotes_in_declared_order(self):
        assert urls.entry_keys(pf.PRIMARY_REPO, pf.REMOTES) == (pf.PRIMARY_REPO, *pf.REMOTES)

    def test_single_remote_project_is_one_key(self):
        assert urls.entry_keys(pf.PRIMARY_REPO) == (pf.PRIMARY_REPO,)
        assert urls.entry_keys(pf.PRIMARY_REPO, None) == (pf.PRIMARY_REPO,)
        assert urls.entry_keys(pf.PRIMARY_REPO, []) == (pf.PRIMARY_REPO,)

    def test_repoless_project_has_no_keys(self):
        # D19: no repo:, no remotes: — such an entry never matches a lookup.
        assert urls.entry_keys(None) == ()
        assert urls.entry_keys(None, None) == ()
        assert urls.entry_keys("", ()) == ()

    def test_values_are_normalized_not_copied(self):
        keys = urls.entry_keys(SPELLINGS["https-credential"], [SPELLINGS["scp"], PORTED])
        assert keys == (CANONICAL, PORTED_KEY)
        assert TOKEN not in "".join(keys)

    def test_repeated_spellings_fold_to_one_key_primary_first(self):
        # Just normalizes: a repo: repeated in remotes: is the registry
        # validator's ERROR to raise, detected by the count shrinking here.
        remotes = [SPELLINGS["scp"], SPELLINGS["ssh"], *pf.REMOTES]
        keys = urls.entry_keys(pf.PRIMARY_REPO, remotes)
        assert keys == (pf.PRIMARY_REPO, *pf.REMOTES)
        assert len(keys) < 1 + len(remotes)

    def test_path_case_variants_fold_to_one_key(self):
        keys = urls.entry_keys(f"https://{HOST}/{MIXED_CASE_PATH}", [f"git@{HOST}:{PATH}"])
        assert keys == (CANONICAL,)

    def test_schema_2_keeps_path_case_variants_as_distinct_keys(self):
        upper = f"https://{HOST}/{MIXED_CASE_PATH}"

        assert urls.entry_keys(upper, [CANONICAL], schema=2) == (
            f"{HOST}/{MIXED_CASE_PATH}",
            CANONICAL,
        )

    def test_userless_and_user_qualified_scp_forms_fold_to_one_key(self):
        keys = urls.entry_keys(SPELLINGS["scp"], [SPELLINGS["scp-userless"]])
        assert keys == (CANONICAL,)

    def test_returns_a_tuple(self):
        assert isinstance(urls.entry_keys(pf.PRIMARY_REPO, list(pf.REMOTES)), tuple)


class TestRepoRemotesLookupTriple:
    """§4 "Resolution": the manifest's ``repo:``, its ``remotes:`` and the URL
    a session looks up all compare in the same form, so a multi-remote project
    lands in ONE tree whichever spelling the session arrived with."""

    def test_all_three_fold_to_one_key(self):
        manifest_repo = SPELLINGS["https-git-suffix"]   # a hand-edited, non-canonical spelling
        manifest_remote = SPELLINGS["scp"]
        session_url = SPELLINGS["https-credential"]     # what a clone URL looks like
        assert len({
            urls.normalize(manifest_repo),
            urls.normalize(manifest_remote),
            urls.lookup_key(session_url),
        }) == 1

    def test_session_lookup_hits_the_fixture_entry_by_any_spelling(self, schema_plexus):
        keys = urls.entry_keys(schema_plexus.primary_repo, schema_plexus.remotes)
        for spelling in SPELLINGS.values():
            assert urls.lookup_key(spelling) in keys, spelling
        assert keys.index(urls.lookup_key(SPELLINGS["scp"])) == 0

    def test_session_lookup_hits_an_additional_remote(self, schema_plexus):
        keys = urls.entry_keys(schema_plexus.primary_repo, schema_plexus.remotes)
        mirror = schema_plexus.remotes[0]
        mirror_host, mirror_path = mirror.split("/", 1)
        assert urls.lookup_key(f"git@{mirror_host}:{mirror_path}.git") in keys

    def test_ported_remote_is_listed_with_its_port_and_found_by_it(self):
        # The spec's example: a remote reached on :2222 is a DIFFERENT key,
        # listed under remotes: with its port — exactly what remotes: is for.
        keys = urls.entry_keys(pf.PRIMARY_REPO, [PORTED])
        assert keys == (pf.PRIMARY_REPO, PORTED_KEY)
        assert urls.lookup_key(PORTED) in keys
        assert urls.lookup_key(SPELLINGS["ssh"]) in keys

    def test_unrelated_repo_does_not_match(self):
        keys = urls.entry_keys(pf.PRIMARY_REPO, pf.REMOTES)
        assert urls.lookup_key(f"https://{pf.BUCKET_HOST}/{pf.BUCKET_PROJECT}.git") not in keys


@pytest.fixture(scope="module")
def tree():
    """The adapter's AST — what the drift guard inspects."""
    return ast.parse(URLS_PATH.read_text(encoding="utf-8"), filename=str(URLS_PATH))


class TestDriftGuard:
    """The adapter contains NO independent URL parsing (D21).

    Asserted on the module SOURCE, not on behaviour: behaviour tests would
    stay green through a faithful private copy, and a faithful private copy is
    exactly the drift this guards against — the next credential/port fix in
    ``sources.py`` would silently not apply here.
    """

    @staticmethod
    def _imports(tree):
        """``(module, name)`` for every import; ``name`` is ``None`` for ``import x``."""
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                out.extend((alias.name, None) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                out.extend((node.module or "", alias.name) for alias in node.names)
        return out

    def test_no_regex_and_no_urllib(self, tree):
        for module, name in self._imports(tree):
            top = module.split(".", 1)[0]
            assert top not in {"re", "urllib"}, f"private URL parsing via {module!r}"
            assert name not in {"urlparse", "urlsplit", "compile", "match"}, (module, name)
        # Belt and braces: no `re.<anything>` attribute access either.
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert node.value.id not in {"re", "urllib", "urlparse"}, ast.dump(node)

    def test_normalizer_is_imported_from_the_one_definition(self, tree):
        assert ("lmer_cli.container.sources", "normalize_repo_url") in self._imports(tree)
        assert urls.normalize_repo_url is sources.normalize_repo_url

    def test_nothing_else_is_taken_from_sources(self, tree):
        # The re-export decision, pinned: repo_url_host / scrub_credentials
        # stay with lmer_cli.container.sources (module docstring says why).
        taken = {name for module, name in self._imports(tree) if module.startswith("lmer_cli")}
        assert taken == {"normalize_repo_url"}
        for private in ("_parse_repo_url", "_userinfo_boundary", "_SCHEME_RE"):
            assert not hasattr(urls, private)

    def test_no_url_delimiter_string_constants(self, tree):
        """No ``"://"``, ``"@"``, ``":"``, ``"/"`` or ``".git"`` literal outside
        docstrings — the shape every scheme/userinfo/port/suffix rule needs."""
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None:
                    docstrings.add(id(node.body[0].value))
        offenders = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and any(delim in node.value for delim in ("://", "@", ":", "/", ".git"))
        ]
        assert offenders == []

    def test_no_string_dissection_calls(self, tree):
        """No ``partition``/``split``/``find``/``strip``… — the tools a
        reimplementation is built from. ``lower()`` (the §4 case fold) is the
        only string method the adapter is entitled to."""
        dissectors = {
            "partition", "rpartition", "split", "rsplit", "splitlines",
            "find", "rfind", "index", "rindex",
            "startswith", "endswith", "removeprefix", "removesuffix",
            "strip", "lstrip", "rstrip", "replace", "sub", "match", "search",
        }
        used = sorted({
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in dissectors
        })
        assert used == []

    def test_docstring_records_the_reexport_decision(self):
        doc = urls.__doc__ or ""
        assert "repo_url_host" in doc
        assert "scrub_credentials" in doc
