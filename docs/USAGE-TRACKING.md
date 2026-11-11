# AI usage tracking

lmer records what each session spends with its model provider, and shows the fleet's spend live. The work is #315 (the live view) and #647 (a durable record per run). Both features read harness transcripts through one usage core, `lmer_usage`. As a result, a run's record and the live numbers come from the same counting rules.

- **The durable record** is written by the run itself, from inside its container, with `work usage`. Platform-spawned and standalone `lmer` sessions write it in the same way.
- **The live view** is computed by the platform daemon from the same transcripts. The daemon only reads: it never writes to the work repo.

Nothing here depends on human time tracking (`timetrack*`, `lmer_timesheet`, `timesheet.d/`), and a guard test enforces that.

## What is counted

Every adapter reports the same disjoint buckets per model and agent:

| bucket | meaning | claude | codex | pi |
|---|---|---|---|---|
| `input` | uncached input | `input_tokens` | `input_tokens − cached_input_tokens − cache_write_input_tokens` (floored at 0) | `input` |
| `cache_create` | input written to cache | `cache_creation_input_tokens` | `cache_write_input_tokens` | `cacheWrite` |
| `cache_read` | input served from cache | `cache_read_input_tokens` | `cached_input_tokens` | `cacheRead` |
| `output` | all output, thinking included | `output_tokens` | `output_tokens` | `output` |
| `thinking` | **part of** `output`, never added to it | `output_tokens_details.thinking_tokens` | `reasoning_output_tokens` | `reasoning` |

`requests` counts API responses after deduplication.

There is deliberately no single "total tokens" figure, because cache reads alone usually outweigh everything else. Every count is a non-negative integer below 2^53. A response carrying anything else is dropped rather than counted.

### Deduplication

Deduplication is per session, across all of the session's files. A resume or a fork that copies history into a new file therefore still counts each response once.

- **claude** writes one API response as several records, one per content block, and each record repeats the response's full `usage`. Summing records would count a response once per block. Responses are therefore keyed by `message.id`, then `requestId`, then the record `uuid`, and the last record wins.
  - Records whose model is `<synthetic>`, records with no usage, and records with all-zero usage are skipped.
  - Subagents' `subagents/agent-*.jsonl` files become their own rows, named from the sibling `.meta.json`.
- **codex** records the thread's cumulative `total_token_usage`. Usage is the delta between consecutive snapshots, attributed to the latest `turn_context` model.
  - Each delta is keyed by its pair of cumulative values, so copied history dedupes.
  - A snapshot that does not increase is ignored.
  - A lower snapshot starts a new segment and counts as a baseline. So does the first snapshot of a forked rollout, whose `session_meta` names its parent.
  - **Assumption:** `cache_write_input_tokens` is taken to be part of `input_tokens`. No captured sample has been non-zero yet (`tests/fixtures/usage/codex/README.md`).
- **pi** follows pi's own aggregation (`usage-totals.js`). Every branch of the session tree counts, because tokens spent on an abandoned branch were still spent. The records that count are:
  - assistant messages, under `responseModel` (falling back to `model`);
  - `usage` entries such as `cache_warm`;
  - tool results and `compaction`/`branch_summary` entries that carry usage of their own. These are reported under `pi:tools-and-summaries`.

  pi's own `cost.total` is kept as **harness-reported** cost and is never mixed into lmer's estimate.
- **Any other harness** (a drop-in) is reported as `not_collected` with a reason, never as zero.

### Fan-out children

`spawn-harness` children no longer run statelessly. Each child keeps its own transcript in a reserved folder inside the parent's harness transcript directory: `<session_dir>/.lmer-children/<child-session-id>/`.

On a platform session that directory is the host-side mount, so children's transcripts outlive the container. The platform's transcript readers skip `.lmer-children/` by name, so a child never shows up as the parent: not in the chat view, the latest-message tail, stall detection or the live collector. That rule is host-side, so a container can only hide its own files.

| Harness | How the child is counted |
|---|---|
| pi | Its transcript, written directly into the child's folder (`--session-dir`). Text output is unchanged. |
| claude | Its transcript (`--session-id <child uuid>`). claude has no supported way to move only its transcripts, so it writes into the parent's projects dir. The file is recognised by its id while it runs, and moved into the child's folder after exit. Text output is unchanged. |
| codex | Its JSON output (`--json --output-last-message`), parsed as it streams, which gives back the same bytes text mode printed. Without the last-message file the last `agent_message` is the answer. A last line cut off mid-write (a child killed by a timeout) is dropped, so what it had already reported stands. Error events are written to stderr as text mode prints them, so a failure footer still names the cause. The counts are stored in the child's folder. |
| other harnesses | `not_collected`, never zero. |

