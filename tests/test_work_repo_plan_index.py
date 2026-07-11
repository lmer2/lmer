"""Unit tests for the pure plan-index lint kernel (issue #90).

Every rule is exercised against planted inputs — no filesystem, no env —
mirroring the module's own purity contract. The IO/reporting side is
covered by tests/test_work_repo_plan_check_cli.py.
"""

import json

from work_repo import plan_index
from work_repo.plan_index import (
    Finding,
    count_plan_checkboxes,
    format_findings,
    lint_plan_index,
    parse_goal_ids,
    parse_plan_index,
    paths_overlap,
)


def _task(task_id, **overrides):
    base = {
        "id": task_id,
        "description": f"task {task_id}",
        "files": [],
        "deps": [],
        "verify_commands": ["gate-check"],
        "session_scope": "one",
    }
    base.update(overrides)
    return base


def _index(*tasks, **extra):
    return {"schema": 1, "tasks": list(tasks), **extra}


def _errors(findings):
    return [f for f in findings if f.level == "error"]


def _warnings(findings):
    return [f for f in findings if f.level == "warning"]


def _messages(findings):
    return "\n".join(f.message for f in findings)


class TestParsePlanIndex:
    def test_valid_document_parses_clean(self):
        index, findings = parse_plan_index(json.dumps(_index(_task("T1"))))
        assert findings == []
        assert index["schema"] == 1

    def test_invalid_json_is_an_error(self):
        index, findings = parse_plan_index("{not json")
        assert index is None
        assert [f.rule for f in _errors(findings)] == ["parse"]

    def test_non_object_root_is_an_error(self):
        index, findings = parse_plan_index("[1, 2]")
        assert index is None
        assert _errors(findings)

    def test_missing_schema_is_an_error(self):
        index, findings = parse_plan_index(json.dumps({"tasks": []}))
        assert index is None
        assert [f.rule for f in findings] == ["schema"]

    def test_newer_schema_is_refused(self):
        index, findings = parse_plan_index(json.dumps({"schema": 99, "tasks": []}))
        assert index is None
        assert "newer than supported" in _messages(findings)

    def test_boolean_schema_is_not_an_integer(self):
        index, findings = parse_plan_index(json.dumps({"schema": True, "tasks": []}))
        assert index is None
        assert [f.rule for f in findings] == ["schema"]


class TestStructuralRules:
    def test_clean_index_has_no_findings(self):
        findings = lint_plan_index(_index(_task("T1"), _task("T2", deps=["T1"])))
        assert findings == []

    def test_tasks_must_be_an_array(self):
        findings = lint_plan_index({"schema": 1, "tasks": {}})
        assert [f.rule for f in findings] == ["structure"]

    def test_task_must_be_an_object(self):
        findings = lint_plan_index(_index("not-a-task"))
        assert "task #1: must be an object" in _messages(_errors(findings))

    def test_id_must_be_non_empty_string(self):
        findings = lint_plan_index(_index(_task("  "), _task(7)))
        errors = _errors(findings)
        assert len([f for f in errors if "id must be a non-empty string" in f.message]) == 2

    def test_duplicate_ids_error(self):
        findings = lint_plan_index(_index(_task("T1"), _task("T1")))
        assert "task T1: duplicate id" in _messages(_errors(findings))

    def test_description_required(self):
        findings = lint_plan_index(_index(_task("T1", description="")))
        assert "description must be a non-empty string" in _messages(_errors(findings))

    def test_list_fields_must_be_string_arrays(self):
        findings = lint_plan_index(_index(_task("T1", files="src/a.py", goals=[1])))
        msgs = _messages(_errors(findings))
        assert "files must be an array of strings" in msgs
        assert "goals must be an array of strings" in msgs


class TestSessionScopeRules:
    def test_missing_session_scope_errors(self):
        task = _task("T1")
        del task["session_scope"]
        findings = lint_plan_index(_index(task))
        assert 'session_scope must be "one" or "multi"' in _messages(_errors(findings))

    def test_invalid_session_scope_errors(self):
        findings = lint_plan_index(_index(_task("T1", session_scope="both")))
        assert 'session_scope must be "one" or "multi"' in _messages(_errors(findings))

    def test_multi_without_rationale_errors(self):
        findings = lint_plan_index(_index(_task("T1", session_scope="multi")))
        assert "requires a scope_rationale" in _messages(_errors(findings))

    def test_multi_with_rationale_passes(self):
        findings = lint_plan_index(_index(
            _task("T1", session_scope="multi",
                  scope_rationale="cross-repo migration, needs two sessions")))
        assert _errors(findings) == []


