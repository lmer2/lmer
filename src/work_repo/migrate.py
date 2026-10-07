"""`work migrate <project> [--apply] [--compat-link]` — the project-form engine
(issue #368; run-identity spec §13.B, §13.D, §1.6, §10).

What it does
============
Plans and applies the moves that take ONE registered project from the legacy
area names to schema form, dry-run by default:

1. every legacy `runs/<slug>[--<name>]/` becomes
   `_runs/<slug>/<session-id>[--<name>]/`, the id taken from the dir's FIRST
   `session_start` event; the record's creation timestamp, spelled in
   `LMER_SESSION_ID` form, is the fallback (schema spec §3.5). A dir holding
   several sessions' history stays ONE session dir — v1 never splits
   history — and the plan says so;
2. `runs/archive/<slug>[--<name>]/` becomes
   `_archive/_runs/<slug>/<session-id>[--<name>]/`, D13's one archive home;
3. `specs/` → `_specs/`, `plans/` → `_plans/`, `info/` → `_info/`,
   `taskdef/` → `_taskdef/`;
4. `memory/` → `_agents/memory/`, each file routed by its `metadata.type`
   through :func:`work_repo.memory.scope_for_memory` (D17, used unchanged):
   `project` stays in the project scope, `user`/`feedback`/`reference` lift
   to the instance scope `_agents/memory/` at the plexus root, and a file
   with no type or an unknown one stays in project scope and is reported.
   The EXISTING `MEMORY.md` lines are routed between the two indexes by the
   file each `](name.md)` link names — a line that lost its newline and
   carries entries for both scopes is cut back into one line per entry —
   and no line is generated: a file with no entry is reported (D.index's
   reconcile adds its line on the next persist), an entry with no file stays
   in the project index, and a tombstone stays a tombstone in the index that
   wrote it;
5. every relative symlink in `_specs/` and `_plans/` (either spelling) whose
   target moved is re-pointed IN PLACE at the moved path, so the entry keeps
   its name and with it its registration date. A link that was ALREADY
   dangling is reported and left untouched: the tool is not the hygiene fix,
   and it never drops an entry the way `work specs-index --rebuild` does;
6. with `--compat-link`, a relative symlink at each old path.

Re-pointing in place, rather than rewriting the entries in the schema-form
grammar `YYYY-MM-DD-<label>-<session-id>--<artifact>.md`, is deliberate. The
`-<session-id>` half of that grammar is what keeps two launches' entries
apart, and a legacy run dir holds exactly ONE record, so a migrated entry
collides with nothing: every launch registered from now on gets the suffix
from `upsert_spec_link`, whose stale-entry sweep never matches the bare
`<label>` spelling, and the freeze/re-slug re-point still recognises that
spelling. Rewriting would re-derive every label from the records — the job
`work specs-index --rebuild` already does, with the drops the guide warns
about — while re-pointing needs nothing but this plan's own move table and
so runs from an operator shell without the session env `work specs-index`
needs.

The identity model (§13.B)
==========================
A moved record is renamed and nothing else: `name: null` stays null (no D5
default label), no uid is minted for history, and its `state.yaml` /
`events.jsonl` bytes are untouched. This module is not an execution writing a
record (§1.6): it never calls `write_state`, `append_event` or
`freeze_run_dir` on a record it moves. The only record it writes is the
CALLER's own, when the command runs inside a session — one
:data:`MOVE_EVENT` per applied change plus one run-log line (`cmd_event`
writes only the caller's record, §10/N3). The commit message always lists
the changes, so an operator-shell run leaves the same trail in git history.

Preconditions, in the order they are checked
============================================
- a manifest is present — otherwise this is a legacy tree and `work adopt`
  registers the project first (D20: adoption flips the form, migration
  never does);
- the version floor (SCHEMA.md "Version floor"): this lmer reads both forms
  and the manifest's `schema:` is one it knows;
- `schema:` is writable: :func:`lmer_schema.writer.load_for_write` is the
  one tool that may touch the manifest file, and its refusal — a newer schema,
  a symlinked or unparseable file, a registry that does not build — is this
  check. Nothing is written through the handle: this command changes no
  manifest;
- the project is registered and has a directory
  (:func:`lmer_schema.checks.walk.project_dirs`, the ONE project walk);
- every path a move touches is clean in git — the dry run reports it, the
  apply re-checks under the lock. A clean tree is NOT evidence that a
  writer stopped, which is what the next check is for;
- NO record whose address changes is live (§1.6, astra PR3):
  :func:`preflight_no_live_records`, the ONE read-only lifecycle preflight
  that `work adopt` and the plain-session fold call too. It runs inside the
  mutation lock immediately before the first move; a refusal makes no
  filesystem, index or manifest change.

The plan is data
================
:class:`Plan` is an ordered tuple of :class:`Move`s — records, archive,
areas, the memory lift — plus the re-points, the memory routing tables with
the index writes they imply (:class:`MemoryLift`), the compat links, notes
and blockers. The dry run renders it (`git mv` lines, the two routing
tables, `ln -sfn` lines, `ln -s` lines, notes); the apply replays it with
`git mv` (history preserved; a plain rename only for an empty, untracked
directory git cannot move), writes the two indexes, re-points the links and
lands ONE commit through :func:`git_ops.commit_work_path`, the existing
commit → rebase → push helper. Later rules add their own kinds without
changing this shape: the plain-session fold (B.fold), `--instance`
(B.instance-floor). A migrated project plans zero moves and the command says
so.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import git_ops, memory, run_state, session_record
from .transactions import work_mutation
from .utils import MANIFEST_NAME, CoreUnavailableError, core_module, work_repo_root

__all__ = [
    "ARCHIVE",
    "AREA",
    "AREA_RENAMES",
    "INDEX",
    "INDEX_AREAS",
    "INSTANCE_MEMORY",
    "LEGACY_MEMORY",
    "LINK",
    "MEMORY",
    "MOVE_EVENT",
    "RECORD",
    "REPOINT",
    "SCHEMA_MEMORY",
    "IndexWrite",
    "MemoryLift",
    "MigrateError",
    "Move",
    "Plan",
    "cmd_migrate",
    "legacy_session_id",
    "migrate_project",
    "plan_project",
    "preflight_no_live_records",
    "write_rel_paths",
]

#: :attr:`Move.kind` values. A kind names the rule that planned the change;
#: later slices add theirs beside these.
RECORD = "record"
ARCHIVE = "archive"
AREA = "area"
MEMORY = "memory"
REPOINT = "repoint"
LINK = "compat-link"
#: The kind an :class:`IndexWrite` reports under.
INDEX = "index"

#: The bare area names and their schema twins (SCHEMA.md Appendix A), in
#: plan order. `memory/` is deliberately absent: its lift is routed by
#: `metadata.type` (:meth:`_Planner.memory`), not renamed whole.
AREA_RENAMES = (
    ("specs", "_specs"),
    ("plans", "_plans"),
    ("info", "_info"),
    ("taskdef", "_taskdef"),
)

LEGACY_RUNS = "runs"
SCHEMA_RUNS = "_runs"
ARCHIVE_HOME = "_archive"

#: The memory scopes: the legacy project dir, its schema twin, and the
#: instance scope at the plexus root (D17; `utils.project_memory_dir` /
#: `utils.instance_memory_dir` spell the same three).
LEGACY_MEMORY = "memory"
SCHEMA_MEMORY = "_agents/memory"
INSTANCE_MEMORY = "_agents/memory"

#: The index areas whose symlink entries are re-pointed, both spellings: a
#: project adopted and renamed by hand may hold `_specs/` while its runs are
#: still legacy.
INDEX_AREAS = ("specs", "_specs", "plans", "_plans")

#: The routing-table scope of a line both indexes keep (commentary).
BOTH = "both"

#: The event one applied change leaves in the CALLER's record.
MOVE_EVENT = "migrate_move"

#: The synthesized half of a fallback session id: `<timestamp>-0-0` is in
#: `LMER_SESSION_ID` form (`SESSION_ID_RE`) and visibly nobody's pid.
_FALLBACK_SUFFIX = "-0-0"

#: A list bullet closing the text before an entry link, for cutting a line
#: that lost its newline back into one line per entry.
_BULLET_RE = re.compile(r"[-*+]\s*$")


class MigrateError(Exception):
    """A precondition the command refuses on, with the remedy in the text."""


def _join(prefix: str, rel: str) -> str:
    """*rel* (project-relative, possibly climbing out with `..`) under
    *prefix* (the project dir, work-repo relative), normalised: the instance
    memory scope is `../_agents/memory` from the project dir and spells
    `_agents/memory` from the root."""
    return posixpath.normpath(f"{prefix}/{rel}")


@dataclass(frozen=True)
class Move:
    """One planned change, project-relative on both ends.

    For :data:`RECORD`, :data:`ARCHIVE`, :data:`AREA` and :data:`MEMORY` it
    is a rename: `src` → `dst`, applied with `git mv` when `tracked` (git
    tracks something under `src`) and a plain rename otherwise — git refuses
    to move an empty untracked directory, and there is no history there to
    keep. A :data:`MEMORY` lift to the instance scope has a `dst` that
    climbs out of the project dir (`../_agents/memory/<file>`).
    For :data:`LINK`, `src` is where the link is placed and `dst` is its
    RELATIVE target. For :data:`REPOINT`, `src` is the link at its post-move
    address, `dst` the relative target it gets and `was` the one it had.
    `note` is what the dry run says about this change.
    """

    kind: str
    src: str
    dst: str
    note: Optional[str] = None
    tracked: bool = True
    was: Optional[str] = None

    def render(self, prefix: str) -> str:
        """The shell spelling, work-repo relative (*prefix* is the project
        dir's relative path) — what the dry run prints and the commit
        message lists."""
        if self.kind == LINK:
            return f"ln -s {self.dst} {prefix}/{self.src}"
        if self.kind == REPOINT:
            return f"ln -sfn {self.dst} {_join(prefix, self.src)}"
        verb = "git mv" if self.tracked else "mv"
        return f"{verb} {_join(prefix, self.src)} {_join(prefix, self.dst)}"

    def event(self, prefix: str) -> tuple:
        """`(note, data)` for the :data:`MOVE_EVENT` this change leaves in
        the caller's record: addresses for a rename, targets (and the link's
        path) for a re-point."""
        if self.kind == REPOINT:
            path = _join(prefix, self.src)
            return (
                f"{path}: {self.was} -> {self.dst}",
                {"kind": self.kind, "from": self.was, "to": self.dst, "path": path},
            )
        src, dst = _join(prefix, self.src), _join(prefix, self.dst)
        return f"{src} -> {dst}", {"kind": self.kind, "from": src, "to": dst}


@dataclass(frozen=True)
class IndexWrite:
    """One `MEMORY.md` the memory lift writes: `path` project-relative at its
    post-move address (the instance one climbs out with `..`), `text` the
    whole file, `note` what the dry run says about it. The text is composed
    from the legacy index's EXISTING lines by memory.py's own splitter and
    merge — never a line this module made up."""

    path: str
    text: str
    note: str

    @property
    def kind(self) -> str:
        return INDEX

    def render(self, prefix: str) -> str:
        return f"write {_join(prefix, self.path)}"

    def event(self, prefix: str) -> tuple:
        src = _join(prefix, f"{LEGACY_MEMORY}/{memory.MEMORY_INDEX}")
        dst = _join(prefix, self.path)
        return f"{src} -> {dst} ({self.note})", {"kind": INDEX, "from": src, "to": dst}


@dataclass(frozen=True)
class MemoryLift:
    """The memory half of a plan as data, beside the :data:`MEMORY` renames
    in :attr:`Plan.moves`: the two routing tables the dry run shows and the
    index writes they imply.

    `files` is `(rel, scope, remark)` per file under `memory/`, `lines` is
    `(scope, line)` per index line after the lost-newline cut — scope is
    `project`, `instance` or :data:`BOTH` for commentary both indexes keep —
    and `writes` the :class:`IndexWrite`s (none when the `git mv` carries
    the index unchanged and nothing lifts to the instance scope).
    """

    files: tuple
    lines: tuple
    writes: tuple

    def render(self, prefix: str) -> list:
        lines = []
        if self.files:
            lines.append(f"memory routing ({prefix}/{LEGACY_MEMORY}/, one scope per metadata.type):")
            lines.extend(f"  {scope:<8}  {rel}" for rel, scope, _remark in self.files)
        if self.lines:
            lines.append(
                f"memory index routing ({prefix}/{LEGACY_MEMORY}/{memory.MEMORY_INDEX}, "
                "one index per scope):"
            )
            lines.extend(f"  {scope:<8}  {line}" for scope, line in self.lines if line.strip())
        for write in self.writes:
            lines.append(write.render(prefix))
            lines.append(f"    # {write.note}")
        return lines


@dataclass(frozen=True)
class Plan:
    """What `work migrate <project>` would do, as data.

    `moves` are the renames in apply order; `repoints` the index entries
    re-pointed at their moved targets; `memory_lift` the memory routing and
    its index writes (None when there is no `memory/` to lift); `links` the
    compat symlinks (empty unless asked for); `notes` what the operator
    should know; `blockers` what stops the apply (a taken destination, two
    legacy dirs mapping to one session dir, a bare and a schema area both
    present). Git cleanliness is not stored here — the dry run reports it
    and the apply re-checks it under the lock, since it can change in
    between.
    """

    root: Path
    project: str
    project_dir: Path
    moves: tuple
    links: tuple
    notes: tuple
    blockers: tuple
    repoints: tuple = ()
    memory_lift: Optional[MemoryLift] = None

    @property
    def rel(self) -> str:
        """The project dir, work-repo relative."""
        return self.project_dir.relative_to(self.root).as_posix()

    @property
    def is_noop(self) -> bool:
        return not self.moves

    @property
    def index_writes(self) -> tuple:
        return self.memory_lift.writes if self.memory_lift is not None else ()

    def rel_of(self, project_rel: str) -> str:
        return _join(self.rel, project_rel)

    def record_dirs(self) -> list:
        """The record dirs whose address changes — the preflight's input."""
        return [
            self.project_dir / move.src
            for move in self.moves
            if move.kind in (RECORD, ARCHIVE)
        ]

    def git_paths(self) -> list:
        """Every work-repo-relative path a change touches, old and new, for
        the clean-tree check and for staging."""
        paths = []
        for move in (*self.moves, *self.repoints, *self.links):
            rels = (move.src,) if move.kind in (LINK, REPOINT) else (move.src, move.dst)
            for rel in rels:
                rel = self.rel_of(rel)
                if rel not in paths:
                    paths.append(rel)
        for write in self.index_writes:
            rel = self.rel_of(write.path)
            if rel not in paths:
                paths.append(rel)
        return paths

    def cleanups(self) -> list:
        """Legacy directories expected empty once the moves land (git tracks
        none), project relative, innermost first — `rmdir`'d by the apply so
        no bare `runs/` shell survives the migration. With compat links the
        shells hold the links and stay."""
        if self.links:
            return []
        out = []
        for rel in (f"{LEGACY_RUNS}/{run_state.ARCHIVE_DIR}", LEGACY_RUNS):
            directory = self.project_dir / rel
            if not directory.is_dir() or directory.is_symlink():
                continue
            moved = {m.src for m in self.moves} | set(out)
            left = [
                child for child in directory.iterdir()
                if f"{rel}/{child.name}" not in moved
            ]
            if not left:
                out.append(rel)
        return out

    def summary(self) -> str:
        """`N moves, N re-points, N index writes, N compat links` — the
        families that apply, counted."""
        parts = [_count(len(self.moves), "move")]
        if self.repoints:
            parts.append(_count(len(self.repoints), "re-point"))
        if self.index_writes:
            parts.append(_count(len(self.index_writes), "index write"))
        if self.links:
            parts.append(_count(len(self.links), "compat link"))
        return ", ".join(parts)

    def render(self) -> str:
        """The dry-run text."""
        lines = [f"work migrate {self.project} — plan for `{self.rel}/`"]
        if self.is_noop:
            lines.append(
                f"nothing to migrate: `{self.project}` is already in schema form "
                "(no legacy run dirs, no bare areas)"
            )
        for move in self.moves:
            lines.append(move.render(self.rel))
            if move.note:
                lines.append(f"    # {move.note}")
        if self.memory_lift is not None:
            lines.extend(self.memory_lift.render(self.rel))
        for repoint in self.repoints:
            lines.append(repoint.render(self.rel))
            lines.append(f"    # was {repoint.was}")
        for link in self.links:
            lines.append(link.render(self.rel))
        for rel in self.cleanups():
            lines.append(f"rmdir {self.rel_of(rel)}")
        if self.notes:
            lines.append("notes:")
            lines.extend(f"  - {note}" for note in self.notes)
        if self.blockers:
            lines.append("blockers (the apply refuses until these are resolved):")
            lines.extend(f"  - {blocker}" for blocker in self.blockers)
        if not self.is_noop:
            lines.append(f"{self.summary()}; dry run — add --apply to perform them in ONE commit")
        return "\n".join(lines)

    def commit_message(self) -> str:
        """ONE commit, its body the change list: the durable trail of what
        moved, whether or not a session record carried the events."""
        subject = f"work migrate {self.project}: {self.summary()} to schema form"
        body = [
            change.render(self.rel)
            for change in (*self.moves, *self.index_writes, *self.repoints, *self.links)
        ]
        return subject + "\n\n" + "\n".join(body) + "\n"


