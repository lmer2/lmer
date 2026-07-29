#!/bin/bash
# negative-test.sh — the G2 negative tag-verification tests, run in the
# rehearsal rig (README.md in this directory is the frozen design).
#
# Three cases, each expected to fail the release pipeline in its FIRST job
# (verify-tag-signature) before anything builds or publishes:
#   unsigned-tag    a plain (unsigned) v* tag at GitHub main HEAD
#   wrong-signer    a v* tag signed by a key absent from the rig repo's
#                   RELEASE_ALLOWED_SIGNERS variable
#   not-main-head   a correctly signed v* tag whose commit is NOT at
#                   GitHub main HEAD
#
# For each case the script pushes the tag to the rig repo, waits for the
# release.yml run, and asserts: run conclusion is failure; the failing job
# is verify-tag-signature; every downstream job (checks/build/publish/
# release) is skipped or never started; TestPyPI gained no file for the
# version; and no GitHub Release exists.
#
# Evidence: one per-case file via lib.sh's writer (one file per rehearsal
# run, per the README) plus the aggregate
# docs/rehearsal/evidence-negative-test.md — prose header + a single
# fenced yaml block, re-checkable offline with --verify-evidence. The
# aggregate is written ONLY from real run data, only after all three cases
# pass; until then it carries status: pending and TBD placeholders — this
# script never fabricates run URLs, conclusions, or listings.
#
# Safety, in order, before anything else runs (same as stand-up.sh):
#   1. Production-target guard (offline, lib.sh) in EVERY mode.
#   2. R14 skip-clean: rig-touching modes (--all, --case) exit 0 with a
#      clear notice when the LMER_REHEARSAL_* credential variables are
#      absent, so this script verifies in a sandbox.
#   3. Drift guard: derive-workflow.py --check must be green immediately
#      before any rehearsal run (README: Workflow derivation).

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

# The case matrix (scenario name = negative-<case>; both spellings are in
# lib.sh's REHEARSAL_SCENARIOS).
CASES=(unsigned-tag wrong-signer not-main-head)
NEGATIVE_SCENARIOS=(negative-unsigned-tag negative-wrong-signer negative-not-main-head)

VERIFY_JOB_ID="verify-tag-signature"

usage() {
    cat <<'EOF'
usage: negative-test.sh --all | --case <name> | --verify-evidence <file>
                        | --write-pending <file> | --assert-jobs <json>
                        [--dry-run] [--repo <owner/name>] [--project <name>]
                        [--env-file <path>] [--help]

G2 negative tag-verification tests in the rehearsal rig
(Ctl/rehearsal/README.md). Each case must fail the release pipeline in
verify-tag-signature before any build or publish step.

  --all              run all three cases end-to-end in the rig; rewrite
                     the aggregate evidence file with the recorded results
                     (docs/rehearsal/evidence-negative-test.md)
  --case <name>      run one case (debugging): unsigned-tag |
                     wrong-signer | not-main-head. Per-case evidence only;
                     the aggregate file is untouched.
  --verify-evidence  offline re-check of the aggregate evidence file; a
                     pending skeleton exits 0 with a loud PENDING notice,
                     a populated file gets the full consistency check
  --write-pending    write the pending evidence skeleton (refuses to
                     overwrite recorded evidence)
  --assert-jobs      offline: assert a GitHub jobs-API JSON document
                     proves fail-before-publish (fixture/test entry point)
  --dry-run          print the plan for --all/--case; no network calls
  --repo             override LMER_REHEARSAL_REPO
  --project          override LMER_REHEARSAL_PROJECT
  --env-file         rig.env path (default: Ctl/rehearsal/rig.env)

Without the LMER_REHEARSAL_* credential variables, rig-touching modes
(--all, --case) SKIP-CLEAN (exit 0 with a notice). The production-target
guard always runs, offline, before anything else.
EOF
}

MODE=""
MODE_ARG=""
CASE_NAME=""
DRY_RUN=0
REPO_OVERRIDE=""
PROJECT_OVERRIDE=""
ENV_FILE=""

set_mode() {
    if [[ -n "$MODE" ]]; then
        rehearsal_err "negative-test.sh: modes are mutually exclusive (--all, --case, --verify-evidence, --write-pending, --assert-jobs)"
        usage >&2
        exit 2
    fi
    MODE=$1
}

