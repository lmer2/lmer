<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useDisplay, useTheme } from 'vuetify'
import {
  mdiRobot,
  mdiAlertOutline,
  mdiArrowLeft,
  mdiBroom,
  mdiMagnify,
  mdiPlus,
  mdiRefresh,
  mdiThemeLightDark,
  mdiWeatherNight,
  mdiWeatherSunny,
} from '@mdi/js'
import AddRun from './components/AddRun.vue'
import AssistantChat from './components/AssistantChat.vue'
import RunCard from './components/RunCard.vue'
import RunDetail from './components/RunDetail.vue'
import RunNav from './components/RunNav.vue'
import SlotRow from './components/SlotRow.vue'
import { fetchState, forgetRun, prune, rescan } from './api.js'
import {
  ago, attentionLabel, driverLabel, slotIsFree, stateMeta, targetRef,
} from './format.js'
import { rememberChoice, storedChoice } from './preferences.js'

const POLL_MS = 10000

// --- theme mode (extracted by tests/test_platform_web_theme.py) --------------
// Three states rather than a switch, and that is the whole design. Both schemes
// are first-class with the OS picking (spec §10.1), so "follow the system" is not
// one end of a toggle — it is the default, and a two-state control deletes it: the
// first flip leaves no way back, and an operator who reads the fleet on a dark
// desk and a bright phone loses the behaviour they had. Hence the OS is a mode of
// its own, and the first entry, which is what makes it storedChoice's default.
const THEME_MODES = ['system', 'light', 'dark']
// Spelled out again by the inline script in web/index.html, which runs before this
// bundle exists and therefore cannot import it — tests/test_platform_web_theme.py
// pins the key and these three words across the two files.
const THEME_STORAGE_KEY = 'lmer.theme.mode'

function storedThemeMode() {
  return storedChoice(
    () => window.localStorage.getItem(THEME_STORAGE_KEY), THEME_MODES,
  )
}

function rememberThemeMode(value) {
  rememberChoice(
    (stored) => window.localStorage.setItem(THEME_STORAGE_KEY, stored),
    THEME_MODES,
    value,
  )
}
// --- end of theme mode -------------------------------------------------------

const THEME_ICONS = {
  system: mdiThemeLightDark,
  light: mdiWeatherSunny,
  dark: mdiWeatherNight,
}

const THEME_LABELS = {
  system: 'follow the system',
  light: 'light',
  dark: 'dark',
}

const state = ref(null)
const error = ref(null)
const loading = ref(true)
const busy = ref(false)
const now = ref(Date.now())
const view = ref('fleet')
const selectedKey = ref(null)

// Vuetify's own breakpoint rather than a media query of ours: the drawer below
// reads the same `mobile` the framework uses to decide overlay-versus-inline, so
// the two cannot end up disagreeing about which device this is.
const { mobile } = useDisplay()

// The override on top of main.js's `defaultTheme: 'system'`, which stays the
// default for anyone who has never picked.
//
// `theme.change()` is the lever, confirmed against the installed Vuetify (4.1.6,
// node_modules/vuetify/lib/composables/theme.js): it is the only entry point that
// accepts 'system' as a name, and assigning `theme.global.name.value` is
// deprecated there — it warns and, worse, `theme.name` resolves 'system' down to
// light or dark, so what was chosen cannot be read back out of it. Which is why
// this ref is the state and Vuetify is told about it rather than asked.
// Changing the name is also what repaints the emulator, which no stylesheet
// reaches: the session view watches `theme.current.value.dark`, and `current` is
// derived from that same name — so forcing a scheme drives that watcher exactly as
// the OS flipping does. (That component is deliberately not named here: the shell
// mentioning it is indistinguishable — to a reader and to
// tests/test_platform_web_shell.py — from the static import that would pull xterm
// into the first load.)
const theme = useTheme()
const themeMode = ref(storedThemeMode())
theme.change(themeMode.value)

