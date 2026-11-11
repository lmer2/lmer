import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import AssistantSettings from '../src/components/AssistantSettings.vue'
import { fetchAssistantConfig, setAssistantConfig } from '../src/api.js'
import { chatEnterSends, chatShowTranscript, chatTintOperator, applyChatSettings } from '../src/operatorChat.js'
import { deferred, mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({ fetchAssistantConfig: vi.fn(), setAssistantConfig: vi.fn() }))
const config = (changed = [], shown = false, tint = undefined, enter = undefined) => ({
  settings: Object.fromEntries(['model', 'harness', 'preset', 'agents', 'effort', 'threads'].map((key) => [
    key, { value: null, stored: null, source: 'default' },
  ])),
  chat: {
    show_transcript: {
      value: shown, stored: shown ? true : null, source: shown ? 'config.json' : 'default',
    },
    ...(tint === undefined ? {} : { tint_operator: { value: tint, stored: tint, source: 'config.json' } }),
    ...(enter === undefined ? {} : { enter_sends: { value: enter, stored: enter, source: 'config.json' } }),
  },
  changed,
})
let wrapper
const button = (label) => [...document.querySelectorAll('.assistant-settings button')]
  .find((node) => node.textContent.trim() === label)
// The host card owns the Save button and calls the form's own save().
async function save() {
  await wrapper.vm.save()
  await flushPromises()
}
const transcriptSwitch = () => {
  const control = [...document.querySelectorAll('.assistant-settings .v-switch')]
    .find((node) => node.textContent.includes('working transcript'))
  expect(control).toBeDefined()
  return control.querySelector('input')
}

afterEach(() => { wrapper?.unmount(); wrapper = null })
beforeEach(() => {
  vi.clearAllMocks()
  applyChatSettings(config())
  fetchAssistantConfig.mockResolvedValue(config())
  setAssistantConfig.mockImplementation(async (named) => config(
    Object.keys(named), named.chat_show_transcript ?? false, named.chat_tint_operator,
    named.chat_enter_sends,
  ))
})

// The uber lmer pane's transcript switch (issue #488): stored through the
// config route, applied to the drawer the moment the save lands.
it('turns the working transcript on through the config route and applies it at once', async () => {
  await open()
  expect(chatShowTranscript.value).toBe(false)
  const input = transcriptSwitch()
  expect(input.checked).toBe(false)
  input.click()
  await flushPromises()
  await save()
  expect(setAssistantConfig).toHaveBeenCalledWith({ chat_show_transcript: true })
  expect(chatShowTranscript.value).toBe(true)
  expect(document.body.textContent).toContain('applies now')
  expect(button('restart')).toBeUndefined()
})

it('prefills the switch from the stored setting and sends nothing when it is unchanged', async () => {
  fetchAssistantConfig.mockResolvedValue(config([], true))
  await open()
  expect(transcriptSwitch().checked).toBe(true)
  expect(chatShowTranscript.value).toBe(true)
  await save()
  expect(setAssistantConfig).not.toHaveBeenCalled()
})

it('turns the working transcript off again with an explicit false', async () => {
  fetchAssistantConfig.mockResolvedValue(config([], true))
  await open()
  transcriptSwitch().click()
  await flushPromises()
  await save()
  expect(setAssistantConfig).toHaveBeenCalledWith({ chat_show_transcript: false })
  expect(chatShowTranscript.value).toBe(false)
})

async function open(props = {}) {
  wrapper = mountPane(AssistantSettings, props)
  await flushPromises()
  return wrapper
}

// The dock's retention lives in its own Platform settings card (issue 465): this
// form is the assistant's, and the setting governs every run's dock.
it('offers no retention field and never writes the retired chat_keep_last', async () => {
  await open()
  const dialog = document.querySelector('.assistant-settings')
  expect(dialog.textContent).not.toMatch(/keep last/i)
  transcriptSwitch().click()
  await flushPromises()
  await save()
  expect(setAssistantConfig).toHaveBeenCalledWith({ chat_show_transcript: true })
})

