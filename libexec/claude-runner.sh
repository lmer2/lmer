#!/bin/bash
# Internal script to run Claude with proper setup

export PATH="/home/developer/.npm-global/bin:$PATH"
export HOME="/home/developer"

# Mint a stable per-session id for run-state owner claims and event
# attribution (`work session-start` / `work session-end`). A host-injected
# LMER_SESSION_ID is preserved; otherwise UTC timestamp + pid + random is
# unique enough for one container session.
export LMER_SESSION_ID="${LMER_SESSION_ID:-$(date -u +%Y%m%d-%H%M%S)-$$-$RANDOM}"

# Check if we have credentials
if [ -f "$HOME/.claude/.credentials.json" ]; then
    echo "✅ Credentials found in .claude/"
else
    echo "❌ No credentials found at $HOME/.claude/.credentials.json"
fi

# Check for .claude.json
if [ -f "$HOME/.claude.json" ]; then
    echo "✅ Claude config found at ~/.claude.json"
else
    echo "⚠️  No .claude.json found"
fi

# Check statsig directory
if [ -d "$HOME/.claude/statsig" ]; then
    echo "✅ Statsig cache found"
else
    echo "⚠️  No statsig cache directory"
fi

# Source the agent-files helpers (claude_link_agent_files,
# claude_merge_work_settings). Tests source the helpers file directly.
# shellcheck source=./claude-agent-files.sh
source "$(dirname "$0")/claude-agent-files.sh"

# Path to the global lmer agent-files/claude tree, set by whichever
# discovery branch below succeeds. Consumed by the finalizer block that
# calls the helpers above.
CLAUDE_GLOBAL_AGENT_FILES=""

# ── Self-development mode detection ──
# When /workspace IS the lmer repository itself, skip --add-dir to avoid
# Claude seeing two copies of the same codebase (one at /workspace, one at
# /Agents/global). Claude will only see /workspace/AGENTS.md.
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
        echo "   Skipping --add-dir to avoid duplicate project directories"
        echo "   All development work should happen in /workspace/"
        echo "   /Agents/global/ is the operational runtime — do not modify"

        # Still create symlinks for commands and settings from the best available source
        # These are independent of --add-dir and needed for Claude Code functionality
        SYMLINK_SOURCE=""
        if [ -d "/home/developer/.lmer" ]; then
            SYMLINK_SOURCE="/home/developer/.lmer"
        elif [ -d "/Agents/global" ]; then
            SYMLINK_SOURCE="/Agents/global"
        fi

        if [ -n "$SYMLINK_SOURCE" ]; then
            CLAUDE_GLOBAL_AGENT_FILES="$SYMLINK_SOURCE/agent-files/claude"
            echo "✅ Global agent-files source: $SYMLINK_SOURCE"
        fi

        # Point the venv's editable install at /workspace/src first, with
        # /Agents/global/src as a fallback, so top-level packages absent from
        # /workspace (e.g. integrations/, which lives only on certain refs)
        # still resolve via /Agents/global/src instead of vanishing from
        # sys.path.
        for pth in /Agents/global/.venv/lib/python*/site-packages/__editable__.lmer*.pth; do
            [ -f "$pth" ] && printf '/workspace/src\n/Agents/global/src\n' > "$pth"
        done
        echo "✅ Editable install redirected to /workspace/src (with /Agents/global/src fallback)"

        # The .pth above is NOT sufficient on its own. The container entrypoint
        # exports PYTHONPATH=/Agents/global/src:…, and PYTHONPATH precedes
        # site-packages — so it beats the .pth and the operational tree wins
        # anyway (#198). Prepend the dev checkout, in the same order as the
        # .pth, so entry-point scripts and pytest share ONE view of
        # lmer_cli/work_repo/etc. rather than two that disagree when the trees
        # diverge. (An earlier fix set PYTHONPATH only, with no .pth — that is
        # what left the two views out of step; both halves together are what
        # make the dev checkout win consistently.)
        PYTHONPATH="/workspace/src${PYTHONPATH:+:$PYTHONPATH}"
        export PYTHONPATH

        # State the resolved path unconditionally. A developer should be able
        # to CHECK which tree this session imports, at startup, instead of
        # inferring it from an AttributeError several commands later — which is
        # how #198 was found. The suite asserts the same invariant
        # (tests/test_import_provenance.py); this line is what makes it visible
        # before any test runs.
        RESOLVED_LMER="$("${LMER_PYTHON:-python3}" -c 'import lmer_cli; print(lmer_cli.__file__)' 2>/dev/null)"
        if [ -z "$RESOLVED_LMER" ]; then
            echo "⚠️  lmer_cli is not importable — cannot confirm which tree this session tests"
        elif [ "${RESOLVED_LMER#/workspace/}" = "$RESOLVED_LMER" ]; then
            echo "⚠️  lmer_cli resolves to $RESOLVED_LMER — NOT the /workspace checkout"
            echo "   Tests and tooling here would exercise the operational runtime (#198)"
        else
            echo "✅ lmer_cli resolves to $RESOLVED_LMER"
        fi

        # No --add-dir: Claude only sees /workspace (the development checkout)
        EXTRA_ARGS=""
    fi
