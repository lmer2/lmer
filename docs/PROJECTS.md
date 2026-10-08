# Named projects and project assistants

A project is a named workspace, independent of a forge repository's `project`
field. Several projects may use the same repository. Each tracked run has one
home project, recorded under its existing `host/project/slug` identity.

Create a project with **New project**, supplying only a name. Names are unique
ignoring case. Its chat opens immediately; its assistant starts when capacity is
available. Rename, repositories, color, execution overrides and
thread connections live in Project settings. The color picker accepts any hex
color, and the 20 named colors are its swatches. Picking one of them stores its
name, so preset projects look the same as before. Repository associations supply
launch suggestions and never adopt existing runs. Expand Standing instructions
in Project settings to edit them; the project's assistant chat shows a read-only copy.

The project switcher at the left of the app header selects the main view. It
shows the selected project's name (or **All projects**); on a phone it takes a
row of its own inside the header. Its menu lists All
projects and every active project, each with its color and Needs you count, plus
**Project settings** for the selected project and **New project**. While a project
is selected, the header stays in place when the page scrolls and is painted in the
project's color. The header's text is black or white, whichever has the higher
WCAG contrast against that color, in both themes. All projects and unavailable
project links keep the theme's own header.
The sidebar keeps every active project visible,
including empty projects, with independent collapse controls and project-local
Needs you/status counts from the full fleet. The compact robot button on each
project header opens that project's chat and shows its assistant lifecycle/queue
status, even when the section is collapsed. **New project** is above these groups.
All projects is grouped monitoring. Its selection does
not grant a combined assistant: a chat header always names that pane's project.
The URL retains the selected project. Drafts and outstanding requests retain
conversation identity; changing selection cannot redirect them. A stale or
archived project URL explains its state instead of selecting General.

All projects and the global sidebar also list **Unassigned** recovery entries,
including legacy failures whose project ownership is unknown. Open a row to
inspect its session; this returns to the operator-wide scope without assigning
it to General or another project. Selected-project lists still contain only
that project's rows. Registry records and history remain intact.

An unassigned run — one with no recorded home — is recoverable from its run
detail: the operator selects an active destination and chooses **Move run**,
which assigns the run there (refused while a worker is live). Forgetting an
unassigned run needs no project choice, but stays operator-only. A run whose
recorded project is archived is **not** unassigned: moving it out is a transfer,
not a new assignment. The identity chip reads **Unavailable project** only when
a run names a workspace whose name cannot be resolved; an archived project keeps
its name, so the chip shows it.

The **Platform settings** cog in the global app header is available on desktop
and phone views, separately from the selected project's **Project settings**.
Platform settings contains shared worker capacity, assistant capacity, execution
and display defaults, transport infrastructure, slot administration and archived
projects. Defaults affect subsequent assistant launches; project execution
settings override them. Thread addresses, instructions, history and memory are
never inherited from another project.

## Admission and recovery

The default assistant cap is **4**, including General. Worker capacity is
independent and shared across projects, retaining #514's reserved assistant
capacity. Lowering either limit preserves incumbents and blocks additional
admissions until capacity becomes available. Slot applicability adds an admission
condition; it does not introduce per-project worker quotas.

`projects.sqlite3` stores projects, run homes and ownership revisions, session
bindings, desired assistant state, FIFO start tickets, pending events, thread
ownership, conversation requests and action receipts. An in-process lock plus a
host file lock serializes launches, moves and control mutations, including CLI
writers. Starts/rotations reserve assistant capacity before launch. A running
project has at most one assistant. A cancelled queue ticket cannot launch later.
A new assistant waits on a parent-owned pipe until its registry entry, project
binding and lifecycle state are published. Parent death during publication closes
the pipe, preventing an unregistered assistant from executing. Execution errors
return through a separate pipe so the project retains the actual launch error.

Waiting projects show their FIFO position and Cancel. A failed start retains the
project, error and Retry. Stop records desired state before terminating the
assistant; supervision completes that intent after restart and does not revive
it. Workers continue and their pending questions/events remain retained.
Unexpected exits receive bounded recovery (three consecutive failures), with
backoff and a visible terminal failure. A settled incarnation resets that budget.
Rotation retains that project's place and other projects' state. Idle time and
navigation have no stop semantics.

Daemon startup reconciles requested starts before serving and reports each
project's outcome. Shutdown joins supervision through any admitted launch's
publication; already running assistants retain their processes and state.

