import { afterEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import PlatformSettings from '../src/components/PlatformSettings.vue'
import { mountPane } from './mount.js'

afterEach(() => vi.unstubAllGlobals())

function mountRequests(initial = []) {
  const source = { rows: initial, fail: false, refuseRelease: false, unavailable: 0 }
  const calls = []
  vi.stubGlobal('fetch', vi.fn(async (path, options) => {
    calls.push({ path, options })
    if (path === 'api/settings') return Response.json({ max_live_assistants: 4, max_concurrent_sessions: 8 })
    if (path === 'api/assistant/requests') {
      return source.fail ? Response.json({ detail: 'Request status unavailable' }, { status: 503 }) : Response.json({ requests: source.rows })
    }
    if (path === 'api/assistant/requests/blocked/release') {
      if (source.refuseRelease) return Response.json({ detail: 'Outcome became uncertain; release refused' }, { status: 409 })
      if (source.unavailable > 0) {
        source.unavailable -= 1
        return Response.json({ detail: 'Release service unavailable' }, { status: 503 })
      }
      source.rows = source.rows.map(row => ({ ...row, state: 'invalid', recovery: { ...row.recovery,
        release_allowed: false, released: true, instruction: 'Request released; no reply sent. Completed action receipts are retained.' } }))
      return Response.json({ released: true })
    }
    throw new Error(`Unexpected request: ${path}`)
  }))
  const wrapper = mountPane(PlatformSettings, { state: {}, projects: [{ id: 'example', name: 'Example' }] }, {
    global: { stubs: { AssistantSettings: true, IngressStatus: true, SlotEditor: true } },
  })
  return { wrapper, source, calls }
}

const blocked = () => ({ id: 'blocked', project_id: 'example', transport: 'slack', state: 'delivered',
  conversation: 'CEXAMPLE', root: '1700000000.000000', error: 'The source thread ownership changed.',
  actions: [{ method: 'POST', path: '/api/sessions', state: 'complete' }],
  recovery: { code: 'request_ownership_changed', reason: 'The source thread ownership changed.', release_allowed: true,
    instruction: 'Take pending or release this blocked request. No action or reply will be replayed.' } })

const button = (wrapper, text) => wrapper.findAll('button').find(item => item.text() === text)

it('refreshes a delivered request with a completed spawn and releases only after explicit operator input', async () => {
  const { wrapper, source, calls } = mountRequests()
  await flushPromises()
  expect(wrapper.text()).not.toContain('slack: delivered')
  source.rows = [{ ...blocked(), error: null }]
  await wrapper.setProps({ state: { revision: 2 } })
  await flushPromises()
  expect(wrapper.text()).toContain('slack: delivered')
  expect(wrapper.text()).toContain('source thread ownership changed')
  expect(wrapper.text()).toContain('POST /api/sessions: complete')
  expect(wrapper.text()).toContain('No action or reply will be replayed')
  expect(button(wrapper, 'Park uncertain reply')).toBeUndefined()
  await button(wrapper, 'Release blocked request').trigger('click')
  await flushPromises()
  const dialog = document.querySelector('.v-dialog')
  expect(dialog.textContent).toContain('Example')
  expect(dialog.textContent).toContain('Keep the existing worker')
  const confirm = [...dialog.querySelectorAll('button')].find(item => item.textContent.includes('Release and allow'))
  expect(confirm.disabled).toBe(true)
  const reason = dialog.querySelector('textarea')
  reason.value = 'Preserve the completed review and allow independent work'
  reason.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
  confirm.click()
  await flushPromises()
  const writes = calls.filter(item => item.options.method === 'POST')
  expect(writes).toHaveLength(1)
  expect(writes[0].path).toBe('api/assistant/requests/blocked/release')
  expect(JSON.parse(writes[0].options.body)).toEqual({ workspace_id: 'example', reason: reason.value })
  expect(wrapper.text()).toContain('Request released; no reply sent')
  expect(wrapper.text()).toContain('POST /api/sessions: complete')
  expect(button(wrapper, 'Release blocked request')).toBeUndefined()
})

it('refreshes manually, keeps failed refresh visible and never offers release for uncertainty', async () => {
  const row = blocked()
  row.recovery.release_allowed = false
  row.recovery.instruction = 'Inspect the uncertain action outcome before recovery.'
  row.actions.push({ method: 'POST', path: '/api/sessions/example/comms', state: 'uncertain', error: 'Response lost' })
  const { wrapper, source, calls } = mountRequests([row])
  await flushPromises()
  expect(button(wrapper, 'Release blocked request')).toBeUndefined()
  expect(wrapper.text()).toContain('Inspect the uncertain action outcome')
  source.fail = true
  await button(wrapper, 'Refresh requests').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('Request status unavailable')
  expect(wrapper.text()).toContain('slack: delivered')
  expect(calls.filter(item => item.options.method === 'POST')).toHaveLength(0)
})

it('keeps the blocked request when execution-time recovery is refused', async () => {
  const { wrapper, source } = mountRequests([blocked()])
  source.refuseRelease = true
  await flushPromises()
  await button(wrapper, 'Release blocked request').trigger('click')
  await flushPromises()
  const dialog = document.querySelector('.v-dialog')
  const reason = dialog.querySelector('textarea')
  reason.value = 'Try supported recovery'
  reason.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
  const confirm = [...dialog.querySelectorAll('button')].find(item => item.textContent.includes('Release and allow'))
  confirm.click()
  await flushPromises()
  expect(dialog.textContent).toContain('Outcome became uncertain; release refused')
  expect(wrapper.text()).toContain('slack: delivered')
  expect(wrapper.text()).not.toContain('Request released; no reply sent')
})

async function confirmReleaseWith(wrapper, text) {
  const dialog = document.querySelector('.v-dialog')
  const reason = dialog.querySelector('textarea')
  reason.value = text
  reason.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
  ;[...dialog.querySelectorAll('button')].find(item => item.textContent.includes('Release and allow')).click()
  await flushPromises()
  return dialog
}

it('clears the earlier release error when a retry in the same dialog succeeds', async () => {
  const { wrapper, source } = mountRequests([blocked()])
  source.unavailable = 1
  await flushPromises()
  await button(wrapper, 'Release blocked request').trigger('click')
  await flushPromises()
  const dialog = await confirmReleaseWith(wrapper, 'Allow independent work')
  expect(dialog.textContent).toContain('Release service unavailable')
  expect(wrapper.text()).toContain('Release service unavailable')
  await confirmReleaseWith(wrapper, 'Allow independent work')
  expect(wrapper.text()).not.toContain('Release service unavailable')
  expect(wrapper.text()).toContain('Request released; no reply sent')
  expect(wrapper.text()).toContain('POST /api/sessions: complete')
  expect(button(wrapper, 'Release blocked request')).toBeUndefined()
})

it('keeps the error of a retry that fails again, and a later refresh failure', async () => {
  const { wrapper, source } = mountRequests([blocked()])
  source.unavailable = 2
  await flushPromises()
  await button(wrapper, 'Release blocked request').trigger('click')
  await flushPromises()
  await confirmReleaseWith(wrapper, 'Allow independent work')
  const dialog = await confirmReleaseWith(wrapper, 'Allow independent work')
  expect(dialog.textContent).toContain('Release service unavailable')
  expect(wrapper.text()).toContain('slack: delivered')
  await confirmReleaseWith(wrapper, 'Allow independent work')
  expect(wrapper.text()).not.toContain('Release service unavailable')
  source.fail = true
  await button(wrapper, 'Refresh requests').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('Request status unavailable')
})
