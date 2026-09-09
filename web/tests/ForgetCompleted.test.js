import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import App from '../src/App.vue'
import ForgetCompleted from '../src/components/ForgetCompleted.vue'
import { fetchState, forgetRun } from '../src/api.js'
import { mountPane, deferred } from './mount.js'

vi.mock('../src/api.js', () => ({ fetchState: vi.fn(), forgetRun: vi.fn(), prune: vi.fn(), rescan: vi.fn() }))
const run = (slug, extra = {}) => ({
  host: 'gitlab.example.com', project: 'acme/widget', slug, label: slug,
  state: 'complete', live: false, ...extra,
})
const dialogButton = (text) => [...document.querySelectorAll('.v-dialog button')]
  .find((button) => button.textContent.trim() === text)

beforeEach(() => { vi.clearAllMocks(); forgetRun.mockResolvedValue({ forgotten: true }) })

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
