<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { mdiFormatListBulleted, mdiMagnify, mdiPencil } from '@mdi/js'
import { adoptRun, fetchCandidates, fetchSpawnOptions, spawnSession } from '../api.js'

const emit = defineEmits(['changed'])

const mode = ref('spawn')

// `taskdef` keeps its default rather than waiting for discovery: the form is
// usable on the first frame, and the default survives a host whose taskdef list
// comes back empty or without it.
// `agents` is an array here and a comma-delimited string on the wire, which is
// the shape `lmer --agents` takes.
// `model` is free text and stays that way: it becomes the session's
// LMER_LLM_NAME, and the daemon cannot enumerate the models a harness serves —
// only the harness can, and it does so by rejecting what it does not know.
const spawn = ref({
  taskdef: 'develop', target: '', repo_url: '', preset: '', agents: [],
  model: '', ports: 0,
})
const busy = ref(false)
const error = ref(null)
const notice = ref(null)
// A spawn that succeeded and still lost something. Today that is one case: a run
// the daemon could not identify is never recorded, so the session runs normally
// and its row leaves the fleet view when it exits — a failure with no symptom
// until it is too late to act on, which is exactly why it is shown here and not
// left in the daemon's log.
const warning = ref(null)

// Suggestions for the three name fields, never their permitted values — see
// loadOptions. Empty until discovery answers, and legitimately empty forever on
// a host that can enumerate nothing.
const options = ref({ taskdefs: [], presets: [] })

// Blank is not a neutral choice in the repository field, so the hint names the
// outcome instead of calling the field optional: the run's identity is derived
// from this URL, or from the target when it is a resource URL, and with neither
// the run is never recorded — the session runs normally and its row leaves the
// fleet view when it exits. A constant rather than a 200-character attribute.
const REPO_URL_HINT = 'identifies the run. Blank falls back to the daemon\'s '
  + '$LMER_REPO_URL, then to the target when it is an MR, PR or issue URL — with '
  + 'none of those the run is not tracked and disappears from this view when the '
  + 'session exits'

const candidates = ref([])
const candidatesError = ref(null)
const filter = ref('')
const loadingCandidates = ref(false)

const untracked = computed(() =>
  candidates.value.filter((candidate) => {
    if (candidate.tracked) return false
    if (!filter.value?.trim()) return true
    return candidate.rel_path.toLowerCase().includes(filter.value.trim().toLowerCase())
  }),
)

async function loadCandidates() {
  loadingCandidates.value = true
  candidatesError.value = null
  try {
    const payload = await fetchCandidates()
    candidates.value = payload.candidates || []
  } catch (exc) {
    candidatesError.value = exc.message
  } finally {
    loadingCandidates.value = false
  }
}

// The last lookup's arguments, so the debounced watcher below cannot re-ask the
// same question — the prefill assigns repo_url, which is itself watched.
let lastOptionsQuery = null

// Discovery is a convenience and is treated like one. A host cannot enumerate
// every taskdef it can run — the work repo's own tiers come off a mirror that
// may be stale, and may hold nothing for the project this spawn names — so
// these lists are completions for fields that stay typeable, and a failure is
// swallowed rather than shown: the operator who knows the name they want must
// not be told the form is broken.
//
// The target and the repository URL are sent because the work repo's
// project-scoped taskdefs are filed under the run's host and project, which the
// daemon cannot know until this form names a repository. That is what makes the
// menu the menu for the spawn being composed rather than for the daemon's
// default one.
async function loadOptions() {
  const target = typed(spawn.value.target)
  const repoUrl = typed(spawn.value.repo_url)
  const query = JSON.stringify([target, repoUrl])
  if (query === lastOptionsQuery) return
  lastOptionsQuery = query
  try {
    const payload = await fetchSpawnOptions({ target, repoUrl })
    options.value = {
      taskdefs: payload.taskdefs || [],
      presets: payload.presets || [],
    }
    // The repository URL is not a suggestion: it is the value the daemon would
    // have used itself for a request that names none, so it is filled in rather
    // than described. An empty box beside a daemon that has a URL is a box that
    // misrepresents what leaving it blank does. Only into a field nobody has
    // touched — this resolves after the first frame, so the operator may already
    // be typing, and their value wins.
    if (payload.repo_url && !typed(spawn.value.repo_url)) {
      spawn.value.repo_url = payload.repo_url
      // Recorded as already asked, because this assignment cannot change the
      // answer: the daemon fell back to this very URL to decide which work-repo
      // project tier the list above came from. Without this the prefill would
      // trip its own watcher into re-asking an identical question.
      lastOptionsQuery = JSON.stringify([target, typed(spawn.value.repo_url)])
    }
  } catch {
    // Keep the lists as they are. Every field below still accepts free text.
    // Forgotten rather than remembered, so the next keystroke retries instead of
    // leaving one failed lookup to silence the menu for the rest of the dialog.
    lastOptionsQuery = null
  }
}

