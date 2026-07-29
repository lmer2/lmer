#!/bin/bash
# stand-up.sh — create-or-verify the release rehearsal rig.
#
# The frozen design is README.md in this directory; this script implements
# it and must not deviate without updating it first. It is idempotent:
# re-running against an existing rig converges instead of failing.
#
# Modes (mutually exclusive; --dry-run modifies any of them):
#   (default)     create-or-verify every rig piece
#   --check       verify-only; mutates nothing; exit non-zero on any gap
#   --teardown    delete the scratch repo + throwaway key; print manual residue
#   --dry-run     print the plan for the selected mode; NO network calls
#
# Parameters come from rig.env (see rig.env.example) and may be overridden:
#   --repo <owner/name>   override LMER_REHEARSAL_REPO
#   --project <name>      override LMER_REHEARSAL_PROJECT
#   --env-file <path>     rig.env location (default: alongside this script)
#
# Safety, in order, before anything else runs:
#   1. Production-target guard (offline, lib.sh): refuses lmer2/lmer, any
#      repo/project named lmer, pypi.org, and any environment carrying the
#      production release PAT or release signing key. Runs in EVERY mode,
#      including --dry-run, before any network call.
#   2. R14 skip-clean: every rig-touching mode (default, --check,
#      --teardown) exits 0 with a clear notice when the LMER_REHEARSAL_*
#      credential variables are absent, so this script verifies in a
#      sandbox. --dry-run needs no credentials (it never touches the rig).
#
# Automation boundaries (documented per the README's design):
#   - GitHub pieces are fully automated via the REST API: scratch repo,
#     main-branch and v* tag rulesets (bot bypass via the repository admin
#     role — the bot owns the scratch repo), PR/issue posture, the
#     RELEASE_ALLOWED_SIGNERS Actions variable, the deploy environment and
#     its tag-pattern deployment policy, the throwaway signing key, and
#     the rig repo's contents (derived release workflow, tag-verification
#     script, minimal reusable checks workflow, minimal buildable
#     pyproject.toml with project.name set to the rehearsal project).
#   - TestPyPI has no complete management API: the project materializes on
#     first upload (or is created manually), and trusted-publisher (OIDC)
#     registration has no public API at all. Where a step cannot be
#     automated this script prints the exact manual step with the exact
#     values, and verifies what the read-only JSON API exposes (project
#     existence).

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

usage() {
    cat <<'EOF'
usage: stand-up.sh [--check | --teardown] [--dry-run]
                   [--repo <owner/name>] [--project <name>]
                   [--env-file <path>] [--help]

Create-or-verify the release rehearsal rig (Ctl/rehearsal/README.md).

  (default)    create-or-verify scratch repo, rulesets, environment,
               RELEASE_ALLOWED_SIGNERS variable, throwaway key, TestPyPI
  --check      verify-only; exit non-zero on any gap
  --teardown   delete scratch repo + throwaway key; print manual residue
  --dry-run    print the plan; no network calls
  --repo       override LMER_REHEARSAL_REPO
  --project    override LMER_REHEARSAL_PROJECT
  --env-file   rig.env path (default: Ctl/rehearsal/rig.env)

Without the LMER_REHEARSAL_* credential variables, rig-touching modes
SKIP-CLEAN (exit 0 with a notice). The production-target guard always
runs, offline, before anything else.
EOF
}

MODE=standup
DRY_RUN=0
REPO_OVERRIDE=""
PROJECT_OVERRIDE=""
ENV_FILE=""

