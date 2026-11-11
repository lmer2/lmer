<script setup>
// The person's time entries (spec D18), read-only: the page is for debugging, and
// an entry is fixed by editing its YAML file. GET api/time/entries does the join;
// grouping and totals are this page's.
//
// `filters` is what the page was opened with (`{}` from the bar, `{ status: 'held',
// handler }` from a header warning); `status` is GET api/time/status, read only for
// the filters' choices and the handler fields' labels.
//
// A row's `sessions` are the runs the entry covered (issue #648); each one with a
// run key is an `lmer://run/` link, opened in-app as a reference written in chat is.
//
// Days and times are UTC: the `from`/`to` filters are UTC days on the daemon and
// the files are named and stamped in UTC, so a local day would put an entry under
// one date here and another in the filter and the file.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  mdiAlertOutline, mdiChevronDown, mdiChevronUp, mdiGestureTapButton, mdiLinkVariant, mdiMessageTextOutline,
  mdiOpenInNew, mdiPencilOutline, mdiRefresh, mdiSleep, mdiSwapHorizontal,
} from '@mdi/js'
import { fetchTimeEntries } from '../api.js'
import { issueLabel, targetLink } from '../format.js'
import { dispatchRunRef, parseRunRef } from '../runref.js'

const props = defineProps({
  filters: { type: Object, default: () => ({}) },
  status: { type: Object, default: null },
})

const STATUSES = [
  { value: 'running', title: 'running' },
  { value: 'in_outbox', title: 'in outbox' },
  { value: 'kept_locally', title: 'kept locally' },
  { value: 'written', title: 'written' },
  { value: 'synced', title: 'synced' },
  { value: 'held', title: 'held' },
  { value: 'pending', title: 'pending' },
]
const TRIGGERS = {
  manual: { icon: mdiGestureTapButton, label: 'by hand in lmer' },
  activity: { icon: mdiMessageTextOutline, label: 'by activity' },
  idle: { icon: mdiSleep, label: 'after the idle grace period' },
  switch: { icon: mdiSwapHorizontal, label: 'by a switch to another project' },
  hand: { icon: mdiPencilOutline, label: 'in a hand-written entry' },
  edit: { icon: mdiPencilOutline, label: 'by an edit after time had accrued' },
}
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

const loaded = ref(false)
const loading = ref(false)
const problem = ref(null)
const reply = ref(null)
const form = ref(initial(props.filters))
const expanded = ref({})

let disposed = false
// A filter changed twice in a row sends two reads; only the newest may land, or
// the older filter's rows would sit under the newer filter's controls.
let generation = 0

function initial(filters = {}) {
  const status = filters.status ? String(filters.status).replace(/ /g, '_') : null
  return { from: filters.from || '', to: filters.to || '', project: filters.project || null,
    client: filters.client || null, handler: filters.handler || null, status }
}

function query() {
  const value = form.value
  const text = (item) => (typeof item === 'string' ? item.trim() : item) || undefined
  return { from: text(value.from), to: text(value.to), project: text(value.project), client: text(value.client),
    handler: text(value.handler), status: text(value.status) }
}

async function load() {
  const mine = ++generation
  loading.value = true
  try {
    const answer = await fetchTimeEntries(query())
    if (disposed || mine !== generation) return
    reply.value = answer
    problem.value = null
    remember(answer.rows || [])
  } catch (exc) {
    if (disposed || mine !== generation) return
    reply.value = null
    if (exc.status === 503) problem.value = 'Time tracking is off, so there are no entries to show.'
    else problem.value = exc.message
  } finally {
    if (!disposed && mine === generation) {
      loading.value = false
      loaded.value = true
    }
  }
}

// What the filters offer only grows: choosing a project must not take the other
// projects out of the list it was chosen from.
const seen = ref({ projects: {}, handlers: {}, clients: [] })
function remember(rows) {
  const next = { projects: { ...seen.value.projects }, handlers: { ...seen.value.handlers },
    clients: [...seen.value.clients] }
  for (const row of rows) {
    if (row.project && !next.projects[row.project]) next.projects[row.project] = row.project_name || row.project
    if (row.handler && !next.handlers[row.handler]) next.handlers[row.handler] = row.handler_label || row.handler
    if (row.client && !next.clients.includes(row.client)) next.clients.push(row.client)
  }
  seen.value = next
}

