<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  mdiAlertOutline, mdiChevronUp, mdiFormatListBulleted, mdiPencilOutline, mdiPlay, mdiStop, mdiTimerOutline,
  mdiTimerSyncOutline,
} from '@mdi/js'
import { setTimeAutomatic, startTime, stopTime, updateTime } from '../api.js'
import { issueLabel } from '../format.js'

// The time-tracking bar under the header (spec D3, issue 668). Slim, it is one line:
// the timer (project, client, title, elapsed time and mode) or the project a Start
// would track, the warning and prompt chips, Start or Stop and the edit toggle.
// `editing` adds the manual controls (D19). `status` is GET api/time/status;
// `readAt` is when it was read, so the elapsed time ticks from the daemon's own count
// with `now`; `defaultProject` is the project the page is on; `focus` is
// `{ id, nonce, prompt }` from a prompt chip, the nonce making a repeat tap count;
// Start then answers `prompt` (#665).
const props = defineProps({
  status: { type: Object, required: true },
  defaultProject: { type: String, default: '' },
  focus: { type: Object, default: null },
  editing: Boolean,
  readAt: { type: Number, default: 0 },
  now: { type: Number, default: () => Date.now() },
})
const emit = defineEmits(['changed', 'entries', 'prompt', 'edit', 'size'])

const running = computed(() => props.status.running || null)
const options = computed(() => Object.values(props.status.projects || {})
  .filter(row => !row.archived || row.workspace_id === running.value?.workspace_id)
  .map(row => ({ value: row.workspace_id, title: row.name })))
const selectedId = ref(null)
const project = computed(() => props.status.projects?.[selectedId.value] || null)
const handlerFields = computed(() => project.value?.handler?.fields || [])
const runningHere = computed(() => !!running.value && running.value.workspace_id === selectedId.value)
const form = ref({ client: null, title: null, fields: {} })
// The running timer's values as the form loaded them, sent as PATCH `base` (D24).
const base = ref(null)
const answering = ref(null)
const busy = ref(false)
const error = ref(null)
const text = (value) => (typeof value === 'string' ? value.trim() : value == null ? '' : String(value))

// Set when something is typed into the form, cleared when it is seeded or sent.
let touched = false
// The form as a prompt seeded it.
let seed = null

function known(id) { return !!id && !!props.status.projects?.[id] }

watch([() => props.defaultProject, () => options.value.length], () => {
  if (known(selectedId.value)) return
  selectedId.value = [running.value?.workspace_id, props.defaultProject, options.value[0]?.value].find(known) || null
}, { immediate: true })
// A timer that starts or switches elsewhere (chat, activity) brings the bar to it,
// unless that would throw away what is being typed.
watch(() => running.value?.workspace_id, (id) => {
  if (known(id) && !touched) selectedId.value = id
})
watch(() => props.focus?.nonce, () => {
  if (known(props.focus?.id)) selectedId.value = props.focus.id
  answering.value = props.focus?.prompt || null
}, { immediate: true })
const prompt = computed(() => (answering.value?.workspace_id === selectedId.value ? answering.value : null))

// Re-seeded only when the project or the running timer changes: the status poll
// replaces `status` every few seconds, and a seed on every reply would wipe what is
// being typed. Separate sources, because a getter returning an array is a new
// array every time and would count every reply as a change.
watch([selectedId, runningHere, () => running.value?.start, () => prompt.value?.id], () => {
  touched = false
  error.value = null
  // An answer is the prompt's, whatever runs in its project: what it lacks starts
  // empty, the rest shows what the daemon fills in itself, and only what is typed
  // over that is sent.
  if (prompt.value) {
    const lacking = new Set(prompt.value.missing.map(item => item.key))
    form.value = { client: project.value?.clients?.[0] || null, title: null,
      fields: Object.fromEntries(handlerFields.value.map(item => [item.key,
        lacking.has(item.key) || item.per_issue ? '' : project.value?.recent?.fields?.[item.key]?.[0] || ''])) }
    base.value = null
    seed = sent()
    return
  }
  if (runningHere.value) {
    form.value = { client: running.value.client || null, title: running.value.title || null,
      fields: { ...running.value.fields } }
    base.value = sent()
    return
  }
  base.value = null
  // A new start takes what the tracker would default to: the project's first
  // client label (D12). Handler fields start empty, because a manual start never
  // reuses a recent value on its own (D19); the recent values are offered below.
  form.value = { client: project.value?.clients?.[0] || null, title: null,
    fields: Object.fromEntries(handlerFields.value.map(item => [item.key, ''])) }
}, { immediate: true })

