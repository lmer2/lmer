// Thin API client.
//
// Authentication is the browser's: the daemon answers an unauthenticated request
// with `WWW-Authenticate: Basic`, the browser prompts once, and then attaches the
// credentials to every same-origin request itself. So there is no token handling
// here at all — which is the point, since a token in JS is a token in the DOM.
// (A login form exchanging the secret for an httpOnly cookie is the eventual
// design, spec §10.5; it is not needed to make the UI work.)

function workspacePath(path, workspaceId) {
  return workspaceId ? `${path}${path.includes('?') ? '&' : '?'}workspace_id=${encodeURIComponent(workspaceId)}` : path
}

function runQuery(fields) {
  return new URLSearchParams(Object.fromEntries(Object.entries(fields).filter(([, value]) => value != null)))
}

const jsonHeaders = { 'Content-Type': 'application/json' }

async function request(path, options = {}) {
  const response = await fetch(path, { credentials: 'same-origin', ...options })
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    let code = null
    let errorBody = null
    try {
      const body = await response.json()
      errorBody = body
      // Most routes answer with `detail` as a sentence. The resume route answers
      // with {code, message}, because two of its refusals are requests for one
      // more field rather than failures — a caller that had to match on the
      // sentence to tell those apart would break the first time the sentence was
      // improved (lmer_platform.resume.ResumeError). The prose is still what gets
      // shown either way: every refusal names both the reason and the way through.
      if (body?.detail?.message) {
        detail = body.detail.message
        code = body.detail.code || null
      } else if (body && body.detail) {
        detail = body.detail
      }
    } catch {
      // Non-JSON error body (a 401 challenge page, say) — the status is enough.
    }
    const error = new Error(detail)
    error.status = response.status
    // Null for every route that answers with a sentence, so a caller branching on
    // a code needs no knowledge of which routes can produce one.
    error.code = code
    error.source = errorBody?.detail?.source || null
    error.decisions = errorBody?.detail?.decisions || []
    throw error
  }
  return response.json()
}

export function fetchState() {
  return request('api/state')
}

export function fetchProjects() {
  return request('api/projects')
}

export function createProject(name) {
  return request('api/projects', { method: 'POST', headers: jsonHeaders, body: JSON.stringify({ name }) })
}

export function updateProject(workspaceId, changes) {
  return request(`api/projects/${encodeURIComponent(workspaceId)}`, {
    method: 'PATCH', headers: jsonHeaders, body: JSON.stringify(changes),
  })
}

export function archiveProject(workspaceId, restore = false) {
  return request(`api/projects/${encodeURIComponent(workspaceId)}/${restore ? 'restore' : 'archive'}`, { method: 'POST' })
}

export function moveRun(run, destinationId) {
  return request('api/runs/move', {
    method: 'POST', headers: jsonHeaders,
    body: JSON.stringify({ ...runFields(run), destination_id: destinationId }),
  })
}

export function fetchPlatformSettings() {
  return request('api/settings')
}

export function setPlatformSettings(changes) {
  return request('api/settings', { method: 'POST', headers: jsonHeaders, body: JSON.stringify(changes) })
}

export function fetchConversationRequests(workspaceId) {
  return request(workspacePath('api/assistant/requests', workspaceId))
}

export function parkConversationRequest(identity, workspaceId, reason) {
  return request(`api/assistant/requests/${encodeURIComponent(identity)}/park`, {
    method: 'POST', headers: jsonHeaders, body: JSON.stringify({ workspace_id: workspaceId, reason }),
  })
}

export function releaseConversationRequest(identity, workspaceId, reason) {
  return request(`api/assistant/requests/${encodeURIComponent(identity)}/release`, {
    method: 'POST', headers: jsonHeaders, body: JSON.stringify({ workspace_id: workspaceId, reason }),
  })
}

export function fetchHealth() {
  return request('api/health')
}

export function rescan() {
  return request('api/rescan', { method: 'POST' })
}

export function prune() {
  return request('api/prune', { method: 'POST' })
}

