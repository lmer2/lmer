// A number field read the way Save sends it, shared by Save and the unsaved-edit
// check so the two cannot disagree: `300.0`, `3e2` and `+300` are all 300.
export const asNumber = (value) => Number(String(value ?? '').trim())

// The same reading as text to compare: a field Save would send as the same number
// is not an edit. A blank or unreadable field compares as typed.
export function numberReading(value) {
  const text = String(value ?? '').trim()
  return text !== '' && Number.isFinite(asNumber(text)) ? String(asNumber(text)) : text
}
