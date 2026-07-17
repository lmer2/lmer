#!/bin/bash
# Runner for the pi harness (github.com/earendil-works/pi, formerly
# badlogic/pi-mono) — core feature tier. Invoked by clone_and_exec.py when
# LMER_HARNESS=pi (token "pi-runner"). See docs/HARNESSES.md.

export PATH="/home/developer/.npm-global/bin:$PATH"
# Container sessions have HOME set by the entrypoint; the fallback covers a
# bare env (and unit tests inject a scratch HOME).
export HOME="${HOME:-/home/developer}"
export LMER_HARNESS="${LMER_HARNESS:-pi}"
# Session containers are ephemeral — skip pi's startup version check.
export PI_SKIP_VERSION_CHECK="${PI_SKIP_VERSION_CHECK:-1}"

# shellcheck source=./harness-common.sh
source "$(dirname "$0")/harness-common.sh"
harness_init

# ── Credentials ──
# pi reads provider API keys from the environment (ANTHROPIC_API_KEY,
# OPENAI_API_KEY, GEMINI_API_KEY, ...) and stored credentials from
# ~/.pi/agent/auth.json (mounted from the host when present; populated by
# pi's in-TUI /login).
if [ -f "$HOME/.pi/agent/auth.json" ]; then
    echo "✅ pi credentials found at ~/.pi/agent/auth.json"
elif [ -n "$ANTHROPIC_API_KEY" ] || [ -n "$OPENAI_API_KEY" ] || [ -n "$GEMINI_API_KEY" ]; then
    echo "✅ Provider API key present in environment"
else
    echo "⚠️  No pi credentials (~/.pi/agent/auth.json missing, no provider API key in env)"
    echo "   Use pi's /login on the host, or set a provider key in your .env"
fi

# ~/.pi/agent/models.json (mounted from the host when present) registers
# custom providers/models — e.g. a local llama.cpp server. Without it, pi
# only knows its built-in model catalog.
if [ -f "$HOME/.pi/agent/models.json" ]; then
    echo "✅ pi custom model registry found at ~/.pi/agent/models.json"
fi

# ── Config + global context ──
harness_provision_config "pi/settings.json" "$HOME/.pi/agent/settings.json"
harness_render_global_context "$HOME/.pi/agent/AGENTS.md"

# ── Slash commands + agent memory ──
# lmer's claude command files render as pi prompt templates (invoked as
# /start, /followup, ... with autocomplete); saved agent memory is restored
# before pi starts (usage contract delivered via the global context above).
harness_render_prompt_templates "$HOME/.pi/agent/prompts"
harness_restore_memory

EXTRA_ARGS=""

# LMER_LLM_NAME → pi --model (accepts a pattern, an id, or provider/id, with
# an optional :thinking suffix; passed verbatim, pi validates)
if [ -n "$LMER_LLM_NAME" ]; then
    EXTRA_ARGS="$EXTRA_ARGS --model $LMER_LLM_NAME"
    echo "✅ pi model: $LMER_LLM_NAME"
fi

# LMER_REASONING_EFFORT → pi --thinking (off|minimal|low|medium|high|xhigh);
# the shared tier semantics (max→xhigh, auto/unset→no flag, unknown→
# warn+skip) live in harness_map_effort.
effort="$(harness_map_effort "Thinking level")"
if [ -n "$effort" ]; then
    EXTRA_ARGS="$EXTRA_ARGS --thinking $effort"
fi

# ── Safety posture ──
# pi has NO permission prompts by design — every tool call runs unprompted
# with the pi process's privileges; the lmer container is the security
# boundary. What LMER_DANGER_ZONE maps to here is pi's *project trust*: by
# default the target repo's own .pi/ resources (settings, extensions, skills —
# arbitrary code) are NOT loaded, keeping startup non-interactive and the
# session limited to lmer-provisioned config. Danger zone opts into loading
# them.
echo "ℹ️  pi runs tools without permission prompts (container is the boundary)"
if [ "$LMER_DANGER_ZONE" = "1" ]; then
    echo "⚠️  DANGER ZONE: trusting the target repo's own .pi/ resources!"
    EXTRA_ARGS="$EXTRA_ARGS --approve"
else
    EXTRA_ARGS="$EXTRA_ARGS --no-approve"
fi

# shellcheck disable=SC2086  # EXTRA_ARGS is intentionally word-split
harness_exec pi $EXTRA_ARGS "$@"
