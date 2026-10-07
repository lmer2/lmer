"""``lmer_schema.writer`` — the one tool-side write path for ``_plexus.yaml``
(issue #366).

The writer is the importable API ``work adopt`` and ``work migrate`` share so
neither grows YAML dump code of its own. These tests pin its contract:

- ``load_for_write`` refuses (raises) a ``schema:`` newer than this lmer, an
  unparseable file, and a manifest whose registry already fails — the
  writer never makes a broken manifest worse — and refuses a MISSING file:
  ``create`` is the explicit bootstrap, never a side effect;
- every mutation (``add_project``, ``remove_project``, ``rename_project``,
  ``set_sources``, ``canonicalize_project``) leaves a file that re-parses
  and passes the validator's manifest + registry checks with zero ERRORs;
- REGISTRY remotes (``repo:``/``remotes:``) are written in canonical
  ``host/path`` form whatever spelling came in (scp, ssh, https with a
  credential, ``.git``, trailing slash — D21, the ONE normalizer);
  ``sources.<key>.repo`` is a TRANSPORT and is written VERBATIM, credential
  free, with ``ref`` untouched (astra PR1) — the writer only refuses an
  embedded credential or an empty ``repo``; trust rules stay the caller's;
- every pre-write invariant raises BEFORE disk is touched, through the
  registry's own rules (``build_registry`` on the mutated model), and a
  refused mutation leaves the handle usable;
- ``create`` writes a minimal ``schema: 1`` manifest and flips the tree's
  form (D20); writes are atomic (temp file + rename, mode preserved) and a
  simulated mid-write failure leaves no partial file;
- the comment-relocation rule the #365 spike measured
  (``tests/test_lmer_schema_manifest_roundtrip.py``) holds THROUGH this
  API on ``tests/fixtures/plexus_manifest/commented.yaml``, byte for byte,
  including the case the spike left open — an entry with no keys;
- two keys that share ONE node through a YAML anchor/alias (MR !380
  review: ``taskdef: &common {...}`` / ``napkin: *common``, where updating
  ``taskdef`` alone used to persist BOTH at the old value) are written out
  as independent mappings — the persisted, re-parsed file holds the
  requested two-source result, alias syntax dropped, every comment once;
  a merge key (``<<: *common``) is refused before any write; a document
  that shares nothing is not affected;
- ``import lmer_schema`` never loads ruamel (the writer imports it lazily),
  and the writer imports the relocation rule from nowhere — least of all
  from the test module that measured it.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from lmer_schema import (
    ManifestError,
    ManifestWriter,
    RegistryError,
    WriterError,
    create,
    load_for_write,
    load_manifest,
    load_registry,
    normalize,
    parse_manifest,
)
from lmer_schema.checks import Form, Report
from lmer_schema.checks.manifest import check_manifest
from lmer_schema.checks.registry import check_registry
from lmer_schema.manifest import CURRENT_SCHEMA, MANIFEST_NAME, SUPPORTED_SCHEMAS
from tests import plexus_fixtures as pf

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
WRITER_PATH = SRC_ROOT / "lmer_schema" / "writer.py"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "plexus_manifest" / "commented.yaml"

# Fake token value (glpat- shape, not a real secret).
TOKEN = "glpat-FAKEtoken123"

HOST, PATH = pf.PRIMARY_REPO.split("/", 1)

#: The entry the fixture round trips add — the spike's, so the expected
#: lines are the spike's too.
NEW_NAME = "gizmos"
NEW_ENTRY = dict(
    repo="git.example.com/acme/gizmos",
    remotes=("github.com/acme/gizmos",),
    description="Gizmos service",
)
NEW_ENTRY_LINES = [
    "  gizmos:\n",
    "    repo: git.example.com/acme/gizmos\n",
    "    remotes:\n",
    "      - github.com/acme/gizmos\n",
    "    description: Gizmos service\n",
]

#: The fixture's ``sources:`` as a mapping — ``set_sources`` of exactly this
#: is a no-op.
FIXTURE_SOURCES = {
    "taskdef": {"repo": "https://git.example.com/agents/taskdef.git", "ref": "main"},
    "napkin": {"repo": "https://git.example.com/agents/napkin.git"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(root, text):
    path = root / MANIFEST_NAME
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def schema_manifest(tmp_path):
    """The §4 schema-form fixture manifest, in its own tree."""
    return _write(tmp_path, pf.SCHEMA_MANIFEST)


@pytest.fixture
def commented(tmp_path):
    """A copy of the spike's commented fixture, in its own tree."""
    return _write(tmp_path, FIXTURE.read_text(encoding="utf-8"))


def _assert_clean(path):
    """The post-write invariant: the file re-parses and the validator's
    manifest + registry checks report zero ERRORs. Returns the manifest."""
    path = Path(path)
    manifest = parse_manifest(path.read_text(encoding="utf-8"), path=path)
    report = Report(Form.SCHEMA)
    loaded = check_manifest(path.parent, report)
    assert loaded is not None, [f.message for f in report.findings]
    check_registry(report, loaded)
    assert report.errors == (), [f.message for f in report.errors]
    assert report.unusable is None
    return manifest


def _line_index(lines, needle):
    hits = [i for i, line in enumerate(lines) if needle in line]
    assert len(hits) == 1, f"{needle!r} matched lines {hits}"
    return hits[0]


def _assert_text(expected, actual):
    import difflib

    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(True), actual.splitlines(True),
            "expected", "written", n=2,
        )
    )
    assert actual == expected, f"the written text differs from the intended one:\n{diff}"


def _only_manifest(root):
    """No temp file, no partial file: the manifest is the only entry."""
    return sorted(p.name for p in Path(root).iterdir()) == [MANIFEST_NAME]


