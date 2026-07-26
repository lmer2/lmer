# Documentation Index

This directory contains comprehensive documentation for the LMER (LLM Environment Runtime) project. Each document covers a specific aspect of the system.

## Documentation Files

### [CONTAINER.md](./CONTAINER.md)
**Container Runtime Support**

Complete guide to using Docker and Podman with LMER. Covers:
- Container runtime detection (Docker vs Podman)
- FIPS 140-2 compliance and verification
- Runtime-specific differences (build commands, compose files, user namespaces, SELinux)
- Container usage, volume mounts, and user ID handling
- Security settings and resource limits
- Makefile commands and troubleshooting
- Performance considerations and security benefits

**Best for**: Understanding how containers work in this project, troubleshooting container issues, and learning about FIPS compliance.

---

### [AUTHENTICATION.md](./AUTHENTICATION.md)
**Container Authentication and SSH Setup**

Detailed documentation on authentication mechanisms in containerized environments:
- Container-home directory structure and management
- SSH authentication methods (SSH keys, SSH agent forwarding)
- Claude API authentication and credential management
- Security considerations and best practices
- Troubleshooting authentication issues

**Best for**: Setting up SSH access for Git operations, configuring Claude API authentication, and understanding security best practices.

---

### [LMER-CLI.md](./LMER-CLI.md)
**LMER Python CLI (lmer)**

User guide for the `lmer` command-line interface:
- Installation and global setup
- Environment variable configuration
- Task-based workflows (built-in `chat`, plus tasks loaded from the work-repo or `LMER_TASKDEF_PATHS`)
- Basic usage examples and command-line options
- Repository targeting (URLs, local paths, PR/MR/issue URLs)
- Exec mode and debugging options

**Best for**: Learning how to use the `lmer` CLI tool, understanding task workflows, and getting started with LMER.

---

### [HARNESSES.md](./HARNESSES.md)
**Agent Harnesses**

How lmer abstracts the agent CLI it runs in the session container, and the
supported harnesses (Claude Code, Codex, pi):
- Selecting a harness (`--harness` / `LMER_HARNESS`)
- Capability matrix (full tier vs core tier) and per-harness env mapping
- Authentication per harness and sandbox/approval posture
- Architecture (registry, runner scripts, supervisor profiles)
- Step-by-step checklist for adding support for a new harness
- User-installed harnesses (`~/.lmer/harnesses/` drop-in, no fork required)

**Best for**: Running lmer sessions on Codex/pi, understanding what works where, and adding support for additional harnesses.

---

### [USER-HARNESS-OPENCODE.md](./USER-HARNESS-OPENCODE.md)
**Worked example: opencode as a user-installed harness**

A complete, paste-ready setup of opencode via the user-harness mechanism —
manifest, runner script, base permission config, host-side login flow, and
troubleshooting. Verified against opencode 1.18.4.

**Best for**: Setting up your first user-installed harness, or a template to
adapt for any other agent CLI.

---

### [PROMPT-FRAGMENTS.md](./PROMPT-FRAGMENTS.md)
**Prompt Fragments**

How LMER injects session-specific markdown sections (e.g. human-user
identity) into Claude's system prompt at container start-up:
- Three-piece pipeline: Jinja2 templates under `prompts/`, the
  `render-prompt-fragment.py` renderer, and the append step in
  `claude-runner.sh`
- Template search precedence across script-relative / workspace /
  `$LMER_HOME` / `/Agents/global`
- Env-var filtering that keeps tokens (`TOKEN`/`KEY`/`SECRET`/... and URLs
  with embedded credentials) out of template context
- Step-by-step recipe for adding a new fragment (gate, template, env
  forwarding, shell block, tests, docs)

**Best for**: Understanding the `LMER_HUMAN_IDENTITY` injection machinery, or adding another prompt fragment for a future feature.

---

### [DOCUMENTATION-PROVISIONING.md](./DOCUMENTATION-PROVISIONING.md)
**Documentation Provisioning**

