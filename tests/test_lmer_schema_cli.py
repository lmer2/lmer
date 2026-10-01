"""``lmer_schema.cli`` — the ``lmer-schema`` command surface (spec §7, D14/D15;
SCHEMA.md "Validator"; issue #353).

The CLI owns no check logic: it parses arguments, calls
``lmer_schema.checks.runner.validate`` once and renders the one
:class:`~lmer_schema.checks.findings.Report` it gets back. What these tests pin
is therefore the SURFACE, and every part of it is a contract somebody else
depends on:

- the EXIT TAXONOMY — ``0`` clean (warnings never block, D14), ``1`` errors,
  ``2`` unusable input — the shape of ``work plan check`` and deliberately not
  the ``0/2/64`` codes of ``lmer_cli.container.sources``. A usage error is
  unusable input too (``2``), and never writes to stdout;
- the JSON DOCUMENT, which the hosted connector consumes: an exact key set, the
  finding keys as ``Report.to_dict`` spells them, and never an absolute path —
  the document is copied around, so a machine path in it is a leak;
- the TEXT output, which is an ordered fix-list: numbered, most severe first,
  each entry naming its code and its repo-relative path. That list is what a
  legacy tree's output is FOR (D16: a migration aid, not a gate);
- the VERDICT never depending on the ambient environment: the work-repo URL
  variables are read only when the validated path IS the work repo
  (``$LMER_WORK_REPO_PATH``), so the same tree cannot be conformant on one
  machine and not on another;
- ``--list-checks``, the machine-readable inventory docs/SCHEMA.md's strictness
  table is reconciled against — one line per registered code, severity in the
  second field, every check module's namespace present (a module nobody
  imported would silently drop its codes from the inventory), and no code
  advertised that cannot fire at this lmer's schema support;
- the console entry ``lmer-schema = lmer_schema.cli:main`` really resolving,
  and ``main(argv)`` being callable with a list so none of this needs a
  subprocess. ``python -m lmer_schema.cli`` is the same command before an
  editable reinstall puts the script on PATH.
"""

import importlib
import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml

from lmer_schema.checks import CHECKS, Form, Report, Severity, Tier
from lmer_schema.checks.findings import EXIT_CLEAN, EXIT_ERRORS, EXIT_UNUSABLE
from lmer_schema.checks.record import STALE_AFTER_SECONDS
from lmer_schema.manifest import CURRENT_SCHEMA, SUPPORTED_SCHEMAS
from lmer_schema import cli
from tests import plexus_fixtures as pf

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A remote on a host the fixture's `sources:` entries do not share — the
#: input T8's same-host guard needs to say anything at all.
FOREIGN_REPO_URL = "https://elsewhere.example.net/group/plexus"

#: One rendered fix-list entry: `  1. ERROR  <code>  <repo-relative path>`.
ENTRY_RE = re.compile(r"^\s*(\d+)\. (ERROR|WARN|INFO)\s+(\S+)\s+(\S+)$")

#: One `--list-checks` row: the code first, its severity second (the shape the
#: task's verify command greps for).
CHECK_ROW_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s+(ERROR|WARN|INFO)\s+(\S+)\s+(.+)$")


@pytest.fixture(autouse=True)
def _no_ambient_plexus_url(monkeypatch):
    """``--plexus-repo-url``'s environment fallback reads the work-repo
    variables, which a container running these tests really has set. Clear all
    three so a test's verdict never depends on the machine it runs on; the
    tests that own the fallback set them themselves.

    The CLI now has that property too — the fallback only applies when the
    validated path IS ``$LMER_WORK_REPO_PATH`` — and
    :class:`TestPassThrough` asserts it rather than leaning on this fixture.
    """
    for name in ("LMER_WORK_REPO", "LMER_REPO_URL", "LMER_WORK_REPO_PATH"):
        monkeypatch.delenv(name, raising=False)


def _run(capsys, *argv):
    """``main(argv)`` -> ``(exit_code, stdout, stderr)``."""
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _json(capsys, *argv):
    code, out, err = _run(capsys, *argv)
    return code, json.loads(out), err


def _entries(text):
    """The fix-list entries of a text report, in printed order."""
    return [ENTRY_RE.match(line).groups() for line in text.splitlines() if ENTRY_RE.match(line)]


def _break_manifest(tree, text="schema: 1\nname: [unterminated\n"):
    tree.manifest.write_text(text, encoding="utf-8")


