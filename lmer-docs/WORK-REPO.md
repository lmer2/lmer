# Work Repository Management Tool

The `work` command provides utilities for managing project-specific information and logs in the work repository. The work repository is automatically cloned to `/work` when you start a task.

**Important**: The work repository is entirely for tracking and project-specific information (logs, notes, metadata), **not** for actual work to be done. All actual development work happens in the project repository at `/workspace`.

**Note**: All `work` commands operate on the work repository (at `/work`), not the project repository you're working on.

## Quick Reference

The command is available as: `work <command> [options]`

## Commands

### Read Project Info

Read and display project information from info directories:

```bash
work read-project-info
```

This command concatenates all `.md` files from:
- `{host}/{project}/info/` - Global project information
- `{host}/{project}/{task_type}/info/` - Task-specific information

**Example:**
```bash
work read-project-info
```

### Log Messages

Log a message to the work repository:

```bash
work log "Your log message here"
```

With optional metadata:

```bash
work log "Task completed" --metadata status=success duration=5m
```

Display recent log entries (last 50 lines):

```bash
work log
```

Logs are written to `log.yaml` in the target work directory: `{host}/{project}/{task_type}/{task_target}/log.yaml`

When run without a message, `work log` displays:
- The location of the log file
- The last 50 lines of the log file (or fewer if the file has fewer lines)
- A truncation message if the file has more than 50 lines

**Examples:**
```bash
# Simple log message
work log "Started code review"

# Log with metadata
work log "Review completed" --metadata issues_found=3 severity=low

# Display recent log entries
work log
# Output:
# Log file: /work/github.com/owner/repo/review/pr-123/log.yaml
#
# (truncated to last 50 lines)
# - timestamp: 2024-01-01T12:00:00Z
#   message: Started code review
# - timestamp: 2024-01-01T12:05:00Z
#   message: Reviewed 5 files
#   files_reviewed: 5
# ...
```

### Commit Changes

Commit and push changes to the work repository:

```bash
work commit
```

With custom commit message:

```bash
work commit --message "Updated project logs"
```

This performs: `git fetch → git pull → git add → git commit → git push` in the work repository (not the project repository being worked on).

**Examples:**
```bash
# Commit with auto-generated message
work commit

# Commit with custom message
work commit --message "Added review logs for MR-123"
```

### Report Files

Copy a report file to the work repository with a timestamped filename:

```bash
work report --file report.md
```

The file will be copied to: `{host}/{project}/{task_type}/{task_target}/{YYMMDD-HH-MM-SS.md}`

**Examples:**
```bash
# Copy a report file
work report --file report.md

# Copy with short flag
work report -f analysis.md
```

### Set/Display Goal

Set or display a temporary context/goal for the current session:

```bash
# Set a goal
work goal "description of current goal"

# Display current goal
work goal
```

The goal is stored temporarily in `/tmp/lmer_work_goal.txt` and persists across CLI invocations but is not permanently saved (cleaned up on system restart). This is useful for tracking the current objective or context during a work session.

**Examples:**
```bash
# Set a goal for the current session
work goal "Fix authentication bug in login endpoint"

# Check what the current goal is
work goal
# Output: Fix authentication bug in login endpoint

# If no goal is set
work goal
# Output: No goal set
```

## Project-Specific Gate Configuration

An optional `gate-check.yaml` (or `.yml`) in the project info directory lets you tune `gate-check` behavior per project. The file is read directly from `{LMER_WORK_REPO_PATH}/{host}/{project}/info/gate-check.yaml`.

Currently supported keys:

```yaml
secrets:
  ignore:
    # Whole-file paths or fnmatch globs, relative to the project repo root.
    # Files matched here are skipped by gate-check's secret scan.
    - tests/util.py
    - mainsite/settings/run_tests.py
    - "mainsite/settings/run_*.py"
```

The file is optional — if it is missing, malformed, or omits a key, gate-check
falls back to its default behavior. Use this to allowlist files that legitimately
contain test fixtures, placeholder API keys, or sample credentials.

