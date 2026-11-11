import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import App from '../src/App.vue'
import TimeEntries from '../src/components/TimeEntries.vue'
import * as api from '../src/api.js'
import { onRunRef } from '../src/runref.js'
import { deferred, mountPane } from './mount.js'

vi.mock('../src/api.js', async importOriginal => ({
  ...await importOriginal(), fetchState: vi.fn(), fetchProjects: vi.fn(), fetchAssistantConfig: vi.fn(),
  fetchAssistantInstructions: vi.fn(), fetchChannelRoutes: vi.fn(), fetchTimeStatus: vi.fn(),
  fetchTimeEntries: vi.fn(),
}))

const board = {
  name: 'tasks', label: 'Task board', sync: true,
  fields: [{ key: 'task_id', label: 'Board task', required: true, link: 'https://tasks.example.test/t/{value}' },
    { key: 'note', label: 'Note', required: false }],
}
const timesheet = { name: 'timesheet', label: 'Timesheet', sync: false, fields: [] }

function status(overrides = {}) {
  return {
    person: 'pat', enabled: true, problems: [], running: null, warnings: [], prompts: [], pause: null,
    outbox: { pending: 0, last_error: null }, writer_refusal: null,
    acting: { person: 'pat', name: 'Pat', via: 'default_person', record: null, request_id: null },
    projects: {
      alpha: { workspace_id: 'alpha', name: 'Alpha', archived: false, project_key: 'alpha', handler: board,
        handler_warning: null, clients: ['acme'], idle_grace_minutes: null, recent: {} },
      beta: { workspace_id: 'beta', name: 'Beta', archived: false, project_key: 'beta', handler: timesheet,
        handler_warning: null, clients: [], idle_grace_minutes: null, recent: {} },
    },
    handlers: [timesheet, board], handler_problems: [], unmapped: [],
    ...overrides,
  }
}

let serial = 0
function row(overrides = {}) {
  serial += 1
  const id = overrides.id === undefined ? `e${serial}` : overrides.id
  return {
    id, stage: 'written', sync: null, person: 'pat', project: 'alpha', workspace_id: 'alpha',
    project_name: 'Alpha', handler: 'timesheet', handler_label: 'Timesheet', fields: {}, field_links: {},
    client: 'acme', title: 'Checkout triage', start: '2026-10-29T09:00:00Z', end: '2026-10-29T10:00:00Z',
    duration: 3600, trigger: { start: 'manual', stop: 'manual' }, source: 'lmer', mode: null,
    path: `timesheet.d/pat/2026/10/20261029T090000Z--alpha--${id}.yaml`, origin: 'mirror',
    raw: `id: ${id}\nperson: pat\n`, problems: [], outbox: null, kept: null, sessions: [],
    ...overrides,
  }
}

function synced(overrides = {}) {
  return { status: 'synced', detail: null, link: 'https://tasks.example.test/entries/77', ref: '77', reason: null,
    at: '2026-10-29T11:00:00Z', hash: 'sha256:abc', ...overrides }
}

function entries(rows, overrides = {}) {
  return { person: 'pat', rows, count: rows.length, filters: {}, problems: [], acting: { person: 'pat' }, ...overrides }
}

const refusal = (status, code, message) => Object.assign(new Error(message), { status, code })

function mountEntries(props = {}) {
  return mountPane(TimeEntries, { filters: {}, status: status(), ...props })
}

const phoneWidth = window.innerWidth
afterEach(() => { window.innerWidth = phoneWidth })

const entry = (wrapper, id) => wrapper.get(`[data-entry="${id}"]`)
const totals = (element) => element.findAll('.time-total')
  .map(item => [item.get('.time-total-name').text(), item.get('.time-total-seconds').text()])

beforeEach(() => {
  vi.clearAllMocks()
  window.history.replaceState({}, '', '/')
  window.localStorage.clear()
  api.fetchState.mockResolvedValue({ runs: [], attention: [], totals: {}, relations: [] })
  api.fetchProjects.mockResolvedValue({ projects: [
    { id: 'alpha', name: 'Alpha', color: 'Blue', repositories: [], settings: {}, assistant: { lifecycle: 'running' } },
  ] })
  api.fetchAssistantConfig.mockResolvedValue({})
  api.fetchAssistantInstructions.mockResolvedValue({ instructions: null })
  api.fetchChannelRoutes.mockResolvedValue({ routes: [] })
  api.fetchTimeStatus.mockResolvedValue(status())
  api.fetchTimeEntries.mockResolvedValue(entries([]))
})

