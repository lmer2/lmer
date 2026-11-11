<script setup>
// How the supervising session is *run* — model, harness, preset, agents fan-out —
// per platform instance (issue #234). Platform settings shows it inline, one card
// per group (issue #652): the host passes `groups`, owns the card and its Save,
// and calls `save()` / `reset()` here.
//
// Its own component, and the split is load-bearing rather than tidy: the drawer
// (AssistantChat.vue) is pinned by tests/test_platform_web_assistant.py to carry
// NO input affordance at all, because the tempting thing to grow there is a
// textarea for the standing orders — a second writer of a document whose write
// path is deliberately the chat (T87). This form is not that: it edits launch
// *facts* the daemon resolves from env and config.json, a form is the honest
// writer of a config file, and nothing here can reach the instructions route.
// Keeping the affordances in a separate file is what lets the drawer's guard
// stay a one-line ban instead of a judgement call.
//
// Everything the form says is aimed at the two ways a settings screen can
// quietly lie:
//
// - a save that changes nothing visible. Settings apply to the NEXT incarnation
//   — the running one keeps its context window — so the scope is stated in the
//   intro and again on the saved banner. A host that knows the running session
//   can offer the restart that applies it (`allowRestart`, `restart`); Platform
//   settings does not, and points at the project chat instead.
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
// form's reentry guards for the life of the component.

import { computed, onBeforeUnmount, onMounted, reactive, ref, shallowRef, watch } from 'vue'
import { mdiRestart } from '@mdi/js'
import { fetchAssistantConfig, setAssistantConfig } from '../api.js'
import { LAUNCH_SETTING_HINTS, LAUNCH_SETTING_KEYS, LAUNCH_SETTING_LABELS } from '../launchSettings.js'
import { asNumber, numberReading } from '../numberField.js'
import { applyChatSettings } from '../operatorChat.js'
import { assistantName } from '../ratchet.js'

const props = defineProps({
  groups: { type: Array, default: () => ['launch', 'conversation', 'reminders'] },
  running: { type: Boolean, default: false },
  allowRestart: { type: Boolean, default: true },
  // What the running incarnation was launched with (the status's `settings`),
  // for the current-vs-next line — a pending change with nothing naming the
  // difference reads as either a no-op or a lie.
  runningSettings: { type: Object, default: null },
})

const emit = defineEmits(['restart', 'state'])

const show = (group) => props.groups.includes(group)

const SETTING_KEYS = [...LAUNCH_SETTING_KEYS, 'threads']

// The daemon's own reminder intervals (#244, #317, #446). A different kind of
// setting from the five above and captioned as one: they are read on the next
// detection tick rather than at the next launch, so nothing here is waiting on a
// restart. Each names the group it is served under and the config key it writes,
// because the two spellings differ on purpose — the group is the operator's
// vocabulary and the key is the file's.
const TICK_FIELDS = [
  {
    field: 'checkinWindow',
    group: 'checkin',
    key: 'window_seconds',
    api: 'checkin_window_seconds',
    min: 0,
    label: 'Check-in window (seconds)',
    unit: 'seconds',
    singular: 'second',
    hint: 'How long a run may go unlooked-at before it is named in a digest. 0 turns check-ins off.',
  },
  {
    field: 'nudgeAfter',
    group: 'nudge',
    key: 'after_seconds',
    api: 'nudge_after_seconds',
    min: 0,
    label: 'Digest reminder after (seconds)',
    unit: 'seconds',
    singular: 'second',
    hint: 'How long unretrieved digests wait, beside a quiet session, before one reminder is typed into it. 0 turns reminders off.',
  },
  {
    field: 'nudgePending',
    group: 'nudge',
    key: 'pending_threshold',
    api: 'nudge_pending_threshold',
    min: 1,
    label: 'Digest reminder threshold (digests)',
    unit: 'digests',
    singular: 'digest',
    hint: 'How many digests it takes. At least 1 — use the interval above to turn reminders off.',
  },
]

// What a blank number field means here, and it is the same thing a blank launch
// field means: unset this layer and let the one below show through. Sent as null,
// which is what the write route reads as "clear".
const UNSET = ''

