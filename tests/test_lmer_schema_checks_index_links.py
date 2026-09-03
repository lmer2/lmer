"""``lmer_schema.checks.index_links`` — the ``_specs/``/``_plans/`` symlink
and ``PLANS.md`` index checks (spec §3.2/§7, D11/D14; SCHEMA.md "Inventory"
and "Index lines").

SCHEMA.md's Inventory is the contract these tests pin, at both severities the
D14 tier table gives each code:

- an index symlink must RESOLVE (dangling = ERROR in schema form, WARN under
  the legacy tier: an integrity gap), and must be RELATIVE and stay inside the
  plexus (absolute or escaping = ERROR in every form: a shape violation of the
  index mechanic, which no tree ever gets to soften);
- a symlink into a session dir is the normal case, including the ``--<name>``
  suffix a named session's directory carries after its freeze;
- ``PLANS.md`` consistency: an entry whose plan file is missing is an
  integrity finding (ERROR schema / WARN legacy), a plan file with no entry is
  a WARN, and a TOMBSTONED line — link replaced by ``archived``/``forgotten``
  and a date, or re-pointed at the ``_archive/`` mirror — is legal and is
  NEVER a finding;
- the legacy bare ``specs/``/``plans/`` twins are checked exactly like their
  ``_``-prefixed forms, and a host bucket inside a schema-form tree is checked
  at the legacy tier's severities (SCHEMA.md "Outside the schema");
- WHICH directories are projects is :mod:`lmer_schema.checks.walk`'s answer,
  shared with the other per-project modules (!256 discussion 5) and pinned in
  that module's tests; what is pinned here is that this module asks it and
  agrees with it about the tier;
- the three symlink rules themselves are :mod:`lmer_schema.checks.symlinks`'
  (the support module ``record`` consumes for the session-dir artifact links,
  !261 discussion 1) and are pinned in that module's tests; what is pinned
  here is the MEANING this module gives the answers — the codes, the words
  and the tier — and that the reviewer's control tree reads exactly as it did
  before the rule moved.

Every negative test injects its defect into ITS OWN fresh fixture tree; the
shared builders in ``tests/plexus_fixtures.py`` are consumed read-only.
"""

from pathlib import Path

import pytest

from tests import plexus_fixtures as pf
from lmer_schema.checks import CHECKS, Form, Report, Severity
from lmer_schema.checks import index_links as il
from lmer_schema.checks import walk
from lmer_schema.checks.index_links import check_index_links

DANGLING = "index.dangling-link"
NOT_RELATIVE = "index.link-not-relative"
ESCAPES = "index.link-escapes"
ENTRY_WITHOUT_FILE = "index.entry-without-file"
FILE_WITHOUT_ENTRY = "index.file-without-entry"


def _run(tree, **kwargs):
    """Run the check over a built fixture tree at the tree's own form."""
    report = Report(tree.form)
    check_index_links(tree.root, report, **kwargs)
    return report


def _codes(report):
    return [finding.code for finding in report.findings]


def _one(report, code):
    """The single finding carrying *code* (fails loudly on 0 or 2+)."""
    matches = [finding for finding in report.findings if finding.code == code]
    assert len(matches) == 1, f"expected exactly one {code}, got {_codes(report)}"
    return matches[0]


def _plan(tree, name, body="# A plan\n\nFixture plan body.\n"):
    """Write a plan file into the tree's project ``_plans``/``plans`` dir."""
    plans = _plans_dir(tree)
    plans.mkdir(parents=True, exist_ok=True)
    path = plans / name
    path.write_text(body, encoding="utf-8")
    return path


def _plans_dir(tree):
    """The project's plans dir in whichever form the tree is in."""
    name = "_plans" if tree.form == pf.SCHEMA_FORM else "plans"
    return tree.project_dir / name


def _index(tree, text):
    """Write the project's ``PLANS.md`` index."""
    plans = _plans_dir(tree)
    plans.mkdir(parents=True, exist_ok=True)
    path = plans / "PLANS.md"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The registered codes
# ---------------------------------------------------------------------------


