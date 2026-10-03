"""``lmer_schema.checks.manifest`` — the manifest-envelope checks (issue #353).

The validator's first check module: it looks at the ``_plexus.yaml``
ENVELOPE only — presence, parseability, the ``schema:`` int in BOTH
directions, ``name:``, unknown keys, and the ``sources:``/``housekeeping:``
sections. What is pinned here:

- absent manifest = recognized-legacy INFO (§4 finding 9), never an error,
  and the only outcome that returns ``None`` without marking the report
  unusable;
- a present-but-broken manifest is an ERROR finding AND an unusable report
  (exit 2): D20 forbids a broken manifest demoting a schema-form plexus, so
  the check never swallows a ``ManifestError`` and never crashes on one;
- ``schema:`` both ways — a bool is the ``read_taskdef_schema`` trap and is
  ERROR, a NEWER int is ERROR (tools refuse to write), an OLDER supported
  int is WARN "schema N available" (driven end to end with a future
  ``SUPPORTED_SCHEMAS``, because v1 supports exactly one int);
- unknown top-level and per-entry keys are WARN (schema plasticity), never
  errors, and the manifest still loads;
- the ``sources:`` same-host guard (a declared source on another host would
  route the work-repo credential elsewhere — ERROR, with the env override
  named as the sanctioned escape hatch) and the precedence nudge (a
  standalone ``sources.yaml`` beside a manifest = WARN, INFO in a legacy
  tree);
- the LAYERING: registry-entry semantics (short names, primary vs additional
  remotes, duplicates) belong to ``lmer_schema.registry`` and its sibling
  check module — this module emits no ``registry.*`` finding and carries no
  second copy of the charset or URL rules.

Every negative test mutates ITS OWN fresh fixture tree (the shared builder is
consumed read-only).
"""

import inspect

import pytest
import yaml

from lmer_schema import manifest as core_manifest
from lmer_schema.checks import CHECKS, EXIT_CLEAN, EXIT_UNUSABLE, Form, Report, Severity
from lmer_schema.checks import manifest as checks_manifest
from lmer_schema.checks.manifest import check_manifest, schema_finding
from tests import plexus_fixtures as pf

#: Every code this module owns, with the severity it carries in a schema-form
#: tree. The registration test reconciles this list against ``CHECKS``.
EXPECTED_CODES = {
    "manifest.absent": Severity.INFO,
    "manifest.unparseable": Severity.ERROR,
    "manifest.schema-not-int": Severity.ERROR,
    "manifest.schema-newer": Severity.ERROR,
    "manifest.schema-older": Severity.WARN,
    "manifest.schema-unsupported": Severity.ERROR,
    "manifest.name-invalid": Severity.ERROR,
    "manifest.unknown-key": Severity.WARN,
    "manifest.sources-reserved": Severity.WARN,
    "manifest.sources-repo-invalid": Severity.ERROR,
    "manifest.sources-credential": Severity.ERROR,
    "manifest.sources-cross-host": Severity.ERROR,
    "manifest.sources-legacy-file": Severity.WARN,
    "manifest.housekeeping-policy-invalid": Severity.ERROR,
}


def _report(form=Form.SCHEMA):
    return Report(form)


def _codes(report):
    return [finding.code for finding in report.findings]


def _find(report, code):
    """The one finding carrying *code* (fails the test when it is not there
    exactly once — a check that fires twice is a bug, not a detail)."""
    matches = [finding for finding in report.findings if finding.code == code]
    assert len(matches) == 1, f"expected exactly one {code}, got {_codes(report)}"
    return matches[0]


def _doc():
    """The §4 fixture manifest as a plain dict, for targeted mutation."""
    return yaml.safe_load(pf.SCHEMA_MANIFEST)


def _rewrite(tree, doc):
    """Overwrite THIS test's own manifest with *doc*."""
    tree.manifest.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _sources_host(tree):
    return tree.primary_repo.split("/", 1)[0]