The parent's `work usage` counts each child's folder with the same adapters, as rows carrying `agent: <agent name>` and `child_session`. A claude child's own subagents show as `<agent>/<subagent>`. Children are counted only for the session that spawned them.

spawn-harness sets `LMER_USAGE_CHILD`, so a child never writes a record of its own. Children's rows count toward the record's totals and its per-day split: a transcript by its own timestamps, and a codex child, whose rows carry none, on the day its manifest was written (when it finished). The live view does not read children, so for children the record is the authority.

### Wall clock

- `started_at` and `ended_at` are the first and last record timestamps.
- `active_seconds` sums the gaps between consecutive records that are at most `LMER_USAGE_IDLE_SECONDS` (default 300). Time blocked on a question therefore does not count as active.
- The `days` split is per UTC day. A response is attributed to its own timestamp.
- `days` includes the fan-out children, and `child_days` repeats the children's share on its own. The live view reads only the session's own transcripts, so the rollup compares it with `days` less `child_days`. A record without `child_days` has no children to subtract.

## The record: `ai-usage/<session-id>.yaml`

The record lives in the run's directory, as resolved by the `work` CLI, in every tree form:
- legacy `runs/<slug>[--<name>]/`;
- schema 1 `_runs/<slug>/<session-id>/`;
- schema 2 `_runs/<slug>/<uid>/`.

The file is named by session id because a schema-1 record directory can hold several launches joined by lineage. A later launch must not overwrite an earlier one's numbers. `work usage` writes only where the run's `state.yaml` loads.

```yaml
version: 1
key: {host, project, slug, form: legacy|schema1|schema2, schema, session, uid}  # uid on schema 2 only
taskdef: masterplan
target: https://…/issues/315
linked: {kind: issue|mr|null, ref: "group/project#315"}
harness: claude
model: opus                 # requested
effort: high
platform_session: s-… | null
status: running|ended|crashed|unknown
started_at: … / ended_at: … / active_seconds: …
models:                     # one row per harness, model and agent
  - {harness, model, agent, status: collected, requests, input, cache_create, cache_read, output, thinking}
  - {harness, model, agent: reviewer, child_session: …, status: collected, …}
  - {harness: null, model: null, agent: null, status: not_collected, reason: "…", …zeros}
days: {YYYY-MM-DD: {…buckets…}}
child_days: {YYYY-MM-DD: {…buckets…}}   # the children's own share of days
totals: {…buckets…}         # the sum of the collected rows
harness_reported_cost: null | [{currency, total}]
updated_at: …
final: true|false
partial: true|false
```

