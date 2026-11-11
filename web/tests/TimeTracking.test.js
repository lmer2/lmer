import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import App from '../src/App.vue'
import ProjectSettings from '../src/components/ProjectSettings.vue'
import RunDetail from '../src/components/RunDetail.vue'
import TimeEntries from '../src/components/TimeEntries.vue'
import TimeIndicator from '../src/components/TimeIndicator.vue'
import TimeTrackingBar from '../src/components/TimeTrackingBar.vue'
import * as api from '../src/api.js'
import { contrastRatio, projectColor, projectColors, runningAccent } from '../src/projects.js'
import { dispatchRunRef, parseRunRef } from '../src/runref.js'
import { chooseProject, deferred, mountPane } from './mount.js'

vi.mock('../src/api.js', async importOriginal => ({
  ...await importOriginal(), fetchState: vi.fn(), fetchProjects: vi.fn(), fetchAssistantConfig: vi.fn(),
  fetchAssistantInstructions: vi.fn(), fetchChannelRoutes: vi.fn(), updateProject: vi.fn(),
  fetchTimeStatus: vi.fn(), startTime: vi.fn(), stopTime: vi.fn(), updateTime: vi.fn(),
  setTimeAutomatic: vi.fn(), fetchTimeEntries: vi.fn(),
}))

const board = {
  name: 'tasks', label: 'Task board', sync: true,
  fields: [{ key: 'task_id', label: 'Board task', required: true, pattern: '[a-z0-9]{6,}',
    hint: 'The id at the end of the task URL', link: 'https://tasks.example.test/t/{value}' }],
}
const timesheet = { name: 'timesheet', label: 'Timesheet', sync: false, fields: [] }
const projects = [
  { id: 'alpha', name: 'Alpha', color: 'Blue', repositories: [], settings: { time_clients: ['acme'] },
    assistant: { lifecycle: 'running' } },
  { id: 'beta', name: 'Beta', color: 'Teal', repositories: [], settings: {}, assistant: { lifecycle: 'running' } },
]

function option(project, handler, clients, recent) {
  return { workspace_id: project.id, name: project.name, archived: false, project_key: project.id, handler,
    handler_warning: null, clients, idle_grace_minutes: null, recent }
}

function status(overrides = {}) {
  return {
    person: 'pat', enabled: true, problems: [], running: null, warnings: [], prompts: [], pause: null,
    automatic: { on: true, off_since: null },
    outbox: { pending: 0, last_error: null }, writer_refusal: null,
    acting: { person: 'pat', name: 'Pat', via: 'default_person', record: null, request_id: null },
    projects: {
      alpha: option(projects[0], board, ['acme'],
        { titles: ['Checkout triage'], clients: ['acme'], fields: { task_id: ['abc123x', 'def456y'] } }),
      beta: option(projects[1], timesheet, [], { titles: [], clients: [], fields: {} }),
    },
    handlers: [timesheet, board], handler_problems: [], unmapped: [],
    ...overrides,
  }
}

function running(overrides = {}) {
  return { workspace_id: 'alpha', project_key: 'alpha', project_name: 'Alpha', mode: 'manual',
    start: '2026-10-29T09:00:00Z', last_activity: '2026-10-29T09:00:00Z', start_trigger: 'manual',
    handler: 'tasks', client: 'acme', title: 'Checkout triage', fields: { task_id: 'abc123x' }, run_name: null,
    elapsed_seconds: 65, idle_at: null, ...overrides }
}

const refusal = (status, code, message) => Object.assign(new Error(message), { status, code })

const phoneWidth = window.innerWidth

// Desktop width unless a test says otherwise: a phone's header splits the indicator
// across two rows (see the last header test).
function mountApp({ width = 1440 } = {}) {
  window.innerWidth = width
  return mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
}

afterEach(() => { window.innerWidth = phoneWidth })

// A poll outside the interval: the fleet refetches when the window is focused.
async function poll() {
  window.dispatchEvent(new Event('focus'))
  await flushPromises()
}

const bar = (wrapper) => wrapper.get('#time-tracking-bar')
const shown = (element) => element.element.style.display !== 'none'
const button = (wrapper, label) => wrapper.findAll('button').find(item => item.text() === label)

beforeEach(() => {
  vi.clearAllMocks()
  window.history.replaceState({}, '', '/')
  window.localStorage.clear()
  api.fetchState.mockResolvedValue({ runs: [], attention: [], totals: {}, relations: [] })
  api.fetchProjects.mockResolvedValue({ projects: structuredClone(projects) })
  api.fetchAssistantConfig.mockResolvedValue({})
  api.fetchAssistantInstructions.mockResolvedValue({ instructions: null })
  api.fetchChannelRoutes.mockResolvedValue({ routes: [] })
  api.updateProject.mockResolvedValue({})
  api.fetchTimeStatus.mockResolvedValue(status())
  api.startTime.mockResolvedValue({ ok: true, outcome: 'started' })
  api.stopTime.mockResolvedValue({ ok: true, outcome: 'stopped' })
  api.updateTime.mockResolvedValue({ ok: true, outcome: 'updated' })
  api.setTimeAutomatic.mockResolvedValue({ ok: true, outcome: 'updated' })
  api.fetchTimeEntries.mockResolvedValue({ person: 'pat', rows: [], count: 0, filters: {}, problems: [] })
})

it('opens and closes the time-tracking bar from the clock in the header', async () => {
  const wrapper = mountApp()
  await flushPromises()
  const clock = wrapper.get('header .time-toggle')
  expect(clock.attributes('aria-label')).toBe('show time tracking')
  expect(clock.attributes('aria-expanded')).toBe('false')
  expect(clock.attributes('aria-describedby')).toBeTruthy()
  expect(shown(bar(wrapper))).toBe(false)
  await clock.trigger('click')
  expect(shown(bar(wrapper))).toBe(true)
  expect(clock.attributes('aria-expanded')).toBe('true')
  expect(clock.attributes('aria-label')).toBe('hide time tracking')
  await clock.trigger('click')
  expect(shown(bar(wrapper))).toBe(false)
})

