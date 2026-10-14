import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import App from '../src/App.vue'
import ForgetCompleted from '../src/components/ForgetCompleted.vue'
import { fetchProjects, fetchRunRelations, fetchState, forgetRun } from '../src/api.js'
import { chooseProject, mountPane, deferred } from './mount.js'

vi.mock('../src/api.js', () => ({
  fetchState: vi.fn(),
  fetchProjects: vi.fn().mockResolvedValue({ projects: [{ id: 'general', name: 'General', color: 'Blue', assistant: { lifecycle: 'stopped' } }] }),
  // The shell reads relations for the map when the payload carries none.
  fetchRunRelations: vi.fn().mockResolvedValue({ run: {}, relations: [] }),
  forgetRun: vi.fn(),
  prune: vi.fn(),
  rescan: vi.fn(),
}))
const run = (slug, extra = {}) => ({
  host: 'gitlab.example.com', project: 'acme/widget', slug, label: slug, workspace_id: 'general',
  state: 'complete', live: false, ...extra,
})
const dialogButton = (text) => [...document.querySelectorAll('.v-dialog button')]
  .find((button) => button.textContent.trim() === text)

beforeEach(() => {
  vi.clearAllMocks()
  fetchProjects.mockResolvedValue({ projects: [{ id: 'general', name: 'General', color: 'Blue', assistant: { lifecycle: 'stopped' } }] })
  window.history.replaceState({}, '', '/?workspace=general')
  forgetRun.mockResolvedValue({ forgotten: true })
  fetchRunRelations.mockResolvedValue({ run: {}, relations: [] })
})

async function open(runs) {
  const wrapper = mountPane(ForgetCompleted, { runs })
  await wrapper.get('button').trigger('click')
  await flushPromises()
  return wrapper
}

it('confirms before sending, and cancel sends nothing', async () => {
  const wrapper = await open([run('one')])
  expect(document.body.textContent).toContain('Forget 1 completed run?')
  expect(forgetRun).not.toHaveBeenCalled()
  dialogButton('cancel').click()
  await flushPromises()
  expect(forgetRun).not.toHaveBeenCalled()
  expect(wrapper.emitted('forgotten')).toBeUndefined()
})

it('forgets complete stopped runs sequentially and reports each success', async () => {
  const pending = deferred()
  const first = run('one')
  const second = run('two')
  forgetRun.mockReturnValueOnce(pending.promise)
  const wrapper = await open([
    first, second, run('live', { live: true }), run('failed', { state: 'failed' }),
    run('working', { state: 'in-progress' }),
  ])
  dialogButton('Forget 2 runs').click()
  dialogButton('Forget 2 runs').click()
  await flushPromises()
  expect(forgetRun).toHaveBeenCalledTimes(1)
  pending.resolve({ forgotten: true })
  await flushPromises()
  expect(forgetRun.mock.calls.map(([item]) => item.slug)).toEqual(['one', 'two'])
  expect(wrapper.emitted('forgotten')).toEqual([[first], [second]])
  expect(wrapper.emitted('changed')).toHaveLength(1)
})

it('keeps failures visible and continues with the remaining runs', async () => {
  forgetRun.mockRejectedValueOnce(new Error('Request refused'))
  const wrapper = await open([run('one'), run('two')])
  dialogButton('Forget 2 runs').click()
  await flushPromises()
  expect(wrapper.text()).toContain('one: Request refused')
  expect(wrapper.emitted('forgotten').map(([item]) => item.slug)).toEqual(['two'])
})

it('rechecks eligibility and does not add runs that arrived after confirmation opened', async () => {
  const wrapper = await open([run('one'), run('two')])
  await wrapper.setProps({ runs: [run('one', { live: true }), run('two'), run('three')] })
  dialogButton('Forget 2 runs').click()
  await flushPromises()
  expect(forgetRun.mock.calls.map(([item]) => item.slug)).toEqual(['two'])
  expect(wrapper.text()).toContain('one: no longer a completed, stopped run')
})

it('stops sending further requests after the listing is unmounted', async () => {
  const pending = deferred()
  forgetRun.mockReturnValueOnce(pending.promise)
  const wrapper = await open([run('one'), run('two')])
  dialogButton('Forget 2 runs').click()
  await flushPromises()
  wrapper.unmount()
  pending.resolve({ forgotten: true })
  await flushPromises()
  expect(forgetRun).toHaveBeenCalledTimes(1)
})