# ---------------------------------------------------------------------------
# The codes this module registers
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_every_code_is_registered_with_its_documented_severity(self):
        for code, severity in EXPECTED_CODES.items():
            assert code in CHECKS, f"{code} is not registered"
            assert CHECKS[code].severity is severity

    def test_the_module_owns_exactly_these_codes(self):
        registered = {
            entry.code for entry in CHECKS if entry.namespace == "manifest"
        }
        assert registered == set(EXPECTED_CODES)

    def test_only_the_legacy_recognition_codes_are_tier_sensitive(self):
        # D14 softens what a legacy tree cannot help; a manifest SHAPE
        # violation is a shape violation in any form.
        softened = {
            entry.code
            for entry in CHECKS.tier_sensitive()
            if entry.namespace == "manifest"
        }
        assert softened == {"manifest.sources-legacy-file"}

    def test_every_code_carries_a_summary_for_list_checks(self):
        for code in EXPECTED_CODES:
            assert CHECKS[code].summary


# ---------------------------------------------------------------------------
# Presence: absent manifest, clean manifest
# ---------------------------------------------------------------------------


class TestPresence:
    def test_the_fixture_manifest_is_clean(self, schema_plexus):
        report = _report()
        manifest = check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        assert _codes(report) == []
        assert report.exit_code == EXIT_CLEAN
        assert manifest is not None
        assert manifest.name == pf.INSTANCE_NAME
        assert set(manifest.projects) == {pf.PROJECT, pf.REPOLESS_PROJECT}

    def test_absent_manifest_is_recognized_legacy_info(self, legacy_plexus):
        report = _report(Form.LEGACY)
        manifest = check_manifest(legacy_plexus.root, report)
        assert manifest is None
        finding = _find(report, "manifest.absent")
        assert finding.severity is Severity.INFO
        assert finding.path == "."
        assert report.unusable is None
        assert report.exit_code == EXIT_CLEAN

    def test_absent_manifest_never_blocks_and_names_the_file(self, legacy_plexus):
        report = _report(Form.LEGACY)
        check_manifest(legacy_plexus.root, report)
        finding = _find(report, "manifest.absent")
        assert core_manifest.MANIFEST_NAME in finding.message


# ---------------------------------------------------------------------------
# Present but broken: an ERROR finding and an unusable report (exit 2)
# ---------------------------------------------------------------------------


class TestUnusable:
    def test_unparseable_yaml_is_an_error_not_a_crash(self, schema_plexus):
        schema_plexus.manifest.write_text("schema: 1\nname: [oops\n", encoding="utf-8")
        report = _report()
        assert check_manifest(schema_plexus.root, report) is None
        assert _find(report, "manifest.unparseable").severity is Severity.ERROR
        assert report.unusable is not None
        assert report.exit_code == EXIT_UNUSABLE

    def test_a_non_mapping_document_is_unparseable(self, schema_plexus):
        schema_plexus.manifest.write_text("just a string\n", encoding="utf-8")
        report = _report()
        check_manifest(schema_plexus.root, report)
        assert "manifest.unparseable" in _codes(report)
        assert report.exit_code == EXIT_UNUSABLE

    def test_an_unreadable_manifest_is_unusable(self, schema_plexus):
        # A directory where the manifest belongs: present, unreadable.
        schema_plexus.manifest.unlink()
        schema_plexus.manifest.mkdir()
        report = _report()
        check_manifest(schema_plexus.root, report)
        assert "manifest.unparseable" in _codes(report)
        assert "manifest.absent" not in _codes(report)
        assert report.exit_code == EXIT_UNUSABLE

    def test_the_finding_path_is_the_manifest_not_an_absolute_path(self, schema_plexus):
        schema_plexus.manifest.write_text("{[\n", encoding="utf-8")
        report = _report()
        check_manifest(schema_plexus.root, report)
        assert _find(report, "manifest.unparseable").path == core_manifest.MANIFEST_NAME

    def test_a_missing_name_is_reported_precisely(self, schema_plexus):
        doc = _doc()
        del doc["name"]
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(schema_plexus.root, report)
        assert _find(report, "manifest.name-invalid").severity is Severity.ERROR
        assert report.exit_code == EXIT_UNUSABLE

    @pytest.mark.parametrize("name", [42, "", None])
    def test_a_non_string_name_is_name_invalid(self, schema_plexus, name):
        doc = _doc()
        doc["name"] = name
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(schema_plexus.root, report)
        assert "manifest.name-invalid" in _codes(report)