it('keeps the bar open while a timer runs, its time line ticking, and the clock opens the editor', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-29T09:01:05Z'))
  api.fetchTimeStatus.mockResolvedValue(status({ running: running() }))
  const wrapper = mountApp()
  await flushPromises()
  expect(shown(bar(wrapper))).toBe(true)
  expect(wrapper.get('header').text()).not.toContain('0:01:05')
  const slim = wrapper.get('#time-tracking-bar .time-bar-slim')
  expect(slim.get('.time-project').text()).toBe('Alpha')
  expect(slim.get('.time-client-chip').text()).toBe('acme')
  expect(slim.get('.time-line-title').text()).toBe('Checkout triage')
  expect(slim.get('.time-mode').text()).toBe('manual')
  expect(slim.get('.time-elapsed').text()).toBe('0:01:05')
  await vi.advanceTimersByTimeAsync(5000)
  expect(slim.get('.time-elapsed').text()).toBe('0:01:10')
  // A poll's fresh count replaces the local tick.
  api.fetchTimeStatus.mockResolvedValue(status({ running: running({ mode: 'auto', elapsed_seconds: 3700 }) }))
  await vi.advanceTimersByTimeAsync(5000)
  await flushPromises()
  expect(slim.get('.time-elapsed').text()).toBe('1:01:40')
  expect(slim.get('.time-mode').text()).toBe('automatic')
  expect(api.fetchTimeStatus).toHaveBeenCalledTimes(2)

  const clock = wrapper.get('header .time-toggle')
  expect(clock.classes()).toContain('time-toggle-running')
  expect(clock.attributes('aria-label')).toBe('edit the running timer · tracking automatic')
  expect(shown(wrapper.get('#time-bar-editor'))).toBe(false)
  await clock.trigger('click')
  expect(shown(wrapper.get('#time-bar-editor'))).toBe(true)
  expect(clock.attributes('aria-expanded')).toBe('true')
  await clock.trigger('click')
  expect(shown(wrapper.get('#time-bar-editor'))).toBe(false)
  expect(shown(bar(wrapper)), 'a running timer keeps the bar open').toBe(true)
  vi.useRealTimers()
})

it('hides the bar again once the timer stops, unless the clock had opened it, and reopens it slim', async () => {
  api.fetchTimeStatus.mockResolvedValue(status({ running: running() }))
  const wrapper = mountApp()
  await flushPromises()
  expect(shown(bar(wrapper))).toBe(true)
  await wrapper.get('header .time-toggle').trigger('click')
  expect(shown(wrapper.get('#time-bar-editor'))).toBe(true)
  api.fetchTimeStatus.mockResolvedValue(status())
  await poll()
  expect(shown(bar(wrapper))).toBe(false)
  const clock = wrapper.get('header .time-toggle')
  expect(clock.classes()).not.toContain('time-toggle-running')
  await clock.trigger('click')
  expect(shown(bar(wrapper))).toBe(true)
  expect(wrapper.get('#time-tracking-bar .time-start-slim').text()).toBe('Start')
  expect(shown(wrapper.get('#time-bar-editor')), 'the editor open at the stop is not where it reopens').toBe(false)
})

it('keeps only the clock in the header on a phone; the time line and the chips are the bar\'s', async () => {
  api.fetchTimeStatus.mockResolvedValue(status({ running: running(), warnings: [{ kind: 'held', handler: 'tasks',
    handler_label: 'Task board', reason: 'no board identity for pat', count: 1,
    message: '1 Task board entry held: no board identity for pat' }] }))
  const wrapper = mountApp({ width: phoneWidth })
  await flushPromises()
  expect(wrapper.find('header .bar-actions .time-toggle').exists()).toBe(true)
  expect(wrapper.find('header .time-chip').exists()).toBe(false)
  expect(wrapper.find('header .time-elapsed').exists()).toBe(false)
  const slim = wrapper.get('#time-tracking-bar .time-bar-slim')
  expect(slim.get('.time-elapsed').text()).toBe('0:01:05')
  expect(slim.get('.time-chip-held').text()).toContain('no board identity for pat')
})

it('derives the running clock\'s fill from any tint so it stands out at 3:1, with a readable icon', () => {
  for (const tint of [...Object.keys(projectColors), '#ffffff', '#000000', '#ffeb3b', '#808080']) {
    const { ground, ink } = runningAccent(tint)
    expect(contrastRatio(ground, projectColor(tint)), tint).toBeGreaterThanOrEqual(3)
    expect(contrastRatio(ink, ground), tint).toBeGreaterThanOrEqual(4.5)
  }
})

it('paints the running clock in the header\'s accent, or the theme primary on the theme\'s own header', async () => {
  const tinted = mountPane(TimeIndicator, { status: status({ running: running({ mode: 'auto' }) }), tint: '#225ec9' })
  const clock = tinted.get('.time-toggle')
  const { ground } = runningAccent('#225ec9')
  const rgb = (hex) => `rgb(${[1, 3, 5].map(start => parseInt(hex.slice(start, start + 2), 16)).join(', ')})`
  expect(clock.element.style.backgroundColor).toBe(rgb(ground))
  await clock.trigger('click')
  expect(tinted.emitted('edit')).toHaveLength(1)
  expect(tinted.emitted('toggle')).toBeUndefined()

  const plain = mountPane(TimeIndicator, { status: status({ running: running() }) })
  expect(plain.get('.time-toggle').element.style.backgroundColor).toBe('')
  expect(plain.get('.time-toggle').classes()).toContain('bg-primary')

  const idle = mountPane(TimeIndicator, { status: status(), tint: '#225ec9' })
  expect(idle.get('.time-toggle').element.style.backgroundColor).toBe('')
  await idle.get('.time-toggle').trigger('click')
  expect(idle.emitted('toggle')).toHaveLength(1)
})

it('counts what waits on the clock while the bar is hidden', async () => {
  api.fetchTimeStatus.mockResolvedValue(status({ warnings: [{ kind: 'writer', message: 'Entries are waiting' }],
    prompts: [{ id: 3, workspace_id: 'alpha', missing: [], message: 'Alpha needs a Board task to track time' }] }))
  const wrapper = mountApp()
  await flushPromises()
  const clock = wrapper.get('header .time-toggle')
  expect(clock.attributes('aria-label')).toBe('show time tracking · 2 waiting')
  expect(wrapper.get('header .time-badge .v-badge__badge').text()).toBe('2')
  await clock.trigger('click')
  expect(clock.attributes('aria-label')).toBe('hide time tracking')
  expect(wrapper.findAll('#time-tracking-bar .time-chip')).toHaveLength(2)
})

