import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import PlatformSettings from '../src/components/PlatformSettings.vue'
import { fetchPlatformSettings, setPlatformSettings, fetchConversationRequests } from '../src/api.js'
import { mountPane, deferred } from './mount.js'

vi.mock('../src/api.js', async (original) => ({
  ...await original(), fetchPlatformSettings: vi.fn(), setPlatformSettings: vi.fn(), fetchConversationRequests: vi.fn(),
}))
const settings = { max_live_assistants: 4, max_concurrent_sessions: 8, fleet_groups: [], fleet_groups_problem: null }
const stubs = { global: { stubs: { AssistantSettings: true, IngressStatus: true, SlotEditor: true, SlotRow: true } } }
let wrapper
beforeEach(() => {
  vi.clearAllMocks()
  fetchPlatformSettings.mockResolvedValue(settings)
  fetchConversationRequests.mockResolvedValue({ requests: [] })
  setPlatformSettings.mockImplementation(async changes => ({ ...settings, ...changes }))
})
afterEach(() => { wrapper?.unmount(); wrapper = null })
async function open() {
  wrapper = mountPane(PlatformSettings, {}, stubs)
  await flushPromises()
}
function button(label) {
  return wrapper.findAll('button').find(node => (node.attributes('aria-label') || node.text()) === label)
}
async function click(label) { await button(label).trigger('click'); await flushPromises() }

it('adds, edits, orders, saves and clears graph groups without writing capacity settings', async () => {
  await open()
  expect(wrapper.get('.fleet-groups').text()).toContain('No graph groups')
  expect(wrapper.get('.fleet-groups').text()).toContain('case-insensitive text contained in the repository path')
  await click('Add group')
  await wrapper.get('.fleet-group-row input').setValue('Team')
  await wrapper.get('.fleet-group-row textarea:not(.v-textarea__sizer)').setValue(' team/ \n\ntools/team- ')
  await click('Add group')
  await wrapper.findAll('.fleet-group-row input')[1].setValue('Widget')
  await wrapper.findAll('.fleet-group-row textarea:not(.v-textarea__sizer)')[1].setValue('widget')
  await click('Move group 2 up')
  expect(wrapper.findAll('.fleet-group-row input').map(node => node.element.value)).toEqual(['Widget', 'Team'])
  expect(setPlatformSettings).not.toHaveBeenCalled()
  await click('Save groups')
  expect(setPlatformSettings).toHaveBeenLastCalledWith({ fleet_groups: [
    { name: 'Widget', patterns: ['widget'] }, { name: 'Team', patterns: ['team/', 'tools/team-'] },
  ] })
  expect(wrapper.emitted('changed')).toHaveLength(1)
  expect(wrapper.get('[role="status"]').text()).toBe('Graph groups saved.')
  await click('Move group 1 down')
  await wrapper.findAll('.fleet-group-row input')[0].setValue('Renamed')
  await click('Remove group 2')
  await click('Save groups')
  expect(setPlatformSettings).toHaveBeenLastCalledWith({ fleet_groups: [{ name: 'Renamed', patterns: ['team/', 'tools/team-'] }] })
  await click('Remove group 1')
  await click('Save groups')
  expect(setPlatformSettings).toHaveBeenLastCalledWith({ fleet_groups: [] })
})

it('loads stored order and retains edits across state polls and failed saves', async () => {
  fetchPlatformSettings.mockResolvedValue({ ...settings, fleet_groups: [{ name: 'Team', patterns: ['team/', 'tools/team-'] }] })
  await open()
  expect(wrapper.get('.fleet-group-row textarea:not(.v-textarea__sizer)').element.value).toBe('team/\ntools/team-')
  await wrapper.get('.fleet-group-row input').setValue('Edited')
  await wrapper.setProps({ state: { at: 'a new poll' } })
  await flushPromises()
  expect(wrapper.get('.fleet-group-row input').element.value).toBe('Edited')
  setPlatformSettings.mockRejectedValueOnce(new Error('Group 1 must have 1–16 text entries.'))
  await click('Save groups')
  expect(wrapper.get('.fleet-groups .v-alert').text()).toContain('Group 1 must have')
  expect(wrapper.emitted('changed')).toBeUndefined()
  expect(wrapper.get('.fleet-group-row input').element.value).toBe('Edited')
  await wrapper.get('.fleet-group-row textarea:not(.v-textarea__sizer)').setValue('catalog/')
  expect(wrapper.find('.fleet-groups .v-alert').exists()).toBe(false)
  await click('Save groups')
  expect(wrapper.emitted('changed')).toHaveLength(1)
})

it('prevents saving before settings load and exposes a broken stored configuration for repair', async () => {
  const loading = deferred()
  fetchPlatformSettings.mockReturnValue(loading.promise)
  await open()
  expect(button('Save groups').attributes('disabled')).toBeDefined()
  expect(button('Add group').attributes('disabled')).toBeDefined()
  loading.resolve({ ...settings, fleet_groups_problem: 'fleet_groups must be a list of at most 32 groups.' })
  await flushPromises()
  expect(wrapper.get('.fleet-groups .v-alert').text()).toContain('fleet_groups must be a list')
  await click('Save groups')
  expect(setPlatformSettings).toHaveBeenLastCalledWith({ fleet_groups: [] })
  expect(wrapper.find('.fleet-groups .v-alert').exists()).toBe(false)
})

it('locks group editing during a save and does not overwrite a failed initial read', async () => {
  fetchPlatformSettings.mockRejectedValueOnce(new Error('Settings unavailable'))
  await open()
  expect(button('Save groups').attributes('disabled')).toBeDefined()
  expect(wrapper.text()).toContain('Settings unavailable')
  wrapper.unmount()
  await open()
  await click('Add group')
  const saving = deferred()
  setPlatformSettings.mockReturnValueOnce(saving.promise)
  await click('Save groups')
  expect(wrapper.get('.fleet-group-row input').attributes('disabled')).toBeDefined()
  expect(button('Remove group 1').attributes('disabled')).toBeDefined()
  saving.resolve(settings)
  await flushPromises()
})
