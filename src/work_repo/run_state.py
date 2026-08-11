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
A release run takes one further, deliberate move when it records its
version (reslug_run: `release-<repo>` → `release-<repo>-v0.6.0`), which is
what frees the bare address for the NEXT release. Because dirs can be
renamed, a directory name is never a valid address: every lookup goes
through find_run_dir(), which matches on state.yaml content (slug, then
name), with find_successor_run_dir() covering the re-slug case.

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
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from . import specs_index
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
# `aborted` is the release-run terminal (abort_run): it rides the
# descriptive stop_reason axis, NEVER a fourth STATUSES value — see the
# design comment on abort_run for why the status enum stays closed.
STOP_REASONS = ("question", "yield", "complete", "critical_error", "aborted")
STATUSES = ("in-progress", "complete", "archived")
# A foreign owner claim younger than this is treated as a live concurrent
# session (warn loudly); older claims are reported as stale (likely a crash).
STALE_CLAIM_MINUTES = 120
# The single-flight release claim's staleness threshold (RUN-STATE.md §7) —
# deliberately its OWN constant: unlike STALE_CLAIM_MINUTES (advisory,
# aimed at humans coordinating), this one is ENFORCED — a live foreign
# claim is a hard refusal, and a claim past this age is taken over
# automatically (and loudly) by the next claimant, normally the unattended
# scheduled relaunch. Default matches STALE_CLAIM_MINUTES; the watch loop's
# refresh cadence must sit well inside it.
RELEASE_CLAIM_STALE_MINUTES = 120
# claim_status() verdicts on the release claim block.
CLAIM_UNCLAIMED = "unclaimed"
CLAIM_OURS = "ours"
CLAIM_FOREIGN_LIVE = "foreign-live"
CLAIM_FOREIGN_STALE = "foreign-stale"
# The one-line seed excerpt in the completed-run direction contract (#96).
SEED_EXCERPT_MAX_LEN = 120


class RunStateError(Exception):
    """Raised when run state cannot be read or written safely."""


class ClaimRefusedError(RunStateError):
    """A foreign claim holds the run — hard refusal, never a warning
    (RUN-STATE.md §7). Carries the loser's pointer: everything needed to
    find the active release without archaeology (slug, run dir, holder
    session, claimed_at/age, status/phase). The CLI adds the run dir's web
    URL via git_ops.web_url_for — this kernel stays git-unaware."""

    def __init__(self, message: str, pointer: dict):
        super().__init__(message)
        self.pointer = pointer


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
    ``state.slug`` exactly, with no aliasing between the two forms. The
    re-slug successor fallback does not weaken that — it matches addresses a
    run RECORDS having vacated (``reslugged_from``), which a legacy run never
    has, rather than re-deriving identity through this function.
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


def slug_available(
    slug: str, base: Optional[Path] = None, exclude: Optional[Path] = None
) -> bool:
    """True when `slug` is free for a run to move to — no sibling RECORDS it
    and no sibling dir ADDRESSES it.

    `exclude` is the run doing the asking; it never counts as occupying the
    address it is trying to take. That matters for the crash window a
    re-slug can be interrupted in — dir renamed, `state.slug` not yet
    written — where the run is already SITTING at the address it wants and
    the next record verb has to be able to finish the move rather than
    conclude the address is taken and drift to a fresh one.

    Two distinct resources have to be free, and checking only one of them is
    the iteration-6 defect. A run's identity is its recorded `state.slug`
    (`find_run_dir` matches on that alone), while the paths that address it
    are `runs/<slug>` and `runs/<slug>--<name>`. They come apart in both
    directions:

    - an UNNAMED terminal run occupies `runs/<slug>`, so the path is taken
      while nothing would stop a second run recording the slug;
    - a NAMED run lives at `runs/<slug>--<name>`, so the SLUG is taken while
      `runs/<slug>` is free — and two runs recording one slug make every
      lookup a coin flip decided by directory sort order.

    Callers that need an address they can definitely take should compute one
    that satisfies this (release_run.unique_release_slug) rather than give up
    when it is False: an address that cannot be freed is what wedges a
    repository.
    """
    if base is None:
        base = runs_base()
    if base is None:
        return False
    recorded = find_run_dir(slug, base)
    if recorded is not None and recorded != exclude:
        return False
    try:
        bare = base / slug
        if bare.exists() and bare != exclude:
            return False
        return not any(d for d in base.glob(f"{slug}--*") if d != exclude)
    except OSError:
        return False