function choices(known, current) {
  const items = { ...known }
  if (current && !items[current]) items[current] = current
  return Object.entries(items).map(([value, title]) => ({ value, title }))
}
const projectChoices = computed(() => {
  const known = {}
  for (const row of Object.values(props.status?.projects || {})) {
    if (row.project_key) known[row.project_key] = row.name || row.project_key
  }
  return choices({ ...seen.value.projects, ...known }, form.value.project)
})
const handlerChoices = computed(() => {
  const known = {}
  for (const handler of props.status?.handlers || []) known[handler.name] = handler.label || handler.name
  return choices({ ...seen.value.handlers, ...known }, form.value.handler)
})
const clientChoices = computed(() => {
  const all = new Set(seen.value.clients)
  for (const row of Object.values(props.status?.projects || {})) for (const client of row.clients || []) all.add(client)
  return [...all].sort()
})

function fieldLabel(row, key) {
  const handler = (props.status?.handlers || []).find(item => item.name === row.handler)
  return handler?.fields?.find(item => item.key === key)?.label || key
}

function clock(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return '—'
  const total = Math.max(0, Math.round(Number(seconds)))
  const pad = (value) => String(value).padStart(2, '0')
  return `${Math.floor(total / 3600)}:${pad(Math.floor(total / 60) % 60)}:${pad(total % 60)}`
}
const dayOf = (iso) => (iso ? iso.slice(0, 10) : null)
function dayLabel(day) {
  if (!day) return 'Start unknown'
  const date = new Date(`${day}T00:00:00Z`)
  return Number.isNaN(date.getTime()) ? day : `${WEEKDAYS[date.getUTCDay()]} ${day}`
}
function timeOf(iso, day) {
  if (!iso) return null
  const time = iso.slice(11, 16)
  return dayOf(iso) === day ? time : `${dayOf(iso)} ${time}`
}
const projectKey = (row) => row.project || row.workspace_id || ''
const projectName = (row) => row.project_name || row.project || 'unknown project'
const rowKey = (row, index) => `${row.id || row.stage}:${row.start || index}`

function totalsByProject(rows) {
  const totals = new Map()
  for (const row of rows) {
    const key = projectKey(row)
    const item = totals.get(key) || { key, name: projectName(row), seconds: 0 }
    item.seconds += Number(row.duration) || 0
    totals.set(key, item)
  }
  return [...totals.values()]
}

// Newest day first, as the rows arrive; a row the daemon could not date comes last.
const days = computed(() => {
  const groups = new Map()
  for (const [index, row] of (reply.value?.rows || []).entries()) {
    const day = dayOf(row.start)
    if (!groups.has(day)) groups.set(day, [])
    groups.get(day).push({ row, key: rowKey(row, index) })
  }
  return [...groups.entries()].map(([day, items]) => ({
    day, label: dayLabel(day), items,
    seconds: items.reduce((sum, { row }) => sum + (Number(row.duration) || 0), 0),
    projects: totalsByProject(items.map(({ row }) => row)),
  }))
})
const overall = computed(() => totalsByProject(reply.value?.rows || []))

function stageColor(stage) {
  if (stage === 'running') return 'primary'
  if (stage === 'written') return 'success'
  return 'tone-idle'
}
function syncColor(sync) {
  if (sync.status === 'synced') return 'success'
  if (sync.status === 'held') return 'error'
  return 'warning'
}
function triggerSides(row) {
  if (!row.trigger) return []
  return [['start', 'started'], ['stop', 'stopped']]
    .filter(([side]) => row.trigger[side])
    .map(([side, verb]) => {
      const known = TRIGGERS[row.trigger[side]]
      return { side, name: row.trigger[side], icon: known?.icon || mdiAlertOutline,
        label: `${verb} ${known?.label || row.trigger[side]}` }
    })
}

