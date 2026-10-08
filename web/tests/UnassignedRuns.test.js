import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import App from '../src/App.vue'
import RunDetail from '../src/components/RunDetail.vue'
import Chat from '../src/components/Chat.vue'
import AskHistory from '../src/components/AskHistory.vue'
import RunCard from '../src/components/RunCard.vue'
import * as api from '../src/api.js'
import { chooseProject, mountPane } from './mount.js'

vi.mock('../src/api.js', async importOriginal => ({
  ...await importOriginal(), fetchState: vi.fn(), fetchProjects: vi.fn(),
  fetchAssistantConfig: vi.fn(), fetchRunRelations: vi.fn(),
}))

const fixture = JSON.parse(execFileSync(process.env.PROJECT_TEST_PYTHON || 'python3',
  [resolve('tests/fixtures/unassigned_inventory.py')], { encoding: 'utf8' }))
const legacy = ['s-legacy-assistant', 's-legacy-worker']

afterEach(() => vi.unstubAllGlobals())

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  window.history.replaceState({}, '', '/')
  api.fetchState.mockResolvedValue(structuredClone(fixture.state))
  api.fetchProjects.mockResolvedValue(structuredClone(fixture.projects))
  api.fetchAssistantConfig.mockResolvedValue({})
  api.fetchRunRelations.mockResolvedValue({ relations: [] })
  vi.stubGlobal('fetch', vi.fn(async (path, options = {}) => {
    const url = new URL(path, window.location.origin)
    expect(options.method || 'GET').toBe('GET')
    const match = url.pathname.match(/^\/api\/sessions\/([^/]+)\/(messages|ask)$/)
    if (match) {
      expect(url.searchParams.has('workspace_id')).toBe(false)
      const response = fixture.reads[match[1]][match[2]]
      return { ok: response.status === 200, status: response.status, json: async () => structuredClone(response.body) }
    }
    expect(url.pathname).toBe('/api/runs/files')
    return { ok: false, status: 404, json: async () => ({ detail: 'No work files for this isolated session.' }) }
  }))
})

it('renders actual migrated API failures in both operator-wide lists without inventing a project', async () => {
  const wrapper = mountPane(App)
  await flushPromises()
  expect(fixture.state.runs).toHaveLength(4)
  expect(fixture.state.attention).toHaveLength(3)
  for (const location of ['main', '.v-navigation-drawer--left']) {
    const group = wrapper.get(`${location} [aria-label="Unassigned"]`)
    expect(group.findAll('.run-row')).toHaveLength(2)
    expect(group.find('.project-assistant').exists()).toBe(false)
    for (const id of legacy) expect(group.text()).toContain(id)
    expect(wrapper.findAll(`${location} .run-row`)).toHaveLength(4)
  }
  expect(fixture.state.runs.filter(row => legacy.includes(row.session.id))
    .every(row => row.workspace_id === null && !row.workspace_name)).toBe(true)
  expect(wrapper.get('.project-nav[aria-label="General"]').findAll('.run-row')).toHaveLength(0)
})

it.each(legacy)('opens and reads %s retained history after a project selection', async id => {
  window.history.replaceState({}, '', `/?workspace=${fixture.alpha}`)
  const wrapper = mountPane(App)
  await flushPromises()
  expect(wrapper.find('main [aria-label="Unassigned"]').exists()).toBe(false)
  expect(wrapper.get('main').text()).not.toContain(id)
  const group = wrapper.get('.v-navigation-drawer--left [aria-label="Unassigned"]')
  await group.findAll('.run-row').find(row => row.text().includes(id)).trigger('click')
  await flushPromises()
  expect(window.location.search).toBe('')
  const detail = wrapper.getComponent(RunDetail)
  expect(detail.props('run').session.id).toBe(id)
  expect(detail.props('run').workspace_id).toBeNull()
  expect(detail.props('identifiedWorkspaceId')).toBeNull()
  expect(detail.props('run').state).toBe('crashed')
  expect(detail.text()).toContain(id)
  expect(wrapper.get('.project-switcher').text()).toContain('All projects')
  expect(group.findAll('[aria-current="true"]')).toHaveLength(1)
  await detail.findAll('[role="tab"]').find(tab => tab.text().trim() === 'lmer').trigger('click')
  await vi.waitFor(() => {
    const chat = detail.getComponent(Chat)
    expect(chat.text()).toContain(`Retained history for ${id}`)
    expect(chat.text()).toContain(`Retained question for ${id}`)
    expect(chat.text()).toContain(`Retained answer for ${id}`)
    expect(chat.text()).not.toContain('Choose a project for this request.')
    expect(chat.find('textarea').exists()).toBe(false)
  })
  await detail.get('.operator-chat-tab').trigger('click')
  await vi.waitFor(() => {
    const history = detail.getComponent(AskHistory)
    expect(history.text()).toContain(`Retained question for ${id}`)
    expect(history.text()).toContain(`Retained answer for ${id}`)
    expect(history.find('textarea').exists()).toBe(false)
  })
  expect(fetch.mock.calls.some(([path]) => path.includes(`/sessions/${id}/messages`))).toBe(true)
  expect(fetch.mock.calls.some(([path]) => path.includes(`/sessions/${id}/ask`))).toBe(true)
  await chooseProject(wrapper, 'Alpha')
  await flushPromises()
  expect(window.location.search).toBe(`?workspace=${fixture.alpha}`)
  expect(wrapper.findComponent(RunDetail).exists()).toBe(false)
  expect(wrapper.findAllComponents(RunCard).map(card => card.props('run').session.id).sort())
    .toEqual(['s-alpha-current', 's-alpha-failure'])
})

it('keeps scoped failures and the live successor in their project after opening recovery', async () => {
  const wrapper = mountPane(App)
  await flushPromises()
  await wrapper.get('main [aria-label="Unassigned"] .run-row').trigger('click')
  await flushPromises()
  expect(wrapper.findComponent(RunDetail).exists()).toBe(true)
  await chooseProject(wrapper, 'Alpha')
  await flushPromises()
  expect(window.location.search).toBe(`?workspace=${fixture.alpha}`)
  expect(wrapper.findComponent(RunDetail).exists()).toBe(false)
  expect(wrapper.find('main [aria-label="Unassigned"]').exists()).toBe(false)
  const scoped = fixture.state.runs.filter(row => row.workspace_id === fixture.alpha)
  expect(scoped.map(row => row.session.id).sort()).toEqual(['s-alpha-current', 's-alpha-failure'])
  for (const row of scoped) expect(wrapper.get('main').text()).toContain(row.label)
  expect(wrapper.findAll('.v-navigation-drawer--left [aria-label="Unassigned"] .run-row')).toHaveLength(2)
})
