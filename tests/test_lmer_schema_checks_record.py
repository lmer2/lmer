"""``lmer_schema.checks.record`` — the record-area checks (spec §3.5, D22,
D12; SCHEMA.md "record" / "Sessions and runs" / "Session directory"; issue
#353).

The record's own files are the shapes whose severity never softens: a corrupt
``state.yaml`` is broken in a legacy tree exactly as it is in a schema-form
one (D14, "state-file SHAPE violations stay ERROR wherever they live"), so
these tests run the same defects through BOTH forms of report and assert the
severity is identical. What DOES vary is the LAYOUT: ``_runs/`` is the
session-unit layout of D22 (a grouping level holding session dirs) and bare
``runs/`` is the legacy shared run dir, read in place and never restructured.

Pinned here:

- the four ``record.*`` codes ``findings.py`` pre-registers are CONSUMED, and
  every code in the namespace — the ones this module adds included — is
  form-insensitive;
- the run-area walk: both layouts at any depth (project dir and host bucket),
  ``_archive/``, ``runs/archive/`` and dot-dirs skipped, never descending
  into a record dir;
- the opaque/free-form boundary: the walk never enters ``_agents/``,
  ``_taskdef/``, ``_knowledge/``, ``_napkin/`` or their bare legacy twins, so
  a vendored fork's own run fixtures are never a finding — while the two
  areas that ARE records, ``<project>/_runs/`` and legacy
  ``{host}/{project}/runs/``, are checked exactly as before;
- state-file shape: ``state.yaml`` (with the legacy ``state.yml`` fallback),
  ``ledger.yaml`` and ``plan.index.json`` parse, their ``schema:`` is an int
  (a bool is not), and their closed enums — run status and ledger task
  status — are in the ``docs/RUN-STATE.md`` range;
- ``events.jsonl`` line parseability, including a truncated trailing line
  reported precisely instead of aborting the walk;
- the D22 session-unit shape: nothing is written at the grouping level, a
  session dir SHOULD carry a ``state.yaml``, a session id SHOULD be in the
  ``LMER_SESSION_ID`` form, and an ``in-progress`` session older than the
  staleness window is informational "possibly orphaned";
- masterplan opacity (D12): the bundle is checked for existence only — a
  defect INSIDE it is provably not walked — while the pre-D12 nested
  ``masterplan/<mp-slug>/`` shape is reported, never errored;
- the ``<session-dir>/<artifact>.md -> masterplan/<artifact>.md`` links the
  bundle sync writes (SCHEMA.md "Session directory"), in both layouts. This
  is the one place the module emits a TIER-SENSITIVE code: the three rules
  and the three codes are the index mechanic's — a dangling link is the
  integrity finding D14 softens to a WARN in a legacy tree, while an
  absolute or escaping target is a shape violation of the link mechanic and
  is an ERROR in every form — so "the record's severity never softens" is
  about the record's own file shapes, which is where it is asserted above.

The fixture trees are the shared ones (``tests/plexus_fixtures.py``); a
defect is injected into this test's own fresh copy, never into the builder.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from lmer_schema.checks import CHECKS, Form, Report, Severity, Tier
from lmer_schema.checks import record
from lmer_schema.checks import walk
from tests import plexus_fixtures as pf
from work_repo import run_state

#: A fixed "now" one hour after the fixture record's last update: the fixture
#: carries fixed dates, so a wall-clock default would make the staleness
#: assertions rot with time.
NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def _report(tree, *, form=None, **kwargs):
    """Run the record checks over *tree* and return the filled report."""
    report = Report(tree.form if form is None else form)
    record.check_record(tree.root, report, now=NOW, **kwargs)
    return report


def _codes(report):
    return [finding.code for finding in report.findings]


def _at(report, code):
    """The findings carrying *code*, in report order."""
    return [finding for finding in report.findings if finding.code == code]


def _session_dir(tree):
    """The fixture's first session dir (schema form) / shared run dir."""
    return tree.session_dirs[0]


def _group_dir(tree):
    """The `_runs/<slug>/` grouping level of a schema-form tree."""
    return tree.runs_dir / tree.slug


# ---------------------------------------------------------------------------
# The code table
# ---------------------------------------------------------------------------


#: Everything the ``record.*`` namespace owns after this module is imported:
#: the four codes findings.py pre-registers plus the session-unit ones.
RECORD_CODES = {
    "record.events-line-unparseable",
    "record.grouping-level-entry",
    "record.masterplan-nested",
    "record.plain-session-slug",
    "record.seed-dir-leftover",
    "record.session-dir-without-state",
    "record.session-id-malformed",
    "record.session-metadata",
    "record.release-claim",
    "record.session-stale",
    "record.state-enum",
    "record.state-schema",
    "record.state-unparseable",
}


class TestCodes:
    def test_the_namespace_is_exactly_these_codes(self):
        assert {cc.code for cc in CHECKS if cc.namespace == "record"} == RECORD_CODES

    @pytest.mark.parametrize("code", sorted(RECORD_CODES))
    def test_no_record_code_is_ever_downgraded(self, code):
        # D14: the record is the area whose severity never softens.
        entry = CHECKS[code]
        assert not entry.tier_sensitive
        assert entry.severity_for(Form.SCHEMA) is entry.severity_for(Form.LEGACY)

    @pytest.mark.parametrize(
        "code",
        [
            "record.state-unparseable",
            "record.state-schema",
            "record.state-enum",
            "record.events-line-unparseable",
        ],
    )
    def test_the_pre_registered_shape_codes_are_consumed_not_reregistered(self, code):
        # findings.py owns the D14 table; re-registering raises, and these
        # four are its record entries — still ERROR, still machine tier.
        assert CHECKS[code].severity is Severity.ERROR
        assert CHECKS[code].tier is Tier.MACHINE

    def test_session_unit_severities(self):
        assert CHECKS["record.grouping-level-entry"].severity is Severity.WARN
        assert CHECKS["record.session-dir-without-state"].severity is Severity.WARN
        assert CHECKS["record.session-id-malformed"].severity is Severity.WARN
        assert CHECKS["record.seed-dir-leftover"].severity is Severity.WARN
        assert CHECKS["record.session-stale"].severity is Severity.INFO
        assert CHECKS["record.masterplan-nested"].severity is Severity.INFO
        assert CHECKS["record.masterplan-nested"].tier is Tier.OPAQUE
        assert CHECKS["record.plain-session-slug"].severity is Severity.ERROR
        assert CHECKS["record.plain-session-slug"].tier is Tier.MACHINE

    def test_the_enums_are_the_run_state_writers(self):
        # docs/RUN-STATE.md is the authority; the writer's own constants are
        # its live copy, so a drift on either side fails here.
        assert record.RUN_STATUSES == run_state.STATUSES
        assert record.TASK_STATUSES == run_state.TASK_STATUSES
        assert record.MASTERPLAN_ARTIFACTS == run_state.MASTERPLAN_ARTIFACTS
        assert record.MASTERPLAN_DIR == run_state.MASTERPLAN_DIR


# ---------------------------------------------------------------------------
# The fixture trees are clean in both forms
# ---------------------------------------------------------------------------


class TestCleanTrees:
    def test_schema_form_fixture_has_nothing_to_say(self, schema_plexus):
        report = _report(schema_plexus)
        assert _codes(report) == []

    def test_legacy_fixture_has_nothing_to_say(self, legacy_plexus):
        # The legacy shared run dir is read in place: no session-unit
        # warnings, no restructuring nudges.
        report = _report(legacy_plexus)
        assert _codes(report) == []

    def test_finding_paths_are_repo_relative(self, schema_plexus):
        (_group_dir(schema_plexus) / "stray.txt").write_text("x", encoding="utf-8")
        finding = _report(schema_plexus).findings[0]
        assert finding.path == f"lmer/_runs/{schema_plexus.slug}/stray.txt"


