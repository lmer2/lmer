# Task Definitions

A *taskdef* is the prompt and supporting context that defines a task type
(`chat`, plus any custom tasks you supply). It is rendered into the Claude Code
session when the user runs `/start` (and `/followup`, if the taskdef provides
one).

## Anatomy

A taskdef is a directory containing `instructions.txt` and, optionally,
`followup.txt` and a per-task `task.yaml` manifest:

```
taskdef/
└── my-task/
    ├── instructions.txt   # rendered by /start
    ├── followup.txt       # rendered by /followup (optional)
    └── task.yaml          # per-task manifest (optional)
```

Tasks are discovered from multiple sources, in this precedence order
(first match wins):

1. **Work-repo project-scoped** — `{work_repo}/{host}/{project}/taskdef/`,
   applies only when working on that project.
2. **Work-repo global** — `{work_repo}/taskdef/`, applies to every project.
3. **`LMER_TASKDEF_PATHS`** — colon-separated list of extra directories
   passed via the environment.
4. **`/taskdef`** — the clone of `LMER_TASKDEF_REPO`, when configured. The
   CLI appends it after any user-supplied `LMER_TASKDEF_PATHS` entries;
   `LMER_TASKDEF_REF` pins the clone to a branch/tag/SHA (the rollback
   lever).
5. **Built-in** — the `taskdef/` directory shipped with this repository.

This lets a project ship a customised taskdef in its work-repo project
directory that shadows the built-in one (or adds a new task type) without
touching the agents/global repo.

## The per-task manifest (`task.yaml`)

A taskdef whose instructions require the **masterplan plugin** declares that
need in a `task.yaml` beside its `instructions.txt`:

```yaml
masterplan: true
```

At session start the container provisioning gate
(`lmer_cli.container.masterplan`, called from `libexec/claude-runner.sh`)
resolves the active task's `task.yaml` through the same tier precedence as
every other taskdef file and, when the flag is truthy (`true`/`1`/`yes`),
installs the masterplan plugin and exports `MASTERPLAN_RUNS_DIR` — exactly as
if `LMER_MASTERPLAN=1` had been set. Without the declaration, a custom
taskdef that tells the agent to run `/masterplan` comes up without the plugin
unless the operator remembers the env toggle at launch.

Notes:

- Like `instructions.txt`/`followup.txt`, the manifest resolves per-file:
  the highest-precedence tier shipping a `task.yaml` wins, so a work-repo
  override can flip the flag either way.
- An unreadable, malformed, or non-mapping manifest counts as "not
  declared" — provisioning is logged-never-fatal and a bad YAML file must
  not flip masterplan on (or take the session down).
- The dedicated `masterplan` taskdef needs no manifest (`LMER_TASK=masterplan`
  implies the workflow), and a truthy `LMER_MASTERPLAN` still enables it for
  any task. An explicit `LMER_MASTERPLAN=0` switches off the env toggle only;
  it does not veto a taskdef declaration.
- Distinct from the source-root `taskdef.yaml` (schema versioning, below):
  `task.yaml` sits inside one task's directory and describes that task.

## Tier ownership

Each tier has one clear owner:

- **Built-in (`taskdef/` in this repo)** — the base taskdef templates and
  shared partials: `base-task.jinja2` (the shared document skeleton), the
  cross-cutting partials (`run-state.jinja2`, `service-mode.jinja2`,
  `changelog.jinja2`, `provider-tooling.jinja2`, `phasic.jinja2`,
  `self-dev.jinja2`), and the `chat` taskdef as the zero-config fallback.
  Everything here is generic. Changing a template here means a release of
  this repo.
- **The dedicated taskdef content repo (`LMER_TASKDEF_REPO`)** — the real
  task bodies (`develop`, `followup`, `review`, `modernize`, `masterplan`):
  schema-2 `{% raw %}{% extends 'base-task.jinja2' %}{% endraw %}` bodies
  that override named blocks and contain only what makes each task itself.
  Prompt iteration is a push to that repo — no image rebuild. The content
  repo's CI proves its bodies render against a pinned base via the render
  matrix's external-source mode: from a checkout of this repo at the pinned
  tag, `LMER_RENDER_SOURCE=<clone of the content repo> uv run pytest
  tests/test_taskdef_render_matrix.py -q` renders every taskdef directory
  under the given path — honoring its root `taskdef.yaml` — against that
  checkout's built-in base.
- **Work repo** — override tiers. The project tier customises one project;
  the global tier customises every project. **Work-repo-only mode is
  first-class**: with no dedicated taskdef or napkin repo configured, the
  work repo alone serves both (taskdefs from `{work_repo}/taskdef/`
  extending the built-in base across tiers, napkin at `{work_repo}/napkin/`
  captured by `work commit`). One work repo, nothing else.

## The base template

`taskdef/base-task.jinja2` defines the shared spine of every task document
as ordered, individually overridable Jinja blocks: `intro` → `service_mode`
→ `run_state` → `branch_setup` → `self_dev` → `phase0_tools` (containing
`provider_tooling`) → `project_info` → `secrets` → `clean_gate` →
`task_phases` → `changelog` → `phasic` → `delivery` → `close_out` →
`do_not` (containing `do_not_extra`).

A schema-2 body extends the base and overrides only what makes it that
task. `intro` and `task_phases` are the required per-task pieces —
`task_phases` is the task's own workflow and renders empty if not
overridden. Add task-specific HARD RULES via `do_not_extra` instead of
replacing the whole `do_not` list; extend a block while keeping its base
content with `{% raw %}{{ super() }}{% endraw %}`.

## Schema versioning

A taskdef *source root* (any directory on the precedence list) declares its
schema in a `taskdef.yaml` manifest:

```yaml
schema: 2
```

- **Schema 1 (legacy)** — include-style bodies, rendered exactly as before
  manifests existed. **An absent manifest means schema 1** — this
  grandfather clause keeps existing work-repo and `LMER_TASKDEF_PATHS`
  collections working unchanged.
- **Schema 2** — bodies may extend `base-task.jinja2` and override its
  blocks; the block lint (below) applies.

The renderer declares the schemas it supports and checks the manifest of
**every source root the render actually consults**: the root the rendered
file resolved from, and the root of each parent template it (transitively)
extends — the same roots the source banner reports, so a tier that shadows
`base-task.jinja2` under an unsupported schema is gated too. A manifest in
an unused tier is never consulted, so a stale manifest in an inactive tier
cannot break sessions. An unsupported schema in any consulted root fails
`/start` loudly, naming the source, its schema, and the supported set. Cross-source skew is
what schema versioning guards; *within* schema 2, base evolution is guarded
by the block-interface stability policy below.

## Shadowing and source banners

Because all tiers share one loader search path, **template shadowing across
tiers is a supported override feature**: a work-repo tier can shadow not
just whole taskdefs but also `base-task.jinja2` or any shared partial (e.g.
patching one shared block for a single deployment) — no loader
configuration needed.

To keep shadowing observable, `/start` prints a greppable banner naming the
resolved source of the rendered template and of every parent template it
extends:

```
taskdef source: /taskdef (schema 2)
taskdef source (base-task.jinja2): /Agents/global/taskdef (schema 1)
```

`/followup` prints the same for `followup.txt`, which resolves
independently through `find_taskdef_file` — a taskdef's `instructions.txt`
and `followup.txt` can straddle tiers.

## Block-interface stability policy

Jinja **silently ignores** a child block whose name no longer exists in the
parent — renaming or removing a base block would silently drop content from
every extending body while everything still renders green. Two guards:

1. **Renaming or removing a block in `base-task.jinja2` is a schema bump.**
   Treat the block set as a public interface.
2. **The renderer lints blocks at render time**: every top-level override
   block a child defines must exist somewhere in its parent chain, or
   `/start` fails loudly. The lint walks the template AST (`env.parse` →
   `Extends`/`Block` nodes), so a *new* block nested inside an overridden
   block — legal Jinja — is not flagged.

## Template format

`instructions.txt` and `followup.txt` are Jinja2 templates. When `/start`
fires, the template is rendered with:

- every `LMER_*` environment variable (e.g. `{{ LMER_REPO_URL }}`,
  `{{ LMER_TASK }}`, `{{ LMER_TASK_TARGET }}`)
- `{{ work_mode }}` — `finish` (default) or `phasic`
- `{{ instructions_file }}` — the absolute path of the file being rendered
- `{{ taskdef_name }}` — the directory name of the active taskdef
- `{{ taskdef_file }}` — alias for the path of the rendered file
- `{{ is_github }}` / `{{ is_gitlab }}` — booleans for the main task
  target's provider, so a template can branch on the review interface (e.g.
  use `github-review` vs `gitlab-review`). The host is taken from
  `LMER_REPO_HOST` (falling back to the host parsed from `LMER_TASK_TARGET` /
  `LMER_REPO_URL`); github.com and GitHub Enterprise hosts set `is_github`,
  any other non-empty host sets `is_gitlab`, and both are `False` when no host
  can be determined.

To emit a literal `{{ VAR }}` in the rendered output (e.g. when documenting
the template format itself), wrap it in a Jinja string: `{{ '{{ VAR }}' }}`.
For longer literal blocks, use `{% raw %}...{% endraw %}`.

## Shared partials

Templates can pull in shared snippets with Jinja's `include` directive. The
included file is resolved against the taskdef's parent directory first, then
against the work-repo taskdef directories (project-scoped, then global), then
against every entry on `LMER_TASKDEF_PATHS`, then against the built-in taskdef
root.

The built-in `taskdef/service-mode.jinja2` is a good example: it expands to
service-mode-specific guidance only when `LMER_SERVICE_MODE=1`, and several
taskdefs include it.

```jinja2
{% raw %}{% include 'service-mode.jinja2' %}{% endraw %}
```

## Patterns

When writing a new taskdef, the templates shipped under `taskdef/` and any
external taskdef collections on `LMER_TASKDEF_PATHS` are worth reading as
references. The recurring patterns:

1. **Open with context.** State what the user wants, where the file lives,
   and what work mode is active — this orients the session immediately.
2. **Include shared partials** for cross-cutting concerns (service mode,
   changelog rules, secret-handling reminders).
3. **Make the `work` integration explicit.** Tell the session when to call
   `work read-project-info`, when to `work log`, and when to `work commit`.
4. **Structure non-trivial tasks as phases** (e.g. Phase 0: tools,
   Phase 1: explore, Phase 2: interview, Phase 3: implement). This pairs
   well with `/start phasic`.
5. **End with HARD RULES** for things that must not happen (no commits
   without approval, no pushing without permission, etc.).

## Adding a taskdef

1. Create the directory: `mkdir -p ~/my-tasks/my-task`
2. Write `instructions.txt` (and `followup.txt` if `/followup` makes sense).
3. Add the parent directory to `LMER_TASKDEF_PATHS`:
   `export LMER_TASKDEF_PATHS=~/my-tasks`
4. Launch with `lmer my-task <repo>` and run `/start` inside the session.

A minimal `instructions.txt`:

```jinja2
{% raw %}You are chatting with the user about {{ LMER_REPO_URL }}.

The current work mode is `{{ work_mode }}`.{% endraw %}
```