def _count(n: int, noun: str, plural: Optional[str] = None) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {plural or noun + 's'}"


# ---------------------------------------------------------------------------
# The session id a legacy run dir nests under
# ---------------------------------------------------------------------------


def legacy_session_id(rdir: Path) -> tuple:
    """`(session_id, source, session_starts)` for a legacy run dir.

    The id of the dir's FIRST `session_start` event when that event names
    one in `LMER_SESSION_ID` form; otherwise the record's creation timestamp
    (`state.created`, else the first event's `ts`, else the directory's
    mtime) spelled `YYYYMMDD-HHMMSS-0-0` — the id form, with a pid and
    random half nobody's launch could carry. `source` says which
    (`"session_start"`, `"created"`, `"first event"`, `"mtime"`);
    `session_starts` is how many sessions logged a start here, so the plan
    can say when a shared dir's history stays interleaved. Read-only.
    """
    events = run_state.read_events(rdir, last_n=0)
    starts = [e for e in events if e.get("type") == "session_start"]
    if starts:
        session = starts[0].get("session")
        if isinstance(session, str) and run_state.SESSION_ID_RE.fullmatch(session):
            return session, "session_start", len(starts)
    state = run_state._read_sibling_state(rdir) or {}
    stamp = _timestamp_id(state.get("created"))
    if stamp:
        return stamp, "created", len(starts)
    for event in events:
        stamp = _timestamp_id(event.get("ts"))
        if stamp:
            return stamp, "first event", len(starts)
    try:
        when = datetime.fromtimestamp(rdir.stat().st_mtime, timezone.utc)
    except OSError:
        when = datetime.now(timezone.utc)
    return when.strftime("%Y%m%d-%H%M%S") + _FALLBACK_SUFFIX, "mtime", len(starts)


