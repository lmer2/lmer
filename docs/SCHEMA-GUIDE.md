# The lmer schema, for the operator

An operator companion to [SCHEMA.md](./SCHEMA.md) (the schema-1 layout) and
[SCHEMA-V2.md](./SCHEMA-V2.md) (the schema-2 ownership and migration contract).
Those documents are normative. This guide separates legacy layout adoption
(§3) from schema-1 → schema-2 cutover (§4); the test checklist is §5.
A manifest with `schema: 1` retains schema-1 behavior even on a v2-capable
runtime. Installing a new runtime does not migrate a tree.

## 1. What the schema is

**The problem.** Everything lmer writes outside a code repo — run records,
specs, plans, memory, notes, taskdefs, agent config — lives in one git repo
whose layout was whatever the code happened to do, and whose project identity
was its *path*: `{host}/{project}/`. So a repo with two remotes is two trees, a
renamed repo a third, and "what is the layout?" is answerable only by reading
the code. The schema writes the layout down and ships an executable form of
it.

**One repo conforming to that layout is a plexus.** Your work repo is a plexus
in its *legacy form*.

**Form detection.** A `_plexus.yaml` at the repo root means schema form; no
manifest means legacy form. That is the whole test — one file, no flag, no
config. The same lmer release reads both, so you migrate trees one at a time,
and never have to.

