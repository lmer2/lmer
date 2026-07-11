"""Tests for the goals kernel (issue #91 — frozen goal-sets).

Vectors ported from the masterplan fork's test/goals.test.mjs and
test/retro-goals.test.mjs (reference semantics, decision 3 of the spec),
adapted where this port deliberately diverges: `evidence:` is captured and
part of the canonical hash/diff, and verdicts are met/partial/missed/waived.
Waiver and receipt-validator vectors are out of scope (deferred).
"""
import pytest

from work_repo import goals as goals_mod
from work_repo.goals import (
    GOAL_VERDICTS,
    active_goals,
    amendment_diff,
    canonical_goals,
    classify_evidence,
    escape_cell,
    format_findings,
    goals_hash,
    latest_goals_event,
    parse_goals,
    parse_verdict_flag,
    render_verdict_skeleton,
    render_verdict_table,
    validate_amendment,
    validate_goals,
    validate_verdicts,
)


def _errors(findings):
    return [f for f in findings if f.level == "error"]


def _warnings(findings):
    return [f for f in findings if f.level == "warning"]


def _goal(goal_id, text="A", signal="test", evidence="tests pass", **extra):
    return {"id": goal_id, "text": text, "signal": signal,
            "evidence": evidence, **extra}


TOMBSTONE = {"reason": "done", "amended_at": "2026-01-01T00:00:00Z"}


class TestParseGoals:
    def test_extracts_topic_seed_and_one_section_per_goal(self):
        md = (
            "topic: build a widget\n"
            "that delights\n"
            "\n"
            "## G1: Increase coverage\n"
            "signal: test\n"
            "evidence: npm test\n"
            "\n"
            "## G2: Add CLI flag\n"
            "signal: command\n"
        )
        parsed = parse_goals(md)
        assert parsed["topic_seed"] == "build a widget\nthat delights"
        assert len(parsed["goals"]) == 2
        # Divergence from goals.mjs: evidence is captured, not ignored.
        assert parsed["goals"][0] == {
            "id": "G1", "text": "Increase coverage",
            "signal": "test", "evidence": "npm test",
        }
        assert parsed["goals"][1]["id"] == "G2"
        assert parsed["goals"][1]["signal"] == "command"
        assert parsed["goals"][1]["evidence"] == ""

    def test_non_string_input_parses_empty(self):
        assert parse_goals(None) == {"topic_seed": "", "goals": []}

    def test_empty_topic_seed_when_no_topic_line(self):
        assert parse_goals("## G1: x\nsignal: test\n")["topic_seed"] == ""

    def test_goal_heading_interrupts_topic_collection(self):
        parsed = parse_goals("topic: build\n## G1: x\nsignal: test\n")
        assert parsed["topic_seed"] == "build"
        assert [g["id"] for g in parsed["goals"]] == ["G1"]

    def test_reads_tombstoned_goal(self):
        md = (
            "## G3: old goal\n"
            "tombstone_reason: superseded\n"
            "tombstone_at: 2026-07-01T00:00:00Z\n"
        )
        goal = parse_goals(md)["goals"][0]
        assert goal["id"] == "G3"
        assert goal["tombstone"] == {
            "reason": "superseded", "amended_at": "2026-07-01T00:00:00Z",
        }

    def test_heading_case_is_normalized(self):
        # Divergence from goals.mjs (case-sensitive headings): a `## g2:`
        # heading silently dropped from a freeze is a footgun, and every
        # rule (parse, freeze, plan-check refs and coverage) must agree on
        # what a goal heading is.
        parsed = parse_goals("## g1: lower\nsignal: test\nevidence: e\n")
        assert [g["id"] for g in parsed["goals"]] == ["G1"]
        assert goals_hash(parsed) == goals_hash(
            "## G1: lower\nsignal: test\nevidence: e\n")

    def test_body_prose_and_unknown_keys_ignored(self):
        md = (
            "## G1: x\n"
            "signal: test\n"
            "evidence: pytest\n"
            "priority: high\n"
            "\n"
            "Some body prose explaining the goal.\n"
        )
        goal = parse_goals(md)["goals"][0]
        assert goal["signal"] == "test"
        assert goal["evidence"] == "pytest"
        assert "priority" not in goal