it('groups the entries by UTC day, newest first, with totals per day and per project', async () => {
  api.fetchTimeEntries.mockResolvedValue(entries([
    row({ id: 'late', start: '2026-10-29T14:00:00Z', end: '2026-10-29T14:30:00Z', duration: 1800,
      project: 'beta', workspace_id: 'beta', project_name: 'Beta' }),
    row({ id: 'early', start: '2026-10-29T09:00:00Z', end: '2026-10-29T10:00:00Z', duration: 3600 }),
    row({ id: 'before', start: '2026-10-28T23:30:00Z', end: '2026-10-29T00:15:00Z', duration: 2700 }),
  ]))
  const wrapper = mountEntries()
  await flushPromises()

  const days = wrapper.findAll('.time-day')
  expect(days.map(day => day.attributes('data-day'))).toEqual(['2026-10-29', '2026-10-28'])
  expect(days[0].get('h2').text()).toBe('Thu 2026-10-29')
  expect(days[0].get('.time-day-total').text()).toBe('1:30:00')
  expect(totals(days[0].get('.time-day-projects'))).toEqual([['Beta', '0:30:00'], ['Alpha', '1:00:00']])
  expect(days[0].findAll('.time-entry').map(item => item.attributes('data-entry'))).toEqual(['late', 'early'])
  expect(days[1].get('.time-day-total').text()).toBe('0:45:00')
  expect(totals(wrapper.get('.time-entries-overall'))).toEqual([['Beta', '0:30:00'], ['Alpha', '1:45:00']])
  expect(wrapper.get('.time-entries-overall h2').text()).toContain('3 entries')

  const crossing = entry(wrapper, 'before')
  expect(crossing.get('.time-entry-start').text()).toBe('23:30')
  expect(crossing.get('.time-entry-end').text()).toBe('2026-10-29 00:15')
  expect(crossing.get('.time-entry-duration').text()).toBe('0:45:00')
  expect(crossing.get('.time-entry-project').text()).toBe('Alpha')
  expect(crossing.get('.time-entry-title').text()).toBe('Checkout triage')
})

it('shows each pipeline stage and the ledger status: synced with its link, held with its reason, pending', async () => {
  api.fetchTimeEntries.mockResolvedValue(entries([
    row({ id: null, stage: 'running', end: null, duration: 65, trigger: { start: 'activity', stop: null },
      path: null, origin: 'tracker', mode: 'automatic' }),
    row({ id: 'queued', stage: 'in outbox', origin: 'outbox',
      outbox: { seq: 1, state: 'pending', attempts: 2, last_error: 'push refused: 403' } }),
    row({ id: 'sliver', stage: 'kept locally', path: null, origin: 'tracker', duration: 20,
      kept: 'shorter than the 60 s minimum' }),
    row({ id: 'done', handler: 'tasks', handler_label: 'Task board', sync: synced() }),
    row({ id: 'stuck', handler: 'tasks', handler_label: 'Task board',
      sync: synced({ status: 'held', link: null, reason: 'no board identity for pat' }) }),
    row({ id: 'waiting', handler: 'tasks', handler_label: 'Task board',
      sync: synced({ status: 'pending', link: null, ref: null, at: null, detail: 'no ledger line yet' }) }),
  ]))
  const wrapper = mountEntries()
  await flushPromises()

  const running = entry(wrapper, 'running')
  expect(running.get('.time-entry-stage').text()).toBe('running')
  expect(running.get('.time-entry-end').text()).toBe('now')
  expect(running.find('.time-entry-sync').exists()).toBe(false)
  expect(entry(wrapper, 'queued').get('.time-entry-stage').text()).toBe('in outbox')
  expect(entry(wrapper, 'queued').get('.time-entry-outbox-error').text()).toBe('push refused: 403')
  expect(entry(wrapper, 'sliver').get('.time-entry-stage').text()).toBe('kept locally')
  expect(entry(wrapper, 'sliver').get('.time-entry-kept').text()).toBe('shorter than the 60 s minimum')

  const done = entry(wrapper, 'done')
  expect(done.get('.time-entry-stage').text()).toBe('written')
  const link = done.get('a.time-entry-sync')
  expect(link.text()).toBe('synced')
  expect(link.attributes('href')).toBe('https://tasks.example.test/entries/77')
  expect(link.attributes('target')).toBe('_blank')
  expect(link.attributes('rel')).toBe('noopener')

  const stuck = entry(wrapper, 'stuck')
  expect(stuck.get('.time-entry-sync').attributes('data-sync')).toBe('held')
  expect(stuck.get('.time-entry-sync').text()).toBe('held')
  expect(stuck.get('.time-entry-held-reason').text()).toBe('no board identity for pat')
  expect(stuck.find('a.time-entry-sync').exists()).toBe(false)

  const waiting = entry(wrapper, 'waiting')
  expect(waiting.get('.time-entry-sync').text()).toBe('pending')
  expect(waiting.get('.time-entry-sync-detail').text()).toBe('no ledger line yet')
})

