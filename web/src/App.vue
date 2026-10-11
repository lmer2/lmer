<script setup>
import { computed, mergeProps, onMounted, onUnmounted, ref, watch } from 'vue'
import { useDisplay, useTheme } from 'vuetify'
import {
  mdiRobot,
  mdiAlertOutline,
  mdiArrowLeft,
  mdiArrowCollapseHorizontal,
  mdiArrowExpandHorizontal,
  mdiBroom,
  mdiCogOutline,
  mdiMagnify,
  mdiMapOutline,
  mdiPlus,
  mdiViewListOutline,
  mdiRefresh,
  mdiThemeLightDark,
  mdiWeatherNight,
  mdiWeatherSunny,
} from '@mdi/js'
import AddRun from './components/AddRun.vue'
import ForgetCompleted from './components/ForgetCompleted.vue'
import { fleetTitle } from './fleetTitle.js'
import AssistantChat from './components/AssistantChat.vue'
import FleetMap from './components/FleetMap.vue'
import { orchestratorOf, pairsFromReplies } from './fleetMap.js'
import RunCard from './components/RunCard.vue'
import RunDetail from './components/RunDetail.vue'
import RunNav from './components/RunNav.vue'
import ProjectNav from './components/ProjectNav.vue'
import PlatformSettings from './components/PlatformSettings.vue'
import ProjectSlots from './components/ProjectSlots.vue'
import ProjectSettings from './components/ProjectSettings.vue'
import ProjectSwitcher from './components/ProjectSwitcher.vue'
import { createProject, fetchProjects, fetchRunRelations, fetchState, forgetRun, prune, rescan } from './api.js'
import { projectColor, projectTextColor } from './projects.js'
import {
  ago, attentionLabel, driverLabel, slotIsFree, stateMeta, targetRef, toneColor,
} from './format.js'
import {
  rememberChoice, rememberFlag, storedChoice, storedFlag,
} from './preferences.js'
import { onRunRef, runKey } from './runref.js'

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

// --- fleet view mode ---------------------------------------------------------
// Map or list, remembered, because it is a property of how this operator reads
// the fleet rather than of any run — the same reasoning as the theme above.
//
// `list` first, which `storedChoice` therefore treats as the default: it is the
// view that exists today, so an operator who never touches the switch sees no
// change at all.
const FLEET_VIEW_MODES = ['list', 'map']
const FLEET_VIEW_STORAGE_KEY = 'lmer.fleet.view'

function storedFleetView() {
  return storedChoice(
    () => window.localStorage.getItem(FLEET_VIEW_STORAGE_KEY), FLEET_VIEW_MODES,
  )
}

function rememberFleetView(value) {
  rememberChoice(
    (stored) => window.localStorage.setItem(FLEET_VIEW_STORAGE_KEY, stored),
    FLEET_VIEW_MODES,
    value,
  )
}
// --- end of fleet view mode --------------------------------------------------

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

// A run reference naming a run this host does not track (issue #241) — the
// wording RelatedRuns.vue uses for the same case.
const NOT_TRACKED_HINT = 'this orchestrator does not track that run, so there is '
  + 'no page to open — adopt it from "spawn or adopt" first'

const state = ref(null)
const error = ref(null)
const loading = ref(true)
const busy = ref(false)
const now = ref(Date.now())
const view = ref('fleet')
const selectedKey = ref(null)
const projects = ref([])
const activeProject = ref(new URLSearchParams(window.location.search).get('workspace') || '')
const workspace = computed(() => projects.value.find(project => project.id === activeProject.value))
const projectGroups = computed(() => projects.value.filter(project => !project.archived))
const projectDialog = ref(false)
const projectName = ref('')
const projectError = ref(null)
const creatingProject = ref(false)
const platformSection = ref(null)
const projectSettingsFocus = ref(null)
const seenChats = ref([])
const chatProject = ref(activeProject.value)
const chatWorkspace = computed(() => projects.value.find(project => project.id === chatProject.value))
// The bar wears the selected project, so which project the page belongs to is on
// screen at every scroll position. No project (All projects, or a link to one that
// is gone) leaves the theme's own bar.
const barProject = computed(() => (workspace.value && !workspace.value.archived ? workspace.value : null))
const barColor = computed(() => (barProject.value ? projectColor(barProject.value.color) : undefined))
const barStyle = computed(() => (barProject.value ? { color: projectTextColor(barProject.value.color) } : undefined))
const switcherEvents = {
  choose: chooseProject,
  settings: () => { projectSettingsFocus.value = null; view.value = 'project-settings' },
  create: () => { projectDialog.value = true },
}
const switcherProps = computed(() => ({
  projects: projectGroups.value,
  activeProject: activeProject.value,
  label: activeProject.value ? workspace.value?.name || 'Unavailable project' : 'All projects',
  current: barProject.value,
  attention: projectAttention,
}))
const openedChats = computed(() => projects.value.filter(project => seenChats.value.includes(project.id) && !project.archived))

