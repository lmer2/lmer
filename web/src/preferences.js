// What this operator likes, remembered between runs and across reloads (T49).
//
// Three preferences so far — the terminal's height, whether it fits itself to this
// screen, and which view of a run was last being read — and one set of rules for
// all of them. The rules are not new: the height presets had them first, and they
// are here because that contract is the right one for anything read back out of a
// browser's storage.
//
// Validate against what exists *now*, and fall back to the default on anything
// else. A remembered value is not data this app controls: a preset can be dropped,
// a tab renamed, and another version of this app writes the same keys from the same
// origin. A value that no longer names anything is not a forgotten preference, it
// is a view that renders nothing at all — and a blank run view is a far worse
// failure than a default one.
//
// Never fail to render over a preference. A browser's storage *throws* rather than
// returning null in private browsing and in some embedded webviews, so reading it
// is not a safe operation; neither is writing it when storage is disabled or full.
//
// What is deliberately not here is the keys. Every access spells its own key out at
// the site that owns it, because that is what keeps the storage allowlist in
// tests/test_platform_web_app.py able to read what this app puts in a browser:
// `getItem(key)` tells a reviewer nothing, while `getItem(HEIGHT_STORAGE_KEY)` next
// to that constant's declaration tells them everything. So what is shared is the
// *policy* — validate, fall back, survive a storage that throws — and the access
// itself stays one named line per key, handed in as the read or the write to make.

// A flag is stored as a word rather than as `true`/`false`: everything comes back
// out of storage as a string anyway, and these two are unmistakable in a devtools
// storage pane.
const FLAG_ON = 'on'
const FLAG_OFF = 'off'

// The one place that knows a browser's storage can refuse. Both halves of that are
// real: Chrome throws on the storage object itself when cookies are blocked, and
// Safari throws on write in private mode. Hence a thunk rather than a value — the
// access has to happen inside the try, not before it.
function guarded(act, fallback) {
  try {
    return act()
  } catch {
    return fallback
  }
}

// One of `allowed`, or its first entry — which is therefore the default, the same
// convention the terminal's height presets already use. `parse` is for a preference
// that is not a string once it is back in the app: the height is a number, and
// `'1.5' !== 1.5` would fail the check and silently reset every reload.
export function storedChoice(read, allowed, parse = (raw) => raw) {
  const stored = parse(guarded(read, null))
  return allowed.includes(stored) ? stored : allowed[0]
}

// Validated on the way in as well as on the way out. Storing something no longer
// offered would be read back once, rejected, and reset — which does not look like a
// stale value, it looks like the preference not being remembered at all.
export function rememberChoice(write, allowed, value) {
  if (!allowed.includes(value)) return
  guarded(() => write(String(value)))
}

// A remembered boolean, where "nothing was ever stored" and "the operator turned it
// off" are different answers — so the caller's default is only used for the first,
// and for a value neither end of this wrote.
export function storedFlag(read, fallback) {
  const stored = guarded(read, null)
  if (stored === FLAG_ON) return true
  if (stored === FLAG_OFF) return false
  return fallback
}

export function rememberFlag(write, value) {
  guarded(() => write(value ? FLAG_ON : FLAG_OFF))
}