it('marks the tracking project in the sidebar as manual or automatic', async () => {
  api.fetchTimeStatus.mockResolvedValue(status({ running: running() }))
  const wrapper = mountApp()
  await flushPromises()
  const marker = (name) => wrapper.find(`.project-nav[aria-label="${name}"] .project-tracking`)
  expect(marker('Alpha').attributes('aria-label')).toBe('tracking time here · manual')
  expect(marker('Alpha').classes()).toContain('project-tracking-manual')
  expect(marker('Beta').exists()).toBe(false)
  api.fetchTimeStatus.mockResolvedValue(status({
    running: running({ workspace_id: 'beta', project_key: 'beta', project_name: 'Beta', mode: 'auto' }),
  }))
  await poll()
  expect(marker('Alpha').exists()).toBe(false)
  expect(marker('Beta').attributes('aria-label')).toBe('tracking time here · automatic')
  expect(marker('Beta').classes()).toContain('project-tracking-auto')
  api.fetchTimeStatus.mockResolvedValue(status())
  await poll()
  expect(wrapper.find('.project-tracking').exists()).toBe(false)
})

it('generates one input per handler field with its label, hint and recent values', async () => {
  const wrapper = mountPane(TimeTrackingBar, { status: status(), defaultProject: 'alpha' })
  await flushPromises()
  const fields = wrapper.findAll('.time-field')
  expect(fields).toHaveLength(1)
  expect(fields[0].attributes('data-field')).toBe('task_id')
  expect(fields[0].get('label').text()).toBe('Board task *')
  expect(fields[0].text()).toContain('The id at the end of the task URL')
  expect(wrapper.getComponent('.time-field').props('items')).toEqual(['abc123x', 'def456y'])
  expect(wrapper.getComponent('.time-title').props('items')).toEqual(['Checkout triage'])
  expect(wrapper.getComponent('.time-client').props('items')).toEqual(['acme'])
  // A project under the built-in handler has no extra fields to fill.
  await wrapper.setProps({ defaultProject: 'beta', focus: { id: 'beta', nonce: 1 } })
  await flushPromises()
  expect(wrapper.findAll('.time-field')).toHaveLength(0)
})

it('keeps Start disabled while a required field is empty or fails its pattern', async () => {
  const wrapper = mountPane(TimeTrackingBar, { status: status(), defaultProject: 'alpha' })
  await flushPromises()
  const start = () => wrapper.get('.time-start')
  expect(start().attributes('disabled')).toBeDefined()
  await wrapper.get('.time-field input').setValue('AB-1')
  await flushPromises()
  expect(start().attributes('disabled')).toBeDefined()
  expect(wrapper.get('.time-field').text()).toContain('Does not match [a-z0-9]{6,}')
  await wrapper.get('.time-field input').setValue('abc123x')
  await flushPromises()
  expect(start().attributes('disabled')).toBeUndefined()
  expect(wrapper.get('.time-field').text()).not.toContain('Does not match')
  await wrapper.get('.time-field input').setValue('')
  await flushPromises()
  expect(start().attributes('disabled')).toBeDefined()
})

it('warns inline about a missing client label and a missing handler field, and in the slim bar too', async () => {
  const wrapper = mountPane(TimeTrackingBar, { status: status(), defaultProject: 'alpha', editing: true })
  await flushPromises()
  expect(wrapper.find('.time-no-client').exists()).toBe(false)
  expect(wrapper.get('.time-missing-field').text()).toBe('Alpha needs a Board task to track time')
  await wrapper.get('.time-field input').setValue('abc123x')
  await flushPromises()
  expect(wrapper.find('.time-missing-field').exists()).toBe(false)
  await wrapper.setProps({ focus: { id: 'beta', nonce: 1 } })
  await flushPromises()
  expect(wrapper.get('.time-no-client').text()).toContain('No client label')
  expect(wrapper.get('.time-start').attributes('disabled')).toBeUndefined()

  const slim = mountPane(TimeTrackingBar, { status: status({
    running: running({ client: null, fields: {} }),
    warnings: [{ kind: 'no_client', message: 'Alpha has no client label' },
      { kind: 'missing_field', field: 'task_id', label: 'Board task', message: 'Alpha needs a Board task to track time' }],
  }) })
  const chips = slim.findAll('.time-chip')
  expect(chips.map(chip => chip.text())).toEqual(['no client', 'needs Board task'])
  expect(chips[1].attributes('title')).toBe('Alpha needs a Board task to track time')
  await chips[0].trigger('click')
  expect(slim.emitted('edit')).toHaveLength(1)
})

it('shows a suppressed automatic start in the bar until the prompt clears', async () => {
  const prompt = { workspace_id: 'alpha', project_key: 'alpha', project_name: 'Alpha',
    missing: [{ key: 'task_id', label: 'Board task' }], at: '2026-10-29T09:00:00Z',
    message: 'Alpha needs a Board task to track time' }
  api.fetchTimeStatus.mockResolvedValue(status({ prompts: [prompt] }))
  window.history.replaceState({}, '', '/?workspace=beta')
  const wrapper = mountApp()
  await flushPromises()
  await wrapper.get('header .time-toggle').trigger('click')
  const chip = wrapper.get('#time-tracking-bar .time-chip-prompt')
  expect(chip.text()).toBe('Alpha needs a Board task to track time')
  await chip.trigger('click')
  await flushPromises()
  expect(shown(bar(wrapper))).toBe(true)
  expect(shown(wrapper.get('#time-bar-editor'))).toBe(true)
  expect(wrapper.getComponent(TimeTrackingBar).getComponent('.time-project-select').props('modelValue')).toBe('alpha')
  api.fetchTimeStatus.mockResolvedValue(status())
  await poll()
  expect(wrapper.find('#time-tracking-bar .time-chip-prompt').exists()).toBe(false)
  expect(bar(wrapper).text()).not.toContain('needs a Board task to track time.')
})

it('shows why entries are held in the bar and opens the entries filtered to them', async () => {
  api.fetchTimeStatus.mockResolvedValue(status({ warnings: [{ kind: 'held', handler: 'tasks',
    handler_label: 'Task board', reason: 'no board identity for pat', count: 2,
    message: '2 Task board entries held: no board identity for pat' }] }))
  const wrapper = mountApp()
  await flushPromises()
  await wrapper.get('header .time-toggle').trigger('click')
  const chip = wrapper.get('#time-tracking-bar .time-chip-held')
  expect(chip.text()).toContain('no board identity for pat')
  await chip.trigger('click')
  await flushPromises()
  expect(wrapper.getComponent(TimeTrackingBar).emitted('entries')).toEqual([[{ status: 'held', handler: 'tasks' }]])
  expect(wrapper.getComponent(TimeEntries).props('filters')).toEqual({ status: 'held', handler: 'tasks' })
})

