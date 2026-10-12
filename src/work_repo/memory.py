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
    work_repo_root,
)

if TYPE_CHECKING:
    from .utils import Manifest

PERSIST_ENV_VAR = "LMER_PERSIST_AGENT_MEMORY"

#: The per-scope line index (``docs/SCHEMA.md``, "memory").
MEMORY_INDEX = "MEMORY.md"

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


def _retirement_archive_rel(key: str, manifest: Manifest) -> str:
    """The archive-mirror path for one validated retirement *key*."""
    parts = PurePosixPath(key).parts
    if parts[:2] == ("_agents", "memory"):
        return PurePosixPath("_archive", *parts).as_posix()
    return PurePosixPath(parts[0], "_archive", *parts[1:]).as_posix()


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


def memory_retirement_paths(scoped_path: str, *, root: Optional[Path] = None) -> List[str]:
    """Validate and name the exact paths before a CLI journals a mutation."""
    root = Path(root) if root is not None else work_repo_root()
    manifest = _schema2_manifest(root)
    if manifest is None:
        raise MemoryRetirementError("explicit memory retirement requires schema 2")
    key = _validate_retirement_key(scoped_path, manifest)
    paths = _retirement_paths(key, manifest)
    return [path.as_posix() for path in paths] + [RETIREMENT_STATE_NAME]


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
    memory_rel = source_rel.relative_to(index_rel.parent)
    archive_target = posixpath.relpath(
        archive_rel.as_posix(), index_rel.parent.as_posix()
    )
    original_lines: List[str] = []
    rewritten_lines: List[str] = []
    for line in index_text.splitlines(keepends=True):
        rewritten = _rewrite_index_target(line, memory_rel.as_posix(), archive_target)
        if rewritten != line:
            original_lines.append(line.rstrip("\r\n"))
        rewritten_lines.append(rewritten)
    if not original_lines:
        raise MemoryRetirementError(f"memory index has no entry for {key}")
    entry: Dict[str, Any] = {
        "archive": archive_rel.as_posix(),
        "index_lines": original_lines,
    }
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
    memory_rel = source_rel.relative_to(index_rel.parent).as_posix()
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


