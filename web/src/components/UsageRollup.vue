<script setup>
// Usage per period for one repo project, `<host>/<project>` (issue 315). Read on
// mount and on selection, never on the poll; absent until the first answer.
// Why: docs/USAGE-TRACKING.md, "How each surface reads".
import { computed, onMounted, ref, watch } from 'vue'
import { fetchUsageRollup } from '../api.js'
import { bucketLine, BUCKETS, costLabel, costTitle, exact, tokens } from '../usage.js'

const props = defineProps({
  // The fleet's rows, read only for which repositories they are against.
  runs: { type: Array, default: () => [] },
})

const PERIODS = ['day', 'week', 'month']

// The dash is how the daemon spells a session that has not committed a run
// identity yet (fleetMap.js recognises the same one); it names no repository.
const projects = computed(() => [...new Set(props.runs
  .filter((run) => [run.host, run.project].every((field) => field && field !== '—'))
  .map((run) => [run.host, run.project].join('/')))].sort())

const project = ref(null)
const period = ref(PERIODS[0])
const from = ref('')
const to = ref('')
const reply = ref(null)
const error = ref(null)
const loading = ref(false)
let sequence = 0

async function load() {
  if (!project.value) return
  const mine = ++sequence
  loading.value = true
  try {
    const answer = await fetchUsageRollup({
      project: project.value, period: period.value, from: from.value, to: to.value,
    })
    if (mine !== sequence) return
    reply.value = answer
    error.value = null
  } catch (exc) {
    if (mine === sequence) error.value = exc.message
  } finally {
    if (mine === sequence) loading.value = false
  }
}

// The first repository until the operator picks one, and kept while it is still
// in the fleet: a poll that reorders the rows must not move the panel.
watch(projects, (choices) => {
  if (!choices.includes(project.value)) project.value = choices[0] || null
}, { immediate: true })
watch([project, period, from, to], load)
onMounted(load)

const periods = computed(() => reply.value?.periods || [])
</script>

<template>
  <section v-if="projects.length && reply" aria-label="Usage rollup" class="usage-rollup mb-5">
    <h2 class="text-title-medium">Usage</h2>
    <div class="d-flex flex-wrap align-center ga-3 my-2">
      <v-select
        v-model="project"
        :items="projects"
        label="repository"
        density="compact"
        hide-details
        class="usage-rollup-project"
      />
      <v-btn-toggle v-model="period" density="compact" mandatory variant="text" aria-label="period">
        <v-btn v-for="value in PERIODS" :key="value" :value="value" size="small">{{ value }}</v-btn>
      </v-btn-toggle>
      <v-text-field v-model="from" type="date" label="from" density="compact" hide-details clearable class="usage-rollup-date" />
      <v-text-field v-model="to" type="date" label="to" density="compact" hide-details clearable class="usage-rollup-date" />
    </div>

    <template v-if="reply">
      <p v-if="!periods.length" class="text-medium-emphasis">
        No saved usage records for {{ reply.project || project }} in this range.
      </p>
      <v-table v-else density="compact" class="usage-rollup-table">
        <thead>
          <tr>
            <th>{{ period }}</th>
            <th v-for="bucket in BUCKETS" :key="bucket.key" class="text-end">{{ bucket.label }}</th>
            <th class="text-end">cost</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in periods" :key="row.period">
            <tr class="usage-period">
              <td>{{ row.period }}</td>
              <td
                v-for="bucket in BUCKETS"
                :key="bucket.key"
                class="text-end"
                :title="exact(row.totals?.[bucket.key])"
              >{{ tokens(row.totals?.[bucket.key]) }}</td>
              <td class="text-end" :title="costTitle(row.cost)">{{ costLabel(row.cost) || '' }}</td>
            </tr>
            <!-- This host's live numbers that run ahead of the saved records: not
                 in any record yet, so labelled and never folded into the row. -->
            <tr v-if="row.provisional" class="usage-provisional text-medium-emphasis">
              <td :title="bucketLine(row.provisional)">+ provisional</td>
              <td
                v-for="bucket in BUCKETS"
                :key="bucket.key"
                class="text-end"
                :title="exact(row.provisional[bucket.key])"
              >{{ tokens(row.provisional[bucket.key]) }}</td>
              <td />
            </tr>
          </template>
        </tbody>
      </v-table>
      <p v-if="error" class="usage-rollup-error text-body-small text-medium-emphasis mt-2 mb-0">
        could not refresh: {{ error }}
      </p>
      <p class="text-body-small text-medium-emphasis mt-2 mb-0">
        {{ loading ? 'reading… · ' : '' }}{{ reply.records }} record(s) · thinking is part of output
        <template v-if="reply.invalid_records"> · {{ reply.invalid_records }} invalid and not counted</template>
        <template v-if="reply.prices_problem"> · price table: {{ reply.prices_problem }}</template>
      </p>
    </template>
  </section>
</template>

<style scoped>
.usage-rollup-project {
  max-width: 320px;
}
.usage-rollup-date {
  max-width: 180px;
}
</style>
