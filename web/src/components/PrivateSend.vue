<script setup>
// Send Privately (issue 510). The text is held in this component's own state
// and nowhere else — not the composer's draft, not browser storage — and it goes
// when the dialog does. Mounted only while open, so a closed dialog holds nothing.
import { onBeforeUnmount, ref, watch } from 'vue'
import { sendPrivately, sendSessionInput, withdrawPrivate } from '../api.js'

const props = defineProps({
  sessionId: { type: String, required: true },
  workspaceId: { type: String, default: undefined },
  // The composer's own pause (an assistant rotation): the session id still
  // names the outgoing generation then, so the id alone cannot tell.
  sendPaused: { type: Boolean, default: false },
})
const emit = defineEmits(['close', 'sent'])
// uploads.MAX_PURPOSE_CHARS; the server refuses a longer one.
const MAX_PURPOSE_CHARS = 80

// The session this editor was opened for. The pane can switch runs, or the
// assistant rotate, underneath an open dialog; nothing is ever delivered anywhere
// but here, and nothing is started once either has happened.
const target = props.sessionId
const text = ref('')
const purpose = ref('')
const busy = ref(false)
const problem = ref(null)
let closed = false

function switched() {
  return props.sessionId !== target
}

function moved() {
  return closed || switched() || props.sendPaused
}

function close(reason = null) {
  closed = true
  text.value = ''
  purpose.value = ''
  emit('close', reason)
}

function withdrawn() {
  const why = switched() ? 'the conversation changed' : 'the assistant began rotating'
  return `Send Privately closed because ${why} — nothing was sent.`
}

function delivered() {
  return switched()
    ? 'Send Privately: it was delivered to the conversation you had open when you pressed send, before the switch.'
    : 'Send Privately: it was delivered to the outgoing assistant as it began rotating, and is deleted when that generation ends — send it again if the new one needs it.'
}

// Mid-send the outcome is not known yet, so the dialog stays and submit()
// reports it once it is.
watch([() => props.sessionId, () => props.sendPaused], () => {
  if (!closed && !busy.value && moved()) close(withdrawn())
})

onBeforeUnmount(() => {
  closed = true
  text.value = ''
})

async function submit() {
  const value = text.value
  if (!value.trim() || busy.value || moved()) return
  busy.value = true
  problem.value = null
  let stored = null
  try {
    stored = await sendPrivately(target, { text: value, purpose: purpose.value.trim(), workspaceId: props.workspaceId })
    if (moved()) throw new Error('the conversation changed before it was delivered')
    // `meta.private` is what history draws as a compact entry instead of this line.
    await sendSessionInput(target, stored.reference, {
      sanitize: true, workspaceId: props.workspaceId, meta: { private: { purpose: stored.purpose ?? null } },
    })
    if (moved()) {
      if (!closed) close(delivered())
      return
    }
    emit('sent', { session: target, reference: stored.reference })
    close()
  } catch (exc) {
    // The file is useless without the reference that names it.
    if (stored) withdrawPrivate(target, stored.name, props.workspaceId).catch(() => {})
    if (moved()) {
      if (!closed) close(withdrawn())
      return
    }
    // The server's refusals never quote the text, and neither does this.
    problem.value = `not sent — ${exc.message}`
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <v-dialog
    :model-value="true"
    max-width="560"
    :persistent="busy"
    @update:model-value="(open) => { if (!open) close() }"
  >
    <v-card class="private-send">
      <v-card-title class="text-title-medium">send privately</v-card-title>
      <v-card-text>
        <p class="text-body-small mb-3">
          For a secret or other private text. The run receives it as a read-only
          file, and the chat shows only that a private message was sent. It is
          deleted when the session ends. The agent is asked to use it in place and never print it,
          because anything it prints is recorded in its transcript.
        </p>
        <v-textarea
          v-model="text"
          :disabled="busy"
          label="private text"
          rows="4"
          auto-grow
          max-rows="12"
          autofocus
          autocomplete="off"
          autocapitalize="off"
          autocorrect="off"
          spellcheck="false"
          hide-details
          @keydown.ctrl.enter.prevent="submit"
          @keydown.meta.enter.prevent="submit"
        />
        <v-text-field
          v-model="purpose"
          :disabled="busy"
          class="private-purpose mt-3"
          label="purpose (optional)"
          :maxlength="MAX_PURPOSE_CHARS"
          persistent-hint
          hint="Not secret: shown in the chat and to the agent, for example “GitLab deploy token”."
          @keydown.enter.prevent="submit"
        />
        <v-alert v-if="problem" type="error" density="compact" class="mt-3" role="alert">
          {{ problem }}
        </v-alert>
      </v-card-text>
      <v-card-actions class="flex-wrap">
        <v-btn variant="text" :disabled="busy" @click="close()">cancel</v-btn>
        <v-spacer />
        <v-btn
          color="primary"
          variant="tonal"
          :loading="busy"
          :disabled="!text.trim()"
          @click="submit"
        >send privately</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
