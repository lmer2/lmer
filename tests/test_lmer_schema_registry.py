"""``lmer_schema.registry`` — the projects registry (spec D8/D19, §4).

The registry is the typed reading of the manifest's raw ``projects:`` block:
each entry's ``repo:`` is the PRIMARY remote, ``remotes:`` the ADDITIONAL
ones, both optional (D19: a repoless project is a short name with a dir).
The union of every entry's normalized keys must map one repo to AT MOST one
project. These tests pin:

- the fixture registry loads: a multi-remote entry answers to every one of
  its remotes in every §4 spelling, the repoless entry is present, holds no
  keys and is addressable by name, and a legacy tree has no registry;
- the identity errors — one remote claimed by two entries (naming both),
  the primary repeated inside its own ``remotes:`` under ANY spelling, two
  ``remotes:`` folding to one key (§4 "registry corners": twice within one
  entry is an error, exactly like twice across entries);
- the shape errors — ``remotes:`` without ``repo:``, ``remotes:`` that is
  not a list of strings, an embedded credential, a short name outside
  ``[a-z0-9][a-z0-9_-]*``;
- every error is a ``ManifestError`` (a broken registry is a broken
  manifest: D20, never demoted to legacy) and never echoes a credential.
  Two layers raise it for a document loaded from disk — the loader owns the
  key charset and the ``repo:``/``remotes:`` types, the registry owns the
  identity rules and re-checks the shape at its own API boundary (a
  hand-built :class:`~lmer_schema.manifest.Manifest`) — so these tests pin
  the taxonomy and the message, not which layer got there first;
  ``TestLayering`` pins the ownership itself.

Every negative test rewrites ITS OWN fresh fixture manifest (the shared
builder is consumed read-only).
"""

from pathlib import Path

import pytest
import yaml

from tests import plexus_fixtures as pf
from lmer_cli.container.sources import normalize_repo_url
from lmer_schema import registry as reg
from lmer_schema.manifest import Manifest, ManifestError, parse_manifest
from lmer_schema.registry import (
    Project,
    Registry,
    RegistryError,
    build_registry,
    is_short_name,
    load_registry,
)

HOST, PATH = pf.PRIMARY_REPO.split("/", 1)

# Fake token value (glpat- shape, not a real secret).
TOKEN = "glpat-FAKEtoken123"


def _doc():
    """The §4 fixture manifest as a plain dict, for targeted mutation."""
    return yaml.safe_load(pf.SCHEMA_MANIFEST)


def _rewrite_doc(tree, doc):
    tree.manifest.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _registry_from(doc):
    """Build a registry straight from a mutated document (no disk)."""
    return build_registry(parse_manifest(yaml.safe_dump(doc, sort_keys=False)))


def _raises(doc, *needles):
    """Assert *doc* is refused, loudly, with *needles* in the message.

    ``ManifestError`` rather than ``RegistryError``: for a document that goes
    through ``parse_manifest`` the loader owns the key charset and the
    ``repo:``/``remotes:`` types and raises first, the registry owns the
    identity rules and raises its ``RegistryError`` subclass. Both are the
    same taxonomy to every consumer (D20). ``TestLayering`` is where the
    ownership is pinned.
    """
    with pytest.raises(ManifestError) as excinfo:
        _registry_from(doc)
    message = str(excinfo.value)
    for needle in needles:
        assert needle in message, (needle, message)
    return message


def _hand_built(projects):
    """A ``Manifest`` built by hand, skipping the loader entirely — the API
    boundary ``build_registry`` has to defend on its own."""
    return Manifest(
        path=Path("/x/_plexus.yaml"),
        schema=1,
        name="hand-built",
        projects=projects,
        sources={},
        housekeeping={},
        warnings=(),
    )


# ---------------------------------------------------------------------------
# Constants the rest of the package (and the docs) rely on
# ---------------------------------------------------------------------------


def test_short_name_charset_is_the_spec_rule():
    assert reg.SHORT_NAME_RE.pattern == pf.SHORT_NAME_RE.pattern == r"[a-z0-9][a-z0-9_-]*"