# ---------------------------------------------------------------------------
# `schema:` — both directions (§4 finding 9)
# ---------------------------------------------------------------------------


class TestSchemaInt:
    def test_a_bool_schema_is_the_read_taskdef_schema_trap(self, schema_plexus):
        # `isinstance(True, int)` is true, so a bool must be caught by name.
        schema_plexus.manifest.write_text(
            pf.SCHEMA_MANIFEST.replace("schema: 1", "schema: true", 1), encoding="utf-8"
        )
        report = _report()
        check_manifest(schema_plexus.root, report)
        finding = _find(report, "manifest.schema-not-int")
        assert finding.severity is Severity.ERROR
        assert report.exit_code == EXIT_UNUSABLE

    def test_a_string_schema_is_not_an_int(self, schema_plexus):
        doc = _doc()
        doc["schema"] = "1"
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(schema_plexus.root, report)
        assert "manifest.schema-not-int" in _codes(report)

    def test_a_newer_schema_int_is_an_error(self, schema_plexus):
        doc = _doc()
        doc["schema"] = core_manifest.CURRENT_SCHEMA + 1
        _rewrite(schema_plexus, doc)
        report = _report()
        assert check_manifest(schema_plexus.root, report) is None
        finding = _find(report, "manifest.schema-newer")
        assert finding.severity is Severity.ERROR
        assert str(core_manifest.CURRENT_SCHEMA + 1) in (finding.detail or finding.message)
        # A shape this lmer cannot interpret is unusable input, not a list of
        # findings about a tree it understood.
        assert report.exit_code == EXIT_UNUSABLE

    def test_an_older_unsupported_schema_int_is_an_error(self, schema_plexus):
        doc = _doc()
        doc["schema"] = 0
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(schema_plexus.root, report)
        assert "manifest.schema-unsupported" in _codes(report)
        assert report.exit_code == EXIT_UNUSABLE

    def test_an_older_supported_int_warns_and_still_loads(self, schema_plexus, monkeypatch):
        # v1 supports exactly one int, so the "older" direction is driven
        # with a future support tuple — the loader and this module both read
        # it at call time.
        monkeypatch.setattr(core_manifest, "SUPPORTED_SCHEMAS", (1, 2))
        monkeypatch.setattr(core_manifest, "CURRENT_SCHEMA", 2)
        report = _report()
        manifest = check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        assert manifest is not None and manifest.schema == 1
        finding = _find(report, "manifest.schema-older")
        assert finding.severity is Severity.WARN
        assert "2" in finding.message
        # A warning never blocks (D14) and the tree stays usable.
        assert report.unusable is None
        assert report.exit_code == EXIT_CLEAN

    def test_schema_finding_is_a_pure_function_of_the_value(self):
        assert schema_finding(2, supported=(1, 2), current=2) is None
        code, message = schema_finding(1, supported=(1, 2), current=2)
        assert code == "manifest.schema-older"
        assert "2" in message
        code, _ = schema_finding(3, supported=(1, 2), current=2)
        assert code == "manifest.schema-newer"
        code, _ = schema_finding(0, supported=(1, 2), current=2)
        assert code == "manifest.schema-unsupported"

    @pytest.mark.parametrize("value", [True, False, "1", 1.0, None, [1]])
    def test_schema_finding_rejects_every_non_int(self, value):
        code, _ = schema_finding(value, supported=(1,), current=1)
        assert code == "manifest.schema-not-int"

    def test_schema_finding_defaults_to_the_loaders_support_tuple(self):
        assert schema_finding(core_manifest.CURRENT_SCHEMA) is None
        code, _ = schema_finding(core_manifest.CURRENT_SCHEMA + 1)
        assert code == "manifest.schema-newer"


