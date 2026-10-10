import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import RunDetail from '../src/components/RunDetail.vue'
import {
  adoptRun, answerRun, fetchRunMeta, fetchRunRelations, forgetRun, moveRun,
  relateRun, resumeRun, setRunMeta, unrelateRun,
} from '../src/api.js'
import { mountPane } from './mount.js'

// Spec §6.1/§6.3: a schema-2 fleet row is addressed by its uid, so every
// run-keyed request carries the row's `uid` beside host/project/slug. A row with
// no uid (schema-1, or `uid: null` off the daemon) must put exactly the bytes on
// the wire it always did — no `uid` key at all, not `"uid":null` — which is why
// the slug-row assertions compare the raw body string and the raw query rather
// than a parsed object that would not notice an extra null.
//
// The real api.js runs against a stubbed fetch, as in ApiAnswerResume.test.js:
// the component tests mock api.js wholesale and never see the wire.

const uid = '0123456789abcdef0123456789abcdef'
const slugRow = {
  host: 'forge.test', project: 'acme/widget', slug: 'develop-1',
  workspace_id: 'beta', home_revision: 2, uid: null,
  label: 'develop-1', state: 'complete', live: false, questions: [], attention: null,
}
const uidRow = { ...slugRow, uid }
const relatedSlug = { host: 'forge.test', project: 'acme/other', slug: 'review-1', uid: null, tracked: true }
const relatedUid = { ...relatedSlug, uid: 'fedcba9876543210fedcba9876543210' }

let request

beforeEach(() => {
  request = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
  vi.stubGlobal('fetch', request)
})

afterEach(() => vi.unstubAllGlobals())

function sent(index = 0) {
  const [path, options = {}] = request.mock.calls[index]
  return { path, body: options.body }
}

function queryOf(path) {
  return Object.fromEntries(new URL(path, 'http://ui.test/').searchParams)
}

const bodyCalls = {
  adoptRun: (row) => adoptRun(row),
  forgetRun: (row) => forgetRun(row),
  setRunMeta: (row) => setRunMeta(row, { title: 'T', description: 'D' }),
  answerRun: (row) => answerRun(row, 'Proceed'),
  resumeRun: (row) => resumeRun(row),
  moveRun: (row) => moveRun(row, 'gamma'),
}

// What each of these sent before uid existed, byte for byte.
const legacyBodies = {
  adoptRun: '{"host":"forge.test","project":"acme/widget","slug":"develop-1","workspace_id":"beta","home_revision":2}',
  forgetRun: '{"host":"forge.test","project":"acme/widget","slug":"develop-1","workspace_id":"beta","home_revision":2}',
  setRunMeta: '{"host":"forge.test","project":"acme/widget","slug":"develop-1","workspace_id":"beta","home_revision":2,"title":"T","description":"D"}',
  answerRun: '{"host":"forge.test","project":"acme/widget","slug":"develop-1","workspace_id":"beta","home_revision":2,"answer":"Proceed"}',
  resumeRun: '{"host":"forge.test","project":"acme/widget","slug":"develop-1","workspace_id":"beta","home_revision":2}',
  moveRun: '{"host":"forge.test","project":"acme/widget","slug":"develop-1","workspace_id":"beta","home_revision":2,"destination_id":"gamma"}',
}

for (const [name, call] of Object.entries(bodyCalls)) {
  it(`${name} sends the uid of a schema-2 row in its body`, async () => {
    await call(uidRow)
    const body = JSON.parse(sent().body)
    expect(body).toMatchObject({
      host: 'forge.test', project: 'acme/widget', slug: 'develop-1', uid,
      workspace_id: 'beta', home_revision: 2,
    })
  })

  it(`${name} sends a slug row's body byte-identical, with no uid key`, async () => {
    await call(slugRow)
    expect(sent().body).toBe(legacyBodies[name])
  })
}

it('answerRun and resumeRun keep uid beside the #542 and schema-v2 fields', async () => {
  const source = 'a'.repeat(32)
  await answerRun(uidRow, 'Proceed', { confirmProject: true, slot: 'web', allowUnsignedSource: source })
  await resumeRun(uidRow, { direction: 'go on', confirmProject: true, slot: 'web', allowUnsignedSource: source })
  expect(JSON.parse(sent(0).body)).toEqual({
    host: 'forge.test', project: 'acme/widget', slug: 'develop-1', uid,
    workspace_id: 'beta', home_revision: 2, answer: 'Proceed',
    confirm_project: true, slot: 'web', allow_unsigned_source: source,
  })
  expect(JSON.parse(sent(1).body)).toEqual({
    host: 'forge.test', project: 'acme/widget', slug: 'develop-1', uid,
    workspace_id: 'beta', home_revision: 2, direction: 'go on',
    confirm_project: true, slot: 'web', allow_unsigned_source: source,
  })
})

