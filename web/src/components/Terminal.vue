<script setup>
// One session's terminal: its scrollback, then its live output, and a way to type
// back into it (spec D16).
//
// The order is the whole design. Attaching to the live stream alone gives a blank
// screen until the session next prints something, and the first question on
// opening a session is always "what is it doing and how did it get here" — so the
// log is replayed first and the socket picks up exactly where the replay stopped,
// using the byte offset the server resolved for us. Neither side of that seam may
// guess: an offset computed from a byte count either duplicates output or
// silently loses a chunk, and both read as a session misbehaving rather than as a
// bug here.
//
// The replay is a bounded tail, not the whole file. A long run's log is hundreds
// of megabytes and none of it is interesting next to the last screenful, so
// asking for more is an explicit act ("earlier output") — which restarts the
// attach, because a terminal emulator cannot prepend to its own buffer.
//
// Mobile is the target, and xterm's hidden textarea is unreliable on iOS and
// Android: the keyboard opens, and keystrokes arrive doubled, dropped or
// autocorrected. So the affordance that has to work is the visible one — a field
// that submits a line, and buttons for the keys a phone keyboard does not have.
// Both are for that device and only that device: where the pointer is not a
// finger, they are hidden and the emulator takes the real keyboard directly
// (clicking it focuses it, and term.onData already encodes every key for a PTY).
//
// Why the fit is on, and still a switch
// -------------------------------------
// A resize is not a property of this client; it is a write to a PTY that belongs
// to the session, and it reflows that session's TUI for everyone attached. For a
// run this platform spawned there is no everyone: the daemon creates the PTY,
// nothing interactive is attached to it, and the drain thread only tees the
// output to a log. The one screen the session is being drawn for is this one — so
// left unfitted a phone renders an 80-column TUI into about 45 columns, which is
// not a degraded view but an unreadable one. Hence on by default.
//
// The switch stays for the case where that assumption is wrong: somebody is
// driving the session from a terminal on the host and this is a shoulder-surf.
// Turning it off only stops sending more — nothing here knows what size to put
// back.
//
// Reports are debounced either way: iOS and Android fire `resize` when the
// on-screen keyboard opens and again when it closes, and each of those would
// otherwise be a round trip that reflows the session mid-draw.
//
// Two logs, and why only one of them streams
// ------------------------------------------
// A session can have its output on disk twice, and the server picks one as the
// record: the log the session writes from inside its container wherever that
// exists, the host-side tee otherwise (lmer_platform.session_io.canonical_log).
// Every offset this component holds — the replay's, the socket's cursor — is a
// position in *that* file, which is the only reason a reconnect can resume
// without duplicating or losing output.
//
// For a session whose own log is the record, that leaves the launch out of the
// picture entirely: the image pull, the clone and lmer's own lines were printed
// on the host, before the container had a log to write to, and they live in the
// other file. "earlier output" cannot reach them — it pages back through the
// canonical log and stops at its first byte, which is the harness's first byte
// and not the session's. So the launch is a second, deliberate read of the
// *other* log, shown on its own: the two streams are never stitched together,
// because nothing here (or on the server) can tell where one ends inside the
// other, and a guess would show duplicated output as history that happened
// twice.

import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useTheme } from 'vuetify'
import {
  mdiArrowDown,
  mdiArrowLeft,
  mdiArrowUp,
  mdiInformationOutline,
  mdiRefresh,
  mdiRocketLaunchOutline,
  mdiSend,
  mdiUnfoldMoreHorizontal,
} from '@mdi/js'
import { Terminal as XTerm } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
// xterm ships its own stylesheet and it is not optional: without it the viewport
// is unpositioned and the terminal renders as a wall of wrapped text. Imported
// from the package so it is bundled with everything else — this UI is served on
// networks with no route out, where a CDN link is a blank screen.
import '@xterm/xterm/css/xterm.css'
import {
  decodeLogData,
  fetchSessionLog,
  mintTtyTicket,
  ttySocketUrl,
} from '../api.js'
import { toneColor } from '../format.js'
import {
  rememberChoice, rememberFlag, storedChoice, storedFlag,
} from '../preferences.js'

const props = defineProps({
  // The id, not the session object: the fleet poll replaces that object every ten
  // seconds, and a watcher on it would tear down a working terminal for no
  // reason. Liveness is not a prop either — the log route answers it freshly, and
  // a poll-old boolean is not what should decide whether input is offered.
  sessionId: { type: String, required: true },
})

// How much scrollback the first attach asks for, and what "earlier output" steps
// through. The ceiling is the server's limit on one read (MAX_LOG_LIMIT); past
// that the log is only readable as a file, which the detail view names.
const TAIL_STEPS = [64 * 1024, 256 * 1024, 1024 * 1024]

// What the server calls the log a session wrote itself, from inside its
// container (lmer_platform.session_io.LOG_SOURCE_CONTAINER). Every chunk says
// which log it came from; this is the one value that means "the launch happened
// somewhere else", and the affordance for it is offered on nothing else — a
// session served from the host tee already has its launch on screen.
const CONTAINER_LOG = 'container'

// The name the same module gives the host-side tee, which is what the launch
// read asks for by name instead of taking whatever is canonical.
const HOST_LOG = 'host'

// How much of the host log the launch view reads, from its first byte. A pull, a
// clone and a handful of announce lines are kilobytes; the bound is here because
// the same file goes on to hold everything the container forwarded, and reading
// all of that would show the session's output a second time in a different
// offset space.
const LAUNCH_BYTES = 64 * 1024

// Backoff for a dropped socket, capped so a phone left on this page keeps trying
// without hammering a daemon that is down. The last value repeats.
const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 15000]

// Separate from the socket's backoff because it answers a different question: the
// socket is up and working, only the session's control plane has not started
// listening yet. Short and finite — a harness takes a second or two to come up,
// and a plane that is still silent after this is not a startup race.
const GEOMETRY_RETRY_DELAYS = [1000, 2000, 4000, 8000, 15000]

// Orientation changes and the on-screen keyboard fire a burst of resize events,
// and for an operator who has opted into fitting, every one of those is a round
// trip that reflows the session — so they are coalesced.
const RESIZE_DEBOUNCE_MS = 250

