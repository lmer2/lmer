<script setup>
// The service-slot editor (issue #256): declare, re-point and remove a slot
// without opening `~/.lmer/platform/config.json` on the host.
//
// Two things about it are the feature rather than the form.
//
// The verdict shown before a save is the daemon's, not this component's. Every
// keystroke settles into a `dry_run` call that runs the whole save path and
// writes nothing, and what comes back is the row — the same chip, the same
// sentence the fleet view will show afterwards. A form with its own opinion
// about what makes a slot valid is how the two come to disagree, and the
// disagreement surfaces as a slot that saved cleanly and reads broken.
//
// A save carries the etag the row was read at — and *read at* means when this
// dialog opened, not when the fleet last polled. `props.row` is a computed over
// the polled payload and is replaced every ten seconds, so sending its etag
// would hand the daemon the version of a change the operator has not seen: the
// check would pass, the save would land, and the fields (still holding the old
// preset) would revert it. So the row is snapshotted at open and the snapshot
// is what is diffed against and sent. The other writer here is a person in a
// text editor; "last write wins" means losing their edit silently.

import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { mdiDelete, mdiRefresh } from '@mdi/js'

import { createSlot, fetchSpawnOptions, removeSlot, updateSlot } from '../api.js'
import { slotMeta, toneColor } from '../format.js'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  // The row being edited, or null to declare a new slot. The row is where the
  // etag comes from, which is why the editor takes the whole thing rather than
  // a name. Named `row` for SlotRow.vue's reason: `slot` is Vue's own
  // vocabulary and warns under the migration build.
  row: { type: Object, default: null },
})

const emit = defineEmits(['update:modelValue', 'changed'])

const open = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value),
})

// The row as it was when this dialog opened. Everything the save depends on
// reads from here rather than from `props.row`, which moves under it (see the
// header). Null when declaring a new slot.
const opened = ref(null)

const editing = computed(() => !!opened.value)

const name = ref('')
const preset = ref(null)
const description = ref('')
const presets = ref([])
const problem = ref(null)
// The row the daemon says this would be. Null until a preview lands, and
// deliberately kept through a failed one: a stale-but-real verdict beside an
// error is how the rest of this app treats a read that did not land.
const preview = ref(null)
const previewing = ref(false)
const saving = ref(false)
// The removal's second step. A slot a live session holds is refused outright
// the first time; this is what the operator has to say to mean it.
const confirmingRemoval = ref(false)
// The same second step for re-pointing an occupied slot. Separate from the
// removal's, because they are different sentences and the operator may hit
// either one first.
const confirmingRebind = ref(false)

// Per-request, so a slow preview that lost the race cannot land on top of a
// newer one — the guard AssistantSettings.vue uses for the same reason.
let seq = 0
let disposed = false
let previewTimer = null

function stale(mine) {
  return disposed || mine !== seq
}

// A preview is a real resolution on the daemon — it reads the presets file and
// probes the dev service — so one per keystroke would pay that per character of
// a name nobody has finished typing.
const PREVIEW_DELAY_MS = 350

function typed(value) {
  return typeof value === 'string' ? value.trim() : ''
}

function reset() {
  problem.value = null
  preview.value = null
  confirmingRemoval.value = false
  confirmingRebind.value = false
  // A copy, not the prop: the prop is replaced on every poll.
  opened.value = props.row ? { ...props.row } : null
  name.value = opened.value?.name || ''
  preset.value = opened.value?.preset || null
  description.value = opened.value?.description || ''
}

// The row moved under the dialog — another operator saved, or the file was
// edited. Said rather than absorbed: the fields were composed against a version
// that is gone, so a save would be refused (412) and the operator should know
// before pressing it. Compared against the snapshot, which is what makes this
// observable at all.
const movedUnderneath = computed(() => {
  if (!opened.value) return false
  // Gone from the payload entirely: removed, or edited into a shape
  // `parse_slots` drops. Not the same thing as changed, and it must not silently
  // turn this dialog into a "declare a new slot" — which is what reading
  // `props.row` directly did.
  if (!props.row) return true
  return props.row.etag !== opened.value.etag
})