# ---------------------------------------------------------------------------
# Unknown keys — schema plasticity (WARN, never an error)
# ---------------------------------------------------------------------------


class TestUnknownKeys:
    def test_an_unknown_top_level_key_warns_and_the_manifest_loads(self, schema_plexus):
        doc = _doc()
        doc["plugins"] = {"widget": True}
        _rewrite(schema_plexus, doc)
        report = _report()
        manifest = check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        assert manifest is not None
        finding = _find(report, "manifest.unknown-key")
        assert finding.severity is Severity.WARN
        assert "plugins" in (finding.detail or finding.message)
        assert report.exit_code == EXIT_CLEAN

    def test_an_unknown_registry_entry_key_warns(self, schema_plexus):
        doc = _doc()
        doc["projects"][pf.PROJECT]["owner"] = "someone"
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(schema_plexus.root, report)
        finding = _find(report, "manifest.unknown-key")
        assert f"projects.{pf.PROJECT}.owner" in (finding.detail or finding.message)

    def test_an_unknown_source_entry_key_warns(self, schema_plexus):
        doc = _doc()
        doc["sources"]["taskdef"]["branch"] = "main"
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        assert "sources.taskdef.branch" in (
            _find(report, "manifest.unknown-key").detail or ""
        )

    def test_a_source_ref_under_a_name_that_does_not_take_one_warns(self, schema_plexus):
        """The `sources.<name>` vocabulary is per NAME: `ref:` belongs to
        `taskdef` alone (SCHEMA.md sources table), and `load_sources` refuses
        it anywhere else. The manifest tier is declared-only until its loader
        lands (issue #370), so an unwarned `sources.napkin.ref` is a ref that
        silently does nothing now and refuses to start later."""
        doc = _doc()
        doc["sources"]["napkin"]["ref"] = "main"
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        finding = _find(report, "manifest.unknown-key")
        assert finding.severity is Severity.WARN
        assert "sources.napkin.ref" in (finding.detail or finding.message)
        assert report.exit_code == EXIT_CLEAN

    def test_the_documented_taskdef_ref_is_never_an_unknown_key(self, schema_plexus):
        doc = _doc()
        doc["sources"]["taskdef"]["ref"] = "release-1.2"
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        assert _codes(report) == []

    def test_an_unknown_housekeeping_policy_key_warns(self, schema_plexus):
        doc = _doc()
        doc["housekeeping"]["napkin"]["delete_after"] = "30d"
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        assert "housekeeping.napkin.delete_after" in (
            _find(report, "manifest.unknown-key").detail or ""
        )

    def test_an_unknown_source_name_warns_as_an_unknown_key(self, schema_plexus):
        doc = _doc()
        doc["sources"]["widgets"] = {"repo": f"https://{_sources_host(schema_plexus)}/x/y"}
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        assert "sources.widgets" in (_find(report, "manifest.unknown-key").detail or "")
        assert report.exit_code == EXIT_CLEAN

    def test_a_reserved_source_name_warns_by_name(self, schema_plexus):
        doc = _doc()
        doc["sources"]["knowledge"] = {"repo": f"https://{_sources_host(schema_plexus)}/x/y"}
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        finding = _find(report, "manifest.sources-reserved")
        assert finding.severity is Severity.WARN
        assert "knowledge" in (finding.detail or finding.message)
        assert "manifest.unknown-key" not in _codes(report)


# ---------------------------------------------------------------------------
# `sources:` — the same-host guard and the shape of a declaration
# ---------------------------------------------------------------------------


