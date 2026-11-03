import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import PlatformSettings from '../src/components/PlatformSettings.vue'
import { fetchPlatformSettings, setPlatformSettings, fetchConversationRequests } from '../src/api.js'
import { dockKeepLastNotes } from '../src/operatorChat.js'
import { mountPane, deferred } from './mount.js'

vi.mock('../src/api.js', async (original) => ({
  ...await original(), fetchPlatformSettings: vi.fn(), setPlatformSettings: vi.fn(), fetchConversationRequests: vi.fn(),
}))
const settings = { max_live_assistants: 4, max_concurrent_sessions: 8, fleet_groups: [], fleet_groups_problem: null }
// Each form records its own groups on save and reset, so a test can tell the three apart.
const formCalls = []
const assistantForm = { template: '<div class="assistant-form" />', props: ['groups', 'allowRestart'], emits: ['state'],
  methods: { save() { formCalls.push(['save', this.groups]) }, reset() { formCalls.push(['reset', this.groups]) } } }
const stubs = { global: { stubs: { AssistantSettings: assistantForm, IngressStatus: true, SlotEditor: true, SlotRow: true } } }
let wrapper
beforeEach(() => {
  vi.clearAllMocks()
  formCalls.length = 0
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

// Every run's operator channel dock (issue 465): a global display preference,
// edited in its own card rather than in the assistant's settings.
describe('operator channel dock display', () => {
  const dockSetting = value => ({ ...settings, dock: { keep_last_notes: { value, stored: value, source: value === null ? 'default' : 'config.json' } } })
  const field = () => wrapper.get('.dock-display input')

  it('loads the setting, saves N, 0 and empty as N, 0 and null, and applies it to this browser', async () => {
    fetchPlatformSettings.mockResolvedValue(dockSetting(null))
    setPlatformSettings.mockImplementation(async changes => dockSetting(changes.dock_keep_last_notes))
    await open()
    expect(wrapper.get('.dock-display').text()).toContain('Operator channel dock: keep last N notes')
    expect(wrapper.get('.dock-display').text()).toContain('0 shows open questions only')
    expect(field().element.value).toBe('')
    for (const [typed, sent] of [['3', 3], ['0', 0], ['', null]]) {
      await field().setValue(typed)
      await click('Save dock display')
      expect(setPlatformSettings).toHaveBeenLastCalledWith({ dock_keep_last_notes: sent })
      expect(dockKeepLastNotes.value).toBe(sent)
      expect(field().element.value).toBe(sent === null ? '' : String(sent))
      expect(wrapper.get('.dock-display [role="status"]').text()).toBe('Dock display saved.')
    }
    expect(wrapper.emitted('changed')).toBeUndefined()
  })

  it('refuses a negative or fractional count before saving and shows a refused save', async () => {
    fetchPlatformSettings.mockResolvedValue(dockSetting(2))
    await open()
    expect(field().element.value).toBe('2')
    for (const typed of ['-1', '1.5']) {
      await field().setValue(typed)
      await click('Save dock display')
      expect(setPlatformSettings).not.toHaveBeenCalled()
      expect(wrapper.get('.dock-display .v-alert').text()).toContain('whole number of 0 or more')
    }
    setPlatformSettings.mockRejectedValueOnce(new Error('dock_keep_last_notes is unusable'))
    await field().setValue('4')
    await click('Save dock display')
    expect(wrapper.get('.dock-display .v-alert').text()).toContain('dock_keep_last_notes is unusable')
    expect(field().element.value).toBe('4')
  })
})

// Issue 652: a section nav, one card style, Save enabled only for an edited card,
// and a question before unsaved edits are thrown away.
describe('sections, dirty state and leaving', () => {
  const nav = () => wrapper.findAll('.settings-nav button')
  const navButton = title => nav().find(node => node.text().startsWith(title))
  const panel = id => wrapper.get(`[data-panel="${id}"]`)
  const saveCapacity = () => button('Save capacity')
  const leaveDialog = () => document.querySelector('.settings-leave')
  const dialogButton = label => [...document.querySelectorAll('.settings-leave button')].find(node => node.textContent.trim() === label)
  const capacity = () => wrapper.get('#worker-capacity')

  it('reaches every section from the nav in one click and shows only that one', async () => {
    await open()
    expect(nav().map(node => node.text())).toEqual(
      ['Fleet', 'Assistants', 'Chat and display', 'Graph', 'Service slots', 'Projects', 'Inbound chat'])
    expect(wrapper.get('.settings-picker').exists()).toBe(true)
    for (const [title, id] of [['Graph', 'graph'], ['Chat and display', 'chat'], ['Inbound chat', 'inbound'], ['Fleet', 'fleet']]) {
      await navButton(title).trigger('click')
      await flushPromises()
      expect(panel(id).isVisible()).toBe(true)
      expect(wrapper.findAll('[data-panel]').filter(node => node.isVisible())).toHaveLength(1)
      expect(navButton(title).attributes('aria-current')).toBe('page')
      expect(document.activeElement).toBe(panel(id).get('.settings-section-title').element)
    }
    expect(nav().filter(node => node.attributes('aria-current'))).toHaveLength(1)
  })

  it('gives every card a title and a one-line description, and says when a list is empty', async () => {
    await open()
    const cards = wrapper.findAll('.settings-section')
    expect(cards.map(card => card.get('h2').text())).toEqual(['Capacity', 'Launch defaults', 'Reminders', 'Rotate all',
      'Chat display', 'Operator channel dock', 'Graph groups', 'Service slots', 'Archived projects', 'Message ingress',
      'Conversation requests needing attention'])
    for (const card of cards) {
      expect(card.attributes('aria-labelledby')).toBe(card.get('h2').attributes('id'))
      expect(card.get('.settings-section-description').text().length).toBeGreaterThan(20)
    }
    expect(panel('projects').text()).toContain('No archived projects.')
    expect(panel('inbound').text()).toContain('No conversation requests need attention.')
    expect(panel('slots').findAll('button').map(node => node.text())).toEqual(['Add slot'])
  })

  it('enables Save only for an edited card, marks the card and its section, and saves the capacity', async () => {
    await open()
    expect(saveCapacity().attributes('disabled')).toBeDefined()
    expect(button('Save groups').attributes('disabled')).toBeDefined()
    expect(wrapper.find('.settings-unsaved').exists()).toBe(false)
    await capacity().setValue('12')
    expect(saveCapacity().attributes('disabled')).toBeUndefined()
    expect(panel('fleet').get('.settings-unsaved').text()).toBe('Unsaved')
    expect(navButton('Fleet').find('.settings-dirty-dot').exists()).toBe(true)
    expect(navButton('Fleet').text()).toContain('unsaved changes')
    expect(navButton('Graph').find('.settings-dirty-dot').exists()).toBe(false)
    await capacity().setValue('8')
    expect(saveCapacity().attributes('disabled')).toBeDefined()
    await capacity().setValue('12')
    await click('Save capacity')
    expect(setPlatformSettings).toHaveBeenLastCalledWith({ max_live_assistants: 4, max_concurrent_sessions: 12 })
    expect(wrapper.emitted('changed')).toHaveLength(1)
    expect(saveCapacity().attributes('disabled')).toBeDefined()
    expect(wrapper.find('.settings-unsaved').exists()).toBe(false)
    expect(panel('fleet').get('[role="status"]').text()).toBe('Capacity saved.')
  })

  it('keeps a capacity edit typed while Save was in flight, and counts it as unsaved', async () => {
    await open()
    const saving = deferred()
    setPlatformSettings.mockReturnValueOnce(saving.promise)
    await capacity().setValue('12')
    await button('Save capacity').trigger('click')
    await capacity().setValue('13')
    saving.resolve({ ...settings, max_concurrent_sessions: 12 })
    await flushPromises()
    expect(capacity().element.value).toBe('13')
    expect(panel('fleet').find('.settings-unsaved').exists()).toBe(true)
    expect(saveCapacity().attributes('disabled')).toBeUndefined()
    await capacity().setValue('012')
    expect(panel('fleet').find('.settings-unsaved').exists()).toBe(false)
  })

  it('does not count another spelling of the saved capacity as an edit', async () => {
    await open()
    for (const spelling of ['8.0', '8e0', '008']) {
      await capacity().setValue(spelling)
      expect(panel('fleet').find('.settings-unsaved').exists(), spelling).toBe(false)
    }
    await capacity().setValue('12')
    await click('Save capacity')
    expect(setPlatformSettings).toHaveBeenLastCalledWith({ max_live_assistants: 4, max_concurrent_sessions: 12 })
    for (const spelling of ['12.0', '1.2e1', '012']) {
      await capacity().setValue(spelling)
      expect(panel('fleet').find('.settings-unsaved').exists(), spelling).toBe(false)
      expect(saveCapacity().attributes('disabled'), spelling).toBeDefined()
    }
    expect(panel('fleet').get('[role="status"]').text()).toBe('Capacity saved.')
  })

  it('keeps a failed capacity save unsaved and editable', async () => {
    await open()
    setPlatformSettings.mockRejectedValueOnce(new Error('max_concurrent_sessions must be at least 1'))
    await capacity().setValue('0')
    await click('Save capacity')
    expect(wrapper.text()).toContain('max_concurrent_sessions must be at least 1')
    expect(capacity().element.value).toBe('0')
    expect(panel('fleet').find('.settings-unsaved').exists()).toBe(true)
  })

  it('asks before switching away from unsaved edits, and keeps or discards them as told', async () => {
    await open()
    await capacity().setValue('12')
    await navButton('Graph').trigger('click')
    await flushPromises()
    expect(leaveDialog().textContent).toContain('Fleet has unsaved changes.')
    dialogButton('Keep editing').click()
    await flushPromises()
    expect(panel('fleet').isVisible()).toBe(true)
    expect(capacity().element.value).toBe('12')
    await navButton('Graph').trigger('click')
    await flushPromises()
    dialogButton('Discard').click()
    await flushPromises()
    expect(panel('graph').isVisible()).toBe(true)
    expect(capacity().element.value).toBe('8')
    expect(navButton('Fleet').find('.settings-dirty-dot').exists()).toBe(false)
    expect(setPlatformSettings).not.toHaveBeenCalled()
  })

  it('discards edited graph groups back to the stored list', async () => {
    fetchPlatformSettings.mockResolvedValue({ ...settings, fleet_groups: [{ name: 'Team', patterns: ['team/'] }] })
    await open()
    await navButton('Graph').trigger('click')
    await click('Add group')
    await click('Remove group 1')
    expect(panel('graph').find('.settings-unsaved').exists()).toBe(true)
    await navButton('Fleet').trigger('click')
    await flushPromises()
    dialogButton('Discard').click()
    await flushPromises()
    expect(wrapper.findAll('.fleet-group-row input').map(node => node.element.value)).toEqual(['Team'])
    expect(panel('graph').find('.settings-unsaved').exists()).toBe(false)
  })

  it('lets the shell ask before leaving, and guards a tab close only while edits are unsaved', async () => {
    await open()
    expect(wrapper.vm.hasUnsavedChanges()).toBe(false)
    await expect(wrapper.vm.confirmLeave()).resolves.toBe(true)
    const unload = () => { const event = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented }
    expect(unload()).toBe(false)
    await wrapper.get('.dock-display input').setValue('3')
    expect(wrapper.vm.hasUnsavedChanges()).toBe(true)
    expect(unload()).toBe(true)
    const leaving = wrapper.vm.confirmLeave()
    await flushPromises()
    expect(leaveDialog().textContent).toContain('Chat and display has unsaved changes.')
    dialogButton('Discard').click()
    await expect(leaving).resolves.toBe(true)
    expect(wrapper.get('.dock-display input').element.value).toBe('')
    expect(wrapper.vm.hasUnsavedChanges()).toBe(false)
  })

  it('saves and discards the assistant forms through their own cards', async () => {
    await open()
    const forms = wrapper.findAllComponents(assistantForm)
    expect(forms.map(form => form.props('groups'))).toEqual([['launch'], ['reminders'], ['conversation']])
    expect(button('Save launch defaults').attributes('disabled')).toBeDefined()
    forms[0].vm.$emit('state', { dirty: true, saving: false })
    await flushPromises()
    expect(navButton('Assistants').find('.settings-dirty-dot').exists()).toBe(true)
    await click('Save launch defaults')
    expect(formCalls).toEqual([['save', ['launch']]])
    forms[0].vm.$emit('state', { dirty: true, saving: true })
    await flushPromises()
    expect(wrapper.findAllComponents({ name: 'VBtn' }).find(node => node.text() === 'Save launch defaults').props('loading')).toBe(true)
    forms[0].vm.$emit('state', { dirty: true, saving: false })
    forms[2].vm.$emit('state', { dirty: true, saving: false })
    await flushPromises()
    const leaving = wrapper.vm.confirmLeave()
    await flushPromises()
    expect(leaveDialog().textContent).toContain('Assistants, Chat and display has unsaved changes.')
    dialogButton('Discard').click()
    await expect(leaving).resolves.toBe(true)
    expect(formCalls.filter(([call]) => call === 'reset').map(([, groups]) => groups))
      .toEqual([['launch'], ['reminders'], ['conversation']])
  })

  it('keeps the repair path for a broken stored list after discarding an edit to it', async () => {
    fetchPlatformSettings.mockResolvedValue({ ...settings, fleet_groups_problem: 'fleet_groups must be a list of at most 32 groups.' })
    await open()
    await navButton('Graph').trigger('click')
    await click('Add group')
    await navButton('Fleet').trigger('click')
    await flushPromises()
    dialogButton('Discard').click()
    await flushPromises()
    await navButton('Graph').trigger('click')
    await flushPromises()
    expect(panel('graph').get('.v-alert').text()).toContain('fleet_groups must be a list')
    expect(button('Save groups').attributes('disabled')).toBeUndefined()
    await click('Save groups')
    expect(setPlatformSettings).toHaveBeenLastCalledWith({ fleet_groups: [] })
  })

  it('waits for a save in flight instead of offering to discard it', async () => {
    await open()
    const saving = deferred()
    setPlatformSettings.mockReturnValueOnce(saving.promise)
    await capacity().setValue('12')
    await click('Save capacity')
    await navButton('Graph').trigger('click')
    await flushPromises()
    expect(leaveDialog()).toBeNull()
    expect(panel('fleet').isVisible()).toBe(true)
    saving.resolve({ ...settings, max_concurrent_sessions: 12 })
    await flushPromises()
    expect(leaveDialog()).toBeNull()
    expect(panel('graph').isVisible()).toBe(true)
    expect(capacity().element.value).toBe('12')
  })

  it('asks after a save in flight fails, and clears the save error on discard', async () => {
    await open()
    const saving = deferred()
    setPlatformSettings.mockReturnValueOnce(saving.promise)
    await capacity().setValue('12')
    await click('Save capacity')
    await navButton('Graph').trigger('click')
    saving.reject(new Error('refused'))
    await flushPromises()
    expect(leaveDialog().textContent).toContain('Fleet has unsaved changes.')
    expect(wrapper.text()).toContain('refused')
    dialogButton('Discard').click()
    await flushPromises()
    expect(wrapper.text()).not.toContain('refused')
  })

  it('returns focus to the control that asked when the operator keeps editing', async () => {
    await open()
    await capacity().setValue('12')
    navButton('Graph').element.focus()
    await navButton('Graph').trigger('click')
    await flushPromises()
    dialogButton('Keep editing').click()
    await flushPromises()
    expect(document.activeElement).toBe(navButton('Graph').element)
  })

  it('opens on the service slots when the fleet asks for them', async () => {
    wrapper = mountPane(PlatformSettings, { focusSection: 'slots', state: { slots: [] } }, stubs)
    await flushPromises()
    expect(panel('slots').isVisible()).toBe(true)
    expect(document.activeElement.id).toBe('platform-service-slots')
    expect(panel('slots').text()).toContain('No service slots configured.')
  })

  it('uses the button hierarchy: filled primary Save, tonal secondary, error for removal', async () => {
    fetchPlatformSettings.mockResolvedValue({ ...settings, fleet_groups: [{ name: 'Team', patterns: ['team/'] }] })
    await open()
    const props = label => wrapper.findAllComponents({ name: 'VBtn' }).find(node => (node.attributes('aria-label') || node.text()) === label).props()
    for (const label of ['Save capacity', 'Save groups', 'Save dock display']) {
      expect([props(label).color, props(label).variant]).toEqual(['primary', 'flat'])
    }
    for (const label of ['Add group', 'Add slot', 'Refresh requests', 'Move group 1 up']) expect(props(label).variant).toBe('tonal')
    expect([props('Remove group 1').color, props('Remove group 1').variant]).toEqual(['error', 'tonal'])
  })
})
