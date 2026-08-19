# Rule Enforcement Hooks

Automated enforcement for development shortcuts that ensure rules are followed.

## Installation

```bash
./hooks/install.sh
```

This will:
- Install command wrappers in `~/.local/bin`
- Set up git hooks for commit/push enforcement
- Create tracking directories in `~/.claude`

## Commands

### Basic Commands

- `rgr` - Read Global Rules with tracking
- `rgrpc` - Read rules and run COMMIT GATE checks
- `pc` - Please Commit with gate verification

### Focused Commands

- `rgr-git` - Git & version control rules only
- `rgr-test` - Testing rules only
- `rgr-code` - Code quality rules only
- `rgr-security` - Security rules only
- `rgr-docs` - Documentation rules only
- `rgr-ci` - CI/CD rules only
- `rgr-deps` - Dependency rules only

## How It Works

### Rule Reading Tracking
When you run `rgr` or any focused command, the hook:
1. Verifies rule files exist
2. Records timestamp of reading
3. Shows recent violations
4. Highlights critical sections

### Commit Gate Enforcement
The `rgrpc` command:
1. Checks rules were read recently
2. Runs tests automatically
3. Runs pre-commit checks
4. Shows all results
5. Records gate passage

### Commit Verification
The `pc` command:
1. Verifies COMMIT GATE was passed (<1 hour ago)
2. Checks for staged changes
3. Scans for potential secrets
4. Creates commit with your message
5. Offers to push if repo is on allow list

## Git Hook Integration

### Pre-commit Hook
- Verifies COMMIT GATE was passed recently
- Runs pre-commit if installed
- Blocks commits without gate passage

### Pre-push Hook
- Checks repository against allow list
- Blocks pushes to unauthorized repos
- Shows clear error messages

## Tracking Files

Located in `~/.claude/`:
- `last_rgr_timestamp` - When rules were last read
- `gate_passages.log` - COMMIT GATE passages
- `commits.log` - Commits made via pc
- `violations.log` - Rule violations (if any)

## Claude Code Stop Hooks

### run_state_guard.py

Registered as a `Stop` hook in `agent-files/claude/settings.json` (after
`slack_reply_guard.py`). When the agent yields, it enforces two independent
nudges:

- **Run-state recording** - if the workspace shows real activity (feature
  branch, dirty tree, or unpushed commits) while the run's `phase`, `goal`,
  or `name` is still unset, the stop is blocked with the exact `work`
  commands to record them. Fires **once per session**.
- **Push before stop** - if the run dir in the work repo has uncommitted
  changes or commits its upstream lacks, the stop is blocked with a
  `work commit` nudge. Fires on every non-compliant stop, **capped at 3 per
  session**, so an environment where pushing genuinely fails cannot nag
  forever.

Kill switch: `LMER_RUN_STATE_GUARD` with `get_bool_env` semantics — unset or
truthy enables the guard; `LMER_RUN_STATE_GUARD=0` disables it.

The hook fails open: unreadable payload, git errors, `work` failures, or
sentinel/counter I/O errors all result in exit 0 with no output. It only
reads state and never mutates the run, the workspace, or the work repo.

### signal_guard.py

Registered as the third `Stop` hook in `agent-files/claude/settings.json`.
In an orchestrated session (`LMER_ASK_DIR` set), it blocks a stop once when
the turn shows an unreported milestone: a successful milestone-shaped command
in the transcript (the `_MILESTONE_PATTERNS` list — `gate-push`,
`gitlab-review --create-mr`/`--review-file`/`--reply-thread`,
`github-review --review-file`, the `gitlab-review-post-review.sh` /
`github-review-post-review.sh` wrappers, `work state set --status=complete`)
or a run record reporting itself complete, with no signal-equivalent act after
it (a successful `lmer-signal`; a newer signal file in the channel dir, but
only when the transcript holds no signal of its own — ordered evidence wins
over a file with no position in the turn; or a newly opened `lmer-ask`
question). The reminder asks the agent to run `lmer-signal`; the hook **never
signals on the agent's behalf** — a signal must keep meaning a milestone. The
GitLab and GitHub post-review wrappers own their milestone instead: after the
review command succeeds they call `lmer-signal` directly. Signalling is
best-effort: no orchestrator channel or no installed signal command warns but
does not turn an already-posted review into a failed wrapper that a caller might
retry. A failed review exits with the review command's status and never signals
success.

Fires once per distinct milestone, capped at 3 per session via a `/tmp`
marker keyed on `LMER_SESSION_ID` and written atomically (a torn marker reads
as corrupt, which would disable the guard for the session). Kill switch:
`LMER_SIGNAL_GUARD` with `get_bool_env` semantics. Fan-out children
(`LMER_NONINTERACTIVE`) are skipped entirely — a `claude -p` child's only
output is its last turn, so a Stop block would replace the result its parent
is waiting for. Fails open on every error path, including a marker write
failure (which drops the nudge rather than risk an uncapped one).

Both channel-dir suppressors are bounded against baselines in the marker, so
one signal (or one long-lived open question) cannot silence every later
milestone in the session.

Adding a milestone-shaped command to lmer means adding a row to
`_MILESTONE_PATTERNS` — the list is the single place the guard learns what a
milestone looks like, and
`tests/test_signal_guard.py::TestPatternsMatchRealCommands` pins each row's
flag against the real argparse parser (a row once named a `--post-review`
flag that no CLI has).

The list is known spellings, not a classifier: a milestone reached through a
slash command (which produces no Bash `tool_use` block), a subagent, or an MCP
tool is invisible, so silence from this hook means "nothing to report" rather
than "nothing happened".

## Testing

```bash
python -m pytest tests/test_hooks.py -v
```

## Customization

Edit the hooks to add:
- Additional validation rules
- Custom gate checks
- Integration with your tools
- Violation reporting
