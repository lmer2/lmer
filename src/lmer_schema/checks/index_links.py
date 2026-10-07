"""Index integrity: the ``_specs/``/``_plans/`` symlinks, the ``PLANS.md``
line index and the generated ``README.md`` index blocks (spec §3.2/§7,
D11/D13/D14; SCHEMA.md "Inventory" / "Index lines"; the legible-work-repo
design).

Three mechanics, one module, because they are the halves of one promise —
"``ls`` the index and you see what is active" (D11):

1. **Dated symlinks.** ``<project>/_specs/`` and ``<project>/_plans/`` hold
   relative symlinks named ``YYYY-MM-DD-<runlabel>--<basename>.md`` pointing
   into a session directory (``../_runs/<slug>/<session-id>[--<name>]/…``).
   The target is whatever the session dir is called NOW — a named session
   carries its ``--<name>`` suffix after the freeze — so this module never
   parses a target, it only asks the three questions SCHEMA.md asks: is it
   relative, does it stay inside the plexus, does it resolve. It does not
   implement them either: they are :mod:`lmer_schema.checks.symlinks`', the
   support module that also answers them for the session-dir artifact links
   :mod:`~lmer_schema.checks.record` checks (!261 discussion 1). What stays
   here is what the answers MEAN — the codes, the words and the tier.
2. **``PLANS.md``.** One line per plan. An ENTRY is a line carrying a
   relative markdown link to a ``.md`` file (SCHEMA.md "Index lines"); an
   entry whose file is gone is an integrity finding and a plan file no line
   carries is a warning.
3. **Generated README index blocks.** Every durability push renders one
   Markdown block per project — one row per committed run record under
   ``_runs/``, newest first — between ``<!-- lmer:index:start -->`` and
   ``<!-- lmer:index:end -->`` in ``<project>/README.md``, and
   ``lmer-schema index --write`` renders the root ``README.md``'s project
   list from ``_plexus.yaml``. The block is the kernel's
   (``work_repo.project_index``, imported lazily: the import direction is
   ``work_repo -> lmer_schema``), and so is the freshness oracle here: the
   existing block is compared byte for byte with a fresh render of what is
   on disk. No git, no clock, no write — a checkout holding unpushed
   records may therefore WARN where a clean clone is silent (SCHEMA-GUIDE
   §5). ``index.readme-missing`` (INFO) names a project holding records
   whose README carries no block; ``index.readme-stale`` (WARN) names a
   block that differs from the render, markers that are malformed, or hand
   text holding a line git reads as a conflict marker (the generator skips
   such a file, so the block will never refresh). A block whose format
   token is newer than this runtime's is left alone. Both codes are gated
   on the manifest the runner passes: a legacy tree has no index.

Severity comes from the D14 tier table in :mod:`lmer_schema.checks.findings`
and is never chosen here. Four of the five link codes are registered there
because D14 names them by behaviour; this module registers
``index.file-without-entry`` and the two README codes, none of which the
legacy tier softens.

The split that matters
======================
Dangling links and entries without files are **integrity** findings: the
shape is right, something is missing. A legacy tree softens them to WARN —
there the validator is a migration aid, not a gate (D14). A non-relative or
escaping target is a **shape** violation of the index mechanic itself (an
absolute link breaks on every other clone; an escaping one addresses
something the plexus does not own), so it is an ERROR in every form. That is
why the two live under different codes rather than one code with a severity
argument.

A link is reported ONCE: an escaping target is not also reported dangling,
because "it points outside the tree" is the whole finding and the resolution
question is meaningless past it.

Tombstones are legal
====================
Archiving or forgetting a plan MUST tombstone its ``PLANS.md`` line rather
than delete it (SCHEMA.md "Index lines"): the link is replaced by
``archived``/``forgotten`` plus a date, or re-pointed at the ``_archive/``
mirror. Both spellings are handled by the same rule — **a tombstoned line is
never a finding**. A line with no relative ``.md`` link is not an entry at
all, and a link into an archive home (``_archive/``, legacy ``runs/archive/``)
is an entry whose file this tree deliberately no longer has. A tombstone
still COVERS its plan file: tombstone-then-move is a two-step, and between
the steps the file must not be reported as "no entry".

Where it looks
==============
Index dirs are found under project directories, and WHICH directories those
are is :mod:`lmer_schema.checks.walk`'s answer — the ONE walk every
per-project check module shares (!256 discussion 5: three private descents
disagreed about the same tree). This module restates no marker list, no
bucket rule and no descent of its own. Both the schema names (``_specs``,
``_plans``) and the legacy bare twins (``specs``, ``plans``) are index dirs,
in either form, so a half-migrated project is checked on both sides. A host
bucket's contents are checked at the legacy tier's severities even inside a
schema-form tree (SCHEMA.md "Outside the schema"), which is the ``form=``
override on :meth:`Report.add`; this module derives that from the top-level
name (:func:`_tier_form`) rather than from the project it was handed, so a
caller-supplied list and a discovered one tier identically.

Not walked: ``_archive/`` (a mirror of the past keeps its own now-dangling
links by design, D13), session directories and their masterplan bundles
(a foreign writer's tree, D12), and anything below a project dir that is not
one of the four index dir names. A session directory holds links of its own
(SCHEMA.md's ``<session-dir>/<artifact>.md`` row); they are the RECORD's,
checked by :mod:`~lmer_schema.checks.record`, which owns that walk — this
module still never enters one.

The runner (which already computes the project list for the tree checks) may
pass ``projects=`` to skip the discovery walk; the two paths must agree —
they are now the same walk, the runner's simply built WITH the registry — so
discovery is public (:func:`iter_project_dirs`, :func:`iter_index_dirs`) and
tested directly.
"""