watch(themeMode, (mode) => {
  theme.change(mode)
  rememberThemeMode(mode)
})

// Beside the content on a desktop, behind a scrim on a phone. Only the first
// frame is decided here — after that Vuetify's resize watchers keep it in step
// (permanent implies open, mobile implies closed), which it does not do for the
// initial value of a model somebody else owns.
const navOpen = ref(!mobile.value)

// --- the supervisor's drawer (T31) -------------------------------------------
// Closed on every load, and deliberately not remembered. The landing screen is
// the fleet, and a remembered-open drawer would open a phone onto a full-screen
// chat instead of it — and make every glance at the fleet fetch the supervisor's
// status and its transcript. So no preference and no storage key: this is a
// drawer you open to say something.
const uberOpen = ref(false)
// Mounted on first open and left mounted after. The v-if is what keeps the fleet
// view free of the chat's two polls until somebody asks for it; leaving it mounted
// afterwards is what keeps a half-typed message alive across a close. The bar
// button shows no digest count for the same reason the drawer is not preloaded —
// a badge would mean polling the supervisor on every fleet load.
const uberSeen = ref(false)
watch(uberOpen, (open) => {
  if (open) uberSeen.value = true
})

let poll = null
let tick = null

function keyOf(run) {
  return `${run.host}/${run.project}/${run.slug}`
}

const attention = computed(() => state.value?.attention || [])
const runs = computed(() => state.value?.runs || [])
const calm = computed(() => runs.value.filter((run) => !run.attention))
const totals = computed(() => state.value?.totals || { runs: 0, live: 0, attention: 0 })
const mirror = computed(() => state.value?.mirror || null)
// Service slots (#245). Empty on a host that declares none, and the section
// below then renders nothing rather than an explanatory card — a fixture nobody
// configured is not news.
const slots = computed(() => state.value?.slots || [])
// Filtered here rather than in the dialog, so the row's wording and the
// dialog's list come from one predicate.
const freeSlots = computed(() => slots.value.filter(slotIsFree))

// Staleness must be visible rather than silently wrong (spec R20): a fleet view
// confidently showing week-old phases is worse than one admitting it can't tell.
const mirrorProblem = computed(() => {
  const m = mirror.value
  if (!m) return null
  if (!m.present) return m.last_error || 'work-repo mirror has not been cloned yet'
  if (!m.healthy) return m.last_error || 'work-repo mirror is stale'
  return null
})

// --- how much of the host is spent (T97) -------------------------------------

// The cap the daemon actually enforces, read off the state payload's config
// block. Null when the payload carries none — an older daemon serves a fleet
// without it — and the reading is then absent rather than drawn against a
// denominator this app invented.
const workerCap = computed(() => state.value?.config?.max_concurrent_sessions || null)

// The numerator has to be the count the daemon enforces rather than "live rows",
// and the difference is one row: the cap bounds *workers*, and uber lmer holds its
// own slot beside them (spawn._live_worker_count excludes its kind, and the
// refusal a full host answers with names live/cap from that same count). Counting
// it here would read 8/8 on a host with room for another run — a display that
// disagrees with the refusal is worse than none.
//
// Derivable in the client because every live session is a row: each carries its own
// liveness and the orchestrator flag, and a live session whose run dir has not
// reached the mirror yet still arrives as a row of its own (lmer_platform.inventory).
const liveWorkers = computed(
  () => runs.value.filter((run) => run.live && !run.orchestrator).length,
)