// The validator's view of the work repo (#374): `{form, tier, document, buckets,
// note}`, where `document` is exactly what `lmer-schema validate --json` prints.
// Fleet-wide, not a project's: the work repo is shared by every workspace, so
// this carries no workspace and the daemon binds it to none.
//
// The daemon caches the document per mirror HEAD for a bounded time, because
// staleness findings move with the clock at an unchanged HEAD (astra PR7).
// `refresh` is the way round that cache — the explicit re-validate control —
// and is sent only when asked for, so the ordinary read stays one cheap hit.
export function fetchWorkRepoSchema({ refresh = false } = {}) {
  return request(refresh ? 'api/workrepo/schema?refresh=1' : 'api/workrepo/schema')
}

export function fetchCandidates() {
  return request('api/runs/candidates')
}

export function adoptRun(run) {
  return request('api/runs/adopt', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify(runFields(run)),
  })
}

export function forgetRun(run) {
  return request('api/runs/forget', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify(runFields(run)),
  })
}

// What a run is about: this orchestrator's own title and description for it
// (T52). Read by query string rather than a path, because a project is
// `group/subgroup` and a path parameter would have to be escaped by every caller
// to survive the slash it contains.
//
// The reply carries more than the two fields, and both extras are load-bearing:
// `limits` is the daemon's own bounds so a counter cannot drift from what will
// actually be refused, and `local`/`note` say the part nobody would otherwise be
// told — this is platform state, so it belongs to this orchestrator and not to
// the run. It is invisible to anyone else's fleet view and it goes when the run
// is forgotten.
export function fetchRunMeta(run) {
  const query = runQuery(runFields(run))
  return request(`api/runs/meta?${query}`)
}

// Both fields are sent every time by the one view that calls this, but the route
// treats an omitted field as "leave it alone" and an empty string as "clear it" —
// which is what lets the orchestrator agent set a title without deleting a
// description somebody wrote. The reply is what was *stored*, not what was sent:
// the title comes back collapsed to the single line it will be shown as.
export function setRunMeta(run, { title, description }) {
  return request('api/runs/meta', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ ...runFields(run), title, description }),
  })
}

// Which runs belong together (T53). Read by query string for fetchRunMeta's
// reason, and the reply carries the same two extras — `limits` so a full switcher
// is reported from the daemon's own cap, and `local`/`note` because relations are
// platform state and do not travel with the runs.
//
// Every entry says whether this orchestrator tracks the run it names. An untracked
// one is ordinary, not an error: relating a run before adopting it is allowed, and
// forgetting a run leaves its relations alone. It is a key to show, never a run to
// open — the shell selects out of the fleet, so opening one would land on nothing.
export function fetchRunRelations(run) {
  const query = runQuery(runFields(run))
  return request(`api/runs/relations?${query}`)
}

// Every launch of the run's `_runs/<slug>/` group (#374), in start order. Read
// by query string for fetchRunMeta's reason, and with the same fields: the
// project context binds the read the way it binds the run's other reads (#542).
//
// Each row in the reply carries its OWN `workspace_id`/`home_revision`/
// `confirm_project`, which is what a resume of that launch must post — never the
// row this was asked for, because two launches of one group can live in two
// projects (astra PR6). A schema-1 row has no launches to list; the view that
// calls this is not mounted for one, so a legacy fleet never sends it.
export function fetchRunSessions(run) {
  const query = runQuery(runFields(run))
  return request(`api/runs/sessions?${query}`)
}

// Relating is symmetric and the daemon stores it once, so there is one call rather
// than one per direction: both runs carry it afterwards, either order names the
// same relation, and doing it twice is a no-op the reply reports (`created`). The
// second run travels as an object of the same key fields as the first (runRef,
// uid included when it has one), because what is being related is a run and this
// API has one spelling for that.
//
// The reply is the *subject* run's relations as they now are, which is what the
// view renders — nothing is patched locally.
export function relateRun(run, related) {
  return request('api/runs/relate', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ ...runFields(run), related: runRef(related) }),
  })
}

// The other half, same body and same shape back. One entry is the relation, so
// this removes both directions at once and the order the runs are named in does
// not matter. It needs no adoption, which is what makes it the way to clear a
// relation naming a run this host no longer tracks.
export function unrelateRun(run, related) {
  return request('api/runs/unrelate', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ ...runFields(run), related: runRef(related) }),
  })
}

