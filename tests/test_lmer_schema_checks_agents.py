"""``lmer_schema.checks.agents`` — the ``_agents/`` surface and memory at both
scopes (spec §3.4/§3.6, D14/D17/D18; issue #353).

One module owns the whole agent runtime surface because the surface owns the
memory scope: the reserved-name rule (``memory`` and ``mirrors`` are never
harness names) and the D17 routing rule (a memory's ``metadata.type`` selects
the scope it belongs in) are two halves of the same shape. These tests pin:

- the surface: known harness dirs are OPAQUE (never descended), an unknown
  first-level name is a WARN, and a reserved name used as anything but its
  reserved surface is an ERROR;
- mirror opacity — nothing inside ``<scope>/mirrors/`` is ever a finding, so
  a foreign writer's tree can hold anything, including things that would be
  findings anywhere else;
- memory frontmatter: ``name``, ``description`` and ``metadata.type`` in
  ``{user, feedback, project, reference}`` are required, EXTRA ``metadata``
  keys are never a finding (SCHEMA.md "memory");
- D17 routing: ``project`` in ``<project>/_agents/memory/``, every other type
  in the instance ``_agents/memory/`` — a misroute is a hygiene WARN in a
  schema-form tree and INFO under the legacy tier (D14: recall unions both
  scopes, so nothing is lost, and today's tree holds every type in the
  project dir);
- ``MEMORY.md`` consistency at each scope: an entry without a file is ERROR
  (WARN in a legacy tree), a file without an entry is WARN, a tombstoned line
  is never a finding;
- legacy ``agent-files/``, ``mirrors/`` and ``<project>/memory/`` are
  recognised-legacy INFO, read in place, never errors — in EITHER form;
- both fixture trees come out clean: the schema tree with no findings at all,
  the legacy tree with informational ones only (G3: the live repo exits 0
  unmodified).

Every negative test injects its defect into ITS OWN fresh fixture tree; the
shared builder is consumed read-only.
"""

import importlib
import shutil
from pathlib import Path

import pytest

from lmer_schema.checks import CHECKS, Form, Report, Severity, Tier
from lmer_schema.checks import agents as ag
from lmer_schema.checks import walk
from tests import plexus_fixtures as pf


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run(tree, *, form=None, **kwargs):
    """Check *tree* (a ``PlexusTree``) and return the filled report."""
    report = Report(tree.form if form is None else form)
    ag.check_agents(tree.root, report, **kwargs)
    return report


def _codes(report):
    return [finding.code for finding in report.findings]


def _only(report, code):
    """The single finding carrying *code* (asserts there is exactly one)."""
    matches = [finding for finding in report.findings if finding.code == code]
    assert len(matches) == 1, f"expected one {code}, got {_codes(report)}"
    return matches[0]


def _memory(kind, *, name="a-memory", description="a description", extra=""):
    """A memory file body with the frontmatter SCHEMA.md requires."""
    return (
        "---\n"
        f"name: {name}\n"
        f'description: "{description}"\n'
        "metadata:\n"
        "  node_type: memory\n"
        f"  type: {kind}\n"
        f"{extra}"
        "---\n"
        "\nBody prose; free-form.\n"
    )


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _findings_with(tree, code):
    return [finding for finding in _run(tree).findings if finding.code == code]


def _add_memory(scope_dir, filename, text, *, index=True):
    """Drop a memory file into *scope_dir*, indexing it unless told not to."""
    _write(scope_dir / filename, text)
    if index:
        index_path = scope_dir / ag.MEMORY_INDEX
        line = f"- [{filename[:-3]}]({filename}) — fixture memory.\n"
        with index_path.open("a", encoding="utf-8") as handle:
            handle.write(line)


# ---------------------------------------------------------------------------
# the code vocabulary
# ---------------------------------------------------------------------------