class TestPlainSessionSlug:
    """`record.plain-session-slug` — the §7 fold's validator half (D10).

    The predicate is shared with the migrate blocker and the fold; this check
    only reports it, in schema form only, and never inside a host bucket (D9:
    a bucket never blocks, and a `record.*` code has no legacy downgrade).
    """

    def test_one_finding_per_plain_session_group_in_schema_form(self, schema_plexus):
        planted = pf.plant_plain_session_records(schema_plexus.project_dir)
        report = _report(schema_plexus)
        findings = _at(report, "record.plain-session-slug")
        assert len(findings) == len(pf.PLAIN_SESSION_GROUPS)
        assert {f.path for f in findings} == {
            f"lmer/_runs/{slug}" for _taskdef, _target, slug in pf.PLAIN_SESSION_GROUPS
        }
        for finding in findings:
            assert "work migrate <project>" in finding.message
            assert "slug:" in finding.message
        assert planted  # the planter wrote them where the walk looks

    def test_the_release_record_is_never_flagged(self, schema_plexus):
        pf.plant_plain_session_records(schema_plexus.project_dir)
        paths = {f.path for f in _at(_report(schema_plexus), "record.plain-session-slug")}
        for record_ in pf.RELEASE_RECORDS:
            assert f"lmer/_runs/{record_['slug']}" not in paths

    def test_a_legacy_tree_is_never_flagged(self, legacy_plexus):
        # No manifest: the fold happens at adoption, inside `work migrate`.
        pf.plant_plain_session_records(legacy_plexus.project_dir, area="runs", nested=False)
        assert _at(_report(legacy_plexus), "record.plain-session-slug") == []

    def test_a_host_bucket_never_blocks(self, schema_plexus):
        pf.plant_plain_session_records(schema_plexus.bucket_dir)
        assert _at(_report(schema_plexus), "record.plain-session-slug") == []

    def test_a_bare_runs_area_in_a_schema_tree_is_flagged(self, schema_plexus):
        # A registered project's bare `runs/` area: `work migrate <project>`
        # nests and folds it, so the check names the same command.
        pf.plant_plain_session_records(
            schema_plexus.project_dir, area="runs", nested=False
        )
        findings = _at(_report(schema_plexus), "record.plain-session-slug")
        assert len(findings) == len(pf.PLAIN_SESSION_GROUPS)
        assert all(f.path.startswith("lmer/runs/") for f in findings)

    def test_the_move_text_is_the_one_shared_formatter(self):
        text = record.plain_session_move_text("chat-.", "chat")
        assert "_runs/chat-./" in text
        assert "_runs/chat/" in text
        assert "slug: chat-. \u2192 chat" in text
        assert "work migrate <project>" in text


# ---------------------------------------------------------------------------
# The run-area walk
# ---------------------------------------------------------------------------


class TestRunAreaWalk:
    def _areas(self, tree):
        return {
            (path.relative_to(tree.root).as_posix(), layout)
            for path, layout in record.iter_run_areas(tree.root)
        }

    def test_schema_form_finds_the_project_and_the_host_bucket(self, schema_plexus):
        assert self._areas(schema_plexus) == {
            ("lmer/_runs", record.SESSION_LAYOUT),
            (
                f"{schema_plexus.bucket_dir.relative_to(schema_plexus.root).as_posix()}/_runs",
                record.SESSION_LAYOUT,
            ),
        }

    def test_legacy_finds_the_shared_run_dir_at_its_depth(self, legacy_plexus):
        rel = legacy_plexus.runs_dir.relative_to(legacy_plexus.root).as_posix()
        assert self._areas(legacy_plexus) == {(rel, record.LEGACY_LAYOUT)}

    def test_archive_mirrors_are_never_walked(self, schema_plexus):
        # SCHEMA.md: archived contents are not re-validated (D13).
        archived = (
            schema_plexus.project_dir / "_archive" / "_runs" / schema_plexus.slug
        )
        archived.mkdir(parents=True)
        (archived / "state.yaml").write_text("{{{", encoding="utf-8")
        assert self._areas(schema_plexus) == {
            ("lmer/_runs", record.SESSION_LAYOUT),
            (
                f"{schema_plexus.bucket_dir.relative_to(schema_plexus.root).as_posix()}/_runs",
                record.SESSION_LAYOUT,
            ),
        }
        assert _codes(_report(schema_plexus)) == []

    def test_dot_dirs_and_the_git_dir_are_skipped(self, schema_plexus):
        for name in (".git", "node_modules"):
            broken = schema_plexus.root / name / "x" / "_runs" / "s" / "sid"
            broken.mkdir(parents=True)
            (broken / "state.yaml").write_text("[]", encoding="utf-8")
        assert _codes(_report(schema_plexus)) == []

    def test_the_walk_never_descends_into_a_record_dir(self, schema_plexus):
        # A run dir may hold anything (a checkout, a bundle); a `runs/` name
        # inside one is not a second record area.
        nested = _session_dir(schema_plexus) / "runs" / "slug"
        nested.mkdir(parents=True)
        (nested / "state.yaml").write_text("not: [a, mapping\n", encoding="utf-8")
        assert record.LEGACY_LAYOUT not in {
            layout for _path, layout in record.iter_run_areas(schema_plexus.root)
        }
        assert _codes(_report(schema_plexus)) == []

    def test_a_symlinked_directory_is_not_followed(self, schema_plexus):
        # The §9 compat symlink: the target is walked at its real location.
        link = schema_plexus.root / "gitlab.example.com" / "group" / "project"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to("../../lmer", target_is_directory=True)
        areas = {path for path, _layout in record.iter_run_areas(schema_plexus.root)}
        assert link / "_runs" not in areas
        assert _codes(_report(schema_plexus)) == []

    def test_a_missing_root_says_nothing(self, tmp_path):
        report = Report(Form.SCHEMA)
        record.check_record(tmp_path / "nope", report, now=NOW)
        assert len(report) == 0


# ---------------------------------------------------------------------------
# The opaque / free-form boundary (SCHEMA.md "Strictness tiers", Inventory)
# ---------------------------------------------------------------------------


#: The reviewer's file: a state file broken two ways at once, so "it was not
#: read" and "it was read and forgiven" can never be confused.
BROKEN_STATE = "schema: nope\nstatus: bogus\n"

#: What that file yields WHEN it is read: the closed status enum and the
#: `schema:` int rule (report order — one path, so code order).
BROKEN_CODES = ["record.state-enum", "record.state-schema"]