//: The percent the slider starts at when nothing is stored. Not a claim about
//  anything — the switch beside it is off, so no value is in effect.
const DEFAULT_ROTATION_PERCENT = 70

//: config key -> where the reply reports it, for the saved banner's branch. The
//  threshold rides the same table: what the banner has to know about all four is
//  identical, and it is the provenance rather than the widget.
const TICK_SOURCES = {
  ...Object.fromEntries(TICK_FIELDS.map((spec) => [spec.api, [spec.group, spec.key]])),
  rotation_context_threshold: ['rotation', 'context_threshold'],
}
const SETTING_LABELS = { ...LAUNCH_SETTING_LABELS, threads: 'Chat threads' }
const SETTING_HINTS = { ...LAUNCH_SETTING_HINTS, threads: 'Slack or matrix thread permalinks, one per line.' }

const reply = ref(null)
const problem = ref(null)
const loading = ref(false)
const saving = ref(false)
const saved = ref(false)
const savedLaunch = ref(false)
const savedDisplay = ref(false)
// True when the save touched a setting the daemon re-reads on its next tick and
// the value it will read is the one that was saved. Its own flag because the
// banner beside it offers a restart, and offering one for a change that needs
// none is the same lie as withholding one that does.
const savedTick = ref(false)
// True when a reminder setting was persisted under an export that still wins.
const savedTickShadowed = ref(false)
// True when every key the save changed came back still pinned by an export —
// the one case the offered restart provably does nothing: the incumbent's
// context window would be paid and the exported value would run again. The
// reply carries everything needed to tell (`changed` + per-key `source`), so
// the banner can be as honest as the field captions above it.
const savedShadowed = ref(false)
const fields = reactive({
  model: '', harness: '', preset: '', agents: '', effort: '', threads: '',
  showTranscript: false,
  tintOperator: true,
  enterSends: true,
  checkinWindow: UNSET, nudgeAfter: UNSET, nudgePending: UNSET,
  rotationOn: false, rotationPercent: DEFAULT_ROTATION_PERCENT,
})

let seq = 0
let disposed = false
const threadLines = (text) => (text || '').split('\n').map((line) => line.trim()).filter(Boolean)
// Each field read the way Save reads it, so whitespace or another spelling of the
// same number is not an edit.
function readings() {
  const out = {}
  for (const key of SETTING_KEYS) {
    out[key] = key === 'threads' ? threadLines(fields.threads).join('\n') : String(fields[key] || '').trim()
  }
  out.showTranscript = fields.showTranscript
  out.tintOperator = fields.tintOperator
  out.enterSends = fields.enterSends
  for (const spec of TICK_FIELDS) {
    out[spec.field] = numberReading(fields[spec.field])
  }
  out.rotationOn = fields.rotationOn
  out.rotationPercent = fields.rotationOn ? Number(fields.rotationPercent) : null
  return out
}
const FIELD_GROUP = {
  ...Object.fromEntries(SETTING_KEYS.map((key) => [key, 'launch'])),
  showTranscript: 'conversation',
  tintOperator: 'conversation',
  enterSends: 'conversation',
  ...Object.fromEntries(TICK_FIELDS.map((spec) => [spec.field, 'reminders'])),
  rotationOn: 'reminders',
  rotationPercent: 'reminders',
}
// The fields as last prefilled. Dirty is measured against these, not against the
// stored layer: that is the raw file, and a value the form cannot represent (a
// hand-written "300", an out-of-range 0) is not an edit the operator made.
const baseline = shallowRef(readings())
function touched() {
  const now = readings()
  return Object.keys(now).filter((field) => now[field] !== baseline.value[field])
}
const dirty = computed(() => !!reply.value && touched().some((field) => show(FIELD_GROUP[field])))
// Synchronous, so a host that discards and then asks again reads the new answer.
watch([dirty, saving], () => emit('state', { dirty: dirty.value, saving: saving.value }), { immediate: true, flush: 'sync' })

function stale(mine) {
  return disposed || mine !== seq
}

onMounted(load)

