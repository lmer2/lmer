# lmer

**lmer** is a containerized harness for running [Claude Code](https://docs.anthropic.com/en/docs/claude-code) against external repositories.

It clones a target repo into a sandbox container, mounts a known set of development rules and slash commands, and hands the session to Claude with a task-shaped prompt. Tests, pre-commit, and git operations all run inside the container, so the host stays clean and credentials never leak into the target repo's working tree.

Only `chat` ships as a built-in task in [taskdef/](taskdef/). Additional tasks can be loaded from the work-repo or from directories listed in `LMER_TASKDEF_PATHS` — see [docs/TASKDEFS.md](docs/TASKDEFS.md).

> **Note on GitLab vs GitHub support:** Up to this point the priority has been making lmer behave nicely against GitLab — the `gitlab-review` toolset does a lot of heavy lifting to smooth that integration. GitHub generally works, but with this GitHub release the next focus is bringing GitHub interaction up to the same level of polish. Expect rough edges on the GitHub side until that work lands.

## What you get

- A reproducible container image (Oracle Linux 9 Slim, FIPS-capable) with `claude`, `git`, `uv`, and a pinned Python toolchain pre-installed.
- A consistent ruleset (`AGENTS.md` + `rules/`) injected into Claude's system prompt so behavior is the same across projects: tests before commits, no force-push, show command output, pytest-only, etc.
- Gate commands (`gate-check`, `gate-commit`, `gate-push`) that enforce those rules at commit-time.
- Task-specific workflows that load instructions, set up the work environment, and (optionally) drive a follow-up review cycle on the resulting MR.

## Quick Start

The `lmer` CLI provides a safe, containerized environment for AI-assisted development. The typical setup uses an existing Claude.ai subscription (no API key required) and installs from the `prep-release` branch for the latest features.

### 1. Authenticate Claude on the host (subscription users)

If you already use Claude Code with a Claude.ai subscription, run `/login` once on your host machine:

```bash
claude   # then run /login inside the Claude Code session
```

This creates `~/.claude/.credentials.json`, which `lmer` automatically mounts into the container — no API key needed.

If you instead authenticate via API key, place it in `~/.lmer/.env` (see [API key alternative](#api-key-alternative) below).

### 2. Install lmer

Pick the branch that matches your needs:

```bash
# Latest features — may be unstable
uv tool install lmer --from git+https://github.com/lmer2/lmer@prep-release

# Stable release
uv tool install lmer --from git+https://github.com/lmer2/lmer@main

# Local / editable (developer mode)
uv tool install -e lmer --from .
```

To upgrade later: `uv tool upgrade lmer`.

The container image is pulled automatically on first run from `ghcr.io/lmer2/lmer` — no manual build step is needed. If you can't reach GHCR, or want to develop on the container itself, build it locally from a clone:

```bash
git clone https://github.com/lmer2/lmer /tmp/lmer-src
lmer build --local /tmp/lmer-src
```

Override the pull source with `LMER_REGISTRY=<host>/<path>` for self-hosted registries.

### Common options

Targets can be a repo URL, a PR/MR/issue URL (lmer extracts the base repo), a local git path, or omitted to infer from the current directory's git remote:

```bash
# Remote repo
lmer chat https://gitlab.com/group/project.git

# GitHub PR or GitLab MR/issue URL — base repo is auto-extracted
lmer chat https://github.com/org/repo/pull/123
lmer chat https://gitlab.com/group/project/-/merge_requests/456
lmer chat https://gitlab.com/group/project/-/issues/31

# Local checkout (extracts remote URL)
lmer chat /path/to/repo

# Infer from current directory
cd /path/to/repo && lmer chat
```

Other useful flags:

```bash
# Checkout specific branch or ref
lmer chat https://gitlab.com/group/project.git --branch feature/x

# Get shell access
lmer chat https://gitlab.com/group/project.git --exec -- bash

# Match host UID for SSH agent permissions
lmer chat https://gitlab.com/group/project.git --match-uid --exec -- bash
```

See [docs/LMER-CLI.md](docs/LMER-CLI.md) for complete CLI documentation.

### API key alternative

If you don't have a Claude subscription, place your API key in `~/.lmer/.env`:

```bash
mkdir -p ~/.lmer
cat > ~/.lmer/.env <<'EOF'
CLAUDE_API_KEY=your-claude-key
GITLAB_TOKEN=your-gitlab-token
EOF
```

See [docs/AUTHENTICATION.md](docs/AUTHENTICATION.md) for the full authentication reference.

See [docs/CONTAINER.md](docs/CONTAINER.md) for detailed container documentation.

### Why Oracle Linux 9 / FIPS?

The container is built on `oraclelinux:9-slim-fips`. This base satisfies a FIPS 140-2 compliance requirement: cryptographic operations (TLS, hashing for signed commits, etc.) inside the container must run against a validated module. Oracle Linux 9 ships a validated OpenSSL and exposes `/proc/sys/crypto/fips_enabled`, which `make fips-check` verifies.

If you do not need FIPS, the image still works fine — it just trades a smaller base layer for a validated one. Slimmer Debian/Alpine-based images are not currently supported out of the box; the Containerfile is the single source of truth and contributions for an opt-in alternative base are welcome.

## Developer setup

For working on lmer itself (rather than just using it), clone the repo and build from source:

```bash
git clone https://github.com/lmer2/lmer ~/Agents/global
cd ~/Agents/global

# Container workflow (FIPS-compliant)
make build   # Build the container image
make lmer    # Run lmer with Claude in a fresh container
make test    # Run tests locally
make help    # See all targets

# Or local Python environment
uv sync
uv run pre-commit install
```

> **Path note:** `~/Agents/global` is the conventional host checkout location. Hooks and scripts inside the container refer to `/Agents/global` — that's the **in-container** mount path, populated by lmer at start. You do not need to create `/Agents/global` on the host.

### Rules outside lmer

Rules are injected automatically when running via `lmer` (which uses [libexec/claude-runner.sh](libexec/claude-runner.sh) internally). For standalone `claude` sessions, pass the rules file explicitly:

```bash
claude --append-system-prompt-file ~/Agents/global/AGENTS.md
```

User-specific additions: create `~/.lmer/AGENTS.md` — the runner appends it after the workspace config automatically.

## Documentation

See [docs/INDEX.md](docs/INDEX.md) for the full documentation index. Common starting points: [LMER-CLI.md](docs/LMER-CLI.md), [CONTAINER.md](docs/CONTAINER.md), [AUTHENTICATION.md](docs/AUTHENTICATION.md), [TASKDEFS.md](docs/TASKDEFS.md).

The rules Claude follows live in [AGENTS.md](AGENTS.md) and [rules/](rules/) (split by topic: git, testing, code-quality, security, documentation, ci-cd, dependencies). Rule-reading and commit-gate commands (`rgr`, `rgr-git`, `rgrpc`, `pc`, …) are documented in [hooks/README.md](hooks/README.md).
