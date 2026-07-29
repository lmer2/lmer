# Production Release Setup — Operator Receipts

Operator-performed, one-time production configuration for the lmer release
flow ([RELEASE-FLOW.md](./RELEASE-FLOW.md)). Every item below is performed by
a human operator and ticked here with a date and actor — these receipts gate
the **follow-on G4 release run** (the first production release through the
new flow), not the release-flow build itself.

Work each item with the adoption checklist
([RELEASE-ADOPTION.md](./RELEASE-ADOPTION.md)) open — it carries the detail
each step summarizes. Record fingerprints, dates, and actor only — **never a
secret value** (no tokens, no private keys).

> **Status: PENDING.** No item has been performed yet. This template was
> created during the release-flow build; the operator completes it before
> launching the first production release run. A run of the release taskdef
> must refuse to proceed to leg 2 while any item below is unchecked.
>
> Receipts here are self-attestation, not remote-console proof — a missed or
> misconfigured setting surfaces in the follow-on release run (leg-2 step 5
> is where GitLab-side misconfiguration surfaces; the rehearsal negative
> test proves the GitHub-side mechanism). Tick an item only after performing
> it, with the date and your name.

## Receipts

- [ ] **(0) Release SSH signing keypair generated** — private half stored in
  the lmer credential-provisioning source (`LMER_RELEASE_SIGNING_KEY`);
  public-key fingerprint recorded here: `<fingerprint>` — date/actor:
- [ ] **(1) `lmer2/lmer` branch protection on `main`** — only the bot
  account can push; no force pushes — date/actor:
- [ ] **(2) `v*` tag protection on `lmer2/lmer`** — WITH a bypass entry for
  the bot (without it the bot's own tag push is rejected) — date/actor:
- [ ] **(3) `pypi` environment deployment tag-pattern policy** — deployments
  from `v*` tags only — date/actor:
- [ ] **(4) `vars.RELEASE_ALLOWED_SIGNERS` Actions repository variable set**
  — the release signing PUBLIC key in allowed-signers format (a variable,
  not a secret; deliberately not a file in the repo) — date/actor:
- [ ] **(5) Production fine-grained PAT issued** — `contents:write` +
  `workflows:write` on `lmer2/lmer` only; expiry noted: `<date>`; rotation
  owner: the repository owner; provisioned via lmer credential provisioning
  (`LMER_RELEASE_GITHUB_TOKEN`, release-taskdef sessions only) — date/actor:
- [ ] **(6) Bot GitHub account registered with the signing public key** —
  so signed tags attribute correctly — date/actor:
- [ ] **(7) Production PyPI project trusted publisher registered** — pinned
  to repo `lmer2/lmer` + workflow `.github/workflows/release.yml` +
  environment `pypi` — date/actor:
- [ ] **(8) GitLab canonical repo protected tags `v*`** — configured to
  permit the release credential (note: the repo currently has zero tags, so
  these rules are unexercised until leg-2 step 5) — date/actor:
- [ ] **(9) PR/issue policy + minimal collaborators on `lmer2/lmer`** —
  mirror accepts no PRs; collaborators = bot + owner only — date/actor:
- [ ] **(10) Rehearsal green** — the rig's negative test and leg-2 dry run
  recorded green evidence
  ([evidence-negative-test.md](./rehearsal/evidence-negative-test.md),
  [evidence-leg2.md](./rehearsal/evidence-leg2.md)) before any production
  release — date/actor:
- [ ] **(11) `vars.RELEASE_RESUME_VERSION` confirmed ABSENT** — the
  version-reuse gate ([RELEASE-FLOW.md](./RELEASE-FLOW.md) §5) refuses to
  publish over an already-published version unless this variable names it.
  It exists to be set for one deliberate resume and cleared immediately
  after; a value left behind silently re-arms the republish it authorized.
  Confirm it is unset before each release — date/actor:
