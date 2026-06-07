## LMER Python CLI (lmer)

Python-first CLI for running LMER with a repository target, cloning inside the container, and optional persistent workspaces.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Install Globally](#install-globally)
- [Environment Variables](#environment-variables)
- [Work-Repo Claude Assets](#work-repo-claude-assets)
- [Tasks](#tasks)
- [Basic Usage](#basic-usage)
  - [Starting Your Task](#starting-your-task)
- [Building the Container Image](#building-the-container-image)
- [Command-Line Options](#command-line-options)
- [Troubleshooting](#troubleshooting)

### Prerequisites
- Docker or Podman installed
- Container image built or pulled:
```bash
make build
```

### Installation

#### uv tool install (recommended)

Two branches are published:

- **`prep-release`** — development branch with the latest features (may be unstable)
- **`main`** — stable branch; lags behind `prep-release`

```bash
# Latest features (may be unstable)
uv tool install lmer --from git+https://github.com/lmer2/lmer@prep-release

# Stable
uv tool install lmer --from git+https://github.com/lmer2/lmer@main
```

This installs the `lmer` command globally via uv's tool management. The container image has all rules, task definitions, and hooks baked in — no local checkout needed.

To upgrade later:
```bash
uv tool upgrade lmer
```

**Authentication setup**:

Most users authenticate Claude with their Claude.ai subscription. Run `/login` inside a `claude` session on your host once — this creates `~/.claude/.credentials.json`, which `lmer` automatically mounts into the container. No API key required.

If you authenticate via API key instead, place it in `~/.lmer/.env`:
```bash
mkdir -p ~/.lmer
cat > ~/.lmer/.env <<'EOF'
CLAUDE_API_KEY=your-claude-key
GITLAB_TOKEN=your-token-here
EOF
```

For full details on Claude and Git authentication, see [AUTHENTICATION.md](AUTHENTICATION.md).

#### Developer mode

For developing lmer itself, clone the repo and install in editable mode:

```bash
git clone https://github.com/lmer2/lmer.git ~/Agents/global
cd ~/Agents/global
uv tool install -e lmer --from .
```

In developer mode, the local Containerfile is used for builds, and local directories are mounted into the container so changes take effect immediately.

### Environment Variables

LMER automatically loads environment variables from `.env` files and passes them to containers. It checks the following locations (later entries take priority):

1. Repository root `.env` (developer mode only)
2. `~/.lmer/.env` (both modes)
3. Current working directory `.env`

All variables from `.env` files are automatically merged into the container environment. Variables already set in the environment take precedence over `.env` values.

Example `.env` file:
```bash
GITLAB_TOKEN=your-token-here
GITLAB_HOST=gitlab.com
CLAUDE_API_KEY=your-claude-key
GH_TOKEN=your-github-token
```

#### LMER-Specific Environment Variables

The following environment variables control LMER behavior:

- **`LMER_WORK_REPO`** - **Required.** Git URL of the work repo where lmer persists worklogs, gate-check results, and per-project notes across sessions. Must be a remote URL (SSH or HTTPS) the container can clone, pull from, and push to — a local-filesystem path won't work because the in-container `work` CLI actively syncs via `git fetch`/`pull`/`push`. Typically a small private repo on whatever git host you're already using.

- **`LMER_WORK_REPO_TOKEN`** - Provider-agnostic token used to authenticate against the work repo. Highest-priority lookup for work-repo clones, so it isolates the work-repo credential from per-host target-repo tokens. Works for GitLab, GitHub, and self-hosted hosts. When set, lmer rewrites an `LMER_WORK_REPO=git@host:…` URL into `https://oauth2:<token>@host/…` for cloning. The legacy `GITLAB_TOKEN_worklog` is still honored as a fallback for existing setups.

- **`LMER_WORK_REPO_PATH`** - In-container clone location of the work repo. Defaults to `/work`; rarely needs overriding.

- **`GITLAB_TOKEN`** / **`GITLAB_TOKEN_<sanitized_host>`** - Per-host or generic API token used to authenticate against GitLab hosts (also used by the legacy URL-token-injection path for target repos). Hostname suffix is lowercased with dots/hyphens replaced by underscores — e.g. `git.example.com` → `GITLAB_TOKEN_git_example_com`.

- **`GH_TOKEN`** / **`GITHUB_TOKEN`** - Tokens used for GitHub hosts (`github.com`, `*.github.com`, `*.ghe.com`). `GH_TOKEN` takes priority over `GITHUB_TOKEN`. Either is consulted only after a more-specific per-host `GITLAB_TOKEN_<host>` is checked.

- **`LMER_REGISTRY`** - Container registry to pull pre-built images from. Optional; defaults to `ghcr.io/lmer2/lmer` (the project's GHCR registry). Override to point at a self-hosted or mirrored registry. Empty-string values are treated the same as unset and fall back to the default.

- **`LMER_NO_AUTO_BUILD`** - Disable automatic container image building. Accepted truthy values: `1`, `true`, `yes` (case-insensitive). When enabled, LMER will error if the image is not found locally instead of building it.

- **`REPO_AUTH_PREFER_SSH`** - When set to a truthy value (`1`, `true`, `yes`), LMER will use SSH URLs for git operations instead of converting them to HTTPS with token authentication.

- **`LMER_REASONING_EFFORT`** - Override Claude's reasoning effort for the session. Accepted values: `low`, `medium`, `high`, `max`, `auto` (case-insensitive). When set to one of `low`/`medium`/`high`/`max`, LMER passes `--effort <level>` to the `claude` CLI. When unset or set to `auto`, no flag is passed and Claude uses its own default. Invalid values are ignored with a warning.

- **`LMER_HUMAN_IDENTITY`** - Free-form string identifying the human user the session is collaborating with (e.g. `"Jane Doe <jdoe on example.com, jane@example.com>"`). When set, it is forwarded into the container and injected into Claude's system prompt so the model can attribute matching usernames, emails, or handles in PRs, MRs, issues, comments, and commit history to the user. When unset, LMER falls back to the host's `git config user.name` and `user.email` (respecting system, global, and local config). If neither is available, no identity is injected. The injected text is rendered from the Jinja2 template at `prompts/human-identity.md.jinja2` — edit that file to change the wording.

- **`LMER_GIT_USER_NAME`** / **`LMER_GIT_USER_EMAIL`** - Override the git identity that commits made inside the container are authored and committed under. By default the container commits under the `~/.gitconfig` it inherits (a one-time copy of the host's, persisted in the container-home and bind-mounted). When either variable is set, the entrypoint exports it as git's native `GIT_AUTHOR_NAME`/`GIT_COMMITTER_NAME` (from `LMER_GIT_USER_NAME`) and/or `GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_EMAIL` (from `LMER_GIT_USER_EMAIL`). The two are independent — set only one and the other half falls back to gitconfig. The override is session-scoped and writes no file: the mounted `~/.gitconfig` is left untouched, so unsetting the variable fully reverts the behavior (no persistent state is mutated). Note that this affects commit authorship, not `git config --get user.name`/`user.email` reads, which still report the gitconfig values. This is distinct from `LMER_HUMAN_IDENTITY`, which controls who Claude attributes repository artifacts to in its system prompt and does not change commit authorship.

- **`LMER_QUICK_GATE_COMMIT`** - When set to a truthy value (`1`, `true`, `yes`, case-insensitive), `gate-commit` skips the test suite (the slowest check) but still runs pre-commit hooks, secret scans, and every other check. Tests are still enforced by standalone `gate-check` and by `gate-push`, so coverage is preserved before code leaves the local repo. Only `gate-commit` reads this variable; `gate-check` and `gate-push` ignore it. Falsy values (`0`, `false`, `no`) and unset both leave tests running, so this can be a transient export that you turn off without `unset`. Useful for iterative commits on a feature branch where you'll run `gate-push` (which runs the suite) before code leaves the repo.

- **`LMER_PERSIST_AGENT_MEMORY`** - When set to a truthy value (`1`, `true`, `yes`, case-insensitive), Claude Code's agent memory is persisted to the work repo on a **per-project** basis, stored under `{host}/{project}/memory/` (shared across all task types and targets for that project). Restore is automatic: at session start `libexec/claude-runner.sh` runs `work memory restore`, which copies any saved memory from the work repo into Claude's memory directory (`~/.claude/projects/-workspace/memory/`) before Claude reads it. Persisting back is the agent's responsibility — the agent runs `work memory persist` (which copies the memory directory into the work repo and commits and pushes it). Falsy values (`0`, `false`, `no`) and unset both disable the feature, in which case both `work memory` subcommands are no-ops. Parsed via `get_bool_env` and forwarded into the container by the host CLI. The memory directory path can be overridden for non-standard layouts/tests with `LMER_AGENT_MEMORY_DIR`.

- **`LMER_PIDS_LIMIT`** - Overrides the container PID cap that LMER passes as `--pids-limit` to `docker`/`podman run` (default `512`). Accepts any **positive integer**, or **`-1`** for "unlimited" (Docker/Podman semantics). Any other value — `0`, other negatives, or non-numeric — is rejected with a warning and falls back to `512`, so a misconfiguration can never silently weaken the fork-bomb safety bound. This is a **host-side** variable read by the launching CLI; it does not need to reach inside the container. Raise it (or set `-1`) on hosts affected by the cgroup-v1 pids-controller counter leak, where phantom fork entries accumulate over a long session and prematurely exhaust the cap — see [Troubleshooting: containers hit the PID cap](#troubleshooting-containers-hit-the-pid-cap-cgroup-v1-pids-leak).

- **`LMER_PORT_COUNT`** - Number of host ports to allocate from the pool (see `LMER_PORT_POOL`) and publish into the container, so a service Claude starts inside (e.g. a dev web server) is reachable from the host. Equivalent to the `--ports` flag, which takes precedence when both are set. Must be a non-negative integer; `0` (or unset) disables port passthrough. A non-numeric value aborts startup with an error. The allocated ports are exported to the container as `LMER_PORTS`. Read by the host CLI only.

- **`LMER_PORT_POOL`** - Inclusive port range `LOW-HIGH` the `--ports`/`LMER_PORT_COUNT` ports are picked from (default `8800-8899`, kept distinct from the FastAPI range `8700-8799` so both features can be used together). Equivalent to the `--port-pool` flag, which takes precedence. The host CLI picks the requested number of currently-free ports from this pool before the container starts, so multiple `lmer` instances on the same host get disjoint ports without manual coordination. If fewer than the requested number of free ports are available, startup aborts with an error. Read by the host CLI only.

- **`LMER_PORTS`** - Set **by lmer inside the container** (not a host input): a comma-separated list of the ports allocated via `--ports`/`LMER_PORT_COUNT` (e.g. `8842,8857`). Each is published on the host (loopback `127.0.0.1` by default, overridable via `LMER_PORT_BIND`) with the same port number inside and out. Services Claude starts should bind to `0.0.0.0` on one of these ports to be reachable from the host. Empty/unset when no ports were requested.

- **`LMER_PORT_BIND`** - Host bind address used when publishing `--ports`/`LMER_PORT_COUNT` mappings (default `127.0.0.1`). Equivalent to the `--port-bind` flag, which takes precedence when both are set. Set to `0.0.0.0` to expose the allocated ports on every host interface (so other machines on the LAN can reach a service Claude starts inside the container), or to a specific IP (e.g. `192.168.1.42`) to publish only on that interface. The value is also used to probe for free ports in the pool, so the chosen ports are guaranteed bindable on that address. **Security note:** the default is loopback for a reason — opening published ports to the network exposes any service Claude binds inside the container; only widen the bind when you trust both the network and what the agent is running. Read by the host CLI only.

- **`LMER_AUTO_START_DELAY`** - Seconds the supervisor waits before injecting the initial `/start` into Claude (the marker-based prompt-ready wait, see `LMER_AUTO_START_READY_TIMEOUT`, may extend this). Accepts a float (default `1.5`); negative values are clamped to `0`. Also settable per-invocation with `--auto-start-delay`. Has no effect under `--manual-start`/`LMER_MANUAL_START`. Parsed by `lmer-supervisor` and forwarded into the container by the host CLI.

- **`LMER_AUTO_START_NUDGE_DELAY`** - Seconds between the follow-up carriage-return "nudges" that the supervisor sends after auto-injecting `/start`. The initial Enter is occasionally swallowed during Claude's startup re-render, leaving `/start` typed but unsubmitted; the supervisor sends a few bare CRs afterward to re-trigger submission (each is a harmless no-op once `/start` has gone through). Accepts a float (default `0.5`); negative values are clamped to `0`. Has no effect under `--manual-start`/`LMER_MANUAL_START`. Parsed by `lmer-supervisor` and forwarded into the container by the host CLI.

- **`LMER_AUTO_START_READY_TIMEOUT`** - Maximum seconds the supervisor will wait for Claude's input-prompt glyph (`❯`) to appear in the output stream before auto-injecting `/start`. Claude Code v2.1.119 changed Enter routing so any modal/dialog open during startup (theme picker, IDE detect, permission prompt, etc.) consumes a CR rather than also submitting input-box text; waiting for the prompt glyph lets Claude finish its startup chain before we type. On timeout the injection fires anyway (with the cooked-mode pre-clear + CR nudges still providing best-effort delivery). Accepts a float (default `15.0`); set to `0` to disable marker-based readiness and inject purely on the initial delay. Has no effect under `--manual-start`/`LMER_MANUAL_START`. Parsed by `lmer-supervisor` and forwarded into the container by the host CLI.

- **`LMER_AUTO_START_READY_MARKER`** - UTF-8 string the supervisor scans for in Claude's output to decide that the input prompt has rendered (default `❯` — U+276F). The marker is a heuristic: `❯` is also used as a row-selection indicator in some TUI pickers, so a future Claude UI change could shift the right marker. Override here lets you patch in a more specific string (e.g. a unique-to-input-box sequence) without an lmer release. Set to the empty string to disable marker gating entirely (equivalent to `LMER_AUTO_START_READY_TIMEOUT=0`). Has no effect under `--manual-start`/`LMER_MANUAL_START`. Forwarded into the container by the host CLI.

- **`LMER_START_PROMPT`** - Follow-up prompt the supervisor types and submits immediately after auto-injecting `/start`, so an automated run can hand Claude an extra instruction without manual typing (e.g. `"make sure to research X online first"`). Claude queues input typed while it is still working on `/start`, so the prompt becomes the next conversation turn. Its submit Enter gets the same bare-CR nudge re-submission as `/start` (governed by `LMER_AUTO_START_NUDGE_DELAY`) in case the initial CR is swallowed. Set on the host via the `--prompt` CLI flag (which populates this var); an empty or unset value means no follow-up. Tied to auto-start, so it is a **no-op under `--manual-start`/`LMER_MANUAL_START`** (nothing is auto-injected then). Forwarded into the container by the host CLI.

### Work-Repo Claude Assets

The work repository can contribute Claude Code slash commands, skills, and a limited slice of `settings.json` to every session that uses it. This is the supported way to ship project-specific automation, runbooks, or pre-authorized tool patterns across all developers who share the work repo.

Layout (relative to the work-repo root, i.e. `/work/agent-files/claude/` inside the container):

```
agent-files/claude/
├── commands/
│   └── deploy.md            # Available as /deploy in the session
├── skills/
│   └── runbook-xyz/
│       └── SKILL.md         # Auto-discovered Claude Code skill
└── settings.json            # Only permissions.allow is merged
```

At container start, `claude-runner.sh` does the following:

- Symlinks every entry under `agent-files/claude/commands/` into `~/.claude/commands/` so the files are visible to Claude Code's slash-command loader.
- Symlinks every skill directory under `agent-files/claude/skills/` into `~/.claude/skills/` so Claude Code's skill discovery picks them up.
- If `agent-files/claude/settings.json` exists, merges its `permissions.allow` array into `~/.claude/settings.json` (deduplicated). No other keys are honored — work-repo `settings.json` cannot, for example, add a `deny` entry or change the status-line command, so a misconfigured work repo cannot weaken protections that live in the global settings.

Both sources (lmer global tree at `/Agents/global/agent-files/claude/`, plus the work repo) are overlaid, with work-repo entries overriding global ones on name collision. Per Claude Code's skill loader, changes to skills under `~/.claude/skills/` take effect immediately within the running session — adding a new file in the work repo and re-syncing does not require a container restart.

### Tasks

LMER uses task-based workflows. Tasks are discovered from (in precedence order) the work-repo (project-scoped, then global), `LMER_TASKDEF_PATHS` (colon-separated), and the built-in `taskdef/` directory. The only built-in task is:
- `chat` - Interactive chat session

Additional task types can be supplied via the work-repo or `LMER_TASKDEF_PATHS` — see [TASKDEFS.md](TASKDEFS.md) for the format and discovery rules.

A task is any subdirectory containing an `instructions.txt` file. For example, a minimal chat task:

```
my-tasks/
└── chat/
    └── instructions.txt
```

Where `instructions.txt` contains Jinja2-templated instructions:

```
You are chatting with the user about {{ LMER_REPO_URL }}.

The current work mode is `{{ work_mode }}`.
```

Set `LMER_TASKDEF_PATHS=/path/to/my-tasks` to make your tasks available.

### Basic Usage

**Syntax**: `lmer <task> [<target>...] [options]`

- `<task>` is a required positional argument specifying the task type (e.g., `chat`, or any task discovered via `LMER_TASKDEF_PATHS`)
- `<target>` is an optional repo URL, local git path, or PR/MR/issue URL. Multiple targets can be specified.
  - **First target (primary)**: Sets environment variables and is cloned into `/workspace`
  - **Additional targets (secondary)**: Cloned into subdirectories (e.g., `/workspace/mr-123`) but do not override environment variables
- If no target is provided, the CLI will try to infer from the current directory's git remote.
- When providing a local git path, lmer extracts the remote URL instead of using `file://` protocol.
- PR/MR/issue URLs (e.g., `https://github.com/org/repo/pull/123`) are automatically converted to their base repository URLs.

**Default behavior**: Without `--exec`, lmer runs `claude-runner` which starts an interactive Claude Code session.

### Starting Your Task

**Important**: Once inside the container and Claude Code is started, you **must** manually start the task via the `/start` command. The task context is automatically set based on the task you specified when launching lmer (e.g., `lmer chat <repo>`).

Simply type `/start` in the Claude Code chat interface to begin your task with the appropriate instructions loaded.

#### Work Modes

The `/start` command supports two work modes:

- **`/start`** or **`/start finish`** (default) - Complete the task in one session. Claude will work through the entire task without stopping.

- **`/start phasic`** - Work in phases with explicit stopping points. Claude will:
  - Check the worklog to understand what needs to be done next
  - Use `work goal` to set and track goals for each phase
  - File a report using `work report` at the completion of each phase
  - Stop after completing each phase and yield control back to you for review

**When to use phasic mode:**
- For complex tasks that benefit from iterative review
- When you want to review progress at specific checkpoints
- For long-running tasks where you want to provide feedback between phases
- When working on tasks that have natural phase boundaries

**Example:**
```bash
# Start in default (finish) mode
/start

# Start in phasic mode
/start phasic
```

#### Follow-up Instructions

For cases where a task needs to continue after its initial run (for example, addressing review feedback left on a merge request opened by a previous task run), the task definition can ship a `followup.txt` file alongside its `instructions.txt`. Invoke it with:

```bash
/followup
```

The `/followup` command loads `followup.txt` from the active task definition directory and renders it with the same Jinja2 context as `/start` (all `LMER_*` env vars). If a task type does not provide a `followup.txt`, the command exits with an error pointing at where it looked. Task types opt in simply by adding the file — no code change in lmer is required.

```bash
# Remote repo URL with task
lmer chat https://github.com/org/repo.git
lmer chat git@github.com:org/repo.git

# PR/MR/issue URLs are supported (automatically extracts repo URL)
lmer chat https://github.com/org/repo/pull/123
lmer chat https://gitlab.com/group/project/-/merge_requests/456

# Multiple targets: first is primary (sets env vars), others are cloned but don't override env vars
lmer chat https://gitlab.com/group/project/-/merge_requests/756 https://gitlab.com/group/project/-/merge_requests/757
# Primary MR (756) is cloned into /workspace and sets environment variables
# Secondary MR (757) is cloned into /workspace/mr-757 but doesn't override env vars

# Local git repo as source - extracts remote URL automatically
lmer chat /path/to/repo

# Local git repo with multiple remotes - specify which one
lmer chat /path/to/repo --remote origin
lmer chat /path/to/repo --remote github

# Infer from current directory (uses git remote)
cd /path/to/repo
lmer chat

# Exec a shell in the prepared workspace (requires --exec)
lmer chat https://github.com/org/repo.git --exec -- bash

# Skip clone, just get a shell for debugging (no repo required)
lmer --no-task --no-clone --exec -- bash

# Run without a task (exec mode only)
lmer --no-task https://github.com/org/repo.git --exec -- bash

# Run as root (uid:gid 0:0) when needed
lmer chat https://github.com/org/repo.git --user 0:0 --exec -- bash

# Match host UID:GID for SSH agent permissions
lmer chat https://github.com/org/repo.git --match-uid --exec -- bash

# Checkout a specific branch or ref
lmer chat https://github.com/org/repo.git --branch feature/x
lmer chat https://github.com/org/repo.git --ref v1.2.3

# Enable verbose output
lmer chat https://github.com/org/repo.git --verbose
```

### Building the Container Image

`lmer build` builds the container image from the local Containerfile. By default it passes `--pull` to `docker build` to refresh the base image layers.

```bash
# Build image (pulls latest base image layers by default)
lmer build

# Build without refreshing base image layers
lmer build --no-pull

# Delete existing image before building (clean build)
lmer build --force

# Build from a local repo checkout (useful when lmer is installed via pip/uv)
lmer build --local /path/to/agents/global
```

**Options:**
- `--no-pull` — Skip passing `--pull` to docker/podman build (don't refresh base image layers)
- `--force` — Delete the existing image before building (otherwise the tag is simply overwritten)
- `--local PATH` — Path to a local repo checkout containing the Containerfile. The image is built from this checkout but tagged to match the installed package version so `lmer chat` finds it. This also ensures the container user UID/GID matches your host user, avoiding permission issues with bind-mounted directories.

**Note:** In developer mode (running from a git checkout), `lmer build` automatically finds the Containerfile. Use `--local` when lmer is installed as a package (via pip/uv) and you need to build from a separate checkout.

### Command-Line Options

- `<target>...` - One or more repository URLs, local git paths, or PR/MR/issue URLs. The first target is the primary target (sets environment variables and is cloned into `/workspace`). Additional targets are secondary (cloned into subdirectories like `/workspace/mr-123` but don't override environment variables).
- `--exec` - Run an arbitrary command in the container instead of starting Claude Code
- `--no-clone` - Skip git clone, just run command (requires `--exec` and `--no-task`)
- `--no-task` - Run without selecting a task (exec mode only)
- `--workspace-volume <name>` - Use a Docker/Podman named volume for workspace persistence (currently not functional)
- `--workspace-bind <path>` - Bind mount a host path for workspace (currently not functional)
- `--user <user>` - Container user (e.g., `developer` or `0:0`)
- `--match-uid` - Run container as your host UID:GID (fixes SSH agent permissions)
- `--branch <branch>` - Checkout a specific branch (applies to primary target only)
- `--ref <ref>` - Checkout a specific ref (tag or commit) (applies to primary target only)
- `--remote <name>` - Git remote name to use (required when local repo has multiple remotes)
- `--verbose` - Enable verbose output
- `--manual-start` - Do not auto-inject `/start` into Claude on launch. By default the supervisor sends `/start` shortly after Claude is ready so the configured task begins immediately
- `--prompt <text>` - Follow-up prompt injected immediately after the auto-`/start`, so an automated run can hand Claude an extra instruction without manual typing (e.g. `lmer chat <issue-url> --prompt="research X online first"`). Claude queues input typed while it is still working on `/start`, so the prompt becomes the next conversation turn. Forwarded to the container as `LMER_START_PROMPT`. Ignored under `--manual-start` (nothing is auto-injected then)
- `--fastapi` - Expose a FastAPI endpoint inside the container that lets a controlling process drive Claude's stdin/stdout (`POST /input`, `GET /output`). The chosen port is published to `127.0.0.1` on the host. See [Supervisor and FastAPI Endpoint](#supervisor-and-fastapi-endpoint)
- `--fastapi-port-range LOW-HIGH` - Port range to pick a free FastAPI port from (default `8700-8799`). The host CLI picks one free port from this range before container start and publishes only that port
- `--fastapi-host <host>` - Inside-container bind host for the FastAPI endpoint (default `0.0.0.0` when `--fastapi` is set so the published port works)
- `--fastapi-token <token>` - Bearer token to require on FastAPI requests. If omitted a random token is generated and printed to stderr on startup
- `--ports <N>` - Allocate `N` free host ports and publish them into the container so a service Claude starts inside (e.g. a dev web server) is reachable from the host. The host CLI picks `N` currently-free ports from `--port-pool` before the container starts, publishes each on the host (loopback `127.0.0.1` by default, override with `--port-bind`) with the same port number inside and out, and exports the list to the container as `LMER_PORTS`. Startup aborts if `N` free ports can't be found. Also settable via `LMER_PORT_COUNT` (the flag wins). Bind services to `0.0.0.0` inside the container so the published mapping works. See [Port Passthrough](#port-passthrough)
- `--port-pool LOW-HIGH` - Inclusive port pool the `--ports` ports are picked from (default `8800-8899`, distinct from the FastAPI range so both features coexist). Also settable via `LMER_PORT_POOL` (the flag wins)
- `--port-bind <addr>` - Host bind address used when publishing the allocated `--ports` mappings (default `127.0.0.1`). Pass `0.0.0.0` to expose the ports on every host interface (so other machines on the LAN can reach a service Claude starts inside), or a specific IP to publish only on that interface. The address is also used to probe for free ports in the pool, so the picked ports are guaranteed bindable there. Also settable via `LMER_PORT_BIND` (the flag wins). The default is loopback for a reason — only widen it when you trust both the network and what the agent is running

**Note**: `--workspace-volume` and `--workspace-bind` options are currently not functional. The workspace uses the `/workspace` directory from the container image instead.

### Supervisor and FastAPI Endpoint

Claude is launched through `lmer-supervisor`, a Python process that sits between your terminal and the Claude CLI. It allocates a PTY and forwards keystrokes/output transparently. The supervisor can also expose a FastAPI control plane.

**Auto `/start`** — by default `/start` is typed into Claude after a short delay so an lmer task begins without manual intervention. Because the trailing Enter is occasionally swallowed during Claude's startup re-render (leaving `/start` typed but unsubmitted), the supervisor (1) pre-clears cooked-mode PTY flags before fork so the CR isn't translated to LF, (2) defers injection until Claude has actually rendered the input prompt glyph (`❯`) so any startup modal/dialog has had a chance to clear, and (3) follows the initial `/start\r` with a few bare carriage-return nudges to re-trigger submission; each nudge is a no-op once `/start` has gone through. Tune the gap between nudges with `--auto-start-nudge-delay` (or `LMER_AUTO_START_NUDGE_DELAY`) and the maximum prompt-ready wait with `--auto-start-ready-timeout` (or `LMER_AUTO_START_READY_TIMEOUT`). Disable auto-start entirely with `--manual-start` (or `LMER_MANUAL_START=1`) when you want to drive Claude yourself.

**Follow-up prompt** — pass `--prompt "<text>"` (or set `LMER_START_PROMPT`) to have the supervisor type and submit an extra instruction right after the `/start` injection. Claude queues input typed while it is still working on `/start`, so the prompt lands as the next conversation turn — handy for automating `lmer chat <issue-url> --prompt="research X online first"`. It is part of the auto-start flow, so it is ignored under `--manual-start` (where nothing is auto-injected).

**FastAPI endpoint** — pass `--fastapi` to expose two endpoints (bearer-token protected):

- `POST /input` — body `{"data": "...", "append_newline": true}` writes to Claude's stdin. When `append_newline` is true and `data` does not already end with `\r` or `\n`, a CR (`\r`) is appended so Claude's TUI treats it as Enter — `\n` would only insert a literal newline into the input box without submitting
- `GET /output?cursor=N&timeout=S` — returns buffered output past `cursor` with optional long-poll timeout
- `GET /healthz` — liveness probe (also requires the bearer token)

The host CLI picks one free port from the configured range before the container starts and publishes only that single port to `127.0.0.1` on your host. The picked port is passed into the container via `LMER_FASTAPI_PORT` so the supervisor binds to it inside; this lets multiple `lmer ... --fastapi` sessions coexist on the same host with default flags. Both `LMER_FASTAPI_PORT` and the bearer token (`LMER_FASTAPI_TOKEN`) are exported in the in-container environment so processes spawned by Claude can discover them; the chosen port and a hint about the token are also printed to stderr on startup.

#### Talking to a running session — the `lmer-pipe` CLI

The supervisor ships with a thin client called `lmer-pipe` that wraps the FastAPI endpoint so you don't have to assemble curl + jq pipelines. Every subcommand reads `LMER_FASTAPI_PORT` and `LMER_FASTAPI_TOKEN` from the environment by default, so once you've exported them you get one-liners:

| Need to                                | Command                       |
|----------------------------------------|-------------------------------|
| Type something at Claude's prompt      | `lmer-pipe send "/followup"`  |
| See everything Claude has produced     | `lmer-pipe read`              |
| Stream output live (`tail -f` style)   | `lmer-pipe follow`            |
| Stream only new output, skip backlog   | `lmer-pipe follow --from-end` |
| Check the endpoint is alive            | `lmer-pipe health`            |

Connection settings can also be passed as flags (`--port`, `--token`, `--host`, `--url`) and `--json` is available on every command if you'd rather pipe to `jq`.

#### Foreground example

```bash
lmer chat https://github.com/owner/repo --fastapi --manual-start
# Stderr will show the published port and a hint about the token.

# In another terminal:
export LMER_FASTAPI_PORT=8742
export LMER_FASTAPI_TOKEN=<token from stderr>

lmer-pipe send "/start"
lmer-pipe read
```

#### Running lmer in the background and checking in on it

The FastAPI endpoint is the natural way to drive a long-running lmer session you don't want to babysit at a TTY. Pin the port and token up front so the helpers can find the endpoint without scraping logs, launch detached, then use `lmer-pipe` to drive it.

**Step 1 — pin a port and token, launch detached:**

```bash
export LMER_FASTAPI_PORT=8780
export LMER_FASTAPI_TOKEN="$(openssl rand -hex 24)"

mkdir -p ~/lmer-runs/issue-31
nohup lmer chat https://gitlab.example.com/group/project/-/issues/31 \
    --fastapi \
    --fastapi-port-range "${LMER_FASTAPI_PORT}-${LMER_FASTAPI_PORT}" \
    --fastapi-token "${LMER_FASTAPI_TOKEN}" \
    > ~/lmer-runs/issue-31/stdout.log \
    2> ~/lmer-runs/issue-31/stderr.log < /dev/null &
disown
echo "PID=$! PORT=${LMER_FASTAPI_PORT}"
```

By default `/start` is auto-injected, so once the container is up Claude starts working on the issue immediately. Add `--manual-start` if you'd rather kick things off yourself with `lmer-pipe send /start`.

**Step 2 — wait until the endpoint is up:**

```bash
until lmer-pipe health >/dev/null 2>&1; do sleep 1; done
echo "lmer is up"
```

**Step 3 — see what Claude has done so far:**

```bash
lmer-pipe read
```

**Step 4 — follow output live:**

```bash
lmer-pipe follow            # full backlog then live tail
lmer-pipe follow --from-end # only new output
```

`follow` long-polls for up to 15 s per request, so it won't busy-spin while Claude is thinking. Hit Ctrl-C to stop. If the follower falls so far behind that older bytes get evicted (the buffer holds the most recent 1 MiB), a warning is printed to stderr.

**Step 5 — type something at Claude:**

```bash
lmer-pipe send "/followup"
lmer-pipe send "yes please continue"
lmer-pipe send "/start" --no-newline   # raw, no trailing newline
```

Anything you'd type at Claude's prompt — slash commands, free-form text — works the same way.

**Step 6 — stop the session:**

Ask Claude to exit the way you would interactively:

```bash
lmer-pipe send "/exit"
```

Or, as a hard fallback, stop the container directly:

```bash
docker ps --filter ancestor=lmer --format '{{.ID}} {{.Names}}'
docker stop <container-id>
```

#### Driving lmer from inside the container

When something running *inside* the container (Claude itself, a hook, a script Claude spawned) wants to interact with its own session, the supervisor has already exported `LMER_FASTAPI_PORT` and `LMER_FASTAPI_TOKEN` into the in-container environment. The same `lmer-pipe` commands work with no extra setup.

#### Raw HTTP (alternative to `lmer-pipe`)

If you'd rather avoid `lmer-pipe` and call the endpoint directly:

```bash
curl -sS -H "Authorization: Bearer $LMER_FASTAPI_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"data": "/start", "append_newline": true}' \
     "http://127.0.0.1:$LMER_FASTAPI_PORT/input"

curl -sS -H "Authorization: Bearer $LMER_FASTAPI_TOKEN" \
     "http://127.0.0.1:$LMER_FASTAPI_PORT/output?cursor=0&timeout=5"
```

#### Tips

- **Multiple parallel sessions.** The host CLI publishes only the single port it picked, so leaving `--fastapi-port-range` at the default and running several `lmer ... --fastapi` invocations in parallel works out of the box — each picks a distinct free port from `8700-8799`. Set `LMER_FASTAPI_PORT` differently in each shell when using `lmer-pipe` against multiple sessions.
- **Discovering the auto-picked port and token.** When you don't pin them, both are printed to stderr at startup (the host CLI logs the published port; the supervisor logs the same port plus a hint about the token). Inside the container, processes spawned by Claude can read them from `LMER_FASTAPI_PORT` and `LMER_FASTAPI_TOKEN`.
- **Auth is required on every request.** Including `/healthz`. Wrong tokens come back as `lmer-pipe: HTTP 401`.
- **The endpoint binds to `127.0.0.1` on the host.** Other machines on your network cannot reach it. If you need remote access, tunnel via SSH (`ssh -L 8780:127.0.0.1:8780 …`) rather than rebinding to `0.0.0.0`.
- **Restoring a follower.** Pass `--since N` to `lmer-pipe read`/`follow` to resume from a known cursor; anything older than `cursor - 1 MiB` will be reported as `dropped_bytes` in the response.

### Port Passthrough

When Claude runs a service inside the container that you want to test from your host browser — a dev web server, an API, a preview build — that service's port needs to be published out of the container. `--ports` automates this without making you hand-pick port numbers, which matters when several `lmer` sessions run at once.

```bash
# Allocate 2 free ports and publish them to the host (loopback only).
lmer chat <repo> --ports 2

# Pick from a custom pool instead of the default 8800-8899.
lmer chat <repo> --ports 3 --port-pool 9000-9099

# Publish the allocated ports on every host interface, not just loopback —
# so other machines on the LAN can reach a service Claude runs inside.
lmer chat <repo> --ports 2 --port-bind 0.0.0.0

# Same thing via environment variables (the CLI flags take precedence).
LMER_PORT_COUNT=2 LMER_PORT_POOL=9000-9099 LMER_PORT_BIND=0.0.0.0 lmer chat <repo>
```

How it works:

- The host CLI picks `--ports` (or `LMER_PORT_COUNT`) currently-free ports from the pool (`--port-pool` / `LMER_PORT_POOL`, default `8800-8899`) **before** the container starts, and publishes each with `-p <bind>:PORT:PORT` — the same port number inside and out. The bind address comes from `--port-bind` / `LMER_PORT_BIND` (default `127.0.0.1`).
- The chosen ports are exported into the container as `LMER_PORTS`, a comma-separated list (e.g. `LMER_PORTS=8842,8857`). Claude (or anything it spawns) reads that variable to learn which ports it may use.
- Because allocation happens on the host before start, running multiple `lmer ... --ports N` sessions in parallel works out of the box — each picks a disjoint set of free ports from the pool. The free-port probe runs on the configured bind address so the picked ports are guaranteed bindable there.
- By default the ports publish to `127.0.0.1` only, so a service is reachable from your local machine but not network-exposed. Pass `--port-bind 0.0.0.0` (or a specific interface IP) to expose them more widely — only do this when you trust the network and what the agent is running. Services Claude starts must bind to `0.0.0.0` inside the container (not `127.0.0.1`) for the published mapping to reach them, regardless of `--port-bind`.
- If the pool doesn't have enough free ports to satisfy the request, startup aborts with a clear error rather than starting with fewer ports than asked for.

The default pool (`8800-8899`) is deliberately distinct from the FastAPI range (`8700-8799`), so `--ports` and `--fastapi` can be combined in one session without colliding.

### Troubleshooting

#### Troubleshooting: containers hit the PID cap (cgroup-v1 pids leak)

**Symptom.** During a long-running session the in-container shell suddenly can't fork: every shell-out fails with `Resource temporarily unavailable` (`EAGAIN`) at `fork()`. `bash`/`echo`/`pwd` return exit code 1 with no output, thread-spawning programs panic, and the failure is silent until it happens — actual process activity inside the container is normal.

**Cause.** A kernel-level counter-accounting bug in the **cgroup v1 pids controller** on older kernels (notably the RHEL 8 / `4.18.0-*.el8` line). Every `fork()` charges the cgroup's pids counter; the counter is supposed to decrement when the task exits, but on these kernels a fraction of short-lived child exits are never refunded. LMER sessions fork-exec heavily (every `gate-check`/`gate-commit` runs pytest and pre-commit, plus per-task `git`/`gh`/`jq`/`uv`/`ruff` shell-outs), so phantom entries accumulate. When phantom + real reaches the container's `--pids-limit`, the kernel rejects every further `fork()` even though the real process count is tiny.

You can confirm it from inside or outside the container:

```bash
cat /sys/fs/cgroup/pids/pids.current   # e.g. 511
cat /sys/fs/cgroup/pids/pids.max       # e.g. 512  -> at the cap
docker top <container-id> -ef          # but only a handful of real processes
```

A large gap between `pids.current` and the real process count is phantom accumulation.

**This is a host-kernel issue, not a container-image one.** Containers share the host's kernel — the image LMER builds has no kernel of its own — so both the buggy accounting and the `--pids-limit` enforcement live in the **host** kernel's cgroup controller. That is why the permanent fix (below) is a host action, and why LMER's lever is the launch-time cap.

**Immediate recovery (no restart, preserves in-flight work).** Bump the live container's cap; the very next `fork()` succeeds and the session resumes:

```bash
CID=<container-id>
sudo sh -c "echo 4096 > /sys/fs/cgroup/pids/docker/$CID/pids.max"
```

**Mitigation (LMER-side, out of the box).** Raise the cap LMER launches with via `LMER_PIDS_LIMIT` so new sessions start with more headroom — for example in your `.env`:

```bash
LMER_PIDS_LIMIT=4096   # or -1 for unlimited on badly-leaking hosts
```

This raises the bound but does not fix the leak itself — a high-enough fork rate over a long-enough session can still reach any finite cap. `-1` removes the cap entirely (at the cost of losing the fork-bomb safety bound).

**Permanent remedy (host-side).** The cgroup-v1 pids-controller leak is fixed in newer kernels, and the controller is rewritten in **cgroup v2**, where the bug does not occur. Either eliminates the need for the workaround:

- **Upgrade the host kernel** to a patchlevel where the leak is fixed.
- **Switch the host to cgroup v2** (RHEL 8 supports it but defaults to v1): boot with `systemd.unified_cgroup_hierarchy=1` and reboot. Coordinate this — it affects every container on the host.

Both are host-infrastructure changes (they require a reboot and validation against other workloads on the box) and cannot be shipped by LMER itself. Until one is in place, `LMER_PIDS_LIMIT` plus the live-bump recovery above keep sessions working regardless of host kernel.
