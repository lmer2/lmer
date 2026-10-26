<script setup>
// Sessions per target (#374, spec §13.F): every launch of this run's
// `_runs/<slug>/` group, oldest start first, each addressed by its uid.
//
// Under schema 2 a target is not one record. Each launch against it writes its
// own `<uid>[--<name>]/` directory, so "the run" in the fleet is one launch and
// the others — the review after the develop, the follow-up somebody started from
// a phone — were invisible from here unless this orchestrator happened to track
// them too. This lists them all, from the mirror, beside what only the platform
// knows about each (live, tracked, its project).
//
// Mounted by RunDetail ONLY for a row that carries a uid. A schema-1 run is one
// shared record per target, so there are no launches to list, and a legacy fleet
// must render and request exactly what it did before.
//
// It fetches its own data, like RelatedRuns: a directory walk per group is the
// wrong cost for the fleet poll, for something one open run renders.
//
// Two decisions are load-bearing, and both are about what a row is NOT:
//
// - A resume posts THAT ROW's uid and THAT ROW's own project context —
//   `workspace_id`, `home_revision`, and the reconfirmation it asks for — read
//   off the daemon's reply for the row, never the parent fleet row's (astra
//   PR6). Two launches of one group can be homed in different projects at
//   different revisions, and #542's request binding refuses a borrowed context
//   as a stale one. The host/project/slug are the parent's: they name the group,
//   which is the same for every row.
// - An untracked launch is read-only and says so. There is no adopt control
//   here: this is a read view of the work repo (§13.C), and adopting is the
//   "spawn or adopt" screen's verb, with its own checks.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { mdiPlayOutline } from '@mdi/js'
import CopyButton from './CopyButton.vue'
import { fetchRunSessions, resumeRun } from '../api.js'
import { ago } from '../format.js'
import { eligibleSlots, projectColor } from '../projects.js'

const props = defineProps({
  run: { type: Object, required: true },
  slots: { type: Array, default: () => [] },
  now: { type: Number, default: () => Date.now() },
})

// A resume started a session somewhere, so the shell's fleet is stale; the
// shell owns the reload, exactly as for RunDetail's own continue verb.
const emit = defineEmits(['changed'])

// The refusal codes RunDetail's continue verb already handles (lmer_platform.resume).
// The two "one more field" ones are requests, not failures, and read as such.
const NEEDS_REPO_URL = 'repo_url_required'
const NEEDS_DIRECTION = 'direction_required'
const NEEDS_UNSIGNED_DECISION = 'unsigned_session'
const ASKS_FOR_A_FIELD = [NEEDS_REPO_URL, NEEDS_DIRECTION, NEEDS_UNSIGNED_DECISION]

const UID_PATTERN = /^[0-9a-f]{32}$/

const loaded = ref(false)
const problem = ref(null)
const reply = ref(null)
const sessions = computed(() => reply.value?.sessions || [])

// One launch's form is open at a time: the fields below belong to it, and a
// second open form would invite typing a direction into the wrong launch.
const openUid = ref(null)
const confirmed = ref(false)
const slot = ref(null)
const direction = ref('')
const repoUrl = ref('')
const repoUrlAsked = ref(false)
const busy = ref(false)
// What the daemon answered for each launch, keyed by uid, so a warning or a
// refusal stays on the row it is about after another row's form is opened.
const outcomes = ref({})

let disposed = false

// The short form of a uid is for reading; the copy button carries all of it.
// A timestamp-id launch of a schema-1 tree is shown as it is: it is short already.
function shortId(uid) {
  return UID_PATTERN.test(uid || '') ? uid.slice(0, 8) : uid
}

// Said on the row, because a row with no verb reads as a broken list otherwise.
function readOnlyReason(row) {
  if (row.tracked === false) {
    return 'read-only — this orchestrator does not track this launch, so it is '
      + 'listed here and not continued from here'
  }
  return 'read-only — a launch of a slug-keyed group: the platform tracks the '
    + 'group by its slug, not this launch by its id'
}

// Only where it differs from the run this page is about: the common case is one
// project for the whole group, and a chip on every row would say nothing.
function homedElsewhere(row) {
  return (row.workspace_id || null) !== (props.run.workspace_id || null)
}

function outcomeOf(row) {
  return outcomes.value[row.uid] || null
}

function slotsFor(row) {
  return eligibleSlots(props.slots, row.workspace_id)
}

