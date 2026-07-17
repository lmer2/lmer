# Agent Harnesses

lmer runs an *agent harness* — the AI coding-agent CLI — inside each session
container. Claude Code is the default and most fully integrated harness;
Codex and pi are supported at the **core tier** (see the capability matrix
below).

## Selecting a harness

```bash
# One-off, via CLI flag
lmer develop https://gitlab.example.com/group/project/-/issues/42 --harness codex

# Persistent, via environment (e.g. in ~/.lmer/.env)
LMER_HARNESS=pi
```

Resolution order: `--harness` flag > `LMER_HARNESS` env var > `LMER_LLM_NAME`
model hint > `claude`. Unknown names fail fast on the host with the list of
known harnesses. The resolved name is forwarded into the container as
`LMER_HARNESS`, where it selects the runner script (`clone_and_exec.py`) and
the supervisor's TUI profile (`lmer-supervisor`); the container never
re-derives the harness from the model hint.

**Model hint:** when neither `--harness` nor `LMER_HARNESS` is set, the model
name in `LMER_LLM_NAME` autoselects the matching harness, so
`LMER_LLM_NAME=gpt-5.2 lmer …` runs codex without further configuration.
Matching is word-bounded and case-insensitive on model family names:
`opus`/`haiku`/`fable`/`sonnet`/`mythos` → `claude`;
`gpt`/`codex`/`o3`/`o4` → `codex` (see `MODEL_HARNESS_HINTS` in
`src/lmer_cli/harness.py` — `gpt` covers every current codex-served id, the
rest catch legacy ids like `codex-mini-latest`, `o3`, `o4-mini`). A model that matches
nothing (or an unset `LMER_LLM_NAME`) falls back to `claude`. The CLI
announces an autoselection (`🤖 Harness: codex (auto-selected from
LMER_LLM_NAME=…)`).

All harness CLIs are baked into the single container image; no rebuild is
needed to switch. To force a re-install of one harness's CLI during an image
build:

```bash
lmer build --update-harness codex        # repeatable; also: --update-harness all
lmer build --update-claude               # legacy alias for --update-harness claude
```

## Supported harnesses

| Harness    | CLI / upstream                                        | Install (baked in image)                                  |
|------------|-------------------------------------------------------|-----------------------------------------------------------|
| `claude`   | Claude Code (Anthropic)                               | `curl -fsSL https://claude.ai/install.sh \| bash`          |
| `codex`    | Codex CLI (OpenAI, github.com/openai/codex)           | `npm install -g @openai/codex`                             |
| `pi`       | pi (github.com/earendil-works/pi, ex badlogic/pi-mono)| `npm install -g --ignore-scripts @earendil-works/pi-coding-agent` |

## Capability matrix

**Full tier** (claude) — everything lmer offers. **Core tier** (codex, pi) —
terminal task sessions work end-to-end; the claude-only features degrade
gracefully and are listed explicitly:

| Capability                                   | claude | codex | pi  |
|----------------------------------------------|:------:|:-----:|:---:|
| Repo clone + task instructions (`/start` flow)| ✅     | ✅¹   | ✅¹ |
| AGENTS.md project context                    | ✅²    | ✅    | ✅  |
| User `~/.lmer/AGENTS.md` + human identity    | ✅     | ✅³   | ✅³ |
| Model selection (`LMER_LLM_NAME`)            | ✅     | ✅    | ✅  |
| Reasoning effort (`LMER_REASONING_EFFORT`)   | ✅     | ✅⁴   | ✅⁴ |
| Danger zone (`LMER_DANGER_ZONE`)             | ✅     | ✅    | ⚠️⁵ |
| Permission prompts for tool use              | ✅     | ✅    | ❌⁵ |
| Gate commands / `work` CLI / hooks scripts   | ✅     | ✅    | ✅  |
| Supervisor auto-start + FastAPI endpoint     | ✅     | ✅    | ✅  |
| Slash commands (`agent-files` commands)      | ✅     | ✅⁷   | ✅⁷ |
| Skills (`agent-files` skills)                | ✅     | ❌    | ❌  |
| Stop/SessionEnd hook guards                  | ✅     | ❌    | ❌  |
| Agent memory persistence (`work memory`)     | ✅     | ✅⁸   | ✅⁸ |
| Masterplan workflow                          | ✅     | ❌    | ❌  |
| Slack chat mode / service mode               | ✅     | ❌    | ❌  |
| MCP servers (`.mcp.json`)                    | ✅     | ❌⁶   | ❌⁶ |

1. Non-claude harnesses have no `/start` slash command; the supervisor types a
   plain-text instruction to run `bash /Agents/global/hooks/start.sh` instead
   (see `GENERIC_START_COMMAND` in `src/lmer_cli/harness.py`).
2. Claude Code does not auto-discover AGENTS.md, so `claude-runner.sh` injects
   it via `--append-system-prompt-file`; codex and pi read it natively.
3. Delivered via the harness's *global* context file (`~/.codex/AGENTS.md`,
   `~/.pi/agent/AGENTS.md`), written per session by
   `harness_render_global_context` (lmer-managed marker; a hand-written file
   at that path is never overwritten).