// How much taller than the default the operator can make the emulator. x1 is what
// it has always been; the rest are for a desktop window where 55dvh leaves a TUI
// cramped while the page has room to spare. A short fixed set rather than a drag
// handle: the useful answers are "a bit more" and "a lot more", and a resizable
// pane is a lot of machinery plus a value to store for one of those.
const HEIGHT_SCALES = [1, 1.5, 2]
const HEIGHT_STORAGE_KEY = 'lmer.terminal.heightScale'
const FIT_STORAGE_KEY = 'lmer.terminal.fitToScreen'

// Both of these went through preferences.js when the remembered tab arrived and
// needed the same three rules this one already had: validate against what is
// offered now, fall back to the default on anything else, and survive a storage
// that throws instead of answering. Read that module for why each matters; the keys
// stay spelled out here, at the access, so what this app stores is reviewable
// without following an argument (tests/test_platform_web_app.py reads these lines).
//
// The height is a number, hence the parse: `'1.5' !== 1.5` would fail the check and
// silently reset the preference on every reload. Anything unrecognised falls back
// rather than being trusted — a bad multiplier in `calc()` collapses the box, which
// reads as a broken terminal rather than as a bad preference.
function storedHeightScale() {
  return storedChoice(
    () => window.localStorage.getItem(HEIGHT_STORAGE_KEY), HEIGHT_SCALES, Number,
  )
}

// The fit switch, remembered for the case it exists for: somebody watching a
// session that is being driven from a terminal on the host turns it off, and having
// it back on for the next session would reflow that terminal — the exact thing they
// turned it off to stop. Nothing stored means on, which is what it has always been.
function storedResizeOptIn() {
  return storedFlag(() => window.localStorage.getItem(FIT_STORAGE_KEY), true)
}

// The supervisor's /resize refuses a dimension outside 1..1000, and the platform
// relays that refusal as `resize_failed` — whose message says the PTY is probably
// gone. Clamping the TOP end keeps an implausibly wide window from producing
// that alarming and wrong diagnosis. The BOTTOM end is dropped, never clamped:
// fit() against a mid-animation sliver layout (a tab panel between states, a
// drawer mid-open) proposes 1-2 columns, and an earlier clamp-to-legal here
// turned exactly that artifact into a real write that reflowed a live session's
// TUI to one character per line for every watcher (2026-07-29). A too-small
// reading means "the box is not laid out yet", and the answer to that is
// silence — the next real layout reports the real size. The daemon enforces the
// same floor (session_io.MIN_RESIZE_*) against any other client.
const MIN_COLS = 20
const MIN_ROWS = 5
const MAX_DIMENSION = 1000

// What the key buttons send. Named here because a control byte written into the
// markup is invisible in a diff and unsearchable in review.
const KEYS = {
  // CR, not LF: a PTY in raw mode is where these land, and its TUI reads CR as
  // "submit" while LF is a literal newline in the input box.
  enter: '\r',
  up: '\x1b[A',
  down: '\x1b[B',
  tab: '\t',
  escape: '\x1b',
  interrupt: '\x03',
}

const PHASE_META = {
  loading: { label: 'loading', tone: 'idle' },
  live: { label: 'live', tone: 'live' },
  reconnecting: { label: 'reconnecting', tone: 'attention' },
  history: { label: 'history', tone: 'idle' },
  // Its own phase rather than `history`, which says "this session has exited":
  // the session can be running perfectly while this shows how it started.
  launch: { label: 'launch output', tone: 'idle' },
  failed: { label: 'failed', tone: 'bad' },
}

const host = ref(null)
const phase = ref('loading')
const notice = ref(null)
const problem = ref(null)
const line = ref('')
const tailStep = ref(0)
const earlierExists = ref(false)
// Which of the session's two logs the last read came from, as the server
// reported it. Null until the first read answers: nothing here guesses, because
// the answer is a probe of a file on the host and not a property of this client.
const logSource = ref(null)
// Whether this terminal is showing the host-side launch output instead of the
// session's own stream. A view, not a preference: it is left on every restart.
const showingLaunch = ref(false)
// What that read found, so the view can say which of the two it is: a host log
// with nothing in it (a session the daemon never teed), and one whose head is
// only the first part of a much longer file, read as very different things.
const launchEmpty = ref(false)
const launchTruncated = ref(false)
// Whether the emulator has the keyboard. Everything typed with a real keyboard —
// and every paste — goes to the focused element, so an unfocused terminal
// swallows nothing and reads as a broken one. Tracked so the view can say so.
const focused = ref(false)
// On, because the session was spawned for this screen and no other. See the
// module docstring; the switch is for the exception, not the rule — and the
// exception is now remembered, so nothing here reads `true` unless this operator
// has never turned it off.
const resizeOptIn = ref(storedResizeOptIn())
// Reactive, because the control has to be able to say it is not available: the
// answer only arrives after the first attempt, from the session's own supervisor.
const resizeSupported = ref(true)
// Ctrl-C is confirmed rather than sent, so the tap that starts the quit chord is
// never the same tap that finishes it. See interrupt().
const confirmingInterrupt = ref(false)
// Remembered, because it is a property of this operator's screen and not of the
// session — re-picking it on every run is the same annoyance as the tab that
// forgets which one you were on.
const heightScale = ref(storedHeightScale())

const theme = useTheme()

// Non-reactive on purpose: nothing in the template reads them, and a reactive
// WebSocket or Terminal would have Vue walking an emulator's internals.
let term = null
let fit = null
let socket = null
// The next log byte this terminal has not rendered. Every reconnect resumes from
// it, which is what makes a drop invisible rather than a hole in the output.
let cursor = 0
let attempt = 0
let reconnectTimer = null
let resizeTimer = null
// Reset per connection, not per component: a reconnect is a fresh chance for the
// plane to be up, and carrying the count over would spend the budget on the
// attempt that was always going to fail.
let geometryRetryTimer = null
let geometryRetries = 0
// Whether a "still starting" line is on screen and owed a clear. Tracked
// separately from the retry count because the count stops at the budget while the
// stale message can outlive it by a long way.
let geometryDeferred = false
let boxWatcher = null
let finished = false
let disposed = false
// Bumped by every (re)start, so an in-flight ticket request or a socket belonging
// to a previous attach cannot write into the terminal that replaced it.
let generation = 0