class TestValidateGoals:
    def test_accepts_well_formed_active_set(self):
        parsed = parse_goals(
            "## G1: Test\nsignal: test\nevidence: pytest\n\n"
            "## G2: Artifact\nsignal: artifact\nevidence: spec.md\n"
        )
        assert validate_goals(parsed, strict=True) == []
        assert validate_goals(parsed["goals"], strict=True) == []

    def test_rejects_empty_and_all_tombstone_sets(self):
        assert _errors(validate_goals({"topic_seed": "", "goals": []}))
        tombstoned = parse_goals(
            "## G1: Old\ntombstone_reason: done\n"
            "tombstone_at: 2026-01-01T00:00:00Z\n"
        )
        assert _errors(validate_goals(tombstoned))

    def test_rejects_duplicate_ids(self):
        findings = validate_goals([_goal("G1"), _goal("G1", text="B")])
        assert any("duplicate" in f.message.lower() for f in _errors(findings))

    def test_rejects_bad_id_format_and_empty_text(self):
        assert _errors(validate_goals([_goal("X1")]))
        assert _errors(validate_goals([_goal("G1", text="  ")]))

    def test_rejects_incomplete_tombstone(self):
        findings = validate_goals([
            _goal("G1"),
            _goal("G2", tombstone={"reason": ""}),
        ])
        assert _errors(findings)

    def test_signal_and_evidence_warn_on_draft_error_when_strict(self):
        draft = [_goal("G1", signal="vibes"), _goal("G2", evidence="")]
        findings = validate_goals(draft)
        assert not _errors(findings)
        assert len(_warnings(findings)) == 2
        strict = validate_goals(draft, strict=True)
        assert len(_errors(strict)) == 2
        assert any("signal" in f.message for f in strict)
        assert any("evidence" in f.message for f in strict)

    def test_tombstoned_goals_exempt_from_freeze_contract(self):
        findings = validate_goals(
            [_goal("G1"), _goal("G2", signal="", evidence="", tombstone=TOMBSTONE)],
            strict=True,
        )
        assert findings == []

    def test_out_of_order_headings_warn(self):
        findings = validate_goals([_goal("G2"), _goal("G1", text="B")])
        assert not _errors(findings)
        assert any(f.rule == "sequence" for f in _warnings(findings))

    def test_format_findings_marks_levels(self):
        lines = format_findings(validate_goals([_goal("G1", evidence="")]))
        assert lines and lines[0].startswith("⚠️")


class TestValidateAmendment:
    def test_accepts_stable_ids_with_appended_goal(self):
        old = [_goal("G1"), _goal("G2", signal="command")]
        new = old + [_goal("G3", text="C", signal="docs")]
        assert validate_amendment(old, new) == []

    def test_rejects_hard_deletion(self):
        old = [_goal("G1"), _goal("G2")]
        findings = validate_amendment(old, [_goal("G1")])
        assert any("tombstone" in f.message for f in _errors(findings))
        assert any("G2" in f.message for f in _errors(findings))

    def test_accepts_removal_expressed_as_tombstone(self):
        old = [_goal("G1"), _goal("G2")]
        new = [_goal("G1"), _goal("G2", tombstone=TOMBSTONE)]
        assert validate_amendment(old, new) == []

    def test_rejects_renumbering(self):
        old = [_goal("G1"), _goal("G3", text="C")]
        new = old + [_goal("G2", text="B")]
        findings = validate_amendment(old, new)
        assert any("renumber" in f.message for f in _errors(findings))

    def test_propagates_single_doc_invalidity(self):
        assert _errors(validate_amendment([_goal("G1")], []))


