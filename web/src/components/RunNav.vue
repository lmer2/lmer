<script setup>
import { computed } from 'vue'
import {
  mdiAccountAlertOutline, mdiRobot, mdiAlertCircleOutline,
  mdiAlertOctagonOutline, mdiCarBrakeParking, mdiChatQuestionOutline,
  mdiCheckCircleOutline, mdiCloseCircleOutline, mdiCommentAlertOutline,
  mdiCommentQuestionOutline, mdiEyeOffOutline, mdiHandBackLeftOutline,
  mdiHelpCircleOutline, mdiHumanQueue, mdiPauseCircleOutline,
  mdiPlayCircleOutline, mdiPowerSleep, mdiRepeatOff, mdiSkullOutline, mdiTimerAlertOutline,
} from '@mdi/js'
import { ago, attentionLabel, stateMeta, toneColor } from '../format.js'
import { runKey } from '../runref.js'
import { assistantName } from '../ratchet.js'
import RatchetFace from './RatchetFace.vue'

const STATE_ICONS = {
  running: mdiPlayCircleOutline,
  stuck_teardown: mdiTimerAlertOutline,
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

const TONE_ICONS = {
  live: mdiPlayCircleOutline,
  attention: mdiAlertCircleOutline,
  bad: mdiAlertOctagonOutline,
  idle: mdiPauseCircleOutline,
  done: mdiCheckCircleOutline,
}

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
  attention: { type: Array, default: () => [] },
  currentKey: { type: String, default: null },
  now: { type: Number, default: () => Date.now() },
  // The scope's Ratchet face, `{ mood, off }`; without `off` the row's own state
  // decides it.
  face: { type: Object, default: null },
})

defineEmits(['open'])

const keyOf = runKey

const sections = computed(() => {
  const urgent = props.attention
  const urgentKeys = new Set(urgent.map(keyOf))
  const calm = props.runs.filter((run) => !urgentKeys.has(keyOf(run)))
  const out = []
  if (urgent.length) out.push({ key: 'attention', runs: urgent })
  if (calm.length) {
    out.push({ key: 'ordinary', runs: calm })
  }
  return out
})

function tone(run) {
  if (run.attention) return run.attention.reason === 'crashed' ? 'bad' : 'attention'
  return stateMeta(run.state).tone
}

function icon(run) {
  if (run.attention) return REASON_ICONS[run.attention.reason] || mdiAlertCircleOutline
  return STATE_ICONS[run.state] || TONE_ICONS[stateMeta(run.state).tone]
}

function line(run) {
  return run.attention ? attentionLabel(run.attention.reason) : stateMeta(run.state).label
}

function isCurrent(run) {
  return keyOf(run) === props.currentKey
}

function isOrchestrator(run) {
  return !!run.orchestrator
}
</script>

<template>
  <v-list density="compact" nav :prepend-gap="12">
    <template v-for="(section, index) in sections" :key="section.key">
      <v-divider v-if="index" class="my-2 run-attention-divider" />
      <v-list-item
        v-for="run in section.runs"
        :key="keyOf(run)"
        :active="isCurrent(run)"
        :aria-current="isCurrent(run) ? 'true' : undefined"
        class="run-row"
        color="primary"
        @click="$emit('open', run)"
      >
        <template #prepend>
          <RatchetFace
            v-if="isOrchestrator(run)"
            :mood="face?.mood || 'idle'"
            :off="face?.off ?? run.state !== 'running'"
            :size="24"
            :title="stateMeta(run.state).label"
          />
          <v-icon
            v-else
            :color="toneColor(tone(run))"
            :icon="icon(run)"
            :title="stateMeta(run.state).label"
          />
        </template>
        <v-list-item-title :title="run.title || run.label">
          {{ run.title || run.label }}
        </v-list-item-title>
        <v-list-item-subtitle>
          <span :class="`text-${toneColor(tone(run))}`">{{ line(run) }}</span>
          <span v-if="run.phase" class="text-medium-emphasis" title="phase">
            · {{ run.phase }}</span>
          <span class="text-medium-emphasis"> · {{ ago(run.updated, now) }}</span>
        </v-list-item-subtitle>
        <template v-if="isOrchestrator(run)" #append>
          <v-chip
            :prepend-icon="mdiRobot"
            size="x-small"
            title="the platform's own orchestrating session, not one of the runs"
            variant="tonal"
          >{{ assistantName }}</v-chip>
        </template>
      </v-list-item>
    </template>

    <v-list-item v-if="!sections.length">
      <v-list-item-subtitle>nothing tracked yet</v-list-item-subtitle>
    </v-list-item>
  </v-list>
</template>

<style scoped>
.run-row {
  --v-list-item-subtitle-opacity: 1;
}

</style>
