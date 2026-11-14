<script setup>
// One run's AI usage, session by session (issue 315), beside each session's
// run-dir record. Read once on mount, never on the poll; `live` and `record`
// are kept apart. Why: docs/USAGE-TRACKING.md, "How each surface reads".
import { computed, onMounted, ref } from 'vue'
import { mdiOpenInNew } from '@mdi/js'
import { fetchRunUsage } from '../api.js'
import { ago } from '../format.js'
import {
  BUCKETS, costLabel, costTitle, exact, harnessReported, tokens, wallClock,
} from '../usage.js'

const props = defineProps({
  run: { type: Object, required: true },
  now: { type: Number, default: () => Date.now() },
})

const reply = ref(null)
const error = ref(null)
const loaded = ref(false)

async function load() {
  try {
    reply.value = await fetchRunUsage(props.run)
  } catch (exc) {
    // Shown, not swallowed: "nothing recorded" and "could not read" look the same
    // otherwise, and only one of them means the run spent nothing.
    error.value = exc.message
  } finally {
    loaded.value = true
  }
}

onMounted(load)

function recordState(entry) {
  const record = entry.record
  if (!record) return { text: 'no saved record', invalid: false }
  if (!record.valid) return { text: 'invalid record', invalid: true, errors: record.errors || [] }
  const parts = [`saved ${ago(record.updated_at, props.now)}`]
  if (record.final) parts.push('final')
  else if (entry.record_behind) {
    parts.push(`unfinalised: ${exact(entry.record_behind.unpriced_weight_behind)} tokens behind`)
  } else parts.push('not final yet')
  if (record.partial) parts.push('partial')
  if (record.status && record.status !== 'unknown') parts.push(record.status)
  return { text: parts.join(' · '), invalid: false, title: record.updated_at || '' }
}

function rowLabel(row) {
  return [row.harness, row.model, row.agent].filter(Boolean).join(' · ')
}

const sessions = computed(() => (reply.value?.sessions || []).map((entry) => {
  const validRecord = entry.record?.valid ? entry.record : null
  const source = entry.live || validRecord
  return {
    key: entry.session,
    session: entry.session,
    url: entry.record?.url || null,
    state: recordState(entry),
    from: entry.live ? 'live transcripts' : validRecord ? 'saved record' : null,
    rows: source?.models || [],
    clock: wallClock(source, props.now),
    cost: source?.cost || null,
    reported: harnessReported(source?.harness_reported_cost),
  }
}))
</script>

<template>
  <div class="section-title">usage</div>
  <v-card class="usage-tab mb-3">
    <v-card-text>
      <p v-if="!loaded" class="text-medium-emphasis">reading…</p>

      <v-alert v-else-if="error" type="error" density="compact">{{ error }}</v-alert>

      <template v-else>
        <p v-if="!sessions.length" class="text-medium-emphasis">
          No usage has been collected or saved for this run yet.
        </p>
        <p v-if="reply?.invalid_records" class="text-body-small text-medium-emphasis">
          {{ reply.invalid_records }} saved record(s) failed validation and are not counted.
        </p>
        <p v-if="reply?.prices_problem" class="text-body-small text-medium-emphasis">
          price table: {{ reply.prices_problem }}
        </p>

        <section
          v-for="entry in sessions"
          :key="entry.key"
          class="usage-session mb-4"
          :aria-label="`usage of session ${entry.session}`"
        >
          <div class="d-flex flex-wrap align-center ga-3">
            <code class="scroll-x">{{ entry.session }}</code>
            <span
              class="usage-record-state text-body-small"
              :class="entry.state.invalid ? 'text-high-emphasis' : 'text-medium-emphasis'"
              :title="entry.state.title || ''"
            >{{ entry.state.text }}</span>
            <a
              v-if="entry.url"
              :href="entry.url"
              target="_blank"
              rel="noopener"
              class="usage-record-link text-body-small"
            >record<v-icon :icon="mdiOpenInNew" size="x-small" class="ms-1" /></a>
          </div>
          <ul v-if="entry.state.invalid && entry.state.errors.length" class="usage-record-errors text-body-small">
            <li v-for="(problem, index) in entry.state.errors" :key="index">{{ problem }}</li>
          </ul>

          <div v-if="entry.clock.length" class="d-flex flex-wrap ga-3 text-body-small text-medium-emphasis mt-1">
            <span v-for="part in entry.clock" :key="part.key" :title="part.title">{{ part.text }}</span>
          </div>

          <v-table v-if="entry.rows.length" density="compact" class="usage-rows mt-2">
            <thead>
              <tr>
                <th>model</th>
                <th v-for="bucket in BUCKETS" :key="bucket.key" class="text-end">{{ bucket.label }}</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(row, index) in entry.rows"
                :key="index"
                :class="{ 'usage-child': row.child_session }"
              >
                <td>
                  <span
                    v-if="row.child_session"
                    class="usage-child-mark text-medium-emphasis me-1"
                    :title="`fan-out child session ${row.child_session}`"
                  >child</span>{{ rowLabel(row) }}
                  <span v-if="row.reason" class="text-body-small text-medium-emphasis"> ({{ row.reason }})</span>
                </td>
                <td
                  v-for="bucket in BUCKETS"
                  :key="bucket.key"
                  class="text-end"
                  :title="exact(row[bucket.key])"
                >{{ tokens(row[bucket.key]) }}</td>
              </tr>
            </tbody>
          </v-table>
          <div v-if="entry.from" class="text-body-small text-medium-emphasis mt-1">
            from the {{ entry.from }} · thinking is part of output
          </div>

          <!-- lmer's estimate and the harness's own claim, on two separate lines:
               the first is priced from the operator's table, the second is what
               pi says it cost, and adding them would count one spend twice. -->
          <div v-if="costLabel(entry.cost)" class="usage-cost text-body-small mt-1" :title="costTitle(entry.cost)">
            estimated cost {{ costLabel(entry.cost) }}
          </div>
          <div v-if="entry.reported.length" class="usage-harness-reported text-body-small mt-1">
            harness-reported {{ entry.reported.join(', ') }}
          </div>
        </section>
      </template>
    </v-card-text>
  </v-card>
</template>

<style scoped>
.usage-record-errors {
  margin: 4px 0 0 1.25rem;
}
</style>
