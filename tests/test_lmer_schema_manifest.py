"""``lmer_schema.manifest`` — parsing ``_lmer.yaml`` into a typed model (issue #353).

The loader is the first thing every schema-form consumer touches (form
detection, the registry, the validator's manifest checks, the work CLI's
dual-read), so these tests pin its contract rather than its internals:

- the §4 fixture manifest loads field by field into ``Manifest``;
- "no manifest" (a legacy tree) is ``None`` — the legacy-form signal — and is
  the ONLY silent outcome. A manifest that is present but unreadable,
  unparseable, non-mapping, or carrying a ``schema:`` the loader cannot
  interpret raises ``ManifestError``: D20 forbids a broken manifest demoting a
  schema-form plexus to legacy, the same rule ``load_sources`` applies to a
  present ``sources.yaml``;
- ``schema: true`` is rejected BEFORE the int check (the read_taskdef_schema
  bool trap: ``isinstance(True, int)`` is true);
- a newer ``schema:`` int is rejected with an "upgrade lmer" message (§8
  version floor);
- unknown top-level and per-section keys are forward-compat WARNINGS carried
  on the model, never errors, and the raw sections keep them; warnings come
  out in document order;
- a ``projects:`` key MUST be a §1 short name (``[a-z0-9][a-z0-9_-]*``) and
  the ``repo``/``remotes`` values MUST be a non-empty string / a list of
  non-empty strings — both ``ManifestError``, never a stray ``TypeError`` or
  six one-character registry keys. Those are value TYPES only; the registry's
  semantics (primary vs additional remotes, duplicates, D19 repoless entries)
  are ``lmer_schema.registry``'s and are NOT pinned here.

Every negative test mutates ITS OWN fresh fixture tree (the shared builder is
consumed read-only).
"""

import re
from pathlib import Path

import pytest
import yaml

