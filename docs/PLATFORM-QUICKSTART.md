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
- **Run references in chat** — the assistant writes a run's name as a link whose
  href is `lmer://run/<host>/<project>/<slug>`, and tapping it opens that run's
  view. Details worth knowing:
  - It is an **in-app dispatch, not navigation**: no history entry, no URL to
    paste or bookmark, nothing that survives a reload. Run views are deliberately
    not browser-addressable (issue #241).
  - A reference **can only select a view**. The grammar carries three key
    segments and nothing else — no verb, no payload, no URL to navigate to — and
    only that exact shape is rendered as a link at all: `lmer://run/…?spawn=1`
    and any other `lmer:` URL come out as plain text.
  - A reference to a run this platform does not track opens nothing and says so,
    naming the key. Adopt the run and the same reference becomes a switch.
  - A left or middle click is handled in-app; a context-menu "open link in new
    tab" is not an event the page can cancel, so it hands the href to the OS,
    where nothing is registered for it.
  - **On the way to Slack** the reference is reduced to its visible label before
    posting, because Slack's own link syntax is different (`<url|label>`) and
    CommonMark links render there as raw characters. The reduction is a text
    substitution, not a markdown parse: it does not know what a code span is, so
    a reference written *as an example* is reduced too. An escaped `\[` is left
    alone, as is a label containing brackets.
  - Web-only: it reaches you after a bundle build and a UI image roll.
- **A narrower app** — the toggle in the app bar holds the whole app — the bar, the
  run list, the content and uber lmer's drawer — in a 1620px band in the middle of
  the screen instead of spreading it across a wide window. It is the effect of
  narrowing the browser, for a window that has other tabs in it. Offered on a desktop
  only (below the band's own width it does nothing) and remembered between loads.
  Uber lmer stays where it always is, in the right-hand drawer — and with the band
  on it comes up open, because the band's width is the smallest one that still
  fits the run view beside that drawer.
- **redraw** (terminal view, beside the x1/x1.5/x2 height presets) — for a terminal
  that has gone garbled. It always rebuilds *this* view: the log tail is re-read into
  a fresh emulator, which is what repairs output this browser missed across a
  reconnect. If **fit to my screen** is on it also asks the session itself to
  repaint, by holding it one row taller for a moment before the rebuild hands its
  real size back — that half is a write to a PTY every watcher shares, which is why
  the fit switch (already the one gate on writing sizes) decides whether it happens.
  Whether a given TUI repaints on being resized is the TUI's own behaviour; if it
  does not, tap again or use its own redraw key.
- **uber lmer settings** — how its session is *run* (model, harness, preset,
  agents fan-out), per platform instance: the settings entry in the drawer's
  overflow menu, or `GET`/`POST /api/assistant/config`. Each value shows which
  layer decided it (an `LMER_PLATFORM_ASSISTANT_*` export beats what the
  dialog persists to `config.json`, and the dialog says so). Changes apply to
  the **next** incarnation — the running one keeps its context window until
  you restart it, and the dialog offers the restart. Standing orders are the
  chat's to edit and are deliberately not in this dialog.
- **A message that never settles** — a message you sent is shown as a bubble until
  the harness writes that turn into its transcript, which is how a queued message
  stays visible. When one gets stuck there — a transcript gap, which is a bug rather
  than a wait — the × on the bubble takes it off the screen. Display only, for as
  long as the page is loaded: the message was sent, nothing is deleted, and if the
  turn does land later it appears as the ordinary transcript turn it always was.
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

## uber lmer's memory

The assistant's harness keeps an agent-memory directory — small fact files and an
index it re-reads at the start of every incarnation. That directory lives inside
the container, so it used to reset on every rotation: a lesson learned in the
morning was gone by the afternoon. The platform now mounts one host directory into
every incarnation instead.

- **Where:** `~/.lmer/platform/assistant-memory/`, one per host, owner-only. It
  survives rotation, a daemon restart, and the container's `--rm`.
- **Platform-local, deliberately.** This is the same kind of state as the handoff
  note and the standing orders — it is *this* host's, and it syncs nowhere. Worker
  sessions have their own per-project memory route through the work repo
  (`LMER_PERSIST_AGENT_MEMORY`); for assistant sessions that route is switched off
  inside the container, so operational notes about your fleet cannot end up in a
  shared repository.
- **Curated by the assistant, not by the platform.** Nothing here is ever trimmed
  or deleted. Above 256 KiB or 50 files a spawn logs a warning and records an
  `assistant_memory_large` event, and `GET /api/assistant` reports the store's
  file count and size in its `memory` block.
- **What that count does and does not tell you.** It reports accumulation: a store
  that grows is proof the container-side link is live, and one that never grows
  across many incarnations is worth investigating. It is *not* a liveness check —
  a store that already holds files keeps reporting them even if the harness stops
  reading the path (that path is the harness's own encoding of its working
  directory, which nothing promises). The authoritative answer is in the session's
  own log, where the container entrypoint prints either `🔗 Linked <path> → …` or a
  `⚠️` line naming why it could not.
- **How it gets in, and why not more simply.** The host directory is bound at a
  staging path (`~/.lmer-mounts/assistant/memory`) and the container entrypoint
  symlinks the harness's own memory path to it. It is not bound at that path
  directly, because a container runtime creates a mount destination's missing
  parents **root-owned before any container process runs** — and the harness's
  memory path sits inside the per-session transcript mount, so a direct bind
  would leave claude unable to write its own session JSONL beside it (the
  `#293`/`#290` failure). The entrypoint makes the link as the container user,
  which leaves the whole parent chain owned by the account that writes it. A
  session image whose entrypoint predates that linker makes no link and keeps its
  memory per-session, which is the pre-existing behaviour.
- **claude reads it natively; other harnesses have to be told.** claude is the only
  built-in that declares a memory path, and the link is made from that declaration
  on every assistant spawn regardless of which harness the session runs — so an
  assistant running codex or pi does get the store mounted and linked, but neither
  has a memory feature that reads it on its own. What tells those sessions the
  store exists, and where, is the `orchestrate` taskdef; nothing else does.

## Chat file upload

Both chats — uber lmer's and any run's — take a file: drag it onto the composer,
paste it, or tap **attach** (which on a phone is the camera roll). It goes with
the message, and the message names the path the agent will find it at.

- **How it travels.** `POST /api/sessions/{id}/input` writes raw bytes to a PTY
  and has nowhere for an attachment, so the daemon stores the file host-side and
  bind-mounts the store into the container at `/home/developer/.lmer-uploads` —
  the same mechanism as the ask channel. The message that follows carries a
  `[lmer upload] …` line naming the container path, which is the agent's
  notification; nothing else announces one.
- **Where a worker's files land:** `~/.lmer/platform/logs/<session-id>.uploads/`,
  beside that session's transcript and PTY log, owner-only (0700, files 0600).
  **Nothing removes it** — not when the session exits, not when the run is
  forgotten. Retention is not implemented for any of the per-session directories
  (`.uploads`, `.ask`, `.transcript`), which is deliberate for the transcript and
  simply not done here; the earlier wording said this "goes when the session's
  other per-session state does", which read as cleanup that does not happen
  (!272 review). Deleting that directory by hand is safe once the session has
  exited. A run that wants to *keep* a file copies it into `uploads/` in its own
  run directory in the work repo and commits it — **the run's decision, not the
  platform's**: the daemon has no host-side checkout to write into, since
  `/work` is cloned inside the container.
- **Where uber lmer's land:** `~/.lmer/platform/assistant-uploads/`, one per host,
  like its memory store. It outlives every rotation and it is the assistant's to
  organise, rename and delete. Nothing prunes it.
- **Nothing is pushed anywhere.** An upload can hold whatever was on your screen,
  credentials included. It stays on this host unless a run deliberately commits
  it — and the work repo is shared with every developer using lmer, which the
  prompt fragment tells the agent in as many words.
- **What is accepted** is `png`, `jpeg` and `txt` by default, up to 8 MiB per
  file, both configurable (`LMER_PLATFORM_UPLOAD_TYPES`,
  `LMER_PLATFORM_UPLOAD_MAX_BYTES`, or the matching `config.json` keys; see
  [docs/LMER-CLI.md](./LMER-CLI.md)). The type is decided by the file's **bytes**,
  not its name: a PNG called `notes.txt` is stored as a PNG, and an allowlist
  cannot be got past by renaming a file.
- **A session started before this shipped cannot be handed a file.** Its
  container has no such mount and nothing can add one, so the upload is refused
  with a sentence naming the restart rather than stored where nothing reads it.
  For uber lmer, **restart** in the chat header is that restart.
- **Whether the agent can *see* an image depends on its harness.** claude opens
  one; codex and pi are not equivalent here. The honest fallback is the mechanism
  itself — the file is on disk and the path is in the message, so an agent that
  cannot render a picture can still read a text file and act on a listing.
- **In the history.** An uploaded image is shown back in the chat pane, read from
  the store of **the session that received it** (`GET
  /api/sessions/{id}/uploads/{name}`) — a run's conversation spans every session
  it has had, so the pane resolves each thumbnail against the session whose
  transcript that turn came from rather than against whichever session is current.
  So opening the run later, on another device or after a resume, still shows the
  screenshot rather than a path. A file the store no longer holds — one a run
  copied out and deleted — leaves the path line and drops the thumbnail.

## Where things live

| what | where |
|---|---|
| platform state, logs, UI | `~/.lmer/platform/` |
| uber lmer's memory store | `~/.lmer/platform/assistant-memory/` |
| uber lmer's chat uploads | `~/.lmer/platform/assistant-uploads/` |
| a run's chat uploads | `~/.lmer/platform/logs/<session-id>.uploads/` |
| per-instance config (incl. service slots) | `~/.lmer/platform/config.json` |
| shared secret | `lmer platform secret` |
| per-session host log | `~/.lmer/platform/logs/<session-id>.log` |
| events ledger | `~/.lmer/platform/events.jsonl` |
| REST surface | `GET /api` (plain-text route index) |
