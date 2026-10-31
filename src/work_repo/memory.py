"""Agent memory persistence to the work repository.

Claude Code stores per-session agent memory under
``~/.claude/projects/-workspace/memory/`` (the ``-workspace`` segment encodes
the ``/workspace`` cwd used inside the container). That directory lives only for
the container's lifetime, so memory written during one session is lost when the
container exits.

When ``LMER_PERSIST_AGENT_MEMORY`` is enabled we copy that directory into the
work repo so it survives across sessions.

Two halves, by design:

* **Restore** (work repo -> Claude memory dir) runs automatically at session
  start from ``libexec/claude-runner.sh`` via ``work memory restore`` so the
  saved memory is on disk before Claude reads it.
* **Persist** (Claude memory dir -> work repo, then committed and pushed) is the
  agent's responsibility via ``work memory persist`` (documented in AGENTS.md).
  We intentionally do not auto-persist on session end — the agent decides what
  is worth keeping and runs the command before finishing.

Two scopes, routed by type (spec D17)
=====================================
A legacy work repo has ONE memory home, ``{host}/{project}/memory/``, and every
memory lands there whatever it is about. A schema-form tree has two, and a
memory's own ``metadata.type`` says which one owns it: ``project`` belongs to
the project scope (:func:`work_repo.utils.project_memory_dir`), while ``user``,
``feedback`` and ``reference`` are true of the operator rather than of one
project and belong to the instance scope at the plexus root
(:func:`work_repo.utils.instance_memory_dir`). Restore reads the **union**,
project overlaying instance on a ``name`` collision; each scope keeps its own
``MEMORY.md``. Which form the tree is in is not decided here — the presence of
a second scope IS the form (``instance_memory_dir()`` is ``None`` in a legacy
tree), so this module routes on that one fact and never names a manifest.

An unroutable file — no frontmatter, frontmatter that does not parse, no
``metadata.type``, a non-scalar one, or a type outside the vocabulary — goes to
the **project** scope, out loud (:data:`DEFAULT_SCOPE`), and never raises: one
hand-edited file must not cost a session its whole memory. That destination is
the narrower blast radius: the instance scope is shared by every project's
sessions, and the project dir is where a pre-D17 tree already put every type.
Recall unions both scopes, so a misrouted memory loses nothing and is a hygiene
warning, not an error (``docs/SCHEMA.md``, "memory").

Restore mirrors; persist merges over a restore manifest (issue #369)
====================================================================
**Restore** mirrors: the work repo is the truth for what this session starts
with, so a file it no longer has is removed from the agent's dir.

**Persist** does NOT mirror. The work repo is a shared store — the project
scope is written by every session of one project, the instance scope by every
session of every project — and a mirror reads "absent from my directory" as
"deleted everywhere". It does not take a race: a session holding an older view
mirrors that view back over everyone else's work, which is how a deliberate
`MEMORY.md` repair was reverted line for line by an unrelated session's routine
persist 25 minutes later.

So persist deletes exactly ``restored_then_removed``: files THIS session
restored from THAT scope and that are now gone from its memory dir. A file this
session never saw is never deleted. The record of what was restored is the
**restore manifest** (:func:`restore_manifest_path`), written by
:func:`restore_memory` and updated by :func:`persist_memory`:

* per scope, the relative path and content digest of every file placed in the
  agent's dir, plus the index lines that scope contributed;
* it lives BESIDE the agent's memory dir, not inside it and not in the work
  repo. Inside the memory dir it would be routed and persisted as
  schema-visible state ``lmer-schema validate`` would have to learn to ignore
  (and it would make an empty memory dir look non-empty). Beside it, it shares
  the memory dir's exact lifetime — the two go stale together or not at all,
  which is the only way the manifest can be trusted to describe that dir;
* **no manifest degrades to "delete nothing"**, never to "delete everything".
  An older container, a restore that never ran (the runner continues past a
  failed one), a hand-made directory: all of them persist additively;
* a work-repo file whose bytes changed since this session restored it has been
  rewritten by someone newer, so this session's deletion does not carry.

The ``MEMORY.md`` index gets the same rule rather than being written back
wholesale (:func:`_merged_index_text`): the scope's current index is the base,
this session drops only the lines it restored and then removed, and adds only
the lines it did not restore. A session cannot delete an entry it never had,
and cannot resurrect one it never had either — which is the tombstone case.

Applying a whole-scope write across a SPLIT would be a different thing entirely
— writing a routed subset over a scope touches everything the other scope owns
— so persist works per scope, and two rules keep a deletion inside the scope it
happened in:

1. a scope this persist has nothing routed to is **not touched at all** (no
   copies, no deletions), announced on stderr rather than silently emptied;
2. an INSTANCE-scope file whose memory ``name`` is claimed by a file routed to
   the PROJECT scope is never deleted — restore overlays project on instance,
   so that file was shadowed out of the agent's memory dir and its absence is
   a shadow, not a deletion.

Rule 2 runs in that one direction only. Restore never shadows a *project*-scope
file, so when one's name is claimed by a file this persist routed to the
instance scope, the file is not shadowed: it is MOVING to the scope its type
owns (D17), or being deleted. Keeping it would leave a legacy-migrated tree —
where every type still sits in ``<project>/memory/`` — duplicated across both
scopes forever, with the project copy then overlaying every later instance-side
update made by another project's session.

Both rules still run under the manifest. ``_restored_then_removed`` bounds
implicit deletion to this session's own restored-and-removed set, while an
explicit schema-2 retirement is durable work-repo state. Its root-relative
scoped key filters restore and persist even for a client with no restore
baseline. :func:`forget_memory` moves content into the scope's ``_archive``
mirror and tombstones its index line; :func:`revive_memory` moves it back and
clears the marker. Schema 1 retains the baseline-only behavior above.

A restored file that disappears from the shared scope is also a decision from
another writer. An unchanged local copy is skipped, so stale persist cannot
revive it. A locally edited copy is an explicit conflict: its bytes remain in
the agent directory, the shared deletion remains in force, and revival must be
requested separately rather than inferred from persist.

The split index and its load budget (issue #435)
================================================
A harness loads only the head of ``MEMORY.md`` at session start (claude: 200
lines / 25 KB, :data:`lmer_cli.harness.CLAUDE_MEMORY_INDEX_LIMIT`), so a scope
may split its index: ``MEMORY.md`` becomes a MAP of one line per area, and the
entry lines live in ``areas/<slug>.md`` with links relative to that file. The
shape is the switch — a scope is split once ``areas/`` holds an area index —
so a flat scope behaves exactly as before. Every index file of a scope is
merged, filtered and reconciled by the rules above, each against its own
recorded lines. In a split scope an entry left in the map moves to its area
(:func:`_file_map_entries`) and a generated entry goes to an area, never the
map; a ``MEMORY.md`` line that links an area index is a map line, whatever its
hook links, and never an entry. Index files are merged, never deleted as
files: a manifest written before #435 lists area indexes under ``files``, and
that record never deletes one (:func:`_restored_then_removed`). Persist
reports the loaded index against the budget every time; a flat index over it
is warned, and a split scope's persist that grows an over-budget map is
refused and rolled back (:func:`_refuses_growth`). A badly migrated split
index — a non-slug file in ``areas/``, an area link to a memory missing its
``../`` — is judged once every scope is written, against what the persist
leaves in both scopes (every project's, for the shared instance scope), before
the reconcile (:func:`_settle_scope`, :func:`_area_problems`). A line or file
the session holds is refused and rolled back the same way; one another writer
added is warned about, and its dead link tombstoned. A cross-area link is
never refused, and is tombstoned only when no merged scope has the area. A
``forget`` tombstone is kept verbatim. A flat scope is never checked, and
reads an area link as an ordinary entry link — housekeep and migrate too.

Both halves are no-ops unless ``LMER_PERSIST_AGENT_MEMORY`` is truthy.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import posixpath
import re
import shutil
import sys
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, List, Optional, Set, TYPE_CHECKING

import yaml

from lmer_cli import gate_lock
from lmer_cli.harness import MemoryIndexLimit, memory_index_budget
from lmer_cli.memory_areas import AREAS_DIR, UNSORTED_AREA, is_area_slug
from lmer_cli.memory_areas import is_forget_tombstone as _is_forget_tombstone
from lmer_cli.util import get_bool_env

from .git_ops import check_commit_ready, commit_work_path, fast_forward_upstream
from .run_state import launch_session_id
from .transactions import (
    checkout_lock,
    inherited_transaction,
    mutation_transaction,
    work_transaction,
)
from .utils import (
    load_work_manifest,
    instance_memory_dir,
    project_memory_dir,
    work_repo_layout,
    work_repo_manifest_problem,
    work_repo_project_rels,
    work_repo_root,
)

if TYPE_CHECKING:
    from .utils import Manifest

PERSIST_ENV_VAR = "LMER_PERSIST_AGENT_MEMORY"

#: The per-scope line index (``docs/SCHEMA.md``, "memory"). In a split scope
#: it is the MAP: one line per area, and the only index loaded at start.
MEMORY_INDEX = "MEMORY.md"

#: A generated map line, as :func:`_map_line` writes it (hook included).
_MAP_LINE_RE = re.compile(r"^- \[([a-z0-9][a-z0-9-]*)\]\(areas/\1\.md\) — read when ")

#: The two scopes, spelled as ``lmer_schema.checks.agents`` spells them.
PROJECT_SCOPE = "project"
INSTANCE_SCOPE = "instance"

#: D17's routing table: the scope each ``metadata.type`` belongs in.
TYPE_SCOPES: Dict[str, str] = {
    "user": INSTANCE_SCOPE,
    "feedback": INSTANCE_SCOPE,
    "reference": INSTANCE_SCOPE,
    "project": PROJECT_SCOPE,
}

#: Where a memory whose type does not route goes (see the module docstring).
DEFAULT_SCOPE = PROJECT_SCOPE

#: A markdown link on an index line: ``[text](target)``.
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

#: A link that is not a relative path: an absolute path, a URL, an anchor.
_NOT_RELATIVE_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|/|#)", re.IGNORECASE)

#: An ``_archive/`` component in an index link: a tombstone, not an entry.
_ARCHIVE_MARKERS = ("_archive", "archive")

#: The restore manifest's filename (issue #369). Written BESIDE the agent's
#: memory dir — see :func:`restore_manifest_path`.
RESTORE_MANIFEST_NAME = ".lmer-memory-restore.json"

#: The manifest format. Bumped when the shape below stops being readable by
#: the code that wrote the previous one; an unrecognised version reads as no
#: manifest at all, which deletes nothing. Version 2 adds the `session` field
#: (#369 gap (a)): a manifest recorded under another launch is not this
#: session's baseline.
RESTORE_MANIFEST_VERSION = 2

#: A memory scope's store-format marker (#598): one integer, the newest
#: layout the scope's writer used — :data:`MEMORY_FORMAT_FLAT` or
#: :data:`MEMORY_FORMAT_SPLIT`. A missing marker is the flat format. It only
#: GUARDS: the shape (:func:`_is_split`) still decides how a scope is read,
#: and persist never writes the marker. A dotfile, so ``lmer-schema
#: validate`` skips it like every dot name, and :func:`_files_under` — the one
#: place it is excluded — keeps it out of restore, routing, every deletion
#: path and the memory count.
MEMORY_FORMAT_FILE = ".lmer-memory-format"
MEMORY_FORMAT_FLAT = 1
MEMORY_FORMAT_SPLIT = 2

#: The newest store format this lmer reads and writes. A scope whose marker
#: names a newer one is refused by every writer and warned about on restore.
MEMORY_FORMAT_MAX = MEMORY_FORMAT_SPLIT

#: Schema-2's durable, machine-readable retirement state at the plexus root.
RETIREMENT_STATE_NAME = "_memory_retirements.json"
RETIREMENT_STATE_VERSION = 1


class MemoryPersistConflict(Exception):
    """A locally edited restored memory was deleted from its shared scope."""


class MemoryRetirementError(Exception):
    """Schema-2 memory retirement state or an operation on it is invalid."""


def agent_memory_dir() -> Path:
    """
    Return Claude Code's on-disk agent memory directory inside the container.

    Defaults to ``~/.claude/projects/-workspace/memory`` (computed from ``HOME``
    so it follows a relocated home directory). Overridable via
    ``LMER_AGENT_MEMORY_DIR`` for tests and non-standard layouts.
    """
    override = os.environ.get("LMER_AGENT_MEMORY_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "projects" / "-workspace" / "memory"


def memory_scope_dirs() -> Dict[str, Path]:
    """The memory scopes this session reads and writes, project scope first.

    One entry in a legacy tree (the project's ``memory/``), two in a
    schema-form one (D17). A scope that does not resolve — no run context for
    the project scope, a legacy tree or an unusable manifest for the instance
    one — is simply absent, so ``{}`` means "no memory home at all".
    """
    scopes: Dict[str, Path] = {}
    project = project_memory_dir()
    if project is not None:
        scopes[PROJECT_SCOPE] = project
    instance = instance_memory_dir()
    if instance is not None:
        scopes[INSTANCE_SCOPE] = instance
    return scopes


def memory_write_rel_paths() -> List[str]:
    """The work-repo-relative paths ``work memory persist`` may write.

    The write-attribution journal's scope for the command (issue #233) and the
    paths staged for commit, from one owner so the journal can never name a
    path the writer does not use. Project scope first, then instance; ``[]``
    when no scope resolves.
    """
    root = work_repo_root()
    rels = []
    for scope in memory_scope_dirs().values():
        rel = _rel_to_root(scope, root)
        if rel is not None:
            rels.append(rel)
    return rels


def _rel_to_root(path: Path, root: Path) -> Optional[str]:
    """*path* relative to the work repo root, POSIX-spelled; None if outside."""
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return None


def _display_rel(path: Path, root: Path) -> str:
    """The path to NAME in persist's deletion report: root-relative when it
    lies under the work repo, absolute otherwise. Reporting never crashes on a
    path the root does not enclose."""
    return _rel_to_root(path, root) or str(path)


# ---------------------------------------------------------------------------
# Schema-2 retirement state
# ---------------------------------------------------------------------------


def retirement_state_path(root: Optional[Path] = None) -> Path:
    """Return schema-2's durable memory-retirement state path."""
    base = Path(root) if root is not None else work_repo_root()
    return base / RETIREMENT_STATE_NAME


def _archive_rel_for_key(key: str) -> str:
    """The archive-mirror path for one validated retirement *key*.

    Pure path arithmetic: the retirement key already carries the scope
    (``_agents/memory`` at instance scope, ``<project>/_agents/memory`` at
    project scope), so the mirror is the key with ``_archive`` inserted after
    its scope root. :func:`_retirement_archive_rel` is the historical
    manifest-taking spelling; the manifest is not read.
    """
    parts = PurePosixPath(key).parts
    if parts[:2] == ("_agents", "memory"):
        return PurePosixPath("_archive", *parts).as_posix()
    return PurePosixPath(parts[0], "_archive", *parts[1:]).as_posix()


def _retirement_archive_rel(key: str, manifest: Manifest) -> str:
    """The archive-mirror path for one validated retirement *key*."""
    return _archive_rel_for_key(key)


def _validate_retirement_key(key: object, manifest: Manifest) -> str:
    """Validate and return a root-relative scoped memory path."""
    if not isinstance(key, str) or not key:
        raise MemoryRetirementError("retirement keys must be non-empty strings")
    path = PurePosixPath(key)
    if (
        path.is_absolute()
        or path.as_posix() != key
        or ".." in path.parts
        or "\\" in key
    ):
        raise MemoryRetirementError(f"invalid retirement path {key!r}")
    parts = path.parts
    instance = len(parts) >= 3 and parts[:2] == ("_agents", "memory")
    project = (
        len(parts) >= 4
        and parts[0] in manifest.projects
        and parts[1:3] == ("_agents", "memory")
    )
    if not (instance or project):
        raise MemoryRetirementError(
            f"retirement path is not a schema-2 memory scope: {key!r}"
        )
    if path.suffix.lower() != ".md" or path.name == MEMORY_INDEX:
        raise MemoryRetirementError(
            f"retirement path must name a memory markdown file: {key!r}"
        )
    return key


def _schema2_manifest(root: Path) -> Optional[Manifest]:
    """The schema-2 manifest for *root*, or ``None`` for older/legacy trees."""
    manifest = load_work_manifest(root)
    return manifest if manifest is not None and manifest.schema == 2 else None


def read_memory_retirements(root: Optional[Path] = None, *, manifest=...) -> Dict[str, Any]:
    """Read and validate schema-2 retirement state without discarding extensions.

    The returned mapping is the full stored document. Unknown top-level and
    per-entry fields survive later forget/revive writes. Every field this
    module acts on is validated before callers mutate memory or archive files.
    """
    root = Path(root) if root is not None else work_repo_root()
    if manifest is ...:
        manifest = _schema2_manifest(root)
    if manifest is None or manifest.schema < 2:
        return {"version": RETIREMENT_STATE_VERSION, "retired": {}}
    path = retirement_state_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": RETIREMENT_STATE_VERSION, "retired": {}}
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise MemoryRetirementError(f"cannot read {path}: {e}") from e
    if not isinstance(data, dict):
        raise MemoryRetirementError(f"{path}: retirement state must be an object")
    version = data.get("version")
    if isinstance(version, bool) or version != RETIREMENT_STATE_VERSION:
        raise MemoryRetirementError(
            f"{path}: `version` must be {RETIREMENT_STATE_VERSION}"
        )
    retired = data.get("retired")
    if not isinstance(retired, dict):
        raise MemoryRetirementError(f"{path}: `retired` must be an object")
    for raw_key, entry in retired.items():
        key = _validate_retirement_key(raw_key, manifest)
        if not isinstance(entry, dict):
            raise MemoryRetirementError(f"{path}: retirement {key!r} must be an object")
        expected_archive = _retirement_archive_rel(key, manifest)
        if entry.get("archive") != expected_archive:
            raise MemoryRetirementError(
                f"{path}: retirement {key!r} must archive to {expected_archive!r}"
            )
        lines = entry.get("index_lines")
        if not isinstance(lines, list) or not all(
            isinstance(line, str) for line in lines
        ):
            raise MemoryRetirementError(
                f"{path}: retirement {key!r} `index_lines` must be a list of strings"
            )
        recorded_index = entry.get("index", MEMORY_INDEX)
        if not isinstance(recorded_index, str) or not _is_index_rel(recorded_index):
            raise MemoryRetirementError(
                f"{path}: retirement {key!r} `index` must be {MEMORY_INDEX} or "
                f"{AREAS_DIR}/<slug>.md"
            )
        metadata = entry.get("metadata", {})
        if not isinstance(metadata, dict):
            raise MemoryRetirementError(
                f"{path}: retirement {key!r} `metadata` must be an object"
            )
    return data


def _serialize_memory_retirements(data: Dict[str, Any], root: Path) -> str:
    """Serialize retirement state before any related filesystem mutation."""
    try:
        return json.dumps(data, indent=2, sort_keys=True) + "\n"
    except (TypeError, ValueError) as e:
        raise MemoryRetirementError(
            f"{retirement_state_path(root)}: retirement state is not "
            f"JSON-serializable: {e}"
        ) from e


def _write_memory_retirements(payload: str, root: Path) -> None:
    """Atomically replace the retirement document with serialized state."""
    path = retirement_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _retirement_lock_path(root: Path) -> Path:
    """Return the shared lock path for retirement and persist mutations."""
    git_path = root / ".git"
    if git_path.is_dir():
        return git_path / "lmer-memory-retirement.lock"
    if git_path.is_file():
        try:
            marker = git_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as e:
            raise MemoryRetirementError(f"cannot read {git_path}: {e}") from e
        prefix = "gitdir: "
        if marker.startswith(prefix):
            git_dir = Path(marker[len(prefix) :])
            if not git_dir.is_absolute():
                git_dir = (root / git_dir).resolve()
            return git_dir / "lmer-memory-retirement.lock"
    resolved = root.resolve()
    return resolved.parent / f".{resolved.name}.lmer-memory-retirement.lock"


@contextmanager
def _memory_retirement_lock(root: Path) -> Iterator[None]:
    """Serialize schema-2 retirement and conflicting persist mutations."""
    with checkout_lock(root):
        with _memory_retirement_lock_only(root):
            yield


@contextmanager
def _memory_retirement_lock_only(root: Path) -> Iterator[None]:
    """Second lock in the fixed checkout → memory order."""
    path = _retirement_lock_path(root)
    descriptor: Optional[int] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as e:
        if descriptor is not None:
            os.close(descriptor)
        raise MemoryRetirementError(
            f"cannot lock memory retirement at {path}: {e}"
        ) from e
    assert descriptor is not None
    try:
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _retirement_paths(key: str, manifest: Manifest):
    """Source, archive and index paths for a validated retirement key."""
    source_rel = PurePosixPath(key)
    archive_rel = PurePosixPath(_retirement_archive_rel(key, manifest))
    scope_parts = (
        source_rel.parts[:2]
        if source_rel.parts[0] == "_agents"
        else source_rel.parts[:3]
    )
    index_rel = PurePosixPath(*scope_parts, MEMORY_INDEX)
    return source_rel, archive_rel, index_rel


def _retirement_index_rel(
    root: Path,
    source_rel: PurePosixPath,
    index_rel: PurePosixPath,
    recorded: Optional[str] = None,
) -> PurePosixPath:
    """The index file that carries *source_rel*'s entry (#435).

    *recorded* is the scope-relative index a retirement entry names. Without
    one, the map is searched first and then each area index; an entry found
    nowhere keeps the map, so forget still fails on "no entry" as before.
    """
    scope = index_rel.parent
    if recorded is not None:
        return PurePosixPath(scope, recorded)
    scope_dir = root / Path(scope)
    memory_rel = posixpath.relpath(source_rel.as_posix(), scope.as_posix())
    for rel in [Path(MEMORY_INDEX)] + sorted(_area_indexes(scope_dir)):
        text = _read_text(scope_dir / rel)
        if memory_rel in _indexed_in(text, rel.as_posix(), split=_is_split(scope_dir)):
            return PurePosixPath(scope, rel.as_posix())
    return index_rel


def memory_retirement_paths(scoped_path: str, *, root: Optional[Path] = None) -> List[str]:
    """Validate and name the exact paths before a CLI journals a mutation."""
    root = Path(root) if root is not None else work_repo_root()
    manifest = _schema2_manifest(root)
    if manifest is None:
        raise MemoryRetirementError("explicit memory retirement requires schema 2")
    key = _validate_retirement_key(scoped_path, manifest)
    source_rel, archive_rel, index_rel = _retirement_paths(key, manifest)
    recorded = read_memory_retirements(root, manifest=manifest)["retired"].get(key, {})
    index_rel = _retirement_index_rel(root, source_rel, index_rel, recorded.get("index"))
    paths = (source_rel, archive_rel, index_rel)
    return [path.as_posix() for path in paths] + [RETIREMENT_STATE_NAME]


def _refuse_newer_store_format(scope_dir: Path) -> None:
    """Raise :class:`MemoryRetirementError` for a scope no writer may touch (#598)."""
    problem = check_store_format(scope_dir)
    if problem is not None:
        raise MemoryRetirementError(problem)


def _rewrite_index_target(line: str, old: str, new: str) -> str:
    """Rewrite relative markdown link targets equal to *old* on one line."""
    pieces = []
    position = 0
    for match in _LINK_RE.finditer(line):
        target = match.group(1)
        if posixpath.normpath(target) != posixpath.normpath(old):
            continue
        start, end = match.span(1)
        pieces.extend((line[position:start], new))
        position = end
    if not pieces:
        return line
    pieces.append(line[position:])
    return "".join(pieces)


def forget_memory(
    scoped_path: str,
    metadata: Optional[Dict[str, Any]] = None,
    *,
    root: Optional[Path] = None,
) -> List[str]:
    """Archive one schema-2 memory and record its scoped retirement key.

    Returns every root-relative path a CLI must include in the operation's
    single commit. This function performs no Git operation.
    """
    root = Path(root) if root is not None else work_repo_root()
    with _memory_retirement_lock(root):
        return _forget_memory_locked(scoped_path, metadata, root)


def _forget_memory_locked(
    scoped_path: str, metadata: Optional[Dict[str, Any]], root: Path
) -> List[str]:
    """Implement :func:`forget_memory` while holding the per-root lock."""
    manifest = _schema2_manifest(root)
    if manifest is None:
        raise MemoryRetirementError("explicit memory retirement requires schema 2")
    key = _validate_retirement_key(scoped_path, manifest)
    if metadata is not None and not isinstance(metadata, dict):
        raise MemoryRetirementError("retirement metadata must be an object")
    state = read_memory_retirements(root)
    if key in state["retired"]:
        raise MemoryRetirementError(f"memory is already retired: {key}")
    source_rel, archive_rel, index_rel = _retirement_paths(key, manifest)
    _refuse_newer_store_format(root / Path(index_rel.parent))
    index_rel = _retirement_index_rel(root, source_rel, index_rel)
    source = root / Path(source_rel)
    archive = root / Path(archive_rel)
    index = root / Path(index_rel)
    if not source.is_file():
        raise MemoryRetirementError(f"memory does not exist: {key}")
    if archive.exists():
        raise MemoryRetirementError(
            f"archive destination already exists: {archive_rel}"
        )
    try:
        index_text = index.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise MemoryRetirementError(
            f"cannot read memory index {index_rel}: {e}"
        ) from e
    memory_rel = posixpath.relpath(source_rel.as_posix(), index_rel.parent.as_posix())
    archive_target = posixpath.relpath(
        archive_rel.as_posix(), index_rel.parent.as_posix()
    )
    original_lines: List[str] = []
    rewritten_lines: List[str] = []
    for line in index_text.splitlines(keepends=True):
        rewritten = _rewrite_index_target(line, memory_rel, archive_target)
        if rewritten != line:
            original_lines.append(line.rstrip("\r\n"))
        rewritten_lines.append(rewritten)
    if not original_lines:
        raise MemoryRetirementError(f"memory index has no entry for {key}")
    entry: Dict[str, Any] = {
        "archive": archive_rel.as_posix(),
        "index_lines": original_lines,
    }
    scope_index = index_rel.relative_to(_retirement_paths(key, manifest)[2].parent)
    if scope_index.as_posix() != MEMORY_INDEX:
        # Revive must put the line back where it was: an area index (#435).
        entry["index"] = scope_index.as_posix()
    if metadata is not None:
        entry["metadata"] = metadata
    state["retired"][key] = entry
    payload = _serialize_memory_retirements(state, root)
    moved = False
    index_started = False
    try:
        archive.parent.mkdir(parents=True, exist_ok=True)
        source.replace(archive)
        moved = True
        index_started = True
        index.write_text("".join(rewritten_lines), encoding="utf-8")
        _write_memory_retirements(payload, root)
    except OSError as e:
        rollback_errors = []
        if index_started:
            try:
                index.write_text(index_text, encoding="utf-8")
            except OSError as rollback_error:
                rollback_errors.append(f"index: {rollback_error}")
        if moved:
            try:
                archive.replace(source)
            except OSError as rollback_error:
                rollback_errors.append(f"memory: {rollback_error}")
        detail = (
            f"; rollback failed ({', '.join(rollback_errors)})"
            if rollback_errors
            else ""
        )
        raise MemoryRetirementError(f"could not forget {key}: {e}{detail}") from e
    return [key, archive_rel.as_posix(), index_rel.as_posix(), RETIREMENT_STATE_NAME]


def revive_memory(scoped_path: str, *, root: Optional[Path] = None) -> List[str]:
    """Restore one schema-2 memory from its archive and clear retirement."""
    root = Path(root) if root is not None else work_repo_root()
    with _memory_retirement_lock(root):
        return _revive_memory_locked(scoped_path, root)


def _revive_memory_locked(scoped_path: str, root: Path) -> List[str]:
    """Implement :func:`revive_memory` while holding the per-root lock."""
    manifest = _schema2_manifest(root)
    if manifest is None:
        raise MemoryRetirementError("explicit memory revival requires schema 2")
    key = _validate_retirement_key(scoped_path, manifest)
    state = read_memory_retirements(root)
    entry = state["retired"].get(key)
    if entry is None:
        raise MemoryRetirementError(f"memory is not retired: {key}")
    source_rel, archive_rel, index_rel = _retirement_paths(key, manifest)
    _refuse_newer_store_format(root / Path(index_rel.parent))
    index_rel = _retirement_index_rel(root, source_rel, index_rel, entry.get("index"))
    source = root / Path(source_rel)
    archive = root / Path(archive_rel)
    index = root / Path(index_rel)
    if source.exists():
        raise MemoryRetirementError(f"live memory already exists: {key}")
    if not archive.is_file():
        raise MemoryRetirementError(f"archived memory is missing: {archive_rel}")
    index_existed = index.exists()
    try:
        current = index.read_text(encoding="utf-8") if index_existed else ""
    except (OSError, UnicodeDecodeError) as e:
        raise MemoryRetirementError(
            f"cannot read memory index {index_rel}: {e}"
        ) from e
    memory_rel = posixpath.relpath(source_rel.as_posix(), index_rel.parent.as_posix())
    archive_target = posixpath.relpath(
        archive_rel.as_posix(), index_rel.parent.as_posix()
    )
    tombstones = {
        _rewrite_index_target(line, memory_rel, archive_target).strip()
        for line in entry["index_lines"]
    }
    kept = [
        line
        for line in current.splitlines(keepends=True)
        if line.strip() not in tombstones
    ]
    present = {line.strip() for line in kept}
    for line in entry["index_lines"]:
        if line.strip() not in present:
            kept.append(line.rstrip("\r\n") + "\n")
            present.add(line.strip())
    del state["retired"][key]
    payload = _serialize_memory_retirements(state, root)
    moved = False
    index_started = False
    try:
        source.parent.mkdir(parents=True, exist_ok=True)
        archive.replace(source)
        moved = True
        index.parent.mkdir(parents=True, exist_ok=True)
        index_started = True
        index.write_text("".join(kept), encoding="utf-8")
        _write_memory_retirements(payload, root)
    except OSError as e:
        rollback_errors = []
        if index_started:
            try:
                if index_existed:
                    index.write_text(current, encoding="utf-8")
                else:
                    index.unlink(missing_ok=True)
            except OSError as rollback_error:
                rollback_errors.append(f"index: {rollback_error}")
        if moved:
            try:
                source.replace(archive)
            except OSError as rollback_error:
                rollback_errors.append(f"memory: {rollback_error}")
        detail = (
            f"; rollback failed ({', '.join(rollback_errors)})"
            if rollback_errors
            else ""
        )
        raise MemoryRetirementError(f"could not revive {key}: {e}{detail}") from e
    return [key, archive_rel.as_posix(), index_rel.as_posix(), RETIREMENT_STATE_NAME]


def _schema2_retired_keys(root: Path) -> Set[str]:
    """Validated retirement keys enforced by runtime memory operations."""
    if _schema2_manifest(root) is None:
        return set()
    return set(read_memory_retirements(root)["retired"])


def _retired_by_scope(
    scopes: Dict[str, Path], keys: Set[str], root: Path
) -> Dict[str, Set[Path]]:
    """Map root-relative retirement keys to paths relative to each scope."""
    retired: Dict[str, Set[Path]] = {scope: set() for scope in scopes}
    for key in keys:
        path = root / Path(PurePosixPath(key))
        for scope, directory in scopes.items():
            try:
                retired[scope].add(path.relative_to(directory))
            except ValueError:
                continue
    return retired


def _filter_retired_index(
    text: str, retired: Set[Path], index_rel: Optional[str] = None, *, split: bool
) -> str:
    """Drop active index entries for retired paths; keep archive tombstones.

    *split* is off for a flat scope's index (:func:`_entry_targets`).
    """
    names = {rel.as_posix() for rel in retired}
    return "".join(
        line
        for line in text.splitlines(keepends=True)
        if not names.intersection(_entry_targets(line, index_rel, split=split))
    )


# ---------------------------------------------------------------------------
# The restore manifest: what THIS session restored (issue #369)
# ---------------------------------------------------------------------------


def restore_manifest_path() -> Path:
    """Where this session records what it restored.

    A SIBLING of the agent's memory dir, never a file inside it: inside, it
    would be routed and persisted into the work repo as schema-visible state
    (and would make an empty memory dir look non-empty). Beside it, it has the
    memory dir's exact lifetime — whatever mount or container the memory dir
    lives in, the manifest lives in the same one, so it can never describe a
    directory it is not next to. It follows ``LMER_AGENT_MEMORY_DIR`` for the
    same reason, which is also how tests give two "sessions" two manifests.
    """
    return agent_memory_dir().parent / RESTORE_MANIFEST_NAME


def read_restore_baseline() -> Optional[Dict[str, dict]]:
    """This session's restore manifest, or ``None`` when it has none.

    ``None`` for every shape that is not a manifest this code wrote for THIS
    memory dir and launch — absent, unreadable, not JSON, a version, memory
    dir or session that does not match. A valid manifest naming no scopes is
    ``{}``: a baseline of nothing (a fresh start), which is not the same as
    no baseline at all (#369 gap (c)).
    """
    path = restore_manifest_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, ValueError) as e:
        print(
            f"⚠️  Ignoring unreadable memory restore manifest {path}: {e} "
            f"(this persist will not delete anything)",
            file=sys.stderr,
        )
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != RESTORE_MANIFEST_VERSION:
        return None
    if data.get("memory_dir") != str(agent_memory_dir()):
        return None
    if data.get("session") != launch_session_id():
        # A baseline recorded under another launch — or an older container's
        # record with no session at all against a set one — is not this
        # session's. Treating it as one would let a memory dir that outlives
        # its session delete on a foreign record. Both ids absent counts as
        # equal: the regression pins run with LMER_SESSION_ID stripped.
        print(
            f"⚠️  Memory restore manifest {path} was recorded under session "
            f"{data.get('session')!r}, not this launch "
            f"({launch_session_id()!r}) — no baseline, this persist deletes "
            f"nothing",
            file=sys.stderr,
        )
        return None
    scopes = data.get("scopes")
    if not isinstance(scopes, dict):
        return None
    return {
        scope: entry
        for scope, entry in scopes.items()
        if isinstance(entry, dict)
    }


