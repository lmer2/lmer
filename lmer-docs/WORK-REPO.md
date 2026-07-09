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

Logs are written to `log.yaml` at the run-dir root: `{host}/{project}/runs/{slug}/log.yaml` (the run dir is the single home for run output). Displaying falls back to the legacy `{host}/{project}/{task_type}/{task_target}/log.yaml` location when the run dir has no log yet.

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
# Log file: /work/github.com/owner/repo/runs/review-pr-123/log.yaml
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

Only the current run dir (and the legacy task-target dir) are staged, so a file
added elsewhere in the work repo is left untouched. After committing, `work
commit` therefore prints a **reminder** listing any untracked or unstaged items
that remain anywhere in the work repo — a repo-wide `git status --porcelain`,
capped with a `... and N more` tail — so a stray file (for example a new
`{host}/{project}/info/*.md`) is not silently left uncommitted. The reminder is
advisory only: it never changes the commit's exit code, and prints nothing when
the tree is clean. Add and commit any flagged items manually if they should be
kept.

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

The file will be copied to: `{host}/{project}/runs/{slug}/reports/{YYMMDD-HH-MM-SS.md}` (inside the run dir — the single home for run output).

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
{host}/{project}/runs/{slug}/             # Run dir — the single home for run output
{host}/{project}/runs/{slug}/log.yaml     # Logs
{host}/{project}/runs/{slug}/reports/{YYMMDD-HH-MM-SS.md}  # Reports
```

**Example:**
```
github.com/owner/repo/info/              # Project-wide info
github.com/owner/repo/review/info/       # Review task info
github.com/owner/repo/runs/review-pr-123/log.yaml  # PR-123 logs
github.com/owner/repo/runs/review-pr-123/reports/241215-14-30-45.md  # Timestamped report
```

A named run's dir may carry the name-bearing form `runs/{slug}--{name}/`
after its pre-execution freeze; never address a run by directory name —
the `work` CLI resolves runs by the `slug:`/`name:` recorded in
`state.yaml`. Runs that predate the run-dir unification keep their
log/report files at the legacy `{task_type}/{task_target}/` location,
which `work log` still reads as a display fallback.

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
`state.yaml`, an append-only `events.jsonl`, and (once the run has plan
tasks to track) a per-task `ledger.yaml` — at
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
`work ledger`, `work session-start`, `work session-end`) print a "no run
context" message and exit 0, but the mutating verbs (`work state set`,
`work event`, `work verify`, `work artifact`, `work ledger set`) print an
error to stderr and exit 1 — scripts chaining them under `set -e` should
expect that.

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

### Record a validation receipt

```bash
work verify tests -- pytest tests/ -q
work verify lint -- uv run pre-commit run --all-files
```

Runs the command (stderr merged into stdout, `2>&1`-style), streams its
output through, **mirrors its exit code**, and appends a `verify` receipt
event to `events.jsonl`: `{name, argv, exit_code, duration_s, summary_line,
output_tail_sha256}`. The receipt is written by the tool process — never
typed by the model — which is what makes it proof that the validation
actually ran. `output_tail_sha256` is the sha256 of the last 64 KiB of
output, so the receipt is checkable later without storing the output
itself; `summary_line` is the last non-empty output line, and both it and
`argv` are secret-redacted before landing in the work repo.

The `--` separator is **required**. A signal-killed command exits (and is
recorded as) `128+N`, shell-style; a command that cannot start exits 127.

Use this for every validation named in a plan task's `verify:` line that
isn't already a gate command — `gate-check`/`gate-commit`/`gate-push`
record their own `gate` receipts automatically. Claiming a plan task
complete requires a matching receipt from the current session.

This is a mutating verb: without run context it errors *before* running
the command. If the receipt append fails after the command ran, a loud
stderr warning is printed but the exit code still mirrors the command.

### Track plan tasks in the execution ledger

```bash
# Record a task's state — the same-breath rule: gate-commit, then this
work ledger set T2 --status done --commit 4a1f9c2 --receipt t2-tests
work ledger set T3a --status in-progress --title "resume brief line"

