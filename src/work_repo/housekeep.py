"""``work housekeep`` — the dry-run-first archival and index verb (#371).

The frame only (plan task 39): policy resolution, the shared project walk, the
plan model later area tasks register into, the dry-run table, and the single
``--apply`` commit. The area planners themselves land in the tasks that
follow (plans, napkin, index, runs, bundles, buckets).

Contract:

- **Dry run by default.** The table prints and nothing changes.
- **One commit.** ``--apply`` performs the moves inside ``work_mutation()``
  (``git mv`` for tracked paths) and makes ONE commit through
  :func:`work_repo.git_ops.commit_work_path` over every touched path. That
  helper pushes and refreshes each touched project's generated README, so this
  module carries no README code. A findings-only run makes no commit.
- **Tooling, not an execution.** This module never calls ``write_state``,
  ``freeze_run_dir`` or ``load_state`` (the last backs up a corrupt file,
  which is a write). It calls ``append_event`` only on the caller's own record,
  and only when a session owns the invocation.
- **Policy** is read from the work manifest's ``housekeeping:`` section
  through the shared loader; a legacy tree gets the defaults plus one INFO line.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import git_ops, run_state
from .transactions import work_mutation
from .utils import MANIFEST_NAME, core_module, load_work_manifest, work_repo_root

#: The areas the verb understands, in report order.
AREAS = ("napkin", "plans", "runs", "index", "bundles", "buckets")

#: Per-area ``archive_after`` defaults, in whole days, when the manifest does
#: not say (the spec's example gives napkin 365d and plans null; runs defaults
#: to 90d, and the listing-only areas are null/disabled).
DEFAULT_ARCHIVE_AFTER: dict[str, Optional[int]] = {
    "napkin": 365,
    "plans": None,
    "runs": 90,
    "index": None,
    "bundles": None,
    "buckets": None,
}


@dataclass(frozen=True)
class Action:
    """One planned change. ``src``/``dst`` are work-repo-relative POSIX paths."""

    kind: str          # move | retarget | tombstone | delete | info
    area: str
    src: str
    dst: Optional[str] = None
    policy: str = ""
    detail: str = ""
    date: str = ""     # tombstone date for the `archived <date>` form


@dataclass
class Plan:
    """The action table later planners append to."""

    actions: list[Action] = field(default_factory=list)

    def add(self, action: Action) -> None:
        self.actions.append(action)

    @property
    def changes(self) -> list[Action]:
        return [a for a in self.actions if a.kind != "info"]


#: Area name -> planner. Later tasks register theirs here, so this module never
#: needs to know an area's rules.
PLANNERS: dict[str, Callable[..., None]] = {}


def register(area: str):
    def _wrap(function):
        PLANNERS[area] = function
        return function
    return _wrap


@dataclass(frozen=True)
class Policy:
    """The resolved per-area TTL policy plus any refusal.

    ``refused`` names the areas whose ``archive_after`` could not be parsed:
    an invalid value refuses only ITS area, so a typo in one area does not
    stop every other area from running unattended.
    """

    archive_after: dict[str, Optional[int]]
    errors: tuple[str, ...] = ()
    legacy: bool = False
    refused: tuple[str, ...] = ()


def _parse_archive_after(raw, area: str) -> tuple[Optional[int], Optional[str]]:
    """``(days, error)`` for one ``housekeeping.<area>.archive_after`` value."""
    if raw is None:
        return None, None
    archive_after_re = core_module("checks.manifest").ARCHIVE_AFTER_RE

    if isinstance(raw, str) and archive_after_re.fullmatch(raw):
        return int(raw[:-1]), None
    return None, (
        f"housekeeping.{area}.archive_after {raw!r} is not a `<N>d` duration or "
        "null (manifest.housekeeping-policy-invalid); refusing the area"
    )


def resolve_policy(root: Path) -> Policy:
    """The effective per-area policy: manifest ``housekeeping:`` over defaults."""
    manifest = load_work_manifest(root)
    if manifest is None:
        return Policy(dict(DEFAULT_ARCHIVE_AFTER), legacy=True)
    values = dict(DEFAULT_ARCHIVE_AFTER)
    errors: list[str] = []
    refused: list[str] = []
    for area, entry in (manifest.housekeeping or {}).items():
        if area not in values:
            continue  # an unknown area is not the frame's to judge
        if not isinstance(entry, dict) or "archive_after" not in entry:
            continue
        days, error = _parse_archive_after(entry.get("archive_after"), area)
        if error:
            errors.append(error)
            refused.append(area)
            continue
        values[area] = days
    return Policy(values, tuple(errors), refused=tuple(refused))


def _registry(root: Path):
    manifest = load_work_manifest(root)
    if manifest is None:
        return None
    return core_module("registry").build_registry(manifest)


def project_dirs(root: Path, project: Optional[str] = None):
    """Every project directory from the shared walk, optionally one project.

    A host bucket is listed (kind ``BUCKET``) but never touched. ``project``
    is a registry short name or a legacy ``{host}/{project}`` path; an unknown
    value raises :class:`UnknownProject`.
    """
    walk = core_module("checks.walk")
    found = list(walk.project_dirs(root, registry=_registry(root)))
    if project is None:
        return found
    wanted = project.strip("/")
    for entry in found:
        if entry.name == wanted or entry.rel == wanted:
            return [entry]
    raise UnknownProject(project)


class UnknownProject(Exception):
    """``--project`` named no project the walk found."""


#: `.new-*` orphans older than this are swept. Long enough that a seed still
#: being written (the tmp-then-rename window is milliseconds) is never touched.
NEW_ORPHAN_AGE_SECONDS = 24 * 60 * 60

#: The run-area directory names in each tree form.
_SCHEMA_RUNS = "_runs"
_LEGACY_RUNS = "runs"


def _parse_events_strict(rdir: Path) -> Optional[list[dict]]:
    """Every event, or ``None`` when the log cannot be read/parsed in full.

    The tolerant ``read_events`` hides a torn tail; archiving must not, so an
    unparsable line means the lifecycle is UNKNOWN and the record is never
    moved.
    """
    path = rdir / "events.jsonl"
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            return None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return None
        events.append(event)
    return events


def _last_event_ts(events: list[dict]) -> Optional[str]:
    return events[-1].get("ts") if events else None


def _age_days(stamp, now: datetime) -> Optional[float]:
    if isinstance(stamp, datetime):
        when = stamp
    elif isinstance(stamp, str):
        try:
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now - when.astimezone(timezone.utc)).total_seconds() / 86400.0


def _stale_seconds() -> float:
    from .session_record import STALE_AFTER_SECONDS

    return STALE_AFTER_SECONDS


def _links_under(project_dir: Path, src_rel: str) -> list[tuple[Path, str]]:
    """``(link, sub-path)`` for dated symlinks in ``_specs``/``_plans`` whose
    target lies under *src_rel*. ``sub-path`` is the target relative to the
    moved dir, so the retarget keeps the FILE (``spec.md``), not just the dir."""
    found: list[tuple[Path, str]] = []
    old_dir = os.path.normpath(str(project_dir / src_rel))
    for index_name in ("_specs", "_plans"):
        index = project_dir / index_name
        if not index.is_dir():
            continue
        for entry in sorted(index.iterdir()):
            if not entry.is_symlink():
                continue
            target = os.path.normpath(os.path.join(str(entry.parent), os.readlink(entry)))
            if target == old_dir:
                found.append((entry, "."))
            elif target.startswith(old_dir + os.sep):
                found.append((entry, os.path.relpath(target, old_dir)))
    return found


@register("runs")
def _plan_runs(plan, *, root, project, policy, archive_stale=False, now=None, **_):
    """The runs area: archive STOPPED records past their TTL (never by label)."""
    if policy is None:
        return  # runs: {archive_after: null} disables the whole area
    from .session_record import is_stopped

    now = now or datetime.now(timezone.utc)
    # The walk does not carry the form; a manifest is the schema form (the
    # same boundary `load_work_manifest` owns everywhere else).
    schema_form = load_work_manifest(root) is not None
    if schema_form and getattr(project, "kind", None) == "bucket":
        return  # a schema-form host bucket is listed, never touched
    project_dir = project.path
    run_area = project_dir / (_SCHEMA_RUNS if schema_form else _LEGACY_RUNS)
    if not run_area.is_dir():
        return
    own = run_state.run_dir()
    own_resolved = own.resolve() if own is not None else None
    stale_days = _stale_seconds() / 86400.0
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    for rdir in run_state.iter_record_dirs(run_area):
        if own_resolved is not None and rdir.resolve() == own_resolved:
            continue  # the caller's own record is never a candidate
        state = run_state._read_sibling_state(rdir)
        events = _parse_events_strict(rdir)
        if state is None or events is None:
            continue  # unknown/unreadable lifecycle: never moved
        rel = rdir.relative_to(root).as_posix()
        last = max(
            [s for s in (state.get("updated"), _last_event_ts(events)) if s],
            default=None,
        )
        age = _age_days(last, now)
        if age is None:
            continue  # no usable timestamp: never moved

        if schema_form:
            if is_stopped(state, events):
                eligible = age > policy
            elif age > stale_days and archive_stale:
                eligible = True
            else:
                if age > stale_days:
                    plan.add(Action("info", "runs", rel, detail="possibly orphaned"))
                continue
        else:
            status = state.get("status")
            if status in ("complete", "archived"):
                eligible = age > policy
            elif run_state._is_orphaned(state, now_iso, 60):
                if archive_stale:
                    eligible = True
                else:
                    plan.add(Action("info", "runs", rel, detail="orphaned"))
                    continue
            else:
                continue  # a live owner is never moved
        if not eligible:
            continue

        if schema_form:
            slug = state.get("slug") or rdir.parent.name
            dst_rel = (project_dir / "_archive" / _SCHEMA_RUNS / slug / rdir.name)
        else:
            dst_rel = project_dir / _LEGACY_RUNS / "archive" / rdir.name
        if dst_rel.exists():
            plan.add(Action("info", "runs", rel, detail=f"destination exists: {dst_rel.relative_to(root).as_posix()}"))
            continue
        plan.add(Action("move", "runs", rel, dst_rel.relative_to(root).as_posix(), f"{policy}d"))
        for link, sub in _links_under(project_dir, rdir.relative_to(project_dir).as_posix()):
            new_target = dst_rel if sub == "." else dst_rel / sub
            plan.add(Action(
                "retarget", "runs", link.relative_to(root).as_posix(),
                new_target.relative_to(root).as_posix(),
            ))

    # The one deletion housekeep makes: `.new-*` orphans older than the threshold.
    cutoff = now.timestamp() - NEW_ORPHAN_AGE_SECONDS
    for orphan in sorted(run_area.glob(".new-*")):
        try:
            if orphan.is_dir() and orphan.stat().st_mtime < cutoff:
                plan.add(Action("delete", "runs", orphan.relative_to(root).as_posix(),
                                detail="stale .new-* orphan"))
        except OSError:
            continue


#: PLANS.md states housekeep recognises as "done". Conservative on purpose:
#: a line whose state is not in this set is never retired. Tombstone words
#: count too (the plan is already retired).
_DONE_PLAN_STATES = frozenset(
    {"done", "complete", "completed", "archived", "forgotten", "retired"}
)
_PLAN_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")


def _last_touches(root: Path, rels: Sequence[str]) -> dict[str, str]:
    """``rel -> ISO date`` of the last commit touching it, from ONE git log.

    The batched last-git-touch lookup the napkin area reuses. A path with no
    history is absent from the map (never "untouched").
    """
    if not rels:
        return {}
    try:
        proc = subprocess.run(
            ["git", "log", "--format=%cs", "--name-only", "--", *rels],
            cwd=str(root), capture_output=True, text=True, check=False,
        )
    except OSError:
        return {}
    found: dict[str, str] = {}
    date = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", line):
            date = line
            continue
        if date is not None:
            found.setdefault(line, date)  # newest commit wins
    return found


def _done_plan_names(text: str) -> set[str]:
    """The plan basenames whose PLANS.md line records a done state."""
    done: set[str] = set()
    for line in text.splitlines():
        match = _PLAN_LINK_RE.search(line)
        if not match:
            continue
        target = match.group(2).strip().split("#", 1)[0]
        tail = line[match.end():]
        state = re.findall(r"[A-Za-z]+", tail)
        if any(word.lower() in _DONE_PLAN_STATES for word in state):
            done.add(os.path.basename(target))
    return done


def _retarget_line(text: str, old_target: str, new_target: str) -> Optional[str]:
    """The index text with the line linking *old_target* re-pointed at
    *new_target* (relative to the index's own directory)."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        for match in _PLAN_LINK_RE.finditer(line):
            target = match.group(2).strip().split("#", 1)[0]
            if os.path.normpath(target) == os.path.normpath(old_target):
                start, end = match.span(2)
                lines[index] = line[:start] + new_target + line[end:]
                return "".join(lines)
    return None


def _archive_links(text: str, old_target: str, date: str) -> Optional[str]:
    """The index text with the *old_target* link replaced by `archived <date>`."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        for match in _PLAN_LINK_RE.finditer(line):
            target = match.group(2).strip().split("#", 1)[0]
            if os.path.normpath(target) == os.path.normpath(old_target):
                start, end = match.span()
                lines[index] = line[:start] + f"archived {date}" + line[end:]
                return "".join(lines)
    return None


@register("plans")
def _plan_plans(plan, *, root, project, policy, now=None, **_):
    """The plans area: retire done plan files past their TTL (schema form)."""
    if policy is None:
        return  # plans: {archive_after: null} disables the area
    if load_work_manifest(root) is None:
        return  # a legacy tree has no _plans/
    if getattr(project, "kind", None) == "bucket":
        return
    project_dir = project.path
    plans_dir = project_dir / "_plans"
    index = plans_dir / "PLANS.md"
    if not index.is_file():
        return
    try:
        text = index.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    done = _done_plan_names(text)
    files = {
        p.name: p for p in sorted(plans_dir.glob("*.md"))
        if p.name != "PLANS.md" and p.is_file()
    }
    touches = _last_touches(
        root, [p.relative_to(root).as_posix() for p in files.values()]
    )
    now = now or datetime.now(timezone.utc)
    for name, path in files.items():
        if name not in done:
            continue  # not recognisably done: never retired
        rel = path.relative_to(root).as_posix()
        touched = touches.get(rel)
        if touched is None:
            continue  # no history: never "untouched"
        age = _age_days(touched, now)
        if age is None or age <= policy:
            continue
        dst = project_dir / "_archive/_plans" / name
        if dst.exists():
            plan.add(Action("info", "plans", rel,
                            detail=f"destination exists: {dst.relative_to(root).as_posix()}"))
            continue
        plan.add(Action("move", "plans", rel, dst.relative_to(root).as_posix(), f"{policy}d"))
        plan.add(Action(
            "tombstone", "plans", index.relative_to(root).as_posix(),
            dst=os.path.relpath(dst, plans_dir), detail=name,
        ))


@register("napkin")
def _plan_napkin(plan, *, root, project, policy, now=None, **_):
    """The napkin area: decay untouched notes under `_napkin/`.

    The bare `napkin/` directory (where subdir-mode notes actually live today)
    and a separately mounted napkin repository are REPORT-ONLY: the former has
    no archive mirror at the work-repo root, and the latter's `_archive/` is at
    its own root and would need its own commit, against the one-commit rule.
    """
    if policy is None:
        return  # napkin: {archive_after: null} disables the area
    if getattr(project, "kind", None) == "bucket":
        return
    now = now or datetime.now(timezone.utc)
    napkin = root / "_napkin"
    if napkin.is_dir():
        files = [p for p in napkin.rglob("*.md") if p.is_file()]
        touches = _last_touches(
            root, [p.relative_to(root).as_posix() for p in files]
        )
        for path in files:
            rel = path.relative_to(root).as_posix()
            touched = touches.get(rel)
            if touched is None:
                continue  # no history: never decayed
            age = _age_days(touched, now)
            if age is None or age <= policy:
                continue
            dst = root / "_archive" / rel
            if dst.exists():
                plan.add(Action("info", "napkin", rel,
                                detail=f"destination exists: {dst.relative_to(root).as_posix()}"))
                continue
            plan.add(Action("move", "napkin", rel, dst.relative_to(root).as_posix(), f"{policy}d"))
    # Report-only: the bare `napkin/` directory.
    bare = root / "napkin"
    if bare.is_dir() and any(p.is_file() for p in bare.rglob("*.md")):
        plan.add(Action("info", "napkin", "napkin",
                        detail="bare napkin/ is report-only; move notes under _napkin/ to decay them"))
    # Report-only: a separately mounted napkin repository.
    mounted = os.environ.get("LMER_NAPKIN_PATH")
    if mounted:
        try:
            outside = Path(mounted).resolve() != root.resolve() and not Path(mounted).resolve().is_relative_to(root.resolve())
        except OSError:
            outside = False
        if outside:
            plan.add(Action("info", "napkin", "napkin",
                            detail="mounted napkin repository is report-only (its archive is its own commit)"))


@register("bundles")
def _plan_bundles(plan, *, root, project, policy, now=None, **_):
    """The bundles area: flatten a legacy double-nested masterplan bundle.

    Only a STOPPED record with exactly ONE nested bundle is relocated (D12);
    a live record or a multi-bundle dir is reported and left untouched, and a
    legacy run dir is never touched. `state.artifacts` names never change and
    no state or event is written.
    """
    if getattr(project, "kind", None) == "bucket":
        return
    if load_work_manifest(root) is None:
        return  # legacy run dirs are never touched
    run_area = project.path / "_runs"
    if not run_area.is_dir():
        return
    from .session_record import is_stopped

    for rdir in run_state.iter_record_dirs(run_area):
        mp = rdir / "masterplan"
        if not mp.is_dir():
            continue
        bundles = sorted(p for p in mp.iterdir() if p.is_dir())
        if not bundles:
            continue
        rel = rdir.relative_to(root).as_posix()
        if len(bundles) > 1:
            plan.add(Action("info", "bundles", rel,
                            detail=f"{len(bundles)} nested bundles; a human decides"))
            continue
        state = run_state._read_sibling_state(rdir)
        events = _parse_events_strict(rdir)
        if state is None or events is None or not is_stopped(state, events):
            plan.add(Action("info", "bundles", rel,
                            detail="record is live; bundle left untouched"))
            continue
        bundle = bundles[0]
        for child in sorted(bundle.iterdir()):
            if child.is_file() or child.is_symlink():
                plan.add(Action("move", "bundles", child.relative_to(root).as_posix(),
                                (mp / child.name).relative_to(root).as_posix()))
        # The record-root artifact links that point into the nested bundle.
        for link in sorted(rdir.iterdir()):
            if not link.is_symlink():
                continue
            target = os.path.normpath(os.path.join(str(link.parent), os.readlink(link)))
            if target.startswith(str(bundle) + os.sep):
                new = mp / os.path.relpath(target, str(bundle))
                plan.add(Action("retarget", "bundles", link.relative_to(root).as_posix(),
                                new.relative_to(root).as_posix()))
        # Every `_specs` entry that targets the nested path.
        for link, sub in _links_under(project.path, bundle.relative_to(project.path).as_posix()):
            new = mp / sub
            plan.add(Action("retarget", "bundles", link.relative_to(root).as_posix(),
                            new.relative_to(root).as_posix()))
        plan.add(Action("delete", "bundles", bundle.relative_to(root).as_posix(),
                        detail="emptied nested bundle dir"))


@register("buckets")
def _plan_buckets(plan, *, root, project, policy, now=None, **_):
    """The buckets area: list host buckets awaiting adoption (INFO only)."""
    if getattr(project, "kind", None) != "bucket":
        return
    plan.add(Action("info", "buckets", project.rel,
                    detail=f"work adopt {project.rel} <short-name>"))


@register("index")
def _plan_index(plan, *, root, project, policy, now=None, **_):
    """The index area: tombstone dangling entries in MEMORY.md and PLANS.md.

    No TTL: an entry whose file is gone is swept on every run. A generated
    tombstone reads as a non-entry line to memory.py (so its index
    regeneration keeps it) and to the validator.
    """
    if getattr(project, "kind", None) == "bucket":
        return
    from . import memory

    now = now or datetime.now(timezone.utc)
    date = now.strftime("%Y-%m-%d")
    manifest = load_work_manifest(root)
    # Instance scope (schema form only), then the project's own scope.
    if manifest is not None:
        _plan_memory_index(plan, root, root / "_agents" / "memory", manifest, date)
        _plan_memory_index(plan, root, project.path / "memory", manifest, date)
        _plan_plans_index(plan, root, project.path, date)


def _plan_memory_index(plan, root, scope_dir, manifest, date):
    from . import memory

    index = scope_dir / "MEMORY.md"
    if not index.is_file():
        return
    try:
        text = index.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    scope_rel = scope_dir.relative_to(root).as_posix()
    for line in text.splitlines():
        if memory._is_tombstone(line):
            continue
        for target in memory._entry_targets(line):
            if (scope_dir / target).exists():
                continue  # the file is present: not dangling
            key = f"{scope_rel}/{target}"
            archive_rel = memory._retirement_archive_rel(key, manifest)
            if (root / archive_rel).exists():
                new_target = os.path.relpath(root / archive_rel, scope_dir)
                plan.add(Action("tombstone", "index", index.relative_to(root).as_posix(),
                                dst=new_target, detail=target))
            else:
                plan.add(Action("tombstone", "index", index.relative_to(root).as_posix(),
                                detail=target, date=date))


def _plan_plans_index(plan, root, project_dir, date):
    index = project_dir / "_plans" / "PLANS.md"
    if not index.is_file():
        return
    try:
        text = index.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    for line in text.splitlines():
        match = _PLAN_LINK_RE.search(line)
        if not match:
            continue
        target = match.group(2).strip().split("#", 1)[0]
        if not target.lower().endswith(".md"):
            continue
        if (index.parent / target).exists():
            continue
        archive = project_dir / "_archive/_plans" / os.path.basename(target)
        if archive.exists():
            new_target = os.path.relpath(archive, index.parent)
            plan.add(Action("tombstone", "index", index.relative_to(root).as_posix(),
                            dst=new_target, detail=target))
        else:
            plan.add(Action("tombstone", "index", index.relative_to(root).as_posix(),
                            detail=target, date=date))


def build_plan(root: Path, projects, policy: Policy, areas: Sequence[str],
               *, archive_stale: bool = False, now: Optional[datetime] = None) -> Plan:
    """Run each selected area's planner over each project."""
    plan = Plan()
    for area in areas:
        if area in policy.refused:
            continue  # an invalid TTL refuses only its own area
        planner = PLANNERS.get(area)
        if planner is None:
            continue
        for entry in projects:
            planner(plan, root=root, project=entry,
                    policy=policy.archive_after.get(area), archive_stale=archive_stale,
                    now=now)
    return plan


def _print_plan(plan: Plan, *, apply: bool) -> None:
    header = "Applying" if apply else "Planned"
    if not plan.actions:
        print("Nothing to do.")
        return
    print(f"{header} {len(plan.changes)} change(s):")
    # Grouped by action kind, so a long run reads as move/retarget/delete
    # sections rather than planner order.
    for kind in ("move", "retarget", "tombstone", "delete", "info"):
        group = [a for a in plan.actions if a.kind == kind]
        if not group:
            continue
        print(f"  {kind}:")
        for action in group:
            policy = f"  [{action.policy}]" if action.policy else ""
            dst = f" -> {action.dst}" if action.dst else ""
            print(f"    {action.src}{dst}{policy}")
            if action.detail:
                print(f"              {action.detail}")


def _run_git(args: Sequence[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    return proc.returncode, (proc.stdout or proc.stderr).strip()


#: Action kind -> applier, the same registration idea as PLANNERS. An
#: unregistered kind is a hard failure, never silently dropped, so a planner
#: that emits a kind no applier handles cannot ship a dry run that says
#: "Applying" and then do nothing.
APPLIERS: dict[str, Callable[[Path, Action], bool]] = {}


def applier(kind: str):
    def _wrap(function):
        APPLIERS[kind] = function
        return function
    return _wrap


@applier("move")
def _apply_move(root: Path, action: Action) -> bool:
    if not action.dst:
        print(f"⚠️  move without a destination: {action.src}", file=sys.stderr)
        return False
    dst = root / action.dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    rc, out = _run_git(["mv", action.src, action.dst], root)
    if rc != 0:
        print(f"⚠️  git mv failed for {action.src}: {out}", file=sys.stderr)
        return False
    return True


@applier("retarget")
def _apply_retarget(root: Path, action: Action) -> bool:
    link = root / action.src
    if not link.is_symlink() or not action.dst:
        return False
    rel = os.path.relpath(root / action.dst, link.parent)
    link.unlink()
    link.symlink_to(rel)
    return True


@applier("tombstone")
def _apply_tombstone(root: Path, action: Action) -> bool:
    index = root / action.src
    if not action.detail:
        return False
    try:
        text = index.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if action.dst:
        updated = _retarget_line(text, action.detail, action.dst)
    elif action.date:
        updated = _archive_links(text, action.detail, action.date)
    else:
        return False
    if updated is None or updated == text:
        return False
    if index.name == "MEMORY.md":
        from . import memory

        with memory._memory_retirement_lock(root):
            index.write_text(updated, encoding="utf-8")
    else:
        index.write_text(updated, encoding="utf-8")
    return True


@applier("delete")
def _apply_delete(root: Path, action: Action) -> bool:
    try:
        _remove_tree(root / action.src)
    except OSError as exc:
        print(f"⚠️  could not remove {action.src}: {exc}", file=sys.stderr)
        return False
    return True


def _remove_tree(target: Path) -> None:
    if not target.exists() and not target.is_symlink():
        return
    if target.is_dir() and not target.is_symlink():
        import shutil

        shutil.rmtree(target)
    else:
        target.unlink()


def _change_line(action: Action) -> str:
    if action.kind == "move" and action.dst:
        return f"{action.src} -> {action.dst}"
    if action.kind == "retarget" and action.dst:
        return f"retarget {action.src} -> {action.dst}"
    return f"{action.kind} {action.src}"


def _apply_changes(root: Path, plan: Plan) -> tuple[list[str], list[str], list[str]]:
    """Apply every change; return ``(touched rels, lines, failures)``."""
    touched: list[str] = []
    lines: list[str] = []
    failures: list[str] = []
    for action in plan.changes:
        apply = APPLIERS.get(action.kind)
        if apply is None:
            failures.append(f"{action.kind} {action.src}")
            print(f"❌ no applier for action kind {action.kind!r} ({action.src})",
                  file=sys.stderr)
            continue
        if apply(root, action):
            touched.append(action.src)
            if action.dst:
                touched.append(action.dst)
            lines.append(_change_line(action))
        else:
            failures.append(f"{action.kind} {action.src}")
    return touched, lines, failures


def _commit_message(lines: list[str]) -> str:
    """A short subject and one line per change in the body."""
    return f"housekeep: {len(lines)} change(s)\n\n" + "\n".join(lines)


def _caller_record() -> Optional[Path]:
    """The caller's own, EXISTING record, or None from an operator shell.

    ``run_dir()`` returns the record's creation ADDRESS when no record
    exists; appending there would push a stray half-record. Reuse adopt's
    guard (which requires the state file to exist) and never raise.
    """
    try:
        from .adopt import _caller_record as _adopt_caller_record

        return _adopt_caller_record()
    except Exception:
        return None


def _record_events(caller: Optional[Path], lines: list[str]) -> Optional[str]:
    """One event per change in the caller's own record; the events rel to
    stage, or None when nothing was appended."""
    if caller is None:
        return None
    appended = False
    for line in lines:
        try:
            run_state.append_event(caller, "housekeep_move", note=line)
            appended = True
        except (run_state.RunStateError, OSError) as exc:
            print(f"⚠️  housekeep event not recorded in {caller}: {exc}", file=sys.stderr)
    if not appended:
        return None
    return str((caller / "events.jsonl").relative_to(work_repo_root()))


def cmd_housekeep(args) -> int:
    """The ``work housekeep`` entry point."""
    root = work_repo_root()
    policy = resolve_policy(root)
    areas = [args.area] if args.area else list(AREAS)
    try:
        projects = project_dirs(root, args.project)
    except UnknownProject:
        print(f"❌ no project named {args.project!r} in {root}", file=sys.stderr)
        return 2
    if policy.legacy:
        print("ℹ️  no manifest: housekeeping policy is not configurable until "
              f"the tree carries a `{MANIFEST_NAME}`; using defaults")
    for error in policy.errors:
        print(f"⚠️  {error}", file=sys.stderr)
    refused_selected = [area for area in areas if area in policy.refused]
    if getattr(args, "archive_stale", False) and os.environ.get("LMER_NONINTERACTIVE"):
        # Scheduled runs must never pass the human-only flag.
        print("❌ --archive-stale is human-only; it is refused under "
              "LMER_NONINTERACTIVE", file=sys.stderr)
        return 2

    plan = build_plan(root, projects, policy, areas,
                      archive_stale=getattr(args, "archive_stale", False))
    _print_plan(plan, apply=args.apply)
    if not args.apply or not plan.changes:
        return 2 if refused_selected else 0

    with work_mutation(root):
        # Resolve the caller's record BEFORE any move: a raising run_dir()
        # must not leave staged moves behind.
        caller = _caller_record()
        touched, lines, failures = _apply_changes(root, plan)
        rc = 0
        if touched:
            appended = _record_events(caller, lines)
            if appended is not None:
                touched.append(appended)
            rc = git_ops.commit_work_path(touched, _commit_message(lines))
        if failures:
            return 1
        if refused_selected:
            return 2
        return rc if touched else 0
