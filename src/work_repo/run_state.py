"""Durable run-state kernel (Layer 1).

One run = one directory at {work}/{host}/{project}/runs/<slug>/ holding
state.yaml (authoritative run state) and events.jsonl (append-only session
and audit log). Single-writer discipline: state.yaml is only ever written by
write_state() (atomic tmp+rename); events only via append_event(). Sessions
never edit these files directly — the `work` CLI is the sole writer.
Legacy runs that predate the rename still hold state.yml: load_state()
reads it when state.yaml is absent, and the first write_state() migrates
it aside as state.yml.migrated.

Design: this feature's spec lives in the work repo, in this project's run
directory ({work}/{host}/{project}/runs/develop-durable-run-state/spec.md).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from .utils import _work_repo_base, sanitize_task_target

SCHEMA_VERSION = 1
STATE_FILE = "state.yaml"
# Pre-rename runs wrote this name; read-fallback + rename-on-write migration.
LEGACY_STATE_FILE = "state.yml"
EVENTS_FILE = "events.jsonl"
# Masterplan bundles nest at <run>/masterplan/<mp-slug>/; these well-known
# artifacts get relative symlinks at the run-dir root when present (spec §6).
MASTERPLAN_DIR = "masterplan"
MASTERPLAN_ARTIFACTS = ("spec.md", "goals.md", "plan.md", "plan.html", "retro.md")
STOP_REASONS = ("question", "yield", "complete", "critical_error")
STATUSES = ("in-progress", "complete", "archived")
# A foreign owner claim younger than this is treated as a live concurrent
# session (warn loudly); older claims are reported as stale (likely a crash).
STALE_CLAIM_MINUTES = 120


class RunStateError(Exception):
    """Raised when run state cannot be read or written safely."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def current_session_id() -> str:
    return os.environ.get("LMER_SESSION_ID", "unknown")


def derive_slug(taskdef: Optional[str] = None, target: Optional[str] = None) -> str:
    """Deterministic run slug: same taskdef + target => same slug (spec §4.5)."""
    taskdef = taskdef or os.environ.get("LMER_TASK", "default")
    if target is None:
        target = os.environ.get("LMER_TASK_TARGET", "")
    safe = sanitize_task_target(target) if target else "default"
    if safe == "default":
        return taskdef
    return f"{taskdef}-{safe}"


def runs_base() -> Optional[Path]:
    base = _work_repo_base()
    if base is None:
        return None
    return base / "runs"


def run_dir(slug: Optional[str] = None) -> Optional[Path]:
    base = runs_base()
    if base is None:
        return None
    return base / (slug or derive_slug())


def run_rel_path(slug: Optional[str] = None) -> Optional[str]:
    """Run dir relative to the work-repo root, for git_ops.commit_work_path()."""
    host = os.environ.get("LMER_REPO_HOST")
    project = os.environ.get("LMER_REPO_PROJECT")
    if not host or not project:
        return None
    return f"{host}/{project}/runs/{slug or derive_slug()}"


def seed_state(slug: str, taskdef: str, target: str) -> dict:
    """Fresh state for a new run (spec §4.1). taskdef/target are immutable."""
    now = utc_now_iso()
    return {
        "schema": SCHEMA_VERSION,
        "slug": slug,
        "name": None,
        "taskdef": taskdef,
        "target": target,
        "status": "in-progress",
        "phase": None,
        "stop_reason": None,
        "critical_error": None,
        "goal": None,
        "artifacts": {},
        "owner": None,
        "created": now,
        "updated": now,
    }


def _backup_bad_state(path: Path, reason: str) -> "RunStateError":
    """Move an unusable state file aside and return the error to raise.

    Never overwrite what we can't parse (spec §6): the bad bytes are
    preserved as <name>.bad-<utc-compact> for post-mortem, named after
    whichever file was actually read (state.yaml or legacy state.yml).
    """
    stamp = utc_now_iso().replace(":", "").replace("-", "")
    backup = path.with_name(f"{path.name}.bad-{stamp}")
    path.rename(backup)
    return RunStateError(f"{reason} — backed up to {backup}")


def _state_path(rdir: Path) -> Optional[Path]:
    """The state file to read: state.yaml, else legacy state.yml (migrated
    on the next write_state()), else None."""
    for name in (STATE_FILE, LEGACY_STATE_FILE):
        path = rdir / name
        if path.exists():
            return path
    return None


