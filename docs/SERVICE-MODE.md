# Service Mode

Run lmer against an **already-running** containerized project. Instead of
cloning a fresh copy, lmer mounts your existing local checkout and uses
`docker exec` to run commands (tests, migrations, etc.) inside the project's
container. Works with both Docker and Podman.

## Motivation

Many projects run as Docker/Podman Compose stacks during development. Running
tests or Django management commands requires the container's runtime
environment (database connections, installed packages, service dependencies).
Rather than building a sidecar/MCP bridge, service mode gives Claude direct
exec access to the running container — no extra infrastructure.

## Invocation

```bash
lmer <task> [target] --service <service-name> --checkout <host-path>
```

| Argument | Purpose |
|----------|---------|
| `target` (positional) | Repo URL, MR/PR/issue link — used for git operations, branch checkout, MR comments. Unchanged from normal mode. |
| `--service <name>` | Docker Compose service name (or container name/ID) to `docker exec` into. Must be running. |
| `--checkout <path>` | Path to existing local source checkout on the host. Mounted as `/workspace` instead of cloning. |

### Examples

```bash
# Simple single-service project
lmer chat \
  --service myapp \
  --checkout ~/myapp \
  https://gitlab.example.com/group/project/-/merge_requests/123

# Monorepo, working on a specific service
lmer chat \
  --service web \
  --checkout ~/project/dev-env \
  https://gitlab.example.com/group/project/-/merge_requests/123

# Chat mode with a running service (no target repo needed)
lmer chat \
  --service myapp \
  --checkout ~/myapp
```

## How It Works

### Container Resolution

When `--service` is provided, lmer resolves the running container using
whichever runtime is detected on the host (`docker` or `podman`):

1. Try `<runtime> ps --filter name=<service>` to find a running container
   matching the service name.
2. If no match, fail with a clear error listing running containers.

The resolved container ID is passed to the lmer container as
`LMER_SERVICE_CONTAINER`.

### Container Inspection

After resolution, lmer inspects the target container to discover:

- **Working directory**: `<runtime> inspect --format '{{.Config.WorkingDir}}'`
  — passed as `LMER_SERVICE_WORKDIR` so `target-exec` runs commands in the
  right directory.

### Volume Mounts (service mode additions)

| Mount | Container path | Access | Purpose |
|-------|---------------|--------|---------|
| Runtime socket | `/var/run/docker.sock` | RW | Required for exec access |
| Checkout path | `/workspace` | RW | Source code (same files the target container sees) |

The normal clone flow is **skipped** — `clone_and_exec.py` detects
`LMER_SERVICE_MODE=1` and goes straight to command dispatch.

**Git operations** (branch checkout, MR fetch) still happen against
`/workspace` since it's a real git checkout.

### Runtime Compatibility (Docker & Podman)

Service mode works with both Docker and Podman through a unified approach:

1. **Host-side**: `service.py` already accepts a `runtime` parameter from
   `detect_runtime()` — all container queries (`ps`, `inspect`) use whichever
   runtime is available on the host.

2. **Socket discovery**: `_find_container_socket()` in `mounts.py` locates the
   correct socket for the detected runtime:
   - Docker: `/var/run/docker.sock`
   - Podman (rootless): `/run/user/<uid>/podman/podman.sock`
   - Podman (rootful): `/var/run/podman/podman.sock`

3. **Inside the lmer container**: The host socket is **always mounted to
   `/var/run/docker.sock`** regardless of which runtime is on the host. The
   container image ships `docker-ce-cli`, which talks to whichever socket is
   mounted at that path. Podman exposes a Docker-compatible API on its socket,
   so `docker exec`, `docker logs`, and `docker inspect` all work transparently.

This means `target-exec` and `target-logs` always use the `docker` CLI —
no runtime-conditional logic needed inside the container.

4. **Socket permissions**: The socket file's GID is read at launch and passed
   via `--group-add <gid>` so the container's `developer` user can access it
   without needing to be in a pre-configured group.

### Wrapper Scripts

Two scripts are installed to the lmer container's PATH:

#### `target-exec`
```bash
#!/bin/bash
# Run a command inside the target service container
exec docker exec -w "$LMER_SERVICE_WORKDIR" "$LMER_SERVICE_CONTAINER" "$@"
```