def read_restore_manifest() -> Dict[str, dict]:
    """This session's restore manifest, as ``{scope: {files, index}}``.

    ``{}`` when there is no usable baseline (:func:`read_restore_baseline`).
    That is the safe direction: an empty manifest says this session restored
    nothing, so persist deletes nothing.
    """
    return read_restore_baseline() or {}


@mutation_transaction
def write_restore_manifest(scopes: Dict[str, dict]) -> None:
    """Record *scopes* as what this session now holds. Never raises.

    A manifest that cannot be written leaves the session with no record, and
    a session with no record deletes nothing — a warning, not a failure.
    """
    path = restore_manifest_path()
    payload = {
        "version": RESTORE_MANIFEST_VERSION,
        "memory_dir": str(agent_memory_dir()),
        "session": launch_session_id(),
        "scopes": scopes,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as e:
        print(
            f"⚠️  Could not record what this session restored ({path}: {e}) — "
            f"a later persist will add memories but delete none",
            file=sys.stderr,
        )


def _manifest_files(manifest: Dict[str, dict], scope: str) -> Dict[str, str]:
    """The ``{relative path: digest}`` this session restored from *scope*.

    Keys are validated here because this is the one manifest field that drives
    an ``unlink``: a key that is absolute or escapes with ``..`` resolves
    OUTSIDE the scope (``dest / Path("/abs")`` is ``/abs``), so a hand-written
    or corrupted manifest could delete a file the scope does not own. Nothing
    this module writes can produce such a key — the writer stores
    ``relative_to(directory)`` — but "a manifest I cannot read deletes
    nothing" is the invariant the whole design rests on, and an unusable key
    is exactly as unreadable as a bad version. Such a key is dropped, not
    trusted: dropping it can only under-delete.
    """
    entry = manifest.get(scope) or {}
    files = entry.get("files")
    if not isinstance(files, dict):
        return {}
    safe: Dict[str, str] = {}
    for rel, digest in files.items():
        if not isinstance(rel, str) or not isinstance(digest, str) or not rel:
            continue
        candidate = PurePosixPath(rel)
        if candidate.is_absolute() or ".." in candidate.parts or rel.startswith("/"):
            continue
        safe[rel] = digest
    return safe


def _manifest_index(manifest: Dict[str, dict], scope: str) -> Set[str]:
    """The index lines this session restored from *scope*, stripped."""
    entry = manifest.get(scope) or {}
    lines = entry.get("index")
    if not isinstance(lines, list):
        return set()
    return {line for line in lines if isinstance(line, str)}


def _digest(path: Path) -> str:
    """A content digest for *path*; ``""`` when it cannot be read.

    An unreadable file never compares equal to a recorded digest, so it is
    never deleted on the strength of one.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _index_lines(text: str) -> List[str]:
    """The non-blank lines of an index, stripped — its content, not its shape.

    Blank lines are spacing rather than statements about a memory, and the
    merge keeps the destination's spacing, so they are not part of what a
    session is recorded as having restored.
    """
    return [line.strip() for line in text.splitlines() if line.strip()]


def _manifest_area_index(manifest: Dict[str, dict], scope: str, rel: Path) -> Set[str]:
    """The lines this session restored from *scope*'s area index *rel*."""
    areas = (manifest.get(scope) or {}).get("areas")
    lines = areas.get(rel.as_posix()) if isinstance(areas, dict) else None
    if not isinstance(lines, list):
        return set()
    return {line for line in lines if isinstance(line, str)}


def _restored_index_lines(manifest: Dict[str, dict], scope: str, rel: Path) -> Set[str]:
    """The lines this session restored from one of *scope*'s index files."""
    if rel == Path(MEMORY_INDEX):
        return _manifest_index(manifest, scope)
    return _manifest_area_index(manifest, scope, rel)


def _scope_snapshot(
    files: Dict[Path, Path], index_text: str, area_texts: Optional[Dict[Path, str]] = None
) -> dict:
    """One scope's manifest entry: the files restored from it, plus its indexes.

    ``areas`` is absent from manifests written before #435. Such a manifest
    lists the area files under ``files`` instead, as if they were memories:
    :func:`_restored_then_removed` never deletes an index file on the strength
    of that record, and :func:`_adopt_pre_435_area_bases` turns it into a
    merge base wherever the session's copy is unchanged. An area it cannot
    vouch for has an empty base, so its merge is additive: it never deletes,
    but it can re-add a line another session moved.
    """
    snapshot = {
        "files": {rel.as_posix(): _digest(path) for rel, path in files.items()},
        "index": _index_lines(index_text),
    }
    if area_texts:
        snapshot["areas"] = {
            rel.as_posix(): _index_lines(text) for rel, text in area_texts.items()
        }
    return snapshot


# ---------------------------------------------------------------------------
# Memory files: frontmatter, routing, index lines
# ---------------------------------------------------------------------------


def _frontmatter(text: str) -> Optional[dict]:
    """The YAML frontmatter of *text* as a mapping, or ``None``.

    ``None`` for every shape a writer cannot route on — no fence, an
    unterminated fence, YAML that does not parse, a document that is not a
    mapping. The reader is deliberately tolerant: this is a writer, and a file
    it cannot read is still a file it must not lose.
    """
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    try:
        data = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def scope_for_memory(text: str) -> Optional[str]:
    """The scope *text*'s ``metadata.type`` selects (D17), or ``None``.

    ``None`` says the file does not route itself — an absent, non-scalar,
    malformed or unknown type — and the caller applies :data:`DEFAULT_SCOPE`.
    """
    data = _frontmatter(text)
    if data is None:
        return None
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return None
    kind = metadata.get("type")
    if not isinstance(kind, str):
        # A non-scalar type is not a lookup key at all — ``dict.get`` raises
        # on an unhashable one — and ``type: [user, feedback]`` or a ``type:``
        # whose value got indented into a mapping is an ordinary hand-edit.
        # Same rule as every other shape this reader cannot use: route it,
        # never raise. One malformed file must not cost the session its
        # entire memory.
        return None
    return TYPE_SCOPES.get(kind)


def _memory_name(path: Path) -> Optional[str]:
    """The declared ``name`` of the memory at *path*, or ``None``.

    Only an explicit frontmatter name counts: the name is what makes two files
    in different scopes the same memory, and guessing one from a filename
    would let unrelated files shadow each other.
    """
    data = _frontmatter(_read_text(path))
    if data is None:
        return None
    name = data.get("name")
    return name if isinstance(name, str) and name else None


def _read_text(path: Path) -> str:
    """*path* as text; unreadable or non-UTF-8 content reads as empty."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _index_statement(path: Optional[Path]) -> Optional[str]:
    """What the agent's ``MEMORY.md`` SAYS about its index lines, or ``None``
    for "this session has nothing to say about indexes".

    The distinction the whole tombstone rule rests on. An index this session
    holds is a STATEMENT — these are the lines I have, so a line I no longer
    carry is one I retired — and an empty statement is a real one: "I retired
    everything". An ABSENT index is not a statement at all, and a persist that
    reads it as one deletes every line the session restored while the memory
    files stay on disk. Nothing looks broken; the next session simply cannot
    find them.

    Two shapes are absence, and only one of them is obvious. No file at all is
    the easy half. The other is a file that EXISTS and says nothing: a
    zero-byte or whitespace-only ``MEMORY.md`` is what a crash, a killed
    container or a half-flushed write leaves behind, and ``is_file()`` is true
    for every one of them. A session that genuinely has no lines for one scope
    still has an index carrying the OTHER scope's lines, so the statement this
    rule exists to honour is never blank — which is what makes reading blank as
    absence safe. Where the two readings differ at all, under-deleting is the
    direction that loses nothing.

    Both persist paths ask here — the one-scope branch and the routed one —
    because this is one rule about one file, and it has already been fixed
    once in each place separately.
    """
    if path is None or not path.is_file():
        return None
    text = _read_text(path)
    return None if not text.strip() else text


def _entry_targets(line: str, index_rel: Optional[str] = None, *, split: bool) -> List[str]:
    """The entry link targets on one index line (``docs/SCHEMA.md``).

    An entry is a line carrying a RELATIVE markdown link to a ``.md`` file. A
    heading, prose, a tombstoned line (the link replaced by
    ``archived``/``forgotten`` and a date) and a link re-pointed at the
    ``_archive/`` mirror carry no entry: they belong to no single scope and
    are kept in both indexes. Neither does a MAP line (#435): any line of
    ``MEMORY.md`` that links an area index is one, whatever else its
    read-when hook links — a hook that says "start with [Alpha](alpha.md)" is
    still part of the map line, and must not be filed or dropped as an entry
    for ``alpha.md``. Inside an area index a cross-reference to another area
    is skipped, and the line's memory links still count (a dead one is
    tombstoned by :func:`_tombstone_dead_area_links`). A ``forget`` tombstone
    (:func:`_is_forget_tombstone`) carries none at all, whatever it links
    later: the validator reads it the same way (#601).

    Links resolve against *index_rel* — the index file's path inside its
    scope, ``MEMORY.md`` when omitted — so an area index's ``../fact.md`` and
    the map's ``fact.md`` both come back as ``fact.md``.

    Without *split* (a flat scope, which ignores ``areas/``) there are no map
    lines and no area cross-references: a link to ``areas/<slug>.md`` is an
    ordinary entry link, as it was before #435, and the reconcile tombstones
    it when the file is not there. *split* has no default: it is a fact about
    the scope the line is read in (:func:`_is_split`), and every caller —
    housekeep and migrate included — must say which shape it is reading.
    """
    if _is_forget_tombstone(line):
        return []
    base = posixpath.dirname(index_rel) if index_rel else ""
    targets = []
    for target in _LINK_RE.findall(line):
        if not target.lower().endswith(".md"):
            continue
        if _NOT_RELATIVE_RE.match(target):
            continue
        resolved = posixpath.normpath(posixpath.join(base, target))
        if split and _is_area_index(resolved):
            if not base:
                return []
            continue
        if any(part in _ARCHIVE_MARKERS for part in resolved.split("/")):
            continue
        if resolved == MEMORY_INDEX:
            continue
        targets.append(resolved)
    return targets


def _is_area_index(rel) -> bool:
    """Whether scope-relative *rel* is an area index, ``areas/<slug>.md``."""
    parts = PurePosixPath(str(rel)).parts
    return (
        len(parts) == 2
        and parts[0] == AREAS_DIR
        and parts[1].endswith(".md")
        and is_area_slug(parts[1][: -len(".md")])
    )


def _is_index_rel(rel) -> bool:
    """Whether scope-relative *rel* is one of the scope's index files."""
    return str(PurePosixPath(str(rel))) == MEMORY_INDEX or _is_area_index(rel)


def _area_rel(slug: str) -> Path:
    return Path(AREAS_DIR) / f"{slug}.md"


def _area_indexes(directory: Path) -> Dict[Path, Path]:
    """The area index files under *directory*, keyed by relative path."""
    return {
        rel: path for rel, path in _files_under(directory).items() if _is_area_index(rel)
    }


def _pop_area_indexes(files: Dict[Path, Path]) -> Dict[Path, Path]:
    """Remove and return the area index files from a ``_files_under`` map."""
    areas = {rel: path for rel, path in files.items() if _is_area_index(rel)}
    for rel in areas:
        del files[rel]
    return areas


def _is_split(directory: Path) -> bool:
    """A scope is split once it holds an area index: the shape is the switch."""
    return bool(_area_indexes(directory))


def _memory_view(dest: Path, peers=()) -> tuple:
    """``(memories, areas)`` the agent's merged view holds, across scopes.

    Restore merges every scope into one memory dir, so a link in one scope's
    index may name a memory or an area that lives in the OTHER scope, and is
    valid there. This is the union of *dest* and its *peers*
    (:func:`_memory_peers`), read once every scope this persist writes HAS
    been written (:func:`_settle_scope`): what the persist leaves behind. The
    agent's own dir is not part of it — it can still hold an area another
    session deleted, which the merge declines to recreate — and neither is a
    scope's state before this persist deletes from it. Memories are
    scope-relative POSIX paths (``fact.md``), areas are area index paths
    (``areas/dev.md``).
    """
    memories: Set[str] = set()
    areas: Set[str] = set()
    for directory in (dest, *peers):
        if directory.is_dir():
            memories |= _live_memory_rels(directory)
            areas |= {rel.as_posix() for rel in _area_indexes(directory)}
    return memories, areas


def _memory_peers(scope: str, scopes: Dict[str, Path], root: Path) -> tuple:
    """The scope dirs a restore merges with *scope*, as the validator names them.

    A project scope merges with the instance scope. The instance scope is
    shared by every project, and restore merges it with whichever project is
    restored, so its peers are every project's scope — ``lmer-schema``'s
    ``_peer_memory_dirs`` reading. Judged against the persisting project
    alone, a no-change persist from another project tombstones a link that is
    live for the project it names.
    """
    peers = [path for name, path in scopes.items() if name != scope]
    if scope != INSTANCE_SCOPE:
        return tuple(peers)
    try:
        projects = work_repo_project_rels(root)
    except Exception as exc:  # the core's own refusal; persist must not die on it
        print(
            f"⚠️  Could not list the work repo's projects ({exc}) — judging the "
            f"instance memory scope's links against this project only",
            file=sys.stderr,
        )
        projects = []
    for rel in projects:
        path = root / rel / "_agents" / "memory"
        if path not in peers and path != scopes.get(scope):
            peers.append(path)
    return tuple(peers)


def _area_problems(directory: Path, memories: Set[str], areas: Set[str]) -> List[tuple]:
    """How a split scope's ``areas/`` breaks the migration contract.

    Only the two signatures of a bad hand migration, both of which the
    reconcile would otherwise "repair" quietly:

    * a file in ``areas/`` that is not ``<slug>.md`` (it would be filed as a
      memory);
    * an area-index link that stays inside ``areas/`` where the memory it
      names lives one level up: ``x.md`` where ``../x.md`` was meant (the
      link would be tombstoned and the memory re-filed to ``unsorted``).

    *memories* and *areas* are the agent's whole view (:func:`_memory_view`),
    so a link to an area that lives in the other scope is a valid
    cross-reference even where a memory has the same name, and a memory in
    the other scope still makes ``x.md`` a mis-migration. A link to an area
    that exists nowhere and names no memory is not a problem either: it is a
    dead link, and the reconcile tombstones it like any other. Returns
    ``(rel, detail, line)`` triples, scope-relative; *line* is ``None`` for a
    file problem.
    """
    areas_dir = directory / AREAS_DIR
    if not areas_dir.is_dir():
        return []
    problems: List[tuple] = []
    for entry in sorted(areas_dir.iterdir()):
        if entry.name.startswith("."):
            continue
        rel = f"{AREAS_DIR}/{entry.name}"
        if not (entry.is_file() and _is_area_index(rel)):
            problems.append(
                (rel, "not an area index: an area is a file named <slug>.md, "
                      "slug [a-z0-9][a-z0-9-]*", None)
            )
            continue
        for line in _read_text(entry).splitlines():
            for target in _LINK_RE.findall(line):
                if not target.lower().endswith(".md") or _NOT_RELATIVE_RE.match(target):
                    continue
                resolved = posixpath.normpath(posixpath.join(AREAS_DIR, target))
                if posixpath.dirname(resolved) != AREAS_DIR or resolved in areas:
                    continue
                name = posixpath.basename(resolved)
                if name in memories:
                    problems.append(
                        (rel, f"`{target}` resolves to {resolved}: an area index "
                              f"links a memory as `../{name}`", line)
                    )
    return problems


def _holds_problem(source: Path, problem: tuple) -> bool:
    """Whether the agent's own dir holds the line (or file) *problem* names.

    Only such a problem is this session's to fix — it restored or wrote that
    line, and removing it locally drops it in the three-way merge. A line
    another writer added after this session restored is not in its copy, and
    no local edit can reach it.
    """
    rel, _detail, line = problem
    path = source / rel
    if line is None:
        return path.exists()
    if not path.is_file():
        return False
    return line.strip() in {held.strip() for held in _read_text(path).splitlines()}


class MemoryAreaMigrationError(Exception):
    """A split scope's area indexes break the migration contract.

    Raised by :func:`_settle_scope` once every scope is written and before the
    reconcile, so it judges what this persist would leave behind — and the
    persist is rolled back, never left half written. It carries only problems
    in lines the session holds (:func:`_holds_problem`), which the session can
    fix.
    """

    def __init__(self, dest: Path, problems: List[tuple]):
        super().__init__(f"{dest}: {len(problems)} area index problem(s)")
        self.dest = dest
        self.problems = problems


def _report_bad_areas(error: MemoryAreaMigrationError, source: Path) -> None:
    """Print the refusal for a badly migrated split index, and how to fix it.

    Every problem it carries is in a line or file the agent's own memory dir
    holds, so that is where it is fixed.
    """
    print(
        "❌ Refusing to persist agent memory: an area index is not migrated "
        "cleanly, and persisting would tombstone or misfile its entries:",
        file=sys.stderr,
    )
    for rel, detail, _line in error.problems:
        print(f"     {source / rel} (your memory dir): {detail}", file=sys.stderr)
    print(
        "   Fix it in your memory dir and persist again. Nothing was written "
        "to the work repo.",
        file=sys.stderr,
    )


def _warn_unheld_area_problems(dest: Path, problems: List[tuple], root: Path) -> None:
    """Report area problems in lines this session never held, and go on.

    Another writer put them there after this session restored, so refusing
    would block this session with nothing it can fix (#435). The persist
    proceeds; the reconcile tombstones a dead link as it does any other.
    """
    print(
        f"⚠️  The split memory index in {_display_rel(dest, root)} has a line "
        f"this session never held that is not migrated cleanly (another "
        f"writer's) — persisting anyway; a dead link in it is tombstoned:",
        file=sys.stderr,
    )
    for rel, detail, _line in problems:
        print(f"     {_display_rel(dest / rel, root)}: {detail}", file=sys.stderr)


def _memory_area(path: Path) -> str:
    """The area a memory names in ``metadata.area``, else :data:`UNSORTED_AREA`."""
    data = _frontmatter(_read_text(path))
    metadata = data.get("metadata") if data is not None else None
    area = metadata.get("area") if isinstance(metadata, dict) else None
    if is_area_slug(area):
        return area
    return UNSORTED_AREA


def _rebase_links(line: str, src_index: str, dst_index: str) -> str:
    """Rewrite *line*'s relative links from *src_index*'s dir to *dst_index*'s."""
    src_dir = posixpath.dirname(src_index)
    dst_dir = posixpath.dirname(dst_index) or "."

    def replace(match: "re.Match") -> str:
        target = match.group(1)
        if _NOT_RELATIVE_RE.match(target):
            return match.group(0)
        resolved = posixpath.normpath(posixpath.join(src_dir, target))
        whole, offset = match.group(0), match.start(0)
        return (
            whole[: match.start(1) - offset]
            + posixpath.relpath(resolved, dst_dir)
            + whole[match.end(1) - offset :]
        )

    return _LINK_RE.sub(replace, line)


def _map_line(slug: str) -> str:
    """The generated map line for an area that has none yet.

    It carries a default read-when hook, because the map is the only index
    loaded at start and agents choose an area by its hook: a bare link would
    never be chosen. ``unsorted`` gets a fixed hook; any other area's is
    derived from its slug and marked as generated so a person rewords it.
    """
    if slug == UNSORTED_AREA:
        hook = "read when no other area fits: memories not yet filed into one"
    else:
        hook = f"read when your task concerns {slug.replace('-', ' ')} (generated hook)"
    return f"- [{slug}]({_area_rel(slug).as_posix()}) — {hook}\n"


def _mapped_areas(map_text: str) -> Set[str]:
    """The area index paths the map already links to."""
    return {
        posixpath.normpath(target)
        for target in _LINK_RE.findall(map_text)
        if not _NOT_RELATIVE_RE.match(target) and _is_area_index(posixpath.normpath(target))
    }


def _index_statements(
    index: Optional[Path], areas: Dict[Path, Path], removed: Set[Path] = frozenset()
) -> Optional[Dict[Path, str]]:
    """What the agent's index files SAY, per file, or ``None`` for nothing.

    ``MEMORY.md`` decides whether the agent makes a statement at all
    (:func:`_index_statement`); an area index the agent holds blank says
    nothing about that area, for the same crash-safety reason. An area in
    *removed* — restored to this session and no longer in its dir — is an
    empty statement: the three-way merge then drops exactly the lines this
    session was handed, and keeps any another session added since.
    """
    top = _index_statement(index)
    if top is None:
        return None
    statements = {Path(MEMORY_INDEX): top}
    for rel in removed:
        statements[rel] = ""
    for rel, path in areas.items():
        text = _index_statement(path)
        if text is not None:
            statements[rel] = text
    return statements


def _adopt_pre_435_area_bases(source: Path, manifest: Dict[str, dict]) -> None:
    """Give a pre-#435 manifest the area-index base it never recorded.

    Such a manifest has no ``areas`` record, which makes the three-way base
    of every area index empty: every line the session holds then counts as
    its own addition, and a no-change persist puts back an entry another
    session moved to a different area. It does list each area index under
    ``files``, with the digest it was restored with. Where the session's copy
    still carries those bytes, the session changed nothing there, so the copy
    IS what it restored: record its lines as the base, in place. Where it
    does not, nothing is recorded and the merge stays additive, which can
    over-add but never deletes.
    """
    for scope, entry in manifest.items():
        if not isinstance(entry, dict) or "areas" in entry:
            continue
        for rel, digest in _manifest_files(manifest, scope).items():
            local = source / rel
            if _is_area_index(rel) and local.is_file() and _digest(local) == digest:
                entry.setdefault("areas", {})[rel] = _index_lines(_read_text(local))


def _removed_areas(source: Path, manifest: Dict[str, dict], scopes) -> Set[Path]:
    """Area indexes this session restored and has since deleted."""
    restored = {
        Path(rel)
        for scope in scopes
        for rel in ((manifest.get(scope) or {}).get("areas") or {})
        if isinstance(rel, str) and _is_area_index(rel)
    }
    return {rel for rel in restored if not (source / rel).exists()}


def _split_index(
    text: str,
    scope_of: Dict[str, str],
    restored: Optional[Dict[str, Set[str]]] = None,
    index_rel: Optional[str] = None,
    area_scopes: Optional[Dict[str, Set[str]]] = None,
    *,
    split: bool,
    flat_scopes: Set[str] = frozenset(),
) -> Dict[str, List[str]]:
    """Split one index into a per-scope index, by where each entry's file went.

    An entry line follows its file; every other line is commentary about the
    index rather than about one memory, so it is kept in both scopes — which
    is what lets :func:`_merge_index` drop the duplicate on the way back.

    A tombstone is the exception: it is about one memory, but its file is gone,
    so nothing routes it, and its archive link is relative to the index it was
    written in (both scopes spell it identically, so the link cannot say which).
    *restored* — the index lines this session restored from each scope — can:
    a tombstone restored from exactly one scope goes back to that scope only.
    One this session did not restore, or restored from both, is kept in both
    as before.

    A map line (#435) is the other one: restored from one scope it goes back
    there; otherwise it follows its own area (:func:`_own_area`), to the
    scopes *area_scopes* names for that area index. Copied into a scope with no
    such area it would dangle, and two scopes' differently-worded hooks would
    cross into each other.

    The agent's dir is split when EITHER scope is, so a line read as a map
    line here may be a flat scope's entry that also links ``areas/``. Such a
    line — not restored from one scope, and leading with a memory link where
    a map line leads with its area — follows its memory, as any entry does,
    when no scope holds its area or when that memory's scope is in
    *flat_scopes*: a flat scope has no map, and reads the line as an ordinary
    entry. Otherwise it is a map line.

    Without *split* (an agent dir with no area index) there are no map lines:
    a line linking ``areas/`` follows its memory like any entry.
    """
    per_scope: Dict[str, List[str]] = {PROJECT_SCOPE: [], INSTANCE_SCOPE: []}
    for line in text.splitlines(keepends=True):
        targets = _entry_targets(line, index_rel, split=split)
        if not targets:
            mapped = (
                _mapped_areas(line) if split and index_rel in (None, MEMORY_INDEX) else set()
            )
            owners = [
                scope for scope in per_scope
                if line.strip() in (restored or {}).get(scope, ())
            ] if (mapped or _is_forget_tombstone(line)) else []
            if len(owners) != 1 and mapped:
                owners = _map_line_owners(
                    line, index_rel, scope_of, area_scopes or {}, flat_scopes
                )
            if mapped and not owners:
                owners = [DEFAULT_SCOPE]
            for scope in (owners if owners and (len(owners) == 1 or mapped) else per_scope):
                per_scope[scope].append(line)
            continue
        per_scope[scope_of.get(targets[0], DEFAULT_SCOPE)].append(line)
    return per_scope


def _map_line_owners(
    line: str,
    index_rel: Optional[str],
    scope_of: Dict[str, str],
    area_scopes: Dict[str, Set[str]],
    flat_scopes: Set[str],
) -> List[str]:
    """The scopes an unowned ``MEMORY.md`` line linking an area goes to.

    See :func:`_split_index`: a flat scope's entry follows its memory, and a
    map line follows its own area. ``[]`` leaves it to the caller's default.

    Only a memory this session holds (one in *scope_of*) is a memory link
    here. A link to any other document — ``../notes.md`` — routes nothing:
    read as a memory, it would file an edited map line under the default
    scope, where a flat index tombstones its area link (#601).
    """
    own = _own_area(line)
    held = sorted(area_scopes.get(own[1], ())) if own is not None else []
    targets = [
        target
        for target in _entry_targets(line, index_rel, split=False)
        if _is_area_index(target) or target in scope_of
    ]
    if not targets or _is_area_index(targets[0]):
        return held
    home = scope_of[targets[0]]
    if not held or home in flat_scopes:
        return [home]
    return held


def _merge_index(texts: List[str]) -> str:
    """Merge per-scope indexes into one, instance first, without duplicates.

    Exact duplicate lines (the commentary :func:`_split_index` copied into
    both scopes) appear once; blank lines from the later index are dropped.

    What this restores is the index's CONTENT, not its line order. The merge
    appends, so entry lines come back grouped by scope — every instance entry,
    then every project one — and commentary keeps the place the FIRST index
    gives it, which for a note written after the entries means the project
    entries come back below it. Dedupe is by stripped text over the whole
    document, so a legitimately repeated line (a second ``---``, a repeated
    sub-heading) survives once. The reorder is stable, not drifting: merging
    an already-merged index moves nothing further. Preserving position would
    mean recording where each entry sat, which is not what this does — an
    index groups one line per memory and the agent reads it whole.
    """
    merged: List[str] = []
    seen: Set[str] = set()
    for position, text in enumerate(texts):
        for line in text.splitlines(keepends=True):
            stripped = line.strip()
            if not stripped:
                if position == 0:
                    merged.append(line)
                continue
            if stripped in seen:
                continue
            seen.add(stripped)
            merged.append(line)
    if merged and not merged[-1].endswith("\n"):
        merged[-1] += "\n"
    return "".join(merged)


def _merged_index_text(
    current: str, local_text: str, restored: Set[str], *, area: bool = False
) -> str:
    """Merge this session's index for one scope into the scope's own index.

    Three-way, with *restored* (the lines this session restored from this
    scope, :func:`_manifest_index`) as the base:

    * a line in the scope's *current* index is kept, UNLESS this session
      restored it and no longer has it — that, and only that, is a deletion;
    * a line this session has and did not restore is an addition, appended;
    * a line this session restored, still has, and the scope no longer has
      was deleted by somebody else after this session read it: it is NOT
      re-added. That is the tombstone case — a session must not resurrect an
      entry another session deliberately retired.

    With an empty base nothing is dropped, so a session with no manifest
    persists additively. With no current index there is nothing to merge and
    the session's own bytes go through verbatim: that is what a legacy tree's
    first persist has always written, and it heals a ``MEMORY.md`` something
    outside persist blanked or deleted (persist never blanks one itself).

    An *area* index is different (#435): sessions delete and empty areas as
    part of re-filing, so one that is gone or blank although this session
    restored lines from it was retired by another session. Only this
    session's genuine additions are written, and a stale session cannot
    recreate an area another one deleted.
    """
    if not current.strip() and not (area and restored & set(_index_lines(local_text))):
        return local_text

    local_lines = local_text.splitlines(keepends=True)
    dropped = restored - set(_index_lines(local_text))

    merged: List[str] = []
    seen: Set[str] = set()
    for line in current.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and stripped in dropped:
            continue
        if stripped:
            seen.add(stripped)
        merged.append(line)

    # Additions keep the spacing they had locally, but only around a line
    # that is actually added — trailing blanks must not accumulate on a
    # scope whose index this session changes nothing in.
    pending: List[str] = []
    for line in local_lines:
        stripped = line.strip()
        if not stripped:
            pending.append(line)
            continue
        if stripped in seen or stripped in restored:
            pending.clear()
            continue
        if pending and merged and merged[-1].strip():
            merged.extend(pending)
        pending.clear()
        seen.add(stripped)
        merged.append(line)

    if merged and not merged[-1].endswith("\n"):
        merged[-1] += "\n"
    return "".join(merged)


# ---------------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------------


def _has_files(directory: Path) -> bool:
    """Return True if ``directory`` exists and contains at least one file."""
    return bool(_files_under(directory))


def _files_under(directory: Path) -> Dict[Path, Path]:
    """Every file under *directory*, keyed by its relative path.

    The scope's store-format marker (:data:`MEMORY_FORMAT_FILE`) is no file
    of the memory set: it is never restored, routed, persisted or deleted.
    """
    if not directory.is_dir():
        return {}
    return {
        p.relative_to(directory): p
        for p in directory.rglob("*")
        if p.is_file() and p.relative_to(directory) != Path(MEMORY_FORMAT_FILE)
    }


def check_store_format(scope_dir: Path) -> Optional[str]:
    """Why this lmer must not write *scope_dir*, or ``None`` when it may.

    Reads the scope's store-format marker (:data:`MEMORY_FORMAT_FILE`, #598).
    No marker is the flat format. A marker naming a format newer than
    :data:`MEMORY_FORMAT_MAX` — written for a layout this lmer does not know —
    is a refusal, and so is one that is not a positive integer: neither says
    what the scope holds.
    """
    path = scope_dir / MEMORY_FORMAT_FILE
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        return f"{path}: store format marker could not be read ({e}); fix or remove it"
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < MEMORY_FORMAT_FLAT:
        return (
            f"{path}: store format marker {text[:40]!r} is not a positive integer; "
            f"fix or remove it"
        )
    if value > MEMORY_FORMAT_MAX:
        return (
            f"{path}: store format {value} is newer than this lmer understands "
            f"(max {MEMORY_FORMAT_MAX}); update the image"
        )
    return None


def _store_format_problems(directories) -> List[str]:
    """:func:`check_store_format` for each of *directories*, problems only."""
    return [
        problem
        for problem in (check_store_format(Path(directory)) for directory in directories)
        if problem is not None
    ]


def _mirror_files(files: Dict[Path, Path], dst: Path, keep: Set[Path]) -> int:
    """
    Make ``dst`` an exact mirror of ``files`` (relative path -> source path),
    except for the relative paths in ``keep``, which are left as they are.

    Files present in ``dst`` but neither in ``files`` nor in ``keep`` are
    deleted. This is the RESTORE direction only: the work repo is the truth
    for what a session starts with, so a memory it no longer holds must not
    linger in the agent's dir. Persist goes the other way into a store other
    sessions write, and merges instead (:func:`_write_scope`, issue #369).
    Existing files are overwritten; missing parent directories are created.

    Returns the number of files copied.
    """
    dst.mkdir(parents=True, exist_ok=True)

    # Remove destination files that no longer exist in the source.
    for item in dst.rglob("*"):
        if item.is_file():
            rel = item.relative_to(dst)
            if rel not in files and rel not in keep:
                item.unlink()

    # Copy/overwrite every source file into the destination.
    count = 0
    for rel, source in files.items():
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        count += 1
    return count


def _mirror_tree(src: Path, dst: Path) -> int:
    """Make ``dst`` an exact mirror of ``src`` (files only).

    The single-scope copy: whole directory in, whole directory out, index
    bytes included. A legacy tree has exactly one memory scope, so this is
    still its entire RESTORE path. Persist never mirrors (issue #369).
    """
    return _mirror_files(_files_under(src), dst, set())


def _copy_files(
    files: Dict[Path, Path], dst: Path, restored: Optional[Dict[str, str]] = None
) -> int:
    """Copy ``files`` (relative path -> source path) into *dst*; delete nothing.

    A write is skipped when THIS SESSION HAS NO OPINION about the file: it
    restored the file, still holds exactly the bytes it was handed, and the
    scope's copy has since changed. That combination means another session
    edited it after this one read it, and writing would revert their edit —
    the same loss :func:`_restored_then_removed` refuses on the delete side,
    one level down on the file body. Symmetry matters here: gating the delete
    and leaving the write open still loses another session's work.

    Everything else is written: a file this session created or edited (its
    bytes differ from the recorded digest, so it has an opinion), a file the
    scope has not touched since the restore, and any file with no recorded
    digest. Where BOTH sides changed, last-writer-wins — that is a genuine
    conflict between two opinions, not a session overwriting one it does not
    hold.

    *restored* is ``{relative path: digest}`` from the restore manifest;
    ``None`` (no manifest) means no file can be shown to be unopinionated, so
    everything is written, exactly as before the manifest existed.

    Existing files are overwritten, missing parents created. Returns the
    number of files copied.
    """
    dst.mkdir(parents=True, exist_ok=True)
    recorded = restored or {}
    skipped: Set[Path] = set()
    conflicts: List[Path] = []
    for rel, source in files.items():
        handed = recorded.get(rel.as_posix())
        if not handed or (dst / rel).is_file():
            continue
        if _digest(source) == handed:
            # Unchanged here, deleted there -> this session has no opinion.
            skipped.add(rel)
        else:
            conflicts.append(rel)
    if conflicts:
        paths = ", ".join(rel.as_posix() for rel in conflicts)
        raise MemoryPersistConflict(
            "locally edited restored memory was deleted upstream; refusing "
            f"accidental revival: {paths}"
        )

    count = 0
    for rel, source in files.items():
        if rel in skipped:
            continue
        target = dst / rel
        handed = recorded.get(rel.as_posix())
        if handed and target.is_file():
            # Unchanged here, changed there -> this session has no opinion.
            if _digest(source) == handed and _digest(target) != handed:
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        count += 1
    return count


def _restored_then_removed(
    dest: Path, restored: Dict[str, str], held: Set[Path], keep: Set[Path]
) -> Set[Path]:
    """What this persist may delete from *dest*: nothing else, ever.

    A path qualifies only when this session restored it from this scope
    (*restored*, from the manifest), no longer holds it for this scope
    (*held* is what persist is writing here), is not protected by *keep*, and
    still carries the bytes this session was handed. Different bytes mean
    another session rewrote the file after this one read it — their update is
    newer than this session's decision, so the deletion does not carry.

    An index file never qualifies, whatever *restored* says. ``MEMORY.md`` and
    the area indexes are merged line by line, never held as files, so they
    are never in *held* — and a manifest written before #435 lists
    ``areas/*.md`` under ``files``, as plain memories. Trusting it deleted
    every area index of a split store whenever restore and persist ran
    different code. An area index goes only through ``removed_areas`` in
    :func:`_write_scope`, which reads the manifest's own ``areas`` record.
    """
    doomed: Set[Path] = set()
    for rel_posix, digest in restored.items():
        rel = Path(rel_posix)
        if rel in held or rel in keep or _is_index_rel(rel):
            continue
        path = dest / rel
        if not path.is_file():
            continue
        if not digest or _digest(path) != digest:
            continue
        doomed.add(rel)
    return doomed


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


@work_transaction
def restore_memory() -> int:
    """
    Restore saved agent memory from the work repo into Claude's memory dir.

    Reads the UNION of the memory scopes (D17), the project scope overlaying
    the instance one: a file at the same relative path wins outright, and a
    file declaring the same frontmatter ``name`` under a different filename
    shadows the instance copy (which persist then knows never to delete). The
    scopes' ``MEMORY.md`` indexes merge into the one the agent reads.

    Records what it restored, per scope, in the restore manifest
    (:func:`restore_manifest_path`) — the only thing a later persist is
    allowed to delete from a shared scope (issue #369).

    No-op (returns 0) unless ``LMER_PERSIST_AGENT_MEMORY`` is enabled. Safe to
    call when nothing has been saved yet — it simply reports a fresh start.

    Returns:
        Exit code (0 on success or graceful no-op, 1 on copy failure)
    """
    if not get_bool_env(PERSIST_ENV_VAR):
        return 0

    root = work_repo_root()
    try:
        retired_keys = _schema2_retired_keys(root)
    except MemoryRetirementError as e:
        print(f"❌ Invalid memory retirement state: {e}", file=sys.stderr)
        return 1

    # Guarantee the on-disk store unconditionally once persistence is on:
    # session context (Claude Code's memory instructions, the harness
    # agent-memory fragment) promises the agent an existing directory gated
    # solely on LMER_PERSIST_AGENT_MEMORY — a missing path invites an
    # improvised memory/ dir inside the project workspace instead. This
    # holds for every graceful no-op branch below, not just fresh start.
    dest = agent_memory_dir()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"⚠️  Could not create agent memory dir: {e}", file=sys.stderr)

    # Every path out of here rewrites the manifest, including the ones that
    # restore nothing: a session that restored nothing has deleted nothing,
    # and a manifest left over from an earlier restore of this same dir must
    # never be the record persist deletes on.
    write_restore_manifest({})

    scopes = memory_scope_dirs()
    if not scopes:
        print(
            "⚠️  Cannot restore agent memory: LMER_REPO_HOST/LMER_REPO_PROJECT not set",
            file=sys.stderr,
        )
        _report_manifest_problem()
        return 0
    retired = _retired_by_scope(scopes, retired_keys, root)
    for problem in _store_format_problems(scopes.values()):
        # Restore only reads, so it hands the agent its memory and says so;
        # every writer refuses the scope until the image is updated (#598).
        print(
            f"⚠️  {problem} — restoring anyway; persisting will be refused",
            file=sys.stderr,
        )

    if work_repo_layout().form == "legacy":
        # One scope: the whole directory, index bytes and all. This is the
        # legacy tree's entire restore, unchanged.
        source = next(iter(scopes.values()))
        if not _has_files(source):
            print("🧠 No saved agent memory found in work repo — starting fresh")
            return 0
        try:
            copied = _mirror_tree(source, dest)
        except OSError as e:
            print(f"⚠️  Failed to restore agent memory: {e}", file=sys.stderr)
            return 1
        restored = _files_under(source)
        index = restored.pop(Path(MEMORY_INDEX), None)
        areas = _pop_area_indexes(restored)
        write_restore_manifest(
            {
                PROJECT_SCOPE: _scope_snapshot(
                    restored,
                    _read_text(index) if index is not None else "",
                    {rel: _read_text(path) for rel, path in areas.items()},
                )
            }
        )
        print(f"🧠 Restored {copied} agent memory file(s) from work repo into {dest}")
        _warn_restored_index_budget(dest)
        return 0

    union, indexes, counts, taken = _union_of_scopes(scopes, retired)
    if not union and not indexes:
        print("🧠 No saved agent memory found in work repo — starting fresh")
        return 0

    try:
        copied = _mirror_files(union, dest, set())
        for rel, texts in indexes.items():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            (dest / rel).write_text(_merge_index(texts), encoding="utf-8")
            copied += 1
    except OSError as e:
        print(f"⚠️  Failed to restore agent memory: {e}", file=sys.stderr)
        return 1

    write_restore_manifest(
        {
            scope: _scope_snapshot(
                files,
                _filter_retired_index(
                    _read_text(scopes[scope] / MEMORY_INDEX),
                    retired.get(scope, set()),
                    split=_is_split(scopes[scope]),
                ),
                {
                    rel: _filter_retired_index(
                        _read_text(path), retired.get(scope, set()), rel.as_posix(), split=True
                    )
                    for rel, path in _area_indexes(scopes[scope]).items()
                },
            )
            for scope, files in taken.items()
            if scope in scopes
        }
    )

    print(f"🧠 Restored {copied} agent memory file(s) from work repo into {dest}")
    _warn_restored_index_budget(dest)
    print(
        f"   scopes: instance {counts[INSTANCE_SCOPE]}, "
        f"project {counts[PROJECT_SCOPE]}"
    )
    return 0


def _union_of_scopes(
    scopes: Dict[str, Path], retired: Optional[Dict[str, Set[Path]]] = None
):
    """The union of both memory scopes: files, indexes, counts, provenance.

    Instance scope underneath, project scope on top (D17): a project file at
    the same relative path replaces the instance one, and a project file
    declaring the same ``name`` under a different filename drops it — a name
    is one memory, and recalling two files that both claim it is how the
    "more specific scope wins" rule would get lost in practice.

    The fourth return is which scope each restored file actually CAME FROM,
    which is what the manifest records: an instance file the project scope
    overlaid was not handed to the agent, so this session never restored it
    and can never be the one to delete it.
    """
    retired = retired or {INSTANCE_SCOPE: set(), PROJECT_SCOPE: set()}
    instance_files = {
        rel: path
        for rel, path in (
            _files_under(scopes[INSTANCE_SCOPE]) if INSTANCE_SCOPE in scopes else {}
        ).items()
        if rel not in retired.get(INSTANCE_SCOPE, set())
    }
    project_files = {
        rel: path
        for rel, path in (
            _files_under(scopes[PROJECT_SCOPE]) if PROJECT_SCOPE in scopes else {}
        ).items()
        if rel not in retired.get(PROJECT_SCOPE, set())
    }
    # Every index file — the map and each area index — merges across the
    # scopes by relative path, instance first, exactly as MEMORY.md always has.
    indexes: Dict[Path, List[str]] = {}
    for scope, files in (
        (INSTANCE_SCOPE, instance_files),
        (PROJECT_SCOPE, project_files),
    ):
        index = files.pop(Path(MEMORY_INDEX), None)
        found = {Path(MEMORY_INDEX): index} if index is not None else {}
        areas = _pop_area_indexes(files)
        found.update(areas)
        for rel, path in sorted(found.items()):
            indexes.setdefault(rel, []).append(
                _filter_retired_index(
                    _read_text(path),
                    retired.get(scope, set()),
                    rel.as_posix(),
                    split=bool(areas),
                )
            )

    claimed = {
        name
        for name in (_memory_name(path) for path in project_files.values())
        if name is not None
    }
    union = {
        rel: path
        for rel, path in instance_files.items()
        if rel in project_files or _memory_name(path) not in claimed
    }
    union.update(project_files)
    counts = {
        INSTANCE_SCOPE: len(union) - len(project_files),
        PROJECT_SCOPE: len(project_files),
    }
    taken = {
        INSTANCE_SCOPE: {
            rel: path
            for rel, path in instance_files.items()
            if rel in union and union[rel] is path
        },
        PROJECT_SCOPE: project_files,
    }
    return union, indexes, counts, taken


# ---------------------------------------------------------------------------
# The session-start load budget (issue #435)
# ---------------------------------------------------------------------------


def _index_budget() -> Optional[MemoryIndexLimit]:
    """The budget the loaded index must fit: the strictest harness's load."""
    return memory_index_budget()


def _lines_within(text: str, limit: MemoryIndexLimit) -> int:
    """How many of *text*'s lines a load capped at *limit* reaches.

    Cut at a line boundary, counting UTF-8 bytes: never more than the harness
    actually shows, whatever unit it counts in.
    """
    used = 0
    count = 0
    for line in text.splitlines(keepends=True):
        size = len(line.encode("utf-8"))
        if count + 1 > limit.max_lines or used + size > limit.max_bytes:
            break
        used += size
        count += 1
    return count


def _over_budget(text: str, limit: MemoryIndexLimit) -> bool:
    return (
        len(text.encode("utf-8")) > limit.max_bytes
        or len(text.splitlines()) > limit.max_lines
    )


def _loaded_index_text(
    scopes: Dict[str, Path], retired: Optional[Dict[str, Set[Path]]] = None
) -> str:
    """The ``MEMORY.md`` a restore would hand the agent: what the harness loads."""
    if work_repo_layout().form == "legacy" or len(scopes) == 1:
        index = next(iter(scopes.values())) / MEMORY_INDEX
        return _read_text(index) if index.is_file() else ""
    texts = []
    for scope in (INSTANCE_SCOPE, PROJECT_SCOPE):
        index = scopes[scope] / MEMORY_INDEX if scope in scopes else None
        if index is not None and index.is_file():
            texts.append(
                _filter_retired_index(
                    _read_text(index),
                    (retired or {}).get(scope, set()),
                    split=_is_split(scopes[scope]),
                )
            )
    return _merge_index(texts) if texts else ""


def _report_index_budget(
    scopes: Dict[str, Path], retired: Optional[Dict[str, Set[Path]]] = None
) -> None:
    """Print the loaded index's size against the budget, and the area layout.

    Every persist prints it, so the next drift is visible before it costs a
    session its index. A flat scope over the budget is warned, never refused.
    """
    text = _loaded_index_text(scopes, retired)
    size = len(text.encode("utf-8"))
    lines = len(text.splitlines())
    budget = _index_budget()
    over = budget is not None and _over_budget(text, budget)
    if budget is None:
        print(
            f"📏 MEMORY.md as loaded: {size:,} bytes, {lines} lines "
            f"(no harness declares a session-start load limit)"
        )
    else:
        print(
            f"📏 MEMORY.md as loaded at session start: {size:,} / "
            f"{budget.max_bytes:,} bytes, {lines} / {budget.max_lines} lines — "
            f"{'OVER' if over else 'ok'}"
        )
        if over:
            print(
                f"   sessions see only the first {_lines_within(text, budget)} "
                f"line(s) of it; the rest is never loaded"
            )
    areas = [
        (scope, rel, path)
        for scope, directory in scopes.items()
        for rel, path in sorted(_area_indexes(directory).items())
    ]
    if not areas:
        print("   flat index (no areas/): every entry line is loaded at session start")
        if over:
            print(
                "⚠️  The memory index is over the session-start load. Split it: "
                "keep MEMORY.md as a map of areas and move entry lines into "
                "areas/<slug>.md indexes (docs/SCHEMA.md, \"memory\"). A flat "
                "index is warned, never refused.",
                file=sys.stderr,
            )
        return
    sizes = [
        (len(_read_text(path).encode("utf-8")), rel, scope, path) for scope, rel, path in areas
    ]
    largest_size, largest_rel, largest_scope, largest_path = max(sizes, key=lambda s: s[0])
    unsorted = sum(
        len(_indexed_in(_read_text(path), rel.as_posix(), split=True))
        for _scope, rel, path in areas
        if rel == _area_rel(UNSORTED_AREA)
    )
    where = f" [{largest_scope}]" if len(scopes) > 1 else ""
    print(
        f"   split index: {len(areas)} area index(es), largest "
        f"{largest_rel.as_posix()}{where} ({largest_size:,} bytes, "
        f"{len(_indexed_in(_read_text(largest_path), largest_rel.as_posix(), split=True))} "
        f"entries); unsorted: {unsorted} line(s)"
    )


def _warn_restored_index_budget(dest: Path) -> None:
    """Warn at session start when the restored index is over the load."""
    budget = _index_budget()
    index = dest / MEMORY_INDEX
    if budget is None or not index.is_file():
        return
    text = _read_text(index)
    if not _over_budget(text, budget):
        return
    print(
        f"⚠️  MEMORY.md is {len(text.encode('utf-8')):,} bytes / "
        f"{len(text.splitlines())} lines, over the session-start load "
        f"({budget.max_bytes:,} bytes / {budget.max_lines} lines): only the "
        f"first {_lines_within(text, budget)} line(s) will be read",
        file=sys.stderr,
    )


def _snapshot_dirs(directories: List[Path]) -> Dict[Path, tuple]:
    """Every file's bytes and every subdirectory under *directories*.

    Also the nearest ancestor of each that already exists (the directory
    itself when it does), so a rollback can remove a scope dir — and the
    parents — that the persist created.
    """
    snapshot = {}
    for directory in directories:
        anchor = directory
        while not anchor.exists() and anchor != anchor.parent:
            anchor = anchor.parent
        snapshot[directory] = (
            {rel: path.read_bytes() for rel, path in _files_under(directory).items()},
            {p for p in directory.rglob("*") if p.is_dir()} if directory.is_dir() else set(),
            anchor,
        )
    return snapshot


def _rollback_dirs(snapshot: Dict[Path, tuple]) -> None:
    """Return each snapshotted directory to exactly the files and dirs it held."""
    for directory, (files, dirs, anchor) in snapshot.items():
        for rel, path in _files_under(directory).items():
            if rel not in files:
                path.unlink()
        for rel, data in files.items():
            path = directory / rel
            if not path.is_file() or path.read_bytes() != data:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        if directory.is_dir():
            for sub in sorted(directory.rglob("*"), reverse=True):
                if sub.is_dir() and sub not in dirs and not any(sub.iterdir()):
                    sub.rmdir()
        created = directory
        while created != anchor and created.is_dir() and not any(created.iterdir()):
            created.rmdir()
            created = created.parent


def _refuses_growth(
    scopes: Dict[str, Path], retired: Dict[str, Set[Path]], before: str
) -> bool:
    """Whether this persist grew an over-budget map in a split index.

    The rollout rule (#435): a flat index is only warned — refusing it would
    fail every wind-down persist of a store that predates the split — and so
    is a load that includes any flat scope's index. Only when every scope
    with an index is split does growth past the budget come from the map
    alone: a line added to it, or a new area's map line. Says so when it is.
    """
    budget = _index_budget()
    if budget is None or not any(_is_split(path) for path in scopes.values()):
        return False
    if any(
        not _is_split(path) and _read_text(path / MEMORY_INDEX).strip()
        for path in scopes.values()
    ):
        # A flat scope's index is part of what loads: the overage is not the
        # split map's to answer for, and a flat index is never refused.
        return False
    after = _loaded_index_text(scopes, retired)
    if not _over_budget(after, budget):
        return False
    grew = len(after.encode("utf-8")) > len(before.encode("utf-8")) or len(
        after.splitlines()
    ) > len(before.splitlines())
    if grew:
        _refuse_growth(before, after, budget)
    return grew


def _is_generated_map_line(line: str) -> bool:
    """Whether *line* is exactly what :func:`_map_line` writes for a new area.

    A hand-written or lengthened hook on an existing area's map line is the
    agent's, never "this persist creates a new area".
    """
    match = _MAP_LINE_RE.match(line)
    return match is not None and line.strip() == _map_line(match.group(1)).strip()


def _refuse_growth(before: str, after: str, budget: MemoryIndexLimit) -> None:
    """Say why a split scope's persist was refused, and how to get unblocked."""
    previous = {line.strip() for line in before.splitlines()}
    added = [line for line in after.splitlines() if line.strip() and line.strip() not in previous]
    generated = [line for line in added if _is_generated_map_line(line)]
    added = [line for line in added if line not in generated]
    print(
        f"❌ Refusing to persist agent memory: MEMORY.md as loaded would grow from "
        f"{len(before.encode('utf-8')):,} to {len(after.encode('utf-8')):,} bytes "
        f"({len(before.splitlines())} → {len(after.splitlines())} lines), over the "
        f"{budget.max_bytes:,}-byte / {budget.max_lines}-line session-start load.",
        file=sys.stderr,
    )
    if added:
        print(
            "   This memory index is split, so MEMORY.md is only the map of areas. "
            "To get unblocked, move these lines out of MEMORY.md in your memory "
            "dir into an area index, areas/<slug>.md (link `../<file>.md`), or "
            "shorten them, then persist again:",
            file=sys.stderr,
        )
        for line in added:
            print(f"     {line}", file=sys.stderr)
    if generated:
        print(
            "   This persist creates a new area, and the map has no room for its "
            "line. File those memories into an existing area instead (its "
            "areas/<slug>.md, or `metadata.area: <slug>`), or ask for the map "
            "to be trimmed:",
            file=sys.stderr,
        )
        for line in generated:
            print(f"     {line}", file=sys.stderr)
    print("   Nothing was written to the work repo.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------


#: ``<stem>.conflict-<sid>[-<n>].md`` at the memory dir's root, where <sid> is
#: a schema-2 uid or a schema-1 timestamp id: the only names
#: :func:`_write_conflict_copies` writes, so a memory merely mentioning
#: ``.conflict-`` in its name is still a memory.
_CONFLICT_COPY_RE = re.compile(
    r"\.conflict-(?:[0-9a-f]{32}|\d{8}-\d{6}-\d+-[0-9A-Za-z]+|unknown)(?:-\d+)?\.md$"
)


def _is_conflict_copy(rel: Path) -> bool:
    """Whether *rel* is a ``.conflict-<sid>.md`` copy this module wrote.

    These live in the agent's memory dir so the session's version of a
    conflicted memory is not lost, but they are NOT memories: routing one
    would persist a session id into a shared scope, and restoring one would
    resurrect it as a real memory. They are skipped on the way out and cleared
    by the restore mirror (which makes the work repo the truth again).
    """
    return len(rel.parts) == 1 and _CONFLICT_COPY_RE.search(rel.name) is not None


def _write_conflict_copies(
    conflicts, scope_rels: List[str]
) -> tuple[List[str], List[tuple]]:
    """Save UPSTREAM's side of each conflicting memory beside the agent's own.

    Returns ``(written, uncopied)``: the copy names, and ``(path, reason)``
    for each conflicting path that got none. The session's own version needs
    no copy — it is still the agent's file. What the agent has not seen is
    the version another session published, ``ours`` (index stage 2, the
    commit being rebased ONTO); saving it is what gives the agent something
    to reconcile before it persists again. No copy for the index
    (``MEMORY.md``), for a path outside *scope_rels* (not a memory), for a
    path this session deleted (no stage 3), or for one upstream deleted (no
    stage 2). A copy never overwrites an earlier, unreconciled one.
    """
    sid = launch_session_id() or "unknown"
    dest = agent_memory_dir()
    written: List[str] = []
    uncopied: List[tuple] = []
    for conflict in conflicts or []:
        name = Path(conflict.path).name
        if not any(
            PurePosixPath(conflict.path).is_relative_to(rel) for rel in scope_rels
        ):
            uncopied.append((conflict.path, "not a memory"))
            continue
        if name == MEMORY_INDEX:
            uncopied.append((conflict.path, "the index; merged again on the next persist"))
            continue
        if conflict.theirs is None:
            uncopied.append((conflict.path, "deleted by this session, edited upstream"))
            continue
        if conflict.ours is None:
            uncopied.append((conflict.path, "deleted upstream, edited by this session"))
            continue
        stem = Path(name).stem
        copy_name = f"{stem}.conflict-{sid}.md"
        n = 1
        while (dest / copy_name).exists():
            n += 1
            copy_name = f"{stem}.conflict-{sid}-{n}.md"
        target = dest / copy_name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(conflict.ours, encoding="utf-8")
        except OSError as e:
            print(
                f"⚠️  Could not save the conflicted memory copy {target}: {e}",
                file=sys.stderr,
            )
            uncopied.append((conflict.path, "the copy could not be written"))
            continue
        written.append(copy_name)
    return written, uncopied


def _skip_pre_merge_pull(root: Path) -> Optional[str]:
    """The reason persist must NOT pull first, or ``None`` to pull.

    A live gate means a sibling process owns the checkout and a pull would
    race it, as :func:`commit_work_path` defers for. (Its other deferral, an
    inherited Git transaction, cannot reach here: persist's checkout lock
    refuses it first; the check stays as a guard for other callers.) A root that is not a Git work tree has nothing
    to pull — production never reaches it, because ``check_commit_ready``
    refuses a non-Git root first, but the plain-directory regression fixtures
    do.
    """
    if inherited_transaction(root):
        return "a Git transaction is already in progress"
    if gate_lock.active_gate() is not None:
        return "a gate is in flight"
    if not _is_git_work_tree(root):
        return "the work-repo root is not a Git work tree"
    return None


def _is_git_work_tree(root: Path) -> bool:
    return (root / ".git").exists()


def _pull_before_merge(root: Path) -> None:
    """Integrate the published branch before persist merges into it (#369 (b)).

    The three-way merge in :func:`_write_scope` uses the checkout's CURRENT
    bytes as the upstream side; without this pull that side is whatever the
    last writer left locally, so a sibling's add or edit is invisible until it
    collides at the post-commit rebase. A pull that cannot fast-forward
    changes nothing and is reported; the merge then runs against the local
    side and the post-commit rebase stays the backstop, as before.
    """
    skip = _skip_pre_merge_pull(root)
    if skip is not None:
        print(f"⚠️  Skipping the pre-merge memory pull: {skip}", file=sys.stderr)
        return
    rc, reason = fast_forward_upstream(root, "work repo")
    if rc != 0:
        print(f"⚠️  Pre-merge memory pull did not integrate: {reason}", file=sys.stderr)


def _roll_back_manifest(manifest_before: Optional[str]) -> None:
    """Put the restore manifest back to its bytes before this persist."""
    path = restore_manifest_path()
    try:
        if manifest_before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(manifest_before, encoding="utf-8")
    except OSError as e:
        print(f"⚠️  Could not roll back the memory restore manifest {path}: {e}", file=sys.stderr)


def _publish_conflict(conflicts, scope_rels: List[str], manifest_before: Optional[str]) -> None:
    """The ``on_conflict`` handler for the post-commit rebase.

    The shared checkout has aborted the rebase and taken this persist's
    changes back, so it stays publishable. The restore manifest goes back to
    what it was before this persist: it had already been moved forward to
    "what this session holds", and keeping that would make the next persist
    read the unpublished adds and edits as already published and skip them.
    Upstream's version of each conflicting memory is saved beside the agent's
    own (:func:`_write_conflict_copies`), and every outcome is said —
    recovery is the agent merging the copies into its files and persisting
    again.
    """
    _roll_back_manifest(manifest_before)
    copies, uncopied = _write_conflict_copies(conflicts, scope_rels)
    print(
        "❌ Memory publication conflicted; this persist's commit was dropped "
        "and nothing of it was published",
        file=sys.stderr,
    )
    for name in copies:
        print(
            f"   upstream's version saved as {name} — merge it into your own "
            "file, then delete the copy",
            file=sys.stderr,
        )
    for rel, reason in uncopied:
        print(f"   {rel}: conflicted, no copy ({reason})", file=sys.stderr)
    print("   Then run `work memory persist` again.", file=sys.stderr)


@work_transaction
def persist_memory(
    commit_message: str | None = None, allow_unbaselined_delete: bool = False
) -> int:
    """
    Persist Claude's current agent memory to the work repo and push it.

    Each file is routed by its own ``metadata.type`` (D17): in a schema-form
    tree ``project`` memories land in the project scope and the rest at the
    instance scope, each scope keeping its own ``MEMORY.md``; in a legacy tree
    there is one scope and every type lands in ``{host}/{project}/memory``.
    The scopes actually written are committed and pushed together via
    :func:`commit_work_path`.

    A scope is MERGED, not mirrored: this session's files are written over
    it and the only files removed from it are the ones this session restored
    and has since deleted (:func:`_write_scope`, issue #369). Everything else
    in the scope belongs to a session this one knows nothing about.

    With no restore baseline at all, persist deletes nothing implicitly; it
    names the scope files the agent dir no longer holds and declines to
    propagate their absence. *allow_unbaselined_delete* deletes exactly that
    named set. Retirement enforcement is unaffected: an explicit ``forget``
    is durable state, not a baseline question.

    No-op (returns 0) unless ``LMER_PERSIST_AGENT_MEMORY`` is enabled.

    Returns:
        Exit code (0 on success or graceful no-op, non-zero on failure)
    """
    if not get_bool_env(PERSIST_ENV_VAR):
        print(f"🧠 {PERSIST_ENV_VAR} is not enabled — skipping agent memory persist")
        return 0

    scopes = memory_scope_dirs()
    if not scopes:
        # The project scope is where an unroutable memory goes, so without it
        # there is no home for part of what we hold. Never fall back to the
        # instance scope alone: that would file this project's work where
        # every project reads it.
        print(
            "❌ Cannot persist agent memory: LMER_REPO_HOST/LMER_REPO_PROJECT not set",
            file=sys.stderr,
        )
        _report_manifest_problem()
        return 1

    root = work_repo_root()
    lock = (
        _memory_retirement_lock(root)
        if _schema2_manifest(root) is not None
        else nullcontext()
    )
    try:
        with lock:
            retired = _retired_by_scope(scopes, _schema2_retired_keys(root), root)

            source = agent_memory_dir()
            if not _has_files(source):
                # The #369 manifest distinguishes retiring restored memories
                # from a session handed none. A completed retirement may still
                # need pushing.
                restored = read_restore_manifest()
                retired_live = any(
                    (scopes[scope] / rel).is_file()
                    for scope, rels in retired.items()
                    for rel in rels
                )
                if (
                    not retired_live
                    and not any(
                        _manifest_files(restored, scope) for scope in scopes
                    )
                ):
                    print("🧠 No agent memory to persist")
                    _report_index_budget(scopes, retired)
                    return commit_work_path([], commit_message)

            rc = check_commit_ready(root)
            if rc != 0:
                return rc

            # Integrate the published branch BEFORE merging into it, so the
            # current side of the three-way merge is what upstream holds
            # (#369 gap (b)).
            _pull_before_merge(root)
            # A scope a newer lmer wrote (#598) is refused before anything is
            # read for writing: no manifest, no snapshot, no write.
            problems = _store_format_problems(scopes.values())
            if problems:
                for problem in problems:
                    print(f"❌ Refusing to persist agent memory: {problem}", file=sys.stderr)
                return 1
            # The pull may have brought in another session's forget.
            retired = _retired_by_scope(scopes, _schema2_retired_keys(root), root)
            scope_rels = [
                rel
                for rel in (_rel_to_root(path, root) for path in scopes.values())
                if rel is not None
            ]

            try:
                manifest_before = restore_manifest_path().read_text(encoding="utf-8")
            except FileNotFoundError:
                manifest_before = None
            except (OSError, UnicodeDecodeError):
                manifest_before = None
            loaded_before = _loaded_index_text(scopes, retired)
            snapshot = _snapshot_dirs(list(scopes.values()))
            try:
                # The default call keeps its pinned three-positional shape;
                # the flag rides as a keyword only when it is set.
                if allow_unbaselined_delete:
                    copied, rel_paths, deletions, declined = _persist_scopes(
                        source, scopes, retired, allow_unbaselined_delete=True
                    )
                else:
                    copied, rel_paths, deletions, declined = _persist_scopes(
                        source, scopes, retired
                    )
            except MemoryPersistConflict as e:
                print(f"❌ Agent memory persist conflict: {e}", file=sys.stderr)
                return 1
            except MemoryAreaMigrationError as e:
                _rollback_dirs(snapshot)
                _roll_back_manifest(manifest_before)
                _report_bad_areas(e, source)
                return 1
            except OSError as e:
                print(
                    f"❌ Failed to copy agent memory into work repo: {e}",
                    file=sys.stderr,
                )
                return 1
            if _refuses_growth(scopes, retired, loaded_before):
                _rollback_dirs(snapshot)
                _roll_back_manifest(manifest_before)
                return 1
    except MemoryRetirementError as e:
        print(f"❌ Invalid memory retirement state: {e}", file=sys.stderr)
        return 1

    _report_deletions(deletions, declined, _schema2_manifest(root) is not None)

    if not rel_paths:
        print("🧠 No agent memory to persist")
        _report_index_budget(scopes, retired)
        return commit_work_path([], commit_message)

    print(f"🧠 Merged {copied} agent memory file(s) into work repo")
    _report_index_budget(scopes, retired)

    if commit_message is None:
        commit_message = f"Update agent memory: {_memory_label()}"
    # One scope is staged as the bare path it always was; the list shape
    # appears only when there is genuinely more than one scope to stage.
    target = rel_paths[0] if len(rel_paths) == 1 else rel_paths
    if not _is_git_work_tree(root):
        # Nothing to rebase, so nothing can conflict; the plain-tree pins
        # stub commit_work_path with this exact positional shape.
        return commit_work_path(target, commit_message)
    return commit_work_path(
        target,
        commit_message,
        on_conflict=lambda conflicts: _publish_conflict(
            conflicts, scope_rels, manifest_before
        ),
    )


def _report_deletions(
    deletions: List[tuple], declined: List[tuple], can_forget: bool = False
) -> None:
    """Name every file persist removed, and every absence it declined to.

    One line per deletion — the scope and the work-repo-relative path, with a
    retirement-enforced unlink labelled because it carries out an explicit
    ``forget`` rather than a session's own removal — then the total, which is
    ``0`` when a persist without a baseline declined to delete (#369 gap (d)).
    """
    for scope, rel, retired in deletions:
        suffix = " (retirement-enforced)" if retired else ""
        print(f"🗑️  Deleted {rel} [{scope}]{suffix}")
    if declined:
        # This persist records a baseline, so a later
        # --allow-unbaselined-delete would delete none of these; only an
        # explicit forget still can.
        how = (
            " — retire any you meant to remove with `work memory forget <path>`"
            if can_forget
            else ""
        )
        print(
            f"🗑️  Kept {len(declined)} file(s) this session does not hold: "
            f"no restore baseline, so their absence is not a deletion{how}:"
        )
        for scope, rel in declined:
            print(f"   {rel} [{scope}]")
    if deletions or declined:
        print(f"🗑️  Deleted {len(deletions)} file(s)")


def _persist_scopes(
    source: Path,
    scopes: Dict[str, Path],
    retired: Optional[Dict[str, Set[Path]]] = None,
    allow_unbaselined_delete: bool = False,
):
    """Write the agent's memory into the scopes it routes to.

    Returns ``(copied, rel_paths, deletions, declined)``: the number of files
    copied, the work-repo-relative path of every scope written (the paths
    staged for commit, a subset of the journalled scope
    :func:`memory_write_rel_paths`), the deletions as ``(scope, rel,
    retired)`` triples, and the unbaselined absences as ``(scope, rel)`` pairs
    this persist declined to propagate.
    """
    baseline = read_restore_baseline()
    # A whole-manifest absence or mismatch is "no baseline". A valid manifest
    # naming no files — for one scope or for all, as a fresh start writes —
    # is NOT: this session did restore, and its empty view is not licence to
    # delete what it never saw.
    unbaselined = baseline is None
    manifest = baseline if baseline is not None else {}
    _adopt_pre_435_area_bases(source, manifest)
    retired = retired or {scope: set() for scope in scopes}

    # Preflight every scope before deleting/filtering anything. In particular,
    # retirement filtering must not hide a locally edited restored file, and
    # a later-scope conflict must not leave earlier scopes partially written.
    conflicts = []
    for scope, dest in scopes.items():
        for rel, digest in _manifest_files(manifest, scope).items():
            local = source / rel
            if (
                local.is_file()
                and (Path(rel) in retired.get(scope, set()) or not (dest / rel).is_file())
                and digest
                and _digest(local) != digest
            ):
                conflicts.append(f"{scope}:{rel}")
    if conflicts:
        raise MemoryPersistConflict(
            "locally edited restored memory was deleted or retired upstream; "
            "refusing accidental revival: " + ", ".join(conflicts)
        )

    if work_repo_layout().form == "legacy":
        # One scope: the whole directory. Nothing is routed because there is
        # nowhere else for it to go — but the index is still merged rather
        # than written over, because one scope is still shared by every
        # session of this project (issue #369).
        dest = scopes[PROJECT_SCOPE]
        files = {
            rel: path
            for rel, path in _files_under(source).items()
            if not _is_conflict_copy(rel)
        }
        index = files.pop(Path(MEMORY_INDEX), None)
        areas = _pop_area_indexes(files)
        removed = _removed_areas(source, manifest, (PROJECT_SCOPE,))
        copied, deleted, declined = _write_scope(
            PROJECT_SCOPE,
            files,
            # Absence and an empty statement are different answers here
            # (:func:`_index_statement`), and this is the branch a LEGACY
            # work repo takes — the form the repos in use are in today, so
            # a blank `MEMORY.md` wiping the scope's index is live, not
            # theoretical.
            _index_statements(index, areas, removed),
            dest,
            set(),
            manifest,
            retired.get(PROJECT_SCOPE, set()),
            unbaselined=unbaselined,
            allow_unbaselined_delete=allow_unbaselined_delete,
            removed_areas=removed,
        )
        retired_here = retired.get(PROJECT_SCOPE, set())
        _settle_scope(
            dest, retired_here, source=source, planned=_planned_areas(dest, retired_here)
        )
        write_restore_manifest(manifest)
        rel = _rel_to_root(dest, work_repo_root())
        return copied, ([rel] if rel is not None else []), deleted, declined

    routed, scope_of = _route(source)
    unavailable = [scope for scope, rels in routed.items() if rels and scope not in scopes]
    if unavailable:
        raise MemoryPersistConflict(
            "no memory destination for scope(s): " + ", ".join(unavailable)
        )
    for scope, rels in retired.items():
        # The FILE stops being routed; its index line keeps its route. Dropping
        # the path from `scope_of` as well left the line unrouted, and the
        # split files an unrouted entry under DEFAULT_SCOPE (the project) —
        # whose retirement filter knows only the project's retired paths. An
        # instance-scope forget therefore leaked a live, dangling entry into
        # the project index of every session that had restored before it.
        # Routed to its own scope, the line meets the filter that knows it.
        routed[scope].difference_update(rels)
    # The memory names this persist routes to the PROJECT scope: the ones
    # restore shadows the instance copy of, and so the only names the keep
    # rule below runs on (the instance scope shadows nothing).
    project_names = {
        name
        for name in (_memory_name(source / rel) for rel in routed[PROJECT_SCOPE])
        if name is not None
    }
    indexes = _scope_indexes(source, scope_of, manifest, scopes)
    removed = _removed_areas(source, manifest, scopes)
    for scope, rels in retired.items():
        for rel, text in indexes.get(scope, {}).items():
            indexes[scope][rel] = _filter_retired_index(
                text, rels, rel.as_posix(), split=_is_split(source)
            )
    # Whether the agent has an index at all — the difference between
    # "no lines for this scope" and "nothing to say about indexes"
    # (:func:`_index_statement`, which the one-scope branch above asks the
    # same question of). Per SCOPE the split may legitimately be empty; that
    # is a statement. It is the FILE being absent or blank that is not.
    has_index = _index_statement(source / MEMORY_INDEX) is not None

    copied = 0
    rel_paths = []
    deletions: List[tuple] = []
    declined: List[tuple] = []
    root = work_repo_root()
    written: List[tuple] = []
    for scope, dest in scopes.items():
        rels = routed[scope]
        retired_here = retired.get(scope, set())
        has_retired_live_file = any((dest / rel).is_file() for rel in retired_here)
        if (
            not rels
            and not _manifest_files(manifest, scope)
            and not has_retired_live_file
        ):
            # Say nothing about a scope this session has nothing for: even a
            # merge writes an index, and the instance scope is shared by
            # every project's sessions.
            #
            # "Nothing for" has to mean nothing ROUTED and nothing RESTORED.
            # Routing alone reads a retirement that empties a scope as
            # silence: the last memory in it stays upstream and the next
            # restore hands it back, and a memory whose type moved to the
            # other scope leaves its old copy here to shadow the corrected
            # one forever (restore overlays project on instance). Both are
            # deletions this session asked for, and the manifest is what
            # distinguishes them from a scope it never touched.
            if _has_files(dest):
                print(
                    f"⚠️  Nothing routed to the {scope} memory scope — leaving "
                    f"{dest} untouched (a scope this session has nothing to "
                    f"say about is never written over)",
                    file=sys.stderr,
                )
            continue
        # One-directional: restore overlays project on instance, so only an
        # instance-scope file can be shadowed out of the agent's dir. A
        # project-scope file whose name is claimed by an instance-routed file
        # was handed to the agent and is now MOVING to the scope its type owns
        # (D17) — or being deleted. Keeping it would leave a migrated tree
        # duplicated in both scopes forever, the project copy then pinning
        # itself over every later instance-side update.
        claimed = project_names if scope == INSTANCE_SCOPE else set()
        scope_copied, scope_deleted, scope_declined = _write_scope(
            scope,
            {rel: source / rel for rel in rels},
            # An empty split is a STATEMENT ("I have no lines for this
            # scope") only when the agent actually has an index: then the
            # merge drops the lines this session restored and removed, which
            # is what stops a forgotten last entry leaving a line pointing at
            # a deleted file. When the agent has NO index at all there is no
            # statement to make — `_scope_indexes` returns {} — and treating
            # that as an empty one would drop every line this session
            # restored. `None` keeps the index untouched, as before.
            indexes.get(scope, {Path(MEMORY_INDEX): ""}) if has_index else None,
            dest,
            claimed,
            manifest,
            retired_here,
            unbaselined=unbaselined,
            allow_unbaselined_delete=allow_unbaselined_delete,
            removed_areas=removed,
        )
        written.append((scope, dest, retired_here))
        copied += scope_copied
        deletions.extend(scope_deleted)
        declined.extend(scope_declined)
        rel = _rel_to_root(dest, root)
        if rel is not None:
            rel_paths.append(rel)
    # Only now is every scope this persist writes in its end state, which is
    # what a link in any of them is judged against (#435) — and that end
    # state includes the areas each scope's reconcile is about to generate.
    planned: Set[str] = set()
    for _scope, dest, retired_here in written:
        planned |= _planned_areas(dest, retired_here)
    for scope, dest, retired_here in written:
        _settle_scope(
            dest, retired_here, _memory_peers(scope, scopes, root), source, planned
        )
    write_restore_manifest(manifest)
    return copied, rel_paths, deletions, declined


def _write_scope(
    scope: str,
    files: Dict[Path, Path],
    index_texts: Optional[Dict[Path, str]],
    dest: Path,
    claimed: Set[str],
    manifest: Dict[str, dict],
    retired: Optional[Set[Path]] = None,
    *,
    unbaselined: bool = False,
    allow_unbaselined_delete: bool = False,
    removed_areas: Set[Path] = frozenset(),
) -> tuple[int, List[tuple], List[tuple]]:
    """Write one scope: delete what this session removed, copy, merge the index.

    The deletion set is :func:`_restored_then_removed` and nothing else, so a
    file this session never saw survives a persist that does not mention it.
    The WRITE side is gated the same way (:func:`_copy_files`): a file this
    session restored, still holds unchanged, and that has since changed in the
    scope is not written back, because reverting another session's edit is the
    same loss as deleting their file.
    *manifest* is updated IN PLACE to this session's new view of the scope —
    what it now holds, not what the scope now contains — so the next persist
    from the same session can still tell its own deletions from another
    session's additions.

    With *unbaselined* (no restore baseline at all) a scope file the agent dir
    no longer holds is a local absence, not a deletion: it is returned as
    *declined* unless *allow_unbaselined_delete* is set, in which case exactly
    that set is unlinked. *retired* enforcement is unconditional either way,
    because it carries out an explicit ``forget``.

    The scope's indexes are not checked or reconciled here: that waits for
    every scope this persist writes (:func:`_settle_scope`), because a link
    is judged against what the whole persist leaves.

    Returns ``(copied, deleted, declined)``, where *deleted* is ``(scope, rel,
    retired)`` triples and *declined* is ``(scope, rel)`` pairs.
    """
    dest.mkdir(parents=True, exist_ok=True)
    root = work_repo_root()
    index_path = dest / MEMORY_INDEX
    held = set(files)
    keep = _shadowed(dest, held, claimed)
    restored = _manifest_files(manifest, scope)
    deleted: List[tuple] = []
    for rel in retired or set():
        path = dest / rel
        if path.is_file():
            path.unlink()
            deleted.append((scope, _display_rel(path, root), True))
    for rel in _restored_then_removed(dest, restored, held, keep):
        path = dest / rel
        path.unlink()
        deleted.append((scope, _display_rel(path, root), False))

    declined: List[tuple] = []
    if unbaselined:
        for rel in sorted(_files_under(dest)):
            if rel in held or rel in keep or _is_index_rel(rel):
                continue
            path = dest / rel
            if allow_unbaselined_delete:
                if path.is_file():
                    path.unlink()
                    deleted.append((scope, _display_rel(path, root), False))
            else:
                declined.append((scope, _display_rel(path, root)))

    copied = _copy_files(files, dest, restored)

    entry = manifest.setdefault(scope, {})
    # What THIS SESSION HOLDS — the source bytes — never the destination.
    #
    # Digesting `dest` records the work repo's copy even for a file the write
    # gate just DECLINED to overwrite, so the next cycle reads "I hold their
    # newer copy": the gate no longer fires (local != recorded), persist #2
    # reverts their edit, and a later removal deletes their newer file as
    # `restored_then_removed`. The skip and the record have to agree about
    # what this session has an opinion on, or the gate only survives one
    # cycle. For a file that WAS written the two are the same bytes; for a
    # file that was skipped the source is exactly the digest we were handed,
    # so both gates keep seeing local == recorded and dest != recorded.
    entry["files"] = {
        rel.as_posix(): _digest(source)
        for rel, source in files.items()
        if source.is_file()
    }
    if index_texts is not None and not _is_split(dest):
        # Splitting a scope is a deliberate migration (#435), never the side
        # effect of one session's persist: a flat scope takes no area index.
        # The memories still land, and the reconcile below indexes them here.
        ignored = sorted(
            rel.as_posix()
            for rel, text in index_texts.items()
            if rel != Path(MEMORY_INDEX) and _indexed_in(text, rel.as_posix(), split=True)
        )
        if ignored:
            print(
                f"⚠️  The {scope} memory index is flat (no {AREAS_DIR}/ in {dest}): "
                f"{', '.join(ignored)} not persisted — their memories are indexed "
                f"in {MEMORY_INDEX} instead",
                file=sys.stderr,
            )
        index_texts = {
            rel: text for rel, text in index_texts.items() if rel == Path(MEMORY_INDEX)
        }
    if index_texts is not None:
        for rel, text in index_texts.items():
            path = dest / rel
            current = _read_text(path) if path.is_file() else ""
            merged = _merged_index_text(
                current,
                text,
                _restored_index_lines(manifest, scope, rel),
                area=rel != Path(MEMORY_INDEX),
            )
            if rel != Path(MEMORY_INDEX) and not _indexed_in(merged, rel.as_posix(), split=True):
                # An area left with no entries: one this session deleted goes,
                # and one this scope does not have is never created for a
                # heading alone.
                if rel in removed_areas and path.is_file():
                    path.unlink()
                if rel in removed_areas or not path.is_file():
                    continue
            elif not merged.strip() and not path.is_file():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(merged, encoding="utf-8")
            copied += 1
        # What THIS SESSION HOLDS — its own index lines — never the merged
        # survivors, exactly as `entry["files"]` records the source and not
        # the destination.
        #
        # Intersecting with the survivors dropped precisely the lines the
        # merge had just protected. A line this session restored, still holds,
        # and that another session has since RETIRED does not survive (that is
        # the tombstone rule working), so it fell out of the record — and on
        # the next cycle it was no longer "restored", which makes it "a line
        # this session has and did not restore": an ADDITION. Cycle 2
        # resurrected the retired entry. That is the `a108934ae -> 40c997340`
        # incident itself, one cycle later.
        #
        # Recording what this session holds keeps the line in the base, so the
        # tombstone rule keeps applying on every later cycle instead of only
        # the first.
        entry["index"] = _index_lines(index_texts.get(Path(MEMORY_INDEX), ""))
        areas = dict(entry.get("areas") or {})
        areas.update(
            {
                rel.as_posix(): _index_lines(text)
                for rel, text in index_texts.items()
                if rel != Path(MEMORY_INDEX)
            }
        )
        if areas:
            entry["areas"] = areas
    else:
        entry.setdefault("index", [])

    # A split scope's map holds no entries: file what this persist (or an
    # older writer) left there into the areas, before reconciling (#435).
    _file_map_entries(dest)
    return copied, deleted, declined


def _settle_scope(
    dest: Path,
    retired: Set[Path],
    peers: tuple = (),
    source: Optional[Path] = None,
    planned: Set[str] = frozenset(),
) -> None:
    """Check and reconcile one scope's indexes against what the persist leaves.

    Runs once EVERY scope this persist writes has been written, so the merged
    view (:func:`_memory_view` over *dest* and its *peers*) is the end state:
    a memory this persist deletes from a scope written later is already gone,
    and an area the merge declined to recreate is not counted because the
    agent's dir (*source*) still holds a copy of it. *planned* adds the areas
    this persist's reconciles will generate, this scope's own included
    (:func:`_planned_areas`).

    A split scope is first judged against its migration contract, before the
    reconcile tombstones what a bad migration broke (#435). A flat scope
    ignores areas, so it has none to break. Only a problem in a line this
    session holds refuses (:func:`_holds_problem`): it can fix that one, and
    no other.

    The reconcile then makes the scope's OWN indexes agree with its live files
    and retirement state (#369 gap (e)). It runs for both tree forms and even
    when the agent has no index: this persist changed the scope's live files,
    and a dangling entry is a validator ERROR in schema form. The session's
    recorded ``index`` stays what the session HOLDS, so the next persist still
    tells its own removals from another writer's edits.
    """
    memories, areas = _memory_view(dest, peers)
    areas |= planned
    if _is_split(dest):
        problems = _area_problems(dest, memories, areas)
        held = [p for p in problems if source is None or _holds_problem(source, p)]
        if held:
            raise MemoryAreaMigrationError(dest, held)
        if problems:
            _warn_unheld_area_problems(dest, problems, work_repo_root())
    _reconcile_scope_indexes(dest, retired, memories, areas)


def _tombstone_date() -> str:
    """The date a reconciled tombstone carries (``YYYY-MM-DD``, UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _tombstone_line(
    line: str,
    live: Set[str],
    when: str,
    index_rel: Optional[str] = None,
    *,
    split: bool,
) -> str:
    """Replace the link of every DEAD entry on *line* with ``archived <date>``.

    The ``docs/SCHEMA.md`` 'Index lines' tombstone: the line stays and its
    hand-written text survives; only the link to a file that is no longer
    there goes. A link that is not a relative ``.md`` entry (a URL, an anchor,
    a link already pointing at ``_archive/``, a map line's area index) is left
    alone, and so is an entry whose file IS live — a line may legitimately
    carry several. Links resolve against *index_rel*, and *split* says whether
    an area link is one (:func:`_entry_targets`).
    """
    base = posixpath.dirname(index_rel) if index_rel else ""

    def replace(match: "re.Match") -> str:
        target = match.group(1)
        if not target.lower().endswith(".md") or _NOT_RELATIVE_RE.match(target):
            return match.group(0)
        normalized = posixpath.normpath(posixpath.join(base, target))
        if any(part in _ARCHIVE_MARKERS for part in normalized.split("/")):
            return match.group(0)
        if normalized in live or normalized == MEMORY_INDEX:
            return match.group(0)
        if split and _is_area_index(normalized):
            return match.group(0)
        return f"archived {when}"

    return _LINK_RE.sub(replace, line)


def _generated_entry(
    dest: Path, rel: Path, index_rel: str = MEMORY_INDEX
) -> Optional[str]:
    """A generated index line for a live memory with no entry, or ``None``.

    Form: ``- [<frontmatter name or stem>](<relative path>) — <description>``,
    the path relative to *index_rel* (``../<file>`` from an area index).
    The title prefers the memory's own ``name``, falling back to the filename
    stem; the description is the frontmatter's, omitted when absent so the
    line is still a valid entry.

    The line MUST round-trip through ``_entry_targets`` and the validator's
    ``parse_index_entries``, or the next persist re-appends it forever. Both
    parse one line with the same bracket-link pattern, so the title may hold
    no ``]`` and no newline, and the path no whitespace or ``)``. A name like
    ``Cache [v2]`` loses its inner ``]`` (the closing one is the syntax), and
    a block-scalar name collapses to one line. A path the grammar cannot
    address returns ``None`` — leaving the file unindexed is a WARN, while an
    unparseable line is an unbounded growth loop.
    """
    path = posixpath.relpath(rel.as_posix(), posixpath.dirname(index_rel) or ".")
    if any(ch.isspace() for ch in path) or ")" in path:
        return None
    data = _frontmatter(_read_text(dest / rel))
    name = data.get("name") if data is not None else None
    title = name if isinstance(name, str) and name.strip() else rel.stem
    title = " ".join(title.split()).replace("]", "")
    if not title:
        title = " ".join(rel.stem.split()).replace("]", "") or "memory"
    line = f"- [{title}]({path})"
    description = data.get("description") if data is not None else None
    if isinstance(description, str) and description.strip():
        line += f" — {' '.join(description.split())}"
    return line + "\n"


def _has_exact_archive_tombstone(
    root: Path, text: str, dest: Path, rel: Path, index_rel: Optional[str] = None
) -> bool:
    """Whether *text* re-points *rel*'s link at *rel*'s own archive mirror.

    The identity-bearing half of the retirement contract: ``work memory
    forget`` re-points the entry at ``<scope>/_archive/.../<file>``, so a
    tombstone whose target normalizes to exactly that path is a retirement of
    THIS file. Nothing about a title, a name or a nearby word is consulted —
    a handwritten sentence or a link to a different archived memory cannot
    match, which is what keeps an unrelated mention from hiding a live file.
    *text* is the index file at *index_rel* (``MEMORY.md`` when omitted).
    """
    try:
        scope_rel = dest.relative_to(root)
    except ValueError:
        return False
    expected = posixpath.normpath(
        posixpath.relpath(
            _archive_rel_for_key((scope_rel / rel).as_posix()),
            scope_rel.as_posix(),
        )
    )
    base = posixpath.dirname(index_rel) if index_rel else ""
    for line in text.splitlines():
        for target in _LINK_RE.findall(line):
            if not target.lower().endswith(".md") or _NOT_RELATIVE_RE.match(target):
                continue
            if posixpath.normpath(posixpath.join(base, target)) == expected:
                return True
    return False


def _live_memory_rels(dest: Path) -> Set[str]:
    """The scope's live memory files, as the POSIX paths entries resolve to."""
    return {
        rel.as_posix()
        for rel in _files_under(dest)
        if rel.suffix.lower() == ".md"
        and not _is_index_rel(rel)
        and not _is_conflict_copy(rel)
    }


