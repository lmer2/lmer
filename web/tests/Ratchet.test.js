import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import App from '../src/App.vue'
import RatchetFace from '../src/components/RatchetFace.vue'
import { fetchProjects, fetchRunRelations, fetchState } from '../src/api.js'
import { mountPane } from './mount.js'
import { DEFAULT_ASSISTANT_NAME, applyAssistantName, assistantName, faceFile, faceUrl, moodOf, moodReason } from '../src/ratchet.js'

vi.mock('../src/api.js', () => ({
  fetchProjects: vi.fn(),
  fetchState: vi.fn(), forgetRun: vi.fn(), prune: vi.fn(), rescan: vi.fn(),
  fetchRunRelations: vi.fn().mockResolvedValue({ run: {}, relations: [] }),
}))

describe('assistantName', () => {
  it('defaults to Ratchet', () => {
    expect(DEFAULT_ASSISTANT_NAME).toBe('Ratchet')
    expect(assistantName.value).toBe('Ratchet')
  })

  it.each([
    [{ assistant_name: 'Sprocket' }, 'Sprocket'],
    [{ assistant_name: '  Cog  ' }, 'Cog'],
    [{ assistant_name: '' }, 'Ratchet'],
    [{ assistant_name: '   ' }, 'Ratchet'],
    [{}, 'Ratchet'],
    [undefined, 'Ratchet'],
    [null, 'Ratchet'],
  ])('applyAssistantName(%j) → %s', (config, expected) => {
    applyAssistantName(config)
    expect(assistantName.value).toBe(expected)
    applyAssistantName({})
  })
})

const run = (over = {}) => ({ slug: 's', state: 'running', live: true, orchestrator: false, attention: null, ...over })
const att = (reason, over = {}) => run({ attention: { reason }, ...over })
const base = (over = {}) => ({
  loaded: true, runs: [], attention: [], liveWorkers: 0, cap: 6,
  openQuestions: 0, replying: false, justFinished: 0, ...over,
})

describe('moodOf', () => {
  it.each([
    ['not loaded', base({ loaded: false }), 'loading'],
    ['empty fleet', base(), 'idle'],
    ['a live question', base({ attention: [att('live_question')] }), 'question'],
    ['a question', base({ attention: [att('question')] }), 'question'],
    ["Ratchet's own open question", base({ openQuestions: 1 }), 'question'],
    ['a crashed run', base({ runs: [run({ state: 'crashed', live: false })] }), 'oops'],
    ['a failed run', base({ runs: [run({ state: 'failed', live: false })] }), 'oops'],
    ['a stalled run', base({ attention: [att('stalled')] }), 'oops'],
    ['a critical error is serious', base({ attention: [att('critical_error')] }), 'panic'],
    ['two broken runs are serious', base({ runs: [run({ state: 'crashed', live: false }), run({ slug: 't', state: 'failed', live: false })] }), 'panic'],
    ['near the cap', base({ liveWorkers: 5, cap: 6 }), 'strained'],
    ['at the cap', base({ liveWorkers: 6, cap: 6 }), 'strained'],
    ['under the cap', base({ liveWorkers: 4, cap: 6 }), 'on_it'],
    ['cap 0 → never strained', base({ liveWorkers: 50, cap: 0 }), 'on_it'],
    ['cap null → never strained', base({ liveWorkers: 50, cap: null }), 'on_it'],
    ['a live worker', base({ liveWorkers: 1 }), 'on_it'],
    ['a yielded run', base({ attention: [att('yield')] }), 'skeptical'],
    ['feedback waiting', base({ attention: [att('feedback')] }), 'skeptical'],
    ['cap contention', base({ attention: [att('cap_reached')] }), 'skeptical'],
    ['a run just finished', base({ justFinished: 1 }), 'yes'],
    ['replying with a quiet fleet', base({ replying: true }), 'talking'],
  ])('%s → %s', (_label, input, mood) => {
    expect(moodOf(input)).toBe(mood)
  })

  it.each([
    ['question beats panic', base({ attention: [att('question'), att('critical_error')] }), 'question'],
    ['panic beats a single crash', base({ attention: [att('critical_error')], runs: [run({ state: 'crashed' })] }), 'panic'],
    ['panic beats near-cap', base({ attention: [att('critical_error')], liveWorkers: 6, cap: 6 }), 'panic'],
    ['oops beats near-cap', base({ runs: [run({ state: 'crashed', live: false })], liveWorkers: 6, cap: 6 }), 'oops'],
    ['yes beats on_it', base({ justFinished: 1, liveWorkers: 2 }), 'yes'],
    ['yes beats skeptical', base({ justFinished: 1, attention: [att('yield')] }), 'yes'],
    ['strained beats on_it', base({ liveWorkers: 5, cap: 6 }), 'strained'],
    ['on_it beats skeptical', base({ liveWorkers: 1, attention: [att('yield')] }), 'on_it'],
    ['skeptical beats talking', base({ attention: [att('yield')], replying: true }), 'skeptical'],
    ['on_it beats talking', base({ replying: true, liveWorkers: 1 }), 'on_it'],
  ])('precedence: %s', (_label, input, mood) => {
    expect(moodOf(input)).toBe(mood)
  })

  it('counts the orchestrator out of the worker count: the caller passes liveWorkers', () => {
    expect(moodOf(base({ runs: [run({ orchestrator: true })], liveWorkers: 0 }))).toBe('idle')
  })
})

