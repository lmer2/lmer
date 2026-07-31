<script setup>
// One run's conversation, read from the harness transcript (spec D6 / §10.4).
//
// The terminal beside this is the faithful view — every byte the session drew.
// This is the readable one, and the two are sourced differently on purpose: a PTY
// stream is a redrawing screen, so "who said what" can only be reconstructed from
// the structured transcript the harness already writes.
//
// The consequence is that the two halves of a conversation have different
// latencies, and that shapes this whole component:
//
//   reading  ← GET  api/sessions/{id}/messages   (append-only history, polled)
//   writing  → POST api/sessions/{id}/input     (the session's control plane)
//
// A message sent through /input does not appear in the transcript until the
// harness writes it, which is after the session's TUI has accepted the keystrokes
// and started a turn — seconds, sometimes longer. So a sent message is held
// locally as *pending* until the poll finds it, and a slow transcript is never
// reported as a failed send. Only the POST failing is a failure.
//
// The chat spans every session of the run, not just this one. Answering a question
// respawns the run in a new container, and the operator should not have to know
// that happened — the server concatenates the sessions it can join (see
// lmer_platform.transcripts for which those are), so this component just renders
// what it is given, in order.
//
// Why there is no composer for a run waiting on you
// -------------------------------------------------
// A question-blocked run has already exited: there is nothing to type into. The
// answer path is a different action — respawn the run with the answer, which the
// fresh session records at its own start — and it belongs to AnswerBox, above this
// view. An input box here would be a control that looks like it works and silently
// goes nowhere, which is the exact failure the /input route is loud about
// avoiding. `composerMode` is the seam that keeps the two apart.
//
// Answers you gave through the ask channel are in here too
// --------------------------------------------------------
// An lmer-ask answer never goes through /input: it is a file the platform writes
// into a directory the session polls, so the harness records nothing about it and
// the transcript genuinely has no trace of the operator's words. The server
// interleaves the channel's questions and answers into this timeline instead
// (lmer_platform.transcripts), and they arrive as ordinary messages — the answer
// with role 'user', so it falls into the verbatim branch below with everything
// else you sent, and `via: 'ask'` only so the header can say where it came from.
// Nothing here has to know about the ask channel beyond that one label.
//
// Why the agent's half is rendered and yours is not
// -------------------------------------------------
// What the agent writes is markdown in practice — fenced commands, lists, tables
// — and shown verbatim it is a wall of asterisks and backticks. What *you* sent
// is shown exactly as sent: it is a message typed into a session, and a bubble
// that quietly ate a pair of asterisks would be misreporting what the run got.
// The split keeps the two sides of the conversation looking different too, which
// is what a thumb-scroll reads before any of the words.
//
// The rendering itself is not this component's: Markdown.vue owns it, for the
// operator channel as much as for here, and its header says why turning
// container output into markup is one implementation rather than three. What
// belongs to this file is the *split* above — which half goes through it.
//
// The three grounds
// -----------------
// operator, live testing: "assistant messages, user messages, assistant actions
// should all be color coded backgrounds". A scroll of this pane is three kinds of
// thing — the agent talking, you talking, and the agent doing — and read as one
// column of prose it takes a header per turn to tell which. So each kind is drawn
// on its own ground: `ground()` below decides which, and the colours are the
// theme's (main.js, both schemes, so the switcher repaints them).
//
// What that buys is one visual identity for "something I sent" however it
// travelled — typed into the composer, merged back from the ask channel, or still
// held as pending — which is what the ask-channel merge was for in the first
// place.
//
// The fourth kind of turn takes the action ground rather than a colour of its
// own: a watch the session armed, firing (`transcripts.MONITOR_ROLE`). It is the
// machinery talking, which is what that ground already means here, and a fourth
// tone would be one more thing to tell apart on a phone for a turn that is rare.
//
// Who a turn is titled as
// -----------------------
// operator, live testing: "in the agent chats the agent messages are titled as
// `assistant` - they should be `lmer` in run chats and `uber lmer` in the uber
// lmer chat". The transcript's role is a *code* spelling — 'assistant' is the
// module, the taskdef and every API field, and AssistantChat.vue's header states
// the contract that the word reaches an operator nowhere — so a role is never
// rendered. It is mapped, `agentLabel` carries the one name that differs between
// the two places this component is mounted (same reasoning as `composerLabel`),
// and a role this build has never seen falls back to a name rather than to
// itself: a raw role string on the screen is the bug being fixed, and the next
// harness release is free to invent one.

