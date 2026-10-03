"""``lmer_schema.checks.findings`` — the validator's finding model and
strictness-tier engine (spec §7, D14; issue #353).

This is the foundation every check module plugs into, so these tests pin the
CONTRACT the later modules register against rather than any check logic
(there is none here):

- ``Finding`` is an immutable value: severity (ERROR/WARN/INFO), stable
  machine code, repo-relative path, message, optional detail;
- ``Report`` maps to the validator's own exit taxonomy — 0 clean (warnings
  and info never block), 1 errors, 2 unusable input — where "unusable" is a
  report STATE the caller sets (a ``ManifestError``, a missing path), never a
  finding severity; explicitly NOT the 0/2/64 codes of
  ``lmer_cli.container.sources``;
- the tier engine is a CENTRAL map keyed by code: a code is registered once
  with its schema-form severity and, only when D14 makes it tier-sensitive, a
  strictly lower legacy-tier severity. ``Report.add`` resolves the severity
  from the registry and the form, so no call site chooses one;
- the D14 table itself: dangling index symlinks and index entries without a
  file are ERROR in schema form and WARN in a legacy tree; a memory-scope
  mismatch is WARN / INFO; state-file SHAPE violations (and the non-relative
  / escaping symlink shapes) are ERROR in every form;
- the code registry rejects duplicates and malformed codes, and its inventory
  is what ``--list-checks`` enumerates;
- findings sort deterministically (severity, path, code) and the report
  round-trips through JSON.
"""

import dataclasses
import json
from enum import Enum
from pathlib import Path, PurePath, PurePosixPath

import pytest

from lmer_schema.checks import findings as f
from lmer_schema.checks import (
    CHECKS,
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_UNUSABLE,
    NAMESPACES,
    SEVERITIES,
    CheckCode,
    CheckRegistry,
    Finding,
    Form,
    Report,
    Severity,
    Tier,
)
from tests import plexus_fixtures as pf


def _registry():
    """A fresh registry with one code per severity, none tier-sensitive, plus
    one downgraded code — enough to drive a Report without touching the
    global table."""
    reg = CheckRegistry()
    reg.register("tree.err", Severity.ERROR, tier=Tier.MACHINE, summary="an error")
    reg.register("tree.warn", Severity.WARN, tier=Tier.MACHINE, summary="a warning")
    reg.register("tree.info", Severity.INFO, tier=Tier.NOT_SCHEMA, summary="an info")
    reg.register(
        "index.soft",
        Severity.ERROR,
        legacy=Severity.WARN,
        tier=Tier.MACHINE,
        summary="an integrity finding the legacy tier softens",
    )
    return reg


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------


class TestSeverity:
    def test_exactly_three_members_in_report_order(self):
        assert SEVERITIES == (Severity.ERROR, Severity.WARN, Severity.INFO)
        assert set(Severity) == set(SEVERITIES)

    def test_values_are_the_upper_case_names(self):
        assert [s.value for s in SEVERITIES] == ["ERROR", "WARN", "INFO"]

    def test_rank_orders_error_before_warn_before_info(self):
        assert Severity.ERROR.rank < Severity.WARN.rank < Severity.INFO.rank
        assert sorted(reversed(SEVERITIES), key=lambda s: s.rank) == list(SEVERITIES)

    @pytest.mark.parametrize("raw", ["ERROR", "error", Severity.ERROR])
    def test_coerce_accepts_name_case_insensitively_and_members(self, raw):
        assert Severity.coerce(raw) is Severity.ERROR

    @pytest.mark.parametrize("raw", ["fatal", "", None, 1])
    def test_coerce_rejects_anything_else(self, raw):
        with pytest.raises(ValueError):
            Severity.coerce(raw)


# ---------------------------------------------------------------------------
# Form — the two answers form detection gives (D20)
# ---------------------------------------------------------------------------


