<script setup>
import { computed, defineAsyncComponent, ref, watch } from 'vue'
import {
  mdiRobot, mdiClipboardTextOutline, mdiClockAlertOutline,
  mdiClockOutline, mdiCommentQuestionOutline, mdiConsole, mdiExitRun,
  mdiEyeOffOutline, mdiFileDocumentOutline, mdiFolderOpenOutline,
  mdiForumOutline, mdiInformationOutline, mdiLinkVariant, mdiOpenInNew,
  mdiPlayOutline, mdiPowerPlugOff, mdiRobotOutline, mdiSourceBranchPlus,
  mdiSourceRepository, mdiTextBoxOutline, mdiWeatherSunset,
} from '@mdi/js'
import AnswerBox from './AnswerBox.vue'
import AskChannel from './AskChannel.vue'
import AskHistory from './AskHistory.vue'
import Chat from './Chat.vue'
import RelatedRuns from './RelatedRuns.vue'
import RunMeta from './RunMeta.vue'
import { forgetRun, resumeRun } from '../api.js'
import {
  ago, attentionLabel, duration, ledgerSummary, portUrl, shortTarget, stateMeta,
  targetLink, targetRef, toneColor,
} from '../format.js'
import { rememberChoice, storedChoice } from '../preferences.js'

// The emulator is over half the JS in the bundle and only this view opens one, so
// it arrives as its own chunk. It is now deferred twice over: a tab panel renders
// nothing until it is first selected, so the chunk is fetched when the terminal is
// opened rather than when the run is.
//
// Chat is imported normally, and the numbers are why: it brings no library of its
// own, so it costs the fleet view 4.5 kB gzipped. Splitting it recovers 3.1 kB of
// that and charges the detail view two extra requests plus a shared-helper chunk
// that nobody named. The terminal split saved 90 kB gzipped — an order of
// magnitude more — which is the scale that earns a second round trip.
const Terminal = defineAsyncComponent(() => import('./Terminal.vue'))

// The shared renderer, deferred the way every consumer defers it (T42). Three
// surfaces here are prose an agent wrote — why the run needs a human, what it is
// working towards, and what it noted on an event — and all three are *lines*, so
// they are rendered in the compact mode that cannot produce a block element (T46).
// Deferred still costs the fleet view nothing: this chunk is fetched when one of
// those first renders, which is inside a run.
const Markdown = defineAsyncComponent(() => import('./Markdown.vue'))

const props = defineProps({
  run: { type: Object, required: true },
  now: { type: Number, default: () => Date.now() },
})

// `open` is forwarded from the related-runs switcher at the bottom of the page
// (T53) and belongs to the shell: which run is selected is App.vue's state, the
// same state the fleet list and the drawer change. A tap here would otherwise have
// to reach around it, and this component is built for exactly one run.
const emit = defineEmits(['close', 'changed', 'open'])

const busy = ref(false)
const error = ref(null)

// --- which view of a run this operator likes (T49) ---------------------------
//
// The page is four tabs: the run's own facts, what it is about, the session's
// three views, and how this run ends. Stacked, it was five cards and two scrolling
// panes deep, and on a phone the wind-down button lived somewhere below a
// transcript.
//
// The operator asked: "i prefer using the terminal, but everytime i go into a run
// it defaults back to conversation, can we have the ui store my choice?" So the
// choice is remembered — and remembered *globally*, because the preference is "I
// read the terminal", not "this run opens on the terminal". That is also why it is
// stored in the browser rather than in a module-level ref: App.vue keys this
// component on the run, so switching runs builds a new one, and a reload starts
// the whole app over.
//
// Both levels are remembered, and both are validated on the way back in — see
// preferences.js for why a remembered tab name is not to be trusted. The short
// version: Vuetify renders no panel for a value that selects none, so a tab that
// was renamed or dropped would leave a blank run view behind. First entry is the
// default in both lists.
//
// --- remembered choice (extracted by tests/test_platform_web_details_tabs.py) --
// Every tab that exists is in this list and every entry of it has a panel, which
// is the invariant the validation below rests on: a value that selects no panel
// renders a blank run view, and a tab missing from here can never be remembered.
const TABS = ['overview', 'meta', 'lmer', 'exit']
// Conversation first: "what is going on" is the common question, and it is the
// cheap panel to render.
const PANES = ['conversation', 'terminal', 'chat']

const RUN_TAB_STORAGE_KEY = 'lmer.run.tab'
const RUN_PANE_STORAGE_KEY = 'lmer.run.pane'

function storedTab() {
  return storedChoice(() => window.localStorage.getItem(RUN_TAB_STORAGE_KEY), TABS)
}

function storedPane() {
  return storedChoice(() => window.localStorage.getItem(RUN_PANE_STORAGE_KEY), PANES)
}

function rememberTab(value) {
  rememberChoice(
    (stored) => window.localStorage.setItem(RUN_TAB_STORAGE_KEY, stored), TABS, value,
  )
}

function rememberPane(value) {
  rememberChoice(
    (stored) => window.localStorage.setItem(RUN_PANE_STORAGE_KEY, stored), PANES, value,
  )
}
// --- end of remembered choice ------------------------------------------------

const tab = ref(storedTab())
const pane = ref(storedPane())

// Written when the operator moves, which is the only thing that writes these: the
// tab bar and its panels share one model, so a watcher catches every route to a new
// tab without caring which of them did it.
watch(tab, (value) => rememberTab(value))
watch(pane, (value) => rememberPane(value))

// --- the run's own files (T66) ------------------------------------------------
//
// The operator asked: "the detail view should have a list of run files, clicking
// takes the user to the work repo (gitlab, github etc.,) to view the file". The
// run dir holds the whole record — the spec, the goals, the plan, the ledger, the
// events, the reports — and this view showed none of it.
//
// Fetched when the overview panel is first opened rather than with the fleet poll:
// the payload that feeds every row must not grow a directory walk per run for
// something only an open run renders. Once per visit is enough — the list is the
// set of files a finished phase left behind, not a progress indicator, and the
// panel is rebuilt whenever the operator opens the run again.
const runFiles = ref([])
const runFilesMeta = ref(null)
const runFilesError = ref(null)
const runFilesLoaded = ref(false)
let runFilesRequested = false

