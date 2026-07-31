// Presentation helpers shared by the views.

// Run states carry different urgency; the label and tone come from one place so
// a badge never disagrees with a filter. Keep in step with
// lmer_platform.inventory.RUN_STATES.
const STATE_META = {
  running: { label: 'running', tone: 'live' },
  // Not 'live' and not 'bad': the process is up but the platform lost its only
  // view of it (the host terminal died with a daemon restart and the container
  // did not answer), so nothing is being recorded. Rendering that as a healthy
  // green row is the exact lie this state exists to stop.
  detached: { label: 'detached — not being recorded', tone: 'attention' },
  held: { label: 'held', tone: 'idle' },
  feedback: { label: 'wants a look', tone: 'attention' },
  waiting_on_you: { label: 'waiting on you', tone: 'attention' },
  yielded: { label: 'yielded', tone: 'attention' },
  parked: { label: 'parked', tone: 'idle' },
  failed: { label: 'failed', tone: 'bad' },
  crashed: { label: 'crashed', tone: 'bad' },
  dormant: { label: 'dormant', tone: 'idle' },
  complete: { label: 'complete', tone: 'done' },
  unknown: { label: 'unreadable', tone: 'bad' },
}

const ATTENTION_LABELS = {
  question: 'asked you a question',
  // Deliberately different words from `question`: that run has exited and
  // answering it starts a container, while this one is up and waiting on a
  // reply. Same urgency, different consequence, so the row must not read the
  // same. Keep in step with lmer_platform.inventory.ATTENTION_REASONS.
  live_question: 'is waiting on your reply',
  feedback: 'wants you to look at something',
  yield: 'stopped for your review',
  critical_error: 'hit an unrecoverable error',
  crashed: 'died unexpectedly',
  unreadable: 'run state could not be read',
  cap_reached: 'hit the followup round cap',
  slot_contention: 'is blocking queued work',
}

// Tones are this domain's urgency vocabulary; Vuetify colours are how a chip or
// an alert renders one. Translating in a single place is what keeps a badge from
// disagreeing with a card edge, and keeps format.js importable by plain Node —
// these are colour *names*, defined by the theme in main.js.
const TONE_COLORS = {
  live: 'success',
  attention: 'warning',
  bad: 'error',
  idle: 'tone-idle',
  done: 'tone-done',
}

export function stateMeta(state) {
  return STATE_META[state] || { label: state || 'unknown', tone: 'idle' }
}

// An unmapped tone must still paint something: an undefined colour renders as an
// unstyled chip, which reads as "nothing to see here" — the one thing a state the
// UI does not understand must not look like.
export function toneColor(tone) {
  return TONE_COLORS[tone] || TONE_COLORS.idle
}

export function attentionLabel(reason) {
  return ATTENTION_LABELS[reason] || reason
}

// What one entry on a session's ask channel is, in a word (T40). Here rather than
// in a component because two views render the same entries — the dock above the
// tabs and the record in the lmer tab — and a channel that called the same
// question "closed" in one and "unanswered" in the other would be two accounts of
// what happened to it.
//
// `answered` before `closed`, always: an answer that raced the session's close is
// still the operator's work and still what the session reads if it comes back for
// it (ask_channel/protocol.py argues this once, for both ends).
//
// `live` is a fact about the session rather than about the entry, and it is what
// separates the last two: "open" on a session that has exited reads as an
// invitation to answer something nothing will ever read.
export function askEntryLabel(entry, live = false) {
  if (!entry) return ''
  if (entry.kind === 'note') return 'note'
  if (entry.answered) return 'answered'
  if (entry.closed) return 'closed by the session'
  return live ? 'open' : 'unanswered'
}

// Relative time, because "3m ago" answers the question an operator actually has.
// Absolute timestamps stay available as tooltips.
export function ago(iso, now = Date.now()) {
  if (!iso) return 'never'
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return 'unknown'
  const seconds = Math.max(0, Math.round((now - then) / 1000))
  if (seconds < 45) return `${seconds}s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 45) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 36) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

// A span of seconds as the shortest thing that answers "how long". The sibling of
// `ago()` and deliberately on its own thresholds-in-step with it: a duration and
// an age in the same dimmed line must not disagree about when minutes become
// hours, and the only way to guarantee that is one set of numbers written twice
// next to each other.
//
// It takes seconds rather than a timestamp because its one caller has seconds: the
// idle reading is measured by a monotonic clock inside the session's container
// (lmer_cli.supervisor), and re-deriving it from a timestamp against *this*
// machine's clock is how a phone with a skewed clock reports a busy session as
// abandoned. Null in and null out, because absent is the ordinary case — a run
// with no live session, or one whose image is too old to report it — and a caller
// renders nothing rather than "idle unknown".
export function duration(seconds) {
  if (seconds === null || seconds === undefined) return null
  const value = Number(seconds)
  if (!Number.isFinite(value)) return null
  const total = Math.max(0, Math.round(value))
  if (total < 45) return `${total}s`
  const minutes = Math.round(total / 60)
  if (minutes < 45) return `${minutes}m`
  const hours = Math.round(minutes / 60)
  if (hours < 36) return `${hours}h`
  return `${Math.round(hours / 24)}d`
}

export function ledgerSummary(ledger) {
  if (!ledger || !ledger.total) return null
  const parts = [`${ledger.done}/${ledger.total} tasks`]
  if (ledger.in_flight && ledger.in_flight.length) {
    parts.push(`${ledger.in_flight.join(', ')} in flight`)
  }
  return parts.join(' · ')
}

// A published port is only useful as something to open, and the daemon publishes
// host==container, so one number identifies both.
export function portUrl(port, hostname) {
  const host = hostname || window.location.hostname
  return `http://${host}:${port.host}/`
}