def _timestamp_id(stamp) -> Optional[str]:
    """An ISO timestamp as the fallback session id, or None."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S") + _FALLBACK_SUFFIX


# ---------------------------------------------------------------------------
# The read-only lifecycle preflight (§1.6, astra PR3) — shared with adopt and
# the fold
# ---------------------------------------------------------------------------


def preflight_no_live_records(records: Iterable[Path]) -> list:
    """Refuse to move any record that may still be written: the reasons, one
    per record, or `[]` when every record is stopped.

    Read-only, and the ONE rule for "may this record's address change?":
    `work migrate`, `work adopt` and the plain-session fold all call it
    inside the mutation lock immediately before their first move. A record
    passes only when :func:`session_record.is_stopped` says so from its own
    state and events — signed out (a final `session_end`) or a never-started
    seed. Unknown is not stopped: an unreadable or missing state file, or an
    events file with a line that does not parse, refuses (a torn line is a
    writer that may still be there). The caller's own record among them
    refuses too — the process asking is the writer that would be moved out
    from under itself — with the remedy in the text. A clean git tree is
    not consulted: it says nothing about whether a writer stopped.
    """
    own = _own_record()
    problems = []
    for rdir in records:
        rdir = Path(rdir)
        if own is not None and _same_path(rdir, own):
            problems.append(
                f"{rdir}: this session's own record; run from an operator shell "
                "or against another project"
            )
            continue
        state = run_state._read_sibling_state(rdir)
        if state is None:
            problems.append(f"{rdir}: state unreadable or missing — lifecycle unknown, not stopped")
            continue
        events = _strict_events(rdir)
        if events is None:
            problems.append(f"{rdir}: events.jsonl unreadable — lifecycle unknown, not stopped")
            continue
        if not session_record.is_stopped(state, events):
            problems.append(
                f"{rdir}: live — no final session_end and not a never-started seed"
            )
    return problems


def _strict_events(rdir: Path) -> Optional[list]:
    """Every event in `events.jsonl`, or None when any line does not parse
    or the file cannot be read. Stricter than :func:`run_state.read_events`
    on purpose: that reader tolerates a torn line so a resume survives a
    crash; this one decides whether a writer may still be there."""
    path = rdir / run_state.EVENTS_FILE
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        events.append(parsed)
    return events


def _own_record() -> Optional[Path]:
    """The address of this invocation's own record, or None from an
    operator shell (nothing resolves) — never raised over, so a broken
    launch context cannot stop a preflight that has records to judge."""
    try:
        return run_state.run_dir()
    except Exception:
        return None


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def _caller_record() -> Optional[Path]:
    """The caller's own, EXISTING record — where the move events and the
    run-log line go when the command runs inside a session."""
    own = _own_record()
    if own is None or run_state._state_path(own) is None:
        return None
    return own


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_project(project: str, *, root: Optional[Path] = None, compat_link: bool = False) -> Plan:
    """The plan for *project*, after the preconditions that need no lock.
    Raises :class:`MigrateError` on a missing manifest, the version floor,
    the writer's refusal, or an unregistered project."""
    root = Path(root) if root is not None else work_repo_root()
    project_dir = _resolve_project_dir(root, project)
    rel = project_dir.relative_to(root).as_posix()
    moves, repoints, notes, blockers = [], [], [], []
    planner = _Planner(
        project_dir, moves, repoints, notes, blockers,
        tracked=lambda src: git_ops._has_tracked_files(root, f"{rel}/{src}"),
    )
    planner.run_dirs(LEGACY_RUNS, SCHEMA_RUNS, RECORD)
    planner.run_dirs(
        f"{LEGACY_RUNS}/{run_state.ARCHIVE_DIR}", f"{ARCHIVE_HOME}/{SCHEMA_RUNS}", ARCHIVE
    )
    planner.areas()
    instance_dir = root / INSTANCE_MEMORY
    planner.memory(instance_dir, os.path.relpath(instance_dir, project_dir).replace(os.sep, "/"))
    planner.index_links()
    links = tuple(_compat_links(moves)) if compat_link else ()
    return Plan(
        root=root, project=project, project_dir=project_dir,
        moves=tuple(moves), links=links, notes=tuple(notes), blockers=tuple(blockers),
        repoints=tuple(repoints), memory_lift=planner.memory_lift,
    )


