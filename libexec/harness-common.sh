#!/bin/bash
# Shared setup helpers for harness runner scripts (codex-runner.sh,
# pi-runner.sh, ...). Source this file, then call
# harness_init early; the other helpers are opt-in per runner.
#
# claude-runner.sh predates this file and keeps its own (behavior-identical)
# inline copies of these steps — its byte-for-byte stability is the
# backward-compatibility contract for existing installs. New runners must use
# these helpers instead of copying them again. See docs/HARNESSES.md.

# ── Source-time constants ──
# Everything evaluated when this file is sourced lives here, above all
# function definitions, so no source-time code can ever read one unset.

# Marker distinguishing lmer-managed global context files (regenerated each
# session by harness_render_global_context) from user-authored ones, which
# are left untouched.
HARNESS_MANAGED_MARKER="<!-- lmer-managed context: regenerated each session -->"

# Root of the work repo's agent-files tree, for harness_provision_config
# overrides ("" when the work repo carries none). LMER_WORK_AGENT_FILES_ROOT
# overrides the path for non-standard layouts and tests (mirrors
# LMER_SETTINGS_FILE in claude-runner.sh).
WORK_AGENT_FILES_ROOT="${LMER_WORK_AGENT_FILES_ROOT:-/work/agent-files}"
[ -d "$WORK_AGENT_FILES_ROOT" ] || WORK_AGENT_FILES_ROOT=""

# ── Session id ──
# Mint a stable per-session id for run-state owner claims and event
# attribution (`work session-start` / `work session-end`). A host-injected
# LMER_SESSION_ID is preserved.
harness_session_id() {
    export LMER_SESSION_ID="${LMER_SESSION_ID:-$(date -u +%Y%m%d-%H%M%S)-$$-$RANDOM}"
}

# ── Self-development mode ──
# When /workspace IS the lmer repository, export LMER_SELF_DEV=1 and point the
# venv's editable install at /workspace/src first (with /Agents/global/src as
# fallback) so the whole runtime — entry-point scripts AND pytest — resolves
# lmer_cli/work_repo/etc. from the dev checkout. Mirrors claude-runner.sh.
harness_detect_self_dev() {
    LMER_SELF_DEV=0
    if [ -f "/workspace/pyproject.toml" ]; then
        if "${LMER_PYTHON:-python3}" -c "
import tomllib
with open('/workspace/pyproject.toml', 'rb') as f:
    data = tomllib.load(f)
exit(0 if data.get('project', {}).get('name') in ('lmer', 'lmer-cli') else 1)
" 2>/dev/null; then
            LMER_SELF_DEV=1
            export LMER_SELF_DEV
            echo "🔧 Self-development mode: /workspace is the lmer repository"
            for pth in /Agents/global/.venv/lib/python*/site-packages/__editable__.lmer*.pth; do
                [ -f "$pth" ] && printf '/workspace/src\n/Agents/global/src\n' > "$pth"
            done
        fi
    fi
}

# ── Global lmer tree discovery ──
# Sets HARNESS_GLOBAL_DIR to the best available global lmer installation
# (user-level ~/.lmer wins over the mounted/baked /Agents/global). Empty when
# neither exists (bare test environments).
harness_find_global_dir() {
    HARNESS_GLOBAL_DIR=""
    local lmer_home="${LMER_GLOBAL_DIR:-/home/developer/.lmer}"
    if [ -d "$lmer_home" ]; then
        HARNESS_GLOBAL_DIR="$lmer_home"
    elif [ -d "/Agents/global" ]; then
        HARNESS_GLOBAL_DIR="/Agents/global"
    fi
}

# One-call convenience: session id + self-dev detection + global dir.
harness_init() {
    harness_session_id
    harness_detect_self_dev
    harness_find_global_dir
}