// `{ key, href, ref }` per linked run; `ref` is null for an item the app cannot
// open (a uid with no run key), which is shown as text.
function linkedRuns(row) {
  return (row.sessions || []).map((item, index) => {
    const href = item.run ? `lmer://run/${item.run}` : null
    return { key: `${index}:${item.run || item.uid}`, text: item.run || item.uid, href,
      ref: href ? parseRunRef(href) : null }
  })
}
// The run a running or kept entry is for (#665), opened like a linked run.
function identityRun(row) {
  const run = row.identity?.run
  if (!run) return null
  const href = `lmer://run/${run}`
  return { text: run, href, ref: parseRunRef(href) }
}
function runCount(row) {
  const count = row.sessions?.length || 0
  return `${count} linked ${count === 1 ? 'run' : 'runs'}`
}

// A middle click too: left to the browser, it would hand lmer:// to the OS.
function openRun(event, ref) {
  if (event.type === 'auxclick' && event.button !== 1) return
  event.preventDefault()
  dispatchRunRef(ref)
}

function toggle(key) {
  expanded.value = { ...expanded.value, [key]: !expanded.value[key] }
}

watch(() => props.filters, (filters) => {
  form.value = initial(filters)
})
watch(form, () => load(), { deep: true })

onMounted(() => load())
onBeforeUnmount(() => {
  disposed = true
})
</script>

