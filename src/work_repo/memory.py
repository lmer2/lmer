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

Both rules still run under the manifest, but what rule 1 is FOR has changed,
and the cost is worth naming. Its original job was to stop a mirror of an empty
set from wiping a shared scope; ``_restored_then_removed`` now bounds deletion
to this session's own restored-and-removed set, so writing an empty routed set
could no longer wipe anything. What rule 1 still does is UNDER-DELETE: a
session that forgets the last memory it held for a scope, or empties its memory
directory entirely, skips that scope — the file survives, its index line
survives, and the next restore hands the memory straight back, so that
deletion can never be expressed. That is the shape an explicit
``work memory forget`` verb has to solve, and it is deferred to one; rule 1 is
kept meanwhile because a scope this session says nothing about is still safer
left alone than written over. Rule 2 keeps a file whose name moved scopes from
being read as a deletion in the scope it left.

Both halves are no-ops unless ``LMER_PERSIST_AGENT_MEMORY`` is truthy.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Set

import yaml

from lmer_cli.util import get_bool_env

from .git_ops import check_commit_ready, commit_work_path
from .utils import (
    instance_memory_dir,
    project_memory_dir,
    work_repo_layout,
    work_repo_manifest_problem,
    work_repo_root,
)

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
#: manifest at all, which deletes nothing.
RESTORE_MANIFEST_VERSION = 1


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


def read_restore_manifest() -> Dict[str, dict]:
    """This session's restore manifest, as ``{scope: {files, index}}``.

    ``{}`` for every shape that is not a manifest this code wrote for THIS
    memory dir — absent, unreadable, not JSON, a version or a memory dir that
    does not match. That is the safe direction: an empty manifest says this
    session restored nothing, so persist deletes nothing.
    """
    path = restore_manifest_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, ValueError) as e:
        print(
            f"⚠️  Ignoring unreadable memory restore manifest {path}: {e} "
            f"(this persist will not delete anything)",
            file=sys.stderr,
        )
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("version") != RESTORE_MANIFEST_VERSION:
        return {}
    if data.get("memory_dir") != str(agent_memory_dir()):
        return {}
    scopes = data.get("scopes")
    if not isinstance(scopes, dict):
        return {}
    return {
        scope: entry
        for scope, entry in scopes.items()
        if isinstance(entry, dict)
    }


def write_restore_manifest(scopes: Dict[str, dict]) -> None:
    """Record *scopes* as what this session now holds. Never raises.

    A manifest that cannot be written leaves the session with no record, and
    a session with no record deletes nothing — a warning, not a failure.
    """
    path = restore_manifest_path()
    payload = {
        "version": RESTORE_MANIFEST_VERSION,
        "memory_dir": str(agent_memory_dir()),
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


def _split_index(text: str, scope_of: Dict[str, str]) -> Dict[str, List[str]]:
    """Split one index into a per-scope index, by where each entry's file went.

    An entry line follows its file; every other line is commentary about the
    index rather than about one memory, so it is kept in both scopes — which
    is what lets :func:`_merge_index` drop the duplicate on the way back.
    """
    per_scope: Dict[str, List[str]] = {PROJECT_SCOPE: [], INSTANCE_SCOPE: []}
    for line in text.splitlines(keepends=True):
        targets = _entry_targets(line)
        if not targets:
            for lines in per_scope.values():
                lines.append(line)
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
    count = 0
    for rel, source in files.items():
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

    if len(scopes) == 1:
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

    union, indexes, counts, taken = _union_of_scopes(scopes)
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
            scope: _scope_snapshot(files, _read_text(scopes[scope] / MEMORY_INDEX))
            for scope, files in taken.items()
        }
    )

    print(f"🧠 Restored {copied} agent memory file(s) from work repo into {dest}")
    print(
        f"   scopes: instance {counts[INSTANCE_SCOPE]}, "
        f"project {counts[PROJECT_SCOPE]}"
    )
    return 0


def _union_of_scopes(scopes: Dict[str, Path]):
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
    instance_files = _files_under(scopes[INSTANCE_SCOPE])
    project_files = _files_under(scopes[PROJECT_SCOPE])
    indexes = []
    for files in (instance_files, project_files):
        index = files.pop(Path(MEMORY_INDEX), None)
        if index is not None:
            indexes.append(_read_text(index))

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


