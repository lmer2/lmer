# Production Release Setup — Operator Receipts

Operator-performed, one-time production configuration for the lmer release
flow ([RELEASE-FLOW.md](./RELEASE-FLOW.md)). Every item below is performed by
a human operator and ticked with a date and actor — the **gating**
receipts gate the **first production release run through this flow**, not
the release-flow build itself.

Work each item with the adoption checklist
([RELEASE-ADOPTION.md](./RELEASE-ADOPTION.md)) open — it carries the detail
each step summarizes. Record fingerprints, dates, and actor only — **never a
secret value** (no tokens, no private keys).

This file is the reusable template, kept unticked. A completed deployment's
receipts — dates, actors, evidence, and any waiver — are recorded with that
release run's records in the work repo, not here.

> **How this file gates.** A deployment WORKS this checklist in a copy: it
> lands in the release run's own run directory in the work repo — the one
> the release taskdef's run state lives in — and each item is ticked there
> with its date and actor. The completed copy is what gates. A run of the
> release taskdef must refuse to proceed to leg 2 while any **gating** item
> is unchecked in that copy; the template here stays blank, and the
> hardening section does not gate.
>
> Receipts are self-attestation unless an item cites externally verifiable
> evidence — a missed or misconfigured setting surfaces in the follow-on
> release run (leg-2 step 5 is where canonical-forge-side misconfiguration
> surfaces). Tick an item only after performing it, with the date and your
> name.

## Gating receipts

These must all be checked in the release run's copy before that run proceeds
to leg 2.

- [ ] **(0) Release SSH signing keypair generated** — private half stored in
  the lmer credential-provisioning source (`LMER_RELEASE_SIGNING_KEY`);
  public-key fingerprint recorded here: `<fingerprint>` — date/actor:
- [ ] **(4) `vars.RELEASE_ALLOWED_SIGNERS` Actions repository variable set**
  — the release signing PUBLIC key in allowed-signers format, principal `*`
  (a variable, not a secret; deliberately not a file in the repo) —
  date/actor:
- [ ] **(5) Production fine-grained PAT issued** — `contents:write` +
  `workflows:write` on the mirror repo only; expiry noted: `<date>`;
  rotation owner: the repository owner; provisioned via lmer credential
  provisioning (`LMER_RELEASE_GITHUB_TOKEN`, release-taskdef sessions only)
  — date/actor:
- [ ] **(5a) Push-allow entries provisioned** — mirror `refs/heads/main` and
  `refs/tags/*`, canonical origin `refs/tags/*`. Mirror entries must be
  written `host/path|refpattern` — host-anchored and ref-scoped. Leg 2 adds
  the mirror as a NAMED remote and pushes through it, and the named-remote
  half of the grammar accepts a host-less `group/project` entry, which
  grants on any host serving that path; naming the host confines the grant
  to the one host git will dial. The ref half matters just as much: a bare
  entry authorizes `refs/heads/*` only, so the tag push needs
  `|refs/tags/*` spelled out, and `|refs/heads/main` keeps the branch grant
  off every other branch. (If a future leg pushes by URL instead, the match
  there is anchored and a path-only entry would not authorize it at all.) —
  date/actor:
- [ ] **(7) Production PyPI project trusted publisher registered** — pinned
  to the mirror repo + workflow `.github/workflows/release.yml` +
  environment `pypi`. A project whose mirror already publishes tokenlessly
  (workflow with `id-token: write` and no API token, releases on the index
  published by it) may record this as **pre-existing**, citing that live
  binding as the evidence — date/actor:
- [ ] **(11) `vars.RELEASE_RESUME_VERSION` confirmed ABSENT** — the
  version-reuse gate ([RELEASE-FLOW.md](./RELEASE-FLOW.md) §5) refuses to
  publish over an already-published version unless this variable names it.
  It exists to be set for one deliberate resume and cleared immediately
  after; a value left behind silently re-arms the republish it authorized.
  Confirm it is unset before each release — date/actor:

## Hardening — recommended, not gating

Each of these closes a real hole, but none of them is required for a release
run to complete correctly. They are listed separately so that leaving one
undone is a recorded decision rather than a silent gap — and so that an
unrelated hardening task cannot block a release.

- [ ] **(1) Mirror branch protection on `main`** — only the bot account can
  push; no force pushes. The flow assumes no out-of-band writers to the
  mirror; this is what enforces that assumption — date/actor:
- [ ] **(2) `v*` tag protection on the mirror** — WITH a bypass entry for
  the bot. **Do not add this rule without the bypass**: without it the
  bot's own tag push is rejected and leg 2 fails at the tag push. A repo
  with no tag protection at all pushes fine; a repo with protection and no
  bypass does not — date/actor:
- [ ] **(3) `pypi` environment deployment tag-pattern policy** —
  deployments from `v*` tags only. The environment itself already exists
  (it is what the trusted-publisher binding names); this item is the
  protection rule on it, which is what makes the environment binding mean
  something under PAT compromise — date/actor:
- [ ] **(6) Bot account registered with the signing public key** — so
  signed tags attribute correctly. Display/attribution only; the publish
  path does not depend on it — date/actor:
- [ ] **(8) Canonical repo protected tags `v*`** — configured to permit the
  release credential. Unexercised until the first release, since the
  canonical repo currently carries zero tags. A misconfiguration surfaces
  at **leg-2 step 5**, which runs *after* the public publish is green, so
  it cannot burn a version — date/actor:
- [ ] **(9) PR/issue policy + minimal collaborators on the mirror** —
  mirror accepts no PRs; collaborators = bot + owner only. Note that the
  project's issue tracker may deliberately live on the mirror; decide
  rather than default — date/actor:

## Rehearsal

- [ ] **(10) Rehearsal green** — the rig's negative test and leg-2 dry run
  recorded green evidence
  ([evidence-negative-test.md](./rehearsal/evidence-negative-test.md),
  [evidence-leg2.md](./rehearsal/evidence-leg2.md)) before any production
  release — date/actor:

Waiving this for a given release is possible but never implicit: the waiver
— its rationale and the residual it accepts — is recorded with that release
run's records, alongside the run's other receipts. An unrecorded skip is not
a waiver. A waiver covers the one release it names; it does not retire the
standing rule in [RELEASE-REHEARSAL.md](./RELEASE-REHEARSAL.md) and does not
carry over to another adopter.