// The taskdef list follows the target because the work repo's project tier does.
// Debounced, and on a timer rather than on blur: the field is typed into, and one
// request per keystroke would ask the daemon to walk its mirror for every
// character of a URL nobody has finished pasting yet.
const OPTIONS_REFRESH_DELAY_MS = 400
let optionsTimer = null

watch(
  [() => spawn.value.target, () => spawn.value.repo_url],
  () => {
    if (optionsTimer) clearTimeout(optionsTimer)
    optionsTimer = setTimeout(loadOptions, OPTIONS_REFRESH_DELAY_MS)
  },
)

onMounted(() => {
  loadOptions()
  if (mode.value === 'adopt') loadCandidates()
})

onUnmounted(() => {
  if (optionsTimer) clearTimeout(optionsTimer)
})

function switchTo(next) {
  mode.value = next
  error.value = null
  notice.value = null
  warning.value = null
  if (next === 'adopt' && !candidates.value.length) loadCandidates()
}

// A combobox hands back whatever it holds: the picked item, the typed string,
// or null once it has been cleared. Everything read out of one goes through
// here so a cleared field is an absent field rather than "null" on the wire.
function typed(value) {
  return typeof value === 'string' ? value.trim() : ''
}

// The taskdef field is a v-select, an operator decision: the list is static and
// rarely changes, and a menu reads better than a text box with completions.
//
// The tradeoff being accepted, stated once here because it is the thing that
// will eventually bite: the host's list is *not* the set of taskdefs a session
// can run. Only some live in the code repo, LMER_TASKDEF_PATHS can name
// anything, and the work repo's own tiers are read off the daemon's mirror —
// which lags the remote by a poll interval, may not be cloned at all, and holds
// a project tier only for projects it has. So an operator can still arrive
// wanting a taskdef this menu does not have, and this dialog is the UI's only
// way to start a session. That is precisely why T37 made it a combobox.
//
// Two things keep the reversal from costing that outright. The items always
// include the current value, so the default (and anything already chosen)
// cannot be dropped by a discovery that does not list it; and the pencil
// switches the field to plain text, which is the combobox's freedom kept as a
// deliberate act rather than as the default state of the field.
const taskdefFreeText = ref(false)

const taskdefItems = computed(() => {
  const listed = options.value.taskdefs || []
  const current = typed(spawn.value.taskdef)
  return current && !listed.includes(current) ? [current, ...listed] : listed
})

// The wire form of the agents selection: `lmer --agents` takes comma-delimited
// preset names and splits on commas itself, so a chip the operator typed with a
// comma in it separates two agents exactly as it reads.
const agentsSelection = computed(() =>
  (spawn.value.agents || [])
    .flatMap((entry) => typed(entry).split(','))
    .map((name) => name.trim())
    .filter(Boolean)
    .join(','),
)

const canSpawn = computed(() =>
  Boolean(typed(spawn.value.taskdef) && typed(spawn.value.target)),
)

async function doSpawn() {
  busy.value = true
  error.value = null
  notice.value = null
  warning.value = null
  try {
    const payload = {
      taskdef: typed(spawn.value.taskdef),
      target: typed(spawn.value.target),
      ports: Number(spawn.value.ports) || 0,
    }
    if (typed(spawn.value.repo_url)) payload.repo_url = typed(spawn.value.repo_url)
    // Omitted rather than sent empty: the daemon reads a present-but-blank
    // preset or agents as a value it must refuse, and an untouched field is not
    // a value at all.
    if (typed(spawn.value.preset)) payload.preset = typed(spawn.value.preset)
    if (agentsSelection.value) payload.agents = agentsSelection.value
    // Same rule for the model, and the same reason the field is not prefilled
    // with a guess: unset means "whatever this session's environment, preset or
    // harness settles on", which the session itself reports back once it knows.
    if (typed(spawn.value.model)) payload.model = typed(spawn.value.model)
    const result = await spawnSession(payload)
    notice.value = `spawned ${result.session_id}`
    // The daemon says so when a session it just started belongs to no run it can
    // record. Shown beside the success rather than instead of it — the session is
    // real and running — and it quotes the target it could derive nothing from,
    // which is the one field this clears on success.
    warning.value = result.warning || null
    spawn.value.target = ''
    emit('changed')
  } catch (exc) {
    error.value = exc.message
  } finally {
    busy.value = false
  }
}

