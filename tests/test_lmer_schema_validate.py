"""G4 — the schema-form plexus validates green, and green means CHECKED
(spec §2/§4/§7, D8/D9/D17/D19/D22; SCHEMA.md; issue #353).

This module is the goal's evidence: ``tests/plexus_fixtures.build_schema_plexus``
lays out the spec's ONE form and ``lmer-schema validate`` walks it end to end
and exits ``0`` with zero ERROR findings. That sentence is worth nothing on its
own — a fixture with nothing in it exits 0 too — so the file is deliberately
two halves, and neither is the point without the other:

**The positive half** asserts the verdict AND that the fixture reaches every
part of the form G4 names. For each one the assertion is not "the tree has it"
(``tests/test_lmer_schema_fixtures.py`` owns the shape) but "the validator
LOOKED at it": the multi-remote entry resolves through the real registry from
every spelling of every remote, the repoless entry resolves by name and by
nothing else, the flat short-name project dirs are what the shared walk
returns, and each surface gets a probe — a defect planted where that check
would have to see it — so a check that silently stopped running would fail a
test here rather than keep the tree green.

**The negative half** injects ONE defect per check family into a fresh tree
(bad ``schema:`` int, a duplicate normalized remote, a real directory at a
registered remote's bucket path, a dangling ``_specs/`` symlink, a ``MEMORY.md``
entry with no file, a corrupt ``state.yaml``) and pins the code, the severity
AT THE SCHEMA-FORM TIER, the path and the exit code. Six families, six check
namespaces: if any check module stopped contributing, the green run above would
not notice and one of these would.

Where the trees come from
=========================
Every tree is a fresh build under the test's own ``tmp_path`` — the "copy" a
mutating test needs is what the builder gives it for free — so nothing here
edits ``tests/plexus_fixtures.py`` or any checked-in file. The fixture module
is consumed strictly read-only.

The clock, and the ambient environment
======================================
Two things a verdict must never depend on. The staleness window is real time,
so the fixture's in-progress session becomes ``record.session-stale`` (INFO,
never blocking) once the tree's timestamps age past it; the one assertion that
pins the finding list EXACTLY therefore goes through the importable core with a
fixed ``now``, and the CLI assertions state what is true on any day — exit 0,
no ERROR, no WARN. The work-repo URL variables are read by the CLI only when
the validated path IS ``$LMER_WORK_REPO_PATH``, which a tmp tree never is;
:meth:`TestGreenEndToEnd.test_the_green_verdict_ignores_the_ambient_environment`
asserts that rather than clearing them, because a fixture that hides a variable
the product does not handle is a finding waiting to be written.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from lmer_schema import cli
from lmer_schema.checks import CHECKS, Severity
from lmer_schema.checks import walk
from lmer_schema.checks.findings import EXIT_CLEAN, EXIT_ERRORS, EXIT_UNUSABLE
from lmer_schema.checks.runner import CHECK_ORDER, validate
from lmer_schema.registry import (
    MISS,
    REGISTRY_HIT,
    is_short_name,
    load_registry,
    resolve_project_dir,
)
from tests import plexus_fixtures as pf

#: Inside both sessions' staleness window (the second is ``in-progress``,
#: ``updated`` 2026-08-24T11:32Z), so a run with this ``now`` says nothing
#: about staleness however long after 2026 the suite is run.
FIXED_NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)

#: Far enough past that window that the in-progress session IS stale — the
#: probe that proves the record check read its state file.
LATE_NOW = FIXED_NOW + timedelta(days=90)

#: What a conformant schema-form plexus reports: ONE informational finding,
#: for the host bucket the fixture carries on purpose (D9 — an unnamed repo's
#: work, awaiting adoption, which never blocks). Anything else appearing here
#: is a new statement about the form and belongs in a review, not in a diff.
CLEAN_FINDINGS = (("tree.host-bucket", "INFO"),)

#: One defect per check family: the family, the code it must produce and the
#: repo-relative path it must be reported against. The tests below inject them
#: one at a time; :class:`TestOneDefectPerCheckFamily` also reads this table to
#: assert the six span the six check modules.
FAMILY_CODES = {
    "manifest": "manifest.schema-not-int",
    "registry": "registry.invalid",
    "layout": "tree.bucket-collision",
    "index_links": "index.dangling-link",
    "agents": "agents.memory-entry-without-file",
    "record": "record.state-unparseable",
}

#: The two memory scopes (D17), as ``(scope, tree attribute, index path)``.
#: The instance scope lives at the plexus root, the project scope inside the
#: flat short-name project dir — the pair G4 asks to see exercised.
MEMORY_SCOPES = (
    ("instance", "instance_memory_dir", "_agents/memory/MEMORY.md"),
    ("project", "project_memory_dir", f"{pf.PROJECT}/_agents/memory/MEMORY.md"),
)


# ---------------------------------------------------------------------------
# Running the validator
# ---------------------------------------------------------------------------


def _cli_json(capsys, tree, *flags):
    """``lmer-schema validate <root> --json`` -> ``(exit_code, document)``.

    The CLI is a function of argv (no subprocess anywhere in this file), and
    the JSON document is the consumed contract, so the negative half asserts
    against the same shape the hosted connector reads.
    """
    code = cli.main(["validate", str(tree.root), "--json", *flags])
    captured = capsys.readouterr()
    assert captured.err == "", captured.err
    return code, json.loads(captured.out)


def _findings(document, code):
    return [finding for finding in document["findings"] if finding["code"] == code]


def _codes(document):
    return [finding["code"] for finding in document["findings"]]


def _only_error(document, code, path):
    """The one ERROR *code* at *path*, asserting it is the ONLY error.

    A mutation that broke something else as well would let a test pass for the
    wrong reason: the code it looked for would be there, planted by a defect it
    did not mean to inject.
    """
    errors = [f for f in document["findings"] if f["severity"] == "ERROR"]
    assert [f["code"] for f in errors] == [code], _codes(document)
    assert errors[0]["path"] == path
    return errors[0]


def _edit_manifest(tree, mutate):
    """Round-trip ``_plexus.yaml`` through *mutate* (comments are not kept —
    the manifest's shape is what these tests change, never its prose)."""
    document = yaml.safe_load(tree.manifest.read_text(encoding="utf-8"))
    mutate(document)
    tree.manifest.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _dangle_memory_entry(memory_dir):
    """Append a ``MEMORY.md`` entry whose memory file does not exist."""
    index = memory_dir / "MEMORY.md"
    index.write_text(
        index.read_text(encoding="utf-8") + "- [gone](user_gone.md) — nothing here.\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# The positive half: the verdict
# ---------------------------------------------------------------------------


class TestGreenEndToEnd:
    def test_the_schema_fixture_validates_green_through_the_cli(
        self, capsys, schema_plexus
    ):
        # G4's headline: the shipped command, the shipped exit taxonomy.
        code, document = _cli_json(capsys, schema_plexus)
        assert code == EXIT_CLEAN
        assert document["exit_code"] == EXIT_CLEAN
        assert document["counts"]["error"] == 0
        assert [f for f in document["findings"] if f["severity"] == "ERROR"] == []

    def test_it_is_not_warned_about_either(self, capsys, schema_plexus):
        # Warnings never block (D14), so a WARN would not move the exit code —
        # which is exactly why it has to be asserted separately. A conformant
        # tree earns none: every remaining finding is informational.
        _code, document = _cli_json(capsys, schema_plexus)
        assert document["counts"]["warn"] == 0
        assert {f["severity"] for f in document["findings"]} <= {"INFO"}

    def test_the_text_report_says_conformant(self, capsys, schema_plexus):
        code = cli.main(["validate", str(schema_plexus.root)])
        out = capsys.readouterr().out
        assert code == EXIT_CLEAN
        assert "conformant" in out and "not conformant" not in out

    def test_the_run_was_a_full_schema_form_run(self, capsys, schema_plexus):
        # A short report is never a clean tree: exit 0 means every check ran,
        # not that the run stopped early or a check crashed.
        _code, document = _cli_json(capsys, schema_plexus)
        assert document["form"] == "schema"
        assert document["unusable"] is None
        assert "runner.checks-skipped" not in _codes(document)
        assert "runner.check-failed" not in _codes(document)

    def test_the_only_finding_is_the_bucket_the_fixture_carries_on_purpose(
        self, schema_plexus
    ):
        # Through the core with a fixed `now`, so the exact list is a fact
        # about the tree and not about the day the suite runs (the staleness
        # window is the one check that reads the clock).
        report = validate(schema_plexus.root, now=FIXED_NOW)
        assert report.exit_code == EXIT_CLEAN
        assert tuple(
            (finding.code, finding.severity.value) for finding in report.findings
        ) == CLEAN_FINDINGS

    def test_the_green_verdict_ignores_the_ambient_environment(
        self, capsys, monkeypatch, schema_plexus, tmp_path
    ):
        # The work-repo URL variables arm the manifest's same-host `sources:`
        # guard, and a container running this suite really has them set. They
        # apply only when the validated path IS the work repo, so the same tree
        # cannot be conformant on one machine and not on another.
        _code, before = _cli_json(capsys, schema_plexus)
        monkeypatch.setenv("LMER_WORK_REPO", "https://elsewhere.example.net/group/plexus")
        monkeypatch.setenv("LMER_REPO_URL", "https://elsewhere.example.net/group/plexus")
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(tmp_path / "some-other-checkout"))
        code, after = _cli_json(capsys, schema_plexus)
        assert code == EXIT_CLEAN
        assert after == before


# ---------------------------------------------------------------------------
# The positive half: the fixture really exercises the form
# ---------------------------------------------------------------------------


class TestTheMultiRemoteEntryResolves:
    """D8: one project, a primary ``repo:`` and additional ``remotes:``, all
    resolving to the ONE flat project dir the validator walked."""

    @pytest.mark.parametrize(
        "spelling",
        [
            pf.PRIMARY_REPO,
            f"https://{pf.PRIMARY_REPO}.git",
            pf.REMOTES[0],
            f"HTTPS://{pf.REMOTES[0].split('/', 1)[0].upper()}/"
            f"{pf.REMOTES[0].split('/', 1)[1]}/",
            pf.REMOTES[1],
        ],
        ids=["primary", "primary-decorated", "remote-1", "remote-1-shouty", "remote-2"],
    )
    def test_every_remote_resolves_to_the_one_project_dir(self, schema_plexus, spelling):
        # Normalization (D21) is doing the work on the decorated spellings: a
        # scheme, a `.git` suffix, a trailing slash and case are the same repo.
        resolution = resolve_project_dir(schema_plexus.root, spelling)
        assert resolution.how == REGISTRY_HIT
        assert resolution.project == pf.PROJECT
        assert resolution.path == schema_plexus.project_dir

    def test_a_remote_the_manifest_does_not_list_does_not_resolve_to_it(
        self, schema_plexus
    ):
        # The control: resolution is a lookup, not a rubber stamp. Without it
        # the parametrized test above would pass against a registry that
        # matched everything.
        resolution = resolve_project_dir(
            schema_plexus.root, f"https://{pf.BUCKET_HOST}/group/not-a-project"
        )
        assert resolution.how == MISS
        assert resolution.path is None

    def test_the_registry_check_really_read_those_remotes(self, capsys, schema_plexus):
        # The probe: the fixture's remotes pass the canonical-spelling check
        # because they were CHECKED, not because nothing looked. Respell one
        # and the same run reports it.
        def respell(document):
            entry = document["projects"][pf.PROJECT]
            entry["remotes"][0] = f"https://{pf.REMOTES[0]}.git"

        _edit_manifest(schema_plexus, respell)
        code, document = _cli_json(capsys, schema_plexus)
        assert code == EXIT_CLEAN  # a spelling nudge never blocks
        warning = _findings(document, "registry.non-canonical-remote")
        assert len(warning) == 1
        assert warning[0]["severity"] == "WARN"


class TestTheRepolessEntryResolves:
    """D19: a project with a directory, a description and no remote at all."""

    def test_it_resolves_by_name(self, schema_plexus):
        registry = load_registry(schema_plexus.root)
        project = registry[pf.REPOLESS_PROJECT]
        assert project.repoless
        assert project.description == pf.REPOLESS_DESCRIPTION
        assert project.dir(schema_plexus.root) == schema_plexus.repoless_dir
        assert schema_plexus.repoless_dir.is_dir()

    def test_it_never_matches_a_remote_lookup(self, schema_plexus):
        # A repoless entry contributes NO keys to the union (§4), so it can
        # never be the answer to "where does this repo live".
        registry = load_registry(schema_plexus.root)
        assert registry[pf.REPOLESS_PROJECT].keys == ()
        for spelling in (pf.PRIMARY_REPO, *pf.REMOTES):
            assert registry.lookup(spelling).name == pf.PROJECT

    def test_the_registry_check_really_read_it(self, capsys, schema_plexus):
        # The probe: a repoless entry is legal, so nothing about the green run
        # proves the entry was examined. Remove what makes it legible and the
        # same run says so.
        _edit_manifest(
            schema_plexus,
            lambda doc: doc["projects"][pf.REPOLESS_PROJECT].pop("description"),
        )
        _code, document = _cli_json(capsys, schema_plexus)
        assert [f["severity"] for f in _findings(
            document, "registry.repoless-without-description"
        )] == ["WARN"]


class TestFlatShortNameProjectDirs:
    """§2: identity lives in the registry, so a project dir is one flat
    short-name directory holding ``_``-named areas."""

    def test_the_walk_returns_the_flat_registered_dirs(self, schema_plexus):
        # The shared walk (checks/walk.py) is what the runner hands to every
        # per-project check, so this is what the validator actually saw.
        registry = load_registry(schema_plexus.root)
        projects = walk.project_dirs(schema_plexus.root, registry=registry)
        registered = {p.rel: p for p in projects if p.kind == walk.REGISTERED}
        assert set(registered) == {pf.PROJECT, pf.REPOLESS_PROJECT}
        for rel, project in registered.items():
            assert "/" not in rel  # flat: one segment, not {host}/{project}
            assert is_short_name(rel)
            assert project.path == schema_plexus.root / rel

    def test_the_project_areas_are_the_underscore_namespace(self, schema_plexus):
        # D6: inside a project dir every area name the schema owns is `_`-named
        # — the tree the validator called conformant has no bare legacy twin.
        names = {child.name for child in schema_plexus.project_dir.iterdir()}
        assert names == {"_runs", "_specs", "_plans", "_agents", "_info", "_taskdef"}

    def test_the_layout_check_reaches_inside_a_flat_project_dir(
        self, capsys, schema_plexus
    ):
        # The probe: plant an unknown `_` name where the check must see it if
        # it descends the flat dir at all.
        (schema_plexus.project_dir / "_bogus").mkdir()
        _code, document = _cli_json(capsys, schema_plexus)
        unknown = _findings(document, "tree.unknown-reserved-name")
        assert [(f["severity"], f["path"]) for f in unknown] == [
            ("WARN", f"{pf.PROJECT}/_bogus")
        ]


class TestAgentsAtBothScopes:
    """D17/D18: ``_agents/`` at the instance root AND inside the project, each
    with a memory file its index carries."""

    @pytest.mark.parametrize(
        "scope,attribute,index", MEMORY_SCOPES, ids=["instance", "project"]
    )
    def test_each_scope_holds_an_indexed_memory_file(
        self, schema_plexus, scope, attribute, index
    ):
        memory_dir = getattr(schema_plexus, attribute)
        assert memory_dir.is_dir()
        assert (schema_plexus.root / index).is_file()
        memories = [p.name for p in memory_dir.glob("*.md") if p.name != "MEMORY.md"]
        assert memories, f"{scope} scope has no memory file"
        assert schema_plexus.memory_files[
            "user" if scope == "instance" else "project"
        ].name in memories

    # The proof that BOTH scopes are checked rather than merely present is the
    # scope-parametrized defect in TestOneDefectPerCheckFamily: an unresolvable
    # index entry is an ERROR at whichever scope it is planted in.


class TestTheTwoSessionDirs:
    """D22: ``_runs/<slug>/`` groups the launches; each session owns its own
    directory and nothing is written at the grouping level."""

    def test_the_group_holds_exactly_the_two_session_dirs(self, schema_plexus):
        group = schema_plexus.runs_dir / schema_plexus.slug
        assert sorted(p.name for p in group.iterdir()) == sorted(pf.SESSION_DIR_NAMES)
        assert {p.parent for p in schema_plexus.session_dirs} == {group}

    @pytest.mark.parametrize("index", [0, 1], ids=["first", "second"])
    def test_each_session_dir_is_subject_to_the_shape_check(
        self, capsys, schema_plexus, index
    ):
        # The probe, run against EACH session dir in turn: the D22 session-unit
        # shape rule is what the two dirs pass, and a rule only one of them is
        # measured against would be no rule at all. `<session-id>[--<name>]` is
        # the form; a dir named anything else is reported at its own path.
        session = schema_plexus.session_dirs[index]
        renamed = session.parent / "not-a-session-id"
        session.rename(renamed)
        _code, document = _cli_json(capsys, schema_plexus)
        malformed = _findings(document, "record.session-id-malformed")
        assert [(f["severity"], f["path"]) for f in malformed] == [
            ("WARN", f"{pf.PROJECT}/_runs/{pf.SLUG}/not-a-session-id")
        ]

    def test_nothing_may_be_written_at_the_grouping_level(self, capsys, schema_plexus):
        # The other half of D22: the grouping dir itself holds session dirs and
        # nothing else — and the two real ones are not reported by that rule.
        (schema_plexus.runs_dir / schema_plexus.slug / "notes.md").write_text(
            "stray\n", encoding="utf-8"
        )
        _code, document = _cli_json(capsys, schema_plexus)
        stray = _findings(document, "record.grouping-level-entry")
        assert [(f["severity"], f["path"]) for f in stray] == [
            ("WARN", f"{pf.PROJECT}/_runs/{pf.SLUG}/notes.md")
        ]

    def test_the_sessions_are_independent_records(self, schema_plexus):
        # Each session dir carries its OWN state file and its own event log
        # (the legacy twin interleaves both sessions in one dir), and the
        # record check reads each: with a `now` past the staleness window only
        # the in-progress session is called possibly orphaned.
        report = validate(schema_plexus.root, now=LATE_NOW)
        stale = [f for f in report.findings if f.code == "record.session-stale"]
        assert [f.path for f in stale] == [
            f"{pf.PROJECT}/_runs/{pf.SLUG}/{pf.SESSION_DIR_NAMES[1]}"
        ]
        assert report.exit_code == EXIT_CLEAN  # informational: it never blocks


# ---------------------------------------------------------------------------
# The negative half: one defect per check family
# ---------------------------------------------------------------------------


class TestOneDefectPerCheckFamily:
    """Green means checked. Each test breaks ONE thing in a fresh tree and
    pins the code, the severity at the schema-form tier, the path and the exit
    code — so a check that stopped firing fails here instead of leaving the
    tree above quietly green."""

    def test_the_six_families_span_the_six_check_modules(self):
        # The ledger: one family per check module in the runner's pipeline, so
        # no module can drop out of this file unnoticed. (`layout` owns the
        # `tree.*` namespace and `index_links` the `index.*` one.)
        assert len(FAMILY_CODES) == len(CHECK_ORDER)
        assert {check.name for check in CHECK_ORDER} == set(FAMILY_CODES)
        for code in FAMILY_CODES.values():
            assert code in CHECKS

    # -- manifest -----------------------------------------------------------

    @pytest.mark.parametrize("value", ["1", True], ids=["string", "bool"])
    def test_a_schema_that_is_not_an_int_is_unusable_input(
        self, capsys, schema_plexus, value
    ):
        # A YAML bool is not an int (D15's isinstance trap), and a manifest
        # nobody can read is exit 2, not exit 1: there is no tree to judge.
        _edit_manifest(schema_plexus, lambda doc: doc.update(schema=value))
        code, document = _cli_json(capsys, schema_plexus)
        assert code == EXIT_UNUSABLE
        assert document["unusable"]
        _only_error(document, FAMILY_CODES["manifest"], pf.MANIFEST_NAME)
        # The downstream checks did not run, and the report says so rather than
        # reading as a short clean list.
        assert "runner.checks-skipped" in _codes(document)

    # -- registry -----------------------------------------------------------

    def test_one_repo_spelled_twice_in_one_entry_is_a_registry_error(
        self, capsys, schema_plexus
    ):
        # §4: the same normalized remote twice inside one entry. The second
        # spelling is decorated, so it is NORMALIZATION that catches it — two
        # byte-different strings naming one repo.
        _edit_manifest(
            schema_plexus,
            lambda doc: doc["projects"][pf.PROJECT]["remotes"].append(
                f"https://{pf.PRIMARY_REPO}.git"
            ),
        )
        code, document = _cli_json(capsys, schema_plexus)
        assert code == EXIT_ERRORS
        finding = _only_error(document, FAMILY_CODES["registry"], pf.MANIFEST_NAME)
        assert pf.PROJECT in finding["message"]
        # Rule 3 of the runner: an unusable registry is exit 1, not exit 2 —
        # the tree still says true things about everything but identity.
        assert document["unusable"] is None

    # -- layout -------------------------------------------------------------

    def test_a_real_directory_at_a_registered_remotes_bucket_path_collides(
        self, capsys, schema_plexus
    ):
        # D9/§9: `{host}/{project}/` for a REGISTERED remote is the split-tree
        # defect — the same project addressed two ways, with two records.
        (schema_plexus.root / pf.PRIMARY_REPO / "_runs").mkdir(parents=True)
        code, document = _cli_json(capsys, schema_plexus)
        assert code == EXIT_ERRORS
        _only_error(document, FAMILY_CODES["layout"], pf.PRIMARY_REPO)

    # -- index_links --------------------------------------------------------

    def test_a_dangling_specs_symlink_is_an_error_in_schema_form(
        self, capsys, schema_plexus
    ):
        # One of the two live defects G7 repairs. The `_specs/` entry is a
        # relative symlink into a session dir; delete what it points at.
        entry = schema_plexus.specs_entries[0]
        (schema_plexus.session_dirs[0] / "spec.md").unlink()
        code, document = _cli_json(capsys, schema_plexus)
        assert code == EXIT_ERRORS
        _only_error(
            document,
            FAMILY_CODES["index_links"],
            entry.relative_to(schema_plexus.root).as_posix(),
        )
        # ERROR here because this tree is schema form: the code IS downgraded
        # in a legacy tree (D14), which is what makes the tier assertion mean
        # something. The legacy side is tests/test_lmer_schema_legacy_tier.py.
        assert CHECKS[FAMILY_CODES["index_links"]].legacy is Severity.WARN

    # -- agents -------------------------------------------------------------

    @pytest.mark.parametrize(
        "scope,attribute,index", MEMORY_SCOPES, ids=["instance", "project"]
    )
    def test_a_memory_entry_without_a_file_is_an_error_at_either_scope(
        self, capsys, schema_plexus, scope, attribute, index
    ):
        # The other live defect from G7, injected at EACH of the two D17
        # scopes: both `_agents/memory/` surfaces are checked, and neither is
        # the one the validator happens to look at.
        _dangle_memory_entry(getattr(schema_plexus, attribute))
        code, document = _cli_json(capsys, schema_plexus)
        assert code == EXIT_ERRORS
        _only_error(document, FAMILY_CODES["agents"], index)
        assert CHECKS[FAMILY_CODES["agents"]].legacy is Severity.WARN

    # -- record -------------------------------------------------------------

    @pytest.mark.parametrize("session", [0, 1], ids=["first", "second"])
    def test_a_corrupt_state_file_is_an_error_in_either_session(
        self, capsys, schema_plexus, session
    ):
        # Both session dirs' records are parsed, not just the first one found.
        state = schema_plexus.session_dirs[session] / "state.yaml"
        state.write_text("schema: 1\nslug: [unterminated\n", encoding="utf-8")
        code, document = _cli_json(capsys, schema_plexus)
        assert code == EXIT_ERRORS
        _only_error(
            document,
            FAMILY_CODES["record"],
            state.relative_to(schema_plexus.root).as_posix(),
        )
        # A state-file SHAPE violation is ERROR wherever it lives (D14): this
        # code is NOT tier-sensitive, so the lenient tier can never be the
        # reason a corrupt state file passes.
        assert not CHECKS[FAMILY_CODES["record"]].tier_sensitive
