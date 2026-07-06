"""Plan-index lint kernel (issue #90 — checkable plan gates).

`runs/<slug>/plan.index.json` is the machine-readable companion to plan.md:
one entry per plan task carrying id, declared write-scope (`files`, globs
allowed), `deps`, `verify_commands`, and a `session_scope` atomicity
declaration. `work plan check` lints it so the plan gate verifies what
humans previously asserted by hand — the task DAG is acyclic,
dependency-independent tasks touch disjoint paths, and every task declares
it fits a single session (or says in writing why not).

Everything here is PURE (hygiene.mjs discipline, ported shape-for-shape
from the masterplan fork): callers read files and pass text/objects in,
findings come out. No filesystem, no env — every rule is unit-testable
against planted inputs. The IO lives in cli.cmd_plan_check.

Schema v1 is field-compatible with masterplan's plan.index.json where the
two overlap (`id`, `description`, `files`, `verify_commands`, `goals`) so a
future unification is a merge, not a migration; `deps`/`session_scope`/
`shared_files` are the run-state-side additions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase

PLAN_INDEX_FILE = "plan.index.json"
PLAN_INDEX_SCHEMA_VERSION = 1
SESSION_SCOPES = ("one", "multi")

# plan.md task checkboxes — the drift signal compares their count with the
# index's task count. Matches GFM `- [ ]` / `* [x]` items at any indent.
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[[ xX]\]", re.MULTILINE)
# goals.md goal headings, per the masterplan parseGoals convention:
# `## G<number>: <statement>`.
_GOAL_HEADING_RE = re.compile(r"^##\s+(G\d+):", re.MULTILINE | re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    """One lint finding. level is 'error' (blocks the gate) or 'warning'."""

    level: str
    rule: str
    message: str


def _error(rule: str, message: str) -> Finding:
    return Finding("error", rule, message)


def _warning(rule: str, message: str) -> Finding:
    return Finding("warning", rule, message)


def parse_plan_index(text: str) -> tuple[dict | None, list[Finding]]:
    """Parse plan.index.json text into a mapping, or explain why not.

    Returns (index, findings); index is None when the text is unusable
    (invalid JSON, not an object, missing/invalid/newer `schema`) — the
    same read discipline load_state applies to state.yaml.
    """
    try:
        index = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [_error("parse", f"plan.index.json is not valid JSON: {exc}")]
    if not isinstance(index, dict):
        return None, [_error("parse", "plan.index.json must be a JSON object")]
    schema = index.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool):
        return None, [_error(
            "schema",
            'plan.index.json must declare an integer "schema" (current version: '
            f"{PLAN_INDEX_SCHEMA_VERSION})",
        )]
    if schema > PLAN_INDEX_SCHEMA_VERSION:
        return None, [_error(
            "schema",
            f"plan.index.json schema {schema} is newer than supported "
            f"{PLAN_INDEX_SCHEMA_VERSION} — refusing to lint",
        )]
    return index, []


def parse_goal_ids(goals_md_text: str) -> set[str]:
    """Goal ids declared in goals.md (`## G<n>: …` headings), uppercased."""
    return {m.upper() for m in _GOAL_HEADING_RE.findall(goals_md_text or "")}


def count_plan_checkboxes(plan_md_text: str) -> int:
    """Number of GFM task checkboxes in plan.md."""
    return len(_CHECKBOX_RE.findall(plan_md_text or ""))


def paths_overlap(a: str, b: str) -> bool:
    """Whether two declared write-scope entries can address the same path.

    Entries are project-repo-relative paths, globs allowed. Two entries
    overlap when they are equal or either one, read as an fnmatch pattern,
    matches the other — so ``src/*.py`` collides with ``src/foo.py`` in
    both declaration orders. Glob-vs-glob pairs whose *patterns* don't
    textually match each other (``src/a*.py`` vs ``src/*b.py``) are not
    detected — a documented v1 limitation; deciding glob intersection in
    general is not worth the machinery here. Note fnmatch's ``*`` crosses
    ``/`` (unlike shell globs), so ``src/*`` also collides with
    ``src/a/b.py`` — over-detection, never under-detection.
    """
    return a == b or fnmatchcase(a, b) or fnmatchcase(b, a)


def _canonical_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _parse_shared_files(
    index: dict, known_ids: set[str]
) -> tuple[dict[tuple[str, str], list[str]], list[Finding]]:
    """Read the top-level `shared_files` allowlist: {"T2+T4": ["CHANGELOG.yaml"]}.

    Malformed or stale-looking entries degrade to warnings — an allowlist
    problem must never mask (or manufacture) an overlap error.
    """
    findings: list[Finding] = []
    allow: dict[tuple[str, str], list[str]] = {}
    shared = index.get("shared_files")
    if shared is None:
        return allow, findings
    if not isinstance(shared, dict):
        findings.append(_warning(
            "shared-files", "shared_files must be an object mapping "
            '"<id>+<id>" pair keys to path lists — ignoring it'))
        return allow, findings
    for key, paths in shared.items():
        parts = [p.strip() for p in str(key).split("+")]
        if len(parts) != 2 or not all(parts):
            findings.append(_warning(
                "shared-files",
                f'shared_files key "{key}" is not a "<id>+<id>" pair — ignoring it'))
            continue
        unknown = [p for p in parts if p not in known_ids]
        if unknown:
            findings.append(_warning(
                "shared-files",
                f'shared_files key "{key}" names unknown task id(s) '
                f"{', '.join(unknown)} (stale allowlist?) — ignoring it"))
            continue
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            findings.append(_warning(
                "shared-files",
                f'shared_files["{key}"] must be a list of paths — ignoring it'))
            continue
        allow[_canonical_pair(*parts)] = paths
    return allow, findings


def _ancestors(deps_by_id: dict[str, list[str]]) -> dict[str, set[str]]:
    """Transitive dependency closure per task (cycle-tolerant DFS)."""
    closure: dict[str, set[str]] = {}

    def visit(task_id: str, trail: set[str]) -> set[str]:
        if task_id in closure:
            return closure[task_id]
        if task_id in trail:
            return set()  # cycle — reported separately by the Kahn pass
        trail = trail | {task_id}
        acc: set[str] = set()
        for dep in deps_by_id.get(task_id, []):
            if dep in deps_by_id:
                acc.add(dep)
                acc |= visit(dep, trail)
        closure[task_id] = acc
        return acc

    for task_id in deps_by_id:
        visit(task_id, set())
    return closure


def _find_cycle_members(deps_by_id: dict[str, list[str]]) -> list[str]:
    """Task ids that cannot be topologically ordered (Kahn's algorithm)."""
    remaining_deps = {
        task_id: {d for d in deps if d in deps_by_id}
        for task_id, deps in deps_by_id.items()
    }
    resolved: set[str] = set()
    while True:
        ready = [t for t, deps in remaining_deps.items() if not deps - resolved and t not in resolved]
        if not ready:
            break
        resolved.update(ready)
    return sorted(set(deps_by_id) - resolved)


def lint_plan_index(
    index: dict,
    plan_md: str | None = None,
    goals_md: str | None = None,
) -> list[Finding]:
    """Run every lint rule over a parsed plan index. Pure; ordered findings.

    plan_md / goals_md are the sibling documents' text when they exist,
    None when they don't — the drift and goal-ref rules only fire when the
    document they compare against is actually present (the goals rule stays
    soft until develop-goal-freeze lands a real goals contract).
    """
    findings: list[Finding] = []
    tasks = index.get("tasks")
    if not isinstance(tasks, list):
        return [_error("structure", 'plan.index.json "tasks" must be an array')]

    # -- per-task structural validation + id table ---------------------------
    ids: list[str] = []
    deps_by_id: dict[str, list[str]] = {}
    valid_tasks: list[dict] = []
    seen: set[str] = set()
    # (task_id, field) pairs already flagged structurally — the softer
    # convention warnings skip these rather than double-reporting.
    invalid_fields: set[tuple[str, str]] = set()
    for pos, task in enumerate(tasks):
        label = f"task #{pos + 1}"
        if not isinstance(task, dict):
            findings.append(_error("structure", f"{label}: must be an object"))
            continue
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id.strip():
            findings.append(_error("structure", f"{label}: id must be a non-empty string"))
            continue
        task_id = task_id.strip()
        label = f"task {task_id}"
        if task_id in seen:
            findings.append(_error("structure", f"{label}: duplicate id"))
            continue
        seen.add(task_id)
        if not isinstance(task.get("description"), str) or not task["description"].strip():
            findings.append(_error("structure", f"{label}: description must be a non-empty string"))
        for field in ("files", "deps", "verify_commands", "goals"):
            value = task.get(field)
            if value is not None and (
                not isinstance(value, list) or not all(isinstance(v, str) for v in value)
            ):
                findings.append(_error("structure", f"{label}: {field} must be an array of strings"))
                task = {**task, field: []}
                invalid_fields.add((task_id, field))
        # session_scope: the whole point is forcing the atomicity question
        # to be answered in writing at plan time — absent is not "one".
        scope = task.get("session_scope")
        if scope not in SESSION_SCOPES:
            findings.append(_error(
                "session-scope",
                f'{label}: session_scope must be "one" or "multi" (got {scope!r})'))
        elif scope == "multi":
            rationale = task.get("scope_rationale")
            if not isinstance(rationale, str) or not rationale.strip():
                findings.append(_error(
                    "session-scope",
                    f'{label}: session_scope "multi" requires a scope_rationale string'))
        ids.append(task_id)
        # Ids are stripped above; strip dep refs the same way so a stray
        # space never manufactures an unknown-deps error.
        deps_by_id[task_id] = [d.strip() for d in task.get("deps") or []]
        valid_tasks.append({**task, "id": task_id})

    # -- dependency graph: unknown refs, cycles ------------------------------
    known_ids = set(ids)
    for task_id in ids:
        unknown = [d for d in deps_by_id[task_id] if d not in known_ids]
        if unknown:
            findings.append(_error(
                "deps", f"task {task_id}: unknown deps id(s): {', '.join(unknown)}"))
    cycle = _find_cycle_members(deps_by_id)
    if cycle:
        findings.append(_error(
            "cycle", f"dependency cycle among task(s): {', '.join(cycle)}"))

    # -- file overlap between dependency-independent tasks -------------------
    # Two tasks may be worked concurrently exactly when neither is an
    # ancestor of the other — that is when their declared write-scopes must
    # be disjoint (the followup-#3 collision class). Overlaps a pair
    # declares under shared_files (CHANGELOG.yaml, docs indexes) are exempt.
    allow, allow_findings = _parse_shared_files(index, known_ids)
    findings.extend(allow_findings)
    ancestors = _ancestors(deps_by_id)
    overlapping_pairs: set[tuple[str, str]] = set()
    dependent_pairs: set[tuple[str, str]] = set()
    for i, a in enumerate(valid_tasks):
        for b in valid_tasks[i + 1:]:
            a_id, b_id = a["id"], b["id"]
            pair = _canonical_pair(a_id, b_id)
            if a_id in ancestors.get(b_id, set()) or b_id in ancestors.get(a_id, set()):
                dependent_pairs.add(pair)
                continue
            allowed = allow.get(pair, [])
            hits: list[str] = []
            for fa in a.get("files") or []:
                for fb in b.get("files") or []:
                    if not paths_overlap(fa, fb):
                        continue
                    overlapping_pairs.add(pair)
                    # Allowlist entries use the same overlap semantics as
                    # the files themselves, so a glob entry (docs/*.md)
                    # covers the literal paths it matches. An entry must
                    # cover BOTH colliding sides — matching one side says
                    # nothing about the path actually shared with the other.
                    if any(paths_overlap(fa, s) and paths_overlap(fb, s) for s in allowed):
                        continue
                    hits.append(fa if fa == fb else f"{fa} ~ {fb}")
            if hits:
                findings.append(_error(
                    "overlap",
                    f"tasks {pair[0]} and {pair[1]} are dependency-independent "
                    f"but share write-scope: {', '.join(sorted(set(hits)))} "
                    "(declare in shared_files if genuinely shared)"))
    for pair in sorted(allow):
        if pair in overlapping_pairs:
            continue
        if pair in dependent_pairs:
            findings.append(_warning(
                "shared-files",
                f"shared_files declares {pair[0]}+{pair[1]} but they are "
                "dependency-ordered — overlap between them is allowed anyway "
                "(unnecessary allowlist entry)"))
        else:
            findings.append(_warning(
                "shared-files",
                f"shared_files declares {pair[0]}+{pair[1]} but their files "
                "don't overlap (stale allowlist entry)"))

    # -- warnings: drift, receipts convention, goal refs ---------------------
    if plan_md is not None:
        checkboxes = count_plan_checkboxes(plan_md)
        if checkboxes != len(valid_tasks):
            findings.append(_warning(
                "drift",
                f"plan.md has {checkboxes} checkbox(es) but the index has "
                f"{len(valid_tasks)} task(s) — plan and index may have drifted"))
    for task in valid_tasks:
        if not task.get("verify_commands") and (task["id"], "verify_commands") not in invalid_fields:
            findings.append(_warning(
                "verify",
                f"task {task['id']}: no verify_commands — every task should "
                "name the command that proves it (gate-receipts convention)"))
    if goals_md is not None:
        known_goals = parse_goal_ids(goals_md)
        for task in valid_tasks:
            missing = [g for g in task.get("goals") or [] if g.upper() not in known_goals]
            if missing:
                findings.append(_warning(
                    "goals",
                    f"task {task['id']}: goal ref(s) {', '.join(missing)} "
                    "not found in goals.md"))

    return findings


def format_findings(findings: list[Finding]) -> list[str]:
    """Render findings as report lines (errors ❌, warnings ⚠️ )."""
    return [
        ("❌ " if f.level == "error" else "⚠️  ") + f.message
        for f in findings
    ]
