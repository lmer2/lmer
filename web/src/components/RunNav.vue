<script setup>
import { computed } from 'vue'
import {
  mdiAccountAlertOutline, mdiRobot, mdiAlertCircleOutline,
  mdiAlertOctagonOutline, mdiCarBrakeParking, mdiChatQuestionOutline,
  mdiCheckCircleOutline, mdiCloseCircleOutline, mdiCommentAlertOutline,
  mdiCommentQuestionOutline, mdiEyeOffOutline, mdiHandBackLeftOutline,
  mdiHelpCircleOutline, mdiHumanQueue, mdiPauseCircleOutline,
  mdiPlayCircleOutline, mdiPowerSleep, mdiRepeatOff, mdiSkullOutline,
} from '@mdi/js'
import { ago, attentionLabel, stateMeta, toneColor } from '../format.js'

// The fleet view's list, shrunk to what fits beside a run: a name, one line of
// news, and the signal. The signal is the part that must not be what gets cut —
// the whole app exists to answer "which run needs me", and a navigator that
// dropped the tone would be a list of names that answers nothing while looking
// perfectly fine.
//
// So the compaction takes the ports, the targets, the ledger, the attention note
// and the buttons, and keeps colour, shape and order.
//
// It does show where the work has got to. The operator asked for it — "id also like
// the listings to show the run phase" — which overrules the earlier reading of this
// row: that a third segment under the name would either push the timestamp out or
// shorten the name further. It is a third segment, and it is subordinate, which is
// the compromise: dimmed like the timestamp beside it, absent on the runs that have
// recorded no phase, and never in front of the state word this row exists to carry.
//
// The committed *status* is still left out, and that half of the argument stands: it
// is the value that disagrees with the derived state on purpose, and a disagreement
// needs the two words near each other to read as one — which is the fleet card's job
// and the detail overview's, not a 40px row's.

// Shape, because colour alone is not a signal. main.js says so outright: with
// the ramp carrying urgency by itself, a red/green-blind operator reads "bad"
// and "live" as the same muddy tone — and it names the fix as belonging in the
// components that draw the rows, which is here. The map is what makes the
// redundant channel real, so two states that mean different things must not
// share an icon: `detached` is deliberately nothing like `running`, because a
// session nothing is recording must never read as a healthy one.
const STATE_ICONS = {
  running: mdiPlayCircleOutline,
  detached: mdiEyeOffOutline,
  held: mdiPauseCircleOutline,
  feedback: mdiCommentAlertOutline,
  waiting_on_you: mdiAccountAlertOutline,
  yielded: mdiHandBackLeftOutline,
  parked: mdiCarBrakeParking,
  failed: mdiCloseCircleOutline,
  crashed: mdiAlertOctagonOutline,
  dormant: mdiPowerSleep,
  complete: mdiCheckCircleOutline,
  unknown: mdiHelpCircleOutline,
}

// A state that reaches format.js before it reaches the map above still has a
// tone, so it still gets a shape. An iconless row is a blank gutter, which reads
// as "nothing here" — the one thing a state this UI does not understand yet must
// not look like. Same argument as toneColor()'s fallback, one channel over.
const TONE_ICONS = {
  live: mdiPlayCircleOutline,
  attention: mdiAlertCircleOutline,
  bad: mdiAlertOctagonOutline,
  idle: mdiPauseCircleOutline,
  done: mdiCheckCircleOutline,
}

// What a run that needs a human is asking for. These win over the state icon,
// because a live question sits on a session that is *running*: taking the icon
// and the colour from the state would put a calm green play button on the row at
// the top of "needs you" and contradict the only thing that row is there to say.
//
// `question` and `live_question` get different shapes for the reason format.js
// gives them different words — same urgency, different consequence.
const REASON_ICONS = {
  question: mdiCommentQuestionOutline,
  live_question: mdiChatQuestionOutline,
  feedback: mdiCommentAlertOutline,
  yield: mdiHandBackLeftOutline,
  critical_error: mdiAlertOctagonOutline,
  crashed: mdiSkullOutline,
  unreadable: mdiHelpCircleOutline,
  cap_reached: mdiRepeatOff,
  slot_contention: mdiHumanQueue,
}