class TestForm:
    def test_exactly_two_members(self):
        assert {m.value for m in Form} == {"schema", "legacy"}
        assert Form.SCHEMA.value == "schema"
        assert Form.LEGACY.value == "legacy"

    def test_spelling_matches_the_fixture_constants(self):
        assert Form.coerce(pf.SCHEMA_FORM) is Form.SCHEMA
        assert Form.coerce(pf.LEGACY_FORM) is Form.LEGACY

    def test_coerce_accepts_members_and_strings(self):
        assert Form.coerce(Form.SCHEMA) is Form.SCHEMA
        assert Form.coerce("SCHEMA") is Form.SCHEMA
        assert Form.coerce("legacy") is Form.LEGACY

    def test_coerce_accepts_a_sibling_enum_that_is_this_vocabulary(self):
        # Form detection defines its own two-valued enum and neither module
        # imports the other; same class name, same two values, so it reads.
        sibling = Enum("Form", {"SCHEMA": "schema", "LEGACY": "legacy"})
        assert Form.coerce(sibling.LEGACY) is Form.LEGACY

    @pytest.mark.parametrize("value", [Tier.LEGACY, Severity.ERROR])
    def test_coerce_refuses_a_foreign_enum_even_when_it_spells_a_form(self, value):
        # `Tier.LEGACY.value == "legacy"`: the tier of a SURFACE, not the
        # form of a TREE. Accepting it would silently put a whole report on
        # the other severity table (!254 nits).
        with pytest.raises(ValueError, match="vocabulary"):
            Form.coerce(value)

    def test_coerce_refuses_a_near_miss_vocabulary(self):
        # Right name, wrong membership — not this vocabulary.
        extra = Enum("Form", {"SCHEMA": "schema", "LEGACY": "legacy", "OTHER": "other"})
        with pytest.raises(ValueError):
            Form.coerce(extra.SCHEMA)

    @pytest.mark.parametrize("raw", ["nested", "", None, True])
    def test_coerce_rejects_anything_else(self, raw):
        with pytest.raises(ValueError):
            Form.coerce(raw)

    def test_coerce_rejects_a_duck_typed_value_holder(self):
        # A plain object that happens to carry `.value` is not the sibling
        # enum this contract admits; the enum test above is the whole
        # interoperability surface.
        class Other:
            value = "legacy"

        with pytest.raises(ValueError):
            Form.coerce(Other())


# ---------------------------------------------------------------------------
# Tier — the SCHEMA.md inventory vocabulary
# ---------------------------------------------------------------------------


class TestTier:
    def test_matches_the_inventory_column_vocabulary(self):
        assert {t.value for t in Tier} == {
            "machine",
            "free-form",
            "opaque",
            "legacy",
            "not-schema",
        }

    def test_coerce(self):
        assert Tier.coerce("free-form") is Tier.FREE_FORM
        assert Tier.coerce(Tier.OPAQUE) is Tier.OPAQUE
        with pytest.raises(ValueError):
            Tier.coerce("strict")


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------