def _edit_manifest(tree, mutate):
    doc = yaml.safe_load(tree.manifest.read_text(encoding="utf-8"))
    mutate(doc)
    tree.manifest.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _dangling_memory_entry(memory_dir):
    """A ``MEMORY.md`` entry with no file: ERROR in schema form (D14)."""
    memory_dir.mkdir(parents=True, exist_ok=True)
    index = memory_dir / "MEMORY.md"
    existing = index.read_text(encoding="utf-8") if index.is_file() else ""
    index.write_text(existing + "- [gone](project_gone.md) — nothing here.\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


class TestEntryPoint:
    def test_the_declared_console_entry_resolves(self):
        # pyproject declares `lmer-schema = lmer_schema.cli:main` (core owns
        # that file; this subsystem never edits it). Asserting the DECLARATION
        # is the package test's job — this asserts the target really resolves,
        # which is what a fresh install then puts on PATH.
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        target = pyproject["project"]["scripts"]["lmer-schema"]
        module_name, _, attribute = target.partition(":")
        entry = getattr(importlib.import_module(module_name), attribute)
        assert callable(entry)
        assert entry is cli.main

    def test_main_takes_an_argv_list_and_returns_an_int(self, capsys):
        # No subprocess anywhere in this file: the CLI is a function of argv.
        code, out, _err = _run(capsys, "--help")
        assert code == EXIT_CLEAN
        assert isinstance(code, int)
        assert "validate" in out

    def test_help_does_not_escape_as_an_exception(self):
        # argparse exits the process for --help; main() turns that back into a
        # return value so `main(["--help"])` is callable in-process.
        assert cli.main(["--help"]) == EXIT_CLEAN

    def test_the_module_runs_as_python_m(self):
        # The console script is only on PATH after an editable reinstall, so
        # `python -m lmer_schema.cli` is the spelling that always works.
        source = Path(cli.__file__).read_text(encoding="utf-8")
        assert 'if __name__ == "__main__":' in source
        assert "raise SystemExit(main())" in source

    def test_help_documents_both_outputs_and_the_exit_codes(self, capsys):
        _code, out, _err = _run(capsys, "--help")
        for promise in ("--json", "--list-checks", "validate"):
            assert promise in out
        for code in ("0", "1", "2"):
            assert code in out

    def test_help_never_promises_a_finding_that_cannot_happen(self, capsys):
        # At schema 1 there is no supported OLDER int, so `manifest.schema-older`
        # is unreachable; the help text must not advertise a WARN for it.
        _code, out, _err = _run(capsys, "--help")
        assert "schema-older" not in out


# ---------------------------------------------------------------------------
# Usage errors
# ---------------------------------------------------------------------------


class TestUsage:
    @pytest.mark.parametrize(
        "argv",
        [
            (),                      # no command at all
            ("validate",),           # validate needs its path
            ("nonsense",),           # not a command
            ("--list-checks", "validate", "."),  # the inventory takes no command
        ],
        ids=["no-command", "no-path", "unknown-command", "list-checks-with-command"],
    )
    def test_a_bad_invocation_is_exit_2_on_stderr(self, capsys, argv):
        code, out, err = _run(capsys, *argv)
        assert code == EXIT_UNUSABLE
        # Nothing on stdout: a consumer piping stdout to a JSON parser sees
        # either one valid document or nothing at all.
        assert out == ""
        assert err.strip()

    def test_a_usage_error_names_the_command(self, capsys):
        _code, _out, err = _run(capsys, "nonsense")
        assert "lmer-schema" in err


# ---------------------------------------------------------------------------
# The exit taxonomy
# ---------------------------------------------------------------------------


class TestExitTaxonomy:
    def test_a_clean_schema_tree_is_zero(self, capsys, schema_plexus):
        code, out, err = _run(capsys, "validate", str(schema_plexus.root))
        assert code == EXIT_CLEAN
        assert err == ""
        assert out

    def test_a_legacy_tree_is_zero_too(self, capsys, legacy_plexus):
        # G3/D16: the live legacy tree validates unmodified — the lenient tier
        # plus "warnings never block" (D14).
        code, _out, _err = _run(capsys, "validate", str(legacy_plexus.root))
        assert code == EXIT_CLEAN

    def test_warnings_alone_never_block(self, capsys, schema_plexus):
        _edit_manifest(schema_plexus, lambda doc: doc.update(colour="blue"))
        code, _doc, _err = _json(capsys, "validate", str(schema_plexus.root), "--json")
        assert code == EXIT_CLEAN

    def test_an_error_is_exit_1(self, capsys, schema_plexus):
        _dangling_memory_entry(schema_plexus.project_memory_dir)
        code, _out, _err = _run(capsys, "validate", str(schema_plexus.root))
        assert code == EXIT_ERRORS

    def test_a_missing_path_is_unusable_input(self, capsys, tmp_path):
        code, _out, _err = _run(capsys, "validate", str(tmp_path / "nope"))
        assert code == EXIT_UNUSABLE

    def test_a_broken_manifest_is_unusable_input(self, capsys, schema_plexus):
        _break_manifest(schema_plexus)
        code, _out, _err = _run(capsys, "validate", str(schema_plexus.root))
        assert code == EXIT_UNUSABLE

    def test_an_unsupported_schema_int_is_unusable_input(self, capsys, schema_plexus):
        # Newer OR older: at schema 1 `SUPPORTED_SCHEMAS` is `(1,)`, so
        # `schema: 0` is unusable exactly like `schema: 99` — never a WARN.
        for value in (99, 0):
            _edit_manifest(schema_plexus, lambda doc, v=value: doc.update(schema=v))
            code, doc, _err = _json(capsys, "validate", str(schema_plexus.root), "--json")
            assert code == EXIT_UNUSABLE, value
            assert doc["unusable"]
            assert "manifest.schema-older" not in [f["code"] for f in doc["findings"]]

    def test_the_returned_code_is_the_reports_own(self, capsys, schema_plexus):
        _dangling_memory_entry(schema_plexus.project_memory_dir)
        code, doc, _err = _json(capsys, "validate", str(schema_plexus.root), "--json")
        assert code == doc["exit_code"] == EXIT_ERRORS

    def test_a_broken_tree_never_raises(self, capsys, schema_plexus):
        # Whatever the tree looks like, the CLI answers with an exit code.
        _break_manifest(schema_plexus, ": not a mapping\n")
        code, _out, _err = _run(capsys, "validate", str(schema_plexus.root))
        assert code in (EXIT_CLEAN, EXIT_ERRORS, EXIT_UNUSABLE)


# ---------------------------------------------------------------------------
# The JSON document: a consumed contract
# ---------------------------------------------------------------------------


class TestJsonDocument:
    #: The document's keys, exactly. Five come from ``Report.to_dict`` (which
    #: pins them); the CLI adds the two envelope keys and nothing else.
    KEYS = {"schema", "tool", "form", "exit_code", "unusable", "counts", "findings"}
    FINDING_KEYS = {"code", "severity", "path", "message", "detail"}

    def test_the_top_level_keys_are_exactly_these(self, capsys, schema_plexus):
        _code, doc, _err = _json(capsys, "validate", str(schema_plexus.root), "--json")
        assert set(doc) == self.KEYS

    def test_the_envelope_identifies_the_document(self, capsys, schema_plexus):
        _code, doc, _err = _json(capsys, "validate", str(schema_plexus.root), "--json")
        assert doc["schema"] == cli.OUTPUT_SCHEMA == 1
        assert doc["tool"] == "lmer-schema"

    def test_the_body_is_the_reports_own_document(self, capsys, schema_plexus):
        _dangling_memory_entry(schema_plexus.project_memory_dir)
        _code, doc, _err = _json(capsys, "validate", str(schema_plexus.root), "--json")
        from lmer_schema.checks.runner import validate as run_validate

        report = run_validate(schema_plexus.root)
        body = report.to_dict()
        for key in ("form", "exit_code", "unusable", "counts", "findings"):
            assert doc[key] == body[key], key

    def test_every_finding_has_the_contract_keys(self, capsys, schema_plexus):
        _dangling_memory_entry(schema_plexus.project_memory_dir)
        _code, doc, _err = _json(capsys, "validate", str(schema_plexus.root), "--json")
        assert doc["findings"]
        for finding in doc["findings"]:
            assert set(finding) == self.FINDING_KEYS
            assert finding["severity"] in {s.value for s in Severity}
            assert finding["code"] in CHECKS

    def test_the_counts_summarise_the_findings(self, capsys, legacy_plexus):
        _code, doc, _err = _json(capsys, "validate", str(legacy_plexus.root), "--json")
        assert set(doc["counts"]) == {"error", "warn", "info"}
        assert sum(doc["counts"].values()) == len(doc["findings"])

    def test_the_form_is_reported(self, capsys, schema_plexus, legacy_plexus):
        for tree, form in ((schema_plexus, "schema"), (legacy_plexus, "legacy")):
            _code, doc, _err = _json(capsys, "validate", str(tree.root), "--json")
            assert doc["form"] == form

    def test_stdout_is_exactly_one_json_document(self, capsys, legacy_plexus):
        code, out, err = _run(capsys, "validate", str(legacy_plexus.root), "--json")
        assert json.loads(out)["exit_code"] == code
        assert err == ""
        # One document, not a stream: nothing else is printed alongside it.
        assert out.strip().startswith("{") and out.strip().endswith("}")

    @pytest.mark.parametrize("fixture", ["schema_plexus", "legacy_plexus"])
    def test_no_path_in_the_document_is_ever_absolute(self, capsys, fixture, request):
        # The document is copied around (the hosted connector stores it), so it
        # carries the tree's repo-relative paths and never a machine path —
        # not in a finding, not in the envelope.
        tree = request.getfixturevalue(fixture)
        _dangling_memory_entry(tree.project_memory_dir)
        _code, out, _err = _run(capsys, "validate", str(tree.root), "--json")
        doc = json.loads(out)
        assert str(tree.root) not in out
        for finding in doc["findings"]:
            assert not finding["path"].startswith("/")
            assert not (finding["detail"] or "").startswith("/")

    def test_the_json_flag_reads_either_side_of_the_command(self, capsys, schema_plexus):
        for argv in (
            ("validate", str(schema_plexus.root), "--json"),
            ("--json", "validate", str(schema_plexus.root)),
        ):
            _code, doc, _err = _json(capsys, *argv)
            assert doc["tool"] == "lmer-schema"

    def test_unusable_input_is_still_a_document(self, capsys, tmp_path):
        # Exit 2 is not "no answer": the connector gets the same shape, with
        # the reason spelled out.
        code, doc, _err = _json(capsys, "validate", str(tmp_path / "nope"), "--json")
        assert code == EXIT_UNUSABLE
        assert doc["exit_code"] == EXIT_UNUSABLE
        assert isinstance(doc["unusable"], str) and doc["unusable"]
        assert [f["code"] for f in doc["findings"]] == ["runner.no-such-path"]

    def test_schema_one_has_only_the_upgrade_warning(self, capsys, schema_plexus):
        _code, doc, _err = _json(capsys, "validate", str(schema_plexus.root), "--json")
        assert doc["unusable"] is None
        assert doc["counts"]["error"] == 0
        assert [f["code"] for f in doc["findings"] if f["severity"] == "WARN"] == ["manifest.schema-older"]


# ---------------------------------------------------------------------------
# The text output: an ordered fix-list
# ---------------------------------------------------------------------------


class TestTextOutput:
    def test_the_header_names_the_target_and_the_form(self, capsys, schema_plexus):
        _code, out, _err = _run(capsys, "validate", str(schema_plexus.root))
        first = out.splitlines()[0]
        assert str(schema_plexus.root) in first
        assert "schema" in first

    def test_the_findings_are_a_numbered_list(self, capsys, legacy_plexus):
        _dangling_memory_entry(legacy_plexus.project_memory_dir)
        _code, out, _err = _run(capsys, "validate", str(legacy_plexus.root))
        entries = _entries(out)
        assert entries
        assert [int(number) for number, *_ in entries] == list(range(1, len(entries) + 1))

    def test_the_list_is_ordered_most_severe_first(self, capsys, schema_plexus):
        # That order is what makes it a fix-list: work down it and the blocking
        # findings are done first.
        _dangling_memory_entry(schema_plexus.project_memory_dir)
        _code, out, _err = _run(capsys, "validate", str(schema_plexus.root))
        ranks = [Severity(severity).rank for _n, severity, _c, _p in _entries(out)]
        assert ranks == sorted(ranks)
        assert Severity.ERROR.rank in ranks

    def test_every_finding_is_listed_once_with_its_code_and_path(
        self, capsys, schema_plexus
    ):
        from lmer_schema.checks.runner import validate as run_validate

        _dangling_memory_entry(schema_plexus.project_memory_dir)
        _code, out, _err = _run(capsys, "validate", str(schema_plexus.root))
        printed = [(code, path) for _n, _s, code, path in _entries(out)]
        report = run_validate(schema_plexus.root)
        assert printed == [(f.code, f.path) for f in report.findings]

    def test_each_entry_carries_its_message(self, capsys, schema_plexus):
        from lmer_schema.checks.runner import validate as run_validate

        _dangling_memory_entry(schema_plexus.project_memory_dir)
        _code, out, _err = _run(capsys, "validate", str(schema_plexus.root))
        for finding in run_validate(schema_plexus.root).findings:
            assert finding.message in out

    def test_a_detail_is_rendered_when_there_is_one(self, capsys, schema_plexus):
        _break_manifest(schema_plexus)
        _code, out, _err = _run(capsys, "validate", str(schema_plexus.root))
        assert "detail:" in out

    def test_a_multi_line_message_stays_inside_its_entry(self, capsys, schema_plexus):
        # A YAML parser's complaint about the manifest spans several lines; an
        # unindented continuation would run flush left and read as the next
        # item of the list.
        _break_manifest(schema_plexus)
        _code, out, _err = _run(capsys, "validate", str(schema_plexus.root))
        block = out.split("fix list", 1)[1].split("\n\n", 1)[0].splitlines()[1:]
        continuations = [line for line in block if line.strip() and not ENTRY_RE.match(line)]
        assert continuations
        assert all(line.startswith("     ") for line in continuations)

    def test_a_clean_tree_says_conformant(self, capsys, schema_plexus):
        # "Clean" is about severity, not silence: the fixture's host bucket is
        # listed (informational) and the tree is conformant all the same.
        _code, out, _err = _run(capsys, "validate", str(schema_plexus.root))
        assert [(severity, code) for _n, severity, code, _p in _entries(out)
                if severity != "INFO"] == [("WARN", "manifest.schema-older")]
        assert "conformant" in out.lower()
        assert "not conformant" not in out.lower()

    def test_a_report_with_nothing_in_it_says_there_is_nothing_to_fix(self):
        # The empty fix-list is a real rendering, not an unreachable branch:
        # a tree with no findings at all must not print a headless list.
        rendered = "\n".join(cli.render_text(Report(Form.SCHEMA), "/somewhere"))
        assert "no findings" in rendered.lower()
        assert "fix list" not in rendered.lower()

    def test_an_error_report_never_claims_conformance(self, capsys, schema_plexus):
        _dangling_memory_entry(schema_plexus.project_memory_dir)
        _code, out, _err = _run(capsys, "validate", str(schema_plexus.root))
        assert "not conformant" in out.lower()
        assert "1 error(s)" in out

    def test_a_legacy_tree_reads_as_a_migration_list_not_a_gate(
        self, capsys, legacy_plexus
    ):
        # D14/D16: in a legacy tree the output is the list of things to fix
        # before adopting the schema. The verdict must say that, because the
        # exit code alone (0) says the opposite of "there is work here".
        _dangling_memory_entry(legacy_plexus.project_memory_dir)
        code, out, _err = _run(capsys, "validate", str(legacy_plexus.root))
        assert code == EXIT_CLEAN
        assert _entries(out)
        assert "legacy" in out.lower()
        assert "migration" in out.lower()

    def test_unusable_input_explains_itself(self, capsys, schema_plexus):
        _break_manifest(schema_plexus)
        code, out, _err = _run(capsys, "validate", str(schema_plexus.root))
        assert code == EXIT_UNUSABLE
        assert "unusable" in out.lower()
        # The skipped checks are announced, so a short list is never mistaken
        # for a clean tree (the runner's own INFO finding).
        assert "runner.checks-skipped" in out

    def test_the_text_report_goes_to_stdout(self, capsys, schema_plexus):
        _code, out, err = _run(capsys, "validate", str(schema_plexus.root))
        assert out
        assert err == ""


# ---------------------------------------------------------------------------
# --list-checks: the inventory docs/SCHEMA.md is reconciled against
# ---------------------------------------------------------------------------


class TestListChecks:
    def test_one_row_per_registered_code_in_code_order(self, capsys):
        code, out, err = _run(capsys, "--list-checks")
        assert code == EXIT_CLEAN
        assert err == ""
        rows = [CHECK_ROW_RE.match(line) for line in out.splitlines() if line.strip()]
        assert all(rows), "every printed line is one check row"
        codes = [row.group(1) for row in rows]
        assert codes == sorted(entry.code for entry in CHECKS)

    def test_the_second_field_is_the_severity(self, capsys):
        # The row shape the task's verify command greps for; also what makes
        # `--list-checks | awk '{print $1, $2}'` the D14 table.
        _code, out, _err = _run(capsys, "--list-checks")
        for line in out.splitlines():
            row = CHECK_ROW_RE.match(line)
            assert row.group(2) == CHECKS[row.group(1)].severity.value

    def test_every_check_modules_namespace_is_present(self, capsys):
        # The registry fills at import time, so a check module the CLI never
        # imports would drop its codes from the inventory silently.
        _code, out, _err = _run(capsys, "--list-checks")
        namespaces = {line.split(".", 1)[0] for line in out.splitlines() if line.strip()}
        assert namespaces == {entry.namespace for entry in CHECKS}
        expected = {"manifest", "registry", "tree", "agents", "record", "index", "runner"}
        assert expected <= namespaces

    def test_the_row_carries_the_tier_and_the_summary(self, capsys):
        _code, out, _err = _run(capsys, "--list-checks")
        rows = {r.group(1): r for r in map(CHECK_ROW_RE.match, out.splitlines()) if r}
        entry = CHECKS["record.state-unparseable"]
        assert rows[entry.code].group(3) == entry.tier.value
        assert entry.summary.split()[0] in rows[entry.code].group(4)

    def test_a_tier_sensitive_code_shows_its_legacy_severity(self, capsys):
        # D14's softenings are the whole point of the table: the row has to
        # say "ERROR here, WARN in a legacy tree".
        _code, out, _err = _run(capsys, "--list-checks")
        row = next(line for line in out.splitlines() if line.startswith("index.dangling-link"))
        assert "ERROR" in row
        assert "WARN" in row

    def test_older_schema_warning_is_reachable(self, capsys):
        assert any(value < CURRENT_SCHEMA for value in SUPPORTED_SCHEMAS)
        _code, out, _err = _run(capsys, "--list-checks")
        row = next(line for line in out.splitlines() if line.startswith("manifest.schema-older"))
        assert "unreachable" not in row

    def test_reachable_codes_are_not_flagged(self, capsys):
        _code, out, _err = _run(capsys, "--list-checks")
        flagged = [line for line in out.splitlines() if "unreachable" in line]
        assert flagged == []

    def test_the_json_inventory_is_the_registry_document(self, capsys):
        code, doc, _err = _json(capsys, "--list-checks", "--json")
        assert code == EXIT_CLEAN
        assert set(doc) == {"schema", "tool", "checks"}
        assert doc["schema"] == cli.OUTPUT_SCHEMA
        assert doc["tool"] == "lmer-schema"
        assert [entry["code"] for entry in doc["checks"]] == sorted(e.code for e in CHECKS)
        for entry in doc["checks"]:
            assert set(entry) == {"code", "severity", "legacy", "tier", "summary", "unreachable"}
            registered = CHECKS[entry["code"]]
            assert entry["severity"] == registered.severity.value
            assert entry["legacy"] == registered.legacy.value
            assert entry["tier"] == registered.tier.value

    def test_the_json_inventory_has_no_unreachable_codes(self, capsys):
        _code, doc, _err = _json(capsys, "--list-checks", "--json")
        unreachable = {e["code"]: e["unreachable"] for e in doc["checks"] if e["unreachable"]}
        assert unreachable == {}


# ---------------------------------------------------------------------------
# What the CLI passes through to the runner
# ---------------------------------------------------------------------------


class TestPassThrough:
    def test_the_plexus_repo_url_wakes_the_sources_guard(self, capsys, schema_plexus):
        # T8's same-host guard is DORMANT unless the caller supplies the URL —
        # the CLI is that caller, so this is the test that keeps it awake.
        code, doc, _err = _json(
            capsys,
            "validate",
            str(schema_plexus.root),
            "--json",
            "--plexus-repo-url",
            FOREIGN_REPO_URL,
        )
        assert "manifest.sources-cross-host" in [f["code"] for f in doc["findings"]]
        assert code == EXIT_ERRORS

    def test_without_a_url_the_guard_stays_quiet(self, capsys, schema_plexus):
        _code, doc, _err = _json(capsys, "validate", str(schema_plexus.root), "--json")
        assert "manifest.sources-cross-host" not in [f["code"] for f in doc["findings"]]

    @pytest.mark.parametrize("variable", ["LMER_WORK_REPO", "LMER_REPO_URL"])
    def test_the_work_repo_url_is_the_default_for_the_work_repo_itself(
        self, capsys, monkeypatch, schema_plexus, variable
    ):
        # When PATH really IS $LMER_WORK_REPO_PATH, the tree on the command
        # line and the variables describe ONE repo, so doctor's precedence is
        # evidence about it and the CLI reuses those documented variables
        # rather than inventing an LMER_* var of its own.
        monkeypatch.setenv(variable, FOREIGN_REPO_URL)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(schema_plexus.root))
        code, doc, _err = _json(capsys, "validate", str(schema_plexus.root), "--json")
        assert "manifest.sources-cross-host" in [f["code"] for f in doc["findings"]]
        assert code == EXIT_ERRORS

    @pytest.mark.parametrize("variable", ["LMER_WORK_REPO", "LMER_REPO_URL"])
    def test_an_ambient_url_cannot_change_another_trees_verdict(
        self, capsys, monkeypatch, schema_plexus, variable
    ):
        # A conformance verdict must never depend on an ambient variable.
        # `validate PATH` takes an arbitrary root, so a work-repo URL that
        # merely happens to be exported says nothing about THIS tree: same
        # bytes, two environments, ONE document (findings, counts and exit
        # code alike).
        code, env_free, _err = _json(capsys, "validate", str(schema_plexus.root), "--json")
        monkeypatch.setenv(variable, FOREIGN_REPO_URL)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(schema_plexus.root.parent))
        ambient_code, ambient, _err = _json(capsys, "validate", str(schema_plexus.root), "--json")
        assert (ambient_code, ambient) == (code, env_free)
        assert "manifest.sources-cross-host" not in [f["code"] for f in ambient["findings"]]

    @pytest.mark.parametrize("work_repo_path", [None, "", "/no/such/work/repo"])
    def test_an_unset_or_unusable_work_repo_path_just_means_no_fallback(
        self, capsys, monkeypatch, schema_plexus, work_repo_path
    ):
        # Not an error and not a guess: with nothing to prove the tree is the
        # work repo, the same-host guard stays dormant.
        monkeypatch.setenv("LMER_WORK_REPO", FOREIGN_REPO_URL)
        if work_repo_path is not None:
            monkeypatch.setenv("LMER_WORK_REPO_PATH", work_repo_path)
        code, doc, _err = _json(capsys, "validate", str(schema_plexus.root), "--json")
        assert "manifest.sources-cross-host" not in [f["code"] for f in doc["findings"]]
        assert code == EXIT_CLEAN

    def test_the_work_repo_is_recognised_through_a_symlink_or_a_trailing_slash(
        self, capsys, monkeypatch, tmp_path, schema_plexus
    ):
        # The comparison is between RESOLVED directories, so the many
        # spellings of one path (symlink, trailing slash, `..`) are one repo.
        link = tmp_path / "work-link"
        link.symlink_to(schema_plexus.root, target_is_directory=True)
        monkeypatch.setenv("LMER_WORK_REPO", FOREIGN_REPO_URL)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(schema_plexus.root))
        _code, doc, _err = _json(capsys, "validate", f"{link}/", "--json")
        assert "manifest.sources-cross-host" in [f["code"] for f in doc["findings"]]

    def test_the_flag_outranks_the_environment(self, capsys, monkeypatch, schema_plexus):
        # The flag wins even where the fallback WOULD apply.
        monkeypatch.setenv("LMER_WORK_REPO", FOREIGN_REPO_URL)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(schema_plexus.root))
        _code, doc, _err = _json(
            capsys,
            "validate",
            str(schema_plexus.root),
            "--json",
            "--plexus-repo-url",
            f"https://{pf.PRIMARY_REPO}",
        )
        assert "manifest.sources-cross-host" not in [f["code"] for f in doc["findings"]]

    def test_the_lmer_work_repo_variable_wins_over_the_legacy_one(
        self, capsys, monkeypatch, schema_plexus
    ):
        monkeypatch.setenv("LMER_WORK_REPO", f"https://{pf.PRIMARY_REPO}")
        monkeypatch.setenv("LMER_REPO_URL", FOREIGN_REPO_URL)
        monkeypatch.setenv("LMER_WORK_REPO_PATH", str(schema_plexus.root))
        _code, doc, _err = _json(capsys, "validate", str(schema_plexus.root), "--json")
        assert "manifest.sources-cross-host" not in [f["code"] for f in doc["findings"]]

    def test_the_staleness_window_is_passed_in_days(self, capsys, monkeypatch, schema_plexus):
        # A seam test on purpose: the alternative pins a verdict to the wall
        # clock, and the record check's window is measured in seconds.
        seen = {}

        def recorder(target, **kwargs):
            seen.update(kwargs, target=target)
            return Report(Form.SCHEMA)

        monkeypatch.setattr(cli, "validate", recorder)
        assert cli.main(["validate", str(schema_plexus.root), "--stale-after", "30"]) == EXIT_CLEAN
        capsys.readouterr()
        assert seen["stale_after"] == 30 * 86400
        assert str(seen["target"]) == str(schema_plexus.root)
        assert seen["plexus_repo_url"] is None

    def test_the_default_window_is_the_record_checks_own(self, capsys, monkeypatch, schema_plexus):
        seen = {}

        def recorder(target, **kwargs):
            seen.update(kwargs)
            return Report(Form.SCHEMA)

        monkeypatch.setattr(cli, "validate", recorder)
        cli.main(["validate", str(schema_plexus.root)])
        capsys.readouterr()
        assert seen["stale_after"] == STALE_AFTER_SECONDS


