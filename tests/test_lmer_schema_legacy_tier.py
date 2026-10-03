"""D14's legacy lenient tier, on the shared legacy-form plexus (issue #353;
spec §0 D14/D16 and §7 "Legacy tier"; SCHEMA.md "Validator" / "Legacy form").

D14 makes a tree with no manifest the LENIENT tier: it is recognised as
legacy (informational), and the integrity findings schema form calls errors
are downgraded, so the live 20C repo validates unmodified (G3) and the
validator reads as the list of things to fix BEFORE adopting rather than as a
gate. The same sentence draws the line the other way: state-file SHAPES are
checked "wherever they live", so a corrupt ``state.yaml`` is broken in any
form. This module proves both halves, and the second one as hard as the
first — the lenient tier must never be the reason a corrupt state file passes.

How each downgrade is proved
============================
One defect, injected into BOTH fixture trees by the same function, validated
twice:

1. the SCHEMA-form tree must produce the un-downgraded finding (this is the
   half that makes the test mean something — a downgrade test that passes
   because the defect never reached the walk at all is worse than no test, so
   the un-downgraded severity is asserted first, at the path the injection
   names);
2. the LEGACY tree must produce the SAME code at the SAME kind of path, one
   severity softer;
3. the pristine tree must not carry that code at that path, so the finding is
   the injection's and not the fixture's;
4. and :data:`lmer_schema.checks.findings.CHECKS` — the central downgrade map
   keyed by code — must say the same thing, so the behaviour and the table
   that ``--list-checks`` publishes cannot drift apart.

Both fixture trees are consumed READ-ONLY: every injection mutates the fresh
copy this test was handed (``tests/plexus_fixtures.py`` builds one per test),
never the builder.

The validator is reached through its importable core
(``lmer_schema.checks.runner.validate``) with a FIXED ``now``: the tier is a
property of the tree, never of the wall clock or of the ambient environment.
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lmer_schema.checks import CHECKS, Form, Severity, Tier
from lmer_schema.checks.runner import validate
from tests import plexus_fixtures as pf

#: Just after the fixture record's last session event, so no session is stale
#: and the verdict never depends on the machine the suite runs on.
NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)

LEGACY_HOST, LEGACY_PATH = pf.PRIMARY_REPO.split("/", 1)

#: The legacy project home, repo-relative — `{host}/{project}/` (Appendix A).
LEGACY_PROJECT_REL = f"{LEGACY_HOST}/{LEGACY_PATH}"

#: A memory file the injections add to a project-scope memory dir. The type is
#: `user`, which D17 routes to the INSTANCE scope — so the file is in the
#: wrong scope in either form, and only the severity differs.
MISSCOPED_MEMORY = "user_injected_scope_probe.md"

#: A `MEMORY.md` entry the injections add with no file beside it.
ABSENT_MEMORY = "project_injected_gone.md"

#: A `PLANS.md` entry the injections add with no plan file beside it.
ABSENT_PLAN = "2026-08-24-injected-gone.md"

#: The dated `_specs/`/`specs/` entry the injections add as a dangling link.
DANGLING_SPEC = f"2026-08-24-{pf.SLUG}--injected-gone.md"


def _validate(tree, **kwargs):
    kwargs.setdefault("now", NOW)
    return validate(tree.root, **kwargs)


def _rel(tree, path):
    """*path* as the repo-relative POSIX string a finding carries."""
    return Path(path).relative_to(tree.root).as_posix()


def _at(report, rel):
    """Every finding about exactly *rel*."""
    return [finding for finding in report.findings if finding.path == rel]


def _under(report, rel):
    """Every finding about *rel* or anything below it."""
    return [
        finding
        for finding in report.findings
        if finding.path == rel or finding.path.startswith(f"{rel}/")
    ]


def _one(report, code, rel):
    """The single *code* finding at *rel* — the injection's own."""
    matches = [
        finding for finding in _at(report, rel) if finding.code == code
    ]
    assert len(matches) == 1, (
        f"expected exactly one {code} at {rel}, got "
        f"{[(f.code, f.severity.value, f.path) for f in report.findings]}"
    )
    return matches[0]