// Inline for the reason postVerb below is inline: this slice's file scope was the
// component. api.js is where it belongs the next time this file is opened.
async function loadRunFiles() {
  if (runFilesRequested) return
  runFilesRequested = true
  try {
    const query = new URLSearchParams({
      host: props.run.host, project: props.run.project, slug: props.run.slug,
    })
    const response = await fetch(`api/runs/files?${query}`, {
      credentials: 'same-origin',
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok) throw new Error(payload?.detail || `HTTP ${response.status}`)
    runFiles.value = payload.files || []
    runFilesMeta.value = payload
  } catch (exc) {
    // Shown, not swallowed: an empty list and a failed read look identical
    // otherwise, and one of them means the run pushed nothing.
    runFilesError.value = exc.message
  } finally {
    runFilesLoaded.value = true
  }
}

// `immediate` because overview is the default tab and a remembered one may already
// select it, so there is no first switch to hear about.
watch(tab, (value) => {
  if (value === 'overview') loadRunFiles()
}, { immediate: true })

// Why a file is named but not clickable, said next to the file rather than left in
// the API docs. Both remaining reasons are the operator's own settings, since a
// work-repo host the daemon cannot classify now gets GitLab's layout rather than no
// link: either no work repo is configured, or work_repo_forge=none switched the
// links off. The run's own host is deliberately NOT named — these links point at
// the work repo, which is a different host from the one the run's code lives on.
const unlinkedFileHint = 'no link — this platform builds no browse URLs for the '
  + 'work repo: none is configured, or links are switched off for it '
  + '(work_repo_forge=none). The files are named but not linked.'

const meta = computed(() => stateMeta(props.run.state))
const ledger = computed(() => ledgerSummary(props.run.ledger))
const session = computed(() => props.run.session || null)

// The run's identity — host, project and the slug it is recorded under — for the
// overview's "run" row when the daemon sent no address for it.
//
// It is not a path and must not be made to look like one by adding `runs/` to it:
// a named run's directory is `runs/<slug>--<name>`, so the composed path was wrong
// for most runs and right for none that mattered. The daemon sends `rel_path` only
// for a directory it actually found (workrepo.resolve_run_dir confirms it by
// content), and a row with no directory yet — tracked but unpushed, or a session
// whose first commit has not landed — has this instead.
const runKey = computed(
  () => `${props.run.host}/${props.run.project}/${props.run.slug}`,
)

// What the run last recorded about itself, joined here rather than in the
// template. Two things follow from that, and both are the fleet row's `committed`
// argument one view later: a run that recorded a stop reason and no status renders
// the reason alone rather than a stray "(question)", and a run that has recorded
// neither — which is every run in its first seconds — is *absent*. Absent renders
// nothing, so the row goes with it; the em dash that used to stand here was the
// view claiming a fact nobody has.
const status = computed(() => [
  props.run.status,
  props.run.stop_reason && `(${props.run.stop_reason})`,
].filter(Boolean).join(' ') || null)

// How long the live session's harness has been quiet (T95), in the fleet row's
// words and from the daemon's own measurement — one monotonic clock in the process
// that saw the output (lmer_cli.supervisor), so nothing about this device's clock
// can turn a busy session into an idle-looking one. The row already says it; this
// view is where the operator lands from that row, and having to go back to the
// list to find out whether the session is still doing anything is the trip the
// fleet view exists to save.
//
// Absent renders nothing, and absent is ordinary: a session the platform cannot
// reach and one whose image predates the fact both report nothing rather than an
// idle of zero, which would read as "just did something".
const idle = computed(() => {
  const label = duration(props.run.session?.activity?.idle_seconds)
  return label ? `idle ${label}` : null
})

// The issue/PR/MR the target names, in the words the fleet row already uses: a
// run gets talked about as "MR #164", and that is also what fits a header line
// where the URL does not. Parsed by format.js and not here (T50) — the row and
// this header must never disagree about what a target points at — and null is
// ordinary rather than exceptional, so the header falls back to the shortened
// target for a bare repo, a compare range or a taskdef whose target is prose.
const resource = computed(() => targetRef(props.run.target))

// The words the target is shown as, in one place because two branches render them:
// a target is a link only when it is one (below), and the label must not be able to
// differ between the linked and the unlinked branch.
const targetLabel = computed(
  () => (resource.value ? resource.value.label : shortTarget(props.run.target)),
)

// Where the target goes, and null when there is nowhere to go — format.js's gate,
// the same one the fleet row uses. Unconditionally an `<a href>`, a target that is
// not an absolute URL is a *relative* link: the orchestrator's own `fleet`, a branch
// name or a prose target resolves against this page and 404s (the operator asked:
// "its target does link to a 404 currently"). So the anchor is what is
// conditional, not the target.
const targetHref = computed(() => targetLink(props.run.target))

// The terminal follows the log, not the container, so it is offered for a session
// that has exited too: `last_session_id` is the live session when there is one and
// the last one this orchestrator ran otherwise (inventory.session_id_for_history).
// A clean exit removes the registry entry while the log deliberately stays, and
// that history is the record of everything the run did.
const terminalSession = computed(() => props.run.last_session_id || null)

// A run stopped on a question has no session to type into, and answering it is a
// separate action from chatting (T19). The chat view needs to know which of those
// it is looking at, and the fleet view is where that fact already lives — it is
// also what decides whether the answer box is offered at all.
const questionPending = computed(() => props.run.attention?.reason === 'question')

// The other question case, and pointedly not the same one (T23): a session that
// is *running* and waiting on its ask channel. Answering that one delivers a file
// the container is already polling — no respawn — so it gets its own section and
// its own component, and the two are never both offered.
const askPending = computed(() => props.run.attention?.reason === 'live_question')

// Whether the surface below the alert already renders the attention note, in which
// case the alert says only that a reply is wanted. For both question reasons the
// note *is* the question, and the two things that can act on it — the answer box
// for a run that stopped to ask, the channel dock for a live session — render the
// same text a few lines further down (the operator asked: "the details page shows
// the is waiting on your reply full text in the alert, then again right below in the
// operation note, the alert can literally just be 'waiting for your reply'").
//
// Every other reason keeps its note, and that is the half worth being careful
// about: a crash's note is the only account of a crash anywhere on this page.
// Keyed on the surface being *present* rather than on the reason alone, because a
// live question on a run with no session id renders no dock — and dropping the text
// then would leave the page asking for a reply and never saying what to.
const noteRepeatedBelow = computed(
  () => questionPending.value || (askPending.value && !!terminalSession.value),
)

// --- ending this session (spec §7.5 / D22) -----------------------------------
//
// Two verbs, and they are deliberately NOT two buttons side by side. Wind down is
// a request the agent gets to finish — commit, push, report — so it is the large
// primary action with a place to add to it. Exit signals immediately and the agent
// gets nothing, so it is small, subordinate, and behind a confirmation. The reason
// that asymmetry is load-bearing rather than decorative: a session that bound a
// port and set something up for you to look at must not be shut down out from
// under you, and the blunt verb is one mis-aimed tap away on a phone.

const windDownNote = ref('')
const winding = ref(false)
const ending = ref(false)
const confirmingExit = ref(false)
const lifecycleError = ref(null)
// What the daemon said when it accepted the request, kept so the card can answer
// immediately: the fleet poll is seconds away and the registry record only appears
// with it.
const windDownSent = ref(null)

// The daemon's own record of the request, on the session's registry entry — which
// is what makes this survive a reload, and what makes it true rather than local
// optimism. The fleet payload carries the whole entry, so it costs no request.
const lifecycle = computed(() => session.value?.lifecycle || null)
const windDownAsked = computed(
  () => !!windDownSent.value || lifecycle.value?.verb === 'wind_down',
)
const windDownAt = computed(
  () => windDownSent.value?.requested_at || lifecycle.value?.requested_at || null,
)

// The backstop the daemon recorded (spec R18). Nothing escalates when it passes —
// D22 forbids that — so this is the surfacing instead: past the deadline the card
// says the wind-down has not finished and leaves the choice where it belongs.
const windDownOverdue = computed(() => {
  const at = windDownSent.value?.backstop_at || lifecycle.value?.backstop_at
  const deadline = at ? Date.parse(at) : NaN
  return !Number.isNaN(deadline) && deadline < props.now
})

// Why exit is unavailable, in the same words the daemon would refuse it with. A
// re-attached session (T36) is alive and readable but no longer this platform's
// child, so its pid is not the platform's to signal — while wind down still
// reaches it, because that travels the control plane and not the process table.
const exitBlocked = computed(() => {
  if (!props.run.detached) return null
  return (
    'This session survived a daemon restart, so the platform re-adopted its log '
    + 'but not its process — its pid is no longer reserved here and will not be '
    + 'signalled. Wind it down instead, or end it by hand on the host.'
  )
})

const canWindDown = computed(() => !winding.value && !ending.value)
const canExit = computed(() => canWindDown.value && !exitBlocked.value)

// The one thing in this component that does not read like the rest of the app:
// every other view posts through api.js. These two verbs are inline because this
// slice's file scope was the component, and the whole of the move is lifting this
// function next to sendSessionInput — nothing below it changes.
//
// Keyed on the live session rather than `terminalSession`, which is the id for
// *history* and can name a session that has already exited. These verbs act on a
// running container; there is nothing to wind down in a log.
async function postVerb(verb, body) {
  const response = await fetch(
    `api/sessions/${encodeURIComponent(session.value.id)}/${verb}`,
    {
      credentials: 'same-origin',
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    },
  )
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    // The daemon's refusals name both the reason and the way through (wind down
    // instead of exit, most of the time), so they are shown as written.
    throw new Error(payload?.detail || `HTTP ${response.status}`)
  }
  return payload
}