def persist_memory(commit_message: str | None = None) -> int:
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

    No-op (returns 0) unless ``LMER_PERSIST_AGENT_MEMORY`` is enabled.

    Returns:
        Exit code (0 on success or graceful no-op, non-zero on failure)
    """
    if not get_bool_env(PERSIST_ENV_VAR):
        print(f"🧠 {PERSIST_ENV_VAR} is not enabled — skipping agent memory persist")
        return 0

    scopes = memory_scope_dirs()
    if PROJECT_SCOPE not in scopes:
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

    source = agent_memory_dir()
    if not _has_files(source):
        # The #369 manifest distinguishes retiring restored memories from a
        # session handed none. A completed retirement may still need pushing.
        restored = read_restore_manifest()
        if not any(_manifest_files(restored, scope) for scope in scopes):
            print("🧠 No agent memory to persist")
            return commit_work_path([], commit_message)

    rc = check_commit_ready(work_repo_root())
    if rc != 0:
        return rc

    try:
        copied, rel_paths = _persist_scopes(source, scopes)
    except OSError as e:
        print(f"❌ Failed to copy agent memory into work repo: {e}", file=sys.stderr)
        return 1

    if not rel_paths:
        print("🧠 No agent memory to persist")
        return commit_work_path([], commit_message)

    print(f"🧠 Merged {copied} agent memory file(s) into work repo")

    if commit_message is None:
        commit_message = f"Update agent memory: {_memory_label()}"
    # One scope is staged as the bare path it always was; the list shape
    # appears only when there is genuinely more than one scope to stage.
    target = rel_paths[0] if len(rel_paths) == 1 else rel_paths
    return commit_work_path(target, commit_message)


def _persist_scopes(source: Path, scopes: Dict[str, Path]):
    """Write the agent's memory into the scopes it routes to.

    Returns the number of files copied and the work-repo-relative path of
    every scope written — the paths staged for commit, which are a subset of
    the journalled scope (:func:`memory_write_rel_paths`).
    """
    manifest = read_restore_manifest()

    if len(scopes) == 1:
        # One scope: the whole directory. Nothing is routed because there is
        # nowhere else for it to go — but the index is still merged rather
        # than written over, because one scope is still shared by every
        # session of this project (issue #369).
        dest = scopes[PROJECT_SCOPE]
        files = _files_under(source)
        index = files.pop(Path(MEMORY_INDEX), None)
        copied = _write_scope(
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
        )
        write_restore_manifest(manifest)
        rel = _rel_to_root(dest, work_repo_root())
        return copied, ([rel] if rel is not None else [])

    routed, scope_of = _route(source)
    # The memory names this persist routes to the PROJECT scope: the ones
    # restore shadows the instance copy of, and so the only names the keep
    # rule below runs on (the instance scope shadows nothing).
    project_names = {
        name
        for name in (_memory_name(source / rel) for rel in routed[PROJECT_SCOPE])
        if name is not None
    }
    indexes = _scope_indexes(source, scope_of)
    # Whether the agent has an index at all — the difference between
    # "no lines for this scope" and "nothing to say about indexes"
    # (:func:`_index_statement`, which the one-scope branch above asks the
    # same question of). Per SCOPE the split may legitimately be empty; that
    # is a statement. It is the FILE being absent or blank that is not.
    has_index = _index_statement(source / MEMORY_INDEX) is not None

    copied = 0
    rel_paths = []
    root = work_repo_root()
    for scope, dest in scopes.items():
        rels = routed[scope]
        if not rels and not _manifest_files(manifest, scope):
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
        copied += _write_scope(
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
        )
        rel = _rel_to_root(dest, root)
        if rel is not None:
            rel_paths.append(rel)
    write_restore_manifest(manifest)
    return copied, rel_paths


def _write_scope(
    scope: str,
    files: Dict[Path, Path],
    index_text: Optional[str],
    dest: Path,
    claimed: Set[str],
    manifest: Dict[str, dict],
) -> int:
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
    """
    dest.mkdir(parents=True, exist_ok=True)
    held = set(files)
    keep = _shadowed(dest, held, claimed)
    restored = _manifest_files(manifest, scope)
    for rel in _restored_then_removed(dest, restored, held, keep):
        (dest / rel).unlink()

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
        if (dest / rel).is_file() and source.is_file()
    }
    if index_text is not None:
        index = dest / MEMORY_INDEX
        merged = _merged_index_text(
            _read_text(index) if index.is_file() else "",
            index_text,
            _manifest_index(manifest, scope),
        )
        index.write_text(merged, encoding="utf-8")
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
    return copied


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
        if rel == Path(MEMORY_INDEX):
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


def _scope_indexes(source: Path, scope_of: Dict[str, str]) -> Dict[str, str]:
    """Each scope's ``MEMORY.md`` text, split out of the agent's own index."""
    index = source / MEMORY_INDEX
    if not index.is_file():
        return {}
    return {
        scope: "".join(lines)
        for scope, lines in _split_index(_read_text(index), scope_of).items()
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
