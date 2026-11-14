<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { mdiAccountPlusOutline, mdiDeleteOutline, mdiPencilOutline } from '@mdi/js'
import { addTimePerson, fetchTimeSettings, removeTimePerson, updateTimeDefaults, updateTimePerson } from '../api.js'
import SettingsSection from './SettingsSection.vue'

// Platform settings → Time tracking: the person registry, the switch and
// the platform-wide time defaults, written to config.json by the daemon and applied
// without a restart. Every write answers the settings as they now are.
const emit = defineEmits(['state'])

const settings = ref(null)
const loadError = ref(null)
const applied = ref(null)

const people = computed(() => settings.value?.people || [])
const tracking = computed(() => settings.value?.tracking || null)
const APPLIED = {
  started: 'Time tracking started.', stopped: 'Time tracking stopped.', updated: 'Applied to the running daemon.',
  off: 'Saved. Time tracking is off.', restart: 'Saved. Restart the daemon to apply it.',
}

// A people edit or the switch leaves an unsaved default where it is.
function take(reply, { defaults = false } = {}) {
  const keep = !defaults && defaultsDirty.value
  settings.value = reply
  applied.value = reply.applied ? APPLIED[reply.applied] || null : null
  if (!keep) readDefaults()
}

async function load() {
  try {
    take(await fetchTimeSettings(), { defaults: true })
    loadError.value = null
  } catch (exc) { loadError.value = exc.message }
}
onMounted(load)

// --- the switch -------------------------------------------------------------
const switchBusy = ref(false)
const switchError = ref(null)
async function setSwitch(on) {
  switchBusy.value = true
  switchError.value = null
  try { take(await updateTimeDefaults({ tracking: !!on })) } catch (exc) { switchError.value = exc.message } finally {
    switchBusy.value = false
  }
}
const trackingLine = computed(() => {
  if (!tracking.value) return ''
  if (tracking.value.problems.length) return 'Off: config.json has a problem.'
  if (!tracking.value.switch) return 'Off: switched off. The people and settings below are kept.'
  if (!people.value.length) return 'Off: nobody is listed yet. Add a person to start tracking.'
  return tracking.value.live ? 'Running.' : 'On in config.json; this daemon is not running it yet.'
})

// --- people -----------------------------------------------------------------
const draft = ref(null)
const editing = ref(null)
const peopleBusy = ref(false)
const peopleError = ref(null)
const removing = ref(null)

const emptyDraft = () => ({ key: '', name: '', slack: [], matrix: [] })
function startAdd(identity = null) {
  editing.value = null
  const row = emptyDraft()
  if (identity) {
    const [transport, raw] = [identity.slice(0, identity.indexOf(':')), identity.slice(identity.indexOf(':') + 1)]
    if (transport === 'slack' || transport === 'matrix') row[transport] = [raw]
  }
  draft.value = row
}
function startEdit(person) {
  draft.value = null
  editing.value = { key: person.key, name: person.name || '', slack: [...(person.slack || [])],
    matrix: [...(person.matrix || [])] }
}
function cancel() { draft.value = null; editing.value = null; peopleError.value = null }

const clean = (ids) => ids.map(item => String(item).trim()).filter(Boolean)
async function writePeople(call) {
  peopleBusy.value = true
  peopleError.value = null
  try {
    take(await call())
    draft.value = null
    editing.value = null
    removing.value = null
  } catch (exc) { peopleError.value = exc.message } finally { peopleBusy.value = false }
}
const saveAdd = () => writePeople(() => addTimePerson({ key: draft.value.key.trim(), name: draft.value.name.trim() || undefined,
  slack: clean(draft.value.slack), matrix: clean(draft.value.matrix) }))
const saveEdit = () => writePeople(() => updateTimePerson(editing.value.key, { name: editing.value.name.trim(),
  slack: clean(editing.value.slack), matrix: clean(editing.value.matrix) }))
