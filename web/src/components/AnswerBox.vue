<script setup>
// Answering a run that stopped to ask a question (T19) — the one thing the fleet
// view could show but not do.
//
// This is not a chat composer, and the copy has to keep saying so. The session
// that asked has exited, so answering *starts a new one*: the daemon respawns the
// run with the answer attached and the fresh session applies it at its own start
// (see lmer_platform.answer). An operator who reads this as "send a message" is
// surprised by a container starting, which on a host with a concurrency cap is a
// consequence worth stating before the tap, not after.
//
// One fact belongs to the server and is only rendered here: **the question**. The
// fleet payload carries it as the attention note, which is either what the run
// asked or the sentence saying it never recorded one. NO_QUESTION_NOTE is that
// sentence — the one string duplicated from the backend
// (lmer_platform.inventory._derive), pinned by a test in
// tests/test_platform_answer.py so the two cannot drift apart silently.
//
// A run that recorded no question text is answerable like any other (T24): the
// session applies the answer to the question *stop*. So the missing text changes
// what there is to read above the box, and nothing else — it is context for
// whoever writes the answer, not a reason to withhold the field.
//
// The question is rendered, exactly as the live session's is one component over
// (AskBox.vue): the same agent wrote both, laying out alternatives and quoting
// commands, and verbatim it is a wall of punctuation. Fully rendered rather than
// in the compact mode (T46) — this is a card written to be read, not a row, so a
// list of alternatives should arrive as a list. What you type in reply is never
// rendered anywhere, for the reason Markdown.vue's header gives.

import { computed, defineAsyncComponent, ref } from 'vue'
import { mdiCommentQuestionOutline, mdiReplyOutline } from '@mdi/js'
import { answerRun } from '../api.js'

// Deferred into its own chunk, like every other consumer of it — Markdown.vue's
// header says why, and why nothing is drawn until it lands.
const Markdown = defineAsyncComponent(() => import('./Markdown.vue'))

const props = defineProps({
  run: { type: Object, required: true },
})

// The fleet view is what has to change after an answer — the run goes from
// "waiting on you" to "running", with a new session behind it.
const emit = defineEmits(['answered'])

// Kept verbatim from lmer_platform.inventory: the note for a question stop with
// no recorded text.
const NO_QUESTION_NOTE = 'question text was not recorded — open the run to see'

const draft = ref('')
const busy = ref(false)
const error = ref(null)
const startedSession = ref(null)

const note = computed(() => props.run.attention?.note || '')
const questionRecorded = computed(
  () => !!note.value && note.value !== NO_QUESTION_NOTE,
)

// Answered once is answered: the fleet poll replaces this run with a running one
// within seconds and the box goes away with it, and a second answer in the gap
// would only earn the daemon's "already has a live session" refusal.
const handedOff = computed(() => !!startedSession.value)
const canSend = computed(
  () => !handedOff.value && !!draft.value.trim() && !busy.value,
)

async function submit() {
  if (!canSend.value) return
  busy.value = true
  error.value = null
  try {
    const result = await answerRun(props.run, draft.value.trim())
    startedSession.value = result.session?.session_id || 'a new session'
    draft.value = ''
    emit('answered')
  } catch (exc) {
    // Loud, and in the server's own words: every refusal names both the reason and
    // the way through, and paraphrasing them here would lose that.
    //
    // The draft is deliberately *not* cleared on this path. The platform records
    // nothing before the spawn (spec D3 — the session is what records the answer),
    // so a failed respawn means the answer exists nowhere but this box; clearing it
    // would be the one way for an operator to actually lose what they typed.
    error.value = exc.message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <v-card class="mb-3">
    <v-card-text>
      <div class="d-flex ga-2 align-start">
        <v-icon :icon="mdiCommentQuestionOutline" color="warning" class="mt-1" />
        <div class="flex-1-1 question-body">
          <div class="text-body-small text-medium-emphasis">it asked you</div>
          <!-- Rendered, and with no `pre-wrap` of its own: the newlines the agent
               wrote are line breaks in the markup now, and pre-wrap on top of it
               would show the ones *between* block tags as blank lines. The
               renderer also owns wrapping a long path, so nothing is restated. -->
          <Markdown
            v-if="questionRecorded"
            :text="note"
            class="text-body-medium question"
          />
          <p v-else class="text-body-medium text-medium-emphasis mb-0">
            No question text was recorded — only that the run stopped to ask one.
          </p>
        </div>
      </div>

      <!-- Context, not a refusal: the answer is applied to the question stop, so
           it lands whether or not the text was saved. What the operator loses is
           the question itself, and the conversation below is where to recover it
           before writing an answer to something they cannot read here. -->
      <v-alert v-if="!questionRecorded" type="warning" density="compact" class="mt-3">
        The asking session did not save the question's text, so there is nothing to
        show above. Your answer still reaches the run — it is applied to the
        question stop itself. Read the conversation below to see what it asked
        before you answer.
      </v-alert>

      <v-alert v-if="error" type="error" density="compact" class="mt-3">
        {{ error }}
      </v-alert>

      <v-alert v-if="startedSession" type="success" density="compact" class="mt-3">
        Answer handed to {{ startedSession }}. It records the answer and clears the
        question at session start, so this run stays listed as waiting for another
        moment.
      </v-alert>

      <!-- Send is inside the field, reachable by the icon on any pointer and by
           Ctrl+Enter (Cmd+Enter on a Mac) from a keyboard; bare Enter stays a
           newline, because this is a box someone writes a paragraph in. Chat.vue's
           composer carries the reasoning for all three of those.
           What the icon cannot carry is this box's one distinguishing fact — that
           sending it starts a container — so the sentence that used to sit beside
           the button stays, and the button's label survives as the icon's
           accessible name. -->
      <v-textarea
        v-model="draft"
        :disabled="busy || handedOff"
        label="your answer"
        hint="Ctrl+Enter (Cmd on a Mac) sends, Enter is a new line. Goes to a new session for this run — the one that asked has exited"
        persistent-hint
        rows="3"
        auto-grow
        max-rows="10"
        autocapitalize="sentences"
        class="mt-3"
        @keydown.ctrl.enter.prevent="submit"
        @keydown.meta.enter.prevent="submit"
      >
        <template #append-inner>
          <v-btn
            :icon="mdiReplyOutline"
            :loading="busy"
            :disabled="!canSend"
            color="primary"
            variant="tonal"
            size="small"
            aria-label="answer and start a session"
            @click="submit"
          />
        </template>
      </v-textarea>
      <p class="text-body-small text-medium-emphasis mt-2 mb-0">
        this starts a new session for the run
      </p>
    </v-card-text>
  </v-card>
</template>

<style scoped>
/* A flex child refuses to shrink below its content unless told to, and taking the
   page width with it is exactly what a long question would do. Vuetify 4 ships no
   min-width helper, so it is one rule here rather than a class in style.css that
   only this view uses. */
.question-body {
  min-width: 0;
}

/* The rendered question keeps the spacing the plain paragraph had. Nothing about
   wrapping is restated: the renderer owns that for the markup it injects, which a
   rule here could not reach anyway, and a `pre-wrap` on rendered markup would show
   the newlines between block tags as blank lines. */
.question {
  margin-bottom: 0;
}
</style>