class TestFinding:
    def test_construction_and_defaults(self):
        fd = Finding("tree.err", Severity.ERROR, "lmer/_runs", "broken")
        assert fd.code == "tree.err"
        assert fd.severity is Severity.ERROR
        assert fd.path == "lmer/_runs"
        assert fd.message == "broken"
        assert fd.detail is None

    def test_detail_is_kept_when_given(self):
        fd = Finding("tree.err", "ERROR", "x", "m", detail="target: ../gone.md")
        assert fd.detail == "target: ../gone.md"

    def test_severity_is_coerced_from_its_name(self):
        assert Finding("tree.err", "warn", "x", "m").severity is Severity.WARN

    def test_is_immutable(self):
        fd = Finding("tree.err", Severity.ERROR, "x", "m")
        with pytest.raises(dataclasses.FrozenInstanceError):
            fd.message = "changed"
        with pytest.raises(dataclasses.FrozenInstanceError):
            fd.severity = Severity.INFO

    def test_equality_is_by_value(self):
        a = Finding("tree.err", Severity.ERROR, "x", "m")
        b = Finding("tree.err", "ERROR", PurePosixPath("x"), "m")
        assert a == b
        assert hash(a) == hash(b)

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("lmer/_plans/PLANS.md", "lmer/_plans/PLANS.md"),
            (PurePath("lmer") / "_plans", "lmer/_plans"),
            ("./lmer//_plans/", "lmer/_plans"),
            ("", "."),
            (".", "."),
        ],
    )
    def test_path_is_a_normalised_repo_relative_posix_string(self, raw, expected):
        assert Finding("tree.err", Severity.INFO, raw, "m").path == expected

    @pytest.mark.parametrize("raw", ["../outside", "a/../../outside", PurePosixPath("../x")])
    def test_a_dot_dot_hop_is_rejected(self, raw):
        # Same reason as an absolute path: a finding addresses the tree being
        # validated, and `..` leaves it (!254 nits).
        with pytest.raises(ValueError, match="inside the plexus"):
            Finding("tree.err", Severity.WARN, raw, "m")

    @pytest.mark.parametrize("raw", ["/abs/plexus/lmer", Path("/abs")])
    def test_absolute_path_is_rejected(self, raw):
        with pytest.raises(ValueError, match="repo-relative"):
            Finding("tree.err", Severity.INFO, raw, "m")

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"code": ""},
            {"code": None},
            {"message": ""},
            {"message": None},
            {"detail": 3},
        ],
    )
    def test_empty_or_mistyped_fields_are_rejected(self, kwargs):
        base = dict(code="tree.err", severity=Severity.ERROR, path="x", message="m")
        with pytest.raises((ValueError, TypeError)):
            Finding(**{**base, **kwargs})

    def test_sort_key_is_severity_then_path_then_code(self):
        fd = Finding("tree.err", Severity.WARN, "b", "m")
        assert fd.sort_key[:3] == (Severity.WARN.rank, "b", "tree.err")

    def test_to_dict_and_from_dict_round_trip(self):
        fd = Finding("tree.err", Severity.ERROR, "x/y", "m", detail="d")
        doc = fd.to_dict()
        assert doc == {
            "code": "tree.err",
            "severity": "ERROR",
            "path": "x/y",
            "message": "m",
            "detail": "d",
        }
        assert Finding.from_dict(json.loads(json.dumps(doc))) == fd
        # ``detail`` may be omitted entirely by a hand-written document.
        doc.pop("detail")
        assert Finding.from_dict(doc).detail is None


# ---------------------------------------------------------------------------
# CheckRegistry — the code namespace every check module registers against
# ---------------------------------------------------------------------------


class TestCheckRegistry:
    def test_register_returns_the_check_code(self):
        reg = CheckRegistry()
        cc = reg.register("tree.x", Severity.WARN, tier=Tier.MACHINE, summary="s")
        assert isinstance(cc, CheckCode)
        assert cc.code == "tree.x"
        assert cc.namespace == "tree"
        assert cc.severity is Severity.WARN
        assert cc.legacy is Severity.WARN
        assert cc.tier is Tier.MACHINE
        assert cc.summary == "s"
        assert not cc.tier_sensitive
        assert "tree.x" in reg
        assert reg["tree.x"] is cc
        assert len(reg) == 1

    def test_severity_and_tier_are_coerced_from_strings(self):
        reg = CheckRegistry()
        cc = reg.register("tree.x", "error", legacy="warn", tier="machine", summary="s")
        assert cc.severity is Severity.ERROR
        assert cc.legacy is Severity.WARN
        assert cc.tier is Tier.MACHINE
        assert cc.tier_sensitive

    def test_rejects_duplicates(self):
        reg = _registry()
        with pytest.raises(ValueError, match="already registered"):
            reg.register("tree.err", Severity.WARN, tier=Tier.MACHINE, summary="again")
        # The first registration is untouched.
        assert reg["tree.err"].severity is Severity.ERROR

    @pytest.mark.parametrize(
        "code",
        [
            "nodot",
            "Tree.x",
            "tree.X",
            "tree.",
            ".x",
            "tree.a_b",
            "tree.-a",
            "tree.a-",
            "tree.a.b",
            "tree x",
            "",
        ],
    )
    def test_rejects_malformed_codes(self, code):
        reg = CheckRegistry()
        with pytest.raises(ValueError, match="check code"):
            reg.register(code, Severity.ERROR, tier=Tier.MACHINE, summary="s")

    def test_rejects_unknown_namespace(self):
        reg = CheckRegistry()
        with pytest.raises(ValueError, match="namespace"):
            reg.register("wiki.x", Severity.ERROR, tier=Tier.MACHINE, summary="s")

    def test_namespace_vocabulary_covers_the_seven_check_modules(self):
        assert {"manifest", "registry", "tree", "agents", "record", "index", "runner"} <= set(
            NAMESPACES
        )
        assert list(NAMESPACES) == sorted(NAMESPACES)

    @pytest.mark.parametrize(
        "severity, legacy",
        [
            (Severity.ERROR, Severity.ERROR),  # not a downgrade
            (Severity.WARN, Severity.ERROR),  # an upgrade
            (Severity.INFO, Severity.WARN),  # an upgrade
        ],
    )
    def test_legacy_severity_must_be_a_strict_downgrade(self, severity, legacy):
        reg = CheckRegistry()
        with pytest.raises(ValueError, match="downgrade"):
            reg.register("index.x", severity, legacy=legacy, tier=Tier.MACHINE, summary="s")

    def test_rejects_empty_summary(self):
        reg = CheckRegistry()
        with pytest.raises(ValueError, match="summary"):
            reg.register("tree.x", Severity.ERROR, tier=Tier.MACHINE, summary="")

    def test_severity_for_resolves_by_form(self):
        reg = _registry()
        assert reg.severity_for("index.soft", Form.SCHEMA) is Severity.ERROR
        assert reg.severity_for("index.soft", Form.LEGACY) is Severity.WARN
        assert reg.severity_for("index.soft", "legacy") is Severity.WARN
        assert reg.severity_for("tree.err", Form.LEGACY) is Severity.ERROR
        with pytest.raises(KeyError):
            reg.severity_for("tree.unknown", Form.SCHEMA)

    def test_iteration_and_inventory_are_sorted_by_code(self):
        reg = _registry()
        codes = [cc.code for cc in reg]
        assert codes == sorted(codes)
        inv = reg.inventory()
        assert [row["code"] for row in inv] == codes
        assert inv[0] == {
            "code": "index.soft",
            "severity": "ERROR",
            "legacy": "WARN",
            "tier": "machine",
            "summary": "an integrity finding the legacy tier softens",
        }
        assert json.loads(json.dumps(inv)) == inv

    def test_tier_sensitive_lists_only_downgraded_codes(self):
        reg = _registry()
        assert [cc.code for cc in reg.tier_sensitive()] == ["index.soft"]