it('links handler fields through the link the daemon built from the handler template', async () => {
  api.fetchTimeEntries.mockResolvedValue(entries([
    row({ id: 'linked', handler: 'tasks', handler_label: 'Task board', fields: { task_id: 'abc123x', note: 'pairing' },
      field_links: { task_id: 'https://tasks.example.test/t/abc123x' } }),
  ]))
  const wrapper = mountEntries()
  await flushPromises()

  const linked = entry(wrapper, 'linked')
  expect(linked.get('.time-entry-handler').text()).toBe('Task board')
  const task = linked.get('[data-field="task_id"]')
  expect(task.text()).toBe('Board task: abc123x')
  const anchor = task.get('a')
  expect(anchor.attributes('href')).toBe('https://tasks.example.test/t/abc123x')
  expect(anchor.attributes('target')).toBe('_blank')
  expect(anchor.attributes('rel')).toBe('noopener')
  const note = linked.get('[data-field="note"]')
  expect(note.text()).toBe('Note: pairing')
  expect(note.find('a').exists()).toBe(false)
})

it('links only http(s) ledger and field links: a script URL renders as plain text', async () => {
  api.fetchTimeEntries.mockResolvedValue(entries([
    row({ id: 'scripted', handler: 'tasks', handler_label: 'Task board', fields: { task_id: 'abc123x' },
      field_links: { task_id: 'javascript:alert(document.cookie)' },
      sync: synced({ link: 'javascript:alert(document.cookie)' }) }),
    row({ id: 'safe', handler: 'tasks', handler_label: 'Task board', fields: { task_id: 'abc123x' },
      field_links: { task_id: 'https://tasks.example.test/t/abc123x' }, sync: synced() }),
  ]))
  const wrapper = mountEntries()
  await flushPromises()

  const scripted = entry(wrapper, 'scripted')
  expect(scripted.find('[href]').exists()).toBe(false)
  expect(scripted.get('.time-entry-sync').text()).toBe('synced')
  expect(scripted.get('[data-field="task_id"]').text()).toBe('Board task: abc123x')
  const safe = entry(wrapper, 'safe')
  expect(safe.get('a.time-entry-sync').attributes('href')).toBe('https://tasks.example.test/entries/77')
  expect(safe.get('[data-field="task_id"] a').attributes('href')).toBe('https://tasks.example.test/t/abc123x')
})

