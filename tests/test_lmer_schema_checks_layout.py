"""``lmer_schema.checks.layout`` — the tree-shape checks (spec §4/§6/§7,
D6/D9/D13; SCHEMA.md "Project resolution and host buckets", "Host buckets",
"Inventory"; issue #353).

This module is the one that compares the REGISTRY against what is on disk, so
these tests pin the four families of that comparison:

- registry ↔ directory consistency: an entry with no directory is a warning,
  a project-shaped top-level directory no entry claims is a warning
  ("unregistered project or dotless host bucket");
- host buckets (D9): recognised by the dot in the top-level name, listed once
  as INFO "unnamed, awaiting adoption", never a warning and never an error,
  and found by the §1 MARKER TEST at whatever depth they sit — the walk
  descends until the first project-shaped directory and every level above it
  is a namespace level. There is no depth rule anywhere (!254 thread 2), so a
  five- or six-component bucket resolves exactly like a shallow one. The walk
  itself is now shared (:mod:`lmer_schema.checks.walk`, !256 discussion 5)
  and its rules are pinned in that module's tests; what is pinned here is
  what this module REPORTS about what it finds;
- bucket ↔ registry collision (§4 finding 7): a registered remote's
  ``{host}/{project}/`` path still present as a REAL directory is an ERROR (a
  split tree — work would land in two places), the same path as a SYMLINK is
  the §9 compatibility link and is INFO. The host directory carries no port,
  so a ported remote and its unported spelling are ONE bucket path;
- the `_` reservation (D6) and archive mirror validity (§6's ONE archive
  home): an unknown `_`-prefixed name is a warning, an `_archive/` entry
  whose first component is not an area of that scope is an error, an archive
  home inside `_runs/` is a warning, and legacy `runs/archive/` is recognised
  and never required to move.

Both fixture trees are validated as a whole too: the schema tree's only
layout finding is its bucket listing and the legacy tree exits 0 (G3).
"""

import os
import shutil
from pathlib import Path

import pytest
import yaml

from lmer_schema.checks import CHECKS, Form, Report, Severity, Tier
from lmer_schema.checks import layout as lay
from lmer_schema.checks import walk
from lmer_schema.checks.layout import (
    ARCHIVE_DIR,
    BUCKET,
    INSTANCE_AREAS,
    LEGACY_HOME,
    PROJECT_AREAS,
    REGISTERED,
    UNREGISTERED,
    ProjectDir,
    check_layout,
    find_project_dirs,
)
from lmer_schema.registry import load_registry
from tests import plexus_fixtures as pf

HOST, PATH = pf.PRIMARY_REPO.split("/", 1)


def _run(tree, **kwargs):
    """Run the layout checks over a built fixture tree."""
    report = Report(tree.form)
    projects = check_layout(tree.root, report, **kwargs)
    return report, projects


def _codes(report):
    return [finding.code for finding in report.findings]


def _by_code(report, code):
    return [finding for finding in report.findings if finding.code == code]


def _one(report, code):
    matches = _by_code(report, code)
    assert len(matches) == 1, f"expected exactly one {code}, got {_codes(report)}"
    return matches[0]