def find_successor_run_dir(identifier: str, base: Optional[Path] = None) -> Optional[Path]:
    """The LIVE run carrying this slug's identity after a re-slug.

    A release run re-slugs itself the moment leg 1 records the version
    (`release-<repo>` → `release-<repo>-v0.6.0`, reslug_run), so the address
    a session derives stops matching any recorded slug.

    The match is on `reslugged_from` — the addresses a run has actually
    VACATED, recorded by reslug_run itself. Nothing else knows that fact, so
    nothing else can be mistaken for it. Deriving the identity instead
    (`derive_slug(taskdef, target) == identifier`) looks equivalent and is
    not: derive_slug truncates a 40-hex target to 12 chars, so a legacy
    full-SHA run would answer to the truncated slug and a `develop` session
    would adopt a pre-#87 run it had never touched — the aliasing
    derive_slug's own contract rules out. Runs that never re-slugged carry
    no `reslugged_from` and are excluded by construction.

    Only IN-PROGRESS runs qualify, and that is the whole point of the
    version-bearing slug: a terminal run has given its address up, so the
    next release seeds fresh at the bare address instead of resolving a
    finished run and being refused forever. (It is also what keeps a
    declined release and its successor apart — both vacated the same seed
    address, but only one of them is live.)

    Ties are not expected (single-flight allows one live release per repo)
    but resolve deterministically: newest `created`, then dir name.
    """
    if base is None:
        base = runs_base()
    if base is None:
        return None
    matches: list[tuple[str, str, Path]] = []
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
        if not isinstance(state, dict) or state.get("status") != "in-progress":
            continue
        vacated = state.get("reslugged_from")
        if not isinstance(vacated, list) or identifier not in vacated:
            continue
        matches.append((str(state.get("created") or ""), child.name, child))
    if not matches:
        return None
    return max(matches)[2]


def resolve_run_dir(slug: str, base: Optional[Path] = None) -> Optional[Path]:
    """The run dir addressed by `slug`: the run recording it, else the live
    run that re-slugged away from it. None when neither exists."""
    found = find_run_dir(slug, base)
    if found is not None:
        return found
    return find_successor_run_dir(slug, base)


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
    found = resolve_run_dir(slug, base)
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


def run_rel_for_dir_name(dir_name: str) -> Optional[str]:
    """The work-repo-relative path of a run dir by NAME:
    `{host}/{project}/runs/<dir_name>`, or None when the repo identity is
    not in the environment.

    One owner for the shape. Callers that stage a run dir for commit build
    this path, and every copy of it also copies the host/project fallback —
    so it lives here, beside run_rel_path_candidates, which is built on it.
    """
    host = os.environ.get("LMER_REPO_HOST")
    project = os.environ.get("LMER_REPO_PROJECT")
    if not host or not project:
        return None
    return f"{host}/{project}/runs/{dir_name}"


def run_rel_path_candidates(slug: Optional[str] = None) -> list[str]:
    """Work-repo-relative paths worth staging for the run: the resolved dir
    first, then the bare-slug dir when it differs. Staging both lets a
    commit after a dir rename pick up the old path's deletions even when
    the rename-time push failed (commit_work_path skips clean paths) — the
    naming freeze's rename and the release re-slug's alike, since the bare
    slug is exactly the address a re-slugged run vacated."""
    slug = slug or derive_slug()
    rels = []
    found = resolve_run_dir(slug)
    if found is not None:
        resolved = run_rel_for_dir_name(found.name)
        if resolved is None:
            return []
        rels.append(resolved)
    bare = run_rel_for_dir_name(slug)
    if bare is None:
        return []
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
        "open_question": None,
        "goal": None,
        "estimate": None,
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
    # Specs-index entries are relative symlinks into runs/<dirname>/ — the
    # rename would leave them dangling (and the label change from slug to
    # name defeats upsert's stale-cleanup), so re-point them now. Fail-soft
    # both here and inside (index maintenance never fails a freeze).
    try:
        specs_index.repoint_run_dir_entries(
            old_name, final_name, specs_index.run_label(rdir, state)
        )
    except Exception as exc:
        print(f"⚠️  specs index re-point failed (continuing): {exc}")
    return rdir, old_name