// --- forgetting a finished run (T101) ----------------------------------------
//
// One tap and no dialog, because the operator asked to do this *quickly* ("allow
// me to quickly forget completed runs from the list") and a confirm on every row
// is the thing that makes clearing ten of them not worth starting. What stands in
// for the confirm is an undo window: the row goes at once, nothing is sent, and
// the request is made only once the window lapses — so undo is not a rollback of
// anything, it is the call never happening.
//
// Timers are per run rather than one shared queue: forgetting three rows in as
// many seconds is the case this exists for, and a single pending timer would let
// the third forget cancel or swallow the first two.
const FORGET_UNDO_MS = 7000
//: Rows already out of the list: the ones still inside their undo window and the
//: ones whose forget has been sent and not yet dropped from a poll's payload.
//: Hiding rather than removing is what makes an undo — and a refused forget — put
//: the row back with nothing to rebuild it from.
const hiddenKeys = ref([])
//: Of those, the ones still undoable, newest first. Drives the snackbar.
const pendingForgets = ref([])
const forgetTimers = new Map()

function forget(run) {
  const key = keyOf(run)
  if (forgetTimers.has(key)) return
  hiddenKeys.value = [...hiddenKeys.value, key]
  pendingForgets.value = [
    { key, run, label: run.title || run.label }, ...pendingForgets.value,
  ]
  forgetTimers.set(key, setTimeout(() => commitForget(key), FORGET_UNDO_MS))
}

async function commitForget(key) {
  const pending = pendingForgets.value.find((item) => item.key === key)
  forgetTimers.delete(key)
  pendingForgets.value = pendingForgets.value.filter((item) => item.key !== key)
  if (!pending) return
  try {
    await forgetRun(pending.run)
  } catch (exc) {
    // The row comes back on its own: it was only hidden, so un-hiding it is the
    // whole of the recovery, and the alert above the list says what happened.
    hiddenKeys.value = hiddenKeys.value.filter((hidden) => hidden !== key)
    error.value = exc.message
  }
}

// Undo takes back the whole pending set rather than the last row, because that is
// exactly what is still undoable — anything older has already been sent — and the
// button says which it is doing.
function undoForget() {
  const restored = new Set(pendingForgets.value.map((item) => item.key))
  for (const key of restored) {
    clearTimeout(forgetTimers.get(key))
    forgetTimers.delete(key)
  }
  hiddenKeys.value = hiddenKeys.value.filter((key) => !restored.has(key))
  pendingForgets.value = []
}

// What forgetting does and does not do, said where the operator is deciding
// whether to undo it: tracking is all that ends, so nothing here is destroyed and
// adopting the run brings it back.
const forgetNotice = computed(() => {
  const [first, ...rest] = pendingForgets.value
  if (!first) return ''
  const more = rest.length ? ` and ${rest.length} more` : ''
  return `no longer tracking ${first.label}${more} — the run's files and work-repo `
    + 'state are untouched, and adopting it brings it back'
})

const undoLabel = computed(
  () => (pendingForgets.value.length > 1 ? 'undo all' : 'undo'),
)

// --- searching the list (T100) -----------------------------------------------
//
// In the client, and that follows from the scope rule rather than being a shortcut:
// the fleet is the runs THIS orchestrator tracks (spec D25), so the list is small
// enough that a pass over it costs nothing — and a filter that needs no request
// keeps working while the daemon is unreachable, on the payload already on screen.
const query = ref('')

// Every word, in any order: "mr develop" is how a list of thirty rows gets narrowed,
// and a single-substring match finds nothing for it.
const queryWords = computed(
  () => (query.value || '').toLowerCase().split(/\s+/).filter(Boolean),
)

// What the row *shows*, and only that. A search matching a field the card does not
// render answers with rows whose reason for matching is invisible; one that misses a
// rendered field is a search an operator stops trusting. So this reads the same
// values RunCard.vue draws and through the same helpers — the state word and the
// attention line are labels, and both the label and the enum behind it are matched
// because either is what somebody types.
function haystack(run) {
  return [
    run.title, run.label, run.slug, run.taskdef, run.project, run.host,
    run.target, targetRef(run.target)?.label,
    run.state, stateMeta(run.state).label,
    run.status, run.phase,
    run.attention?.reason,
    run.attention && attentionLabel(run.attention.reason),
    run.attention?.note,
    run.preset, run.agents, driverLabel(run),
    run.orchestrator && 'uber lmer',
    // The word the row shows, not the age beside it: "unchecked" is a state an
    // operator asks the list for ("what has nobody looked at"), while the time is
    // rendered relative and a query never means one — the same reason `updated`
    // is not in here.
    run.checkin?.stale && 'unchecked',
    ...(run.ports || []).map((port) => `:${port.host}`),
  ].filter(Boolean).join(' ').toLowerCase()
}

