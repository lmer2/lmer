import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import App from '../src/App.vue'
import AssistantChat from '../src/components/AssistantChat.vue'
import PlatformSettings from '../src/components/PlatformSettings.vue'
import Chat from '../src/components/Chat.vue'
import RunDetail from '../src/components/RunDetail.vue'
import RunNav from '../src/components/RunNav.vue'
import { contrastRatio, projectColor, projectColors, projectTextColor, storedProjectColor } from '../src/projects.js'
import RunCard from '../src/components/RunCard.vue'
import ProjectNav from '../src/components/ProjectNav.vue'
import ProjectSettings from '../src/components/ProjectSettings.vue'
import ProjectSlots from '../src/components/ProjectSlots.vue'
import * as api from '../src/api.js'
import { chooseProject, mountPane, deferred } from './mount.js'

vi.mock('../src/api.js', async importOriginal => ({
  ...await importOriginal(), fetchState: vi.fn(), fetchProjects: vi.fn(),
  fetchAssistant: vi.fn(), fetchAssistantConfig: vi.fn(), fetchAssistantInstructions: vi.fn(),
  startAssistant: vi.fn(), stopAssistant: vi.fn(), rotateAssistant: vi.fn(), fetchAssistantHistory: vi.fn(),
  fetchPlatformSettings: vi.fn(), fetchConversationRequests: vi.fn(), parkConversationRequest: vi.fn(),
  setAssistantInstructions: vi.fn(), updateProject: vi.fn(), archiveProject: vi.fn(), setPlatformSettings: vi.fn(),
  fetchSessionMessages: vi.fn(), sendSessionInput: vi.fn(), uploadSessionFile: vi.fn(),
  fetchChannelRoutes: vi.fn(), setProjectChannels: vi.fn(),
}))

const projects = [
  { id: 'alpha', name: 'Alpha research and development', color: 'Blue', assistant: { lifecycle: 'running' } },
  { id: 'beta', name: 'Beta', color: 'Blue', assistant: { lifecycle: 'waiting', queue_position: 1 } },
]
const run = (workspace, slug) => ({ host: 'forge.test', project: 'same/repository', slug,
  label: slug, workspace_id: workspace.id, workspace_name: workspace.name, workspace_color: workspace.color,
  state: 'waiting_on_you', attention: { reason: 'question' }, live: false })
const page = (session) => ({ session, messages: [], live: true, from: 0, cursor: 0, total: 0, sessions: [], note: null })

beforeEach(() => {
  vi.clearAllMocks()
  window.history.replaceState({}, '', '/')
  window.localStorage.clear()
  const runs = projects.map((project, i) => run(project, `question-${i}`))
  api.fetchState.mockResolvedValue({ runs, attention: runs, totals: {}, relations: [] })
  api.fetchProjects.mockResolvedValue({ projects })
  api.fetchAssistantConfig.mockResolvedValue({})
  api.fetchAssistantInstructions.mockResolvedValue({ instructions: null })
  api.fetchAssistant.mockImplementation(id => Promise.resolve({ running: false,
    lifecycle: id === 'beta' ? 'waiting' : 'stopped', queue_position: id === 'beta' ? 1 : null }))
  api.fetchSessionMessages.mockImplementation(id => Promise.resolve(page(id)))
  api.fetchAssistantHistory.mockImplementation(id => Promise.resolve(page(id)))
  api.sendSessionInput.mockResolvedValue({})
  api.updateProject.mockResolvedValue({})
  api.setAssistantInstructions.mockImplementation((_id, instructions) => Promise.resolve({ instructions, limit: 4000 }))
  api.fetchPlatformSettings.mockResolvedValue({ max_live_assistants: 4, max_concurrent_sessions: 8 })
  api.fetchConversationRequests.mockResolvedValue({ requests: [] })
  api.setPlatformSettings.mockResolvedValue({})
  api.archiveProject.mockResolvedValue({})
  api.fetchChannelRoutes.mockResolvedValue({ routes: [] })
  api.setProjectChannels.mockImplementation((_id, channels) => Promise.resolve({ routes: channels.map(label => ({ label })) }))
})

it('groups every All-project status locally and keeps the actual chat destination through list switches', async () => {
  const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  const sections = wrapper.findAll('main section')
  expect(sections).toHaveLength(2)
  expect(sections[0].text()).toContain('Alpha research and development')
  expect(sections[0].findAll('.run-row')).toHaveLength(1)
  expect(sections[0].text()).toContain('question-0')
  expect(sections[0].text()).not.toContain('question-1')
  expect(sections[1].text()).toContain('Beta')
  expect(sections[1].findAll('.run-row')).toHaveLength(1)
  expect(sections[1].text()).toContain('question-1')
  expect(sections.every(section => !section.find('.section-title').exists())).toBe(true)
  const chat = wrapper.get('.project-assistant[aria-label^="Chat with Alpha"]')
  await chat.trigger('click')
  await flushPromises()
  expect(window.location.search).toBe('?workspace=alpha')
  expect(wrapper.getComponent(AssistantChat).props('workspace').id).toBe('alpha')
  await chooseProject(wrapper, 'All projects')
  await flushPromises()
  expect(window.location.search).toBe('')
  expect(wrapper.getComponent(AssistantChat).props('workspace').id).toBe('alpha')
  expect(wrapper.getComponent(AssistantChat).element.parentElement.style.display).not.toBe('none')
  expect(wrapper.get('.project-switcher').text()).toContain('All projects')
  const groups = wrapper.findAll('.project-nav')
  await groups[0].get('.project-toggle').trigger('click')
  expect(groups[0].get('.project-toggle').attributes('aria-expanded')).toBe('false')
  expect(groups[1].get('.project-toggle').attributes('aria-expanded')).toBe('true')
})

it('shows waiting/cancel with an explicit project and keeps a draft until the assistant starts', async () => {
  const wrapper = mountPane(AssistantChat, { workspace: projects[1] })
  await flushPromises()
  expect(wrapper.text()).toContain('Beta · Ratchet')
  expect(wrapper.text()).toContain('Waiting to start — assistant limit reached · queue position 1')
  await wrapper.get('textarea').setValue('Beta draft only')
  api.stopAssistant.mockResolvedValue({ running: false, lifecycle: 'stopped' })
  await wrapper.findAll('button').find(button => button.text() === 'Cancel start').trigger('click')
  await flushPromises()
  expect(api.stopAssistant).toHaveBeenCalledWith('beta')
  expect(wrapper.get('textarea').element.value).toBe('Beta draft only')
  api.startAssistant.mockResolvedValue({ running: true, lifecycle: 'running', session_id: 's-beta' })
  await wrapper.findAll('button').find(button => button.text() === 'Start').trigger('click')
  await flushPromises()
  expect(api.startAssistant).toHaveBeenCalledWith('beta')
  expect(wrapper.getComponent(Chat).props('workspaceId')).toBe('beta')
  expect(wrapper.getComponent(Chat).get('textarea').element.value).toBe('Beta draft only')
})

it('keeps all sidebar projects and live local counts while filtering the selected main view', async () => {
  const empty = { id: 'empty', name: 'Empty project', color: 'Teal', assistant: { lifecycle: 'stopped' } }
  api.fetchProjects.mockResolvedValue({ projects: [...projects, empty, { id: 'archived', name: 'Archived', archived: true }] })
  window.history.replaceState({}, '', '/?workspace=beta')
  const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  const groups = wrapper.findAllComponents(ProjectNav)
  expect(groups.map(group => group.props('project').id)).toEqual(['alpha', 'beta', 'empty'])
  expect(groups[0].get('.project-attention').attributes('aria-label')).toBe('1 need you')
  expect(groups[0].text()).toContain('question-0')
  expect(groups[0].text()).not.toContain('question-1')
  expect(groups[1].classes()).toContain('project-nav-selected')
  expect(groups[2].text()).toContain('nothing tracked yet')
  expect(wrapper.get('main').text()).toContain('question-1')
  expect(wrapper.get('main').text()).not.toContain('question-0')
  await groups[0].get('.project-toggle').trigger('click')
  const next = [run(projects[0], 'alpha-new-question'), run(projects[1], 'beta-current')]
  api.fetchState.mockResolvedValue({ runs: next, attention: [next[0]], totals: {}, relations: [] })
  window.dispatchEvent(new Event('focus'))
  await flushPromises()
  expect(groups[0].props('runs').map(row => row.slug)).toEqual(['alpha-new-question'])
  expect(groups[1].props('attention')).toEqual([])
  expect(groups[1].find('.project-attention').exists()).toBe(false)
  expect(groups[0].get('.project-toggle').attributes('aria-expanded')).toBe('false')
  await chooseProject(wrapper, 'All projects')
  await flushPromises()
  expect(wrapper.get('main').text()).toContain('alpha-new-question')
  expect(wrapper.get('main').text()).toContain('beta-current')
  expect(wrapper.findAllComponents(ProjectNav)).toHaveLength(3)
  expect(groups[0].get('.project-toggle').attributes('aria-expanded')).toBe('false')
})

it('joins service holders against the full fleet and opens the existing slot-management section', async () => {
  const rows = projects.map((project, i) => ({ ...run(project, `worker-${i}`), session: { id: `slot-session-${i}` } }))
  const shared = { name: 'Shared slot', state: 'occupied', applicable_projects: ['alpha', 'beta'],
    occupants: [{ session_id: 'slot-session-0' }], service_occupants: [] }
  api.fetchState.mockResolvedValue({ runs: rows, attention: [], slots: [shared], totals: {}, relations: [] })
  window.history.replaceState({}, '', '/?workspace=beta')
  const wrapper = mountPane(App)
  await flushPromises()
  const summary = wrapper.getComponent(ProjectSlots)
  expect(summary.props('runs')).toHaveLength(2)
  expect(summary.text()).toContain(projects[0].name)
  expect(summary.text()).toContain('Unavailable for a new run.')
  api.fetchState.mockResolvedValue({ runs: rows, attention: [], slots: [{ ...shared, state: 'free', occupants: [] }], totals: {}, relations: [] })
  window.dispatchEvent(new Event('focus'))
  await flushPromises()
  expect(summary.text()).toContain('Available now')
  await summary.get('a').trigger('click')
  await flushPromises()
  expect(wrapper.getComponent(PlatformSettings).props('focusSection')).toBe('slots')
  expect(document.activeElement.id).toBe('platform-service-slots')
  expect(wrapper.get('#platform-service-slots').text()).toBe('Service slots')
  expect(api.setPlatformSettings).not.toHaveBeenCalled()
})