def load_state(rdir: Path) -> Optional[dict]:
    """Read state.yaml (falling back to legacy state.yml). None if absent.
    RunStateError if corrupt (backed up first) or if `schema` is newer than
    this kernel supports (file untouched: read-only refusal, spec §6)."""
    path = _state_path(rdir)
    if path is None:
        return None
    try:
        state = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise _backup_bad_state(path, f"unparseable {path.name} ({exc})")
    if not isinstance(state, dict):
        raise _backup_bad_state(path, f"{path.name} is not a mapping")
    schema = state.get("schema", 0)
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise _backup_bad_state(path, f"{path.name} schema field is not an integer ({schema!r})")
    if schema > SCHEMA_VERSION:
        raise RunStateError(
            f"state schema {schema} is newer than supported "
            f"{SCHEMA_VERSION} — read-only refusal"
        )
    return state


def write_state(rdir: Path, state: dict) -> None:
    """Atomic tmp+rename write. The ONLY writer of state.yaml (spec §3.4)."""
    rdir.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    state["updated"] = utc_now_iso()
    tmp = rdir / f".{STATE_FILE}.tmp"
    tmp.write_text(yaml.safe_dump(state, sort_keys=False), encoding="utf-8")
    tmp.replace(rdir / STATE_FILE)
    # Lazy migration: state.yaml now exists (and wins on read), so a leftover
    # legacy file is stale — rename it so it can never be read again. Ordering
    # matters: only after the new file landed, so a crash in between still
    # leaves exactly one readable, current state file.
    legacy = rdir / LEGACY_STATE_FILE
    if legacy.exists():
        legacy.rename(rdir / f"{LEGACY_STATE_FILE}.migrated")


