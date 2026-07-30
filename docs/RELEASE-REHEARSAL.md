# Release Rehearsal Rig — Runbook

How to stand up, run, and tear down the release rehearsal rig: a scratch
GitHub repo plus a TestPyPI trusted publisher that exercises the full
leg-2 release path — including the G2 negative tag-verification tests —
before any production release. The rig is part of the release flow
([RELEASE-FLOW.md](./RELEASE-FLOW.md)); its frozen design lives in
[Ctl/rehearsal/README.md](../Ctl/rehearsal/README.md), and the checked-in
scripts in `Ctl/rehearsal/` implement it. This document is the operator's
runbook for those scripts.

**Standing rule: a production release run must not precede a green
rehearsal.** The rehearsal is not optional polish — it is the proof that
tag verification fails closed and that leg 2 converges. Until both
evidence files in `docs/rehearsal/` carry recorded (non-pending) results
that pass their offline verifiers, do not start a production release run.

**Never paste an actual token, key, or secret value into this document,
`rig.env` commits, or any evidence file — reference credential names
only.**

## Prerequisites

Accounts and credentials (all rehearsal-only — see the isolation rule
below):

- The **bot GitHub account** that owns the production mirror. The scratch
  repo lives under the same account so branch protection, tag protection
  with bot bypass, and the environment tag-pattern policy are configured
  identically and actually exercised.
- A **fine-grained GitHub PAT scoped to the scratch repo only**
  (`contents:write` + `workflows:write` — the production scopes, so the
  push paths are rehearsed faithfully). This is `LMER_REHEARSAL_GITHUB_TOKEN`.
- A **TestPyPI account** for the rig. No API token is required by any
  rig script: the rig workflow publishes via the trusted publisher
  (OIDC), and the runners verify the upload through TestPyPI's tokenless
  read-only JSON API. `LMER_REHEARSAL_TESTPYPI_TOKEN` is an optional
  slot in `rig.env` for manual project maintenance only (e.g. yanking a
  rehearsal upload by hand); it never appears in the rig repo or
  workflow.
- A **throwaway ed25519 signing key** (`LMER_REHEARSAL_SIGNING_KEY` is
  its path). `stand-up.sh` generates it at rig creation; `--teardown`
  discards it. It has no life outside the rig.

Local tooling: `bash`, `git`, `curl`, `ssh-keygen`, and `python3` with
`yaml` (PyYAML) for [Ctl/rehearsal/derive-workflow.py](../Ctl/rehearsal/derive-workflow.py).

Configuration: copy
[Ctl/rehearsal/rig.env.example](../Ctl/rehearsal/rig.env.example) to
`Ctl/rehearsal/rig.env` (git-ignored — it holds token values) and fill it
in. All rig identity lives there; nothing is hard-coded in the scripts.

**Rehearsal-credential isolation rule: the rig never touches production
credentials.** Rehearsal uses the rig PAT and the throwaway signing key —
never the production release PAT and never the production release signing
key. The credential variables are rehearsal-only by name
(`LMER_REHEARSAL_*`), delivered env-borne via `rig.env`, and the offline
production-target guard in [Ctl/rehearsal/lib.sh](../Ctl/rehearsal/lib.sh)
enforces the rule before any network call in every mode: it refuses the
production repo (`lmer2/lmer` or any repo/project named `lmer`),
`pypi.org`, any shell carrying a production release credential variable,
and a rehearsal key path that points at the production release key mount.
The rehearsal key is never delivered through the production release key
mount builder — that builder stays release-taskdef-only, which is exactly
the negative guarantee the rig exists to test.

Sandbox behavior (R14 skip-clean): when the `LMER_REHEARSAL_*` credential
variables are absent, every rig-touching mode exits 0 with a loud notice
instead of failing. The rig is optional in sandboxes; nothing below runs
by accident.

## Stand up the rig

`Ctl/rehearsal/stand-up.sh` is the single entry point, and it is
idempotent — re-running against an existing rig converges instead of
failing:

```
Ctl/rehearsal/stand-up.sh --dry-run     # print the plan; no network calls
Ctl/rehearsal/stand-up.sh               # create-or-verify every rig piece
Ctl/rehearsal/stand-up.sh --check       # verify-only; non-zero on any gap
```

The default mode creates-or-verifies, in order:

1. **Drift guard**: `derive-workflow.py --check` re-derives the rig
   workflow from the production `.github/workflows/release.yml` and fails
   loudly on any drift. It runs on every invocation, and a green
   `--check` is required immediately before any rehearsal run — a stale
   rig workflow can never produce evidence. The rig also receives the
   scripts the workflow invokes — `verify-tag-signature.sh` and
   `gate-version-reuse.py` — copied verbatim from production; both read
   their targets from the step `env:` the derivation rewrites, so the rig
   exercises the production gate code against TestPyPI.
