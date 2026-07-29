# Rehearsal evidence: G2 negative tag-verification tests

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

```yaml
status: pending
rig_repo: TBD
rig_project: TBD
main_head_sha: TBD
derive_check: TBD
recorded_at: TBD
negative-unsigned-tag.tag: TBD
negative-unsigned-tag.tag_sha: TBD
negative-unsigned-tag.workflow_run_id: TBD
negative-unsigned-tag.workflow_run_url: TBD
negative-unsigned-tag.expected_conclusion: failure
negative-unsigned-tag.recorded_conclusion: TBD
negative-unsigned-tag.failed_job: TBD
negative-unsigned-tag.jobs: TBD
negative-unsigned-tag.testpypi_files_before: TBD
negative-unsigned-tag.testpypi_files_after: TBD
negative-unsigned-tag.published: TBD
negative-unsigned-tag.github_release: TBD
negative-wrong-signer.tag: TBD
negative-wrong-signer.tag_sha: TBD
negative-wrong-signer.workflow_run_id: TBD
negative-wrong-signer.workflow_run_url: TBD
negative-wrong-signer.expected_conclusion: failure
negative-wrong-signer.recorded_conclusion: TBD
negative-wrong-signer.failed_job: TBD
negative-wrong-signer.jobs: TBD
negative-wrong-signer.testpypi_files_before: TBD
negative-wrong-signer.testpypi_files_after: TBD
negative-wrong-signer.published: TBD
negative-wrong-signer.github_release: TBD
negative-not-main-head.tag: TBD
negative-not-main-head.tag_sha: TBD
negative-not-main-head.workflow_run_id: TBD
negative-not-main-head.workflow_run_url: TBD
negative-not-main-head.expected_conclusion: failure
negative-not-main-head.recorded_conclusion: TBD
negative-not-main-head.failed_job: TBD
negative-not-main-head.jobs: TBD
negative-not-main-head.testpypi_files_before: TBD
negative-not-main-head.testpypi_files_after: TBD
negative-not-main-head.published: TBD
negative-not-main-head.github_release: TBD
```
