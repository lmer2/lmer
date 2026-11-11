import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import AnswerBox from '../src/components/AnswerBox.vue'
import AskBox from '../src/components/AskBox.vue'
import Chat from '../src/components/Chat.vue'
import {
  answerRun, answerSessionQuestion, fetchSessionMessages, sendSessionInput,
} from '../src/api.js'
import { chatEnterSends, chatSettingsLoaded } from '../src/operatorChat.js'
import { coarsePointer } from '../src/composerKeys.js'
import { mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({
  answerRun: vi.fn(), answerSessionQuestion: vi.fn(),
  fetchSessionMessages: vi.fn(), sendSessionInput: vi.fn(),
  sessionUploadUrl: vi.fn(), uploadSessionFile: vi.fn(), fetchAssistantHistory: vi.fn(),
}))

// Issue 663: the three message boxes share one rule for Enter. Each box is
// mounted fresh per case, with a draft in it, and a key is dispatched at its
// textarea; the case reads whether that box's own route was called and whether
// the keystroke's default (the newline) was suppressed.
const BOXES = {
  Chat: {
    async mount() {
      const wrapper = mountPane(Chat, { sessionId: 's-keys' })
      await flushPromises()
      await vi.dynamicImportSettled()
      await flushPromises()
      return wrapper
    },
    route: () => sendSessionInput,
    button: 'send to this session',
  },
  AskBox: {
    async mount() {
      return mountPane(AskBox, {
        sessionId: 's-keys', question: { id: 'q1', text: 'Proceed?', options: [] },
      })
    },
    route: () => answerSessionQuestion,
    button: 'send reply',
  },
  AnswerBox: {
    async mount() {
      return mountPane(AnswerBox, { run: {
        host: 'git.example', project: 'acme/widget', slug: 'develop-1',
        attention: { note: 'Proceed?' },
      } })
    },
    route: () => answerRun,
    button: 'answer and start a session',
  },
}

async function press(name, init, extra = {}) {
  const box = BOXES[name]
  const wrapper = await box.mount()
  const field = wrapper.get('textarea')
  await field.setValue('first line')
  const event = new KeyboardEvent('keydown', {
    key: 'Enter', bubbles: true, cancelable: true, ...init,
  })
  for (const [key, value] of Object.entries(extra)) {
    Object.defineProperty(event, key, { value })
  }
  field.element.dispatchEvent(event)
  await flushPromises()
  return { sent: box.route().mock.calls.length, prevented: event.defaultPrevented, wrapper }
}

const hintOf = (wrapper) => wrapper.get('.v-messages').text()

beforeEach(() => {
  vi.clearAllMocks()
  chatEnterSends.value = true
  chatSettingsLoaded.value = true
  coarsePointer.value = false
  fetchSessionMessages.mockResolvedValue({ messages: [], total: 0, cursor: 0, live: true })
  sendSessionInput.mockResolvedValue({ submit_confirmed: true })
  answerSessionQuestion.mockResolvedValue({})
  answerRun.mockResolvedValue({ session: { session_id: 's-answer' } })
})

afterEach(() => {
  chatEnterSends.value = true
  chatSettingsLoaded.value = true
  coarsePointer.value = false
})