// Narrowed on purpose: the caller passes a fleet row or a relation entry, and both
// carry a good deal more than the fields that identify a run. Sending the whole
// object would put a whole run payload on the wire and invite the daemon to start
// reading fields the sender did not mean as a request.
//
// `uid` is the key of a schema-2 row (spec §6.1): its directory is named by the
// uid, so the slug alone cannot find it. A row without one — schema-1, or the
// daemon's `uid: null` — sends no `uid` key at all rather than a null, so its
// requests stay byte-identical to the ones every daemon already accepts.
// `undefined` is what makes that true on both paths: JSON.stringify drops the
// key and runQuery drops the parameter.
function runRef({ host, project, slug, uid }) {
  return { host, project, slug, uid: uid || undefined }
}

// The run plus the project fields (#542) every run-keyed write carries, in the
// order they have always been sent.
function runFields(run) {
  return { ...runRef(run), workspace_id: run.workspace_id, home_revision: run.home_revision }
}

// Answering is a *run* action, not a session one, and the route says so: the
// session that asked the question has already exited, so there is nothing to type
// into — the daemon starts a fresh session carrying the answer. Nothing is
// recorded when this resolves (the reply is a 202); the answer lands in the run's
// state a moment later, which the next fleet poll picks up.
export function answerRun(
  run, answer,
  { allowUnsignedSource, confirmProject = false, slot, repoUrl } = {},
) {
  const body = {
    ...runFields(run), answer,
    confirm_project: confirmProject || undefined,
    slot: slot || undefined,
  }
  // The daemon's `repo_url_required` refusal asks for this field: an adopted
  // schema-2 plain-project run has no tracked repo, so answering it needs the
  // transport URL. Same option `resumeRun` already serializes.
  if (repoUrl?.trim()) body.repo_url = repoUrl.trim()
  if (allowUnsignedSource) body.allow_unsigned_source = allowUnsignedSource
  return request('api/runs/answer', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify(body),
  })
}

// Continuing a run this orchestrator already tracks. Same shape as answerRun and
// for the same reason: the run has *exited*, so there is no session to type into
// and continuing it means starting one. Nothing is recorded when this resolves
// (a 202) — the session claims the run and prints its own resume brief a moment
// later, which the next fleet poll sees.
//
// All three options are optional and blank ones are omitted rather than sent
// empty. `taskdef` blank continues the run with the taskdef it recorded; naming
// another starts a sibling run against the same target, and the reply says which
// happened. `repoUrl` and `direction` are what the daemon's two "one more field"
// refusals ask for — see the `code` on a rejected request.
export function resumeRun(
  run, {
    taskdef, repoUrl, direction, allowUnsignedSource, confirmProject = false, slot,
  } = {},
) {
  const body = {
    ...runFields(run),
    confirm_project: confirmProject || undefined,
    slot: slot || undefined,
  }
  if (taskdef?.trim()) body.taskdef = taskdef.trim()
  if (repoUrl?.trim()) body.repo_url = repoUrl.trim()
  if (direction?.trim()) body.direction = direction.trim()
  if (allowUnsignedSource) body.allow_unsigned_source = allowUnsignedSource
  return request('api/runs/resume', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify(body),
  })
}

// What the host can offer the spawn form: taskdef ids and preset names it can
// actually see. Both lists are advisory — the work repo's taskdefs come off a
// mirror that may be stale or hold nothing for the project in question — so
// neither is the set of values the daemon accepts. The preset and agent fields
// therefore stay free text; the taskdef field renders the list as a menu
// (an operator decision — it is a static list) and keeps a switch to plain text
// for the names discovery cannot see, which is the same requirement met a
// different way. The route answers 200 with empty lists when it can enumerate
// nothing, so a caller never has to distinguish "none" from "could not tell".
//
// `target` and `repoUrl` are the spawn being composed, not a filter: the work
// repo's project-scoped taskdefs live under the run's host and project, so the
// daemon can only include that tier once it knows which repository this spawn
// names. Sending them is what makes the menu the menu for THIS spawn; omitting
// them answers for the daemon's own repository, which is what a spawn naming no
// repository would use anyway.
//
// `repo_url` in the reply is the exception to all of that: not a suggestion but
// the URL the daemon would itself use for a spawn that names none (its
// $LMER_REPO_URL, with any credential already stripped), so the form can
// prefill the field with the default it actually has. Null when there is none.
export function fetchSpawnOptions({ target = '', repoUrl = '' } = {}) {
  const query = new URLSearchParams()
  if (target) query.set('target', target)
  if (repoUrl) query.set('repo_url', repoUrl)
  const suffix = query.toString()
  return request(suffix ? `api/spawn-options?${suffix}` : 'api/spawn-options')
}

