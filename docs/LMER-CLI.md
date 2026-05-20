## LMER Python CLI (lmer)

Python-first CLI for running LMER with a repository target, cloning inside the container, and optional persistent workspaces.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Install Globally](#install-globally)
- [Environment Variables](#environment-variables)
- [Tasks](#tasks)
- [Basic Usage](#basic-usage)
  - [Starting Your Task](#starting-your-task)
- [Building the Container Image](#building-the-container-image)
- [Command-Line Options](#command-line-options)

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

- **`LMER_REGISTRY`** - Container registry to pull pre-built images from. Optional; defaults to `ghcr.io/lmer2/lmer` (the project's GHCR registry). Override to point at a self-hosted or mirrored registry. Empty-string values are treated the same as unset and fall back to the default.

- **`LMER_NO_AUTO_BUILD`** - Disable automatic container image building. Accepted truthy values: `1`, `true`, `yes` (case-insensitive). When enabled, LMER will error if the image is not found locally instead of building it.

- **`REPO_AUTH_PREFER_SSH`** - When set to a truthy value (`1`, `true`, `yes`), LMER will use SSH URLs for git operations instead of converting them to HTTPS with token authentication.

- **`LMER_REASONING_EFFORT`** - Override Claude's reasoning effort for the session. Accepted values: `low`, `medium`, `high`, `max`, `auto` (case-insensitive). When set to one of `low`/`medium`/`high`/`max`, LMER passes `--effort <level>` to the `claude` CLI. When unset or set to `auto`, no flag is passed and Claude uses its own default. Invalid values are ignored with a warning.

- **`LMER_HUMAN_IDENTITY`** - Free-form string identifying the human user the session is collaborating with (e.g. `"Jane Doe <jdoe on example.com, jane@example.com>"`). When set, it is forwarded into the container and injected into Claude's system prompt so the model can attribute matching usernames, emails, or handles in PRs, MRs, issues, comments, and commit history to the user. When unset, LMER falls back to the host's `git config user.name` and `user.email` (respecting system, global, and local config). If neither is available, no identity is injected. The injected text is rendered from the Jinja2 template at `prompts/human-identity.md.jinja2` — edit that file to change the wording.

- **`LMER_QUICK_GATE_COMMIT`** - When set to a truthy value (`1`, `true`, `yes`, case-insensitive), `gate-commit` skips the test suite (the slowest check) but still runs pre-commit hooks, secret scans, and every other check. Tests are still enforced by standalone `gate-check` and by `gate-push`, so coverage is preserved before code leaves the local repo. Only `gate-commit` reads this variable; `gate-check` and `gate-push` ignore it. Falsy values (`0`, `false`, `no`) and unset both leave tests running, so this can be a transient export that you turn off without `unset`. Useful for iterative commits on a feature branch where you'll run `gate-push` (which runs the suite) before code leaves the repo.

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
- `--fastapi` - Expose a FastAPI endpoint inside the container that lets a controlling process drive Claude's stdin/stdout (`POST /input`, `GET /output`). The chosen port is published to `127.0.0.1` on the host. See [Supervisor and FastAPI Endpoint](#supervisor-and-fastapi-endpoint)
- `--fastapi-port-range LOW-HIGH` - Port range to pick a free FastAPI port from (default `8700-8799`). The host CLI picks one free port from this range before container start and publishes only that port
- `--fastapi-host <host>` - Inside-container bind host for the FastAPI endpoint (default `0.0.0.0` when `--fastapi` is set so the published port works)
- `--fastapi-token <token>` - Bearer token to require on FastAPI requests. If omitted a random token is generated and printed to stderr on startup

**Note**: `--workspace-volume` and `--workspace-bind` options are currently not functional. The workspace uses the `/workspace` directory from the container image instead.

### Supervisor and FastAPI Endpoint

Claude is launched through `lmer-supervisor`, a Python process that sits between your terminal and the Claude CLI. It allocates a PTY and forwards keystrokes/output transparently. The supervisor can also expose a FastAPI control plane.

**Auto `/start`** — by default `/start` is typed into Claude after a short delay so an lmer task begins without manual intervention. Disable with `--manual-start` (or `LMER_MANUAL_START=1`) when you want to drive Claude yourself.

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
