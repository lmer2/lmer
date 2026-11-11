import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import PlatformSettings from '../src/components/PlatformSettings.vue'
import TimeSettings from '../src/components/TimeSettings.vue'
import * as api from '../src/api.js'
import { mountPane } from './mount.js'

vi.mock('../src/api.js', async importOriginal => ({
  ...await importOriginal(), fetchTimeSettings: vi.fn(), addTimePerson: vi.fn(), updateTimePerson: vi.fn(),
  removeTimePerson: vi.fn(), updateTimeDefaults: vi.fn(), fetchPlatformSettings: vi.fn(),
  fetchConversationRequests: vi.fn(),
}))

const number = (value, extra = {}) => ({ value, source: 'default', stored: null, env: 'LMER_X', kind: 'int',
  default: value, minimum: 0, ...extra })

function settings(overrides = {}) {
  return {
    tracking: { switch: true, runs: true, problems: [], live: true },
    people: [{ key: 'pat', name: 'Pat', slack: ['U0PAT'] }, { key: 'sam', name: 'Sam', matrix: ['@sam:example.org'] }],
    default_person: { stored: 'pat', effective: 'pat' },
    defaults: {
      idle_grace_minutes: number(15, { minimum: 1 }),
      min_entry_seconds: number(60),
      pause_hours: number(2, { source: 'env', env: 'LMER_PLATFORM_TIME_PAUSE_HOURS', default: 0 }),
      title_max_age_days: number(30, { minimum: 1, source: 'config.json', stored: 30 }),
      sweep_seconds: number(30, { minimum: 1 }),
      writer_name: { value: 'lmer timesheet', source: 'default', stored: null, kind: 'text', default: 'lmer timesheet' },
      writer_email: { value: 'timesheet@lmer.invalid', source: 'default', stored: null, kind: 'text',
        default: 'timesheet@lmer.invalid' },
    },
    projects: [
      { workspace_id: 'alpha', name: 'Alpha', idle_grace_minutes: 5, effective_idle_grace_minutes: 5 },
      { workspace_id: 'beta', name: 'Beta', idle_grace_minutes: null, effective_idle_grace_minutes: 15 },
    ],
    unmapped: ['slack:U0NEW'],
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  api.fetchTimeSettings.mockResolvedValue(settings())
  for (const call of ['addTimePerson', 'updateTimePerson', 'removeTimePerson', 'updateTimeDefaults']) {
    api[call].mockResolvedValue({ ...settings(), applied: 'updated' })
  }
})

async function mounted() {
  const wrapper = mountPane(TimeSettings, {})
  await flushPromises()
  return wrapper
}

const button = (wrapper, text) => wrapper.findAll('button').find(item => item.text() === text)

it('shows the switch, the people, where each default comes from and what each project inherits', async () => {
  const wrapper = await mounted()
  expect(wrapper.get('.time-settings-state').text()).toBe('Running.')
  expect(wrapper.findAll('.time-person').map(row => row.attributes('data-person'))).toEqual(['pat', 'sam'])
  expect(wrapper.get('.time-person[data-person="sam"]').text()).toContain('@sam:example.org')
  expect(wrapper.get('[data-setting="pause_hours"]').text()).toContain('LMER_PLATFORM_TIME_PAUSE_HOURS is exported and wins: 2')
  expect(wrapper.get('[data-setting="title_max_age_days"] input').element.value).toBe('30')
  expect(wrapper.get('[data-setting="idle_grace_minutes"] input').element.value).toBe('')
  expect(wrapper.get('.time-inherits [data-project="alpha"]').text()).toContain('5 min')
  expect(wrapper.get('.time-inherits [data-project="beta"]').text()).toContain('inherits the default')
  expect(wrapper.text()).not.toMatch(/glpat|token:/i)
})