it('disables the action when no completed stopped runs exist', () => {
  const wrapper = mountPane(ForgetCompleted, { runs: [run('one', { live: true })] })
  expect(wrapper.get('button').attributes('disabled')).toBeDefined()
})

it('selects completed runs outside the listing search and removes only successful rows', async () => {
  window.localStorage.clear()
  window.location.hash = ''
  const first = run('first')
  const second = run('second')
  const payload = { runs: [first, second], attention: [], totals: { runs: 2, live: 0, attention: 0 } }
  fetchState.mockResolvedValue(payload)
  forgetRun.mockRejectedValueOnce(new Error('Request refused'))
  const wrapper = mountPane(App, {}, { global: { stubs: { RunCard: true, RunNav: true } } })
  await flushPromises()
  await chooseProject(wrapper, 'General')
  const search = wrapper.get('input[placeholder="name, taskdef, project, state, ticket…"]')
  await search.setValue('no matching runs')
  const choose = wrapper.findAll('button').find((button) => button.text().trim() === 'Forget all completed')
  await choose.trigger('click')
  await flushPromises()
  expect(document.body.textContent).toContain('Forget 2 completed runs?')
  dialogButton('Forget 2 runs').click()
  await flushPromises()
  expect(forgetRun.mock.calls.map(([item]) => item.slug)).toEqual(['first', 'second'])
  await search.setValue('')
  const cards = wrapper.findAllComponents({ name: 'RunCard' })
  expect(cards.map((card) => card.props('run').slug)).toEqual(['first'])
  expect(wrapper.text()).toContain('first: Request refused')
})


it('forgets both project scopes from All projects while preserving active and held runs', async () => {
  const beta = { id: 'beta', name: 'Example Beta', color: 'Teal', assistant: { lifecycle: 'stopped' } }
  fetchProjects.mockResolvedValue({ projects: [{ id: 'general', name: 'General' }, beta] })
  const rows = [run('general-done'), run('beta-done', { workspace_id: 'beta' }),
    run('active', { live: true }), run('held', { state: 'held' })]
  fetchState.mockResolvedValue({ runs: rows, attention: [], totals: {}, relations: [] })
  const wrapper = mountPane(App, {}, { global: { stubs: { RunCard: true } } })
  await flushPromises()
  await chooseProject(wrapper, 'All projects')
  await flushPromises()
  await wrapper.getComponent(ForgetCompleted).get('button').trigger('click')
  await flushPromises()
  expect(document.body.textContent).toContain('Forget 2 completed runs?')
  expect(forgetRun).not.toHaveBeenCalled()
  dialogButton('Forget 2 runs').click()
  await flushPromises()
  expect(forgetRun.mock.calls.map(([row]) => [row.workspace_id, row.slug])).toEqual([
    ['general', 'general-done'], ['beta', 'beta-done'],
  ])
  expect(wrapper.get('main').text()).not.toContain('general-done')
  expect(wrapper.get('main').text()).not.toContain('beta-done')
  expect(wrapper.get('main').text()).toContain('active')
  expect(wrapper.get('main').text()).toContain('held')
})

it('disposes a completed-run confirmation when the project scope changes', async () => {
  fetchProjects.mockResolvedValue({ projects: [{ id: 'general', name: 'General' }, { id: 'beta', name: 'Example Beta' }] })
  fetchState.mockResolvedValue({ runs: [run('one'), run('two', { workspace_id: 'beta' })], attention: [], totals: {}, relations: [] })
  const wrapper = mountPane(App, {}, { global: { stubs: { RunCard: true } } })
  await flushPromises()
  await wrapper.getComponent(ForgetCompleted).get('button').trigger('click')
  await flushPromises()
  const oldConfirm = dialogButton('Forget 1 run')
  await chooseProject(wrapper, 'All projects')
  await flushPromises()
  oldConfirm.click()
  await flushPromises()
  expect(forgetRun).not.toHaveBeenCalled()
  await wrapper.getComponent(ForgetCompleted).get('button').trigger('click')
  await flushPromises()
  expect(document.body.textContent).toContain('Forget 2 completed runs?')
})
