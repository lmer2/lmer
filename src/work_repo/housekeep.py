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


def _links_under(project_dir: Path, src_rel: str) -> list[Path]:
    """Dated symlinks in ``_specs``/``_plans`` whose target lies under *src_rel*."""
    found = []
    for index_name in ("_specs", "_plans"):
        index = project_dir / index_name
        if not index.is_dir():
            continue
        for entry in sorted(index.iterdir()):
            if not entry.is_symlink():
                continue
            target = os.path.normpath(os.path.join(str(entry.parent), os.readlink(entry)))
            if target == str(project_dir / src_rel) or target.startswith(
                str(project_dir / src_rel) + os.sep
            ):
                found.append(entry)
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
        for link in _links_under(project_dir, rdir.relative_to(project_dir).as_posix()):
            plan.add(Action(
                "retarget", "runs", link.relative_to(root).as_posix(),
                dst_rel.relative_to(root).as_posix(),
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
        return rc if touched else (2 if refused_selected else 0)
