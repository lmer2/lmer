// #374's two work-repo panels (spec §13.F, §13.C): the unnamed repos awaiting
// adoption and the validator's findings, both read off one
// `lmer-schema validate --json` run the daemon serves at GET /api/workrepo/schema.
//
// The replies are the daemon's own — web/tests/fixtures/schema-form/workrepo-schema.json
// and web/tests/fixtures/legacy/workrepo-schema.json, pinned by
// tests/test_platform_schema_report.py — imported read-only. A case that needs a
// shape the goldens do not have (an unusable manifest, an empty list) changes a
// deep copy and says which field.
//
// Mounted through App, with api.js mocked, because WHERE the panels live is half
// of what is pinned: once, in the all-projects fleet, and only for a mirror in
// schema form. The work repo is shared by every workspace, so it is not inside one.

import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import schemaGolden from './fixtures/schema-form/workrepo-schema.json'
import legacyGolden from './fixtures/legacy/workrepo-schema.json'
import App from '../src/App.vue'
import CopyButton from '../src/components/CopyButton.vue'
import HostBuckets from '../src/components/HostBuckets.vue'
import ValidatorFindings from '../src/components/ValidatorFindings.vue'
import WorkRepoSchema from '../src/components/WorkRepoSchema.vue'
import { adoptRun, fetchProjects, fetchState, fetchWorkRepoSchema } from '../src/api.js'
import { mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({
  fetchProjects: vi.fn(),
  fetchState: vi.fn(), forgetRun: vi.fn(), prune: vi.fn(), rescan: vi.fn(),
  fetchRunRelations: vi.fn().mockResolvedValue({ run: {}, relations: [] }),
  fetchWorkRepoSchema: vi.fn(),
  // Present only so a call to it would be seen: these panels must never make one.
  adoptRun: vi.fn(),
}))

const projects = [{ id: 'general', name: 'General', color: 'Blue', assistant: { lifecycle: 'stopped' } }]

function state(form, extra = {}) {
  return {
    runs: [], attention: [], totals: { runs: 0, live: 0, attention: 0 },
    mirror: {
      present: true, healthy: true, head_sha: 'a'.repeat(40), last_error: null, form,
    },
    ...extra,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  window.history.replaceState({}, '', '/')
  fetchProjects.mockResolvedValue({ projects })
  fetchWorkRepoSchema.mockResolvedValue(structuredClone(schemaGolden))
})

async function mountApp(payload, url = '/') {
  window.history.replaceState({}, '', url)
  fetchState.mockResolvedValue(payload)
  const wrapper = mountPane(App, {}, { global: { stubs: { AssistantChat: true } } })
  await flushPromises()
  await flushPromises()
  return wrapper
}

function findingFields(row) {
  const field = (name) => {
    const cell = row.find(`.finding-${name}`)
    return cell.exists() ? cell.text() : null
  }
  return {
    severity: field('severity'), code: field('code'), path: field('path'),
    message: field('message'), detail: field('detail'),
  }
}

// --- schema form ------------------------------------------------------------------

it('mounts the panels once in the all-projects fleet and reads the report once', async () => {
  const wrapper = await mountApp(state('schema'))
  expect(wrapper.findAllComponents(WorkRepoSchema)).toHaveLength(1)
  expect(wrapper.findAllComponents(HostBuckets)).toHaveLength(1)
  expect(wrapper.findAllComponents(ValidatorFindings)).toHaveLength(1)
  expect(fetchWorkRepoSchema).toHaveBeenCalledTimes(1)
  expect(fetchWorkRepoSchema).toHaveBeenCalledWith()
})

it('lists the unnamed repo with its copyable adopt line and no adopt control', async () => {
  const wrapper = await mountApp(state('schema'))
  const panel = wrapper.getComponent(HostBuckets)
  const [bucket] = schemaGolden.buckets

  expect(panel.text()).toContain('Unnamed repos awaiting adoption')
  const rows = panel.findAll('.bucket-row')
  expect(rows).toHaveLength(schemaGolden.buckets.length)
  expect(rows[0].text()).toContain(bucket.path)
  expect(rows[0].text()).toContain(bucket.adopt_command)
  expect(rows[0].getComponent(CopyButton).props('text')).toBe(bucket.adopt_command)

  // The only buttons in the panel are the copy buttons: no adopt verb, no form.
  const buttons = panel.findAll('button')
  expect(buttons).toHaveLength(panel.findAllComponents(CopyButton).length)
  expect(buttons.some((button) => /adopt/i.test(button.text()))).toBe(false)
  for (const copy of panel.findAllComponents(CopyButton)) await copy.trigger('click')
  await flushPromises()
  expect(adoptRun).not.toHaveBeenCalled()
})

it('renders one finding row per document finding, field for field, in order', async () => {
  const wrapper = await mountApp(state('schema'))
  const panel = wrapper.getComponent(ValidatorFindings)
  const rows = panel.findAll('.finding-row')
  expect(rows).toHaveLength(schemaGolden.document.findings.length)
  expect(rows.map(findingFields)).toEqual(schemaGolden.document.findings.map((finding) => ({
    severity: finding.severity, code: finding.code, path: finding.path,
    message: finding.message, detail: finding.detail,
  })))
})

