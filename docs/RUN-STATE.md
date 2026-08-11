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
├── release.yaml   # Layer 1: release-run record (release runs only; single writer: work release, §7)
├── log.yaml       # worklog (`work log`) — structural since the run-dir unification
├── reports/       # timestamped report files (`work report`)
├── spec.md        # Layer 2: agreed approach from the develop interview
├── goals.md       # Layer 2: the run's goal contract (frozen at spec approval)
├── plan.md        # Layer 2: execution plan (markdown checklist in v1)
├── plan.index.json# Layer 2: machine-readable task index (linted by work plan check)
└── retro.md       # Layer 2: close-out summary
```

Archived runs move to `{host}/{project}/runs/archive/<slug>/` (see §6, the
external cleaner contract) — the archive subtree lives INSIDE `runs/`,
sharing its namespace: the resolver skips it and `archive` is a reserved
run name.

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
  `work goals freeze` invokes this same seam (§2) — spec approval and the
  pre-execution freeze mark the same gate.
- **One resolver, all verbs.** Because dirs can be renamed, a directory
  name is never a valid address: every verb and hook resolves the run dir
  by scanning `runs/*/state.yaml` for a matching `slug:` (then `name:`) —
  cheap at current scale, and name uniqueness is already enforced. Resume
  stays deterministic: the same taskdef + target derives the same slug and
  finds the run regardless of any rename. Dot-dirs and `runs/archive/` are
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
| `goals.md` | the run's goal contract — frozen at spec approval, amended explicitly, assessed goal-by-goal at finish (§2) |
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
stop_reason: question        # null | question | yield | complete | critical_error | aborted
critical_error: null         # {summary, detail} — only when stop_reason=critical_error
open_question: "sqlite or postgres for the cache?"  # only when stop_reason=question; null otherwise
goal: "align on auth refactor approach"
estimate:                    # optional, recorded via `work goal --estimate-*`; null until set
  sessions: 3                # estimated sessions until solved; null when not given
  time: "4h"                 # free-form human string ("4h", "2d") — stored verbatim, never parsed
artifacts:                   # present only once registered via `work artifact`
  spec: spec.md
owner: null                  # {session_id, claimed_at} while a session is live
claim: null                  # {session_id, claimed_at} — single-flight release claim (§7); release runs only
frozen: null                 # UTC stamp of the pre-execution freeze gate (§1); null until it fires
reslugged_from: []           # addresses this run has vacated (§7); absent on runs that never re-slugged
created: 2026-07-03T14:02:11Z
updated: 2026-07-03T15:40:03Z
```

**Stop-reason semantics:** `question` = waiting on a human; `yield` =
deliberate phase-end stop (phasic mode); `complete` = run finished;
`critical_error` = actually broken, and *only* then — routine blockers are
`question`, not `critical_error`; `aborted` = the release-run terminal
(`work release abort`, §7) — recorded on a `status: complete` run, purely
descriptive (nothing switches on it; the closed `status` enum is what
external consumers key on). `taskdef` and `target` are set at seed time
and treated as immutable.

**Open question (issue #97):** `open_question` holds the blocking question's
text so it survives the session that asked it (the transcript it would
otherwise live in is never pushed). It is set via
`work state set --stop-reason=question --question "<text>"` (secret-redacted,
recorded in the `state_changed` event's data) and cleared whenever
`--stop-reason` is set at all — even a fresh `question` stop starts blank
unless `--question` re-records the text, so a stale question never survives
any new stop. When
`stop_reason` is `question` and a question is recorded, the resume brief
renders it FIRST, before the run header, as a
`❓ OPEN QUESTION (answer before anything else):` block.

**Answering (issue #98):** the answer arrives in a FRESH session — the
asking session already ended (that is the #97 contract). Two delivery
paths, one mechanism: `work answer "<text>"` in-container, or
`lmer ... --answer "<text>"` on the host, which exports `LMER_ANSWER`
(flag-only — a host-exported or `.env` value is never forwarded; answers
are one-shot data, not standing configuration) so `work session-start`
applies it automatically before printing the brief
(only when the loaded state is actually stopped on a recorded question;
fail-soft — a problem applying it degrades to the plain brief). Both go
through `run_state.answer_question`: append a `question_answered` event
carrying `{question, answer}` (both secret-redacted), clear
`open_question`, clear `stop_reason`, write through the single writer.
`status` is never touched — an answered question on a completed run keeps
`status: complete`; reopening goes through the §3 completed-run directive.
The auto-applied path renders the brief leading with an
`✅ ANSWERED QUESTION` block (the question, the answer, and "proceed
accordingly — record the follow-up goal/phase as you go") in place of the
open-question block it just resolved.

**Session estimation (issue #99):** `estimate` records how big the run was
expected to be — set with the goal via
`work goal "<text>" --estimate-sessions N --estimate-time "<str>"` (either
flag alone is fine; `time` is a free-form human string like `4h` or `2d`,
stored verbatim and never parsed). The `goal_set` event echoes the estimate
in its data; a goal recorded without the flags behaves exactly as before
(no data payload). An estimate is scoped to the goal it was recorded with:
re-recording the goal without the flags clears any prior estimate back to
null (only goal writes clear — displaying the goal touches nothing). When
an estimate exists, the resume brief renders
`Estimate: ~3 sessions / 4h — used: 2 sessions` — the used count is the
run's `session_start` events so far (computed by the CLI callers that
already read events; `decide()` stays IO-free and just carries it). At
completion, `work state set --status=complete` appends the actuals into
the `state_changed` event's data as
`actuals: {sessions_used, first_session_at, completed_at}` — computed from
events by the tool process, fail-soft to null when events can't be read —
so estimate vs. actual is comparable per run with no extra state field.

`name`, `frozen`, `open_question`, `estimate`, and `claim` are additive —
**schema stays 1**, no migration: older readers ignore unknown keys (an old
state.yaml without the key reads as null), and the schema guard only
refuses *newer* schema numbers. Extending `stop_reason` with `aborted` is
additive on the same terms — `status` stays the closed three-value enum.

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
status/stop_reason/critical_error; on `status=complete` its data also
carries the estimate-vs-actual `actuals`), `question_answered` (the recorded open
question plus its human answer, both secret-redacted), `goal_set`, `run_named`,
`artifact_written`, `gate`, `verify`, `task` (every ledger mutation, §2),
`run_dir_renamed` (the freeze-gate rename, §1), `goals_frozen`,
`goal_amended`, `goals_assessed` (the goal-set lifecycle, §2), `claim`
(every release-claim mutation — claim/refresh/takeover/unclaim, §7),
`release` (every release.yaml mutation, §7 — prior receipt values live
here), `run_aborted` (the release-run abort, §7). The type set
is open-ended — later growth adds types without schema churn.

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
deliberately deferred (§8).

### Gate-in-flight coordination (`commit_deferred`)

Receipts are why this exists: a `gate` event appends to `events.jsonl`, so
**every gate run leaves the run dir dirty**. A full suite takes ~14 minutes,
which makes gating in the background the normal pattern — and yielding inside
that window fires the Stop hook, whose mandated `work commit` sweeps tracked
run-dir files into a commit underneath the running suite, failing its `/work`
isolation guard (`tests/conftest.py`). Self-sustaining, too: the receipt dirties
the run dir, which arms the nudge, which lands a commit inside the next gate's
window. Issue #201.

The three mechanisms now know about each other, through one marker:

- **Producers.** `gate-check`, `gate-commit`, `gate-push` and
  `work verify -- <cmd>` hold a marker (`lmer_cli.gate_lock`) for their whole
  run — one `<pid>.json` under `LMER_GATE_LOCK_DIR` (default
  `/tmp/lmer-gate-inflight`), removed in a `finally`. Liveness is the OS's
  answer, not a promise about how long a gate "should" take: a marker counts
  only while its pid is alive, dead ones are pruned on read, and the six-hour
  age cap is a pid-reuse backstop, never a gate timeout. Writing a marker is
  fail-soft — no lock problem can change a gate's exit code, exactly like
  receipt emission.
- **Commits defer.** `work_repo.git_ops.commit_work_path` — the single choke
  point behind `work commit` *and* the implicit pushes in `work state set` /
  `goal` / `artifact` / `ledger set` — skips add/commit/push while a marker is
  live, leaves every file on disk, and returns 0. Two exemptions: the release
  CAS claim path (arbitration, not durability — it never routes through this
  function) and `work session-end` (`allow_during_gate=True`: the container is
  going away, so the gate whose window a deferral would protect is being torn
  down with it, and there is no later commit to flush into).
- **The Stop hook stands down.** `work resume --json` reports `gate_in_flight`
  (derived server-side, like `run_dir_url`), and `hooks/run_state_guard.py`
  suppresses **every** trigger while it is true — not just the push nudge:
  triggers 1 and 3 mandate `work goal` / `work state set` / `work ledger set`,
  which write the work repo too. No sentinel is burned and no counter consumed,
  so the nudges return in full the moment the gate ends. The suppression is
  bounded by the gate's life, which is what keeps "a session cannot stop with
  unpushed artifacts" true: deferred work leaves the run dir dirty, so trigger 2
  fires as soon as the window closes.

A deferral is recorded as a **`commit_deferred`** event so an exit-zero cannot
quietly claim the work is safe:

```json
{"ts": "…", "session": "…", "type": "commit_deferred",
 "note": "work-repo commit deferred while gate-check was in flight",
 "data": {"gate": "gate-check", "pid": 1952,
          "paths": ["gitlab.example.com/group/project/runs/develop-issue-123"]}}
```

It is parked outside the work repo (beside the markers) until a commit is
allowed through, then flushed into `events.jsonl` by that commit — writing it
into the run dir at deferral time would dirty a possibly-clean tracked file and
be reported by the running suite's leak guard as an appearance, which is the
same failure in a new costume.

Bare writes take the other path: **commits defer around a gate, bare writes
attribute themselves** (issue #233). `work log` and `work event` write the run
dir without committing, so instead of deferring, a write-shaped `work`
invocation running inside a gate window journals its intent beside the markers
— the paths it may write, plus a per-marker verdict on whether the marker's
holder (pytest, for a suite) is among the writer's process ancestors, read
from `/proc` at write time. The suite's leak guard consumes that journal at
teardown and partitions the drift: entries under the write scope a
**non-ancestor** invocation declared (the session that launched the suite,
doing its job) are excused and reported informationally; a journaled write
that **descends from the suite** fails the run naming the leaking command (a
test reached the real `work` CLI — issue #93); anything unattributed and any
HEAD move keeps the hard failure. **Removals are held to a stricter test**:
whether a path was removed is read from the porcelain status code (`D` in
either column), not from which side of the snapshot diff the line landed on —
deleting a tracked file *adds* a ` D path` line — and a removal is excused
only when the same tail reappears under a *different* declared prefix, which
is what the session's own run-dir rename (`work goals freeze`, `work name`)
produces and what a test's `rmtree` never does. The excuse is otherwise
prefix-scoped, not per-file: a write from any source that lands inside an
excused prefix rides that excuse for the suite's window — the granularity
trade-off is documented at `cli._write_intent_rel_paths`, and outside excused
prefixes hand edits still fail the run. The suite also holds a `pytest-suite` gate
marker of its own (written by the conftest lock-dir fixture into the
operational dir), so commit deferral covers bare `pytest` runs, not only
gate-command runs.

`LMER_GATE_INFLIGHT_GUARD=0` restores the pre-#201 behavior for every consumer
at once (markers are still written); see [LMER-CLI.md](./LMER-CLI.md).

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
- `goals` — cross-references into goals.md, used by the goal-coverage
  warning (active goals with zero covering tasks; see the goals.md
  section below).

The lint stops deliberately at plan time: wave-based *execution*
(dispatch, worktrees) and runtime write-scope enforcement stay out of
scope — run-state is a state layer (§8).

### `goals.md` — frozen goal-sets

The run's goal contract (issue #91): what the run agreed to achieve,
written during spec/brainstorm, **frozen at spec approval**, amended only
explicitly, and assessed goal-by-goal at finish — so "did the run do what
we agreed" is answered against a fixed list, not whatever the retro
happens to remember. The format is parse-compatible with the masterplan
fork's goals.md (its `lib/goals.mjs` provided the reference semantics and
test vectors; the kernel is a pure Python port in `src/work_repo/goals.py`
— the work CLI never shells out to node):

```markdown
topic: one-line seed for the run

## G1: Kernel lands
signal: test
evidence: tests/test_kernel.py

## G2: Docs land
signal: docs
evidence: docs/FEATURE.md

body prose per goal is allowed and ignored by the parser
```

- `signal:` — the *class* of proof, one of `test | command | artifact |
  docs`.
- `evidence:` — where the proof lives (free text naming the source).
  Unlike masterplan's parser, evidence is captured and is part of the
  goal's canonical identity: changing what proves a goal is a real
  amendment.
- Drafts may omit `signal:`/`evidence:` (`work goals check` warns);
  **freezing refuses** a goal that doesn't name both.
- A removed goal becomes a **tombstone** (`tombstone_reason:` +
  `tombstone_at:` replacing its signal contract); ids are never renumbered
  — a new goal always takes the next number.

The lifecycle events carry the canonical goal list plus a canonical
`goals_hash` (`sha256:` over the parsed, whitespace-insensitive shape), so
the last agreed set is always recoverable from `events.jsonl` alone:

- `goals_frozen` — recorded by `work goals freeze` at spec approval
  (approval context in the note). Freezing also invokes the run's
  pre-execution freeze seam (§1: `frozen` stamp + the one-shot
  name-bearing dir rename) when the run is *named* and not already frozen
  — both mark the same gate. An unnamed run's seam is left to the phase
  gate: the `frozen` stamp would forfeit the single rename, and spec
  approval can precede the run being named.
- `goal_amended` — every post-freeze change, with the old/new hash and a
  per-goal diff (`added`/`modified`/`tombstoned`/`untombstoned`). A
  goals.md edit *without* an amend is silent divergence, and assess
  reports it.
- `goals_assessed` — the per-goal verdict map (`met | partial | missed |
  waived`, plus evidence) recorded at finish, before
  `work state set --status=complete`. Evidence is classified against the
  run's receipts (§2) and registered artifacts — free-prose evidence is
  allowed but marked `(prose)`.

Plan coverage: `work plan check` warns on active goals no task covers
(§2, the `goals` field; plan.md mention as fallback). All of it is
nudge-don't-block in v1 — fragment rules, not guard hooks; waivers,
user-approval receipts, and anti-fabrication validators for assessments
stay deferred (§8).

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
`status: complete` or `archived` (an archived run still resolves until the
external cleaner moves it under `runs/archive/`), the session does not
re-seed — and it never silently resumes either (#96). `decide()` marks the decision with
`completed_run: true` (carried into `work resume --json` for hooks), and
the resume brief appends an explicit direction contract: with a seed
(`LMER_START_PROMPT`, threaded into `format_brief` by
`work session-start`/`work resume`) the session records it as the goal
(`work goal "<seed>"`), reopens with `work state set --status=in-progress
--stop-reason=none`, and proceeds on the seed; without one it asks the
user (new target vs continue this run), recording
`work state set --stop-reason=question --question "<text>"` and ending
the session if the question goes unanswered — never proceeding on a
guess. The run-state
taskdef fragment teaches the same contract. Runs the external cleaner has
moved to `runs/archive/` no longer occupy the slug, so a genuinely new
engagement seeds a fresh run.

## 4. CLI verbs

| Verb | Behavior |
|---|---|
| `work state` | Print current run state (read-only). |
| `work state set --phase=… --stop-reason=… --status=… [--critical-error=<json>] [--question "<text>"]` | The only mutation path. Constrained fields; atomic write; bumps `updated`; appends a corresponding event. Re-submitting the same `--phase` value with no other flags short-circuits with `State unchanged` and does not write; the other flags always write (see below). |
| `work answer "<text>"` | Record the human's answer to the run's recorded open question (§2): appends `question_answered` (`{question, answer}`, both secret-redacted), clears `open_question` and `stop_reason` (`status` untouched — a completed run stays complete), pushes the run dir. Errors (exit 1) without run context or when no open question is recorded; never auto-seeds. The same logic is applied automatically by `work session-start` when `LMER_ANSWER` is set (the host CLI's `--answer` flag). |
| `work event <type> [--note "…"] [--data <json>]` | Append one event line. Auto-seeds the run if it doesn't exist yet. |
| `work verify <name> -- <command …>` | Run the command (stderr merged into stdout), stream its output through, mirror its exit code, and append a `verify` receipt event (§2): `{name, argv, exit_code, duration_s, summary_line, output_tail_sha256}`. The `--` separator is required (a forgotten name must not silently become the command). A signal-killed command mirrors the shell convention `128+N` (receipt and observed exit code agree); a command that cannot start exits 127. Mutating-verb rules: without run context this errors *before* running the command. A receipt-append failure after the command ran is reported loudly on stderr but the exit code still mirrors the command. |
| `work resume [--json]` | Pure decide function: reads state + events (+ ledger), prints a resume brief — slug, status, phase, stop_reason, goal, an `Estimate: ~3 sessions / 4h — used: 2 sessions` line when an estimate is recorded (§2), a one-line ledger summary when a ledger exists (`Ledger: 4/7 done, in-flight: T3a, last commit 4a1f9c2`), last ~5 events, artifacts, owner-claim warning if applicable. For release runs the brief additionally carries a `Release:` block — derived leg + next step, recorded SHAs/tag, receipt set, and the claim verdict (§7) — so a relaunched session resumes at exactly one next action; non-release briefs are byte-identical to before. `--json` for hooks/machines and carries the full ledger. Never exits non-zero — an unreadable or missing run (or ledger) degrades to a message, not a failure. |
| `work ledger` | Print the execution ledger table (read-only): the summary line plus one row per task. With no ledger prints `No ledger`, exit 0. |
| `work ledger set <task-id> --status <s> [--title …] [--commit <sha>] [--receipt <name>] [--note …]` | The only mutation path for `ledger.yaml` (§2): upserts the row (omitted fields preserved), stamps `updated`, appends a `task` event, pushes the run dir. `--status` is one of `pending\|in-progress\|done\|deferred\|dropped`; `done` with no `--commit` warns loudly but succeeds. |
| `work plan check` | Read-only lint of the run's `plan.index.json` (§2). Errors (exit 1): invalid/newer schema, structural problems (non-string/duplicate ids, missing description), unknown `deps` ids, dependency cycles, file overlap between dependency-independent tasks not declared in `shared_files`, missing/invalid `session_scope`, `multi` without `scope_rationale`. Warnings (exit 0): plan.md checkbox count drifting from the index task count, empty `verify_commands`, `goals` refs that don't parse from goals.md (`## G<n>:` headings; skipped when goals.md is absent), active goals with no covering task (a `goals` ref, or a plan.md mention as fallback), stale/malformed `shared_files` entries. Findings print to stdout so the report can be pasted into the plan-approval request. No run context or no `plan.index.json` prints a message and exits 0 (chat/review taskdefs have no index); writes nothing — no event, no push. |
| `work goals` / `work goals check` | Read-only, exit-0-friendly goal views (§2 goals.md). Bare `work goals` prints the status: active/tombstoned counts, the current hash, and whether goals.md matches the last frozen/amended set (or has diverged). `work goals check` is the draft lint: structural problems (no active goal, duplicate/malformed ids, empty statements, incomplete tombstones) error (exit 1); a missing `signal:` class or `evidence:` source warns — those become errors at freeze. No run context / no goals.md prints a message and exits 0. |
| `work goals freeze [--note …]` | The spec-approval gate: strict-validates goals.md (the check warnings become errors), records `goals_frozen` with the canonical goal list + `goals_hash` (the note carries the approval context), registers goals.md in `state.artifacts`, invokes the §1 pre-execution freeze seam when the run is named and not already frozen (an unnamed run's seam is left to the phase gate — the `frozen` stamp would forfeit the one-shot rename), and pushes. Re-freezing errors — changes go through amend. |
| `work goals amend [--note …]` | Explicit post-freeze change: validates the edited goals.md against the last agreed set (every old id survives — removals tombstone; new ids never reuse numbering), records `goal_amended` with old/new hash and the per-goal diff, pushes. An unchanged hash is a no-op success; not-yet-frozen errors. |
| `work goals assess [--verdict 'G<N>=<verdict>:<evidence>' …] [--note …]` | The finish gate (nudge-don't-block). Bare: prints the per-goal verdict skeleton for retro.md plus a divergence report (goals.md hash vs the last frozen/amended hash) and the receipt names available to cite — read-only and never a failure once a run context exists (missing/draft-grade goals.md degrades to a note). With repeatable `--verdict` flags (verdict one of `met\|partial\|missed\|waived`): requires a complete map over every active goal, classifies each evidence string against recorded receipts and registered artifacts (free prose allowed but marked), records `goals_assessed` (divergence flagged, never blocking), prints the completed table, pushes. Run before `work state set --status=complete`. |
| `work name <kebab-case>` | Set the run's name (a label — the directory slug never changes). Normalizes to kebab-case (lowercase; spaces/underscores → `-`; strip other characters; collapse/trim `-`), printing the normalized form when it differs; errors if nothing survives. Names are **unique per project** — a name held by another run is rejected with an error citing the conflicting slug. Renaming is allowed anytime (same uniqueness check; appends another `run_named` event — history lives in the event log); re-setting the run's own current name is an idempotent no-op success. Bare `work name` prints the current name (or "No name set"), read-only, exit 0. |
| `work artifact <name> --file <path>` | Copy the file into the run dir (secret-redacted), register it in `state.artifacts` (through the single writer, keyed by the artifact's filename stem), append `artifact_written`. `<name>` must be a plain filename (no path components, no leading dot). |
| `work seed <taskdef> <target> [--goal …] [--name …]` | Out-of-session run creation: derives the slug from its args and creates a run for it through the same create-tmp → write-state → rename lifecycle, recording CLI-shaped events (`run_seeded`, then `goal_set` / `run_named` as given). Does **not** claim `owner` (seeding is not owning) and does **not** push — batch with `work commit`. An existing run for the slug (or a name conflict) is an error. |
| `work release claim` / `claim-status` / `unclaim` / `record …` / `status` / `abort` | The release-run verbs (§7): the single-flight release claim (claim-by-push CAS — never the rebase-retry push path), the write-once release record (`release.yaml`), the derived-leg status view, and the terminal abort. Verb-by-verb tables, exit codes, and semantics live in §7. |
| `work session-start` | Hook-facing. Seed the run if absent (via the tmp-then-rename lifecycle), apply a pushed `LMER_ANSWER` when the run is stopped on a recorded question (§2, fail-soft), decide the resume brief *before* claiming, append `session_start`, claim `owner`, print the brief (leading with the answered question+answer pair when one was just applied). Always exits 0. |
| `work session-end` | Hook-facing. Append `session_end`, clear `owner` if it's ours, push the run-state path via `work commit`. Always exits 0. |

`work state set` field choices: `--stop-reason` is one of
`question|yield|complete|critical_error|none` (`none` clears it back to
`null`); `--status` is one of `in-progress|complete|archived`;
`--critical-error` takes a JSON object (`{"summary": ..., "detail": ...}`)
and is required when `--stop-reason=critical_error` is given; `--question`
stores `open_question` (§2) and is only valid together with
`--stop-reason=question` — or on its own while the recorded stop reason is
already `question` — any other combination errors (exit 1). Setting
`--stop-reason` at all clears `open_question` — even a fresh `question`
stop starts blank unless `--question` re-records the text (§2).
`--status=complete` additionally stamps the completion
actuals (§2) into the `state_changed` event's data.

**No-op behavior:** only `--phase` is compared against the current value —
re-submitting the same `--phase` with no other flags short-circuits with
`State unchanged` and writes nothing. Passing `--stop-reason`, `--status`,
or `--critical-error` always writes state and appends a `state_changed`
event, even when the value is identical to what's already recorded — and
`--status=complete` triggers a work-repo push each time, so an idempotent
retry of the close-out command re-pushes (harmless, but not silent).

**Phase-transition advisory (issue #100):** a phase *change* is a step
boundary, and every step must end with a pushed, linkable deliverable. The
transition's own durability push normally guarantees that; when the run dir
is still dirty or ahead of its upstream afterwards (the push failed),
`work state set --phase=…` prints a loud ⚠️ advisory naming the phase that
just ended and pointing at `work commit`, including the run dir's web URL
when derivable. Advisory only — the exit code is unchanged (fail-soft). The
Stop-hook guard's push-before-stop nudge cites the same web URL when
`work resume --json` exposes it (the additive `run_dir_url` field).

`work goal` (existing verb): when a run context exists
(`LMER_REPO_HOST`/`LMER_REPO_PROJECT` set), setting a goal additionally
records it into `state.yaml` and appends a `goal_set` event; the legacy
`/tmp` goal-file behavior is preserved unconditionally either way. Optional
`--estimate-sessions N` / `--estimate-time "<str>"` flags (issue #99, §2)
store a `{sessions, time}` estimate in `state.estimate` and echo it in the
event's data — a goal without them behaves byte-for-byte as before.

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
  `work verify`, `work artifact`, `work ledger set`, `work answer`) print
  an error to stderr and exit 1 — scripts chaining them under `set -e`
  should expect that. Session behavior
  without a repo is unchanged from before this feature existed.

## 6. External cleaner contract

The cleaner is an external process (cron or host command, user-owned).
Contract it can rely on: single-writer state, append-only events, `status`
enum, `updated` timestamps, `owner` claims. Actions:

- Move `runs/<slug>/` (or `runs/<slug>--<name>/`) → `runs/archive/<slug>/`
  (or set `status: archived`) for runs that are `complete` or stale past a
  threshold. The archive subtree stays inside `runs/` — that is the tree
  the resolver's skip logic and the `work name` reservation guard — never
  a `runs/` sibling.
- Sweep `runs/.new-*` orphans: a crash between run-dir create and rename
  leaves one behind; any `.new-*` dir stale past an mtime threshold is
  safe to delete (the resolver never matches dot-dirs).
- Optionally migrate legacy `{task_type}/{target}/` log/report dirs into
  the matching run dir — the CLI only ever falls back to them on read and
  never moves them itself.

## 7. Single-flight release claim (`work release`)

The release flow (release-flow spec §2/§7) requires **single-flight**: at
most one active release run per project, enforced by the run-state layer —
a second launch refuses with a pointer to the active run. This section
records how that claim is made *atomic*, because the obvious mechanism is
broken by the repo topology.

**The load-bearing constraint:** the work repo is a **per-container git
clone** (`LMER_WORK_REPO` → `ensure_clone`), and every run-state push goes
through `git_ops._push_with_rebase_retries` — which reacts to a rejected
push by rebasing and pushing again. Writing `owner`/`claimed_at` into
`state.yaml` locally therefore excludes nothing: two simultaneous launches
each claim in their own clone, and the rebase-retry path integrates the
loser's claim commit on top of the winner's and pushes it. Last writer
wins; both proceed. Any claim written through the normal push path is
check-then-act, not a lock.

### Decision: claim-by-push compare-and-swap

The claim verbs bypass the rebase-retry path and use the git remote itself
as the CAS register:

1. **Fetch** and evaluate the claim against the *remote* head only — never
   against the local clone's possibly-stale view.
2. A **live foreign claim** at that head → lost: exit non-zero, print the
   active-run pointer (below). Nothing is written.
3. Otherwise write the claim through the single writer, commit, and issue
   **one plain `git push` — no `pull --rebase`, no rebase-retry.**
4. Push accepted (fast-forward) → the claim landed atomically → won.
5. **Non-fast-forward push rejection** → the remote advanced between fetch
   and push. That is *not* automatically a lost race (the work repo has
   many unrelated writers), so go to 1 and re-evaluate — bounded attempts
   (3, `RELEASE_CLAIM_ATTEMPTS`). Exhausted, or remote unreachable →
   **fail closed**: exit non-zero as "could not establish claim". A
   release never proceeds unlocked.

The invariant that makes this a compare-and-swap: **a claim commit is
never rebased onto a remote head that has not been re-checked for a
foreign claim.** The race window between fetch and push is exactly what
the server's non-fast-forward rejection closes.

**As shipped**, the protocol is split across two layers. The git leg is
`git_ops.claim_push_once`: fetch, then ONE plain `git push`, returning
`won` (fast-forward — the claim landed atomically), `lost-race`
(non-fast-forward rejection — re-fetch and re-evaluate before any retry),
or `error` (transport/auth/missing remote — callers fail closed; the
up-front fetch makes a dead remote read as `error`, never as a lost race).
The state leg is `run_state.claim_run`/`unclaim_run` (the local
single-writer mutation, evaluated against the remote head the CLI just
fetched). The CLI loop (`work release claim`/`unclaim`/`abort`) composes
them: sync the remote head — a **local snapshot commit** of any pending
run-dir changes first (the session hooks leave `state.yaml`/`events.jsonl`
dirty by design, and a dirty tree makes `pull --rebase` refuse exactly on
the re-entry path these verbs serve; the snapshot is an ordinary run-state
commit, so rebasing it is the normal integration path), then
`pull --rebase` (safe by construction: it runs only while no claim commit
exists locally) — write + commit the claim locally, `claim_push_once`; on
**any non-won outcome** the local claim commit is **dropped**
(`reset --hard` to the pre-claim head, made safe by the snapshot: the
dropped commit carries only what the verb itself wrote). On `lost-race`
the commit is rebuilt from the re-fetched head each attempt, upholding the
invariant; dropping on `error` too matters because a leftover claim commit
would later be silently rebase-pushed onto an un-re-checked head by the
next ordinary verb. Same-file racers on the state.yaml both CAS through
this path, so the git server arbitrates.

### Lock object and scope

- **Scope: project + release taskdef** — the claim keys on the release
  run's slug (same-taskdef-same-target ⇒ same slug, §3), not the whole
  project. Every other run in the project is untouched.
- The lock object is a dedicated **`claim` block in the release run's
  `state.yaml`** — `{session_id, claimed_at}` — written *only* by the
  `work release claim`/`unclaim` CAS path. It is deliberately **not** the
  per-session `owner` field (cleared at every session end, warn-only
  semantics) and **not** bare run existence (`work session-start`
  auto-seeds runs fail-soft through the rebase-retry path, with no CAS
  discipline). The release-flow spec's "an in-progress release run *is*
  the lock" holds in effect: the claim is valid only while the run is
  `status: in-progress`, so completing or aborting the run releases the
  lock with no separate CAS write — a claim block on a `complete`/
  `archived` run reads as unclaimed.

### Stale-claim policy — enforced, not warn-only

`decide()`'s `STALE_CLAIM_MINUTES` semantics are untouched: the
per-session `owner` warning stays advisory ("coordinate before writing"),
aimed at humans. The release claim is different in kind — the party that
must be refused is an unattended second launch, and the party that must
eventually get through is the unattended *scheduled relaunch* (release-flow
spec §3: watch is best-effort, resume is the contract). So:

- A **live** foreign claim (age < `RELEASE_CLAIM_STALE_MINUTES`, its own
  constant — default 120 to match `STALE_CLAIM_MINUTES`, but enforced
  rather than advisory) → `work release claim` refuses. Hard, exit
  non-zero — never a warning.
- The **holder keeps the claim live by re-claiming**: `work release claim`
  from the holding session is an idempotent refresh of `claimed_at`. A
  session idling on a blocking watch cannot refresh mid-block — that is
  fine BECAUSE the release taskdef requires a re-claim on wake, before any
  mutating action: whichever woken session wins the CAS drives on; the
  other ends (single-flight holds at the action point, not across the
  idle).
- A **cleanly-ended holder releases its claim at session end**
  (`work session-end` runs the same CAS discipline as `work release
  unclaim`, best-effort/fail-soft) — the stale threshold exists for
  CRASHED sessions, not clean exits, so a relaunch after a clean exit
  claims immediately instead of waiting out the threshold or doing a
  takeover.
- A **stale claim** (age past the threshold — the holder session crashed
  or was reaped without unclaiming) is **taken over automatically** by the
  next `work release claim`, loudly: a claim event records the displaced
  session and the claim's age. Automatic (not flag-gated) because the
  next claimant is normally the scheduled relaunch with no human attached;
  safe because takeover resumes the *same* run (same slug, resume
  semantics re-derive the leg from run state) — it can never start a
  second parallel release.

A never-expiring claim was rejected: a crashed watcher would strand the
release until a human intervened, breaking the "merged release MR never
sits untagged longer than one schedule interval" contract.

The pure verdict function is `run_state.claim_status`, returning one of
`CLAIM_UNCLAIMED` (no block — or the run is no longer `in-progress`: the
lock-releases-with-the-run rule above), `CLAIM_OURS` (held by this
session; re-claiming is the idempotent refresh), `CLAIM_FOREIGN_LIVE`
(under `RELEASE_CLAIM_STALE_MINUTES` — hard-refusal territory), or
`CLAIM_FOREIGN_STALE` (past the threshold, or `claimed_at` unparseable —
takeover territory). `decide()` carries the verdict as an additive `claim`
key in `work resume --json` and the brief renders it in the release block,
so hooks never re-derive it.

### The loser's pointer

A refused `work release claim` prints (and emits under `--json`) enough to
find the active release without archaeology: the run **slug**, the **run
dir** path, the run dir's **web URL** (via `git_ops.web_url_for`), the
claim holder's **session id** and **claimed_at** (with age), and the run's
current **status/phase**.

### What the lock does NOT protect against

Stated plainly — these are out of scope for this lock:

- **Out-of-band manual pushes to the work repo.** The CAS disciplines the
  claim verbs only; a human hand-editing `state.yaml` and pushing rewrites
  the claim like any other file. Single-writer remains a convention the
  lock strengthens, not a guarantee it creates.
- **The release targets themselves.** The lock serializes release *runs*;
  it does nothing about a manual tag or branch push to GitHub/GitLab that
  bypasses the taskdef entirely (the release-flow spec's idempotency
  checks are the layer that notices).
- **A broken or unreachable remote.** No push means no claim — the
  failure mode is fail-closed refusal, not unlocked progress. A
  force-push/history rewrite of the work repo branch can likewise destroy
  or resurrect a claim; neither is survivable by design.
- **Clock skew** affects staleness judgments only (bounded by the
  threshold **in both directions** — `claim_status` treats a claim as live
  only while `abs(age) < RELEASE_CLAIM_STALE_MINUTES`, so a future-dated
  `claimed_at` from a fast holder clock cannot pin the lock past the
  threshold either), never the CAS itself — atomicity comes from the git
  server, not from timestamps.

### Rejected alternatives

- **Claim via the normal single-writer + `_push_with_rebase_retries`
  path** — the load-bearing constraint above: rebase-retry converts the
  losing claim into a merge, last writer wins, both launches proceed.
- **(b) Create-only lock ref** (e.g. `refs/locks/<project>-release`,
  `--force-with-lease`-style CAS) — atomicity is equivalent, but the lock
  leaves the run-state contract: invisible to normal clones and the
  resolver, outside the `state.yaml` single-writer and the `events.jsonl`
  audit trail, environment-dependent (GitLab restricts non-standard ref
  namespaces), and needing bespoke cleanup tooling. Rejected for opacity,
  not correctness.
- **(c) `O_EXCL` lock file on a shared mount** — rejected outright:
  `/work` is a per-container clone, not a shared mount. Two simultaneous
  launches (possibly on different hosts) share no filesystem, so `O_EXCL`
  excludes nothing. Do not revisit unless the work repo stops being
  per-container.

### Frozen claim verb names (R5)

The claim verb names and flag surface below are **frozen verbatim** —
wave-1 consumers (taskdef bodies) reference them by these exact names. All
release verbs live under `work release <subverb>` (nested subparsers,
matching `work plan check` / `work goals freeze`).

| Verb | Flags | Behavior |
|---|---|---|
| `work release claim` | `[--json]` | Take the single-flight claim (CAS). Exit 0 = claim held (fresh win, holder refresh, or stale takeover). Exit non-zero = lost — prints the active-run pointer — or claim could not be established (push attempts exhausted / remote unreachable): fail closed, never proceed unlocked. |
| `work release claim-status` | `[--json]` | Read-only: holder session, `claimed_at`, age, live/stale verdict — or `unclaimed`. Always exits 0 (read-only convention, like `work ledger`). |
| `work release unclaim` | `[--force]` | Release our claim (CAS-pushed). A foreign claim refuses (exit 1) unless `--force` (human runbook: abort path, stranded-claim cleanup). No claim recorded is an idempotent no-op success. |

#### Release-record verbs

Frozen by the release-record kernel design and shipped: the kernel is
`src/work_repo/release_run.py`, the CLI wiring lives in
`src/work_repo/cli.py`. These names are the contract taskdef bodies cite:

| Verb | Purpose | Flags |
|---|---|---|
| `work release record version <X.Y.Z>` | Record leg 1's release version (write-once; pyproject version, no `v` prefix — the tag adds it; a leading `v` is refused at record time) | — |
| `work release record bump-sha <sha>` | Record the bump-MR merge SHA (leg 1 complete; full 40-hex; write-once; requires the version to be recorded first) | — |
| `work release record merge-sha <sha> --version <observed-version>` | Record the release-MR merge SHA every leg-2 step keys on; hard stop when the observed version at that SHA disagrees with leg 1's recorded version (checked even on idempotent re-record) | `--version` (required) |
| `work release record tag <vX.Y.Z> --sha <sha>` | Signed-tag creation receipt; hard stops: SHA must equal the recorded merge SHA and name must be exactly `v<version>` — never re-point, never re-sign | `--sha` (required) |
| `work release record receipt <name> [--url <url>] [--note "…"]` | Push/upload receipts; `<name>` ∈ `github-main-push`, `github-tag-push`, `actions-run`, `pypi`, `gitlab-tag-push`; `--url` required for `actions-run`/`pypi` (records which run actually uploaded); receipts are re-recordable, history in events.jsonl; requires the tag to be recorded first (nothing can have been pushed yet otherwise) | `--url`, `--note` |
| `work release status [--json]` | Read-only: recorded fields + derived leg + single next step (the resume decision for a scheduled relaunch). No run context / no record yet are normal (exit 0; the JSON form still derives `leg1-bump` from an empty record); an internally inconsistent record (hand-edited tag vs merge SHA) exits 1 with the kernel's hard-stop message | `--json` |
| `work release abort [--reason "…"] [--force]` | Terminal abort — the human declined the release MR (release-flow spec §7). Marks release.yaml aborted, then flips state.yaml in one atomic write, CAS-pushed; a LIVE foreign claim refuses (exit 1) unless `--force`, same as `unclaim`; see abort semantics below | `--reason`, `--force` |

Record verbs are mutating (no run context errors, exit 1; the run
auto-seeds), and every actual write pushes the run dir immediately — the
record is the crash-recovery contract, so durability is per-write, not
per-session. A contradicted write-once field or a kernel hard stop exits 1
with the kernel's message verbatim and writes nothing.

#### `release.yaml` — the release record

A dedicated single-writer sibling file in the release run's dir,
deliberately NOT additive keys in state.yaml: state.yaml is the universal
contract every taskdef writes, and its corrupt-file recovery reseeds IN
PLACE — which would silently drop an embedded release record mid-release.
The sibling file is crash-isolated, versions independently
(`RELEASE_SCHEMA_VERSION` = schema 1), and follows ledger.yaml's
precedent. Same safety contract exactly: only the `release_run` recorders
write it (atomic tmp+rename), a corrupt file is backed up as
`release.yaml.bad-<stamp>` before erroring, and a **newer** schema number
is a read-only refusal (older readers never clobber a newer writer's
file).

```yaml
schema: 1
version: "0.5.0"                  # write-once; pyproject version, no v prefix
bump_mr_merge_sha: <40-hex>       # write-once; leg 1 complete
release_mr_merge_sha: <40-hex>    # write-once; every leg-2 step keys on it
tag:                              # write-once; {name, sha, created}
  name: v0.5.0
  sha: <40-hex>
receipts:                         # re-recordable; one row per RECEIPT_NAMES entry
  github-main-push: {recorded: <ts>}
  actions-run: {recorded: <ts>, url: <run URL>}
aborted: {at: <ts>, reason: "…"}  # terminal; only via work release abort
updated: <ts>
```

Identity fields (version, both SHAs, the tag) are **write-once**:
re-recording the identical value is an idempotent no-op (re-entered legs
converge), a different value is a hard stop — recorded release identity
never silently moves. Receipts MAY be re-recorded (spec §7's re-run
artifact drift: a re-dispatched Actions run must be able to replace the
URL with the run that actually uploaded); prior values stay in the
`release` audit events. Free text (`--note`, URLs) is secret-redacted
before landing in the work repo.

#### Derived leg ladder (`derive_leg` / `next_step`)

Pure derivation from the record alone — no fs, no env, no remotes (spec
§3: relaunching re-derives the leg from run state and continues). The
ladder walks in spec order and stops at the FIRST missing record, so
out-of-order receipts still converge. Frozen step names, in order:

`leg1-bump` → `leg1-record-bump-merge` → `gate-await-release-merge` →
`leg2-create-tag` → `leg2-push-github-main` → `leg2-push-github-tag` →
`leg2-poll-actions` → `leg2-record-pypi` → `leg2-push-gitlab-tag` →
`complete` (legs: `leg1`, `gate`, `leg2`, `complete`).

Plus the terminal `aborted` leg — deliberately NOT a ladder entry (an
aborted run is not a resume row): `derive_leg` reports it with
`next_step: None`, "nothing to advance". An internally inconsistent
record (tag SHA vs merge SHA, tag name vs version — only reachable by
hand-editing, the recorders refuse to write it) is a hard stop: never
converge over it. `work release status` and the resume brief's release
block both render this derivation.

#### Abort semantics (`work release abort`)

Spec §7's abandoned release: the bump merged, the human declined the
release MR. `work release abort [--reason]` composes two terminal writes
plus one CAS push, in the crash-safe order:

1. `release_run.record_abort` marks release.yaml terminal first — the
   `aborted: {at, reason}` block and NOTHING else. Every recorded field
   survives, above all the bump-MR merge SHA: the next release run's ctl
   dry-run needs it to see the version already bumped on `prep-release`
   and skip the bump. The bump commit stays; aborting never reverts
   anything.
2. `run_state.abort_run` lands all three state facts in ONE atomic write:
   `status` → `complete`, `stop_reason` → `aborted`, `claim` → cleared —
   so no observer ever sees an aborted run still holding the lock — and
   appends a `run_aborted` event (reason redacted; any displaced claim
   holder named, with `forced` recording which knob cleared it).

A **live foreign** claim REFUSES the abort (exit 1) unless `--force`, and
the check runs BEFORE step 1 so a refused abort leaves release.yaml
untouched. "The human declined" and "another session is mid-release right
now" are different facts, and only the caller knows which holds: without
the guard a session correctly refused at `work release claim` could follow
the decline path and mark another session's in-flight release terminal,
freeing its lock remotely so a third session drives the same release. A
**stale** foreign claim still clears without `--force` — staleness is the
takeover case, not the refusal case.

Dying between the two leaves an in-progress run whose record already says
aborted; the re-run converges (record_abort no-ops, abort_run completes)
— never a lock-free run whose record still asks for a next leg. Aborting
an already-aborted run is an idempotent no-op success; a run that
finished any other way refuses (exit 1) *before* either write — aborting
it would falsify its recorded outcome. A held claim — even a foreign one
— is cleared, not refused: the decline is the human's explicit terminal
decision.

**Why `status: complete` + `stop_reason: aborted`, not a fourth status:**
`status` is the closed enum external consumers switch on without knowing
releases exist. The §6 external cleaner archives runs that are `complete`
— an aborted run is archived by the *existing* rule, unchanged; a new
`aborted` status would sit outside "complete or stale" until every
deployed cleaner learned the value. `decide()`'s completed-run policy
(issue #96) already refuses to silently resume `complete`/`archived` runs
— exactly the no-resurrection guard an aborted run needs, for free — and
`claim_status` already reads any claim on a non-`in-progress` run as
unclaimed, so the lock releases by the existing rule. `stop_reason` is
the descriptive axis nothing switches on, so `aborted` there is additive
and the schema-stays-1 promise holds.

**An aborted run is TERMINAL and is never re-claimed as itself.** An
exemption would hand out a lock with NO mutual exclusion: `claim_status`
reads every claim on a non-`in-progress` run as unclaimed and `claim_run`
never restores `in-progress`, so two sessions would both be told "claim
taken" and both drive leg 1 — and the CAS push does not save it, since the
loser re-syncs, still reads unclaimed, and wins the retry. The
abandoned-release contract does not need one: `derive_leg` reports
`next_step: None` for an aborted record permanently, and the resume is the
NEXT release run, whose ctl dry-run detects the bump already on
`prep-release` and skips it. Where that next run comes from is the
version-bearing identity below.

#### Release-run identity: the version is in the slug

`derive_slug()` is a pure function of `(taskdef, target)`, so a release run
that kept its derived slug forever meant **a repository could release
exactly once**: the second release resolved to the first one's finished run,
and a finished run is not claimable. Reopening it by hand does not help
either — `release.yaml`'s `version` is write-once.

A release run therefore takes an address of its own as soon as it knows one:

```
release-<repo>                  the seed address — one live release at a time
release-<repo>-v0.6.0           from `work release record version 0.6.0` onward
release-<repo>-v0.6.0-<stamp>   when that address is already taken (below)
release-<repo>-<stamp>          terminal without a version ever recorded
```

**The invariant is that one slug names one run** — not that a directory is
free. The two are different resources and they come apart in both
directions: an unnamed run occupies `runs/<slug>` while leaving the slug
recordable, and a *named* run lives at `runs/<slug>--<name>`, occupying the
slug while `runs/<slug>` stays free. Guarding on the path alone gets both
wrong, so `run_state.slug_available` checks the recorded slug **and** the
dirs that address it.

- **A version can repeat**, so the version-bearing address can already be
  taken. `RELEASE-FLOW.md` §6 leaves a declined release's bump on
  `prep-release`, so the successor's dry-run skips it and the successor
  records the same `X.Y.Z` — while the declined run is still parked on
  `-v<X.Y.Z>` (the decline happens at the release-MR gate, after leg 1 step
  5). `release_run.unique_release_slug` mints the stamped variant in that
  case. Giving up instead would leave the successor on the seed address it
  was supposed to vacate and refuse the release after it forever.
- **`run_state.reslug_run`** performs the move: it renames the dir FIRST,
  then writes `state.slug` and the vacated address, appends a
  `run_reslugged` event and re-points specs-index entries. The order is
  load-bearing — the directory is what must never be double-booked, since
  `seed_run_dir` creates at `runs/<slug>`. A rename that cannot happen
  leaves the slug untouched (loud warning, both together) rather than
  splitting identity from address. A name-bearing dir stays name-bearing.
- **Resolution follows it.** `find_run_dir` still matches `state.slug`
  exactly; when nothing does, `run_dir()` falls back to the newest
  **in-progress** run that RECORDS having vacated the slug being looked up
  (`state.reslugged_from`, written by the re-slug itself —
  `find_successor_run_dir`). Matching a recorded fact rather than a
  re-derived identity is what keeps the fallback to release runs that
  actually moved: a run that never re-slugged has no `reslugged_from`, so
  `derive_slug`'s "no aliasing between the two forms" still holds for
  legacy full-SHA runs. A relaunch, scheduled or manual, therefore lands on
  the in-flight release without any launch parameter carrying the version.
- **Terminal runs never resolve through that fallback**, which is exactly
  what frees the bare address for the next release.
- **`work release claim` rolls a terminal run aside.** Some runs never
  reach `record version` — a release aborted in leg 1, a session that died
  after completing but before its re-slug pushed, a run closed out by hand.
  A claim resolving such a run re-slugs it aside (an available `-v<version>`
  when one was recorded, else the stamp) and seeds the successor at the
  freed address, **in one CAS commit**, then claims that. It computes the
  aside the same way `record version` does, so a taken version-bearing
  address does not dead-end the roll-over. That commit carries **both ends
  of the move** — the address vacated *and* the address moved to. The
  destination is the half nothing else can supply: once the successor owns
  the address the resolver names only it, so a commit staging just the
  vacated path would publish the previous release run's `release.yaml`,
  events and artifacts as a deletion and nothing would add them back. Single-flight is not weakened:
  two racing sessions both roll over locally, one push wins, and the loser
  re-syncs onto the winner's fresh run and refuses against its live claim.
  A roll-over that cannot free the address falls back to the old refusal —
  never a claim on a terminal run.
- `work release abort` does **not** re-slug: it is a terminal write and
  nothing else. Its successor arrives at the next claim.

Known consequence, accepted: a run-dir URL cited for a release before its
version was recorded 404s afterwards. Freeing the address is the point, and
the version-bearing URL — the one every receipt cites — is stable from leg 1
step 5 onward.

## 8. Deferred growth path

Recorded so the design can be worked into deliberately, not accidentally:

- Wave-based execution and planner/decomposer agent briefs on top of the
  `plan.index.json` task DAG. The DAG itself — schema v1 plus the
  `work plan check` lint — shipped with issue #90 (§2); dispatch,
  worktrees, and runtime write-scope enforcement remain deferred.
- Hard anti-fabrication for receipts (hash-chained events, signed receipts)
  and a guard-hook nudge enforcing the claim↔receipt match. The soft v1 —
  structured gate receipts, `work verify`, plan validation contracts —
  shipped with issue #88 (§2).
- Goal waivers, user-approval receipts, and anti-fabrication validators
  for goal assessments — the hardened half of masterplan's goals.mjs,
  deferred until receipts mature. The soft v1 — frozen goal-sets
  (`goals.md` + `work goals check/freeze/amend/assess`), plan-coverage
  warnings, and per-goal finish assessment — shipped with issue #91 (§2).
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