class TestAmendmentDiff:
    def test_records_added_modified_tombstoned_and_omits_unchanged(self):
        old = [_goal("G1"), _goal("G2", text="B")]
        new = [
            _goal("G1"),
            _goal("G2", text="B2"),
            _goal("G3", text="C", signal="docs"),
        ]
        diff = {d["id"]: d for d in amendment_diff(old, new)}
        assert "G1" not in diff
        assert diff["G2"]["change"] == "modified"
        assert diff["G2"]["old"]["text"] == "B"
        assert diff["G2"]["new"]["text"] == "B2"
        assert diff["G3"]["change"] == "added"
        assert diff["G3"]["old"] is None

    def test_records_a_tombstoning(self):
        diff = amendment_diff(
            [_goal("G1")], [_goal("G1", tombstone=TOMBSTONE)])
        assert diff[0]["change"] == "tombstoned"
        assert diff[0]["old"] and diff[0]["new"]
        assert diff[0]["new"]["tombstone"] == TOMBSTONE

    def test_evidence_change_counts_as_modified(self):
        diff = amendment_diff(
            [_goal("G1")], [_goal("G1", evidence="other proof")])
        assert diff[0]["change"] == "modified"
        assert diff[0]["new"]["evidence"] == "other proof"

    def test_untombstoning_is_recorded(self):
        # A resurrected goal changes the hash, so it must leave an audit
        # record — a hash-moving amendment with an empty diff is a hole.
        diff = amendment_diff(
            [_goal("G1"), _goal("G2", tombstone=TOMBSTONE)],
            [_goal("G1"), _goal("G2")])
        assert [d["change"] for d in diff] == ["untombstoned"]
        assert diff[0]["old"]["tombstone"] == TOMBSTONE
        assert diff[0]["new"]["tombstone"] is None

    def test_tombstone_detail_edit_counts_as_modified(self):
        diff = amendment_diff(
            [_goal("G1"), _goal("G2", tombstone=TOMBSTONE)],
            [_goal("G1"), _goal("G2", tombstone={**TOMBSTONE, "reason": "other"})])
        assert [d["change"] for d in diff] == ["modified"]


class TestGoalsHash:
    def test_stable_across_whitespace_changes_on_real_edits(self):
        a = "topic: build\n\n## G1: Alpha\nsignal: test\nevidence: pytest\n"
        b = "topic: build\n\n\n## G1: Alpha\nsignal: test\nevidence: pytest\n"
        assert goals_hash(a) == goals_hash(b)
        assert goals_hash(a).startswith("sha256:")
        assert len(goals_hash(a)) == len("sha256:") + 64

        changed = a.replace("Alpha", "Alpha CHANGED")
        assert goals_hash(a) != goals_hash(changed)

        added = a + "\n## G2: Beta\nsignal: command\nevidence: run it\n"
        assert goals_hash(a) != goals_hash(added)

    def test_evidence_participates_in_identity(self):
        # Deliberate divergence from the goals.mjs vector (which asserts
        # evidence is ignored): changing what proves a goal changes it.
        a = "## G1: Alpha\nsignal: test\nevidence: pytest\n"
        b = "## G1: Alpha\nsignal: test\nevidence: manual check\n"
        assert goals_hash(a) != goals_hash(b)

    def test_parsed_object_matches_raw_text_form(self):
        md = "## G1: Alpha\nsignal: test\nevidence: pytest\n"
        assert goals_hash(md) == goals_hash(parse_goals(md))

    def test_canonical_goals_sorts_by_id_string(self):
        parsed = parse_goals(
            "## G2: b\nsignal: test\nevidence: e\n\n"
            "## G1: a\nsignal: test\nevidence: e\n"
        )
        assert [g["id"] for g in canonical_goals(parsed)] == ["G1", "G2"]


class TestLatestGoalsEvent:
    def test_returns_none_without_freeze(self):
        assert latest_goals_event([]) is None
        assert latest_goals_event([{"type": "phase", "note": "execution"}]) is None

    def test_amendment_supersedes_freeze(self):
        frozen = {"type": "goals_frozen",
                  "data": {"goals_hash": "sha256:a", "goals": [_goal("G1")]}}
        amended = {"type": "goal_amended",
                   "data": {"old_goals_hash": "sha256:a",
                            "new_goals_hash": "sha256:b",
                            "goals": [_goal("G1"), _goal("G2")]}}
        latest = latest_goals_event([frozen, amended])
        assert latest["goals_hash"] == "sha256:b"
        assert len(latest["goals"]) == 2

    def test_tolerates_malformed_events(self):
        events = [
            "not a dict",
            {"type": "goals_frozen"},
            {"type": "goals_frozen", "data": {"goals_hash": "sha256:x"}},
            {"type": "goals_frozen",
             "data": {"goals_hash": "sha256:ok", "goals": []}},
        ]
        assert latest_goals_event(events)["goals_hash"] == "sha256:ok"


