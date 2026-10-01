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
    expect.any(Object), 'Proceed carefully', { allowUnsignedSource: uid },
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