import {
  computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch,
} from 'vue'
import {
  mdiAlertCircleOutline,
  mdiCogOutline,
  mdiRefresh,
  mdiSend,
  mdiUnfoldMoreHorizontal,
} from '@mdi/js'
import { fetchSessionMessages, sendSessionInput } from '../api.js'
import { ago } from '../format.js'

// The renderer arrives as its own chunk, fetched the first time a view renders one
// — Markdown.vue's header has the numbers and the reason (the fleet view is the
// landing screen and shows no markdown at all). Same shape as the terminal's
// split, and nothing is drawn while it is on its way: a placeholder for *this*
// text would be a second render path for it.
//
// What it costs here is one round trip in which every turn has a header and no
// body, which the ref exists to notice — see the watcher at the end of this block.
const rendererLoaded = ref(false)
const Markdown = defineAsyncComponent(async () => {
  const renderer = await import('./Markdown.vue')
  rendererLoaded.value = true
  return renderer.default
})

const props = defineProps({
  // The id, not the session object: the fleet poll replaces that object every ten
  // seconds and a watcher on it would restart a working view for no reason. Same
  // reasoning as Terminal.vue.
  sessionId: { type: String, required: true },
  // Whether the run is stopped waiting for an answer. A run-level fact the fleet
  // view already knows, and the one thing that changes what the composer is for.
  questionPending: { type: Boolean, default: false },
  // Whether a *live* session is waiting on its ask channel (T23). Different from
  // questionPending in the way that matters here: there is still something to
  // type into, so the composer stays — but what it types lands wherever the
  // harness's focus happens to be, which is exactly what the ask channel exists
  // to route around. So the composer is kept and captioned, not removed.
  askPending: { type: Boolean, default: false },
  // What the composer says it is for. A prop rather than the sentence below it,
  // because this component is pointed at two kinds of session: a run's, where
  // "this session" is the run you are already looking at, and the supervisor's in
  // the right-hand drawer, where the whole feature is shaped around never
  // mistaking one for the other (AssistantChat.vue). The label is the last thing
  // read before typing, so it is the last place that can name the recipient.
  composerLabel: { type: String, default: 'say something to this session' },
  // What the agent's own turns are titled. Defaults to the worker's name, which
  // is what a run's chat wants and what every consumer but the drawer passes
  // nothing for; AssistantChat.vue passes the supervisor's. A prop for the same
  // reason `composerLabel` is one — the two conversations must not be tellable
  // apart only by which edge of the screen they are docked to.
  agentLabel: { type: String, default: 'lmer' },
  now: { type: Number, default: () => Date.now() },
})

// What a turn's role is titled as, for the roles that are not the agent's. See
// the header: the role itself is never shown.
const SPEAKERS = {
  user: 'you',
  monitor: 'the watch fired',
}

// Everything else: the harness's own 'system' prose today, a role a later release
// invents tomorrow. Named rather than passed through, because a raw role on the
// screen is what this map exists to prevent.
const OTHER_SPEAKER = 'the session'

// How often the transcript is re-read. Slower than the terminal's socket because
// this is polled history, not keystroke echo, and each poll costs the daemon a
// parse of the tail of a file.
const POLL_MS = 5000

// Messages on a cold open, as a tail: a long run's conversation is hundreds of
// turns and the interesting end is the last few.
const INITIAL_TAIL = 40

// What "earlier messages" steps back by each time.
const PAGE = 40

// How long a sent message waits to appear in the transcript before the view says
// so. Not a failure — the send already succeeded — just an honest answer to "did
// that go through?".
const PENDING_GRACE_MS = 45000

// How far off the end still counts as "reading the end". About one turn's worth:
// a thumb that has come to rest a few pixels short is still following, while a
// reader who has scrolled back deliberately is not.
const STICK_SLACK_PX = 64