def _codes(report):
    return sorted({finding.code for finding in report.findings})


def _append(path, text):
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.write_text(existing + text, encoding="utf-8")


# ---------------------------------------------------------------------------
# The injections — ONE defect, spelled once, applied to either form
# ---------------------------------------------------------------------------
#
# Each takes a built tree and returns the repo-relative path its finding is
# expected at. Where the two forms spell a path differently (`_runs/` versus
# `runs/`) the spelling is DERIVED from the tree's own map, so the defect is
# identical while the address stays each form's own — the fixture is never
# respelled here.


def inject_dangling_index_link(tree):
    """A dated index symlink whose target does not exist (§3.2).

    Relative and inside the plexus — the two shape rules that are errors in
    every form — so what is left is the integrity rule D14 softens.
    """
    link = tree.specs_dir / DANGLING_SPEC
    target = os.path.relpath(tree.session_dirs[0] / "injected-gone.md", tree.specs_dir)
    link.symlink_to(target)
    return _rel(tree, link)


def inject_memory_entry_without_file(tree):
    """A ``MEMORY.md`` entry linking a memory file that is not there."""
    index = tree.project_memory_dir / "MEMORY.md"
    _append(index, f"- [Injected gone]({ABSENT_MEMORY}) — nothing here.\n")
    return _rel(tree, index)


def inject_memory_scope_mismatch(tree):
    """A `user` memory (instance scope, D17) sitting in the PROJECT scope.

    Indexed as well as written, so the only finding about it is the scope one
    (an unindexed file would also be `agents.memory-file-without-entry`).
    """
    path = tree.project_memory_dir / MISSCOPED_MEMORY
    path.write_text(
        "---\n"
        "name: injected-scope-probe\n"
        'description: "a user memory injected into the project scope"\n'
        "metadata:\n"
        "  node_type: memory\n"
        "  type: user\n"
        "---\n"
        "\n"
        "Injected by tests/test_lmer_schema_legacy_tier.py.\n",
        encoding="utf-8",
    )
    _append(
        tree.project_memory_dir / "MEMORY.md",
        f"- [Injected scope probe]({MISSCOPED_MEMORY}) — a user memory in the "
        "project scope.\n",
    )
    return _rel(tree, path)


def inject_plans_entry_without_file(tree):
    """A ``PLANS.md`` entry whose plan file does not exist (§3.3).

    The legacy twin has no plans area at all (Appendix A: `_plans/` is new),
    so the bare `plans/` dir is created here — a legacy-tier name the
    validator recognises in either form.
    """
    plans = tree.plans_dir if tree.plans_dir is not None else tree.project_dir / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    index = plans / "PLANS.md"
    if not index.is_file():
        _append(index, "# Plans\n\n")
    _append(index, f"- [Injected gone]({ABSENT_PLAN}) — plan file removed.\n")
    return _rel(tree, index)


def inject_state_bad_enum(tree):
    """A ``state.yaml`` whose `status:` is outside RUN-STATE.md's closed enum."""
    state = tree.session_dirs[0] / "state.yaml"
    text = state.read_text(encoding="utf-8")
    for status in ("in-progress", "complete", "archived"):
        text = text.replace(f"status: {status}", "status: paused")
    state.write_text(text, encoding="utf-8")
    return _rel(tree, state)


def inject_state_non_int_schema(tree):
    """A ``state.yaml`` whose `schema:` is a YAML bool, not an integer."""
    state = tree.session_dirs[0] / "state.yaml"
    text = state.read_text(encoding="utf-8").replace("schema: 1", "schema: yes", 1)
    state.write_text(text, encoding="utf-8")
    return _rel(tree, state)


def inject_state_unparseable(tree):
    """A ``state.yaml`` that is not YAML at all."""
    state = tree.session_dirs[0] / "state.yaml"
    state.write_text(
        "schema: 1\nstatus: in-progress\n  bad: [unclosed\n", encoding="utf-8"
    )
    return _rel(tree, state)


