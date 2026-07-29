#!/bin/bash
# run-leg2.sh — the leg-2 dry run in the rehearsal rig (README.md in this
# directory is the frozen design; taskdef/release-leg2.jinja2 is the leg-2
# sequence being rehearsed).
#
# The walk exercises the spec's leg-2 path end-to-end in the rig, before
# any production release, so G4 does not run untested:
#
#   1. record the rehearsal "merge SHA": a version-bump commit on rig main
#      (the stand-in for the release-MR merge commit), version re-read AT
#      that SHA — never from working-tree memory
#   2. create the SSH-signed tag v<version> at exactly the recorded SHA
#      with the throwaway rig key; verify signature + target before pushing
#   3. push GitHub main FIRST, then the tag — the ordering the workflow's
#      tag-at-main-head assertion depends on (the tag push is the trigger)
#   4. poll the Actions release run to completion; green required
#   5. confirm TestPyPI holds the version with PEP 740 attestations
#      (integrity API provenance per file) and the GitHub Release exists
#   6. GitLab-side tag push, LAST (only after GitHub green). The rig has
#      NO GitLab-side mirror (README: Rig topology — scratch GitHub repo +
#      TestPyPI only), so this step is recorded as an explicit
#      skipped-in-rig receipt with the reason; the production ordering
#      (GitHub green before any GitLab push) is preserved by sequence for
#      the steps the rig does exercise, and the offline verifier asserts
#      the timestamp ordering whenever a gitlab push IS recorded.
#
# Then leg 2 is RE-ENTERED twice to prove the two idempotency branches the
# spec requires (spec §3 "Idempotency, precisely"):
#
#   re-entry A (refs current + green)  -> derived action is "skip": no new
#      tag, no new push, no new workflow run appears
#   re-entry B (refs current + red)    -> API workflow re-dispatch
#      converges: a non-success conclusion is manufactured WITHOUT touching
#      refs (re-run the green run via the API, cancel the fresh attempt —
#      the tag is immutable: never deleted, never re-pointed, never
#      re-signed), then the ladder's re-dispatch (API re-run keyed on the
#      recorded SHA) is exercised and polled back to green. skip-existing
#      means the re-run uploads nothing — the evidence records which run
#      actually uploaded (the first green run) and that the re-dispatched
#      attempt uploaded nothing (the skip-existing drift caveat).
#
# Evidence: docs/rehearsal/evidence-leg2.md — prose header + one fenced
# yaml block, re-checkable offline with --verify-evidence. The file is
# written ONLY from real run data, only after the whole walk passes; until
# then it carries status: pending and TBD placeholders — this script never
# fabricates SHAs, run URLs, conclusions, or listings. A per-run file in
# lib.sh's one-file-per-run format (scenario leg2-dry-run) is written too.
#
# Safety, in order, before anything else runs (same as negative-test.sh):
#   1. Production-target guard (offline, lib.sh) in EVERY mode.
#   2. R14 skip-clean: the rig-touching mode (--full) exits 0 with a clear
#      notice when the LMER_REHEARSAL_* credential variables are absent,
#      so this script verifies in a sandbox.
#   3. Drift guard: derive-workflow.py --check must be green immediately
#      before any rehearsal run (README: Workflow derivation).

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

LEG2_SCENARIO="leg2-dry-run"

usage() {
    cat <<'EOF'
usage: run-leg2.sh --full | --verify-evidence <file> | --write-pending <file>
                   | --derive-action <json>
                   [--dry-run] [--repo <owner/name>] [--project <name>]
                   [--env-file <path>] [--help]

Leg-2 dry run in the rehearsal rig (Ctl/rehearsal/README.md): signed tag
at a recorded merge SHA, GitHub main pushed before the tag, Actions run
polled to green, TestPyPI version with PEP 740 attestations, GitHub
Release present, GitLab-side step last (skipped-in-rig: the rig has no
GitLab mirror) — then two re-entries proving the idempotency branches
(refs current + green -> skip; refs current + red -> API re-dispatch
converges).

  --full             run the whole walk in the rig and rewrite the
                     evidence file (docs/rehearsal/evidence-leg2.md) from
                     the recorded results
  --verify-evidence  offline re-check of the evidence file; a pending
                     skeleton exits 0 with a loud PENDING notice, a
                     populated file gets the full consistency check
                     (ordering receipts, attestations, idempotency
                     branches, which-run-uploaded)
  --write-pending    write the pending evidence skeleton (refuses to
                     overwrite recorded evidence)
  --derive-action    offline: derive the leg-2 re-entry action from an
                     observed-state JSON document (fixture/test entry
                     point for the idempotency ladder)
  --dry-run          print the plan for --full; no network calls
  --repo             override LMER_REHEARSAL_REPO
  --project          override LMER_REHEARSAL_PROJECT
  --env-file         rig.env path (default: Ctl/rehearsal/rig.env)

Without the LMER_REHEARSAL_* credential variables, the rig-touching mode
(--full) SKIP-CLEANs (exit 0 with a notice). The production-target guard
always runs, offline, before anything else.
EOF
}

MODE=""
MODE_ARG=""
DRY_RUN=0
REPO_OVERRIDE=""
PROJECT_OVERRIDE=""
ENV_FILE=""

set_mode() {
    if [[ -n "$MODE" ]]; then
        rehearsal_err "run-leg2.sh: modes are mutually exclusive (--full, --verify-evidence, --write-pending, --derive-action)"
        usage >&2
        exit 2
    fi
    MODE=$1
}

need_value() {
    if [[ $# -lt 2 ]]; then
        rehearsal_err "run-leg2.sh: $1 requires a value"
        usage >&2
        exit 2
    fi
}

while (($#)); do
    case "$1" in
        --full)
            set_mode full
            ;;
        --verify-evidence|--write-pending|--derive-action)
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
            rehearsal_err "run-leg2.sh: unknown argument '$1'"
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ -z "$MODE" ]]; then
    rehearsal_err "run-leg2.sh: a mode is required"
    usage >&2
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

