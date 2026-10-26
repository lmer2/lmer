import { beforeEach, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import AnswerBox from '../src/components/AnswerBox.vue'
import { answerRun } from '../src/api.js'
import { deferred, mountPane } from './mount.js'

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

// Issue 617: the answer button is unsendable four ways and used to say none of
// them — on a moved run the required confirmation sat above an unrelated field
// and the operator reported a working gate as a broken button. Every case below
// asserts the reason the operator is given, not just that the control is off.
function answerButton(wrapper) {
  return wrapper.findAll('button').find((button) => button.text() === 'answer')
}

function movedRun(extra = {}) {
  return {
    host: 'git.example', project: 'acme/widget', slug: 'develop-1',
    workspace_id: 'beta', home_revision: 2, confirm_project: true,
    workspace_name: 'Example Beta', attention: { note: 'Proceed?' },
    ...extra,
  }
}

it('says an empty answer is why the button is disabled', async () => {
  const wrapper = mountPane(AnswerBox, { run: movedRun() })

  expect(answerButton(wrapper).attributes('disabled')).toBeDefined()
  expect(wrapper.get('.send-reason').text()).toBe('Type an answer to send.')
  // The control points at the reason it is disabled for.
  expect(answerButton(wrapper).attributes('aria-describedby')).toBe(
    wrapper.get('.send-reason').attributes('id'),
  )
})

it('says a moved run needs its authorization before the answer can be sent', async () => {
  const wrapper = mountPane(AnswerBox, { run: movedRun() })
  await wrapper.get('textarea').setValue('Proceed carefully')

  expect(answerButton(wrapper).attributes('disabled')).toBeDefined()
  expect(wrapper.get('.send-reason').text()).toBe(
    'Tick "Authorize this answer in Example Beta" above to send.',
  )
  // The checkbox is where the reason says it is, and its label is the required
  // step rather than an acknowledgement.
  const checkbox = wrapper.get('input[type="checkbox"]')
  expect(checkbox.element.closest('.v-input').textContent).toContain(
    'Authorize this answer in Example Beta — required to send',
  )
  // And it says, in plain words, why it is there.
  expect(checkbox.element.closest('.v-input').textContent)
    .toContain('This run was moved to Example Beta')

  await checkbox.setValue(true)
  expect(wrapper.find('.send-reason').exists()).toBe(false)
  expect(answerButton(wrapper).attributes('disabled')).toBeUndefined()
})

it('announces a blocked Ctrl+Enter and clears the highlight when the state changes', async () => {
  const wrapper = mountPane(AnswerBox, { run: movedRun() })
  const box = wrapper.get('textarea')
  const announcer = () => wrapper.get('.send-announcer')

  // The chord is refused, and says so to a screen reader — the reason text on
  // screen has not changed, so a colour change alone would announce nothing.
  await box.trigger('keydown', { key: 'Enter', ctrlKey: true })
  await flushPromises()
  expect(announcer().attributes('aria-live')).toBe('polite')
  expect(announcer().text()).toBe('Type an answer to send.')
  expect(wrapper.get('.send-reason').classes()).toContain('send-reason--pressed')

  // The condition behind the reason changed, so neither the highlight nor the
  // announcement outlives it — this new reason was never pressed against.
  await box.setValue('Proceed carefully')
  expect(wrapper.get('.send-reason').text()).toContain('Authorize this answer')
  expect(wrapper.get('.send-reason').classes()).not.toContain('send-reason--pressed')
  expect(announcer().text()).toBe('')

  // A second refusal, of the new reason, is audible too.
  await box.trigger('keydown', { key: 'Enter', ctrlKey: true })
  await flushPromises()
  expect(announcer().text()).toBe(
    'Tick "Authorize this answer in Example Beta" above to send.',
  )

  // And once the reason is gone the chord sends, as it always did.
  await wrapper.get('input[type="checkbox"]').setValue(true)
  await box.trigger('keydown', { key: 'Enter', ctrlKey: true })
  await flushPromises()
  expect(answerRun).toHaveBeenCalledTimes(1)
  expect(announcer().text()).toBe('')
  // The control only points at a reason that is rendered.
  expect(answerButton(wrapper).attributes('aria-describedby')).toBeUndefined()
})

it('says a session is starting while the answer is in flight', async () => {
  const pending = deferred()
  answerRun.mockReturnValueOnce(pending.promise)
  const wrapper = mountPane(AnswerBox, { run: movedRun() })
  await wrapper.get('textarea').setValue('Proceed carefully')
  await wrapper.get('input[type="checkbox"]').setValue(true)

  await answerButton(wrapper).trigger('click')
  expect(wrapper.get('.send-reason').text()).toBe(
    'Sending — a new session is starting for this run.',
  )

  pending.resolve({ session: { session_id: 's-answer' } })
  await flushPromises()
  expect(wrapper.get('.send-reason').text()).toBe(
    'This answer already started s-answer — wait for it to pick the run up.',
  )
})

it('says the answer was already handed off rather than looking ready to send', async () => {
  answerRun.mockResolvedValueOnce({ session: { session_id: 's-answer' } })
  const wrapper = mountPane(AnswerBox, { run: movedRun() })
  await wrapper.get('textarea').setValue('Proceed carefully')
  await wrapper.get('input[type="checkbox"]').setValue(true)
  await answerButton(wrapper).trigger('click')
  await flushPromises()

  expect(answerButton(wrapper).attributes('disabled')).toBeDefined()
  expect(wrapper.get('.send-reason').text()).toContain('already started s-answer')
})

it('explains nothing when there is nothing to explain', async () => {
  const wrapper = mountPane(AnswerBox, { run: movedRun({ confirm_project: false }) })
  await wrapper.get('textarea').setValue('Proceed carefully')

  expect(wrapper.find('.send-reason').exists()).toBe(false)
  expect(answerButton(wrapper).attributes('disabled')).toBeUndefined()
})
