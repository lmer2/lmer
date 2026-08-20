# Platform quickstart

Get the lmer platform — the web control plane for a fleet of lmer sessions,
with its supervising assistant ("uber lmer") — running on a host in a few
minutes. The full reference for a container deploy is
[PLATFORM-CONTAINER.md](./PLATFORM-CONTAINER.md); this page is the short path.

## Install and first run

From a checkout of this repository:

```bash
git pull
uv tool install --force --from . lmer   # a copied install is not updated by git pull
lmer platform setup-ui                  # once per update, from the checkout
lmer platform run
```

`setup-ui` builds the web UI and installs it into the platform's state
directory (`~/.lmer/platform/`), so `lmer platform run` afterwards works from
any directory. Re-run it after every update — `npm run build` alone does not
refresh what the daemon serves.

By default the daemon binds loopback (`127.0.0.1:8600`). Open the printed URL
in a browser; it prompts for credentials — any username, and the shared secret
as the password:

```bash
lmer platform secret     # prints it
```

## Reaching it from another machine

Two options.

**Bind an interface directly** (plain HTTP, trusted networks only):

```bash
lmer platform run --bind 0.0.0.0 --port 8180
```

The ports in these examples (`8180` here and below, `7180` behind the proxy)
are arbitrary choices, not defaults — they are deliberately distinct from
`8600` so it is obvious which side of the proxy each number belongs to.

**Front it with a reverse proxy** (TLS, the recommended shape). Bind the
daemon to loopback and put nginx in front. The one thing that always gets
missed: the terminal runs over a WebSocket, and nginx forwards upgrades only
when told to — and its default `proxy_read_timeout` of 60s (or lower) will cut
a quiet terminal socket on a rhythm.

```nginx
# http{} context (top of a conf.d file, above the server block):
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 8180 ssl;
    server_name example.host;
    ssl_certificate     /etc/letsencrypt/live/example.host/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.host/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    location / {
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;

        # WebSocket upgrade (the terminal) — all three lines required
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;

        # terminal sockets are long-lived and mostly quiet
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;

        proxy_pass http://127.0.0.1:7180/;
    }
}
```

Then start the daemon on the proxied port, and tell it the address its
*containers* should use — sessions it spawns (the assistant in particular)
reach the API from inside containers, where the host's loopback does not
exist:

```bash
LMER_PLATFORM_CONTAINER_URL=https://example.host:8180 \
    lmer platform run --bind 127.0.0.1 --port 7180
```

Without the variable, a wildcard bind lets the daemon derive a container
address on its own (podman's host alias, or docker's bridge gateway); behind a
loopback bind + proxy it cannot, so the variable is required in this shape.

**Gotcha:** sessions capture the container URL at *spawn time*. If you change
it — moving behind a proxy, changing ports — a surviving assistant from the
previous daemon keeps calling the old address forever. Restart it from its
drawer (the restart button), or:

```bash
curl -X POST -H "Authorization: Bearer $(lmer platform secret)" \
    http://127.0.0.1:7180/api/assistant/rotate
```

Standing orders and the handover note survive a restart; only the
conversation window is lost.

## Using it

- **Fleet view** — every run this platform tracks, attention first. The row
  with the orange border and the robot is uber lmer itself; `chat` on that
  row (or the robot in the app bar) opens its drawer.
- **Spawn a run** — the `run` button. Taskdef + target, optionally a preset,
  a fan-out roster (`--agents`), a title. A plain clone URL is a complete target
  by itself: the platform derives the run identity from it, records that
  repository, tracks the run, and keeps the title without requiring the repo-URL
  field to repeat the same value. GitLab/GitHub web routes and generic web-page
  URLs are not treated as repository evidence; use the repo-URL field when an
  HTTP clone URL is not recognisable as a forge root or does not end in `.git`.
- **Answer a question** — a run stopped on a question shows a reply box.
  Answering a *stopped* run starts a fresh session with the answer attached;
  replying to a *live* one is delivered into the waiting session.
- **Wind down vs exit** — wind down asks the agent to wrap up and end itself;
  exit ends it now. Both under the run's detail view.
- **uber lmer** — the supervising assistant. It hears attention changes and
  session milestones (`lmer-signal`), can spawn/answer/wind-down runs on
  request, and keeps operator-taught standing orders across restarts. Teach it
  rules in plain chat ("spawn reviewers with preset X"); restart it from the
  drawer when needed.