class TestDependencyRules:
    def test_unknown_dep_errors(self):
        findings = lint_plan_index(_index(_task("T1", deps=["T9"])))
        assert "unknown deps id(s): T9" in _messages(_errors(findings))

    def test_ids_and_deps_are_whitespace_stripped_alike(self):
        findings = lint_plan_index(_index(
            _task(" T1 "), _task("T2", deps=[" T1 "])))
        assert findings == []

    def test_cycle_errors_and_names_members(self):
        findings = lint_plan_index(_index(
            _task("T1", deps=["T2"]), _task("T2", deps=["T1"]), _task("T3")))
        msgs = _messages(_errors(findings))
        assert "dependency cycle among task(s): T1, T2" in msgs
        assert "T3" not in msgs

    def test_self_dependency_is_a_cycle(self):
        findings = lint_plan_index(_index(_task("T1", deps=["T1"])))
        assert "dependency cycle" in _messages(_errors(findings))


class TestOverlapRules:
    def test_independent_tasks_sharing_a_file_error(self):
        findings = lint_plan_index(_index(
            _task("T1", files=["src/cli.py"]), _task("T2", files=["src/cli.py"])))
        msgs = _messages(_errors(findings))
        assert "T1 and T2" in msgs and "src/cli.py" in msgs

    def test_dependent_tasks_may_share_files(self):
        findings = lint_plan_index(_index(
            _task("T1", files=["src/cli.py"]),
            _task("T2", files=["src/cli.py"], deps=["T1"])))
        assert _errors(findings) == []

    def test_transitive_ancestry_also_exempts(self):
        findings = lint_plan_index(_index(
            _task("T1", files=["src/cli.py"]),
            _task("T2", deps=["T1"]),
            _task("T3", files=["src/cli.py"], deps=["T2"])))
        assert _errors(findings) == []

    def test_glob_vs_literal_overlap_detected_both_ways(self):
        assert paths_overlap("src/*.py", "src/foo.py")
        assert paths_overlap("src/foo.py", "src/*.py")
        findings = lint_plan_index(_index(
            _task("T1", files=["src/*.py"]), _task("T2", files=["src/foo.py"])))
        assert _errors(findings)

    def test_disjoint_globs_pass(self):
        findings = lint_plan_index(_index(
            _task("T1", files=["src/*.py"]), _task("T2", files=["docs/*.md"])))
        assert findings == []

    def test_shared_files_allowlist_exempts_declared_path(self):
        findings = lint_plan_index(_index(
            _task("T1", files=["CHANGELOG.yaml", "src/a.py"]),
            _task("T2", files=["CHANGELOG.yaml", "src/b.py"]),
            shared_files={"T1+T2": ["CHANGELOG.yaml"]}))
        assert findings == []

    def test_allowlist_key_order_is_insensitive(self):
        findings = lint_plan_index(_index(
            _task("T1", files=["CHANGELOG.yaml"]),
            _task("T2", files=["CHANGELOG.yaml"]),
            shared_files={"T2+T1": ["CHANGELOG.yaml"]}))
        assert findings == []

    def test_undeclared_overlap_still_errors_beside_allowlisted_one(self):
        findings = lint_plan_index(_index(
            _task("T1", files=["CHANGELOG.yaml", "src/a.py"]),
            _task("T2", files=["CHANGELOG.yaml", "src/a.py"]),
            shared_files={"T1+T2": ["CHANGELOG.yaml"]}))
        msgs = _messages(_errors(findings))
        assert "src/a.py" in msgs
        assert "CHANGELOG.yaml" not in msgs

    def test_allowlist_glob_entry_covers_matching_literals(self):
        findings = lint_plan_index(_index(
            _task("T1", files=["docs/a.md"]), _task("T2", files=["docs/a.md"]),
            shared_files={"T1+T2": ["docs/*.md"]}))
        assert findings == []

    def test_one_sided_allowlist_entry_does_not_exempt(self):
        # The reviewer's repro (MR !115): an entry overlapping only ONE side
        # of the colliding pair says nothing about the actually-shared path
        # and must not suppress the error.
        findings = lint_plan_index(_index(
            _task("T1", files=["docs/*.md"]), _task("T2", files=["docs/b.md"]),
            shared_files={"T1+T2": ["docs/a.md"]}))
        msgs = _messages(_errors(findings))
        assert "docs/*.md ~ docs/b.md" in msgs

    def test_dependency_ordered_allowlist_pair_warns_as_unnecessary(self):
        findings = lint_plan_index(_index(
            _task("T1", files=["src/a.py"]),
            _task("T2", files=["src/a.py"], deps=["T1"]),
            shared_files={"T1+T2": ["src/a.py"]}))
        assert _errors(findings) == []
        msgs = _messages(_warnings(findings))
        assert "dependency-ordered" in msgs
        assert "don't overlap" not in msgs

    def test_slash_crossing_glob_over_detects(self):
        assert paths_overlap("src/*", "src/a/b.py")

    def test_stale_allowlist_pair_warns(self):
        findings = lint_plan_index(_index(
            _task("T1", files=["src/a.py"]), _task("T2", files=["src/b.py"]),
            shared_files={"T1+T2": ["CHANGELOG.yaml"]}))
        assert _errors(findings) == []
        assert "stale allowlist entry" in _messages(_warnings(findings))

    def test_allowlist_with_unknown_id_warns_and_is_ignored(self):
        findings = lint_plan_index(_index(
            _task("T1", files=["src/a.py"]), _task("T2", files=["src/a.py"]),
            shared_files={"T1+T9": ["src/a.py"]}))
        assert "unknown task id(s) T9" in _messages(_warnings(findings))
        assert _errors(findings)  # the overlap still errors

    def test_malformed_allowlist_degrades_to_warning(self):
        findings = lint_plan_index(_index(
            _task("T1"), shared_files={"T1": ["a"], "T1+": ["b"]}))
        warnings = _warnings(findings)
        assert len(warnings) == 2
        assert _errors(findings) == []


