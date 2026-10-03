# Codex structured delivery

Codex sessions use one private app-server backend shared by the normal TUI and
supervisor input adapter. Automated messages, unclaimed answers, startup prompts
and digest reminders use structured input. Raw operator terminal input retains
its existing behavior. Claude and Pi keep their existing runners and delivery.

## Why the backend is needed

Codex 0.153.4 can display a latency menu with **Retry with a faster model** selected.
There is no supported configuration to disable that menu or select always-wait.
The rate-limit notice setting controls a different menu. Automated delivery must
never send terminal Enter, numeric choices, cancellation or a paste into this menu.
The adapter makes no model, effort or approval changes: Codex keeps waiting.

Claude's startup handler preconfigures known trust/onboarding state and waits for
readiness markers. Its timeout path is best effort; it does not acknowledge model
receipt. That approach cannot safely resolve a dynamic Codex menu appearing between
a screen read, paste and submit. Native probes also demonstrated a swallowed paste
sending an old draft later, and a retained draft concatenating with a retry. F11
composer-only submission prevented menu acceptance but did not preserve delivery.
Native queue submission eventually reaches an idle model, but loses midturn steering.

## Lifecycle and configuration

The Codex adapter starts the installed `codex app-server --listen unix://…` before
the supervisor forks its TUI. The socket is mode 0600 inside a fresh mode 0700 local
directory. No TCP service is exposed. The TUI attaches using `--remote` to that same
socket. The backend inherits the runner's environment, credentials, working directory
and shared Codex configuration. Model and `-c` overrides are forwarded to it; the TUI
also sends its workspace and approval selection through native thread startup.
Update prompts are disabled through `check_for_update_on_startup=false`.
The PTY auto-start delay, readiness marker/timeout, post-marker settle delay
(`LMER_AUTO_START_SETTLE_DELAY`), CR-nudge delay, submit delay
and `start_prompt_delay` settings do not apply to Codex. Native startup waits for
thread ownership; its follow-up prompt is submitted after startup acceptance and
can steer that active turn. Manual startup still enables runtime observation.

This implementation was verified with **codex-cli 0.153.4**, upstream tag
`rust-v0.153.4` (`3d2ee51`). Unix WebSocket/remote-TUI support and client message IDs
are experimental APIs. The WebSocket client disables compression because that
version rejects its default compression negotiation. Unsupported native APIs fail
visibly; there is no fallback to terminal submission. Explicit remote endpoints,
ephemeral sessions and profile overrides are refused before launch: they cannot
meet this adapter's backend ownership and persistent receipt contract.

The adapter discovers exactly one persistent user thread, ignoring ephemeral title
threads and child threads. A fresh idle thread is discovered through metadata:
history is unavailable before its first turn in this version. The first thread ID
binds the run. Reconnecting the client or resuming the TUI on that backend retains
it. Multiple primary threads or switching to a different thread holds input with
an error, rather than guessing the TUI's focus. Start a new managed run to change
threads. A backend crash ends this delivery path; recovery is explicit, not an
invisible replacement backend or automatic payload replay.

On shutdown the adapter stops receipt monitoring, closes its connection, terminates
and reaps its backend, and removes its private directory when all inputs were
confirmed. Startup failure logs and unconfirmed originals remain in that private
directory; the notice names its path. Review retained input before any manual retry.
Credentials are inherited, never copied into receipts. Existing auth failures still
require operator attention.

## Delivery and receipts

`POST /input` with `append_newline: true` dispatches Codex prose to the adapter.
An optional `delivery_id` identifies retries of the same submission. Bus messages
use their durable slash-bearing record ID (`session/000001`) unchanged. IDs are
bounded receipt keys, never filesystem paths. The adapter persists the original text and SHA-256
before sending. Reusing an ID with different text is rejected. Repeating an ID
returns its existing receipt, except when `retryable: true` proves `turn/start`
was never attempted. Only that pre-submission failure may retry the same original
text and ID.

Native `turn/start` atomically starts an idle turn or steers the running turn. It
omits model/effort overrides, avoiding a read-then-steer turn-end race. Native
`clientUserMessageId` correlates the original input; it is **not** upstream
idempotency. The adapter owns deduplication for the lifetime of its backend.