# ── Repo resource discovery ──
# Echo the first existing candidate for a repo-relative resource (a prompts/
# fragment, a libexec/ helper, ...), searched in: this file's tree, the
# /workspace checkout, the user-level lmer home, the baked /Agents/global.
# Prints nothing and returns 1 when none exists.
#   harness_find_resource <repo-relative-path>
harness_find_resource() {
    local rel="$1" lmer_home="${LMER_GLOBAL_DIR:-/home/developer/.lmer}" candidate
    for candidate in \
        "$(dirname "${BASH_SOURCE[0]}")/../$rel" \
        "/workspace/$rel" \
        "$lmer_home/$rel" \
        "/Agents/global/$rel"; do
        if [ -f "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

# ── Global context file (user AGENTS.md additions + human identity) ──
# All core-tier harnesses read the *workspace* AGENTS.md natively, so unlike
# claude (which needs --append-system-prompt-file) only two lmer extras need
# delivering: the optional user-level ~/.lmer/AGENTS.md additions and the
# rendered human-identity fragment (LMER_HUMAN_IDENTITY). This helper writes
# them to the harness's *global* context file location (e.g. ~/.codex/AGENTS.md),
# which every supported harness also loads automatically.
#
# The file is marked lmer-managed (HARNESS_MANAGED_MARKER, defined with the
# source-time constants at the top of this file) and regenerated each
# session; a file at the target path without the marker is user-authored and
# is left untouched.
harness_render_global_context() {
    local target="$1"
    [ -n "$target" ] || return 0

    if [ -f "$target" ] && ! grep -qF "$HARNESS_MANAGED_MARKER" "$target"; then
        echo "ℹ️  Keeping existing $target (not lmer-managed)"
        return 0
    fi

    local lmer_home="${LMER_GLOBAL_DIR:-/home/developer/.lmer}"
    local tmp
    tmp=$(mktemp /tmp/harness-context.XXXXXX.md)
    printf '%s\n' "$HARNESS_MANAGED_MARKER" > "$tmp"
    local have_content=0

    if [ -f "$lmer_home/AGENTS.md" ]; then
        cat "$lmer_home/AGENTS.md" >> "$tmp"
        printf '\n' >> "$tmp"
        have_content=1
        echo "✅ User AGENTS.md (~/.lmer/AGENTS.md) added to global context"
    fi

    if [ -n "$(printf '%s' "$LMER_HUMAN_IDENTITY" | tr -d '[:space:]')" ]; then
        local template renderer
        template="$(harness_find_resource "prompts/human-identity.md.jinja2")"
        renderer="$(harness_find_resource "libexec/render-prompt-fragment.py")"
        if [ -n "$template" ] && [ -n "$renderer" ]; then
            if "${LMER_PYTHON:-python3}" "$renderer" "$template" >> "$tmp"; then
                have_content=1
                echo "✅ Human identity added to global context"
            else
                echo "⚠️  Failed to render human identity template at $template"
            fi
        else
            echo "⚠️  LMER_HUMAN_IDENTITY set but identity template/renderer not found"
        fi
    fi

    # Agent memory usage instructions — the memory store is harness-neutral,
    # but the non-claude harnesses have no built-in memory feature, so the
    # read/write/persist contract is delivered as context. Only rendered when
    # memory persistence is enabled: otherwise restore never ran and
    # `work memory persist` is a no-op, so the instructions would mislead.
    case "${LMER_PERSIST_AGENT_MEMORY,,}" in
        1|true|yes)
            local memory_fragment
            memory_fragment="$(harness_find_resource "prompts/agent-memory.md")"
            if [ -n "$memory_fragment" ]; then
                printf '\n' >> "$tmp"
                cat "$memory_fragment" >> "$tmp"
                have_content=1
                echo "✅ Agent memory instructions added to global context"
            else
                echo "⚠️  LMER_PERSIST_AGENT_MEMORY set but agent-memory fragment not found"
            fi
            ;;
    esac

    if [ "$have_content" = "1" ]; then
        if mkdir -p "$(dirname "$target")" && mv "$tmp" "$target"; then
            echo "✅ Global context written to $target"
        else
            rm -f "$tmp"
            echo "⚠️  Failed to write global context to $target (continuing)"
        fi
    else
        rm -f "$tmp"
        # Remove a stale lmer-managed file so old identity text can't linger.
        if [ -f "$target" ] && grep -qF "$HARNESS_MANAGED_MARKER" "$target"; then
            rm -f "$target"
        fi
    fi
}

# ── Harness config provisioning ──
# Copy a config file from the global agent-files tree (work repo overrides
# global on collision) to the harness's expected location, unless the target
# already exists — an existing file is user/session state and wins.
#   harness_provision_config <relative-path-under-agent-files> <target-path>
harness_provision_config() {
    local rel="$1" target="$2" source=""
    if [ -n "$WORK_AGENT_FILES_ROOT" ] && [ -f "$WORK_AGENT_FILES_ROOT/$rel" ]; then
        source="$WORK_AGENT_FILES_ROOT/$rel"
    elif [ -n "$HARNESS_GLOBAL_DIR" ] && [ -f "$HARNESS_GLOBAL_DIR/agent-files/$rel" ]; then
        source="$HARNESS_GLOBAL_DIR/agent-files/$rel"
    fi
    if [ -n "$source" ] && [ ! -e "$target" ]; then
        if mkdir -p "$(dirname "$target")" && cp "$source" "$target"; then
            echo "✅ Provisioned $target (from $source)"
        else
            echo "⚠️  Failed to provision $target from $source (continuing)"
        fi
    fi
}

# ── Slash commands as prompt templates ──
# codex and pi load markdown *prompt templates* from a per-user directory as
# slash commands (pi: /name from ~/.pi/agent/prompts/; codex: /prompts:name
# from ~/.codex/prompts/). Render lmer's claude command files
# (agent-files/claude/commands/*.md) into that directory so /start,
# /followup, the gate commands etc. exist on every harness. Work-repo
# commands override global ones of the same name — the same precedence as
# claude_link_agent_files. Conversion lives in
# lmer_cli.container.prompt_templates.
#   harness_render_prompt_templates <target-dir>
harness_render_prompt_templates() {
    local target="$1"
    [ -n "$target" ] || return 0
    local sources=()
    if [ -n "$HARNESS_GLOBAL_DIR" ] && [ -d "$HARNESS_GLOBAL_DIR/agent-files/claude/commands" ]; then
        sources+=("$HARNESS_GLOBAL_DIR/agent-files/claude/commands")
    fi
    if [ -n "$WORK_AGENT_FILES_ROOT" ] && [ -d "$WORK_AGENT_FILES_ROOT/claude/commands" ]; then
        sources+=("$WORK_AGENT_FILES_ROOT/claude/commands")
    fi
    [ "${#sources[@]}" -gt 0 ] || return 0
    if ! "${LMER_PYTHON:-python3}" -m lmer_cli.container.prompt_templates \
        "$target" "${sources[@]}"; then
        echo "⚠️  Prompt-template render failed (continuing without slash commands)"
    fi
}

# ── Agent memory restore ──
# When LMER_PERSIST_AGENT_MEMORY is enabled, restore previously-saved
# per-project agent memory from the work repo before the harness starts, so
# the saved memory is on disk by the time the session reads it. Persisting
# back is the agent's responsibility via `work memory persist` — the usage
# contract is delivered by the agent-memory fragment in
# harness_render_global_context. Mirrors the equivalent block in
# claude-runner.sh (which keeps its own inline copy per its
# backward-compatibility contract).
harness_restore_memory() {
    case "${LMER_PERSIST_AGENT_MEMORY,,}" in
        1|true|yes)
            if command -v work >/dev/null 2>&1; then
                work memory restore || echo "⚠️  Agent memory restore failed (continuing)"
            fi
            ;;
    esac
}

