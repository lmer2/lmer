<script setup>
// The uber lmer settings dialog (issue #234): how the supervising session is
// *run* — model, harness, preset, agents fan-out — per platform instance.
//
// Its own component, and the split is load-bearing rather than tidy: the drawer
// (AssistantChat.vue) is pinned by tests/test_platform_web_assistant.py to carry
// NO input affordance at all, because the tempting thing to grow there is a
// textarea for the standing orders — a second writer of a document whose write
// path is deliberately the chat (T87). This dialog is not that: it edits launch
// *facts* the daemon resolves from env and config.json, a form is the honest
// writer of a config file, and nothing here can reach the instructions route.
// Keeping the affordances in a separate file is what lets the drawer's guard
// stay a one-line ban instead of a judgement call.
//
// Everything the dialog says is aimed at the two ways a settings screen can
// quietly lie:
//
// - a save that changes nothing visible. Settings apply to the NEXT incarnation
//   — the running one keeps its context window — so the scope is stated in the
//   intro, again on the saved banner, and the restart that applies it is one
//   tap away (routed through the drawer's own confirmation, because ending a
//   context window is the same decision however it is reached).
// - a save an export shadows. Each field carries its provenance (the daemon's
//   `source`), a shadowed one gets its own warning, and fields prefill from the
//   `stored` layer rather than the effective value — prefilling the export's
//   text into a field that writes config.json would bake the export into the
//   file on save, which is exactly what the API's stored-only write refuses to
//   do on its own.
//
// State is self-contained: a per-request sequence discards slow replies that
// lost the race (the same guard the drawer's reads use), and the busy flags are
// reset unconditionally in `finally` — a flag stuck true would wedge the
// dialog's reentry guards for the life of the component.

import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { mdiRestart } from '@mdi/js'
import { fetchAssistantConfig, setAssistantConfig } from '../api.js'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  running: { type: Boolean, default: false },
  // What the running incarnation was launched with (the status's `settings`),
  // for the current-vs-next line — a pending change with nothing naming the
  // difference reads as either a no-op or a lie.
  runningSettings: { type: Object, default: null },
})

const emit = defineEmits(['update:modelValue', 'restart'])

const open = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value),
})

const SETTING_KEYS = ['model', 'harness', 'preset', 'agents']
const SETTING_HINTS = {
  model: 'the model the session runs, handed to the harness verbatim',
  harness: 'agent harness (e.g. claude, codex, pi)',
  preset: 'named startup preset from the host’s presets file',
  agents: 'comma-separated preset names the session may fan out to',
}

const reply = ref(null)
const problem = ref(null)
const loading = ref(false)
const saving = ref(false)
const saved = ref(false)
// True when every key the save changed came back still pinned by an export —
// the one case the offered restart provably does nothing: the incumbent's
// context window would be paid and the exported value would run again. The
// reply carries everything needed to tell (`changed` + per-key `source`), so
// the banner can be as honest as the field captions above it.
const savedShadowed = ref(false)
const fields = reactive({ model: '', harness: '', preset: '', agents: '' })

let seq = 0
let disposed = false

function stale(mine) {
  return disposed || mine !== seq
}

// Re-read on every open rather than once: the file this edits has three other
// writers (an export at daemon restart, the API, another browser), so the last
// open's answer may already be the old truth.
watch(
  () => props.modelValue,
  (opened) => {
    if (!opened) return
    saved.value = false
    savedShadowed.value = false
    problem.value = null
    load()
  },
)

async function load() {
  if (loading.value) return
  const mine = ++seq
  loading.value = true
  try {
    const config = await fetchAssistantConfig()
    if (stale(mine)) return
    reply.value = config
    problem.value = null
    prefill(config)
  } catch (exc) {
    if (stale(mine)) return
    problem.value = exc.message
  } finally {
    // Unconditionally: a reply that lost the race must still release the flag,
    // or the reentry guard above blocks every future load and the dialog shows
    // "reading…" until a remount. The flag gates only this dialog's widgets,
    // so a "stale" reset costs nothing.
    loading.value = false
  }
}

function prefill(config) {
  for (const key of SETTING_KEYS) {
    fields[key] = config.settings?.[key]?.stored || ''
  }
}