it('does not offer a restart when a display change lands beside an environment-pinned launch setting', async () => {
  const before = config()
  before.settings.model = { value: 'pinned', stored: 'old', source: 'env' }
  fetchAssistantConfig.mockResolvedValue(before)
  const after = config(['chat_show_transcript', 'model'], true)
  after.settings.model = { value: 'pinned', stored: 'edited', source: 'env' }
  setAssistantConfig.mockResolvedValue(after)
  await open()
  const model = document.querySelector('.assistant-settings input')
  model.value = 'edited'
  model.dispatchEvent(new Event('input', { bubbles: true }))
  transcriptSwitch().click()
  await flushPromises()
  await save()
  expect(setAssistantConfig).toHaveBeenCalledWith({ model: 'edited', chat_show_transcript: true })
  expect(chatShowTranscript.value).toBe(true)
  expect(button('restart')).toBeUndefined()
  expect(document.body.textContent).toContain('Chat display applies now')
})

it('groups the fields, each group with its scope line, and readable persistent labels and hints', async () => {
  await open()
  const dialog = document.querySelector('.assistant-settings')
  expect([...dialog.querySelectorAll('.settings-group')].map((node) => node.classList[1]))
    .toEqual(['settings-launch', 'settings-conversation', 'settings-reminders'])
  for (const group of dialog.querySelectorAll('.settings-group')) {
    expect(group.querySelector('p').textContent.trim().length).toBeGreaterThan(20)
  }
  const labels = [...dialog.querySelectorAll('.v-field-label, label.v-label')].map((node) => node.textContent.trim())
  for (const label of ['Model', 'Harness', 'Preset', 'Fan-out presets', 'Chat threads']) {
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
  fetchAssistantConfig.mockResolvedValue({ ...config(),
    checkin: { window_seconds: { value: 0, stored: 0, source: 'config.json' } },
    nudge: { after_seconds: { value: 1, stored: 1, source: 'config.json' },
      pending_threshold: { value: 1, stored: null, source: 'default' } },
    rotation: { context_threshold: { value: 0, stored: 0, source: 'default' } } })
  await open()
  const lines = [...document.querySelectorAll('.assistant-settings .setting-source')].map((node) => node.textContent.replace(/\s+/g, ' ').trim())
  expect(lines).toContain('In effect now: off (from config.json)')
  expect(lines).toContain('In effect now: 1 second (from config.json)')
  expect(lines).toContain('In effect now: 1 digest (from default)')
  expect(lines.some((line) => line.includes('0 seconds'))).toBe(false)
})

// Issue 646 item 9: the project tint is on unless the platform stores false.
const tintSwitch = () => [...document.querySelectorAll('.assistant-settings .tint-operator')][0]?.querySelector('input')

it('tints by default and turning it off stores an explicit false that applies at once', async () => {
  await open()
  expect(chatTintOperator.value).toBe(true)
  const input = tintSwitch()
  expect(input.checked).toBe(true)
  input.click()
  await flushPromises()
  await save()
  expect(setAssistantConfig).toHaveBeenCalledWith({ chat_tint_operator: false })
  expect(chatTintOperator.value).toBe(false)
  expect(document.body.textContent).toContain('applies now')
})

// Issue 663: Enter sends unless the platform stores false.
const enterSwitch = () => document.querySelector('.assistant-settings .enter-sends input')

it('sends on Enter by default and turning it off stores an explicit false that applies at once', async () => {
  await open({ groups: ['conversation'] })
  expect(chatEnterSends.value).toBe(true)
  const input = enterSwitch()
  expect(input.checked).toBe(true)
  input.click()
  await flushPromises()
  await save()
  expect(setAssistantConfig).toHaveBeenCalledWith({ chat_enter_sends: false })
  expect(chatEnterSends.value).toBe(false)
  expect(document.body.textContent).toContain('applies now')
})

it('reads a stored false back into the switch', async () => {
  fetchAssistantConfig.mockResolvedValue(config([], false, undefined, false))
  await open({ groups: ['conversation'] })
  expect(enterSwitch().checked).toBe(false)
  expect(chatEnterSends.value).toBe(false)
})

it('offers the launch effort beside the other launch settings', async () => {
  await open()
  const labels = [...document.querySelectorAll('.assistant-settings .v-text-field label')].map((node) => node.textContent.trim())
  expect(labels).toContain('Reasoning effort')
})

// Issue 652: Platform settings shows each group in its own card, so a card names
// the groups it carries and reports its unsaved edits to the card around it.
it('shows only the groups it is given', async () => {
  await open({ groups: ['conversation'] })
  const form = document.querySelector('.assistant-settings')
  expect([...form.querySelectorAll('.settings-group')].map((node) => node.classList[1])).toEqual(['settings-conversation'])
  expect(form.querySelector('.tint-operator')).not.toBeNull()
  expect(form.textContent).not.toContain('Model')
  expect(form.textContent).not.toContain('next incarnation')
})

it('reports unsaved edits, saves only the edit and is clean again once saved', async () => {
  await open({ groups: ['conversation'] })
  expect(wrapper.emitted('state').at(-1)).toEqual([{ dirty: false, saving: false }])
  tintSwitch().click()
  await flushPromises()
  expect(wrapper.emitted('state').at(-1)).toEqual([{ dirty: true, saving: false }])
  await save()
  expect(setAssistantConfig).toHaveBeenCalledWith({ chat_tint_operator: false })
  expect(wrapper.emitted('state').some(([state]) => state.saving)).toBe(true)
  expect(wrapper.emitted('state').at(-1)).toEqual([{ dirty: false, saving: false }])
})

it('discards an unsaved edit on reset and re-reads the stored settings', async () => {
  await open()
  const model = [...document.querySelectorAll('.assistant-settings .v-text-field')]
    .find((node) => node.textContent.includes('Model')).querySelector('input')
  model.value = 'edited'
  model.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
  expect(wrapper.emitted('state').at(-1)[0].dirty).toBe(true)
  const reads = fetchAssistantConfig.mock.calls.length
  wrapper.vm.reset()
  expect(wrapper.emitted('state').at(-1)[0].dirty).toBe(false)
  await flushPromises()
  expect(model.value).toBe('')
  expect(fetchAssistantConfig.mock.calls.length).toBe(reads + 1)
  await save()
  expect(setAssistantConfig).not.toHaveBeenCalled()
})

// What counts as an edit is what Save would send: no dirty card that Save cannot clear.
it('does not count whitespace, a cleared empty field or a leading zero as an edit', async () => {
  fetchAssistantConfig.mockResolvedValue({ ...config(), checkin: { window_seconds: { value: 30, stored: 30, source: 'config.json' } } })
  await open()
  const input = (label) => [...document.querySelectorAll('.assistant-settings .v-input')]
    .find((node) => node.querySelector('label')?.textContent.trim() === label).querySelector('input, textarea:not(.v-textarea__sizer)')
  const type = async (label, value) => {
    const field = input(label)
    field.value = value
    field.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
  }
  const dirty = () => wrapper.emitted('state').at(-1)[0].dirty
  await type('Model', '   ')
  await type('Chat threads', '\n')
  await type('Check-in window (seconds)', '030')
  expect(dirty()).toBe(false)
  await type('Model', ' opus ')
  expect(dirty()).toBe(true)
  await save()
  expect(setAssistantConfig).toHaveBeenCalledWith({ model: 'opus' })
})

it('counts a refused number as an edit, so Save can say why it is refused', async () => {
  await open({ groups: ['reminders'] })
  const field = [...document.querySelectorAll('.assistant-settings .v-input')]
    .find((node) => node.querySelector('label')?.textContent.trim() === 'Digest reminder threshold (digests)').querySelector('input')
  field.value = '0'
  field.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
  expect(wrapper.emitted('state').at(-1)[0].dirty).toBe(true)
  await save()
  expect(setAssistantConfig).not.toHaveBeenCalled()
  expect(document.querySelector('.assistant-settings').textContent).toContain('must be a whole number of 1 or more')
})

it('is not blocked by a stored value in a group it does not show', async () => {
  fetchAssistantConfig.mockResolvedValue({ ...config(), nudge: { pending_threshold: { value: 0, stored: 0, source: 'config.json' } } })
  await open({ groups: ['launch'] })
  const model = [...document.querySelectorAll('.assistant-settings .v-text-field')]
    .find((node) => node.textContent.includes('Model')).querySelector('input')
  model.value = 'opus'
  model.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
  await save()
  expect(setAssistantConfig).toHaveBeenCalledWith({ model: 'opus' })
})

it('keeps chat settings a sibling card saved when it discards from its own older reply', async () => {
  await open({ groups: ['launch'] })
  applyChatSettings(config([], false, false))
  const reading = deferred()
  fetchAssistantConfig.mockReturnValueOnce(reading.promise)
  wrapper.vm.reset()
  expect(chatTintOperator.value).toBe(false)
  reading.resolve(config([], false, false))
  await flushPromises()
  expect(chatTintOperator.value).toBe(false)
})

it('keeps an edit typed while the discard re-read is still out', async () => {
  await open({ groups: ['launch'] })
  const reading = deferred()
  fetchAssistantConfig.mockReturnValueOnce(reading.promise)
  wrapper.vm.reset()
  const model = [...document.querySelectorAll('.assistant-settings .v-text-field')]
    .find((node) => node.textContent.includes('Model')).querySelector('input')
  model.value = 'typed meanwhile'
  model.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
  reading.resolve(config())
  await flushPromises()
  expect(model.value).toBe('typed meanwhile')
  expect(wrapper.emitted('state').at(-1)[0].dirty).toBe(true)
})

// config.json is hand-editable and the reply serves its raw values: what the form
// cannot represent is not an edit, and Discard has to leave the card clean.
it('is clean on load and after a discard when stored values are ones the form cannot represent', async () => {
  const raw = config()
  raw.settings.threads.stored = [' a ']
  raw.settings.model.stored = ' opus '
  raw.checkin = { window_seconds: { value: 300, stored: '300', source: 'config.json' } }
  raw.nudge = { pending_threshold: { value: 0, stored: 0, source: 'config.json' } }
  fetchAssistantConfig.mockResolvedValue(raw)
  await open()
  const dirty = () => wrapper.emitted('state').at(-1)[0].dirty
  expect(dirty()).toBe(false)
  wrapper.vm.reset()
  expect(dirty()).toBe(false)
  await flushPromises()
  expect(dirty()).toBe(false)
  raw.settings.threads.stored = 'https://chat.example.test/thread'
  wrapper.vm.reset()
  await flushPromises()
  expect(dirty()).toBe(false)
})

it('takes another writer\'s value for an untouched field when the discard re-read lands under an edit', async () => {
  await open({ groups: ['launch'] })
  const reading = deferred()
  fetchAssistantConfig.mockReturnValueOnce(reading.promise)
  wrapper.vm.reset()
  const field = (label) => [...document.querySelectorAll('.assistant-settings .v-text-field')]
    .find((node) => node.querySelector('label')?.textContent.trim() === label).querySelector('input')
  field('Model').value = 'typed meanwhile'
  field('Model').dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
  const elsewhere = config()
  elsewhere.settings.harness.stored = 'codex'
  reading.resolve(elsewhere)
  await flushPromises()
  expect(field('Harness').value).toBe('codex')
  expect(field('Model').value).toBe('typed meanwhile')
  await save()
  expect(setAssistantConfig).toHaveBeenCalledWith({ model: 'typed meanwhile' })
})

// Save sends Number()'s reading, so any spelling of the stored number is not an edit.
it('does not count another spelling of the stored number as an edit, and Save agrees', async () => {
  fetchAssistantConfig.mockResolvedValue({ ...config(), checkin: { window_seconds: { value: 300, stored: 300, source: 'config.json' } } })
  await open({ groups: ['reminders'] })
  const field = [...document.querySelectorAll('.assistant-settings .v-input')]
    .find((node) => node.querySelector('label')?.textContent.trim() === 'Check-in window (seconds)').querySelector('input')
  const dirty = () => wrapper.emitted('state').at(-1)[0].dirty
  for (const spelling of ['300.0', '3e2', '0300']) {
    field.value = spelling
    field.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    expect(dirty(), spelling).toBe(false)
    await save()
    expect(setAssistantConfig, spelling).not.toHaveBeenCalled()
  }
  field.value = '3.01e2'
  field.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
  expect(dirty()).toBe(true)
})

it('keeps the rotation switch with its slider when a discard re-read lands under a slider edit', async () => {
  fetchAssistantConfig.mockResolvedValue({ ...config(), rotation: { context_threshold: { value: 70, stored: 70, source: 'config.json' } } })
  await open({ groups: ['reminders'] })
  const reading = deferred()
  fetchAssistantConfig.mockReturnValueOnce(reading.promise)
  wrapper.vm.reset()
  wrapper.vm.$.setupState.fields.rotationPercent = 85
  await flushPromises()
  reading.resolve({ ...config(), rotation: { context_threshold: { value: 0, stored: 0, source: 'config.json' } } })
  await flushPromises()
  expect(wrapper.emitted('state').at(-1)[0].dirty).toBe(true)
  await save()
  expect(setAssistantConfig).toHaveBeenCalledWith({ rotation_context_threshold: 85 })
})