export function shortTarget(target) {
  if (!target) return null
  try {
    const url = new URL(target)
    const tail = url.pathname.split('/').filter(Boolean).slice(-2).join('/')
    return tail ? `${url.hostname}/…/${tail}` : url.hostname
  } catch {
    return target.length > 48 ? `${target.slice(0, 45)}…` : target
  }
}

// The target as somewhere a browser can be *sent*, or null when it is not one.
//
// The label side of a target is already classified — targetRef names the resource
// it points at, shortTarget shortens what is left — and the href side has to gate
// on the same fact from one place. Rendered as `<a :href="target">` unconditionally,
// a target that is not an absolute URL becomes a *relative* link: a branch name, a
// taskdef whose target is prose, or the orchestrator's own `fleet` then resolves
// against whatever page the app is served from and lands on a 404 (the operator
// asked: "its target does link to a 404 currently").
//
// The scheme is checked rather than only the parse, because this value becomes an
// href and `new URL()` is happy with `mailto:` and `javascript:` too. Null is the
// ordinary answer for plenty of runs, so a caller renders the same words unlinked
// rather than hiding the target.
export function targetLink(target) {
  if (!target) return null
  let url
  try {
    url = new URL(target)
  } catch {
    return null
  }
  return url.protocol === 'http:' || url.protocol === 'https:' ? target : null
}

// The resource a task target names, and the word a row calls it. Same URLs as
// lmer_cli.cli's `_derive_repo_url_from_task_target` reads for a repo — this
// reads them for the number instead — and deliberately the same vocabulary: a
// Python function cannot be imported here, so tests/test_platform_web_runcard.py
// reads that function's indicator list and fails when a kind appears there and
// not in this map. That is as close to one grammar as the language boundary
// allows, and it means a new indicator arrives as a failing test.
//
// `work_items` is GitLab's newer URL form for an issue, so it says "issue": the
// row names the thing, not the route to it. Lookups are on a path *segment* (the
// path is split before anything is matched) for the reason cli.py gives — a
// project merely named `issues` is not a resource link.
const TARGET_KINDS = {
  merge_requests: { kind: 'mr', word: 'MR' },
  issues: { kind: 'issue', word: 'issue' },
  work_items: { kind: 'issue', word: 'issue' },
  pull: { kind: 'pr', word: 'PR' },
  // Accepted by the Python grammar and deliberately not refs: a list page or a
  // diff range names no single numbered resource, so there is nothing to put a
  // '#' in front of. Listed rather than omitted so a kind that shows up there
  // has to be classified here instead of silently reading as "not a ref".
  pulls: null,
  compare: null,
  commits: null,
  commit: null,
}

// `{ kind, number, label }` for a target that names one issue/PR/MR, else null.
// Null is a real answer — a target can be a bare repo, a compare range or prose
// — and the caller shows the shortened target then, never an empty slot where a
// number was promised.
export function targetRef(target) {
  if (!target) return null
  let path
  try {
    path = new URL(target).pathname
  } catch {
    return null
  }
  const parts = path.split('/').filter(Boolean)
  // GitLab carries the project path — subgroups and all — up to a '/-/'
  // separator and the resource after it; GitHub has no separator and puts the
  // resource straight under owner/repo.
  const dash = parts.indexOf('-')
  const rest = dash === -1 ? parts.slice(2) : parts.slice(dash + 1)
  const kind = TARGET_KINDS[rest[0]]
  if (!kind || !/^\d+$/.test(rest[1] || '')) return null
  return { kind: kind.kind, number: rest[1], label: `${kind.word} #${rest[1]}` }
}

// What is driving a session, as far as the *platform* can know it. Two optional
// and independent facts, both recorded on the spawn entry
// (lmer_platform.inventory):
//
//   - `harness` is recorded only when the spawn request named one. Left unset,
//     `lmer` resolves it inside the session and the host never learns which.
//   - `model` is the resolved LMER_LLM_NAME, and nothing writes it yet (T51
//     adds the flag). The daemon's own environment is not evidence about a run:
//     an exported LMER_LLM_NAME beats a preset's value host-side, so guessing
//     from it would be wrong for exactly the runs that name a preset.
//
// Null when neither is known, because a chip reading "unknown" where a model
// belongs is worse than a row that never raises the question.
export function driverLabel(run) {
  return [run.harness, run.model].filter(Boolean).join(' · ') || null
}
