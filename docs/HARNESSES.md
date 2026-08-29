# Agent Harnesses

lmer runs an *agent harness* — the AI coding-agent CLI — inside each session
container. Claude Code is the default and most fully integrated harness;
Codex and pi are supported at the **core tier** (see the capability matrix
below). Additional harnesses can be installed without forking lmer — see
[User-installed harnesses](#user-installed-harnesses).

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
| Output styles (`agent-files` output-styles)  | ✅⁹    | ❌    | ❌  |
| Lifecycle hook guards                        | ✅¹⁰   | ⚠️¹⁰  | ❌¹⁰ |
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
7. Rendered per session for pi from the claude command files
   (`agent-files/claude/commands/*.md`) into native *prompt templates* by
   `harness_render_prompt_templates` (`lmer_cli.container.prompt_templates`),
   invoked as `/start`, `/followup`, … with autocomplete. Current Codex
   releases no longer discover custom prompt files: lmer starts Codex with a
   plain-text instruction, and a control-plane `/followup` becomes a plain-text
   instruction to run `hooks/followup.sh`; direct terminal users type that
   instruction themselves. Work-repo commands override global ones of the same
   name for pi, mirroring the claude layout. Claude-specific frontmatter
   (`allowed-tools`) is dropped in conversion, and a leading-`!` execution
   line becomes an instruction to run that command — these harnesses expand
   the template as prompt text and then run the command as a tool call.
8. Restore is automated in the runner (`work memory restore`, gated on
   `LMER_PERSIST_AGENT_MEMORY` exactly like claude). These harnesses have no
   native memory feature, so the read/write/persist contract is injected
   into the global context file (the `prompts/agent-memory.md` fragment,
   note 3's mechanism); persisting back remains the agent's
   `work memory persist`, as on claude. A harness with a *native* memory feature also
   declares where it keeps it (`memory_dir` in `src/lmer_cli/harness.py` —
   claude only today), which is what lets the platform mount that directory out
   of the container: the platform's assistant persists its memory through a host
   mount rather than the work repo (issue #325 — see
   [PLATFORM-QUICKSTART.md](./PLATFORM-QUICKSTART.md#uber-lmers-memory)).
   **That store is read natively on claude only.** The mount and the symlink are
   made on every assistant spawn whatever harness the session runs, but a codex
   or pi assistant has no feature that reads the path and does not get the
   `prompts/agent-memory.md` fragment either — the fragment is gated on
   `LMER_PERSIST_AGENT_MEMORY`, which assistant spawns blank. What tells such a
   session the store exists, and where, is the `orchestrate` taskdef, which names
   the path and says to read and write it by hand. A harness that grows a native
   store is a one-field change away from the claude treatment.
9. Claude-only: codex and pi have no equivalent feature and get nothing, so
   anything *every* harness must obey belongs in the taskdef prompt rather
   than in a style. Shipping a style and selecting one are separate
   mechanisms, and a style reaches neither subagents nor the built-in coding
   instructions by default — see
   [LMER-CLI.md](LMER-CLI.md#output-styles-shipping-one-and-selecting-one-are-separate).
10. Claude fires the full lmer Stop/SessionEnd guard set. Codex fires one narrow
    Stop guard: in an orchestrated interactive session, an unread `lmer-ask`
    answer triggers Codex's native continuation immediately; otherwise an open
    question keeps Stop waiting for the first answer. The oldest unread answer
    wins, including one already present when Stop fires, so the agent can run
    `lmer-ask wait` in a real new turn. The hook never reads or embeds the answer;
    the wait command prints it through the normal terminal path. The hook is
    installed as an image-managed policy from
    `agent-files/codex/requirements.toml`, which pins the `hooks` feature on and
    makes the lmer-owned hook trusted without bypassing Codex's review of user
    or project hooks. It fails open on channel errors, a fully settled channel,
    repeated Stop-hook turns, `LMER_NONINTERACTIVE` children, and after its
    3540-second timeout.

    The remaining Stop-hook guards are still claude-only — including the
    **signal reminder** (`hooks/signal_guard.py`, issue #289), which reminds an
    orchestrated session that ends a turn on an unreported milestone to run
    `lmer-signal`. **codex and pi runs have no mechanical signal reminder at
    all** until the daemon-side watch (issue #294) lands; they keep only the
    prose instruction in the orchestrator-ask prompt fragment
    (`prompts/orchestrator-ask.md`), so an unsignalled milestone on those
    harnesses still surfaces only through the orchestrator's stalled-run
    digest. The same holds for the run-state, SessionEnd, and Slack reply
    guards. Pi fires no lifecycle hook guards.

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

## Non-interactive exec mode (`spawn-harness`)

Besides driving each harness's TUI, the registry also knows how to run every
harness as a **non-interactive child process** — the mechanism behind the
`--agents` fan-out (issue #130): the orchestrating session's agent runs
`spawn-harness <agent-name>` to fan a task out to additional
harness/model configurations and consolidate the results itself.

```bash
# Names resolved from --agents at launch (spawn-harness --list shows them)
spawn-harness sol-review --prompt-file prompt.md \
    --env LMER_REVIEW_ON_MR=0 --output agents/sol-review.md --timeout 1800
```

Each `ExecProfile` in the registry (`src/lmer_cli/harness.py`) carries the
harness's exec invocation; `spawn-harness`
(`lmer_cli.container.spawn_harness`, wrapper in `bin/`) selects the child's
harness as: `LMER_HARNESS` set by the agent's own config (preset env /
`--env` pairs) > the model hint from the agent's own `LMER_LLM_NAME` > the
orchestrating session's inherited harness > claude — the inherited harness
never shadows a model-only agent preset, and conversely the session's
inherited model never re-routes an agent that configures nothing (the
operator's explicit `--harness` already beat that hint at launch). It then
maps `LMER_LLM_NAME` / `LMER_REASONING_EFFORT` to the harness's flags (same
tier semantics as the interactive runners: `max` → the harness's top tier;
an inherited model whose family implies a different harness than the
child's is dropped rather than handed to a cross-harness child as a foreign
`--model`), appends the prompt as the final argument, and mirrors the
child's exit code (a signal death maps to `128+N`; 124 on `--timeout`
expiry, which kills the child's whole process group — as does interrupting
`spawn-harness` itself with SIGINT/SIGTERM, so a cancelled fan-out never
leaves a detached child running). Liveness and failure
are observable without watching the exit code: a heartbeat line is printed
to stderr while the child runs (`--heartbeat`, default 60s, 0 disables —
harnesses buffer their final answer, so an empty output file says nothing
about progress), and when a child dies its `--output` file gets a
`[spawn-harness] child FAILED` footer carrying the exit reason and the
stderr tail, so a failed agent's output explains itself.

The mirrored exit code outranks the footer: once the child has run, a footer
`spawn-harness` cannot write — a full or read-only filesystem — warns on stderr
(`cannot append the failure footer to <path>`) and leaves the code alone, rather
than replacing the 124 or the child's own status with a traceback from the
wrapper (issue #151). Closing the captured output is guarded the same way, since
a buffered write can succeed and only surface its `ENOSPC` when the buffer
drains.

A child that *succeeds while saying nothing at all* is the failure the exit
code cannot express — the shape behind issue #137, where a child ended its
turn on `Shall I proceed with this fix? (yes/no)` and exited 0, so the
orchestrator consolidated from N-1 agents with every signal looking healthy.
After a child exits 0, `spawn-harness` therefore inspects the captured
`--output` and flags three degenerate shapes (issue #139): an empty file,
whitespace-only content, and content below a 10-character floor (low on
purpose — a real `no findings` must survive; only stubs like `ok` and `n/a`
are caught). A hit warns on stderr naming the agent, the reason and the path,
and appends a `[spawn-harness] child produced NO USABLE OUTPUT` footer —
deliberately a different marker from `child FAILED`, because the exit code
genuinely was 0 and "the agent died" and "the agent returned nothing" call for
different responses. **The mirrored exit code is unchanged**: this warns, it
never turns a terse answer into a hard failure.

All three signals are properties of the bytes. **Whether prose amounts to a
complete answer is deliberately not judged here** — including #137's own
halt-and-ask shape, which this check therefore does not catch. Earlier versions
tried: matching approval phrases on the last line, accepting `(y/n)` spellings,
requiring a terminal `?`, grading the response by output length. Each reduced to
inferring intent from phrasing, and a child can halt for any number of reasons
worded any number of ways — an open question with no yes/no framing, or no
question at all. Nothing separates the classes structurally: `Do you want me to
apply the patch?` (34 characters, a halt) and `Looks fine. Should we also check
the retry path?` (47, a real answer) are the same shape at the same size. The
harness's own result envelope does not settle it either, since a model that
stops to ask still ends its turn normally and reports success. Reading a
half-finished answer as half-finished is a judgment about content — a job for a
model, not a heuristic. Issue #153 tracks what the harnesses *can* report
structurally (turn-limit truncation, in-band errors); issue #138 covers the
hook-side session signal for sessions that fire hooks, which `spawn-harness`
children do not.

Without `--output` the same check still applies (issue #152). The child's
stdout is *teed* rather than inherited: every byte is forwarded to
`spawn-harness`'s own stdout unchanged, while a running tally measures what a
captured file would have measured — total length and the span between the first
and last non-whitespace character, which is `len(content.strip())` exactly. Both
paths ask the same rule set (`classify_degenerate_counts`), so a fan-out gets the
same verdict whether or not it remembered to capture. Two differences remain, and
neither is a caveat about detection: there is no file to append a footer to, so
the warning says so and stands alone; and in this mode the child's stdout is a
pipe rather than whatever `spawn-harness` itself was given, which a harness that
inspects `isatty()` could in principle notice. The earlier behavior — the check
silently skipped, so a fan-out that forgot `--output` consolidated from N-1
agents with every signal looking healthy — is gone.

One pipeline behavior does change with the tee, and it is a deliberate trade. If
`spawn-harness`'s own stdout dies while the child is running (`spawn-harness … |
head`, a consumer that was killed), the child used to inherit that descriptor and
take the `EPIPE` itself, ending in a fraction of a second. Now the tee absorbs
it: the failure is reported once on stderr (`cannot forward the child's
stdout`), the child's output stops being passed through, and its stdout keeps
being **drained** so the child runs to completion and its exit code is still
mirrored. Draining is not optional — with nothing reading the pipe the child
blocks as soon as the 64 KiB buffer fills, and `--timeout` has no default, so the
wrapper would hang indefinitely while the heartbeat reported a healthy run.

| | claude | codex | pi |
|---|---|---|---|
| Invocation | `claude -p` | `codex exec` | `pi -p` |
| Permission posture | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` | `--no-approve` (pi never prompts) |
| Statelessness | `--no-session-persistence` | `--ephemeral` | `--no-session` |
| Model / effort | `--model` / `--effort` (accepts `max`) | `--model` / `-c model_reasoning_effort=` (`max`→`xhigh`) | `--model` / `--thinking` (`max`→`xhigh`, matching the interactive runner) |
| Prompt safety | `--` before the prompt | `--` before the prompt | no `--` support — a prompt starting with `-` is rejected |

Children cannot answer permission prompts, so every profile carries its
harness's bypass posture — the lmer container is the security boundary, the
same doctrine pi applies to interactive sessions. The bypass flags live in
the profile's dedicated `permission_bypass_args` field and are appended only
for `build_exec_argv(..., unattended=True)` (which `spawn-harness` passes):
a future consumer of the exec profiles must opt into permission-free
children explicitly, never inherit them from neutral registry data.
Children are also barred
from fanning out further: `spawn-harness` strips both fan-out pairs — the
container-side `LMER_SPAWN_AGENTS` / `LMER_SPAWN_AGENTS_CONFIG` it reads and
the host-input `LMER_AGENTS` / `LMER_AGENTS_CONFIG` — from the child
environment (no grandchildren, structurally).

Children equally cannot answer *approval* questions, which is a prompt-level
problem rather than a flag one: a child that obeys a rule telling it to stop
and ask ends its turn on an unanswerable question, exits 0, and leaves a
near-empty output file — the orchestrator then consolidates from N-1 agents
with nothing to indicate it lost one. `spawn-harness` therefore sets
`LMER_NONINTERACTIVE=1` on every child (unconditionally — no preset overlay
or `--env` pair can unset it) **and prepends the rule itself to the child's
prompt**: "state what you would have asked and why you stopped, in your final
output". The in-band copy is what actually steers the child — a fan-out child
execs its harness binary directly, so no runner script injects `AGENTS.md`
into a claude child's system prompt (Claude Code discovers only `CLAUDE.md`
natively), and no lmer path renders an environment value into any model's
context. The prompt is the one channel every harness reads identically. See
`LMER_NONINTERACTIVE` in [LMER-CLI.md](./LMER-CLI.md) for host-side use on
cron/CI launches, where the same text arrives as the
`prompts/non-interactive.md` fragment instead. How agents are configured and
selected at launch:
[Fan-out agents in docs/PRESETS.md](./PRESETS.md#fan-out-agents---agents--lmer_agents).

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
self-shutdown escalates straight to SIGTERM. `LMER_AUTO_START_READY_MARKER`
uses the same escape decoding (plain text like `❯` passes through
byte-for-byte; an empty value disables marker gating).

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

## User-installed harnesses

A harness can also be installed **without forking lmer** (issue #132): a
drop-in directory holding a declarative manifest plus the runner script that
launches the harness inside the session container. For a complete, paste-ready
real-CLI setup see the
[opencode walkthrough](./USER-HARNESS-OPENCODE.md); the sections below are
the reference.

```
~/.lmer/harnesses/<name>/
├── harness.json     # serialized registry entry (schema 1)
├── runner.sh        # in-container runner; installs its CLI if missing
└── agent-files/     # optional base config the runner provisions
```

The directory name is the harness name (lowercase `[a-z][a-z0-9_-]*`, max 64
chars); `LMER_HARNESSES_DIR` overrides the location. Definitions merge into
the registry at resolution time — `lmer --harness <name>`, `LMER_HARNESS`,
preset `--harness` folding, and `spawn-harness` fan-out selection all accept
user harnesses exactly like built-ins, and `lmer --harness` announces one
with a `[user-installed]` tag. A user harness can never shadow a built-in
name (a colliding directory is skipped with a warning), and its optional
`model_hints` are consulted only after every built-in hint, so it can never
steal `gpt-*` from codex.

Loading degrades gracefully, mirroring presets: a broken entry (malformed
JSON, unsupported schema, invalid name) is warned about and skipped so it
cannot break sessions that don't select it, while *selecting* a skipped or
missing harness fails with the normal unknown-harness error.

**Trust model** — the same as presets: the directory lives on the host and
is writable only by whoever runs lmer. The runner script is
operator-authored code that runs inside the container, which is already true
of the target repo itself; the container remains the security boundary. Be
aware of what the manifest can reach: `credential_mounts` can bind **any
regular file under the host home** into the container (rw by default) — so
treat harness directories with the same care as presets, and note that
`LMER_HARNESSES_DIR`/`LMER_HARNESS` are steerable from a cwd `.env` like
other lmer settings. Guardrails: directories are refused at mount time
(files only, unlike the manifest-free built-ins this is enforced), and every
user-harness credential mount is announced in the launch output (`🔑 …`) so
an unexpected entry is visible before the session starts — unconditionally,
not only under `--verbose`/`LMER_VERBOSE`. The manifest's
`permission_bypass_args` keep the built-in registry's opt-in gate: they
apply only to unattended `spawn-harness` children, never by default.

### Manifest (`harness.json`, schema 1)

Only `schema` and `binary` are required; everything else defaults to the
safest behavior (no ready-marker gating, the generic start instruction, no
quit chord, no credential mounts, empty exec profile).

```json
{
  "schema": 1,
  "description": "ACME agent CLI",
  "binary": "acme",
  "credential_mounts": [
    {"host_path": ".acme/auth.json", "container_path": "/home/developer/.acme/auth.json"},
    {"host_path": ".acme/models.json", "container_path": "/home/developer/.acme/models.json", "mode": "ro"}
  ],
  "supervisor": {
    "ready_marker": "\\u276f",
    "start_command": "",
    "quit_sequence": ["\\x03", "\\x03"],
    "ready_timeout": 60
  },
  "exec": {
    "base_args": ["-p"],
    "permission_bypass_args": ["--yolo"],
    "model_args": ["--model", "{model}"],
    "effort_args": ["--effort", "{effort}"],
    "effort_max_value": "xhigh",
    "dashdash_before_prompt": true
  },
  "model_hints": ["acme"],
  "extra_env": {"ACME_NO_UPDATE": "1"},
  "session_dir": "/home/developer/.acme/sessions"
}
```

Field semantics match the built-in registry dataclasses in
`src/lmer_cli/harness.py` (`CredentialMount`, `SupervisorProfile`,
`ExecProfile`); `credential_mounts[].host_path` is home-relative,
`mode` defaults to `rw`. The byte-valued supervisor fields (`ready_marker`,
`quit_sequence` steps) are unicode-escape decoded — the same encoding as
their runtime env overrides (`LMER_AUTO_START_READY_MARKER`,
`LMER_QUIT_SEQUENCE`), which remain available for debugging a user harness
without editing the manifest. An empty `start_command` means the generic
start instruction (see `GENERIC_START_COMMAND`); set it only if the harness
has a native way to load the task instructions. `exec` powers `spawn-harness`
fan-out children (see the exec-mode section above); leave it minimal if the
harness won't be used as a fan-out child.

`session_dir` is optional and names the **absolute container directory** the
harness writes its session JSONL under (`~/.acme/sessions` for the example
above). The orchestrator (`lmer platform`) mounts a host directory there for
every harness that declares one, so a spawned session's transcript survives the
`--rm` container and the chat view can read it back; a harness that declares
nothing simply has no transcript on the host. An invalid value (relative, or
containing `..`, `:`, `,` or whitespace) is warned about and ignored — the
harness still loads, since a mis-declared transcript path must not cost the
session. The platform imposes two more conditions at spawn time, with the same
warn-and-ignore outcome: the directory must sit **strictly below
`/home/developer`** (the container home — every harness checked keeps its
sessions under `$HOME`), and it must not be, or contain, a directory the
platform already mounts, nor sit inside the mount staging area described
below. A value outside those bounds means the harness runs with no transcript
on the host, and the reason appears only in the platform daemon's log. What the
mount does and does not buy the chat view is
[Transcript visibility](#transcript-visibility-orchestrator-chat-view).

**Every user-harness mount below the container home** — both
`credential_mounts` and `session_dir` — is **delivered via a staged mount plus
an in-container symlink**, because a manifest may name a path whose parents the
image does not ship. (Built-in harnesses are not staged: the image ships their
`~/.claude`, `~/.codex`, `~/.pi` developer-owned.) The bind lands under
`/home/developer/.lmer-mounts`, and the container entrypoint links the declared
path to it before any harness starts. The reason is ownership: a bind mount's
missing parent directories are created by the container runtime as **root**,
before any container process exists, and the session runs as `developer` with
no-new-privileges — so nothing inside can chown them. A harness that writes a
sibling file next to its own mount (opencode's `mkdir
~/.local/share/opencode/repos` beside its `auth.json`) would die at startup with
EACCES. The symlink is created by the
container user, so the parent chain is developer-owned and those writes work. No
manifest change is needed — declare the path the harness actually uses; the
pairs travel as `LMER_MOUNT_LINKS` (see [LMER-CLI.md](./LMER-CLI.md)).

A `credential_mounts[].container_path` **outside** the container home (say
`/etc/acme/auth.json`) binds directly at its declared path instead: staging
cannot help there — the container user cannot create `/etc/acme`, so the
credential would land where the harness never looks — and it is not needed
either, since root-owned parents only hurt a harness *writing beside* its
mount, which it cannot do outside `$HOME` in any case. Reading a
directly-mounted credential is unaffected. (`session_dir` has no such case: a
declared value outside the container home is refused outright, above.)

### Runner script (`runner.sh`)

The runner owns two things a built-in harness gets from the image: **CLI
availability** (install-if-missing at session start — there is no
Containerfile layer for user harnesses) and config provisioning. lmer mounts
a persistent cache volume and exposes it as `LMER_HARNESS_CACHE`
(`/lmer-harness-cache/<name>`) so only the first session pays the install
cost; `lmer build --update-harness` does not apply — wipe
`~/.lmer/harness-cache/<name>` on the host to force a reinstall.

A complete example, following the same shape as `codex-runner.sh` /
`pi-runner.sh`:

```bash
#!/bin/bash
# ~/.lmer/harnesses/acme/runner.sh
HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source /Agents/global/libexec/harness-common.sh
harness_init

# Install-if-missing into the persistent cache volume.
export LMER_HARNESS_CACHE="${LMER_HARNESS_CACHE:-$HOME/.cache/lmer-harness/acme}"
export PATH="$LMER_HARNESS_CACHE/bin:$PATH"
if ! command -v acme >/dev/null 2>&1; then
    echo "📦 Installing acme CLI (first session with this cache)..."
    npm install -g --prefix "$LMER_HARNESS_CACHE" acme-cli || {
        echo "❌ acme CLI install failed"; exit 1; }
fi

# Credentials arrive via the manifest's credential_mounts (or env/.env).
[ -f "$HOME/.acme/auth.json" ] || echo "⚠️  No acme credentials found — the session may fail to authenticate"

# Base config: work-repo agent-files override, then the copy shipped in
# this harness directory (the optional third argument).
harness_provision_config "acme/settings.json" "$HOME/.acme/settings.json" \
    "$HARNESS_DIR/agent-files/settings.json"
harness_render_global_context "$HOME/.acme/AGENTS.md"
harness_render_prompt_templates "$HOME/.acme/prompts"
harness_restore_memory

EXTRA_ARGS=""
[ -n "$LMER_LLM_NAME" ] && EXTRA_ARGS="--model $LMER_LLM_NAME"
effort="$(harness_map_effort)"
[ -n "$effort" ] && EXTRA_ARGS="$EXTRA_ARGS --effort $effort"

harness_exec acme $EXTRA_ARGS "$@"
```

The helpers deliver the core tier automatically: `harness_render_global_context`
(user AGENTS.md + human identity + memory contract),
`harness_render_prompt_templates` (lmer's slash commands converted to the
harness's prompt-template format, if it loads one from a directory),
`harness_restore_memory`, `harness_map_effort` (shared tier semantics), and
`harness_exec` (runs through `lmer-supervisor` using the manifest's
supervisor profile). The runner is executed via `bash` inside the container,
so the host file needs no exec bit.

### How it works / limitations

The host mounts `~/.lmer/harnesses` read-only at `/lmer-harnesses` and
forwards the mount point as `LMER_HARNESSES_DIR`, so the host CLI,
`lmer-supervisor`, and `spawn-harness` all resolve the same definitions from
the same files; the container entrypoint dispatches `<name>-runner` tokens
it doesn't recognize as built-ins to `<dir>/<name>/runner.sh` purely by file
existence.

- "No Containerfile changes" ≠ "no rebuild" for every install: the
  in-container dispatch pieces resolve from `/Agents/global`, which is
  live-mounted from a source checkout when lmer runs from one, but comes
  from the baked image copy otherwise — on such installs, run `lmer build`
  once after upgrading to a user-harness-aware lmer before a user harness
  will dispatch in-container.

- User harnesses run at the **core capability tier** (same as codex/pi —
  see the matrix above); the claude-only features are out of reach
  regardless of how the harness is installed.
- A user harness works as a `spawn-harness` **fan-out child** only if its
  binary is already installed — children run the exec profile directly,
  never `runner.sh`. `spawn-harness` prepends the child harness's cache bin
  directory (`/lmer-harness-cache/<name>/bin`) to the child PATH, so:
  same-harness children always work (the session's runner installed the
  CLI), and a *different* user harness as a child works when a previous
  session installed its CLI at that conventional location; otherwise the
  child fails with a clear `command not found`.
- The empirically fragile bits of TUI driving are the ready marker and quit
  sequence; use the env overrides to iterate, and `--no-supervisor` as the
  escape hatch.

### Transcript visibility (orchestrator chat view)

Declaring `session_dir` is all a user harness does to get its session files
onto the host (readability is the tier question below). `lmer platform`
creates one host directory per
declaring harness underneath the session's own transcript directory and mounts
it read-write into the container — at the declared path for a built-in, at a
staged path the entrypoint symlinks the declared one to for a user harness (see
the staged-mount note above) — so what the harness writes there
survives the `--rm` container; the `.jsonl` files in it get the credential-shape
scrub when the session ends (a masking pass, not a guarantee) and are what
`GET /api/sessions/{id}/messages` reads back
(`_prepare_transcript_subdirs` / `_transcript_mount_flags` in
`src/lmer_platform/spawn.py`). A harness that declares nothing runs exactly as
before and leaves no transcript on the host. The declared directory must sit
strictly below the container home and must not be, or contain, a directory the
platform already mounts — a value outside those bounds is skipped with a
warning in the platform daemon's log, and the harness runs without a
transcript mount.

**Mounting is not reading**, and there are exactly two tiers of format support:

- **Adapter tier — the maintained harnesses.** Claude Code, pi (session format
  3) and codex (rollout format) have per-record adapters in
  `src/lmer_platform/transcripts.py`, so their native session files render as a
  conversation: roles, text, timestamps, tool calls with their outcome. That set
  is **closed** — no further in-tree dialect adapters are added unless the
  operator says otherwise, and every other format integrates via the canonical
  tier below. Adapter-tier messages win mechanically if a session contains both
  a readable native transcript and readable canonical derived output: one
  warning is logged and only native messages render (including for halt
  detection). Source metadata keeps the detected record vocabulary separate
  from the cosmetic harness label. The canonical twin should not exist for an
  adapter-supported harness; precedence prevents it from doubling the view.
- **Canonical tier — any drop-in.** A user harness makes its transcripts
  readable by shipping an **in-container converter**: its own code, running
  where the harness already runs, tailing the harness's native session files and
  appending records in the documented
  [lmer transcript format](./TRANSCRIPT-FORMAT.md) to a `.jsonl` file inside the
  same declared `session_dir`. The reader recognises those records by their
  `type`, per record, so nothing host-side has to be told the converter exists —
  no manifest key, no in-tree change, no second mount.

A format with neither — no adapter, no converter — is reported explicitly: the
page comes back empty carrying the note *"The transcript for this run is on disk
but has nothing to show yet … so does one whose transcript this build cannot
read."* Never a silently blank page, which would read as "this run said
nothing". The terminal log remains the complete record in every case.

A drop-in cannot supply host-side *code* — the daemon never executes anything
out of user-writable `~/.lmer/harnesses` — and does not need to: it ships an
in-container converter instead, which is the same trust class as its
`runner.sh`. A drop-in that *wraps* claude, codex or pi must **not** ship a
converter; its native files already render.

Worked example: [`examples/harnesses/opencode/`](../examples/harnesses/opencode/)
is a complete, copy-installable drop-in — `harness.json`, `runner.sh`,
`converter.py` — demonstrating the canonical tier end to end, with the
walkthrough in [USER-HARNESS-OPENCODE.md](./USER-HARNESS-OPENCODE.md). It is
also the *decoupled* case: opencode keeps its sessions in a SQLite database, so
nothing native lands in `session_dir` at all and the declared directory is
purely the converter's output home — `session_dir` means "where readable
transcripts appear", not "where the harness happens to write".

Only `.jsonl` files inside the declared directory are discovered, recursively —
a harness that writes `.json` or `.log` mounts out but never reads back — and a
symlink there resolving outside the session's own transcript directory is
refused rather than followed, since the directory is container-writable and the
link would serve another run's conversation (`_jsonl_files` in
`src/lmer_platform/transcripts.py`).

### When the CLI doesn't fit the flag model

The [opencode walkthrough](./USER-HARNESS-OPENCODE.md) is the happy path: a
CLI whose model, effort, and credentials map 1:1 onto flags and a mounted
auth file. The manifest+runner shape has absorbed CLIs that *don't* fit that
mould without any change to lmer — but a few recurring frictions are worth
knowing before you write the manifest. (These generalize from field-testing
a second, env-configured CLI against the same mechanism.)

- **The exec profile assumes flags carry the config.** `model_args` /
  `effort_args` only help a CLI that accepts a model/effort *as a flag*. A
  CLI configured purely through the environment (an API key and model id in
  its own env vars, no `--model`) can express none of that in the manifest,
  and `build_exec_argv` now **warns** rather than silently dropping a
  supplied model when `model_args` is empty. The escape hatch: point
  `binary` at a small **wrapper script your `runner.sh` writes into the
  cache bin dir** (`$LMER_HARNESS_CACHE/bin`), translating
  `LMER_LLM_NAME`/`LMER_REASONING_EFFORT` into whatever env the CLI wants.
  This is also the **only** way fan-out children get per-child model/effort,
  since children exec `binary` directly and never run `runner.sh`.
- **The prompt is always the last argv token.** `build_exec_argv` appends
  the prompt last, so a CLI where the prompt is itself a *flag value* (e.g.
  `mycli -p <prompt>`) cannot also take `model_args`/`effort_args` after it
  — they would be swallowed as the prompt flag's value. Put such flags in
  `base_args` with fixed values, or handle them in the wrapper. (A positional
  prompt, like opencode's, sidesteps this entirely.)
- **Credential filenames aren't always mountable.** `credential_mounts`
  rejects paths containing `:`/`,`/whitespace (they would corrupt the `-v`
  spec), so a harness that stores its token in a file whose name is derived
  from a provider id (e.g. `credentials/managed:some-provider.json`, note
  the colon) simply can't be mounted. Fall back to env-key auth: put the
  provider key in your `.env` and let the harness read it from the
  environment.
- **Permission posture does not port** — and copying one can backfire.
  Translating claude's `settings.json` allowlist into another CLI's config
  format is mechanical but treacherous: allow/ask precedence, glob
  semantics, and chain ordering differ per CLI, and a mistranslation can
  leave a catch-all `ask` outranking both your allowlist and the
  danger-zone bypass. Since the container is the security boundary and lmer
  is typically run in danger zone anyway, the honest default for a new
  harness is **no permission rules at all** (let the CLI/danger-zone run
  unprompted) rather than a half-ported allowlist.
- **Slash-command portability is format-deep, not behavior-deep.** The
  `harness_render_prompt_templates` output (frontmatter `description` +
  `$ARGUMENTS`) is accepted by more than the codex/pi prompt-template
  directories — a CLI whose "commands" are flat *skills* can consume the
  same files. But watch the semantics: if that CLI's skills are
  model-invocable, the rendered commands need whatever opt-out keeps them
  operator-only (e.g. a `disableModelInvocation: true` frontmatter key) so
  the model doesn't auto-run `/gate-commit`. Format compatibility ≠
  behavioral compatibility.
- **Auto-start can hang silently on a strict submit heuristic.** The
  supervisor types the start instruction followed by CR. A TUI with a
  paste-burst heuristic (treating typed-text-plus-`\r` in one write as a
  paste, not a submit) needs the supervisor's follow-up CR nudges to
  actually submit; a CLI with a stricter heuristic can sit at the ready
  marker with the instruction typed but never sent, and no error. If
  auto-start hangs with your instruction visible but unsent, that's the
  cause — iterate the quit/start behavior with `--no-supervisor` and the
  supervisor env overrides.
