"""Goal-set kernel (issue #91 — frozen goal-sets).

`runs/<slug>/goals.md` is the run's goal contract: written during
spec/brainstorm, frozen at spec approval, amended only explicitly
(tombstone-not-renumber), and assessed goal-by-goal at finish. This module
is the pure core behind the `work goals` verbs: parse, draft-vs-strict
validation, amendment rules, canonical hashing, verdict-table rendering,
and evidence classification.

Everything here is PURE (the plan_index.py discipline): callers read files
and event logs and pass text/objects in, findings and rendered strings come
out. No filesystem, no env, no clock — timestamps travel in event data
written by the CLI layer.

Semantics are a minimal Python port of the masterplan fork's
`lib/goals.mjs` / `lib/retro-goals.mjs` (reference semantics and test
vectors only — never a dependency; decision 3 of the approved spec). Three
deliberate divergences:

- `evidence:` is captured by the parser and participates in the canonical
  hash and the amendment diff (masterplan ignores it) — changing what
  proves a goal is a real amendment (spec addendum).
- Verdicts are ``met | partial | missed | waived`` (masterplan:
  achieved/partial/missed); waiver machinery and the anti-fabrication
  receipt validators are explicitly out of scope until receipts mature
  (spec addendum).
- Goal headings match case-insensitively with ids normalized to uppercase
  (masterplan's heading match is case-sensitive) — every rule from parse
  to freeze to plan-check coverage shares one definition of "a goal
  heading" (MR !116 review).
"""

from __future__ import annotations

import hashlib
import json
import re

from .findings import Finding, error as _error, warning as _warning, format_findings

__all__ = [
    "Finding", "format_findings", "GOALS_FILE", "SIGNAL_CLASSES",
    "GOAL_VERDICTS", "GOALS_FROZEN_EVENT", "GOAL_AMENDED_EVENT",
    "GOALS_ASSESSED_EVENT", "parse_goals", "active_goals", "validate_goals",
    "validate_amendment", "amendment_diff", "canonical_goals", "goals_hash",
    "latest_goals_event", "classify_evidence", "parse_verdict_flag",
    "validate_verdicts", "escape_cell", "render_verdict_skeleton",
    "render_verdict_table",
]

GOALS_FILE = "goals.md"
# Signal classes a goal may declare (goals.mjs allowedSignals, unchanged):
# the *kind* of proof — a test, a command run, an artifact, or docs.
SIGNAL_CLASSES = ("test", "command", "artifact", "docs")
# Per-goal verdicts at finish (issue #91; diverges from masterplan's enum).
GOAL_VERDICTS = ("met", "partial", "missed", "waived")
# Event types recorded by the `work goals` verbs. goals_frozen and
# goal_amended carry the canonical goal list + hash in `data`, so the last
# agreed set is always recoverable from events.jsonl alone.
GOALS_FROZEN_EVENT = "goals_frozen"
GOAL_AMENDED_EVENT = "goal_amended"
GOALS_ASSESSED_EVENT = "goals_assessed"

# Case-insensitive with the id normalized to uppercase — a divergence from
# goals.mjs (whose heading match is case-sensitive while its interrupt
# check is not): a `## g2:` heading silently dropped from a freeze is a
# footgun, and one definition of "a goal heading" has to serve every rule
# (parse, freeze, plan-check goal refs AND coverage — MR !116 review).
_GOAL_HEADING_RE = re.compile(r"^##\s+(G\d+):\s*(.*)$", re.IGNORECASE)
_GOAL_HEADING_INTERRUPT_RE = re.compile(r"^##\s+G\d+:", re.IGNORECASE)
_TOPIC_RE = re.compile(r"^topic:", re.IGNORECASE)
_KV_RE = re.compile(r"^(\w+):\s*(.*)$")
_GOAL_ID_RE = re.compile(r"^G\d+$")


