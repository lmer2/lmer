# Schema 2: one execution, one record

Schema 2 keeps the [schema-1 layout](./SCHEMA.md), but changes who may write
a record, how repository identity compares, and how memory retirement works.
The root `_plexus.yaml` declares `schema: 2`. Older lmer versions that only
understand schema 1 must refuse writes; schema 1 remains supported under its
original rules until an explicit migration.

## Session identity and metadata

Every independently started agent execution receives a fresh, opaque UID:
32 lowercase hexadecimal characters generated from a random UUID. That UID
identifies both the writer and its record:

```text
<project>/_runs/<slug>/<uid>[--<name>]/
```

Helpers serving the same execution inherit its UID. A new execution never
reuses an old UID, even if the harness calls the operation “resume”. Starting
another execution with context from an older one creates a new record.
Historical records retain their IDs and contents; migration does not invent
process identities for old records containing several launches.

`spawn-harness` children are independent executions. Each receives its own
UID and record before the harness starts. An exec trampoline records the
actual child PID and execution hostname; the supervisor signs out that record
after reaping the child. Child harness/model/effort and resolved configuration
replace inherited selections, while unknown metadata survives. Parent
provenance is retained under `parent`. Inherited source, answer and unsigned
source acknowledgement are cleared; a child may supply its own explicit
source/acknowledgement through its environment overlay. Parent records remain
unchanged. Harness-native transcript persistence and schema-1 child behavior
retain their existing settings.

`state.yaml` uses record `schema: 2` and holds an extensible `session` mapping:

```yaml
session:
  uid: 981ec036134f4a8580927ddca71658a9
  operator: alice
  hostname: workstation
  started_at: '2026-09-17T12:34:56+00:00'
  pid: 4123
  harness: codex
  model: example-model
  effort: high
  config:
    preset: development
  custom:
    experiment: A
```

The same dictionary is recorded in `session_start.data.session` in
`events.jsonl`. The UID contains no operator, machine, PID, model or timestamp.
Those are metadata. Unknown fields and nested configuration are preserved.
Only the mapping and identity fields needed for routing are structural;
harnesses, models, efforts and configuration are not closed enumerations.
Metadata is scrubbed before it is recorded, at every nesting level, by three
rules:

- a value under a secret-looking key (a name matching the secret env-name
  patterns — `token`, `key`, `secret`, `password`, `credentials` — or
  `authorization` / `cookie`) is replaced whole;
- a string containing a value this process holds in a secret-named environment
  variable is redacted where that value occurs;
- a token-shaped substring is redacted even under a benign key and even when
  this process does not hold it: GitLab `glpat-` / `glrt-` / `gldt-`, GitHub
  `ghp_` / `gho_` / `ghu_` / `ghs_` / `ghr_` / `github_pat_`, AWS `AKIA…` /
  `ASIA…` key ids, Slack `xox[abprs]-` / `xapp-`, `sk-` API keys, and PEM
  private-key blocks.

A prefix is matched wherever it occurs, whatever precedes it: a token can
follow an ANSI colour code, a JSON-escaped newline, a hyphen, an underscore, a
letter or a digit, and that set is not enumerable. The cost is accepted
over-redaction of ordinary words that contain a prefix followed by a long
enough run: a hostname such as `lmer-task-worker-5d8f7c9b4-abcde` is recorded
as `lmer-ta***REDACTED***`, because `sk-worker-5d8f7c9b4-abcde` is
indistinguishable from an `sk-` key by its body. A missed token publishes a
credential to a shared repository; a mangled hostname does not.

That is the whole guarantee. An unprefixed secret (a bare password, a random
hex key) under a benign key that this process does not hold is not
detectable, so operator-supplied `session_metadata` must not carry one. Never
log the environment or raw secret-bearing configuration.

Host launch provenance can be retained under `launcher`. The top-level `pid`
and `started_at` describe the new execution; `execution_hostname` identifies
the machine or container where that PID exists. This keeps a host-supplied
`hostname` distinguishable from the container running the agent.

A record `work seed` creates for a later launch carries a seed identity rather
than a launch's metadata:

```yaml
session:
  uid: 2f0c1a7e9b5d4c3e8a1f6b2d7c9e0a4b
  seeded_by: 981ec036134f4a8580927ddca71658a9
  started_at: '2026-10-07T09:00:00+00:00'
```

