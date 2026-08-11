#!/bin/bash
# Run the lmer platform image with the invariants the topology needs (#150).
#
# SPIKE-STAGE TOOLING. This is not a deployment unit: it is the shortest
# readable thing that gets the mounts, the user and the network right, so an
# operator can walk docs/PLATFORM-CONTAINER.md and so the arguments live
# somewhere reviewable instead of in a paste buffer. Read it, run it with
# --print, then copy what you need into whatever supervises the container for
# real. It supervises nothing, restarts nothing and upgrades nothing.
#
# The one rule it exists to encode: the containerized platform derives host
# paths from $HOME and hands them to the *host's* daemon as -v arguments, so
# $HOME/.lmer must be mounted at the identical absolute path and HOME must be
# the host's home. See "The path-identity invariant" in the doc for what a
# renamed mount silently does.

set -euo pipefail

IMAGE="lmer-platform:dev"
NAME="lmer-platform"
RUNTIME=""
SOCKET=""
DETACH=1
PRINT_ONLY=0

# Path-identical extra mounts the operator asked for (--mount-ro/--mount-rw),
# as "path:mode" pairs, plus extra env names to forward (--env).
EXTRA_MOUNTS=()
EXTRA_ENV=()
# Extra runtime flags (--runtime-arg), inserted *before* the image so the
# runtime reads them.
RUNTIME_ARGS=()
# Everything after `--`. It lands after the image, which means the ENTRYPOINT
# (`lmer platform`) reads it as its own arguments — `-- status` runs
# `lmer platform status`. Runtime flags do not go here; that is --runtime-arg.
PASSTHROUGH=()

# Environment the daemon and the `lmer` it spawns read, forwarded by *name* so
# no value (least of all a secret or a token) reaches the process table. The two
# prefixes — LMER_PLATFORM_* and GITLAB_TOKEN_* — are matched below; these are
# the exact names, and each is here because something in-container reads it —
# see docs/PLATFORM-CONTAINER.md.
FORWARD_EXTRA=(
    LMER_WORK_REPO
    # The work-repo mirror clones and pulls over HTTPS with a token when one is
    # exported (lmer_platform.workrepo._authenticated_url), and the `lmer` this
    # daemon spawns authenticates target-repo clones from the same environment
    # (lmer_cli.tokens.get_token: the work-repo pair, then GITLAB_TOKEN_<host>,
    # then GH_TOKEN/GITHUB_TOKEN for GitHub hosts, then plain GITLAB_TOKEN).
    # Host-specific GITLAB_TOKEN_* names are matched as a prefix below, which is
    # also what carries the deprecated GITLAB_TOKEN_worklog fallback.
    # Alternative to all of this: put them in ~/.lmer/.env, which the daemon
    # loads (daemon._load_env_files) and which rides the state mount.
    LMER_WORK_REPO_TOKEN
    GITLAB_TOKEN
    GH_TOKEN
    GITHUB_TOKEN
    LMER_IMAGE
    LMER_REGISTRY
    LMER_CLONE_CACHE
    LMER_CLONE_CACHE_DIR
    LMER_MOUNT_UV_CACHE
    LMER_TASKDEF_PATHS
    LMER_PRESETS_FILE
    UV_CACHE_DIR
    XDG_CACHE_HOME
    SSH_AUTH_SOCK
)

# LMER_PLATFORM_* variables that must NOT be forwarded: both name a *host*
# directory for the UI, and either one would shadow the bundle baked into the
# image with a path that means something else inside the container. `--env NAME`
# still forwards one — that is an operator saying it on purpose, e.g. to serve a
# bundle from a path they also mounted.
FORWARD_BLOCKED=(
    LMER_PLATFORM_UI_DIST
    LMER_PLATFORM_WEB_DIR
)

# LMER_PLATFORM_* variables that name a host path and are forwarded only if that
# path is covered by a mount this run makes. Each overrides a default under
# ~/.lmer (config.secret_path, config.mirror_path), so an unmounted value does
# not fail — it silently relocates state into the container's ephemeral
# filesystem. See the config.json check below, which is the same question asked
# of the persisted fields.
FORWARD_PATH_CHECKED=(
    LMER_PLATFORM_SECRET_FILE
    LMER_PLATFORM_WORK_REPO_MIRROR
)

