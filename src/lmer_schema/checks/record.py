"""The record area — state files, the session unit and the run areas
(spec §3.5, D22, D12; SCHEMA.md "record", "Sessions and runs", "Session
directory").

The record is what a launch leaves behind: the schema-1 state files of
`docs/RUN-STATE.md <../../../docs/RUN-STATE.md>`_ — ``state.yaml``,
``events.jsonl``, ``ledger.yaml``, ``plan.index.json`` and friends. Schema 1
changes their LOCATION only, so this module checks two things that live at
different levels:

1. **file shapes**, which are RUN-STATE.md's and are checked identically in
   every form. D14 downgrades integrity findings in a legacy tree, never
   shape findings: a corrupt ``state.yaml`` is broken wherever it lives, so
   no code in the ``record.*`` namespace is registered with a ``legacy=``
   severity, and ``tests/test_lmer_schema_findings.py`` pins that as a whole
   ("the record namespace must carry the state-file shape codes… none
   tier-sensitive"). The four shape codes are pre-registered in
   :mod:`lmer_schema.checks.findings` (the D14 table) and CONSUMED here;
2. **the session unit** (D22), which is a layout question.

Layout, not form
================
The one structural decision of this module: the run area's DIRECTORY NAME
decides which layout applies, not the tree's form.

- ``<project>/_runs/`` is the session-unit layout: ``_runs/<slug>/`` is a
  grouping level where nothing is written, and each child of it is one
  launch's complete record at ``<session-id>[--<name>]/``.
- bare ``<project>/runs/`` is the legacy shared run dir: ``runs/<slug>/`` IS
  the record. It is read in place and never restructured (D22, §3.5) — no
  grouping-level warnings, no session-id nudges, no orphan notion (several
  sessions interleave in one dir, so "this dir is stale" is not a statement
  about a session).

Keying on the name rather than on ``Report.form`` is what makes co-existence
(D16/D20) work in both directions: a host bucket inside a schema-form tree
carries ``_runs/`` and gets the session-unit checks, while a legacy ``runs/``
tree that survives inside a migrated plexus keeps being read exactly as
before. The severity of every finding is still the registry's — the form
never enters this module's decisions at all.

The walk
========
:func:`iter_run_areas` finds the record areas by name at any depth (a project
dir sits at the root, a host bucket's sits below its host segments — the
!254 "no depth rule" decision), and stops at the first one: a ``runs/`` name
INSIDE a record dir is a run's own content (a checkout, a bundle), not a
second area. It skips ``.git``/``node_modules``/``__pycache__``, dot-dirs,
``_archive/`` (SCHEMA.md: archived contents are not re-validated, D13) and
symlinked directories — the §9 compat symlink would otherwise walk a project
twice, and the target is walked at its real location anyway.

Where the walk stops: the opaque and free-form tiers
====================================================
A record area is a thing INSIDE a project tree, and this walk matches a
directory NAME rather than resolving projects — so on its own it would enter
the tiers the Inventory marks **opaque** (``_agents/mirrors/`` contents,
``<scope>/<harness>/``, ``_taskdef/``) and **free-form** (``_knowledge/``,
``_napkin/``) and report ERRORs about state files belonging to writers this
schema does not govern. A vendored fork under ``_agents/mirrors/`` that ships
run fixtures of its own is the reachable case: the plexus is healthy, the
findings are unfixable, and ``validate`` exits 1. The sibling modules already
draw this boundary (:mod:`~lmer_schema.checks.agents` never descends into
``mirrors/`` or a harness dir), so this module draws the same one and reports
nothing inside those trees. Nothing is lost: no Inventory row puts a record
area inside any of them, and the one machine-owned thing they hold
(``<scope>/memory/``) is the agents module's, not the record's.

Two prune rules, because two kinds of name are involved:

- :data:`OPAQUE_AREAS` — the schema-owned ``_``-prefixed names whose contents
  this module must never enter, whether the Inventory calls them *opaque*
  (``_agents``, ``_taskdef``) or *free-form* (``_info``, ``_knowledge``,
  ``_napkin``, ``_plans``: "never checked"). Both tiers mean the same thing
  to a record walk — the bytes in there belong to a writer this schema does
  not govern — so they share one set. They are pruned by NAME AT ANY DEPTH. ``_*`` is reserved for the schema (D6) and a project
  short name can never start with ``_`` (§1 charset), so one of these names
  is that area wherever it turns up. The prune is by exact name and NOT by
  the ``_`` prefix: a forge group called ``_ns`` is a namespace level, and a
  bucket project at ``{host}/_ns/proj/_runs/`` is a real record.
- :data:`LEGACY_INSTANCE_AREAS` / :data:`LEGACY_PROJECT_AREAS` — the bare
  legacy twins (§9, Appendix A) — are pruned only WHERE THEY CAN BE AN AREA,
  because every one of them is also a legal project short name and a legal
  host-bucket path component (``{host}/mirrors/lmer`` is an ordinary remote).
  The instance surfaces are pruned at the plexus root only, and only when the
  directory is not itself project-shaped — a project named ``mirrors`` is a
  project, whatever it is called (the §1 marker test, the same discriminator
  :mod:`~lmer_schema.checks.layout` uses). ``taskdef`` is pruned directly
  inside a project-shaped directory, which is the only place
  ``<project>/taskdef/`` can be the project-scoped override. Bare ``memory``
  is deliberately NOT pruned: it is machine-owned, not opaque.

A third prune is not about a name at all. The legacy
``{host}/{project}/{task_type}/{target}/`` tree — where ``work log`` and
``work report`` wrote before the run dir unified them — is a §7 scope-out
("read-side fallback only; informational"), and ``{task_type}`` is an OPEN set
(any ``LMER_TASK`` value), so there is no name to prune by and none that would
be safe to prune: every one of them is a legal project short name too.
:func:`_is_legacy_task_tree` recognises it by POSITION and by SHAPE instead,
one per level (issue #379, which is what stops its reproduction: a tree whose
only findings, and whose exit 1, came out of a scope-out):

- ``{task_type}/`` sits DIRECTLY INSIDE A PROJECT. Which directories are
  projects is the layout walk's answer, and the runner hands that list down
  (``projects=``, the same list every per-project module gets) so this walk
  does not rediscover it: a listed directory is a project by its entry,
  before it has grown a single §1 marker. Where the list is silent — no list
  (a bare root, :func:`lmer_schema.migration._record_blockers`), or a nested
  project the walk's own rule never names (nothing below a project is a
  second project; the live ``syseng/infra/ansible-roles/*/runs/`` shape) —
  the §1 marker test answers, as it did before the list arrived. The list
  adds a project; it never becomes a whitelist. The root is never one.
- ``{target}/`` is recognised by what the legacy writers LEFT in it
  (:data:`LEGACY_TARGET_MARKERS`). No position can do this job: a
  ``{target}`` holding only a run area is, directory for directory, a nested
  project holding only a run area, and the list cannot name either.

Severity choices, and where they come from
==========================================
- ``record.state-unparseable`` covers "cannot be read as the document
  RUN-STATE.md describes": unreadable file, broken YAML/JSON, a top-level
  that is not a mapping, and a ``tasks:`` section or ledger row of the wrong
  type (which the writer itself counts as corrupt,
  ``work_repo.run_state.load_ledger``).
- ``record.state-schema`` is the ``schema:`` int rule, bool included (the
  ``isinstance(True, int)`` trap the manifest loader and the taskdef reader
  guard the same way). Absent is NOT a finding on the YAML state files —
  their single writer reads a missing ``schema:`` as ``0`` and RUN-STATE.md
  makes added keys additive — but IS one on ``plan.index.json``, whose own
  linter (``work_repo.plan_index.parse_plan_index``) requires the integer.
  The validator is deliberately no stricter than each file's writer.
- ``record.state-enum`` covers the two CLOSED enums RUN-STATE.md names:
  ``status`` ("`status` stays the closed three-value enum") and a ledger
  row's ``status``. ``stop_reason`` is explicitly extensible there
  ("extending `stop_reason` with `aborted` is additive on the same terms"),
  so an unknown one is not a finding. An ABSENT enum is not a finding
  either: the writer tolerates it, and a missing key is not a wrong value.
- The session-unit codes are warnings because none of them makes the record
  unreadable, and the staleness code is informational because "in-progress
  with nobody home" is a state a human resolves (`work sessions` lists it),
  never an error (§3.5, D22).

Masterplan bundles are opaque (D12): a bundle is checked for existence and
for ONE thing beyond it — the pre-D12 nested ``masterplan/<mp-slug>/`` shape,
reported as informational so a migration can find it. Nothing inside a
bundle is validated, which is why the walk of a record dir never recurses
into ``masterplan/``.

The artifact links at the record root
=====================================
The bundle is opaque; the links that SURFACE it are not. SCHEMA.md's
``<session-dir>/<artifact>.md -> masterplan/<artifact>.md`` row is machine
tier and says of every well-known artifact (:data:`MASTERPLAN_ARTIFACTS`,
the writer's own list) that the link is relative and that **dangling is an
error**. This module asks that here because it owns the walk of the record
area: ``index_links`` deliberately never enters a session directory (a
foreign writer's tree, D12), so a row nobody else reaches was a writer with
no reader (!261 discussion 1 — the links validated clean in every form).

Three decisions, none of them re-litigated at the call site:

- **The rule is not restated.** "Relative, inside the plexus, resolves" is
  :mod:`lmer_schema.checks.symlinks`' — the support module both this module
  and ``index_links`` consume, because a check module never imports a check
  module and a second copy of a rule is a rule that drifts.
- **The codes are the index mechanic's** (``index.link-not-relative``,
  ``index.link-escapes``, ``index.dangling-link``), consumed from the D14
  table like the shape codes above. It is the same promise about the same
  kind of link; a near-duplicate ``record.*`` code would split one contract
  in two and make the JSON consumers key on where a link happens to live.
- **Both layouts, and the tier comes with the code.** The same tooling
  writes these links into a legacy ``runs/<slug>/`` dir, so they are checked
  there too — at the severities D14 gives them, which is exactly what the
  row wants: a dangling link is an integrity finding (ERROR in schema form,
  WARN under the legacy tier, where the validator is a migration aid and not
  a gate), while an absolute or escaping target is a SHAPE violation of the
  link mechanic and stays an ERROR in every form. So this is the one place
  the module emits a tier-sensitive code — it belongs to the index mechanic,
  not to the record's file shapes, which never soften — and it still decides
  no severity itself: the form reaches it through the report, including the
  runner's host-bucket pinning.

Only the SYMLINK case is this row's. A real ``spec.md`` at the record root
is the free-form content shape SCHEMA.md gives the same name, and a link
under any other name (another registered artifact, a legacy multi-bundle
``<mp-slug>-<name>`` link) is outside the well-known list and outside this
row.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import yaml

from lmer_schema import manifest as schema_manifest
from lmer_schema.checks.findings import Report, Severity, Tier, register
from lmer_schema.checks import symlinks, walk
from lmer_schema.registry import PROJECT_MARKERS, is_host_bucket_name, is_project_shaped

__all__ = [
    "EVENTS_FILE",
    "LEDGER_FILE",
    "LEGACY_INSTANCE_AREAS",
    "LEGACY_LAYOUT",
    "LEGACY_PROJECT_AREAS",
    "LEGACY_RUNS_DIR",
    "LEGACY_TARGET_MARKERS",
    "MASTERPLAN_ARTIFACTS",
    "MASTERPLAN_DIR",
    "OPAQUE_AREAS",
    "PLAN_INDEX_FILE",
    "RUNS_DIR",
    "RUN_STATUSES",
    "SEED_DIR_RE",
    "SESSION_DIR_RE",
    "SESSION_ID_RE",
    "SESSION_LAYOUT",
    "STALE_AFTER_SECONDS",
    "STATE_FILES",
    "TASK_STATUSES",
    "check_record",
    "iter_run_areas",
    "plain_session_move_text",
]

#: The session-unit run area (D22) and its legacy, shared-dir twin (§3.5).
RUNS_DIR = "_runs"
LEGACY_RUNS_DIR = "runs"

#: :func:`iter_run_areas` layout tags — which shape the area's children have.
SESSION_LAYOUT = "session"
LEGACY_LAYOUT = "legacy"

#: The ``LMER_SESSION_ID`` form (``libexec/harness-common.sh``: UTC
#: ``%Y%m%d-%H%M%S`` + pid + random), as SCHEMA.md states it.
SESSION_ID_RE = re.compile(r"(?:\d{8}-\d{6}-\d+-\d+|[0-9a-f]{32})")

#: A session DIRECTORY: the id, plus the ``--<name>`` suffix the freeze
#: rename appends once a session is named. The name half is deliberately
#: unconstrained — no document gives it a charset.
SESSION_DIR_RE = re.compile(SESSION_ID_RE.pattern + r"(?:--[^/]+)?")

#: The transient seed dir of the run-dir lifecycle (RUN-STATE.md §1):
#: ``.new-<session-id>-*``, moved to its canonical name by one atomic
#: rename. One left behind is a crash's litter, swept by housekeeping.
SEED_DIR_RE = re.compile(r"\.new-.+")

#: The state file, newest name first: ``state.yml`` is read only when
#: ``state.yaml`` is absent (RUN-STATE.md's lazy migration), and the
#: ``state.yml.migrated`` leftover is out of the resolution path entirely.
STATE_FILES = ("state.yaml", "state.yml")

EVENTS_FILE = "events.jsonl"
LEDGER_FILE = "ledger.yaml"
PLAN_INDEX_FILE = "plan.index.json"

#: The closed run-status enum (RUN-STATE.md §2; the writer's copy is
#: ``work_repo.run_state.STATUSES``).
RUN_STATUSES = ("in-progress", "complete", "archived")

#: The closed ledger task-status enum (RUN-STATE.md, ``ledger.yaml``).
TASK_STATUSES = ("pending", "in-progress", "done", "deferred", "dropped")

#: The masterplan bundle (D12) and the artifacts that identify one. A dir
#: holding any of them is a bundle root; that is the whole of what this
#: module knows about bundle internals. The same five names are the
#: WELL-KNOWN list of SCHEMA.md's ``<session-dir>/<artifact>.md`` row — the
#: names the sync links at the record root — and the writer's own tuple
#: (``work_repo.run_state.MASTERPLAN_ARTIFACTS``, pinned equal by the tests).
MASTERPLAN_DIR = "masterplan"
MASTERPLAN_ARTIFACTS = ("spec.md", "goals.md", "plan.md", "plan.html", "retro.md")

#: What each fault of a record-root artifact link is called and how it reads
#: (module docstring: the codes are the index mechanic's, consumed from the
#: D14 table, and the tier comes with them).
_ARTIFACT_LINK_FINDINGS = {
    symlinks.NOT_RELATIVE: (
        "index.link-not-relative",
        "artifact symlink target is absolute; a link to the bundle beside it "
        "must be relative",
    ),
    symlinks.ESCAPES: (
        "index.link-escapes",
        "artifact symlink resolves outside the plexus root",
    ),
    symlinks.DANGLING: (
        "index.dangling-link",
        "artifact symlink target does not exist",
    ),
}

#: How long an ``in-progress`` session may go untouched before it is called
#: possibly orphaned. Two weeks: long enough that a paused-and-resumed run
#: (the normal multi-day rhythm) is never called dead, short enough that a
#: session whose harness died is surfaced while its context is still worth
#: resuming. It is INFO either way, and a parameter of :func:`check_record`
#: so a caller with a different rhythm can say so.
STALE_AFTER_SECONDS = 14 * 24 * 60 * 60

#: Never walked: VCS/tool dirs and the archive mirrors (D13 — archived
#: contents are not re-validated).
_SKIP_DIR_NAMES = frozenset({".git", "node_modules", "__pycache__", "_archive"})

#: The schema-owned areas the Inventory marks opaque or free-form, at either
#: scope. Pruned by name at any depth (module docstring): a foreign writer's
#: or a human's tree holds no record area of this plexus.
OPAQUE_AREAS = frozenset(
    {"_agents", "_info", "_knowledge", "_napkin", "_plans", "_taskdef"}
)

#: Their bare legacy twins at the INSTANCE ROOT (§9, Appendix A; the same
#: names ``layout.RECOGNIZED_LEGACY_NAMES`` and
#: ``agents.LEGACY_INSTANCE_SURFACES`` recognise). Pruned only there, and
#: only when the directory is not itself project-shaped.
LEGACY_INSTANCE_AREAS = frozenset(
    {"agent-files", "knowledge", "mirrors", "napkin", "taskdef"}
)

#: The bare legacy twins INSIDE a project: ``<project>/taskdef/`` is the
#: project-scoped taskdef override (opaque) and ``<project>/info/`` is the
#: legacy twin of ``_info/`` (free-form). Bare ``memory`` is machine-owned
#: (the agents module walks it) and is never pruned here.
LEGACY_PROJECT_AREAS = frozenset({"info", "taskdef"})

#: What the pre-run-dir WRITERS left in a legacy ``{target}/`` directory, and
#: so the only thing that MAKES a directory one: ``work log``'s ``log.yaml``
#: and ``work report``'s ``reports/`` (RUN-STATE.md "Legacy layout fallback").
#: A ``{target}`` name carries no marker of its own — it is a sanitized
#: branch/issue/SHA — so writer output is all there is to recognise it by.
#:
#: A run area (``runs``/``_runs``) is deliberately NOT in this set, and that
#: exclusion is the whole safety of this prune. ``runs`` is also a §1 PROJECT
#: marker, so "holds a run area" is exactly what makes a directory a target
#: AND exactly what makes it a project — and a project's name is a repo name,
#: the same open set this module already refuses to prune ``{task_type}`` by.
#: Including it pruned nested projects: for example,
#: ``example/infra/roles/{web,common}/runs/`` would leave validation when
#: ``example/infra`` gained one marker (!271 review). Positive writer evidence cannot
#: collide that way: nothing but ``work log`` and ``work report`` puts these
#: here.
#:
#: The cost, taken deliberately: a ``{target}/`` carrying ONLY a run area and
#: no writer output is read as a project and walked. That is the half of issue
#: #379's reproduction no shipped tool produces (``work log`` wrote
#: ``log.yaml`` beside it in every real legacy target — verified against every
#: one in the live tree), and under-pruning costs a scope-out being checked
#: while over-pruning costs a real project's records never being checked at
#: all. Only one of those is silent. The project list the runner hands down
#: (#379's positional half) does not change this: it names projects, never
#: what sits below one, and ``<project>/<x>/<y>/runs`` with nothing else in
#: ``<y>`` is the nested-project shape exactly.
LEGACY_TARGET_MARKERS = frozenset({"log.yaml", "reports"})

#: Names a ``{target}/`` can never be: every schema and legacy AREA name.
#: Without this a ``runs``-shaped directory dropped inside a free-form area
#: (``<project>/_info/pages/runs/``) would read as a target, make the whole
#: PROJECT look like a task tree, and prune away its real record.
_NEVER_A_TARGET = (
    frozenset(PROJECT_MARKERS)
    | OPAQUE_AREAS
    | LEGACY_INSTANCE_AREAS
    | LEGACY_PROJECT_AREAS
    | frozenset({"_archive"})
)

#: The legacy archive home inside a legacy run area (D13, RUN-STATE.md §6).
_LEGACY_ARCHIVE_DIR = "archive"

#: How much of an offending line a finding's ``detail`` quotes.
_DETAIL_LIMIT = 120


# ---------------------------------------------------------------------------
# Codes owned by this module (the shape codes live in findings.py's D14
# table and are consumed, not re-registered — registering twice raises).
# ---------------------------------------------------------------------------

register(
    "record.session-metadata",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary="schema-2 session metadata must be a mapping with the record's UID",
)
register(
    "record.release-claim",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary="schema-2 shared release claim cannot be safely interpreted",
)
register(
    "record.grouping-level-entry",
    Severity.WARN,
    tier=Tier.MACHINE,
    summary=(
        "a file or a non-session directory at a `_runs/` grouping level, "
        "where nothing is written (D22)"
    ),
)
register(
    "record.session-dir-without-state",
    Severity.WARN,
    tier=Tier.MACHINE,
    summary="a session directory with no `state.yaml` (a session SHOULD carry one)",
)
register(
    "record.session-id-malformed",
    Severity.WARN,
    tier=Tier.MACHINE,
    summary=(
        "a session directory whose name is not `<session-id>[--<name>]` in the "
        "`LMER_SESSION_ID` form"
    ),
)
register(
    "record.session-stale",
    Severity.INFO,
    tier=Tier.MACHINE,
    summary=(
        "an `in-progress` session untouched for longer than the staleness "
        "window — possibly orphaned, resumable, never auto-ended"
    ),
)
register(
    "record.seed-dir-leftover",
    Severity.WARN,
    tier=Tier.MACHINE,
    summary=(
        "a `.new-<session-id>-*` seed directory left behind by a crash "
        "(transient state the cleaner sweeps)"
    ),
)
register(
    "record.masterplan-nested",
    Severity.INFO,
    tier=Tier.OPAQUE,
    summary=(
        "a masterplan bundle in the pre-D12 nested `masterplan/<mp-slug>/` "
        "shape; reported for migration, never an error"
    ),
)
register(
    "record.plain-session-slug",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary=(
        "a pre-A1 plain-session leftover: the slug is the obsolete derivation "
        "of the record's own target; `work migrate <project>` folds it"
    ),
)


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


def iter_run_areas(root, *, projects=None) -> Iterator[Tuple[Path, str]]:
    """Yield ``(path, layout)`` for every run area under *root*.

    *layout* is :data:`SESSION_LAYOUT` for a ``_runs/`` area and
    :data:`LEGACY_LAYOUT` for a bare ``runs/`` one. Depth-first in name
    order; see the module docstring for what is skipped and why the walk
    stops at an area rather than descending through it — in particular the
    opaque and free-form tiers, which are never entered and never reported.

    *projects* is the layout walk's project list (anything ``Path()`` accepts,
    the runner's :class:`~lmer_schema.checks.walk.ProjectDir` included) or
    ``None``. It never changes where run areas are looked for — the walk is
    by name at any depth either way — only where the prunes that ask "is
    this directory a project" take their answer from: the list where it
    names a directory, the §1 marker test everywhere else (module
    docstring). ``None`` is the marker test alone, today's answer on a bare
    root.
    """
    root = Path(root)
    known = None if projects is None else frozenset(Path(project) for project in projects)
    stack: List[Path] = [root]
    while stack:
        current = stack.pop()
        descend: List[Path] = []
        for child in _children(current):
            name = child.name
            if child.is_symlink() or not child.is_dir():
                continue
            if name.startswith(".") or name in _SKIP_DIR_NAMES:
                continue
            if name == RUNS_DIR:
                yield child, SESSION_LAYOUT
            elif name == LEGACY_RUNS_DIR:
                yield child, LEGACY_LAYOUT
            elif not _is_scope_out(child, parent=current, root=root, projects=known):
                descend.append(child)
        # Reversed, because the stack pops last-in first: keeps the yield
        # order the name order an operator would `ls`.
        stack.extend(reversed(descend))


def _is_scope_out(child: Path, *, parent: Path, root: Path, projects) -> bool:
    """Whether the walk stops at *child* — the module docstring's prune rules,
    in one decision: an opaque or free-form AREA, or a legacy
    ``{task_type}/`` level. *projects* is :func:`iter_run_areas`'s list,
    already a set of paths, or ``None``."""
    return _is_opaque(
        child, parent=parent, root=root, projects=projects
    ) or _is_legacy_task_tree(child, parent=parent, root=root, projects=projects)


def _is_project(path: Path, *, root: Path, projects) -> bool:
    """Whether *path* is a project directory — the ONE answer every prune
    here that asks takes.

    Positional first: a directory the layout walk LISTED is a project by its
    entry, shaped or not (the runner hands that list down as *projects*).
    Shape second: the §1 marker test, which is the whole answer when there is
    no list and the only answer for what the list never names — a nested
    project, since nothing below a project is a second project to the walk.
    The plexus ROOT is never a project, and it has to be said: a schema-form
    root holds ``_agents/`` and ``_taskdef/``, so the marker test calls it
    project-shaped, and every project would otherwise sit where a
    ``{task_type}`` sits. No legacy task tree lives there anyway — the layout
    starts at ``{host}/{project}/``.
    """
    if path == root:
        return False
    if projects is not None and path in projects:
        return True
    return is_project_shaped(path)


def _is_opaque(child: Path, *, parent: Path, root: Path, projects) -> bool:
    """Whether *child* is an opaque or free-form area, so the walk stops.

    The two prune rules of the module docstring: a schema-owned ``_`` name is
    that area at any depth, while a bare legacy name is only an area where it
    CAN be one — at the plexus root for the instance surfaces (and not when
    the directory is itself a project), directly inside a project for
    ``taskdef``. "A project" is :func:`_is_project`'s answer.
    """
    name = child.name
    if name in OPAQUE_AREAS:
        return True
    if parent == root:
        return name in LEGACY_INSTANCE_AREAS and not _is_project(
            child, root=root, projects=projects
        )
    return name in LEGACY_PROJECT_AREAS and _is_project(parent, root=root, projects=projects)


def _is_legacy_task_tree(child: Path, *, parent: Path, root: Path, projects) -> bool:
    """Whether *child* is a legacy ``{task_type}/`` level, whose contents §7
    puts out of scope (issue #379).

    ``{host}/{project}/{task_type}/{target}/`` is where ``work log`` and
    ``work report`` wrote before the run dir unified them; SCHEMA.md gives it
    "read-side fallback only; informational" and §7 lists it beside the napkin
    and the bundle internals. The walk finds run areas by NAME at any depth,
    so without this it descended into one and ERRORed on every state file it
    found there — a tree whose only findings come out of a scope-out and whose
    ``validate`` exits 1 on them.

    There is no name to prune by: ``{task_type}`` is whatever ``LMER_TASK``
    said (``develop``, ``review``, any taskdef name), an open set that a legal
    project short name is drawn from too. So the rule is two levels, both
    required, each answered its own way:

    - *child* sits DIRECTLY inside a project — :func:`_is_project`: the
      layout walk's list where it names *parent* (the runner's path), the §1
      marker test where it does not. A run area sitting directly inside a
      project is not this layout and stays checked. The marker half costs
      nothing to satisfy on a real legacy tree: the writer that made these
      dirs made ``{host}/{project}/info/`` in the same breath
      (``ensure_work_repo_directory``), which is a §1 marker; what the list
      adds is the project the registry names before it has grown one.
    - at least one of *child*'s children is a legacy ``{target}`` —
      :func:`_is_legacy_target`, by what the legacy writers left there. The
      list has nothing to say at this level and must not: a ``{target}``
      holding only a run area and a nested project holding only a run area
      are the same directory to it (:data:`LEGACY_TARGET_MARKERS`).
    """
    if not _is_project(parent, root=root, projects=projects):
        return False
    return any(_is_legacy_target(entry) for entry in _children(child))


def _is_legacy_target(path: Path) -> bool:
    """Whether *path* is a legacy ``{target}/`` directory: a directory that is
    not an area, not a symlink, and carries one of
    :data:`LEGACY_TARGET_MARKERS`."""
    if path.name in _NEVER_A_TARGET or path.is_symlink() or not path.is_dir():
        return False
    return any((path / marker).exists() for marker in LEGACY_TARGET_MARKERS)


def plain_session_move_text(slug, taskdef, *, area=RUNS_DIR, group=None) -> str:
    """The §7 move text for a plain-session leftover, shared verbatim.

    The validator check and the ``lmer-schema migrate`` blocker both carry
    this one string (B.port-check/B.port-blocker), so the finding and the
    refusal cannot drift on what to do or how. It names the move and the
    ONE command that repairs it. *group* is the directory's on-disk name
    (default *slug*); in a legacy ``runs/`` area that directory IS the
    record, so it moves whole under a session id rather than per ``<uid>``.
    """
    group = slug if group is None else group
    if area == LEGACY_RUNS_DIR:
        move = f"`{area}/{group}/ \u2192 {RUNS_DIR}/{taskdef}/<session-id>/`"
    else:
        move = f"`{area}/{group}/<uid> \u2192 {RUNS_DIR}/{taskdef}/<uid>`"
    return (
        f"`{area}/{group}/` is a pre-A1 plain-session leftover: its `slug:` is "
        f"the obsolete derivation of its own target. Fold it into the bare "
        f"taskdef group ({move}, "
        f"`slug: {slug} \u2192 {taskdef}`) with `work migrate <project>`."
    )


def check_record(root, report: Report, *, now=None, stale_after=STALE_AFTER_SECONDS,
                 manifest=..., projects=None) -> None:
    """Run every record-area check over the plexus at *root* into *report*.

    *now* is the instant staleness is measured against (default: UTC now),
    injected so a test's verdict does not depend on the wall clock;
    *stale_after* is that window in seconds (:data:`STALE_AFTER_SECONDS`).
    *projects* is the layout walk's project list when the caller has one (the
    runner does; ``None`` discovers it here for the schema-2 claim check and
    leaves the run-area walk to the §1 marker test, :func:`iter_run_areas`).
    A root that does not exist yields nothing — "is there a tree at all" is
    the runner's question, not a record finding.
    """
    # deferred: import cost — work_repo (~35 ms: git_ops, clone_and_exec) stays off the lmer-schema import; the direction is work_repo -> lmer_schema
    from work_repo.release_claims import read_claim
    from work_repo.run_state import RunStateError

    root = Path(root)
    now = _utc(now) if now is not None else datetime.now(timezone.utc)
    if manifest is ...:
        try:
            manifest = schema_manifest.load_manifest(root)
        except schema_manifest.ManifestError:
            manifest = None  # the manifest stage already reports the exact cause
    if manifest is not None and manifest.schema >= 2:
        for project in walk.project_dirs(root) if projects is None else projects:
            path = Path(project) / "_claims" / "release.yaml"
            try:
                read_claim(path)
            except RunStateError:
                report.add("record.release-claim", _rel(root, path),
                           "invalid shared release claim; arbitration must refuse")
    # `record.plain-session-slug` is evaluated only in schema form: a legacy
    # tree (no manifest) keeps validating green and folds its leftovers at
    # adoption, inside `work migrate` (spec §7, review S3). A host bucket is
    # skipped inside the check itself — a bucket never blocks (D9) and this
    # `record.*` code has no legacy downgrade.
    plain_session = manifest is not None
    for area, layout in iter_run_areas(root, projects=projects):
        if layout == SESSION_LAYOUT:
            _check_session_area(
                root, area, report, now=now, stale_after=stale_after,
                plain_session=plain_session,
            )
        else:
            _check_legacy_area(root, area, report, plain_session=plain_session)


# ---------------------------------------------------------------------------
# The two layouts
# ---------------------------------------------------------------------------


def _check_session_area(root, area, report, *, now, stale_after, plain_session) -> None:
    """``<project>/_runs/`` — grouping dirs only, one per (taskdef, target)."""
    for child in _children(area):
        name = child.name
        if name.startswith("."):
            _check_seed_dir(root, child, report)
            continue
        if child.is_symlink() or not child.is_dir():
            report.add(
                "record.grouping-level-entry",
                _rel(root, child),
                f"`{RUNS_DIR}/` holds `<slug>/` grouping directories only; "
                f"`{name}` is not one (D22: nothing is written at this level)",
            )
            continue
        _check_slug_group(
            root, child, report, now=now, stale_after=stale_after,
            plain_session=plain_session,
        )


def _check_slug_group(root, group, report, *, now, stale_after, plain_session) -> None:
    """``_runs/<slug>/`` — the grouping level: session dirs and nothing else."""
    for child in _children(group):
        name = child.name
        if name.startswith("."):
            # Dot entries are never matched by the run resolver; they are
            # nobody's record and saying so adds no information.
            continue
        in_form = SESSION_DIR_RE.fullmatch(name) is not None
        if child.is_symlink() or not child.is_dir():
            report.add(
                "record.grouping-level-entry",
                _rel(root, child),
                f"nothing is written at the `{RUNS_DIR}/<slug>/` grouping level "
                f"(D22); `{name}` is a file, not a session directory",
            )
            continue
        state = _state_file(child)
        if state is None and not in_form:
            report.add(
                "record.grouping-level-entry",
                _rel(root, child),
                f"`{name}` is neither a session directory nor anything that "
                f"belongs at the `{RUNS_DIR}/<slug>/` grouping level (D22)",
            )
            continue
        if not in_form:
            report.add(
                "record.session-id-malformed",
                _rel(root, child),
                f"session directory `{name}` is not `<session-id>[--<name>]` "
                f"(`{SESSION_ID_RE.pattern}`, the `LMER_SESSION_ID` form)",
            )
        elif state is None:
            report.add(
                "record.session-dir-without-state",
                _rel(root, child),
                f"session directory `{name}` holds no `{STATE_FILES[0]}`; a "
                "session's record starts with its state file",
            )
        _check_record_dir(
            root, child, report, layout=SESSION_LAYOUT, now=now, stale_after=stale_after
        )
    if plain_session:
        _check_plain_session_group(root, group, report, area=RUNS_DIR)


def _check_legacy_area(root, area, report, *, plain_session) -> None:
    """``<project>/runs/`` — one shared dir per target, read in place (§3.5)."""
    for child in _children(area):
        name = child.name
        if name.startswith("."):
            _check_seed_dir(root, child, report)
            continue
        if name == _LEGACY_ARCHIVE_DIR:
            continue  # the legacy archive home; archived records are not re-validated
        if child.is_symlink() or not child.is_dir():
            continue  # a legacy tree is read as it is, never tidied
        if _state_file(child) is None:
            continue  # not a run dir: the resolver only matches state files
        _check_record_dir(root, child, report, layout=LEGACY_LAYOUT, now=None, stale_after=None)
        if plain_session:
            _check_plain_session_group(root, child, report, area=LEGACY_RUNS_DIR)


def _check_plain_session_group(root, group, report, *, area) -> None:
    """One `record.plain-session-slug` finding when *group* holds a pre-A1
    plain-session record (spec §7, check `record.plain-session-slug`).

    The predicate is `run_state.is_legacy_plain_session_slug`, the ONE shared
    rule the migrate blocker and the fold use; this check never re-derives it.
    A host bucket is skipped: D9 says a bucket never blocks, and a `record.*`
    code carries no legacy downgrade, so the runner's tier pin could not
    soften it. A group in a bare legacy `runs/` area inside a schema-form tree
    is still checked — `work migrate <project>` nests and folds it — but a
    legacy TREE is never walked here (`plain_session` is off without a
    manifest).
    """
    if is_host_bucket_name(_rel(root, group).split("/", 1)[0]):
        return  # D9: a bucket never blocks
    # deferred: import cost — only a group outside a host bucket needs it; work_repo (~35 ms) stays off the lmer-schema import
    from work_repo.run_state import is_legacy_plain_session_slug

    candidates = []
    if _state_file(group) is not None:
        candidates.append(group)
    candidates.extend(child for child in _children(group) if _state_file(child) is not None)
    for rdir in candidates:
        try:
            data = yaml.safe_load(_state_file(rdir).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue  # the shape codes already report an unreadable state
        if is_legacy_plain_session_slug(data):
            report.add(
                "record.plain-session-slug",
                _rel(root, group),
                plain_session_move_text(
                    data.get("slug"), data.get("taskdef"), area=area, group=group.name
                ),
            )
            return


def _check_seed_dir(root, path, report) -> None:
    """A ``.new-*`` child of a run area: the run-dir lifecycle's temp dir."""
    if path.is_dir() and not path.is_symlink() and SEED_DIR_RE.fullmatch(path.name):
        report.add(
            "record.seed-dir-leftover",
            _rel(root, path),
            f"leftover seed directory `{path.name}`: a run dir is written here "
            "and moved by one atomic rename, so this one is a crash's litter",
        )