const meta = computed(() => PHASE_META[phase.value] || PHASE_META.loading)
const canType = computed(() => phase.value === 'live')
const showInput = computed(
  () => phase.value !== 'history' && phase.value !== 'failed'
    && phase.value !== 'launch',
)
// `earlierExists` is the canonical log's own answer — the resolved offset of the
// replay, which is 0 exactly when the terminal is showing that file from its
// first byte. At the origin there is nothing left to page back to *in this
// stream*, and offering the step anyway would re-read the same bytes and look
// like a button that does nothing. What is genuinely out of reach there is the
// other log, which is what `canShowLaunch` is for.
const canLoadEarlier = computed(
  () => !showingLaunch.value
    && earlierExists.value && tailStep.value < TAIL_STEPS.length - 1,
)
const atOldestChunk = computed(
  () => !showingLaunch.value
    && earlierExists.value && tailStep.value === TAIL_STEPS.length - 1,
)
// Offered only where there is a second log to offer: a session whose record is
// the host tee already has its launch in the scrollback above, and an older
// image never writes the container log at all — for both of those this is
// exactly the view it has always been.
const canShowLaunch = computed(
  () => !showingLaunch.value && logSource.value === CONTAINER_LOG,
)

function stale(mine) {
  return disposed || mine !== generation
}

function sizeLabel(bytes) {
  return bytes >= 1024 * 1024 ? `${bytes / (1024 * 1024)} MiB` : `${bytes / 1024} KiB`
}

// xterm refuses a colour it cannot parse, so only a plain hex value from the
// theme is passed through. The palette lives in main.js; nothing here decides
// what a terminal looks like beyond which names it reads.
//
// `terminal` and not `surface`: the emulator has a ground of its own, dark in both
// schemes (the operator asked for a darker one, and main.js argues the value). Which
// is also why the ink is `on-terminal` rather than `on-surface` — the light scheme's
// on-surface is near-black, i.e. invisible here, and the box below has to be painted
// with the same pair or the padding around xterm shows the card through it.
const HEX_COLOR = /^#[0-9a-f]{6}$/i

function terminalTheme() {
  const colors = theme.current.value.colors || {}
  const pick = (name) => (HEX_COLOR.test(colors[name] || '') ? colors[name] : undefined)
  const foreground = pick('on-terminal')
  const accent = pick('primary')
  return {
    background: pick('terminal'),
    foreground,
    // xterm's default cursor and selection are white, which disappears on a
    // light ground; the cursor therefore follows the ink this ground carries.
    cursor: foreground,
    selectionBackground: accent ? `${accent}55` : undefined,
  }
}

// The clipboard chords this component takes back off xterm. Paste first, which
// is the one that was broken.
//
// The operator, live testing: pasting into the terminal only worked through the
// context menu's "paste as plain text"; Ctrl+V did nothing. Nothing here was eating
// it. xterm binds its own keydown listener on its hidden textarea in the capture
// phase, and for Ctrl+V its keyboard evaluation resolves the chord the way a
// terminal does — keyCode 86 with Ctrl is the control byte 0x16 — and then cancels
// the event (preventDefault). A cancelled keydown has no default action, so the
// browser never fires `paste`, and the PTY gets 0x16, which in these TUIs looks
// like nothing happening. The context menu worked because that path dispatches a
// ClipboardEvent with no keydown in front of it, and xterm listens for `paste`.
//
// So the fix is to *decline* the chord: returning false from the custom handler
// makes xterm return before it encodes or cancels anything, the browser's own
// paste proceeds into the textarea, and xterm's paste listener hands the text to
// term.onData — the same callback every keystroke arrives on. Pasted bytes
// therefore take the /input path typed bytes take, and bracketed paste stays
// where it belongs (xterm brackets it only if the harness asked for that mode).
//
// Deliberately no navigator.clipboard.readText: it needs a secure context, and
// this UI is served over plain http on a LAN as often as not — a fix that
// depended on it would work on localhost and fail in exactly the deployment this
// platform is for. Nothing here reads the clipboard at all; the browser does.
//
// What this costs is a literal 0x16 (readline's quoted-insert) from a real
// keyboard. That trade is the right way round: nothing else in a browser can
// paste, and quoted-insert is not why anyone opens this terminal.
function isBrowserPaste(event) {
  // keydown only. xterm asks this handler about keyup and keypress too, and a
  // declined keyup skips the focus and cursor bookkeeping it does there.
  if (event.type !== 'keydown') return false
  if ((event.ctrlKey || event.metaKey) && !event.altKey) {
    // `code` as well as `key`: on a non-Latin layout the same physical chord
    // reports a different `key`, while xterm still encodes it from the keyCode —
    // so matching only the letter would leave those keyboards unable to paste.
    return event.key === 'v' || event.key === 'V' || event.code === 'KeyV'
  }
  // Shift+Insert is the other paste this has to keep working. xterm already
  // leaves it alone, which is why it is named here rather than trusted to.
  return event.key === 'Insert' && event.shiftKey && !event.ctrlKey && !event.altKey
}

// Copying, which is the other half and a different shape of problem.
//
// Selecting output and pressing Ctrl+C cannot work here and never will: that
// chord is the interrupt byte, and a terminal that copied instead would have no
// way to send it. So the copy chords are the ones a terminal user already knows
// — Ctrl+Insert, Cmd+C on a Mac, and Ctrl+Shift+C — and they divide by who
// actually performs the copy.
//
// Ctrl+Insert and Cmd+C are the browser's. xterm declines to encode either (its
// insert case skips when Ctrl or Shift is held, and Cmd+C falls off the end of
// its keyboard evaluation with nothing to send), and the browser's copy is
// answered by a listener xterm registers on the terminal element: when the
// terminal has a selection it fills the event's clipboard data with the
// *terminal's* selection text and cancels the default. So the clipboard gets the
// wrapped, reflowed text the operator selected rather than whatever the DOM
// thinks is selected. Both are named here rather than trusted to, for the reason
// Shift+Insert is.
function isBrowserCopy(event) {
  if (event.type !== 'keydown') return false
  if (event.key === 'Insert') {
    return event.ctrlKey && !event.shiftKey && !event.altKey
  }
  // Cmd, and pointedly not Ctrl: Ctrl+C is the interrupt this terminal exists to
  // be able to send, so it must reach xterm's encoder untouched.
  return event.metaKey && !event.ctrlKey && !event.altKey
    && (event.key === 'c' || event.key === 'C' || event.code === 'KeyC')
}