EVIDENCE_FILE="${REHEARSAL_EVIDENCE_DIR:-${REHEARSAL_ROOT}/docs/rehearsal}/evidence-leg2.md"

# --- Hard guard: offline, every mode, before any network call. -------------
rehearsal_guard "$REPO" "$PROJECT" "$REHEARSAL_TESTPYPI_URL"

note() {
    printf '==> %s\n' "$*"
}

now_utc() {
    date -u +%Y-%m-%dT%H:%M:%SZ
}

# ---------------------------------------------------------------------------
# The idempotency ladder — the re-entry decision, pure and offline, shared
# by the live re-entries, --derive-action (fixtures), and the tests. Same
# rows as the spec and taskdef/release-leg2.jinja2:
#   refs not current                       -> push-refs (advance the pushes)
#   refs current, no run yet               -> wait-for-run
#   refs current, run still going          -> poll
#   refs current, run green                -> skip
#   refs current, run red (any non-green
#   completed conclusion)                  -> redispatch (API re-run keyed
#                                             on the recorded SHA — never
#                                             re-tag)
# ---------------------------------------------------------------------------

leg2_derive_action() {
    local refs_current="$1" conclusion="$2"
    if [[ "$refs_current" != "true" ]]; then
        echo push-refs
        return 0
    fi
    case "$conclusion" in
        success)
            echo skip
            ;;
        none|null|"")
            echo wait-for-run
            ;;
        in_progress|queued|pending|waiting|requested)
            echo poll
            ;;
        *)
            echo redispatch
            ;;
    esac
}

# leg2_state_from_json <file> — read an observed-state JSON document
# ({"refs_current": bool, "run_conclusion": str|null}) and print two
# lines: refs_current (true/false) then run_conclusion.
leg2_state_from_json() {
    python3 -c '
import json
import sys

with open(sys.argv[1]) as handle:
    doc = json.load(handle)
refs = doc.get("refs_current")
print("true" if refs in (True, "true") else "false")
conclusion = doc.get("run_conclusion")
print("" if conclusion is None else conclusion)
' "$1"
}

do_derive_action() {
    local file="$1" refs conclusion action
    if [[ ! -f "$file" ]]; then
        rehearsal_err "derive-action: no such file: $file"
        exit 1
    fi
    { read -r refs; read -r conclusion; } < <(leg2_state_from_json "$file")
    action=$(leg2_derive_action "$refs" "$conclusion")
    printf 'state: refs_current=%s run_conclusion=%s\n' "$refs" "${conclusion:-none}"
    printf 'action: %s\n' "$action"
}

# ---------------------------------------------------------------------------
# Evidence file — prose header + one fenced yaml block (README: Evidence
# format), flat keys so lib.sh's parser (rehearsal_evidence_get) reads it
# unchanged. Written ONLY from real run data (status: complete) or as the
# all-TBD pending skeleton — never anything in between.
# ---------------------------------------------------------------------------

# Recorded values, empty until the walk runs; the writer emits TBD for
# anything unrecorded (pending skeleton).
MERGE_SHA=""
VERSION=""
TAG=""
TAG_SIGNATURE=""
MAIN_PUSHED_AT=""
TAG_PUSHED_AT=""
RUN_ID=""
RUN_URL=""
CONCLUSION=""
ACTIONS_GREEN_AT=""
FILES_BEFORE=""
FILES_AFTER=""
ATTESTATIONS=""
RELEASE_PRESENT=""
UPLOADED_BY=""
GITLAB_PUSH=""
GITLAB_REASON=""
IG_REFS=""
IG_CONCL=""
IG_ACTION=""
IG_NEW_RUNS=""
IR_REFS=""
IR_INITIAL=""
IR_REDISPATCH=""
IR_FINAL=""
IR_UPLOADED=""
IR_FILES_AFTER=""

