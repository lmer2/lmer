"""Durable run-state kernel (Layer 1).

One run = one directory under {work}/{host}/{project}/runs/ holding
state.yaml (authoritative run state), events.jsonl (append-only session and
audit log), log.yaml, reports/, and any registered artifacts — the single
home for everything the run produces. Single-writer discipline: state.yaml
is only ever written by write_state() (atomic tmp+rename); events only via
append_event(). Sessions never edit these files directly — the `work` CLI
is the sole writer.

The CLI also owns the DIRECTORY lifecycle (issue #87): runs are created as
`.new-*` temp dirs and atomically renamed to `runs/<slug>/` once seeded
(seed_run_dir), then renamed exactly once more to `runs/<slug>--<name>/`
at the pre-execution freeze gate when the run is named (freeze_run_dir).
Because dirs can be renamed, a directory name is never a valid address:
every lookup goes through find_run_dir(), which matches on state.yaml
content (slug, then name).

Legacy runs that predate the state-file rename still hold state.yml:
load_state() reads it when state.yaml is absent, and the first
write_state() migrates it aside as state.yml.migrated.

Design: this feature's spec lives in the work repo, in this project's run
directory ({work}/{host}/{project}/runs/develop-durable-run-state/spec.md).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from .utils import _work_repo_base, redact_secrets, sanitize_task_target

SCHEMA_VERSION = 1
STATE_FILE = "state.yaml"
# Pre-rename runs wrote this name; read-fallback + rename-on-write migration.
LEGACY_STATE_FILE = "state.yml"
EVENTS_FILE = "events.jsonl"
# Per-task execution ledger (issue #89): one row per plan task, snapshot of
# current state; every mutation also appends a `task` event to events.jsonl.
LEDGER_FILE = "ledger.yaml"
LEDGER_SCHEMA_VERSION = 1
TASK_STATUSES = ("pending", "in-progress", "done", "deferred", "dropped")
# Run dirs are created under a temporary dot-name and atomically renamed to
# their final name once seeded (issue #87 D2). A crash in between leaves a
# sweepable `.new-*` orphan — the external cleaner contract (RUN-STATE.md §6)
# owns removing those; the resolver never matches dot-dirs.
TMP_DIR_PREFIX = ".new-"
# The archived-runs subtree shares the runs/ namespace; the resolver skips it.
ARCHIVE_DIR = "archive"
# Phases whose (case-insensitive) prefix marks the run as still planning —
# the first `work state set --phase` OUTSIDE this family is the pre-execution
# freeze gate that takes the single name-bearing dir rename (issue #87 D2).
# The list covers the shipped taskdefs' pre-execution phases (develop records
# branch-setup/issue-analysis/interview, review records retrieve, before
# naming is even expected) — a premature freeze on an unnamed run forfeits
# the rename forever, so the family errs wide. An unrecognized phase string
# still counts as execution.
PLANNING_PHASE_PREFIXES = (
    "spec", "plan", "brainstorm", "explor", "design", "review",
    "branch", "issue", "interview", "setup", "retriev",
)
# A bare full SHA-1 task target; slugs use its 12-char short form (D4).
_FULL_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
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
    """Deterministic run slug: same taskdef + target => same slug (spec §4.5).

    A bare full 40-hex commit SHA is truncated to its 12-char short form —
    a slug-only concern (readable dir names, issue #87 D4); the legacy
    ``{task_type}/{target}/`` path builders keep the full form so read-side
    fallback still matches pre-unification dirs on disk. Legacy full-SHA
    runs keep their recorded slug: the resolver matches recorded
    ``state.slug`` exactly, with no aliasing between the two forms.
    """
    taskdef = taskdef or os.environ.get("LMER_TASK", "default")
    if target is None:
        target = os.environ.get("LMER_TASK_TARGET", "")
    if target and _FULL_SHA_RE.fullmatch(target):
        return f"{taskdef}-{target[:12].lower()}"
    safe = sanitize_task_target(target) if target else "default"
    if safe == "default":
        return taskdef
    return f"{taskdef}-{safe}"


def runs_base() -> Optional[Path]:
    base = _work_repo_base()
    if base is None:
        return None
    return base / "runs"


def _read_sibling_state(rdir: Path) -> Optional[dict]:
    """Best-effort state read for resolver scans: state.yaml (legacy state.yml
    fallback), None for corrupt/unreadable/absent — a broken sibling must
    never break resolution of the run actually being looked up."""
    for name in (STATE_FILE, LEGACY_STATE_FILE):
        path = rdir / name
        if not path.exists():
            continue
        try:
            state = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return state if isinstance(state, dict) else None
    return None


def find_run_dir(
    identifier: str,
    base: Optional[Path] = None,
    match_names: bool = False,
) -> Optional[Path]:
    """Resolve a run dir by CONTENT, never by directory name (issue #87 D1).

    Scans runs/*/state.yaml for `state.slug == identifier`; with
    `match_names`, falls back to `state.name == identifier` (a slug match
    always wins — names are unique per project). Session-level resolution
    keeps `match_names` off so a run *name* can never hijack another
    session's derived slug; explicit lookups (e.g. `work seed`'s duplicate
    check) opt in. Dot-dirs (`.new-*` orphans), the archive/ subtree, and
    corrupt siblings are skipped, and any fs error during the scan resolves
    to "no match" — a broken runs/ tree must never raise out of a resolver
    that hook-facing always-exit-0 commands sit on. Returns None when
    nothing matches — dirs can be renamed, so `dir name == slug` is not a
    valid address.
    """
    if base is None:
        base = runs_base()
    if base is None:
        return None
    name_match: Optional[Path] = None
    try:
        children = sorted(base.iterdir())
    except OSError:
        return None
    for child in children:
        try:
            if not child.is_dir() or child.name.startswith(".") or child.name == ARCHIVE_DIR:
                continue
            state = _read_sibling_state(child)
        except OSError:
            continue
        if state is None:
            continue
        if state.get("slug") == identifier:
            return child
        if match_names and name_match is None and state.get("name") == identifier:
            name_match = child
    return name_match


def _unreadable_state_candidate(base: Path, slug: str) -> Optional[Path]:
    """A conventionally-named dir for `slug` whose state file exists but
    cannot be read. The content resolver can't match it, but preferring it
    over a fresh creation address lets load_state()'s backup-and-recover
    path run instead of shadow-seeding a duplicate run beside the stranded
    one. A dir whose state is readable but records a different slug is NOT
    a candidate — that is a foreign run, not a broken one."""
    try:
        candidates = [base / slug] + sorted(base.glob(f"{slug}--*"))
        for cand in candidates:
            if not cand.is_dir():
                continue
            has_state_file = any(
                (cand / name).exists() for name in (STATE_FILE, LEGACY_STATE_FILE)
            )
            if has_state_file and _read_sibling_state(cand) is None:
                return cand
    except OSError:
        return None
    return None


def run_dir(slug: Optional[str] = None) -> Optional[Path]:
    """The current run's directory: resolved by state content when the run
    exists (rename-proof), else the canonical `runs/<slug>` creation address."""
    base = runs_base()
    if base is None:
        return None
    slug = slug or derive_slug()
    found = find_run_dir(slug, base)
    if found is not None:
        return found
    corrupt = _unreadable_state_candidate(base, slug)
    if corrupt is not None:
        return corrupt
    return base / slug


def run_rel_path(slug: Optional[str] = None) -> Optional[str]:
    """Run dir relative to the work-repo root, for git_ops.commit_work_path()."""
    rels = run_rel_path_candidates(slug)
    return rels[0] if rels else None


def run_rel_path_candidates(slug: Optional[str] = None) -> list[str]:
    """Work-repo-relative paths worth staging for the run: the resolved dir
    first, then the bare-slug dir when it differs. Staging both lets a
    commit after a dir rename pick up the old path's deletions even when
    the rename-time push failed (commit_work_path skips clean paths)."""
    host = os.environ.get("LMER_REPO_HOST")
    project = os.environ.get("LMER_REPO_PROJECT")
    if not host or not project:
        return []
    slug = slug or derive_slug()
    rels = []
    found = find_run_dir(slug)
    if found is not None:
        rels.append(f"{host}/{project}/runs/{found.name}")
    bare = f"{host}/{project}/runs/{slug}"
    if bare not in rels:
        rels.append(bare)
    return rels


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
        "frozen": None,
        "created": now,
        "updated": now,
    }


def seed_run_dir(
    slug: str,
    taskdef: str,
    target: str,
    note: Optional[str] = None,
    adopt_existing: bool = True,
) -> tuple[Path, dict]:
    """Create a run dir via the tmp-dir-then-rename lifecycle (issue #87 D2).

    The seed lands in a `runs/.new-<session>-*` temp dir through the normal
    writers, then a single atomic rename() moves it to `runs/<slug>/` — no
    observer ever sees a half-seeded dir at a canonical name, and a crash in
    between leaves only a sweepable `.new-*` orphan. Losing the rename race
    (the final dir appeared concurrently with content) adopts the existing
    run and discards the temp seed — unless `adopt_existing` is False, in
    which case it raises so callers that must not touch a run they didn't
    create (`work seed`) can refuse instead. (An *empty* dir appearing in
    the race window is silently replaced — POSIX rename semantics — which
    is harmless: an empty dir holds no run.) Writes only — pushing is the
    caller's business.
    """
    base = runs_base()
    if base is None:
        raise RunStateError("no run context (LMER_REPO_HOST/LMER_REPO_PROJECT unset)")
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(
        tempfile.mkdtemp(prefix=f"{TMP_DIR_PREFIX}{current_session_id()}-", dir=base)
    )
    os.chmod(tmp, 0o755)  # mkdtemp defaults to 0700; match mkdir-created runs
    state = seed_state(slug, taskdef, target)
    write_state(tmp, state)
    append_event(tmp, "run_seeded", note=note)
    final = base / slug
    try:
        tmp.rename(final)
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        if not final.exists():
            # Not a collision — EACCES/ENOSPC/EXDEV/...: report the real
            # error, or "already exists" would send debugging the wrong way.
            raise RunStateError(f"cannot seed run at {final}: {exc}")
        if not adopt_existing:
            raise RunStateError(f"run '{slug}' already exists at {final}")
        existing = load_state(final)
        if existing is None:
            raise RunStateError(
                f"cannot seed run at {final}: rename failed ({exc}) "
                f"and no readable state there"
            )
        return final, existing
    return final, state


def ensure_run(
    slug: Optional[str] = None,
    taskdef: Optional[str] = None,
    target: Optional[str] = None,
) -> tuple[Path, dict]:
    """Load the current run's state, creating the run when needed.

    Fresh runs are created through seed_run_dir (tmp-then-rename); a dir
    that already exists but lacks readable state is reseeded IN PLACE — it
    already holds its final (possibly name-bearing) name and other files.
    That covers both a stateless legacy dir (load_state returns None) and
    a parse-corrupt state file (load_state backs it up and raises):
    recovering at the already-resolved rdir within this call matters for
    renamed dirs, whose backed-up state file would make them unresolvable
    on a retry — a second invocation would seed a duplicate at the bare
    slug and strand the renamed dir. Only load_state's read-only refusal
    (schema newer than this kernel) propagates.
    """
    slug = slug or derive_slug()
    rdir = run_dir(slug)
    if rdir is None:
        raise RunStateError("no run context (LMER_REPO_HOST/LMER_REPO_PROJECT unset)")
    try:
        state = load_state(rdir)
    except RunStateError:
        if _state_path(rdir) is not None:
            # Newer-schema refusal: the file is intact and must not be
            # overwritten by a reseed — surface the refusal unchanged.
            raise
        state = None  # corrupt file was backed up — recover below, in place
    if state is None:
        taskdef = taskdef or os.environ.get("LMER_TASK", "default")
        if target is None:
            target = os.environ.get("LMER_TASK_TARGET", "")
        if rdir.exists():
            state = seed_state(slug, taskdef, target)
            write_state(rdir, state)
            append_event(rdir, "run_seeded")
        else:
            rdir, state = seed_run_dir(slug, taskdef, target)
    return rdir, state


def is_planning_phase(phase: Optional[str]) -> bool:
    """True when the phase string belongs to the planning family (issue #87
    D2): case-insensitive prefix match against PLANNING_PHASE_PREFIXES.
    None/empty counts as planning (nothing recorded yet is not execution)."""
    if not phase:
        return True
    lowered = phase.strip().lower()
    return lowered.startswith(PLANNING_PHASE_PREFIXES)


def freeze_run_dir(rdir: Path, state: dict) -> tuple[Path, Optional[str]]:
    """Pre-execution freeze gate (issue #87 D2): the run's identity is final.

    Stamps `state["frozen"]` (the caller persists it via write_state) and,
    when the run is named and the dir is not already name-bearing, takes the
    SINGLE rename to `runs/<slug>--<name>/`. A run unnamed at the freeze
    keeps `runs/<slug>/` — the frozen stamp is what makes "one rename means
    one" durable, so there is no second chance. Fail-soft: a rename that
    cannot happen (target taken, fs error) is reported and skipped.

    Returns (possibly-renamed rdir, previous dir name when a rename
    happened, else None).
    """
    state["frozen"] = utc_now_iso()
    name = state.get("name")
    slug = state.get("slug") or rdir.name
    if not name:
        return rdir, None
    final_name = f"{slug}--{name}"
    if rdir.name == final_name:
        return rdir, None
    target = rdir.parent / final_name
    if target.exists():
        print(f"⚠️  run-dir rename skipped: {target} already exists")
        return rdir, None
    old_name = rdir.name
    try:
        rdir = rdir.rename(target)
    except OSError as exc:
        print(f"⚠️  run-dir rename failed (continuing at runs/{old_name}): {exc}")
        return rdir, None
    # The rename has happened — the caller MUST get the new path back even
    # if recording the audit event fails, or its next state write would
    # recreate the old dir and fork the run.
    try:
        append_event(rdir, "run_dir_renamed", note=f"runs/{old_name} -> runs/{final_name}")
    except OSError as exc:
        print(f"⚠️  run_dir_renamed event not recorded: {exc}")
    return rdir, old_name


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


def _load_yaml_mapping(path: Path, label: str, supported_version: int) -> dict:
    """Shared parse/validate preamble for the schema'd single-writer YAML
    files (state.yaml, ledger.yaml): parse → mapping check → schema
    int check → newer-schema read-only refusal. Corrupt files are backed up
    first (spec §6); a newer schema leaves the file untouched."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise _backup_bad_state(path, f"unparseable {path.name} ({exc})")
    if not isinstance(data, dict):
        raise _backup_bad_state(path, f"{path.name} is not a mapping")
    schema = data.get("schema", 0)
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise _backup_bad_state(path, f"{path.name} schema field is not an integer ({schema!r})")
    if schema > supported_version:
        raise RunStateError(
            f"{label} schema {schema} is newer than supported "
            f"{supported_version} — read-only refusal"
        )
    return data


def load_state(rdir: Path) -> Optional[dict]:
    """Read state.yaml (falling back to legacy state.yml). None if absent.
    RunStateError if corrupt (backed up first) or if `schema` is newer than
    this kernel supports (file untouched: read-only refusal, spec §6)."""
    path = _state_path(rdir)
    if path is None:
        return None
    return _load_yaml_mapping(path, "state", SCHEMA_VERSION)


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


def load_ledger(rdir: Path) -> Optional[dict]:
    """Read ledger.yaml. None if absent. Same safety contract as state.yaml
    (the shared preamble): corrupt files are backed up first (RunStateError),
    a newer schema is a read-only refusal, and a non-mapping `tasks` counts
    as corrupt."""
    path = rdir / LEDGER_FILE
    if not path.exists():
        return None
    ledger = _load_yaml_mapping(path, "ledger", LEDGER_SCHEMA_VERSION)
    tasks = ledger.get("tasks")
    if tasks is None:
        ledger["tasks"] = {}
    elif not isinstance(tasks, dict):
        raise _backup_bad_state(path, f"{path.name} tasks field is not a mapping")
    return ledger


def write_ledger(rdir: Path, ledger: dict) -> None:
    """Atomic tmp+rename write. The ONLY writer of ledger.yaml (issue #89:
    single-writer contract, same as state.yaml)."""
    rdir.mkdir(parents=True, exist_ok=True)
    tmp = rdir / f".{LEDGER_FILE}.tmp"
    tmp.write_text(yaml.safe_dump(ledger, sort_keys=False), encoding="utf-8")
    tmp.replace(rdir / LEDGER_FILE)


def set_ledger_task(
    rdir: Path,
    task_id: str,
    status: str,
    title: Optional[str] = None,
    commit: Optional[str] = None,
    receipt: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """Upsert one ledger row and append the `task` audit event (issue #89:
    ledger.yaml is the snapshot, events.jsonl the audit trail — every
    mutation writes both). Fields not passed are preserved from the existing
    row, so `set T2 --status done --commit <sha>` keeps an earlier title.
    Returns the updated row."""
    if status not in TASK_STATUSES:
        raise RunStateError(
            f"invalid task status {status!r} (expected one of {', '.join(TASK_STATUSES)})"
        )
    ledger = load_ledger(rdir) or {"schema": LEDGER_SCHEMA_VERSION, "tasks": {}}
    tasks = ledger["tasks"]
    old = tasks.get(task_id)
    row = dict(old) if isinstance(old, dict) else {}
    # Free-text fields land in the (shared) work repo — redact like the
    # other agent-typed writers (log, artifact, verify/gate receipts) do.
    if title is not None:
        row["title"] = redact_secrets(title)
    row["status"] = status
    if commit is not None:
        row["commit"] = commit
    if receipt is not None:
        row["receipt"] = receipt
    if note is not None:
        row["note"] = redact_secrets(note)
    row["updated"] = utc_now_iso()
    tasks[task_id] = row
    write_ledger(rdir, ledger)
    data = {"task": task_id, "status": status}
    for key, value in (("commit", commit), ("receipt", receipt)):
        if value is not None:
            data[key] = value
    append_event(rdir, "task", note=f"{task_id}: {status}", data=data)
    return row


def summarize_ledger(ledger: Optional[dict]) -> Optional[dict]:
    """Compact ledger summary for the resume brief: done/total counts, the
    in-flight task ids, and the most recently recorded commit sha. None when
    there is no ledger (or no tasks) — the brief omits the line entirely.
    Tolerates malformed rows (a broken row must never break resume)."""
    if not isinstance(ledger, dict):
        return None
    tasks = ledger.get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        return None
    counts = {status: 0 for status in TASK_STATUSES}
    in_flight: list[str] = []
    last_commit = None
    last_commit_ts = ""
    for task_id, row in tasks.items():
        if not isinstance(row, dict):
            continue
        status = row.get("status")
        if isinstance(status, str) and status in counts:
            counts[status] += 1
        if status == "in-progress":
            in_flight.append(str(task_id))
        commit = row.get("commit")
        # ISO-8601 Z timestamps order lexicographically; rows without a
        # stamp lose ties to any stamped row.
        ts = str(row.get("updated") or "")
        if commit and ts >= last_commit_ts:
            last_commit = str(commit)
            last_commit_ts = ts
    return {
        "total": len(tasks),
        "done": counts["done"],
        "counts": counts,
        "in_flight": in_flight,
        "last_commit": last_commit,
    }


def format_ledger_line(summary: Optional[dict]) -> Optional[str]:
    """The one-line brief form: `Ledger: 4/7 done, in-flight: T3a, last
    commit 4a1f9c2` — parts absent when empty. None when no summary."""
    if not summary:
        return None
    line = f"Ledger: {summary['done']}/{summary['total']} done"
    if summary["in_flight"]:
        line += f", in-flight: {', '.join(summary['in_flight'])}"
    if summary["last_commit"]:
        line += f", last commit {summary['last_commit']}"
    return line


def format_ledger(ledger: Optional[dict]) -> str:
    """Human-readable ledger table for `work ledger` (read-only view; the
    YAML stays authoritative). Rows print in file order — plan order."""
    summary = summarize_ledger(ledger)
    if summary is None:
        return "No ledger"
    lines = [format_ledger_line(summary)]
    tasks = ledger["tasks"]
    id_width = max(len(str(task_id)) for task_id in tasks)
    for task_id, row in tasks.items():
        if not isinstance(row, dict):
            row = {}
        status = str(row.get("status") or "?")
        parts = [f"  {str(task_id):<{id_width}}  {status:<11}"]
        if row.get("commit"):
            parts.append(f"commit={row['commit']}")
        if row.get("receipt"):
            parts.append(f"receipt={row['receipt']}")
        if row.get("title"):
            parts.append(str(row["title"]))
        if row.get("note"):
            parts.append(f"— {row['note']}")
        lines.append(" ".join(parts).rstrip())
    return "\n".join(lines)


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
    ledger: Optional[dict] = None,
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
        # Full ledger for --json consumers; the brief renders the one-line
        # summary from it (issue #89).
        "ledger": ledger,
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
    ledger_line = format_ledger_line(summarize_ledger(decision.get("ledger")))
    if ledger_line:
        lines.append(ledger_line)
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


def emit_gate_event(
    gate: str,
    outcome: str,
    exit_code: Optional[int] = None,
    duration_s: Optional[float] = None,
    summary: Optional[str] = None,
    argv: Optional[list] = None,
    commit_sha: Optional[str] = None,
) -> None:
    """Record a gate command outcome ('pass' | 'fail' | 'bypass') on the
    current run, as a machine-written receipt (issue #88): the `data`
    payload proves the gate actually ran and is stamped by the tool
    process, never typed by the model. `gate` and `outcome` are always
    present; the remaining fields land only when the caller measured them
    (`summary` is best-effort and simply absent when unparseable — never
    fabricated). Guarded so gate behavior is byte-identical when no run
    exists, and no failure here can ever change a gate's exit code."""
    try:
        rdir = run_dir()
        if rdir is None or _state_path(rdir) is None:
            return
        data: dict = {"gate": gate, "outcome": outcome}
        if exit_code is not None:
            data["exit_code"] = exit_code
        if duration_s is not None:
            data["duration_s"] = round(duration_s, 1)
        if summary is not None:
            # Receipt text lands in the (shared) work repo — redact like
            # the other writers do (gate-commit's argv carries the commit
            # message; a summary line could echo anything).
            data["summary"] = redact_secrets(summary)
        if argv is not None:
            data["argv"] = [redact_secrets(str(arg)) for arg in argv]
        if commit_sha is not None:
            data["commit_sha"] = commit_sha
        append_event(rdir, "gate", note=f"{gate}: {outcome}", data=data)
    except Exception:
        pass
