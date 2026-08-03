<script setup>
// Replying to a question a LIVE session asked through its ask channel (T23).
//
// The other box on this page, AnswerBox, looks similar and does something quite
// different: that run stopped on a question and *exited*, so answering it starts
// a container. This session is running and blocked in a poll on a directory the
// platform mounted into it — the reply is a file, it lands in seconds, and no
// slot is taken. The copy here says so in as many words, because "answer" on two
// cards a screen apart is exactly how an operator starts a container by accident.
//
// Options are a hint, not a menu (spec D27): they are a shortcut past the box, not
// the only way to reply, and anything can be written instead. Tapping one *sends*
// it. Filling the box was the first reading of that hint, and it cost a second tap
// on the one card in this UI where a session is blocked right now — which is what
// the operator asked to lose in a live pass. The price is a mis-tap that goes
// through, and the two things that bound it are both here: the chips are disabled
// while a send is in flight, and answers are not overwritten server-side, so a
// second tap earns a 409 rather than silently replacing a reply the session may
// already have acted on. A send that fails puts the option in the box, which is
// where the retry — or an edit of it — starts from.
//
// The one exception is a box that already holds something: then a tap appends the
// option to the draft and sends nothing. The prompt contract is one question per
// ask entry, but it cannot stop an agent packing three into one, and the reply to
// a packed entry is assembled — "1: <option> 2: yes 3: <option>" — from chips and
// typing together. A chip that fired mid-sentence would send a third of that
// answer and lose the rest, and this card is the only place it exists.
//
// The question itself is markdown (Markdown.vue, shared with the chat): an agent
// asking a real question lays out the alternatives, and verbatim that is a wall of
// asterisks. The *options* are not, and must not be — each one is the literal text
// a tap sends, so a chip that showed anything else would be promising to send
// something the session never receives.

import { computed, defineAsyncComponent, ref } from 'vue'
import { mdiCommentQuestionOutline, mdiSendOutline } from '@mdi/js'
import { answerSessionQuestion } from '../api.js'
import { ago } from '../format.js'

// Deferred into its own chunk, like every other consumer of it — Markdown.vue's
// header says why, and why nothing is drawn until it lands.
const Markdown = defineAsyncComponent(() => import('./Markdown.vue'))

const props = defineProps({
  sessionId: { type: String, required: true },
  // One entry from the ask channel: { id, text, options, at, answered, answer }.
  question: { type: Object, required: true },
  now: { type: Number, default: () => Date.now() },
})

// The fleet poll is what makes the question disappear once it is answered.
const emit = defineEmits(['answered'])

const draft = ref('')
const busy = ref(false)
const error = ref(null)
const sent = ref(false)

const options = computed(() => props.question.options || [])
const canSend = computed(() => !sent.value && !!draft.value.trim() && !busy.value)

// The one send path, whatever put the text in front of it: the box's contents from
// the composer, a chip's own label from an option. One path rather than two,
// because everything around the call is state an operator reads off this card — in
// flight, delivered, refused — and the second copy of it is how a chip ends up
// delivering a reply the card never admits it sent.
async function send(text) {
  const reply = (text || '').trim()
  if (!reply || busy.value || sent.value) return
  busy.value = true
  error.value = null
  try {
    await answerSessionQuestion(props.sessionId, props.question.id, reply)
    sent.value = true
    // On success only — including when a tapped option went out while something
    // else was half-typed. The question is answered: `sent` disables the box and
    // the fleet poll takes the whole card away within seconds, so what is left in
    // it is a draft for nothing, and the reply it was superseded by is on screen.
    draft.value = ''
    emit('answered')
  } catch (exc) {
    // In the server's words, and the reply goes back in the box: it exists nowhere
    // else yet, so dropping it would be the one way to lose it. A tapped option
    // lands there too, because it was never in the box to be kept — the retry is
    // the send control either way, and from the box it can be edited first.
    error.value = exc.message
    draft.value = reply
  } finally {
    busy.value = false
  }
}

// What a tapped option does, which depends on whether the operator is composing:
// an empty box (whitespace is empty) means the option is the whole reply and goes
// as it does from the send control; anything typed means the option is a piece of a
// reply being assembled, so it joins the draft — at the end, because a chip knows
// nothing about the caret — and the chips stay armed for the next piece.
function chooseOption(option) {
  if (sent.value || busy.value) return
  if (!draft.value.trim()) return send(option)
  draft.value += (/\s$/.test(draft.value) ? '' : ' ') + option
}

