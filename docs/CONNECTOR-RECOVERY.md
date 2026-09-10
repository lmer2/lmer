# Connector ownership, delivery and recovery

Slack and Matrix connectors carry one resolved thread to one supervisor session.
Direct CLI sessions and the platform launch a separate connector child. They
replace that child after failure; they do not restart the session harness or its
supervisor. An unknown remote outcome does not carry an exactly-once guarantee.

## Ownership and lifecycle

A thread key contains transport, resolved conversation ID and thread root. Its
connector directory lives beneath the transport's host registry. A stable
`lease.lock` is held with an exclusive Linux `flock` for the owner's entire
lifetime. Acquiring it precedes any connector delivery, including notices.
`owner.json` records a random instance token, supervisor session, parent and
child process start identities. Display labels and PIDs alone cannot transfer
ownership. A separate metadata lock protects publication, handoff and release.

The platform's `s-...` registry ID identifies its connection records, not the
supervisor session. Platform launches and orphan checks use the session returned
by that connection's authenticated supervisor health endpoint. If it is not yet
available, the child binds the lease when the supervisor publishes its identity;
an unidentified orphan cannot be reclaimed.

A parent reserves the lease, passes its open descriptor to exactly one child,
publishes the child's boot/start identity and then releases a one-use readiness
pipe. The child checks the token, thread, descriptor inode and its own process
identity before delivery. Closing an inherited descriptor never explicitly
unlocks the shared open-file description. The parent waits for child exit before
releasing its copy. A second direct or platform owner cannot overwrite a live
claim. Legacy claims lack sufficient process identity for automatic signaling;
stop their original owner before attaching a replacement.

Both transport loops catch ordinary exceptions, log the failing direction and
retry with bounded backoff. Eight consecutive failures end that child, allowing
its owner to replace it. A definite missing bus ends it. Persistent send
uncertainty ends it with a recovery status. The owner reports retries and recovery
through the supervisor; exceptions cannot silently leave one dead worker behind.

SIGTERM only records a stop request. The main monitor consumes it within its
one-second polling interval and performs shutdown outside the signal handler.
This avoids re-entering a threading event lock if the signal interrupts polling.
A request arriving during reload finalization also prevents the subsequent exec.

The child's main loop checks loaded connector, lifecycle, delivery, ownership
and attribution modules every second. A change stops work and permits up to 45
seconds total for workers, detach notice, checkpoint, final bus acknowledgement
and lease finalization or reload preparation. A drained child executes its canonical
module entrypoint under the same lease, including native `python -m` launches.
On expiry, exec is refused. A retained descriptor keeps the lease held until the
child exits, even if its finalization worker finishes late. An undrained child exits; its parent waits before replacement. The same monitor
notices a dead parent and drains the orphan. A new owner can additionally signal
an orphan using a pidfd only after checking its supervisor, boot/start identity,
module and exact permalink. If pidfds are unavailable, it waits for the child's
own shutdown; it never falls back to signaling a bare numeric PID.

These are connector-process guarantees. Container, deployment and systemctl
acceptance for the already-landed #349 shutdown/store-close work remains
unverified by this change.

## Durable delivery

A checkpoint is bound to a nonempty identity returned by the supervisor. The same
validation handles immediate and deferred startup. Missing identity permits no
checkpoint import or delivery. Checkpoints belonging to another supervisor are
preserved separately. A valid same-session checkpoint takes precedence over an
older bus acknowledgement or launch cursor.

Each checkpoint contains the bus and transport cursors, pending inbound
requests, held messages and known attribution, questions and their remote
references, and the current outbound source record and parts. Inbound and
outbound updates use one state lock. Outbound transport waits occur outside that lock.
The grace queue for unknown attribution is size-limited; known pending input is retained until acceptance, including batches larger than that grace limit. Held items serialize as an ordered list so checkpoint key sorting cannot change their arrival order.
Attribution is acknowledged only after supervisor acceptance; a failed batch
releases its temporary claims while keeping its retained messages. A prepared
answer keeps its original question binding when retried or reconstructed. Prepared inbound requests resume from durable state even when the remote sync no longer returns the event.

For each externally visible outbound operation:

1. Save the immutable source, operation ID and rendered payload identity.
2. Write and fsync a temporary marker, publish it exclusively as
   `pending-send.json` with a hard link, then fsync its directory before calling
   the remote send API. A crash before publication leaves no live marker and no
   possible send; a crash after publication leaves a complete operation selection.
3. Save the returned receipt in the thread's durable receipt-ownership ledger and
   checkpoint before removing the marker or attempting attribution/question bookkeeping.
4. Save bookkeeping, then advance the cursor and remove the pending source in a
   single checkpoint replacement.

