"""``lmer_schema.checks.runner`` — the composing validation core (spec §7,
D14/D20/D22; SCHEMA.md "Validator"; issue #353).

The runner is the only file that names the six check modules, so these tests
pin the composition rather than any check's own rules (each module's tests own
those):

- the ENTRY-POINT convention: every check module is reached through one
  adapter of one shape, and every module in the package is wired into the one
  declared order — a seventh check module that nobody adds here fails a test
  rather than silently never running;
- the SHORT-CIRCUIT rules: an unusable manifest (exit 2) suppresses every
  downstream check instead of running them against an empty registry, while a
  registry that does not build is ONE error finding (exit 1) and the rest of
  the run continues registry-free;
- WALK ONCE: the layout check's ``ProjectDir`` list is what the per-project
  modules are handed, so nothing re-loads the registry or re-discovers the
  projects;
- the FORM-OVERRIDE trap: a host bucket's contents are checked at the legacy
  tier's severities even inside a schema-form tree. The project-list modules
  carry that with the list (each ``ProjectDir`` states the form its contents
  are checked at, !256 discussion 5); ``record`` walks by NAME and is handed
  no list, so the runner pins ITS tier by path;
- CRASH SAFETY: a check that raises becomes one error finding attributed to
  that check and the remaining checks still run, and the exception's text is
  redacted of the tree's own path — of the spellings that CARRY one, which a
  relative root spelling does not;
- the SCOPE-OUTS (§7): napkin/knowledge/info content, the opaque tiers
  (``_agents/mirrors/``, ``<scope>/<harness>/``, ``_taskdef/``), legacy
  ``{task_type}/{target}/`` trees and masterplan bundle internals are never
  read and never reported. The proof is a recorder around the real filesystem
  calls, plus booby-trapped files that would each be an ERROR if anything
  looked at them — planted inside a ``runs/``/``_runs/`` directory, which is
  the one placement the record check's by-name walk can reach at all.
"""

import builtins
import contextlib
import importlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from lmer_schema.checks import CHECKS, Form, Report, Severity, Tier
from lmer_schema.checks import agents as agents_checks
from lmer_schema.checks import index_links as index_checks
from lmer_schema.checks import layout as layout_checks
from lmer_schema.checks import manifest as manifest_checks
from lmer_schema.checks import record as record_checks
from lmer_schema.checks import registry as registry_checks
from lmer_schema.checks import runner as run
from lmer_schema.checks.runner import CHECK_ORDER, Check, Context, validate
from tests import plexus_fixtures as pf

#: Just after the fixture record's last session event, so the in-progress
#: session is fresh and the verdict never depends on the wall clock.
NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)

BUCKET_REL = f"{pf.BUCKET_HOST}/{pf.BUCKET_PROJECT}"
LEGACY_HOST, LEGACY_PATH = pf.PRIMARY_REPO.split("/", 1)
LEGACY_PROJECT_REL = f"{LEGACY_HOST}/{LEGACY_PATH}"


def _validate(tree, **kwargs):
    kwargs.setdefault("now", NOW)
    return validate(tree.root, **kwargs)


def _codes(report):
    return [finding.code for finding in report.findings]


def _by_code(report, code):
    return [finding for finding in report.findings if finding.code == code]


def _one(report, code):
    matches = _by_code(report, code)
    assert len(matches) == 1, f"expected exactly one {code}, got {_codes(report)}"
    return matches[0]


def _namespaces(report):
    return {finding.code.split(".", 1)[0] for finding in report.findings}


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _break_manifest(tree, text):
    tree.manifest.write_text(text, encoding="utf-8")


