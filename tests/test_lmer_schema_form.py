"""``lmer_schema.form`` — which form is this tree in? (issue #353, spec D20).

Form detection is the first question every dual-read consumer asks, and it has
exactly TWO answers: a plexus root holding a ``_plexus.yaml`` is SCHEMA form,
one without it is LEGACY form. These tests pin that contract against the two
shared fixture trees:

- the schema-form tree detects as ``Form.SCHEMA``, the legacy twin as
  ``Form.LEGACY``, and an empty directory is legacy (absence is the signal);
- a manifest that is PRESENT but broken — unparseable, empty, non-mapping, a
  bool ``schema:``, a directory or a dangling symlink in its place — is still
  SCHEMA form, and the loader is the thing that fails (D20: a typo can never
  silently demote a schema-form plexus and route work at the legacy paths).
  Detection agrees with the loader by construction: LEGACY exactly when
  ``load_manifest`` returns ``None``;
- detection from a nested subdirectory resolves to the same root — the walk
  climbs to the nearest ancestor carrying a manifest, works on a file path and
  on a directory that does not exist yet, and never climbs past a caller's
  known root;
- the legacy form has NO marker of its own, so the upward walk finds nothing
  in a legacy tree and the caller's known root is the answer;
- the enum is the shared two-valued vocabulary: ``checks.findings.Form.coerce``
  accepts our members, so the validator can take detection's answer directly
  without either module importing the other.
"""

import ast
import inspect
from pathlib import Path

import pytest

from tests import plexus_fixtures as pf
from lmer_schema import form as form_module
from lmer_schema.checks.findings import Form as FindingsForm
from lmer_schema.form import (
    Detection,
    Form,
    detect,
    detect_form,
    find_plexus_root,
    has_manifest,
)
from lmer_schema import manifest as manifest_module
from lmer_schema.manifest import MANIFEST_NAME, ManifestError, load_manifest


def _replace_manifest(tree, write):
    """Swap this test's OWN fixture manifest for something broken.

    *write* receives the manifest path with nothing at it; the shared builder
    is consumed read-only, so every corruption lands on a fresh tree.
    """
    tree.manifest.unlink()
    write(tree.manifest)
    return tree.manifest


#: One corruption per taxonomy row of ``load_manifest``: every one of these is
#: a PRESENT manifest, so every one of them is schema form (D20).
BROKEN_MANIFESTS = {
    "unparseable": lambda p: p.write_text("schema: 1\n  name: [oops\n", encoding="utf-8"),
    "empty": lambda p: p.write_text("", encoding="utf-8"),
    "not-a-mapping": lambda p: p.write_text("- schema: 1\n", encoding="utf-8"),
    "no-name": lambda p: p.write_text("schema: 1\n", encoding="utf-8"),
    "bool-schema": lambda p: p.write_text("schema: true\nname: example\n", encoding="utf-8"),
    "newer-schema": lambda p: p.write_text("schema: 99\nname: example\n", encoding="utf-8"),
    "bad-short-name": lambda p: p.write_text(
        "schema: 1\nname: example\nprojects:\n  ../escape: {}\n", encoding="utf-8"
    ),
    "a-directory": lambda p: p.mkdir(),
    "dangling-symlink": lambda p: p.symlink_to("nowhere.yaml"),
}


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


def test_form_has_exactly_two_members():
    # D20: presence or absence of the manifest — there is no third answer and
    # no "unknown" member to branch on.
    assert [f.name for f in Form] == ["SCHEMA", "LEGACY"]


def test_form_values_are_the_documented_spelling():
    assert Form.SCHEMA.value == pf.SCHEMA_FORM == "schema"
    assert Form.LEGACY.value == pf.LEGACY_FORM == "legacy"


def test_form_is_the_shared_vocabulary_by_name_and_values():
    """``checks.findings.Form.coerce`` recognises a sibling enum structurally:
    a class named exactly ``Form`` carrying exactly these two values."""
    assert Form.__name__ == FindingsForm.__name__ == "Form"
    assert {f.value for f in Form} == {f.value for f in FindingsForm}