`uid` is fresh — never the seeding launch's own — `seeded_by` names the launch
that wrote the seed, and `started_at` is the moment of seeding; nothing else.
The seeding launch's operator, hostname, PID, harness and model describe an
execution that never ran in that record, so none of them is copied, and the
record's `sessions` lineage is empty. The seed is written whole — `state.yaml`
with its goal and name, then `run_seeded`, `goal_set`, `run_named` — inside its
`.new-<uid>-*` temporary directory and renamed once to its final address. That
temporary directory is the single writer-rule exception: a launch may write a
record whose persisted `session.seeded_by` is itself only while the record is
still its `.new-<uid>-*` temp dir — judged on the `state.yaml` already on disk
whenever there is one, so a relabelled in-memory copy cannot name its caller
the seeder. From the final address on, the record is read-only to the seeding
launch like any other prior record; `seeded_by` is never an owner at
`_runs/<slug>/<uid>`.

Session ordering uses the recorded start time, with the UID as a stable tie
breaker. Random UID spelling is not chronological order. Directory modification
time is only a fallback for records without a usable timestamp.

## Continuing work

`LMER_SESSION_ID` identifies the new writer. `LMER_SOURCE_SESSION_ID` selects
read-only context. The new record captures the source UID, a hash of the source
state, its state snapshot, and recent events. It does not append lineage,
answers, owner changes or sign-out events to the predecessor.
Goal, phase, question, the name label and ledger/release checkpoints seed the
new record (a name inherited from the source is a label; a launch `--name`
replaces it, as it replaces a default label).
The context snapshot records their provenance; owner, claim and terminal
status are not inherited. A launch goal (`lmer --goal`, applied once by
`work session-start`) replaces the inherited one. When the source's target
derives no repository, name it explicitly with `lmer <taskdef> --repo
<url-or-path>`; `--repo` must itself be a repository. A continuation whose source
was filed under a different slug — a `chat-.` record from before local-path
targets exported an empty target — lands under the current derivation
(`chat`), keeps `source_session_uid` and `context.source_path` into the old
group, and `work session-start` warns that it crossed groups; nothing in the
old group moves.

A source without a recorded final `session_end` requires a warning and an
explicit decision. Present the available UID, operator, hostname, start time
and last activity. The user can wait/cancel, inspect the prior session, or
explicitly continue in a fresh execution while leaving the predecessor alone.
Stopping the predecessor is a separate user action. Age or silence never
means “signed out”. Unattended callers receive a needs-decision refusal.
The internal acknowledgement `LMER_ALLOW_UNSIGNED_SOURCE` names the exact
source UID; a boolean or another source's UID grants nothing.
A seeded source nothing ever started in — `session.seeded_by` set, no
`session_start` logged — is stopped by the same rule as a signed-out one: no
session can still be running in it, so it needs no acknowledgement, and an
acknowledgement given anyway is not recorded. Once a `session_start` is logged
in such a record its seeding is history, and the sign-out rule applies to it as
to any other source.

In the CLI, select context with `--source-session SOURCE_UID` (or the
schema-2 interpretation of `--session SOURCE_UID`). The two flags are mutually
exclusive. If the source has no final sign-out, inspect it before explicitly
retrying with `--allow-unsigned-source SOURCE_UID`. That value must match the
selected source. Platform resume and answer calls carry these explicit flags
through the host CLI. Ambient source/acknowledgement variables do not select
context for a fresh host launch.
The UI offers **Continue fresh** for the same decision; cancelling leaves
both the source and launch state alone.

## Repository identity

Schema 2 preserves repository path case and non-default endpoint ports.
`git.example.test/Team/Repo` and `git.example.test/team/repo` are distinct
unless a registry entry explicitly lists both as remotes. Hostnames retain
their usual case-insensitive comparison; normalized default ports remain
equivalent.

Registered repositories keep their short project directory. An unregistered
endpoint with a non-default port uses an encoded host directory:

```text
git.example.test:8443/Team/Repo
    → git.example.test%3A8443/Team/Repo/
```