def parse_goals(text) -> dict:
    """Parse raw goals.md text into ``{"topic_seed": str, "goals": [...]}``.

    Format (parse-compatible with masterplan's goals.md):

    - ``topic:`` line(s) before the first goal heading become the topic
      seed (collected until a blank line or a goal heading).
    - ``## G<number>: <statement>`` starts a goal section.
    - ``signal:`` / ``evidence:`` / ``tombstone_reason:`` /
      ``tombstone_at:`` key lines inside a section; other keys and body
      prose are ignored.

    Each goal is ``{id, text, signal, evidence}`` plus ``tombstone:
    {reason, amended_at}`` when either tombstone key was present.
    Non-string input parses as empty.
    """
    if not isinstance(text, str):
        return {"topic_seed": "", "goals": []}

    topic_lines: list[str] = []
    collecting_topic = False
    goals: list[dict] = []
    current: dict | None = None

    for line in text.split("\n"):
        stripped = line.strip()

        if current is None and not goals and _TOPIC_RE.match(stripped):
            collecting_topic = True
            after = stripped[len("topic:"):].strip()
            if after:
                topic_lines.append(after)
            continue

        if collecting_topic:
            if stripped == "":
                collecting_topic = False
                continue
            if _GOAL_HEADING_INTERRUPT_RE.match(stripped):
                collecting_topic = False
                # fall through to the heading handling below
            else:
                topic_lines.append(stripped)
                continue

        heading = _GOAL_HEADING_RE.match(stripped)
        if heading:
            if current is not None:
                goals.append(current)
            current = {
                "id": heading.group(1).upper(),
                "text": heading.group(2).strip(),
                "signal": "",
                "evidence": "",
            }
            continue

        if current is not None:
            kv = _KV_RE.match(stripped)
            if kv:
                key, value = kv.group(1), kv.group(2).strip()
                if key == "signal":
                    current["signal"] = value
                elif key == "evidence":
                    current["evidence"] = value
                elif key == "tombstone_reason":
                    current.setdefault("tombstone", {})["reason"] = value
                elif key == "tombstone_at":
                    current.setdefault("tombstone", {})["amended_at"] = value
                # unknown keys are ignored

    if current is not None:
        goals.append(current)

    for goal in goals:
        tombstone = goal.get("tombstone")
        if tombstone is not None and not (
            tombstone.get("reason") or tombstone.get("amended_at")
        ):
            del goal["tombstone"]

    return {"topic_seed": "\n".join(topic_lines).strip(), "goals": goals}