# ---------------------------------------------------------------------------
# One record directory (a session dir, or a legacy shared run dir)
# ---------------------------------------------------------------------------


def _check_record_dir(root, rdir, report, *, layout, now, stale_after) -> None:
    state = _state_file(rdir)
    if state is not None:
        _check_state(
            root, rdir, state, report, layout=layout, now=now, stale_after=stale_after
        )
    _check_ledger(root, rdir / LEDGER_FILE, report)
    _check_plan_index(root, rdir / PLAN_INDEX_FILE, report)
    _check_events(root, rdir / EVENTS_FILE, report)
    _check_masterplan(root, rdir / MASTERPLAN_DIR, report)
    _check_artifact_links(root, rdir, report)


def _check_state(root, rdir, path, report, *, layout, now, stale_after) -> None:
    data = _load_yaml_mapping(root, path, report)
    if data is None:
        return
    _check_schema_int(root, path, report, data, required=False)
    if data.get("schema") == 2:
        metadata = data.get("session")
        uid = metadata.get("uid") if isinstance(metadata, dict) else None
        if (not isinstance(uid, str) or re.fullmatch(r"[0-9a-f]{32}", uid) is None
                or uid != rdir.name.split("--", 1)[0]):
            report.add("record.session-metadata", _rel(root, path),
                       "`session` must be a mapping with an opaque `uid` matching its directory")
    status = data.get("status")
    if status is not None and status not in RUN_STATUSES:
        report.add(
            "record.state-enum",
            _rel(root, path),
            f"`status: {status!r}` is not one of {_listed(RUN_STATUSES)} "
            "(RUN-STATE.md §2 — the status enum is closed)",
        )
    if layout == SESSION_LAYOUT and status == "in-progress":
        _check_staleness(root, rdir, data, report, now=now, stale_after=stale_after)