A fallback that encounters a real legacy portless project directory refuses
to guess its endpoint. An actual filesystem case alias also blocks fallback.
Distinct paths on a case-sensitive filesystem are allowed. Registry entries
remain authoritative, and compatibility symlinks to registered projects do
not become competing project directories. Schema 1 retains its original
comparison and bucket rules until migration.

## Shared release claims

Independent records still coordinate shared effects. A project's release
claim lives at `<project>/_claims/release.yaml`, outside all session records:

```yaml
schema: 1
holder: 981ec036134f4a8580927ddca71658a9
updated: '2026-09-17T12:34:56+00:00'
```

The CLI publishes claim changes using the existing Git compare-and-swap
protocol. A local file write is not proof of ownership. A rejected push is
discarded and the remote claim rechecked before retrying. A claim never
expires solely because it is old. Its holder can release it; an operator can
explicitly use `work release unclaim --force` after investigating. Delayed
cleanup from an old session cannot clear a successor's claim.

A checkout lock spans sync, claim evaluation, file replacement, staging,
commit, push verification and rollback. Supported record writers acquire the
same lock before lookup/read/modify, not only when they later commit. This
includes logs, reports, artifacts, state/checkpoints, session lifecycle,
renames, memory and derived specification links, through both commands and
direct library mutation APIs. Thus no transaction's Git activity can erase a
successful concurrent write. The release transitions go further and never move
the checkout at all (see the claim transaction below), so a sibling session's
uncommitted record cannot leave the worktree, and a sibling's uncommitted file
cannot block a release, because of what another session is doing with the
release lock. If both locks are needed, checkout is acquired before memory
retirement; nested calls in one process are reentrant. A Git hook inherits a
transaction token, so a nested durability commit records a deferred request
instead of waiting for its invoking Git process. A hook's record mutation or
claim arbitration refuses before its first write. Library mutation APIs raise
the transaction error; command entry points report it. `session-end` retains
its hook-facing zero exit policy but explicitly warns that the operation was
refused. A verification command holds the lock for setup and receipt writing,
not while its external command runs, so that command can use work helpers.

Library callers combining multiple read/modify/write calls can hold
`work_repo.transactions.work_mutation()` across the complete operation. The
configured work-repository context determines the checkout, as it does for
the writer APIs. Direct filesystem or Git commands outside these APIs do not
participate in the cooperative lock.

Every release transition — claim, unclaim, the session-end release and abort —
publishes an object, and writes nothing in the checkout but this execution's
own record (abort's two terminal record writes, in their documented order).
Each one commits this execution's own record locally (`commit --only`, so an unrelated
staged edit stays staged), fetches the upstream head, and assembles the claim
commit in a TEMPORARY index seeded from that head: the fetched tree, plus this
execution's record subtree copied from the local snapshot, plus the new claim
document. `commit-tree` turns that into a commit object and one plain push
arbitrates it. Ordinary `work commit` and `session-end` keep staging every
record under the run group, as before.

Nothing is stashed, rebased or reset, at any outcome, and nothing outside this
execution's own record is written. A checkout is shared — helper children run beside their parent against one
work-repo clone — so a claim that moved the checkout's state could capture a
sibling session's uncommitted record; a claim cannot. A lost race needs no
rollback, because nothing local was changed: the next attempt re-fetches and
re-evaluates the claim from the new head, which is what keeps a claim from
being stacked onto an unchecked head.

The new claim is computed from the PUBLISHED claim at the fetched head, not
from the checkout's copy, and success still requires reading the expected
holder back from a freshly fetched published tree; a successful push command
alone is insufficient. Because the checkout is not written, its copy of the
claim file is only as fresh as the last `pull`. Every decision that depends on
who holds the claim therefore reads the published claim, not that copy:
`work release claim-status` and the session-end release read the upstream
tracking ref (the last fetch, so no network), and the claim, unclaim and abort
transitions read the head they just fetched. Deciding from the checkout's copy
is what once made a cleanly ended holder skip its own release and strand a
claim that no age expiry would clear.

## Memory retirement

`work memory forget <scoped-path>` archives content and records its retirement.
`work memory revive <scoped-path>` explicitly restores it. For example:

```bash
work memory forget widgets/_agents/memory/old-project-fact.md
work memory revive widgets/_agents/memory/old-project-fact.md
```