Receipts distinguish these states:

| State | Meaning |
| --- | --- |
| pending | Original saved, no native acceptance yet |
| accepted | Native API accepted input; model-context confirmation still pending |
| confirmed | Native thread contains this client ID and exact original content |
| rejected | Discovery or explicit native rejection prevented acceptance |
| uncertain | Response lost, content mismatch, or turn ended without confirmation |

Acceptance alone never sets `submit_confirmed`. Readback compares exact text,
including Unicode and whitespace. Cancellation can leave accepted steering input
unconsumed; it becomes uncertain and remains retained. No uncertain payload or native `turn/start` rejection is
replayed automatically. Errors are published as supervisor notes, and the platform
input client surfaces rejected/uncertain deliveries as failures.

`GET /healthz` includes `codex_delivery.runtime` (native thread ID, model, effort,
and mismatch against launch preset) and the latest 100 public receipts. Runtime metadata is
observed every five seconds even with no pending receipts, including manual-start
sessions. Pending receipt reads run every half second. RPCs have a three-second
deadline; failures retain the last observation timestamp and expose an observation
error. Native model changes produce a warning without overriding human controls. Original
text stays out of this response. Receipt storage is bounded to 10,000 IDs per run;
exhaustion refuses new submissions visibly. A private outbox is diagnostic recovery
material, not an automatically replayed queue. A native-context receipt is stronger
than transport acceptance; the native proof additionally asserts exact model HTTP
request content and rollout records.

Answers claimed by a waiting `lmer-signal` CLI continue through the existing bus
waiter. They are not also injected into the model. Unclaimed answers use the Codex
adapter. Unrelated native approval requests remain owned by the TUI; the delivery
client neither accepts nor dismisses them.

## Digest wake-up

The existing daemon detector tick (every 30 seconds, plus tick execution time)
checks the assistant's pending spool even while
Codex is in another conversation. It uses the advertised Codex adapter only,
with a stable notification text and ID per session/pending accumulation. Additional
digests coalesce while that accumulation remains pending. A lost HTTP response can
be checked with the same ID without a second native submission. The daemon stores
an acknowledgement only after native confirmation, so daemon restarts neither
lose the pending wake nor repeatedly submit it. Errors use the detector's existing
coalesced logging. There is one existing detector watcher, no model-turn polling
loop and no new service.

A wake does not drain the spool. The assistant still calls
`POST /api/assistant/pending` and acts on what it takes. The existing idle daemon
nudge remains the fallback when the assistant ignores a confirmed wake.
Codex idle reminders also use a stable ID and fixed original text, keyed by the
session, pending accumulation and last durably stored nudge mark. Lost responses or
daemon restarts check the same receipt. Only confirmation advances the reminder
interval; accepted or uncertain input cannot create another idle reminder.
In-memory timing bounds never supply a Codex delivery identity. A failed mark
write is retried using the confirmed receipt; it cannot authorize the next reminder.
The primary and idle paths share the accumulation's confirmation state. Primary
confirmation starts the idle reminder interval atomically with its durable
acknowledgement. Until that write succeeds, including across daemon restart,
the primary holds the idle fallback. A confirmed wake whose spool stays unhandled can
receive a deliberate idle reminder after the configured interval.
Proven pre-submission failures retry on a later detector tick after recovery.
Uncertain sends stay blocked for review, and errors are coalesced without a flood.
A known Codex session with unavailable native health holds reminders.
Other runners retain their existing idle fallback. This is the bounded
Codex case of #294's digest-watch problem, not a second generalized watch system.

## Verification

The manual native probe runs an installed Codex TUI and backend against a local
Responses fixture. It injects the exact native latency event and asserts original
payloads in model requests and native transcripts, with model/effort unchanged.
It also covers stale drafts, repeated IDs, idle input, turn boundaries, reconnect,
TUI resume and an unrelated approval requiring a separate test actor.

Focused tests cover API dispatch, no terminal writes, exact receipt readback,
ambiguous delivery, thread ownership, startup/cleanup, the unchanged Claude/Pi
paths, and digest acknowledgement/restarts/error coalescing. Native failure
corrections and detailed logs are kept in the #450 work-run investigation artifact.
