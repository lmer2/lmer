"""The ONE project-directory walk (spec §1/§4, D6/D9; SCHEMA.md "Concepts",
"Ownership namespaces", "Host buckets").

Three check modules — ``layout``, ``agents``, ``index_links`` — each grew
their own descent because they landed in parallel, and they gave DIFFERENT
answers for the same tree (!256 discussion 5). Once the runner hands one
list to all three, whichever reading survives becomes the rule for surfaces
it was never written for, and every module's own tests keep passing. This
module is that one reading, stated once and tested once
(``tests/test_lmer_schema_checks_walk.py``).

Where it lives, and why
=======================
Beside :mod:`lmer_schema.checks.findings`, as a SUPPORT module of the checks
package rather than inside one of the three: it registers no code, emits no
finding and never touches a :class:`~lmer_schema.checks.findings.Report`, so
importing it is not a check module importing a sibling — the invariant the
runner's docstring states and its tests pin (a check module never imports a
check module; the runner alone names them). Putting the walk in ``layout``
would have made the other two import a check module for its ``tree.*``
side of the house; putting it in each would be the divergence again.

The three decisions this walk makes
===================================
Each was a live disagreement between the three implementations, so each is
recorded here and pinned by a test of its own.

1. **``_``-prefixed children are skipped at the PLEXUS ROOT only**, not at
   every level of a bucket descent (``index_links``/``agents``' reading).
   The ownership namespace (D6) is a rule about the names the SCHEMA owns:
   the instance root and the inside of a project directory. The components
   below a bucket's host directory come from the REMOTE — a forge namespace
   is content, and not the schema's to reserve — so ``other.com/_ns/proj/``
   is a project like any other. Skipping ``_`` all the way down silently
   stopped checking it.
2. **The marker test first, the bucket test second** (``layout``/
   ``index_links``' reading). SCHEMA.md Concepts makes the marker test
   (:func:`~lmer_schema.registry.is_project_shaped`) what separates a
   project from a namespace level, with no precondition about the host, so a
   dotted top-level directory that is ITSELF project-shaped IS that project
   and the descent never starts. ``example.com/runs/`` is one project called
   ``example.com``, not the host directory of a bucket below it.
3. **A registered short-name directory is included even when it is not
   project-shaped, and a bucket already reported as ``tree.bucket-collision``
   is NOT suppressed** (``layout``'s registered-dirs rule, without
   ``layout``'s suppression). A registered project's directory is in scope
   whether or not it has grown areas yet; and the ``claimed`` suppression is
   ``layout``'s own REPORTING concern — on a split tree that directory is the
   one most worth checking, so removing it from the list every other module
   is handed would be the widest silence of the three. ``layout`` keeps its
   suppression for its own findings and does not impose it here.

There is **no depth rule** anywhere in this module, in either direction:
SCHEMA.md Concepts and Host buckets both say "not path depth". The descent
stops at the first project-shaped directory on each branch, at whatever depth
it sits, and nothing below a project is a second project. Symlinked levels
are never followed — a §9 compatibility link points at a project already
found, and following one would report it twice or loop.

The form each project carries
=============================
:attr:`ProjectDir.form` is the tier its CONTENTS are checked at, not the
tree's form: a host bucket's contents are checked at the LEGACY tier's
severities even inside a schema-form tree (SCHEMA.md "Host buckets", D9 "a
bucket never blocks"), so a bucket project carries :attr:`Form.LEGACY` while
everything else carries the *form* the caller passed (``None`` meaning "the
report's own form", which is what :meth:`Report.add` already does with it).
That is the value ``check_agents`` reads per project, and the value the
runner's report view pins ``record``'s findings by.

The registry is optional. With one (the runner's path, through
``check_layout``) a registered ``<short-name>/`` is a project by its ENTRY,
shaped or not; without one — a module discovering on its own, a legacy tree —
only the marker test speaks. Passing the registry never removes a directory
from the list, so the two paths differ by inclusion only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from lmer_schema.checks.findings import Form
from lmer_schema.registry import is_host_bucket_name, is_project_shaped

__all__ = [
    "BUCKET",
    "LEGACY_HOME",
    "RECOGNIZED_LEGACY_NAMES",
    "REGISTERED",
    "UNREGISTERED",
    "ProjectDir",
    "by_top_level",
    "find_project_dirs",
    "project_dirs",
]

#: Bare legacy names at the INSTANCE root: the legacy twins of the ``_``
#: areas (SCHEMA.md "Legacy form"), so the root's ownership namespace covers
#: them exactly as it covers ``_agents/`` — they are instance surfaces, never
#: projects. The modules that own those surfaces report them.
RECOGNIZED_LEGACY_NAMES = ("agent-files", "knowledge", "mirrors", "napkin", "taskdef")

#: :attr:`ProjectDir.kind` values.
REGISTERED = "registered"
BUCKET = "bucket"
LEGACY_HOME = "legacy-home"
UNREGISTERED = "unregistered"


@dataclass(frozen=True)
class ProjectDir:
    """One project tree this plexus holds, however it is addressed.

    ``path`` is absolute, ``rel`` its repo-relative POSIX spelling (the
    finding path), ``name`` the registry short name or ``None`` when nothing
    names it (D9: never derived), ``kind`` one of :data:`REGISTERED`,
    :data:`BUCKET`, :data:`LEGACY_HOME`, :data:`UNREGISTERED`, and ``form``
    the tier its CONTENTS are checked at — ``LEGACY`` for a bucket even in a
    schema-form tree (SCHEMA.md "Host buckets"), ``None`` for "the report's
    own form".
    """

    path: Path
    rel: str
    name: Optional[str]
    kind: str
    form: Optional[Form]

    def __fspath__(self) -> str:
        """The absolute project dir, so a check module's ``project_dirs=``/
        ``projects=`` override (which does ``Path(entry)``) takes this list as
        it stands — the runner walks the tree once."""
        return str(self.path)


def project_dirs(
    root, *, registry=None, form: Optional[Form] = None
) -> Tuple[ProjectDir, ...]:
    """Every project directory of the plexus at *root*.

    *registry* is the built :class:`~lmer_schema.registry.Registry` or
    ``None`` (a legacy tree, a registry that did not build, a module
    discovering on its own); *form* is the tree's own form, carried by every
    project that is not a host bucket.

    Registered directories come first, in registry order, then the top level
    in name order — which is the order their findings are reported in.
    """
    root = Path(root)
    found: List[ProjectDir] = []
    if registry is not None:
        for name in registry:
            path = root / name
            if not path.is_dir():
                continue  # `tree.entry-without-dir` is layout's to report
            found.append(
                ProjectDir(path=path, rel=name, name=name, kind=REGISTERED, form=form)
            )
    for child in _child_dirs(root):
        name = child.name
        if name.startswith("_"):
            continue  # the ownership namespace, at the root only (decision 1)
        if registry is not None and name in registry:
            continue  # already listed by its entry
        if name in RECOGNIZED_LEGACY_NAMES:
            continue  # a bare instance surface, not a project
        bucket = is_host_bucket_name(name)
        if is_project_shaped(child):
            # The marker test decides, before the bucket test (decision 2).
            found.append(_project(root, child, bucket=bucket, form=form))
            continue
        for path in find_project_dirs(child):
            found.append(_project(root, path, bucket=bucket, form=form))
    return tuple(found)


def find_project_dirs(top) -> Tuple[Path, ...]:
    """Every project-shaped directory at or below *top*, sorted.

    The descent below a host directory (or a dotless top-level directory
    holding projects): it stops at the FIRST project-shaped directory on each
    branch — the §1 marker test decides, never depth — and *top* itself may be
    that directory. Hidden names and symlinked levels are not descended into;
    ``_`` names ARE (decision 1: below the root they come from the remote).
    """
    found: List[Path] = []
    stack = [Path(top)]
    while stack:
        directory = stack.pop()
        if is_project_shaped(directory):
            found.append(directory)
            continue
        stack.extend(_child_dirs(directory))
    return tuple(sorted(found))


def by_top_level(projects) -> Dict[str, Tuple[ProjectDir, ...]]:
    """*projects* grouped by their top-level directory name.

    What a caller reporting about the TOP LEVEL needs (``layout`` lists a
    bucket's projects under its host directory, and warns once per
    unregistered dotless directory naming the shaped dirs below it), so the
    grouping is not re-derived from paths at each site.
    """
    grouped: Dict[str, List[ProjectDir]] = {}
    for project in projects:
        grouped.setdefault(project.rel.split("/", 1)[0], []).append(project)
    return {name: tuple(items) for name, items in grouped.items()}


def _project(root: Path, path: Path, *, bucket: bool, form) -> ProjectDir:
    """One found directory as a :class:`ProjectDir`. A bucket project is
    never named (D9) and its contents are checked at the legacy tier."""
    if bucket:
        kind = LEGACY_HOME if form is Form.LEGACY else BUCKET
        form = Form.LEGACY
    else:
        kind = UNREGISTERED
    return ProjectDir(
        path=path, rel=_rel(root, path), name=None, kind=kind, form=form
    )


def _child_dirs(directory) -> List[Path]:
    """The real (non-symlink), non-hidden child directories of *directory*,
    sorted; empty when it cannot be listed (a file, a missing path, an
    unreadable directory) — the walk reports shapes, never the file system's
    own troubles."""
    try:
        children = sorted(Path(directory).iterdir())
    except OSError:
        return []
    return [
        child
        for child in children
        if not child.name.startswith(".")
        and not child.is_symlink()
        and child.is_dir()
    ]


def _rel(root: Path, path: Path) -> str:
    """*path* as the repo-relative POSIX string a finding carries."""
    if path == root:
        return "."
    return path.relative_to(root).as_posix()