<template>
  <section aria-label="Time entries" class="time-entries mb-5">
    <div class="time-entries-filters" data-test="filters">
      <v-text-field v-model="form.from" type="date" label="From (UTC)" density="compact" hide-details
        data-test="filter-from" />
      <v-text-field v-model="form.to" type="date" label="To (UTC)" density="compact" hide-details
        data-test="filter-to" />
      <v-select v-model="form.project" :items="projectChoices" label="Project" density="compact" hide-details
        clearable data-test="filter-project" />
      <v-combobox v-model="form.client" :items="clientChoices" label="Client" density="compact" hide-details
        clearable data-test="filter-client" />
      <v-select v-model="form.handler" :items="handlerChoices" label="Handler" density="compact" hide-details
        clearable data-test="filter-handler" />
      <v-select v-model="form.status" :items="STATUSES" label="Status" density="compact" hide-details
        clearable data-test="filter-status" />
      <v-btn class="time-entries-reload" :icon="mdiRefresh" :loading="loading" variant="text" size="small"
        aria-label="read the entries again" title="read the entries again" @click="load()" />
    </div>
    <p class="text-body-small text-medium-emphasis mt-2 mb-3">
      Read-only. Times are UTC, as the entry files store them; to fix an entry, edit its file.
    </p>

    <p v-if="!loaded" class="text-medium-emphasis">reading…</p>

    <v-alert v-if="problem" type="error" density="compact" class="time-entries-problem my-3">{{ problem }}</v-alert>

    <template v-if="reply">
      <v-alert v-for="(item, index) in reply.problems || []" :key="`p${index}`" type="warning" density="compact"
        class="time-entries-source-problem mb-2">{{ item }}</v-alert>

      <p v-if="!reply.rows?.length" class="time-entries-empty text-medium-emphasis">
        No entries match these filters.
      </p>

      <section v-else aria-label="Totals by project" class="time-entries-overall mb-4">
        <h2 class="text-title-small">Total · {{ reply.count }} {{ reply.count === 1 ? 'entry' : 'entries' }}</h2>
        <ul class="time-totals">
          <li v-for="item in overall" :key="item.key" class="time-total" :data-project="item.key">
            <span class="time-total-name">{{ item.name }}</span>
            <span class="time-total-seconds">{{ clock(item.seconds) }}</span>
          </li>
        </ul>
      </section>

      <section v-for="group in days" :key="group.day || 'unknown'" class="time-day mb-4" :data-day="group.day || ''"
        :aria-label="group.label">
        <div class="time-day-head">
          <h2 class="text-title-medium">{{ group.label }}</h2>
          <span class="time-day-total text-title-medium">{{ clock(group.seconds) }}</span>
        </div>
        <ul class="time-totals time-day-projects text-body-small">
          <li v-for="item in group.projects" :key="item.key" class="time-total" :data-project="item.key">
            <span class="time-total-name">{{ item.name }}</span>
            <span class="time-total-seconds">{{ clock(item.seconds) }}</span>
          </li>
        </ul>

        <v-sheet v-for="{ row, key } in group.items" :key="key" class="time-entry" :data-entry="row.id || row.stage"
          border rounded>
          <div class="time-entry-main">
            <span class="time-entry-span">
              <span class="time-entry-start" :title="row.start || ''">{{ timeOf(row.start, group.day) || '—' }}</span>
              –
              <span class="time-entry-end" :title="row.end || ''">{{ row.end ? timeOf(row.end, group.day) : row.stage === 'running' ? 'now' : '—' }}</span>
              <span class="time-entry-duration">{{ clock(row.duration) }}</span>
            </span>
            <span class="time-entry-project">{{ projectName(row) }}</span>
            <span v-if="row.client" class="time-entry-client">{{ row.client }}</span>
            <v-chip v-else class="time-entry-no-client" size="small" color="warning" variant="tonal"
              :prepend-icon="mdiAlertOutline">no client</v-chip>
            <span class="time-entry-title" :class="{ 'text-medium-emphasis': !row.title }">{{ row.title || 'untitled' }}</span>
          </div>

          <div class="time-entry-meta">
            <span class="time-entry-triggers">
              <span v-for="side in triggerSides(row)" :key="side.side" class="time-trigger"
                :data-trigger="`${side.side}:${side.name}`" role="img" :aria-label="side.label" :title="side.label">
                <v-icon :icon="side.icon" size="18" />
              </span>
            </span>
            <v-chip class="time-entry-stage" size="small" variant="tonal" :color="stageColor(row.stage)">{{ row.stage }}</v-chip>
            <template v-if="row.sync">
              <v-chip v-if="row.sync.status === 'synced' && targetLink(row.sync.link)" class="time-entry-sync"
                :data-sync="row.sync.status" size="small" variant="tonal" :color="syncColor(row.sync)"
                :href="targetLink(row.sync.link)" target="_blank" rel="noopener" :append-icon="mdiOpenInNew">synced</v-chip>
              <v-chip v-else class="time-entry-sync" :data-sync="row.sync.status" size="small" variant="tonal"
                :color="syncColor(row.sync)">{{ row.sync.status }}</v-chip>
              <span v-if="row.sync.status === 'held' && row.sync.reason" class="time-entry-held-reason text-body-small">
                {{ row.sync.reason }}</span>
              <span v-else-if="row.sync.status === 'pending' && row.sync.detail"
                class="time-entry-sync-detail text-body-small text-medium-emphasis">{{ row.sync.detail }}</span>
            </template>
            <span v-if="row.sessions?.length" class="time-entry-run-count text-body-small" role="img"
              :aria-label="runCount(row)" :title="runCount(row)">
              <v-icon :icon="mdiLinkVariant" size="18" />{{ row.sessions.length }}</span>
            <span v-if="row.handler" class="time-entry-handler text-body-small text-medium-emphasis">
              {{ row.handler_label || row.handler }}</span>
            <span v-for="(value, field) in row.fields || {}" :key="field" class="time-entry-field text-body-small"
              :data-field="field">
              {{ fieldLabel(row, field) }}:
              <a v-if="targetLink(row.field_links?.[field])" :href="targetLink(row.field_links[field])" target="_blank" rel="noopener">{{ value }}</a>
              <template v-else>{{ value }}</template>
            </span>
            <v-chip v-for="field in row.unassigned || []" :key="`u-${field}`" class="time-entry-unassigned"
              :data-field="field" size="small" color="warning" variant="tonal" :prepend-icon="mdiAlertOutline">
              missing {{ fieldLabel(row, field) }}</v-chip>
            <a v-for="url in row.issues || []" :key="url" class="time-entry-issue text-body-small" :href="url"
              target="_blank" rel="noopener noreferrer">{{ issueLabel(url) }}</a>
            <v-spacer />
            <v-btn class="time-entry-toggle" size="small" variant="text"
              :append-icon="expanded[key] ? mdiChevronUp : mdiChevronDown" :aria-expanded="!!expanded[key]"
              @click="toggle(key)">{{ expanded[key] ? 'less' : 'details' }}</v-btn>
          </div>

          <p v-if="row.kept" class="time-entry-kept text-body-small text-medium-emphasis mb-0">{{ row.kept }}</p>
          <p v-if="row.outbox?.last_error" class="time-entry-outbox-error text-body-small text-error mb-0">
            {{ row.outbox.last_error }}</p>
          <ul v-if="row.problems?.length" class="time-entry-problems text-body-small text-error">
            <li v-for="(item, index) in row.problems" :key="index">{{ item }}</li>
          </ul>

          <div v-if="expanded[key]" class="time-entry-detail text-body-small">
            <dl>
              <dt>file</dt><dd class="time-entry-path">{{ row.path || 'not written yet' }}</dd>
              <dt>id</dt><dd>{{ row.id || '—' }}</dd>
              <dt>read from</dt><dd>{{ row.origin }}</dd>
              <template v-if="row.source"><dt>source</dt><dd>{{ row.source }}</dd></template>
              <template v-if="row.mode"><dt>mode</dt><dd>{{ row.mode }}</dd></template>
              <template v-if="identityRun(row)">
                <dt>run</dt>
                <dd class="time-entry-identity">
                  <a :href="identityRun(row).href" @click="openRun($event, identityRun(row).ref)"
                    @auxclick="openRun($event, identityRun(row).ref)">{{ identityRun(row).text }}</a>
                </dd>
              </template>
              <template v-if="row.sessions?.length">
                <dt>runs</dt>
                <dd>
                  <ul class="time-entry-runs">
                    <li v-for="run in linkedRuns(row)" :key="run.key" class="time-entry-run">
                      <a v-if="run.ref" :href="run.href" @click="openRun($event, run.ref)"
                        @auxclick="openRun($event, run.ref)">{{ run.text }}</a>
                      <template v-else>{{ run.text }}</template>
                    </li>
                  </ul>
                </dd>
              </template>
              <template v-if="row.sync?.ref"><dt>ledger ref</dt><dd>{{ row.sync.ref }}</dd></template>
              <template v-if="row.sync?.at"><dt>ledger at</dt><dd>{{ row.sync.at }}</dd></template>
              <template v-if="row.sync?.hash"><dt>hash</dt><dd class="time-entry-hash">{{ row.sync.hash }}</dd></template>
            </dl>
            <pre class="time-entry-raw">{{ row.raw }}</pre>
          </div>
        </v-sheet>
      </section>
    </template>
  </section>