def _reconcile_index(
    text: str,
    dest: Path,
    retired: Set[Path],
    index_rel: str = MEMORY_INDEX,
    *,
    indexed_elsewhere: Optional[Set[str]] = None,
    generate: bool = True,
    split: bool,
) -> str:
    """Make one index file's entry lines agree with the scope's live files.

    Entry lines for files that are gone are tombstoned IN PLACE (never
    dropped); a live memory file with no entry gets a generated line appended,
    unless the scope's recorded retirement state accounts for it — *retired*
    (the keys ``work memory forget`` recorded) or an exact archive-mirror
    tombstone for that file (:func:`_has_exact_archive_tombstone`). No
    handwritten prose, title or nearby word suppresses a generated entry; the
    identity is the recorded key or the exact mirror path, nothing else.
    Every non-entry line — headings, hand-written commentary, blank lines,
    map lines and the tombstones ``work memory forget`` wrote — is kept
    byte-identical and in position. This runs after the line merge, on the
    scope's own index, so it reads the scope's live files plus its retirement
    state and never one session's view.

    *indexed_elsewhere* names files another index file of the scope already
    carries; *generate* is off where the caller files missing entries itself
    (a split scope's map, and its area indexes — :func:`_reconcile_scope_indexes`).
    *split* is off for a flat scope (:func:`_entry_targets`).
    """
    live = _live_memory_rels(dest)
    when = _tombstone_date()
    out: List[str] = []
    indexed: Set[str] = set(indexed_elsewhere or ())
    for line in text.splitlines(keepends=True):
        targets = _entry_targets(line, index_rel, split=split)
        if not targets:
            out.append(line)
            continue
        for target in targets:
            if target in live:
                indexed.add(target)
        if all(target in live for target in targets):
            out.append(line)
        else:
            out.append(_tombstone_line(line, live, when, index_rel, split=split))
    pending = sorted(live - indexed) if generate else []
    if pending and out and not out[-1].endswith("\n"):
        # The last line had no terminator; without this the first generated
        # entry is concatenated onto it.
        out[-1] += "\n"
    if pending:
        root = work_repo_root()
    for rel in pending:
        path = Path(rel)
        if path in retired or _has_exact_archive_tombstone(root, text, dest, path, index_rel):
            continue
        entry = _generated_entry(dest, path, index_rel)
        if entry is not None:
            out.append(entry)
    result = "".join(out)
    if result and not result.endswith("\n"):
        result += "\n"
    return result


