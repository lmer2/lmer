<script setup>
// One entry of a session's ask channel, as a card (#254, #274).
//
// The two views of the channel render the same entries: the dock (AskChannel.vue)
// answers "is anything waiting on me", the record (AskHistory.vue) answers "what
// was said". What one entry *looks like* is the thing they owe each other — a
// channel that separated its messages in one view and ran them together in the
// other, or that painted the operator's own words in two inks, would read as two
// accounts of who said what. Both files used to carry this markup and its
// stylesheet verbatim, each with a comment saying the other must not disagree with
// it; a rule kept by copying is a rule nothing enforces. So it is kept here.
//
// A card of its own, which is what #254 was. The operator, on the list an entry
// used to be a div in: "messages look like the kinda flow into each other". They
// were separated by a margin and nothing else, so a note followed by a question
// read as one long thing with a dim line in the middle of it. Outlined rather than
// elevated or tonal: whichever view this is drawn in is already a card and carries
// the shadow, so an entry needs an edge rather than a second one — and a wash under
// half a dozen of them would repaint the pane. (The argument is here rather than
// over the element because a comment before the root would make this component's
// root a fragment in a development build, which is a different component from the
// one that ships.)
//
// Presentation only: nothing here fetches, filters or emits. What the two views
// legitimately disagree about is which entries they show and what may be done with
// one — the dock may stop showing an entry and may offer a reply box, the record
// may do neither — and that stays with them.
//
// The few things one view says about an entry and the other does not are the props
// below, each argued where it is declared, rather than one prop naming the view.
// "The record" is not a rendering decision, and a component that took one would be
// two presentations again, one v-if apart, with nothing saying why they differ.
//
// What the agent wrote is rendered, what you replied is not — the same split the
// conversation view makes, for the same two reasons. What is posted on the channel
// is written like a message to a person (options as a list, a command in backticks)
// and shown verbatim it is a wall of punctuation; a reply of yours went to the
// session as bytes, and a line that quietly ate a pair of asterisks would be
// misreporting what it got. Markdown.vue is the renderer, shared with the chat,
// and it is one component for a reason its header explains.

import { defineAsyncComponent } from 'vue'
import { mdiCommentQuestionOutline, mdiInformationOutline } from '@mdi/js'
import { ago, askEntryLabel, askPartColor } from '../format.js'

// Deferred into its own chunk, like every other consumer of it — Markdown.vue's
// header says why, and why nothing is drawn until it lands.
const Markdown = defineAsyncComponent(() => import('./Markdown.vue'))

defineProps({
  // One entry as the channel listing carried it (ask_channel/protocol.py): a note
  // or a question, with whatever answer, closure and timestamps it has.
  entry: { type: Object, required: true },
  // Whether the session is still running. Read for the header word only, which is
  // what askEntryLabel needs it for: a question nobody has answered is "waiting"
  // while there is somebody to answer it and something else once the session has
  // gone. Nothing on this card is answerable either way.
  live: { type: Boolean, default: false },
  now: { type: Number, default: () => Date.now() },
  // Whether a question carries an icon of its own. The record names the kind of
  // every entry it lists, because it is read top to bottom as a transcript; the
  // dock draws one on a note alone, where the icon is saying "this one is not
  // waiting on you" among entries that mostly are.
  questionIcon: { type: Boolean, default: false },
  // The alternatives a question offered, kept where the record keeps everything
  // that was asked. Verbatim, and pointedly not as anything tappable: in the dock
  // a chip is literally the text it puts in the reply box, and one here would
  // promise to send something on a view that sends nothing.
  showOptions: { type: Boolean, default: false },
  // The platform's own sentence about an entry it could not read whole. Carried by
  // the protocol rather than raised, so one unreadable pair cannot empty the
  // record — which is also the only view that owes it: the dock shows what is
  // waiting and what has just happened, and a half-read entry is neither.
  showProblem: { type: Boolean, default: false },
  // Whether the closure line ends by saying a reply can no longer be delivered.
  // The dock is where a reply is typed, so a question the session stopped waiting
  // for has to say why there is no box under it; the record offers no box for any
  // entry, and the sentence there would explain the absence of something it never
  // had.
  unreachable: { type: Boolean, default: false },
})

// The options a question offered, as one line. Empty when it offered none, which
// is what decides whether the line is drawn at all.
function options(entry) {
  return (entry.options || []).join(' · ')
}