// Null while unread, and set to the request's own error when the read fails.
// The distinction matters because the preset field is a `v-select`: an empty
// list leaves nothing selectable, so "loaded, and empty" is a host that needs
// LMER_PRESETS_FILE while "did not load" is a request to retry — and telling
// the operator to go and edit the daemon's environment over an expired token is
// the one place this app would turn a failed read into a claim about the host.
const presetsError = ref(null)

async function loadPresets() {
  try {
    const payload = await fetchSpawnOptions()
    presets.value = payload.presets || []
    presetsError.value = null
  } catch (exc) {
    presets.value = []
    presetsError.value = exc.message
  }
}

// What a save would send. An edit sends only what it changes, so a description
// left alone is not re-asserted over one the file has.
function changes() {
  if (!editing.value) {
    return {
      name: typed(name.value),
      preset: typed(preset.value),
      description: typed(description.value),
    }
  }
  const patch = {}
  if (typed(preset.value) !== (opened.value.preset || '')) {
    patch.preset = typed(preset.value)
  }
  if (typed(description.value) !== (opened.value.description || '')) {
    patch.description = typed(description.value)
  }
  return patch
}

async function refreshPreview() {
  const mine = ++seq
  if (!typed(name.value) || !typed(preset.value)) {
    preview.value = null
    return
  }
  previewing.value = true
  try {
    const body = editing.value
      ? await updateSlot(opened.value.name, {
        preset: typed(preset.value),
        description: typed(description.value),
      }, { etag: opened.value.etag, dryRun: true })
      : await createSlot(changes(), { dryRun: true })
    if (stale(mine)) return
    preview.value = body
    problem.value = null
  } catch (exc) {
    if (stale(mine)) return
    // The refusals a preview can raise are the ones the form exists to show
    // early — an unknown preset, a name already taken. Shown as the problem,
    // and the last good verdict stays under it.
    problem.value = exc.message
  } finally {
    if (!stale(mine)) previewing.value = false
  }
}

function schedulePreview() {
  if (previewTimer) clearTimeout(previewTimer)
  previewTimer = setTimeout(refreshPreview, PREVIEW_DELAY_MS)
}

watch([name, preset, description], schedulePreview)

watch(open, (isOpen) => {
  if (!isOpen) return
  reset()
  loadPresets()
  refreshPreview()
}, { immediate: true })

onBeforeUnmount(() => {
  disposed = true
  if (previewTimer) clearTimeout(previewTimer)
})

async function save() {
  saving.value = true
  problem.value = null
  try {
    if (editing.value) {
      const patch = changes()
      if (Object.keys(patch).length) {
        await updateSlot(opened.value.name, patch, {
          etag: opened.value.etag, confirm: confirmingRebind.value,
        })
      }
    } else {
      await createSlot(changes())
    }
    emit('changed')
    open.value = false
  } catch (exc) {
    problem.value = exc.message
    // The daemon refused because a live session is running in this slot and the
    // edit moves what it is bound to. Armed only after that refusal, never
    // offered up front: a confirm nobody had to read is one people learn to
    // tick.
    if (exc.code === 'slot_occupied') confirmingRebind.value = true
    // A slot that changed under the edit cannot be retried from this form: what
    // is in the fields was composed against a version that is gone. Reloading
    // the fleet is what puts a current row behind the next attempt.
    if (exc.code === 'slot_changed') emit('changed')
  } finally {
    saving.value = false
  }
}

async function remove() {
  saving.value = true
  problem.value = null
  try {
    await removeSlot(opened.value.name, {
      etag: opened.value.etag, confirm: confirmingRemoval.value,
    })
    emit('changed')
    open.value = false
  } catch (exc) {
    problem.value = exc.message
    // The daemon refused because a session is running in it. Offering the
    // confirm only here — never up front — is what keeps the refusal from
    // becoming a checkbox people learn to tick.
    if (exc.code === 'slot_occupied') confirmingRemoval.value = true
    if (exc.code === 'slot_changed') emit('changed')
  } finally {
    saving.value = false
  }
}

const meta = computed(() => slotMeta(preview.value?.state))