const remove = (key) => writePeople(() => removeTimePerson(key))
const addIdentity = (key, identity) => writePeople(() => updateTimePerson(key, { add: [identity] }))
const setDefaultPerson = (key) => writePeople(() => updateTimeDefaults({ default_person: key || null }))
const defaultOptions = computed(() => [{ value: '', title: 'nobody' },
  ...people.value.map(person => ({ value: person.key, title: person.name || person.key }))])

// --- defaults -------------------------------------------------------------------
const NUMBERS = [
  { key: 'idle_grace_minutes', label: 'Idle grace (minutes)', hint: 'For projects that set none.' },
  { key: 'min_entry_seconds', label: 'Shortest entry written (seconds)', hint: '0 writes every entry.' },
  { key: 'pause_hours', label: 'Pause after a manual stop (hours)', hint: '0 is no pause.' },
  { key: 'title_max_age_days', label: 'Keep recent values (days)', hint: 'Also kept entries and prompts.' },
  { key: 'sweep_seconds', label: 'Idle sweep (seconds)', hint: 'How often idle timers are closed.' },
]
const TEXTS = [
  { key: 'writer_name', label: 'Timesheet commit author' },
  { key: 'writer_email', label: 'Timesheet commit email' },
]
const form = ref({})
const baseline = ref('{}')
const defaultsBusy = ref(false)
const defaultsError = ref(null)
const stored = (key) => settings.value?.defaults?.[key]?.stored
function readDefaults() {
  const values = {}
  for (const row of [...NUMBERS, ...TEXTS]) values[row.key] = stored(row.key) == null ? '' : String(stored(row.key))
  form.value = values
  baseline.value = JSON.stringify(values)
}
const defaultsDirty = computed(() => !!settings.value && JSON.stringify(form.value) !== baseline.value)
function source(key) {
  const row = settings.value?.defaults?.[key]
  if (!row) return ''
  if (row.source === 'env') return `${row.env} is exported and wins: ${row.value}`
  if (row.source === 'default') return `default ${row.default}`
  return ''
}
async function saveDefaults() {
  const before = JSON.parse(baseline.value)
  const changes = {}
  for (const [key, value] of Object.entries(form.value)) {
    if (value !== before[key]) changes[key] = String(value).trim() === '' ? null : String(value).trim()
  }
  const submitted = { ...form.value }
  defaultsBusy.value = true
  defaultsError.value = null
  try {
    const reply = await updateTimeDefaults(changes)
    // A field typed into while the save was in flight keeps what was typed, still unsaved.
    const typed = { ...form.value }
    take(reply, { defaults: true })
    for (const key of Object.keys(typed)) if (typed[key] !== submitted[key]) form.value[key] = typed[key]
  } catch (exc) { defaultsError.value = exc.message } finally {
    defaultsBusy.value = false
  }
}

const dirty = computed(() => defaultsDirty.value || !!draft.value || !!editing.value)
const saving = computed(() => defaultsBusy.value || peopleBusy.value || switchBusy.value)
watch([dirty, saving], () => emit('state', { dirty: dirty.value, saving: saving.value }), { immediate: true })

function reset() {
  readDefaults()
  cancel()
  defaultsError.value = null
}
defineExpose({ reset, load })
</script>