@pytest.mark.parametrize("name", ["lmer", "q3-capacity", "a", "0day", "my_proj-2"])
def test_valid_short_names(name):
    assert is_short_name(name)


@pytest.mark.parametrize(
    "name",
    ["Widgets", "git.example.com", "_lmer", "-x", "a/b", "", "lmer ", "über", "a.b"],
)
def test_invalid_short_names(name):
    # Dots are what tell a host bucket from a project (§1); `_` is the
    # schema's namespace (D6); capitals are the case-insensitive-fs trap.
    assert not is_short_name(name)


def test_is_short_name_rejects_non_strings():
    assert not is_short_name(None)
    assert not is_short_name(123)


# ---------------------------------------------------------------------------
# Happy path — the fixture registry
# ---------------------------------------------------------------------------


def test_loads_fixture_registry(schema_plexus):
    registry = load_registry(schema_plexus.root)

    assert isinstance(registry, Registry)
    assert registry.path == schema_plexus.manifest
    assert list(registry) == [pf.PROJECT, pf.REPOLESS_PROJECT]
    assert len(registry) == 2

    project = registry[pf.PROJECT]
    assert isinstance(project, Project)
    assert project.name == pf.PROJECT
    assert project.repo == pf.PRIMARY_REPO
    assert project.remotes == pf.REMOTES
    assert project.description is None
    assert not project.repoless
    # The union keys: primary first, then the additional remotes as declared.
    assert project.keys == (pf.PRIMARY_REPO, *pf.REMOTES)


def test_build_registry_is_the_pure_half(schema_plexus):
    manifest = parse_manifest(pf.SCHEMA_MANIFEST)
    built = build_registry(manifest)
    loaded = load_registry(schema_plexus.root)
    assert list(built) == list(loaded)
    assert built.index == loaded.index


def test_legacy_tree_has_no_registry(legacy_plexus):
    # No manifest -> None: the legacy-form signal, same as load_manifest.
    assert load_registry(legacy_plexus.root) is None


class TestMultiRemoteEntry:
    """D8: a session resolves by normalized lookup across the union, so a
    multi-remote project is ONE entry answering to every one of its remotes."""

    @pytest.fixture
    def registry(self, schema_plexus):
        return load_registry(schema_plexus.root)

    def test_resolvable_by_primary(self, registry):
        assert registry.lookup(pf.PRIMARY_REPO).name == pf.PROJECT

    @pytest.mark.parametrize("remote", pf.REMOTES)
    def test_resolvable_by_every_additional_remote(self, registry, remote):
        assert registry.lookup(remote).name == pf.PROJECT

    @pytest.mark.parametrize(
        "spelling",
        [
            f"https://{HOST}/{PATH}.git",
            f"git@{HOST}:{PATH}.git",
            f"ssh://git@{HOST}/{PATH}",
            f"https://oauth2:{TOKEN}@{HOST}/{PATH}.git",
            f"https://{HOST.upper()}/{PATH.upper()}",
            f"https://{HOST}:443/{PATH}/",
        ],
    )
    def test_resolvable_by_any_spelling_of_a_remote(self, registry, spelling):
        # All comparison goes through the urls adapter (D21): the manifest's
        # canonical spelling and the session's clone URL meet on one key.
        assert registry.lookup(spelling).name == pf.PROJECT

    def test_an_additional_remote_in_scp_form(self, registry):
        host, path = pf.REMOTES[1].split("/", 1)
        assert registry.lookup(f"git@{host}:{path}.git").name == pf.PROJECT

    def test_unrelated_repo_misses(self, registry):
        assert registry.lookup(f"https://{pf.BUCKET_HOST}/{pf.BUCKET_PROJECT}") is None

    def test_non_default_port_is_a_different_repo(self, registry):
        assert registry.lookup(f"ssh://git@{HOST}:2222/{PATH}") is None

    def test_empty_lookup_misses(self, registry):
        assert registry.lookup("") is None
        assert registry.lookup(None) is None

    def test_index_is_the_union_keyed_to_short_names(self, registry):
        assert registry.index == {
            pf.PRIMARY_REPO: pf.PROJECT,
            pf.REMOTES[0]: pf.PROJECT,
            pf.REMOTES[1]: pf.PROJECT,
        }