# ---------------------------------------------------------------------------
# The D14 tier table — pre-registered on the global registry
# ---------------------------------------------------------------------------


#: The integrity codes D14 names, with their (schema, legacy) severities.
D14_DOWNGRADES = {
    "index.dangling-link": (Severity.ERROR, Severity.WARN),
    "index.entry-without-file": (Severity.ERROR, Severity.WARN),
    "agents.memory-entry-without-file": (Severity.ERROR, Severity.WARN),
    "agents.memory-scope-mismatch": (Severity.WARN, Severity.INFO),
}

#: Shape codes D14 keeps at ERROR "wherever they live".
D14_ANY_FORM_ERRORS = (
    "record.state-unparseable",
    "record.state-schema",
    "record.state-enum",
    "record.events-line-unparseable",
    "index.link-not-relative",
    "index.link-escapes",
)


class TestD14TierTable:
    @pytest.mark.parametrize("code, expected", sorted(D14_DOWNGRADES.items()))
    def test_integrity_codes_are_downgraded_in_a_legacy_tree(self, code, expected):
        schema_sev, legacy_sev = expected
        assert CHECKS[code].tier_sensitive
        assert CHECKS.severity_for(code, Form.SCHEMA) is schema_sev
        assert CHECKS.severity_for(code, Form.LEGACY) is legacy_sev

    @pytest.mark.parametrize("code", D14_ANY_FORM_ERRORS)
    def test_shape_codes_are_error_in_every_form(self, code):
        assert not CHECKS[code].tier_sensitive
        for form in Form:
            assert CHECKS.severity_for(code, form) is Severity.ERROR

    def test_state_file_shape_codes_are_never_downgraded(self):
        record = [cc for cc in CHECKS if cc.namespace == "record"]
        assert record, "the record namespace must carry the state-file shape codes"
        assert all(not cc.tier_sensitive for cc in record)

    def test_every_registered_code_is_well_formed(self):
        for cc in CHECKS:
            assert f.CODE_RE.match(cc.code), cc.code
            assert cc.namespace in NAMESPACES, cc.code
            assert cc.summary

    def test_the_global_table_is_the_module_level_registry(self):
        assert f.CHECKS is CHECKS
        assert f.register.__self__ is CHECKS