from tests import plexus_fixtures as pf
from lmer_schema.manifest import (
    CURRENT_SCHEMA,
    MANIFEST_NAME,
    SHORT_NAME_RE,
    SUPPORTED_SCHEMAS,
    Manifest,
    ManifestError,
    is_short_name,
    load_manifest,
    parse_manifest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _fixture_doc():
    """The §4 fixture manifest as a plain dict, for targeted mutation."""
    return yaml.safe_load(pf.SCHEMA_MANIFEST)


def _rewrite(tree, text):
    """Overwrite this test's own manifest with *text*."""
    tree.manifest.write_text(text, encoding="utf-8")


def _rewrite_doc(tree, doc):
    _rewrite(tree, yaml.safe_dump(doc, sort_keys=False))


def _sources_host(tree):
    return tree.primary_repo.split("/", 1)[0]


# ---------------------------------------------------------------------------
# Constants the rest of the package (and the docs) rely on
# ---------------------------------------------------------------------------


def test_manifest_name_matches_the_fixture_and_spec():
    assert MANIFEST_NAME == "_lmer.yaml"
    assert MANIFEST_NAME == pf.MANIFEST_NAME


def test_every_independent_spelling_of_the_manifest_name_agrees():
    """The name is mirrored as a module-local constant wherever importing core
    is not an option (#587). Each mirror must say what core says."""
    from hooks.run_state_guard import PLEXUS_MANIFEST
    from lmer_cli.container import clone_and_exec, sources
    from tests import schema2_fixtures
    from work_repo import utils

    assert {
        PLEXUS_MANIFEST,
        clone_and_exec.PLEXUS_MANIFEST_NAME,
        sources.MANIFEST_FILENAME,
        schema2_fixtures.MANIFEST_NAME,
        utils.MANIFEST_NAME,
    } == {MANIFEST_NAME}


def test_nothing_shipped_looks_for_the_old_manifest_name():
    """The manifest's old name was replaced outright, with no fallback (#587):
    no code, hook or doc may still read, accept or name it."""
    old = "_plexus" + ".yaml"
    shipped = [REPO_ROOT / "AGENTS.md"]
    for tree in ("src", "hooks", "bin", "docs", "lmer-docs", "web/src"):
        shipped += [p for p in (REPO_ROOT / tree).rglob("*") if p.is_file()]
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in shipped
        if "__pycache__" not in path.parts
        and old in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not offenders, f"{offenders} still spell {old}"


def test_supported_schemas_include_legacy_and_process_records():
    """Both tree contracts remain readable; new manifests select schema 2."""
    assert SUPPORTED_SCHEMAS == (1, 2)
    assert CURRENT_SCHEMA == 2
    assert CURRENT_SCHEMA == max(SUPPORTED_SCHEMAS)


# ---------------------------------------------------------------------------
# Happy path — the §4 fixture manifest, field by field
# ---------------------------------------------------------------------------


def test_loads_fixture_manifest_field_by_field(schema_plexus):
    loaded = load_manifest(schema_plexus.root)

    assert isinstance(loaded, Manifest)
    assert loaded.path == schema_plexus.manifest
    assert loaded.schema == 1
    assert loaded.name == pf.INSTANCE_NAME

    # The registry block is RAW: short name -> the entry exactly as written.
    # Its semantics (primary vs additional remotes, normalization, D19
    # repoless entries) belong to lmer_schema.registry, not the loader.
    assert set(loaded.projects) == {pf.PROJECT, pf.REPOLESS_PROJECT}
    assert loaded.projects[pf.PROJECT] == {
        "repo": pf.PRIMARY_REPO,
        "remotes": list(pf.REMOTES),
    }
    assert loaded.projects[pf.REPOLESS_PROJECT] == {
        "description": pf.REPOLESS_DESCRIPTION,
    }

    host = _sources_host(schema_plexus)
    assert loaded.sources == {
        "taskdef": {"repo": f"https://{host}/agents/taskdef.git", "ref": "main"},
        "napkin": {"repo": f"https://{host}/agents/napkin.git"},
    }
    assert loaded.housekeeping == {
        "napkin": {"archive_after": "365d"},
        "plans": {"archive_after": None},
    }

    # A manifest written exactly as §4 spells it produces no warnings.
    assert loaded.warnings == ()


def test_model_is_frozen(schema_plexus):
    loaded = load_manifest(schema_plexus.root)
    with pytest.raises((AttributeError, TypeError)):
        loaded.schema = 2


def test_absent_optional_sections_default_to_empty(schema_plexus):
    """``projects``/``sources``/``housekeeping`` are optional (SCHEMA.md
    top-level key table); absent or ``null`` both mean "none declared"."""
    _rewrite(schema_plexus, "schema: 1\nname: bare\n")
    loaded = load_manifest(schema_plexus.root)
    assert (loaded.projects, loaded.sources, loaded.housekeeping) == ({}, {}, {})
    assert loaded.warnings == ()

    _rewrite(schema_plexus, "schema: 1\nname: bare\nprojects:\nsources:\nhousekeeping:\n")
    loaded = load_manifest(schema_plexus.root)
    assert (loaded.projects, loaded.sources, loaded.housekeeping) == ({}, {}, {})


def test_bare_project_entry_is_an_empty_entry(schema_plexus):
    """D19: ``name:`` with no keys at all is the zero-remote count — a repoless
    project registered by name alone."""
    _rewrite(schema_plexus, "schema: 1\nname: x\nprojects:\n  q3-capacity:\n")
    loaded = load_manifest(schema_plexus.root)
    assert loaded.projects == {"q3-capacity": {}}
    assert loaded.warnings == ()


def test_parse_manifest_is_the_pure_half(schema_plexus):
    """``parse_manifest`` parses text without touching disk; ``load_manifest``
    is that plus reading the file. Same model either way."""
    text = schema_plexus.manifest.read_text(encoding="utf-8")
    parsed = parse_manifest(text, path=schema_plexus.manifest)
    assert parsed == load_manifest(schema_plexus.root)


# ---------------------------------------------------------------------------
# No manifest — the legacy-form signal
# ---------------------------------------------------------------------------


def test_legacy_tree_has_no_manifest(legacy_plexus):
    """A legacy tree (bare names, standalone ``sources.yaml``) loads as
    ``None``: the standalone ``sources.yaml`` is NOT a manifest and must not
    be mistaken for one."""
    assert legacy_plexus.manifest is None
    assert legacy_plexus.sources_file is not None and legacy_plexus.sources_file.is_file()
    assert load_manifest(legacy_plexus.root) is None


def test_schema_tree_without_its_manifest_is_legacy(schema_plexus):
    """Removing the manifest is the one way a tree becomes legacy form: the
    ``_``-owned areas around it do not make it schema form on their own."""
    schema_plexus.manifest.unlink()
    assert load_manifest(schema_plexus.root) is None


# ---------------------------------------------------------------------------
# Present but broken — always loud (D20), never a degrade to legacy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["true", "false"])
def test_bool_schema_rejected(schema_plexus, value):
    """The read_taskdef_schema bool trap: ``schema: true`` is a YAML bool and
    ``isinstance(True, int)`` would otherwise let it through as schema 1."""
    _rewrite(schema_plexus, f"schema: {value}\nname: x\n")
    with pytest.raises(ManifestError, match=r"integer `schema:`"):
        load_manifest(schema_plexus.root)


@pytest.mark.parametrize(
    "value",
    ['"1"', "1.0", "null", "[1]", "{v: 1}", "one"],
    ids=["string", "float", "null", "list", "mapping", "word"],
)
def test_non_int_schema_rejected(schema_plexus, value):
    _rewrite(schema_plexus, f"schema: {value}\nname: x\n")
    with pytest.raises(ManifestError, match=r"integer `schema:`"):
        load_manifest(schema_plexus.root)


def test_missing_schema_rejected(schema_plexus):
    _rewrite(schema_plexus, "name: x\nprojects: {}\n")
    with pytest.raises(ManifestError, match=r"integer `schema:`") as excinfo:
        load_manifest(schema_plexus.root)
    # The supported set is named so the operator knows what to write.
    assert "1" in str(excinfo.value)


def test_newer_schema_rejected_with_upgrade_message(schema_plexus):
    """§8 version floor: a newer int means a newer lmer wrote this tree; the
    fix is to upgrade lmer, never to guess at the newer shape."""
    newer = CURRENT_SCHEMA + 1
    _rewrite(schema_plexus, f"schema: {newer}\nname: x\n")
    with pytest.raises(ManifestError) as excinfo:
        load_manifest(schema_plexus.root)
    message = str(excinfo.value)
    assert f"schema {newer}" in message
    assert "supports: 1" in message
    assert "pgrade lmer" in message


@pytest.mark.parametrize("value", ["0", "-1"])
def test_unsupported_older_int_rejected(schema_plexus, value):
    """Nothing older than 1 exists; an int below the floor is not a schema
    at all and gets the plain unsupported message, not the upgrade nudge."""
    _rewrite(schema_plexus, f"schema: {value}\nname: x\n")
    with pytest.raises(ManifestError, match=r"supports: 1") as excinfo:
        load_manifest(schema_plexus.root)
    assert "pgrade lmer" not in str(excinfo.value)


def test_unparseable_yaml_rejected(schema_plexus):
    _rewrite(schema_plexus, "schema: 1\nname: [unterminated\nprojects: {\n")
    with pytest.raises(ManifestError, match=r"unparseable") as excinfo:
        load_manifest(schema_plexus.root)
    assert str(schema_plexus.manifest) in str(excinfo.value)


@pytest.mark.parametrize(
    "text",
    ["", "- schema: 1\n", "just a string\n", "42\n"],
    ids=["empty", "list", "scalar-string", "scalar-int"],
)
def test_non_mapping_document_rejected(schema_plexus, text):
    """An empty file is NOT "no manifest": the file is present, so the tree
    is schema form, and an empty manifest is a broken one."""
    _rewrite(schema_plexus, text)
    with pytest.raises(ManifestError, match=r"YAML mapping"):
        load_manifest(schema_plexus.root)


def test_unreadable_manifest_rejected(schema_plexus):
    """A directory where the file should be is present-but-unreadable, not
    absent."""
    schema_plexus.manifest.unlink()
    schema_plexus.manifest.mkdir()
    with pytest.raises(ManifestError, match=r"unreadable"):
        load_manifest(schema_plexus.root)


def test_dangling_symlink_manifest_rejected(schema_plexus):
    """A dangling ``_lmer.yaml`` symlink is a present, broken manifest —
    ``Path.exists()`` alone would report it absent and demote the tree."""
    schema_plexus.manifest.unlink()
    schema_plexus.manifest.symlink_to("nowhere.yaml")
    with pytest.raises(ManifestError, match=r"unreadable"):
        load_manifest(schema_plexus.root)


@pytest.mark.parametrize(
    "text",
    ["schema: 1\n", "schema: 1\nname: null\n", "schema: 1\nname: 20\n",
     "schema: 1\nname: [a]\n"],
    ids=["missing", "null", "int", "list"],
)
def test_name_must_be_a_string(schema_plexus, text):
    """``name`` is required (SCHEMA.md top-level key table) and a string."""
    _rewrite(schema_plexus, text)
    with pytest.raises(ManifestError, match=r"`name:`"):
        load_manifest(schema_plexus.root)


@pytest.mark.parametrize("section", ["projects", "sources", "housekeeping"])
@pytest.mark.parametrize("value", ["[a, b]", "text", "3"], ids=["list", "string", "int"])
def test_non_mapping_section_rejected(schema_plexus, section, value):
    _rewrite(schema_plexus, f"schema: 1\nname: x\n{section}: {value}\n")
    with pytest.raises(ManifestError, match=rf"`{section}:` must be a mapping"):
        load_manifest(schema_plexus.root)


@pytest.mark.parametrize("section", ["projects", "sources", "housekeeping"])
def test_non_mapping_section_entry_rejected(schema_plexus, section):
    _rewrite(schema_plexus, f"schema: 1\nname: x\n{section}:\n  thing: just-a-string\n")
    with pytest.raises(ManifestError, match=rf"`{section}\.thing`"):
        load_manifest(schema_plexus.root)


def test_non_string_project_name_rejected(schema_plexus):
    """YAML turns an unquoted ``123:`` (or ``1_0:``) into an int key. The
    loader never coerces a short name (D9: nothing is guessed); it tells the
    operator to quote it."""
    _rewrite(schema_plexus, "schema: 1\nname: x\nprojects:\n  123:\n    description: d\n")
    with pytest.raises(ManifestError, match=r"quote") as excinfo:
        load_manifest(schema_plexus.root)
    assert "123" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Registry keys — THE §1 short-name charset check lives in the loader
# ---------------------------------------------------------------------------


def test_short_name_charset_is_the_documented_one():
    """``SHORT_NAME_RE`` is the §1 rule SCHEMA.md spells out; consumers that
    join a key onto a path rely on the loader having applied exactly it."""
    assert SHORT_NAME_RE.pattern == r"[a-z0-9][a-z0-9_-]*"
    schema_md = (REPO_ROOT / "docs" / "SCHEMA.md").read_text(encoding="utf-8")
    assert f"Short names MUST match `{SHORT_NAME_RE.pattern}`" in schema_md
    assert is_short_name("widgets")
    assert is_short_name("q3-capacity")
    assert not is_short_name("Widgets")
    assert not is_short_name(123)
    assert not is_short_name("")


@pytest.mark.parametrize("key", ["a", "0day", "q3-capacity", "x_y", "widgets2"])
def test_short_name_keys_load(schema_plexus, key):
    doc = {"schema": 1, "name": "x", "projects": {key: {"description": "d"}}}
    _rewrite_doc(schema_plexus, doc)
    loaded = load_manifest(schema_plexus.root)
    assert loaded.projects == {key: {"description": "d"}}
    assert loaded.warnings == ()


@pytest.mark.parametrize(
    "key",
    ["../../escape", "/tmp/abs", "Lmer", "a.b", "_hidden", "-x", "a b", ""],
    ids=["dotdot", "absolute", "uppercase", "dot", "leading-underscore",
         "leading-dash", "space", "empty"],
)
def test_project_key_must_be_a_short_name(schema_plexus, key):
    """A registry key becomes a directory name, so a key outside the §1
    charset is refused (fail-closed, like the ``123:`` quoting error) and the
    error names the key. Every consumer may rely on ``Manifest.projects``
    keys being short names — the loader is the one place that checks."""
    doc = {"schema": 1, "name": "x", "projects": {key: {"description": "d"}}}
    _rewrite_doc(schema_plexus, doc)
    with pytest.raises(ManifestError, match=r"short name") as excinfo:
        load_manifest(schema_plexus.root)
    message = str(excinfo.value)
    assert repr(key) in message
    assert SHORT_NAME_RE.pattern in message


# ---------------------------------------------------------------------------
# Registry entry VALUES — types are the loader's, semantics the registry's
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [12345, True, ["https://example.com/a/b"], {"url": "https://example.com/a/b"},
     "", "   "],
    ids=["int", "bool", "list", "mapping", "empty", "blank"],
)
def test_project_repo_must_be_a_non_empty_string(schema_plexus, value):
    """``repo: 12345`` used to escape as an ``AttributeError`` from the URL
    adapter; a present manifest with a wrong-typed ``repo:`` is broken and
    fails the documented way."""
    doc = _fixture_doc()
    doc["projects"][pf.PROJECT]["repo"] = value
    _rewrite_doc(schema_plexus, doc)
    where = rf"`projects\.{re.escape(pf.PROJECT)}\.repo:`"
    with pytest.raises(ManifestError, match=where):
        load_manifest(schema_plexus.root)