@pytest.mark.parametrize("member", list(Form))
def test_findings_coerce_accepts_our_members(member):
    # The validator takes detection's answer directly; neither module imports
    # the other, so this interop is a contract and not an accident.
    assert FindingsForm.coerce(member) is FindingsForm(member.value)


# ---------------------------------------------------------------------------
# The two fixture trees
# ---------------------------------------------------------------------------


def test_schema_tree_is_schema_form(schema_plexus):
    assert detect_form(schema_plexus.root) is Form.SCHEMA
    assert has_manifest(schema_plexus.root) is True


def test_legacy_tree_is_legacy_form(legacy_plexus):
    # The legacy twin has a standalone sources.yaml, bare areas and no
    # manifest: absence is the whole signal (§9).
    assert legacy_plexus.manifest is None
    assert detect_form(legacy_plexus.root) is Form.LEGACY
    assert has_manifest(legacy_plexus.root) is False


def test_an_empty_directory_is_legacy_form(tmp_path):
    assert detect_form(tmp_path) is Form.LEGACY


def test_a_missing_directory_is_legacy_form(tmp_path):
    # Detection creates nothing and requires nothing to exist; a path with no
    # manifest at it is legacy whether or not it is there yet.
    assert detect_form(tmp_path / "not-created-yet") is Form.LEGACY


def test_a_manifest_named_file_elsewhere_does_not_make_the_root_schema_form(legacy_plexus):
    (legacy_plexus.project_dir / MANIFEST_NAME).write_text(
        "schema: 1\nname: example\n", encoding="utf-8"
    )
    assert detect_form(legacy_plexus.root) is Form.LEGACY


def test_root_accepts_a_string_path(schema_plexus):
    assert detect_form(str(schema_plexus.root)) is Form.SCHEMA


# ---------------------------------------------------------------------------
# D20: a broken manifest never demotes a tree to legacy
# ---------------------------------------------------------------------------


class TestBrokenManifestStaysSchemaForm:
    @pytest.mark.parametrize("kind", sorted(BROKEN_MANIFESTS))
    def test_detection_reports_schema_form(self, schema_plexus, kind):
        _replace_manifest(schema_plexus, BROKEN_MANIFESTS[kind])
        assert detect_form(schema_plexus.root) is Form.SCHEMA

    @pytest.mark.parametrize("kind", sorted(BROKEN_MANIFESTS))
    def test_the_loader_is_the_thing_that_fails(self, schema_plexus, kind):
        # Detection stays silent and total; the ManifestError comes from the
        # loader, so a typo fails loud instead of routing work at the legacy
        # paths.
        _replace_manifest(schema_plexus, BROKEN_MANIFESTS[kind])
        assert detect_form(schema_plexus.root) is Form.SCHEMA
        with pytest.raises(ManifestError):
            load_manifest(schema_plexus.root)

    def test_detection_does_not_read_the_file(self, schema_plexus):
        # An unreadable manifest (mode 0) still detects; only the loader cares
        # whether the bytes can be had.
        schema_plexus.manifest.chmod(0o000)
        try:
            assert detect_form(schema_plexus.root) is Form.SCHEMA
        finally:
            schema_plexus.manifest.chmod(0o644)

    @pytest.mark.parametrize("kind", sorted(BROKEN_MANIFESTS))
    def test_the_walk_finds_a_broken_manifest_too(self, schema_plexus, kind):
        _replace_manifest(schema_plexus, BROKEN_MANIFESTS[kind])
        nested = schema_plexus.session_dirs[0]
        assert find_plexus_root(nested, stop=schema_plexus.root) == schema_plexus.root
        assert detect(nested, root=schema_plexus.root).form is Form.SCHEMA