# One line per argument, so a multi-sentence reason stays readable in a
# terminal instead of arriving as one wrapped paragraph.
die() {
    printf 'platform-container-run: %s\n' "$@" >&2
    exit 1
}

note() {
    printf 'platform-container-run: %s\n' "$@" >&2
}

usage() {
    cat <<'EOF'
Usage: platform-container-run.sh [options] [-- platform-args...]

Options:
  --image TAG        platform image to run (default: lmer-platform:dev)
  --name NAME        container name (default: lmer-platform)
  --runtime NAME     docker|podman (default: whichever is on PATH, docker first)
  --socket PATH      runtime socket to mount (default: the usual candidates for
                     the chosen runtime, as lmer's own probe tries them)
  --foreground       run in the foreground with --rm instead of detached
  --mount-ro PATH    extra host path, bind-mounted read-only at the same path
  --mount-rw PATH    extra host path, bind-mounted read-write at the same path
  --env NAME         forward one more environment variable by name
  --runtime-arg ARG  extra flag for docker/podman itself, before the image
                     (repeatable): --runtime-arg --memory=2g
  -n, --print        print the command that would run, and exit
  -h, --help         this text

Everything after `--` is appended after the image, so it is read by the
image's ENTRYPOINT (`lmer platform`) as its arguments — `-- status` runs
`lmer platform status` instead of the daemon. For flags the *runtime* should
read, use --runtime-arg.

See docs/PLATFORM-CONTAINER.md — especially the path-identity invariant.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --image) [ $# -ge 2 ] || die "--image needs a value"; IMAGE="$2"; shift 2 ;;
        --name) [ $# -ge 2 ] || die "--name needs a value"; NAME="$2"; shift 2 ;;
        --runtime) [ $# -ge 2 ] || die "--runtime needs a value"; RUNTIME="$2"; shift 2 ;;
        --socket) [ $# -ge 2 ] || die "--socket needs a path"; SOCKET="$2"; shift 2 ;;
        --foreground) DETACH=0; shift ;;
        --mount-ro)
            [ $# -ge 2 ] || die "--mount-ro needs a path"
            EXTRA_MOUNTS+=("$2:ro"); shift 2 ;;
        --mount-rw)
            [ $# -ge 2 ] || die "--mount-rw needs a path"
            EXTRA_MOUNTS+=("$2:rw"); shift 2 ;;
        --env)
            [ $# -ge 2 ] || die "--env needs a variable name"
            EXTRA_ENV+=("$2"); shift 2 ;;
        --runtime-arg)
            [ $# -ge 2 ] || die "--runtime-arg needs a value"
            RUNTIME_ARGS+=("$2"); shift 2 ;;
        -n|--print) PRINT_ONLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        --) shift; PASSTHROUGH=("$@"); break ;;
        *) usage >&2; die "unknown argument: $1" ;;
    esac
done

# --- the host side -----------------------------------------------------------

