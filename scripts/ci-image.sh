#!/bin/sh
# Carry a container image from the build stage to the publish stage by digest.
#
# The tag pipeline used to build every image twice — once in the build stage,
# which is the only check there is on a Containerfile, and once again in the
# publish stage, which is the copy that got pushed (#189). Nothing shares a
# layer cache between those two jobs: each declares its own `docker:dind`
# service and every dind starts with an empty /var/lib/docker, so the second
# build resolved everything from scratch. With `docker-ce-cli` deliberately
# unpinned and npm optional dependencies resolving per build, the artifact
# under `:<tag>` was not the artifact anybody had exercised.
#
# So the build stage now pushes the commit-tagged image and records what the
# registry stored it as, and the publish stage promotes *that digest* to the
# release tags. Two subcommands:
#
#   digest <repo> <tag> <VAR>       print `VAR=sha256:…` as a GitLab dotenv line
#   promote <repo> <digest> <tag>…  pull <repo>@<digest>, re-tag, push, verify
#
# Both read digests out of the daemon's own RepoDigests bookkeeping rather than
# scraping `docker push` output: RepoDigests is what docker recorded the
# registry as having accepted, while the output line is a rendering that
# nothing promises to keep.
#
# POSIX sh — this runs in the `docker:24.0` job image, whose shell is BusyBox.
set -eu

usage() {
    cat >&2 <<'EOF'
usage: ci-image.sh digest <repo> <tag> <VAR_NAME>
       ci-image.sh promote <repo> <digest> <tag> [<tag>...]
EOF
    exit 2
}

# Every manifest digest the local daemon holds for <repo>, one per line, sorted
# and de-duplicated, with the `<repo>@` prefix stripped.
#
# Matched as a literal prefix (awk index(), not a regex) because a repository
# path is full of characters a regex would read as syntax, and filtered by
# repository because RepoDigests is a property of the image and an image tagged
# into two repositories lists both. The positional `{{index .RepoDigests 0}}`
# would pick between them by luck.
repo_digests() {
    _rd_repo=$1
    _rd_ref=$2
    docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$_rd_ref" \
        | awk -v prefix="${_rd_repo}@" 'index($0, prefix) == 1 { print substr($0, length(prefix) + 1) }' \
        | sort -u
}

# The one digest <ref> resolves to, or a failure naming what was found instead.
#
# Zero means the image was never pushed (an image that has only ever been built
# has no RepoDigests at all), and more than one means the daemon holds the same
# image under two manifests — which is the divergence this whole script exists
# to catch, so it is an error rather than a pick.
require_single_digest() {
    _sd_repo=$1
    _sd_ref=$2
    _sd_found=$(repo_digests "$_sd_repo" "$_sd_ref")
    if [ -z "$_sd_found" ]; then
        echo "ci-image.sh: $_sd_ref has no registry digest for $_sd_repo — was it pushed?" >&2
        return 1
    fi
    if [ "$(printf '%s\n' "$_sd_found" | wc -l)" -ne 1 ]; then
        echo "ci-image.sh: $_sd_ref resolves to more than one digest for $_sd_repo:" >&2
        printf '  %s\n' $_sd_found >&2
        return 1
    fi
    printf '%s\n' "$_sd_found"
}

# A digest we were handed is a value from outside this script — an unset dotenv
# variable arrives as the empty string and would otherwise be pulled as the
# bare repository, i.e. `:latest`, which is the wrong image published silently.
require_digest_format() {
    case "$1" in
        sha256:*)
            if [ "${#1}" -eq 71 ] && [ -z "$(printf '%s' "${1#sha256:}" | tr -d '0-9a-f')" ]; then
                return 0
            fi
            ;;
    esac
    echo "ci-image.sh: '$1' is not a sha256 digest (empty means the build stage's dotenv did not reach this job)" >&2
    return 1
}

cmd_digest() {
    [ $# -eq 3 ] || usage
    _repo=$1
    _tag=$2
    _var=$3
    _digest=$(require_single_digest "$_repo" "${_repo}:${_tag}")
    echo "ci-image.sh: ${_repo}:${_tag} was stored as ${_digest}" >&2
    printf '%s=%s\n' "$_var" "$_digest"
}

cmd_promote() {
    [ $# -ge 3 ] || usage
    _repo=$1
    _digest=$2
    shift 2
    require_digest_format "$_digest"

    # By digest, so what gets published is the build stage's bytes and not
    # whatever the mutable commit tag points at by the time this job runs.
    echo "ci-image.sh: pulling ${_repo}@${_digest} (built and verified in the build stage)"
    docker pull "${_repo}@${_digest}"

    for _tag in "$@"; do
        docker tag "${_repo}@${_digest}" "${_repo}:${_tag}"
        docker push "${_repo}:${_tag}"
    done

    # Re-tagging cannot change an image's content, but pushing writes a fresh
    # manifest, and no published contract says the daemon regenerates one byte
    # for byte. Rather than assume it, ask the registry what each release tag
    # now resolves to: the pull re-reads the tag (the layers are already local,
    # so it costs a manifest request) and the digest it records has to be the
    # one the build stage recorded. Deliberately a re-read *after* the push and
    # not the value computed on the way in — a check that compares something to
    # itself proves nothing — and a hard failure rather than a warning, because
    # the only thing downstream of it is the release.
    #
    # Nothing above assumes this job runs once. Pull-by-digest, tag and push are
    # each idempotent, so a publish job re-run after a partial failure redoes the
    # whole promotion safely rather than resuming into a half-tagged state.
    for _tag in "$@"; do
        docker pull --quiet "${_repo}:${_tag}" >/dev/null
        _published=$(require_single_digest "$_repo" "${_repo}:${_tag}")
        if [ "$_published" != "$_digest" ]; then
            echo "ci-image.sh: FAIL ${_repo}:${_tag} published as ${_published}, built ${_digest}" >&2
            return 1
        fi
        echo "ci-image.sh: OK ${_repo}:${_tag}  built=${_digest}  published=${_published}"
    done
}

[ $# -ge 1 ] || usage
_cmd=$1
shift
case "$_cmd" in
    digest) cmd_digest "$@" ;;
    promote) cmd_promote "$@" ;;
    *) usage ;;
esac