it('marks a missing client, draws each trigger as its own labelled icon and shows broken files', async () => {
  api.fetchTimeEntries.mockResolvedValue(entries([
    row({ id: 'switched', client: '', trigger: { start: 'activity', stop: 'switch' } }),
    row({ id: 'idled', trigger: { start: 'manual', stop: 'idle' } }),
    row({ id: 'broken', handler: null, fields: {}, client: '', title: '', end: null, duration: null, trigger: null,
      problems: ['yaml: mapping values are not allowed here'], raw: 'id: broken\n  : :\n' }),
    row({ id: 'typed', trigger: { start: 'hand', stop: 'hand' }, source: 'hand' }),
  ], { problems: ['handlers/tasks.yaml: fields[0].key is required'] }))
  const wrapper = mountEntries()
  await flushPromises()

  expect(entry(wrapper, 'switched').get('.time-entry-no-client').text()).toBe('no client')
  expect(entry(wrapper, 'idled').find('.time-entry-no-client').exists()).toBe(false)
  expect(entry(wrapper, 'idled').get('.time-entry-client').text()).toBe('acme')

  const labels = (id) => entry(wrapper, id).findAll('.time-trigger')
    .map(item => [item.attributes('data-trigger'), item.attributes('aria-label')])
  expect(labels('switched')).toEqual([
    ['start:activity', 'started by activity'], ['stop:switch', 'stopped by a switch to another project']])
  expect(labels('idled')).toEqual([
    ['start:manual', 'started by hand in lmer'], ['stop:idle', 'stopped after the idle grace period']])
  expect(labels('typed')[0][1]).toBe('started in a hand-written entry')
  // The test Vuetify has no SVG icon set, so an icon's path lands in its class.
  const icons = ['switched', 'idled', 'typed'].flatMap(id => entry(wrapper, id).findAll('.time-trigger .v-icon'))
    .map(icon => icon.classes()[0])
  expect(new Set(icons).size).toBe(5)

  const broken = entry(wrapper, 'broken')
  expect(broken.get('.time-entry-problems').text()).toContain('yaml: mapping values are not allowed here')
  expect(broken.get('.time-entry-title').text()).toBe('untitled')
  expect(wrapper.get('.time-entries-source-problem').text()).toContain('handlers/tasks.yaml')
})

it('expands a row to its file path and raw text, and offers no control that edits an entry', async () => {
  api.fetchTimeEntries.mockResolvedValue(entries([
    row({ id: 'one', raw: 'id: one\nperson: pat\nclient: acme\n', sync: null }),
  ]))
  const wrapper = mountEntries()
  await flushPromises()

  const one = entry(wrapper, 'one')
  expect(one.find('.time-entry-detail').exists()).toBe(false)
  const toggle = one.get('.time-entry-toggle')
  expect(toggle.attributes('aria-expanded')).toBe('false')
  await toggle.trigger('click')
  expect(toggle.attributes('aria-expanded')).toBe('true')
  expect(one.get('.time-entry-path').text()).toBe('timesheet.d/pat/2026/10/20261029T090000Z--alpha--one.yaml')
  expect(one.get('.time-entry-raw').element.tagName).toBe('PRE')
  expect(one.get('.time-entry-raw').text()).toBe('id: one\nperson: pat\nclient: acme')

  // Every control that takes input is a filter; nothing on a row, expanded or not.
  const editable = wrapper.findAll('input, textarea, select, [contenteditable]')
  expect(editable.length).toBeGreaterThan(0)
  for (const item of editable) expect(item.element.closest('[data-test="filters"]')).not.toBeNull()
  expect(wrapper.findAll('[data-test^="filter-"]').map(item => item.attributes('data-test'))).toEqual([
    'filter-from', 'filter-to', 'filter-project', 'filter-client', 'filter-handler', 'filter-status'])
  const labels = wrapper.findAll('button').map(item => (item.text() || item.attributes('aria-label') || '').toLowerCase())
  for (const label of labels) expect(label).not.toMatch(/edit|save|delete|remove|start|stop/)
})