def reslug_run(
    rdir: Path, state: dict, new_slug: str
) -> tuple[Path, dict, Optional[str]]:
    """Move a run to a new identity — the release run taking its
    version-bearing slug (`release-<repo>` → `release-<repo>-v0.6.0`).

    Run identity is deterministic per `(taskdef, target)`, so without this
    every release of a repository resolved to ONE run dir: the second
    release found the first one's finished run and `work release claim`
    refused it forever. Recording the version moves the run to an address
    of its own and frees the bare one for the next release.

    ORDER IS LOAD-BEARING: the dir is renamed FIRST, then `state.slug` is
    written. The directory is the resource that must never be
    double-booked — `seed_run_dir` creates at `runs/<slug>`, so a slug that
    moved while the dir did not would let the next release's seed land on
    top of this run and adopt it. Both crash windows are recoverable:

    - crashed after the rename, before the slug write → the run still
      resolves (resolution is by content, never by dir name) and the next
      release-record verb completes the re-slug;
    - the rename could not happen (target taken, fs error) → loud warning,
      slug UNCHANGED, run continues at the bare address. `work release
      claim` heals it on the next release rather than dead-ending.

    A name-bearing dir stays name-bearing (`<new-slug>--<name>`); this is a
    distinct lifecycle event from the freeze gate's one-shot naming rename
    (issue #87 D2), which is untouched.

    Returns (possibly-renamed rdir, state, previous dir name when a rename
    happened, else None) — the caller stages the previous path so the old
    path's deletion lands in the same commit.
    """
    old_slug = state.get("slug")
    if not new_slug or new_slug == old_slug:
        return rdir, state, None
    name = state.get("name")
    target_name = f"{new_slug}--{name}" if name else new_slug
    old_dir_name: Optional[str] = None
    if rdir.name != target_name:
        target = rdir.parent / target_name
        if not slug_available(new_slug, rdir.parent, exclude=rdir):
            # The guard is on the SLUG, not on the path: a named run holds
            # `<new_slug>--<name>` while leaving `runs/<new_slug>` free, and
            # letting the move through there would leave two runs recording
            # one `state.slug` for `find_run_dir` to pick between by sort
            # order. Callers compute an address that is actually available
            # (release_run.unique_release_slug), so this is the net rather
            # than the mechanism — reaching it means the run keeps its
            # current identity, which is safe but not free of consequence.
            #
            # stderr for every warning on this path: it runs inside
            # `work release claim`, whose --json form parses stdout.
            print(f"⚠️  re-slug skipped: '{new_slug}' is already taken "
                  f"(run stays at runs/{rdir.name})", file=sys.stderr)
            return rdir, state, None
        old_dir_name = rdir.name
        try:
            rdir = rdir.rename(target)
        except OSError as exc:
            print(f"⚠️  re-slug rename failed (continuing at "
                  f"runs/{old_dir_name}): {exc}", file=sys.stderr)
            return rdir, state, None
    # The rename has happened — from here the caller MUST get the new path
    # back even if a bookkeeping step fails, or its next write would
    # recreate the old dir and fork the run.
    #
    # The vacated address is RECORDED, not merely left behind: it is what
    # `find_successor_run_dir` matches on, so a session deriving the old
    # address still lands on this run. Append-only and de-duplicated — a run
    # that moves twice must stay reachable from every address it has held,
    # and re-running the tail of an interrupted re-slug must not double it.
    # An optional field: runs that never re-slugged simply do not have it,
    # which is exactly what excludes them from successor matching.
    vacated = state.get("reslugged_from")
    vacated = list(vacated) if isinstance(vacated, list) else []
    if old_slug and old_slug not in vacated:
        vacated.append(old_slug)
    state["reslugged_from"] = vacated
    state["slug"] = new_slug
    write_state(rdir, state)
    try:
        append_event(rdir, "run_reslugged", note=f"{old_slug} -> {new_slug}")
    except OSError as exc:
        print(f"⚠️  run_reslugged event not recorded: {exc}", file=sys.stderr)
    if old_dir_name is not None:
        # Index entries are relative symlinks into runs/<dirname>/ — same
        # re-point the freeze rename does, fail-soft for the same reason.
        try:
            specs_index.repoint_run_dir_entries(
                old_dir_name, target_name, specs_index.run_label(rdir, state)
            )
        except Exception as exc:
            print(f"⚠️  specs index re-point failed (continuing): {exc}",
                  file=sys.stderr)
    return rdir, state, old_dir_name


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
    """Atomic tmp+rename write. The ONLY writer of state.yaml (spec §3.4).

    The temp name carries the writer's identity — process *and* thread — for the
    reason spelled out in :func:`lmer_platform.store.write_json`: two writers
    sharing one temp path is worse than losing a write, because the second's
    truncation lands inside the first's file and the first then publishes the
    hole. Single-writer discipline is a contract on the destination, not a
    guarantee that only one process is executing this function.
    """
    rdir.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    state["updated"] = utc_now_iso()
    tmp = rdir / f".{STATE_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
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
    single-writer contract, same as state.yaml) — including its temp naming,
    which carries pid and thread id for the reason :func:`write_state` gives."""
    rdir.mkdir(parents=True, exist_ok=True)
    tmp = rdir / f".{LEDGER_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
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


def answer_question(rdir: Path, state: dict, answer: str) -> dict:
    """Apply a human's answer to the run's open question (issue #98).

    Appends a `question_answered` event carrying both texts (secret-redacted
    like every other free-text writer), clears `open_question`, and clears
    `stop_reason` — the question stop is resolved. `status` is deliberately
    left untouched: an in-progress run stays in-progress, and a COMPLETED
    run is never flipped back silently — reopening goes through the #96
    completed-run directive. Persists through the single writer
    (write_state) and returns the updated state.

    A question stop that never recorded its text is answerable too (T24):
    `work state set --stop-reason=question` without `--question` is the
    common shape in the wild, and refusing there dropped the answer and left
    the stop in place. The event then carries `question: None` — the stop is
    the fact that matters, and a missing text is not a reason to keep a run
    blocked. `stop_reason` still has to be `question`: without a question
    stop there is nothing an answer resolves.
    """
    question = state.get("open_question")
    if not question and state.get("stop_reason") != "question":
        raise RunStateError("no open question recorded — nothing to answer")
    data = {
        "question": redact_secrets(str(question)) if question else None,
        "answer": redact_secrets(answer),
    }
    state["open_question"] = None
    state["stop_reason"] = None
    write_state(rdir, state)
    append_event(rdir, "question_answered", note=data["answer"], data=data)
    return state


def count_session_starts(events: list[dict]) -> int:
    """The run's sessions-used actual (issue #99): how many `session_start`
    events are in `events` (pass read_events(rdir, last_n=0) for the whole
    run). Pure — the caller owns reading events, like decide()'s inputs."""
    return sum(
        1 for e in events if isinstance(e, dict) and e.get("type") == "session_start"
    )


def format_estimate(estimate: Optional[dict]) -> Optional[str]:
    """Human form of state.estimate (issue #99): `~3 sessions / 4h`, either
    part alone when the other is unset. None when there is nothing usable —
    an absent/malformed estimate (legacy or hand-edited state) must never
    break the brief. `time` is a free-form human string, never parsed."""
    if not isinstance(estimate, dict):
        return None
    parts = []
    sessions = estimate.get("sessions")
    if isinstance(sessions, int) and not isinstance(sessions, bool) and sessions > 0:
        parts.append(f"~{sessions} session{'s' if sessions != 1 else ''}")
    if estimate.get("time"):
        parts.append(str(estimate["time"]))
    return " / ".join(parts) if parts else None


def _iso_to_minutes_apart(earlier: str, later: str) -> Optional[float]:
    """Minutes between two ISO-8601 Z timestamps; None if unparseable."""
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        delta = datetime.strptime(later, fmt) - datetime.strptime(earlier, fmt)
        return delta.total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


def claim_status(
    state: Optional[dict],
    session: str,
    now: Optional[str] = None,
) -> dict:
    """Pure verdict on the run's single-flight release claim (RUN-STATE.md §7).

    The claim is the dedicated `claim` block in state.yaml —
    `{session_id, claimed_at}` — deliberately NOT the per-session `owner`
    field (cleared at every session end, warn-only semantics). No fs, no
    env reads: decide() and the CLI both call this on state they already
    hold, like decide()'s other inputs.

    Verdicts: CLAIM_UNCLAIMED (no block, or the run is no longer
    in-progress — completing or aborting the run releases the lock with no
    separate CAS write), CLAIM_OURS (held by `session`; re-claiming is an
    idempotent refresh), CLAIM_FOREIGN_LIVE (age under
    RELEASE_CLAIM_STALE_MINUTES — hard refusal territory), or
    CLAIM_FOREIGN_STALE (past the threshold, or claimed_at unparseable —
    the holder crashed or was reaped; the next claimant takes over loudly).
    `holder`/`claimed_at`/`age_minutes` are reported whenever a block
    exists, even when the verdict reads unclaimed.
    """
    verdict = {
        "verdict": CLAIM_UNCLAIMED,
        "holder": None,
        "claimed_at": None,
        "age_minutes": None,
    }
    if not isinstance(state, dict):
        return verdict
    claim = state.get("claim")
    if not isinstance(claim, dict) or not claim.get("session_id"):
        return verdict
    verdict["holder"] = claim.get("session_id")
    verdict["claimed_at"] = claim.get("claimed_at")
    verdict["age_minutes"] = _iso_to_minutes_apart(
        str(claim.get("claimed_at") or ""), now or utc_now_iso()
    )
    if state.get("status") != "in-progress":
        return verdict  # the claim is valid only while the run is live
    if verdict["holder"] == session:
        verdict["verdict"] = CLAIM_OURS
    elif (
        verdict["age_minutes"] is not None
        and abs(verdict["age_minutes"]) < RELEASE_CLAIM_STALE_MINUTES
    ):
        # abs(): the threshold bounds clock skew in BOTH directions. A
        # plain `age < threshold` reads a future-dated claimed_at (holder
        # clock fast by hours, then crashed) as live until the skew itself
        # elapses; a plain `0 <= age` flips small negative skew into an
        # instant takeover of a genuinely live holder. Beyond the threshold
        # either way, the claimed_at cannot be trusted — stale.
        verdict["verdict"] = CLAIM_FOREIGN_LIVE
    else:
        verdict["verdict"] = CLAIM_FOREIGN_STALE
    return verdict


def is_claimed_by_other(
    state: Optional[dict],
    session: str,
    now: Optional[str] = None,
) -> bool:
    """True exactly when a LIVE foreign claim holds the run — the hard-refusal
    predicate (§7). A stale foreign claim reads False: it is takeover
    territory, not refusal territory."""
    return claim_status(state, session, now)["verdict"] == CLAIM_FOREIGN_LIVE


def _claim_pointer(rdir: Path, state: dict, status: dict) -> dict:
    """The loser's pointer (§7): enough to find the active release without
    archaeology. The CLI adds the web URL (git_ops.web_url_for)."""
    return {
        "slug": state.get("slug"),
        "run_dir": str(rdir),
        "holder": status["holder"],
        "claimed_at": status["claimed_at"],
        "age_minutes": status["age_minutes"],
        "status": state.get("status"),
        "phase": state.get("phase"),
    }


def claim_pointer(
    rdir: Path,
    state: dict,
    session: Optional[str] = None,
    now: Optional[str] = None,
) -> dict:
    """The loser's pointer (§7) for a caller that refuses BEFORE reaching a
    kernel verb — `work release abort`'s live-foreign-claim guard has to run
    ahead of record_abort, so it cannot get the pointer off a
    ClaimRefusedError the way the claim/unclaim paths do."""
    return _claim_pointer(
        rdir, state, claim_status(state, session or current_session_id(), now)
    )


def claim_run(
    rdir: Path,
    state: dict,
    session: Optional[str] = None,
    now: Optional[str] = None,
) -> dict:
    """Write the LOCAL half of the single-flight release claim (§7).

    Evaluates the claim in `state` — which the CLI feeds from the REMOTE
    head after a fetch, composing this with git_ops.claim_push_once (whose
    non-fast-forward rejection closes the fetch→push race window; this
    kernel owns only the state write, never the push):

    - unclaimed → take the claim.
    - ours → idempotent refresh of `claimed_at` (how the holder keeps the
      claim live; the watch loop re-claims well inside the threshold).
    - foreign + live → HARD refusal: ClaimRefusedError carrying the loser's
      pointer. Never a warning — the party being refused is normally an
      unattended second launch.
    - foreign + stale → automatic LOUD takeover: the `claim` event records
      the displaced session and the claim's age. Automatic because the next
      claimant is normally the unattended scheduled relaunch; safe because
      takeover resumes the SAME run — it can never start a second parallel
      release.

    Writes through the single writer (write_state), appends a `claim`
    audit event, and returns the updated state.
    """
    session = session or current_session_id()
    now = now or utc_now_iso()
    status = claim_status(state, session, now)
    if status["verdict"] == CLAIM_FOREIGN_LIVE:
        raise ClaimRefusedError(
            f"run '{state.get('slug')}' is claimed by session {status['holder']} "
            f"({int(status['age_minutes'])} min ago) — refusing while the "
            f"claim is live",
            _claim_pointer(rdir, state, status),
        )
    state["claim"] = {"session_id": session, "claimed_at": now}
    write_state(rdir, state)
    data: dict = {"session_id": session, "claimed_at": now}
    if status["verdict"] == CLAIM_FOREIGN_STALE:
        age = status["age_minutes"]
        age_text = f"{int(age)} min old" if age is not None else "unreadable claimed_at"
        note = (
            f"stale-claim takeover: displaced session {status['holder']} ({age_text})"
        )
        data["action"] = "takeover"
        data["displaced_session"] = status["holder"]
        data["displaced_claimed_at"] = status["claimed_at"]
        data["displaced_age_minutes"] = age
    elif status["verdict"] == CLAIM_OURS:
        note = "claim refreshed"
        data["action"] = "refresh"
    else:
        note = "claim taken"
        data["action"] = "claim"
    append_event(rdir, "claim", note=note, data=data)
    return state


def unclaim_run(
    rdir: Path,
    state: dict,
    session: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Clear the single-flight release claim (§7 `work release unclaim`).

    Clears only OUR claim: a foreign holder refuses (ClaimRefusedError)
    unless `force` — the human runbook (abort path, stranded-claim cleanup).
    The holder check is on the raw block, not liveness: staleness gates
    takeover-by-claim, never silent removal. No claim recorded is an
    idempotent no-op success — no write, no event. Returns the state.
    """
    session = session or current_session_id()
    claim = state.get("claim")
    if not isinstance(claim, dict) or not claim.get("session_id"):
        return state
    holder = claim.get("session_id")
    if holder != session and not force:
        status = claim_status(state, session)
        raise ClaimRefusedError(
            f"claim on '{state.get('slug')}' is held by session {holder} — "
            f"refusing to unclaim a foreign claim (force overrides)",
            _claim_pointer(rdir, state, status),
        )
    state["claim"] = None
    write_state(rdir, state)
    data = {"action": "unclaim", "holder": holder}
    if holder != session:
        data["forced"] = True
        note = f"claim force-released (was held by session {holder})"
    else:
        note = "claim released"
    append_event(rdir, "claim", note=note, data=data)
    return state


def abort_run(
    rdir: Path,
    state: dict,
    reason: Optional[str] = None,
    session: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Terminal abort of a release run (`work release abort` — release-flow
    spec §7's abandoned release: the bump merged, the human declined the
    release MR).

    DESIGN DECISION — abort is a terminal `stop_reason: aborted` on a
    `status: complete` run, NOT a fourth STATUSES value. `status` is the
    closed enum external consumers switch on without knowing releases
    exist: the external cleaner archives runs that are `complete`
    (RUN-STATE.md §6 — a new `aborted` status would sit outside its
    "complete or stale" rule until every deployed cleaner learned the
    value), decide()'s completed-run policy (issue #96) already refuses to
    silently resume `complete`/`archived` runs — exactly the
    no-resurrection guard an aborted run needs, for free — and
    claim_status() (§7) already reads any claim on a non-`in-progress` run
    as unclaimed, so the lock releases by the existing rule. `stop_reason`
    is the descriptive "why it stopped" axis that nothing switches on, so
    extending STOP_REASONS with `aborted` is additive and the
    schema-stays-1 promise holds.

    ONE atomic state write (the single writer) lands all three facts
    together — status → complete, stop_reason → aborted, claim → None —
    so no observer ever sees an aborted run still holding the lock. The
    claim is cleared outright rather than left as an inert block (§7 would
    read it unclaimed anyway) so `claim-status` never reports stranded
    "inactive claim block" noise.

    A LIVE FOREIGN claim REFUSES unless `force` — the same knob and wording
    as unclaim_run. Abort is the human's terminal decline, but "the human
    declined" and "another session is mid-release right now" are different
    facts, and only the caller knows which one holds. Without the guard,
    session B — correctly refused at `work release claim` — could follow
    the decline path and mark A's in-flight release terminal, freeing A's
    lock remotely so a third session drives the same release. A STALE
    foreign claim (crashed/reaped holder) still clears without `force`:
    staleness is exactly the takeover case. Our own claim, and a run with
    no claim, need no force.

    Aborting an already-aborted run is an idempotent no-op (no write, no
    event); a run that finished any other way refuses — aborting it would
    falsify its recorded outcome. The release.yaml half of the abort is
    release_run.record_abort's; pushing is the caller's business (the
    CLI's CAS push, like the claim verbs).
    """
    if state.get("status") != "in-progress":
        if state.get("stop_reason") == "aborted":
            return state  # already terminal — a re-abort converges
        raise RunStateError(
            f"run '{state.get('slug')}' is already {state.get('status')} "
            f"(stop_reason: {state.get('stop_reason')}) — nothing to abort"
        )
    session = session or current_session_id()
    status = claim_status(state, session)
    if status["verdict"] == CLAIM_FOREIGN_LIVE and not force:
        raise ClaimRefusedError(
            f"run '{state.get('slug')}' is claimed by session "
            f"{status['holder']} ({int(status['age_minutes'])} min ago) — "
            f"refusing to abort a release another session is driving "
            f"(--force overrides)",
            _claim_pointer(rdir, state, status),
        )
    claim = state.get("claim")
    holder = claim.get("session_id") if isinstance(claim, dict) else None
    state["status"] = "complete"
    state["stop_reason"] = "aborted"
    state["claim"] = None
    write_state(rdir, state)
    data: dict = {}
    note = "release run aborted"
    if reason is not None:
        # Free-text lands in the (shared) work repo — redact like the
        # other agent-typed writers do.
        data["reason"] = redact_secrets(reason)
        note = f"release run aborted: {data['reason']}"
    if holder is not None:
        data["cleared_claim_holder"] = holder
        if holder != session:
            # A forced abort over a live holder, or a stale-claim clear —
            # either way the displaced session must be findable in the audit
            # trail, not just named in a message that scrolled past.
            data["forced"] = force
            data["cleared_claim_verdict"] = status["verdict"]
    append_event(rdir, "run_aborted", note=note, data=data)
    return state


def decide(
    state: Optional[dict],
    events: list[dict],
    session: str,
    now: Optional[str] = None,
    ledger: Optional[dict] = None,
    sessions_used: Optional[int] = None,
    release: Optional[dict] = None,
) -> dict:
    """Pure resume decision (spec §4.3 `work resume`). No fs, no env reads —
    fully unit-testable; all inputs are passed in by the caller. `release`
    is the loaded release.yaml record (release-flow §3) — the caller loads
    it only for release runs, exactly like ledger/sessions_used, so every
    non-release run passes None and its output stays byte-identical."""
    if state is None:
        return {"kind": "none"}
    warnings: list[str] = []
    claim_info: Optional[dict] = None
    claim = state.get("claim")
    if isinstance(claim, dict) and claim.get("session_id"):
        # Single-flight release claim (RUN-STATE.md §7): ENFORCED, not
        # warn-only — this branch replaces the advisory owner branch below
        # for release runs (only they carry a claim block; the claim verbs
        # are its sole writers). Non-release runs never have the block, so
        # their warn-only semantics stay byte-identical.
        claim_info = claim_status(state, session, now=now)
        if claim_info["verdict"] == CLAIM_FOREIGN_LIVE:
            warnings.append(
                f"release claim held by session {claim_info['holder']} "
                f"({int(claim_info['age_minutes'])} min ago) — enforced "
                f"single-flight: `work release claim` refuses while it is live"
            )
        elif claim_info["verdict"] == CLAIM_FOREIGN_STALE:
            warnings.append(
                f"stale release claim from session {claim_info['holder']} — "
                f"past {RELEASE_CLAIM_STALE_MINUTES} min: the next "
                f"`work release claim` takes over loudly"
            )
    else:
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
    # Release-run position (release-flow §3: "relaunching the release taskdef
    # re-derives the leg from run state and continues"): derived purely from
    # the record the caller passed in — decide() stays IO-free.
    release_info: Optional[dict] = None
    if release is not None:
        # Deferred import: release_run imports this module's shared YAML
        # preamble at load time, so a top-level import here would be a cycle.
        from .release_run import RECEIPT_NAMES, ReleaseRunError, derive_leg

        tag = release.get("tag")
        receipts = release.get("receipts")
        receipts = receipts if isinstance(receipts, dict) else {}
        release_info = {
            "leg": None,
            "next_step": None,
            "version": release.get("version"),
            "bump_mr_merge_sha": release.get("bump_mr_merge_sha"),
            "release_mr_merge_sha": release.get("release_mr_merge_sha"),
            "tag": tag.get("name") if isinstance(tag, dict) else None,
            # Recorded receipts in ladder order — what has already shipped.
            "receipts": [n for n in RECEIPT_NAMES if n in receipts],
        }
        try:
            derived = derive_leg(release)
            release_info["leg"] = derived["leg"]
            release_info["next_step"] = derived["next_step"]
        except ReleaseRunError as exc:
            # derive_leg's hard stop (hand-edited/inconsistent record): the
            # brief must still render — the raw fields above stay, the
            # derived position reads unknown, and the stop text becomes a
            # warning instead of breaking the session-start hook.
            release_info["error"] = str(exc)
            warnings.append(f"release record inconsistent: {exc}")
    return {
        "kind": "run",
        "slug": state.get("slug"),
        "name": state.get("name"),
        "status": state.get("status"),
        # Structural signal for guard JSON / brief consumers (issue #96):
        # a finished run must never be silently resumed. `archived` counts —
        # until the external cleaner moves the dir under runs/archive/ the
        # slug still resolves here.
        "completed_run": state.get("status") in ("complete", "archived"),
        "phase": state.get("phase"),
        "stop_reason": state.get("stop_reason"),
        "critical_error": state.get("critical_error"),
        # The blocking question's text (issue #97): recorded alongside
        # stop_reason=question so it survives the session that asked it.
        "open_question": state.get("open_question"),
        "goal": state.get("goal"),
        # Session estimate recorded with the goal (issue #99), plus the
        # sessions-used actual — computed from events by the caller (this
        # function stays IO-free) and None when the caller didn't count.
        "estimate": state.get("estimate"),
        "sessions_used": sessions_used,
        "artifacts": state.get("artifacts") or {},
        # The single-flight release claim's verdict (§7) — None for runs
        # without a claim block (every non-release run). Additive key for
        # --json consumers, so the CLI and hooks never re-derive it.
        "claim": claim_info,
        # Release-run position block (release-flow §3) — None for every
        # non-release run, so their brief output stays byte-identical.
        "release": release_info,
        # Full ledger for --json consumers; the brief renders the one-line
        # summary from it (issue #89).
        "ledger": ledger,
        "recent_events": events,
        "warnings": warnings,
    }


def format_brief(
    decision: dict,
    seed: str | None = None,
    answered: Optional[dict] = None,
    run_dir_url: Optional[str] = None,
) -> str:
    """Human-readable resume brief for prompt injection. `seed` is the
    launch prompt (LMER_START_PROMPT), passed in by the caller — it selects
    which direction line the completed-run directive renders (issue #96).
    `answered` is a `{question, answer}` pair when a pushed answer was just
    applied on the way in (issue #98: `lmer --answer` → LMER_ANSWER at
    session start) — it leads the brief, since the answer IS the direction.
    `run_dir_url` is the run dir's web (tree) URL, derived by the caller
    (this function stays IO-free) and appended as the brief's final line
    when derivable (issue #104)."""
    if decision.get("kind") != "run":
        return "No run state found — this is a fresh run."
    if decision.get("name"):
        header = (
            f"Run: {decision['name']} "
            f"(slug: {decision['slug']}, status: {decision['status']})"
        )
    else:
        header = f"Run: {decision['slug']} (status: {decision['status']})"
    lines = []
    # A question answered on the way in (issue #98) leads everything: the
    # previous session stopped on it, and the answer is the new direction.
    if answered:
        # A question stop whose text was never recorded still gets its answer
        # (T24). No Q line then — there is nothing to put on it, and "Q: None"
        # would read as the question rather than as its absence.
        if answered.get("question"):
            lines.append("✅ ANSWERED QUESTION (this run's blocking question has its answer):")
            lines.append(f"Q: {answered.get('question')}")
        else:
            lines.append(
                "✅ ANSWERED QUESTION (the question's text was never recorded — "
                "this is the answer to it):"
            )
        lines.append(f"A: {answered.get('answer')}")
        lines.append("Proceed accordingly — record the follow-up goal/phase as you go.")
        lines.append("")
    # A run stopped on a blocking question surfaces it FIRST (issue #97):
    # the answer gates everything else the brief has to say.
    if decision.get("stop_reason") == "question" and decision.get("open_question"):
        lines.append("❓ OPEN QUESTION (answer before anything else):")
        lines.append(str(decision["open_question"]))
        lines.append("")
    lines += [
        header,
        f"Phase: {decision['phase'] or '—'}   Stop reason: {decision['stop_reason'] or '—'}",
    ]
    # Estimate recorded with the goal (issue #99), with the sessions-used
    # actual beside it when the caller counted one.
    estimate_text = format_estimate(decision.get("estimate"))
    if estimate_text:
        line = f"Estimate: {estimate_text}"
        used = decision.get("sessions_used")
        if used is not None:
            line += f" — used: {used} session{'s' if used != 1 else ''}"
        lines.append(line)
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
    # Release-run position block (release-flow §3): the derived leg/next
    # step, recorded identity (version, merge SHAs, tag), receipt set, and
    # claim state — everything a relaunched session needs to resume at
    # exactly one next action. Rendered only when decide() carried a
    # release record; non-release briefs stay byte-identical.
    release = decision.get("release")
    if release:
        if release.get("error"):
            lines.append(
                f"Release: {release.get('version') or '?'} — record "
                f"inconsistent (hard stop; see warning below)"
            )
        else:
            # An aborted record derives next_step None — nothing to advance.
            lines.append(
                f"Release: {release.get('version') or '?'} — "
                f"{release.get('leg')} (next: {release.get('next_step') or '—'})"
            )
        lines.append(f"  bump-MR merge SHA     {release.get('bump_mr_merge_sha') or '—'}")
        lines.append(f"  release-MR merge SHA  {release.get('release_mr_merge_sha') or '—'}")
        lines.append(f"  tag                   {release.get('tag') or '—'}")
        receipts = release.get("receipts") or []
        lines.append(f"  receipts              {', '.join(receipts) if receipts else '—'}")
        claim = decision.get("claim")
        verdict = claim.get("verdict") if isinstance(claim, dict) else None
        if verdict == CLAIM_OURS:
            claim_text = f"ours (session {claim.get('holder')})"
        elif verdict in (CLAIM_FOREIGN_LIVE, CLAIM_FOREIGN_STALE):
            age = claim.get("age_minutes")
            age_text = f"{int(age)} min ago" if age is not None else "unreadable claimed_at"
            live = "LIVE" if verdict == CLAIM_FOREIGN_LIVE else "stale"
            claim_text = f"foreign ({live}) — session {claim.get('holder')}, {age_text}"
        else:
            claim_text = "unclaimed"
        lines.append(f"  claim                 {claim_text}")
    for warning in decision.get("warnings", []):
        lines.append(f"⚠️  {warning}")
    events = decision.get("recent_events") or []
    if events:
        lines.append("Recent events:")
        for event in events:
            note = f" — {event['note']}" if event.get("note") else ""
            lines.append(f"  {event.get('ts', '?')} [{event.get('type', '?')}]{note}")
    if decision.get("completed_run"):
        lines.append("")
        lines.append("⚠️  COMPLETED RUN — direction contract:")
        lines.append("This run is finished. Do NOT resume it or invent work.")
        if seed:
            seed = " ".join(seed.split())  # keep the brief field one-line
            excerpt = (seed if len(seed) <= SEED_EXCERPT_MAX_LEN
                       else seed[:SEED_EXCERPT_MAX_LEN] + "…")
            lines.append(f"Seed provided (LMER_START_PROMPT): {excerpt}")
            lines.append(
                'Record it as the goal (`work goal "<seed>"`), reopen with '
                "`work state set --status=in-progress --stop-reason=none`, "
                "then proceed on the seed."
            )
        else:
            lines.append(
                "No seed — ask the user (new target vs continue this run). "
                "If the question goes unanswered, record "
                '`work state set --stop-reason=question --question "<text>"` '
                "and end the session — never proceed on a guess."
            )
    if run_dir_url:
        lines.append(f"Run dir: {run_dir_url}")
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

    Bundle specs also land in the project-level specs index (issue #101),
    linked to the BUNDLE file — the one canonical target — never to the
    run-root convenience symlink made here.

    Returns the list of link names now present at the run-dir root.
    """
    linked: list[str] = []
    spec_files: list[tuple[Path, str]] = []  # (bundle file, run-root link name)
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
                if specs_index.is_spec_artifact(name):
                    spec_files.append((bundle / name, link_name))
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
        # Specs index (issue #101): each bundle spec gets a dated entry
        # under {host}/{project}/specs/ pointing at the bundle file — the
        # canonical target, never the run-root symlink made above. The
        # entry basename is the run-root link name so multi-bundle specs
        # stay distinct. upsert_spec_link is fail-soft on its own; the
        # label lookup uses the best-effort sibling reader, so neither a
        # corrupt state nor an index problem can fail the sync.
        for spec_file, link_name in spec_files:
            specs_index.upsert_spec_link(
                spec_file, specs_index.run_label(rdir), alias=link_name
            )
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