describe.each(Object.keys(BOXES))('%s', (name) => {
  describe('Enter sends (the default)', () => {
    it('sends on Enter and keeps the newline out of the draft', async () => {
      expect(await press(name, {})).toMatchObject({ sent: 1, prevented: true })
    })

    it('inserts a newline on Shift+Enter', async () => {
      expect(await press(name, { shiftKey: true })).toMatchObject({ sent: 0, prevented: false })
    })

    it('still sends on Ctrl+Enter', async () => {
      expect(await press(name, { ctrlKey: true })).toMatchObject({ sent: 1, prevented: true })
    })

    it('still sends on Cmd+Enter', async () => {
      expect(await press(name, { metaKey: true })).toMatchObject({ sent: 1, prevented: true })
    })

    it('leaves an Enter that commits an IME composition to the IME', async () => {
      expect(await press(name, { isComposing: true })).toMatchObject({ sent: 0, prevented: false })
    })

    it('leaves a keyCode 229 Enter to the IME too', async () => {
      expect(await press(name, {}, { keyCode: 229 })).toMatchObject({ sent: 0, prevented: false })
    })

    it('says so in the hint', async () => {
      const { wrapper } = await press(name, { shiftKey: true })
      expect(hintOf(wrapper)).toContain('Enter sends, Shift+Enter is a new line')
    })
  })

  describe('Enter is a new line (setting off)', () => {
    beforeEach(() => { chatEnterSends.value = false })

    it('inserts a newline on Enter', async () => {
      expect(await press(name, {})).toMatchObject({ sent: 0, prevented: false })
    })

    it('inserts a newline on Shift+Enter', async () => {
      expect(await press(name, { shiftKey: true })).toMatchObject({ sent: 0, prevented: false })
    })

    it('sends on Ctrl+Enter', async () => {
      expect(await press(name, { ctrlKey: true })).toMatchObject({ sent: 1, prevented: true })
    })

    it('sends on Cmd+Enter', async () => {
      expect(await press(name, { metaKey: true })).toMatchObject({ sent: 1, prevented: true })
    })

    it('leaves a composing Ctrl+Enter to the IME', async () => {
      expect(await press(name, { ctrlKey: true, isComposing: true })).toMatchObject({ sent: 0, prevented: false })
    })

    it('says so in the hint', async () => {
      const { wrapper } = await press(name, { shiftKey: true })
      expect(hintOf(wrapper)).toContain('Enter is a new line, Ctrl+Enter (Cmd on a Mac) sends')
    })
  })

  describe('before the platform settings have loaded', () => {
    beforeEach(() => { chatSettingsLoaded.value = false })

    it('keeps Enter a newline, so a slow config fetch cannot send a draft', async () => {
      expect(await press(name, {})).toMatchObject({ sent: 0, prevented: false })
    })

    it('still sends on Ctrl+Enter', async () => {
      expect(await press(name, { ctrlKey: true })).toMatchObject({ sent: 1, prevented: true })
    })
  })

  it('sends once for a held Enter, ignoring its repeats', async () => {
    const box = BOXES[name]
    const wrapper = await box.mount()
    const field = wrapper.get('textarea')
    await field.setValue('first line')
    const held = new KeyboardEvent('keydown', { key: 'Enter', repeat: true, bubbles: true, cancelable: true })
    field.element.dispatchEvent(held)
    await flushPromises()
    expect(held.defaultPrevented).toBe(true)
    expect(box.route().mock.calls.length).toBe(0)
  })

  describe('on a touch-first device', () => {
    beforeEach(() => { coarsePointer.value = true })

    it('keeps Enter a newline even with the setting on', async () => {
      const { sent, prevented, wrapper } = await press(name, {})
      expect({ sent, prevented }).toEqual({ sent: 0, prevented: false })
      expect(hintOf(wrapper)).toContain('Enter is a new line')
      // A touch keyboard has no Ctrl or Cmd to press.
      expect(hintOf(wrapper)).not.toContain('Ctrl')
    })

    it('still sends from the send button', async () => {
      const box = BOXES[name]
      const wrapper = await box.mount()
      await wrapper.get('textarea').setValue('first line\nsecond line')
      await wrapper.get(`button[aria-label="${box.button}"]`).trigger('click')
      await flushPromises()
      expect(box.route().mock.calls.length).toBe(1)
    })
  })
})

it('switches every mounted box at once when the setting changes', async () => {
  const ask = await BOXES.AskBox.mount()
  const answer = await BOXES.AnswerBox.mount()
  expect(hintOf(ask)).toContain('Enter sends')
  expect(hintOf(answer)).toContain('Enter sends')
  chatEnterSends.value = false
  await flushPromises()
  expect(hintOf(ask)).toContain('Enter is a new line')
  expect(hintOf(answer)).toContain('Enter is a new line')
})

// A bare Enter can now reach the sends a box refuses; it must be refused the same
// way the chord is, newline suppressed, and not vanish silently.
it('announces an AnswerBox Enter refused for a missing authorization', async () => {
  const wrapper = mountPane(AnswerBox, { run: {
    host: 'git.example', project: 'acme/widget', slug: 'develop-1',
    workspace_id: 'beta', home_revision: 2, confirm_project: true,
    workspace_name: 'Example Beta', attention: { note: 'Proceed?' },
  } })
  const field = wrapper.get('textarea')
  await field.setValue('Proceed carefully')
  const event = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
  field.element.dispatchEvent(event)
  await flushPromises()
  expect(event.defaultPrevented).toBe(true)
  expect(answerRun).not.toHaveBeenCalled()
  expect(wrapper.get('.send-announcer').text()).toBe(
    'Tick "Authorize this answer in Example Beta" above to send.',
  )
})

it('keeps a Chat draft intact when Enter is pressed while sending is paused', async () => {
  const wrapper = mountPane(Chat, { sessionId: 's-keys', sendPaused: true })
  await flushPromises()
  await vi.dynamicImportSettled()
  await flushPromises()
  const field = wrapper.get('textarea')
  await field.setValue('held for the successor')
  field.element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
  await flushPromises()
  expect(sendSessionInput).not.toHaveBeenCalled()
  expect(field.element.value).toBe('held for the successor')
})