def _resolve_project_dir(root: Path, project: str) -> Path:
    """The registered project's directory, through the core's own readers."""
    try:
        walk_api = core_module("checks.walk")
        REGISTERED, project_dirs = walk_api.REGISTERED, walk_api.project_dirs
        manifest_api = core_module("manifest")
        CURRENT_SCHEMA, ManifestError, SUPPORTED_SCHEMAS = (
            manifest_api.CURRENT_SCHEMA, manifest_api.ManifestError, manifest_api.SUPPORTED_SCHEMAS,
        )
        build_registry = core_module("registry").build_registry
        load_for_write = core_module("writer").load_for_write
    except CoreUnavailableError as exc:
        raise MigrateError(
            "this lmer build has no lmer-schema core and is below the version "
            f"floor for a schema-form tree ({exc}); update lmer on this host first"
        ) from exc

    manifest = root / MANIFEST_NAME
    if not (manifest.is_symlink() or manifest.exists()):
        raise MigrateError(
            f"no {MANIFEST_NAME} at {root}: this is a legacy tree. Register the "
            f"project first — `work adopt <host>/<project> {project}` — then migrate it."
        )
    declared = _declared_schema(manifest)
    if isinstance(declared, int) and declared not in SUPPORTED_SCHEMAS:
        supported = ", ".join(str(s) for s in SUPPORTED_SCHEMAS)
        raise MigrateError(
            f"version floor: {MANIFEST_NAME} declares schema {declared}, this lmer "
            f"reads {supported} (current {CURRENT_SCHEMA}). Update lmer on this host "
            "before migrating (SCHEMA.md 'Version floor')."
        )
    # The writer's refusal IS the "schema: is writable" check. The handle is
    # discarded: this command mutates no manifest.
    try:
        writer = load_for_write(manifest)
    except ManifestError as exc:
        raise MigrateError(f"`schema:` is not writable: {exc}") from exc
    registry = build_registry(writer.manifest)
    if project not in registry:
        names = ", ".join(registry) or "none"
        raise MigrateError(
            f"`{project}` is not a registered project (registered: {names}); "
            "`work adopt` registers one"
        )
    for entry in project_dirs(root, registry=registry):
        if entry.kind == REGISTERED and entry.name == project:
            return entry.path
    raise MigrateError(
        f"`{project}` is registered but has no directory at {root / project}; "
        "`work adopt` moves the host bucket there"
    )


