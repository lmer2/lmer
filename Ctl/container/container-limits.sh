#!/bin/bash
# Derive CONTAINER_LIMITS from the container's own cgroup.
#
# Sourced from ~/.bashrc so every session shell reports the limits that were
# actually applied to this container. LMER_CPUS/LMER_MEMORY/LMER_PIDS_LIMIT
# change those limits per run, so any value fixed at image build time is a
# guess that goes stale the first time a run overrides one of them.
#
# CONTAINER_LIMITS is a display string for a human or an agent reading the shell
# environment. It is not a machine interface: the CPU figure is rounded, and any
# field can read "unlimited" or "unknown". Code that needs a number — a worker
# count, a memory budget — must read the cgroup files itself rather than parse
# this back.
#
# The cgroup root is an optional positional argument (default /sys/fs/cgroup)
# rather than an environment variable: a new LMER_* var would have to be
# threaded through the env plumbing (--show-env, docs, container passthrough)
# purely to give the tests a seam.
#
# Reading $root directly assumes the container's own cgroup is mounted there,
# which holds on cgroup v1 and on v2 under the private cgroup namespace docker
# and podman default to. A v2 host running --cgroupns=host would expose the v2
# root cgroup instead, which carries no cpu.max/memory.max, so every field would
# read "unknown". lmer passes no --cgroupns, so that path is not shipped.
#
# Nothing here may disturb the sourcing shell: reads are guarded, all output is
# captured into the variable, and a shell running under `set -e`/`set -u` must
# survive an unreadable or absent cgroup root (every failure path yields
# "unknown" rather than a non-zero status).

# Print the contents of a readable file, or fail (used only in conditions).
__lmer_limits_read() {
    if [ -r "$1" ]; then
        cat -- "$1" 2>/dev/null
    else
        return 1
    fi
}

# Print quota/period as a trimmed decimal with its unit (1core, 2cores,
# 0.5cores), or "unknown". The unit is carried because the sole reader is a
# human, and a bare number beside "Memory:2GiB" does not say what it counts.
__lmer_limits_ratio() {
    awk -v q="$1" -v p="$2" 'BEGIN {
        if (q !~ /^[0-9]+$/ || p !~ /^[0-9]+$/ || p + 0 == 0) { printf "unknown"; exit }
        s = sprintf("%.3f", q / p)
        sub(/0+$/, "", s)
        sub(/\.$/, "", s)
        printf "%s%s", s, (s == "1" ? "core" : "cores")
    }'
}

__lmer_limits_cpu() {
    local root="$1" raw quota period
    if raw=$(__lmer_limits_read "$root/cpu.max"); then
        # cgroup v2: "<quota|max> <period>" on one line.
        read -r quota period _ <<<"$raw"
        if [ "$quota" = "max" ]; then
            printf 'unlimited'
            return 0
        fi
    elif raw=$(__lmer_limits_read "$root/cpu/cpu.cfs_quota_us"); then
        quota="${raw%%$'\n'*}"
        if period=$(__lmer_limits_read "$root/cpu/cpu.cfs_period_us"); then
            period="${period%%$'\n'*}"
        else
            period=""
        fi
        # cgroup v1 spells "no bandwidth limit" as a negative quota.
        if [ "$quota" -le 0 ] 2>/dev/null; then
            printf 'unlimited'
            return 0
        fi
    else
        printf 'unknown'
        return 0
    fi
    __lmer_limits_ratio "$quota" "$period"
}

__lmer_limits_memory() {
    local root="$1" raw value
    if raw=$(__lmer_limits_read "$root/memory.max"); then
        value="${raw%%$'\n'*}"
        if [ "$value" = "max" ]; then
            printf 'unlimited'
            return 0
        fi
    elif raw=$(__lmer_limits_read "$root/memory/memory.limit_in_bytes"); then
        value="${raw%%$'\n'*}"
    else
        printf 'unknown'
        return 0
    fi
    awk -v b="$value" 'BEGIN {
        if (b !~ /^[0-9]+$/) { printf "unknown"; exit }
        # cgroup v1 has no "max" spelling: an unset limit reads as a page-aligned
        # LONG_MAX. 2^60 bytes (1 EiB) is far beyond any real limit, so treat
        # anything at or above it as unset rather than matching one sentinel.
        if (b >= 1152921504606846976) { printf "unlimited"; exit }
        gib = b / 1073741824
        if (gib >= 1) {
            rounded = int(gib * 10 + 0.5) / 10
            if (rounded == int(rounded)) printf "%dGiB", rounded
            else printf "%.1fGiB", rounded
            exit
        }
        printf "%dMiB", int(b / 1048576 + 0.5)
    }'
}

__lmer_limits_pids() {
    local root="$1" raw value
    if raw=$(__lmer_limits_read "$root/pids.max"); then
        :
    elif raw=$(__lmer_limits_read "$root/pids/pids.max"); then
        :
    else
        printf 'unknown'
        return 0
    fi
    value="${raw%%$'\n'*}"
    if [ "$value" = "max" ]; then
        printf 'unlimited'
        return 0
    fi
    case "$value" in
        ''|*[!0-9]*) printf 'unknown' ;;
        *) printf '%s' "$value" ;;
    esac
}

__lmer_limits_compose() {
    local root="${1:-/sys/fs/cgroup}"
    printf 'CPU:%s Memory:%s Processes:%s' \
        "$(__lmer_limits_cpu "$root")" \
        "$(__lmer_limits_memory "$root")" \
        "$(__lmer_limits_pids "$root")"
}

CONTAINER_LIMITS="$(__lmer_limits_compose "${1:-}" 2>/dev/null)"
export CONTAINER_LIMITS

# Executed rather than sourced: print the value, so the current limits can be
# read without opening a fresh login shell. Sourcing stays silent.
if [ "${BASH_SOURCE[0]:-}" = "$0" ]; then
    printf '%s\n' "$CONTAINER_LIMITS"
fi

unset -f __lmer_limits_read __lmer_limits_ratio __lmer_limits_cpu \
    __lmer_limits_memory __lmer_limits_pids __lmer_limits_compose
