## LMER Python CLI (lmer)

Python-first CLI for running LMER with a repository target, cloning inside the container, and optional persistent workspaces.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Install Globally](#install-globally)
- [Environment Variables](#environment-variables)
- [Canonical Source Declarations (`sources.yaml`)](#canonical-source-declarations-sourcesyaml)
- [Work-Repo Claude Assets](#work-repo-claude-assets)
- [Tasks](#tasks)
- [Basic Usage](#basic-usage)
  - [Starting Your Task](#starting-your-task)
- [Building the Container Image](#building-the-container-image)
- [Command-Line Options](#command-line-options)
  - [Startup presets (`--preset` / `LMER_<TASK>_PRESET` / `LMER_PRESET`)](#startup-presets---preset--lmer_preset)
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

- **`LMER_WORK_REPO_TOKEN`** - Provider-agnostic token used to authenticate against the work repo. Highest-priority lookup for work-repo clones, so it isolates the work-repo credential from per-host target-repo tokens. Works for GitLab, GitHub, and self-hosted hosts. When set, lmer resolves an `LMER_WORK_REPO=git@host:…` URL to HTTPS authentication; immediately before the container clone, the token moves into a mode-`0600` session credential file and Git receives only the clean HTTPS URL. The legacy `GITLAB_TOKEN_worklog` is still honored as a fallback for existing setups.

- **`LMER_WORK_REPO_PATH`** - In-container clone location of the work repo. Defaults to `/work`; rarely needs overriding.

- **`LMER_WORK_REPO_CREDENTIAL_FILE`** - **Computed and injected inside the container** (not a host input): the mode-`0600` session credential file created for the work-repo clone. `lmer doctor` uses a disposable copy for same-host declared-source probes, so Git cannot alter the live credential on success or authentication failure. The entrypoint discards any inherited value before minting this path; setting it on the host or in `.env` has no effect.

- **`LMER_NAPKIN_REPO`** - Optional Git URL (SSH or HTTPS) of a dedicated *napkin* repo for shared team working notes. When set, lmer clones it to `/napkin` inside the container and points `LMER_NAPKIN_PATH` there. When unset, napkin falls back to the work repo's `sources.napkin` declaration if one exists (see [Canonical Source Declarations](#canonical-source-declarations-sourcesyaml)), else to a `napkin/` subdir of the work repo. This variable is an explicit, **mismatch-checked override** of the declaration, not the only way to point at a napkin source: when a declaration also exists and the two URLs are equal after normalization, the env value wins silently (it supplies the credential the declared form lacks); when they differ, lmer stops and asks interactively which to use, or exits 2 headless before any auxiliary clone. It also remains the operator-controlled escape hatch for a napkin repo on a different host than the work repo, which the same-host trust rule rejects as a declaration. The host resolves its credential; the container clone boundary writes that credential to a session file and retains the clean URL.

- **`LMER_NAPKIN_TOKEN`** - Optional auth token for `LMER_NAPKIN_REPO`. It remains the highest-priority host-side lookup for the napkin URL and falls back to the standard per-host `GITLAB_TOKEN_<host>` / `GH_TOKEN` lookups when unset. Its resolved value lands only in the napkin clone's mode-`0600` container credential file, never in Git argv, its remote URL, or repository config.

- **`LMER_NAPKIN_PATH`** - **Computed and injected by lmer** (not a host input): the in-container path agents write napkin notes to. `/napkin` in separate-repo mode, else `{LMER_WORK_REPO_PATH}/napkin`. Agents and company-level Claude config should always reference `$LMER_NAPKIN_PATH` (and `~/napkin`, which is symlinked to it). Because it is computed rather than read from the host environment, it does not appear in `lmer --show-env` unless also set as a host `LMER_` variable.

- **`LMER_TASKDEF_REPO`** - Optional Git URL (SSH or HTTPS) of a shared taskdef repo. When set, lmer clones it to `/taskdef` inside the container and inserts it into the task-definition search order **between** the work-repo taskdefs and the lmer built-in (i.e. after `{work_repo}/taskdef/`, before `/Agents/global/taskdef`). The taskdef source can equally be declared in the work repo's `sources.yaml` (see [Canonical Source Declarations](#canonical-source-declarations-sourcesyaml)); this variable is then an explicit, **mismatch-checked override** of that declaration rather than the only configuration mechanism. When a `sources.taskdef.repo` declaration also exists: equal after normalization → the env value wins silently, because it supplies the credential for the same source; different → lmer stops and asks interactively which to use, or exits 2 headless before any auxiliary clone. The env var also remains the operator-controlled escape hatch for a taskdef repo on a different host than the work repo, which the same-host trust rule rejects as a declaration. Credential handling matches `LMER_NAPKIN_REPO`: a session file plus a clean retained URL.

- **`LMER_TASKDEF_TOKEN`** - Optional auth token for `LMER_TASKDEF_REPO`. It remains the highest-priority host-side lookup and falls back to per-host `GITLAB_TOKEN_<host>` / `GH_TOKEN` lookups when unset. Its resolved value lands only in the taskdef clone's mode-`0600` container credential file.

- **`LMER_TASKDEF_REF`** - Optional git ref/branch/tag to pin the taskdef clone for reproducibility. Forwarded into the container and passed to the clone checkout. When unset, a `sources.taskdef.ref` declaration in the work repo's `sources.yaml` applies if present (see [Canonical Source Declarations](#canonical-source-declarations-sourcesyaml)), else the repo's default branch is used. Like the repo URL variables, this is an explicit, **mismatch-checked override** of the declared ref — resolution runs per field, so a ref-only mismatch counts: declared ref equal to the env value → the env value wins silently; different → interactive stop-and-ask, or exit 2 headless before any auxiliary clone. And like them, it stays the operator-controlled escape hatch when the declared taskdef source can't be used and the env-var pair (`LMER_TASKDEF_REPO` + this ref) points elsewhere.

- **`LMER_RENDER_SOURCE`** - Test/CI-only switch for the render-matrix suite (`tests/test_taskdef_render_matrix.py`), not read by the CLI or the container runtime. When set, every taskdef directory found under the given path is rendered — honoring its root `taskdef.yaml` — against the *current checkout's* built-in base templates: `LMER_RENDER_SOURCE=<clone> uv run pytest tests/test_taskdef_render_matrix.py -q`. This is the contract an external taskdef content repo's CI uses to prove its bodies render against a pinned base (see docs/TASKDEFS.md).

- **`GITLAB_TOKEN`** / **`GITLAB_TOKEN_<sanitized_host>`** - Per-host or generic API token used to authenticate against GitLab hosts (also used by the legacy URL-token-injection path for target repos). Hostname suffix is lowercased with dots/hyphens replaced by underscores — e.g. `git.example.com` → `GITLAB_TOKEN_git_example_com`. The per-host form applies to exactly the host it names. The **generic** `GITLAB_TOKEN` is the last resort and applies only to the host that issued it: `LMER_GITLAB_TOKEN_HOST` when set, otherwise the host in `LMER_WORK_REPO`. For any other host — and for every host when neither is available — the generic token is **not** used and lmer prints a one-time notice to stderr naming the refused host; clone that repo anonymously or give it its own `GITLAB_TOKEN_<sanitized_host>`. Per-host lookups and the GitHub-family `GH_TOKEN`/`GITHUB_TOKEN` fallbacks are unaffected by this scoping.

- **`LMER_GITLAB_TOKEN_HOST`** - Bare hostname (e.g. `git.example.com`, matched case-insensitively) naming the host the generic `GITLAB_TOKEN` was issued for. Only that host gets the generic token; every other host falls through to "no token" with a one-time stderr notice, which is what keeps a GitLab PAT out of `github.com` (and any other third-party) clone URLs. When unset, the issuing host defaults to the host in `LMER_WORK_REPO`, so a single-host setup needs no configuration. Set it explicitly when the generic token belongs to a host other than the work-repo host. Forwarded into the container, where the same rule governs in-container clones.

- **`GH_TOKEN`** / **`GITHUB_TOKEN`** - Tokens used for GitHub hosts (`github.com`, `*.github.com`, `*.ghe.com`). `GH_TOKEN` takes priority over `GITHUB_TOKEN`. Either is consulted only after a more-specific per-host `GITLAB_TOKEN_<host>` is checked.

- **`LMER_REGISTRY`** - Container registry to pull pre-built images from. Optional; defaults to `ghcr.io/lmer2/lmer` (the project's GHCR registry). Override to point at a self-hosted or mirrored registry. Empty-string values are treated the same as unset and fall back to the default.

- **`LMER_NO_AUTO_BUILD`** - Disable automatic container image building. Accepted truthy values: `1`, `true`, `yes` (case-insensitive). When enabled, LMER will error if the image is not found locally instead of building it.

- **`REPO_AUTH_PREFER_SSH`** - When set to a truthy value (`1`, `true`, `yes`), LMER will use SSH URLs for git operations instead of converting them to HTTPS with token authentication.

- **`LMER_HARNESS`** - Which agent harness the session container runs: `claude` (default), `codex`, `pi`, or the name of a user-installed harness (see `LMER_HARNESSES_DIR` below). The `--harness` CLI flag takes precedence over the env var; when neither is set, the model name in `LMER_LLM_NAME` can autoselect the harness (word-bounded match: `opus`/`haiku`/`fable`/`sonnet`/`mythos` → `claude`; `gpt`/`codex`/`o3`/`o4` → `codex`; user-harness `model_hints` are checked after the built-ins). Unknown names fail fast on the host with the list of known harnesses. All built-in harness CLIs are baked into the one container image, so switching requires no rebuild. Claude is the full-feature tier; the others are core tier — see [HARNESSES.md](./HARNESSES.md) for the capability matrix, per-harness authentication, and how `LMER_LLM_NAME`/`LMER_REASONING_EFFORT`/`LMER_DANGER_ZONE` map onto each harness. Existing installs that never set this see no behavior change.

- **`LMER_HARNESSES_DIR`** - Where user-installed harness definitions live on the host (default: `~/.lmer/harnesses`). Each subdirectory `<name>/` carrying a `harness.json` manifest plus a `runner.sh` defines one additional harness, selectable exactly like the built-ins (which it can never shadow). When the directory exists, the host CLI mounts it read-only into the container at `/lmer-harnesses` and forwards **that container path** in this same variable, so the in-container consumers (runner dispatch, `lmer-supervisor`, `spawn-harness`) resolve the same definitions — don't set this variable to a host path inside a container context. Broken definitions are warned about and skipped, never fatal. See [User-installed harnesses in HARNESSES.md](./HARNESSES.md#user-installed-harnesses).

- **`LMER_HARNESS_CACHE`** - **Set by lmer, read by user-harness runner scripts** (not operator configuration): when the session harness is user-installed, the host mounts a persistent read-write cache volume (host side: `~/.lmer/harness-cache`) and sets this variable to the harness's directory on it (`/lmer-harness-cache/<name>`). The runner uses it for its install-if-missing step (e.g. as an npm prefix) so only the first session pays the install cost. To force a reinstall, remove `~/.lmer/harness-cache/<name>` on the host (`lmer build --update-harness` does not apply to user harnesses).

- **`LMER_MOUNT_LINKS`** - **Internal** (set by lmer and by the platform daemon, consumed by the container entrypoint; not operator configuration and there is no flag for it): the `declared:staged` pairs — comma-separated, each half an absolute container path — that `Ctl/container/setup-mount-links.sh` turns into symlinks before anything else starts. User-harness mounts whose declared path is **below the container home** (credential files and the orchestrator's transcript mount for a declared `session_dir`) bind under `/home/developer/.lmer-mounts` rather than at the path the manifest declared, because a bind mount's missing parent directories are created **root-owned** by the container runtime, and the session runs as `developer` with no-new-privileges — a harness writing a sibling file next to its own mount then fails with EACCES. The symlink is made by the container user, so the parent chain ends up developer-owned. A value you set by hand is honored but unsupported: pairs whose staged path was never mounted, or that name an existing non-empty directory or regular file, are skipped with a warning and never overwrite anything, and where two pairs name the same declared path the first one wins (the later is skipped with a warning).

- **`LMER_REASONING_EFFORT`** - Override the agent's reasoning effort for the session. Accepted values: `low`, `medium`, `high`, `xhigh`, `max`, `auto` (case-insensitive). For claude, `low`/`medium`/`high`/`xhigh`/`max` pass `--effort <level>` to the `claude` CLI. Other harnesses map the value onto their own knob (codex: `model_reasoning_effort`, pi: `--thinking`; `max` maps to `xhigh` where that is the top tier — see [HARNESSES.md](./HARNESSES.md)). When unset or set to `auto`, no flag is passed and the harness uses its own default. Invalid values are ignored with a warning. The vocabulary matches the per-lane dispatch efforts (`LMER_DISPATCH_<LANE>`, below), so the session and lane surfaces accept the same set.

- **`LMER_LLM_NAME`** - Start the agent with a specific model for the session. The value is passed verbatim to the harness's model flag (claude/codex/pi: `--model <value>`) — for claude, model aliases (`sonnet`, `opus`, `haiku`) and full model IDs (e.g. `claude-sonnet-4-6`) both work. The harness itself rejects unknown models, so LMER performs no validation of its own. For pi, models registered in the host's `~/.pi/agent/models.json` (custom providers, e.g. a local llama.cpp server) are valid values too — the registry file is mounted into the container when present (see [HARNESSES.md](./HARNESSES.md#custom-models-pi)). When unset or empty, no flag is passed and the harness uses its default model. When no harness is configured (`--harness`/`LMER_HARNESS` both unset), the model name also autoselects the harness — see `LMER_HARNESS` above and [HARNESSES.md](./HARNESSES.md). Forwarded into the container by the host CLI and applied by the harness runner script (e.g. `libexec/claude-runner.sh`).

- **`LMER_DISPATCH_REVIEW`** / **`LMER_DISPATCH_DESIGN`** / **`LMER_DISPATCH_CODE`** / **`LMER_DISPATCH_MECHANICAL`** / **`LMER_DISPATCH_EXPLORE`** - Per-lane model+effort dispatch for Claude **subagent definitions**. Each variable assigns a model (and optionally an effort) to one dispatch lane — the shipped agent def that handles that kind of work: `REVIEW` → `adversarial-reviewer`, `DESIGN` → `designer`, `CODE` → `coder`, `MECHANICAL` → `mechanical`, `EXPLORE` → `explorer` (all under `agent-files/claude/agents/`). Value format: **`<model>[:<effort>]`** — e.g. `LMER_DISPATCH_REVIEW=fable:high`, `LMER_DISPATCH_MECHANICAL=haiku`. The model is a Claude alias (`haiku`, `sonnet`, `opus`, `fable`), a full model ID, or `inherit`; it is passed through verbatim (no allowlist — claude itself rejects unknown models, the `LMER_LLM_NAME` philosophy). The effort, when given after the last colon, must be one of `low`/`medium`/`high`/`xhigh`/`max` (case-insensitive). Parsing rules: surrounding whitespace is trimmed; an empty value counts as unset; the value is split on the **last** colon only when the suffix is a valid effort token, so colon-bearing model IDs (e.g. Bedrock-style `…-v1:0`) pass through intact — an invalid suffix warns and the whole value is used as the model. At session start (`libexec/claude-agent-files.sh` → `lmer_cli.container.dispatch_agents`) a configured lane's agent symlink under `~/.claude/agents/` is replaced by a copy whose frontmatter carries the configured `model:`/`effort:`; an **unset lane keeps today's behavior** — the agent def is linked as-is and inherits the session model. There are no built-in per-lane defaults. Suggested operator settings: `REVIEW=fable:high`, `DESIGN=fable:xhigh`, `CODE=sonnet:high`, `MECHANICAL=haiku`, `EXPLORE=sonnet:low`. **Behavior change note:** `explorer.md` previously hard-pinned `model: sonnet`; that pin is removed, so with `LMER_DISPATCH_EXPLORE` unset the explorer now inherits the session model — set `LMER_DISPATCH_EXPLORE=sonnet` to restore the old behavior. All five variables are forwarded into the container by the host CLI and layer through `.env` files as usual (global defaults in `~/.lmer/.env`, per-project overrides in the project's `.env`).

- **`CLAUDE_AFK_TIMEOUT_MS`** - Enables (and sets, in milliseconds) Claude Code's **AskUserQuestion AFK auto-timeout**: when set, a question the human doesn't answer resolves automatically after the timeout instead of blocking the session forever. This is Claude Code's own variable, not an lmer setting, so it keeps its name (no `LMER_` prefix); lmer only forwards it — both host-exported values and values from a `.env` file reach the container. When unset, lmer applies a default of `300000` (5 minutes) for **Slack-bridged sessions** (any session with a Slack thread target), so an unanswered question times out and the session pings the thread rather than sitting silent; plain terminal sessions get no default and behave as stock Claude Code. `300000` is also the recommended value for other unattended deployments. See also `CLAUDE_AFK_COUNTDOWN_MS` (the on-screen countdown Claude Code shows before the timeout fires) — it can be set the same way via `.env`, but lmer never defaults it.

- **`LMER_HUMAN_IDENTITY`** - Free-form string identifying the human user the session is collaborating with (e.g. `"Jane Doe <jdoe on example.com, jane@example.com>"`). When set, it is forwarded into the container and injected into Claude's system prompt so the model can attribute matching usernames, emails, or handles in PRs, MRs, issues, comments, and commit history to the user. When unset, LMER falls back to the host's `git config user.name` and `user.email` (respecting system, global, and local config). If neither is available, no identity is injected. The injected text is rendered from the Jinja2 template at `prompts/human-identity.md.jinja2` — edit that file to change the wording.

- **`LMER_GIT_USER_NAME`** / **`LMER_GIT_USER_EMAIL`** - Override the git identity that commits made inside the container are authored and committed under. By default the container commits under the `~/.gitconfig` it inherits (a one-time copy of the host's, persisted in the container-home and bind-mounted). When either variable is set, the entrypoint exports it as git's native `GIT_AUTHOR_NAME`/`GIT_COMMITTER_NAME` (from `LMER_GIT_USER_NAME`) and/or `GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_EMAIL` (from `LMER_GIT_USER_EMAIL`). The two are independent — set only one and the other half falls back to gitconfig. The override is session-scoped and writes no file: the mounted `~/.gitconfig` is left untouched, so unsetting the variable fully reverts the behavior (no persistent state is mutated). Note that this affects commit authorship, not `git config --get user.name`/`user.email` reads, which still report the gitconfig values. This is distinct from `LMER_HUMAN_IDENTITY`, which controls who Claude attributes repository artifacts to in its system prompt and does not change commit authorship.

- **`LMER_QUICK_GATE_COMMIT`** - When set to a truthy value (`1`, `true`, `yes`, case-insensitive), `gate-commit` skips the test suite (the slowest check) but still runs pre-commit hooks, secret scans, and every other check. Tests are still enforced by standalone `gate-check` and by `gate-push`, so coverage is preserved before code leaves the local repo. Only `gate-commit` reads this variable; `gate-check` and `gate-push` ignore it. Falsy values (`0`, `false`, `no`) and unset both leave tests running, so this can be a transient export that you turn off without `unset`. Useful for iterative commits on a feature branch where you'll run `gate-push` (which runs the suite) before code leaves the repo.

- **`LMER_PUSH_ALLOW_LIST`** - Comma-separated list of push-authorization entries checked before any push leaves a session. Each entry is either a bare `repo` or `repo|refpattern`: the `repo` half is matched by the **grammar in `lmer_cli.push_allow`** (see below); the `refpattern` half is an fnmatch pattern tested against the **fully-qualified target ref** (e.g. `gitlab.example.com/group/project|refs/tags/*` grants tag pushes to that repo, `github.com/group/mirror|refs/heads/main` grants exactly one branch). The field delimiter is `|` because `:` already appears inside the repo half (SSH remotes `git@host:group/proj`, `https://host:port`) and `,` is the entry separator — the same pick-an-unclaimed-separator reasoning as `LMER_MOUNT_FILES`. **Backward-compatibility rule:** a bare entry authorizes **branch refs only** (`refs/heads/*`) — no pre-existing allow list silently gains tag-push rights; tags must be granted explicitly with `repo|refs/tags/*`. Malformed entries (empty half, more than one `|`) are **ignored fail-closed**: an unparseable grant never widens what is allowed. Authorization keys on what the push **changes on the remote**: for a `<src>:<dst>` refspec the `<dst>` side is matched (a branch-only grant can never authorize a refspec landing on a tag), force pushes (`+…`), glob refspecs, and short/ambiguous ref names are refused outright, an unresolvable named remote **fails closed**, a **detached HEAD** (no resolvable current branch) is refused rather than normalized to the bare `refs/heads/` prefix, and a push-by-URL is gated against the URL itself — never skipped. The url checked for a named remote is its **push** url (`git remote get-url --push`), which is where `git push` actually sends refs when `remote.<name>.pushurl` is set. On the **push-by-URL** branch the match is **anchored**: the url is parsed (userinfo only where it may legally appear — inside a `scheme://` authority, or before the host of the scp-like `[user@]host:path` form) and the entry must name the resulting `host/path` or the bare `host`. A **path-only entry does not authorize a push-by-URL** — any forge can serve the same path — so release-style grants that name a URL target must be written `host/path|refpattern`; wildcard and prefix entries are likewise inert on that branch, since neither names the single host git will dial. **Repo-half grammar** (#107), applied to configured remotes: an **exact repo** (`gitlab.example.com/group/project`, or any SSH/HTTPS spelling of it — they are interchangeable), a **whole host** (`gitlab.example.com`, matched exactly, not as a suffix), a **wildcard domain** (`*.example.com` — subdomains only, the `.` boundary enforced so `evilexample.com` never matches, and the apex needs its own entry), a **host + project prefix** (`gitlab.example.com/group`, segment-boundary safe: `group/project` yes, `groupfoo/x` no), or a **legacy host-less project path** (`org/repo` — that path on any host, kept so pre-#107 allow lists keep working; prefer host-qualified entries for new configuration). **IPv6 hosts are written bracketed** (`[2001:db8::1]`, `[2001:db8::1]/group/project`, `https://[2001:db8::1]/group/project`, `git@[2001:db8::1]:group/project.git` — all one identity, host `2001:db8::1`): git requires the brackets outside a `scheme://` URL, and they are what keeps the address's colons apart from the `host:path` delimiter, so the unbracketed spelling is not an entry (`2001:db8::1/group/project` parses as host `2001`) and an unclosed bracket names no host at all. Hosts compare case-insensitively, project paths case-sensitively. This **replaced an unanchored substring test**, so it is strictly tighter for every entry shape that predates it — `group/project` no longer authorizes `https://evil.example.com/mirror/group/project.git`, and `group/proj` no longer authorizes `group/project`. **Every push URL** of the target remote must be granted (`get-url --push --all`: a remote with several pushurls sends the ref to all of them), and a target that does not parse into `host/path` is refused rather than guessed at. The list is **unioned** with the active taskdef's top-level `push_allow` declaration in its `task.yaml`, resolved from the trusted taskdef tiers only — never the agent-writable work-repo tiers (see docs/TASKDEFS.md) — so a taskdef that knows its target can grant the push without operator env configuration. Both outcomes are transparent: a grant names the entry that authorized it and the source it came from, a refusal names the target, every source consulted, and an example entry that would allow the push. A release setup carries two extra entries: one for the **GitHub mirror repo** (authorizing release pushes to the mirror remote) and a `…|refs/tags/*` entry for the GitLab origin (authorizing the `v*` release-tag push, driven through `gate-push --tag`). By default (unset/empty), no repositories are auto-allowed. Read by **three enforcement points**, kept in lockstep by `tests/test_push_allow_grammar_parity.py`: `gates.py` (`run_push_gate`, backing `gate-push`), `hooks/pc.py` (`push_allowed`), and the generated pre-push hook written by `hooks/install.sh`. Consumed **in-container**; like other non-hardcoded `LMER_*` variables it reaches the container via the `.env` merge (cwd `.env`, `~/.lmer/.env`, or `--env-file`).

- **`LMER_RELEASE_GITHUB_TOKEN`** - Fine-grained GitHub PAT for the release flow: `contents:write` **+ `workflows:write`** on the single GitHub mirror repo. Both scopes are required — release pushes update `main`, which routinely carries `.github/workflows/*` changes GitHub rejects from a PAT without the workflows scope, and push the `v*` tags that trigger `release.yml`. **Forwarded into the container verbatim, but only for release-taskdef sessions** (host-side gate in `cli.py`: the resolved task id must equal `RELEASE_TASK_ID`, i.e. a `lmer release …` invocation); **every other session seeds the key `None`, making it unforwardable** — a key already present in the container env dict is skipped by both the `.env` merge and the preset seeding, so a PAT sitting in a cwd `.env` / `~/.lmer/.env` / `--env-file` or a preset's env can never leak into a non-release container (the `LMER_NAPKIN_TOKEN` precedent). Not required for leg-1 release work (version bump, MR), so an unset value is not an error.

- **`LMER_RELEASE_SIGNING_KEY`** - Release SSH signing key for signed release tags (`git tag -s`), delivered as a **path remap**: on the host, the path to the release SSH signing **private key**, consumed **host-side** by `build_release_signing_key_mount`, which bind-mounts the file **read-only** at the fixed container path `/release-signing-key` — the host path never enters the container environment. Inside a release container the variable holds that container path (`/release-signing-key`, point `user.signingKey` at it). Same scoping gate as `LMER_RELEASE_GITHUB_TOKEN`: **provisioned only to release-taskdef sessions and seeded `None` (unforwardable) for every other session.** The mount applies the credential-mount guard (must be a regular file whose resolved path stays under the host home); a *configured* key the guard rejects **aborts the launch** — a release session that cannot sign must not start (the `LMER_MOUNT_FILES` fail-fast precedent) — while an unconfigured key is simply skipped.

- **`LMER_REHEARSAL_GITHUB_TOKEN`** / **`LMER_REHEARSAL_SIGNING_KEY`** - Rig-scoped **rehearsal** credentials for the release-flow rehearsal rig (`Ctl/rehearsal/`): throwaway values under rig-only names, so no provisioning path can ever reach for the production names above. Deliberately **forwarded to every session** (a rehearsal runs as a masterplan — non-release — task) and **env-borne only**: `LMER_REHEARSAL_SIGNING_KEY` holds a host-side path consumed by the rig scripts and is never delivered through the production key mount.

- **`LMER_REHEARSAL_REPO`** / **`LMER_REHEARSAL_PROJECT`** / **`LMER_REHEARSAL_ENVIRONMENT`** - Rig **identity** (not credentials) for the same rehearsal rig: the scratch GitHub repo (`owner/name`), the TestPyPI project name (never the production project name — the rig's production-target guard refuses it), and the deployment environment in the trusted-publisher binding (default `testpypi`). Consumed only by the `Ctl/rehearsal/` scripts, set via the git-ignored `Ctl/rehearsal/rig.env`; the lmer CLI itself never reads them. See [RELEASE-REHEARSAL.md](./RELEASE-REHEARSAL.md) for the full runbook.

- **`LMER_NONINTERACTIVE`** - When set to a truthy value (`1`, `true`, `yes`, case-insensitive), declares that no human is attached to the session, so the agent-facing gates in `AGENTS.md` report a gate-worthy problem in their final output instead of ending the turn on an approval question nobody can answer (issue #137). Set it on unattended launches — cron jobs, CI, schedulers — where an approval prompt would otherwise be delivered to nobody and silently drop the session's result. Falsy values (`0`, `false`, `no`) and unset both mean "assume a human is present" — the gates ask normally — so this can be a transient export that you turn off without `unset`. The CLI never infers the value from the launch shape, so an interactive session is never mislabeled. **The variable is the signal, not the delivery:** no lmer path renders an environment value into a model's context, so the rule text travels as the `prompts/non-interactive.md` fragment — appended to the system prompt by `libexec/claude-runner.sh` for claude sessions, written into the global context file by `harness_render_global_context` for codex/pi (the `LMER_PERSIST_AGENT_MEMORY` fragment precedent). Forwarded into the container by the host CLI; the truthy parsing is done by those two shell readers. `spawn-harness` sets `LMER_NONINTERACTIVE=1` on its fan-out children unconditionally (a child harness process has no human by construction, and neither a preset overlay nor an `--env` pair can unset it) and prepends the same rule to every child prompt in-band, since a fan-out child execs its harness binary with no runner script to inject a fragment — so fan-out children need no host-side configuration.

- **`LMER_STATUSLINE`** - Comma-separated, ordered list of the segments the in-container Claude Code status line renders (issue #121). Unset or blank keeps the default `repo,branch,task,ctx` — exactly the pre-existing `group/project @ feature/x | develop | ctx 42%` line. Available segment names (case-insensitive, surrounding whitespace ignored): `repo`, `branch`, `task`, `ctx`, `model` (model display name, e.g. `Fable`), `cost` (session cost, `$1.23`), `5h` / `7d` (subscription usage-limit windows, e.g. `5h 24%` — only rendered when the payload carries `rate_limits`, i.e. on Claude subscription sessions), `effort` (reasoning effort, `eff high`), `duration` (session wall-clock time, `1h03m`), and `lines` (lines added/removed, `+156/-23`). Order in the list is render order; `repo` immediately followed by `branch` joins as `repo @ branch`, all other segments join with ` | `. Unknown names are ignored, a list selecting nothing falls back to the default, and a segment whose data is unavailable is simply omitted — the renderer never errors. The 📦/⚡ indicators are always appended and are not part of the list. Read in-container by `hooks/statusline.py` (via the provisioned `statusLine` command); forwarded into the container by the host CLI, so it can be set host-side or in `.env` files. Claude-harness only — codex/pi have no custom-statusline hook today.

- **`LMER_PERSIST_AGENT_MEMORY`** - When set to a truthy value (`1`, `true`, `yes`, case-insensitive), the session's agent memory is persisted to the work repo on a **per-project** basis, stored under `{host}/{project}/memory/` (shared across all task types, targets, and harnesses for that project). Restore is automatic: at session start the harness runner (`libexec/claude-runner.sh`, or `harness_restore_memory` for codex/pi) runs `work memory restore`, which copies any saved memory from the work repo into the shared memory directory (`~/.claude/projects/-workspace/memory/` — a harness-neutral store despite the path) before the agent reads it. Persisting back is the agent's responsibility — the agent runs `work memory persist` (which copies the memory directory into the work repo and commits and pushes it). Claude Code reads the directory natively; codex/pi sessions get the read/write/persist contract injected into their global context file (the `prompts/agent-memory.md` fragment — see [HARNESSES.md](./HARNESSES.md)). Falsy values (`0`, `false`, `no`) and unset both disable the feature, in which case both `work memory` subcommands are no-ops. Parsed via `get_bool_env` and forwarded into the container by the host CLI. The memory directory path can be overridden for non-standard layouts/tests with `LMER_AGENT_MEMORY_DIR`.

- **`LMER_RUN_STATE_GUARD`** - Kill switch for the **run-state compliance Stop hook** (`hooks/run_state_guard.py`), which blocks a stop while the run state is missing its phase, goal, or name — or the run dir carries uncommitted/unpushed changes, or the session has landed gate-commits without recording any execution-ledger row (`work ledger set`) — and replies with the exact `work` commands to fix it. Parsed via `get_bool_env`: unset or truthy (`1`, `true`, `yes`, case-insensitive) leaves the guard **enabled** (the default); set `LMER_RUN_STATE_GUARD=0` (or `false`/`no`) to disable it entirely. The guard is fail-open — any error in its own checks lets the stop proceed — so the kill switch exists to opt out of the nudges, not to work around hook failures. Forwarded into the container by the host CLI.

- **`LMER_SIGNAL_GUARD`** - Kill switch for the **signal-reminder Stop hook** (`hooks/signal_guard.py`, issue #289), which reminds an orchestrated session (`LMER_ASK_DIR` set) that ends a turn on an unreported milestone to run `lmer-signal "<what happened>"`. It blocks the stop when the turn shows milestone evidence — a successful milestone-shaped command in the transcript (`gate-push`, `gitlab-review --create-mr`/`--review-file`/`--reply-thread`, `github-review --review-file`, `work state set --status=complete`) or a run record reporting itself complete — and no signal-equivalent act: a successful `lmer-signal` after that milestone, a newer signal file in the channel dir (only when the transcript shows no signal of its own — ordered evidence wins), or a newly opened `lmer-ask` question (an asking turn already notifies the orchestrator; the suppression is bounded to the stops right after the question is posted, since an agent is expected to keep working while it waits). The GitLab/GitHub post-review wrappers are deliberately absent from transcript inference: they call `lmer-signal` themselves only after a successful post, which is stronger evidence than parsing a wrapper-shaped command. It **never signals on the agent's behalf** — a signal must keep meaning a milestone, so the hook reminds and the agent decides. Fires once per distinct milestone, capped at 3 per session via a `/tmp` marker keyed on `LMER_SESSION_ID`; `spawn-harness` fan-out children (`LMER_NONINTERACTIVE`) are skipped entirely, because a `claude -p` child's only output is its last turn and a Stop block would replace the result its parent is waiting for. Detection is a list of known command spellings, not a classifier: a milestone reached through a slash command, a subagent, or an MCP tool is invisible to it, so silence from this hook means "nothing to report", never "nothing happened". Parsed via `get_bool_env`: unset or truthy (`1`, `true`, `yes`, case-insensitive) leaves the guard **enabled** (the default); set `LMER_SIGNAL_GUARD=0` (or `false`/`no`) to disable it entirely. The guard is fail-open — any error in its own checks lets the stop proceed — so the kill switch exists to opt out of the nudges, not to work around hook failures. Claude-harness only, like every Stop hook; codex and pi runs have no mechanical signal reminder (see [HARNESSES.md](./HARNESSES.md)). Forwarded into the container by the host CLI.

- **`LMER_GATE_INFLIGHT_GUARD`** - Kill switch for **gate-in-flight coordination** (issue #201). A gate command (`gate-check`, `gate-commit`, `gate-push`) and `work verify` hold a marker for the whole time they run; while one is live, every work-repo durability commit (`work commit`, and the implicit pushes in `work state set` / `log` / `goal` / `artifact` / `ledger set`) **defers** instead of committing — it leaves the files on disk, records a `commit_deferred` event on the run, and exits 0 — and the Stop hook (`hooks/run_state_guard.py`) suppresses its push nudge for that window. Without this, a session that gates in the background and yields mid-run has its own mandated `work commit` sweep tracked run-dir files into a commit underneath the running suite, which then fails the `/work` isolation guard in `tests/conftest.py`. Parsed via `get_bool_env`: unset or truthy (`1`, `true`, `yes`, case-insensitive) leaves the coordination **enabled** (the default); set `LMER_GATE_INFLIGHT_GUARD=0` (or `false`/`no`) to restore the pre-#201 behavior, in which those commits land immediately regardless of a running gate. Markers are still written when the switch is off — only the consumers stand down. Deferral is bounded by the gate's life, so the push nudge returns (and still refuses the stop) as soon as the gate ends. The test suite holds a `pytest-suite` marker of its own, so bare `pytest` runs get the same deferral coverage as gate-command runs; and while any marker is live, **bare** work-repo writes (`work log`, `work event`, the file writes behind `work state set`) journal themselves beside the markers with a process-ancestry verdict, letting the suite's leak guard excuse the launching session's own writes instead of failing the run (issue #233). The kill switch covers that journaling too. Forwarded into the container by the host CLI; see [RUN-STATE.md](./RUN-STATE.md) §2.

- **`LMER_GATE_LOCK_DIR`** - Directory holding the gate-in-flight markers described above (default `/tmp/lmer-gate-inflight`; one `<pid>.json` file per holding process). Read at call time, so it can be redirected after the process starts — the test suite points it at a tmp dir via an autouse fixture so tests exercising the commit paths are not deferred by the very `gate-check` running them. A marker counts only while its pid is alive; dead markers are pruned on the next read, and a six-hour age cap exists solely as a pid-reuse backstop (never as a gate timeout — a cap that could expire under a running gate would reintroduce the bug it prevents). Forwarded into the container by the host CLI.

- **`LMER_GATE_NO_FASTPATH`** - Kill switch for the gate's **text-only fast path** (issue #269). When every path a change touches is prose, `gate-check` runs the tests the project declared under `tests.text_diff_subset` in `.lmer/gate-check.yaml` instead of the whole suite; a project that declares nothing gets the full suite, always. Set to `1` (or `true`/`yes`) to run the full suite regardless. See [GATE-FASTPATH.md](./GATE-FASTPATH.md).

- **`LMER_GATE_NO_CACHE`** - Kill switch for the gate's **test-result cache** (issue #269). A passing suite is recorded under a key covering the tree, the uncommitted state, the pytest invocation, the interpreter, its installed distributions and the environment, and a later gate composing the same key reuses that pass instead of re-running; only passes are cached, and anything the key cannot compute means no read and no write. Set to `1` (or `true`/`yes`) to re-run the suite and record nothing. See [GATE-FASTPATH.md](./GATE-FASTPATH.md).

- **`LMER_GATE_CACHE_DIR`** - Directory holding those recorded passes (default `/tmp/lmer-gate-cache`; one `0600` JSON file per key in a `0700` directory, storing digests and variable names but never a value). Read at call time, so it can be redirected after the process starts. `/tmp` is the default on purpose: a cached verdict describes a tree *and* the environment that ran it, so it must not outlive the container. See [GATE-FASTPATH.md](./GATE-FASTPATH.md).

- **`LMER_PRECOMMIT_CACHE_DIR`** - Directory holding the separate, short-lived full-`--all-files` pre-commit pass cache (default `/tmp/lmer-precommit-cache`). It follows the same owner-only file/directory rules and is forwarded into the container, where the gate runs. Reuse remains off unless the work repo's project info opts the repository in. See [GATE-FASTPATH.md](./GATE-FASTPATH.md).

- **`LMER_MASTERPLAN`** - When set to a truthy value (`1`, `true`, `yes`, case-insensitive; parsed via `get_bool_env`), the session runs the **masterplan** workflow (brainstorm → plan → execute → finish, on top of superpowers). Setting `LMER_TASK=masterplan` implies it, and a taskdef can declare the need itself via `masterplan: true` in a per-task `task.yaml` (see docs/TASKDEFS.md) — so this toggle is mainly for one-off enabling of masterplan on top of a task whose taskdef does not declare it. When active, `libexec/claude-runner.sh` at session start runs `libexec/masterplan-enable.sh --gated`, which installs the masterplan plugin from the first existing mirror in `LMER_MASTERPLAN_MIRROR_CANDIDATES` (see below; default order: taskdef repo, then the deprecated work-repo mirror) via `claude plugin marketplace add <mirror>` → `plugin install masterplan@rasatpetabit-masterplan` → `plugin enable masterplan`, and exports `MASTERPLAN_RUNS_DIR` (see below). The same script without `--gated` is the mid-session on-demand path: it forces enablement in a running session, persisting the environment via `~/.bashrc.d/masterplan-env.sh` (the parent claude process cannot be re-enved), after which one `/reload-plugins` from the user activates the plugin. Every step is idempotent and non-fatal — provisioning failures warn and continue rather than aborting the session. superpowers is baked into the image but left disabled (the image bake removes `~/.claude/settings.json` so no `enabledPlugins` entry survives, keeping the runtime global-settings symlink intact) and is re-enabled automatically as masterplan's declared dependency when the plugin installs, so plain sessions pay no runtime cost. Because the runtime `settings.json` starts as a read-only symlink, the provisioning step materializes it into a writable regular file before the `claude plugin` calls; the path is overridable for non-standard layouts/tests with `LMER_SETTINGS_FILE`. Falsy values (`0`, `false`, `no`) and unset both disable the toggle itself — they do not veto `LMER_TASK=masterplan` or a taskdef's own `task.yaml` declaration. Forwarded into the container by the host CLI.

- **`MASTERPLAN_RUNS_DIR`** - Set **by lmer inside the container** for masterplan sessions (not a host input): the bundle root masterplan writes to, computed as `<current-run-dir>/masterplan` where the run dir comes from the run-state kernel (`work_repo.run_state.run_dir()`). This nests masterplan's bundles (`<mp-slug>/` each with its own `state.yml`/`events.jsonl`) inside the lmer run directory alongside the run's own `state.yaml`, so masterplan artifacts are captured with the rest of the run. It is not LMER-prefixed because it is masterplan's own configuration variable (honored by masterplan's `lib/paths.mjs`); lmer only computes and exports it. Only set when the run dir is resolvable (`LMER_REPO_HOST`/`LMER_REPO_PROJECT` present); otherwise provisioning is skipped. Mid-session enablement (`masterplan-enable.sh` without `--gated`) persists it via `~/.bashrc.d/masterplan-env.sh` instead of the process environment, and its `--repo-host`/`--repo-project` flags can supply a repo target for a session launched without one.

- **`LMER_MASTERPLAN_MIRROR_CANDIDATES`** - Colon-separated list of masterplan plugin-mirror directories consulted by `libexec/masterplan-enable.sh` (default `/taskdef/mirrors/masterplan:/work/mirrors/masterplan`): the first *existing* candidate is used for `claude plugin marketplace add`; when none exist the last candidate is attempted anyway (its failure warns and continues, preserving the historical fail-soft behavior for the work-repo mirror). The taskdef repo is the mirror's canonical home — resolving to anything but the first candidate emits a deprecation warning telling the operator to configure `LMER_TASKDEF_REPO` with a repo shipping `mirrors/masterplan`; the work-repo location keeps working but is deprecated. Forwarded into the container by the host CLI.

- **`LMER_SETTINGS_LOCAL_FILE`** - Path to the personal `settings.local.json` whose `permissions.allow`/`deny` `libexec/claude-runner.sh` merges into the effective settings file (`LMER_SETTINGS_FILE`, default `/home/developer/.claude/settings.json`); default `/home/developer/.claude/settings.local.json`, an override for non-standard layouts/tests, and a path that does not exist simply means no merge. Like `LMER_SETTINGS_FILE` and `LMER_AGENT_MEMORY_DIR`, this is an **in-container seam** read only by the runner script — the host CLI does not forward it, so a host `export` never reaches it; it crosses only when a `.env` file (or a preset's `env`) carries it.

- **`LMER_MCP_FILE`** - Path to the base `.mcp.json` that `libexec/claude-runner.sh` merges personal MCP servers into, rewriting it in place (default `/home/developer/.mcp.json`); an override for non-standard layouts/tests, and an in-container seam like `LMER_SETTINGS_FILE` — a host `export` does not reach it, only a `.env` (or preset `env`) entry does.

- **`LMER_MCP_LOCAL_FILE`** - Path to the personal `.mcp.local.json` whose `mcpServers` are merged into the file above (default `/home/developer/.mcp.local.json`); an override for non-standard layouts/tests, a path that does not exist simply means no merge, and — like the two above — an in-container seam a host `export` does not reach.

- **`LMER_MOUNT_FILES`** - Comma-separated list of explicit per-file mounts, each entry using the `--mount-file` grammar `host:container[:mode]` (comma is the entry separator so it does not collide with the `:` field separator, cf. `LMER_TASKDEF_PATHS` choosing its own separator for the same reason). `host` gets `~`/`$VAR` expansion and must resolve to an **existing file**; `container` must be an **absolute** path; `mode` is `ro` (default) or `rw`. Env entries are applied **before** `--mount-file` flags, so a persistent `.env` sets a baseline an ad-hoc flag can override; when two entries target the same container destination, last wins and a warning is emitted. Any invalid entry **aborts the run** — fail-fast is deliberate and stricter than the skip-and-warn mount builders (external taskdefs, uv cache), because these entries are typically credentials (e.g. a kubeconfig) and silently launching without one only surfaces as confusing downstream auth failures. Files are bind-mounted as-is, never copied; SELinux labeling is applied automatically when enforcing. This is a **host-side** variable read by the launching CLI; it does not need to reach inside the container.

- **`LMER_MOUNT_UV_CACHE`** - When set to a truthy value (`1`, `true`, `yes`, case-insensitive), the host's `uv` cache directory is bind-mounted read-write into the container at `/home/developer/.cache/uv`, so `uv sync` / `uv pip install` invocations in the target repo reuse already-downloaded wheels instead of re-fetching them every session. The host path is resolved using uv's own rules: `$UV_CACHE_DIR` first, then `$XDG_CACHE_HOME/uv`, then `~/.cache/uv` on Linux or `~/Library/Caches/uv` on macOS. Falsy values (`0`, `false`, `no`) and unset both disable the mount. If the host cache directory doesn't exist, lmer prints an info message and continues without the mount. Parsed via `get_bool_env`. This is a **host-side** variable read by the launching CLI; it does not need to reach inside the container.

- **`LMER_CLONE_CACHE`** - Toggle for the **persistent git clone cache** (issue #112). **On by default**: unset or truthy (`1`, `true`, `yes`, case-insensitive; parsed via `get_bool_env`) maintains a host directory of **bare mirrors** (`<host>/<project>.git`; a non-default port joins the host segment, e.g. `git.example.com_8443/grp/proj.git`, so two servers sharing a hostname never share a mirror), mounts the mirrors **this launch needs** — one **read-only** bind each, at their usual path under `/clone-cache` (issue #135; see *Per-launch mirror mounts* below) — and forks a detached background **updater** (`python -m lmer_cli.clone_cache`, repo URLs delivered on stdin) that creates/refreshes the mirrors for the repos the session is about to clone (project, work, napkin/taskdef). The session never waits on maintenance: a cold cache just means a plain direct clone while the updater builds the mirror for next time (the first run therefore transfers such a repo twice — once for the workspace, once for the mirror), and a **stale mirror is never wrong** — the container clones with `--reference <mirror> --dissociate`, so it still talks to the real origin and fetches whatever the mirror lacks; origin stays the real remote (branch/MR checkout and push flows are untouched) and `--dissociate` repacks the clone so the workspace never depends on the cache afterwards. Concurrent updaters serialize per-mirror on a non-blocking `flock` (a held lock means that mirror is skipped, never queued on); mirrors are created with `gc.auto=0` so a fetch can never repack packfiles out from under a concurrently-reading container. Everything is fail-soft: a disabled cache, an unusable cache directory, an updater failure, or a failed cache-referenced clone all degrade (with a warning) to the plain direct clone lmer did before. Set `LMER_CLONE_CACHE=0` (or `false`/`no`) to disable mount and updater together. **Credentials never touch the cache or any command line**: mirrors store only the scrubbed origin URL, the tokenized URL is delivered to the updater via stdin and to git via ephemeral per-process env config (never argv), a **non-http(s)** URL — whose userinfo is a transport login rather than an HTTP credential — is fetched from a URL that keeps the `user@` an ssh remote needs and drops any password, and gets no auth header at all (issue #163), and the updater's log (`~/.lmer/logs/clone-cache.log`, size-capped, credential-scrubbed) lives outside the mounted cache root. A credentialed fetch sends **exactly one** `Authorization` header — its own: an `http.extraHeader` from your git config (or an inherited `GIT_CONFIG_KEY_n` pair) is cancelled first, where previously both went out and the remote picked between them, and `credential.helper` is reset so nothing can substitute other credentials or block the detached updater on an interactive unlock. That cancellation is deliberately blunt — an empty `http.<url>.extraHeader` resets git's *whole* header list for the URL, not just its `Authorization` entries, so a **required non-auth** header you carry via `http.extraHeader` (proxy routing, a tenant id) is dropped on a URL lmer credentials; git offers no narrower instrument, and the accumulated list cannot be read back and re-emitted selectively (`git config --get-urlmatch` reports only the single best-matching value); tracked in issue #179. **A rejected credential does not cost the mirror** (issue #157): an injected token is not necessarily one the target host accepts — with `GITLAB_TOKEN` set and no GitHub token, the generic token fallback puts that PAT into `github.com` URLs, and GitHub challenges it even for a public repo — so an attempt that carried credentials and failed is retried once with **lmer's own injection dropped and nothing else changed**, and the retry is recorded in the updater log. The retry is the tokenless environment: it undoes only what lmer attached, so your own git config — headers and credential helper included — still authenticates it, exactly as it would a fetch lmer never credentialed. Only a non-zero git exit earns that retry: a fetch that hit its timeout, or a local filesystem error, is reported as-is rather than paying for a second doomed transfer while the mirror's lock is held. A URL lmer could not credential (no token for that host) is left entirely to your own git config — credential helper included — and makes exactly one attempt; a working token still succeeds on the first. Note: bare-mirror fetches never pull git-LFS content, so warm-mirror savings don't extend to LFS objects (LFS-tracked repos still benefit for their plain git objects). The detached updater runs on the **host**, so its Python import path is deliberately not caller-controlled: it is launched with `-P` (`PYTHONSAFEPATH`, keeping the launch cwd off `sys.path`), `PYTHONPATH` pinned to lmer's own package directory, and a trusted cwd — a stray `lmer_cli/` directory in whatever directory you ran `lmer` from can never become host-side code. The updater entrypoint is also invocable standalone (URLs on stdin, one per line) by external schedulers — a systemd timer or k8s CronJob warming a shared read-only cache uses the same code. **Per-launch mirror mounts (issue #135):** the container is given **only the mirrors for the repos this launch clones** (project, work, napkin/taskdef, plus any secondary MR/PR target the container clones alongside the primary — deduplicated, since the work and napkin repo are frequently one URL), each as its own read-only bind at the same path under `/clone-cache` the whole-root mount used to give it, so the container-side lookup is unchanged. A cached repo unrelated to the session — a private repo whose credentials this session was never given — is therefore not visible in it at all. lmer previously mounted the cache *root*, which handed every session the full history of every repo you had ever cached; `:ro` protected the cache's integrity, not its confidentiality. Only mirrors that **already exist** are mounted: a repo the cache has not built yet contributes no bind (the runtime is never handed a missing source, which Docker/Podman would create as a root-owned directory on your host) and takes the unchanged cold-cache path — direct clone now, warm mirror next time. A mirror without a `HEAD`, or one whose path is a symlink pointing outside the cache root, is skipped for the same reason it would never be used: the container can't borrow from it, and with per-mirror binds a symlink would be followed host-side. This is a **host-side** variable read by the launching CLI; the container is steered via `LMER_CLONE_CACHE_PATH` (below).

- **`LMER_CLONE_CACHE_DIR`** - Host directory backing the persistent clone cache (see `LMER_CLONE_CACHE`). Empty or unset falls back to the default `~/.lmer/clone-cache`; a set value gets `~` expansion and must be an **absolute** path — a relative value is refused with a warning and the default is used instead, because it would otherwise split the feature in two (Docker/Podman read `cache:/clone-cache:ro` as a *named volume* while the host-side updater populates a real `./cache` directory, so the container would never see the mirrors). An obviously-broad root (`/`, or your home directory itself) is refused the same way. Since #135 that is a **host-side** guard rather than a confidentiality one — the container never sees the root, only the individual mirrors a launch needs — but the updater still scatters `<host>/<group>/<project>.git` mirror trees directly through this directory, which your home directory (let alone `/`) should not be. A dedicated absolute directory — `~/mirrors`, `/srv/lmer-clone-cache` — is the intended shape. The directory is created on first use; if it cannot be created, lmer warns and launches without the cache mount. Safe to delete at any time to reclaim disk — thanks to `--dissociate`, existing workspaces keep working, and the updater simply re-creates the mirrors it needs (auto-gc is disabled on mirrors, so deleting the cache periodically is also the simplest way to compact accumulated packs). This is a **host-side** variable read by the launching CLI; it does not need to reach inside the container.

- **`LMER_CLONE_CACHE_PATH`** - Set **by lmer inside the container** (not a host input): the container-side root of the read-only clone-cache mounts (`/clone-cache`) whenever the cache is enabled and its host directory is usable; unset when `LMER_CLONE_CACHE=0` or the host cache directory was unusable. It is set even on a **cold** cache, where the launch mounts no mirror at all (see #135 above) — the variable advertises where mirrors would be, and the lookup below simply misses and clones directly. The container clone script (`clone_and_exec.py`) is a pure cache **consumer** keyed off this variable's presence: when a mirror for the clone URL exists it adds `--reference <mirror> --dissociate`, otherwise it clones directly — it never creates, refreshes, or writes anything in the cache.

- **`LMER_PIDS_LIMIT`** - Overrides the container PID cap that LMER passes as `--pids-limit` to `docker`/`podman run` (default `512`). Accepts any **positive integer**, or **`-1`** for "unlimited" (Docker/Podman semantics). Any other value — `0`, other negatives, or non-numeric — is rejected with a warning and falls back to `512`, so a misconfigured *value* can never silently weaken the fork-bomb safety bound. This is a **host-side** variable read by the launching CLI; it does not need to reach inside the container. Raise it (or set `-1`) on hosts affected by the cgroup-v1 pids-controller counter leak, where phantom fork entries accumulate over a long session and prematurely exhaust the cap — see [Troubleshooting: containers hit the PID cap](#troubleshooting-containers-hit-the-pid-cap-cgroup-v1-pids-leak). **Rootless-podman caveat:** on cgroup-v2 hosts where systemd has not delegated a controller to `user@<uid>.service` (Fedora/RHEL default delegates only a subset), the corresponding resource flag — including `--pids-limit` when the `pids` controller isn't delegated — is **dropped entirely** rather than passed (crun would otherwise abort or hang the session). lmer warns loudly and points at the fix: create `/etc/systemd/system/user@.service.d/delegate.conf` with `[Service]` / `Delegate=cpu cpuset io memory pids`, then `sudo systemctl daemon-reload`. Root podman, docker, and hosts where the user-slice controllers file is missing (e.g. cgroup v1) are not gated — they always receive the flags.

- **`LMER_CPUS`** - Overrides the container CPU quota that LMER passes as `--cpus` to `docker`/`podman run` (default `1`). Accepts a **positive number of cores up to 4096**, integer or fractional (`2`, `8`, `0.5`, `1.5`, at most 9 fractional digits) — a deliberate subset of what both runtimes accept. Any other value — `0`, negatives, `inf`/`nan`/scientific notation, non-numeric, or magnitudes past 4096 (extreme values would otherwise wrap to docker's int64 nano-CPU "unset" encoding, i.e. no limit at all) — is rejected with a warning and falls back to `1`; there is no "unlimited" spelling, so a misconfigured *value* can never silently remove the CPU bound (to use every core, name the core count). The runtime still enforces its own bounds at launch — e.g. docker rejects a core count above what the host has. Raise it when a session's workload is CPU-bound — a parallel test suite or build is capped at a single core out of the box, however many the host has. This is a **host-side** variable read by the launching CLI; it does not need to reach inside the container, and it can be set from a preset's `env` or your `.env` exactly like `LMER_PIDS_LIMIT`. The same **rootless-podman cgroup-v2 delegation caveat** applies: when the `cpu` controller isn't delegated to `user@<uid>.service`, `--cpus` is dropped entirely rather than passed — see `LMER_PIDS_LIMIT` above for the warning and the `delegate.conf` fix.

- **`LMER_MEMORY`** - Overrides the container memory cap that LMER passes as `--memory` to `docker`/`podman run` (default `2g`). Accepts an **integer with an optional unit suffix, resolving to at least `6m`** (docker's minimum allocation) — `b`, `k`, `m`, `g` or their two-letter forms (`kb`, `mb`, `gb`), case-insensitive, bytes when omitted (`8g`, `512m`, `2G`, `1073741824`) — a deliberate subset of the runtimes' size grammar; spellings outside it the runtimes themselves would take (fractions like `2.5g`, `t` suffixes) also fall back. Any other value — including sizes below the floor, like a bare `8` meaning eight bytes, which would otherwise abort the launch — is rejected with a warning and falls back to `2g`, so — as with `LMER_CPUS` — a misconfigured *value* can never silently remove the memory bound. This is a **host-side** variable read by the launching CLI; it does not need to reach inside the container, and it can be set from a preset's `env` or your `.env`. The **rootless-podman cgroup-v2 delegation caveat** described under `LMER_PIDS_LIMIT` applies to the `memory` controller in the same way.

- **`LMER_PORT_COUNT`** - Number of host ports to allocate from the pool (see `LMER_PORT_POOL`) and publish into the container, so a service Claude starts inside (e.g. a dev web server) is reachable from the host. Equivalent to the `--ports` flag, which takes precedence when both are set. Must be a non-negative integer; `0` (or unset) disables port passthrough. A non-numeric value aborts startup with an error. The allocated ports are exported to the container as `LMER_PORTS`. Read by the host CLI only.

- **`LMER_PORT_POOL`** - Inclusive port range `LOW-HIGH` the `--ports`/`LMER_PORT_COUNT` ports are picked from (default `8800-8899`, kept distinct from the FastAPI range `8700-8799` so both features can be used together). Equivalent to the `--port-pool` flag, which takes precedence. The host CLI picks the requested number of currently-free ports from this pool before the container starts, so multiple `lmer` instances on the same host get disjoint ports without manual coordination. If fewer than the requested number of free ports are available, startup aborts with an error. Read by the host CLI only.

- **`LMER_PORTS`** - Set **by lmer inside the container** (not a host input): a comma-separated list of the ports allocated via `--ports`/`LMER_PORT_COUNT` (e.g. `8842,8857`). Each is published on the host (loopback `127.0.0.1` by default, overridable via `LMER_PORT_BIND`) with the same port number inside and out. Services Claude starts should bind to `0.0.0.0` on one of these ports to be reachable from the host. Empty/unset when no ports were requested.

- **`LMER_PORT_BIND`** - Host bind address used when publishing `--ports`/`LMER_PORT_COUNT` mappings (default `127.0.0.1`). Equivalent to the `--port-bind` flag, which takes precedence when both are set. Set to `0.0.0.0` to expose the allocated ports on every host interface (so other machines on the LAN can reach a service Claude starts inside the container), or to a specific IP (e.g. `192.168.1.42`) to publish only on that interface. The value is also used to probe for free ports in the pool, so the chosen ports are guaranteed bindable on that address. **Security note:** the default is loopback for a reason — opening published ports to the network exposes any service Claude binds inside the container; only widen the bind when you trust both the network and what the agent is running. Read by the host CLI only.

- **`LMER_AUTO_START_DELAY`** - Seconds the supervisor waits before injecting the initial `/start` into Claude (the marker-based prompt-ready wait, see `LMER_AUTO_START_READY_TIMEOUT`, may extend this). Accepts a float (default `1.5`); negative values are clamped to `0`. Also settable per-invocation with `--auto-start-delay`. Has no effect under `--manual-start`/`LMER_MANUAL_START`. Parsed by `lmer-supervisor` and forwarded into the container by the host CLI.

- **`LMER_AUTO_START_NUDGE_DELAY`** - Seconds between the follow-up carriage-return "nudges" that the supervisor sends after auto-injecting `/start`. The initial Enter is occasionally swallowed during Claude's startup re-render, leaving `/start` typed but unsubmitted; the supervisor sends a few bare CRs afterward to re-trigger submission (each is a harmless no-op once `/start` has gone through). Accepts a float (default `0.5`); negative values are clamped to `0`. Has no effect under `--manual-start`/`LMER_MANUAL_START`. Parsed by `lmer-supervisor` and forwarded into the container by the host CLI.

- **`LMER_AUTO_START_READY_TIMEOUT`** - Maximum seconds the supervisor will wait for Claude's input-prompt glyph (`❯`) to appear in the output stream before auto-injecting `/start`. Claude Code v2.1.119 changed Enter routing so any modal/dialog open during startup (theme picker, IDE detect, permission prompt, etc.) consumes a CR rather than also submitting input-box text; waiting for the prompt glyph lets Claude finish its startup chain before we type. On timeout the injection fires anyway (with the cooked-mode pre-clear + CR nudges still providing best-effort delivery). Accepts a float (default `15.0`); set to `0` to disable marker-based readiness and inject purely on the initial delay. Has no effect under `--manual-start`/`LMER_MANUAL_START`. Parsed by `lmer-supervisor` and forwarded into the container by the host CLI.

- **`LMER_AUTO_START_READY_MARKER`** - UTF-8 string the supervisor scans for in Claude's output to decide that the input prompt has rendered (default `❯` — U+276F). The marker is a heuristic: `❯` is also used as a row-selection indicator in some TUI pickers, so a future Claude UI change could shift the right marker. Override here lets you patch in a more specific string (e.g. a unique-to-input-box sequence) without an lmer release. Set to the empty string to disable marker gating entirely (equivalent to `LMER_AUTO_START_READY_TIMEOUT=0`). Has no effect under `--manual-start`/`LMER_MANUAL_START`. Forwarded into the container by the host CLI.

- **`LMER_AUTO_START_SETTLE_DELAY`** - Seconds the supervisor pauses after the ready marker is observed before typing the start command. The prompt glyph often renders mid-way through a multi-screen redraw; the short settle lets the input box reach its steady, focused state so the injected text isn't dropped. Accepts a float (default `0.25`). Has no effect under `--manual-start`/`LMER_MANUAL_START`. Parsed by `lmer-supervisor` and forwarded into the container by the host CLI.

- **`LMER_START_COMMAND`** - Text the supervisor types (followed by Enter) to begin the task once the TUI is ready. The default comes from the active harness's supervisor profile — claude uses its native `/start` slash command, the other harnesses get a generic start instruction (see the profile table in [HARNESSES.md](./HARNESSES.md)). Override to patch a harness TUI change without an lmer release. Has no effect under `--manual-start`/`LMER_MANUAL_START`. Read by `lmer-supervisor` and forwarded into the container by the host CLI.

- **`LMER_QUIT_SEQUENCE`** - Key/text steps the supervisor types to make the wrapped TUI exit during a self-shutdown (SIGUSR1). Steps are separated by `|` and unicode-escape decoded — e.g. `\x03|\x03` for Ctrl-C twice, `/quit\r` for a typed command plus Enter. An empty value disables the chord step entirely, so shutdown escalates straight to SIGTERM. The default comes from the active harness's supervisor profile (see the profile table in [HARNESSES.md](./HARNESSES.md)). Read by `lmer-supervisor` and forwarded into the container by the host CLI.

- **`LMER_WINSIZE_RECHECK_DELAY`** - Seconds after launch when the supervisor re-queries the host TTY size and re-applies it to the wrapped PTY (delivering a SIGWINCH). Covers terminals — notably VSCode's integrated terminal — that haven't propagated their real size by the moment the container TTY is allocated, which otherwise leaves the TUI laid out for a stale 80x24-ish default until a manual resize. Accepts a float (default `0.5`). Parsed by `lmer-supervisor` and forwarded into the container by the host CLI.

- **`LMER_START_PROMPT`** - Follow-up prompt the supervisor types and submits a short, configurable delay after auto-injecting `/start` (see `LMER_START_PROMPT_DELAY`), so an automated run can hand Claude an extra instruction without manual typing (e.g. `"make sure to research X online first"`). Claude queues input typed while it is still working on `/start`, so the prompt becomes the next conversation turn. Its submit Enter gets the same bare-CR nudge re-submission as `/start` (governed by `LMER_AUTO_START_NUDGE_DELAY`) in case the initial CR is swallowed. Set on the host via the `--prompt` CLI flag (which populates this var); an empty or unset value means no follow-up. Tied to auto-start, so it is a **no-op under `--manual-start`/`LMER_MANUAL_START`** (nothing is auto-injected then). Forwarded into the container by the host CLI.

- **`LMER_START_PROMPT_DELAY`** - Seconds the supervisor waits between submitting the auto-`/start` and injecting the follow-up `LMER_START_PROMPT`. The delay lets `/start` register as a slash command first; without it, on a slow system the prompt text is typed before `/start` has been recognized and both land on the same input line (`/start <prompt text>`) instead of as separate turns. Accepts a float (default `2.0`); negative values are clamped to `0`. Has no effect when no follow-up prompt is set, and (like the prompt itself) is a no-op under `--manual-start`/`LMER_MANUAL_START`. Also settable per-invocation on the supervisor with `--start-prompt-delay`. Parsed by `lmer-supervisor` and forwarded into the container by the host CLI.

- **`LMER_SUBMIT_ENTER_DELAY`** - Seconds the supervisor waits between typing a submitted message and pressing Enter on it (`POST /input` with `append_newline`, which is every message the platform chat pane, `lmer pipe`, `lmer platform ctl input` and the Slack path send). It is the margin *on top of* waiting for the harness to read the text — see the `POST /input` contract below — covering a harness that may still be coalescing input in its own event loop after the read. Accepts a float from `0` to `1.0`; `0` is honored, and anything else — non-numeric, negative, non-finite (`nan`, `inf`, `1e400`), or above the ceiling — warns on stderr and falls back to the default `0.2`. The ceiling is not a preference: the wait runs while the PTY write lock is held, so a large value freezes the session's terminal I/O for its duration, and the obvious slip here is milliseconds-for-seconds (`LMER_SUBMIT_ENTER_DELAY=200` is rejected, not honored). Raise it if long messages still land typed-but-unsubmitted, but note two limits it does not lift: a message with **many embedded newlines** does not submit under any delivery variant tried (issue #230), and on a **heavily loaded host** a send can still land unsent, where raising the delay to `1.0` was measured *not* to help (issue #231). Read at each send by `lmer-supervisor` but sourced from the container's environment, which is fixed when the session is created — so **changing it on the host affects sessions started afterwards, not the one you are looking at**; restart the session to retune it. Forwarded into the container by the host CLI.

- **`LMER_ANSWER`** - Answer to the run's recorded open question (the durable `open_question` field a previous session stored via `work state set --stop-reason=question --question "<text>"`). **Flag-only**: sourced exclusively from the `--answer` CLI flag (which populates this var, same deliberate pattern as `LMER_START_PROMPT`); a host-exported or `.env`-set `LMER_ANSWER` is never forwarded. An answer is one-shot data while `.env` is standing configuration — an env fallback would let a stale value silently auto-answer every future question-stop on the project. (A future Track-B remote-delivery leg may add a consume-once delivery channel.) At session start, `work session-start` applies it automatically when the resolved run is actually stopped on a recorded question: it appends a `question_answered` event carrying both texts (secret-redacted), clears the question stop, and prints a resume brief that leads with the question+answer pair — so a fresh session resumes with clean context instead of reviving the session that asked. When the run has no open question the value is ignored (fail-soft). See docs/RUN-STATE.md §2.

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

- **`LMER_PRESETS_FILE`** - Read **host-side** (by `lmer-slack-listener` and the `lmer` CLI): path to a JSON file of named startup presets — operator-defined bundles of `checkout`/`service`/`env`/`args` selectable with a `$preset:<name>` token in a Slack triggering message or with `lmer --preset <name>` / `LMER_PRESET` on a direct CLI invocation. Unset (the default) disables the feature. File format, field reference, validation rules, trust model, and per-consumer merge semantics: [docs/PRESETS.md](./PRESETS.md).

- **`LMER_PRESET`** - Read **host-side by the `lmer` CLI**: name of a preset from `LMER_PRESETS_FILE` to apply to the invocation, e.g. `LMER_PRESET=my-preset lmer develop <url>`. The `--preset` flag wins over it (matching `--harness`/`LMER_HARNESS`). Also honored from `.env` files (cwd, `~/.lmer/.env`, `--env-file`), so a project directory can pin a default preset. The explicit invocation always wins over the preset, and an unknown name fails fast (exit 2) listing the available presets; a blank value counts as unset (a whitespace-only value used to fail fast — as of #140 it is treated like the empty value, which already meant "no preset"). Never needs to reach inside the container — the preset's *effects* (flags, forwarded env vars) do instead. See [Startup presets](#startup-presets---preset--lmer_preset) and [docs/PRESETS.md](./PRESETS.md).

- **`LMER_<TASK>_PRESET`** - Read **host-side by the `lmer` CLI**: the taskdef-scoped form of `LMER_PRESET` (issue #140) — it selects a preset for that one taskdef, e.g. `LMER_REVIEW_PRESET=sol-review` applies to `lmer review <mr-url>` and to no other task. The var name is derived from the taskdef id: uppercased, with every non-alphanumeric character folded to an underscore (`review` → `LMER_REVIEW_PRESET`, `code-review` → `LMER_CODE_REVIEW_PRESET`), which covers work-repo and `LMER_TASKDEF_PATHS` taskdefs as well as the built-in ones. Selection order is `--preset` > `LMER_<TASK>_PRESET` > `LMER_PRESET` — the flag always wins, and the taskdef-scoped var beats the generic one because it is the more specific selector, so `~/.lmer/.env` can pair a global default with per-task overrides. **The order is by specificity, not by source tier**: a `.env`-sourced scoped var outranks even an exported `LMER_PRESET` (deliberate — a per-task default an unrelated export could silently disable would be useless — and the only place lmer resolves file-versus-export in the file's favor, so that specific combination prints a warning naming both sides; `--preset` is the per-invocation override). Also honored from `.env` files, and applied with the same defaults-only merge semantics and guard rails as `LMER_PRESET`: an unknown name fails fast (exit 2) listing the available presets, with the selecting variable named in the error (a `--verbose` run also names it in the `🎛️  Preset:` line) so a stale value in a `.env` file is traceable. A blank value counts as unset and falls through to `LMER_PRESET`. The derivation is many-to-one (`code-review`, `code_review` and `code.review` share one variable) and an id with no ASCII alphanumerics has no scoped variable at all — see [docs/PRESETS.md](./PRESETS.md#per-taskdef-presets-lmer_task_preset). A `--no-task` invocation has no taskdef id, so only `--preset`/`LMER_PRESET` apply to it. Never needs to reach inside the container. **One instance also reaches the Slack listener:** `LMER_CHAT_PRESET` in the listener's environment applies to every session it spawns, since each is an `lmer chat` invocation inheriting that environment — a supported listener-wide default that a `$preset:` token displaces whole (see [The listener-wide default](./PRESETS.md#the-listener-wide-default-lmer_chat_preset)). See [Startup presets](#startup-presets---preset--lmer_preset) and [docs/PRESETS.md](./PRESETS.md).

- **`LMER_AGENTS`** - Read **host-side by the `lmer` CLI**: comma-delimited preset names the session's agent may fan a task out to via the in-container `spawn-harness` tool (issue #130), e.g. `LMER_AGENTS=sol-review,opus-review lmer review <mr-url>`. The `--agents` flag wins over it (matching `--preset`/`LMER_PRESET`); also honored from `.env` files and from a preset's own `env`. Each name is resolved against `LMER_PRESETS_FILE` **before the container starts** — a name matching no preset falls back to the model route when its model family implies a harness (`fable` → claude, like `LMER_LLM_NAME` autoselection), a name that is neither fails fast (exit 2), an unknown harness fails fast too, a duplicate warns and keeps the first occurrence, `--harness`/`--prompt` in the preset's `args` fold into the child config (harness into the env overlay, prompt as a preamble), and the remaining launch-shaping preset config (`checkout`/`service`, other args) is ignored with a warning (children are subprocesses of the session container, not new sessions). Credential files of every implied child harness are mounted alongside the session harness's, so a child routed to a different harness can authenticate (e.g. `~/.codex/auth.json` for a codex-routed child of a claude session); an implied child harness with no mountable credential file on the host produces a launch-time warning, never an error (#131). The resolved names cross into the container under a **different** name, `LMER_SPAWN_AGENTS` (issue #283) — this variable is a host input only, and is not set inside the container. See [Fan-out agents in docs/PRESETS.md](./PRESETS.md#fan-out-agents---agents--lmer_agents).

- **`LMER_SPAWN_AGENTS`** / **`LMER_SPAWN_AGENTS_CONFIG`** - Set **by lmer inside the container** (never host inputs, and read by nothing host-side): the resolved `--agents`/`LMER_AGENTS` selection as it reaches the session — the names comma-delimited, and a JSON object `{"<name>": {"env": {...}, "prompt": "..."?}}` carrying each preset's env overlay plus an optional prompt preamble folded from its `args`. Consumed by `spawn-harness` to configure child harness processes, and available to taskdef templates — `hooks/start.py` builds the jinja context from the container's `LMER_`-prefixed environment, so this scoped name is what a taskdef sees and the host input `LMER_AGENTS` is not in the context at all. No taskdef in this repo reads it; the taskdefs that render fan-out instructions live in the work repo, and a taskdef gating such a section must gate on `LMER_SPAWN_AGENTS` (or on `LMER_SPAWN_AGENTS or LMER_AGENTS` to stay renderable under a pre-#283 image) — a gate on `LMER_AGENTS` alone goes falsy and drops the section with no error anywhere. The presets file itself never enters the container — the agent can only spawn what was named at launch. The scoped spelling is what keeps the selection from spreading: container env is ambient, so under the host input name a nested `lmer` invocation (or a test run) inherited the outer session's fan-out and tried to resolve those names against a presets file that is not there (issue #283). `spawn-harness` strips both spellings from every child it runs. Note the overlays may carry preset-supplied credentials, the same exposure class as CLI-preset env forwarding.

- **`LMER_SLACK_CHANNEL`** / **`LMER_SLACK_THREAD_TS`** - Set **by lmer inside the container** (not host inputs): the channel ID and thread timestamp parsed from the first Slack thread permalink target given to `lmer chat`. Their presence switches the `chat` taskdef into Slack conversation mode and supplies the default channel/thread for `lmer-slack` invocations (overridable per-invocation with `--permalink`). Empty/unset when no Slack target was given.

- **`LMER_SLACK_PERMALINK`** - Also set **by lmer inside the container**: the original Slack thread permalink URL the channel/thread values were derived from, kept for reference and diagnostics.

- **`LMER_SUPERVISOR_PID`** - Set **by `lmer-supervisor` inside the container** (not a host input): the supervisor's own PID, exported before it forks Claude so the wrapped process and everything it spawns inherit it. An in-container command can send this PID `SIGUSR1` to request a graceful self-shutdown — the supervisor injects Claude's quit chord (Ctrl-C twice), escalating to SIGTERM/SIGKILL if needed, and reports a clean exit. `lmer-slack end-session` uses this to let a Slack chat session free its orchestrator slot on demand. Unset when Claude runs without the supervisor (`LMER_DISABLE_SUPERVISOR=1`).

- **`LMER_PLATFORM_UI_DIST`** - Path (an absolute one is expected; a relative value is resolved against the daemon's working directory, the same tolerance `LMER_PLATFORM_WEB_DIR` has) to an already-built control-UI bundle (a Vite `dist/` directory holding `index.html`) for the **platform daemon** to serve. Read by the daemon process itself (`lmer_platform.ui_build.dist_dir`), not by session containers, so it needs no container passthrough. It is the *first* place the daemon looks — ahead of the copy `lmer platform setup-ui` installs into `~/.lmer/platform/ui` and ahead of a developer's `web/dist` — because an explicitly configured bundle is a deliberate choice, and because a container mounting the host's platform state dir must not end up serving a UI the host's (possibly older) lmer built against a different API. The platform container image (`Dockerfile.platform`, issue #150) builds the UI during the image build and sets this to `/opt/lmer/ui`, so a pull replaces the host-toolchain `setup-ui` step entirely. A path that does not exist, or a directory with no `index.html`, is skipped rather than turning the UI off — the daemon falls through to the next candidate and, failing all of them, serves the JSON API and says so at startup. Blank counts as unset, and **unset has no effect**: resolution is exactly what it was before this variable existed.

- **`LMER_PLATFORM_ASSISTANT_MODEL`** / **`LMER_PLATFORM_ASSISTANT_HARNESS`** / **`LMER_PLATFORM_ASSISTANT_PRESET`** / **`LMER_PLATFORM_ASSISTANT_AGENTS`** - How the **platform daemon** runs its orchestrating assistant ("uber lmer"), per platform instance (#234). Each maps to the flag of the same name on the assistant's `lmer` invocation (`--model`, `--harness`, `--preset`, `--agents`) and is read by the daemon process itself, not inside session containers, so none needs container passthrough. Resolution per key is the platform's usual chain — an explicit `POST /api/assistant/start`/`rotate` body value > this env var > the `assistant_model`/`assistant_harness`/`assistant_preset`/`assistant_agents` keys in `~/.lmer/platform/config.json` (written by `POST /api/assistant/config` or the chat drawer's settings dialog) > unset, which is the pre-#234 behaviour (the session runs whatever its environment and harness settle on). Resolved **fresh at every assistant start and rotate**, so a persisted change applies to the next incarnation without a daemon restart — but an export set here shadows the persisted value for as long as the daemon carries it (`GET /api/assistant/config` names each value's source for exactly this reason). Harness and preset names are checked against the host's own authorities (installed harnesses, case-insensitively; `LMER_PRESETS_FILE`); an agents selection is asked of `lmer`'s own `--agents` resolver, so a member matching no preset is accepted when its model family implies a harness (the model route — `fable` works on a preset-less host) and refused otherwise. A value the spawned `lmer` would exit 2 on is refused at the explicit surfaces (the API answers 400 naming the field and the catalog) and warned-and-skipped in the standing layers; model names stay verbatim (no host-side authority exists — the harness refuses what it does not know). Unusable values (blank, non-text, dash-leading, over-long, an agents selection `lmer` would refuse, or an unknown harness/preset name) make that layer resolve as unset — falling through to the next layer down — rather than refusing to start the assistant. Blank counts as unset.

- **`LMER_PLATFORM_CHECKIN_WINDOW_SECONDS`** - How long a run may go unchecked before the **platform daemon** spools a digest naming it (#244), in seconds. Default `3600` (one hour); `0` turns check-in digests off entirely. Read by the daemon process itself on every detection tick, not inside session containers, so it needs no container passthrough. Resolution is the platform's usual chain — this env var > the `checkin_window_seconds` key in `~/.lmer/platform/config.json` (written by `POST /api/assistant/config`) > the default — and it is re-read on each tick, so a persisted change applies without a daemon restart or an assistant rotation (`GET /api/assistant/config` reports the value under `checkin` with the layer that decided it). What the window measures is the time since the **assistant** last named the run on a run-addressed route, or since a digest last named it, or since the daemon first saw it — whichever is most recent; the operator's own reads through the browser do not count, which is why the assistant runs on a credential of its own. Numeric text is accepted (`"7200"`); anything else warns and resolves as if this layer were unset, rather than stopping the daemon over a reminder interval. Blank counts as unset.

- **`LMER_PLATFORM_NUDGE_AFTER_SECONDS`** / **`LMER_PLATFORM_NUDGE_PENDING_THRESHOLD`** - When the **platform daemon** types a reminder into its orchestrating assistant's session because spooled digests have gone unretrieved (#317). Defaults `180` (three minutes) and `1` digest: a nudge fires once that many digests have waited that long beside a quiet assistant. The reminder says only that *something is waiting* — it never carries a digest, and `POST /api/assistant/pending` remains the only way to take one. `LMER_PLATFORM_NUDGE_AFTER_SECONDS=0` turns the nudge off and is the only off-switch. Both resolve on the platform's usual chain — this env var > the `nudge_after_seconds` / `nudge_pending_threshold` keys in `~/.lmer/platform/config.json` (written by `POST /api/assistant/config`, which serves them under `nudge`) > the default — and are re-read every detection tick, so a change needs no daemon restart. Read by the daemon itself, so neither needs container passthrough. Full behaviour — the five conditions, the once-per-interval stamp and how a lost reminder self-heals, the error paths, and the threshold's floor and ceiling — is in [Digest nudges in docs/PLATFORM-QUICKSTART.md](./PLATFORM-QUICKSTART.md#digest-nudges).

- **`LMER_ASK_DIR`** - Set **by the platform daemon** and forwarded into the container by lmer (not a host input, and there is no flag for it): the *container* path of the operator ask channel the platform bind-mounts for a session it spawned — `/home/developer/.lmer-ask`, a dedicated directory rather than a subdirectory of the `~/.lmer` mount, so the result does not depend on mount order. Its presence is the single switch deciding whether a session gets the `lmer-ask` contract at all: the session-context builders (`libexec/claude-runner.sh`, `libexec/harness-common.sh`) render the ask-channel prompt fragment only when it is set and non-blank, and the `lmer-ask` CLI exits rather than inventing a directory nobody is watching when it is unset. So an unset value means the session asks its questions into its own terminal — worth checking first when an orchestrated session's question never reaches the web view. Setting it by hand does nothing useful: a value without the matching mount is a session that blocks forever on answers nobody can write. The operator's half of the channel — answering — is in [Using it in docs/PLATFORM-QUICKSTART.md](./PLATFORM-QUICKSTART.md#using-it).

- **`LMER_SERVICE_GROUP`** - Set **by lmer inside the container** (not a host input; sourced from `--service-group`, or a preset's `service_group`): the compose project this session is attached to. `target-switch` resolves the group's members from it on every call, the service-mode taskdef prompt renders the group's instructions off it, and `target-exec`/`target-logs` use its presence to tell "no target chosen yet" from "not in service mode". Unset for single-service and non-service sessions.

- **`LMER_SERVICE_TARGET_FILE`** - Also set **by lmer inside the container**, for group sessions only: the file `target-switch` records the current target in (service name, container id and workdir, one per line), defaulting to `/home/developer/.lmer-session/service-target`. `target-exec` and `target-logs` read it on every invocation — which is what makes a switch visible to shells that started before it — and fall back to `LMER_SERVICE_CONTAINER` when it does not exist. See [Service groups](./SERVICE-MODE.md#service-groups-one-session-a-whole-stack).

- **`LMER_NO_REPO`** - Set **by lmer inside the container** (not a host input) to `1` when the session deliberately has no repository — currently only when a Slack thread permalink is the sole `lmer chat` target and no git origin could be inferred from the current directory. The container's clone step is skipped (`/workspace` stays empty) and the chat taskdef drops its repository-specific instructions. Unset for all repository-backed sessions.

#### Codex ask continuation

The lmer image installs `hooks/codex_ask_guard.py` through the system-managed
`/etc/codex/requirements.toml` and pins Codex's `hooks` feature on. In an
interactive orchestrated session, an unread answer immediately triggers a
native Stop-block continuation; otherwise, while any question remains open,
the hook waits for the first answer. It always selects the oldest answer without
a `.read.json` receipt, whether that answer arrived before or during Stop, and
tells the agent to run `lmer-ask wait`. The hook never reads or embeds the
answer; `lmer-ask wait` still prints it through the normal terminal path. A
close with no other open question, channel errors, repeated Stop-hook turns and
`LMER_NONINTERACTIVE` children fail open. The timeout also fails open, after
holding Stop for at most 3540 seconds. The managed source is trusted without
disabling the ordinary trust review for user or project hooks.

This is the one Codex lifecycle guard lmer currently installs. The signal,
run-state, SessionEnd and Slack guards described elsewhere remain Claude-only.

### Canonical Source Declarations (`sources.yaml`)

The work repo can declare the canonical taskdef and napkin sources in a `sources.yaml` file at its root. This makes the work repo itself the source of truth for where shared taskdefs and napkin notes come from, and turns the `LMER_TASKDEF_REPO` / `LMER_NAPKIN_REPO` / `LMER_TASKDEF_REF` environment variables into explicit, mismatch-checked overrides instead of the only configuration mechanism. Resolution runs in-container (in `clone_and_exec`, after the work-repo clone and before the auxiliary clones), where `sources.yaml` is guaranteed present and fresh.

#### Schema (version 1)

```yaml
schema: 1
sources:
  taskdef:
    repo: https://gitlab.example.com/group/taskdefs.git
    ref: main                # optional; overrides the LMER_TASKDEF_REF default
  napkin:
    repo: https://gitlab.example.com/group/napkin.git
```

- The `sources` mapping is deliberately **profile-shaped**: it is the exact shape intended to become the `sources` section of a future named profile. No profile implementation exists today — only the shape is reserved.
- Schema 1 covers `taskdef` and `napkin` only. `masterplan_mirror` is a documented **reserved** future key and is deliberately **not** part of schema 1.
- `ref` is valid under `taskdef` only in schema 1 (there is no `LMER_NAPKIN_REF`). It participates in the same resolution matrix as `repo`, per field — see below.
- Unknown keys under `sources:` and unknown top-level keys produce a **warning** (forward compatibility); an unknown `schema:` version **fails loud** (same pattern as `taskdef.yaml`).

#### Same-host trust rule

Declared repos must live on the **same host** as the work repo itself. The rationale: the work repo is agent-writable, and a declared source URL inherits the work-repo credential at clone time — an unrestricted declaration would let a committed `sources.yaml` route an operator token to an arbitrary host. Same-host-only kills that: the credential a declared URL receives is the one the work-repo URL already carries for that same host. A declaration on any other host is a **validation error** whose message points at the env-var override (`LMER_TASKDEF_REPO` / `LMER_NAPKIN_REPO`), which stays operator-controlled.

The comparison is on the **hostname alone** — scheme and port do not participate. An SSH work repo on `ssh://git@host:2222/...` therefore accepts an `https://host/...` declaration, which is an ordinary self-hosted layout and one the credential derivation below already handles. The rule is about *which host* receives the operator's credential; a second port on the host that already holds it is not a different trust boundary.

Accepting the declaration is not the same as handing it the credential, though. A declared HTTPS URL naming a **different port** than the work repo's — `https://host:5050/...` when the work repo is `https://host/...` — is cloned **anonymously, with the work-repo credential withheld**: a git host commonly runs other services on other ports, and `oauth2:<token>@host:5050` would send the operator's token to one of them as basic auth. (The same applies to a plain `http://` declaration against an HTTPS work repo.) A public repo on another port still clones; a private one fails through the loud declared-source failure path below, whose message names the withheld credential. If a declared source genuinely needs a credential on another port, set that source's env-var override, which is yours to set rather than the work repo's to state.

**Credential scope requirement:** a declared source is cloned with the credential **derived from the work-repo URL** for that same host — an HTTPS work-repo URL has its userinfo (`oauth2:<token>@`) copied onto the declared URL; an SSH-form work-repo URL causes the declared URL to be converted to the same SSH form so the same key applies. The work-repo credential must therefore have **read access** to every declared repo. If it does not, use the env-var override for that source instead (env-var URLs are credentialed host-side, exactly as today).

A declared-source clone failure is **loud**, never the warn-and-continue that legacy env-var auxiliary clones get: interactively, lmer prompts to either continue in legacy mode (as if that source were undeclared) or abort; headless, it exits 2 with a remediation message (fix the declaration, widen the credential's read scope, or set the env-var override). A declaration is a stated intent — silently proceeding without it would hand the session the wrong taskdefs or napkin.

#### Resolution

Resolution applies **per source** (`taskdef`, `napkin`) **and per field** (`repo`, plus `ref` for taskdef) — a ref-only mismatch is still a mismatch and triggers the mismatch row below.

| declared | env var | behavior |
|---|---|---|
| yes | unset | Use the declaration (the new normal path); the clone URL derives its credential from the work-repo URL. |
| yes | equal (normalized) | The **env value wins, silently** — it supplies authentication for the same source; the retained container value is clean after the credential moves to its session file. |
| yes | different | **Interactive** (stdin is a TTY): a stop-and-ask prompt naming both values (credential-scrubbed), you pick one. **Headless**: exit 2 before any auxiliary clone, with an error naming both scrubbed values and the fix. |
| no | set | Use the env value, with a one-line note that no declaration exists. |
| no | unset | **Silent legacy mode** — behavior identical to a setup without `sources.yaml`. No per-session warning. |

Equality is decided on normalized URLs (credentials stripped, trailing `.git` and slash dropped, host lowercased, default ports removed, SSH forms canonicalized), so cosmetic differences never count as a mismatch. Non-default ports are **kept** by that comparison — `host:2222/x` and `host:9999/x` can be genuinely different repos — with one addition: an env value that matches the URL lmer would *derive* from the declaration counts as equal too. That is what makes the `equal (normalized)` row fire for a work repo on a custom SSH port, where the env var holds the working `ssh://git@host:2222/...` form while the declaration names the clean `https://host/...` one. Only the rewrite the derivation itself performs is folded in; any other port difference is still a mismatch.

#### No secrets, ever

The work repo is shared, so declarations never contain tokens. Host-resolved clone credentials move into mode-`0600` session files immediately before Git runs; clone argv and retained container URL variables are clean, remotes show clean URLs, and repository config contains only non-secret helper references. Password-style and username-only HTTPS userinfo both cross this boundary; bare SSH forms — `git@host:path` and `ssh://git@host/path` — are protocol userinfo and remain unchanged. Existing operator-owned `--checkout` and service-mode bind mounts receive no repository-local helper or remote rewrite. Credential detection for shared declarations is unchanged: a URL whose userinfo carries a password/token component — `https://user:secret@host/...`, `https://oauth2:<token>@host/...` — is a refuse-start validation **error** telling the operator to strip it. Every place a declared or env URL is rendered (the mismatch prompt, the headless error, `lmer --show-env`, log lines) shows it credential-scrubbed.

Detection and scrubbing both cover secrets containing a `/` (the base64 alphabet has one), even though git itself will not clone such a URL unless the `/` is percent-encoded — the point is that the secret is refused and never printed, not that the URL works. The trade-off is a deliberate over-scrub in one exotic shape: a URL with **both** a non-default port and an `@` in its path (`https://host:8443/a/repo@v1.git`) is redacted as if the port were a secret. Redaction's rule is to over-scrub rather than risk a leak.

One shape has no single right reading and is **refused as unreadable** rather than guessed at: a URL with **no scheme** whose `@` comes after a `/` — `a:b/c@d`. `user:se/cret@host/org/x.git` (a secret containing `/`) and `host:org/re@po.git` (an scp-form path containing `@`) are the same string shape, so any rule that reads one correctly misreads the other. The refusal names both readings, so whichever one you meant, the fix is in the message: strip the credential, or write the scheme form (`https://host/org/re@po.git`), which parses unambiguously. Redaction is regex-based and independent of that parse, so such a URL is still scrubbed wherever it is printed.

#### Backward compatibility (silent legacy)

Backward compatibility is absolute. An **absent `sources.yaml`** means the resolution matrix never engages — zero new output, zero behavior change. A **present file with a source key absent** is likewise silent legacy for that source: declarations are opt-in per source. Visibility comes from the `lmer --show-env` origin display (`declared` / `env-match` / `env-override` / `env-only` / `unset-fallback` — `env-match` is the normalized-equal row, where the env value is the credentialed form of the declaration rather than an override), the container-side resolution banner, and the in-container doctor check — not from per-session nagging.

On the host side, `--show-env` reads the declaration from the local clone cache, so it also distinguishes *no declaration* from *no cache*: a warm mirror whose work repo carries no `sources.yaml` renders `declared: none (no sources.yaml in the work repo)`, while `declared: unknown (work repo not cached)` means the mirror itself is cold or unusable and says nothing about whether declarations exist.

#### Known trade-off: declared-only sources are not clone-cache accelerated

The clone-cache updater (see `LMER_CLONE_CACHE`) warms mirrors for the repos named by `LMER_TASKDEF_REPO` / `LMER_NAPKIN_REPO`. A source configured **only** in `sources.yaml` is not among them: the declaration is authoritative container-side, and host-side it is knowable only through the same best-effort (possibly stale) cache read `--show-env` labels as such. A source you migrate from the env var to a declaration therefore loses warm-mirror acceleration and is cloned directly every session. Correctness is unaffected — this is exactly the cold-cache path lmer used before the cache existed — but if clone time matters for a large taskdef repo, keeping the env var set alongside the declaration (the normalized-equal `env-match` row, which is silent and non-conflicting) keeps the mirror warm.

#### `bin/doctor`

Inside the container, `bin/doctor`'s `🔗 Declared Sources` section reports on the declaration: schema validity, reachability of each declared source (`git ls-remote` with a disposable copy of the work-repo session credential attached through process-scoped Git config), whether the declared `ref` exists on the remote, and whether the cloned taskdef repo's `taskdef.yaml` schema is supported. The live credential remains unchanged in its mode-`0600` file even when Git approves or rejects the probe credential; the probe's argv and URL are clean. Every finding is a **warning** — a broken declaration never flips doctor's exit code. The underlying helper CLI (`python3 -m lmer_cli.container.sources doctor --json`) reads `${LMER_WORK_REPO_PATH:-/work}/sources.yaml` by default; pass `--path` to point it elsewhere.

### Work-Repo Claude Assets

The work repository can contribute Claude Code slash commands, skills, subagent definitions, output styles, and a limited slice of `settings.json` to every session that uses it. This is the supported way to ship project-specific automation, runbooks, or pre-authorized tool patterns across all developers who share the work repo.

Layout (relative to the work-repo root, i.e. `/work/agent-files/claude/` inside the container):

```
agent-files/claude/
├── commands/
│   └── deploy.md            # Available as /deploy in the session
├── skills/
│   └── runbook-xyz/
│       └── SKILL.md         # Auto-discovered Claude Code skill
├── agents/
│   └── reviewer.md          # Subagent definition (*.md only)
├── output-styles/
│   └── terse.md             # Selectable Claude Code output style (*.md only)
└── settings.json            # Only permissions.allow is merged
```

At container start, `claude-runner.sh` does the following:

- Symlinks every entry under `agent-files/claude/commands/` into `~/.claude/commands/` so the files are visible to Claude Code's slash-command loader.
- Symlinks every skill directory under `agent-files/claude/skills/` into `~/.claude/skills/` so Claude Code's skill discovery picks them up.
- Symlinks every entry under `agent-files/claude/agents/` into `~/.claude/agents/`, where Claude Code discovers subagent definitions. (`LMER_DISPATCH_<LANE>` may then replace a linked lane definition with a rendered copy carrying the configured model/effort — see the `LMER_DISPATCH_*` entry under [LMER-Specific Environment Variables](#lmer-specific-environment-variables).)
- Symlinks every entry under `agent-files/claude/output-styles/` into `~/.claude/output-styles/`, where Claude Code discovers output styles.
- If `agent-files/claude/settings.json` exists, merges its `permissions.allow` array into `~/.claude/settings.json` (deduplicated). No other keys are honored — work-repo `settings.json` cannot, for example, add a `deny` entry, change the status-line command, or select an output style, so a misconfigured work repo cannot weaken protections that live in the global settings.

Both sources (lmer global tree at `/Agents/global/agent-files/claude/`, plus the work repo) are overlaid, with work-repo entries overriding global ones on name collision. Per Claude Code's skill loader, changes to skills under `~/.claude/skills/` take effect immediately within the running session — adding a new file in the work repo and re-syncing does not require a container restart.

Two properties of the loaders are worth knowing when you author these files (checked against Claude Code 2.1.221):

- **Only `*.md` files are loaded** from `agents/` and `output-styles/`, while the runner links *every* entry it finds. A `terse.markdown` or `terse.md.j2` is linked and then silently ignored; subdirectories are descended into, so `output-styles/team/terse.md` does load.
- **Overriding requires reusing the filename.** The runner's shadowing is by filename, but a file identifies itself by its frontmatter `name`. A work-repo `output-styles/team.md` with `name: lmer` therefore does not replace a global `output-styles/lmer.md` — both are linked and both load. Keep `name` equal to the filename stem. The two loaders differ on what happens when it is absent: a **style** falls back to the filename stem, so omitting `name` is safe there, while a **subagent definition without a `name` is dropped and nothing is logged** — the agent is simply not there. Always give an agent def a `name`.

#### Output styles: shipping one and selecting one are separate

An output style replaces Claude Code's built-in software-engineering system prompt for the main conversation (unless the style sets `keep-coding-instructions: true`), and it does **not** reach subagents — the `adversarial-reviewer`, `coder`, `explorer` and other agent definitions run their own prompts either way. Styles are also Claude-only: codex and pi have no equivalent, so a behavior every harness must follow belongs in the taskdef prompt rather than in a style.

Delivering the file is what the layout above does. *Selecting* it is the `outputStyle` settings key, and not every source that can ship a style can also select one:

| Source | Ships a style file | Can set `outputStyle` |
|---|:---:|:---:|
| lmer global tree (`agent-files/claude/`) | ✅ | ✅ — its `settings.json` becomes `~/.claude/settings.json` (symlinked normally; in danger zone the entrypoint copies it and replaces only the `permissions` object, so other keys survive) |
| Work repo (`/work/agent-files/claude/`) | ✅ | ❌ — only `permissions.allow` is merged from a work-repo `settings.json` |

So a work repo can put a style in front of every session that uses it, but it can never *automatically* activate one — that stays with the lmer global tree. This is deliberate (#252): a shared work repo replacing the main agent's system prompt is a much larger lever than the permission grants that merge is limited to. A shipped style is still offered in the `/config` picker, and the effective `~/.claude/settings.json` is a writable regular file after the merge, so a person in the session can always select a style by hand; what the boundary rules out is a work repo doing it unattended.

`claude-runner.sh` has a third branch that merges `~/.claude/settings.local.json` into the effective settings, which would carry an `outputStyle`. The merge itself works (its `jq` filter was fixed in #251), but it is not a delivery path: **nothing mounts a host file to that container path today**, and the work-repo tree cannot supply one either — the agent-files loop links only `commands/`, `skills/`, `agents/` and `output-styles/`. The branch runs only for a file something already placed inside the container at `/home/developer/.claude/settings.local.json` (or wherever `LMER_SETTINGS_LOCAL_FILE` points) — an explicit `--mount-file`/`LMER_MOUNT_FILES` bind at that path, a taskdef step, or a future mount.

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

Current Codex releases no longer discover custom prompt files, so neither
`/followup` nor the former `/prompts:followup` spelling works in Codex's own
terminal. There, type this plain-text instruction:

```text
Run bash /Agents/global/hooks/followup.sh now and follow the instructions in its output.
```

A whole `/followup` submitted through lmer's control plane is translated to
that instruction automatically; arguments and line endings are preserved. The
delivery receipt still hashes the original `/followup` bytes.

The `/followup` command loads `followup.txt` from the active task definition directory and renders it with the same Jinja2 context as `/start` (the filtered `LMER_*` context — see [TASKDEFS.md](./TASKDEFS.md)). If a task type does not provide a `followup.txt`, the command exits with an error pointing at where it looked. Task types opt in simply by adding the file — no code change in lmer is required.

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

- **Mention outside a thread** — the mention message becomes a new thread's parent and a session is attached to it. **Mention inside a thread** — a session is attached only if none is already connected (a live session sees the message through its own polling). **DMs** — a non-bot DM connects a session the same way; one session runs per DM conversation (a new top-level DM while one is live is pointed back at the active thread).
- **One lmer per thread, even for sessions the listener didn't spawn.** The listener won't connect a second lmer to a thread that already has one — including a session you started yourself with `lmer chat <permalink>` from a shell. Every host-side `lmer chat` session attached to a Slack thread records its attachment in a small registry under `~/.lmer/slack-sessions/` (keyed by channel + thread_ts) and clears it on exit; before spawning, the listener checks that registry and stays silent when a live session is already attached. A stale entry left by an unclean death is detected via the recorded process PID and ignored, so a crash never permanently blocks a thread. This relies on the manual session running on the **same host** as the listener (the shared state dir), which is the normal arrangement since both need the same `lmer` CLI and container runtime.
- Every message in a connected thread — yours or the agent's own posts — resets that session's idle timer. After `LMER_SLACK_CHAT_IDLE_TIMEOUT_MINUTES` of silence the session is disconnected and a reconnect hint is posted; mentioning the bot again spawns a fresh session that reads the thread history. A crashed session posts the same hint; a clean sign-off leaves quietly. When the human signals the conversation is over, the agent can also end the session itself with `lmer-slack end-session` (typically after a goodbye), freeing the slot immediately rather than holding it until the idle timeout.
- At most `LMER_SLACK_CHAT_MAX_SESSIONS` sessions run at once. DM access can be restricted with `LMER_SLACK_DM_ALLOWED_USERS`. See [Environment Variables](#environment-variables) for the full `LMER_SLACK_CHAT_*` / `LMER_SLACK_DM_ALLOWED_USERS` set.

It must run **on a host** (not inside a container): lmer launches a container per session, so the listener has to sit alongside those containers, not within one. Spawned sessions inherit the listener's full environment, so any lmer configuration (`LMER_IMAGE`, git tokens, model API keys, `SLACK_BOT_TOKEN`, ...) in the listener's `.env` reaches the sessions automatically.

##### Service-mode presets (`$preset:<name>`)

By default every spawned session is a generic, repo-less `lmer chat`. **Startup presets** let the operator pre-define named startup configurations that a Slack user can opt into — for example to start a session in **service mode** (`--service` + `--checkout`) against a specific running stack — without ever exposing raw paths or flags to Slack. The user picks a configuration *by name*; the operator controls what each name maps to.

The preset system (the `LMER_PRESETS_FILE` JSON file, field reference, validation rules, and trust model) is shared with direct CLI invocations and documented in [docs/PRESETS.md](./PRESETS.md). Slack-specific behavior:

- A user selects a preset with a `$preset:<name>` token anywhere in the message that **starts** the session:

  ```
  @lmer-bot $preset:my_service can you check why the worker queue is backed up?
  ```

  The listener then spawns, e.g., `lmer chat <permalink> --checkout /srv/my-service --service mysvc --ports 2` with the preset's `env` in its environment, and the connecting ack names the applied preset.
- **The preset wins** on env conflicts: its `env` is merged over the listener's inherited environment, and its `args` are appended verbatim to the spawned `lmer chat <permalink>` command. (On the CLI path the precedence is reversed — the explicit invocation wins; see [Merge semantics per consumer](./PRESETS.md#merge-semantics-per-consumer).)
- **Unknown name** → the listener rejects it with a thread reply listing the available presets and does not spawn.
- **Already-connected thread** → the token is moot (the live session handles the new message), so a `$preset:` token only takes effect on the message that *starts* a session.
- **The token is not the only selector.** `LMER_CHAT_PRESET` (or `LMER_PRESET`) in the *listener's own* environment applies a preset to every session it spawns — a supported listener-wide default, since each session is an `lmer chat` invocation inheriting that environment. A `$preset:` token **displaces** the default entirely rather than stacking with it, and the ack names whichever preset is in effect. Full behavior, precedence, and the undefined-default warning: [The listener-wide default in docs/PRESETS.md](./PRESETS.md#the-listener-wide-default-lmer_chat_preset).

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
- `--answer <text>` - Answer to the run's recorded open question. Forwarded to the container as `LMER_ANSWER` (this flag is the ONLY source — a host-exported or `.env` `LMER_ANSWER` is never forwarded); `work session-start` applies it before printing the resume brief (records a `question_answered` event, clears the question stop) so the fresh session resumes seeded with the question+answer pair. Ignored when the run isn't stopped on a recorded question. See `LMER_ANSWER` above and docs/RUN-STATE.md §2
- `--fastapi` - Expose a FastAPI endpoint inside the container that lets a controlling process drive Claude's stdin/stdout (`POST /input`, `GET /output`). The chosen port is published to `127.0.0.1` on the host. See [Supervisor and FastAPI Endpoint](#supervisor-and-fastapi-endpoint)
- `--fastapi-port-range LOW-HIGH` - Port range to pick a free FastAPI port from (default `8700-8799`). The host CLI picks one free port from this range before container start and publishes only that port
- `--fastapi-host <host>` - Inside-container bind host for the FastAPI endpoint (default `0.0.0.0` when `--fastapi` is set so the published port works)
- `--fastapi-token <token>` - Bearer token to require on FastAPI requests. If omitted a random token is generated and printed to stderr on startup
- `--ports <N>` - Allocate `N` free host ports and publish them into the container so a service Claude starts inside (e.g. a dev web server) is reachable from the host. The host CLI picks `N` currently-free ports from `--port-pool` before the container starts, publishes each on the host (loopback `127.0.0.1` by default, override with `--port-bind`) with the same port number inside and out, and exports the list to the container as `LMER_PORTS`. Startup aborts if `N` free ports can't be found. Also settable via `LMER_PORT_COUNT` (the flag wins). Bind services to `0.0.0.0` inside the container so the published mapping works. See [Port Passthrough](#port-passthrough)
- `--port-pool LOW-HIGH` - Inclusive port pool the `--ports` ports are picked from (default `8800-8899`, distinct from the FastAPI range so both features coexist). Also settable via `LMER_PORT_POOL` (the flag wins)
- `--port-bind <addr>` - Host bind address used when publishing the allocated `--ports` mappings (default `127.0.0.1`). Pass `0.0.0.0` to expose the ports on every host interface (so other machines on the LAN can reach a service Claude starts inside), or a specific IP to publish only on that interface. The address is also used to probe for free ports in the pool, so the picked ports are guaranteed bindable there. Also settable via `LMER_PORT_BIND` (the flag wins). The default is loopback for a reason — only widen it when you trust both the network and what the agent is running
- `--service <name>` - Docker/Compose service (or exact container name) to run `target-exec` commands in ([service mode](./SERVICE-MODE.md)). Requires `--checkout`. **Note:** since `--service-group` joined the parser, abbreviations of this flag (`--serv`, `--se`) are ambiguous and argparse refuses them; the full spelling is unchanged
- `--service-group <project>` - Compose **project** whose running services this session may target ([service groups](./SERVICE-MODE.md#service-groups-one-session-a-whole-stack)). Members are discovered from the `com.docker.compose.project` label, and the agent retargets `target-exec`/`target-logs` at any of them with the in-container `target-switch` — one session for a stack instead of one per container. Requires `--checkout`; combine with `--service <member>` to name the service the session starts on, otherwise it starts with nothing targeted
- `--checkout <path>` - Mount an existing local checkout as `/workspace` instead of cloning. Usable on its own; required by `--service` and `--service-group`
- `--preset <name>` - Apply a named startup preset from `LMER_PRESETS_FILE` to this invocation. Also settable via `LMER_<TASK>_PRESET` (that taskdef only) or `LMER_PRESET` (any task); the flag wins over both. See [Startup presets](#startup-presets---preset--lmer_preset)
- `--list-presets` - List the presets available from `LMER_PRESETS_FILE` (name plus a summary of each preset's fields; env is shown as key names only) and exit
- `--agents <name,...>` - Comma-delimited preset names the session's agent may fan a task out to with the in-container `spawn-harness` tool. Also settable via `LMER_AGENTS` (the flag wins). Names are resolved against `LMER_PRESETS_FILE` **host-side** — a non-preset name whose model family implies a harness (e.g. `fable`) routes as a model-only agent; anything else unknown fails fast (exit 2) — and each preset contributes its `env` overlay plus `--harness`/`--prompt` folded from its `args` (harness into the overlay, prompt as the child's preamble), forwarded into the container as `LMER_SPAWN_AGENTS` + `LMER_SPAWN_AGENTS_CONFIG`; other launch-shaping preset config (`checkout`/`service`, remaining args) is ignored with a warning. Credential files of every implied child harness are mounted alongside the session harness's; a child harness with no host credential file warns at launch (#131). See [Fan-out agents in docs/PRESETS.md](./PRESETS.md#fan-out-agents---agents--lmer_agents)

**Note**: `--workspace-volume` and `--workspace-bind` options are currently not functional. The workspace uses the `/workspace` directory from the container image instead.

### Startup presets (`--preset` / `LMER_PRESET`)

Named startup presets — a local checkout to mount, a service container to target, extra environment variables, extra CLI flags — are defined once by the operator in the JSON file named by `LMER_PRESETS_FILE`; the same file serves Slack-selected and CLI-selected presets, and the format, validation rules, and trust model are documented in [docs/PRESETS.md](./PRESETS.md). A CLI invocation applies one by name:

```bash
# Flag form
lmer develop https://gitlab.example.com/group/project/-/issues/12 --preset my_service

# Env var form (the flag wins when both are given)
LMER_PRESET=my_service lmer develop https://gitlab.example.com/group/project/-/issues/12

# A project directory can pin a default preset via its .env
echo "LMER_PRESET=my_service" >> .env

# Per-taskdef form: applies to `lmer review …` only (issue #140)
LMER_REVIEW_PRESET=sol_review lmer review https://gitlab.example.com/group/project/-/merge_requests/7

# See what's available
lmer --list-presets
```

A preset can also be pinned **per taskdef** with `LMER_<TASK>_PRESET`, whose name derives from the taskdef id (uppercased, non-alphanumerics folded to `_`: `review` → `LMER_REVIEW_PRESET`, `code-review` → `LMER_CODE_REVIEW_PRESET`). Selection order is `--preset` > `LMER_<TASK>_PRESET` > `LMER_PRESET`, so a global default and per-task overrides can live side by side in `~/.lmer/.env`:

```bash
# ~/.lmer/.env
LMER_PRESET=default_config        # every task
LMER_REVIEW_PRESET=sol_review     # except review, which uses this
```

A `--no-task` invocation has no taskdef id, so only `--preset`/`LMER_PRESET` apply to it.

The preset supplies **defaults; the explicit invocation always wins**: explicit flags override preset `args` (and the `--checkout`/`--service` derived from the preset's fields; repeatable flags like `--mount-file` accumulate from both), and exported environment variables override preset `env` entries, which in turn beat `.env`-file values — both host-side and in the container environment. The combined argument set is re-validated normally, and `--show-env` attributes preset-applied variables to `preset (<name>)`. This precedence deliberately differs from Slack-selected presets, where the preset wins — see [Merge semantics per consumer](./PRESETS.md#merge-semantics-per-consumer).

Guard rails: an unknown name fails fast (exit 2) listing the available presets and naming what selected it (`--preset`, `LMER_<TASK>_PRESET`, or `LMER_PRESET`) — a taskdef-scoped var never silently falls back to `LMER_PRESET`; preset `args` must be known lmer flags — a bare positional, a literal `--`, or an unrecognized token fails fast (exit 2) rather than silently rebinding your command line; and a preset-supplied `--env-file` never loads (ignored with a warning — pass it on the command line instead). Details: [CLI-selected presets in docs/PRESETS.md](./PRESETS.md#cli-selected-presets).

### Supervisor and FastAPI Endpoint

Claude is launched through `lmer-supervisor`, a Python process that sits between your terminal and the Claude CLI. It allocates a PTY and forwards keystrokes/output transparently. The supervisor can also expose a FastAPI control plane.

**Auto `/start`** — by default `/start` is typed into Claude after a short delay so an lmer task begins without manual intervention. Because the trailing Enter is occasionally swallowed during Claude's startup re-render (leaving `/start` typed but unsubmitted), the supervisor (1) pre-clears cooked-mode PTY flags before fork so the CR isn't translated to LF, (2) defers injection until Claude has actually rendered the input prompt glyph (`❯`) so any startup modal/dialog has had a chance to clear, and (3) follows the initial `/start\r` with a few bare carriage-return nudges to re-trigger submission; each nudge is a no-op once `/start` has gone through. Tune the gap between nudges with `--auto-start-nudge-delay` (or `LMER_AUTO_START_NUDGE_DELAY`) and the maximum prompt-ready wait with `--auto-start-ready-timeout` (or `LMER_AUTO_START_READY_TIMEOUT`). Disable auto-start entirely with `--manual-start` (or `LMER_MANUAL_START=1`) when you want to drive Claude yourself.

**Follow-up prompt** — pass `--prompt "<text>"` (or set `LMER_START_PROMPT`) to have the supervisor type and submit an extra instruction shortly after the `/start` injection. Claude queues input typed while it is still working on `/start`, so the prompt lands as the next conversation turn — handy for automating `lmer chat <issue-url> --prompt="research X online first"`. The supervisor waits `LMER_START_PROMPT_DELAY` seconds (default `2.0`, also `--start-prompt-delay`) before typing the prompt so `/start` has registered as a slash command first; on a slow system too short a gap makes the prompt land on the same input line as `/start`. Raise it if you still see `/start <prompt>` collapsed onto one line. It is part of the auto-start flow, so it is ignored under `--manual-start` (where nothing is auto-injected).

**FastAPI endpoint** — pass `--fastapi` to expose two endpoints (bearer-token protected):

- `POST /input` — body `{"data": "...", "append_newline": true, "sanitize": true}` writes to Claude's stdin. `sanitize` is optional (absent means false) and asserts one fact only the client knows: a human typed this into a chat composer. When it is set **and** the payload's first character is one of the harness's **first-column escapes**, the supervisor prepends `". "` before writing, so the message reaches the TUI with `.` in the first column and is read as words rather than as a command (issues #254, #272). The escapes are per-harness data (`HARNESS_FIRST_COLUMN_ESCAPES` in `lmer_cli.supervisor`), and today only claude has a set: `!` (bash escape — the reported failure), `#` (write the line to memory) and `/` (slash command), each of which hijacks ordinary prose such as `!206 was merged`, `#254 is done` or `/help me read this backtrace`. `@` is **not** in the set and must not be: claude reads it as a file reference anywhere in a message, so defusing it would break the reference. Codex and pi have recorded `/` escapes of their own (lmer renders its slash commands into their prompt directories) but no set here, because the transform also needs a prefix that is known-inert in *that* composer and only claude's has been checked — so they, and any user-defined harness, are byte-for-byte passthrough along with every unflagged call. The test is on the payload exactly as sent, with no stripping, so `" !206"` (leading space) is not in the first column and is not touched. A dot rather than a space, deliberately: a leading space could be stripped by whitespace trimming before the first-character test, while a `.` survives any such trim; the assumption left is that `.` is not itself a first-column escape, and nothing in the supervisor proves it. The escape sets are this project's **recorded observations** of each harness's input box, not an interface any harness declares — a harness update can invalidate them and no check here would notice. What is enforced at import is the property the data can carry: no recorded escape set contains the prefix character (the supervisor refuses to load if one ever does), which turns a bad edit to the table into a load-time failure rather than a silent one — a self-consistency check on this project's own record, not a reading of the harness. The failure modes divide along that line: an escape missing from a set is the pre-#254 behavior for that character, a spurious entry costs a visible `. ` on a message that did not need one, and a future build that *starts* reading a leading `.` as an escape is the one the check cannot see. The prefix is visible in the transcript (`. !206 was merged`) — accepted, not hidden. The delivery receipt (`payload_sha256`, `payload_length`) deliberately describes the **pre-transform** bytes — the sender verifies its own hash against it, and hashing the transformed text would turn every sanitized send into a false corruption alarm — so `bytes_written` runs ahead of `payload_length` for a sanitized message: two bytes for the prefix, plus the Enter byte that every `append_newline` send already writes (three total on the chat path, pinned by test). That gap is the transform and the submit, not a partial write. When `append_newline` is true the text and its Enter are delivered as **two separate writes**, in that order: a CR in the same write as the text arrives in the same read, and a harness TUI reads a large enough chunk as a *paste*, where `\r` is a newline character rather than the Enter key — so above roughly 80 bytes the message used to land in the input box unsent (issue #210). Between the two writes the supervisor waits for evidence that the harness has *read* the text — `TIOCINQ` on the session's PTY reports how many written bytes the child has not taken yet — bounded (plus the `LMER_SUBMIT_ENTER_DELAY` margin), after which the Enter is sent regardless: a wedged harness delays a message rather than swallowing it. A payload that already ends with `\r` carries its own Enter and is not doubled; a trailing `\n` is **not** a submit, so it stays in the text and the Enter goes behind it (`"text\n"` → `"text\n"` then `"\r"`). To type a newline without pressing Enter, send `append_newline: false`, which still writes exactly the bytes given, once.

  The parenthetical above about rendered prompt directories now applies only to
  pi. Codex still treats `/` as command syntax, but current releases no longer
  discover lmer's former custom-prompt files.

  The Enter itself is sent **once**, with no follow-up bare-CR "nudges": a bare CR is a no-op only against an empty input box with no dialog on screen, and this endpoint is called mid-session, when a tool-permission prompt is exactly what may be up — a second CR would take that prompt's default.

  Codex and Pi wrap the text write in the terminal's bracketed-paste start/end
  sequences before that single Enter. Claude does the same for prose, including
  sanitized chat, but leaves a column-one `!`, `#` or `/` payload as keystrokes:
  Claude enables mode 2004 yet a pasted slash command was measured opening its
  autocomplete without executing (#210). Their installed TUIs were probed to
  enable the protocol, so the
  paste end is an explicit parser boundary instead of a timing prediction. A
  payload that already contains the paste-end control sequence is left
  unframed, preventing the remainder from escaping the bracket and being read as
  keystrokes. Protocol bytes count in `bytes_written` but not in
  `payload_length` or `payload_sha256`; user-defined harnesses retain
  text-then-Enter with no framing unless future support is established. The
  regression fixture verifies one 6.6 KB fenced message end to end; behavior
  above the 8,000-character control-plane limit is not claimed. On a
  submitted Codex message whose entire command token is
  `/followup`, the text becomes a plain instruction to run
  `/Agents/global/hooks/followup.sh`; arguments and line endings are preserved,
  non-matching prose and raw `append_newline: false` keystrokes are untouched,
  and receipts continue to cover the caller's original bytes.

  Since the supervisor writes to the PTY and cannot see whether the TUI registered the CR as a submit, the reply reports what it can and no more:

  ```json
  {"bytes_written": 9, "submit_confirmed": false,
   "note": "Enter was sent after the text. …submit it from the session's terminal view.",
   "submit_text": "read"}
  ```

  `submit_text` reports the half of the delivery that *is* observable — whether the harness was seen taking the text — with three values rather than a flag, because "not observed" is neither a clean delivery nor a warning: `read` (seen taken, so the Enter behind it cannot have been absorbed into the same read), `unread` (seen queued and still queued when the wait ran out — the reading that explains a message left in the input box), and `unknown` (nothing observed: no terminal to probe, or the harness took the bytes before the first probe could see them — the ordinary answer on a responsive session, not a failure report).

  All three fields appear only when `append_newline` is true — a keystroke presses no Enter, so it has no submit to be unsure about — and `POST /api/sessions/{id}/input` forwards them unchanged, omitting `submit_text` entirely for a session whose image predates it rather than inventing a value. A client that needs certainty should watch `/output` or the session's terminal view. (The auto-`/start` path above keeps its nudges: it fires once at startup behind an observed prompt-ready marker, before any permission prompt can exist.)

  **What a payload actually does at the far end**, established against a real Claude Code 2.1.220 TUI on a PTY (issue 194) rather than reasoned about, because two plausible-sounding theories about it were both wrong:

  - **A multi-line payload arrives whole and submits once.** `"line one\nline two\r"` is recorded by the harness as a single turn, embedded newline intact — the LF is inserted as a literal newline in the input box and the trailing CR submits the lot. It does **not** submit at the first newline, and it does not sit unsent. So there is no need to split a message, and splitting one would make two turns out of it.
  - **A dialog on screen swallows the whole message, not just the Enter.** With a modal up (a permission prompt, the trust dialog, a picker) the typed text is discarded *and* the CR answers the dialog with its default. Nothing about that is visible in the reply: the write succeeded, so `bytes_written` is the full payload and the session heard nothing.
  - **A payload written while the TUI is mid-redraw is discarded** the same way, which is what the auto-`/start` path's readiness marker and settle delay exist to avoid.
  - The prompt glyph is **not** evidence that no dialog is up: `❯` is also the selection cursor in Claude's own menus. That is why this endpoint reports the uncertainty instead of trying to detect the screen state.

  A client that shows a human what it sent must therefore not turn a 200 into "delivered and read". The control UI's chat holds the sent message as pending until the session's own transcript has it; past a short grace window it renders the message like any other the operator sent, assuming delivery rather than warning about it (issue #254 — message loss is exceedingly rare, and the warning fired on every ordinary mid-turn send). The rare genuinely-lost message remains visible in the terminal view, where both cases above can be told apart.
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

### Review-Thread Resolution (gitlab-review / github-review)

Thread resolution is the reviewer's verified sign-off (Thread Resolution
Policy, `rules/git.md`): the author of a fix replies with what changed and
the commit SHA and leaves the thread open; only the reviewer — a human, or
a review session that verified the fix — resolves it.

**Resolution guard.** `gitlab-review ... --resolve-thread` and
`github-review ... --resolve-thread` refuse to run (exit 1, no API call)
when `LMER_TASK` is set to anything other than `review` — that includes
develop, followup, masterplan, and chat sessions, deliberately: a session
that fixed the code must not sign off on its own fix. The refusal message
names the workaround: a human resolves in the web UI, or runs the CLI from
a host shell where `LMER_TASK` is unset. There is no override flag.

**GitLab resolvability.** The `gitlab-review --resolve-thread` eligibility gate
follows GitLab's own `resolvable` fact instead of classifying threads by whether
they are inline. This includes non-diff `DiscussionNote` discussions when GitLab
marks the discussion resolvable. Standalone individual notes that advertise no
resolved state are still refused before a write. Resolution uses GitLab's
discussion-level REST endpoint. The aggregate `--info` provenance counts remain
structural: standalone `individual_note` objects are not counted as discussion
threads.

**Thread provenance in `--info`.** Both CLIs render a "Threads" block —
total / unresolved / resolved counts and a per-account resolver breakdown —
and carry the same data under a `thread_provenance` key in `--json`.
gitlab-review additionally flags threads whose resolution predates the MR's
head commit ("resolved before the latest change"); github-review cannot
(the GitHub GraphQL schema exposes no resolution timestamp), so it reports
counts and resolvers only, and derives `blocking_discussions_resolved` from
the fetched thread data instead of hardcoding it. When a page cap truncates
the thread walk, the block says "counts partial — page cap" rather than
presenting partial counts as totals. The block is advisory: `--info` never
fails on provenance findings.
