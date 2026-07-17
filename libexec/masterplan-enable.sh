#!/usr/bin/env bash
# Masterplan provisioning — the single owner of plugin install + bundle-root
# resolution, shared by two callers (spec: masterplan-on-demand):
#
#   masterplan-enable.sh --gated
#       Session start (claude-runner.sh). Honors the launch gating in
#       lmer_cli.container.masterplan: exit 1 = not a masterplan session
#       (silent skip), exit 2 = enabled but run dir indeterminate (warn).
#
#   masterplan-enable.sh [--repo-host <h> --repo-project <p>]
#       Mid-session, on demand. Forces enablement (no gating), persists
#       MASTERPLAN_RUNS_DIR (and the effective repo target, whether from
#       flags or the environment) to ~/.bashrc.d/masterplan-env.sh so
#       every subsequent shell sees it,
#       then instructs the caller to have the user run /reload-plugins.
#       Exit 0 = enabled; exit 2 = nothing provisioned — either the run dir
#       is indeterminate (stderr says to re-run with --repo-host/--repo-project)
#       or the python resolution step itself failed (stderr names the
#       interpreter). Exit 1 never occurs in this mode.
#
# stdout carries exactly one thing: the bundle root (machine-readable, the
# gated caller captures and exports it). All human messages go to stderr.
# Every plugin step is idempotent and non-fatal — warn and continue, never
# break a session.
set -u

GATED=0
REPO_HOST=""
REPO_PROJECT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --gated) GATED=1 ;;
        --repo-host) REPO_HOST="${2:?--repo-host needs a value}"; shift ;;
        --repo-project) REPO_PROJECT="${2:?--repo-project needs a value}"; shift ;;
        *) echo "masterplan-enable: unknown argument: $1" >&2; exit 64 ;;
    esac
    shift
done

# ── Bundle root ──
py_args=()
if [ "$GATED" -eq 0 ]; then
    py_args+=(--force)
    [ -n "$REPO_HOST" ] && py_args+=(--repo-host "$REPO_HOST")
    [ -n "$REPO_PROJECT" ] && py_args+=(--repo-project "$REPO_PROJECT")
fi
BUNDLE_ROOT="$("${LMER_PYTHON:-python3}" -m lmer_cli.container.masterplan ${py_args[@]+"${py_args[@]}"} 2>/dev/null)"
rc=$?
if [ "$GATED" -eq 1 ] && [ "$rc" -eq 1 ]; then
    exit 1  # gated: not a masterplan session — silent skip
fi
if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
    # main() only ever returns 0/2 (forced) or 0/1/2 (gated, and gated rc 1
    # exited above), so anything reaching here is an interpreter-level
    # failure (rc 1 forced: lmer_cli not importable; 126/127: broken
    # LMER_PYTHON) whose stderr the 2>/dev/null above swallowed — say so
    # rather than misreporting it below as a missing repo target (or,
    # gated, asserting enablement for a session the gate never evaluated).
    echo "masterplan-enable: ${LMER_PYTHON:-python3} -m lmer_cli.container.masterplan failed (exit $rc) — is lmer_cli importable in this environment?" >&2
    exit 2
fi
if [ "$rc" -ne 0 ] || [ -z "$BUNDLE_ROOT" ]; then
    if [ "$GATED" -eq 1 ]; then
        echo "⚠️  masterplan: enabled but the run dir is indeterminate (LMER_REPO_HOST/LMER_REPO_PROJECT unset?); skipping provisioning" >&2
    else
        echo "masterplan-enable: run dir indeterminate — re-run with --repo-host <host> --repo-project <project> (ask the user which project this work is for)" >&2
    fi
    exit 2
fi