def _indexed_in(text: str, index_rel: str, *, split: bool) -> Set[str]:
    """Every entry target one index file carries (*split*: :func:`_entry_targets`)."""
    return {
        target
        for line in text.splitlines()
        for target in _entry_targets(line, index_rel, split=split)
    }


def _append_lines(text: str, lines: List[str]) -> str:
    """*text* with *lines* appended, skipping any it already holds."""
    present = {line.strip() for line in text.splitlines()}
    out = text
    for line in lines:
        if line.strip() in present:
            continue
        if out and not out.endswith("\n"):
            out += "\n"
        out += line if line.endswith("\n") else line + "\n"
        present.add(line.strip())
    return out


def _ensure_map_lines(map_text: str, dest: Path) -> str:
    """*map_text* with a generated map line for every area index it lacks."""
    mapped = _mapped_areas(map_text)
    missing = [
        _map_line(rel.stem)
        for rel in sorted(_area_indexes(dest))
        if rel.as_posix() not in mapped
    ]
    return _append_lines(map_text, missing) if missing else map_text


def _file_map_entries(dest: Path) -> None:
    """In a split scope, move entry lines out of the map into their areas.

    An entry line in ``MEMORY.md`` whose files every area index already
    carries is dropped — a session that restored the store before it was
    split still holds every line in its map, and filing those again would
    duplicate the whole index. Any other goes, links rebased, to the area its
    memory names in ``metadata.area`` (else :data:`UNSORTED_AREA`). A flat
    scope is left exactly as it is: the shape is the switch (#435).
    """
    index_path = dest / MEMORY_INDEX
    if not _is_split(dest) or not index_path.is_file():
        return
    areas = {rel: _read_text(path) for rel, path in _area_indexes(dest).items()}
    filed: Set[str] = set()
    for rel, text in areas.items():
        filed |= _indexed_in(text, rel.as_posix(), split=True)
    original = _read_text(index_path)
    kept: List[str] = []
    moved: Dict[Path, List[str]] = {}
    dropped: List[str] = []
    for line in original.splitlines(keepends=True):
        targets = _entry_targets(line, split=True)
        if not targets:
            kept.append(line)
            continue
        if all(target in filed for target in targets):
            dropped.append(line.rstrip("\n"))
        else:
            area = _area_rel(_memory_area(dest / targets[0]))
            moved.setdefault(area, []).append(
                _rebase_links(line, MEMORY_INDEX, area.as_posix())
            )
            filed.update(targets)
    for area, lines in moved.items():
        path = dest / area
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_append_lines(areas.get(area, ""), lines), encoding="utf-8")
    if dropped:
        print(
            f"ℹ️  Dropped {len(dropped)} line(s) from the split {MEMORY_INDEX}: "
            f"their memories are already filed in an area index — edit a hook "
            f"there, not in the map:",
            file=sys.stderr,
        )
        for line in dropped[:5]:
            print(f"     {line}", file=sys.stderr)
        if len(dropped) > 5:
            print(f"     … and {len(dropped) - 5} more", file=sys.stderr)
    result = _ensure_map_lines("".join(kept), dest)
    if result != original:
        index_path.write_text(result, encoding="utf-8")