it('opens the entries in a named dialog over the run being read, which a followed run link closes', async () => {
  const run = { host: 'forge.test', project: 'team/app', slug: 'fix-login', label: 'fix-login',
    workspace_id: 'alpha', state: 'running', live: true, attention: null, questions: [] }
  api.fetchState.mockResolvedValue({ runs: [run], attention: [], totals: {}, relations: [] })
  window.innerWidth = 1440
  window.history.replaceState({}, '', '/?workspace=alpha')
  const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true, RunDetail: true } } })
  await flushPromises()
  const ref = parseRunRef('lmer://run/forge.test/team/app/fix-login')
  expect(dispatchRunRef(ref)).toBe(true)
  await flushPromises()
  expect(wrapper.findComponent(RunDetail).exists()).toBe(true)

  const opener = button(wrapper, 'View entries')
  opener.element.focus()
  await opener.trigger('click')
  await flushPromises()
  expect(wrapper.getComponent(TimeEntries).props('filters')).toEqual({})
  expect(api.fetchTimeEntries).toHaveBeenCalledTimes(1)
  expect(wrapper.findComponent(RunDetail).exists()).toBe(true)
  expect(wrapper.get('header').text()).not.toContain('Time entries')
  const dialog = document.querySelector('.time-entries-dialog')
  expect(dialog.getAttribute('role')).toBe('dialog')
  expect(document.getElementById(dialog.getAttribute('aria-labelledby')).textContent).toBe('Time entries')

  // Closing gives focus back to what opened the dialog, from wherever in it focus was.
  const close = document.querySelector('[aria-label="close the time entries"]')
  close.focus()
  expect(document.activeElement).toBe(close)
  close.click()
  await flushPromises()
  expect(wrapper.findComponent(TimeEntries).exists()).toBe(false)
  expect(wrapper.findComponent(RunDetail).exists()).toBe(true)
  expect(document.activeElement).toBe(opener.element)

  // Each opening reads the entries afresh; following one of its run links closes it.
  await button(wrapper, 'View entries').trigger('click')
  await flushPromises()
  expect(api.fetchTimeEntries).toHaveBeenCalledTimes(2)
  document.querySelector('[aria-label="close the time entries"]').focus()
  dispatchRunRef(ref)
  await flushPromises()
  expect(wrapper.findComponent(TimeEntries).exists()).toBe(false)
  expect(wrapper.findComponent(RunDetail).exists()).toBe(true)
  expect(document.activeElement).not.toBe(opener.element)
  wrapper.unmount()
})

it('turns automatic tracking off and on from the bar, and says when it is off', async () => {
  const wrapper = mountPane(TimeTrackingBar, { status: status(), defaultProject: 'beta' })
  await flushPromises()
  const toggle = wrapper.getComponent('.time-automatic')
  expect(toggle.props('modelValue')).toBe(true)
  expect(wrapper.find('.time-automatic-off').exists()).toBe(false)
  toggle.vm.$emit('update:modelValue', false)
  await flushPromises()
  expect(api.setTimeAutomatic).toHaveBeenLastCalledWith(false)
  expect(wrapper.emitted('changed')).toHaveLength(1)

  await wrapper.setProps({ status: status({ automatic: { on: false, off_since: '2026-10-29T09:00:00Z' } }) })
  expect(wrapper.getComponent('.time-automatic').props('modelValue')).toBe(false)
  expect(wrapper.get('.time-automatic-off').text()).toContain('Automatic tracking is off')
  // Manual time still works while it is off.
  expect(wrapper.get('.time-start').attributes('disabled')).toBeUndefined()
  wrapper.getComponent('.time-automatic').vm.$emit('update:modelValue', true)
  await flushPromises()
  expect(api.setTimeAutomatic).toHaveBeenLastCalledWith(true)
})

it('reports its height for the layout to reserve', async () => {
  const observers = []
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback) { this.callback = callback; observers.push(this) }
    observe(element) { this.element = element }
    disconnect() { this.disconnected = true }
  })
  try {
    const wrapper = mountPane(TimeTrackingBar, { status: status(), defaultProject: 'beta' })
    await flushPromises()
    const [observer] = observers
    expect(observer.element).toBe(wrapper.get('#time-tracking-bar').element)
    Object.defineProperty(observer.element, 'offsetHeight', { configurable: true, value: 44 })
    observer.callback([])
    expect(wrapper.emitted('size')).toEqual([[44]])
    wrapper.unmount()
    expect(observer.disconnected).toBe(true)
  } finally { vi.unstubAllGlobals() }
})

it('opens the entries from the bar', async () => {
  const wrapper = mountPane(TimeTrackingBar, { status: status(), defaultProject: 'alpha' })
  await flushPromises()
  await button(wrapper, 'View entries').trigger('click')
  expect(wrapper.emitted('entries')).toEqual([[{}]])
})

it('sends a client label typed in the bar with the start', async () => {
  const wrapper = mountPane(TimeTrackingBar, { status: status(), defaultProject: 'alpha' })
  await flushPromises()
  await wrapper.get('.time-client input').setValue('walk-in')
  await wrapper.get('.time-field input').setValue('def456y')
  await flushPromises()
  await wrapper.get('.time-start').trigger('click')
  await flushPromises()
  expect(api.startTime).toHaveBeenCalledWith({ project: 'alpha', client: 'walk-in', title: undefined,
    fields: { task_id: 'def456y' } })
  expect(wrapper.emitted('changed')).toHaveLength(1)
})