async function windDown() {
  if (!canWindDown.value) return
  winding.value = true
  lifecycleError.value = null
  try {
    windDownSent.value = await postVerb('wind-down', { note: windDownNote.value })
    windDownNote.value = ''
    emit('changed')
  } catch (exc) {
    lifecycleError.value = exc.message
  } finally {
    winding.value = false
  }
}

async function exitNow() {
  confirmingExit.value = false
  if (!canExit.value) return
  ending.value = true
  lifecycleError.value = null
  try {
    await postVerb('exit')
    // Deliberately not emit('close'): the run is still tracked and its terminal
    // log outlives the container, so the page it left behind is the record of
    // what it did.
    emit('changed')
  } catch (exc) {
    lifecycleError.value = exc.message
  } finally {
    ending.value = false
  }
}

// --- continuing this run (T25, T41) ------------------------------------------
//
// The verb the fleet view was missing. A row can say "stopped for your review"
// with nothing alive behind it — the session that got there has exited — so there
// was nothing to type into and no way to move it forward from here.
//
// Resuming *starts a session*, exactly as answering does, and on a host with a
// concurrency cap that is a consequence worth stating before the tap. It changes
// no run state itself: the session claims the run and prints its own resume brief
// in the container, so this row keeps saying what it says now for a moment longer.
//
// Two of the daemon's refusals are requests for one more field rather than
// failures, and they arrive with a machine-recognisable code precisely so this
// does not have to read English to tell them apart (lmer_platform.resume). The
// message is still shown as the daemon wrote it — every refusal there names both
// the reason and the way through.
const RESUME_NEEDS_REPO_URL = 'repo_url_required'
const RESUME_NEEDS_DIRECTION = 'direction_required'

// Long enough to want a name rather than a 200-character attribute, and each says
// what the field *does* instead of calling it optional — for the taskdef the
// difference between blank and filled in is which run the session lands on.
const RESUME_TASKDEF_HINT = 'blank continues this run with the taskdef it '
  + 'recorded. Naming another starts a sibling run against the same target '
  + '(develop → review) and leaves this run exactly as it is'
const RESUME_DIRECTION_HINT = 'the next session\'s seed: it records this as the '
  + 'goal and works on it. Required to reopen a run that is already complete'
const RESUME_REPO_URL_HINT = 'where this run\'s code is cloned from. It is '
  + 'written into the tracked index as this run\'s repository and every later '
  + 'action believes it, which is why the platform asks instead of guessing'

const resumeTaskdef = ref('')
const resumeDirection = ref('')
const resumeRepoUrl = ref('')
const resuming = ref(false)
const resumeError = ref(null)
// The code off the last refusal, when there was one — what points at the field
// being asked for.
const resumeNeeds = ref(null)
// Revealed by the refusal that asks for it, and never hidden again: a supplied URL
// can itself be refused (one naming another project), and taking the field away
// would take the operator's value with it.
const repoUrlAsked = ref(false)
const resumeStarted = ref(null)

// Nothing to continue while a session is up. Liveness outranks committed run
// state (spec D24), so the row can read "yielded" with a container already working
// it, and the daemon refuses a second one — this is also the half of the page that
// offers wind-down instead, and the two are never both right.
const canResume = computed(() => !props.run.live)

// Started once is started: the fleet poll turns this into a live run within
// seconds and this section goes with it, and a second tap in the gap would only
// earn the daemon's "already has a live session" refusal.
const resumeHandedOff = computed(() => !!resumeStarted.value)
const canSubmitResume = computed(() => !resuming.value && !resumeHandedOff.value)