const scroller = ref(null)
// Whether the reader is following the end of the conversation. Remembered rather
// than measured at the moment it is needed, because this pane is a tab panel:
// hiding it destroys its scroll box and showing it again hands back a fresh one
// scrolled to the top, so "where is it scrolled right now" stops answering "is
// this person reading the newest turn".
const following = ref(true)
const messages = ref([])
const sessions = ref([])
const total = ref(0)
const note = ref(null)
const live = ref(false)
const phase = ref('loading')
const problem = ref(null)
const draft = ref('')
const sending = ref(false)
const pending = ref([])
const showInjected = ref(false)

// The next `since` to poll with. Server-issued: computing it from a message count
// would skip or repeat the moment one record normalised to nothing.
let cursor = 0
let timer = null
let boxWatcher = null
let disposed = false
// One poll at a time. Two in flight would both read from the same cursor and both
// append what they found, so the conversation would show the overlap twice — and
// there are two ways to get two: a send asks for an immediate refresh, and a slow
// parse of a long transcript can outlast the interval.
let polling = false
// Bumped by every (re)start so a reply from a previous session's request cannot
// land in the list that replaced it.
let generation = 0

const visible = computed(() =>
  showInjected.value
    ? messages.value
    : messages.value.filter((message) => message.kind === 'said'),
)

const hiddenCount = computed(
  () => messages.value.length - messages.value.filter((m) => m.kind === 'said').length,
)

const canLoadEarlier = computed(
  () => messages.value.length > 0 && messages.value[0].seq > 0,
)

// The three states the bottom of the view can be in. Named rather than inlined
// because 'answer' is a pointer at another component, not a composer.
const composerMode = computed(() => {
  if (props.questionPending) return 'answer'
  return live.value ? 'send' : 'closed'
})

const spansSessions = computed(() => sessions.value.length > 1)

// Which of the three grounds a turn is drawn on. One decision, so a turn cannot
// end up with its header on one colour and its words on another — and one place
// to add a fourth class to, which is why the theme's names are a prefix.
//
// `kind` is asked before `role`, and that order is the whole of it: the harness
// injects turns into the model's context with role 'user' (a system reminder, a
// hook's output), and those are not something the operator sent. They are the
// same machinery as a tool call and they hide behind the same toggle, so they get
// the same ground. Which is a different question from how the words are rendered
// — that one is the role alone, below — because rendering is about not mangling
// bytes and this is about who is speaking.
//
// The agent ground is the *assistant's* rather than "not the operator's", which
// is the part worth stating: a watch firing is neither party, and drawing it as
// the agent talking would attribute an event to the session that only received
// it. Still keyed on the role alone, so where a turn came from stays a caption.
function ground(message) {
  if (message.kind !== 'said') return 'action'
  if (message.role === 'user') return 'operator'
  return message.role === 'assistant' ? 'agent' : 'action'
}

function speaker(message) {
  if (message.role === 'assistant') return props.agentLabel
  return SPEAKERS[message.role] || OTHER_SPEAKER
}

function stale(mine) {
  return disposed || mine !== generation
}

function atBottom() {
  const box = scroller.value
  if (!box) return true
  return box.scrollHeight - box.scrollTop - box.clientHeight <= STICK_SLACK_PX
}

function stickToBottom() {
  const box = scroller.value
  if (box) box.scrollTop = box.scrollHeight
}

// The only thing that decides whether this view is following: the reader's own
// scrolling. Scrolling back up says stop, scrolling to the end says resume.
function onScroll() {
  following.value = atBottom()
}

// Neither side of the text match is the bytes that were typed, so the comparison
// is on whitespace-collapsed text: every run of whitespace — newlines, tabs, a
// no-break space a keyboard or a terminal substituted — becomes one space, and the
// ends are trimmed. What a turn goes through before it comes back is the harness's
// own recording and then the server's normalisation, which joins a turn recorded
// as several text blocks with a blank line between them (lmer_platform.transcripts),
// and trimming only ever touched the ends.
//
// Lossy in whitespace and in nothing else, which is the property that matters:
// two messages whose *words* differ cannot collapse to one string, so the double
// "yes" the seq guard below exists for stays two distinguishable messages.
function comparable(text) {
  return String(text || '').replace(/\s+/g, ' ').trim()
}

