#!/usr/bin/env bash
# Host-side verification for issue #351 (keep-id chown startup stall).
#
# Run this ON THE HOST (not inside a container) after updating lmer. It
# verifies, with timings, that the three #351 changes behave as intended:
#
#   1. tag        — the developer-mode image tag is an input hash
#                   (lmer:dev-<12 hex>) and is stable across a commit that
#                   touches only source (no declared build input), so a
#                   source-only commit reuses the existing image and its
#                   warmed keep-id layer.
#   2. warm start — a keep-id run of the current image is sub-second once
#                   warmed. If this step takes minutes, the image was cold
#                   and this run just warmed it (that IS the remap cost;
#                   re-run to see the warm number).
#   3. root start — (optional, --root) a --user 0:0 run without keep-id is
#                   sub-second even on a cold image.
#   4. measure    — (optional, --measure-build) the before/after evidence for
#                   the pre-warm: force a real rebuild, time it, then time the
#                   first keep-id start of the image it produced. Before the
#                   pre-warm the build is quick and the first start takes
#                   minutes; after it, the minutes are inside the build and the
#                   first start is ~1 s. Destructive in one way only: it deletes
#                   and rebuilds the developer image — so it refuses to start
#                   unless it can name that image up front (a resolvable tag,
#                   and an --image that agrees with it).
#
# Expected numbers (operator's measurements, napkin lmer/keep-id-chown.md,
# 2026-08-22, 4.30 GB / 86.5k-inode image):
#   cold keep-id run:   ~7 minutes
#   warm keep-id run:   ~1 s
#   root (no keep-id):  ~0.8 s
#
# Host requirements for the shipped fix: NONE. No podman version floor
# beyond what already runs lmer, no storage.conf or containers.conf
# changes. The kernel-level fix (idmapped mounts) is separate and
# unavailable on overlay/xfs with kernel 5.14 — this script reports the
# current support status so a future host upgrade is noticed.
#
# The tag checks import lmer_cli, so they need the interpreter lmer is
# installed into — NOT necessarily the system python3 (a `uv tool install`
# puts lmer in its own venv). That interpreter is read from the shebang of
# the `lmer` entry point on PATH; --python overrides it.
#
# Usage: verify-keep-id-chown.sh [--image <image>] [--root] [--skip-timing]
#                                [--python <interpreter>] [--measure-build]

set -euo pipefail

IMAGE=""
DO_ROOT=0
SKIP_TIMING=0
PYTHON=""
MEASURE_BUILD=0
while [ $# -gt 0 ]; do
    case "$1" in
        --image) IMAGE="$2"; shift 2 ;;
        --root) DO_ROOT=1; shift ;;
        --skip-timing) SKIP_TIMING=1; shift ;;
        --python) PYTHON="$2"; shift 2 ;;
        --measure-build) MEASURE_BUILD=1; shift ;;
        -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if ! command -v podman >/dev/null 2>&1; then
    echo "podman not found — this verification is podman-specific" >&2
    exit 2
fi

# Interpreter that can import lmer_cli. lmer is commonly installed with
# `uv tool install`, which puts it in a private venv the system python3
# knows nothing about — so ask the `lmer` entry point itself which
# interpreter runs it rather than assuming python3 (#351 host test).
LMER_BIN="$(command -v lmer 2>/dev/null || true)"
PY_BIN=""
PY_ORIGIN=""
if [ -n "$PYTHON" ]; then
    PY_BIN="$PYTHON"
    PY_ORIGIN="--python"