it('says why tracking is off', async () => {
  api.fetchTimeSettings.mockResolvedValue(settings({ people: [],
    tracking: { switch: true, runs: false, problems: [], live: false } }))
  expect((await mounted()).get('.time-settings-state').text()).toContain('nobody is listed')
  api.fetchTimeSettings.mockResolvedValue(settings({
    tracking: { switch: true, runs: false, problems: ['time_pause_hours is unusable (-1)'], live: false } }))
  const broken = await mounted()
  expect(broken.get('.time-settings-problems').text()).toContain('time_pause_hours is unusable')
})

it('switches tracking off and says what the daemon did', async () => {
  api.updateTimeDefaults.mockResolvedValue({ ...settings({ tracking: { switch: false, runs: false, problems: [], live: false } }),
    applied: 'stopped' })
  const wrapper = await mounted()
  await wrapper.getComponent('.time-settings-switch').vm.$emit('update:modelValue', false)
  await flushPromises()
  expect(api.updateTimeDefaults).toHaveBeenCalledWith({ tracking: false })
  expect(wrapper.get('.time-settings-applied').text()).toBe('Time tracking stopped.')
  expect(wrapper.get('.time-settings-state').text()).toContain('switched off')
})

it('adds a person with their ids', async () => {
  const wrapper = await mounted()
  await wrapper.get('.time-person-new').trigger('click')
  await wrapper.get('.time-person-key input').setValue(' lee ')
  await wrapper.getComponent('.time-person-add .v-combobox').vm.$emit('update:modelValue', ['U0LEE', ' '])
  await wrapper.get('.time-person-add-save').trigger('click')
  await flushPromises()
  expect(api.addTimePerson).toHaveBeenCalledWith({ key: 'lee', name: undefined, slack: ['U0LEE'], matrix: [] })
  expect(wrapper.find('.time-person-add').exists()).toBe(false)
})

it('edits a person in place, sending the lists as the form has them', async () => {
  const wrapper = await mounted()
  await wrapper.get('.time-person[data-person="pat"] .time-person-edit').trigger('click')
  await wrapper.get('.time-person-editing input').setValue('Pat P.')
  await wrapper.getComponent('.time-person-slack').vm.$emit('update:modelValue', ['U0PAT', 'U0PAT2'])
  await wrapper.get('.time-person-save').trigger('click')
  await flushPromises()
  expect(api.updateTimePerson).toHaveBeenCalledWith('pat', { name: 'Pat P.', slack: ['U0PAT', 'U0PAT2'], matrix: [] })
})

it('asks before removing a person', async () => {
  const wrapper = await mounted()
  await wrapper.get('.time-person[data-person="sam"] .time-person-remove').trigger('click')
  expect(api.removeTimePerson).not.toHaveBeenCalled()
  expect(wrapper.get('.time-person-confirm').text()).toContain('Remove sam?')
  await wrapper.get('.time-person-remove-confirm').trigger('click')
  await flushPromises()
  expect(api.removeTimePerson).toHaveBeenCalledWith('sam')
})

it('adds an unlisted id to a person in one click, or starts a new person with it', async () => {
  const wrapper = await mounted()
  await wrapper.get('.time-unmapped-add').trigger('click')
  await flushPromises()
  document.querySelector('.v-overlay .v-list-item[data-person="sam"]').click()
  await flushPromises()
  expect(api.updateTimePerson).toHaveBeenCalledWith('sam', { add: ['slack:U0NEW'] })

  await wrapper.get('.time-unmapped-add').trigger('click')
  await flushPromises()
  document.querySelector('.v-overlay .time-unmapped-new').click()
  await flushPromises()
  expect(wrapper.getComponent('.time-person-add .v-combobox').props('modelValue')).toEqual(['U0NEW'])
})

it('sets the default person, and nobody clears it', async () => {
  const wrapper = await mounted()
  await wrapper.getComponent('.time-settings-default').vm.$emit('update:modelValue', 'sam')
  await flushPromises()
  expect(api.updateTimeDefaults).toHaveBeenLastCalledWith({ default_person: 'sam' })
  await wrapper.getComponent('.time-settings-default').vm.$emit('update:modelValue', '')
  await flushPromises()
  expect(api.updateTimeDefaults).toHaveBeenLastCalledWith({ default_person: null })
})