it('keeps what is typed, and the error, across a poll that brings the same running timer', async () => {
  api.updateTime.mockRejectedValue(refusal(422, 'time_refused', 'task_id: does not match'))
  const wrapper = mountPane(TimeTrackingBar, { status: status({ running: running() }), defaultProject: 'alpha' })
  await flushPromises()
  const values = () => ['.time-client', '.time-title', '.time-field']
    .map(selector => wrapper.getComponent(selector).props('modelValue'))
  expect(values()).toEqual(['acme', 'Checkout triage', 'abc123x'])
  await wrapper.get('.time-client input').setValue('globex')
  await wrapper.get('.time-title input').setValue('Renamed')
  await wrapper.get('.time-field input').setValue('def456y')
  await flushPromises()
  await wrapper.get('.time-update').trigger('click')
  await flushPromises()
  expect(wrapper.get('.time-error').text()).toBe('task_id: does not match')
  // The poll replaces the status with an equal one.
  await wrapper.setProps({ status: status({ running: running() }) })
  await flushPromises()
  expect(values()).toEqual(['globex', 'Renamed', 'def456y'])
  expect(wrapper.get('.time-error').text()).toBe('task_id: does not match')

  // Another project, and back, seeds the form again; so does a new start.
  await wrapper.setProps({ focus: { id: 'beta', nonce: 1 } })
  await flushPromises()
  expect(wrapper.getComponent('.time-title').props('modelValue')).toBe(null)
  expect(wrapper.find('.time-error').exists()).toBe(false)
  await wrapper.setProps({ focus: { id: 'alpha', nonce: 2 } })
  await flushPromises()
  expect(values()).toEqual(['acme', 'Checkout triage', 'abc123x'])
  await wrapper.get('.time-title input').setValue('Renamed')
  await wrapper.setProps({ status: status({ running: running({ start: '2026-10-29T10:00:00Z', title: 'Next task' }) }) })
  await flushPromises()
  expect(values()).toEqual(['acme', 'Next task', 'abc123x'])
})

it('clears the title, client and an optional field on Update by sending them empty', async () => {
  const noted = { ...board, fields: [...board.fields, { key: 'note', label: 'Note', required: false }] }
  const current = status({ running: running({ fields: { task_id: 'abc123x', note: 'pairing' } }) })
  current.projects.alpha.handler = noted
  const wrapper = mountPane(TimeTrackingBar, { status: current, defaultProject: 'alpha' })
  await flushPromises()
  await wrapper.get('.time-title input').setValue('')
  await wrapper.get('.time-client input').setValue('')
  await wrapper.get('.time-field[data-field="note"] input').setValue('')
  await flushPromises()
  await wrapper.get('.time-update').trigger('click')
  await flushPromises()
  expect(api.updateTime).toHaveBeenCalledWith({ client: '', title: '', fields: { task_id: 'abc123x', note: '' } },
    { client: 'acme', title: 'Checkout triage', fields: { task_id: 'abc123x', note: 'pairing' } })
})

it('puts a start from the bar in the header and the sidebar at once', async () => {
  const wrapper = mountApp()
  await flushPromises()
  await wrapper.get('header .time-toggle').trigger('click')
  await wrapper.get('.time-field input').setValue('abc123x')
  await flushPromises()
  api.startTime.mockImplementation(async () => {
    api.fetchTimeStatus.mockResolvedValue(status({ running: running({ elapsed_seconds: 0 }) }))
    return { ok: true, outcome: 'started' }
  })
  await wrapper.get('.time-start').trigger('click')
  await flushPromises()
  expect(api.startTime).toHaveBeenCalledWith({ project: 'alpha', client: 'acme', title: undefined,
    fields: { task_id: 'abc123x' } })
  expect(wrapper.get('#time-tracking-bar .time-project').text()).toBe('Alpha')
  expect(wrapper.get('.project-nav[aria-label="Alpha"] .project-tracking').attributes('aria-label'))
    .toBe('tracking time here · manual')
  expect(wrapper.find('.time-start').exists()).toBe(false)
  expect(wrapper.find('.time-stop').exists()).toBe(true)
})

it('reads the status again after a start even while a poll that left before it is in flight', async () => {
  const wrapper = mountApp()
  await flushPromises()
  await wrapper.get('header .time-toggle').trigger('click')
  await wrapper.get('.time-field input').setValue('abc123x')
  await flushPromises()
  const stale = deferred()
  api.startTime.mockImplementation(async () => {
    api.fetchTimeStatus.mockReturnValueOnce(stale.promise)
    window.dispatchEvent(new Event('focus'))
    api.fetchTimeStatus.mockResolvedValue(status({ running: running() }))
    return { ok: true, outcome: 'started' }
  })
  await wrapper.get('.time-start').trigger('click')
  await flushPromises()
  stale.resolve(status())
  await flushPromises()
  expect(wrapper.get('#time-tracking-bar .time-project').text()).toBe('Alpha')
})

it('says why a start was refused and stops the running timer', async () => {
  api.startTime.mockRejectedValue(refusal(422, 'time_refused', 'task_id: required (Board task)'))
  const wrapper = mountPane(TimeTrackingBar, { status: status({ running: running({ workspace_id: 'beta',
    project_name: 'Beta', mode: 'auto', handler: 'timesheet', fields: {} }) }), defaultProject: 'alpha' })
  await flushPromises()
  // The bar follows the running timer, so it opens on Beta; Alpha is chosen by hand.
  wrapper.getComponent('.time-project-select').vm.$emit('update:modelValue', 'alpha')
  await flushPromises()
  await wrapper.get('.time-field input').setValue('abc123x')
  await wrapper.get('.time-start').trigger('click')
  await flushPromises()
  expect(wrapper.get('.time-error').text()).toBe('task_id: required (Board task)')
  await wrapper.get('.time-stop').trigger('click')
  await flushPromises()
  expect(api.stopTime).toHaveBeenCalledWith({ project: 'beta' })
})

it('merges a client label added from the bar while the settings form was open', async () => {
  const project = projects[0]
  const wrapper = mountPane(ProjectSettings, { project, time: status() })
  await flushPromises()
  expect(wrapper.getComponent('.time-handler').props('items').map(item => item.value)).toEqual(['timesheet', 'tasks'])
  expect(wrapper.get('.time-project-key input').attributes('placeholder')).toBe('alpha')
  wrapper.getComponent('.time-clients').vm.$emit('update:modelValue', ['acme', 'globex'])
  await flushPromises()
  // Meanwhile the bar's start added `walk-in` to the project.
  api.fetchProjects.mockResolvedValue({ projects: [{ ...project, settings: { time_clients: ['acme', 'walk-in'],
    time_project_key: 'alpha' } }, projects[1]] })
  await button(wrapper, 'Save project').trigger('click')
  await flushPromises()
  expect(api.fetchProjects).toHaveBeenCalled()
  // The edited list as typed here, then what the bar added meanwhile.
  expect(api.updateProject.mock.calls.at(-1)[1].settings).toMatchObject({
    time_clients: ['acme', 'globex', 'walk-in'], time_project_key: 'alpha' })

  // An edit to another setting leaves the labels as the project has them now.
  api.fetchProjects.mockResolvedValue({ projects: [{ ...project, settings: {
    time_clients: ['acme', 'globex', 'walk-in', 'initech'], time_project_key: 'alpha' } }] })
  wrapper.getComponent('.time-handler').vm.$emit('update:modelValue', 'tasks')
  await wrapper.get('.time-idle-grace input').setValue('20')
  await button(wrapper, 'Save project').trigger('click')
  await flushPromises()
  expect(api.updateProject.mock.calls.at(-1)[1].settings).toMatchObject({
    time_clients: ['acme', 'globex', 'walk-in', 'initech'], time_handler: 'tasks', time_idle_grace_minutes: 20 })

  // Removing a label here removes it, and only it.
  wrapper.getComponent('.time-clients').vm.$emit('update:modelValue', ['acme', 'walk-in', 'initech'])
  await button(wrapper, 'Save project').trigger('click')
  await flushPromises()
  expect(api.updateProject.mock.calls.at(-1)[1].settings.time_clients).toEqual(['acme', 'walk-in', 'initech'])
})