from __future__ import annotations

import difflib
import os
import re
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, List, Optional, Tuple

from lmer_schema import registry
from lmer_schema.checks import symlinks
from lmer_schema.checks.findings import Form, Report, Severity, Tier, register
from lmer_schema.checks.walk import project_dirs

__all__ = [
    "ARCHIVE_NAMES",
    "CODES",
    "INDEX_DIR_NAMES",
    "LINK_RE",
    "MARKER_LINE_DETAIL",
    "PLANS_DIR_NAMES",
    "PLANS_INDEX",
    "README_NAME",
    "IndexEntry",
    "check_index_links",
    "check_plans_index",
    "check_readme_index",
    "check_symlinks",
    "index_entries",
    "iter_index_dirs",
    "iter_project_dirs",
    "line_delta",
]

#: The index dirs, schema names first, legacy bare twins second (§3.2, §9).
INDEX_DIR_NAMES: Tuple[str, ...] = ("_specs", "_plans", "specs", "plans")

#: Of those, the ones that carry plan files and the ``PLANS.md`` index.
PLANS_DIR_NAMES: Tuple[str, ...] = ("_plans", "plans")

#: The plans index file name.
PLANS_INDEX = "PLANS.md"

#: Archive homes: the schema mirror (D13, §6) and the legacy ``runs/archive/``.
#: A link into either is a tombstone, never a dangling entry.
ARCHIVE_NAMES: Tuple[str, ...] = ("_archive", "archive")

#: An inline markdown link: ``[text](target)``.
LINK_RE = re.compile(r"\[[^\]]*\]\(\s*(?P<target>[^)\s]+)\s*\)")

#: ``scheme:`` prefix of a non-path link (``https:``, ``mailto:``).
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)

register(
    "index.file-without-entry",
    Severity.WARN,
    tier=Tier.MACHINE,
    summary=(
        "a `<project>/_plans/<slug>.md` plan file that no `PLANS.md` line "
        "carries (the index is expected once plan files exist)"
    ),
)
register(
    "index.readme-missing",
    Severity.INFO,
    tier=Tier.MACHINE,
    summary=(
        "a project holding run records whose `README.md` carries no generated "
        "index block (the project's next durability push or `lmer-schema index "
        "--write` creates it)"
    ),
)
register(
    "index.readme-stale",
    Severity.WARN,
    tier=Tier.MACHINE,
    summary=(
        "a `README.md` generated index block that no longer matches a fresh "
        "render of the records (or, at the plexus root, of `_plexus.yaml`), or "
        "whose markers are malformed"
    ),
)

#: Every code this module emits, in code order (the four D14 codes are
#: registered by ``findings`` and consumed here).
CODES: Tuple[str, ...] = (
    "index.dangling-link",
    "index.entry-without-file",
    "index.file-without-entry",
    "index.link-escapes",
    "index.link-not-relative",
    "index.readme-missing",
    "index.readme-stale",
)

#: The file the generated index block lives in, at the plexus root and at
#: every project root (``work_repo.project_index.README_NAME``).
README_NAME = "README.md"

