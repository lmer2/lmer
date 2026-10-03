import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import CopyButton from '../src/components/CopyButton.vue'
import Markdown from '../src/components/Markdown.vue'
import Chat from '../src/components/Chat.vue'
import AskEntry from '../src/components/AskEntry.vue'
import { fetchSessionMessages, sendSessionInput } from '../src/api.js'
import { mountPane, deferred } from './mount.js'

vi.mock('../src/api.js', () => ({
  fetchSessionMessages: vi.fn(), sendSessionInput: vi.fn(),
  sessionUploadUrl: vi.fn(), uploadSessionFile: vi.fn(),
}))

let writeText
beforeEach(() => {
  vi.clearAllMocks()
  writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
})

it('confirms a successful copy briefly and keeps raw markdown', async () => {
  vi.useFakeTimers()
  const text = '**source** & <tag>\nsecond line'
  const wrapper = mountPane(CopyButton, { text })
  await wrapper.get('button').trigger('click')
  await flushPromises()
  expect(writeText).toHaveBeenCalledWith(text)
  expect(wrapper.get('button').attributes('title')).toBe('Copied')
  await vi.advanceTimersByTimeAsync(1500)
  expect(wrapper.get('button').attributes('title')).toBe('Copy message')
})

it.each(['denied', 'unavailable'])('reports clipboard %s without a success message', async (reason) => {
  if (reason === 'denied') writeText.mockRejectedValue(new Error('Denied'))
  else Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined })
  const wrapper = mountPane(CopyButton, { text: 'source' })
  await wrapper.get('button').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('Copy failed')
  expect(wrapper.text()).not.toContain('Copied')
})

it('does not confirm an old copy after the message changes', async () => {
  const pending = deferred()
  writeText.mockReturnValue(pending.promise)
  const wrapper = mountPane(CopyButton, { text: 'old' })
  await wrapper.get('button').trigger('click')
  await wrapper.setProps({ text: 'new' })
  pending.resolve()
  await flushPromises()
  expect(wrapper.get('button').attributes('title')).toBe('Copy message')
})

it('copies each fenced block without fences or language and preserves code characters', async () => {
  const text = '~~~js\nconst markup = "<b>&";\n~~~\n\n```sh\necho hi\n```'
  const wrapper = mountPane(Markdown, { text })
  await flushPromises()
  const buttons = wrapper.findAll('.code-copy button')
  expect(buttons).toHaveLength(2)
  expect(wrapper.findAll('.code-block > pre')).toHaveLength(2)
  expect(wrapper.find('pre button').exists()).toBe(false)
  expect(buttons[0].attributes('title')).toBe('Copy code')
  await buttons[0].trigger('click')
  expect(writeText).toHaveBeenLastCalledWith('const markup = "<b>&";\n')
  await buttons[1].trigger('click')
  expect(writeText).toHaveBeenLastCalledWith('echo hi\n')
  expect(wrapper.find('b').exists()).toBe(false)
})

it('replaces code controls with new content and leaves inline code alone', async () => {
  const wrapper = mountPane(Markdown, { text: '```\nold\n```' })
  await flushPromises()
  await wrapper.setProps({ text: '```\nnew\n```' })
  await flushPromises()
  expect(wrapper.findAll('.code-block')).toHaveLength(1)
  expect(wrapper.findAll('.code-copy button')).toHaveLength(1)
  await wrapper.get('.code-copy button').trigger('click')
  expect(writeText).toHaveBeenCalledWith('new\n')
  await wrapper.setProps({ text: '`inline`' })
  await flushPromises()
  expect(wrapper.find('button').exists()).toBe(false)
  expect(wrapper.find('.code-block').exists()).toBe(false)
})

it('copies source text from both operator and agent message bubbles', async () => {
  const messages = ['user', 'assistant'].map((role, seq) => ({
    id: String(seq), seq, role, kind: 'said', text: `**${role}** & <tag>`, tools: [],
  }))
  fetchSessionMessages.mockResolvedValue({ messages, cursor: 2, total: 2 })
  const wrapper = mountPane(Chat, { sessionId: 's-1' })
  await flushPromises()
  await vi.dynamicImportSettled()
  await flushPromises()
  const buttons = wrapper.findAll('.turn button[title="Copy message"]')
  expect(buttons).toHaveLength(2)
  for (let index = 0; index < buttons.length; index += 1) {
    await buttons[index].trigger('click')
    expect(writeText).toHaveBeenLastCalledWith(messages[index].text)
  }
})

it('copies the operator-channel message and its answer separately', async () => {
  const wrapper = mountPane(AskEntry, { entry: {
    id: '1', kind: 'question', text: '**Question?**', answered: true,
    answer: { text: '*Yes*' },
  } })
  await flushPromises()
  const buttons = wrapper.findAll('button[title="Copy message"]')
  expect(buttons).toHaveLength(2)
  await buttons[0].trigger('click')
  expect(writeText).toHaveBeenLastCalledWith('**Question?**')
  await buttons[1].trigger('click')
  expect(writeText).toHaveBeenLastCalledWith('*Yes*')
})

it('copies a sent message while the transcript still has no copy', async () => {
  fetchSessionMessages.mockResolvedValue({ messages: [], cursor: 0, total: 0, live: true })
  sendSessionInput.mockResolvedValue({ sent: true })
  const wrapper = mountPane(Chat, { sessionId: 'pending-copy' })
  await flushPromises()
  await wrapper.get('textarea').setValue('**Still waiting** for the transcript')
  const send = wrapper.findAll('button').find((button) => button.text().trim() === 'send')
  await send.trigger('click')
  await flushPromises()
  const copy = wrapper.get('.turn button[title="Copy message"]')
  await copy.trigger('click')
  expect(writeText).toHaveBeenCalledWith('**Still waiting** for the transcript')
})
