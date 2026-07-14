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

- **`LMER_NAPKIN_REPO`** - Optional Git URL (SSH or HTTPS) of a dedicated *napkin* repo for shared team working notes. When set, lmer clones it to `/napkin` inside the container and points `LMER_NAPKIN_PATH` there. When unset, napkin falls back to a `napkin/` subdir of the work repo. **Host-side only:** the URL is credentialed on the host and the resulting URL is forwarded into the container; the variable name itself is not consumed inside the container.

- **`LMER_NAPKIN_TOKEN`** - Optional auth token for `LMER_NAPKIN_REPO`. **Consumed host-side only** (highest-priority lookup for the napkin URL), baked into the URL as `https://oauth2:<token>@…`, and **never forwarded into the container**. Falls back to the standard per-host `GITLAB_TOKEN_<host>` / `GH_TOKEN` lookups when unset.

- **`LMER_NAPKIN_PATH`** - **Computed and injected by lmer** (not a host input): the in-container path agents write napkin notes to. `/napkin` in separate-repo mode, else `{LMER_WORK_REPO_PATH}/napkin`. Agents and company-level Claude config should always reference `$LMER_NAPKIN_PATH` (and `~/napkin`, which is symlinked to it). Because it is computed rather than read from the host environment, it does not appear in `lmer --show-env` unless also set as a host `LMER_` variable.

- **`LMER_TASKDEF_REPO`** - Optional Git URL (SSH or HTTPS) of a shared taskdef repo. When set, lmer clones it to `/taskdef` inside the container and inserts it into the task-definition search order **between** the work-repo taskdefs and the lmer built-in (i.e. after `{work_repo}/taskdef/`, before `/Agents/global/taskdef`). **Host-side only**, credentialed like `LMER_NAPKIN_REPO`.

- **`LMER_TASKDEF_TOKEN`** - Optional auth token for `LMER_TASKDEF_REPO`. **Consumed host-side only**, baked into the URL, and **never forwarded into the container**. Falls back to per-host `GITLAB_TOKEN_<host>` / `GH_TOKEN` lookups when unset.

- **`LMER_TASKDEF_REF`** - Optional git ref/branch/tag to pin the `LMER_TASKDEF_REPO` clone for reproducibility. Forwarded into the container and passed to the clone checkout. When unset, the repo's default branch is used.

- **`LMER_RENDER_SOURCE`** - Test/CI-only switch for the render-matrix suite (`tests/test_taskdef_render_matrix.py`), not read by the CLI or the container runtime. When set, every taskdef directory found under the given path is rendered — honoring its root `taskdef.yaml` — against the *current checkout's* built-in base templates: `LMER_RENDER_SOURCE=<clone> uv run pytest tests/test_taskdef_render_matrix.py -q`. This is the contract an external taskdef content repo's CI uses to prove its bodies render against a pinned base (see docs/TASKDEFS.md).

- **`GITLAB_TOKEN`** / **`GITLAB_TOKEN_<sanitized_host>`** - Per-host or generic API token used to authenticate against GitLab hosts (also used by the legacy URL-token-injection path for target repos). Hostname suffix is lowercased with dots/hyphens replaced by underscores — e.g. `git.example.com` → `GITLAB_TOKEN_git_example_com`.

- **`GH_TOKEN`** / **`GITHUB_TOKEN`** - Tokens used for GitHub hosts (`github.com`, `*.github.com`, `*.ghe.com`). `GH_TOKEN` takes priority over `GITHUB_TOKEN`. Either is consulted only after a more-specific per-host `GITLAB_TOKEN_<host>` is checked.