@pytest.fixture(scope="module")
def tree():
    """The writer's source, parsed once for the import guards."""
    return ast.parse(WRITER_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# load_for_write — the gate
# ---------------------------------------------------------------------------


class TestLoadForWrite:
    def test_returns_the_handle_with_the_parsed_model(self, schema_manifest):
        writer = load_for_write(schema_manifest)
        assert isinstance(writer, ManifestWriter)
        assert writer.path == schema_manifest
        assert writer.manifest.name == pf.INSTANCE_NAME
        assert list(writer.manifest.projects) == [pf.PROJECT, pf.REPOLESS_PROJECT]
        assert writer.manifest.schema == 1

    @pytest.mark.parametrize(
        "value, fragment",
        [
            (str(CURRENT_SCHEMA + 1), "pgrade lmer"),
            ("true", "integer `schema:`"),
        ],
    )
    def test_refuses_a_newer_or_bool_schema_untouched(self, tmp_path, value, fragment):
        text = pf.SCHEMA_MANIFEST.replace("schema: 1", f"schema: {value}", 1)
        path = _write(tmp_path, text)
        with pytest.raises(ManifestError) as excinfo:
            load_for_write(path)
        assert fragment in str(excinfo.value)
        assert path.read_text(encoding="utf-8") == text

    def test_refuses_an_unparseable_file(self, tmp_path):
        path = _write(tmp_path, "schema: 1\nname: [unclosed\n")
        with pytest.raises(ManifestError) as excinfo:
            load_for_write(path)
        assert "unparseable" in str(excinfo.value)

    def test_refuses_a_manifest_whose_registry_already_fails(self, tmp_path):
        """Never make a broken manifest worse: one remote claimed by two
        entries does not even open for writing."""
        text = pf.SCHEMA_MANIFEST + f"  twin:\n    repo: {pf.PRIMARY_REPO}\n"
        text = text.replace("sources:", f"  twin:\n    repo: {pf.PRIMARY_REPO}\nsources:", 1)
        text = text.rsplit("  twin:\n", 1)[0]
        path = _write(tmp_path, text)
        with pytest.raises(RegistryError) as excinfo:
            load_for_write(path)
        assert "claimed by both" in str(excinfo.value)
        assert path.read_text(encoding="utf-8") == text

    def test_refuses_a_missing_file_and_names_create(self, tmp_path):
        """``create`` is the explicit bootstrap; a write API that silently
        invented a manifest would flip the tree's form as a side effect."""
        path = tmp_path / MANIFEST_NAME
        with pytest.raises(WriterError) as excinfo:
            load_for_write(path)
        assert "create" in str(excinfo.value)
        assert not path.exists()

    def test_refuses_a_symlinked_manifest(self, tmp_path, schema_manifest):
        """An atomic replace over a symlink would swap the link for a file
        and leave the target untouched — refused, like ``migrate``."""
        link = tmp_path / "linked" / MANIFEST_NAME
        link.parent.mkdir()
        link.symlink_to(schema_manifest)
        with pytest.raises(WriterError):
            load_for_write(link)

    def test_writer_error_is_a_manifest_error(self):
        assert issubclass(WriterError, ManifestError)


# ---------------------------------------------------------------------------
# add_project
# ---------------------------------------------------------------------------


class TestAddProject:
    def test_appends_a_canonical_entry_in_documented_key_order(self, schema_manifest):
        writer = load_for_write(schema_manifest)
        assert writer.add_project(
            "gizmos",
            repo="https://git.example.com/acme/gizmos.git",
            remotes=("git@github.com:acme/gizmos.git",),
            description="Gizmos service",
        ) is True
        manifest = _assert_clean(schema_manifest)
        assert list(manifest.projects) == [pf.PROJECT, pf.REPOLESS_PROJECT, "gizmos"]
        assert list(manifest.projects["gizmos"]) == ["repo", "remotes", "description"]
        assert manifest.projects["gizmos"] == {
            "repo": "git.example.com/acme/gizmos",
            "remotes": ["github.com/acme/gizmos"],
            "description": "Gizmos service",
        }
        # The handle's model is refreshed from what was written.
        assert writer.manifest == manifest
        assert load_registry(schema_manifest.parent).lookup(
            "ssh://git@github.com/acme/gizmos"
        ).name == "gizmos"

    @pytest.mark.parametrize(
        "spelling",
        [
            f"git@{HOST}:acme/gizmos.git",
            f"ssh://git@{HOST}/acme/gizmos.git",
            f"https://oauth2:{TOKEN}@{HOST}/acme/gizmos.git",
            f"https://{HOST}/acme/gizmos.git",
            f"https://{HOST}/acme/gizmos/",
            f"{HOST}/acme/gizmos/",
        ],
        ids=["scp", "ssh", "https-credential", "git-suffix", "trailing-slash", "bare-slash"],
    )
    def test_registry_remotes_are_written_canonical(self, schema_manifest, spelling):
        """Every spelling of one remote lands as ``host/path`` — in ``repo:``
        and inside ``remotes:`` alike — and the credential never reaches
        the file."""
        writer = load_for_write(schema_manifest)
        writer.add_project("gizmos", repo=spelling)
        writer.add_project(
            "mirror", repo="github.com/acme/mirror", remotes=(spelling.replace("gizmos", "mirror"),)
        )
        manifest = _assert_clean(schema_manifest)
        assert manifest.projects["gizmos"]["repo"] == f"{HOST}/acme/gizmos"
        assert manifest.projects["mirror"]["remotes"] == [f"{HOST}/acme/mirror"]
        text = schema_manifest.read_text(encoding="utf-8")
        assert TOKEN not in text
        assert spelling not in text or spelling == f"{HOST}/acme/gizmos"
        assert normalize(spelling) == f"{HOST}/acme/gizmos"

    def test_repoless_entry_is_description_only(self, schema_manifest):
        writer = load_for_write(schema_manifest)
        writer.add_project("ops-review", description="Weekly ops review")
        manifest = _assert_clean(schema_manifest)
        assert manifest.projects["ops-review"] == {"description": "Weekly ops review"}
        assert "remotes" not in manifest.projects["ops-review"]

    def test_entry_with_no_keys_is_written_as_an_empty_mapping(self, schema_manifest):
        """A D19 project registered by name alone: ``name: {}`` — the loader
        reads it as an empty entry; the validator WARNs (no description)
        but never ERRORs."""
        writer = load_for_write(schema_manifest)
        writer.add_project("alone")
        manifest = _assert_clean(schema_manifest)
        assert manifest.projects["alone"] == {}
        assert "  alone: {}\n" in schema_manifest.read_text(encoding="utf-8")

    def test_empty_remotes_writes_no_remotes_key(self, schema_manifest):
        """``remotes: []`` beside ``repo:`` is the validator's nudge; the
        writer never produces it."""
        writer = load_for_write(schema_manifest)
        writer.add_project("gizmos", repo=f"{HOST}/acme/gizmos", remotes=())
        manifest = _assert_clean(schema_manifest)
        assert manifest.projects["gizmos"] == {"repo": f"{HOST}/acme/gizmos"}

    def test_refuses_an_existing_name(self, schema_manifest):
        before = schema_manifest.read_bytes()
        writer = load_for_write(schema_manifest)
        with pytest.raises(WriterError) as excinfo:
            writer.add_project(pf.PROJECT, repo=f"{HOST}/acme/other")
        assert f"projects.{pf.PROJECT}" in str(excinfo.value)
        assert schema_manifest.read_bytes() == before

    def test_creates_the_projects_section_after_name(self, tmp_path):
        """A manifest with no ``projects:`` gets one, in the documented
        position (after ``name:``, before ``sources:``)."""
        path = _write(
            tmp_path,
            "schema: 1\nname: edge\nsources:\n  taskdef: {repo: https://h/t.git}\n",
        )
        writer = load_for_write(path)
        writer.add_project("gizmos", repo="h/gizmos")
        _assert_clean(path)
        assert path.read_text(encoding="utf-8") == (
            "schema: 1\nname: edge\nprojects:\n  gizmos:\n    repo: h/gizmos\n"
            "sources:\n  taskdef: {repo: https://h/t.git}\n"
        )

    def test_populates_a_null_projects_section_keeping_its_comment(self, tmp_path):
        path = _write(tmp_path, "schema: 1\nname: edge\nprojects:  # empty so far\n")
        writer = load_for_write(path)
        writer.add_project("gizmos", repo="h/gizmos")
        _assert_clean(path)
        assert path.read_text(encoding="utf-8") == (
            "schema: 1\nname: edge\nprojects:  # empty so far\n  gizmos:\n    repo: h/gizmos\n"
        )


#: Every pre-write invariant, each expressed as the ``add_project`` call that
#: would violate it on the §4 fixture (whose ``lmer`` entry owns
#: ``PRIMARY_REPO`` and ``REMOTES``). All raise through the registry's own
#: rules — the writer restates none of them.
INVARIANTS = {
    "short-name charset": dict(name="Gizmos", repo=f"{HOST}/acme/gizmos"),
    "dotted name is a host bucket": dict(name="git.example.com", repo=f"{HOST}/acme/gizmos"),
    "remotes without repo": dict(name="gizmos", remotes=(f"{HOST}/acme/gizmos",)),
    "repo repeated in remotes": dict(
        name="gizmos",
        repo=f"{HOST}/acme/gizmos",
        remotes=(f"git@{HOST}:acme/gizmos.git",),
    ),
    "two remotes folding to one key": dict(
        name="gizmos",
        repo=f"{HOST}/acme/gizmos",
        remotes=("github.com/acme/gizmos", "ssh://git@github.com/acme/gizmos.git"),
    ),
    "one remote bound to two projects": dict(
        name="gizmos", repo=f"https://{pf.PRIMARY_REPO}.git",
    ),
    "additional remote bound to two projects": dict(
        name="gizmos", repo=f"{HOST}/acme/gizmos", remotes=(pf.REMOTES[1],),
    ),
    "remote name is not a URL": dict(name="gizmos", repo="origin"),
    "empty repo": dict(name="gizmos", repo=""),
    "description not a string": dict(name="gizmos", description=42),
}


class TestPreWriteInvariants:
    @pytest.mark.parametrize("case", list(INVARIANTS), ids=list(INVARIANTS))
    def test_raises_before_disk_is_touched_and_handle_survives(self, schema_manifest, case):
        before = schema_manifest.read_bytes()
        writer = load_for_write(schema_manifest)
        with pytest.raises(ManifestError):
            writer.add_project(**INVARIANTS[case])
        assert schema_manifest.read_bytes() == before
        assert _only_manifest(schema_manifest.parent)
        assert "gizmos" not in writer.manifest.projects
        # The refused mutation rolled back: the handle is still usable and
        # writes exactly what a fresh one would.
        writer.add_project("gizmos", repo=f"{HOST}/acme/gizmos")
        manifest = _assert_clean(schema_manifest)
        assert list(manifest.projects) == [pf.PROJECT, pf.REPOLESS_PROJECT, "gizmos"]
        assert manifest.projects["gizmos"] == {"repo": f"{HOST}/acme/gizmos"}

    def test_rename_onto_a_bad_short_name_raises(self, schema_manifest):
        before = schema_manifest.read_bytes()
        writer = load_for_write(schema_manifest)
        with pytest.raises(ManifestError):
            writer.rename_project(pf.PROJECT, "Lmer")
        assert schema_manifest.read_bytes() == before


# ---------------------------------------------------------------------------
# remove_project / rename_project
# ---------------------------------------------------------------------------


class TestRemoveProject:
    def test_removes_the_entry_and_its_keys_from_the_union(self, schema_manifest):
        writer = load_for_write(schema_manifest)
        assert writer.remove_project(pf.PROJECT) is True
        manifest = _assert_clean(schema_manifest)
        assert list(manifest.projects) == [pf.REPOLESS_PROJECT]
        assert load_registry(schema_manifest.parent).lookup(pf.PRIMARY_REPO) is None
        assert pf.PROJECT not in writer.manifest.projects

    def test_removing_the_last_entry_leaves_an_empty_registry(self, schema_manifest):
        writer = load_for_write(schema_manifest)
        writer.remove_project(pf.PROJECT)
        writer.remove_project(pf.REPOLESS_PROJECT)
        manifest = _assert_clean(schema_manifest)
        assert manifest.projects == {}

    def test_refuses_an_unknown_name(self, schema_manifest):
        before = schema_manifest.read_bytes()
        writer = load_for_write(schema_manifest)
        with pytest.raises(WriterError) as excinfo:
            writer.remove_project("nope")
        assert "projects.nope" in str(excinfo.value)
        assert schema_manifest.read_bytes() == before


class TestRenameProject:
    def test_renames_in_place_keeping_order_and_identity(self, schema_manifest):
        writer = load_for_write(schema_manifest)
        assert writer.rename_project(pf.PROJECT, "lmer-core") is True
        manifest = _assert_clean(schema_manifest)
        assert list(manifest.projects) == ["lmer-core", pf.REPOLESS_PROJECT]
        assert manifest.projects["lmer-core"]["repo"] == pf.PRIMARY_REPO
        assert load_registry(schema_manifest.parent).lookup(pf.PRIMARY_REPO).name == "lmer-core"

    def test_keeps_the_entry_comments_at_their_columns(self, commented):
        """The key line's end-of-line comment stays at the column it had
        (the spike's column rule: a shorter key is padded out to it); the
        neighbours' comments do not move."""
        lines = commented.read_text(encoding="utf-8").splitlines(keepends=True)
        at = _line_index(lines, "  capacity-planning:")
        original = lines[at]
        column = original.index("#")
        lines[at] = "  capacity:".ljust(column) + original[column:]
        expected = "".join(lines)
        writer = load_for_write(commented)
        writer.rename_project("capacity-planning", "capacity")
        _assert_text(expected, commented.read_text(encoding="utf-8"))
        assert list(_assert_clean(commented).projects) == ["widgets", "gadgets", "capacity"]

    def test_refuses_an_unknown_old_name(self, schema_manifest):
        writer = load_for_write(schema_manifest)
        with pytest.raises(WriterError):
            writer.rename_project("nope", "gizmos")

    def test_refuses_an_existing_new_name(self, schema_manifest):
        before = schema_manifest.read_bytes()
        writer = load_for_write(schema_manifest)
        with pytest.raises(WriterError) as excinfo:
            writer.rename_project(pf.PROJECT, pf.REPOLESS_PROJECT)
        assert f"projects.{pf.REPOLESS_PROJECT}" in str(excinfo.value)
        assert schema_manifest.read_bytes() == before


# ---------------------------------------------------------------------------
# set_sources — transports, verbatim
# ---------------------------------------------------------------------------


class TestSetSources:
    TRANSPORTS = {
        "taskdef": {"repo": "https://git.example.test/g/tasks.git", "ref": "v1.2.3"},
        "napkin": {"repo": "ssh://git@host:2222/g/p.git"},
    }

    def test_stores_transport_spellings_and_ref_verbatim(self, schema_manifest):
        """A source ``repo`` is what the container clones; the registry's
        identity normalizer never touches it."""
        writer = load_for_write(schema_manifest)
        assert writer.set_sources(self.TRANSPORTS) is True
        manifest = _assert_clean(schema_manifest)
        assert manifest.sources == self.TRANSPORTS
        text = schema_manifest.read_text(encoding="utf-8")
        assert "https://git.example.test/g/tasks.git" in text
        assert "ssh://git@host:2222/g/p.git" in text
        assert "ref: v1.2.3" in text
        for entry in self.TRANSPORTS.values():
            # The canonical form is a different string, and it is NOT what
            # was written.
            assert normalize(entry["repo"]) != entry["repo"]
            assert f"repo: {normalize(entry['repo'])}" not in text

    def test_refuses_an_embedded_credential_without_echoing_it(self, schema_manifest):
        before = schema_manifest.read_bytes()
        writer = load_for_write(schema_manifest)
        with pytest.raises(WriterError) as excinfo:
            writer.set_sources(
                {"taskdef": {"repo": f"https://oauth2:{TOKEN}@git.example.test/g/tasks.git"}}
            )
        assert "credential" in str(excinfo.value)
        assert TOKEN not in str(excinfo.value)
        assert schema_manifest.read_bytes() == before
        assert writer.manifest.sources == parse_manifest(pf.SCHEMA_MANIFEST).sources

    @pytest.mark.parametrize(
        "mapping",
        [
            {"taskdef": {"repo": ""}},
            {"taskdef": {"repo": "   "}},
            {"taskdef": {"ref": "main"}},
            {"taskdef": "https://h/t.git"},
            {"taskdef": {"repo": 42}},
            {"": {"repo": "https://h/t.git"}},
        ],
        ids=["empty-repo", "blank-repo", "no-repo", "not-a-mapping", "repo-not-str", "empty-name"],
    )
    def test_refuses_a_malformed_entry(self, schema_manifest, mapping):
        before = schema_manifest.read_bytes()
        writer = load_for_write(schema_manifest)
        with pytest.raises(WriterError):
            writer.set_sources(mapping)
        assert schema_manifest.read_bytes() == before

    def test_same_mapping_is_a_no_op(self, commented):
        before = commented.read_bytes()
        writer = load_for_write(commented)
        assert writer.set_sources(FIXTURE_SOURCES) is False
        assert commented.read_bytes() == before

    def test_appends_a_new_source_below_the_last_and_keeps_the_section_gap(self, commented):
        lines = commented.read_text(encoding="utf-8").splitlines(keepends=True)
        at = _line_index(lines, "  napkin: {repo:") + 1
        expected = "".join(
            lines[:at]
            + ["  knowledge:\n", "    repo: https://git.example.com/agents/knowledge.git\n"]
            + lines[at:]
        )
        writer = load_for_write(commented)
        mapping = dict(FIXTURE_SOURCES)
        mapping["knowledge"] = {"repo": "https://git.example.com/agents/knowledge.git"}
        writer.set_sources(mapping)
        _assert_text(expected, commented.read_text(encoding="utf-8"))
        _assert_clean(commented)

    def test_removes_an_absent_source_with_its_own_comment(self, commented):
        lines = commented.read_text(encoding="utf-8").splitlines(keepends=True)
        start = _line_index(lines, "  # taskdef tracks main")
        stop = _line_index(lines, "    ref: main") + 1
        expected = "".join(lines[:start] + lines[stop:])
        writer = load_for_write(commented)
        writer.set_sources({"napkin": FIXTURE_SOURCES["napkin"]})
        _assert_text(expected, commented.read_text(encoding="utf-8"))
        assert list(_assert_clean(commented).sources) == ["napkin"]

    def test_updates_a_changed_scalar_in_place(self, commented):
        lines = commented.read_text(encoding="utf-8").splitlines(keepends=True)
        at = _line_index(lines, "    ref: main")
        lines[at] = "    ref: v2\n"
        expected = "".join(lines)
        writer = load_for_write(commented)
        mapping = {
            "taskdef": {"repo": FIXTURE_SOURCES["taskdef"]["repo"], "ref": "v2"},
            "napkin": FIXTURE_SOURCES["napkin"],
        }
        writer.set_sources(mapping)
        _assert_text(expected, commented.read_text(encoding="utf-8"))
        assert _assert_clean(commented).sources == mapping

    def test_drops_a_key_the_mapping_no_longer_has(self, commented):
        lines = commented.read_text(encoding="utf-8").splitlines(keepends=True)
        at = _line_index(lines, "    ref: main")
        expected = "".join(lines[:at] + lines[at + 1:])
        writer = load_for_write(commented)
        writer.set_sources(
            {"taskdef": {"repo": FIXTURE_SOURCES["taskdef"]["repo"]}, "napkin": FIXTURE_SOURCES["napkin"]}
        )
        _assert_text(expected, commented.read_text(encoding="utf-8"))
        _assert_clean(commented)

    def test_creates_the_section_between_projects_and_housekeeping(self, tmp_path):
        path = _write(
            tmp_path,
            "schema: 1\nname: edge\nprojects:\n  one:\n    repo: h/one\n"
            "housekeeping:\n  plans: {archive_after: null}\n",
        )
        writer = load_for_write(path)
        writer.set_sources({"taskdef": {"repo": "https://h/t.git", "ref": "main"}})
        _assert_clean(path)
        assert path.read_text(encoding="utf-8") == (
            "schema: 1\nname: edge\nprojects:\n  one:\n    repo: h/one\n"
            "sources:\n  taskdef:\n    repo: https://h/t.git\n    ref: main\n"
            "housekeeping:\n  plans: {archive_after: null}\n"
        )

    def test_empty_mapping_with_no_section_is_a_no_op(self, tmp_path):
        path = _write(tmp_path, "schema: 1\nname: edge\n")
        writer = load_for_write(path)
        assert writer.set_sources({}) is False
        assert path.read_text(encoding="utf-8") == "schema: 1\nname: edge\n"


# ---------------------------------------------------------------------------
# create — the explicit bootstrap
# ---------------------------------------------------------------------------


class TestCreate:
    def test_writes_a_minimal_schema_1_manifest_and_flips_the_form(self, tmp_path):
        path = tmp_path / MANIFEST_NAME
        assert load_manifest(tmp_path) is None, "legacy form before"
        writer = create(path, "acme")
        assert isinstance(writer, ManifestWriter)
        assert path.read_text(encoding="utf-8") == "schema: 1\nname: acme\n"
        manifest = _assert_clean(path)
        assert manifest.schema == 1 and manifest.schema in SUPPORTED_SCHEMAS
        assert manifest.name == "acme"
        assert manifest.projects == {} and manifest.sources == {}
        assert load_manifest(tmp_path) is not None, "schema form after (D20)"
        assert _only_manifest(tmp_path)

    def test_the_handle_continues_with_add_project(self, tmp_path):
        """Bootstrap then register — two explicit calls, one handle."""
        writer = create(tmp_path / MANIFEST_NAME, "acme")
        writer.add_project("lmer", repo=f"https://{pf.PRIMARY_REPO}.git")
        manifest = _assert_clean(tmp_path / MANIFEST_NAME)
        assert manifest.projects == {"lmer": {"repo": pf.PRIMARY_REPO}}
        assert (tmp_path / MANIFEST_NAME).read_text(encoding="utf-8") == (
            f"schema: 1\nname: acme\nprojects:\n  lmer:\n    repo: {pf.PRIMARY_REPO}\n"
        )

    def test_refuses_an_existing_manifest(self, schema_manifest):
        before = schema_manifest.read_bytes()
        with pytest.raises(WriterError) as excinfo:
            create(schema_manifest, "other")
        assert "load_for_write" in str(excinfo.value)
        assert schema_manifest.read_bytes() == before

    @pytest.mark.parametrize("name", ["", "   ", None, 7])
    def test_refuses_a_bad_instance_name(self, tmp_path, name):
        with pytest.raises(WriterError):
            create(tmp_path / MANIFEST_NAME, name)
        assert not (tmp_path / MANIFEST_NAME).exists()
        assert list(tmp_path.iterdir()) == []

    def test_a_quoted_name_still_reads_as_a_string(self, tmp_path):
        path = tmp_path / MANIFEST_NAME
        create(path, "123")
        assert _assert_clean(path).name == "123"


# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_mid_write_failure_leaves_the_original_and_no_partial_file(
        self, schema_manifest, monkeypatch
    ):
        from lmer_schema import writer as module

        before = schema_manifest.read_bytes()
        writer = load_for_write(schema_manifest)

        def explode(src, dst):
            raise OSError("simulated: disk full during rename")

        monkeypatch.setattr(module.os, "replace", explode)
        with pytest.raises(WriterError) as excinfo:
            writer.add_project("gizmos", repo=f"{HOST}/acme/gizmos")
        assert "simulated" in str(excinfo.value)
        assert schema_manifest.read_bytes() == before
        assert _only_manifest(schema_manifest.parent)
        # The handle rolled back too: nothing half-applied is remembered.
        assert "gizmos" not in writer.manifest.projects
        monkeypatch.undo()
        writer.add_project("gizmos", repo=f"{HOST}/acme/gizmos")
        assert "gizmos" in _assert_clean(schema_manifest).projects

    def test_mode_is_preserved(self, schema_manifest):
        schema_manifest.chmod(0o600)
        writer = load_for_write(schema_manifest)
        writer.add_project("gizmos", repo=f"{HOST}/acme/gizmos")
        assert schema_manifest.stat().st_mode & 0o777 == 0o600
        assert _only_manifest(schema_manifest.parent)

    def test_refuses_to_overwrite_a_manifest_changed_on_disk(self, schema_manifest):
        writer = load_for_write(schema_manifest)
        edited = pf.SCHEMA_MANIFEST.replace("name: example", "name: renamed", 1)
        schema_manifest.write_text(edited, encoding="utf-8")
        with pytest.raises(WriterError) as excinfo:
            writer.add_project("gizmos", repo=f"{HOST}/acme/gizmos")
        assert "changed" in str(excinfo.value)
        assert schema_manifest.read_text(encoding="utf-8") == edited


