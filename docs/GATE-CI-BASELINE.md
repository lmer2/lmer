# Initial Baseline from CI

How `gate-check --baseline` lets a development run skip re-running the test suite on code it has not touched yet, when trusted CI already ran that suite on the exact same commit — and when it refuses to.

- [What it replaces](#what-it-replaces)
- [Turning it on](#turning-it-on)
- [The coverage mapping](#the-coverage-mapping)
- [Definition pins](#definition-pins)
- [The decision](#the-decision)
- [The evidence token](#the-evidence-token)
- [The smoke check](#the-smoke-check)
- [Outcomes and exit codes](#outcomes-and-exit-codes)
- [The receipt](#the-receipt)
- [What it never does](#what-it-never-does)

## What it replaces

A development run starts with a clean-state gate: before changing anything, it proves that the starting code passes and that the worker can run it. The first half is often already known. If CI already ran the project's lint and full-suite jobs on the exact commit the worker checked out, a local re-run tells nothing new and costs the length of the suite.

`gate-check --baseline` establishes the first half from CI evidence and the second half from a short, bounded local smoke check. Only the initial baseline uses it. Every later gate — after the first edit, before a commit, before a push — is the normal gate.

## Turning it on

Two things are needed. If either is missing, `gate-check --baseline` says why and runs the normal full `gate-check`.

1. **The toggle**, which is on for this session. It is set on the host on the same scoped ladder as `LMER_MEMORY`/`LMER_CPUS`/`LMER_GPU`, read most specific first:
   - `LMER_<TASK>__<REPO>__GATE_CI_BASELINE`, e.g. `LMER_DEVELOP__AGENTS_GLOBAL__GATE_CI_BASELINE=1`
   - `LMER_<TASK>_GATE_CI_BASELINE`, e.g. `LMER_DEVELOP_GATE_CI_BASELINE=1`
   - `LMER_GATE_CI_BASELINE`

   It is off by default. It accepts `1`/`true`/`yes` and `0`/`false`/`no`, so an explicit `0` at a specific tier switches one repository off under a broader on. Blank and invalid values fall through to the next tier. The launching CLI resolves the ladder and passes one resolved `LMER_GATE_CI_BASELINE` into the session container, and the gate reads only that value. A `--verbose` launch shows it on the `📐 Limits:` line (`gate-ci-baseline=on (LMER_DEVELOP__AGENTS_GLOBAL__GATE_CI_BASELINE)`), and `lmer --show-env` lists whichever spellings are set. See **Scoped container resources** in [LMER-CLI.md](./LMER-CLI.md) for the naming and tier rules, and for forwarding through the platform container.
2. **A coverage mapping** for the project, in the work repo (next section). The mapping declares which jobs count. It does not switch anything on.

The command is safe to name in a taskdef before every image carries it: older `gate-check` builds ignore arguments they do not know, so `gate-check --baseline` on them is plain `gate-check`.

## The coverage mapping

The mapping says which CI jobs make up the baseline and what each one covers. It is read **only** from the work repo's `{host}/{project}/info/gate-check.yaml`, never from the repository being gated. It is a setting that skips a check, so the code being gated must not be able to write it. The same rule applies to `secrets.ignore` and pre-commit reuse.

```yaml
ci_baseline:
  provider: gitlab          # the only supported provider
  jobs:                     # exact CI job name -> what it covers, and its audited definition
    pre-commit:
      covers: lint
      definition: "sha256:40db3c71…"
    pytest:
      covers: tests
      definition: "sha256:8185e0b1…"
  smoke:
    timeout: 300            # seconds for the whole smoke check (default 300)
    tools: [git, jq]        # commands that must be on PATH
```

- Job names are matched **exactly**, and each must be a top-level entry in the CI configuration. A job split with `parallel:` or `parallel:matrix` runs under names such as `pytest 1/2` that no entry carries, so it can never be pinned. A mapping that declares one always falls back. Parallel jobs cannot be reused yet.
- At least one job must cover `tests`. Every declared job is required.
- Only declare a job as `tests` after auditing that it runs the project's full suite. The pin (next section) holds the gate to the job you audited.
- Jobs that are not declared, such as builds, image pushes and publishing, are ignored either way.
- A job may also be given as `pytest: tests`, without a pin. It is then never reused, and the gate prints the definition it found so it can be audited and pinned.

A mapping that fails to parse is treated as absent.

## Definition pins

The coverage claim is about one job definition. A later change can keep the name `pytest` and narrow what it runs, for example with `-m "not slow"`, a `--deselect`, or a pipeline-wide `PYTEST_ADDOPTS` in `variables:` or `workflow:rules:variables`. A pin makes such a change fall back to the full gate instead of reusing CI on a subset.

`definition` is a SHA-256 digest of everything in the CI configuration that decides what the job runs:

- the job's own entry;
- every entry it reaches through `extends` or `!reference`;
- the inherited top-level keys: `variables`, `default`, `image`, `services`, `before_script`, `after_script`, `cache` and `workflow`.

The configuration is the one GitLab's CI lint API returns for the exact commit (`GET /projects/:id/ci/lint?content_ref=<sha>`), with includes resolved. The digest is deliberately wider than what GitLab merges into the job. Any change to the job's inputs moves it, including harmless ones such as a new `timeout`. Changes to other jobs leave it alone.

To pin, or to re-pin after auditing a CI change, run this on a commit CI has the configuration for:

```bash
gate-check --baseline --print-pins          # at HEAD
gate-check --baseline --print-pins <rev>    # at another commit
```

It prints the mapping's `jobs` block with each job's current `definition`, ready to paste into the work repo's `gate-check.yaml`. It reads only: it runs no checks and records no receipt, and the toggle need not be on. It exits 1 when a declared job is not in the configuration.

A pin does not cover:

- **Files a job's script calls.** A wrapper script in the repository is the same file locally and in CI at the same commit, but the pin does not notice it changing.
- **Includes that float.** These are resolved as of the gate run, not as of the pipeline: a remote URL, or a project or template include without a fixed ref.
- **Includes conditional on the pipeline.** The lint call has no pipeline context. An `include:` whose `rules:` test `$CI_PIPELINE_SOURCE` or `$CI_MERGE_REQUEST_*` is likely left out of the configuration it returns, so a change to that file would not move the pin. This has not been measured against a server.
- **CI inputs.** For the same reason, `spec:inputs` are likely interpolated with their defaults, not with the values a pipeline was given. This has not been measured against a server either.

## The decision

The gate reads the forge's own records for the full `HEAD` SHA in the `origin` project:

1. the project, resolved from `origin`, which must match `LMER_REPO_URL` when that is set;
2. every pipeline whose `sha` is that exact commit in that project;
3. every job in those pipelines, retried attempts included.

For each declared job, the **latest attempt** (the highest job id) decides:

| Latest attempt | Job verdict |
|---|---|
| `success` | passed |
| `failed`, including `allow_failure: true` | failed |
| unfinished, canceled, skipped or manual, and the most recent `success`/`failed` attempt before it failed | failed: a known failure stands until a pass supersedes it |
| unfinished, canceled, skipped or manual, otherwise (including on top of a passed retry) | unverified |
| no attempt at all | unverified |

Overall: any failed job means **FAILED**, otherwise any unverified job means **UNVERIFIED**, otherwise **PASSED**. The overall pipeline status is never read. A red pipeline caused by a failed publish job still allows reuse, and a green pipeline whose test job failed with `allow_failure` does not.

The following are all UNVERIFIED and fall back to the normal gate:

- the tree is not clean (untracked files included);
- `origin` is not the run's repository;
- the project uses its own test runner (`gate-check-run-tests.sh` or a parallel runner);
- there is no `tests/` directory;
- no token, an API or transport error, unreadable output, more records than the gate reads, or the evidence budget running out (below);
- the pipeline-variables read refused with 403, which happens below the Maintainer role. This reason is specific, not a generic API failure: it names the refused credential and the fix (see [The evidence token](#the-evidence-token));
- a passing job whose definition, trigger or variables fail the confirmation step (below);
- an unexpected error anywhere in the evaluation before the local checks start;
- no pipeline at the commit.

A PASSED verdict then has to survive a confirmation step, per declared job. A job that fails it is UNVERIFIED, and so is the verdict:

- **Definition.** The job's definition at this commit must equal its pin. A missing pin, a mismatch, or a job absent from the configuration all fail. The fallback message names the job and both digests.
- **Trigger.** The pipeline holding the deciding attempt must come from a `push` or `merge_request_event`. Web, API, schedule, trigger and child pipelines fail, because they can run the job with inputs the commit does not record.
- **Variables.** That pipeline must have been created with no pipeline variables (`GET /projects/:id/pipelines/:id/variables`). This also catches `git push -o ci.variable=…`. Only the variable names are recorded, never their values. GitLab answers this read with 403 to anyone below Maintainer, so a Developer run token always falls back here unless an [evidence token](#the-evidence-token) is set.

Project-, group- and instance-level CI/CD variables are not read. They need the Maintainer role and are settings at the same trust level as the mapping itself. Variables given when a manual job is played are not read either. A pinned `when: manual` on a declared job shows that it can happen.

The evidence read has one 120 s budget. No request starts after it, and an answer that completes after it is discarded, so the gate falls back. A request already in flight can still hold the gate past 120 s, for up to the client's 30 s socket timeout per read on a stalled connection.

The decision reads only API facts: pipeline sha, project, source and variables; job name, status, `allow_failure` and id; and the CI configuration at the commit. Job logs are never read.

The gate prints which credential it reads with before it starts, and prints the evidence, and any fallback reason, as soon as they are known. A redirected `gate-check --baseline` is block-buffered, so without that the reason would only appear when the full gate behind it exits.

## The evidence token

The variables read needs the Maintainer role. A run account should not need Maintainer, because that role also brings merge and protected-branch rights. Instead, give the gate a separate token that can read but not write:

- **Variable:** `LMER_CI_BASELINE_TOKEN_<host>`, with the host sanitized as for `GITLAB_TOKEN_<host>`: lowercased, with dots and hyphens turned into underscores. For example, `git.example.com` becomes `LMER_CI_BASELINE_TOKEN_git_example_com`. There is no generic form; each variable names one host. As with `GITLAB_TOKEN_<host>`, two hosts that differ only by `.` versus `-` share a variable name.
- **Use:** when it is set, it is used for **every** evidence read of `gate-check --baseline` and `--print-pins` (project, pipelines, jobs, CI lint and pipeline variables), and the run token is not used for the decision at all. When it is unset or blank, the gate uses the run token exactly as before. When a read made with it fails (an expired or revoked token answers 401 on the first read), the fallback reason names the variable. The gate never retries with the run token.
- **Token:** role **Maintainer**, scope **`read_api` only**. `read_api` permits only `GET` requests, so the token cannot push, merge or change settings. Either:
  - a **project access token** (project → Settings → Access tokens), which reads only that project. **Prefer this.**
  - a **group access token** (group → Settings → Access tokens), which reads every project in the group. Use it only when several projects on the host use the baseline, since there is one variable per host.

  Give it an expiry date and rotate it like any other access token.
- **What it can read:** read-only does not mean harmless. A Maintainer can read things a Developer cannot, among them the **values of the project's CI/CD variables**, masked and protected ones included (`GET /projects/:id/variables`). With a group token, that covers every project in the group. Treat it as a secret at that level.
- **Where:** in `~/.lmer/.env`, beside `GITLAB_TOKEN_<host>`:

  ```bash
  LMER_CI_BASELINE_TOKEN_git_example_com=<the token>
  ```

  lmer merges `.env` files into the session environment, but passes `LMER_CI_BASELINE_TOKEN_*` only into a session whose **resolved** `LMER_GATE_CI_BASELINE` is on (see [Turning it on](#turning-it-on)). Which sessions those are depends on the tier you turned it on at. The bare global `LMER_GATE_CI_BASELINE=1` turns it on for every session that no narrower tier switches off, chat and review included, and so hands each of them the token. Turn it on with a task-scoped form instead, such as `LMER_DEVELOP_GATE_CI_BASELINE=1` or `LMER_DEVELOP__<REPO>__GATE_CI_BASELINE=1`. A baseline session does receive the token, and any agent in it could read what the token can read. A variable that is only exported in the host shell does not reach the session.

  Do **not** put it in the lmer checkout's own `.env`. In developer mode that file is mounted into every session at `/Agents/global/.env`, whatever the toggle says, and the environment filter cannot reach a mounted file.

The gate shows only the variable's name: on the `Reading CI evidence with …` line, in fallback reasons, and as `credential` in the receipt. A token, evidence or run, that is not printable latin-1 text (a control character inside the value, such as a pasted CR, or a non-latin-1 character) is refused before any request, with `no GitLab API access: … is not a valid token (control or non-latin-1 characters)` and no value. The evidence token is trimmed of surrounding whitespace first; the run token is checked as given, so a trailing CR or TAB on it is refused too.

When the variables read is refused, the fallback reason says which credential was refused and what to do:

```text
cannot read pipeline variables: GitLab refused the run token (403); it needs the Maintainer role, or set LMER_CI_BASELINE_TOKEN_git_example_com to a Maintainer read_api token (see docs/GATE-CI-BASELINE.md)
cannot read pipeline variables: GitLab refused LMER_CI_BASELINE_TOKEN_git_example_com (403); it needs the Maintainer role (see docs/GATE-CI-BASELINE.md)
```

The variables guard itself is unchanged: with or without the evidence token, the deciding pipeline's variables are read, and a pipeline that has any is unverified.

## The smoke check

After a PASSED verdict, the gate checks that this worker can run the suite at all. Everything below shares one deadline (`smoke.timeout`):

- every declared tool is on `PATH`;
- an interpreter that imports pytest, chosen as the tests check chooses it (the project `.venv` first);
- `python -m pytest --collect-only -q -p no:cacheprovider tests/ --ignore=tests/test_container_build.py`, with `PYTHONDONTWRITEBYTECODE=1`. Collection imports every test module and therefore the source and its dependencies, without running a test. It writes nothing into the checkout, so the clean-tree check that follows still sees the commit CI tested.

The interpreter probe and the collection each run in their own process group. On a timeout the whole group is killed, so nothing a `conftest.py` started keeps running into the full gate that follows. A descendant that starts its own session leaves the group and escapes this.

On this repository collection takes about 13–50 s, against a full suite of tens of minutes.

## Outcomes and exit codes

| Outcome | What happens | Exit |
|---|---|---|
| **reused** | smoke passed; every other gate check (git status through pre-commit) ran locally and passed; the suite was not run | 0 |
| **ci-failed** | CI already failed a required job at this commit; the job evidence is printed; nothing runs locally | 3 |
| **local-failed** | the smoke check failed (an environment problem to diagnose), or a local gate check failed | 1 |
| **fallback** | UNVERIFIED, the flag is off, there is no mapping, or the smoke check timed out; the normal full `gate-check` runs | that gate's |

A CI failure is the baseline failure itself. Treat it as you would a failing clean-state `gate-check`: stop and ask the operator. Do not re-run the suite locally to get a different answer.

## The receipt

Every `--baseline` run that reaches a decision records a gate receipt named **`gate-baseline`**, deliberately not `gate-check`, with outcome `pass`, `fail` or `fallback`. Its `ci_baseline` field carries:

- `decision` and `reason`;
- `evidence`: provider, host, project, project id, full sha, `credential` (the evidence token's variable name, or `the run token`), `completed_at` (when the last deciding job finished), the pipelines (id, status, source, ref, link), and per job its `covers`, verdict, the deciding attempt and **every** attempt (job id, pipeline id, status, `allow_failure`, finished time, link), plus, once confirmed, its `definition`, `pinned` digest and `pipeline_source`;
- `smoke`: outcome, duration, detail and interpreter.

A reused receipt sets `test_scope: ci baseline` and `test_cache_verdict: not-applicable`. It never claims the suite ran locally. A fallback is followed by the ordinary `gate-check` receipt of the full gate that ran.

## What it never does

- It never writes the gate's test-result cache (see [GATE-FASTPATH.md](./GATE-FASTPATH.md)). A CI baseline cannot answer for a later `gate-check`, `gate-commit` or `gate-push`, so changed source always goes through the normal gate.
- It never waits on CI. A pipeline that is still running is UNVERIFIED, and the run proceeds with the local gate.
- It never infers coverage from job names, and never substitutes an older passing attempt for a newer one.