it.each(['running', 'waiting', 'starting', 'rotating', 'stopping', 'stopped', 'failed', undefined])(
  'keeps the compact assistant control reachable when collapsed with lifecycle %s', async lifecycle => {
    const project = { ...projects[1], assistant: { lifecycle, queue_position: lifecycle === 'waiting' ? 2 : null } }
    const wrapper = mountPane(ProjectNav, { project })
    const toggle = wrapper.get('.project-toggle')
    const chat = wrapper.get('.project-assistant')
    expect(chat.attributes('aria-label')).toBe(`Chat with Beta — assistant ${lifecycle || 'unavailable'}${lifecycle === 'waiting' ? ' · queue 2' : ''}`)
    expect(toggle.element.tagName).toBe('BUTTON')
    expect(chat.element.tagName).toBe('BUTTON')
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    await chat.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(wrapper.emitted('chat')).toEqual([[project]])
  },
)

it('has one global settings cog in the header, with a separate sidebar creation button', async () => {
  api.fetchState.mockResolvedValue({ runs: [], attention: [], totals: {}, relations: [] })
  const wrapper = mountPane(App, {}, { global: { stubs: { PlatformSettings: true } } })
  await flushPromises()
  const settings = wrapper.findAll('button[aria-label="Platform settings"]')
  expect(settings).toHaveLength(1)
  expect(settings[0].element.closest('header')).not.toBeNull()
  expect(settings[0].find('.v-icon').exists()).toBe(true)
  expect(wrapper.get('.v-navigation-drawer--left').text()).not.toContain('Platform settings')
  await wrapper.get('.project-assistant[aria-label^="Chat with Beta"]').trigger('click')
  await flushPromises()
  expect(wrapper.get('.v-navigation-drawer--right').classes()).toContain('v-navigation-drawer--active')
  expect(wrapper.get('.v-navigation-drawer--left').classes()).not.toContain('v-navigation-drawer--active')
  await settings[0].trigger('click')
  await flushPromises()
  expect(wrapper.findComponent(PlatformSettings).exists()).toBe(true)
  expect(wrapper.get('.v-navigation-drawer--right').classes()).not.toContain('v-navigation-drawer--active')
  const create = wrapper.get('.project-create button')
  expect(create.text()).toBe('New project')
  expect(create.classes()).toContain('v-btn--variant-tonal')
  expect(create.find('.v-icon').exists()).toBe(true)
  await create.trigger('click')
  await flushPromises()
  expect(document.querySelector('.v-dialog input')).not.toBeNull()
})

it('names and colors the selected project in the bar and switches projects from it', async () => {
  const custom = { id: 'pink', name: 'Hot pink', color: '#ff69b4', assistant: { lifecycle: 'stopped' } }
  api.fetchProjects.mockResolvedValue({ projects: [...projects, custom, { id: 'archived', name: 'Archived', archived: true }] })
  window.history.replaceState({}, '', '/?workspace=beta')
  const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true, ProjectSettings: true } } })
  await flushPromises()
  expect(wrapper.find('.project-identity').exists()).toBe(false)
  expect(wrapper.find('main [role="tab"]').exists()).toBe(false)
  const bar = () => wrapper.get('header.v-app-bar')
  expect(wrapper.get('.project-switcher').text()).toBe('Beta')
  expect(wrapper.get('.project-switcher').attributes('aria-label')).toBe('project: Beta — switch project')
  expect(bar().element.style.backgroundColor).toBe('rgb(34, 94, 201)')
  expect(bar().element.style.color).toBe('rgb(255, 255, 255)')
  await wrapper.get('.project-switcher').trigger('click')
  await flushPromises()
  const entries = [...document.querySelectorAll('.project-switcher-list .v-list-item-title')].map(title => title.textContent.trim())
  expect(entries).toEqual(['All projects', 'Alpha research and development', 'Beta', 'Hot pink',
    'Project settings for Beta', 'New project'])
  expect(document.querySelector('.project-switcher-list [aria-label="1 need you"]')).not.toBeNull()
  await chooseProject(wrapper, 'Hot pink')
  expect(window.location.search).toBe('?workspace=pink')
  expect(wrapper.get('.project-switcher').text()).toBe('Hot pink')
  expect(bar().element.style.backgroundColor).toBe('rgb(255, 105, 180)')
  expect(bar().element.style.color).toBe('rgb(0, 0, 0)')
  expect(wrapper.findAllComponents(ProjectNav).find(nav => nav.props('project').id === 'pink').element.style.borderInlineStartColor)
    .toBe('#ff69b4')
  await chooseProject(wrapper, 'Project settings for Hot pink')
  expect(wrapper.getComponent(ProjectSettings).props('project').id).toBe('pink')
  await chooseProject(wrapper, 'All projects')
  expect(window.location.search).toBe('')
  expect(wrapper.get('.project-switcher').text()).toBe('All projects')
  expect(bar().element.style.backgroundColor).toBe('')
  expect(bar().element.style.color).toBe('')
  await chooseProject(wrapper, 'New project')
  expect(document.querySelector('.v-dialog input')).not.toBeNull()
})

it('puts the switcher in the bar row itself at desktop width', async () => {
  const width = window.innerWidth
  window.innerWidth = 1440
  try {
    window.history.replaceState({}, '', '/?workspace=beta')
    const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
    await flushPromises()
    expect(wrapper.findAll('.project-switcher')).toHaveLength(1)
    expect(wrapper.find('.bar-switcher .project-switcher').exists()).toBe(true)
    expect(wrapper.find('.v-toolbar__extension').exists()).toBe(false)
    await chooseProject(wrapper, 'Alpha research and development')
    expect(window.location.search).toBe('?workspace=alpha')
  } finally { window.innerWidth = width }
})

it.each([
  ['All projects', ''],
  ['a project sharing the tone\'s hue', 'green'],
  ['a dark custom project', 'night'],
  ['a light custom project', 'pink'],
  ['a project with no Ratchet run', 'alpha'],
])('draws Ratchet\'s badge in the bar\'s own colour, with no disc, on %s', async (_label, workspace) => {
  const extra = [{ id: 'green', name: 'Green', color: 'Green' }, { id: 'night', name: 'Night', color: '#101030' },
    { id: 'pink', name: 'Pink', color: '#ff69b4' }]
  api.fetchProjects.mockResolvedValue({ projects: [...projects, ...extra] })
  const home = [...projects, ...extra].find(project => project.id === workspace) || projects[0]
  const supervisor = { ...run(home, 'uber'), orchestrator: true, state: 'running', attention: null, live: true }
  const rows = workspace === 'alpha' ? [{ ...run(home, 'worker'), attention: null }] : [supervisor]
  api.fetchState.mockResolvedValue({ runs: rows, attention: [], totals: {}, relations: [] })
  window.history.replaceState({}, '', workspace ? `/?workspace=${workspace}` : '/')
  const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  const supervised = workspace !== 'alpha'
  const badge = wrapper.get(supervised ? 'header button[aria-label^="Ratchet — running"]' : 'header button[aria-label^="talk to Ratchet"]')
  expect(badge.classes()).toContain('ratchet-badge')
  expect(badge.classes().filter((name) => name.startsWith('text-') || name === 'robot-on-surface')).toEqual([])
})

it('gives project-colored chips and buttons the contrast rule\'s text color, presets unchanged', async () => {
  const pink = { id: 'pink', name: 'Hot pink', color: '#ff69b4', assistant: { lifecycle: 'stopped' } }
  const shared = { name: 'Shared slot', state: 'occupied', applicable_projects: ['pink', 'alpha'],
    occupants: [{ session_id: 'slot-session' }], service_occupants: [] }
  for (const [project, text] of [[pink, 'rgb(0, 0, 0)'], [{ ...pink, color: 'Blue' }, 'rgb(255, 255, 255)']]) {
    const runs = [{ ...run(project, 'held'), session: { id: 'slot-session' }, live: true }]
    const slots = mountPane(ProjectSlots, { slots: [shared], runs,
      projects: [project, projects[0]], loaded: true })
    const chip = slots.get('.slot-holder .v-chip')
    expect(chip.text()).toBe('Hot pink')
    expect(chip.element.style.color).toBe(text)
    const detail = mountPane(RunDetail, { run: run(project, 'detail'), identifiedWorkspaceId: null },
      { global: { stubs: { Chat: true, AskChannel: true, RelatedRuns: true, RunMeta: true, ThreadLinks: true } } })
    expect(detail.get('.run-project-identity').element.style.color).toBe(text)
    const settings = mountPane(ProjectSettings, { project: { ...project, repositories: [], settings: {} } })
    await flushPromises()
    expect(settings.get('.project-color .v-chip').element.style.color).toBe(text)
    for (const wrapper of [slots, detail, settings]) wrapper.unmount()
  }
})

it('reads preset names and hex values, and picks the more contrasting text color', () => {
  expect(projectColor('Blue')).toBe('#225ec9')
  expect(projectColor('#FF69B4')).toBe('#ff69b4')
  for (const bad of ['', null, undefined, 'Random', '#fff', '#12345g', 'toString']) expect(projectColor(bad)).toBe('#666666')
  expect(storedProjectColor('#225EC9')).toBe('Blue')
  expect(storedProjectColor('#ff69b4')).toBe('#ff69b4')
  expect(storedProjectColor('#FF69B480')).toBe('#ff69b4')
  for (const [name, hex] of Object.entries(projectColors)) {
    const text = projectTextColor(name)
    expect(contrastRatio(hex, text), name).toBeGreaterThanOrEqual(contrastRatio(hex, text === '#000000' ? '#ffffff' : '#000000'))
    expect(contrastRatio(hex, text), name).toBeGreaterThanOrEqual(4.5)
  }
  expect(projectTextColor('#ffffff')).toBe('#000000')
  expect(projectTextColor('#ffff00')).toBe('#000000')
  expect(projectTextColor('#ff69b4')).toBe('#000000')
  expect(projectTextColor('#000000')).toBe('#ffffff')
  expect(projectTextColor('#1a1a40')).toBe('#ffffff')
  expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 5)
})

