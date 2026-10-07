import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import App from '../src/App.vue'
import AssistantChat from '../src/components/AssistantChat.vue'
import PlatformSettings from '../src/components/PlatformSettings.vue'
import Chat from '../src/components/Chat.vue'
import RunDetail from '../src/components/RunDetail.vue'
import RunNav from '../src/components/RunNav.vue'
import { projectColor } from '../src/projects.js'
import RunCard from '../src/components/RunCard.vue'
import ProjectNav from '../src/components/ProjectNav.vue'
import ProjectSettings from '../src/components/ProjectSettings.vue'
import ProjectSlots from '../src/components/ProjectSlots.vue'
import * as api from '../src/api.js'
import { mountPane, deferred } from './mount.js'

vi.mock('../src/api.js', async importOriginal => ({
  ...await importOriginal(), fetchState: vi.fn(), fetchProjects: vi.fn(),
  fetchAssistant: vi.fn(), fetchAssistantConfig: vi.fn(), fetchAssistantInstructions: vi.fn(),
  startAssistant: vi.fn(), stopAssistant: vi.fn(), rotateAssistant: vi.fn(), fetchAssistantHistory: vi.fn(),
  fetchPlatformSettings: vi.fn(), fetchConversationRequests: vi.fn(), parkConversationRequest: vi.fn(),
  setAssistantInstructions: vi.fn(), updateProject: vi.fn(), archiveProject: vi.fn(), setPlatformSettings: vi.fn(),
  fetchSessionMessages: vi.fn(), sendSessionInput: vi.fn(), uploadSessionFile: vi.fn(),
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
  await wrapper.findAll('[role="tab"]').find(tab => tab.text() === 'All projects').trigger('click')
  await flushPromises()
  expect(window.location.search).toBe('')
  expect(wrapper.getComponent(AssistantChat).props('workspace').id).toBe('alpha')
  expect(wrapper.getComponent(AssistantChat).element.parentElement.style.display).not.toBe('none')
  expect(wrapper.get('.project-identity').text()).toContain('All projects')
  const groups = wrapper.findAll('.project-nav')
  await groups[0].get('.project-toggle').trigger('click')
  expect(groups[0].get('.project-toggle').attributes('aria-expanded')).toBe('false')
  expect(groups[1].get('.project-toggle').attributes('aria-expanded')).toBe('true')
})

it('shows waiting/cancel with an explicit project and keeps a draft until the assistant starts', async () => {
  const wrapper = mountPane(AssistantChat, { workspace: projects[1] })
  await flushPromises()
  expect(wrapper.text()).toContain('Beta · uber lmer')
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
  await wrapper.findAll('[role="tab"]').find(tab => tab.text() === 'All projects').trigger('click')
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

it('keeps a text project badge beside three named icon actions bound to the selected project', async () => {
  window.history.replaceState({}, '', '/?workspace=beta')
  const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true, ProjectSettings: true } } })
  await flushPromises()
  const toolbar = wrapper.get('.project-identity')
  expect(toolbar.get('.v-chip').text()).toBe('Beta')
  const buttons = toolbar.findAll('button')
  expect(buttons.map(button => button.attributes('aria-label'))).toEqual(['Chat with Beta', 'Project settings for Beta', 'New project'])
  for (const button of buttons) {
    expect(button.classes()).toContain('v-btn--icon')
    expect(button.find('.v-icon').exists()).toBe(true)
    expect(button.text()).toBe('')
    expect(button.attributes('aria-describedby')).toBeTruthy()
  }
  await buttons[0].trigger('click')
  await flushPromises()
  expect(wrapper.getComponent(AssistantChat).props('workspace').id).toBe('beta')
  await buttons[1].trigger('click')
  await flushPromises()
  expect(wrapper.getComponent(ProjectSettings).props('project').id).toBe('beta')
  await buttons[2].trigger('click')
  await flushPromises()
  expect(document.querySelector('.v-dialog input')).not.toBeNull()
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
  await wrapper.get('[aria-label="close the uber lmer chat"]').trigger('click')
  await wrapper.findAll('[role="tab"]').find(tab => tab.text().startsWith('Beta')).trigger('click')
  await flushPromises()
  if (shortcut === 'toolbar') await wrapper.get('header button[aria-label^="uber lmer"]').trigger('click')
  else await wrapper.getComponent(RunCard).get('[aria-label="talk to uber lmer"]').trigger('click')
  await flushPromises()
  const pane = wrapper.findAllComponents(AssistantChat).find(pane => pane.get('.project-chat-title').isVisible())
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
  await wrapper.findAll('button').find(button => button.text() === 'restart').trigger('click')
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
    if (identity === 'beta') expect(wrapper.get('.project-identity').text()).toContain('Beta')
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

it('badges uber lmer\'s open questions from its status poll and asks the shell to open them', async () => {
  vi.useFakeTimers()
  const answers = [2, 1, 0]
  api.fetchAssistant.mockImplementation(() => Promise.resolve({ running: true, lifecycle: 'running',
    session_id: 's-alpha', generation: 3, open_questions: answers.length > 1 ? answers.shift() : answers[0] }))
  const wrapper = mountPane(AssistantChat, { workspace: projects[0] })
  await flushPromises()
  const badge = () => wrapper.find('.open-questions')
  expect(badge().exists()).toBe(true)
  expect(badge().text()).toContain('2')
  expect(badge().attributes('aria-label')).toBe('2 open questions from uber lmer — answer in its run view')
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

it('opens uber lmer\'s own run on its question badge and focuses the ask dock there', async () => {
  const orchestrator = { host: '—', project: '—', slug: 's-alpha', label: 'uber lmer', orchestrator: true,
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

it('labels project execution overrides readably and says what an empty one inherits', async () => {
  const wrapper = mountPane(ProjectSettings, { project: { ...projects[0], repositories: [], settings: {} } })
  await flushPromises()
  const labels = wrapper.findAll('.v-field-label, label.v-label').map(node => node.text())
  for (const label of ['Model', 'Harness', 'Preset', 'Fan-out presets']) expect(labels).toContain(label)
  for (const raw of ['model', 'harness', 'preset', 'agents']) expect(labels).not.toContain(raw)
  expect(wrapper.text()).toContain('Empty inherits the platform default.')
})