class TestEvidenceClassification:
    def test_receipt_artifact_and_prose(self):
        receipts = {"t1-tests", "gate-check"}
        artifacts = {"spec.md", "plan.index.json"}
        assert classify_evidence("receipt t1-tests green", receipts, artifacts) == "receipt"
        assert classify_evidence("see spec.md §2", receipts, artifacts) == "artifact"
        assert classify_evidence("I looked at it and it seemed fine", receipts, artifacts) == "prose"
        assert classify_evidence("", receipts, artifacts) == "prose"

    def test_whole_word_matching(self):
        # A receipt name embedded in a longer token does not count.
        assert classify_evidence("t1-tests-extra ran", {"t1-tests"}, set()) == "prose"
        assert classify_evidence("ran t1-tests.", {"t1-tests"}, set()) == "receipt"


class TestVerdictFlagsAndValidation:
    def test_parse_verdict_flag(self):
        assert parse_verdict_flag("G1=met:gate-check receipt") == (
            "G1", "met", "gate-check receipt")

    @pytest.mark.parametrize("bad", ["G1", "G1=met", "G1=met:", "=met:x", "G1=:x"])
    def test_parse_verdict_flag_rejects_malformed(self, bad):
        with pytest.raises(ValueError):
            parse_verdict_flag(bad)

    def test_validate_verdicts_requires_every_active_goal(self):
        goal_set = [_goal("G1"), _goal("G2"),
                    _goal("G3", tombstone=TOMBSTONE)]
        verdicts = {"G1": {"verdict": "met", "evidence": "x"}}
        findings = validate_verdicts(goal_set, verdicts)
        assert any("G2" in f.message for f in _errors(findings))

    def test_validate_verdicts_rejects_bad_enum_empty_evidence_unknown_goal(self):
        goal_set = [_goal("G1"), _goal("G3", tombstone=TOMBSTONE)]
        findings = validate_verdicts(goal_set, {
            "G1": {"verdict": "vibes", "evidence": ""},
            "G3": {"verdict": "met", "evidence": "tombstoned!"},
            "G9": {"verdict": "met", "evidence": "fabricated"},
        })
        messages = [f.message for f in _errors(findings)]
        assert any("vibes" in m for m in messages)
        assert any("evidence" in m for m in messages)
        assert any("G3" in m for m in messages)
        assert any("G9" in m for m in messages)

    def test_complete_map_passes(self):
        goal_set = [_goal("G1"), _goal("G2")]
        assert validate_verdicts(goal_set, {
            "G1": {"verdict": "met", "evidence": "a"},
            "G2": {"verdict": "waived", "evidence": "descoped with approval"},
        }) == []

    def test_verdict_enum(self):
        assert sorted(GOAL_VERDICTS) == ["met", "missed", "partial", "waived"]


class TestRendering:
    def test_escape_cell(self):
        assert escape_cell(None) == "—"
        assert escape_cell("a|b") == "a\\|b"
        assert escape_cell("a\n\t  b") == "a b"

    def test_skeleton_lists_active_rows_and_tombstones(self):
        goal_set = [_goal("G1", text="Alpha"),
                    _goal("G2", text="Beta", tombstone={"reason": "descoped",
                                                        "amended_at": "ts"})]
        out = render_verdict_skeleton(goal_set)
        assert out.startswith("## Goal verdicts")
        assert "| G1 | Alpha | met / partial / missed / waived | — |" in out
        assert "### Tombstoned goals" in out
        assert "- **G2** — descoped" in out

    def test_skeleton_with_zero_goals(self):
        assert "_No goals were recorded for this run._" in render_verdict_skeleton([])

    def test_completed_table_marks_prose_evidence(self):
        goal_set = [_goal("G1", text="Alpha"), _goal("G2", text="Beta")]
        out = render_verdict_table(goal_set, {
            "G1": {"verdict": "met", "evidence": "t1-tests receipt",
                   "evidence_kind": "receipt"},
            "G2": {"verdict": "partial", "evidence": "looked fine",
                   "evidence_kind": "prose"},
        })
        assert "| G1 | Alpha | met | t1-tests receipt |" in out
        assert "| G2 | Beta | partial | looked fine (prose) |" in out

    def test_missing_or_invalid_entries_render_em_dashes(self):
        out = render_verdict_table([_goal("G1", text="Alpha")], {
            "G1": {"verdict": "vibes", "evidence": None},
        })
        assert "| G1 | Alpha | — | — |" in out

    def test_active_goals_helper(self):
        goal_set = [_goal("G1"), _goal("G2", tombstone=TOMBSTONE)]
        assert [g["id"] for g in active_goals(goal_set)] == ["G1"]
        assert [g["id"] for g in active_goals({"goals": goal_set})] == ["G1"]