def _check_staleness(root, rdir, data, report, *, now, stale_after) -> None:
    updated = _timestamp(data.get("updated"))
    if updated is None:
        # A stamp we cannot read is not a staleness verdict; the file's own
        # shape rules already had their say.
        return
    age = (now - updated).total_seconds()
    if age <= stale_after:
        return
    report.add(
        "record.session-stale",
        _rel(root, rdir),
        f"in-progress since {_stamp(updated)} ({_age(age)} ago): possibly "
        "orphaned — resumable, never auto-ended",
        detail=f"staleness window: {_age(stale_after)}",
    )


def _check_ledger(root, path, report) -> None:
    data = _load_yaml_mapping(root, path, report)
    if data is None:
        return
    _check_schema_int(root, path, report, data, required=False)
    tasks = data.get("tasks")
    if tasks is None:
        return
    if not isinstance(tasks, dict):
        report.add(
            "record.state-unparseable",
            _rel(root, path),
            f"`tasks:` must be a mapping of task id to row, got "
            f"{type(tasks).__name__}",
        )
        return
    for task_id, row in tasks.items():
        if not isinstance(row, dict):
            report.add(
                "record.state-unparseable",
                _rel(root, path),
                f"ledger row `{task_id}` must be a mapping, got "
                f"{type(row).__name__}",
            )
            continue
        status = row.get("status")
        if status is not None and status not in TASK_STATUSES:
            report.add(
                "record.state-enum",
                _rel(root, path),
                f"task `{task_id}` has `status: {status!r}`, not one of "
                f"{_listed(TASK_STATUSES)}",
            )