it('saves a custom hex from the picker and keeps a preset stored by name', async () => {
  const wrapper = mountPane(ProjectSettings, { project: { ...projects[1], color: 'Blue', repositories: [], settings: {} } })
  await flushPromises()
  const picker = wrapper.getComponent({ name: 'VColorPicker' })
  expect(picker.props('modelValue')).toBe('#225ec9')
  expect(picker.props('swatches').flat()).toEqual(Object.values(projectColors))
  await wrapper.findAll('button').find(button => button.text() === 'Save project').trigger('click')
  await flushPromises()
  expect(api.updateProject.mock.calls.at(-1)[1].color).toBe('Blue')
  picker.vm.$emit('update:modelValue', '#FF69B4')
  await flushPromises()
  expect(wrapper.get('.project-color .v-chip').text()).toBe('#ff69b4')
  await wrapper.findAll('button').find(button => button.text() === 'Save project').trigger('click')
  await flushPromises()
  expect(api.updateProject.mock.calls.at(-1)[1].color).toBe('#ff69b4')
  picker.vm.$emit('update:modelValue', '#00796b')
  await flushPromises()
  expect(wrapper.get('.project-color .v-chip').text()).toBe('Teal')
  await wrapper.findAll('button').find(button => button.text() === 'Save project').trigger('click')
  await flushPromises()
  expect(api.updateProject.mock.calls.at(-1)[1].color).toBe('Teal')
})

it.each(['toolbar', 'attention card', 'calm card'])('binds a closed drawer to the selected project when reopening from %s', async shortcut => {
  const betaRun = { ...run(projects[1], 'beta-assistant'), orchestrator: true,
    attention: shortcut === 'calm card' ? null : { reason: 'question' } }
  api.fetchState.mockResolvedValue({ runs: [betaRun], attention: betaRun.attention ? [betaRun] : [], totals: {}, relations: [] })
  api.fetchAssistant.mockImplementation(id => Promise.resolve({ running: true, lifecycle: 'running', session_id: `s-${id}` }))
  const wrapper = mountPane(App)
  await flushPromises()
  await wrapper.get('.project-assistant[aria-label^="Chat with Alpha"]').trigger('click')
  await flushPromises()
  await wrapper.get('[aria-label="close the Ratchet chat"]').trigger('click')
  await chooseProject(wrapper, 'Beta')
  await flushPromises()
  if (shortcut === 'toolbar') await wrapper.get('header button[aria-label^="Ratchet"]').trigger('click')
  else await wrapper.getComponent(RunCard).get('[aria-label="talk to Ratchet"]').trigger('click')
  await flushPromises()
  const pane = wrapper.findAllComponents(AssistantChat).find(pane => pane.get('.uber-toolbar').isVisible())
  expect(pane.props('workspace').id).toBe('beta')
  const composer = pane.getComponent(Chat).get('textarea')
  await composer.setValue('Beta destination check')
  await composer.trigger('keydown', { key: 'Enter', ctrlKey: true })
  await flushPromises()
  expect(api.sendSessionInput).toHaveBeenCalledWith('s-beta', 'Beta destination check', { sanitize: true, workspaceId: 'beta' })
})

it('retains an editable queued draft alongside previous history and transfers it on admission', async () => {
  api.fetchAssistant.mockResolvedValue({ running: false, lifecycle: 'waiting', queue_position: 1, history_session: 'old-beta' })
  api.fetchAssistantHistory.mockImplementation(id => Promise.resolve({ ...page(id), live: id !== 'old-beta',
    messages: id === 'old-beta' ? [{ seq: 0, id: 'history', session: 'old-beta', generation: 1,
      role: 'assistant', kind: 'message', source: { channel: 'agent' }, text: 'Previous Beta history' }] : [] }))
  const wrapper = mountPane(AssistantChat, { workspace: projects[1] })
  await flushPromises()
  expect(wrapper.getComponent(Chat).props('sessionId')).toBe('old-beta')
  await wrapper.get('textarea').setValue('Draft while capacity is full')
  expect(api.sendSessionInput).not.toHaveBeenCalled()
  api.stopAssistant.mockResolvedValue({ running: false, lifecycle: 'stopped', history_session: 'old-beta' })
  await wrapper.findAll('button').find(button => button.text() === 'Cancel start').trigger('click')
  await flushPromises()
  expect(wrapper.get('textarea').element.value).toBe('Draft while capacity is full')
  api.startAssistant.mockResolvedValue({ running: true, lifecycle: 'running', session_id: 'new-beta', history_session: 'old-beta' })
  await wrapper.findAll('button').find(button => button.text() === 'Start').trigger('click')
  await flushPromises()
  const liveChat = wrapper.getComponent(Chat)
  expect(liveChat.props('sessionId')).toBe('new-beta')
  expect(liveChat.get('textarea').element.value).toBe('Draft while capacity is full')
  await liveChat.get('textarea').trigger('keydown', { key: 'Enter', ctrlKey: true })
  await flushPromises()
  expect(api.sendSessionInput).toHaveBeenCalledWith('new-beta', 'Draft while capacity is full', { sanitize: true, workspaceId: 'beta' })
})

it('keeps the live Chat instance and draft through a running generation change', async () => {
  api.fetchAssistant.mockResolvedValue({ running: true, lifecycle: 'running', session_id: 'first-beta' })
  const wrapper = mountPane(AssistantChat, { workspace: projects[1] })
  await flushPromises()
  const original = wrapper.getComponent(Chat).vm.$.uid
  const editor = wrapper.getComponent(Chat).get('textarea').element
  await wrapper.getComponent(Chat).get('textarea').setValue('Draft across rotation')
  api.rotateAssistant.mockResolvedValue({ running: true, lifecycle: 'running', session_id: 'second-beta' })
  await wrapper.get('[aria-label="restart Ratchet"]').trigger('click')
  await flushPromises()
  const confirm = [...document.querySelectorAll('.v-dialog button')].find(button => button.textContent.trim() === 'restart')
  confirm.click()
  await flushPromises()
  expect(api.rotateAssistant).toHaveBeenCalledWith('beta')
  expect(wrapper.getComponent(Chat).props('sessionId')).toBe('second-beta')
  expect(wrapper.getComponent(Chat).vm.$.uid).toBe(original)
  expect(wrapper.getComponent(Chat).get('textarea').element).toBe(editor)
  expect(wrapper.getComponent(Chat).get('textarea').element.value).toBe('Draft across rotation')
})

it('a delayed send stays in its originating conversation while another project composes', async () => {
  const delayed = deferred()
  api.sendSessionInput.mockReturnValueOnce(delayed.promise)
  const alpha = mountPane(Chat, { sessionId: 's-alpha', workspaceId: 'alpha', conversationKey: 'test-alpha' })
  const beta = mountPane(Chat, { sessionId: 's-beta', workspaceId: 'beta', conversationKey: 'test-beta' })
  await flushPromises()
  await alpha.get('textarea').setValue('Alpha pending send')
  await alpha.get('textarea').trigger('keydown', { key: 'Enter', ctrlKey: true })
  await flushPromises()
  await beta.get('textarea').setValue('Beta unfinished draft')
  expect(api.sendSessionInput).toHaveBeenCalledWith('s-alpha', 'Alpha pending send', { sanitize: true, workspaceId: 'alpha' })
  delayed.resolve({})
  await flushPromises()
  expect(alpha.text()).toContain('Alpha pending send')
  expect(beta.text()).not.toContain('Alpha pending send')
  expect(beta.get('textarea').element.value).toBe('Beta unfinished draft')
  beta.unmount()
  const reopened = mountPane(Chat, { sessionId: 's-beta', workspaceId: 'beta', conversationKey: 'test-beta' })
  await flushPromises()
  expect(reopened.get('textarea').element.value).toBe('Beta unfinished draft')
})


it('retains URL destinations during delayed initial project loading and rejects stale links without fallback', async () => {
  for (const identity of ['beta', 'removed-project']) {
    window.history.replaceState({}, '', `/?workspace=${identity}`)
    const pending = deferred()
    api.fetchProjects.mockReturnValueOnce(pending.promise)
    const wrapper = mountPane(App)
    await flushPromises()
    expect(window.location.search).toBe(`?workspace=${identity}`)
    pending.resolve({ projects })
    await flushPromises()
    expect(window.location.search).toBe(`?workspace=${identity}`)
    if (identity === 'beta') expect(wrapper.get('.project-switcher').text()).toContain('Beta')
    else expect(wrapper.text()).toContain('This project link is unavailable or archived')
    wrapper.unmount()
  }
})


it('keeps uncertain actions visible in platform settings after the source reply completes', async () => {
  api.fetchPlatformSettings.mockResolvedValue({ max_live_assistants: 4, max_concurrent_sessions: 8 })
  api.fetchConversationRequests.mockResolvedValue({ requests: [{ id: 'request', transport: 'slack', state: 'replied',
    actions: [{ method: 'POST', path: '/api/project-relay', state: 'uncertain', error: 'Lost result after acceptance' }] }] })
  const wrapper = mountPane(PlatformSettings, { state: {} }, { global: {
    stubs: { AssistantSettings: true, IngressStatus: true, SlotEditor: true },
  } })
  await flushPromises()
  expect(wrapper.text()).toContain('Conversation requests needing attention')
  expect(wrapper.text()).toContain('slack: replied')
  expect(wrapper.text()).toContain('POST /api/project-relay: uncertain')
  expect(wrapper.text()).toContain('Lost result after acceptance')
})


it('retains staged files and the composer through the false-running rotation poll', async () => {
  api.fetchAssistant.mockResolvedValue({ running: true, lifecycle: 'running', session_id: 'first-beta' })
  const wrapper = mountPane(AssistantChat, { workspace: projects[1] })
  await flushPromises()
  const chat = wrapper.getComponent(Chat)
  const original = chat.vm.$.uid
  const file = new File(['retain attachment bytes'], 'rotation.txt', { type: 'text/plain' })
  Object.defineProperty(chat.get('input[type="file"]').element, 'files', { value: [file], configurable: true })
  await chat.get('input[type="file"]').trigger('change')
  await chat.get('textarea').setValue('Draft across the rotation gap')
  expect(chat.text()).toContain('rotation.txt')
  api.fetchAssistant.mockResolvedValue({ running: false, lifecycle: 'rotating', session_id: null, history_session: 'first-beta' })
  await wrapper.vm.$.setupState.load()
  await flushPromises()
  expect(wrapper.getComponent(Chat).vm.$.uid).toBe(original)
  expect(wrapper.getComponent(Chat).text()).toContain('rotation.txt')
  expect(wrapper.getComponent(Chat).get('textarea').element.value).toBe('Draft across the rotation gap')
  await wrapper.getComponent(Chat).get('textarea').trigger('keydown', { key: 'Enter', ctrlKey: true })
  expect(api.uploadSessionFile).not.toHaveBeenCalled()
  expect(api.sendSessionInput).not.toHaveBeenCalled()
  api.fetchAssistant.mockResolvedValue({ running: true, lifecycle: 'running', session_id: 'second-beta', history_session: 'first-beta' })
  await wrapper.vm.$.setupState.load()
  await flushPromises()
  expect(wrapper.getComponent(Chat).vm.$.uid).toBe(original)
  expect(wrapper.getComponent(Chat).text()).toContain('rotation.txt')
  api.uploadSessionFile.mockResolvedValue({ reference: '[lmer upload] /fixture/rotation.txt' })
  await wrapper.getComponent(Chat).get('textarea').trigger('keydown', { key: 'Enter', ctrlKey: true })
  await flushPromises()
  await vi.waitFor(() => expect(api.sendSessionInput).toHaveBeenCalledTimes(1))
  expect(api.uploadSessionFile).toHaveBeenCalledWith('second-beta', { name: 'rotation.txt', data: btoa('retain attachment bytes'), workspaceId: 'beta' })
  expect(api.sendSessionInput.mock.calls[0][0]).toBe('second-beta')
  expect(api.sendSessionInput.mock.calls[0][1]).toContain('Draft across the rotation gap')
  expect(api.sendSessionInput.mock.calls[0][1]).toContain('/fixture/rotation.txt')
})