# ── Mid-session env persistence ──
# Each Bash tool call starts a fresh profile-initialized shell and the
# container ~/.bashrc sources ~/.bashrc.d/*, so a drop-in is how env reaches
# the rest of a running session (the parent claude process cannot be changed).
if [ "$GATED" -eq 0 ]; then
    # Persist the EFFECTIVE repo target, not just the flag values: a flag-less
    # re-run (idempotence) may inherit LMER_REPO_HOST/LMER_REPO_PROJECT from a
    # previously written drop-in, and the rewrite below must not drop them.
    persist_host="${REPO_HOST:-${LMER_REPO_HOST:-}}"
    persist_project="${REPO_PROJECT:-${LMER_REPO_PROJECT:-}}"
    mkdir -p "$HOME/.bashrc.d"
    {
        echo "# Written by masterplan-enable.sh — masterplan on-demand session env."
        echo "export MASTERPLAN_RUNS_DIR=\"$BUNDLE_ROOT\""
        [ -n "$persist_host" ] && echo "export LMER_REPO_HOST=\"$persist_host\""
        [ -n "$persist_project" ] && echo "export LMER_REPO_PROJECT=\"$persist_project\""
        true
    } > "$HOME/.bashrc.d/masterplan-env.sh"
fi

# ── Plugin provisioning ──
echo "✅ Masterplan mode: bundles nest at $BUNDLE_ROOT" >&2
if command -v claude >/dev/null 2>&1; then
    # `claude plugin` persists enable-state into settings.json. If it is still
    # a symlink to a read-only mount, materialize a writable copy first (same
    # concern claude-runner.sh handles for its settings merge). cp preserves
    # the source's mode bits, so force owner-write after the copy.
    SETTINGS_FILE="${LMER_SETTINGS_FILE:-/home/developer/.claude/settings.json}"
    if [ -L "$SETTINGS_FILE" ]; then
        if cp --remove-destination "$(readlink -f "$SETTINGS_FILE")" "$SETTINGS_FILE" 2>/dev/null; then
            chmod u+w "$SETTINGS_FILE" 2>/dev/null || true
        else
            echo "⚠️  masterplan: could not materialize settings.json (continuing)" >&2
        fi
    fi
    # First existing candidate wins; when none exist, fall through to the
    # last one blindly — its failure warns via the claude call itself, which
    # preserves the pre-extraction behavior for the default /work mirror.
    # Resolving to anything but the FIRST candidate means the canonical
    # location is absent: warn so operators fix the first candidate (with
    # the default list, that means moving the mirror into the taskdef repo).
    MIRROR=""
    FIRST=""
    LAST=""
    IFS=':' read -ra CANDIDATES <<< "${LMER_MASTERPLAN_MIRROR_CANDIDATES:-/taskdef/mirrors/masterplan:/work/mirrors/masterplan}"
    for candidate in "${CANDIDATES[@]}"; do
        [ -n "$candidate" ] || continue
        [ -n "$FIRST" ] || FIRST="$candidate"
        LAST="$candidate"
        if [ -z "$MIRROR" ] && [ -d "$candidate" ]; then
            MIRROR="$candidate"
        fi
    done
    MIRROR="${MIRROR:-$LAST}"
    if [ "$MIRROR" != "$FIRST" ]; then
        echo "⚠️  masterplan: mirror resolved to $MIRROR, not the canonical (first) candidate ($FIRST) — that fallback is deprecated; configure LMER_TASKDEF_REPO with a taskdef repo shipping mirrors/masterplan" >&2
    fi
    # >&2 on each: `claude plugin` success chatter goes to stdout, and this
    # script's stdout contract is the bare bundle root (the gated caller
    # captures ALL of stdout into MASTERPLAN_RUNS_DIR).
    claude plugin marketplace add "$MIRROR" >&2 \
        || echo "⚠️  masterplan: marketplace add failed (continuing)" >&2
    claude plugin install masterplan@rasatpetabit-masterplan >&2 \
        || echo "⚠️  masterplan: plugin install failed (continuing)" >&2
    claude plugin enable masterplan >&2 \
        || echo "⚠️  masterplan: plugin enable failed (continuing)" >&2
else
    echo "⚠️  masterplan: claude not on PATH; skipping plugin provisioning" >&2
fi

if [ "$GATED" -eq 0 ]; then
    echo "masterplan-enable: done. Ask the user to run /reload-plugins once; then /masterplan is available (bundle root: $BUNDLE_ROOT)" >&2
fi

echo "$BUNDLE_ROOT"
exit 0
