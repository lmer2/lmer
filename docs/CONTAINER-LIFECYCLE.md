# Session container lifecycle

`lmer` starts every session container **detached**, on podman and docker alike.
The container's lifetime is not tied to any client's terminal: a closed
terminal, a dead attach client or a platform daemon restart leaves the session
running. Anyone can follow it again with `lmer follow`.

## Launch and follow

1. **Create.** `lmer` runs `<runtime> create` with the session's flags,
   labels and [name](#names), and with a `--cidfile` it reads the container id
   from. It does not pass `--rm`: the exit code has to outlive the exit.
   - The private env file holding the session's secrets is deleted as soon as
     `create` returns, because the runtime has read it by then. That holds
     whether `create` succeeded or failed.
   - A failed `create` that still produced a container removes it.
2. **Start attached.** `<runtime> start --attach [--interactive] <id>` attaches
   the console before the container prints anything, so no clone or entrypoint
   output is lost.
   - The container gets `-t -i` only when the host's stdin is a terminal,
     which is the rule `lmer` always used. A headless launch keeps clean,
     separate stdout and stderr.
3. **Wait, record, remove.** When the console ends, `lmer` runs
   `<runtime> wait <id>` for the real exit code, prints
   `── lmer: container exited with code N ──`, removes the container and exits
   with `N`.
   - A console that ends while the container still runs is treated as a
     **detach** when `lmer` is interactive and its terminal is still there:
     `lmer` hands the terminal back and leaves the container running. It does
     not depend on the console's exit code, which differs by verb (`docker
     attach` exits nonzero on a detach, `docker start --attach` 0); a nonzero
     code is shown in the notice.
   - A lost terminal (it hung up), a headless run whose attach broke, and any
     platform follower all keep waiting, because the exit code has to reach
     someone.
   - A start that never ran (the runtime refused a GPU or a port) returns 125,
     the same as a failed create, and removes the container.

On SIGTERM `lmer` stops the container (`<runtime> stop`) and still waits,
records and removes. Killing the attach client stops nothing, so the stop has to
be explicit.

Every container is created with `--stop-timeout 200`, so any stop allows that
long before the runtime kills it, instead of the runtimes' 10 s default. The
in-container session-end backstop (a work-repo push, up to 180 s) runs inside
that window. Attach clients run with `--sig-proxy=false` wherever the runtime's
`start` or `attach` takes it, so a signal ends the console and never the
session.

## Detach keys

Detach keys belong to the client that attaches, not to the container. Every
console `lmer` attaches (`start --attach`, `attach`) uses
`--detach-keys=ctrl-],ctrl-\` instead of the runtimes' Ctrl-P Ctrl-Q, which
swallowed input (Ctrl-P is history-up in most TUIs). In an `lmer` session,
**Ctrl-] then Ctrl-\ detaches**.

**A console you attach by hand uses your runtime's own default.** Pass
`--detach-keys 'ctrl-],ctrl-\'` to `podman attach` / `docker attach`, or set it
once: `detachKeys` in `~/.docker/config.json`, or `detach_keys` in
`containers.conf`.

## Labels

Every session container carries:

| Label | Value |
|---|---|
| `io.lmer.managed` | `1` |
| `io.lmer.launcher` | `platform` or `cli` |
| `io.lmer.repo.host`, `io.lmer.repo.project` | the run's repository, when known |
| `io.lmer.run.slug` | the run slug (platform launches) |
| `io.lmer.platform.session` | the platform's session id (platform launches) |
| `io.lmer.platform.instance` | which platform started it, a hash of its user and state directory, so two platforms sharing one runtime keep to their own (platform launches) |

Labels never carry a secret: anyone who can list containers can read them.

## Names

Every session container gets a readable `--name`, so `<runtime> ps` shows which
run a container belongs to. Discovery, reattach, exit and stop go by label and
id. Names are used in only two places: the conflict check below, and an
operator's `lmer follow <name>`.

| Launch | Name | Example |
|---|---|---|
| Platform | `lmer-<run slug>-<session id's trailing hex>` | session `s-20261009T152516-3fa9c1d2e4b7` of run `chat-mr-389` → `lmer-chat-mr-389-3fa9c1d2e4b7` |
| CLI | `lmer-<run slug>-<6 random hex>` | `lmer-develop-issue-444-3fa9c1` |
| Platform, no slug (no task) | `lmer-<session id>` | `lmer-s-20261009T152516-3fa9c1d2e4b7` |
| CLI, no task (`--no-task`) | `lmer-<6 random hex>` | `lmer-3fa9c1` |