#: The detail both README codes carry when a conflict-marker line in the hand
#: text is what keeps the generator away from the file: the publication
#: path's staged-marker gate would otherwise refuse every push of the project,
#: so the generator never stages such a README and its block never refreshes.
MARKER_LINE_DETAIL = (
    "hand text holds a conflict-marker line (seven = < > or |); the generator "
    "skips this file — indent or lengthen the line"
)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def iter_project_dirs(root) -> Iterator[Path]:
    """The project directories under *root*, in the shared walk's order.

    A top-level dir is a project when the §1 marker test says so; a dotted
    top-level dir whose own directory is not project-shaped is a host bucket
    and is descended (at any depth, never by depth) until the first
    project-shaped dir on each branch. Hidden and ``_``-prefixed TOP-LEVEL
    names are the instance's own areas, never projects — below the root a
    ``_`` component comes from the remote and is descended like any other —
    and symlinks are never followed (a ``{host}/{project}`` compat link
    points at a project already yielded, §9). The rules and their tests are
    :mod:`lmer_schema.checks.walk`'s; this module only asks.
    """
    for project in project_dirs(root):
        yield project.path


def iter_index_dirs(root, *, projects: Optional[Iterable] = None) -> Iterator[Path]:
    """The index dirs (``_specs``/``_plans`` and their legacy twins) of every
    project under *root*, or of *projects* when the caller supplies them.

    Supplied paths may be absolute or root-relative; either way the finding
    paths and the host-bucket tier are derived from the path relative to
    *root*, so caller-supplied and discovered projects behave identically.
    """
    root = Path(root)
    found = iter_project_dirs(root) if projects is None else _resolve_projects(root, projects)
    for project in found:
        for name in INDEX_DIR_NAMES:
            candidate = project / name
            if candidate.is_dir() and not candidate.is_symlink():
                yield candidate


def _resolve_projects(root: Path, projects: Iterable) -> Iterator[Path]:
    for project in projects:
        project = Path(project)
        yield project if project.is_absolute() else root / project


# ---------------------------------------------------------------------------
# Symlink integrity
# ---------------------------------------------------------------------------


#: What each fault of an index link is called and how it reads. The three
#: questions themselves are :mod:`lmer_schema.checks.symlinks`' (the support
#: module the session-dir artifact row consumes too); the codes and the words
#: stay here, with the surface they are about.
_LINK_FINDINGS = {
    symlinks.NOT_RELATIVE: (
        "index.link-not-relative",
        "index symlink target is absolute; index links must be relative",
    ),
    symlinks.ESCAPES: (
        "index.link-escapes",
        "index symlink resolves outside the plexus root",
    ),
    symlinks.DANGLING: (
        "index.dangling-link",
        "index symlink target does not exist",
    ),
}


def check_symlinks(root, report: Report, index_dir) -> None:
    """Check every symlink directly inside *index_dir*: relative, inside the
    plexus, resolving — in that order, at most one finding per link."""
    root = Path(root)
    index_dir = Path(index_dir)
    form = _tier_form(root, index_dir)
    try:
        children = sorted(index_dir.iterdir())
    except OSError:
        return
    for link in children:
        fault = symlinks.link_fault(root, link)
        if fault is None:
            continue
        code, message = _LINK_FINDINGS[fault.kind]
        report.add(
            code,
            _rel(root, link),
            message,
            detail=f"target: {fault.target}",
            form=form,
        )


# ---------------------------------------------------------------------------
# The PLANS.md line index
# ---------------------------------------------------------------------------


class IndexEntry:
    """One ``PLANS.md`` entry: a relative markdown link to a ``.md`` file on
    line *line* (SCHEMA.md "Index lines"). ``tombstone`` marks a link
    re-pointed at an archive home — the line is the plan's index line still,
    but the file it names is deliberately gone from the live tree."""

    __slots__ = ("line", "target", "tombstone")

    def __init__(self, line: int, target: str, tombstone: bool):
        self.line = line
        self.target = target
        self.tombstone = tombstone

    @property
    def name(self) -> str:
        """The basename the entry carries — how a line and a plan file meet."""
        return PurePosixPath(self.target).name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        kind = "tombstone" if self.tombstone else "entry"
        return f"<IndexEntry {kind} line={self.line} target={self.target!r}>"