write_evidence() {
    # write_evidence <pending|complete> <file>
    local status="$1" file="$2"
    mkdir -p "$(dirname -- "$file")"
    {
        printf '# Rehearsal evidence: leg-2 dry run\n\n'
        if [[ "$status" == "pending" ]]; then
            cat <<'EOF'
**STATUS: PENDING — the leg-2 dry run has NOT run yet.** Nothing below is
recorded evidence; every value is a TBD placeholder (the pending skeleton
carries no data at all).

Prerequisites before this file can be populated (all under Ctl/rehearsal):

- the rig is stood up and green: `stand-up.sh`, then `stand-up.sh --check`
  exits 0 (needs `rig.env` with the `LMER_REHEARSAL_*` credentials, and
  the TestPyPI trusted publisher registered per the printed manual steps)
- the workflow drift guard is green: `derive-workflow.py --check`
- the G2 negative tests have evidence:
  `negative-test.sh --verify-evidence docs/rehearsal/evidence-negative-test.md`

To populate: run `Ctl/rehearsal/run-leg2.sh --full` with the rig env
present — it walks the full leg-2 sequence in the rig (signed tag at the
recorded merge SHA, main before tag, Actions green, TestPyPI with
attestations, Release present, two idempotency re-entries) and REWRITES
this file with the recorded evidence. Re-check offline with
`Ctl/rehearsal/run-leg2.sh --verify-evidence <this file>`.
EOF
        else
            cat <<'EOF'
Recorded by `Ctl/rehearsal/run-leg2.sh --full` against the rehearsal rig
(Ctl/rehearsal/README.md). The full leg-2 walk: SSH-signed tag at exactly
the recorded merge SHA, GitHub main pushed before the tag (the ordering
the tag-at-main-head assertion depends on), Actions release run polled to
green, TestPyPI holding the version with PEP 740 attestations, GitHub
Release present, GitLab-side step last (skipped-in-rig: the rig topology
has no GitLab mirror) — then two re-entries proving the idempotency
branches: refs current + green -> skip (no new run), refs current + red
-> API re-dispatch converges (skip-existing: the re-run uploaded nothing;
`uploaded_by_run_id` names the run that actually uploaded). Re-check
offline with `Ctl/rehearsal/run-leg2.sh --verify-evidence <this file>`.
EOF
        fi
        printf '\n```yaml\n'
        printf 'status: %s\n' "$status"
        if [[ "$status" == "pending" ]]; then
            printf 'rig_repo: TBD\n'
            printf 'rig_project: TBD\n'
            printf 'derive_check: TBD\n'
            printf 'recorded_at: TBD\n'
        else
            printf 'rig_repo: %s\n' "$REPO"
            printf 'rig_project: %s\n' "$PROJECT"
            printf 'derive_check: pass\n'
            printf 'recorded_at: %s\n' "$(now_utc)"
        fi
        printf 'merge_sha: %s\n' "${MERGE_SHA:-TBD}"
        printf 'version: %s\n' "${VERSION:-TBD}"
        printf 'tag: %s\n' "${TAG:-TBD}"
        printf 'tag_sha: %s\n' "${MERGE_SHA:-TBD}"
        printf 'tag_signature: %s\n' "${TAG_SIGNATURE:-TBD}"
        printf 'main_pushed_at: %s\n' "${MAIN_PUSHED_AT:-TBD}"
        printf 'tag_pushed_at: %s\n' "${TAG_PUSHED_AT:-TBD}"
        printf 'workflow_run_id: %s\n' "${RUN_ID:-TBD}"
        printf 'workflow_run_url: %s\n' "${RUN_URL:-TBD}"
        printf 'expected_conclusion: success\n'
        printf 'recorded_conclusion: %s\n' "${CONCLUSION:-TBD}"
        printf 'actions_green_at: %s\n' "${ACTIONS_GREEN_AT:-TBD}"
        printf 'testpypi_files_before: %s\n' "${FILES_BEFORE:-TBD}"
        printf 'testpypi_files: %s\n' "${FILES_AFTER:-TBD}"
        printf 'attestations: %s\n' "${ATTESTATIONS:-TBD}"
        printf 'published: %s\n' "${FILES_AFTER:+true}"
        printf 'github_release: %s\n' "${RELEASE_PRESENT:-TBD}"
        printf 'uploaded_by_run_id: %s\n' "${UPLOADED_BY:-TBD}"
        printf 'gitlab_tag_push: %s\n' "${GITLAB_PUSH:-TBD}"
        printf 'gitlab_tag_push_reason: %s\n' "${GITLAB_REASON:-TBD}"
        printf 'idempotency_green.refs_current: %s\n' "${IG_REFS:-TBD}"
        printf 'idempotency_green.run_conclusion: %s\n' "${IG_CONCL:-TBD}"
        printf 'idempotency_green.action: %s\n' "${IG_ACTION:-TBD}"
        printf 'idempotency_green.new_runs: %s\n' "${IG_NEW_RUNS:-TBD}"
        printf 'idempotency_red.refs_current: %s\n' "${IR_REFS:-TBD}"
        printf 'idempotency_red.initial_conclusion: %s\n' "${IR_INITIAL:-TBD}"
        printf 'idempotency_red.redispatch: %s\n' "${IR_REDISPATCH:-TBD}"
        printf 'idempotency_red.final_conclusion: %s\n' "${IR_FINAL:-TBD}"
        printf 'idempotency_red.uploaded: %s\n' "${IR_UPLOADED:-TBD}"
        printf 'idempotency_red.testpypi_files_after: %s\n' "${IR_FILES_AFTER:-TBD}"
        printf '```\n'
    } > "$file"
}

# The pending writer emits `published:` empty via ${FILES_AFTER:+true};
# patch it to TBD so the skeleton is uniformly TBD. (Kept out of the main
# writer so the complete path stays a pure record of observed state.)
do_write_pending() {
    local file="$1"
    if [[ -f "$file" && "$(rehearsal_evidence_get "$file" status)" == "complete" ]]; then
        rehearsal_err "write-pending: ${file} carries recorded evidence (status: complete) — refusing to overwrite it with the skeleton"
        exit 1
    fi
    write_evidence pending "$file"
    sed -i 's/^published: $/published: TBD/' "$file"
    echo "wrote pending evidence skeleton: ${file}"
}

# ---------------------------------------------------------------------------
# Offline evidence verifier (--verify-evidence): pending skeleton -> loud
# notice + exit 0; populated file -> full consistency re-check (ordering
# receipts main-before-tag and GitHub-green-before-GitLab, attestation
# presence, idempotency branch records, which-run-uploaded consistency);
# anything malformed or inconsistent -> nonzero.
# ---------------------------------------------------------------------------

EV_ERRORS=0
ev_problem() {
    rehearsal_err "verify-evidence: $*"
    EV_ERRORS=$(( EV_ERRORS + 1 ))
}

TS_RE='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'

check_timestamp() {
    # check_timestamp <field> <value> — well-formed UTC ISO-8601 or problem.
    if [[ ! "$2" =~ $TS_RE ]]; then
        ev_problem "$1 '${2}' is not UTC ISO-8601 (YYYY-MM-DDThh:mm:ssZ)"
        return 1
    fi
    return 0
}