it('parks uncertain replies only after an explicit reason and retains their uncertainty', async () => {
  api.fetchPlatformSettings.mockResolvedValue({ max_live_assistants: 4, max_concurrent_sessions: 8 })
  api.fetchConversationRequests.mockResolvedValue({ requests: [{ id: 'uncertain', project_id: 'beta', transport: 'matrix',
    state: 'reply_uncertain', error: 'Response lost', conversation: '!source:example.org', root: '$root' }] })
  const parking = { by: 'operator', at: '2026-09-24T19:00:00Z', reason: 'Continue independent Beta work' }
  api.parkConversationRequest.mockResolvedValue({ parking })
  const wrapper = mountPane(PlatformSettings, { state: {}, projects }, { global: {
    stubs: { AssistantSettings: true, IngressStatus: true, SlotEditor: true },
  } })
  await flushPromises()
  expect(api.parkConversationRequest).not.toHaveBeenCalled()
  await wrapper.findAll('button').find(button => button.text() === 'Park uncertain reply').trigger('click')
  await flushPromises()
  const dialog = document.querySelector('.v-dialog')
  expect(dialog.textContent).toContain('Beta')
  expect(dialog.textContent).toContain('!source:example.org')
  expect(dialog.textContent).toContain('will not be resent or marked delivered')
  const confirm = [...dialog.querySelectorAll('button')].find(button => button.textContent.includes('Park and allow'))
  expect(confirm.disabled).toBe(true)
  const reason = dialog.querySelector('textarea')
  reason.value = parking.reason
  reason.dispatchEvent(new Event('input', { bubbles: true }))
  await flushPromises()
  confirm.click()
  await flushPromises()
  expect(api.parkConversationRequest).toHaveBeenCalledWith('uncertain', 'beta', parking.reason)
  expect(wrapper.text()).toContain('matrix: reply_uncertain')
  expect(wrapper.text()).toContain('Response lost')
  expect(wrapper.text()).toContain('Parked by operator')
  expect(wrapper.text()).toContain('Delivery remains unknown; archive stays blocked.')
})


it('selects only the intended project assistant when both assistants have the same run identity', async () => {
  const assistants = projects.map(project => ({ ...run(project, 'orchestrate-fleet'),
    orchestrator: true, state: 'running', live: true, attention: null, session: { id: `s-${project.id}` } }))
  api.fetchState.mockResolvedValue({ runs: assistants, attention: [], totals: {}, relations: [] })
  const wrapper = mountPane(App, {}, { global: { stubs: { RunDetail: true } } })
  await flushPromises()
  const groups = wrapper.findAllComponents(ProjectNav)
  for (const [index, project] of projects.entries()) {
    await groups[index].get('.run-row').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.project-nav [aria-current="true"]')).toHaveLength(1)
    expect(groups[index].get('.run-row').attributes('aria-current')).toBe('true')
    expect(groups[1 - index].get('.run-row').attributes('aria-current')).toBeUndefined()
    expect(wrapper.getComponent(RunDetail).props('run').workspace_id).toBe(project.id)
    expect(wrapper.getComponent(RunDetail).props('identifiedWorkspaceId')).toBe(project.id)
    expect(window.location.search).toBe(`?workspace=${project.id}`)
  }
  assistants[1] = { ...assistants[1], session: { id: 's-beta-rotated' } }
  api.fetchState.mockResolvedValue({ runs: [...assistants], attention: [], totals: {}, relations: [] })
  window.dispatchEvent(new Event('focus'))
  await flushPromises()
  expect(groups[1].get('.run-row').attributes('aria-current')).toBe('true')
  expect(groups[0].get('.run-row').attributes('aria-current')).toBeUndefined()
  expect(wrapper.getComponent(RunDetail).props('run').session.id).toBe('s-beta-rotated')
})

it.each(['attention', 'ordinary', 'mixed'])('renders %s rows with icons and a mixed-group divider, without subheaders', async kind => {
  const urgent = run(projects[0], 'question')
  const ordinary = { ...run(projects[0], 'working'), attention: null }
  const rows = kind === 'attention' ? [urgent] : kind === 'ordinary' ? [ordinary] : [ordinary, urgent]
  const wrapper = mountPane(RunNav, { runs: rows, attention: kind === 'ordinary' ? [] : [urgent] })
  expect(wrapper.find('details, summary').exists()).toBe(false)
  expect(wrapper.find('.section-title').exists()).toBe(false)
  expect(wrapper.text()).not.toContain('needs you')
  expect(wrapper.findAll('.v-list-item__prepend .v-icon')).toHaveLength(rows.length)
  expect(wrapper.findAll('.run-attention-divider')).toHaveLength(kind === 'mixed' ? 1 : 0)
  expect(wrapper.findAll('.run-row .v-list-item-title').map(row => row.text())).toEqual(
    kind === 'mixed' ? ['question', 'working'] : [rows[0].slug],
  )
  await wrapper.get('.run-row').trigger('click')
  expect(wrapper.emitted('open')[0][0]).toEqual(kind === 'ordinary' ? ordinary : urgent)
})

it.each(['alpha', 'beta', null])('shows run identity only when the toolbar does not identify its project (%s)', identifiedWorkspaceId => {
  const wrapper = mountPane(RunDetail, { run: run(projects[0], 'run'), identifiedWorkspaceId }, {
    global: { stubs: { Chat: true, AskChannel: true, RelatedRuns: true, RunMeta: true, ThreadLinks: true } },
  })
  expect(wrapper.find('.run-project-identity').exists()).toBe(identifiedWorkspaceId !== 'alpha')
  if (identifiedWorkspaceId !== 'alpha') expect(wrapper.get('.run-project-identity').text()).toBe(projects[0].name)
})

it('shows only this project\'s mention channels and saves them only when edited', async () => {
  api.fetchChannelRoutes.mockResolvedValue({ routes: [
    { project_id: 'alpha', label: '#i-alpha', archived: false }, { project_id: 'beta', label: 'C0BETA123', archived: false },
    { project_id: 'alpha', label: '!room:example.org', archived: false }] })
  const wrapper = mountPane(ProjectSettings, { project: projects[0] })
  await flushPromises()
  const field = wrapper.get('.mention-channels textarea')
  expect(field.element.value).toBe('#i-alpha\n!room:example.org')
  const save = wrapper.findAll('button').find(button => button.text() === 'Save project')
  await save.trigger('click')
  await flushPromises()
  expect(api.setProjectChannels).not.toHaveBeenCalled()
  await field.setValue(' #i-alpha \n\nhttps://example.slack.com/archives/C0NEW1234\n')
  api.setProjectChannels.mockRejectedValueOnce(new Error('C0NEW1234 is already mapped to project Beta; remove it there first.'))
  await save.trigger('click')
  await flushPromises()
  expect(api.setProjectChannels).toHaveBeenLastCalledWith('alpha', ['#i-alpha', 'https://example.slack.com/archives/C0NEW1234'])
  expect(wrapper.text()).toContain('already mapped to project Beta')
  expect(field.element.value).toContain('C0NEW1234')
  await field.setValue('')
  await save.trigger('click')
  await flushPromises()
  expect(api.setProjectChannels).toHaveBeenLastCalledWith('alpha', [])
  expect(field.element.value).toBe('')
})

it('does not offer the mention channels for editing before they load', async () => {
  const pending = deferred()
  api.fetchChannelRoutes.mockReturnValueOnce(pending.promise)
  const wrapper = mountPane(ProjectSettings, { project: projects[0] })
  await flushPromises()
  expect(wrapper.get('.mention-channels textarea').attributes('disabled')).toBeDefined()
  await wrapper.findAll('button').find(button => button.text() === 'Save project').trigger('click')
  await flushPromises()
  expect(api.setProjectChannels).not.toHaveBeenCalled()
  pending.resolve({ routes: [{ project_id: 'alpha', label: 'C0ALPHA12' }] })
  await flushPromises()
  expect(wrapper.get('.mention-channels textarea').element.value).toBe('C0ALPHA12')
})

it('keeps standing instructions collapsed and untouched when only project metadata is saved', async () => {
  api.fetchAssistantInstructions.mockResolvedValue({ instructions: 'Keep these orders.', limit: 4000 })
  const wrapper = mountPane(ProjectSettings, { project: projects[0] })
  await flushPromises()
  expect(wrapper.get('.v-expansion-panel-title').attributes('aria-expanded')).toBe('false')
  await wrapper.get('input').setValue('Example Alpha renamed')
  await wrapper.findAll('button').find(button => button.text() === 'Save project').trigger('click')
  await flushPromises()
  expect(api.setAssistantInstructions).not.toHaveBeenCalled()
  expect(api.updateProject).toHaveBeenCalledWith('alpha', expect.objectContaining({ name: 'Example Alpha renamed' }))
  await wrapper.get('.v-expansion-panel-title').trigger('click')
  await flushPromises()
  expect(wrapper.get('.project-orders textarea').element.value).toBe('Keep these orders.')
})