class TestCodes:
    def test_every_code_is_registered_in_the_agents_namespace(self):
        for code in ag.AGENT_CODES:
            assert code in CHECKS, f"{code} is not registered"
            assert CHECKS[code].namespace == "agents"

    def test_the_pre_registered_d14_codes_are_consumed_not_redefined(self):
        """findings.py owns the D14 table; this module must not re-register
        (a second registration raises) nor restate a severity."""
        entry = CHECKS[ag.MEMORY_ENTRY_WITHOUT_FILE]
        assert (entry.severity, entry.legacy) == (Severity.ERROR, Severity.WARN)
        mismatch = CHECKS[ag.MEMORY_SCOPE_MISMATCH]
        assert (mismatch.severity, mismatch.legacy) == (Severity.WARN, Severity.INFO)

    @pytest.mark.parametrize(
        "code, severity, tier",
        [
            ("agents.unknown-harness", Severity.WARN, Tier.MACHINE),
            ("agents.reserved-harness-name", Severity.ERROR, Tier.MACHINE),
            ("agents.memory-frontmatter", Severity.ERROR, Tier.MACHINE),
            ("agents.memory-type-unknown", Severity.ERROR, Tier.MACHINE),
            ("agents.memory-unknown-file", Severity.WARN, Tier.MACHINE),
            ("agents.memory-index-missing", Severity.WARN, Tier.MACHINE),
            ("agents.memory-file-without-entry", Severity.WARN, Tier.MACHINE),
            ("agents.legacy-surface", Severity.INFO, Tier.LEGACY),
        ],
    )
    def test_own_codes_carry_their_documented_severity(self, code, severity, tier):
        entry = CHECKS[code]
        assert entry.severity is severity
        assert entry.tier is tier

    def test_shape_codes_are_never_softened_by_the_legacy_tier(self):
        """D14 softens INTEGRITY findings only; a reserved name used as a
        harness and malformed frontmatter are shape violations."""
        for code in ("agents.reserved-harness-name", "agents.memory-frontmatter"):
            assert not CHECKS[code].tier_sensitive

    def test_importing_the_module_twice_is_not_a_double_registration(self):
        importlib.reload(ag)

    def test_memory_types_are_the_d17_vocabulary(self):
        assert ag.MEMORY_TYPES == ("user", "feedback", "project", "reference")

    @pytest.mark.parametrize("kind", ["user", "feedback", "reference"])
    def test_scope_for_type_routes_every_non_project_type_to_the_instance(self, kind):
        assert ag.scope_for_type(kind) == ag.INSTANCE_SCOPE

    def test_scope_for_type_routes_project_to_the_project_scope(self):
        assert ag.scope_for_type("project") == ag.PROJECT_SCOPE


# ---------------------------------------------------------------------------
# the fixture trees
# ---------------------------------------------------------------------------


class TestFixtureTrees:
    def test_schema_tree_is_clean(self, schema_plexus):
        report = _run(schema_plexus)
        assert report.findings == ()
        assert report.exit_code == 0

    def test_legacy_tree_has_no_errors_or_warnings(self, legacy_plexus):
        """G3: the live repo validates unmodified. Its bare surfaces are
        recognised-legacy INFO and its pre-D17 memory placement is INFO."""
        report = _run(legacy_plexus)
        assert report.errors == ()
        assert report.warnings == ()
        assert report.exit_code == 0

    def test_legacy_tree_lists_every_bare_surface(self, legacy_plexus):
        paths = {
            finding.path
            for finding in _findings_with(legacy_plexus, ag.LEGACY_SURFACE)
        }
        rel = legacy_plexus.project_dir.relative_to(legacy_plexus.root).as_posix()
        assert paths == {"agent-files", "mirrors", f"{rel}/memory"}

    def test_legacy_tree_reports_the_pre_d17_memory_as_informational(
        self, legacy_plexus
    ):
        """The legacy twin holds a `user` memory in the project dir — the
        exact misroute D17 makes structural, downgraded to INFO."""
        finding = _only(_run(legacy_plexus), ag.MEMORY_SCOPE_MISMATCH)
        assert finding.severity is Severity.INFO
        assert pf.USER_MEMORY_FILE in finding.path