class TestSourcesSameHost:
    def test_a_cross_host_source_is_an_error(self, schema_plexus):
        doc = _doc()
        doc["sources"]["napkin"] = {"repo": "https://elsewhere.example.net/a/napkin.git"}
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        finding = _find(report, "manifest.sources-cross-host")
        assert finding.severity is Severity.ERROR
        assert "elsewhere.example.net" in (finding.detail or "")

    def test_the_error_names_the_env_override_escape_hatch(self, schema_plexus):
        doc = _doc()
        doc["sources"]["napkin"] = {"repo": "https://elsewhere.example.net/a/napkin.git"}
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        finding = _find(report, "manifest.sources-cross-host")
        assert "LMER_NAPKIN_REPO" in f"{finding.message} {finding.detail}"

    def test_a_different_port_on_the_same_host_is_not_cross_host(self, schema_plexus):
        # The trust rule compares hostnames only: a second port on the host
        # that already holds the credential is not another host.
        host = _sources_host(schema_plexus)
        doc = _doc()
        doc["sources"]["napkin"] = {"repo": f"ssh://git@{host}:2222/agents/napkin.git"}
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        assert "manifest.sources-cross-host" not in _codes(report)

    def test_the_canonical_host_path_spelling_is_understood_on_both_sides(
        self, schema_plexus
    ):
        # The registry stores remotes in the canonical `host/path` spelling
        # (no scheme), and a source may be written the same way; the guard
        # must read a host from it rather than fall over into "no host".
        host = _sources_host(schema_plexus)
        doc = _doc()
        doc["sources"]["napkin"] = {"repo": f"{host}/agents/napkin"}
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        assert _codes(report) == []

        doc["sources"]["napkin"] = {"repo": "elsewhere.example.net/a/napkin"}
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        assert _codes(report) == ["manifest.sources-cross-host"]

    def test_without_a_plexus_repo_url_the_host_check_is_skipped(self, schema_plexus):
        doc = _doc()
        doc["sources"]["napkin"] = {"repo": "https://elsewhere.example.net/a/napkin.git"}
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(schema_plexus.root, report)
        assert _codes(report) == []

    def test_a_credential_in_a_declaration_is_an_error_and_never_echoed(self, schema_plexus):
        host = _sources_host(schema_plexus)
        doc = _doc()
        doc["sources"]["napkin"] = {
            "repo": f"https://user:s3cr3t-token@{host}/agents/napkin.git"
        }
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        finding = _find(report, "manifest.sources-credential")
        assert finding.severity is Severity.ERROR
        assert "s3cr3t-token" not in f"{finding.message} {finding.detail}"

    @pytest.mark.parametrize(
        "entry", [{}, {"ref": "main"}, {"repo": ""}, {"repo": 42}, {"repo": "origin"}]
    )
    def test_a_declaration_without_a_usable_repo_url_is_an_error(self, schema_plexus, entry):
        doc = _doc()
        doc["sources"]["napkin"] = entry
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        assert _find(report, "manifest.sources-repo-invalid").severity is Severity.ERROR

    def test_an_unsupported_source_name_is_not_host_checked(self, schema_plexus):
        # Nothing clones a reserved source, so it carries no credential risk;
        # it is warned about by name and left alone (as load_sources does).
        doc = _doc()
        doc["sources"]["knowledge"] = {"repo": "https://elsewhere.example.net/a/k.git"}
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        assert _codes(report) == ["manifest.sources-reserved"]


# ---------------------------------------------------------------------------
# `sources:` precedence — env > manifest sources > legacy sources.yaml
# ---------------------------------------------------------------------------


class TestSourcesPrecedence:
    def test_a_standalone_sources_yaml_beside_a_manifest_warns(self, schema_plexus):
        (schema_plexus.root / "sources.yaml").write_text(
            pf.LEGACY_SOURCES_YAML, encoding="utf-8"
        )
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        finding = _find(report, "manifest.sources-legacy-file")
        assert finding.severity is Severity.WARN
        assert finding.path == "sources.yaml"
        assert report.exit_code == EXIT_CLEAN

    def test_the_legacy_file_alone_is_recognized_legacy_info(self, legacy_plexus):
        report = _report(Form.LEGACY)
        check_manifest(legacy_plexus.root, report)
        finding = _find(report, "manifest.sources-legacy-file")
        assert finding.severity is Severity.INFO
        assert report.exit_code == EXIT_CLEAN

    def test_no_legacy_file_no_finding(self, schema_plexus):
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        assert "manifest.sources-legacy-file" not in _codes(report)