need_value() {
    if [[ $# -lt 2 ]]; then
        rehearsal_err "negative-test.sh: $1 requires a value"
        usage >&2
        exit 2
    fi
}

while (($#)); do
    case "$1" in
        --all)
            set_mode all
            ;;
        --case)
            need_value "$@"
            set_mode case
            CASE_NAME=$2
            shift
            ;;
        --verify-evidence|--write-pending|--assert-jobs)
            need_value "$@"
            set_mode "${1#--}"
            MODE_ARG=$2
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            ;;
        --repo)
            need_value "$@"
            REPO_OVERRIDE=$2
            shift
            ;;
        --project)
            need_value "$@"
            PROJECT_OVERRIDE=$2
            shift
            ;;
        --env-file)
            need_value "$@"
            ENV_FILE=$2
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            rehearsal_err "negative-test.sh: unknown argument '$1'"
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ -z "$MODE" ]]; then
    rehearsal_err "negative-test.sh: a mode is required"
    usage >&2
    exit 2
fi

case_valid() {
    local c
    for c in "${CASES[@]}"; do
        if [[ "$c" == "$1" ]]; then
            return 0
        fi
    done
    return 1
}

if [[ "$MODE" == "case" ]] && ! case_valid "$CASE_NAME"; then
    rehearsal_err "negative-test.sh: unknown case '${CASE_NAME}' (cases: ${CASES[*]})"
    exit 2
fi

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

EVIDENCE_FILE="${REHEARSAL_EVIDENCE_DIR:-${REHEARSAL_ROOT}/docs/rehearsal}/evidence-negative-test.md"

# --- Hard guard: offline, every mode, before any network call. -------------
rehearsal_guard "$REPO" "$PROJECT" "$REHEARSAL_TESTPYPI_URL"

note() {
    printf '==> %s\n' "$*"
}