def index_entries(text: str) -> List[IndexEntry]:
    """The entries of an index document. A line with no relative ``.md`` link
    — a heading, prose, a tombstone that dropped its link, an off-tree URL —
    is not an entry and can never be a finding; a line may carry several."""
    entries: List[IndexEntry] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in LINK_RE.finditer(line):
            target = match.group("target")
            if not _is_relative_md_link(target):
                continue
            entries.append(
                IndexEntry(number, target, tombstone=_is_tombstone_target(target))
            )
    return entries


def _is_relative_md_link(target: str) -> bool:
    if target.startswith(("/", "#")) or _SCHEME_RE.match(target):
        return False
    path = target.split("#", 1)[0].split("?", 1)[0]
    return path.lower().endswith(".md")


def _is_tombstone_target(target: str) -> bool:
    parts = PurePosixPath(target.split("#", 1)[0]).parts
    return any(part in ARCHIVE_NAMES for part in parts)


def check_plans_index(root, report: Report, plans_dir) -> None:
    """Check ``PLANS.md`` against the plan files beside it, both directions."""
    root = Path(root)
    plans_dir = Path(plans_dir)
    form = _tier_form(root, plans_dir)
    index = plans_dir / PLANS_INDEX
    entries: List[IndexEntry] = []
    if index.is_file():
        try:
            text = index.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        entries = index_entries(text)
        index_path = _rel(root, index)
        for entry in entries:
            if entry.tombstone:
                continue
            if not (plans_dir / entry.target).exists():
                report.add(
                    "index.entry-without-file",
                    index_path,
                    f"{PLANS_INDEX} entry links to a plan file that does not exist",
                    detail=f"line {entry.line}: {entry.target}",
                    form=form,
                )
    carried = {entry.name for entry in entries}
    for plan in _plan_files(plans_dir):
        if plan.name in carried:
            continue
        report.add(
            "index.file-without-entry",
            _rel(root, plan),
            f"plan file has no {PLANS_INDEX} entry",
            form=form,
        )


def _plan_files(plans_dir: Path) -> List[Path]:
    """The plan files of *plans_dir*: real markdown files beside the index.
    The dated run-plan links are symlinks — the index mechanic, not plans —
    and the index itself is not one of the things it carries."""
    try:
        children = sorted(plans_dir.iterdir())
    except OSError:
        return []
    return [
        child
        for child in children
        if child.is_file()
        and not child.is_symlink()
        and child.name != PLANS_INDEX
        and child.suffix.lower() == ".md"
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def check_index_links(
    root, report: Report, *, projects: Optional[Iterable] = None, manifest=None
) -> None:
    """Run the index checks over the plexus at *root*, adding to *report*.

    *projects* is the caller's project-dir list (the runner has one already);
    omitted, the projects are discovered. *manifest* is the loaded
    :class:`~lmer_schema.manifest.Manifest`, which gates the README index
    codes (:func:`check_readme_index`): without one — a legacy tree, or a
    caller that did not pass it — those say nothing. A root that does not
    exist says nothing: "there is no tree" is the runner's exit-2 call, not
    a finding.
    """
    root = Path(root)
    if not root.is_dir():
        return
    for index_dir in iter_index_dirs(root, projects=projects):
        check_symlinks(root, report, index_dir)
        if index_dir.name in PLANS_DIR_NAMES:
            check_plans_index(root, report, index_dir)
    check_readme_index(root, report, projects=projects, manifest=manifest)


# ---------------------------------------------------------------------------
# The generated README index blocks
# ---------------------------------------------------------------------------


def line_delta(old: str, new: str) -> Tuple[int, int]:
    """``(added, removed)`` line counts between two texts (pure).

    The ``+N/-M`` a stale finding's detail quotes and the ``lmer-schema
    index`` action line prints — the same line-level matching
    ``difflib.unified_diff`` groups into hunks, so the two never disagree.
    """
    matcher = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False)
    added = removed = 0
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += old_end - old_start
        if tag in ("replace", "insert"):
            added += new_end - new_start
    return added, removed


def check_readme_index(
    root, report: Report, *, projects: Optional[Iterable] = None, manifest=None
) -> None:
    """Compare every generated README index block with a fresh render.

    Gated on *manifest*: the index exists only in schema form, so a legacy
    tree — or a caller that passes none — hears nothing. For each project
    dir (*projects*, else the shared walk) the records under ``_runs/`` are
    read exactly as the generator reads them, from what is on disk with no
    git, and rendered with the same kernel; the root ``README.md`` is
    compared with the render of the manifest's project list, and only when
    it already carries a block. Clock-free, lock-free, write-free.
    """
    if manifest is None:
        return
    root = Path(root)
    if not root.is_dir():
        return
    from work_repo import project_index  # lazy: the import direction is work_repo -> lmer_schema

    found = iter_project_dirs(root) if projects is None else _resolve_projects(root, projects)
    for project in found:
        _check_project_readme(root, report, Path(project), project_index)
    _check_root_readme(root, report, manifest.document, project_index)


