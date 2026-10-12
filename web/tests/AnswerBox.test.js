import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import AnswerBox from '../src/components/AnswerBox.vue'
import { answerRun } from '../src/api.js'
import { mountPane } from './mount.js'

vi.mock('../src/api.js', () => ({ answerRun: vi.fn() }))

beforeEach(() => vi.clearAllMocks())

it('binds fresh answer continuation approval to the question source UID', async () => {
  const uid = 'a'.repeat(32)
  const refusal = Object.assign(new Error('source has no final sign-out'), {
    code: 'unsigned_session', source: { uid, operator: 'alice', hostname: 'build.example' },
  })
  answerRun.mockRejectedValueOnce(refusal).mockResolvedValueOnce({
    session: { session_id: 's-answer' },
  })
  const wrapper = mountPane(AnswerBox, { run: {
    host: 'git.example', project: 'acme/widget', slug: 'develop-1',
    attention: { note: 'Proceed?' },
  } })
  await wrapper.get('textarea').setValue('Proceed carefully')
  await wrapper.findAll('button').find((button) => button.text() === 'answer').trigger('click')
  await flushPromises()

  expect(wrapper.text()).toContain('source has no final sign-out')
  await wrapper.findAll('button').find(
    (button) => button.text().includes('continue fresh'),
  ).trigger('click')
  await flushPromises()

  expect(answerRun).toHaveBeenLastCalledWith(
    expect.any(Object), 'Proceed carefully',
    { confirmProject: false, slot: null, allowUnsignedSource: uid },
  )
})

it('offers a repository URL and resends when the daemon asks for one', async () => {
  const refusal = Object.assign(new Error('this run records no repository'), {
    code: 'repo_url_required',
  })
  answerRun.mockRejectedValueOnce(refusal).mockResolvedValueOnce({
    session: { session_id: 's-answer' },
  })
  const wrapper = mountPane(AnswerBox, { run: {
    host: 'git.example', project: 'acme/widget', slug: 'develop-1',
    attention: { note: 'Proceed?' },
  } })
  await wrapper.get('textarea').setValue('Proceed carefully')
  await wrapper.findAll('button').find((button) => button.text() === 'answer').trigger('click')
  await flushPromises()

  expect(wrapper.text()).toContain('this run records no repository')
  const field = wrapper.findAll('input').find(
    (input) => input.attributes('placeholder') === 'https://gitlab.example.com/group/project.git',
  )
  await field.setValue('https://git.example/acme/widget.git')
  await wrapper.findAll('button').find(
    (button) => button.text().includes('answer with this repository'),
  ).trigger('click')
  await flushPromises()

  expect(answerRun).toHaveBeenLastCalledWith(
    expect.any(Object), 'Proceed carefully',
    { confirmProject: false, slot: null, repoUrl: 'https://git.example/acme/widget.git' },
  )
})

it('carries the repository URL through unsigned-source confirmation', async () => {
  const uid = 'b'.repeat(32)
  const repoRefusal = Object.assign(new Error('this run records no repository'), {
    code: 'repo_url_required',
  })
  const unsignedRefusal = Object.assign(new Error('source has no final sign-out'), {
    code: 'unsigned_session',
    source: { uid, operator: 'alice', hostname: 'build.example' },
  })
  answerRun
    .mockRejectedValueOnce(repoRefusal)
    .mockRejectedValueOnce(unsignedRefusal)
    .mockResolvedValueOnce({ session: { session_id: 's-answer' } })
  const wrapper = mountPane(AnswerBox, { run: {
    host: 'git.example', project: 'acme/widget', slug: 'develop-1',
    attention: { note: 'Proceed?' },
  } })
  await wrapper.get('textarea').setValue('Proceed carefully')
  await wrapper.findAll('button').find((button) => button.text() === 'answer').trigger('click')
  await flushPromises()

  // repo_url_required: supply the transport URL and resend.
  const field = wrapper.findAll('input').find(
    (input) => input.attributes('placeholder') === 'https://gitlab.example.com/group/project.git',
  )
  await field.setValue('https://git.example/acme/widget.git')
  await wrapper.findAll('button').find(
    (button) => button.text().includes('answer with this repository'),
  ).trigger('click')
  await flushPromises()

  // unsigned_session: the URL entered above must survive "continue fresh".
  expect(wrapper.text()).toContain('source has no final sign-out')
  await wrapper.findAll('button').find(
    (button) => button.text().includes('continue fresh'),
  ).trigger('click')
  await flushPromises()

  expect(answerRun).toHaveBeenLastCalledWith(
    expect.any(Object), 'Proceed carefully',
    {
      confirmProject: false, slot: null,
      repoUrl: 'https://git.example/acme/widget.git',
      allowUnsignedSource: uid,
    },
  )
  expect(wrapper.text()).toContain('Answer handed to s-answer')
})

