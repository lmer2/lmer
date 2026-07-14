"""In-container taskdef tier resolution.

Mirror of the taskdef search in ``hooks/start.py`` (``work_repo_taskdef_dirs``,
``builtin_taskdef_root``, ``taskdef_search_dirs``, ``find_taskdef_file``). The
hook deliberately does not import the lmer_cli package (it runs under the
global venv, self-contained), and session provisioning needs the same tier
precedence *before* Claude starts — so the search is mirrored here rather than
shared. Keep the function bodies in sync with hooks/start.py — a source-level
guard test in ``tests/test_masterplan_provisioning.py`` asserts they match
(same pattern as ``_is_github_host`` in ``lmer_cli.tokens``).
"""
from __future__ import annotations

import os
from pathlib import Path


def work_repo_taskdef_dirs():
    """Return work-repo taskdef directories in precedence order.

    Looks up:
      1. {LMER_WORK_REPO_PATH}/{LMER_REPO_HOST}/{LMER_REPO_PROJECT}/taskdef/
         (project-scoped — applies only to the current project)
      2. {LMER_WORK_REPO_PATH}/taskdef/
         (work-global — applies to all projects)

    Only directories that exist on disk are returned. Returns an empty list
    when LMER_WORK_REPO_PATH is unset or does not exist.
    """
    dirs = []
    work_repo_path = os.environ.get("LMER_WORK_REPO_PATH")
    if not work_repo_path:
        return dirs
    work_root = Path(work_repo_path)
    if not work_root.is_dir():
        return dirs

    repo_host = os.environ.get("LMER_REPO_HOST")
    repo_project = os.environ.get("LMER_REPO_PROJECT")
    if repo_host and repo_project:
        project_dir = work_root / repo_host / repo_project / "taskdef"
        if project_dir.is_dir():
            dirs.append(project_dir)

    global_dir = work_root / "taskdef"
    if global_dir.is_dir():
        dirs.append(global_dir)

    return dirs


def builtin_taskdef_root():
    """Locate the built-in taskdef directory — shared fragments
    (service-mode.jinja2, run-state.jinja2, changelog.jinja2, …) live here.

    Resolves the global install location mounted inside the container
    (``/home/developer/.lmer`` or ``/Agents/global``) rather than trusting
    ``LMER_TASKDEF_ROOT``, which the CLI sets to a *host* path that does not
    exist inside the container. Both task lookup and include-resolution must
    be able to reach it regardless of which external taskdef repo
    (LMER_TASKDEF_PATHS) is active."""
    lmer_global = Path("/home/developer/.lmer")
    agents_global = Path("/Agents/global")

    if lmer_global.exists():
        base_path = lmer_global
    elif agents_global.exists():
        base_path = agents_global
    else:
        base_path = Path.cwd()
    return base_path / "taskdef"


def taskdef_search_dirs():
    """Return ordered list of directories to search for task definitions.

    Precedence (first match wins):
      1. Work-repo project taskdefs: {work_repo}/{host}/{project}/taskdef/
      2. Work-repo global taskdefs: {work_repo}/taskdef/
      3. LMER_TASKDEF_PATHS entries (colon-separated)
      4. Built-in taskdef directory (under /home/developer/.lmer or
         /Agents/global, depending on what is mounted)
    """
    search_dirs = list(work_repo_taskdef_dirs())

    extra_paths = os.environ.get("LMER_TASKDEF_PATHS", "")
    if extra_paths:
        for p in extra_paths.split(":"):
            p = p.strip()
            if p:
                search_dirs.append(Path(p))

    search_dirs.append(builtin_taskdef_root())
    return search_dirs


def find_taskdef_file(filename, taskdef_name=None):
    """Locate a file inside the active task definition directory.

    Resolution order:
      1. taskdef_search_dirs() — work-repo (project, then global), then
         LMER_TASKDEF_PATHS, then the built-in taskdef directory. This is
         the canonical precedence; work-repo overrides naturally win here.
      2. LMER_TASKDEF_DIR env var — fallback fast path the CLI sets when it
         pre-resolves the taskdef directory. In practice this points into
         one of the search_dirs entries already, but is retained for cases
         where the CLI sets it to a path not covered by the search list.
      3. LMER_TASK_INSTRUCTIONS env var parent — older fallback equivalent
         to LMER_TASKDEF_DIR (the CLI sets both to the same directory).

    Returns a Path if found, else None. This function is silent; callers are
    responsible for any user-facing error reporting.
    """
    if not taskdef_name:
        taskdef_name = os.environ.get('LMER_TASK') or os.environ.get('LMER_TASKDEF')

    if taskdef_name:
        for search_dir in taskdef_search_dirs():
            candidate = search_dir / taskdef_name / filename
            if candidate.exists():
                return candidate

    taskdef_dir_env = os.environ.get('LMER_TASKDEF_DIR')
    if taskdef_dir_env:
        candidate = Path(taskdef_dir_env) / filename
        if candidate.exists():
            return candidate

    pre_resolved = os.environ.get('LMER_TASK_INSTRUCTIONS')
    if pre_resolved:
        candidate = Path(pre_resolved).parent / filename
        if candidate.exists():
            return candidate

    return None
