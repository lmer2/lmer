// Thin API client.
//
// Authentication is the browser's: the daemon answers an unauthenticated request
// with `WWW-Authenticate: Basic`, the browser prompts once, and then attaches the
// credentials to every same-origin request itself. So there is no token handling
// here at all — which is the point, since a token in JS is a token in the DOM.
// (A login form exchanging the secret for an httpOnly cookie is the eventual
// design, spec §10.5; it is not needed to make the UI work.)

const jsonHeaders = { 'Content-Type': 'application/json' }

async function request(path, options = {}) {
  const response = await fetch(path, { credentials: 'same-origin', ...options })
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    let code = null
    try {
      const body = await response.json()
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
    throw error
  }
  return response.json()
}

export function fetchState() {
  return request('api/state')
}

export function rescan() {
  return request('api/rescan', { method: 'POST' })
}

export function prune() {
  return request('api/prune', { method: 'POST' })
}

export function fetchCandidates() {
  return request('api/runs/candidates')
}

export function adoptRun({ host, project, slug }) {
  return request('api/runs/adopt', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ host, project, slug }),
  })
}

export function forgetRun({ host, project, slug }) {
  return request('api/runs/forget', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ host, project, slug }),
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
export function fetchRunMeta({ host, project, slug }) {
  const query = new URLSearchParams({ host, project, slug })
  return request(`api/runs/meta?${query}`)
}

// Both fields are sent every time by the one view that calls this, but the route
// treats an omitted field as "leave it alone" and an empty string as "clear it" —
// which is what lets the orchestrator agent set a title without deleting a
// description somebody wrote. The reply is what was *stored*, not what was sent:
// the title comes back collapsed to the single line it will be shown as.
export function setRunMeta({ host, project, slug }, { title, description }) {
  return request('api/runs/meta', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ host, project, slug, title, description }),
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
export function fetchRunRelations({ host, project, slug }) {
  const query = new URLSearchParams({ host, project, slug })
  return request(`api/runs/relations?${query}`)
}

// Relating is symmetric and the daemon stores it once, so there is one call rather
// than one per direction: both runs carry it afterwards, either order names the
// same relation, and doing it twice is a no-op the reply reports (`created`). The
// second run travels as an object of the same three fields as the first, because
// what is being related is a run and this API has one spelling for that.
//
// The reply is the *subject* run's relations as they now are, which is what the
// view renders — nothing is patched locally.
export function relateRun({ host, project, slug }, related) {
  return request('api/runs/relate', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ host, project, slug, related: runRef(related) }),
  })
}

// The other half, same body and same shape back. One entry is the relation, so
// this removes both directions at once and the order the runs are named in does
// not matter. It needs no adoption, which is what makes it the way to clear a
// relation naming a run this host no longer tracks.
export function unrelateRun({ host, project, slug }, related) {
  return request('api/runs/unrelate', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ host, project, slug, related: runRef(related) }),
  })
}

// Narrowed on purpose: the caller passes a fleet row or a relation entry, and both
// carry a good deal more than the three fields that identify a run. Sending the
// whole object would put a whole run payload on the wire and invite the daemon to
// start reading fields the sender did not mean as a request.
function runRef({ host, project, slug }) {
  return { host, project, slug }
}

// Answering is a *run* action, not a session one, and the route says so: the
// session that asked the question has already exited, so there is nothing to type
// into — the daemon starts a fresh session carrying the answer. Nothing is
// recorded when this resolves (the reply is a 202); the answer lands in the run's
// state a moment later, which the next fleet poll picks up.
export function answerRun({ host, project, slug }, answer) {
  return request('api/runs/answer', {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ host, project, slug, answer }),
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
  { host, project, slug }, { taskdef, repoUrl, direction } = {},
) {
  const body = { host, project, slug }
  if (taskdef?.trim()) body.taskdef = taskdef.trim()
  if (repoUrl?.trim()) body.repo_url = repoUrl.trim()
  if (direction?.trim()) body.direction = direction.trim()
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

// The chat view's write half, and pointedly a different route from the read half:
// this types into the live session's control plane, while what comes back arrives
// through the transcript whenever the harness writes it. `appendNewline` is the
// control plane's "and press Enter", which a TUI reads as submit — without it the
// text sits in the session's prompt unsent. `sanitize` asserts that the payload
// is prose intended to steer the session, rather than raw terminal keystrokes.
// That lets the supervisor defuse a message a TUI would otherwise run as a shell
// command. Off unless a caller says so, so raw paths keep their old semantics.
export function sendSessionInput(
  sessionId, data, { appendNewline = true, sanitize = false } = {},
) {
  const body = { data, append_newline: appendNewline }
  if (sanitize) body.sanitize = true
  return request(`api/sessions/${encodeURIComponent(sessionId)}/input`, {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify(body),
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
export function uploadSessionFile(sessionId, { name, data }) {
  return request(`api/sessions/${encodeURIComponent(sessionId)}/uploads`, {
    method: 'POST',
    headers: jsonHeaders,
    body: JSON.stringify({ name, data }),
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
export function answerSessionQuestion(sessionId, questionId, answer) {
  return request(
    `api/sessions/${encodeURIComponent(sessionId)}/ask/${encodeURIComponent(questionId)}/answer`,
    { method: 'POST', headers: jsonHeaders, body: JSON.stringify({ answer }) },
  )
}

// --- the supervising session (spec §8) ---------------------------------------

// Is one running, and what is it. The body every assistant route answers with, so
// a client renders one shape however it arrived at it — and a read that starts
// nothing: the route is deliberately not the module's `ensure_running` (api.py),
// because opening a drawer must not cost a session slot.
export function fetchAssistant() {
  return request('api/assistant')
}

// Starting one by hand. The daemon supervises it and normally starts it itself
// (T63), so this is the way past a start that has not happened yet rather than
// the usual path. The reply is the reconciled status and not an acknowledgement —
// it returns once the session is spawned and in the registry. A 409 means one is
// already running, which is not a failure to report: re-read the status.
export function startAssistant() {
  return request('api/assistant/start', { method: 'POST' })
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
export function stopAssistant() {
  return request('api/assistant/stop', { method: 'POST' })
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
export function rotateAssistant() {
  return request('api/assistant/rotate', { method: 'POST' })
}

// The operator's standing orders for uber lmer (T87). A read and only a read: the
// write path is the chat, which the operator chose over a settings screen — they
// state a rule, uber lmer confirms the wording and stores the document, and this UI
// shows it so the rule can be checked without asking. Two writers of the same prose
// would be a merge nobody asked for, and the panel exists because the chat is not a
// place you can re-read a document in.
//
// Fetched on demand rather than polled: the drawer already polls the status every
// ten seconds and standing orders change a handful of times a month, so the panel
// asks when it is opened.
export function fetchAssistantInstructions() {
  return request('api/assistant/instructions')
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
