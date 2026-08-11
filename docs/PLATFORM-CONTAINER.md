# Platform Container Image

Run the orchestrator platform — the FastAPI control plane and its Vue UI —
as a container image instead of installing it on the host. Built from
`Dockerfile.platform`; the UI bundle is built during the image build and
baked in, so deploying an upgrade is a pull rather than a Node toolchain
fetch and a `lmer platform setup-ui`.

> **Status: spike.** The image builds and serves, and the invariants below
> are derived from the code that reads them, but the topology has not been
> through a full session lifecycle in anger yet. One limitation is known and
> unfixed today — see [Restart semantics](#restart-semantics-and-what-breaks-today).
> The runbook at the end is what confirms or refutes the rest.

## What this image is

| Artifact | Builds | Contains |
|----------|--------|----------|
| `Containerfile` | the **session** image (`lmer:<version>`) | Oracle Linux FIPS base, harness CLIs, browser, gate tooling — what agents work in |
| `Dockerfile.platform` | the **platform** image | slim Python, `git`, the docker *client*, lmer installed, the prebuilt UI |

They are separate artifacts on purpose. The platform serves HTTP and starts
session containers through the host's runtime socket; it needs none of the
session image's toolchain, and a slim Python is its whole runtime.

Against a bare-host install, the difference is only in how the platform
itself is deployed:

- **Bare host** — `uv tool install lmer`, then `lmer platform setup-ui`
  (fetches a pinned Node into `~/.lmer/platform/node`, builds `web/`,
  installs the bundle to `~/.lmer/platform/ui`), then `lmer platform run`.
- **This image** — `docker run …`, and the bundle is already there
  (`LMER_PLATFORM_UI_DIST=/opt/lmer/ui`, preferred over the state-dir copy
  by `lmer_platform.ui_build.dist_dir`).

Everything else is unchanged: the same state directory, the same session
images, the same `lmer` invocations. The platform in a container still
starts sessions as ordinary containers **beside** itself — never nested.

## Quick start

```bash
# 1. Build (BUILD_UID/BUILD_GID must match the user that owns ~/.lmer)
docker build -f Dockerfile.platform -t lmer-platform:dev \
    --build-arg BUILD_UID="$(id -u)" --build-arg BUILD_GID="$(id -g)" \
    --build-arg LMER_BUILD_COMMIT="$(git rev-parse HEAD)" .

# 2. Run, via the helper that encodes the invariants below
scripts/platform-container-run.sh --image lmer-platform:dev

# See exactly what it would run, without running it
scripts/platform-container-run.sh --image lmer-platform:dev --print
```

The helper is deliberately small and readable: it derives the host home,
uid/gid and socket, and prints the command it runs. It is spike-stage
tooling, not a supported deployment unit — read it before you trust it, and
copy what you need into whatever supervises the container for real
(systemd unit, Compose file).

## The path-identity invariant

**The platform container's `$HOME` must be the host's `$HOME`, and
`$HOME/.lmer` must be bind-mounted at that identical absolute path.**

This is the one rule the whole topology stands on. The reason:

1. `lmer_cli.runtime.lmer_state_dir()` is literally `Path.home() / ".lmer"`
   (`src/lmer_cli/runtime.py:37`). There is no environment override, and
   that is deliberate — see below.
2. When the containerized platform spawns a session it runs `lmer`, and
   that `lmer` builds `docker run -v <host-path>:<container-path>` argument
   lists from paths it derived from `Path.home()`.
3. Those `-v` arguments are resolved by the **host's** daemon, over the
   mounted socket. The host has never heard of the platform container's
   filesystem.

So every path lmer derives inside the platform container is a promise about
the host, and a mount that renames it makes the platform lie in a way
nothing checks. Mount `~/.lmer` at `/home/developer/.lmer` while `HOME` is
`/home/developer` and the platform works fine right up until it spawns a
session — at which point the session's `-v` arguments name
`/home/developer/.lmer/...`, and:

- if the host has no such path, the daemon **creates the directories**
  (root-owned, empty). The session starts, sees an empty ask channel, an
  empty transcript directory and an empty session-log directory, and the
  platform reads its own — separate, still empty — copies. Nothing errors.
- if the path is a bare name rather than absolute, Docker and Podman read
  it as a **named volume** and helpfully create one. Same silence.
- if the host *does* have such a path (an actual `developer` user), the
  session's files land in a stranger's home.

None of the three is a crash, which is why this section is shouting. The
failure mode is a fleet of sessions whose logs, transcripts and ask
channels are written to one place and read from another.

### Three config fields that point outside the mount

`~/.lmer/platform/config.json` arrives intact on the state mount — including
three fields that persist an absolute **host** path and override a state-dir
default. A value under `~/.lmer` rides the mount and is fine. A value outside
it names a path this container does not have, and not one of the three says so:

- **`lmer_bin`** (`src/lmer_platform/config.py:211`) — the executable the
  daemon spawns per session. `spawn.resolve_lmer_bin`
  (`src/lmer_platform/spawn.py:564-581`) returns it verbatim, so a path into a
  host checkout (`/home/you/src/lmer/.venv/bin/lmer`) makes **every spawn fail
  with ENOENT**, surfacing from a drain thread rather than at the call site.
  Removed, `lmer` is found on the image's `PATH` — which is the copy this image
  was built with, and the one you want.
- **`secret_file`** (`config.py:208`, read by `secret_path`,
  `config.py:235-240`) — where the shared secret lives. An unmounted path is an
  empty path in here, so `ensure_secret` **mints a new secret** and every client
  holding the old one is refused. Nothing errors: minting one is exactly what a
  first run does.
- **`work_repo_mirror`** (`config.py:213`, read by `mirror_path`,
  `config.py:242-246`) — the mirror
  clone the fleet view is read from. An unmounted path is **re-cloned into the
  container's ephemeral filesystem** on every start: a full clone per restart,
  thrown away with the container, while the host's mirror sits untouched.

Two of them have environment equivalents that beat the file and have the same
problem — `LMER_PLATFORM_SECRET_FILE` and `LMER_PLATFORM_WORK_REPO_MIRROR`.

So for each of the three: either mount the path it names **path-identically**
(`--mount-rw` on the helper) or remove the field from `config.json` before
containerizing, and let the default under `~/.lmer` take over. The helper checks
all three against the mounts it is actually making and prints a note naming the
field, the path and the predicted failure; it also declines to forward those two
variables when the path they name is not covered by a mount. Worth reading
those notes rather than skimming past them: the state dir most likely to carry
these fields is one that has already served a bare-host platform, which is the
state dir [the runbook](#prerequisites) asks you to reuse.

### Why there is no state-dir environment variable

Adding `LMER_STATE_DIR` would not fix this; it would hide it. The path has
to be identical on both sides of the mount, so a variable would only give
the operator a second place to make them disagree. One bind mount at one
path, and `HOME` telling the truth, is the whole configuration.

## What to mount

| Host path | Container path | Mode | Why |
|-----------|----------------|------|-----|
| `$HOME/.lmer` | **same absolute path** | rw | Config, shared secret, work-repo mirror, session registry, session logs, ask channels, transcripts, container-home, clone cache, user harnesses. Also the source of every session mount the platform constructs — hence path-identical. |
| the runtime socket | `/var/run/docker.sock` | rw | Starting session containers. See [the socket](#the-container-runtime-socket). |
| `$HOME/.claude`, `$HOME/.claude.json` | same paths | ro | Harness credentials for spawned sessions (below). |
| `$HOME/.codex`, `$HOME/.pi` | same paths | ro | Same, for the other built-in harnesses. |
| `$HOME/.ssh`, `$HOME/.gitconfig` | same paths | ro | The platform's own `git` (work-repo mirror over SSH) and the first-run seeding of `~/.lmer/container-home`. |
| `$SSH_AUTH_SOCK` | same path | rw | Only if you use agent forwarding — see the caveat below. |

`HOME` is passed as the host's home (`-e HOME=…`), the container runs
`--user <uid>:<gid>` matching the `BUILD_UID`/`BUILD_GID` the image was
built with, and networking is `--network=host`. The helper does all of it.

The table is the *default* install. What a config file can add to it is
[three fields that point outside the mount](#three-config-fields-that-point-outside-the-mount)
— `lmer_bin`, `secret_file`, `work_repo_mirror` — each of which has to be
mounted path-identically or removed before this topology is honest.

Credentials for git, incidentally, do not have to be mounted at all: the daemon
loads `~/.lmer/.env` at startup (`_load_env_files`,
`src/lmer_platform/daemon.py:584-601`, via `apply_env_file_defaults` in
`src/lmer_cli/cli.py:257-279`), so `LMER_WORK_REPO`, `LMER_WORK_REPO_TOKEN` and
`GITLAB_TOKEN*` put there ride the state mount and need no `-e` at all. Exported
in the invoking shell instead, the helper forwards them **by name** — the value
is copied by the runtime and never appears in an argument list.

### Credential paths outside `~/.lmer`

Most of what lmer reads is already under the state directory —
container-home (`~/.lmer/container-home`), the clone cache
(`~/.lmer/clone-cache`), user-harness definitions and their install cache
(`~/.lmer/harnesses`, `~/.lmer/harness-cache`), and everything the platform
itself owns (`~/.lmer/platform/...`). One bind mount covers all of it.

Container-home lands there rather than in a checkout because the image
installs lmer non-editably, so install mode is INSTALLED, `repo_root` is
`None`, and the container-home base falls back to the state dir
(`src/lmer_cli/cli.py:1875`). Mount a checkout over the install and that
stops being true — which is one more reason this image is not the place to
develop lmer.

Harness credentials do not. `plan_credential_mounts`
(`src/lmer_cli/mounts.py:231-283`) resolves each entry as
`Path.home() / cred.host_path` and **checks that it exists** before
mounting it, so the file has to be visible *inside* the platform container
or the session is started without it:

| Path | Declared at |
|------|-------------|
| `~/.claude/.credentials.json` | `src/lmer_cli/harness.py:230` |
| `~/.claude.json` | `src/lmer_cli/harness.py:231` |
| `~/.codex/auth.json` | `src/lmer_cli/harness.py:261` |
| `~/.pi/agent/auth.json` | `src/lmer_cli/harness.py:297` |
| `~/.pi/agent/models.json` | `src/lmer_cli/harness.py:303` |

The `.claude` mount covers one more read on the platform's own side: the
default transcript root it resolves a recorded pointer against is
`~/.claude/projects` (`transcripts.transcript_root`, overridable with
`LMER_PLATFORM_TRANSCRIPT_ROOT`). Read-only is right there too — a
session's transcripts are mounted in from `~/.lmer/platform/logs/`, so
nothing writes to that root.

Mounted read-only, and read-only is correct: the platform container only
*stats* these files. The session's own mount is `rw` where the harness
refreshes tokens (`CredentialMount.mode`), and that mount is created by the
host daemon from the host path — independent of how the platform container
sees it.

The *shape* of each mount is the containing directory where the credential has
one (`.claude/`, `.codex/`, `.pi/`, `.ssh/`) and the file itself where it does
not (`.claude.json`, `.gitconfig`). Directories are preferred because a
credential rewritten via temp-file-and-rename replaces the inode, which a file
bind mount pins to the old one — but the rename happens *inside* the mounted
directory, so mounting the directory sees each new inode as it appears. The two
file mounts are safe here for the reason above rather than by luck: nothing in
the platform container reads their contents, it only checks that they exist, and
the session's own mount is resolved by the host daemon from the host path. If
you find yourself wanting to *read* a mounted credential file in here, that
reasoning stops holding and the mount should become its directory.

More paths outside `~/.lmer` are opt-in and yours to add
(`--mount-ro` / `--mount-rw` on the helper, which mount path-identically):

- **User-installed harnesses** may declare any home-relative credential
  file in their manifest (`credential_mounts`), and the guard only requires
  a regular file resolving under the host home. If yours keeps credentials
  outside `~/.lmer`, mount that path too.
- **`LMER_TASKDEF_PATHS`**, **`LMER_PRESETS_FILE`**, `--mount-file` /
  `--mount-dir` and `LMER_MOUNT_FILES` / `LMER_MOUNT_DIRS` are all
  existence-validated in-process before becoming a session mount. Whatever
  they point at must be visible at the same path.
- **`LMER_CLONE_CACHE_DIR`**, if you moved the clone cache out of
  `~/.lmer`: mount it **rw**. The mirror maintenance runs as a fork of the
  spawning `lmer` — inside the platform container — and writes to it
  (`resolve_host_clone_cache_dir`, `src/lmer_cli/mounts.py:611-655`). The
  default under `~/.lmer` is already covered.
- **`~/.cache/uv`** (or `UV_CACHE_DIR` / `XDG_CACHE_HOME`), if you set
  `LMER_MOUNT_UV_CACHE`: it is existence-checked in-container
  (`resolve_host_uv_cache_dir`, `src/lmer_cli/mounts.py:565-587`), so
  without the mount the check silently declines and the session gets no
  cache. The helper handles this one when the variable is set.
- **Whatever `lmer_bin`, `secret_file` or `work_repo_mirror` names**, if your
  `config.json` sets them outside `~/.lmer` —
  [above](#three-config-fields-that-point-outside-the-mount) for what each one
  breaks when it is not mounted.

### Rough edges of `HOME` without the home directory

Only `~/.lmer` and the paths above are mounted, so the rest of `$HOME`
inside the platform container is whatever the runtime created to hold the
mount points — root-owned and unwritable by the container user. Nothing
lmer does needs to write elsewhere under `$HOME` today, but a tool that
decides to (`git` appending to `~/.ssh/known_hosts`, a CLI writing
`~/.config/...` outside the state dir) will fail with a permission error
rather than something legible. Worth knowing before you read one.

Reading it is fine, and something does: the detached clone-cache updater is
started with `cwd=Path.home()` (`src/lmer_cli/cli.py:1523`), deliberately —
"a trusted, always-present cwd". That holds here only because the runtime
created the directory to hold the `~/.lmer` mount point. It is the closest
thing to a canary for this section: if `$HOME` inside the container ever
turns out not to exist, that is the code that notices first.

The `$SSH_AUTH_SOCK` mount has a second caveat: the socket path belongs to
a login session on the host, so it goes stale when that session ends and
the mount then points at nothing. Fine for a spike, not something to bake
into a unit file.

## Networking

Run with **`--network=host`**. Three separate reasons, all of them about
loopback:

1. **Session control planes publish on host loopback.** Every session's
   control plane is published as `-p 127.0.0.1:<port>:<port>`
   (`_publish_host_ports`, `src/lmer_cli/cli.py:191-204`), and the platform
   dials `127.0.0.1:<port>` to type into a session
   (`_DEFAULT_CONTROL_HOST`, `src/lmer_platform/session_io.py:272`). In its
   own network namespace the platform would dial itself.
2. **Free ports are picked by binding.** The spawning `lmer` picks a
   session's control port by probing for a free one on `127.0.0.1` before
   the container starts. In a private namespace it would probe the wrong
   loopback and hand out ports the host has already published.
3. **The UI is reachable the way it is on a bare host.** In the host
   namespace, a loopback bind (the default, spec D14) is *host* loopback —
   so `http://127.0.0.1:8600` works from a browser on the host, and
   remote access is the same reverse-proxy question as ever.

With `--network=host` the image's `EXPOSE 8600` and any `-p` are moot: the
daemon's own bind address is the only thing that decides where it listens.
That stays an opt-in — `LMER_PLATFORM_BIND_ADDRESS` (or `bind_address` in
`config.json`) — and a non-loopback bind serves **plaintext**, which the
startup notice says out loud (`binding_notice`,
`src/lmer_platform/config.py:564`).

Weigh that opt-in against what this container holds: a read-write runtime
socket, which is [root-equivalent on the host](#the-container-runtime-socket).
Binding `0.0.0.0` here does not merely expose a control plane in plaintext — it
means anyone who can reach the port and present the shared secret (sent in
clear, over a network that can read it) can create containers on this host as
root. On a spike host behind nothing, prefer loopback plus an SSH tunnel.

### Sessions dialing back to the platform

One session type talks back to the API: the assistant, which is handed
`LMER_PLATFORM_URL` and the shared secret in its environment. That URL
comes from `container_base_url` (`src/lmer_platform/config.py:860`), which
answers "where can a container *this host starts* reach the platform" — and
a session container is a normal bridged container, **not** in the host's
network namespace. So host networking for the platform changes nothing
here: a loopback bind still leaves the assistant with a stated reason
instead of a URL, and the ways out are the same as on a bare host — bind a
routable address, bind the wildcard on docker (the default bridge's gateway
is then derived from the runtime, which works from inside this image
because the docker client talks to the host daemon), or set
`LMER_PLATFORM_CONTAINER_URL` explicitly.

`LMER_PLATFORM_CONTAINER_URL` accepts a loopback URL, deliberately
(`_override_reach`, `src/lmer_platform/config.py:833`): it is wrong for
every container lmer starts today, but it is exactly what an operator whose
sessions run in the host's network namespace would correctly write, and
second-guessing an explicit setting is how an escape hatch stops being one.
Which is to say: it is the right knob if you make the *sessions* host-network
too, and the wrong one otherwise.

## The container runtime socket

Mount the host's runtime socket **rw at `/var/run/docker.sock`** and pass
`--group-add <socket gid>`. Both are the conventions service mode already
uses, including for Podman hosts, and the reasoning is written up once in
[SERVICE-MODE.md](./SERVICE-MODE.md) ("Runtime Compatibility", "Socket
permissions") rather than restated here. The short version: the image ships
`docker-ce-cli` only, Podman's socket speaks a Docker-compatible API, and
mounting whichever socket the host has at that one path means nothing
inside the container needs runtime-conditional logic.

Say the size of that grant plainly: **read-write access to the runtime socket is
root-equivalent on the host.** Anything that can talk to it can start a
privileged container that bind-mounts `/`, which is the whole filesystem, as
root. That is inherent to a platform whose job is starting containers — it is the
same grant the bare-host install has, where the invoking user is in the socket's
group — but in a container it is easy to read the isolation as a boundary, and it
is not one. `--security-opt no-new-privileges` in the helper is hygiene inside
the container and nothing more. So: who can reach the API, and over what, is a
question about host root. See
[the bind address](#networking) for the one setting that widens it.

One divergence specific to *this* image, which service mode does not have:
`lmer_cli.runtime.detect_runtime()` picks a runtime by looking for a binary
on `PATH`, and this image carries only the docker client. On a Podman host
the in-container `lmer` therefore builds **docker-flavoured** run arguments
(no `--userns=keep-id`) while a Podman engine executes them. Sessions on a
Podman host are untested for that reason; treat them as part of the spike.

SELinux is *not* part of that divergence, deliberately.
`lmer_cli.runtime._is_selinux_enforcing()` reads `/sys/fs/selinux/enforce`
rather than shelling out to `getenforce`, which this image does not carry
(`selinux-utils` is not installed). The kernel exposes selinuxfs inside the
container on an enforcing host, so the sessions this daemon spawns get
`--security-opt label=disable` and the `,z` relabel suffix on their bind
mounts there just as a bare-host install would — an enforcing host would
otherwise fail every spawned session on AVC denials against `/workspace`
while the platform container itself ran fine.

## Upgrading

```bash
docker pull <registry>/lmer-platform:<tag>   # or rebuild
docker stop lmer-platform && docker rm lmer-platform
scripts/platform-container-run.sh --image <registry>/lmer-platform:<tag>
```

State lives entirely in the mounted `~/.lmer`, so the container is
disposable and no migration step exists. The session image is a separate
upgrade: the spawning `lmer` resolves its tag from *its own* package
version (`lmer:<version>`, `resolve_image_tag`) unless `LMER_IMAGE` says
otherwise, so a platform image built from a newer commit will start looking
for a session image tag the host may not have. It will try to pull it. Pin
`LMER_IMAGE` if you want that decision to be yours.

### Restart semantics, and what breaks today

On a bare host, sessions survive a daemon restart. Each session has a
host-side `lmer` client process that owns the PTY master, and those
processes are not children of the daemon in any meaningful sense — the
daemon restarts, re-attaches, and the fleet carries on (spec R11, T36).

In this topology those client processes live **inside the platform
container**, in its PID namespace. So every platform upgrade — every
`docker restart`, every stop/run — kills all of them. Two consequences,
one benign and one not:

- **Scrollback survives.** The canonical session log is written by the
  supervisor *inside the session container*
  (`lmer_cli.supervisor.SessionLog`) into a directory mounted from
  `~/.lmer/platform/logs/<id>.session/`, and the read path prefers it when
  it holds anything (`session_io.canonical_log`). The host-side tee dying
  costs nothing for a session whose image writes its own log.
- **Re-attach does not, yet.** `reattach_all`
  (`src/lmer_platform/reattach.py:814`) is scoped to
  `registry.list_sessions(live_only=True)`, and liveness is
  `kill(pid, 0)` plus a `/proc` zombie check against the pid recorded in
  the registry entry (`registry.is_live`,
  `src/lmer_platform/registry.py:160-188`). Those pids were minted in the
  *previous* container's PID namespace. In the new namespace they are
  almost all absent — so the whole fleet is skipped, quietly, and
  `control_endpoint` refuses input for each session before it even reads
  the port (`src/lmer_platform/session_io.py:708`). Worse and less likely:
  a recycled pid number matches something unrelated in the new namespace
  and a dead session reads as live.

**So today, after restarting the platform container, expect every session
to become read-only.** The containers are still running and their logs
still grow; the platform just will not type into them. Confirming that
prediction is step 5 of the runbook.

The fix is pass 2, deliberately after the spike rather than before it:
stamp registry entries with an epoch identifying the platform incarnation
that wrote them, and for entries from a foreign epoch prefer the session's
own `/healthz` probe over the pid check — the session control plane answers
it for as long as it is up, and it is namespace-independent. Nothing in
this doc should be read as claiming that already works.

## Spike runbook

The point of the walk is to confirm or refute seven predictions with
observations, not to demonstrate that it works. **Record raw output per
item** — the log lines, the JSON, the arguments — rather than a verdict;
a "worked" with no paste is not usable by whoever implements pass 2.

### Prerequisites

- Docker (or Podman, knowing the caveat above) reachable by the invoking
  user, and the session image present on the host or pullable.
- A working bare-host platform state directory at `~/.lmer` — ideally one
  that has already served sessions, so the registry is not empty.
- `BUILD_UID`/`BUILD_GID` for the build equal to the uid/gid that owns
  `~/.lmer`.
- The work-repo URL configured (`work_repo_url` in
  `~/.lmer/platform/config.json`, or `LMER_WORK_REPO`), or the fleet view
  comes back empty and reads as a bug.
- **No bare-host `lmer platform run` at the same time.** One state
  directory, two daemons, one registry — do not.

### Walk

1. **Build** the image with the command in [Quick start](#quick-start).
   Note the digest and the commit you built from.
2. **Inspect the command** first: `scripts/platform-container-run.sh
   --print`. Check by eye that `$HOME/.lmer` appears on **both** sides of
   its `-v`, that `HOME` is your host home, and that `--user` matches
   `id -u`/`id -g`. Keep the notes it writes to stderr — an unmounted
   `config.json` path is named there, and that is checklist item (g).
3. **Start it**, then `docker logs -f lmer-platform`. Expect the bind
   notice (`🛰  Platform listening on …`), the secret hint, possibly the
   mirror notice — and **no** "Control UI not built" line. Then open the UI,
   or reach the API:

   ```bash
   SECRET="$(docker exec lmer-platform lmer platform secret)"
   curl -sS -u ":$SECRET" http://127.0.0.1:8600/api/health
   ```

4. **Spawn a session** from the UI (or `POST /api/sessions`), and use it:
   the terminal view must stream, and typed input must land. Note the
   session id.
5. **Restart** — `docker restart lmer-platform` — and watch the logs
   through startup. Then re-open the same session.
6. **Work the checklist** below against what you just saw.

### Checklist

Each item names the prediction, where to look, and what to record.

**(a) The fleet is skipped on re-attach (expected to FAIL today).**
Prediction: after step 5, no session re-attaches; the terminal still shows
growing scrollback but input is refused.
Where: the daemon log at startup — absence of the `🔌 N session(s)
survived the last daemon` notice (`reattach.startup_notice`) is the signal,
and `platform_reattach_failed` lines if anything raised instead. Then
`POST /api/sessions/<id>/input` and record the status and message (expect
`session … is not running (its process is gone)`). Compare the `pid` in
`~/.lmer/platform/sessions/<id>.json` against `docker exec lmer-platform
ps -ef` **and** the host's own `ps -ef` — record all three. If a recorded
pid *does* match a live process in the new container, say so loudly: that
is the collision case, and it is worse than the skip.

**(b) A path outside `~/.lmer` is read and not mounted.**
Prediction: the mount set in this doc is complete for a default install.
Where: `docker inspect` the session container the platform started and dump
its `Mounts` — every `Source` must be a host path that exists **with
content**. Then `docker exec lmer-platform ls -la "$HOME"`: any root-owned
directory in there that is not one of the mount points is a path the
runtime invented for a mount, and each one is a gap in this doc's table.
Look for the same on the host, under `~` and wherever a spawn touched.
Record the whole listing.

**(c) Credential mounts reach the spawned session.**
Prediction: the session authenticates exactly as a bare-host session does.
Where: the spawn's own output/log (`~/.lmer/platform/logs/<id>.log`) — the
`🔑` announce lines and any `⚠️  User harness …` skip warnings — plus the
`Mounts` from (b) filtered to the credential paths. Then confirm from
inside the session that the harness is authenticated. If a credential path
is missing from the mount list, record which harness declared it.

**(d) The UI is served from the baked bundle.**
Prediction: `/opt/lmer/ui`, not a state-dir copy, not "unbuilt".
Where: `docker exec lmer-platform sh -c 'echo "$LMER_PLATFORM_UI_DIST"; cat /opt/lmer/BUILD_INFO; ls /opt/lmer/ui'`,
and the absence of the "🖼  Control UI not built" startup line. Cross-check the commit in
`BUILD_INFO` against what you built. If `~/.lmer/platform/ui` also exists
from an earlier bare-host `setup-ui`, note it: the baked bundle must win
(`ui_build.dist_dir` prefers the variable), and this is the case that
proves it.

**(e) A session that dials back reaches the platform.**
Prediction: with the default loopback bind, it does **not** — the
assistant is told why instead of being given a URL.
Where: start the assistant (`POST /api/assistant/start`) and read
`~/.lmer/platform/assistant.env` for `LMER_PLATFORM_URL` /
`LMER_PLATFORM_UNREACHABLE`; record the exact reason text. Then set
`LMER_PLATFORM_BIND_ADDRESS=0.0.0.0` (plaintext, and this container holds a
socket that is [root-equivalent on the host](#the-container-runtime-socket) —
a spike host reachable by nobody else, only), restart, and record which rule
produced the URL (`source=bridge-gateway` on docker) and whether the
assistant's requests actually arrive. If you override with
`LMER_PLATFORM_CONTAINER_URL`, record the value and that the loopback shape was
accepted.

**(f) The clone-cache updater writes to the mounted cache.**
Prediction: mirror maintenance forked by the in-container `lmer` lands in
the host's cache, not in the platform container's filesystem.
Where: `ls -la ~/.lmer/clone-cache/` on the **host** before and after a
spawn (timestamps, new mirror directories) and
`~/.lmer/logs/clone-cache.log`. Record ownership of anything new — a
root-owned entry means the path was created by the runtime for a mount and
not by the updater. If you moved the cache with `LMER_CLONE_CACHE_DIR`,
record whether you mounted it `rw` and what happened either way.

**(g) `config.json`'s host-path fields are covered by the mounts.**
Prediction: a state dir that has served a bare-host platform may set
`lmer_bin`, `secret_file` or `work_repo_mirror` to a host path outside
`~/.lmer`, and each one then fails as
[described above](#three-config-fields-that-point-outside-the-mount) — ENOENT
on every spawn, a newly minted secret, a mirror re-cloned into the container.
Where: read `~/.lmer/platform/config.json` and record those three fields
verbatim (absent, or `null`, is the answer you want). Record the helper's
notes about them from step 2, including whether it declined to forward
`LMER_PLATFORM_SECRET_FILE` / `LMER_PLATFORM_WORK_REPO_MIRROR`. If a field is
set: `docker exec lmer-platform ls -la <path>` — missing or empty is the
prediction — and then compare `docker exec lmer-platform lmer platform secret`
against the host's `cat ~/.lmer/platform/secret`. Two different values is the
re-minted case, and it is the explanation for any client that suddenly gets a
401.

Anything the walk turns up that is not on this list is the most valuable
part of it. Write it down verbatim.

## Non-goals

- **Session containers are unchanged.** Nothing here alters how a session
  is built, mounted or run. The platform in a container starts the same
  sessions the bare-host platform does, beside itself, through the host's
  daemon. No nesting, no docker-in-docker.
- **The bare-host install stays supported.** `uv tool install` +
  `lmer platform setup-ui` + `lmer platform run` remains the reference
  deployment, and `setup-ui` is not deprecated by the baked bundle.
- **No TLS, no reverse proxy, no orchestration.** The daemon terminates no
  TLS (spec D9) in a container any more than on a host, and the helper
  script is not a deployment unit — see [Quick start](#quick-start).
- **No new state-dir indirection.** See
  [why there is no state-dir environment variable](#why-there-is-no-state-dir-environment-variable).

## See also

- [SERVICE-MODE.md](./SERVICE-MODE.md) — the socket-mount and `--group-add`
  convention this image reuses, and the Docker/Podman socket table.
- [CONTAINER.md](./CONTAINER.md) — runtime detection, SELinux, build-time
  uid/gid handling for the session image.
- [LMER-CLI.md](./LMER-CLI.md) — the environment variables the spawned
  `lmer` reads (`LMER_IMAGE`, `LMER_CLONE_CACHE_DIR`, `LMER_MOUNT_*`,
  `LMER_TASKDEF_PATHS`).