# ---------------------------------------------------------------------------
# Report — collection, exit taxonomy, sorting, JSON
# ---------------------------------------------------------------------------


class TestReportExitTaxonomy:
    def test_constants(self):
        assert (EXIT_CLEAN, EXIT_ERRORS, EXIT_UNUSABLE) == (0, 1, 2)

    def test_empty_report_is_clean(self):
        rep = Report(Form.SCHEMA, checks=_registry())
        assert rep.exit_code == EXIT_CLEAN
        assert not rep.has_errors
        assert rep.unusable is None
        assert len(rep) == 0

    def test_warnings_and_info_never_block(self):
        rep = Report(Form.SCHEMA, checks=_registry())
        rep.add("tree.warn", "a", "w")
        rep.add("tree.info", "b", "i")
        assert rep.exit_code == EXIT_CLEAN
        assert rep.counts == {"error": 0, "warn": 1, "info": 1}

    def test_one_error_is_exit_1(self):
        rep = Report(Form.SCHEMA, checks=_registry())
        rep.add("tree.warn", "a", "w")
        rep.add("tree.err", "b", "e")
        assert rep.has_errors
        assert rep.exit_code == EXIT_ERRORS

    def test_unusable_is_exit_2_with_or_without_findings(self):
        rep = Report(Form.SCHEMA, checks=_registry())
        rep.mark_unusable("no such path: plexus")
        assert rep.unusable == "no such path: plexus"
        assert rep.exit_code == EXIT_UNUSABLE
        rep.add("tree.err", "b", "e")
        assert rep.exit_code == EXIT_UNUSABLE

    def test_unusable_needs_a_reason(self):
        rep = Report(Form.SCHEMA, checks=_registry())
        with pytest.raises(ValueError):
            rep.mark_unusable("")

    def test_unusable_is_not_a_severity(self):
        assert not hasattr(Severity, "UNUSABLE")
        assert not hasattr(Severity, "FATAL")


class TestReportAdd:
    def test_add_resolves_severity_from_the_registry_and_form(self):
        schema = Report(Form.SCHEMA, checks=_registry())
        legacy = Report(Form.LEGACY, checks=_registry())
        assert schema.add("index.soft", "p", "m").severity is Severity.ERROR
        assert legacy.add("index.soft", "p", "m").severity is Severity.WARN
        assert legacy.add("tree.err", "p", "m").severity is Severity.ERROR

    def test_form_is_coerced_from_its_string(self):
        assert Report("legacy", checks=_registry()).form is Form.LEGACY

    def test_per_finding_form_override_for_host_buckets(self):
        # A host bucket inside a schema-form tree is checked at the legacy
        # tier's severities (SCHEMA.md "Outside the schema").
        rep = Report(Form.SCHEMA, checks=_registry())
        fd = rep.add("index.soft", "gitlab.example.com/x/specs/a.md", "m", form=Form.LEGACY)
        assert fd.severity is Severity.WARN
        assert rep.add("index.soft", "lmer/_specs/a.md", "m").severity is Severity.ERROR

    def test_add_returns_the_finding_with_detail(self):
        rep = Report(Form.SCHEMA, checks=_registry())
        fd = rep.add("tree.err", PurePath("a/b"), "m", detail="d")
        assert fd == Finding("tree.err", Severity.ERROR, "a/b", "m", detail="d")
        assert rep.findings == (fd,)

    def test_add_rejects_unregistered_code(self):
        rep = Report(Form.SCHEMA, checks=_registry())
        with pytest.raises(KeyError, match="tree.nope"):
            rep.add("tree.nope", "p", "m")
        assert len(rep) == 0

    def test_append_and_extend_take_prebuilt_findings(self):
        rep = Report(Form.SCHEMA, checks=_registry())
        a = Finding("runner.check-failed", Severity.ERROR, ".", "boom")
        b = Finding("tree.info", Severity.INFO, "z", "i")
        rep.append(a)
        rep.extend([b])
        assert rep.findings == (a, b)
        with pytest.raises(TypeError):
            rep.append("not a finding")

    def test_by_severity_views(self):
        rep = Report(Form.SCHEMA, checks=_registry())
        rep.add("tree.err", "a", "e")
        rep.add("tree.warn", "a", "w")
        rep.add("tree.info", "a", "i")
        assert [x.code for x in rep.errors] == ["tree.err"]
        assert [x.code for x in rep.warnings] == ["tree.warn"]
        assert [x.code for x in rep.infos] == ["tree.info"]