it('saves the edited standing draft after collapse and retains it after a failed save', async () => {
  api.fetchAssistantInstructions.mockResolvedValue({ instructions: 'Original instructions', limit: 4000 })
  const wrapper = mountPane(ProjectSettings, { project: projects[1] })
  await flushPromises()
  await wrapper.get('.v-expansion-panel-title').trigger('click')
  await flushPromises()
  await wrapper.get('.project-orders textarea').setValue('Edited Beta instructions')
  await wrapper.get('.v-expansion-panel-title').trigger('click')
  api.setAssistantInstructions.mockRejectedValueOnce(new Error('Save refused'))
  const save = wrapper.findAll('button').find(button => button.text() === 'Save project')
  await save.trigger('click')
  await flushPromises()
  expect(api.updateProject).not.toHaveBeenCalled()
  expect(wrapper.text()).toContain('Save refused')
  await wrapper.get('.v-expansion-panel-title').trigger('click')
  await flushPromises()
  expect(wrapper.get('.project-orders textarea').element.value).toBe('Edited Beta instructions')
  await save.trigger('click')
  await flushPromises()
  expect(api.setAssistantInstructions).toHaveBeenLastCalledWith('beta', 'Edited Beta instructions')
  expect(api.updateProject).toHaveBeenCalledWith('beta', expect.any(Object))
})

it('does not write hidden instructions before loading, and ignores a previous project response', async () => {
  const pending = deferred()
  api.fetchAssistantInstructions.mockReturnValueOnce(pending.promise)
  const wrapper = mountPane(ProjectSettings, { project: projects[0] })
  await wrapper.findAll('button').find(button => button.text() === 'Save project').trigger('click')
  await flushPromises()
  expect(api.setAssistantInstructions).not.toHaveBeenCalled()
  api.fetchAssistantInstructions.mockResolvedValue({ instructions: 'Beta orders', limit: 4000 })
  await wrapper.setProps({ project: projects[1] })
  await flushPromises()
  pending.resolve({ instructions: 'Late Alpha orders', limit: 4000 })
  await flushPromises()
  expect(wrapper.get('.v-expansion-panel-title').attributes('aria-expanded')).toBe('false')
  await wrapper.get('.v-expansion-panel-title').trigger('click')
  await flushPromises()
  expect(wrapper.get('.project-orders textarea').element.value).toBe('Beta orders')
})

it('keeps numeric capacity values and restores the intended colored archived project', async () => {
  const archived = { id: 'archived', name: 'Example Archive', color: 'Purple', archived: true }
  const wrapper = mountPane(PlatformSettings, { projects: [...projects, archived] }, {
    global: { stubs: { AssistantSettings: true, IngressStatus: true, SlotEditor: true } },
  })
  await flushPromises()
  const fields = wrapper.findAll('.capacity-limits input')
  expect(fields).toHaveLength(2)
  for (const field of fields) {
    expect(field.attributes('type')).toBe('number')
    expect(field.attributes('min')).toBe('1')
    expect(field.attributes('step')).toBe('1')
    expect(wrapper.get(`label[for="${field.attributes('id')}"]`).text()).toBeTruthy()
  }
  await fields[0].setValue('12')
  await fields[1].setValue('5')
  await wrapper.findAll('button').find(button => button.text() === 'Save limit').trigger('click')
  await flushPromises()
  expect(api.setPlatformSettings).toHaveBeenCalledWith({ max_concurrent_sessions: 12, max_live_assistants: 5 })
  const row = wrapper.get('.archived-project')
  expect(row.attributes('style')).toContain(projectColor('Purple'))
  expect(row.getComponent({ name: 'VChip' }).props('color')).toBe(projectColor('Purple'))
  expect(row.getComponent({ name: 'VBtn' }).props('color')).toBe(projectColor('Purple'))
  expect(row.getComponent({ name: 'VChip' }).element.style.color).toBe('rgb(255, 255, 255)')
  expect(row.getComponent({ name: 'VBtn' }).element.style.color).toBe('rgb(255, 255, 255)')
  await row.get('button').trigger('click')
  expect(api.archiveProject).toHaveBeenCalledWith('archived', true)
})

it('gives every rendered global-header action an accessible tooltip association', async () => {
  const wrapper = mountPane(App, {}, { global: { stubs: { PlatformSettings: true } } })
  await flushPromises()
  for (const button of wrapper.findAll('header button')) {
    expect(button.attributes('aria-label')).toBeTruthy()
    expect(button.attributes('aria-describedby')).toBeTruthy()
  }
  await wrapper.get('header button[aria-label="Platform settings"]').trigger('click')
  await flushPromises()
  expect(wrapper.get('header button[aria-label="back to the fleet"]').attributes('aria-describedby')).toBeTruthy()
})


it('posts project instructions as JSON to the explicitly scoped HTTP endpoint', async () => {
  const realApi = await vi.importActual('../src/api.js')
  const request = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ instructions: 'Saved orders' }) })
  vi.stubGlobal('fetch', request)
  try {
    await realApi.setAssistantInstructions('example beta', 'Saved orders')
    expect(request).toHaveBeenCalledWith('api/assistant/instructions?workspace_id=example%20beta', {
      credentials: 'same-origin', method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instructions: 'Saved orders' }),
    })
  } finally { vi.unstubAllGlobals() }
})

it('badges Ratchet\'s open questions from its status poll and asks the shell to open them', async () => {
  vi.useFakeTimers()
  const answers = [2, 1, 0]
  api.fetchAssistant.mockImplementation(() => Promise.resolve({ running: true, lifecycle: 'running',
    session_id: 's-alpha', generation: 3, open_questions: answers.length > 1 ? answers.shift() : answers[0] }))
  const wrapper = mountPane(AssistantChat, { workspace: projects[0] })
  await flushPromises()
  const badge = () => wrapper.find('.open-questions')
  expect(badge().exists()).toBe(true)
  expect(badge().text()).toContain('2')
  expect(badge().attributes('aria-label')).toBe('2 open questions from Ratchet — answer in its run view')
  await badge().trigger('click')
  expect(wrapper.emitted('questions')).toEqual([['s-alpha']])
  await vi.advanceTimersByTimeAsync(10000)
  expect(badge().text()).toContain('1')
  expect(badge().attributes('aria-label')).toContain('1 open question from')
  await vi.advanceTimersByTimeAsync(10000)
  expect(badge().exists()).toBe(false)
  wrapper.unmount()
  vi.useRealTimers()
})

it('keeps the question badge inside the composer, its corner kept when it clears, the field unfocused', async () => {
  vi.useFakeTimers()
  const answers = [1, 0]
  api.fetchAssistant.mockImplementation(() => Promise.resolve({ running: true, lifecycle: 'running',
    session_id: 's-alpha', generation: 3, open_questions: answers.length > 1 ? answers.shift() : answers[0] }))
  const wrapper = mountPane(AssistantChat, { workspace: projects[0] })
  await flushPromises()
  const slot = () => wrapper.getComponent(Chat).find('.v-field__append-inner .question-slot')
  expect(slot().find('.open-questions').exists()).toBe(true)
  await slot().get('.open-questions').trigger('mousedown')
  await slot().get('.open-questions').trigger('click')
  expect(wrapper.emitted('questions')).toEqual([['s-alpha']])
  expect(document.activeElement).not.toBe(wrapper.getComponent(Chat).get('textarea').element)
  await vi.advanceTimersByTimeAsync(10000)
  expect(slot().exists()).toBe(true)
  expect(slot().find('.open-questions').exists()).toBe(false)
  wrapper.unmount()
  vi.useRealTimers()
})

it('puts every Ratchet control on one toolbar, with no overflow menu and no intro lines', async () => {
  api.fetchAssistant.mockResolvedValue({ running: true, lifecycle: 'running', session_id: 's-alpha', generation: 3 })
  const wrapper = mountPane(AssistantChat, { workspace: projects[0] })
  await flushPromises()
  const toolbar = wrapper.get('.uber-toolbar')
  for (const label of ['restart Ratchet', 'stop Ratchet', 'project settings', 'standing orders', 'close the Ratchet chat']) {
    expect(toolbar.find(`[aria-label="${label}"]`).exists()).toBe(true)
  }
  expect(wrapper.find('[aria-label="more Ratchet controls"]').exists()).toBe(false)
  expect(wrapper.text()).not.toContain('You are talking to')
  expect(wrapper.text()).not.toContain('belong to this project')
  await toolbar.get('[aria-label="project settings"]').trigger('click')
  await toolbar.get('[aria-label="standing orders"]').trigger('click')
  expect(wrapper.emitted('settings')).toEqual([[], ['orders']])
  await toolbar.get('[aria-label="stop Ratchet"]').trigger('click')
  await flushPromises()
  expect(document.querySelector('.v-dialog')?.textContent).toContain('Stop Ratchet?')
  wrapper.unmount()
})

it('counts loaded messages and steps back to earlier ones from the toolbar, not the card', async () => {
  api.fetchAssistant.mockResolvedValue({ running: true, lifecycle: 'running', session_id: 's-alpha', generation: 3 })
  const said = (seq) => ({ seq, id: `m${seq}`, session: 's-alpha', generation: 3, role: 'assistant',
    kind: 'message', source: { channel: 'agent' }, text: `turn ${seq}` })
  const rows = (from, to) => Array.from({ length: to - from }, (_, i) => said(from + i))
  api.fetchAssistantHistory.mockImplementation((id, request = {}) => Promise.resolve(request.before
    ? { ...page(id), messages: rows(20, 60), from: 20, cursor: 60, total: 100 }
    : { ...page(id), messages: rows(60, 100), from: 60, cursor: 100, total: 100 }))
  const wrapper = mountPane(AssistantChat, { workspace: projects[0] })
  await flushPromises()
  const toolbar = wrapper.get('.uber-toolbar')
  expect(toolbar.get('.loaded-count').text()).toBe('40 messages loaded')
  expect(wrapper.getComponent(Chat).text()).not.toContain('messages loaded')
  expect(wrapper.getComponent(Chat).text()).not.toContain('earlier messages')
  await toolbar.get('[aria-label="earlier messages"]').trigger('click')
  await flushPromises()
  expect(api.fetchAssistantHistory).toHaveBeenCalledWith('s-alpha', expect.objectContaining({ before: 'm60' }))
  expect(wrapper.get('.loaded-count').text()).toBe('80 messages loaded')
  wrapper.unmount()
})