# The verify job's display name (the jobs API reports display names, not
# job ids) — parsed from the production workflow so a rename cannot make
# the assertion match nothing silently; falls back to the known name.
verify_job_display_name() {
    local wf="${REHEARSAL_ROOT}/${REHEARSAL_WORKFLOW_PATH}" name=""
    if [[ -f "$wf" ]]; then
        name=$(awk -v job="$VERIFY_JOB_ID" '
            $0 == "  " job ":" { injob = 1; next }
            injob && /^  [A-Za-z0-9_-]+:[[:space:]]*$/ { injob = 0 }
            injob && /^    name:/ {
                sub(/^    name:[[:space:]]*/, "")
                print
                exit
            }
        ' "$wf")
    fi
    printf '%s\n' "${name:-Verify tag signature and main head}"
}
VERIFY_JOB_NAME=$(verify_job_display_name)

# ---------------------------------------------------------------------------
# Job-table assertion — the heart of the negative test, shared by the live
# run path (rehearsal_run_jobs output), --assert-jobs (fixtures), and the
# offline evidence verifier.
# ---------------------------------------------------------------------------

# negative_assert_jobs — stdin: "name<TAB>conclusion" lines (the shape of
# lib.sh's rehearsal_run_jobs output). Prints the one-line evidence
# summary ("name=conclusion | ...") on stdout. Returns 0 iff the table
# proves the pipeline failed before any build or publish step: exactly one
# job failed, it is the verify-tag-signature job, and every other job is
# skipped (never-started jobs simply do not appear).
negative_assert_jobs() {
    local name concl summary="" failures=0 verify_failed=0 problems=0 total=0
    while IFS=$'\t' read -r name concl; do
        if [[ -z "$name" ]]; then
            continue
        fi
        total=$(( total + 1 ))
        summary+="${summary:+ | }${name}=${concl}"
        if [[ "$name" == "$VERIFY_JOB_NAME" || "$name" == "$VERIFY_JOB_ID" ]]; then
            if [[ "$concl" == "failure" ]]; then
                verify_failed=1
            else
                rehearsal_err "negative-test: verify job '${name}' concluded '${concl}' (expected failure)"
                problems=$(( problems + 1 ))
            fi
        elif [[ "$concl" != "skipped" ]]; then
            rehearsal_err "negative-test: downstream job '${name}' concluded '${concl}' (must be skipped — the pipeline must fail before build/publish)"
            problems=$(( problems + 1 ))
        fi
        if [[ "$concl" == "failure" ]]; then
            failures=$(( failures + 1 ))
        fi
    done
    printf '%s\n' "$summary"
    if (( total == 0 )); then
        rehearsal_err "negative-test: no jobs in the run"
        return 1
    fi
    if (( ! verify_failed )); then
        rehearsal_err "negative-test: the ${VERIFY_JOB_ID} job did not fail"
        problems=$(( problems + 1 ))
    fi
    if (( failures != 1 )); then
        rehearsal_err "negative-test: expected exactly 1 failed job, saw ${failures}"
        problems=$(( problems + 1 ))
    fi
    (( problems == 0 ))
}

# negative_jobs_tsv_from_json <file> — GitHub jobs-API JSON to
# name<TAB>conclusion lines; the same extraction as lib.sh's
# rehearsal_run_jobs, minus the API call (offline fixture path).
negative_jobs_tsv_from_json() {
    python3 -c '
import json
import sys

for job in json.load(sys.stdin).get("jobs", []):
    print(job["name"], job.get("conclusion") or job.get("status") or "", sep="\t")
' < "$1"
}

# summary_to_tsv <summary> — invert the evidence "name=conclusion | ..."
# summary back into name<TAB>conclusion lines for negative_assert_jobs.
summary_to_tsv() {
    local rest="$1" entry
    while [[ -n "$rest" ]]; do
        entry=${rest%% | *}
        if [[ "$entry" == "$rest" ]]; then
            rest=""
        else
            rest=${rest#* | }
        fi
        printf '%s\t%s\n' "${entry%=*}" "${entry##*=}"
    done
}

# job_precedes_publish <job-id> — same rule as lib.sh's evidence verifier:
# the failing job must come before publish-pypi in the workflow job order.
job_precedes_publish() {
    local target="$1" job
    while IFS= read -r job; do
        if [[ "$job" == "$REHEARSAL_PUBLISH_JOB" ]]; then
            break
        fi
        if [[ "$job" == "$target" ]]; then
            return 0
        fi
    done < <(rehearsal_release_job_order)
    return 1
}

# ---------------------------------------------------------------------------
# Aggregate evidence file — prose header + one fenced yaml block (README:
# Evidence format), holding all three cases as flat "<scenario>.<field>"
# keys so lib.sh's parser (rehearsal_evidence_get) reads it unchanged.
# ---------------------------------------------------------------------------

# Per-case recorded values, keyed by scenario. Empty until a case runs;
# the writer emits TBD for anything unrecorded (pending skeleton).
declare -A EV_TAG EV_SHA EV_RUN_ID EV_RUN_URL EV_CONCL EV_FAILED EV_JOBS \
    EV_BEFORE EV_AFTER EV_PUB EV_RELEASE

MAIN_HEAD=""

write_aggregate() {
    # write_aggregate <pending|complete> <file>
    local status="$1" file="$2" s
    mkdir -p "$(dirname -- "$file")"
    {
        printf '# Rehearsal evidence: G2 negative tag-verification tests\n\n'
        if [[ "$status" == "pending" ]]; then
            cat <<'EOF'
**STATUS: PENDING — the negative test has NOT run yet.** Nothing below is
recorded evidence; every value is a TBD placeholder (the pending skeleton
carries no data at all).

Prerequisites before this file can be populated (all under Ctl/rehearsal):

- the rig is stood up and green: `stand-up.sh`, then `stand-up.sh --check`
  exits 0 (needs `rig.env` with the `LMER_REHEARSAL_*` credentials, and
  the TestPyPI trusted publisher registered per the printed manual steps)
- the workflow drift guard is green: `derive-workflow.py --check`

To populate: run `Ctl/rehearsal/negative-test.sh --all` with the rig env
present — it executes the three cases in the rig and REWRITES this file
with the recorded evidence. Re-check offline with
`Ctl/rehearsal/negative-test.sh --verify-evidence <this file>`.
EOF
        else
            cat <<'EOF'
Recorded by `Ctl/rehearsal/negative-test.sh --all` against the rehearsal
rig (Ctl/rehearsal/README.md). Three cases, each asserted to fail the
release pipeline in verify-tag-signature before any build or publish
step: run conclusion failure, every downstream job skipped, no TestPyPI
file for the version, no GitHub Release. Per-case run files (lib.sh
format) live alongside this file. Re-check offline with
`Ctl/rehearsal/negative-test.sh --verify-evidence <this file>`.
EOF
        fi
        printf '\n```yaml\n'
        printf 'status: %s\n' "$status"
        if [[ "$status" == "pending" ]]; then
            printf 'rig_repo: TBD\n'
            printf 'rig_project: TBD\n'
            printf 'main_head_sha: TBD\n'
            printf 'derive_check: TBD\n'
            printf 'recorded_at: TBD\n'
        else
            printf 'rig_repo: %s\n' "$REPO"
            printf 'rig_project: %s\n' "$PROJECT"
            printf 'main_head_sha: %s\n' "$MAIN_HEAD"
            printf 'derive_check: pass\n'
            printf 'recorded_at: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        fi
        for s in "${NEGATIVE_SCENARIOS[@]}"; do
            printf '%s.tag: %s\n' "$s" "${EV_TAG[$s]:-TBD}"
            printf '%s.tag_sha: %s\n' "$s" "${EV_SHA[$s]:-TBD}"
            printf '%s.workflow_run_id: %s\n' "$s" "${EV_RUN_ID[$s]:-TBD}"
            printf '%s.workflow_run_url: %s\n' "$s" "${EV_RUN_URL[$s]:-TBD}"
            printf '%s.expected_conclusion: failure\n' "$s"
            printf '%s.recorded_conclusion: %s\n' "$s" "${EV_CONCL[$s]:-TBD}"
            printf '%s.failed_job: %s\n' "$s" "${EV_FAILED[$s]:-TBD}"
            printf '%s.jobs: %s\n' "$s" "${EV_JOBS[$s]:-TBD}"
            printf '%s.testpypi_files_before: %s\n' "$s" "${EV_BEFORE[$s]:-TBD}"
            printf '%s.testpypi_files_after: %s\n' "$s" "${EV_AFTER[$s]:-TBD}"
            printf '%s.published: %s\n' "$s" "${EV_PUB[$s]:-TBD}"
            printf '%s.github_release: %s\n' "$s" "${EV_RELEASE[$s]:-TBD}"
        done
        printf '```\n'
    } > "$file"
}

do_write_pending() {
    local file="$1"
    if [[ -f "$file" && "$(rehearsal_evidence_get "$file" status)" == "complete" ]]; then
        rehearsal_err "write-pending: ${file} carries recorded evidence (status: complete) — refusing to overwrite it with the skeleton"
        exit 1
    fi
    write_aggregate pending "$file"
    echo "wrote pending evidence skeleton: ${file}"
}

# ---------------------------------------------------------------------------
# Offline evidence verifier (--verify-evidence): pending skeleton -> loud
# notice + exit 0; populated file -> full consistency re-check; anything
# malformed or inconsistent -> nonzero.
# ---------------------------------------------------------------------------

EV_ERRORS=0
ev_problem() {
    rehearsal_err "verify-evidence: $*"
    EV_ERRORS=$(( EV_ERRORS + 1 ))
}

verify_negative_evidence() {
    local file="$1"
    if [[ ! -f "$file" ]]; then
        rehearsal_err "verify-evidence: no such file: $file"
        return 1
    fi

    local status
    status=$(rehearsal_evidence_get "$file" status)
    case "$status" in
        pending)
            cat <<EOF
============================================================
PENDING — negative test has not run yet
============================================================
${file} is the pending skeleton: no rig case has been executed
and no evidence is recorded (TBD placeholders only).
Populate it by running Ctl/rehearsal/negative-test.sh --all with
the rig env (rig.env) present. Exiting 0: a pending skeleton is
well-formed, it just is not evidence.
EOF
            return 0
            ;;
        complete)
            ;;
        "")
            rehearsal_err "verify-evidence: ${file} has no status field — not a negative-test evidence file (or malformed)"
            return 1
            ;;
        *)
            rehearsal_err "verify-evidence: unknown status '${status}' in ${file} (expected pending or complete)"
            return 1
            ;;
    esac

    EV_ERRORS=0

    # Top-level fields, incl. the production-target guard from lib.sh —
    # evidence claiming a production target is inconsistent by definition.
    local rig_repo rig_project main_head derive_check recorded_at field value
    for field in rig_repo rig_project main_head_sha derive_check recorded_at; do
        value=$(rehearsal_evidence_get "$file" "$field")
        if [[ -z "$value" || "$value" == "TBD" ]]; then
            ev_problem "field '${field}' is missing or TBD in a status: complete file"
        fi
    done
    rig_repo=$(rehearsal_evidence_get "$file" rig_repo)
    rig_project=$(rehearsal_evidence_get "$file" rig_project)
    main_head=$(rehearsal_evidence_get "$file" main_head_sha)
    derive_check=$(rehearsal_evidence_get "$file" derive_check)
    recorded_at=$(rehearsal_evidence_get "$file" recorded_at)
    rehearsal_guard_repo "$rig_repo" || ev_problem "rig_repo '${rig_repo}' fails the production-target guard"
    rehearsal_guard_project "$rig_project" || ev_problem "rig_project '${rig_project}' fails the production-target guard"
    if [[ -n "$main_head" && "$main_head" != "TBD" && ! "$main_head" =~ ^[0-9a-f]{40}$ ]]; then
        ev_problem "main_head_sha '${main_head}' is not 40 lowercase hex"
    fi
    if [[ "$derive_check" != "pass" ]]; then
        ev_problem "derive_check is '${derive_check}', must be 'pass'"
    fi
    if [[ -n "$recorded_at" && "$recorded_at" != "TBD" && ! "$recorded_at" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]]; then
        ev_problem "recorded_at '${recorded_at}' is not UTC ISO-8601 (YYYY-MM-DDThh:mm:ssZ)"
    fi

    local s tag_sha run_id run_url expected recorded failed_job jobs before after published release
    for s in "${NEGATIVE_SCENARIOS[@]}"; do
        for field in tag tag_sha workflow_run_id workflow_run_url \
                     expected_conclusion recorded_conclusion failed_job jobs \
                     testpypi_files_before testpypi_files_after published \
                     github_release; do
            value=$(rehearsal_evidence_get "$file" "${s}.${field}")
            if [[ -z "$value" || "$value" == "TBD" ]]; then
                ev_problem "${s}.${field} is missing or TBD in a status: complete file"
            fi
        done
        tag_sha=$(rehearsal_evidence_get "$file" "${s}.tag_sha")
        run_id=$(rehearsal_evidence_get "$file" "${s}.workflow_run_id")
        run_url=$(rehearsal_evidence_get "$file" "${s}.workflow_run_url")
        expected=$(rehearsal_evidence_get "$file" "${s}.expected_conclusion")
        recorded=$(rehearsal_evidence_get "$file" "${s}.recorded_conclusion")
        failed_job=$(rehearsal_evidence_get "$file" "${s}.failed_job")
        jobs=$(rehearsal_evidence_get "$file" "${s}.jobs")
        before=$(rehearsal_evidence_get "$file" "${s}.testpypi_files_before")
        after=$(rehearsal_evidence_get "$file" "${s}.testpypi_files_after")
        published=$(rehearsal_evidence_get "$file" "${s}.published")
        release=$(rehearsal_evidence_get "$file" "${s}.github_release")

        if [[ -n "$tag_sha" && "$tag_sha" != "TBD" && ! "$tag_sha" =~ ^[0-9a-f]{40}$ ]]; then
            ev_problem "${s}.tag_sha '${tag_sha}' is not 40 lowercase hex"
        fi
        if [[ -n "$run_id" && "$run_id" != "TBD" && ! "$run_id" =~ ^[0-9]+$ ]]; then
            ev_problem "${s}.workflow_run_id '${run_id}' is not numeric"
        fi
        if [[ -n "$run_url" && "$run_url" != "TBD" && "$run_url" != https://* ]]; then
            ev_problem "${s}.workflow_run_url '${run_url}' is not an https URL"
        fi
        if [[ "$expected" != "failure" ]]; then
            ev_problem "${s}.expected_conclusion is '${expected}', negatives must expect failure"
        fi
        if [[ "$recorded" != "failure" ]]; then
            ev_problem "${s}.recorded_conclusion is '${recorded}', not failure — the pipeline did not fail"
        fi
        if [[ "$failed_job" != "$VERIFY_JOB_ID" ]]; then
            ev_problem "${s}.failed_job is '${failed_job}', must be ${VERIFY_JOB_ID}"
        elif ! job_precedes_publish "$failed_job"; then
            ev_problem "${s}.failed_job '${failed_job}' does not precede ${REHEARSAL_PUBLISH_JOB} in the release workflow job order"
        fi
        if [[ -n "$jobs" && "$jobs" != "TBD" ]]; then
            if ! summary_to_tsv "$jobs" | negative_assert_jobs >/dev/null; then
                ev_problem "${s}.jobs does not prove fail-before-publish (see messages above)"
            fi
        fi
        if [[ "$published" != "false" ]]; then
            ev_problem "${s}.published is '${published}', must be false"
        fi
        if [[ "$after" != "$before" ]]; then
            ev_problem "${s}: TestPyPI listing changed ('${before}' -> '${after}') — something was published"
        fi
        if [[ "$release" != "absent" ]]; then
            ev_problem "${s}.github_release is '${release}', must be absent"
        fi
    done

    # The case-defining property of not-main-head: its tag commit must
    # differ from the recorded main HEAD.
    tag_sha=$(rehearsal_evidence_get "$file" "negative-not-main-head.tag_sha")
    if [[ -n "$tag_sha" && "$tag_sha" != "TBD" && "$tag_sha" == "$main_head" ]]; then
        ev_problem "negative-not-main-head.tag_sha equals main_head_sha — the case did not exercise the tag-placement check"
    fi

    if (( EV_ERRORS )); then
        rehearsal_err "verify-evidence: FAIL — ${EV_ERRORS} problem(s) in ${file}"
        return 1
    fi
    echo "verify-evidence: OK — ${file} (negative test, ${#NEGATIVE_SCENARIOS[@]} cases, fail-before-publish holds)"
    return 0
}

do_assert_jobs() {
    local file="$1" summary rc=0
    if [[ ! -f "$file" ]]; then
        rehearsal_err "assert-jobs: no such file: $file"
        exit 1
    fi
    summary=$(negative_jobs_tsv_from_json "$file" | negative_assert_jobs) || rc=1
    printf 'jobs: %s\n' "$summary"
    if (( rc )); then
        rehearsal_err "assert-jobs: FAIL — ${file} does not prove fail-before-publish"
        exit 1
    fi
    echo "assert-jobs: OK — pipeline failed in ${VERIFY_JOB_ID} before any build or publish"
}

# ---------------------------------------------------------------------------
# Offline modes dispatch (no rig credentials required).
# ---------------------------------------------------------------------------

case "$MODE" in
    verify-evidence)
        verify_negative_evidence "$MODE_ARG"
        exit
        ;;
    write-pending)
        do_write_pending "$MODE_ARG"
        exit 0
        ;;
    assert-jobs)
        do_assert_jobs "$MODE_ARG"
        exit 0
        ;;