it('sends the project fields and the selected slot beside the answer', async () => {
  answerRun.mockResolvedValueOnce({ session: { session_id: 's-answer' } })
  const wrapper = mountPane(AnswerBox, {
    slots: [{ name: 'web', applicable_projects: ['beta'] }],
    run: {
      host: 'git.example', project: 'acme/widget', slug: 'develop-1',
      workspace_id: 'beta', home_revision: 2, confirm_project: true,
      workspace_name: 'Example Beta',
      attention: { note: 'Proceed?' },
    },
  })
  await wrapper.get('textarea').setValue('Proceed carefully')
  await wrapper.get('input[type="checkbox"]').setValue(true)
  await wrapper.findComponent({ name: 'VSelect' }).setValue('web')
  await wrapper.findAll('button').find((button) => button.text() === 'answer').trigger('click')
  await flushPromises()

  expect(answerRun).toHaveBeenLastCalledWith(
    expect.objectContaining({ workspace_id: 'beta', home_revision: 2 }),
    'Proceed carefully',
    { confirmProject: true, slot: 'web' },
  )
})

it('cancels an unsigned answer without losing the draft or retrying', async () => {
  const refusal = Object.assign(new Error('source has no final sign-out'), {
    code: 'unsigned_session', source: { uid: 'a'.repeat(32) },
  })
  answerRun.mockRejectedValueOnce(refusal)
  const wrapper = mountPane(AnswerBox, { run: {
    host: 'git.example', project: 'acme/widget', slug: 'develop-1',
    attention: { note: 'Proceed?' },
  } })
  await wrapper.get('textarea').setValue('Wait for the other session')
  await wrapper.findAll('button').find((button) => button.text() === 'answer').trigger('click')
  await flushPromises()

  await wrapper.findAll('button').find(
    (button) => button.text().includes('cancel and wait'),
  ).trigger('click')
  await flushPromises()

  expect(answerRun).toHaveBeenCalledTimes(1)
  expect(wrapper.get('textarea').element.value).toBe('Wait for the other session')
  expect(wrapper.text()).not.toContain('source has no final sign-out')
})

// Spec §6.1: a schema-2 row is keyed by its uid, so the row itself — uid and all
// — is what reaches answerRun. The component narrows nothing; api.js decides
// what goes on the wire.
it('answers a schema-2 run with the row that carries its uid', async () => {
  const uid = '0123456789abcdef0123456789abcdef'
  answerRun.mockResolvedValueOnce({ session: { session_id: 's-answer' } })
  const wrapper = mountPane(AnswerBox, { run: {
    host: 'git.example', project: 'acme/widget', slug: 'develop-1', uid,
    attention: { note: 'Proceed?' },
  } })
  await wrapper.get('textarea').setValue('Proceed carefully')
  await wrapper.findAll('button').find((button) => button.text() === 'answer').trigger('click')
  await flushPromises()

  expect(answerRun).toHaveBeenLastCalledWith(
    expect.objectContaining({ host: 'git.example', project: 'acme/widget', slug: 'develop-1', uid }),
    'Proceed carefully',
    { confirmProject: false, slot: null },
  )
})

it('shows the concurrency warning an accepted answer carries', async () => {
  const warning = 'Another session claimed this run while the answer was being started.'
  answerRun.mockResolvedValueOnce({ session: { session_id: 's-answer' }, warning })
  const wrapper = mountPane(AnswerBox, { run: {
    host: 'git.example', project: 'acme/widget', slug: 'develop-1',
    uid: 'f'.repeat(32), attention: { note: 'Proceed?' },
  } })
  await wrapper.get('textarea').setValue('Proceed carefully')
  await wrapper.findAll('button').find((button) => button.text() === 'answer').trigger('click')
  await flushPromises()

  expect(wrapper.text()).toContain('Answer handed to s-answer')
  const alert = wrapper.findAll('.v-alert').find((node) => node.text().includes(warning))
  expect(alert).toBeTruthy()
  expect(alert.classes()).toContain('bg-warning')
})

it('shows no warning when the answer reply carries none', async () => {
  answerRun.mockResolvedValueOnce({ session: { session_id: 's-answer' } })
  const wrapper = mountPane(AnswerBox, { run: {
    host: 'git.example', project: 'acme/widget', slug: 'develop-1',
    attention: { note: 'Proceed?' },
  } })
  await wrapper.get('textarea').setValue('Proceed carefully')
  await wrapper.findAll('button').find((button) => button.text() === 'answer').trigger('click')
  await flushPromises()

  expect(wrapper.findAll('.v-alert').map((node) => node.classes())).not.toContainEqual(
    expect.arrayContaining(['bg-warning']),
  )
})