export function spawnSession(payload) {
  return request('api/sessions', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify(payload),
  })
}

// --- service slots (issue #256) ----------------------------------------------

// A patch of one slot, never a POST of the whole list: the daemon merges it into
// `config.json` as the file is at write time, so a slot added in an editor since
// this page loaded is not lost to a save here.
//
// `dryRun` runs every check and writes nothing, answering with the row the slot
// *would* have — the same route the save takes, which is what stops the form
// growing a second opinion about what makes a slot valid. `etag` travels as
// `If-Match`: the daemon recomputes it under its write lock and refuses (412,
// `slot_changed`) rather than overwriting an edit made behind this one.
export function createSlot(slot, { dryRun = false } = {}) {
  return request(`api/slots${dryRun ? '?dry_run=true' : ''}`, {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify(slot),
  })
}

// `confirm` on either verb is the operator saying yes to a change the daemon
// refused because a live session is in the slot — a removal, or a re-point that
// moves what that session is bound to. Absent by default, so the refusal is what
// an unthinking press gets.
export function updateSlot(
  name, changes, { etag, dryRun = false, confirm = false } = {},
) {
  const query = new URLSearchParams()
  if (dryRun) query.set('dry_run', 'true')
  if (confirm) query.set('confirm', 'true')
  const suffix = query.toString()
  return request(
    `api/slots/${encodeURIComponent(name)}${suffix ? `?${suffix}` : ''}`,
    {
      method: 'PUT',
      headers: { ...jsonHeaders, 'If-Match': etag || '' },
      body: JSON.stringify(changes),
    },
  )
}

export function removeSlot(name, { etag, confirm = false } = {}) {
  return request(
    `api/slots/${encodeURIComponent(name)}${confirm ? '?confirm=true' : ''}`,
    { method: 'DELETE', headers: { 'If-Match': etag || '' } },
  )
}

// --- one session's terminal --------------------------------------------------

// A negative offset reads the last |offset| bytes, so a terminal can attach to
// the end of a log of any size without first asking how big it is. The reply's
// `offset` is where that landed and `next_offset` is where the socket must
// continue from — the client never computes either from a byte count.
export function fetchSessionLog(sessionId, { offset = 0, limit } = {}) {
  const query = new URLSearchParams({ offset: String(offset) })
  if (limit) query.set('limit', String(limit))
  return request(`api/sessions/${encodeURIComponent(sessionId)}/log?${query}`)
}

// The socket's only credential. A WebSocket handshake carries no Authorization
// header and the browser does not apply the credentials it holds for the origin
// to an upgrade, so an authenticated POST mints a ticket instead — bound to one
// session, redeemed once, expiring in seconds. Single use is the part callers
// have to remember: every connection attempt, reconnects included, mints its own.
export function mintTtyTicket(sessionId) {
  return request(`api/sessions/${encodeURIComponent(sessionId)}/tty-ticket`, {
    method: 'POST',
  })
}

// Resolved against the document, like every other path here, so the UI keeps
// working behind a reverse proxy on a subpath; only the scheme is rewritten.
export function ttySocketUrl(sessionId, { ticket, offset }) {
  const url = new URL(
    `api/sessions/${encodeURIComponent(sessionId)}/tty`, window.location.href,
  )
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  url.searchParams.set('ticket', ticket)
  if (Number.isFinite(offset)) url.searchParams.set('offset', String(offset))
  return url.toString()
}