// Both listings, filtered the same way, so the attention-first grouping survives a
// search: which runs need a human is what this view is for, and collapsing results
// into one list would hand that back for a filter.
function visible(list) {
  const words = queryWords.value
  return list.filter((run) => {
    if (hiddenKeys.value.includes(keyOf(run))) return false
    const text = words.length ? haystack(run) : ''
    return words.every((word) => text.includes(word))
  })
}

const attentionRows = computed(() => visible(attention.value))
const calmRows = computed(() => visible(calm.value))
// Said out loud, because a tracked fleet with an empty list on screen otherwise
// reads as the fleet having been lost rather than as the search having matched
// nothing.
const nothingMatches = computed(() => (
  !!queryWords.value.length && !attentionRows.value.length && !calmRows.value.length
))

const selected = computed(() =>
  runs.value.find((run) => keyOf(run) === selectedKey.value) || null,
)

// One app bar for every view, so the back gesture and the notch inset are handled
// in one place; the title is what tells you which view you are looking at.
const title = computed(() => {
  if (view.value === 'add') return 'spawn or adopt'
  if (view.value === 'detail') return selected.value?.label || 'run'
  return 'lmer platform'
})

// One fleet read at a time. Four things call this now — the interval, a view that
// just changed something, the prune button and coming back to the tab — and the
// last of those arrives twice by design (see refetchOnReturn). Without the guard
// that is two builds of the whole fleet racing on the daemon and the loser's
// payload deciding what stays on screen.
let inFlight = null

function load() {
  if (inFlight) return inFlight
  inFlight = (async () => {
    try {
      state.value = await fetchState()
      error.value = null
      // Rows the daemon no longer reports are forgotten for real, so the key has
      // nothing left to hide; keeping them would grow this list for the life of
      // the tab. A row still inside its undo window is still in the payload.
      hiddenKeys.value = hiddenKeys.value.filter(
        (key) => runs.value.some((run) => keyOf(run) === key),
      )
    } catch (exc) {
      error.value = exc.status === 401
        ? 'Not authenticated — reload and enter the shared secret.'
        : exc.message
    } finally {
      loading.value = false
      inFlight = null
    }
  })()
  return inFlight
}

// Coming back to the app, which the interval alone does not cover: a phone throttles
// a background timer hard or stops it outright, so the first thing an operator sees
// on returning can be minutes old — which is how a session that ended looks like one
// that is still running for a minute or two, with the poll working exactly as
// written. Both events, because neither implies the other: switching apps on a phone
// fires visibilitychange with no focus event, and a desktop window raised over
// another fires focus without ever having been hidden. The double fire the pair
// causes on one gesture is what the load guard is for.
function refetchOnReturn() {
  if (document.visibilityState === 'hidden') return
  load()
}

async function doRescan() {
  busy.value = true
  try {
    state.value = await rescan()
    error.value = null
  } catch (exc) {
    error.value = exc.message
  } finally {
    busy.value = false
  }
}

async function doPrune() {
  busy.value = true
  try {
    await prune()
    await load()
  } catch (exc) {
    error.value = exc.message
  } finally {
    busy.value = false
  }
}

function open(run) {
  selectedKey.value = keyOf(run)
  view.value = 'detail'
  // On a phone the drawer is an overlay sitting on top of the run it was just
  // used to open.
  if (mobile.value) navOpen.value = false
}

onMounted(() => {
  load()
  poll = setInterval(load, POLL_MS)
  // Separate, faster tick so "3m ago" ages without refetching the fleet.
  tick = setInterval(() => { now.value = Date.now() }, 1000)
  document.addEventListener('visibilitychange', refetchOnReturn)
  window.addEventListener('focus', refetchOnReturn)
})

