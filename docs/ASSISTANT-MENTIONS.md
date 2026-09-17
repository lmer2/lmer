# Summon the assistant from Slack or Matrix

The host listeners can route bot mentions to the existing Uber Lmer assistant.
The assistant reads the request, answers in that conversation, or starts a worker
under the operator's existing run-start policy. Receiving a mention does not
start a worker or connect the conversation to the assistant's broadcast channel.

## Configuration

Set these in the host environment or `~/.lmer/.env`, alongside the listener and
platform configuration:

| Variable | Default | Accepted values |
| --- | --- | --- |
| `LMER_ASSISTANT_MENTION_ROUTING` | `false` | `1`, `true`, `yes`, `on`; `0`, `false`, `no`, `off`, or empty. Case-insensitive. |
| `LMER_ASSISTANT_MENTION_BACKLOG` | `10` | Integer from `0` through `100`. Zero omits preceding room/channel messages. |

Restart the platform daemon and each listener after changing these values. Invalid
values prevent startup with a message naming the variable. Both listeners must
run on the platform host, with the same lmer state directory and transport
account as the daemon. They read its configured bind address and secret file;
they do not choose a remote assistant from a message or its sender.

Enable the existing `lmer-slack-listener` and/or `lmer-matrix-listener` as usual.
The Slack app needs `app_mention` and direct-message events, the history scopes
for the conversations it reads, and its existing message-post permissions. If
`LMER_SLACK_DM_ALLOWED_USERS` contains handles, the app also needs `users:read`
to confirm the sender of a top-level or threaded assistant mention or a DM
reply; exact user-ID entries need no lookup. Matrix uses the existing bot account
and joined-room configuration. Encrypted Matrix conversations remain subject to
the existing chat transport limitations.

When disabled, listeners retain their existing behavior: eligible mentions and
Slack DMs can launch chat sessions. The added assistant DM-reply route is off.
Disabling this setting does not disconnect any run or remove an already queued
request. Ordinary connected-run delivery is independent of the setting.

## Entry points and ownership

- **Mention inside a thread:** preserve the thread root and provide its permalink
  so the assistant can read its history in place.
- **Top-level channel/room mention:** use the triggering message itself as the
  thread root. Include up to N preceding messages, oldest first, followed by the
  trigger. The first assistant or worker reply establishes the reply thread;
  ingestion does not post a second root or a public acknowledgement.
- **Reply to an assistant DM:** match Slack `thread_ts`, or the Matrix `m.thread`
  relation, to the exact parent receipt recorded when the assistant sent its DM.
  The receipt contains identifiers, recipient and originating assistant session;
  it contains no message body and survives assistant rotation.
  DMs sent before this feature was installed have no receipt; send a new DM
  to establish a parent for this entry point.

Inside a DM conversation, ordinary mention routing is off on both transports.
Only a reply under an exact assistant DM parent qualifies, even when another
message explicitly mentions the bot. No channel/room backlog is fetched for a DM.
Matrix DM classification uses the bot account's `m.direct` directory and host
DM records. The shared DM-send path records the room before posting a message,
for both workers and assistants, even when routing is disabled. If that local
write fails, the send is refused. A failed `m.direct` update therefore cannot
turn a room used for a DM into an ordinary mention destination. Room records
grant no reply authority: only the separate assistant-parent receipt does.
Existing assistant DM receipts also identify their rooms. Failure to read DM
metadata refuses admission. These host records contain no message text and
survive daemon restarts; they have no automatic expiry.

In enabled mode, Matrix mentions require the bot's exact MXID in
`content.m.mentions.user_ids`. Plain-text names, quoted names, similarly named
users and localpart substrings do not count. The disabled listener retains its
legacy mention recognition. An unmentioned threaded reply can still qualify
through the parent-matched DM route.