it('saves client labels reordered or re-cased here as the form has them, and the Unsaved mark clears', async () => {
  const project = { ...projects[0], settings: { time_clients: ['Acme', 'Beta'] } }
  api.fetchProjects.mockResolvedValue({ projects: [project, projects[1]] })
  const wrapper = mountPane(ProjectSettings, { project, time: status() })
  await flushPromises()
  const unsaved = () => wrapper.find('.project-time .settings-unsaved')

  // Moving a label first is how the chip box changes the default label.
  wrapper.getComponent('.time-clients').vm.$emit('update:modelValue', ['Beta', 'Acme'])
  await flushPromises()
  expect(unsaved().exists()).toBe(true)
  await button(wrapper, 'Save project').trigger('click')
  await flushPromises()
  expect(api.updateProject.mock.calls.at(-1)[1].settings.time_clients).toEqual(['Beta', 'Acme'])
  expect(wrapper.getComponent('.time-clients').props('modelValue')).toEqual(['Beta', 'Acme'])
  expect(unsaved().exists()).toBe(false)

  api.fetchProjects.mockResolvedValue({ projects: [{ ...project, settings: { time_clients: ['Beta', 'Acme'] } }, projects[1]] })
  wrapper.getComponent('.time-clients').vm.$emit('update:modelValue', ['Beta', 'acme'])
  await flushPromises()
  await button(wrapper, 'Save project').trigger('click')
  await flushPromises()
  expect(api.updateProject.mock.calls.at(-1)[1].settings.time_clients).toEqual(['Beta', 'acme'])
  expect(unsaved().exists()).toBe(false)
})

it('saves a cleared idle grace as null, an emptied list as [], and sends a 0 for the daemon to refuse', async () => {
  const project = { ...projects[0], settings: { ...projects[0].settings, time_idle_grace_minutes: 20,
    threads: ['https://chat.example.test/archives/C1/p1'] } }
  api.fetchProjects.mockResolvedValue({ projects: [project, projects[1]] })
  const wrapper = mountPane(ProjectSettings, { project, time: status() })
  await flushPromises()
  const unsaved = () => wrapper.find('.project-time .settings-unsaved')

  await wrapper.get('.time-idle-grace input').setValue('')
  expect(unsaved().exists()).toBe(true)
  // An emptied threads list is an explicit "no threads", not "inherit".
  wrapper.getComponent('.project-threads').vm.$emit('update:modelValue', [])
  await button(wrapper, 'Save project').trigger('click')
  await flushPromises()
  expect(api.updateProject.mock.calls.at(-1)[1].settings).toMatchObject({ time_idle_grace_minutes: null, threads: [] })
  expect(unsaved().exists()).toBe(false)

  api.updateProject.mockRejectedValueOnce(new Error('time_idle_grace_minutes must be at least 1'))
  await wrapper.get('.time-idle-grace input').setValue('0')
  expect(unsaved().exists()).toBe(true)
  await button(wrapper, 'Save project').trigger('click')
  await flushPromises()
  expect(api.updateProject.mock.calls.at(-1)[1].settings.time_idle_grace_minutes).toBe(0)
  expect(wrapper.text()).toContain('time_idle_grace_minutes must be at least 1')
  // Refused, so the 0 is still an edit rather than shown as stored.
  expect(unsaved().exists()).toBe(true)
})

it('offers the work repo project names for the key, matched first, a taken one disabled', async () => {
  const project = projects[0]
  const time = status()
  time.projects.alpha.key_suggestions = [
    { key: 'widgets', matched: true, taken: false },
    { key: 'docs-site', matched: false, taken: true },
    { key: 'napkin', matched: false, taken: false },
  ]
  const wrapper = mountPane(ProjectSettings, { project, time })
  await flushPromises()

  const field = wrapper.getComponent('.time-project-key')
  expect(field.props('returnObject')).toBe(false)
  expect(field.props('items')).toEqual([
    { value: 'widgets', title: 'widgets', props: { disabled: false, subtitle: 'matches this project\'s repository' } },
    { value: 'docs-site', title: 'docs-site', props: { disabled: true, subtitle: 'another project\'s key' } },
    { value: 'napkin', title: 'napkin', props: { disabled: false, subtitle: undefined } },
  ])
  expect(wrapper.get('.time-project-key input').attributes('placeholder')).toBe('alpha')
  expect(api.updateProject).not.toHaveBeenCalled()

  field.vm.$emit('update:modelValue', 'widgets')
  await flushPromises()
  await button(wrapper, 'Save project').trigger('click')
  await flushPromises()
  expect(api.updateProject.mock.calls.at(-1)[1].settings.time_project_key).toBe('widgets')
})

it('marks the time section unsaved while its fields hold an edit, as a settings card does', async () => {
  const project = { ...projects[0], settings: { ...projects[0].settings, time_idle_grace_minutes: 20 } }
  api.fetchProjects.mockResolvedValue({ projects: [project, projects[1]] })
  const wrapper = mountPane(ProjectSettings, { project, time: status() })
  await flushPromises()
  const unsaved = () => wrapper.find('.project-time .settings-unsaved')
  expect(unsaved().exists()).toBe(false)

  // The number Save would send is no edit.
  await wrapper.get('.time-idle-grace input').setValue('20.0')
  expect(unsaved().exists()).toBe(false)
  // An edit outside the section leaves it unmarked.
  await wrapper.get('.project-settings input').setValue('Alpha renamed')
  expect(unsaved().exists()).toBe(false)

  wrapper.getComponent('.time-project-key').vm.$emit('update:modelValue', 'widgets')
  await flushPromises()
  expect(unsaved().text()).toBe('Unsaved')
  await button(wrapper, 'Save project').trigger('click')
  await flushPromises()
  expect(api.updateProject.mock.calls.at(-1)[1].settings.time_project_key).toBe('widgets')
  expect(unsaved().exists()).toBe(false)
})