function chooseProject(id) { activeProject.value = id; view.value = 'fleet' }
function showProjectChat(project) {
  activeProject.value = project.id
  chatProject.value = project.id
  uberOpen.value = true
  rememberChat(project.id)
  if (mobile.value) navOpen.value = false
}
function showPlatformSettings(section = null) {
  platformSection.value = section === 'slots' ? 'slots' : null
  view.value = 'platform-settings'
  if (mobile.value) { uberOpen.value = false; navOpen.value = false }
}
function openProjectChat(id) {
  const project = projects.value.find(candidate => candidate.id === id && !candidate.archived)
  if (project) showProjectChat(project)
  else { chatProject.value = id || null; uberOpen.value = true }
}
function toggleProjectChat() {
  if (uberOpen.value) uberOpen.value = false
  else openProjectChat(activeProject.value || chatProject.value)
}
function rememberChat(id) { if (id && !seenChats.value.includes(id)) seenChats.value.push(id) }
function projectRuns(id) { return (state.value?.runs || []).filter(run => run.workspace_id === id && !hiddenKeys.value.includes(keyOf(run))) }
function projectAttention(id) { return (state.value?.attention || []).filter(run => run.workspace_id === id) }
const unassignedRuns = computed(() => projectRuns(null))
const unassignedAttention = computed(() => projectAttention(null))

async function newProject() {
  if (creatingProject.value) return
  creatingProject.value = true
  projectError.value = null
  try {
    const project = await createProject(projectName.value)
    await load()
    projectDialog.value = false
    projectName.value = ''
    showProjectChat(project)
  } catch (exc) { projectError.value = exc.message } finally { creatingProject.value = false }
}

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

// --- the narrow band (#286) --------------------------------------------------
// The operator, after living with the first cut: on a widescreen the app is spread
// across the whole window — the run list against the left edge, the conversation's
// drawer against the right — and the fix they wanted was not a special layout for
// uber lmer at all. It is the effect of narrowing the browser window: hold the whole
// app in a band in the middle of the screen and leave the rest of it empty. As a
// platform option rather than an actual narrow window, because the window has other
// tabs in it.
//
// So this is one class and a width. Everything the mode used to do — a second dock
// for the conversation, a fixed-width run column, a pinned pane — is gone with it,
// and the drawer, the run view and the bar behave exactly as they do at full width.
//
// Remembered, unlike the drawer's open state: this is a property of the screen being
// read from, like the terminal's height and the colour scheme, and an operator who
// wants the band wants it on every load. The key is `.narrow` rather than the
// `.focus` this feature was first called, because what it names has changed —
// anything that read the old key would be reading a different feature's answer.
const NARROW_STORAGE_KEY = 'lmer.app.narrow'
const narrow = ref(storedFlag(
  () => window.localStorage.getItem(NARROW_STORAGE_KEY), false,
))

watch(narrow, (on) => {
  rememberFlag(
    (value) => window.localStorage.setItem(NARROW_STORAGE_KEY, value), on,
  )
})
// --- end of the narrow band ---------------------------------------------------

// --- the supervisor's drawer (T31) -------------------------------------------
// Closed on every load unless the band is on, and deliberately not remembered on its
// own. The landing screen is the fleet, and a remembered-open drawer would open a
// phone onto a full-screen chat instead of it — and make every glance at the fleet
// fetch the supervisor's status and its transcript. So no preference and no storage
// key of its own: this is a drawer you open to say something.
//
// The band is the one exception, and it is the operator's (#286, from living with it):
// the band exists to hold the app beside this conversation — its width is the smallest
// one at which the run view still fits *with this drawer open* — so a screen the band
// was left on is a screen the pane belongs on, whole. Not on a phone: there the band is
// inert (the window is already narrower than it), and the flag alone must not land a
// phone on a full-screen chat.
const uberOpen = ref(narrow.value && !mobile.value)
// Mounted on first open and left mounted after. The v-if is what keeps the fleet
// view free of the chat's two polls until somebody asks for it; leaving it mounted
// afterwards is what keeps a half-typed message alive across a close. The bar
// button shows no digest count for the same reason the drawer is not preloaded —
// a badge would mean polling the supervisor on every fleet load.
// Seeded from the drawer's own first frame rather than `false`, because the watcher
// below fires on a *change*: a drawer that starts open would otherwise come up empty.
const uberSeen = ref(uberOpen.value)
watch(uberOpen, (open) => {
  if (open) uberSeen.value = true
  if (open) rememberChat(chatProject.value)
})
watch(activeProject, (id) => {
  const url = new URL(window.location.href)
  if (id) url.searchParams.set('workspace', id)
  else url.searchParams.delete('workspace')
  window.history.replaceState({}, '', url)
  if (id && uberOpen.value) { chatProject.value = id; rememberChat(id) }
})