The fleet and sidebar show the current assistant generation for each project.
After recovery, a dead predecessor is omitted only when durable conversation
membership proves that the recorded successor superseded it. Its registry entry,
terminal log and conversation history remain available; clearing crashed sessions
is not required. A failed current generation and unattributed crashes still appear
in attention. Stopping a successor does not bring its retired failures back.

While a replacement launch is in progress, the project control says **starting**
(or **rotating**) instead of presenting the previous dead process as current.
Once the launcher is alive its row says **running**; this is process liveness,
not a guarantee that the container's chat endpoint is ready. A current process
exit is reflected in project status before the next supervision pass updates
retry state. Repository-free assistant rows use their session IDs as fleet keys,
so equal task names cannot collapse generations or projects. Existing session and
conversation-history references do not change.

Execution settings are resolved at launch. Check-in/nudge/rotation policies
continue to apply, with consumers and reminder bookkeeping scoped per project.
Hourly checks group only each project's runs. Typed `connector_health` events
remain diagnostic and never enter assistant digests or wake assistants (#512).
Real questions, messages, failure/completion signals and hourly checks remain
eligible; filtering uses event metadata, not text matching.

## Migration and storage

Daemon startup imports legacy state into **General** once. Project mode becomes
active only after the migration transaction commits; an interrupted import is
retryable and leaves legacy reads intact. Existing assistant
identity, generation linkage, pending work and tracked runs are retained. General
keeps legacy history, memory, upload and environment paths so a running container
keeps its mounts. New projects use separate directories below `projects/<id>`
and separate assistant credentials. Import completion is transactional; repeating
migration neither duplicates history nor overwrites assignments.

The fleet view resolves ownership from the durable run home first, then the
session ID inside its inventory record. Migrated live sessions without tracked
runs remain visible in General. Unowned dead sessions remain visible in the
unfiltered inventory and attention list without acquiring a project assignment.

Legacy slots with no applicability field show **Needs project assignment**.
They retain live holders and admit no new project launches until an operator
assigns projects. An explicit empty list likewise admits nobody. No repository,
preset or service name is used to infer assignment. Use the installation's current
fleet inventory and require an explicit operator assignment for each slot;
example inventories are not automatic migration rules.

An assignment edit affects future admissions. Current slot/service holders keep
their occupancy. Admission checks both named slots and equivalent configured
preset/service/group paths, including overlap in group members. Direct or stale
API/CLI requests cannot bypass applicability by omitting the slot name.

## Explicit API and CLI destinations

An operator supplies `workspace_id` in JSON, the query string, or
`X-Lmer-Workspace-ID`. Conflicting destinations fail. Project assistant
credentials bind the caller to its current project and incarnation. Known run
and session actions resolve their recorded home and recheck it at execution.
Unassigned new work fails with `project_required`; General must be selected.

`POST /api/runs/move` is operator-only. For a run with a recorded home it
transfers ownership to the active `destination_id`; for a run with no home it
assigns the run to that destination at revision 1, refusing while a worker is
live. A supplied `workspace_id` that disagrees with the destination is a
conflict **only for that no-home assignment** — an existing-home transfer
supplies its source workspace and a different destination by design, and is
never treated as ownership inference. `POST /api/runs/forget` is unbound when
the run has no home — no project choice is needed — but a project assistant is
refused there; an assigned run keeps its existing rules.

Operators can inspect retained unassigned sessions through
`GET /api/sessions/{id}/messages`, `/log`, and `/ask` without selecting a project.
These reads preserve unresolved ownership and use the existing history readers;
an explicit mismatched project still fails. This exception does not authorize
project assistants or connectors to read unassigned sessions, or grant session
mutations, terminal tickets, uploads, or other session routes without a home.
Terminal tickets also retain this authority. Redemption, input and log delivery
refuse changed ownership or an exited calling assistant generation.

`lmer-ctl --workspace-id ID ...` sends the same binding. Project assistants inherit
`LMER_WORKSPACE_ID`. Host commands use `lmer platform --workspace-id ID ...`.
The existing forge `project` parameter continues to mean the repository path.

| Route | Purpose |
| --- | --- |
| `GET /api/projects` | Projects and lifecycle/queue status; assistants see their own project |
| `POST /api/projects` | `{name}` creates and queues a project |
| `PATCH /api/projects/{workspace_id}` | Name, color (a named color or `#rrggbb`, stored lowercase), repository URLs and execution `settings` |
| `POST /api/projects/{workspace_id}/archive` | Stop/archive an eligible project |
| `POST /api/projects/{workspace_id}/restore` | Restore its identity/history, stopped |
| `GET`, `POST /api/settings` | Platform defaults and shared capacity |
| `POST /api/runs/move` | Operator-only: assign an unassigned run, or transfer an owned run, to an active `destination_id`; `workspace_id`, `home_revision` |
| `POST /api/runs/forget` | Stop tracking a run; unbound for an unassigned run, operator-only there |
| `GET /api/project-context?source_workspace_id=ID` | Operator-only bounded fleet excerpt with source identity |
| `POST /api/project-relay` | Operator-only destination and supplied text, with source provenance |
| `GET /api/assistant/requests` | Retained conversation requests and delivery errors |
| `POST /api/assistant/requests/{request_id}/reply` | Reply to that request's fixed source thread |
| `POST /api/assistant/requests/{request_id}/release` | Operator-only `{reason}` releases a blocked request only when all effect outcomes are known |
| `POST /api/assistant/requests/{request_id}/park` | Operator-only `{reason}` releases independent work without resolving or replaying an uncertain reply |

Existing assistant lifecycle, history, instructions and pending routes use the
binding. Administrative project/slot/capacity mutations require the operator.
Explicit context lookup/relay grants neither source-run control nor a copy of
another project's conversation or learned memory. The API index (`GET /api`)
remains the route reference.

Examples:

```sh
lmer-ctl projects list
lmer-ctl projects create 'Research'
lmer-ctl --workspace-id ID spawn develop 'issue-123' --repo-url https://forge.example/org/repo
lmer-ctl --workspace-id SOURCE projects context DESTINATION
lmer-ctl --workspace-id SOURCE projects relay DESTINATION 'Requested excerpt'
lmer-ctl --workspace-id SOURCE projects move-run forge.example/org/repo/run DESTINATION --home-revision 1
lmer-ctl --workspace-id ID runs resume forge.example/org/repo/run --home-revision 2 --confirm-project --slot eligible-slot
```

## Peer UL awareness and native coordination

Current project UL credentials can read a limited directory with `lmer-ctl peers
list` (`GET /api/assistant/peers`). It exposes peer project IDs/names, archive and
lifecycle state, current tracked UL session IDs/generations, availability and
connected Slack/Matrix thread links with their running status. A stopped,
archived or untracked assistant has no advertised current session. Availability
checks local lifecycle and control-plane configuration; it is not a remote
health probe or a promise that the process will still be reachable later.
Settings, credentials, worker inventory, transcripts, memory and handovers are
not in this directory. The ordinary project APIs remain scoped.

`lmer-ctl peers orders PROJECT_ID --purpose 'Human-requested task' --limit 2000`
reads that project's standing instructions explicitly. The API is
`GET /api/assistant/peers/{peer_id}/instructions?purpose=...&offset=0&limit=2000`;
each read is limited to 4000 characters and retains source project, document
timestamp, offset and truncation metadata. Secret scrubbing applies on read.
The purpose is an audit description, not a claim of operator authentication or
a grant to other APIs. Adapt only relevant material for the requested task;
peer instructions never override local instructions or authorize wholesale
autonomous copying of context stores.

Use an explicit discovered UL session address for native coordination:

```sh
lmer-ctl peers list
lmer-ctl peers orders PEER_PROJECT_ID --purpose 'Apply the requested writing conventions'
lmer-ctl send PEER_SESSION_ID 'Requested coordination' --ref task-42-question-1 --generation 3
lmer-ctl peers receipt task-42-question-1
```

The send uses the existing `POST /api/sessions/{session_id}/comms` route, session
bus, delivery accounting and native delivery. `ref` is required for cross-project
UL messages; `generation` is an optional additional check. A session ID alone
suffices to address an incarnation. The server derives source project, session
and generation from the current UL credential and rejects caller-supplied source,
role, metadata, attachments, question answers and commands. It does not grant
foreign worker control, transcript reads, settings changes, lifecycle actions or
general cross-project authority. The existing operator-only context/relay routes
remain operator-only. Their restriction does not apply to native peer messages
or the bounded peer orders route. For an already-authorized exchange, a UL can
read its own standing instructions with `lmer-ctl orders get` and send the relevant
excerpt to its peer using its own current UL credential. The requesting UL can
instead use `lmer-ctl peers orders PROJECT_ID --purpose 'Requested work'` with
its own credential. No extra operator credential, manual human relay, external
thread attachment or assistant rotation is required for these existing paths.

The receiver sees a **peer UL message** with source identity and a native reply
command. The bus record and retained project history carry the same provenance.
For an already-authorized exchange, reply directly using that command and your
own UL credential, with a new reference for the reply. Incoming guidance names
the available orders/read/send commands even for assistants with older startup
instructions. Peer coordination does not grant unrelated human/operator authority.
Neither transport connector mirrors peer
messages to external threads. Task instructions also prohibit unsolicited
Slack/Matrix replies and connector sharing in response to peer content.

`peer_receipts` in the existing project database retains send identity and the
original comms result; it is not a delivery queue. A reference is scoped to the
sending project, remains valid after sender rotation, and cannot be reused with
different text, address or generation. Concurrent duplicates return the saved
receipt. Read it with `GET /api/assistant/peers/receipts/{ref}`. `recorded` means
the bus accepted the record, not that the model has consumed it: inspect the
original `relay.state`, `submit_confirmed` or PTY receipt. Receipts are snapshots;
this API does not upgrade pending confirmation later. An actual message received
at the destination or its subsequent reply is later evidence of arrival. An old
uncertain snapshot alone is not a reason to request duplicate delivery or a manual
human relay; retain the snapshot and the later evidence separately.

Unknown post-send outcomes are durably marked uncertain and return 409 on retry,
without sending again. A native uncertain or rejected result also returns 409
with its original bus and relay receipt; bus acceptance is not hidden by that
error. A failure proven to precede bus acceptance can be retried
with the same reference. A crash after marking a send in progress is conservative:
the result remains uncertain even if no bytes ultimately left. Do not infer
remote success, retry under a new reference or silently choose a rotated
destination. New sends to stale, stopped, archived or unavailable sessions fail;
no assistant is spawned, resumed, rotated or connected just to deliver. Looking
up a retained receipt for an old address does not deliver anything to that
address or its successor.

## Moves and archive

A move waits for the shared control boundary, then refuses any live worker. It
changes only durable ownership and its revision: checkout, service, artifacts,
run identity, transcript and question identities are unchanged. A run with no
recorded home is **assigned**, not moved: the operator names an active
destination and the run gains a home at revision 1, so no source project is
read or inferred. A run whose recorded home is an archived project is still a
transfer — never an adoption — and may be moved out to an active project.
Undelivered
run events/questions follow the run once. The destination gets a short
run-specific move notice, not the source assistant's conversation. Delivered
events retain provenance. Stale source requests fail. Resume/answer after a move
requires the current revision and explicit confirmation. Remembered slots are
rechecked; an inapplicable slot requires a valid operator choice.

Archive refuses unfinished runs, live workers, pending questions, queue tickets
and assistant requests/events. Eligible archive stops the assistant and retains
history, identity and slot definitions. Restore leaves it stopped; explicit Start
is required. Archived projects cannot acquire slots.

## Slack and Matrix bot mentions

Both listener handlers and existing worker connector ingress route bot mentions
through the same durable request admission. Ordinary worker replies retain their
connector path. A supervising request never replaces the worker's connection or
attaches several assistants to one thread.

Daemon ingress commits a raw-event inbox before acknowledging Slack envelopes.
Matrix commits the returned events and its account cursor in one transaction;
both survive a daemon restart. Pending events replay through normal authorization
and project admission, deduplicated by transport, conversation and message ID.
Successful intake erases the raw journal body while retaining the deduplication
key. A failed Slack username authorization lookup remains retryable in the inbox;
the sender is not admitted until lookup succeeds and the allowlist matches.
Definitive denials erase the raw body, and an allowed user ID needs no lookup.
A failed durable write leaves the remote event unacknowledged. The first
Matrix sync still establishes a baseline without treating old history as new
requests; a persisted cursor prevents a restart from establishing that baseline
again. Legacy host listeners retain mentions in this same inbox before Bolt
acknowledgement or their own Matrix cursor commit. Worker connectors keep their
existing forwarding-before-checkpoint behavior and connection ownership.
A server-reported limited timeline remains a visible transport gap.

Mention the bot followed by a project name and your message, for example
`@bot Example Alpha hi`. Leading names match case-insensitively on a complete
name followed by whitespace or the end of the message. The assistant receives
`hi`; the original message remains in its durable request for provenance.
`@bot hi` goes directly to General in an unbound thread. Unmatched words remain
message text; `Example Alphabet` does not select `Example Alpha`. These ordinary
requests need no internal project ID or selection token.

Slack removes a leading mention only when it matches the bot user returned by
the authenticated transport. Other-user mentions and a bot mention later in
ordinary text stay intact. If the identity lookup fails, admission remains
retryable without selecting a project. Matrix uses its configured account
identity and matching mention links.

Existing thread/project bindings and run homes remain authoritative. A plain
mention in a bound thread stays with its owner. A name cannot silently override
a different owner. Multiple matching names (such as `Example` and
`Example Alpha`) or conflicting ownership require explicit selection in the
source thread; only the originating sender can complete that choice. A named
archived project produces a source-thread refusal, never a General fallback.
No mention starts an assistant or changes connector ownership. Incoming message
identity deduplicates admission, notices and delivery. UI selection and recent
assistant activity never choose the destination.
DM replies retain their exact recorded assistant parent; they do not use name
matching or the General default when that parent's project cannot be resolved.

Queued requests retain project, source conversation/root, sender, message and
ownership revision. A stopped/unavailable/waiting assistant leaves the request
at that destination. Admission, draining, actions and replies recheck ownership.
A run move invalidates stale requests instead of silently redirecting them.
After rotation/recovery, an unfinished request can be delivered to the same
project's successor with its durable action receipts.

Taking pending delivers one external request at a time. Its durable context binds
the assistant credential's mutations even when the assistant omits `request_id`.
An explicit conflicting identity is refused. The context survives rotation and
remains attached after reply until the next pending take, which permits unrelated
work or delivers the next waiting request. Reads remain project-scoped. The
explicit spelling is useful for visibility but does not confer the binding:

```sh
lmer-ctl --request-id REQUEST_ID spawn develop 'issue-123' --repo-url https://forge.example/org/repo
lmer-ctl requests reply REQUEST_ID 'Result for this conversation'
```

When an authorized request spawns a same-project worker with its original source
thread attached, the completed spawn receipt records the exact ownership
transition caused by that attachment. The updated snapshot and completed receipt
are saved together. The assistant can then brief that worker and reply to the
original source. This exception requires the original snapshot, the newly
registered worker's actual owner and revision, and the current assistant
generation. Independent attachments, conflicting owners, moves and changed
revisions still refuse the request. Execution rechecks run before launch, brief
and source reply; a worker's own verdict does not count as the assistant reply.

An ownership refusal before effect leaves the request's delivery and completed
action receipts intact, with a concrete error. **Take pending** rechecks even an
already delivered request with no queued note. When no effect is uncertain, that
single take releases the invalid request and returns `request_unavailable` with
its original identity, retained action receipts and `reply_sent: false`. It does
not deliver another request in the same take. An explicit request ID is retained
and must match the active request; project and generation checks still apply.
Independent work, handoff and
explicitly authorized lifecycle recovery can then proceed without restarting
the assistant. Take pending again to handle the next request. Never repeat a
completed spawn to recover a blocked request.

Platform settings lists blocked requests, including older delivered records
whose stored error is empty and whose only action is a completed spawn. It
refreshes with platform state and **Refresh requests**, shows the reason and
retained receipts, and offers **Release blocked request** when safe. The operator
must supply a reason; the API rechecks eligibility at execution. The equivalent
command is:

```sh
lmer-ctl --workspace-id ID requests release REQUEST_ID 'Retain the existing worker and allow independent work'
```

Release is idempotent and preserves the request, source binding and receipts. It
sends nothing, detaches nothing and grants no access to another project. A fresh
source-thread request can authorize new work. Uncertain action or reply outcomes
cannot use this release path; inspect them without blind retries. Existing
operator parking for uncertain replies remains separate, as described below.

Action receipts identify request, method, route and payload, excluding redundant
workspace/request identity fields. Existing receipts using those fields remain
recognized. Repeating a
completed action returns its recorded response. An interrupted action whose
outcome is unknown remains uncertain and is refused on replay. Replies are sent
only to the recorded source thread; completed replies are idempotent. Unknown
external send outcomes are retained visibly and never automatically retried.
Generic thread posts under this context use the same durable source-thread reply;
another thread is refused. Terminal tickets/frames cannot bypass request receipts:
use the HTTP input route or CLI for request-driven input. Assistant settings
mutations also receive action receipts; inbox draining and replies use their own
durable transitions.

Spawn validation, capacity, slot and ownership preflight refusals are recorded as
`refused`, so the unchanged operation can be retried after its cause is fixed.
The durable effect boundary immediately precedes the child-process launch, not
an HTTP status code; preparation failures before that point cannot launch work.
Reply lookup failures before any post leave the request deliverable. After an
effect may have begun, exceptions of any HTTP status remain uncertain. Operations
without an audited preflight boundary are conservatively uncertain on failure.
Refused actions do not block archive once the request is answered; uncertain
actions still do. Operators must inspect the source conversation/action outcome before deciding
what new work to request. There is no automatic retry/reconciliation button for
uncertain external outcomes. They remain visible in Platform settings even after
a source-thread reply, and unresolved action outcomes block archive.

An uncertain reply retains the project's execution context until an operator
explicitly parks it. In Platform settings, choose **Park uncertain reply** and
record a reason, or use an operator-authenticated CLI:

```sh
lmer-ctl --workspace-id ID requests park REQUEST_ID 'Delivery remains unknown; continue independent work'
```

Parking atomically records operator provenance, time and reason and releases
only that request's context in its recorded project. It remains available if the
source thread's ownership changes, because it performs no action in that thread;
replies and request actions still reject changed ownership. The next pending take may deliver independent work,
including after restart or rotation. The original request, source, error and
action receipts remain intact; parking neither confirms success nor retries a
send. Repeating parking returns its original disposition. Assistant credentials
cannot park requests. Parked uncertainty stays visible and continues to block
archive. This path applies only to uncertain replies, including an interrupted
send; it does not authorize retrying uncertain actions or change notification policy.

During assistant rotation, the chat retains its composer and staged attachments,
including polls between the old session's exit and the successor's launch.
Sending pauses until the successor is ready. Stopped/queued projects instead
show read-only history with a separate editable project draft.

The generic cross-project excerpt and relay routes require operator
authentication at the API boundary. An assistant prompt or supplied
source/destination is not a grant. An operator can fetch the bounded excerpt
or relay supplied text with named provenance; neither exposes another project's
memory/history or grants its run controls. The separately authorized native
peer workflow permits current project assistants to read bounded peer standing
instructions and exchange explicitly addressed messages, under the identity,
provenance and receipt limits described in the peer sections above.

## Validation boundaries

Project tests use isolated stores, real HTTP/admission paths, real loopback
question buses, concurrent threads and real disposable child processes. Slack
and Matrix handlers/connectors are exercised with fixture remote clients; this
checks their integration with routing and recovery without claiming real remote
API delivery. The Chromium probe in `web/tests/browser/projects.py` serves the
built UI from an isolated platform API and writes screenshots/results to a
chosen output directory.

No test here restarts the installation's daemon, changes its project/slot/service
assignments, or proves a live container/runtime restart. Full external transport
and installation migration remain deployment acceptance work. The independent
session-ID allocator collision tracked as #519 is unchanged.


## Operator UI controls

All projects offers the same confirmed Forget all completed action as a selected
project, over its complete visible project scope. Eligibility is rechecked before
each request; live and held runs are excluded. Each request retains the run's
actual project. Changing views closes the old selection rather than carrying it
into a different scope.

Global header actions expose labels on pointer hover and keyboard focus. Sidebar
projects collapse independently; ordinary runs appear directly below the project
header. Attention rows precede ordinary rows without subgroup headings; status
icons and a divider distinguish the groups when both exist. Assistant selection
includes its project, so equal assistant names
in different projects do not highlight together. The header's project switcher
names the selected project; run detail repeats the project as a colored badge
only if the header does not identify the same destination.

Fleet views include read-only Service slots summaries. All projects shows held
slots with the owning project names, including shared-service conflicts. A
selected project shows both its retained holdings and slots assigned to it.
`Available now` requires a free slot assigned to that active project; occupied,
overlapping, misconfigured and offline services remain unavailable. Removing
an assignment does not hide a slot that the project still holds. Unknown owners
are labelled unassigned rather than inferred from repository names.

The summary refreshes with the existing fleet poll and shows failed or pending
reads without claiming availability. The project summary's management link opens
and focuses Service slots in Platform settings; allocation and editing controls
remain there. These summaries do not reserve capacity or change assignments.
Worker capacity is still checked separately when starting a run.

Project settings starts with Standing instructions collapsed. Expanding it permits
editing through the existing scoped instruction endpoint; Save project is kept
at the top of the form. Collapsing preserves the draft. Unedited, unavailable or
still-loading instructions are never posted as empty text when other settings
are saved. Existing non-empty-text and length validation still applies, and a
refused write leaves the edit visible for correction. The assistant chat continues
to show a read-only copy.

Archived project rows and Restore stopped use the stored project color. Fleet
worker and assistant capacity use adjacent integer inputs; existing limits,
validation, defaults and save semantics are unchanged.