Patterns are matched against the path *relative to the project repo root* using
Python's `fnmatch`. Note that `fnmatch`'s `*` does **not** treat `/` as a
boundary — `mainsite/settings/run_*.py` will also match
`mainsite/settings/run_x/y/z.py`. If you want to match a single filename
anywhere in the tree, use `*name.py` (rather than expecting git-style `**`
semantics, which `fnmatch` does not implement).

## Directory Structure

The work repository uses the following directory structure:

```
{host}/{project}/info/                    # Global project info
{host}/{project}/info/gate-check.yaml     # Optional per-project gate-check config
{host}/{project}/{task_type}/info/        # Task-specific info
{host}/{project}/{task_type}/{task_target}/log.yaml  # Logs
{host}/{project}/{task_type}/{task_target}/{YYMMDD-HH-MM-SS.md}  # Reports
```

**Example:**
```
github.com/owner/repo/info/              # Project-wide info
github.com/owner/repo/review/info/       # Review task info
github.com/owner/repo/review/pr-123/log.yaml  # PR-123 logs
github.com/owner/repo/review/pr-123/241215-14-30-45.md  # Timestamped report
```

## Environment Variables

The `work` command uses the following environment variables (set automatically by lmer):

- `LMER_WORK_REPO_PATH` - Path to work repository (default: `/work`)
- `LMER_REPO_HOST` - Git service host (e.g., `github.com`, `gitlab.example.com`)
- `LMER_REPO_PROJECT` - Project path (e.g., `owner/repo`)
- `LMER_TASK` - Task type (e.g., `review`, `develop`, `modernize`)
- `LMER_TASK_TARGET` - Task target (e.g., `pr-123`, `mr-456`, branch name)

## Common Workflows

### Logging Task Progress

```bash
# Log start of task
work log "Starting code review"

# Log progress
work log "Reviewed 5 files" --metadata files_reviewed=5

# View recent log entries
work log

# Log completion
work log "Review completed" --metadata issues_found=2 status=approved
```

### Reading Project Context

```bash
# Read project information before starting work
work read-project-info
```

### Committing Work Repository Changes

```bash
# After logging or updating info files, commit changes
work commit --message "Updated review logs"
```

### Saving Reports

```bash
# Copy a report file to the work repository
work report --file report.md

# The file will be saved with a timestamp
# Then commit the changes
work commit --message "Added report"
```

### Managing Session Goals

```bash
# Set a goal at the start of a session
work goal "Review authentication changes in MR-123"

# Check the current goal anytime
work goal

# Update the goal as work progresses
work goal "Focus on security review of auth endpoints"
```

**Note**: The `work goal` command is particularly useful when using `/start phasic` mode, as it helps track objectives for each phase of work. In phasic mode, Claude will automatically set goals for each phase and check them regularly.

## Run state (Run D.M.C.)