class TestRepolessEntry:
    """D19: no ``repo:``, no ``remotes:`` — a short name with a directory.
    It contributes NO keys to the union and never matches a lookup, but it
    is a first-class project addressed by name."""

    @pytest.fixture
    def registry(self, schema_plexus):
        return load_registry(schema_plexus.root)

    def test_present_and_name_addressable(self, registry, schema_plexus):
        assert pf.REPOLESS_PROJECT in registry
        project = registry[pf.REPOLESS_PROJECT]
        assert registry.get(pf.REPOLESS_PROJECT) is project
        assert project.repoless
        assert project.repo is None
        assert project.remotes == ()
        assert project.description == pf.REPOLESS_DESCRIPTION
        assert project.dir(schema_plexus.root) == schema_plexus.repoless_dir

    def test_contributes_no_keys(self, registry):
        assert registry[pf.REPOLESS_PROJECT].keys == ()
        assert pf.REPOLESS_PROJECT not in registry.index.values()

    def test_never_matches_a_lookup(self, registry):
        # Its name is not a URL key either: `q3-capacity` normalizes to a
        # bare host-less string that no entry lists.
        assert registry.lookup(pf.REPOLESS_PROJECT) is None

    def test_unknown_name_is_absent(self, registry):
        assert "nope" not in registry
        assert registry.get("nope") is None
        with pytest.raises(KeyError):
            registry["nope"]

    def test_bare_entry_is_repoless(self):
        doc = {"schema": 1, "name": "x", "projects": {"q3-capacity": None}}
        registry = _registry_from(doc)
        assert registry["q3-capacity"].repoless
        assert registry["q3-capacity"].keys == ()

    def test_no_projects_section_is_an_empty_registry(self):
        registry = _registry_from({"schema": 1, "name": "x"})
        assert list(registry) == []
        assert registry.index == {}
        assert registry.lookup(pf.PRIMARY_REPO) is None


def test_single_remote_project_is_one_key():
    doc = {"schema": 1, "name": "x", "projects": {"p": {"repo": pf.PRIMARY_REPO}}}
    registry = _registry_from(doc)
    assert registry["p"].keys == (pf.PRIMARY_REPO,)
    assert registry["p"].remotes == ()


def test_empty_remotes_list_beside_repo_is_accepted():
    # §4: `remotes: []` beside `repo:` is a WARN (pointless, harmless) — the
    # validator's nudge, not a load failure.
    doc = {"schema": 1, "name": "x", "projects": {"p": {"repo": pf.PRIMARY_REPO, "remotes": []}}}
    assert _registry_from(doc)["p"].keys == (pf.PRIMARY_REPO,)


def test_non_canonical_spellings_are_kept_as_written_and_keyed_normalized():
    # A hand-edited spelling is a validator WARN; the registry still keys it.
    doc = {"schema": 1, "name": "x", "projects": {"p": {
        "repo": f"https://{HOST}/{PATH}.git",
        "remotes": [f"git@{pf.REMOTES[0].split('/', 1)[0]}:{pf.REMOTES[0].split('/', 1)[1]}.git"],
    }}}
    project = _registry_from(doc)["p"]
    assert project.repo == f"https://{HOST}/{PATH}.git"
    assert project.keys == (pf.PRIMARY_REPO, pf.REMOTES[0])


def test_model_is_frozen(schema_plexus):
    registry = load_registry(schema_plexus.root)
    with pytest.raises((AttributeError, TypeError)):
        registry[pf.PROJECT].name = "other"


# ---------------------------------------------------------------------------
# Identity errors — one repo binds to AT MOST one project
# ---------------------------------------------------------------------------


