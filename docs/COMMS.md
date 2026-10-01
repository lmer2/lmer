# The unified communications channel

One channel, in both directions, with the supervisor as the single port.

An agent in an lmer session has one command to talk to the world,
`lmer-signal`, and does not know or care who is listening. Conversational messages
reach every connected party — the platform's chat pane, uber lmer, a Slack
thread, a matrix thread, a human at the terminal — and everything any of them
says arrives in the harness as an ordinary message tagged with where it came
from. The supervisor inside the container (`lmer-supervisor`) keeps the one
record of that conversation and serves it over its control plane. Nothing
outside the container talks to the supervisor except the process that holds
the session's control-plane token; no Slack, matrix or platform credential
ever enters the container.

Progress `note` records stay on the session bus and reach the platform and
orchestrator; Slack and Matrix connectors consume them without posting them
into threads. Use `post` for human-facing updates, decisions, and results.

Standalone and platform-attached connectors have no thread-silence timeout.
Listener-owned sessions retain their configured inactivity timeout (300
minutes by default). A new agent bus record, including a quiet note, or a
new live inbound record refreshes activity through the connector's host-side
lease. The listener accepts that timestamp only from its own child process
identity. Replayed records, empty polls, and connector restarts alone do not
refresh it. A genuinely idle listener-owned session still expires.

A live human message accepted by the bus gets a **📥 receipt reaction** after
about 20 seconds if the agent has not posted a conversational response first.
The connector owns this timer, so a long model turn or tool call does not
delay it. An agent `message`, `question`, or `milestone` after the input
cancels pending receipts for preceding messages. Quiet notes, other people's
messages, and posts addressed to another thread do not cancel them. This
rule uses bus order, without guessing which words answer which question.
For assistant sessions, the bus's authenticated `session_origin` marker
distinguishes the session's own output from external supervisor steering.
The reaction confirms session acceptance, **not model consumption**.

