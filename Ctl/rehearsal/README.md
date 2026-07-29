# Release rehearsal rig

Design for the rehearsal rig required by the release-flow spec (deliverable
4): a scratch GitHub repo plus a TestPyPI trusted publisher that exercises
the full leg-2 path — including the negative tag-verification test — before
the first production release. This document is the frozen design; the
scripts in this directory implement it and must not deviate without
updating it first.

Pieces in this directory:

- `stand-up.sh` — creates/configures the rig from `rig.env` parameters;
  `--teardown` reverses it.
- `lib.sh` — shared helpers, including `--verify-evidence` (offline
  re-check of evidence files).
- `rig.env.example` — the full parameter surface; copy to `rig.env` (git-
  ignored) and fill in.
- `derive-workflow.py` — derives the rig workflow from the production
  workflow; `--check` is the drift guard.

## Rig topology

- **Scratch GitHub repo:** `lmer-rehearsal` under the bot GitHub account
  (parameter `LMER_REHEARSAL_REPO`, default `<bot>/lmer-rehearsal`).
  Private is fine; TestPyPI trusted publishing does not require a public
  repo. Rationale: same account that owns the production mirror, so repo
  controls (branch protection, tag protection with bot bypass, environment
  tag-pattern policy) are configured identically and the rehearsal actually
  exercises them.
- **TestPyPI project:** `lmer-rehearsal` (parameter
  `LMER_REHEARSAL_PROJECT`). Never `lmer`: the rehearsal must not squat or
  collide with the real name anywhere, and rehearsal artifacts must be
  unmistakably non-production. `stand-up.sh` rewrites `project.name` in the
  rig repo's `pyproject.toml` to the rehearsal project name (creating the
  minimal scratch `pyproject.toml` when absent — the import package is a
  rig-only placeholder; the distribution name is what matters).
- **Rig repo contents** (committed to rig `main` by `stand-up.sh`,
  idempotently — without them a tag push triggers no workflow at all):
  the derived `.github/workflows/release.yml` (from `derive-workflow.py
  --emit`), `.github/scripts/verify-tag-signature.sh` and
  `.github/scripts/gate-version-reuse.py` copied verbatim from production
  (both take their targets from the step `env:` the transform rewrites, so
  the rig runs the production gate code against TestPyPI), a minimal
  reusable `.github/workflows/checks.yml`
  (production's checks run the real lmer suite, which the scratch repo
  cannot satisfy; the rehearsal exercises the pipeline shape — a required
  reusable checks job gating build/publish), and a minimal buildable
  `pyproject.toml` with `project.name` set to the rehearsal project.
- **Trusted-publisher binding** (registered on the TestPyPI project,
  standard three-part shape mirroring production):
  - repository: `<bot>/lmer-rehearsal`
  - workflow path: `.github/workflows/release.yml` — same path as
    production, deliberately, so the binding shape is a 1:1 rehearsal of
    the real registration
  - environment: `testpypi` (distinct name so a rig workflow can never
    satisfy a production `pypi` binding, and vice versa), carrying the same
    deployment tag-pattern policy (`v*`) the spec requires on production's
    `pypi` environment.
- **Rig repo variables:** `RELEASE_ALLOWED_SIGNERS` — same variable name as
  production (the workflow is derived, not rewritten) but holding only the
  throwaway rehearsal public key (see Credential isolation).

## Workflow derivation

**Decision: derive by transform** — `derive-workflow.py` reads
`.github/workflows/release.yml` (the production workflow in this repo) and
emits the rig workflow. A checked-in copy is rejected because it goes stale
silently; a transform keeps the production file as the single source of
truth and makes every production change flow into the rig by re-running one
script.

The transform is minimal and enumerable (anything else passes through
verbatim):

1. `publish-pypi` step: add `repository-url: https://test.pypi.org/legacy/`
   to the `pypa/gh-action-pypi-publish` step.
2. `environment.name`: `pypi` → `testpypi`; `environment.url` →
   `https://test.pypi.org/project/<LMER_REHEARSAL_PROJECT>/`.
3. Project name references (environment url, GitHub Release step if any) →
   the rehearsal project name.

**Drift guard:** the derived workflow is committed to the rig repo, and
`derive-workflow.py --check` shape-checks the current production workflow;
handed the rig repo's committed copy via `--rig-workflow <path>` (`-`
reads stdin) it additionally re-derives from production and diffs against
that copy, exiting non-zero with the diff on any difference. The runners
fetch the committed copy through the GitHub contents API (`lib.sh`'s
`rehearsal_rig_workflow_fetch`, rig credentials only): `stand-up.sh` runs
the full check on `--check` and again right after populating the rig repo
(first stand-up runs the shape-only form before the rig copy exists), and
`negative-test.sh` / `run-leg2.sh` run the full check immediately before
every rehearsal run — so a stale rig workflow can never produce evidence.