class TestRegistryError:
    def test_is_a_manifest_error(self):
        # A broken registry is a broken manifest: callers that already treat
        # ManifestError as "schema form, unusable" (D20) need no second catch.
        assert issubclass(RegistryError, ManifestError)
        err = RegistryError("/x/_plexus.yaml", "boom")
        assert isinstance(err, ManifestError)
        assert str(err).startswith("/x/_plexus.yaml: boom")

    def test_load_registry_raises_on_disk(self, schema_plexus):
        doc = _doc()
        doc["projects"]["mirror"] = {"repo": pf.REMOTES[0]}
        _rewrite_doc(schema_plexus, doc)
        with pytest.raises(RegistryError) as excinfo:
            load_registry(schema_plexus.root)
        assert str(schema_plexus.manifest) in str(excinfo.value)

    def test_broken_manifest_still_raises_manifest_error(self, schema_plexus):
        schema_plexus.manifest.write_text("schema: true\nname: x\n", encoding="utf-8")
        with pytest.raises(ManifestError):
            load_registry(schema_plexus.root)


class TestLayering:
    """WHICH layer owns the shape rules, pinned so neither goes silently dead.

    The loader type-checks ``repo:``/``remotes:`` and the key charset for
    every document it parses (its module docstring says so); the registry
    re-checks both because ``build_registry`` takes a public dataclass a
    caller can build by hand. Same rule, same taxonomy, two entry points.
    """

    @pytest.mark.parametrize(
        "entry, needle",
        [
            ({"repo": pf.PRIMARY_REPO, "remotes": "origin"}, "remotes:"),
            ({"repo": 12345}, "repo:"),
        ],
    )
    def test_loader_refuses_the_document_before_the_registry_sees_it(self, entry, needle):
        doc = _doc()
        doc["projects"][pf.PROJECT] = entry
        with pytest.raises(ManifestError) as excinfo:
            parse_manifest(yaml.safe_dump(doc, sort_keys=False))
        assert not isinstance(excinfo.value, RegistryError)
        assert needle in str(excinfo.value)

    @pytest.mark.parametrize(
        "projects, needle",
        [
            ({pf.PROJECT: {"repo": pf.PRIMARY_REPO, "remotes": "origin"}}, "remotes:"),
            ({pf.PROJECT: {"repo": 12345}}, "repo:"),
            ({"Widgets": {"repo": pf.PRIMARY_REPO}}, "[a-z0-9][a-z0-9_-]*"),
        ],
    )
    def test_registry_refuses_a_hand_built_manifest_on_its_own(self, projects, needle):
        # No loader in this path at all: build_registry is a public entry
        # point and defends itself rather than trusting its input.
        with pytest.raises(RegistryError) as excinfo:
            build_registry(_hand_built(projects))
        assert needle in str(excinfo.value)

    def test_a_remote_name_never_becomes_one_character_keys(self):
        # The failure both layers exist to prevent (!253 finding 1).
        with pytest.raises(ManifestError):
            build_registry(_hand_built({pf.PROJECT: {"remotes": "origin"}}))


class TestDuplicateAcrossProjects:
    def test_same_primary_in_two_entries_names_both(self):
        doc = _doc()
        doc["projects"]["fork"] = {"repo": pf.PRIMARY_REPO}
        message = _raises(doc, pf.PROJECT, "fork", pf.PRIMARY_REPO)
        assert "at most one project" in message

    def test_one_entrys_remote_is_anothers_primary(self):
        doc = _doc()
        doc["projects"]["mirror"] = {"repo": pf.REMOTES[1]}
        _raises(doc, pf.PROJECT, "mirror", pf.REMOTES[1])

    def test_one_entrys_remote_is_anothers_remote(self):
        doc = _doc()
        doc["projects"]["other"] = {"repo": "example.org/other/repo", "remotes": [pf.REMOTES[0]]}
        _raises(doc, pf.PROJECT, "other", pf.REMOTES[0])

    def test_different_spelling_is_still_the_same_repo(self):
        doc = _doc()
        doc["projects"]["fork"] = {"repo": f"git@{HOST}:{PATH.upper()}.git"}
        _raises(doc, pf.PROJECT, "fork")

    def test_non_default_port_is_not_a_duplicate(self):
        doc = _doc()
        doc["projects"]["ported"] = {"repo": f"ssh://git@{HOST}:2222/{PATH}"}
        registry = _registry_from(doc)
        assert registry.lookup(f"{HOST}:2222/{PATH}").name == "ported"
        assert registry.lookup(pf.PRIMARY_REPO).name == pf.PROJECT