def inject_events_line_unparseable(tree):
    """An ``events.jsonl`` line that is not one JSON object."""
    events = tree.session_dirs[0] / "events.jsonl"
    _append(events, "not json at all\n")
    return _rel(tree, events)


#: The D14 downgrades: (code, injection, schema-form severity, legacy
#: severity). The first three are the ones D14 and §7 name by behaviour; the
#: fourth is the `PLANS.md` half of the same sentence ("index entries without
#: a file"), which the legacy fixture has no area for until one is made.
DOWNGRADES = (
    ("index.dangling-link", inject_dangling_index_link, Severity.ERROR, Severity.WARN),
    (
        "agents.memory-entry-without-file",
        inject_memory_entry_without_file,
        Severity.ERROR,
        Severity.WARN,
    ),
    (
        "agents.memory-scope-mismatch",
        inject_memory_scope_mismatch,
        Severity.WARN,
        Severity.INFO,
    ),
    (
        "index.entry-without-file",
        inject_plans_entry_without_file,
        Severity.ERROR,
        Severity.WARN,
    ),
)

#: The shape violations D14 keeps at ERROR "wherever they live".
SHAPE_DEFECTS = (
    ("record.state-enum", inject_state_bad_enum),
    ("record.state-schema", inject_state_non_int_schema),
    ("record.state-unparseable", inject_state_unparseable),
    ("record.events-line-unparseable", inject_events_line_unparseable),
)


def _downgrade_id(case):
    return case[0]


def _shape_id(case):
    return case[0]


# ---------------------------------------------------------------------------
# Recognition: a legacy tree is recognised, not reported as noise
# ---------------------------------------------------------------------------


class TestRecognizedLegacy:
    """No manifest is a FORM, not a defect (D16: migration is never a
    prerequisite). The bare names of §9 are recognised in place, so the
    output of a legacy tree is short enough to be read."""

    def test_no_manifest_is_the_legacy_form_and_an_informational_finding(
        self, legacy_plexus
    ):
        report = _validate(legacy_plexus)
        assert report.form is Form.LEGACY
        finding = _one(report, "manifest.absent", ".")
        assert finding.severity is Severity.INFO
        assert CHECKS["manifest.absent"].tier is Tier.LEGACY

    def test_the_untouched_legacy_tree_exits_zero(self, legacy_plexus):
        """G3: the live legacy repo validates unmodified."""
        report = _validate(legacy_plexus)
        assert report.errors == ()
        assert report.warnings == ()
        assert report.exit_code == 0
        assert report.unusable is None

    def test_every_finding_about_it_is_informational(self, legacy_plexus):
        report = _validate(legacy_plexus)
        assert [f for f in report.findings if f.severity is not Severity.INFO] == []
        assert len(report.findings) == len(report.infos) > 0

    @pytest.mark.parametrize(
        "surface",
        ["agent-files", "mirrors", f"{LEGACY_PROJECT_REL}/memory"],
    )
    def test_bare_agent_surfaces_are_recognised_legacy(self, legacy_plexus, surface):
        report = _validate(legacy_plexus)
        finding = _one(report, "agents.legacy-surface", surface)
        assert finding.severity is Severity.INFO
        assert CHECKS["agents.legacy-surface"].tier is Tier.LEGACY

    def test_a_standalone_sources_yaml_is_recognised_legacy(self, legacy_plexus):
        report = _validate(legacy_plexus)
        finding = _one(report, "manifest.sources-legacy-file", "sources.yaml")
        assert finding.severity is Severity.INFO
        # WARN beside a manifest (a leftover to fold in), INFO on its own.
        assert CHECKS["manifest.sources-legacy-file"].severity is Severity.WARN

    def test_the_host_path_is_the_project_home_not_an_unnamed_bucket(
        self, legacy_plexus
    ):
        report = _validate(legacy_plexus)
        finding = _one(report, "tree.legacy-project-home", LEGACY_PROJECT_REL)
        assert finding.severity is Severity.INFO
        assert "tree.host-bucket" not in _codes(report)

    @pytest.mark.parametrize("area", ["runs", "specs", "info", "taskdef"])
    def test_bare_project_areas_are_never_unknown_file_noise(
        self, legacy_plexus, area
    ):
        """`runs/` and `specs/` (and the areas beside them) are read in place:
        nothing about them, or anything inside them, is a warning."""
        report = _validate(legacy_plexus)
        loud = [
            finding
            for finding in _under(report, f"{LEGACY_PROJECT_REL}/{area}")
            if finding.severity is not Severity.INFO
        ]
        assert loud == []

    def test_the_bare_instance_areas_are_silent(self, legacy_plexus):
        """`napkin/`, `taskdef/`, `knowledge/` carry no finding at all — they
        are recognised by the walk itself, never listed as projects."""
        report = _validate(legacy_plexus)
        for name in ("napkin", "taskdef", "README.md"):
            assert _under(report, name) == []