// A sent message is matched to the transcript by its text: the harness assigns no
// id we could correlate on, and the alternative — trusting a timeout — would drop
// the pending bubble while the message was still on its way.
//
// Only messages that arrived *after* the send count. Without that, answering "yes"
// twice in one conversation would settle the second bubble against the first
// answer and stop showing that the new one is still in flight.
//
// The words themselves are not always recoverable, which is why there is a second
// way out of the bubble. The same server chokepoint that normalises a turn also
// masks credential shapes, deletes the harness's wrapper tags out of the middle of
// an operator turn, and keeps only the tail of a turn past its length cap
// (lmer_platform.transcripts) — each of which leaves a transcript copy that says
// something different from what was sent, and no text rule can match those without
// also matching messages that really are different. Reported live: a message the
// agent had plainly acted on stayed at the tail of the conversation as if it had
// just been sent, for the rest of the run.
//
// So an item settles on arrival evidence as well, which is two facts about the
// transcript and neither of them about text: it now holds an operator turn later
// than this send's cursor, and a turn of the agent's follows that one. Both are
// needed to say the message is no longer in flight —
//
//   - the harness records what it is sent in the order it receives it, so an
//     operator turn recorded after this send's cursor cannot be one that predates
//     this message: if such a turn is there, this message is written down too,
//     whatever the normalisation left of it;
//   - and the agent's turn postdates *that* turn, so the evidence is a reply
//     produced after the harness read input it took after this bubble appeared —
//     not the tail of the turn that was already running when the message went. A
//     harness queues a message typed at a working session and keeps streaming what
//     it was doing (Claude Code records the queued turn with a prompt source of
//     its own), so an agent turn on its own says nothing about whether this message
//     has been read, and settling on one would drop the bubble for the same reason
//     a timeout was rejected. It is also what stops an answer merged in from the
//     ask channel standing in for this message: that turn is placed by its own
//     clock rather than by the queue this one is in.
function settlePending() {
  if (!pending.value.length) return
  const said = messages.value.filter(
    (message) => message.role === 'user' && message.kind === 'said',
  )
  const agent = messages.value.filter((message) => message.role === 'assistant')
  pending.value = pending.value.filter((item) => {
    const arrived = said.filter((message) => message.seq >= item.since)
    if (arrived.some((message) => comparable(message.text) === comparable(item.text))) {
      return false
    }
    return !arrived.some(
      (message) => agent.some((reply) => reply.seq > message.seq),
    )
  })
}

function absorb(page, mode = 'append') {
  // Follow the end only when the end is what is being read. A poll every five
  // seconds that yanks the view down is what makes reading back through a long
  // conversation impossible on a phone — and 'prepend' is that read, so it never
  // follows. A fresh list is scrolled down because its tail is the interesting
  // part, which is why it was asked for as a tail.
  const follow = mode === 'replace' || (mode === 'append' && following.value)
  const arriving = page.messages || []
  if (mode === 'replace') {
    messages.value = arriving
    cursor = page.cursor
  } else if (mode === 'prepend') {
    // Older messages, so the cursor — which tracks the *end* — is untouched. The
    // seq filter is what keeps a page that overlaps what is already rendered from
    // showing a turn twice.
    const known = new Set(messages.value.map((message) => message.seq))
    messages.value = [
      ...arriving.filter((message) => !known.has(message.seq)),
      ...messages.value,
    ]
  } else {
    if (arriving.length) messages.value = [...messages.value, ...arriving]
    // Taken even from an empty page: it is the server's answer about where the end
    // is, and history can be renumbered by a transcript file that appeared late.
    cursor = page.cursor
  }
  total.value = page.total
  sessions.value = page.sessions || []
  note.value = page.note || null
  live.value = !!page.live
  settlePending()
  if (follow) nextTick(stickToBottom)
}