</template>

<style scoped>
.time-entries-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.time-entries-filters > .v-input {
  flex: 1 1 140px;
  min-width: 130px;
}
.time-totals {
  list-style: none;
  padding: 0;
  margin: 4px 0 8px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
}
.time-total {
  display: flex;
  gap: 6px;
  min-width: 0;
}
.time-total-name {
  overflow-wrap: anywhere;
}
.time-total-seconds,
.time-day-total,
.time-entry-span {
  font-variant-numeric: tabular-nums;
}
.time-day-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.time-entry {
  padding: 8px 12px;
  margin-bottom: 8px;
  min-width: 0;
}
.time-entry-main,
.time-entry-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 12px;
  min-width: 0;
}
.time-entry-main > *,
.time-entry-meta > * {
  min-width: 0;
  overflow-wrap: anywhere;
}
.time-entry-span {
  white-space: nowrap;
}
.time-entry-duration {
  margin-inline-start: 8px;
  font-weight: 500;
}
.time-entry-project {
  font-weight: 500;
}
.time-entry-triggers {
  display: inline-flex;
  gap: 2px;
}
.time-entry-run-count {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}
.time-entry-runs {
  list-style: none;
  padding: 0;
  margin: 0;
}
.time-entry-problems {
  margin: 4px 0 0;
  padding-inline-start: 20px;
}
.time-entry-detail dl {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 2px 12px;
  margin: 8px 0;
}
.time-entry-detail dt {
  opacity: .7;
}
.time-entry-detail dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.time-entry-raw {
  max-height: 320px;
  overflow: auto;
  padding: 8px;
  border-radius: 4px;
  background: rgb(var(--v-theme-code));
  color: rgb(var(--v-theme-on-code));
  white-space: pre;
}
</style>
