# Documentation Index

This directory contains comprehensive documentation for the LMER (LLM Environment Runtime) project. Each document covers a specific aspect of the system.

## Documentation Files

### [PLATFORM-QUICKSTART.md](./PLATFORM-QUICKSTART.md)
**Platform Quickstart**

The short path to running the lmer platform (web control plane + supervising
assistant) on a host:
- Install, `setup-ui`, first run, the shared secret
- Reaching it remotely: direct bind vs an nginx reverse proxy (WebSocket
  upgrade + timeouts included)
- `LMER_PLATFORM_CONTAINER_URL` and the surviving-assistant restart gotcha
- A tour of the fleet view, spawning, answering, wind down vs exit, uber lmer
- Digest nudges: when the daemon reminds uber lmer that its spool is unread
- uber lmer's memory: the host-local store its incarnations share, what its
  file count does and does not show

**Best for**: Getting the platform running and usable in a few minutes.

---

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

### [PLATFORM-CONTAINER.md](./PLATFORM-CONTAINER.md)
**Platform Container Image**

Running the orchestrator platform (control plane + UI) as a container image
built from `Dockerfile.platform`, instead of installing it on the host:
- What the image is, against the session image and against a bare-host install
- The path-identity invariant (`$HOME/.lmer` mounted at the identical absolute
  path) and exactly what a renamed mount breaks
- What to mount, including the credential paths that live outside `~/.lmer`
- Why `--network=host`, the bind-address opt-in, and sessions dialing back
- Upgrades, the restart semantics of a containerized daemon, and the session
  re-attach limitation that is still open
- Spike runbook: build, run, spawn, restart, and the predictions to confirm

**Best for**: Deploying the platform as a pull rather than a Node build, and
walking the container-platform spike.

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

### [GATE-FASTPATH.md](./GATE-FASTPATH.md)
**Gate fast paths**

How the commit and push gates avoid re-running the test suite, and when they refuse to:
- The text-only fast path and the per-project `tests.text_diff_subset` declaration
- The test-result cache, its key, and what the key cannot see
- When the full suite always runs
- Kill switches and how to read a gate receipt's `test_scope`

**Best for**: Understanding why a gate ran a subset or skipped the suite, and declaring the subset for a new project.

---

### [LMER-CLI.md](./LMER-CLI.md)
**LMER Python CLI (lmer)**

User guide for the `lmer` command-line interface:
- Installation and global setup
- Environment variable configuration
- Task-based workflows (built-in `chat`, plus tasks loaded from the work-repo or `LMER_TASKDEF_PATHS`)
- Basic usage examples and command-line options
- Repository targeting (URLs, local paths, PR/MR/issue URLs)
- Canonical source configuration (`sources.yaml`)
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

### [TRANSCRIPT-FORMAT.md](./TRANSCRIPT-FORMAT.md)
**The lmer transcript format (version 1)**

The public contract a drop-in harness converts its sessions into so they render
in the orchestrator's chat view:
- The three record types (`lmer.meta`, `lmer.message`, `lmer.tool_update`) with
  full field semantics and examples
- The constraints the reader enforces (append-only, self-contained lines,
  sizing, discovery, scrubbing)
- What each canonical field looks like in the chat view
- Converter lifecycle: the backgrounded tailer in `runner.sh`, per-record flush,
  and the alternative end-of-session pass

**Best for**: Making a user-installed harness's transcripts readable without
any change to lmer itself.

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

How `--service` and `--checkout` let lmer run against an already-running containerized project (Docker Compose stack, Podman pod, etc.) by `docker exec`-ing into the project's container instead of cloning a fresh copy. Plus **service slots**: named single-occupancy bindings from a runner to one of this host's dev services, declared in the platform's `config.json`, so several agents can share a host without two of them landing on one database.

**Best for**: Working with projects whose tests require their own runtime environment (database fixtures, Django settings, etc.), and handing out one dev stack across a fleet.

---

### [MATRIX-CHAT.md](./MATRIX-CHAT.md)
**Matrix Chat Bridge**

`lmer-matrix-bridge`: one platform daemon in a Matrix room, so a run that needs
a human can be answered from a phone.
- What the room shows: a thread per run, transitions only, and why the two
  question states read differently (one continues a session, one starts a
  container)
- Setup: the `matrix` extra, the `matrix` section in `config.json`, the three
  `LMER_MATRIX_*` secrets, `register` and `check`