# ---------------------------------------------------------------------------
# the surface: harness dirs, reserved names, opacity
# ---------------------------------------------------------------------------


class TestSurfaceShape:
    def test_unknown_first_level_dir_is_a_warning(self, schema_plexus):
        (schema_plexus.root / "_agents" / "cursor").mkdir()
        finding = _only(_run(schema_plexus), ag.UNKNOWN_HARNESS)
        assert finding.severity is Severity.WARN
        assert finding.path == "_agents/cursor"

    def test_unknown_first_level_file_is_a_warning(self, schema_plexus):
        _write(schema_plexus.root / "_agents" / "notes.md", "stray\n")
        assert _only(_run(schema_plexus), ag.UNKNOWN_HARNESS).path == "_agents/notes.md"

    def test_the_project_scope_has_the_same_shape(self, schema_plexus):
        (schema_plexus.project_dir / "_agents" / "cursor").mkdir()
        finding = _only(_run(schema_plexus), ag.UNKNOWN_HARNESS)
        assert finding.path.endswith("_agents/cursor")

    def test_a_dotfile_is_not_an_unknown_name(self, schema_plexus):
        _write(schema_plexus.root / "_agents" / ".gitkeep", "")
        assert _run(schema_plexus).findings == ()

    def test_the_known_harness_dir_is_opaque(self, schema_plexus):
        """`<scope>/claude/` is the harness's own layout — never walked."""
        junk = schema_plexus.agents_claude_dir / "skills" / "x" / "MEMORY.md"
        _write(junk, "not frontmatter at all\n")
        assert _run(schema_plexus).findings == ()

    @pytest.mark.parametrize("name", ["memory", "mirrors"])
    def test_a_reserved_name_that_is_not_a_directory_is_an_error(
        self, schema_plexus, name
    ):
        target = schema_plexus.root / "_agents" / name
        shutil.rmtree(target)
        _write(target, "not a directory\n")
        finding = _only(_run(schema_plexus), ag.RESERVED_HARNESS_NAME)
        assert finding.severity is Severity.ERROR
        assert finding.path == f"_agents/{name}"

    def test_memory_used_as_a_harness_dir_is_an_error(self, schema_plexus):
        """A harness named `memory` is an error (SCHEMA.md agent surface).
        On disk that reads as `<scope>/memory/` holding harness configuration
        and no memory at all."""
        scope = schema_plexus.instance_memory_dir
        for path in sorted(scope.iterdir()):
            path.unlink()
        _write(scope / "skills" / "hello" / "SKILL.md", "# hello\n")
        report = _run(schema_plexus)
        finding = _only(report, ag.RESERVED_HARNESS_NAME)
        assert finding.severity is Severity.ERROR
        assert finding.path == "_agents/memory"

    def test_a_reserved_name_error_stops_the_memory_walk(self, schema_plexus):
        scope = schema_plexus.instance_memory_dir
        for path in sorted(scope.iterdir()):
            path.unlink()
        _write(scope / "skills" / "hello" / "SKILL.md", "# hello\n")
        assert _codes(_run(schema_plexus)) == [ag.RESERVED_HARNESS_NAME]


class TestMirrorOpacity:
    """A mirror is a foreign writer's tree, checked no further than its
    existence (D12's rule, applied to `<scope>/mirrors/`)."""

    def test_nothing_inside_a_mirror_is_ever_a_finding(self, schema_plexus):
        mirror = schema_plexus.mirrors_dir / "masterplan"
        _write(mirror / "memory" / "broken.md", "no frontmatter here\n")
        _write(mirror / "memory" / "MEMORY.md", "- [gone](missing.md) — nope.\n")
        _write(mirror / "_agents" / "cursor" / "config.toml", "x = 1\n")
        assert _run(schema_plexus).findings == ()

    def test_a_project_scope_mirror_is_opaque_too(self, schema_plexus):
        mirror = schema_plexus.project_dir / "_agents" / "mirrors" / "vendored"
        _write(mirror / "MEMORY.md", "- [gone](missing.md) — nope.\n")
        assert _run(schema_plexus).findings == ()


