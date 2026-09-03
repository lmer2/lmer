#!/Agents/global/.venv/bin/python3
"""
Hook for start command.
Prompts the task's instructions to Claude at the start of a task.
Usage:
  start [work_mode]  - Read instructions with optional work mode (default: "finish", valid: "finish", "phasic")
  start               - Read instructions from current task context (LMER_TASK) with default work mode "finish"
"""
import sys
import os
import re
import time
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse
import json
import yaml
from jinja2 import Environment, FileSystemLoader, Template
from jinja2 import nodes as jinja_nodes

# Importable in every invocation path: the .sh wrappers export
# PYTHONPATH=/Agents/global/src, and direct invocation runs the venv python,
# which has the lmer package installed.
from work_repo.utils import (
    is_secret_env_name,
    redact_secrets,
    strip_url_credentials,
)

# Taskdef schema versions this renderer understands (docs/TASKDEFS.md).
# Schema 1 is the legacy include-style layout (no manifest); schema 2 adds
# `{% extends 'base-task.jinja2' %}` bodies over the builtin base template.
# A source root declares its schema in a `taskdef.yaml` manifest; an absent
# manifest means schema 1 (grandfather clause).
SUPPORTED_TASKDEF_SCHEMAS = (1, 2)

TASKDEF_MANIFEST = "taskdef.yaml"


def _is_github_host(host):
    """Return True for GitHub / GitHub Enterprise hosts.

    Local mirror of ``lmer_cli.tokens._is_github_host`` so this hook stays
    self-contained (it runs under the global venv and does not import the
    lmer_cli package). Keep the two in sync — a source-level guard test in
    ``tests/test_start_hook.py`` asserts the bodies match.
    """
    if not host:
        return False
    h = host.lower()
    return h == "github.com" or h.endswith(".github.com") or h.endswith(".ghe.com")