# ---------------------------------------------------------------------------
# The spike's round trips, through this API
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_add_project_appends_after_the_last_entry(self, commented):
        """The spike's insert: the new entry goes directly under
        ``capacity-planning``; the blank line before ``sources:`` stays
        where it was instead of moving above the new entry."""
        lines = commented.read_text(encoding="utf-8").splitlines(keepends=True)
        at = _line_index(lines, "    description:") + 1
        expected = "".join(lines[:at] + NEW_ENTRY_LINES + lines[at:])
        load_for_write(commented).add_project(NEW_NAME, **NEW_ENTRY)
        out = commented.read_text(encoding="utf-8")
        _assert_text(expected, out)
        assert yaml.safe_load(out)["projects"][NEW_NAME] == {
            "repo": NEW_ENTRY["repo"],
            "remotes": list(NEW_ENTRY["remotes"]),
            "description": NEW_ENTRY["description"],
        }
        _assert_clean(commented)

    @pytest.mark.parametrize(
        "name, first, last",
        [
            ("widgets", "  widgets:", "  # quoted on purpose"),
            ("gadgets", "  # gadgets moved", "    repo: github.com/acme/gadgets"),
            ("capacity-planning", "  capacity-planning:", "    description:"),
        ],
    )
    def test_remove_project_takes_only_its_own_lines(self, commented, name, first, last):
        """The spike's remove: the first entry leaves ``gadgets``' comment
        with ``gadgets``; an entry's own comment above it goes with it; the
        last entry leaves the blank line before ``sources:``."""
        lines = commented.read_text(encoding="utf-8").splitlines(keepends=True)
        start = _line_index(lines, first)
        stop = _line_index(lines, last) + 1
        expected = "".join(lines[:start] + lines[stop:])
        load_for_write(commented).remove_project(name)
        out = commented.read_text(encoding="utf-8")
        _assert_text(expected, out)
        assert name not in yaml.safe_load(out)["projects"]
        _assert_clean(commented)

    def test_canonicalize_project_keeps_the_trailing_comment_column(self, commented):
        """The spike's canonicalize: one scalar changes, its end-of-line
        comment stays at the column it had; the already-canonical,
        quoted-on-purpose remote is untouched."""
        lines = commented.read_text(encoding="utf-8").splitlines(keepends=True)
        at = _line_index(lines, "    repo: https://git.example.com/acme/widgets.git")
        original = lines[at]
        column = original.index("#")
        value = "    repo: git.example.com/acme/widgets"
        lines[at] = value.ljust(column) + original[column:]
        expected = "".join(lines)
        writer = load_for_write(commented)
        assert writer.canonicalize_project("widgets") is True
        out = commented.read_text(encoding="utf-8")
        _assert_text(expected, out)
        assert "'gitlab.com/acme/widgets'  # quoted on purpose" in out
        assert _assert_clean(commented).projects["widgets"]["repo"] == "git.example.com/acme/widgets"
        # Already canonical now: a second call changes nothing and writes nothing.
        before = commented.read_bytes()
        assert writer.canonicalize_project("widgets") is False
        assert commented.read_bytes() == before

    def test_canonicalize_rewrites_additional_remotes_too(self, commented):
        lines = commented.read_text(encoding="utf-8").splitlines(keepends=True)
        at = _line_index(lines, "      - github.com/acme/widgets")
        lines[at] = "      - git@github.com:acme/widgets.git\n"
        commented.write_text("".join(lines), encoding="utf-8")
        load_for_write(commented).canonicalize_project("widgets")
        assert _assert_clean(commented).projects["widgets"]["remotes"] == [
            "github.com/acme/widgets", "gitlab.com/acme/widgets",
        ]

    def test_canonicalize_refuses_an_unknown_name(self, commented):
        with pytest.raises(WriterError):
            load_for_write(commented).canonicalize_project("nope")

    @pytest.mark.parametrize("schema", SUPPORTED_SCHEMAS)
    def test_schema_int_is_written_back_unchanged(self, tmp_path, schema):
        """A rewrite never bumps ``schema:`` — that is a migration's job."""
        text = FIXTURE.read_text(encoding="utf-8").replace("schema: 1", f"schema: {schema}", 1)
        path = _write(tmp_path, text)
        load_for_write(path).add_project(NEW_NAME, **NEW_ENTRY)
        out = path.read_text(encoding="utf-8")
        assert f"schema: {schema}  # an int, never a bool\n" in out
        assert type(yaml.safe_load(out)["schema"]) is int
        assert parse_manifest(out).schema == schema

    def test_add_then_remove_is_the_identity(self, commented):
        before = commented.read_bytes()
        writer = load_for_write(commented)
        writer.add_project(NEW_NAME, **NEW_ENTRY)
        writer.remove_project(NEW_NAME)
        assert commented.read_bytes() == before