elif [ -n "$LMER_BIN" ]; then
    SHEBANG="$(head -1 "$LMER_BIN" 2>/dev/null || true)"
    case "$SHEBANG" in
        '#!'*)
            # shellcheck disable=SC2086 # word splitting is the parse here
            set -- ${SHEBANG#\#!}
            # `#!/usr/bin/env python3` names the interpreter in argv[1].
            if [ $# -gt 1 ] && [ "$(basename "$1")" = "env" ]; then
                shift
                # `env -S python3 -u` style: the interpreter is the first
                # non-option word after env.
                while [ $# -gt 1 ] && [ "${1#-}" != "$1" ]; do shift; done
            fi
            PY_BIN="${1:-}"
            PY_ORIGIN="shebang of $LMER_BIN"
            ;;
    esac
fi
if [ -z "$PY_BIN" ]; then
    PY_BIN="python3"
    PY_ORIGIN="fallback (no interpreter resolved from an lmer entry point)"
fi

PY_OK=0
if "$PY_BIN" -c "import lmer_cli" >/dev/null 2>&1; then
    PY_OK=1
fi

echo "== idmapped-mount support (informational) =="
# "Supports shifting: true" would make the whole chown problem disappear at
# mount time; report it so a kernel/storage upgrade is noticed.
podman info 2>/dev/null | grep -iE "supports shifting|using metacopy|graphDriverName" || true
echo

echo "== 1. developer-mode tag =="
TAG_PY='
import sys
from pathlib import Path
from lmer_cli.build import resolve_image_tag
root = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if root is None:
    from lmer_cli.runtime import repo_root_path
    root = repo_root_path()
print(resolve_image_tag(root) or "")
'
TAG=""
REPO_ROOT=""
VERIFIED=0
if [ "$PY_OK" = 0 ]; then
    echo "cannot import lmer_cli with $PY_BIN [$PY_ORIGIN] — skipping the tag checks" >&2
    echo "lmer itself is fine; this is the wrong interpreter for it. A" >&2
    echo "\`uv tool install\` keeps lmer in its own venv, e.g." >&2
    echo "  ~/.local/share/uv/tools/lmer/bin/python" >&2
    echo "Re-run with --python <interpreter> to name it explicitly." >&2
else
    echo "interpreter: $PY_BIN [$PY_ORIGIN]"
    TAG="$("$PY_BIN" -c "$TAG_PY" || true)"
    if [ -z "$TAG" ]; then
        echo "could not resolve a tag with $PY_BIN — not a developer checkout?" >&2
    else
        echo "resolved tag: $TAG"
        VERIFIED=1
        case "$TAG" in
            *:dev-*) echo "OK: input-hash developer tag" ;;
            *) echo "NOTE: not an input-hash tag — installed/registry mode, or an old lmer" ;;
        esac
    fi
    REPO_ROOT="$("$PY_BIN" -c 'from lmer_cli.runtime import repo_root_path; print(repo_root_path() or "")' || true)"
fi

if [ -n "$REPO_ROOT" ] && [ -d "$REPO_ROOT/.git" ]; then
    echo "-- tag stability across a source-only commit (throwaway worktree) --"
    WT="$(mktemp -d)"
    trap 'git -C "$REPO_ROOT" worktree remove --force "$WT" >/dev/null 2>&1 || true; rm -rf "$WT"' EXIT
    if ! git -C "$REPO_ROOT" worktree add --detach "$WT" >/dev/null; then
        echo "could not create a throwaway worktree — skipping the stability check" >&2
    else
        # A real source change, not --allow-empty: an empty commit alters no
        # file at all, so even a hash that keyed off file mtimes or the whole
        # tree would pass it. src/ is deliberately outside
        # DEV_IMAGE_BUILD_INPUTS — it reaches the container by bind mount —
        # so touching it is exactly the case the input-hash tag exists for.
        PROBE="$(git -C "$WT" ls-files 'src/*' | head -1)"
        if [ -z "$PROBE" ]; then
            echo "no tracked src/ file to modify — skipping the stability check" >&2
        else
            BEFORE="$("$PY_BIN" -c "$TAG_PY" "$WT")"
            printf '\n# touched by verify-keep-id-chown.sh (source-only change)\n' >> "$WT/$PROBE"
            # -n: worktrees share .git/hooks with the checkout, and the
            # project tells developers to install pre-commit. A hook failure
            # (a cold hook cache with no network is enough) would abort this
            # script under `set -e` for a reason unrelated to what it
            # verifies — the tag hash does not care whether hooks ran.
            git -C "$WT" -c user.name=verify -c user.email=verify@localhost \
                commit -qn -am "source-only change ($PROBE)"
            AFTER="$("$PY_BIN" -c "$TAG_PY" "$WT")"
            if [ "$BEFORE" = "$AFTER" ]; then
                echo "OK: tag unchanged across a source-only commit to $PROBE ($BEFORE)"
            else
                echo "FAIL: tag changed with no build-input change: $BEFORE -> $AFTER" >&2
                exit 1
            fi
        fi
    fi
fi
echo

# Before any image lookup: --skip-timing asks for the tag checks alone, which
# is the only verification available before the first build (#351 host
# test — this used to sit below `podman image exists` and exit 2 there).
if [ "$SKIP_TIMING" = 1 ]; then
    if [ "$VERIFIED" = 0 ]; then
        # The tag checks are all this mode does, so a run that skipped them
        # verified nothing — say so in the exit status, not just on stderr,
        # for anyone wrapping this in CI.
        echo "(timings skipped) — and the tag checks did not run, so nothing was verified" >&2
        exit 2
    fi
    echo "(timings skipped)"
    exit 0
fi

# The command lmer's own pre-warm runs (lmer_cli.keep_id.prewarm): overriding
# the entrypoint keeps the number about the remap rather than about mise.
keep_id_start() {
    podman run --rm --userns=keep-id --network=none \
        --entrypoint /usr/bin/true "$1"
}

marker_state() {
    local id
    id="$(podman image inspect --format '{{.Id}}' "$1" 2>/dev/null || true)"
    if [ -z "$id" ]; then
        echo "marker: image id unavailable"
    elif [ -e "$HOME/.lmer/remapped/${id//\//_}" ]; then
        echo "marker: present for ${id:0:19} (a spawn will be silent)"
    else
        echo "marker: absent for ${id:0:19} (a spawn will warn)"
    fi
}

if [ "$MEASURE_BUILD" = 1 ]; then
    echo "== 4. build + first keep-id start (before/after evidence, #351) =="
    if ! command -v lmer >/dev/null 2>&1; then
        echo "lmer not on PATH — cannot time a build" >&2
        exit 2
    fi
    # Settle WHICH image is measured before the destructive rebuild, not
    # after it. `lmer build --force` builds whatever resolve_image_tag gives
    # it, so a --image naming anything else would delete and rebuild one image
    # and then time a start of another — and if that other one is already warm
    # the run prints ~1 s and reads it back as success (!309 review).
    if [ -z "$TAG" ]; then
        echo "cannot tell which image \`lmer build\` will produce: the tag checks did not run" >&2
        echo "(re-run with --python <interpreter>). Refusing to rebuild and measure blind." >&2
        exit 2
    fi
    # $TAG is resolve_image_tag alone, which is NOT the whole of what
    # `lmer build` targets: it honours $LMER_IMAGE first, and it builds with
    # whichever runtime detect_runtime() picks — docker before podman when
    # both are installed. Timing a podman start of a tag that docker built,
    # or of the input-hash tag when LMER_IMAGE redirected the build, produces
    # a green number about an image the build never touched — and a
    # pre-existing warm image supplies exactly such a number
    # (!309 adversarial review, F7). So resolve the *effective* target the
    # way the build does, and refuse rather than measure across stores.
    RUNTIME_PY='from lmer_cli.runtime import detect_runtime; print(detect_runtime())'
    IMAGE_PY='
import os
from lmer_cli.build import resolve_image_tag
from lmer_cli.runtime import repo_root_path
print(os.environ.get("LMER_IMAGE") or resolve_image_tag(repo_root_path()) or "")
'
    BUILD_RUNTIME="$("$PY_BIN" -c "$RUNTIME_PY" 2>/dev/null || true)"
    BUILD_IMAGE="$("$PY_BIN" -c "$IMAGE_PY" 2>/dev/null || true)"
    if [ -z "$BUILD_RUNTIME" ]; then
        echo "cannot tell which container runtime \`lmer build\` will use." >&2
        echo "Refusing to rebuild and measure blind." >&2
        exit 2
    fi
    if [ "$BUILD_RUNTIME" != "podman" ]; then
        echo "\`lmer build\` will build with $BUILD_RUNTIME, but this script times podman." >&2
        echo "Separate image stores: the timing would be about an image this build" >&2
        echo "never touched. Make podman the runtime lmer selects, then re-run." >&2
        exit 2
    fi
    if [ -z "$BUILD_IMAGE" ]; then
        echo "cannot tell which image \`lmer build\` will produce. Refusing to measure blind." >&2
        exit 2
    fi
    if [ -n "$IMAGE" ] && [ "$IMAGE" != "$BUILD_IMAGE" ]; then
        echo "--image $IMAGE is not the image \`lmer build\` will produce ($BUILD_IMAGE)." >&2
        echo "The timing would be about an image this build never touched." >&2
        exit 2
    fi
    MEASURED_IMAGE="$BUILD_IMAGE"
    echo "measuring: $MEASURED_IMAGE"
    marker_state "$MEASURED_IMAGE"
    echo "-- forcing a real rebuild so the image is genuinely cold --"
    time lmer build --force
    marker_state "$MEASURED_IMAGE"
    echo "-- first keep-id start of $MEASURED_IMAGE --"
    time keep_id_start "$MEASURED_IMAGE"
    marker_state "$MEASURED_IMAGE"
    echo
    echo "Read it as: minutes in the build + ~1 s in the first start = pre-warm"
    echo "working; a quick build + minutes in the first start = the pre-warm did"
    echo "not run (check LMER_NO_PREWARM and the build output)."
    exit 0
fi

IMAGE="${IMAGE:-$TAG}"
if [ -z "$IMAGE" ]; then
    echo "no image to time (pass --image <image>, or --skip-timing for the tag checks alone)" >&2
    exit 2
fi
if ! podman image exists "$IMAGE"; then
    echo "image $IMAGE not present locally — build it first (lmer build), then re-run" >&2
    exit 2
fi

echo "== 2. keep-id start of $IMAGE =="
echo "(minutes here = the image was cold; that is the remap being paid + cached. Re-run for the warm number, expected ~1 s)"
time podman run --rm --userns=keep-id "$IMAGE" true
echo

if [ "$DO_ROOT" = 1 ]; then
    echo "== 3. root start (no keep-id) of $IMAGE =="
    echo "(expected ~0.8 s even on a cold image)"
    time podman run --rm --user 0:0 "$IMAGE" true
fi

echo "done."