@pytest.mark.parametrize(
    "value",
    ["origin", 5, True, {"origin": "https://example.com/a/b"},
     ["https://example.com/a/b", 5], [""], ["  "], [None]],
    ids=["string", "int", "bool", "mapping", "int-element", "empty-element",
         "blank-element", "null-element"],
)
def test_project_remotes_must_be_a_list_of_non_empty_strings(schema_plexus, value):
    """``remotes: origin`` (a remote NAME, not a list) used to be iterated as
    a string and gave the project six one-character lookup keys; ``remotes: 5``
    was a ``TypeError``. Both are ``ManifestError`` naming the key."""
    doc = _fixture_doc()
    doc["projects"][pf.PROJECT]["remotes"] = value
    _rewrite_doc(schema_plexus, doc)
    where = rf"`projects\.{re.escape(pf.PROJECT)}\.remotes:"
    with pytest.raises(ManifestError, match=where) as excinfo:
        load_manifest(schema_plexus.root)
    if isinstance(value, list):
        # The offending element is pointed at, not just the list.
        assert f".remotes:[{len(value) - 1}]`" in str(excinfo.value)
    else:
        assert repr(value) in str(excinfo.value)


def test_null_repo_and_remotes_read_as_absent(schema_plexus):
    """``repo:`` / ``remotes:`` written with no value are absent keys, not
    wrong-typed ones (the registry's ``entry.get`` reads them the same way);
    the raw entry still carries them as written."""
    doc = _fixture_doc()
    doc["projects"][pf.PROJECT] = {"repo": None, "remotes": None}
    _rewrite_doc(schema_plexus, doc)
    loaded = load_manifest(schema_plexus.root)
    assert loaded.projects[pf.PROJECT] == {"repo": None, "remotes": None}
    assert loaded.warnings == ()