# ---------------------------------------------------------------------------
# The two-directional invariant between --list-checks and docs/SCHEMA.md
# ---------------------------------------------------------------------------


class TestTheCodeTableMatchesTheDocument:
    """`docs/SCHEMA.md`'s "Check codes" table IS the registry: it names every
    registered code, registers every code it names, and states each one's
    severities and tier — checked, not reconciled by hand.

    T44 reconciled the two by diffing them manually. A table agreed once drifts
    the next time a code is added, and this build has repeatedly found
    documents crediting the validator with rules it does not implement (the
    session-dir symlink row is the sharpest case: a writer, a documented
    ERROR, and no check). The document is normative, so the drift is worth a
    test rather than a review pass.

    The COLUMNS are pinned, not just the names, because the document's own
    claim is stronger than a name list: "`lmer-schema --list-checks` prints
    exactly these codes with exactly these severities and tiers". A code
    flipped from WARN to ERROR in its check module with the table left alone
    is the drift this class exists to catch, and a name-only comparison stays
    green through it.

    The reading is scoped to the table ROWS rather than to the file, which is
    what makes both directions mean what they say: a code deleted from its row
    but still mentioned in prose is NOT documented (the document's promise is
    the table), and a backticked `record.py` or `manifest.yaml` written in
    prose — the change-process section already spells `src/lmer_schema/checks/`
    — is not a code the validator fails to register.

    NOTE: this test reads a repo doc, so `tests/test_lmer_schema_cli.py` must
    stay declared in `.lmer/gate-check.yaml`'s `tests.text_diff_subset` — a
    text-only edit to SCHEMA.md has to run it.
    """

    SCHEMA_MD = Path(__file__).resolve().parents[1] / "docs" / "SCHEMA.md"

    #: The heading the table lives under; the next level-2 heading ends it.
    SECTION_HEADING = "### Check codes"

    #: One table row: the backticked code, then the `severity`, `legacy` and
    #: `tier` cells. The `legacy` cell is empty for every code the legacy tier
    #: leaves alone, and the `reports` prose after it is not read.
    ROW_RE = re.compile(
        r"^\|\s*`([a-z][a-z-]*\.[a-z][a-z-]*)`\s*\|([^|]*)\|([^|]*)\|([^|]*)\|"
    )

    def _rows(self):
        """Every row of the "Check codes" table as raw
        `(code, severity, legacy, tier)` cells, in document order.

        Scoped to that ONE section: the reading has to be the table's, not the
        file's, or a code mentioned anywhere in prose counts as documented.
        """
        text = self.SCHEMA_MD.read_text(encoding="utf-8")
        start = text.find(self.SECTION_HEADING)
        assert start >= 0, (
            f"docs/SCHEMA.md has no {self.SECTION_HEADING!r} section — the "
            "table this class reads is the document's registry of codes"
        )
        rest = text[start + len(self.SECTION_HEADING):]
        end = rest.find("\n## ")
        section = rest if end < 0 else rest[:end]
        for line in section.splitlines():
            row = self.ROW_RE.match(line)
            if row is not None:
                yield tuple(group.strip() for group in row.groups())

    def _table(self):
        """The "Check codes" table as `{code: (severity, legacy, tier)}`,
        spelled in the registry's own types.

        An empty `legacy` cell means "the legacy tier does not soften this
        one", which is `CheckCode.legacy is severity` — the same default
        `register()` applies.
        """
        table = {}
        for code, severity, legacy, tier in self._rows():
            try:
                severity = Severity.coerce(severity)
                legacy = Severity.coerce(legacy) if legacy else severity
                tier = Tier.coerce(tier)
            except ValueError as exc:
                raise AssertionError(
                    f"docs/SCHEMA.md's row for `{code}` is unreadable: {exc}"
                ) from exc
            assert code not in table, (
                f"docs/SCHEMA.md lists `{code}` twice; a code is one rule"
            )
            table[code] = (severity, legacy, tier)
        assert table, (
            f"no rows parsed under {self.SECTION_HEADING!r} in docs/SCHEMA.md "
            "— the table moved or changed shape, and every assertion in this "
            "class would be vacuous"
        )
        return table

    def test_every_registered_code_is_named_in_the_document(self):
        registered = {entry.code for entry in CHECKS}
        missing = sorted(registered - set(self._table()))
        assert not missing, (
            f"{len(missing)} registered code(s) missing from docs/SCHEMA.md's "
            f"check-code table: {missing} — a code nobody documented is a rule "
            "nobody agreed to"
        )

    def test_every_documented_code_is_registered(self):
        registered = {entry.code for entry in CHECKS}
        extra = sorted(set(self._table()) - registered)
        assert not extra, (
            f"docs/SCHEMA.md names {len(extra)} code(s) the validator does not "
            f"register: {extra} — the document would be promising a check that "
            "cannot fire"
        )

    def test_the_table_states_each_codes_severities_and_tier(self):
        # The columns, not just the names: SCHEMA.md says --list-checks prints
        # exactly these codes with exactly these severities and tiers, so a
        # severity changed in a check module and not in the table is a
        # normative document stating a rule the validator does not enforce.
        table = self._table()
        mismatched = {
            entry.code: {
                "registered": (entry.severity, entry.legacy, entry.tier),
                "documented": table[entry.code],
            }
            for entry in CHECKS
            if entry.code in table
            and table[entry.code] != (entry.severity, entry.legacy, entry.tier)
        }
        assert not mismatched, (
            f"{len(mismatched)} code(s) whose (severity, legacy, tier) in "
            f"docs/SCHEMA.md is not the one the validator registers: "
            f"{mismatched}"
        )

    def test_only_the_tier_sensitive_codes_fill_the_legacy_column(self):
        # The document says the legacy column "is given only for the five
        # codes where the two differ". A code that softens under the legacy
        # tier with a blank cell reads as "never softens"; a redundant cell on
        # a code that does not soften reads as a D14 downgrade that is not one.
        filled = {code for code, _sev, legacy, _tier in self._rows() if legacy}
        sensitive = {entry.code for entry in CHECKS if entry.tier_sensitive}
        assert filled == sensitive, (
            "docs/SCHEMA.md fills the `legacy` column for "
            f"{sorted(filled)}, but the tier-sensitive codes (D14) are "
            f"{sorted(sensitive)}"
        )


