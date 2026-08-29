// Executable checks for run references — the parser and the dispatch registry
// (issue #241). Run by tests/test_platform_web_runref.py.
//
// The parse is the security boundary: anything that is not exactly a reference has
// to come out `null`, because a too-loose pattern means a live link the renderer
// built and the dispatcher then acts on.

import assert from 'node:assert/strict'
import {
  RUN_REF_RE, dispatchRunRef, onRunRef, parseRunRef,
} from '../web/src/runref.js'

// --- what a reference is ----------------------------------------------------
const ref = parseRunRef('lmer://run/gitlab.example.com/acme/widget/develop-issue-381')
assert.deepEqual(ref, {
  host: 'gitlab.example.com',
  project: 'acme/widget',
  slug: 'develop-issue-381',
  key: 'gitlab.example.com/acme/widget/develop-issue-381',
})

// A project path can be deeper than one segment; the slug is always the last.
assert.equal(
  parseRunRef('lmer://run/gitlab.example.com/gh/acme/widget-docs/review-mr-9').project,
  'gh/acme/widget-docs',
)

// Surrounding whitespace is what a markdown link can carry; the key is not.
assert.equal(
  parseRunRef('  lmer://run/h/p/s  ').key,
  'h/p/s',
)

// --- what a reference is not -------------------------------------------------
const rejected = [
  // Not enough to name a run.
  'lmer://run/host/slug',
  'lmer://run/host',
  'lmer://run/',
  // Anything riding along — a reference may only select a view.
  'lmer://run/h/p/s?spawn=1',
  'lmer://run/h/p/s#x',
  'lmer://run/h/p/s/../../etc',
  'lmer://spawn/h/p/s',
  'lmer://run/h/p/s%20',
  'lmer://run/h/p/s https://evil.example',
  // Other schemes, including ones that look like this one.
  'lmerx://run/h/p/s',
  'javascript:alert(1)',
  'https://example.com/run/h/p/s',
  'data:text/html,<script>alert(1)</script>',
  // Empty segments and separators the key format would not survive.
  'lmer://run/h//s',
  'lmer://run/h/p/',
  'lmer://run/-h/p/s',
  // Not a string at all.
  null,
  undefined,
  42,
  {},
]
for (const bad of rejected) {
  assert.equal(parseRunRef(bad), null, `parsed a non-reference: ${String(bad)}`)
  if (typeof bad === 'string') {
    assert.equal(RUN_REF_RE.test(bad.trim()), false, `regexp admits ${bad}`)
  }
}

// --- the dispatch registry ---------------------------------------------------
// Nothing listening is a real state: the renderer is imported by surfaces with no
// fleet to select out of.
assert.equal(dispatchRunRef(ref), false)

let seen = null
const stop = onRunRef((got) => { seen = got })
assert.equal(dispatchRunRef(ref), true)
assert.equal(seen.key, ref.key)

// A null ref must not reach the resolver.
seen = null
assert.equal(dispatchRunRef(null), false)
assert.equal(seen, null)

// Teardown belongs to whoever registered: after it, taps are inert again.
stop()
seen = null
assert.equal(dispatchRunRef(ref), false)
assert.equal(seen, null)

// A stale teardown must not unregister somebody else's resolver.
const first = onRunRef(() => { seen = 'first' })
const second = onRunRef(() => { seen = 'second' })
first()
assert.equal(dispatchRunRef(ref), true)
assert.equal(seen, 'second')
second()

console.log('all assertions passed')
