#!/bin/bash
# Shared session-scoping helper for the chrome-agent launch wrapper and the
# SessionEnd sweep (issue #410, MR !289 review).
#
# The problem it solves: captivus's instance registry is CONTAINER-global
# (/tmp/chrome-agent/registry.json), and instance names derive from the working
# directory basename — so every session in /workspace produces "workspace-NN"
# and the registry carries no session identity at all. A sweep that reads it
# stops every browser in the container, including one another session is using.
#
# Attributing a browser by process tree does not work either: Chrome is
# reparented the moment the launcher exits (measured: chrome ppid=1,
# podman-init), so there is no path from a browser back to the session that
# started it.
#
# What DOES work is ancestry over our own processes. The launch wrapper and the
# SessionEnd hook both run as descendants of the session's harness process, so
# both can name it — and a spawn-harness child is a different harness process,
# which is exactly the distinction that was missing. We record what we launched
# under that key and stop only that. An instance we did not record is not ours
# to stop: this fails toward a leaked browser, never toward killing a live one.

# chrome_agent_session_key
#   Print a stable per-session key, or nothing when no harness ancestor is
#   found (in which case the caller records nothing and sweeps nothing).
#
#   The key pairs the harness process's pid with its start time, because pids
#   are recycled and a stale ledger must not be adopted by an unrelated
#   process that happens to land on the same number.
chrome_agent_session_key() {
    local pid ppid comm start
    # Start at this process, not at its parent: the caller may itself BE the
    # harness (a script the harness runs in-process), and starting a level up
    # would walk straight past it to whatever launched the harness.
    pid="$$"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        [ -z "$pid" ] && return 0
        [ "$pid" = "0" ] || [ "$pid" = "1" ] && return 0
        comm="$(cat "/proc/$pid/comm" 2>/dev/null)" || return 0
        case "$comm" in
            claude|codex|pi|node)
                # Field 22 of /proc/<pid>/stat is starttime in clock ticks.
                start="$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null)"
                printf '%s-%s\n' "$pid" "${start:-0}"
                return 0
                ;;
        esac
        ppid="$(awk '/^PPid:/{print $2}' "/proc/$pid/status" 2>/dev/null)"
        [ -z "$ppid" ] && return 0
        pid="$ppid"
    done
}

# chrome_agent_pid_starttime <pid>
#   Print the process's start time in clock ticks since boot, or nothing if it
#   is gone. Paired with a pid this is a stable unique identity: pids are
#   recycled, (pid, starttime) is not.
#
#   Parsed by stripping through the last ")" rather than by field index,
#   because /proc/<pid>/stat's comm field is parenthesised and may itself
#   contain spaces. starttime is field 22 overall, so field 20 of what remains.
chrome_agent_pid_starttime() {
    local pid="$1" stat rest
    case "$pid" in
        "" | *[!0-9]* ) return 0 ;;
    esac
    stat="$(cat "/proc/$pid/stat" 2>/dev/null)" || return 0
    rest="${stat#*') '}"
    [ "$rest" = "$stat" ] && return 0
    printf '%s\n' "$rest" | awk '{print $20}'
}

# chrome_agent_instance_pid <registry-file> <instance-name>
#   Print the pid captivus currently has registered for that instance name, or
#   nothing when the registry, the entry or jq are unavailable. Nothing means
#   "cannot confirm", and an unconfirmable instance is never stopped.
chrome_agent_instance_pid() {
    local registry="$1" name="$2"
    [ -f "$registry" ] || return 0
    command -v jq >/dev/null 2>&1 || return 0
    jq -r --arg n "$name" '
        if type == "object" and (.[$n] | type) == "object"
        then (.[$n].pid // empty)
        else empty end
    ' "$registry" 2>/dev/null
}

# chrome_agent_ledger_path
#   Path to this session's record of the instances it launched. Empty output
#   means "no session key" — the caller must then do nothing.
chrome_agent_ledger_path() {
    local key root
    key="$(chrome_agent_session_key)"
    [ -n "$key" ] || return 0
    root="${LMER_CHROME_AGENT_LEDGER_DIR:-/tmp/chrome-agent-lmer}"
    printf '%s/%s.instances\n' "$root" "$key"
}