# ---------------------------------------------------------------------------
# memory frontmatter
# ---------------------------------------------------------------------------


class TestMemoryFrontmatter:
    def test_the_fixture_memories_are_well_formed(self, schema_plexus):
        assert _run(schema_plexus).findings == ()

    def test_extra_metadata_keys_are_never_a_finding(self, schema_plexus):
        """SCHEMA.md: harness-written metadata keys are PERMITTED and are
        never a finding, not even a warning — the vocabulary may grow."""
        _add_memory(
            schema_plexus.instance_memory_dir,
            "user_extra.md",
            _memory(
                "user",
                extra="  originSessionId: 20260824-101500-1-11111\n"
                "  modified: '2026-08-24T10:15:00Z'\n",
            ),
        )
        assert _run(schema_plexus).findings == ()

    @pytest.mark.parametrize(
        "text",
        [
            "# just markdown\n\nno frontmatter at all\n",
            "---\nname: x\ndescription: y\n",  # unterminated fence
            "---\n- a list, not a mapping\n---\n\nbody\n",
            "---\nname: [unbalanced\n---\n\nbody\n",  # unparseable YAML
            "\n---\nname: x\n---\n",  # fence not on the first line
        ],
    )
    def test_missing_or_malformed_frontmatter_is_an_error(self, schema_plexus, text):
        _add_memory(schema_plexus.instance_memory_dir, "user_broken.md", text)
        finding = _only(_run(schema_plexus), ag.MEMORY_FRONTMATTER)
        assert finding.severity is Severity.ERROR
        assert finding.path.endswith("user_broken.md")

    @pytest.mark.parametrize(
        "text, missing",
        [
            ("---\ndescription: d\nmetadata:\n  type: user\n---\n", "name"),
            ("---\nname: n\nmetadata:\n  type: user\n---\n", "description"),
            ("---\nname: n\ndescription: d\n---\n", "metadata.type"),
            ("---\nname: n\ndescription: d\nmetadata: user\n---\n", "metadata"),
            ("---\nname: ''\ndescription: d\nmetadata:\n  type: user\n---\n", "name"),
        ],
    )
    def test_a_missing_required_key_is_an_error(self, schema_plexus, text, missing):
        _add_memory(schema_plexus.instance_memory_dir, "user_partial.md", text)
        finding = _only(_run(schema_plexus), ag.MEMORY_FRONTMATTER)
        assert missing in (finding.message + (finding.detail or ""))

    def test_a_type_outside_the_vocabulary_is_an_error(self, schema_plexus):
        _add_memory(
            schema_plexus.instance_memory_dir, "user_odd.md", _memory("cheese")
        )
        report = _run(schema_plexus)
        finding = _only(report, ag.MEMORY_TYPE_UNKNOWN)
        assert finding.severity is Severity.ERROR
        # An unknown type routes nowhere, so it is not also a scope mismatch.
        assert ag.MEMORY_SCOPE_MISMATCH not in _codes(report)

    def test_an_unreadable_memory_file_is_reported_not_raised(self, schema_plexus):
        scope = schema_plexus.instance_memory_dir
        (scope / "user_binary.md").write_bytes(b"\xff\xfe\x00not utf-8\n")
        with (scope / ag.MEMORY_INDEX).open("a", encoding="utf-8") as handle:
            handle.write("- [binary](user_binary.md) - fixture memory.\n")
        finding = _only(_run(schema_plexus), ag.MEMORY_FRONTMATTER)
        assert finding.severity is Severity.ERROR

    def test_parse_frontmatter_is_importable_and_raises_valueerror(self):
        assert ag.parse_frontmatter(_memory("user"))["name"] == "a-memory"
        with pytest.raises(ValueError):
            ag.parse_frontmatter("no frontmatter\n")

    def test_a_non_markdown_file_in_a_memory_dir_is_a_warning(self, schema_plexus):
        _write(schema_plexus.instance_memory_dir / "notes.txt", "scratch\n")
        finding = _only(_run(schema_plexus), ag.MEMORY_UNKNOWN_FILE)
        assert finding.severity is Severity.WARN


