<script setup>
// Runs that belong together, and the tap that moves between them (T53).
//
// The motivating case is the followup cycle: an assistant starts a develop run
// and later a review run against the same target, and from then on the operator
// crosses between those two repeatedly. Unrelated, every crossing is a scroll
// through a list of runs whose names differ by one word.
//
// Why this is not a tab, which is the whole reason it lives at the bottom of the
// detail view instead of beside the other panels: the point of a switcher is
// switching *while looking at something else*. A related-runs tab would be a tab
// you leave the terminal to visit in order to go somewhere else — one tap to find
// it, one to leave. Always visible at the end of the page, it is where a thumb
// already is on a phone, and it is behind nothing.
//
// The relation is symmetric and has no direction: the daemon stores each one once
// under a canonical pair key, so a run this one was related *to* and a run related
// *from* are the same thing here and there is no "incoming" case to render.
//
// Two states per entry, and the difference is load-bearing:
//
// - a run this orchestrator tracks is a switch. Tapping it asks the shell to open
//   that run, which is the shell's business — this component knows one run.
// - a run it does not track is a key with a hint and no way through. That is not
//   defensive politeness: the fleet view is what the shell selects a run out of,
//   so a "switch" to a run that is not in it would land on an empty page. The
//   daemon says which case each is (`tracked`), because relating a run before
//   adopting it is allowed and forgetting a run leaves its relations alone.
//
// Removal is offered on both, and it is the only way to clear a relation naming a
// run nobody tracks any more.

import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { mdiLinkVariantPlus, mdiSwapHorizontal } from '@mdi/js'
import { fetchRunRelations, fetchState, relateRun, unrelateRun } from '../api.js'

const props = defineProps({
  run: { type: Object, required: true },
})

// The shell owns which run is selected (it is what the fleet list, the drawer and
// this all change), so switching is a request rather than an action taken here.
const emit = defineEmits(['open'])

const loaded = ref(false)
const entries = ref([])
const problem = ref(null)
// The daemon's own cap, so what this says about a full switcher comes from the
// number that will actually refuse the next relation.
const limit = ref(null)
const busy = ref(false)

// The picker, and it is loaded on demand: relating is the rare act and the fleet
// payload is the biggest one this app fetches. Opening a run must not cost a
// second copy of it.
const adding = ref(false)
const fleet = ref([])
const fleetError = ref(null)
const loadingFleet = ref(false)
const choice = ref(null)

let disposed = false

// Said next to the run rather than left in the API docs, because a key with no
// link reads as a broken list otherwise — and the honest reason is specific.
const NOT_HERE_HINT = 'not on this host: this orchestrator does not track this '
  + 'run, so there is no row to open. Adopt it from "spawn or adopt" and it '
  + 'becomes a switch — or remove the relation'

const ADD_HINT = 'only runs this orchestrator already tracks, and relating is '
  + 'symmetric: the run you pick gets this one at the bottom of its own page too'

const full = computed(() => !!limit.value && entries.value.length >= limit.value)

function keyOf(run) {
  return `${run.host}/${run.project}/${run.slug}`
}

// What the picker offers: the fleet, minus this run and minus everything already
// related. A menu whose items are all refusals is a menu that reads as broken.
//
// Named by the title when there is one *and* by the slug either way: the title is
// what identifies a run (T65), and the slug is what makes two runs with the same
// title — a develop and its review, which is this feature's own case — tellable
// apart in a menu.
const choices = computed(() => {
  const taken = new Set([keyOf(props.run), ...entries.value.map((e) => e.key)])
  return fleet.value
    .filter((run) => !taken.has(keyOf(run)))
    .map((run) => ({
      title: run.title ? `${run.title} — ${run.slug}` : (run.label || run.slug),
      value: keyOf(run),
    }))
})

function apply(reply) {
  entries.value = reply.relations || []
  limit.value = reply.limits?.relations || limit.value
  problem.value = null
}

async function load() {
  try {
    const reply = await fetchRunRelations(props.run)
    if (disposed) return
    apply(reply)
  } catch (exc) {
    // Shown, not swallowed: no relations and a failed read look identical
    // otherwise, and one of them is a switcher that has stopped working.
    if (!disposed) problem.value = exc.message
  } finally {
    if (!disposed) loaded.value = true
  }
}

async function openPicker() {
  adding.value = true
  if (fleet.value.length || loadingFleet.value) return
  loadingFleet.value = true
  fleetError.value = null
  try {
    const state = await fetchState()
    if (disposed) return
    fleet.value = state.runs || []
  } catch (exc) {
    if (!disposed) fleetError.value = exc.message
  } finally {
    if (!disposed) loadingFleet.value = false
  }
}

