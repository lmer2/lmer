// The assistant's identity in the UI (issue 581). One module, so no component
// hardcodes the name again: the daemon serves the configured value in
// `GET /api/state` → `config.assistant_name`, and App.vue hands it here after
// every fleet poll. Before the first poll the default stands.
import { ref } from 'vue'

export const DEFAULT_ASSISTANT_NAME = 'Ratchet'

export const assistantName = ref(DEFAULT_ASSISTANT_NAME)

export function applyAssistantName(config) {
  const raw = config && typeof config.assistant_name === 'string' ? config.assistant_name.trim() : ''
  assistantName.value = raw || DEFAULT_ASSISTANT_NAME
}

// --- the mood (spec §4.3 + §4.5, chadd's 2026-10-12 mapping) ----------------
// One pure function of the fleet state the view already holds. First rule wins.
// Files are named for the state; `loading` has no face of its own and shows
// `talking` — before the first poll there is nothing to be alarmed about.
export const MOODS = [
  'question', 'oops', 'panic', 'strained', 'on_it', 'skeptical',
  'idle', 'yes', 'talking', 'loading',
]
// Mood -> chadd's file basename (documentation; the files are the states).
export const MOOD_FACES = {
  question: 'ratchet_question', oops: 'ratchet_oops', panic: 'ratchet_panic',
  strained: 'ratchet_strained', on_it: 'ratchet_on_it',
  skeptical: 'ratchet_skeptical', idle: 'ratchet_idle', yes: 'ratchet_yes',
  talking: 'ratchet_talking', loading: 'ratchet_talking', off: 'ratchet_off',
}
export const QUESTION_REASONS = ['live_question', 'question']
export const BROKEN_REASONS = ['crashed', 'critical_error', 'stalled', 'stuck_teardown', 'unreadable']
export const BROKEN_STATES = ['crashed', 'failed']
export const REVIEW_REASONS = ['yield', 'feedback', 'cap_reached', 'slot_contention']
export const NEAR_CAP_RATIO = 0.8

const reasonOf = (row) => row?.attention?.reason
const count = (rows, test) => (rows || []).filter(test).length

function questions(input) {
  const rows = count(input.attention, (row) => QUESTION_REASONS.includes(reasonOf(row)))
  // The assistant's own live ask is both its `live_question` attention row and
  // its status `open_questions`; count it once (issue 581 review).
  const ownRows = count(input.attention, (row) => row.orchestrator && QUESTION_REASONS.includes(reasonOf(row)))
  return rows + Math.max(0, (input.openQuestions || 0) - ownRows)
}
// A run is broken once whether the state or an attention reason says so; the
// reason is not counted again for a run whose state already did.
function broken(input) {
  const byState = count(input.runs, (row) => BROKEN_STATES.includes(row.state))
  const byReason = count(input.attention, (row) => BROKEN_REASONS.includes(reasonOf(row)) && !BROKEN_STATES.includes(row.state))
  return byState + byReason
}
// `panic` is only for something genuinely serious (chadd's rule): the explicit
// `critical_error` signal. Ordinary crashes and stalls stay `oops`, however many.
function serious(input) {
  return count(input.attention, (row) => reasonOf(row) === 'critical_error') > 0
}
function nearCap(input) {
  return !!input.cap && input.cap > 0 && (input.liveWorkers || 0) >= input.cap * NEAR_CAP_RATIO
}
function reviews(input) {
  return count(input.attention, (row) => REVIEW_REASONS.includes(reasonOf(row)))
}

export function moodOf(input) {
  if (!input || !input.loaded) return 'loading'
  if (questions(input) > 0) return 'question'
  if (serious(input)) return 'panic'
  if (broken(input) > 0) return 'oops'
  if ((input.justFinished || 0) > 0) return 'yes'
  if (nearCap(input)) return 'strained'
  if ((input.liveWorkers || 0) > 0) return 'on_it'
  if (reviews(input) > 0) return 'skeptical'
  if (input.replying) return 'talking'
  return 'idle'
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`

export function moodReason(mood, input) {
  switch (mood) {
    case 'question': return `question: ${plural(questions(input), 'question')} waiting on you`
    case 'panic': return 'panic: the daemon reported a critical error'
    case 'oops': return `oops: ${plural(broken(input), 'run')} crashed or stuck`
    case 'strained': return `strained: ${input.liveWorkers} of ${input.cap} sessions in use`
    case 'on_it': return `on_it: ${input.liveWorkers} running`
    case 'skeptical': return `skeptical: ${reviews(input)} waiting on your review`
    case 'yes': return `yes: ${plural(input.justFinished, 'run')} just finished`
    case 'talking': return 'talking: answering you'
    case 'loading': return 'loading: waiting for the fleet view'
    default: return 'idle: nothing needs you'
  }
}

export function faceFile(mood) {
  return MOOD_FACES[mood] || MOOD_FACES.idle
}

export function faceUrl(mood) {
  return `./assets/ratchet/${faceFile(mood)}.png`
}

// The off face's plain-text label (spec §4.3): the lifecycle word, so a stopped
// assistant never reads as a fleet mood it is not reporting on.
export function offReason(lifecycle) {
  const word = typeof lifecycle === 'string' && lifecycle.trim() ? lifecycle.trim() : 'stopped'
  return `off: ${word}`
}
