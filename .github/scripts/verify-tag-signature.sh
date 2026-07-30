#!/usr/bin/env bash
# verify-tag-signature.sh — publish-gate tag verification for release.yml.
#
# Verifies that the pushed release tag is an SSH-signed tag object
# (this project signs release tags with `git tag -s` under gpg.format=ssh)
# whose signer is in the admin-controlled allowlist, and that the tag's
# target commit equals the head of GitHub `main` as reported by the REST
# API. Trust anchors are deliberately kept OUT of repo content: a tag push
# runs the workflow from the tag's own tree, so nothing read from the
# checkout (allowed_signers files, branch pointers) can be trusted.
#
# Environment seams (all inputs; no positional arguments):
#   RELEASE_ALLOWED_SIGNERS  required. Contents of an ssh allowed-signers
#                            file ("principal key-type key ..." lines). The
#                            workflow populates this from the admin-managed
#                            vars.RELEASE_ALLOWED_SIGNERS Actions repository
#                            variable — never from a checked-in file.
#   GITHUB_REF_NAME          required. The pushed tag name (e.g. v0.5.0).
#   GITHUB_REPOSITORY        required. Repo slug, e.g. lmer2/lmer.
#   GITHUB_TOKEN             optional. Bearer token for the API call; when
#                            empty the request is sent unauthenticated
#                            (lets tests stub the API without a token).
#   GITHUB_API_URL           optional. API base, default
#                            https://api.github.com. Tests may point this
#                            at a local HTTP stub or a file:// tree that
#                            serves repos/<slug>/commits/heads/main.
#
# Exit status: 0 only when every check passes; any failure emits a GitHub
# Actions ::error:: annotation and exits nonzero (fail closed).

set -euo pipefail

err() {
  echo "::error::$*" >&2
  exit 1
}

: "${GITHUB_API_URL:=https://api.github.com}"

[ -n "${RELEASE_ALLOWED_SIGNERS:-}" ] ||
  err "RELEASE_ALLOWED_SIGNERS is empty or unset; refusing to verify (set the admin-controlled Actions repository variable)"
[ -n "${GITHUB_REF_NAME:-}" ] ||
  err "GITHUB_REF_NAME is empty or unset; expected the pushed tag name"
[ -n "${GITHUB_REPOSITORY:-}" ] ||
  err "GITHUB_REPOSITORY is empty or unset; expected an owner/repo slug"

tag="${GITHUB_REF_NAME}"

# Allowed-signers material lives outside the checkout so tag-borne repo
# content can never influence it.
signers_dir="$(mktemp -d)"
trap 'rm -rf "${signers_dir}"' EXIT
signers_file="${signers_dir}/allowed_signers"
printf '%s\n' "${RELEASE_ALLOWED_SIGNERS}" > "${signers_file}"

git rev-parse --verify --quiet "refs/tags/${tag}" > /dev/null ||
  err "tag '${tag}' not found in the checkout (fetch tags before verifying)"

# `git tag -v` fails for lightweight tags, missing/invalid signatures, and
# signers absent from the allowed-signers file — all of which must fail
# the gate.
if ! git -c gpg.format=ssh \
    -c gpg.ssh.allowedSignersFile="${signers_file}" \
    tag -v "${tag}"; then
  err "signature verification failed for tag '${tag}' (unsigned, invalid, or signer not in RELEASE_ALLOWED_SIGNERS)"
fi

tag_commit="$(git rev-parse "refs/tags/${tag}^{commit}")"

# Pin what was VERIFIED to what gets BUILT and PUBLISHED. The downstream
# build/publish jobs check out ${GITHUB_SHA} (the commit the tag resolved
# to when the push event fired); this job re-reads the tag at its own
# runtime. A tag re-pointed in between would split the two — this job
# would verify one commit while another is published. Requires tag-write
# on the mirror, which RELEASE-FLOW.md books as an accepted PAT residual,
# so this is hardening rather than a hole; it is also one comparison.
[ -n "${GITHUB_SHA:-}" ] ||
  err "GITHUB_SHA is empty or unset; expected the commit that triggered the run"
if [ "${tag_commit}" != "${GITHUB_SHA}" ]; then
  err "tag '${tag}' now points at ${tag_commit} but the run was triggered for ${GITHUB_SHA}; the tag moved after the push event — refusing to publish"
fi

# Resolve main HEAD from the API — never from a branch pointer in the
# tag's own tree.
api_url="${GITHUB_API_URL}/repos/${GITHUB_REPOSITORY}/commits/heads/main"
curl_args=(--fail --silent --show-error --location
  --header "Accept: application/vnd.github+json")
if [ -n "${GITHUB_TOKEN:-}" ]; then
  curl_args+=(--header "Authorization: Bearer ${GITHUB_TOKEN}")
fi
if ! response="$(curl "${curl_args[@]}" "${api_url}")"; then
  err "failed to resolve GitHub main HEAD from ${api_url}"
fi

main_sha="$(jq -er '.sha' <<< "${response}")" ||
  err "API response from ${api_url} has no .sha field"

if [ "${tag_commit}" != "${main_sha}" ]; then
  err "tag '${tag}' points at ${tag_commit} but GitHub main HEAD is ${main_sha}; refusing to publish"
fi

echo "Tag '${tag}' verified: SSH signature valid, signer allowlisted, commit ${tag_commit} matches GitHub main HEAD"
