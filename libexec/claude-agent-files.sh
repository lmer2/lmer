#!/bin/bash
# Helpers for laying out commands/skills/agents/output-styles/settings under
# ~/.claude.
#
# Both the lmer global tree (typically /Agents/global/agent-files/claude)
# and — when present — the work repository (/work/agent-files/claude) can
# contribute slash commands, skills, subagent definitions, output styles, and
# permission grants. Earlier versions of claude-runner.sh symlinked
# ~/.claude/commands as a single directory pointing at the global tree, which
# made it impossible to overlay work-repo contributions. The helpers below
# replace that with per-entry symlinks so multiple sources can coexist, with
# the work repository taking precedence on name collisions.
#
# This file is sourced by claude-runner.sh and by tests.

# claude_link_agent_files <home_claude> <global_src> <work_src>
#   Populate <home_claude>/commands/, skills/, agents/, and output-styles/
#   with symlinks from each source's corresponding subdirectory.
#   <global_src> and <work_src> may each be empty. <home_claude> is taken as
#   a parameter so tests can target a temporary directory.
#
#   output-styles/ is a delivery mechanism only (Issue #249): a style file here
#   is inert until the outputStyle settings key selects it, which a work-repo
#   settings.json cannot do (claude_merge_work_settings takes only
#   permissions.allow, per Issue #48 — asserted by
#   tests/test_claude_agent_files.py::test_does_not_merge_work_repo_output_style,
#   tracked as Issue #252).
#   docs/LMER-CLI.md "Output styles: shipping one and selecting one are
#   separate" is the full account; keep it the only one.
claude_link_agent_files() {
    local home_claude="$1"
    local global_src="$2"
    local work_src="$3"
    local subdir target item linked_global linked_work

    for subdir in commands skills agents output-styles; do
        target="$home_claude/$subdir"
        # An older runner may have left ~/.claude/$subdir as a directory
        # symlink pointing at the global tree. Replace it with a real
        # directory so we can layer entries from multiple sources.
        if [ -L "$target" ]; then
            rm "$target"
        fi
        mkdir -p "$target"

        # Link the work-repo entries first so the post-override counter for
        # the global pass below reflects the effective layout (a global entry
        # shadowed by a work-repo override of the same name is not counted).
        linked_work=0
        if [ -n "$work_src" ] && [ -d "$work_src/$subdir" ]; then
            for item in "$work_src/$subdir"/*; do
                [ -e "$item" ] || continue
                ln -sfn "$item" "$target/$(basename "$item")"
                linked_work=$((linked_work + 1))
            done
        fi

        linked_global=0
        if [ -n "$global_src" ] && [ -d "$global_src/$subdir" ]; then
            for item in "$global_src/$subdir"/*; do
                [ -e "$item" ] || continue
                name=$(basename "$item")
                if [ -L "$target/$name" ] || [ -e "$target/$name" ]; then
                    continue
                fi
                ln -sfn "$item" "$target/$name"
                linked_global=$((linked_global + 1))
            done
        fi

        if [ "$linked_global" -gt 0 ]; then
            echo "✅ Linked $linked_global global $subdir into $target"
        fi
        if [ "$linked_work" -gt 0 ]; then
            echo "✅ Linked $linked_work work-repo $subdir into $target"
        fi
    done

    claude_render_dispatch_lanes "$home_claude" "$global_src" "$work_src"
    return 0
}

# claude_render_dispatch_lanes <home_claude> <global_src> <work_src>
#   Post-link render pass for the five dispatch lanes (LMER_DISPATCH_<LANE>,
#   value model[:effort]). A configured lane's agent symlink is replaced by
#   a real file whose frontmatter carries the configured model/effort; an
#   unset lane is forced back to the bare symlink so a stale materialized
#   copy never outlives its configuration. Parsing and rendering live in
#   lmer_cli.container.dispatch_agents (the python side owns the lane→agent
#   map; the stem list below is only the cheap skip-gate and must match it).
#   Fail-soft: a render problem warns and leaves the linked defs standing.
claude_render_dispatch_lanes() {
    local home_claude="$1"
    local global_src="$2"
    local work_src="$3"
    local agents_dir="$home_claude/agents"

    [ -d "$agents_dir" ] || return 0

    # Skip the python spawn when nothing is configured AND no lane file
    # needs repair — a stale materialized real file from a previously
    # configured lane, or a dangling symlink whose source went away (both
    # sides of the invariant: correct layout regardless of pre-existing
    # state).
    local need_render=0 stem
    if [ -n "${LMER_DISPATCH_REVIEW}${LMER_DISPATCH_DESIGN}${LMER_DISPATCH_CODE}${LMER_DISPATCH_MECHANICAL}${LMER_DISPATCH_EXPLORE}" ]; then
        need_render=1
    else
        for stem in adversarial-reviewer designer coder mechanical explorer; do
            if [ -e "$agents_dir/$stem.md" ] && [ ! -L "$agents_dir/$stem.md" ]; then
                need_render=1
                break
            fi
            if [ -L "$agents_dir/$stem.md" ] && [ ! -e "$agents_dir/$stem.md" ]; then
                need_render=1
                break
            fi
        done
    fi
    [ "$need_render" = "1" ] || return 0

    local global_agents="" work_agents=""
    [ -n "$global_src" ] && global_agents="$global_src/agents"
    [ -n "$work_src" ] && work_agents="$work_src/agents"
    if ! "${LMER_PYTHON:-python3}" -m lmer_cli.container.dispatch_agents \
        "$agents_dir" --global-src "$global_agents" --work-src "$work_agents"; then
        echo "⚠️  Dispatch lane render failed (lmer_cli.container.dispatch_agents) — agent defs left as linked"
    fi
    return 0
}

# claude_merge_work_settings <home_claude> <work_src>
#   Merge permissions.allow from <work_src>/settings.json into
#   <home_claude>/settings.json. Only permissions.allow is taken from the
#   work-repo file (per Issue #48 — limited merge to avoid the work repo
#   silently disabling protections that live in the global settings).
claude_merge_work_settings() {
    local home_claude="$1"
    local work_src="$2"
    local settings_file="$home_claude/settings.json"
    local work_settings="$work_src/settings.json"

    [ -f "$work_settings" ] || return 0
    if [ ! -f "$settings_file" ]; then
        # No base settings.json to merge into (none of the global discovery
        # branches in claude-runner.sh linked one). Surface this so a
        # work-repo maintainer who adds a permissions.allow entry and sees no
        # effect can tell *why* it was dropped instead of silently filed.
        echo "⚠️  Work-repo settings.json found but no global settings.json at $settings_file — permissions.allow not merged"
        return 0
    fi
    if ! command -v jq >/dev/null 2>&1; then
        echo "⚠️  Work-repo settings.json found but jq is unavailable for merging"
        return 0
    fi

    local merged
    merged=$(jq -s '
        .[0] as $base | .[1] as $work |
        $base | .permissions.allow = ((($base.permissions.allow // []) + ($work.permissions.allow // [])) | unique)
    ' "$settings_file" "$work_settings" 2>/dev/null)
    if [ $? -ne 0 ] || [ -z "$merged" ]; then
        echo "⚠️  Failed to merge work-repo settings.json"
        return 0
    fi

    # settings.json may be a symlink to a read-only mount — replace with a regular file
    [ -L "$settings_file" ] && rm "$settings_file"
    printf '%s\n' "$merged" > "$settings_file"
    echo "✅ Merged work-repo permissions.allow into $settings_file"
}

# claude_output_style_named_here <style> <styles_dir>
#   Advisory: does <style> plausibly name a style claude can resolve?
#
#   Lenient on purpose. Which spelling claude matches — file stem or frontmatter
#   `name:` — is not something claude promises, and the built-in set moves, so a
#   strict check would warn about working configurations. Nothing behavioural
#   hangs on the answer: the key is written either way.
claude_output_style_named_here() {
    local style="$1"
    local styles_dir="$2"
    local wanted="${style,,}"
    local item stem name

    case "$wanted" in
        default|explanatory|learning) return 0 ;;
    esac

    [ -d "$styles_dir" ] || return 1

    for item in "$styles_dir"/*; do
        [ -f "$item" ] || continue
        stem=$(basename "$item")
        stem="${stem%.md}"
        if [ "${stem,,}" = "$wanted" ]; then
            return 0
        fi
        # Compared as a string, not matched as a pattern: a style name is
        # operator-supplied text and would otherwise be a regex reaching grep.
        name=$(head -n 20 "$item" 2>/dev/null \
            | sed -n 's/^name:[[:space:]]*//p' | head -n 1)
        name="${name%$'\r'}"
        name="${name#\"}"; name="${name%\"}"
        name="${name#\'}"; name="${name%\'}"
        name="${name%"${name##*[![:space:]]}"}"
        if [ -n "$name" ] && [ "${name,,}" = "$wanted" ]; then
            return 0
        fi
    done
    return 1
}

# claude_apply_output_style <settings_file> <styles_dir>
#   Select the output style named by LMER_CLAUDE_OUTPUT_STYLE (issue #257). The
#   ships-vs-selects boundary this sits on: docs/LMER-CLI.md, "Output styles:
#   shipping one and selecting one are separate".
#
#   Called last, after every settings merge, so the launch's own variable is the
#   last word on the key. An implausible name warns and is still written — which
#   names claude resolves is claude's business.
claude_apply_output_style() {
    local settings_file="$1"
    local styles_dir="$2"
    local style="${LMER_CLAUDE_OUTPUT_STYLE}"
    local merged

    [ -n "$style" ] || return 0

    if ! command -v jq >/dev/null 2>&1; then
        echo "⚠️  LMER_CLAUDE_OUTPUT_STYLE=$style but jq is unavailable — outputStyle not set"
        return 0
    fi

    if ! claude_output_style_named_here "$style" "$styles_dir"; then
        echo "⚠️  Output style '$style' does not match any file in $styles_dir (by file stem or 'name:' frontmatter) and is not a built-in — setting outputStyle to it as given; claude uses its default if it cannot resolve the name"
    fi

    if [ -f "$settings_file" ]; then
        if ! merged=$(jq --arg style "$style" '.outputStyle = $style' "$settings_file" 2>/dev/null) \
           || [ -z "$merged" ]; then
            echo "⚠️  Failed to set outputStyle in $settings_file — leaving it unchanged"
            return 0
        fi
    else
        # No settings file at all: the operator still asked for a style, and a
        # file carrying only this key is a valid one.
        merged=$(jq -n --arg style "$style" '{outputStyle: $style}')
        mkdir -p "$(dirname "$settings_file")"
    fi

    # settings.json may be a symlink into a read-only mount (as above).
    [ -L "$settings_file" ] && rm "$settings_file"
    printf '%s\n' "$merged" > "$settings_file"
    echo "✅ Output style '$style' selected in $settings_file"
}
