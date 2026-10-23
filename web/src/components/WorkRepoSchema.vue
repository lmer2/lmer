<script setup>
// The work repo as the schema validator sees it (#374, spec §13.F): the unnamed
// repos awaiting adoption and the validator's findings, both read off ONE
// `lmer-schema validate --json` run the daemon serves at GET /api/workrepo/schema.
//
// Mounted once, by App.vue's all-projects fleet, and only when the fleet payload
// says the mirror is in schema form (`mirror.form`). The work repo is shared by
// every workspace, so these panels belong to none of them; and a legacy tree is
// never validated, so a legacy fleet mounts nothing and asks nothing.
//
// Read views only (§13.C). Nothing here writes the work repo or adopts anything:
// a bucket is named with one commit by whoever chooses its short name, and the
// panel hands them the line to run.
//
// Three ways to read, and each has its own reason:
// - on mount;
// - when the mirror's HEAD moves, because a new commit is new findings;
// - on the re-validate control, which bypasses the daemon's cache. Staleness
//   findings age with the clock at an unchanged HEAD (astra PR7), so a path that
//   depends only on HEAD would show yesterday's verdict for as long as nobody
//   pushed.
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { mdiRefresh } from '@mdi/js'
import HostBuckets from './HostBuckets.vue'
import ValidatorFindings from './ValidatorFindings.vue'
import { fetchWorkRepoSchema } from '../api.js'

const props = defineProps({
  headSha: { type: String, default: null },
})

const loaded = ref(false)
const loading = ref(false)
const problem = ref(null)
const report = ref(null)

let disposed = false
// A HEAD move and a refresh can overlap; only the newest read may land, or an
// older verdict could replace a newer one on the screen.
let generation = 0

async function load({ refresh = false } = {}) {
  const mine = ++generation
  loading.value = true
  try {
    const reply = await (refresh ? fetchWorkRepoSchema({ refresh: true }) : fetchWorkRepoSchema())
    if (disposed || mine !== generation) return
    report.value = reply
    problem.value = null
  } catch (exc) {
    // Shown, not swallowed: a failed read and a clean tree must not look alike.
    if (!disposed && mine === generation) problem.value = exc.message
  } finally {
    if (!disposed && mine === generation) {
      loading.value = false
      loaded.value = true
    }
  }
}

watch(() => props.headSha, (next, previous) => {
  if (next !== previous) load()
})

onMounted(() => load())
onBeforeUnmount(() => {
  disposed = true
})
</script>

<template>
  <section aria-label="Work repo" class="work-repo-schema mb-5">
    <div class="d-flex align-center ga-2">
      <h2 class="text-title-medium">Work repo</h2>
      <v-spacer />
      <v-btn
        :prepend-icon="mdiRefresh"
        :loading="loading"
        size="small"
        variant="tonal"
        title="validate the mirror again now, rather than reuse the cached verdict"
        @click="load({ refresh: true })"
      >re-validate</v-btn>
    </div>

    <p v-if="!loaded" class="text-medium-emphasis">reading…</p>

    <v-alert v-if="problem" type="error" density="compact" class="my-3">
      {{ problem }}
    </v-alert>

    <template v-if="report">
      <HostBuckets :buckets="report.buckets || []" />
      <ValidatorFindings :form="report.form" :tier="report.tier" :document="report.document" />
      <p v-if="report.note" class="text-body-small text-medium-emphasis">{{ report.note }}</p>
    </template>
  </section>
</template>