def test_entry_semantics_are_not_the_loaders(schema_plexus):
    """Well-typed but semantically wrong entries load: ``remotes:`` without
    ``repo:``, ``repo:`` repeated in ``remotes:``, two projects sharing a
    remote. Those are the registry's D8/D19 rules, raised there, not here."""
    url = "https://example.com/a/b"
    doc = {"schema": 1, "name": "x", "projects": {
        "no-primary": {"remotes": [url]},
        "repeated": {"repo": url, "remotes": [url]},
        "shared": {"repo": url},
    }}
    _rewrite_doc(schema_plexus, doc)
    loaded = load_manifest(schema_plexus.root)
    assert set(loaded.projects) == {"no-primary", "repeated", "shared"}
    assert loaded.warnings == ()


def test_manifest_error_carries_the_path(schema_plexus):
    _rewrite(schema_plexus, "schema: true\nname: x\n")
    with pytest.raises(ManifestError) as excinfo:
        load_manifest(schema_plexus.root)
    assert excinfo.value.path == schema_plexus.manifest
    assert str(schema_plexus.manifest) in str(excinfo.value)


def test_manifest_error_is_not_a_yaml_or_os_error():
    """Callers catch ONE type; the loader wraps parser and I/O failures."""
    assert not issubclass(ManifestError, yaml.YAMLError)
    assert not issubclass(ManifestError, OSError)


