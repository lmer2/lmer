import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import App from '../src/App.vue'
import AssistantChat from '../src/components/AssistantChat.vue'
import RatchetFace from '../src/components/RatchetFace.vue'
import RunNav from '../src/components/RunNav.vue'
import { fetchAssistant, fetchProjects, fetchRunRelations, fetchState } from '../src/api.js'
import { chooseProject, mountPane } from './mount.js'
import { DEFAULT_ASSISTANT_NAME, applyAssistantName, assistantName, faceFile, faceUrl, moodOf, moodReason, offReason } from '../src/ratchet.js'

vi.mock('../src/api.js', () => ({
  fetchAssistant: vi.fn(),
  fetchProjects: vi.fn(),
  fetchState: vi.fn(), forgetRun: vi.fn(), prune: vi.fn(), rescan: vi.fn(),
  fetchRunRelations: vi.fn().mockResolvedValue({ run: {}, relations: [] }),
  rotateAssistant: vi.fn(), startAssistant: vi.fn(), stopAssistant: vi.fn(),
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
    ['two broken runs stay oops', base({ runs: [run({ state: 'crashed', live: false }), run({ slug: 't', state: 'failed', live: false })] }), 'oops'],
    ['two stalled runs stay oops', base({ attention: [att('stalled'), att('stalled', { slug: 't' })], liveWorkers: 2 }), 'oops'],
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
    ['one orchestrator ask is not double counted', base({ attention: [att('live_question', { orchestrator: true })], openQuestions: 1 }), 'question'],
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
    ['question', base({ attention: [att('live_question', { orchestrator: true })], openQuestions: 1 }), 'question: 1 question waiting on you'],
    ['panic', base({ attention: [att('critical_error')] }), 'panic: the daemon reported a critical error'],
    ['oops', base({ runs: [run({ state: 'crashed' }), run({ slug: 't', state: 'failed' })] }), 'oops: 2 runs crashed or stuck'],
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
    const face = wrapper.get('header .bar-badge img.ratchet-face')
    expect(face.attributes('src')).toBe('./assets/ratchet/ratchet_on_it.png')
    expect(face.attributes('alt')).toBe('Ratchet, on_it')
    wrapper.unmount()
  })

  it('holds the last face while the fleet is offline and says so', async () => {
    vi.useFakeTimers()
    fetchState.mockResolvedValue(appState({ runs: [{ slug: 'a', state: 'running', live: true, orchestrator: false }], totals: { live: 1 } }))
    const wrapper = mountPane(App)
    await flushPromises()
    const face = () => wrapper.get('header .bar-badge img.ratchet-face')
    expect(face().attributes('src')).toBe('./assets/ratchet/ratchet_on_it.png')
    fetchState.mockRejectedValueOnce(new Error('Network unavailable'))
    await vi.advanceTimersByTimeAsync(10000)
    await flushPromises()
    expect(face().attributes('src')).toBe('./assets/ratchet/ratchet_on_it.png')
    expect(wrapper.get('header .bar-badge button').attributes('aria-label')).toContain('fleet unreachable')
    vi.useRealTimers()
    wrapper.unmount()
  })

  it('sets the tab icon to the current face', async () => {
    document.head.innerHTML = '<link rel="icon" id="favicon" href="./assets/ratchet/ratchet_talking.png">'
    fetchState.mockResolvedValue(appState({ attention: [{ slug: 'q', attention: { reason: 'question' } }], totals: { attention: 1 } }))
    const wrapper = mountPane(App)
    await flushPromises()
    expect(document.getElementById('favicon').getAttribute('href')).toBe('./assets/ratchet/ratchet_question.png')
    wrapper.unmount()
  })

  it('pairs the bar face with a plain-text status, in the switcher\'s row on a phone, and the off label when stopped', async () => {
    fetchProjects.mockResolvedValue({ projects: [{ id: 'general', name: 'General', color: 'Blue', assistant: { lifecycle: 'stopped' } }] })
    fetchState.mockResolvedValue(appState())
    window.history.replaceState({}, '', '/?workspace=general')
    const wrapper = mountPane(App)
    await flushPromises()
    // jsdom's 1024px window is a phone to Vuetify's `lg` breakpoint.
    expect(wrapper.find('header .bar-badge .ratchet-bar-label').exists()).toBe(false)
    expect(wrapper.get('header .bar-extension .ratchet-bar-label').text()).toBe('off: stopped')
    expect(wrapper.get('header .bar-badge img.ratchet-face').attributes('src')).toBe('./assets/ratchet/ratchet_off.png')
    wrapper.unmount()
  })

  it('keeps the pane toolbar to the face, its own lifecycle carried in the face\'s name', async () => {
    fetchAssistant.mockResolvedValue({ running: false, lifecycle: 'stopped' })
    const workspace = { id: 'alpha', name: 'Alpha', color: 'Blue' }
    const wrapper = mountPane(AssistantChat, { workspace, mood: 'idle', moodReason: 'idle: nothing needs you' })
    await flushPromises()
    expect(wrapper.get('.uber-toolbar').text()).not.toContain('off: stopped')
    expect(wrapper.get('.uber-identity').attributes('aria-label')).toContain('off: stopped')
    expect(wrapper.get('.uber-identity img.ratchet-face').attributes('src')).toBe('./assets/ratchet/ratchet_off.png')
    wrapper.unmount()
  })

  it('gives each project\'s Ratchet row, in the sidebar and the main list, that project\'s face', async () => {
    const alpha = { id: 'alpha', name: 'Alpha', color: 'Blue', assistant: { lifecycle: 'running' } }
    const beta = { id: 'beta', name: 'Beta', color: 'Blue', assistant: { lifecycle: 'running' } }
    const gamma = { id: 'gamma', name: 'Gamma', color: 'Blue', assistant: { lifecycle: 'stopped' } }
    fetchProjects.mockResolvedValue({ projects: [alpha, beta, gamma] })
    const supervisor = (id, state = 'running') => ({ workspace_id: id, host: 'h', project: id, slug: 'uber', state, live: state === 'running', orchestrator: true })
    const broken = { workspace_id: 'beta', host: 'h', project: 'beta', slug: 'b1', state: 'running', live: true, orchestrator: false, attention: { reason: 'critical_error' } }
    fetchState.mockResolvedValue(appState({
      runs: [supervisor('alpha'), supervisor('beta'), supervisor('gamma', 'held'), broken,
        { workspace_id: 'alpha', host: 'h', project: 'alpha', slug: 'w', state: 'running', live: true, orchestrator: false }],
      attention: [broken],
      totals: { live: 4, attention: 1 },
    }))
    window.history.replaceState({}, '', '/')
    const wrapper = mountPane(App)
    await flushPromises()
    const face = (name) => wrapper.get(`.project-nav[aria-label="${name}"] .run-row img.ratchet-face`).attributes('src')
    expect(face('Alpha')).toBe('./assets/ratchet/ratchet_on_it.png')
    expect(face('Beta')).toBe('./assets/ratchet/ratchet_panic.png')
    expect(face('Gamma')).toBe('./assets/ratchet/ratchet_off.png')
    const main = wrapper.findAll('.v-main section').find((section) => section.find('h2').text() === 'Alpha')
    expect(main.get('.run-row img.ratchet-face').attributes('src')).toBe('./assets/ratchet/ratchet_on_it.png')
    wrapper.unmount()
  })

  it('flashes `yes` on the Unassigned Ratchet row for an unassigned completion, and takes `off` from the row', async () => {
    vi.useFakeTimers()
    const uber = (state) => ({ workspace_id: null, host: 'h', project: 'loose', slug: 'uber', state, live: state === 'running', orchestrator: true })
    const worker = (state) => ({ workspace_id: null, host: 'h', project: 'loose', slug: 'w', state, live: state === 'running', orchestrator: false })
    fetchState.mockResolvedValue(appState({ runs: [uber('running'), worker('running')], totals: { live: 2 } }))
    window.history.replaceState({}, '', '/')
    const wrapper = mountPane(App)
    await flushPromises()
    const face = () => wrapper.get('.unassigned-runs .run-row img.ratchet-face').attributes('src')
    expect(face()).toBe('./assets/ratchet/ratchet_on_it.png')
    fetchState.mockResolvedValue(appState({ runs: [uber('running'), worker('complete')], totals: { live: 1 } }))
    await vi.advanceTimersByTimeAsync(10000)
    await flushPromises()
    expect(face()).toBe('./assets/ratchet/ratchet_yes.png')
    fetchState.mockResolvedValue(appState({ runs: [uber('held'), worker('complete')], totals: {} }))
    await vi.advanceTimersByTimeAsync(10000)
    await flushPromises()
    expect(face()).toBe('./assets/ratchet/ratchet_off.png')
    vi.useRealTimers()
    wrapper.unmount()
  })

  it('falls back to the row\'s own state for a Ratchet row given no face', () => {
    const row = (state) => ({ host: 'h', project: 'p', slug: 'uber', state, orchestrator: true })
    const running = mountPane(RunNav, { runs: [row('running')] })
    expect(running.get('img.ratchet-face').attributes('src')).toBe('./assets/ratchet/ratchet_idle.png')
    const held = mountPane(RunNav, { runs: [row('held')], face: { mood: 'on_it' } })
    expect(held.get('img.ratchet-face').attributes('src')).toBe('./assets/ratchet/ratchet_off.png')
    running.unmount()
    held.unmount()
  })

  it('names the platform and the daemon\'s version below the project list, not in the bar', async () => {
    fetchState.mockResolvedValue(appState({ config: { max_concurrent_sessions: 6, version: '0.11.0' } }))
    const wrapper = mountPane(App)
    await flushPromises()
    expect(wrapper.findAll('.nav-footer > div').map((line) => line.text())).toEqual(['lmer platform', 'v0.11.0'])
    expect(wrapper.get('header').text()).not.toContain('lmer platform')
    fetchState.mockResolvedValue(appState())
    wrapper.unmount()
    const older = mountPane(App)
    await flushPromises()
    expect(older.findAll('.nav-footer > div').map((line) => line.text())).toEqual(['lmer platform'])
    older.unmount()
  })

  it('keys completion history by run identity, not slug', async () => {
    vi.useFakeTimers()
    fetchState.mockResolvedValue(appState({
      runs: [
        { workspace_id: 'general', host: 'one.example', project: 'p', slug: 'same', state: 'complete', live: false, orchestrator: false },
        { workspace_id: 'general', host: 'two.example', project: 'b', slug: 'same', state: 'running', live: true, orchestrator: false },
      ],
      totals: { live: 1 },
    }))
    const wrapper = mountPane(App)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(10000)
    await flushPromises()
    expect(wrapper.get('header .bar-badge img.ratchet-face').attributes('src')).toBe('./assets/ratchet/ratchet_on_it.png')
    vi.useRealTimers()
    wrapper.unmount()
  })

  it('scopes each pane to its own project when the view moves to All projects', async () => {
    const alpha = { id: 'alpha', name: 'Alpha', color: 'Blue', assistant: { lifecycle: 'running' } }
    const beta = { id: 'beta', name: 'Beta', color: 'Blue', assistant: { lifecycle: 'running' } }
    fetchProjects.mockResolvedValue({ projects: [alpha, beta] })
    fetchAssistant.mockResolvedValue({ running: true, lifecycle: 'running', session_id: 's-alpha' })
    fetchState.mockResolvedValue(appState({
      runs: [{ workspace_id: 'beta', host: 'h', project: 'p', slug: 'b1', state: 'running', live: true, orchestrator: false, attention: { reason: 'critical_error' } }],
      attention: [{ workspace_id: 'beta', host: 'h', project: 'p', slug: 'b1', attention: { reason: 'critical_error' } }],
      totals: { live: 1, attention: 1 },
    }))
    window.history.replaceState({}, '', '/?workspace=alpha')
    const wrapper = mountPane(App)
    await flushPromises()
    await wrapper.get('.project-assistant[aria-label^="Chat with Alpha"]').trigger('click')
    await flushPromises()
    await chooseProject(wrapper, 'All projects')
    await flushPromises()
    const pane = wrapper.findAllComponents(AssistantChat).find((candidate) => candidate.get('.uber-toolbar').isVisible())
    expect(pane.props('mood')).toBe('idle')
    // The bar is the fleet view, so it does see Beta's critical error.
    expect(wrapper.get('header .bar-badge img.ratchet-face').attributes('src')).toBe('./assets/ratchet/ratchet_panic.png')
    wrapper.unmount()
  })
})

describe('offReason', () => {
  it.each([
    ['stopped', 'off: stopped'],
    ['starting', 'off: starting'],
    [undefined, 'off: stopped'],
    ['', 'off: stopped'],
  ])('%s → %s', (lifecycle, text) => {
    expect(offReason(lifecycle)).toBe(text)
  })
})