#: The case the spike left open: an entry with no keys has no leaf of its own
#: to carry what follows it. The writer's rule: the entry's own key slot IS
#: its leaf — a comment that follows ``alone: {}`` hangs on ``alone``.
NO_KEYS_TEXT = (
    "schema: 1\n"
    "name: edge\n"
    "projects:\n"
    "  alone: {}\n"
    "\n"
    "  # gadgets moved here\n"
    "  gadgets:\n"
    "    repo: github.com/acme/gadgets\n"
    "\n"
    "# area mounts\n"
    "sources: {}\n"
)


class TestEntryWithNoKeys:
    def test_add_after_an_empty_last_entry_keeps_the_gap_below_the_new_one(self, tmp_path):
        text = NO_KEYS_TEXT.replace("\n  # gadgets moved here\n  gadgets:\n    repo: github.com/acme/gadgets\n", "")
        path = _write(tmp_path, text)
        load_for_write(path).add_project("two", repo="h/two")
        _assert_text(
            text.replace("  alone: {}\n", "  alone: {}\n  two:\n    repo: h/two\n"),
            path.read_text(encoding="utf-8"),
        )
        _assert_clean(path)

    def test_remove_the_empty_first_entry_leaves_its_neighbours_comment(self, tmp_path):
        path = _write(tmp_path, NO_KEYS_TEXT)
        load_for_write(path).remove_project("alone")
        _assert_text(NO_KEYS_TEXT.replace("  alone: {}\n", ""), path.read_text(encoding="utf-8"))
        _assert_clean(path)

    def test_remove_the_entry_after_an_empty_one_takes_only_its_own_comment(self, tmp_path):
        path = _write(tmp_path, NO_KEYS_TEXT)
        load_for_write(path).remove_project("gadgets")
        expected = NO_KEYS_TEXT.replace(
            "  # gadgets moved here\n  gadgets:\n    repo: github.com/acme/gadgets\n", ""
        )
        _assert_text(expected, path.read_text(encoding="utf-8"))
        _assert_clean(path)

    def test_add_an_empty_entry_to_the_fixture_then_remove_it(self, commented):
        """Through the API on the real fixture: ``alone: {}`` lands under
        the last entry with the ``sources:`` gap below it, and removing it
        restores the original bytes."""
        before = commented.read_bytes()
        lines = before.decode("utf-8").splitlines(keepends=True)
        at = _line_index(lines, "    description:") + 1
        expected = "".join(lines[:at] + ["  alone: {}\n"] + lines[at:])
        writer = load_for_write(commented)
        writer.add_project("alone")
        _assert_text(expected, commented.read_text(encoding="utf-8"))
        writer.add_project("after", repo="git.example.com/acme/after")
        _assert_text(
            expected.replace("  alone: {}\n", "  alone: {}\n  after:\n    repo: git.example.com/acme/after\n"),
            commented.read_text(encoding="utf-8"),
        )
        writer.remove_project("after")
        writer.remove_project("alone")
        assert commented.read_bytes() == before

    def test_null_entry_reads_as_empty_and_is_written_as_null(self, tmp_path):
        """A hand-written ``name:`` (no value) is the one spelling ruamel
        normalizes — to ``null``, which the loader reads as ``{}``."""
        path = _write(tmp_path, "schema: 1\nname: edge\nprojects:\n  bare:\n  # above two\n  two:\n    repo: h/two\n")
        load_for_write(path).remove_project("two")
        assert path.read_text(encoding="utf-8") == "schema: 1\nname: edge\nprojects:\n  bare: null\n"
        assert _assert_clean(path).projects == {"bare": {}}


