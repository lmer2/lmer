# Global Development Configuration

## 🚨 Quick Reference - Critical Rules
- **NEVER** use `git add -A` or `git add .` - ONLY add specific files you've modified
- **ALWAYS** use gate commands for git operations
- **ALWAYS** write documentation for new features and APIs
- **ALWAYS** deliver specs, plans, and reports as Markdown (`.md`) — never docx/pdf/binary documents
- **ALWAYS** check that ALL tests pass before declaring anything complete
- **ALWAYS** check git rules before commit
- **NEVER** push to any repository without explicit permission (except allow list)
- **NEVER** commit without explicit user approval or request
- **NEVER** expose API keys, tokens, or secrets in code or logs

## 🤖 NON-INTERACTIVE SESSIONS - when there is nobody to ask
`LMER_NONINTERACTIVE` set to a truthy value (`1`, `true`, `yes`, case-insensitive)
means no human is attached to this session — a `spawn-harness` child, a cron run, any
headless launch. A falsy value (`0`, `false`, `no`) or an unset variable means a human
is present and the gates below ask normally. You do not have to go looking for the
variable: when it is set, this session was told so in its prompt or its global context.
The situations it names are also self-identifying — an agent that already knows it is
headless applies this section whether or not it ever saw the flag.

In such a session **no gate below may end your turn with a question**. An unanswered
prompt is not a pause, it is a dropped result: the caller gets a near-empty output file
and your work vanishes from the fan-out with no error surfaced.

**Approval already granted before the session started is still approval.** The launch
prompt, the taskdef instructions, and `/gate-commit` all authorize what they name — a
headless run whose whole purpose is to produce committed work still commits, and still
runs the gate's checks first. This section covers approvals you would have to obtain
*now*, from someone who is not there.

When a gate would stop you for approval you do not already have:
- Do NOT ask for approval as your final answer.
- Do NOT perform the gated action either — approval you cannot get is approval you do
  not have.
- Stop that line of work and state plainly in your final output what you would have
  asked, why you stopped, and what you completed before stopping. The caller needs a
  report, not a prompt.

Per-gate, in a non-interactive session:
- **COMMIT GATE** — run `gate-check`, show its output, and commit under the approval
  the launch already carries. Without such approval, stop at the checks and report.
- **CONTEXT SWITCH GATE** — state the switch (completed / new objective / files) in
  your output and proceed; do not stop for the confirmation step.
- **ERROR GATE** — for the STOP cases, use the report format below with its
  non-interactive closing line.

## 🛑 COMMIT GATE - MANDATORY CHECKS
Before EVERY commit, you MUST:
1. Run gate check: `gate-check`
2. Show gate check results to user
3. Get explicit approval: "commit" / "please commit" / `/gate-commit` command
4. Use gate commit: `gate-commit -m "message"`

**IMPORTANT**: The `/gate-commit` command IS explicit approval to commit:
- When user runs `/gate-commit`, generate appropriate message if none provided
- Do NOT ask for further confirmation - the command itself is the approval
- Follow all rules: run checks, show output, use gate commands

**NEVER use git commit directly - ONLY use gate commands**
**NO EXCEPTIONS - Even for "simple" changes**

## 📋 Show Your Work Policy
**ALWAYS demonstrate, never just claim:**
- ALWAYS paste command output, don't summarize
- ALWAYS show error messages in full
- ALWAYS include file paths when discussing files (with line numbers when relevant)
- NEVER say "it works" without showing proof
- NEVER say "tests pass" without showing the output
- NEVER claim "I checked" without showing what you checked

## Rule Modules

This configuration is organized into focused modules. Each module contains specific rules for its domain:

### 📁 Core Rule Modules
- [`rules/git.md`](rules/git.md) - Version control, commits, pushes
- [`rules/testing.md`](rules/testing.md) - Test framework, coverage, fixtures
- [`rules/code-quality.md`](rules/code-quality.md) - Code organization, linting, style
- [`rules/security.md`](rules/security.md) - Secrets, vulnerabilities, dependencies
- [`rules/documentation.md`](rules/documentation.md) - README, docstrings, comments
- [`rules/ci-cd.md`](rules/ci-cd.md) - GitHub Actions, monitoring, verification
- [`rules/dependencies.md`](rules/dependencies.md) - Lock files, package management

### 🎯 Quick Access Commands
Use these focused commands:
- `rgr` - Read all global rules
- `rgr-git` - Read only git rules
- `rgr-test` - Read only testing rules
- `rgr-security` - Read only security rules
- `rgr-code` - Read only code quality rules

### 🔒 Gate Commands (MANDATORY)
- `gate-check` - Run all checks without committing
- `gate-commit -m "message"` - Check and commit if passes
- `gate-push` - Check repository and push if allowed

**Invoke these bare, NOT as `bin/gate-*`.** They are on `$PATH` (installed at `/Agents/global/bin/`). The `bin/gate-*` form is ONLY valid when `LMER_SELF_DEV=1` (developing lmer itself), where it resolves to the in-workspace development copy.