// Ctrl+Shift+C is the third one, and the only chord here that is not the
// browser's to perform. No browser binds it to copy — in Chrome and Firefox it
// is the inspector — so unlike the paste chord there is nothing to hand back:
// xterm already leaves it alone (with Shift held it resolves to no key at all,
// so xterm returns without cancelling), and the operator gets an inspector and
// an empty clipboard.
function isTerminalCopy(event) {
  if (event.type !== 'keydown') return false
  return event.ctrlKey && event.shiftKey && !event.altKey && !event.metaKey
    && (event.key === 'c' || event.key === 'C' || event.code === 'KeyC')
}

// …so this one is performed here, and still without this component ever handling
// the text. `document.execCommand('copy')` is the copy the browser would have
// run: it dispatches a `copy` event synchronously, xterm's own listener answers
// it with the selection, and the clipboard write is the browser's. Deprecated
// and used anyway, because the replacement (navigator.clipboard.writeText) needs
// a secure context and this UI is served over plain http on a LAN as often as
// not — the same reason the paste path above does not read the clipboard.
//
// The selection is put in xterm's textarea first, which is the trick xterm
// itself uses for the right-click menu (Clipboard.rightClickHandler): the copy
// event fires on the focused element, and a browser asked to copy with nothing
// selected anywhere can decline to fire it at all. The textarea is already
// focused — the chord arrived on its keydown listener — and xterm empties it on
// every paste, so borrowing it costs nothing.
//
// Returns whether it copied, so a chord pressed with no selection is left alone
// rather than swallowed.
function copySelection() {
  const selection = term ? term.getSelection() : ''
  const textarea = term ? term.textarea : null
  if (!selection || !textarea) return false
  textarea.value = selection
  textarea.select()
  let copied = false
  try {
    copied = document.execCommand('copy')
  } catch {
    copied = false
  }
  textarea.value = ''
  if (!copied) {
    // Loud, because a copy that silently did not happen is discovered at the
    // paste — by which time whatever was on the clipboard before is what lands.
    notice.value = 'the browser refused that copy — try Ctrl+Insert, or the '
      + 'right-click menu'
  }
  return copied
}

// Clicking the terminal is what gives it the keyboard, and until it has one,
// typing and Ctrl+V go wherever the focus actually is — which reads as a
// terminal that drops input at random. The click handler is on the whole box
// rather than on the emulator, so the padding around it counts too, and the hint
// says so for as long as the emulator does not have the focus.
function focusTerminal() {
  if (term) term.focus()
}

function buildTerminal() {
  if (term) term.dispose()
  term = null
  fit = null
  // dispose() takes its element with it; the container is this component's alone,
  // so making that certain costs nothing.
  if (host.value) host.value.replaceChildren()

  term = new XTerm({
    // Input stays off until the platform confirms the session is live, so a dead
    // session's terminal cannot swallow keystrokes that would go nowhere.
    disableStdin: true,
    // The stack style.css gives <code>: system monospace, never a download.
    fontFamily: 'ui-monospace, "SFMono-Regular", Menlo, monospace',
    // Small enough that a phone in portrait reports a usable column count. This
    // number decides the geometry the session is told it has, and therefore how
    // its own TUI lays itself out.
    fontSize: 12,
    // A 64 KiB tail can be well past xterm's default 1000 lines, and scrollback
    // dropped on the way in would make "earlier output" a lie.
    scrollback: 5000,
    theme: terminalTheme(),
  })
  fit = new FitAddon()
  term.loadAddon(fit)
  term.open(host.value)
  // A real keyboard, when there is one. Everything xterm produces — arrows, Ctrl
  // chords, pasted text — arrives here already encoded for a PTY.
  term.onData((data) => sendInput(data))
  // The clipboard chords, in the two shapes described above: the ones the
  // browser performs are declined so its own default action runs, and the one no
  // browser performs is performed here — but only when there is a selection, so
  // the chord is otherwise left exactly as it was.
  term.attachCustomKeyEventHandler((event) => {
    if (isBrowserPaste(event) || isBrowserCopy(event)) return false
    if (isTerminalCopy(event) && copySelection()) {
      // Handled, so the chord's other meaning does not also fire.
      event.preventDefault()
      return false
    }
    return true
  })
  applyGeometry()
}

function render(frame) {
  if (!term) return
  // Bytes, not text: xterm decodes UTF-8 itself and carries a sequence split
  // across two writes. next_offset comes from the server for the same reason.
  term.write(decodeLogData(frame.data))
  // Which log this came from, taken from every frame and not only from the
  // replay: the server resolves the source per stream, and a session that starts
  // writing its own log while somebody is watching is served the tee until the
  // next reconnect. Believing the first answer forever would offer the launch
  // view for a session that has not got a second log yet, or hide it from one
  // that has.
  if (frame.source) logSource.value = frame.source
  cursor = frame.next_offset
}

function clampDimension(value, floor) {
  if (!Number.isFinite(value) || value < floor) return 0
  return Math.min(Math.round(value), MAX_DIMENSION)
}

function reportGeometry() {
  // The single gate on writing to a shared PTY. Nothing else in this component
  // sends a resize frame, so opting out here is the whole guarantee that watching
  // a session does not change it for whoever else is watching.
  if (!resizeOptIn.value) return
  if (!term || !resizeSupported.value) return
  if (!socket || socket.readyState !== WebSocket.OPEN) return
  const rows = clampDimension(term.rows, MIN_ROWS)
  const cols = clampDimension(term.cols, MIN_COLS)
  if (!rows || !cols) return
  // Clear a previous "still starting" line as the next attempt goes out. A
  // successful resize is answered with SILENCE by design (the server only speaks
  // up to refuse), so there is no success event to clear it on — which left the
  // message on screen after the retry had worked and the terminal was fitting
  // fine. Optimistic and self-correcting: a refusal comes back within a round
  // trip and sets its own line again.
  if (geometryDeferred) {
    geometryDeferred = false
    notice.value = null
  }
  socket.send(JSON.stringify({ type: 'resize', rows, cols }))
}

function applyGeometry() {
  // Nothing to measure, nothing to report. The terminal sits in a tab panel and
  // is display:none whenever the conversation is showing: fit() computes nothing
  // from a box with no width, and reporting from there would write a stale size
  // to the PTY on behalf of a pane nobody is looking at.
  if (!fit || !host.value?.clientWidth) return
  // The local half runs whether or not the session is ever told about it — the
  // emulator has to lay out for the box it is in.
  fit.fit()
  reportGeometry()
}

