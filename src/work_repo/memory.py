"""Agent memory persistence to the work repository.

Claude Code stores per-session agent memory under
``~/.claude/projects/-workspace/memory/`` (the ``-workspace`` segment encodes
the ``/workspace`` cwd used inside the container). That directory lives only for
the container's lifetime, so memory written during one session is lost when the
container exits.

When ``LMER_PERSIST_AGENT_MEMORY`` is enabled we mirror that directory to the
work repo under ``{host}/{project}/memory/`` so it survives across sessions on a
per-project basis (shared across every task type and target for the project).

Two halves, by design:

* **Restore** (work repo -> Claude memory dir) runs automatically at session
  start from ``libexec/claude-runner.sh`` via ``work memory restore`` so the
  saved memory is on disk before Claude reads it.
* **Persist** (Claude memory dir -> work repo, then committed and pushed) is the
  agent's responsibility via ``work memory persist`` (documented in AGENTS.md).
  We intentionally do not auto-persist on session end — the agent decides what
  is worth keeping and runs the command before finishing.

Both copy directions use *mirror* (not merge) semantics: a file removed on one
side is removed on the other, so memory the agent deletes stays deleted instead
of being resurrected from the work repo on the next restore.

Both halves are no-ops unless ``LMER_PERSIST_AGENT_MEMORY`` is truthy.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from lmer_cli.util import get_bool_env

from .git_ops import commit_work_path
from .utils import project_memory_dir

PERSIST_ENV_VAR = "LMER_PERSIST_AGENT_MEMORY"


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


def _has_files(directory: Path) -> bool:
    """Return True if ``directory`` exists and contains at least one file."""
    return directory.is_dir() and any(p.is_file() for p in directory.rglob("*"))


def _mirror_tree(src: Path, dst: Path) -> int:
    """
    Make ``dst`` an exact mirror of ``src`` (files only), preserving the
    relative directory structure.

    Files present in ``dst`` but not in ``src`` are deleted, so memory
    *deletions* propagate in whichever direction this is called — without this,
    a memory the agent deleted would linger in the work repo and be resurrected
    on the next session's automatic restore. Existing files are overwritten;
    missing parent directories are created.

    Returns the number of files copied from ``src``.
    """
    dst.mkdir(parents=True, exist_ok=True)
    src_rel = {p.relative_to(src) for p in src.rglob("*") if p.is_file()}

    # Remove destination files that no longer exist in the source.
    for item in dst.rglob("*"):
        if item.is_file() and item.relative_to(dst) not in src_rel:
            item.unlink()

    # Copy/overwrite every source file into the destination.
    count = 0
    for rel in src_rel:
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, target)
        count += 1
    return count


def restore_memory() -> int:
    """
    Restore saved agent memory from the work repo into Claude's memory dir.

    No-op (returns 0) unless ``LMER_PERSIST_AGENT_MEMORY`` is enabled. Safe to
    call when nothing has been saved yet — it simply reports a fresh start.

    Returns:
        Exit code (0 on success or graceful no-op, 1 on copy failure)
    """
    if not get_bool_env(PERSIST_ENV_VAR):
        return 0

    source = project_memory_dir()
    if source is None:
        print(
            "⚠️  Cannot restore agent memory: LMER_REPO_HOST/LMER_REPO_PROJECT not set",
            file=sys.stderr,
        )
        return 0

    if not _has_files(source):
        print("🧠 No saved agent memory found in work repo — starting fresh")
        return 0

    dest = agent_memory_dir()
    try:
        copied = _mirror_tree(source, dest)
    except OSError as e:
        print(f"⚠️  Failed to restore agent memory: {e}", file=sys.stderr)
        return 1

    print(f"🧠 Restored {copied} agent memory file(s) from work repo into {dest}")
    return 0


def persist_memory(commit_message: str | None = None) -> int:
    """
    Persist Claude's current agent memory to the work repo and push it.

    No-op (returns 0) unless ``LMER_PERSIST_AGENT_MEMORY`` is enabled. Copies the
    memory directory into ``{host}/{project}/memory`` in the work repo, then
    commits and pushes that path via :func:`commit_work_path`.

    Returns:
        Exit code (0 on success or graceful no-op, non-zero on failure)
    """
    if not get_bool_env(PERSIST_ENV_VAR):
        print(f"🧠 {PERSIST_ENV_VAR} is not enabled — skipping agent memory persist")
        return 0

    repo_host = os.environ.get("LMER_REPO_HOST")
    repo_project = os.environ.get("LMER_REPO_PROJECT")
    dest = project_memory_dir()
    if dest is None or not repo_host or not repo_project:
        print(
            "❌ Cannot persist agent memory: LMER_REPO_HOST/LMER_REPO_PROJECT not set",
            file=sys.stderr,
        )
        return 1

    source = agent_memory_dir()
    if not _has_files(source):
        print("🧠 No agent memory to persist")
        return 0

    try:
        copied = _mirror_tree(source, dest)
    except OSError as e:
        print(f"❌ Failed to copy agent memory into work repo: {e}", file=sys.stderr)
        return 1

    print(f"🧠 Mirrored {copied} agent memory file(s) into work repo")

    if commit_message is None:
        commit_message = f"Update agent memory: {repo_host}/{repo_project}"
    rel_path = f"{repo_host}/{repo_project}/memory"
    return commit_work_path(rel_path, commit_message)