def _reconcile_scope_indexes(
    dest: Path,
    retired: Set[Path],
    memories: Optional[Set[str]] = None,
    areas: Optional[Set[str]] = None,
) -> None:
    """Reconcile every index file of one scope against its live files.

    A flat scope reconciles ``MEMORY.md`` alone, as it always has. In a split
    scope each index tombstones its own dead entries, a live file carried by
    none of them gets a generated entry in its area index — never in the map
    — and every area index ends up with a map line. A cross-reference to an
    area and a map hook's link are judged against the agent's merged view,
    *memories* and *areas* (:func:`_memory_view`, this scope alone when
    omitted): they are tombstoned only when what they name exists nowhere.
    An area this reconcile generates is in *areas* only when the caller put
    it there (:func:`_planned_areas`).
    """
    index_path = dest / MEMORY_INDEX
    current = _read_text(index_path) if index_path.is_file() else ""
    if not _is_split(dest):
        # A flat scope ignores `areas/`: a line linking one is an entry like
        # any other, and its dead links are tombstoned one by one.
        if current.strip() or any(rel.name != MEMORY_INDEX for rel in _files_under(dest)):
            reconciled = _reconcile_index(current, dest, retired, split=False)
            if reconciled != current:
                index_path.write_text(reconciled, encoding="utf-8")
        return

    texts = _split_index_texts(dest, current)
    out: Dict[Path, str] = {}
    if memories is None or areas is None:
        memories, areas = _memory_view(dest)
    for rel, text in texts.items():
        out[rel] = _reconcile_index(
            text, dest, retired, rel.as_posix(), generate=False, split=True
        )
        if rel != Path(MEMORY_INDEX):
            out[rel] = _tombstone_dead_area_links(out[rel], rel.as_posix(), areas)
    for area, entries in _generated_area_entries(dest, retired, texts).items():
        out[area] = _append_lines(out.get(area, ""), entries)

    for rel, text in out.items():
        if rel != Path(MEMORY_INDEX) and text != texts.get(rel):
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            (dest / rel).write_text(text, encoding="utf-8")
    top = _tombstone_dead_map_lines(
        _ensure_map_lines(out[Path(MEMORY_INDEX)], dest), dest, memories, areas
    )
    if top != current:
        index_path.write_text(top, encoding="utf-8")