**Timeouts**: Gate commands run tests and pre-commit hooks and can take several minutes. When invoking via the Bash tool, always set `timeout` to at least `300000` (5 minutes). Do not accept the default 2-minute timeout for gate commands.

## Development Practices

### Configuration Verification
- Always request paste verification for external configs
- Watch for subtle differences (release.yml vs release.yaml)
- Verify exact settings from both sides during troubleshooting
- Document configuration requirements

### Work Tracking
- Update WORKLOG.md with brief task overviews
- Document what was done for each task
- Include context for future reference

### Error Handling & Recovery
- Always provide clear error messages with context
- Include suggested fixes for common errors
- Log errors appropriately (never expose secrets)
- Implement graceful degradation where possible
- Clean up resources on failure

## 🛑 CONTEXT SWITCH GATE
**When switching between tasks, STOP and:**
- [ ] Summarize what was just completed
- [ ] Run rgr-[relevant module] for new task (e.g., rgr-git, rgr-test)
- [ ] State the new objective clearly
- [ ] List specific files/areas you'll be working with
- [ ] STOP: Confirm understanding with user

**Example:**
```
Completed: Added test coverage for user authentication
Switching to: Bug fix for login timeout
Reading: rgr-git (for commit rules)
Objective: Fix timeout issue in auth.py line 45
Will modify: src/auth.py, tests/test_auth.py
Is this correct?
```

## 🛑 ERROR GATE - When a fix needs authorization

Always show the full error, never summarize it away (see Show Your Work Policy).
Whether you STOP depends on what the *fix* costs, not on the fact that something
failed:

- **Your own malformed command** (bad regex, wrong flag, wrong path) — fix it and
  continue; report it in one line.
- **Environment or capability gap** (dependency absent, host unreachable, credential
  missing) — always report it, then keep working the problem. An equivalent substitute
  (`grep` for a missing `rg`) or another sound route to the same result is normal work;
  you do not need permission per attempt — just name what you used. **STOP and ask**
  when only the human can supply what is missing — credentials, network access, an
  install — or when every remaining route degrades the deliverable: stubbing, skipping
  a check, faking data, a tool whose semantics differ. Auth failures are always the
  human's call (see Authentication & Access).
- **STOP and get approval** when the fix would mutate state, is hard to reverse, or
  touches something a gate protects: force-pushing after a rejected push, editing
  production code so a test passes, re-running a half-applied migration.
- **STOP and get approval** when you do not understand the cause, or when you have run
  out of approaches you can justify — you are guessing, or each remaining option is
  worse than the last. Grinding on without a diagnosis is the churn this gate exists to
  prevent.

Visibility is unconditional in all four cases; only the STOP cases wait for a human.
No silent retries — every fix attempt is reported, and undiagnosed ones need approval.

**For the STOP cases, format your error report like this:**
```
❌ ERROR ENCOUNTERED:
[Paste the complete error message here]

📍 WHERE: [File path, line number, or command that failed]

🔍 ANALYSIS: [What I think caused this error]

🔧 PROPOSED FIX: [Specific steps I want to take]

💭 WHY THIS FIX: [Reasoning for this approach]

Shall I proceed with this fix? (yes/no)
```

In a non-interactive session, replace that closing question with
`⏸️ STOPPED — would have asked: "…". Not proceeding.` and end the turn there. Never
emit the `(yes/no)` line when nobody is attached to answer it.

## Authentication & Access

### Authentication Handling
- Always prompt for auth issues (which may show up as 404s to GitHub and GitLab)
- Do not fall back to different methods without user confirmation
- Show the actual error message when authentication fails
- Request credentials or tokens explicitly when needed

## Communication & Reporting

### Status Updates
- Provide concise status updates during long operations
- Use clear, actionable language in error messages
- Document assumptions and decisions in code comments
- Keep WORKLOG.md updated with task progress

### Communication Guidelines
- Be specific about what's happening
- Estimate time for long operations
- Report failures immediately
- Document workarounds and solutions

## Claude Code Configuration

### Documentation Reference
**Official Claude Code Documentation**: https://docs.anthropic.com/en/docs/claude-code/claude_code_docs_map.md
- Settings configuration: https://docs.anthropic.com/en/docs/claude-code/settings
- Permissions and access control details
- Hooks and custom commands

### Settings Files
- `.claude/settings.json` - Shared team settings (committed to repo)
- `.claude/settings.local.json` - Personal settings (gitignored)
- `.claude/commands/` - Custom slash commands

**Note**: Wildcard syntax in permissions may have limitations. Use specific command patterns where possible.

## Work Repository Management
The `work` command provides utilities for managing project-specific information and logs in the work repository. The work repository is automatically cloned to `/work` when you start a task.

**Important**: The work repository is entirely for tracking and project-specific information (logs, notes, metadata), **not** for actual work to be done. All actual development work happens in the project repository at `/workspace`.

**Note**: All `work` commands operate on the work repository (at `/work`), not the project repository you're working on.

#### Read Project Info
Read and display project information from info directories:

```bash
work read-project-info
```

This command concatenates all `.md` files from:
- `{host}/{project}/info/` - Global project information
- `{host}/{project}/{task_type}/info/` - Task-specific information