# ---------------------------------------------------------------------------
# The lenient half: the D14 downgrades, one defect at a time
# ---------------------------------------------------------------------------


class TestDowngrades:
    """Each downgrade proved by the SAME injection in both trees.

    The schema-form assertion is not decoration: it is what shows the defect
    reached the check at all. Without it a downgrade test passes just as
    happily when nothing fired.
    """

    @pytest.mark.parametrize("case", DOWNGRADES, ids=_downgrade_id)
    def test_the_defect_is_not_in_the_pristine_tree(self, case, tmp_path):
        """The finding is the INJECTION's: an untouched twin of each tree says
        nothing about the path the injection names."""
        code, inject, _schema_severity, _legacy_severity = case
        for build in (pf.build_schema_plexus, pf.build_legacy_plexus):
            form = build.__name__.split("_")[1]
            rel = inject(build(tmp_path / f"subject-{form}"))
            pristine = _validate(build(tmp_path / f"pristine-{form}"))
            assert [f for f in _at(pristine, rel) if f.code == code] == [], (
                f"{code} at {rel} is in the fixture already — the injection "
                "proves nothing"
            )

    @pytest.mark.parametrize("case", DOWNGRADES, ids=_downgrade_id)
    def test_schema_form_reports_the_undowngraded_severity(self, case, schema_plexus):
        code, inject, schema_severity, _legacy_severity = case
        rel = inject(schema_plexus)
        report = _validate(schema_plexus)
        assert report.form is Form.SCHEMA
        assert _one(report, code, rel).severity is schema_severity

    @pytest.mark.parametrize("case", DOWNGRADES, ids=_downgrade_id)
    def test_the_legacy_tree_softens_the_same_defect(self, case, legacy_plexus):
        code, inject, schema_severity, legacy_severity = case
        rel = inject(legacy_plexus)
        report = _validate(legacy_plexus)
        assert report.form is Form.LEGACY
        finding = _one(report, code, rel)
        assert finding.severity is legacy_severity
        assert finding.severity is not schema_severity

    @pytest.mark.parametrize("case", DOWNGRADES, ids=_downgrade_id)
    def test_the_downgrade_never_blocks_the_legacy_tree(self, case, legacy_plexus):
        """D14: in a legacy tree the validator is a migration aid, not a gate
        — the same defect that is exit 1 in schema form is exit 0 here."""
        _code, inject, schema_severity, _legacy_severity = case
        inject(legacy_plexus)
        report = _validate(legacy_plexus)
        assert report.errors == ()
        assert report.exit_code == 0
        assert schema_severity in (Severity.ERROR, Severity.WARN)

    @pytest.mark.parametrize("case", DOWNGRADES, ids=_downgrade_id)
    def test_the_schema_tree_still_blocks_on_it(self, case, schema_plexus):
        code, inject, schema_severity, _legacy_severity = case
        inject(schema_plexus)
        report = _validate(schema_plexus)
        expected = 1 if schema_severity is Severity.ERROR else 0
        assert report.exit_code == expected, (
            f"{code} at {schema_severity.value} should exit {expected}"
        )

    @pytest.mark.parametrize("case", DOWNGRADES, ids=_downgrade_id)
    def test_the_central_map_says_the_same(self, case):
        """The severity is a property of the CODE (findings.py's decision), so
        the table ``--list-checks`` publishes and the behaviour above cannot
        drift apart."""
        code, _inject, schema_severity, legacy_severity = case
        entry = CHECKS[code]
        assert entry.severity is schema_severity
        assert entry.legacy is legacy_severity
        assert entry.tier_sensitive
        assert entry.severity_for(Form.SCHEMA) is schema_severity
        assert entry.severity_for(Form.LEGACY) is legacy_severity

    def test_the_downgrade_table_is_the_one_this_module_pins(self):
        """Every tier-sensitive code is either one of the four integrity
        downgrades above or the standalone-`sources.yaml` recognition (WARN
        beside a manifest, INFO on its own — the recognition half). A new
        downgrade lands here, in the D14 table, with a test."""
        assert {entry.code for entry in CHECKS.tier_sensitive()} == {
            code for code, _inject, _schema, _legacy in DOWNGRADES
        } | {"manifest.sources-legacy-file"}