- **uber lmer settings** — how its session is *run* (model, harness, preset,
  agents fan-out), per platform instance: the settings entry in the drawer's
  overflow menu, or `GET`/`POST /api/assistant/config`. Each value shows which
  layer decided it (an `LMER_PLATFORM_ASSISTANT_*` export beats what the
  dialog persists to `config.json`, and the dialog says so). Changes apply to
  the **next** incarnation — the running one keeps its context window until
  you restart it, and the dialog offers the restart. Standing orders are the
  chat's to edit and are deliberately not in this dialog.
- **Adopt / forget** — runs started outside the platform can be adopted into
  the fleet view (`+ run` → adopt, or `POST /api/runs/adopt`); ended runs can
  be forgotten from their card (undo window; nothing on disk is touched).
- **Service slots** — named single-occupancy bindings from a runner to a dev
  service on this host, declared under `slots` in `config.json` and shown as a
  row each under the runs list. The spawn dialog's picker offers the free ones;
  `lmer-ctl spawn … --slot <name>` takes one from a shell. Occupancy is derived
  from live sessions, so a slot frees when its session's process ends and a
  crash cannot strand one. Full reference:
  [SERVICE-MODE.md](SERVICE-MODE.md#service-slots-several-agents-one-dev-stack).
- **"stopped responding"** — a run whose session is up but whose agent stopped
  producing anything, with no stop recorded by the run itself. That is what a
  provider refusal looks like from outside: a spend limit, a `529 Overloaded`
  the retries gave up on, or any error that ended a turn the agent never came
  back from. The row moves onto the attention list and the assistant gets a
  digest, so nobody has to open a terminal to find out. Nothing is sent to the
  session — flagging is the whole behaviour.

  The note says how it was decided, and the three answers do not carry the same
  confidence: *the harness recorded a provider refusal* (the harness marks such a
  turn as one, and names the class — `billing_error`, `server_error` — so this is
  its own report rather than a reading of what the turn said), *it was given
  input and never answered*, or *flagged on silence alone* (the backstop below;
  no diagnosis is being claimed).

  | variable | default | what it sets |
  |---|---|---|
  | `LMER_PLATFORM_STALL_IDLE_SECONDS` | `600` | how long a session may be silent before the first two checks apply |
  | `LMER_PLATFORM_STALL_BACKSTOP_SECONDS` | `3600` | silence after which a run is flagged whatever its transcript says |

  Both also live in `config.json`, and **`0` turns either off** — the precise
  checks without the backstop is a coherent choice, and so is the whole feature
  disabled. A backstop below the first threshold is refused rather than
  quietly reordered.

  Two things it deliberately will not do. It does not fire while a session is
  *retrying* — a harness retrying an overloaded API is painting a countdown, so
  it is not silent, and it writes nothing to its transcript until the retries are
  exhausted (both measured against a real harness). It is caught at that point.
  And past the backstop threshold it stops trying to tell a halt from a genuinely
  long-running background command: an hour-old silence is flagged either way,
  which is the trade that keeps a halt this build cannot recognise — a harness
  with no readable transcript, or one that stops marking refusals — from going
  unnoticed forever.

  **On upgrade:** rebuild or re-pull the UI bundle (`lmer platform setup-ui`, or
  pull the platform image) alongside the daemon restart, or the fleet view
  renders the new chip as the raw word `stalled`. A restart is what makes the two
  config fields take effect; there is no migration, and an existing
  `config.json` without them takes the defaults.

- **Digest nudges** — the daemon spools digests for uber lmer, and nothing reads
  that spool until uber lmer takes it. When digests sit there while the session
  is quiet, the daemon types one reminder into it. Defaults: one digest, three
  minutes. Full behaviour in the section below.

## Digest nudges

The digest spool is pull-only: the daemon detects questions, crashes, finished
work and signalled milestones, writes a digest, and waits. What reads it is a
watch uber lmer arms on itself — and a watch stops. A skipped re-arm, a stale
edge detector, a harness with no background-monitor tool at all: each ends with
digests sitting unread. Measured on 2026-08-19: ten digests over seventeen
minutes, live questions among them, and the operator noticed before the
assistant did.

So the daemon nudges. On each detection tick (30s) it types **one sentence**
into uber lmer's own session, over the same input path a wind-down uses:

```
[lmer platform] 4 digests have been waiting in your spool for 6 minutes and this
session has been quiet. This is an automatic reminder from the daemon, not the
operator, and it is not a new task: take the spool with POST
/api/assistant/pending, ...
```

**No digest is ever pushed.** The reminder says something is waiting;
`POST /api/assistant/pending` remains the only way out of the spool, which stays
the one bounded, scrubbed source.

### When a nudge fires

All five, or nothing is typed:

| condition | why it is there |
|---|---|
| the interval is non-zero | `0` is the off-switch, and the only one |
| an assistant is running | there is otherwise no session to type into |
| at least *N* digests are waiting | a threshold, not a trigger: one nudge covers however many arrived |
| the accumulation has waited past *X* | dated from the moment the spool went from empty to holding something |
| the session has been quiet for *X*, and has drawn at least one byte | a working assistant does not need telling, and a session whose harness has not rendered yet has no input to read |

The accumulation's age is deliberately **not** the age of the oldest digest still
in the spool. The spool holds at most 50, so on a loud fleet the retained ages
drift younger as older digests are evicted — reading them would mean the busiest
fleet is the one never nudged. A state written before this stamp existed falls
back to the oldest retained digest, and an inherited spool keeps its age across
the upgrade rather than being re-dated by the next digest to arrive.

"Quiet" is the in-container supervisor's own `idle_seconds`, which measures time
since the session last produced **output**. A long silent tool call therefore
reads as quiet, and a reminder can land mid-turn; the agent picks it up at its
next boundary. Where the daemon cannot obtain the reading at all — an older
image, a control plane that did not answer — it nudges anyway, because treating
"unknown" as "busy" would switch the safety net off on exactly the hosts least
able to notice. It does not then claim the session was quiet.

### One reminder per interval, and how it self-heals

The nudge stamps the spool, and the stamp restarts the interval clock — so a
spool still unread *X* later is nudged again, while the wait the sentence reports
stays the accumulation's own. Taking the spool clears the stamp and ends the
accumulation; whatever arrives next is a new one, owed its own reminder. It is
never one reminder per digest.

If clock correction leaves that stamp in the future, the daemon durably clamps
it to the current time once. The ordinary interval can then elapse across ticks
and daemon restarts; if the correction cannot be written, that future value is
ignored for the decision so it cannot silence the accumulation indefinitely.

The repeat is the remedy for the one thing the platform cannot know: the input
path proves the bytes reached the control plane and cannot prove the harness
registered the Enter that submits them. A reminder that vanished leaves an idle
session and a full spool, and the next window covers it. A reminder that landed
makes the session busy, which stops the next one with no bookkeeping at all —
the same property that means there is no separate "is it draining right now"
check.

Two error paths matter, and they are opposite:

- A send that was **refused**, or a control plane that could not be reached,
  typed nothing. Nothing is marked and the next tick retries.
- A send that raised **after** delivering — a control plane acknowledging
  different bytes than were sent — put the bytes in the session. It is bounded
  like a clean send, because repeating it every tick is the failure the bound
  exists to prevent. A transport failure cannot be told from either and is
  treated as retryable: a duplicate reminder beats a lost window.

The bound does not depend on the state file being writable. The daemon remembers
the nudge in memory for the accumulation it was about, which matters because the
outage that stops it recording a nudge also stops uber lmer draining the spool —
without that memory, an unwritable `assistant.json` turned the interval into the
30-second tick.

### Configuration

| variable | default | what it sets |
|---|---|---|
| `LMER_PLATFORM_NUDGE_AFTER_SECONDS` | `180` | how long the spool waits, and how long the session must have been quiet |
| `LMER_PLATFORM_NUDGE_PENDING_THRESHOLD` | `1` | how many digests it takes |

Both also live in `config.json` and on `GET`/`POST /api/assistant/config` under
`nudge`, and both are re-read on every tick — a change applies without a daemon
restart or an assistant rotation. Precedence is the platform's usual chain: the
export beats the file beats the default, and each value reports which layer
decided it.

`LMER_PLATFORM_NUDGE_AFTER_SECONDS=0` disables the nudge entirely and is the only
off-switch. The threshold has a floor of `1` — a nudge about an empty spool is
not a feature — and a ceiling of `50`, the spool's own capacity, since a higher
threshold could never be met and would be a second off-switch nobody documented.
An unusable value costs its own layer with a warning and falls through to the
next, rather than stopping the daemon over a reminder interval; an explicit write
through the API is refused with a 400 naming the field.

**On upgrade:** restart the platform daemon to run the new tick stage. There is
no migration — the three state fields are additive and tolerate absence — and no
image rebuild or container passthrough, since the daemon reads both settings
itself. A host that does not want the feature sets the interval to `0`.

## Where things live

| what | where |
|---|---|
| platform state, logs, UI | `~/.lmer/platform/` |
| per-instance config (incl. service slots) | `~/.lmer/platform/config.json` |
| shared secret | `lmer platform secret` |
| per-session host log | `~/.lmer/platform/logs/<session-id>.log` |
| events ledger | `~/.lmer/platform/events.jsonl` |
| REST surface | `GET /api` (plain-text route index) |
