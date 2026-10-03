<script setup>
import { computed, onBeforeUnmount, ref } from 'vue'
import { mdiBroom } from '@mdi/js'
import { forgetRun } from '../api.js'
import { runKey } from '../runref.js'

const props = defineProps({ runs: { type: Array, default: () => [] } })
const emit = defineEmits(['forgotten', 'changed'])
const eligible = computed(() => props.runs.filter((run) => run.state === 'complete' && !run.live))
const selected = ref([])
const open = ref(false)
const busy = ref(false)
const progress = ref(0)
const failures = ref([])
let disposed = false

function choose() {
  selected.value = [...eligible.value]
  failures.value = []
  progress.value = 0
  open.value = true
}

async function forgetAll() {
  if (busy.value) return
  busy.value = true
  failures.value = []
  try {
    for (const run of selected.value) {
      if (disposed) break
      const current = eligible.value.find((row) => runKey(row) === runKey(run))
      if (!current) {
        failures.value.push(`${run.title || run.label || run.slug}: no longer a completed, stopped run`)
        progress.value += 1
        continue
      }
      try {
        await forgetRun(current)
        if (!disposed) emit('forgotten', current)
      } catch (exc) {
        if (!disposed) failures.value.push(`${run.title || run.label || run.slug}: ${exc.message}`)
      }
      progress.value += 1
    }
  } finally {
    busy.value = false
    open.value = false
    if (!disposed) emit('changed')
  }
}

onBeforeUnmount(() => { disposed = true })
</script>

<template>
  <div class="mb-3">
    <v-btn
      variant="tonal"
      size="small"
      :prepend-icon="mdiBroom"
      :disabled="!eligible.length || busy"
      @click="choose"
    >Forget all completed</v-btn>
    <v-alert v-if="failures.length" type="warning" density="compact" class="mt-2">
      Some runs were not forgotten:
      <ul><li v-for="failure in failures" :key="failure">{{ failure }}</li></ul>
    </v-alert>
    <v-dialog v-model="open" max-width="480" :persistent="busy">
      <v-card>
        <v-card-title>Forget {{ selected.length }} completed {{ selected.length === 1 ? 'run' : 'runs' }}?</v-card-title>
        <v-card-text>
          This stops tracking all selected completed runs, including those hidden
          by search. It cannot be undone from this page. Run files and work-repo
          state stay intact.
          <p v-if="busy" class="mt-2">Processed {{ progress }} of {{ selected.length }} runs.</p>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn :disabled="busy" @click="open = false">cancel</v-btn>
          <v-btn color="warning" :loading="busy" :disabled="busy" @click="forgetAll">
            Forget {{ selected.length }} {{ selected.length === 1 ? 'run' : 'runs' }}
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>
