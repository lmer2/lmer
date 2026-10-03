# Git & Version Control Rules

## 🚨 MANDATORY Gate Commands
**ONLY use gate commands for git operations:**
- `gate-check` - Run all checks without committing
- `gate-commit -m "message"` - Check and commit if passes
- `gate-push` - Check repository allowlist and push if allowed
- **NEVER use direct git commands for commit/push operations**

### Command form: bare `gate-*`, not `bin/gate-*`
The gate commands are on `$PATH` (installed at `/Agents/global/bin/`). **Always invoke them bare** — `gate-check`, not `bin/gate-check`. The `bin/` prefix is a historical form that only works in **self-development mode** (when `LMER_SELF_DEV=1`, i.e. editing the lmer repo itself), where it resolves to the in-workspace development copy of the gate scripts. Outside of self-dev, `bin/gate-*` will fail because there is no `bin/` directory in arbitrary project repositories.

### Timeouts: use at least 5 minutes
Gate commands run the full test suite and pre-commit hooks, and routinely take several minutes. When invoking a gate command through the Bash tool, **always set `timeout` to at least `300000` (5 minutes)**. Do not rely on the default 2-minute timeout — it will kill long-running gate checks partway through and produce misleading failures.

### `LMER_QUICK_GATE_COMMIT`: skip tests during `gate-commit`
Setting `LMER_QUICK_GATE_COMMIT` to a truthy value (`1`, `true`, `yes`, case-insensitive) makes `gate-commit` skip the test suite (the slowest check) while still running pre-commit hooks, secret scans, and the other fast checks. Tests are still run by standalone `gate-check` and by `gate-push`, so coverage is preserved before code leaves the local repo. The flag only affects `gate-commit` — `gate-check` and `gate-push` ignore it. Set to `0`, `false`, `no`, or leave unset to keep tests running.

### Deliverable format check
The gate checks warn when a staged file that names a spec-class deliverable (any path component with a word starting `spec`, `plan`, or `report`) uses a binary document extension (`.docx`, `.doc`, `.pdf`, `.odt`, `.rtf`). Specs, plans, and reports deliver as Markdown (`.md`) — binary documents are unreviewable in GitLab (undiffable, unlinkable at line level). This is a WARNING, not a hard fail, because reports from external sources may legitimately be PDF; unrelated binary files (vendored manuals, fixtures) are not flagged.

## 🚨 Critical Git Rules
- **NEVER** use `git add -A` or `git add .` - ONLY add specific files you've modified
- **ALWAYS** use gate commands instead of direct git commit/push
- **ALWAYS** run all tests before commit
- **ALWAYS** run precommit before commit
- **NEVER** push to any repository without explicit permission (except allow list)
- **ALWAYS** use required gates for routine commits within authorized work; respect explicit user holds
- **NEVER** add Claude attributions or signatures

## 🚨 Commit Rules - STOP AND CHECK
- **MUST** use `gate-commit -m "message"` for all commits
- **NEVER** use `git commit` directly
- Routine commits within authorized work need no separate generic commit confirmation
- An explicit user hold, such as "do not commit yet", remains binding
- **BEFORE committing:**
  1. ✅ Run `gate-check` to verify all checks pass
  2. ✅ Show user the gate check results
  3. ✅ Summarize what changed
  4. ✅ Confirm the changes stay within authorized work and no explicit user hold applies
- Proceed through `gate-commit` when required checks pass; failing checks block
- A commit does not authorize new scope, push, history rewrite, merge, deploy,
  destructive operations, or another worker's lifecycle
- Only add and commit files you created or modified
- Never include Claude code attributions in commit messages
- Never add or commit any Claude-specific files to git
- Remove debug print statements before committing

## 🚨 Push Policy
**CRITICAL**: Only use `gate-push` for pushing to repositories
- **NEVER** use `git push` directly
- The gate-push command will:
  1. Check repository against allow list
  2. Run all commit gate checks
  3. Block push if not allowed or checks fail

**Repository Allow List**:
The push allow list is the **union of two sources** — any single entry granting
the target allows the push:
1. The `LMER_PUSH_ALLOW_LIST` environment variable (comma-separated)
2. The active taskdef's top-level `push_allow` list in its `task.yaml`,
   resolved from the **trusted taskdef tiers only** — never the
   agent-writable work-repo tiers (see docs/TASKDEFS.md)