esac

# ---------------------------------------------------------------------------
# Rig modes (--all, --case) from here on.
# ---------------------------------------------------------------------------

case_description() {
    case "$1" in
        unsigned-tag)
            echo "push an unsigned (lightweight) v* tag at GitHub main HEAD"
            ;;
        wrong-signer)
            echo "push a v* tag signed by a throwaway key NOT in RELEASE_ALLOWED_SIGNERS, at main HEAD"
            ;;
        not-main-head)
            echo "push a v* tag signed by the rig key at a commit that is NOT GitHub main HEAD"
            ;;
    esac
}

run_list() {
    if [[ "$MODE" == "case" ]]; then
        printf '%s\n' "$CASE_NAME"
    else
        printf '%s\n' "${CASES[@]}"
    fi
}

print_plan() {
    local c
    echo "dry-run (negative-test --${MODE}${CASE_NAME:+ ${CASE_NAME}}): plan only — no network calls made."
    echo "Each case must fail the pipeline in ${VERIFY_JOB_ID} before any build or publish:"
    while IFS= read -r c; do
        printf '  - %s: %s\n' "$c" "$(case_description "$c")"
    done < <(run_list)
    cat <<EOF
Per case: drift guard (derive-workflow.py --check) first; TestPyPI file
listing before/after; workflow run conclusion + per-job table; GitHub
Release absence; per-case evidence file via lib.sh's writer.
Only --all rewrites the aggregate evidence: ${EVIDENCE_FILE}
EOF
}

