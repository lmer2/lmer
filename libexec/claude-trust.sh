#!/bin/bash
# Pre-trust the container workspace in claude's config, so the folder-trust
# and first-run-onboarding dialogs never block a session (issue #415).
#
# Claude Code stores folder trust per resolved project path, as
# projects["<path>"].hasTrustDialogAccepted in ~/.claude.json. The container's
# path is never the host path the user trusted, so without this a user whose
# host config has no entry for the workspace is prompted on every run — and a
# user with no host config at all also meets the first-run onboarding.
#
# The host ~/.claude.json is bind-mounted rw (harness.py's CredentialMount
# default), so when one exists this merges into it — the same class of write
# claude itself already makes to that file.
#
# Sourced by claude-runner.sh; the config path and workspace are parameters so
# tests can point them at a temp directory.

# claude_pretrust_workspace <config_file> <workspace_path>
#
# Merge the trust flag for <workspace_path> into <config_file>, creating the
# file when it is absent. Idempotent, and re-run on every container start, so
# nothing here depends on claude persisting the flag (upstream
# anthropics/claude-code#36403 says it sometimes does not).
#
# An existing config keeps every other key it has, including the user's real
# onboarding and auth state. Only a config this function *creates* is given
# hasCompletedOnboarding — a fresh container has no onboarding state to lose,
# and without it the first-run wizard blocks instead of the trust dialog.
#
# Never fatal: a missing jq, unparseable JSON or an unwritable config warns and
# leaves the file alone. Claude then shows its dialog, which is the status quo.
claude_pretrust_workspace() {
    local config="${1:?claude_pretrust_workspace: config file required}"
    local ws="${2:?claude_pretrust_workspace: workspace path required}"
    local merged seeded=""

    ws="$(realpath "$ws" 2>/dev/null || printf '%s' "$ws")"
    ws="${ws%/}"
    [ -n "$ws" ] || ws="/"

    if ! command -v jq >/dev/null 2>&1; then
        echo "⚠️  jq not available; cannot pre-trust $ws (claude may show its folder-trust dialog)"
        return 0
    fi

    # Already trusted is the common case, and the host config is a 73 KB file
    # every running container has bind-mounted rw. Reading before writing keeps
    # us out of a truncate-and-rewrite race with the other sessions' claudes.
    if [ -s "$config" ] && jq -e --arg p "$ws" \
            '.projects[$p].hasTrustDialogAccepted == true' "$config" >/dev/null 2>&1; then
        echo "✅ Workspace already trusted for claude: $ws"
        return 0
    fi

    # -s, not -f: a zero-byte config takes the seed path. It is what a failed
    # write leaves behind, and the merge path cannot recover from it — jq reads
    # empty input as no output, which the guard below reads as "do not touch".
    if [ -s "$config" ]; then
        merged="$(jq --arg p "$ws" '.projects[$p].hasTrustDialogAccepted = true' "$config" 2>/dev/null)"
    else
        merged="$(jq -n --arg p "$ws" '{hasCompletedOnboarding: true, projects: {($p): {hasTrustDialogAccepted: true}}}')"
        seeded=1
    fi

    if [ -z "$merged" ]; then
        echo "⚠️  Could not pre-trust $ws in $config (unreadable or invalid JSON) — leaving it untouched"
        return 0
    fi

    # Written in place, not renamed over: when the host config exists this path
    # is a single-file bind mount, and you cannot rename onto a mount point.
    # umask only bites on creation, so an existing config keeps its own mode.
    # A seeded one is chmod'd rather than left to umask: the empty-leftover case
    # is a create we did not do, and claude will put credentials in here.
    if ( umask 077; printf '%s\n' "$merged" > "$config" ) 2>/dev/null; then
        [ -n "$seeded" ] && chmod 600 "$config" 2>/dev/null
        echo "✅ Workspace pre-trusted for claude: $ws"
    else
        echo "⚠️  Could not write $config; $ws stays untrusted"
    fi
    return 0
}