function setHeightScale(scale) {
  if (!HEIGHT_SCALES.includes(scale)) return
  heightScale.value = scale
  rememberChoice(
    (value) => window.localStorage.setItem(HEIGHT_STORAGE_KEY, value),
    HEIGHT_SCALES,
    scale,
  )
  // No explicit refit: the box changing size is what the ResizeObserver on the
  // host is for, and it goes through the same debounce as a rotation — so one
  // taller terminal is one geometry report, not one per animation frame.
}

function setResizeOptIn(enabled) {
  // Read from the event rather than the model, so this does not depend on which
  // of two handlers Vue runs first.
  resizeOptIn.value = !!enabled
  // Remembered here and nowhere else, which is the whole of the distinction: this
  // is the switch, so reaching it means the operator decided. The two places the
  // *platform* turns fitting off — a session that cannot be resized, and one whose
  // PTY is going away — must not be stored, or an old image seen once would leave
  // every terminal afterwards unfitted with nothing on screen saying why.
  rememberFlag(
    (value) => window.localStorage.setItem(FIT_STORAGE_KEY, value),
    resizeOptIn.value,
  )
  // Turning it off cannot put the session back the way it was — nothing here
  // knows what size it had before — so it only stops sending more.
  if (resizeOptIn.value) applyGeometry()
}

function onWindowResize() {
  clearTimeout(resizeTimer)
  resizeTimer = setTimeout(applyGeometry, RESIZE_DEBOUNCE_MS)
}

function sendInput(data, appendNewline = false) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    // Loud: an answer the operator believes was delivered, while the session sits
    // there still waiting for it, is worse than an error.
    problem.value = 'not connected — that input was not sent'
    return
  }
  socket.send(
    JSON.stringify({ type: 'input', data, append_newline: appendNewline }),
  )
}

function submitLine() {
  // append_newline is the control plane's "and press Enter", which it turns into
  // the CR a raw-mode TUI reads as a submit. An empty field therefore sends a
  // bare Enter — often exactly what a waiting prompt needs.
  sendInput(line.value, true)
  line.value = ''
}

// Every Ctrl-C goes through a confirmation. Esc is what interrupts a turn in
// these TUIs; Ctrl-C's real function is the first half of the quit chord — twice
// in a row and the harness exits — and stopping a run is otherwise unbuilt, so
// the button has to stay. On a phone the second tap is one mis-aim away, and a
// dialog between the two presses is also what makes an accidental double
// impossible, so no separate double-press guard is needed.
function interrupt() {
  confirmingInterrupt.value = false
  sendInput(KEYS.interrupt)
}

function handleStatus(frame) {
  const message = frame.message || frame.event
  switch (frame.event) {
    case 'ended':
      // The socket is about to close and there is nothing to reconnect to. The
      // log stays readable, which is the state this drops into.
      finished = true
      phase.value = 'history'
      notice.value = message
      if (term) term.options.disableStdin = true
      return
    case 'resize_unsupported':
      // A deployment fact — an older image, or a supervisor with no PTY hook. Not
      // an error, but it is now the answer to something the operator asked for, so
      // it gets a line rather than silence, and the control goes unavailable
      // instead of asking again on every rotation.
      resizeSupported.value = false
      resizeOptIn.value = false
      notice.value = message
      return
    case 'resize_failed':
      // The ioctl failed, which in practice means the PTY is gone and the session
      // is ending. Said once, then not asked again: the follower will report the
      // exit in its own words.
      resizeSupported.value = false
      resizeOptIn.value = false
      problem.value = message
      return
    case 'resize_refused':
      // The daemon's sanity floor (session_io.MIN_RESIZE_*): the geometry read
      // like a layout artifact, not a window. This client drops such readings
      // before sending, so hearing it means some other watcher proposed one —
      // a notice, and nothing is switched off: this terminal's own fitting is
      // fine, and the next real layout on the other side reports a real size.
      notice.value = message
      return
    case 'resize_deferred':
      // The control plane is not listening *yet* — a session opened before its
      // harness finished starting. Transient, so nothing is switched off and this
      // is a notice rather than a problem: the previous behaviour disabled fitting
      // and left an error that survived until the page was reloaded, on a session
      // that came up seconds later. Retried with backoff, and bounded, because a
      // plane that never answers is a real failure the follower will report.
      geometryDeferred = true
      if (geometryRetries >= GEOMETRY_RETRY_DELAYS.length) {
        // Still not a latch: the observer keeps calling reportGeometry on a
        // rotation, a tab switch or a height change, and any of those clears this
        // line if the plane has come up since.
        notice.value = `${message} — giving up on fitting for now`
      } else {
        const delay = GEOMETRY_RETRY_DELAYS[geometryRetries]
        geometryRetries += 1
        notice.value = `the session is still starting — fitting it in ${
          Math.round(delay / 1000)}s`
        clearTimeout(geometryRetryTimer)
        geometryRetryTimer = setTimeout(() => {
          // Only if the operator still wants it and the socket is still the one
          // that asked: a retry firing into a torn-down terminal would resize a
          // PTY on behalf of a view nobody is looking at.
          if (resizeOptIn.value && socket && socket.readyState === WebSocket.OPEN) {
            reportGeometry()
          }
        }, delay)
      }
      return
    case 'log_failed':
      finished = true
      phase.value = 'failed'
      problem.value = message
      return
    case 'input_failed':
    case 'bad_frame':
      problem.value = message
      return
    default:
      notice.value = message
  }
}

function handleFrame(text) {
  let frame
  try {
    frame = JSON.parse(text)
  } catch {
    return
  }
  if (frame.type === 'data') {
    render(frame)
    return
  }
  if (frame.type === 'status') {
    handleStatus(frame)
    return
  }
  if (frame.type === 'open') {
    attempt = 0
    notice.value = null
    problem.value = null
    // The session can have exited between the replay and the handshake, in which
    // case this is history and the follower is about to say so. Believing the
    // handshake keeps the view from offering a prompt for those few milliseconds.
    phase.value = frame.live ? 'live' : 'history'
    if (term) term.options.disableStdin = !frame.live
    // Re-applies a fit the operator asked for, because this may be a reconnect and
    // the PTY's size is not this client's to remember. A no-op otherwise.
    reportGeometry()
  }
}