function edit(change) {
  touched = true
  form.value = { ...form.value, ...change }
}
function editField(key, value) {
  edit({ fields: { ...form.value.fields, [key]: value ?? '' } })
}

// Python's re.fullmatch, which is what the daemon checks a value with. A pattern
// this browser cannot compile is left to the daemon.
function matches(pattern, value) {
  try { return new RegExp(`^(?:${pattern})$`).test(value) } catch { return true }
}
const fieldProblems = computed(() => Object.fromEntries(handlerFields.value.map((item) => {
  const value = text(form.value.fields[item.key])
  const needed = prompt.value ? prompt.value.missing.some(lack => lack.key === item.key) : item.required
  if (!value) return [item.key, needed ? 'missing' : null]
  if (item.pattern && !matches(item.pattern, value)) return [item.key, `Does not match ${item.pattern}`]
  return [item.key, null]
})))
const missingFields = computed(() => handlerFields.value.filter(item => fieldProblems.value[item.key] === 'missing'))
const ready = computed(() => !!project.value && Object.values(fieldProblems.value).every(problem => !problem))
const fieldValues = computed(() => Object.fromEntries(handlerFields.value
  .map(item => [item.key, text(form.value.fields[item.key])]).filter(([, value]) => value)))
// Every value, empty included: the daemon reads a missing value as "unchanged"
// and an empty one as "clear".
function sent() {
  return { client: text(form.value.client), title: text(form.value.title),
    fields: Object.fromEntries(handlerFields.value.map(item => [item.key, text(form.value.fields[item.key])])) }
}
// Against what the form loaded, not the live timer: a title the run renamed is no edit.
const dirty = computed(() => runningHere.value && !!base.value
  && JSON.stringify(sent()) !== JSON.stringify(base.value))
const canStart = computed(() => ready.value && !busy.value
  && (!!prompt.value || !(runningHere.value && running.value.mode === 'manual')))

async function act(call) {
  busy.value = true
  error.value = null
  try {
    await call()
    touched = false
    emit('changed')
  } catch (exc) {
    error.value = exc.message
    if (exc.status === 503 || exc.status === 409) emit('changed')
  } finally { busy.value = false }
}

const start = () => act(async () => {
  if (prompt.value) {
    const now = sent()
    const typed = (value, was) => (value && value !== was ? value : undefined)
    const fields = Object.fromEntries(Object.entries(now.fields).filter(([key, value]) => typed(value, seed.fields[key])))
    await startTime({ prompt: prompt.value.id, client: typed(now.client, seed.client),
      title: typed(now.title, seed.title), fields })
  } else {
    await startTime({ client: text(form.value.client) || undefined, title: text(form.value.title) || undefined,
      fields: fieldValues.value, project: selectedId.value })
  }
  answering.value = null
})
const update = () => act(async () => {
  const values = sent()
  await updateTime(values, base.value)
  base.value = values
})

const titleSources = { run: 'from the run', person: 'set by hand', recent: 'the last title used' }
const runInfo = computed(() => (runningHere.value && (running.value.identity || running.value.issues?.length)
  ? { issues: running.value.issues || [], source: titleSources[running.value.title_source] || '' }
  : null))
const stop = () => act(() => stopTime({ project: running.value?.workspace_id }))
const automatic = computed(() => props.status.automatic?.on !== false)
const setAutomatic = (on) => act(() => setTimeAutomatic(!!on))

const modeLabel = computed(() => (running.value?.mode === 'manual' ? 'manual' : 'automatic'))
const elapsed = computed(() => {
  if (!running.value) return ''
  const since = props.readAt ? Math.max(0, Math.floor((props.now - props.readAt) / 1000)) : 0
  const total = (running.value.elapsed_seconds || 0) + since
  const pad = (value) => String(value).padStart(2, '0')
  return `${Math.floor(total / 3600)}:${pad(Math.floor(total / 60) % 60)}:${pad(total % 60)}`
})
// The slim line: the running timer, else what a Start from here would track.
const line = computed(() => (running.value
  ? { project: running.value.project_name || running.value.project_key, client: running.value.client,
      title: running.value.title }
  : { project: project.value?.name || '', client: text(form.value.client), title: text(form.value.title) }))
const summary = computed(() => (running.value
  ? `tracking ${line.value.project} · ${elapsed.value} · ${modeLabel.value}` : 'not tracking'))