**The manifest and the registry.** [`_plexus.yaml`](./SCHEMA.md#the-manifest-_plexusyaml)
carries a `schema:` int, a `name:`, and `projects:` — the registry, which is
now the *only* source of project identity. An entry maps a short name to a
primary `repo:` and optional additional `remotes:`. A session normalizes its
target URL and looks it up; a hit resolves to `<short-name>/`. Two remotes for
one repo land in one tree, with no symlinks.

**Flat short names, and the compat symlink.** Projects are top-level
directories named `widgets/`, not `git.example.com/acme/widgets/`; a dot in a
top-level name marks a [host bucket](./SCHEMA.md#host-buckets) instead — the
holding place for a repo nobody has named yet. Names are never derived: an
unregistered repo's work goes to the bucket and stays there, visibly unnamed.
After you move a project you MAY leave a relative symlink at the old path
(informational); a real directory left there is an ERROR, because work would
then land in two places.

**Session dirs are the run record.** In schema form the unit of record is the
*session*: one launch writes one complete directory at
`<project>/_runs/<slug>/<session-id>[--<name>]/`. `_runs/<slug>/` is a grouping
folder keyed by (taskdef, target) and nothing is written at that level. Two
launches against one issue are two records that never touch. For legacy → schema-1 adoption, file shapes from
[RUN-STATE.md](./RUN-STATE.md) stay unchanged. Schema 2 gives every new
execution a fresh opaque UID and a record with `schema: 2` plus a `session`
metadata mapping. Continuation reads an older record as context and writes
a new one; historical records keep their IDs and bytes (see §4).

**Two memory scopes.** A memory's own `metadata.type` picks its scope:
`project` stays at `<project>/_agents/memory/`; `user`, `feedback` and
`reference` lift to `_agents/memory/` at the root. Recall is the union, project
overlaying instance, so a misplaced memory loses nothing — it is a hygiene
warning, not an error.

**`_agents/`, `_specs/`, `_plans/`.** `_agents/` is the runtime surface at
both scopes — `claude/` (harness config), `mirrors/` (vendored tooling, opaque)
and `memory/`. `_specs/` is a flat index of dated relative symlinks into
session dirs. `_plans/` is specified and **has no shipped writer at schema 1**:
it exists so a connector's writes and the validator's reads agree from the
first one.

**The `_` prefix is the tell.** Every schema-owned name carries it, so `ls`
shows a tree's migration state. Undefined `_` names are warnings. Inside a
project, non-`_` names are content and never checked; at the instance root,
non-`_` top-level names are checked (see *Instance root* below).

## 2. How each part is used

`lmer-schema validate` checks shape. It does **not** check whether lmer can
still find a thing. The "dual-read" column is the one that matters when you
migrate: areas marked *no* are routed by form, so renaming the project without
renaming them makes them silently invisible.

### Instance root

| path | written by | read by | dual-read | exercised by |
|---|---|---|---|---|
| `_plexus.yaml` | you, by hand, or `work adopt` | `lmer_schema.form.detect_form`, `work_repo.utils.work_repo_layout` | n/a | `lmer-schema validate .` |
| `<short-name>/` | your `git mv`; a session on first write | registry resolution | n/a | `work sessions` |
| `_knowledge/` | humans and agents | humans and agents | n/a | nothing ships at schema 1 |
| `_napkin/` | humans and agents | **nothing in this release — see below** | no | — |
| `_taskdef/` | you, or a mounted taskdef repo | `taskdef_search_dirs()` | yes (`_taskdef` then `taskdef`) | `lmer --task <name>` |
| `_agents/` | see *Agent surface* | | yes | |
| `_archive/` | housekeeping tooling | humans | n/a | nothing ships at schema 1 |
| `README.md` | you (outside the markers); `lmer-schema index --write` (the block) | humans on GitLab; the validator | n/a | `lmer-schema index .` |
| any other top-level name | humans | the validator | n/a | `lmer-schema validate` — project-shaped with no registry entry: `tree.unregistered-project` WARN; dotted: `tree.host-bucket` INFO; a registered remote's old path as a real directory: `tree.bucket-collision` ERROR |

**`_napkin/` has no reader in this release.** Subdir mode resolves
`$LMER_NAPKIN_PATH` to `{work_repo}/napkin` unconditionally
(`lmer_cli.cli._resolve_napkin_path`). Renaming `napkin/` → `_napkin/` splits
your notes in two and the validator will not say a word, because a bare
`napkin/` is a recognised legacy name and `_napkin/` is a legal schema name.
**Leave `napkin/` alone** until a release routes it.

### Project tree

| path | written by | read by | dual-read | exercised by |
|---|---|---|---|---|
| `<project>/_runs/` | `work` CLI | `work` CLI resolver | no | `work session-start` |
| `<project>/_runs/<slug>/` | nothing — grouping level only | the resolver's scan | no | `work sessions` |
| `<project>/_runs/.new-*` | `work` CLI (seed, then one atomic rename) | nobody | no | any first write in a run |
| `<session-dir>/` | see *Session directory* | | no | |
| `<project>/_specs/` | `work artifact`, `work session-end`, `work specs-index --rebuild` | humans; `work specs-index` | no | `work specs-index` |
| `<project>/_plans/`, `PLANS.md`, run-plan links | no shipped writer at schema 1 | the validator | no | `lmer-schema validate` |
| `<project>/_info/` | you | `work read-project-info` | **no** | `work read-project-info` |
| `<project>/_info/gate-check.yaml` | you | `gate-check` (after `.lmer/gate-check.yaml`) | no | `gate-check` |
| `<project>/_taskdef/` | you | `taskdef_search_dirs()` | yes | `lmer --task <name>` |
| `<project>/_agents/` | see *Agent surface* | | no (memory) | |
| `<project>/_archive/` | housekeeping; the session cleaner | humans | n/a | nothing ships at schema 1 |
| `<project>/README.md` | every durability push in the project (block); you (outside); `lmer-schema index --write` | humans on GitLab; the validator | no | `work commit` |

### Session directory

File contracts are in RUN-STATE.md; schema-2 identity and ownership additions
are in SCHEMA-V2.md. Historical schema-1 records remain readable. The verb *is*
the writer; nothing else may touch the file.

| path | writer verb | read by | exercised by |
|---|---|---|---|
| `state.yaml` | `work state` | resume brief, `work sessions`, validator | `work state` |
| `events.jsonl` | `work event` (append-only) | resume brief, validator | `work event <type> --note …` |
| `ledger.yaml` | `work ledger set` | a successor session's recovery | `work ledger` |
| `release.yaml` | `work release` | resume brief, `work release status` | `work release status` |
| `log.yaml` | `work log` | `work log` (display) | `work log "…"` |
| `plan.index.json` | the session; linted by `work plan check` | the plan gate | `work plan check` |
| `goals.md` | `work goals` (freeze/amend/assess) | the goal gate | `work goals check` |
| `spec.md`, `plan.md`, `retro.md`, … | the session, via `work artifact` | humans; `_specs/` links here | `work artifact spec.md --file …` |
| `reports/` | `work report` | humans | `work report --file <file>` |
| `masterplan/` | the masterplan orchestrator (foreign, opaque) | humans | `/masterplan` |
| `<artifact> -> masterplan/<artifact>` (including `plan.html`) | `work artifact --sync`, `work commit`, `work session-end` | humans; validator | `work commit` |

### Agent surface (`_agents/`; only `memory/` is read at both scopes)

| path | written by | read by | dual-read | exercised by |
|---|---|---|---|---|
| `_agents/claude/` (instance only) | you | `libexec/claude-runner.sh` → `harness_work_agent_files_root` (`_agents` then `agent-files`, work-repo root only) | yes, with a stderr notice if both names exist | session start |
| `_agents/mirrors/<name>/` (instance only) | mirror tooling (opaque) | `libexec/masterplan-enable.sh` candidate list (`/taskdef/mirrors/masterplan`, then `_agents/mirrors/`, then `mirrors/`) | yes (schema preferred over the legacy work-repo path) | `/masterplan` |
| `<scope>/memory/` | `work memory persist` | `work memory restore` at session start | **no** | `work memory persist` |
| `<scope>/memory/<name>.md` | same | recall (union of both scopes) | no | same |
| `<scope>/memory/MEMORY.md` | same | recall | no | same |

## 3. Adopting a legacy repo into schema 1

Legacy layout adoption is tooling. `work adopt <host>/<project> <shortname>`
names a host bucket, registers it and moves it to `<short-name>/` in one
commit (dry run by default); `work migrate <project>` then moves the bare
areas inside it to their schema names. The separate `lmer-schema migrate`
command upgrades an already adopted schema-1 tree to schema 2 (§4); it does
not move legacy directories. The form each tree takes is described in
SCHEMA.md's [Legacy form](./SCHEMA.md#legacy-form) section and
[Appendix A](./SCHEMA.md#appendix-a-legacy-mapping-informative).

### Prerequisites

- An lmer release that supports the manifest version. Check
  `lmer-schema --help` and the versioned contract; check counts alone do not
  establish runtime compatibility. Pre-schema lmer pointed at an adopted tree
  can write a second, legacy-shaped record beside the real one; schema-1-only
  lmer refuses a schema-2 manifest.
- Every fleet member on that release. Cutover is of **lmer versions, not
  trees**.
- A clean work repo: `git -C "$LMER_WORK_REPO_PATH" status --short` empty, and
  nothing live in the project you are about to move (`work sessions --running`
  answers for the current project, so ask per project).

### Dry run on a scratch clone — do this first, every time

```bash
# A local bare remote: every `--apply` pushes its one commit there, and a
# stray push can never reach the real work repo.
git clone --bare "$LMER_WORK_REPO_PATH" /tmp/work-scratch-remote.git
git clone /tmp/work-scratch-remote.git /tmp/work-scratch
PYTHONPATH=src python -m lmer_schema.cli validate /tmp/work-scratch
```

Keep that baseline validator output; compare each step's findings with it.
Run the project steps inside a subshell whose work context points only at the
scratch clone (replace the example identity with the project being migrated):

```bash
(
  cd /tmp/work-scratch
  export LMER_WORK_REPO_PATH=/tmp/work-scratch
  export LMER_REPO_URL=https://git.example.test/group/project
  export LMER_REPO_HOST=git.example.test
  export LMER_REPO_PROJECT=group/project
  export LMER_TASK=develop
  export LMER_TASK_TARGET=https://git.example.test/group/project/-/issues/999
  export LMER_SESSION_ID=20260912-160000-9-4242
  # Run steps 1–6 here with the schema-capable work/lmer-schema executables.
)
```

`work` resolves its tree from this environment, not the shell's working
directory. Keep the same context for every `work` command, including index
rebuild and session listing. Exiting the subshell restores the live context.
When using a source checkout instead of installed executables, give
`PYTHONPATH` an absolute path to that checkout's `src/` directory.

### The steps, in order, for one project

Every `--apply` lands ONE commit and pushes it; a failed push exits `1` with
the commit already made locally. Re-run `lmer-schema validate` after each
step and compare the finding codes with the baseline.

**1. Adopt the bucket.** `work adopt` resolves `<host>/<project>`, checks the
shortname, the registry and the schema-tier fix list, bootstraps `_plexus.yaml`
if the tree has none (one loud notice: that file flips the WHOLE tree to schema
form), writes the registry entry through the writer, and `git mv`s the directory
to `<short-name>/` in ONE commit. Dry run by default; `--compat-link` leaves a
relative symlink at the old path (INFO), `--remote URL` adds a remote, and
`--force` adopts despite schema-tier ERRORs, carrying them into the new
directory as they are.

```bash
work adopt git.example.test/group/project example          # dry run
work adopt git.example.test/group/project example --apply  # one commit
```

**2. Migrate the project.** `work migrate <project>` plans the bare-area
renames (`runs/` → `_runs/`, `specs/` → `_specs/`, `plans/` → `_plans/`,
`info/` → `_info/`, `taskdef/` → `_taskdef/`), nests each legacy run dir under
its first session id, lifts non-`project` memories to the instance scope,
re-points the `_specs/`/`_plans/` entries in place (keeping their registration
dates), folds the pre-A1 plain-session groups (step 3), and lands ONE commit,
which also regenerates the project README's index block.
Dry run by default and idempotent: a re-run is a no-op. `--compat-link` leaves
a relative symlink at each old bare path. A legacy tree gets the nesting and
the fold in one move.

```bash
work migrate example            # dry run
work migrate example --apply    # one commit
```

**3. The fold.** A record whose slug is the obsolete derivation of its own
target — `chat-.`, `chat-<repo>`, `chat-<repo>.git`, `masterplan-.` — moves
into the bare taskdef group: `_runs/<taskdef>/<session-id>[--<name>]`, with
only its `slug:` scalar rewritten and every other byte identical; its `_specs/`
entries are re-registered under the record's new label, dates kept. A record
re-slugged on purpose (anything carrying `reslugged_from`, the release record)
is never touched. A destination session dir that already exists is never
overwritten: in a schema-form `_runs/` the record is reported and skipped;
while a legacy `runs/` dir is still being nested, it is a blocker that refuses
the whole migration. Merge the two by hand, then re-run. The fold is refused
while any session in the group is live (no final `session_end`, or an
unreadable record): finish or sign out that session, or for a stale record
that never signed out run `work housekeep --archive-stale`. The same predicate
is the validator's `record.plain-session-slug` ERROR and the `lmer-schema
migrate` blocker. A platform row tracked under an old slug stops resolving and
is forgotten by hand: `lmer platform forget <host>/<project>/<old-slug>`.

**4. The instance surfaces.** `work migrate --instance` moves
`agent-files/claude` → `_agents/claude`, `mirrors` → `_agents/mirrors`,
and `taskdef/` → `_taskdef/` — it leaves `napkin/` in place (see *What to
leave alone*) — and folds a standalone
`sources.yaml` into `_plexus.yaml sources:` through the writer, removing the
file in the same commit. It REFUSES unless the running release reads the
manifest `sources:` tier: deploy the release carrying #370 on every host first,
because removing `sources.yaml` switches declared sources off on any host still
running a pre-#370 release.

```bash
work migrate --instance            # dry run
work migrate --instance --apply
```

After an applied `work migrate`, the validator runs at the schema tier and its
whole report is printed; only ERRORs under what this run migrated make the
exit code non-zero (other projects awaiting their turn are reported, not
counted), and the commit stands either way.

**5. Validate, then start a session against the migrated tree.**

```bash
lmer-schema validate /tmp/work-scratch
LMER_WORK_REPO_PATH=/tmp/work-scratch LMER_REPO_URL=https://<host>/<group>/<repo> \
  LMER_TASK=develop LMER_TASK_TARGET=https://<host>/<group>/<repo>/-/issues/999 \
  LMER_SESSION_ID=20260912-160000-9-4242 work session-start
```

The record must land at
`<short-name>/_runs/develop-issue-999/20260912-160000-9-4242/` holding
`state.yaml` and `events.jsonl`. Confirm the old history came through:
`work sessions` lists one more record after this launch than before, with the
migrated records' session ids unchanged.

**6. Roll back.** On a scratch clone, `git reset --hard <baseline commit> &&
git clean -fd` returns the tree to the baseline validator output exactly. On the
real repo **do not use `git clean`** — it deletes untracked run state other
sessions have written. Branch-switching the live checkout is not safe either:
other sessions `work commit` into it. With a local migration branch that has no
upstream checked out, every `work commit` there fails ("the current branch has
no remote branch upstream configured"); with an upstream, other sessions'
records publish to the migration branch. Migrate in a **separate clone**, push
the result as one commit, and roll back by not pushing (or by reverting that
commit).

**7. Repeat per project.** One project, one commit, one validate. A tree
holds migrated and unmigrated projects at once; that is the point of the `_`
prefix.

**8. Render the index blocks.** `lmer-schema index . --write` (preview first
without `--write`): every project README gets its generated run table and the
root README its project list, text outside the markers untouched. Commit the
result as one change through the normal gated commit — the root `README.md`
included: no session's push ever stages it, so `work commit` would leave it
behind. From here every durability push keeps the project blocks fresh on
its own; the root block is yours to re-render, and commit the same way, when
`_plexus.yaml` changes.

### What to leave alone

- `napkin/` — no reader for `_napkin/` in this release (§2); `work migrate
  --instance` leaves it in place and says so.
- `{host}/{project}/{task_type}/{target}/` — recognised legacy and a read-side
  fallback; informational, not required to move. (`runs/archive/` is not in
  this list: `work migrate` relocates it to `_archive/_runs/`.)
- Instance `agent-files/` and `taskdef/` — dual-read — and `mirrors/` — an
  opaque operator directory no runtime reads — so `work migrate --instance`
  can move them whenever you like once #370 is deployed fleet-wide.
- `<project>/README.md` text outside the markers is yours; the block is
  regenerated; edit the hand text in a separate clone while sessions push.

## 4. Cutting over from schema 1 to schema 2

### Deploy the runtime before changing the manifest

Every writer of the shared plexus must support schema 2, including host
CLIs, containers, platform-launched sessions and independent harness children.
A schema-1-only runtime refuses the newer manifest. Stop existing writers
before cutover; a clean validator does not prove that the fleet is stopped.
Keep the legacy layout adoption in §3 separate from this version change.

Upgrade every operator's host `lmer` as well, before the cut-over, not only
the image: the host CLI alone exports `LMER_TASK_TARGET`. A current container
classifies the target it is given (`work_repo.utils.classify_target`), so a
lagging host's `.` lands in the bare-taskdef group anyway — but a host still
exporting the raw spelling keeps filing local-path launches (`lmer chat .`)
under `chat-.` beside the `chat` groups current hosts create. After the
cut-over, a new `record.plain-session-slug` finding from `lmer-schema
validate` names a lagging runtime; fold the group (below), then reinstall
that host's CLI.

On each operator host, from the checkout of the merged tree:

1. Reinstall the CLI so `lmer`, `work` and `lmer-schema` run that tree:
   `uv tool install --force --from <checkout> lmer` (a copied install,
   repeated after every pull) or `uv tool install --force --editable
   <checkout>` (follows the checkout). `lmer-schema --list-checks` lists
   61 codes on this release.
2. Re-run `bash <checkout>/hooks/install.sh`. The pre-push hook it writes is
   not self-updating — the container entrypoint installs it only into a
   checkout that has none — so a checkout set up before #116 keeps a hook
   that substring-matches the remote URL and refuses every `repo|refpattern`
   allow-list entry. The symptom is `gate-push` passing every check,
   including the full suite, and the push itself then failing with
   `PUSH BLOCKED: Repository not in allow list`.
   `grep -q refpattern <checkout>/.git/hooks/pre-push` confirms the current
   hook; linked worktrees share the checkout's hooks, so one run covers
   them. The installer also writes the `rgr`/`rgrpc`/`pc` wrappers into
   `~/.local/bin` and leaves existing `~/.claude/commands` and
   `~/.claude/settings.json` alone.
3. Rebuild the session image.

In the session image, confirm that PyYAML bundles libyaml
(`python3 -c "import yaml; assert yaml.__with_libyaml__"`): the index
kernel's fast reader keeps the per-push render cheap either way, and libyaml
makes its full-parse fallback an order of magnitude faster.

Use an isolated clone of the intended committed migration tree. A plain
`git clone` does not copy staged or unstaged changes from a live checkout.
Retain the original manifest and record hashes for comparison. Keep its
remote removed for layout tests, or point it only at a disposable local bare
repository when testing publication.

### Fold plain-session groups

A schema-2 tree that already holds `chat-.`, `chat-<repo>` or
`chat-<repo>.git` groups (filed by a pre-classifier host) folds them into the
bare-taskdef group with `work migrate <project>`: dry run first, then
`--apply`. It is idempotent, and it refuses while a session in the group is
live. `lmer-schema migrate` lists a plain-session group as a blocker naming
the same command, so `--apply` cannot land while one remains. See §3 for the
adoption step that produces those groups.

### Preview, apply, and prove preservation

With the v2-capable runtime selected:

```bash
lmer-schema migrate /path/to/scratch-plexus --json
lmer-schema migrate /path/to/scratch-plexus --apply --json
lmer-schema validate /path/to/scratch-plexus --json
lmer-schema migrate /path/to/scratch-plexus --apply --json
```

The first call is read-only. Review `diff` and `blockers`; do not apply with
blockers present. The first successful apply changes only the manifest's
version scalar, preserving comments, unknown keys, historical IDs, record
bytes and paths. A plain-session fold is a prerequisite the blocker names,
not part of the manifest apply: it changes only the paths of the folded
records and the one `slug:` scalar each carries. The second apply reports `applied: false` with no changes.
A blocked migration exits 1; unusable input exits 2. Validate before and
after as well: migration preflight is not a replacement for full validation.

Blockers include flat records needing §3's layout adoption, duplicate
historical session IDs, unresolved release claims, and in-progress records
without recorded sign-out. Declared endpoints are also checked for ambiguous
legacy buckets and filesystem aliases. Inspect a blocked predecessor;
never infer sign-out from age or manufacture an end event to pass preflight.

### Exercise writes, publication, and continuation

Before launching these tasks, commit the scratch manifest change through the
normal gate and publish it to the disposable local remote; verify that remote's
`_plexus.yaml` declares `schema: 2`. Ordinary `work commit` publishes session,
specification and memory paths, not the manifest. Running it with an uncommitted
version change would otherwise publish v2 records under a schema-1 manifest.
Production cutover is a separate step after the rehearsal.

Launch a real task with the v2 runtime against the scratch plexus. Normal
launch provisioning assigns the fresh UID and execution metadata; do not
copy an old `LMER_SESSION_ID` into a new execution. Inside the launched task:

```bash
work log "Schema 2 migration rehearsal: new execution writes its own record"
work artifact spec.md --file /path/to/rehearsal-spec.md
work sessions --json
work resume --json
work commit
```

Use a disposable local remote for publication. Check the published tree,
not just the command's exit status. Expect a new directory at
`<project>/_runs/<slug>/<32-hex-uid>/` (optionally with a `--name` suffix),
`state.yaml` with record schema 2 and `session.uid`, and a matching
`session_start.data.session` event. Confirm the artifact and `_specs/` link
are published and every predecessor's bytes are unchanged.

After the execution signs out normally, start a new execution with
`lmer --source-session <source-uid>` and the same task/target selection.
It must receive a different UID, retain source provenance and context, and
leave the source record byte-identical. An unsigned source requires an
explicit decision naming that exact UID (`--allow-unsigned-source`); a
boolean acknowledgement or another UID is refused. See
[Continuing work](./SCHEMA-V2.md#continuing-work) for platform behavior and
human answers; `work answer` is scoped to an owning execution, not a way to
edit somebody else's historical record.

Also exercise memory forget/revive and release claim/unclaim/abort in the
scratch repository when those features are used. Schema 2 keeps retirement
in `_memory_retirements.json` and shared claims at
`<project>/_claims/release.yaml`. Claim decisions use published state: the
checkout copy can be stale. Neither retirement nor release ownership is
established merely by a local file write.

Only after these checks and fleet readiness should the reviewed manifest
change be committed and published through normal repository gates. Before
v2 writes begin, reverting an unshipped manifest change restores the prior
state. After v2 records, shared claims or retirement markers exist, changing
`schema: 2` back to `1` is not a safe rollback; retain a v2-capable runtime
and recover with the recorded data intact.

### Survey chain depth, then backfill the index blocks

Every durability push in a project re-renders that project's `README.md`
index block from its records, and a continuation record embeds its source's
whole state, so the deepest continuation chain sets the per-push cost. Survey
it before the cut-over: `grep -c '^context:'` is not enough (it counts one
level per record), so count the `source_session_uid` links and follow them to
the longest chain. A 20-deep chain renders in tens of milliseconds with the
kernel's context cut; a project that measures much slower deserves a look
before hundreds of sessions start paying for it on every push.

After the manifest change is published, backfill the blocks as an explicit
step of the migration, not as something the first pushes will get round to:

```bash
lmer-schema index .
lmer-schema index . --write
lmer-schema validate .
```

The preview lists one action per README (`create`, `update`, `unchanged`, or
`refused` with its reason); `--write` creates every project README that is
missing — a registered project without records gets the zero-record block —
and the root project list, text outside the markers untouched. Commit the
result through the normal gated commit, and make sure the root `README.md` is
in it: `work commit` publishes records, specs, memory and project READMEs,
never the root, which stays off the push hot path on purpose. The `--write`
summary line says so whenever it wrote the root (`commit the root README.md
explicitly through the normal gated commit`); a root left to `work commit`
stays uncommitted and disappears with the container. From then on the
project blocks keep themselves fresh; the root block is re-rendered, and
committed the same way, when `_plexus.yaml` changes.

The backfill never writes inside a historical record. A project directory is
written into only when it is the real directory its name says: one that is a
symlink (a registered short name aliased to another path), resolves outside
the repository, or lies inside a `_runs/` record is `refused` before
anything is written, and the publication path skips it with a warning. An
alias like that is a layout error to fix, not a project to index; running
the command through a symlink to the repository root is fine.

## 5. Test checklist

Run these checks against the runtime version you intend to deploy. The
schema-1 measurements below are historical examples; v2 differences are
called out explicitly. Run them from the repo root
(`PYTHONPATH=src` and the fixture import are both relative to it). `$SCRATCH`
is any empty directory.

**1. The validator is installed and complete.**

```bash
PYTHONPATH=src python -m lmer_schema.cli --list-checks | wc -l      # v2: 61
PYTHONPATH=src python -m lmer_schema.cli --list-checks --json \
  | python -c 'import json,sys; d=json.load(sys.stdin); print(len(d["checks"]))'
```

The v2 runtime registers 61 codes: 29 ERROR, 22 WARN, 10 INFO; tiers
`machine` 52, `legacy` 5, `not-schema` 3, `opaque` 1. The schema-1 runtime
registered 56 codes (26 ERROR and 47 machine-tier checks). Five soften under the legacy tier
(`agents.memory-entry-without-file`, `agents.memory-scope-mismatch`,
`index.dangling-link`, `index.entry-without-file`,
`manifest.sources-legacy-file`). On the v2 runtime `manifest.schema-older` is reachable: it warns for
`schema: 1`, which remains supported.

**2. Both reference trees validate clean.** The fixtures in
`tests/plexus_fixtures.py` are the schema's worked example.

```bash
python - "$SCRATCH" <<'PY'
import sys; sys.path[:0] = ["src", "tests"]
from pathlib import Path
from plexus_fixtures import build_schema_plexus, build_legacy_plexus
base = Path(sys.argv[1])
build_schema_plexus(base / "plexus-schema")
build_legacy_plexus(base / "plexus-legacy")
PY
PYTHONPATH=src python -m lmer_schema.cli validate "$SCRATCH/plexus-schema"; echo "exit=$?"
PYTHONPATH=src python -m lmer_schema.cli validate "$SCRATCH/plexus-legacy"; echo "exit=$?"
```

On the v2 runtime the schema-1 fixture gives `exit=0`,
`0 error(s), 1 warning(s), 3 informational`: `manifest.schema-older`, plus
`tree.host-bucket` for the fixture's unnamed repo, `record.session-stale` for
its in-progress session and `index.readme-missing` for `lmer/README.md` (the
project holds run records and no generated index block). Legacy tree:
`exit=0`, `0 / 0 / 7`, verdict line ending *"a migration aid, not a gate"*.

**3. Exit codes mean what they say.**

```bash
PYTHONPATH=src python -m lmer_schema.cli validate /nope; echo "exit=$?"          # 2
mkdir -p "$SCRATCH/broken"
printf 'schema: true\nname: x\n' > "$SCRATCH/broken/_plexus.yaml"
PYTHONPATH=src python -m lmer_schema.cli validate "$SCRATCH/broken"; echo "exit=$?"  # 2
PYTHONPATH=src python -m lmer_schema.cli validate "$SCRATCH/broken" --list-checks   # 2, stderr
```

`/nope` reports `runner.no-such-path`. The bool `schema:` reports
`manifest.schema-not-int` plus `runner.checks-skipped` naming the five checks
that did not run — a short report is never a clean tree. The third is a usage
error: message on stderr, **nothing on stdout**.

**4. The JSON document is stable.**

```bash
PYTHONPATH=src python -m lmer_schema.cli validate "$SCRATCH/plexus-schema" --json \
  | python -c 'import json,sys; d=json.load(sys.stdin); print(d["form"], d["exit_code"], d["counts"])'
```

On the v2 runtime: `schema 0 {'error': 0, 'warn': 1, 'info': 3}`. Every finding carries exactly
`code`, `severity`, `path`, `message`, `detail`; `path` is repo-relative and
never absolute.

**5. Your own tree, before you touch it.**

```bash
PYTHONPATH=src python -m lmer_schema.cli validate "$LMER_WORK_REPO_PATH" --json \
  | python -c '
import json,sys
from collections import Counter
d = json.load(sys.stdin)
print(d["form"], d["exit_code"], d["counts"])
for (sev, code), n in sorted(Counter((f["severity"], f["code"]) for f in d["findings"]).items()):
    print(f"{sev:6} {code:38} {n}")'
```

The list to work down before migrating, not a gate. Note that
`agents.memory-frontmatter` is a **shape** finding — an ERROR in legacy form
too, because a memory with no `description:` is broken wherever it lives.

**6. The suite.** `gate-check` runs it. The gate has a text-diff fast path that
narrows a prose-only change to a declared `tests.text_diff_subset`. This repo
declares one (`.lmer/gate-check.yaml`), so a docs-only branch runs that subset,
not the whole suite. The subset lists `tests/test_lmer_schema_cli.py`, the
two-directional check that `--list-checks` and SCHEMA.md's check-code table
agree.

**7. The index blocks are fresh.**

```bash
PYTHONPATH=src python -m lmer_schema.cli index "$TREE"; echo "exit=$?"       # 0
```

- `lmer-schema index $TREE` prints `unchanged` for every file and exits `0`;
  a `create` or `update` is a block the next push would rewrite anyway
  (`--write` applies it now), and a `refused` line names a README a human has
  to fix (the reason is on the line).
- Validate a clean clone: the validator and the dry run both render what is
  on disk without consulting git, so a checkout holding unpushed records may
  report `index.readme-stale` where a clean clone of the same repository is
  silent.

## See also

- [SCHEMA.md](./SCHEMA.md) — the contract: concepts, areas, manifest,
  [inventory](./SCHEMA.md#inventory), [check codes](./SCHEMA.md#check-codes).
- [LMER-CLI.md](./LMER-CLI.md#validating-a-plexus-lmer-schema-validate) — the
  validator's flags, outputs and exit codes; `work sessions` and resume.
- [SCHEMA-V2.md](./SCHEMA-V2.md) — execution identities, continuation, shared
  release claims, memory retirement and explicit version cutover.
- [RUN-STATE.md](./RUN-STATE.md) — what is *in* a session directory.