// Both are refusals; only one of them is a problem. Being asked for the repository
// URL is the designed path — the platform will not invent that field — and painting
// it red would teach an operator that the verb is broken, which is how the ask gets
// read as a dead end. Anything else (a live session, an open question, the cap) is
// a failure and looks like one.
const resumeAsksForAField = computed(() => (
  resumeNeeds.value === RESUME_NEEDS_REPO_URL
  || resumeNeeds.value === RESUME_NEEDS_DIRECTION
))

// Naming a taskdef that is not this run's does not continue this run: the
// container derives its run directory from taskdef and target, so the session
// lands on a *sibling* run against the same target. That is the
// develop → review → followup workflow and it is a legitimate thing to want — it
// is just not what "continue this run" would be describing.
const resumeStartsSibling = computed(() => {
  const named = resumeTaskdef.value.trim()
  return !!named && named !== (props.run.taskdef || '')
})
const resumeLabel = computed(() => (
  resumeStartsSibling.value
    ? `start ${resumeTaskdef.value.trim()} on this target`
    : 'continue this run'
))

async function resume() {
  if (!canResume.value || !canSubmitResume.value) return
  resuming.value = true
  resumeError.value = null
  try {
    resumeStarted.value = await resumeRun(props.run, {
      taskdef: resumeTaskdef.value,
      repoUrl: resumeRepoUrl.value,
      direction: resumeDirection.value,
    })
    resumeNeeds.value = null
    emit('changed')
  } catch (exc) {
    // Nothing typed is cleared on this path: the platform records nothing before
    // the spawn (spec D3), so a refused resume leaves the direction existing
    // nowhere but this box.
    resumeError.value = exc.message
    resumeNeeds.value = exc.code || null
    if (exc.code === RESUME_NEEDS_REPO_URL) repoUrlAsked.value = true
  } finally {
    resuming.value = false
  }
}