async function poll() {
  if (polling) return
  const mine = generation
  polling = true
  try {
    const page = await fetchSessionMessages(props.sessionId, {
      since: cursor,
      limit: PAGE,
    })
    if (stale(mine)) return
    absorb(page)
    problem.value = null
  } catch (exc) {
    if (stale(mine)) return
    // A poll that fails leaves the messages already rendered in place: this view
    // is history, and losing it because one request failed would be worse than a
    // stale list with a line saying so.
    problem.value = exc.message
  } finally {
    polling = false
  }
}

async function start() {
  generation += 1
  const mine = generation
  phase.value = 'loading'
  problem.value = null
  messages.value = []
  pending.value = []
  following.value = true
  cursor = 0
  try {
    const page = await fetchSessionMessages(props.sessionId, {
      since: -INITIAL_TAIL,
      limit: INITIAL_TAIL,
    })
    if (stale(mine)) return
    absorb(page, 'replace')
    phase.value = 'ready'
  } catch (exc) {
    if (stale(mine)) return
    phase.value = 'failed'
    problem.value = exc.message
  }
}

async function loadEarlier() {
  // Asking for older turns is the plainest possible statement that the end is not
  // what is being read; prepending leaves the scroll position where it was, which
  // on its own still counts as at the bottom of a short list.
  following.value = false
  const mine = generation
  const first = messages.value.length ? messages.value[0].seq : 0
  const since = Math.max(0, first - PAGE)
  try {
    const page = await fetchSessionMessages(props.sessionId, {
      since,
      limit: first - since || PAGE,
    })
    if (stale(mine)) return
    absorb(page, 'prepend')
  } catch (exc) {
    if (stale(mine)) return
    problem.value = exc.message
  }
}

async function send() {
  const text = draft.value.trim()
  if (!text || sending.value) return
  const mine = generation
  sending.value = true
  problem.value = null
  try {
    await sendSessionInput(props.sessionId, text)
    if (stale(mine)) return
    // Held until the transcript catches up. The send has already succeeded, so
    // this bubble is not a maybe — it is "delivered, not yet written down".
    // `since` is where the transcript ended when it went, which is what makes the
    // match forward-looking.
    pending.value = [...pending.value, { text, at: Date.now(), since: cursor }]
    draft.value = ''
    // Unconditionally, unlike a poll: this turn is one the operator just typed,
    // and not showing it would read as the send having gone nowhere.
    following.value = true
    nextTick(stickToBottom)
    // The transcript will not have it yet, but the round trip is cheap and it
    // makes the common case (a session that answers fast) feel immediate.
    poll()
  } catch (exc) {
    if (stale(mine)) return
    // Loud, and only here: the control plane refusing the input is the one thing
    // that means the session never got it.
    problem.value = `not sent — ${exc.message}`
  } finally {
    if (!stale(mine)) sending.value = false
  }
}

function pendingLabel(item) {
  return props.now - item.at > PENDING_GRACE_MS
    ? 'sent — the transcript has not caught up yet'
    : 'sending…'
}

onMounted(() => {
  start()
  timer = setInterval(poll, POLL_MS)
  // Nothing fires a scroll event when a tab panel is hidden and shown again, and
  // the box comes back at the top — so the box itself is watched, and a reader
  // who was following the end is put back on it. It also covers the pane growing
  // as the first turns land, and the viewport changing under it.
  boxWatcher = new ResizeObserver(() => {
    if (following.value) stickToBottom()
  })
  boxWatcher.observe(scroller.value)
})

onBeforeUnmount(() => {
  disposed = true
  generation += 1
  clearInterval(timer)
  boxWatcher.disconnect()
  boxWatcher = null
})

// A respawn puts a different session behind the same run. Its transcript is a
// different file, and the server joins it to the previous ones — so this restarts
// rather than appending blindly.
watch(() => props.sessionId, () => start())

// Following the end has to survive the renderer arriving after the turns do.
// `absorb` scrolls to the end of a pane whose bodies are still empty, and that is
// not the end once the words land; the ResizeObserver above does not catch it
// either, because a conversation long enough for this to matter has already filled
// the pane to its bound, so the box it observes never changes size. `post` so the
// DOM being measured is the one with the words in it, and `following` so a reader
// who has scrolled back is left where they are.
watch(rendererLoaded, () => {
  if (following.value) stickToBottom()
}, { flush: 'post' })
</script>

