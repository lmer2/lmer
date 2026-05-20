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
Set it to a comma-separated list of repository path patterns:
```bash
LMER_PUSH_ALLOW_LIST="org/repo1,org/repo2"
```
By default, no repositories are auto-allowed for push.

**Pre-push Checklist**:
1. Use `gate-push` command
2. Gate will verify repository against `LMER_PUSH_ALLOW_LIST`
3. Request permission if not on allow list
4. User must configure allow list via env var

## Branch Management
- Create descriptive branch names
- Keep branches up to date with main
- Delete branches after merging

## Commit Messages
- Write clear, concise commit messages
- Focus on why, not what
- Reference issue numbers when applicable
