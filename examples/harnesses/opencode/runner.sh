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