#### `target-logs`
```bash
#!/bin/bash
# Tail logs from the target service container (or another named container)
CONTAINER="${1:-$LMER_SERVICE_CONTAINER}"
LINES="${2:-50}"
exec docker logs --tail "$LINES" -f "$CONTAINER"
```

### What Claude Sees

Task instructions are augmented with a service mode section:

```
## Service Mode

A dev environment is running. Your workspace at /workspace contains the
project source code (shared with the running container).

To run commands in the project runtime (pytest, manage.py, pip, etc.):
  target-exec <command>

Examples:
  target-exec pytest tests/ -x
  target-exec python manage.py migrate
  target-exec python manage.py shell

To check service logs:
  target-logs              # target service
  target-logs <container>  # another container

Local tools (git, grep, rg, etc.) run directly in the lmer container.
```

### Flow Diagram

```
Normal mode:                       Service mode:
────────────                       ─────────────
1. Clone repo to /workspace        1. Bind-mount --checkout as /workspace
2. Run everything in lmer          2. Resolve --service → container ID
   container                       3. Mount docker.sock into lmer container
                                   4. Git operations (branch/MR checkout)
                                      happen directly in /workspace
                                   5. Claude edits /workspace
                                   6. Claude runs tests via target-exec
                                   7. Edits are instantly visible in
                                      the running service container
```

### File Ownership

The `--checkout` directory is bind-mounted read-write. The lmer container's
`developer` user must be able to write to it. Use `--match-uid` if needed:

```bash
lmer chat --service web --checkout ~/project/dev-env --match-uid ...
```

## Environment Variables (service mode)

| Variable | Set by | Purpose |
|----------|--------|---------|
| `LMER_SERVICE_MODE` | CLI | `1` when service mode is active |
| `LMER_SERVICE_CONTAINER` | CLI | Resolved container ID |
| `LMER_SERVICE_NAME` | CLI | Original `--service` value |
| `LMER_SERVICE_WORKDIR` | CLI | Working directory inside target container |

## Constraints

- The target container **must be running** when lmer starts.
- Container runtime socket access is required (security consideration — same
  as any Docker-in-Docker pattern). Works with both Docker and Podman sockets.
- The `--checkout` path should be the **same directory** (or parent of) that
  the target container has bind-mounted. Otherwise edits won't be visible
  to the running service. Note that the target container may only mount
  **specific subdirectories** — not the entire checkout.
- `--service` requires `--checkout` (can't exec into a container without
  local source to edit).
- `--checkout` can be used alone (without `--service`) to skip cloning and
  use an existing checkout.

## Customizing `gate-check` Tests

By default `gate-check` runs `pytest tests/` from `/workspace`. This won't work
for projects whose tests must run inside the target container with a specific
environment (Django settings, env-var fixtures, `target-exec`-wrapped invocation,
etc.).

To override the test step, drop a `gate-check-run-tests.sh` script into the
work-repo project info directory:

- Global: `{work_repo}/{host}/{project}/info/gate-check-run-tests.sh`
- Per-task (overrides global): `{work_repo}/{host}/{project}/{task}/info/gate-check-run-tests.sh`

The script must be executable. `gate-check` invokes it with the project root
as the working directory; exit code 0 = pass, non-zero = fail. stdout/stderr
is captured and the tail is surfaced as failure details.

Example for a Django service-mode project:

```bash
#!/usr/bin/env bash
set -euo pipefail
target-exec bash -c 'cd /srv/www.example.com && source venv/bin/activate && \
  export DJANGO_SETTINGS_MODULE=mainsite.settings \
         DATABASE_USER=root DATABASE_PASSWORD="" \
         RELEASE_ENV=run_tests && \
  pytest --reuse-db --disable-warnings "$@"'
```

## Comparison with Compose/Sidecar Approach

| Aspect | Sidecar (POC) | Service mode |
|--------|--------------|--------------|
| Extra containers | MCP bridge sidecar | None |
| Protocol | MCP over HTTP | `docker exec` via bash |
| Dockerfile changes | Scaffold Dockerfile.lmer | None |
| Compose override | Generated docker-compose.lmer.yml | None |
| Token management | Bearer token per session | None |
| Code changes | ~1000 lines, 21 files | ~150 lines, 5 files |
| Works with running stack | No (launches its own) | Yes |