<template>
  <v-card class="mb-3">
    <v-card-text>
      <div class="d-flex flex-wrap align-center ga-2 mb-3">
        <span v-if="phase === 'loading'" class="text-body-small text-medium-emphasis">
          <v-progress-circular indeterminate size="12" width="2" class="me-2" />
          reading the transcript…
        </span>
        <span
          v-else-if="total"
          class="text-body-small text-medium-emphasis"
        >{{ total }} messages<template v-if="spansSessions"> across {{ sessions.length }} sessions</template></span>
        <v-spacer />
        <v-btn
          v-if="hiddenCount"
          :prepend-icon="mdiCogOutline"
          size="small"
          variant="tonal"
          @click="showInjected = !showInjected"
        >{{ showInjected ? 'hide' : 'show' }} {{ hiddenCount }} internal</v-btn>
      </div>

      <v-alert v-if="problem" type="error" density="compact" class="mb-3">
        {{ problem }}
        <v-btn
          v-if="phase === 'failed'"
          :prepend-icon="mdiRefresh"
          class="mt-2"
          variant="tonal"
          @click="start()"
        >try again</v-btn>
      </v-alert>

      <!-- The common answer today, and it must not read as "the run said nothing".
           The note comes from the server so the explanation stays in one place. -->
      <v-alert v-if="note && !messages.length" type="info" density="compact" class="mb-3">
        {{ note }}
      </v-alert>

      <div v-if="canLoadEarlier" class="d-flex mb-3">
        <v-btn
          :prepend-icon="mdiUnfoldMoreHorizontal"
          variant="tonal"
          size="small"
          @click="loadEarlier"
        >earlier messages</v-btn>
      </div>

      <div ref="scroller" class="chat" @scroll="onScroll">
        <div
          v-for="message in visible"
          :key="message.seq"
          class="turn"
          :class="[`turn-${message.role}`, `ground-${ground(message)}`]"
        >
          <div class="d-flex align-center ga-2 text-body-small text-medium-emphasis">
            <span>{{ speaker(message) }}</span>
            <span v-if="message.kind !== 'said'">· internal</span>
            <!-- A turn the server merged in from the session's ask channel: your
                 answer really is yours and the question really is the agent's,
                 but neither is in the transcript, so neither is presented as if
                 it were. See lmer_platform.transcripts on the merge. -->
            <span v-if="message.via === 'ask'">· ask channel</span>
            <span :title="message.at || ''">{{ ago(message.at, now) }}</span>
          </div>

          <!-- Yours verbatim, the agent's rendered. See the header comment for
               why the two halves are not treated the same, and Markdown.vue for
               what makes the second one safe. Both carry `said`, because the cap
               that keeps one turn from filling the pane is a property of a turn,
               not of how it was rendered. -->
          <!-- Above the text, because what was dropped is above it: the END of a
               turn is kept, since that is where an agent says what it did and
               what it wants. Trimming the tail instead left the preamble and cut
               the conclusion, which made a shortened report a useless one. -->
          <p
            v-if="message.truncated"
            class="text-body-small text-medium-emphasis mb-1"
          >
            … earlier part of this message trimmed — the terminal has all of it
          </p>
          <p
            v-if="message.text && message.role === 'user'"
            class="text-body-medium said plain"
          >{{ message.text }}</p>
          <!-- A watch firing: the condition it was armed on and the line that
               fired it, which the server has already dug out of the harness's
               injection markup and decoded (lmer_platform.transcripts). Its own
               line rather than either half of the split above — it is not prose
               to render and it is not something the operator sent — and keyed on
               the role, so how a turn is drawn still follows who it is from. -->
          <p
            v-else-if="message.text && message.role === 'monitor'"
            class="text-body-small said watch"
          >{{ message.text }}</p>
          <Markdown
            v-else-if="message.text"
            :text="message.text"
            class="text-body-medium said"
          />

          <!-- Tools are collapsed to a line each: name, what it acted on, and
               whether it failed. A wall of JSON is what the terminal is for.

               On the action ground even inside a turn that said something as
               well: a said turn with tool rows is one turn holding two kinds of
               content, and what the agent *did* is the second kind. Inside a turn
               that is already internal the ground is the same colour, which is
               right — that turn is machinery all the way down. -->
          <div v-if="message.tools.length" class="tools ground-action">
            <div
              v-for="(tool, index) in message.tools"
              :key="index"
              class="d-flex ga-2 align-start text-body-small"
            >
              <v-icon
                v-if="tool.status === 'failed'"
                :icon="mdiAlertCircleOutline"
                color="error"
                size="small"
              />
              <span :class="tool.status === 'failed' ? 'text-error' : 'text-medium-emphasis'">
                <strong>{{ tool.name }}</strong>
                <template v-if="tool.detail"> · {{ tool.detail }}</template>
                <template v-if="tool.status === 'pending'"> · running</template>
                <template v-if="tool.error"> — {{ tool.error }}</template>
              </span>
            </div>
          </div>
        </div>

        <!-- Sent, not yet in the transcript — and on the operator's ground like
             everything else you sent, because that is what it is. -->
        <div
          v-for="(item, index) in pending"
          :key="`pending-${index}`"
          class="turn turn-user ground-operator"
        >
          <div class="text-body-small text-medium-emphasis">
            you · {{ pendingLabel(item) }}
          </div>
          <p class="text-body-medium said plain">{{ item.text }}</p>
        </div>
      </div>

      <!-- A run stopped on a question has no session to type into: the answer is a
           distinct action, and an input box here would go nowhere. See
           composerMode. -->
      <v-alert v-if="composerMode === 'answer'" type="warning" density="compact" class="mt-3">
        This run stopped to ask you something and has already exited, so there is
        nothing to type into. Use the answer box above: the run is respawned with
        your answer, and the fresh session is what records it.
      </v-alert>

      <p
        v-else-if="composerMode === 'closed'"
        class="text-body-small text-medium-emphasis mt-3 mb-0"
      >
        Recorded conversation, not a live one: this session has exited. Its
        transcript is kept after the container goes, which is why the history above
        is still readable.
      </p>

      <template v-else>
        <!-- The session asked a proper question and is waiting on the channel for
             it. Typing here still works, but these keystrokes go to the harness's
             current focus — a permission dialog or an open menu eats them, and
             nothing on this side can tell. The reply box above cannot be
             misdelivered, so point at it rather than removing the composer. -->
        <v-alert v-if="askPending" type="warning" density="compact" class="mt-3">
          This session asked you something through its ask channel — reply in the
          box above. Text typed here goes to whatever the session's terminal is
          showing right now, which may be a menu rather than its prompt.
        </v-alert>

        <!-- Send is inside the field, and there are two ways to reach it.

             The icon: it costs no layout — the field already occupies that
             corner — which is what makes it worth keeping on every pointer.
             The terminal's key pad is hidden for a fine pointer because there
             it is *redundant* chrome (xterm takes the keystrokes itself, and
             the block sits between the terminal and everything under it);
             here the field is the only way in on every device, so hiding the
             one visible affordance would leave a desktop operator with a box
             and no sign of what sends it. `pointer: coarse` also reports the
             *primary* pointer, so a touchscreen laptop is `fine` — gating the
             only send control on it takes the button away from a finger.

             The chord: Ctrl+Enter, Cmd+Enter on a Mac, which is the same
             event with `metaKey` instead. Bare Enter is deliberately NOT
             bound — this is a multi-line box (auto-grow, max-rows) an
             operator writes a paragraph in, and a newline has to stay a
             newline. `.prevent` because a browser that would have inserted
             one on the chord must not leave it behind in a draft that a
             failed send keeps. -->
        <v-textarea
          v-model="draft"
          :disabled="sending"
          :label="composerLabel"
          hint="Ctrl+Enter (Cmd on a Mac) sends, Enter is a new line. Typed into the session, then read back from its transcript — so it appears here with a delay"
          persistent-hint
          rows="2"
          auto-grow
          max-rows="6"
          autocapitalize="sentences"
          class="mt-3"
          @keydown.ctrl.enter.prevent="send"
          @keydown.meta.enter.prevent="send"
        >
          <template #append-inner>
            <v-btn
              :icon="mdiSend"
              :loading="sending"
              :disabled="!draft.trim()"
              color="primary"
              variant="tonal"
              size="small"
              aria-label="send to this session"
              @click="send"
            />
          </template>
        </v-textarea>
      </template>
    </v-card-text>
  </v-card>