def _check_plan_index(root, path, report) -> None:
    text = _read_text(root, path, report)
    if text is None:
        return
    try:
        data = json.loads(text)
    except ValueError as exc:
        report.add(
            "record.state-unparseable",
            _rel(root, path),
            f"unparseable {path.name}: {exc}",
        )
        return
    if not isinstance(data, dict):
        report.add(
            "record.state-unparseable",
            _rel(root, path),
            f"{path.name} must be a JSON object with `schema` and `tasks`, got "
            f"{type(data).__name__}",
        )
        return
    # Its own linter refuses to run without the int, so absence is a finding
    # here where it is tolerated on the YAML state files.
    _check_schema_int(root, path, report, data, required=True)


def _check_events(root, path, report) -> None:
    """``events.jsonl``: one JSON object per line, checked line by line.

    Read as BYTES and decoded per line, so a line that is not valid UTF-8 is
    reported as that line rather than aborting the file — the same reason a
    truncated trailing line (a crash mid-append) is named as one and the
    lines before it keep their verdict.
    """
    try:
        if not (path.is_file() or path.is_symlink()):
            return
        raw = path.read_bytes()
    except OSError as exc:
        report.add(
            "record.state-unparseable",
            _rel(root, path),
            f"unreadable {path.name}: {exc}",
        )
        return
    if not raw:
        return
    lines = raw.split(b"\n")
    # A file ending in a newline splits with an empty last element; anything
    # else means the last line was never terminated.
    truncated = lines[-1] != b""
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        is_last = number == len(lines)
        why = None
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            why = f"not valid UTF-8 ({exc.reason})"
        else:
            try:
                event = json.loads(text)
            except ValueError as exc:
                why = str(exc)
            else:
                if not isinstance(event, dict):
                    why = f"a JSON {type(event).__name__}, not an object"
        if why is None:
            continue
        suffix = " (truncated final line: no trailing newline)" if is_last and truncated else ""
        report.add(
            "record.events-line-unparseable",
            _rel(root, path),
            f"line {number}: not one JSON object — {why}{suffix}",
            detail=_quote(line),
        )