# ---------------------------------------------------------------------------
# Forward-compat warnings — unknown keys never raise, and are kept raw
# ---------------------------------------------------------------------------


def test_unknown_top_level_key_warned(schema_plexus):
    doc = _fixture_doc()
    doc["tenancy"] = {"tier": "gold"}
    _rewrite_doc(schema_plexus, doc)
    loaded = load_manifest(schema_plexus.root)
    assert len(loaded.warnings) == 1
    assert "`tenancy`" in loaded.warnings[0]
    assert "forward-compat" in loaded.warnings[0]
    assert str(schema_plexus.manifest) in loaded.warnings[0]
    # Everything known still loaded exactly as before.
    assert loaded.name == pf.INSTANCE_NAME
    assert set(loaded.projects) == {pf.PROJECT, pf.REPOLESS_PROJECT}


def test_unknown_project_entry_key_warned_and_kept(schema_plexus):
    doc = _fixture_doc()
    doc["projects"][pf.PROJECT]["colour"] = "red"
    _rewrite_doc(schema_plexus, doc)
    loaded = load_manifest(schema_plexus.root)
    assert len(loaded.warnings) == 1
    assert f"`projects.{pf.PROJECT}.colour`" in loaded.warnings[0]
    # Raw means raw: the registry sees the entry as written.
    assert loaded.projects[pf.PROJECT]["colour"] == "red"