How lmer provides default AGENTS.md and rules/ files when the target repository doesn't have them:
- Layered override hierarchy (lmer defaults < work repo < project repo)
- Git integration via .git/info/exclude to avoid untracked file noise
- Gate-check awareness and warning behavior
- Work repo overrides for project-specific configuration
- Design rationale

**Best for**: Understanding how documentation files are provisioned, configuring per-project overrides via the work repo, and troubleshooting missing rules.

---

### [RUN-STATE.md](./RUN-STATE.md)
**Run D.M.C. — Durable Run State**

Durable per-run state in the work repo — `state.yaml`/`events.jsonl`, resume briefs, `work` CLI verbs, cleaner contract:
- The `runs/<slug>/` layout and the `state.yaml` schema
- Deterministic slug derivation and the completed-run policy
- `work` CLI verbs (`state`, `state set`, `event`, `resume`, `artifact`, `session-start`, `session-end`)
- Session lifecycle (seeding, owner claims, `LMER_SESSION_ID`) and fail-soft guarantees
- The external cleaner contract and the deferred growth path

**Best for**: Understanding how sessions persist and resume machine-readable state, and what external tools can rely on when reading `runs/<slug>/`.

---

### [SERVICE-MODE.md](./SERVICE-MODE.md)
**Service Mode**

How `--service` and `--checkout` let lmer run against an already-running containerized project (Docker Compose stack, Podman pod, etc.) by `docker exec`-ing into the project's container instead of cloning a fresh copy.

**Best for**: Working with projects whose tests require their own runtime environment (database fixtures, Django settings, etc.).

---

### [PRESETS.md](./PRESETS.md)
**Startup Presets**

Named, operator-defined startup configurations (`LMER_PRESETS_FILE`) that a session starter selects by name — via a `$preset:<name>` Slack token or `lmer --preset` / `LMER_<TASK>_PRESET` / `LMER_PRESET` on the CLI:
- The presets file: JSON format, field reference (`checkout`, `service`, `env`, `args`)
- Loading and validation rules (forgiving by design) and the trust model
- Per-consumer merge semantics (Slack listener vs. direct CLI) and why they differ
- Contributor checklist for adding a preset field, and troubleshooting

**Best for**: Defining reusable startup configurations (e.g. service-mode stacks), and understanding exactly how a preset combines with an explicit invocation.

---

### [MCP-CONFIGURATION.md](./MCP-CONFIGURATION.md)
**MCP Server Configuration**

How `.mcp.json` is loaded and how MCP servers are exposed to Claude inside the lmer container.

**Best for**: Adding or troubleshooting Model Context Protocol servers (e.g. Playwright).

---

### [gitlab-pipeline.md](./gitlab-pipeline.md)
**GitLab Pipeline Monitor**

Reference for the `gitlab-pipeline` helper that watches and traces GitLab CI/CD pipelines from inside (or outside) the lmer container.

**Best for**: Debugging failing CI jobs and tailing pipeline output.

---

### [DEVELOPMENT.md](./DEVELOPMENT.md)
**Development Conventions**

Design decisions and code conventions for working on lmer itself, recorded as dated sections with rationale (e.g. stdlib dataclasses vs. pydantic for internal records).

**Best for**: Contributing to lmer's own source and understanding why the code is shaped the way it is.

---

## Quick Navigation

- **New to LMER?** Start with [LMER-CLI.md](./LMER-CLI.md) to learn how to use the tool
- **Setting up containers?** See [CONTAINER.md](./CONTAINER.md) for Docker/Podman configuration
- **Having auth issues?** Check [AUTHENTICATION.md](./AUTHENTICATION.md) for SSH and API setup
- **Need to troubleshoot?** All documents include troubleshooting sections

---

## Additional Resources

- **Container Image**: `lmer` (Oracle Linux 9 Slim FIPS)
- **Repository**: See `README.md` in the project root for general project information
- **Task Definitions**: See [TASKDEFS.md](./TASKDEFS.md) for the format and authoring guide. `taskdef/` in the repo root holds the built-in task types (currently `chat`). Additional tasks can be loaded by pointing `LMER_TASKDEF_PATHS` at directories containing more taskdefs.
