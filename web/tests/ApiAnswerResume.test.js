import { expect, it, vi } from 'vitest'
import { answerRun, resumeRun } from '../src/api.js'

// The wire mapping the component tests cannot see: AnswerBox.test.js and
// OperatorChat.test.js mock api.js wholesale, so the body builders never run.
// These call the real functions against a stubbed fetch and assert the JSON that
// goes on the wire — in particular that schema-v2's `allow_unsigned_source`
// survives alongside prep-release's `confirm_project`/`slot`.
async function sentBody(call) {
  const request = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
  vi.stubGlobal('fetch', request)
  try {
    await call()
    return JSON.parse(request.mock.calls[0][1].body)
  } finally {
    vi.unstubAllGlobals()
  }
}

it('answerRun sends both field sets together', async () => {
  const uid = 'a'.repeat(32)
  const body = await sentBody(() => answerRun(
    {
      host: 'forge.test', project: 'acme/widget', slug: 'develop-1',
      workspace_id: 'beta', home_revision: 2,
    },
    'Proceed carefully',
    { confirmProject: true, slot: 'web', allowUnsignedSource: uid },
  ))
  expect(body).toEqual({
    host: 'forge.test', project: 'acme/widget', slug: 'develop-1',
    workspace_id: 'beta', home_revision: 2,
    answer: 'Proceed carefully',
    confirm_project: true, slot: 'web',
    allow_unsigned_source: uid,
  })
})

it('answerRun omits absent project fields rather than sending them empty', async () => {
  const body = await sentBody(() => answerRun(
    { host: 'forge.test', project: 'acme/widget', slug: 'develop-1' },
    'Proceed carefully',
  ))
  expect(body).toEqual({
    host: 'forge.test', project: 'acme/widget', slug: 'develop-1',
    answer: 'Proceed carefully',
  })
})

it('resumeRun sends both field sets together', async () => {
  const uid = 'b'.repeat(32)
  const body = await sentBody(() => resumeRun(
    {
      host: 'forge.test', project: 'acme/widget', slug: 'develop-1',
      workspace_id: 'beta', home_revision: 2,
    },
    {
      taskdef: 'review',
      repoUrl: 'https://forge.test/acme/widget.git',
      direction: 'look at the timeout',
      confirmProject: true,
      slot: 'web',
      allowUnsignedSource: uid,
    },
  ))
  expect(body).toEqual({
    host: 'forge.test', project: 'acme/widget', slug: 'develop-1',
    workspace_id: 'beta', home_revision: 2,
    taskdef: 'review',
    repo_url: 'https://forge.test/acme/widget.git',
    direction: 'look at the timeout',
    confirm_project: true,
    slot: 'web',
    allow_unsigned_source: uid,
  })
})