verify_leg2_evidence() {
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
PENDING — leg-2 dry run has not run yet
============================================================
${file} is the pending skeleton: the leg-2 walk has not been
executed in the rig and no evidence is recorded (TBD
placeholders only).
Populate it by running Ctl/rehearsal/run-leg2.sh --full with
the rig env (rig.env) present. Exiting 0: a pending skeleton
is well-formed, it just is not evidence.
EOF
            return 0
            ;;
        complete)
            ;;
        "")
            rehearsal_err "verify-evidence: ${file} has no status field — not a leg-2 evidence file (or malformed)"
            return 1
            ;;
        *)
            rehearsal_err "verify-evidence: unknown status '${status}' in ${file} (expected pending or complete)"
            return 1
            ;;
    esac

    EV_ERRORS=0

    # Every recorded field must be present and non-TBD in a complete file.
    # (gitlab_tag_push_reason / gitlab_tag_pushed_at are conditional on
    # the gitlab_tag_push branch and checked below.)
    local field value
    for field in rig_repo rig_project derive_check recorded_at merge_sha \
                 version tag tag_sha tag_signature main_pushed_at \
                 tag_pushed_at workflow_run_id workflow_run_url \
                 expected_conclusion recorded_conclusion actions_green_at \
                 testpypi_files_before testpypi_files attestations \
                 published github_release uploaded_by_run_id gitlab_tag_push \
                 idempotency_green.refs_current idempotency_green.run_conclusion \
                 idempotency_green.action idempotency_green.new_runs \
                 idempotency_red.refs_current idempotency_red.initial_conclusion \
                 idempotency_red.redispatch idempotency_red.final_conclusion \
                 idempotency_red.uploaded idempotency_red.testpypi_files_after; do
        value=$(rehearsal_evidence_get "$file" "$field")
        if [[ -z "$value" || "$value" == "TBD" ]]; then
            ev_problem "field '${field}' is missing or TBD in a status: complete file"
        fi
    done

    # Production-target guard from lib.sh — evidence claiming a production
    # target is inconsistent by definition.
    local rig_repo rig_project derive_check recorded_at
    rig_repo=$(rehearsal_evidence_get "$file" rig_repo)
    rig_project=$(rehearsal_evidence_get "$file" rig_project)
    derive_check=$(rehearsal_evidence_get "$file" derive_check)
    recorded_at=$(rehearsal_evidence_get "$file" recorded_at)
    rehearsal_guard_repo "$rig_repo" || ev_problem "rig_repo '${rig_repo}' fails the production-target guard"
    rehearsal_guard_project "$rig_project" || ev_problem "rig_project '${rig_project}' fails the production-target guard"
    if [[ "$derive_check" != "pass" ]]; then
        ev_problem "derive_check is '${derive_check}', must be 'pass'"
    fi
    if [[ -n "$recorded_at" && "$recorded_at" != "TBD" ]]; then
        check_timestamp recorded_at "$recorded_at" || true
    fi

    # The tag: at exactly the recorded merge SHA, named v<version>, with a
    # verified signature.
    local merge_sha version tag tag_sha tag_signature
    merge_sha=$(rehearsal_evidence_get "$file" merge_sha)
    version=$(rehearsal_evidence_get "$file" version)
    tag=$(rehearsal_evidence_get "$file" tag)
    tag_sha=$(rehearsal_evidence_get "$file" tag_sha)
    tag_signature=$(rehearsal_evidence_get "$file" tag_signature)
    if [[ -n "$merge_sha" && "$merge_sha" != "TBD" && ! "$merge_sha" =~ ^[0-9a-f]{40}$ ]]; then
        ev_problem "merge_sha '${merge_sha}' is not 40 lowercase hex"
    fi
    if [[ -n "$tag_sha" && "$tag_sha" != "TBD" && ! "$tag_sha" =~ ^[0-9a-f]{40}$ ]]; then
        ev_problem "tag_sha '${tag_sha}' is not 40 lowercase hex"
    fi
    if [[ "$tag_sha" != "$merge_sha" ]]; then
        ev_problem "tag_sha '${tag_sha}' does not equal merge_sha '${merge_sha}' — the tag must sit at exactly the recorded merge SHA"
    fi
    if [[ -n "$version" && "$version" != "TBD" && "$tag" != "v${version}" ]]; then
        ev_problem "tag '${tag}' is not v<version> for version '${version}'"
    fi
    if [[ "$tag_signature" != "verified" ]]; then
        ev_problem "tag_signature is '${tag_signature}', must be 'verified'"
    fi

    # Ordering receipts: main before tag, tag before green. UTC ISO-8601
    # timestamps compare lexicographically.
    local main_pushed_at tag_pushed_at actions_green_at
    main_pushed_at=$(rehearsal_evidence_get "$file" main_pushed_at)
    tag_pushed_at=$(rehearsal_evidence_get "$file" tag_pushed_at)
    actions_green_at=$(rehearsal_evidence_get "$file" actions_green_at)
    local ts_ok=1
    check_timestamp main_pushed_at "$main_pushed_at" || ts_ok=0
    check_timestamp tag_pushed_at "$tag_pushed_at" || ts_ok=0
    check_timestamp actions_green_at "$actions_green_at" || ts_ok=0
    if (( ts_ok )); then
        if [[ "$main_pushed_at" > "$tag_pushed_at" ]]; then
            ev_problem "ordering violated: main_pushed_at '${main_pushed_at}' is after tag_pushed_at '${tag_pushed_at}' — main must land before the tag triggers the workflow"
        fi
        if [[ "$tag_pushed_at" > "$actions_green_at" ]]; then
            ev_problem "ordering violated: tag_pushed_at '${tag_pushed_at}' is after actions_green_at '${actions_green_at}'"
        fi
    fi

    # The run: green, with the version published and attested.
    local run_id run_url expected recorded
    run_id=$(rehearsal_evidence_get "$file" workflow_run_id)
    run_url=$(rehearsal_evidence_get "$file" workflow_run_url)
    expected=$(rehearsal_evidence_get "$file" expected_conclusion)
    recorded=$(rehearsal_evidence_get "$file" recorded_conclusion)
    if [[ -n "$run_id" && "$run_id" != "TBD" && ! "$run_id" =~ ^[0-9]+$ ]]; then
        ev_problem "workflow_run_id '${run_id}' is not numeric"
    fi
    if [[ -n "$run_url" && "$run_url" != "TBD" && "$run_url" != https://* ]]; then
        ev_problem "workflow_run_url '${run_url}' is not an https URL"
    fi
    if [[ "$expected" != "success" ]]; then
        ev_problem "expected_conclusion is '${expected}', leg 2 must expect success"
    fi
    if [[ "$recorded" != "success" ]]; then
        ev_problem "recorded_conclusion is '${recorded}', not success — the release run was not green"
    fi

    local files_before files_after attestations published release
    files_before=$(rehearsal_evidence_get "$file" testpypi_files_before)
    files_after=$(rehearsal_evidence_get "$file" testpypi_files)
    attestations=$(rehearsal_evidence_get "$file" attestations)
    published=$(rehearsal_evidence_get "$file" published)
    release=$(rehearsal_evidence_get "$file" github_release)
    if [[ "$files_before" != "(none)" ]]; then
        ev_problem "testpypi_files_before is '${files_before}', must be (none) — a pre-existing listing makes which-run-uploaded unattributable"
    fi
    if [[ -z "$files_after" || "$files_after" == "(none)" || "$files_after" == "TBD" ]]; then
        ev_problem "testpypi_files is '${files_after}' — leg 2 must leave the version's files on TestPyPI"
    elif [[ -n "$version" && "$version" != "TBD" && "$files_after" != *"$version"* ]]; then
        ev_problem "testpypi_files '${files_after}' does not mention version '${version}'"
    fi
    if [[ "$attestations" != "present" ]]; then
        ev_problem "attestations is '${attestations}', must be 'present' (PEP 740 provenance for every published file)"
    fi
    if [[ "$published" != "true" ]]; then
        ev_problem "published is '${published}', leg 2 must record true"
    fi
    if [[ "$release" != "present" ]]; then
        ev_problem "github_release is '${release}', must be 'present'"
    fi

    # Which run actually uploaded (skip-existing drift caveat): the first
    # green run is the uploader; the re-dispatched attempt uploads nothing.
    local uploaded_by
    uploaded_by=$(rehearsal_evidence_get "$file" uploaded_by_run_id)
    if [[ -n "$uploaded_by" && "$uploaded_by" != "TBD" && ! "$uploaded_by" =~ ^[0-9]+$ ]]; then
        ev_problem "uploaded_by_run_id '${uploaded_by}' is not numeric"
    fi
    if [[ "$uploaded_by" != "$run_id" ]]; then
        ev_problem "uploaded_by_run_id '${uploaded_by}' does not name the recorded green run '${run_id}' — which-run-uploaded is inconsistent"
    fi

    # GitLab-side step, LAST: either an explicit skipped-in-rig receipt
    # with a reason (the rig has no GitLab mirror) or a recorded push that
    # happened only after GitHub was green.
    local gitlab_push gitlab_reason gitlab_pushed_at
    gitlab_push=$(rehearsal_evidence_get "$file" gitlab_tag_push)
    gitlab_reason=$(rehearsal_evidence_get "$file" gitlab_tag_push_reason)
    gitlab_pushed_at=$(rehearsal_evidence_get "$file" gitlab_tag_pushed_at)
    case "$gitlab_push" in
        skipped-in-rig)
            if [[ -z "$gitlab_reason" || "$gitlab_reason" == "TBD" ]]; then
                ev_problem "gitlab_tag_push is skipped-in-rig but gitlab_tag_push_reason is missing or TBD — the skip must be explicit and reasoned"
            fi
            ;;
        pushed)
            if [[ -z "$gitlab_pushed_at" || "$gitlab_pushed_at" == "TBD" ]]; then
                ev_problem "gitlab_tag_push is pushed but gitlab_tag_pushed_at is missing — the GitHub-green-before-GitLab ordering cannot be checked"
            elif check_timestamp gitlab_tag_pushed_at "$gitlab_pushed_at"; then
                if [[ -n "$actions_green_at" && "$actions_green_at" > "$gitlab_pushed_at" ]]; then
                    ev_problem "ordering violated: gitlab_tag_pushed_at '${gitlab_pushed_at}' is before actions_green_at '${actions_green_at}' — GitLab only after GitHub is green"
                fi
            fi
            ;;
        *)
            ev_problem "gitlab_tag_push is '${gitlab_push}', must be 'pushed' or 'skipped-in-rig'"
            ;;
    esac

    # Idempotency branch A: refs current + green -> skip, no new run.
    local ig_refs ig_concl ig_action ig_new_runs
    ig_refs=$(rehearsal_evidence_get "$file" idempotency_green.refs_current)
    ig_concl=$(rehearsal_evidence_get "$file" idempotency_green.run_conclusion)
    ig_action=$(rehearsal_evidence_get "$file" idempotency_green.action)
    ig_new_runs=$(rehearsal_evidence_get "$file" idempotency_green.new_runs)
    if [[ "$ig_refs" != "true" ]]; then
        ev_problem "idempotency_green.refs_current is '${ig_refs}', the branch requires refs current"
    fi
    if [[ "$ig_concl" != "success" ]]; then
        ev_problem "idempotency_green.run_conclusion is '${ig_concl}', the branch requires a green run"
    fi
    if [[ "$ig_action" != "skip" ]]; then
        ev_problem "idempotency_green.action is '${ig_action}', refs current + green must derive 'skip'"
    fi
    if [[ "$ig_new_runs" != "none" ]]; then
        ev_problem "idempotency_green.new_runs is '${ig_new_runs}', the skip branch must trigger no new run"
    fi

    # Idempotency branch B: refs current + red -> API re-dispatch converges,
    # and skip-existing means it uploaded nothing.
    local ir_refs ir_initial ir_redispatch ir_final ir_uploaded ir_files_after
    ir_refs=$(rehearsal_evidence_get "$file" idempotency_red.refs_current)
    ir_initial=$(rehearsal_evidence_get "$file" idempotency_red.initial_conclusion)
    ir_redispatch=$(rehearsal_evidence_get "$file" idempotency_red.redispatch)
    ir_final=$(rehearsal_evidence_get "$file" idempotency_red.final_conclusion)
    ir_uploaded=$(rehearsal_evidence_get "$file" idempotency_red.uploaded)
    ir_files_after=$(rehearsal_evidence_get "$file" idempotency_red.testpypi_files_after)
    if [[ "$ir_refs" != "true" ]]; then
        ev_problem "idempotency_red.refs_current is '${ir_refs}', the branch requires refs current (re-dispatch, never re-tag)"
    fi
    case "$ir_initial" in
        failure|cancelled|timed_out)
            ;;
        *)
            ev_problem "idempotency_red.initial_conclusion is '${ir_initial}', the branch requires a completed non-success conclusion"
            ;;
    esac
    case "$ir_redispatch" in
        api-rerun|workflow-dispatch)
            ;;
        *)
            ev_problem "idempotency_red.redispatch is '${ir_redispatch}', must be the API path (api-rerun or workflow-dispatch) — never a re-tag"
            ;;
    esac
    if [[ "$ir_final" != "success" ]]; then
        ev_problem "idempotency_red.final_conclusion is '${ir_final}', the re-dispatch must converge to success"
    fi
    if [[ "$ir_uploaded" != "false" ]]; then
        ev_problem "idempotency_red.uploaded is '${ir_uploaded}', must be false — skip-existing means the re-dispatched run uploads nothing"
    fi
    if [[ -n "$ir_files_after" && "$ir_files_after" != "TBD" && "$ir_files_after" != "$files_after" ]]; then
        ev_problem "idempotency_red.testpypi_files_after '${ir_files_after}' differs from testpypi_files '${files_after}' — the re-dispatched run changed the listing"
    fi

    if (( EV_ERRORS )); then
        rehearsal_err "verify-evidence: FAIL — ${EV_ERRORS} problem(s) in ${file}"
        return 1
    fi
    echo "verify-evidence: OK — ${file} (leg-2 dry run: ordering, attestations, both idempotency branches, which-run-uploaded all hold)"
    return 0
}

