"""In-container taskdef tier resolution.

Mirror of the taskdef search in ``hooks/start.py`` (``_userinfo_boundary``,
``_normalize_repo_url``, ``_plexus_project_dir``, ``plexus_manifest_problem``,
``work_repo_taskdef_dirs``, ``builtin_taskdef_root``, ``taskdef_search_dirs``,
``find_taskdef_file``). The
hook deliberately does not import the lmer_cli package (it runs under the
global venv, self-contained), and session provisioning needs the same tier
precedence *before* Claude starts — so the search is mirrored here rather than
shared. Keep the function bodies in sync with hooks/start.py — a source-level
guard test in ``tests/test_masterplan_provisioning.py`` (and
``tests/test_taskdefs_second_name.py`` for the registry-lookup helpers)
asserts they match (same pattern as ``_is_github_host`` in ``lmer_cli.tokens``).
The URL helpers are in turn inline mirrors of ``lmer_cli.container.sources``
(spec D21: ONE normalization definition) because the hook cannot import it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml


def _userinfo_boundary(text, scp_form=False):
    """Index of the ``@`` separating userinfo from host, or ``-1`` if none.

    Mirror of ``lmer_cli.container.sources._userinfo_boundary`` (the
    authority rule plus its malformed-``user:se/cret@host`` extension), kept
    body-identical so the inline normalizer below parses exactly what the
    canonical one does — see that function's docstring for the reasoning.
    """
    slash = text.find("/")
    at = text.rfind("@", 0, slash if slash != -1 else len(text))
    if at == -1 and "@" in text:
        candidate = text.rpartition("@")[0]
        _, colon, secret = candidate.partition("/")[0].partition(":")
        if colon and not secret.isdigit():
            if not scp_form or ":" in text[len(candidate) + 1:]:
                at = len(candidate)
    return at


def _normalize_repo_url(url):
    """Comparison key of a git repo URL: ``host/path``.

    Inline mirror of ``lmer_cli.container.sources.normalize_repo_url`` —
    the ONE definition of remote normalization (spec D21) — which the hook
    cannot import. Same rules: lowercase the host, drop scheme and
    userinfo, drop the default ports 443/22 but keep a non-default port as
    ``host:port``, fold scp-form ``git@host:path`` (user optional, git's
    ``[user@]host:path``) and ``ssh://git@host/path`` into ``host/path``,
    strip a trailing ``.git`` and trailing ``/``. Path case is preserved
    here; the registry lookup compares case-insensitively. Scheme-less
    ``host:2222/path`` (bare digits after the colon) is a ported host, not
    an scp path, so the canonical form re-normalizes to itself; the
    ambiguous ``a:b/c@d`` shape stays opaque — both as the canonical parser
    decides (see ``_parse_repo_url`` there for the reasoning).
    ``tests/test_taskdefs_second_name.py`` pins this against the canonical
    function behaviorally.
    """
    url = (url or "").strip()
    scheme = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url)
    if scheme:
        rest = url[scheme.end():]
        at = _userinfo_boundary(rest)
        if at != -1:
            rest = rest[at + 1:]
        netloc, _, path = rest.partition("/")
        host, _, port = netloc.partition(":")
    else:
        at = _userinfo_boundary(url, scp_form=True)
        if at != -1:
            host, _, path = url[at + 1:].partition(":")
        else:
            host, colon, path = url.partition(":")
            if not (
                colon
                and host
                and "/" not in host
                and not path.partition("/")[0].isdigit()
                and _userinfo_boundary(url) == -1
            ):
                host, path = "", url
        port = ""
    host = host.lower()
    if port and port not in ("443", "22"):
        host = f"{host}:{port}"
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.rstrip("/")
    if host and path:
        return f"{host}/{path}"
    return host or path


def _plexus_project_dir(work_root):
    """Schema-form project dir for the launch's repo: ``(dir, problem)``.

    Form detection is manifest presence (spec D20): with no
    ``{work_root}/_lmer.yaml`` the tree is legacy and ``(None, None)`` is
    returned without reading anything. In schema form the launch's repo —
    ``LMER_REPO_URL``, else ``https://{LMER_REPO_HOST}/{LMER_REPO_PROJECT}``
    — is normalized and looked up across every registry entry's ``repo:``
    and ``remotes:`` (D8); a hit is ``({work_root}/<shortname>, None)``. No
    hit (``(None, None)``) means the repo is unregistered and its work lives
    in the ``{host}/{project}`` host bucket (D9), which the caller falls
    through to. Registry keys must be §1 short names
    (``[a-z0-9][a-z0-9_-]*``) — the work repo is agent-writable, so
    anything else is skipped rather than joined into a path.

    Fail-CLOSED on a manifest that is present but unusable (unreadable,
    not YAML, not a mapping, or a ``projects:`` that is not a mapping):
    ``(None, <reason>)``. A present-but-broken manifest is never demoted
    to legacy form (plan T6) — a one-character typo must not silently
    point the tier search at the stale pre-migration ``{host}/{project}``
    bucket — so the caller drops the project-scoped tier altogether and
    reports ``<reason>``. This is the one place the lookup differs from
    "skip what you cannot use": all three second-name sites (this pair,
    hooks/start.py, hooks/run_state_guard.py) now take the same reading.
    A missing ``projects:`` is an empty registry, not a problem; an entry
    that is not a mapping is skipped (the validator's complaint, D19).
    """
    manifest = work_root / "_lmer.yaml"
    if not manifest.exists():
        return None, None
    try:
        with open(manifest, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except OSError:
        return None, "unreadable"
    except yaml.YAMLError:
        return None, "not valid YAML"
    if not isinstance(doc, dict):
        return None, "document is not a mapping"
    projects = doc.get("projects")
    if projects is None:
        projects = {}
    if not isinstance(projects, dict):
        return None, "projects: is not a mapping"
    repo_url = os.environ.get("LMER_REPO_URL")
    if not repo_url:
        repo_host = os.environ.get("LMER_REPO_HOST")
        repo_project = os.environ.get("LMER_REPO_PROJECT")
        if not (repo_host and repo_project):
            return None, None
        repo_url = f"https://{repo_host}/{repo_project}"
    wanted = _normalize_repo_url(repo_url).lower()
    if not wanted:
        return None, None
    for name, entry in projects.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            continue
        if not isinstance(entry, dict):
            continue
        remotes = [entry.get("repo")]
        extra = entry.get("remotes")
        if isinstance(extra, list):
            remotes.extend(extra)
        for remote in remotes:
            if isinstance(remote, str) and _normalize_repo_url(remote).lower() == wanted:
                return work_root / name, None
    return None, None


def plexus_manifest_problem():
    """Why ``{LMER_WORK_REPO_PATH}/_lmer.yaml`` cannot be used, or None.

    The reason string ``_plexus_project_dir`` fails closed on, surfaced
    for callers that report rather than resolve: None when there is no
    work repo, no manifest, or a usable one. The tier search itself stays
    silent; hooks/start.py turns this into its one-line diagnostic.
    """
    work_repo_path = os.environ.get("LMER_WORK_REPO_PATH")
    if not work_repo_path:
        return None
    work_root = Path(work_repo_path)
    if not work_root.is_dir():
        return None
    _schema_root, problem = _plexus_project_dir(work_root)
    return problem


def work_repo_taskdef_dirs():
    """Return work-repo taskdef directories in precedence order.

    Looks up:
      1. {LMER_WORK_REPO_PATH}/<shortname>/_taskdef/
         (project-scoped, schema form — <shortname> comes from the
         ``_lmer.yaml`` registry via _plexus_project_dir(); absent when
         the tree is legacy or the repo is unregistered)
      2. {LMER_WORK_REPO_PATH}/{LMER_REPO_HOST}/{LMER_REPO_PROJECT}/taskdef/
         (project-scoped, legacy {host}/{project} — the host bucket an
         unnamed repo's work lives in, spec D9)
      Both project tiers are dropped when a ``_lmer.yaml`` is present
      but unusable (fail-closed, plan T6 — see _plexus_project_dir();
      plexus_manifest_problem() says why).
      3. {LMER_WORK_REPO_PATH}/_taskdef/ then {LMER_WORK_REPO_PATH}/taskdef/
         (work-global — applies to all projects)

    Each project root is probed for ``_taskdef/`` (the schema-owned name,
    spec §2) and then the bare ``taskdef/`` (grandfathered until the tree
    migrates, D6); the global tier likewise. Only directories that exist
    on disk are returned. Returns an empty list when LMER_WORK_REPO_PATH is
    unset or does not exist.
    """
    dirs = []
    work_repo_path = os.environ.get("LMER_WORK_REPO_PATH")
    if not work_repo_path:
        return dirs
    work_root = Path(work_repo_path)
    if not work_root.is_dir():
        return dirs

    project_roots = []
    schema_root, problem = _plexus_project_dir(work_root)
    if schema_root is not None:
        project_roots.append(schema_root)
    repo_host = os.environ.get("LMER_REPO_HOST")
    repo_project = os.environ.get("LMER_REPO_PROJECT")
    if problem is None and repo_host and repo_project:
        project_roots.append(work_root / repo_host / repo_project)

    for root in project_roots + [work_root]:
        for name in ("_taskdef", "taskdef"):
            candidate = root / name
            if candidate.is_dir():
                dirs.append(candidate)

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
      1. Work-repo project taskdefs: {work_repo}/<shortname>/_taskdef/
         (schema form, registry lookup) then the legacy
         {work_repo}/{host}/{project}/taskdef/ — see work_repo_taskdef_dirs()
      2. Work-repo global taskdefs: {work_repo}/_taskdef/ then {work_repo}/taskdef/
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