<template>
  <div class="time-settings">
    <v-alert v-if="loadError" type="error" density="compact" class="mb-3">{{ loadError }}</v-alert>
    <template v-if="settings">
      <SettingsSection title="Time tracking" heading-id="time-settings-tracking"
        description="Whether the daemon tracks people's time at all.">
        <v-switch :model-value="tracking.switch" class="time-settings-switch" color="primary" inset hide-details
          label="Track time" :loading="switchBusy" :disabled="switchBusy" @update:model-value="setSwitch" />
        <p class="time-settings-state text-body-medium mt-2 mb-0">{{ trackingLine }}</p>
        <ul v-if="tracking.problems.length" class="time-settings-problems text-body-small">
          <li v-for="problem in tracking.problems" :key="problem">{{ problem }}</li>
        </ul>
        <p v-if="applied" class="time-settings-applied text-body-small text-medium-emphasis mt-2 mb-0">{{ applied }}</p>
        <v-alert v-if="switchError" type="error" density="compact" class="mt-2">{{ switchError }}</v-alert>
      </SettingsSection>

      <SettingsSection title="People" heading-id="time-settings-people"
        description="Whose time is tracked, and the Slack and Matrix ids that are them. Only listed ids start a timer.">
        <v-select :model-value="settings.default_person.stored || ''" :items="defaultOptions" label="Default person"
          class="time-settings-default mb-3" density="compact" :disabled="peopleBusy"
          :hint="`The control UI, the web chat and lmer-ctl with the shared secret act for them.${
            !settings.default_person.stored && settings.default_person.effective
              ? ` With one person listed it is ${settings.default_person.effective}.` : ''}`"
          persistent-hint @update:model-value="setDefaultPerson" />
        <v-table density="compact" class="time-settings-people">
          <thead><tr><th>Key</th><th>Name</th><th>Slack</th><th>Matrix</th><th /></tr></thead>
          <tbody>
            <template v-for="person in people" :key="person.key">
              <tr v-if="editing?.key !== person.key" class="time-person" :data-person="person.key">
                <td><code>{{ person.key }}</code></td>
                <td>{{ person.name || person.key }}</td>
                <td>{{ (person.slack || []).join(', ') }}</td>
                <td>{{ (person.matrix || []).join(', ') }}</td>
                <td class="text-end text-no-wrap">
                  <v-btn :icon="mdiPencilOutline" variant="text" size="small" :aria-label="`edit ${person.key}`"
                    class="time-person-edit" @click="startEdit(person)" />
                  <v-btn :icon="mdiDeleteOutline" variant="text" size="small" :aria-label="`remove ${person.key}`"
                    class="time-person-remove" @click="removing = person.key" />
                </td>
              </tr>
              <tr v-else class="time-person-editing">
                <td><code>{{ person.key }}</code></td>
                <td><v-text-field v-model="editing.name" label="Name" density="compact" hide-details /></td>
                <td><v-combobox v-model="editing.slack" label="Slack user ids" multiple chips closable-chips
                  density="compact" hide-details class="time-person-slack" /></td>
                <td><v-combobox v-model="editing.matrix" label="Matrix ids" multiple chips closable-chips
                  density="compact" hide-details class="time-person-matrix" /></td>
                <td class="text-end text-no-wrap">
                  <v-btn variant="text" size="small" @click="cancel">Cancel</v-btn>
                  <v-btn color="primary" variant="flat" size="small" class="time-person-save" :loading="peopleBusy"
                    @click="saveEdit">Save</v-btn>
                </td>
              </tr>
            </template>
            <tr v-if="!people.length"><td colspan="5" class="text-medium-emphasis">Nobody is listed.</td></tr>
          </tbody>
        </v-table>
        <v-alert v-if="removing" type="warning" density="compact" class="time-person-confirm mt-3">
          Remove {{ removing }}? Their entries stay in the work repo; their ids stop starting time.
          <template #append>
            <v-btn variant="text" size="small" @click="removing = null">Keep</v-btn>
            <v-btn color="error" variant="flat" size="small" class="time-person-remove-confirm" :loading="peopleBusy"
              @click="remove(removing)">Remove</v-btn>
          </template>
        </v-alert>
        <div v-if="draft" class="time-person-add mt-3">
          <v-text-field v-model="draft.key" label="Key" hint="Lowercase letters, digits, '.', '_' or '-'; the directory under timesheet.d/."
            persistent-hint density="compact" class="time-person-key" />
          <v-text-field v-model="draft.name" label="Name" density="compact" hide-details />
          <v-combobox v-model="draft.slack" label="Slack user ids" multiple chips closable-chips density="compact" hide-details />
          <v-combobox v-model="draft.matrix" label="Matrix ids (@user:server)" multiple chips closable-chips
            density="compact" hide-details />
          <div class="d-flex ga-2">
            <v-btn variant="text" @click="cancel">Cancel</v-btn>
            <v-btn color="primary" variant="flat" class="time-person-add-save" :loading="peopleBusy"
              :disabled="!draft.key.trim()" @click="saveAdd">Add</v-btn>
          </div>
        </div>
        <v-alert v-if="peopleError" type="error" density="compact" class="time-people-error mt-3">{{ peopleError }}</v-alert>
        <div v-if="settings.unmapped.length" class="time-unmapped mt-4">
          <h3 class="text-title-small">Not listed yet</h3>
          <p class="text-body-small text-medium-emphasis">Recent messages from these ids tracked no time.</p>
          <div v-for="identity in settings.unmapped" :key="identity" class="time-unmapped-row" :data-identity="identity">
            <code>{{ identity }}</code>
            <v-menu>
              <template #activator="{ props: menu }">
                <v-btn v-bind="menu" variant="tonal" size="small" class="time-unmapped-add" :disabled="peopleBusy">Add to…</v-btn>
              </template>
              <v-list density="compact">
                <v-list-item v-for="person in people" :key="person.key" :title="person.name || person.key"
                  :data-person="person.key" @click="addIdentity(person.key, identity)" />
                <v-list-item title="A new person…" class="time-unmapped-new" @click="startAdd(identity)" />
              </v-list>
            </v-menu>
          </div>
        </div>
        <template #actions>
          <v-btn v-if="!draft" variant="tonal" :prepend-icon="mdiAccountPlusOutline" class="time-person-new"
            @click="startAdd()">Add a person</v-btn>
        </template>
      </SettingsSection>

      <SettingsSection title="Defaults" heading-id="time-settings-defaults"
        description="Platform-wide time settings. Leave a field empty for the default." :dirty="defaultsDirty"
        save-label="Save" :saving="defaultsBusy" @save="saveDefaults">
        <div class="time-defaults">
          <v-text-field v-for="row in NUMBERS" :key="row.key" v-model="form[row.key]" :label="row.label"
            :data-setting="row.key" :placeholder="String(settings.defaults[row.key].default)" type="number"
            :min="settings.defaults[row.key].minimum" density="compact" persistent-hint
            :hint="[row.hint, source(row.key)].filter(Boolean).join(' · ')" />
          <v-text-field v-for="row in TEXTS" :key="row.key" v-model="form[row.key]" :label="row.label"
            :data-setting="row.key" :placeholder="settings.defaults[row.key].default" density="compact"
            persistent-hint :hint="source(row.key)" />
        </div>
        <v-alert v-if="defaultsError" type="error" density="compact" class="time-defaults-error mt-3">{{ defaultsError }}</v-alert>
        <h3 class="text-title-small mt-4">Idle grace by project</h3>
        <v-table density="compact" class="time-inherits">
          <tbody>
            <tr v-for="row in settings.projects" :key="row.workspace_id" :data-project="row.workspace_id">
              <td>{{ row.name }}</td>
              <td>{{ row.effective_idle_grace_minutes }} min</td>
              <td class="text-medium-emphasis">{{ row.idle_grace_minutes == null ? 'inherits the default' : 'its own' }}</td>
            </tr>
          </tbody>
        </v-table>
        <p class="text-body-small text-medium-emphasis mt-2 mb-0">
          The writer's push token is never set here: export LMER_PLATFORM_TIME_WRITER_TOKEN in the daemon's environment.
        </p>
      </SettingsSection>
    </template>
  </div>
</template>

<style scoped>
.time-defaults { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
.time-person-add { display: grid; gap: 8px; max-width: 560px; }
.time-unmapped-row { display: flex; align-items: center; gap: 8px; margin-top: 4px; flex-wrap: wrap; }
.time-settings-problems { margin: 8px 0 0 20px; }
</style>