it('offers no key suggestions when the status carries none', async () => {
  const wrapper = mountPane(ProjectSettings, { project: projects[1], time: status() })
  await flushPromises()
  expect(wrapper.getComponent('.time-project-key').props('items')).toEqual([])
})

it('hides the time section of project settings while tracking is off', async () => {
  const wrapper = mountPane(ProjectSettings, { project: projects[0], time: null })
  await flushPromises()
  expect(wrapper.find('.project-time').exists()).toBe(false)
})

it('renders nothing and logs nothing while time tracking is off', async () => {
  const logged = vi.spyOn(console, 'error')
  api.fetchTimeStatus.mockRejectedValue(refusal(503, 'time_tracking_off', 'Time tracking is not running.'))
  const wrapper = mountApp()
  await flushPromises()
  await poll()
  expect(api.fetchTimeStatus).toHaveBeenCalledTimes(2)
  expect(wrapper.find('.time-indicator').exists()).toBe(false)
  expect(wrapper.find('.time-toggle').exists()).toBe(false)
  expect(wrapper.find('.time-hint').exists()).toBe(false)
  expect(wrapper.find('#time-tracking-bar').exists()).toBe(false)
  expect(wrapper.find('.project-tracking').exists()).toBe(false)
  expect(wrapper.find('main .v-alert').exists()).toBe(false)
  expect(logged).not.toHaveBeenCalled()
})

it('hints in the header, with no controls, when the operator credential acts for nobody', async () => {
  api.fetchTimeStatus.mockRejectedValue(refusal(409, 'time_no_default_person',
    'No default_person is set in the platform config, so the operator credential acts for nobody.'))
  const wrapper = mountApp()
  await flushPromises()
  const hint = wrapper.get('header .time-hint')
  expect(hint.text()).toBe('time: no default person')
  expect(hint.attributes('title')).toContain('No default_person is set')
  expect(wrapper.find('.time-toggle').exists()).toBe(false)
  expect(wrapper.find('#time-tracking-bar').exists()).toBe(false)
  expect(wrapper.find('main .v-alert').exists()).toBe(false)
})

it('drops the hint, the indicator and the settings time section once tracking is off, as with no people', async () => {
  api.fetchTimeStatus.mockRejectedValue(refusal(409, 'time_no_default_person',
    'No default_person is set in the platform config, so the operator credential acts for nobody.'))
  window.history.replaceState({}, '', '/?workspace=alpha')
  const wrapper = mountApp()
  await flushPromises()
  expect(wrapper.get('header .time-hint').text()).toBe('time: no default person')
  api.fetchTimeStatus.mockRejectedValue(refusal(503, 'time_tracking_off', 'Time tracking is not running.'))
  await poll()
  expect(wrapper.find('header .time-hint').exists()).toBe(false)
  expect(wrapper.find('header .time-indicator').exists()).toBe(false)
  await chooseProject(wrapper, 'Project settings for Alpha')
  expect(wrapper.getComponent(ProjectSettings).props('project').id).toBe('alpha')
  expect(wrapper.find('.project-time').exists()).toBe(false)
  // The section is there while tracking runs, so its absence above is the 503's.
  api.fetchTimeStatus.mockResolvedValue(status())
  await poll()
  expect(wrapper.find('.project-time').exists()).toBe(true)
})

it('asks the time routes on relative paths and names no person', async () => {
  const realApi = await vi.importActual('../src/api.js')
  const request = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
  vi.stubGlobal('fetch', request)
  try {
    await realApi.fetchTimeStatus()
    await realApi.fetchTimeStatus({ project: 'alpha one' })
    await realApi.startTime({ project: 'alpha', client: 'acme', fields: { task_id: 'abc123x' } })
    await realApi.stopTime({ project: 'alpha' })
    await realApi.updateTime({ title: 'Renamed' })
    await realApi.fetchTimeEntries({ from: '2026-10-01', status: 'held', project: null })
    await realApi.fetchTimeEntries()
    expect(request.mock.calls.map(([path, options]) => [path, options.method || 'GET', options.body])).toEqual([
      ['api/time/status', 'GET', undefined],
      ['api/time/status?project=alpha+one', 'GET', undefined],
      ['api/time/start', 'POST', JSON.stringify({ project: 'alpha', client: 'acme', fields: { task_id: 'abc123x' } })],
      ['api/time/stop', 'POST', JSON.stringify({ project: 'alpha' })],
      ['api/time/current', 'PATCH', JSON.stringify({ title: 'Renamed' })],
      ['api/time/entries?from=2026-10-01&status=held', 'GET', undefined],
      ['api/time/entries', 'GET', undefined],
    ])
  } finally { vi.unstubAllGlobals() }
})


// --- runs, issues, base and answering a prompt (#665) ----------------------

const ISSUE = 'https://git.example.test/acme/widgets/-/issues/12'
const MR = 'https://git.example.test/acme/widgets/-/merge_requests/7'

it('sends the values the form loaded as base, and a run renaming the title lights no Update', async () => {
  const current = status({ running: running({ mode: 'auto', title: 'Fix 12', title_source: 'run' }) })
  const wrapper = mountPane(TimeTrackingBar, { status: current, defaultProject: 'alpha' })
  await flushPromises()
  await wrapper.setProps({ status: status({ running: running({ mode: 'auto', title: 'Fix 12, renamed', title_source: 'run' }) }) })
  await flushPromises()
  expect(wrapper.find('.time-update').exists()).toBe(false)
  await wrapper.get('.time-title input').setValue('Mine')
  await flushPromises()
  await wrapper.get('.time-update').trigger('click')
  await flushPromises()
  expect(api.updateTime).toHaveBeenCalledWith({ client: 'acme', title: 'Mine', fields: { task_id: 'abc123x' } },
    { client: 'acme', title: 'Fix 12', fields: { task_id: 'abc123x' } })
})