def test_form_agrees_with_the_loaders_none_signal(schema_plexus, legacy_plexus):
    """LEGACY is exactly ``load_manifest(...) is None``; anything else is
    SCHEMA, whether it loads or raises."""
    assert load_manifest(legacy_plexus.root) is None
    assert detect_form(legacy_plexus.root) is Form.LEGACY
    assert load_manifest(schema_plexus.root) is not None
    assert detect_form(schema_plexus.root) is Form.SCHEMA


# ---------------------------------------------------------------------------
# Finding the root from inside the tree
# ---------------------------------------------------------------------------


class TestFindPlexusRoot:
    def test_the_root_finds_itself(self, schema_plexus):
        assert find_plexus_root(schema_plexus.root) == schema_plexus.root

    @pytest.mark.parametrize(
        "where",
        [
            "session_dir",
            "specs_dir",
            "instance_memory_dir",
            "project_memory_dir",
            "bucket_dir",
            "repoless_dir",
        ],
    )
    def test_a_nested_subdirectory_resolves_to_the_same_root(self, schema_plexus, where):
        nested = (
            schema_plexus.session_dirs[0]
            if where == "session_dir"
            else getattr(schema_plexus, where)
        )
        assert find_plexus_root(nested) == schema_plexus.root

    def test_a_file_path_resolves_from_its_directory(self, schema_plexus):
        assert (
            find_plexus_root(schema_plexus.session_dirs[0] / "state.yaml")
            == schema_plexus.root
        )

    def test_a_path_that_does_not_exist_yet_resolves(self, schema_plexus):
        # A session resolves its dir before creating it (registry.resolve_
        # project_dir has the same property).
        unborn = schema_plexus.runs_dir / "new-slug" / "20260825-120000-1-33333"
        assert find_plexus_root(unborn) == schema_plexus.root

    def test_a_string_path_is_accepted(self, schema_plexus):
        assert find_plexus_root(str(schema_plexus.specs_dir)) == schema_plexus.root

    def test_the_nearest_manifest_wins(self, schema_plexus):
        # Two manifests in one chain is a broken tree, not a form question;
        # the walk answers "the tree I am standing in" and the validator
        # reports the nesting.
        inner = schema_plexus.project_dir / MANIFEST_NAME
        inner.write_text("schema: 1\nname: inner\n", encoding="utf-8")
        assert (
            find_plexus_root(schema_plexus.session_dirs[0])
            == schema_plexus.project_dir
        )

    def test_a_legacy_tree_has_no_marker_to_find(self, legacy_plexus):
        # §9: bare names are not a marker — nothing on disk says "this is the
        # root", so the caller's own root is the only honest answer.
        nested = legacy_plexus.session_dirs[0]
        assert find_plexus_root(nested, stop=legacy_plexus.root) is None
        assert find_plexus_root(legacy_plexus.root, stop=legacy_plexus.root) is None


class TestSearchBoundary:
    def test_stop_is_inclusive(self, schema_plexus):
        assert (
            find_plexus_root(schema_plexus.session_dirs[0], stop=schema_plexus.root)
            == schema_plexus.root
        )

    def test_the_walk_never_climbs_past_stop(self, schema_plexus):
        nested = schema_plexus.session_dirs[0]
        assert find_plexus_root(nested, stop=nested) is None
        assert find_plexus_root(nested, stop=schema_plexus.project_dir) is None

    def test_a_stop_outside_the_chain_fails_loud(self, schema_plexus, tmp_path):
        # Silently walking to the filesystem root would let a mis-spelled
        # boundary find a plexus the caller never meant.
        with pytest.raises(ValueError):
            find_plexus_root(schema_plexus.session_dirs[0], stop=tmp_path / "elsewhere")


# ---------------------------------------------------------------------------
# The composed answer
# ---------------------------------------------------------------------------


