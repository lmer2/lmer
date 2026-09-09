<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { mdiCheck, mdiContentCopy, mdiAlertCircleOutline } from '@mdi/js'

const props = defineProps({
  text: { type: String, required: true },
  label: { type: String, default: 'Copy message' },
})
const status = ref('')
const busy = ref(false)
const caption = computed(() => status.value || props.label)
const icon = computed(() => status.value === 'Copied' ? mdiCheck
  : status.value ? mdiAlertCircleOutline : mdiContentCopy)
let timer
let generation = 0

function reset() {
  generation += 1
  clearTimeout(timer)
  status.value = ''
  busy.value = false
}

async function copy() {
  if (busy.value) return
  const mine = ++generation
  clearTimeout(timer)
  status.value = ''
  busy.value = true
  try {
    await navigator.clipboard.writeText(props.text)
    if (mine !== generation) return
    status.value = 'Copied'
  } catch {
    if (mine !== generation) return
    status.value = 'Copy failed'
  } finally {
    if (mine === generation) {
      busy.value = false
      timer = setTimeout(() => { status.value = '' }, 1500)
    }
  }
}

watch(() => props.text, reset)
onBeforeUnmount(reset)
</script>

<template>
  <button
    type="button"
    class="copy-button"
    :title="caption"
    :aria-label="caption"
    :disabled="busy"
    @click.stop="copy"
  >
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
      <path :d="icon" fill="currentColor" />
    </svg>
    <span class="copy-status" role="status">{{ status }}</span>
  </button>
</template>

<style scoped>
.copy-button {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  min-width: 32px;
  min-height: 32px;
  border: 0;
  border-radius: 4px;
  color: inherit;
  background: rgb(var(--v-theme-surface));
  cursor: pointer;
}
.copy-button:hover,
.copy-button:focus-visible {
  outline: 1px solid currentColor;
}
.copy-button:disabled {
  cursor: wait;
}
.copy-status:empty {
  display: none;
}
.copy-status {
  position: absolute;
  right: 100%;
  padding: 2px 6px;
  background: rgb(var(--v-theme-surface));
  color: rgb(var(--v-theme-on-surface));
  font-size: 0.75rem;
  white-space: nowrap;
}
</style>