it('takes what an in-place Update sent as the next base, and Update goes off', async () => {
  const wrapper = mountPane(TimeTrackingBar, { status: status({ running: running() }), defaultProject: 'alpha' })
  await flushPromises()
  await wrapper.get('.time-title input').setValue('Second')
  await flushPromises()
  await wrapper.get('.time-update').trigger('click')
  await flushPromises()
  expect(wrapper.find('.time-update').exists()).toBe(false)
  await wrapper.get('.time-title input').setValue('Third')
  await flushPromises()
  await wrapper.get('.time-update').trigger('click')
  await flushPromises()
  expect(api.updateTime).toHaveBeenLastCalledWith({ client: 'acme', title: 'Third', fields: { task_id: 'abc123x' } },
    { client: 'acme', title: 'Second', fields: { task_id: 'abc123x' } })
})

it('shows the running entry\'s issues and where its title came from', async () => {
  const current = status({ running: running({ mode: 'auto', title_source: 'run',
    identity: { run: 'git.example.test/acme/widgets/fix-12' }, issues: [ISSUE, MR] }) })
  const wrapper = mountPane(TimeTrackingBar, { status: current, defaultProject: 'alpha' })
  await flushPromises()
  expect(wrapper.findAll('.time-issue').map(link => [link.text(), link.attributes('href')])).toEqual(
    [['#12', ISSUE], ['!7', MR]])
  expect(wrapper.get('.time-title-source').text()).toContain('from the run')
})

it('answers a per-issue prompt from its chip: Start sends the prompt id and the field', async () => {
  const prompt = { id: 5, workspace_id: 'alpha', project_key: 'alpha', project_name: 'Alpha',
    work_key: ISSUE, label: '#12', issues: [ISSUE], missing: [{ key: 'task_id', label: 'Board task' }],
    at: '2026-10-29T09:00:00Z', message: 'Alpha #12 needs a Board task to track time' }
  api.fetchTimeStatus.mockResolvedValue(status({ prompts: [prompt] }))
  const wrapper = mountApp()
  await flushPromises()
  await wrapper.get('#time-tracking-bar .time-chip-prompt').trigger('click')
  await flushPromises()
  expect(bar(wrapper).get('.time-prompt').text()).toContain('Alpha #12 needs a Board task to track time')
  await bar(wrapper).get('.time-field[data-field="task_id"] input').setValue('abc123x')
  await flushPromises()
  await button(bar(wrapper), 'Start').trigger('click')
  await flushPromises()
  const [sent] = api.startTime.mock.calls.at(-1)
  expect(sent).toMatchObject({ prompt: 5, fields: { task_id: 'abc123x' } })
  expect(sent.project).toBeUndefined()
})

const ISSUE_13 = 'https://git.example.test/acme/widgets/-/issues/13'
const prompt13 = { id: 6, workspace_id: 'alpha', project_key: 'alpha', project_name: 'Alpha',
  work_key: ISSUE_13, label: '#13', issues: [ISSUE_13],
  missing: [{ key: 'task_id', label: 'Board task', per_issue: true }],
  at: '2026-10-29T09:00:00Z', message: 'Alpha #13 needs a Board task to track time' }

it('answers a prompt with what was typed for it, not the values of the timer running in its project', async () => {
  api.fetchTimeStatus.mockResolvedValue(status({ prompts: [prompt13], running: running({
    mode: 'auto', title: 'Fix 12', title_source: 'run', identity: { run: 'git.example.test/acme/widgets/fix-12' },
    issues: [ISSUE], work_key: ISSUE }) }))
  const wrapper = mountApp()
  await flushPromises()
  await wrapper.get('#time-tracking-bar .time-chip-prompt').trigger('click')
  await flushPromises()
  expect(bar(wrapper).get('.time-field[data-field="task_id"] input').element.value).toBe('')
  expect(bar(wrapper).get('.time-title input').element.value).toBe('')
  expect(button(bar(wrapper), 'Start').attributes('disabled')).toBeDefined()
  await bar(wrapper).get('.time-field[data-field="task_id"] input').setValue('zzz999q')
  await flushPromises()
  await button(bar(wrapper), 'Start').trigger('click')
  await flushPromises()
  expect(api.startTime.mock.calls.at(-1)[0]).toEqual({ prompt: 6, fields: { task_id: 'zzz999q' } })
})

it('answers a prompt without retyping a field the daemon fills from a recent value', async () => {
  const handler = { ...board, fields: [{ ...board.fields[0], per_issue: true },
    { key: 'activity', label: 'Activity', required: true }] }
  const current = status({ prompts: [prompt13] })
  current.projects.alpha = option(projects[0], handler, ['acme'],
    { titles: [], clients: ['acme'], fields: { task_id: ['abc123x'], activity: ['dev'] } })
  api.fetchTimeStatus.mockResolvedValue(current)
  const wrapper = mountApp()
  await flushPromises()
  await wrapper.get('#time-tracking-bar .time-chip-prompt').trigger('click')
  await flushPromises()
  expect(bar(wrapper).get('.time-field[data-field="activity"] input').element.value).toBe('dev')
  expect(bar(wrapper).get('.time-field[data-field="task_id"] input').element.value).toBe('')
  await bar(wrapper).get('.time-field[data-field="task_id"] input').setValue('zzz999q')
  await flushPromises()
  await button(bar(wrapper), 'Start').trigger('click')
  await flushPromises()
  expect(api.startTime.mock.calls.at(-1)[0]).toEqual({ prompt: 6, fields: { task_id: 'zzz999q' } })
})

it('offers Start for a prompt while a manual timer runs in its project', async () => {
  api.fetchTimeStatus.mockResolvedValue(status({ prompts: [prompt13], running: running() }))
  const wrapper = mountApp()
  await flushPromises()
  expect(button(bar(wrapper), 'Start')).toBeUndefined()
  await wrapper.get('#time-tracking-bar .time-chip-prompt').trigger('click')
  await flushPromises()
  await bar(wrapper).get('.time-field[data-field="task_id"] input').setValue('zzz999q')
  await flushPromises()
  await button(bar(wrapper), 'Start').trigger('click')
  await flushPromises()
  expect(api.startTime.mock.calls.at(-1)[0]).toEqual({ prompt: 6, fields: { task_id: 'zzz999q' } })
})

it('flags an unassigned running timer in the header', async () => {
  api.fetchTimeStatus.mockResolvedValue(status({
    running: running({ mode: 'auto', fields: {}, unassigned: ['task_id'] }),
    warnings: [{ kind: 'missing_field', field: 'task_id', label: 'Board task',
      message: 'Alpha needs a Board task to track time' }],
  }))
  const wrapper = mountApp()
  await flushPromises()
  expect(wrapper.get('#time-tracking-bar .time-chip-missing_field').text()).toBe('needs Board task')
})