def _split_index_texts(dest: Path, current: Optional[str] = None) -> Dict[Path, str]:
    """A split scope's index files' text: the map (*current*, when read) and areas."""
    if current is None:
        index_path = dest / MEMORY_INDEX
        current = _read_text(index_path) if index_path.is_file() else ""
    texts: Dict[Path, str] = {Path(MEMORY_INDEX): current}
    texts.update({rel: _read_text(path) for rel, path in _area_indexes(dest).items()})
    return texts


def _generated_area_entries(
    dest: Path, retired: Set[Path], texts: Dict[Path, str]
) -> Dict[Path, List[str]]:
    """The entries a split scope's reconcile generates, per area index.

    One for each live memory none of *texts* carries, unless its retirement
    state accounts for it (:func:`_reconcile_index`), filed in the area its
    ``metadata.area`` names. Tombstoning changes no live entry, so what
    *texts* carry before the reconcile is what it carries after.
    """
    indexed: Set[str] = set()
    for rel, text in texts.items():
        indexed |= _indexed_in(text, rel.as_posix(), split=True)
    root = work_repo_root()
    generated: Dict[Path, List[str]] = {}
    for name in sorted(_live_memory_rels(dest) - indexed):
        path = Path(name)
        if path in retired or any(
            _has_exact_archive_tombstone(root, text, dest, path, rel.as_posix())
            for rel, text in texts.items()
        ):
            continue
        area = _area_rel(_memory_area(dest / path))
        entry = _generated_entry(dest, path, area.as_posix())
        if entry is not None:
            generated.setdefault(area, []).append(entry)
    return generated