const props = defineProps({
  runs: { type: Array, default: () => [] },
  // The backend's attention list, already in priority order — not recomputed
  // here, so the drawer and the fleet view can never disagree about which run is
  // most urgent.
  attention: { type: Array, default: () => [] },
  currentKey: { type: String, default: null },
  now: { type: Number, default: () => Date.now() },
})

defineEmits(['open'])

// Must stay in step with App.vue's keyOf: the shell decides which run is open by
// this string, and a row that spells it differently is a navigator that never
// marks where you are — silently, because every row still opens fine.
function keyOf(run) {
  return `${run.host}/${run.project}/${run.slug}`
}

// The fleet view's order, kept: runs needing a human first, always. Compact took
// the cards' size away, not their priority.
const sections = computed(() => {
  const urgent = props.attention
  const urgentKeys = new Set(urgent.map(keyOf))
  const calm = props.runs.filter((run) => !urgentKeys.has(keyOf(run)))
  const out = []
  if (urgent.length) out.push({ title: 'needs you', runs: urgent })
  if (calm.length) {
    out.push({ title: urgent.length ? 'everything else' : 'runs', runs: calm })
  }
  return out
})

// The row's urgency, and pointedly not the state's: RunCard draws the same
// distinction down the edge of a fleet card. A crash and a question both need a
// human and are not the same news.
function tone(run) {
  if (run.attention) return run.attention.reason === 'crashed' ? 'bad' : 'attention'
  return stateMeta(run.state).tone
}

function icon(run) {
  if (run.attention) return REASON_ICONS[run.attention.reason] || mdiAlertCircleOutline
  return STATE_ICONS[run.state] || TONE_ICONS[stateMeta(run.state).tone]
}

// The one line under the name, and the news is what goes in it: what a run is
// waiting for while it is waiting, else what state it is in. The state stays
// reachable either way as the icon's tooltip.
function line(run) {
  return run.attention ? attentionLabel(run.attention.reason) : stateMeta(run.state).label
}

function isCurrent(run) {
  return keyOf(run) === props.currentKey
}

// The platform's own session, read once so the leading icon, the badge and nothing
// else are keyed on the same fact — every other row has to render exactly as it did
// before this row got a mark of its own.
//
// The mark is the icon, and it used to be a tinted row: the operator ended that on
// the pass that also took the fleet card's tint away — "i don't think solid
// background for the uber lmer row is the way ... in the side list view instead of
// the play icon show the robot icon". So this row keeps every other row's ground and
// spends the one channel a 40px row has plenty of, which is shape.
//
// What it costs is stated where the swap is made: the glyph is this row's *kind*
// rather than its state, so the state travels in the two channels it has left here —
// the icon's colour and the word under the name.
function isOrchestrator(run) {
  return !!run.orchestrator
}
</script>