def _target_provider_flags():
    """Compute ``(is_github, is_gitlab)`` booleans for the main task target.

    The host is taken from ``LMER_REPO_HOST`` when set, otherwise parsed from
    ``LMER_TASK_TARGET`` / ``LMER_REPO_URL``. github.com and GitHub Enterprise
    hosts are GitHub; any other non-empty host is treated as GitLab — the same
    binary github-or-gitlab model used by ``lmer_cli.tokens``. When no host can
    be determined both flags are ``False``.
    """
    host = os.environ.get("LMER_REPO_HOST", "").strip()
    if not host:
        for var in ("LMER_TASK_TARGET", "LMER_REPO_URL"):
            value = os.environ.get(var, "")
            if "://" in value:
                parsed = urlparse(value)
                if parsed.hostname:
                    host = parsed.hostname
                    break
    is_github = _is_github_host(host)
    is_gitlab = bool(host) and not is_github
    return is_github, is_gitlab


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
    ``{work_root}/_plexus.yaml`` the tree is legacy and ``(None, None)`` is
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
    manifest = work_root / "_plexus.yaml"
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
    """Why ``{LMER_WORK_REPO_PATH}/_plexus.yaml`` cannot be used, or None.

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
         ``_plexus.yaml`` registry via _plexus_project_dir(); absent when
         the tree is legacy or the repo is unregistered)
      2. {LMER_WORK_REPO_PATH}/{LMER_REPO_HOST}/{LMER_REPO_PROJECT}/taskdef/
         (project-scoped, legacy {host}/{project} — the host bucket an
         unnamed repo's work lives in, spec D9)
      Both project tiers are dropped when a ``_plexus.yaml`` is present
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


def find_taskdef_instructions(taskdef_name=None):
    """Find and read task definition instructions."""
    resolved_name = taskdef_name or os.environ.get('LMER_TASK') or os.environ.get('LMER_TASKDEF')
    instructions_file = find_taskdef_file("instructions.txt", resolved_name)
    if instructions_file:
        return instructions_file

    if not resolved_name:
        print("❌ ERROR: No task definition specified and LMER_TASK not set")
        print("💡 Usage: start [work_mode]")
        return None

    search_dirs = taskdef_search_dirs()
    print(f"❌ ERROR: Instructions file not found for task '{resolved_name}'")
    print(f"💡 Searched in: {', '.join(str(d) for d in search_dirs)}")
    available = []
    for search_dir in search_dirs:
        if search_dir.exists():
            available.extend(d.name for d in search_dir.iterdir() if d.is_dir())
    if available:
        print(f"📁 Available task definitions: {', '.join(sorted(set(available)))}")
    return None


def configured_taskdef_root():
    """Root directory of the configured shared-taskdef repo, or None.

    Identification: LMER_TASKDEF_REPO must be non-empty (a shared taskdef
    repo is configured for this session) AND LMER_TASKDEF_PATHS must carry
    at least one entry — the repo's tier is the LAST entry. This mirrors how
    the CLI propagates the repo into the container: cli.py appends the
    container clone path (/taskdef) as the final LMER_TASKDEF_PATHS entry
    when LMER_TASKDEF_REPO is set, after any external taskdef mounts.
    Matching that last entry was chosen over hardcoding the /taskdef clone
    path (the hook keeps working if the clone location changes, and unit
    tests can point the root at a tmp dir) and over keying off
    LMER_TASKDEF_REPO alone (shadow detection needs to know WHICH
    taskdef_search_dirs() slot the repo occupies). Returns None when
    LMER_TASKDEF_REPO is unset/empty — legacy sessions without a shared
    taskdef repo get no detection and no output — or when
    LMER_TASKDEF_PATHS carries no entries.
    """
    if not os.environ.get("LMER_TASKDEF_REPO", "").strip():
        return None
    extra_paths = os.environ.get("LMER_TASKDEF_PATHS", "")
    entries = [p.strip() for p in extra_paths.split(":") if p.strip()]
    if not entries:
        return None
    return Path(entries[-1])


def _shadow_tier_label(search_dir):
    """Tier label for a taskdef_search_dirs() entry that shadows the
    configured taskdef repo (see detect_shadowed_taskdefs).

    Vocabulary — one unambiguous label per shadowing tier:
      - "work-repo project tier"  {work_repo}/<shortname>/_taskdef (schema
        form, via the _plexus.yaml registry) or the legacy
        {work_repo}/{host}/{project}/taskdef — either name of either root
      - "work-repo global tier"   {work_repo}/_taskdef or {work_repo}/taskdef
      - "LMER_TASKDEF_PATHS entry <dir>" for an earlier PATHS entry; the
        label carries the shadowing path so warning text stays unambiguous
        when several external taskdef mounts are active.
    """
    work_repo_path = os.environ.get("LMER_WORK_REPO_PATH")
    if work_repo_path:
        work_root = Path(work_repo_path)
        if search_dir.name in ("_taskdef", "taskdef"):
            if search_dir.parent == work_root:
                return "work-repo global tier"
            project_roots = []
            schema_root, problem = _plexus_project_dir(work_root)
            if schema_root is not None:
                project_roots.append(schema_root)
            repo_host = os.environ.get("LMER_REPO_HOST")
            repo_project = os.environ.get("LMER_REPO_PROJECT")
            if problem is None and repo_host and repo_project:
                project_roots.append(work_root / repo_host / repo_project)
            if search_dir.parent in project_roots:
                return "work-repo project tier"
    return f"LMER_TASKDEF_PATHS entry {search_dir}"


def detect_shadowed_taskdefs(configured_root=None):
    """Read-only detector: which configured-repo taskdefs are shadowed?

    Enumerates the taskdef names (subdirectories carrying instructions.txt)
    under ``configured_root`` — default: configured_taskdef_root() — and,
    for each name, walks the taskdef_search_dirs() entries that PRECEDE
    that root in precedence order. A preceding dir shadows a name when
    ``<dir>/<name>/instructions.txt`` exists — the same per-file check
    find_taskdef_file() uses to resolve instructions. The first
    (highest-precedence) match wins and is reported as
    ``(taskdef_name, winning_dir, tier_label)`` with tier_label from
    _shadow_tier_label().

    Returns [] when no taskdef repo is configured (legacy sessions stay
    silent), when the root does not appear in taskdef_search_dirs(), or
    when nothing is shadowed. Detection is diagnostics only: it never
    reorders the search, never changes tier precedence, and never raises
    into the render path — unreadable or missing directories are skipped.
    """
    try:
        if configured_root is None:
            configured_root = configured_taskdef_root()
        if configured_root is None:
            return []
        root = Path(configured_root)
        preceding = []
        found_root = False
        for search_dir in taskdef_search_dirs():
            if search_dir == root:
                found_root = True
                break
            preceding.append(search_dir)
        if not found_root or not preceding:
            return []
        try:
            names = sorted(
                entry.name
                for entry in root.iterdir()
                if entry.is_dir() and (entry / "instructions.txt").exists()
            )
        except OSError:
            return []
        shadowed = []
        for name in names:
            for search_dir in preceding:
                try:
                    hit = (search_dir / name / "instructions.txt").exists()
                except OSError:
                    continue
                if hit:
                    shadowed.append((name, search_dir, _shadow_tier_label(search_dir)))
                    break
        return shadowed
    except Exception:
        # Best-effort diagnostics — never break /start rendering.
        return []


def taskdef_shadow_warning_lines():
    """Warning lines for configured-repo taskdefs shadowed by other tiers.

    One line per shadowed name, greppable via ``TASKDEF SHADOW``:

      ⚠️  TASKDEF SHADOW: taskdef '<name>' in the configured taskdef repo
      is shadowed by <tier label>

    where <tier label> comes from _shadow_tier_label() and names the
    winning tier ("work-repo project tier", "work-repo global tier", or
    "LMER_TASKDEF_PATHS entry <dir>"). Advisory only: these lines never
    change which file is rendered, and detect_shadowed_taskdefs() is
    fail-soft, so this can never break `/start`. Legacy sessions (no
    LMER_TASKDEF_REPO) get [] — zero new output.
    """
    return [
        f"⚠️  TASKDEF SHADOW: taskdef '{name}' in the configured taskdef "
        f"repo is shadowed by {tier_label}"
        for name, _winning_dir, tier_label in detect_shadowed_taskdefs()
    ]


def plexus_manifest_warning_lines():
    """Warning line for a work-repo ``_plexus.yaml`` that is present but
    unusable — the fail-closed case of _plexus_project_dir(), which drops
    the whole project taskdef tier (schema AND legacy) rather than
    demoting the tree to legacy form (plan T6). One greppable line:

      ⚠️  PLEXUS MANIFEST: <path> present but unusable (<reason>);
      project taskdef tier skipped — fix _plexus.yaml

    Same slot and posture as taskdef_shadow_warning_lines(): advisory,
    never alters the render, [] whenever there is nothing to say (no work
    repo, no manifest, or a usable one). Not mirrored in
    lmer_cli.container.taskdefs — the library exposes the reason through
    plexus_manifest_problem() and leaves reporting to its callers.
    """
    problem = plexus_manifest_problem()
    if problem is None:
        return []
    manifest = Path(os.environ.get("LMER_WORK_REPO_PATH", "")) / "_plexus.yaml"
    return [
        f"⚠️  PLEXUS MANIFEST: {manifest} present but unusable ({problem}); "
        "project taskdef tier skipped — fix _plexus.yaml"
    ]


class TaskdefRenderError(Exception):
    """A taskdef failed schema validation or the block lint.

    Raised (never swallowed) so `/start` and `/followup` fail loudly with the
    message instead of rendering a silently-wrong prompt.
    """


def taskdef_source_root(template_file):
    """The source-root directory ``template_file`` actually resolved from.

    The root is the directory whose ``taskdef.yaml`` manifest governs the
    file: the matching ``taskdef_search_dirs()`` entry when the file came
    through the canonical search, else the parent of ``LMER_TASKDEF_DIR`` /
    the ``LMER_TASK_INSTRUCTIONS`` taskdef dir (the CLI fast-paths — both
    point at the taskdef directory itself, one level below the root). Only
    the root a file RESOLVED from is consulted — manifests in unused tiers
    can never affect a session.
    """
    resolved = template_file.resolve()
    for search_dir in taskdef_search_dirs():
        try:
            resolved.relative_to(search_dir.resolve())
        except (ValueError, OSError):
            continue
        return search_dir
    for var in ("LMER_TASKDEF_DIR", "LMER_TASK_INSTRUCTIONS"):
        value = os.environ.get(var)
        if not value:
            continue
        fast_dir = Path(value)
        if var == "LMER_TASK_INSTRUCTIONS":
            fast_dir = fast_dir.parent
        try:
            resolved.relative_to(fast_dir.resolve())
        except (ValueError, OSError):
            continue
        return fast_dir.parent
    # Conventional layout fallback: <root>/<taskdef>/<file>.
    return template_file.parent.parent


def read_taskdef_schema(source_root):
    """Schema version declared by ``source_root``'s ``taskdef.yaml``.

    Absent manifest → schema 1 (legacy, rendered exactly as before manifests
    existed). A manifest that cannot be parsed or carries a non-integer
    ``schema`` raises TaskdefRenderError — a broken manifest must fail loudly,
    not silently downgrade to legacy rendering. Booleans are explicitly
    rejected: ``schema: true`` is a YAML bool, and ``isinstance(True, int)``
    would otherwise let it sail through as schema 1.
    """
    manifest = Path(source_root) / TASKDEF_MANIFEST
    if not manifest.exists():
        return 1
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TaskdefRenderError(
            f"❌ ERROR: unreadable {TASKDEF_MANIFEST} at {manifest}: {exc}"
        )
    schema = data.get("schema") if isinstance(data, dict) else None
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise TaskdefRenderError(
            f"❌ ERROR: {manifest} must declare an integer `schema:` "
            f"(supported: {', '.join(str(s) for s in SUPPORTED_TASKDEF_SCHEMAS)})"
        )
    return schema


def _require_supported_schema(source_root, schema):
    """Raise TaskdefRenderError when ``schema`` is outside the supported set,
    naming the source, its schema, and the supported set."""
    if schema not in SUPPORTED_TASKDEF_SCHEMAS:
        raise TaskdefRenderError(
            f"❌ ERROR: taskdef source {source_root} declares schema "
            f"{schema}, but this renderer supports: "
            f"{', '.join(str(s) for s in SUPPORTED_TASKDEF_SCHEMAS)}. "
            "Upgrade lmer or pin a compatible taskdef source "
            "(LMER_TASKDEF_REF)."
        )


def check_taskdef_schema(template_file):
    """Validate the schema of the source ``template_file`` resolved from.

    Returns ``(source_root, schema)``. Raises TaskdefRenderError when the
    declared schema is not in SUPPORTED_TASKDEF_SCHEMAS.
    """
    source_root = taskdef_source_root(template_file)
    schema = read_taskdef_schema(source_root)
    _require_supported_schema(source_root, schema)
    return source_root, schema


def _template_extends_chain(env, template_name, _seen=None):
    """Names of the templates ``template_name`` (transitively) extends.

    Only literal `{% extends '...' %}` targets are followed — a dynamic
    extends expression cannot be resolved statically and is skipped.
    """
    if _seen is None:
        _seen = set()
    if template_name in _seen:
        return []
    _seen.add(template_name)
    source, _, _ = env.loader.get_source(env, template_name)
    ast = env.parse(source, name=template_name)
    chain = []
    for ext in ast.find_all(jinja_nodes.Extends):
        if isinstance(ext.template, jinja_nodes.Const):
            parent = ext.template.value
            chain.append(parent)
            chain.extend(_template_extends_chain(env, parent, _seen))
    return chain


def lint_template_blocks(env, template_name):
    """Render-time block lint: every top-level override block in a child
    template must exist somewhere in its parent chain.

    Jinja silently ignores a child block whose name is unknown to the parent
    — a renamed/removed base block would silently drop content from every
    extending body. This walks the template AST (``env.parse`` →
    ``Extends``/``Block`` nodes — deliberately NOT ``template.blocks``, a
    flat dict that cannot distinguish a top-level override from a new block
    nested inside an overridden block, which is legal Jinja and must not be
    flagged). Raises TaskdefRenderError on any violation; a template with no
    literal extends is exempt.
    """
    source, _, _ = env.loader.get_source(env, template_name)
    ast = env.parse(source, name=template_name)
    parents = _template_extends_chain(env, template_name)
    if not parents:
        return
    top_level = []

    def collect(node, inside_block):
        for child in node.iter_child_nodes():
            if isinstance(child, jinja_nodes.Block):
                if not inside_block:
                    top_level.append(child.name)
                collect(child, True)
            else:
                collect(child, inside_block)

    collect(ast, False)

    parent_blocks = set()
    for parent in parents:
        psource, _, _ = env.loader.get_source(env, parent)
        past = env.parse(psource, name=parent)
        for block in past.find_all(jinja_nodes.Block):
            parent_blocks.add(block.name)

    unknown = [name for name in top_level if name not in parent_blocks]
    if unknown:
        raise TaskdefRenderError(
            f"❌ ERROR: template {template_name} overrides block(s) "
            f"{', '.join(sorted(unknown))} that do not exist in its parent "
            f"chain ({' -> '.join(parents)}). Jinja would silently drop "
            "them. Fix the block name(s), or if a base block was "
            "renamed/removed this is a taskdef schema bump — see "
            "docs/TASKDEFS.md."
        )


def taskdef_source_banner(env, template_file, checked=None):
    """Greppable per-template source/schema banner lines.

    One ``taskdef source: <dir> (schema <n>)`` line for the rendered file's
    source root, plus one per parent template it (transitively) extends —
    labelled with the parent's name — so cross-tier shadowing of the base or
    a partial is always observable in the `/start` output.

    Every consulted source root is schema-gated, parents included — a tier
    that shadows ``base-task.jinja2`` under an unsupported schema fails the
    render here, before any banner line is emitted. ``checked`` takes the
    rendered file's already-validated ``(source_root, schema)`` pair so the
    caller's gate isn't re-run; when omitted the check runs here.
    """
    lines = []
    source_root, schema = (
        checked if checked is not None else check_taskdef_schema(template_file)
    )
    lines.append(f"taskdef source: {source_root} (schema {schema})")
    taskdef_dir = template_file.parent.parent
    template_name = str(template_file.relative_to(taskdef_dir))
    for parent in _template_extends_chain(env, template_name):
        _, filename, _ = env.loader.get_source(env, parent)
        # The loader resolved `<search_path>/<parent>` — strip the template
        # name to recover the search path, which IS the parent's source root
        # (base templates and partials live at the root of their tier).
        parent_root = Path(filename).parent
        for _ in range(len(Path(parent).parts) - 1):
            parent_root = parent_root.parent
        parent_schema = read_taskdef_schema(parent_root)
        _require_supported_schema(parent_root, parent_schema)
        lines.append(
            f"taskdef source ({parent}): {parent_root} "
            f"(schema {parent_schema})"
        )
    return lines


def render_taskdef_template(template_file, extra_context=None):
    """Render a task-definition template file with LMER_* env vars as context.

    Sets up a Jinja2 environment that can resolve `{% include %}` references
    against the current taskdef's parent directory, any LMER_TASKDEF_PATHS
    entries, and the built-in taskdef root. Validates the source's declared
    taskdef schema and runs the block lint before rendering, and prints the
    greppable `taskdef source: <dir> (schema <n>)` banner for the rendered
    template and its parent base — both `/start` and `/followup` render
    through here, so both surface the same banner. Returns the rendered
    string; raises TaskdefRenderError on an unsupported schema or a block
    lint violation.
    """
    taskdef_dir = template_file.parent.parent
    search_paths = [str(taskdef_dir)]
    for work_dir in work_repo_taskdef_dirs():
        if str(work_dir) not in search_paths:
            search_paths.append(str(work_dir))
    extra_paths = os.environ.get("LMER_TASKDEF_PATHS", "")
    if extra_paths:
        for p in extra_paths.split(":"):
            p = p.strip()
            if p and p not in search_paths:
                search_paths.append(p)
    builtin_taskdef = Path(os.environ.get("LMER_TASKDEF_ROOT", ""))
    if builtin_taskdef.is_dir() and str(builtin_taskdef) not in search_paths:
        search_paths.append(str(builtin_taskdef))
    # Defensive (issue #80): LMER_TASKDEF_ROOT can carry a path that doesn't
    # exist in this environment (e.g. a host path leaked into the container),
    # which the is_dir() guard above silently drops — always add the real
    # built-in root so shared fragments (service-mode.jinja2,
    # run-state.jinja2, …) resolve for taskdefs served from external mounts
    # or alternate work repos. Without it an external taskdef repo that
    # {% include %}s a shared partial it does not vendor would crash
    # rendering with TemplateNotFound.
    detected_builtin = builtin_taskdef_root()
    if str(detected_builtin) not in search_paths:
        search_paths.append(str(detected_builtin))
    env = Environment(loader=FileSystemLoader(search_paths))

    template_name = template_file.relative_to(taskdef_dir)

    # Schema + block-lint validation, then the greppable source banner —
    # all BEFORE rendering so a bad source fails loudly instead of
    # producing a silently-wrong prompt. The banner reuses the schema-check
    # result and additionally gates every parent tier it reports on.
    checked = check_taskdef_schema(template_file)
    lint_template_blocks(env, str(template_name))
    for line in taskdef_source_banner(env, template_file, checked=checked):
        print(line)

    template = env.get_template(str(template_name))

    # Credentialed URL values are redacted in place, not dropped: taskdefs
    # legitimately render {{ LMER_REPO_URL }}, so the tokenless URL must
    # survive and its truthiness guards must keep firing. secret_values=[]
    # is deliberate: a secret-NAMED var can hold non-secret text
    # (LMER_GITLAB_TOKEN_HOST is a hostname) that the value sweep would
    # stamp out of every URL — shape-based passes only.
    context = {
        k: redact_secrets(strip_url_credentials(v), secret_values=[])
        for k, v in os.environ.items()
        if k.startswith('LMER_') and not is_secret_env_name(k)
    }
    context['taskdef_name'] = template_file.parent.name
    context['taskdef_file'] = str(template_file)
    is_github, is_gitlab = _target_provider_flags()
    context['is_github'] = is_github
    context['is_gitlab'] = is_gitlab
    if extra_context:
        context.update(extra_context)

    return template.render(**context)


def run_state_session_start():
    """Start/claim the durable run for this session via `work session-start`
    and return its resume brief for template injection.

    Fail-soft by design (the state layer must never break a session): any
    missing binary, non-zero exit, timeout, or exception returns "".
    """
    if not shutil.which("work"):
        return ""
    try:
        result = subprocess.run(
            ["work", "session-start"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def read_and_display_instructions(instructions_file, work_mode="finish"):
    """Read and display the task instructions, rendering with Jinja2."""
    print(f"📋 Task Instructions: {instructions_file.parent.name}")
    print(f"📍 Location: {instructions_file}")
    # Tier diagnostics sit between the task-context block above and the
    # `taskdef source:` banner below the separator — impossible to miss,
    # and directly under the Location line they explain. Advisory only:
    # never alters the render, never returns False. Empty for legacy
    # sessions (no LMER_TASKDEF_REPO, no _plexus.yaml), keeping their
    # output byte-identical. The manifest line comes first: a skipped
    # project tier explains any shadow reported after it.
    for line in plexus_manifest_warning_lines() + taskdef_shadow_warning_lines():
        print(line)
    print("\n" + "="*60)

    try:
        rendered_content = render_taskdef_template(
            instructions_file,
            extra_context={
                'instructions_file': str(instructions_file),
                'work_mode': work_mode,
                'run_state_brief': run_state_session_start(),
            },
        )
    except TaskdefRenderError as exc:
        print(exc)
        return False
    print(rendered_content)

    print("="*60 + "\n")

    timestamp_file = Path.home() / ".claude/last_start_timestamp"
    timestamp_file.parent.mkdir(exist_ok=True)

    with open(timestamp_file, 'w') as f:
        json.dump({
            "timestamp": time.time(),
            "file": str(instructions_file),
            "taskdef": instructions_file.parent.name
        }, f)

    return True


# The /start banner and the taskdef context filter share one URL-credential
# rule; the historical local mirror of lmer_cli.cli._redact_env_value's URL
# branch moved to work_repo.utils.strip_url_credentials (issue #285).
_redact_url_credentials = strip_url_credentials


def check_task_context():
    """Display current task context from environment variables."""
    repo_url = os.environ.get('LMER_REPO_URL')
    task_target = os.environ.get('LMER_TASK_TARGET')
    task = os.environ.get('LMER_TASK') or os.environ.get('LMER_TASKDEF')

    if any([repo_url, task_target, task]):
        print("\n📊 Task Context:")
        if task:
            print(f"  • Task: {task}")
        if repo_url:
            print(f"  • Repository: {_redact_url_credentials(repo_url)}")
        if task_target:
            print(f"  • Target: {task_target}")
        print()


def main():
    """Main hook execution."""
    # Parse work mode parameter (default: "finish")
    work_mode = "finish"
    if len(sys.argv) > 1:
        work_mode = sys.argv[1]
        # Validate work mode
        valid_modes = ["finish", "phasic"]
        if work_mode not in valid_modes:
            print(f"⚠️  WARNING: Invalid work mode '{work_mode}'. Valid modes: {', '.join(valid_modes)}")
            print(f"   Using default mode: 'finish'\n")
            work_mode = "finish"

    print(f"🚀 Starting task (work mode: {work_mode})...\n")

    # Find instructions file (no longer accepts taskdef override)
    instructions_file = find_taskdef_instructions(None)
    if not instructions_file:
        sys.exit(1)

    # Display current task context
    check_task_context()

    # Read and display instructions
    if not read_and_display_instructions(instructions_file, work_mode):
        sys.exit(1)

    print("✅ Task instructions loaded")
    print("📋 Next: Begin working on the task following the instructions\n")


if __name__ == "__main__":
    main()