2. **Scratch repo** (`LMER_REHEARSAL_REPO`, private): issues/wiki/projects
   off; PRs unaccepted by policy (GitHub has no switch to disable them —
   the script reports any open PRs).
3. **Rulesets**: main-branch protection (no deletion, no force-push) and
   a `v*` tag ruleset with a repository-admin bypass entry for the bot.
4. **Throwaway signing key** at `LMER_REHEARSAL_SIGNING_KEY` (generated
   if absent), and the `RELEASE_ALLOWED_SIGNERS` Actions variable set to
   its public half — the same variable name as production, holding only
   the throwaway key.
5. **Environment** (`LMER_REHEARSAL_ENVIRONMENT`, default `testpypi`)
   with the `v*` tag-pattern deployment policy — the same policy the
   release flow requires on production's `pypi` environment.
6. **TestPyPI**: the project (`LMER_REHEARSAL_PROJECT`) is verified via
   the read-only JSON API where possible.

**Manual TestPyPI steps** — TestPyPI has no complete management API, and
trusted-publisher (OIDC) registration has no public API at all.
`stand-up.sh` prints the exact manual steps with the exact values:
register a pending publisher (or the existing project's publisher) with
the scratch repo owner/name, workflow path `.github/workflows/release.yml`
(same path as production, deliberately), and the rig environment name.
The project itself materializes on the first trusted-publisher upload.

TestPyPI naming caveat: the rehearsal project is never `lmer` — rehearsal
artifacts must be unmistakably non-production, and the guard refuses the
production name. `stand-up.sh` rewrites `project.name` in the rig repo's
`pyproject.toml` to the rehearsal project name (the `lmer` import package
is untouched; only the distribution name changes). TestPyPI also enforces
its own project-size and upload quotas and its index can lag an upload by
a few seconds — the leg-2 script polls for the listing rather than
asserting it immediately.

Rerun `stand-up.sh --check` until it reports every rig piece present. The
trusted publisher itself is not API-verifiable; confirm it once via the
TestPyPI UI.

## Run the negative test

`Ctl/rehearsal/negative-test.sh` runs the G2 negative tag-verification
tests: three cases, each of which must fail the release pipeline in its
first job (`verify-tag-signature`) before anything builds or publishes:

- `unsigned-tag` — a plain (unsigned) `v*` tag at GitHub main HEAD;
- `wrong-signer` — a `v*` tag signed by a key absent from the rig repo's
  `RELEASE_ALLOWED_SIGNERS` variable;
- `not-main-head` — a correctly signed `v*` tag whose commit is not at
  GitHub main HEAD.

```
Ctl/rehearsal/negative-test.sh --all --dry-run   # the plan; no network calls
Ctl/rehearsal/negative-test.sh --all             # all three cases; rewrites the
                                                 # aggregate evidence file
Ctl/rehearsal/negative-test.sh --case unsigned-tag   # one case (debugging);
                                                     # aggregate untouched
```

For each case the script pushes the tag, waits for the workflow run, and
asserts: run conclusion is failure; the failing job is
`verify-tag-signature`; every downstream job is skipped or never started;
TestPyPI gained no file for the version; and no GitHub Release exists.
Only `--all` rewrites the aggregate evidence file
[docs/rehearsal/evidence-negative-test.md](./rehearsal/evidence-negative-test.md),
and only from real run data after all three cases pass — the script never
fabricates run URLs, conclusions, or listings.

Re-check recorded evidence offline (no network) with:

```
Ctl/rehearsal/negative-test.sh --verify-evidence docs/rehearsal/evidence-negative-test.md
```

A pending skeleton exits 0 with a loud PENDING notice; a populated file
gets the full consistency check (fail-before-publish job order, no
publish, no release, the not-main-head tag genuinely off main HEAD).

## Run the leg-2 rehearsal

`Ctl/rehearsal/run-leg2.sh --full` walks the release flow's leg-2 sequence
end-to-end in the rig (the negative-test evidence is a prerequisite —
see the pending skeleton's checklist):

```
Ctl/rehearsal/run-leg2.sh --full --dry-run   # the plan; no network calls
Ctl/rehearsal/run-leg2.sh --full             # the whole walk; rewrites the
                                             # evidence file from recorded results
```

The walk: record the rehearsal "merge SHA" (a version-bump commit on rig
main, version re-read at that SHA — never from working-tree memory);
create the SSH-signed tag at exactly that SHA with the throwaway rig key
and verify signature + target before pushing; push GitHub main first,
then the tag; poll the Actions run to green; confirm TestPyPI holds the
version with PEP 740 attestations and the GitHub Release exists. The
GitLab-side tag push comes last in the sequence but the rig has no
GitLab-side mirror, so it is recorded as an explicit `skipped-in-rig`
receipt with the reason — never a silent omission; the production
ordering (GitHub green before any GitLab push) is preserved by sequence
for the steps the rig does exercise.

Then leg 2 is re-entered twice to prove the idempotency branches the
release flow requires: refs current + green derives `skip` (no new tag, no new push,
no new workflow run); refs current + red converges via API re-dispatch
(the tag is immutable — never deleted, never re-pointed, never
re-signed). `skip-existing` means the re-dispatched run uploads nothing;
the evidence records which run actually uploaded.

Burned-version behavior: TestPyPI mirrors production PyPI here —
filenames are permanent, so once an upload succeeds for a version that
version is spent even if yanked. The rig sidesteps collisions by minting
a timestamp-derived version (`0.0.<epoch>`) per walk, and it aborts if
TestPyPI already lists files for the version (a pre-existing listing
makes which-run-uploaded unattributable). If a rehearsal version does get
burned, do what production does: never delete or re-point the tag; run
again with a new version. Re-check recorded evidence offline with:

```
Ctl/rehearsal/run-leg2.sh --verify-evidence docs/rehearsal/evidence-leg2.md
```

## Evidence

The evidence format is one markdown file per rehearsal run at
`docs/rehearsal/evidence-<UTC-timestamp>-<scenario>.md`: a short prose
header plus a single fenced `yaml` block holding the machine-checkable
record (scenario, rig repo/project, tag and tag SHA, workflow run id/URL,
expected vs recorded conclusion, failing job for negatives,
published flag, drift-guard result, recorded-at timestamp). One format
for negatives and leg-2 alike, re-checkable offline with:

```
Ctl/rehearsal/lib.sh --verify-evidence <file>
```

On top of the per-run files, the two aggregate files the scripts own:

- [docs/rehearsal/evidence-negative-test.md](./rehearsal/evidence-negative-test.md)
  — all three G2 negative cases, rewritten by `negative-test.sh --all`.
- [docs/rehearsal/evidence-leg2.md](./rehearsal/evidence-leg2.md)
  — the leg-2 walk plus both idempotency re-entries, rewritten by
  `run-leg2.sh --full`.

**Both aggregate files currently carry pending skeletons — the rig has
not run yet.** They contain no recorded evidence, only `status: pending`
and TBD placeholders; the scripts populate them from real run data when
the rig runs, and refuse to overwrite recorded evidence with a skeleton.
No rehearsal has happened until those files verify as complete — and per
the standing rule above, no production release run happens before that.

## Teardown

The rig is disposable by construction:

```
Ctl/rehearsal/stand-up.sh --teardown --dry-run   # the plan
Ctl/rehearsal/stand-up.sh --teardown             # delete the rig
```

Teardown deletes the scratch repo and discards the throwaway signing key
(private and public halves), then prints the manual residue that has no
API path: revoke the `LMER_REHEARSAL`-prefixed fine-grained PAT on
GitHub, revoke any TestPyPI API token kept for manual rig maintenance,
and optionally delete the TestPyPI project.

Evidence files under `docs/rehearsal/` are the one artifact teardown
never removes — they are the point of the rig.

## Re-standing the rig for another repo

ctl adoption (adopter #2) rebuilds the rig from the same scripts with
parameters only — new `rig.env` values, zero script changes. Exactly what
is parameterized per adopter:

- **GitHub target** — `LMER_REHEARSAL_REPO` (the adopter's scratch repo,
  under the bot account that owns that adopter's production mirror) and
  `LMER_REHEARSAL_GITHUB_TOKEN` (a fresh fine-grained PAT scoped to that
  scratch repo only).
- **TestPyPI project** — `LMER_REHEARSAL_PROJECT` (unmistakably
  non-production, e.g. `ctl-rehearsal`; never the adopter's real project
  name), plus the manual trusted-publisher registration `stand-up.sh`
  prints. No TestPyPI token is needed.
- **Tag prefix** — `v` everywhere, matching the production convention;
  the rig's tag ruleset and tag-pattern policy pin `v*`. A different
  prefix is a design change, not an adoption parameter.
- **Signing key reference** — `LMER_REHEARSAL_SIGNING_KEY`, the path
  where `stand-up.sh` generates the adopter's throwaway key. Only the
  path is parameterized, never key material, and never the adopter's
  production release signing key.
- **Environment tag-pattern policy** — `LMER_REHEARSAL_ENVIRONMENT` (the
  deployment environment name in the trusted-publisher binding, distinct
  from production's `pypi`); `stand-up.sh` attaches the `v*` tag-pattern
  deployment policy to it, mirroring the policy the adopter's production
  environment must carry.

The sequence is the same as above: fill in `rig.env`, `stand-up.sh` (plus
the printed manual TestPyPI steps), `stand-up.sh --check` green,
`negative-test.sh --all`, `run-leg2.sh --full`, verify both evidence
files — and only then the adopter's first production release. The
production-target guard applies unchanged: `pypi.org` and any shell
carrying production release credential variables are refused regardless
of adopter (the guard additionally hard-refuses lmer's production repo
and project names; extend it with the new adopter's production names
when adopting).
