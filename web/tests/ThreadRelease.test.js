import { afterEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import PlatformSettings from '../src/components/PlatformSettings.vue'
import { mountPane } from './mount.js'

afterEach(() => vi.unstubAllGlobals())

const LINK = 'https://example.slack.com/archives/CEXAMPLE/p1700000000000000'
const projects = [{ id: 'general', name: 'General' }, { id: 'billing', name: 'Billing' }, { id: 'old', name: 'Old', archived: true }]

function mountWith(rows, refusal = null) {
  const calls = []
  vi.stubGlobal('fetch', vi.fn(async (path, options) => {
    calls.push({ path, options })
    if (path === 'api/settings') return Response.json({ max_live_assistants: 4, max_concurrent_sessions: 8 })
    if (path === 'api/assistant/requests') return Response.json({ requests: rows })
    if (path === 'api/threads/release') {
      if (refusal) return Response.json({ detail: refusal }, { status: 409 })
      return Response.json({ permalink: LINK, workspace_id: 'general', released: ['choice'] })
    }
    throw new Error(`Unexpected request: ${path}`)
  }))
  const wrapper = mountPane(PlatformSettings, { state: {}, projects }, {
    global: { stubs: { AssistantSettings: true, IngressStatus: true, SlotEditor: true } },
  })
  return { wrapper, calls }
}

const listed = () => ({ id: 'mention', project_id: 'general', transport: 'slack', state: 'delivered', conversation: 'CEXAMPLE',
  root: '1700000000.000000', permalink: LINK, prompt_state: 'sending', error: null, actions: [] })
const button = (wrapper, text) => wrapper.findAll('button').find(item => item.text() === text)
const dialogButton = text => [...document.querySelector('.thread-release').querySelectorAll('button')]
  .find(item => item.textContent.trim() === text)

async function reason(text) {
  const area = document.querySelector('.thread-release textarea')
  area.value = text
  area.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
}

it('hands a listed request\'s thread to another project only with a reason', async () => {
  const { wrapper, calls } = mountWith([listed()])
  await flushPromises()
  await button(wrapper, 'Release thread…').trigger('click')
  await flushPromises()
  expect(document.querySelector('.thread-release').textContent).toContain(LINK)
  const [owner, target] = wrapper.findAllComponents({ name: 'VSelect' }).slice(-2)
  expect(owner.props('modelValue')).toBe('general')
  expect(target.props('items').map(item => item.id)).toEqual(['billing'])
  target.vm.$emit('update:modelValue', 'billing')
  await flushPromises()
  expect(dialogButton('Hand off thread').disabled).toBe(true)
  await reason('Billing owns this request')
  dialogButton('Hand off thread').click()
  await flushPromises()
  const writes = calls.filter(item => item.options?.method === 'POST')
  expect(writes.map(item => item.path)).toEqual(['api/threads/release'])
  expect(JSON.parse(writes[0].options.body)).toEqual({ workspace_id: 'general', permalink: LINK,
    reason: 'Billing owns this request', to: 'billing' })
  expect(document.querySelector('.thread-release')).toBeNull()
})

it('releases an unlisted thread by permalink and keeps a refusal visible in the dialog', async () => {
  const refusal = 'Session s-1 holds this thread live; a release never takes a thread from a session.'
  const { wrapper, calls } = mountWith([], refusal)
  await flushPromises()
  await button(wrapper, 'Release a thread…').trigger('click')
  await flushPromises()
  const field = document.querySelector('.thread-release input[type="text"]')
  field.value = LINK
  field.dispatchEvent(new Event('input', { bubbles: true }))
  const owner = wrapper.findAllComponents({ name: 'VSelect' }).slice(-2)[0]
  expect(owner.props('items').map(item => item.id)).toEqual(['general', 'billing'])
  owner.vm.$emit('update:modelValue', 'general')
  await reason('Not ours')
  dialogButton('Release thread').click()
  await flushPromises()
  const write = calls.find(item => item.path === 'api/threads/release')
  expect(JSON.parse(write.options.body)).toEqual({ workspace_id: 'general', permalink: LINK, reason: 'Not ours' })
  expect(document.querySelector('.thread-release').textContent).toContain(refusal)
})

it('offers no thread release on an unowned choice row or a request a release already closed', async () => {
  const { wrapper } = mountWith([{ ...listed(), id: 'choice', project_id: null, state: 'choice' },
    { ...listed(), id: 'closed', state: 'invalid', error: 'Handed off to project Billing.' }])
  await flushPromises()
  expect(wrapper.text()).toContain('slack: choice')
  expect(wrapper.text()).toContain('Handed off to project Billing.')
  expect(button(wrapper, 'Release thread…')).toBeUndefined()
  expect(button(wrapper, 'Release a thread…')).toBeDefined()
})

// Queued too: rotation recovery queues a delivered request again for the next assistant.
it.each(['delivered', 'queued'])('offers operator parking for a %s request whose actions are uncertain', async state => {
  const { wrapper } = mountWith([{ ...listed(), id: 'stuck', state, parkable: true, prompt_state: null,
    error: 'The request\'s thread ownership changed.',
    actions: [{ method: 'POST', path: '/api/sessions', state: 'uncertain', error: 'connection reset' }] }])
  await flushPromises()
  await button(wrapper, 'Park uncertain request').trigger('click')
  await flushPromises()
  const dialog = [...document.querySelectorAll('.v-dialog')].find(item => item.textContent.includes('Park uncertain request for General'))
  expect(dialog.textContent).toContain('Its uncertain actions keep their receipts')
  expect(dialog.textContent).not.toContain('The reply stays uncertain')
})
