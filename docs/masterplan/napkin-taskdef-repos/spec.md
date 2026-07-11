# Design: Napkin & Taskdef as Optional Separate Repos

**Date:** 2026-06-22
**Status:** Approved — revised 2026-06-23 (auth, napkin push, conventions)

## Problem

Two repos serve overlapping purposes without coordination:

- `~/napkin/` — team-visible shared working notes, organized by org (org-a/, org-b/, lmer/, etc.)
- `~/Agents/work/` — AI work context: task worklogs, taskdef templates, agent files

Agents write analysis notes into `Agents/work/` when they should go to napkin. There is no single place to write and no in-container path that company-level Claude instructions can reference.

Similarly, taskdef is always bundled into the work repo with no way to share a taskdef repo across multiple work repos.

## Goals

1. Agents have one place to write org notes regardless of which repo is active
2. `~/napkin` works as a stable path inside the container (company Claude config references it)
3. Taskdef can optionally live in its own repo, inserted between work-taskdef and lmer-global in the search order
4. Both napkin and taskdef follow the same optional-separate-repo pattern, falling back to subdirs of work

## Non-Goals

- Git submodules (explicitly rejected)
- Changing `LMER_WORK_REPO_PATH` or the `/work` container path
- Forcing migration of the existing napkin repo

## Design

### New Environment Variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `LMER_NAPKIN_REPO` | no | — | Git URL of napkin repo (host-side; SSH or HTTPS) |
| `LMER_NAPKIN_TOKEN` | no | — | Auth token, consumed **host-side** to credential the napkin URL |
| `LMER_NAPKIN_PATH` | injected | see below | In-container path agents write to |
| `LMER_TASKDEF_REPO` | no | — | Git URL of shared taskdef repo (host-side; SSH or HTTPS) |
| `LMER_TASKDEF_TOKEN` | no | — | Auth token, consumed **host-side** to credential the taskdef URL |
| `LMER_TASKDEF_REF` | no | default branch | Optional ref/branch to pin the taskdef clone (reproducibility) |

### Credential Handling (host-side URL injection)

**This mirrors the existing work-repo pattern exactly** — and that pattern does *not* pass a token into the container. `LMER_WORK_REPO_TOKEN` is consumed on the host by
`_convert_ssh_to_https_if_token_available(..., for_work_repo=True)` (`tokens.py`), which bakes the
credential into the URL; only the resulting credentialed `LMER_WORK_REPO` is placed in the container
env. The token variable itself never crosses into the container.

Napkin and taskdef follow suit:

- On the host, `cli.py` resolves each repo URL and injects its token into the URL
  (reuse `_convert_ssh_to_https_if_token_available` / `_get_gitlab_token`, adding lookups for
  `LMER_NAPKIN_TOKEN` and `LMER_TASKDEF_TOKEN`).
- Only the **credentialed URLs** are passed into the container env (`LMER_NAPKIN_REPO`,
  `LMER_TASKDEF_REPO` carry their own auth). `LMER_NAPKIN_TOKEN` / `LMER_TASKDEF_TOKEN` are **not**
  forwarded into the container.

This is required because `clone_and_exec.py` runs as a standalone, import-free module
(it cannot import `lmer_cli.tokens`) and `ensure_clone()` takes no token argument — so the URL it
receives must already be cloneable as-is. Passing a token-less URL plus a separate token would leave
private clones unauthenticated.

### Napkin Path Resolution

`LMER_NAPKIN_PATH` is computed at container start and always injected into the container environment:

- If `LMER_NAPKIN_REPO` is set → clone at `/napkin`, `LMER_NAPKIN_PATH=/napkin`
- If not set → `LMER_NAPKIN_PATH={LMER_WORK_REPO_PATH}/napkin` (e.g. `/work/napkin`)

Agents always reference `$LMER_NAPKIN_PATH` — the mode is transparent to them.

### Napkin Commit / Push

Napkin notes must be pushed back, in **both** modes:

- **Subdir mode** (`$LMER_NAPKIN_PATH` under `/work`): already covered — `work commit` stages and
  pushes everything under the work repo, including `napkin/`. No new code.
- **Separate-repo mode** (`/napkin` is its own git repo): `work commit` only operates on `/work`, so
  `/napkin` would be cloned-but-orphaned. Add a napkin push step to the `work` CLI: when
  `$LMER_NAPKIN_PATH` resolves to a git repo **outside** `LMER_WORK_REPO_PATH`, `work commit` also
  runs `fetch → pull → add → commit → push` against it (same `git_ops` flow as the work repo). This
  keeps a single agent-facing verb (`work commit`) regardless of mode.

The push uses the credentialed `LMER_NAPKIN_REPO` remote established at clone time (see Credential
Handling). Failure to push napkin is a warning, not a hard error, so it cannot block worklog commits.

### Taskdef Search Order (updated)

```
1. {work_repo}/{host}/{project}/taskdef/   ← project-scoped (highest)
2. {work_repo}/taskdef/                    ← work-global
3. /taskdef                                ← LMER_TASKDEF_REPO, if set (new)
4. /Agents/global/taskdef                  ← lmer built-in (lowest)
```

When `LMER_TASKDEF_REPO` is set, the container-side path `/taskdef` is **appended to the
`container_taskdef_paths` list in `cli.py`** (the same list that becomes `LMER_TASKDEF_PATHS`),
*after* any externally-mounted taskdef paths. It is appended as a literal container path — it does
**not** go through `build_external_taskdef_mounts`, since `/taskdef` is a clone target inside the
container, not a host directory being mounted in.

`taskdef_search_dirs()` (`start.py`) already orders entries as: work-repo dirs → `LMER_TASKDEF_PATHS`
entries → built-in. So `/taskdef` naturally lands between work-taskdef and the lmer built-in without
any change to `start.py`.

