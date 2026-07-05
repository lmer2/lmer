#!/bin/bash
# Helpers for laying out commands/skills/settings under ~/.claude.
#
# Both the lmer global tree (typically /Agents/global/agent-files/claude)
# and — when present — the work repository (/work/agent-files/claude) can
# contribute slash commands, skills, and permission grants. Earlier versions
# of claude-runner.sh symlinked ~/.claude/commands as a single directory
# pointing at the global tree, which made it impossible to overlay work-repo
# contributions. The helpers below replace that with per-entry symlinks so
# multiple sources can coexist, with the work repository taking precedence
# on name collisions.
#
# This file is sourced by claude-runner.sh and by tests.

# claude_link_agent_files <home_claude> <global_src> <work_src>
#   Populate <home_claude>/commands/, skills/, and agents/ with symlinks
#   from each source's corresponding subdirectory. <global_src> and
#   <work_src> may each be empty. <home_claude> is taken as a parameter so
#   tests can target a temporary directory.
claude_link_agent_files() {
    local home_claude="$1"
    local global_src="$2"
    local work_src="$3"
    local subdir target item linked_global linked_work

    for subdir in commands skills agents; do
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