def _filter_retired_index(text: str, retired: Set[Path]) -> str:
    """Drop active index entries for retired paths; keep archive tombstones."""
    names = {rel.as_posix() for rel in retired}
    return "".join(
        line
        for line in text.splitlines(keepends=True)
        if not names.intersection(_entry_targets(line))
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


def _scope_snapshot(files: Dict[Path, Path], index_text: str) -> dict:
    """One scope's manifest entry: the files restored from it, plus its index."""
    return {
        "files": {rel.as_posix(): _digest(path) for rel, path in files.items()},
        "index": _index_lines(index_text),
    }


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


def _entry_targets(line: str) -> List[str]:
    """The entry link targets on one index line (``docs/SCHEMA.md``).

    An entry is a line carrying a RELATIVE markdown link to a ``.md`` file. A
    heading, prose, a tombstoned line (the link replaced by
    ``archived``/``forgotten`` and a date) and a link re-pointed at the
    ``_archive/`` mirror carry no entry: they belong to no single scope and
    are kept in both indexes.
    """
    targets = []
    for target in _LINK_RE.findall(line):
        if not target.lower().endswith(".md"):
            continue
        if _NOT_RELATIVE_RE.match(target):
            continue
        parts = posixpath.normpath(target).split("/")
        if any(part in _ARCHIVE_MARKERS for part in parts):
            continue
        targets.append(posixpath.normpath(target))
    return targets


def _is_tombstone(line: str) -> bool:
    """Whether *line* is an entry ``forget`` re-pointed at the archive mirror."""
    for target in _LINK_RE.findall(line):
        if not target.lower().endswith(".md") or _NOT_RELATIVE_RE.match(target):
            continue
        if any(part in _ARCHIVE_MARKERS for part in posixpath.normpath(target).split("/")):
            return True
    return False


def _split_index(
    text: str,
    scope_of: Dict[str, str],
    restored: Optional[Dict[str, Set[str]]] = None,
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
    """
    per_scope: Dict[str, List[str]] = {PROJECT_SCOPE: [], INSTANCE_SCOPE: []}
    for line in text.splitlines(keepends=True):
        targets = _entry_targets(line)
        if not targets:
            owners = [
                scope for scope in per_scope
                if line.strip() in (restored or {}).get(scope, ())
            ] if _is_tombstone(line) else []
            for scope in (owners if len(owners) == 1 else per_scope):
                per_scope[scope].append(line)
            continue
        per_scope[scope_of.get(targets[0], DEFAULT_SCOPE)].append(line)
    return per_scope


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


def _merged_index_text(current: str, local_text: str, restored: Set[str]) -> str:
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
    the session's own bytes go through verbatim, which is what a legacy tree's
    first persist has always written.
    """
    if not current.strip():
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
    return directory.is_dir() and any(p.is_file() for p in directory.rglob("*"))


def _files_under(directory: Path) -> Dict[Path, Path]:
    """Every file under *directory*, keyed by its relative path."""
    if not directory.is_dir():
        return {}
    return {p.relative_to(directory): p for p in directory.rglob("*") if p.is_file()}


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
    """
    doomed: Set[Path] = set()
    for rel_posix, digest in restored.items():
        rel = Path(rel_posix)
        if rel in held or rel in keep:
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
        write_restore_manifest(
            {
                PROJECT_SCOPE: _scope_snapshot(
                    restored, _read_text(index) if index is not None else ""
                )
            }
        )
        print(f"🧠 Restored {copied} agent memory file(s) from work repo into {dest}")
        return 0

    union, indexes, counts, taken = _union_of_scopes(scopes, retired)
    if not union and not indexes:
        print("🧠 No saved agent memory found in work repo — starting fresh")
        return 0

    try:
        copied = _mirror_files(union, dest, set())
        if indexes:
            (dest / MEMORY_INDEX).write_text(_merge_index(indexes), encoding="utf-8")
            copied += 1
    except OSError as e:
        print(f"⚠️  Failed to restore agent memory: {e}", file=sys.stderr)
        return 1

    write_restore_manifest(
        {
            scope: _scope_snapshot(
                files,
                _filter_retired_index(
                    _read_text(scopes[scope] / MEMORY_INDEX), retired.get(scope, set())
                ),
            )
            for scope, files in taken.items()
            if scope in scopes
        }
    )

    print(f"🧠 Restored {copied} agent memory file(s) from work repo into {dest}")
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
    indexes = []
    for scope, files in (
        (INSTANCE_SCOPE, instance_files),
        (PROJECT_SCOPE, project_files),
    ):
        index = files.pop(Path(MEMORY_INDEX), None)
        if index is not None:
            indexes.append(
                _filter_retired_index(
                    _read_text(index), retired.get(scope, set())
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
    path = restore_manifest_path()
    try:
        if manifest_before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(manifest_before, encoding="utf-8")
    except OSError as e:
        print(f"⚠️  Could not roll back the memory restore manifest {path}: {e}", file=sys.stderr)
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
                    return commit_work_path([], commit_message)

            rc = check_commit_ready(root)
            if rc != 0:
                return rc

            # Integrate the published branch BEFORE merging into it, so the
            # current side of the three-way merge is what upstream holds
            # (#369 gap (b)).
            _pull_before_merge(root)
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
            except OSError as e:
                print(
                    f"❌ Failed to copy agent memory into work repo: {e}",
                    file=sys.stderr,
                )
                return 1
    except MemoryRetirementError as e:
        print(f"❌ Invalid memory retirement state: {e}", file=sys.stderr)
        return 1

    _report_deletions(deletions, declined, _schema2_manifest(root) is not None)

    if not rel_paths:
        print("🧠 No agent memory to persist")
        return commit_work_path([], commit_message)

    print(f"🧠 Merged {copied} agent memory file(s) into work repo")

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
        copied, deleted, declined = _write_scope(
            PROJECT_SCOPE,
            files,
            # Absence and an empty statement are different answers here
            # (:func:`_index_statement`), and this is the branch a LEGACY
            # work repo takes — the form the repos in use are in today, so
            # a blank `MEMORY.md` wiping the scope's index is live, not
            # theoretical.
            _index_statement(index),
            dest,
            set(),
            manifest,
            retired.get(PROJECT_SCOPE, set()),
            unbaselined=unbaselined,
            allow_unbaselined_delete=allow_unbaselined_delete,
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
    indexes = _scope_indexes(source, scope_of, manifest)
    for scope, rels in retired.items():
        if scope in indexes:
            indexes[scope] = _filter_retired_index(indexes[scope], rels)
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
            indexes.get(scope, "") if has_index else None,
            dest,
            claimed,
            manifest,
            retired_here,
            unbaselined=unbaselined,
            allow_unbaselined_delete=allow_unbaselined_delete,
        )
        copied += scope_copied
        deletions.extend(scope_deleted)
        declined.extend(scope_declined)
        rel = _rel_to_root(dest, root)
        if rel is not None:
            rel_paths.append(rel)
    write_restore_manifest(manifest)
    return copied, rel_paths, deletions, declined


def _write_scope(
    scope: str,
    files: Dict[Path, Path],
    index_text: Optional[str],
    dest: Path,
    claimed: Set[str],
    manifest: Dict[str, dict],
    retired: Optional[Set[Path]] = None,
    *,
    unbaselined: bool = False,
    allow_unbaselined_delete: bool = False,
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
            if rel in held or rel in keep:
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
    if index_text is not None:
        merged = _merged_index_text(
            _read_text(index_path) if index_path.is_file() else "",
            index_text,
            _manifest_index(manifest, scope),
        )
        index_path.write_text(merged, encoding="utf-8")
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
        entry["index"] = _index_lines(index_text)
    else:
        entry.setdefault("index", [])

    # Reconcile the scope's OWN index with the scope's live files and its
    # retirement state (#369 gap (e)). Runs for both tree forms and even when
    # the agent has no index: this persist changed the scope's live files, and
    # a dangling entry is a validator ERROR in schema form. The session's
    # recorded `index` above stays what the session HOLDS, so the next persist
    # still tells its own removals from another writer's edits.
    current = _read_text(index_path) if index_path.is_file() else ""
    if current.strip() or any(rel.name != MEMORY_INDEX for rel in _files_under(dest)):
        reconciled = _reconcile_index(current, dest, retired or set())
        if reconciled != current:
            index_path.write_text(reconciled, encoding="utf-8")
    return copied, deleted, declined


def _tombstone_date() -> str:
    """The date a reconciled tombstone carries (``YYYY-MM-DD``, UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _tombstone_line(line: str, live: Set[str], when: str) -> str:
    """Replace the link of every DEAD entry on *line* with ``archived <date>``.

    The ``docs/SCHEMA.md`` 'Index lines' tombstone: the line stays and its
    hand-written text survives; only the link to a file that is no longer
    there goes. A link that is not a relative ``.md`` entry (a URL, an anchor,
    a link already pointing at ``_archive/``) is left alone, and so is an
    entry whose file IS live — a line may legitimately carry several.
    """
    def replace(match: "re.Match") -> str:
        target = match.group(1)
        if not target.lower().endswith(".md") or _NOT_RELATIVE_RE.match(target):
            return match.group(0)
        normalized = posixpath.normpath(target)
        if any(part in _ARCHIVE_MARKERS for part in normalized.split("/")):
            return match.group(0)
        if normalized in live:
            return match.group(0)
        return f"archived {when}"

    return _LINK_RE.sub(replace, line)


def _generated_entry(dest: Path, rel: Path) -> str:
    """A generated index line for a live memory with no entry.

    Form: ``- [<frontmatter name or stem>](<relative path>) — <description>``.
    The title prefers the memory's own ``name`` (what the agent and the
    validator read), falling back to the filename stem; the description is the
    frontmatter's, omitted when absent so the line is still a valid entry.
    """
    data = _frontmatter(_read_text(dest / rel))
    name = data.get("name") if data is not None else None
    title = name if isinstance(name, str) and name.strip() else rel.stem
    line = f"- [{title}]({rel.as_posix()})"
    description = data.get("description") if data is not None else None
    if isinstance(description, str) and description.strip():
        line += f" — {' '.join(description.split())}"
    return line + "\n"


def _tombstone_covers(text: str, dest: Path, rel: Path) -> bool:
    """Whether *text* carries a tombstone that accounts for *rel*.

    A retired memory's file may still be live in the scope — a stale session
    re-created it, or ``forget`` moved it while a copy stayed behind — and its
    entry line was deliberately removed. Re-indexing it would resurrect a line
    somebody retired, so the reconcile must see the tombstone as covering it.

    Two tombstone shapes (docs/SCHEMA.md, 'Index lines'): a link re-pointed at
    the ``_archive/`` mirror, whose target names the file; and the word form
    (``archived``/``forgotten`` plus a date), which keeps the title. The file's
    frontmatter ``name`` or its stem is what either carries.
    """
    data = _frontmatter(_read_text(dest / rel))
    name = data.get("name") if data is not None else None
    # Match the full filename, not the stem: a short stem (``a.md``) would
    # match almost any line carrying that letter and silently suppress a real
    # entry. Word boundaries keep ``shared.md`` from matching ``sharedness``.
    candidates = [rel.name.lower()]
    if isinstance(name, str) and name.strip():
        candidates.append(name.strip().lower())
    for line in text.splitlines():
        if _entry_targets(line):
            continue
        lowered = line.lower()
        if not _is_tombstone(line) and not re.search(
            r"\b(?:archived|forgotten)\b", lowered
        ):
            continue
        if any(
            re.search(rf"(?<![a-z0-9]){re.escape(c)}(?![a-z0-9])", lowered)
            for c in candidates
        ):
            return True
    return False


def _reconcile_index(text: str, dest: Path, retired: Set[Path]) -> str:
    """Make the scope index's entry lines agree with the scope's live files.

    Entry lines for files that are gone are tombstoned IN PLACE (never
    dropped); live memory files with no entry get a generated line appended,
    unless the scope's retirement state already accounts for them — *retired*
    (the keys ``work memory forget`` recorded) or a tombstone the index
    already carries (:func:`_tombstone_covers`) — so a retired line stays
    retired. Every non-entry line — headings, hand-written commentary, blank
    lines and the tombstones ``work memory forget`` wrote — is kept
    byte-identical and in position. This runs after the line merge, on the
    scope's own index, so it reads the scope's live files plus its retirement
    state and never one session's view.
    """
    live = {
        rel.as_posix()
        for rel in _files_under(dest)
        if rel.suffix.lower() == ".md"
        and rel.name != MEMORY_INDEX
        and not _is_conflict_copy(rel)
    }
    when = _tombstone_date()
    out: List[str] = []
    indexed: Set[str] = set()
    for line in text.splitlines(keepends=True):
        targets = _entry_targets(line)
        if not targets:
            out.append(line)
            continue
        for target in targets:
            if target in live:
                indexed.add(target)
        if all(target in live for target in targets):
            out.append(line)
        else:
            out.append(_tombstone_line(line, live, when))
    for rel in sorted(live - indexed):
        if Path(rel) in retired:
            continue
        if _tombstone_covers(text, dest, Path(rel)):
            continue
        out.append(_generated_entry(dest, Path(rel)))
    result = "".join(out)
    if result and not result.endswith("\n"):
        result += "\n"
    return result


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
        if rel == Path(MEMORY_INDEX) or _is_conflict_copy(rel):
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
    source: Path, scope_of: Dict[str, str], manifest: Optional[Dict[str, dict]] = None
) -> Dict[str, str]:
    """Each scope's ``MEMORY.md`` text, split out of the agent's own index."""
    index = source / MEMORY_INDEX
    if not index.is_file():
        return {}
    restored = {
        scope: _manifest_index(manifest or {}, scope)
        for scope in (PROJECT_SCOPE, INSTANCE_SCOPE)
    }
    return {
        scope: "".join(lines)
        for scope, lines in _split_index(_read_text(index), scope_of, restored).items()
    }


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
