// Executable checks for the UI's presentation helpers (issue #141, slice M2).
//
// Run by tests/test_platform_web_app.py through whatever Node is available (the
// platform's own toolchain after `setup-ui`, else PATH), and skipped when there is
// none. This is deliberately narrow: it covers the pure logic most likely to hold
// an off-by-one — relative times, state labels, target shortening — not component
// rendering, which needs a DOM and is what live test LT3 is for.

import assert from 'node:assert/strict'
import {
  ago, attentionLabel, ledgerSummary, shortTarget, stateMeta, toneColor,
} from '../web/src/format.js'

const NOW = Date.parse('2026-07-26T12:00:00Z')

// --- ago --------------------------------------------------------------------
assert.equal(ago(null, NOW), 'never')
assert.equal(ago('not a date', NOW), 'unknown')
assert.equal(ago('2026-07-26T12:00:00Z', NOW), '0s ago')
assert.equal(ago('2026-07-26T11:59:31Z', NOW), '29s ago')
assert.equal(ago('2026-07-26T11:58:00Z', NOW), '2m ago')
assert.equal(ago('2026-07-26T10:00:00Z', NOW), '2h ago')
assert.equal(ago('2026-07-24T12:00:00Z', NOW), '2d ago')
// A clock skewed into the future must not render "-5s ago".
assert.equal(ago('2026-07-26T12:05:00Z', NOW), '0s ago')

// --- state labels -----------------------------------------------------------
assert.equal(stateMeta('waiting_on_you').tone, 'attention')
assert.equal(stateMeta('running').tone, 'live')
assert.equal(stateMeta('crashed').tone, 'bad')
// An unknown state must degrade to something printable, never "undefined".
const unknown = stateMeta('some_future_state')
assert.equal(unknown.label, 'some_future_state')
assert.equal(unknown.tone, 'idle')
assert.equal(stateMeta(undefined).label, 'unknown')

// --- tone colours -----------------------------------------------------------
// Every state must end up with a colour name; an undefined one renders as an
// unstyled chip, which reads as "nothing to see here". The state list mirrors
// lmer_platform.inventory.RUN_STATES (test_platform_web_app.py checks that it
// stays in step); this checks the tone each one carries actually paints.
const RUN_STATES = [
  'running', 'held', 'feedback', 'waiting_on_you', 'yielded', 'parked',
  'failed', 'crashed', 'dormant', 'complete', 'unknown',
]
for (const state of RUN_STATES) {
  const { label, tone } = stateMeta(state)
  assert.ok(label, `${state} has no label`)
  assert.match(toneColor(tone), /^[a-z][a-z-]*$/, `${state} tone ${tone} has no colour`)
}
assert.equal(toneColor('attention'), 'warning')
assert.equal(toneColor('bad'), 'error')
// A tone from a future state must still paint something.
assert.equal(toneColor('a_tone_nobody_defined'), toneColor('idle'))
assert.equal(toneColor(undefined), toneColor('idle'))

// --- attention labels -------------------------------------------------------
assert.match(attentionLabel('question'), /question/)
assert.equal(attentionLabel('brand_new_reason'), 'brand_new_reason')

// --- ledger -----------------------------------------------------------------
assert.equal(ledgerSummary(null), null)
assert.equal(ledgerSummary({ total: 0, done: 0 }), null)
assert.equal(ledgerSummary({ total: 9, done: 6, in_flight: [] }), '6/9 tasks')
assert.equal(
  ledgerSummary({ total: 9, done: 6, in_flight: ['T7'] }),
  '6/9 tasks · T7 in flight',
)

// --- targets ----------------------------------------------------------------
assert.equal(shortTarget(null), null)
assert.equal(
  shortTarget('https://gitlab.example.com/agents/global/-/work_items/141'),
  'gitlab.example.com/…/work_items/141',
)
assert.equal(shortTarget('not a url'), 'not a url')
assert.ok(shortTarget('x'.repeat(80)).endsWith('…'))
assert.ok(shortTarget('x'.repeat(80)).length <= 46)

console.log('web format helpers: all assertions passed')