#### Log Messages
Log a message to the work repository:

```bash
work log "Your log message here"
```

With optional metadata:

```bash
work log "Task completed" --metadata status=success duration=5m
```

Logs are written to `log.yaml` at the run-dir root in the work repository: `{host}/{project}/runs/{slug}/log.yaml`

#### Commit Changes
Commit and push changes to the work repository:

```bash
work commit
```

With custom commit message:

```bash
work commit --message "Updated project logs"
```

This performs: `git fetch → git pull → git add → git commit → git push` in the work repository (not the project repository being worked on).

#### Set/Display Goal

Set or display a temporary context/goal for the current session:

```bash
# Set a goal
work goal "description of current goal"

# Display current goal
work goal
```

The goal is stored temporarily and persists across CLI invocations but is not permanently saved. Useful for tracking the current objective during a work session.

#### Persist Agent Memory

When `LMER_PERSIST_AGENT_MEMORY` is enabled, your agent memory is persisted to
the work repo on a **per-project** basis (stored under
`{host}/{project}/memory/`) so it survives across sessions.

- **Restore is automatic.** At session start, any previously-saved memory for
  this project is copied back into your memory directory before you begin — you
  do not need to do anything to load it.
- **Persisting is your responsibility.** Memory you write during a session lives
  only in the ephemeral container until you save it. Before you finish a task,
  run:

  ```bash
  work memory persist
  ```

  This copies your current memory into the work repo and commits and pushes it.
  Run it whenever you've recorded memory worth keeping (and again at the end of
  the task). It is a no-op when `LMER_PERSIST_AGENT_MEMORY` is disabled, so it is
  always safe to call.

Only persist durable, project-level facts to memory — never credentials,
secrets, or customer data, since the work repo is shared.

## Masterplan On Demand

Any session can turn on the masterplan workflow mid-session. When the user
asks to masterplan something (e.g. "let's masterplan this") and the
`/masterplan` command is not available:

1. Run `/Agents/global/libexec/masterplan-enable.sh`.
2. If it exits 2 reporting the run dir is indeterminate, ask the user which
   repo host/project the work is for, then re-run it with
   `--repo-host <host> --repo-project <project>`. Never guess a project.
3. When it succeeds, ask the user to type `/reload-plugins` (a built-in you
   cannot run for them), then proceed with `/masterplan`.

The script is idempotent and non-fatal; masterplan bundles land inside the
work-repo run directory (`MASTERPLAN_RUNS_DIR`), never in the code repo.

## Efficient Command Usage

### Avoid Compound Commands
Do NOT chain commands with `&&`, `||`, `;`, or pipes when each command can be run separately. Compound commands trigger permission prompts because only the first command in the chain is matched against the allow list.

**Bad** (triggers permission prompt):
```bash
cd /workspace && git log --oneline -10
which uv && uv pip install -e ".[dev]" 2>&1 | tail -10
```

**Good** (runs without prompting):
```bash
git log --oneline -10
```
```bash
uv pip install -e ".[dev]"
```

If you need to run commands sequentially and they depend on each other, make separate Bash tool calls. If you need output from one command to feed another, capture it in a variable or read the output between calls.

### Use Dedicated Tools Instead of Bash
Claude Code provides dedicated tools that are always permitted and give better output:
- **Read** instead of `cat`, `head`, `tail` — for reading file contents
- **Grep** instead of `grep`, `rg` — for searching file contents
- **Glob** instead of `find`, `ls` — for finding files by pattern
- **Edit** instead of `sed`, `awk` — for modifying files
- **Write** instead of `echo >`, `cat <<EOF` — for creating files

### Environment Information
Use `env` (already permitted) to inspect environment variables. Do NOT use elaborate `echo` chains:

**Bad** (triggers permission prompt):
```bash
echo "VAR1=$VAR1"; echo "VAR2=$VAR2"; env | grep -i '_TOKEN' | sed 's/=.*/=***/'
```

**Good** (runs without prompting):
```bash
env
```

### Commit Messages
Keep gate-commit messages on a single line or use simple multi-line format. Do NOT use `$(cat <<'EOF' ...)` subshells:

**Bad** (triggers permission prompt):
```bash
gate-commit -m "$(cat <<'EOF'
long multi-line message
EOF
)"
```

**Good** (runs without prompting):
```bash
gate-commit -m "fix: short description of the change"
```

### File Operations in /tmp
Operations in `/tmp` are permitted. Use simple, direct commands — do not wrap them in compound expressions.

## Debugging Best Practices

### Debug Guidelines
- Use proper logging frameworks (not print statements)
- Include context in log messages
- Clean up debug code before committing
- Use debugger tools when available
- Document complex debugging sessions

### Debug Cleanup Checklist
- [ ] Remove console.log/print statements
- [ ] Remove temporary debug code
- [ ] Update logging levels appropriately
- [ ] Document any permanent debug helpers

---
*Last Updated*: Check git history for latest changes
*Note*: User-specific additions can be placed in `~/.lmer/AGENTS.md` (appended after the global config)
*Structure*: Rules are modularized in the `rules/` directory for better organization