def _check_masterplan(root, path, report) -> None:
    """The bundle is opaque (D12): existence, plus the pre-D12 nesting."""
    if not path.is_dir():
        return
    if _is_bundle(path):
        return  # flat, as D12 requires — nothing further to look at
    nested = sorted(
        child.name
        for child in _children(path)
        if child.is_dir() and not child.is_symlink() and _is_bundle(child)
    )
    if not nested:
        return
    report.add(
        "record.masterplan-nested",
        _rel(root, path),
        "masterplan bundle in the pre-D12 nested `masterplan/<mp-slug>/` "
        "shape; D12 flattens it to one bundle per session directory",
        detail="bundles: " + ", ".join(nested),
    )


def _check_artifact_links(root, rdir, report) -> None:
    """The ``<artifact>.md -> masterplan/<artifact>.md`` links at the record
    root (SCHEMA.md "Session directory"; module docstring for why here).

    Asked by NAME, over the well-known list only, and only about what is a
    symlink: :func:`~lmer_schema.checks.symlinks.link_fault` answers ``None``
    for the free-form content shape of the same name, and for a link with
    nothing wrong with it.
    """
    for name in MASTERPLAN_ARTIFACTS:
        link = rdir / name
        fault = symlinks.link_fault(root, link)
        if fault is None:
            continue
        code, message = _ARTIFACT_LINK_FINDINGS[fault.kind]
        report.add(code, _rel(root, link), message, detail=f"target: {fault.target}")