describe('moodReason', () => {
  it.each([
    ['question', base({ attention: [att('question'), att('live_question')] }), 'question: 2 questions waiting on you'],
    ['question', base({ openQuestions: 1 }), 'question: 1 question waiting on you'],
    ['panic', base({ attention: [att('critical_error')] }), 'panic: the daemon reported a critical error'],
    ['panic', base({ runs: [run({ state: 'crashed' }), run({ slug: 't', state: 'failed' })] }), 'panic: 2 runs down at once'],
    ['oops', base({ runs: [run({ state: 'crashed' })] }), 'oops: 1 run crashed or stuck'],
    ['strained', base({ liveWorkers: 5, cap: 6 }), 'strained: 5 of 6 sessions in use'],
    ['on_it', base({ liveWorkers: 3 }), 'on_it: 3 running'],
    ['skeptical', base({ attention: [att('yield')] }), 'skeptical: 1 waiting on your review'],
    ['yes', base({ justFinished: 1 }), 'yes: 1 run just finished'],
    ['idle', base(), 'idle: nothing needs you'],
    ['talking', base({ replying: true }), 'talking: answering you'],
    ['loading', base({ loaded: false }), 'loading: waiting for the fleet view'],
  ])('%s', (mood, input, text) => {
    expect(moodReason(mood, input)).toBe(text)
  })
})

describe('faceUrl', () => {
  it('points inside the served assets directory, relative to the page', () => {
    expect(faceUrl('idle')).toBe('./assets/ratchet/ratchet_idle.png')
    expect(faceUrl('strained')).toBe('./assets/ratchet/ratchet_strained.png')
  })

  it('gives the pre-poll state its own mood name but the talking face', () => {
    expect(faceFile('loading')).toBe('ratchet_talking')
    expect(faceUrl('loading')).toBe('./assets/ratchet/ratchet_talking.png')
  })
})

describe('RatchetFace', () => {
  it('draws the mood with alt text and the configured name', () => {
    const wrapper = mountPane(RatchetFace, { mood: 'question', size: 32 })
    const img = wrapper.get('img.ratchet-face')
    expect(img.attributes('src')).toBe('./assets/ratchet/ratchet_question.png')
    expect(img.attributes('alt')).toBe('Ratchet, question')
    expect(img.attributes('width')).toBe('32')
    expect(wrapper.classes()).not.toContain('ratchet-off')
  })

  it('shows the off face, crossed out, when the assistant is not running', () => {
    const wrapper = mountPane(RatchetFace, { mood: 'idle', off: true })
    expect(wrapper.classes()).toContain('ratchet-off')
    expect(wrapper.get('img').attributes('src')).toBe('./assets/ratchet/ratchet_off.png')
    expect(wrapper.get('img').attributes('alt')).toBe('Ratchet, off')
  })

  it('declares the fade and its reduced-motion opt-out in its own stylesheet', async () => {
    const source = (await import('../src/components/RatchetFace.vue?raw')).default
    expect(source).toMatch(/transition:\s*opacity\s+(1[0-4]\d|150)ms/)
    expect(source).toContain('prefers-reduced-motion: reduce')
  })
})

const appState = (extra = {}) => ({
  runs: [], attention: [], totals: { runs: 0, live: 0, attention: 0 },
  config: { max_concurrent_sessions: 6 }, ...extra,
})

describe('Ratchet in the shell', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    fetchProjects.mockResolvedValue({ projects: [{ id: 'general', name: 'General', color: 'Blue', assistant: { lifecycle: 'running' } }] })
    fetchRunRelations.mockResolvedValue({ run: {}, relations: [] })
    window.localStorage.clear()
    document.title = 'lmer'
  })

  it('draws the mood face in the app bar', async () => {
    fetchState.mockResolvedValue(appState({ runs: [{ slug: 'a', state: 'running', live: true, orchestrator: false }], totals: { live: 1 } }))
    const wrapper = mountPane(App)
    await flushPromises()
    const face = wrapper.get('header .bar-cluster img.ratchet-face')
    expect(face.attributes('src')).toBe('./assets/ratchet/ratchet_on_it.png')
    expect(face.attributes('alt')).toBe('Ratchet, on_it')
    wrapper.unmount()
  })

  it('holds the last face while the fleet is offline and says so', async () => {
    vi.useFakeTimers()
    fetchState.mockResolvedValue(appState({ runs: [{ slug: 'a', state: 'running', live: true, orchestrator: false }], totals: { live: 1 } }))
    const wrapper = mountPane(App)
    await flushPromises()
    const face = () => wrapper.get('header .bar-cluster img.ratchet-face')
    expect(face().attributes('src')).toBe('./assets/ratchet/ratchet_on_it.png')
    fetchState.mockRejectedValueOnce(new Error('Network unavailable'))
    await vi.advanceTimersByTimeAsync(10000)
    await flushPromises()
    expect(face().attributes('src')).toBe('./assets/ratchet/ratchet_on_it.png')
    expect(wrapper.get('header .bar-cluster button').attributes('aria-label')).toContain('fleet unreachable')
    vi.useRealTimers()
    wrapper.unmount()
  })
})