async function load() {
  if (loading.value) return
  const mine = ++seq
  loading.value = true
  try {
    const config = await fetchAssistantConfig()
    if (stale(mine)) return
    reply.value = config
    problem.value = null
    // An edit typed while the read was out stays, now compared against the new
    // reply; every other field takes the new reply, so Save cannot send a value
    // another writer has since replaced.
    const changed = touched()
    // The switch and its slider are one setting: an edit to either keeps both.
    if (changed.includes('rotationOn') || changed.includes('rotationPercent')) changed.push('rotationOn', 'rotationPercent')
    const kept = Object.fromEntries(changed.map((field) => [field, fields[field]]))
    prefill(config)
    Object.assign(fields, kept)
  } catch (exc) {
    if (stale(mine)) return
    problem.value = exc.message
  } finally {
    // Unconditionally: a reply that lost the race must still release the flag,
    // or the reentry guard above blocks every future load and the form shows
    // "reading…" until a remount. The flag gates only this form's widgets,
    // so a "stale" reset costs nothing.
    loading.value = false
  }
}

// `apply` is off only for a reply already in hand: a sibling card may have saved
// newer chat settings since, and this browser already shows those.
function prefill(config, apply = true) {
  // The effective value is the stored layer's own reading here: the chat group
  // has no environment layer to shadow it, so nothing an export set can be
  // baked into the file by saving this.
  fields.showTranscript = config.chat?.show_transcript?.value === true
  fields.tintOperator = config.chat?.tint_operator?.value !== false
  fields.enterSends = config.chat?.enter_sends?.value !== false
  if (apply) applyChatSettings(config)
  for (const spec of TICK_FIELDS) {
    const stored = config[spec.group]?.[spec.key]?.stored
    fields[spec.field] = stored === null || stored === undefined ? UNSET : String(stored)
  }
  // Stored, never effective — for the launch fields' reason: an export's value
  // prefilled into a widget that writes config.json bakes the export into the
  // file the first time anything is saved.
  const threshold = config.rotation?.context_threshold?.stored ?? 0
  fields.rotationOn = threshold > 0
  fields.rotationPercent = threshold > 0 ? threshold : DEFAULT_ROTATION_PERCENT
  for (const key of SETTING_KEYS) {
    const stored = config.settings?.[key]?.stored
    fields[key] = key === 'threads' && Array.isArray(stored) ? stored.join('\n') : stored || ''
  }
  baseline.value = readings()
}

// Drop unsaved edits. Re-read rather than re-prefill: the file this edits has
// other writers (an export at daemon restart, the API, another browser), so the
// reply in hand may already be the old truth.
function reset() {
  saved.value = false
  problem.value = null
  if (reply.value) prefill(reply.value, false)
  load()
}

// Send only the keys that differ from the stored layer, empty field = clear the
// key: a whole-form write would stamp untouched keys with their own values, and
// the difference matters the day two writers patch different keys. Only the
// groups this card shows: a hidden field is never the reason a save is refused.
function diff() {
  const named = {}
  if (show('launch')) for (const key of SETTING_KEYS) {
    if (key === 'threads') {
      const stored = reply.value?.settings?.threads?.stored || []
      const edited = threadLines(fields.threads)
      if (JSON.stringify(edited) !== JSON.stringify(stored)) named.threads = edited
      continue
    }
    const stored = reply.value?.settings?.[key]?.stored || ''
    const edited = (fields[key] || '').trim()
    if (edited !== stored) named[key] = edited || null
  }
  if (show('conversation')) {
    if (fields.showTranscript !== (reply.value?.chat?.show_transcript?.value === true)) {
      named.chat_show_transcript = fields.showTranscript
    }
    if (fields.tintOperator !== (reply.value?.chat?.tint_operator?.value !== false)) {
      named.chat_tint_operator = fields.tintOperator
    }
    if (fields.enterSends !== (reply.value?.chat?.enter_sends?.value !== false)) {
      named.chat_enter_sends = fields.enterSends
    }
  }
  if (!show('reminders')) return { named, refused: null }
  for (const spec of TICK_FIELDS) {
    const stored = reply.value?.[spec.group]?.[spec.key]?.stored ?? null
    const edited = String(fields[spec.field] ?? '').trim()
    if (edited === UNSET) {
      if (stored !== null) named[spec.api] = null
      continue
    }
    const value = asNumber(edited)
    // The daemon's own floor, per field, so a 0 in the threshold is refused here
    // rather than coming back as a 400 the operator has to read to discover.
    if (!Number.isSafeInteger(value) || value < spec.min) {
      return { named, refused: `${spec.label} must be a whole number of ${spec.min} or more.` }
    }
    if (value !== stored) named[spec.api] = value
  }
  // The switch is the off state and 0 is how the daemon spells it, so an
  // operator who turned it off writes 0 rather than clearing the key: the two
  // have the same effect today and only one of them says what was meant.
  const threshold = fields.rotationOn ? Number(fields.rotationPercent) : 0
  if (threshold !== (reply.value?.rotation?.context_threshold?.stored ?? 0)) {
    named.rotation_context_threshold = threshold
  }
  return { named, refused: null }
}

