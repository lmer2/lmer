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
└── agent-files/
    └── opencode.json
```

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