# ---------------------------------------------------------------------------
# Offline modes dispatch (no rig credentials required).
# ---------------------------------------------------------------------------

case "$MODE" in
    verify-evidence)
        verify_leg2_evidence "$MODE_ARG"
        exit
        ;;
    write-pending)
        do_write_pending "$MODE_ARG"
        exit 0
        ;;
    derive-action)
        do_derive_action "$MODE_ARG"
        exit 0
        ;;
esac

# ---------------------------------------------------------------------------
# The rig mode (--full) from here on.
# ---------------------------------------------------------------------------

print_plan() {
    cat <<EOF
dry-run (run-leg2 --full): plan only — no network calls made.
The full leg-2 walk in the rig, then two idempotency re-entries:
  1. record the rehearsal merge SHA (version-bump commit on rig main);
     re-read the version AT that SHA
  2. SSH-signed tag v<version> at exactly the recorded SHA (throwaway rig
     key); verify signature + target before any push
  3. push GitHub main FIRST, then the tag (the tag push triggers
     release.yml; its verify job asserts tag == main HEAD)
  4. poll the Actions run to completion; green required
  5. confirm TestPyPI holds the version with PEP 740 attestations and the
     GitHub Release exists; record which run uploaded
  6. GitLab-side tag push LAST — skipped-in-rig (the rig topology has no
     GitLab mirror), recorded explicitly with the reason
  re-entry A: refs current + green -> derived action skip, no new run
  re-entry B: refs current + red (manufactured via API re-run + cancel,
     refs untouched) -> API re-dispatch converges to green; skip-existing
     means it uploads nothing (recorded)
Evidence rewritten from recorded results only: ${EVIDENCE_FILE}
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
    rehearsal_skip_clean "run-leg2 --full" $missing
    exit 0
fi

if [[ -z "$REPO" || -z "$PROJECT" ]]; then
    rehearsal_err "run-leg2.sh: LMER_REHEARSAL_REPO and LMER_REHEARSAL_PROJECT must be set (rig.env)"
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
    rehearsal_err "run-leg2.sh: cannot fetch the rig repo's committed ${REHEARSAL_WORKFLOW_PATH} — run stand-up.sh to populate the rig repo"
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

testpypi_listing() {
    # testpypi_listing <project> <version> — comma-joined filenames, or
    # "(none)" when the version has no files on TestPyPI (the JSON API
    # 404s for an unknown version).
    local files
    files=$(rehearsal_testpypi_files "$1" "$2" 2>/dev/null | paste -sd, -) || files=""
    printf '%s\n' "${files:-(none)}"
}

wait_for_listing() {
    # wait_for_listing — poll until TestPyPI lists files for the version
    # (the index can lag the green run by a few seconds).
    local tries="${LEG2_LISTING_TRIES:-18}" files
    while (( tries > 0 )); do
        files=$(testpypi_listing "$PROJECT" "$VERSION")
        if [[ "$files" != "(none)" ]]; then
            printf '%s\n' "$files"
            return 0
        fi
        sleep "${LEG2_LISTING_INTERVAL:-10}"
        tries=$(( tries - 1 ))
    done
    rehearsal_err "leg2: TestPyPI never listed files for ${PROJECT} ${VERSION}"
    return 1
}

attestations_present() {
    # attestations_present <comma-joined filenames> — 0 iff EVERY published
    # file has PEP 740 provenance on TestPyPI's integrity API.
    local files_csv="$1" f
    local IFS=,
    for f in $files_csv; do
        if ! curl -sS --fail -o /dev/null \
            -H "Accept: application/vnd.pypi.integrity.v1+json" \
            "${REHEARSAL_TESTPYPI_URL}/integrity/${PROJECT}/${VERSION}/${f}/provenance"; then
            rehearsal_err "leg2: no PEP 740 provenance on TestPyPI for ${f}"
            return 1
        fi
    done
    return 0
}

wait_for_run() {
    # wait_for_run <tag> — poll until the release.yml run for the tag push
    # appears; print its id.
    local tag="$1" tries="${LEG2_RUN_FIND_TRIES:-36}" run_id=""
    while (( tries > 0 )); do
        run_id=$(rehearsal_find_run_for_tag "$REPO" "$tag" || true)
        if [[ -n "$run_id" ]]; then
            printf '%s\n' "$run_id"
            return 0
        fi
        sleep "${LEG2_RUN_FIND_INTERVAL:-5}"
        tries=$(( tries - 1 ))
    done
    rehearsal_err "leg2: no ${REHEARSAL_WORKFLOW_PATH} run appeared for tag ${tag}"
    return 1
}

run_count_for_tag() {
    # Number of release.yml runs for the tag push — the "no new run"
    # receipt for the green re-entry.
    rehearsal_gh_api GET "/repos/${REPO}/actions/runs?event=push&branch=${TAG}" |
        python3 -c '
import json
import sys

wf_path = sys.argv[1]
runs = json.load(sys.stdin).get("workflow_runs", [])
print(sum(1 for run in runs if run.get("path") == wf_path))
' "$REHEARSAL_WORKFLOW_PATH"
}

current_run_conclusion() {
    rehearsal_gh_api GET "/repos/${REPO}/actions/runs/${RUN_ID}" |
        rehearsal_json_get conclusion
}

observe_refs_current() {
    # true iff GitHub main AND the (peeled) tag ref both sit at the
    # recorded merge SHA — the precondition of both re-entry branches.
    local main_sha tag_sha
    main_sha=$(rehearsal_gh_api GET "/repos/${REPO}/branches/main" | rehearsal_json_get commit.sha)
    tag_sha=$(rig_git ls-remote --tags origin "refs/tags/${TAG}*" |
        awk '/\^\{\}$/ { print $1; exit }')
    if [[ -z "$tag_sha" ]]; then
        tag_sha=$(rig_git ls-remote origin "refs/tags/${TAG}" | awk '{ print $1; exit }')
    fi
    if [[ "$main_sha" == "$MERGE_SHA" && "$tag_sha" == "$MERGE_SHA" ]]; then
        echo true
    else
        echo false
    fi
}

manufacture_red() {
    # Manufacture a completed non-success conclusion WITHOUT touching refs
    # (the tag is immutable — never deleted, never re-pointed, never
    # re-signed): re-run the green run via the API, then cancel the fresh
    # attempt. "Red" here is any completed non-green conclusion; the
    # ladder's re-dispatch branch keys on exactly that.
    local tries=3 conclusion
    while (( tries > 0 )); do
        # rehearsal_rerun_run waits until the fresh attempt actually
        # starts: right after the POST the API can still serve the
        # PREVIOUS attempt's completed status, so cancelling/polling
        # immediately would act on a stale conclusion.
        rehearsal_rerun_run "$REPO" "$RUN_ID"
        rehearsal_gh_api POST "/repos/${REPO}/actions/runs/${RUN_ID}/cancel" >/dev/null 2>&1 || true
        conclusion=$(rehearsal_poll_run "$REPO" "$RUN_ID")
        if [[ "$conclusion" != "success" ]]; then
            printf '%s\n' "$conclusion"
            return 0
        fi
        rehearsal_err "leg2: manufactured re-run went green before the cancel landed; retrying"
        tries=$(( tries - 1 ))
    done
    rehearsal_err "leg2: could not manufacture a red run (the re-run kept winning the cancel race)"
    return 1
}

# --- The walk. -------------------------------------------------------------

RUN_STAMP=$(date -u +%s)
VERSION="0.0.${RUN_STAMP}"
TAG="v${VERSION}"

# Step 1 — record the rehearsal merge SHA: a version-bump commit on rig
# main plays the release-MR merge commit; the version is then re-read AT
# that SHA (leg-2 step 1: never working-tree memory).
ensure_clone
note "step 1: recording the rehearsal merge SHA (version ${VERSION})"
rig_git checkout --quiet main
if ! grep -q '^version = ' "${RIG_CLONE}/pyproject.toml" 2>/dev/null; then
    rehearsal_err "leg2: the rig clone has no pyproject.toml with a version line — run stand-up.sh to populate the rig repo"
    exit 1
fi
sed -i "s/^version = .*/version = \"${VERSION}\"/" "${RIG_CLONE}/pyproject.toml"
rig_git commit --quiet -am "rehearsal leg2: version ${VERSION}"
MERGE_SHA=$(rig_git rev-parse HEAD)
observed=$(rig_git show "${MERGE_SHA}:pyproject.toml" |
    sed -n 's/^version = "\(.*\)"$/\1/p' | head -n 1)
if [[ "$observed" != "$VERSION" ]]; then
    rehearsal_err "leg2: version re-read at ${MERGE_SHA} is '${observed}', expected '${VERSION}' — HARD STOP"
    exit 1
fi
note "step 1: merge SHA ${MERGE_SHA}, version ${VERSION} (re-read at that SHA)"

# Step 3 (rig) — the SSH-signed tag at exactly the merge SHA, verified
# (signature against the rig key's public half, target against the SHA)
# BEFORE anything is pushed.
note "step 2: signed tag ${TAG} at ${MERGE_SHA}"
rig_git -c user.signingkey="${LMER_REHEARSAL_SIGNING_KEY}" tag -s "$TAG" "$MERGE_SHA" \
    -m "Rehearsal release ${VERSION}"
allowed_signers="${WORK_DIR}/allowed-signers"
printf '* %s\n' "$(ssh-keygen -y -f "${LMER_REHEARSAL_SIGNING_KEY}")" > "$allowed_signers"
rig_git -c gpg.ssh.allowedSignersFile="$allowed_signers" tag -v "$TAG" >/dev/null 2>&1 || {
    rehearsal_err "leg2: tag signature did not verify against the rig key"
    exit 1
}
if [[ "$(rig_git rev-parse "${TAG}^{commit}")" != "$MERGE_SHA" ]]; then
    rehearsal_err "leg2: tag ${TAG} does not point at the recorded merge SHA"
    exit 1
fi
TAG_SIGNATURE=verified

FILES_BEFORE=$(testpypi_listing "$PROJECT" "$VERSION")
if [[ "$FILES_BEFORE" != "(none)" ]]; then
    rehearsal_err "leg2: TestPyPI already lists files for ${VERSION} ('${FILES_BEFORE}') — which-run-uploaded would be unattributable; aborting"
    exit 1
fi

# Step 4 (rig) — GitHub: main FIRST, then the tag. The ordering is
# load-bearing: the tag push triggers release.yml and its verify job
# asserts tag == main HEAD.
note "step 3: pushing main first, then the tag"
rig_git push --quiet origin main
MAIN_PUSHED_AT=$(now_utc)
rig_git push --quiet origin "refs/tags/${TAG}"
TAG_PUSHED_AT=$(now_utc)

# Step 5 (rig) — poll the Actions run to completion.
note "step 4: waiting for the release.yml run"
RUN_ID=$(wait_for_run "$TAG")
RUN_URL="https://github.com/${REPO}/actions/runs/${RUN_ID}"
CONCLUSION=$(rehearsal_poll_run "$REPO" "$RUN_ID" "${LEG2_POLL_TIMEOUT:-900}")
note "step 4: run ${RUN_ID} concluded '${CONCLUSION}'"
if [[ "$CONCLUSION" != "success" ]]; then
    rehearsal_err "leg2: release run red — FAIL LOUDLY with the run URL, nothing published internally: ${RUN_URL}"
    exit 1
fi
ACTIONS_GREEN_AT=$(now_utc)

# TestPyPI holds the version with PEP 740 attestations; the Release exists.
note "step 5: TestPyPI listing + attestations + GitHub Release"
FILES_AFTER=$(wait_for_listing)
attestations_present "$FILES_AFTER"
ATTESTATIONS=present
if ! rehearsal_release_lookup "$REPO" "$TAG" >/dev/null 2>&1; then
    rehearsal_err "leg2: no GitHub Release exists for ${TAG}"
    exit 1
fi
RELEASE_PRESENT=present
# Which run uploaded: the listing went (none) -> files across exactly this
# run, so this run is the uploader (skip-existing makes later green runs
# upload nothing — recorded below in the red re-entry).
UPLOADED_BY=$RUN_ID

# Step 6 (rig) — GitLab-side tag push, LAST. The rig has no GitLab-side
# mirror (README: Rig topology), so this is an explicit, reasoned skip —
# never a silent omission. The production ordering (only after GitHub
# green) is preserved by this step's position in the sequence.
GITLAB_PUSH="skipped-in-rig"
GITLAB_REASON="rig topology has no GitLab-side mirror (README: scratch GitHub repo + TestPyPI only); production step 6 ordering — GitLab tag push only after GitHub green — is preserved by sequence for the steps the rig exercises"
note "step 6: GitLab-side tag push — ${GITLAB_PUSH}"

# Re-entry A — refs current + green -> skip (and no new run appears).
note "re-entry A: refs current + green"
runs_before=$(run_count_for_tag)
IG_REFS=$(observe_refs_current)
IG_CONCL=$(current_run_conclusion)
IG_ACTION=$(leg2_derive_action "$IG_REFS" "$IG_CONCL")
if [[ "$IG_ACTION" != "skip" ]]; then
    rehearsal_err "leg2: re-entry A derived '${IG_ACTION}' (refs_current=${IG_REFS}, conclusion=${IG_CONCL}), expected skip"
    exit 1
fi
runs_after=$(run_count_for_tag)
if [[ "$runs_after" != "$runs_before" ]]; then
    rehearsal_err "leg2: re-entry A saw new runs appear (${runs_before} -> ${runs_after}) — the skip branch must not trigger anything"
    exit 1
fi
IG_NEW_RUNS=none
note "re-entry A: derived action skip, no new run"

# Re-entry B — refs current + red -> API re-dispatch converges.
note "re-entry B: refs current + red (manufacturing a non-success conclusion)"
IR_INITIAL=$(manufacture_red)
IR_REFS=$(observe_refs_current)
action=$(leg2_derive_action "$IR_REFS" "$IR_INITIAL")
if [[ "$action" != "redispatch" ]]; then
    rehearsal_err "leg2: re-entry B derived '${action}' (refs_current=${IR_REFS}, conclusion=${IR_INITIAL}), expected redispatch"
    exit 1
fi
note "re-entry B: run ${RUN_ID} is '${IR_INITIAL}' with refs current — re-dispatching via the API (never re-tagging)"
# rehearsal_rerun_run waits for the fresh attempt to start, so the poll
# below cannot return the manufactured red conclusion as a stale read.
rehearsal_rerun_run "$REPO" "$RUN_ID"
IR_REDISPATCH="api-rerun"
IR_FINAL=$(rehearsal_poll_run "$REPO" "$RUN_ID" "${LEG2_POLL_TIMEOUT:-900}")
note "re-entry B: re-dispatched run concluded '${IR_FINAL}'"
if [[ "$IR_FINAL" != "success" ]]; then
    rehearsal_err "leg2: re-dispatch did not converge to green: ${RUN_URL}"
    exit 1
fi
IR_FILES_AFTER=$(testpypi_listing "$PROJECT" "$VERSION")
if [[ "$IR_FILES_AFTER" != "$FILES_AFTER" ]]; then
    rehearsal_err "leg2: the re-dispatched run changed the TestPyPI listing ('${FILES_AFTER}' -> '${IR_FILES_AFTER}') — skip-existing did not hold"
    exit 1
fi
IR_UPLOADED=false

# Evidence — the aggregate file plus a per-run file in lib.sh's format.
write_evidence complete "$EVIDENCE_FILE"
per_run=$(rehearsal_evidence_write \
    "scenario=${LEG2_SCENARIO}" \
    "rig_repo=${REPO}" \
    "rig_project=${PROJECT}" \
    "tag=${TAG}" \
    "tag_sha=${MERGE_SHA}" \
    "workflow_run_id=${RUN_ID}" \
    "workflow_run_url=${RUN_URL}" \
    "expected_conclusion=success" \
    "recorded_conclusion=${CONCLUSION}" \
    "published=true" \
    "derive_check=pass")
note "leg-2 walk complete; evidence: ${EVIDENCE_FILE} (per-run: ${per_run})"
# Immediately re-check the evidence we just wrote, offline.
verify_leg2_evidence "$EVIDENCE_FILE"
