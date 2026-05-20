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

## See Also

- `gitlab-review` - GitLab merge request and issue management
- `/start` - Load task instructions (supports `finish` and `phasic` work modes)