// #542's gate, read off the row: a launch that moved projects is continued only
// once the operator has confirmed its current one. Handed off is handed off — a
// second tap before the fleet poll catches up would only earn a refusal.
//
// The reason travels with the verdict (issue 617): a disabled control that says
// nothing is what made the answer box's equivalent gate read as broken.
function reasonFor(row) {
  if (outcomeOf(row)?.started) return 'A session already started for this launch.'
  if (busy.value) return 'Starting a session…'
  if (row.confirm_project && !confirmed.value) {
    return `Tick "Authorize this launch in ${row.workspace_name || 'its project'}" above to resume.`
  }
  return null
}

function canSubmit(row) {
  return !reasonFor(row)
}

function toggle(row) {
  if (openUid.value === row.uid) {
    openUid.value = null
    return
  }
  openUid.value = row.uid
  confirmed.value = false
  slot.value = null
  direction.value = ''
  repoUrl.value = ''
  repoUrlAsked.value = false
}

// The launch a resume addresses: the group's key from the parent row, the
// launch's uid and the launch's own project context from the daemon's reply.
// Built field by field so nothing of the parent row's context can ride along.
function launchOf(row) {
  return {
    host: props.run.host,
    project: props.run.project,
    slug: props.run.slug,
    uid: row.uid,
    workspace_id: row.workspace_id,
    home_revision: row.home_revision,
  }
}

async function resume(row, allowUnsignedSource = null) {
  if (!canSubmit(row)) return
  busy.value = true
  const outcome = { started: null, error: null, needs: null, source: null }
  try {
    outcome.started = await resumeRun(launchOf(row), {
      direction: direction.value,
      repoUrl: repoUrl.value,
      allowUnsignedSource,
      confirmProject: !!row.confirm_project && confirmed.value,
      slot: slot.value,
    })
    emit('changed')
  } catch (exc) {
    // Nothing typed is cleared: the platform records nothing before the spawn,
    // so a refused direction exists nowhere but this box.
    outcome.error = exc.message
    outcome.needs = exc.code || null
    outcome.source = exc.code === NEEDS_UNSIGNED_DECISION ? exc.source : null
    if (exc.code === NEEDS_REPO_URL) repoUrlAsked.value = true
  } finally {
    if (!disposed) {
      outcomes.value = { ...outcomes.value, [row.uid]: outcome }
      busy.value = false
    }
  }
}

function continueUnsigned(row) {
  const source = outcomeOf(row)?.source?.uid
  if (source) return resume(row, source)
}

async function load() {
  try {
    const answer = await fetchRunSessions(props.run)
    if (!disposed) reply.value = answer
  } catch (exc) {
    // Shown, not swallowed: an empty group and a failed read look identical
    // otherwise, and one of them means the mirror could not be read.
    if (!disposed) problem.value = exc.message
  } finally {
    if (!disposed) loaded.value = true
  }
}

onMounted(load)
onBeforeUnmount(() => {
  disposed = true
})
</script>