if (( DRY_RUN )); then
    print_plan
    exit 0
fi

# R14 skip-clean: no rig credentials -> nothing to touch, loudly, exit 0.
missing=$(rehearsal_missing_env)
if [[ -n "$missing" ]]; then
    # shellcheck disable=SC2086
    rehearsal_skip_clean "negative-test --${MODE}" $missing
    exit 0
fi

if [[ -z "$REPO" || -z "$PROJECT" ]]; then
    rehearsal_err "negative-test.sh: LMER_REHEARSAL_REPO and LMER_REHEARSAL_PROJECT must be set (rig.env)"
    exit 1
fi

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

# Drift guard — a stale rig workflow can never produce evidence (README):
# production shape check plus the diff of the rig repo's committed
# release.yml against the freshly derived output.
note "drift guard: derive-workflow.py --check (diff against the rig repo's committed copy)"
rig_wf_snapshot=$(mktemp)
if ! rehearsal_rig_workflow_fetch "$REPO" > "$rig_wf_snapshot"; then
    rm -f -- "$rig_wf_snapshot"
    rehearsal_err "negative-test.sh: cannot fetch the rig repo's committed ${REHEARSAL_WORKFLOW_PATH} — run stand-up.sh to populate the rig repo"
    exit 1
fi
LMER_REHEARSAL_PROJECT="$PROJECT" \
LMER_REHEARSAL_ENVIRONMENT="${LMER_REHEARSAL_ENVIRONMENT:-testpypi}" \
    python3 "${SCRIPT_DIR}/derive-workflow.py" --check --rig-workflow "$rig_wf_snapshot"