4. `max` maps to the harness's top tier `xhigh`; `low|medium|high|xhigh` pass
   through; `auto`/unset lets the harness decide.
5. **pi has no permission-prompt system by design** — tools run unprompted
   with the pi process's privileges; the lmer container is the security
   boundary. For pi, `LMER_DANGER_ZONE` instead controls *project trust*:
   default `--no-approve` (the target repo's own `.pi/` settings, extensions,
   and skills are not loaded), danger zone `--approve` (they are).
6. The `.mcp.json` merge machinery is claude-specific today. Codex supports
   MCP through its own config file (`agent-files/codex/config.toml`); pi has
   no built-in MCP support (extensions can add it).
7. Rendered per session from the claude command files
   (`agent-files/claude/commands/*.md`) into the harness's native *prompt
   templates* by `harness_render_prompt_templates`
   (`lmer_cli.container.prompt_templates`): pi invokes them as `/start`,
   `/followup`, … with autocomplete; codex as `/prompts:start`, … (codex
   custom prompts are deprecated upstream in favor of skills but remain
   functional). Work-repo commands override global ones of the same name,
   mirroring the claude layout. Claude-specific frontmatter
   (`allowed-tools`) is dropped in conversion, and a leading-`!` execution
   line becomes an instruction to run that command — these harnesses expand
   the template as prompt text and then run the command as a tool call.