async function add() {
  const picked = fleet.value.find((run) => keyOf(run) === choice.value)
  if (!picked || busy.value) return
  busy.value = true
  try {
    // The reply is this run's relations as they now are, so nothing is guessed
    // locally and a relation somebody else added in the meantime arrives with it.
    apply(await relateRun(props.run, picked))
    choice.value = null
    adding.value = false
  } catch (exc) {
    if (!disposed) problem.value = exc.message
  } finally {
    if (!disposed) busy.value = false
  }
}

async function remove(entry) {
  if (busy.value) return
  busy.value = true
  try {
    apply(await unrelateRun(props.run, entry))
  } catch (exc) {
    if (!disposed) problem.value = exc.message
  } finally {
    if (!disposed) busy.value = false
  }
}

function switchTo(entry) {
  // Never for a run this fleet does not carry: the shell selects out of the fleet
  // list, so it would have nothing to show and would land on a blank page.
  if (!entry.tracked) return
  emit('open', { host: entry.host, project: entry.project, slug: entry.slug })
}

onMounted(load)
onBeforeUnmount(() => {
  disposed = true
})
</script>

<template>
  <div class="mt-4">
    <div class="section-title">related runs</div>

    <v-alert v-if="problem" type="error" density="compact" class="mb-3">
      {{ problem }}
    </v-alert>

    <v-card class="mb-3">
      <v-card-text>
        <p v-if="!loaded" class="text-medium-emphasis">reading…</p>

        <template v-else>
          <!-- The empty case says what the element is for. A run related to
               nothing is the normal state, and an empty card reads as broken. -->
          <p v-if="!entries.length" class="text-medium-emphasis mb-3">
            Nothing is related to this run yet. Relating two runs — a develop run
            and the review of it, say — puts each at the bottom of the other's
            page, so moving between them is one tap instead of a scroll through
            the fleet.
          </p>

          <div v-else class="d-flex flex-wrap ga-2 mb-3">
            <template v-for="entry in entries" :key="entry.key">
              <!-- Tracked: a switch. Closable, because the X is how a relation
                   goes away and there is no other verb for it. -->
              <v-chip
                v-if="entry.tracked"
                :prepend-icon="mdiSwapHorizontal"
                color="primary"
                variant="tonal"
                closable
                :disabled="busy"
                :title="entry.key"
                @click="switchTo(entry)"
                @click:close="remove(entry)"
              >{{ entry.title || entry.slug }}</v-chip>
              <!-- Untracked: the key, a hint, and no way through. The key rather
                   than the slug, because there is no row anywhere else in this app
                   that says which run this is. -->
              <v-chip
                v-else
                variant="tonal"
                closable
                :disabled="busy"
                :title="NOT_HERE_HINT"
                @click:close="remove(entry)"
              >{{ entry.key }}</v-chip>
            </template>
          </div>

          <!-- Adding is the rare half, so it stays a button until it is asked
               for: the switcher is what this element is for, and a select sitting
               open under it would be the tallest thing on the page. -->
          <template v-if="adding">
            <v-alert v-if="fleetError" type="error" density="compact" class="mb-3">
              {{ fleetError }}
            </v-alert>
            <v-select
              v-model="choice"
              :items="choices"
              :loading="loadingFleet"
              :disabled="busy"
              label="relate to"
              :hint="ADD_HINT"
              persistent-hint
              class="mb-3"
            />
            <div class="d-flex flex-wrap ga-2">
              <v-btn
                :prepend-icon="mdiLinkVariantPlus"
                :loading="busy"
                :disabled="!choice"
                color="primary"
                @click="add"
              >relate</v-btn>
              <v-btn :disabled="busy" variant="tonal" @click="adding = false">
                cancel
              </v-btn>
            </div>
          </template>

          <v-btn
            v-else
            :prepend-icon="mdiLinkVariantPlus"
            :disabled="busy || full"
            color="primary"
            variant="tonal"
            size="small"
            @click="openPicker"
          >relate another run</v-btn>

          <p v-if="full" class="text-body-small text-medium-emphasis mt-3">
            This run is related to as many as the daemon will hold ({{ limit }}).
            Remove one to add another — this is a switcher for a handful of runs
            that belong together, not a way to group the fleet.
          </p>
        </template>
      </v-card-text>
    </v-card>

    <!-- The surprising half, on the page rather than in the API docs, for the
         reason the meta tab says it there: somebody will otherwise assume this
         travels with the run. -->
    <p class="text-body-small text-medium-emphasis">
      Which runs belong together is this orchestrator's own note, kept here and not
      in the work repo — the platform never writes a run's state, and its copy of
      the repo is reset on every pull, so anything written there would be destroyed
      by the next one. That means these relations are local to this orchestrator:
      they are not in anyone else's fleet view, and the session in the container
      cannot read them. Forgetting a run does not remove them, so a relation can
      name a run this host no longer tracks.
    </p>
  </div>
</template>