def _declared_schema(manifest: Path):
    """The raw `schema:` scalar, for the version-floor refusal to name.
    Anything unreadable is left to the writer, whose message is better."""
    import yaml

    try:
        raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except Exception:
        return None
    value = raw.get("schema") if isinstance(raw, dict) else None
    return None if isinstance(value, bool) else value


class _Planner:
    """The rules that fill one plan's move list, re-points, notes and
    blockers. Each rule appends in apply order; `tracked` asks git whether a
    source holds tracked files (a `git mv` or a plain rename)."""

    def __init__(self, project_dir, moves, repoints, notes, blockers, *, tracked):
        self.project_dir = project_dir
        self.moves, self.repoints = moves, repoints
        self.notes, self.blockers = notes, blockers
        self.tracked = tracked
        self.memory_lift: Optional[MemoryLift] = None

    def run_dirs(self, area_rel: str, dst_prefix: str, kind: str) -> None:
        """Every record-shaped child of one legacy run area nests under its
        session id. Non-records are noted and left: the tool is not the
        hygiene fix, and a dot-dir is a seed in flight or a crash's litter."""
        area = self.project_dir / area_rel
        if not area.is_dir() or area.is_symlink():
            return
        targets = {}
        for child in sorted(area.iterdir()):
            name = child.name
            src = f"{area_rel}/{name}"
            if kind == RECORD and name == run_state.ARCHIVE_DIR:
                continue  # planned as the archive relocation
            if child.is_symlink():
                self.notes.append(f"{src}: a symlink, left in place")
                continue
            if not child.is_dir():
                self.notes.append(f"{src}: a file, left in place")
                continue
            if name.startswith("."):
                self.notes.append(
                    f"{src}: dot directory (a seed or litter), not a record; left in place"
                )
                continue
            if run_state._state_path(child) is None:
                self.notes.append(f"{src}: no state file, not a record; left in place")
                continue
            slug, _sep, label = name.partition("--")
            sid, source, starts = legacy_session_id(child)
            dst = f"{dst_prefix}/{slug}/{sid}" + (f"--{label}" if label else "")
            remarks = []
            if starts > 1:
                remarks.append(
                    f"{starts} sessions' history interleaved; stays one session dir "
                    "(v1 never splits history)"
                )
            if source != "session_start":
                remarks.append(
                    f"session id from the {source} timestamp (no usable session_start event)"
                )
            if dst in targets:
                self.blockers.append(f"{src} and {targets[dst]} both map to {dst}")
            elif os.path.lexists(self.project_dir / dst):
                self.blockers.append(f"{dst} already exists; {src} cannot move there")
            targets.setdefault(dst, src)
            self.moves.append(
                Move(kind, src, dst, note="; ".join(remarks) or None, tracked=self.tracked(src))
            )

    def areas(self) -> None:
        for old, new in AREA_RENAMES:
            src = self.project_dir / old
            if not os.path.lexists(src):
                continue
            if src.is_symlink():
                self.notes.append(f"{old}: already a symlink -> {os.readlink(src)}, left in place")
                continue
            if not src.is_dir():
                self.notes.append(f"{old}: a file, left in place")
                continue
            if os.path.lexists(self.project_dir / new):
                self.blockers.append(
                    f"{old}/ and {new}/ both exist; merge them by hand, then re-run"
                )
                continue
            self.moves.append(Move(AREA, old, new, tracked=self.tracked(old)))

    def memory(self, instance_dir: Path, instance_rel: str) -> None:
        """`memory/` lifts to `_agents/memory/`, each file where its
        `metadata.type` routes it (D17): instance-scope files move out one by
        one to *instance_dir* (*instance_rel* is its spelling from the
        project dir), then the directory — the project-scope files and the
        index — renames whole, so every file keeps its history. The index
        routing is :func:`_plan_memory_lift`'s."""
        src = self.project_dir / LEGACY_MEMORY
        if not os.path.lexists(src):
            return
        if src.is_symlink():
            self.notes.append(
                f"{LEGACY_MEMORY}: already a symlink -> {os.readlink(src)}, left in place"
            )
            return
        if not src.is_dir():
            self.notes.append(f"{LEGACY_MEMORY}: a file, left in place")
            return
        if os.path.lexists(self.project_dir / SCHEMA_MEMORY):
            self.blockers.append(
                f"{LEGACY_MEMORY}/ and {SCHEMA_MEMORY}/ both exist; merge them by hand, then re-run"
            )
            return
        routes = _memory_routes(src)
        stays = (src / memory.MEMORY_INDEX).is_file()
        for rel, scope, remark in routes:
            if remark:
                self.notes.append(f"{LEGACY_MEMORY}/{rel}: {remark}")
            if scope != memory.INSTANCE_SCOPE:
                stays = True
                continue
            file_src = f"{LEGACY_MEMORY}/{rel}"
            if os.path.lexists(instance_dir / rel):
                self.blockers.append(
                    f"{INSTANCE_MEMORY}/{rel} already exists; {file_src} cannot move there"
                )
            self.moves.append(
                Move(MEMORY, file_src, f"{instance_rel}/{rel}", tracked=self.tracked(file_src))
            )
        # Once every file has lifted out, git has nothing left to move and
        # refuses the empty dir: a plain rename then, as for any empty area.
        self.moves.append(
            Move(MEMORY, LEGACY_MEMORY, SCHEMA_MEMORY, tracked=stays and self.tracked(LEGACY_MEMORY))
        )
        self.memory_lift = _plan_memory_lift(src, routes, instance_dir, instance_rel, self.notes)

    def index_links(self) -> None:
        """Every relative symlink in an index area whose target one of the
        renames so far moves is re-pointed, in place, at the moved path. A
        link that already dangles, or an absolute one, is reported and left:
        not this tool's hygiene to fix, and never dropped."""
        renames = list(self.moves)
        for area in INDEX_AREAS:
            directory = self.project_dir / area
            if directory.is_symlink() or not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                if not entry.is_symlink():
                    continue
                target = os.readlink(entry)
                rel = f"{area}/{entry.name}"
                if os.path.isabs(target):
                    self.notes.append(f"{rel}: absolute symlink (-> {target}), left in place")
                    continue
                if not entry.exists():
                    self.notes.append(
                        f"{rel}: already dangling (-> {target}); left untouched — "
                        "the migration is not the hygiene fix, and it drops no entry"
                    )
                    continue
                moved = _relocated(posixpath.normpath(posixpath.join(area, target)), renames)
                if moved is None:
                    continue
                new_dir = _relocated(area, renames) or area
                new_target = posixpath.relpath(moved, new_dir)
                if new_target == target:
                    continue
                self.repoints.append(Move(REPOINT, f"{new_dir}/{entry.name}", new_target, was=target))


