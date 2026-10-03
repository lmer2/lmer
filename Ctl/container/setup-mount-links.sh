#!/bin/bash
# Symlink each `declared:staged` pair in LMER_MOUNT_LINKS (#293/#290). The
# host stages user-harness mounts under ~/.lmer-mounts because the runtime
# creates a destination's missing parents root-owned; making the symlink HERE,
# as `developer`, is what leaves the declared path's parent chain
# developer-owned. Fail-soft throughout: one warning line per problem,
# remaining pairs still processed, exit always 0 — a lost link costs one
# harness its mount, a non-zero exit would cost the session. Nothing is ever
# deleted except a symlink this mechanism owns and an empty directory (rmdir).

set -u

links="${LMER_MOUNT_LINKS:-}"
if [ -z "$links" ]; then
    exit 0
fi

warn() {
    echo "⚠️  Mount link '$1': $2" >&2
}

# Where this script records what it did, inside the staged directory itself —
# the one place both sides can see (#383 item 2). Read back by
# lmer_cli.mounts.read_link_marker, which owns the field grammar.
marker=".lmer-mount-link"

# Staged directories an earlier pair in THIS run already linked. The record is
# keyed by the *staged* directory while its verdict is about one *declared*
# path, and several declared paths legitimately share one staged directory
# (`memory_links()` returns one pair per harness, all pointing at one store) —
# so a later obstructed path must not report a store dead while it is live.
# Only successes are remembered; a failure may still be overwritten by one.
staged_linked=()

# mark <declared> <staged> <yes|no> <detail>
#
# Only into a staged *directory*: a staged credential is a single file, and a
# staged path that does not exist is an outcome the host can already see.
mark() {
    [ -d "$2" ] || return 0
    for seen in ${staged_linked[@]+"${staged_linked[@]}"}; do
        if [ "$seen" = "$2" ]; then
            return 0
        fi
    done
    if [ "$3" = "yes" ]; then
        staged_linked+=("$2")
    fi
    if ! {
        printf 'schema=1\n'
        printf 'declared=%s\n' "$1"
        printf 'staged=%s\n' "$2"
        printf 'linked=%s\n' "$3"
        printf 'detail=%s\n' "$4"
        printf 'at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$2/$marker" 2>/dev/null; then
        warn "$1:$2" "could not record the link state in the staged store"
    fi
}

# Declared paths a link is already in place for: duplicates are first-wins
# with a warning (a silent last-wins would repoint a correct link). A pair
# that was *skipped* claims nothing, so a later pair for the same declared
# path still gets its chance.
declared_seen=()

# The path grammars on the host refuse ':', ',' and whitespace, so splitting on
# them here recovers exactly the pairs that were encoded.
IFS=',' read -r -a pairs <<< "$links"
for pair in "${pairs[@]}"; do
    [ -n "$pair" ] || continue
    # One colon, two absolute paths, or the pair is not one this mechanism
    # wrote. The two-colon case is tested first, so the `*` in the second
    # pattern cannot absorb a colon.
    case "$pair" in
        *:*:*)
            warn "$pair" "not a 'declared:staged' pair of absolute paths — skipped"
            continue
            ;;
        /*:/*) ;;
        *)
            warn "$pair" "not a 'declared:staged' pair of absolute paths — skipped"
            continue
            ;;
    esac
    declared="${pair%%:*}"
    staged="${pair#*:}"
    duplicate=""
    for seen in ${declared_seen[@]+"${declared_seen[@]}"}; do
        if [ "$seen" = "$declared" ]; then
            duplicate="yes"
            break
        fi
    done
    if [ -n "$duplicate" ]; then
        warn "$pair" "declared path already linked — skipped"
        mark "$declared" "$staged" no "declared path already linked by an earlier pair"
        continue
    fi
    if [ ! -e "$staged" ]; then
        warn "$pair" "the staged path does not exist — skipped (nothing was mounted there)"
        continue
    fi
    # -L before -d: a symlink to a directory answers to both, and the symlink
    # case is the idempotent re-run this script must survive.
    if [ -L "$declared" ]; then
        if [ "$(readlink "$declared")" = "$staged" ]; then
            declared_seen+=("$declared")
            mark "$declared" "$staged" yes "already linked"
            continue
        fi
        if ! rm -f "$declared"; then
            warn "$pair" "cannot replace the existing symlink — skipped"
            mark "$declared" "$staged" no "cannot replace the existing symlink"
            continue
        fi
    elif [ -d "$declared" ]; then
        # rmdir, never rm -r: it succeeds only on an empty directory, so a
        # directory holding anything at all (or a mountpoint, EBUSY) keeps
        # what it has and this pair is skipped.
        if ! rmdir "$declared" 2>/dev/null; then
            warn "$pair" "the declared path is a non-empty directory or a mountpoint — skipped"
            mark "$declared" "$staged" no "the declared path is a non-empty directory or a mountpoint"
            continue
        fi
    elif [ -e "$declared" ]; then
        warn "$pair" "the declared path already exists and is not a directory — skipped"
        mark "$declared" "$staged" no "the declared path already exists and is not a directory"
        continue
    fi
    # 2>/dev/null so a refused parent is exactly one ⚠️ line, not mkdir's
    # message and then ours.
    if ! mkdir -p "$(dirname "$declared")" 2>/dev/null; then
        warn "$pair" "cannot create the parent directory — skipped"
        mark "$declared" "$staged" no "cannot create the parent directory"
        continue
    fi
    if ! ln -s "$staged" "$declared"; then
        warn "$pair" "cannot create the symlink — skipped"
        mark "$declared" "$staged" no "cannot create the symlink"
        continue
    fi
    declared_seen+=("$declared")
    mark "$declared" "$staged" yes "linked"
    echo "🔗 Linked $declared → $staged"
done

exit 0
