import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import RunDetail from '../src/components/RunDetail.vue'
import { fetchAssistantConfig, fetchSessionAsk, forgetRun, moveRun } from '../src/api.js'
import { mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({
  fetchAssistantConfig: vi.fn(),
  fetchSessionAsk: vi.fn(),
  forgetRun: vi.fn(),
  resumeRun: vi.fn(),
  moveRun: vi.fn(),
}))

const projects = [
  { id: 'alpha', name: 'Alpha', color: 'Blue' },
  { id: 'beta', name: 'Beta', color: 'Teal' },
]
const unassigned = {
  host: 'forge.test', project: 'same/repository', slug: 'orphan', label: 'orphan',
  workspace_id: null, workspace_name: undefined, state: 'complete', live: false,
  questions: [], attention: null,
}
const stubs = { global: { stubs: {
  Chat: true, Terminal: true, RelatedRuns: true, RunMeta: true, ThreadLinks: true,
  AskChannel: true, AskHistory: true,
} } }

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  fetchAssistantConfig.mockResolvedValue({})
  fetchSessionAsk.mockResolvedValue({ entries: [] })
  moveRun.mockResolvedValue({})
  forgetRun.mockResolvedValue({ forgotten: true })
})

it('labels an unassigned run and a recorded project with no name differently', async () => {
  const wrapper = mountPane(RunDetail, { run: unassigned, projects, identifiedWorkspaceId: null }, stubs)
  await flushPromises()
  expect(wrapper.get('.run-project-identity').text()).toBe('Unassigned')
  await wrapper.setProps({ run: { ...unassigned, workspace_id: 'ghost' } })
  await flushPromises()
  expect(wrapper.get('.run-project-identity').text()).toBe('Unavailable project')
})

it('assigns an unassigned run to the selected destination', async () => {
  const wrapper = mountPane(RunDetail, { run: unassigned, projects, identifiedWorkspaceId: null }, stubs)
  await flushPromises()
  wrapper.vm.$.setupState.moveDestination = 'beta'
  await flushPromises()
  await wrapper.findAll('button').find(button => button.text() === 'Move run').trigger('click')
  await flushPromises()
  expect(moveRun).toHaveBeenCalledWith(expect.objectContaining({ slug: 'orphan', workspace_id: null }), 'beta')
  expect(wrapper.emitted('changed')).toBeTruthy()
  expect(wrapper.emitted('close')).toBeTruthy()
})

it('renders a refused assignment beside the move control, not behind a tab', async () => {
  moveRun.mockRejectedValueOnce(new Error('This run is not assigned to that project.'))
  const wrapper = mountPane(RunDetail, { run: unassigned, projects, identifiedWorkspaceId: null }, stubs)
  await flushPromises()
  wrapper.vm.$.setupState.moveDestination = 'beta'
  await flushPromises()
  await wrapper.findAll('button').find(button => button.text() === 'Move run').trigger('click')
  await flushPromises()
  expect(wrapper.findAll('.v-alert').map(alert => alert.text())).toContain('This run is not assigned to that project.')
  expect(wrapper.findAll('[role="tab"]').find(tab => tab.text().trim() === 'exit').attributes('aria-selected')).not.toBe('true')
})

it('forgets an unassigned run without naming a project', async () => {
  const wrapper = mountPane(RunDetail, { run: unassigned, projects, identifiedWorkspaceId: null }, stubs)
  await flushPromises()
  await wrapper.findAll('[role="tab"]').find(tab => tab.text().trim() === 'exit').trigger('click')
  await flushPromises()
  await wrapper.findAll('button').find(button => button.text() === 'forget this run').trigger('click')
  await flushPromises()
  expect(forgetRun).toHaveBeenCalledWith(expect.objectContaining({ slug: 'orphan', workspace_id: null }))
})