function changes() {
  const { named, refused } = diff()
  if (refused === null) return named
  problem.value = refused
  return null
}

async function save() {
  if (saving.value) return
  const named = changes()
  if (!named) return
  if (!Object.keys(named).length) return
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
    const launchChanges = changed.filter((key) => SETTING_KEYS.includes(key))
    savedLaunch.value = launchChanges.length > 0
    savedDisplay.value = ['chat_show_transcript', 'chat_tint_operator', 'chat_enter_sends'].some((key) => changed.includes(key))
    // Split by provenance, not by which fields moved: a reminder setting an
    // export still pins is persisted and has no effect, so promising it "applies
    // on the next check" is the same lie the launch banner already refuses to
    // tell. The caption under the field says so; the banner has to agree.
    const tickChanges = changed.filter((key) => key in TICK_SOURCES)
    const shadowed = tickChanges.filter((key) => {
      const [group, name] = TICK_SOURCES[key]
      return config[group]?.[name]?.source === 'env'
    })
    savedTick.value = tickChanges.length > shadowed.length
    savedTickShadowed.value = shadowed.length > 0
    savedShadowed.value =
      launchChanges.length > 0 &&
      launchChanges.every((key) => config.settings?.[key]?.source === 'env')
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
  emit('restart')
}

function tickSetting(spec) {
  return reply.value?.[spec.group]?.[spec.key] || null
}

function runningDiffers(key) {
  if (!props.running) return false
  const current = props.runningSettings?.[key] || null
  const next = reply.value?.settings?.[key]?.value || null
  if (key === 'threads') return JSON.stringify(current || []) !== JSON.stringify(next || [])
  return current !== next
}

onBeforeUnmount(() => {
  disposed = true
  seq += 1
})

defineExpose({ save, reset })
</script>

