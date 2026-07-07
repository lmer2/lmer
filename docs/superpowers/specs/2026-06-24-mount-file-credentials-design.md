# Design: `--mount-file` arbitrary file mounts

**Date:** 2026-06-24
**Status:** Implemented (!119)

## Problem

There is no way to selectively mount specific host files into specific
destinations inside the lmer container. The motivating case is a Kubernetes
config (`~/.kube/config`), but the need is general: credential and config files
that the agent's tooling expects at particular paths.

Today the only ways to get credentials into the container are:

- `build_user_mounts()` — hard-coded set (`~/.claude/.credentials.json`,
  `~/.claude.json`, `SSH_AUTH_SOCK`).
- `build_container_home_mounts()` — hard-coded persistent dirs under
  `container-home/` (`.ssh`, `.config`, `.gitconfig`, …).
- `.env` files — environment variables only, not files.

None let a user say "take *this* file and put it *there*."

## Goal

A repeatable CLI flag plus an env var that mount individual host files to
explicit container destinations, with strict (fail-fast) validation.

## Spec format

Each entry is `host:container[:mode]`:

- **host** — host path. `~` and `$VAR` expanded. Must resolve to an **existing
  file** (fail-fast otherwise).
- **container** — destination path inside the container. Must be **absolute**
  (fail-fast otherwise).
- **mode** — optional. `ro` (default) or `rw`. Any other value is an error.

## Input sources (merged)

1. Repeatable `--mount-file host:container[:mode]` flag
   (argparse `action="append"`).
2. `LMER_MOUNT_FILES` env var — **comma-separated** entries, each using the same
   `host:container[:mode]` grammar. Comma is the entry separator so it does not
   collide with the `:` field separator (cf. `LMER_TASKDEF_PATHS`, which picks
   its own separator for the same reason).

Merge order: **env entries first, then `--mount-file` flags.** If two entries
target the same container destination, **last wins** and a warning is emitted.
This lets a persistent `.env` set a baseline that an ad-hoc flag can override.

## Implementation

Mirrors existing mount-builder conventions in `src/lmer_cli/mounts.py` and the
wiring in `src/lmer_cli/cli.py`.

### `mounts.py`

- A small `FileMountSpec` value (host `Path`, container `str`, mode `str`).
- `build_file_mounts(runtime, specs) -> list[str]` — emits
  `-v {host}:{container}:{mode}{se}` per spec, where `{se}` comes from the
  existing `selinux_opt(runtime)` helper, exactly like every other builder.

### `cli.py`

- `parse_file_mount_specs(flag_values, env_value) -> list[FileMountSpec]`:
  - splits env var on `,`, flags are already a list;
  - expands `~` and env vars in the host path;
  - validates: host file exists, container path absolute, mode in `{ro, rw}`;
  - **raises a clear error that aborts the run** on any invalid entry;
  - applies env-first-then-flags ordering and last-wins dedup (with warning).
- `--mount-file` argparse flag (`action="append"`, `metavar="HOST:CONTAINER[:MODE]"`),
  documented with `(env: LMER_MOUNT_FILES)` to match the existing flag style.
- Wire into the run-args assembly right after `build_user_mounts` (~`cli.py:900`),
  emitting one `success()` line per mount: `host → container (mode)`.

### Fail-fast rationale

This is intentionally **stricter** than `build_external_taskdef_mounts` and the
uv-cache mount, which skip-and-warn on a missing source. Credentials are the
whole point here: silently launching without a kubeconfig the user explicitly
asked for would produce confusing downstream auth failures. Surfacing the typo
immediately is the safer default. The implementation/PR notes should call out
this deliberate divergence from the skip-and-warn builders.

## Safety

- Bind mounts of existing host files, mounted as-is. The tool never copies
  secrets anywhere — in particular nothing is written to shared locations.
- SELinux `,z` labeling applied automatically via `selinux_opt()`.
- Default mode `ro` keeps credentials read-only unless `rw` is explicitly asked
  for.

## Testing

Following `tests/test_lmer_cli_mounts.py` style:

- `parse_file_mount_specs`: valid single, valid multi, `~`/`$VAR` expansion,
  env+flag merge ordering, last-wins dedup, and each fail-fast case (missing
  file, relative dest, bad mode).
- `build_file_mounts`: arg shape, `ro`/`rw`, SELinux suffix present/absent
  (patching `_is_selinux_enforcing` as existing tests do).

## Out of scope (YAGNI)

- Directory mounts — existing builders already cover directories; this feature
  is files-by-design.
- Auto-injecting `KUBECONFIG` — the user sets the destination to
  `/home/developer/.kube/config` (kubectl's default), so no env var is needed.