// --- one run's conversation ---------------------------------------------------

// The chat view's read half. A negative `since` reads the last |since| messages,
// the same convention as the log's negative offset: a cold open wants the end of
// a long conversation without a round trip to learn how long it is. The reply's
// `cursor` is what the next poll passes as `since` — computing it from a message
// count would skip or repeat the moment one message was filtered out.
export function fetchSessionMessages(sessionId, { since = 0, limit } = {}) {
  const query = new URLSearchParams({ since: String(since) })
  if (limit) query.set('limit', String(limit))
  return request(`api/sessions/${encodeURIComponent(sessionId)}/messages?${query}`)
}

export function fetchAssistantHistory(sessionId, { since = -40, before, limit = 40, view = 'conversation' } = {}) {
  const query = new URLSearchParams({ since: String(since), limit: String(limit), view })
  if (before) query.set('before', before)
  return request(`api/assistant/history/${encodeURIComponent(sessionId)}?${query}`)
}

// The chat view's write half, and pointedly a different route from the read half:
// this types into the live session's control plane, while what comes back arrives
// through the transcript whenever the harness writes it. `appendNewline` is the
// control plane's "and press Enter", which a TUI reads as submit — without it the
// text sits in the session's prompt unsent. `sanitize` asserts that the payload
// is prose intended to steer the session, rather than raw terminal keystrokes.
// That lets the supervisor defuse a message a TUI would otherwise run as a shell
// command. Off unless a caller says so, so raw paths keep their old semantics.
//
// Since the unified channel (issue 407) a *submitted* message is a record on the
// session's bus, so every connected channel sees it. The keystroke form —
// `appendNewline: false`, text left in the prompt unsent — is not a message
// anyone else should see and keeps the raw `/input` route. `sanitize` is
// accepted for the callers that still pass it; the bus defuses by construction.
export function sendSessionInput(
  sessionId, data, { appendNewline = true, sanitize = false, workspaceId } = {},
) {
  if (!appendNewline) {
    const body = { data, append_newline: false, workspace_id: workspaceId }
    if (sanitize) body.sanitize = true
    return request(`api/sessions/${encodeURIComponent(sessionId)}/input`, {
      method: 'POST',
      headers: jsonHeaders,
      body: JSON.stringify(body),
    })
  }
  return request(`api/sessions/${encodeURIComponent(sessionId)}/comms`, {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ kind: 'message', text: data, workspace_id: workspaceId }),
  })
}

// --- files you hand a session (issue #246) ------------------------------------

// The composer's other half. `/input` types bytes at a PTY and has nowhere for a
// file in it, so an attachment is stored host-side first and the message that
// follows names the path it was mounted at — which is what the reply's
// `reference` is: the exact line to include, composed by the daemon so the two
// ends cannot spell it differently.
//
// Base64 in a JSON body rather than a multipart form: the daemon would need a
// packaged dependency for multipart, and a screenshot is small enough that the
// 33% is not what matters. FileReader gives a data URL, so the header before the
// comma is dropped here rather than in every caller.
//
// Nothing is sent to the session by this call, and nothing calls it until the
// operator sends: a file staged and then removed from the tray has been uploaded
// nowhere.
//
// What this does *not* claim, because it was claimed here and was not true: a
// send that uploads two files and is then refused on the second leaves the first
// on the host with no message naming it (!272 review). The composer remembers
// that reply against the tray entry, so sending again names the file already
// stored instead of storing a second copy — but the copy from the abandoned send
// is on the host, and only the session it was stored for can remove it.
export function uploadSessionFile(sessionId, { name, data, workspaceId }) {
  return request(`api/sessions/${encodeURIComponent(sessionId)}/uploads`, {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ name, data, workspace_id: workspaceId }),
  })
}

// Where the browser reads one back — an `<img>` src, so it is a URL rather than a
// fetch. Resolved against the document like every other path here, so the UI keeps
// working behind a reverse proxy on a subpath; the browser attaches the same
// credentials it holds for the origin, which is what makes this route no more open
// than the rest of the API.
export function sessionUploadUrl(sessionId, name) {
  return `api/sessions/${encodeURIComponent(sessionId)}/uploads/${encodeURIComponent(name)}`
}