def _plant(root, rel, text=BROKEN_STATE):
    """Write a state file at *rel* under *root*, creating its parents."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _bare_report(root, form=Form.SCHEMA):
    """Run the record checks over a hand-built root (no fixture tree)."""
    report = Report(form)
    record.check_record(root, report, now=NOW)
    return report


class TestOpaqueTiers:
    """The walk stops at the tiers SCHEMA.md declares opaque or free-form.

    A record area is a thing inside a PROJECT tree. `_agents/` (its `mirrors/`
    and per-harness subtrees), `_taskdef/`, `_knowledge/` and `_napkin/` are a
    foreign writer's or a human's, so a `runs/`-shaped directory inside one is
    not this validator's record — reading it produces ERRORs about a file the
    schema does not govern, with nothing an operator can do about them.
    """

    def test_the_vendored_fork_reproduction_is_clean(self, tmp_path):
        # !256 discussion 2, verbatim: a healthy plexus carrying a vendored
        # fork that ships run fixtures of its own.
        _plant(tmp_path, "_agents/mirrors/somefork/runs/job1/state.yaml")
        _plant(tmp_path, "_agents/claude/runs/x/state.yaml", "status: whatever\n")
        _plant(tmp_path, "_knowledge/acme/runs/y/state.yaml", "status: nonsense\n")
        report = _bare_report(tmp_path)
        assert _codes(report) == []
        assert report.exit_code == 0
        assert list(record.iter_run_areas(tmp_path)) == []

    def test_the_same_corrupt_state_inside_a_project_is_still_an_error(self, tmp_path):
        # The boundary is about WHERE the file is, never about the file: the
        # byte-identical state file in a project's `_runs/` still errors.
        _plant(tmp_path, "_agents/mirrors/somefork/runs/job1/state.yaml")
        _plant(tmp_path, "widgets/_runs/develop-1/20260824-101500-1-11111/state.yaml")
        report = _bare_report(tmp_path)
        assert _codes(report) == BROKEN_CODES
        assert all(f.path.startswith("widgets/_runs/") for f in report.findings)
        assert report.exit_code == 1

    @pytest.mark.parametrize(
        "area", ["_agents", "_info", "_knowledge", "_napkin", "_plans", "_taskdef"]
    )
    @pytest.mark.parametrize("layout", ["runs", "_runs"])
    def test_no_instance_scope_opaque_area_is_descended_into(self, tmp_path, area, layout):
        _plant(tmp_path, f"{area}/vendor/{layout}/job1/state.yaml")
        assert list(record.iter_run_areas(tmp_path)) == []
        assert _codes(_bare_report(tmp_path)) == []

    @pytest.mark.parametrize("area", ["_info", "_plans"])
    @pytest.mark.parametrize("layout", ["runs", "_runs"])
    def test_the_free_form_project_areas_are_not_descended_into(
        self, tmp_path, area, layout
    ):
        # !257 iteration 2: `_info` and `_plans` are free-form ("never
        # checked", SCHEMA.md Inventory) and were still being walked. A tier
        # that is never checked and a tier checked no further than its
        # existence mean the same thing to a record walk.
        _plant(tmp_path, f"widgets/{area}/notes/{layout}/x/state.yaml")
        _plant(tmp_path, "widgets/_runs/develop-1/20260824-101500-1-11111/state.yaml")
        report = _bare_report(tmp_path)
        assert _codes(report) == BROKEN_CODES, "only the real record is judged"
        assert all(f.path.startswith("widgets/_runs/") for f in report.findings)

    @pytest.mark.parametrize("layout", ["runs", "_runs"])
    def test_the_legacy_info_twin_is_not_descended_into(self, tmp_path, layout):
        # Appendix A maps `<project>/info/` to `_info/`; the legacy twin is
        # the same free-form tier, so the boundary has to know both spellings.
        _plant(tmp_path, f"git.example.org/acme/widgets/info/pages/{layout}/x/state.yaml")
        _plant(tmp_path, "git.example.org/acme/widgets/runs/develop-1/state.yaml")
        report = _bare_report(tmp_path)
        assert _codes(report) == BROKEN_CODES
        assert all("/info/" not in f.path for f in report.findings)

    @pytest.mark.parametrize("rel", ["_agents/mirrors/fork", "_agents/claude", "_taskdef"])
    def test_the_project_scope_opaque_areas_are_not_descended_into(self, schema_plexus, rel):
        # The reviewer's note: the same walk reaches `<project>/_taskdef/` and
        # `<project>/_agents/` by the same path.
        _plant(schema_plexus.project_dir, f"{rel}/runs/job1/state.yaml")
        assert _codes(_report(schema_plexus)) == []

    @pytest.mark.parametrize(
        "rel",
        [
            "mirrors/somefork/runs/job1/state.yaml",
            "agent-files/claude/runs/x/state.yaml",
            "knowledge/acme/runs/y/state.yaml",
            "napkin/acme/runs/y/state.yaml",
            "taskdef/vendor/runs/job1/state.yaml",
        ],
    )
    def test_the_bare_legacy_instance_surfaces_are_opaque_too(self, legacy_plexus, rel):
        # §9's bare twins of the same areas, at the one place they can BE one.
        _plant(legacy_plexus.root, rel)
        assert _codes(_report(legacy_plexus)) == []

    def test_the_bare_legacy_project_taskdef_is_opaque(self, legacy_plexus):
        # `<project>/taskdef/` is the project-scoped taskdef override: opaque,
        # and recognisable because its parent is project-shaped.
        _plant(legacy_plexus.project_dir, "taskdef/vendor/runs/job1/state.yaml")
        assert _codes(_report(legacy_plexus)) == []

    def test_a_bare_legacy_name_that_is_itself_a_project_is_still_checked(self, tmp_path):
        # `mirrors` is a legal project short name; a project-shaped directory
        # at the root is a project, whatever it is called.
        _plant(tmp_path, "mirrors/_runs/develop-1/20260824-101500-1-11111/state.yaml")
        assert _codes(_bare_report(tmp_path)) == BROKEN_CODES

    @pytest.mark.parametrize("component", ["mirrors", "taskdef", "napkin"])
    def test_a_bare_legacy_name_as_a_bucket_path_component_is_still_walked(
        self, tmp_path, component
    ):
        # `gitlab.example.com/mirrors/lmer` is one of the fixture's own
        # remotes: a bare name deep in a bucket is a namespace level, not an
        # area, so pruning it by name at any depth would lose a real project.
        _plant(
            tmp_path,
            f"gitlab.example.com/{component}/lmer/runs/develop-1/state.yaml",
        )
        assert _codes(_bare_report(tmp_path, form=Form.LEGACY)) == BROKEN_CODES

    def test_an_underscore_bucket_namespace_level_is_still_descended(self, tmp_path):
        # The prune is by schema-area NAME, not by the `_` prefix: a forge
        # group called `_ns` is a namespace level and its project is real.
        _plant(tmp_path, "gitlab.example.com/_ns/proj/_runs/develop-1/sid/state.yaml")
        assert _codes(_bare_report(tmp_path)) == BROKEN_CODES + [
            "record.session-id-malformed"
        ]


# ---------------------------------------------------------------------------
# The legacy `{task_type}/{target}/` tree (§7 scope-out, issue #379)
# ---------------------------------------------------------------------------


#: The pre-run-dir project the checks below are built around: the shape
#: `ensure_work_repo_directory` wrote before the run dir unified it
#: (`{host}/{project}/info/`, `{host}/{project}/{task_type}/info/`,
#: `{host}/{project}/{task_type}/{target}/`).
LEGACY_PROJECT = "git.example.org/acme/widgets"
TASK_TYPE = "develop"
TASK_TARGET = "mr-42"
TASK_TREE = f"{LEGACY_PROJECT}/{TASK_TYPE}/{TASK_TARGET}"


def _legacy_project(root):
    """Plant the pre-run-dir project skeleton and return its directory."""
    project = root / LEGACY_PROJECT
    (project / "info").mkdir(parents=True)
    (project / TASK_TYPE / "info").mkdir(parents=True)
    (root / TASK_TREE).mkdir(parents=True)
    return project


class TestTheLegacyTaskTree:
    """`{host}/{project}/{task_type}/{target}/` is OUT of scope (§7).

    SCHEMA.md gives the pre-run-dir layout one row and one promise —
    "read-side fallback only; informational" — and §7 lists it beside the
    napkin and the bundle internals as a scope-out. The walk finds run areas
    by NAME at any depth, so it descended into one anyway and reported every
    state file it found there: issue #379's reproduction is a tree whose
    ONLY findings come out of a scope-out, and `validate` exits 1 on it.

    The discriminator cannot be the name. `{task_type}` is an open set (any
    `LMER_TASK` value: `develop`, `review`, whatever a taskdef is called), so
    this boundary is drawn by WHERE a directory sits and by WHAT it holds: a
    `{task_type}/` sits directly inside a project, and its children are
    legacy TARGET dirs. "Inside a project" is answered positionally when the
    caller hands the layout walk's project list down (the runner's path,
    #379's remaining half) and by the §1 marker test where the list is silent
    — no list at all, or a nested project the list by its own rule never
    names; "a target" is answered by what the legacy writers left there,
    always, because that is the one thing separating a `{target}` from a
    nested project. Both halves are asserted here — the scope-out is silent,
    and the legacy `{host}/{project}/runs/` record the validator exists to
    check is not swallowed with it.
    """

    @pytest.mark.parametrize("layout", ["runs", "_runs"])
    def test_the_issue_379_reproduction_is_clean(self, tmp_path, layout):
        # The reproduction verbatim: a corrupt record inside the scope-out,
        # in either run-area spelling, on a tree with nothing else wrong.
        # The target carries `log.yaml` because every real one does — it is
        # what `work log` wrote there, and after !271's review it is the only
        # thing that marks a target (see LEGACY_TARGET_MARKERS).
        _legacy_project(tmp_path)
        (tmp_path / TASK_TREE / "log.yaml").write_text("- {}\n", encoding="utf-8")
        _plant(tmp_path, f"{TASK_TREE}/{layout}/chat-.--x/state.yaml")
        report = _bare_report(tmp_path, form=Form.LEGACY)
        assert _codes(report) == []
        assert report.exit_code == 0
        assert list(record.iter_run_areas(tmp_path)) == []

    def test_a_nested_project_is_not_read_as_a_task_tree(self, tmp_path):
        """!271 review: the prune dropped a real project's real run area.

        `runs` is a §1 PROJECT marker as well as a run area, so "holds a run
        area" was both what made a directory a `{target}` and what made it a
        project. A namespace directory whose children are projects therefore
        read as a `{task_type}`, and every record under it left validation
        without a word.

        A nested project must stay visible when its parent gains a marker.
        """
        project = tmp_path / "h/example/infra"
        (project / "info").mkdir(parents=True)
        (project / "runs").mkdir()
        _plant(tmp_path, "h/example/infra/roles/web/runs/deploy-1/state.yaml")

        report = _bare_report(tmp_path, form=Form.LEGACY)

        assert "record.state-enum" in _codes(report), (
            "a real project's corrupt record was silently dropped from "
            "validation because a namespace dir above it read as a task tree"
        )
        areas = {p.as_posix() for p, _ in record.iter_run_areas(tmp_path)}
        assert any(a.endswith("roles/web/runs") for a in areas), (
            f"the nested project's run area is not walked at all: {areas}"
        )

    def test_a_target_holding_only_a_run_area_is_left_alone(self, tmp_path):
        """The cost of the fix above, pinned so it is a decision and not a
        surprise: writer output is the only evidence this prune accepts, so a
        `{target}` carrying a run area and nothing else is walked.

        No shipped tool writes that shape — `work log` put `log.yaml` beside
        it in every real legacy target — and the trade is deliberate:
        under-pruning costs a scope-out being checked, over-pruning costs a
        real project's records never being checked at all, and only one of
        those is silent.

        #379's positional half looked at flipping this and did not. Handed
        the layout walk's project list the prune knows WHERE the project is,
        but the list — by its own rule, nothing below a project is a second
        project — never names anything below one, so it says nothing about a
        `{target}`; and `<project>/<x>/<y>/runs` with nothing else in `<y>`
        is byte-for-byte the nested-project shape above
        (`syseng/infra/ansible-roles/caddy/runs`). No position tells them
        apart, only the writer's output does, so this is walked on both paths
        and the pin now says so for both.
        """
        _legacy_project(tmp_path)
        _plant(tmp_path, f"{TASK_TREE}/runs/chat-.--x/state.yaml")

        report = _bare_report(tmp_path, form=Form.LEGACY)
        assert "record.state-enum" in _codes(report)

        # The same tree on the runner's path: the list names `widgets` (it is
        # project-shaped) and still says nothing about `mr-42`.
        projects = walk.project_dirs(tmp_path)
        assert {project.rel for project in projects} == {LEGACY_PROJECT}
        report = Report(Form.LEGACY)
        record.check_record(tmp_path, report, now=NOW, projects=projects)
        assert "record.state-enum" in _codes(report)

    def test_the_legacy_task_tree_prune_is_positional(self, tmp_path):
        """#379's remaining half: handed the layout walk's project list, the
        prune reads "directly inside a project" from the LIST, and falls back
        to the §1 marker test only where the list is silent.

        One tree, one leg per directory:

        - `bare/` is a registered project that has not grown an area yet. By
          shape it is nothing, so its legacy task tree is walked; by POSITION
          it is a project, so the same tree is a scope-out. This is the case
          the list closes that no marker test can.
        - `h/syseng/infra/ansible-roles/caddy/` is a nested project — the live
          shape, and the !271 review's tree. The walk's own rule never lists
          it, so the list says nothing about it: its run area is still found
          and the legacy task tree INSIDE it is still pruned, both by shape.
          The list adds a project; it never becomes a whitelist.
        - with no list at all the answer is today's, which
          `lmer_schema.migration._record_blockers` and the runner tests'
          negative control both depend on.

        What the list does NOT decide is the `{target}` level — see
        test_a_target_holding_only_a_run_area_is_left_alone.
        """
        bare = tmp_path / "bare"
        _plant(tmp_path, "bare/develop/issue-12/log.yaml", "- {}\n")
        _plant(tmp_path, "bare/develop/issue-12/runs/chat-.--x/state.yaml")
        infra = tmp_path / "h" / "syseng" / "infra"
        (infra / "info").mkdir(parents=True)
        (infra / "runs").mkdir()
        _plant(tmp_path, "h/syseng/infra/ansible-roles/caddy/runs/deploy-1/state.yaml")
        _plant(tmp_path, "h/syseng/infra/ansible-roles/caddy/develop/issue-1/log.yaml", "- {}\n")
        _plant(tmp_path, "h/syseng/infra/ansible-roles/caddy/develop/issue-1/runs/chat/state.yaml")

        def areas(**kwargs):
            return {
                path.relative_to(tmp_path).as_posix()
                for path, _layout in record.iter_run_areas(tmp_path, **kwargs)
            }

        # No list: `bare` is not project-shaped, so by shape alone its task
        # tree is nobody's scope-out and is walked. Today's answer.
        assert areas() == {
            "bare/develop/issue-12/runs",
            "h/syseng/infra/runs",
            "h/syseng/infra/ansible-roles/caddy/runs",
        }

        # The layout walk's list as the runner hands it down: `bare` by its
        # registry entry, `infra` by its shape, `caddy` never (nothing below
        # a project is a second project). Both spellings a caller may pass —
        # a ProjectDir and a plain path — are accepted.
        projects = (
            walk.ProjectDir(path=bare, rel="bare", name="bare", kind=walk.REGISTERED, form=None),
            infra,
        )
        assert areas(projects=projects) == {
            "h/syseng/infra/runs",
            "h/syseng/infra/ansible-roles/caddy/runs",
        }
        report = Report(Form.LEGACY)
        record.check_record(tmp_path, report, now=NOW, projects=projects)
        assert _codes(report) == BROKEN_CODES
        assert all(
            f.path.startswith("h/syseng/infra/ansible-roles/caddy/runs/")
            for f in report.findings
        )

    def test_the_projects_own_run_area_beside_it_is_still_checked(self, tmp_path):
        # The converse, and the reason this prune is written by shape: the
        # legacy run dir one level up is the record this validator is FOR.
        # The target carries the `log.yaml` every real one has — after !271's
        # review that is what marks it, and without it the task tree is left
        # alone (see test_a_target_holding_only_a_run_area_is_left_alone).
        _legacy_project(tmp_path)
        (tmp_path / TASK_TREE / "log.yaml").write_text("- {}\n", encoding="utf-8")
        _plant(tmp_path, f"{TASK_TREE}/runs/chat-.--x/state.yaml")
        _plant(tmp_path, f"{LEGACY_PROJECT}/runs/develop-1/state.yaml")
        report = _bare_report(tmp_path, form=Form.LEGACY)
        assert _codes(report) == BROKEN_CODES
        assert all(
            f.path.startswith(f"{LEGACY_PROJECT}/runs/") for f in report.findings
        )

    @pytest.mark.parametrize("marker", ["log.yaml", "reports/gate.md"])
    def test_a_target_is_recognised_by_what_the_legacy_writers_left(
        self, tmp_path, marker
    ):
        # `work log` and `work report` are all that ever wrote into a target
        # dir (RUN-STATE.md "Legacy layout fallback"), so their artifacts are
        # what makes a directory a target — the run area under the scope-out
        # here sits a level below one, and is reached if the marker is missed.
        _legacy_project(tmp_path)
        _plant(tmp_path, f"{TASK_TREE}/{marker}", "- entry\n")
        _plant(tmp_path, f"{TASK_TREE}/notes/runs/chat/state.yaml")
        assert _codes(_bare_report(tmp_path, form=Form.LEGACY)) == []

    def test_a_task_tree_left_inside_a_schema_form_project_is_pruned_too(
        self, schema_plexus
    ):
        # Migration leaves the old dirs where they were (the CLI never moves
        # legacy files, RUN-STATE.md §6), so the scope-out outlives the form.
        _plant(schema_plexus.project_dir, f"{TASK_TYPE}/{TASK_TARGET}/log.yaml", "- e\n")
        _plant(
            schema_plexus.project_dir,
            f"{TASK_TYPE}/{TASK_TARGET}/runs/chat/state.yaml",
        )
        assert _codes(_report(schema_plexus)) == []

    def test_the_shape_is_two_levels_deep_not_one(self, tmp_path):
        # Guard against a prune that fires on "any directory inside a
        # project": a run area sitting DIRECTLY in one has no target dir
        # between it and the project, is not the legacy layout, and is
        # checked exactly as before.
        _legacy_project(tmp_path)
        _plant(tmp_path, f"{LEGACY_PROJECT}/vendored/_runs/develop-1/sid/state.yaml")
        assert _codes(_bare_report(tmp_path)) == BROKEN_CODES + [
            "record.session-id-malformed"
        ]

    def test_a_project_at_the_root_is_never_a_task_type_candidate(self, tmp_path):
        # A schema-form root holds `_agents/` and `_taskdef/`, so the §1
        # marker test calls the ROOT project-shaped and every project in it
        # would otherwise sit where a `{task_type}` sits. One stray file named
        # like a legacy artifact — beside a checkout, say — would then prune a
        # whole project and its record away. No task tree lives at the root
        # anyway: the layout starts at `{host}/{project}/`.
        (tmp_path / "_agents").mkdir()
        (tmp_path / "_taskdef").mkdir()
        _plant(tmp_path, "widgets/checkout/log.yaml", "- entry\n")
        _plant(tmp_path, "widgets/_runs/develop-1/20260824-101500-1-11111/state.yaml")
        report = _bare_report(tmp_path)
        assert _codes(report) == BROKEN_CODES
        assert all(f.path.startswith("widgets/_runs/") for f in report.findings)

    def test_a_free_form_area_is_never_read_as_a_target(self, tmp_path):
        # The sharpest over-prune: `_info/` is already pruned BY NAME, so a
        # `runs`-shaped directory inside one must never be read as a target —
        # that would make the directory holding it a task tree and prune away
        # the record beside it (the walk has no depth rule: a `_runs/` below a
        # project-shaped directory is a record wherever it sits).
        (tmp_path / "outer" / "_runs").mkdir(parents=True)
        _plant(tmp_path, "outer/inner/_info/runs/x/state.yaml")
        _plant(
            tmp_path, "outer/inner/_runs/develop-1/20260824-101500-1-11111/state.yaml"
        )
        report = _bare_report(tmp_path)
        assert _codes(report) == BROKEN_CODES
        assert all(f.path.startswith("outer/inner/_runs/") for f in report.findings)


class TestTheAreasThatMustNotChange:
    """The two cases the review calls correct today: schema `<project>/_runs/`
    and legacy `{host}/{project}/runs/`. The boundary is above them."""

    def test_the_schema_project_runs_area_is_still_found_and_checked(self, schema_plexus):
        rel = schema_plexus.runs_dir.relative_to(schema_plexus.root).as_posix()
        assert (schema_plexus.runs_dir, record.SESSION_LAYOUT) in list(
            record.iter_run_areas(schema_plexus.root)
        )
        (_session_dir(schema_plexus) / "state.yaml").write_text(
            BROKEN_STATE, encoding="utf-8"
        )
        report = _report(schema_plexus)
        assert _codes(report) == BROKEN_CODES
        assert report.findings[0].path.startswith(f"{rel}/")

    def test_the_legacy_host_project_runs_area_is_still_found_and_checked(
        self, legacy_plexus
    ):
        assert (legacy_plexus.runs_dir, record.LEGACY_LAYOUT) in list(
            record.iter_run_areas(legacy_plexus.root)
        )
        (_session_dir(legacy_plexus) / "state.yaml").write_text(
            BROKEN_STATE, encoding="utf-8"
        )
        report = _report(legacy_plexus)
        assert _codes(report) == BROKEN_CODES
        assert report.exit_code == 1

    def test_the_schema_host_bucket_runs_area_is_still_found(self, schema_plexus):
        # A bucket inside a schema-form tree keeps the session-unit checks.
        assert (schema_plexus.bucket_dir / "_runs", record.SESSION_LAYOUT) in list(
            record.iter_run_areas(schema_plexus.root)
        )


# ---------------------------------------------------------------------------
# state.yaml
# ---------------------------------------------------------------------------


class TestStateFile:
    def _state(self, tree, text, *, name="state.yaml"):
        path = _session_dir(tree) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_unparseable_yaml_is_an_error(self, schema_plexus):
        self._state(schema_plexus, "status: [in-progress\n")
        report = _report(schema_plexus)
        assert _codes(report) == ["record.state-unparseable"]
        assert report.findings[0].severity is Severity.ERROR
        assert report.findings[0].path.endswith("/state.yaml")

    def test_a_non_mapping_document_is_an_error(self, schema_plexus):
        self._state(schema_plexus, "- one\n- two\n")
        assert _codes(_report(schema_plexus)) == ["record.state-unparseable"]

    def test_an_unreadable_state_file_is_an_error(self, schema_plexus):
        path = _session_dir(schema_plexus) / "state.yaml"
        path.unlink()
        path.symlink_to("gone.yaml")
        assert _codes(_report(schema_plexus)) == ["record.state-unparseable"]

    @pytest.mark.parametrize("value", ["true", "'1'", "1.0", "[]"])
    def test_a_schema_that_is_not_an_integer_is_an_error(self, schema_plexus, value):
        # `schema: true` is the bool trap: isinstance(True, int) is True.
        self._state(schema_plexus, f"schema: {value}\nslug: s\nstatus: complete\n")
        assert _codes(_report(schema_plexus)) == ["record.state-schema"]

    def test_an_absent_schema_key_is_tolerated(self, schema_plexus):
        # The writer reads a missing `schema:` as 0 (RUN-STATE.md's additive
        # promise); the validator is no stricter than the single writer.
        self._state(schema_plexus, "slug: s\nstatus: complete\n")
        assert _codes(_report(schema_plexus)) == []

    def test_a_status_outside_the_closed_enum_is_an_error(self, schema_plexus):
        self._state(schema_plexus, "schema: 1\nstatus: finished\n")
        report = _report(schema_plexus)
        assert _codes(report) == ["record.state-enum"]
        assert "finished" in report.findings[0].message

    @pytest.mark.parametrize("status", run_state.STATUSES)
    def test_every_documented_status_is_accepted(self, schema_plexus, status):
        self._state(schema_plexus, f"schema: 1\nstatus: {status}\n")
        assert _codes(_report(schema_plexus)) == []

    def test_stop_reason_is_not_a_closed_enum(self, schema_plexus):
        # RUN-STATE.md extends stop_reason additively ("`status` stays the
        # closed three-value enum"), so an unknown one is not a finding.
        self._state(schema_plexus, "schema: 1\nstatus: complete\nstop_reason: parked\n")
        assert _codes(_report(schema_plexus)) == []

    def test_the_legacy_state_yml_is_read_when_state_yaml_is_absent(self, schema_plexus):
        (_session_dir(schema_plexus) / "state.yaml").unlink()
        self._state(schema_plexus, "schema: 1\nstatus: nope\n", name="state.yml")
        assert _codes(_report(schema_plexus)) == ["record.state-enum"]

    def test_a_migrated_leftover_is_out_of_the_resolution_path(self, schema_plexus):
        self._state(schema_plexus, "{{{\n", name="state.yml.migrated")
        assert _codes(_report(schema_plexus)) == []


class TestLedger:
    def _ledger(self, tree, text):
        (_session_dir(tree) / "ledger.yaml").write_text(text, encoding="utf-8")

    def test_a_task_status_outside_the_enum_is_an_error(self, schema_plexus):
        self._ledger(
            schema_plexus,
            "schema: 1\ntasks:\n  T1:\n    title: t\n    status: finished\n",
        )
        report = _report(schema_plexus)
        assert _codes(report) == ["record.state-enum"]
        assert "T1" in report.findings[0].message
        assert report.findings[0].path.endswith("/ledger.yaml")

    @pytest.mark.parametrize("status", run_state.TASK_STATUSES)
    def test_every_documented_task_status_is_accepted(self, schema_plexus, status):
        self._ledger(schema_plexus, f"schema: 1\ntasks:\n  T1:\n    status: {status}\n")
        assert _codes(_report(schema_plexus)) == []

    def test_a_non_mapping_tasks_section_is_corrupt(self, schema_plexus):
        self._ledger(schema_plexus, "schema: 1\ntasks:\n  - T1\n")
        assert _codes(_report(schema_plexus)) == ["record.state-unparseable"]

    def test_a_non_mapping_row_is_corrupt(self, schema_plexus):
        self._ledger(schema_plexus, "schema: 1\ntasks:\n  T1: done\n")
        assert _codes(_report(schema_plexus)) == ["record.state-unparseable"]

    def test_a_bool_schema_is_an_error(self, schema_plexus):
        self._ledger(schema_plexus, "schema: yes\ntasks: {}\n")
        assert _codes(_report(schema_plexus)) == ["record.state-schema"]

    def test_an_empty_ledger_is_fine(self, schema_plexus):
        self._ledger(schema_plexus, "schema: 1\ntasks: {}\n")
        assert _codes(_report(schema_plexus)) == []


class TestPlanIndex:
    def _index(self, tree, text):
        (_session_dir(tree) / "plan.index.json").write_text(text, encoding="utf-8")

    def test_a_valid_index_is_clean(self, schema_plexus):
        self._index(
            schema_plexus,
            json.dumps({"schema": 1, "tasks": [{"id": "T1", "description": "d"}]}),
        )
        assert _codes(_report(schema_plexus)) == []

    def test_invalid_json_is_an_error(self, schema_plexus):
        self._index(schema_plexus, '{"schema": 1,}')
        report = _report(schema_plexus)
        assert _codes(report) == ["record.state-unparseable"]
        assert report.findings[0].path.endswith("/plan.index.json")

    def test_a_non_object_document_is_an_error(self, schema_plexus):
        self._index(schema_plexus, "[]")
        assert _codes(_report(schema_plexus)) == ["record.state-unparseable"]

    def test_a_missing_schema_is_an_error_here(self, schema_plexus):
        # Unlike the YAML state files, `work plan check` REQUIRES the int.
        self._index(schema_plexus, json.dumps({"tasks": []}))
        assert _codes(_report(schema_plexus)) == ["record.state-schema"]

    def test_a_bool_schema_is_an_error(self, schema_plexus):
        self._index(schema_plexus, json.dumps({"schema": True, "tasks": []}))
        assert _codes(_report(schema_plexus)) == ["record.state-schema"]


# ---------------------------------------------------------------------------
# events.jsonl
# ---------------------------------------------------------------------------


class TestEvents:
    def _events(self, tree, text):
        path = _session_dir(tree) / "events.jsonl"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_bad_line_is_an_error_naming_its_line_number(self, schema_plexus):
        self._events(
            schema_plexus,
            '{"ts": "t", "type": "session_start"}\nnot json at all\n',
        )
        report = _report(schema_plexus)
        assert _codes(report) == ["record.events-line-unparseable"]
        finding = report.findings[0]
        assert finding.severity is Severity.ERROR
        assert finding.path.endswith("/events.jsonl")
        assert "line 2" in finding.message

    def test_a_line_that_is_not_an_object_is_an_error(self, schema_plexus):
        self._events(schema_plexus, '[1, 2]\n"a string"\n')
        assert _codes(_report(schema_plexus)) == [
            "record.events-line-unparseable",
            "record.events-line-unparseable",
        ]

    def test_a_bad_line_never_aborts_the_walk(self, schema_plexus):
        self._events(schema_plexus, 'oops\n{"ts": "t"}\nalso bad\n')
        report = _report(schema_plexus)
        assert [f.message.split(":")[0] for f in _at(report, "record.events-line-unparseable")] == [
            "line 1",
            "line 3",
        ]

    def test_a_truncated_trailing_line_is_reported_precisely(self, schema_plexus):
        # A crash mid-append: the last line has no newline and no closing
        # brace. Report it as what it is, keep the earlier lines' verdict.
        self._events(schema_plexus, '{"ts": "t", "type": "phase"}\n{"ts": "t2"')
        report = _report(schema_plexus)
        assert _codes(report) == ["record.events-line-unparseable"]
        finding = report.findings[0]
        assert "line 2" in finding.message
        assert "truncated" in finding.message

    def test_blank_lines_are_tolerated(self, schema_plexus):
        self._events(schema_plexus, '{"ts": "t"}\n\n   \n{"ts": "t2"}\n')
        assert _codes(_report(schema_plexus)) == []

    def test_an_undecodable_line_is_reported_as_that_line(self, schema_plexus):
        path = _session_dir(schema_plexus) / "events.jsonl"
        path.write_bytes(b'{"ts": "t"}\n\xff\xfe not utf-8\n')
        report = _report(schema_plexus)
        assert _codes(report) == ["record.events-line-unparseable"]
        assert "line 2" in report.findings[0].message

    def test_an_absent_events_file_is_optional(self, schema_plexus):
        (_session_dir(schema_plexus) / "events.jsonl").unlink()
        assert _codes(_report(schema_plexus)) == []


# ---------------------------------------------------------------------------
# The session unit (D22)
# ---------------------------------------------------------------------------


class TestSessionUnitShape:
    def test_a_file_at_the_grouping_level_is_a_warning(self, schema_plexus):
        (_group_dir(schema_plexus) / "state.yaml").write_text("schema: 1\n", encoding="utf-8")
        report = _report(schema_plexus)
        assert _codes(report) == ["record.grouping-level-entry"]
        assert report.findings[0].severity is Severity.WARN

    def test_a_file_directly_under_runs_is_a_warning(self, schema_plexus):
        (schema_plexus.runs_dir / "notes.md").write_text("x", encoding="utf-8")
        assert _codes(_report(schema_plexus)) == ["record.grouping-level-entry"]

    def test_a_non_session_directory_at_the_grouping_level_is_a_warning(self, schema_plexus):
        (_group_dir(schema_plexus) / "scratch").mkdir()
        report = _report(schema_plexus)
        assert _codes(report) == ["record.grouping-level-entry"]
        assert report.findings[0].path.endswith("/scratch")

    def test_a_session_dir_without_state_is_a_warning(self, schema_plexus):
        (_group_dir(schema_plexus) / "20260824-120000-9-99999").mkdir()
        assert _codes(_report(schema_plexus)) == ["record.session-dir-without-state"]

    def test_a_session_id_out_of_form_is_a_warning_when_it_holds_a_record(self, schema_plexus):
        odd = _group_dir(schema_plexus) / "yesterdays-run"
        odd.mkdir()
        (odd / "state.yaml").write_text("schema: 1\nstatus: complete\n", encoding="utf-8")
        report = _report(schema_plexus)
        assert _codes(report) == ["record.session-id-malformed"]
        assert report.findings[0].path.endswith("/yesterdays-run")

    def test_a_named_session_dir_is_in_form(self, schema_plexus):
        # The fixture's second session carries the `--<name>` suffix a freeze
        # rename adds; it must not read as malformed.
        assert schema_plexus.session_dirs[1].name.endswith("--second-look")
        assert _codes(_report(schema_plexus)) == []

    def test_the_record_inside_a_malformed_session_dir_is_still_checked(self, schema_plexus):
        odd = _group_dir(schema_plexus) / "yesterdays-run"
        odd.mkdir()
        (odd / "state.yaml").write_text("schema: 1\nstatus: nope\n", encoding="utf-8")
        assert _codes(_report(schema_plexus)) == [
            "record.state-enum",
            "record.session-id-malformed",
        ]

    def test_a_leftover_seed_dir_is_a_warning(self, schema_plexus):
        seed = schema_plexus.runs_dir / ".new-20260824-120000-9-99999-abcd"
        seed.mkdir()
        (seed / "state.yaml").write_text("schema: 1\nstatus: complete\n", encoding="utf-8")
        report = _report(schema_plexus)
        assert _codes(report) == ["record.seed-dir-leftover"]
        assert report.findings[0].severity is Severity.WARN

    def test_other_dot_entries_under_runs_say_nothing(self, schema_plexus):
        (schema_plexus.runs_dir / ".keep").write_text("", encoding="utf-8")
        (schema_plexus.runs_dir / ".scratch").mkdir()
        assert _codes(_report(schema_plexus)) == []

    def test_dot_entries_at_the_grouping_level_say_nothing(self, schema_plexus):
        (_group_dir(schema_plexus) / ".DS_Store").write_text("", encoding="utf-8")
        assert _codes(_report(schema_plexus)) == []


class TestStaleness:
    def _in_progress(self, tree, updated):
        path = _session_dir(tree) / "state.yaml"
        path.write_text(
            f"schema: 1\nslug: {tree.slug}\nstatus: in-progress\nupdated: '{updated}'\n",
            encoding="utf-8",
        )

    def test_an_old_in_progress_session_is_possibly_orphaned(self, schema_plexus):
        self._in_progress(schema_plexus, "2026-07-01T10:00:00Z")
        report = _report(schema_plexus)
        assert _codes(report) == ["record.session-stale"]
        finding = report.findings[0]
        assert finding.severity is Severity.INFO
        assert finding.path == f"lmer/_runs/{schema_plexus.slug}/{_session_dir(schema_plexus).name}"
        assert "orphan" in finding.message

    def test_a_fresh_in_progress_session_is_not_stale(self, schema_plexus):
        self._in_progress(schema_plexus, "2026-08-24T11:00:00Z")
        assert _codes(_report(schema_plexus)) == []

    def test_a_finished_run_is_never_stale(self, schema_plexus):
        path = _session_dir(schema_plexus) / "state.yaml"
        path.write_text(
            "schema: 1\nstatus: complete\nupdated: '2020-01-01T00:00:00Z'\n",
            encoding="utf-8",
        )
        assert _codes(_report(schema_plexus)) == []

    def test_the_window_is_a_parameter(self, schema_plexus):
        self._in_progress(schema_plexus, "2026-08-23T10:00:00Z")
        assert _codes(_report(schema_plexus)) == []
        report = Report(Form.SCHEMA)
        record.check_record(
            schema_plexus.root, report, now=NOW, stale_after=timedelta(hours=1).total_seconds()
        )
        assert _codes(report) == ["record.session-stale"]

    def test_an_unparseable_updated_stamp_is_not_a_staleness_finding(self, schema_plexus):
        self._in_progress(schema_plexus, "whenever")
        assert _codes(_report(schema_plexus)) == []

    def test_a_naive_stamp_reads_as_utc(self, schema_plexus):
        # PyYAML parses an unquoted timestamp into a datetime; RUN-STATE.md
        # stamps are UTC whether or not the offset was written.
        path = _session_dir(schema_plexus) / "state.yaml"
        path.write_text(
            "schema: 1\nstatus: in-progress\nupdated: 2026-07-01 10:00:00\n",
            encoding="utf-8",
        )
        assert _codes(_report(schema_plexus)) == ["record.session-stale"]

    def test_staleness_is_not_reported_for_a_legacy_shared_run_dir(self, legacy_plexus):
        # The legacy dir interleaves sessions and is never resumed as a unit;
        # D22's orphan notion is the session dir's.
        path = _session_dir(legacy_plexus) / "state.yaml"
        text = path.read_text(encoding="utf-8").replace(
            "updated: '2026-08-24T11:32:00Z'", "updated: '2020-01-01T00:00:00Z'"
        )
        path.write_text(text, encoding="utf-8")
        assert _codes(_report(legacy_plexus)) == []


# ---------------------------------------------------------------------------
# The legacy layout: read in place, shapes still checked
# ---------------------------------------------------------------------------


class TestLegacyLayout:
    def test_a_corrupt_state_file_is_still_an_error(self, legacy_plexus):
        (_session_dir(legacy_plexus) / "state.yaml").write_text("{{{\n", encoding="utf-8")
        report = _report(legacy_plexus)
        assert _codes(report) == ["record.state-unparseable"]
        assert report.findings[0].severity is Severity.ERROR
        assert report.exit_code == 1

    def test_the_shared_dir_is_never_nudged_into_session_shape(self, legacy_plexus):
        # `runs/<slug>/` IS the run dir: no grouping level, no session id.
        assert _codes(_report(legacy_plexus)) == []

    def test_a_dir_without_a_state_file_is_not_a_run_dir(self, legacy_plexus):
        (legacy_plexus.runs_dir / "leftovers").mkdir()
        assert _codes(_report(legacy_plexus)) == []

    def test_the_legacy_archive_home_is_never_re_validated(self, legacy_plexus):
        archived = legacy_plexus.runs_dir / "archive" / "old-run"
        archived.mkdir(parents=True)
        (archived / "state.yaml").write_text("- broken\n", encoding="utf-8")
        assert _codes(_report(legacy_plexus)) == []

    def test_a_leftover_seed_dir_is_reported_in_the_legacy_layout_too(self, legacy_plexus):
        (legacy_plexus.runs_dir / ".new-20260824-120000-9-99999-abcd").mkdir()
        assert _codes(_report(legacy_plexus)) == ["record.seed-dir-leftover"]


class TestSeverityNeverSoftens:
    """D14: a corrupt state file is broken wherever it lives."""

    DEFECTS = {
        "record.state-unparseable": ("state.yaml", "{{{\n"),
        "record.state-schema": ("state.yaml", "schema: 'one'\nstatus: complete\n"),
        "record.state-enum": ("state.yaml", "schema: 1\nstatus: nope\n"),
        "record.events-line-unparseable": ("events.jsonl", "nope\n"),
    }

    @pytest.mark.parametrize("code", sorted(DEFECTS))
    @pytest.mark.parametrize("form", [Form.SCHEMA, Form.LEGACY])
    def test_a_shape_defect_is_an_error_in_either_form(self, schema_plexus, code, form):
        name, text = self.DEFECTS[code]
        (_session_dir(schema_plexus) / name).write_text(text, encoding="utf-8")
        report = _report(schema_plexus, form=form)
        assert _codes(report) == [code]
        assert report.findings[0].severity is Severity.ERROR
        assert report.exit_code == 1

    def test_the_same_defect_in_a_legacy_tree_is_the_same_error(self, legacy_plexus):
        (_session_dir(legacy_plexus) / "events.jsonl").write_text("nope\n", encoding="utf-8")
        report = _report(legacy_plexus)
        assert report.form is Form.LEGACY
        assert _codes(report) == ["record.events-line-unparseable"]
        assert report.findings[0].severity is Severity.ERROR


# ---------------------------------------------------------------------------
# Masterplan bundles (D12)
# ---------------------------------------------------------------------------


class TestMasterplanOpacity:
    def test_a_flat_bundle_is_accepted_and_never_walked(self, schema_plexus):
        bundle = _session_dir(schema_plexus) / "masterplan"
        bundle.mkdir()
        (bundle / "spec.md").write_text("# spec\n", encoding="utf-8")
        # Bundle internals belong to a foreign writer: a state file inside is
        # not this validator's to judge.
        (bundle / "state.yaml").write_text("- broken\n", encoding="utf-8")
        (bundle / "events.jsonl").write_text("nope\n", encoding="utf-8")
        assert _codes(_report(schema_plexus)) == []

    def test_the_pre_d12_nested_shape_is_reported_not_errored(self, schema_plexus):
        nested = _session_dir(schema_plexus) / "masterplan" / "develop-run-naming"
        nested.mkdir(parents=True)
        (nested / "plan.md").write_text("# plan\n", encoding="utf-8")
        report = _report(schema_plexus)
        assert _codes(report) == ["record.masterplan-nested"]
        finding = report.findings[0]
        assert finding.severity is Severity.INFO
        assert finding.path.endswith("/masterplan")
        assert "develop-run-naming" in (finding.detail or "")
        assert report.exit_code == 0

    def test_the_legacy_nested_shape_is_reported_in_a_legacy_tree(self, legacy_plexus):
        nested = _session_dir(legacy_plexus) / "masterplan" / "develop-run-naming"
        nested.mkdir(parents=True)
        (nested / "retro.md").write_text("# retro\n", encoding="utf-8")
        report = _report(legacy_plexus)
        assert _codes(report) == ["record.masterplan-nested"]
        assert report.findings[0].severity is Severity.INFO

    def test_a_bundle_dir_with_no_well_known_artifact_says_nothing(self, schema_plexus):
        # Opacity: a subdirectory alone is not evidence of the nested shape.
        working = _session_dir(schema_plexus) / "masterplan" / "handoff"
        working.mkdir(parents=True)
        (working / "notes.md").write_text("x", encoding="utf-8")
        assert _codes(_report(schema_plexus)) == []


# ---------------------------------------------------------------------------
# The session-dir artifact links (SCHEMA.md "Session directory": the
# `<session-dir>/<artifact>.md -> masterplan/<artifact>.md` row)
# ---------------------------------------------------------------------------


#: The row's three findings. They are the INDEX mechanic's codes, registered
#: once in the D14 table and consumed here — the same promise about the same
#: kind of link, so a near-duplicate `record.*` code would split one contract
#: in two (!261 discussion 1).
DANGLING = "index.dangling-link"
NOT_RELATIVE = "index.link-not-relative"
ESCAPES = "index.link-escapes"


def _artifact_link(rdir, name, target):
    """The record-root link `work artifact --sync` writes, spelled *target*.

    Whatever is at the name already is removed first: the fixture's session
    dir carries a REAL `spec.md`, and the sync replaces such a file with the
    link (the bundle copy is canonical).
    """
    path = rdir / name
    if path.is_symlink() or path.exists():
        path.unlink()
    path.symlink_to(target)
    return path


def _bundle(rdir, *names):
    """A flat D12 bundle inside *rdir* holding *names*."""
    bundle = rdir / record.MASTERPLAN_DIR
    bundle.mkdir(exist_ok=True)
    for name in names:
        (bundle / name).write_text(f"# {name}\n", encoding="utf-8")
    return bundle


class TestSessionArtifactLinks:
    """SCHEMA.md's row: *"relative symlink for each well-known bundle
    artifact ...; dangling is an error"*, tier `machine`.

    The row's writer is `run_state.sync_masterplan_artifacts` (T26); until
    !261 discussion 1 nothing read the links back, in any form — the three
    rules existed only over `_specs/`/`_plans/`. This module owns the walk of
    the record area, so this module asks them here too, through the shared
    rule in `lmer_schema.checks.symlinks`.
    """

    @pytest.mark.parametrize("name", record.MASTERPLAN_ARTIFACTS)
    def test_every_well_known_artifact_link_is_checked(self, schema_plexus, name):
        rdir = schema_plexus.session_dirs[1]
        _artifact_link(rdir, name, f"masterplan/{name}")
        report = _report(schema_plexus)
        assert _codes(report) == [DANGLING]
        finding = report.findings[0]
        assert finding.severity is Severity.ERROR
        assert finding.path == f"{rdir.relative_to(schema_plexus.root).as_posix()}/{name}"
        assert finding.detail == f"target: masterplan/{name}"
        assert report.exit_code == 1

    def test_a_link_that_resolves_into_the_bundle_says_nothing(self, schema_plexus):
        rdir = schema_plexus.session_dirs[1]
        _bundle(rdir, *record.MASTERPLAN_ARTIFACTS)
        for name in record.MASTERPLAN_ARTIFACTS:
            _artifact_link(rdir, name, f"{record.MASTERPLAN_DIR}/{name}")
        assert _codes(_report(schema_plexus)) == []

    def test_an_absolute_target_is_a_shape_violation(self, schema_plexus):
        rdir = schema_plexus.session_dirs[1]
        bundle = _bundle(rdir, "plan.md")
        _artifact_link(rdir, "plan.md", bundle / "plan.md")
        report = _report(schema_plexus)
        assert _codes(report) == [NOT_RELATIVE]
        assert report.findings[0].severity is Severity.ERROR

    def test_an_escaping_target_is_reported_once_and_never_as_dangling(
        self, schema_plexus
    ):
        rdir = schema_plexus.session_dirs[1]
        depth = len(rdir.relative_to(schema_plexus.root).parts)
        _artifact_link(rdir, "retro.md", "../" * (depth + 1) + "elsewhere/retro.md")
        report = _report(schema_plexus)
        assert _codes(report) == [ESCAPES]
        assert report.findings[0].severity is Severity.ERROR

    def test_a_real_file_at_an_artifact_name_is_free_form_content(self, schema_plexus):
        # SCHEMA.md gives `<session-dir>/spec.md` two legal shapes: free-form
        # content, or the symlink this row owns. The fixture's first session
        # carries the content form.
        assert (schema_plexus.session_dirs[0] / "spec.md").is_file()
        assert _codes(_report(schema_plexus)) == []

    @pytest.mark.parametrize("name", ["followups.md", "notes.md", "roadmap-spec.md"])
    def test_a_name_the_row_does_not_list_is_not_this_check(self, schema_plexus, name):
        # The row names five artifacts. Anything else at the record root —
        # another registered artifact, a legacy multi-bundle `<mp-slug>-`
        # prefixed link — is not the well-known set and is not checked here.
        _artifact_link(schema_plexus.session_dirs[1], name, "masterplan/nowhere.md")
        assert _codes(_report(schema_plexus)) == []

    def test_a_broken_link_inside_the_bundle_is_still_opaque(self, schema_plexus):
        # D12: the bundle's internals belong to a foreign writer. The row is
        # about the links at the record ROOT, not about the bundle.
        bundle = _bundle(schema_plexus.session_dirs[1], "spec.md")
        (bundle / "plan.md").symlink_to("/nowhere/plan.md")
        assert _codes(_report(schema_plexus)) == []


class TestArtifactLinksInALegacyRunDir:
    """The links exist in a legacy tree too — the same tooling writes them
    into `runs/<slug>/` — so the same three questions are asked there, at the
    tier D14 gives them: a dangling link is the integrity finding a legacy
    tree softens to a WARN ("the list of things to fix before adopting the
    schema, not a gate"), while a link that is absolute or escaping is a
    SHAPE violation of the link mechanic and stays an ERROR in every form.
    """

    def test_a_dangling_link_is_a_warning_that_never_blocks(self, legacy_plexus):
        rdir = _session_dir(legacy_plexus)
        _artifact_link(rdir, "spec.md", "masterplan/spec.md")
        report = _report(legacy_plexus)
        assert _codes(report) == [DANGLING]
        assert report.findings[0].severity is Severity.WARN
        assert report.exit_code == 0

    def test_the_same_link_in_a_schema_tree_is_an_error(self, schema_plexus):
        # The pair that makes the softening visible: one defect, two forms.
        _artifact_link(schema_plexus.session_dirs[1], "spec.md", "masterplan/spec.md")
        report = _report(schema_plexus)
        assert _codes(report) == [DANGLING]
        assert report.findings[0].severity is Severity.ERROR

    @pytest.mark.parametrize("code", [NOT_RELATIVE, ESCAPES])
    def test_a_shape_violation_still_blocks_a_legacy_tree(self, legacy_plexus, code):
        rdir = _session_dir(legacy_plexus)
        depth = len(rdir.relative_to(legacy_plexus.root).parts)
        target = (
            str(legacy_plexus.root / "elsewhere" / "plan.md")
            if code == NOT_RELATIVE
            else "../" * (depth + 1) + "elsewhere/plan.md"
        )
        _artifact_link(rdir, "plan.md", target)
        report = _report(legacy_plexus)
        assert _codes(report) == [code]
        assert report.findings[0].severity is Severity.ERROR
        assert report.exit_code == 1

    def test_a_healthy_legacy_link_says_nothing(self, legacy_plexus):
        rdir = _session_dir(legacy_plexus)
        _bundle(rdir, "spec.md")
        _artifact_link(rdir, "spec.md", f"{record.MASTERPLAN_DIR}/spec.md")
        assert _codes(_report(legacy_plexus)) == []


class TestTheReviewersDemonstration:
    """!261 discussion 1, reproduced: a minimal schema-form plexus whose
    session dir holds one flat bundle and three broken artifact links — a
    dangling `spec.md`, an absolute `plan.md` and an escaping `retro.md`.

    At `ba4cbd2` that tree validated *exit 0, zero findings* while the same
    three links inside `_specs/` were errors; the control lives in
    ``tests/test_lmer_schema_checks_index_links.py``.
    """

    SESSION = "widgets/_runs/develop-1/20260824-101500-1-11111"

    def _tree(self, root):
        _plant(root, f"{self.SESSION}/state.yaml", "schema: 1\nstatus: complete\n")
        session = root / self.SESSION
        _bundle(session, "goals.md")
        _artifact_link(session, "spec.md", "masterplan/spec.md")
        # The reviewer spelled the last two `/etc/hostname` and
        # `../../../../../../etc/hostname`; the fault is the shape, so the
        # fixture convention (nothing outside the tmp tree) keeps it here.
        _artifact_link(session, "plan.md", str(root / "elsewhere" / "plan.md"))
        _artifact_link(session, "retro.md", "../" * 6 + "elsewhere/retro.md")
        return session

    def test_the_three_links_are_now_three_findings(self, tmp_path):
        self._tree(tmp_path)
        report = _bare_report(tmp_path)
        assert sorted(_codes(report)) == sorted([DANGLING, NOT_RELATIVE, ESCAPES])
        assert [f.path for f in report.findings] == [
            f"{self.SESSION}/plan.md",
            f"{self.SESSION}/retro.md",
            f"{self.SESSION}/spec.md",
        ]
        assert report.exit_code == 1

    def test_the_bundle_beside_them_is_still_untouched(self, tmp_path):
        # Nothing else about the tree changed: the flat bundle is accepted,
        # the state file is fine, and only the links are reported.
        session = self._tree(tmp_path)
        (session / record.MASTERPLAN_DIR / "state.yaml").write_text(
            "- broken\n", encoding="utf-8"
        )
        assert len(_bare_report(tmp_path)) == 3


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_module_exports_its_surface():
    for name in record.__all__:
        assert hasattr(record, name), name
    assert "check_record" in record.__all__
    assert "iter_run_areas" in record.__all__