let poll = null
let tick = null

const widthHint = computed(() => narrow.value
  ? 'use the whole width of the screen'
  : 'hold the app in a band in the middle of the screen')

const keyOf = runKey

const fleetOnline = ref(false)
const browserTitle = computed(() => fleetTitle(state.value, fleetOnline.value))
watch(browserTitle, (value) => { document.title = value }, { immediate: true })

const attention = computed(() => (state.value?.attention || []).filter(run => !activeProject.value || run.workspace_id === activeProject.value))
const runs = computed(() => (state.value?.runs || []).filter(run => !activeProject.value || run.workspace_id === activeProject.value))
// Every relation in the fleet, carried on the payload so the map draws its edges
// without a request per run. Empty from a daemon older than this bundle, which
// renders as a map with no lines rather than as an error.
const payloadRelations = computed(() => state.value?.relations || null)

// --- back-compat: relations for a daemon that does not send them -------------
//
// **This block is temporary and becomes dead code the moment the deployed daemon
// carries this MR's `build_state` change.** The forward design is the `relations`
// key on the fleet payload: one read for the whole fleet, already de-duplicated,
// which is the shape the map wants and the reason that key exists.
//
// Until then a demo runs against an older daemon that omits it, and a map with no
// lines is this feature's headline property missing rather than a graceful
// degrade. So when the key is absent we ask the per-run route instead — the one
// the run detail view already uses.
//
// The cost is the thing that makes this a fallback and not a design: it is one
// request per visible run, where the payload key is one for the fleet. It is kept
// survivable by refetching only when the *set of runs* changes rather than on
// every poll — a ten-second poll over fourteen runs would otherwise be 84
// requests a minute for data that changes when somebody relates two runs.
const fetchedRelations = ref([])
let relationsFetchedFor = null

async function loadRelationsFallback(keys, rows) {
  relationsFetchedFor = keys
  const replies = await Promise.all(rows.map(async (run) => {
    try {
      return await fetchRunRelations(run)
    } catch {
      // One failed read must not cost the whole edge set: a run the daemon will
      // not answer for contributes no edges, exactly as it would if it had none.
      // Caught around the call as well as the promise — this is a fallback for
      // an older daemon, and "the route is not there at all" is one of the ways
      // an older daemon says no.
      return null
    }
  }))
  // A newer poll may have landed while these were in flight; its answer wins.
  if (relationsFetchedFor !== keys) return
  fetchedRelations.value = pairsFromReplies(replies.filter(Boolean))
}

watch([payloadRelations, runs], ([carried, rows]) => {
  if (carried) {
    // The daemon grew the key. Drop the fallback and never ask again.
    fetchedRelations.value = []
    relationsFetchedFor = null
    return
  }
  const keys = rows.map(runKey).sort().join('\n')
  if (keys === relationsFetchedFor) return
  if (!rows.length) {
    relationsFetchedFor = keys
    fetchedRelations.value = []
    return
  }
  loadRelationsFallback(keys, rows)
}, { immediate: true })

// Every relation in the fleet. From the payload when the daemon sends it; from
// the per-run fallback above while it does not.
const relations = computed(() => payloadRelations.value || fetchedRelations.value)

// The platform's own session, which the map deliberately does not draw (it is one
// tap away in the bar instead). The bar control carries its state, so the thing
// that replaced the node still answers the question the node answered.
const orchestrator = computed(() => orchestratorOf(runs.value))
const orchestratorTone = computed(() => (
  orchestrator.value ? toneColor(stateMeta(orchestrator.value.state).tone) : null
))
const orchestratorHint = computed(() => {
  const run = orchestrator.value
  if (!run) return 'talk to uber lmer'
  const meta = stateMeta(run.state)
  const parts = [`uber lmer — ${meta.label}`]
  // What it is doing, or what it needs. The attention note is the more urgent of
  // the two and wins when both are there.
  const note = run.attention?.note || run.title || run.goal || run.phase
  if (note) parts.push(note)
  return parts.join(' · ')
})
const fleetView = ref(storedFleetView())
watch(fleetView, rememberFleetView)
const calm = computed(() => runs.value.filter((run) => !run.attention))
const totals = computed(() => ({ runs: runs.value.length, live: runs.value.filter(run => run.live).length, attention: attention.value.length }))
const mirror = computed(() => state.value?.mirror || null)
const slots = computed(() => state.value?.slots || [])
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

