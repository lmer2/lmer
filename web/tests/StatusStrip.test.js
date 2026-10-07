import { beforeEach, expect, it, vi } from 'vitest'
import { statusAge } from '../src/format.js'
import { mountPane } from './mount.js'

const reading = { updated: '2026-09-15T12:00:00Z', age_seconds: 180 }
const status = {
  harness: 'codex', updated: reading.updated, age_seconds: 0,
  context: { ...reading, percent: 44, source: 'rollout/token_count', thread_id: 'native-one' },
  codex: {
    thread_id: 'native-one', version: '0.154.0',
    tokens: { ...reading, source: 'rollout/token_count', scope: 'thread',
      last: { total_tokens: 120393 }, total: { total_tokens: 1530372 }, context_window: 258400 },
    account: { ...reading, age_seconds: 3, source: 'account/rateLimits/read', scope: 'account', main_bucket: 'codex', limits: [
      { key: 'codex:primary', bucket: 'codex', label: 'codex', period_label: '7d', percent: 10 },
      { key: 'other:primary', bucket: 'other', label: 'Special model', period_label: '5h', percent: 2 },
    ] },
    runtime: { ...reading, age_seconds: 2, source: 'thread/read', model: 'model-runtime', effort: 'high' },
  },
}
let StatusStrip
beforeEach(async () => {
  window.localStorage.clear()
  vi.resetModules()
  StatusStrip = (await import('../src/components/StatusStrip.vue')).default
})

it.each([false, true])('defaults to consumed context, main windows and effort (compact=%s)', (compact) => {
  const wrapper = mountPane(StatusStrip, { status, compact })
  const text = wrapper.text()
  expect(text).toContain('44%')
  expect(text).toContain('7d')
  expect(text).toContain('10%')
  expect(text).toContain('high')
  expect(text).not.toMatch(/tokens|capacity|model-runtime|5h|used/)
  expect(text.match(/as of/g)).toHaveLength(1)
  expect(text).toContain('as of 3m ago')
  const titles = wrapper.findAll('[title]').map((item) => item.attributes('title')).join(' ')
  expect(titles).toContain('account bucket codex')
  expect(titles).toContain('model-runtime')
  expect(titles).toContain(reading.updated)
  expect(titles).toContain('rollout/token_count')
  if (!compact) {
    expect(wrapper.findAllComponents({ name: 'VProgressLinear' }).map((meter) => meter.props('modelValue'))).toEqual([44, 10])
  }
})

it('persists details across remounts and updates other mounted strips', async () => {
  const full = mountPane(StatusStrip, { status })
  const row = mountPane(StatusStrip, { status, compact: true })
  await full.get('button').trigger('click')
  for (const wrapper of [full, row]) {
    expect(wrapper.text()).toContain('120,393 tokens')
    expect(wrapper.text()).toContain('1,530,372 tokens')
    expect(wrapper.text()).toContain('258,400 tokens')
    expect(wrapper.text()).toContain('model-runtime')
    expect(wrapper.text()).toContain('5h')
    expect(wrapper.get('button').attributes('aria-pressed')).toBe('true')
    expect(wrapper.findAll('[title]').some((item) => item.attributes('title').includes('Special model'))).toBe(true)
  }
  full.unmount()
  row.unmount()
  expect(window.localStorage.getItem('lmer:usage-details')).toBe('on')
  vi.resetModules()
  StatusStrip = (await import('../src/components/StatusStrip.vue')).default
  const reopened = mountPane(StatusStrip, { status })
  expect(reopened.text()).toContain('120,393 tokens')
  await reopened.get('button').trigger('click')
  reopened.unmount()
  expect(mountPane(StatusStrip, { status }).text()).not.toContain('tokens')
})

it('still toggles when browser storage refuses access', async () => {
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked') })
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })
  const wrapper = mountPane(StatusStrip, { status })
  expect(wrapper.text()).not.toContain('tokens')
  await wrapper.get('button').trigger('click')
  expect(wrapper.text()).toContain('120,393 tokens')
  const second = mountPane(StatusStrip, { status, compact: true })
  for (const strip of [wrapper, second]) {
    expect(strip.text()).toContain('120,393 tokens')
    expect(strip.get('button').attributes('aria-pressed')).toBe('true')
  }
  await second.get('button').trigger('click')
  for (const strip of [wrapper, second]) {
    expect(strip.text()).not.toContain('tokens')
    expect(strip.get('button').attributes('aria-pressed')).toBe('false')
  }
})

