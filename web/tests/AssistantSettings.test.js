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

it('groups the fields under labelled headings with readable persistent labels and hints', async () => {
  await open()
  const dialog = document.querySelector('.v-dialog')
  expect([...dialog.querySelectorAll('h3')].map((node) => node.textContent.trim()))
    .toEqual(['Session launch', 'Conversation', 'Reminders'])
  for (const section of dialog.querySelectorAll('section.settings-group')) {
    expect(section.getAttribute('aria-labelledby')).toBe(section.querySelector('h3').id)
  }
  const labels = [...dialog.querySelectorAll('.v-field-label, label.v-label')].map((node) => node.textContent.trim())
  for (const label of ['Model', 'Harness', 'Preset', 'Fan-out presets', 'Chat threads', 'Keep last (entries)']) {
    expect(labels).toContain(label)
  }
  for (const raw of ['model', 'harness', 'preset', 'agents', 'threads']) expect(labels).not.toContain(raw)
  // Empty fields still carry a label element tied to their input, not a placeholder.
  const model = [...dialog.querySelectorAll('.v-text-field')].find((node) => node.textContent.includes('Model'))
  const input = model.querySelector('input')
  expect(input.getAttribute('placeholder')).toBeNull()
  expect(dialog.querySelector(`label[for="${input.id}"]`)).not.toBeNull()
  // Hints are persistent: shown without focus.
  expect(dialog.textContent).toContain('The model the session runs, handed to the harness verbatim.')
  expect(dialog.textContent).toContain('Next incarnation: unset — the session decides (harness default)')
  expect(dialog.querySelectorAll('.mt-n1, .mt-n2')).toHaveLength(0)
})

it('says off, not zero seconds, for a reminder whose 0 is its off switch', async () => {
  fetchAssistantConfig.mockResolvedValue({ ...config(25),
    checkin: { window_seconds: { value: 0, stored: 0, source: 'config.json' } },
    nudge: { after_seconds: { value: 1, stored: 1, source: 'config.json' },
      pending_threshold: { value: 1, stored: null, source: 'default' } },
    rotation: { context_threshold: { value: 0, stored: 0, source: 'default' } } })
  await open()
  const lines = [...document.querySelectorAll('.v-dialog .setting-source')].map((node) => node.textContent.replace(/\s+/g, ' ').trim())
  expect(lines).toContain('In effect now: off (from config.json)')
  expect(lines).toContain('In effect now: 1 second (from config.json)')
  expect(lines).toContain('In effect now: 1 digest (from default)')
  expect(lines.some((line) => line.includes('0 seconds'))).toBe(false)
})