it('opens project settings on the standing instructions from Ratchet\'s toolbar', async () => {
  api.fetchAssistant.mockImplementation(id => Promise.resolve({ running: true, lifecycle: 'running', session_id: `s-${id}` }))
  api.fetchAssistantInstructions.mockResolvedValue({ instructions: 'Always rebase', limit: 4000 })
  const wrapper = mountPane(App)
  await flushPromises()
  await wrapper.get('.project-assistant[aria-label^="Chat with Alpha"]').trigger('click')
  await flushPromises()
  const pane = wrapper.findAllComponents(AssistantChat).find(candidate => candidate.get('.uber-toolbar').isVisible())
  await pane.get('[aria-label="standing orders"]').trigger('click')
  await flushPromises()
  const settings = wrapper.getComponent(ProjectSettings)
  expect(settings.props('project').id).toBe('alpha')
  expect(settings.get('.project-orders .v-expansion-panel').classes()).toContain('v-expansion-panel--active')
  await pane.get('[aria-label="project settings"]').trigger('click')
  await flushPromises()
  expect(wrapper.getComponent(ProjectSettings).get('.project-orders .v-expansion-panel').classes())
    .toContain('v-expansion-panel--active')
  wrapper.unmount()
})

it('moves the question badge to the toolbar while there is no composer to hold it', async () => {
  api.fetchAssistant.mockResolvedValue({ running: true, lifecycle: 'running', session_id: 's-alpha', generation: 3, open_questions: 2 })
  api.fetchAssistantHistory.mockRejectedValue(new Error('transcript unreadable'))
  const wrapper = mountPane(AssistantChat, { workspace: projects[0] })
  await flushPromises()
  expect(wrapper.getComponent(Chat).find('textarea').exists()).toBe(false)
  expect(wrapper.findAll('.open-questions')).toHaveLength(1)
  const badge = wrapper.get('.uber-toolbar .open-questions')
  expect(badge.text()).toContain('2')
  await badge.trigger('click')
  expect(wrapper.emitted('questions')).toEqual([['s-alpha']])
  wrapper.unmount()
})

it('re-reads the standing orders on every tap and keeps an unsaved edit', async () => {
  api.fetchAssistant.mockImplementation(id => Promise.resolve({ running: true, lifecycle: 'running', session_id: `s-${id}` }))
  api.fetchAssistantInstructions.mockResolvedValue({ instructions: 'Always rebase', limit: 4000 })
  const wrapper = mountPane(App)
  await flushPromises()
  await wrapper.get('.project-assistant[aria-label^="Chat with Alpha"]').trigger('click')
  await flushPromises()
  const pane = wrapper.findAllComponents(AssistantChat).find(candidate => candidate.get('.uber-toolbar').isVisible())
  const orders = () => wrapper.getComponent(ProjectSettings).get('.project-orders textarea')
  await pane.get('[aria-label="standing orders"]').trigger('click')
  await flushPromises()
  expect(orders().element.value).toBe('Always rebase')
  api.fetchAssistantInstructions.mockResolvedValue({ instructions: 'Always rebase, never squash', limit: 4000 })
  await pane.get('[aria-label="standing orders"]').trigger('click')
  await flushPromises()
  expect(orders().element.value).toBe('Always rebase, never squash')
  await orders().setValue('My own wording')
  api.fetchAssistantInstructions.mockResolvedValue({ instructions: 'Newest from the chat', limit: 4000 })
  await pane.get('[aria-label="standing orders"]').trigger('click')
  await flushPromises()
  expect(orders().element.value).toBe('My own wording')
  wrapper.unmount()
})

it('opens only the tapped project\'s standing orders, not the next project\'s settings', async () => {
  api.fetchAssistant.mockImplementation(id => Promise.resolve({ running: true, lifecycle: 'running', session_id: `s-${id}` }))
  const wrapper = mountPane(App)
  await flushPromises()
  await wrapper.get('.project-assistant[aria-label^="Chat with Alpha"]').trigger('click')
  await flushPromises()
  const alpha = wrapper.findAllComponents(AssistantChat).find(candidate => candidate.get('.uber-toolbar').isVisible())
  await alpha.get('[aria-label="standing orders"]').trigger('click')
  await flushPromises()
  await wrapper.get('.project-assistant[aria-label^="Chat with Beta"]').trigger('click')
  await flushPromises()
  const settings = wrapper.getComponent(ProjectSettings)
  expect(settings.props('project').id).toBe('beta')
  expect(settings.get('.project-orders .v-expansion-panel').classes()).not.toContain('v-expansion-panel--active')
  await wrapper.get('.project-assistant[aria-label^="Chat with Alpha"]').trigger('click')
  await flushPromises()
  const back = wrapper.getComponent(ProjectSettings)
  expect(back.props('project').id).toBe('alpha')
  expect(back.get('.project-orders .v-expansion-panel').classes()).not.toContain('v-expansion-panel--active')
  wrapper.unmount()
})

const openAlphaPane = async () => {
  api.fetchAssistant.mockImplementation(id => Promise.resolve({ running: true, lifecycle: 'running', session_id: `s-${id}` }))
  const wrapper = mountPane(App)
  await flushPromises()
  await wrapper.get('.project-assistant[aria-label^="Chat with Alpha"]').trigger('click')
  await flushPromises()
  const pane = wrapper.findAllComponents(AssistantChat).find(candidate => candidate.get('.uber-toolbar').isVisible())
  const tap = async (label = 'standing orders') => { await pane.get(`[aria-label="${label}"]`).trigger('click'); await flushPromises() }
  const orders = () => wrapper.getComponent(ProjectSettings).get('.project-orders textarea')
  return { wrapper, tap, orders }
}

it('re-reads the standing orders on the first tap after project settings was opened another way', async () => {
  api.fetchAssistantInstructions.mockResolvedValue({ instructions: 'Always rebase', limit: 4000 })
  const { wrapper, tap, orders } = await openAlphaPane()
  await tap('project settings')
  api.fetchAssistantInstructions.mockResolvedValue({ instructions: 'Rewritten from the chat', limit: 4000 })
  await tap()
  expect(orders().element.value).toBe('Rewritten from the chat')
  wrapper.unmount()
})

it('lands only the newest read or save of the standing orders', async () => {
  api.fetchAssistantInstructions.mockResolvedValue({ instructions: 'Always rebase', limit: 4000 })
  const { wrapper, tap, orders } = await openAlphaPane()
  await tap()
  const [first, second, third] = [deferred(), deferred(), deferred()]
  api.fetchAssistantInstructions.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
  await tap()
  await tap()
  second.resolve({ instructions: 'Newer', limit: 4000 })
  await flushPromises()
  first.resolve({ instructions: 'Older', limit: 4000 })
  await flushPromises()
  expect(orders().element.value).toBe('Newer')
  api.fetchAssistantInstructions.mockReturnValueOnce(third.promise)
  await tap()
  await orders().setValue('My own wording')
  await wrapper.getComponent(ProjectSettings).findAll('button').find(button => button.text() === 'Save project').trigger('click')
  await flushPromises()
  expect(api.setAssistantInstructions).toHaveBeenLastCalledWith('alpha', 'My own wording')
  third.resolve({ instructions: 'Older', limit: 4000 })
  await flushPromises()
  expect(orders().element.value).toBe('My own wording')
  wrapper.unmount()
})

it('re-reads standing orders tapped during a save only after the save has landed', async () => {
  let stored = 'Always rebase'
  api.fetchAssistantInstructions.mockImplementation(() => Promise.resolve({ instructions: stored, limit: 4000 }))
  const { wrapper, tap, orders } = await openAlphaPane()
  await tap()
  const [get, put] = [deferred(), deferred()]
  api.fetchAssistantInstructions.mockImplementation(() => {
    const served = stored
    return get.promise.then(() => ({ instructions: served, limit: 4000 }))
  })
  api.setAssistantInstructions.mockImplementation((_id, text) => put.promise.then(() => {
    stored = text
    return { instructions: text, limit: 4000 }
  }))
  await orders().setValue('My own wording')
  const save = () => wrapper.getComponent(ProjectSettings).findAll('button').find(button => button.text() === 'Save project').trigger('click')
  await save()
  await tap()
  get.resolve()
  await flushPromises()
  put.resolve()
  await flushPromises()
  expect(orders().element.value).toBe('My own wording')
  stored = 'Newer from the chat'
  await save()
  await flushPromises()
  expect(api.setAssistantInstructions).toHaveBeenCalledTimes(1)
  expect(stored).toBe('Newer from the chat')
  wrapper.unmount()
})

it('clears a failed read of the standing orders once a re-read succeeds', async () => {
  const { wrapper, tap } = await openAlphaPane()
  api.fetchAssistantInstructions.mockRejectedValueOnce(new Error('orders unreadable'))
  await tap()
  expect(wrapper.getComponent(ProjectSettings).text()).toContain('orders unreadable')
  await tap()
  expect(wrapper.getComponent(ProjectSettings).text()).not.toContain('orders unreadable')
  wrapper.unmount()
})

// Issue 578: the toolbar is a neutral theme surface, not the project colour.
it('paints Ratchet\'s toolbar with the neutral theme colour, never the project colour', async () => {
  api.fetchAssistant.mockResolvedValue({ running: true, lifecycle: 'running', session_id: 's-alpha', generation: 3 })
  const wrapper = mountPane(AssistantChat, { workspace: { ...projects[0], color: 'Purple' } })
  await flushPromises()
  const toolbar = wrapper.get('.uber-toolbar')
  expect(toolbar.classes()).toContain('bg-uber-toolbar')
  expect(toolbar.attributes('style') || '').not.toMatch(/background|color/)
  expect(toolbar.html()).not.toContain(projectColor('Purple'))
  wrapper.unmount()
})

it('names the generation and its age in the robot\'s tooltip, not in the pane body', async () => {
  const now = Date.parse('2026-10-11T12:00:00Z')
  api.fetchAssistant.mockResolvedValue({ running: true, lifecycle: 'running', session_id: 's-alpha',
    generation: 22, started_at: '2026-10-11T04:00:00Z' })
  const wrapper = mountPane(AssistantChat, { workspace: projects[0], now })
  await flushPromises()
  const identity = `${projects[0].name} · Ratchet · generation 22 · started 8h ago`
  const robot = wrapper.get('.uber-toolbar .uber-identity')
  expect(robot.attributes()).toMatchObject({ role: 'button', tabindex: '0', 'aria-label': identity })
  expect(wrapper.get('.uber-body').text()).not.toMatch(/generation|started/)
  expect(wrapper.findComponent({ name: 'StatusStrip' }).exists()).toBe(true)
  // The tooltip's text is in the DOM before it opens, so its model is what says it
  // is up. A real tap's mouseenter-then-click is pinned in web/tests/browser/projects.py.
  const tooltip = wrapper.findAllComponents({ name: 'VTooltip' }).find(t => t.props('text') === identity)
  expect(tooltip.props('modelValue')).toBe(false)
  for (const activation of ['first', 'second']) {
    await robot.trigger('click')
    await flushPromises()
    expect(tooltip.props('modelValue'), activation).toBe(true)
  }
  wrapper.unmount()
})