it('lists the runs an entry covered as in-app run links, counted on the collapsed row', async () => {
  const uid = '981ec036134f4a8580927ddca71658a9'
  api.fetchTimeEntries.mockResolvedValue(entries([
    row({ id: 'linked', sessions: [{ run: `forge.test/team/app/${uid}`, uid },
      { run: 'forge.test/team/app/develop-issue-7' }, { uid: 'f'.repeat(32) }] }),
    row({ id: 'plain' }),
  ]))
  const opened = []
  const stop = onRunRef(ref => opened.push(ref))
  try {
    const wrapper = mountEntries()
    await flushPromises()

    expect(entry(wrapper, 'plain').find('.time-entry-run-count').exists()).toBe(false)
    const linked = entry(wrapper, 'linked')
    const count = linked.get('.time-entry-run-count')
    expect(count.text()).toBe('3')
    expect(count.attributes('aria-label')).toBe('3 linked runs')
    expect(linked.find('.time-entry-runs').exists()).toBe(false)

    await linked.get('.time-entry-toggle').trigger('click')
    const runs = linked.findAll('.time-entry-run')
    expect(runs.map(item => item.text())).toEqual([
      `forge.test/team/app/${uid}`, 'forge.test/team/app/develop-issue-7', 'f'.repeat(32)])
    expect(runs[0].get('a').attributes('href')).toBe(`lmer://run/forge.test/team/app/${uid}`)
    expect(runs[2].find('a').exists()).toBe(false)

    const click = new MouseEvent('click', { bubbles: true, cancelable: true })
    runs[0].get('a').element.dispatchEvent(click)
    expect(click.defaultPrevented).toBe(true)
    await runs[1].get('a').trigger('auxclick', { button: 1 })
    await runs[1].get('a').trigger('auxclick', { button: 2 })
    expect(opened).toEqual([
      { host: 'forge.test', project: 'team/app', slug: uid, uid, key: `forge.test/team/app/${uid}` },
      { host: 'forge.test', project: 'team/app', slug: 'develop-issue-7', key: 'forge.test/team/app/develop-issue-7' },
    ])
  } finally {
    stop()
  }
})

it('opens filtered to the held entries from the time bar\'s warning', async () => {
  api.fetchTimeStatus.mockResolvedValue(status({ warnings: [{ kind: 'held', handler: 'tasks',
    handler_label: 'Task board', reason: 'no board identity for pat', count: 1,
    message: '1 Task board entry held: no board identity for pat' }] }))
  api.fetchTimeEntries.mockResolvedValue(entries([
    row({ id: 'stuck', handler: 'tasks', handler_label: 'Task board',
      sync: synced({ status: 'held', link: null, reason: 'no board identity for pat' }) }),
  ]))
  window.innerWidth = 1440
  const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(api.fetchTimeEntries).not.toHaveBeenCalled()

  await wrapper.get('header .time-toggle').trigger('click')
  await wrapper.get('#time-tracking-bar .time-chip-held').trigger('click')
  await flushPromises()
  expect(api.fetchTimeEntries).toHaveBeenCalledTimes(1)
  expect(api.fetchTimeEntries).toHaveBeenCalledWith(expect.objectContaining({ status: 'held', handler: 'tasks' }))
  const page = wrapper.getComponent(TimeEntries)
  expect(page.get('[data-test="filter-status"]').text()).toContain('held')
  expect(page.get('[data-test="filter-handler"]').text()).toContain('Task board')
  expect(entry(page, 'stuck').get('.time-entry-held-reason').text()).toBe('no board identity for pat')
})

it('reads again with the new filter when one changes, and ignores an older read that lands late', async () => {
  const first = deferred()
  const second = deferred()
  api.fetchTimeEntries.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
  const wrapper = mountEntries({ filters: { status: 'pending' } })
  await flushPromises()
  expect(api.fetchTimeEntries).toHaveBeenLastCalledWith(
    { from: undefined, to: undefined, project: undefined, client: undefined, handler: undefined, status: 'pending' })

  await wrapper.get('[data-test="filter-from"] input').setValue('2026-10-01')
  await flushPromises()
  expect(api.fetchTimeEntries).toHaveBeenCalledTimes(2)
  expect(api.fetchTimeEntries).toHaveBeenLastCalledWith(expect.objectContaining({ from: '2026-10-01', status: 'pending' }))

  second.resolve(entries([row({ id: 'newer' })]))
  await flushPromises()
  first.resolve(entries([row({ id: 'older' })]))
  await flushPromises()
  expect(wrapper.find('[data-entry="newer"]').exists()).toBe(true)
  expect(wrapper.find('[data-entry="older"]').exists()).toBe(false)
})