def _is_bundle(path) -> bool:
    """Whether *path* is a bundle root — it directly holds a well-known
    masterplan artifact. The only thing this module knows about bundle
    internals, and deliberately the smallest such thing."""
    return any(_present(path / name) for name in MASTERPLAN_ARTIFACTS)


# ---------------------------------------------------------------------------
# State-file primitives
# ---------------------------------------------------------------------------


def _load_yaml_mapping(root, path, report) -> Optional[dict]:
    """Parse *path* as a YAML mapping, reporting the shape codes on the way.

    ``None`` when the file is absent or unusable (the finding is already
    filed) — callers then have nothing left to check.
    """
    text = _read_text(root, path, report)
    if text is None:
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        report.add(
            "record.state-unparseable",
            _rel(root, path),
            f"unparseable {path.name}: {exc}",
        )
        return None
    if data is None:
        return {}
    if not isinstance(data, dict):
        report.add(
            "record.state-unparseable",
            _rel(root, path),
            f"{path.name} must be a YAML mapping, got {type(data).__name__}",
        )
        return None
    return data


def _check_schema_int(root, path, report, data, *, required) -> None:
    if "schema" not in data:
        if required:
            report.add(
                "record.state-schema",
                _rel(root, path),
                f"{path.name} must declare an integer `schema`",
            )
        return
    value = data["schema"]
    # Booleans first: `schema: yes` is a YAML bool and isinstance(True, int)
    # would wave it through (the read_taskdef_schema / load_sources trap).
    if isinstance(value, bool) or not isinstance(value, int):
        report.add(
            "record.state-schema",
            _rel(root, path),
            f"`schema` must be an integer, got {value!r}"
            + (" (a YAML bool is not an integer)" if isinstance(value, bool) else ""),
        )


