import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { fetchAssistantConfig } from './api.js'

// How many notes every run's operator channel dock keeps beside its open
// questions (issue 465): null keeps them all, 0 none. The operator chat tab is the
// full record and never reads it.
export const dockKeepLastNotes = ref(null)
// Whether the uber lmer pane draws the harness transcript beside the
// conversation (issue 488). Platform-side like `dockKeepLastNotes`, so it survives
// browsers; only an explicit `true` turns it on.
export const chatShowTranscript = ref(false)
// Whether the operator's bubbles take the project's hue (issue 646). On unless
// the platform says an explicit `false`.
export const chatTintOperator = ref(true)
export const chatConfigProblem = ref(null)
const readEntries = new Map()
let configGeneration = 0

export function applyChatSettings(config) {
  configGeneration += 1
  const value = config.dock?.keep_last_notes?.value ?? null
  dockKeepLastNotes.value = Number.isSafeInteger(value) && value >= 0 ? value : null
  chatShowTranscript.value = config.chat?.show_transcript?.value === true
  chatTintOperator.value = config.chat?.tint_operator?.value !== false
  chatConfigProblem.value = null
}

export async function refreshChatSettings() {
  const mine = ++configGeneration
  try {
    const config = await fetchAssistantConfig()
    if (mine === configGeneration) applyChatSettings(config)
  } catch (exc) {
    if (mine === configGeneration) chatConfigProblem.value = exc.message
  }
}

const identity = (entry) => `${entry.id}:${entry.answer?.answered_at || ''}`

export function useOperatorChat(sessionId, entries, active) {
  const pageVisible = ref(document.visibilityState !== 'hidden')
  const revision = ref(0)
  const count = computed(() => entries.value.length)
  const unread = computed(() => {
    revision.value
    const seen = readEntries.get(sessionId.value) || new Set()
    return entries.value.some((entry) => !seen.has(identity(entry)))
  })

  function markRead() {
    if (!active.value || !pageVisible.value || !sessionId.value) return
    readEntries.set(sessionId.value, new Set(entries.value.map(identity)))
    revision.value += 1
  }

  function visibilityChanged() {
    pageVisible.value = document.visibilityState !== 'hidden'
    if (pageVisible.value) refreshChatSettings()
  }

  watch([sessionId, entries, active, pageVisible], markRead, { immediate: true })
  onMounted(() => {
    refreshChatSettings()
    document.addEventListener('visibilitychange', visibilityChanged)
  })
  onBeforeUnmount(() => document.removeEventListener('visibilitychange', visibilityChanged))
  return { count, unread }
}