class TestCodes:
    def test_the_four_shared_codes_come_from_the_tier_table(self):
        """The D14 codes are registered by ``findings`` (this module consumes
        them); re-registering any of them would raise at import time."""
        for code in (DANGLING, NOT_RELATIVE, ESCAPES, ENTRY_WITHOUT_FILE):
            assert code in CHECKS
        source = Path(il.__file__).read_text(encoding="utf-8")
        for code in (DANGLING, NOT_RELATIVE, ESCAPES, ENTRY_WITHOUT_FILE):
            assert f'register(\n    "{code}"' not in source, code

    def test_integrity_codes_soften_in_a_legacy_tree(self):
        for code in (DANGLING, ENTRY_WITHOUT_FILE):
            assert CHECKS.severity_for(code, Form.SCHEMA) is Severity.ERROR
            assert CHECKS.severity_for(code, Form.LEGACY) is Severity.WARN

    def test_shape_codes_are_error_in_every_form(self):
        for code in (NOT_RELATIVE, ESCAPES):
            for form in Form:
                assert CHECKS.severity_for(code, form) is Severity.ERROR

    def test_file_without_entry_is_a_warning_in_both_forms(self):
        assert FILE_WITHOUT_ENTRY in CHECKS
        entry = CHECKS[FILE_WITHOUT_ENTRY]
        assert not entry.tier_sensitive
        for form in Form:
            assert CHECKS.severity_for(FILE_WITHOUT_ENTRY, form) is Severity.WARN

    def test_every_code_this_module_emits_lives_in_the_index_namespace(self):
        for code in il.CODES:
            assert CHECKS[code].namespace == "index"


# ---------------------------------------------------------------------------
# The clean fixture trees
# ---------------------------------------------------------------------------


class TestCleanTrees:
    def test_schema_fixture_has_nothing_to_say(self, schema_plexus):
        assert _run(schema_plexus).findings == ()

    def test_legacy_fixture_has_nothing_to_say(self, legacy_plexus):
        assert _run(legacy_plexus).findings == ()

    def test_a_project_with_no_index_dirs_is_silent(self, schema_plexus):
        """The repoless project carries an empty ``PLANS.md`` and no
        ``_specs/`` — absence says nothing."""
        for path in (schema_plexus.specs_dir, schema_plexus.plans_dir):
            assert path.is_dir()
        assert _run(schema_plexus).findings == ()


# ---------------------------------------------------------------------------
# Symlink integrity
# ---------------------------------------------------------------------------


class TestDanglingLinks:
    def test_dangling_specs_link_is_an_error_in_schema_form(self, schema_plexus):
        entry = schema_plexus.specs_entries[0]
        entry.resolve().unlink()
        report = _run(schema_plexus)
        finding = _one(report, DANGLING)
        assert finding.severity is Severity.ERROR
        assert finding.path == str(entry.relative_to(schema_plexus.root))
        assert "spec.md" in (finding.detail or "")

    def test_dangling_legacy_specs_link_is_only_a_warning(self, legacy_plexus):
        entry = legacy_plexus.specs_entries[0]
        entry.resolve().unlink()
        report = _run(legacy_plexus)
        finding = _one(report, DANGLING)
        assert finding.severity is Severity.WARN
        assert report.exit_code == 0

    def test_dangling_run_plan_link_is_found_in_the_plans_dir(self, schema_plexus):
        link = _plans_dir(schema_plexus) / "2026-08-24-issue-12--plan.md"
        link.symlink_to(f"../_runs/{schema_plexus.slug}/{pf.SESSION_DIR_NAMES[0]}/plan.md")
        finding = _one(_run(schema_plexus), DANGLING)
        assert finding.path.endswith("2026-08-24-issue-12--plan.md")

    def test_a_link_into_a_named_session_dir_resolves(self, schema_plexus):
        """A named session's dir carries the ``--<name>`` suffix after its
        freeze; the index points at whatever the dir is called now."""
        session = schema_plexus.session_dirs[1]
        assert session.name.endswith(f"--{pf.SESSION_NAME}")
        (session / "plan.md").write_text("# Plan\n", encoding="utf-8")
        link = _plans_dir(schema_plexus) / "2026-08-24-issue-12--plan.md"
        link.symlink_to(f"../_runs/{schema_plexus.slug}/{session.name}/plan.md")
        assert _run(schema_plexus).findings == ()


