import { expect, it } from 'vitest'
import ProjectSlots from '../src/components/ProjectSlots.vue'
import { mountPane } from './mount.js'

const alpha = { id: 'alpha', name: 'Example Alpha', color: 'Blue' }
const beta = { id: 'beta', name: 'Example Beta', color: 'Teal' }
const projects = [alpha, beta]
const runs = [alpha, beta].map(project => ({ host: 'forge.test', project: 'shared/repo', slug: `worker-${project.id}`,
  session: { id: `s-${project.id}` }, workspace_id: project.id, workspace_name: project.name }))
const held = id => ({ session_id: `s-${id}` })
const slot = (name, values = {}) => ({ name, state: 'free', applicable_projects: ['alpha', 'beta'],
  needs_project_assignment: false, occupants: [], service_occupants: [], ...values })
const slots = [
  slot('Alpha held', { state: 'occupied', applicable_projects: [], occupants: [held('alpha')] }),
  slot('Shared held', { state: 'occupied', occupants: [held('beta')] }),
  slot('Ready'),
  slot('Beta only', { applicable_projects: ['beta'] }),
  slot('Service overlap', { state: 'occupied', service_occupants: [{ ...held('beta'), slot: 'Shared held' }] }),
  slot('Offline', { state: 'service_down', reason: 'Service is not running' }),
  slot('Unassigned', { state: 'misconfigured', applicable_projects: [], reason: 'No applicable projects' }),
]

it('shows held slots and every owning project in All projects, without management controls', () => {
  const wrapper = mountPane(ProjectSlots, { slots: [...slots, slot('Contended', { state: 'occupied', occupants: runs.map(row => ({ session_id: row.session.id })) })],
    runs, projects, loaded: true })
  expect(wrapper.findAll('.slot-name').map(row => row.text())).toEqual(['Alpha held', 'Shared held', 'Service overlap', 'Contended'])
  expect(wrapper.get('[aria-label="Service slot Alpha held"]').text()).toContain('Example Alpha')
  expect(wrapper.get('[aria-label="Service slot Shared held"]').text()).toContain('Example Beta')
  const overlap = wrapper.get('[aria-label="Service slot Service overlap"]')
  expect(overlap.text()).toContain('Service held by')
  expect(overlap.text()).toContain('via slot Shared held')
  expect(overlap.get('.slot-holder .v-chip').text()).toBe('Example Beta')
  expect(wrapper.get('[aria-label="Service slot Contended"]').findAll('.slot-holder .v-chip').map(row => row.text())).toEqual(
    ['Example Alpha', 'Example Beta'],
  )
  expect(wrapper.find('button, a').exists()).toBe(false)
})

it('separates current availability from eligibility and retained own allocations', async () => {
  const wrapper = mountPane(ProjectSlots, { slots, runs, projects, project: alpha, loaded: true })
  expect(wrapper.findAll('.slot-name').map(row => row.text())).toEqual(['Alpha held', 'Shared held', 'Ready', 'Service overlap', 'Offline'])
  expect(wrapper.findAll('.slot-availability').filter(row => row.text() === 'Available now')).toHaveLength(1)
  expect(wrapper.get('[aria-label="Service slot Ready"]').text()).toContain('Available now')
  expect(wrapper.get('[aria-label="Service slot Shared held"]').text()).toContain('Unavailable for a new run.')
  expect(wrapper.get('[aria-label="Service slot Service overlap"]').text()).toContain('Unavailable for a new run.')
  expect(wrapper.get('[aria-label="Service slot Alpha held"]').text()).toContain('Held allocation retained; not assigned for new runs.')
  expect(wrapper.get('[aria-label="Service slot Offline"]').text()).toContain('Service is not running')
  expect(wrapper.find('button, input').exists()).toBe(false)
  await wrapper.get('a[href="#platform-service-slots"]').trigger('click')
  expect(wrapper.emitted('manage')).toHaveLength(1)
  await wrapper.setProps({ project: beta })
  expect(wrapper.find('[aria-label="Service slot Alpha held"]').exists()).toBe(false)
  expect(wrapper.get('[aria-label="Service slot Beta only"]').text()).toContain('Available now')
  expect(wrapper.get('[aria-label="Service slot Shared held"]').text()).toContain('Held by this project')
})

it('updates ownership, names and availability from refreshed fleet data', async () => {
  const wrapper = mountPane(ProjectSlots, { slots: [slots[1]], runs, projects, project: alpha, loaded: true })
  expect(wrapper.text()).toContain('Example Beta')
  await wrapper.setProps({ projects: [alpha, { ...beta, name: 'Example Beta renamed' }] })
  expect(wrapper.text()).toContain('Example Beta renamed')
  await wrapper.setProps({ slots: [slot('Shared held')] })
  expect(wrapper.text()).toContain('Available now')
  expect(wrapper.find('.slot-holder').exists()).toBe(false)
  await wrapper.setProps({ slots: [slot('Shared held', { state: 'occupied', occupants: [held('alpha')] })] })
  expect(wrapper.text()).toContain('Held by this project')
  expect(wrapper.text()).not.toContain('Available now')
  await wrapper.setProps({ slots: [slot('Shared held', { applicable_projects: ['beta'] })] })
  expect(wrapper.text()).toContain('No service slots held by or assigned to Example Alpha.')
})

it('keeps unknown and indirect holders unavailable rather than inventing a project', () => {
  const wrapper = mountPane(ProjectSlots, { slots: [slot('Unknown holder', { state: 'occupied', occupants: [held('unknown')] })],
    runs, projects, project: alpha, loaded: true })
  expect(wrapper.text()).toContain('Unassigned project')
  expect(wrapper.text()).toContain('Unavailable for a new run.')
  expect(wrapper.text()).not.toContain('Available now')
})

it('distinguishes no configuration, no held slots, loading and failed reads', async () => {
  const wrapper = mountPane(ProjectSlots, { projects })
  expect(wrapper.text()).toContain('Loading service slots')
  expect(wrapper.text()).not.toContain('No service slots')
  await wrapper.setProps({ loaded: true })
  expect(wrapper.text()).toContain('No service slots configured.')
  await wrapper.setProps({ slots: [slot('Ready')] })
  expect(wrapper.text()).toContain('No service slots are currently held.')
  await wrapper.setProps({ project: alpha, problem: 'Slot status could not be refreshed.' })
  expect(wrapper.text()).toContain('Slot status could not be refreshed.')
  expect(wrapper.text()).not.toContain('Available now')
  expect(wrapper.find('.project-slot').exists()).toBe(false)
})

it('names the holder of a uid row, never its sibling in the same group', () => {
  // Two schema-2 launches of one group share host/project/slug; the second
  // holds the slot. A slug match would name the first.
  const uidRuns = [alpha, beta].map((project, i) => ({ host: 'forge.test', project: 'shared/repo', slug: 'chat',
    uid: String(i + 1).repeat(32), workspace_id: project.id, workspace_name: project.name }))
  const heldBySecond = { host: 'forge.test', project: 'shared/repo', slug: 'chat', uid: '2'.repeat(32) }
  const wrapper = mountPane(ProjectSlots, {
    slots: [slot('Chat held', { state: 'occupied', occupants: [heldBySecond] })], runs: uidRuns, projects, loaded: true,
  })
  expect(wrapper.get('[aria-label="Service slot Chat held"] .slot-holder .v-chip').text()).toBe('Example Beta')
})