def _check_project_readme(root: Path, report: Report, project: Path, project_index) -> None:
    rel = f"{_rel(root, project)}/{README_NAME}"
    form = _tier_form(root, project)
    text, obstacle = _readme_text(project)
    try:
        block = None if text is None else project_index.extract_block(text)
    except project_index.MalformedMarkers as exc:
        _malformed(report, rel, exc, form)
        return
    if block is None:
        if not project_index.scan_project(project):
            return  # nothing to list: the block is created with the first record's push
        if text is not None and project_index.hand_text_has_marker(text):
            obstacle = MARKER_LINE_DETAIL
        report.add(
            "index.readme-missing",
            rel,
            f"project holds run records but its {README_NAME} carries no generated "
            "index block; the project's next durability push or `lmer-schema index "
            "--write` creates it",
            detail=obstacle,
            form=form,
        )
        return
    token = project_index.block_format(block)
    if token is not None and token > project_index.FORMAT:
        return  # a newer runtime wrote it; this one neither rewrites nor judges it
    if project_index.hand_text_has_marker(text):
        report.add(
            "index.readme-stale",
            rel,
            f"{README_NAME} hand text holds a conflict-marker line, so no push "
            "will refresh its generated index block",
            detail=MARKER_LINE_DETAIL,
            form=form,
        )
        return
    fresh = project_index.render_project_block(project_index.scan_project(project))
    if block != fresh:
        _stale(report, rel, block, fresh, "the run records under `_runs/`", form)


def _check_root_readme(root: Path, report: Report, document, project_index) -> None:
    text, _obstacle = _readme_text(root)
    if text is None:
        return  # the root block is optional: only a present block is judged
    try:
        block = project_index.extract_block(text)
    except project_index.MalformedMarkers as exc:
        _malformed(report, README_NAME, exc, None)
        return
    if block is None:
        return
    token = project_index.block_format(block)
    if token is not None and token > project_index.FORMAT:
        return
    fresh = project_index.render_root_block(document)
    if block != fresh:
        _stale(report, README_NAME, block, fresh, "the manifest's project list", None)


def _malformed(report: Report, rel: str, exc: Exception, form: Optional[Form]) -> None:
    report.add(
        "index.readme-stale",
        rel,
        "generated index block markers are malformed; the generator refuses the "
        "file until one start and one end marker delimit one block",
        detail=f"markers malformed ({exc})",
        form=form,
    )


def _stale(
    report: Report, rel: str, block: str, fresh: str, source: str, form: Optional[Form]
) -> None:
    added, removed = line_delta(block, fresh)
    report.add(
        "index.readme-stale",
        rel,
        f"generated index block no longer matches a fresh render of {source}",
        detail=(
            f"+{added}/-{removed} lines; lmer-schema index <root> --diff shows the "
            "change, --write applies it"
        ),
        form=form,
    )


def _readme_text(directory: Path) -> Tuple[Optional[str], Optional[str]]:
    """``(text, obstacle)`` for the README of *directory*: its text when it is
    a regular UTF-8 file, else ``None`` and — unless it is simply absent —
    why the generator could not write one there either."""
    path = directory / README_NAME
    if path.is_symlink():
        return None, f"{README_NAME} is a symlink; the generator skips it"
    if path.is_dir():
        return None, f"{README_NAME} is a directory; the generator skips it"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        reason = exc.strerror or exc
        return None, f"{README_NAME} is not readable ({reason}); the generator skips it"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, f"{README_NAME} is not UTF-8 text; the generator skips it"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _rel(root: Path, path: Path) -> str:
    """*path* as a repo-relative POSIX string (what a finding carries)."""
    return os.path.relpath(os.fspath(path), os.fspath(root)).replace(os.sep, "/")


def _tier_form(root: Path, path: Path) -> Optional[Form]:
    """The form override for findings about *path*: a host bucket's contents
    are checked at the legacy tier's severities in either form (SCHEMA.md
    "Outside the schema"). ``None`` elsewhere — the report's own form."""
    head = _rel(root, path).split("/", 1)[0]
    return Form.LEGACY if registry.is_host_bucket_name(head) else None
