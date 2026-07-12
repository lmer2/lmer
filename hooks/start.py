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
from urllib.parse import urlparse, urlunparse
import json
import yaml
from jinja2 import Environment, FileSystemLoader, Template
from jinja2 import nodes as jinja_nodes

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

    context = {k: v for k, v in os.environ.items() if k.startswith('LMER_')}
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


def _redact_url_credentials(url):
    """Strip any embedded ``user:password@`` credentials from a URL.

    LMER_REPO_URL is typically an https clone URL carrying an ``oauth2:<token>@``
    prefix; printing it verbatim leaks the token to the console. Rebuild the URL
    from scheme/host/port/path only. Mirror of the URL branch of
    ``lmer_cli.cli._redact_env_value`` — kept local because this hook runs under
    the global venv and does not import the lmer_cli package.
    """
    if not url or "://" not in url or "@" not in url:
        return url
    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            return urlunparse(parsed._replace(netloc=netloc))
        return url
    except Exception:
        # Fail closed: a redaction helper must never emit a value that may still
        # carry credentials. If urlparse raises (e.g. an out-of-range port makes
        # `parsed.port` raise ValueError), strip the userinfo with a regex that
        # cannot raise instead of returning the original token-bearing URL.
        return re.sub(r"(://)[^/]*@", r"\1", url)


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