# $HOME rather than the passwd entry: it is what Python's Path.home() reads
# in-container, so deriving the mount from anything else could disagree with
# the thing the invariant is about.
HOST_HOME="${HOME:-}"
[ -n "$HOST_HOME" ] || die "HOME is not set — nothing to keep identical"
case "$HOST_HOME" in
    /*) ;;
    *) die "HOME must be an absolute path (got '$HOST_HOME')" ;;
esac
[ -d "$HOST_HOME" ] || die "HOME does not exist: $HOST_HOME"

HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

# expand_tilde <value>: *value* with a leading ~ replaced by the host home, which
# is what Python's Path.expanduser() does to the config values read below — so a
# `~/...` setting is compared as the path lmer will actually use. Anything else is
# echoed unchanged.
expand_tilde() {
    # shellcheck disable=SC2088  # a literal ~ is the pattern here, not a path
    case "$1" in
        "~") printf '%s\n' "$HOST_HOME" ;;
        "~/"*) printf '%s\n' "$HOST_HOME/${1#\~/}" ;;
        *) printf '%s\n' "$1" ;;
    esac
}

STATE_DIR="$HOST_HOME/.lmer"
[ -d "$STATE_DIR" ] || die \
    "no state directory at $STATE_DIR" \
    "run lmer once on this host first, or create it: the platform's config," \
    "secret, registry and session logs all live there, and it has to be" \
    "owned by uid $HOST_UID"

if [ -z "$RUNTIME" ]; then
    # Same order as lmer_cli.runtime.detect_runtime(), so the helper and the
    # in-container CLI cannot disagree about which engine this host has.
    if command -v docker >/dev/null 2>&1; then
        RUNTIME="docker"
    elif command -v podman >/dev/null 2>&1; then
        RUNTIME="podman"
    else
        die "neither docker nor podman on PATH"
    fi
fi
command -v "$RUNTIME" >/dev/null 2>&1 || die "$RUNTIME is not on PATH"

# Socket candidates, mirroring mounts._find_container_socket. Always mounted at
# /var/run/docker.sock inside: the image ships the docker client only, and
# podman's socket speaks a docker-compatible API (see docs/SERVICE-MODE.md).
# --socket names one the candidate list does not know (a relocated rootless
# socket, and it is also how this script's tests exercise --print).
if [ -n "$SOCKET" ]; then
    [ -S "$SOCKET" ] || die "--socket $SOCKET is not a socket"
elif [ "$RUNTIME" = "podman" ]; then
    for candidate in \
        "/run/user/$HOST_UID/podman/podman.sock" \
        "/var/run/podman/podman.sock" \
        "/run/podman/podman.sock"
    do
        if [ -S "$candidate" ]; then SOCKET="$candidate"; break; fi
    done
elif [ -S "/var/run/docker.sock" ]; then
    SOCKET="/var/run/docker.sock"
fi
[ -n "$SOCKET" ] || die \
    "no $RUNTIME socket found on this host" \
    "the platform starts every session through it, so there is nothing to run"

# --- the run command ---------------------------------------------------------

cmd=()
# Destinations already claimed by a --volume. Both runtimes refuse a duplicate
# mount point outright, and there are two honest ways to ask for one here (a
# relocated clone cache that is also passed as --mount-rw, an extra mount that
# repeats a credential path), so the second request is dropped with a note
# rather than turned into a container that will not start.
mounted=()
# Host paths that are mounted at their own absolute path — every mount here
# except the socket, which is the one deliberate rename. Used by path_is_mounted
# to answer "would a host path this container is told about actually be there?".
identical=()

# add_mount <host-path> <container-path> <mode>
add_mount() {
    local host="$1" dest="$2" mode="$3" claimed
    for claimed in ${mounted[@]+"${mounted[@]}"}; do
        if [ "$claimed" = "$dest" ]; then
            note "already mounting $dest — skipping the duplicate ($host:$mode)"
            return 0
        fi
    done
    mounted+=("$dest")
    if [ "$host" = "$dest" ]; then identical+=("$host"); fi
    cmd+=("--volume" "$host:$dest:$mode")
}

# path_is_mounted <absolute-host-path>: true when the path is one of the
# path-identical mounts or lives under one. A prefix test on strings, so a
# symlink pointing out of a mounted tree reads as covered when it is not —
# conservative in the direction of not crying wolf.
path_is_mounted() {
    local path="$1" root
    for root in ${identical[@]+"${identical[@]}"}; do
        [ "$path" = "$root" ] && return 0
        case "$path" in
            "$root"/*) return 0 ;;
        esac
    done
    return 1
}

cmd+=("$RUNTIME" "run")
if [ "$DETACH" -eq 1 ]; then
    # Not --rm: `docker restart <name>` is how an upgrade and the spike's
    # restart step are performed, and a removed container cannot be restarted.
    cmd+=("--detach")
else
    cmd+=("--rm" "--interactive" "--tty")
fi
cmd+=("--name" "$NAME")

# tini as PID 1: the daemon forks an `lmer` per session, and that `lmer` forks
# the clone-cache updater. A grandchild whose parent exits reparents to PID 1
# and has to be reaped by something, or the fleet accumulates zombies that the
# registry's liveness check then has to reason about.
cmd+=("--init")

# Host network namespace, for three loopback reasons (all in the doc): session
# control planes are published on host loopback, free control ports are picked
# by probing host loopback, and the daemon's own loopback bind is then the
# host's — reachable from a browser on this host exactly as a bare-host install
# would be. -p and EXPOSE are moot under this.
cmd+=("--network=host")

# The image user must be the uid/gid that owns the state dir; the image is
# built with matching BUILD_UID/BUILD_GID, and this states it at run time too
# so a mismatched image fails on permissions rather than writing root-owned
# state into the operator's home.
cmd+=("--user" "$HOST_UID:$HOST_GID")
if [ "$RUNTIME" = "podman" ]; then
    # Same reason base_run_args passes it for sessions: keep the host uid
    # mapped so files written into the mounted state dir stay owned by it.
    cmd+=("--userns=keep-id")
fi

# THE invariant: identical absolute path on both sides, read-write.
add_mount "$STATE_DIR" "$STATE_DIR" "rw"
# HOME is what Path.home() resolves, and everything under ~/.lmer is derived
# from it — including the -v arguments the host's daemon will resolve.
cmd+=("--env" "HOME=$HOST_HOME")

add_mount "$SOCKET" "/var/run/docker.sock" "rw"
if socket_gid="$(stat -c '%g' "$SOCKET" 2>/dev/null)" && [ -n "$socket_gid" ]; then
    cmd+=("--group-add" "$socket_gid")
else
    note \
        "could not read the group of $SOCKET — running without --group-add" \
        "if the container cannot start sessions, this is why"
fi

# SELinux: disable labelling rather than relabel, which is what base_run_args
# does for sessions. A ,z on the operator's own home directory tree relabels
# host files, and this mount is that tree.
if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce 2>/dev/null)" = "Enforcing" ]; then
    cmd+=("--security-opt" "label=disable")
fi
# Hygiene, and only that: this container has a read-write runtime socket, which
# is root-equivalent on the host (it can start a privileged container mounting
# /). Blocking setuid escalation *inside* the container is still worth having —
# it costs nothing and the image needs no setuid binary — but nothing here is a
# security boundary between the container and the host. See "The container
# runtime socket" in docs/PLATFORM-CONTAINER.md.
cmd+=("--security-opt" "no-new-privileges")

# Paths outside ~/.lmer that the in-container lmer reads before it can mount
# them into a session: harness credentials (existence-checked by
# plan_credential_mounts), plus what the platform's own git and the
# container-home first-run seeding read. Read-only is enough — the session's
# own mount of these files is created by the host daemon from the host path,
# and directories rather than files so an atomically-rewritten credential is
# not pinned to a stale inode.
for rel in .claude .claude.json .codex .pi .ssh .gitconfig; do
    if [ -e "$HOST_HOME/$rel" ]; then
        add_mount "$HOST_HOME/$rel" "$HOST_HOME/$rel" "ro"
    fi
done

# The uv cache is opt-in for sessions (LMER_MOUNT_UV_CACHE) and
# existence-checked in-process, so without a path-identical mount the check
# declines and the session silently gets no cache. Resolution order copied
# from mounts.resolve_host_uv_cache_dir.
#
# Lowercased and matched against exactly lmer's truthy set (util.TRUTHY_VALUES:
# 1, true, yes, case-insensitive). A wider set here would mount for a value the
# in-container lmer reads as false, which is a mount that quietly does nothing.
uv_toggle="${LMER_MOUNT_UV_CACHE:-}"
case "${uv_toggle,,}" in
    1|true|yes)
        if [ -n "${UV_CACHE_DIR:-}" ]; then
            uv_cache="$UV_CACHE_DIR"
        elif [ -n "${XDG_CACHE_HOME:-}" ]; then
            uv_cache="$XDG_CACHE_HOME/uv"
        else
            uv_cache="$HOST_HOME/.cache/uv"
        fi
        if [ -d "$uv_cache" ]; then
            add_mount "$uv_cache" "$uv_cache" "ro"
        else
            note "LMER_MOUNT_UV_CACHE is set but $uv_cache does not exist — not mounted"
        fi
        ;;
esac

# A clone cache moved out of ~/.lmer needs a read-WRITE mount: the mirror
# maintenance runs as a fork of the spawning lmer, which is a process inside
# this container. The default (~/.lmer/clone-cache) is already covered.
#
# The three cases below are resolve_host_clone_cache_dir's own (mounts.py): it
# expands a leading ~, refuses a relative value, and refuses one equal to / or
# $HOME as too broad to bind-mount. Both refusals fall back to the default under
# ~/.lmer, which the state mount already carries — so there is nothing to mount
# for them, only something to say.
if [ -n "${LMER_CLONE_CACHE_DIR:-}" ]; then
    clone_cache="$(expand_tilde "$LMER_CLONE_CACHE_DIR")"
    # Trailing slashes first: lmer compares Path objects, for which /home/x/ and
    # /home/x are the same path, and the too-broad check is an equality test.
    while [ "$clone_cache" != "/" ] && [ "${clone_cache%/}" != "$clone_cache" ]; do
        clone_cache="${clone_cache%/}"
    done
    case "$clone_cache" in
        "$HOST_HOME"|/)
            note \
                "ignoring LMER_CLONE_CACHE_DIR='$LMER_CLONE_CACHE_DIR'" \
                "lmer refuses / and \$HOME as too broad to bind-mount and falls" \
                "back to \$HOME/.lmer/clone-cache, which the state mount covers" ;;
        /*)
            add_mount "$clone_cache" "$clone_cache" "rw" ;;
        *)
            note \
                "ignoring relative LMER_CLONE_CACHE_DIR='$LMER_CLONE_CACHE_DIR'" \
                "lmer refuses a relative value too — as a -v source it would be" \
                "read as a named volume" ;;
    esac
fi

# The agent socket, if this shell has one. Path-identical because the value is
# forwarded as-is and reaches a session mount unchanged; stale as soon as the
# login session that owns it ends, which is why it is not in a unit file.
if [ -n "${SSH_AUTH_SOCK:-}" ] && [ -S "${SSH_AUTH_SOCK}" ]; then
    add_mount "$SSH_AUTH_SOCK" "$SSH_AUTH_SOCK" "rw"
fi

for spec in ${EXTRA_MOUNTS[@]+"${EXTRA_MOUNTS[@]}"}; do
    path="${spec%:*}"
    mode="${spec##*:}"
    case "$path" in
        /*) ;;
        *) die "extra mount must be an absolute path (got '$path')" ;;
    esac
    [ -e "$path" ] || die "extra mount does not exist: $path"
    add_mount "$path" "$path" "$mode"
done

# --- host paths the persisted config points at -------------------------------
#
# Three fields in ~/.lmer/platform/config.json override a state-dir default with
# an absolute *host* path, and a veteran state dir — the one the runbook says to
# reuse — is the most likely to carry them. Under ~/.lmer they ride the state
# mount; outside it they name a path this container does not have, and none of
# the three fails loudly. So they are read here and checked against the mounts
# actually being made.
#
# Reading only, and only these fields: a note is all this can honestly do, since
# mounting a path the operator did not ask for would be the helper deciding
# something for them. Any parse trouble is silence — a helper that refuses to
# start the platform because it could not read a JSON file it does not need would
# be worse than the failure it is warning about.
config_json="$STATE_DIR/platform/config.json"
config_field_note() {
    local field="$1" value="$2" consequence
    case "$field" in
        lmer_bin) consequence="every session spawn fails: spawn.resolve_lmer_bin returns this path verbatim and the daemon execs it (ENOENT)" ;;
        secret_file) consequence="config.ensure_secret finds no secret and mints a NEW one, so every client holding the old one is refused" ;;
        work_repo_mirror) consequence="the work-repo mirror is re-cloned into the container's ephemeral filesystem on every start" ;;
        *) consequence="the daemon reads a path that is not there" ;;
    esac
    note \
        "config.json sets $field=$value, which nothing here mounts" \
        "predicted: $consequence" \
        "fix it with --mount-rw $value (path-identical), or drop the field from" \
        "$config_json so the default under \$HOME/.lmer is used"
}
if [ -f "$config_json" ]; then
    config_fields=""
    if command -v python3 >/dev/null 2>&1; then
        config_fields="$(
            python3 - "$config_json" <<'PY' 2>/dev/null || true
import json
import sys

FIELDS = ("lmer_bin", "secret_file", "work_repo_mirror")
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, ValueError):
    raise SystemExit(0)
if not isinstance(data, dict):
    raise SystemExit(0)
for field in FIELDS:
    value = data.get(field)
    if isinstance(value, str) and value.strip():
        print(f"{field}\t{value.strip()}")
PY
        )"
    else
        # No python3: one line per field, values without JSON escapes only. A
        # miss here is a missing note, which is the same place this check started.
        config_fields="$(
            grep -oE '"(lmer_bin|secret_file|work_repo_mirror)"[[:space:]]*:[[:space:]]*"[^"\\]*"' \
                "$config_json" 2>/dev/null \
                | sed -E 's/"([a-z_]+)"[[:space:]]*:[[:space:]]*"(.*)"/\1\t\2/' || true
        )"
    fi
    while IFS="$(printf '\t')" read -r field value; do
        [ -n "${field:-}" ] && [ -n "${value:-}" ] || continue
        # A leading ~ is expanded for secret_file and work_repo_mirror
        # (PlatformConfig.secret_path/mirror_path call expanduser); lmer_bin is
        # exec'd as written, which is already broken on a bare host, so this
        # check stays out of that argument and just reports the path.
        value="$(expand_tilde "$value")"
        case "$value" in
            /*) ;;
            *) continue ;;  # relative: resolved against a cwd this cannot know
        esac
        path_is_mounted "$value" || config_field_note "$field" "$value"
    done <<EOF
$config_fields
EOF
fi

# --- environment forwarding --------------------------------------------------
#
# By name, never by value: `-e NAME` tells the runtime to copy this shell's
# value, so LMER_PLATFORM_SECRET never appears in an argument list, a `ps`
# output or the --print rendering.
forwarded=()
add_env() {
    local name="$1" seen
    for seen in ${forwarded[@]+"${forwarded[@]}"}; do
        [ "$seen" = "$name" ] && return 0
    done
    forwarded+=("$name")
    cmd+=("--env" "$name")
}

for name in $(compgen -e | sort || true); do
    case "$name" in
        # Host-specific target-repo tokens, plus the deprecated
        # GITLAB_TOKEN_worklog work-repo fallback: matched as a prefix because
        # the name carries the host and cannot be enumerated (tokens.get_token).
        GITLAB_TOKEN_*) add_env "$name"; continue ;;
        LMER_PLATFORM_*) ;;
        *) continue ;;
    esac
    blocked=0
    for skip in "${FORWARD_BLOCKED[@]}"; do
        if [ "$name" = "$skip" ]; then blocked=1; fi
    done
    if [ "$blocked" -eq 1 ]; then
        note \
            "not forwarding $name — it names a host directory holding the UI" \
            "and would shadow the bundle baked into the image"
        continue
    fi
    checked=0
    for candidate in "${FORWARD_PATH_CHECKED[@]}"; do
        if [ "$name" = "$candidate" ]; then checked=1; fi
    done
    if [ "$checked" -eq 1 ]; then
        # Read to decide, still forwarded by name. An absolute value no mount
        # covers is the same failure the config.json check above describes, so it
        # is refused rather than passed on: the state-dir default that takes over
        # is at least a path that exists in here and on the host.
        value="$(expand_tilde "${!name:-}")"
        case "$value" in
            /*)
                if ! path_is_mounted "$value"; then
                    note \
                        "not forwarding $name=$value — nothing here mounts that path" \
                        "in-container it would name an empty location, and the" \
                        "secret or mirror would be created there instead of on the" \
                        "host; mount it with --mount-rw to use it"
                    continue
                fi
                ;;
        esac
    fi
    add_env "$name"
done

for name in "${FORWARD_EXTRA[@]}"; do
    if [ -n "${!name:-}" ]; then
        add_env "$name"
    fi
done

# Asked for by name on the command line, so an unset one is worth saying: `-e
# NAME` with nothing behind it forwards nothing, and dropping it in silence looks
# exactly like a variable that arrived.
for name in ${EXTRA_ENV[@]+"${EXTRA_ENV[@]}"}; do
    if [ -n "${!name:-}" ]; then
        add_env "$name"
    else
        note "--env $name is not set in this shell — nothing to forward"
    fi
done

# Runtime flags last but still before the image, which is where docker and podman
# stop reading their own arguments.
cmd+=(${RUNTIME_ARGS[@]+"${RUNTIME_ARGS[@]}"})
cmd+=("$IMAGE")
# After the image: arguments for the ENTRYPOINT, i.e. `lmer platform <these>`.
cmd+=(${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"})

if [ "$PRINT_ONLY" -eq 1 ]; then
    printf '%q ' "${cmd[@]}"
    printf '\n'
    exit 0
fi

"${cmd[@]}"

if [ "$DETACH" -eq 1 ]; then
    note \
        "started '$NAME' from $IMAGE" \
        "logs:    $RUNTIME logs -f $NAME" \
        "secret:  $RUNTIME exec $NAME lmer platform secret" \
        "restart: $RUNTIME restart $NAME" \
        "         a restart leaves every session read-only today — see" \
        "         'Restart semantics' in docs/PLATFORM-CONTAINER.md"
fi