# ---------------------------------------------------------------------------
# `housekeeping:` — the TTL policy shape
# ---------------------------------------------------------------------------


class TestHousekeeping:
    @pytest.mark.parametrize("value", ["365d", "1d", None])
    def test_a_valid_policy_is_silent(self, schema_plexus, value):
        doc = _doc()
        doc["housekeeping"]["napkin"] = {"archive_after": value}
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        assert _codes(report) == []

    @pytest.mark.parametrize("value", [365, "365 days", "d", True, "365D"])
    def test_a_malformed_duration_is_a_shape_error(self, schema_plexus, value):
        doc = _doc()
        doc["housekeeping"]["napkin"] = {"archive_after": value}
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        finding = _find(report, "manifest.housekeeping-policy-invalid")
        assert finding.severity is Severity.ERROR

    def test_an_unknown_area_name_is_the_housekeeping_tools_business(self, schema_plexus):
        doc = _doc()
        doc["housekeeping"]["knowledge"] = {"archive_after": None}
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        assert _codes(report) == []


# ---------------------------------------------------------------------------
# Layering: the envelope only (registry semantics are a sibling's)
# ---------------------------------------------------------------------------


class TestLayering:
    def test_registry_semantics_are_not_checked_here(self, schema_plexus):
        # `remotes: []` beside a `repo:` is a WARN — the REGISTRY's, emitted
        # by its own check module. This one says nothing about it.
        doc = _doc()
        doc["projects"][pf.PROJECT]["remotes"] = []
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        assert _codes(report) == []

    def test_a_duplicate_remote_is_not_this_modules_finding(self, schema_plexus):
        doc = _doc()
        doc["projects"][pf.PROJECT]["remotes"] = [
            pf.REMOTES[0],
            f"https://{pf.REMOTES[0]}.git",
        ]
        _rewrite(schema_plexus, doc)
        report = _report()
        check_manifest(
            schema_plexus.root, report, plexus_repo_url=schema_plexus.primary_repo
        )
        assert _codes(report) == []

    def test_a_bad_short_name_stops_the_loader_and_reads_as_unusable(self, schema_plexus):
        # The loader owns the §1 charset (fail-closed), so a bad key never
        # reaches the registry checks: this module reports the manifest as
        # unusable and quotes the loader's own message.
        doc = _doc()
        doc["projects"]["Not A Name"] = {"description": "nope"}
        _rewrite(schema_plexus, doc)
        report = _report()
        assert check_manifest(schema_plexus.root, report) is None
        finding = _find(report, "manifest.unparseable")
        assert "Not A Name" in f"{finding.message} {finding.detail}"
        assert report.exit_code == EXIT_UNUSABLE

    def test_no_finding_leaves_the_manifest_namespace(self, schema_plexus):
        schema_plexus.manifest.write_text("schema: 2\nname: x\n", encoding="utf-8")
        report = _report()
        check_manifest(schema_plexus.root, report)
        assert all(code.startswith("manifest.") for code in _codes(report))

    def test_the_module_carries_no_second_copy_of_the_url_or_name_rules(self):
        source = inspect.getsource(checks_manifest)
        # The trust-rule host is `repo_url_host`'s answer, the short-name
        # charset is the loader's, and no URL is dissected here: scheme,
        # userinfo and port rules have exactly one home (D21).
        assert "repo_url_host" in source
        assert "SHORT_NAME_RE" not in source
        for banned in ("urlparse", "urllib", "normalize_repo_url"):
            assert banned not in source