def append_event(
    rdir: Path,
    event_type: str,
    note: Optional[str] = None,
    data: Optional[dict] = None,
) -> None:
    """Append one JSON line to events.jsonl (append-only, spec §4.2)."""
    rdir.mkdir(parents=True, exist_ok=True)
    event = {"ts": utc_now_iso(), "session": current_session_id(), "type": event_type}
    if note is not None:
        event["note"] = note
    if data is not None:
        event["data"] = data
    with open(rdir / EVENTS_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_events(rdir: Path, last_n: int = 5) -> list[dict]:
    """Read events, tolerating torn/corrupt lines (a crash mid-append must
    not break resume). last_n=0 returns all."""
    path = rdir / EVENTS_FILE
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events[-last_n:] if last_n else events


def _iso_to_minutes_apart(earlier: str, later: str) -> Optional[float]:
    """Minutes between two ISO-8601 Z timestamps; None if unparseable."""
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        delta = datetime.strptime(later, fmt) - datetime.strptime(earlier, fmt)
        return delta.total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


def decide(
    state: Optional[dict],
    events: list[dict],
    session: str,
    now: Optional[str] = None,
) -> dict:
    """Pure resume decision (spec §4.3 `work resume`). No fs, no env reads —
    fully unit-testable; all inputs are passed in by the caller."""
    if state is None:
        return {"kind": "none"}
    warnings: list[str] = []
    owner = state.get("owner")
    if isinstance(owner, dict) and owner.get("session_id") not in (None, session):
        age = _iso_to_minutes_apart(owner.get("claimed_at", ""), now or utc_now_iso())
        if age is not None and age < STALE_CLAIM_MINUTES:
            warnings.append(
                f"run is claimed by another live session "
                f"({owner.get('session_id')}, {int(age)} min ago) — coordinate before writing"
            )
        else:
            warnings.append(
                f"stale owner claim from session {owner.get('session_id')} — "
                f"the previous session likely did not close cleanly"
            )
    return {
        "kind": "run",
        "slug": state.get("slug"),
        "name": state.get("name"),
        "status": state.get("status"),
        "phase": state.get("phase"),
        "stop_reason": state.get("stop_reason"),
        "critical_error": state.get("critical_error"),
        "goal": state.get("goal"),
        "artifacts": state.get("artifacts") or {},
        "recent_events": events,
        "warnings": warnings,
    }


def format_brief(decision: dict) -> str:
    """Human-readable resume brief for prompt injection."""
    if decision.get("kind") != "run":
        return "No run state found — this is a fresh run."
    if decision.get("name"):
        header = (
            f"Run: {decision['name']} "
            f"(slug: {decision['slug']}, status: {decision['status']})"
        )
    else:
        header = f"Run: {decision['slug']} (status: {decision['status']})"
    lines = [
        header,
        f"Phase: {decision['phase'] or '—'}   Stop reason: {decision['stop_reason'] or '—'}",
    ]
    if decision.get("critical_error"):
        crit = decision["critical_error"]
        summary = crit.get("summary", "?") if isinstance(crit, dict) else str(crit)
        lines.append(f"CRITICAL ERROR: {summary}")
    if decision.get("goal"):
        lines.append(f"Goal: {decision['goal']}")
    if decision.get("artifacts"):
        arts = ", ".join(sorted(decision["artifacts"].values()))
        lines.append(f"Artifacts in run dir: {arts}")
    for warning in decision.get("warnings", []):
        lines.append(f"⚠️  {warning}")
    events = decision.get("recent_events") or []
    if events:
        lines.append("Recent events:")
        for event in events:
            note = f" — {event['note']}" if event.get("note") else ""
            lines.append(f"  {event.get('ts', '?')} [{event.get('type', '?')}]{note}")
    return "\n".join(lines)


def sync_masterplan_artifacts(rdir: Path) -> list[str]:
    """Surface masterplan bundle artifacts at the run-dir root (spec §6).

    Discovers masterplan/<mp-slug>/ bundle dirs under `rdir` and creates
    RELATIVE symlinks at the run-dir root for each well-known artifact
    (MASTERPLAN_ARTIFACTS) that is present — relative because both ends live
    in the same work-repo checkout, so the links survive git round-trips.

    Collision policy: a single bundle links under the plain artifact names;
    multiple bundles prefix each link `<mp-slug>-<name>`. A pre-existing
    regular file at a link name is replaced (the bundle copy is canonical);
    an already-correct symlink is left alone, so the sync is idempotent.

    Every linked name is registered in state.artifacts through the single
    writer (load_state → write_state); the state file is only rewritten when
    the registration actually changes. Fail-soft: any error is reported and
    skipped — this never raises, and a run without a masterplan/ dir is
    untouched (no state write).

    Returns the list of link names now present at the run-dir root.
    """
    linked: list[str] = []
    try:
        mp_dir = rdir / MASTERPLAN_DIR
        if not mp_dir.is_dir():
            return []
        bundles = sorted(
            (p for p in mp_dir.iterdir() if p.is_dir()), key=lambda p: p.name
        )
        prefixed = len(bundles) > 1
        for bundle in bundles:
            for name in MASTERPLAN_ARTIFACTS:
                if not (bundle / name).is_file():
                    continue
                link_name = f"{bundle.name}-{name}" if prefixed else name
                target = os.path.join(MASTERPLAN_DIR, bundle.name, name)
                link_path = rdir / link_name
                try:
                    if link_path.is_symlink():
                        if os.readlink(link_path) == target:
                            linked.append(link_name)  # already correct
                            continue
                        link_path.unlink()
                    elif link_path.exists():
                        # Regular file (e.g. a manually copied spec.md):
                        # the bundle copy is canonical — replace it.
                        link_path.unlink()
                    link_path.symlink_to(target)
                    linked.append(link_name)
                except OSError as exc:
                    print(f"⚠️  masterplan artifact sync: skipped {link_name}: {exc}")
        if linked:
            try:
                state = load_state(rdir)
                if state is not None:
                    artifacts = dict(state.get("artifacts") or {})
                    changed = False
                    assigned: dict[str, str] = {}
                    for link_name in linked:
                        # Key by filename stem, matching `work artifact`'s
                        # registry convention (spec: spec.md) — unless the
                        # stem is already claimed by a different link this
                        # sync (plan.md vs plan.html both stem to `plan`),
                        # in which case the full name keeps both registered
                        # and the keys stable across syncs.
                        key = Path(link_name).stem
                        if assigned.get(key, link_name) != link_name:
                            key = link_name
                        assigned[key] = link_name
                        if artifacts.get(key) != link_name:
                            artifacts[key] = link_name
                            changed = True
                    if changed:
                        state["artifacts"] = artifacts
                        write_state(rdir, state)
            except Exception as exc:
                print(f"⚠️  masterplan artifact sync: could not register in state: {exc}")
    except Exception as exc:
        print(f"⚠️  masterplan artifact sync skipped: {exc}")
    return linked


def emit_gate_event(gate: str, outcome: str) -> None:
    """Record a gate command outcome ('pass' | 'fail' | 'bypass') on the
    current run. Guarded so gate behavior is byte-identical when no run
    exists, and no failure here can ever change a gate's exit code."""
    try:
        rdir = run_dir()
        if rdir is None or _state_path(rdir) is None:
            return
        append_event(rdir, "gate", note=f"{gate}: {outcome}")
    except Exception:
        pass
