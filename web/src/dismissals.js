// What the operator has cleared from a session's ask dock, for as long as this page
// is loaded (T40).
//
// A module of its own for one reason: this state has to outlive the component that
// owns the button. AskChannel.vue is rebuilt every time a run is left and re-entered,
// so a dismissal held inside it came back on the next visit — which reads as a clear
// button that does not work, and is what the operator reported.
//
// Memory, and nothing else. Not the browser's storage: the value would be a per-run
// set of question ids, which is the one shape preferences.js cannot hold (its whole
// contract is that a remembered value is validated against what exists *now*, and
// there is nothing to validate a growing id list against), it would grow without
// bound, and a dismissal made once would hide a stranded container on every future
// visit behind a preference nobody can find. So a reload is the reset, and a reload
// is something an operator does on purpose when they want the channel back.
//
// Keyed by session, because the ids belong to one channel: another session's entries
// are different questions under numbers that mean something else. One short array per
// session opened in this page's life, all of it gone when the tab is reloaded.
const cleared = new Map()

// What this session's dock has cleared so far. An array rather than a Set because it
// is read in a template, and a copy rather than the stored one so nothing outside
// this module can grow it by accident.
export function clearedIds(sessionId) {
  return [...(cleared.get(sessionId) || [])]
}

// Add to that, and hand back the whole set so the caller renders from what was
// stored rather than from its own idea of it. Ids only: nothing here holds an entry,
// so this store can never keep a question's text — or its answer — alive.
export function rememberCleared(sessionId, ids) {
  const kept = [...new Set([...clearedIds(sessionId), ...ids])]
  cleared.set(sessionId, kept)
  return [...kept]
}