def _planned_areas(dest: Path, retired: Set[Path]) -> Set[str]:
    """The area indexes *dest*'s reconcile will write a generated entry into.

    A cross-reference is judged before the reconcile that creates such an area
    runs — that of the same scope, which tombstones before it generates, or
    of a scope settled later — so the persist counts them live up front (#601).
    """
    if not _is_split(dest):
        return set()
    return {
        rel.as_posix()
        for rel in _generated_area_entries(dest, retired, _split_index_texts(dest))
    }


def _tombstone_dead_area_links(text: str, index_rel: str, areas: Set[str]) -> str:
    """Tombstone an area index's cross-references to areas that exist nowhere.

    A link from one area index to another is commentary, not an entry, but a
    dead one is a dead link like any other: it becomes ``archived <date>`` in
    place, so ``lmer-schema validate`` and persist agree on it (#435). *areas*
    is the agent's merged view, so an area in the other scope is live.

    A ``forget`` tombstone (:func:`_is_forget_tombstone`) is kept verbatim, as
    every index pass keeps it: ``revive`` finds it again by its exact text. On
    any other line an archive link is left alone and the rest is judged.
    """
    base = posixpath.dirname(index_rel)
    when = None

    def replace(match: "re.Match") -> str:
        nonlocal when
        target = match.group(1)
        if not target.lower().endswith(".md") or _NOT_RELATIVE_RE.match(target):
            return match.group(0)
        resolved = posixpath.normpath(posixpath.join(base, target))
        if not _is_area_index(resolved) or resolved in areas:
            return match.group(0)
        when = when or _tombstone_date()
        return f"archived {when}"

    return "".join(
        line if _is_forget_tombstone(line) else _LINK_RE.sub(replace, line)
        for line in text.splitlines(keepends=True)
    )


