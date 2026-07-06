# Run D.M.C. — Durable Run State

**D**urable, **M**achine-readable, **C**rash-proof state for every lmer
session — not just orchestrated multi-phase runs. It replaces prose
archaeology ("read the worklog, guess what happened") with a small state
file and an append-only event log that any session, hook, or external tool
can read. When a session dies, the resume brief tells the next one to walk
this way.

(The name is the fixed part; the acronym expansion is allowed to evolve.)

## 1. What it is

lmer keeps a durable **run state** for every session, every taskdef. This is
hook-driven (structural), not prompt-dependent — it exists whether or not a
taskdef's instructions mention it.

Two layers:

- **Layer 1 — run state (always-on, lmer core):** small, universal, stable.
  `state.yaml` + `events.jsonl` per run — plus `ledger.yaml` once the run
  has plan tasks to track (issue #89) — written only through the `work`
  CLI, wired into session hooks.
- **Layer 2 — orchestration artifacts (per-task, additive):** `spec.md`,
  `plan.md`, `retro.md` written next to the state file only when the taskdef
  produces them.

Layout, per project (`{host}/{project}/` already namespaces by project):

```
{host}/{project}/runs/<slug>/          # or runs/<slug>--<name>/ after the freeze rename
├── state.yaml     # Layer 1: authoritative run state (single writer: work CLI)
├── events.jsonl   # Layer 1: append-only session/audit log
├── ledger.yaml    # Layer 1: per-task execution ledger (single writer: work ledger set)
├── log.yaml       # worklog (`work log`) — structural since the run-dir unification
├── reports/       # timestamped report files (`work report`)
├── spec.md        # Layer 2: agreed approach from the develop interview
├── plan.md        # Layer 2: execution plan (markdown checklist in v1)
├── plan.index.json# Layer 2: machine-readable task index (linted by work plan check)
└── retro.md       # Layer 2: close-out summary
```

Archived runs move to `{host}/{project}/archive/<slug>/` (see §6, the
external cleaner contract).

### Run-dir lifecycle

The `work` CLI owns the directory's whole lifecycle (issue #87):

- **Create as tmp, rename when seeded.** Every run dir starts as a
  `runs/.new-<session>-*` temp dir; the state/events seed is written there
  through the normal single writer, then one atomic `rename()` moves it to
  `runs/<slug>/`. No observer ever sees a half-seeded dir at a canonical
  name; a crash in between leaves only a sweepable `.new-*` orphan (§6).
- **One more rename, exactly once, at the pre-execution freeze.** The first
  `work state set --phase=<p>` where `<p>` falls outside the planning
  family (case-insensitive prefix match against `spec`, `plan`,
  `brainstorm`, `explor`, `design`, `review`, `branch`, `issue`,
  `interview`, `setup`, `retriev` — covering the shipped taskdefs'
  pre-execution phases; an unrecognized phase counts as execution) stamps
  `state.frozen` and — when the run is named — renames
  the dir to its name-bearing final form `runs/<slug>--<name>/`. A run
  still unnamed at the freeze keeps `runs/<slug>/` with no second chance:
  the `frozen` stamp is what makes "one rename means one" durable. Runs
  that never leave the planning family (e.g. `chat`) are never renamed.
  Once `work goals freeze` exists it will invoke this same seam.
- **One resolver, all verbs.** Because dirs can be renamed, a directory
  name is never a valid address: every verb and hook resolves the run dir
  by scanning `runs/*/state.yaml` for a matching `slug:` (then `name:`) —
  cheap at current scale, and name uniqueness is already enforced. Resume
  stays deterministic: the same taskdef + target derives the same slug and
  finds the run regardless of any rename. Dot-dirs and `archive/` are
  never matched.
- **Legacy layout fallback.** `work log` / `work report` used to write to
  `{host}/{project}/{task_type}/{target}/`; those dirs get read-side
  fallback only (display falls back when the run dir has no `log.yaml`).
  The CLI never moves legacy files — an optional one-time migration is the
  external cleaner's business (§6).
- **Fail-soft.** Resolver, rename, and seed problems are reported and
  skipped; they never change a host command's exit code.

### Run artifacts convention

The run directory is the **single home for everything a run produces** —
machine state and human artifacts alike. Nothing about a run belongs in the
target repo, the lmer repo, or ad-hoc notes elsewhere: if it isn't under
`runs/<slug>/`, it gets lost. Beyond the Layer 2 files above, the canonical
artifact set is:

| File | Contents |
|---|---|
| `spec.md` | the agreed approach / approved design |
| `plan.md` | the implementation plan — each task carries a `verify: <command or gate>` line naming the validation that proves it (its receipt, §2) |
| `plan.index.json` | machine-readable companion to plan.md — the task DAG with declared write-scopes, linted by `work plan check` (§2) |
| `retro.md` | close-out: what was done, key decisions, gotchas |
| `followups.md` | deferred/follow-up work tracked out of the run |
| `reports/` | per-task or per-review reports, when the run produced them |

Register session-written artifacts with `work artifact <name>.md --file
<path>` so they land in `state.artifacts` with an audit event. Host-side or
manual runs follow the same convention with plain files and a work-repo
commit. The work repo's own README documents the full repository layout.

### Masterplan artifact links

Masterplan bundles nest at `runs/<slug>/masterplan/<mp-slug>/`, which would
leave their key artifacts hard to find from the run dir. The work layer
therefore syncs **relative symlinks** at the run-dir root pointing into the
bundle (e.g. `spec.md -> masterplan/develop-run-naming/spec.md`) — both ends
live in the same work-repo checkout, so relative links survive git
round-trips and can never drift the way copies do.

- **What syncs:** the well-known bundle artifacts, when present: `spec.md`,
  `goals.md`, `plan.md`, `plan.html`, `retro.md`. Each linked artifact is
  also registered in `state.artifacts` through the single writer (so it
  appears in the resume brief).
- **When:** during `work commit` and `work session-end` (before staging), so
  every push self-maintains the links; `work artifact --sync` invokes the
  same sync manually.
- **Naming:** with a single bundle under `masterplan/`, links use the plain
  artifact names; with several bundles, links are prefixed
  `<mp-slug>-<name>` (e.g. `develop-run-naming-plan.md`). A pre-existing
  regular file at a link name (e.g. a manually copied spec.md) is replaced
  by the symlink — the bundle copy is canonical.
- **Fail-soft:** like the rest of the state layer, sync errors are reported
  and skipped; they never change the host command's behavior or exit code.
  Runs without a `masterplan/` dir are untouched.

## 2. `state.yaml` schema

```yaml
schema: 1
slug: develop-issue-123
name: auth-refactor          # human-set label via `work name`; null until set
taskdef: develop
target: https://gitlab.example.com/org/proj/-/issues/123
status: in-progress          # in-progress | complete | archived
phase: interview             # free-form string, taskdef-defined; null for chat
stop_reason: question        # null | question | yield | complete | critical_error
critical_error: null         # {summary, detail} — only when stop_reason=critical_error
goal: "align on auth refactor approach"
artifacts:                   # present only once registered via `work artifact`
  spec: spec.md
owner: null                  # {session_id, claimed_at} while a session is live
frozen: null                 # UTC stamp of the pre-execution freeze gate (§1); null until it fires
created: 2026-07-03T14:02:11Z
updated: 2026-07-03T15:40:03Z
```

**Stop-reason semantics:** `question` = waiting on a human; `yield` =
deliberate phase-end stop (phasic mode); `complete` = run finished;
`critical_error` = actually broken, and *only* then — routine blockers are
`question`, not `critical_error`. `taskdef` and `target` are set at seed time
and treated as immutable.

`name` and `frozen` are additive — **schema stays 1**, no migration: older
readers ignore unknown keys, and the schema guard only refuses *newer*
schema numbers.

**File rename, lazy migration:** the state file is `state.yaml` (lmer-owned
YAML files standardize on `.yaml`). Run dirs written before the rename hold
a legacy `state.yml`; it is read whenever `state.yaml` is absent, and on the
run's first write the new `state.yaml` is written and the leftover legacy
file is renamed to `state.yml.migrated` (kept for post-mortem, out of the
resolution path). Each run therefore migrates on its first mutation and is
never left readable-but-stale.

`events.jsonl` is one JSON object per line: `{ts, session, type, note, data}`
(`note` and `data` optional). Core event types: `run_seeded`, `session_start`,
`session_end`, `phase`, `state_changed` (non-phase mutations to
status/stop_reason/critical_error), `goal_set`, `run_named`,
`artifact_written`, `gate`, `verify`, `task` (every ledger mutation, §2),
`run_dir_renamed` (the freeze-gate rename, §1). The type set is open-ended
— later growth adds types without schema churn.

### Receipt events (`gate`, `verify`)

Receipts are the anti-fabrication layer (issue #88): proof that a named
validation command actually ran, written into `events.jsonl` by the tool
process itself — never typed by the model. Two producers:

**`gate` events** — emitted by `gate-check`/`gate-commit`/`gate-push`,
exactly one per invocation, at command end:

```json
{"ts": "…", "session": "…", "type": "gate",
 "note": "gate-check: pass",
 "data": {"gate": "gate-check", "outcome": "pass", "exit_code": 0,
          "duration_s": 142.3, "summary": "1397 passed in 140.2s",
          "argv": ["gate-check"]}}
```

- `outcome` (`pass` | `fail` | `bypass`) carries the check verdict;
  `exit_code` is the whole command's result — a push that fails *after*
  passing checks is `outcome: pass` with a non-zero `exit_code`.
- `summary` is a best-effort parse of the run (the test runner's tail line
  on pass, the failed check names on fail); absent when unparseable —
  never fabricated. A `bypass` receipt carries no summary (nothing ran).
- `gate-commit` receipts additionally record `commit_sha` whenever a commit
  actually landed — including bypass commits. It is read from HEAD
  immediately after the commit (best-effort). The sha is the natural join
  key for ledger-side attribution, as a *cross-check*: gates stay
  ledger-unaware entirely (no `--task` flags, no auto-add — one writer for
  the task↔receipt mapping, and it is not the gates).
- Receipt text that could echo arbitrary content — `summary`, `argv`
  elements (gate-commit's argv carries the commit message), verify's
  `summary_line` — is secret-redacted before landing in the work repo.
- Fail-soft contract unchanged: with no run context the gates behave
  byte-identically, and no receipt failure can ever change a gate's exit
  code.

**`verify` events** — emitted by `work verify <name> -- <command …>` (§4),
the receipt path for validation that isn't a gate command:

```json
{"ts": "…", "session": "…", "type": "verify",
 "note": "tests: pass",
 "data": {"name": "tests", "argv": ["pytest", "tests/", "-q"],
          "exit_code": 0, "duration_s": 41.9,
          "summary_line": "1397 passed in 41.8s",
          "output_tail_sha256": "8b35e1c7…"}}
```

`output_tail_sha256` is the sha256 of the last 64 KiB of the command's
combined output (stderr merged into stdout, `2>&1`-style), so a receipt is
checkable after the fact without the work repo storing bulky output —
receipt bodies are hash-only by design. `summary_line` is the last
non-empty output line (secret-redacted), absent when there was no output.

**Validation contracts:** plans name the validation that proves each task —
a `verify: <command or gate>` line per plan.md task (see the artifacts
convention, §1) — and claiming a task complete requires a matching
`verify`/`gate` receipt from the current session. v1 enforcement is soft:
the finish/retro step compares claims to receipts; a guard-hook nudge is
deliberately deferred (§7).

### `ledger.yaml` — per-task execution ledger

The ledger (issue #89) is what makes a crashed orchestrated run recoverable
by *reading state* instead of diff archaeology: one row per plan task,
carrying exactly what the next session needs — is it landed (which commit,
which receipt), in flight, or untouched.

```yaml
schema: 1
tasks:
  T2:
    title: lmer wiring
    status: done        # pending | in-progress | done | deferred | dropped
    commit: 4a1f9c2     # optional; project-repo sha
    receipt: t2-tests   # optional; name of a verify/gate receipt event
    note: "22 tests, env passthrough + guard"
    updated: 2026-07-05T04:12:03Z
```

- **Single writer, atomic writes** — the `state.yaml` contract exactly:
  only `work ledger set` mutates it (tmp+rename, corrupt files backed up
  as `ledger.yaml.bad-<stamp>`, newer schema is a read-only refusal).
  Fields omitted on a later write are preserved, so
  `work ledger set T2 --status done --commit <sha>` keeps an earlier title.
- **Snapshot + audit trail** — every mutation also appends a `task` event
  (`{task, status, commit?, receipt?}` in `data`), so `ledger.yaml` is the
  at-a-glance current state and `events.jsonl` stays the history.
- **Task ids come from plan.md** (or `plan.index.json` when the run has
  one); hand-named ids are fine — the ledger does not depend on the plan
  format.
- **The same-breath rule** (taskdef fragment): the moment a gate-commit
  lands a plan task, record it — gate-commit, then
  `work ledger set <id> --status done --commit <sha>`, before moving on.
  A Stop-hook nudge (trigger 3 of `hooks/run_state_guard.py`, same
  `LMER_RUN_STATE_GUARD` kill switch, once per session, fail-open) fires
  when a session has landed gate-commits but written no ledger row.
- **Gates stay ledger-unaware** — `work ledger set` is the only writer of
  the task↔commit mapping; the `commit_sha` on gate receipts exists for
  finish-time cross-checking (every ledger `--commit` sha should match a
  `gate` event), never as a write path into the ledger.
- **`done` with no `--commit` warns loudly but succeeds** — docs-only
  tasks exist; the warning is for the forgotten-sha case.
- Each mutation pushes the run dir (like `work artifact`) — an unledgered,
  unpushed row is exactly what a dead session loses.
- The ledger is machine-authoritative and has no rendered `ledger.md`
  counterpart; humans read `work ledger` (or the YAML itself).

### `plan.index.json` — checkable plan gates

The machine-readable companion to plan.md (issue #90): plan-gate approval
becomes partially machine-checkable instead of resting on properties humans
assert by hand. Authored alongside plan.md (never generated from its
prose), registered like any artifact
(`work artifact plan.index.json --file <path>`), linted by
`work plan check` (§4). Schema v1, field-compatible with the masterplan
fork's plan index where the two overlap (`id`, `description`, `files`,
`verify_commands`, `goals`) so a future unification is a merge, not a
migration:

```json
{"schema": 1,
 "tasks": [
   {"id": "T2", "description": "lmer wiring",
    "files": ["src/lmer_cli/cli.py", "libexec/claude-runner.sh"],
    "deps": ["T0.1"],
    "verify_commands": ["gate-check"],
    "session_scope": "one",
    "goals": ["G1", "G3"]}],
 "shared_files": {"T2+T4": ["CHANGELOG.yaml"]}}
```

- `files` — the task's **declared write-scope**: project-repo-relative
  paths, globs allowed. Overlap detection treats two entries as colliding
  when they are equal or either matches the other as an fnmatch pattern
  (so `src/*.py` collides with `src/foo.py`, and — fnmatch's `*` crossing
  `/` — `src/*` collides with `src/a/b.py`: over-detection, never
  under-detection); glob-vs-glob pairs that don't textually match each
  other are a documented v1 limitation.
- `session_scope` — `"one"` (atomic: completable in a single session) or
  `"multi"` plus a required `scope_rationale` string. The point is not
  enforcement — it forces the atomicity question to be answered in
  writing at plan time, when descoping is cheap.
- `shared_files` — top-level allowlist exempting declared pairs' genuinely
  shared touchpoints (CHANGELOG.yaml, docs indexes) from the overlap rule;
  entries use the same overlap semantics as `files`, so a glob entry
  covers the literal paths it matches — and an entry exempts a collision
  only when it covers *both* colliding sides.
- `goals` — optional cross-reference plumbing for goal-coverage linting
  (soft until `develop-goal-freeze` lands).

The lint stops deliberately at plan time: wave-based *execution*
(dispatch, worktrees) and runtime write-scope enforcement stay out of
scope — run-state is a state layer (§7).

## 3. Slug derivation

Per project (the `{host}/{project}/` prefix already namespaces):

- Target parses as an issue → `<taskdef>-issue-<id>` (e.g. `develop-issue-123`)
- Target parses as an MR/PR → `<taskdef>-mr-<id>` (e.g. `review-mr-456`)
- Full 40-hex commit SHA → truncated to the 12-char short form
  (e.g. `review-2a418231d83e`)
- Named branch, no issue/MR → `<taskdef>-<slugified-branch>`
- No target (plain project session) → `<taskdef>` (e.g. `chat` — one
  long-lived per-project run; events accumulate per session)

Same target + same taskdef ⇒ same slug ⇒ a fresh container lands on the
existing run — the resolver matches the recorded `state.slug`, never the
directory name, so legacy full-SHA runs stay addressable by their original
slug (no aliasing between the two forms).

**Completed-run policy:** if the slug resolves to a run with
`status: complete`, the session does not re-seed — the resume brief reports
the completed run, and the session reopens it (`work state set
--status=in-progress`) only if new work on that target is actually
requested. Runs the external cleaner has moved to `archive/` no longer
occupy the slug, so a genuinely new engagement seeds a fresh run.

## 4. CLI verbs

| Verb | Behavior |
|---|---|
| `work state` | Print current run state (read-only). |
| `work state set --phase=… --stop-reason=… --status=… [--critical-error=<json>]` | The only mutation path. Constrained fields; atomic write; bumps `updated`; appends a corresponding event. Re-submitting the same `--phase` value with no other flags short-circuits with `State unchanged` and does not write; the other flags always write (see below). |
| `work event <type> [--note "…"] [--data <json>]` | Append one event line. Auto-seeds the run if it doesn't exist yet. |
| `work verify <name> -- <command …>` | Run the command (stderr merged into stdout), stream its output through, mirror its exit code, and append a `verify` receipt event (§2): `{name, argv, exit_code, duration_s, summary_line, output_tail_sha256}`. The `--` separator is required (a forgotten name must not silently become the command). A signal-killed command mirrors the shell convention `128+N` (receipt and observed exit code agree); a command that cannot start exits 127. Mutating-verb rules: without run context this errors *before* running the command. A receipt-append failure after the command ran is reported loudly on stderr but the exit code still mirrors the command. |
| `work resume [--json]` | Pure decide function: reads state + events (+ ledger), prints a resume brief — slug, status, phase, stop_reason, goal, a one-line ledger summary when a ledger exists (`Ledger: 4/7 done, in-flight: T3a, last commit 4a1f9c2`), last ~5 events, artifacts, owner-claim warning if applicable. `--json` for hooks/machines and carries the full ledger. Never exits non-zero — an unreadable or missing run (or ledger) degrades to a message, not a failure. |
| `work ledger` | Print the execution ledger table (read-only): the summary line plus one row per task. With no ledger prints `No ledger`, exit 0. |
| `work ledger set <task-id> --status <s> [--title …] [--commit <sha>] [--receipt <name>] [--note …]` | The only mutation path for `ledger.yaml` (§2): upserts the row (omitted fields preserved), stamps `updated`, appends a `task` event, pushes the run dir. `--status` is one of `pending\|in-progress\|done\|deferred\|dropped`; `done` with no `--commit` warns loudly but succeeds. |
| `work plan check` | Read-only lint of the run's `plan.index.json` (§2). Errors (exit 1): invalid/newer schema, structural problems (non-string/duplicate ids, missing description), unknown `deps` ids, dependency cycles, file overlap between dependency-independent tasks not declared in `shared_files`, missing/invalid `session_scope`, `multi` without `scope_rationale`. Warnings (exit 0): plan.md checkbox count drifting from the index task count, empty `verify_commands`, `goals` refs that don't parse from goals.md (`## G<n>:` headings; skipped when goals.md is absent), stale/malformed `shared_files` entries. Findings print to stdout so the report can be pasted into the plan-approval request. No run context or no `plan.index.json` prints a message and exits 0 (chat/review taskdefs have no index); writes nothing — no event, no push. |
| `work name <kebab-case>` | Set the run's name (a label — the directory slug never changes). Normalizes to kebab-case (lowercase; spaces/underscores → `-`; strip other characters; collapse/trim `-`), printing the normalized form when it differs; errors if nothing survives. Names are **unique per project** — a name held by another run is rejected with an error citing the conflicting slug. Renaming is allowed anytime (same uniqueness check; appends another `run_named` event — history lives in the event log); re-setting the run's own current name is an idempotent no-op success. Bare `work name` prints the current name (or "No name set"), read-only, exit 0. |
| `work artifact <name> --file <path>` | Copy the file into the run dir (secret-redacted), register it in `state.artifacts` (through the single writer, keyed by the artifact's filename stem), append `artifact_written`. `<name>` must be a plain filename (no path components, no leading dot). |
| `work seed <taskdef> <target> [--goal …] [--name …]` | Out-of-session run creation: derives the slug from its args and creates a run for it through the same create-tmp → write-state → rename lifecycle, recording CLI-shaped events (`run_seeded`, then `goal_set` / `run_named` as given). Does **not** claim `owner` (seeding is not owning) and does **not** push — batch with `work commit`. An existing run for the slug (or a name conflict) is an error. |
| `work session-start` | Hook-facing. Seed the run if absent (via the tmp-then-rename lifecycle), decide the resume brief *before* claiming, append `session_start`, claim `owner`, print the brief. Always exits 0. |
| `work session-end` | Hook-facing. Append `session_end`, clear `owner` if it's ours, push the run-state path via `work commit`. Always exits 0. |

`work state set` field choices: `--stop-reason` is one of
`question|yield|complete|critical_error|none` (`none` clears it back to
`null`); `--status` is one of `in-progress|complete|archived`;
`--critical-error` takes a JSON object (`{"summary": ..., "detail": ...}`)
and is required when `--stop-reason=critical_error` is given.

**No-op behavior:** only `--phase` is compared against the current value —
re-submitting the same `--phase` with no other flags short-circuits with
`State unchanged` and writes nothing. Passing `--stop-reason`, `--status`,
or `--critical-error` always writes state and appends a `state_changed`
event, even when the value is identical to what's already recorded — and
`--status=complete` triggers a work-repo push each time, so an idempotent
retry of the close-out command re-pushes (harmless, but not silent).

`work goal` (existing verb, unchanged interface): when a run context exists
(`LMER_REPO_HOST`/`LMER_REPO_PROJECT` set), setting a goal additionally
records it into `state.yaml` and appends a `goal_set` event; the legacy
`/tmp` goal-file behavior is preserved unconditionally either way.

## 5. Session lifecycle

- **Session id:** minted once per container session by `claude-runner.sh`
  (UTC timestamp + pid + random, preserved if already set), exported as
  `LMER_SESSION_ID`, used in `owner` claims and every event's `session`
  field. Defaults to `"unknown"` if unset.
- **Start:** the `/start` render pipeline calls `work session-start`, which
  seeds the run if absent (`run_seeded`), appends `session_start`, claims
  `owner`, and returns a resume brief that gets injected into the rendered
  instructions. A session opening on an existing run *begins* with its
  state — resume is the default, not a recovery procedure.
- **End:** `work session-end` clears `owner` and pushes the run-state path.
  It runs twice by design: the `SessionEnd` Claude Code hook (best-effort —
  `command -v work` guarded, `|| true`) and, authoritatively, the harness
  teardown in `clone_and_exec.dispatch_runner` after the runner exits —
  Claude does not block its exit on SessionEnd hooks, so the hook's push can
  be killed by container teardown; the harness backstop runs while the
  container is still alive. A duplicate `session_end` event is harmless. A
  crashed session (no `session_end`, stale claim) is detectable by the next
  resume brief.
- **During:** the model sets `stop_reason` via `work state set` when
  yielding or asking a blocking question (taskdef fragments instruct this).
  Hooks are the unconditional backstop, so state stays truthful even with an
  uncooperative or dead session.
- **Fail-soft guarantees:** the state layer never breaks session start or
  end — `work session-start` and `work session-end` always exit 0; a broken
  state layer (corrupt file, missing work repo, unexpected exception) is
  reported to stdout/stderr but never breaks a session. `work resume`
  likewise never exits non-zero, and the gate/goal integrations never alter
  their host command's behavior or exit code. When there is no run context
  at all (`LMER_REPO_HOST`/`LMER_REPO_PROJECT` unset — e.g. `chat` without a
  repo), the read-only and hook-facing verbs (`work state`, `work resume`,
  `work session-start`, `work session-end`) print a "no run context" message
  and exit 0, but the mutating verbs (`work state set`, `work event`,
  `work verify`, `work artifact`, `work ledger set`) print an error to
  stderr and exit 1 — scripts chaining them under `set -e` should expect
  that. Session behavior
  without a repo is unchanged from before this feature existed.

## 6. External cleaner contract

The cleaner is an external process (cron or host command, user-owned).
Contract it can rely on: single-writer state, append-only events, `status`
enum, `updated` timestamps, `owner` claims. Actions:

- Move `runs/<slug>/` (or `runs/<slug>--<name>/`) → `archive/<slug>/` (or
  set `status: archived`) for runs that are `complete` or stale past a
  threshold.
- Sweep `runs/.new-*` orphans: a crash between run-dir create and rename
  leaves one behind; any `.new-*` dir stale past an mtime threshold is
  safe to delete (the resolver never matches dot-dirs).
- Optionally migrate legacy `{task_type}/{target}/` log/report dirs into
  the matching run dir — the CLI only ever falls back to them on read and
  never moves them itself.

## 7. Deferred growth path

Recorded so the design can be worked into deliberately, not accidentally:

- Wave-based execution and planner/decomposer agent briefs on top of the
  `plan.index.json` task DAG. The DAG itself — schema v1 plus the
  `work plan check` lint — shipped with issue #90 (§2); dispatch,
  worktrees, and runtime write-scope enforcement remain deferred.
- Hard anti-fabrication for receipts (hash-chained events, signed receipts)
  and a guard-hook nudge enforcing the claim↔receipt match. The soft v1 —
  structured gate receipts, `work verify`, plan validation contracts —
  shipped with issue #88 (§2).
- Frozen goal-sets with plan-coverage checks and finish-time per-goal
  assessment.
- Ledger-driven automated recovery (re-running stranded tasks) and
  resume-time commit-sha existence checks — the ledger (issue #89) makes
  state legible; acting on it stays with the session/human, and the brief
  never requires the project repo to be cloned.
- Durable finish/branch-fate gate objects.
- The external cleaner process itself (contract defined in §6 above).
- Multi-session GitLab/GitHub coordination — separate effort by explicit
  decision.
- A doctor check for run-state health (work-repo side; lmer's container
  doctor is unaffected).

## See also

- [lmer-docs/WORK-REPO.md](../lmer-docs/WORK-REPO.md) — the `work` CLI
  reference agents read in-container, including the `## Run state` section
  with per-verb usage examples.
- [INDEX.md](INDEX.md) — documentation index.