// The map draws the rows the list would draw, search included. A search box that
// quietly does nothing while the map is up would be a control the operator has to
// learn the exceptions to; and `nothingMatches` then means the same thing in both
// views, which is what lets one message serve them.
const mapRows = computed(() => visible(runs.value))
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
      projects.value = (await fetchProjects()).projects
      if (uberOpen.value) rememberChat(chatProject.value)
      fleetOnline.value = true
      error.value = null
      // Rows the daemon no longer reports are forgotten for real, so the key has
      // nothing left to hide; keeping them would grow this list for the life of
      // the tab. A row still inside its undo window is still in the payload.
      hiddenKeys.value = hiddenKeys.value.filter(
        (key) => (state.value?.runs || []).some((run) => keyOf(run) === key),
      )
    } catch (exc) {
      fleetOnline.value = false
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
  activeProject.value = run.workspace_id || ''
  selectedKey.value = keyOf(run)
  questionsFocus.value = null
  view.value = 'detail'
  // On a phone the drawer is an overlay sitting on top of the run it was just
  // used to open.
  if (mobile.value) navOpen.value = false
}

// --- run references written in chat (issue #241) -----------------------------
//
// The resolver lives here because this is where the fleet lives: whether a
// reference is live is the same question as whether its key is in that list. An
// untracked key is not a switch — the fleet list is what a run is selected out of,
// so it would land on an empty page (RelatedRuns.vue's reasoning) — so it reports
// instead.
const runRefNotice = ref(null)
let stopRunRefs = null

// On a phone the drawer would cover the settings it opens.
let settingsNonce = 0

function openProjectSettings(id, section) {
  activeProject.value = id
  view.value = 'project-settings'
  settingsNonce += 1
  projectSettingsFocus.value = section ? { id, section, nonce: settingsNonce } : null
  if (mobile.value) uberOpen.value = false
}

// uber lmer's question badge (issue 507): open its own fleet row and point the run
// view at the ask dock. A nonce, so a second tap on the same run focuses again.
const questionsFocus = ref(null)
let questionsNonce = 0

function openAssistantQuestions(sessionId) {
  const run = (state.value?.runs || []).find((candidate) => candidate.orchestrator
    && sessionId && (candidate.session?.id === sessionId || candidate.last_session_id === sessionId))
  if (!run) {
    runRefNotice.value = 'uber lmer\'s session is not in the fleet view yet — its questions will be answerable there once it is'
    return
  }
  runRefNotice.value = null
  open(run)
  questionsNonce += 1
  questionsFocus.value = { key: keyOf(run), nonce: questionsNonce }
  if (mobile.value) uberOpen.value = false
}

function openRunRef(ref) {
  const run = (state.value?.runs || []).find((candidate) => keyOf(candidate) === ref.key)
  if (!run) {
    runRefNotice.value = `${ref.key} — ${NOT_TRACKED_HINT}`
    return
  }
  runRefNotice.value = null
  open(run)
  // On a phone the assistant's drawer is an overlay over the run it just opened.
  if (mobile.value) uberOpen.value = false
}

// A file dropped anywhere the composer is not. The browser's default action for
// a drop on a page is to *navigate to the file*, so a screenshot released on the
// message list — which sits directly above the composer and is the large obvious
// target — unloaded the SPA and replaced it with the raw image, taking the draft,
// the staged tray and any unsettled bubbles with it (!272 review). The composer's
// own handlers stage the file and prevent the default themselves; these make
// every miss a no-op instead of a page load.
//
// On `window` rather than on the app's root element, because the default fires
// for the document however far the drop lands from any component of ours.
function swallowStrayDrop(event) {
  event.preventDefault()
}

onMounted(() => {
  load()
  window.addEventListener('dragover', swallowStrayDrop)
  window.addEventListener('drop', swallowStrayDrop)
  stopRunRefs = onRunRef(openRunRef)
  poll = setInterval(load, POLL_MS)
  // Separate, faster tick so "3m ago" ages without refetching the fleet.
  tick = setInterval(() => { now.value = Date.now() }, 1000)
  document.addEventListener('visibilitychange', refetchOnReturn)
  window.addEventListener('focus', refetchOnReturn)
})