it.each([false, true])('keeps unknown-main windows behind details (compact=%s)', async (compact) => {
  const copy = structuredClone(status)
  copy.codex.account.main_bucket = null
  copy.codex.account.limits = [
    { key: 'spark:primary', bucket: 'spark', label: 'Spark', period_label: '5h', percent: 23 },
    { key: 'reserve:primary', bucket: 'reserve', label: 'Reserve', period_label: '7d', percent: 67 },
  ]
  const wrapper = mountPane(StatusStrip, { status: copy, compact })
  expect(wrapper.text()).toContain('44%')
  expect(wrapper.text()).toContain('high')
  expect(wrapper.text()).not.toMatch(/5h|7d|23%|67%/)
  await wrapper.get('button').trigger('click')
  expect(wrapper.text()).toMatch(/5h\s*23%/)
  expect(wrapper.text()).toMatch(/7d\s*67%/)
  const titles = wrapper.findAll('[title]').map((item) => item.attributes('title')).join(' ')
  expect(titles).toContain('account bucket spark: Spark')
  expect(titles).toContain('account bucket reserve: Reserve')
})

it.each([[101, 100], [-1, 0]])('bounds reported account usage %s to %s', (reported, displayed) => {
  const copy = structuredClone(status)
  copy.codex.account.limits[0].percent = reported
  const wrapper = mountPane(StatusStrip, { status: copy })
  expect(wrapper.findAllComponents({ name: 'VProgressLinear' }).map((meter) => meter.props('modelValue')))
    .toEqual([44, displayed])
  expect(wrapper.findAll('.status-segment')[1].text()).toContain(`${displayed}%`)
  expect(mountPane(StatusStrip, { status: copy, compact: true }).text()).toContain(`7d ${displayed}%`)
})

it('offers details when they are the only reported values', async () => {
  const onlyTokens = { harness: 'codex', codex: { thread_id: 'one', tokens: status.codex.tokens } }
  const wrapper = mountPane(StatusStrip, { status: onlyTokens })
  expect(wrapper.text()).toBe('details')
  await wrapper.get('button').trigger('click')
  expect(wrapper.text()).toContain('120,393 tokens')
})

it('does not add controls or alter Claude when Codex details are enabled', async () => {
  const codex = mountPane(StatusStrip, { status })
  await codex.get('button').trigger('click')
  const claude = { harness: 'claude', ...reading, context: { percent: 44 },
    limits: [{ key: 'week', label: '7d', percent: 10 }], effort: { level: 'high' } }
  const wrapper = mountPane(StatusStrip, { status: claude, compact: true })
  expect(wrapper.text()).toBe('ctx 44% · 7d 10% · eff high (as of 3m ago)')
  expect(wrapper.find('button').exists()).toBe(false)
})

it('keeps unknown and future source dates visible in the single caption', () => {
  const copy = structuredClone(status)
  delete copy.context.age_seconds
  expect(statusAge(copy)).toBe('age unknown')
  copy.context.age_seconds = -1
  expect(statusAge(copy)).toBe('clock ahead')
})

it('lets the assistant pane hide its strip, remembers it, and leaves other strips shown', async () => {
  const buttonNamed = (wrapper, text) => wrapper.findAll('button').find((item) => item.text() === text)
  const pane = mountPane(StatusStrip, { status, hideable: true, meterColor: 'primary' })
  const run = mountPane(StatusStrip, { status })
  expect(pane.text()).toContain('44%')
  expect(pane.findAllComponents({ name: 'VProgressLinear' }).map((meter) => meter.props('color'))).toEqual(['primary', 'primary'])
  expect(run.findAllComponents({ name: 'VProgressLinear' }).map((meter) => meter.props('color'))).toEqual(['tone-idle', 'tone-idle'])
  expect(buttonNamed(run, 'hide')).toBeUndefined()
  await buttonNamed(pane, 'hide').trigger('click')
  expect(pane.text()).not.toContain('44%')
  expect(buttonNamed(pane, 'show status')).toBeDefined()
  expect(run.text()).toContain('44%')
  expect(window.localStorage.getItem('lmer:assistant-status-strip')).toBe('on')
  pane.unmount()
  vi.resetModules()
  StatusStrip = (await import('../src/components/StatusStrip.vue')).default
  const reopened = mountPane(StatusStrip, { status, hideable: true })
  expect(reopened.text()).not.toContain('44%')
  expect(mountPane(StatusStrip, { status }).text()).toContain('44%')
  await buttonNamed(reopened, 'show status').trigger('click')
  expect(reopened.text()).toContain('44%')
  expect(window.localStorage.getItem('lmer:assistant-status-strip')).toBe('off')
})

it('shows the assistant strip by default and when storage refuses access', () => {
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked') })
  expect(mountPane(StatusStrip, { status, hideable: true }).text()).toContain('44%')
  vi.restoreAllMocks()
})
