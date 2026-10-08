# The lmer schema (versions 1 and 2)

Schema 2 is current. [Schema 2](./SCHEMA-V2.md) defines its changed writer,
identity, session and retirement contracts. The layout and schema-1 behavior
below remain the baseline; explicit schema-2 rules take precedence for a
`schema: 2` tree. Existing schema-1 trees retain their original semantics.

The lmer schema is the normative layout of everything lmer reads and writes
outside the code repositories it works on: run records, specifications, plans,
agent memory, knowledge pages, scratch notes, task definitions and the agent
runtime surface. One repository holding this layout is a **plexus**. This
document is the contract a plexus conforms to; the `lmer-schema validate`
command (see [Validator](#validator)) is its executable form. What lmer's
older documentation calls "the work repo" is a plexus in its legacy form
(see [Legacy form](#legacy-form)).

The key words MUST, MUST NOT, REQUIRED, SHALL, SHOULD, SHOULD NOT, RECOMMENDED,
MAY and OPTIONAL in this document are to be interpreted as described in
RFC 2119. Sections marked *informative* carry no requirements.

The schema is versioned by a single integer (`schema:` in the manifest). This
document describes schema `1` and the process by which the schema changes. It
is not a final form.

## Table of Contents

- [Scope and conformance](#scope-and-conformance)
- [Concepts](#concepts)
- [Layout](#layout)
- [Ownership namespaces](#ownership-namespaces)
- [Areas](#areas)
  - [napkin](#napkin)
  - [plans](#plans)
  - [knowledge](#knowledge)
  - [memory](#memory)
  - [record](#record)
  - [agents](#agents)
  - [Supporting surfaces](#supporting-surfaces)
- [The manifest: `_plexus.yaml`](#the-manifest-_plexusyaml) — top-level keys,
  [the registry](#the-registry),
  [project resolution and host buckets](#project-resolution-and-host-buckets),
  [sources](#sources), [housekeeping policy](#housekeeping-policy)
- [Index and write contracts](#index-and-write-contracts)
- [Archive and housekeeping](#archive-and-housekeeping)
- [Sessions and runs](#sessions-and-runs)
- [Validator](#validator)
- [Inventory](#inventory) — every schema path: pattern, writer, shape,
  optionality, tier; [index lines](#index-lines),
  [check codes](#check-codes)
- [Versioning and change process](#versioning-and-change-process) —
  [version floor](#version-floor), [change process](#change-process)
- [Legacy form](#legacy-form)
- [Host buckets](#host-buckets) — the documented holding form outside the schema
- [Appendix A: Legacy mapping (informative)](#appendix-a-legacy-mapping-informative)

## Scope and conformance

The schema covers the plexus repository and every repository mounted into it
as an area (see [Sources](#sources)); not the code repositories lmer clones as
targets, nor lmer's own configuration outside the plexus.

There is exactly **one** schema and **one** form. A plexus conforms to schema 1
when:

1. its root carries a `_plexus.yaml` manifest declaring `schema: 1`, and
2. `lmer-schema validate` reports no findings at ERROR severity.

Warnings and informational findings never affect conformance. A tree without a
manifest is a **legacy tree**: it is recognised, read and validated leniently,
but it does not conform to the schema and is not claimed to.

Consumers of a plexus — lmer itself, hosted connectors, and any other tool —
MUST write only shapes defined in this document, and MUST NOT depend on any
layout property this document does not state.

## Concepts

- **Plexus** — one repository instance of this schema. A person or organisation
  runs any number. The RECOMMENDED default repository name is `lmer-plexus`.
- **Project** — a flat top-level directory named by **short name**, holding
  that project's per-project areas. A project's identity is its registry entry
  (see [The registry](#the-registry)), never its path.
- **Short name** — a project directory name. Short names MUST match
  `[a-z0-9][a-z0-9_-]*`: lowercase, no dots, no slashes, no leading `_` or `-`.
  The absence of dots is what distinguishes a project (`widgets/`) from a host
  bucket (`git.example.com/`) at a glance and in code.
- **Host bucket** — a top-level `{host}/{project}/` tree holding the work of a
  repository nobody has named yet. A bucket is recognised by the dot in its
  top-level directory name. The host-bucket path is **not schema form**; it is
  the documented holding state for unnamed work (see
  [Host buckets](#host-buckets)).
- **Project-shaped directory** — a directory that directly contains any schema
  area (`_runs/`, `_plans/`, `_agents/`, `_info/`, `_specs/`, `_taskdef/`) or
  any legacy area (`runs/`, `specs/`, `info/`, `memory/`). Tools MUST use this
  marker test, not path depth, to tell a project from a namespace level inside
  a host bucket.
- **Area** — a concern with one role and one writer discipline (see
  [Areas](#areas)).
- **Ownership namespace** — names beginning with `_` belong to the schema;
  every other name is content (see [Ownership namespaces](#ownership-namespaces)).
- **Session** — one launch of lmer against one target; in schema form the
  unit of record (see [Sessions and runs](#sessions-and-runs)).
- **Legacy** — bare-named areas (`runs/`, `memory/`, `napkin/`, a standalone
  `sources.yaml`) and the `{host}/{project}/` placement of projects (see
  [Legacy form](#legacy-form)).

## Layout

```
_plexus.yaml              # the manifest: schema int, name, registry, sources, policy
_knowledge/               # instance wiki, org/topic directories
_napkin/                  # transient scratch, org/topic directories
_taskdef/                 # instance-level task definitions
_agents/                  # the agent runtime surface, instance scope
  claude/                 #   per-harness agent config (skills, commands, hooks)
  mirrors/                #   vendored agent tooling (opaque)
  memory/                 #   instance-scope memory + MEMORY.md index
_archive/                 # archive mirror (relative paths preserved)
widgets/                  # a project, by short name
  _runs/<slug>/           #   grouping by (taskdef, target); nothing is written here
    <session-id>[--<name>]/ #   one launch's complete record (state files as today)
      masterplan/         #     its orchestration bundle, flat
  _specs/                 #   dated symlink index into session dirs
  _plans/                 #   plan files, run-plan symlinks, PLANS.md index
  _agents/                #   the runtime surface, project scope
    memory/               #     project-scope memory + MEMORY.md index
  _info/                  #   project context (info pages, gate-check.yaml)
  _taskdef/               #   project-scoped task definition overrides
gadgets/                  # another project, same shape
capacity-planning/        # a project with no repository
git.example.com/acme/…    # host bucket: an UNNAMED repository's work (not schema form)
README.md                 # content namespace (human); MAY carry the generated index block (Inventory)
```

Every directory in the tree above is OPTIONAL; the only required entry is the
`_plexus.yaml` file. A plexus with one registry entry and a plexus with fifty
are the same form; there is no other.

## Ownership namespaces

- A top-level or in-project name beginning with `_` is **reserved for the
  schema**, in this version and every later one. Tools and humans MUST NOT
  create `_`-prefixed names the schema does not define; the validator reports
  an unknown `_` name as a warning.
- Every schema-owned directory carries the `_` prefix. This is what lets a
  reader tell at a glance which parts of a tree have migrated, and it
  guarantees a project short name can never collide with a schema directory.
- Everything else is the **content namespace**: project directories, org and
  topic directories inside `_knowledge/` and `_napkin/`, and human files such
  as `README.md`. Content-namespace files are never validated for shape. The
  one exception is the generated index block a `README.md` at the plexus root
  or at a project root may carry between `<!-- lmer:index:start -->` and
  `<!-- lmer:index:end -->`: tooling-owned, checked for freshness only; the
  text outside the markers stays content and is never checked, except that a
  line that git would read as a conflict marker keeps the generator away from
  the file.
- Schema-owned names are lowercase. Capitalised variants (`_Runs/`,
  `_Plans/`) are not schema names and MUST NOT be used; case-insensitive
  filesystems make them indistinguishable from the real name.

## Areas

Each area has one role and one writer discipline. "Writer" below names who is
permitted to create or change files in the area; a writer MUST NOT write
outside its own area's rules.

### napkin

`_napkin/`, instance level, org/topic directories.

- **Role:** transient scratch — ideas, problem discussions, remember-for-later.
- **Writers:** humans and agents, free-form.
- **Discipline:** content is never authoritative and is never validated. A
  napkin note leaves the area by **promotion** (its content becomes a plan or a
  knowledge page) or by **decay** (housekeeping archives it under the
  manifest's TTL policy). Tools MUST NOT treat napkin content as a source of
  truth.
- MAY be mounted from its own repository (see [Sources](#sources)).

### plans

`<project>/_plans/`, per project.

- **Role:** work intent for one project.
- **Contents:**
  - **Plan files** — one markdown file per plan, kebab-case slug filename
    (`<slug>.md`).
  - **Run-plan symlinks** — one dated relative symlink per active run plan,
    `YYYY-MM-DD-<runlabel>--plan.md -> ../_runs/<slug>/<session-id>[--<name>]/plan.md`.
    The target is the session directory's current name; a named session's
    directory carries its `--<name>` suffix. `<runlabel>` is the record's
    label, `<name-or-slug>-<session-id>` — the grammar `_specs/` uses, for the
    same reason: each of a run's launches keeps its own entry.
  - **`PLANS.md`** — the index: one line per plan (title, state, pointer).
- **Writers:** lmer tooling and hosted connectors for plan files and the index;
  lmer tooling for run-plan symlinks. No shipped lmer verb writes into
  `_plans/` at schema 1 — the area is specified so that a connector's writes
  and the validator's reads agree from the first one.
- **Discipline:** archiving or forgetting a plan MUST tombstone its `PLANS.md`
  line rather than delete it silently. Symlinks MUST be relative and MUST NOT
  escape the plexus. Last-writer-wins on the index is acceptable at schema 1.

### knowledge

`_knowledge/`, instance level, org/topic directories.

- **Role:** the durable wiki — how things are done, for humans and agents.
- **Writers:** humans and agents; fed by promoted napkin notes and distilled
  run output.
- **Discipline:** pages are free-form markdown and are never validated for
  shape. A per-org `INDEX.md` is RECOMMENDED and not required at schema 1.
- MAY be mounted from its own repository (see [Sources](#sources)).

### memory

Agent-recalled facts. Memory exists at **two scopes**, and a memory's type
selects its scope:

| `metadata.type` | scope | directory | holds |
|---|---|---|---|
| `user` | instance | `_agents/memory/` | who the operator is — true in every project |
| `feedback` | instance | `_agents/memory/` | how the operator wants agents to work |
| `reference` | instance | `_agents/memory/` | external pointers: dashboards, tickets, docs |
| `project` | project | `<project>/_agents/memory/` | this project's work, goals and constraints |

- **Shape:** one markdown file per memory, with YAML frontmatter. `name`,
  `description` and `metadata.type` (one of `user`, `feedback`, `project`,
  `reference`) are REQUIRED. Additional `metadata` keys written by a harness
  (for example `node_type`, `originSessionId`, `modified`) are PERMITTED and
  are never a finding, not even a warning: as with the manifest, the
  vocabulary may grow without invalidating files written before it did. Each
  scope keeps its own `MEMORY.md` index, one line per memory.
- **Writers:** `work memory persist` (routes each file by its frontmatter type)
  and hosted connectors' `remember`/`forget` operations.
- **Discipline:** a writer MUST place a memory in the scope its type selects.
  **Recall is the union** of both scopes, project overlaying instance; on a
  `name` collision the project-scope file wins. Because recall unions both
  scopes, a misplaced memory loses nothing and is a hygiene finding, not an
  error. Forgetting a memory MUST tombstone its index line.
- **A writer MUST merge, never mirror.** A scope is shared: several sessions
  hold their own view of it at once, and none of them sees the whole. A writer
  may therefore delete only what it was given and has since removed — never a
  file it simply does not have — and MUST apply the same rule to the index, so
  a session can neither drop an entry it never held nor restore one another
  session retired. A writer that cannot tell which files it was given MUST
  delete nothing. Mirroring a scope from one session's directory is how a
  session deletes work it never knew about.

### record

`<project>/_runs/`, `<project>/_specs/` and the state files inside a session
directory.

- **Role:** the durable record of every launch — state, events, ledger, logs,
  reports, and the orchestration artifacts (spec, goals, plan, retro).
- **Writers:** the `work` CLI, through its existing single-writer contract for
  each state file ([RUN-STATE.md](./RUN-STATE.md)).
- **Discipline:** the session is the unit of record. Schema 1 changes the
  **location** of the run record only: every file shape in RUN-STATE.md is
  unchanged, and what that document calls "the run dir" is, in schema form,
  the session directory (see [Sessions and runs](#sessions-and-runs)).

### agents

`_agents/`, at instance scope and, where a project needs its own, at project
scope. It holds what the agent runtime consumes or produces **about itself**:
its configuration and its accumulated self-knowledge.

- **`_agents/<harness>/`** — per-harness agent configuration (skills,
  commands, hooks). The first level is the harness name; `claude` is the only
  harness defined at schema 1. `memory` and `mirrors` are reserved and MUST
  NOT be used as harness names.
- **`_agents/mirrors/`** — vendored forks of agent tooling. Mirror contents are
  **opaque**: a mirror is a foreign writer's tree and the validator checks no
  further than its existence.
- **`_agents/memory/`** — the memory scope at that level (see
  [memory](#memory)).

`_taskdef/` deliberately lives **outside** `_agents/`: a task definition is a
work contract handed to an agent, operator-authored and independently
mountable, not runtime plumbing.

### Supporting surfaces

- **`_taskdef/`** — task definitions. At instance level the shared set; at
  `<project>/_taskdef/` project-scoped overrides. Shape is the existing
  task-definition contract ([TASKDEFS.md](./TASKDEFS.md)). MAY be mounted from
  its own repository (see [Sources](#sources)).
- **`<project>/_info/`** — project context: free-form info pages and the
  project's `gate-check.yaml`. Prose is unchecked; `gate-check.yaml` keeps its
  existing shape.

## The manifest: `_plexus.yaml`

The manifest is a YAML file at the plexus root. Its presence is what makes a
tree schema form; its absence makes a tree legacy. The registry inside it is
the **only** source of project identity.

```yaml
schema: 1                       # integer; the lmer schema version
name: acme                      # instance name
projects:                       # the registry: identity lives here, not in paths
  widgets:
    repo: git.example.com/acme/widgets   # the primary remote: cloned and pushed to by default
    remotes:                             # additional remotes; the primary is never repeated
      - github.com/acme/widgets
  gadgets:
    repo: github.com/acme/gadgets        # single-remote project: one line
  capacity-planning:                     # a project with no repository
    description: Quarterly capacity planning workstream
sources:                        # area mounts (optional)
  taskdef: {repo: "https://git.example.com/acme/taskdefs.git", ref: main}
  napkin:  {repo: "https://git.example.com/acme/napkin.git"}
housekeeping:                   # per-area TTL policy (optional; absent = defaults)
  napkin: {archive_after: 365d}
  plans:  {archive_after: null}          # null = never auto-archive
```

### Top-level keys

| key | required | shape | meaning |
|---|---|---|---|
| `schema` | yes | integer | the lmer schema version this tree conforms to; `1` for this document |
| `name` | yes | string | the instance name |
| `projects` | no | mapping | the registry, short name to entry (see below); absent means no registered projects |
| `sources` | no | mapping | area mounts (see [Sources](#sources)) |
| `housekeeping` | no | mapping | per-area TTL policy (see [Housekeeping policy](#housekeeping-policy)) |

- `schema` MUST be an integer. A boolean or string is a parse error.
- Unknown top-level keys, and unknown keys inside any section, are
  **warnings**, never errors: schema-owned vocabulary may grow, and a manifest
  written for a later schema 1 tool remains readable by an earlier one.
- A `schema` value **newer** than the tool understands: tools MUST refuse to
  write to the tree, and the validator reports an error
  (`manifest.schema-newer`) and stops — a tree written by a newer schema is
  unusable input, not a tree with findings. An integer that is neither newer
  nor supported is the same refusal (`manifest.schema-unsupported`).
- A **supported** value older than the newest the tool knows: tools read and
  write normally, and the validator reports a warning naming the newer version
  available (`manifest.schema-older`). **At schema 1 that warning cannot
  fire**: the supported set is exactly `1`, so every integer other than `1` is
  one of the two refusals above. The code stays registered — it is the older
  half of a rule this document states in both directions — and
  `--list-checks` marks it unreachable until a schema 2 ships.

### The registry

`projects:` maps each **short name** to an entry. The directory `<short-name>/`
at the plexus root is that project's tree.

| entry key | required | shape | meaning |
|---|---|---|---|
| `repo` | no | normalized remote | the **primary** remote: the one lmer clones, and pushes to by default, when launched by project name alone |
| `remotes` | no | list of normalized remotes | **additional** remotes (mirrors, forks) that resolve to this project |
| `description` | no | string | what the project is for; RECOMMENDED for repoless projects |

Identity rules:

- Short names MUST match `[a-z0-9][a-z0-9_-]*`.
- A registry entry SHOULD have a matching directory, and every project-shaped
  top-level directory without a dot in its name SHOULD have a registry entry;
  the validator reports both mismatches.
- **The primary is named, never positional.** `repo:` is the primary; `remotes:`
  lists only additional remotes. `repo:` MUST NOT be repeated inside `remotes:`.
  `remotes:` without `repo:` is an error (there is nothing to clone). A
  single-remote project is one `repo:` line and no list.
- **Zero, one or many remotes are one rule at three counts.** Both keys are
  OPTIONAL: a project is a short name with a directory, and a repository
  binding is something it MAY have. A repoless project (a client engagement,
  an ops workstream) holds plans, runs, memory and info exactly like a
  repo-backed one; it is addressed by name and never matches a remote lookup.
- **A remote binds to at most one project.** The same normalized remote in two
  entries, or twice within one entry, is an error; one repository mapping to
  many projects would make resolution non-deterministic.
- `remotes: []` beside `repo:` is a warning. A non-canonical spelling (scheme,
  userinfo, `.git` suffix, trailing `/`) is a warning: tools always emit
  canonical form, so a drifting hand edit is worth a nudge.
- The schema names the primary; it is not the safety net. lmer's push
  allow-list and an explicit repository URL on launch (which overrides `repo:`)
  remain the guard against pushing to the wrong remote.

**Remote normalization.** Every `repo:` and `remotes:` value, and every URL a
session looks up against them, is compared in one normalized form: host
lowercased; scheme and userinfo dropped; default ports (443, 22) dropped but a
non-default port kept as `host:port`, since a different port can be a
different repository; `git@host:path` and `ssh://git@host/path` folded to
`host/path`; trailing `.git` and `/` stripped; path case preserved as written
and compared case-insensitively. Thus `https://git.example.com/acme/widgets.git`
and `git@git.example.com:acme/widgets` are both `git.example.com/acme/widgets`,
while `ssh://git@git.example.com:2222/acme/widgets` is the distinct key
`git.example.com:2222/acme/widgets`. A normalized value splits into host and
project path at its first `/`; there are no separate `host`/`project` scalars.
The port belongs to the KEY and to nothing else: a [host bucket](#host-buckets)
names its directory with the hostname alone, because that is the directory
lmer already writes for that remote.

### Project resolution and host buckets

A session resolves its project directory by normalizing its target
repository URL and looking it up across every entry's `repo:` and `remotes:`.
A match resolves to `<short-name>/`, so a multi-remote project lands in one
tree with no symlinks. Symlinks remain OPTIONAL finding aids (for example a
compatibility link from a project's old `{host}/{project}` path to its short
name during migration).

**Unregistered repositories are never auto-named.** When no entry matches, the
work goes to `{host}/{project}/` — a **host bucket** — and stays there,
visibly unnamed, until a human names it. The bucket is documented so that its
existence is expected rather than surprising, but it is not a shape the schema
blesses: [Host buckets](#host-buckets) states the holding form, what tools
MUST and MUST NOT do with it, and how a tree leaves it by adoption.

A registered remote whose `{host}/{project}/` path still exists as a real
directory is an error (a split tree); the same path as a symlink is the
compatibility link and is informational.

### Sources

`sources:` declares which areas are mounted from their own repositories. It
folds in the standalone `sources.yaml` of the legacy form: same shape, same
rules, same resolution against the environment overrides (see
[Canonical Source Declarations](./LMER-CLI.md#canonical-source-declarations-sourcesyaml)).

| key | shape | meaning |
|---|---|---|
| `taskdef` | `{repo, ref?}` | the shared task-definition repository |
| `napkin` | `{repo}` | the napkin repository |
| `knowledge`, `plans` | `{repo}` | reserved: mountable the same way when needed |

- **The key set is the name's, not the section's.** `ref:` is `taskdef`'s
  alone — there is no `LMER_NAPKIN_REF` and nothing pins any other area to a
  ref — so a `ref:` under any other name is an unknown key in `_plexus.yaml`
  (`manifest.unknown-key`, WARN) and a refuse-start error in a standalone
  `sources.yaml`.
- A mounted repository's **root is the area**: a napkin repository's root is
  the contents of `_napkin/`, and its archive is `_archive/` at that root.
- **Same-host guard.** A `sources.*.repo` on a different host than the plexus
  repository itself is an error, because a declared source inherits the plexus
  credential at clone time; the environment override for that source remains
  the operator-controlled escape hatch. Declarations MUST NOT contain
  credentials.
- **Precedence:** environment overrides, then `_plexus.yaml sources:`, then a
  legacy standalone `sources.yaml` (recognised with a warning; retires when
  the tree migrates).

### Housekeeping policy

`housekeeping:` maps an area name (`napkin`, `plans`, and any other area the
housekeeping tool documents) to its policy:

| key | shape | meaning |
|---|---|---|
| `archive_after` | duration `<N>d`, or `null` | content untouched for longer than `<N>` days is archived; `null` means never auto-archive |

An absent section or area means the housekeeping tool's documented defaults
apply. Policy governs archiving only; nothing in it can cause deletion.

## Index and write contracts

These rules bind every writer — lmer tooling and hosted connectors alike:

- `remember(text, type)` creates one memory file with the frontmatter of
  [memory](#memory) plus one `MEMORY.md` line, in the scope the type selects.
  `recall` reads both scopes, project overlaying instance.
- `save_plan(title, body)` creates `<project>/_plans/<slug>.md` plus a
  `PLANS.md` entry. `archive_plan` and `forget` move the file to the
  `_archive/` mirror and tombstone the index line.
- Every tool write to a plexus is one git commit. A hosted service commits as
  its own bot identity.
- Index files (`MEMORY.md`, `PLANS.md`) are last-writer-wins at schema 1. The
  generated `README.md` index blocks are not: they are re-rendered from the
  records (project) or the manifest (root), so concurrent writers converge,
  and a rebase conflict confined to them is resolved by rendering again from
  the merged tree ([WORK-REPO-SAFETY.md](./WORK-REPO-SAFETY.md)).
  Every schema-defined symlink MUST be relative and MUST resolve inside the
  plexus.
- A bare `git clone` of any conforming plexus is readable markdown throughout
  and passes `lmer-schema validate` with no errors.

### Tool writes

The registry writer (`lmer_schema.writer`) is the ONE tool that rewrites
`_plexus.yaml`. It writes canonical remotes (the same normalizer resolution
uses), checks the registry's invariants BEFORE anything reaches disk (a
non-canonical spelling is a WARN, a remote bound twice is an error, and an
unusable manifest is refused with the file byte-identical), and replaces the
file atomically. Comments, blank lines, quoting and indentation are preserved
by a ruamel.yaml round trip, with the exact comment behaviour decided in
[DEVELOPMENT.md](./DEVELOPMENT.md) (a rewritten scalar keeps its end-of-line
comment on its original column; `null` spelling is normalized). Hand edits stay
legal — the writer is a convenience, not a gate — and `writer.create()` is the
one call that flips a tree's form, by adding the manifest.

## Archive and housekeeping

- `_archive/` exists at instance level and, as needed, per project tree.
  Archived content keeps its relative path: `_napkin/acme/idea.md` archives to
  `_archive/_napkin/acme/idea.md`.
- **Housekeeping never deletes.** Every archive move emits an event; content
  remains in git history and in the mirror.
- There is **one archive home** per tree. An archived session directory is
  relocated to `<project>/_archive/_runs/<slug>/<session-id>[--<name>]/`, the
  same mirror rule every other area uses; the archiving semantics (a complete
  session, or a stale one past the threshold) are the existing cleaner
  contract of [RUN-STATE.md](./RUN-STATE.md). Legacy `runs/archive/` is
  recognised in legacy trees and is never required to move.
- The housekeeping tool applies the manifest's TTL policy — napkin decay,
  done-plan retirement, index tombstone sweeps — and lists host buckets
  awaiting adoption without ever adopting them. Its default mode is a dry run.
  Scheduling is the operator's concern.

## Sessions and runs

The **session** is the unit of record. A launch's record is complete and
self-contained; nothing about it is shared with any other launch.

- `<project>/_runs/<slug>/` groups every launch against one (taskdef, target)
  pair. It is a grouping folder: tools MUST NOT write anything at that level,
  and the validator warns on files or non-session directories found there.
- A new session is seeded in a temporary `<project>/_runs/.new-<session-id>-*/`
  directory and moved to its canonical name by one atomic rename (the run-dir
  lifecycle of [RUN-STATE.md](./RUN-STATE.md)). The seed directory is
  transient tooling state; one left behind by a crash is a warning until the
  cleaner sweeps it.
- `<project>/_runs/<slug>/<session-id>[--<name>]/` is **one launch's complete
  record**: `state.yaml`, `events.jsonl`, `ledger.yaml`, `release.yaml`,
  `log.yaml`, `goals.md`, `plan.md`, `plan.index.json`, `reports/` and
  artifacts, with exactly the shapes documented in
  [RUN-STATE.md](./RUN-STATE.md).
- **`<session-id>`** is the launcher's session id (`LMER_SESSION_ID`): a UTC
  timestamp `YYYYMMDD-HHMMSS`, a process id and a random suffix — four
  `-`-joined all-digit fields matching `\d{8}-\d{6}-\d+-\d+` (for example
  `20260824-165230-2-31245`). Timestamp-first means `ls _runs/<slug>/` lists
  launches in start order. (Under schema 2 the id is an opaque UID and carries
  no order — [SCHEMA-V2.md](./SCHEMA-V2.md); the generated
  `<project>/README.md` block is the ordered view.) The id is minted by the
  launch that OPENS the record; a launch that continues an existing record
  carries that record's id instead of minting one (see **Resume** below), so
  the timestamp and pid in a directory name are always the launch that opened
  it — which is what keeps that listing in start order however often a record
  is resumed. A session dir SHOULD carry a `state.yaml`; a session-id not
  matching this pattern is a warning.
- **`--<name>`** is appended to the directory name when the session is named
  at its pre-execution freeze; the rename happens at most once. Index symlinks
  are re-pointed by the tool that renames.
- **Independence and single writer.** Two launches on one target are two
  directories that never touch each other; there is nothing to merge, and a
  session writes only its own directory. The `owner` field in `state.yaml`
  records which harness process is live there — lineage bookkeeping, not
  arbitration.
- **Resume** continues a specific session directory: the continuing launch
  writes in that directory rather than opening a record of its own, and each
  new harness id is appended to the `sessions:` chain in its `state.yaml`, so a
  session dir is a lineage across process deaths. A launch the host addressed
  by id (`lmer --session <id>`, which is how the platform resumes and how it
  delivers an answer) writes in the record that id names and gets no second
  directory created beside it — including when that record carries a
  `--<name>`, where the bare `<session-id>/` would otherwise appear as an empty
  twin of the run. When no record answers to the id yet, it names the one the
  launch creates, exactly as a freshly minted id does. A directory that is
  `in-progress` with no live process is **orphaned**: listable, resumable,
  never auto-ended.
- `_specs/` and `_plans/` index into session directories with dated relative
  symlinks (`YYYY-MM-DD-<runlabel>--<artifact>.md`). `<runlabel>` names the
  **record**, not the run: the launch's name when it has one, else the run's
  slug, followed by `-<session-id>`. That is what guarantees **one entry per
  (record, artifact)** — two launches against one target are two complete
  records, so a run-keyed label would name both launches' entries identically
  and the second launch's registration would silently unlink the first
  launch's spec. A legacy tree's shared `runs/<slug>/` is its target's only
  record, so there the label is the name or slug alone.
- **Orchestration bundle.** A session's masterplan bundle lives flat at
  `<session-dir>/masterplan/`, at most one per session dir. The well-known
  artifacts (spec, goals, plan, retro) keep session-root symlinks; bundle
  internals belong to a foreign writer and are validated as opaque.
- **Legacy** `runs/<slug>/` directories (one shared directory per target) are
  read in place and never restructured by schema 1 tooling.

The `state.yaml` lineage fields (the `sessions:` chain) are specified in
[RUN-STATE.md](./RUN-STATE.md); the listing and resume command surface is
specified in the CLI reference,
[LMER-CLI.md](./LMER-CLI.md#sessions-and-resume-work-sessions).

## Validator

`lmer-schema validate <path>` is the executable form of this document. It has
a human-readable text output and a machine-readable JSON output, and an
importable core.

**Exit codes** are the validator's own: `0` clean (warnings and informational
findings allowed), `1` one or more errors, `2` unusable input — no such path, a
`_plexus.yaml` that cannot be read, or a `schema:` this lmer does not support
(see [Top-level keys](#top-level-keys)). A misuse of the command is `2` as
well: the exact command surface is the CLI reference's,
[LMER-CLI.md](./LMER-CLI.md#validating-a-plexus-lmer-schema-validate).

**Severities.** Every finding is one of ERROR, WARN or INFO. Errors are the
only findings that affect conformance; **warnings never block**.

**Strictness tiers.** The [Inventory](#inventory) assigns every schema path
to one of these tiers, row by row; the validator implements the rows.

- **Machine-owned** (`_*` directories, state files, indexes, the manifest):
  shape violations are errors; an unknown file is never worse than a warning
  (`_agents/memory/` is the surface that names them today).
- **Free-form** (napkin bodies, knowledge pages, info prose, plan bodies):
  never checked.
- **Opaque** (`_agents/mirrors/` contents, orchestration bundle internals): a
  foreign writer's tree, checked no further than its existence.
- **Legacy tier.** A tree with no manifest is recognised as legacy
  (informational). State-file **shapes** are still checked wherever they live
  — a corrupt `state.yaml` is broken in any form — but integrity findings that
  are errors in schema form are downgraded: dangling index symlinks and index
  entries without a file are warnings; a memory whose type does not match its
  scope, and a standalone legacy `sources.yaml`, are informational.
  [Check codes](#check-codes) lists the five codes this affects and nothing
  else is tier-sensitive. In a legacy tree the validator's output is the list
  of things to fix before adopting the schema, not a gate.

**What is checked at schema 1**, each at the severity stated in the section
that defines it: the manifest (parse, `schema` integer and version direction,
short-name charset, every registry rule, sources same-host guard and
precedence); registry-to-directory consistency and bucket/registry collisions;
host-bucket recognition; `_agents/` shape and reserved names; memory scope
routing and index consistency at both scopes; session-unit shape under
`_runs/`; state-file schemas and `events.jsonl` line-parseability;
`_specs/`/`_plans/` symlink integrity and `PLANS.md` consistency; generated
`README.md` index block freshness; `_` reservation; archive mirror path
validity; bundle and mirror opacity.

**Out of scope:** napkin, knowledge and info content; legacy
`{task_type}/{target}/` trees (informational); orchestration bundle internals.
Host buckets are listed as informational and their contents checked at the
legacy tier (see [Host buckets](#host-buckets)); they never affect
conformance.

Every finding carries a stable machine code. [Check codes](#check-codes) is
the whole registry of them, each with the severity and tier it is registered
at; `lmer-schema --list-checks` prints exactly that list. The command
surface — flags, the text fix-list, the JSON output document — is the CLI
reference's, [LMER-CLI.md](./LMER-CLI.md#validating-a-plexus-lmer-schema-validate).

## Inventory

This section is the per-path contract: for every path the schema defines it
states the path pattern, its writer, its shape, whether it is optional and its
strictness tier. The validator implements these rows and nothing else, and
[Check codes](#check-codes) names the code every row's findings carry. A path
not listed here is not a schema path: a `_`-prefixed name the rows do not
cover is an unknown reserved name (a warning), and every other name is
content (never checked).

Column vocabulary:

- **path pattern** — relative to the plexus root. `<project>` is a project
  directory: `<short-name>/` at the root or, inside a host bucket, the
  bucket's project-shaped directory. `<session-dir>` is
  `<project>/_runs/<slug>/<session-id>[--<name>]`. `<scope>` is the agent
  surface at either level: `_agents/` or `<project>/_agents/`.
- **writer** — who may create or change the path. *operator* is a human
  editing the repository; *session* is the harness running a task, through
  the `work` verb named where one exists; *connector* is a hosted connector;
  *tooling* is lmer tooling other than a session's own record writes
  (housekeeping, migration, index sync).
- **shape** — what the validator checks. Record-file shapes live in
  [RUN-STATE.md](./RUN-STATE.md) and are not restated.
- **optional** — `required` (absence is an error), `expected` (SHOULD be
  present; absence is a warning), `optional` (absence says nothing),
  `transient` (exists only while a tool is mid-write; left behind is a
  warning).
- **tier** — the strictness tier of the [Validator](#validator), by short
  name — the same five names the validator records per check code (see
  [Check codes](#check-codes)): `machine` (machine-owned: a shape violation is
  an error, an unknown file inside is never worse than a warning), `free-form`
  (never checked; the directory name itself is still schema-owned), `opaque`
  (a foreign writer's tree, checked no further than its existence), `legacy`
  (a pre-schema name: recognised and read in place, informational, never
  required to move), `not-schema` (the documented holding form of
  [Host buckets](#host-buckets), informational — with the one split-tree
  exception the code table names).
  The `_` reservation and the symlink rules (relative, resolving inside the
  plexus) apply to every tier. In a legacy tree the legacy-tier downgrades of
  the Validator section apply to every row.

Severities named in the rows are for a tree in schema form; the legacy tier
downgrades integrity findings (dangling links, index entries without a file,
memory scope) but never shape findings.

### Instance root

| path pattern | writer | shape | optional | tier |
|---|---|---|---|---|
| `_plexus.yaml` | operator; adoption tooling writes it in canonical form | YAML mapping per [The manifest](#the-manifest-_plexusyaml): `schema` integer, `name`, `projects`, `sources`, `housekeeping` | required | machine |
| `<short-name>/` | operator or adoption tooling creates it; a session creates it on first write for a registered project | directory; name matches `[a-z0-9][a-z0-9_-]*`; SHOULD have a registry entry and a registry entry SHOULD have the directory (either mismatch is a warning) | optional | machine |
| `_knowledge/` | humans and agents | directory of `<org>/<topic>/` directories | optional | free-form |
| `_knowledge/<org>/<topic>/*.md` | humans and agents; promotion from napkin | free-form markdown | optional | free-form |
| `_knowledge/<org>/INDEX.md` | humans and agents | free-form markdown index; RECOMMENDED, never checked | optional | free-form |
| `_napkin/` | humans and agents | directory of `<org>/<topic>/` directories; subject to the housekeeping TTL | optional | free-form |
| `_napkin/<org>/<topic>/*.md` | humans and agents | free-form markdown; never authoritative | optional | free-form |
| `_taskdef/` | operator, or the root of a mounted taskdef repository | the task-definition contract of [TASKDEFS.md](./TASKDEFS.md); validated by the taskdef loader, not by `lmer-schema` | optional | opaque |
| `_agents/` | see [Agent surface](#agent-surface-both-scopes) | see Agent surface (`<scope>/` row) | see Agent surface | see Agent surface |
| `_archive/` | housekeeping tooling; connectors' `archive_plan` and `forget` | mirror: `_archive/<relative path>`. A **`_`-prefixed** first component MUST be an area of this scope — `_agents`, `_knowledge`, `_napkin`, `_taskdef` — and anything else `_`-prefixed is a broken mirror path (an error). A content-namespace first component (`README.md`, a project's short name) is mirrored content and is never a finding; archived contents are not re-validated | optional | machine |
| `README.md` | humans outside the markers; `lmer-schema index --write` inside them | free-form markdown; MAY carry one generated block rendered from `_plexus.yaml` (project list) plus a fixed legend; a block that differs from a fresh render is a warning; a block of a newer format than the tool is left alone; text outside the markers is never checked | optional | machine (the block only) |
| any other top-level name | humans | content namespace, never checked; a `_`-prefixed name is a warning; a dotless directory that is project-shaped and unregistered is a warning; a dotted directory is a host bucket | optional | free-form |

### Project tree

| path pattern | writer | shape | optional | tier |
|---|---|---|---|---|
| `<project>/_runs/` | session (`work` CLI) creates it on first launch | directory of `<slug>/` grouping directories | optional | machine |
| `<project>/_runs/<slug>/` | session (`work` CLI) creates it; nothing is written at this level | directory holding session directories only; a file or non-session directory here is a warning; a slug that is the obsolete derivation of its own target (`chat-.`, `chat-<repo>.git`) is `record.plain-session-slug` and `work migrate` folds it into the bare taskdef group | optional | machine |
| `<project>/_runs/.new-<session-id>-*/` | session (`work` CLI) | seed directory of the run-dir lifecycle in [RUN-STATE.md](./RUN-STATE.md): written, then moved to `<session-dir>` by one atomic rename | transient | machine |
| `<session-dir>/` | see [Session directory](#session-directory) | see Session directory (`<session-dir>/` row) | see Session directory | see Session directory |
| `<project>/_specs/` | tooling: the specs index sync of `work artifact`, `work session-end` and `work specs-index --rebuild` | flat directory of dated relative symlinks `YYYY-MM-DD-<runlabel>--<basename>.md` into session directories (`../_runs/<slug>/<session-dir>/…`); `<runlabel>` is the RECORD's label, `<name-or-slug>-<session-id>` (see [Sessions and runs](#sessions-and-runs)), so one entry per (record, artifact) and a second launch of one target never unlinks the first launch's entry; the `--` is reserved as the label/basename boundary and a label never contains one; no index file; dangling is an error, non-relative or escaping is an error in every form | optional | machine |
| `<project>/_plans/` | tooling and connectors | directory of plan files, run-plan symlinks and `PLANS.md` | optional | machine |
| `<project>/_plans/<slug>.md` | tooling and connectors (`save_plan`) | kebab-case slug filename; free-form markdown body | optional | free-form |
| `<project>/_plans/YYYY-MM-DD-<runlabel>--plan.md` | tooling: the run-plan link sync (no shipped lmer verb writes one at schema 1) | relative symlink to `../_runs/<slug>/<session-dir>/plan.md`, the session directory's current name (`--<name>` included once named); same integrity rules as `_specs/` | optional | machine |
| `<project>/_plans/PLANS.md` | tooling and connectors | one line per plan carrying its title, state and a relative link to the plan file (see [Index lines](#index-lines)); an entry without a file is an error, a plan file without an entry is a warning | expected when plan files exist | machine |
| `<project>/_info/` | operator | free-form info pages | optional | free-form |
| `<project>/_info/gate-check.yaml` | operator | the existing gate-check declaration ([GATE-FASTPATH.md](./GATE-FASTPATH.md)); parsed by its consumer, not by `lmer-schema` | optional | opaque |
| `<project>/_taskdef/` | operator | project-scoped task-definition overrides ([TASKDEFS.md](./TASKDEFS.md)) | optional | opaque |
| `<project>/_agents/` | see [Agent surface](#agent-surface-both-scopes) | see Agent surface (`<scope>/` row) | see Agent surface | see Agent surface |
| `<project>/_archive/` | housekeeping tooling; the session cleaner for archived sessions | mirror, same rule as the instance `_archive/` with this scope's areas (`_agents`, `_info`, `_plans`, `_runs`, `_specs`, `_taskdef`): `<project>/_archive/_runs/<slug>/<session-dir>/` for an archived session | optional | machine |
| `<project>/README.md` | humans outside the markers; the durability push of any session in the project and `lmer-schema index --write` inside them | free-form markdown; MAY carry one generated block: one row per committed run record under `_runs/`, newest first, each a pure function of its own record; absence while records exist is informational; a block that differs from a fresh render is a warning; a conflict-marker line in the hand text keeps the generator away | optional | machine (the block only) |
| any other name inside `<project>/` | humans | content namespace, never checked; a `_`-prefixed name is a warning | optional | free-form |

### Session directory

Every file below keeps exactly the shape and the single writer documented in
[RUN-STATE.md](./RUN-STATE.md); what that document calls "the run dir" is
`<session-dir>` here. Only the location moves.

| path pattern | writer | shape | optional | tier |
|---|---|---|---|---|
| `<session-dir>/` | session (`work` CLI creates and, at most once, renames it) | directory named `<session-id>[--<name>]`, session-id `YYYYMMDD-HHMMSS-<pid>-<random>`; a name not in this form is a warning | optional | machine |
| `<session-dir>/state.yaml` | `work state` (single writer) | RUN-STATE.md §2, plus the `sessions:` lineage chain; unparseable, wrong integer or enum is an error in every form | expected | machine |
| `<session-dir>/events.jsonl` | `work event` (append-only) | one JSON object per line, RUN-STATE.md §2; a line that does not parse is an error | optional | machine |
| `<session-dir>/ledger.yaml` | `work ledger set` | RUN-STATE.md `ledger.yaml` | optional | machine |
| `<session-dir>/release.yaml` | `work release` | RUN-STATE.md §7; release runs only. `lmer-schema` does not read this file at schema 1: the state files it parses are `state.yaml`, `ledger.yaml`, `plan.index.json` and `events.jsonl` | optional | machine |
| `<session-dir>/log.yaml` | `work log` | the worklog of RUN-STATE.md; like `release.yaml`, not read by `lmer-schema` at schema 1 | optional | machine |
| `<session-dir>/plan.index.json` | session; linted by `work plan check` | RUN-STATE.md `plan.index.json` | optional | machine |
| `<session-dir>/goals.md` | `work goals` (freeze and amend) | RUN-STATE.md `goals.md`: free-form content owned by its verb, not checked by `lmer-schema`; or a relative symlink into `masterplan/`, which the `<artifact>.md -> masterplan/<artifact>.md` row below owns (dangling is an error) | optional | free-form |
| `<session-dir>/spec.md`, `plan.md`, `retro.md`, `followups.md`, `plan.html` and other registered artifacts | session, registered by `work artifact` | free-form; or a relative symlink into `masterplan/`, which the `<artifact>.md -> masterplan/<artifact>.md` row below owns | optional | free-form |
| `<session-dir>/reports/` | `work report` | timestamped report files, free-form | optional | free-form |
| `<session-dir>/masterplan/` | the masterplan orchestrator (foreign writer) | flat bundle, at most one per session directory; internals opaque | optional | opaque |
| `<session-dir>/<artifact>.md -> masterplan/<artifact>.md` | tooling: `work artifact --sync`, `work commit`, `work session-end` | relative symlink for each well-known bundle artifact (`spec.md`, `goals.md`, `plan.md`, `plan.html`, `retro.md`); this row owns the symlink case of every name in that list, `goals.md` included: dangling is an error | optional | machine |

### Agent surface (both scopes)

`<scope>` is `_agents/` (instance) or `<project>/_agents/` (project). Both
scopes have the same shape; the instance scope is the normal home of harness
configuration and mirrors.

| path pattern | writer | shape | optional | tier |
|---|---|---|---|---|
| `<scope>/` | operator | directory whose first level is harness names plus the reserved `memory` and `mirrors`; an unknown first-level name is a warning; a harness named `memory` or `mirrors` is an error | optional | machine |
| `<scope>/claude/` | operator | per-harness configuration (skills, commands, hooks) in the harness's own layout; `claude` is the only harness at schema 1 | optional | opaque |
| `<scope>/mirrors/<name>/` | mirror tooling (foreign writer) | vendored agent tooling | optional | opaque |
| `<scope>/memory/` | `work memory persist`; connectors' `remember` and `forget` | directory of memory files plus `MEMORY.md` | optional | machine |
| `<scope>/memory/<name>.md` | same | markdown with YAML frontmatter `name`, `description`, `metadata.type` in `user`, `feedback`, `project`, `reference`; missing or malformed frontmatter is an error; a type that does not select this scope is a warning (informational in a legacy tree); the body is free-form | optional | machine |
| `<scope>/memory/MEMORY.md` | same | one line per memory carrying a relative link to its file (see [Index lines](#index-lines)); an entry without a file is an error (a warning in a legacy tree), a file without an entry is a warning | expected when memory files exist | machine |

### Index lines

`MEMORY.md` and `PLANS.md` are line indexes. For the validator an **entry**
is a line that carries a relative markdown link to a `.md` file, resolved
against the index's own directory; a line with no such link (a heading, prose,
or a tombstone) is never an entry and never a finding. **Tombstoning** a line
means replacing its link with the word `archived` or `forgotten` and the date,
or re-pointing the link at the `_archive/` mirror; either way the line stays
and the file's disappearance is not a dangling entry.

### Outside the schema

These paths are documented so that tools read and write them predictably;
none of them is part of the schema and none affects conformance.

| path pattern | writer | shape | optional | tier |
|---|---|---|---|---|
| `{host}/…/{project}/` (a dotted top-level directory) | session tooling on a registry miss | a host bucket: the same project shape as `<project>/`, every Project tree row applying, found by the marker test at whatever depth it sits below the host (never by depth); listed as informational "unnamed, awaiting adoption"; contents checked at the legacy tier's severities (see [Host buckets](#host-buckets)) | optional | not-schema |
| `{host}/…/{project}` as a symlink to `<short-name>/` | adoption or migration tooling | relative compatibility symlink left at a project's old path; informational. A real directory here for a registered remote is an error (a split tree) | optional | not-schema |
| `runs/`, `specs/`, `info/`, `memory/`, `napkin/`, `taskdef/`, `agent-files/`, `mirrors/`, a standalone `sources.yaml` | pre-schema lmer | the bare-named areas of [Legacy form](#legacy-form), each mapping to one row above ([Appendix A](#appendix-a-legacy-mapping-informative)); read in place in both forms. The validator names the ones it recognises — `agent-files/`, `mirrors/`, `<project>/memory/`, `runs/archive/`, and a legacy `{host}/{project}` project home — as informational, and passes over the rest (`runs/`, `specs/`, `info/`, `napkin/`, `taskdef/`) without a finding. **The one exception is a standalone `sources.yaml`:** informational in a legacy tree, but a WARN (`manifest.sources-legacy-file`) in a schema-form tree, where the manifest's `sources:` outranks it and the file should be folded in — see the [Check codes](#check-codes) table. Record-file shapes are still checked everywhere | optional | legacy |
| `runs/archive/<slug>/` | the pre-schema cleaner | legacy archive home; never required to move | optional | legacy |
| `{host}/{project}/{task_type}/{target}/` | pre-run-dir lmer | read-side fallback only; informational | optional | legacy |

### Check codes

Every finding the validator emits carries a stable machine code,
`<namespace>.<name>`. The code — not the wording of the message — is the
identifier of a rule in this document, and the table below is the whole
registry: `lmer-schema --list-checks` prints exactly these codes with
exactly these severities and tiers, and nothing outside this list is
registered. Keeping the two in step is part of the
[change process](#change-process).

- **severity** is the severity in a **schema-form** tree.
- **legacy** is the severity the same code carries under the legacy tier — in
  a tree with no manifest, and in the contents of a
  [host bucket](#host-buckets) inside a schema-form tree. It is given only for
  the five codes where the two differ; integrity findings soften, shape
  findings never do (see [Validator](#validator)).
- **tier** is the [inventory](#inventory) tier of the surface the code checks.

#### `manifest.*` — the manifest envelope

| code | severity | legacy | tier | reports |
|---|---|---|---|---|
| `manifest.absent` | INFO | | legacy | no `_plexus.yaml` at the plexus root: the tree is [legacy form](#legacy-form), recognised and read in place — never an error |
| `manifest.unparseable` | ERROR | | machine | a present `_plexus.yaml` that cannot be read or interpreted: unusable input, and never a demotion to legacy |
| `manifest.schema-not-int` | ERROR | | machine | `schema:` is missing or is not an integer (a YAML bool is not one) |
| `manifest.schema-newer` | ERROR | | machine | `schema:` is newer than this lmer understands; tools refuse to write, and the tree is unusable input |
| `manifest.schema-unsupported` | ERROR | | machine | `schema:` is an integer no version of this lmer supports |
| `manifest.schema-older` | WARN | | machine | `schema:` is a supported integer older than the newest this lmer knows. **Unreachable at schema 1** — see [Top-level keys](#top-level-keys) |
| `manifest.name-invalid` | ERROR | | machine | `name:` is missing or is not a non-empty string |
| `manifest.unknown-key` | WARN | | machine | an unknown top-level or per-entry key, ignored: the vocabulary may grow under one `schema:` int |
| `manifest.sources-repo-invalid` | ERROR | | machine | a `sources.<name>` entry with no usable `repo:` remote URL |
| `manifest.sources-credential` | ERROR | | machine | a `sources.<name>.repo` with a credential embedded in the URL |
| `manifest.sources-cross-host` | ERROR | | machine | a `sources.<name>.repo` on a different host than the plexus repo (see [Sources](#sources)); compared only when the plexus repo's own URL is known to the command |
| `manifest.sources-reserved` | WARN | | machine | a `sources:` name the schema reserves for a later version and does not mount yet |
| `manifest.sources-legacy-file` | WARN | INFO | legacy | a standalone legacy `sources.yaml`; the manifest's `sources:` outranks it once one exists |
| `manifest.housekeeping-policy-invalid` | ERROR | | machine | a `housekeeping.<area>.archive_after` that is neither a `<N>d` duration nor `null` (see [Housekeeping policy](#housekeeping-policy)) |

#### `registry.*` — the identity rules of `projects:`

| code | severity | legacy | tier | reports |
|---|---|---|---|---|
| `registry.invalid` | ERROR | | machine | the `projects:` registry breaks an identity rule of [The registry](#the-registry) and does not build — no remote resolves through it |
| `registry.non-canonical-remote` | WARN | | machine | a remote spelled with a scheme, userinfo, a trailing `.git` or a trailing `/`: the same repo, but not the form tools write |
| `registry.remotes-empty` | WARN | | machine | an empty `remotes:` list beside `repo:` — pointless, harmless |
| `registry.repoless-without-description` | WARN | | machine | a repoless project with no `description:`: legal, but nothing says what the tree is for |

#### `tree.*` — the tree shape, the `_` reservation and the archive

| code | severity | legacy | tier | reports |
|---|---|---|---|---|
| `tree.entry-without-dir` | WARN | | machine | a registry entry whose `<short-name>/` directory does not exist |
| `tree.unregistered-project` | WARN | | machine | a project-shaped top-level directory with no registry entry — an unregistered project, or a dotless host bucket (unsupported at schema 1) |
| `tree.unknown-reserved-name` | WARN | | machine | an unknown `_`-prefixed name, at either scope: `_*` belongs to the schema (see [Ownership namespaces](#ownership-namespaces)) |
| `tree.archive-mirror-path` | ERROR | | machine | an `_archive/` entry whose `_`-prefixed first component is not an area of that scope — the archive mirrors relative paths |
| `tree.archive-inside-runs` | WARN | | machine | an archive home inside `_runs/`; there is one archive home per tree |
| `tree.legacy-archive` | INFO | | legacy | the legacy `runs/archive/` home, recognised and never required to move |
| `tree.legacy-project-home` | INFO | | legacy | a legacy `{host}/{project}` project home, read in place |
| `tree.host-bucket` | INFO | | not-schema | a [host bucket](#host-buckets): an unnamed repo's work, awaiting adoption. Listed once, never raised above INFO |
| `tree.bucket-collision` | ERROR | | not-schema | a registered remote's `{host}/{project}/` path is still a real directory — a split tree, where work would land in two places. The one `not-schema` code that is an error |
| `tree.bucket-compat-link` | INFO | | not-schema | the compatibility symlink left at a project's old path after adoption |

#### `agents.*` — the agent surface and memory

Rows of [Agent surface](#agent-surface-both-scopes); the scope rules are
[memory](#memory)'s.

| code | severity | legacy | tier | reports |
|---|---|---|---|---|
| `agents.unknown-harness` | WARN | | machine | a first-level name under `_agents/` that is neither a known harness nor the reserved `memory`/`mirrors` |
| `agents.reserved-harness-name` | ERROR | | machine | the reserved `_agents/` name `memory` or `mirrors` used as something else — a harness configuration dir, or not a directory at all |
| `agents.memory-frontmatter` | ERROR | | machine | a memory file whose YAML frontmatter is missing, malformed or short of `name`, `description` or `metadata.type` |
| `agents.memory-retirement` | ERROR | | machine | schema-2 retirement state cannot be safely interpreted; no memory mutation is permitted |
| `agents.memory-type-unknown` | ERROR | | machine | a memory whose `metadata.type` is outside the four-value vocabulary |
| `agents.memory-scope-mismatch` | WARN | INFO | machine | a memory whose `metadata.type` does not select the scope it lives in; recall unions both scopes, so nothing is lost |
| `agents.memory-unknown-file` | WARN | | machine | a file in a memory directory that is not a memory or its index |
| `agents.memory-index-missing` | WARN | | machine | a memory directory holding memory files but no `MEMORY.md` index |
| `agents.memory-file-without-entry` | WARN | | machine | a memory file with no `MEMORY.md` entry |
| `agents.memory-entry-without-file` | ERROR | WARN | machine | a `MEMORY.md` entry whose linked memory file does not exist |
| `agents.legacy-surface` | INFO | | legacy | a bare `agent-files/`, `mirrors/` or `<project>/memory/` surface: recognised legacy, read in place, never required to move |

#### `record.*` — the session unit and the state files

Rows of [Session directory](#session-directory); the file shapes are
[RUN-STATE.md](./RUN-STATE.md)'s. No `record.*` code softens under the legacy
tier: a corrupt state file is broken in every form.

| code | severity | legacy | tier | reports |
|---|---|---|---|---|
| `record.state-unparseable` | ERROR | | machine | a state file (`state.yaml`, `ledger.yaml`, `plan.index.json`) that does not parse |
| `record.state-schema` | ERROR | | machine | a state file whose `schema:` is not an integer (a bool is not one) |
| `record.state-enum` | ERROR | | machine | a state file enum (run status, task status) outside its RUN-STATE.md range |
| `record.session-metadata` | ERROR | | machine | a schema-2 session dictionary is absent or its opaque UID does not match the record directory |
| `record.release-claim` | ERROR | | machine | a schema-2 shared release claim cannot be safely interpreted; arbitration must refuse |
| `record.events-line-unparseable` | ERROR | | machine | an `events.jsonl` line that is not one JSON object |
| `record.grouping-level-entry` | WARN | | machine | a file or a non-session directory at a `_runs/<slug>/` grouping level, where nothing is written |
| `record.session-dir-without-state` | WARN | | machine | a session directory with no `state.yaml`: a session SHOULD carry one |
| `record.session-id-malformed` | WARN | | machine | a session directory whose name is not `<session-id>[--<name>]` in the `LMER_SESSION_ID` form |
| `record.seed-dir-leftover` | WARN | | machine | a `.new-<session-id>-*` seed directory left behind by a crash — transient state the cleaner sweeps |
| `record.session-stale` | INFO | | machine | an `in-progress` session untouched for longer than the staleness window: possibly orphaned, resumable, never auto-ended |
| `record.masterplan-nested` | INFO | | opaque | a masterplan bundle in the pre-schema-1 nested `masterplan/<mp-slug>/` shape; reported for migration, never an error |
| `record.plain-session-slug` | ERROR | | machine | a pre-A1 plain-session leftover: the slug is the obsolete derivation of the record's own target (`chat-.`, `chat-<repo>.git`); `work migrate <project>` folds it |

#### `index.*` — the link mechanic of the indexes and the bundle, and the generated README index blocks

One set of codes for every schema-defined symlink: the `_specs/` and `_plans/`
index links and the `<session-dir>/<artifact>.md` bundle links (see
[Index lines](#index-lines)). A link is reported once — an escaping target is
never also reported dangling. The two `readme` codes are about the generated
index block a root or project `README.md` carries: the block is compared with
a fresh render by the same kernel that writes it, so the finding is exact and
clock-free, and a block of a newer format than this tool is never judged.

| code | severity | legacy | tier | reports |
|---|---|---|---|---|
| `index.link-not-relative` | ERROR | | machine | an index or bundle-artifact symlink with an absolute target: a shape violation in every form |
| `index.link-escapes` | ERROR | | machine | an index or bundle-artifact symlink resolving outside the plexus root: a shape violation in every form |
| `index.dangling-link` | ERROR | WARN | machine | a `_specs/`/`_plans/` index symlink or a `<session-dir>/<artifact>.md` bundle link whose target does not exist |
| `index.entry-without-file` | ERROR | WARN | machine | a `PLANS.md` entry whose linked plan file does not exist |
| `index.file-without-entry` | WARN | | machine | a `<project>/_plans/<slug>.md` plan file that no `PLANS.md` line carries |
| `index.readme-missing` | INFO | | machine | a project holding run records whose `README.md` carries no generated index block (`<!-- lmer:index:start -->` … `<!-- lmer:index:end -->`); the project's next durability push or `lmer-schema index --write` creates it; the detail says when a conflict-marker line in the hand text is what keeps the generator away |
| `index.readme-stale` | WARN | | machine | a `README.md` generated index block that no longer matches a fresh render of the records under `_runs/` (or, at the plexus root, of `_plexus.yaml`), whose markers are malformed, or whose hand text holds a conflict-marker line the generator refuses to stage; a block of a newer format than this tool is left alone; warnings never block |

#### `runner.*` — the run itself

These are about the validation run rather than about the tree, and they are
what keeps a short report from reading as a clean one.

| code | severity | legacy | tier | reports |
|---|---|---|---|---|
| `runner.no-such-path` | ERROR | | machine | the path to validate is not a directory: unusable input, and no check runs |
| `runner.check-failed` | ERROR | | machine | a check module raised an unexpected exception; it is reported as an error and the remaining checks still run |
| `runner.checks-skipped` | INFO | | machine | the checks that did not run because the input is unusable — a short report is never a clean tree |

## Versioning and change process

The schema is versioned by the single integer `schema:` in `_plexus.yaml`.
Schema `1` is this document.

- **Additive or loosening** changes — a new optional key, a new `_` name, a
  relaxed check — keep the integer.
- **Breaking** changes — a shape change, a location change, a tightened
  requirement that would make a conforming tree non-conforming — bump it.
- Tools MUST refuse to write under a `schema` newer than they understand (the
  existing `state.yaml` convention, applied to the tree).

### Version floor

A tree in schema form REQUIRES an lmer release that understands schema 1: the
release in which this document and `lmer-schema validate` first ship, or any
later one. Older lmer releases MUST NOT be pointed at a schema-form tree: they
do not detect the manifest, resolve projects by path only, and would write a
second, legacy-shaped record beside the schema-form one.

Cutover is of **lmer versions, not of trees**. The release that carries schema
1 reads both forms, so a fleet updates lmer first and migrates trees
afterwards, one at a time; no tree is ever required to migrate for lmer to
keep working against it (see [Legacy form](#legacy-form)).

### Change process

This document is the normative source: its [Inventory](#inventory) states,
for every schema path, the path pattern, its writer, its shape, whether it is
optional, and its strictness tier. The validator implements this document and
nothing else; a new or changed path is a new or changed inventory row.

A change to the schema is **one merge request** that edits `docs/SCHEMA.md`
(this file) to state the new contract, changes the validator — `src/lmer_schema/`,
whose check modules are `src/lmer_schema/checks/` — so that it enforces exactly
what the document now says, and carries a `changelog.d/` fragment describing
the change for users (see [changelog.d/README.md](../changelog.d/README.md)).
The document change and the validator change ship in the same merge request,
always: one without the other MUST NOT be merged.

A new or changed rule is a new or changed [inventory](#inventory) row **and** a
check code registered in the module that owns the surface, listed in
[Check codes](#check-codes) with the same severity and tier the module
registers it at. The two directions are both checked: a code the validator
registers and this document does not list, and a code this document names and
no module registers, are each a defect —
`lmer-schema --list-checks` prints the registry to diff against.
Retiring a code is a breaking change of the JSON output, so a rule that goes
away keeps its code reserved rather than reusing it.

A breaking change additionally bumps the `schema` integer in this document, in
the validator (`lmer_schema.manifest.SUPPORTED_SCHEMAS` and `CURRENT_SCHEMA`),
and in every tool that writes a manifest. Proposals start as napkin notes or
issues; there is no further governance at schema 1.

## Legacy form

A tree with no `_plexus.yaml` is a **legacy tree**. lmer detects the form by
manifest presence; in a legacy tree it resolves a project by its
`{host}/{project}/` path, reads and writes every area in place at its bare
name (`runs/`, `specs/`, `info/`, `memory/`, `napkin/`, `taskdef/`,
`agent-files/`, `mirrors/`, a standalone `sources.yaml`), and validates at the
legacy tier.

Migrating is never a prerequisite. Migration is incremental and per tree, and
it is tooling: `work adopt <host>/<project> <shortname>` gains the registry
entry and moves `{host}/{project}/` to `<short-name>/` in one commit, then
`work migrate <project>` renames the bare areas to their `_` names,
nests the run dirs and re-points the index symlinks — one commit, with an
OPTIONAL compatibility symlink at the old path. The instance surfaces migrate
independently with `work migrate --instance`; memory migrates by lifting every
non-`project`-type memory from `<project>/memory/` to `_agents/memory/`. The
underscore and the flat short name make each tree's migration state visible
in `ls`.

Because migration is per area as well as per tree, a schema-form tree may
still hold bare-named areas (`<project>/memory/` beside `<project>/_agents/`,
a root `agent-files/` or `mirrors/`). They are recognised as legacy in either
form — informational, read in place, never an unknown-file warning — and
their record-file shapes are checked like everyone else's. The
[Inventory](#inventory) lists them under "Outside the schema".

Bare area names migrate and retire; the `{host}/{project}/` path persists in
schema form as the host bucket for unnamed work, so lmer's path fallback never
goes away — a tree leaves it by being named (see [Host buckets](#host-buckets)).

## Host buckets

A **host bucket** is the `{host}/{project}/` tree that holds the work of a
repository no registry entry names. It is documented so that every tool can
read and write it predictably, and it is explicitly **not part of the
schema**: a plexus's conformance says nothing about its buckets, no schema
version will ever require a bucket to move, and the validator never raises a
bucket above INFO. A bucket leaves the tree only by adoption.

- **Where it is.** A bucket starts at a top-level directory whose name
  contains a dot — the **hostname of the normalized remote, without any
  port** — and follows the remote's path: `git.example.com/acme/widgets/`
  for the remote `git.example.com/acme/widgets`, and the same directory for
  `ssh://git@git.example.com:2222/acme/widgets`, whose key keeps the port
  (see [The registry](#the-registry)) while its directory does not. The host
  directory is the hostname lmer already writes for that remote; a bucket
  spelled with a port would name a directory nothing wrote. Intermediate
  levels (`acme/`) are namespace levels, not projects. The project begins at
  the project-shaped directory (see [Concepts](#concepts)): tools MUST use
  this marker test, not path depth, to tell a project from a namespace level
  inside a host bucket, descending until the first project-shaped directory
  at whatever depth it sits — the deepest subgroup tree resolves like the
  shallowest. A dotless host (`localhost`, a bare `gitlab`) is not supported
  as a bucket at schema 1: the validator warns on an unregistered dotless
  top-level directory, and the fix is to register the project.
- **What is inside.** The project shape inside a bucket is the same as inside
  `<short-name>/`: every Project tree row of the [Inventory](#inventory)
  applies with `<project>` being the bucket's project-shaped directory. The
  form of the tree decides which names a session writes: in schema form it
  writes `_runs/<slug>/<session-id>/`, `_specs/`, `_agents/memory/` and the
  other `_` areas into the bucket exactly as into a named project. In a
  legacy tree the same path is not a bucket at all but the project's only
  home, holding bare-named areas (see [Legacy form](#legacy-form)).
- **What tools do.** Resolution is registry first, bucket path second. On a
  registry miss a session MUST write to the bucket, MUST NOT derive a short
  name, MUST NOT write a registry entry, and MUST NOT refuse or lose the
  work. When a remote has both a registry entry and a real bucket directory,
  the registry entry is the project and the directory is the split-tree
  error of [Project resolution](#project-resolution-and-host-buckets).
- **What the validator reports.** Each bucket is listed once, as INFO
  ("unnamed, awaiting adoption"), never as a warning or an error. Its
  contents are checked as a project at the legacy tier's severities:
  record-file shapes are still errors — a corrupt `state.yaml` is broken
  wherever it lives — and integrity findings (index and symlink consistency,
  memory scope) are downgraded exactly as in a legacy tree. Housekeeping
  lists buckets awaiting adoption and never adopts them.
- **Leaving the bucket: adoption.** Adoption is a tooling job, not a schema
  rule: one commit that moves the directory to `<short-name>/` and adds the
  registry entry listing the remote. A relative compatibility symlink MAY be
  left at the old path (informational); a real directory left there is an
  error (a split tree). Renaming a project later is the same move.

## Appendix A: Legacy mapping (informative)

How a legacy tree maps onto schema form. A reader of a legacy tree can use
this table to recognise each part; a migration moves each row left to right.
Every right-hand path is an [Inventory](#inventory) row; every left-hand
path is a `legacy`-tier row there, or a host bucket.

| legacy (no manifest) | schema form | note |
|---|---|---|
| `sources.yaml` at the root | `_plexus.yaml` `sources:` | same shape and rules; the standalone file retires on migration |
| (none) | `_plexus.yaml` `projects:` | the registry; identity moves out of paths |
| `{host}/{project}/` | `<short-name>/` | by registry entry; the old path survives only as a host bucket for unnamed work or as a compatibility symlink |
| `{host}/{project}/runs/<slug>/` | `<project>/_runs/<slug>/<session-id>/` | a legacy run dir maps to its first session's id; the shared dir becomes one session dir |
| `{host}/{project}/runs/<slug>--<name>/` | `<project>/_runs/<slug>/<session-id>--<name>/` | the name suffix moves onto the session dir |
| `{host}/{project}/runs/.new-<session>-*/` | `<project>/_runs/.new-<session-id>-*/` | the seed directory, same lifecycle |
| `runs/<slug>/{state.yaml,events.jsonl,ledger.yaml,release.yaml,log.yaml,plan.index.json,goals.md,reports/}` | `<session-dir>/` same names | shapes and writers unchanged; location only |
| `runs/<slug>/masterplan/<mp-slug>/` | `<session-dir>/masterplan/` | the bundle de-nests: one bundle per session dir |
| `runs/<slug>/<artifact>.md -> masterplan/<mp-slug>/<artifact>.md` | `<session-dir>/<artifact>.md -> masterplan/<artifact>.md` | the same well-known artifact links, targets one level shallower; the multi-bundle `<mp-slug>-<name>` prefix retires |
| `{host}/{project}/runs/archive/<slug>/` | `<project>/_archive/_runs/<slug>/<session-id>/` | one archive home; legacy `runs/archive/` stays recognised in legacy trees |
| `{host}/{project}/specs/YYYY-MM-DD-<runlabel>--<basename>.md` | `<project>/_specs/YYYY-MM-DD-<runlabel>--<basename>.md` | same symlink mechanics and the same entry grammar; targets re-pointed at `../_runs/<slug>/<session-dir>/…`, and `<runlabel>` gains its `-<session-id>` half because the one legacy run dir becomes one record per launch |
| `{host}/{project}/info/` | `<project>/_info/` | unchanged contents |
| `{host}/{project}/info/gate-check.yaml` | `<project>/_info/gate-check.yaml` | same shape, same consumer |
| `{host}/{project}/memory/` | `<project>/_agents/memory/` and `_agents/memory/` | split by `metadata.type`: `project` stays, every other type lifts to instance scope |
| `{host}/{project}/memory/MEMORY.md` | `<project>/_agents/memory/MEMORY.md` and `_agents/memory/MEMORY.md` | one index per scope, each listing only the files in its own directory |
| `{host}/{project}/{task_type}/{target}/` | (none) | pre-run-dir layout; read-side only, informational |
| `napkin/` | `_napkin/` | or a mounted napkin repository |
| `taskdef/` | `_taskdef/` | or a mounted taskdef repository |
| `agent-files/claude/` | `_agents/claude/` | harness name is the first level under `_agents/` |
| `mirrors/` | `_agents/mirrors/` | opaque in both forms |
| (none) | `_knowledge/<org>/<topic>/` | new area |
| (none) | `<project>/_plans/` (plan files, run-plan symlinks, `PLANS.md`) | new area |
| (none) | `_archive/` and `<project>/_archive/` | new; legacy has only `runs/archive/` |
| `{host}/{project}/` after migration | `{host}/{project} -> <short-name>` | OPTIONAL compatibility symlink; a real directory left behind is a split tree |