# ---------------------------------------------------------------------------
# Anchors and aliases — one slot, one node (MR !380 review)
# ---------------------------------------------------------------------------

SHARED = "https://git.example.com/g/shared.git"
TASKS = "https://git.example.com/g/tasks.git"

#: The review's document: ``taskdef`` and ``napkin`` are ONE ruamel node.
ALIASED_FLOW = (
    "schema: 1\nname: edge\nprojects:\n  one:\n    repo: h/one\n"
    f"sources:\n  taskdef: &common {{repo: {SHARED}}}\n  napkin: *common\n"
)
#: The same sharing in block style, with a comment in every position ruamel
#: treats specially around an anchor or alias: on the anchored key's line,
#: after the shared scalar, above the alias, on the alias line, after it.
ALIASED_BLOCK = (
    "schema: 1\nname: edge\nprojects:\n  one:\n    repo: h/one\n"
    "sources:\n"
    "  taskdef: &common  # anchored\n"
    f"    repo: {SHARED}  # eol\n"
    "  # napkin shares it\n"
    "  napkin: *common  # alias eol\n"
    "\n"
    "# tail\n"
    "housekeeping:\n  plans: {archive_after: null}\n"
)
#: The review's request: ``taskdef`` moves, ``napkin`` stays.
TWO_SOURCES = {"taskdef": {"repo": TASKS}, "napkin": {"repo": SHARED}}
#: What the writer emits for each: independent mappings, alias syntax
#: dropped, each key line's comment back on its line at the column it had.
TWO_SOURCES_FLOW = (
    "schema: 1\nname: edge\nprojects:\n  one:\n    repo: h/one\n"
    f"sources:\n  taskdef: {{repo: {TASKS}}}\n  napkin: {{repo: {SHARED}}}\n"
)
TWO_SOURCES_BLOCK = (
    "schema: 1\nname: edge\nprojects:\n  one:\n    repo: h/one\n"
    "sources:\n"
    "  taskdef:          # anchored\n"
    f"    repo: {TASKS}   # eol\n"
    "  # napkin shares it\n"
    "  napkin:          # alias eol\n"
    f"    repo: {SHARED}\n"
    "\n"
    "# tail\n"
    "housekeeping:\n  plans: {archive_after: null}\n"
)