def _edit_manifest(tree, mutate):
    doc = yaml.safe_load(tree.manifest.read_text(encoding="utf-8"))
    mutate(doc)
    tree.manifest.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _dangling_memory_entry(memory_dir):
    """A ``MEMORY.md`` entry with no file — an integrity finding D14 softens
    (ERROR in schema form, WARN at the legacy tier)."""
    memory_dir.mkdir(parents=True, exist_ok=True)
    index = memory_dir / "MEMORY.md"
    existing = index.read_text(encoding="utf-8") if index.is_file() else ""
    index.write_text(existing + "- [gone](project_gone.md) — nothing here.\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# The codes the runner owns
# ---------------------------------------------------------------------------


class TestCodes:
    #: code -> (schema severity, legacy severity, tier)
    EXPECTED = {
        "runner.no-such-path": (Severity.ERROR, Severity.ERROR, Tier.MACHINE),
        "runner.check-failed": (Severity.ERROR, Severity.ERROR, Tier.MACHINE),
        "runner.checks-skipped": (Severity.INFO, Severity.INFO, Tier.MACHINE),
    }

    @pytest.mark.parametrize("code", sorted(EXPECTED))
    def test_registered_with_its_severity_and_tier(self, code):
        schema, legacy, tier = self.EXPECTED[code]
        entry = CHECKS[code]
        assert entry.severity is schema
        assert entry.legacy is legacy
        assert entry.tier is tier

    def test_the_runner_owns_the_whole_runner_namespace(self):
        registered = {entry.code for entry in CHECKS if entry.namespace == "runner"}
        assert registered == set(self.EXPECTED)

    def test_no_runner_code_is_tier_sensitive(self):
        # The runner's findings are about the RUN, not about a surface of the
        # tree, so a legacy tree never softens them (D14 softens integrity
        # findings only).
        for code in self.EXPECTED:
            assert CHECKS[code].tier_sensitive is False


# ---------------------------------------------------------------------------
# The entry-point convention and the declared order
# ---------------------------------------------------------------------------


class TestCheckOrder:
    MODULES = (
        manifest_checks,
        registry_checks,
        layout_checks,
        agents_checks,
        record_checks,
        index_checks,
    )

    def test_the_order_is_fixed_and_declared(self):
        assert [check.module for check in CHECK_ORDER] == list(self.MODULES)

    def test_each_check_names_itself_by_its_module(self):
        assert [check.name for check in CHECK_ORDER] == [
            "manifest",
            "registry",
            "layout",
            "agents",
            "record",
            "index_links",
        ]

    #: Modules of the package that are NOT checks: the finding model, the
    #: shared project walk, the shared symlink rule and this composer. A check
    #: module registers codes and emits findings; these are what check modules
    #: are built from, which is also why importing one is not a check
    #: importing a sibling.
    SUPPORT_MODULES = {"__init__", "findings", "walk", "symlinks", "runner"}

    def test_every_check_module_in_the_package_is_wired_in(self):
        # The runner is the single file that names the check modules, so a
        # module nobody wires in must fail here rather than never run.
        package = Path(run.__file__).parent
        modules = {
            path.stem
            for path in package.glob("*.py")
            if path.stem not in self.SUPPORT_MODULES
        }
        assert {check.name for check in CHECK_ORDER} == modules

    def test_the_support_modules_register_no_check_code(self):
        # The exclusion above must stay honest: a support module that grew a
        # finding is a check module that nobody wired in. `findings` is the
        # registry itself and `runner` owns the `runner.*` codes about the
        # RUN, so the guard is on what is excluded beside them.
        for name in sorted(self.SUPPORT_MODULES - {"__init__", "findings", "runner"}):
            module = importlib.import_module(f"lmer_schema.checks.{name}")
            source = Path(module.__file__).read_text(encoding="utf-8")
            assert "register(" not in source, name

    def test_every_adapter_has_the_one_shape(self):
        # The six modules' own entry points differ (root-first, report-first,
        # keyword-only extras); the convention this file imposes is that the
        # runner reaches each through ONE adapter taking the shared context
        # and returning nothing.
        context = Context(root=Path("/nonexistent"), report=Report(Form.SCHEMA))
        for check in CHECK_ORDER:
            assert check.run.__code__.co_argcount == 1
            assert check.run(context) is None

    def test_checks_are_frozen_values(self):
        with pytest.raises((AttributeError, TypeError)):
            CHECK_ORDER[0].module = None


# ---------------------------------------------------------------------------
# The two fixture trees, end to end
# ---------------------------------------------------------------------------


class TestFixtureTrees:
    def test_schema_tree_is_clean(self, schema_plexus):
        report = _validate(schema_plexus)
        assert report.errors == ()
        assert [f.code for f in report.warnings] == ["manifest.schema-older"]
        assert report.exit_code == 0
        assert report.unusable is None

    def test_schema_tree_lists_its_bucket_and_nothing_else_surprising(self, schema_plexus):
        report = _validate(schema_plexus)
        assert _one(report, "tree.host-bucket").path == BUCKET_REL

    def test_legacy_tree_exits_clean(self, legacy_plexus):
        # G3: the live legacy tree validates unmodified — warnings never block
        # and the legacy tier downgrades what schema form would call an error.
        report = _validate(legacy_plexus)
        assert report.errors == ()
        assert report.exit_code == 0

    def test_form_comes_from_the_core_api(self, schema_plexus, legacy_plexus):
        assert _validate(schema_plexus).form is Form.SCHEMA
        assert _validate(legacy_plexus).form is Form.LEGACY

    def test_every_check_module_contributed_to_the_legacy_tree(self, legacy_plexus):
        # The legacy fixture exercises the manifest (absent), layout (legacy
        # project home) and agents (bare surfaces) modules; a run that quietly
        # stopped after one of them would not carry all three namespaces.
        assert {"manifest", "tree", "agents"} <= _namespaces(_validate(legacy_plexus))

    def test_the_report_is_the_json_document(self, schema_plexus):
        report = _validate(schema_plexus)
        doc = report.to_dict()
        assert doc["form"] == "schema"
        assert doc["exit_code"] == 0
        assert doc["unusable"] is None
        # Plain types only: the CLI serialises this as it stands.
        assert json.loads(json.dumps(doc)) == doc

    def test_no_finding_carries_a_machine_path(self, schema_plexus, legacy_plexus):
        for tree in (schema_plexus, legacy_plexus):
            report = _validate(tree)
            for finding in report.findings:
                assert not finding.path.startswith("/")
                assert str(tree.root) not in (finding.detail or "")
                assert str(tree.root) not in finding.message


# ---------------------------------------------------------------------------
# Unusable input (exit 2) and the short-circuit rule
# ---------------------------------------------------------------------------


class TestUnusableInput:
    def test_a_path_that_does_not_exist(self, tmp_path):
        report = validate(tmp_path / "nope", now=NOW)
        finding = _one(report, "runner.no-such-path")
        assert finding.path == "."
        assert report.exit_code == 2
        assert report.unusable
        assert _codes(report) == ["runner.no-such-path"]

    def test_a_file_where_a_plexus_root_was_expected(self, schema_plexus):
        report = validate(schema_plexus.manifest, now=NOW)
        assert _one(report, "runner.no-such-path")
        assert report.exit_code == 2

    def test_no_check_runs_without_a_tree(self, tmp_path):
        # "Unusable" says there is no tree to say anything about, so nothing
        # downstream is even attempted.
        ran = []
        checks = tuple(
            Check(check.module, lambda ctx, name=check.name: ran.append(name))
            for check in CHECK_ORDER
        )
        validate(tmp_path / "nope", checks=checks, now=NOW)
        assert ran == []

    def test_an_unparseable_manifest_is_exit_2(self, schema_plexus):
        _break_manifest(schema_plexus, "schema: 1\nname: [unterminated\n")
        report = _validate(schema_plexus)
        assert report.exit_code == 2
        assert report.unusable
        assert "manifest.unparseable" in _codes(report)

    def test_an_unusable_manifest_suppresses_every_downstream_check(self, schema_plexus):
        # THE decision: downstream checks do not run against an empty
        # registry. Every registered project would become "unregistered" and
        # every registered remote's path a plain bucket — confident nonsense
        # about a tree nobody could read.
        _break_manifest(schema_plexus, "schema: 1\nname: [unterminated\n")
        report = _validate(schema_plexus)
        assert _namespaces(report) <= {"manifest", "runner"}
        assert _by_code(report, "tree.host-bucket") == []

    def test_the_suppression_is_announced(self, schema_plexus):
        _break_manifest(schema_plexus, "schema: 1\nname: [unterminated\n")
        finding = _one(_validate(schema_plexus), "runner.checks-skipped")
        assert finding.severity is Severity.INFO
        assert finding.path == "."
        for name in ("registry", "layout", "agents", "record", "index_links"):
            assert name in (finding.detail or "")

    def test_a_real_defect_below_is_not_reported_from_a_broken_manifest(self, schema_plexus):
        # The suppressed checks are suppressed even when the tree really does
        # carry a defect: fix the manifest first, then the list is trustworthy.
        _dangling_memory_entry(schema_plexus.project_memory_dir)
        _break_manifest(schema_plexus, ": not a mapping\n")
        report = _validate(schema_plexus)
        assert _by_code(report, "agents.memory-entry-without-file") == []
        assert report.exit_code == 2

    def test_a_newer_schema_int_is_unusable_too(self, schema_plexus):
        # T8's reading of §4: validating a tree written by a newer schema
        # against schema-1 rules would emit confident nonsense, so the run
        # stops exactly as a broken manifest does.
        _edit_manifest(schema_plexus, lambda doc: doc.update(schema=99))
        report = _validate(schema_plexus)
        assert _one(report, "manifest.schema-newer").severity is Severity.ERROR
        assert report.exit_code == 2
        assert _namespaces(report) <= {"manifest", "runner"}

    def test_a_warned_manifest_still_runs_every_check(self, schema_plexus):
        # A warned manifest is a usable one: an unknown top-level key is a
        # nudge (the vocabulary may grow under one `schema:` int), never a
        # reason to stop reading the tree. (At schema 1 there is no usable
        # OLDER int — `SUPPORTED_SCHEMAS` is `(1,)`, so `schema: 0` is
        # unsupported and unusable exactly like a newer one.)
        _edit_manifest(schema_plexus, lambda doc: doc.update(colour="blue"))
        report = _validate(schema_plexus)
        assert _one(report, "manifest.unknown-key").severity is Severity.WARN
        assert report.exit_code == 0
        assert "tree" in _namespaces(report)

    def test_a_broken_manifest_is_still_schema_form(self, schema_plexus):
        # D20: detection is presence-only. A typo must never demote a migrated
        # plexus to the legacy tier — that is the silent demotion the whole
        # dual-read decision exists to prevent.
        _break_manifest(schema_plexus, "schema: 1\nname: [unterminated\n")
        assert _validate(schema_plexus).form is Form.SCHEMA


# ---------------------------------------------------------------------------
# A registry that does not build: one error, the run continues
# ---------------------------------------------------------------------------


class TestInvalidRegistry:
    @staticmethod
    def _claim_twice(doc):
        # One remote claimed by two entries: resolution would be ambiguous, so
        # `build_registry` refuses (D8/§4).
        doc["projects"]["other"] = {"repo": pf.PRIMARY_REPO}

    def test_it_is_one_error_finding_not_exit_2(self, schema_plexus):
        _edit_manifest(schema_plexus, self._claim_twice)
        report = _validate(schema_plexus)
        assert _one(report, "registry.invalid").severity is Severity.ERROR
        assert report.exit_code == 1
        assert report.unusable is None

    def test_the_remaining_checks_still_run(self, schema_plexus):
        # The manifest PARSED: the tree, its form and every non-registry
        # surface still say something true.
        _dangling_memory_entry(schema_plexus.project_memory_dir)
        _edit_manifest(schema_plexus, self._claim_twice)
        report = _validate(schema_plexus)
        assert _one(report, "agents.memory-entry-without-file").severity is Severity.ERROR
        assert _one(report, "tree.host-bucket")

    def test_registry_free_layout_invents_no_unregistered_projects(self, schema_plexus):
        # Without a registry the layout check must not call every registered
        # project dir "unregistered" — it runs the registry-free half.
        _edit_manifest(schema_plexus, self._claim_twice)
        report = _validate(schema_plexus)
        assert _by_code(report, "tree.unregistered-project") == []
        assert _by_code(report, "tree.entry-without-dir") == []


# ---------------------------------------------------------------------------
# Walk once, load once
# ---------------------------------------------------------------------------


class TestWalkOnce:
    def test_the_registry_is_built_once(self, schema_plexus, monkeypatch):
        calls = []
        real = registry_checks.build_registry

        def counted(manifest):
            calls.append(manifest)
            return real(manifest)

        monkeypatch.setattr(registry_checks, "build_registry", counted)
        # The layout module reloads the registry from disk when the caller
        # does not pass one; the runner always passes one.
        monkeypatch.setattr(
            layout_checks,
            "load_registry",
            lambda root: pytest.fail("the runner must pass the registry it built"),
        )
        report = _validate(schema_plexus)
        assert len(calls) == 1
        assert report.exit_code == 0

    def test_the_manifest_is_loaded_once(self, schema_plexus, monkeypatch):
        calls = []
        real = manifest_checks.core.load_manifest

        def counted(root):
            calls.append(root)
            return real(root)

        monkeypatch.setattr(manifest_checks.core, "load_manifest", counted)
        _validate(schema_plexus)
        assert len(calls) == 1

    def test_the_per_project_modules_do_not_re_discover(self, schema_plexus, monkeypatch):
        # Both grew their own descent because they were written in parallel;
        # the runner hands them the layout check's list instead.
        monkeypatch.setattr(
            agents_checks,
            "iter_project_dirs",
            lambda root: pytest.fail("agents must take the runner's project list"),
        )
        monkeypatch.setattr(
            index_checks,
            "iter_project_dirs",
            lambda root: pytest.fail("index_links must take the runner's project list"),
        )
        report = _validate(schema_plexus)
        assert report.exit_code == 0

    def test_the_project_list_reaches_both_modules(self, schema_plexus):
        # Same list, both modules: the bucket's memory and its index dir are
        # checked, which only happens if the bucket dir was passed down.
        _dangling_memory_entry(schema_plexus.bucket_dir / "_agents" / "memory")
        specs = schema_plexus.bucket_dir / "_specs"
        specs.mkdir()
        (specs / "2026-08-24-x--spec.md").symlink_to("../_runs/gone/spec.md")
        report = _validate(schema_plexus)
        assert _one(report, "agents.memory-entry-without-file").path.startswith(BUCKET_REL)
        assert _one(report, "index.dangling-link").path.startswith(BUCKET_REL)


# ---------------------------------------------------------------------------
# The form-override trap: a bucket's contents are legacy-tier
# ---------------------------------------------------------------------------


class TestBucketTier:
    def test_a_bucket_memory_defect_is_softened(self, schema_plexus):
        # SCHEMA.md "Host buckets": bucket contents are checked at the legacy
        # tier's severities even inside a schema-form tree.
        _dangling_memory_entry(schema_plexus.bucket_dir / "_agents" / "memory")
        report = _validate(schema_plexus)
        finding = _one(report, "agents.memory-entry-without-file")
        assert finding.path.startswith(BUCKET_REL)
        assert finding.severity is Severity.WARN
        assert CHECKS[finding.code].severity is Severity.ERROR  # schema tier
        assert report.exit_code == 0  # a bucket never blocks (D9)

    def test_the_same_defect_in_a_named_project_is_an_error(self, schema_plexus):
        _dangling_memory_entry(schema_plexus.project_memory_dir)
        report = _validate(schema_plexus)
        finding = _one(report, "agents.memory-entry-without-file")
        assert finding.path.startswith(pf.PROJECT)
        assert finding.severity is Severity.ERROR
        assert report.exit_code == 1

    def test_a_memory_scope_mismatch_in_a_bucket_is_informational(self, schema_plexus):
        memory = schema_plexus.bucket_dir / "_agents" / "memory"
        memory.mkdir(parents=True)
        _write(memory / pf.USER_MEMORY_FILE, pf._memory_text(pf.USER_MEMORY_FILE))
        _write(memory / "MEMORY.md", f"- [user]({pf.USER_MEMORY_FILE}) — instance scope.\n")
        report = _validate(schema_plexus)
        assert _one(report, "agents.memory-scope-mismatch").severity is Severity.INFO

    def test_the_index_module_agrees_about_the_tier(self, schema_plexus):
        # index_links derives the bucket tier itself; the two paths must not
        # disagree about the same directory.
        specs = schema_plexus.bucket_dir / "_specs"
        specs.mkdir()
        (specs / "2026-08-24-x--spec.md").symlink_to("../_runs/gone/spec.md")
        assert _one(_validate(schema_plexus), "index.dangling-link").severity is Severity.WARN

    def test_the_tier_travels_with_the_project_list(self, schema_plexus):
        # !256 discussion 5: `project_dirs=` used to force the report's own
        # form on every dir it was handed, so the runner pinned the tier back
        # by path. Each ProjectDir now states the form its CONTENTS are
        # checked at and `check_agents` reads it — no view in sight.
        _dangling_memory_entry(schema_plexus.bucket_dir / "_agents" / "memory")
        projects = layout_checks.check_layout(schema_plexus.root, Report(Form.SCHEMA))
        report = Report(Form.SCHEMA)
        agents_checks.check_agents(schema_plexus.root, report, project_dirs=projects)
        assert _one(report, "agents.memory-entry-without-file").severity is Severity.WARN

    def test_only_the_record_module_writes_through_the_tier_view(
        self, schema_plexus, monkeypatch
    ):
        """`record` finds run areas by NAME (it must work on a bare root) and
        is handed no project list to read a form from, so the runner pins ITS
        tier by path. The two project-list modules carry the tier with the
        list and get the report itself — the view can only get in their way.
        The wiring stopped being latent in !261: `record` now emits the
        index mechanic's `index.dangling-link` for a session-dir artifact
        symlink, and that code IS tier-sensitive — so the test below exercises
        the view rather than only pinning that it is installed."""
        seen = {}

        def spy(module, attr, name):
            real = getattr(module, attr)

            def wrapped(root, report, *args, **kwargs):
                seen[name] = report
                return real(root, report, *args, **kwargs)

            monkeypatch.setattr(module, attr, wrapped)

        spy(agents_checks, "check_agents", "agents")
        spy(record_checks, "check_record", "record")
        spy(index_checks, "check_index_links", "index_links")
        report = _validate(schema_plexus)
        assert seen["agents"] is report
        assert seen["index_links"] is report
        assert seen["record"] is not report

    def test_a_buckets_dangling_artifact_link_is_softened_by_the_view(
        self, schema_plexus
    ):
        # The tier view doing real work: `record` emits a tier-sensitive code
        # (!261 gave SCHEMA.md's artifact-symlink row a check), and a host
        # bucket's contents are checked at the legacy tier inside a schema
        # tree — so the SAME defect is WARN here and ERROR in a named project.
        bucket_session = schema_plexus.bucket_dir / "_runs" / pf.SLUG / pf.SESSION_IDS[0]
        bucket_session.mkdir(parents=True, exist_ok=True)
        _write(bucket_session / "state.yaml", "schema: 1\nslug: x\nstatus: complete\n")
        (bucket_session / "retro.md").symlink_to("masterplan/retro.md")

        # `spec.md` is already a real file in the fixture's session dir (the
        # free-form content shape of the same row), so break a different
        # well-known artifact rather than reshaping the fixture.
        named_session = schema_plexus.project_dir / "_runs" / pf.SLUG / pf.SESSION_IDS[0]
        (named_session / "retro.md").symlink_to("masterplan/retro.md")

        report = _validate(schema_plexus)
        by_path = {
            f.path: f.severity
            for f in report.findings
            if f.code == "index.dangling-link"
        }
        bucket_link = next(p for p in by_path if p.startswith(BUCKET_REL))
        named_link = next(p for p in by_path if not p.startswith(BUCKET_REL))
        assert by_path[bucket_link] is Severity.WARN, "a bucket never blocks"
        assert by_path[named_link] is Severity.ERROR

    def test_a_shape_violation_in_a_bucket_is_still_an_error(self, schema_plexus):
        # SCHEMA.md "Host buckets": record-file shapes are still errors — a
        # corrupt state.yaml is broken wherever it lives — and only the
        # integrity findings are downgraded.
        session = schema_plexus.bucket_dir / "_runs" / pf.SLUG / pf.SESSION_IDS[0]
        _write(session / "state.yaml", "schema: [unterminated\n")
        report = _validate(schema_plexus)
        finding = _one(report, "record.state-unparseable")
        assert finding.path.startswith(BUCKET_REL)
        assert finding.severity is Severity.ERROR

    def test_a_legacy_tree_needs_no_override(self, legacy_plexus):
        _dangling_memory_entry(legacy_plexus.project_memory_dir)
        report = _validate(legacy_plexus)
        assert _one(report, "agents.memory-entry-without-file").severity is Severity.WARN
        assert report.exit_code == 0


# ---------------------------------------------------------------------------
# One check raising never aborts the run
# ---------------------------------------------------------------------------


class TestCrashSafety:
    @staticmethod
    def _explode(*args, **kwargs):
        raise RuntimeError("boom")

    def test_a_raising_check_becomes_one_error_finding(self, schema_plexus, monkeypatch):
        monkeypatch.setattr(record_checks, "check_record", self._explode)
        report = _validate(schema_plexus)
        finding = _one(report, "runner.check-failed")
        assert finding.severity is Severity.ERROR
        assert finding.path == "."
        assert "record" in finding.message
        assert "RuntimeError" in (finding.detail or "")
        assert report.exit_code == 1

    def test_the_remaining_checks_still_run(self, schema_plexus, monkeypatch):
        monkeypatch.setattr(layout_checks, "check_layout", self._explode)
        specs = schema_plexus.specs_dir
        (specs / "2026-08-24-y--spec.md").symlink_to("../_runs/gone/spec.md")
        report = _validate(schema_plexus)
        assert _one(report, "runner.check-failed")
        # index_links runs after layout and discovers the projects itself when
        # the layout walk never produced a list.
        assert _one(report, "index.dangling-link")

    def test_a_crash_in_the_first_check_degrades_to_a_registry_free_run(
        self, schema_plexus, monkeypatch
    ):
        monkeypatch.setattr(manifest_checks, "check_manifest", self._explode)
        report = _validate(schema_plexus)
        assert _one(report, "runner.check-failed")
        assert _by_code(report, "tree.unregistered-project") == []
        assert report.unusable is None

    def test_the_exception_text_never_leaks_a_machine_path(self, schema_plexus, monkeypatch):
        root = schema_plexus.root

        def leaky(*args, **kwargs):
            raise OSError(f"cannot read {root}/lmer/_runs")

        monkeypatch.setattr(record_checks, "check_record", leaky)
        finding = _one(_validate(schema_plexus), "runner.check-failed")
        assert str(root) not in (finding.detail or "")
        assert "<root>/lmer/_runs" in (finding.detail or "")

    def test_an_interrupt_is_never_swallowed(self, schema_plexus, monkeypatch):
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(record_checks, "check_record", interrupted)
        with pytest.raises(KeyboardInterrupt):
            _validate(schema_plexus)

    def test_every_check_can_crash_and_the_run_still_reports(
        self, schema_plexus, monkeypatch
    ):
        for check in CHECK_ORDER:
            monkeypatch.setattr(check.module, f"check_{check.name}", self._explode)
        report = _validate(schema_plexus)
        assert len(_by_code(report, "runner.check-failed")) == len(CHECK_ORDER)
        assert report.exit_code == 1


# ---------------------------------------------------------------------------
# Redaction: which spellings of the root are patterns
# ---------------------------------------------------------------------------


class TestRedaction:
    """A crash's message is the one string in a finding the validator did not
    write, so the tree's path is redacted out of it. Only the ABSOLUTE
    spellings carry a machine path, and only they are specific enough to be a
    substring pattern: `lmer-schema validate .` is the most natural
    invocation, and a bare `.` as a pattern is every sentence end and every
    file extension in the message.
    """

    @staticmethod
    def _raise(message):
        def leaky(*args, **kwargs):
            raise ValueError(message)

        return leaky

    def test_a_relative_root_is_never_a_pattern(self, schema_plexus, monkeypatch):
        message = "bad state.yaml at ./lmer/_runs/x"
        monkeypatch.setattr(record_checks, "check_record", self._raise(message))
        monkeypatch.chdir(schema_plexus.root)
        finding = _one(validate(".", now=NOW), "runner.check-failed")
        assert finding.detail == f"ValueError: {message}"

    def test_a_root_spelled_as_an_ordinary_word_is_not_one_either(
        self, tmp_path, monkeypatch
    ):
        # `validate('work')` from the work repo's parent: "work" is a word
        # that turns up in a message about a work repo more often than not.
        tree = pf.build_schema_plexus(tmp_path / "work")
        message = "the work repo moved under me"
        monkeypatch.setattr(record_checks, "check_record", self._raise(message))
        monkeypatch.chdir(tmp_path)
        finding = _one(validate("work", now=NOW), "runner.check-failed")
        assert finding.detail == f"ValueError: {message}"
        assert tree.root.is_dir()  # the tree really was the one validated

    def test_the_absolute_path_is_still_redacted_through_a_relative_root(
        self, schema_plexus, monkeypatch
    ):
        # The point of redacting at all: whatever the caller spelled, the
        # message routinely carries the path the check was actually reading.
        root = schema_plexus.root
        monkeypatch.setattr(
            record_checks, "check_record", self._raise(f"cannot read {root}/lmer/_runs")
        )
        monkeypatch.chdir(root)
        detail = _one(validate(".", now=NOW), "runner.check-failed").detail
        assert str(root) not in detail
        assert "<root>/lmer/_runs" in detail

    def test_the_spellings_that_are_patterns_are_the_absolute_ones(self, tmp_path):
        assert run._redact("bad state.yaml at ./x", Path(".")) == "bad state.yaml at ./x"
        assert run._redact("nothing here", Path("")) == "nothing here"
        # The filesystem root names no machine and matches every separator.
        assert run._redact("everything under /etc", Path("/")) == "everything under /etc"
        assert run._redact(f"cannot read {tmp_path}/x", tmp_path) == "cannot read <root>/x"


# ---------------------------------------------------------------------------
# Parameters the CLI passes through
# ---------------------------------------------------------------------------


class TestParameters:
    def test_the_staleness_window_reaches_the_record_check(self, schema_plexus):
        later = NOW + timedelta(days=30)
        report = validate(schema_plexus.root, now=later)
        finding = _one(report, "record.session-stale")
        assert finding.severity is Severity.INFO
        assert report.exit_code == 0
        assert _by_code(validate(schema_plexus.root, now=NOW), "record.session-stale") == []

    def test_a_wider_window_silences_it(self, schema_plexus):
        later = NOW + timedelta(days=30)
        report = validate(schema_plexus.root, now=later, stale_after=60 * 86400)
        assert _by_code(report, "record.session-stale") == []

    def test_the_plexus_repo_url_reaches_the_sources_guard(self, schema_plexus):
        # T8's same-host guard is dormant unless the caller supplies the URL.
        report = _validate(schema_plexus, plexus_repo_url="https://elsewhere.example.net/x/y")
        assert _by_code(report, "manifest.sources-cross-host")
        assert _by_code(_validate(schema_plexus), "manifest.sources-cross-host") == []


# ---------------------------------------------------------------------------
# The run never writes
# ---------------------------------------------------------------------------


def _snapshot(root):
    """Every path under *root* with what it is and what it holds — a validator
    that touched the tree changes this."""
    snapshot = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[rel] = ("symlink", os.readlink(path))
        elif path.is_dir():
            snapshot[rel] = ("dir", None)
        else:
            snapshot[rel] = ("file", path.read_bytes())
    return snapshot


class TestReadOnly:
    """Validation is a read: the validator never repairs, tidies or moves
    anything — not even a state file it cannot parse (the work repo it is
    pointed at is somebody's live record, and one of lmer's own read paths
    already learned this the hard way)."""

    @pytest.mark.parametrize("fixture", ["schema_plexus", "legacy_plexus"])
    def test_the_tree_is_unchanged(self, fixture, request):
        tree = request.getfixturevalue(fixture)
        _booby_trap(tree)
        before = _snapshot(tree.root)
        _validate(tree)
        assert _snapshot(tree.root) == before

    def test_a_corrupt_record_is_reported_not_repaired(self, schema_plexus):
        state = schema_plexus.session_dirs[0] / "state.yaml"
        state.write_text("schema: [unterminated\n", encoding="utf-8")
        before = _snapshot(schema_plexus.root)
        report = _validate(schema_plexus)
        assert _one(report, "record.state-unparseable")
        assert _snapshot(schema_plexus.root) == before


# ---------------------------------------------------------------------------
# The pipeline as a caller's own object
# ---------------------------------------------------------------------------


class TestRunChecks:
    def test_a_caller_can_run_a_subset_into_its_own_context(self, schema_plexus):
        report = Report(Form.SCHEMA)
        context = Context(root=schema_plexus.root, report=report)
        assert run.run_checks(context, CHECK_ORDER[:2]) is report
        # The manifest and registry stages ran and left their artifacts.
        assert context.manifest is not None
        assert context.registry is not None
        assert context.project_dirs is None  # the layout stage never ran
        assert _codes(report) == ["manifest.schema-older"]

    def test_the_context_carries_the_stages_output(self, schema_plexus):
        report = Report(Form.SCHEMA)
        context = Context(root=schema_plexus.root, report=report, now=NOW)
        run.run_checks(context)
        assert context.manifest.name == pf.INSTANCE_NAME
        assert pf.PROJECT in context.registry
        assert {project.rel for project in context.project_dirs} == {
            pf.PROJECT,
            pf.REPOLESS_PROJECT,
            BUCKET_REL,
        }


# ---------------------------------------------------------------------------
# The scope-outs (§7): never read, never reported
# ---------------------------------------------------------------------------


class _Recorder:
    """Records every directory listing and every file read the validator
    performs, by wrapping the two calls every check module goes through."""

    def __init__(self):
        self.listed = []
        self.read = []

    @contextlib.contextmanager
    def recording(self):
        real_iterdir = Path.iterdir
        real_read_text = Path.read_text
        real_read_bytes = Path.read_bytes
        real_open = builtins.open

        def iterdir(this):
            self.listed.append(Path(this))
            return real_iterdir(this)

        def read_text(this, *args, **kwargs):
            self.read.append(Path(this))
            return real_read_text(this, *args, **kwargs)

        def read_bytes(this):
            self.read.append(Path(this))
            return real_read_bytes(this)

        def opened(file, *args, **kwargs):
            self.read.append(Path(file))
            return real_open(file, *args, **kwargs)

        Path.iterdir = iterdir
        Path.read_text = read_text
        Path.read_bytes = read_bytes
        builtins.open = opened
        try:
            yield self
        finally:
            Path.iterdir = real_iterdir
            Path.read_text = real_read_text
            Path.read_bytes = real_read_bytes
            builtins.open = real_open

    def under(self, base, which):
        base = Path(base)
        return sorted(
            path for path in which if path == base or base in path.parents
        )


class _Traps:
    """What :func:`_booby_trap` planted: the ``bundle`` root, the scope-out
    ``roots`` nothing may read or report, the run ``areas`` planted inside
    them (the placement that can be reached at all), and every ``file``
    planted (so a typo in the planter cannot make a scope-out test pass
    vacuously)."""

    def __init__(self, bundle, roots, areas, files):
        self.bundle = bundle
        self.roots = roots
        self.areas = areas
        self.files = files


#: What a trap is made of: each of these would be a ``record.*`` ERROR if any
#: check opened it.
CORRUPT_YAML = "schema: [unterminated\n"
CORRUPT_JSON = "{not json\n"
BAD_EVENTS = "not a json object\n"

#: The names a trapped record uses inside a run area: ``runs/<slug>/`` is the
#: legacy shared dir and ``_runs/<slug>/<session-id>/`` the session unit, so
#: whichever layout the walk would apply, it finds a corrupt record.
TRAP_SLUG = "demo"
TRAP_SESSION_ID = "20260824-101500-1-1"


def _trap_run_areas(base):
    """Plant a corrupt record in both run-area spellings under *base* and
    return the two area directories.

    This is the placement that MATTERS. The record check finds run areas by
    NAME at any depth, so a trap sitting DIRECTLY in a scope-out is
    unreachable for it by construction and proves nothing about a boundary; a
    trap inside a directory called ``runs``/``_runs`` is what a vendored fork
    shipping its own run fixtures looks like (``_agents/mirrors/<fork>/runs/``
    — the case the !257 review reproduced), and the walk reaches it unless
    something stops it at the scope-out.
    """
    legacy = base / "runs"
    _write(legacy / TRAP_SLUG / "state.yaml", CORRUPT_YAML)
    _write(legacy / TRAP_SLUG / "events.jsonl", BAD_EVENTS)
    session = base / "_runs"
    record = session / TRAP_SLUG / TRAP_SESSION_ID
    _write(record / "state.yaml", CORRUPT_YAML)
    _write(record / "plan.index.json", CORRUPT_JSON)
    return (legacy, session)


def _booby_trap(tree):
    """Plant, in every scope-out, files that would each be an ERROR if any
    check looked at them — inside a run area wherever the walk could reach
    one (:func:`_trap_run_areas`), and directly in the scope-out as well."""
    scope_outs = []
    areas = []

    def scope_out(base):
        scope_outs.append(base)
        areas.extend(_trap_run_areas(base))
        return base

    napkin_topic = scope_out(tree.napkin_dir / pf.INSTANCE_NAME)
    _write(napkin_topic / "state.yaml", CORRUPT_YAML)
    _write(napkin_topic / "events.jsonl", BAD_EVENTS)
    _write(napkin_topic / "MEMORY.md", "- [gone](gone.md) — not an index.\n")

    # `_agents/mirrors/<fork>/` is the opaque tier (SCHEMA.md: "a foreign
    # writer's tree, checked no further than its existence"). The vendored
    # fork ships run fixtures of its own, which this plexus can never fix.
    scope_out(tree.mirrors_dir / "masterplan")
    # `<scope>/<harness>/` is opaque too: the harness's own layout.
    scope_out(tree.agents_claude_dir)
    # The taskdef trees, at both scopes — instance and project override.
    scope_out(tree.taskdef_dir / "review")
    scope_out(tree.project_dir / tree.taskdef_dir.name / "review")

    if tree.knowledge_dir is not None:
        knowledge_topic = scope_out(tree.knowledge_dir / pf.INSTANCE_NAME)
        _write(knowledge_topic / "state.yaml", CORRUPT_YAML)
        _write(knowledge_topic / "ledger.yaml", CORRUPT_YAML)

    # Info CONTENT: free-form, pruned by name at both spellings (`_info/`
    # and the legacy `info/` twin).
    info = scope_out(tree.info_dir)
    _write(info / "state.yaml", CORRUPT_YAML)
    _write(info / "plan.index.json", CORRUPT_JSON)

    # A masterplan bundle, flat as D12 requires, with a corrupt state file and
    # a sub-tree inside it: a foreign writer's tree, opaque past existence.
    # Its run area needs no prune rule — the walk stops at the session area
    # above it — so it is a trap, not one of `areas`.
    bundle = tree.session_dirs[0] / "masterplan"
    _write(bundle / "spec.md", "# spec\n")
    _write(bundle / "plan.index.json", CORRUPT_JSON)
    _write(bundle / "handoff" / "state.yaml", CORRUPT_YAML)
    _write(bundle / "runs" / TRAP_SLUG / "state.yaml", CORRUPT_YAML)
    scope_outs.append(bundle)

    # The pre-run-dir legacy layout: {host}/{project}/{task_type}/{target}/.
    # `log.yaml` and `reports/` are what the legacy writers left there, and
    # what makes the directory recognisable as a target at all (issue #379).
    task_tree = tree.project_dir / "develop" / "issue-12"
    _write(task_tree / "state.yaml", CORRUPT_YAML)
    _write(task_tree / "log.yaml", CORRUPT_YAML)
    _write(task_tree / "reports" / "gate.md", "# gate\n")
    areas.extend(_trap_run_areas(task_tree))
    scope_outs.append(tree.project_dir / "develop")

    planted = sorted(
        path
        for base in scope_outs
        for path in base.rglob("*")
        if path.is_file()
    )
    return _Traps(bundle, scope_outs, tuple(areas), planted)


#: Every module-level set the record walk's scope-out boundary is made of:
#: the three area-name sets and the ``{target}`` marker set that the legacy
#: task tree is recognised by. Emptying all four is "the walk without its
#: boundary", the negative control the guarantee tests are measured against.
_PRUNE_SETS = (
    "OPAQUE_AREAS",
    "LEGACY_INSTANCE_AREAS",
    "LEGACY_PROJECT_AREAS",
    "LEGACY_TARGET_MARKERS",
)


def _run_areas_without_the_boundary(root, monkeypatch):
    """Every run area the record walk finds with its scope-out boundary
    switched OFF — that is, everywhere a plain by-name walk could reach.

    The boundary is ``record.py``'s four prune sets; emptying them turns
    :func:`~lmer_schema.checks.record.iter_run_areas` back into the walk it
    would be without them. If that boundary is ever re-expressed some other
    way this stops finding the trapped areas, and the reachability assertions
    FAIL rather than pass vacuously — the direction a guard should fail in.
    """
    with monkeypatch.context() as without:
        for name in _PRUNE_SETS:
            without.setattr(record_checks, name, frozenset())
        return {area for area, _layout in record_checks.iter_run_areas(root)}


class TestScopeOuts:
    """§7 scope-outs: napkin/knowledge/info CONTENT, the opaque tiers
    (``_agents/mirrors/``, ``<scope>/<harness>/``, ``_taskdef/``), legacy
    ``{task_type}/{target}/`` trees and masterplan bundle internals.

    Two guarantees, both provable: no byte inside them is ever read, and no
    finding ever points inside them. The one thing that does happen is a
    directory-NAME scan: the record check finds run areas by name at any
    depth, so it lists directories on the way. It reads nothing there, which
    is what the first test pins.

    That by-name scan is also what makes the PLACEMENT of a trap the whole
    question. A corrupt state file sitting directly in a scope-out is
    unreachable for the record check by construction — nothing looks for one
    there — so the traps live inside a ``runs/``/``_runs/`` directory under
    each scope-out, which is the case the guarantee is about and the one the
    !257 review reproduced. ``test_the_traps_are_planted`` pins that property
    of the placement, and the test below it — the same traps with the walk's
    boundary switched off — is its negative control.

    Every scope-out here carries the run-area placement, the legacy
    ``{task_type}/{target}/`` tree included: it was the last one the record
    walk entered (issue #379), and it is pruned by SHAPE rather than by name
    because ``{task_type}`` is an open set.
    """

    def _run(self, tree, **kwargs):
        recorder = _Recorder()
        with recorder.recording():
            report = _validate(tree, **kwargs)
        # Guard the guard: a recorder that recorded nothing would make every
        # assertion below vacuously true.
        assert recorder.read and recorder.listed
        return report, recorder

    @pytest.mark.parametrize("fixture", ["schema_plexus", "legacy_plexus"])
    def test_the_traps_are_planted(self, fixture, request, monkeypatch):
        # And the guard for the guard's guard: a typo in the planter would
        # otherwise leave the scope-out tests looking at empty directories.
        tree = request.getfixturevalue(fixture)
        traps = _booby_trap(tree)
        assert len(traps.files) >= 10
        assert all(path.is_file() for path in traps.files)
        # The property the placement must have: every trapped run area is
        # somewhere the by-name walk WOULD reach if the boundary were removed.
        # Without this the two guarantee tests below pass on traps nothing
        # could have found, whatever the boundary does.
        assert traps.areas
        reachable = _run_areas_without_the_boundary(tree.root, monkeypatch)
        assert set(traps.areas) <= reachable

    @pytest.mark.parametrize("fixture", ["schema_plexus", "legacy_plexus"])
    def test_without_the_boundary_the_same_traps_are_errors(
        self, fixture, request, monkeypatch
    ):
        # The negative control, and the !257 reproduction kept as a test: with
        # the record walk's boundary switched off, each trapped record under
        # an opaque or free-form tier is opened, parsed and reported, and an
        # otherwise conforming tree exits 1. With the boundary as shipped (the
        # two tests below) the same files are silent.
        tree = request.getfixturevalue(fixture)
        traps = _booby_trap(tree)
        for name in _PRUNE_SETS:
            monkeypatch.setattr(record_checks, name, frozenset())
        report = _validate(tree)
        reported = {finding.path for finding in _by_code(report, "record.state-unparseable")}
        for area in traps.areas:
            rel = area.relative_to(tree.root).as_posix()
            assert any(path.startswith(f"{rel}/") for path in reported), (rel, reported)
        assert report.exit_code == 1

    @pytest.mark.parametrize("fixture", ["schema_plexus", "legacy_plexus"])
    def test_nothing_inside_a_scope_out_is_ever_read(self, fixture, request):
        tree = request.getfixturevalue(fixture)
        traps = _booby_trap(tree)
        _report, recorder = self._run(tree)
        for base in traps.roots:
            assert recorder.under(base, recorder.read) == []

    @pytest.mark.parametrize("fixture", ["schema_plexus", "legacy_plexus"])
    def test_nothing_inside_a_scope_out_is_ever_reported(self, fixture, request):
        tree = request.getfixturevalue(fixture)
        traps = _booby_trap(tree)
        report, _recorder = self._run(tree)
        rels = [base.relative_to(tree.root).as_posix() for base in traps.roots]
        for finding in report.findings:
            for rel in rels:
                assert not finding.path.startswith(f"{rel}/"), finding

    def test_the_booby_traps_are_real_findings_anywhere_else(self, schema_plexus):
        # Guard the guard: the planted files are only harmless because of
        # WHERE they are. The same state.yaml inside a session dir is an error.
        _write(schema_plexus.session_dirs[0] / "ledger.yaml", "schema: [unterminated\n")
        report = _validate(schema_plexus)
        assert _one(report, "record.state-unparseable").severity is Severity.ERROR

    def test_a_bundle_is_never_descended_into(self, schema_plexus):
        traps = _booby_trap(schema_plexus)
        _report, recorder = self._run(schema_plexus)
        # D12/opacity: a flat bundle is known to exist and nothing more — not
        # one directory inside it is even listed.
        assert recorder.under(traps.bundle, recorder.listed) == []

    def test_a_nested_bundle_is_reported_but_still_opaque(self, schema_plexus):
        # The pre-D12 nested shape is the ONE thing looked for, and finding it
        # still reads nothing inside the bundle.
        bundle = schema_plexus.session_dirs[1] / "masterplan" / "old-slug"
        _write(bundle / "spec.md", "# spec\n")
        _write(bundle / "plan.index.json", "{not json\n")
        _report, recorder = self._run(schema_plexus)
        assert recorder.under(bundle, recorder.read) == []
        assert recorder.under(bundle, recorder.listed) == []
        report = _validate(schema_plexus)
        finding = _one(report, "record.masterplan-nested")
        assert finding.severity is Severity.INFO
        assert finding.path.endswith("masterplan")

    def test_free_form_content_is_never_read(self, schema_plexus):
        # The free-form tier as a whole: knowledge pages, napkin bodies, info
        # prose and plan bodies are never opened.
        _report, recorder = self._run(schema_plexus)
        never = (
            schema_plexus.napkin_dir,
            schema_plexus.knowledge_dir,
            schema_plexus.info_dir,
        )
        for base in never:
            assert recorder.under(base, recorder.read) == []

    def test_the_walk_lists_but_never_reads_below_an_area(self, schema_plexus):
        # Honest about the one descent that does happen: the record check
        # finds `_runs/` by NAME at any depth, so directories under a
        # scope-out are listed. Nothing there is read and nothing is reported.
        _booby_trap(schema_plexus)
        _report, recorder = self._run(schema_plexus)
        listed = recorder.under(schema_plexus.napkin_dir, recorder.listed)
        assert all(path.is_dir() for path in listed)
        assert recorder.under(schema_plexus.napkin_dir, recorder.read) == []
