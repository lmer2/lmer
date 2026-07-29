# Rehearsal evidence: leg-2 dry run

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

```yaml
status: pending
rig_repo: TBD
rig_project: TBD
derive_check: TBD
recorded_at: TBD
merge_sha: TBD
version: TBD
tag: TBD
tag_sha: TBD
tag_signature: TBD
main_pushed_at: TBD
tag_pushed_at: TBD
workflow_run_id: TBD
workflow_run_url: TBD
expected_conclusion: success
recorded_conclusion: TBD
actions_green_at: TBD
testpypi_files_before: TBD
testpypi_files: TBD
attestations: TBD
published: TBD
github_release: TBD
uploaded_by_run_id: TBD
gitlab_tag_push: TBD
gitlab_tag_push_reason: TBD
idempotency_green.refs_current: TBD
idempotency_green.run_conclusion: TBD
idempotency_green.action: TBD
idempotency_green.new_runs: TBD
idempotency_red.refs_current: TBD
idempotency_red.initial_conclusion: TBD
idempotency_red.redispatch: TBD
idempotency_red.final_conclusion: TBD
idempotency_red.uploaded: TBD
idempotency_red.testpypi_files_after: TBD
```