The slug is the one the platform labels the run with. Without one (a CLI run),
it is the slug the container derives for its own run directory
(`run_state.derive_slug(task, target)`). Only a run with no task has no slug.

- **Charset and length.** Anything outside `[a-zA-Z0-9_.-]` becomes `-`, and
  names are capped at 63 characters. Only the slug is cut; the suffix never is.
  A session id lends the suffix at most its last 12 hex digits.
- **Uniqueness.** Stopped CLI containers are never removed automatically, so a
  slug alone would collide on a rerun. The suffix makes a collision unlikely.
  If `create` still fails and the runtime reports a container already holding
  that name, `lmer` retries once with a fresh random suffix and says so. It
  asks the runtime (`inspect --type container <name>`) instead of reading
  `create`'s error text, whose wording neither runtime promises.

### Finding a run's container

- By name: `<runtime> ps -a --filter name=lmer-<run slug>` finds that run's
  containers, and any other container whose name contains that text; for an
  exact match use the label filters below.
- By label, exactly: `<runtime> ps -a --filter label=io.lmer.run.slug=<slug>`
  (platform launches), or `--filter label=io.lmer.platform.session=<session id>`.
- In the control UI, the run's overview shows the name in the **live session**
  block, beside the session id, pid, start time and log.
- `lmer follow <name>` re-attaches by name as well as by id. The detach notice
  prints the name.

## Closing the terminal no longer ends a CLI session

This is a behaviour change. A plain `lmer` session keeps running when its
terminal closes or its `lmer` process is killed, and so does a detach
(Ctrl-] Ctrl-\, which also hands the terminal back).

- Re-attach with `lmer follow <container>` (its name or id). It replays nothing by default;
  `--since <RFC 3339 time>` replays the container's own log from that point.
  It only follows containers labelled `io.lmer.managed=1`, since following ends
  in a removal.
- **A detach costs a thread-attached session its thread.** When this `lmer`
  hosts a Slack or Matrix thread connection or the `lmer-signal` thread
  service, those end with it, and `lmer follow` does not restart them. The
  agent keeps working but no longer hears or reaches the thread. The detach
  notice says so.
- Once a CLI session's container has exited, it stays as a stopped, labelled
  container until someone follows it (which prints its code and removes it) or
  removes it by hand: `<runtime> rm <container>`. Nothing removes CLI
  containers automatically.
- **A stopped container still holds the session's secrets.** `create` copies
  the environment into the container's configuration (`<runtime> inspect` shows
  it as `Config.Env`). `--rm` used to discard that copy at exit; now it lives
  until the container is removed. Clean up stopped CLI containers for that
  reason. The same goes for a platform container left behind by a killed
  follower, until the next daemon start.
- `<runtime> ps -a --filter label=io.lmer.launcher=cli` lists them.

## Platform sessions and daemon restarts

The platform passes four host-only variables to the `lmer` it spawns:
- `LMER_PLATFORM_SESSION_ID`, `LMER_PLATFORM_RUN_SLUG` and
  `LMER_PLATFORM_INSTANCE` become labels, and the slug and session id make up
  the [name](#names).
- `LMER_PLATFORM_EXIT_FILE` is where the follower records the exit code before
  it removes the container. If that write fails, the follower leaves the
  container for the daemon to read.

Once `create` succeeds, `lmer` reports the container's id and name in the
session facts file (`LMER_PLATFORM_PORTS_FILE`, which also carries the
published ports, model and harness). The daemon folds them into the session's
registry entry as `container_id` and `container_name` when it next builds the
fleet view (`spawn.absorb_ports`). A reattach records both again from
`inspect`. `GET /api/state` serves only `container_name`, in the run's
`session` block; the id stays host-side.

The facts file is used rather than a label query because the fleet view is
polled. Asking the runtime would cost a `ps` and an `inspect` per session on
every poll until the container exists, which can take minutes of image build
and clone. Reading the facts file is something that path already does.

A platform follower that loses its console keeps waiting, because the platform
needs the exit code from it. Until the next daemon start it is blind: the host
log stops growing. Sessions from images that write their own log are
unaffected (`session_io.canonical_log`).

A follower is spawned on a PTY but has no controlling terminal, so a daemon
restart does not end it. That is why the new daemon ends the old follower
before following again.

At startup the daemon lists `launcher=platform` containers, matches each one to
its registry entry through `io.lmer.platform.session`, and handles it by state:

| Container state | What the new daemon does |
|---|---|
| Running | Ends the previous daemon's follower if it is still alive, identified by boot id and start time. Then it starts a new `lmer follow` in its own PTY, replaying the output since the session log last grew. The session is not marked detached: the log, the exit code and the exit verb all work. |
| Exited | Replays the output the container printed since the session log last grew, records the real exit code from `inspect`, appends a closing line to the log, and removes the container. |
| Gone, exit file present | The old follower saw the exit and removed the container while no daemon was up. The exit file holds the code, and the daemon records that. The output printed after the restart cannot be replayed in this case, because it went with the container. |
| Never started (`created`, `configured`) | Left alone while its session is live (its `lmer` is between create and start); otherwise removed, since it holds the session's environment and will never run. |
| Stopping (podman), restarting, paused | Not an ending: followed again like a running one (or, with no registry entry, stopped gracefully). Only `exited` and `dead` are endings. |
| No registry entry | This platform's own orphan. A running one is stopped gracefully and removed; an exited one is removed. |
| No container | A session from before this change: control-plane recovery as before (`lmer_platform.reattach`). |

The platform never removes a `launcher=cli` container, nor one carrying another
platform's instance label. It checks the labels again on the inspected record
before acting.

The exit verb and the assistant's stop end the follower, then stop and remove
the container in the background. That stop uses the container's stop timeout,
so the session-end backstop gets its time, and the exit route does not block
on it. If the background stop fails, the next daemon start finds the container
untracked and stops it again.

If the runtime's log driver cannot replay output (e.g. docker's `none`), the
session log records the gap instead of failing the follow.