Each entry is either `repo` or `repo|refpattern`:
```bash
LMER_PUSH_ALLOW_LIST="org/repo1,gitlab.example.com/group,*.example.com|refs/tags/*"
```
- A bare `repo` entry authorizes **branch refs only** (`refs/heads/*`) — bare
  entries never grant tag pushes.
- A `repo|refpattern` entry matches `refpattern` (a glob) against the
  fully-qualified target ref: `org/repo|refs/tags/*` grants tag pushes,
  `org/repo|refs/heads/main` grants exactly one branch.
- Malformed entries (empty half, more than one `|`) are ignored — the list
  fails closed, never open.

The **repo half** is matched by the grammar in `lmer_cli.push_allow` — not by a
substring test. It is one of:
- Exact repo: `gitlab.example.com/group/project` (SSH/HTTPS URL spellings also
  accepted, and interchangeable)
- Whole host: `gitlab.example.com` (any project on that exact host)
- Wildcard domain: `*.example.com` (any host ending in `.example.com`;
  subdomains only — the apex needs its own entry)
- Host + project prefix: `gitlab.example.com/group` (segment-boundary safe —
  matches `group/project`, not `groupfoo/x`)
- Legacy host-less path: `org/repo` (that project path on any host)

Hosts compare case-insensitively, project paths case-sensitively. Every push URL
of the target remote must be granted, and a target that does not parse into
`host/path` (a filesystem remote, say) is refused rather than guessed at.

When git is handed a **URL** instead of a configured remote name, the match is
**anchored**: the entry must name that URL's `host/path` or its bare `host`.
Host-less, wildcard and prefix entries authorize nothing there, because that URL
came from the command line rather than the operator's git config.

By default, no repositories are auto-allowed for push. Both the grant and the
refusal name the checked target; a grant also names the entry that authorized it
and which source it came from, and a refusal lists the sources consulted plus an
example entry that would allow the push.

**Pushing a release tag**:
Release tags go through the same gate. `gate-push --tag NAME` pushes
`refs/tags/NAME` (`--ref REFSPEC` pushes an explicit refspec instead; the two
are mutually exclusive) to the remote named by `--remote NAME` (default
`origin`). The tag ref must be covered by a `repo|refs/tags/*` allow-list
entry — a bare entry does not authorize it.
```bash
gate-push --tag v1.2.3                  # refs/tags/v1.2.3 -> origin
gate-push --tag v1.2.3 --remote github  # same tag -> the GitHub mirror remote
```

**Publishing an authorized rebased branch**:
First obtain explicit human operator permission for the specific history
rewrite. Only then set the approval variable for that command and use an
explicit lease through the gate:
```bash
LMER_FORCE_PUSH_EXPLICITLY_APPROVED_BY_USER=1 gate-push --force-with-lease refs/heads/feature/example OLD_OID --remote origin
```
Before rebasing, use `gate-push --inspect-lease refs/heads/feature/example
--remote origin` to inspect the push transport/host/port and its current commit.
Keep that full, nonzero lowercase commit ID as `OLD_OID`; do not print raw
remote URLs, which may contain credentials. Inspection omits URL userinfo,
path, query and fragment, withholds captured Git failure output, and disables
Git's inherited trace/diagnostic sinks throughout inspection, including
configuration-backed Trace2 targets. It does not grant push authorization.
The source is the clean, checked-out
branch's committed HEAD; the destination must be covered by the normal
allowlist. Generic task/development authorization and repository/ref allowlisting
alone are insufficient. Existing explicit human approval for the same scoped
rewrite need not be requested again; no direct `git push` exception is needed.

`LMER_FORCE_PUSH_EXPLICITLY_APPROVED_BY_USER` must be exactly `1`: unset, empty,
whitespace and all other values refuse before gates, source pinning or transport.
This is a workflow checkpoint, not independent authentication of human consent.
Keep it command-scoped; never export it for the session, add it to defaults,
presets, taskdefs or persistent `.env` files, or automatically enable it after
refusal. Obtain explicit operator permission first, then set it only for the
approved command. Ordinary pushes and read-only inspection do not require it.