function teardownSocket() {
  clearTimeout(reconnectTimer)
  reconnectTimer = null
  // A pending geometry retry belongs to the socket that asked for it: firing it
  // after a teardown would resize a PTY on behalf of a view that is gone, and the
  // next connection gets its own budget.
  clearTimeout(geometryRetryTimer)
  geometryRetryTimer = null
  geometryRetries = 0
  geometryDeferred = false
  const closing = socket
  socket = null
  if (!closing) return
  // Handlers off before the close: a frame delivered after a restart would be
  // written into a terminal that has already replayed those same bytes.
  closing.onmessage = null
  closing.onclose = null
  closing.close()
}

function scheduleReconnect(mine, reason) {
  socket = null
  if (stale(mine) || finished) return
  const delay = RECONNECT_DELAYS[Math.min(attempt, RECONNECT_DELAYS.length - 1)]
  attempt += 1
  phase.value = 'reconnecting'
  notice.value =
    `${reason} — reconnecting in ${Math.round(delay / 1000)}s (attempt ${attempt})`
  reconnectTimer = setTimeout(() => {
    if (!stale(mine)) connect(mine)
  }, delay)
}

async function connect(mine) {
  let ticket
  try {
    // A ticket is consumed by the handshake it authorizes, so every attempt —
    // the first one and every reconnect — has to mint its own.
    ticket = (await mintTtyTicket(props.sessionId)).ticket
  } catch (exc) {
    if (stale(mine)) return
    // A refusal (401, 404, 409) will refuse the next request identically; only a
    // transport or server failure is worth waiting out.
    if (exc.status && exc.status < 500) {
      finished = true
      phase.value = 'failed'
      problem.value = exc.message
      return
    }
    scheduleReconnect(mine, exc.message)
    return
  }
  if (stale(mine)) return

  // offset: the socket streams the log from here, so it continues exactly where
  // the replay — or the socket before it — stopped.
  const ws = new WebSocket(ttySocketUrl(props.sessionId, { ticket, offset: cursor }))
  socket = ws
  ws.onmessage = (event) => {
    if (!stale(mine)) handleFrame(event.data)
  }
  // No onerror handler: the browser always follows one with a close, and the
  // event itself carries nothing that could be shown to anyone.
  ws.onclose = () => {
    if (socket === ws) scheduleReconnect(mine, 'connection lost')
  }
}

// The launch read, inline for the reason RunDetail's loadRunFiles is inline:
// this slice's file scope is the component, and api.js belongs to a change in
// flight. It goes there the next time this file is opened.
//
// `source` is the read-only parameter T79 added to the route: it names a log
// instead of taking whichever one is canonical, which is the whole of what makes
// the other file reachable. Offset 0 and a bound, because what is wanted is the
// head of that file — the part that was printed before the session had a log of
// its own.
async function fetchLaunchLog(sessionId) {
  const query = new URLSearchParams({
    offset: '0', limit: String(LAUNCH_BYTES), source: HOST_LOG,
  })
  const response = await fetch(
    `api/sessions/${encodeURIComponent(sessionId)}/log?${query}`,
    { credentials: 'same-origin' },
  )
  const payload = await response.json().catch(() => null)
  if (!response.ok) throw new Error(payload?.detail || `HTTP ${response.status}`)
  return payload
}

async function showLaunchOutput(mine) {
  let chunk
  try {
    chunk = await fetchLaunchLog(props.sessionId)
  } catch (exc) {
    if (stale(mine)) return
    phase.value = 'failed'
    problem.value = exc.message
    return
  }
  if (stale(mine)) return

  // Written, not rendered: these offsets are positions in the *other* file, and
  // the cursor this component keeps is the canonical log's. Taking one from here
  // would resume a socket at a byte position that means something else, which is
  // the mistake every offset in this component is arranged to prevent.
  if (term) term.write(decodeLogData(chunk.data))
  launchEmpty.value = !chunk.size
  launchTruncated.value = chunk.next_offset < chunk.size
  // Nothing to follow and nothing to type into: this is a fixed stretch of a
  // file, and the live stream belongs to the log this view is not showing.
  finished = true
  phase.value = 'launch'
  notice.value = null
  applyGeometry()
}

async function start() {
  generation += 1
  const mine = generation
  teardownSocket()
  clearTimeout(resizeTimer)
  finished = false
  attempt = 0
  cursor = 0
  phase.value = 'loading'
  problem.value = null
  if (showingLaunch.value) {
    notice.value = 'reading how this session was launched…'
    buildTerminal()
    await showLaunchOutput(mine)
    return
  }
  const tailBytes = TAIL_STEPS[tailStep.value]
  notice.value = `reading the last ${sizeLabel(tailBytes)} of output…`
  buildTerminal()

  let chunk
  try {
    // Negative offset: the last tailBytes of a log of any size, without a round
    // trip to ask how big it is.
    chunk = await fetchSessionLog(props.sessionId, {
      offset: -tailBytes,
      limit: tailBytes,
    })
  } catch (exc) {
    if (stale(mine)) return
    phase.value = 'failed'
    problem.value = exc.message
    return
  }
  if (stale(mine)) return

  render(chunk)
  // The resolved start of the replay, in the canonical log's own space: zero
  // means this terminal is showing that file from its first byte, which is as
  // far back as this stream goes. Not `size`, and not the source — a session
  // that records itself still has a beginning, and this is it.
  earlierExists.value = chunk.offset > 0
  applyGeometry()

  // The log's own answer about liveness, not the fleet view's: that one is up to
  // a poll interval old, and this decides whether anything can be typed.
  if (!chunk.live) {
    finished = true
    phase.value = 'history'
    notice.value = null
    return
  }
  await connect(mine)
}

function loadEarlier() {
  // xterm cannot prepend to its buffer, so more history means replaying from
  // further back: terminal rebuilt, socket resumed from the new end.
  tailStep.value += 1
  start()
}

// Both ways through the same restart the two above use, because a terminal
// emulator cannot show two streams at once any more than it can prepend to its
// own buffer. Coming back re-reads the tail and re-attaches, which is what makes
// the live view live again rather than a screenful from a minute ago.
function showLaunch() {
  showingLaunch.value = true
  start()
}

function hideLaunch() {
  showingLaunch.value = false
  start()
}

onMounted(() => {
  window.addEventListener('resize', onWindowResize)
  // The window is not the only thing that changes this box. The terminal lives in
  // a tab panel that is display:none while the conversation is showing, so a
  // rotation there resizes nothing measurable and leaves the emulator laid out
  // for the screen it last saw. Watching the box itself catches that the moment
  // the tab comes back, and the debounce lets the tab transition settle first.
  boxWatcher = new ResizeObserver(onWindowResize)
  boxWatcher.observe(host.value)
  start()
})

