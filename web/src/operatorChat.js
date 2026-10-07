import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { fetchAssistantConfig } from './api.js'

export const chatKeepLast = ref(0)
// Whether the uber lmer pane draws the harness transcript beside the
// conversation (issue 488). Platform-side like `chatKeepLast`, so it survives
// browsers; only an explicit `true` turns it on.
export const chatShowTranscript = ref(false)
export const chatConfigProblem = ref(null)
const readEntries = new Map()
let configGeneration = 0

export function applyChatSettings(config) {
  configGeneration += 1
  const value = config.chat?.keep_last?.value ?? 0
  chatKeepLast.value = Number.isSafeInteger(value) && value >= 0 ? value : 0
  chatShowTranscript.value = config.chat?.show_transcript?.value === true
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

export function retainedEntries(entries, limit = chatKeepLast.value) {
  return limit > 0 ? entries.slice(-limit) : entries
}

const identity = (entry) => `${entry.id}:${entry.answer?.answered_at || ''}`

export function useOperatorChat(sessionId, entries, active) {
  const pageVisible = ref(document.visibilityState !== 'hidden')
  const revision = ref(0)
  const shown = computed(() => retainedEntries(entries.value))
  const count = computed(() => shown.value.length)
  const unread = computed(() => {
    revision.value
    const seen = readEntries.get(sessionId.value) || new Set()
    return shown.value.some((entry) => !seen.has(identity(entry)))
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

  watch([sessionId, entries, active, pageVisible, chatKeepLast], markRead, { immediate: true })
  onMounted(() => {
    refreshChatSettings()
    document.addEventListener('visibilitychange', visibilityChanged)
  })
  onBeforeUnmount(() => document.removeEventListener('visibilitychange', visibilityChanged))
  return { count, unread }
}