// --- one session's ask channel -------------------------------------------------

// The live counterpart of answerRun. That one respawns a run that stopped on a
// question; this one drops a reply into a directory a *running* session is
// polling, so nothing is spawned and the answer lands in seconds. The fleet
// payload already carries each waiting run's open questions, so this read is for
// the detail view — where the session's progress notes live too.
export function fetchSessionAsk(sessionId) {
  return request(`api/sessions/${encodeURIComponent(sessionId)}/ask`)
}

// The question id is part of the URL, not the body: a reply is bound to the
// question it answers, so it can never be applied to whatever happens to be open
// when it arrives. A 409 means someone already answered it — answers are not
// overwritten, because the session may have acted on the first one — and a 410
// means the session that would have read the reply has exited (T94), which no
// retry fixes: the channel belongs to that one session, so resuming the run is
// what the operator can still do.
export function answerSessionQuestion(sessionId, questionId, answer, workspaceId) {
  return request(
    `api/sessions/${encodeURIComponent(sessionId)}/ask/${encodeURIComponent(questionId)}/answer`,
    { method: 'POST', headers: jsonHeaders, body: JSON.stringify({ answer, workspace_id: workspaceId }) },
  )
}

// --- the supervising session (spec §8) ---------------------------------------

// Is one running, and what is it. The body every assistant route answers with, so
// a client renders one shape however it arrived at it — and a read that starts
// nothing: the route is deliberately not the module's `ensure_running` (api.py),
// because opening a drawer must not cost a session slot.
// The chat threads one session is mirrored into (issue #412). Three calls over
// one record: the daemon runs a connector process per connection, so connecting
// is a write and disconnecting is a delete — neither restarts anything.
//
// Nothing here patches a local copy of the reply: the fleet poll (and the
// assistant status, for uber lmer) is what the icons render from, and one source
// for that is what keeps every copy of the icons on screen agreeing — which is
// also why there is no client for the GET: nothing would read it.
// The link determines transport; a separate transport field could disagree.
export function connectSessionThread(sessionId, permalink, workspaceId) {
  return request(`api/sessions/${encodeURIComponent(sessionId)}/threads`, {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ permalink, workspace_id: workspaceId }),
  })
}

// Open a new thread in a channel and connect the session to it (#429). The same
// route as a connect, and each body carries exactly one of the two: a permalink
// names a thread that exists, a channel a place to make one.
export function openSessionThread(sessionId, channel, title, workspaceId) {
  return request(`api/sessions/${encodeURIComponent(sessionId)}/threads`, {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ channel, title, workspace_id: workspaceId }),
  })
}

export function disconnectSessionThread(sessionId, threadId, workspaceId) {
  return request(
    workspacePath(`api/sessions/${encodeURIComponent(sessionId)}/threads/${encodeURIComponent(threadId)}`, workspaceId),
    { method: 'DELETE' },
  )
}

// A thread the run is pointed at rather than connected to (#414): saving one
// starts nothing — the run is told once and reads or posts it on demand.
export function addSessionReference(sessionId, permalink, title, workspaceId) {
  return request(`api/sessions/${encodeURIComponent(sessionId)}/references`, {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ permalink, title, workspace_id: workspaceId }),
  })
}

export function removeSessionReference(sessionId, referenceId, workspaceId) {
  return request(
    workspacePath(`api/sessions/${encodeURIComponent(sessionId)}/references/${encodeURIComponent(referenceId)}`, workspaceId),
    { method: 'DELETE' },
  )
}

export function fetchAssistant(workspaceId) {
  return request(workspacePath('api/assistant', workspaceId))
}

// Starting one by hand. The daemon supervises it and normally starts it itself
// (T63), so this is the way past a start that has not happened yet rather than
// the usual path. The reply is the reconciled status and not an acknowledgement —
// it returns once the session is spawned and in the registry. A 409 means one is
// already running, which is not a failure to report: re-read the status.
export function startAssistant(workspaceId) {
  return request(workspacePath('api/assistant/start', workspaceId), { method: 'POST' })
}

