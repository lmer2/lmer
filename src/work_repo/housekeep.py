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

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

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
    """The resolved per-area TTL policy plus any refusal."""

    archive_after: dict[str, Optional[int]]
    errors: tuple[str, ...] = ()
    legacy: bool = False


def _parse_archive_after(raw, area: str) -> tuple[Optional[int], Optional[str]]:
    """``(days, error)`` for one ``housekeeping.<area>.archive_after`` value."""
    if raw is None:
        return None, None
    archive_after_re = core_module("checks.manifest").ARCHIVE_AFTER_RE

    if isinstance(raw, str) and not isinstance(raw, bool) and archive_after_re.fullmatch(raw):
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
    for area, entry in (manifest.housekeeping or {}).items():
        if area not in values:
            continue  # an unknown area is not the frame's to judge
        if not isinstance(entry, dict) or "archive_after" not in entry:
            continue
        days, error = _parse_archive_after(entry.get("archive_after"), area)
        if error:
            errors.append(error)
            continue
        values[area] = days
    return Policy(values, tuple(errors))


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


def build_plan(root: Path, projects, policy: Policy, areas: Sequence[str]) -> Plan:
    """Run each selected area's planner over each project."""
    plan = Plan()
    for area in areas:
        planner = PLANNERS.get(area)
        if planner is None:
            continue
        for entry in projects:
            planner(plan, root=root, project=entry, policy=policy.archive_after.get(area))
    return plan


def _print_plan(plan: Plan, *, apply: bool) -> None:
    header = "Applying" if apply else "Planned"
    if not plan.actions:
        print("Nothing to do.")
        return
    print(f"{header} {len(plan.changes)} change(s):")
    for action in plan.actions:
        policy = f"  [{action.policy}]" if action.policy else ""
        dst = f" -> {action.dst}" if action.dst else ""
        print(f"  {action.kind:<9} {action.src}{dst}{policy}")
        if action.detail:
            print(f"            {action.detail}")


def _run_git(args: Sequence[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def _apply_moves(root: Path, plan: Plan) -> tuple[list[str], list[str]]:
    """Perform the file moves; return ``(touched rels, move lines)``."""
    touched: list[str] = []
    lines: list[str] = []
    for action in plan.changes:
        if action.kind == "move" and action.dst:
            src, dst = root / action.src, root / action.dst
            dst.parent.mkdir(parents=True, exist_ok=True)
            rc, out = _run_git(["mv", action.src, action.dst], root)
            if rc != 0:
                print(f"⚠️  git mv failed for {action.src}: {out}", file=sys.stderr)
                continue
            touched.extend([action.src, action.dst])
            lines.append(f"{action.src} -> {action.dst}")
    return touched, lines


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
    if policy.errors:
        return 2

    plan = build_plan(root, projects, policy, areas)
    _print_plan(plan, apply=args.apply)
    if not args.apply or not plan.changes:
        return 0

    with work_mutation(root):
        touched, lines = _apply_moves(root, plan)
        if not touched:
            return 0
        message = "housekeep: " + "; ".join(lines)
        # Events: inside a session, one per applied move on the CALLER's
        # record; from a shell, the lines live in the commit body above.
        rdir = run_state.run_dir()
        if rdir is not None:
            for line in lines:
                try:
                    run_state.append_event(rdir, "housekeep_move", note=line)
                except Exception:
                    pass
            events = rdir / "events.jsonl"
            touched.append(str(events.relative_to(root)))
        rc = git_ops.commit_work_path(touched, message)
    return rc