onUnmounted(() => {
  window.removeEventListener('dragover', swallowStrayDrop)
  window.removeEventListener('drop', swallowStrayDrop)
  if (stopRunRefs) stopRunRefs()
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
  <!-- The narrow band is a class on the app's own root, because the things that sit
       against the window's edges are the app bar and the two drawers — all three
       `position: fixed`, which no `max-width` on an ancestor can reach. The rules
       are at the bottom of this file. -->
  <v-app :class="{ 'app-narrow': narrow, 'app-mobile': mobile }">
    <v-app-bar :elevation="1" density="comfortable" :color="barColor" :style="barStyle">
      <v-tooltip v-if="mobile" text="show the run list" location="bottom" open-on-focus>
        <template #activator="{ props: tooltip }">
          <v-app-bar-nav-icon v-bind="tooltip" aria-label="show the run list" @click="navOpen = !navOpen" />
        </template>
      </v-tooltip>
      <v-tooltip v-if="view !== 'fleet'" text="back to the fleet" location="bottom" open-on-focus>
        <template #activator="{ props: tooltip }">
          <v-btn v-bind="tooltip" :icon="mdiArrowLeft" variant="text" aria-label="back to the fleet" @click="view = 'fleet'" />
        </template>
      </v-tooltip>
      <div v-if="!mobile" class="bar-switcher">
        <ProjectSwitcher v-bind="switcherProps" v-on="switcherEvents" />
      </div>
      <v-app-bar-title class="text-title-medium">{{ title }}</v-app-bar-title>
      <div class="bar-cluster" :class="{ 'bar-cluster-centered': !mobile }">
        <v-tooltip :text="orchestratorHint" location="bottom" open-on-focus>
          <template #activator="{ props: tooltip }">
            <v-btn v-bind="tooltip" :icon="mdiRobot" :color="orchestratorTone" variant="text"
              :class="{ 'robot-on-surface': barColor && orchestratorTone }"
              :aria-label="orchestratorHint" @click="toggleProjectChat" />
          </template>
        </v-tooltip>
        <v-tooltip v-if="!mobile" :text="widthHint" location="bottom" open-on-focus>
          <template #activator="{ props: tooltip }">
            <v-btn v-bind="tooltip" :icon="narrow ? mdiArrowExpandHorizontal : mdiArrowCollapseHorizontal"
              :variant="narrow ? 'tonal' : 'text'" :aria-label="widthHint" @click="narrow = !narrow" />
          </template>
        </v-tooltip>
        <v-menu location="bottom end">
          <template #activator="{ props: picker }">
            <v-tooltip :text="`colour scheme: ${THEME_LABELS[themeMode]}`" location="bottom" open-on-focus>
              <template #activator="{ props: tooltip }">
                <v-btn v-bind="mergeProps(picker, tooltip)" :icon="THEME_ICONS[themeMode]" variant="text"
                  :aria-label="`colour scheme: ${THEME_LABELS[themeMode]}`" />
              </template>
            </v-tooltip>
          </template>
          <v-list :selected="[themeMode]" density="compact">
            <v-list-item v-for="mode in THEME_MODES" :key="mode" :value="mode"
              :prepend-icon="THEME_ICONS[mode]" :title="THEME_LABELS[mode]" @click="themeMode = mode" />
          </v-list>
        </v-menu>
        <v-tooltip text="Platform settings" location="bottom" open-on-focus>
          <template #activator="{ props: tooltip }">
            <v-btn v-bind="tooltip" :icon="mdiCogOutline" variant="text" aria-label="Platform settings"
              :aria-pressed="view === 'platform-settings'" @click="showPlatformSettings" />
          </template>
        </v-tooltip>
        <template v-if="view === 'fleet'">
          <v-btn-toggle v-model="fleetView" class="me-1" density="compact" mandatory variant="text">
            <v-tooltip text="read the fleet as a list" location="bottom" open-on-focus>
              <template #activator="{ props: tooltip }">
                <v-btn v-bind="tooltip" value="list" :icon="mdiViewListOutline" aria-label="read the fleet as a list" />
              </template>
            </v-tooltip>
            <v-tooltip text="read the fleet as a constellation map" location="bottom" open-on-focus>
              <template #activator="{ props: tooltip }">
                <v-btn v-bind="tooltip" value="map" :icon="mdiMapOutline" aria-label="read the fleet as a constellation map" />
              </template>
            </v-tooltip>
          </v-btn-toggle>
          <v-tooltip text="rescan now" location="bottom" open-on-focus>
            <template #activator="{ props: tooltip }">
              <v-btn v-bind="tooltip" :icon="mdiRefresh" :loading="busy" variant="text" aria-label="rescan now" @click="doRescan" />
            </template>
          </v-tooltip>
          <v-tooltip text="spawn or adopt a run" location="bottom" open-on-focus>
            <template #activator="{ props: tooltip }">
              <v-btn v-bind="tooltip" class="me-2" variant="tonal" :icon="mobile ? mdiPlus : undefined"
                :prepend-icon="mobile ? undefined : mdiPlus" aria-label="spawn or adopt a run" @click="view = 'add'"
              ><v-icon v-if="mobile" :icon="mdiPlus" /><template v-else>run</template></v-btn>
            </template>
          </v-tooltip>
        </template>
      </div>
      <!-- A phone's bar has no width left for a name beside its controls, so the
           switcher takes a row of its own there, inside the bar and so still fixed. -->
      <template v-if="mobile" #extension>
        <ProjectSwitcher v-bind="switcherProps" v-on="switcherEvents" />
      </template>
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
      <div class="project-create pa-3">
        <v-btn block variant="tonal" :prepend-icon="mdiPlus" @click="projectDialog = true">New project</v-btn>
      </div>
      <ProjectNav v-for="project in projectGroups" :key="project.id" :project="project"
        :selected="project.id === activeProject" :runs="projectRuns(project.id)" :attention="projectAttention(project.id)"
        :current-key="view === 'detail' && selected?.workspace_id === project.id ? selectedKey : null" :now="now" @open="open" @chat="showProjectChat" />
      <section v-if="unassignedRuns.length || unassignedAttention.length" aria-label="Unassigned" class="unassigned-runs">
        <h2 class="text-title-small pa-3">Unassigned</h2>
        <RunNav :runs="unassignedRuns" :attention="unassignedAttention"
          :current-key="view === 'detail' && !selected?.workspace_id ? selectedKey : null" :now="now" @open="open" />
      </section>
    </v-navigation-drawer>

    <!-- The supervisor's chat, on the right, and the side is the feature: the
         operator has two conversations — with an lmer working in a repository and
         with the uber lmer watching all of them — and telling the second to stop
         something while typing at the first is the mistake worth designing
         against. So they never share an edge, and this one carries the project-colour
         toolbar its component draws.
         No `permanent` and no `temporary`: Vuetify reads the same `mobile`
         breakpoint the left drawer is given, so it sits beside the content on a
         desktop and comes over it with a scrim on a phone, and the two drawers
         cannot end up disagreeing about which device this is. -->
    <!-- Twice the navigator's width: this drawer holds a conversation, not a
         list, and prose in a 360px column wraps too hard to read. -->
    <v-navigation-drawer v-model="uberOpen" location="right" width="720">
      <div v-for="project in openedChats" :key="project.id" v-show="project.id === chatProject" class="uber-pane-slot">
        <AssistantChat v-if="uberSeen" :workspace="project" :now="now"
          @settings="openProjectSettings(project.id, $event)" @close="uberOpen = false"
          @questions="openAssistantQuestions" />
      </div>
      <p v-if="!chatWorkspace || chatWorkspace.archived" class="pa-4">Choose an active project to open its assistant chat.</p>
    </v-navigation-drawer>

    <v-main>
      <v-container max-width="900" class="pa-3">
        <v-alert v-if="!loading && activeProject && (!workspace || workspace.archived)" type="warning" class="mb-3">
          This project link is unavailable or archived. Choose another project, or restore it from Platform settings.
        </v-alert>
        <ProjectSettings v-if="view === 'project-settings' && workspace && !workspace.archived" :key="workspace.id"
          :project="workspace" :focus="projectSettingsFocus" @changed="load" @close="view = 'fleet'"
          @focused="projectSettingsFocus = null" />
        <PlatformSettings :state="state" :loading="loading" v-else-if="view === 'platform-settings'" :projects="projects" :slots="slots"
          :runs="state?.runs || []" :now="now" :focus-section="platformSection" @changed="load" @open="open" />
        <!-- Keyed by the run, so switching from the drawer starts the detail
             view over instead of reusing one that still holds the last run's
             error. The session views restart on their own (they watch the
             session id); this is about the state that has no watcher. -->
        <RunDetail
          v-else-if="view === 'detail' && selected"
          :key="`${selectedKey}:${selected?.workspace_id}`"
          :run="selected"
          :identified-workspace-id="workspace?.id"
          :slots="slots"
          :now="now"
          :projects="projects"
          :focus-questions="questionsFocus?.key === selectedKey ? questionsFocus.nonce : 0"
          @close="view = 'fleet'"
          @changed="load"
          @open="open"
        />
        <AddRun
          v-else-if="view === 'add'"
          :key="activeProject || 'choose-project'"
          :workspace-id="activeProject || null"
          :projects="projects"
          :free-slots="freeSlots"
          :slots="slots"
          @changed="load"
        />

        <template v-else-if="!activeProject">
          <v-alert v-if="error" type="error" class="mb-3">{{ error }}</v-alert>
          <ProjectSlots :slots="slots" :runs="state?.runs || []" :projects="projects" :loaded="!!state"
            :problem="state?.slots_problem || (error ? 'Slot status could not be refreshed.' : null)" />
          <ForgetCompleted
            :key="activeProject || 'all'"
            v-if="runs.length"
            :runs="runs.filter((run) => !hiddenKeys.includes(keyOf(run)))"
            @forgotten="(run) => { hiddenKeys = [...hiddenKeys, keyOf(run)] }"
            @changed="load"
          />
          <section v-for="project in projectGroups" :key="project.id" class="mb-5">
            <h2 class="text-title-medium project-name" :style="{ borderInlineStart: `4px solid ${projectColor(project.color)}`, paddingInlineStart: '12px' }">{{ project.name }}</h2>
            <RunNav :runs="projectRuns(project.id)" :attention="projectAttention(project.id)" :now="now" @open="open" />
          </section>
          <section v-if="unassignedRuns.length || unassignedAttention.length" aria-label="Unassigned" class="unassigned-runs mb-5">
            <h2 class="text-title-medium">Unassigned</h2>
            <RunNav :runs="unassignedRuns" :attention="unassignedAttention" :now="now" @open="open" />
          </section>
          <p v-if="!loading && !projects.length">Create a project to start.</p>
        </template>
        <template v-else>
          <v-alert v-if="error" type="error" class="mb-3">{{ error }}</v-alert>
          <ProjectSlots v-if="workspace && !workspace.archived" :slots="slots" :runs="state?.runs || []" :projects="projects"
            :project="workspace" :loaded="!!state" :problem="state?.slots_problem || (error ? 'Slot status could not be refreshed.' : null)"
            @manage="showPlatformSettings('slots')" />
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

          <ForgetCompleted
            :key="activeProject || 'all'"
            v-if="runs.length"
            :runs="runs.filter((run) => !hiddenKeys.includes(keyOf(run)))"
            @forgotten="(run) => { hiddenKeys = [...hiddenKeys, keyOf(run)] }"
            @changed="load"
          />

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
            <!-- The map draws the same fleet the cards below do, filtered by the
                 same search, and hands a click back to the same `open`: it is
                 another way to choose a run, not another way to read one. -->
            <FleetMap
              v-if="fleetView === 'map'"
              :runs="mapRows"
              :relations="relations"
              :groups="state?.config?.fleet_groups || []"
              @open="open"
            />

            <!-- One branch around the whole list rather than a condition on each
                 of its three parts: the cards, their headings and the split
                 between them are one view, and conditioning them separately is
                 how they drift out of step. -->
            <template v-if="fleetView === 'list'">
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
                  @open-chat="openProjectChat(run.workspace_id)"
                  @threads-changed="load"
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
                @open-chat="openProjectChat(run.workspace_id)"
                @threads-changed="load"
              />
            </template>

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

        </template>
      </v-container>
    </v-main>

    <v-dialog v-model="projectDialog" max-width="480">
      <v-card title="New project">
        <v-card-text>
          <v-alert v-if="projectError" type="error" class="mb-2">{{ projectError }}</v-alert>
          <v-text-field v-model="projectName" label="Project name" :disabled="creatingProject" autofocus @keydown.enter="newProject" />
          <p>Its assistant starts when capacity is available.</p>
        </v-card-text>
        <v-card-actions><v-spacer /><v-btn @click="projectDialog = false">Cancel</v-btn><v-btn :loading="creatingProject" @click="newProject">Create project</v-btn></v-card-actions>
      </v-card>
    </v-dialog>


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

    <!-- Its own bar rather than the content column's error alert: the tap happens
         in a drawer that covers that column on a phone, and an explanation nobody
         sees is the dead link this feature exists to remove. Timed, because
         nothing is pending. -->
    <v-snackbar
      :model-value="!!runRefNotice"
      :timeout="8000"
      multi-line
      @update:model-value="runRefNotice = null"
    >
      {{ runRefNotice }}
    </v-snackbar>
  </v-app>
</template>

<style scoped>
.uber-pane-slot {
  height: 100%;
}

/* The tone palette was chosen against the theme's surface, not a project's color
   (green "running" vanishes on a green project), so on a project bar a toned robot
   keeps its tone and sits on a disc of that surface. Untoned, it is bar text. */
.robot-on-surface.v-btn {
  background: rgb(var(--v-theme-surface));
}

.app-mobile .bar-cluster .robot-on-surface.v-btn {
  height: 36px;
}

/* Vuetify paints a button group in the theme's on-surface color, which on a
   project-colored bar can be black on navy; it follows the bar like its siblings. */
.bar-cluster :deep(.v-btn-group) {
  color: inherit;
}

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

.bar-switcher {
  flex: 0 1 auto;
  min-width: 0;
  max-width: 240px;
}

.project-create {
  border-block-end: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  margin-block-end: 8px;
}

.app-mobile .bar-cluster :deep(.v-btn--icon) {
  width: 36px;
  height: 40px;
}

/* The band. What the mode does, and all it does: hold the whole app in a strip in
   the middle of a wide screen and leave the rest of the window empty — the effect of
   narrowing the browser, without narrowing a window that has other tabs in it.

   It is four rules rather than a `max-width` on one element because the parts that
   sit against the window's edges are `position: fixed` (the app bar and both
   drawers), and a fixed box takes its edges from the viewport however its ancestors
   are sized. The alternative — giving an ancestor a transform so it becomes their
   containing block — turns `fixed` into something that scrolls with the page, which
   is the app bar and the drawers scrolling away.

   `100vw` includes a vertical scrollbar where the platform draws one, so the band can
   sit up to half a scrollbar off true centre. Left alone deliberately: correcting it
   means measuring the scrollbar in script, and the error is a few pixels on the
   screens this mode is for. */
.app-narrow {
  /* The band's width, in one place, and the number is measured rather than chosen:
     it is the smallest band at which the terminal's control row still fits on one
     line with uber lmer's drawer open (operator report — at 1600 the `redraw` button
     wrapped to a second row). With the drawer open the content column gets
     `band - 280 - 720`, so the run view sees 620px here; 619 wraps, 620 does not.
     The extra width comes out of the empty page either side, not out of the run list
     or the drawer — both are untouched at 280 and 720.
     No slack on purpose, since the operator asked for the smallest: the row keeps
     its own `flex-wrap`, so a control added to it later wraps rather than overflows,
     and a transiently wider row (the `reconnecting` chip is far wider than `live`)
     can still wrap by design. Change this line to change the band. */
  --app-band: 1620px;
  --app-inset: max(0px, (100vw - var(--app-band)) / 2);
}

/* `!important` on exactly these, and only these: Vuetify's layout composable writes
   `position`, `left`/`right` and `width` as INLINE styles on every layout item it
   places (measured on the built bundle — the bar carries
   `left: 0px; width: calc(100% + 0px)`, the navigator `left: 0px; width: 280px`), and
   an inline declaration outranks any stylesheet. The alternative is a wrapper with a
   transform, which makes it the containing block for its fixed descendants and turns
   the bar and both drawers into things that scroll away with the page.
   The width is released as well as the edges moved, because a left/right inset cannot
   shrink a box whose width the framework has already pinned. Everything else about
   these elements — the open/closed transform, the z-index, the heights — is left to
   the framework. */
.app-narrow .v-app-bar {
  left: var(--app-inset) !important;
  right: var(--app-inset) !important;
  width: auto !important;
}

.app-narrow .v-navigation-drawer--left {
  left: var(--app-inset) !important;
}

.app-narrow .v-navigation-drawer--right {
  right: var(--app-inset) !important;
}

/* Parked, the drawer has to clear the window's edge — and moving that edge inwards is
   what stopped it clearing. The framework parks a right drawer by translating it its
   own width plus a pixel (measured: `translateX(721px)` on a 720px drawer), which lands
   it exactly on whatever `right` says, and `right` is now `--app-inset` from the edge.
   So a closed drawer left the inset's worth of itself sitting in the band's right-hand
   gutter — 469px at 2560 — empty on a fresh load, because the conversation is not
   mounted until it is first opened. The park is extended by the same inset. `transform`
   is what the framework transitions, so it still slides rather than jumping, which is
   why this moves the park instead of moving `right` back to the edge. */
.app-narrow .v-navigation-drawer--right:not(.v-navigation-drawer--active) {
  transform: translateX(calc(100% + 1px + var(--app-inset))) !important;
}

/* The content between them. The two layout variables and the safe-area insets are
   style.css's rule for this element, restated because the band adds to it rather
   than replacing it — dropping either half would put the content under a drawer or
   under a phone's rounded corner. */
.app-narrow .v-main {
  padding-left: calc(
    var(--v-layout-left, 0px) + env(safe-area-inset-left) + var(--app-inset)
  );
  padding-right: calc(
    var(--v-layout-right, 0px) + env(safe-area-inset-right) + var(--app-inset)
  );
}
</style>