// The reason the agent gave for closing a question, if it gave one — rendered
// inline inside the sentence below rather than shown alone, because on its own a
// clause like "took the safe branch" reads as something the operator did.
function closureReason(entry) {
  return entry.closure?.reason || ''
}
</script>

<template>
  <v-card class="entry" variant="outlined">
    <v-card-text class="py-2">
      <div class="d-flex ga-2 align-center text-body-small text-medium-emphasis">
        <v-icon
          v-if="entry.kind === 'note' || questionIcon"
          :icon="entry.kind === 'note' ? mdiInformationOutline : mdiCommentQuestionOutline"
          size="small"
        />
        <span>{{ askEntryLabel(entry, live) }}</span>
        <span :title="entry.at || ''">{{ ago(entry.at, now) }}</span>
      </div>

      <!-- Which half of the exchange starts here, in a word, coloured from
           format.js so no view of the channel can disagree with another about it.
           Not on a note: a note is one voice saying one thing, and labelling it
           "QUESTION" would promise an answer that is never coming. -->
      <div
        v-if="entry.kind !== 'note'"
        class="part"
        :class="`text-${askPartColor('question')}`"
      >QUESTION</div>

      <Markdown :text="entry.text" class="text-body-medium said" />

      <p
        v-if="showOptions && options(entry)"
        class="text-body-small text-medium-emphasis said plain"
      >it offered: {{ options(entry) }}</p>

      <!-- The label leans with the words it labels, so the reply reads as one
           block on its own side rather than as a heading over a stray line. "you"
           stays on the line under it: the label says which half of the exchange
           this is, and that is not the same fact as whose words they are. -->
      <template v-if="entry.answer">
        <div
          class="part reply"
          :class="`text-${askPartColor('answer')}`"
        >ANSWER</div>
        <p class="text-body-medium said plain reply">
          you: {{ entry.answer.text }}
        </p>
      </template>

      <!-- Only when there is no answer: one that raced the close is the answer to
           show, and saying "stopped waiting" under it would read as if the reply
           had been thrown away. The agent's own reason is rendered inline — it is
           prose, and this is a line. -->
      <p
        v-if="entry.closed && !entry.answered"
        class="text-body-small text-medium-emphasis mb-0"
      >
        The session stopped waiting for this<template
          v-if="closureReason(entry)"
        >: <Markdown
          :text="closureReason(entry)"
          inline
        /></template>.<template
          v-if="unreachable"
        > A reply can no longer reach it.</template>
      </p>

      <!-- Shown as it was written: it is the platform's sentence, not the
           agent's. -->
      <p
        v-if="showProblem && entry.problem"
        class="text-body-small text-medium-emphasis mb-0"
      >{{ entry.problem }}</p>
    </v-card-text>
  </v-card>
</template>

<style scoped>
.entry {
  margin-bottom: 12px;
  min-width: 0;
}

/* The word that names a half of the exchange (#254). Small, spaced and heavier than
   the body, so it reads as a label rather than as the first line of the message —
   which is also why it is the label that is coloured and the message never is: the
   operator asked for exactly that ("i don't think color code the text entirely").
   The colour itself comes from format.js, because the words are the same words
   wherever an entry is drawn, and this rule is the one place they are drawn. */
.part {
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: 0.09em;
  margin-top: 6px;
}

/* Every part of an entry sits in the same column with the same spacing; the class
   reaches the rendered one because a child component's root element carries its
   parent's scope attribute too. `anywhere` so a path or URL in a note does not
   scroll the page sideways on a phone (Markdown.vue restates it for the text it
   injects, which this rule cannot reach). */
.said {
  overflow-wrap: anywhere;
  margin: 2px 0 4px;
}

/* Only the verbatim parts need this: what you replied carries the newlines you
   typed, and collapsing them would run a reply written as a list into one
   paragraph. On rendered markup it would instead show the newlines *between* block
   tags as blank lines. Same class name as the conversation view, same meaning. */
.said.plain {
  white-space: pre-wrap;
}

/* The reply leans right — the label above it with it — so "you" and the agent read
   as two sides here the way they do in the conversation. It is the text that leans
   because there is still no bubble to lean: an entry's card holds both halves of one
   exchange rather than one turn each. That is also the whole of what makes it
   acceptable — a reply here is one short line, while the conversation's own turns
   lean as containers and keep their words left-aligned, because justified prose is
   what the operator has to read back. */
.reply {
  text-align: end;
}
</style>