## Credential isolation

**Rule: the rig never touches production credentials.** Rehearsal uses
rig-only credentials with rig-only environment variable names, so no
provisioning path, script default, or copy-paste can reach for the
production names:

- `LMER_REHEARSAL_GITHUB_TOKEN` — a fine-grained PAT scoped to the scratch
  repo only (`contents:write` + `workflows:write`, same scopes as
  production so the push paths are rehearsed faithfully). Never the
  production release PAT.
- `LMER_REHEARSAL_TESTPYPI_TOKEN` — optional, read by no rig script today:
  `stand-up.sh` verifies the project via TestPyPI's tokenless read-only
  JSON API, and the workflow publishes via the trusted publisher (OIDC),
  so this token gates nothing (its absence never SKIP-CLEANs a rig mode).
  Reserved for manual TestPyPI maintenance under a rig-only name; it never
  appears in the rig repo or workflow.
- `LMER_REHEARSAL_SIGNING_KEY` — path to a **throwaway** ed25519 SSH key
  generated by `stand-up.sh` at rig creation. Its public half is what
  `stand-up.sh` writes into the rig repo's `RELEASE_ALLOWED_SIGNERS`
  Actions variable. Never the production release signing key.

Delivery is **env-borne via `rig.env`** (path/value only): all three
variables are set in `rig.env` and sourced by the rehearsal scripts. The
rehearsal signing key is never delivered through the production release key
mount builder — that builder stays release-taskdef-only, which is exactly
the G2/G3 negative guarantee the rig exists to test. The teardown discards
the throwaway key; it has no life outside the rig.

## Evidence format

**Decision: one markdown file per rehearsal run** at
`docs/rehearsal/evidence-<UTC-timestamp>-<scenario>.md`, written by both
the negative tests and the leg-2 dry run in the same format, re-checkable
offline via `lib.sh --verify-evidence <file>`. One format for both
producers means one parser and no drift between "negative" and "positive"
evidence.

Each file is a short prose header plus a single fenced `yaml` block holding
the machine-checkable record:

```yaml
scenario: negative-unsigned-tag   # or negative-wrong-signer,
                                  # negative-not-main-head, leg2-dry-run
rig_repo: <bot>/lmer-rehearsal
rig_project: lmer-rehearsal
tag: v0.0.0-rc1
tag_sha: <40-hex>
workflow_run_id: <id>
workflow_run_url: <url>
expected_conclusion: failure      # negatives expect failure; leg2 expects success
recorded_conclusion: failure
failed_job: verify-tag-signature  # negatives only; must be pre-publish
published: false                  # true only for leg2-dry-run
recorded_at: <UTC ISO-8601>
derive_check: pass                # drift guard result at run time
```

`--verify-evidence` re-checks offline (no network): all required fields
present and well-formed, `recorded_conclusion` matches
`expected_conclusion`, negative scenarios name a `failed_job` that precedes
`publish-pypi` in job order and record `published: false`, and
`derive_check` is `pass`. The run URL is for humans following up online;
the offline check stands without it.

## Teardown and re-standup

The rig is disposable by construction so ctl's adoption can rebuild it from
the same scripts with parameters only:

- **All identity lives in `rig.env`** (`LMER_REHEARSAL_REPO`,
  `LMER_REHEARSAL_PROJECT`, the three credential variables, environment
  name). No rig name, URL, or key path is hard-coded in `stand-up.sh`,
  `lib.sh`, or `derive-workflow.py`; `rig.env.example` documents every
  parameter. ctl adoption = new `rig.env` values, same scripts.
- **`stand-up.sh` is idempotent:** it creates-or-verifies the scratch repo,
  repo controls, environment + tag-pattern policy, `RELEASE_ALLOWED_SIGNERS`
  variable, throwaway key, derived workflow, and TestPyPI registration —
  re-running against an existing rig converges instead of failing.
- **`stand-up.sh --teardown`** deletes the scratch repo, discards the
  throwaway signing key, and prints the manual residue that has no API
  path: revoke `LMER_REHEARSAL`-prefixed tokens (PAT on GitHub, token on
  TestPyPI) and optionally delete the TestPyPI project. Evidence files in
  `docs/rehearsal/` are the one artifact teardown never removes — they are
  the point of the rig.