it('answerRun serializes the repository URL the daemon asks for', async () => {
  await answerRun(uidRow, 'Proceed', { repoUrl: 'https://forge.test/acme/widget.git' })
  expect(JSON.parse(sent().body)).toEqual({
    host: 'forge.test', project: 'acme/widget', slug: 'develop-1', uid,
    workspace_id: 'beta', home_revision: 2, answer: 'Proceed',
    repo_url: 'https://forge.test/acme/widget.git',
  })
})

for (const [name, call] of Object.entries({ relateRun, unrelateRun })) {
  it(`${name} sends the uid of both the subject and the related run`, async () => {
    await call(uidRow, relatedUid)
    expect(JSON.parse(sent().body)).toEqual({
      host: 'forge.test', project: 'acme/widget', slug: 'develop-1', uid,
      workspace_id: 'beta', home_revision: 2,
      related: {
        host: 'forge.test', project: 'acme/other', slug: 'review-1',
        uid: 'fedcba9876543210fedcba9876543210',
      },
    })
  })

  it(`${name} keys each side independently of the other`, async () => {
    await call(slugRow, relatedUid)
    await call(uidRow, relatedSlug)
    expect(JSON.parse(sent(0).body)).not.toHaveProperty('uid')
    expect(JSON.parse(sent(0).body).related).toHaveProperty('uid', relatedUid.uid)
    expect(JSON.parse(sent(1).body)).toHaveProperty('uid', uid)
    expect(JSON.parse(sent(1).body).related).not.toHaveProperty('uid')
  })

  it(`${name} sends two slug rows byte-identical, with no uid key`, async () => {
    await call(slugRow, relatedSlug)
    expect(sent().body).toBe(
      '{"host":"forge.test","project":"acme/widget","slug":"develop-1","workspace_id":"beta","home_revision":2,'
      + '"related":{"host":"forge.test","project":"acme/other","slug":"review-1"}}',
    )
  })
}

for (const [name, call, route] of [
  ['fetchRunMeta', fetchRunMeta, 'api/runs/meta'],
  ['fetchRunRelations', fetchRunRelations, 'api/runs/relations'],
]) {
  it(`${name} puts the uid of a schema-2 row in the query`, async () => {
    await call(uidRow)
    expect(queryOf(sent().path)).toEqual({
      host: 'forge.test', project: 'acme/widget', slug: 'develop-1', uid,
      workspace_id: 'beta', home_revision: '2',
    })
  })

  it(`${name} sends a slug row's query unchanged, with no uid parameter`, async () => {
    await call(slugRow)
    expect(sent().path).toBe(
      `${route}?host=forge.test&project=acme%2Fwidget&slug=develop-1&workspace_id=beta&home_revision=2`,
    )
  })
}

// The run-files read is still inline in RunDetail (T66), so it is checked by
// mounting the view: the overview tab is the default and fetches on mount.
const stubs = { global: { stubs: {
  Chat: true, Terminal: true, RelatedRuns: true, RunMeta: true, ThreadLinks: true,
  AskChannel: true, AskHistory: true, AnswerBox: true,
} } }

function filesCall() {
  return request.mock.calls.map(([path]) => path).find((path) => path.startsWith('api/runs/files?'))
}

it('RunDetail reads a schema-2 run\'s files by uid', async () => {
  window.localStorage.clear()
  mountPane(RunDetail, { run: uidRow }, stubs)
  await flushPromises()
  expect(queryOf(filesCall())).toEqual({
    host: 'forge.test', project: 'acme/widget', slug: 'develop-1', uid,
  })
})

it('RunDetail reads a slug run\'s files with the query it always sent', async () => {
  window.localStorage.clear()
  mountPane(RunDetail, { run: slugRow }, stubs)
  await flushPromises()
  expect(filesCall()).toBe('api/runs/files?host=forge.test&project=acme%2Fwidget&slug=develop-1')
})

it('RunDetail passes the uid-bearing row to forget and resume', async () => {
  window.localStorage.clear()
  const wrapper = mountPane(RunDetail, { run: uidRow }, stubs)
  await flushPromises()
  await wrapper.findAll('button').find((button) => button.text() === 'continue this run').trigger('click')
  await flushPromises()
  await wrapper.findAll('[role="tab"]').find((tab) => tab.text().trim() === 'exit').trigger('click')
  await flushPromises()
  await wrapper.findAll('button').find((button) => button.text() === 'forget this run').trigger('click')
  await flushPromises()
  const posted = request.mock.calls
    .filter(([path]) => path === 'api/runs/resume' || path === 'api/runs/forget')
    .map(([path, options]) => [path, JSON.parse(options.body).uid])
  expect(posted).toEqual([['api/runs/resume', uid], ['api/runs/forget', uid]])
})