- **`LMER_REGISTRY`** - Container registry to pull pre-built images from. Optional; defaults to `ghcr.io/lmer2/lmer` (the project's GHCR registry). Override to point at a self-hosted or mirrored registry. Empty-string values are treated the same as unset and fall back to the default.

- **`LMER_NO_AUTO_BUILD`** - Disable automatic container image building. Accepted truthy values: `1`, `true`, `yes` (case-insensitive). When enabled, LMER will error if the image is not found locally instead of building it.

- **`REPO_AUTH_PREFER_SSH`** - When set to a truthy value (`1`, `true`, `yes`), LMER will use SSH URLs for git operations instead of converting them to HTTPS with token authentication.

- **`LMER_REASONING_EFFORT`** - Override Claude's reasoning effort for the session. Accepted values: `low`, `medium`, `high`, `xhigh`, `max`, `auto` (case-insensitive). When set to one of `low`/`medium`/`high`/`xhigh`/`max`, LMER passes `--effort <level>` to the `claude` CLI. When unset or set to `auto`, no flag is passed and Claude uses its own default. Invalid values are ignored with a warning. The vocabulary matches the per-lane dispatch efforts (`LMER_DISPATCH_<LANE>`, below), so the session and lane surfaces accept the same set.

- **`LMER_LLM_NAME`** - Start Claude with a specific model for the session. The value is passed verbatim to the `claude` CLI as `--model <value>` — model aliases (`sonnet`, `opus`, `haiku`) and full model IDs (e.g. `claude-sonnet-4-6`) both work; `claude` itself rejects unknown models, so LMER performs no validation of its own. When unset or empty, no flag is passed and Claude Code uses its default model. Forwarded into the container by the host CLI and applied by `libexec/claude-runner.sh`.

- **`LMER_DISPATCH_REVIEW`** / **`LMER_DISPATCH_DESIGN`** / **`LMER_DISPATCH_CODE`** / **`LMER_DISPATCH_MECHANICAL`** / **`LMER_DISPATCH_EXPLORE`** - Per-lane model+effort dispatch for Claude **subagent definitions**. Each variable assigns a model (and optionally an effort) to one dispatch lane — the shipped agent def that handles that kind of work: `REVIEW` → `adversarial-reviewer`, `DESIGN` → `designer`, `CODE` → `coder`, `MECHANICAL` → `mechanical`, `EXPLORE` → `explorer` (all under `agent-files/claude/agents/`). Value format: **`<model>[:<effort>]`** — e.g. `LMER_DISPATCH_REVIEW=fable:high`, `LMER_DISPATCH_MECHANICAL=haiku`. The model is a Claude alias (`haiku`, `sonnet`, `opus`, `fable`), a full model ID, or `inherit`; it is passed through verbatim (no allowlist — claude itself rejects unknown models, the `LMER_LLM_NAME` philosophy). The effort, when given after the last colon, must be one of `low`/`medium`/`high`/`xhigh`/`max` (case-insensitive). Parsing rules: surrounding whitespace is trimmed; an empty value counts as unset; the value is split on the **last** colon only when the suffix is a valid effort token, so colon-bearing model IDs (e.g. Bedrock-style `…-v1:0`) pass through intact — an invalid suffix warns and the whole value is used as the model. At session start (`libexec/claude-agent-files.sh` → `lmer_cli.container.dispatch_agents`) a configured lane's agent symlink under `~/.claude/agents/` is replaced by a copy whose frontmatter carries the configured `model:`/`effort:`; an **unset lane keeps today's behavior** — the agent def is linked as-is and inherits the session model. There are no built-in per-lane defaults. Suggested operator settings: `REVIEW=fable:high`, `DESIGN=fable:xhigh`, `CODE=sonnet:high`, `MECHANICAL=haiku`, `EXPLORE=sonnet:low`. **Behavior change note:** `explorer.md` previously hard-pinned `model: sonnet`; that pin is removed, so with `LMER_DISPATCH_EXPLORE` unset the explorer now inherits the session model — set `LMER_DISPATCH_EXPLORE=sonnet` to restore the old behavior. All five variables are forwarded into the container by the host CLI and layer through `.env` files as usual (global defaults in `~/.lmer/.env`, per-project overrides in the project's `.env`).

- **`CLAUDE_AFK_TIMEOUT_MS`** - Enables (and sets, in milliseconds) Claude Code's **AskUserQuestion AFK auto-timeout**: when set, a question the human doesn't answer resolves automatically after the timeout instead of blocking the session forever. This is Claude Code's own variable, not an lmer setting, so it keeps its name (no `LMER_` prefix); lmer only forwards it — both host-exported values and values from a `.env` file reach the container. When unset, lmer applies a default of `300000` (5 minutes) for **Slack-bridged sessions** (any session with a Slack thread target), so an unanswered question times out and the session pings the thread rather than sitting silent; plain terminal sessions get no default and behave as stock Claude Code. `300000` is also the recommended value for other unattended deployments. See also `CLAUDE_AFK_COUNTDOWN_MS` (the on-screen countdown Claude Code shows before the timeout fires) — it can be set the same way via `.env`, but lmer never defaults it.

- **`LMER_HUMAN_IDENTITY`** - Free-form string identifying the human user the session is collaborating with (e.g. `"Jane Doe <jdoe on example.com, jane@example.com>"`). When set, it is forwarded into the container and injected into Claude's system prompt so the model can attribute matching usernames, emails, or handles in PRs, MRs, issues, comments, and commit history to the user. When unset, LMER falls back to the host's `git config user.name` and `user.email` (respecting system, global, and local config). If neither is available, no identity is injected. The injected text is rendered from the Jinja2 template at `prompts/human-identity.md.jinja2` — edit that file to change the wording.

- **`LMER_GIT_USER_NAME`** / **`LMER_GIT_USER_EMAIL`** - Override the git identity that commits made inside the container are authored and committed under. By default the container commits under the `~/.gitconfig` it inherits (a one-time copy of the host's, persisted in the container-home and bind-mounted). When either variable is set, the entrypoint exports it as git's native `GIT_AUTHOR_NAME`/`GIT_COMMITTER_NAME` (from `LMER_GIT_USER_NAME`) and/or `GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_EMAIL` (from `LMER_GIT_USER_EMAIL`). The two are independent — set only one and the other half falls back to gitconfig. The override is session-scoped and writes no file: the mounted `~/.gitconfig` is left untouched, so unsetting the variable fully reverts the behavior (no persistent state is mutated). Note that this affects commit authorship, not `git config --get user.name`/`user.email` reads, which still report the gitconfig values. This is distinct from `LMER_HUMAN_IDENTITY`, which controls who Claude attributes repository artifacts to in its system prompt and does not change commit authorship.

- **`LMER_QUICK_GATE_COMMIT`** - When set to a truthy value (`1`, `true`, `yes`, case-insensitive), `gate-commit` skips the test suite (the slowest check) but still runs pre-commit hooks, secret scans, and every other check. Tests are still enforced by standalone `gate-check` and by `gate-push`, so coverage is preserved before code leaves the local repo. Only `gate-commit` reads this variable; `gate-check` and `gate-push` ignore it. Falsy values (`0`, `false`, `no`) and unset both leave tests running, so this can be a transient export that you turn off without `unset`. Useful for iterative commits on a feature branch where you'll run `gate-push` (which runs the suite) before code leaves the repo.

- **`LMER_PERSIST_AGENT_MEMORY`** - When set to a truthy value (`1`, `true`, `yes`, case-insensitive), Claude Code's agent memory is persisted to the work repo on a **per-project** basis, stored under `{host}/{project}/memory/` (shared across all task types and targets for that project). Restore is automatic: at session start `libexec/claude-runner.sh` runs `work memory restore`, which copies any saved memory from the work repo into Claude's memory directory (`~/.claude/projects/-workspace/memory/`) before Claude reads it. Persisting back is the agent's responsibility — the agent runs `work memory persist` (which copies the memory directory into the work repo and commits and pushes it). Falsy values (`0`, `false`, `no`) and unset both disable the feature, in which case both `work memory` subcommands are no-ops. Parsed via `get_bool_env` and forwarded into the container by the host CLI. The memory directory path can be overridden for non-standard layouts/tests with `LMER_AGENT_MEMORY_DIR`.

- **`LMER_RUN_STATE_GUARD`** - Kill switch for the **run-state compliance Stop hook** (`hooks/run_state_guard.py`), which blocks a stop while the run state is missing its phase, goal, or name — or the run dir carries uncommitted/unpushed changes, or the session has landed gate-commits without recording any execution-ledger row (`work ledger set`) — and replies with the exact `work` commands to fix it. Parsed via `get_bool_env`: unset or truthy (`1`, `true`, `yes`, case-insensitive) leaves the guard **enabled** (the default); set `LMER_RUN_STATE_GUARD=0` (or `false`/`no`) to disable it entirely. The guard is fail-open — any error in its own checks lets the stop proceed — so the kill switch exists to opt out of the nudges, not to work around hook failures. Forwarded into the container by the host CLI.

- **`LMER_MASTERPLAN`** - When set to a truthy value (`1`, `true`, `yes`, case-insensitive; parsed via `get_bool_env`), the session runs the **masterplan** workflow (brainstorm → plan → execute → finish, on top of superpowers). Setting `LMER_TASK=masterplan` implies it, so this toggle is mainly for enabling masterplan on top of another task. When active, `libexec/claude-runner.sh` at session start installs the masterplan plugin from the work-repo mirror (`claude plugin marketplace add /work/mirrors/masterplan` → `plugin install masterplan@rasatpetabit-masterplan` → `plugin enable masterplan`) and exports `MASTERPLAN_RUNS_DIR` (see below). Every step is idempotent and non-fatal — provisioning failures warn and continue rather than aborting the session. superpowers is baked into the image but left disabled (the image bake removes `~/.claude/settings.json` so no `enabledPlugins` entry survives, keeping the runtime global-settings symlink intact) and is re-enabled automatically as masterplan's declared dependency when the plugin installs, so plain sessions pay no runtime cost. Because the runtime `settings.json` starts as a read-only symlink, the provisioning step materializes it into a writable regular file before the `claude plugin` calls; the path is overridable for non-standard layouts/tests with `LMER_SETTINGS_FILE`. Falsy values (`0`, `false`, `no`) and unset both disable the feature. Forwarded into the container by the host CLI.

- **`MASTERPLAN_RUNS_DIR`** - Set **by lmer inside the container** for masterplan sessions (not a host input): the bundle root masterplan writes to, computed as `<current-run-dir>/masterplan` where the run dir comes from the run-state kernel (`work_repo.run_state.run_dir()`). This nests masterplan's bundles (`<mp-slug>/` each with its own `state.yml`/`events.jsonl`) inside the lmer run directory alongside the run's own `state.yaml`, so masterplan artifacts are captured with the rest of the run. It is not LMER-prefixed because it is masterplan's own configuration variable (honored by masterplan's `lib/paths.mjs`); lmer only computes and exports it. Only set when the run dir is resolvable (`LMER_REPO_HOST`/`LMER_REPO_PROJECT` present); otherwise provisioning is skipped.

- **`LMER_MOUNT_FILES`** - Comma-separated list of explicit per-file mounts, each entry using the `--mount-file` grammar `host:container[:mode]` (comma is the entry separator so it does not collide with the `:` field separator, cf. `LMER_TASKDEF_PATHS` choosing its own separator for the same reason). `host` gets `~`/`$VAR` expansion and must resolve to an **existing file**; `container` must be an **absolute** path; `mode` is `ro` (default) or `rw`. Env entries are applied **before** `--mount-file` flags, so a persistent `.env` sets a baseline an ad-hoc flag can override; when two entries target the same container destination, last wins and a warning is emitted. Any invalid entry **aborts the run** — fail-fast is deliberate and stricter than the skip-and-warn mount builders (external taskdefs, uv cache), because these entries are typically credentials (e.g. a kubeconfig) and silently launching without one only surfaces as confusing downstream auth failures. Files are bind-mounted as-is, never copied; SELinux labeling is applied automatically when enforcing. This is a **host-side** variable read by the launching CLI; it does not need to reach inside the container.

- **`LMER_MOUNT_UV_CACHE`** - When set to a truthy value (`1`, `true`, `yes`, case-insensitive), the host's `uv` cache directory is bind-mounted read-write into the container at `/home/developer/.cache/uv`, so `uv sync` / `uv pip install` invocations in the target repo reuse already-downloaded wheels instead of re-fetching them every session. The host path is resolved using uv's own rules: `$UV_CACHE_DIR` first, then `$XDG_CACHE_HOME/uv`, then `~/.cache/uv` on Linux or `~/Library/Caches/uv` on macOS. Falsy values (`0`, `false`, `no`) and unset both disable the mount. If the host cache directory doesn't exist, lmer prints an info message and continues without the mount. Parsed via `get_bool_env`. This is a **host-side** variable read by the launching CLI; it does not need to reach inside the container.

- **`LMER_PIDS_LIMIT`** - Overrides the container PID cap that LMER passes as `--pids-limit` to `docker`/`podman run` (default `512`). Accepts any **positive integer**, or **`-1`** for "unlimited" (Docker/Podman semantics). Any other value — `0`, other negatives, or non-numeric — is rejected with a warning and falls back to `512`, so a misconfigured *value* can never silently weaken the fork-bomb safety bound. This is a **host-side** variable read by the launching CLI; it does not need to reach inside the container. Raise it (or set `-1`) on hosts affected by the cgroup-v1 pids-controller counter leak, where phantom fork entries accumulate over a long session and prematurely exhaust the cap — see [Troubleshooting: containers hit the PID cap](#troubleshooting-containers-hit-the-pid-cap-cgroup-v1-pids-leak). **Rootless-podman caveat:** on cgroup-v2 hosts where systemd has not delegated a controller to `user@<uid>.service` (Fedora/RHEL default delegates only a subset), the corresponding resource flag — including `--pids-limit` when the `pids` controller isn't delegated — is **dropped entirely** rather than passed (crun would otherwise abort or hang the session). lmer warns loudly and points at the fix: create `/etc/systemd/system/user@.service.d/delegate.conf` with `[Service]` / `Delegate=cpu cpuset io memory pids`, then `sudo systemctl daemon-reload`. Root podman, docker, and hosts where the user-slice controllers file is missing (e.g. cgroup v1) are not gated — they always receive the flags.

- **`LMER_PORT_COUNT`** - Number of host ports to allocate from the pool (see `LMER_PORT_POOL`) and publish into the container, so a service Claude starts inside (e.g. a dev web server) is reachable from the host. Equivalent to the `--ports` flag, which takes precedence when both are set. Must be a non-negative integer; `0` (or unset) disables port passthrough. A non-numeric value aborts startup with an error. The allocated ports are exported to the container as `LMER_PORTS`. Read by the host CLI only.

- **`LMER_PORT_POOL`** - Inclusive port range `LOW-HIGH` the `--ports`/`LMER_PORT_COUNT` ports are picked from (default `8800-8899`, kept distinct from the FastAPI range `8700-8799` so both features can be used together). Equivalent to the `--port-pool` flag, which takes precedence. The host CLI picks the requested number of currently-free ports from this pool before the container starts, so multiple `lmer` instances on the same host get disjoint ports without manual coordination. If fewer than the requested number of free ports are available, startup aborts with an error. Read by the host CLI only.

- **`LMER_PORTS`** - Set **by lmer inside the container** (not a host input): a comma-separated list of the ports allocated via `--ports`/`LMER_PORT_COUNT` (e.g. `8842,8857`). Each is published on the host (loopback `127.0.0.1` by default, overridable via `LMER_PORT_BIND`) with the same port number inside and out. Services Claude starts should bind to `0.0.0.0` on one of these ports to be reachable from the host. Empty/unset when no ports were requested.

- **`LMER_PORT_BIND`** - Host bind address used when publishing `--ports`/`LMER_PORT_COUNT` mappings (default `127.0.0.1`). Equivalent to the `--port-bind` flag, which takes precedence when both are set. Set to `0.0.0.0` to expose the allocated ports on every host interface (so other machines on the LAN can reach a service Claude starts inside the container), or to a specific IP (e.g. `192.168.1.42`) to publish only on that interface. The value is also used to probe for free ports in the pool, so the chosen ports are guaranteed bindable on that address. **Security note:** the default is loopback for a reason — opening published ports to the network exposes any service Claude binds inside the container; only widen the bind when you trust both the network and what the agent is running. Read by the host CLI only.

- **`LMER_AUTO_START_DELAY`** - Seconds the supervisor waits before injecting the initial `/start` into Claude (the marker-based prompt-ready wait, see `LMER_AUTO_START_READY_TIMEOUT`, may extend this). Accepts a float (default `1.5`); negative values are clamped to `0`. Also settable per-invocation with `--auto-start-delay`. Has no effect under `--manual-start`/`LMER_MANUAL_START`. Parsed by `lmer-supervisor` and forwarded into the container by the host CLI.

- **`LMER_AUTO_START_NUDGE_DELAY`** - Seconds between the follow-up carriage-return "nudges" that the supervisor sends after auto-injecting `/start`. The initial Enter is occasionally swallowed during Claude's startup re-render, leaving `/start` typed but unsubmitted; the supervisor sends a few bare CRs afterward to re-trigger submission (each is a harmless no-op once `/start` has gone through). Accepts a float (default `0.5`); negative values are clamped to `0`. Has no effect under `--manual-start`/`LMER_MANUAL_START`. Parsed by `lmer-supervisor` and forwarded into the container by the host CLI.

- **`LMER_AUTO_START_READY_TIMEOUT`** - Maximum seconds the supervisor will wait for Claude's input-prompt glyph (`❯`) to appear in the output stream before auto-injecting `/start`. Claude Code v2.1.119 changed Enter routing so any modal/dialog open during startup (theme picker, IDE detect, permission prompt, etc.) consumes a CR rather than also submitting input-box text; waiting for the prompt glyph lets Claude finish its startup chain before we type. On timeout the injection fires anyway (with the cooked-mode pre-clear + CR nudges still providing best-effort delivery). Accepts a float (default `15.0`); set to `0` to disable marker-based readiness and inject purely on the initial delay. Has no effect under `--manual-start`/`LMER_MANUAL_START`. Parsed by `lmer-supervisor` and forwarded into the container by the host CLI.

- **`LMER_AUTO_START_READY_MARKER`** - UTF-8 string the supervisor scans for in Claude's output to decide that the input prompt has rendered (default `❯` — U+276F). The marker is a heuristic: `❯` is also used as a row-selection indicator in some TUI pickers, so a future Claude UI change could shift the right marker. Override here lets you patch in a more specific string (e.g. a unique-to-input-box sequence) without an lmer release. Set to the empty string to disable marker gating entirely (equivalent to `LMER_AUTO_START_READY_TIMEOUT=0`). Has no effect under `--manual-start`/`LMER_MANUAL_START`. Forwarded into the container by the host CLI.

- **`LMER_START_PROMPT`** - Follow-up prompt the supervisor types and submits a short, configurable delay after auto-injecting `/start` (see `LMER_START_PROMPT_DELAY`), so an automated run can hand Claude an extra instruction without manual typing (e.g. `"make sure to research X online first"`). Claude queues input typed while it is still working on `/start`, so the prompt becomes the next conversation turn. Its submit Enter gets the same bare-CR nudge re-submission as `/start` (governed by `LMER_AUTO_START_NUDGE_DELAY`) in case the initial CR is swallowed. Set on the host via the `--prompt` CLI flag (which populates this var); an empty or unset value means no follow-up. Tied to auto-start, so it is a **no-op under `--manual-start`/`LMER_MANUAL_START`** (nothing is auto-injected then). Forwarded into the container by the host CLI.

- **`LMER_START_PROMPT_DELAY`** - Seconds the supervisor waits between submitting the auto-`/start` and injecting the follow-up `LMER_START_PROMPT`. The delay lets `/start` register as a slash command first; without it, on a slow system the prompt text is typed before `/start` has been recognized and both land on the same input line (`/start <prompt text>`) instead of as separate turns. Accepts a float (default `2.0`); negative values are clamped to `0`. Has no effect when no follow-up prompt is set, and (like the prompt itself) is a no-op under `--manual-start`/`LMER_MANUAL_START`. Also settable per-invocation on the supervisor with `--start-prompt-delay`. Parsed by `lmer-supervisor` and forwarded into the container by the host CLI.

- **`SLACK_BOT_TOKEN`** - Slack Bot User OAuth Token (`xoxb-...`) used by the Slack-thread chat integration. **Required** whenever a Slack thread permalink is passed as a target to `lmer chat` — lmer fails fast at startup if it is missing. Typically set in your `.env` file. Forwarded into the container, where the `lmer-slack` CLI uses it (Bearer auth) for all Slack Web API calls. See [Slack Thread Chat](#slack-thread-chat) for how to create the Slack app and obtain the token.

- **`SLACK_APP_TOKEN`** - Slack App-Level Token (`xapp-...`, scope `connections:write`). **Required by `lmer-slack-listener`** — the host-side listener uses it to open the socket-mode connection. It is also forwarded into the container when set alongside a Slack target, but is not consumed by the in-container thread-chat flow itself (only `SLACK_BOT_TOKEN` is needed there). See [Spawning sessions automatically (`lmer-slack-listener`)](#spawning-sessions-automatically-lmer-slack-listener).

- **`LMER_SLACK_CHAT_IDLE_TIMEOUT_MINUTES`** - Read **host-side by `lmer-slack-listener`**: minutes of total thread silence (no human or agent messages) before a spawned session is disconnected and a reconnect hint is posted (default `300`, i.e. 5 hours). Invalid values fall back to the default.

- **`LMER_SLACK_CHAT_MAX_SESSIONS`** - Read **host-side by `lmer-slack-listener`**: maximum number of concurrently running `lmer chat` sessions (default `5`). When the limit is reached the listener replies that all session slots are busy instead of spawning another.

- **`LMER_SLACK_CHAT_BIN`** - Read **host-side by `lmer-slack-listener`**: the executable used to spawn sessions (default `lmer`). Set this to an absolute path when `lmer` is not on the listener process's `PATH`.

- **`LMER_SLACK_CHAT_CWD`** - Read **host-side by `lmer-slack-listener`**: working directory for spawned `lmer chat` processes (default `/tmp/lmer-slack-chat-sessions`). Must **not** be a git checkout — lmer would infer a repo target from it, but chat sessions start repo-less so the agent can resolve and clone a workspace from the conversation.

- **`LMER_SLACK_CHAT_LOG_DIR`** - Read **host-side by `lmer-slack-listener`**: directory for per-session PTY transcripts, one file per `(channel, thread_ts)` (default `/tmp/lmer-slack-chat-sessions/logs`).

- **`LMER_SLACK_CHAT_ENV_FILE`** - Read **host-side by `lmer-slack-listener`**: path to a `.env` file forwarded to each spawned `lmer chat` as `lmer --env-file <path>`. The listener spawns sessions from a scratch working directory with no `.env` of its own, so variables that live only in its deployment `.env` (git tokens like `GITLAB_TOKEN_<host>`, `LMER_*` settings, ...) would otherwise be dropped at the container boundary (issue #75); pointing this at that `.env` carries them into the chat container. Overridden by the `--lmer-env-file` flag; unset (and no flag) means no `--env-file` is passed and spawning is unchanged.

- **`LMER_SLACK_DM_ALLOWED_USERS`** - Read **host-side by `lmer-slack-listener`**: comma-separated Slack user IDs allowed to hold conversational DM sessions. Unset (the default) means DMs are open to everyone; when set, a DM from anyone not on the list is silently ignored. Channel mentions are never gated by this list.

- **`LMER_SLACK_LOG_LEVEL`** - Read **host-side by `lmer-slack-listener`**: Python logging level for the listener (default `INFO`). Overridden by the `--log-level` flag.

- **`LMER_SLACK_CHANNEL`** / **`LMER_SLACK_THREAD_TS`** - Set **by lmer inside the container** (not host inputs): the channel ID and thread timestamp parsed from the first Slack thread permalink target given to `lmer chat`. Their presence switches the `chat` taskdef into Slack conversation mode and supplies the default channel/thread for `lmer-slack` invocations (overridable per-invocation with `--permalink`). Empty/unset when no Slack target was given.

- **`LMER_SLACK_PERMALINK`** - Also set **by lmer inside the container**: the original Slack thread permalink URL the channel/thread values were derived from, kept for reference and diagnostics.

- **`LMER_SUPERVISOR_PID`** - Set **by `lmer-supervisor` inside the container** (not a host input): the supervisor's own PID, exported before it forks Claude so the wrapped process and everything it spawns inherit it. An in-container command can send this PID `SIGUSR1` to request a graceful self-shutdown — the supervisor injects Claude's quit chord (Ctrl-C twice), escalating to SIGTERM/SIGKILL if needed, and reports a clean exit. `lmer-slack end-session` uses this to let a Slack chat session free its orchestrator slot on demand. Unset when Claude runs without the supervisor (`LMER_DISABLE_SUPERVISOR=1`).

- **`LMER_NO_REPO`** - Set **by lmer inside the container** (not a host input) to `1` when the session deliberately has no repository — currently only when a Slack thread permalink is the sole `lmer chat` target and no git origin could be inferred from the current directory. The container's clone step is skipped (`/workspace` stays empty) and the chat taskdef drops its repository-specific instructions. Unset for all repository-backed sessions.

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
- PR/MR/issue URLs (e.g., `https://github.com/org/repo/pull/123`) are automatically converted to their base repository URLs. Newer GitLab installs that serve issues under `/-/work_items/<id>` are also recognized and treated the same as the older `/-/issues/<id>` form.

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

### Slack Thread Chat

`lmer chat` can conduct the conversation over a Slack thread instead of the interactive terminal. Pass a Slack thread permalink as an additional target:

```bash
lmer chat https://github.com/org/repo.git "https://myworkspace.slack.com/archives/C0123456789/p1700000000123456"
```

The repository target is optional — a Slack thread permalink can be the only target:

```bash
lmer chat "https://myworkspace.slack.com/archives/C0123456789/p1700000000123456"
```

When no repository target is given, lmer first tries to infer a repo from the current directory's git origin (as usual); if the current directory is not a git repository, the session starts without a repository — the container skips the workspace clone (signalled via `LMER_NO_REPO=1`) and the chat task is the Slack conversation itself. This repo-less fallback is supported for the `chat` task only; every other task assumes code in `/workspace` and fails fast with an error if a Slack thread is its sole target outside a git checkout.

To get the permalink, hover the thread's first message in Slack and choose **"Copy link"**.

lmer parses the permalink into a channel ID and thread timestamp, injects them into the container as `LMER_SLACK_CHANNEL` / `LMER_SLACK_THREAD_TS` / `LMER_SLACK_PERMALINK` (along with `SLACK_BOT_TOKEN`), and the chat task instructions switch Claude into a Slack conversation driven by the `lmer-slack` CLI:

- `lmer-slack history` — fetch and print the thread messages, advancing a cursor so already-seen messages are not re-printed
- `lmer-slack post` — post a reply into the thread. Prefer `--message-file PATH` or `--stdin` over a positional `lmer-slack post "<text>"` for any free-form text: a command-line argument is processed by the shell first, so backticks (common in Slack inline code), `$`, and quotes get expanded/mangled before they reach Slack, with no error. `--message-file`/`--stdin` post the body verbatim. (`-` is an alias for `--stdin`.)
- `lmer-slack watch [--out FILE]` — continuously long-poll the thread and stream each new human message as a single JSON line (`{"ts", "user", "text"}`) to stdout, optionally appending the same line to `FILE`. Bot/self/parent messages are filtered out, and a persisted cursor means a restarted watcher resumes instead of replaying. This is the recommended way to wait for replies: point Claude Code's `/monitor` at `lmer-slack watch` so each new message arrives as an event — including mid-task — rather than blocking the agent in a poll loop.
- `lmer-slack poll` — block until a new human message arrives (exit 0) or the timeout elapses (exit 2; exit 1 means a real error). A simple blocking alternative to `watch` when a stream monitor is not available.
- `lmer-slack end-session` — end this chat session when the human signals the conversation is over. It optionally posts a goodbye (same `--message-file` / `--stdin` / positional body rules as `post`; omit it to leave silently), then asks the in-container supervisor (`LMER_SUPERVISOR_PID`) to quit Claude cleanly via `SIGUSR1`. The clean exit makes the `lmer chat` process exit 0, which the host orchestrator's reaper treats as a deliberate sign-off and frees the session slot immediately — instead of holding it until the idle timeout. Exit 0 on success; exit 3 when there is no supervisor to signal (e.g. `LMER_DISABLE_SUPERVISOR=1`), in which case the session ends only on the idle timeout.

In a Slack-bridged session the terminal is a separate channel from the Slack thread: replies the agent composes as ordinary assistant text land in the (normally unattended) terminal, not in Slack, so the human sees nothing. To backstop this, a Claude Code `Stop` hook (`hooks/slack_reply_guard.py`, wired in `agent-files/claude/settings.json`) fires at the end of each turn when `LMER_SLACK_CHANNEL` is set: if the turn produced a substantive reply but made no `lmer-slack post` call, it re-prompts the agent to post it. The check is scoped to text emitted since the last post, so acknowledge-then-work and periodic progress notes do not trip it; it nudges at most once per turn and is a no-op (and fails open) outside Slack mode.

#### Creating the Slack app and obtaining tokens

1. Go to **https://api.slack.com/apps** → **"Create New App"** → **"From scratch"**. Enter a name (e.g. "lmer") and select your workspace.
2. In the left sidebar, go to **"OAuth & Permissions"** → **"Scopes" → "Bot Token Scopes"** and add:
   - `chat:write` — send messages
   - `channels:history` — read public channel messages
   - `groups:history` — read private channel messages (if you want to chat in private channels)
   - `im:history` / `mpim:history` — read DM / group-DM messages (only if you want to use threads there)
   - `app_mentions:read` — receive @bot mentions (**required for `lmer-slack-listener`**; not needed for the direct `lmer chat <permalink>` flow)
3. Go back to **"OAuth & Permissions"**, click **"Install to Workspace"**, and approve. Copy the **"Bot User OAuth Token"** (starts with `xoxb-`) and set it in your `.env` file:

   ```bash
   SLACK_BOT_TOKEN=xoxb-...
   ```

4. *(Optional, required only for `lmer-slack-listener`)* `SLACK_APP_TOKEN` (starts with `xapp-`): under **"Socket Mode"**, enable Socket Mode and generate an App-Level Token with the `connections:write` scope, then set `SLACK_APP_TOKEN=xapp-...` in your `.env`. The direct `lmer chat <permalink>` thread-chat flow does **not** need it; the host-side listener (next section) does.
5. Invite the bot to the channel containing the thread (`/invite @lmer` in the channel). Without membership, Slack API calls fail with `not_in_channel`.

#### Spawning sessions automatically (`lmer-slack-listener`)

The flow above attaches a session to one thread you already have a permalink for. `lmer-slack-listener` automates that: it is a long-lived **host** process that listens for Slack events and spawns an `lmer chat <thread-permalink>` session whenever the bot is mentioned or DMed. The spawned agent joins the thread and does all conversation I/O itself via the in-container `lmer-slack` CLI — the listener never relays conversation content, it only spawns and tracks sessions.

##### Quickstart

Prerequisite: a host where plain `lmer chat <repo>` already works (lmer CLI on `PATH`, a container runtime, model + git auth) and a Slack app with Socket Mode and the scopes/events listed under [Slack app setup](#slack-app-setup-for-the-listener) below.

1. **Install the command on the host** (it runs on the host, so the package must be installed there, not just in the image):

   ```bash
   uv tool install lmer --from git+https://github.com/lmer2/lmer@prep-release
   # or, from a local checkout: uv tool install -e lmer --from .
   lmer-slack-listener --help   # confirm it's on PATH
   ```

2. **Provide the tokens.** The listener reads `.env` the same way the main `lmer` CLI does — the current directory's `.env`, then `~/.lmer/.env`, with already-exported environment variables winning. Put both tokens in whichever you prefer:

   ```bash
   # ~/.lmer/.env  (shared with the rest of lmer) — or a .env in your run dir
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_APP_TOKEN=xapp-...
   ```

   Everything `lmer` itself needs (model auth, `LMER_IMAGE`, `GITLAB_TOKEN`/`GITLAB_TOKEN_<host>`, …) must be visible to the listener — spawned sessions inherit its full environment. Note, though, that inheriting a variable only gets it into the spawned `lmer` **process**; for it to reach **inside the chat container** lmer must forward it, which it does for variables it loads from a `.env` file. The listener spawns from a scratch working directory with no `.env`, so put deployment variables (git tokens, `LMER_*` settings, …) in `~/.lmer/.env` (always loaded) **or** point `--lmer-env-file` / `LMER_SLACK_CHAT_ENV_FILE` at the `.env` that holds them so they are forwarded into each session (issue #75).

3. **Run it** (foreground; `Ctrl-C` stops it and shuts down live sessions gracefully):

   ```bash
   lmer-slack-listener --log-level DEBUG
   ```

4. **Drive it from Slack.** Invite the bot to a channel (`/invite @your-bot`), then `@`-mention it (or DM it). You should see `lmer_session_spawned` in the logs and a *"Connecting a session to this thread… ⏳"* reply; the **first reply takes ~a minute** (container boot). Reply in the thread to continue.

5. **Watch a session** (each gets a PTY transcript):

   ```bash
   tail -f /tmp/lmer-slack-chat-sessions/logs/<channel>-<thread_ts>.log
   ```

While testing, `LMER_SLACK_CHAT_MAX_SESSIONS=2` and a shorter `LMER_SLACK_CHAT_IDLE_TIMEOUT_MINUTES` make limits and idle-disconnect easy to observe.

Behavior:

- **Mention outside a thread** — the mention message becomes a new thread's parent and a session is attached to it. **Mention inside a thread** — a session is attached only if none is already running (a live session sees the message through its own polling). **DMs** — a non-bot DM connects a session the same way; one session runs per DM conversation (a new top-level DM while one is live is pointed back at the active thread).
- Every message in a connected thread — yours or the agent's own posts — resets that session's idle timer. After `LMER_SLACK_CHAT_IDLE_TIMEOUT_MINUTES` of silence the session is disconnected and a reconnect hint is posted; mentioning the bot again spawns a fresh session that reads the thread history. A crashed session posts the same hint; a clean sign-off leaves quietly. When the human signals the conversation is over, the agent can also end the session itself with `lmer-slack end-session` (typically after a goodbye), freeing the slot immediately rather than holding it until the idle timeout.
- At most `LMER_SLACK_CHAT_MAX_SESSIONS` sessions run at once. DM access can be restricted with `LMER_SLACK_DM_ALLOWED_USERS`. See [Environment Variables](#environment-variables) for the full `LMER_SLACK_CHAT_*` / `LMER_SLACK_DM_ALLOWED_USERS` set.

It must run **on a host** (not inside a container): lmer launches a container per session, so the listener has to sit alongside those containers, not within one. Spawned sessions inherit the listener's full environment, so any lmer configuration (`LMER_IMAGE`, git tokens, model API keys, `SLACK_BOT_TOKEN`, ...) in the listener's `.env` reaches the sessions automatically.

##### Slack app setup for the listener

The listener needs more Slack setup than the direct thread-chat flow. In addition to the steps above:

- **Socket Mode** enabled with an `SLACK_APP_TOKEN` (App-Level Token, scope `connections:write`).
- **Bot Token Scopes** (under **"OAuth & Permissions"**): `app_mentions:read` (receive mentions), `chat:write` (post acks and reconnect notices), `channels:history` / `groups:history` (read public / private thread messages), and `im:history` (DM conversations). Re-install the app after adding scopes.
- **Event Subscriptions** (under **"Event Subscriptions" → "Subscribe to bot events"**):
  - `app_mention` — bot mentions that start or reconnect a session
  - `message.im` — DM conversations
  - `message.channels` / `message.groups` — thread activity in public / private channels, which resets the idle timer for connected sessions

The same `SLACK_BOT_TOKEN` is forwarded into each spawned session, so its history scopes also cover the in-container `lmer-slack` reads. The bot must be a member of any channel it is used in (`/invite @your-bot`), or Slack calls fail with `not_in_channel`.

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
- `--mount-file HOST:CONTAINER[:MODE]` - Mount a single host file into the container at an explicit destination (repeatable). `HOST` supports `~`/`$VAR` expansion and must be an existing file; `CONTAINER` must be an absolute path; `MODE` is `ro` (default) or `rw`. Motivating case: `--mount-file ~/.kube/config:/home/developer/.kube/config` puts a kubeconfig where `kubectl` looks by default (no `KUBECONFIG` needed). Merges with `LMER_MOUNT_FILES` (env entries first, flags override on the same destination, last wins with a warning); any invalid entry aborts the run (fail-fast — see the env var entry for the rationale) (env: `LMER_MOUNT_FILES`)
- `--env-file <path>` - Load an additional `.env` file as the **highest-priority** `.env` source (above the working-directory `.env` and `~/.lmer/.env`, which still load; below variables already exported in the environment). Its variables are forwarded into the container like any other `.env`-sourced var. Useful when `lmer` is launched from a directory without the relevant `.env` — e.g. `lmer-slack-listener` spawns `lmer chat` from a scratch cwd and forwards its deployment `.env` this way (see `--lmer-env-file` / `LMER_SLACK_CHAT_ENV_FILE`). A path that does not exist is warned and skipped (non-fatal)
- `--verbose` - Enable verbose output (same as `LMER_VERBOSE=1`)
- `--debug` - Alias for `--verbose` (sets `LMER_VERBOSE=1`)
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

**Follow-up prompt** — pass `--prompt "<text>"` (or set `LMER_START_PROMPT`) to have the supervisor type and submit an extra instruction shortly after the `/start` injection. Claude queues input typed while it is still working on `/start`, so the prompt lands as the next conversation turn — handy for automating `lmer chat <issue-url> --prompt="research X online first"`. The supervisor waits `LMER_START_PROMPT_DELAY` seconds (default `2.0`, also `--start-prompt-delay`) before typing the prompt so `/start` has registered as a slash command first; on a slow system too short a gap makes the prompt land on the same input line as `/start`. Raise it if you still see `/start <prompt>` collapsed onto one line. It is part of the auto-start flow, so it is ignored under `--manual-start` (where nothing is auto-injected).

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