</template>

<style scoped>
/* A conversation is a column of turns, not a table: the only layout decisions
   here are the gap between them and which side a turn leans to.
   And a bound, because a transcript has hundreds of turns and this view no
   longer sits at the bottom of the page where it could simply grow: it is a tab
   panel with a composer under it. The bound is a share of the viewport rather
   than a pixel count — a pixel count is a small box on a desktop and still too
   tall for a phone — and a maximum rather than a height, so three turns are a
   short card instead of a mostly empty one. */
.chat {
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-height: 50vh;
  max-height: 50dvh;
  overflow-y: auto;
}

.turn {
  min-width: 0;
  max-width: 92%;
  border-radius: 10px;
  padding: 4px 10px 6px;
}

/* The colour coding, and only which class gets which: the tones themselves are
   the theme's, defined for both schemes in main.js so switching repaints them
   (tests/test_platform_web_theme.py holds that seam).
   Painted through the variables Vuetify emits rather than with `color=` on a
   v-sheet, because that prop also forces the text colour — and the text here is
   deliberately the emphasis classes' — and because a background is all that is
   wanted: a sheet brings elevation to a bubble that needs none. */
.ground-agent {
  background: rgb(var(--v-theme-chat-agent));
}

.ground-operator {
  background: rgb(var(--v-theme-chat-operator));
}