it('saves only the defaults that changed, an emptied one as null, and marks the card unsaved meanwhile', async () => {
  const wrapper = await mounted()
  expect(button(wrapper, 'Save').attributes('disabled')).toBeDefined()
  await wrapper.get('[data-setting="idle_grace_minutes"] input').setValue('20')
  await wrapper.get('[data-setting="title_max_age_days"] input').setValue('')
  expect(wrapper.emitted('state').at(-1)).toEqual([{ dirty: true, saving: false }])
  expect(wrapper.find('.settings-unsaved').exists()).toBe(true)
  await button(wrapper, 'Save').trigger('click')
  await flushPromises()
  expect(api.updateTimeDefaults).toHaveBeenCalledWith({ idle_grace_minutes: '20', title_max_age_days: null })
  expect(wrapper.get('.time-settings-applied').text()).toBe('Applied to the running daemon.')
})

it('shows the daemon\'s refusal and keeps the edit', async () => {
  api.updateTimeDefaults.mockRejectedValue(Object.assign(new Error('idle_grace_minutes: an automatic stop needs at least a minute of grace'), { status: 400 }))
  const wrapper = await mounted()
  await wrapper.get('[data-setting="idle_grace_minutes"] input').setValue('0')
  await button(wrapper, 'Save').trigger('click')
  await flushPromises()
  expect(wrapper.get('.time-defaults-error').text()).toContain('at least a minute')
  expect(wrapper.get('[data-setting="idle_grace_minutes"] input').element.value).toBe('0')
})

it('is a section of the platform settings, read only once it is opened', async () => {
  api.fetchPlatformSettings.mockResolvedValue({})
  api.fetchConversationRequests.mockResolvedValue({ requests: [] })
  const wrapper = mountPane(PlatformSettings, { state: { runs: [] }, projects: [], slots: [] })
  await flushPromises()
  expect(api.fetchTimeSettings).not.toHaveBeenCalled()
  await wrapper.get('.settings-nav-item[data-section="time"]').trigger('click')
  await flushPromises()
  expect(api.fetchTimeSettings).toHaveBeenCalledTimes(1)
  expect(wrapper.get('[data-panel="time"]').text()).toContain('People')
})

it('keeps an unsaved default when a people edit or the switch answers', async () => {
  const wrapper = await mounted()
  await wrapper.get('[data-setting="idle_grace_minutes"] input').setValue('20')
  await wrapper.getComponent('.time-settings-switch').vm.$emit('update:modelValue', false)
  await flushPromises()
  expect(api.updateTimeDefaults).toHaveBeenCalledWith({ tracking: false })
  expect(wrapper.get('[data-setting="idle_grace_minutes"] input').element.value).toBe('20')
  expect(wrapper.find('.settings-unsaved').exists()).toBe(true)
})

it('keeps a default typed while the save was in flight, still unsaved', async () => {
  let answer
  api.updateTimeDefaults.mockReturnValue(new Promise(resolve => { answer = resolve }))
  const wrapper = await mounted()
  await wrapper.get('[data-setting="idle_grace_minutes"] input').setValue('20')
  await button(wrapper, 'Save').trigger('click')
  await wrapper.get('[data-setting="idle_grace_minutes"] input').setValue('25')
  answer({ ...settings({ defaults: { ...settings().defaults,
    idle_grace_minutes: number(20, { minimum: 1, source: 'config.json', stored: 20 }) } }), applied: 'updated' })
  await flushPromises()
  expect(api.updateTimeDefaults).toHaveBeenCalledWith({ idle_grace_minutes: '20' })
  expect(wrapper.get('[data-setting="idle_grace_minutes"] input').element.value).toBe('25')
  expect(wrapper.find('.settings-unsaved').exists()).toBe(true)
})