def _relocated(rel: str, renames: list) -> Optional[str]:
    """*rel* (project-relative) after *renames* — the longest matching
    source wins, so a file lifted out of a directory that also moves follows
    its own move — or None when no rename touches it."""
    for move in sorted(renames, key=lambda m: len(m.src), reverse=True):
        if rel == move.src or rel.startswith(move.src + "/"):
            return move.dst + rel[len(move.src):]
    return None


def _memory_routes(memory_dir: Path) -> list:
    """`(rel, scope, remark)` per file under *memory_dir* (the index
    excluded), sorted. :func:`memory.scope_for_memory` is the ONE routing
    rule and is used unchanged; a file it cannot route — no frontmatter, no
    `metadata.type`, a type nobody routes — stays in :data:`memory.DEFAULT_SCOPE`
    and the remark says so when it is a memory file."""
    if memory_dir.is_symlink() or not memory_dir.is_dir():
        return []
    routes = []
    for path in sorted(memory_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(memory_dir).as_posix()
        if rel == memory.MEMORY_INDEX:
            continue
        scope = memory.scope_for_memory(memory._read_text(path))
        remark = None
        if scope is None:
            scope = memory.DEFAULT_SCOPE
            if rel.lower().endswith(".md"):
                remark = (
                    f"no usable `metadata.type` — stays in {scope} scope "
                    "(docs/SCHEMA.md, memory)"
                )
        routes.append((rel, scope, remark))
    return routes


def _plan_memory_lift(memory_dir, routes, instance_dir, instance_rel, notes) -> MemoryLift:
    """Route the legacy index's EXISTING lines between the two scopes and
    say what each index write is; every finding goes to *notes*.

    The routing is memory.py's own: :func:`memory._entry_targets` names the
    file each `](name.md)` link points at and :func:`memory._split_index`
    files each line by it, with every line declared restored from the
    project scope — the only index a legacy tree has — so a tombstone goes
    back there and nowhere else, and commentary is kept in both as persist
    keeps it. The one thing done first is the lost-newline cut: a physical
    line carrying entries for BOTH scopes is cut back into one line per
    entry (:func:`_split_entries`) before the splitter sees it, since the
    splitter files a whole line by its first link. Nothing is generated: a
    file with no line is reported for D.index's reconcile, an entry with no
    file stays in the project index as it is.
    """
    index_name = f"{LEGACY_MEMORY}/{memory.MEMORY_INDEX}"
    scope_of = {rel: scope for rel, scope, _remark in routes}
    files = tuple(routes)
    unindexed = (
        "no {index} line — none is generated here; the next persist's reconcile "
        "(D.index) adds it".format(index=memory.MEMORY_INDEX)
    )
    index = memory_dir / memory.MEMORY_INDEX
    if not index.is_file():
        notes.extend(f"{LEGACY_MEMORY}/{rel}: {unindexed}" for rel, _scope, _remark in routes)
        return MemoryLift(files=files, lines=(), writes=())

    text = memory._read_text(index)
    pieces, indexed = [], set()
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        targets = memory._entry_targets(line)
        indexed.update(targets)
        for target in targets:
            if target not in scope_of:
                notes.append(
                    f"{index_name} line {number}: entry without a file ({target}); "
                    "stays in the project index, untouched"
                )
        if len({scope_of.get(target, memory.DEFAULT_SCOPE) for target in targets}) > 1:
            notes.append(
                f"{index_name} line {number}: {_count(len(targets), 'entry', 'entries')} on one line "
                "(a lost newline) — split, one line per scope"
            )
            pieces.extend(_split_entries(line))
        else:
            pieces.append(line)
    notes.extend(
        f"{LEGACY_MEMORY}/{rel}: {unindexed}" for rel, _scope, _remark in routes if rel not in indexed
    )

    restored = {memory.PROJECT_SCOPE: {piece.strip() for piece in pieces}, memory.INSTANCE_SCOPE: set()}
    routed = {memory.PROJECT_SCOPE: [], memory.INSTANCE_SCOPE: []}
    rows = []
    for piece in pieces:
        split = memory._split_index(piece, scope_of, restored)
        owners = [scope for scope in (memory.PROJECT_SCOPE, memory.INSTANCE_SCOPE) if split[scope]]
        rows.append((BOTH if len(owners) == 2 else owners[0], piece.rstrip("\n")))
        for scope in owners:
            routed[scope].append(piece)

    writes = []
    lifted = sum(1 for scope, _line in rows if scope == memory.INSTANCE_SCOPE)
    project_text = "".join(routed[memory.PROJECT_SCOPE])
    if project_text != text:
        writes.append(IndexWrite(
            f"{SCHEMA_MEMORY}/{memory.MEMORY_INDEX}", project_text,
            f"project index: {_count(lifted, 'entry', 'entries')} moved out to the instance index",
        ))
    if lifted:
        existing = instance_dir / memory.MEMORY_INDEX
        current = memory._read_text(existing) if existing.is_file() else ""
        instance_text = "".join(routed[memory.INSTANCE_SCOPE])
        if current.strip():
            merged = memory._merge_index([current, instance_text])
            note = f"merged into the existing instance index: {_count(lifted, 'entry', 'entries')} added"
        else:
            merged, note = instance_text, f"new instance index: {_count(lifted, 'entry', 'entries')}"
        if merged != current:
            writes.append(IndexWrite(f"{instance_rel}/{memory.MEMORY_INDEX}", merged, note))
    return MemoryLift(files=files, lines=tuple(rows), writes=tuple(writes))


def _split_entries(line: str) -> list:
    """One physical index line carrying entries for both scopes — a newline
    lost somewhere — cut back into one line per entry. Each later entry
    starts at the list bullet that introduces it (at its link when there is
    none), so nothing but the newlines is added: the lines are the ones the
    index already carried."""
    cuts, previous_end = [], None
    for match in memory._LINK_RE.finditer(line):
        if not memory._entry_targets(match.group(0)):
            continue
        if previous_end is not None:
            bullet = _BULLET_RE.search(line, previous_end, match.start())
            cuts.append(bullet.start() if bullet else match.start())
        previous_end = match.end()
    pieces, start = [], 0
    for cut in cuts:
        pieces.append(line[start:cut].rstrip() + "\n")
        start = cut
    pieces.append(line[start:])
    return pieces


def _compat_links(moves) -> list:
    """A relative symlink at each old path: an area (the memory dir
    included) links to its twin beside it; a record links across to its
    session dir. A file lifted to the instance scope gets none: the project
    scope must hold no pointer into the instance one."""
    links = []
    for move in moves:
        if move.kind == MEMORY and move.src != LEGACY_MEMORY:
            continue
        if move.kind in (AREA, MEMORY):
            target = move.dst
        else:
            target = os.path.relpath(move.dst, start=os.path.dirname(move.src))
        links.append(Move(LINK, move.src, target))
    return links


def _dirty_paths(root: Path, rels: list) -> list:
    """`git status --porcelain` over the paths that exist; any line is dirt.
    Untracked content counts: `git mv` would carry it along unstaged."""
    present = [rel for rel in rels if os.path.lexists(root / rel)]
    if not present:
        return []
    rc, output = git_ops.run_git_command(
        ["status", "--porcelain", "--", *present], root, check=False
    )
    if rc != 0:
        raise MigrateError(f"git status failed under {root}: {output}")
    return [line for line in output.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _apply(plan: Plan, out: Callable[[str], None], err: Callable[[str], None]) -> int:
    """Under the mutation lock: re-check the tree, run the preflight, replay
    the plan, record it, commit once. Returns the exit code."""
    root = plan.root
    configured = work_repo_root()
    if _resolved(root) != _resolved(configured):
        raise MigrateError(
            f"LMER_WORK_REPO_PATH ({configured}) must name the tree being migrated "
            f"({root}): the commit helper is bound to the configured checkout"
        )
    with work_mutation(root):
        problems = list(plan.blockers)
        problems += [f"dirty in git: {line}" for line in _dirty_paths(root, plan.git_paths())]
        problems += preflight_no_live_records(plan.record_dirs())
        if problems:
            for problem in problems:
                err(f"❌ {problem}")
            err("refused: nothing was changed")
            return 1

        applied = []
        for move in plan.moves:
            src, dst = plan.rel_of(move.src), plan.rel_of(move.dst)
            (root / dst).parent.mkdir(parents=True, exist_ok=True)
            if move.tracked:
                rc, output = git_ops.run_git_command(["mv", src, dst], root, check=False)
                if rc != 0:
                    err(f"❌ git mv {src} {dst} failed: {output}")
                    err(
                        f"{_count(len(applied), 'change')} already applied and staged, "
                        "nothing committed: inspect `git status` and finish or revert by hand"
                    )
                    return rc
            else:
                os.rename(root / src, root / dst)
            out(move.render(plan.rel))
            applied.append(move)
        for write in plan.index_writes:
            rel = plan.rel_of(write.path)
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            _write_index(root / rel, write.text)
            rc = _stage(root, rel, err)
            if rc != 0:
                return rc
            out(write.render(plan.rel))
            applied.append(write)
        for repoint in plan.repoints:
            rel = plan.rel_of(repoint.src)
            os.unlink(root / rel)
            os.symlink(repoint.dst, root / rel)
            rc = _stage(root, rel, err)
            if rc != 0:
                return rc
            out(repoint.render(plan.rel))
            applied.append(repoint)
        for link in plan.links:
            rel = plan.rel_of(link.src)
            os.symlink(link.dst, root / rel)
            rc = _stage(root, rel, err)
            if rc != 0:
                return rc
            out(link.render(plan.rel))
        for rel in plan.cleanups():
            try:
                os.rmdir(root / plan.rel_of(rel))
                out(f"rmdir {plan.rel_of(rel)}")
            except OSError as exc:
                out(f"⚠️  {plan.rel_of(rel)} not removed: {exc}")

        paths = plan.git_paths() + _record_in_caller(plan, applied, out)
        return git_ops.commit_work_path(paths, plan.commit_message())


def _stage(root: Path, rel: str, err: Callable[[str], None]) -> int:
    """`git add` one written path (a link, an index) for the ONE commit."""
    rc, output = git_ops.run_git_command(["add", "--", rel], root, check=False)
    if rc != 0:
        err(f"❌ git add {rel} failed: {output}")
    return rc


def _write_index(path: Path, text: str) -> None:
    """The memory lift's index write — the ONE file this module writes.
    `MEMORY.md` is memory.py's file, but the split is the migration's to
    make (SCHEMA.md Appendix A: one index per scope, each listing its own
    files), and memory.py writes an index only behind a session's restore
    baseline. The text was composed from the index's existing lines by
    memory.py's own splitter and merge (:func:`_plan_memory_lift`)."""
    path.write_text(text, encoding="utf-8")


def _resolved(path: Path) -> Path:
    try:
        return Path(path).resolve()
    except OSError:
        return Path(path)


def _record_in_caller(plan: Plan, applied: list, out: Callable[[str], None]) -> list:
    """One :data:`MOVE_EVENT` per applied change plus a run-log line in the
    CALLER's own record when there is one; the paths to stage it with.
    From an operator shell there is nothing to write — the commit message
    carries the list — and that is said, not assumed. Fail-soft: a record
    that refuses the event never fails a migration that already happened."""
    caller = _caller_record()
    if caller is None:
        out("ℹ️  no session record here; the commit message lists the moves")
        return []
    for change in applied:
        note, data = change.event(plan.rel)
        try:
            run_state.append_event(
                caller, MOVE_EVENT, note=note, data={"project": plan.project, **data},
            )
        except (run_state.RunStateError, OSError) as exc:
            out(f"⚠️  {MOVE_EVENT} events not recorded in {caller}: {exc}")
            break
    try:
        from .loggers import get_logger  # local: loggers imports run_state

        get_logger(caller / "log.yaml").log(
            f"work migrate {plan.project}: applied {plan.summary()} to schema form"
        )
    except Exception as exc:  # the log is a courtesy, never the result
        out(f"⚠️  run-log line not recorded in {caller}: {exc}")
    try:
        return list(run_state.run_rel_path_candidates())
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def migrate_project(
    project: str,
    *,
    root: Optional[Path] = None,
    apply: bool = False,
    compat_link: bool = False,
    out: Callable[[str], None] = print,
    err: Optional[Callable[[str], None]] = None,
) -> int:
    """`work migrate <project> [--apply] [--compat-link]` as a library call.

    Plans (printing the plan through *out* on a dry run), applies when
    asked, and reports every refusal through *err* (stderr by default) with
    exit code 1. A migrated project is a no-op that says so, in both modes.
    """
    if err is None:
        err = lambda line: print(line, file=sys.stderr)  # noqa: E731
    try:
        plan = plan_project(project, root=root, compat_link=compat_link)
        if plan.is_noop or not apply:
            out(plan.render())
            if not plan.is_noop:
                dirty = _dirty_paths(plan.root, plan.git_paths())
                if dirty:
                    out("dirty in git (the apply refuses until these are clean):")
                    for line in dirty:
                        out(f"  {line}")
            return 0
        rc = _apply(plan, out, err)
    except MigrateError as exc:
        err(f"❌ {exc}")
        return 1
    if rc == 0:
        out(f"✅ work migrate {project}: {plan.summary()} applied in one commit")
    return rc


def cmd_migrate(args) -> int:
    """The `work migrate` handler: `args.project`, `args.apply`,
    `args.compat_link`. `--instance` is B.instance-floor's and is named,
    not silently ignored."""
    if getattr(args, "instance", False):
        print(
            "❌ `work migrate --instance` is not in this release yet; "
            "`work migrate <project>` migrates one registered project",
            file=sys.stderr,
        )
        return 1
    return migrate_project(
        args.project,
        apply=bool(getattr(args, "apply", False)),
        compat_link=bool(getattr(args, "compat_link", False)),
    )


def write_rel_paths(args) -> Optional[list]:
    """The work-repo-relative paths a `work migrate` invocation may write;
    None for a dry run (read-only). The write-attribution journal's scope
    (issue #233): the project dir — every move but one family stays inside
    it — plus the instance memory scope when a memory file routes there,
    plus the caller's own record when there is one. Never raises."""
    if not getattr(args, "apply", False):
        return None
    project = getattr(args, "project", None)
    rels = []
    try:
        is_short_name = core_module("registry").is_short_name
    except CoreUnavailableError:  # pragma: no cover - the apply refuses later anyway
        return rels
    root = work_repo_root()
    if is_short_name(project) and (root / project).is_dir():
        rels.append(project)
        try:
            routes = _memory_routes(root / project / LEGACY_MEMORY)
        except Exception:
            routes = []
        if any(scope == memory.INSTANCE_SCOPE for _rel, scope, _remark in routes):
            rels.append(INSTANCE_MEMORY)
    try:
        rels.extend(rel for rel in run_state.run_rel_path_candidates() if rel not in rels)
    except Exception:
        pass
    return rels