while (($#)); do
    case "$1" in
        --check)
            MODE=check
            ;;
        --teardown)
            MODE=teardown
            ;;
        --dry-run)
            DRY_RUN=1
            ;;
        --repo)
            if [[ $# -lt 2 ]]; then
                rehearsal_err "stand-up.sh: --repo requires a value"
                usage >&2
                exit 2
            fi
            REPO_OVERRIDE=$2
            shift
            ;;
        --project)
            if [[ $# -lt 2 ]]; then
                rehearsal_err "stand-up.sh: --project requires a value"
                usage >&2
                exit 2
            fi
            PROJECT_OVERRIDE=$2
            shift
            ;;
        --env-file)
            if [[ $# -lt 2 ]]; then
                rehearsal_err "stand-up.sh: --env-file requires a value"
                usage >&2
                exit 2
            fi
            ENV_FILE=$2
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            rehearsal_err "stand-up.sh: unknown argument '$1'"
            usage >&2
            exit 2
            ;;
    esac
    shift
done

# All rig identity lives in rig.env (README: Teardown and re-standup).
ENV_FILE=${ENV_FILE:-${SCRIPT_DIR}/rig.env}
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi
if [[ -n "$REPO_OVERRIDE" ]]; then
    LMER_REHEARSAL_REPO=$REPO_OVERRIDE
fi
if [[ -n "$PROJECT_OVERRIDE" ]]; then
    LMER_REHEARSAL_PROJECT=$PROJECT_OVERRIDE
fi

REPO=${LMER_REHEARSAL_REPO:-}
PROJECT=${LMER_REHEARSAL_PROJECT:-}
RIG_ENVIRONMENT=${LMER_REHEARSAL_ENVIRONMENT:-testpypi}

# --- Hard guard: offline, every mode, before any network call. -------------
rehearsal_guard "$REPO" "$PROJECT" "$REHEARSAL_TESTPYPI_URL"

# --- R14 skip-clean for rig-touching modes. ---------------------------------
if (( ! DRY_RUN )); then
    missing=$(rehearsal_missing_env)
    if [[ -n "$missing" ]]; then
        # shellcheck disable=SC2086
        rehearsal_skip_clean "$MODE" $missing
        exit 0
    fi
fi

BOT=${REPO%%/*}
REPO_NAME=${REPO##*/}
KEY_PATH=${LMER_REHEARSAL_SIGNING_KEY:-}

MAIN_RULESET_NAME="rehearsal-main-protection"
TAG_RULESET_NAME="rehearsal-tag-protection"

# ---------------------------------------------------------------------------
# Dry run: the plan, nothing else. No credentials required, no network.
# ---------------------------------------------------------------------------

print_manual_testpypi_steps() {
    cat <<EOF
Manual TestPyPI steps (no API exists for these — print-and-verify only):
  1. Trusted publisher: as the rig account, open
       ${REHEARSAL_TESTPYPI_URL}/manage/account/publishing/
     and add a pending publisher (or, if the project '${PROJECT:-<project>}'
     already exists, its Settings -> Publishing page) with exactly:
       PyPI project name : ${PROJECT:-<LMER_REHEARSAL_PROJECT>}
       Owner             : ${BOT:-<bot>}
       Repository name   : ${REPO_NAME:-lmer-rehearsal}
       Workflow name     : release.yml   (path: ${REHEARSAL_WORKFLOW_PATH})
       Environment name  : ${RIG_ENVIRONMENT}
  2. The project itself materializes on the first trusted-publisher upload;
     no separate creation step is needed once the pending publisher exists.
EOF
}

print_plan() {
    local repo_disp=${REPO:-"<LMER_REHEARSAL_REPO unset>"}
    local project_disp=${PROJECT:-"<LMER_REHEARSAL_PROJECT unset>"}
    local key_disp=${KEY_PATH:-"<LMER_REHEARSAL_SIGNING_KEY unset>"}
    echo "dry-run (${MODE}): plan only — no network calls made."
    case "$MODE" in
        teardown)
            cat <<EOF
Would tear down the rehearsal rig:
  - DELETE scratch repo ${repo_disp}
  - remove throwaway signing key ${key_disp} (+ .pub)
  - print manual residue: revoke LMER_REHEARSAL-prefixed tokens (GitHub
    PAT, TestPyPI token); optionally delete TestPyPI project ${project_disp}
  - evidence files under docs/rehearsal/ are never removed
EOF
            ;;
        *)
            local verb="create-or-verify"
            if [[ "$MODE" == "check" ]]; then
                verb="verify (read-only)"
            fi
            cat <<EOF
Would ${verb}:
  - drift guard: derive-workflow.py --check against ${REHEARSAL_WORKFLOW_PATH}
    (plus the diff against the rig repo's committed copy once it exists)
  - scratch repo ${repo_disp} (private; issues/wiki/projects off; PRs
    unaccepted by policy — GitHub has no switch to disable them)
  - rig repo contents, committed to main: derived ${REHEARSAL_WORKFLOW_PATH}
    (derive-workflow.py --emit), .github/scripts/verify-tag-signature.sh
    and .github/scripts/gate-version-reuse.py (verbatim from production,
    both env-seamed onto the rig's targets), a minimal reusable checks.yml, and a
    minimal buildable pyproject.toml with project.name ${project_disp}
  - ruleset '${MAIN_RULESET_NAME}': refs/heads/main, no deletion or
    force-push; only the bot (repo admin) pushes
  - ruleset '${TAG_RULESET_NAME}': refs/tags/v* locked, with a repo-admin
    bypass entry for the bot
  - throwaway ed25519 signing key at ${key_disp}
  - Actions variable RELEASE_ALLOWED_SIGNERS = throwaway public key
  - environment '${RIG_ENVIRONMENT}' with tag-pattern deployment policy v*
  - TestPyPI project ${project_disp}: verify via the read-only JSON API;
    trusted publisher (repo + ${REHEARSAL_WORKFLOW_PATH} + ${RIG_ENVIRONMENT})
    has no API — exact manual steps are printed instead
EOF
            ;;
    esac
}

if (( DRY_RUN )); then
    print_plan
    exit 0
fi

# ---------------------------------------------------------------------------
# From here on the rig credentials are present and the guard has passed.
# Every mutation below is create-or-verify (idempotent).
# ---------------------------------------------------------------------------

note() {
    printf '==> %s\n' "$*"
}

WORK_DIR=""
RIG_CLONE=""

cleanup() {
    rehearsal_gh_auth_cleanup
    if [[ -n "$WORK_DIR" ]]; then
        rm -rf -- "$WORK_DIR"
    fi
}
trap cleanup EXIT
# One shared 0600 header file for every curl in this run (the token never
# rides argv); created here in the parent shell so command-substituted
# API calls reuse it and the trap above removes it.
rehearsal_gh_auth_setup

CHECK_FAILURES=0
verify_step() {
    # verify_step <description> <command...> — record instead of abort, so
    # --check reports every gap at once.
    local desc="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        echo "ok: ${desc}"
    else
        echo "MISSING: ${desc}"
        CHECK_FAILURES=$(( CHECK_FAILURES + 1 ))
    fi
}

run_drift_guard() {
    # run_drift_guard [--with-rig-diff]
    # README: stand-up.sh runs derive-workflow.py --check on every
    # invocation. The plain form shape-checks the production workflow only
    # (needed on first stand-up, before the rig repo holds a workflow at
    # all); --with-rig-diff additionally fetches the rig repo's committed
    # release.yml and diffs it against the freshly derived output.
    local deriver="${SCRIPT_DIR}/derive-workflow.py"
    if [[ "${1:-}" == "--with-rig-diff" ]]; then
        note "drift guard: derive-workflow.py --check (diff against the rig repo's committed copy)"
        local rig_wf
        rig_wf=$(mktemp)
        if ! rehearsal_rig_workflow_fetch "$REPO" > "$rig_wf"; then
            rm -f -- "$rig_wf"
            rehearsal_err "stand-up: cannot fetch the rig repo's committed ${REHEARSAL_WORKFLOW_PATH} — the rig repo is unpopulated (run stand-up.sh without --check to converge)"
            return 1
        fi
        if ! LMER_REHEARSAL_PROJECT="$PROJECT" LMER_REHEARSAL_ENVIRONMENT="$RIG_ENVIRONMENT" \
            python3 "$deriver" --check --rig-workflow "$rig_wf"; then
            rm -f -- "$rig_wf"
            return 1
        fi
        rm -f -- "$rig_wf"
        return 0
    fi
    note "drift guard: derive-workflow.py --check (production shape)"
    python3 "$deriver" --check
}

ruleset_id_by_name() {
    # ruleset_id_by_name <name> — '' when absent.
    rehearsal_gh_api GET "/repos/${REPO}/rulesets" | python3 -c '
import json
import sys

name = sys.argv[1]
for ruleset in json.load(sys.stdin):
    if ruleset.get("name") == name:
        print(ruleset["id"])
        break
' "$1"
}

main_ruleset_body() {
    # Branch protection on main: no deletion, no force-push, linear history
    # not required (mirror of production main). Only the bot can push
    # regardless (it owns the private scratch repo); the ruleset makes the
    # protection explicit and identical in shape to production. Bypass:
    # none — even the bot cannot delete or force-push main.
    cat <<EOF
{
  "name": "${MAIN_RULESET_NAME}",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
  "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}]
}
EOF
}

tag_ruleset_body() {
    # Tag protection on v*: creation/update/deletion locked, with a bypass
    # entry for the bot. actor_id 5 is the built-in repository admin role;
    # the bot owns the scratch repo, so this is exactly "bot bypass".
    cat <<EOF
{
  "name": "${TAG_RULESET_NAME}",
  "target": "tag",
  "enforcement": "active",
  "bypass_actors": [
    {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
  ],
  "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
  "rules": [{"type": "creation"}, {"type": "update"}, {"type": "deletion"}]
}
EOF
}

ensure_ruleset() {
    # ensure_ruleset <name> <body-fn>
    local name="$1" body_fn="$2" existing
    existing=$(ruleset_id_by_name "$name")
    if [[ -n "$existing" ]]; then
        note "ruleset '${name}': exists (id ${existing}) — converging"
        rehearsal_gh_api PUT "/repos/${REPO}/rulesets/${existing}" "$("$body_fn")" >/dev/null
    else
        note "ruleset '${name}': creating"
        rehearsal_gh_api POST "/repos/${REPO}/rulesets" "$("$body_fn")" >/dev/null
    fi
}

ensure_repo() {
    if rehearsal_gh_api GET "/repos/${REPO}" >/dev/null 2>&1; then
        note "scratch repo ${REPO}: exists"
    else
        note "scratch repo ${REPO}: creating (private)"
        rehearsal_gh_api POST "/user/repos" "$(cat <<EOF
{
  "name": "${REPO_NAME}",
  "private": true,
  "description": "lmer release rehearsal rig (scratch; see Ctl/rehearsal in the lmer repo)",
  "has_issues": false,
  "has_wiki": false,
  "has_projects": false,
  "auto_init": true
}
EOF
)" >/dev/null
    fi
    # PR posture: GitHub has no switch that disables pull requests; the
    # policy is "PRs are not accepted" (mirror-repo posture, spec §6).
    # Converge what the API does control and report any open PRs.
    rehearsal_gh_api PATCH "/repos/${REPO}" \
        '{"has_issues": false, "has_wiki": false, "has_projects": false}' >/dev/null
    local open_prs
    open_prs=$(rehearsal_gh_api GET "/repos/${REPO}/pulls?state=open&per_page=1" |
        python3 -c 'import json, sys; print(len(json.load(sys.stdin)))')
    if [[ "$open_prs" != "0" ]]; then
        note "WARNING: ${REPO} has open PRs; rig policy is PRs-unaccepted — close them"
    else
        note "PR posture: no open PRs (policy: PRs are not accepted; no API switch exists to disable them)"
    fi
}

ensure_rig_clone() {
    if [[ -n "$RIG_CLONE" ]]; then
        return 0
    fi
    WORK_DIR=$(mktemp -d)
    # git askpass helper: the token stays out of URLs, argv, and
    # .git/config (it is read from the environment at prompt time) —
    # the same pattern as negative-test.sh / run-leg2.sh.
    local askpass="${WORK_DIR}/askpass.sh"
    cat > "$askpass" <<'EOF'
#!/bin/sh
case "$1" in
    Username*) printf '%s\n' x-access-token ;;
    *)         printf '%s\n' "$LMER_REHEARSAL_GITHUB_TOKEN" ;;
esac
EOF
    chmod 700 "$askpass"
    export GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0
    RIG_CLONE="${WORK_DIR}/rig"
    note "cloning ${REPO} (scratch)"
    git clone --quiet "https://github.com/${REPO}.git" "$RIG_CLONE"
}

rig_git() {
    git -C "$RIG_CLONE" \
        -c user.name="lmer-rehearsal" \
        -c user.email="lmer-rehearsal@invalid" \
        "$@"
}

rig_pyproject_body() {
    cat <<EOF
# Scratch package for the lmer release rehearsal rig (Ctl/rehearsal in
# the lmer repo). Never production: the distribution name below is the
# rehearsal project so artifacts are unmistakably non-production, and the
# import package is a rig-only placeholder. run-leg2.sh bumps the version
# line on each dry run.
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "${PROJECT}"
version = "0.0.0"
description = "lmer release rehearsal rig scratch package (never published to production PyPI)"
requires-python = ">=3.9"

[tool.hatch.build.targets.wheel]
packages = ["src/rehearsal_rig"]
EOF
}

rig_checks_workflow_body() {
    # The derived release workflow's `checks` job reuses
    # ./.github/workflows/checks.yml (passed through verbatim from
    # production). Production's checks.yml runs the real lmer suite
    # (uv sync --frozen + pre-commit + pytest), which the scratch repo
    # cannot satisfy — the rehearsal exercises the pipeline SHAPE (a
    # required reusable checks job gating build/publish), so the rig gets
    # a minimal passing stand-in at the same path.
    cat <<'EOF'
# Minimal reusable checks workflow for the lmer release rehearsal rig.
# Committed by Ctl/rehearsal/stand-up.sh (see that repo); a stand-in for
# production's checks.yml, which needs the real lmer source tree. Do not
# edit here — edit stand-up.sh and re-run it.
name: Checks

on:
  workflow_call:

permissions:
  contents: read

jobs:
  checks:
    name: Rehearsal rig checks
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6

      - name: Rig placeholder check
        run: |
          set -euo pipefail
          test -f pyproject.toml
          echo "rehearsal rig checks pass (placeholder for production checks.yml)"
EOF
}

ensure_rig_contents() {
    # Populate-or-converge the rig repo's contents. Without these the
    # first rehearsal run cannot succeed: a tag push would trigger no
    # workflow at all. Idempotent: identical content means no commit.
    ensure_rig_clone
    note "rig repo contents: converging (derived workflow, verify script, checks.yml, pyproject.toml)"
    mkdir -p "${RIG_CLONE}/.github/workflows" "${RIG_CLONE}/.github/scripts" \
        "${RIG_CLONE}/src/rehearsal_rig"

    # (a) The derived release workflow — always re-derived from the
    # CURRENT production workflow, so re-running stand-up converges the
    # rig copy after any production change.
    LMER_REHEARSAL_PROJECT="$PROJECT" LMER_REHEARSAL_ENVIRONMENT="$RIG_ENVIRONMENT" \
        python3 "${SCRIPT_DIR}/derive-workflow.py" --emit \
        > "${RIG_CLONE}/${REHEARSAL_WORKFLOW_PATH}"

    # (b) The scripts the derived workflow invokes — tag verification and
    # the version-reuse gate — verbatim from production. Both are env-seamed,
    # so the rig runs the production code against the rig's own targets.
    for script in verify-tag-signature.sh gate-version-reuse.py; do
        cp "${REHEARSAL_ROOT}/.github/scripts/${script}" \
            "${RIG_CLONE}/.github/scripts/${script}"
        chmod 755 "${RIG_CLONE}/.github/scripts/${script}"
    done

    # (c) The reusable checks workflow the derived workflow `uses:`.
    rig_checks_workflow_body > "${RIG_CLONE}/.github/workflows/checks.yml"

    # (d) pyproject.toml: created minimal when absent; when present only
    # project.name is converged to the rehearsal project (README: Rig
    # topology) — the version line belongs to run-leg2.sh's dry-run bumps.
    if [[ -f "${RIG_CLONE}/pyproject.toml" ]]; then
        sed -i "s/^name = .*/name = \"${PROJECT}\"/" "${RIG_CLONE}/pyproject.toml"
    else
        rig_pyproject_body > "${RIG_CLONE}/pyproject.toml"
    fi
    if [[ ! -f "${RIG_CLONE}/src/rehearsal_rig/__init__.py" ]]; then
        printf '"""lmer release rehearsal rig scratch package."""\n' \
            > "${RIG_CLONE}/src/rehearsal_rig/__init__.py"
    fi

    if [[ -z "$(rig_git status --porcelain)" ]]; then
        note "rig repo contents: already converged (nothing to commit)"
        return 0
    fi
    rig_git add -A
    rig_git commit --quiet \
        -m "stand-up: converge rig contents (derived workflow, verify script, checks, pyproject)"
    rig_git push --quiet origin main
    note "rig repo contents: committed and pushed to ${REPO} main"
}

ensure_throwaway_key() {
    if [[ -f "$KEY_PATH" ]]; then
        note "throwaway signing key: exists at ${KEY_PATH}"
    else
        note "throwaway signing key: generating ed25519 at ${KEY_PATH}"
        mkdir -p "$(dirname -- "$KEY_PATH")"
        ssh-keygen -q -t ed25519 -N "" -C "lmer-rehearsal throwaway" -f "$KEY_PATH"
    fi
}

allowed_signers_value() {
    # ssh allowed-signers line: principal, key type, key. The wildcard
    # principal mirrors production's verify script, which matches on the
    # key, not the identity.
    printf '* %s\n' "$(cut -d' ' -f1-2 < "${KEY_PATH}.pub")"
}

ensure_signers_variable() {
    local value body
    value=$(allowed_signers_value)
    body="{\"name\": \"RELEASE_ALLOWED_SIGNERS\", \"value\": $(printf '%s' "$value" | rehearsal_json_string)}"
    if rehearsal_gh_api GET "/repos/${REPO}/actions/variables/RELEASE_ALLOWED_SIGNERS" >/dev/null 2>&1; then
        note "Actions variable RELEASE_ALLOWED_SIGNERS: updating to throwaway key"
        rehearsal_gh_api PATCH "/repos/${REPO}/actions/variables/RELEASE_ALLOWED_SIGNERS" "$body" >/dev/null
    else
        note "Actions variable RELEASE_ALLOWED_SIGNERS: creating"
        rehearsal_gh_api POST "/repos/${REPO}/actions/variables" "$body" >/dev/null
    fi
}

ensure_environment() {
    note "environment '${RIG_ENVIRONMENT}': create-or-verify with custom deployment policies"
    rehearsal_gh_api PUT "/repos/${REPO}/environments/${RIG_ENVIRONMENT}" \
        '{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}' >/dev/null
    # Tag-pattern deployment policy v* — without it the environment binding
    # does not survive PAT compromise (spec §4/§6).
    local have_policy
    have_policy=$(rehearsal_gh_api GET "/repos/${REPO}/environments/${RIG_ENVIRONMENT}/deployment-branch-policies" |
        python3 -c '
import json
import sys

for policy in json.load(sys.stdin).get("branch_policies", []):
    if policy.get("name") == "v*" and policy.get("type") == "tag":
        print("yes")
        break
')
    if [[ "$have_policy" == "yes" ]]; then
        note "deployment tag-pattern policy v*: exists"
    else
        note "deployment tag-pattern policy v*: creating"
        rehearsal_gh_api POST "/repos/${REPO}/environments/${RIG_ENVIRONMENT}/deployment-branch-policies" \
            '{"name": "v*", "type": "tag"}' >/dev/null
    fi
}

verify_testpypi() {
    # Verify what the read-only JSON API exposes; everything else is a
    # printed manual step (no API — see the header comment).
    if rehearsal_testpypi_project_exists "$PROJECT"; then
        note "TestPyPI project '${PROJECT}': visible on ${REHEARSAL_TESTPYPI_URL}"
    else
        note "TestPyPI project '${PROJECT}': not visible yet (it materializes on first trusted-publisher upload)"
    fi
    note "TestPyPI trusted publisher: NOT automatable (no public API) — manual step follows"
    print_manual_testpypi_steps
}

do_standup() {
    # Shape-only guard first: on first stand-up the rig repo holds no
    # workflow yet, so the committed-copy diff can only run after
    # ensure_rig_contents converges it.
    run_drift_guard
    ensure_repo
    ensure_ruleset "$MAIN_RULESET_NAME" main_ruleset_body
    ensure_ruleset "$TAG_RULESET_NAME" tag_ruleset_body
    ensure_throwaway_key
    ensure_signers_variable
    ensure_environment
    ensure_rig_contents
    run_drift_guard --with-rig-diff
    verify_testpypi
    note "stand-up complete (idempotent; re-run any time to converge)"
}

check_ruleset_exists() {
    [[ -n "$(ruleset_id_by_name "$1")" ]]
}

check_tag_policy_exists() {
    rehearsal_gh_api GET "/repos/${REPO}/environments/${RIG_ENVIRONMENT}/deployment-branch-policies" |
        python3 -c '
import json
import sys

for policy in json.load(sys.stdin).get("branch_policies", []):
    if policy.get("name") == "v*" and policy.get("type") == "tag":
        sys.exit(0)
sys.exit(1)
'
}

do_check() {
    # The full drift guard (production shape + rig-copy diff) is recorded
    # as a gap instead of aborting, matching the report-every-gap style;
    # its own output stays visible (a hidden diff helps nobody).
    if ! run_drift_guard --with-rig-diff; then
        echo "MISSING: drift guard green (production shape + rig-copy diff)"
        CHECK_FAILURES=$(( CHECK_FAILURES + 1 ))
    fi
    verify_step "scratch repo ${REPO}" \
        rehearsal_gh_api GET "/repos/${REPO}"
    verify_step "rig repo workflow ${REHEARSAL_WORKFLOW_PATH} committed" \
        rehearsal_gh_api GET "/repos/${REPO}/contents/${REHEARSAL_WORKFLOW_PATH}"
    verify_step "ruleset '${MAIN_RULESET_NAME}' on main" \
        check_ruleset_exists "$MAIN_RULESET_NAME"
    verify_step "ruleset '${TAG_RULESET_NAME}' on v* (bot bypass)" \
        check_ruleset_exists "$TAG_RULESET_NAME"
    verify_step "throwaway signing key at ${KEY_PATH}" \
        test -f "$KEY_PATH"
    verify_step "Actions variable RELEASE_ALLOWED_SIGNERS" \
        rehearsal_gh_api GET "/repos/${REPO}/actions/variables/RELEASE_ALLOWED_SIGNERS"
    verify_step "environment '${RIG_ENVIRONMENT}'" \
        rehearsal_gh_api GET "/repos/${REPO}/environments/${RIG_ENVIRONMENT}"
    verify_step "deployment tag-pattern policy v*" \
        check_tag_policy_exists
    # Informational, NEVER a gate: the TestPyPI project materializes on the
    # first trusted-publisher upload (same wording as stand-up's
    # verify_testpypi), so counting its absence as a failure would make the
    # documented "--check exits 0" prerequisite unsatisfiable on a fresh
    # rig — the negative tests publish nothing.
    if rehearsal_testpypi_project_exists "$PROJECT"; then
        note "TestPyPI project '${PROJECT}': visible on ${REHEARSAL_TESTPYPI_URL}"
    else
        note "TestPyPI project '${PROJECT}': not visible yet (it materializes on first trusted-publisher upload; not a rig failure)"
    fi
    if (( CHECK_FAILURES )); then
        rehearsal_err "check: ${CHECK_FAILURES} rig piece(s) missing — run stand-up.sh (and the printed manual TestPyPI steps) to converge"
        print_manual_testpypi_steps
        exit 1
    fi
    note "check: all rig pieces present (trusted publisher itself is not API-verifiable; confirm once via the TestPyPI UI)"
}

do_teardown() {
    note "teardown: deleting scratch repo ${REPO}"
    if rehearsal_gh_api GET "/repos/${REPO}" >/dev/null 2>&1; then
        rehearsal_gh_api DELETE "/repos/${REPO}" >/dev/null
        note "deleted ${REPO}"
    else
        note "scratch repo ${REPO}: already absent"
    fi
    if [[ -n "$KEY_PATH" && -f "$KEY_PATH" ]]; then
        note "discarding throwaway signing key ${KEY_PATH} (+ .pub)"
        rm -f -- "$KEY_PATH" "${KEY_PATH}.pub"
    else
        note "throwaway signing key: already absent"
    fi
    # Evidence files under docs/rehearsal/ are the one artifact teardown
    # never removes — they are the point of the rig.
    cat <<EOF
Manual residue (no API path — do these by hand):
  - revoke the LMER_REHEARSAL-prefixed fine-grained PAT on GitHub
  - revoke the LMER_REHEARSAL-prefixed API token on TestPyPI
  - optionally delete the TestPyPI project '${PROJECT}'
Evidence files in docs/rehearsal/ were intentionally left in place.
EOF
}

case "$MODE" in
    standup)  do_standup ;;
    check)    do_check ;;
    teardown) do_teardown ;;
esac