it('opens the robot\'s tooltip from the keyboard with Enter or Space', async () => {
  api.fetchAssistant.mockResolvedValue({ running: true, lifecycle: 'running', session_id: 's-alpha', generation: 3 })
  const identity = `${projects[0].name} · Ratchet · generation 3`
  for (const key of ['enter', 'space']) {
    const wrapper = mountPane(AssistantChat, { workspace: projects[0] })
    await flushPromises()
    const tooltip = wrapper.findAllComponents({ name: 'VTooltip' }).find(t => t.props('text') === identity)
    expect(tooltip.props('modelValue'), key).toBe(false)
    await wrapper.get('.uber-toolbar .uber-identity').trigger(`keydown.${key}`)
    await flushPromises()
    expect(tooltip.props('modelValue'), key).toBe(true)
    wrapper.unmount()
  }
})

it('labels a stopped assistant with its lifecycle, keeping the project name', async () => {
  const wrapper = mountPane(AssistantChat, { workspace: projects[0] })
  await flushPromises()
  expect(wrapper.get('.uber-toolbar .uber-identity').attributes('aria-label')).toBe(`${projects[0].name} · Ratchet · off: stopped`)
  wrapper.unmount()
})

// jsdom has no layout and the suite loads no CSS, so this pins the structure that
// keeps the notices out of the flex column: Vuetify's `.v-alert { flex: 1 1 }`
// only applies to an alert that is itself a flex item of `.uber-body`.
it('keeps the digest and problem notices out of the flex body so they size to their text', async () => {
  api.fetchAssistant.mockResolvedValue({ running: true, lifecycle: 'running', session_id: 's-alpha', generation: 3, pending: 2 })
  const wrapper = mountPane(AssistantChat, { workspace: projects[0] })
  await flushPromises()
  api.fetchAssistant.mockRejectedValue(new Error('daemon unreachable'))
  await wrapper.vm.$.setupState.load()
  await flushPromises()
  const body = wrapper.get('.uber-body').element
  const alerts = [...body.querySelectorAll('.v-alert')]
  expect(alerts.map(alert => alert.textContent)).toEqual([
    expect.stringContaining('daemon unreachable'),
    expect.stringContaining('2 digests waiting for Ratchet'),
  ])
  for (const alert of alerts) {
    expect(alert.parentElement).not.toBe(body)
    expect(alert.parentElement.classList).toContain('uber-notices')
    expect(alert.classList).toContain('v-alert--density-compact')
  }
  expect(wrapper.get('.uber-notices').element.parentElement).toBe(body)
  wrapper.unmount()
})

const settingsWithOrders = async (orders, props = {}) => {
  api.fetchAssistantInstructions.mockResolvedValue({ limit: 4000, ...orders })
  const wrapper = mountPane(ProjectSettings, { project: projects[0], focus: { id: 'alpha', section: 'orders', nonce: 1 }, ...props })
  await flushPromises()
  return wrapper
}

it('says when the standing orders are empty, from the stored document rather than the draft', async () => {
  const wrapper = await settingsWithOrders({ instructions: null, instructions_at: null })
  expect(wrapper.get('.orders-empty').text()).toBe('No standing instructions yet.')
  await wrapper.get('.project-orders textarea').setValue('A draft that is not saved yet')
  expect(wrapper.find('.orders-empty').exists()).toBe(true)
  await wrapper.findAll('button').find(button => button.text() === 'Save project').trigger('click')
  await flushPromises()
  expect(wrapper.find('.orders-empty').exists()).toBe(false)
  wrapper.unmount()
  const stored = await settingsWithOrders({ instructions: 'Always rebase', instructions_at: '2026-10-11T10:00:00Z' })
  expect(stored.find('.orders-empty').exists()).toBe(false)
  stored.unmount()
})

it('hints that standing orders are usually set by telling Ratchet in chat', async () => {
  for (const orders of [{ instructions: null }, { instructions: 'Always rebase' }]) {
    const wrapper = await settingsWithOrders(orders)
    expect(wrapper.get('.orders-hint').text().replace(/\s+/g, ' ')).toBe(
      'Usually set by telling Ratchet in chat — "from now on…", "always…" — and it confirms the wording before storing them.')
    wrapper.unmount()
  }
})

it('shows when the standing orders last changed and refreshes it from a save', async () => {
  const now = Date.parse('2026-10-11T13:00:00Z')
  const wrapper = await settingsWithOrders({ instructions: 'Always rebase', instructions_at: '2026-10-11T10:00:00Z' }, { now })
  const changed = wrapper.get('.orders-changed')
  expect(changed.text()).toBe('last changed 3h ago')
  expect(changed.attributes('title')).toBe('2026-10-11T10:00:00Z')
  await wrapper.setProps({ now: now + 2 * 3600 * 1000 })
  expect(wrapper.get('.orders-changed').text()).toBe('last changed 5h ago')
  api.setAssistantInstructions.mockResolvedValueOnce({ instructions: 'Always rebase, never squash', limit: 4000,
    instructions_at: '2026-10-11T14:59:00Z' })
  await wrapper.get('.project-orders textarea').setValue('Always rebase, never squash')
  await wrapper.findAll('button').find(button => button.text() === 'Save project').trigger('click')
  await flushPromises()
  expect(wrapper.get('.orders-changed').text()).toBe('last changed 1m ago')
  wrapper.unmount()
  const never = await settingsWithOrders({ instructions: null, instructions_at: null })
  expect(never.find('.orders-changed').exists()).toBe(false)
  never.unmount()
})

it('opens Ratchet\'s own run on its question badge and focuses the ask dock there', async () => {
  const orchestrator = { host: '—', project: '—', slug: 's-alpha', label: 'Ratchet', orchestrator: true,
    workspace_id: 'alpha', workspace_name: projects[0].name, workspace_color: 'Blue', live: true,
    last_session_id: 's-alpha', session: { id: 's-alpha' }, state: 'waiting_on_you', attention: { reason: 'live_question' },
    questions: [{ id: 'q1', kind: 'question', text: 'Ship it?', options: [], answered: false, closed: false }] }
  const runs = [...projects.map((project, i) => run(project, `question-${i}`)), orchestrator]
  api.fetchState.mockResolvedValue({ runs, attention: runs, totals: {}, relations: [] })
  const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true, RunDetail: true } } })
  await flushPromises()
  await wrapper.get('.project-assistant[aria-label^="Chat with Alpha"]').trigger('click')
  await flushPromises()
  wrapper.getComponent(AssistantChat).vm.$emit('questions', 's-unknown')
  await flushPromises()
  expect(wrapper.findComponent(RunDetail).exists()).toBe(false)
  expect(document.body.textContent).toContain('not in the fleet view yet')
  wrapper.getComponent(AssistantChat).vm.$emit('questions', 's-alpha')
  await flushPromises()
  const detail = wrapper.getComponent(RunDetail)
  expect(detail.props('run').slug).toBe('s-alpha')
  const first = detail.props('focusQuestions')
  expect(first).toBeGreaterThan(0)
  wrapper.getComponent(AssistantChat).vm.$emit('questions', 's-alpha')
  await flushPromises()
  expect(wrapper.getComponent(RunDetail).props('focusQuestions')).toBeGreaterThan(first)
})

it('sets the supervisor authority, reads unset as none and clears the key for none', async () => {
  const wrapper = mountPane(ProjectSettings, { project: { ...projects[0], repositories: [], settings: {} } })
  await flushPromises()
  const select = wrapper.getComponent('.supervisor-authority')
  expect(select.props('modelValue')).toBe('none')
  expect(select.props('label')).toBe('What Ratchet may approve for workers')
  const save = () => wrapper.findAll('button').find(button => button.text() === 'Save project').trigger('click')
  select.vm.$emit('update:modelValue', 'approvals')
  await save()
  await flushPromises()
  expect(api.updateProject.mock.calls.at(-1)[1].settings.supervisor_authority).toBe('approvals')
  select.vm.$emit('update:modelValue', 'none')
  await save()
  await flushPromises()
  expect(api.updateProject.mock.calls.at(-1)[1].settings.supervisor_authority).toBeNull()
})

it('labels project execution overrides readably and says what an empty one inherits', async () => {
  const wrapper = mountPane(ProjectSettings, { project: { ...projects[0], repositories: [], settings: {} } })
  await flushPromises()
  const labels = wrapper.findAll('.v-field-label, label.v-label').map(node => node.text())
  for (const label of ['Model', 'Harness', 'Preset', 'Fan-out presets']) expect(labels).toContain(label)
  for (const raw of ['model', 'harness', 'preset', 'agents']) expect(labels).not.toContain(raw)
  expect(wrapper.text()).toContain('Empty fields inherit the platform defaults.')
})

it('groups project settings into titled sections, each saying what it applies to', async () => {
  const wrapper = mountPane(ProjectSettings, { project: { ...projects[0], repositories: [], settings: {} } })
  await flushPromises()
  const sections = wrapper.findAll('section.settings-group')
  expect(sections.map(section => section.get('h3').text()))
    .toEqual(['Identity', 'This browser', 'Execution', 'Assistant', 'Chat channels', 'Archive'])
  for (const section of sections) {
    expect(section.get('h3').attributes('id')).toBe(section.attributes('aria-labelledby'))
    expect(section.get('p').text().length).toBeGreaterThan(20)
  }
  const topActions = wrapper.get('.project-actions').findAll('button').map(button => button.text())
  expect(topActions).toEqual(['Save project', 'Close'])
  expect(sections.at(-1).findAll('button').map(button => button.text())).toEqual(['Archive project'])
})

it('remembers which projects were collapsed across a reload, keyed by project id', async () => {
  const first = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  const toggle = (wrapper, index) => wrapper.findAllComponents(ProjectNav)[index].get('.project-toggle')
  await toggle(first, 0).trigger('click')
  expect(toggle(first, 0).attributes('aria-expanded')).toBe('false')
  expect(JSON.parse(window.localStorage.getItem('lmer.projects.collapsed'))).toEqual(['alpha'])
  first.unmount()

  const second = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(toggle(second, 0).attributes('aria-expanded')).toBe('false')
  expect(toggle(second, 1).attributes('aria-expanded')).toBe('true')
  await toggle(second, 0).trigger('click')
  expect(JSON.parse(window.localStorage.getItem('lmer.projects.collapsed'))).toEqual([])
})

