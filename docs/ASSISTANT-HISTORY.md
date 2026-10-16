# Assistant conversation history

For named workspaces, project-bound routing, capacity and General migration, see
[Projects](PROJECTS.md).

The existing Ratchet conversation survives rotation, page reload, and daemon
restart. A generation keeps its original session identity. Its first displayed
row explains the rotation and distinguishes the retained archive from the new
assistant's context. The conversation view remains the default; the existing
transcript preference reveals working transcript rows alongside it.

## Storage and ownership

The host stores explicit predecessor links and retained assistant communications
in `assistant-history.sqlite3` under its private platform state directory. This
archive is separate from the rotating fleet communications journal. Successful
assistant lifecycle publication adds a generation to the same conversation;
worker run identities and arbitrary sessions are never used to infer membership.
The database and its directory are owner-only. Credentials are scrubbed on
retention and again by the transcript presenter.

Publication also retains a credential fingerprint bound to the generation;
the archive stores no credential value. Authentication carries that immutable
session identity into reply recording. A reply authenticated before rotation
therefore stays with its original generation even if posting finishes afterward.
Legacy incumbents acquire a binding before their credential is replaced or
revoked. An unattributed launch cannot inherit a stale credential's identity.
If ownership cannot be recovered, the external post still happens once and the
response reports `pane_recorded: false`; it is never assigned to the current
generation by guesswork.

Messages carry `session` and `generation` independently of the transcript
filename. Attachment reads use that original session's recorded upload-store
association after registry removal. Existing assistant upload storage remains
host-owned; deleting or renaming an attachment can make an old link unavailable.
The archive does not copy attachment contents into conversation records.

On an upgrade, only the explicitly known incumbent can be linked. Earlier
unlinked generations and records already retired from the fleet journal cannot
be reconstructed reliably. The oldest available generation reports that gap.
The optional transcript view also reports missing or capped transcript sources.

## Authenticated reads

`GET /api/assistant/history/{session_id}` returns one bounded generation page.
Its parameters are `limit` (1–500, default 40), `since` (negative values select a
recent tail), optional `before` (a stable message id for an earlier page), and
`view` (`conversation` or `transcript`). Each response has:

- `messages`, with stable ids, original session/generation, provenance and text;
- `from`, `cursor` and `total`, describing positions in this generation/view;
- `history`, with the conversation identity and explicit `previous_session`;
- `live`, transcript `sessions`, and any availability `note`.

A missing stable anchor is reported instead of silently jumping to another
exchange. Follow `history.previous_session` only after reaching the beginning
of the current generation. The browser loads a small recent window, reconciles
the active generation, and pages backward on request. Each poll also reconciles
one loaded predecessor's tail, cycling through predecessors so late replies
become visible without a reload. Each request reads at most 500 rows, and paging
backward cannot rewind a generation's tail cursor. Normal conversation pages
read bounded database slices; they do not parse old working transcripts. The
optional transcript view reuses the existing normalizer for one generation at
a time and retains its source limits, including at most 5,000 retained
communications. When that view reaches the limit, its notice directs the reader
to the conversation endpoint, where later public messages remain pageable.

`GET /api/sessions/{session_id}/messages` also remains available for recorded
assistant generations after their registry entries are removed. It returns the
user and assistant transcript, including merged communications, through the
existing bounded transcript API. It does not join unrelated run sessions.

## Recovering missing conversational context

`GET /api/assistant/handoff` includes a `previous_session` reference with its
session id, generation, `messages_url`, and `history_url`. Merely reading the
handover reads no transcript. Normal startup still uses the handover and standing
orders; rotation does not inject old exchanges into the new context.

The orchestrate instructions direct the assistant to consult relevant recent
pages only when a missing exchange matters to the reply or action. Both sides
of the exchange are read. Missing, unreadable or inconclusive history is
acknowledged and only the missing context is requested. The latest user
direction takes precedence and completed historical actions are not replayed.
There is no recall button or per-turn full-history read.

Only transcript and ordinary communications records are eligible sources.
Private input excluded from those records is not reconstructed from private
files, attachment bodies, terminal scrollback, or other side channels. This
feature does not implement Send Privately or change its exclusion contract.