The record holds tokens only. **Cost is never stored**: it is priced when the record is read (see [Price table](#price-table)).

## Who writes it, and when

- **Checkpoint.** The container's runner (`clone_and_exec.dispatch_runner`) lives as long as the session, for every harness. Every `LMER_USAGE_CHECKPOINT_SECONDS` (default 900) it runs `work usage --checkpoint`.
  - The file is rewritten only when its counters changed. When it is, that file alone is committed and pushed.
  - Counts never go down. Transcripts only grow, so a lower count means some are missing, and the record keeps what it already holds. A session with nothing to count yet writes no file.
  - A tick is skipped while a gate is running, and the next tick catches up.
  - A `work commit` the run makes for its own reasons also refreshes the file, at most once per interval.
- **Final.** `work session-end` writes `final: true` just before its own push, and like that push it ignores a running gate. Session-end runs twice: from the harness hook, and from the runner's backstop after the harness exits.
  - The backstop passes the harness's exit code (`--exit-code`). A non-zero code marks the record `crashed`, and a later zero never turns it back into `ended`.
  - The final pass has its own 30 s budget inside session-end's 180 s, covering the session's transcripts and its children's. If it runs out, or it counts less than the last checkpoint, the last checkpoint is written as final with `partial: true`, its harness-reported cost and uncounted files included.
  - Session-end holds the work-repo checkout lock from loading the run state to its push, so its two runs cannot interleave.
  - A lock keeps a checkpoint that is still running from writing over the final record.
- **Killed container** (`podman kill`, OOM, host reboot): the record keeps its last checkpoint, at most about one interval stale, with `final: false`.
  - The run detail view marks it unfinalised.
  - For a platform session, it also shows how far the live transcripts are ahead of the record. A running session's record is always somewhat behind, so the gap is shown only once the session is no longer live. The live view knows a session by the platform's `s-…` id and the record by `LMER_SESSION_ID`; the record's `platform_session` joins the two.
- **Never blocking.** A usage error is reported on stderr and never changes the exit status of `work commit` or `work session-end`.
- `work usage` without flags prints the session's current usage and writes nothing.

## The live view (platform daemon)

`lmer_platform.usage_live.UsageLive` ticks every `LMER_PLATFORM_USAGE_INTERVAL` seconds (default 30).

- **What it reads.** It reads the transcripts of every registry session and of each tracked run's last session, incrementally. Files are located through the transcript view's own containment, so a planted symlink cannot attribute another run's spend.
- **Its cache.** It keeps a 0600 cache under `~/.lmer/platform/usage/`, holding cursors, the responses seen and the session's active spans, so a restart neither loses nor re-counts usage.
- **Burn.** Burn is bucketed by each response's own timestamp, from the latest record of each response. A re-read therefore never dumps history into "now".
- **Published views.** Each tick builds a session's summary and per-minute burn outside the lock and publishes them under it, unchanged afterwards. Requests only copy them, so `/api/state` neither races a tick nor recounts a session.
- **Ended and forgotten sessions.** A session that is no longer live is read once more and then left alone until it is live again. One that neither the registry nor a tracked run names any more is dropped, cache file included.
- **Coverage.** Standalone sessions have no host-side transcripts, so they are not in the live view. Their records still appear in rollups.

Routes, all read-only and behind the platform's auth:
- `/api/state`: each run row gains `usage`: totals, `burn` (the last 15 minutes), the `models` seen, `status`, and, when priced, `cost` (the run so far) and `burn_cost` (the burn).
- `GET /api/usage?window=1h|24h|7d`: fleet totals per harness and model, the runs burning most now, collected and not-collected counts, and `headroom[]`. With projects on it is scoped like `/api/state`: an assistant, or an operator naming a `workspace_id`, sees only that project's runs and their spend. Headroom names no run and stays the account's.
- `GET /api/runs/usage?host&project&slug[&uid]`: one run per session, with the run-dir record (`updated_at`, `final`, link) beside the live numbers. It is read when the run detail's usage section is shown, not on the poll.
- `GET /api/usage/rollup?project=<host>/<project>&period=day|week|month[&from&to]`: see below. It sums a repository across every project that ran against it, so it is operator-only, like the work repo's validator report.

**In the control UI:**
- the fleet view carries a usage strip, with window totals and the headroom chips;
- each run card shows its burn in the dimmed facts line;
- the run list has an order toggle, **as listed** or **most expensive now**. The second ranks by burn inside each group, so runs that need you still come first; it is offered once some run has a burn reading, and the choice is remembered in the browser;
- the run detail's overview has a **usage** section beside the run files, with each session's record state and links;
- the all-projects view has a per-project, per-period rollup panel.

How each surface reads, so none of them adds work to the fleet poll:
- **Run card.** It reads the `usage` the `/api/state` poll already carries. Burn is a word in the dimmed facts line, not a chip, and unpainted, because spend is not urgency. A run the collector has nothing for shows no burn.
- **Usage strip.** It reads `/api/usage` on its own 30-second timer, slower than the fleet poll, because the collector ticks every 30 seconds and a faster read would only fetch the same numbers. Like `/api/state`, it names no workspace, so the operator sees the whole fleet. It renders nothing when usage is switched off (`enabled: false`), when the daemon predates the route, or before the first answer.
- **Usage section.** It reads `/api/runs/usage` once when the run view opens, like the run files. Its live numbers are as fresh as that read; the card's burn is the reading that moves with the poll. Each session shows two sources: the platform's own `live` read and the `record` the session wrote. Rows come from the live read when there is one, because it keeps counting after a container is killed; otherwise they come from a valid record, and the section names which. A failed read is shown, because "nothing recorded" and "could not read" otherwise look alike.
- **Rollup panel.** It reads `/api/usage/rollup` when it mounts and when the repository changes, never on the poll: the daemon caches the answer per mirror commit. The choices are the repositories (`<host>/<project>`) the fleet's runs are against, not platform workspaces, because records are filed under the repository. It is absent until the first answer, and a refresh that fails after one is noted quietly under the table.

**Ranking "the expensive run".** Runs are ranked by the cost of their burn when the price table covers every model being compared. Otherwise they are ranked by unpriced token weight: `output + cache_create + input`. Cache reads are shown but left out of that weight, because they dominate and would misrank runs.

### Headroom and its projection

Limit readings come from the statusline mirror (#248): claude's 5-hour and 7-day percent with reset time, and codex's account `limits`.

Readings are grouped per (harness, launching operator, window). The operator comes from the host-side registry, never from container-written files. Each entry is labelled an approximation of the account, because one operator can run several accounts. An entry is stale when its reading is more than 30 minutes old, or its reset has passed.

The projection works as follows:
- It takes the median percent across the group's sessions at each sample time, so one session cannot steer the group.
- It fits a least-squares line over the last `LMER_PLATFORM_USAGE_PROJECTION_MINUTES` (default 30). It needs at least 3 readings spanning at least 10 minutes; otherwise the rate is null with the reason "too few readings".
- A change in `resets_at` means the window rolled over, and a new fit begins.
- It reports `rate_pct_per_hour` and `projected_full_at`, and `fills_before_reset` when the line crosses 100% before the reset.

It extrapolates the harness's own percent and never converts tokens into quota. It cannot say whether a particular *run* will finish before a limit.

## Rollups

The rollup sums the `ai-usage/*.yaml` records in the daemon's read-only work-repo mirror by their `days` split, standalone runs included, and every launch of a schema-2 run, not only the newest. Periods are UTC days, ISO weeks and calendar months.

- **Validation.** A container can push anywhere in the work repo, so each record is checked before it is summed. It must have a known version, counters within bounds, and totals that equal its rows. Its key must name the directory and file it was found at. A record that fails is skipped and counted under `invalid_records`.
- **Breakdown.** Records split by day only in total, so per-model figures per period are apportioned: each counter by its own daily share. That is exact for a record with one model.
- **Provisional numbers.** This host's live numbers that run ahead of their records are added as `provisional`, joined to their records by `platform_session`.
- **Caching.** Results are cached per mirror commit.

## Price table

`LMER_PLATFORM_USAGE_PRICES` names a daemon-side YAML file. Prices are per million tokens:

```yaml
currency: USD
models:
  claude-opus-5-5: {input: 0, cache_create: 0, cache_read: 0, output: 0}
  "claude-*":      {output: 0}     # an fnmatch glob; an exact id wins, then the longest glob
```

- **No defaults ship.** Without a table every cost is null and only tokens are shown.
- **Partial cover.** A model the table does not cover makes the cost `partial`. An omitted bucket is priced at zero.
- **Traceability.** Every cost names the `sha256` of the table that produced it.
- **Read-time only.** Cost is always computed when the record is read, never stored. A container cannot see the host's table, and a cost a container wrote would be a claim the daemon would have to distrust anyway.

## Configuration

| Variable | Read by | Default | Meaning |
|---|---|---|---|
| `LMER_USAGE` | container | on | Writes the run's usage record. `0` turns it off. |
| `LMER_USAGE_CHECKPOINT_SECONDS` | container | 900 | Checkpoint interval. |
| `LMER_USAGE_IDLE_SECONDS` | container | 300 | Longest gap that still counts as active. |
| `LMER_USAGE_PLATFORM_SESSION` | container | unset | The platform session's `s-…` id, set by the launcher (not configuration). Becomes the record's `platform_session`. |
| `LMER_PLATFORM_USAGE` | daemon | on | Runs the live collector. |
| `LMER_PLATFORM_USAGE_INTERVAL` | daemon | 30 | Collector tick, in seconds. |
| `LMER_PLATFORM_USAGE_PROJECTION_MINUTES` | daemon | 30 | Headroom fit window. |
| `LMER_PLATFORM_USAGE_PRICES` | daemon | unset | Price table path. |

The container variables are forwarded by the host CLI. See [LMER-CLI.md](./LMER-CLI.md).

## Limits

- **Killed containers.** A container killed outright keeps only its last checkpoint.
- **Standalone sessions** are absent from the live view.
- **Drop-in harnesses** are not collected until the canonical transcript format carries usage.
- **Fan-out children's usage** is in the record and the rollups, but not in the live view, so the fleet totals and the burn leave children out. A codex child's usage falls on the day it finished. A child killed by a timeout may leave a partial transcript.
- **codex** `cache_write_input_tokens` handling is an assumption, until a non-zero sample is captured.
- **Per-model rollup figures** are apportioned per day.