it('heads the findings with the form and the tier', async () => {
  const wrapper = await mountApp(state('schema'))
  const header = wrapper.getComponent(ValidatorFindings).get('.findings-header')
  expect(header.text()).toContain(`form ${schemaGolden.form}`)
  expect(header.text()).toContain(`tier ${schemaGolden.tier}`)
  expect(header.text()).not.toContain('unusable')
})

it('reads an all-INFO list as notes, not as problems', async () => {
  const counts = schemaGolden.document.counts
  expect(counts.error + counts.warn).toBe(0)
  const wrapper = await mountApp(state('schema'))
  const text = wrapper.getComponent(ValidatorFindings).text()
  expect(text).toContain('No errors or warnings')
  expect(text).toContain(`${counts.info} notes`)
})

it('says an empty list is a clean tree', async () => {
  const reply = structuredClone(schemaGolden)
  reply.document.findings = []
  reply.document.counts = { error: 0, warn: 0, info: 0 }
  reply.buckets = []
  fetchWorkRepoSchema.mockResolvedValue(reply)
  const wrapper = await mountApp(state('schema'))
  expect(wrapper.getComponent(ValidatorFindings).text()).toContain('Nothing to report')
  expect(wrapper.getComponent(HostBuckets).text()).toContain('No unnamed repo')
})

it('shows `unusable` in the header when the manifest could not be read', async () => {
  const reply = structuredClone(schemaGolden)
  reply.document.unusable = '_lmer.yaml: not a mapping'
  reply.document.exit_code = 2
  fetchWorkRepoSchema.mockResolvedValue(reply)
  const wrapper = await mountApp(state('schema'))
  const header = wrapper.getComponent(ValidatorFindings).get('.findings-header')
  expect(header.text()).toContain('unusable')
  expect(header.text()).toContain('_lmer.yaml: not a mapping')
})

it('shows a failed read rather than empty panels', async () => {
  fetchWorkRepoSchema.mockRejectedValue(new Error('HTTP 500'))
  const wrapper = await mountApp(state('schema'))
  expect(wrapper.getComponent(WorkRepoSchema).text()).toContain('HTTP 500')
})

// --- re-reading: on a new HEAD, and on demand ------------------------------------------

it('reads again when the mirror HEAD moves, and not otherwise', async () => {
  const wrapper = mountPane(WorkRepoSchema, { headSha: 'a'.repeat(40) })
  await flushPromises()
  expect(fetchWorkRepoSchema).toHaveBeenCalledTimes(1)
  await wrapper.setProps({ headSha: 'a'.repeat(40) })
  await flushPromises()
  expect(fetchWorkRepoSchema).toHaveBeenCalledTimes(1)
  await wrapper.setProps({ headSha: 'b'.repeat(40) })
  await flushPromises()
  expect(fetchWorkRepoSchema).toHaveBeenCalledTimes(2)
  expect(fetchWorkRepoSchema).toHaveBeenLastCalledWith()
})

it('re-validates on the refresh control, independent of HEAD', async () => {
  // astra PR7: staleness findings age with the clock at an unchanged HEAD.
  const wrapper = mountPane(WorkRepoSchema, { headSha: 'a'.repeat(40) })
  await flushPromises()
  const refresh = wrapper.findAll('button').find((button) => button.text().includes('re-validate'))
  await refresh.trigger('click')
  await flushPromises()
  expect(fetchWorkRepoSchema).toHaveBeenCalledTimes(2)
  expect(fetchWorkRepoSchema).toHaveBeenLastCalledWith({ refresh: true })
})

// --- where they are not --------------------------------------------------------------

it('mounts neither panel for a legacy mirror and never asks', async () => {
  fetchWorkRepoSchema.mockResolvedValue(structuredClone(legacyGolden))
  const wrapper = await mountApp(state(legacyGolden.form))
  expect(legacyGolden.form).toBe('legacy')
  expect(wrapper.findComponent(WorkRepoSchema).exists()).toBe(false)
  expect(wrapper.findComponent(HostBuckets).exists()).toBe(false)
  expect(wrapper.findComponent(ValidatorFindings).exists()).toBe(false)
  expect(wrapper.text()).not.toContain('Unnamed repos awaiting adoption')
  expect(fetchWorkRepoSchema).not.toHaveBeenCalled()
})

it('mounts nothing for a payload that carries no form (an older daemon)', async () => {
  const payload = state(undefined)
  delete payload.mirror.form
  const wrapper = await mountApp(payload)
  expect(wrapper.findComponent(WorkRepoSchema).exists()).toBe(false)
  expect(fetchWorkRepoSchema).not.toHaveBeenCalled()
})

it('does not mount inside one project\'s fleet', async () => {
  const wrapper = await mountApp(state('schema'), '/?workspace=general')
  expect(wrapper.findComponent(WorkRepoSchema).exists()).toBe(false)
  expect(fetchWorkRepoSchema).not.toHaveBeenCalled()
})