class TestDuplicateWithinOneEntry:
    """§4 registry corners: two values in ONE entry that normalize to the
    same key are an error, exactly like across entries — `repo:` is never
    repeated (D8), and a twice-listed mirror is drift waiting to happen."""

    def test_primary_repeated_verbatim_in_remotes(self):
        doc = _doc()
        doc["projects"][pf.PROJECT]["remotes"].append(pf.PRIMARY_REPO)
        _raises(doc, pf.PROJECT, "repo:", "remotes:")

    @pytest.mark.parametrize(
        "spelling",
        [
            f"https://{HOST}/{PATH}.git",
            f"git@{HOST}:{PATH}",
            f"ssh://git@{HOST}:22/{PATH}/",
            f"{HOST}/{PATH.upper()}",
        ],
    )
    def test_primary_repeated_in_remotes_under_any_spelling(self, spelling):
        doc = _doc()
        doc["projects"][pf.PROJECT]["remotes"].append(spelling)
        _raises(doc, pf.PROJECT, "repo:", "remotes:")

    def test_two_remotes_folding_to_one_key(self):
        doc = _doc()
        host, path = pf.REMOTES[0].split("/", 1)
        doc["projects"][pf.PROJECT]["remotes"].append(f"git@{host}:{path}.git")
        _raises(doc, pf.PROJECT, "remotes:", pf.REMOTES[0])

    def test_two_remotes_verbatim_twice(self):
        doc = _doc()
        doc["projects"][pf.PROJECT]["remotes"].append(pf.REMOTES[1])
        _raises(doc, pf.PROJECT, pf.REMOTES[1])


# ---------------------------------------------------------------------------
# Shape errors
# ---------------------------------------------------------------------------


class TestShape:
    def test_remotes_without_repo(self):
        doc = _doc()
        doc["projects"]["mirror-only"] = {"remotes": ["example.org/a/b"]}
        _raises(doc, "mirror-only", "nothing to clone")

    def test_empty_remotes_without_repo(self):
        doc = _doc()
        doc["projects"]["mirror-only"] = {"remotes": []}
        _raises(doc, "mirror-only", "nothing to clone")

    def test_null_remotes_is_absent(self):
        # `remotes:` with no value is YAML null — the key is absent, not a
        # list; a repoless entry written that way is still repoless.
        doc = _doc()
        doc["projects"]["bare"] = {"remotes": None}
        assert _registry_from(doc)["bare"].repoless

    @pytest.mark.parametrize("remotes", ["example.org/a/b", {"a": 1}, 3, True])
    def test_remotes_must_be_a_list(self, remotes):
        doc = _doc()
        doc["projects"][pf.PROJECT]["remotes"] = remotes
        _raises(doc, pf.PROJECT, "remotes:", "list")

    @pytest.mark.parametrize("bad", [None, 3, "", ["nested"], {"repo": "x"}])
    def test_every_remote_must_be_a_non_empty_string(self, bad):
        doc = _doc()
        doc["projects"][pf.PROJECT]["remotes"].append(bad)
        _raises(doc, pf.PROJECT, "remotes:")

    @pytest.mark.parametrize("bad", [3, True, [pf.PRIMARY_REPO], {"url": pf.PRIMARY_REPO}, ""])
    def test_repo_must_be_a_non_empty_string(self, bad):
        doc = _doc()
        doc["projects"][pf.PROJECT]["repo"] = bad
        _raises(doc, pf.PROJECT, "repo:")

    def test_null_repo_is_absent(self):
        doc = _doc()
        doc["projects"]["bare"] = {"repo": None}
        assert _registry_from(doc)["bare"].repoless

    @pytest.mark.parametrize("value", ["https://", "git@", "/", "   "])
    def test_value_that_normalizes_to_nothing_is_rejected(self, value):
        doc = _doc()
        doc["projects"][pf.PROJECT]["repo"] = value
        _raises(doc, pf.PROJECT, "repo:")

    @pytest.mark.parametrize("value", ["origin", "upstream", "widgets.git"])
    def test_a_bare_remote_name_is_not_a_url(self, value):
        # `repo: origin` type-checks and normalizes to something non-empty,
        # so only a HOST test catches it — and it must, because a hostless
        # key would become a registry key and a bucket of its own (D8).
        doc = _doc()
        doc["projects"][pf.PROJECT]["repo"] = value
        _raises(doc, pf.PROJECT, "repo:", "no host")

    def test_description_must_be_a_string(self):
        doc = _doc()
        doc["projects"][pf.REPOLESS_PROJECT]["description"] = ["a", "b"]
        _raises(doc, pf.REPOLESS_PROJECT, "description:")

    @pytest.mark.parametrize(
        "name", ["Widgets", "git.example.com", "_lmer", "-x", "a/b", "a b", "a.b"]
    )
    def test_short_name_charset(self, name):
        doc = _doc()
        doc["projects"][name] = {"repo": "example.org/x/y"}
        _raises(doc, repr(name), "[a-z0-9][a-z0-9_-]*")

    def test_unknown_entry_keys_are_not_an_error(self):
        # Forward-compat: the loader WARNs, the registry ignores.
        doc = _doc()
        doc["projects"][pf.PROJECT]["future_key"] = "x"
        assert _registry_from(doc)[pf.PROJECT].keys == (pf.PRIMARY_REPO, *pf.REMOTES)