const presetHint = computed(() => {
  if (presetsError.value) {
    return `could not read this host's presets (${presetsError.value}) — retry`
  }
  if (!presets.value.length) {
    return 'this host has no presets loaded — the daemon\'s own environment '
      + 'must set LMER_PRESETS_FILE'
  }
  return 'the preset that puts the session into service mode'
})
</script>

<template>
  <v-dialog v-model="open" max-width="560">
    <v-card>
      <v-card-title class="text-title-medium">
        {{ editing ? `service slot ${opened.name}` : 'declare a service slot' }}
      </v-card-title>
      <v-card-text>
        <p class="text-body-small text-medium-emphasis mb-3">
          A slot names one dev service and lets one session at a time run against
          it. The service comes from the preset — which is why the preset is
          picked from this host's own list and a name it does not have is
          refused. Changes apply to the next poll; nothing restarts.
        </p>

        <v-alert v-if="problem" type="warning" density="compact" class="mb-3">
          {{ problem }}
        </v-alert>

        <!-- The row moved while this dialog was open. Said before the save
             rather than after its 412, because the fields on screen were
             composed against a version that no longer exists — and because a
             slot that has left the payload entirely must not quietly turn this
             into a form that declares a new one. -->
        <v-alert
          v-if="movedUnderneath"
          type="warning"
          density="compact"
          class="mb-3"
        >
          This slot changed on the host since the dialog opened — close and
          reopen it to edit the current version. Saving now will be refused.
        </v-alert>

        <v-text-field
          v-model="name"
          :disabled="editing"
          :hint="editing
            ? 'a slot is renamed by removing it and declaring the new name — '
              + 'the name is what a running session recorded'
            : 'what you spawn into, and what the fleet view calls the row'"
          label="name"
          persistent-hint
          class="mb-3"
        />

        <!-- A select rather than a combobox, unlike the spawn dialog's preset
             field: there the daemon's list is a suggestion because a session may
             name a preset the host cannot enumerate, and here it is the rule —
             the write refuses anything that is not on it, so offering free text
             would be offering a 400. -->
        <v-select
          v-model="preset"
          :items="presets"
          :hint="presetHint"
          label="preset"
          persistent-hint
          class="mb-3"
        >
          <!-- A retry rather than a dead select: the list did not load, which is
               this dialog's problem and not the host's. -->
          <template v-if="presetsError" #append>
            <v-btn
              :icon="mdiRefresh"
              density="comfortable"
              size="small"
              variant="text"
              title="try reading this host's presets again"
              @click="loadPresets"
            />
          </template>
        </v-select>

        <v-text-field
          v-model="description"
          label="description"
          hint="optional, shown on the row"
          persistent-hint
          class="mb-4"
        />

        <!-- The daemon's verdict on what is in the fields, in the row's own
             words. Rendered whenever there is one, including "free": a form that
             only speaks up when something is wrong leaves the operator unsure
             whether it looked. -->
        <div v-if="preview" class="d-flex flex-wrap align-center ga-2">
          <span class="text-body-small text-medium-emphasis">would be</span>
          <v-chip :color="toneColor(meta.tone)" size="small" variant="tonal">
            {{ meta.label }}
          </v-chip>
          <v-progress-circular
            v-if="previewing"
            indeterminate
            size="12"
            width="2"
          />
        </div>
        <div
          v-if="preview?.reason"
          class="text-body-small text-medium-emphasis mt-1 scroll-x"
        >{{ preview.reason }}</div>
      </v-card-text>

      <v-card-actions>
        <v-btn
          v-if="editing"
          :disabled="saving"
          :prepend-icon="mdiDelete"
          color="error"
          variant="text"
          @click="remove"
        >{{ confirmingRemoval ? 'remove anyway' : 'remove' }}</v-btn>
        <v-spacer />
        <v-btn :disabled="saving" variant="text" @click="open = false">cancel</v-btn>
        <v-btn
          :disabled="saving || !name || !preset"
          :loading="saving"
          color="primary"
          variant="tonal"
          @click="save"
        >{{ confirmingRebind ? 'save anyway' : 'save' }}</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
