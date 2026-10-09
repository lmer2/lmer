// "Sessions per target" (#374, spec §13.F): every launch of one `_runs/<slug>/`
// group, each addressable by its uid.
//
// The rows are the daemon's own reply — web/tests/fixtures/schema-form/run-sessions.json
// is what GET /api/runs/sessions answers for the two-record fixture group
// (tests/test_platform_schema_views.py pins it) — imported read-only. Where a
// case needs the rows to differ in a way the golden cannot (a minted project id
// is `<volatile>` there), it changes a deep copy and says which field.
//
// The real api.js runs against a stubbed fetch, as in RunRefUid.test.js, because
// what is pinned is the wire: which uid and which project context a resume posts.
// Astra PR6 is the reason that matters — two records of one group can be homed in
// different projects at different revisions, and #542's request binding refuses a
// resume that borrows the parent row's.

import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import golden from './fixtures/schema-form/run-sessions.json'
import CopyButton from '../src/components/CopyButton.vue'
import RunDetail from '../src/components/RunDetail.vue'
import SessionsPerTarget from '../src/components/SessionsPerTarget.vue'
import { mountPane } from './mount.js'

const [FIRST, SECOND] = golden.sessions

// The fleet row the view is opened from: the group's first launch, homed in a
// project of its own so that a resume borrowing it is detectable.
const parent = {
  host: golden.run.host, project: golden.run.project, slug: 'chat', uid: FIRST.uid,
  workspace_id: 'ws-parent', home_revision: 9, confirm_project: false,
  label: 'chat', state: 'complete', live: false, questions: [], attention: null,
}

let request
let sessionsReply
let resumeReply

function respond(status, body) {
  return { ok: status < 400, status, json: async () => body }
}

beforeEach(() => {
  window.localStorage.clear()
  sessionsReply = structuredClone(golden)
  resumeReply = () => respond(202, { session: { session_id: 's-new' }, note: 'continuation started' })
  request = vi.fn(async (path) => {
    if (path.startsWith('api/runs/sessions?')) return respond(200, sessionsReply)
    if (path === 'api/runs/resume') return resumeReply()
    return respond(200, {})
  })
  vi.stubGlobal('fetch', request)
})

afterEach(() => vi.unstubAllGlobals())

function calls(prefix) {
  return request.mock.calls.filter(([path]) => path.startsWith(prefix))
}

function queryOf(path) {
  return Object.fromEntries(new URL(path, 'http://ui.test/').searchParams)
}

async function mountView(run = parent) {
  const wrapper = mountPane(SessionsPerTarget, { run })
  await flushPromises()
  return wrapper
}

function rows(wrapper) {
  return wrapper.findAll('.session-row')
}

function button(scope, label) {
  return scope.findAll('button').find((candidate) => candidate.text().trim() === label)
}

// Both launches tracked, each homed somewhere different from the other and from
// the parent row — the golden's ids are `<volatile>`, so they are named here.
function bothTrackedInTwoHomes() {
  sessionsReply.sessions[0].workspace_id = 'ws-general'
  sessionsReply.sessions[1].workspace_id = 'ws-alpha'
  sessionsReply.sessions[1].tracked = true
}

// --- the rows -------------------------------------------------------------------

it('asks for the group by the parent row\'s key', async () => {
  await mountView()
  const [sent] = calls('api/runs/sessions?')
  expect(queryOf(sent[0])).toEqual({
    host: parent.host, project: parent.project, slug: 'chat', uid: FIRST.uid,
    workspace_id: 'ws-parent', home_revision: '9',
  })
})

it('renders the two records as two rows, in the reply\'s order, with their fields', async () => {
  const wrapper = await mountView()
  const found = rows(wrapper)
  expect(found).toHaveLength(golden.sessions.length)

  golden.sessions.forEach((session, index) => {
    const row = found[index]
    const text = row.text()
    expect(text).toContain(session.uid.slice(0, 8))
    expect(text).not.toContain(session.uid)
    expect(text).toContain(session.status)
    expect(text).toContain(session.record_state)
    // The whole uid is what is copied, not the short one that is shown.
    expect(row.findComponent(CopyButton).props('text')).toBe(session.uid)
    expect(row.find('.session-live').exists()).toBe(session.live)
  })

  expect(found[0].text()).not.toContain('--')
  expect(found[1].text()).toContain(`--${SECOND.name_suffix}`)
  // The record the request addressed is marked as the one this page is about.
  expect(found[0].text()).toContain('this run')
  expect(found[1].text()).not.toContain('this run')
})

it('gives a tracked row a resume action and an untracked one none, and no adopt control', async () => {
  const wrapper = await mountView()
  const [tracked, untracked] = rows(wrapper)
  expect(FIRST.tracked).toBe(true)
  expect(SECOND.tracked).toBe(false)

  expect(button(tracked, 'resume')).toBeTruthy()
  expect(button(untracked, 'resume')).toBeUndefined()
  expect(untracked.text()).toContain('read-only')
  for (const scope of [tracked, untracked]) {
    expect(scope.findAll('button').some((b) => /adopt/i.test(b.text()))).toBe(false)
  }
  expect(calls('api/runs/adopt')).toHaveLength(0)
})