def _own_area(line: str) -> Optional[tuple]:
    """``(start, area rel)`` of the area index a map line maps, or ``None``.

    That is the line's FIRST link to an area index — the shape every map line
    has (:func:`_map_line`). Any later area link is part of the read-when hook,
    a cross-reference that may name an area in another scope.
    """
    for match in _LINK_RE.finditer(line):
        target = match.group(1)
        if _NOT_RELATIVE_RE.match(target):
            continue
        resolved = posixpath.normpath(target)
        if _is_area_index(resolved):
            return match.start(), resolved
    return None


def _tombstone_dead_map_lines(
    text: str,
    dest: Path,
    memories: Optional[Set[str]] = None,
    areas: Optional[Set[str]] = None,
) -> str:
    """Tombstone what is dead on the map's lines (#435).

    A map line links an area index (:func:`_own_area`); its read-when hook may
    also link memories, other areas or other documents. Each dead link is
    tombstoned the way every index tombstone is — the link becomes
    ``archived <date>`` — and the line stays:

    * the line's own area index, when THIS scope does not have it: the map
      maps this scope's areas;
    * a hook link to an area in neither the agent's merged view, *areas*, nor
      this scope;
    * a hook link to neither a memory in the merged view, *memories*, nor a
      file that exists — ``../notes.md``, or a memory spelled by its path
      into the other scope.

    *memories* and *areas* default to this scope's (:func:`_memory_view`).
    When the line's own area is dead, the hook's live links are unlinked to
    their label text. The line is a tombstone now, and a tombstone links
    nothing: left linked, the next persist would read it as an entry and drop,
    move or route it. A ``forget`` tombstone (:func:`_is_forget_tombstone`)
    is kept verbatim; on any other line an archive link alone is left alone.
    """
    live = _live_memory_rels(dest) if memories is None else memories
    live_areas = (
        {rel.as_posix() for rel in _area_indexes(dest)} if areas is None else areas
    )
    when = None
    out = []
    for line in text.splitlines(keepends=True):
        own = _own_area(line)
        if own is None or _is_forget_tombstone(line):
            out.append(line)
            continue
        own_start, own_rel = own
        area_dead = not (dest / own_rel).is_file()

        def replace(match: "re.Match") -> str:
            nonlocal when
            target = match.group(1)
            if not target.lower().endswith(".md") or _NOT_RELATIVE_RE.match(target):
                return match.group(0)
            resolved = posixpath.normpath(target)
            if any(part in _ARCHIVE_MARKERS for part in resolved.split("/")):
                return match.group(0)
            if match.start() == own_start:
                if not area_dead:
                    return match.group(0)
            elif (
                resolved in live
                or resolved in live_areas
                or resolved == MEMORY_INDEX
                or (dest / resolved).is_file()
            ):
                if not area_dead:
                    return match.group(0)
                label = match.group(0)
                return label[1 : label.index("](")]
            when = when or _tombstone_date()
            return f"archived {when}"

        out.append(_LINK_RE.sub(replace, line))
    return "".join(out)


def _route(source: Path):
    """Route every file in the agent's memory dir to a scope (D17).

    Returns the relative paths per scope and a POSIX-keyed scope map for the
    index split. The index itself is not routed — it is rebuilt per scope from
    the routing. A file whose type does not route lands in
    :data:`DEFAULT_SCOPE`, and says so on stderr when it is a memory file:
    silently filing a `user` fact under one project is exactly the recall gap
    D17 exists to close.
    """
    routed: Dict[str, Set[Path]] = {PROJECT_SCOPE: set(), INSTANCE_SCOPE: set()}
    scope_of: Dict[str, str] = {}
    for rel in sorted(_files_under(source)):
        if _is_index_rel(rel) or _is_conflict_copy(rel):
            continue
        scope = scope_for_memory(_read_text(source / rel))
        if scope is None:
            scope = DEFAULT_SCOPE
            if rel.suffix.lower() == ".md":
                print(
                    f"⚠️  {rel.as_posix()}: no usable `metadata.type` — persisting "
                    f"to the {DEFAULT_SCOPE} scope (see docs/SCHEMA.md, memory)",
                    file=sys.stderr,
                )
        routed[scope].add(rel)
        scope_of[rel.as_posix()] = scope
    return routed, scope_of


def _scope_indexes(
    source: Path,
    scope_of: Dict[str, str],
    manifest: Optional[Dict[str, dict]] = None,
    scopes: Optional[Dict[str, Path]] = None,
) -> Dict[str, Dict[Path, str]]:
    """Each scope's index files' text, split out of the agent's own indexes.

    ``{scope: {index rel: text}}``. Only index files the agent makes a
    statement in are split (:func:`_index_statements`); ``{}`` when it has no
    ``MEMORY.md`` statement at all. Area indexes split first, so a map line
    can follow its area to the scopes that hold it (:func:`_split_index`).
    """
    statements = _index_statements(
        source / MEMORY_INDEX,
        _area_indexes(source),
        _removed_areas(source, manifest or {}, (PROJECT_SCOPE, INSTANCE_SCOPE)),
    )
    if statements is None:
        return {}
    agent_split = _is_split(source)
    flat_scopes = {
        scope for scope, dest in (scopes or {}).items() if not _is_split(dest)
    }
    per_scope: Dict[str, Dict[Path, str]] = {PROJECT_SCOPE: {}, INSTANCE_SCOPE: {}}
    area_scopes: Dict[str, Set[str]] = {}
    for rel in sorted(statements, key=lambda r: r == Path(MEMORY_INDEX)):
        restored = {
            scope: _restored_index_lines(manifest or {}, scope, rel)
            for scope in (PROJECT_SCOPE, INSTANCE_SCOPE)
        }
        split = _split_index(
            statements[rel],
            scope_of,
            restored,
            rel.as_posix(),
            area_scopes,
            split=agent_split,
            flat_scopes=flat_scopes,
        )
        for scope, lines in split.items():
            per_scope[scope][rel] = "".join(lines)
            # Only a scope that is already split takes an area (_write_scope),
            # so only such a scope may take the area's map line.
            dest = scopes.get(scope) if scopes is not None else None
            has_entries = any(
                _entry_targets(line, rel.as_posix(), split=True) for line in lines
            )
            if (
                rel != Path(MEMORY_INDEX)
                and dest is not None
                and ((dest / rel).is_file() or (has_entries and _is_split(dest)))
            ):
                area_scopes.setdefault(rel.as_posix(), set()).add(scope)
    return per_scope


def _shadowed(dest: Path, rels: Set[Path], claimed: Set[str]) -> Set[Path]:
    """Relative paths in *dest* that this persist must not delete.

    The scope's own index (persist rewrites it, never drops it) plus every
    file whose memory ``name`` is in *claimed*: restore shadowed it, so its
    absence from the agent's dir is a shadow, not a deletion. Only the caller
    knows whether a shadow is possible at all — *claimed* is empty for the
    project scope, which restore overlays and therefore never shadows.
    """
    keep = {Path(MEMORY_INDEX)}
    if not claimed or not dest.is_dir():
        return keep
    for rel, path in _files_under(dest).items():
        if rel in rels or rel in keep:
            continue
        if _memory_name(path) in claimed:
            keep.add(rel)
    return keep


def _memory_label() -> str:
    """What the default commit message calls this memory: the launch's
    ``{host}/{project}`` when it has one, else the project dir's name.

    The fallback reads the RESOLVED BASE, never a scope path: the project
    scope is ``<project>/memory`` in a legacy tree but
    ``<project>/_agents/memory`` in a schema-form one, so a name walked back
    up from the memory dir says ``_agents`` in the one case the fallback
    exists for — and would move again the next time a scope path changes
    shape. ``persist_memory`` refuses before reaching this when no base
    resolves; the root's name is still a word about this tree, not a crash.
    """
    repo_host = os.environ.get("LMER_REPO_HOST")
    repo_project = os.environ.get("LMER_REPO_PROJECT")
    if repo_host and repo_project:
        return f"{repo_host}/{repo_project}"
    layout = work_repo_layout()
    return (layout.base or layout.root).name


def _report_manifest_problem() -> None:
    """Name the unusable manifest when that, not the run context, is why no
    memory scope resolved (D20 fail-closed)."""
    problem = work_repo_manifest_problem()
    if problem:
        print(f"   ({problem})", file=sys.stderr)