def _read_text(root, path, report) -> Optional[str]:
    """The file's text, ``None`` when it is absent (no finding) or unreadable
    (an unparseable finding — a present-but-unreadable state file is broken,
    the same rule the manifest loader applies)."""
    if not _present(path):
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.add(
            "record.state-unparseable",
            _rel(root, path),
            f"unreadable {path.name}: {exc}",
        )
        return None


def _state_file(rdir) -> Optional[Path]:
    """The record's state file: ``state.yaml``, or the legacy ``state.yml``
    when the newer name is absent (RUN-STATE.md's lazy migration)."""
    for name in STATE_FILES:
        path = rdir / name
        if _present(path):
            return path
    return None


def _present(path) -> bool:
    """Whether something IS at *path* — a dangling symlink counts, so a
    broken state file is reported rather than read as absent."""
    return path.is_symlink() or path.exists()


def _children(path) -> List[Path]:
    """*path*'s entries in name order; an unreadable dir yields nothing (the
    validator never crashes on a directory it cannot list)."""
    try:
        return sorted(path.iterdir(), key=lambda child: child.name)
    except OSError:
        return []


def _rel(root, path) -> str:
    """*path* as the repo-relative POSIX string a finding carries."""
    return path.relative_to(root).as_posix()


def _listed(values) -> str:
    return ", ".join(str(value) for value in values)


def _quote(line: bytes) -> str:
    text = line.decode("utf-8", errors="replace").strip()
    return text if len(text) <= _DETAIL_LIMIT else text[:_DETAIL_LIMIT] + "…"


def _timestamp(value) -> Optional[datetime]:
    """A RUN-STATE.md stamp as an aware UTC datetime, or ``None`` when it is
    absent or unreadable. PyYAML hands back a ``datetime`` for an unquoted
    stamp and a ``str`` for the quoted form the writer emits."""
    if isinstance(value, datetime):
        return _utc(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return _utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except ValueError:
        return None


def _utc(moment: datetime) -> datetime:
    """An aware UTC datetime. A naive stamp reads as UTC: every stamp lmer
    writes is UTC, written with or without the offset."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _age(seconds) -> str:
    """A compact human age (``3d``, ``5h``, ``12m``) for a finding's text."""
    seconds = int(seconds)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"