Git receives that exact lease with the real push. A stale expectation refuses
the update without an automatic refresh, retry, or unconditional force.
Leased mode supports one fully qualified branch on one push URL, and refuses
tags, deletions, wildcard/compound refspecs, mirror remotes and advertised
symbolic destination branches. It cannot be combined with `--tag` or `--ref`.
The selected remote's custom service selectors (`receivepack`, `uploadpack`,
`vcs`, `serverOption`), custom URL helpers, `GIT_OVERRIDE_VIRTUAL_HOST` and
`GIT_NAMESPACE` are unsupported for leased pushes and inspection. Required credential helpers,
hooks, proxies and SSH programs retain their existing trust boundary.
See [gate-push usage and guarantees](../docs/GATE-PUSH.md) for source locking,
Git compatibility, credential routing and receipt semantics.

**Pre-push Checklist**:
1. Use `gate-push` command
2. Gate will verify the remote URL AND the target ref against
   `LMER_PUSH_ALLOW_LIST` and the taskdef's `push_allow`
3. Request permission if not on allow list — the refusal names the checked
   target, the sources consulted, and an example entry that would allow it
4. User must configure allow list via env var (or ship it in the taskdef)

## 🚨 MR Target Policy — target the branch you forked from
**CRITICAL**: An MR's target branch is ALWAYS the branch you forked from.
There are no "special" changes that target elsewhere — release version
bumps, changelog rolls, docs, and hotfixes all follow the same edge as
feature work.

In repos with a `prep-release` integration branch (this org's standard
flow), that means:
- Every MR you create targets `prep-release` — never `main`.
- `main` only ever receives the standing `prep-release` → `main` release
  MR, merged by a human as part of the release/deploy process.
- Do NOT infer the target from repository history (e.g. old
  `release/vX.Y.Z` branches merged straight to `main`) — history records
  superseded processes; the rules and project info record the current one.

If a change genuinely seems to require targeting `main` directly, STOP and
ask the human — never create such an MR on your own judgment.

## 🚨 Thread Resolution Policy — only the reviewer resolves
Resolution of a review discussion thread is the reviewer's verified
sign-off, not housekeeping:
- **The author of a fix NEVER resolves the thread.** Reply on the thread
  naming what changed and the commit SHA, then leave it open for the
  reviewer.
- **Only the reviewer resolves**: a human, or a REVIEW session that
  re-checked the pushed fix (reply "verified in <sha>" before resolving).
- Resolving your own fix's thread is a policy violation. Tooling enforces
  this: `gitlab-review`/`github-review --resolve-thread` refuse to run in
  non-review sessions (`LMER_TASK` set and not `review`).

## 🚨 Merge Policy — review must be COMPLETE, not just quiet
**CRITICAL**: Before merging any MR/PR (via UI, API, or a local merge pushed
to the target branch), verify ALL of:
1. **No unresolved discussion threads** (`gitlab-review <proj> <mr> --comments`;
   system "mentioned in commit" notes don't count).
2. **No un-re-reviewed fixes**: if commits were pushed to the source branch
   after the reviewer's last pass, the reviewer must re-review first —
   author-resolved threads violate the Thread Resolution Policy above and
   are never review sign-off (`--info`'s thread-provenance block surfaces
   who resolved what, and when resolution predates the MR's head commit).
3. Pipeline green (or the human explicitly waives it).

A human instruction to merge does not waive these checks — verify and
surface the review state; merge only when it's clean or the human explicitly
overrides after seeing it. Note that a local `git merge` + push bypasses
GitLab's server-side merge checks entirely — which is exactly why this rule
exists.

**Merge method by edge**:
- **Feature branch → `prep-release`: ALWAYS squash** (one commit per
  feature; the full history stays on the feature branch / in the MR).
- **`prep-release` → `main`: NEVER squash** (regular merge commit — the
  release history must be preserved).

## Branch Management
- Create descriptive branch names
- Keep branches up to date with the branch you forked from
- Delete branches after merging

## Commit Messages
- Write clear, concise commit messages
- Focus on why, not what
- Reference issue numbers when applicable