Every assistant entry point requires the sender to pass
`LMER_SLACK_DM_ALLOWED_USERS` or `LMER_MATRIX_ALLOWED_USERS`: top-level mentions,
mentions inside threads and replies under assistant DM parents share the same
admission rule. These are the same host-side lists used for outbound DMs; unset
or empty means nobody. Slack IDs and transport-confirmed handles keep their
existing distinct namespaces. Refused senders are recorded in host logs without
message text or a public reply. The established-conversation exemption for
outbound DMs does not authorize this inbound route. Disabled-mode legacy mention
routing continues to use the listener behavior described above.

A thread already held by a run stays with that run, including another thread in
the same DM channel. This also covers connections the daemon has recorded whose
connector has not started yet. Top-level DM messages and replies under unrelated
DM parents do not qualify for the DM-reply entry point. Matrix rich replies using
only `m.in_reply_to` do not count as threaded DM replies.

## Assistant delivery and replies

The daemon adds a `conversation_request` to `POST /api/assistant/pending`, the
assistant's existing inbox. Its normal pending watch and nudge mechanism notify
the current assistant. Requests remain pending if it is offline; this feature
does not start it automatically. Source data includes the transport, sender ID,
triggering message ID, conversation, thread root, permalink and optional DM
parent receipt. Each message carries text and an explicit clipping flag if it
exceeds 8,000 characters. Read the source thread to obtain the full text.

The assistant reads the supplied permalink with `lmer-ctl thread read <permalink>`
and responds using `lmer-ctl thread post <permalink> <text>`. That explicit post
addresses only the named conversation and does not journal the body on the
assistant's shared bus. Do not use a broadcast `lmer-signal post` for contextual
or private replies. `lmer-signal dm` still starts a private message to a recipient;
use the thread-post verb to answer beneath the original DM parent.

To hand work off, the assistant can use `lmer-ctl spawn` with an additional
`--target-thread <permalink>` option, or connect a chosen live worker using
`lmer-ctl threads connect <session-id> <permalink>`. The ordinary single-owner
rule applies. The assistant decides whether to answer or spawn; the listener
does not infer that decision from the message text.

## Replay and failures

An accepted request and the hash of its transport/conversation/message identity
are written atomically to the assistant state. The content-free receipt remains
after consumption and daemon restarts, so replay cannot create another request.
Distinct messages in the same thread remain distinct requests. Receipts have no
automatic expiry. DM parent receipts are stored under `assistant-dms/` in the
host state directory and also have no automatic expiry.

Conversation requests are protected from ordinary digest eviction. A full inbox
refuses new requests with HTTP 429, leaving their identities eligible for retry.
An unavailable daemon or failed history read is logged by the listener; there
is no fallback worker spawn. A failed delivery is not marked accepted. Listener
and daemon settings must agree: an enabled listener receiving `disabled` from
the daemon logs a configuration failure and does not spawn a worker. Listener
transport delivery is not a durable outbox: after a logged failure, retry by
mentioning the bot again once the cause is resolved. An initial Matrix sync is
still treated as history, as in the existing listener.

Slack context uses `conversations.history` with an exclusive `latest` bound.
Matrix uses the trigger's `/context` backwards token and `/messages?dir=b`,
so the boundary is the event's timeline position, independent of clock skew.
Both follow pagination up to N. See the
[Slack history contract](https://docs.slack.dev/reference/methods/conversations.history/)
and [Matrix event context contract](https://spec.matrix.org/v1.15/client-server-api/#get_matrixclientv3roomsroomidcontexteventid).

## Host API

`POST /api/assistant/requests` takes
`{transport: "slack"|"matrix", conversation: "…", event: {...}}` from the host
listener. The platform's operator credential is required; assistant and connector
credentials cannot submit transport identities. Responses report `accepted`,
`duplicate`, `connected`, `ignored`, or `disabled`. Acceptance reports the current
assistant session, if one is live, and the destination permalink. Bodies are
stored in the private inbox, not broadcast to any assistant-connected thread.