class TestAnchorsAndAliases:
    @pytest.mark.parametrize(
        "text, expected",
        [(ALIASED_FLOW, TWO_SOURCES_FLOW), (ALIASED_BLOCK, TWO_SOURCES_BLOCK)],
        ids=["flow", "block"],
    )
    def test_updating_one_of_two_aliased_sources_persists_both_values(
        self, tmp_path, text, expected
    ):
        """The review's case. Both keys referenced one mapping; updating it
        through ``taskdef`` and then "keeping" it through ``napkin`` undid
        the first update, and the call returned ``True`` with the PERSISTED
        file pointing both at ``shared.git``. Pinned on the re-parsed file,
        not the handle."""
        path = _write(tmp_path, text)
        writer = load_for_write(path)
        assert writer.set_sources(TWO_SOURCES) is True
        out = path.read_text(encoding="utf-8")
        persisted = parse_manifest(out, path=path)
        assert persisted.sources == TWO_SOURCES
        assert persisted.sources["taskdef"]["repo"] == TASKS
        assert persisted.sources["napkin"]["repo"] == SHARED
        assert yaml.safe_load(out)["sources"] == TWO_SOURCES
        assert writer.manifest.sources == TWO_SOURCES
        assert _assert_clean(path).sources == TWO_SOURCES
        assert "&common" not in out and "*common" not in out
        _assert_text(expected, out)

    def test_block_alias_document_prints_every_comment_once_in_order(self, tmp_path):
        """ruamel mirrors the anchored key's comment onto the alias key by
        identity and hangs what follows the alias line on the shared node;
        detached, nothing prints twice and nothing moves out of order."""
        path = _write(tmp_path, ALIASED_BLOCK)
        load_for_write(path).set_sources(TWO_SOURCES)
        out = path.read_text(encoding="utf-8")
        comments = ["# anchored", "# eol", "# napkin shares it", "# alias eol", "# tail"]
        for comment in comments:
            assert out.count(comment) == 1, out
        positions = [out.index(comment) for comment in comments]
        assert positions == sorted(positions), out
        _assert_clean(path)

    def test_aliased_entries_set_to_what_they_hold_write_nothing(self, tmp_path):
        """A no-op stays a no-op: the detach happens in memory on load, and
        a file nothing was written to keeps its alias syntax."""
        path = _write(tmp_path, ALIASED_FLOW)
        before = path.read_bytes()
        writer = load_for_write(path)
        assert writer.set_sources({"taskdef": {"repo": SHARED}, "napkin": {"repo": SHARED}}) is False
        assert path.read_bytes() == before
        assert b"*common" in before

    def test_load_gives_each_slot_its_own_node_and_leaves_a_shared_nothing_alone(self):
        """Unit view of the rule: after ``_load`` the two keys hold two equal
        but distinct nodes, and a document with no sharing — the spike's
        fixture — still round-trips byte for byte."""
        from lmer_schema import writer as module

        section = module._load(ALIASED_FLOW)["sources"]
        assert section["taskdef"] is not section["napkin"]
        assert section["taskdef"] == section["napkin"] == {"repo": SHARED}
        text = FIXTURE.read_text(encoding="utf-8")
        assert module._dump(module._load(text)) == text

    def test_an_anchored_key_line_comment_stays_on_its_line(self, tmp_path):
        """No alias at all: an anchor between the key and its comment made
        ruamel park the comment where the fold printed it on a line of its
        own. It is the key line's comment and stays there, column kept."""
        path = _write(
            tmp_path,
            "schema: 1\nname: edge\nsources:\n  taskdef: &common  # note\n"
            f"    repo: {SHARED}\n    ref: main\n",
        )
        load_for_write(path).set_sources({"taskdef": {"repo": SHARED, "ref": "v2"}})
        _assert_text(
            f"schema: 1\nname: edge\nsources:\n  taskdef:          # note\n"
            f"    repo: {SHARED}\n    ref: v2\n",
            path.read_text(encoding="utf-8"),
        )
        _assert_clean(path)

    @pytest.mark.parametrize(
        "text",
        [
            "projects:\n  one: &p {repo: https://git.example.com/acme/one.git}\n  two: *p\n",
            "projects:\n  one: {repo: h/one, remotes: &r [github.com/acme/one]}\n"
            "  two: {repo: h/two, remotes: *r}\n",
        ],
        ids=["entry", "remotes"],
    )
    def test_projects_sharing_a_node_with_a_remote_are_refused_before_the_writer_opens(
        self, tmp_path, text
    ):
        """The one in-place rewrite of a ``projects:`` entry is
        ``canonicalize_project``, and the only nodes it rewrites — ``repo``
        and ``remotes`` — are identities the registry lets ONE entry hold:
        shared, they are refused at ``load_for_write`` (never make a broken
        manifest worse), so no mutation can reach them."""
        text = "schema: 1\nname: edge\n" + text
        path = _write(tmp_path, text)
        with pytest.raises(RegistryError) as excinfo:
            load_for_write(path)
        assert "claimed by both" in str(excinfo.value)
        assert path.read_text(encoding="utf-8") == text

    def test_projects_sharing_a_description_are_written_out_independently(self, tmp_path):
        """What the registry DOES let two entries share — a description —
        is detached like any other node: a write that touches a neighbour
        emits two independent entries holding the same values, and the
        pair survives a rename."""
        path = _write(
            tmp_path,
            "schema: 1\nname: edge\nprojects:\n"
            "  ops: &weekly {description: Weekly review}\n"
            "  review: *weekly\n"
            "  one:\n    repo: https://git.example.com/acme/one.git\n",
        )
        writer = load_for_write(path)
        assert writer.canonicalize_project("one") is True
        manifest = _assert_clean(path)
        assert manifest.projects["ops"] == manifest.projects["review"] == {"description": "Weekly review"}
        assert manifest.projects["one"] == {"repo": "git.example.com/acme/one"}
        out = path.read_text(encoding="utf-8")
        assert "&weekly" not in out and "*weekly" not in out
        assert "  ops: {description: Weekly review}\n  review: {description: Weekly review}\n" in out
        assert writer.rename_project("review", "weekly") is True
        manifest = _assert_clean(path)
        assert list(manifest.projects) == ["ops", "weekly", "one"]
        assert manifest.projects["weekly"] == {"description": "Weekly review"}

    def test_set_sources_refuses_an_entry_assembled_with_a_merge_key(self, tmp_path):
        """``napkin: {<<: *common}`` is its own node whose ``repo`` READS
        THROUGH to ``taskdef``'s: updated key by key, a successful call left
        ``napkin`` reading ``taskdef``'s new value. Refused before any write,
        naming the entry; what the section already holds is still a no-op;
        the handle stays usable and the merge syntax survives a write that
        does not touch ``sources:``."""
        text = ALIASED_FLOW.replace("napkin: *common", "napkin: {<<: *common}")
        held = {"taskdef": {"repo": SHARED}, "napkin": {"repo": SHARED}}
        path = _write(tmp_path, text)
        writer = load_for_write(path)
        assert writer.manifest.sources == held
        assert writer.set_sources(held) is False
        with pytest.raises(WriterError) as excinfo:
            writer.set_sources(TWO_SOURCES)
        message = str(excinfo.value)
        assert "`sources.napkin`" in message and "merge key" in message
        assert "sources.taskdef" not in message
        assert path.read_text(encoding="utf-8") == text
        assert _only_manifest(tmp_path)
        assert writer.add_project("two", repo="h/two") is True
        out = path.read_text(encoding="utf-8")
        assert "napkin: {<<: *common}" in out
        assert _assert_clean(path).sources == held


