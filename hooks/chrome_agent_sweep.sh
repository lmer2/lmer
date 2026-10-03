#!/bin/bash
# SessionEnd backstop: stop the captivus chrome-agent browsers THIS session
# launched — and nothing else.
#
# Why a hook and not just documentation (issue #410): each headless Chrome
# costs a few hundred MiB against the container's memory limit (2 GiB by
# default), so roughly two fit, and captivus keeps the browser alive between
# CLI calls by design. Whether the agent remembers to tear one down is a
# prediction about model behaviour that nothing guarantees, and when it is
# wrong nobody finds out. A sweep we run ourselves is a fact we control.
#
# SCOPE — read this before changing the source it reads (MR !289 review).
# It reads a ledger THIS session wrote (via bin/chrome-agent-launch), never
# captivus's own registry. That registry is container-global and its instance
# names come from the working-directory basename, so every session in
# /workspace produces "workspace-NN" and the registry cannot say who owns what.
# Sweeping it stopped every browser in the container: a spawn-harness child's
# SessionEnd would stop its parent's browser, and `/clear` would stop a browser
# the same session was still using. Attributing by process tree does not rescue
# it either — Chrome is reparented to init the moment the launcher exits
# (measured: chrome ppid=1). Ancestry over OUR OWN processes does work, and
# that is what keys the ledger.
#
# The ledger records an IDENTITY per browser (name + pid + that pid's start
# time), not just a name, because captivus REUSES names: it hands out the
# lowest free `<dir>-NN` and `stop` frees it again. A session that stops its
# browser promptly — what the skill tells it to do — leaves a name that a later
# session then holds, and stopping by name would kill that one. Before stopping
# anything the sweep re-confirms that the entry captivus holds under the name is
# still the same process.
#
# The consequence, deliberately: a browser started with a bare
# `chrome-agent launch` is not recorded and is NOT swept, and neither is one
# whose identity cannot be confirmed. Leaking a browser is visible and bounded;
# stopping someone else's is silent breakage. When in doubt this leaks.
#
# What it does not cover: Claude does not block its exit on SessionEnd hooks
# (docs/RUN-STATE.md), so container teardown can kill it mid-sweep, and by the
# time it fires a run that leaked mid-session has already paid. It earns its
# place where the container outlives the session. Prompt in-session teardown
# stays the primary discipline, which is why the skill still teaches it.

set -u

# `clear` and `resume` fire SessionEnd while the session CARRIES ON, so
# sweeping there would stop a browser still in use. settings.json already
# matches only the terminal reasons; this reads the payload as well, so the
# script stays correct if it is ever wired without that matcher — by a
# work-repo settings override, or by hand. An absent or unreadable payload
# means "no reason given" and the sweep proceeds, which is the pre-matcher
# behaviour.
if [ ! -t 0 ]; then
    _payload="$(timeout 2 cat 2>/dev/null || true)"
    case "$_payload" in
        *'"reason"'*'"clear"'* | *'"reason"'*'"resume"'*) exit 0 ;;
    esac
fi

command -v chrome-agent >/dev/null 2>&1 || exit 0

_dir="$(dirname "$(readlink -f "$0")")"
# shellcheck source=../libexec/chrome-agent-session.sh
if [ -r "$_dir/../libexec/chrome-agent-session.sh" ]; then
    . "$_dir/../libexec/chrome-agent-session.sh"
else
    exit 0
fi

LEDGER="$(chrome_agent_ledger_path)"
[ -n "$LEDGER" ] || exit 0
[ -f "$LEDGER" ] || exit 0

REGISTRY="${LMER_CHROME_AGENT_REGISTRY:-/tmp/chrome-agent/registry.json}"

stopped=0
while read -r instance pid start _rest; do
    # Written by us, re-validated anyway: these reach a command line.
    case "$instance" in
        "" | *[!A-Za-z0-9._-]* ) continue ;;
    esac
    case "$pid$start" in
        "" | *[!0-9]* ) continue ;;
    esac

    # IDENTITY, not name. captivus reuses instance names — it hands out the
    # lowest free `<dir>-NN` and `stop` frees it — so by the time this runs,
    # a name we recorded may belong to somebody else's browser. Stopping on the
    # name alone would kill theirs, which is the cross-session kill this whole
    # mechanism exists to prevent, reintroduced one layer down. So the entry
    # captivus currently holds under that name must still be the same process:
    # same pid, and that pid must still have the start time we saw (pids are
    # recycled; a pid paired with its start time is not).
    #
    # Anything we cannot confirm — registry gone, entry gone, pid moved on, no
    # jq to read it with — is left alone. Fail toward a leaked browser.
    live_pid="$(chrome_agent_instance_pid "$REGISTRY" "$instance")"
    [ -n "$live_pid" ] || continue
    [ "$live_pid" = "$pid" ] || continue
    live_start="$(chrome_agent_pid_starttime "$pid")"
    [ -n "$live_start" ] || continue
    [ "$live_start" = "$start" ] || continue

    # `stop` can block indefinitely: it sends Browser.close and awaits the
    # reply with no timeout of its own, so a Chrome holding the socket open
    # never returns. `|| true` in the hook wiring bounds the exit code, not the
    # wall clock — only `timeout` bounds a hang.
    # </dev/null so `stop` cannot consume the loop's own stdin.
    out="$(timeout 10 chrome-agent stop "$instance" 2>/dev/null </dev/null)" || continue
    # `stop` also exits 0 for an instance it merely found dead and reaped, so
    # the exit status does not mean memory came back. Only its own "Stopped"
    # line does.
    case "$out" in
        Stopped*) stopped=$((stopped + 1)) ;;
    esac
done < "$LEDGER"

rm -f "$LEDGER" 2>/dev/null || true

if [ "$stopped" -gt 0 ]; then
    echo "🧹 chrome-agent: stopped $stopped browser instance(s) this session launched"
fi

exit 0