onUnmounted(() => {
  clearInterval(poll)
  clearInterval(tick)
  document.removeEventListener('visibilitychange', refetchOnReturn)
  window.removeEventListener('focus', refetchOnReturn)
  // A forget still inside its undo window is never sent, which is the direction
  // worth failing in: the run stays tracked and the row is back on the next load.
  for (const timer of forgetTimers.values()) clearTimeout(timer)
  forgetTimers.clear()
})
</script>

<template>
  <v-app>
    <v-app-bar :elevation="1" density="comfortable">
      <!-- Only where the drawer is an overlay: where it is permanent there is
           nothing to toggle, and a button that does nothing is worse than no
           button. -->
      <v-app-bar-nav-icon
        v-if="mobile"
        aria-label="show the run list"
        @click="navOpen = !navOpen"
      />
      <v-btn
        v-if="view !== 'fleet'"
        :icon="mdiArrowLeft"
        variant="text"
        aria-label="back to the fleet"
        @click="view = 'fleet'"
      />
      <v-app-bar-title class="text-title-medium">{{ title }}</v-app-bar-title>
      <!-- The bar's control cluster, centred on a desktop (operator request: it
           read as "tucked into the upper right"). Absolute against the bar rather
           than a spacer sandwich, so it sits at the bar's true centre instead of
           the centre of whatever the title leaves over. On a phone the cluster
           stays in flow on the right: a 390px bar already carries the nav toggle
           and the title on the left, and a centred cluster lands on top of them. -->
      <div class="bar-cluster" :class="{ 'bar-cluster-centered': !mobile }">
      <!-- In the bar on every view, and always in the same place: the supervisor
           is the app's, not one run's, and "tell it to stop that" has to be one
           tap from wherever you noticed. Icon-only because the bar already carries
           four controls on a 390px screen — the words are in the drawer's own
           header, which is what an operator reads before typing. -->
      <v-btn
        :icon="mdiRobot"
        variant="text"
        aria-label="talk to uber lmer"
        @click="uberOpen = !uberOpen"
      />
      <!-- In the bar because the scheme is the app's and not one run's: it stays
           reachable from the fleet, a run and the spawn form alike.
           A menu rather than three buttons in the bar: on a 390px screen the bar
           already carries the drawer icon, the title and the fleet's two verbs,
           and a permanent third of that width for a preference is the view paying
           for it. The menu also lets each state say what it is in words — three
           icons alone leave "which one is following the OS" to be guessed. -->
      <v-menu location="bottom end">
        <template #activator="{ props: picker }">
          <v-btn
            v-bind="picker"
            :icon="THEME_ICONS[themeMode]"
            variant="text"
            :aria-label="`colour scheme: ${THEME_LABELS[themeMode]}`"
          />
        </template>
        <v-list :selected="[themeMode]" density="compact">
          <v-list-item
            v-for="mode in THEME_MODES"
            :key="mode"
            :value="mode"
            :prepend-icon="THEME_ICONS[mode]"
            :title="THEME_LABELS[mode]"
            @click="themeMode = mode"
          />
        </v-list>
      </v-menu>
      <template v-if="view === 'fleet'">
        <v-btn
          :icon="mdiRefresh"
          :loading="busy"
          variant="text"
          aria-label="rescan now"
          @click="doRescan"
        />
        <v-btn
          class="me-2"
          variant="tonal"
          :prepend-icon="mdiPlus"
          @click="view = 'add'"
        >run</v-btn>
      </template>
      </div>
    </v-app-bar>

    <!-- The run list, reachable from wherever you are. Without it the detail
         view is a dead end: switching runs means going back to the fleet first,
         which is three taps to answer the second of two waiting questions.
         Left, and only left — the right side belongs to the drawer below it.
         Permanent beside the content on a desktop and an overlay on a phone,
         which is Vuetify's own `mobile` breakpoint doing the deciding: a
         permanent drawer on a 390px screen eats the view it exists to reach. -->
    <v-navigation-drawer
      v-model="navOpen"
      :permanent="!mobile"
      location="left"
      width="280"
    >
      <RunNav
        :runs="runs"
        :attention="attention"
        :current-key="view === 'detail' ? selectedKey : null"
        :now="now"
        @open="open"
      />
    </v-navigation-drawer>

    <!-- The supervisor's chat, on the right, and the side is the feature: the
         operator has two conversations — with an lmer working in a repository and
         with the uber lmer watching all of them — and telling the second to stop
         something while typing at the first is the mistake worth designing
         against. So they never share an edge, and this one carries the accent
         header its component draws.
         No `permanent` and no `temporary`: Vuetify reads the same `mobile`
         breakpoint the left drawer is given, so it sits beside the content on a
         desktop and comes over it with a scrim on a phone, and the two drawers
         cannot end up disagreeing about which device this is. -->
    <!-- Twice the navigator's width: this drawer holds a conversation, not a
         list, and prose in a 360px column wraps too hard to read. -->
    <v-navigation-drawer v-model="uberOpen" location="right" width="720">
      <AssistantChat v-if="uberSeen" :now="now" @close="uberOpen = false" />
    </v-navigation-drawer>

    <v-main>
      <v-container max-width="900" class="pa-3">
        <!-- Keyed by the run, so switching from the drawer starts the detail
             view over instead of reusing one that still holds the last run's
             error. The session views restart on their own (they watch the
             session id); this is about the state that has no watcher. -->
        <RunDetail
          v-if="view === 'detail' && selected"
          :key="selectedKey"
          :run="selected"
          :now="now"
          @close="view = 'fleet'"
          @changed="load"
          @open="open"
        />
        <AddRun
          v-else-if="view === 'add'"
          :free-slots="freeSlots"
          :slots="slots"
          @changed="load"
        />

        <template v-else>
          <v-alert v-if="error" type="error" class="mb-3">{{ error }}</v-alert>
          <v-alert
            v-if="mirrorProblem"
            type="warning"
            :icon="mdiAlertOutline"
            class="mb-3"
          >{{ mirrorProblem }}</v-alert>

          <!-- Over the list and nowhere near the app bar: it filters what is below
               it, and it is absent on a fleet with nothing in it — a search box
               above the "nothing tracked yet" card is furniture for a screen whose
               whole job is to explain that there is nothing here yet.
               Compact, because this sits in the header area with the counts and the
               dimmed "as of" line rather than in a form. -->
          <v-text-field
            v-if="runs.length"
            v-model="query"
            label="search the list"
            placeholder="name, taskdef, project, state, ticket…"
            :prepend-inner-icon="mdiMagnify"
            clearable
            hide-details
            density="compact"
            class="mb-3"
          />

          <div class="d-flex flex-wrap ga-3 text-body-small text-medium-emphasis mb-3">
            <span>{{ totals.runs }} tracked</span>
            <span>{{ totals.live }} live</span>
            <!-- The cap, where the spending of it can be seen (the operator asked
                 for it to be displayed somewhere). Beside "live" and deliberately
                 not the same number: that one counts every live row, this one
                 counts what the cap counts — uber lmer runs beside the cap rather
                 than inside it, so its row is not in this numerator. Absent
                 entirely on a daemon whose payload carries no cap. -->
            <span
              v-if="workerCap"
              title="live worker sessions against this host's cap; uber lmer holds its own slot beside them"
            >{{ liveWorkers }}/{{ workerCap }} workers</span>
            <span>{{ totals.attention }} need you</span>
            <span v-if="state?.generated_at" :title="state.generated_at">
              as of {{ ago(state.generated_at, now) }}
            </span>
          </div>

          <div v-if="loading" class="text-center text-medium-emphasis py-8">
            <v-progress-circular indeterminate size="18" width="2" class="me-2" />
            loading…
          </div>

          <!-- The empty state explains the scope rule, so an empty fleet never
               reads as a broken one (spec D25). -->
          <v-card v-else-if="!runs.length" class="text-center pa-6">
            <p class="text-body-large mb-2">Nothing tracked yet.</p>
            <p class="text-body-small text-medium-emphasis mb-4">
              {{ state?.hint || 'This view shows only runs this orchestrator spawned or adopted — never the whole shared work repo.' }}
            </p>
            <v-btn color="primary" :prepend-icon="mdiPlus" @click="view = 'add'">
              spawn or adopt a run
            </v-btn>
          </v-card>

          <template v-else>
            <!-- Runs needing a human come first, always: it is the one thing this
                 view exists to tell you. The split is inside the search results
                 rather than around them, so filtering never buries a run that is
                 waiting on somebody among the rest. -->
            <template v-if="attentionRows.length">
              <div class="section-title">needs you</div>
              <RunCard
                v-for="run in attentionRows"
                :key="keyOf(run)"
                :run="run"
                :now="now"
                @open="open"
                @forget="forget"
                @open-chat="uberOpen = true"
              />
            </template>

            <div v-if="calmRows.length" class="section-title">
              {{ attentionRows.length ? 'everything else' : 'runs' }}
            </div>
            <RunCard
              v-for="run in calmRows"
              :key="keyOf(run)"
              :run="run"
              :now="now"
              @open="open"
              @forget="forget"
              @open-chat="uberOpen = true"
            />

            <p
              v-if="nothingMatches"
              class="text-center text-medium-emphasis py-6"
            >Nothing in the {{ totals.runs }} tracked runs matches that search.</p>

            <v-card v-if="runs.some((run) => run.state === 'crashed')" class="mb-3">
              <v-card-text>
                <p class="text-medium-emphasis mb-3">
                  Crashed runs stay listed until acknowledged — that leftover entry
                  is what records the crash.
                </p>
                <v-btn
                  :disabled="busy"
                  :prepend-icon="mdiBroom"
                  variant="tonal"
                  @click="doPrune"
                >clear crashed sessions</v-btn>
              </v-card-text>
            </v-card>
          </template>

          <!-- Under the runs and never among them (#245): interleaving would put
               rows that can never ask for anything into the list whose whole job
               is to say who is asking. Outside the runs/empty-state branch
               above, because a fleet tracking nothing is the state you are most
               likely to be about to spawn into a slot from. -->
          <template v-if="!loading && slots.length">
            <div class="section-title">service slots</div>
            <SlotRow
              v-for="slot in slots"
              :key="slot.name"
              :row="slot"
              :runs="runs"
              @open="open"
            />
          </template>
        </template>
      </v-container>
    </v-main>

    <!-- The undo window, made visible. It is not a report of something that
         happened — nothing has been sent yet — which is why it stays up for the
         whole window (`timeout="-1"`, the window's own timers close it) and why the
         wording is what forgetting costs rather than a confirmation.
         One bar for however many rows are pending: each row has its own timer, so
         a second forget never cancels the first, and the bar names the newest plus
         how many are with it. -->
    <v-snackbar
      :model-value="!!pendingForgets.length"
      :timeout="-1"
      multi-line
    >
      {{ forgetNotice }}
      <template #actions>
        <v-btn variant="text" @click="undoForget">{{ undoLabel }}</v-btn>
      </template>
    </v-snackbar>
  </v-app>
</template>

<style scoped>
/* The bar's controls, at the bar's centre on a desktop. Absolute because the
   v-app-bar is itself positioned, so 50% here is the viewport's middle — a
   flex-spacer approach would centre on what the title leaves over, which moves
   with the title's length. In flow (right-aligned) on a phone; the template
   comment above the cluster says why. */
.bar-cluster {
  display: flex;
  align-items: center;
}

.bar-cluster-centered {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
}
</style>
