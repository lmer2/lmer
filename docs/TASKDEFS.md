# Task Definitions

A *taskdef* is the prompt and supporting context that defines a task type
(`chat`, plus any custom tasks you supply). It is rendered into the Claude Code
session when the user runs `/start` (and `/followup`, if the taskdef provides
one).

## Anatomy

A taskdef is a directory containing `instructions.txt` and, optionally,
`followup.txt`:

```
taskdef/
└── my-task/
    ├── instructions.txt   # rendered by /start
    └── followup.txt       # rendered by /followup (optional)
```

Tasks are discovered from multiple sources, in this precedence order
(first match wins):

1. **Work-repo project-scoped** — `{work_repo}/{host}/{project}/taskdef/`,
   applies only when working on that project.
2. **Work-repo global** — `{work_repo}/taskdef/`, applies to every project.
3. **`LMER_TASKDEF_PATHS`** — colon-separated list of extra directories
   passed via the environment.
4. **Built-in** — the `taskdef/` directory shipped with this repository.

This lets a project ship a customised taskdef in its work-repo project
directory that shadows the built-in one (or adds a new task type) without
touching the agents/global repo.

## Template format

`instructions.txt` and `followup.txt` are Jinja2 templates. When `/start`
fires, the template is rendered with:

- every `LMER_*` environment variable (e.g. `{{ LMER_REPO_URL }}`,
  `{{ LMER_TASK }}`, `{{ LMER_TASK_TARGET }}`)
- `{{ work_mode }}` — `finish` (default) or `phasic`
- `{{ instructions_file }}` — the absolute path of the file being rendered
- `{{ taskdef_name }}` — the directory name of the active taskdef
- `{{ taskdef_file }}` — alias for the path of the rendered file

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
