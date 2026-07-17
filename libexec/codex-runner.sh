#!/bin/bash
# Runner for the Codex CLI (OpenAI) harness — core feature tier.
# Invoked by clone_and_exec.py when LMER_HARNESS=codex (token "codex-runner").
# See docs/HARNESSES.md for the capability matrix and per-harness mapping.

export PATH="/home/developer/.npm-global/bin:$PATH"
# Container sessions have HOME set by the entrypoint; the fallback covers a
# bare env (and unit tests inject a scratch HOME).
export HOME="${HOME:-/home/developer}"
export LMER_HARNESS="${LMER_HARNESS:-codex}"

# shellcheck source=./harness-common.sh
source "$(dirname "$0")/harness-common.sh"
harness_init

# ── Credentials ──
# codex reads ~/.codex/auth.json (mounted from the host when present) or, for
# non-interactive runs, CODEX_API_KEY. Warn early so a missing login is
# diagnosable from the session banner instead of a mid-task auth error.
if [ -f "$HOME/.codex/auth.json" ]; then
    echo "✅ Codex credentials found at ~/.codex/auth.json"
elif [ -n "$CODEX_API_KEY" ]; then
    echo "✅ CODEX_API_KEY provided"
else
    echo "⚠️  No codex credentials (~/.codex/auth.json missing and CODEX_API_KEY unset)"
    echo "   Run 'codex login' on the host, or set CODEX_API_KEY in your .env"
fi

# ── Config + global context ──
harness_provision_config "codex/config.toml" "$HOME/.codex/config.toml"
harness_render_global_context "$HOME/.codex/AGENTS.md"

# ── Slash commands + agent memory ──
# lmer's claude command files render as codex custom prompts (invoked as
# /prompts:start, /prompts:followup, ... — deprecated upstream in favor of
# skills but functional); saved agent memory is restored before codex starts
# (usage contract delivered via the global context above).
harness_render_prompt_templates "$HOME/.codex/prompts"
harness_restore_memory

EXTRA_ARGS=""

# LMER_LLM_NAME → codex --model (verbatim; codex rejects unknown models itself)
if [ -n "$LMER_LLM_NAME" ]; then
    EXTRA_ARGS="$EXTRA_ARGS --model $LMER_LLM_NAME"
    echo "✅ Codex model: $LMER_LLM_NAME"
fi

# LMER_REASONING_EFFORT → codex model_reasoning_effort config override.
# codex accepts minimal|low|medium|high|xhigh; the shared tier semantics
# (max→xhigh, auto/unset→no override, unknown→warn+skip) live in
# harness_map_effort.
effort="$(harness_map_effort "Reasoning effort")"
if [ -n "$effort" ]; then
    EXTRA_ARGS="$EXTRA_ARGS -c model_reasoning_effort=$effort"
fi

# ── Sandbox / approvals ──
# The lmer container is the security boundary, and codex's own bwrap/seccomp
# sandbox cannot initialize under the container's no-new-privileges security
# opt — so codex always runs with its sandbox off in here (OpenAI's documented
# guidance for containerized use). What LMER_DANGER_ZONE controls is the
# approval prompts: off (default) keeps codex asking before risky actions,
# matching claude's permission-prompt posture; on bypasses approvals entirely.
if [ "$LMER_DANGER_ZONE" = "1" ]; then
    echo "⚠️  DANGER ZONE: bypassing codex approvals and sandbox!"
    EXTRA_ARGS="$EXTRA_ARGS --dangerously-bypass-approvals-and-sandbox"
else
    EXTRA_ARGS="$EXTRA_ARGS --sandbox danger-full-access --ask-for-approval on-request"
fi

# shellcheck disable=SC2086  # EXTRA_ARGS is intentionally word-split
harness_exec codex $EXTRA_ARGS "$@"