function chipText(warning) {
  if (warning.kind === 'no_client') return 'no client'
  if (warning.kind === 'missing_field') return `needs ${warning.label || warning.field}`
  if (warning.kind === 'writer') return 'entries not written'
  return warning.message
}
const chips = computed(() => [
  ...(props.status.warnings || []).map((warning, index) => ({
    key: `w${index}`, kind: warning.kind, text: chipText(warning), title: warning.message, warning,
  })),
  ...(props.status.prompts || []).map((prompt, index) => ({
    key: `p${index}`, kind: 'prompt', text: prompt.message, title: prompt.message, prompt,
  })),
])
// A held warning opens the entries already filtered to what is held (spec D18); a
// prompt opens the editor on its project; the rest are fixed in the editor.
function choose(chip) {
  if (chip.kind === 'held') emit('entries', { status: 'held', handler: chip.warning.handler || null })
  else if (chip.kind === 'prompt') emit('prompt', chip.prompt)
  else if (!props.editing) emit('edit')
}

// The shell reserves the bar's height in the layout (a v-layout-item), so it reports it.
const sheet = ref(null)
let observer = null
onMounted(() => {
  const el = sheet.value?.$el
  if (!el || typeof ResizeObserver === 'undefined') return
  observer = new ResizeObserver(() => emit('size', el.offsetHeight))
  observer.observe(el)
})
onBeforeUnmount(() => { observer?.disconnect() })
</script>

