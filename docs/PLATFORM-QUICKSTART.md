# Platform quickstart

For named workspaces, project-bound routing, capacity and General migration, see
[Projects](PROJECTS.md).

Get the lmer platform — the web control plane for a fleet of lmer sessions,
with its supervising assistant ("Ratchet") — running on a host in a few
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

Standing orders, the handover note, and the visible conversation survive a
restart. The replacement assistant starts with a fresh model context and can
consult relevant prior exchanges when context is missing.

## Using it

- **Fleet view** — every run this platform tracks, attention first. The row
  with the orange border and the face is Ratchet itself; its expression is the
  fleet's mood (hover or tap it for the reason). `chat` on that
  row (or the face in the app bar) opens its drawer.
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
- **Schema-2 runs are keyed by uid** — on a schema-2 work repo every launch is
  a record of its own, so the platform mints the launch's execution uid before
  the container starts (it passes `lmer --launch-uid`) and keys the run
  `host/project/<uid>`; the slug is only the group label every launch of it
  shares. A schema-1 run keeps its `host/project/<slug>` key. The `uid` comes
  back in the spawn reply's `run` and in the resume and answer replies' `run`
  and `started`, fleet rows carry it, and every run-keyed route (`adopt`,
  `forget`, `meta`, `relations`, `answer`, `resume`, `files`) takes `uid`
  beside `{host, project, slug}` (body or `?uid=`). `lmer-ctl`'s
  `host/project/<key>` argument reads a 32-hex last segment as a uid.
  Resuming or answering such a run starts a fresh execution with the old
  record as read-only context: a new row of its own, while the source keeps
  the state it stopped in.
- **A second session on a project is a warning, not a 409** — on schema 2 a
  second chat (or a second continuation of one record) is a separate record,
  so nothing refuses it. The spawn, resume and answer replies' `warning`
  carries a concurrency note instead, `N live session(s) already on
  host/project [for <target>]: <session ids>`, narrowed to the same work item
  when the target is one; the UI shows it. Detected and said, never enforced.
  A schema-1 run is still one run, one session.
- **Target-less schema-2 records resume** — a schema-2 record with an empty
  target, a document (reference) URL, or an `other` URL (one that is not a
  repository, a work item or a known thread) can be resumed and answered: the
  launch names its repository with `--repo`, from the request's `repo_url` or
  else the clone URL recorded for the run. With neither, the call is refused
  `repo_url_required` and asks for one; the platform will not invent a URL. A
  schema-1 record with no target is still refused. A record whose target is a
  bare repository URL resumes on `--repo` too, with that URL dropped as the
  positional; it lands in the same run, whose slug is the taskdef either way.
- **Wind down vs exit** — wind down asks the agent to wrap up and end itself;
  exit ends it now. Both under the run's detail view.
