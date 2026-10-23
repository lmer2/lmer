// Executable checks for the UI's mute toggle (issue 458). Run by
// tests/test_platform_mute.py.
//
// Which door a row goes through decides whether the request is scoped to the
// right project, and whether it can reach the session at all: a live row and a
// row with no run identity go by their session's own path, a tracked run whose
// session has ended by its key.

import assert from 'node:assert/strict'
import { muteRun } from '../web/src/api.js'

const calls = []
globalThis.fetch = async (path, options) => {
  calls.push({ path, method: options.method, body: JSON.parse(options.body) })
  return { ok: true, status: 200, json: async () => ({}) }
}

async function door(run, muted = true) {
  calls.length = 0
  await muteRun(run, muted)
  assert.equal(calls.length, 1)
  return calls[0]
}

const named = {
  host: 'gitlab.example.com', project: 'agents/global', slug: 'chat-1',
  workspace_id: 'w-1', home_revision: 3,
}

// A live row: by its session, carrying its project for the request binding.
let call = await door({ ...named, live: true, session: { id: 's-1' } })
assert.equal(call.path, 'api/sessions/s-1/mute')
assert.equal(call.method, 'POST')
assert.deepEqual(call.body, { workspace_id: 'w-1' })

call = await door({ ...named, live: true, session: { id: 's-1' } }, false)
assert.equal(call.path, 'api/sessions/s-1/unmute')

// No run identity: the daemon writes a dash into host and project.
call = await door({ host: '—', project: '—', slug: '—', session: { id: 's-chat' } })
assert.equal(call.path, 'api/sessions/s-chat/mute')
assert.deepEqual(call.body, {})

// A tracked run whose session has ended: by its key, with its project fields.
call = await door({ ...named, live: false, session: { id: 's-old' } })
assert.equal(call.path, 'api/runs/mute')
assert.deepEqual(call.body, {
  host: 'gitlab.example.com', project: 'agents/global', slug: 'chat-1',
  workspace_id: 'w-1', home_revision: 3,
})

call = await door({ ...named, uid: 'a'.repeat(32) }, false)
assert.equal(call.path, 'api/runs/unmute')
assert.equal(call.body.uid, 'a'.repeat(32))

// A session id is a path segment, so it is escaped.
call = await door({ ...named, live: true, session: { id: 's/../x' } })
assert.equal(call.path, 'api/sessions/s%2F..%2Fx/mute')

console.log('all assertions passed')
