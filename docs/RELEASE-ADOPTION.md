# Release Flow — Per-Repo Adoption Checklist & Runbooks

How a repository adopts the release flow (GitLab-driven, read-only
GitHub mirror, tokenless PyPI publish). The flow is parameterized —
adopting it is configuration plus the prerequisites below, **never design
changes**. lmer is adopter #1; ctl is adopter #2.

The flow itself — topology, the two release legs, the signing model — is
documented in [RELEASE-FLOW.md](./RELEASE-FLOW.md); read it alongside this
checklist.

This document is a checklist an operator ticks, plus the operational
runbooks that come with adoption (credential rotation, burned versions,
GitHub `main` divergence). **Never paste an actual token, key, or secret value into this
document or any run artifact — reference credential names and lmer
credential-provisioning entries only.**

---

## 1. Parameterization surface

The release taskdef takes four required per-repo parameters and one
optional one (taskdef config / work-repo project info). If an adoption needs
anything beyond these, stop — that is a design change, not an adoption.

- [ ] **GitHub target URL** — the mirror repo the release pushes `main` +
      the signed tag to (e.g. `github.com/lmer2/lmer`).
- [ ] **Tag prefix** — `v` everywhere (both remotes); the tag name is
      always derived from `pyproject.toml` at the tagged commit.
- [ ] **Signing-key reference** — the name of the release SSH signing key
      in lmer credential provisioning (reference only, never the key
      material).
- [ ] **Changelog mechanism** — `changelog.d/` fragments where adopted,
      legacy `CHANGELOG.yaml` roll otherwise.
- [ ] **Dependency-refresh command** (`dep_refresh`, OPTIONAL) — the
      command leg 2 runs once the release has shipped, to open the next
      cycle's lockfile-refresh MR against `prep-release` (lmer:
      `make dep-up`). Omit it and the flow runs no refresh — an explicit
      opt-out, recorded as a receipt. Declaring it empty is a hard stop,
      not an opt-out.

## 2. Prerequisites — all of them, none optional

Every item below must be ticked before the first production release.
None of them are free; each one closes a specific hole.

### GitHub mirror repo

- [ ] Mirror repo exists; collaborators are minimal — the bot account and
      the owner, nothing else.
- [ ] **Branch protection** on `main`: only the bot account can push. No
      human or other integration has a write path to `main`.
- [ ] **Tag protection** on `v*` — **with a bypass entry for the bot**.
      Without the bypass, the bot's own tag push is rejected and leg 2
      fails at the tag push.
- [ ] `pypi` environment carries a **deployment tag-pattern policy**
      narrowing which refs may deploy (without protection rules the
      environment binding does not survive PAT compromise: an unprotected
      environment can be requested by any workflow in the repo — see
      [RELEASE-FLOW.md](./RELEASE-FLOW.md) §4).
- [ ] PR/issue policy set: mirror repos accept **no PRs**. Disable issues
      unless the repo deliberately hosts them.

### PyPI

- [ ] PyPI project exists.
- [ ] A **trusted publisher** is registered, pinned to all three bindings:
      the mirror repo + the workflow path (`release.yml`) + the `pypi`
      environment. Publishing stays tokenless (OIDC); no API token exists
      for the project.

### GitLab (canonical repo)

- [ ] **Protected tags** `v*` on the canonical repo, configured to permit
      the release credential to push.
- [ ] Note and verify at first release: GitLab currently carries **zero
      tags**, so these rules are unexercised — **leg-2 step 5** (the tag
      push to GitLab origin, after GitHub is green) is where a
      misconfiguration surfaces. Watch that step on the first run.

### Credentials

- [ ] Per-repo **fine-grained PAT** issued with `contents:write` +
      `workflows:write` on the single mirror repo (`workflows:write` is
      required: mirrored `main` routinely carries `.github/workflows/*`
      changes, and GitHub rejects workflow-touching pushes from a PAT
      without it).
- [ ] PAT provisioned via lmer credential provisioning to **release-taskdef
      sessions only**, with `push_allow` entries for the mirror repo and
      for tag pushes to GitLab origin (`refs/tags/*`).
- [ ] Release SSH signing key provisioned via lmer credential provisioning
      to **release-taskdef sessions only**; public half registered on the
      bot GitHub account.
