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
from pathlib import Path
from urllib.parse import urlparse, urlunparse
import json
from jinja2 import Environment, FileSystemLoader, Template


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


def taskdef_search_dirs():
    """Return ordered list of directories to search for task definitions.

    Precedence (first match wins):
      1. Work-repo project taskdefs: {work_repo}/{host}/{project}/taskdef/
      2. Work-repo global taskdefs: {work_repo}/taskdef/
      3. LMER_TASKDEF_PATHS entries (colon-separated)
      4. Built-in taskdef directory (under /home/developer/.lmer or
         /Agents/global, depending on what is mounted)
    """
    lmer_global = Path("/home/developer/.lmer")
    agents_global = Path("/Agents/global")

    if lmer_global.exists():
        base_path = lmer_global
    elif agents_global.exists():
        base_path = agents_global
    else:
        base_path = Path.cwd()

    search_dirs = list(work_repo_taskdef_dirs())

    extra_paths = os.environ.get("LMER_TASKDEF_PATHS", "")
    if extra_paths:
        for p in extra_paths.split(":"):
            p = p.strip()
            if p:
                search_dirs.append(Path(p))

    search_dirs.append(base_path / "taskdef")
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


def render_taskdef_template(template_file, extra_context=None):
    """Render a task-definition template file with LMER_* env vars as context.

    Sets up a Jinja2 environment that can resolve `{% include %}` references
    against the current taskdef's parent directory, any LMER_TASKDEF_PATHS
    entries, and the built-in taskdef root. Returns the rendered string.
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
    env = Environment(loader=FileSystemLoader(search_paths))

    template_name = template_file.relative_to(taskdef_dir)
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


def read_and_display_instructions(instructions_file, work_mode="finish"):
    """Read and display the task instructions, rendering with Jinja2."""
    print(f"📋 Task Instructions: {instructions_file.parent.name}")
    print(f"📍 Location: {instructions_file}")
    print("\n" + "="*60)

    rendered_content = render_taskdef_template(
        instructions_file,
        extra_context={
            'instructions_file': str(instructions_file),
            'work_mode': work_mode,
        },
    )
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