rm -f -- "$rig_wf_snapshot"

ensure_clone() {
    if [[ -n "$RIG_CLONE" ]]; then
        return 0
    fi
    WORK_DIR=$(mktemp -d)
    # git askpass helper: the token stays out of URLs, argv, and
    # .git/config (it is read from the environment at prompt time).
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
        -c gpg.format=ssh \
        "$@"
}

rig_main_head() {
    rehearsal_gh_api GET "/repos/${REPO}/branches/main" | rehearsal_json_get commit.sha
}

testpypi_listing() {
    # testpypi_listing <project> <version> — comma-joined filenames, or
    # "(none)" when the version has no files on TestPyPI (the expected
    # negative outcome; the JSON API 404s for an unknown version).
    local files
    files=$(rehearsal_testpypi_files "$1" "$2" 2>/dev/null | paste -sd, -) || files=""
    printf '%s\n' "${files:-(none)}"
}

wait_for_run() {
    # wait_for_run <tag> — poll until the release.yml run for the tag push
    # appears; print its id.
    local tag="$1" tries="${NEGATIVE_RUN_FIND_TRIES:-36}" run_id=""
    while (( tries > 0 )); do
        run_id=$(rehearsal_find_run_for_tag "$REPO" "$tag" || true)
        if [[ -n "$run_id" ]]; then
            printf '%s\n' "$run_id"
            return 0
        fi
        sleep "${NEGATIVE_RUN_FIND_INTERVAL:-5}"
        tries=$(( tries - 1 ))
    done
    rehearsal_err "negative-test: no ${REHEARSAL_WORKFLOW_PATH} run appeared for tag ${tag}"
    return 1
}