# ---------------------------------------------------------------------------
# The half that is NOT downgraded, asserted just as hard
# ---------------------------------------------------------------------------


class TestShapesAreBrokenInEveryForm:
    """"State-file SHAPES are still checked wherever they live" (§7).

    This is what makes D14 safe: leniency is about INTEGRITY between files,
    never about whether a machine-owned file parses. A regression here would
    let a corrupt record pass merely because its tree has no manifest yet —
    and the legacy tree is the one holding today's live records.
    """

    @pytest.mark.parametrize("case", SHAPE_DEFECTS, ids=_shape_id)
    def test_the_schema_tree_calls_it_an_error(self, case, schema_plexus):
        code, inject = case
        rel = inject(schema_plexus)
        report = _validate(schema_plexus)
        assert _one(report, code, rel).severity is Severity.ERROR
        assert report.exit_code == 1

    @pytest.mark.parametrize("case", SHAPE_DEFECTS, ids=_shape_id)
    def test_the_legacy_tree_calls_it_an_error_too(self, case, legacy_plexus):
        code, inject = case
        rel = inject(legacy_plexus)
        report = _validate(legacy_plexus)
        assert report.form is Form.LEGACY
        assert _one(report, code, rel).severity is Severity.ERROR
        assert report.exit_code == 1

    @pytest.mark.parametrize("case", SHAPE_DEFECTS, ids=_shape_id)
    def test_no_shape_code_is_tier_sensitive(self, case):
        code, _inject = case
        entry = CHECKS[code]
        assert not entry.tier_sensitive
        assert entry.severity is Severity.ERROR
        assert entry.legacy is Severity.ERROR
        assert entry.severity_for(Form.LEGACY) is Severity.ERROR

    def test_the_lenient_tier_is_never_why_a_corrupt_record_passes(
        self, legacy_plexus
    ):
        """Every downgrade AND a corrupt state file in one legacy tree: the
        softened findings stay soft, and the tree still exits 1 — the corrupt
        record is the only reason, and it is reason enough."""
        downgraded = {code: inject(legacy_plexus) for code, inject, _s, _l in DOWNGRADES}
        state_rel = inject_state_bad_enum(legacy_plexus)
        report = _validate(legacy_plexus)

        for (code, _inject, _schema, legacy_severity) in DOWNGRADES:
            assert _one(report, code, downgraded[code]).severity is legacy_severity
        assert [finding.code for finding in report.errors] == ["record.state-enum"]
        assert _one(report, "record.state-enum", state_rel).severity is Severity.ERROR
        assert report.exit_code == 1

    def test_a_legacy_state_file_is_reached_at_all(self, legacy_plexus):
        """The guard for the guard: the legacy record lives in a bare
        ``runs/<slug>/`` dir, so the checks above would pass vacuously if the
        record walk never found it. It is found, and by that path."""
        rel = inject_state_bad_enum(legacy_plexus)
        assert rel == f"{LEGACY_PROJECT_REL}/runs/{pf.SLUG}/state.yaml"
        report = _validate(legacy_plexus)
        assert _at(report, rel) != []