<template>
  <v-sheet id="time-tracking-bar" ref="sheet" class="time-tracking-bar" :class="{ 'time-bar-editing': editing }"
    role="region" aria-label="Time tracking" elevation="1">
    <div class="time-bar-slim" role="status" :aria-label="summary">
      <v-icon v-if="running" class="time-mode-icon" :icon="running.mode === 'manual' ? mdiTimerOutline : mdiTimerSyncOutline"
        size="18" />
      <span class="time-project">{{ line.project }}</span>
      <v-chip v-if="line.client" class="time-client-chip" size="small" variant="tonal">{{ line.client }}</v-chip>
      <span v-if="line.title" class="time-line-title" :title="line.title">{{ line.title }}</span>
      <span v-if="running" class="time-elapsed">{{ elapsed }}</span>
      <span v-if="running" class="time-mode">{{ modeLabel }}</span>
      <v-chip v-for="chip in chips" :key="chip.key" class="time-chip" :class="`time-chip-${chip.kind}`"
        size="small" variant="flat" :color="chip.kind === 'held' || chip.kind === 'writer' ? 'error' : 'warning'"
        :prepend-icon="mdiAlertOutline" :title="chip.title" @click="choose(chip)">{{ chip.text }}</v-chip>
      <span class="time-slim-actions">
        <v-btn v-if="!editing && !running" class="time-start time-start-slim" color="primary" variant="flat" size="small"
          :prepend-icon="mdiPlay" :disabled="!canStart" :loading="busy" @click="start">Start</v-btn>
        <v-btn v-if="!editing && running" class="time-stop time-stop-slim" variant="tonal" size="small"
          :prepend-icon="mdiStop" :disabled="busy" @click="stop">Stop</v-btn>
        <v-btn class="time-edit" variant="text" size="small" :prepend-icon="editing ? mdiChevronUp : mdiPencilOutline"
          :aria-expanded="editing" aria-controls="time-bar-editor" @click="emit('edit')">{{ editing ? 'Done' : 'Edit' }}</v-btn>
      </span>
    </div>
    <v-alert v-if="error && !editing" type="error" density="compact" class="time-error mt-2">{{ error }}</v-alert>
    <div v-show="editing" id="time-bar-editor" class="time-bar-editor">
      <div class="time-bar-row">
        <v-select v-model="selectedId" :items="options" label="Project" density="compact" hide-details
          class="time-project-select" :disabled="busy" />
        <v-combobox :model-value="form.client" :items="project?.clients || []" label="Client" density="compact"
          hide-details class="time-client" :disabled="busy || !project" @update:model-value="value => edit({ client: value })" />
        <v-combobox :model-value="form.title" :items="project?.recent?.titles || []" label="Title" density="compact"
          hide-details class="time-title" :placeholder="project?.recent?.titles?.[0] || ''" :disabled="busy || !project"
          @update:model-value="value => edit({ title: value })" />
        <v-combobox v-for="item in handlerFields" :key="item.key" :model-value="form.fields[item.key]"
          :items="project?.recent?.fields?.[item.key] || []" :label="item.required ? `${item.label} *` : item.label"
          :hint="item.hint || ''" persistent-hint density="compact" class="time-field" :data-field="item.key"
          :error-messages="fieldProblems[item.key] && fieldProblems[item.key] !== 'missing' ? [fieldProblems[item.key]] : []"
          :disabled="busy" @update:model-value="value => editField(item.key, value)" />
        <div class="time-bar-actions">
          <v-btn v-if="prompt || !(runningHere && running.mode === 'manual')" class="time-start" color="primary" variant="flat"
            :prepend-icon="mdiPlay" :disabled="!canStart" :loading="busy" @click="start">Start</v-btn>
          <v-btn v-if="dirty" class="time-update" variant="tonal" :disabled="busy || !ready" @click="update">Update</v-btn>
          <v-btn v-if="running" class="time-stop" variant="tonal" :prepend-icon="mdiStop" :disabled="busy" @click="stop">Stop</v-btn>
          <v-btn class="time-entries" variant="tonal" :prepend-icon="mdiFormatListBulleted"
            @click="emit('entries', {})">View entries</v-btn>
          <v-switch :model-value="automatic" class="time-automatic" color="primary" density="compact" hide-details
            inset label="Automatic" :disabled="busy" @update:model-value="setAutomatic" />
        </div>
      </div>
      <div class="time-bar-notes">
        <v-alert v-if="error" type="error" density="compact" class="time-error">{{ error }}</v-alert>
        <v-alert v-if="prompt" type="info" density="compact" class="time-prompt">
          {{ prompt.message }}. Start records the value for it.
        </v-alert>
        <p v-if="runInfo" class="time-run text-body-small mb-0">
          <span v-if="runInfo.source" class="time-title-source">Title {{ runInfo.source }}.</span>
          <template v-for="url in runInfo.issues" :key="url">
            <a class="time-issue" :href="url" target="_blank" rel="noopener noreferrer">{{ issueLabel(url) }}</a>{{ ' ' }}
          </template>
        </p>
        <v-alert v-if="project && !text(form.client)" type="warning" density="compact" class="time-no-client">
          No client label: {{ project.name }}'s entry is logged without one.
        </v-alert>
        <v-alert v-for="item in missingFields" :key="item.key" type="warning" density="compact" class="time-missing-field">
          {{ project.name }} needs a {{ item.label }} to track time
        </v-alert>
        <p v-if="project?.handler_warning" class="time-handler-warning text-body-small mb-0">{{ project.handler_warning }}</p>
        <p v-if="!automatic" class="time-automatic-off text-body-small text-medium-emphasis mb-0">
          Automatic tracking is off: messages start no time until it is turned back on. Start and Stop still work.
        </p>
        <p v-if="status.pause && !running" class="time-pause text-body-small text-medium-emphasis mb-0">
          Automatic tracking is paused for {{ Math.ceil(status.pause.remaining_seconds / 60) }} min after a manual stop.
        </p>
      </div>
    </div>
  </v-sheet>
</template>

<style scoped>
/* Placed by the shell's v-layout-item, full width under the header (issue 668). An
   editor taller than the screen scrolls inside the bar. */
.time-tracking-bar {
  max-height: calc(100dvh - var(--v-layout-top, 64px) - env(safe-area-inset-top, 0px));
  overflow-y: auto;
  padding: 6px 12px;
  border-radius: 0;
}
.time-bar-slim {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
  min-height: 32px;
}
.time-project {
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 20ch;
}
.time-line-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
  flex: 0 1 auto;
  max-width: 40ch;
}
.time-elapsed {
  font-variant-numeric: tabular-nums;
  font-weight: 500;
}
.time-mode {
  opacity: .8;
}
.time-chip {
  flex-shrink: 1;
  min-width: 0;
  max-width: 28ch;
}
.time-chip :deep(.v-chip__content) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.time-slim-actions {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-inline-start: auto;
}
.time-bar-editor {
  margin-top: 8px;
}
.time-automatic {
  flex: 0 0 auto;
}
.time-bar-row {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 8px;
}
.time-bar-row > .v-input {
  flex: 1 1 160px;
  min-width: 140px;
}
.time-bar-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  min-height: 40px;
}
.time-bar-notes {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.time-bar-notes > :first-child {
  margin-top: 8px;
}
</style>
