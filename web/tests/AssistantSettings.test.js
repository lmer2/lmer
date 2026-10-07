import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import AssistantSettings from '../src/components/AssistantSettings.vue'
import { fetchAssistantConfig, setAssistantConfig } from '../src/api.js'
import { chatKeepLast, chatShowTranscript, applyChatSettings } from '../src/operatorChat.js'
import { mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({ fetchAssistantConfig: vi.fn(), setAssistantConfig: vi.fn() }))
const config = (value = 0, changed = [], shown = false) => ({
  settings: Object.fromEntries(['model', 'harness', 'preset', 'agents', 'threads'].map((key) => [
    key, { value: null, stored: null, source: 'default' },
  ])),
  chat: {
    keep_last: { value, stored: value, source: 'config.json' },
    show_transcript: {
      value: shown, stored: shown ? true : null, source: shown ? 'config.json' : 'default',
    },
  },
  changed,
})
const button = (label) => [...document.querySelectorAll('.v-dialog button')]
  .find((node) => node.textContent.trim() === label)
const transcriptSwitch = () => {
  const control = [...document.querySelectorAll('.v-dialog .v-switch')]
    .find((node) => node.textContent.includes('working transcript'))
  expect(control).toBeDefined()
  return control.querySelector('input')
}

beforeEach(() => {
  vi.clearAllMocks()
  applyChatSettings(config())
  fetchAssistantConfig.mockResolvedValue(config(25))
  setAssistantConfig.mockImplementation(async (named) => config(
    named.chat_keep_last ?? 25, Object.keys(named), named.chat_show_transcript ?? false,
  ))
})

// The uber lmer pane's transcript switch (issue #488): stored through the same
// config route as retention, applied to the drawer the moment the save lands.
it('turns the working transcript on through the config route and applies it at once', async () => {
  await open()
  expect(chatShowTranscript.value).toBe(false)
  const input = transcriptSwitch()
  expect(input.checked).toBe(false)
  input.click()
  await flushPromises()
  button('save').click()
  await flushPromises()
  expect(setAssistantConfig).toHaveBeenCalledWith({ chat_show_transcript: true })
  expect(chatShowTranscript.value).toBe(true)
  expect(document.body.textContent).toContain('applies now')
  expect(button('restart')).toBeUndefined()
})

it('prefills the switch from the stored setting and sends nothing when it is unchanged', async () => {
  fetchAssistantConfig.mockResolvedValue(config(25, [], true))
  await open()
  expect(transcriptSwitch().checked).toBe(true)
  expect(chatShowTranscript.value).toBe(true)
  button('save').click()
  await flushPromises()
  expect(setAssistantConfig).not.toHaveBeenCalled()
})

it('turns the working transcript off again with an explicit false', async () => {
  fetchAssistantConfig.mockResolvedValue(config(25, [], true))
  await open()
  transcriptSwitch().click()
  await flushPromises()
  button('save').click()
  await flushPromises()
  expect(setAssistantConfig).toHaveBeenCalledWith({ chat_show_transcript: false })
  expect(chatShowTranscript.value).toBe(false)
})

async function open() {
  const wrapper = mountPane(AssistantSettings, { modelValue: false })
  await wrapper.setProps({ modelValue: true })
  await flushPromises()
  return wrapper
}

async function edit(value) {
  const field = document.querySelector('.v-dialog input[type="number"]')
  expect(field).not.toBeNull()
  field.value = value
  field.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
}

it('loads the stored retention, persists only that key, and applies it without restart', async () => {
  await open()
  expect(document.querySelector('input[type="number"]').value).toBe('25')
  await edit('10')
  button('save').click()
  await flushPromises()
  expect(setAssistantConfig).toHaveBeenCalledWith({ chat_keep_last: 10 })
  expect(chatKeepLast.value).toBe(10)
  expect(document.body.textContent).toContain('Operator chat display applies now')
  expect(button('restart')).toBeUndefined()
})

it('saves zero to turn automatic clearing off', async () => {
  await open()
  await edit('0')
  button('save').click()
  await flushPromises()
  expect(setAssistantConfig).toHaveBeenCalledWith({ chat_keep_last: 0 })
  expect(chatKeepLast.value).toBe(0)
})

it('keeps the entered value and reports a refused save', async () => {
  setAssistantConfig.mockRejectedValueOnce(new Error('Configuration is read-only'))
  await open()
  await edit('10')
  button('save').click()
  await flushPromises()
  expect(document.body.textContent).toContain('Configuration is read-only')
  expect(document.querySelector('input[type="number"]').value).toBe('10')
  expect(chatKeepLast.value).toBe(25)
})

it.each(['-1', '1.5'])('refuses invalid retention %s before saving', async (value) => {
  await open()
  await edit(value)
  button('save').click()
  await flushPromises()
  expect(setAssistantConfig).not.toHaveBeenCalled()
  expect(document.body.textContent).toContain('whole number')
})

it('does not offer a restart when retention changes beside an environment-pinned launch setting', async () => {
  const before = config(25)
  before.settings.model = { value: 'pinned', stored: 'old', source: 'env' }
  fetchAssistantConfig.mockResolvedValue(before)
  const after = config(10, ['model', 'chat_keep_last'])
  after.settings.model = { value: 'pinned', stored: 'edited', source: 'env' }
  setAssistantConfig.mockResolvedValue(after)
  await open()
  const model = document.querySelector('.v-dialog input')
  model.value = 'edited'
  model.dispatchEvent(new Event('input', { bubbles: true }))
  await edit('10')
  button('save').click()
  await flushPromises()
  expect(setAssistantConfig).toHaveBeenCalledWith({ model: 'edited', chat_keep_last: 10 })
  expect(chatKeepLast.value).toBe(10)
  expect(button('restart')).toBeUndefined()
  expect(document.body.textContent).toContain('Operator chat display applies now')
})