it('takes the new filters when it is opened again from elsewhere', async () => {
  const wrapper = mountEntries({ filters: {} })
  await flushPromises()
  await wrapper.setProps({ filters: { status: 'held', handler: 'tasks' } })
  await flushPromises()
  expect(api.fetchTimeEntries).toHaveBeenCalledTimes(2)
  expect(api.fetchTimeEntries).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'held', handler: 'tasks' }))
})

it('says so when nothing matches, and shows a refusal as a short message', async () => {
  const wrapper = mountEntries()
  await flushPromises()
  expect(wrapper.get('.time-entries-empty').text()).toBe('No entries match these filters.')

  api.fetchTimeEntries.mockRejectedValueOnce(refusal(503, 'time_tracking_off', 'Time tracking is not running.'))
  await wrapper.get('.time-entries-reload').trigger('click')
  await flushPromises()
  expect(wrapper.get('.time-entries-problem').text()).toBe('Time tracking is off, so there are no entries to show.')
  expect(wrapper.find('.time-entries-empty').exists()).toBe(false)

  api.fetchTimeEntries.mockRejectedValueOnce(refusal(409, 'time_no_default_person',
    'The operator credential acts for nobody: set default_person.'))
  await wrapper.get('.time-entries-reload').trigger('click')
  await flushPromises()
  expect(wrapper.get('.time-entries-problem').text()).toBe('The operator credential acts for nobody: set default_person.')

  api.fetchTimeEntries.mockResolvedValueOnce(entries([row({ id: 'back' })]))
  await wrapper.get('.time-entries-reload').trigger('click')
  await flushPromises()
  expect(wrapper.find('.time-entries-problem').exists()).toBe(false)
  expect(wrapper.find('[data-entry="back"]').exists()).toBe(true)
})


// --- issues, the entry's run, edit and unassigned (#665) -------------------

it('lists an entry\'s issues as links, labelled the way the daemon labels them', async () => {
  const issue = 'https://git.example.test/acme/widgets/-/issues/12'
  const mr = 'https://git.example.test/acme/widgets/-/merge_requests/7'
  api.fetchTimeEntries.mockResolvedValue(entries([row({ id: 'issued', issues: [issue, mr] })]))
  const wrapper = mountEntries()
  await flushPromises()
  expect(entry(wrapper, 'issued').findAll('.time-entry-issue').map(link => [link.text(), link.attributes('href')]))
    .toEqual([['#12', issue], ['!7', mr]])
})

it('names a running entry\'s run and opens it in the fleet view', async () => {
  const run = 'git.example.test/acme/widgets/fix-12'
  api.fetchTimeEntries.mockResolvedValue(entries([row({ id: null, stage: 'running', end: null, mode: 'auto',
    identity: { run } })]))
  const opened = []
  const stop = onRunRef(ref => opened.push(ref))
  try {
    const wrapper = mountEntries()
    await flushPromises()
    await wrapper.get('.time-entry-toggle').trigger('click')
    const link = wrapper.get('.time-entry-identity a')
    expect(link.text()).toBe(run)
    await link.trigger('click')
    expect(opened).toHaveLength(1)
  } finally { stop() }
})

it('labels an edit trigger', async () => {
  api.fetchTimeEntries.mockResolvedValue(entries([row({ id: 'edited', trigger: { start: 'edit', stop: 'edit' } })]))
  const wrapper = mountEntries()
  await flushPromises()
  expect(entry(wrapper, 'edited').get('[data-trigger="stop:edit"]').attributes('aria-label'))
    .toBe('stopped by an edit after time had accrued')
})

it('flags an unassigned per-issue field as missing, not as a problem', async () => {
  api.fetchTimeEntries.mockResolvedValue(entries([row({ id: 'unassigned', handler: 'tasks',
    handler_label: 'Task board', unassigned: ['task_id'] })]))
  const wrapper = mountEntries()
  await flushPromises()
  const flagged = entry(wrapper, 'unassigned')
  expect(flagged.get('.time-entry-unassigned').text()).toBe('missing Board task')
  expect(flagged.find('.time-entry-problems').exists()).toBe(false)
})