CASE_TAG_SHA=""

push_case_tag() {
    # push_case_tag <case> <tag> — create and push the case's tag; sets
    # CASE_TAG_SHA to the commit the tag points at.
    local case_name="$1" tag="$2"
    case "$case_name" in
        unsigned-tag)
            # Lightweight (unsigned) tag at main HEAD via the refs API — a
            # PAT-created ref triggers the tag-push workflow.
            rehearsal_gh_api POST "/repos/${REPO}/git/refs" \
                "{\"ref\": \"refs/tags/${tag}\", \"sha\": \"${MAIN_HEAD}\"}" >/dev/null
            CASE_TAG_SHA=$MAIN_HEAD
            ;;
        wrong-signer)
            ensure_clone
            local wrong_key="${WORK_DIR}/wrong-signer-key"
            if [[ ! -f "$wrong_key" ]]; then
                # A throwaway key that is deliberately NOT in the rig
                # repo's RELEASE_ALLOWED_SIGNERS variable.
                ssh-keygen -q -t ed25519 -N "" -C "lmer-rehearsal wrong signer" -f "$wrong_key"
            fi
            rig_git -c user.signingkey="$wrong_key" tag -s "$tag" \
                -m "rehearsal negative test: signer not in RELEASE_ALLOWED_SIGNERS" \
                origin/main
            rig_git push --quiet origin "refs/tags/${tag}"
            CASE_TAG_SHA=$(rig_git rev-parse "refs/tags/${tag}^{commit}")
            ;;
        not-main-head)
            ensure_clone
            # A commit that exists only behind the tag: correctly signed,
            # but not at GitHub main HEAD.
            rig_git checkout --quiet --detach origin/main
            rig_git commit --quiet --allow-empty \
                -m "rehearsal negative test: commit not at main HEAD"
            rig_git -c user.signingkey="${LMER_REHEARSAL_SIGNING_KEY}" tag -s "$tag" \
                -m "rehearsal negative test: correctly signed, not at main HEAD"
            rig_git push --quiet origin "refs/tags/${tag}"
            CASE_TAG_SHA=$(rig_git rev-parse "refs/tags/${tag}^{commit}")
            ;;
    esac
}

