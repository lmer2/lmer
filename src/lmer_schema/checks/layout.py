"""Tree-shape checks: the registry against what is on disk (spec §4/§6/§7,
D6/D9/D13; SCHEMA.md "Project resolution and host buckets", "Host buckets",
"Inventory").

Every other check module is handed a path and asked whether its CONTENTS are
in shape. This one answers the question before that: which directories of
this plexus are projects at all, and does the registry agree with them. It
owns the ``tree.*`` codes and four families of finding:

1. **registry ↔ directory** — a registry entry whose ``<short-name>/``
   directory does not exist (WARN), and a project-shaped top-level directory
   no entry claims (WARN, "unregistered project or dotless host bucket").
   Neither is an error: the Inventory's ``<short-name>/`` row makes either
   mismatch a warning, because a fresh entry with no directory yet and a
   directory whose entry has not been written yet are both states a
   migrating tree passes through.
2. **host buckets** (D9) — a top-level directory whose name carries a dot
   holds the work of a repo nobody has named. Each bucket is listed once as
   INFO "unnamed, awaiting adoption"; it is never a warning, never an error
   and never blocks.
3. **bucket ↔ registry collision** (§4 finding 7) — a registered remote's
   ``{host}/{project}/`` path still present as a REAL directory is an ERROR
   (a split tree: work would land in two places). The same path as a SYMLINK
   is the §9 compatibility link: INFO.
4. **the `_` reservation and the archive mirror** (D6, §6) — an unknown
   ``_``-prefixed name is a WARN at either scope, an ``_archive/`` entry
   whose first component is not an area of that scope is an ERROR (a broken
   mirror path), an archive home inside ``_runs/`` is a WARN (D13's ONE
   archive home) and legacy ``runs/archive/`` is INFO, never required to
   move.

Which directories are projects: the shared walk
===============================================
This module does NOT own that question any more. It asks
:mod:`lmer_schema.checks.walk`, which every per-project check module asks —
one walk, one reading, one set of tests (!256 discussion 5: the three
private descents disagreed, and folding them silently changed what two
modules checked). A bucket starts at the dotted top-level directory and
follows the remote's path; the intermediate levels (``acme/``) are NAMESPACE
levels and the project begins at the first project-shaped directory — the §1
marker test at WHATEVER DEPTH it sits. **There is no depth rule** (SCHEMA.md
Concepts and Host buckets both say "not path depth"), and below the plexus
root ``_`` is not reserved: those components come from the remote.

The same walk answers the dotless case. A dotless host (``localhost``) is not
supported as a bucket at schema 1, so a dotless unregistered top-level
directory that is project-shaped, or that holds a project-shaped directory
below it, gets the one WARN §7 names: "unregistered project or dotless host
bucket".

What this module hands the runner
=================================
:func:`check_layout` returns the :class:`ProjectDir` list the walk found —
registered directories, bucket projects, legacy ``{host}/{project}`` homes
and unregistered project-shaped directories — each carrying the
:class:`~lmer_schema.checks.findings.Form` its CONTENTS are checked at. That
is the point of the return value: a bucket's contents are checked at the
legacy tier's severities even inside a schema-form tree (SCHEMA.md "Host
buckets"), so the per-project check modules read ``project.form`` rather
than re-deriving the rule. The walk happens once.

**The list is wider than this module's own findings, deliberately.** A
registered ``<short-name>/`` is on it whether or not it is project-shaped
yet, and a bucket path this module reports as ``tree.bucket-collision`` is
NOT removed from it: the ``claimed`` suppression below is about not calling a
named tree "unnamed", which is a REPORTING concern of the ``tree.*`` codes.
Dropping that directory from the list would stop the agent and index modules
checking it — and on a split tree it is the directory most worth checking.

Two judgement calls, recorded because the text they implement is terse:

- The Inventory says an ``_archive/`` first component that is not "a schema
  area name" is an error. Taken literally that would error on
  ``_archive/README.md`` and ``_archive/<short-name>/…``, both of which are
  content mirrored by its relative path, so the check is scoped to the
  MACHINE-owned half of the name space: a ``_``-prefixed first component
  must be an area of that scope (``_archive/_runs`` is a project-level area
  at the instance root, so that mirror path is broken), and a content name
  is never flagged.
- A dotted top-level directory holding nothing project-shaped yields no
  finding at all. SCHEMA.md lists each BUCKET once, and a bucket is the
  project-shaped directory; a dotted directory with no project in it is a
  content name that happens to have a dot, and inventing an INFO for it
  would report a bucket that does not exist.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Tuple

from lmer_schema.checks.findings import Form, Severity, Tier, register
from lmer_schema.checks.walk import (
    BUCKET,
    LEGACY_HOME,
    RECOGNIZED_LEGACY_NAMES,
    REGISTERED,
    UNREGISTERED,
    ProjectDir,
    by_top_level,
    find_project_dirs,
    project_dirs,
)
from lmer_schema.manifest import MANIFEST_NAME
from lmer_schema.registry import bucket_path, is_host_bucket_name, load_registry
from lmer_schema.urls import normalize

#: ``ProjectDir``, its ``kind`` values, ``RECOGNIZED_LEGACY_NAMES`` and
#: ``find_project_dirs`` now live in :mod:`lmer_schema.checks.walk` (one walk
#: for every check module) and are re-exported here: they were this module's
#: published names, and the vocabulary reads where the ``tree.*`` codes are.
__all__ = [
    "ARCHIVE_DIR",
    "BUCKET",
    "INSTANCE_AREAS",
    "LEGACY_HOME",
    "PROJECT_AREAS",
    "RECOGNIZED_LEGACY_NAMES",
    "REGISTERED",
    "RESERVED_INSTANCE_NAMES",
    "RESERVED_PROJECT_NAMES",
    "UNREGISTERED",
    "ProjectDir",
    "check_layout",
    "find_project_dirs",
]

#: The archive mirror, at either scope (§6, D13).
ARCHIVE_DIR = "_archive"

#: Schema areas that live at the INSTANCE root (§2) — also the legal first
#: components of an instance ``_archive/`` mirror path.
INSTANCE_AREAS = ("_agents", "_knowledge", "_napkin", "_taskdef")

#: Schema areas that live inside a PROJECT (§2) — also the legal first
#: components of a project ``_archive/`` mirror path.
PROJECT_AREAS = ("_agents", "_info", "_plans", "_runs", "_specs", "_taskdef")

#: `_`-prefixed names the schema defines at the instance root (D6).
RESERVED_INSTANCE_NAMES = INSTANCE_AREAS + (ARCHIVE_DIR, MANIFEST_NAME)

#: `_`-prefixed names the schema defines inside a project (D6).
RESERVED_PROJECT_NAMES = PROJECT_AREAS + (ARCHIVE_DIR,)

#: Sentinel for "load the registry from *root*" (``None`` is meaningful:
#: legacy tree / no registry), matching ``registry.resolve_project_dir``.
_LOAD = object()


register(
    "tree.entry-without-dir",
    Severity.WARN,
    tier=Tier.MACHINE,
    summary="a registry entry whose `<short-name>/` directory does not exist",
)
register(
    "tree.unregistered-project",
    Severity.WARN,
    tier=Tier.MACHINE,
    summary=(
        "a project-shaped top-level directory with no registry entry — an "
        "unregistered project, or a dotless host bucket (unsupported at v1)"
    ),
)
register(
    "tree.host-bucket",
    Severity.INFO,
    tier=Tier.NOT_SCHEMA,
    summary="a host bucket: an unnamed repo's work, awaiting adoption (D9)",
)
register(
    "tree.legacy-project-home",
    Severity.INFO,
    tier=Tier.LEGACY,
    summary="a legacy `{host}/{project}` project home, read in place",
)
register(
    "tree.bucket-collision",
    Severity.ERROR,
    tier=Tier.NOT_SCHEMA,
    summary=(
        "a registered remote's `{host}/{project}/` path is still a real "
        "directory — a split tree, work would land in two places (§4)"
    ),
)
register(
    "tree.bucket-compat-link",
    Severity.INFO,
    tier=Tier.NOT_SCHEMA,
    summary="the §9 compatibility symlink left at a project's old path",
)
register(
    "tree.unknown-reserved-name",
    Severity.WARN,
    tier=Tier.MACHINE,
    summary="an unknown `_`-prefixed name; `_*` belongs to the schema (D6)",
)
register(
    "tree.archive-mirror-path",
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary=(
        "an `_archive/` entry whose first component is not an area of that "
        "scope — the archive mirrors relative paths (§6)"
    ),
)
register(
    "tree.archive-inside-runs",
    Severity.WARN,
    tier=Tier.MACHINE,
    summary="an archive home inside `_runs/`; ONE archive home per tree (D13)",
)
register(
    "tree.legacy-archive",
    Severity.INFO,
    tier=Tier.LEGACY,
    summary="the legacy `runs/archive/` home, recognised and never required to move",
)


def check_layout(root, report, registry=_LOAD) -> Tuple[ProjectDir, ...]:
    """Run the tree-shape checks over the plexus rooted at *root*.

    *report* carries the tree's form and collects the findings. *registry*
    defaults to the tree's own (:func:`~lmer_schema.registry.load_registry`,
    which raises on a broken manifest and returns ``None`` for a legacy
    tree); pass ``None`` for the registry-free checks or an already-loaded
    :class:`~lmer_schema.registry.Registry` to skip the reload.

    Returns the :class:`ProjectDir` list, in report order, for the runner to
    hand to the per-project check modules.
    """
    root = Path(root)
    if registry is _LOAD:
        registry = load_registry(root)

    claimed = _check_bucket_collisions(root, report, registry)
    projects = project_dirs(root, registry=registry, form=report.form)
    _check_registry_dirs(root, report, registry)
    _check_top_level(root, report, registry, projects, claimed)
    _check_archive(root, report, root, INSTANCE_AREAS, report.form)
    for project in projects:
        _check_project_names(root, report, project)
        _check_archive(root, report, project.path, PROJECT_AREAS, project.form)
        _check_archive_home(root, report, project)
    return projects


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _children(directory) -> Tuple[Path, ...]:
    """Sorted children of *directory* — every entry, files and symlinks
    included, because the `_` reservation is about NAMES. Empty when the
    directory cannot be listed (a file, a missing path, an unreadable
    directory): this module reports shapes, never the file system's own
    troubles."""
    try:
        return tuple(sorted(Path(directory).iterdir()))
    except OSError:
        return ()


def _rel(root, path) -> str:
    """*path* as the repo-relative POSIX string a finding carries."""
    path = Path(path)
    if path == Path(root):
        return "."
    return path.relative_to(root).as_posix()


# ---------------------------------------------------------------------------
# Registry <-> directory
# ---------------------------------------------------------------------------


def _check_registry_dirs(root, report, registry) -> None:
    """Every entry SHOULD have its directory (Inventory ``<short-name>/``).

    The findings only: the directories themselves are the shared walk's
    (which lists a registered ``<short-name>/`` shaped or not)."""
    if registry is None:
        return
    for name in registry:
        path = root / name
        if not os.path.lexists(path):
            report.add(
                "tree.entry-without-dir",
                name,
                f"`projects.{name}` has no `{name}/` directory — the entry "
                "names a project the tree does not hold (a session creates it "
                "on first write; a stale entry wants removing).",
            )
            continue
        if not path.is_dir():
            report.add(
                "tree.entry-without-dir",
                name,
                f"`projects.{name}` names `{name}/`, which is not a directory.",
                detail=f"`{name}` exists as a file",
            )


def _check_top_level(root, report, registry, projects, claimed) -> None:
    """Report the top level: the `_` reservation, host buckets, and the
    dotless directories no entry claims.

    *projects* is the shared walk's list, grouped by top-level name — this
    function names directories, it does not discover them."""
    found = by_top_level(projects)
    for child in _children(root):
        name = child.name
        if name.startswith("."):
            continue
        if name.startswith("_"):
            if name not in RESERVED_INSTANCE_NAMES:
                report.add(
                    "tree.unknown-reserved-name",
                    _rel(root, child),
                    f"`{name}` is not a schema name at the instance root; "
                    "`_*` is reserved for the schema (D6). Rename it into the "
                    "content namespace, or use one of "
                    f"{', '.join(RESERVED_INSTANCE_NAMES)}.",
                )
            continue
        if registry is not None and name in registry:
            continue  # its directory is handled by _check_registry_dirs
        if child.is_symlink() or not child.is_dir():
            continue
        if is_host_bucket_name(name):
            _list_buckets(report, found.get(name, ()), claimed)
            continue
        if name in RECOGNIZED_LEGACY_NAMES:
            continue
        _check_dotless_dir(root, report, registry, child, found.get(name, ()))


def _list_buckets(report, buckets, claimed) -> None:
    """Every bucket under one dotted top-level directory (D9).

    A bucket already reported as ``tree.bucket-collision`` is not ALSO called
    unnamed — this module's own reporting rule. It stays on the list the
    runner hands the other check modules (module docstring).
    """
    for project in buckets:
        if project.path in claimed:
            continue  # a registered remote's split tree: already an ERROR
        rel = project.rel
        if report.form is Form.LEGACY:
            # SCHEMA.md "Host buckets": in a legacy tree the same path is
            # not a bucket at all — it is the project's only home.
            report.add(
                "tree.legacy-project-home",
                rel,
                f"legacy `{{host}}/{{project}}` project home `{rel}`, read in "
                "place; it becomes a short-name project by gaining a registry "
                "entry (Appendix A).",
            )
        else:
            report.add(
                "tree.host-bucket",
                rel,
                f"host bucket `{rel}`: an unnamed repo's work, awaiting "
                "adoption (D9). Name it with one commit — `git mv` to "
                "`<short-name>/` plus a registry entry listing its remote.",
            )


def _check_dotless_dir(root, report, registry, child, shaped) -> None:
    """A dotless top-level directory no entry claims: a project wanting an
    entry, or a dotless host bucket (§1: unsupported at v1)."""
    if not shaped:
        return  # content namespace, never checked
    if registry is None:
        return  # nothing to be unregistered against
    rel = _rel(root, child)
    detail = None
    if [project.path for project in shaped] != [child]:
        detail = "project-shaped: " + ", ".join(project.rel for project in shaped)
    report.add(
        "tree.unregistered-project",
        rel,
        f"`{rel}/` is project-shaped but no registry entry claims it: an "
        "unregistered project, or a dotless host bucket (a dotless host "
        "is not supported as a bucket at schema 1 — register the "
        "project).",
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Bucket <-> registry collision (§4 finding 7)
# ---------------------------------------------------------------------------


def _check_bucket_collisions(root, report, registry):
    """A registered remote's ``{host}/{project}/`` path on disk.

    Returns the bucket paths the registry claims and that exist as real
    directories, so the bucket listing does not also call a split tree
    "unnamed".
    """
    claimed = set()
    if registry is None:
        return claimed
    seen = set()
    for name in registry:
        project = registry[name]
        for spelling in (project.repo, *project.remotes):
            if not spelling:
                continue
            path = bucket_path(root, normalize(spelling))
            # Two spellings of one remote (a ported one and its unported
            # twin) name ONE directory — the port lives in the key only.
            if path is None or path in seen:
                continue
            seen.add(path)
            if not os.path.lexists(path):
                continue
            rel = _rel(root, path)
            if path.is_symlink():
                report.add(
                    "tree.bucket-compat-link",
                    rel,
                    f"`{rel}` is the compatibility symlink left at "
                    f"`{name}`'s old path (§9); it is a finding aid, not a "
                    "second home.",
                    detail=f"-> {os.readlink(path)}",
                )
            elif path.is_dir():
                claimed.add(path)
                report.add(
                    "tree.bucket-collision",
                    rel,
                    f"`{rel}` is a real directory while `projects.{name}` "
                    f"registers that remote: a split tree — work would land "
                    f"in both `{rel}/` and `{name}/`. Merge it into "
                    f"`{name}/` (a relative compatibility symlink may stay).",
                )
    return claimed


# ---------------------------------------------------------------------------
# The `_` reservation and the archive mirror
# ---------------------------------------------------------------------------


def _check_project_names(root, report, project) -> None:
    """`_*` inside a project belongs to the schema (D6)."""
    for child in _children(project.path):
        name = child.name
        if not name.startswith("_") or name in RESERVED_PROJECT_NAMES:
            continue
        report.add(
            "tree.unknown-reserved-name",
            _rel(root, child),
            f"`{name}` is not a schema name inside a project; `_*` is "
            "reserved for the schema (D6). Rename it into the content "
            f"namespace, or use one of {', '.join(RESERVED_PROJECT_NAMES)}.",
            form=project.form,
        )


def _check_archive(root, report, base, areas, form) -> None:
    """`_archive/` mirrors relative paths (§6): a machine-owned first
    component must be an area of this scope."""
    archive = base / ARCHIVE_DIR
    for child in _children(archive):
        name = child.name
        if not name.startswith("_") or name in areas:
            continue
        rel = _rel(root, child)
        report.add(
            "tree.archive-mirror-path",
            rel,
            f"`{rel}` is not a mirror path: `{ARCHIVE_DIR}/` keeps each "
            f"archived path relative to `{_rel(root, base)}`, whose schema "
            f"areas are {', '.join(areas)} (§6).",
            form=form,
        )


def _check_archive_home(root, report, project) -> None:
    """D13's ONE archive home, and the legacy one it replaces."""
    inside_runs = project.path / "_runs" / "archive"
    if inside_runs.is_dir():
        report.add(
            "tree.archive-inside-runs",
            _rel(root, inside_runs),
            "archived sessions live at "
            f"`{project.rel}/{ARCHIVE_DIR}/_runs/<slug>/<session-id>/`, not "
            "inside `_runs/`: ONE archive home per tree (D13).",
            form=project.form,
        )
    legacy = project.path / "runs" / "archive"
    if legacy.is_dir():
        report.add(
            "tree.legacy-archive",
            _rel(root, legacy),
            "legacy `runs/archive/` home, recognised and read in place; it is "
            "never required to move (§6).",
            form=project.form,
        )