// Send only the keys that differ from the stored layer, empty field = clear the
// key: a whole-form write would stamp untouched keys with their own values, and
// the difference matters the day two writers patch different keys.
function changes() {
  const named = {}
  for (const key of SETTING_KEYS) {
    const stored = reply.value?.settings?.[key]?.stored || ''
    const edited = (fields[key] || '').trim()
    if (edited !== stored) named[key] = edited || null
  }
  return named
}

async function save() {
  if (saving.value) return
  const named = changes()
  if (!Object.keys(named).length) {
    open.value = false
    return
  }
  const mine = ++seq
  saving.value = true
  saved.value = false
  try {
    const config = await setAssistantConfig(named)
    if (stale(mine)) return
    reply.value = config
    problem.value = null
    saved.value = true
    const changed = config.changed || []
    savedShadowed.value =
      changed.length > 0 &&
      changed.every((key) => config.settings?.[key]?.source === 'env')
    prefill(config)
  } catch (exc) {
    if (stale(mine)) return
    // The daemon's own sentence — it names the field it refused. The form keeps
    // the operator's text so the refusal can be fixed rather than retyped.
    problem.value = exc.message
  } finally {
    // Unconditional, for load()'s reason: a wedged flag disables save forever.
    saving.value = false
  }
}

function restartNow() {
  open.value = false
  emit('restart')
}

function runningDiffers(key) {
  if (!props.running) return false
  const current = props.runningSettings?.[key] || null
  const next = reply.value?.settings?.[key]?.value || null
  return current !== next
}

onBeforeUnmount(() => {
  disposed = true
  seq += 1
})
</script>

<template>
  <v-dialog v-model="open" max-width="560">
    <v-card>
      <v-card-title class="text-title-medium">uber lmer settings</v-card-title>
      <v-card-text>
        <p class="text-body-small text-medium-emphasis mb-3">
          How the session is run — not what it is told (standing orders stay in
          the chat). Changes here apply to the <strong>next</strong> incarnation:
          the one running keeps its context window until you restart it.
        </p>

        <v-alert v-if="problem" type="warning" density="compact" class="mb-3">
          {{ problem }}
        </v-alert>

        <div
          v-if="loading && !reply"
          class="text-body-small text-medium-emphasis"
        >
          <v-progress-circular indeterminate size="12" width="2" class="me-2" />
          reading…
        </div>

        <template v-else-if="reply">
          <div v-for="key in SETTING_KEYS" :key="key" class="mb-2">
            <v-text-field
              v-model="fields[key]"
              :label="key"
              :hint="SETTING_HINTS[key]"
              density="compact"
              variant="outlined"
              clearable
              :disabled="saving"
            />
            <!-- The provenance line, per field: what will actually run and which
                 layer decided it. An emptied field falls back to whatever the
                 layer below says, so "unset" here means lmer's own resolution
                 (environment, preset, harness default) — not a platform value. -->
            <p class="text-body-small text-medium-emphasis mt-n2 mb-0">
              <template v-if="reply.settings[key].source === 'env'">
                ⚠️ an exported environment variable pins this to
                “{{ reply.settings[key].value }}” — what you save here is
                persisted but has no effect until that export is removed.
              </template>
              <template v-else-if="reply.settings[key].value">
                next incarnation runs “{{ reply.settings[key].value }}”
                (from {{ reply.settings[key].source }})
              </template>
              <template v-else>
                unset — the session decides (harness default)
              </template>
              <template v-if="runningDiffers(key)">
                · running now:
                {{ props.runningSettings?.[key] || 'unset' }}
              </template>
            </p>
          </div>

          <!-- Two banners, because a save has two truths: the shadowed one must
               not offer a restart that provably runs the exported value again —
               the operator would pay a context window for nothing. -->
          <v-alert
            v-if="saved && savedShadowed"
            type="warning"
            density="compact"
          >
            Saved to config.json, but every value you changed is still pinned by
            an exported environment variable — a restart would run the export,
            not what you saved. Remove the export first.
          </v-alert>
          <v-alert v-else-if="saved" type="success" density="compact">
            Saved. It applies when a fresh uber lmer starts — restart to apply it
            now, or let the change wait for the next incarnation.
            <template #append>
              <v-btn
                size="small"
                variant="tonal"
                :prepend-icon="mdiRestart"
                @click="restartNow"
              >restart</v-btn>
            </template>
          </v-alert>
        </template>
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn variant="tonal" @click="open = false">close</v-btn>
        <v-btn
          color="primary"
          variant="tonal"
          :loading="saving"
          :disabled="loading || !reply"
          @click="save"
        >save</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