onBeforeUnmount(() => {
  disposed = true
  generation += 1
  window.removeEventListener('resize', onWindowResize)
  boxWatcher.disconnect()
  boxWatcher = null
  clearTimeout(resizeTimer)
  teardownSocket()
  if (term) term.dispose()
  term = null
  fit = null
})

// A respawn puts a different session behind the same run, and its log is a
// different file — both of them, and neither answer carries over: the new
// session may be from an older image with no log of its own, and staying in the
// launch view would show a different session's launch under the same heading.
watch(() => props.sessionId, () => {
  tailStep.value = 0
  showingLaunch.value = false
  logSource.value = null
  start()
})

// The OS flipping between light and dark repaints everything else through the
// theme; the emulator has to be told.
watch(() => theme.current.value.dark, () => {
  if (term) term.options.theme = terminalTheme()
})
</script>

<template>
  <v-card class="mb-3">
    <!-- Clicking anywhere in the box focuses the emulator, padding included:
         xterm only focuses itself from a click that lands on its own element, and
         a click a few pixels off it left the keyboard where it was — which is a
         terminal that ignores what you type. `focusin`/`focusout` rather than
         `focus`/`blur` because only those bubble, and what has the keyboard is
         xterm's hidden textarea rather than this box. -->
    <div
      ref="host"
      class="terminal-host"
      :style="{ '--term-scale': heightScale }"
      @click="focusTerminal"
      @focusin="focused = true"
      @focusout="focused = false"
    />

    <v-card-text>
      <div class="d-flex flex-wrap align-center ga-2 mb-2">
        <v-chip :color="toneColor(meta.tone)" variant="tonal">
          <v-progress-circular
            v-if="phase === 'loading' || phase === 'reconnecting'"
            indeterminate
            size="12"
            width="2"
            class="me-2"
          />
          {{ meta.label }}
        </v-chip>
        <!-- One line: state, the way back, the height, and the one control that
             writes to something shared. They were three stacked rows, which on a
             phone spent most of a screenful saying very little.
             Two things keep it one line rather than nominally one line. There is no
             `v-spacer`: right-aligning the controls separated them from the state
             chip the moment the row wrapped, which is what put `live` on a row of
             its own. And the notice comes LAST, because it is the only element here
             whose width is unbounded — in front, a long one filled the first line
             and pushed every fixed-size control below it. `flex-wrap` is still on,
             so a narrow screen wraps rather than overflowing; what wraps now is the
             prose, which is the part that can afford to. -->
        <v-btn
          v-if="canLoadEarlier"
          size="small"
          variant="tonal"
          :prepend-icon="mdiUnfoldMoreHorizontal"
          @click="loadEarlier"
        >earlier output</v-btn>
        <!-- The other direction, and a different file rather than more of this
             one: the launch happened on the host before this session had a log to
             write into. Offered only where that is true (see canShowLaunch), so a
             session served from the host tee — an older image, or one that died
             before its harness drew a byte — has exactly the row it always had. -->
        <v-btn
          v-if="canShowLaunch"
          size="small"
          variant="tonal"
          :prepend-icon="mdiRocketLaunchOutline"
          @click="showLaunch"
        >launch output</v-btn>
        <v-btn
          v-if="showingLaunch"
          size="small"
          variant="tonal"
          :prepend-icon="mdiArrowLeft"
          @click="hideLaunch"
        >back to the session</v-btn>
        <v-btn
          v-if="phase === 'failed'"
          size="small"
          variant="tonal"
          :prepend-icon="mdiRefresh"
          @click="start()"
        >try again</v-btn>

        <!-- Height is this screen's business, not the session's: unlike the fit
             switch below it writes nothing to the PTY, it just gives the emulator
             more room. Growing the box does trigger a refit when fitting is on,
             which is the point — more pixels should mean more rows. -->
        <v-btn-toggle
          :model-value="heightScale"
          density="compact"
          variant="tonal"
          divided
          mandatory
          @update:model-value="setHeightScale"
        >
          <v-btn
            v-for="scale in HEIGHT_SCALES"
            :key="scale"
            :value="scale"
            size="small"
            :aria-label="`terminal height ${scale} times the default`"
          >x{{ scale }}</v-btn>
        </v-btn-toggle>

        <div v-if="showInput" class="d-flex align-center ga-1">
          <v-switch
            :model-value="resizeOptIn"
            :disabled="!canType || !resizeSupported"
            color="primary"
            density="compact"
            hide-details
            label="fit to my screen"
            @update:model-value="setResizeOptIn"
          />
          <!-- The explanation moved off the page and behind this: it is three
               sentences that matter once and then never again. `open-on-click` as
               well as hover, because a tooltip that only answers to a mouse is no
               explanation at all on the device this UI is mostly used from. -->
          <v-tooltip location="top" open-on-click max-width="340">
            <template #activator="{ props: tip }">
              <v-icon
                v-bind="tip"
                :icon="mdiInformationOutline"
                size="small"
                class="text-medium-emphasis"
                aria-label="what fitting the session does"
              />
            </template>
            <span>
              On, because a terminal drawn for a different screen is unreadable on
              this one: fitting sends your size to the session and it lays its
              display out again to match. Turn it off if you are looking over
              someone's shoulder — a terminal's size belongs to the session, so it
              changes for everyone attached, including a terminal on the host.
            </span>
          </v-tooltip>
        </div>

        <!-- In front of the notice because it is a fixed sentence and the notice
             is the unbounded one. Shown only while the session can be typed into
             and the emulator has not got the keyboard: that is precisely when a
             keystroke or a Ctrl+V goes somewhere else, which is otherwise
             indistinguishable from a terminal that drops input. Hidden where the
             pointer is a finger — see the stylesheet: there the composer is the
             way in and the emulator is never focused at all. -->
        <span
          v-if="canType && !focused"
          class="text-body-small text-medium-emphasis focus-hint"
        >
          click the terminal to type or paste into it
        </span>

        <span v-if="notice" class="text-body-small text-medium-emphasis">
          {{ notice }}
        </span>
      </div>

      <v-alert v-if="problem" type="error" density="compact" class="mb-3">
        {{ problem }}
      </v-alert>

      <!-- The log outlives the container, so a session that is gone still has a
           terminal — a read-only one. Saying so beats a prompt that looks alive
           and swallows whatever is typed into it. -->
      <p v-if="phase === 'history'" class="text-body-small text-medium-emphasis mb-3">
        Recorded output, not a live terminal: this session has exited, so there is
        nothing left to type into. Its log is kept after the container goes, which
        is why the history above is still readable.
      </p>

      <p v-if="atOldestChunk" class="text-body-small text-medium-emphasis mb-3">
        Showing the last {{ sizeLabel(TAIL_STEPS[tailStep]) }} — as far back as one
        read goes. The complete log stays on the host.
      </p>

      <!-- What this view is, said while it is on screen: it is the same emulator
           showing a different file, and without a line saying so it reads as the
           session having scrolled back to a start it does not have. -->
      <template v-if="phase === 'launch'">
        <p class="text-body-small text-medium-emphasis mb-3">
          How this session was launched: the image pull, the clone and lmer's own
          lines, printed on the host before the session had a log of its own.
          A separate recording, not earlier scrollback — the two are never spliced
          together, because nothing can tell where one ends inside the other.
          <template v-if="launchTruncated">
            Showing the first {{ sizeLabel(LAUNCH_BYTES) }}: what follows in that
            file is the session's own output, which the live view above shows from
            the session's own log.
          </template>
        </p>
        <p v-if="launchEmpty" class="text-body-small text-medium-emphasis mb-3">
          Nothing was recorded on the host for this session — the file is empty or
          gone. Everything there is to read is in the live view.
        </p>
      </template>

      <template v-if="showInput">
        <!-- The composer is one unit: the keys a phone keyboard does not have,
             and the field that submits a line because xterm's own textarea is
             unreliable on iOS and Android. Both exist for the same reason and
             both are dead weight where that reason does not hold — a real
             keyboard types straight into the emulator (term.onData), and clicking
             the terminal is what gives it focus. So the whole block is shown only
             for a coarse pointer; see the stylesheet for why that is a pointer
             question and not a width one. -->
        <div class="composer">
          <div class="key-pad mb-3">
            <v-btn :disabled="!canType" variant="tonal" @click="sendInput(KEYS.enter)">
              Enter
            </v-btn>
            <v-btn
              :disabled="!canType"
              :icon="mdiArrowUp"
              variant="tonal"
              aria-label="arrow up — the previous command"
              @click="sendInput(KEYS.up)"
            />
            <v-btn
              :disabled="!canType"
              :icon="mdiArrowDown"
              variant="tonal"
              aria-label="arrow down"
              @click="sendInput(KEYS.down)"
            />
            <v-btn :disabled="!canType" variant="tonal" @click="sendInput(KEYS.tab)">
              Tab
            </v-btn>
            <!-- Esc, not Ctrl-C, is what interrupts a turn in these TUIs. -->
            <v-btn :disabled="!canType" variant="tonal" @click="sendInput(KEYS.escape)">
              Esc
            </v-btn>
            <v-btn
              :disabled="!canType"
              color="warning"
              variant="tonal"
              @click="confirmingInterrupt = true"
            >Ctrl-C</v-btn>
          </div>

          <!-- Confirmed on every press, not just a suspiciously fast second one.
               See interrupt(). -->
          <v-dialog v-model="confirmingInterrupt" max-width="420">
            <v-card>
              <v-card-text class="text-body-medium">
                Send Ctrl-C to this session? Two of them in a row is the quit
                chord in most harness TUIs, so the next tap could end the run. To
                stop whatever the session is doing right now, Esc is the key that
                does it.
              </v-card-text>
              <v-card-actions>
                <v-spacer />
                <v-btn variant="tonal" @click="confirmingInterrupt = false">
                  cancel
                </v-btn>
                <v-btn color="warning" variant="tonal" @click="interrupt">
                  send Ctrl-C
                </v-btn>
              </v-card-actions>
            </v-card>
          </v-dialog>

          <v-text-field
            v-model="line"
            :disabled="!canType"
            :append-inner-icon="mdiSend"
            label="type a line"
            hint="sent with Enter, as if typed into the session"
            persistent-hint
            autocapitalize="off"
            autocorrect="off"
            autocomplete="off"
            spellcheck="false"
            @keydown.enter="submitLine"
            @click:append-inner="submitLine"
          />
        </div>

      </template>
    </v-card-text>
  </v-card>