// --- resume, by the row's own uid and project context ------------------------------

it('resumes each row with that row\'s uid and its own workspace and home revision', async () => {
  bothTrackedInTwoHomes()
  const wrapper = await mountView()
  const [first, second] = rows(wrapper)

  await button(first, 'resume').trigger('click')
  await flushPromises()
  await button(first, 'resume this launch').trigger('click')
  await flushPromises()

  // The second launch moved projects, so the daemon asks for reconfirmation and
  // the verb waits for it — #542's checkbox, read off THIS row, not the parent.
  await button(second, 'resume').trigger('click')
  await flushPromises()
  expect(button(second, 'resume this launch').attributes('disabled')).toBeDefined()
  await second.find('input[type="checkbox"]').setValue(true)
  await flushPromises()
  await button(second, 'resume this launch').trigger('click')
  await flushPromises()

  const posted = calls('api/runs/resume').map(([path, options]) => {
    expect(options.method).toBe('POST')
    return JSON.parse(options.body)
  })
  expect(posted).toEqual([
    {
      host: parent.host, project: parent.project, slug: 'chat', uid: FIRST.uid,
      workspace_id: 'ws-general', home_revision: FIRST.home_revision,
    },
    {
      host: parent.host, project: parent.project, slug: 'chat', uid: SECOND.uid,
      workspace_id: 'ws-alpha', home_revision: SECOND.home_revision, confirm_project: true,
    },
  ])
  // The two rows differ from each other and neither borrowed the parent's.
  expect(posted[0].workspace_id).not.toBe(posted[1].workspace_id)
  expect(posted[0].home_revision).not.toBe(posted[1].home_revision)
  for (const body of posted) {
    expect(body.workspace_id).not.toBe(parent.workspace_id)
    expect(body.home_revision).not.toBe(parent.home_revision)
  }
})

it('shows A2\'s concurrency warning inline on the row that was resumed', async () => {
  const warning = '1 live session(s) already on `git.example.test/acme/widgets` for `chat`: s-1'
  resumeReply = () => respond(202, {
    session: { session_id: 's-new' }, note: 'continuation started', warning,
  })
  const wrapper = await mountView()
  const [first, second] = rows(wrapper)
  await button(first, 'resume').trigger('click')
  await button(first, 'resume this launch').trigger('click')
  await flushPromises()
  expect(first.text()).toContain(warning)
  expect(first.text()).toContain('s-new')
  expect(second.text()).not.toContain(warning)
})

it('shows the daemon\'s refusal inline, in its own words', async () => {
  resumeReply = () => respond(409, { detail: {
    code: 'project_reconfirmation_required',
    message: 'This run moved. Confirm its current project before continuing.',
  } })
  const wrapper = await mountView()
  const [first] = rows(wrapper)
  await button(first, 'resume').trigger('click')
  await button(first, 'resume this launch').trigger('click')
  await flushPromises()
  expect(first.text()).toContain('This run moved. Confirm its current project before continuing.')
})

it('says so when the mirror holds no launch of the group yet', async () => {
  sessionsReply = { ...structuredClone(golden), group: null, sessions: [] }
  const wrapper = await mountView()
  expect(rows(wrapper)).toHaveLength(0)
  expect(wrapper.text()).toContain('No launch of this target')
})

it('shows a failed read rather than an empty list', async () => {
  request.mockImplementation(async (path) => (
    path.startsWith('api/runs/sessions?')
      ? respond(500, { detail: 'mirror unreadable' })
      : respond(200, {})
  ))
  const wrapper = await mountView()
  expect(wrapper.text()).toContain('mirror unreadable')
})

// --- where it is mounted ------------------------------------------------------------

const stubs = { global: { stubs: {
  Chat: true, Terminal: true, RelatedRuns: true, RunMeta: true, ThreadLinks: true,
  AskChannel: true, AskHistory: true, AnswerBox: true,
} } }

it('RunDetail mounts the view for a schema-2 row and it reads the group', async () => {
  const wrapper = mountPane(RunDetail, { run: parent }, stubs)
  await flushPromises()
  expect(wrapper.findComponent(SessionsPerTarget).exists()).toBe(true)
  expect(calls('api/runs/sessions?')).toHaveLength(1)
})

it('RunDetail mounts nothing and asks nothing for a legacy row', async () => {
  const legacy = { ...parent, uid: null }
  const wrapper = mountPane(RunDetail, { run: legacy }, stubs)
  await flushPromises()
  expect(wrapper.findComponent(SessionsPerTarget).exists()).toBe(false)
  expect(wrapper.text()).not.toContain('launches of this target')
  expect(calls('api/runs/sessions')).toHaveLength(0)
})