Checkpoint writes use a temporary file, file fsync, atomic replacement and
directory fsync. The `receipts/` ledger binds each remote receipt to its supervisor,
source, part, payload and exact attempt. It survives checkpoint completion, owner
reconstruction, supervisor changes and eviction from the 256-entry echo cache.
Recovery also reads confirmation from this ledger when the subsequent checkpoint
write failed. That exact-operation receipt prohibits retry and can complete
bookkeeping without a remote resend or a supplied remote reference. Conflicting
receipt records remain blocked with their state preserved.
Ledger files are retained for the thread's lifetime; deleting them would remove
receipt-ownership evidence. Text and each Matrix attachment have separate receipts. File
parts are indexed, even when names match, and retain their content hash, size,
media type and uploaded URI. A retry uses the saved original payload. Confirmed
parts finish bookkeeping without another remote send; remaining parts resume in
order. Original supervisor records remain available for source validation.

A failed receipt write after a confirmed remote response freezes checkpoint
updates and leaves the pre-send marker intact. Stop, drain, process exit and a
fresh owner do not clear it. An unknown outcome is also blocked. Verified
pre-send failures and structured remote rejections can retry normally once their
non-acceptance is safely recorded. A lost response, an unclassified transport
exception or a response without a receipt is uncertain, even if the remote
probably accepted the operation.

## Explicit recovery CLI

Use the host installation and the original supervisor's control endpoint. Use a
canonical thread-root permalink, including a resolved Matrix room ID. Stop the
current owner or allow its blocked child to exit before recovery; the command
must acquire the same exclusive lease.

```bash
python -m lmer_cli.connector_recovery --permalink "$THREAD" inspect
```

Inspection reports the exact pending operation ID, supervisor, source sequence,
part, rendered payload and any saved receipt. It does not change cursors. Inspect
the remote thread to determine what happened.

When a receipt was saved, complete its bookkeeping path with:

```bash
python -m lmer_cli.connector_recovery --permalink "$THREAD" receipt --operation "$OPERATION"
```

When a confirmed remote reference is available but was not saved, select it:

```bash
python -m lmer_cli.connector_recovery --permalink "$THREAD" receipt --operation "$OPERATION" --remote-ref "$REMOTE_REF"
```

The command verifies thread, sender and content (including attachment bytes).
It rejects a receipt assigned to any other operation in the durable thread ledger,
as well as receipts in retained attribution or another pending part. It also validates the
current supervisor and the retained source record before changing state.

For one genuinely uncertain send, the operator may explicitly authorize one
retry after inspecting the remote thread. **That retry may create a duplicate.**

```bash
python -m lmer_cli.connector_recovery --permalink "$THREAD" retry --operation "$OPERATION" --inspected-thread --accept-possible-duplicate
```

The authorization applies only to that operation. Other receipts, the source,
question context, attribution and remaining attachments stay intact. Confirmed
sends cannot use this retry action. A saved verified rejection or pre-send
failure does not require acceptance of uncertain delivery. Wrong or stale
operation IDs fail without mutation. There is no blanket force replay or
arbitrary checkpoint/cursor import.

Recovery writes the transition before removing the marker. An interrupted
transition may be resumed using the still-pending original operation selection.
If an interrupted retry authorization is replaced by a verified receipt, the unused
retry is cancelled and confirmation stays bound to the original attempt. Interrupting
that receipt transition still permits saved-receipt completion with the original ID.
The next send consumes its authorization and creates a new attempt ID; a second
uncertain outcome needs a new explicit selection. The old authorization cannot
silently authorize another send. Restart or reconnect the owner after recovery;
a running owner waiting on recovery can resume through its normal retry loop.

## Test matrix and limits

| Boundary | Behavioral verification |
| --- | --- |
| Real startup and transport exceptions | Both loops, continued polling, bounded failure exit |
| Immediate/deferred identity | No mutation before identity; saved cursor outranks older ack; other session preserved |
| Confirmed delivery | Bookkeeping faults and receipt replacement faults retain one remote send and original journal |
| Ingestion | Failed held batch and reconstructed pending answer retain attribution and original question |
| Ownership | Competing startup and child handoff exclude a second sender; unique instance token survives exec |
| Owner replacement | Direct owner replaces only its child; platform reconstruction preserves durable blocking |
| Reload/drain | Native `python -m` and actual exec argv; blocked workers and delayed checkpoint/ack/lease preparation obey one total deadline |
| SIGTERM | Real signal while the main thread holds the event-wait lock; normal exit after a signal during reload finalization |
| Orphan | Real owner exit and stale selections; outer cleanup supervisor collects detached descendants on timeout/nonzero, which remain failures |
| Recovery CLI | Actual CLI, wrong/stale IDs, 257 completed sends before receipt-reuse rejection, interrupted retry then interrupted receipt; ledger-only confirmation refuses retry and resumes receipt recovery without a resend |
| Marker publication | Process exit during temporary creation and after complete publication; no empty live JSON or automatic uncertain replay |
| Matrix files | Same names with distinct indices/content, partial receipts, selected retry and remaining attachments |

Tests use real supervisor HTTP paths and real child processes with controlled
remote adapters. Timing tests scale the polling/drain constants to keep runs
bounded. They do not establish remote service availability or deployment
acceptance. Fixtures retain source journals, join their worker/server threads,
and terminate and wait every test child. Native pidfd signaling requires kernel
support; the sandbox exercises the safe child-side orphan shutdown path.