# ---------------------------------------------------------------------------
# `migrate`: the verb the cutover docs prescribe running before `--apply`
# ---------------------------------------------------------------------------


def _schema_one_tree(tmp_path, *, in_progress):
    """A schema-1 tree with one record; ``in_progress`` makes it a blocker
    (no recorded sign-out, so the cutover must refuse)."""
    root = tmp_path / "plexus"
    record = root / "widgets" / "_runs" / "chat" / "20260917-120000-1-7"
    record.mkdir(parents=True)
    (root / "_plexus.yaml").write_text(
        "schema: 1\nname: example\nprojects:\n  widgets:\n"
        "    repo: git.example.test/acme/widgets\n",
        encoding="utf-8",
    )
    status = "in-progress" if in_progress else "complete"
    (record / "state.yaml").write_text(f"schema: 1\nstatus: {status}\n", encoding="utf-8")
    return root


def _tree_bytes(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class TestMigrate:
    """The CLI wrapper around ``lmer_schema.migration.migrate``: the library
    is covered by ``tests/test_schema_v2_migration.py``; what these pin is the
    verb's exit code and output on each outcome — a blocked preview crashing
    with a ``NameError`` shipped green because nothing drove the verb here."""

    @pytest.mark.parametrize("extra", [(), ("--json",), ("--apply",)], ids=["text", "json", "apply"])
    def test_a_blocked_preview_reports_the_blocker_and_exits_errors(self, capsys, tmp_path, extra):
        root = _schema_one_tree(tmp_path, in_progress=True)
        before = _tree_bytes(root)
        code, out, err = _run(capsys, "migrate", str(root), *extra)
        assert code == EXIT_ERRORS
        assert "Traceback" not in err and "NameError" not in err
        if "--json" in extra:
            doc = json.loads(out)
            assert doc["applied"] is False
            assert any("sign-out" in blocker for blocker in doc["blockers"])
        else:
            assert "Blocked:" in out and "sign-out" in out
            assert "Schema 2 applied" not in out
        assert _tree_bytes(root) == before, "a blocked preview must write nothing"

    def test_an_unblocked_preview_is_clean_and_writes_nothing(self, capsys, tmp_path):
        root = _schema_one_tree(tmp_path, in_progress=False)
        before = _tree_bytes(root)
        code, out, _err = _run(capsys, "migrate", str(root))
        assert code == EXIT_CLEAN
        assert "+schema: 2" in out and "Preview only" in out
        assert _tree_bytes(root) == before

    def test_apply_changes_only_the_version_and_reports_clean(self, capsys, tmp_path):
        root = _schema_one_tree(tmp_path, in_progress=False)
        before = _tree_bytes(root)
        code, out, _err = _run(capsys, "migrate", str(root), "--apply")
        assert code == EXIT_CLEAN
        assert "Schema 2 applied locally." in out
        after = _tree_bytes(root)
        manifest = Path("_plexus.yaml")
        assert after[manifest] == before[manifest].replace(b"schema: 1", b"schema: 2")
        assert {k: v for k, v in after.items() if k != manifest} == {
            k: v for k, v in before.items() if k != manifest
        }

    def test_a_tree_without_a_manifest_is_unusable_input(self, capsys, tmp_path):
        code, _out, err = _run(capsys, "migrate", str(tmp_path))
        assert code == EXIT_UNUSABLE
        assert "Migration refused" in err