class TestReportSorting:
    def _shuffled(self):
        rep = Report(Form.SCHEMA, checks=_registry())
        rep.add("tree.info", "b", "i")
        rep.add("tree.err", "z", "e")
        rep.add("tree.warn", "a", "w")
        rep.add("tree.err", "a", "e2")
        rep.add("tree.warn", "a", "w0")  # same severity+path, sorted by code then message
        rep.append(Finding("index.soft", Severity.WARN, "a", "s"))
        return rep

    def test_findings_sort_by_severity_then_path_then_code(self):
        keys = [(x.severity, x.path, x.code, x.message) for x in self._shuffled().findings]
        assert keys == [
            (Severity.ERROR, "a", "tree.err", "e2"),
            (Severity.ERROR, "z", "tree.err", "e"),
            (Severity.WARN, "a", "index.soft", "s"),
            (Severity.WARN, "a", "tree.warn", "w"),
            (Severity.WARN, "a", "tree.warn", "w0"),
            (Severity.INFO, "b", "tree.info", "i"),
        ]

    def test_order_is_independent_of_insertion_order(self):
        forward = self._shuffled()
        reverse = Report(Form.SCHEMA, checks=_registry())
        reverse.extend(reversed(list(forward.findings)))
        assert reverse.findings == forward.findings
        assert reverse.to_dict() == forward.to_dict()


class TestReportJson:
    def test_to_dict_shape(self):
        rep = Report(Form.LEGACY, checks=_registry())
        rep.add("index.soft", "lmer/specs/a.md", "dangling", detail="-> ../gone.md")
        doc = rep.to_dict()
        assert doc == {
            "form": "legacy",
            "exit_code": 0,
            "unusable": None,
            "counts": {"error": 0, "warn": 1, "info": 0},
            "findings": [
                {
                    "code": "index.soft",
                    "severity": "WARN",
                    "path": "lmer/specs/a.md",
                    "message": "dangling",
                    "detail": "-> ../gone.md",
                }
            ],
        }

    def test_json_round_trip_of_every_state(self):
        reg = _registry()
        clean = Report(Form.SCHEMA, checks=reg)
        clean.add("tree.info", "a", "i")
        errors = Report(Form.SCHEMA, checks=reg)
        errors.add("tree.err", "a", "e")
        errors.add("tree.warn", "b", "w")
        unusable = Report(Form.LEGACY, checks=reg)
        unusable.mark_unusable("unreadable manifest")
        for rep, code in ((clean, 0), (errors, 1), (unusable, 2)):
            doc = rep.to_dict()
            assert doc["exit_code"] == code
            text = json.dumps(doc, sort_keys=True)
            back = Report.from_dict(json.loads(text), checks=reg)
            assert back.form is rep.form
            assert back.unusable == rep.unusable
            assert back.findings == rep.findings
            assert back.exit_code == rep.exit_code
            assert back.to_dict() == doc

    def test_from_dict_rejects_a_document_whose_exit_code_disagrees(self):
        doc = Report(Form.SCHEMA, checks=_registry()).to_dict()
        doc["exit_code"] = 1
        with pytest.raises(ValueError, match="exit_code"):
            Report.from_dict(doc, checks=_registry())


# ---------------------------------------------------------------------------
# Package surface
# ---------------------------------------------------------------------------


def test_checks_package_exports_the_model():
    from lmer_schema import checks

    for name in (
        "Severity",
        "SEVERITIES",
        "Form",
        "Tier",
        "Finding",
        "CheckCode",
        "CheckRegistry",
        "CHECKS",
        "register",
        "Report",
        "EXIT_CLEAN",
        "EXIT_ERRORS",
        "EXIT_UNUSABLE",
        "NAMESPACES",
    ):
        assert name in checks.__all__, name
        assert getattr(checks, name) is getattr(f, name)