Receipts attach to each human message, never add a thread bubble, and do not
apply to history replay or cross-session bot traffic. Pending deadlines
survive connector reloads. Reactions already emitted remain as receipts.
Slack uses [`reactions.add`](https://docs.slack.dev/reference/methods/reactions.add/)
with `inbox_tray`; the app needs `reactions:write` and must be reinstalled
after that scope is added. Matrix sends an
[`m.reaction`](https://spec.matrix.org/v1.15/client-server-api/#mreaction)
with an `m.annotation` relation and a stable transaction ID. A reaction
failure is logged by the connector and does not block conversation delivery
or fall back to a message bubble.

Slack receipts run in a short-lived process with a five-second deadline.
On expiry the connector kills and reaps that process, so stalled DNS,
TLS, response bodies, or interpreter cleanup cannot hold up its 45-second
drain. Credentials travel over stdin, and the child inherits no connector
lease descriptors. The request uses Requests' normal certificate verification:
`REQUESTS_CA_BUNDLE` (or fallback `CURL_CA_BUNDLE`) accepts a CA file or an
OpenSSL-hashed CA directory. Rate limits fail immediately without retrying
or waiting for `Retry-After`. The receipt worker rechecks replies
between sends; a slow reaction cannot hide a reply to a later message.
An explicitly addressed post to the same canonical thread also cancels its
pending receipts.

This page is the contract connectors and tools ship against. The record shape
and the routes are versioned by `schema` (currently **1**), which moves only
when a field changes meaning; readers accept newer records for the fields
they know and carry the rest.

---

## 1. Message model (schema 1)

One record per communication, immutable once written. A JSON object, one per
line in the log and the same object on the wire.

```json
{
  "schema": 1,
  "id": "20260904-153332-7-21957/000042",
  "seq": 42,
  "session": "20260904-153332-7-21957",
  "at": "2026-09-04T16:02:11.482Z",
  "source": {"channel": "slack", "author": "vegu", "ref": "1725465731.001200"},
  "kind": "answer",
  "text": "prep-release",
  "in_reply_to": "20260904-153332-7-21957/000039",
  "options": [],
  "attachments": [],
  "meta": {"delivered": "cli"}
}
```

| field | type | rule |
|---|---|---|
| `schema` | int | `1`. |
| `id` | str | `<session>/<seq zero-padded to 6>`. Globally unique because session ids are. |
| `seq` | int | Per-session, allocated by the supervisor, strictly increasing, dense. **The cursor.** |
| `session` | str | The session id (`LMER_SESSION_ID`). |
| `at` | str | UTC ISO-8601 with milliseconds, stamped on acceptance. Order by `seq`, not `at`. |
| `source.channel` | str | `agent`, `supervisor`, `terminal`, `platform`, `uber-lmer`, `slack`, `matrix`, or a future connector's name (lowercase, letters/digits/dashes). Stamped by the party that vouches for it, never taken from the body alone. |
| `source.author` | str? | Who spoke, for rendering: a Slack display name, an MXID, an operator identity. Absent for `agent`, `uber-lmer` and an unnamed terminal. |
| `source.ref` | str? | The origin channel's own id for the message (Slack `channel:thread_ts:message_ts`, matrix `event_id`, a request id). Dedupe key for retries, and how a connector recognises its own message. |
| `kind` | str | `message`, `note`, `milestone`, `question`, `answer`, `close`, `command`. An unknown kind is stored, fanned out and rendered as plain text labelled with the kind. |
| `text` | str | Body. Caps enforced on post: question 8192, note 4096, milestone 1000, message/answer 16384, close 500, command 4096 characters. |
| `in_reply_to` | str? | An `id` on the same session. Required on `answer` and `close`; optional on `message`. |
| `options` | [str] | Questions only. At most 12, each at most 200 characters. A hint, not a menu. |
| `attachments` | [obj] | `{"name", "bytes", "media_type", "path"}` — references into the session's upload store. |
| `local_only` | bool? | Supervisor-only routing field, absent by default. Terminal candidates carry `true`: retained locally and omitted from every HTTP bus read, including replay (§5). Callers cannot set it through `POST /messages`. |
| `meta` | obj | Rendering and lifecycle status, never message routing. The supervisor sets `delivered` (`cli` or `pty`) on an accepted answer, `late: true` and `answered_by` on a late one, `option` on an answer that named its choice by index (§6), and `session_origin` on a session's own record posted for it. Every key in that list is reserved: a caller's own copy of one is dropped rather than stored. Any other key is kept as sent, `fidelity: "reconstructed"` from the terminal tracker included. |

### Kinds

| kind | who posts it | typed into the harness? | meaning |
|---|---|---|---|
| `message` | any channel; the agent via `lmer-signal post` | inbound yes, agent-origin no | conversational text |
| `note` | agent or supervisor | no | progress or lifecycle status, retained on bus/platform; omitted from Slack/Matrix threads |
| `milestone` | agent | no | reportable work happened (what a bare `lmer-signal "…"` means) |
| `question` | agent | no | blocks the asker until an `answer`; carries `options` |
| `answer` | any channel | to the waiting CLI if one is attached, else into the PTY | reply to a `question` |
| `close` | agent | no | the asker stopped waiting |
| `command` | `platform` and `uber-lmer` only | yes, **untagged** | a slash command for the harness (`/followup`) |

**Question state is a fold, not a field.** A `question` is open until the
first accepted `answer` naming it (answered) or a `close` naming it (closed).
Records are never rewritten, so "replay since" is a plain read of the log;
the supervisor keeps the fold in memory and serves it at
`GET /messages/questions`.

---

## 2. Where the record lives

The supervisor appends every record to
`/home/developer/.lmer-session/messages.jsonl` (mode 0600) **before** it
acknowledges or relays it. That directory is the same host bind mount the
session's own PTY log uses (`<platform state>/logs/<id>.session/` for a
platform-spawned session). On start the supervisor reads the file back to
continue `seq` and rebuild the fold; a torn last line is dropped, never
re-numbered. With nothing mounted (a laptop `lmer` run) the bus lives in
memory only.

**The file is the record, not a read path.** The platform daemon never reads
it: every daemon read goes over the control plane (§3) and the daemon's own
journal, so a hosted session with no shared filesystem behaves the same. To
let the daemon fetch the last records of a session that is ending, the
supervisor keeps its control plane up after the harness exits until every
acked subscriber stands at the last `seq` or `LMER_COMMS_DRAIN_GRACE` seconds
(default 5, at most 60) pass.

Inside the container the path is exported as `LMER_COMMS_LOG` so stdlib-only
hooks (`hooks/signal_guard.py`) can read it.

### Accepted session end

On an end request the supervisor publishes a `note` with
`source.channel: "supervisor"` and `meta.event: "session_end_requested"` before
attempting the harness's quit sequence. Its `at` timestamp is the acknowledgment
of shutdown; neither the CLI's printed message nor a wind-down prompt is one.
The agent credential cannot claim the supervisor channel.

The daemon associates the acknowledgment with the registry session established
by its authenticated subscriber. The record retains its container work-session
identity; that identity differs from the platform's `s-...` registry id.
The daemon persists the acknowledgment before acknowledging the bus record.

Before typing quit, the supervisor waits up to **six seconds** for the
`platform` subscriber to confirm receipt of this specific record through
`POST /messages/ack`. This covers the daemon's five-second reconciliation cycle
plus a round trip. No subscribers, registration at sequence zero, and another
subscriber's acknowledgment do not satisfy this wait. The agent credential
cannot acknowledge records on the daemon's behalf. The ordinary bus drain
still uses `LMER_COMMS_DRAIN_GRACE`.

Quit escalation and API finalization share this handoff deadline. If the harness
exits naturally while publication or delivery is pending, the API stays up for
the remaining handoff window. Once delivery is confirmed or the deadline expires,
the usual 10+10-second escalation can proceed; a blocked PTY writer cannot delay
it. Without authenticated delivery to the daemon, shutdown proceeds and the
detector stays unarmed.

Only authenticated bus ingestion can set the daemon's teardown acknowledgment.
Container-written files, including an old `end-request.json`, are never read as
lifecycle authority. They cannot turn a wind-down prompt into an accepted end.

The daemon samples the ending session's control plane and uses the configurable grace
described in [PLATFORM-QUICKSTART.md](PLATFORM-QUICKSTART.md) to flag stuck
teardown. Replaying the record does not reset a recorded loss interval. A failed
first fleet read retains newly stuck sessions for the next successful snapshot
so their named digests are not lost while establishing the daemon's baseline.

This lifecycle metadata is not typed into the harness. The supervisor keeps the
control plane until the harness has been reaped, then gives subscribers the
normal final-record drain grace before stopping it.

---

## 3. The supervisor's bus routes

All routes live on the supervisor's existing control plane and take an
`Authorization: Bearer <token>` header; 401 otherwise. A supervisor started
without a bus answers 503 on all of them, and its `/healthz` carries
`"comms": null`.

**Two credentials reach them.** The *control* token belongs to whoever owns the
session from outside — the platform daemon, the host `lmer` process, a connector
that process runs — and may say who is speaking. The *session* token is minted
by the supervisor for the harness it wraps and exported as
`LMER_FASTAPI_TOKEN`; it may only ever speak as `agent`, and may not ack. They
are different values on purpose: with one shared token any process inside the
container could post as `[slack · vegu]`, and a source tag nobody can trust is
not worth rendering. What crosses into the container is
`LMER_FASTAPI_TOKEN_SHA256`, the control token's digest — the supervisor
verifies against it and has no preimage to leak, and the container's own initial
environment is readable from any shell in it (`/proc/1/environ`, same user).

### `POST /messages` — post a record

```
body: {kind, text?, source: {channel, author?, ref?}, in_reply_to?, option?, options?, attachments?, meta?}
200:  {"message": <record>, "accepted_as": "new" | "duplicate" | "late", "relay": {...} | null}
400:  {"detail": "<why>"}  — empty text, over a cap, unknown in_reply_to, kind/channel
      mismatch, an out-of-range `option`, `option` on a kind other than `answer`,
      or `option` together with text (§6)
422:  {"detail": [...]}    — a malformed body; `option` is strict, so `true`, `"2"`
      and `1.0` are refused here rather than coerced

`text` may be omitted only on an `answer` that names its choice with `option`,
and on a `close`.
```

- A post naming no `source.channel` is taken as the agent's own (`agent`).
  The agent may post `message`, `note`, `milestone`, `question`, `close`.
- **403** when a post made with the session's own token claims any other
  channel, or an author: the source is stamped by the party that vouches for
  it, and the session vouches for nobody but itself.
- **`duplicate`**: a record with the same `(source.channel, source.ref)` is
  already stored. The stored record is returned; nothing is appended or
  relayed. This is what makes a connector's retry after a timeout safe.
- **`late`**: an `answer` to a question that is already answered or closed.
  Stored with `meta.late` and `meta.answered_by`, fanned out, and typed into
  the harness as a tagged message — a human said something — but never handed
  to a waiter.
- **`relay`** is present when the record was typed into the harness:
  `{bytes_written, submit_text, payload_sha256, payload_length, tagged}` —
  the `/input` receipt fields, so the caller can prove what was handed to the
  PTY. Before startup submission, it instead reports
  `{queued: true, bytes_written: 0}` (§4). A PTY that could not be written
  reports `error` here; the record is already on the log.
  The deprecated platform input route preserves `queued` and explains the
  startup hold in its `note`; raw `/input` writes remain immediate.

### `GET /messages?since=<seq>&timeout=<0..30>&kinds=<csv>&waiter=<question>` — subscribe

```
200: {"messages": [<record>...], "cursor": <last seq>, "schema": 1}
```

Returns everything past `since` immediately, or blocks up to `timeout`
seconds for the next record. `since=0` is the beginning of the log — the
whole "replay since" mechanism. `kinds` narrows the reply. `waiter` names a
question the caller is blocked on: while the call waits, an accepted answer
to that question is marked `delivered: cli` and handed to this caller instead
of being typed into the PTY.

### `POST /messages/ack` — a subscriber's position

```
body: {"subscriber": "<name>", "seq": <int>, "final": false}
200:  {"subscribers": {"<name>": <seq>, ...}}
403:  presented with the session's own token — acks come from subscribers
```

High-water marks, monotonic per subscriber. They feed the drain grace (§2)
and `/healthz`. An ack of **`seq 0` registers** a subscriber that holds nothing
yet, which is what gives the drain somebody to wait for: a session whose only
record is its last one would otherwise be drained instantly, with the record
still inside the container. **`final: true`** is a subscriber saying it has
gone, so a connector stopped at session end does not cost the whole grace.

### `POST /messages/claim` and `POST /messages/release` — a wait's claim

```
body: {"question": "<id>", "ttl": <seconds>}   (claim; release takes the id alone)
200:  {"claimed": "<id>"} / {"released": "<id>"}
```

A CLI about to wait holds that question's answer against the PTY leg, and gives
it up when it stops waiting. `lmer-signal ask` claims as it posts the question
(`wait: <seconds>` in the `POST /messages` body), so the gap between asking and
the first poll is covered; every poll with `waiter=` refreshes the claim. The
initial answer lookup also supplies `waiter=`, consuming an answer already
available when the CLI starts. Claims are capped at 120 seconds and refreshed
by subsequent polls. Both routes accept a sequence number, padded sequence or
full id and return the canonical id; an unknown question is 400. See
§6.

### `GET /messages/questions?open=<bool>` — the fold

```
200: {"questions": [{"question": <record>, "state": "open"|"answered"|"closed", "answered_by": <id>?, "closed_by": <id>?}]}
```

### `/healthz`

Gains `"comms": {"schema": 1, "session": <id>, "seq": <last>, "log":
<path|null>, "open_questions": <n>, "subscribers": {...}}`. **Its absence is
how a reader tells a pre-bus supervisor from a current one.**

`session` is the second such signal, and unlike the block's absence it is not
inert: a thread connector and a thread servicer both ask here whose session
they serve (§9, *Posts from other runs*), so a supervisor that does not report
it leaves every session-origin post unattributed — held for the grace window
and then dropped. A host that rolls the daemon and the CLI without rolling the
**session image** is exactly that case.

---

## 3b. The daemon's routes

The platform daemon (`lmer platform`) subscribes to every live session whose
`/healthz` carries `comms`, journals each record into one **fleet journal**
(`<platform state>/comms/journal.jsonl`, with a daemon-wide `cursor`), acks
its position to the supervisor as subscriber `platform`, and re-serves the
traffic. It reads no file a container wrote.

| route | what |
|---|---|
| `GET /api/comms/feed?since=<cursor>&timeout=<0..30>&session=<id>` | the fleet feed: `{"events": [{cursor, at, session, session_kind, run, title, message}], "cursor"}`; `since=0` replays the oldest retained journal generation |
| `GET /api/sessions/{id}/comms?since=<cursor>&timeout=` | one session's records, same cursor space |
| `POST /api/sessions/{id}/comms {kind, text?, author?, ref?, in_reply_to?, option?, options?, attachments?}` | the one inbound path; the reply is the supervisor's (`message`, `accepted_as`, `relay`). `option` (§6) needs a bus to resolve against, so on a session without one an `answer` naming an index is a `409` pointing at the ask route |
| `GET /api/sessions/{id}/threads` | the chat threads this session is mirrored into, and whether each connector is running |
| `POST /api/sessions/{id}/threads {permalink}` | connect one; the transport is read out of the link |
| `DELETE /api/sessions/{id}/threads/{thread_id}` | disconnect one |
| `POST /api/comms/ack {connector?, cursor}` | a platform-side client's position |
| `GET /api/comms/connectors` | acked positions and the minted connector credentials (names and channels, never values) |
| `GET /api/comms/questions?open=<bool>` | the question fold across every journaled session |
| `POST /api/sessions/{id}/ask/{qid}/answer {answer?, option?}` | the older answer route; one of the two is required and they may not both be given (§6); on a session with a bus it posts an `answer` (a late one is a `409`) against the bus question that *is* this ask-directory entry, and answers by file when the bus holds none (§10) |
| `POST /api/sessions/{id}/input` (submitted prose) | deprecated for one release: converted to a bus `message` (or `command`), answers `deprecated: true` |

**Source is stamped from the caller's credential**, never from the body:

| caller | `source.channel` | `source.author` |
|---|---|---|
| the shared secret | `platform` | body `author`, else `operator` |
| the assistant's minted credential | `uber-lmer` | none |
| a connector credential | the channel bound to it | body `author` |

`lmer platform connector add <name> --channel <channel>` mints a connector
credential (printed once, stored owner-only under `<platform state>/connectors/`);
`connector list` and `connector remove <name>` manage them. `kind: command` is
accepted from the operator and the assistant only.

The assistant receives everything: a `milestone` as the immediate
`session_signal` digest it always had, everything else coalesced into one
`session_comms` digest per session every 20 seconds. Assistant-origin records
(`source.channel: uber-lmer` or the supervisor's `meta.session_origin: true`)
remain in the journal but do not spool back to the assistant, including
milestones. Milestones still enter platform history and advance ingestion marks.
The ask-directory reader preserves the same history and suppresses only the
assistant's notification when the hub is absent or reconnecting. Worker and human
records still reach the assistant through the immediate and batched paths.

---

## 4. Relay into the harness

On a launch with a startup command, inbound typing waits until the launcher
has submitted that command through the supervisor's locked submit path. Bus
acceptance, durable logging, and pane visibility are immediate. The PTY legs
of `message`, `answer`, and `command` records queue and then flush in bus order;
records arriving afterwards relay normally. This includes records imported
from the compatibility ask directory and answers whose CLI waiter disappeared.

The launcher signals completion of its own startup submission (including its
existing retry nudges). If a seed prompt is configured, it is submitted next,
before the held records are flushed. The hold does not infer readiness from a transcript,
a prompt marker, or an elapsed delay. A failed or incomplete startup write does
not release it and emits a startup-submit warning. A manual launch or a launch
with no startup command has no hold.

Every accepted inbound record (any channel but `agent`) of kind `message`,
`answer` (when no CLI is waiting) or `command` is typed into the harness as
one submitted turn, through the same locked submit path `/input` uses:

```
[slack · vegu] which target branch?
[uber lmer] the review is posted; fix the two majors and re-signal
[matrix · @alice:example.org] (answer to #39 'target branch?') prep-release
[matrix · @alice:example.org] (late answer to #39 'target branch?', already answered by 20260904-…/000040) main
[terminal] ok go ahead
```

The tag is `[<channel>]` or `[<channel> · <author>]`; `uber-lmer` renders as
`uber lmer`. Because the tag always takes column one, a message starting with
`!`, `#` or `/` can no longer reach the harness's first-column command
escapes: **the tag is the defusal**. A `command` is relayed untagged so the
harness reads it as the slash command it is; only `platform` and `uber-lmer`
may post one.

Agent-origin records have no PTY leg. Terminal-origin records (§5) have none
either — the harness already has them.

### What the chat pane draws

The pane reads one route, `GET /api/sessions/{id}/messages`. The joins below
keep copies from being drawn twice; terminal records remain a separate case.

**The join, on the read path.** Where the harness's transcript holds the same
words as a bus record, the **bus record is the turn** and the harness's own copy
is hidden — it is what every other channel saw, and it alone carries the source
and kind the turn is labelled with. That copy exists in both directions: the
tagged line the supervisor typed in for an inbound record, and the prose the
agent wrote before posting it with `lmer-signal post`. Matched on collapsed text,
one copy per record, spent on the last turn at or before the record's own time —
so a reply genuinely sent twice is still two turns. A turn that also made tool
calls keeps its place with the words removed, because the calls are recorded
nowhere else.

**Outbound tool copies, in the pane.** Alongside the join above (T4), the pane
hides a literal `lmer-signal` `post`, `note` or `ask`, or `lmer-slack post`, tool call
when its whitespace-normalised body matches a loaded `message`, `note` or
`question` assistant bus record from `agent` or `uber-lmer`. It removes only the matching
tool: unrelated tools, calls marked failed and the agent's own prose (including closing
prose) stay; an otherwise empty turn disappears. The record must be in the
pane's loaded window; each record hides at most one preceding tool copy, nearest
first. The route's nullable tool `command` field carries the complete scrubbed
command from Claude, pi or Codex arguments, up to 8,000 characters. Missing or
over-limit commands are `null`. Canonical transcripts never supply a command,
even if an unknown `command` key is present; their tool schema carries hints
rather than raw arguments. The existing first-line, 160-character `detail` remains
unchanged and is never match evidence. The 8,000-character ceiling is per field: a
tool-heavy turn can contain several full native commands. It does not bound the
whole page payload, so clients still need pagination. Shell expansions, compound commands and
unknown/incomplete commands stay visible. File/stdin bodies and attachment posts
remain visible because command text alone does not describe their payloads.
Milestones, the bare `lmer-signal "text"` spelling and unsupported options remain
visible too. Claude and pi report failed calls, so failed asks (including wait
timeouts) remain visible with those adapters. Codex currently marks every
returned `function_call_output` as `ok`; a failed ask can therefore be hidden
when it matches a bus record. Its adapter cannot guarantee failed-ask visibility.

**Thread feedback.** Connect and Disconnect immediately show `connecting…` or
`disconnecting…` beside the thread controls and on the affected entry, with repeat
actions disabled. A successful request keeps that state until the next fleet or
assistant status confirms the thread is present or absent; a refusal restores
the control and shows the daemon's error text. A new session clears pending
actions belonging to the previous session.

Not joined: a **terminal** record. It has no PTY leg, so the tagged line
`[terminal] deploy it` was never typed anywhere and cannot match the raw
`deploy it` the harness recorded, and the operator's line is drawn twice — once
plain, once chipped `· typed`. An exact-text join would not settle it either,
since a terminal record's text is reconstructed from keystrokes and is not
guaranteed to equal what the harness received.

**Stable ids, for a pane that is already open.** Every turn carries an `id`: a
bus record's own id, or — for a turn read from a transcript — its path below the
session directory, or below the harness transcript root where a recorded pointer
lives, plus its position in that file, which is append-only and so does not move.
A file below neither root is named by a digest of its path. `seq` cannot serve
this purpose: it is a position in the *merged* conversation and every read
renumbers from scratch, so a transcript row written after the record it precedes
in time shifts every later turn by one, and a client paging by position and
appending drew the record it already held a second time.

The pane uses that id for two things, and both are recognition rather than
placement: an arriving turn whose id is already rendered is dropped — by the
forward poll and by the backward *earlier messages* read alike — and it is the
row's render key. That is what closes the case above: the record comes back under
a number the pane has never seen, carrying a name it has.

**Still open (#419).** Recognising a turn is not the same as knowing where it
goes. The pane still holds a *window* it reads by `since`/`limit`, and it can
only append to it, so three things follow. The third is the one to read twice,
because it is the opposite shape from the other two:

- A turn the server stops serving is not withdrawn. This has two forms. A harness
  copy that made tool calls is kept by the join with its words removed, so the
  record is served as a new turn and the pane draws it beside the copy it already
  has — the same words twice. A **prose-only** copy is dropped by the join
  instead, so the conversation's length does not change, the pane's next poll from
  its cursor is served nothing, and the record never arrives at all: the copy
  stays, permanently, without the source chip that says who spoke.
- A turn whose text changed between reads is not replaced where it stands.
- **A turn inserted below the pane's cursor is never drawn.** This is the same
  event the id closes the duplicate half of. A row written late lands below the
  cursor, every turn after it renumbers, the poll returns those turns, the dedupe
  correctly drops them — and the cursor advances past the inserted row, which no
  forward read will offer again. *Earlier messages* is not a recovery path: it
  asks from the head's own stale `seq`, and for a pane opened at the start of a
  conversation `canLoadEarlier` is false and the button is not there. The only
  trace is the header counting a turn the list does not hold.

These limitations still apply to run chats. The assistant pane re-reads its
loaded window on each poll, replacing cached membership and content. This makes
a late bus post replace its cached prose copy without reopening the pane.

Removing the position from the run-chat read path is issue 419; a client that never infers
a shift can withdraw, replace and insert as well as append. Until then the pane's
window is also unbounded — it grows as the session runs — which is the same issue.

### What a turn is, and what each view draws

A **turn** is one harness response cycle. It starts when an input lands — an
operator message relayed from the bus, a watch firing, a task notification, a
hook's re-prompt, a message queued while the model was working and delivered
mid-turn — and ends when the model stops and the Stop hooks run. Inside
it the model alternates prose and tool calls, and Claude Code writes one
transcript record per content block: a prose block, a tool call and the model's
thinking are three records, so one turn is usually several. The normaliser turns
every record that carries something into one served turn; a record that only
feeds the model (a tool result) is folded into the call it answers and served as
nothing. Measured on a recorded uber lmer session of 1,816 records: 132 assistant
prose blocks in the file, 132 served, and a time-ordered replay of the pane's
polling delivered all 491 served turns once each.

The uber lmer pane has two views, chosen by the **Show uber lmer's working
transcript** switch in its settings dialog — `chat_show_transcript` on
`GET`/`POST /api/assistant/config`, stored in `config.json`, applied on the pane's
next config refresh in every browser. The run chat keeps its older rendering
(spoken turns, the harness's internals behind a button) and is not in this table.

| what the route serves | recognised by | conversation (default) | transcript |
|---|---|---|---|
| a bus record: the operator's message from any channel, the agent's `post`/`note`/`ask`/answer/milestone, a platform `command` | `via: comms` | drawn as speech, with source and kind | drawn as speech |
| a reply to a conversation request, sent with `lmer-ctl thread post` | assistant credential; platform-only conversation journal, `via: comms` | drawn with destination link; no broadcast to other threads | drawn |
| a private DM reply | delivery fact only; body never journalled | drawn without body | drawn without body |
| a question (`question`/`live_question`), crash (`crashed`), or completion (`complete`) digest | structured event kind; platform-only journal, `via: comms`, platform role | drawn even before the assistant drains its spool | drawn |
| routine check-ins, wake traffic and other digest categories | excluded from the platform-only journal | hidden unless explicitly posted | any harness copy is muted |
| the legacy ask-channel exchange, on a session with no bus | `via: ask` | drawn | drawn |
| the agent's unposted prose | `role: assistant`, no `via` | hidden | drawn muted, chipped `transcript` |
| a tool call | `tools[]` on a transcript turn | hidden | drawn muted, one chip per call |
| the harness copy of a posted reply, or of a relayed inbound line | its text equals a bus record's (the join above) | not served — the record is the turn | not served |
| the literal `lmer-signal post`/`note`/`ask` call carrying a loaded record's text | the outbound rule above | hidden | hidden — the record stands for it |
| a watch firing | `role: monitor` | hidden | drawn muted, titled *the watch fired*, on the action ground |
| a hook re-prompt, a task notification, a system reminder, the harness's own "previous response had no visible output" re-prompt (a `user` record marked `isMeta`) | `kind: injected` | hidden | drawn muted, chipped `internal` |
| the harness's `system` prose | `kind: notice` | hidden | drawn muted |
| the harness's silent-turn reminder and its other non-message attachments | not a served record | not served | not served |
| a terminal keystroke line as the harness recorded it (§5: not joined); a `[platform]` reminder typed into the PTY | `role: user`/`platform`, no `via` | hidden | drawn muted |

Unposted terminal prose stays out of the conversation. The daemon's PTY nudge
only announces that digests are waiting; it stays in the transcript. Required
digests have their own durable records, so draining the spool or restarting the
assistant does not remove them. The platform-only journal is separate from the
fleet feed and session buses: neither reply copies nor digests are broadcast to
connected threads. It retains the same bounded disk generations as the comms
journal and serves the most recent 2,000 records. The daemon retains one journal
instance for the resolved platform path, loading retained history once. A path
change replaces that instance. Run chats do not read it.

Thread posts return `pane_recorded: false` if the provider accepted the reply but
its pane copy could not be stored. The successful provider receipt still stands:
retrying the post would send it twice. DM thread replies retain only a delivery
fact and character count; when a Matrix room's privacy cannot be established,
its body is also withheld from the journal.

The reply-routing guard (`hooks/slack_reply_guard.py`, §8) reads only the ending
turn. Its `PreToolUse` hook adds the harness session and tool-call IDs to each
Bash child's `LMER_ROUTING_CALL` environment. This uses Claude Code's
[`updatedInput` contract](https://code.claude.com/docs/en/hooks#pretooluse-decision-control),
preserves the other tool arguments, and returns no permission decision. Each
child retains its own identity, including parallel and background commands.

After a successful post, ask or DM, the CLI atomically writes a body-free local
record keyed by the lmer session, harness session and tool-call ID. Records live
in the container's temporary directory under `lmer-routing/`; only successful
delivery creates them. The Stop hook checks those records for calls in the ending
turn. Reading a saved API response, quoting an example or redirecting a CLI's
output cannot create or transfer delivery evidence. Missing or unreadable records
supply no exemption. This is correlation for cooperative routing producers, not
an attestation boundary against a process deliberately rewriting local state.

Routing CLIs still print JSON delivery metadata on stderr, and the platform
thread-post route includes it in its JSON response. Those output objects are not
proof to the guard. `lmer-ctl thread post` writes its local record after the live
POST succeeds. An `ask` records delivery before waiting, so a wait timeout still
counts. A later failure to store a DM/thread-post pane copy does not undo delivery;
a local evidence-write failure is logged without changing the successful send's
exit status. Notes, milestones, help and failed sends create no record.

A turn with substantive prose and no delivery record is nudged once. An empty
turn owes nothing and may end without a filler line. Roll the session image with
the PreToolUse/Stop hooks and CLI changes together; older CLIs supply no local
record. The records disappear with the container and contain no message body.

---

## 5. The terminal as a source

A human at the host TTY, or at the browser terminal pane, types keystrokes,
not messages. The supervisor rebuilds submitted lines from those keystrokes:
printable bytes accumulate, backspace removes a character, escape sequences
are dropped, bracketed paste is taken verbatim, Ctrl-C and Ctrl-U clear the
line, Enter emits it. Each emitted line becomes
`kind: message, source: {channel: "terminal", author?}, meta: {fidelity:
"reconstructed"}` on the local bus.

With a supported transcript reader, this is a **candidate**, stamped
`local_only: true`. The supervisor holds it until a fresh user turn matches its
complete text after whitespace normalisation. Each transcript occurrence can
confirm one candidate. The matcher reads the transcript directly; the merged
chat view includes bus records and cannot provide independent evidence.

A match appends a terminal message with `meta.fidelity: "confirmed"` and
`in_reply_to` naming the local candidate. External subscribers receive only
this message. The original remains immutable. Unmatched candidates stay in the
local log after expiry and never appear in external bus reads, even on replay.
The cursor advances past local records, so they do not hold up other messages.

The window defaults to **30 seconds**. The daemon resolves
`LMER_PLATFORM_TERMINAL_CONFIRM_SECONDS` over `terminal_confirm_seconds` in
`~/.lmer/platform/config.json` over the default. Values are positive integer
seconds; blank or invalid environment values fall through to the stored value.
Set or clear the stored value through `POST /api/assistant/config`; GET reports
the effective value and source under `terminal.confirm_seconds`.
The daemon passes it at launch and updates running supervisors on their next
bus poll. New submissions use the new window; existing candidates retain their
submission-time deadlines. Each poll matches fetched evidence against its
delivery timestamp before expiring candidates. Delivery before the deadline
can confirm even when that poll arrives late; delivery at or after the deadline
cannot. Without a timestamp, the poll must arrive before the deadline. Increase
the window for sessions where queued prompts commonly take longer.

Claude, Codex and pi use native transcript readers. A temporarily missing or
unreadable transcript does not confirm anything. Drop-ins with a recognised
native or canonical lmer transcript can confirm once that format is detected.
Harnesses with no declared transcript directory or no recognised reader retain
immediate `fidelity: "reconstructed"` delivery. For standalone sessions, the
same environment override sets the supervisor window over the default.

The supervisor reports `terminal_mirror.window_seconds` on `GET /messages`.
The host control credential may update it with
`POST /messages/terminal-mirror`, body `{"window_seconds": 30}`. Non-positive
values and non-integers return 400; session credentials return 403. This
updates timing only and does not expose candidates.

This is best effort and says so — in the message, not only here: the chat pane
marks such a line **· typed** (`fidelity` on the turn), Slack and matrix add
**· typed** to the source label, and the connectors carry
the same record, because cursor movement, tab completion and a TUI's own edits
are invisible to the tracker, so the mirror can differ from what the harness
received. A line whose first character is `/`, `!` or `#` is the human
driving the TUI and is not mirrored. `LMER_COMMS_TERMINAL_MIRROR=0` turns the
tracker and confirmation off. Both reconstructed and confirmed external
messages retain the **· typed** label.
Bus validation applies to each reconstructed line separately. A rejected line
does not prevent later lines in the same input chunk from being mirrored.

---

## 6. Questions across channels

```
agent       lmer-signal ask "target branch?" --option main --option prep-release
supervisor  seq 39, kind question, open; fan-out
channels    UI card · Slack: text + numbered options · matrix: the same in the thread
vegu/Slack  replies "2"  → the connector posts answer, in_reply_to 39, ref <channel:thread_ts:ts>
supervisor  first accepted answer wins, under one lock
            a CLI is waiting?  → its stdout: "prep-release", exit 0
            none waiting?      → typed into the PTY: "[slack · vegu] (answer to #39 …) prep-release"
            fan-out            → UI: answered by slack · vegu · matrix: "answered from slack by vegu"
alice/matrix "main", a minute later → accepted_as late; stored, fanned out, typed as a late message
```

**A number is an ordinal only where a numbered list was rendered.** Slack and
matrix draw `1. main / 2. prep-release` and tell the human they may reply with a
number, so those connectors read `2` as an ordinal and post the option's text.
No other door showed a number: the platform's ask card draws one chip per
option text, `lmer-signal list` prints `options: main, prep-release`, and
`lmer-ctl answer` shows nothing. So the supervisor infers nothing from an
answer's words — a question offering `3 · 5 · 10` answered `1` would otherwise
arrive as `3`.

**Every other door names the option by index or by its text.** An `answer`
carries an optional `option`: the 1-based index of the choice the human picked,
into the question's `options` in the order they were given.
`MessageLog.append()` validates it against that list — an index the question
does not offer is refused with the count — stores the option's own text as the
record's `text`, and records the index as `meta.option`. Such an answer needs no
words of its own. An index **with** text is refused rather than one silently
winning — the loser would be the operator's words, and which of the two they
meant is not the bus's to guess — unless the text names that same option, which
is a redundancy rather than a contradiction and is what the ask-directory answer
file carries. A body that *is* an option's text still matches it, case and
surrounding space aside, so the record carries the canonical spelling. Anything
else is stored as sent.

The doors that accept an index: `POST /messages` on the bus,
`POST /api/sessions/{id}/ask/{qid}/answer` and `POST /api/sessions/{id}/comms`
on the platform, `lmer-ctl answer --option N`, and an ask-directory
`.answer.json` carrying `option` (§10). The Slack and matrix connectors send
resolved text today and may send an index in a later change. The platform's ask
card sends the chip's text and is unaffected.

Rules: the first accepted answer wins and the supervisor decides it; the
fan-out of the accepted answer *is* the echo to the other channels, and the
originating channel skips its own record; a `close` fans out so every channel
withdraws its reply affordance; a late answer is a message, never a re-answer.

**What binds as an answer in a thread.** A human writing in a Slack or matrix
thread has not promised to be answering, so only three things bind, and
everything else is an ordinary message that leaves the question open:

1. a **reply relation** to the question the connector posted — matrix's
   `m.in_reply_to`; Slack has no per-message relation inside a thread, so
   nothing satisfies this there;
2. an **option match**: the option's number (`2`, `2.`, `2)`) or its own text;
3. the **first human message after an option-less question** — a question with
   no options is asking for words, and the first ones are the answer.

"hold on, let me look" is not an answer. Binding it as one closed the question
under first-answer-wins, told every channel who answered, and handed the words
to `lmer-signal ask` as its exit-0 stdout.

**Delivery is guaranteed one way or the other** — the waiting CLI's stdout, or
the harness's PTY — and the claim that decides which is durable rather than
"a request happens to be open". `ask` claims the answer as it posts the
question, `wait <id>` claims before it looks, every poll refreshes the claim,
and every exit releases it, including an error in the initial poll. An answer
that arrives with a live claim is
`delivered: cli`; with no claim it is `delivered: pty` and typed in at once.
If the claiming process dies without reading its answer, the supervisor's
watcher types the answer in when the claim lapses — a harness command killed
at its own timeout is the ordinary case, and the guarantee cannot rest on the
process surviving.

The stopped-run question (`work state set --stop-reason=question`, no
container, answered by `POST /api/runs/answer` which spawns) is a different
mechanism and is unchanged.

---

## 7. Loop prevention

1. **Origin is stamped once**, by the party that vouches for it: the
   supervisor for `agent` and `terminal`, a connector for its own channel,
   the daemon from a caller's credential.
2. **A connector never posts to its channel a record whose `source.channel`
   is its own.** Its own post comes back on the subscription; it skips it.
3. **A connector never ingests from its channel what it itself sent there** —
   what *it* sent, which is not the same as what the app it shares with every
   other session sent. A chat connector decides that from a poster the host
   stamped (§9, *Posts from other runs, and from the platform*), so another
   run's post into the thread arrives instead of being dropped as an echo; the
   terminal has no such question, because the supervisor reads only keystrokes
   and never its own PTY writes.
4. **Posting is idempotent** on `(source.channel, source.ref)`; every connector
   sets `ref` from its channel's message id.
5. **One PTY leg per inbound record**, once, under the write lock.
6. **Late answers are messages, not answers.**

Tags are never parsed by a connector to make decisions; a relayed line quoted
back by a human is just text.

---

## 8. The agent's command: `lmer-signal`

```
lmer-signal "<what happened>"                       milestone (the historical spelling)
lmer-signal milestone "<text>"                      the same, explicit
lmer-signal note "<text>"                           quiet bus/platform progress
lmer-signal post "<text>"                           a message to the humans
lmer-signal thread open <channel> ["<text>"]        open a thread there and connect to it
lmer-signal thread connect <permalink>              connect to a thread that exists
lmer-signal thread disconnect <permalink>           let one of its threads go
lmer-signal ask "<question>" [--option T]... [--timeout S] [--interval S] [--no-wait]
lmer-signal wait <id> [--timeout S]                 resume a wait
lmer-signal close <id> [--reason T]                 stop waiting
lmer-signal list [--open] [--json] [--since SEQ]    the conversation so far
```

Text may be given positionally, with `--message-file PATH`, or with
`--stdin` / `-` (prefer the last two for anything with backticks, `$` or
quotes). `--attach PATH` (repeatable, on `note`/`post`/`ask`) copies a file
into the session's upload store (`LMER_UPLOADS_DIR`) and references it on the
record; without a store it is refused.

The command talks to the supervisor at `http://127.0.0.1:$LMER_FASTAPI_PORT`
with `LMER_FASTAPI_TOKEN`, both inherited from the supervisor; a standalone
connector process sets `LMER_SUPERVISOR_URL` instead of the port.

The three `thread` verbs act on the run's **own** connected threads, and are
RPCs to the host side exactly as `read` and `post --thread` are (§12) — the
container holds no Slack or matrix credential, and the connections themselves
are platform state. `open` prints the new thread's permalink on stdout, alone,
so a shell can capture it; the rest goes to stderr. §9 has what they do and
what they refuse. A session whose host side is a shell `lmer` process is
refused by name: that process takes its threads as launch arguments and cannot
change them while it runs.

Exit codes: `0` posted/answered/closed/listed · `1` a real error · `2`
waited, no answer yet (the question is open; `lmer-signal wait <id>`
resumes) · `3` no channel (no control plane in the environment, or a
supervisor with no bus). The default wait is 90 seconds, under the tightest
harness command timeout in the fleet; a longer wait is a chain of waits.

### Compatibility shims (one release)

| old | new | shim |
|---|---|---|
| `lmer-ask ask/note/wait/close/list` | `lmer-signal ask/note/wait/close/list` | same stdout and exit codes; one deprecation line on stderr; `--dir` accepted and ignored |
| `lmer-slack post` | `lmer-signal post` | posts on the bus; a session still carrying `SLACK_BOT_TOKEN` also posts to the thread directly |
| `lmer-slack history` | `lmer-signal list` | without a token, lists the bus |
| `lmer-slack watch` / `poll` | — | inbound arrives as tagged turns; without a token they say so and exit 0 |

### Hooks

`hooks/signal_guard.py` counts only the milestone forms of `lmer-signal`
(bare text, or `milestone`) as signals and reads `LMER_COMMS_LOG` first for
the newest milestone and the newest open question, falling back to the ask
directory. `hooks/slack_reply_guard.py` is armed by `LMER_SLACK_CHANNEL` or
`LMER_CHAT_CHANNEL` and reads only the turn that is ending (§4, the turn
table): `lmer-signal post`, `ask` and `dm` — and the `lmer-slack post` shim —
count as routing wherever they fall in the turn, a turn that wrote substantive
prose and routed nothing is re-prompted once, and a turn that wrote nothing may
stop with an empty response.

---

## 9. Chat-thread connectors: Slack and matrix

Platform-spawned sessions accept `targets`, a JSON list of Slack or matrix
thread permalinks on `POST /api/sessions`. They are additional targets beside
`target`, passed to the host `lmer` launch — and on the platform side they are
also **connections** the daemon owns and can change while the session runs, so
they can be added and removed from the UI (see *Connecting and disconnecting a
thread while a run is up*, below). From the control CLI:

```sh
lmer-ctl spawn develop ISSUE_URL --target-thread SLACK_THREAD --target-thread MATRIX_THREAD
```

For **uber lmer**, open its settings beside model/harness/preset, enter the
thread permalinks **one per line**, save, then restart (rotate). The equivalent
API is `POST /api/assistant/config {"threads": ["<permalink>"]}`; it stores
`assistant_threads` in `config.json`. `GET /api/assistant/config` reports the
list under `settings.threads` with `stored`, `value` and `source`. Each start
and rotation reads the saved list fresh and launches
`lmer orchestrate fleet <thread> ... --fastapi`. The threads therefore survive
rotation without a daemon restart.

`POST /api/assistant/start` and `/rotate` also accept `threads` as a per-call
override. An omitted or null override uses the saved list; `[]` connects to no
threads for that incarnation. An override does not rewrite the saved setting.
Saving `[]` disconnects future incarnations; saving null clears the setting.
There is no environment layer for this list. Both surfaces accept at most 16
permalinks of at most 4096 characters each, refuse malformed lists/links with
400 before spawning or stopping an incumbent, and reject embedded credentials.
Slack path timestamps must contain ASCII digits; a `thread_ts` query must be
numeric seconds followed by a dot and six fractional digits. Invalid timestamps
are refused even when another timestamp in the same link is valid.

Every configured thread gets its own connector, polling loops and cursor,
including several threads on the same transport. Links with the same parsed
conversation and root event connect once, including alternate query strings,
trailing slashes and equivalent matrix.to/Element links. The first thread per
transport supplies the container's chat context; all threads receive the bus.
The existing rule that a transport never
mirrors its own channel's records back remains in effect.
Slack inbound deduplication uses `channel:thread_ts:message_ts` as `source.ref`,
so equal timestamps in different threads remain distinct messages. Repeated
delivery from the same thread still records and relays the message once.

Credentials belong in the **daemon's host environment**: the daemon runs these
connectors as its own children, and a shell-launched `lmer chat` runs its own.
The daemon also stamps `LMER_SESSION_KIND` from the session kind it owns.
For posts authenticated with the session credential, the supervisor uses that
launch identity to stamp its own outgoing records as `uber-lmer`, rendered
`[uber lmer]`; workers remain `agent`.
The session credential still may claim only `agent`, may not name an author,
may not post commands, and may not ack. The supervisor sets
`meta.session_origin` on its own assistant records, discarding any caller's
value; this prevents self-relay and keeps the chat pane's reply on the assistant
side. It also counts those records against the existing agent caps. A human
reply is a normal inbound turn such as `[slack · Alice] hello`.
Control-credential posts retain their claimed source; claiming `agent` does not
give them the assistant's identity or its `session_origin` marker.

Hands-on: **set the assistant's Slack thread in settings, rotate it, mention
it in the thread, it replies in the thread and the chat pane shows the same
exchange**. Repeat with a matrix thread or several threads to check fan-out.
The reply is posted with `lmer-signal post`, as in a connected worker session.

A Slack thread permalink or a matrix thread permalink is a session target
(`lmer chat <permalink>`, or an extra target on any taskdef). The target
handler in the host `lmer` process (`SlackThreadTargets`,
`MatrixThreadTargets` in `lmer_cli/targets.py`) then:

- turns the supervisor control plane on for the session and mints its bearer
  token host-side when the environment did not supply one;
- prepares a per-session **upload store** under `~/.lmer/uploads/<session>/`,
  bound at `/home/developer/.lmer-uploads` (the #246 path) and announced as
  `LMER_UPLOADS_DIR`;
- sets the container's chat context — `LMER_CHAT_CHANNEL` (`slack` or
  `matrix`) beside the channel's own variables — and keeps every credential
  out of it: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` and `LMER_MATRIX_*`
  credentials are seeded `None` so a `.env` file cannot forward them;
- supervises a separate **connector child** for that thread and waits for it
  to stop when the session ends.

The connector is a separable component with its own entrypoint and
configuration — `lmer-slack-connector`, `lmer-matrix-connector` — given a
supervisor address (`LMER_SUPERVISOR_URL`, or `LMER_FASTAPI_PORT` on
loopback), the supervisor token, the thread, and the channel's credentials.
Both the CLI and platform supervise connector children. Ownership, durable
receipts, module reload and explicit per-operation recovery are described in
[CONNECTOR-RECOVERY.md](CONNECTOR-RECOVERY.md). A failed or uncertain remote send
is never blindly replayed. The connector runs until stopped:

- **outbound**: long-polls the supervisor's `GET /messages`, omits quiet notes
  and records from its own channel, and posts the rest (a question with numbered options,
  an answer from another channel with who gave it, milestones with a
  glyph, other channels' messages tagged `[uber lmer] …`), and acks its
  position under a per-thread subscriber name (`slack:<hash>` / `matrix:<hash>`);
- **inbound**: Slack polls `conversations.replies` every
  `LMER_SLACK_POLL_SECONDS` (default 3); matrix `/sync`s with a filter on the
  one room. Each new human message — and each post another run or the platform
  made, below — becomes `POST /messages` with `source
  {channel, author, ref}` — the Slack display name (from `users.info`) or the
  MXID, and the message's own `ts` / `event_id`. While the session has an open
  question the message is an `answer` to the oldest one, and a reply that is
  exactly an option's number or text is that option, and the rest of §6's
  binding rule decides whether anything else does; otherwise it is a
  `message`. On attach, one notice describes the thread's existing messages
  and names the command to read them on demand. Messages arriving *after*
  the attach snapshot are one record each, as they always were.
- **files**: a file in a live thread message is downloaded into the session's upload
  store — capped at the same size a read of a thread copies in — and the record
  names it with the `[lmer upload] <path> (<type>, <size>)` reference #246
  defines. The agent's `--attach` goes the other way:
  Slack posts the name; matrix uploads the bytes only under
  `LMER_MATRIX_AUTHENTICATED_MEDIA=1` (the operator's assertion that the
  homeserver requires authentication for media) and only when the bytes pass
  the scrub, else posts a one-line "withheld" note.

**Attach delivers one notice.** A single `message`, tagged `[slack]` or
`[matrix]`, names the thread, its full snapshot count (root included), the
oldest and newest UTC timestamps, and the exact read command:

```text
[replay notice] the slack thread C0123ABCDEF:1788784440.000000 has 20 earlier messages (2026-09-07T12:34:00Z .. 2026-09-07T20:35:00Z). Read them with: lmer-signal read https://example.slack.com/archives/C0123ABCDEF/p1788784440000000 --limit 20
```

The count includes the full conversation: human messages, bot/session replies,
and file-only messages. Connector attach/detach status notices are excluded by
their explicit transport markers: a `lmer-connection-status:` block id in a
Slack post by this app, or `net.20c.lmer.connection: true` in a matrix event
from the connector's bot. The acknowledgement's visible text is unchanged;
the marker survives reconnects without consulting a local attribution cache.
Older unmarked status posts remain ordinary history. There is no 20-message replay limit or overflow
trimming. A root-only thread gets the same notice with a count of 1. No bodies,
authors, upload references, or attachments are carried by the notice, and no
backlog files are downloaded on attach. `lmer-signal read` copies files into the
upload store when the run asks for the history (§12).

Slack's published [replies response schema](https://github.com/slackapi/node-slack-sdk/blob/main/packages/web-api/src/types/response/ConversationsRepliesResponse.ts)
includes `messages[].blocks[].block_id`, and its
[section block reference](https://docs.slack.dev/reference/block-kit/blocks/section-block/)
defines the caller-supplied identifier. These are the upstream basis for the
marker; the local fake verifies our handling, not Slack's service behavior.

The snapshot is the first thread read (plus matrix's initial sync window).
Messages already present in that snapshot contribute to the notice; later
messages arrive individually. `source.ref` is the newest snapshot message's
ref, so restarting a connector on an unchanged thread posts the same ref and
the bus relays nothing twice. A changed snapshot gets a new ref. The connecting
ack posted to the thread is unchanged.

**§6's binding rule does not apply to the notice.** It is always a `message`,
never an `answer`, whatever questions are open. It does not spend the first
free-text message slot after an option-less question: the next live human
message can still answer it.

**The listeners** default to spawning sessions: `lmer-slack-listener` on a
mention or DM
(socket mode, as before — it relays no content), `lmer-matrix-listener` on a
mention of the bot's MXID or `m.mentions` entry from an MXID in
`LMER_MATRIX_ALLOWED_USERS` (explicit MXIDs only; empty means nobody — the
homeserver federates). Both spawn `lmer chat <permalink>` as a subprocess and
share the session manager's capacity gate, per-thread dedup (the thread
registry, keyed by channel kind) and idle reaper; the `LMER_SLACK_CHAT_*`
tunables apply to both.

With `LMER_ASSISTANT_MENTION_ROUTING=true`, both listeners instead send eligible
mentions and replies beneath assistant-sent DMs to Uber Lmer's private pending
inbox. Existing connected threads keep their normal delivery. See
[assistant mention routing](ASSISTANT-MENTIONS.md) for configuration, context,
replay protection and explicit replies to the originating conversation.

Daemon-owned connectors inherit the daemon's resolved host bind address, port
and secret-file location, including `lmer platform run --bind ... --port ...`
overrides. Connected-thread mentions therefore reach the same platform that
owns the connector without duplicating configuration. Wildcard listeners use a
loopback client address; IPv6 client addresses are bracketed. The platform host
endpoint remains separate from the session's supervisor URL/control token and
from container-facing platform URLs.

A running connector process can still have failed delivery. Thread status shows
`delivery-retrying`, the failing direction, unresolved failure count and error
class from its durable checkpoint. Recovery clears that status after a successful
iteration. The existing consecutive-failure limit can restart a persistently
failing connector; its checkpoint retains cursors and pending delivery identity.
Routine retry/recovery notes retain `connector_health` metadata and do not wake
project assistants (#512).

**Limits stated plainly.** The matrix connector speaks the client-server API
as a bot account and handles plaintext rooms; an end-to-end encrypted room is
refused at connect time with its own name, and an encrypted event in a plain
room is logged and skipped. Encryption for this connector is a follow-up.
In the default mode, both listeners spawn subprocesses; spawning through a
platform daemon is not wired.

The matrix chat connector is not the fleet bridge (`lmer-matrix-bridge`,
`docs/MATRIX-CHAT.md`), which announces runs wanting a human into one room per
orchestrator as an appservice. The two coexist; the connector reuses the
bridge's scrub and nothing else.

### Posts from other runs, and from the platform

A thread is where two runs, or uber lmer and a run, talk in front of the
humans, so a connector has to ingest a message its own app posted when
*another* session posted it — and still skip its own (§7 rule 3). Every lmer
session posts into Slack through one app and into matrix as one bot account, so
the bot's own id cannot tell those apart. The question is answered from a
**poster** stamped by the host that made the post, and never from the
`[lmer session …]` prefix in the text, which stays in the body and is read by
nobody.

| origin | who posted | the label |
|---|---|---|
| `session` | a run's `lmer-signal post --thread`, through its thread servicer (§12) | the run's fleet-view title, else its slug, else absent |
| `platform` | the daemon's own `POST /api/comms/thread` — `lmer-ctl thread post`, the UI's post button | from the caller's credential: `uber lmer`, `operator`, a connector's channel |

**matrix carries the stamp on the event**, under the content key
`net.20c.lmer.session` (`{origin, session?, label?}`). Ordinary event content:
`/sync` and `/event` hand it back verbatim, so it survives a daemon restart, a
reconnect, and a connector running beside a session on another host.

**Slack carries it in a host-side registry**, `(channel, message_ts) →
(origin, session, label)`, one JSON file per channel under the lmer state dir,
bounded per channel and pruned by age *when that channel is next written* — a
channel nobody posts into again keeps its file, and its lock file, for good.
Same host **and same POSIX user**, since the path is `~/.lmer/thread-posts`:
that is where every poster and every connector already is (the daemon runs its
own servicers and connectors, and a shell `lmer chat` runs both in one
process), but a daemon and a shell session under two accounts on one host do
not share it, and what they lose is the ingest this section describes.

**Not keyed by the thread**, deliberately. A poster resolves a permalink to the
thread root Slack reports for it, and a shell session's connector keys itself
by the link as written — so for a link naming a message *inside* a thread the
two hold different `thread_ts` for the same conversation. They agree on the
channel and on the message id, and a message id is unique within a channel.

**Slack message metadata is deliberately not used**, and this note is here to
stop it being added back without the work it needs. Slack keeps metadata only
for an event type registered in the app's manifest — an unregistered one is
dropped and the message posts without it — and returns it only to an app
holding the `message_metadata:read` scope, with `include_all_metadata=true` on
the read. Neither is in the scope set lmer asks for
([docs/LMER-CLI.md](./LMER-CLI.md)), so that path stays silently inert until
every operator edits their Slack app and reinstalls it. Add the scope and the
manifest entry first, or leave this as it is.

An ingested post becomes an ordinary inbound record: `source.channel` is the
connector's own channel, `source.ref` the channel's message id — so §7 rule 4
idempotency is unchanged — and `source.author` is one of exactly four forms:
`lmer <run name>`, `lmer session <id>` when the run has no name yet,
`platform · <credential>`, or bare `platform`. The run *name* is the run's title
or slug and not the `[label]` the thread shows, which already begins with
"lmer" — only one of the two readers may own that word. A session-origin stamp
that carries no session id is **not** a poster at all: `is_session` could never
match it, so the run that made the post would read it back as another run's and
could answer its own question with it. The agent therefore reads
`[slack · lmer chat-mr-288]` or `[slack · platform · operator]`, which is not a
human and does not look like one. Because `source.channel` is the connector's
own, §7 rule 2 keeps the record from going straight back into the thread. And
because it is an ordinary inbound record, **§6's question binding applies to it
exactly as to a human message**: another run's reply can answer this run's open
question.

**A connector stamps its own mirror too**, with its own session, so live
readers can suppress their own echo and attribute another run's posts.
Attach notices count both sides without rendering either side's authors;
on-demand reads retain their existing display-name attribution.

What a connector skips, then, is what its own session posted — its mirror
included — and, as the safe default, a message by this app that nothing on the
host can attribute: the listener's own ack, or a post whose registry entry never
landed. A message from a *different* Slack app is not a candidate at all: an
`xoxb` post always carries the bot's own user id, so a message without it is not
this app's to attribute and never occupies a hold slot.

Slack's registry entry is written just *after* Slack accepts the post, so an
inbound poll can read the message before the entry lands; such a message is held
for 30 seconds and asked about again on every poll rather than dropped on first
sight. A hold that does expire is logged, at most once every 10 minutes —
because the alternative is that this failure looks exactly like the one it
replaced (nothing, anywhere), and a single line per connector would be spent on
the first benign expiry and silent about a stamp that broke an hour later.

### Connecting and disconnecting a thread while a run is up

A thread is not only a launch argument. The platform keeps **connections** —
one session, one thread — as state of its own, runs a connector **process** per
connection, and reconciles the two, so a running session can gain a thread and
lose one without restarting anything.

| route | what |
|---|---|
| `GET /api/sessions/{id}/threads` | this session's connections, each with `running`, `since`, `last_exit`, `detail` and `attention` |
| `POST /api/sessions/{id}/threads {permalink}` | connect; the channel is read out of the link |
| `POST /api/sessions/{id}/threads {channel, title?}` | open a new thread there and connect that (below) |
| `DELETE /api/sessions/{id}/threads/{thread_id}` | disconnect |
| `POST /api/sessions/{id}/threads/{thread_id}/recover` | inspect or recover the host-side pending send |

```sh
lmer-ctl threads list SESSION
lmer-ctl threads connect SESSION THREAD_PERMALINK
lmer-ctl threads open SESSION CHANNEL [--title "…"]
lmer-ctl threads disconnect SESSION THREAD_ID
lmer-ctl threads recover SESSION THREAD_ID inspect
```

The daemon automatically settles saved receipts and permits one safe recovery
retry. Unknown send outcomes remain parked, with one assistant digest per
recovery transition and a warning on the thread controls. `receipt`, `retry` and
acknowledged `clear` are also available through `threads recover`; see
[Connector recovery](CONNECTOR-RECOVERY.md#platform-recovery) for their arguments,
duplicate-risk override and API contract.

In the UI the same calls are two icons — Slack and matrix — on every run row, in
the run view, and in the uber lmer chat pane header. An inactive icon opens a
one-field prompt that takes **either** a thread permalink or a channel to open a
new thread in; an active one is coloured, carries a count when the run holds more
than one thread on that transport, names the threads on hover, and offers a link
and a disconnect for each. Which of the two was typed is decided by whether it is
a URL, and a channel offers a second field for the root message's first line.

A connection is refused, with nothing written or started, when:

- the permalink is not a Slack or matrix thread link, carries embedded
  credentials, or is over the length bound;
- the transport's credentials are missing from the **daemon's** environment —
  the refusal names `SLACK_BOT_TOKEN`, or the `LMER_MATRIX_*` variables;
- the matrix room is end-to-end encrypted (named), which the connector refuses
  too;
- this run already holds that thread;
- **Slack says the conversation is not this token's**: the resolution's own
  `conversations.replies` answers `no_permission` or `access_denied` — what that
  method documents for "make sure your app is a member of the conversation" —
  or `not_in_channel` / `channel_not_found` / `is_archived` /
  `thread_not_found`. Both vocabularies are named because Slack's error tables
  for a read and for a post overlap only in part and the code that never fires
  costs nothing, while the one left out costs a run mirrored into a stranger's
  channel. Three things are **not** that refusal: a transport that could not be
  *asked* (an exhausted 429, a network failure — the link's own reading stands,
  since a directory lookup must not decide a thread's identity); any other code
  Slack answers with; and `missing_scope`, which says this token may not *read*
  a thread it may well be a member of — refusing on it would leave a
  freshly-opened thread's root message in a channel with no record to disconnect.
  What it becomes instead is a connector that **runs, retries and logs**: the
  read failure reaches the connector's own log (`<platform state>/logs/connector-slack-<digest>.log`) and nothing else, so the
  connection reads as `running` with no `detail` and the thread menu shows it as
  connected (the state `connectors._spawn_process`'s docstring already names as
  one the runtime block cannot reach — propagating it is a follow-up, not this
  behaviour's excuse). A *launch* target is
  recorded whatever the answer: a spawn's threads are recorded, not decided.
- **another live run holds it** — a thread maps to one live session, the rule
  the listeners already enforce, because two runs answering into one thread
  makes a human's reply ambiguous and neither the binding rule nor
  first-answer-wins spans sessions. The refusal names the run holding it. A
  thread whose holder is no longer live is free.
- the session is not live or has no control plane.

#### Opening a thread rather than pasting one

A thread need not exist first. `POST …/threads {channel, title?}`,
`lmer-ctl threads open`, the UI's one field, and the run's own
`lmer-signal thread open <channel> ["<text>"]` all do one thing: post a root
message in that channel, then connect the run to the thread that now exists,
exactly as a pasted permalink is connected. The reply carries the same record
`GET …/threads` shows, and its `permalink` names the new thread.

`channel` is a Slack channel id or `#name`, or a matrix room id or alias. One
field for two transports, told apart by the `:server` half a matrix room carries
and a Slack channel does not.

**The root message** is three lines, for a person who finds the thread in their
channel and has to work out what it is: the run's fleet-view title, its
`host/project/slug` key, and the control-UI link. An explicit `title` — or the
text a run passes to `lmer-signal thread open` — replaces the **first** line and
nothing below it, which is what keeps the thread traceable to a run. The key
line is left out for a session no run owns, and the link for a daemon nobody
else can dial: it is `matrix.control_url` when the operator has set one (the key
the fleet bridge already asks for, and for its reason — what the daemon knows is
a bind pair) and the bind URL when that address is routable, and otherwise
nothing at all, because `http://127.0.0.1:8600` in somebody's channel is not a
link. The connector's own attach ack follows in the thread, as it does for every
other attachment.

**Only channels the bot is already in**, and the two transports enforce that
differently — which is worth stating plainly, because on one of them lmer is not
the one enforcing it.

- **matrix**: lmer asks. A room the bot has not joined is refused by name
  *before* anything is posted, and so is an encrypted one — membership is asked
  first of the two, because the encryption probe is a room-state read and a
  homeserver answers a non-member `403` for it.
- **Slack**: *Slack* answers. `chat.postMessage` returning `not_in_channel` (or
  `channel_not_found`) **is** the membership check, and the refusal carries that
  code with the invite hint. That holds for the documented scope set
  (docs/LMER-CLI.md) and depends on it: **`chat:write.public` grants posting into
  every public channel in the workspace**, so with that scope the error never
  fires and the first thing an uninvited channel would see is the run's own root
  message. An app carrying it is therefore **refused by name** — the scopes come
  from the `auth.test` the open already makes, in the `x-oauth-scopes` header —
  and the operator is told to remove the scope. A header that was not there to
  read is *not knowing* rather than evidence of the scope, and stays permitted:
  refusing on a stripped header would break every install behind a proxy that
  drops it. Checking membership directly would need `conversations.info` and
  therefore `channels:read`, which the documented set does not include (the same
  trade `_slack_label` makes for a channel's name); a probe on that scope, for
  operators who grant it, is a follow-up.

lmer joins nothing and invites nobody either way.

**The order the failures decide.** Everything refusable without asking Slack or
the homeserver anything is refused first, because a root message cannot be
unposted: no control plane, no room for another thread on this run, and — on
uber lmer — no room in its standing `threads` list, which is a **second,
independent bound** (a session's connections and the launch list diverge, so a
run with no connections can still have sixteen stored links). The 409s above are
otherwise unchanged and apply to the connect half; if the connect fails after
the root has landed, the refusal says so and names the thread, which is then a
thread with a root and no connector.

The connection is an ordinary live one: `origin: live`, so the thread is mirrored
from the run's current position rather than from its whole journal, and the
on-attach notice for a thread just opened reports its root as one message.

**The root carries the same stamp every host-side post carries** (*Posts from
other runs, and from the platform*, below): the run's own when the run opened
the thread, and the platform's — the caller's credential — when the operator
did, from the route, `lmer-ctl` or the UI prompt. Which matters in both
directions. A root nothing attributes is a bot-authored message no connector can
place, so every connector on the thread holds it and then drops it as an own
echo, the attaching run's included. And the two origins are read differently on
purpose: a run that opened its own thread does not need its own root read back
to it, while an operator who opened one *for* a run has addressed that run, and
the root reaching it as `[slack · platform · operator]` is the message, not
noise.

**One live session per thread, at every door.** The rule above is not only the
live-connect route's. A spawn whose launch `targets` name a thread another live
run holds — `POST /api/sessions`, `lmer-ctl spawn --target-thread` — is refused
**409** naming that run, before a command line is built, a control port drawn or
a directory made: no session starts and the thread is posted nothing. The same
409 answers an **explicit** `threads` override on `POST /api/assistant/start` or
`/rotate`, and `lmer-ctl` relays status and text to the agent that asked. What
does *not* refuse is uber lmer's **standing** `threads` list: a worker can take
one of those threads during the seconds a rotation is between incarnations, and
an orchestrator that will not come back up over one thread is worse than one
that comes back up without it — so a held standing thread is recorded as a
connection whose connector does not start, showing on `GET /api/assistant` and
in the fleet payload as `running: false` with `detail` naming the holder, and
attaching by itself once the thread is free. Writing the standing list
(`POST /api/assistant/config`) is not an attach and is never refused for this.

Behind the refusals, a connector is never taken off a session that is still
using it: the reconcile asks a connector holding a thread to stop only when it
is **not one this daemon is itself running for a live session** — an orphan of a
previous daemon, or a connector whose session has ended. Two live registrations
on one thread used to trade the connector back and forth, each reconcile
stopping the other's and starting its own, which a human saw as "lmer has
stopped listening" / "lmer is listening" several times a minute.

**Rotating uber lmer.** A held-thread refusal before the incumbent is stopped is recorded as
`assistant_rotate_refused`, with its reason and detail; ordinary start refusals
remain `assistant_start_refused`.

 A rotation is a stop and a start, and it settles both of
its refusals *before* the stop, while there is still an assistant to keep: an
unusable launch override, and a thread another live run holds. The two are
settled differently, and the difference is the whole of it.

An unusable **override** cannot be decided twice: the start that follows
validates the same object, so a second look could only agree. The **standing**
layers are not frozen between the two reads — a concurrent settings write
serializes on the config's own lock, not the assistant's — and need not be,
because an unusable standing value warns and resolves as unset rather than
refusing.

**Who holds a thread** is the one input that genuinely changes underneath. A
spawn or a live connect can take one in the moment between the stop and the
respawn, and neither waits on the assistant's lock. So the question is asked
once, before the stop, with the incumbent's own claim excluded — otherwise a
rotation naming the threads it already holds would refuse itself — and the start
is told not to ask again. Were it asked again, a thread taken in that moment
would answer "no assistant at all", which is exactly what the standing-list
deferral exists to prevent; instead the thread comes back as a deferred
connection, and attaches when it is free.

**Where the connectors run.** For a session the platform spawned — uber lmer
included — the **daemon** runs them, one process per connection, with the
session's control-plane token and the host credentials in the child's
environment and never in its argv. The launch-time `targets` of such a session
become connections like any other, so there is one mechanism on the platform
side; the spawn passes `--no-thread-connectors` to `lmer`, which then sets the
container's chat context and leaves the connectors, the thread claim and the
upload store to the daemon. A session started from the command line is
unchanged: its own `lmer` process hosts its connectors, as before.

**What a connection sees when it attaches**, in order:

1. **Where it already got to.** The supervisor holds every subscriber's position
   and a connector's subscriber name *is* its connection's id, so a connector
   that died resumes exactly where it stopped. A clean stop acks `final`, which
   removes that name — so the daemon also keeps the position on the connection's
   own record (`cursor` in `threads.json`), which is what survives a **graceful**
   restart, where every connector is asked to stop politely and each one erases
   the supervisor's answer on its way out.
2. **Otherwise, where its kind starts.** A connection that exists for the
   session's whole life — a launch target, or a CLI `lmer chat` — reads from the
   beginning, so its thread has the whole conversation. One made *while* the
   session is running reads from the current position: mirroring hours of a run's
   journal into a thread, one message per record, is not what connecting it
   means.

Either way the session receives the one-line backlog notice (§9). It can read
its own connected thread on demand with `lmer-signal read`; reading posts
nothing, connects nothing, and does not move the connector's cursor. Standalone
and platform-run connectors use the same attach path.

Connections live in `<platform state>/comms/threads.json` (0600). A daemon
restart reattaches every recorded connection whose session is still live —
SIGTERM-ing first any connector a previous incarnation left running for that
thread, which it identifies by an `owner` marker on the thread-registry entry
and confirms by reading the process's own argv, so a recycled pid is never
signalled; a
session that ends keeps its records and loses its connectors; forgetting a run
drops its records.

**A connection is keyed by the thread its transport says it is**, not by the link
that named it: a matrix permalink naming a room **alias** is recorded under the
room id the homeserver resolves, and a Slack permalink naming a message *inside*
a thread is recorded under the root `conversations.replies` reports for it. Both
spellings otherwise produce a second connection to the same thread — two
connectors, and every reply in it twice. A transport that cannot be asked leaves
the link's own reading standing, because a directory lookup must not be what
makes a connect fail.

The launch-time held-thread check requires a verified identity: Slack lookup
failures or empty answers, and matrix aliases that cannot resolve to a room id,
refuse the launch instead of accepting the link's spelling as a different
thread. Restore transport access and retry, or launch without that thread.
Literal matrix room ids need no directory lookup. Adoption of an already
started session retains best-effort resolution.

**What the thread sees.** Exactly one announcement per attachment, and one line
when it ends:

- a listener that spawned the session announces it at once, naming the preset
  the session got and how long the first reply may take, and tells its child not
  to announce again (`LMER_CHAT_ATTACH_ACK=0`);
- otherwise the connector posts a one-line ack when it attaches — a live
  connect, a launch-time target, a shell `lmer chat <permalink>`. Slack and matrix include
  the session's resolved launch preset and model from its supervisor's
  `comms.launch` health metadata. An unset preset is shown as `none`, an unset
  model as `harness default`; older supervisors without metadata retain the
  original notice. Slack escapes markup in these labels; matrix sends plain text;
- and the connector posts a one-line note whenever it stops: a disconnect, a
  session that ended, a daemon shutting down. A daemon restart therefore posts a
  note and then an ack in every thread it holds that no listener spawned, which
  is honest — those connectors really do stop listening for those seconds.

Both lines pass `strip_run_references` like every other post.

The affected session also receives a platform-origin bus message,
`thread disconnected: <label>`, when a connection is removed or another holder
prevents it from attaching. A held connection is reported once until it attaches
successfully; reconcile retries do not repeat the notice. A later loss after
reconnection produces a new bus record. A routine daemon
shutdown keeps the connection records and sends no session disconnect notice.
A bus failure is logged and does not undo a completed disconnect.

**Uber lmer.** The routes work on its session id like any other, and a write
there also edits the stored `threads` setting, so a thread connected from the
chat pane header survives a rotation and a disconnect there stops it coming
back.


---

## 10. The ask directory is a projection

For one release the supervisor keeps writing the ask channel directory
(`LMER_ASK_DIR`, when mounted) as a **projection** of the bus:
`question`/`note`/`milestone` records become `NNNNNN.question|note|signal.json`
with `NNNNNN` = the bus `seq`, an accepted `answer` becomes
`NNNNNN.answer.json` plus its `.read.json` receipt (delivery is guaranteed),
a `close` becomes `.closed.json`.

It reads the directory as well, because during the window it is not the only
writer. Anything in it that does not carry `writer: bus` is **lifted onto the
bus** with `ref: ask-dir:<file>`, which is also what stops it being lifted twice
— a repeated `(channel, ref)` is a duplicate, and the log is reloaded on
restart, so a second supervisor over the same mount lifts nothing again. That
prefix is reserved: `POST /messages` refuses a caller-supplied `source.ref`
beginning with it, because a posted one would make the projection skip a file
for good. An `.answer.json`
goes on as a `platform` answer (an older daemon answered by file), carrying its
`option` (§6) when it names one; a
`.question`, `.note`, `.signal` or `.closed` goes on as the **session's own**,
because the only thing that files those is the session's own CLI.

| session image | in-container CLI | daemon | what happens |
|---|---|---|---|
| current | current | current | bus everywhere; the projection is written and read by nothing current |
| current | current | older | the daemon reads the projection and answers by file; the supervisor lifts the answer onto the bus |
| current | **older** | current | the CLI asks, notes and signals through the directory only. The supervisor lifts each record onto the bus, so the daemon reads one ordinary bus session — one `session_signal` per milestone, the question in the chat pane. `POST …/ask/{qid}/answer` asks the bus which record *is* the entry the operator answered — the one lifted from that file, or the one the projection mirrored into it — and answers by file when there is none, so it never binds a reply to a record that merely carries the same number, and never refuses an answer `GET …/ask` had just offered. An `--option N` answered by file is resolved before the write, because that file's reader is the session's own pre-bus CLI and it has no resolver. Every self-dev session on a checkout older than the unified channel is this row, because `/workspace/src` shadows the installed CLIs |
| older | older | current | `/healthz` has no `comms`; the daemon's retained ask-directory readers serve it, and `POST …/comms` types the record in through `/input` rather than refusing it — except an `answer` naming an `option`, which has no options to resolve against here and is refused with a `409` naming the ask route. During a rollout every already-running session is this row |
| current, one release old | either | current | the bus exists but has no `option` field, so an index-only answer is refused as `text is empty` — the field the caller deliberately omitted. Nothing explains it: `/healthz`'s `comms` key tells a bus from no bus and there is **no other version negotiation**, so the platform cannot know whether a bus understands `option`, and a note it guessed at would be wrong for every other 400 on the same request. Send the option's own text; the window closes when the image rolls |

A question lifted out of the directory keeps the stem it was filed under: its
asker is polling that file, so every answer to it — lifted, or posted on the bus
by a connector — is mirrored back under that stem rather than under the bus
`seq` the lifted question was given. Nothing about it is typed into the harness,
for the same reason: the file poll is the delivery, and a PTY leg would be a
second copy.

An entry id is **not** a `seq`. The directory's next id is one past its highest
file; the bus counts records, including inbound ones and lifts that file
nothing. Neither leads — a session using only the bus sits at `seq` 3 with 4 as
the next id, and one record behind a lift sits at `seq` 2 with 2 — so nothing
converts one into the other, in either direction.

**Which writer filed a record is stamped, never inferred.** Every file the
projection writes carries `writer: bus` — a key every reader in
`ask_channel.protocol` ignores, so it costs an older CLI nothing — and every
record it lifts keeps the file's name in `source.ref`. That prefix is checked on
the ref the log will store, not the one the request carried, because the two
differ by whitespace. Those two marks are what the readers ask: the
answer route asks which bus question *is* the entry the operator answered, and
the mark the daemon's two milestone readers share counts *directory entries*, so
a lifted milestone is marked under its entry id. A mark kept in the other
counter is wrong in both directions — too high drops the next old-path
milestone on daemon recovery, too low re-spools one already delivered.

**Deploy order: the session image leads, the platform follows.** A host `lmer`
of this release hands the container the control token's *digest* rather than the
token, which a supervisor from before the unified channel does not understand.
Roll in this order — session image, platform image, UI bundle, daemon, host CLI
and listeners — and every window is covered by a row above. The reverse order
leaves new sessions with a control plane nobody can authenticate to.

Removed the release after: the projection, the shims, the daemon's
ask-directory readers and the steering form of `POST /api/sessions/{id}/input`.

---

## 11. Environment variables

| variable | where | meaning |
|---|---|---|
| `LMER_FASTAPI_PORT`, `LMER_FASTAPI_TOKEN` | container (exported by the supervisor) | what `lmer-signal` uses to reach the bus. The token is the *session's* own, minted per session: it speaks as `agent` and nothing else |
| `LMER_FASTAPI_TOKEN_SHA256` | container, set by `lmer` | the control token's digest, so the supervisor can verify the credential the daemon and the connectors use without holding it |
| `LMER_SUPERVISOR_URL` | a standalone connector | the supervisor address, instead of the loopback port |
| `LMER_COMMS_CONNECT_BUDGET` | host / a connector | seconds a connector waits for a supervisor that is not up yet (default 900). The host CLI starts a connector before it launches the container, and a cold image can take minutes |
| `LMER_COMMS_LOG` | container, set by the supervisor | path of `messages.jsonl`; absent when nothing is mounted |
| `LMER_COMMS_DRAIN_GRACE` | container, seconds (default 5, max 60) | how long the control plane stays up after the harness exits for subscribers to ack the last seq |
| `LMER_COMMS_TERMINAL_MIRROR` | container, bool (default on) | the terminal line tracker |
| `LMER_CHAT_CHANNEL` | container, set by a chat-thread target handler | `slack` or `matrix`; arms the reply guard and the chat taskdef |
| `SLACK_BOT_TOKEN` | host (the `lmer` process, the listener) | the Slack bot; required for a Slack thread target, never forwarded into the container |
| `LMER_SLACK_POLL_SECONDS` | host | the Slack connector's `conversations.replies` interval (default 3) |
| `LMER_MATRIX_HOMESERVER`, `LMER_MATRIX_USER_ID`, `LMER_MATRIX_ACCESS_TOKEN` | host | the bot account the matrix connector and listener act as; required for a matrix thread target, never forwarded |
| `LMER_MATRIX_AUTHENTICATED_MEDIA` | host, bool | the operator's assertion that the homeserver requires authentication for media; the agent's attachments are withheld until set |
| `LMER_MATRIX_ALLOWED_USERS` | host | comma-separated MXIDs allowed to mention the bot and receive `lmer-signal dm` messages; exact match, unset or empty means nobody |
| `LMER_SLACK_DM_ALLOWED_USERS` | host | comma-separated Slack user IDs or handles allowed to DM the listener and receive `lmer-signal dm` messages; entries matching `^[UW][A-Z0-9]{8,}$` are exact, case-sensitive IDs, while all others are case-insensitive, confirmed handles with a leading `@` ignored; unset or empty means nobody |
| `LMER_MATRIX_ROOMS` | host, listener | optional room ids to watch; default every joined room |
| `LMER_ASSISTANT_MENTION_ROUTING` | host, platform and listeners | default false; route mentions and replies beneath assistant DM parents to UL; see [assistant mention routing](ASSISTANT-MENTIONS.md) |
| `LMER_ASSISTANT_MENTION_BACKLOG` | host, platform and listeners | default 10, integer 0–100; messages preceding a top-level mention |
| `LMER_MATRIX_ROOM_ID`, `LMER_MATRIX_THREAD_EVENT`, `LMER_MATRIX_PERMALINK` | container, set by the target handler | the thread this session serves — context, not a credential |
| `LMER_CHAT_ATTACH_ACK` | host / a connector, bool (default on) | whether a connector announces its own attachment in the thread. Both listeners set it to `0` on the `lmer` they spawn, because they announce that thread themselves. It gates the **ack** only — the note a connector posts when it stops listening is unconditional |
| `LMER_CHAT_ATTACH_FROM_NOW` | a connector, bool (default off) | whether a **fresh** attachment starts reading the bus at its current position rather than at the beginning. The daemon sets it for a connection made while the session is already running; without it such a connector posts the run's whole journal into the thread. A connector resuming after its own death continues from its acked position either way |
| `LMER_CHAT_ATTACH_SINCE` | a connector, int | where this attachment had read up to, according to whoever remembers it. Outranked by the supervisor's own record of the subscriber's position, which is fresher — but a clean stop acks `final` and **removes** that name, so this is what a graceful restart has left to go on |


## Recovery details

Only the control credential may contribute raw `/input` bytes to the terminal
mirror. Session-credential writes still reach the PTY but never enter the
terminal tracker, including partial lines. Agent messages use `/messages`.

For options, exact text (ignoring case and surrounding whitespace) takes
precedence over an ordinal: with options `3`, `5`, `10`, a reply of `3` chooses
`3`; `3.` chooses the third option, `10`. Matrix resolves an explicit reply
against the question event it names before considering the oldest open
question. A thread's automatic fallback relation is not an explicit reply.
Both connectors mark a human message consumed only after the bus accepts it;
Matrix advances its sync token at the same boundary. Startup replay also
includes the initial sync timeline, so replies arriving after the history
snapshot are accepted before its sync token advances.

The fleet journal retains the active file and `journal.jsonl.1`. Disk reads
and restart recovery traverse both in order. Before rotation the daemon
atomically saves `journal.jsonl.checkpoint.json`, holding the cursor and question
fold so even a question older than both generations survives. Message-id dedupe
covers retained records. `since=0` reads the oldest retained history; rotation
is bounded retention, not a permanent archive. Never reset a connector cursor
merely because the daemon restarted.

`lmer-ctl send` accepts path-leading prose such as `/tmp/build.log has the
traceback`. A lone command-shaped token such as `/followup` is refused with
instructions to use `lmer-ctl command` to execute it or a sentence with `send`
to discuss it.

A control-plane 401 retains the upstream error and suggests checking the
credential and session-image version. An image predating digest credentials
is one possible cause, not a diagnosis from status alone. Roll the session
image first, then the platform and host CLI, and start a new session.


---

## 12. Reading and posting threads on demand

A run can **read** and **post** into any accessible thread on demand, including
its own connected threads. Reading starts no connector, posts no ack or message,
and changes no connection ownership or connector cursor. Reading an unconnected
thread does not subscribe the run to later replies; reading a connected one
leaves its existing subscription in place. A thread another run holds can also
be read and posted into.

```
lmer-signal read <permalink> [--limit N] [--json] [--timeout S]
lmer-signal post --thread <permalink> "<text>" [--attach PATH]
```

A post carries at most 3500 characters into the thread — what the connectors
already hold themselves to — and a longer one is sent clipped and **says so**,
while the record on the session's own bus keeps the whole text.

`read` keeps its existing default of 20 messages and maximum of 200; larger
requests are rejected. A notice always reports the full count, but its command
uses at most `--limit 200` and says "Read up to 200" when the count exceeds that.
Pagination is unchanged: Slack fetches all reply pages before taking the last
N; matrix stops its forward relation fetch at N before sorting the fetched
events. A bounded matrix read therefore does not guarantee the newest N
messages of a longer thread. It prints
messages as bus-style records — the same
`[slack · vegu] …` tag the harness sees for an inbound message — and copies any
files into the session's upload store, naming them with the `[lmer upload]`
reference (#246). `post --thread` posts once, and records the post on the
session's own bus as a `message` carrying `meta.thread`, so the run's connected
threads show where it went.

### Direct messages

A run can send one **private** message to a person, on whichever transport this
session is connected to:

```
lmer-signal dm <recipient> "<text>" [--message-file PATH | --stdin] [--timeout S]
```

`<recipient>` is a Slack user id or username, or a matrix user id — told apart
by the `:server` half a matrix id carries and a Slack one does not, the same
reading that separates a room from a channel above. It opens the 1:1
conversation, or reuses the one that already exists, posts once, and prints the
conversation's permalink on stdout.

It is **one-shot**. The session is not connected to that conversation, so a
reply the person types goes nowhere and they hear nothing back. Connect it
deliberately with `lmer-signal thread connect <permalink>` if a conversation is
wanted.

**The body is never recorded on the bus.** What the run's own conversation gets
is one note — who it went to, on which transport, and how many characters —
never the text. That is by construction rather than by a flag: `post --thread`
writes its text to the bus with `meta.thread`, and every connector renders that
into every connected thread as `↗️ posted into …` followed by the body, which is
exactly the mirror a DM must not take. The cost is that **the body is not
recoverable from the run's journal**; the recipient has it, and so does the
host's transport.

**Who may exchange DMs is the operator's decision**, held in one host-side list
per transport: `LMER_SLACK_DM_ALLOWED_USERS` for Slack ids or handles, and
`LMER_MATRIX_ALLOWED_USERS` for matrix MXIDs. Both govern inbound messages to
the listener and outbound `lmer-signal dm` messages. **Unset or empty means
nobody**: every new outbound DM is refused on stderr naming the relevant
variable, with no fallback to a public thread or prompt. With no established
conversations, an empty list is refused before any transport is called. Slack
ID entries match the confirmed ID exactly; handle entries match only a
confirmed handle, case-insensitively with a leading `@` dropped. Matrix entries match exactly,
because two MXIDs differing only in case are two people. Only what the transport
confirms about an account is compared — its user id and the handle Slack holds
for it — never a **display name**, which is self-set and not unique, and which
any member could otherwise set to match an entry naming somebody else. The
listener gets an inbound sender's handle from Slack by ID when a handle entry
needs comparing; that lookup requires `users:read`.

A conversation this session is **already connected to** needs no entry: a DM the
human opened with the run is an ordinary connected thread, and they chose that
channel. Only connections somebody *else* made count — a launch target, an
operator connect, the platform — never one the run made itself with
`lmer-signal thread connect`. Each connection records who made it, and the
filter is load-bearing: `thread open` accepts any Slack channel string, a bare
user id included, and Slack resolves that to the user's IM, so without it a run
could establish its own exemption. Where the host side keeps no connections —
the shell `lmer` process, whose threads are launch arguments — it knows of none,
and the list is then the only door.

**Resolving the recipient.** A Slack user id needs no scope. A username needs
`users:read`, so the scopes `auth.test` already reported decide whether to look
at all: known and missing is refused by name before any lookup, and scopes that
could not be read (a proxy that strips the header) are *not knowing* rather than
absence, so the lookup runs and Slack's own `missing_scope` answers. Granting
`users:read` is what would let a run name people by handle; without it, name
them by user id, and list ids rather than usernames in the allow list, since a
username entry can then never match. A name two people answer to is **refused
naming both** — the one case where a guess delivers a private message to a
stranger and nothing can take it back.

**Refusals**, all of them before anything is sent: no allow list, not on the
allow list, a username without the scope, a username matching none or several,
an encrypted matrix DM room (the message would arrive unreadable), and the
existing 503 when no host side is attached. Transport errors keep their own
names — `cannot_dm_bot`, `user_not_found`, `im_disabled`. An **empty** list is
refused before any call; a non-empty one has to resolve the recipient first,
since comparing against it needs the id, but the mutating `conversations.open`
is not reached for somebody no exemption could rescue.

The Slack app needs **`im:write`** for any of this to work, and `users:read`
only to name people by handle; both are in the setup scope list in
docs/LMER-CLI.md and both need a re-install to take effect.

**A timed-out `dm` is indeterminate, not failed.** Its request is already
claimed, so the host may finish after the CLI stops waiting; the message says so
and says not to retry, because a second attempt sends a second copy. Unlike
`thread open` there is no way to look and find out — ask the operator.

### Who actually does it

The container holds no Slack or matrix credential and neither does the
supervisor, so the work is an RPC to the host:

```
session      lmer-signal read <permalink>
supervisor   POST /thread/requests → queued, and claimed by one servicer, once
servicer     conversations.replies / the room's thread relations, with the host's token
supervisor   POST /thread/requests/<id>/result
session      the records on stdout
```

The **servicer** is the host side of that exchange, and it is where the run's
attribution comes from: a post is prefixed with a label composed on the host,
never taken from what the session sent — the same rule that makes the bus stamp
`source.channel` rather than read it out of the body. The platform daemon runs
one per live session, reconciled like its comms subscribers, and labels a post
with the run's fleet-view title; the host `lmer` process runs one for any
session it started with a control plane — `lmer --fastapi`, which a chat-thread
target turns on anyway — and under `--no-thread-connectors` leaves it to whoever
owns the rest of that session's host side. The daemon passes that flag on **every**
session it spawns, which is what keeps the two from both servicing one session: a
run whose posts reached a thread under either of two names, depending on which
servicer claimed the request, is the failure the flag's name already describes. It is a thread of the process
and not a child of it, unlike a connector: a connector is a long-lived mirror of
one thread and has to survive a daemon restart mid-conversation, while a servicer
holds nothing between requests.

The same queue carries the three verbs for a run's **own** threads —
`lmer-signal thread open | connect | disconnect`, §9 — for the same reason: the
credentials are on the host, and so are the connections. `open` is the one of
those that names a `channel` instead of a `permalink`. A servicer whose host
side keeps no connections (the shell `lmer` process, whose threads are launch
arguments) refuses those three by name and serves `read` and `post` as before.

`dm` is a sixth op and names neither: a `recipient` addresses it. A
connectionless host side **serves** it rather than refusing it — it simply sees
no established conversations, so the recipient's transport list is the only door.

The queue is **not** the message bus: a request to go and read somebody else's
thread is not part of this session's conversation, so it is neither journaled
nor replayed. What the run *says* still is.

| route | caller | what |
|---|---|---|
| `POST /thread/requests {op, permalink, channel?, recipient?, limit?, text?, attachments?}` | session or control | queue one request; **503** when no servicer has polled in the last 90 s |
| `GET /thread/requests/{id}?timeout=<0..30>` | session or control | the request, long-polling for its result; a collected result is dropped, and a **410** says so |
| `GET /thread/requests?since=&timeout=<0..30>` | **control only** | the servicer's poll — a request handed out here is *claimed* and never offered again, because a post delivered twice is two messages in a stranger's thread and nothing downstream could tell them apart |
| `POST /thread/requests/{id}/result {result}` | **control only** | the answer, handed back to the asker unread |

A servicer counts as attached while it has polled recently **or** while it holds
a request it has not answered: it works a claimed batch serially, so between two
polls it can spend minutes on one thread's files, and reporting "nothing here can
reach Slack" while it works is the one lie this fact exists to prevent.

`/healthz` gains `"threads": {"servicer": <bool>, "in_flight": <n>, "seq": <n>}`,
which is what makes "nothing here can do this" an answer rather than a timeout:
`lmer-signal` exits **3** naming it, exactly as it does for a session with no
control plane at all. At most 8 requests may be in flight per session; a
servicer that dies holding one lets the asker's `--timeout` end the wait, because
nothing retries a request that was already claimed.

A file in a thread is **described** on the record either way, and copied into the
session's upload store when there is one and the read still has room: each file is
bounded (25 MiB) and so is a whole read (100 MiB), counted as the bytes arrive
rather than after they are resident — the files are posted by whoever is in the
thread, and on the platform route the process fetching them serves the fleet. A
description that could not become a copy says which of those reasons it was.

`meta.thread` on a record is the one field the pane turns into a link, so the
daemon drops it unless it parses as a thread permalink — the marker still renders
without one. Everything else a record carries is rendered as text.

An attachment the session sends is reduced to the four fields the host side reads
— name, path, media type, size — each bounded and checked. An attachment is named
by its path inside the container, and the host resolves it
as `<the session's upload store>/<one name>` or not at all — the only join of
session-supplied text onto a host path in the feature. The bytes are then read
through a descriptor opened `O_NOFOLLOW`, because that store is bind-mounted
`rw` into the container: a name that was a file when it was checked can be a
symlink to the host's own state by the time it is opened.

### The platform's own two verbs

```
GET  /api/comms/thread?permalink=<link>&limit=<n>
POST /api/comms/thread {permalink, text}
```

```sh
lmer-ctl thread read <permalink> [--limit N]
lmer-ctl thread post <permalink> "<text>"
```

The same work, in the daemon, with the daemon's credentials — for the UI and for
uber lmer. No session and no connection is involved, so a file in the thread is
described rather than copied anywhere, and the prefix comes from the **caller's
credential** the way `source.channel` does: `uber lmer` for the assistant's,
`operator` for the shared secret, a connector's channel for a connector's.

### Refusals

- A permalink that is not a Slack or matrix thread link, carries embedded
  credentials, or is over the length bound.
- Credentials missing from the **host's** environment — the refusal names
  `SLACK_BOT_TOKEN`, or the `LMER_MATRIX_*` variables, and says whose
  environment it means.
- An end-to-end encrypted matrix room, by name, as the connector refuses one.
- A channel the host's bot token cannot see: the Slack error code with what to
  do about it (`channel_not_found` → invite the bot).
- No servicer attached (above).

### Reference threads

A run is often *pointed at* a discussion rather than run from it. The run's
thread menu in the UI carries a third entry, **Add a thread as reference**: a
permalink and a title. It connects nothing — no connector, no ack in the thread,
no claim on it — so a thread another run holds can be referenced too.

The reference is platform state for that **run**: like its title and description
(spec D3), local to this orchestrator, invisible to anyone else's fleet view, and
dropped when the run is forgotten. It rides on the fleet payload as
`run.references` beside `run.threads`, and the menu lists it under a *referenced*
heading with a remove action.

The run is told **once**, as a record from the platform, on the first tick after
its bus is up:

```
[platform · operator] reference thread "the design discussion": <permalink> — lmer-signal read / post --thread
```

Once, in two ways that have to agree: the record is marked announced only after
the bus accepts it, and it carries `ref: reference:<id>`, so the bus's own dedupe
answers even if this daemon has forgotten it told the run.

| route | what |
|---|---|
| `GET /api/sessions/{id}/references` | this run's reference threads |
| `POST /api/sessions/{id}/references {permalink, title?}` | add one; without a title the link supplies one |
| `DELETE /api/sessions/{id}/references/{reference_id}` | remove one |

```sh
lmer-ctl threads references SESSION
lmer-ctl threads reference SESSION PERMALINK --title "…"
lmer-ctl threads unreference SESSION REFERENCE_ID
```

The routes are addressed by **session** because that is where the operator is —
the run's own thread menu — and stored against the **run**, which outlives it. A
session that belongs to no tracked run is refused: there is nothing to attach a
thread to. At most 16 references per run, and the same **link** twice is one
reference — nothing is asked of Slack or the homeserver when one is written, so
a room named by its alias and the same room named by its id are two references,
unlike a *connection*, which resolves before it is recorded.

The same field is offered when composing a spawn, so a run can start knowing its
context threads. **Links only there**: the UI's spawn form takes permalinks and
the title is added afterwards from the run's own thread menu, where the two-field
prompt is — a deliberate narrowing, because a spawn form that asked for a title
per link would be a form for something the operator has not seen yet.

```sh
lmer-ctl spawn develop ISSUE_URL --reference-thread "<permalink>|<title>"
```

`references: [{permalink, title?}]` on `POST /api/sessions`, and a *reference
threads* field in the UI's spawn form. Unlike `targets` these reach no launch
argument and start no connector: they are attached to the run once it is tracked,
and announced on the first tick after its session's bus is up. A reference the
daemon cannot store is logged and the session still runs — by then it is up, and
there is nothing to undo it with.