<template>
  <div class="sessions-per-target">
    <div class="section-title">launches of this target</div>
    <v-card class="mb-3">
      <v-card-text>
        <p v-if="!loaded" class="text-medium-emphasis">reading…</p>

        <v-alert v-else-if="problem" type="error" density="compact">
          {{ problem }}
        </v-alert>

        <template v-else>
          <!-- Not an error: a launch that pushed seconds ago can be one pull
               away, and a group that has not pushed at all is not there yet. -->
          <p v-if="!sessions.length" class="text-medium-emphasis">
            No launch of this target is in the platform's copy of the work repo
            yet. A session pushes its record when it commits, and this list
            catches up on the next pull.
          </p>

          <div
            v-for="(row, index) in sessions"
            :key="row.uid"
            class="session-row py-2"
            :class="{ 'border-t': index > 0 }"
          >
            <div class="d-flex flex-wrap align-center ga-2">
              <code :title="row.uid">{{ shortId(row.uid) }}</code>
              <CopyButton :text="row.uid" label="Copy uid" />
              <span v-if="row.name_suffix" class="text-medium-emphasis">--{{ row.name_suffix }}</span>
              <v-chip v-if="row.current" size="small" color="primary" variant="tonal">this run</v-chip>
              <v-chip
                v-if="row.live"
                class="session-live"
                size="small"
                color="success"
                variant="tonal"
                :title="row.live_session || ''"
              >live</v-chip>
              <v-chip
                v-if="homedElsewhere(row)"
                size="small"
                variant="flat"
                :color="projectColor(row.workspace_color)"
              >{{ row.workspace_id ? (row.workspace_name || 'Unavailable project') : 'Unassigned' }}</v-chip>
              <v-spacer />
              <v-btn
                v-if="row.tracked"
                :prepend-icon="mdiPlayOutline"
                :disabled="busy"
                size="small"
                variant="tonal"
                color="primary"
                @click="toggle(row)"
              >resume</v-btn>
            </div>

            <div class="d-flex flex-wrap ga-3 text-body-small text-medium-emphasis mt-1">
              <span v-if="row.status">{{ row.status }}</span>
              <span v-if="row.record_state">{{ row.record_state }}</span>
              <span v-if="row.started_at" :title="row.started_at">started {{ ago(row.started_at, now) }}</span>
            </div>

            <p v-if="!row.tracked" class="text-body-small text-medium-emphasis mt-1">
              {{ readOnlyReason(row) }}
            </p>

            <!-- The verb's form, under the row it continues. Two taps rather than
                 one, unlike the page's own continue: this is a list of launches,
                 and a mis-aimed tap would start a session for the wrong one. -->
            <div v-if="row.tracked && openUid === row.uid" class="mt-3">
              <v-select
                v-if="slots.length"
                v-model="slot"
                :items="slotsFor(row)"
                item-title="name"
                item-value="name"
                label="Service slot"
                clearable
                hint="Leave empty to reuse this launch's remembered slot."
                persistent-hint
                class="mb-3"
              />
              <v-textarea
                v-model="direction"
                :disabled="busy"
                :error="outcomeOf(row)?.needs === NEEDS_DIRECTION"
                label="direction"
                hint="the next session's seed. Required to reopen a launch that is already complete"
                persistent-hint
                rows="2"
                auto-grow
                max-rows="6"
                autocapitalize="sentences"
                class="mb-3"
              />
              <v-text-field
                v-if="repoUrlAsked"
                v-model="repoUrl"
                :disabled="busy"
                :error="outcomeOf(row)?.needs === NEEDS_REPO_URL"
                label="repository URL"
                placeholder="https://git.example.com/group/project.git"
                class="mb-3"
              />
              <!-- Directly above the control it gates (issue 617), and labelled as
                   the required step it is. -->
              <v-checkbox
                v-if="row.confirm_project"
                v-model="confirmed"
                :label="`Authorize this launch in ${row.workspace_name || 'its project'} — required to resume`"
                :hint="`This launch was moved to ${row.workspace_name || 'another project'}, so resuming it needs a confirmation that it runs with that project's current settings.`"
                persistent-hint
                class="mb-2"
              />
              <div class="d-flex flex-wrap align-center ga-3">
                <v-btn
                  :loading="busy"
                  :disabled="!canSubmit(row)"
                  color="primary"
                  @click="resume(row)"
                >resume this launch</v-btn>
                <span
                  v-if="reasonFor(row)"
                  class="launch-reason text-body-small text-medium-emphasis"
                >{{ reasonFor(row) }}</span>
              </div>
            </div>

            <template v-if="outcomeOf(row)">
              <!-- The daemon's own words: every refusal names the reason and the
                   way through. A request for one more field is not painted red. -->
              <v-alert
                v-if="outcomeOf(row).error"
                :type="ASKS_FOR_A_FIELD.includes(outcomeOf(row).needs) ? 'warning' : 'error'"
                density="compact"
                class="mt-3"
              >
                {{ outcomeOf(row).error }}
                <div
                  v-if="outcomeOf(row).needs === NEEDS_UNSIGNED_DECISION && outcomeOf(row).source?.uid"
                  class="mt-2"
                >
                  <v-btn color="warning" variant="tonal" size="small" @click="continueUnsigned(row)">
                    continue fresh
                  </v-btn>
                </div>
              </v-alert>
              <v-alert v-if="outcomeOf(row).started" type="success" density="compact" class="mt-3">
                Session {{ outcomeOf(row).started.session?.session_id }} started —
                {{ outcomeOf(row).started.note }}
              </v-alert>
              <!-- A2's concurrency note (spec §6.3): other live sessions on the same
                   target. Never enforced — a fork is two rows with one label — so
                   it is said, beside the launch it was about. -->
              <v-alert
                v-if="outcomeOf(row).started?.warning"
                type="warning"
                density="compact"
                class="mt-3"
              >{{ outcomeOf(row).started.warning }}</v-alert>
            </template>
          </div>
        </template>
      </v-card-text>
    </v-card>
    <p v-if="reply?.note" class="text-body-small text-medium-emphasis mb-3">{{ reply.note }}</p>
  </div>
</template>