def test_known_project_entry_keys_do_not_warn(schema_plexus):
    """``repo``, ``remotes`` and ``description`` are the documented entry
    vocabulary (SCHEMA.md registry table)."""
    doc = _fixture_doc()
    doc["projects"][pf.REPOLESS_PROJECT] = {"repo": "example.com/a/b", "remotes": [],
                                            "description": "d"}
    _rewrite_doc(schema_plexus, doc)
    assert load_manifest(schema_plexus.root).warnings == ()


def test_unknown_source_name_warned_and_kept(schema_plexus):
    doc = _fixture_doc()
    doc["sources"]["widgets"] = {"repo": "https://example.com/w.git"}
    _rewrite_doc(schema_plexus, doc)
    loaded = load_manifest(schema_plexus.root)
    assert len(loaded.warnings) == 1
    assert "`sources.widgets`" in loaded.warnings[0]
    assert "widgets" in loaded.sources


@pytest.mark.parametrize("area", ["knowledge", "plans"])
def test_reserved_source_name_warned_by_name(schema_plexus, area):
    """``knowledge``/``plans`` are documented as reserved mounts (SCHEMA.md
    sources table): the warning says known-but-unsupported, not typo."""
    doc = _fixture_doc()
    doc["sources"][area] = {"repo": "https://example.com/k.git"}
    _rewrite_doc(schema_plexus, doc)
    loaded = load_manifest(schema_plexus.root)
    assert len(loaded.warnings) == 1
    assert f"`sources.{area}`" in loaded.warnings[0]
    assert "reserved" in loaded.warnings[0]


def test_unknown_source_entry_key_warned(schema_plexus):
    doc = _fixture_doc()
    doc["sources"]["taskdef"]["branch"] = "main"
    _rewrite_doc(schema_plexus, doc)
    loaded = load_manifest(schema_plexus.root)
    assert len(loaded.warnings) == 1
    assert "`sources.taskdef.branch`" in loaded.warnings[0]


def test_a_ref_under_a_source_that_does_not_take_one_is_warned(schema_plexus):
    """``ref:`` is ``taskdef``'s key alone (SCHEMA.md sources table:
    ``taskdef`` is ``{repo, ref?}``, ``napkin`` ``{repo}``), so the entry
    vocabulary is per NAME rather than one uniform set.

    What a manifest carrying ``sources.napkin.ref`` does today is nothing at
    all: the ``sources:`` section is declared-only until its loader lands
    (issue #370 — ``bin/doctor`` says so, and container start reads
    ``{work_repo}/sources.yaml``), so the ref takes effect nowhere and,
    without this warning, no tool mentions it. ``load_sources`` already
    refuses the same two keys under ``napkin`` as a refuse-start error, so
    every manifest written with one becomes a tree that will not start the
    day the loader arrives. The warning has to exist first.
    """
    doc = _fixture_doc()
    doc["sources"]["napkin"]["ref"] = "main"
    _rewrite_doc(schema_plexus, doc)
    loaded = load_manifest(schema_plexus.root)
    assert len(loaded.warnings) == 1
    assert "`sources.napkin.ref`" in loaded.warnings[0]
    # Recoverable means recoverable: the manifest still loads, raw.
    assert loaded.sources["napkin"] == {
        "repo": f"https://{_sources_host(schema_plexus)}/agents/napkin.git",
        "ref": "main",
    }


