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
  `state.yaml` + `events.jsonl` per run, written only through the `work` CLI,
  wired into session hooks.
- **Layer 2 — orchestration artifacts (per-task, additive):** `spec.md`,
  `plan.md`, `retro.md` written next to the state file only when the taskdef
  produces them.

Layout, per project (`{host}/{project}/` already namespaces by project):

```
{host}/{project}/runs/<slug>/          # or runs/<slug>--<name>/ after the freeze rename
├── state.yaml     # Layer 1: authoritative run state (single writer: work CLI)
├── events.jsonl   # Layer 1: append-only session/audit log
├── log.yaml       # worklog (`work log`) — structural since the run-dir unification
├── reports/       # timestamped report files (`work report`)
├── spec.md        # Layer 2: agreed approach from the develop interview
├── plan.md        # Layer 2: execution plan (markdown checklist in v1)
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
| `plan.md` | the implementation plan |
| `retro.md` | close-out: what was done, key decisions, gotchas |
| `ledger.md` | execution ledger (per-task commits, review outcomes) |
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
`artifact_written`, `gate`, `run_dir_renamed` (the freeze-gate rename, §1).
The type set is open-ended — later growth adds types without schema churn.

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
| `work resume [--json]` | Pure decide function: reads state + events, prints a resume brief — slug, status, phase, stop_reason, goal, last ~5 events, artifacts, owner-claim warning if applicable. `--json` for hooks/machines. Never exits non-zero — an unreadable or missing run degrades to a message, not a failure. |
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
  `work artifact`) print an error to stderr and exit 1 — scripts chaining
  them under `set -e` should expect that. Session behavior without a repo is
  unchanged from before this feature existed.

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

- `plan.index.json` task DAG, wave-based execution, planner/decomposer agent
  briefs.
- Receipt-based anti-fabrication hardening of gates.
- Frozen goal-sets with plan-coverage checks and finish-time per-goal
  assessment.
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