fi

# Check for global LMER installation first
LMER_GLOBAL_DIR="${LMER_GLOBAL_DIR:-/home/developer/.lmer}"
if [ "$LMER_SELF_DEV" = "1" ]; then
    # Self-dev path already handled above; skip the normal --add-dir chain
    true
elif [ -d "$LMER_GLOBAL_DIR" ]; then
    echo "✅ Global LMER installation found at $LMER_GLOBAL_DIR"
    EXTRA_ARGS="--add-dir $LMER_GLOBAL_DIR"
    CLAUDE_GLOBAL_AGENT_FILES="$LMER_GLOBAL_DIR/agent-files/claude"
elif [ -d "/Agents/global" ]; then
    echo "✅ Global rules mounted at /Agents/global"
    # Add --add-dir for global rules so Claude can access them
    EXTRA_ARGS="--add-dir /Agents/global"
    CLAUDE_GLOBAL_AGENT_FILES="/Agents/global/agent-files/claude"
elif [ -d "/workspace" ] && [ -f "/workspace/AGENTS.md" ]; then
    echo "✅ Global rules found at /workspace"
    EXTRA_ARGS="--add-dir /workspace"

    if [ -d "/Agents/global" ]; then
        EXTRA_ARGS="$EXTRA_ARGS --add-dir /Agents/global"
        CLAUDE_GLOBAL_AGENT_FILES="/Agents/global/agent-files/claude"
    fi

    # If no arguments provided (interactive mode), remind about rules
    if [ $# -eq 0 ]; then
        echo ""
        echo "🚨 Global rules loaded at /Agents/global/AGENTS.md"
        echo ""
        echo "📖 Quick commands:"
        echo "  📋 rgr        - Read all global rules"
        echo "  🔀 rgr-git    - Read git rules only"
        echo "  🧪 rgr-test   - Read testing rules only"
        echo "  ✨ rgr-code   - Read code quality rules only"
        echo ""
        echo "💡 Start with: rgr"
        echo ""
    fi
else
    echo "⚠️  Global rules not mounted, but ensuring access to /Agents/global if available"
    if [ -d "/Agents/global" ]; then
        EXTRA_ARGS="--add-dir /Agents/global"
        CLAUDE_GLOBAL_AGENT_FILES="/Agents/global/agent-files/claude"
    else
        EXTRA_ARGS=""
    fi
fi

# ── Lay out commands, skills, and settings under ~/.claude ──
# Symlink settings.json from the global tree first (if discovered above),
# then merge in the work-repo's permissions.allow. Then populate
# ~/.claude/commands/ and ~/.claude/skills/ with per-entry symlinks from
# the global tree and the work repo (work overrides on name collision).
WORK_AGENT_FILES="/work/agent-files/claude"
[ -d "$WORK_AGENT_FILES" ] || WORK_AGENT_FILES=""

if [ -n "$CLAUDE_GLOBAL_AGENT_FILES" ] \
   && [ -f "$CLAUDE_GLOBAL_AGENT_FILES/settings.json" ] \
   && [ ! -e "/home/developer/.claude/settings.json" ]; then
    ln -sf "$CLAUDE_GLOBAL_AGENT_FILES/settings.json" /home/developer/.claude/settings.json
    echo "✅ Global settings.json linked to Claude home"
fi

claude_link_agent_files "/home/developer/.claude" "$CLAUDE_GLOBAL_AGENT_FILES" "$WORK_AGENT_FILES"

if [ -n "$WORK_AGENT_FILES" ]; then
    claude_merge_work_settings "/home/developer/.claude" "$WORK_AGENT_FILES"
fi

# Merge personal MCP configuration if .mcp.local.json exists
# This allows users to add personal MCPs without modifying the base .mcp.json
# Place your file at ~/.lmer/.mcp.local.json or /home/developer/.mcp.local.json
MCP_FILE="/home/developer/.mcp.json"
MCP_LOCAL="/home/developer/.mcp.local.json"
if [ -f "$MCP_LOCAL" ] && [ -f "$MCP_FILE" ] && command -v jq >/dev/null 2>&1; then
    echo "🔧 Merging personal MCP servers from .mcp.local.json"
    MERGED=$(jq -s '.[0] * .[1] | .mcpServers = ((.[0].mcpServers // {}) * (.[1].mcpServers // {}))' "$MCP_FILE" "$MCP_LOCAL" 2>/dev/null)
    if [ $? -eq 0 ] && [ -n "$MERGED" ]; then
        echo "$MERGED" > "$MCP_FILE"
        echo "✅ Merged personal MCP servers into .mcp.json"
    else
        echo "⚠️  Failed to merge .mcp.local.json, using base MCP config"
    fi
elif [ -f "$MCP_LOCAL" ] && ! command -v jq >/dev/null 2>&1; then
    echo "⚠️  Found .mcp.local.json but jq not available for merging"
fi

# Merge personal permissions if settings.local.json exists
# This allows users to add personal permissions without modifying the base settings.json
# Place your file at ~/.lmer/.claude/settings.local.json
# LMER_SETTINGS_FILE overrides the path for non-standard layouts and tests
# (mirrors LMER_AGENT_MEMORY_DIR); defaults to the container's real location.
SETTINGS_FILE="${LMER_SETTINGS_FILE:-/home/developer/.claude/settings.json}"
SETTINGS_LOCAL="/home/developer/.claude/settings.local.json"
if [ -f "$SETTINGS_LOCAL" ] && [ -f "$SETTINGS_FILE" ] && command -v jq >/dev/null 2>&1; then
    echo "🔧 Merging personal permissions from settings.local.json"
    MERGED=$(jq -s '
      .[0] * .[1] |
      .permissions.allow = (((.[0].permissions.allow // []) + (.[1].permissions.allow // [])) | unique) |
      .permissions.deny = (((.[0].permissions.deny // []) + (.[1].permissions.deny // [])) | unique)
    ' "$SETTINGS_FILE" "$SETTINGS_LOCAL" 2>/dev/null)
    if [ $? -eq 0 ] && [ -n "$MERGED" ]; then
        # Settings may be a symlink to a read-only mount — replace with regular file
        [ -L "$SETTINGS_FILE" ] && rm "$SETTINGS_FILE"
        echo "$MERGED" > "$SETTINGS_FILE"
        echo "✅ Merged personal permissions into settings.json"
    else
        echo "⚠️  Failed to merge settings.local.json, using base settings"
    fi
elif [ -f "$SETTINGS_LOCAL" ] && ! command -v jq >/dev/null 2>&1; then
    echo "⚠️  Found settings.local.json but jq not available for merging"
fi

# Check for Danger Zone mode (skip permissions)
if [ "$LMER_DANGER_ZONE" = "1" ]; then
    echo "⚠️  DANGER ZONE: Skipping all permissions checks!"
    EXTRA_ARGS="$EXTRA_ARGS --allow-dangerously-skip-permissions"
fi

# Translate LMER_REASONING_EFFORT into claude's --effort flag.
# Valid claude values: low, medium, high, xhigh, max. Treat "auto" or unset
# as "let claude decide" — pass no flag. Invalid values get a warning + skip.
# Normalize to lowercase so HIGH/High/high all work. The vocabulary matches
# the per-lane dispatch efforts (LMER_DISPATCH_<LANE>) so the session and
# lane surfaces accept the same set.
if [ -n "$LMER_REASONING_EFFORT" ]; then
    effort_lower="${LMER_REASONING_EFFORT,,}"
    if [ "$effort_lower" != "auto" ]; then
        case "$effort_lower" in
            low|medium|high|xhigh|max)
                EXTRA_ARGS="$EXTRA_ARGS --effort $effort_lower"
                echo "✅ Reasoning effort: $effort_lower"
                ;;
            *)
                echo "⚠️  Ignoring LMER_REASONING_EFFORT='$LMER_REASONING_EFFORT' (expected: low|medium|high|xhigh|max|auto)"
                ;;
        esac
    fi
fi

# Translate LMER_LLM_NAME into claude's --model flag.
# The value is passed through verbatim (alias like "sonnet"/"opus" or a full
# model ID) — claude itself rejects unknown models, and hardcoding a list here
# would go stale as models change. Unset or empty means no flag: claude uses
# its own default model.
if [ -n "$LMER_LLM_NAME" ]; then
    EXTRA_ARGS="$EXTRA_ARGS --model $LMER_LLM_NAME"
    echo "✅ Claude model: $LMER_LLM_NAME"
fi

# ── AGENTS.md system prompt injection ──
# Since AGENTS.md is not auto-discovered by Claude Code (unlike CLAUDE.md),
# we inject it via --append-system-prompt-file. The chain is:
#   1. Workspace AGENTS.md (project or provisioned global config)
#   2. ~/.lmer/AGENTS.md (optional user-specific additions)
AGENTS_PROMPT_ARGS=""
AGENTS_COMBINED=""
rm -f /tmp/agents-prompt.*.md 2>/dev/null

# Find workspace AGENTS.md
WORKSPACE_AGENTS=""
if [ -f "/workspace/AGENTS.md" ]; then
    WORKSPACE_AGENTS="/workspace/AGENTS.md"
fi

# Find user AGENTS.md
USER_AGENTS=""
LMER_HOME="${LMER_GLOBAL_DIR:-/home/developer/.lmer}"
if [ -f "$LMER_HOME/AGENTS.md" ]; then
    USER_AGENTS="$LMER_HOME/AGENTS.md"
fi

if [ -n "$WORKSPACE_AGENTS" ] && [ -n "$USER_AGENTS" ]; then
    # Concatenate both into a temp file
    AGENTS_COMBINED=$(mktemp /tmp/agents-prompt.XXXXXX.md)
    cat "$WORKSPACE_AGENTS" "$USER_AGENTS" > "$AGENTS_COMBINED"
    AGENTS_PROMPT_ARGS="--append-system-prompt-file $AGENTS_COMBINED"
    echo "✅ AGENTS.md system prompt: workspace + user (~/.lmer/AGENTS.md)"
elif [ -n "$WORKSPACE_AGENTS" ]; then
    AGENTS_PROMPT_ARGS="--append-system-prompt-file $WORKSPACE_AGENTS"
    echo "✅ AGENTS.md system prompt: workspace"
elif [ -n "$USER_AGENTS" ]; then
    AGENTS_PROMPT_ARGS="--append-system-prompt-file $USER_AGENTS"
    echo "✅ AGENTS.md system prompt: user (~/.lmer/AGENTS.md)"
fi

# ── Human user identity injection ──
# When LMER_HUMAN_IDENTITY is set (explicitly via env or auto-derived from the
# host's git config by the lmer CLI), render the human-identity prompt
# fragment template and append it to the system prompt so the model can
# attribute matching usernames/emails in PRs, MRs, issues, and comments to the
# human user it is collaborating with. The fragment text lives in
# prompts/human-identity.md.jinja2 — not in this script — so it can be edited
# without touching shell code.
if [ -n "$(printf '%s' "$LMER_HUMAN_IDENTITY" | tr -d '[:space:]')" ]; then
    IDENTITY_TEMPLATE=""
    for candidate in \
        "$(dirname "$0")/../prompts/human-identity.md.jinja2" \
        "/workspace/prompts/human-identity.md.jinja2" \
        "$LMER_HOME/prompts/human-identity.md.jinja2" \
        "/Agents/global/prompts/human-identity.md.jinja2"; do
        if [ -f "$candidate" ]; then
            IDENTITY_TEMPLATE="$candidate"
            break
        fi
    done

    RENDERER=""
    for candidate in \
        "$(dirname "$0")/render-prompt-fragment.py" \
        "/workspace/libexec/render-prompt-fragment.py" \
        "$LMER_HOME/libexec/render-prompt-fragment.py" \
        "/Agents/global/libexec/render-prompt-fragment.py"; do
        if [ -f "$candidate" ]; then
            RENDERER="$candidate"
            break
        fi
    done

    if [ -n "$IDENTITY_TEMPLATE" ] && [ -n "$RENDERER" ]; then
        if [ -z "$AGENTS_COMBINED" ]; then
            AGENTS_COMBINED=$(mktemp /tmp/agents-prompt.XXXXXX.md)
            if [ -n "$WORKSPACE_AGENTS" ]; then
                cat "$WORKSPACE_AGENTS" > "$AGENTS_COMBINED"
            elif [ -n "$USER_AGENTS" ]; then
                cat "$USER_AGENTS" > "$AGENTS_COMBINED"
            fi
        fi
        printf '\n\n' >> "$AGENTS_COMBINED"
        if "${LMER_PYTHON:-python3}" "$RENDERER" "$IDENTITY_TEMPLATE" >> "$AGENTS_COMBINED"; then
            AGENTS_PROMPT_ARGS="--append-system-prompt-file $AGENTS_COMBINED"
            echo "✅ Human identity injected into system prompt"
        else
            echo "⚠️  Failed to render human identity template at $IDENTITY_TEMPLATE"
        fi
    else
        if [ -z "$IDENTITY_TEMPLATE" ]; then
            echo "⚠️  LMER_HUMAN_IDENTITY set but human-identity.md.jinja2 not found"
        fi
        if [ -z "$RENDERER" ]; then
            echo "⚠️  LMER_HUMAN_IDENTITY set but render-prompt-fragment.py not found"
        fi
    fi
fi

# ── Operator ask channel (orchestrated sessions) ──
# When the lmer orchestrator started this session it mounts an ask channel and
# sets LMER_ASK_DIR to its container path (issue #141). Render the fragment that
# tells the model to use `lmer-ask` instead of asking into a terminal nobody is
# watching. Gated on the env var, so an ordinary session is told nothing.
#
# The template/renderer search and the append are factored into a function here
# rather than copied from the human-identity block above: that block is left
# byte-for-byte intact (its stability is this script's compatibility contract),
# and a second inline copy of the same 40 lines is how they drift apart.
append_prompt_fragment() {
    local rel="$1" label="$2" template="" renderer="" candidate

    for candidate in \
        "$(dirname "$0")/../prompts/$rel" \
        "/workspace/prompts/$rel" \
        "$LMER_HOME/prompts/$rel" \
        "/Agents/global/prompts/$rel"; do
        if [ -f "$candidate" ]; then
            template="$candidate"
            break
        fi
    done

    for candidate in \
        "$(dirname "$0")/render-prompt-fragment.py" \
        "/workspace/libexec/render-prompt-fragment.py" \
        "$LMER_HOME/libexec/render-prompt-fragment.py" \
        "/Agents/global/libexec/render-prompt-fragment.py"; do
        if [ -f "$candidate" ]; then
            renderer="$candidate"
            break
        fi
    done

    if [ -z "$template" ] || [ -z "$renderer" ]; then
        echo "⚠️  $label requested but its template/renderer was not found"
        return 1
    fi

    if [ -z "$AGENTS_COMBINED" ]; then
        AGENTS_COMBINED=$(mktemp /tmp/agents-prompt.XXXXXX.md)
        if [ -n "$WORKSPACE_AGENTS" ]; then
            cat "$WORKSPACE_AGENTS" > "$AGENTS_COMBINED"
        elif [ -n "$USER_AGENTS" ]; then
            cat "$USER_AGENTS" > "$AGENTS_COMBINED"
        fi
    fi
    printf '\n\n' >> "$AGENTS_COMBINED"
    if "${LMER_PYTHON:-python3}" "$renderer" "$template" >> "$AGENTS_COMBINED"; then
        AGENTS_PROMPT_ARGS="--append-system-prompt-file $AGENTS_COMBINED"
        echo "✅ $label injected into system prompt"
        return 0
    fi
    echo "⚠️  Failed to render $label template at $template"
    return 1
}

if [ -n "$(printf '%s' "$LMER_ASK_DIR" | tr -d '[:space:]')" ]; then
    append_prompt_fragment "orchestrator-ask.md.jinja2" "Operator ask channel" || true
fi

# ── Non-interactive session notice ──
# Claude Code discovers only CLAUDE.md natively, so AGENTS.md — and with it the
# NON-INTERACTIVE SESSIONS rule — reaches the model solely through the system
# prompt assembled above. Setting LMER_NONINTERACTIVE in the environment tells
# no agent anything on its own (no path renders LMER_* values into a session's
# context), so the rule text itself is appended here for headless launches.
# Plain markdown, no renderer needed — the fragment carries no session values.
case "${LMER_NONINTERACTIVE,,}" in
    1|true|yes)
        NONINTERACTIVE_FRAGMENT=""
        for candidate in \
            "$(dirname "$0")/../prompts/non-interactive.md" \
            "/workspace/prompts/non-interactive.md" \
            "$LMER_HOME/prompts/non-interactive.md" \
            "/Agents/global/prompts/non-interactive.md"; do
            if [ -f "$candidate" ]; then
                NONINTERACTIVE_FRAGMENT="$candidate"
                break
            fi
        done

        if [ -n "$NONINTERACTIVE_FRAGMENT" ]; then
            if [ -z "$AGENTS_COMBINED" ]; then
                AGENTS_COMBINED=$(mktemp /tmp/agents-prompt.XXXXXX.md)
                if [ -n "$WORKSPACE_AGENTS" ]; then
                    cat "$WORKSPACE_AGENTS" > "$AGENTS_COMBINED"
                elif [ -n "$USER_AGENTS" ]; then
                    cat "$USER_AGENTS" > "$AGENTS_COMBINED"
                fi
            fi
            printf '\n\n' >> "$AGENTS_COMBINED"
            cat "$NONINTERACTIVE_FRAGMENT" >> "$AGENTS_COMBINED"
            AGENTS_PROMPT_ARGS="--append-system-prompt-file $AGENTS_COMBINED"
            echo "✅ Non-interactive session notice injected into system prompt"
        else
            echo "⚠️  LMER_NONINTERACTIVE set but non-interactive.md not found"
        fi
        ;;
esac

# ── Agent memory restore ──
# When LMER_PERSIST_AGENT_MEMORY is enabled, restore previously-saved
# per-project agent memory from the work repo into Claude's memory directory
# before claude starts, so the saved memory is on disk by the time the session
# loads. Persisting memory back to the work repo is the agent's responsibility
# via `work memory persist` (see AGENTS.md) — we only automate the restore here.
# `work memory restore` self-guards on LMER_PERSIST_AGENT_MEMORY, but we gate the
# call in the shell too so disabled sessions stay quiet and skip it when the
# `work` CLI isn't on PATH (e.g. the claude-runner unit tests).
case "${LMER_PERSIST_AGENT_MEMORY,,}" in
    1|true|yes)
        if command -v work >/dev/null 2>&1; then
            work memory restore || echo "⚠️  Agent memory restore failed (continuing)"
        fi
        ;;
esac

# ── Masterplan plugin provisioning ──
# Delegated to masterplan-enable.sh — the single owner of the provisioning
# steps, shared with mid-session on-demand enablement (its default, forced
# mode). Gated mode keeps the launch contract: the session provisions only
# when LMER_TASK=masterplan, a truthy LMER_MASTERPLAN, or a taskdef's
# task.yaml declares `masterplan: true`. Script exit codes: 1 = not a
# masterplan session (silent skip); 2 = enabled but the run dir is
# indeterminate (the script already warned on stderr); 0 = provisioned,
# bundle root on stdout — captured and exported so masterplan's tooling
# nests bundles inside the lmer run dir. Mirror resolution, settings.json
# materialization, and idempotence/non-fatality notes live in the script.
# [ -x ] guard: on the legacy baked-copy path (claude-runner.sh alone at
# /home/developer/, no sibling libexec/) the script is absent — keep the
# non-fatality contract with a warning instead of a raw bash error.
if [ -x "$(dirname "$0")/masterplan-enable.sh" ]; then
    if MASTERPLAN_RUNS_DIR="$("$(dirname "$0")/masterplan-enable.sh" --gated)"; then
        export MASTERPLAN_RUNS_DIR
    fi
else
    echo "⚠️  masterplan: masterplan-enable.sh not found beside claude-runner.sh; skipping provisioning" >&2
fi

# Run Claude through the lmer supervisor when available.
#
# The supervisor wraps claude under a PTY so a controlling Python process
# sits between the user and the model. It enables:
#   - LMER_FASTAPI=1   -> a FastAPI endpoint to read/write the process
#   - auto-injecting `/start` so a task begins immediately (disable with
#     LMER_MANUAL_START=1)
#
# Falls back to direct `exec claude` if `lmer-supervisor` is not on PATH
# (e.g. older containers built before this feature shipped) or if the
# user opts out with LMER_DISABLE_SUPERVISOR=1 (escape hatch for
# bisecting rendering issues attributable to the PTY wrapper).
if [ "${LMER_DISABLE_SUPERVISOR:-0}" != "1" ] && command -v lmer-supervisor >/dev/null 2>&1; then
    exec lmer-supervisor -- claude $EXTRA_ARGS $AGENTS_PROMPT_ARGS "$@"
fi
exec claude $EXTRA_ARGS $AGENTS_PROMPT_ARGS "$@"