- The allowlist: explicit MXIDs only, humans and bots alike, and why this
  deliberately does not match domains the way `LMER_PUSH_ALLOW_LIST` does
- Encryption: the crypto store, the recovery key, and the one state where the
  bridge refuses to start rather than mint a fresh device
- Attachments: the three preconditions, and what happens when one fails

**Best for**: Setting up the Matrix bridge, deciding who may answer what, and
diagnosing a room that has gone quiet.

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

### [RELEASE-FLOW.md](./RELEASE-FLOW.md)
**Release Flow — GitLab-Driven, Read-Only GitHub**

Canonical explanation of how a public release ships, as a tracked lmer run with durable state and receipts:
- Topology: canonical GitLab instance, GitHub as a read-only mirror/publish target, tokenless PyPI upload via trusted publishing (OIDC)
- The two-leg release run (Leg 1 prep, human release-MR merge gate, Leg 2 ship) and the push ordering (GitHub green before any GitLab publish)
- Watch/resume semantics, single-flight locking, and per-step idempotency keyed on the recorded merge SHA
- The signing model (SSH-signed `v*` tags — signed for history, **not verified by CI**) and where authorization actually sits: the mirror's `v*` tag ruleset, the `pypi` environment's tag-pattern policy, and its required reviewer — plus an honest residual-risk statement
- Version reuse (no `skip-existing`; PyPI's own refusal is the gate), the "Re-run failed jobs" recovery ladder, error paths, and the deferred first production release with its prerequisites

**Best for**: Understanding the release flow end-to-end, cutting a release, and evaluating the publish-authorization threat model.

---

### [RELEASE-ADOPTION.md](./RELEASE-ADOPTION.md)
**Release Flow — Per-Repo Adoption Checklist & Runbooks**

How a repository adopts the release flow (configuration plus prerequisites, never design changes):
- The four-parameter surface (GitHub target URL, tag prefix, signing-key reference, changelog mechanism)
- Prerequisite checklists: GitHub mirror controls, PyPI trusted publisher, GitLab protected tags, credentials
- Rotation runbook for the fine-grained PAT and the release SSH signing key
- Burned-version and GitHub `main` divergence remediation runbooks
- The bootstrap sequence (lmer first, then ctl)

**Best for**: Operators onboarding a new repository onto the release flow and running the credential-rotation or repair runbooks.

---

### [RELEASE-PROD-SETUP.md](./RELEASE-PROD-SETUP.md)
**Production Release Setup — Operator Receipts**

One-time, operator-performed production configuration receipts for the lmer release flow — each item ticked with date and actor (never a secret value):
- Signing keypair, mirror branch protection, and the authorization controls that gate a release: the `v*` tag ruleset, the `pypi` environment tag-pattern policy, and its required reviewer
- Production PAT issuance and provisioning, bot account signing key, PyPI trusted publisher, GitLab protected tags
- Mirror PR/collaborator policy
- Split into **gating** receipts and **hardening** items: a release run must refuse leg 2 while any gating item is unchecked; hardening is tracked separately so it neither blocks a release nor passes unnoticed

**Best for**: The operator completing (and auditors reviewing) the production setup checklist before the first release run.

---

## Quick Navigation

- **New to LMER?** Start with [LMER-CLI.md](./LMER-CLI.md) to learn how to use the tool
- **Setting up containers?** See [CONTAINER.md](./CONTAINER.md) for Docker/Podman configuration
- **Having auth issues?** Check [AUTHENTICATION.md](./AUTHENTICATION.md) for SSH and API setup
- **Cutting a release?** Start with [RELEASE-FLOW.md](./RELEASE-FLOW.md) for the release flow, then [RELEASE-ADOPTION.md](./RELEASE-ADOPTION.md) and [RELEASE-PROD-SETUP.md](./RELEASE-PROD-SETUP.md) for adoption and operator setup
- **Answering runs from your phone?** See [MATRIX-CHAT.md](./MATRIX-CHAT.md)
- **Need to troubleshoot?** All documents include troubleshooting sections

---

## Additional Resources

- **Container Image**: `lmer` (Oracle Linux 9 Slim FIPS)
- **Repository**: See `README.md` in the project root for general project information
- **Task Definitions**: See [TASKDEFS.md](./TASKDEFS.md) for the format and authoring guide. `taskdef/` in the repo root holds the built-in task types (currently `chat`). Additional tasks can be loaded by pointing `LMER_TASKDEF_PATHS` at directories containing more taskdefs.