class TestDetect:
    def test_detection_is_frozen(self, schema_plexus):
        detection = detect(schema_plexus.root)
        with pytest.raises(Exception):
            detection.form = Form.LEGACY

    def test_schema_form_at_the_root(self, schema_plexus):
        detection = detect(schema_plexus.root)
        assert detection == Detection(
            form=Form.SCHEMA,
            root=schema_plexus.root,
            manifest=schema_plexus.manifest,
        )

    def test_schema_form_from_a_nested_subdirectory(self, schema_plexus):
        detection = detect(schema_plexus.session_dirs[1])
        assert detection.form is Form.SCHEMA
        assert detection.root == schema_plexus.root
        assert detection.manifest == schema_plexus.manifest

    def test_legacy_form_at_a_known_root(self, legacy_plexus):
        detection = detect(legacy_plexus.root, root=legacy_plexus.root)
        assert detection == Detection(
            form=Form.LEGACY, root=legacy_plexus.root, manifest=None
        )

    def test_legacy_form_from_a_nested_subdirectory_of_a_known_root(self, legacy_plexus):
        detection = detect(legacy_plexus.session_dirs[0], root=legacy_plexus.root)
        assert detection.form is Form.LEGACY
        assert detection.root == legacy_plexus.root
        assert detection.manifest is None

    def test_legacy_form_with_no_known_root_has_no_root(self, legacy_plexus):
        # Nothing on disk marks a legacy root, so detection does not invent
        # one (the D9 rule about names, applied to roots): the caller either
        # knows its root or gets ``None``.
        detection = detect(legacy_plexus.session_dirs[0])
        assert detection == Detection(form=Form.LEGACY, root=None, manifest=None)

    def test_a_known_root_bounds_the_walk(self, schema_plexus):
        # The caller asked about THIS tree; a manifest above it is another
        # tree's and must not answer for it.
        nested = schema_plexus.session_dirs[0]
        detection = detect(nested, root=schema_plexus.project_dir)
        assert detection.form is Form.LEGACY
        assert detection.root == schema_plexus.project_dir
        assert detection.manifest is None

    def test_the_manifest_path_is_where_it_is_not_what_it_says(self, schema_plexus):
        _replace_manifest(schema_plexus, BROKEN_MANIFESTS["unparseable"])
        detection = detect(schema_plexus.root)
        assert detection.form is Form.SCHEMA
        assert detection.manifest == schema_plexus.root / MANIFEST_NAME


# ---------------------------------------------------------------------------
# Drift guard: detection is presence-only
# ---------------------------------------------------------------------------


class TestPresenceOnly:
    """Detection must never parse the manifest — that is the loader's job and
    the whole of D20's "a broken manifest does not demote the tree"."""

    def _tree(self):
        return ast.parse(inspect.getsource(form_module))

    def test_no_yaml_parser_is_imported(self):
        imported = {
            node.module
            for node in ast.walk(self._tree())
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            alias.name
            for node in ast.walk(self._tree())
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not any(name.split(".")[0] == "yaml" for name in imported), (
            "form.py parses nothing; a broken manifest is still schema form"
        )

    def test_the_loader_is_not_called(self):
        called = {
            node.func.id
            for node in ast.walk(self._tree())
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert {"load_manifest", "parse_manifest"} & called == set()

    def test_the_manifest_name_comes_from_the_loader(self):
        # One spelling of ``_plexus.yaml`` in the package, not a second
        # literal that can drift.
        assert form_module.manifest_path is manifest_module.manifest_path
        assert "\"_plexus.yaml\"" not in inspect.getsource(form_module)
        assert "'_plexus.yaml'" not in inspect.getsource(form_module)


def test_module_exports_the_documented_surface():
    assert sorted(form_module.__all__) == [
        "Detection",
        "Form",
        "detect",
        "detect_form",
        "find_plexus_root",
        "has_manifest",
    ]
    for name in form_module.__all__:
        assert hasattr(form_module, name)


def test_paths_come_back_as_paths(schema_plexus):
    assert isinstance(detect_form(schema_plexus.root), Form)
    assert isinstance(find_plexus_root(str(schema_plexus.root)), Path)
    assert isinstance(detect(str(schema_plexus.root)).root, Path)