### Home Directory Symlinks

Created in `clone_and_exec.py` after all repo clones complete:

- `~/work` → `/work` (always — convenience alias for work repo)
- `~/napkin` → `$LMER_NAPKIN_PATH` (always — stable path for company Claude config)

The container user is `developer` (`/home/developer`).

Symlink creation must be **idempotent**: if the link (or a path) already exists at the target,
remove/replace it before creating, rather than letting `symlink_to` raise `FileExistsError`. This
matters for service mode, where a container can re-enter the entrypoint over its lifetime. In
subdir mode, also `mkdir -p` the `{LMER_WORK_REPO_PATH}/napkin` directory so `~/napkin` is not a
dangling link before the first write.

## Code Changes

### `src/lmer_cli/cli.py`

- Read `LMER_NAPKIN_REPO`, `LMER_NAPKIN_TOKEN`, `LMER_TASKDEF_REPO`, `LMER_TASKDEF_TOKEN`,
  `LMER_TASKDEF_REF`
- Credential the napkin/taskdef URLs host-side (token → URL), reusing the work-repo token helpers
- Compute `LMER_NAPKIN_PATH` (napkin repo path or `{LMER_WORK_REPO_PATH}/napkin`)
- If `LMER_TASKDEF_REPO` set: append `/taskdef` to `container_taskdef_paths` (the list that becomes
  `LMER_TASKDEF_PATHS`), after any external entries
- Add the **credentialed URLs** (not the raw tokens), `LMER_NAPKIN_PATH`, and `LMER_TASKDEF_REF` to
  the container env dict — same dict that already declares `LMER_WORK_REPO` etc.

### `src/lmer_cli/container/clone_and_exec.py`

After the existing work repo clone block:

1. If `LMER_NAPKIN_REPO` set: `ensure_clone(Path("/napkin"), napkin_repo_url, None, None)`
   (the URL already carries credentials)
2. If `LMER_TASKDEF_REPO` set: `ensure_clone(Path("/taskdef"), taskdef_repo_url, None,
   os.environ.get("LMER_TASKDEF_REF"))`
3. In subdir mode, `mkdir -p` the `$LMER_NAPKIN_PATH` directory
4. Create idempotent symlinks (unlink-if-exists, then create):
   - `/home/developer/work` → `LMER_WORK_REPO_PATH`
   - `/home/developer/napkin` → `LMER_NAPKIN_PATH`

Clone failures for napkin and taskdef are non-fatal (warn and continue), matching the pattern for
secondary MR clones.

### `src/work_repo/` (napkin push)

- Extend `work commit` (`cli.py` / `git_ops.py`): when `LMER_NAPKIN_PATH` is a git repo outside
  `LMER_WORK_REPO_PATH`, run the standard `fetch → pull → add → commit → push` flow against it after
  the work-repo commit. Push failures warn but do not fail the command.

### `docs/LMER-CLI.md` (update — required by project env-var convention)

Add a bullet for each new user-visible `LMER_` variable (`LMER_NAPKIN_REPO`, `LMER_NAPKIN_TOKEN`,
`LMER_NAPKIN_PATH`, `LMER_TASKDEF_REPO`, `LMER_TASKDEF_TOKEN`, `LMER_TASKDEF_REF`) under the
**LMER-Specific Environment Variables** section: accepted values, host-vs-container consumption, and
which scripts read them. Note that `LMER_NAPKIN_PATH` is computed/injected (decide whether to also
surface it in `lmer --show-env`, which otherwise only enumerates host-side `LMER_` vars).

### `lmer-docs/NAPKIN.md` (new file)

Documents napkin for agents:

- What it is: shared team working notes, organized by org
- Where to write: `$LMER_NAPKIN_PATH/{org}/` (org-a/, org-b/, lmer/, other/, etc.)
- File naming: topic-based (`qfx5220-channelization.md`), date-prefixed only for dated artifacts
- How to push: `work commit` (covers napkin in both modes — see Napkin Commit / Push)
- Cross-link to related notes when synthesizing across files
- No credentials, PII, or AI attribution lines

### `~/napkin/AGENTS.md` (trim)

Remove all agent operational instructions — they move to `lmer-docs/NAPKIN.md`. Keep only human-facing conventions (file naming, what belongs here, how to use MRs).

## Migration

1. Move loose `.md` files from `Agents/work/` root (`rate-limiting-analysis-202601.md`, `TODO-lmer.md`) into napkin under the appropriate org dir
2. Set `LMER_NAPKIN_REPO` in `~/.lmer/.env` pointing at the existing napkin GitLab repo — no napkin content needs to move

## Testing

- `test_lmer_cli_mounts.py` — add cases for napkin/taskdef clone steps, idempotent symlink creation,
  and subdir-mode `mkdir`
- `test_lmer_cli_env_sources.py` — verify `LMER_NAPKIN_PATH` is computed and injected correctly in
  both modes (separate repo and subdir); verify napkin/taskdef URLs are credentialed host-side and
  that raw tokens are **not** forwarded into the container env
- **Source guard test** (required by env-var convention §4) — regex source check asserting the new
  container-bound vars are declared in the cli.py passthrough env dict (cf.
  `test_cli_env_dict_declares_reasoning_effort`)
- `test_work_repo` — napkin push fires only when `LMER_NAPKIN_PATH` is a git repo outside the work
  repo, and push failure is non-fatal
- Manual: run `lmer` with `LMER_NAPKIN_REPO` set, confirm `/napkin` cloned, `~/napkin` symlink
  present, and `work commit` pushes napkin; run without, confirm `~/napkin` → `/work/napkin` and
  `work commit` still captures it
