// Run references an agent can write, and the tap that opens one (issue #241).
//
// A reference is an ordinary markdown link whose href carries a run's
// `host/project/<uid or slug>` key, so the visible text stays the run's name and
// the reference degrades into it wherever this app is not doing the rendering.
// Behaviour, constraints and the Slack reduction: docs/PLATFORM-QUICKSTART.md,
// "Run references in chat".
//
// Two properties are load-bearing and easy to undo from here:
//
//   - the dispatch is in-app. No router, no history, nothing addressable;
//   - the grammar carries three key segments and no verb, so a reference can
//     only ever select a view.
//
// One resolver in a module rather than props: `Markdown.vue` is reached from four
// surfaces, two of them through `Chat.vue`, and a reference that quietly does
// nothing in one pane is indistinguishable from a dead link.

//: The only `lmer:` URL both layers of `Markdown.vue` admit. Anchored at both
//: ends, so nothing with a query is even a link. Three or more segments because
//: a project path contributes several (`acme/widget`).
export const RUN_REF_RE = /^lmer:\/\/run\/[A-Za-z0-9][A-Za-z0-9._-]*(?:\/[A-Za-z0-9][A-Za-z0-9._-]*){2,}$/

const PREFIX = 'lmer://run/'

// One resolver, because there is one fleet view to select out of. Not a ref:
// nothing renders it, and a reactive value would invite a component to decide for
// itself whether a reference is live.
let resolver = null

//: A schema-2 run's uid, as `lmer_platform.runs.UID_RE` spells it. Nothing
//: else is one: an uppercase or short hex string is a slug that happens to look
//: like it.
const UID_RE = /^[0-9a-f]{32}$/

// runKey(run) -> 'host/project/<uid or slug>'
//
// The backend's `runs.key_parts_lenient` rule: a well-formed `uid` wins,
// otherwise the slug. A schema-2 row's slug is its `_runs/<slug>/` group label,
// shared by every record of one work item, so keying by it made two rows one —
// selecting the second opened the first and forgetting one hid both (!387). A
// malformed uid degrades to the slug rather than throwing, because this is read
// to select a row, never to act on one. A row with no uid keeps exactly the
// string `lmer_cli.runref.compose` writes on the Python side.
//
// The shell decides which run is open by this string, so a view that spells it
// differently is a navigator that never marks where you are — silently,
// because every row still opens fine (#383 item 3).
export function runKey(run) {
  if (!run) return null
  const segment = typeof run.uid === 'string' && UID_RE.test(run.uid) ? run.uid : run.slug
  return `${run.host}/${run.project}/${segment}`
}

// parseRunRef(href) -> {host, project, slug, uid?, key} | null
//
// Total: anything that is not exactly a reference is `null`, which every caller
// reads as "not one of ours" rather than as an error. `project` is everything
// between host and the last segment, so a nested project path survives.
//
// A last segment of 32 lowercase hex names a schema-2 run by its uid, and the
// ref carries it as `uid` so it keys to that row and not to its group. `slug`
// stays the last segment either way — the field the Python twin
// (`lmer_cli.runref.parse_reference`) returns — and a slug reference carries no
// `uid` key at all, so it is the object it always was.
export function parseRunRef(href) {
  if (typeof href !== 'string') return null
  const raw = href.trim()
  if (!RUN_REF_RE.test(raw)) return null
  const parts = raw.slice(PREFIX.length).split('/')
  const host = parts[0]
  const slug = parts[parts.length - 1]
  const project = parts.slice(1, -1).join('/')
  const uid = UID_RE.test(slug) ? slug : null
  const key = runKey({ host, project, slug, uid })
  return uid ? { host, project, slug, uid, key } : { host, project, slug, key }
}

// onRunRef(fn) -> stop()
//
// The shell says what a tap means. The returned teardown is a no-op unless `fn`
// is still the installed resolver, so a late unmount cannot unregister a newer
// one.
export function onRunRef(fn) {
  resolver = fn
  return () => {
    if (resolver === fn) resolver = null
  }
}

// dispatchRunRef(ref) -> boolean
//
// False when nothing is listening — a real state, not an error: the renderer can
// be mounted where there is no fleet to select out of. The caller has already
// cancelled the click by then, handled or not.
export function dispatchRunRef(ref) {
  if (!resolver || !ref) return false
  resolver(ref)
  return true
}