def test_the_taskdef_ref_is_documented_and_never_warned(schema_plexus):
    """The other half: ``ref`` under the ONE name that documents it is the
    §4 fixture's own spelling and stays silent."""
    doc = _fixture_doc()
    doc["sources"]["taskdef"]["ref"] = "release-1.2"
    _rewrite_doc(schema_plexus, doc)
    assert load_manifest(schema_plexus.root).warnings == ()


def test_a_reserved_source_takes_no_ref_either(schema_plexus):
    """``knowledge``/``plans`` are documented as ``{repo}`` (SCHEMA.md sources
    table): reserved names get the reserved warning AND the key warning, so
    the operator learns both things about the entry."""
    doc = _fixture_doc()
    doc["sources"]["knowledge"] = {"repo": "https://example.com/k.git", "ref": "main"}
    _rewrite_doc(schema_plexus, doc)
    warnings = load_manifest(schema_plexus.root).warnings
    assert len(warnings) == 2
    assert "reserved" in warnings[0]
    assert "`sources.knowledge.ref`" in warnings[1]


def test_unknown_housekeeping_policy_key_warned(schema_plexus):
    doc = _fixture_doc()
    doc["housekeeping"]["napkin"]["purge_after"] = "30d"
    _rewrite_doc(schema_plexus, doc)
    loaded = load_manifest(schema_plexus.root)
    assert len(loaded.warnings) == 1
    assert "`housekeeping.napkin.purge_after`" in loaded.warnings[0]


def test_any_housekeeping_area_is_accepted(schema_plexus):
    """The area key set is the housekeeping tool's to document ("and any
    other area"); the loader does not second-guess it."""
    doc = _fixture_doc()
    doc["housekeeping"]["knowledge"] = {"archive_after": None}
    _rewrite_doc(schema_plexus, doc)
    assert load_manifest(schema_plexus.root).warnings == ()


def test_warnings_accumulate_in_document_order(schema_plexus):
    """Document order, not tier order: the sections are written here in the
    reverse of the §4 layout and the unknown top-level key comes last, so a
    loader that reported "top-level first, then projects, sources,
    housekeeping" would fail this."""
    fixture = _fixture_doc()
    fixture["housekeeping"]["plans"]["purge_after"] = "1d"
    fixture["sources"]["taskdef"]["branch"] = "main"
    fixture["projects"][pf.PROJECT]["colour"] = "red"
    doc = {
        "schema": fixture["schema"],
        "name": fixture["name"],
        "housekeeping": fixture["housekeeping"],
        "sources": fixture["sources"],
        "projects": fixture["projects"],
        "extra_top": 1,
    }
    _rewrite_doc(schema_plexus, doc)
    loaded = load_manifest(schema_plexus.root)
    keys = [w.split("`")[1] for w in loaded.warnings]
    assert keys == [
        "housekeeping.plans.purge_after",
        "sources.taskdef.branch",
        f"projects.{pf.PROJECT}.colour",
        "extra_top",
    ]


_BROKEN = {
    "unparseable": "schema: 1\nname: [unterminated\n",
    "non-mapping": "- schema: 1\n",
    "bool-schema": "schema: true\nname: x\n",
    "newer-schema": f"schema: {CURRENT_SCHEMA + 1}\nname: x\n",
    "no-name": "schema: 1\n",
    "section-not-mapping": "schema: 1\nname: x\nprojects: [a]\n",
    "entry-not-mapping": "schema: 1\nname: x\nprojects:\n  p: s\n",
    "int-key": "schema: 1\nname: x\nprojects:\n  123: {}\n",
    "bad-short-name": "schema: 1\nname: x\nprojects:\n  Lmer: {}\n",
    "repo-int": "schema: 1\nname: x\nprojects:\n  p: {repo: 12345}\n",
    "remotes-scalar": (
        "schema: 1\nname: x\nprojects:\n"
        "  p: {repo: https://example.com/a/b, remotes: origin}\n"
    ),
}


@pytest.mark.parametrize("text", list(_BROKEN.values()), ids=list(_BROKEN))
def test_every_broken_manifest_is_caught_by_the_one_type(text):
    """The error taxonomy as behaviour: consumers (form detection, registry,
    validator checks) write ONE ``except ManifestError`` and it catches every
    present-but-broken shape — nothing leaks as a YAML, OS, Type or Attribute
    error."""
    with pytest.raises(ManifestError):
        parse_manifest(text)