def _register(tree, name, entry):
    """Add a registry entry to this test's OWN fixture manifest."""
    doc = yaml.safe_load(tree.manifest.read_text(encoding="utf-8"))
    doc["projects"][name] = entry
    tree.manifest.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _project_shaped(path, marker="_runs"):
    path.mkdir(parents=True, exist_ok=True)
    (path / marker).mkdir(exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# The codes this module owns
# ---------------------------------------------------------------------------


class TestCodes:
    #: code -> (schema severity, legacy severity, tier)
    EXPECTED = {
        "tree.entry-without-dir": (Severity.WARN, Severity.WARN, Tier.MACHINE),
        "tree.unregistered-project": (Severity.WARN, Severity.WARN, Tier.MACHINE),
        "tree.host-bucket": (Severity.INFO, Severity.INFO, Tier.NOT_SCHEMA),
        "tree.legacy-project-home": (Severity.INFO, Severity.INFO, Tier.LEGACY),
        "tree.bucket-collision": (Severity.ERROR, Severity.ERROR, Tier.NOT_SCHEMA),
        "tree.bucket-compat-link": (Severity.INFO, Severity.INFO, Tier.NOT_SCHEMA),
        "tree.unknown-reserved-name": (Severity.WARN, Severity.WARN, Tier.MACHINE),
        "tree.archive-mirror-path": (Severity.ERROR, Severity.ERROR, Tier.MACHINE),
        "tree.archive-inside-runs": (Severity.WARN, Severity.WARN, Tier.MACHINE),
        "tree.legacy-archive": (Severity.INFO, Severity.INFO, Tier.LEGACY),
    }

    @pytest.mark.parametrize("code", sorted(EXPECTED))
    def test_registered_with_its_severity_and_tier(self, code):
        schema, legacy, tier = self.EXPECTED[code]
        entry = CHECKS[code]
        assert entry.severity is schema
        assert entry.legacy is legacy
        assert entry.tier is tier

    def test_the_module_owns_the_whole_tree_namespace(self):
        registered = {entry.code for entry in CHECKS if entry.namespace == "tree"}
        assert registered == set(self.EXPECTED)

    def test_a_bucket_never_rises_above_info(self):
        # D9 / SCHEMA.md "Host buckets": the validator never raises a bucket
        # above INFO, and it never blocks.
        for code in ("tree.host-bucket", "tree.legacy-project-home"):
            assert CHECKS[code].severity is Severity.INFO


def test_the_walk_has_no_depth_rule():
    # SCHEMA.md Concepts and Host buckets: tools MUST use the marker test,
    # "not path depth". A depth constant is the shape of the rule this module
    # must NOT have (!254 thread 2).
    source = Path(lay.__file__).read_text(encoding="utf-8")
    assert "MAX_DEPTH" not in source
    assert not any(name.endswith("DEPTH") for name in dir(lay))


# ---------------------------------------------------------------------------
# The fixture trees as a whole
# ---------------------------------------------------------------------------


class TestFixtureTrees:
    def test_schema_tree_only_finding_is_its_bucket(self, schema_plexus):
        report, _projects = _run(schema_plexus)
        assert _codes(report) == ["tree.host-bucket"]
        assert report.exit_code == 0

    def test_legacy_tree_exits_clean(self, legacy_plexus):
        # G3: the live legacy tree validates unmodified.
        report, _projects = _run(legacy_plexus)
        assert report.errors == ()
        assert report.warnings == ()
        assert report.exit_code == 0

    def test_legacy_project_home_is_recognised_not_bucketed(self, legacy_plexus):
        # SCHEMA.md "Host buckets": in a legacy tree the same path is not a
        # bucket at all but the project's only home.
        report, projects = _run(legacy_plexus)
        finding = _one(report, "tree.legacy-project-home")
        assert finding.path == f"{HOST}/{PATH}"
        assert _by_code(report, "tree.host-bucket") == []
        assert [p.kind for p in projects] == [LEGACY_HOME]

    def test_schema_tree_project_dirs_are_returned_for_the_runner(self, schema_plexus):
        _report, projects = _run(schema_plexus)
        by_path = {p.rel: p for p in projects}
        assert set(by_path) == {
            pf.PROJECT,
            pf.REPOLESS_PROJECT,
            f"{pf.BUCKET_HOST}/{pf.BUCKET_PROJECT}",
        }
        assert by_path[pf.PROJECT].kind == REGISTERED
        assert by_path[pf.PROJECT].name == pf.PROJECT
        assert by_path[pf.PROJECT].form is Form.SCHEMA
        bucket = by_path[f"{pf.BUCKET_HOST}/{pf.BUCKET_PROJECT}"]
        assert bucket.kind == BUCKET
        assert bucket.name is None
        # Bucket contents are checked at the legacy tier's severities.
        assert bucket.form is Form.LEGACY

    def test_project_dir_is_os_pathlike_for_the_sibling_modules(self, schema_plexus):
        # The sibling check modules take a caller-supplied project list and
        # do `Path(entry)` on it; the runner walks the tree once, here.
        _report, projects = _run(schema_plexus)
        assert [Path(p) for p in projects] == [p.path for p in projects]
        assert os.fspath(projects[0]) == str(projects[0].path)

    def test_project_dir_is_a_frozen_value(self, schema_plexus):
        _report, projects = _run(schema_plexus)
        assert isinstance(projects[0], ProjectDir)
        with pytest.raises((AttributeError, TypeError)):
            projects[0].kind = BUCKET


# ---------------------------------------------------------------------------
# Registry <-> directory consistency
# ---------------------------------------------------------------------------


class TestRegistryToDir:
    def test_entry_without_a_directory(self, schema_plexus):
        shutil.rmtree(schema_plexus.repoless_dir)
        report, projects = _run(schema_plexus)
        finding = _one(report, "tree.entry-without-dir")
        assert finding.path == pf.REPOLESS_PROJECT
        assert finding.severity is Severity.WARN
        assert pf.REPOLESS_PROJECT not in {p.name for p in projects}

    def test_entry_whose_name_is_a_file(self, schema_plexus):
        shutil.rmtree(schema_plexus.repoless_dir)
        schema_plexus.repoless_dir.write_text("not a directory\n", encoding="utf-8")
        report, _projects = _run(schema_plexus)
        finding = _one(report, "tree.entry-without-dir")
        assert "file" in (finding.detail or "").lower()

    def test_a_registered_dir_is_never_flagged_unregistered(self, schema_plexus):
        report, _projects = _run(schema_plexus)
        assert _by_code(report, "tree.unregistered-project") == []

    def test_unregistered_project_shaped_dir(self, schema_plexus):
        _project_shaped(schema_plexus.root / "widgets")
        report, projects = _run(schema_plexus)
        finding = _one(report, "tree.unregistered-project")
        assert finding.path == "widgets"
        assert finding.severity is Severity.WARN
        assert [p.kind for p in projects if p.rel == "widgets"] == [UNREGISTERED]

    def test_unregistered_dotless_host_bucket(self, schema_plexus):
        # A dotless host is not supported as a bucket at v1 (§1): the same
        # warning, naming the project-shaped dir found below it.
        _project_shaped(schema_plexus.root / "localhost" / "acme" / "widgets")
        report, _projects = _run(schema_plexus)
        finding = _one(report, "tree.unregistered-project")
        assert finding.path == "localhost"
        assert "localhost/acme/widgets" in (finding.detail or "")
        assert "dotless" in finding.message

    def test_a_dir_that_is_not_project_shaped_is_content(self, schema_plexus):
        (schema_plexus.root / "notes").mkdir()
        (schema_plexus.root / "notes" / "thought.md").write_text("hi\n", encoding="utf-8")
        report, _projects = _run(schema_plexus)
        assert _by_code(report, "tree.unregistered-project") == []

    def test_a_top_level_file_is_never_a_project(self, schema_plexus):
        (schema_plexus.root / "CONTRIBUTING.md").write_text("hi\n", encoding="utf-8")
        report, _projects = _run(schema_plexus)
        assert _codes(report) == ["tree.host-bucket"]

    def test_bare_legacy_areas_in_a_schema_tree_are_not_unregistered(self, schema_plexus):
        # SCHEMA.md "Legacy form": a schema-form tree may still hold bare
        # names; they are recognised elsewhere, never an unknown-name finding
        # here.
        _project_shaped(schema_plexus.root / "agent-files", marker="memory")
        report, _projects = _run(schema_plexus)
        assert _by_code(report, "tree.unregistered-project") == []

    def test_a_legacy_tree_has_nothing_to_be_unregistered_against(self, legacy_plexus):
        _project_shaped(legacy_plexus.root / "widgets")
        report, projects = _run(legacy_plexus)
        assert _by_code(report, "tree.unregistered-project") == []
        assert "widgets" in {p.rel for p in projects}


# ---------------------------------------------------------------------------
# Host buckets (D9)
# ---------------------------------------------------------------------------


class TestHostBuckets:
    def test_the_fixture_bucket_is_listed_once_as_info(self, schema_plexus):
        report, _projects = _run(schema_plexus)
        finding = _one(report, "tree.host-bucket")
        assert finding.path == f"{pf.BUCKET_HOST}/{pf.BUCKET_PROJECT}"
        assert finding.severity is Severity.INFO
        assert "unnamed" in finding.message and "adoption" in finding.message

    @pytest.mark.parametrize(
        "deep",
        ["widgets", "acme/widgets", "acme/team/widgets", "a/b/c/d/e", "a/b/c/d/e/f"],
    )
    def test_found_at_any_depth(self, schema_plexus, deep):
        # !254 thread 2: the marker test decides, never depth — a five- or
        # six-component bucket is found exactly like a shallow one. The old
        # four-level cap lost the deep ones silently.
        parts = deep.split("/")
        _project_shaped(schema_plexus.root.joinpath("git.example.org", *parts))
        report, projects = _run(schema_plexus)
        rel = "git.example.org/" + deep
        assert rel in {f.path for f in _by_code(report, "tree.host-bucket")}
        assert rel in {p.rel for p in projects}

    def test_namespace_levels_are_not_listed(self, schema_plexus):
        _project_shaped(schema_plexus.root / "git.example.org" / "a" / "b" / "c")
        report, _projects = _run(schema_plexus)
        paths = {f.path for f in _by_code(report, "tree.host-bucket")}
        assert "git.example.org/a/b/c" in paths
        assert not {p for p in paths if p.startswith("git.example.org/a/b/c/")}
        assert "git.example.org/a" not in paths
        assert "git.example.org/a/b" not in paths

    def test_the_walk_stops_at_the_first_project_shaped_dir(self, schema_plexus):
        # A project dir may hold anything below it; nothing under it is a
        # second bucket.
        project = _project_shaped(schema_plexus.root / "git.example.org" / "acme" / "widgets")
        _project_shaped(project / "vendor" / "inner")
        report, _projects = _run(schema_plexus)
        paths = {f.path for f in _by_code(report, "tree.host-bucket")}
        assert "git.example.org/acme/widgets" in paths
        assert "git.example.org/acme/widgets/vendor/inner" not in paths

    def test_two_projects_under_one_host_are_two_buckets(self, schema_plexus):
        _project_shaped(schema_plexus.root / "git.example.org" / "acme" / "one")
        _project_shaped(schema_plexus.root / "git.example.org" / "acme" / "two")
        report, _projects = _run(schema_plexus)
        paths = {f.path for f in _by_code(report, "tree.host-bucket")}
        assert paths == {
            f"{pf.BUCKET_HOST}/{pf.BUCKET_PROJECT}",  # the fixture's own
            "git.example.org/acme/one",
            "git.example.org/acme/two",
        }

    def test_a_legacy_shaped_bucket_counts(self, schema_plexus):
        _project_shaped(schema_plexus.root / "git.example.org" / "acme" / "old", marker="runs")
        report, _projects = _run(schema_plexus)
        assert "git.example.org/acme/old" in {
            f.path for f in _by_code(report, "tree.host-bucket")
        }

    def test_a_dotted_dir_with_nothing_project_shaped_says_nothing(self, schema_plexus):
        (schema_plexus.root / "notes.example.org" / "drafts").mkdir(parents=True)
        report, _projects = _run(schema_plexus)
        assert _codes(report) == ["tree.host-bucket"]

    def test_a_bucket_never_blocks(self, schema_plexus):
        _project_shaped(schema_plexus.root / "git.example.org" / "a" / "b" / "c" / "d" / "e")
        report, _projects = _run(schema_plexus)
        assert report.errors == ()
        assert report.warnings == ()
        assert report.exit_code == 0

    def test_hidden_and_schema_names_are_never_walked(self, schema_plexus):
        _project_shaped(schema_plexus.root / ".git" / "modules" / "x")
        _project_shaped(schema_plexus.root / "_knowledge" / "org" / "topic")
        report, _projects = _run(schema_plexus)
        assert _codes(report) == ["tree.host-bucket"]


class TestTheSharedWalk:
    """The descent itself is :mod:`lmer_schema.checks.walk`'s, and
    ``tests/test_lmer_schema_checks_walk.py`` owns its rules (!256 discussion
    5). What belongs here is that this module reports what that walk finds."""

    def test_find_project_dirs_is_the_shared_walk_s(self):
        assert find_project_dirs is walk.find_project_dirs

    def test_project_dir_is_the_shared_value(self):
        assert ProjectDir is walk.ProjectDir

    def test_a_namespace_level_may_be_underscore_prefixed(self, schema_plexus):
        # Decision 1: `_` is the schema's namespace at the ROOT and inside a
        # project, not in the middle of a bucket path — those components come
        # from the remote. The old skip-at-every-level stopped listing this
        # bucket, and stopped every other module checking it.
        _project_shaped(schema_plexus.root / "git.example.org" / "_ns" / "widgets")
        report, projects = _run(schema_plexus)
        rel = "git.example.org/_ns/widgets"
        assert rel in {f.path for f in _by_code(report, "tree.host-bucket")}
        assert rel in {p.rel for p in projects}

    def test_a_dotted_top_level_dir_may_itself_be_the_project(self, schema_plexus):
        # Decision 2: the marker test comes first, so the descent never
        # starts and nothing below it is a second bucket.
        _project_shaped(schema_plexus.root / "example.org")
        _project_shaped(schema_plexus.root / "example.org" / "sub" / "proj")
        report, _projects = _run(schema_plexus)
        paths = {f.path for f in _by_code(report, "tree.host-bucket")}
        assert "example.org" in paths
        assert "example.org/sub/proj" not in paths

    def test_a_registered_dir_is_a_project_before_it_has_any_areas(self, schema_plexus):
        # Decision 3: a registered project's directory is in scope whether or
        # not it has grown areas yet — its entry is its identity (§1).
        shutil.rmtree(schema_plexus.repoless_dir)
        schema_plexus.repoless_dir.mkdir()
        report, projects = _run(schema_plexus)
        assert _by_code(report, "tree.entry-without-dir") == []
        assert pf.REPOLESS_PROJECT in {p.rel for p in projects}


# ---------------------------------------------------------------------------
# Bucket <-> registry collision (§4 finding 7)
# ---------------------------------------------------------------------------


class TestBucketCollision:
    def test_clean_tree_has_no_collision(self, schema_plexus):
        report, _projects = _run(schema_plexus)
        assert _by_code(report, "tree.bucket-collision") == []
        assert _by_code(report, "tree.bucket-compat-link") == []

    def test_real_directory_at_a_registered_remote_path_is_an_error(self, schema_plexus):
        _project_shaped(schema_plexus.root / HOST / PATH)
        report, _projects = _run(schema_plexus)
        finding = _one(report, "tree.bucket-collision")
        assert finding.path == f"{HOST}/{PATH}"
        assert finding.severity is Severity.ERROR
        assert pf.PROJECT in finding.message
        assert report.exit_code == 1

    def test_the_split_tree_is_not_also_listed_as_an_unnamed_bucket(self, schema_plexus):
        # It is named — that is exactly why it is an error.
        _project_shaped(schema_plexus.root / HOST / PATH)
        report, _projects = _run(schema_plexus)
        assert f"{HOST}/{PATH}" not in {f.path for f in _by_code(report, "tree.host-bucket")}

    def test_the_split_tree_is_still_handed_to_the_other_modules(self, schema_plexus):
        # !256 discussion 5, decision 3: the `claimed` suppression is this
        # module's own REPORTING rule. The directory stays on the shared
        # walk's list — on a split tree it is the one most worth checking.
        _project_shaped(schema_plexus.root / HOST / PATH)
        _report, projects = _run(schema_plexus)
        split = [p for p in projects if p.rel == f"{HOST}/{PATH}"]
        assert [p.form for p in split] == [Form.LEGACY]

    def test_symlink_at_the_same_path_is_the_compat_link(self, schema_plexus):
        old = schema_plexus.root / HOST / PATH
        old.parent.mkdir(parents=True, exist_ok=True)
        old.symlink_to(os.path.relpath(schema_plexus.project_dir, old.parent))
        report, _projects = _run(schema_plexus)
        finding = _one(report, "tree.bucket-compat-link")
        assert finding.path == f"{HOST}/{PATH}"
        assert finding.severity is Severity.INFO
        assert _by_code(report, "tree.bucket-collision") == []
        assert report.exit_code == 0

    def test_an_additional_remote_collides_too(self, schema_plexus):
        host, path = pf.REMOTES[0].split("/", 1)
        _project_shaped(schema_plexus.root / host / path)
        report, _projects = _run(schema_plexus)
        assert _one(report, "tree.bucket-collision").path == f"{host}/{path}"

    def test_a_ported_spelling_is_the_same_bucket_reported_once(self, schema_plexus):
        # !254 thread 1: the port lives in the KEY only; both spellings name
        # one directory, so the split tree is one finding.
        _register(
            schema_plexus,
            "ported",
            {
                "repo": "git@git.example.org:acme/widgets",
                "remotes": ["ssh://git@git.example.org:2222/acme/widgets"],
            },
        )
        _project_shaped(schema_plexus.root / "git.example.org" / "acme" / "widgets")
        (schema_plexus.root / "ported").mkdir()
        report, _projects = _run(schema_plexus)
        finding = _one(report, "tree.bucket-collision")
        assert finding.path == "git.example.org/acme/widgets"

    def test_a_repoless_entry_has_no_bucket_path(self, schema_plexus):
        report, _projects = _run(schema_plexus)
        assert _by_code(report, "tree.bucket-collision") == []

    def test_a_legacy_tree_has_no_registry_to_collide_with(self, legacy_plexus):
        report, _projects = _run(legacy_plexus)
        assert _by_code(report, "tree.bucket-collision") == []

    def test_an_explicit_registry_is_used_as_given(self, schema_plexus):
        _project_shaped(schema_plexus.root / HOST / PATH)
        registry = load_registry(schema_plexus.root)
        report = Report(schema_plexus.form)
        check_layout(schema_plexus.root, report, registry=registry)
        assert _by_code(report, "tree.bucket-collision") != []

    def test_registry_none_forces_the_registry_free_checks(self, schema_plexus):
        _project_shaped(schema_plexus.root / HOST / PATH)
        report = Report(schema_plexus.form)
        check_layout(schema_plexus.root, report, registry=None)
        assert _by_code(report, "tree.bucket-collision") == []
        # With no registry the same path is just another unnamed bucket.
        assert f"{HOST}/{PATH}" in {f.path for f in _by_code(report, "tree.host-bucket")}


# ---------------------------------------------------------------------------
# The `_` reservation (D6)
# ---------------------------------------------------------------------------


class TestReservedNames:
    def test_known_instance_names_are_silent(self, schema_plexus):
        report, _projects = _run(schema_plexus)
        assert _by_code(report, "tree.unknown-reserved-name") == []

    def test_unknown_instance_name(self, schema_plexus):
        (schema_plexus.root / "_frobnicate").mkdir()
        report, _projects = _run(schema_plexus)
        finding = _one(report, "tree.unknown-reserved-name")
        assert finding.path == "_frobnicate"
        assert finding.severity is Severity.WARN

    def test_unknown_reserved_file(self, schema_plexus):
        (schema_plexus.root / "_notes.md").write_text("hi\n", encoding="utf-8")
        report, _projects = _run(schema_plexus)
        assert _one(report, "tree.unknown-reserved-name").path == "_notes.md"

    def test_a_project_level_area_is_not_an_instance_level_one(self, schema_plexus):
        (schema_plexus.root / "_runs").mkdir()
        report, _projects = _run(schema_plexus)
        assert _one(report, "tree.unknown-reserved-name").path == "_runs"

    def test_unknown_name_inside_a_project(self, schema_plexus):
        (schema_plexus.project_dir / "_scratch").mkdir()
        report, _projects = _run(schema_plexus)
        assert _one(report, "tree.unknown-reserved-name").path == f"{pf.PROJECT}/_scratch"

    def test_known_project_areas_are_silent(self, schema_plexus):
        for area in PROJECT_AREAS:
            (schema_plexus.project_dir / area).mkdir(exist_ok=True)
        (schema_plexus.project_dir / ARCHIVE_DIR).mkdir(exist_ok=True)
        report, _projects = _run(schema_plexus)
        assert _by_code(report, "tree.unknown-reserved-name") == []

    def test_inside_a_bucket_too(self, schema_plexus):
        (schema_plexus.bucket_dir / "_scratch").mkdir()
        report, _projects = _run(schema_plexus)
        assert _one(report, "tree.unknown-reserved-name").path == (
            f"{pf.BUCKET_HOST}/{pf.BUCKET_PROJECT}/_scratch"
        )

    def test_content_names_are_never_checked(self, schema_plexus):
        (schema_plexus.project_dir / "scratch").mkdir()
        (schema_plexus.project_dir / "NOTES.md").write_text("hi\n", encoding="utf-8")
        report, _projects = _run(schema_plexus)
        assert _by_code(report, "tree.unknown-reserved-name") == []


# ---------------------------------------------------------------------------
# Archive mirror validity (§6, D13)
# ---------------------------------------------------------------------------


class TestArchiveMirror:
    def test_a_valid_instance_mirror(self, schema_plexus):
        target = schema_plexus.archive_dir / "_napkin" / pf.INSTANCE_NAME
        target.mkdir(parents=True)
        (target / "old-idea.md").write_text("decayed\n", encoding="utf-8")
        report, _projects = _run(schema_plexus)
        assert _by_code(report, "tree.archive-mirror-path") == []

    def test_every_instance_area_is_a_valid_mirror_path(self, schema_plexus):
        for area in INSTANCE_AREAS:
            (schema_plexus.archive_dir / area).mkdir(parents=True)
        report, _projects = _run(schema_plexus)
        assert _by_code(report, "tree.archive-mirror-path") == []

    def test_a_project_area_is_not_an_instance_mirror_path(self, schema_plexus):
        (schema_plexus.archive_dir / "_runs" / pf.SLUG).mkdir(parents=True)
        report, _projects = _run(schema_plexus)
        finding = _one(report, "tree.archive-mirror-path")
        assert finding.path == f"{ARCHIVE_DIR}/_runs"
        assert finding.severity is Severity.ERROR

    def test_content_names_inside_the_archive_are_never_flagged(self, schema_plexus):
        (schema_plexus.archive_dir / pf.PROJECT).mkdir(parents=True)
        (schema_plexus.archive_dir / "README.md").write_text("hi\n", encoding="utf-8")
        report, _projects = _run(schema_plexus)
        assert _by_code(report, "tree.archive-mirror-path") == []

    def test_the_archived_session_home(self, schema_plexus):
        # §6 ONE archive home: <project>/_archive/_runs/<slug>/<session-id>/
        home = (
            schema_plexus.project_dir
            / ARCHIVE_DIR
            / "_runs"
            / pf.SLUG
            / pf.SESSION_IDS[0]
        )
        home.mkdir(parents=True)
        report, _projects = _run(schema_plexus)
        assert _by_code(report, "tree.archive-mirror-path") == []
        assert _by_code(report, "tree.unknown-reserved-name") == []

    def test_an_instance_area_is_not_a_project_mirror_path(self, schema_plexus):
        (schema_plexus.project_dir / ARCHIVE_DIR / "_napkin").mkdir(parents=True)
        report, _projects = _run(schema_plexus)
        assert _one(report, "tree.archive-mirror-path").path == (
            f"{pf.PROJECT}/{ARCHIVE_DIR}/_napkin"
        )

    def test_an_archive_home_inside_runs(self, schema_plexus):
        (schema_plexus.runs_dir / "archive").mkdir()
        report, _projects = _run(schema_plexus)
        finding = _one(report, "tree.archive-inside-runs")
        assert finding.path == f"{pf.PROJECT}/_runs/archive"
        assert finding.severity is Severity.WARN
        assert report.exit_code == 0

    def test_legacy_runs_archive_is_recognised_and_never_moved(self, legacy_plexus):
        (legacy_plexus.runs_dir / "archive" / pf.SLUG).mkdir(parents=True)
        report, _projects = _run(legacy_plexus)
        finding = _one(report, "tree.legacy-archive")
        assert finding.path == f"{HOST}/{PATH}/runs/archive"
        assert finding.severity is Severity.INFO
        assert report.exit_code == 0