def _goals_list(parsed) -> list[dict] | None:
    """Normalize a parsed object or bare goal list to the goals array."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("goals"), list):
        return parsed["goals"]
    return None


def active_goals(parsed) -> list[dict]:
    """The non-tombstoned goals of a parsed document (or bare list)."""
    return [g for g in (_goals_list(parsed) or []) if not g.get("tombstone")]


def validate_goals(parsed, strict: bool = False) -> list[Finding]:
    """Validate one goals document. Ordered findings, plan_index style.

    Structural rules always error: at least one active goal, unique ids,
    ``G<number>`` id format, non-empty statement, complete tombstones
    (non-empty reason AND amended_at). The freeze contract — ``signal:``
    one of SIGNAL_CLASSES and a non-empty ``evidence:`` on every active
    goal — is a warning on drafts (``strict=False``, `work goals check`)
    and an error at freeze/amend time (``strict=True``): drafts may sketch
    goals before naming their proof, frozen goals may not.
    """
    goals = _goals_list(parsed)
    if goals is None:
        return [_error("structure", "input must be a goals array or an object with a goals array")]

    findings: list[Finding] = []
    require = _error if strict else _warning

    if not any(not g.get("tombstone") for g in goals if isinstance(g, dict)):
        findings.append(_error(
            "structure", "there must be at least one active (non-tombstoned) goal"))

    ids = [g.get("id") for g in goals if isinstance(g, dict)]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        findings.append(_error(
            "structure", f"duplicate goal id(s): {', '.join(str(d) for d in duplicates)}"))

    last_num = 0
    out_of_order: list[str] = []
    for goal in goals:
        if not isinstance(goal, dict):
            findings.append(_error("structure", "each goal must be an object"))
            continue
        goal_id = goal.get("id")
        if not isinstance(goal_id, str) or not _GOAL_ID_RE.match(goal_id):
            findings.append(_error(
                "structure", f"goal id {goal_id!r} must match format G<number>"))
            continue
        text = goal.get("text")
        if not isinstance(text, str) or not text.strip():
            findings.append(_error(
                "structure", f"goal {goal_id} must have a non-empty statement"))
        num = int(goal_id[1:])
        if num <= last_num:
            out_of_order.append(goal_id)
        last_num = max(last_num, num)

        tombstone = goal.get("tombstone")
        if tombstone is not None:
            if not isinstance(tombstone, dict):
                findings.append(_error(
                    "structure", f"goal {goal_id} tombstone must be an object"))
                continue
            for field in ("reason", "amended_at"):
                value = tombstone.get(field)
                if not isinstance(value, str) or not value.strip():
                    findings.append(_error(
                        "structure",
                        f"goal {goal_id} tombstone must have a non-empty {field}"))
            continue  # tombstoned goals are exempt from the freeze contract

        signal = goal.get("signal") or ""
        if signal not in SIGNAL_CLASSES:
            findings.append(require(
                "signal",
                f"goal {goal_id} signal {signal!r} must be one of "
                f"{', '.join(SIGNAL_CLASSES)}"))
        evidence = goal.get("evidence") or ""
        if not str(evidence).strip():
            findings.append(require(
                "evidence",
                f"goal {goal_id} must name its evidence source (evidence:)"))

    if out_of_order:
        findings.append(_warning(
            "sequence",
            f"goal heading(s) out of ascending order: {', '.join(out_of_order)}"))

    return findings


def validate_amendment(old_goals: list[dict], new_goals: list[dict]) -> list[Finding]:
    """Amendment rules on top of strict single-doc validity (goals.mjs
    semantics): every old id survives (removal must become a tombstone,
    never a deletion), and new ids never reuse the numbering — a brand-new
    goal's number must be strictly greater than the old maximum.
    """
    findings = validate_goals(new_goals, strict=True)

    old_ids = {g.get("id") for g in old_goals if isinstance(g, dict)}
    new_ids = {g.get("id") for g in new_goals if isinstance(g, dict)}
    for goal_id in sorted(old_ids - new_ids):
        findings.append(_error(
            "amendment",
            f"goal {goal_id} was removed — removal must become a tombstone, "
            "not a deletion"))

    old_max = max(
        (int(i[1:]) for i in old_ids if isinstance(i, str) and _GOAL_ID_RE.match(i)),
        default=0,
    )
    for goal_id in sorted(new_ids - old_ids):
        if isinstance(goal_id, str) and _GOAL_ID_RE.match(goal_id) and int(goal_id[1:]) <= old_max:
            findings.append(_error(
                "amendment",
                f"goal {goal_id} is new but its number is not greater than the "
                f"old maximum (G{old_max}) — ids must never be renumbered"))

    return findings


def _extract(goal: dict) -> dict:
    tombstone = goal.get("tombstone")
    return {
        "text": goal.get("text", ""),
        "signal": goal.get("signal") or "",
        "evidence": goal.get("evidence") or "",
        "tombstone": {
            "reason": tombstone.get("reason", ""),
            "amended_at": tombstone.get("amended_at", ""),
        } if isinstance(tombstone, dict) else None,
    }


def amendment_diff(old_goals: list[dict], new_goals: list[dict]) -> list[dict]:
    """Change records for the goal_amended event: one
    ``{id, change, old, new}`` per changed goal, in new-document order;
    unchanged goals are omitted. ``change`` is ``added`` (old is None),
    ``tombstoned`` (newly tombstoned), ``untombstoned`` (a resurrected
    goal — legal, but it must leave an audit record), or ``modified``
    (text/signal/evidence/tombstone-detail changed). The extracts carry
    the tombstone alongside text/signal/evidence so every change the
    canonical hash observes also appears here — a hash-moving edit with an
    empty diff would be an unauditable amendment.
    """
    old_map = {g.get("id"): g for g in old_goals if isinstance(g, dict)}
    changes: list[dict] = []
    for new_goal in new_goals:
        goal_id = new_goal.get("id")
        old_goal = old_map.get(goal_id)
        if old_goal is None:
            change = "added"
        elif not old_goal.get("tombstone") and new_goal.get("tombstone"):
            change = "tombstoned"
        elif old_goal.get("tombstone") and not new_goal.get("tombstone"):
            change = "untombstoned"
        elif _extract(old_goal) != _extract(new_goal):
            change = "modified"
        else:
            continue
        changes.append({
            "id": goal_id, "change": change,
            "old": None if old_goal is None else _extract(old_goal),
            "new": _extract(new_goal),
        })
    return changes


def canonical_goals(parsed) -> list[dict]:
    """The canonical goal shapes the hash is keyed over (and the shape the
    goals_frozen/goal_amended events carry): id, trimmed text, signal,
    evidence, tombstone-or-None — sorted by id string, goals.mjs-style."""
    canon = []
    for goal in _goals_list(parsed) or []:
        if not isinstance(goal, dict):
            continue
        tombstone = goal.get("tombstone")
        canon.append({
            "id": goal.get("id"),
            "text": (goal.get("text") or "").strip(),
            "signal": goal.get("signal") or "",
            "evidence": goal.get("evidence") or "",
            "tombstone": {
                "reason": tombstone.get("reason", ""),
                "amended_at": tombstone.get("amended_at", ""),
            } if isinstance(tombstone, dict) else None,
        })
    return sorted(canon, key=lambda g: str(g["id"]))


def goals_hash(text_or_parsed) -> str:
    """Canonical identity of a goal set: ``sha256:<hex>`` over the
    parsed+canonicalized shape, so incidental whitespace/formatting never
    changes identity but any real add/tombstone/text/signal/evidence
    change does. Keys goals_frozen/goal_amended events; divergence between
    goals.md and the last recorded hash is what `work goals assess`
    reports as a silent edit. Accepts raw goals.md text or a parsed object.
    """
    if isinstance(text_or_parsed, dict) and isinstance(text_or_parsed.get("goals"), list):
        parsed = text_or_parsed
    else:
        parsed = parse_goals(text_or_parsed if isinstance(text_or_parsed, str) else "")
    canonical = json.dumps(
        {
            "topic_seed": (parsed.get("topic_seed") or "").strip(),
            "goals": canonical_goals(parsed),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def latest_goals_event(events: list[dict]) -> dict | None:
    """Data of the last goals_frozen/goal_amended event — the last agreed
    goal set. Returns ``{"goals": [...], "goals_hash": "..."}`` normalized
    (goal_amended carries the new set as `goals`/`new_goals_hash`), or
    None when the run never froze. Tolerates malformed events.
    """
    latest = None
    for event in events or []:
        if not isinstance(event, dict):
            continue
        data = event.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("goals"), list):
            continue
        if event.get("type") == GOALS_FROZEN_EVENT and data.get("goals_hash"):
            latest = {"goals": data["goals"], "goals_hash": data["goals_hash"]}
        elif event.get("type") == GOAL_AMENDED_EVENT and data.get("new_goals_hash"):
            latest = {"goals": data["goals"], "goals_hash": data["new_goals_hash"]}
    return latest


def classify_evidence(
    evidence: str,
    receipt_names: set[str] | None = None,
    artifact_names: set[str] | None = None,
) -> str:
    """Classify a verdict's evidence string: ``receipt`` when it names a
    recorded verify/gate receipt, ``artifact`` when it names a registered
    artifact, else ``prose`` (allowed, but marked — the spec's
    "free-prose evidence allowed but marked"). A name counts when it
    appears as a whole word in the evidence text; receipts win ties.
    """
    text = str(evidence or "")
    for kind, names in (("receipt", receipt_names), ("artifact", artifact_names)):
        for name in sorted(names or set(), key=len, reverse=True):
            # Whole-word: no word/dash/dot run continuing on either side —
            # but a trailing dot that ends a sentence ("… t1-tests.") is a
            # boundary, while one that extends the token (spec.md.bak) isn't.
            if name and re.search(
                rf"(?<![\w.-]){re.escape(name)}(?![\w-])(?!\.[\w-])", text
            ):
                return kind
    return "prose"


def parse_verdict_flag(value: str) -> tuple[str, str, str]:
    """Parse one ``--verdict`` flag: ``G<N>=<verdict>:<evidence>``.

    Raises ValueError with a usable message on any malformed part; enum
    and goal-set membership are validated separately (validate_verdicts).
    """
    if "=" not in value:
        raise ValueError(
            f"--verdict {value!r} must look like G1=met:<evidence>")
    goal_id, _, rest = value.partition("=")
    verdict, sep, evidence = rest.partition(":")
    goal_id, verdict, evidence = goal_id.strip(), verdict.strip(), evidence.strip()
    if not goal_id or not verdict or not sep or not evidence:
        raise ValueError(
            f"--verdict {value!r} must look like G1=met:<evidence> "
            f"(verdict one of {', '.join(GOAL_VERDICTS)}; evidence non-empty)")
    return goal_id, verdict, evidence


def validate_verdicts(goals: list[dict], verdicts: dict[str, dict]) -> list[Finding]:
    """A complete assessment covers EVERY active goal — exactly (the
    goals.mjs receipt-validator discipline): missing goals, unknown or
    tombstoned goal ids, out-of-enum verdicts, and empty evidence all
    error.
    """
    findings: list[Finding] = []
    active_ids = [g.get("id") for g in goals if isinstance(g, dict) and not g.get("tombstone")]
    for goal_id in active_ids:
        entry = verdicts.get(goal_id)
        if not isinstance(entry, dict):
            findings.append(_error(
                "verdicts", f"missing verdict for goal {goal_id} "
                f"(every active goal needs one)"))
            continue
        if entry.get("verdict") not in GOAL_VERDICTS:
            findings.append(_error(
                "verdicts",
                f"goal {goal_id} verdict {entry.get('verdict')!r} must be one "
                f"of {', '.join(GOAL_VERDICTS)}"))
        if not str(entry.get("evidence") or "").strip():
            findings.append(_error(
                "verdicts", f"goal {goal_id} evidence must be non-empty"))
    for goal_id in sorted(set(verdicts) - set(active_ids)):
        findings.append(_error(
            "verdicts",
            f"verdict for unknown or tombstoned goal {goal_id}"))
    return findings


def escape_cell(value) -> str:
    """Markdown-table cell escape (retro-goals.mjs semantics): None → em
    dash; ``|`` escaped; whitespace runs collapsed to single spaces."""
    if value is None:
        return "—"
    text = str(value).replace("|", "\\|")
    return re.sub(r"[\n\r\t ]+", " ", text).strip()


def _verdict_rows(goals: list[dict], cells) -> list[str]:
    """Shared table scaffolding: header + one row per active goal (via
    `cells(goal) -> (verdict, evidence)`), then the tombstoned section."""
    active = [g for g in goals if isinstance(g, dict) and not g.get("tombstone")]
    tombstoned = [g for g in goals if isinstance(g, dict) and g.get("tombstone")]
    parts = ["## Goal verdicts", ""]
    if not active and not tombstoned:
        parts.append("_No goals were recorded for this run._")
        return parts
    if active:
        parts.append("| Goal | Statement | Verdict | Evidence |")
        parts.append("| --- | --- | --- | --- |")
        for goal in active:
            verdict, evidence = cells(goal)
            parts.append(
                f"| {escape_cell(goal.get('id'))} | {escape_cell(goal.get('text'))} "
                f"| {escape_cell(verdict)} | {escape_cell(evidence)} |")
    if tombstoned:
        parts.extend(["", "### Tombstoned goals"])
        for goal in tombstoned:
            reason = (goal.get("tombstone") or {}).get("reason")
            reason_text = escape_cell(reason) if reason else "(no reason recorded)"
            parts.append(f"- **{goal.get('id')}** — {reason_text}")
    return parts


def render_verdict_skeleton(goals: list[dict]) -> str:
    """The assessment skeleton `work goals assess` prints for the session
    to complete: one row per active goal with the verdict enum as a
    placeholder, tombstoned goals listed below. Deterministic; no clock."""
    placeholder = " / ".join(GOAL_VERDICTS)
    return "\n".join(_verdict_rows(goals, lambda g: (placeholder, None)))


def render_verdict_table(goals: list[dict], verdicts: dict[str, dict]) -> str:
    """The completed per-goal verdict table for retro.md. ``verdicts`` maps
    goal id → ``{verdict, evidence, evidence_kind}``; prose evidence is
    marked ``(prose)`` per the spec. Goals without an entry render em
    dashes (the skeleton case never reaches here via assess --verdict,
    which requires completeness)."""
    def cells(goal):
        entry = verdicts.get(goal.get("id")) or {}
        verdict = entry.get("verdict") if entry.get("verdict") in GOAL_VERDICTS else None
        evidence = entry.get("evidence")
        if evidence and entry.get("evidence_kind") == "prose":
            evidence = f"{evidence} (prose)"
        return verdict, evidence

    return "\n".join(_verdict_rows(goals, cells))