it('renders every project expanded when the stored collapse list is unusable or storage throws', async () => {
  window.localStorage.setItem('lmer.projects.collapsed', '{"alpha":true}')
  const garbled = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(garbled.findAllComponents(ProjectNav).map(nav => nav.get('.project-toggle').attributes('aria-expanded')))
    .toEqual(['true', 'true'])
  garbled.unmount()

  const refuse = () => { throw new DOMException('denied', 'SecurityError') }
  const failures = vi.spyOn(console, 'error').mockImplementation(() => {})
  const read = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(refuse)
  const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(refuse)
  try {
    const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
    await flushPromises()
    const navs = wrapper.findAllComponents(ProjectNav)
    expect(navs).toHaveLength(2)
    await navs[0].get('.project-toggle').trigger('click')
    expect(navs[0].get('.project-toggle').attributes('aria-expanded')).toBe('false')
    expect(failures).not.toHaveBeenCalled()
  } finally {
    read.mockRestore()
    write.mockRestore()
    failures.mockRestore()
  }
})

const switcherLabel = wrapper => wrapper.get('.project-switcher').text()

it('reopens the project last viewed, and writes it only when the operator moves', async () => {
  const first = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(switcherLabel(first)).toBe('All projects')
  expect(window.localStorage.getItem('lmer.project.last')).toBeNull()
  await chooseProject(first, 'Beta')
  expect(window.localStorage.getItem('lmer.project.last')).toBe('beta')
  first.unmount()

  const second = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(switcherLabel(second)).toBe('Beta')
  expect(window.location.search).toBe('?workspace=beta')
  await chooseProject(second, 'All projects')
  expect(window.localStorage.getItem('lmer.project.last')).toBe('')
  second.unmount()

  const third = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(switcherLabel(third)).toBe('All projects')
})

it('skips a remembered project that is gone or archived, and keeps it stored for when it returns', async () => {
  window.localStorage.setItem('lmer.project.last', 'gone')
  const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(switcherLabel(wrapper)).toBe('All projects')
  expect(window.location.search).toBe('')
  expect(wrapper.find('.v-alert').exists()).toBe(false)
  expect(window.localStorage.getItem('lmer.project.last')).toBe('gone')
  wrapper.unmount()

  api.fetchProjects.mockResolvedValue({ projects: [...projects, { id: 'old', name: 'Old', archived: true }] })
  window.localStorage.setItem('lmer.project.default', 'old')
  window.localStorage.setItem('lmer.project.last', 'beta')
  const fallback = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(switcherLabel(fallback)).toBe('Beta')
  expect(window.localStorage.getItem('lmer.project.default')).toBe('old')
})

it('does not call a remembered project unavailable when the project list cannot be fetched', async () => {
  window.localStorage.setItem('lmer.project.last', 'beta')
  api.fetchProjects.mockRejectedValue(new Error('daemon down'))
  const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(wrapper.text()).not.toContain('This project link is unavailable or archived.')
})

it('records a project opened from a link as the last viewed one, once it is confirmed live', async () => {
  window.localStorage.setItem('lmer.project.last', 'alpha')
  window.history.replaceState({}, '', '/?workspace=beta')
  const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(window.localStorage.getItem('lmer.project.last')).toBe('beta')
  wrapper.unmount()

  window.history.replaceState({}, '', '/?workspace=gone')
  const dead = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(window.localStorage.getItem('lmer.project.last')).toBe('beta')
  dead.unmount()
})

it('lets a link win over the remembered project and shows its own notice for a dead link', async () => {
  window.localStorage.setItem('lmer.project.default', 'alpha')
  window.history.replaceState({}, '', '/?workspace=beta')
  const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(switcherLabel(wrapper)).toBe('Beta')
  wrapper.unmount()

  window.history.replaceState({}, '', '/?workspace=gone')
  const dead = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(switcherLabel(dead)).toBe('Unavailable project')
  expect(dead.text()).toContain('This project link is unavailable or archived.')
})

it('opens the default project on every load, over the last viewed, until it is cleared', async () => {
  window.localStorage.setItem('lmer.project.last', 'beta')
  const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(switcherLabel(wrapper)).toBe('Beta')
  await chooseProject(wrapper, 'Project settings for Beta')
  const flag = () => wrapper.getComponent(ProjectSettings).get('.project-default input')
  expect(flag().element.checked).toBe(false)
  await chooseProject(wrapper, 'Alpha research and development')
  await chooseProject(wrapper, 'Project settings for Alpha research and development')
  await flag().setValue(true)
  expect(window.localStorage.getItem('lmer.project.default')).toBe('alpha')
  expect(flag().element.checked).toBe(true)
  await chooseProject(wrapper, 'Beta')
  expect(window.localStorage.getItem('lmer.project.last')).toBe('beta')
  wrapper.unmount()

  const reloaded = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(switcherLabel(reloaded)).toBe('Alpha research and development')
  expect(window.localStorage.getItem('lmer.project.last')).toBe('beta')
  await chooseProject(reloaded, 'Project settings for Alpha research and development')
  await reloaded.getComponent(ProjectSettings).get('.project-default input').setValue(false)
  expect(window.localStorage.getItem('lmer.project.default')).toBe('')
  reloaded.unmount()

  const cleared = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(switcherLabel(cleared)).toBe('Beta')
})

// A remembered All projects is stored as '' and is a choice: clearing the default
// must bring it back, not the tab's own URL, which still names the old default.
it('brings back All projects as the last viewed once the default is cleared', async () => {
  const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  await chooseProject(wrapper, 'Alpha research and development')
  await chooseProject(wrapper, 'Project settings for Alpha research and development')
  await wrapper.getComponent(ProjectSettings).get('.project-default input').setValue(true)
  expect(window.localStorage.getItem('lmer.project.default')).toBe('alpha')
  await chooseProject(wrapper, 'All projects')
  expect(window.localStorage.getItem('lmer.project.last')).toBe('')
  wrapper.unmount()

  const reloaded = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(switcherLabel(reloaded)).toBe('Alpha research and development')
  expect(window.location.search).toBe('?workspace=alpha')
  await chooseProject(reloaded, 'Project settings for Alpha research and development')
  await reloaded.getComponent(ProjectSettings).get('.project-default input').setValue(false)
  expect(window.localStorage.getItem('lmer.project.default')).toBe('')
  expect(window.localStorage.getItem('lmer.project.last')).toBe('')
  reloaded.unmount()

  const cleared = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  expect(switcherLabel(cleared)).toBe('All projects')
})

it('renders and switches projects when storage refuses every read and write', async () => {
  const refuse = () => { throw new DOMException('denied', 'SecurityError') }
  const failures = vi.spyOn(console, 'error').mockImplementation(() => {})
  const read = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(refuse)
  const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(refuse)
  try {
    const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
    await flushPromises()
    expect(switcherLabel(wrapper)).toBe('All projects')
    await chooseProject(wrapper, 'Beta')
    expect(switcherLabel(wrapper)).toBe('Beta')
    await chooseProject(wrapper, 'Project settings for Beta')
    await wrapper.getComponent(ProjectSettings).get('.project-default input').setValue(true)
    expect(wrapper.getComponent(ProjectSettings).get('.project-default input').element.checked).toBe(true)
    expect(failures).not.toHaveBeenCalled()
  } finally {
    read.mockRestore()
    write.mockRestore()
    failures.mockRestore()
  }
})

const mountApp = async () => {
  const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  return wrapper
}

it('treats the URL the app wrote itself as its own on a reload, but a link someone followed as a link', async () => {
  window.localStorage.setItem('lmer.project.default', 'alpha')
  window.localStorage.setItem('lmer.project.last', 'beta')
  const first = await mountApp()
  expect(switcherLabel(first)).toBe('Alpha research and development')
  expect(window.location.search).toBe('?workspace=alpha')
  first.unmount()

  const reloaded = await mountApp()
  expect(switcherLabel(reloaded)).toBe('Alpha research and development')
  expect(window.localStorage.getItem('lmer.project.last')).toBe('beta')
  reloaded.unmount()

  window.history.replaceState({}, '', '/?workspace=beta')
  const linked = await mountApp()
  expect(switcherLabel(linked)).toBe('Beta')
  linked.unmount()
  const relinked = await mountApp()
  expect(switcherLabel(relinked)).toBe('Beta')
})

it('keeps the current project across a reload through the URL when storage refuses everything', async () => {
  const refuse = () => { throw new DOMException('denied', 'SecurityError') }
  const read = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(refuse)
  const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(refuse)
  try {
    const first = await mountApp()
    await chooseProject(first, 'Beta')
    first.unmount()
    const reloaded = await mountApp()
    expect(switcherLabel(reloaded)).toBe('Beta')
  } finally {
    read.mockRestore()
    write.mockRestore()
  }
})

it('does not write a remembered project into the URL before the project list confirms it', async () => {
  window.localStorage.setItem('lmer.project.last', 'gone')
  api.fetchProjects.mockRejectedValue(new Error('daemon down'))
  const failed = await mountApp()
  expect(window.location.search).toBe('')
  failed.unmount()

  api.fetchProjects.mockResolvedValue({ projects })
  const reloaded = await mountApp()
  expect(switcherLabel(reloaded)).toBe('All projects')
  expect(reloaded.text()).not.toContain('This project link is unavailable or archived.')
})

it('labels a remembered project neutrally until the first project list arrives', async () => {
  window.localStorage.setItem('lmer.project.last', 'beta')
  const pending = deferred()
  api.fetchProjects.mockReturnValue(pending.promise)
  const wrapper = await mountApp()
  expect(switcherLabel(wrapper)).toBe('Loading project…')
  pending.resolve({ projects })
  await flushPromises()
  expect(switcherLabel(wrapper)).toBe('Beta')
})

it('does not record a followed link as last viewed once the operator has moved away', async () => {
  window.localStorage.setItem('lmer.project.last', 'beta')
  window.history.replaceState({}, '', '/?workspace=alpha')
  const pending = deferred()
  api.fetchProjects.mockReturnValue(pending.promise)
  const wrapper = await mountApp()
  await chooseProject(wrapper, 'All projects')
  expect(window.localStorage.getItem('lmer.project.last')).toBe('')
  pending.resolve({ projects })
  await flushPromises()
  expect(switcherLabel(wrapper)).toBe('All projects')
  expect(window.localStorage.getItem('lmer.project.last')).toBe('')
})