// Stopping one by hand, and deliberately not `POST api/sessions/{id}/exit`: that
// verb refuses the supervising session outright, because a stop also has to clear
// the pointer to it and record why it ended. The reply is `{stopped, reason}` on
// top of the same reconciled status, so a caller renders the state it arrived at
// without a second read — and `stopped: false` is a normal answer meaning nothing
// was running, not a failure.
//
// No `reason` is sent: the route defaults to `operator`, which is the true one for
// every call this UI makes, and it has to stay distinguishable in state from a
// `rotation` — otherwise "why did the last one end" is unanswerable afterwards.
// Nothing here restarts one: the daemon's supervisor does that when it is watching,
// and `startAssistant` is what brings one back when it is not.
export function stopAssistant(workspaceId) {
  return request(workspacePath('api/assistant/stop', workspaceId), { method: 'POST' })
}

// Replacing the incarnation with a fresh context window — one call rather than a
// stop and a start, because two would leave a window where another start lands
// between them and wins the generation counter. It starts one when nothing is
// running, so it is also the verb for an incarnation that has already died, which
// is what makes it the safe primary control: it always leaves something up.
//
// The reply is the reconciled status of the *new* one, and the session id in it is
// the whole of what "a fresh window" means to a client — the conversation follows
// that id, so no reload is needed for the chat to be talking to the replacement.
//
// No handoff is sent. The note on record travels forward untouched (the daemon
// carries it when the field is omitted), and the party entitled to write a fresh
// one is the incarnation being replaced, since it is the only one that knows what
// it was doing. Standing orders are not a handoff and survive either way: nothing
// consumes them and every incarnation reads them at startup.
export function rotateAssistant(workspaceId) {
  return request(workspacePath('api/assistant/rotate', workspaceId), { method: 'POST' })
}

export function fetchAssistantInstructions(workspaceId) {
  return request(workspacePath('api/assistant/instructions', workspaceId))
}

export function setAssistantInstructions(workspaceId, instructions) {
  return request(workspacePath('api/assistant/instructions', workspaceId), {
    method: 'POST', headers: jsonHeaders, body: JSON.stringify({ instructions }),
  })
}

// How the NEXT incarnation will be run (issue #234): model/harness/preset/agents,
// each with the layer that decided it (env / config.json / default) and the
// config.json layer's own value beside it. The `source` is the honest half of a
// settings screen — an export shadows what the screen persists, and a screen that
// appears to have no effect is exactly the bug this field exists to not be. What
// the *running* incarnation was launched with is on `fetchAssistant()` instead
// (`settings`), because that is a fact about the session, not the configuration;
// the two differing is the normal state between a save and the next restart.
export function fetchAssistantConfig() {
  return request('api/assistant/config')
}

// Persist launch settings into config.json — a patch of exactly the keys named;
// null (or an emptied field's '') clears one so the layer below shows through.
// Nothing restarts: the running incarnation keeps its context window, and the
// restart verb is the way to apply what this stored. The reply is the same shape
// as the read, re-resolved — so a save under a shadowing export comes back still
// saying `source: 'env'`, which the dialog surfaces instead of reporting success.
export function setAssistantConfig(changes) {
  return request('api/assistant/config', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify(changes),
  })
}

// There is deliberately no client here for `POST api/assistant/pending`, and that
// absence is the decision (T31/T69): the route is a destructive *take* — the
// digests the daemon spooled are handed to the caller and cleared — and the one
// caller entitled to it is the supervising session itself, which polls it over
// this same API. A peek from this UI would eat digests its own supervisor has not
// read, silently and with nothing to replay them from. So the drawer shows the
// `pending` count off the status instead, which is what that number is on the
// status for, and nothing on this side can drain the queue.

// Log bytes travel base64-encoded, in both the REST reply and the socket's data
// frames, because a chunk boundary regularly splits a UTF-8 sequence or an
// escape: decoding anywhere but the terminal emulator turns those into U+FFFD
// and corrupts the stream permanently. So this yields bytes, never a string —
// xterm's write() takes a Uint8Array and does its own decoding across writes.
export function decodeLogData(encoded) {
  const binary = atob(encoded || '')
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index)
  }
  return bytes
}