# Show the ledger table (read-only; "No ledger" + exit 0 when none exists)
work ledger
```

`ledger.yaml` holds one row per plan task (`pending | in-progress | done |
deferred | dropped`, plus optional `title`/`commit`/`receipt`/`note`), so a
crashed run's successor recovers by reading state, not by diffing.
`work ledger set` is the **only** writer (atomic, single-writer — never
edit the file by hand; gates never write it either): it upserts the row
(fields omitted on a later write are preserved), appends a `task` event to
`events.jsonl`, and pushes the run dir. Task ids come from plan.md;
hand-named ids are fine.

Record `done` **immediately after the gate-commit that lands the task**,
with its sha — `--receipt` names the `verify`/gate receipt that proves it.
`done` with no `--commit` warns loudly but succeeds (docs-only tasks
exist). A Stop-hook nudge fires (once per session) when a session has
landed gate-commits without writing any ledger row.

### Check the plan index

```bash
work plan check
```

Read-only lint of the run's `plan.index.json` — the machine-readable
companion you author beside plan.md when the plan has more than one task
(and register with `work artifact plan.index.json --file <path>`). Schema
v1: per task `id`, `description`, `files` (declared write-scope,
project-relative paths/globs), `deps`, `verify_commands`, and
`session_scope: "one"` — or `"multi"` plus a `scope_rationale` saying why
the task cannot fit one session. Genuinely shared touchpoints between
independent tasks (CHANGELOG.yaml, docs indexes) are declared in a
top-level `shared_files` allowlist: `{"T2+T4": ["CHANGELOG.yaml"]}`.

The lint verifies what plan-gate approval otherwise takes on faith:
**errors** (exit 1) on dependency cycles, unknown/duplicate task ids, file
overlap between dependency-independent tasks not covered by
`shared_files`, and missing session-scope declarations; **warnings**
(exit 0) on plan.md checkbox/index count drift, tasks with no
`verify_commands`, unresolvable `goals` refs (when goals.md exists),
active goals with no covering task (a task's `goals` ref, or a plan.md
mention as fallback), and stale `shared_files` entries. Findings print to
stdout so the report can go straight into the plan-approval request — the
plan gate wants plan.md presented **with a green `work plan check`**
included.

No plan index (or no run context) is a clean exit 0 with a message —
chat/review runs have no index and are never nagged. The command writes
nothing: no event, no push; safe to re-run anytime.

### Track the run's goal contract

```bash
work goals               # status: counts, hash, frozen/diverged
work goals check         # draft lint (read-only)
work goals freeze --note "approved in review thread"
work goals amend --note "scope grew at plan time"
work goals assess        # print the verdict skeleton + divergence report
work goals assess \
  --verdict 'G1=met:t1-tests receipt' \
  --verdict 'G2=partial:docs/FEATURE.md landed, examples pending'
```

`runs/<slug>/goals.md` is the run's goal contract (issue #91): drafted
during spec/brainstorm, **frozen at spec approval**, amended only
explicitly, assessed goal-by-goal at finish. Format: `## G<N>: <title>`
headings, each active goal carrying `signal:` (one of
`test|command|artifact|docs`) and `evidence:` (where the proof lives);
removed goals become tombstones (`tombstone_reason:`/`tombstone_at:`) and
ids never renumber.

- `check` — draft lint: structural problems error; missing
  `signal:`/`evidence:` warns (drafts may sketch; frozen goals may not).
- `freeze` — the spec-approval gate: strict validation, records
  `goals_frozen` with the canonical goal list + hash (put the approval
  context in `--note`), registers goals.md as an artifact, invokes the
  run's pre-execution freeze seam (named runs only — an unnamed run's
  seam is left to the phase gate so the one-shot dir rename isn't
  forfeited), pushes. Re-freezing errors — use amend.
- `amend` — every post-freeze change: validated against the last agreed
  set (tombstone-not-renumber), recorded as `goal_amended` with a
  per-goal diff. Editing goals.md without amending is silent divergence
  and assess will say so.
- `assess` — the finish step, BEFORE `work state set --status=complete`.
  Bare form prints the verdict skeleton and the receipt names you can
  cite; the `--verdict` form (one flag per active goal, complete map
  required, verdicts `met|partial|missed|waived`) records
  `goals_assessed` and prints the completed table — land it in retro.md.
  Evidence citing a recorded receipt or registered artifact is classified
  as such; free prose is allowed but marked `(prose)`.

Everything is nudge-don't-block in v1: divergence and coverage gaps are
reported loudly, never fatal.

### Print the resume brief

```bash
work resume
work resume --json
```

Reads state + recent events (+ ledger) and prints a human-readable brief
(slug, status, phase, stop_reason, goal, a `Ledger: 4/7 done, in-flight:
T3a, last commit 4a1f9c2` line when a ledger exists, last ~5 events,
artifacts, and an owner-claim warning if another session has the run
claimed). `--json` emits the same decision as machine-readable JSON — with
the full ledger — for hooks/scripts.

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

### Seed a run for another slug

```bash
work seed <taskdef> <target> [--goal "..."] [--name <kebab-case>]
# e.g.
work seed develop gate-receipts --goal "receipt-based gate hardening" --name gate-receipts
work seed review https://git.example.com/org/repo/-/merge_requests/456
```

Out-of-session run creation: derives the slug from the given taskdef +
target (exactly like a session would) and creates the run dir through the
same create-tmp → write-state → rename lifecycle, recording CLI-shaped
events (`run_seeded`, then `goal_set` / `run_named` when the flags are
given). Use this instead of ever hand-authoring `state.yaml` /
`events.jsonl` — hand-written files sidestep the single-writer rule.

Seeding is not owning: no `owner` claim is made, and nothing is pushed —
batch the push with `work commit`. An existing run for the slug, or a
`--name` already held by another run, is an error (exit 1).

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
