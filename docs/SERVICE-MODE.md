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
| `--service-group <project>` | Compose **project** whose running services this session may target. The agent retargets with `target-switch`. See [Service groups](#service-groups-one-session-a-whole-stack). |
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

Three scripts are installed to the lmer container's PATH:

| script | what it does |
|---|---|
| `target-exec <command>` | Runs the command inside the target container, in that container's working directory |
| `target-logs [container] [N]` | Follows the target container's logs, or a named container's |
| `target-switch [service]` | Lists the [service group](#service-groups-one-session-a-whole-stack) and its current target, or retargets to a member |

`target-exec` and `target-logs` take their container from the target file
`target-switch` writes (`LMER_SERVICE_TARGET_FILE`), falling back to
`LMER_SERVICE_CONTAINER` from the launch when no switch has happened. Reading it
per invocation is what makes a switch visible to shells that were already
running; a target file that exists but is incomplete is an error naming the
file, never a quiet fall-back to a container the agent did not choose.

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
| `LMER_SERVICE_GROUP` | CLI | Compose project the session is attached to (`--service-group`) |
| `LMER_SERVICE_TARGET_FILE` | CLI | Where `target-switch` records the current target; read by `target-exec`/`target-logs` |

## Constraints

- The target container **must be running** when lmer starts.
- Container runtime socket access is required (security consideration — same
  as any Docker-in-Docker pattern). Works with both Docker and Podman sockets.
- The `--checkout` path should be the **same directory** (or parent of) that
  the target container has bind-mounted. Otherwise edits won't be visible
  to the running service. Note that the target container may only mount
  **specific subdirectories** — not the entire checkout.
- `--service` and `--service-group` both require `--checkout` (can't exec into
  a container without local source to edit).
- `--checkout` can be used alone (without `--service`) to skip cloning and
  use an existing checkout.

## Service groups: one session, a whole stack

`--service` binds a session to one container. A stack that runs as a unit — the
fullctl suite is 12+ containers, all up together — would need twelve sessions
that way, one per service. `--service-group <compose-project>` attaches **one**
session to the whole project instead, and the agent moves its target around
inside it:

```bash
lmer chat \
  --service-group fullctl \
  --checkout ~/fullctl/dev-env
```

### The group is discovered, not declared

Membership is read from Compose's own `com.docker.compose.project` label — the
same label family `--service` already matches on. Nobody lists the members: a
stack that is up has already named them, and a list written by hand would go
stale the first time the stack changed. The list is re-read on every switch, so
a member that restarted under a new container id is still reachable, and a
member that is down simply is not offered.

A member is named by its **compose service name**, whatever the replica count —
a name that moved when a sibling stopped would not match what a running session
recorded as the containers it holds. A scaled service (`--scale web=3`) is
therefore listed once per replica under the one service name, and that name is
refused as ambiguous when you switch to it; the replica's container name
(`stack-web-2`) selects one. Containers appearing and disappearing mid-read is
ordinary for a live stack, so a member that vanishes between the listing and the
inspection is dropped rather than failing the whole group.

### Switching target

Inside the container:

```bash
target-switch                # the group, the current target, and every member
target-switch tasks          # point target-exec/target-logs at 'tasks'
target-exec pytest tests/    # …now runs in the tasks container
```

A switch takes effect for every command started after it, including in shells
and agents that were already running — the target lives in a small file
(`LMER_SERVICE_TARGET_FILE`), not in the environment, precisely so that a
retarget is not invisible to processes that started before it. `target-exec`
and `target-logs` read that file on each invocation and fall back to the
launch-time container when it does not exist, which is what leaves
single-service sessions behaving exactly as they did.

`--service` and `--service-group` compose: `--service <member>` names where the
session **starts**, and must be a running member of the group. With the group
alone the session starts with nothing targeted, and `target-exec` says so and
names the switch rather than picking a member nobody chose.

### What a group session holds

A group session can retarget to any member without asking anyone, so for
[slot](#service-slots-several-agents-one-dev-stack) purposes it holds **all** of
them. That runs in both directions, because the harm — two agents in one dev
container — does not care which session started first:

- a slot naming any member reads `in use` while a group session runs, and
- a group slot reads `in use` while any *single-service* session holds one of
  its members, naming that container in the reason.

The members a session holds are resolved once, at the spawn that claims the
slot, and recorded on its registry entry — the "recorded rather than re-derived"
rule single-service slots already follow, so a presets edit cannot move what a
running session is understood to hold. Both spellings of each container are
recorded (compose service name and container name), since a slot's preset may
name either. A group *slot* learns its members from the same probe that already
asks whether the project is up, so the symmetry costs no extra runtime query.

A preset attaches to a group with `service_group` (see
[PRESETS.md](./PRESETS.md#fields)); a slot pointing at such a preset guards the
project.

### Not in this slice

- **A control-plane route to retarget a running session.** The switch is an
  in-container command; the operator moves a session by asking its agent.
- **Groups that are not a compose project.** No ad-hoc name lists, no group
  spanning two projects.
- **A container that joined the project after a group session started.** The
  session can switch to it — membership is live — but what that session was
  recorded as holding was read at spawn, so a *single-service* slot naming the
  late member reads free. (A group slot over the project is not affected: its
  own membership is re-read on every poll.) Closing the remaining case means
  re-resolving every live session's group on every poll; if it bites, that is
  the trade to revisit.
- **Retargeting `/workspace`.** One checkout per session, as before: a group is
  the services of one stack over one source tree.
- **Per-member slots inside a group.** A group is taken whole.

## Service slots (several agents, one dev stack)

A dev service is a single-occupancy resource: two agents running migrations
against one database is a data-corruption story, not a concurrency story. A
**service slot** is how the platform makes that rule enforceable — a named
binding from one runner to one dev service, which a session either holds or
does not.

Slots are declared once per host in `~/.lmer/platform/config.json`:

```json
{
  "slots": [
    {
      "name": "webapp-dev",
      "preset": "webapp_dev",
      "description": "Web app dev stack"
    }
  ]
}
```

| key | meaning |
|---|---|
| `name` | what you spawn into, and what the fleet view calls the row |
| `preset` | a preset from this host's presets file (see [PRESETS.md](PRESETS.md)); it must set `service`, must not override `--service`/`--checkout` in its own `args`, and it is what puts the session into service mode |
| `description` | optional, shown on the row |

**One service, one slot.** The resource a slot protects is the dev service, not
the name written over it, so two slots resolving to the same `service` would
each read free and each grant — and the sessions would land in one container.
The second one therefore loads *unusable*, naming the slot that bound the
service first. The first slot that **resolves** wins — which is declaration
order among slots that resolve at all, so an earlier slot that is itself
unusable reserves nothing and a later one can take the service.

That rule is derived from the presets file, and the presets file is hot — so it
is backed by a second check that measures rather than predicts: a spawn is also
refused (409) when a **live session** is running against the slot's service,
whatever slot name that session claimed. Without it, fixing an unusable slot's
preset while a session runs under a different slot on the same service would
hand the fixed slot a service already in use.

**Two operational preconditions**, both easy to miss:

- **`slots` is read when the daemon starts.** `config.json` is resolved once at
  boot, so a slot you add to a running platform is invisible until you restart
  it. Fixes to the *presets* file stay hot — which is what makes the
  `misconfigured` row below recoverable without touching `config.json`.
- **The daemon's own environment must set `LMER_PRESETS_FILE`.** It is read
  where the daemon runs, not where `lmer` runs. Without it no presets load at
  all and every slot reports so by name — a daemon started from systemd or a
  fresh shell is the usual way to hit this, while `lmer --preset X` keeps
  working fine in your terminal.

A slot points at a **preset** rather than spelling out the service and
checkout itself. That keeps host paths in the presets file — the one place
they already live — and makes occupying a slot and running in service mode the
same act rather than two settings that have to agree.

### Spawning into one

From the fleet view's spawn dialog (the picker lists free slots only), or:

```bash
lmer-ctl spawn develop https://git.example.com/g/p/-/issues/7 --slot webapp-dev
```

`--slot` and `--preset` are exclusive — the slot supplies its own.

### Occupancy is derived, never stored

Nothing writes down which slot is taken. A slot reads **occupied** while a live
session's registry entry names it, and **free** the moment that entry stops
being live. Two things follow, and both are the reason it works this way:

- it survives a daemon restart, because liveness is a stateless PID probe over
  files on disk;
- it cannot strand a slot. A session that dies without cleaning up — crash,
  `kill -9`, host reboot — leaves a dead PID behind, and a dead PID reads as
  free. There is nothing to reset and no reconciler to run.

So a slot frees when the session's process ends, whatever ended it. There is no
release verb because there is nothing to release.

### What a row can say

| state | meaning | fix |
|---|---|---|
| `free` | nothing holds it and its service is running | — |
| `in use` | a live session holds it; the row links to that run | wait, or use another slot |
| `service not running` | the definition is fine, the dev service is not up | start the stack |
| `misconfigured` | see below | fix the presets file — the slot recovers without a config edit |

A slot is `misconfigured` when:

- no presets are loaded at all — the row says *which* cause rather than blaming
  the preset name: the daemon's `LMER_PRESETS_FILE` is unset (above), or it is
  set and the file is missing, unreadable or not a JSON object;
- the named preset does not exist on this host;
- the preset sets neither `service` nor `service_group`, so it cannot put a
  session into service mode;
- the preset's `args` set `--service`, `--service-group` or `--checkout` —
  **including any abbreviation** argparse accepts (`--che=…`, `--service-g`),
  since `lmer` leaves `allow_abbrev` on. (`--serv`/`--se` are ambiguous between
  `--service` and `--service-group` and are refused by the parser instead, which
  lands the slot under the *unparseable args* bullet below rather than this
  one.) `lmer` re-parses the preset's own tokens followed by
  its `args` and the last occurrence wins, so the slot would probe, display and
  guard one service while the session ran against another. The binding a slot
  claims has to be the one the session gets. Decided by handing the `args` to
  the real parser rather than by matching spellings, so this cannot drift from
  what `lmer` does;
- the preset's `args` do not parse at all, since the session could not start;
- the preset's `service` — or, for a group preset, its project or any of the
  project's running members — is already bound by an earlier slot (one service,
  one slot — above). Two slots that overlap only through a group's membership
  are caught here too, so neither reads free until someone spawns.

A slot whose preset this host cannot resolve **loads anyway**, unusable and
with the reason on its row. It is not dropped, because it can become true again
by fixing the presets file, and a slot that silently vanished would explain
nothing to the operator who typed the name.

The service state comes from a probe that is cached for 30 seconds — the fleet
view is polled every ten, and a container query per slot per poll is a cost the
row does not need. A spawn re-probes without the cache, because an action taken
on the answer cannot afford a stale one.

### Refusals

Each names the gate that closed:

| condition | status | class |
|---|---|---|
| slot is held by a live session | 409 | `SlotOccupied` |
| slot's **service** is held by a live session under another slot name | 409 | `SlotOccupied` |
| no such slot on this host | 400 | `SpawnError` |
| slot is `misconfigured` — any cause in the list above | 400 | `SpawnError` |
| slot's service is not running | 400 | `SpawnError` |
| host is at `max_concurrent_sessions` | 429 | `CapacityError` |

The slot gate and the concurrency cap are **independent**: a free slot on a
full host still refuses, and a held slot on an idle host still refuses.

### Not in this slice

- **Queueing.** A spawn into an occupied slot is refused now, not queued.
- **`park` / `hold`.** A slot frees when its session's process ends.
- **Editing slots in the UI.** They are declared in `config.json`; the fleet
  view only reads them.
- **Exactly tracking sessions that predate this version.** Occupancy by service
  is read from the service each session recorded at spawn time. A session that
  started before that field existed has it inferred instead, from both the preset
  its entry names and the slot's current resolution — so an inferred service
  blocks both candidates rather than picking one. If the *preset itself* is edited
  while such a session runs, neither candidate is the service it actually holds
  and that service is not blocked. This drains as those sessions end and cannot
  recur: every new spawn records the service.
- **Service-mode sessions that hold no slot.** A session started with a preset
  but no slot — or `lmer --service web` on the host — is not visible to slot
  occupancy and blocks nothing. Slots enforce sharing among sessions that use
  slots.
- **Reclaiming a slot on resume.** Resuming a run starts a fresh session that
  carries no slot and no service mode, so the slot it held is genuinely free and
  the new session is an ordinary one. Pre-existing: resume has never carried
  `--preset` either.
- **An atomic claim.** The occupancy check and the registry write that records it
  are separated by the session's process start, so two spawns racing for one free
  slot can both pass — the same window the concurrency cap and the
  one-run-one-session rule have. It is *reported* rather than prevented: a
  contended row names every holder and says how many there are, and the daemon
  logs `slot_double_occupancy` when the set of colliding sessions changes.

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