# ── LMER_REASONING_EFFORT tier mapping ──
# Shared semantics for mapping lmer's effort setting onto a harness flag
# (docs/HARNESSES.md codifies these for every runner): case-insensitive;
# "auto"/unset yield nothing (no flag); "max" maps to xhigh (each current
# harness's top tier); low|medium|high|xhigh pass through; anything else warns
# and yields nothing. Echoes the normalized tier on stdout — call as
#   effort="$(harness_map_effort "Reasoning effort")"
# and format the harness-specific flag from it when non-empty. The label
# names the harness's own concept in the ✅ line (e.g. pi: "Thinking level");
# messages go to stderr so the command substitution captures only the tier.
harness_map_effort() {
    local label="${1:-Reasoning effort}"
    local effort_lower="${LMER_REASONING_EFFORT,,}"
    case "$effort_lower" in
        ""|auto) ;;
        max)
            echo "✅ $label: xhigh (mapped from max)" >&2
            echo "xhigh"
            ;;
        low|medium|high|xhigh)
            echo "✅ $label: $effort_lower" >&2
            echo "$effort_lower"
            ;;
        *)
            echo "⚠️  Ignoring LMER_REASONING_EFFORT='$LMER_REASONING_EFFORT' (expected: low|medium|high|xhigh|max|auto)" >&2
            ;;
    esac
}

# ── Final exec through the supervisor ──
# Run the harness through lmer-supervisor when available (PTY wrapper: FastAPI
# endpoint, auto start-command injection — profile selected via LMER_HARNESS),
# falling back to a direct exec. Mirrors the tail of claude-runner.sh.
#   harness_exec <binary> [args...]
harness_exec() {
    if [ "${LMER_DISABLE_SUPERVISOR:-0}" != "1" ] && command -v lmer-supervisor >/dev/null 2>&1; then
        exec lmer-supervisor -- "$@"
    fi
    exec "$@"
}