class TestLinkShape:
    def test_absolute_target_is_an_error_in_schema_form(self, schema_plexus):
        entry = schema_plexus.specs_entries[0]
        target = entry.resolve()
        entry.unlink()
        entry.symlink_to(target)
        finding = _one(_run(schema_plexus), NOT_RELATIVE)
        assert finding.severity is Severity.ERROR
        assert _codes(_run(schema_plexus)) == [NOT_RELATIVE]

    def test_absolute_target_stays_an_error_in_a_legacy_tree(self, legacy_plexus):
        """A shape violation of the index mechanic, not an integrity gap: the
        legacy tier never softens it (D14)."""
        entry = legacy_plexus.specs_entries[0]
        target = entry.resolve()
        entry.unlink()
        entry.symlink_to(target)
        report = _run(legacy_plexus)
        assert _one(report, NOT_RELATIVE).severity is Severity.ERROR
        assert report.exit_code == 1

    def test_a_target_escaping_the_plexus_is_an_error(self, schema_plexus, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("# Outside\n", encoding="utf-8")
        link = schema_plexus.specs_dir / "2026-08-24-issue-12--escape.md"
        link.symlink_to(Path("..") / ".." / ".." / outside.name)
        finding = _one(_run(schema_plexus), ESCAPES)
        assert finding.severity is Severity.ERROR
        assert ".." in (finding.detail or "")

    def test_an_escaping_target_is_not_also_reported_dangling(self, schema_plexus):
        link = schema_plexus.specs_dir / "2026-08-24-issue-12--escape.md"
        link.symlink_to("../../../nowhere/spec.md")
        assert _codes(_run(schema_plexus)) == [ESCAPES]

    def test_escaping_stays_an_error_in_a_legacy_tree(self, legacy_plexus):
        link = legacy_plexus.specs_dir / "2026-08-24-issue-12--escape.md"
        # The legacy specs dir sits under {host}/{project}/: one hop more than
        # its depth leaves the plexus.
        depth = len(legacy_plexus.specs_dir.relative_to(legacy_plexus.root).parts)
        link.symlink_to("../" * (depth + 1) + "nowhere/spec.md")
        report = _run(legacy_plexus)
        assert _one(report, ESCAPES).severity is Severity.ERROR

    def test_a_relative_target_with_parent_hops_inside_the_tree_is_fine(self, schema_plexus):
        link = schema_plexus.specs_dir / "2026-08-24-issue-12--readme.md"
        link.symlink_to("../../README.md")
        assert _run(schema_plexus).findings == ()

    def test_a_plain_file_in_the_specs_dir_is_not_a_link_finding(self, schema_plexus):
        (schema_plexus.specs_dir / "2026-08-24-issue-12--note.md").write_text(
            "# Not a link\n", encoding="utf-8"
        )
        assert _run(schema_plexus).findings == ()


# ---------------------------------------------------------------------------
# PLANS.md index-to-file consistency
# ---------------------------------------------------------------------------


class TestPlansIndex:
    def test_entry_with_a_file_is_clean(self, schema_plexus):
        _plan(schema_plexus, "mcp-slice-1.md")
        _index(schema_plexus, "# Plans — lmer\n\n- [MCP slice 1](mcp-slice-1.md) — active.\n")
        assert _run(schema_plexus).findings == ()

    def test_entry_without_a_file_is_an_error_in_schema_form(self, schema_plexus):
        index = _index(
            schema_plexus, "# Plans — lmer\n\n- [MCP slice 1](mcp-slice-1.md) — active.\n"
        )
        report = _run(schema_plexus)
        finding = _one(report, ENTRY_WITHOUT_FILE)
        assert finding.severity is Severity.ERROR
        assert finding.path == str(index.relative_to(schema_plexus.root))
        assert "mcp-slice-1.md" in (finding.detail or "")
        assert "line 3" in (finding.detail or "")

    def test_entry_without_a_file_is_a_warning_in_a_legacy_tree(self, legacy_plexus):
        _index(legacy_plexus, "# Plans\n\n- [MCP slice 1](mcp-slice-1.md) — active.\n")
        report = _run(legacy_plexus)
        assert _one(report, ENTRY_WITHOUT_FILE).severity is Severity.WARN
        assert report.exit_code == 0

    def test_plan_file_without_an_entry_is_a_warning(self, schema_plexus):
        plan = _plan(schema_plexus, "mcp-slice-1.md")
        _index(schema_plexus, "# Plans — lmer\n\n(no active plans)\n")
        report = _run(schema_plexus)
        finding = _one(report, FILE_WITHOUT_ENTRY)
        assert finding.severity is Severity.WARN
        assert finding.path == str(plan.relative_to(schema_plexus.root))
        assert report.exit_code == 0

    def test_a_plan_file_beside_no_index_at_all_is_the_same_warning(self, schema_plexus):
        """SCHEMA.md: ``PLANS.md`` is *expected when plan files exist* — an
        absent index is reported through the files it fails to carry, not as a
        second code."""
        (_plans_dir(schema_plexus) / "PLANS.md").unlink()
        _plan(schema_plexus, "mcp-slice-1.md")
        assert _codes(_run(schema_plexus)) == [FILE_WITHOUT_ENTRY]

    def test_the_index_itself_is_never_its_own_missing_plan_file(self, schema_plexus):
        _index(schema_plexus, "# Plans — lmer\n\n(no active plans)\n")
        assert _run(schema_plexus).findings == ()

    def test_a_run_plan_symlink_is_not_a_plan_file(self, schema_plexus):
        """The dated run-plan links are the index mechanic, not plan files;
        they are checked as symlinks and never want a ``PLANS.md`` line."""
        session = schema_plexus.session_dirs[0]
        (session / "plan.md").write_text("# Plan\n", encoding="utf-8")
        link = _plans_dir(schema_plexus) / "2026-08-24-issue-12--plan.md"
        link.symlink_to(f"../_runs/{schema_plexus.slug}/{session.name}/plan.md")
        assert _run(schema_plexus).findings == ()


class TestIndexLineGrammar:
    """SCHEMA.md "Index lines": an ENTRY is a line carrying a relative
    markdown link to a ``.md`` file; anything else is not an entry and is
    never a finding."""

    @pytest.mark.parametrize(
        "line",
        [
            "# Plans — lmer",
            "(no active plans)",
            "- Nothing to see, no link at all.",
            "- [The wiki](https://example.com/plans/mcp-slice-1.md) — off-tree.",
            "- [Absolute](/etc/plans/mcp-slice-1.md) — off-tree.",
            "- [A directory](_runs/) — not a markdown file.",
            "- [A picture](diagram.png) — not a markdown file.",
        ],
    )
    def test_non_entry_lines_are_never_findings(self, schema_plexus, line):
        _index(schema_plexus, f"# Plans — lmer\n\n{line}\n")
        assert _run(schema_plexus).findings == ()

    def test_a_line_with_two_links_checks_both(self, schema_plexus):
        _plan(schema_plexus, "kept.md")
        _index(
            schema_plexus,
            "# Plans — lmer\n\n- [Kept](kept.md) supersedes [Gone](gone.md).\n",
        )
        report = _run(schema_plexus)
        assert _codes(report) == [ENTRY_WITHOUT_FILE]
        assert "gone.md" in (_one(report, ENTRY_WITHOUT_FILE).detail or "")


class TestTombstones:
    """A tombstoned line is legal and is NEVER a finding (SCHEMA.md "Index
    lines"): the plan's content lives in git history and its line stays."""

    @pytest.mark.parametrize(
        "line",
        [
            "- MCP slice 1 — archived 2026-08-24.",
            "- MCP slice 1 — forgotten 2026-08-24.",
        ],
    )
    def test_a_line_whose_link_was_replaced_is_not_an_entry(self, schema_plexus, line):
        _index(schema_plexus, f"# Plans — lmer\n\n{line}\n")
        assert _run(schema_plexus).findings == ()

    def test_a_line_repointed_at_the_archive_mirror_is_never_dangling(self, schema_plexus):
        _index(
            schema_plexus,
            "# Plans — lmer\n\n"
            "- [MCP slice 1](../_archive/_plans/mcp-slice-1.md) — archived 2026-08-24.\n",
        )
        assert _run(schema_plexus).findings == ()

    def test_an_archive_tombstone_still_covers_a_file_not_yet_moved(self, schema_plexus):
        """Tombstone first, move second: the line IS the plan's entry, so the
        file it names is not also 'a plan file without an entry'."""
        _plan(schema_plexus, "mcp-slice-1.md")
        _index(
            schema_plexus,
            "# Plans — lmer\n\n"
            "- [MCP slice 1](../_archive/_plans/mcp-slice-1.md) — archived 2026-08-24.\n",
        )
        assert _run(schema_plexus).findings == ()

    def test_the_legacy_archive_home_tombstones_too(self, legacy_plexus):
        _index(
            legacy_plexus,
            "# Plans\n\n- [MCP slice 1](../runs/archive/mcp-slice-1.md) — archived.\n",
        )
        assert _run(legacy_plexus).findings == ()


# ---------------------------------------------------------------------------
# Where the check looks
# ---------------------------------------------------------------------------


class TestScope:
    def test_legacy_bare_dirs_are_checked_in_a_schema_form_tree(self, schema_plexus):
        """A half-migrated project keeps its bare ``specs/`` beside ``_specs/``;
        both are index dirs (SCHEMA.md Appendix A)."""
        legacy_specs = schema_plexus.project_dir / "specs"
        legacy_specs.mkdir()
        (legacy_specs / "2026-08-24-issue-12--spec.md").symlink_to("../runs/gone/spec.md")
        finding = _one(_run(schema_plexus), DANGLING)
        assert finding.path.startswith(f"{pf.PROJECT}/specs/")
        assert finding.severity is Severity.ERROR

    def test_a_host_bucket_is_checked_at_the_legacy_tier(self, schema_plexus):
        """SCHEMA.md "Outside the schema": a bucket's contents are checked at
        the legacy tier's severities even inside a schema-form tree."""
        specs = schema_plexus.bucket_dir / "_specs"
        specs.mkdir()
        (specs / "2026-08-24-issue-12--spec.md").symlink_to("../_runs/gone/spec.md")
        report = _run(schema_plexus)
        finding = _one(report, DANGLING)
        assert finding.path.startswith(f"{pf.BUCKET_HOST}/")
        assert finding.severity is Severity.WARN
        assert report.exit_code == 0

    def test_a_bucket_shape_violation_is_still_an_error(self, schema_plexus):
        specs = schema_plexus.bucket_dir / "_specs"
        specs.mkdir()
        (specs / "2026-08-24-issue-12--spec.md").symlink_to("/etc/hostname")
        assert _one(_run(schema_plexus), NOT_RELATIVE).severity is Severity.ERROR

    def test_the_archive_mirror_is_not_walked(self, schema_plexus):
        """Archived material keeps its own (now dangling) index links; the
        archive is a mirror of the past, not a live index (D13)."""
        archived = schema_plexus.project_dir / "_archive" / "_specs"
        archived.mkdir(parents=True)
        (archived / "2026-08-24-issue-12--spec.md").symlink_to("../../_runs/gone/spec.md")
        assert _run(schema_plexus).findings == ()

    def test_session_dirs_are_not_walked_for_indexes(self, schema_plexus):
        """A masterplan bundle's own ``_specs``-shaped dirs belong to a
        foreign writer (D12) and are opaque."""
        bundle = schema_plexus.session_dirs[0] / "masterplan" / "_plans"
        bundle.mkdir(parents=True)
        (bundle / "PLANS.md").write_text("- [Gone](gone.md)\n", encoding="utf-8")
        assert _run(schema_plexus).findings == ()

    def test_project_dirs_may_be_supplied_by_the_caller(self, schema_plexus):
        """The runner already knows the project dirs (the tree checks compute
        them); passing them in skips the discovery walk and must find the same
        findings."""
        entry = schema_plexus.specs_entries[0]
        entry.resolve().unlink()
        report = Report(schema_plexus.form)
        check_index_links(
            schema_plexus.root, report, projects=[schema_plexus.project_dir]
        )
        assert _codes(report) == [DANGLING]

    def test_supplied_project_dirs_may_be_root_relative(self, schema_plexus):
        entry = schema_plexus.specs_entries[0]
        entry.resolve().unlink()
        report = Report(schema_plexus.form)
        check_index_links(schema_plexus.root, report, projects=[Path(pf.PROJECT)])
        assert _codes(report) == [DANGLING]

    def test_a_missing_root_says_nothing(self, tmp_path):
        report = Report(Form.SCHEMA)
        check_index_links(tmp_path / "no-such-plexus", report)
        assert report.findings == ()


class TestDiscovery:
    def test_index_dirs_of_the_schema_tree(self, schema_plexus):
        found = {
            path.relative_to(schema_plexus.root).as_posix()
            for path in il.iter_index_dirs(schema_plexus.root)
        }
        assert found == {f"{pf.PROJECT}/_specs", f"{pf.PROJECT}/_plans",
                         f"{pf.REPOLESS_PROJECT}/_plans"}

    def test_discovery_is_the_shared_walk(self, schema_plexus):
        """Three modules once had three descents and three answers (!256
        discussion 5); this one keeps no copy of its own."""
        assert list(il.iter_project_dirs(schema_plexus.root)) == [
            project.path for project in walk.project_dirs(schema_plexus.root)
        ]

    def test_a_bucket_below_an_underscore_namespace_level_is_checked(
        self, schema_plexus
    ):
        """The concrete surface the fold could have lost: below the root, a
        ``_`` component comes from the REMOTE and is a namespace level like
        any other (!256 discussion 5, decision 1)."""
        specs = schema_plexus.root / "git.example.org" / "_ns" / "proj" / "_specs"
        specs.mkdir(parents=True)
        (specs / "2026-08-24-issue-12--spec.md").symlink_to("../_runs/gone/spec.md")
        finding = _one(_run(schema_plexus), DANGLING)
        assert finding.path.startswith("git.example.org/_ns/proj/")
        assert finding.severity is Severity.WARN  # a bucket: legacy tier

    def test_index_dirs_of_the_legacy_tree(self, legacy_plexus):
        found = {
            path.relative_to(legacy_plexus.root).as_posix()
            for path in il.iter_index_dirs(legacy_plexus.root)
        }
        assert found == {f"{pf.PRIMARY_REPO}/specs"}


# ---------------------------------------------------------------------------
# The control for !261 discussion 1
# ---------------------------------------------------------------------------


class TestTheReviewersControl:
    """The !261 demonstration put three broken links in a SESSION dir, where
    nothing looked at them, and the same three inside ``_specs/`` as the
    control, where they were already errors.

    The session-dir half is now checked (``record``, the SCHEMA.md
    ``<session-dir>/<artifact>.md`` row); this control is what must NOT have
    changed while the three rules moved into
    :mod:`lmer_schema.checks.symlinks` — the same codes, the same severities,
    the same one-answer-per-link order, over the same index dir.
    """

    def _demonstration(self, tree):
        specs = tree.specs_dir
        depth = len(specs.relative_to(tree.root).parts)
        (specs / "2026-08-24-issue-12--spec.md").symlink_to("../_runs/gone/spec.md")
        (specs / "2026-08-24-issue-12--plan.md").symlink_to(
            str(tree.root / "elsewhere" / "plan.md")
        )
        (specs / "2026-08-24-issue-12--retro.md").symlink_to(
            "../" * (depth + 1) + "elsewhere/retro.md"
        )

    def test_the_three_links_are_still_the_same_three_errors(self, schema_plexus):
        self._demonstration(schema_plexus)
        report = _run(schema_plexus)
        assert sorted(_codes(report)) == sorted([DANGLING, NOT_RELATIVE, ESCAPES])
        assert {finding.severity for finding in report.findings} == {Severity.ERROR}
        assert report.exit_code == 1

    def test_each_link_still_carries_its_own_target(self, schema_plexus):
        self._demonstration(schema_plexus)
        report = _run(schema_plexus)
        assert {finding.path.rsplit("--", 1)[1] for finding in report.findings} == {
            "spec.md",
            "plan.md",
            "retro.md",
        }
        for finding in report.findings:
            assert (finding.detail or "").startswith("target: ")

    def test_the_healthy_fixture_entry_beside_them_is_untouched(self, schema_plexus):
        self._demonstration(schema_plexus)
        entry = str(schema_plexus.specs_entries[0].relative_to(schema_plexus.root))
        assert entry not in [finding.path for finding in _run(schema_plexus).findings]