The replay is approximate:
- It starts at the second the host log last grew, so up to a second of output
  can appear twice.
- Output printed between the replay and the new attach is lost.
- Under Docker Desktop or podman machine, clock skew with the VM can shift the
  window.

## Docker and podman

**Expected, pending the probe.** Every verb used here is documented with the
same meaning on both runtimes: `create --name`, `start --attach`, `attach`,
`wait`, `inspect`, `ps --filter label=`, `stop`, `rm` and `logs --since`. None of this
has been measured on a real runtime yet.

`python3 scripts/probe_detached_container.py podman docker` checks the
behaviour lmer relies on, and should be run on any host before relying on it.
It ends with a pass/fail summary per runtime. It checks:
- survival of a dead console plus output, with the output provoked after the
  console died;
- stdin staying open;
- a terminal hangup;
- the detach keys on both `start` and `attach`, recording each exit code;
- `logs --since` in lmer's timestamp format;
- `wait` codes;
- a plain `stop` honouring `--stop-timeout`;
- `kill` plus `rm --force` being immediate;
- the state a refused start leaves;
- not-started state names;
- ANDed label filters;
- container names: a taken name fails `create`, `inspect --type container
  <name>` answers whether a name is taken, `inspect` reports the name, and a
  `name=` filter matches a prefix.

Removals meant to be immediate run `kill` before `rm --force`: podman's
`rm --force` honours the container's stop timeout, and docker's `rm` takes no
`--time`.

One difference is handled by capability rather than by name: `--sig-proxy`
exists on podman's `start` and `attach` and on docker's `attach`, but not on
docker's `start`. lmer asks the runtime's `--help`. The existing differences
(podman's `--userns=keep-id`, and inspect field spellings) stay where they were.

The platform-container topology drives the docker CLI against podman's compat
socket, which is expected to serve the same verbs. That is not verified.

## macOS

The same path works with Docker Desktop and podman machine. Survival comes from
the container being detached.

What does not carry over is ending the previous daemon's follower, which needs
`/proc` to prove the pid is still that process. On macOS both followers run
after a restart. Whichever sees the exit first records the code in the exit
file before removing the container, and the other one reads it from there.

## Running the daemon under systemd

Restart survival depends on the unit's `KillMode`. Followers are started in
their own sessions but stay in the daemon's cgroup, so with the default
`KillMode=control-group` a `systemctl restart` sends every follower SIGTERM —
and a follower stops its container on SIGTERM. Use `KillMode=process`.

`mixed` SIGKILLs the followers, which leaves the containers running only when
the runtime's container processes are outside the unit's cgroup: true for
docker's shims and for podman with the systemd cgroup manager, not for podman
with the cgroupfs manager.

## Not covered yet

**Adopting containers the platform did not start.** Labels make CLI sessions
discoverable, but controlling one from the platform also needs:

- a control-plane token hand-off, since a token cannot go in a label;
- a rule for who removes the container;
- the run slug;
- an "adopted" mark in the registry and UI.

This is planned as a follow-up.