.ground-action {
  background: rgb(var(--v-theme-chat-action));
}

/* The tool rows as a block, so one turn's actions are one shape rather than a
   stack of striped lines. `start` is stated rather than inherited: an injected turn
   carries the operator's role without being the operator's words, so machine text
   must read left-to-right whatever a future lean does to the bubble around it. */
.tools {
  border-radius: 8px;
  padding: 3px 8px;
  margin-top: 4px;
  text-align: start;
}

/* What you said leans right, so a thumb-scroll can tell the two sides apart
   without reading either. The BUBBLE leans — the words in it do not: right-justified
   prose has a ragged left edge, which is where the eye returns for every line (the
   operator, on reading their own messages back). So no text-align here at all, and
   the inherited `start` is what a turn of yours gets. */
.turn-user {
  align-self: flex-end;
}

/* One turn's body, whichever of the two ways it is rendered — the class is on
   the <p> and on the <Markdown>, whose root element it reaches because a child
   component's root carries its parent's scope too. The bound is what both halves
   share: tool output and pasted files are routine in these transcripts, and a
   single message taller than the pane would fill it and push every other turn
   out of reach — rendering only made that more likely, because a list or a table
   is taller than the text it came from. `anywhere` because a long path or URL
   must scroll nothing sideways. Scroll chaining is left alone deliberately —
   reaching the end of a message has to carry on scrolling the conversation, or a
   thumb gets stuck in one bubble. */
.said {
  overflow-wrap: anywhere;
  margin: 2px 0 4px;
  max-height: 30vh;
  max-height: 30dvh;
  overflow-y: auto;
}

/* A watch firing, on the action ground its turn already has: two short lines of
   machine text, so they keep their newline like the verbatim half does. No
   colour of its own — see the header on why this is the third ground and not a
   fourth. */
.watch {
  white-space: pre-wrap;
}

/* What you sent, shown as sent. Transcript text carries its own newlines and
   collapsing them would run a formatted line into one paragraph. The rendered
   half needs no counterpart here: everything inside it is styled by Markdown.vue,
   which is also where the rules that reach into injected markup have to live. */
.said.plain {
  white-space: pre-wrap;
}
</style>