async function forget() {
  busy.value = true
  error.value = null
  try {
    await forgetRun(props.run)
    emit('changed')
    emit('close')
  } catch (exc) {
    error.value = exc.message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div>
    <!-- The run's identity, and the reason it is asking for you. Both sit above
         the tabs on purpose: which panel is showing is the operator's remembered
         choice now, so this view no longer decides what they land on — and
         anything that answers "why am I looking at this run" must not be behind a
         tab nobody chose today. -->
    <div class="d-flex flex-wrap align-center ga-2">
      <v-chip :color="toneColor(meta.tone)" variant="tonal">
        {{ meta.label }}
      </v-chip>
      <!-- The platform's own session, on the heading line beside the state and in
           the same words the fleet listings use (the operator asked: "i think the
           orchestrator run needs to be clearly marked that it is that, both in the
           list and the detail of the run"). It belongs here rather than on the
           identity line below because it is not a fact *about* a run — it says
           which run this is, and the verbs in the exit tab read very differently
           once you know that the session you are about to end is the one
           orchestrating everything else. The accent and the icon are the uber lmer
           drawer's: the same thing named in two places, and the word is what keeps
           the accent from reading as "you are here". -->
      <v-chip
        v-if="run.orchestrator"
        :prepend-icon="mdiRobot"
        color="primary"
        size="small"
        title="the platform's own orchestrating session, not one of the runs"
        variant="tonal"
      >uber lmer</v-chip>
      <!-- The heading line is the run's *name*, so this is where the title goes
           (the operator asked: "if meta.title is set it should be used in the
           details header and the listings") — not an extra item on the identity
           line below, which is deliberately one row and would grow with it.
           Both, and in this order, because this is the one view with room for
           both: the title says what the run is about, the label says what it is
           called, and the run path in the overview is otherwise the only place the
           latter survives. The label is dimmed and second — a name you already
           know beside the sentence you came here to read.
           Interpolated rather than rendered, like RunMeta.vue's copy of it: the
           daemon collapses the title to one line and bounds it at 120 characters,
           and markdown in a heading would be this view pretending to be a
           document. `scroll-x` keeps an unbreakable one inside its own box, and
           the state chip is *before* it on the flex line, so a long title wraps
           under the chip rather than pushing it off a phone. -->
      <span
        class="text-body-large font-weight-medium scroll-x"
      >{{ run.title || run.label }}</span>
      <span
        v-if="run.title"
        class="text-body-small text-medium-emphasis scroll-x"
      >{{ run.label }}</span>
    </div>

    <!-- What the run IS — the taskdef, the repo and the thing it is working on —
         in the header rather than in a panel (the operator asked: "i think i want
         target, repo name and taskdef to be in the header ... realistically i am
         not interested in overview or meta tabs most of the time (as long as i
         know the taskdef, target, and repo name)"). Three fields is most of what
         made the overview worth opening, and once they are always visible it
         mostly is not.
         Deliberately one wrapping line and not a card: everything below it here
         is what a run needs from a human, and a block of key/value rows in the
         header would push the attention alert and the answer box off a phone
         screen — which is the exact thing they are above the tabs to avoid. The
         overview still carries the same facts in full (the run path, the whole
         target URL, the goal); this line is the glance, not the record.
         Same fields, same order and — for the two bare values — the same icons as
         the fleet row, because it is the same question being answered one view
         later, and the icon is what says which field "develop" or "group/project"
         is. The target keeps a plain link icon instead of the row's per-kind
         shapes: that map earns its place in a list scanned at arm's length, and a
         second copy of it here would be one more thing to keep in step for a
         header that shows exactly one target and names it in words. -->
    <div class="d-flex flex-wrap align-center ga-3 mt-2 text-body-small">
      <span v-if="run.taskdef" class="d-inline-flex align-center">
        <v-icon
          :icon="mdiClipboardTextOutline"
          size="small"
          class="me-1"
          title="taskdef"
        />{{ run.taskdef }}
      </span>
      <span v-if="run.project" class="d-inline-flex align-center">
        <v-icon
          :icon="mdiSourceRepository"
          size="small"
          class="me-1"
          :title="`project on ${run.host}`"
        />{{ run.project }}
      </span>
      <!-- The number, not the URL, for the reason the row gives: an MR and its
           number is how this run gets talked about, and it fits a phone's width
           where the target does not. `scroll-x` is still on it because the fallback
           for a target that names no numbered resource is a shortened URL — one
           unbreakable string, which takes the whole page sideways with it unless it
           scrolls inside its own box. The full target stays the tooltip.
           Two branches for one field, because most of what a taskdef can be handed
           as a target is not a URL at all: a branch, a sentence, the orchestrator's
           own `fleet`. Anchored, those were relative links resolved against this
           page, so every one of them 404'd — the anchor is therefore conditional on
           there being somewhere to go, and the link icon goes with it, since a shape
           saying "this leads somewhere" over plain text is the same lie one step
           quieter. The words are `targetLabel` either way: the two branches must not
           be able to name the same target differently. -->
      <a
        v-if="targetHref"
        class="scroll-x"
        :href="targetHref"
        :title="run.target"
        target="_blank"
        rel="noopener"
      ><v-icon
        :icon="mdiLinkVariant"
        size="small"
        class="me-1"
      />{{ targetLabel }}</a>
      <span
        v-else-if="run.target"
        class="scroll-x"
        :title="run.target"
      >{{ targetLabel }}</span>
      <!-- What the run published, on the line every tab can see. For a run that
           built something to look at, opening it is the reason the operator came —
           and inside the overview's session card it was a tab away from the
           terminal they were watching it from.
           No gate on the session is needed beyond the array itself: the daemon
           reads these off the live session's registry entry (inventory's
           `RunView.ports`), so a run with nothing running has no ports and the
           line ends at the target. -->
      <template v-if="run.ports?.length">
        <v-chip
          v-for="port in run.ports"
          :key="port.host"
          :href="portUrl(port)"
          target="_blank"
          rel="noopener"
          :append-icon="mdiOpenInNew"
          color="primary"
          size="small"
          variant="tonal"
        >:{{ port.host }}</v-chip>
      </template>
    </div>

    <!-- The note is prose an agent wrote — it quotes a path, emphasises a word,
         puts a command in backticks — so it is rendered rather than shown as
         punctuation. In the compact mode: this is one line of an alert, and a
         heading or a fenced block in it would turn the reason a run needs a human
         into three lines of document (T46).
         And it is dropped entirely when the box that can answer it is right below
         (`noteRepeatedBelow`): for a question the note is the question, so the
         alert was the first of two copies of the same paragraph, with the copy that
         has a reply box under it. The label alone stays, because "why am I looking
         at this run" is what the alert is for. -->
    <v-alert
      v-if="run.attention"
      :type="run.attention.reason === 'crashed' ? 'error' : 'warning'"
      density="compact"
      class="mt-3"
    >
      <strong>{{ attentionLabel(run.attention.reason) }}</strong>
      <template v-if="run.attention.note && !noteRepeatedBelow"> — <Markdown
        :text="run.attention.note"
        inline
      /></template>
    </v-alert>

    <!-- Answering stays above the tabs for the same reason, and it is the stronger
         case of it: the operator opened this run because a fleet row said it was
         waiting on them, and a remembered tab could otherwise open on the terminal
         with the box that clears the question one tap out of sight. -->
    <template v-if="questionPending">
      <div class="section-title">answer</div>
      <AnswerBox :run="run" @answered="emit('changed')" />
    </template>

    <!-- The session's own channel, docked here for the same reason and one of its
         own (T40). The same reason: a session sitting in a poll loop waiting for a
         reply is a human being blocked, and a remembered tab could otherwise open
         this run on the terminal with the box that unblocks it out of sight. Its
         own: this dock is *clearable*, and what makes that safe is that clearing
         only dismisses from here — the whole record, cleared entries included, is
         in the operator-chat pane below, which is why the two are separate views.
         It renders nothing at all when the channel is empty, which is almost every
         run, so the tabs sit where they always did until a session posts. -->
    <AskChannel
      v-if="terminalSession"
      :session-id="terminalSession"
      :live="!!run.live"
      :initial="run.questions || []"
      :now="now"
      @answered="emit('changed')"
    />

    <!-- Tabs rather than one long page (T49): what this run is, what it is about,
         what its session is saying, and how it ends. Stacked, this view was five
         cards and two scrolling panes deep — the terminal, the events and the
         verbs all sat under a transcript that runs to hundreds of turns.
         A panel is also a bounded box, which is what the emulator sizes itself
         from, and it renders nothing until it is first selected, which is what
         keeps xterm's chunk off the path of opening a run. -->
    <!-- Icons beside the words, in both tab rows (the operator asked: "tabs in
         detail view should get some icons"). Additive: four one-word labels are
         four shapes apart at a glance, and a bar of glyphs alone would be a puzzle
         on the one view that has two rows of them.
         Shapes the app already speaks wherever the concept already exists — the
         harness outline the fleet card puts on the model driving a session, the
         question mark the ask channel draws on every entry of itself — so the
         same idea is not two drawings in two views. The icon is inline in the tab
         rather than passed as `prepend-icon` for the reason the header's fields
         are: the label sits tight against it, and nothing here needs the padding
         a button's slot adds. -->
    <v-tabs v-model="tab" grow color="primary" class="mt-4">
      <v-tab value="overview"><v-icon
        :icon="mdiInformationOutline"
        size="small"
        class="me-1"
      />overview</v-tab>
      <v-tab value="meta"><v-icon
        :icon="mdiTextBoxOutline"
        size="small"
        class="me-1"
      />meta</v-tab>
      <v-tab value="lmer"><v-icon
        :icon="mdiRobotOutline"
        size="small"
        class="me-1"
      />lmer</v-tab>
      <v-tab value="exit"><v-icon
        :icon="mdiExitRun"
        size="small"
        class="me-1"
      />exit</v-tab>
    </v-tabs>

    <v-tabs-window v-model="tab">
      <v-tabs-window-item value="overview" class="pt-3">
        <div class="section-title">task</div>
        <v-card class="mb-3">
          <v-card-text>
            <dl class="kv">
              <!-- The directory when the daemon found one, and the run's key when
                   it did not — never a path this view assembles. See `runKey`: a
                   composed `runs/<slug>` names nothing for a run with a name, and
                   the row that has no address is precisely the row whose directory
                   nobody has located. -->
              <dt>run</dt>
              <dd><code class="scroll-x">{{ run.rel_path || runKey }}</code></dd>
              <dt v-if="run.taskdef">taskdef</dt>
              <dd v-if="run.taskdef">{{ run.taskdef }}</dd>
              <dt v-if="run.target">target</dt>
              <dd v-if="run.target">
                <!-- Gated exactly as the header is, and by the same helper: this
                     row shows the whole target rather than its number, which makes
                     a dead relative link here even easier to read as a real one. -->
                <a
                  v-if="targetHref"
                  :href="targetHref"
                  target="_blank"
                  rel="noopener"
                >{{ run.target }}</a>
                <template v-else>{{ run.target }}</template>
              </dd>
              <dt v-if="run.goal">goal</dt>
              <!-- The one field in this grid that is a sentence somebody wrote,
                   so it is rendered — inline, because a grid cell is a row and a
                   list in one would push the rest of the run's facts down the
                   page (T46). -->
              <dd v-if="run.goal"><Markdown :text="run.goal" inline /></dd>
              <dt v-if="run.phase">phase</dt>
              <dd v-if="run.phase">{{ run.phase }}</dd>
              <!-- Absent renders nothing, here as everywhere: a run that has
                   recorded no status yet — every run in its first seconds — used
                   to get a row with an em dash in it, which is a placeholder
                   standing in for a fact nobody has. The status and the stop
                   reason are one value (see the binding), so the row cannot show
                   a stray "(question)" with nothing in front of it either. -->
              <dt v-if="status">status</dt>
              <dd v-if="status">{{ status }}</dd>
              <dt v-if="ledger">plan</dt>
              <dd v-if="ledger">{{ ledger }}</dd>
              <dt>updated</dt>
              <dd :title="run.updated || ''">{{ ago(run.updated, now) }}</dd>
            </dl>
          </v-card-text>
        </v-card>

        <template v-if="session">
          <div class="section-title">live session</div>
          <v-card class="mb-3">
            <v-card-text>
              <dl class="kv">
                <dt>id</dt>
                <dd><code class="scroll-x">{{ session.id }}</code></dd>
                <dt>pid</dt>
                <dd>{{ session.pid }}</dd>
                <dt>started</dt>
                <dd :title="session.started_at || ''">{{ ago(session.started_at, now) }}</dd>
                <dt v-if="session.log_path">log</dt>
                <dd v-if="session.log_path"><code class="scroll-x">{{ session.log_path }}</code></dd>
              </dl>

              <!-- How long the harness has been quiet (T95), in the fleet row's
                   idiom: dimmed, subordinate, no chip and no colour. The state
                   above is derived with liveness first (spec D24), so it reads
                   "running" from the moment a session starts until it exits — a
                   session that finished its work and is sitting at its prompt
                   looks exactly like one that is working, and this is the line
                   that tells them apart. The tooltip is the moment the container
                   dated the output to, because a rendered age would depend on
                   this device's clock. Nothing at all when there is no reading. -->
              <div
                v-if="idle"
                class="text-body-small text-medium-emphasis mt-2"
                :title="session.activity?.last_output_at || ''"
              >{{ idle }}</div>
            </v-card-text>
          </v-card>
        </template>

        <!-- Continuing the run is here rather than in the exit tab, even though it
             is the other half of the same decision — what happens to this run next
             — and the two are never offered at once. Two reasons, and both are
             about the operator: the tab is labelled for ending a session, and
             finding the verb that *starts* one behind it is the kind of mislabel
             that makes someone hesitate over the button they came to press; and
             this is the tab holding the facts that decide it — a row reading
             "stopped for your review" is answered by the status, the goal and the
             plan sitting directly above. -->
        <template v-if="canResume">
          <div class="section-title">continuing this run</div>
          <v-card class="mb-3">
            <v-card-text>
              <p class="text-medium-emphasis mb-3">
                Continuing starts a fresh session for this run. It claims the run,
                prints its own resume brief and carries on from the state above — so
                this row keeps saying what it says now until that session gets going.
                Nothing here writes the run's state; the session does that, in the
                container.
              </p>

              <!-- A question stop has its own verb and the box above the tabs is
                   it: a plain continue would start a session that reads the
                   question back with no answer and leaves the stop exactly where it
                   is, which the daemon refuses. Naming a taskdef is still allowed
                   here — that starts different work against the same target, not
                   this run again. -->
              <v-alert v-if="questionPending" type="info" density="compact" class="mb-3">
                This run stopped on a question, so continuing it as it stands will be
                refused — answer it above instead, which is what clears the question.
                Naming a taskdef below starts different work against the same target.
              </v-alert>

              <!-- A refusal that names a field is not a failure: the platform is
                   asking for something only the operator has. Shown in the daemon's
                   own words, which say both what is missing and what supplying it
                   will do. -->
              <v-alert
                v-if="resumeError"
                :type="resumeAsksForAField ? 'warning' : 'error'"
                density="compact"
                class="mb-3"
              >
                {{ resumeError }}
              </v-alert>

              <v-alert v-if="resumeStarted" type="success" density="compact" class="mb-3">
                Session {{ resumeStarted.session?.session_id }} started —
                {{ resumeStarted.note }}
              </v-alert>

              <!-- A resume that started a session and still lost something. The
                   daemon carries the same key here as on a spawn, so it is rendered
                   the same way rather than being a case only one view knows about. -->
              <v-alert
                v-if="resumeStarted?.warning"
                type="warning"
                density="compact"
                class="mb-3"
              >
                {{ resumeStarted.warning }}
              </v-alert>

              <!-- Every field is optional and the button works with none of them
                   filled in: continuing is meant to be one tap. -->
              <v-text-field
                v-model="resumeTaskdef"
                :disabled="!canSubmitResume"
                label="taskdef"
                :placeholder="run.taskdef || 'develop'"
                :hint="RESUME_TASKDEF_HINT"
                persistent-hint
                class="mb-3"
              />
              <v-textarea
                v-model="resumeDirection"
                :disabled="!canSubmitResume"
                :error="resumeNeeds === RESUME_NEEDS_DIRECTION"
                label="direction"
                :hint="RESUME_DIRECTION_HINT"
                persistent-hint
                rows="2"
                auto-grow
                max-rows="8"
                autocapitalize="sentences"
                class="mb-3"
              />
              <!-- Only once the platform has asked for it. An adopted run knows its
                   host, project and slug but not where its code is cloned from, and
                   a guess would not be used once and discarded — it becomes the
                   run's repository of record. -->
              <v-text-field
                v-if="repoUrlAsked"
                v-model="resumeRepoUrl"
                :disabled="!canSubmitResume"
                :error="resumeNeeds === RESUME_NEEDS_REPO_URL"
                label="repository URL"
                :prepend-inner-icon="mdiLinkVariant"
                :hint="RESUME_REPO_URL_HINT"
                persistent-hint
                placeholder="https://git.example.com/group/project.git"
                class="mb-3"
              />

              <div class="d-flex flex-wrap align-center ga-3">
                <v-btn
                  :prepend-icon="resumeStartsSibling ? mdiSourceBranchPlus : mdiPlayOutline"
                  :loading="resuming"
                  :disabled="!canSubmitResume"
                  color="primary"
                  size="large"
                  @click="resume"
                >{{ resumeLabel }}</v-btn>
                <span class="text-body-small text-medium-emphasis">
                  <template v-if="resumeStartsSibling">
                    this starts a different run against the same target — this one is
                    left as it is
                  </template>
                  <template v-else>this starts a session for the run</template>
                </span>
              </div>
            </v-card-text>
          </v-card>
        </template>

        <template v-if="run.events && run.events.length">
          <div class="section-title">recent events</div>
          <v-card class="mb-3">
            <v-card-text>
              <div
                v-for="(event, index) in run.events"
                :key="index"
                class="d-flex flex-wrap ga-3 text-body-small text-medium-emphasis"
              >
                <span :title="event.ts || ''">{{ ago(event.ts, now) }}</span>
                <span>{{ event.type }}</span>
                <!-- The agent's own words about what happened, rendered inline:
                     an event line is a line, and a fenced block in one would make
                     the log unscannable — which is the only thing it is for. -->
                <Markdown v-if="event.note" :text="event.note" inline />
              </div>
            </v-card-text>
          </v-card>
        </template>

        <!-- The run's own record, and last in this panel on purpose. It is the
             reference rather than the news: the facts at the top answer "what is
             this doing", the verb in the middle acts on it, and this is where you
             go to read what the run actually wrote. It is in the overview and not
             a fifth tab because four rows of tabs on a phone is already the
             ceiling — and not above the tabs either, where the attention alert and
             the answer box have to stay reachable without scrolling. The bottom of
             the whole details view, below the tabs, is deliberately left free for
             the related-runs element that wants it (T53).
             Every entry is a link into the forge, which is what was asked for:
             clicking a file opens it in the work repo. -->
        <div class="section-title">run files</div>
        <v-card class="mb-3">
          <v-card-text>
            <p v-if="!runFilesLoaded" class="text-medium-emphasis">reading…</p>

            <v-alert v-else-if="runFilesError" type="error" density="compact">
              {{ runFilesError }}
            </v-alert>

            <template v-else>
              <!-- Two different empty cases, and they are not the same news: one
                   says nothing has been pushed yet, the other that the directory
                   is there and bare. -->
              <p v-if="!runFiles.length" class="text-medium-emphasis">
                <template v-if="runFilesMeta && !runFilesMeta.present">
                  Nothing for this run is in the platform's copy of the work repo
                  yet. A session pushes its run directory when it commits, and this
                  view catches up on the next pull.
                </template>
                <template v-else>
                  The run's directory is there, but holds no files.
                </template>
              </p>

              <div v-else class="d-flex flex-wrap ga-2">
                <template v-for="file in runFiles" :key="file.name">
                  <v-chip
                    v-if="file.url"
                    :href="file.url"
                    target="_blank"
                    rel="noopener"
                    :prepend-icon="mdiFileDocumentOutline"
                    :append-icon="mdiOpenInNew"
                    color="primary"
                    variant="tonal"
                  >{{ file.name }}</v-chip>
                  <!-- A name and no anchor: the daemon returned no url for this
                       file, which since work_repo_forge exists means links are off
                       for this work repo (or there is none). The name is still the
                       record; an anchor pointing nowhere would not be. -->
                  <v-chip
                    v-else
                    :prepend-icon="mdiFileDocumentOutline"
                    variant="tonal"
                    :title="unlinkedFileHint"
                  >{{ file.name }}</v-chip>
                </template>
              </div>

              <p
                v-if="runFilesMeta?.truncated"
                class="text-body-small text-medium-emphasis mt-3"
              >
                Not every file is listed — this run has more than the daemon will
                name at once. Open the directory for the rest.
              </p>

              <div v-if="runFilesMeta?.run_dir_url" class="mt-3">
                <v-btn
                  :href="runFilesMeta.run_dir_url"
                  target="_blank"
                  rel="noopener"
                  :prepend-icon="mdiFolderOpenOutline"
                  :append-icon="mdiOpenInNew"
                  variant="tonal"
                  size="small"
                >open the run directory</v-btn>
              </div>
            </template>
          </v-card-text>
        </v-card>
      </v-tabs-window-item>

      <!-- What this run is about, which is the one thing on this page nobody can
           derive: a slug, a taskdef and a target say what a run *is*, not why you
           started it. Its own tab because the text is arbitrarily long — a
           description is paragraphs, and putting it at the top of the overview
           would push the facts that answer "what is it doing" off a phone screen.
           It is also the only tab whose content this orchestrator owns rather than
           reads: everything else here comes out of the work repo or a session, and
           this is written locally and stays local. -->
      <v-tabs-window-item value="meta" class="pt-3">
        <RunMeta :run="run" :now="now" />
      </v-tabs-window-item>

      <!-- Three views of one session, and nobody needs two of them at once. The
           readable one answers "what is going on", the faithful one answers "what
           exactly happened", and the channel is the one conversation that is not
           the session's own output — questions it posted for a human, and the
           replies. All three read from state that outlives the container, so all
           three are offered for a session that has gone. -->
      <v-tabs-window-item value="lmer" class="pt-3">
        <template v-if="terminalSession">
          <v-tabs v-model="pane" grow color="primary">
            <v-tab value="conversation"><v-icon
              :icon="mdiForumOutline"
              size="small"
              class="me-1"
            />conversation</v-tab>
            <v-tab value="terminal"><v-icon
              :icon="mdiConsole"
              size="small"
              class="me-1"
            />terminal</v-tab>
            <!-- The channel's own shape, the one AskHistory draws on the entries
                 this pane renders and the drawer draws on a run that is waiting to
                 be answered. Nothing like the conversation's: replying to the
                 session and reading what it said are different acts. -->
            <v-tab value="chat"><v-icon
              :icon="mdiCommentQuestionOutline"
              size="small"
              class="me-1"
            />operator chat</v-tab>
          </v-tabs>

          <v-tabs-window v-model="pane">
            <v-tabs-window-item value="conversation" class="pt-3">
              <Chat
                :session-id="terminalSession"
                :question-pending="questionPending"
                :ask-pending="askPending"
                :now="now"
              />
            </v-tabs-window-item>

            <v-tabs-window-item value="terminal" class="pt-3">
              <Terminal :session-id="terminalSession" :harness="run.harness" />
            </v-tabs-window-item>

            <v-tabs-window-item value="chat" class="pt-3">
              <!-- The record, not the dock (T40). Everything the session posted
                   on its channel is here — including whatever was cleared from
                   above, which is what makes clearing up there a view operation
                   rather than a deletion. Read-only on purpose: replying belongs
                   in the one place the question that needs an answer is, and a
                   second box down here would be a second way to send the same
                   file, out of sight of the alert that says whether the session
                   is even alive. -->
              <p class="text-medium-emphasis mb-3">
                Everything this run's session posted on its channel and everything
                you replied, oldest first — notes, questions it was blocked on, the
                ones it stopped waiting for, and any that were never answered.
                Clearing the dock above this page does not remove anything from
                here. A session that never used the channel has nothing here.
              </p>
              <AskHistory
                :session-id="terminalSession"
                :live="!!run.live"
                :now="now"
              />
            </v-tabs-window-item>
          </v-tabs-window>
        </template>

        <p v-else class="text-medium-emphasis">
          No session has run for this run yet, so there is no conversation, no
          terminal log and no channel to read. Continuing it from the overview tab is
          what starts one.
        </p>
      </v-tabs-window-item>

      <!-- How this run ends: the session's two verbs, and giving up tracking it.
           Deliberately its own tab rather than a card under the transcript — on a
           phone that put the safe verb below hundreds of turns of conversation, and
           the reason wind down is the large one is that it has to be the easy one to
           reach. -->
      <v-tabs-window-item value="exit" class="pt-3">
        <!-- Two verbs, and they are deliberately NOT two buttons side by side. Wind
             down is a request the agent gets to finish — commit, push, report — so
             it is the large primary action with a place to add to it. Exit signals
             immediately and the agent gets nothing, so it is small, subordinate, and
             behind a confirmation. That asymmetry is load-bearing rather than
             decorative: a session that bound a port and set something up for you to
             look at must not be shut down out from under you, and the blunt verb is
             one mis-aimed tap away on a phone. -->
        <template v-if="session && run.live">
          <div class="section-title">ending this session</div>
          <v-card class="mb-3">
            <v-card-text>
              <v-alert v-if="lifecycleError" type="error" density="compact" class="mb-3">
                {{ lifecycleError }}
              </v-alert>

              <!-- Asked, and still going. Not an error and not a success: the agent
                   is wrapping up, which is what was asked for. Past the recorded
                   backstop it turns into a warning that says so and stops there —
                   nothing escalates a wind-down into a kill. -->
              <v-alert
                v-if="windDownAsked"
                :type="windDownOverdue ? 'warning' : 'info'"
                :icon="windDownOverdue ? mdiClockAlertOutline : mdiClockOutline"
                density="compact"
                class="mb-3"
              >
                <template v-if="windDownOverdue">
                  Asked to wind down {{ ago(windDownAt, now) }} and still running. It
                  may be finishing something long, or it may not be winding down at
                  all — nothing here will kill it for you. Read the conversation in
                  the lmer tab, then wait or exit it.
                </template>
                <template v-else>
                  Asked to wind down {{ ago(windDownAt, now) }}. It ends itself when
                  it has finished wrapping up, so this row stays live for a while
                  yet.
                </template>
              </v-alert>

              <p class="text-medium-emphasis mb-3">
                Winding down asks the agent to stop picking up work and land what it
                already has — commit and push, record the run's state, post its
                summary — and then end the session itself. It decides when it is
                done, and nothing here kills it in the meantime, so a session still
                serving you something stays up until it has finished.
              </p>

              <v-textarea
                v-model="windDownNote"
                :disabled="!canWindDown"
                label="anything to add (optional)"
                hint="goes on the end of the wind-down request, e.g. skip the MR, just push"
                persistent-hint
                rows="2"
                auto-grow
                max-rows="6"
                autocapitalize="sentences"
                class="mb-3"
              />
              <v-btn
                :prepend-icon="mdiWeatherSunset"
                :loading="winding"
                :disabled="!canWindDown"
                color="primary"
                size="large"
                @click="windDown"
              >wind down</v-btn>

              <v-divider class="my-4" />

              <!-- The blunt verb, kept subordinate on purpose: small, secondary,
                   confirmed, and under a line that says what it costs. -->
              <div class="d-flex ga-2 align-start text-body-small text-medium-emphasis">
                <v-icon :icon="mdiPowerPlugOff" size="small" class="mt-1" />
                <div>
                  Or end it now, if it is wedged, wrong, or you need the slot. Exit
                  signals the session immediately: nothing is committed, nothing is
                  pushed, no summary is written, and anything it was serving goes with
                  it. The terminal log stays readable.
                  <template v-if="exitBlocked"><br />{{ exitBlocked }}</template>
                </div>
              </div>
              <v-btn
                :prepend-icon="mdiPowerPlugOff"
                :disabled="!canExit"
                :loading="ending"
                color="error"
                variant="tonal"
                size="small"
                class="mt-2"
                @click="confirmingExit = true"
              >exit now</v-btn>

              <v-dialog v-model="confirmingExit" max-width="480">
                <v-card>
                  <v-card-text class="text-body-medium">
                    End this session now? The agent gets no chance to commit, push or
                    report, so anything it has not already landed is lost — including
                    whatever it was serving for you to look at. Wind it down instead
                    if you want the work kept.
                  </v-card-text>
                  <v-card-actions>
                    <v-spacer />
                    <v-btn variant="tonal" @click="confirmingExit = false">cancel</v-btn>
                    <v-btn color="error" variant="tonal" @click="exitNow">exit now</v-btn>
                  </v-card-actions>
                </v-card>
              </v-dialog>
            </v-card-text>
          </v-card>
        </template>

        <!-- An empty tab reads as a broken one, and this is the common case: most
             runs in this list have no container behind them. -->
        <p v-else class="text-medium-emphasis">
          Nothing is running for this run, so there is no session to wind down or
          exit. The overview tab is where continuing it lives.
        </p>

        <div class="section-title">tracking</div>
        <v-card class="mb-3">
          <v-card-text>
            <v-alert v-if="error" type="error" density="compact" class="mb-3">
              {{ error }}
            </v-alert>
            <p class="text-medium-emphasis mb-3">
              Forgetting a run removes it from this orchestrator's view. Its work-repo
              state and any running session are left untouched.
            </p>
            <v-btn
              color="error"
              variant="tonal"
              :prepend-icon="mdiEyeOffOutline"
              :loading="busy"
              @click="forget"
            >forget this run</v-btn>
          </v-card-text>
        </v-card>
      </v-tabs-window-item>
    </v-tabs-window>

    <!-- Runs that belong together, below the tabs and inside none of them (T53).
         The slot T66 left free, and the reason it is a slot rather than a fifth
         tab: this is a switcher, and the point of one is switching while looking
         at something else. Behind a tab it would be a place you leave the terminal
         to visit in order to go somewhere else.
         Last on the page on purpose — everything above it is this run, and this is
         the way out of it. -->
    <RelatedRuns :run="run" @open="emit('open', $event)" />
  </div>
</template>