<template>
  <v-list density="compact" nav>
    <template v-for="section in sections" :key="section.title">
      <v-list-subheader class="section-title">{{ section.title }}</v-list-subheader>

      <!-- `color` is the brand accent and it means one thing here — "you are here".
           The ramp in main.js was re-separated around the accent precisely so that
           can never be mistaken for "this needs you", and nothing else in this row
           wears it: the badge below is unpainted for the same reason, since the row
           it marks is not the row you are on.
           The row that is not a run at all is marked in the gutter instead, and on
           its own ground: a tint here was the previous pass and the operator ended
           it. -->
      <v-list-item
        v-for="run in section.runs"
        :key="keyOf(run)"
        :active="isCurrent(run)"
        :aria-current="isCurrent(run) ? 'true' : undefined"
        class="run-row"
        color="primary"
        @click="$emit('open', run)"
      >
        <!-- The gutter carries what the row IS on the one row that is not a run, and
             what it NEEDS on every other one (the operator: "in the side list view
             instead of the play icon show the robot icon"). The filled robot is the
             uber lmer mark wherever this app means the supervising session — the
             outline variant belongs to the driver chip, which is a harness and not
             this — so the drawer marks it with the same glyph the fleet card badges
             it with.
             What the swap spends is this row's shape channel, which everywhere else
             in this file is urgency: `running` loses its play glyph here. The two
             channels left keep it readable — the icon is still painted from the
             row's tone, the word under the name is still what the run needs, and the
             state stays in the tooltip either way. That is a trade one row in a
             listing can make and no other row may: the fleet cannot be scanned for
             urgency by shape if the shapes stop meaning it. -->
        <template #prepend>
          <v-icon
            :color="toneColor(tone(run))"
            :icon="isOrchestrator(run) ? mdiRobot : icon(run)"
            :title="stateMeta(run.state).label"
          />
        </template>

        <!-- The same name the fleet card shows, for the same reason: the title is
             what an operator recognises a run by, and the two listings disagreeing
             about what a run is called would be a drawer you cannot match against
             the view beside it. Plain text, never rendered — one line of it, and a
             40px row is not a document.
             The tooltip carries the whole string because this row is the one place
             it is *truncated*: Vuetify ellipsises a list title, which is the right
             answer in a 300px drawer and the wrong one to leave with no way to
             read the rest. -->
        <v-list-item-title :title="run.title || run.label">
          {{ run.title || run.label }}
        </v-list-item-title>
        <!-- The news, then where the work has got to, then when it last moved. The
             phase is dimmed and second: it is a fact about the run, not the signal,
             and the signal is the only thing in this row that must never be cut.
             The separator is inside the same element as the value, so a run with no
             phase renders no stray "· " (absent renders nothing, here as
             everywhere). -->
        <v-list-item-subtitle>
          <span :class="`text-${toneColor(tone(run))}`">{{ line(run) }}</span>
          <span v-if="run.phase" class="text-medium-emphasis" title="phase">
            · {{ run.phase }}</span>
          <span class="text-medium-emphasis"> · {{ ago(run.updated, now) }}</span>
        </v-list-item-subtitle>

        <!-- The platform's own session, marked in the drawer for the reason the
             fleet card marks it: the two listings must agree about which row is
             the orchestrator, and this is the listing that is on screen while a
             run is open. In the append slot rather than in the title, because the
             title is the one thing here that gets ellipsised and a badge inside
             it would eat the name. The slot itself is conditional — an empty
             append pads every other row for nothing.
             Unpainted like the fleet card's badge: the marking is the glyph in the
             gutter, and a coloured chip beside it would be a second accent saying
             the same thing. -->
        <template v-if="isOrchestrator(run)" #append>
          <v-chip
            :prepend-icon="mdiRobot"
            size="x-small"
            title="the platform's own orchestrating session, not one of the runs"
            variant="tonal"
          >uber lmer</v-chip>
        </template>
      </v-list-item>
    </template>

    <v-list-item v-if="!runs.length">
      <v-list-item-subtitle>nothing tracked yet</v-list-item-subtitle>
    </v-list-item>
  </v-list>
</template>

<style scoped>
/* Vuetify renders a subtitle at medium emphasis, and the state word in it is the
   signal — dimming it is the quiet half of losing it. The framework exposes the
   opacity as a variable for exactly this, so the row opts out and the timestamp
   beside the word dims itself instead. */
.run-row {
  --v-list-item-subtitle-opacity: 1;
}

/* No rule for the row that is not a run: it had a ground of its own for one pass and
   the operator took it away, so the drawer marks it with the robot in the gutter
   (see the prepend slot) and every row in this list is drawn on the same surface. */

/* The same heading role as the fleet view (style.css), at drawer scale: the
   page's vertical rhythm would put an 18px gap between 40px rows, and the
   subheader already renders dimmed, so a second helping of medium emphasis is
   just harder to read. */
.section-title {
  margin: 6px 0 0;
  opacity: 1;
}
</style>