# ---------------------------------------------------------------------------
# D17 scope routing
# ---------------------------------------------------------------------------


class TestScopeRouting:
    @pytest.mark.parametrize("kind", ["user", "feedback", "reference"])
    def test_instance_types_belong_in_the_instance_scope(self, schema_plexus, kind):
        _add_memory(
            schema_plexus.instance_memory_dir, f"{kind}_fine.md", _memory(kind)
        )
        assert _run(schema_plexus).findings == ()

    @pytest.mark.parametrize("kind", ["user", "feedback", "reference"])
    def test_an_instance_type_in_the_project_scope_is_a_warning(
        self, schema_plexus, kind
    ):
        _add_memory(
            schema_plexus.project_memory_dir, f"{kind}_misrouted.md", _memory(kind)
        )
        finding = _only(_run(schema_plexus), ag.MEMORY_SCOPE_MISMATCH)
        assert finding.severity is Severity.WARN
        assert finding.path.endswith(f"{kind}_misrouted.md")

    def test_a_project_memory_in_the_instance_scope_is_a_warning(self, schema_plexus):
        _add_memory(
            schema_plexus.instance_memory_dir, "project_misrouted.md", _memory("project")
        )
        finding = _only(_run(schema_plexus), ag.MEMORY_SCOPE_MISMATCH)
        assert finding.severity is Severity.WARN

    def test_a_misroute_is_never_an_error(self, schema_plexus):
        """Recall is the union of both scopes, so a misplaced memory loses
        nothing — hygiene, never conformance."""
        _add_memory(
            schema_plexus.project_memory_dir, "user_misrouted.md", _memory("user")
        )
        assert _run(schema_plexus).exit_code == 0

    def test_host_bucket_contents_are_checked_at_the_legacy_tier(self, schema_plexus):
        """A bucket is outside the schema: its contents are checked at the
        legacy tier's severities even in a schema-form tree."""
        bucket_memory = schema_plexus.bucket_dir / "_agents" / "memory"
        _add_memory(bucket_memory, "user_misrouted.md", _memory("user"))
        finding = _only(_run(schema_plexus), ag.MEMORY_SCOPE_MISMATCH)
        assert finding.severity is Severity.INFO
        assert finding.path.startswith(f"{pf.BUCKET_HOST}/")


# ---------------------------------------------------------------------------
# MEMORY.md index consistency
# ---------------------------------------------------------------------------