- [ ] `RELEASE_ALLOWED_SIGNERS` GitHub Actions repository **variable** set
      to the signer public key (admin-controlled variable, deliberately
      not a checked-in file — a tag-triggered workflow runs from the tag's
      own tree, so in-repo trust anchors can be altered by whoever pushes
      a tag).
- [ ] **Negative guarantee** holds: sessions of any other taskdef do
      not receive the PAT or the signing key. The provisioning-scoping
      test asserts this; confirm it passes for the new entries.
- [ ] `RELEASE_RESUME_VERSION` Actions repository **variable** left
      **unset**. It is the only switch that lets the version-reuse gate
      ([RELEASE-FLOW.md](./RELEASE-FLOW.md) §5) publish over a version the
      index already holds: an admin sets it to that exact version for one
      deliberate resume and clears it immediately after. Never provision
      it as standing configuration.

## 3. Rotation runbook (PAT + signing key)

**Rotation owner: the repository owner.** Credentials are delivered by
lmer credential provisioning and by nothing else; no other taskdef's
sessions receive them.

### Fine-grained PAT renewal

Fine-grained PATs **expire** (≤ 1 year). Before expiry:

- [ ] Issue a new fine-grained PAT with the same scopes
      (`contents:write` + `workflows:write`, single mirror repo).
- [ ] Update the lmer credential-provisioning entry (reference the entry
      name — never record the token value anywhere).
- [ ] Confirm the next release-taskdef session receives the new PAT and
      that no other taskdef's sessions do.
- [ ] Revoke the old PAT.

### Signing-key rotation

New key → update the Actions variable + bot account → next release uses
it. **No repo commit is involved** at any step.

- [ ] Generate a new release SSH signing key; store it only in the lmer
      credential-provisioning entry.
- [ ] Update the `RELEASE_ALLOWED_SIGNERS` Actions variable with the new
      public key.
- [ ] Register the new public key on the bot GitHub account; remove the
      old one.
- [ ] The next release signs with the new key — nothing else to do.

## 4. Burned-version runbook

PyPI filenames are **permanent**: once an upload succeeds for a version,
that version is **spent — even when yanked**. Repair path for a bad
release:

- [ ] **Yank** the bad version on PyPI *if warranted* (yanking hides it
      from default resolution; it does not free the version).
- [ ] Ship a **new patch version through the same flow** — bump, roll,
      release MR, signed tag, publish. No shortcuts.
- [ ] Tags are **never deleted or re-pointed.** The burned tag stays where
      it is, on both remotes. `gate-push` and the pre-push hook refuse ref
      deletions outright, so removing one is a deliberate human act with
      plain git — not something a session can do on its way past.
- [ ] Do **not** reach for `RELEASE_RESUME_VERSION` here. It authorizes
      re-entering a publish for a version this run built; it does not make
      a spent version reusable, and `skip-existing` would keep the
      artifacts PyPI already serves.

## 5. GitHub `main` divergence remediation

A **non-fast-forward push** to the GitHub mirror is a **hard stop**.

- [ ] Remediation is a **human decision recorded in the run** — never an
      automatic **force-push**. The taskdef must not, under any failure
      mode, force-push the mirror.
- [ ] First adoption includes the **one-time ancestor
      check/reconciliation**: verify GitHub `main` is an ancestor of the
      GitLab merge SHA and reconcile any pre-flow drift once, by hand,
      recorded in the run. After that, any divergence means something
      wrote to the mirror out-of-band — investigate before touching refs.

## 6. Bootstrap sequence — in order, the pieces block each other

- [ ] **1.** lmer releases first, in **legacy changelog mode**
      (`CHANGELOG.yaml` roll), with ctl installed from the canonical GitLab
      instance at a **pinned ref**.
- [ ] **2.** ctl's adoption prerequisites are stood up (mirror repo, PyPI
      project + trusted publisher, PAT, variables — the full §2 checklist
      above); ctl releases through the flow.
- [ ] **3.** With released ctl, lmer's pending `changelog.d`
      fragment-mode change merges, and the fragment-mode MRs queued behind
      it land.
- [ ] **4.** lmer's next release runs in **fragment mode**, and the
      taskdef's ctl dependency switches from git-pinned to the **PyPI
      package**.