class TestEmbeddedCredential:
    """A credential inside the manifest is a refuse-load error (the no-secrets
    rule), and the message never echoes it."""

    def test_in_repo(self):
        doc = _doc()
        doc["projects"][pf.PROJECT]["repo"] = f"https://oauth2:{TOKEN}@{HOST}/{PATH}.git"
        message = _raises(doc, pf.PROJECT, "repo:", "credential")
        assert TOKEN not in message

    def test_in_a_remote(self):
        doc = _doc()
        doc["projects"][pf.PROJECT]["remotes"].append(f"https://user:{TOKEN}@example.org/a/b")
        message = _raises(doc, pf.PROJECT, "remotes:", "credential")
        assert TOKEN not in message

    def test_scp_form_credential(self):
        doc = _doc()
        doc["projects"][pf.PROJECT]["remotes"].append(f"user:{TOKEN}@example.org:a/b")
        message = _raises(doc, pf.PROJECT, "credential")
        assert TOKEN not in message

    def test_bare_ssh_userinfo_is_not_a_credential(self):
        doc = _doc()
        doc["projects"][pf.PROJECT]["remotes"].append("ssh://git@example.org/a/b")
        assert "example.org/a/b" in _registry_from(doc)[pf.PROJECT].keys

    def test_duplicate_error_never_echoes_a_credential(self):
        # The credential check runs per value BEFORE the union is built, so
        # a credentialed duplicate fails on the credential — scrubbed.
        doc = _doc()
        doc["projects"]["fork"] = {"repo": f"https://oauth2:{TOKEN}@{HOST}/{PATH}"}
        message = _raises(doc, "fork")
        assert TOKEN not in message


# ---------------------------------------------------------------------------
# D21: no local normalization
# ---------------------------------------------------------------------------


def test_keys_are_exactly_the_sources_normalizer_folded():
    doc = _doc()
    for key in _registry_from(doc)[pf.PROJECT].keys:
        assert key == normalize_repo_url(key).lower()


def test_module_does_no_url_parsing_of_its_own():
    """Every URL comparison goes through ``lmer_schema.urls`` (D21); this
    module imports no normalizer and dissects no URL itself."""
    import ast
    from pathlib import Path

    source = (Path(reg.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imported.add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    assert "urllib" not in " ".join(imported)
    assert "lmer_cli.container.sources.normalize_repo_url" not in imported
    assert "lmer_cli.container.sources._parse_repo_url" not in imported
    assert {
        "lmer_schema.urls.lookup_key",
        "lmer_schema.urls.normalize",
        "lmer_schema.urls.entry_keys",
    } <= imported
    assert "lmer_cli.container.sources.scrub_credentials" in imported
    assert "lmer_cli.container.sources.url_has_embedded_credential" in imported