class TestMemoryIndex:
    def test_an_entry_without_a_file_is_an_error_in_schema_form(self, schema_plexus):
        index = schema_plexus.instance_memory_dir / ag.MEMORY_INDEX
        with index.open("a", encoding="utf-8") as handle:
            handle.write("- [gone](user_gone.md) — forgotten by hand.\n")
        finding = _only(_run(schema_plexus), ag.MEMORY_ENTRY_WITHOUT_FILE)
        assert finding.severity is Severity.ERROR
        assert finding.path == "_agents/memory/MEMORY.md"
        assert "user_gone.md" in (finding.detail or "")

    def test_an_entry_without_a_file_is_a_warning_in_a_legacy_tree(
        self, legacy_plexus
    ):
        index = legacy_plexus.project_memory_dir / ag.MEMORY_INDEX
        with index.open("a", encoding="utf-8") as handle:
            handle.write("- [gone](user_gone.md) — forgotten by hand.\n")
        report = _run(legacy_plexus)
        assert _only(report, ag.MEMORY_ENTRY_WITHOUT_FILE).severity is Severity.WARN
        assert report.exit_code == 0

    def test_a_file_without_an_entry_is_a_warning(self, schema_plexus):
        _add_memory(
            schema_plexus.instance_memory_dir,
            "user_unindexed.md",
            _memory("user"),
            index=False,
        )
        finding = _only(_run(schema_plexus), ag.MEMORY_FILE_WITHOUT_ENTRY)
        assert finding.severity is Severity.WARN
        assert finding.path.endswith("user_unindexed.md")

    @pytest.mark.parametrize(
        "line",
        [
            "# Memory\n",
            "Prose about the index.\n",
            "- user-gone — forgotten 2026-08-24.\n",
            "- [gone](archived 2026-08-24) — tombstoned.\n",
            "- [gone](../_archive/_agents/memory/user_gone.md) — archived.\n",
            "- [upstream](https://example.com/doc.md) — an external link.\n",
        ],
    )
    def test_a_line_without_a_resolvable_entry_link_is_never_a_finding(
        self, schema_plexus, line
    ):
        index = schema_plexus.instance_memory_dir / ag.MEMORY_INDEX
        with index.open("a", encoding="utf-8") as handle:
            handle.write(line)
        assert _run(schema_plexus).findings == ()

    def test_a_dot_slash_link_resolves(self, schema_plexus):
        scope = schema_plexus.instance_memory_dir
        _write(scope / "user_dotted.md", _memory("user"))
        with (scope / ag.MEMORY_INDEX).open("a", encoding="utf-8") as handle:
            handle.write("- [dotted](./user_dotted.md) — fixture memory.\n")
        assert _run(schema_plexus).findings == ()

    def test_a_missing_index_beside_memory_files_is_one_warning(self, schema_plexus):
        (schema_plexus.instance_memory_dir / ag.MEMORY_INDEX).unlink()
        report = _run(schema_plexus)
        finding = _only(report, ag.MEMORY_INDEX_MISSING)
        assert finding.severity is Severity.WARN
        assert finding.path == "_agents/memory"
        # One finding about the index, not one per orphaned file.
        assert ag.MEMORY_FILE_WITHOUT_ENTRY not in _codes(report)

    def test_an_empty_memory_dir_needs_no_index(self, schema_plexus):
        scope = schema_plexus.instance_memory_dir
        for path in sorted(scope.iterdir()):
            path.unlink()
        assert _run(schema_plexus).findings == ()


# ---------------------------------------------------------------------------
# legacy surfaces
# ---------------------------------------------------------------------------


class TestLegacySurfaces:
    def test_bare_instance_surfaces_are_informational_in_a_schema_tree(
        self, schema_plexus
    ):
        """§9 step 3: a tree that has adopted the schema may still hold bare
        `agent-files/` and `mirrors/` until the instance surfaces migrate."""
        _write(schema_plexus.root / "agent-files" / "claude" / "skills" / "s.md", "x\n")
        _write(schema_plexus.root / "mirrors" / "masterplan" / "MIRROR.md", "x\n")
        report = _run(schema_plexus)
        assert {finding.path for finding in report.findings} == {
            "agent-files",
            "mirrors",
        }
        assert set(_codes(report)) == {ag.LEGACY_SURFACE}
        assert report.exit_code == 0

    def test_a_legacy_surface_is_read_in_place_never_walked(self, schema_plexus):
        _write(schema_plexus.root / "agent-files" / "claude" / "MEMORY.md",
               "- [gone](missing.md) — nope.\n")
        assert _codes(_run(schema_plexus)) == [ag.LEGACY_SURFACE]

    def test_a_bare_project_memory_dir_is_informational_and_still_checked(
        self, schema_plexus
    ):
        """The mixed case SCHEMA.md names: `<project>/memory/` beside
        `<project>/_agents/memory/`. Recognised-legacy, and its files are
        still memory files."""
        legacy_dir = schema_plexus.project_dir / "memory"
        _add_memory(legacy_dir, pf.PROJECT_MEMORY_FILE, _memory("project"))
        _add_memory(legacy_dir, "user_lifted.md", _memory("user"))
        report = _run(schema_plexus)
        rel = schema_plexus.project_dir.relative_to(schema_plexus.root).as_posix()
        assert _only(report, ag.LEGACY_SURFACE).path == f"{rel}/memory"
        assert _only(report, ag.MEMORY_SCOPE_MISMATCH).path.endswith("user_lifted.md")