run_case() {
    local case_name="$1"
    local scenario="negative-${case_name}"
    local tag="v0.0.0-neg-${case_name}-${RUN_STAMP}"
    local version="${tag#v}"
    local before after run_id run_url conclusion jobs_tsv summary

    note "case ${case_name}: $(case_description "$case_name")"
    before=$(testpypi_listing "$PROJECT" "$version")
    push_case_tag "$case_name" "$tag"
    note "case ${case_name}: pushed ${tag} (commit ${CASE_TAG_SHA}); waiting for the workflow run"

    run_id=$(wait_for_run "$tag")
    run_url="https://github.com/${REPO}/actions/runs/${run_id}"
    conclusion=$(rehearsal_poll_run "$REPO" "$run_id")
    note "case ${case_name}: run ${run_id} concluded '${conclusion}'"
    if [[ "$conclusion" != "failure" ]]; then
        rehearsal_err "negative-test: case ${case_name}: run concluded '${conclusion}', expected failure — NEGATIVE TEST FAILED (${run_url})"
        return 1
    fi

    jobs_tsv=$(rehearsal_run_jobs "$REPO" "$run_id")
    if ! summary=$(negative_assert_jobs <<<"$jobs_tsv"); then
        rehearsal_err "negative-test: case ${case_name}: job table does not prove fail-before-publish — NEGATIVE TEST FAILED (${run_url})"
        return 1
    fi

    if rehearsal_release_lookup "$REPO" "$tag" >/dev/null 2>&1; then
        rehearsal_err "negative-test: case ${case_name}: a GitHub Release exists for ${tag} — NEGATIVE TEST FAILED"
        return 1
    fi
    after=$(testpypi_listing "$PROJECT" "$version")
    if [[ "$after" != "$before" ]]; then
        rehearsal_err "negative-test: case ${case_name}: TestPyPI listing changed ('${before}' -> '${after}') — NEGATIVE TEST FAILED"
        return 1
    fi
    if [[ "$case_name" == "not-main-head" && "$CASE_TAG_SHA" == "$MAIN_HEAD" ]]; then
        rehearsal_err "negative-test: case ${case_name}: tag commit equals main HEAD — the case did not exercise the tag-placement check"
        return 1
    fi

    EV_TAG[$scenario]=$tag
    EV_SHA[$scenario]=$CASE_TAG_SHA
    EV_RUN_ID[$scenario]=$run_id
    EV_RUN_URL[$scenario]=$run_url
    EV_CONCL[$scenario]=$conclusion
    EV_FAILED[$scenario]=$VERIFY_JOB_ID
    EV_JOBS[$scenario]=$summary
    EV_BEFORE[$scenario]=$before
    EV_AFTER[$scenario]=$after
    EV_PUB[$scenario]=false
    EV_RELEASE[$scenario]=absent

    # Per-run evidence file via lib.sh's writer (README: one file per
    # rehearsal run, one format for negatives and leg-2 alike).
    local per_case
    per_case=$(rehearsal_evidence_write \
        "scenario=${scenario}" \
        "rig_repo=${REPO}" \
        "rig_project=${PROJECT}" \
        "tag=${tag}" \
        "tag_sha=${CASE_TAG_SHA}" \
        "workflow_run_id=${run_id}" \
        "workflow_run_url=${run_url}" \
        "expected_conclusion=failure" \
        "recorded_conclusion=${conclusion}" \
        "failed_job=${VERIFY_JOB_ID}" \
        "published=false" \
        "derive_check=pass")
    note "case ${case_name}: OK — fail-before-publish holds; per-case evidence: ${per_case}"
}

RUN_STAMP=$(date -u +%Y%m%dT%H%M%SZ)
MAIN_HEAD=$(rig_main_head)
note "rig ${REPO} main HEAD: ${MAIN_HEAD}"

if [[ "$MODE" == "case" ]]; then
    run_case "$CASE_NAME"
    note "single case complete; aggregate ${EVIDENCE_FILE} NOT rewritten (run --all for that)"
else
    for c in "${CASES[@]}"; do
        run_case "$c"
    done
    write_aggregate complete "$EVIDENCE_FILE"
    note "all three negative cases passed; aggregate evidence: ${EVIDENCE_FILE}"
    # Immediately re-check the evidence we just wrote, offline.
    verify_negative_evidence "$EVIDENCE_FILE"
fi
