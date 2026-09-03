"""The ``_agents/`` runtime surface and agent memory at both scopes
(spec §3.4/§3.6, D14/D17/D18; SCHEMA.md "agents", "memory", "Agent surface").

One check module owns both because they are one surface: `_agents/` holds
harness configuration, the vendored mirrors and the memory scope, and the two
rules that make it a schema surface — ``memory``/``mirrors`` are RESERVED and
never harness names (D18), and a memory's ``metadata.type`` selects the SCOPE
it belongs in (D17) — are two halves of the same shape. The module walks the
instance surface (``<root>/_agents/``) and every project's
(``<project>/_agents/``), plus the bare-named legacy stand-ins.

What is walked, and what is deliberately not
============================================
- ``<scope>/<harness>/`` is **opaque**: ``claude/`` is the harness's own
  layout (skills, commands, hooks) and this module never descends into it.
  Only the NAME is checked — a name that is neither a known harness nor a
  reserved surface is a WARN (SCHEMA.md: "an unknown first-level name is a
  warning"), which is the one nudge that catches ``_agents/claud/``.
- ``<scope>/mirrors/`` is **opaque** in the strong sense (D12's rule applied
  to a mirror): a mirror is a FOREIGN writer's tree, so nothing inside it is
  ever read, let alone a finding. A ``MEMORY.md`` inside a vendored fork is
  that fork's file, not this plexus's index.
- ``<scope>/memory/`` is machine-owned and fully walked: every ``*.md`` is a
  memory file with required frontmatter, and ``MEMORY.md`` is its index.

Reserved names, and how a validator can SEE the rule
====================================================
"A harness named ``memory`` or ``mirrors`` is an error" is a rule about a
name, and a name on disk is just a directory — so the check is about a
reserved name being used as something other than the surface it names. Two
observable cases, both ERROR (:data:`RESERVED_HARNESS_NAME`):

1. the reserved name is not a directory at all (``_agents/mirrors`` as a
   file) — whatever it is, it is not the reserved surface;
2. ``<scope>/memory/`` holds harness CONFIGURATION (a ``skills/``,
   ``commands/`` or ``hooks/`` directory) and no memory at all — someone
   used the reserved name as a harness, exactly the sentence's case.

There is deliberately no equivalent heuristic for ``mirrors/``: its contents
are opaque, so looking inside it to judge it would break the stronger rule.
A mirror named ``skills`` is a plausible vendored fork, and guessing about a
foreign writer's tree is how a validator earns its false positives.

Severities (D14)
================
The two tier-sensitive codes are already in :mod:`lmer_schema.checks.findings`
(the D14 table): ``agents.memory-entry-without-file`` (ERROR → WARN in a
legacy tree) and ``agents.memory-scope-mismatch`` (WARN → INFO). This module
CONSUMES them — re-registering a code raises — and registers the rest, none
of which is tier-sensitive: a reserved name used as a harness and malformed
frontmatter are SHAPE violations, and D14 softens integrity findings only.
:func:`_register` states the whole vocabulary in one place and skips what is
already registered, so the module's codes are readable here without the
import order deciding anything.

A host bucket's contents are checked at the legacy tier's severities in
either form (SCHEMA.md "Outside the schema"), which is what the per-call
``form=`` override of :meth:`Report.add` exists for. This module never
derives that override: every project dir it checks — discovered or handed in
by the runner — is a :class:`~lmer_schema.checks.walk.ProjectDir` CARRYING
the form its contents are checked at, and ``check_agents`` passes it through.
That is why ``project_dirs=`` no longer flattens a bucket to the report's own
form (!256 discussion 5), and why the runner needs no report view to pin the
tier back.

Which dirs are projects is not this module's question either: it is
:mod:`lmer_schema.checks.walk`'s, the ONE walk every per-project check module
shares. :func:`iter_project_dirs` is the module-local name for it, so the
runner's "nobody re-discovers" guard still has something to hold.

The index-line rule
===================
``MEMORY.md`` is a line index and SCHEMA.md's "Index lines" defines an entry
for the validator: a line carrying a RELATIVE markdown link to a ``.md``
file, resolved against the index's own directory. Anything else — a heading,
prose, a line whose link was replaced by ``archived``/``forgotten`` and a
date, or a link re-pointed at the ``_archive/`` mirror — is a tombstone or
commentary, never an entry and never a finding. ``PLANS.md`` obeys the same
rule under :mod:`lmer_schema.checks.index_links`; the two parsers are
deliberately not shared yet (the modules landed in parallel) — see the digest
note about folding them together.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import yaml

from lmer_schema.checks.findings import CHECKS, Form, Severity, Tier, register
from lmer_schema.checks.walk import ProjectDir, project_dirs

__all__ = [
    "AGENTS_DIR",
    "AGENT_CODES",
    "HARNESS_MARKERS",
    "INSTANCE_SCOPE",
    "KNOWN_HARNESSES",
    "LEGACY_INSTANCE_SURFACES",
    "LEGACY_MEMORY_DIR",
    "LEGACY_SURFACE",
    "MEMORY_DIR",
    "MEMORY_ENTRY_WITHOUT_FILE",
    "MEMORY_FILE_WITHOUT_ENTRY",
    "MEMORY_FRONTMATTER",
    "MEMORY_INDEX",
    "MEMORY_INDEX_MISSING",
    "MEMORY_SCOPE_MISMATCH",
    "MEMORY_TYPES",
    "MEMORY_TYPE_UNKNOWN",
    "MEMORY_UNKNOWN_FILE",
    "MIRRORS_DIR",
    "PROJECT_SCOPE",
    "RESERVED_HARNESS_NAME",
    "RESERVED_NAMES",
    "UNKNOWN_HARNESS",
    "check_agents",
    "iter_project_dirs",
    "parse_frontmatter",
    "parse_index_entries",
    "scope_for_type",
]

# ---------------------------------------------------------------------------
# The surface's vocabulary (§3.6, SCHEMA.md "Agent surface")
# ---------------------------------------------------------------------------

#: The agent surface's directory name at either scope (D18).
AGENTS_DIR = "_agents"

#: The two reserved first-level names under it — never harness names.
MEMORY_DIR = "memory"
MIRRORS_DIR = "mirrors"
RESERVED_NAMES = (MEMORY_DIR, MIRRORS_DIR)

#: The harnesses defined at schema 1 (SCHEMA.md: `claude` is the only one).
KNOWN_HARNESSES = ("claude",)

#: Directories that make a dir harness CONFIGURATION rather than memory —
#: the only way "a harness named `memory`" is visible on disk.
HARNESS_MARKERS = ("skills", "commands", "hooks", "agents")

#: The per-scope memory index (§3.4).
MEMORY_INDEX = "MEMORY.md"

#: The bare instance surfaces §9 step 3 migrates, recognised in either form.
LEGACY_INSTANCE_SURFACES = ("agent-files", "mirrors")

#: The bare project memory dir of a pre-D17 tree.
LEGACY_MEMORY_DIR = "memory"

#: The two memory scopes (D17).
INSTANCE_SCOPE = "instance"
PROJECT_SCOPE = "project"

#: The `metadata.type` vocabulary, in the order SCHEMA.md's table lists it.
MEMORY_TYPES = ("user", "feedback", "project", "reference")

#: D17's routing table: the scope each type belongs in.
TYPE_SCOPES: Dict[str, str] = {
    "user": INSTANCE_SCOPE,
    "feedback": INSTANCE_SCOPE,
    "reference": INSTANCE_SCOPE,
    "project": PROJECT_SCOPE,
}

#: Frontmatter keys every memory file must carry (SCHEMA.md "memory").
REQUIRED_FRONTMATTER = ("name", "description")

#: An `_archive/` mirror component in an index link: a tombstone, not an entry.
ARCHIVE_MARKERS = ("_archive", "archive")

#: A markdown link on an index line: ``[text](target)``.
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

#: A link this module never resolves: an absolute path, a URL, an anchor.
_NOT_RELATIVE_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|/|#)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Codes
# ---------------------------------------------------------------------------

UNKNOWN_HARNESS = "agents.unknown-harness"
RESERVED_HARNESS_NAME = "agents.reserved-harness-name"
MEMORY_FRONTMATTER = "agents.memory-frontmatter"
MEMORY_TYPE_UNKNOWN = "agents.memory-type-unknown"
MEMORY_UNKNOWN_FILE = "agents.memory-unknown-file"
MEMORY_INDEX_MISSING = "agents.memory-index-missing"
MEMORY_FILE_WITHOUT_ENTRY = "agents.memory-file-without-entry"
#: Pre-registered in the D14 table (findings.py); consumed, never re-registered.
MEMORY_ENTRY_WITHOUT_FILE = "agents.memory-entry-without-file"
MEMORY_SCOPE_MISMATCH = "agents.memory-scope-mismatch"
LEGACY_SURFACE = "agents.legacy-surface"

#: Every code this module emits, in emit order — the module's contract.
AGENT_CODES = (
    UNKNOWN_HARNESS,
    RESERVED_HARNESS_NAME,
    MEMORY_FRONTMATTER,
    MEMORY_TYPE_UNKNOWN,
    MEMORY_UNKNOWN_FILE,
    MEMORY_INDEX_MISSING,
    MEMORY_FILE_WITHOUT_ENTRY,
    MEMORY_ENTRY_WITHOUT_FILE,
    MEMORY_SCOPE_MISMATCH,
    LEGACY_SURFACE,
)


def _register(code, severity, *, tier, summary, legacy=None):
    """Register *code* unless the D14 table already did.

    ``findings.py`` pre-registers the two tier-sensitive ``agents.*`` codes
    and registering a code twice raises, so this module states its whole
    vocabulary and consumes what is already there — which also makes a module
    reload (a test, an importlib dance) harmless.
    """
    existing = CHECKS.get(code)
    if existing is not None:
        return existing
    return register(code, severity, tier=tier, summary=summary, legacy=legacy)


_register(
    UNKNOWN_HARNESS,
    Severity.WARN,
    tier=Tier.MACHINE,
    summary=(
        "a first-level name under `_agents/` that is neither a known harness "
        "nor the reserved `memory`/`mirrors`"
    ),
)
_register(
    RESERVED_HARNESS_NAME,
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary=(
        "the reserved `_agents/` name `memory` or `mirrors` used as something "
        "else — a harness configuration dir, or not a directory at all"
    ),
)
_register(
    MEMORY_FRONTMATTER,
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary=(
        "a memory file whose YAML frontmatter is missing, malformed or short "
        "of `name`, `description` or `metadata.type`"
    ),
)
_register(
    MEMORY_TYPE_UNKNOWN,
    Severity.ERROR,
    tier=Tier.MACHINE,
    summary="a memory whose `metadata.type` is outside the D17 vocabulary",
)
_register(
    MEMORY_UNKNOWN_FILE,
    Severity.WARN,
    tier=Tier.MACHINE,
    summary="a file in a memory directory that is not a memory or its index",
)
_register(
    MEMORY_INDEX_MISSING,
    Severity.WARN,
    tier=Tier.MACHINE,
    summary="a memory directory holding memory files but no `MEMORY.md` index",
)
_register(
    MEMORY_FILE_WITHOUT_ENTRY,
    Severity.WARN,
    tier=Tier.MACHINE,
    summary="a memory file with no `MEMORY.md` entry",
)
_register(
    MEMORY_ENTRY_WITHOUT_FILE,
    Severity.ERROR,
    legacy=Severity.WARN,
    tier=Tier.MACHINE,
    summary="a `MEMORY.md` entry whose linked memory file does not exist",
)
_register(
    MEMORY_SCOPE_MISMATCH,
    Severity.WARN,
    legacy=Severity.INFO,
    tier=Tier.MACHINE,
    summary=(
        "a memory whose `metadata.type` does not select the scope it lives in "
        "(D17; recall unions both scopes, so nothing is lost)"
    ),
)
_register(
    LEGACY_SURFACE,
    Severity.INFO,
    tier=Tier.LEGACY,
    summary=(
        "a bare `agent-files/`, `mirrors/` or `<project>/memory/` surface: "
        "recognised legacy, read in place, never required to move"
    ),
)


# ---------------------------------------------------------------------------
# Small public helpers
# ---------------------------------------------------------------------------


def scope_for_type(kind) -> Optional[str]:
    """D17: the scope *kind* belongs in, or ``None`` for an unknown type."""
    return TYPE_SCOPES.get(kind)


def parse_frontmatter(text) -> Dict:
    """The YAML frontmatter of *text* as a mapping.

    Raises ``ValueError`` — with the message the finding carries — when the
    fence is missing or unterminated, the document does not parse, or it is
    not a mapping. The body is free-form and is not returned: no check reads
    it.
    """
    if not text.startswith("---\n"):
        raise ValueError("no YAML frontmatter (the file must open with `---`)")
    end = text.find("\n---", 4)
    if end == -1:
        raise ValueError("unterminated YAML frontmatter (no closing `---`)")
    try:
        data = yaml.safe_load(text[4:end])
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML frontmatter does not parse: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("YAML frontmatter is not a mapping")
    return data


def parse_index_entries(text) -> List[str]:
    """The entry link targets of a ``MEMORY.md``/``PLANS.md`` body.

    SCHEMA.md "Index lines": an entry is a line carrying a RELATIVE markdown
    link to a ``.md`` file. A heading, prose, a tombstoned line (the link
    replaced by ``archived``/``forgotten`` and a date) and a link re-pointed
    at the ``_archive/`` mirror carry no entry, so they are returned by
    nobody and are never a finding.
    """
    targets = []
    for line in text.splitlines():
        for target in _LINK_RE.findall(line):
            if not target.lower().endswith(".md"):
                continue
            if _NOT_RELATIVE_RE.match(target):
                continue
            parts = posixpath.normpath(target).split("/")
            if any(part in ARCHIVE_MARKERS for part in parts):
                continue
            targets.append(target)
    return targets


@dataclass(frozen=True)
class _Scope:
    """One memory scope on disk: where it is, which of the two scopes it is,
    and the per-call form override its tree imposes (a host bucket)."""

    path: Path
    scope: str
    form: Optional[Form]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def iter_project_dirs(root) -> Tuple[ProjectDir, ...]:
    """Every project dir under *root*, each carrying the form its contents
    are checked at — the shared walk
    (:func:`lmer_schema.checks.walk.project_dirs`).

    A registry-free reading, which is all this module can have on its own: a
    dir is a project by the §1 marker test, a bucket's is found inside a
    dotted top-level name at any depth, and a bucket carries
    :attr:`Form.LEGACY` because its contents are checked at the legacy tier's
    severities. The runner passes its own list instead, built from the same
    walk WITH the registry, so the two paths differ by inclusion only.
    """
    return project_dirs(root)


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


def check_agents(root, report, *, project_dirs: Optional[Iterable] = None) -> None:
    """Check the agent surface and memory of the plexus at *root*.

    Findings go into *report*, which carries the tree's form; nothing is
    returned and nothing is written. *project_dirs* overrides discovery for a
    caller that already walked the tree — an iterable of project dirs, each
    checked at the ``form`` IT carries (a
    :class:`~lmer_schema.checks.walk.ProjectDir` pins a host bucket to the
    legacy tier; a plain path leaves the tier to the report). Pass ``()`` to
    check the instance scope only.
    """
    root = Path(root)
    _check_instance(root, report)
    scopes = iter_project_dirs(root) if project_dirs is None else project_dirs
    for project in scopes:
        _check_project(
            root, Path(project), report, form=getattr(project, "form", None)
        )


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _check_instance(root: Path, report) -> None:
    surface = root / AGENTS_DIR
    if surface.is_dir():
        _check_surface(root, surface, INSTANCE_SCOPE, report, form=None)
    for name in LEGACY_INSTANCE_SURFACES:
        path = root / name
        if path.is_dir():
            _legacy_surface(root, path, report, form=None)


def _check_project(root: Path, project_dir: Path, report, *, form) -> None:
    surface = project_dir / AGENTS_DIR
    if surface.is_dir():
        _check_surface(root, surface, PROJECT_SCOPE, report, form=form)
    legacy_memory = project_dir / LEGACY_MEMORY_DIR
    if legacy_memory.is_dir():
        _legacy_surface(root, legacy_memory, report, form=form)
        # Read in place: a bare memory dir is still this project's memory.
        _check_memory(root, _Scope(legacy_memory, PROJECT_SCOPE, form), report)


def _legacy_surface(root: Path, path: Path, report, *, form) -> None:
    report.add(
        LEGACY_SURFACE,
        _rel(root, path),
        f"`{path.name}/` is a recognised legacy agent surface, read in place",
        detail="§9 migrates it to the `_agents/` surface; it is never required to move",
        form=form,
    )


def _check_surface(root: Path, surface: Path, scope: str, report, *, form) -> None:
    """The `<scope>/` row: first-level names only. Harness dirs and mirrors
    are opaque; only `memory/` is walked."""
    for entry in sorted(surface.iterdir()):
        name = entry.name
        if name.startswith("."):
            continue
        if name in RESERVED_NAMES:
            _check_reserved(root, entry, scope, report, form=form)
            continue
        if name in KNOWN_HARNESSES and entry.is_dir():
            continue  # opaque: the harness's own layout
        report.add(
            UNKNOWN_HARNESS,
            _rel(root, entry),
            f"`{name}` is not a known harness under `{AGENTS_DIR}/`",
            detail=f"harnesses at schema 1: {', '.join(KNOWN_HARNESSES)}",
            form=form,
        )


def _check_reserved(root: Path, entry: Path, scope: str, report, *, form) -> None:
    """`memory` and `mirrors` are reserved (D18): they are those surfaces or
    they are an error (module docstring)."""
    if not entry.is_dir():
        report.add(
            RESERVED_HARNESS_NAME,
            _rel(root, entry),
            f"`{entry.name}` is reserved under `{AGENTS_DIR}/` and is not a directory",
            detail="reserved names: " + ", ".join(RESERVED_NAMES),
            form=form,
        )
        return
    if entry.name == MIRRORS_DIR:
        return  # opaque: a foreign writer's tree, existence is the whole check
    if _looks_like_harness(entry):
        report.add(
            RESERVED_HARNESS_NAME,
            _rel(root, entry),
            "`memory` is reserved and can never be a harness name",
            detail=(
                "this directory holds harness configuration "
                f"({', '.join(HARNESS_MARKERS)}) and no memory files"
            ),
            form=form,
        )
        return
    _check_memory(root, _Scope(entry, scope, form), report)


def _looks_like_harness(path: Path) -> bool:
    """Whether *path* is harness configuration rather than a memory dir: it
    holds a harness-config directory and no memory file at all."""
    if not any((path / marker).is_dir() for marker in HARNESS_MARKERS):
        return False
    return not any(child.suffix == ".md" for child in path.iterdir() if child.is_file())


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def _check_memory(root: Path, scope: _Scope, report) -> None:
    files: List[Path] = []
    for entry in sorted(scope.path.iterdir()):
        if entry.name.startswith(".") or entry.name == MEMORY_INDEX:
            continue
        if entry.is_dir() or entry.suffix != ".md":
            report.add(
                MEMORY_UNKNOWN_FILE,
                _rel(root, entry),
                f"`{entry.name}` is not a memory file or its index",
                detail=f"a memory directory holds `<name>.md` files and {MEMORY_INDEX}",
                form=scope.form,
            )
            continue
        files.append(entry)

    for path in files:
        _check_memory_file(root, path, scope, report)
    _check_memory_index(root, scope, files, report)


def _check_memory_file(root: Path, path: Path, scope: _Scope, report) -> None:
    rel = _rel(root, path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.add(
            MEMORY_FRONTMATTER,
            rel,
            "memory file could not be read",
            detail=str(exc),
            form=scope.form,
        )
        return
    try:
        data = parse_frontmatter(text)
    except ValueError as exc:
        report.add(MEMORY_FRONTMATTER, rel, str(exc), form=scope.form)
        return

    for key in REQUIRED_FRONTMATTER:
        if not _is_non_empty_str(data.get(key)):
            report.add(
                MEMORY_FRONTMATTER,
                rel,
                f"memory frontmatter is missing the required key `{key}`",
                detail="required: " + ", ".join(REQUIRED_FRONTMATTER + ("metadata.type",)),
                form=scope.form,
            )
            return

    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        report.add(
            MEMORY_FRONTMATTER,
            rel,
            "memory frontmatter has no `metadata` mapping",
            detail="`metadata.type` selects the memory's scope (D17)",
            form=scope.form,
        )
        return
    kind = metadata.get("type")
    if not _is_non_empty_str(kind):
        report.add(
            MEMORY_FRONTMATTER,
            rel,
            "memory frontmatter is missing the required key `metadata.type`",
            detail="one of: " + ", ".join(MEMORY_TYPES),
            form=scope.form,
        )
        return
    # Extra `metadata` keys are PERMITTED and never a finding (SCHEMA.md).

    expected = scope_for_type(kind)
    if expected is None:
        report.add(
            MEMORY_TYPE_UNKNOWN,
            rel,
            f"`metadata.type: {kind}` is not a memory type",
            detail="one of: " + ", ".join(MEMORY_TYPES),
            form=scope.form,
        )
        return
    if expected != scope.scope:
        report.add(
            MEMORY_SCOPE_MISMATCH,
            rel,
            f"a `{kind}` memory belongs in the {expected} scope, not the {scope.scope} one",
            detail="recall unions both scopes, so nothing is lost (D17)",
            form=scope.form,
        )


def _check_memory_index(root: Path, scope: _Scope, files: List[Path], report) -> None:
    index = scope.path / MEMORY_INDEX
    if not index.is_file():
        if files:
            report.add(
                MEMORY_INDEX_MISSING,
                _rel(root, scope.path),
                f"memory files but no {MEMORY_INDEX} index",
                detail=f"{len(files)} memory file(s) in this scope",
                form=scope.form,
            )
        return
    rel_index = _rel(root, index)
    try:
        text = index.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        report.add(
            MEMORY_UNKNOWN_FILE,
            rel_index,
            f"{MEMORY_INDEX} could not be read",
            detail=str(exc),
            form=scope.form,
        )
        return

    listed = set()
    for target in parse_index_entries(text):
        resolved = (scope.path / target).resolve()
        if resolved.is_file():
            listed.add(resolved)
            continue
        report.add(
            MEMORY_ENTRY_WITHOUT_FILE,
            rel_index,
            f"{MEMORY_INDEX} entry has no memory file",
            detail=target,
            form=scope.form,
        )
    for path in files:
        if path.resolve() not in listed:
            report.add(
                MEMORY_FILE_WITHOUT_ENTRY,
                _rel(root, path),
                f"memory file has no {MEMORY_INDEX} entry",
                detail=f"index: {rel_index}",
                form=scope.form,
            )


def _is_non_empty_str(value) -> bool:
    return isinstance(value, str) and value.strip() != ""
