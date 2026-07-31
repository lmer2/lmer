<script setup>
// What this run is about, in this orchestrator's own words (T52).
//
// The operator asked: "I want the platform to be able to maintain metadata about a
// run. This can be title and description to start with... These are meant for the
// user to be able to quickly identify what a run is about."
//
// Two fields and one warning, and the warning is the part that needs explaining.
// This is *platform* state, because the platform never writes run state (spec
// D3): its copy of the work repo is a mirror the daemon force-resets on every
// pull, so a title stored beside the run's own state would be destroyed by the
// next fetch, silently. The consequence is that a title belongs to this
// orchestrator rather than to the run — it is not in anybody else's fleet view,
// the agent in the container cannot see it, and it goes when the run is
// forgotten. Somebody will otherwise assume it syncs, so the panel says so
// instead of leaving it to the API docs.
//
// The description is rendered rather than shown verbatim, and it goes through the
// shared renderer for exactly the reason that component's header gives: an agent
// writes this field, which makes it untrusted text becoming markup, and there is
// one place in this app allowed to do that. The title is not rendered — it is a
// label, it has already been collapsed to one line by the daemon, and a bold
// half-sentence in a heading would be the tab pretending to be a document.
//
// Read and write are separate modes rather than a live form. The agent owns this
// text as much as the operator does, so a form sitting open over a value that has
// since changed would save the stale one back without anybody noticing; opening
// the editor takes a fresh copy of what is on screen.

import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref } from 'vue'
import { mdiCheck, mdiPencilOutline } from '@mdi/js'
import { fetchRunMeta, setRunMeta } from '../api.js'
import { ago } from '../format.js'

// Deferred into its own chunk, like every other consumer of it — Markdown.vue's
// header says why, and why nothing is drawn until it lands.
const Markdown = defineAsyncComponent(() => import('./Markdown.vue'))

const props = defineProps({
  run: { type: Object, required: true },
  now: { type: Number, default: () => Date.now() },
})

const loaded = ref(false)
const record = ref({ title: '', description: '', updated_at: null, empty: true })
// The daemon's own bounds, so the counter on a field and the refusal on the
// server cannot disagree. Null until the first read lands, which is also why the
// editor is not offered before then: a maxlength this view invented would be a
// second copy of a number that already exists.
const limits = ref({ title: null, description: null })
const problem = ref(null)

const editing = ref(false)
const saving = ref(false)
const draftTitle = ref('')
const draftDescription = ref('')

let disposed = false

const empty = computed(() => !record.value.title && !record.value.description)
const canSave = computed(() => !saving.value)

async function load() {
  try {
    const reply = await fetchRunMeta(props.run)
    if (disposed) return
    record.value = reply.meta
    limits.value = reply.limits || limits.value
    problem.value = null
  } catch (exc) {
    if (disposed) return
    problem.value = exc.message
  } finally {
    if (!disposed) loaded.value = true
  }
}

function edit() {
  draftTitle.value = record.value.title || ''
  draftDescription.value = record.value.description || ''
  problem.value = null
  editing.value = true
}

async function save() {
  if (!canSave.value) return
  saving.value = true
  problem.value = null
  try {
    // What comes back is what was stored, not what was typed — the title arrives
    // collapsed to the line it will be shown as — so the reply replaces the local
    // copy rather than the draft being trusted as the new truth.
    const reply = await setRunMeta(props.run, {
      title: draftTitle.value,
      description: draftDescription.value,
    })
    if (disposed) return
    record.value = reply.meta
    limits.value = reply.limits || limits.value
    editing.value = false
  } catch (exc) {
    // Nothing typed is cleared: a refusal names a bound that was exceeded, and
    // taking the text away would take the operator's only copy of it.
    if (!disposed) problem.value = exc.message
  } finally {
    if (!disposed) saving.value = false
  }
}

onMounted(load)
onBeforeUnmount(() => {
  disposed = true
})
</script>

<template>
  <div>
    <div class="section-title">what this run is about</div>

    <v-alert v-if="problem" type="error" density="compact" class="mb-3">
      {{ problem }}
    </v-alert>

    <v-card class="mb-3">
      <v-card-text>
        <p v-if="!loaded" class="text-medium-emphasis">reading…</p>

        <!-- Reading. The empty case says what the fields are for rather than
             showing two blank rows, because a run with nothing written about it
             is the normal state and an empty card reads as a broken one. -->
        <template v-else-if="!editing">
          <template v-if="empty">
            <p class="text-medium-emphasis mb-3">
              Nothing is written about this run yet. A title and a description
              here are how you tell it apart from every other run in the list —
              the orchestrator sets them when it starts a run, and you can write
              or correct them at any time.
            </p>
          </template>
          <template v-else>
            <p v-if="record.title" class="text-body-large font-weight-medium mb-2">
              {{ record.title }}
            </p>
            <Markdown
              v-if="record.description"
              :text="record.description"
              class="text-body-medium mb-2"
            />
            <p
              v-if="record.updated_at"
              class="text-body-small text-medium-emphasis"
              :title="record.updated_at"
            >written {{ ago(record.updated_at, now) }}</p>
          </template>

          <v-btn
            :prepend-icon="mdiPencilOutline"
            :disabled="!loaded"
            color="primary"
            variant="tonal"
            class="mt-2"
            @click="edit"
          >{{ empty ? 'describe this run' : 'edit' }}</v-btn>
        </template>

        <!-- Writing. Both bounds come off the daemon's reply, so the counter
             stops the text at the same place the server would refuse it. -->
        <template v-else>
          <v-text-field
            v-model="draftTitle"
            :disabled="saving"
            :maxlength="limits.title"
            :counter="limits.title"
            label="title"
            hint="one line, and the thing you will recognise this run by in the list"
            persistent-hint
            autocapitalize="sentences"
            class="mb-3"
          />
          <v-textarea
            v-model="draftDescription"
            :disabled="saving"
            :maxlength="limits.description"
            :counter="limits.description"
            label="description"
            hint="what this run is for, and anything you want to remember about it. Markdown is rendered"
            persistent-hint
            rows="4"
            auto-grow
            max-rows="14"
            autocapitalize="sentences"
            class="mb-3"
          />
          <div class="d-flex flex-wrap ga-2">
            <v-btn
              :prepend-icon="mdiCheck"
              :loading="saving"
              :disabled="!canSave"
              color="primary"
              @click="save"
            >save</v-btn>
            <v-btn :disabled="saving" variant="tonal" @click="editing = false">
              cancel
            </v-btn>
          </div>
          <p class="text-body-small text-medium-emphasis mt-3">
            Clearing both fields removes the note entirely.
          </p>
        </template>
      </v-card-text>
    </v-card>

    <!-- The surprising half of this tab, so it is on the page and not in the API
         docs. It is also why this is not "the run's title": the run does not have
         one, this orchestrator does. -->
    <p class="text-body-small text-medium-emphasis">
      This is the platform's own note about the run, kept here and not in the work
      repo — the platform never writes a run's state, and its copy of the repo is
      reset on every pull, so anything written there would be destroyed by the next
      one. That means this text is local to this orchestrator: it is not in anyone
      else's fleet view, the session in the container cannot read it, and it is
      removed when you forget the run.
    </p>
  </div>
</template>