async function doAdopt(candidate) {
  busy.value = true
  error.value = null
  try {
    await adoptRun(candidate)
    candidate.tracked = true
    emit('changed')
  } catch (exc) {
    error.value = exc.message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div>
    <v-tabs :model-value="mode" grow class="mb-3" @update:model-value="switchTo">
      <v-tab value="spawn">spawn</v-tab>
      <v-tab value="adopt">adopt</v-tab>
    </v-tabs>

    <v-alert v-if="error" type="error" class="mb-3">{{ error }}</v-alert>
    <v-alert v-if="notice" type="success" class="mb-3">{{ notice }}</v-alert>
    <v-alert v-if="warning" type="warning" class="mb-3">{{ warning }}</v-alert>

    <template v-if="mode === 'spawn'">
      <v-card class="mb-3">
        <v-card-text>
          <!-- A menu for the taskdef, a combobox for the preset and agent
               names below it. See taskdefFreeText for the tradeoff that makes
               those two different, and for what the pencil is doing here. -->
          <v-select
            v-if="!taskdefFreeText"
            v-model="spawn.taskdef"
            :items="taskdefItems"
            label="taskdef"
            hint="what this host can see, work repo included, for this target"
            persistent-hint
            :append-icon="mdiPencil"
            class="mb-3"
            @click:append="taskdefFreeText = true"
          />
          <!-- Same v-model, so switching between the two never costs the value.
               append (not append-inner) keeps the icon outside the field, where
               a click cannot also be a click on the menu activator. -->
          <v-text-field
            v-else
            v-model="spawn.taskdef"
            label="taskdef"
            placeholder="develop"
            hint="any name the container knows, incl. one only the work repo has"
            persistent-hint
            :append-icon="mdiFormatListBulleted"
            class="mb-3"
            @click:append="taskdefFreeText = false"
          />
          <v-text-field
            v-model="spawn.target"
            label="target"
            hint="issue or MR URL, branch, anything the taskdef expects"
            persistent-hint
            placeholder="https://git.example.com/group/project/-/work_items/1"
            class="mb-3"
          />
          <!-- Prefilled from the daemon's own $LMER_REPO_URL when it has one
               (GET /api/spawn-options): an empty box beside a daemon that has a
               URL misrepresents what leaving it blank does. What it does when
               there is nothing to fall back on is REPO_URL_HINT's subject. -->
          <v-text-field
            v-model="spawn.repo_url"
            label="repository URL"
            :hint="REPO_URL_HINT"
            persistent-hint
            placeholder="https://git.example.com/group/project.git"
            class="mb-3"
          />
          <v-combobox
            v-model="spawn.preset"
            :items="options.presets"
            label="preset"
            hint="a named startup config from this host's presets file (--preset)"
            persistent-hint
            clearable
            class="mb-3"
          />
          <v-combobox
            v-model="spawn.agents"
            :items="options.presets"
            label="agents"
            hint="presets the session may fan a task out to (--agents)"
            persistent-hint
            multiple
            chips
            closable-chips
            clearable
            class="mb-3"
          />
          <!-- Free text, and no menu behind it: the model is whatever the
               session's harness answers to, and this host has no list of those
               to offer. Blank leaves it to the daemon's environment, the
               preset, or the harness's own default — and the run's row shows
               whichever of those the session reports having resolved. -->
          <v-text-field
            v-model="spawn.model"
            label="model"
            hint="the session's LMER_LLM_NAME (--model); blank runs the harness default"
            persistent-hint
            placeholder="opus"
            class="mb-3"
          />
          <v-text-field
            v-model="spawn.ports"
            label="published ports"
            hint="for a session that needs to show you something"
            persistent-hint
            type="number"
            min="0"
            max="9"
            class="mb-4"
          />
          <v-btn
            color="primary"
            :loading="busy"
            :disabled="!canSpawn"
            @click="doSpawn"
          >spawn session</v-btn>
        </v-card-text>
      </v-card>
    </template>

    <template v-else>
      <v-card class="mb-3">
        <v-card-text>
          <!-- Adoption is opt-in per run, and the list is everyone's work: say so
               here rather than letting the list imply ownership. -->
          <p class="text-medium-emphasis mb-3">
            These are <strong>all</strong> runs in the shared work repo, including
            other people's. Adopting one adds it to this orchestrator's view;
            nothing appears here on its own.
          </p>
          <v-text-field
            v-model="filter"
            label="filter"
            placeholder="project or slug"
            :prepend-inner-icon="mdiMagnify"
            clearable
            hide-details
            class="mb-3"
          />
          <v-btn variant="tonal" :loading="loadingCandidates" @click="loadCandidates">
            reload
          </v-btn>
        </v-card-text>
      </v-card>

      <v-alert v-if="candidatesError" type="error" class="mb-3">
        {{ candidatesError }}
      </v-alert>

      <div
        v-if="!loadingCandidates && !untracked.length"
        class="text-center text-medium-emphasis py-8"
      >
        No untracked runs match.
      </div>

      <v-card v-for="candidate in untracked" :key="candidate.rel_path" class="mb-3">
        <v-card-text class="d-flex flex-wrap align-center ga-3">
          <div class="scroll-x flex-1-1"><code>{{ candidate.rel_path }}</code></div>
          <v-btn variant="tonal" :disabled="busy" @click="doAdopt(candidate)">
            adopt
          </v-btn>
        </v-card-text>
      </v-card>
    </template>
  </div>
</template>