- **Finished but still live** — a run whose `state.yaml` records a terminal
  status (`complete`/`archived`) reads `complete` even while its session is still
  up. The run's own record is a deliberate act by `work state set`, and a session
  sitting at its prompt afterwards can still add artifacts; reporting it as
  `running` kept a finished run drawing check-ins for hours (#308). The row keeps
  reporting the live session, so "complete, session still up" is distinguishable
  from a run that is still working.
- **Wound down, not crashed** — a dead session's leftover registry entry is
  normally the only evidence that something died, so the fleet reads it as a
  crash. The exception is an ending the operator asked for: a wind-down leaves a
  record on the entry and then ends exactly as asked, with no reaper to clear it,
  and such a row reads `dormant` with no attention badge (#324). Two facts decide
  it, not one — the recorded request, *and* the absence of a bad exit a watcher
  saw. A session wound down at 09:00 and OOM-killed at 09:04 is still a crash,
  and the detail view names the status that watcher saw. Where nobody watched (a
  re-attached session, or one `lmer platform spawn` started) there is no such
  record either way, which is the case the wind-down mark is allowed to speak
  for. A corrupt or truncated `exited` record counts as a bad exit rather than as
  none: mislabelling a wound-down run costs a badge an operator can dismiss,
  while the other direction retires a death nobody is ever told about. The
  daemon's sweep clears these entries, since the UI's "clear crashed sessions"
  card only renders on a crashed row.
- **Ratchet** — the supervising assistant. It hears attention changes and
  session milestones (`lmer-signal`), can spawn/answer/wind-down runs on
  request, and keeps operator-taught standing orders across restarts. Teach it
  rules in plain chat ("spawn reviewers with preset X"); restart it from the
  drawer when needed. Its [conversation history](./ASSISTANT-HISTORY.md)
  remains available across rotations, reloads, and daemon restarts.
  The drawer's one compact toolbar, on a neutral surface a shade darker than the
  pane, holds the Slack and matrix icons, restart, stop, project settings,
  standing orders (opens project settings with **Standing instructions**
  expanded), the count of loaded messages, earlier messages, and close. Hover, tap
  or focus the robot icon for the incarnation's generation and how long ago it
  started; a tap elsewhere puts it away. The
  conversation fills the window's height at every size, from a phone to a 4K
  screen. **Standing instructions** in project settings shows when the orders
  last changed, says when there are none, and reminds you that they are usually
  set by telling Ratchet in chat, which confirms the wording before storing them.
- **Run references in chat** — the assistant writes a run's name as a link whose
  href is `lmer://run/<host>/<project>/<slug>` (`<uid>` in place of the slug
  for a schema-2 run), and tapping it opens that run's view. Details worth
  knowing:
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
  - **One grammar, two languages.** The key and the reference are spelled by
    `src/lmer_cli/runref.py` and `web/src/runref.js`, with one corpus run
    through both by `tests/test_runref_grammar.py`. Splitting a key is
    deliberately permissive (an argument the daemon should refuse by name),
    matching a reference deliberately anchored (it decides whether a link is
    live).
  - Web-only: it reaches you after a bundle build and a UI image roll.
- **A narrower app** — the toggle in the app bar holds the whole app — the bar, the
  run list, the content and Ratchet's drawer — in a 1620px band in the middle of
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
- **Ratchet settings** — how its session is *run* (model, harness, preset,
  agents fan-out, Slack/matrix threads), per platform instance: **Execution and chat
  defaults** in Platform settings, or `GET`/`POST /api/assistant/config`. The dialog groups its
  fields as **Session launch**, **Conversation** and **Reminders**, each with a line
  saying when its changes apply. Each value shows which
  layer decided it (an `LMER_PLATFORM_ASSISTANT_*` export beats what the
  dialog persists to `config.json`, and the dialog says so). Changes apply to
  the **next** incarnation — the running one keeps its context window until
  you restart it, and the dialog offers the restart. Standing orders are the
  chat's to edit and are deliberately not in this dialog. Thread permalinks go
  one per line; save and rotate to connect. The saved list survives rotations.
  The dialog also holds the chat pane's **Show Ratchet's working transcript**
  switch: off (the default) the pane draws the conversation only — your
  messages, and what Ratchet posted, asked and answered; on, its tool calls,
  watch events, hook re-prompts and unposted working notes appear muted beside
  them. Platform state (`chat_show_transcript` in `config.json`): the browser
  that saved it switches at once, and every other open drawer follows on its
  next status poll. `docs/COMMS.md` §4 has the table of what each view draws.
  Workers accept the same links through `POST /api/sessions` (`targets`) or
  repeatable `lmer-ctl spawn --target-thread` flags. See
  [thread setup and overrides](COMMS.md#9-chat-thread-connectors-slack-and-matrix).
- **Open-question badge** — Ratchet's message box shows a bell, in its right-hand
  corner, with the number
  of its own questions to you that are neither answered nor withdrawn
  (`open_questions` on `GET /api/assistant`, counted from its ask channel exactly as
  the run view's ask dock counts them). It updates on the pane's status poll,
  whichever channel answered, and disappears at zero. Tapping it opens Ratchet's
  own run view and focuses the open questions there, answerable with an option or
  free text. The corner's space is kept while the bell is absent, so the box does
  not shift when it appears or clears, and tapping the bell does not focus the box.
- **Status strip** — Ratchet's pane draws its strip with orange meters and a
  **hide** / **show status** control remembered in this browser; run views and the
  run list are unaffected. `LMER_PLATFORM_STATUS_STRIP=0` on the daemon turns the
  strip off fleet-wide, and with it the context-rotation reminder, which reads the
  same figure ([LMER-CLI.md](LMER-CLI.md#lmer-specific-environment-variables)).
- **Thread icons** — every run row, the run view header and the Ratchet chat
  pane's toolbar carry two small icons, Slack and matrix. A dim one opens a
  one-field prompt for a thread permalink; a coloured one names the threads on
  hover, carries a count when there is more than one, and offers a link and a
  disconnect for each. Connecting and disconnecting happen **while the session
  runs** — nothing restarts — and doing it on Ratchet also moves its standing
  `threads` setting, so the connection survives a rotation. A run with no live
  session shows what it was connected to and offers no new connection. The same
  three calls are `lmer-ctl threads list|connect|disconnect`.
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
  row each under the runs list. Declare, edit and remove them from that section;
  the spawn dialog's picker offers the free ones, and
  `lmer-ctl spawn … --slot <name>` takes one from a shell. Changes apply on the
  next poll — nothing is restarted. Occupancy is derived from live sessions, so
  a slot frees when its session's process ends and a crash cannot strand one.
  Full reference:
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

- **Stuck teardown** — after the supervisor acknowledges an end request, a
  launcher that stays alive while its control plane continuously fails to
  respond is shown as **stuck teardown**. The assistant gets one digest naming
  the session, including its id in the digest's structured data. The daemon
  does not terminate it automatically; the operator or orchestrator decides
  whether to use exit. Wind down alone does not start this timer.

  Before typing quit, the supervisor allows up to six seconds for the daemon
  to confirm the acknowledgment over the authenticated bus. The API stays up
  through this bounded handoff even if the harness exits naturally first. This
  covers the daemon's five-second subscription reconciliation cycle. Without
  authenticated delivery to the daemon, shutdown proceeds and the detector
  remains unarmed.
  Container-written files cannot arm it. The subscriber identifies the platform
  session; the work-session id in the record stays intact.

  `LMER_PLATFORM_STUCK_TEARDOWN_SECONDS` overrides `stuck_teardown_seconds` in
  `~/.lmer/platform/config.json`; the default is **60** seconds. Accepts positive
  integers; zero, negative values and malformed values are refused. A blank
  environment value falls through to the config file. Restart the daemon after
  changing it, as with the halt-detection thresholds above.

  Timing starts at the first failed control-plane sample after acknowledgment.
  The detector samples every 30 seconds, so the warning appears on the first
  tick at or beyond the configured grace. An HTTP response, including an HTTP
  error, clears the loss interval. Missing local probe credentials cannot prove
  connection failure and also clear the interval.

  This is the safety net for shutdown. The supervisor first asks the harness to
  quit, allows 10 seconds, sends SIGTERM, allows another 10 seconds, then sends
  SIGKILL. A blocked PTY writer cannot delay this ladder. It reaps the harness
  before stopping the control plane, including when the PTY closes early or a
  descendant retains it. Transient tiny host dimensions preserve the previous
  terminal size; a headless session starts at 80 columns by 24 rows.

  Upgrade the session image, daemon, and UI bundle together. Existing sessions
  from older images do not publish the acknowledgment needed by this detector.

- **Digest nudges** — the daemon spools digests for Ratchet, and nothing reads
  that spool until Ratchet takes it. When digests sit there while the session
  is quiet, the daemon types one reminder into it. Defaults: one digest, three
  minutes. Full behaviour in the section below.

- **Context-fullness reminders** — when Ratchet's own status line reports that
  its context window has reached a threshold the operator set, the daemon
  recommends a rotation at its next breakpoint. Off by default; it never rotates
  anything itself. Full behaviour in the section below.

### Schema-form views

Three views appear only when the work repo is in schema form (#374). The fleet
payload's `state.mirror.form` says which form the mirror is in: `schema`,
`legacy`, or `null` when there is no mirror. It is a presence test for the
manifest and runs no validation. A legacy tree shows no change: nothing extra
mounts and no extra request is sent.

- **Sessions per target** — in a run's detail view, for a row that carries a
  `uid`, the "launches of this target" panel lists every session record in the
  run's `_runs/<slug>/` group, oldest start first. Each row shows the short uid
  (with a copy button), the `--<name>` suffix, chips for "this run", `live`, and
  the row's project when it differs from the run's, then the status, the record
  state (`active`, `stale`, `seeded`, …) and when it started. **resume** opens a
  form under that row (slot, direction, and project confirmation when needed) and
  posts the row's own `uid`, so it continues that launch and no other. A row this
  orchestrator does not track is read-only and says so; adopt it to continue it
  from here. The data comes from
  `GET /api/runs/sessions?host=&project=&slug=|uid=`, which reads the daemon's
  mirror without pulling, so a launch that pushed seconds ago can be missing for
  one interval.
- **Work repo** — once, at the bottom of the all-projects fleet view, because the
  work repo belongs to no single project. It holds the two panels below, both
  read from `GET /api/workrepo/schema`, and a **re-validate** button. It reloads
  when the mirror's HEAD changes.
  - **Unnamed repos awaiting adoption** — built from the validator's
    `tree.host-bucket` findings. Each bucket shows its path and a copyable
    `work adopt <host>/<project> <short-name>` line. Replace `<short-name>` with
    the name you want before you run it: the platform never derives a name, and
    the panel has no adopt action.
  - **Validator findings** — renders `lmer-schema validate --json` over the
    mirror, exactly as the CLI prints it for that tree. A form/tier header comes
    first, then a count summary, then each finding's severity, code, path and
    message. Only errors affect conformance. An unusable manifest is shown as
    the validator's verdict, not as an API failure.

The validation is cached per mirror HEAD for up to ten minutes, because
staleness findings move with the clock. **re-validate** (`?refresh=1`) runs it
again now. Neither route ever pulls, and a legacy tree is never validated.

## Assistant mentions and DM replies

The daemon owns Slack and Matrix ingress for the assistant. Enable
`LMER_ASSISTANT_MENTION_ROUTING=true` in its environment or `~/.lmer/.env`, set
the transport credentials and sender allow-lists below, and restart the daemon
once. Stop any standalone `lmer-slack-listener` or `lmer-matrix-listener`: they
are legacy entry points and are not needed here. Running a second Socket Mode
consumer can split Slack delivery between processes.

Ingress adds requests to the assistant's private inbox. It never starts an
`lmer chat` session. The assistant answers or starts a worker under the
operator's run-start policy. A mention does not automatically start an offline
assistant. See [admission and reply rules](./ASSISTANT-MENTIONS.md), including
connected-run ownership and the exact assistant-parent requirement for DMs.

### Slack app requirements

Slack uses [Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/),
the same transport as the legacy listener. This avoids enumerating and polling
every channel for mentions and needs no public HTTP endpoint.

- Set `SLACK_BOT_TOKEN` to the installed bot token and `SLACK_APP_TOKEN` to the
  app-level token with `connections:write`, for the same app/workspace.
- Enable Socket Mode and subscribe to bot events `app_mention` and `message.im`.
- Grant `app_mentions:read` and `im:history`, plus the existing history scopes
  for context reads (`channels:history` for public channels and `groups:history`
  for private channels). Keep `chat:write` for replies and `im:write` to start
  assistant DMs. Add `files:write` so runs' `--attach` files upload into their
  threads. Reinstall the app after changing its granted scopes.
- Invite the bot to the channels where it should receive mentions. Membership
  still controls access to history; `not_in_channel` in the daemon log identifies
  a refused context read.
- Set `LMER_SLACK_DM_ALLOWED_USERS` to permitted Slack IDs or confirmed handles.
  Unset or empty admits nobody, including channel mentions. Handle entries need
  `users:read`; exact IDs avoid that lookup.

### Matrix account requirements

Set `LMER_MATRIX_HOMESERVER`, `LMER_MATRIX_USER_ID` and
`LMER_MATRIX_ACCESS_TOKEN` for the existing bot account. Set
`LMER_MATRIX_ALLOWED_USERS` to permitted exact MXIDs; unset or empty admits
nobody. `LMER_MATRIX_ROOMS` optionally selects rooms to join and sync; otherwise
the daemon syncs all rooms the bot has joined. Room aliases are resolved by the
join response before they enter the sync filter. The first sync establishes a
cursor and ignores historical events; reconnects retain that cursor until the
daemon restarts. Encrypted rooms retain the existing transport limitations.

A failed selected-room join logs the room and error code and retries after
30 seconds. Successfully selected rooms continue to sync. If none of the
selected rooms can be joined, ingress reports `retrying` and waits for a join
to succeed; it never falls back to syncing all joined rooms.
The first sync after a selected room joins excludes that room's initial history.
Other selected rooms keep receiving fresh events, and reconnects retain this
per-room initialization state along with the cursor.

### Ingress health and failures

`GET /api/health` includes `ingress.slack` and `ingress.matrix`. Each contains
`up`, `state`, `last_event_at`, and `last_error`. These are local snapshots;
reading health never calls Slack or Matrix. The fleet UI's **Assistant message
ingress** panel shows the same state, last event time and last error.

`up` means the Socket Mode connection is live or Matrix sync succeeded.
`last_event_at` records receipt of an event, including refused events, rather
than the last empty poll. `last_error` retains the most recent error after
recovery, and is empty until a failure occurs. Times/errors reset at daemon
startup; inbox replay receipts remain durable.

Disabled routing reports `disabled`. No credentials reports `unconfigured`;
partial credentials reports `misconfigured` and names the missing variables.
One transport failing does not stop the other or the API. Initial failures retry
with bounded backoff; Socket Mode also reconnects through its SDK. Check the
app's subscriptions and channel membership when ingress is up but no event
arrives: connection health cannot verify an event the app never sends.

Every received event that is not admitted produces an `assistant_ingress_dropped`
daemon log record with its transport, identifiers and reason, such as `denied`,
`connected`, `unmatched_dm_parent`, `duplicate`, or `not_in_channel`. Message
bodies are not logged. Disabled submissions through the external API log
`disabled`. Transport failures also update health; limited Matrix sync timelines
log `limited_sync` because events may have been omitted by the homeserver.
There is no durable ingress outbox: after a failed admission, mention again
once the cause is fixed. External host listeners may still submit authenticated
`POST /api/assistant/requests` events through the same admission rules.

## Digest nudges

The digest spool is pull-only: the daemon detects questions, crashes, finished
work and signalled milestones, writes a digest, and waits. What reads it is a
watch Ratchet arms on itself — and a watch stops. A skipped re-arm, a stale
edge detector, a harness with no background-monitor tool at all: each ends with
digests sitting unread. Measured on 2026-08-19: ten digests over seventeen
minutes, live questions among them, and the operator noticed before the
assistant did.

So the daemon nudges. On each detection tick (30s) it types **one sentence**
into Ratchet's own session, over the same input path a wind-down uses:

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
outage that stops it recording a nudge also stops Ratchet draining the spool —
without that memory, an unwritable `assistant.json` turned the interval into the
30-second tick.

### When the assistant is not taking its spool

A reminder can't help an assistant that can't act, for example one whose
harness login expired overnight. Repeating it every interval used to mean
hundreds of inputs a day into a session that could do nothing with them, while
the operator heard nothing. So the nudge backs off (#584):

- **After 3 unanswered nudges, typing stops.** A nudge counts as unanswered
  once its own interval has passed without a take. The back-off decision still
  needs every condition above, so a busy assistant is neither nudged nor
  reported.
- **Then the operator gets one message.** The daemon posts it itself into the
  assistant's standing threads: the project's `threads` setting, or
  `assistant_threads` outside projects mode. It gives the waiting count and the
  wait, and says that reminders have stopped. The post carries no
  session attribution, so a connector watching the thread treats it as its own
  echo and doesn't type it into the assistant.
- **It names a known marker, never a diagnosis and never terminal text.** The
  daemon searches the tail of the assistant's terminal for a fixed list of
  markers (`Login expired`, `Please run /login`, `authentication_failed`,
  `rate_limit`) and names only the one it saw last, for example "its terminal
  shows `Login expired`". Nothing else the terminal drew leaves the host. When no
  marker is there, the message says the platform saw no known reason.
- **Once per episode.** An episode ends when a new assistant incarnation starts,
  when the spool is taken, or when moving a run empties the spool. Any of these
  restores the 3 nudges and allows one more message. The count and the signal
  stamp are stored beside `nudged_at`: in `assistant.json`, or in the project
  database in projects mode. The daemon also records them in memory before each
  write, so a store that refuses writes can't turn the message into a post every
  tick. A count or stamp written after a take has landed is refused, so it can't
  carry over into the next backlog.
- **Codex counts 3 too.** The one native wake a codex assistant gets per
  accumulation comes before the reminders and doesn't count, so it sees 4 inputs
  before typing stops.

If no standing thread exists, the `assistant_stalled` event in `events.jsonl` and
the `platform_assistant_stalled` warning are the whole signal. If every thread
refuses the post, the episode stays unsignalled and the daemon tries again one
interval later. Every attempt is recorded in an `assistant_stalled` event with
the threads it reached, the failures (scrubbed) and the marker.

### Projects mode: merged digests

A project's spool is never truncated, unlike the 50-note spool outside projects
mode. Instead, the two digest classes that repeat on a quiet fleet are merged
as they arrive:

- A new `stale_runs` check-in absorbs the project's earlier ones. The merged note
  names every run any of them named, uses the newest details for each, and counts
  the check-ins it absorbed in `data.check_ins`.
- A routine `session_comms` batch absorbs that session's earlier routine note.
  A batch is routine only when every record in it is a `note`. Connector-health
  notices, such as delivery retrying or recovered, are filtered out before the
  spool and never reach a batch. The counts add up and the note keeps the latest
  lines. The full record is still in the session's comms feed.

Questions, conversation requests and attention digests are never merged or
dropped. A comms batch holding anything other than notes stays a note of its
own: a question, a milestone, a close, a relayed command or answer, a record
with no kind, or a kind added later. Notes
spooled before this change carry no `routine` tag and are left as they are until
they are taken.

### Confirming a crash

A `crashed` digest claims two things: that a session died, and that its run
committed nothing. The second half is read off the work-repo mirror, and a
detection tick deliberately does *not* pull it — so a run that pushed its final
state and exited inside the throttle window looks exactly like one that never
pushed at all, and was announced as a death with its work declared lost (#324).

The claim is therefore re-read against a mirror forced to catch up, and only
when such a signal is fresh in that tick's diff — never on a tick that found a
crash standing, and never on one that found none. `work_repo_pull_interval`
keeps its meaning for all the traffic the confirmation is not for.

What the forced read costs, because it is what the next reader will budget
against: a second whole `build_state`. That is the fetch in `list_tracked()`, a
`resolve_run_dir` per tracked run, the titles, the ask channel and the check-in
fold — plus, since no `activity` mapping is handed in, one control-plane HTTP
read per *live* session at `ACTIVITY_TIMEOUT_SECONDS` each. On a busy fleet with
a few unreachable containers that roughly doubles the tick and can push it past
the detection interval. It is the right trade on an event this rare, and worth
knowing before a second such read is added.

A confirmation that fails delivers the crash anyway: the absence may be real,
and a mirror that will not answer is the worst moment to go quiet about a
session that died.

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

## Context-fullness reminders

Ratchet fills its context window over a working day, and it cannot see its own
fullness as a threshold — the figure exists, but reading it means asking the
platform about itself on a schedule, which is the polling the whole design keeps
out of the assistant's turns. So the daemon holds the number.

Set `rotation.context_threshold` (a percentage, `0` is off and is the default)
and on each detection tick the daemon compares it against what Ratchet's own
harness publishes on its status line. When the reading reaches the threshold it
types **one sentence** into that session, over the same input path the digest
nudge uses — including that path's harness-specific delivery: on a codex
assistant a reminder the native adapter refuses is not marked as sent, and the
next tick asks again under the same delivery id, which the adapter replays only
if it never submitted the first one:

```
[lmer platform] Your context window is at 71 percent and this host's rotation
threshold is 70 percent. This is an automatic reminder from the daemon, not the
operator, and it is not a new task: nothing has been rotated and nothing will be
without you. Finish what is in front of you, then rotate yourself at the next
clean breakpoint ...
```

**Nothing here rotates anything.** The daemon recommends; Ratchet decides, and
what a good breakpoint is lives in the `orchestrate` taskdef — no open question of
its own, no digest taken and not yet acted on, no half-finished brief, and never
mid-decision. A daemon that rotated on a number would end a context window in the
middle of the operator's sentence.

### When a reminder fires

All six, or nothing is typed:

| condition | why it is there |
|---|---|
| the threshold is non-zero | `0` is the off-switch, and the only one |
| an assistant is running | there is otherwise no session to type into |
| its harness published a `context.percent` | the reading is the harness's, and not every harness has one |
| the reading is not older than 10 minutes | a reading is a fact about a moment, and a publisher that has stopped must not keep recommending on an hour-old figure |
| the reading is **at or above** the threshold | 70 is the number the operator asked to hear about, so reaching it is reaching it |
| no reminder went to this session in the last 15 minutes | the repeat window |

The reading is `context.percent` from [SESSION-STATUS.md](./SESSION-STATUS.md):
the percentage of the window **used**, `0`–`100`. Its age is the reading's own
where the harness dates it and the document's otherwise — a status line that
re-publishes a figure it can no longer refresh would otherwise look permanently
fresh.

**The age bound is also why a reminder lands while Ratchet is working.** A
status line is published when the harness renders one, which does not happen
while a session sits idle at its prompt — so an assistant that has been idle for
longer than the bound has no usable reading and is told nothing until it is
active again, and then within a tick. That is the opposite of the digest nudge,
which exists to reach an idle session, and it is the right way round for this
one: what the reminder asks for is a decision at a breakpoint, and a session with
no turn in progress has no breakpoint to take it at. Where a harness publishes none
the daemon says so **once per incarnation** — one
`platform_assistant_rotation_unreadable` log line and one
`assistant_rotation_unreadable` record in `events.jsonl` — and then stays quiet,
rather than alerting on a figure it does not have. Codex and pi status-line parity
is tracked separately.

### Delivery, and why a reminder can be attempted more than once

The line travels the same input path as the digest nudge, which means it inherits
that path's harness-specific delivery. On a **codex** assistant the native
adapter is the transport, and three of its properties decide what the daemon may
record:

- A refusal is not an exception. The adapter answers 200 carrying
  `state: rejected` or `uncertain` and there is no fallback to terminal
  submission, so a reminder is only ever marked once the adapter reports the
  model actually took it.
- `uncertain` means the submission may have landed. It is never replayed and
  never recorded as a recommendation — an unknown outcome is not evidence that
  anyone was told.
- Receipts are keyed on a delivery id, and **reusing an id with different text is
  refused outright**. So one reminder attempt is one id and one frozen sentence:
  a retry after a refusal re-offers byte-identical prose under the same id, and a
  new crossing or a new repeat window gets an id of its own.

A consequence worth knowing when reading a reminder that arrives late: it reports
the percentage that *triggered* it, which may be a point or two below the reading
by the time it lands.

### One reminder per crossing, and the window

The reminder repeats at most every 15 minutes while the reading stays at or above
the threshold, for the digest nudge's reason: the input path proves the bytes
reached the control plane and cannot prove the harness registered the Enter that
submits them, so the remedy for a lost reminder is the repeat rather than a claim
that it landed.

The 15 minutes is **not** configurable. The group has one knob deliberately: what
an operator tunes is the percentage they want to hear about, and the interval is a
property of what the reminder is — a recommendation about a decision taken at a
breakpoint, not a wake-up about work waiting.

If the reading falls back below the threshold — a compaction, a fresh
incarnation — the window ends with it, so the *next* crossing is announced
immediately instead of a quarter of an hour later. The bookkeeping is the daemon's
own memory, keyed on the session it typed into, so a rotation never inherits a
window and a successor is never held quiet by a reminder its predecessor received.
A daemon restart while a reading sits above the threshold costs one extra
reminder, which is the whole price of not storing it.

### Reserved assistant capacity

`max_concurrent_sessions` limits **workers**. Ratchet has one additional,
reserved place: with a limit of eight, eight workers and one assistant may run
at the same time. A full worker pool does not prevent assistant startup or
replacement, and starting or rotating the assistant does not free a place for
a ninth worker. The worker occupancy remains 8/8 during that rotation.

This applies to `POST /api/assistant/start`, operator-initiated
`POST /api/assistant/rotate`, self-rotation through `lmer-ctl rotate`, and the
supervisor's startup and recovery paths. They share the same launcher, which
applies the worker limit only to worker launches. Worker launches through
spawn, answer, and resume remain subject to that limit, including workers
requested by the assistant. The reservation follows the server-selected session
kind; naming the `orchestrate` task does not grant it.

The existing assistant lifecycle lock and live registry check still prevent
duplicate assistants: a second start returns HTTP 409. Rotation stops the old
assistant before publishing its successor, without stopping any worker. The
successor inherits the handoff and pending updates; retained
[conversation history](./ASSISTANT-HISTORY.md) links the generations. Old
conversation text is not automatically inserted into the successor's context.

Capacity admission still runs inside the shared launch lock, alongside request
validation and publication safeguards. Reserved assistant capacity does not
guarantee that a process can start when host resources or other launch
requirements are unavailable. Configured **service slots** are a separate
ownership mechanism and are unaffected.

### Rotating Ratchet, and who may

Ratchet can rotate **itself**:

```sh
lmer-ctl rotate --handoff -     # or --handoff-file PATH
```

That writes the handover through `POST /api/assistant/handoff` and then calls
`POST /api/assistant/rotate`. The handover goes first on purpose: a rotation the
daemon declines still leaves the note stored, so the operator's own rotate — or
the supervisor's respawn after a crash — is as briefed as the planned rotation
would have been.

**The rotate call does not return.** The daemon ends that container to start the
successor, so from inside it a dropped connection is what success looks like; the
CLI reports it as a transport error and claims nothing, because a dropped
connection is equally consistent with a daemon that died. The receipt is the
successor: a fresh `generation` on `GET /api/assistant`, with `handoff` the note
its predecessor wrote and the digest spool preserved for it.

**Who may rotate is unchanged.** This adds a caller to an existing route, not a
route and not a caller class: Ratchet's minted credential (#244) already opens
what the operator's shared secret opens, and an unauthenticated rotate is still a
401. The operator-initiated rotate — the chat drawer's restart, or the route by
hand — is untouched.

### Configuration

| variable | default | what it sets |
|---|---|---|
| `LMER_PLATFORM_ROTATION_CONTEXT_THRESHOLD` | `0` (off) | the percentage of the context window at which a rotation is recommended |

It also lives in `config.json` as `rotation_context_threshold` and on
`GET`/`POST /api/assistant/config` under `rotation`, beside `checkin` and
`nudge` — all three are re-read on every tick, so a change applies without a
daemon restart and without a rotation. Precedence is the platform's usual chain:
the export beats the file beats the default, and the value reports which layer
decided it. The settings screen in the chat drawer edits all four numbers.

`0` disables the reminder and is the only off-switch. The ceiling is `100`,
because the figure it is compared against is itself a percentage clamped to 100 —
a higher threshold could never be met and would be a second off-switch nobody
documented. An unusable value costs its own layer with a warning and falls through
to the next, rather than stopping the daemon over a reminder; an explicit write
through the API is refused with a 400 naming the field.

**On upgrade**, in this order, because three of the four moving parts live in
different places:

1. **Build or pull the matching session image.** `lmer-ctl` and the `orchestrate`
   taskdef are baked into it (the repo is copied in at image build). The source
   and taskdef mounts that make edits live are *developer mode only*, so on an
   installed host an assistant from the old image has neither the `rotate` verb
   nor the breakpoint instructions — however new the daemon is.
2. **Restart the platform daemon** to run the new tick stage.
3. **Rebuild the UI bundle** for the settings screen.
4. **Replace the incumbent assistant.** A new image does not touch a running
   session, and `ensure_running` deliberately keeps a live one — so rotate or
   restart it through the usual operator path (the chat drawer's restart, or
   `POST /api/assistant/rotate`) before relying on the reminder. Until then the
   daemon can recommend a rotation to a session that cannot perform one.

Then set a threshold: the feature is off until an operator does. There is no
migration — nothing new is stored — and no container passthrough, since the
daemon reads the setting itself.

## Ratchet's memory

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
  directory, which nothing promises).
- **Liveness is a separate field, and it comes from the container.** The entrypoint
  linker leaves a `.lmer-mount-link` record in the staged store saying whether it
  made the symlink and, if not, why; `GET /api/assistant` serves it as the `link`
  block beside the counts. That is what makes `files: 0` diagnosable: with
  `link.linked` true it means "linked, nothing saved yet", with it false the
  `detail` names the obstruction, and `link: null` means no incarnation has left
  the fact — a session image older than the marker, or a store nothing has used.
  The session's own log still prints `🔗 Linked <path> → …` or a `⚠️` line, and the
  record is that line made durable and machine-readable. The marker is not counted
  among the store's files.
- **`link.stale` says whether the record is *this* incarnation's.** Only a session
  that ran the linker rewrites it, so one whose image predates the marker — or
  whose memory directory could not be prepared — leaves the previous
  incarnation's answer in place. `true` means the record predates the running
  session and describes a link it did not make; `null` means the question cannot
  be asked (no record, or nothing running). Several harnesses can declare a
  memory path and share one store, and a success is never downgraded by a later
  pair, so `linked: true` means *something* linked it, not that everything did.
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

Both chats — Ratchet's and any run's — take a file: drag it onto the composer,
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
- **Where Ratchet's land:** `~/.lmer/platform/assistant-uploads/`, one per host,
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
  For Ratchet, **restart** in the chat header is that restart.
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
| Ratchet's memory store | `~/.lmer/platform/assistant-memory/` |
| Ratchet's chat uploads | `~/.lmer/platform/assistant-uploads/` |
| a run's chat uploads | `~/.lmer/platform/logs/<session-id>.uploads/` |
| per-instance config (incl. service slots) | `~/.lmer/platform/config.json` |
| shared secret | `lmer platform secret` |
| per-session host log | `~/.lmer/platform/logs/<session-id>.log` |
| events ledger | `~/.lmer/platform/events.jsonl` |
| REST surface | `GET /api` (plain-text route index) |
