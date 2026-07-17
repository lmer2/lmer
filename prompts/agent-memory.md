## Agent memory

You have persistent, file-based memory at
`/home/developer/.claude/projects/-workspace/memory/`. The path is
harness-neutral despite the `.claude` segment — every lmer agent (Claude
Code, codex, pi, ...) working on this project shares the same memory, and
previously saved memory has already been restored there before this
session started. This directory already exists — write to it directly at
that absolute path. Never create a `memory/` directory inside the project
repository or workspace: memory lives outside the repo, and a stray
`memory/` folder pollutes the project's git status.

- **At session start**, read `MEMORY.md` in that directory — it is the
  index, one line per memory. Open any linked memory file whose
  description looks relevant to your task.
- **To save a durable fact**, write a new markdown file in that directory
  with frontmatter (`name:` short-kebab-case slug, `description:` one-line
  summary, `metadata.type:` one of `user` | `feedback` | `project` |
  `reference`), then add a one-line pointer for it in `MEMORY.md`
  (`- [Title](file.md) — hook`). Update an existing file instead of
  creating a near-duplicate; delete memories that turn out to be wrong.
- **Save only durable, project-level facts** that are not derivable from
  the repository itself (code structure, git history, and CLAUDE.md
  content do not belong in memory). Never store credentials, secrets, or
  customer data — memory is shared through the work repository.
- **Before finishing the task**, run `work memory persist` to push your
  memory to the work repository. Memory that is not persisted is lost with
  the container.