The root `_memory_retirements.json` is machine-owned state:

```json
{
  "version": 1,
  "retired": {
    "widgets/_agents/memory/old-project-fact.md": {
      "archive": "widgets/_archive/_agents/memory/old-project-fact.md",
      "index_lines": [],
      "metadata": {"reason": "superseded"}
    }
  }
}
```

Keys are scoped paths relative to the plexus root. Unknown document and entry
fields are retained. Restore and persist honor retirement even without a
restore baseline. A stale copy cannot revive retired content. An unchanged
local copy of an upstream deletion stays deleted; a locally edited copy
produces an explicit conflict and remains available locally.

Restore and persist also support an instance-only schema-2 context. A retired
project path cannot remove an index entry for a live instance path with the
same relative filename (or conversely). Routing and retirement comparisons
always include their scope. Edited restored files conflict before retired
paths are filtered out, so an edit cannot be silently discarded.

`forget` leaves a tombstone in the scope's index: the entry re-pointed at the
archive mirror. A tombstone stays in its own scope's index. Its relative link
is spelled the same in both scopes, so persist routes it by the index it was
restored from (the restore baseline records each scope's lines): a
project-scope tombstone is never copied into the instance `MEMORY.md`, where
the link would resolve nowhere, nor the reverse. Headings and other commentary
are still kept in both indexes.

Each forget/revive transition queues its complete live file, archive, scope
index and retirement-marker path set in checkout-local Git bookkeeping before
publication. Gate or hook deferral and pre-stage/commit/push failure leave that
request pending. The next ordinary `work commit` replays complete pending sets,
including requests from other sessions sharing the marker. Requests are removed
only after verified publication, so an event flush or failed retry cannot lose
half of a transition.

## Project index

A project directory's `README.md` may carry one generated Markdown block
between `<!-- lmer:index:start -->` and `<!-- lmer:index:end -->`: a table
with one row per execution record under `_runs/`, newest first, rendered from
each record's own `state.yaml`, `events.jsonl` and — only when the record has
neither name nor goal — `log.yaml`. The root `README.md` may carry the same
kind of block listing the registered projects from `_plexus.yaml`. Text
outside the markers is human content, never touched and never checked; the
first append normalises only the hand text's trailing newlines.

**Who writes it.** Every durability push of a session in the project —
`work commit` and the implicit pushes of `work state set`, `goals`,
`ledger set`, `artifact`, `answer`, `release record` and `session-end` —
re-renders the project's block from the records the commit will contain
(state files already tracked, or under a path the push stages) and stages
the README with the record, so no row can point at a directory the remote
does not have. A push that stages no record (the memory verbs) re-renders
nothing; it only carries a tool-written README another push left behind. `lmer-schema index PATH [--write] [--diff]`
previews and backfills every project README and is the only writer of the
root block, which never changes on the hot path. It never commits, and since
no push stages the root `README.md`, a backfill that wrote the root needs an
explicit commit through the normal gated commit; `work commit` carries only
the project READMEs. Both writers hold a project directory to the same
boundary before writing in it: one that is a symlink, resolves outside the
repository or lies inside a `_runs/` record is never written (the push skips
it with a warning, the backfill reports it `refused`), so neither can reach
outside the tree or into a historical record.

**What a row says.** The label is the record's `name` — an unnamed schema-2
record carries the default label `<taskdef>-<yymmdd>-<uid4>`, and a
continuation inherits its source's name — else the goal's first line, else
the first `log.yaml` message (a derived label is marked `_(unnamed)_`, which
now means only `name: null`); a continuation links its source record, by
session uid. The state cell uses five clock-free words — `closed` (status
complete or archived), `waiting on question` (a recorded question outranks a
sign-out), `seeded` (a `work seed` record nobody has run in),
`ended, not closed` (in progress with a recorded final `session_end`) and
`open` (no sign-out recorded) — beside a last-activity column that is the
date the record last changed. Nothing in the block ends a run; as everywhere
in this schema, age or silence never means “signed out” (`work sessions`,
which holds its own clock, is where `open` splits into `active` and `stale`).
Every row is a pure function of its own record — no generated-at stamp, no
counts, no mtime — so two regenerations differ only where records differ.

**Concurrency.** Because rows are pure and ordered by the record's own start
time, git's three-way merge of two regenerations is the fresh render whenever
it merges cleanly. Two sessions pushing in one project usually touch adjacent
lines, so the conflict case is the normal one, not the exception: when
`pull --rebase` stops and every unmerged path is a project README whose text
outside the markers is identical on both sides and whose upstream block is
not of a newer format, the push keeps the upstream text, renders the block
again from the merged tree and continues the rebase; any other conflict
aborts publication as before ([WORK-REPO-SAFETY.md](./WORK-REPO-SAFETY.md)).

**Mixed versions.** The legend carries a format token
(`lmer index format 1`). A writer never rewrites, the conflict handler never
resolves over, and the validator never judges a block of a newer format than
its own. A block an older runtime left behind is a self-healing
`index.readme-stale` WARN until the next push from a current runtime or an
`lmer-schema index --write`; old runtimes never write READMEs, so they never
conflict on them.

**The hand-text rule.** A project README whose text outside the markers
holds a line git reads as a conflict marker — seven identical `<`, `=`, `>`
or `|` at column 0, a setext `=======` underline being the usual case — is
never staged by the generator, because the staged-marker gate would otherwise
refuse every push of the project. The record still publishes, each push
prints a one-line warning, and the validator reports the file
(`index.readme-missing` INFO without a block, `index.readme-stale` WARN with
one) until the line is indented or lengthened. The validator compares an
existing block with a fresh render of what is on disk, clock-free, so
validate a clean clone: a checkout holding unpushed records may differ.

## Compatibility rule

An optional addition may keep the same version only when old readers **and
old writers** can safely ignore it. Parsing successfully is insufficient.
Any addition that changes ownership, routing, deletion, exclusion or write
authority requires a version boundary unless existing writers already enforce
the necessary behavior. Writers must preserve unknown fields when rewriting
a compatible document. Unsupported newer versions are read-only refusals.

The root schema version governs the tree; individual machine-owned documents
keep their own format versions. Consequently a schema-2 tree can contain
historical record schema 1 and release-claim document schema 1. New executions
write record schema 2; historical records remain context.
Log/report entry points and direct ledger/release checkpoint writers enforce
this boundary too. Replacing a newer on-disk document with a caller-supplied
older version is refused without changing its bytes.

## Migration

Stop old writers, then review the preview before applying:

```bash
lmer-schema migrate /path/to/plexus
lmer-schema migrate /path/to/plexus --apply
lmer-schema validate /path/to/plexus
```

The preview shows the manifest diff and every blocker. Cutover preserves
comments, unknown fields, historical state bytes, session IDs and paths.
It refuses duplicate historical session IDs, unresolved release claims,
active records without sign-out, and flat records that still need layout
migration. Declared remote endpoints are checked for ambiguous legacy buckets
and filesystem aliases. An unregistered portless historical directory cannot
reveal its original endpoint; migration never invents one. It does not sign
out a session or infer that one is dead. Applying
an already migrated tree is a no-op. The command changes local files; review,
commit and publish the result through the normal repository gates.

## Additional inventory

| Path | Writer | Contract |
|---|---|---|
| `_memory_retirements.json` | Memory forget/revive | Optional machine-owned versioned retirement dictionary |
| `<project>/_claims/release.yaml` | Release claim CLI | Optional machine-owned shared resource claim |
| `<session-dir>/state.yaml` → `session` | Current execution | Freeform metadata dictionary, opaque UID required |
| `<session-dir>/events.jsonl` → `session_start.data.session` | Current execution | Start metadata snapshot |
| `README.md` → index block | `lmer-schema index --write` | Optional generated project list between the index markers, rendered from `_plexus.yaml` plus a fixed legend; text outside the markers is content |
| `<project>/README.md` → index block | Every durability push in the project; `lmer-schema index --write` | Optional generated run table between the index markers, one row per committed record, newest first; text outside the markers is content |

The validator accepts unknown session metadata. It reports invalid identity
envelopes as `record.session-metadata`, invalid shared claims as
`record.release-claim`, malformed retirement state as
`agents.memory-retirement`, and a missing or stale index block as
`index.readme-missing` / `index.readme-stale`.