</template>

<style scoped>
/* The one component whose child sizes itself from its container: the fit addon
   reads this box's computed height to decide how many rows the session is told it
   has, so an `auto` height would leave the terminal at its 24x80 default. dvh
   over vh because a phone's browser chrome shrinks the viewport as you scroll,
   and vh does not notice. */
.terminal-host {
  /* Scaled by the x1/x1.5/x2 control. All three bounds have to scale, not just
     the height: max-height was a flat 560px, so on a desktop where 55dvh already
     exceeded it, x2 would have changed nothing at all. */
  --term-scale: 1;
  height: calc(55vh * var(--term-scale));
  height: calc(55dvh * var(--term-scale));
  min-height: calc(220px * var(--term-scale));
  max-height: calc(560px * var(--term-scale));
  padding: 6px;
  overflow: hidden;
  /* The emulator's own ground, not the card's: the same name terminalTheme() hands
     xterm, so the 6px of padding around it is part of the terminal rather than a
     frame of card showing through. */
  background: rgb(var(--v-theme-terminal));
}

/* The composer — the key row and the line field — exists because an on-screen
   keyboard has no Ctrl, Tab, Esc or arrows, and because xterm's own hidden
   textarea is unreliable on iOS and Android. A real keyboard has all of those
   keys and types straight into the emulator, so on that machine the block is
   chrome sitting between the terminal and everything under it.
   The question is therefore "is the pointer a finger", not "is the window
   narrow": a width breakpoint would hide these on a tablet in landscape, which
   needs them, and show them in a small desktop window, which does not.
   `display` is owned here rather than by Vuetify's `d-flex` utility because that
   one is !important and a media query cannot turn it off. */
.composer {
  display: none;
}

@media (pointer: coarse) {
  .composer {
    display: block;
  }

  /* The mirror image of the rule above, and the same question decides it: where
     the pointer is a finger the composer is how a session is typed into, the
     emulator is never given the keyboard, and "click the terminal" would be a
     line of advice that is permanently on screen and permanently wrong. */
  .focus-hint {
    display: none;
  }
}

/* The gap is `ga-2`'s 8px, restated for the same !important reason. */
.key-pad {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
</style>