<template>
  <div class="assistant-settings">
    <p v-if="show('launch')" class="text-body-small text-medium-emphasis mb-3">
      How the session is run — not what it is told (standing orders stay in
      the chat). Launch changes apply to the <strong>next</strong> incarnation:
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
      <!-- Three groups, each with its own scope line, because they apply at
           three different moments: the next incarnation, now, and the
           daemon's next check. -->
      <div v-if="show('launch')" class="settings-group settings-launch">
        <p class="text-body-small text-medium-emphasis mb-3">
          Applies to the next incarnation. Empty fields fall back to lmer's own
          resolution: environment, preset, then harness default.
        </p>
        <div v-for="key in SETTING_KEYS" :key="key" class="settings-field">
          <v-textarea
            v-if="key === 'threads'"
            v-model="fields.threads"
            :label="SETTING_LABELS.threads"
            :hint="SETTING_HINTS.threads"
            persistent-hint
            rows="3"
            auto-grow
            density="compact"
            variant="outlined"
            clearable
            :disabled="saving"
          />
          <v-text-field
            v-else
            v-model="fields[key]"
            :label="SETTING_LABELS[key]"
            :hint="SETTING_HINTS[key]"
            persistent-hint
            density="compact"
            variant="outlined"
            clearable
            :disabled="saving"
          />
          <!-- The provenance line, per field: what will actually run and which
               layer decided it. -->
          <p class="text-body-small text-medium-emphasis setting-source">
            <template v-if="key === 'threads'">
              Next incarnation: connects to {{ (reply.settings?.[key]?.value || []).length }} threads
              <template v-if="runningDiffers(key)">
                · running now: {{ (props.runningSettings?.threads || []).join(', ') || 'none' }}
              </template>
            </template>
            <template v-else-if="reply.settings?.[key]?.source === 'env'">
              ⚠️ an exported environment variable pins this to
              “{{ reply.settings?.[key]?.value }}” — what you save here is
              persisted but has no effect until that export is removed.
            </template>
            <template v-else-if="reply.settings?.[key]?.value">
              Next incarnation: “{{ reply.settings?.[key]?.value }}”
              (from {{ reply.settings?.[key]?.source }})
            </template>
            <template v-else>
              Next incarnation: unset — the session decides (harness default)
            </template>
            <template v-if="key !== 'threads' && runningDiffers(key)">
              · running now:
              {{ props.runningSettings?.[key] || 'unset' }}
            </template>
          </p>
        </div>
      </div>

      <div v-if="show('conversation')" class="settings-group settings-conversation">
        <p class="text-body-small text-medium-emphasis mb-3">
          Applies now, to this pane; other open browsers follow within a poll.
        </p>
        <!-- The uber lmer pane's one view switch (issue 488). Platform state,
             not this browser's: the operator debugging a stuck session from a
             phone should find it where they left it on the desktop. -->
        <div class="settings-field">
          <v-switch
            v-model="fields.showTranscript"
            color="primary"
            density="compact"
            hide-details
            :disabled="saving"
            :label="`Show ${assistantName}'s working transcript in the chat`"
          />
          <p class="text-body-small text-medium-emphasis setting-source">
            Off, the chat is the conversation only: your messages, and what {{ assistantName }}
            posted, asked and answered. On, its tool calls, watch events, hook
            re-prompts and unposted working notes appear muted beside them.
          </p>
        </div>
        <div class="settings-field">
          <v-switch
            v-model="fields.tintOperator"
            class="tint-operator"
            color="primary"
            density="compact"
            hide-details
            :disabled="saving"
            label="Tint my messages with the project color"
          />
          <p class="text-body-small text-medium-emphasis setting-source">
            On, your messages in each chat take that project's hue, kept readable in
            both color schemes. Off, they use one color everywhere.
          </p>
        </div>
        <!-- Issue 663. composerKeys.js applies it to all three message boxes. -->
        <div class="settings-field">
          <v-switch
            v-model="fields.enterSends"
            class="enter-sends"
            color="primary"
            density="compact"
            hide-details
            :disabled="saving"
            label="Enter sends messages"
          />
          <p class="text-body-small text-medium-emphasis setting-source">
            On, Enter sends and Shift+Enter starts a new line. Off, Enter starts a
            new line and Ctrl+Enter (Cmd on a Mac) sends. On a phone or other touch
            device Enter always starts a new line; use the send button.
          </p>
        </div>
      </div>

      <div v-if="show('reminders')" class="settings-group settings-reminders">
        <p class="text-body-small text-medium-emphasis mb-3">
          Reminders the daemon types into the chat. These apply on its next
          check, in about half a minute — no restart, and the running session
          keeps its context window.
        </p>

        <div v-for="spec in TICK_FIELDS" :key="spec.api" class="settings-field">
          <v-text-field
            v-model="fields[spec.field]"
            :label="spec.label"
            type="number"
            :min="spec.min"
            step="1"
            :hint="spec.hint"
            persistent-hint
            density="compact"
            variant="outlined"
            clearable
            :disabled="saving"
          />
          <p class="text-body-small text-medium-emphasis setting-source">
            <template v-if="tickSetting(spec)?.source === 'env'">
              ⚠️ an exported environment variable pins this to
              “{{ tickSetting(spec).value }}” — what you save here is
              persisted but has no effect until that export is removed.
            </template>
            <template v-else>
              <!-- min 0 is exactly the fields whose 0 is the off switch. -->
              <template v-if="spec.min === 0 && tickSetting(spec)?.value === 0">In effect now: off</template>
              <template v-else>
                In effect now: {{ tickSetting(spec)?.value }}
                {{ tickSetting(spec)?.value === 1 ? spec.singular : spec.unit }}
              </template>
              (from {{ tickSetting(spec)?.source }})
            </template>
          </p>
        </div>

        <!-- The context reminder. A switch beside the slider rather than a
             0 on it: the API's off-switch is the value 0, and a slider whose
             lowest position means "never" reads as "always" to everyone who
             has ever used one. -->
        <div class="settings-field">
          <v-switch
            v-model="fields.rotationOn"
            color="primary"
            density="compact"
            hide-details
            :disabled="saving"
            label="Recommend a rotation when the context window fills"
          />
          <v-slider
            v-model="fields.rotationPercent"
            :min="1"
            :max="100"
            :step="1"
            :disabled="saving || !fields.rotationOn"
            label="Context used"
            thumb-label
            hide-details
            density="compact"
            class="mt-2"
          >
            <template #append>
              <span class="text-body-small">{{ fields.rotationPercent }}%</span>
            </template>
          </v-slider>
          <p class="text-body-small text-medium-emphasis setting-source">
            <template v-if="reply.rotation?.context_threshold?.source === 'env'">
              ⚠️ an exported environment variable pins this to
              “{{ reply.rotation.context_threshold.value }}” — what you save
              here is persisted but has no effect until that export is removed.
            </template>
            <template v-else-if="reply.rotation?.context_threshold?.value">
              In effect now: at
              {{ reply.rotation.context_threshold.value }}% of the context
              window used, {{ assistantName }} is told once — and again at most every
              fifteen minutes while it stays there — to rotate itself at its
              next breakpoint. It decides when; nothing here ends a session.
              Needs the status strip, which LMER_PLATFORM_STATUS_STRIP=0 turns off.
            </template>
            <template v-else>
              In effect now: off — {{ assistantName }} is never told its context window is filling.
              Needs a harness that publishes the reading on its status line, and the
              status strip left on.
            </template>
          </p>
        </div>
      </div>

      <!-- Two banners, because a save has two truths: the shadowed one must
           not offer a restart that provably runs the exported value again —
           the operator would pay a context window for nothing. -->
      <v-alert
        v-if="saved && savedShadowed"
        type="warning"
        density="compact"
      >
        Saved to config.json, but every launch value you changed is still pinned by
        an exported environment variable — a restart would run the export,
        not what you saved. Remove the export first.
        <p v-if="savedDisplay">Chat display applies now.</p>
        <p v-if="savedTick">Reminder settings apply on the daemon's next check.</p>
        <p v-if="savedTickShadowed">
          ⚠️ a reminder setting you changed is still pinned by an exported
          environment variable — it is saved, but the daemon goes on reading
          the export.
        </p>
      </v-alert>
      <v-alert v-else-if="saved && !savedLaunch" type="success" density="compact">
        Saved. No session restart is needed.
        <p v-if="savedDisplay">Chat display applies now.</p>
        <p v-if="savedTick">Reminder settings apply on the daemon's next check.</p>
        <p v-if="savedTickShadowed">
          ⚠️ a reminder setting you changed is still pinned by an exported
          environment variable — it is saved, but the daemon goes on reading
          the export.
        </p>
      </v-alert>
      <v-alert v-else-if="saved" type="success" density="compact">
        Saved. It applies when a fresh {{ assistantName }} starts — restart to apply it
        from the project chat, or let the change wait for the next incarnation.
        <p v-if="savedDisplay">Chat display applies now.</p>
        <p v-if="savedTick">Reminder settings apply on the daemon's next check.</p>
        <p v-if="savedTickShadowed">
          ⚠️ a reminder setting you changed is still pinned by an exported
          environment variable — it is saved, but the daemon goes on reading
          the export.
        </p>
        <template v-if="allowRestart" #append>
          <v-btn
            size="small"
            variant="tonal"
            :prepend-icon="mdiRestart"
            @click="restartNow"
          >restart</v-btn>
        </template>
      </v-alert>
    </template>
  </div>
</template>
