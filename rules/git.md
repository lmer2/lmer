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
- **NEVER** commit without explicit user approval or request
- **NEVER** add Claude attributions or signatures

## 🚨 Commit Rules - STOP AND CHECK
- **MUST** use `gate-commit -m "message"` for all commits
- **NEVER** use `git commit` directly
- **NEVER** commit without explicit user approval (user must say "commit", "please commit", "pc", "rgrpc")
- **BEFORE committing:**
  1. ✅ Run `gate-check` to verify all checks pass
  2. ✅ Show user the gate check results
  3. ✅ Summarize what changed
  4. ✅ Get explicit user approval
- **STOP**: Wait for user to review changes before committing
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
The push allow list is configured via the `LMER_PUSH_ALLOW_LIST` environment variable.
Set it to a comma-separated list of entries, each either `repo` or `repo|refpattern`:
```bash
LMER_PUSH_ALLOW_LIST="org/repo1,org/repo2|refs/tags/*"
```
- A bare `repo` entry is matched as a substring of the remote URL and authorizes
  **branch refs only** (`refs/heads/*`) — bare entries never grant tag pushes.
- A `repo|refpattern` entry matches `refpattern` (a glob) against the
  fully-qualified target ref: `org/repo|refs/tags/*` grants tag pushes,
  `org/repo|refs/heads/main` grants exactly one branch.
- Malformed entries (empty half, more than one `|`) are ignored — the list
  fails closed, never open.
- The substring rule applies to **configured remotes**, whose URL the operator
  set up. When git is handed a **URL** instead of a remote name, the match is
  anchored on the parsed identity and the entry must name `host/path` or the
  bare `host` — `org/repo` alone authorizes nothing there, because any forge
  can serve that path.

By default, no repositories are auto-allowed for push.

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

**Pre-push Checklist**:
1. Use `gate-push` command
2. Gate will verify the remote URL AND the target ref against `LMER_PUSH_ALLOW_LIST`
3. Request permission if not on allow list
4. User must configure allow list via env var

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

## 🚨 Merge Policy — review must be COMPLETE, not just quiet
**CRITICAL**: Before merging any MR/PR (via UI, API, or a local merge pushed
to the target branch), verify ALL of:
1. **No unresolved discussion threads** (`gitlab-review <proj> <mr> --comments`;
   system "mentioned in commit" notes don't count).
2. **No un-re-reviewed fixes**: if commits were pushed to the source branch
   after the reviewer's last pass, the reviewer must re-review first —
   author-resolved threads are NOT review sign-off.
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