# ---------------------------------------------------------------------------
# Imports: ruamel stays out of the read path; the rule is ported, not imported
# ---------------------------------------------------------------------------


class TestImports:
    def test_importing_the_package_and_the_api_loads_no_ruamel(self):
        """The verify line of the brief, in a fresh interpreter: the package
        root re-exports the writer eagerly, so the writer MUST import ruamel
        lazily or every ``work`` read path would pay for it."""
        script = (
            "import sys, lmer_schema\n"
            "from lmer_schema import load_for_write, create\n"
            "loaded = sorted(m for m in sys.modules if m.startswith('ruamel'))\n"
            "assert not loaded, loaded\n"
        )
        env = dict(os.environ, PYTHONPATH=str(SRC_ROOT))
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0, result.stderr

    def test_no_module_level_ruamel_import(self, tree):
        for node in tree.body:
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("ruamel") for a in node.names), ast.dump(node)
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("ruamel"), ast.dump(node)

    def test_never_imports_from_the_tests(self, tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("tests"), ast.dump(node)
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("tests") for a in node.names), ast.dump(node)

    def test_normalizes_through_the_schema_adapter_only(self, tree):
        """Registry values go through ``lmer_schema.urls.normalize`` (D21);
        the writer never reaches for ``normalize_repo_url`` itself (the
        docstring may NAME it; the code never imports or calls it)."""
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "normalize_repo_url"
            if isinstance(node, ast.Attribute):
                assert node.attr != "normalize_repo_url"
            if isinstance(node, ast.ImportFrom):
                assert not any(a.name == "normalize_repo_url" for a in node.names)
        imported = {
            (node.module, alias.name)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert ("lmer_schema.urls", "normalize") in imported
        assert ("lmer_schema.registry", "build_registry") in imported
