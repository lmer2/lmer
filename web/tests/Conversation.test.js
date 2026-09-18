import { expect, it } from 'vitest'
import { inView, layerOf } from '../src/conversation.js'

// The partition is keyed on provenance alone (issue #488): a bus record in any
// role is the conversation, a transcript turn in any role is not.
const bus = (role, extra = {}) => ({ role, kind: 'said', via: 'comms', text: 'x', ...extra })

it.each([
  ['an operator message from Slack', bus('user', { source: { channel: 'slack', author: 'operator' } })],
  ['a composer message', bus('user', { source: { channel: 'platform', author: 'operator' } })],
  ['the agent\'s post', bus('assistant', { source: { channel: 'uber-lmer' }, comms_kind: 'message' })],
  ['the agent\'s note', bus('assistant', { source: { channel: 'agent' }, comms_kind: 'note' })],
  ['a question', bus('assistant', { comms_kind: 'question' })],
  ['a milestone', bus('assistant', { comms_kind: 'milestone' })],
  ['a platform command', bus('platform', { comms_kind: 'command' })],
  ['a legacy ask-channel answer', { role: 'user', kind: 'said', via: 'ask', text: 'prep' }],
])('%s is the conversation', (_name, message) => {
  expect(layerOf(message)).toBe('conversation')
  expect(inView('conversation', message)).toBe(true)
  expect(inView('transcript', message)).toBe(true)
})

it.each([
  ['unposted prose', { role: 'assistant', kind: 'said', text: 'working on it', tools: [] }],
  ['a tool call', { role: 'assistant', kind: 'said', text: '', tools: [{ name: 'Bash' }] }],
  ['a watch firing', { role: 'monitor', kind: 'said', via: 'monitor', text: 'SPOOL pending=1' }],
  ['a hook re-prompt', { role: 'user', kind: 'injected', text: 'Stop hook feedback: …' }],
  ['a system notice', { role: 'system', kind: 'notice', text: 'compacted' }],
  ['the operator\'s words as the harness recorded them', { role: 'user', kind: 'said', text: 'sitrep please' }],
  ['a platform line typed into the PTY', { role: 'platform', kind: 'said', text: '[platform] digests pending' }],
  ['a turn with no via at all', { role: 'assistant', kind: 'said', text: 'x' }],
])('%s is the transcript', (_name, message) => {
  expect(layerOf(message)).toBe('transcript')
  expect(inView('conversation', message)).toBe(false)
  expect(inView('transcript', message)).toBe(true)
  expect(inView('session', message)).toBe(true)
})

it('reads nothing but via — a bus record is the conversation whatever it says', () => {
  expect(layerOf(bus('assistant', { text: 'Working output only.' }))).toBe('conversation')
  expect(layerOf({ role: 'assistant', kind: 'said', via: 'other', text: 'x' })).toBe('transcript')
  expect(layerOf(undefined)).toBe('transcript')
})
