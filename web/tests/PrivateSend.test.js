// Send Privately (issue #510): the composer control and its editor, mounted.
// The dialog renders in an overlay outside the wrapper, so it is read from the
// document. Markers are synthetic.
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import Chat from '../src/components/Chat.vue'
import {
  fetchSessionMessages, sendPrivately, sendSessionInput, withdrawPrivate,
} from '../src/api.js'
import { deferred, mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({
  fetchSessionMessages: vi.fn(), sendSessionInput: vi.fn(),
  sessionUploadUrl: vi.fn(), uploadSessionFile: vi.fn(),
  sendPrivately: vi.fn(), withdrawPrivate: vi.fn(),
}))

const MARKER = 'PRIVMARK-web-5f1c9a'
const REFERENCE = '[lmer private] /home/developer/.lmer-uploads/.lmer-private/s-1/20261028-120000-private.txt — private text'
let wrapper

beforeEach(() => {
  vi.clearAllMocks()
  fetchSessionMessages.mockResolvedValue({ messages: [], total: 0, cursor: 0, live: true })
  sendSessionInput.mockResolvedValue({ session: 's-1' })
  withdrawPrivate.mockResolvedValue({ withdrawn: true })
  sendPrivately.mockResolvedValue({
    session: 's-1', name: '20261028-120000-private.txt', container_path: '/x', reference: REFERENCE,
  })
})

afterEach(() => {
  wrapper?.unmount()
  document.body.innerHTML = ''
})

async function settle() {
  await flushPromises()
  await vi.dynamicImportSettled()
  await flushPromises()
}

async function mountChat(props = {}) {
  wrapper = mountPane(Chat, { sessionId: 's-1', ...props })
  await settle()
  return wrapper
}

function control() {
  return wrapper.get('[aria-label="send privately: text the run can use, kept out of the chat"]')
}

function editor() {
  return document.querySelector('.private-send textarea')
}

function button(label) {
  return [...document.querySelectorAll('.private-send .v-btn')]
    .find(node => node.textContent.trim() === label)
}

async function openAndType(value) {
  await control().trigger('click')
  await settle()
  const field = editor()
  field.value = value
  field.dispatchEvent(new Event('input'))
  await flushPromises()
}

function everythingSent() {
  return JSON.stringify([
    sendSessionInput.mock.calls, withdrawPrivate.mock.calls, fetchSessionMessages.mock.calls,
  ])
}

it('sits in the send row beside attach, labelled and thumb sized', async () => {
  await mountChat()
  const row = wrapper.get('.send-row')
  const labels = row.findAll('.v-btn').map(node => node.text())
  expect(labels.slice(0, 2)).toEqual(['attach', 'send privately'])
  expect(control().classes()).toContain('v-btn--size-large')
})

it('sends the text to the private route and only the reference to the session', async () => {
  await mountChat({ workspaceId: 'general' })
  await openAndType(`line one\npassword ${MARKER} ✓`)
  button('send privately').click()
  await settle()
  expect(sendPrivately).toHaveBeenCalledWith('s-1', { text: `line one\npassword ${MARKER} ✓`, workspaceId: 'general' })
  expect(sendSessionInput).toHaveBeenCalledWith('s-1', REFERENCE, { sanitize: true, workspaceId: 'general' })
  expect(everythingSent()).not.toContain(MARKER)
  expect(editor()).toBeNull()
  expect(wrapper.text()).not.toContain(MARKER)
})

it('cannot send empty or blank text', async () => {
  await mountChat()
  await openAndType('   \n  ')
  expect(button('send privately').disabled).toBe(true)
  button('send privately').click()
  await settle()
  expect(sendPrivately).not.toHaveBeenCalled()
})

it('cancel sends nothing and the next editor starts empty', async () => {
  await mountChat()
  await openAndType(MARKER)
  button('cancel').click()
  await settle()
  expect(editor()).toBeNull()
  expect(sendPrivately).not.toHaveBeenCalled()
  await control().trigger('click')
  await settle()
  expect(editor().value).toBe('')
})

it('a failed store keeps the text for retry, says so without quoting it, and retry works', async () => {
  await mountChat()
  sendPrivately.mockRejectedValueOnce(new Error('this session has no upload store'))
  await openAndType(MARKER)
  button('send privately').click()
  await settle()
  const alert = document.querySelector('.private-send [role="alert"]')
  expect(alert.textContent).toContain('not sent')
  expect(alert.textContent).not.toContain(MARKER)
  expect(editor().value).toBe(MARKER)
  expect(sendSessionInput).not.toHaveBeenCalled()
  button('send privately').click()
  await settle()
  expect(sendPrivately).toHaveBeenCalledTimes(2)
  expect(sendSessionInput).toHaveBeenCalledTimes(1)
  expect(editor()).toBeNull()
})

it('a failed delivery withdraws the stored file and keeps the text', async () => {
  await mountChat()
  sendSessionInput.mockRejectedValueOnce(new Error('control plane unreachable'))
  await openAndType(MARKER)
  button('send privately').click()
  await settle()
  expect(withdrawPrivate).toHaveBeenCalledWith('s-1', '20261028-120000-private.txt', undefined)
  expect(editor().value).toBe(MARKER)
})