# ---------------------------------------------------------------------------
# the entry point
# ---------------------------------------------------------------------------


class TestEntryPoint:
    def test_explicit_project_dirs_replace_discovery(self, schema_plexus):
        """A composing runner that already walked the tree hands the project
        dirs in; discovery is the default, not the only way."""
        _add_memory(
            schema_plexus.project_memory_dir, "user_misrouted.md", _memory("user")
        )
        report = Report(Form.SCHEMA)
        ag.check_agents(schema_plexus.root, report, project_dirs=())
        assert report.findings == ()

    def test_discovery_finds_every_project_scope(self, schema_plexus):
        found = {project.path for project in ag.iter_project_dirs(schema_plexus.root)}
        assert schema_plexus.project_dir in found
        assert schema_plexus.repoless_dir in found
        assert schema_plexus.bucket_dir in found

    def test_discovery_skips_schema_areas_and_dotfiles(self, schema_plexus):
        (schema_plexus.root / ".hidden" / "_runs").mkdir(parents=True)
        found = {
            project.path.name for project in ag.iter_project_dirs(schema_plexus.root)
        }
        assert "_agents" not in found
        assert ".hidden" not in found

    def test_discovery_is_the_shared_walk(self, schema_plexus):
        """!256 discussion 5: three modules, three descents, three answers.
        There is one walk now, and this module does not keep a copy."""
        assert ag.iter_project_dirs(schema_plexus.root) == walk.project_dirs(
            schema_plexus.root
        )

    def test_a_supplied_project_dir_is_checked_at_the_form_it_carries(
        self, schema_plexus
    ):
        """The bucket tier travels WITH the project dir: `project_dirs=` used
        to force the report's own form on everything it was handed, which
        would have promoted a bucket's memory findings to the schema tier —
        the opposite of D9's "a bucket never blocks"."""
        _add_memory(
            schema_plexus.bucket_dir / "_agents" / "memory",
            "user_misrouted.md",
            _memory("user"),
        )
        report = Report(Form.SCHEMA)
        ag.check_agents(
            schema_plexus.root,
            report,
            project_dirs=walk.project_dirs(schema_plexus.root, form=Form.SCHEMA),
        )
        assert _only(report, ag.MEMORY_SCOPE_MISMATCH).severity is Severity.INFO

    def test_a_supplied_plain_path_leaves_the_tier_to_the_report(self, schema_plexus):
        # The keyword still takes bare paths; without a form to carry, the
        # report's own form decides, as it always did.
        _add_memory(
            schema_plexus.project_memory_dir, "user_misrouted.md", _memory("user")
        )
        report = Report(Form.SCHEMA)
        ag.check_agents(
            schema_plexus.root, report, project_dirs=[schema_plexus.project_dir]
        )
        assert _only(report, ag.MEMORY_SCOPE_MISMATCH).severity is Severity.WARN

    def test_a_tree_without_an_agents_surface_is_silent(self, tmp_path):
        root = tmp_path / "bare"
        (root / "lmer" / "_runs").mkdir(parents=True)
        report = Report(Form.SCHEMA)
        ag.check_agents(root, report)
        assert report.findings == ()

    def test_paths_are_repo_relative(self, schema_plexus):
        _add_memory(
            schema_plexus.project_memory_dir, "user_misrouted.md", _memory("user")
        )
        for finding in _run(schema_plexus).findings:
            assert not Path(finding.path).is_absolute()

    def test_check_agents_accepts_a_string_root(self, schema_plexus):
        report = Report(Form.SCHEMA)
        ag.check_agents(str(schema_plexus.root), report)
        assert report.findings == ()