class TestWarnings:
    def test_checkbox_drift_warns(self):
        plan_md = "# Plan\n\n- [ ] T1 thing\n- [x] T2 thing\n- [ ] T3 thing\n"
        findings = lint_plan_index(_index(_task("T1"), _task("T2")), plan_md=plan_md)
        assert "3 checkbox(es)" in _messages(_warnings(findings))

    def test_matching_checkbox_count_is_silent(self):
        plan_md = "- [ ] T1\n- [ ] T2\n"
        findings = lint_plan_index(_index(_task("T1"), _task("T2")), plan_md=plan_md)
        assert findings == []

    def test_no_plan_md_skips_drift_rule(self):
        findings = lint_plan_index(_index(_task("T1")))
        assert findings == []

    def test_empty_verify_commands_warns(self):
        findings = lint_plan_index(_index(_task("T1", verify_commands=[])))
        assert "no verify_commands" in _messages(_warnings(findings))

    def test_invalid_verify_commands_errors_without_double_warning(self):
        findings = lint_plan_index(_index(_task("T1", verify_commands="gate-check")))
        assert "verify_commands must be an array of strings" in _messages(_errors(findings))
        assert "no verify_commands" not in _messages(_warnings(findings))

    def test_goal_refs_checked_only_when_goals_md_exists(self):
        index = _index(_task("T1", goals=["G1", "G7"]))
        assert lint_plan_index(index) == []
        findings = lint_plan_index(index, goals_md="## G1: ship it\n")
        msgs = _messages(_warnings(findings))
        assert "G7" in msgs and "G1" not in msgs


class TestGoalCoverage:
    """Issue #91 D3: active goals with zero covering tasks warn (soft v1)."""

    GOALS_MD = (
        "## G1: ship it\nsignal: test\nevidence: e\n\n"
        "## G2: document it\nsignal: docs\nevidence: e\n"
    )

    def test_uncovered_goal_warns(self):
        findings = lint_plan_index(
            _index(_task("T1", goals=["G1"])), goals_md=self.GOALS_MD)
        msgs = _messages(_warnings(findings))
        assert "G2 has no covering task" in msgs
        assert not _errors(findings)

    def test_index_goals_refs_cover(self):
        findings = lint_plan_index(
            _index(_task("T1", goals=["G1"]), _task("T2", goals=["g2"], deps=["T1"])),
            goals_md=self.GOALS_MD)
        assert "covering task" not in _messages(findings)

    def test_plan_md_mention_is_the_fallback(self):
        findings = lint_plan_index(
            _index(_task("T1", goals=["G1"])),
            plan_md="- [ ] T1 also lands G2 docs\n",
            goals_md=self.GOALS_MD)
        assert "covering task" not in _messages(findings)
        # An embedded token (G2x / docs-G2) is not a mention.
        findings = lint_plan_index(
            _index(_task("T1", goals=["G1"])),
            plan_md="- [ ] T1 lands G2x\n",
            goals_md=self.GOALS_MD)
        assert "G2 has no covering task" in _messages(_warnings(findings))

    def test_tombstoned_goals_need_no_coverage(self):
        goals_md = self.GOALS_MD + (
            "\n## G3: dropped\ntombstone_reason: descoped\n"
            "tombstone_at: 2026-07-01T00:00:00Z\n"
        )
        findings = lint_plan_index(
            _index(_task("T1", goals=["G1", "G2"])), goals_md=goals_md)
        assert "covering task" not in _messages(findings)

    def test_no_goals_md_skips_coverage(self):
        assert lint_plan_index(_index(_task("T1"))) == []


class TestTextHelpers:
    def test_parse_goal_ids(self):
        text = "topic: x\n\n## G1: first\nsignal: s\n\n## g2: second\n### G3: not a goal heading\n"
        assert parse_goal_ids(text) == {"G1", "G2"}

    def test_count_plan_checkboxes(self):
        text = "- [ ] a\n  * [x] b\n- [X] c\n- not one\n[ ] nor this\n"
        assert count_plan_checkboxes(text) == 3

    def test_format_findings_prefixes(self):
        lines = format_findings([
            Finding("error", "cycle", "boom"),
            Finding("warning", "drift", "meh"),
        ])
        assert lines == ["❌ boom", "⚠️  meh"]

    def test_helpers_tolerate_empty_input(self):
        assert parse_goal_ids("") == set()
        assert count_plan_checkboxes("") == 0