it('switching conversations closes the editor and sends nothing', async () => {
  await mountChat()
  await openAndType(MARKER)
  await wrapper.setProps({ sessionId: 's-2' })
  await settle()
  expect(editor()).toBeNull()
  expect(sendPrivately).not.toHaveBeenCalled()
  expect(wrapper.text()).toContain('nothing was sent')
})

it('a switch while the store is in flight delivers nowhere and withdraws the file', async () => {
  await mountChat()
  const pending = deferred()
  sendPrivately.mockReturnValueOnce(pending.promise)
  await openAndType(MARKER)
  button('send privately').click()
  await flushPromises()
  await wrapper.setProps({ sessionId: 's-2' })
  pending.resolve({ session: 's-1', name: 'late.txt', container_path: '/x', reference: REFERENCE })
  await settle()
  expect(sendSessionInput).not.toHaveBeenCalled()
  expect(withdrawPrivate).toHaveBeenCalledWith('s-1', 'late.txt', undefined)
  expect(wrapper.text()).toContain('nothing was sent')
})

it('a switch while the reference is in flight says it was delivered, not that nothing was', async () => {
  await mountChat()
  const pending = deferred()
  sendSessionInput.mockReturnValueOnce(pending.promise)
  await openAndType(MARKER)
  button('send privately').click()
  await flushPromises()
  expect(sendSessionInput).toHaveBeenCalledWith('s-1', REFERENCE, { sanitize: true, workspaceId: undefined })
  await wrapper.setProps({ sessionId: 's-2' })
  await settle()
  // The outcome is not known yet, so nothing is claimed about it.
  expect(wrapper.text()).not.toContain('nothing was sent')
  pending.resolve({ session: 's-1' })
  await settle()
  expect(editor()).toBeNull()
  expect(withdrawPrivate).not.toHaveBeenCalled()
  expect(wrapper.text()).toContain('it was delivered to the conversation you had open when you pressed send')
  expect(wrapper.text()).not.toContain('nothing was sent')
})

it('an assistant rotation closes the editor and sends nothing', async () => {
  await mountChat()
  await openAndType(MARKER)
  await wrapper.setProps({ sendPaused: true })
  await settle()
  expect(editor()).toBeNull()
  expect(sendPrivately).not.toHaveBeenCalled()
  expect(wrapper.text()).toContain('the assistant began rotating — nothing was sent')
})

it('a rotation while the store is in flight delivers to neither generation and withdraws the file', async () => {
  await mountChat()
  const pending = deferred()
  sendPrivately.mockReturnValueOnce(pending.promise)
  await openAndType(MARKER)
  button('send privately').click()
  await flushPromises()
  // The id still names the outgoing generation: only the pause tells.
  await wrapper.setProps({ sendPaused: true })
  pending.resolve({ session: 's-1', name: 'late.txt', container_path: '/x', reference: REFERENCE })
  await settle()
  expect(sendSessionInput).not.toHaveBeenCalled()
  expect(withdrawPrivate).toHaveBeenCalledWith('s-1', 'late.txt', undefined)
  expect(wrapper.text()).toContain('nothing was sent')
})

it('focus returns to the control when the editor closes', async () => {
  await mountChat()
  await openAndType(MARKER)
  button('cancel').click()
  await settle()
  expect(document.activeElement).toBe(control().element)
})

it('never leaves the text in browser storage or in the composer draft', async () => {
  await mountChat()
  await openAndType(MARKER)
  button('send privately').click()
  await settle()
  const stored = [localStorage, sessionStorage].flatMap(area => Object.keys(area).map(key => area.getItem(key)))
  expect(stored.join('\n')).not.toContain(MARKER)
  expect(wrapper.get('.composer textarea').element.value).toBe('')
})

it('the rotation notice outlives polls and the new generation, and goes only when dismissed', async () => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
  try {
    await mountChat()
    const pending = deferred()
    sendSessionInput.mockReturnValueOnce(pending.promise)
    await openAndType(MARKER)
    button('send privately').click()
    await flushPromises()
    await wrapper.setProps({ sendPaused: true })
    pending.resolve({ session: 's-1' })
    await settle()
    const notice = () => wrapper.find('.private-notice')
    expect(notice().text()).toContain('send it again if the new one needs it')
    expect(withdrawPrivate).not.toHaveBeenCalled()
    await wrapper.setProps({ sessionId: 's-2', sendPaused: false })
    const polled = fetchSessionMessages.mock.calls.length
    await vi.advanceTimersByTimeAsync(15000)
    await settle()
    expect(fetchSessionMessages.mock.calls.length).toBeGreaterThan(polled)
    expect(notice().text()).toContain('send it again if the new one needs it')
    await notice().get('.v-alert__close .v-btn').trigger('click')
    await settle()
    expect(notice().exists()).toBe(false)
  } finally {
    vi.useRealTimers()
  }
})