// What the send control and the chord are bound to. Both hand their handler an
// event, so this takes no argument at all and reads the box itself; the guards it
// used to carry live in `send`, which every path now goes through.
function submit() {
  return send(draft.value)
}
</script>

<template>
  <v-card class="mb-3">
    <v-card-text>
      <div class="d-flex ga-2 align-start">
        <v-icon :icon="mdiCommentQuestionOutline" color="warning" class="mt-1" />
        <div class="flex-1-1 question-body">
          <div class="text-body-small text-medium-emphasis">
            the running session is asking
            <span :title="question.at || ''">· {{ ago(question.at, now) }}</span>
          </div>
          <!-- Rendered, and with no `pre-wrap` of its own: the newlines an agent
               wrote are line breaks in the markup now, and pre-wrap on top of it
               would show the ones *between* block tags as blank lines. The
               renderer also owns wrapping long paths, so nothing is restated. -->
          <Markdown :text="question.text" class="text-body-medium" />
        </div>
      </div>

      <v-alert v-if="question.problem" type="warning" density="compact" class="mt-3">
        {{ question.problem }}
      </v-alert>

      <!-- Verbatim, unlike the question above: a chip's label is exactly the text
           a tap sends — or adds to the draft — and a rendered one would promise
           something else. See the header comment.
           Outlined, which for a chip overrides the variant that was swept out of
           the rest of the app: a tonal chip puts a low-contrast wash behind
           mid-emphasis text, and on a dark card an option is then guessed at rather
           than read — "the chips (questions answers, port links) are quite hard to
           read, lets try variant outlined" (the operator, live). Outlined gives the
           label full-strength accent on the card's own surface, with a border doing
           the work the tint was doing. Chips only: every v-btn stays tonal. -->
      <div v-if="options.length" class="d-flex flex-wrap ga-2 mt-3">
        <v-chip
          v-for="option in options"
          :key="option"
          :disabled="sent || busy"
          color="primary"
          variant="outlined"
          @click="chooseOption(option)"
        >{{ option }}</v-chip>
      </div>
      <p
        v-if="options.length"
        class="text-body-small text-medium-emphasis mt-1 mb-0"
      >
        suggestions — tap one to send it, or to add it to a reply in progress,
        or write anything else
      </p>

      <v-alert v-if="error" type="error" density="compact" class="mt-3">
        {{ error }}
      </v-alert>

      <v-alert v-if="sent" type="success" density="compact" class="mt-3">
        Reply delivered. The session picks it up on its next check, within a few
        seconds, and carries on where it stopped.
      </v-alert>

      <!-- Send is a labelled control on its own row (`.send-row`, style.css),
           reachable by a thumb on any device and by Ctrl+Enter (Cmd+Enter on a
           Mac) from a keyboard; bare Enter stays a newline, because a reply to a
           real question is written in sentences — and on a phone the return key is
           the only way to type one. Chat.vue's composer carries the reasoning for
           all three of those, including why the icon that used to sit in the
           field's corner was not reachable (issue 194). The word is "reply", not
           "answer", because nothing is started here. -->
      <v-textarea
        v-model="draft"
        :disabled="busy || sent"
        label="your reply"
        hint="Tap send below — Enter is a new line, Ctrl+Enter (Cmd on a Mac) sends too. Goes straight to the running session — nothing is started"
        persistent-hint
        rows="2"
        auto-grow
        max-rows="10"
        autocapitalize="sentences"
        class="mt-3"
        @keydown.ctrl.enter.prevent="submit"
        @keydown.meta.enter.prevent="submit"
      />
      <div class="send-row">
        <v-btn
          :prepend-icon="mdiSendOutline"
          :loading="busy"
          :disabled="!canSend"
          color="primary"
          variant="tonal"
          size="large"
          aria-label="send reply"
          @click="submit"
        >send reply</v-btn>
      </div>
    </v-card-text>
  </v-card>
</template>

<style scoped>
/* A flex child will not shrink below its content unless told to, and a long
   question would take the page width with it. Vuetify 4 ships no min-width
   helper, so it is one rule here — the same one AnswerBox needs. */
.question-body {
  min-width: 0;
}
</style>
