# Worked example: opencode as a user-installed harness

This is a complete, paste-ready setup of [opencode](https://opencode.ai)
(sst/opencode) as a **user-installed harness** — the no-fork mechanism from
[HARNESSES.md § User-installed harnesses](./HARNESSES.md#user-installed-harnesses).
opencode was evaluated as a built-in harness (#86) and dropped (its TUI has
no reasoning-effort flag, and pi covers the multi-provider slot), which makes
it the honest test of the plug-in path: everything below was verified against
**opencode 1.18.4** (the supervisor profile was additionally live-verified
against 1.18.1 during the original built-in evaluation).

## Layout

```
~/.lmer/harnesses/opencode/
├── harness.json
├── runner.sh
├── converter.py
└── agent-files/
    └── opencode.json
```

Every file below is checked in, ready to copy, at
[`examples/harnesses/opencode/`](../examples/harnesses/opencode/).

## 1. `harness.json`

```json
{
  "schema": 1,
  "description": "OpenCode (opencode.ai) — user-installed, core tier",
  "binary": "opencode",
  "credential_mounts": [
    {
      "host_path": ".local/share/opencode/auth.json",
      "container_path": "/home/developer/.local/share/opencode/auth.json"
    }
  ],
  "session_dir": "/home/developer/.opencode-transcripts",
  "supervisor": {
    "ready_marker": "Ask anything...",
    "quit_sequence": ["\\x1b", "\\x03", "\\x03"]
  },
  "exec": {
    "base_args": ["run"],
    "permission_bypass_args": ["--auto"],
    "model_args": ["--model", "{model}"],
    "effort_args": ["--variant", "{effort}"]
  },
  "extra_env": {"OPENCODE_DISABLE_AUTOUPDATE": "1"}
}
```

Field notes:

- **`credential_mounts`** — `opencode auth login` on the host writes
  `~/.local/share/opencode/auth.json`; this entry bind-mounts it (rw, the
  default) so **logging in on the host IS logging in the container**, and a
  token refresh inside a session writes back to the host file. See
  [Authentication](#4-authentication-log-in-on-the-host).
- **`session_dir`** — where readable transcripts appear, **not** where
  opencode writes: it keeps its sessions in a SQLite database, so nothing
  native ever lands here and this directory is purely the converter's output
  home (see [Transcripts](#6-transcripts-in-the-chat-view)). Any absolute path
  below the container home that the platform does not already mount works;
  this one is unique to the harness and covers nothing.
  (`~/.local/share/opencode` itself would be *refused* — it contains the
  already-mounted `auth.json`.)
- **`supervisor.ready_marker`** — `Ask anything...` is the stable prefix of
  the TUI's empty-input placeholder. `quit_sequence`: Esc dismisses/interrupts,
  then Ctrl-C twice (first clears input, second fires `app_exit`, which is
  only bound while the input is empty). If a future opencode version changes
  these strings, patch per-session via `LMER_AUTO_START_READY_MARKER` /
  `LMER_QUIT_SEQUENCE` and then update the manifest.
- **`exec`** — powers `spawn-harness` fan-out children:
  `opencode run --auto --model <m> --variant <e> <prompt>`. `--auto`
  (auto-approve permissions) is the unattended posture and, per the registry
  contract, applies **only** to unattended children — never to interactive
  sessions. `--variant` values are provider-specific (anthropic knows
  `high`/`max`); lmer's `low`/`medium` tiers may be rejected by some
  providers — `LMER_REASONING_EFFORT=auto`/unset passes no flag.
  `dashdash_before_prompt` stays false (yargs `--` handling is not relied
  on), so a fan-out prompt starting with `-` is rejected with a clear error.
- **No `model_hints`** — opencode model ids are `provider/model`
  (`anthropic/claude-sonnet-5`), and family words inside them (`sonnet`)
  would match the *built-in* claude hint first anyway. **Always select this
  harness explicitly** (`--harness opencode` or `LMER_HARNESS=opencode`);
  never rely on model autoselection for it.

## 2. `runner.sh`

```bash
#!/bin/bash
# User-installed opencode harness runner (docs/HARNESSES.md § User-installed
# harnesses). Dispatched by the container entrypoint via bash — no exec bit
# needed on the host file.
HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source /Agents/global/libexec/harness-common.sh
harness_init

export HOME="${HOME:-/home/developer}"
# Session containers are ephemeral — never self-update mid-session.
export OPENCODE_DISABLE_AUTOUPDATE="${OPENCODE_DISABLE_AUTOUPDATE:-1}"

# ── CLI availability (runner-owned; user harnesses have no image layer) ──
export LMER_HARNESS_CACHE="${LMER_HARNESS_CACHE:-$HOME/.cache/lmer-harness/opencode}"
export PATH="$LMER_HARNESS_CACHE/bin:$PATH"
if ! command -v opencode >/dev/null 2>&1; then
    echo "📦 Installing opencode CLI into $LMER_HARNESS_CACHE (first session with this cache)..."
    npm install -g --prefix "$LMER_HARNESS_CACHE" opencode-ai || {
        echo "❌ opencode CLI install failed"; exit 1; }
fi

# ── Credentials (bind-mounted from the host by the manifest) ──
if [ -f "$HOME/.local/share/opencode/auth.json" ]; then
    echo "✅ OpenCode credentials found at ~/.local/share/opencode/auth.json"
elif [ -n "$ANTHROPIC_API_KEY" ] || [ -n "$OPENAI_API_KEY" ]; then
    echo "✅ Provider API key present in environment"
else
    echo "⚠️  No opencode credentials (auth.json missing, no provider API key in env)"
    echo "   Run 'opencode auth login' on the host, or set a provider key in your .env"
fi

# ── Config, global context, slash commands, agent memory ──
# Base config ships in this harness dir (third arg = lowest-priority
# fallback); a work-repo agent-files/opencode/opencode.json overrides it.
harness_provision_config "opencode/opencode.json" \
    "$HOME/.config/opencode/opencode.json" \
    "$HARNESS_DIR/agent-files/opencode.json"
harness_render_global_context "$HOME/.config/opencode/AGENTS.md"
# lmer's slash commands (/start, /followup, /rgr, gate commands, ...) as
# opencode custom commands (~/.config/opencode/command/<name>.md → /<name>).
harness_render_prompt_templates "$HOME/.config/opencode/command"
harness_restore_memory

# ── Transcripts (docs/TRANSCRIPT-FORMAT.md) ──
# opencode keeps its sessions in a SQLite database, so nothing native ever
# lands in the declared session_dir; the converter polls opencode's own reader
# and writes the canonical records the chat view understands. Backgrounded
# before harness_exec (which execs), so it is orphaned to the container's init
# and dies with the container — and fail-soft: it logs into the session
# directory when there is one and exits quietly otherwise, because a converter
# that cannot start must not cost the session.
CONVERTER_LOG_DIR="$HOME/.lmer-session"; [ -d "$CONVERTER_LOG_DIR" ] || CONVERTER_LOG_DIR="$HOME"
python3 "$HARNESS_DIR/converter.py" >>"$CONVERTER_LOG_DIR/converter.log" 2>&1 &

EXTRA_ARGS=""

# LMER_LLM_NAME → --model provider/model (verbatim; opencode validates).
if [ -n "$LMER_LLM_NAME" ]; then
    EXTRA_ARGS="$EXTRA_ARGS --model $LMER_LLM_NAME"
    echo "✅ OpenCode model: $LMER_LLM_NAME"
fi

# No LMER_REASONING_EFFORT mapping: the opencode TUI has no reasoning flag —
# only `opencode run` has --variant (verified on 1.18.1 and 1.18.4; this is
# the reason opencode is not a built-in harness). The exec profile still
# maps effort for non-interactive fan-out children.
if [ -n "$LMER_REASONING_EFFORT" ] && [ "${LMER_REASONING_EFFORT,,}" != "auto" ]; then
    echo "ℹ️  LMER_REASONING_EFFORT ignored for interactive opencode (TUI has no reasoning flag)"
fi

# Danger zone: flip every permission to allow via opencode's
# highest-priority config layer.
if [ "$LMER_DANGER_ZONE" = "1" ]; then
    echo "⚠️  DANGER ZONE: disabling all opencode permission prompts!"
    export OPENCODE_CONFIG_CONTENT='{"permission": "allow"}'
fi

# shellcheck disable=SC2086  # EXTRA_ARGS is intentionally word-split
harness_exec opencode $EXTRA_ARGS "$@"
```

## 3. `agent-files/opencode.json`

The base permission posture, approximating lmer's claude
`settings.json` allowlist (read-only tools and the gate/work tooling run
unprompted; edits and unknown commands ask). Provisioned by the runner
unless a config already exists; a work-repo `agent-files/opencode/opencode.json`
overrides it.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "permission": {
    "read": "allow",
    "glob": "allow",
    "grep": "allow",
    "edit": "ask",
    "webfetch": "allow",
    "websearch": "allow",
    "bash": {
      "awk *": "allow",
      "basename *": "allow",
      "cat *": "allow",
      "cut *": "allow",
      "date *": "allow",
      "diff *": "allow",
      "dirname *": "allow",
      "echo *": "allow",
      "env": "allow",
      "env *": "allow",
      "find *": "allow",
      "gate-check": "allow",
      "gate-check *": "allow",
      "gate-commit *": "allow",
      "git add *": "allow",
      "git branch": "allow",
      "git branch *": "allow",
      "git checkout *": "allow",
      "git config *": "allow",
      "git diff *": "allow",
      "git fetch *": "allow",
      "git log *": "allow",
      "git ls-files *": "allow",
      "git remote *": "allow",
      "git rev-parse *": "allow",
      "git show *": "allow",
      "git stash *": "allow",
      "git status": "allow",
      "git status *": "allow",
      "gitlab-review": "allow",
      "gitlab-review *": "allow",
      "github-review *": "allow",
      "grep *": "allow",
      "head *": "allow",
      "jq *": "allow",
      "ls": "allow",
      "ls *": "allow",
      "make *": "allow",
      "mkdir *": "allow",
      "pre-commit *": "allow",
      "pwd": "allow",
      "python *": "allow",
      "python3 *": "allow",
      "readlink *": "allow",
      "realpath *": "allow",
      "sed *": "allow",
      "sort *": "allow",
      "tail *": "allow",
      "tee *": "allow",
      "touch *": "allow",
      "tr *": "allow",
      "uv run *": "allow",
      "uv sync *": "allow",
      "wc *": "allow",
      "which *": "allow",
      "work *": "allow",
      "bash /Agents/global/hooks/*": "allow",
      "*": "ask"
    }
  }
}
```

## 4. Authentication: log in on the host

The manifest's `credential_mounts` entry is the whole story — the same
mechanism the built-in harnesses use:

```bash
# On the host, once:
opencode auth login          # writes ~/.local/share/opencode/auth.json
```

At session launch, lmer bind-mounts that file into the container when it
exists (per-file, never the whole config dir; `rw` by default so
self-refreshing tokens write back through the mount — the host stays logged
in even when the refresh happens inside a session). No copy, no re-login in
the container. If the harness's login ever splits across multiple files,
add one `credential_mounts` entry per file (`"mode": "ro"` for hand-authored
config the harness only reads).

Alternative: skip `auth login` entirely and put a provider API key
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, …) in your `.env` — opencode reads
keys from the environment, and lmer forwards `.env` as usual.

## 5. Run it

```bash
# Explicit harness selection — required for opencode (see model_hints note):
lmer develop https://gitlab.example.com/group/project/-/issues/42 --harness opencode

# With a model (provider/model form), e.g. in ~/.lmer/.env:
#   LMER_HARNESS=opencode
#   LMER_LLM_NAME=anthropic/claude-sonnet-5
```

Launch output confirms the wiring:
`🤖 Harness: opencode [user-installed]` plus the definition path. The first
session pays the npm install into the cache volume
(`~/.lmer/harness-cache/opencode` on the host); later sessions start as fast
as built-ins. Force a reinstall by deleting that host directory.

What works out of the box (the core tier): repo clone + task instructions
(the supervisor types the generic start instruction and waits on the ready
marker), workspace `AGENTS.md` natively, user `~/.lmer/AGENTS.md` + human
identity via the global context file, gate/work tooling, lmer's slash
commands as opencode custom commands (`/start`, `/followup`, `/rgr`,
`/gate-commit`, … — work-repo commands override global ones), agent-memory
restore, and `spawn-harness` fan-out (both directions, subject to the
cross-harness child caveat in HARNESSES.md).

## 6. Transcripts in the chat view

Without this half, an opencode session runs fine and the orchestrator's chat
view answers *"this build cannot read it"* — mounting is not reading. Two
pieces close that, both drop-in-local:

1. **`"session_dir"` in the manifest** (above) — the platform creates one host
   directory per session, mounts it read-write at that path, scrubs the
   `.jsonl` files in it when the session ends, and reads them back for
   `GET /api/sessions/{id}/messages`.
2. **`converter.py` beside `runner.sh`** — the drop-in's own code, running in
   the container where opencode already runs, appending records in the
   documented [lmer transcript format](./TRANSCRIPT-FORMAT.md) to
   `~/.opencode-transcripts/<sessionID>.jsonl`. The daemon never executes it;
   it only reads what it writes.

opencode is the *decoupled* case, and the reason it is the worked example:
current releases persist sessions in a SQLite database (not per-session JSONL),
so nothing native ever appears in `session_dir` — the declared directory holds
only the converter's output. The converter never touches that database either.
Rather than track a schema that migrates without notice while a live writer
holds it, it polls opencode's own reader:

```bash
opencode session list --format json     # newest session for this directory
opencode export <sessionID>             # full message history, as JSON
```

Each export is a whole snapshot, so the converter appends only the message
*parts* it has not written yet — per part, because a turn keeps growing after
the poll that first saw it (another paragraph, a second tool call), and one
consumed whole would lose the rest. A turn can therefore arrive as more than
one record, across polls; the view concatenates them in file order. A tool call
that finished after its turn went out is resolved with a one-line
`lmer.tool_update` rather than a rewrite, so the file stays strictly
append-only, which is what a chat client polling with a cursor needs.

Text parts become message text; tool parts become tool chips (name, a one-line hint, and the
outcome); a part opencode marks `synthetic` — the context it injects in front
of the model — becomes `kind: "injected"`, from opencode's own flag rather than
any host-side guess.

What the chat view then shows: your prompts as operator turns, opencode's
replies as assistant turns with their tool calls and outcomes, injected context
behind the "internal" toggle instead of masquerading as something you typed,
and `opencode` as the transcript's source.

The converter is deliberately unremarkable code — a poll loop, a mapping, an
append — and it is fail-soft by construction: it is backgrounded, it never
signals the session, and if it dies the session is untouched (you get today's
behavior, an unreadable transcript, plus the complete terminal log). Read
[TRANSCRIPT-FORMAT.md](./TRANSCRIPT-FORMAT.md) for the record schemas and the
lifecycle rules, and copy
[`examples/harnesses/opencode/converter.py`](../examples/harnesses/opencode/converter.py)
as the starting point for your own harness — it is tested in CI against
captured `opencode export` fixtures, so what it maps is what opencode actually
writes.

## Troubleshooting

- **Session starts but never auto-types the start instruction** — the ready
  marker didn't match (TUI string drift after an opencode upgrade). Set
  `LMER_AUTO_START_READY_MARKER=` (empty disables marker gating, falls back
  to fixed delays) to confirm, then find the new stable string and update
  the manifest.
- **`opencode: command not found` mid-session** — the install step failed
  (network, npm registry). The runner exits 1 at start with the npm error;
  check the session log, then retry or pre-warm the cache by running the
  npm install into `~/.lmer/harness-cache/opencode` on the host.
- **Model errors at start** — remember `--model` wants `provider/model`.
  `opencode models` (host side) lists what your credentials can reach.
- **Rendering issues** — `--no-supervisor` execs the harness directly
  (debug aid; auto-start and the FastAPI endpoint are bypassed).
- **Chat view empty for a session that clearly ran** — the converter never
  started or never found the session. It logs to `converter.log` in the
  session directory (`~/.lmer-session/` in a platform-spawned session, `$HOME`
  otherwise), which lands beside the session's own files on the host. Run it by
  hand with `--once` inside the container to see the same errors on stderr.