Every session maintains durable, machine-readable state for its run —
`state.yaml` and an append-only `events.jsonl` — at
`{host}/{project}/runs/<slug>/` in the work repo. (Older runs may still
hold a legacy `state.yml`; it is read transparently and migrated to
`state.yaml` on the run's first mutation.) This is separate from
`log.yaml`/reports above: those are narrative, this is structured and safe
for hooks and external tools to read.

**Single-writer rule: never edit `state.yaml` directly.** All mutation goes
through the `work` CLI, which does atomic writes and appends the matching
event. Editing the file by hand (or from a script outside `work`) breaks the
single-writer guarantee the state layer depends on.

**Fail-soft guarantee:** the state layer never breaks your session.
`work session-start` and `work session-end` always exit 0, `work resume`
never exits non-zero, and the gate/goal integrations never change their
host command's behavior or exit code — state-layer errors are reported and
skipped. Without a run context (`LMER_REPO_HOST`/`LMER_REPO_PROJECT`
unset), the read-only and hook-facing verbs (`work state`, `work resume`,
`work session-start`, `work session-end`) print a "no run context" message
and exit 0, but the mutating verbs (`work state set`, `work event`,
`work artifact`) print an error to stderr and exit 1 — scripts chaining
them under `set -e` should expect that.

### Show current state

```bash
work state
```

Prints the run's `state.yaml` (read-only). If no run exists yet, says so and
exits 0.

### Update state

```bash
work state set --phase=implementation --stop-reason=question --status=in-progress \
  --critical-error='{"summary": "tests hang", "detail": "..."}'
```

- `--phase=<free-form string>` — record the current phase (taskdef-defined).
- `--stop-reason=<question|yield|complete|critical_error|none>` — why the
  session is stopping. `question` = waiting on a human; `yield` = a
  deliberate phasic phase-end; `complete` = the run is done;
  `critical_error` = actually broken (routine blockers are `question`, not
  this). Use `--stop-reason=none` to clear it back to unset:

  ```bash
  work state set --stop-reason=none
  ```
- `--status=<in-progress|complete|archived>` — the run's overall status.
- `--critical-error=<json>` — a JSON object (`{"summary": ..., "detail":
  ...}`); required when `--stop-reason=critical_error` is given.

At least one of `--phase`/`--stop-reason`/`--status`/`--critical-error` is
required. Only `--phase` is compared against the current value:
re-submitting the same `--phase` with no other flags short-circuits with
`State unchanged` and writes nothing. Passing `--stop-reason`, `--status`,
or `--critical-error` always writes state and appends a `state_changed`
event, even when the value is identical to what's already recorded — and
`--status=complete` triggers a work-repo push each time, so an idempotent
retry of the close-out command re-pushes (harmless, but not silent).

### Name the run

```bash
# Set (or change) the run's human-readable name
work name auth-refactor

# Display the current name
work name
```

Records a short kebab-case `name:` in the run state (input is normalized:
lowercased, spaces/underscores become hyphens). Auto-derived slugs like
`develop-issue-123` are hard to find later — the name is the label you'll
recognize, shown first in the resume brief:
`Run: auth-refactor (slug: develop-issue-123, status: in-progress)`.

Names are **unique per project**: a name already held by another run — via
its `name:` or its directory slug — is rejected with the conflicting slug
named. Renaming is free (each rename appends a `run_named` event);
re-setting the run's own current name is a no-op. Set the name early, once
direction is clear — an unanswered name proposal may default to accepted,
unlike task questions.

### Record an event

```bash
work event review_posted --note "posted round 2 comments" --data '{"count": 4}'
```

Appends one line to `events.jsonl`. `--note` and `--data` (a JSON object)
are both optional. The run is auto-seeded if this is the first `work`
invocation for it.

### Print the resume brief

```bash
work resume
work resume --json
```

Reads state + recent events and prints a human-readable brief (slug,
status, phase, stop_reason, goal, last ~5 events, artifacts, and an
owner-claim warning if another session has the run claimed). `--json` emits
the same decision as machine-readable JSON, for hooks/scripts.

### Register a durable artifact

```bash
work artifact spec.md --file /tmp/agreed-approach.md
```

Copies the file into the run dir (secrets redacted) under a plain filename
(no paths, no leading dot), registers it in `state.artifacts`, and appends
an `artifact_written` event.

### Masterplan artifact links

```bash
work artifact --sync
```

For masterplan-mode runs (a `masterplan/<mp-slug>/` bundle nested in the run
dir), maintains relative symlinks at the run-dir root for the bundle's
well-known artifacts (`spec.md`, `goals.md`, `plan.md`, `plan.html`,
`retro.md`) so they are findable without digging into the bundle, and
registers them in `state.artifacts`. The same sync runs automatically during
`work commit` and `work session-end`, so you rarely need the manual form.
Fail-soft: sync problems never change the host command's exit code; runs
without a bundle are untouched.

### Session hooks

```bash
work session-start   # hook-facing: seed/claim the run, print the resume brief
work session-end     # hook-facing: release the claim, push run state
```

These back the `/start` render pipeline and the `SessionEnd` Claude Code
hook respectively — you generally won't invoke them by hand. Both always
exit 0.

### `work goal` also records into the run

When a run context exists, `work goal "description"` now additionally
writes the goal into `state.yaml` and appends a `goal_set` event, in
addition to its existing `/tmp` goal-file behavior — no change to how you
call it.

See [docs/RUN-STATE.md](../docs/RUN-STATE.md) for the full design: schema,
slug derivation, session lifecycle, and the external cleaner contract.

## See Also

- `gitlab-review` - GitLab merge request and issue management
- `/start` - Load task instructions (supports `finish` and `phasic` work modes)