8. Restore is automated in the runner (`work memory restore`, gated on
   `LMER_PERSIST_AGENT_MEMORY` exactly like claude). These harnesses have no
   native memory feature, so the read/write/persist contract is injected
   into the global context file (the `prompts/agent-memory.md` fragment,
   note 3's mechanism); persisting back remains the agent's
   `work memory persist`, as on claude.

## Authentication

Credentials are bind-mounted from the host when present (per-file, never whole
config directories), and provider API keys flow through `.env` like any other
variable:

| Harness    | Mounted credential file(s)                              | Env alternative                       |
|------------|---------------------------------------------------------|---------------------------------------|
| `claude`   | `~/.claude/.credentials.json`, `~/.claude.json`         | `CLAUDE_API_KEY`                       |
| `codex`    | `~/.codex/auth.json` (`codex login` on the host)        | `CODEX_API_KEY` (non-interactive only) |
| `pi`       | `~/.pi/agent/auth.json` (pi's in-TUI `/login`), `~/.pi/agent/models.json` (custom provider/model registry) | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, … |

### Custom models (pi)

pi resolves model names against its built-in catalog plus the custom
provider/model registry at `~/.pi/agent/models.json` — the mechanism for
self-hosted endpoints such as a local llama.cpp `llama-server`. lmer mounts
that file into the container when it exists on the host, so
`LMER_LLM_NAME=<custom-id> lmer … --harness pi` works with host-registered
models.

#### Example: pi against a local llama.cpp server

1. **Run `llama-server`** with your model on the host (or any machine the
   container can reach). See the
   [llama.cpp server docs](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
   for setup; the relevant part here is that it exposes an OpenAI-compatible
   API on its port (default 8080).

2. **Register the model** in `~/.pi/agent/models.json` on the host:

   ```json
   {
     "providers": {
       "llamacpp": {
         "baseUrl": "http://192.168.1.50:8080/v1",
         "api": "openai-completions",
         "apiKey": "dummy",
         "models": [
           {
             "id": "my-local-model",
             "name": "My local model",
             "contextWindow": 64000,
             "maxTokens": 32000,
             "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 }
           }
         ]
       }
     }
   }
   ```

   `apiKey` is required by the schema — any placeholder works for an
   unauthenticated local server. See the
   [pi docs](https://github.com/earendil-works/pi) for the full
   `models.json` schema and the other supported `api` values.

3. **Start the session** naming the registered model id. Custom model names
   match no harness-autoselection hint, so select pi explicitly:

   ```bash
   LMER_LLM_NAME=my-local-model lmer develop <target> --harness pi
   ```

   (or set `LMER_HARNESS=pi` alongside `LMER_LLM_NAME` in your `.env`).
   The pi runner logs
   `✅ pi custom model registry found at ~/.pi/agent/models.json` when the
   mount is in place.

**`baseUrl` is dialed from inside the container** — it must be an address
the container can reach: a LAN/bridge IP as above, or
`host.docker.internal` when the server runs on the container host.
`localhost`/`127.0.0.1` would point at the container itself and fail.

## Sandbox and approval posture

The lmer container is the security boundary (resource limits,
`no-new-privileges`, isolated home). Inside it:

- **claude** — permission prompts per `agent-files/claude/settings.json`
  allowlist; danger zone passes `--allow-dangerously-skip-permissions`.
- **codex** — codex's own bwrap/seccomp sandbox cannot initialize under the
  container's `no-new-privileges`, so the runner always passes
  `--sandbox danger-full-access` (OpenAI's documented guidance for
  containerized use) and keeps `--ask-for-approval on-request`; danger zone
  passes `--dangerously-bypass-approvals-and-sandbox`.
- **pi** — no tool-call prompts at all (see matrix note 5).

## Architecture

```
lmer (host)                              container
──────────────                           ────────────────────────────────────
--harness / LMER_HARNESS                 clone_and_exec.py
  └─ lmer_cli/harness.py registry   ──►    └─ dispatches <name>-runner token
      • runner token                       libexec/<name>-runner.sh
      • credential mounts (mounts.py)        • sources harness-common.sh
      • build cache-bust (build.py)          • credentials check
      • supervisor profile                   • config provisioning
                                             • env → CLI flag mapping
                                             • exec lmer-supervisor -- <binary>
                                           lmer-supervisor
                                             • ready marker, start command,
                                               quit sequence from the profile
```

- `src/lmer_cli/harness.py` — the registry; single source of truth for
  everything the host CLI and supervisor know per harness.
- `libexec/harness-common.sh` — shared runner steps (session id, self-dev
  detection, global-dir discovery, config provisioning, global context file,
  slash-command prompt templates, agent memory restore, supervisor exec).
- `libexec/<name>-runner.sh` — per-harness runner: credential checks, config
  provisioning, `LMER_*` → CLI flag mapping, final exec.
- `agent-files/<name>/` — per-harness base config provisioned into the
  harness's expected location at session start (an existing file always wins;
  the work repo's `agent-files/<name>/` overrides lmer's on collision).
- `claude-runner.sh` predates the shared helpers and keeps its own inline
  copies — its byte-for-byte stability is the backward-compatibility contract.

### Supervisor profile

The PTY supervisor drives each TUI differently; the profile lives in the
registry and every field has an env override for patching without a release:

| Field          | claude        | codex         | pi                | Env override                    |
|----------------|---------------|---------------|-------------------|---------------------------------|
| Ready marker   | `❯`           | `›`           | `Press ctrl+o`    | `LMER_AUTO_START_READY_MARKER`  |
| Start command  | `/start`      | generic text  | generic text      | `LMER_START_COMMAND`            |
| Quit sequence  | Ctrl-C ×2     | `/quit` + CR  | Ctrl-C ×2         | `LMER_QUIT_SEQUENCE`            |
| Ready timeout  | 15s (global)  | 15s (global)  | 60s               | `LMER_AUTO_START_READY_TIMEOUT` |

`LMER_QUIT_SEQUENCE` steps are separated by `|` and unicode-escape decoded
(e.g. `\x03|\x03`, or `/quit\r`); an empty value disables the chord step so a
self-shutdown escalates straight to SIGTERM.

## Adding a new harness

The framework is registry-driven; adding a harness is a checklist, not a
refactor:

1. **Registry entry** — add a `Harness` to `HARNESSES` in
   `src/lmer_cli/harness.py`: name, binary, `<name>-runner` token/script,
   credential mounts, supervisor profile (ready marker, start command, quit
   sequence, optional ready timeout), `<NAME>_CACHE_BUST` build-arg, and any
   fixed container env. If the harness has recognizable model family names,
   optionally add them to `MODEL_HARNESS_HINTS` so `LMER_LLM_NAME` can
   autoselect it.
2. **Dispatch token** — add `<name>-runner` to `KNOWN_HARNESS_RUNNERS` in
   `src/lmer_cli/container/clone_and_exec.py` (standalone copy; the
   registry-sync test will fail until both places agree).
3. **Runner script** — create `libexec/<name>-runner.sh` (executable):
   source `harness-common.sh`, call `harness_init`, check credentials, call
   `harness_provision_config` / `harness_render_global_context`, map
   `LMER_LLM_NAME` / `LMER_REASONING_EFFORT` / `LMER_DANGER_ZONE` to the
   harness's flags, and finish with `harness_exec <binary> $EXTRA_ARGS "$@"`.
   For the effort mapping use `harness_map_effort` (owns the shared tier
   semantics: `max`→`xhigh`, `auto`/unset→no flag, unknown→warn+skip) and
   format only the harness-specific flag from its output — don't re-implement
   the case block (see codex-/pi-runner). If the harness loads prompt
   templates from a directory, call `harness_render_prompt_templates
   <that-dir>` to deliver lmer's slash commands; call
   `harness_restore_memory` to restore persisted agent memory (both are
   quiet no-ops when unconfigured).
4. **Base config** — add `agent-files/<name>/` with the harness's base config
   file(s) referenced by the runner.
5. **Image install** — add an `ARG <NAME>_CACHE_BUST=0` + install `RUN` to the
   Containerfile (alternative-harnesses section).
6. **Tests** — extend `tests/test_harness.py` (registry shape is
   parametrized — mostly free) and add a runner class to
   `tests/test_harness_runners.py` using the stub-binary helper.
7. **Docs** — add the harness to the tables in this file, and to the
   authentication table in `docs/AUTHENTICATION.md`. Add a CHANGELOG.yaml
   entry.

Conventions that keep this cheap: the runner token/script MUST be named
`<name>-runner` / `<name>-runner.sh`, the cache-bust arg `<NAME>_CACHE_BUST`,
and `LMER_REASONING_EFFORT` accepts `low|medium|high|xhigh|max|auto` — map
`max` to the harness's top tier and warn-and-skip anything unknown.
