<script setup>
// Marking up an image before it is sent (issue 246, slice 2).
//
// The operator asked for "very simply draw shapes on the image — e.g. highlight
// an area or draw an arrow pointing at something. Simple marks, not an editor."
// So this is three tools, one colour, an undo and a done, and it is deliberately
// not: layers, text, colour picking, cropping, or a saved history. Every one of
// those turns a report into a task.
//
// It runs entirely in the browser. What leaves is one flattened PNG, uploaded
// through the same route as any other attachment — so the daemon, the store and
// the agent know nothing about this file having been drawn on, which is what
// keeps slice 1 unchanged.
//
// It is an ordinary centered dialog, the shape every other dialog in this app
// has (AssistantSettings, the three confirm dialogs) — not the full-screen one
// it shipped as first. Marking up a screenshot is work done *about* a
// conversation, and a full-screen surface paints over the conversation being
// reported, so the operator loses sight of what the mark is for (!273 followup).
// The cost is that the scrim and Escape now discard unaccepted marks as well as
// the close button; that is what dismissable means, and *done* is the only path
// that keeps them either way.
//
// Two decisions are worth stating, because both are invisible until a phone is
// in the room:
//
// * The canvas is drawn at the image's own pixel size and *displayed* scaled to
//   fit. Marks are therefore recorded in image coordinates and mapped on the way
//   in (`toNatural`), or a mark would land where the finger was on a 390px-wide
//   phone rather than where it looked like it landed on a 3000px screenshot.
// * `touch-action: none` on the canvas. Without it a drag on a touchscreen
//   scrolls the dialog instead of drawing, which is the whole feature not
//   working on the device this is for.
import { computed, ref, watch } from 'vue'
import {
  mdiArrowTopRight,
  mdiCheck,
  mdiClose,
  mdiDraw,
  mdiRectangleOutline,
  mdiUndoVariant,
} from '@mdi/js'

const props = defineProps({
  // Whether the dialog is up. `v-model` from the composer.
  modelValue: { type: Boolean, default: false },
  // The image to mark up. Not read until the dialog opens, and never mutated:
  // accepting produces a *new* file and the original is the caller's to drop.
  file: { type: Object, default: null },
})

const emit = defineEmits(['update:modelValue', 'marked'])

// The three marks. One colour and one width for all of them: choosing either is
// a decision the operator did not ask to make.
const TOOLS = [
  { id: 'arrow', icon: mdiArrowTopRight, label: 'arrow' },
  { id: 'box', icon: mdiRectangleOutline, label: 'box' },
  { id: 'free', icon: mdiDraw, label: 'freehand' },
]

// An arrow head as a share of the shaft, bounded so a short arrow still reads as
// one and a long one does not grow a flag.
const HEAD_SHARE = 0.22
const HEAD_MIN = 12
const HEAD_MAX = 48

const tool = ref('arrow')
const marks = ref([])
const canvas = ref(null)
const problem = ref(null)
// The decoded image, kept out of the reactive graph: it is a bitmap, nothing
// renders it, and making it reactive would have Vue walk it.
let image = null
let drawing = null

const ready = computed(() => !!props.file)
const canAccept = computed(() => marks.value.length > 0)

// Read from the theme rather than written here, so a mark is the same red the
// rest of the app uses for "look at this" and the palette stays in one place
// (src/main.js). Vuetify emits its colours as bare `r,g,b` triples on the root
// element. The fallback is a *named* colour rather than a second literal
// palette entry: if the token is missing the mark still has to be visible, and
// which red it is then is not a decision anyone needs to make.
function markColour() {
  const token = getComputedStyle(document.documentElement)
    .getPropertyValue('--v-theme-error').trim()
  return token ? `rgb(${token})` : 'red'
}

// Proportional to the image, because a 3-pixel line on a 4K screenshot is
// invisible and a 20-pixel one on a phone screenshot covers what it points at.
function strokeWidth() {
  const longest = Math.max(image?.naturalWidth || 0, image?.naturalHeight || 0)
  return Math.max(3, Math.round(longest / 300))
}

// Where a pointer is, in the image's own pixels. Exported to the test through
// the source rather than through a module, and pure so it can be executed
// there: this mapping is where a "why did my arrow land two centimetres to the
// left" bug lives, and it cannot be seen by reading markup.
function toNatural(clientX, clientY, rect, natural) {
  // An unreachable case, asserted rather than reasoned about. A 0x0 element is
  // not hit-testable, so no pointer event can arrive while the canvas has no
  // layout — and if one ever did, the arithmetic would divide by zero and the
  // clamp below would turn the resulting Infinity into the *far corner* (or pass
  // NaN straight through). The first version of this comment claimed the
  // unguarded code put every mark at the origin, which is not what it does
  // (!273 review); the guard stays because a mark at a coordinate nobody chose
  // is worth one branch, and `{0, 0}` is as arbitrary as any other answer here.
  if (!rect.width || !rect.height) return { x: 0, y: 0 }
  const x = ((clientX - rect.left) / rect.width) * natural.width
  const y = ((clientY - rect.top) / rect.height) * natural.height
  // Clamped, so a drag that leaves the canvas ends at its edge instead of
  // drawing a mark nobody can see into the image's margins.
  return {
    x: Math.min(Math.max(x, 0), natural.width),
    y: Math.min(Math.max(y, 0), natural.height),
  }
}

function pointFrom(event) {
  const element = canvas.value
  return toNatural(
    event.clientX, event.clientY, element.getBoundingClientRect(),
    { width: element.width, height: element.height },
  )
}

function drawMark(context, mark) {
  context.strokeStyle = markColour()
  context.lineWidth = mark.width
  context.lineCap = 'round'
  context.lineJoin = 'round'
  if (mark.tool === 'box') {
    context.strokeRect(
      mark.points[0].x, mark.points[0].y,
      mark.points[1].x - mark.points[0].x,
      mark.points[1].y - mark.points[0].y,
    )
    return
  }
  if (mark.tool === 'arrow') {
    const [from, to] = mark.points
    const angle = Math.atan2(to.y - from.y, to.x - from.x)
    const shaft = Math.hypot(to.x - from.x, to.y - from.y)
    const head = Math.min(Math.max(shaft * HEAD_SHARE, HEAD_MIN), HEAD_MAX)
    context.beginPath()
    context.moveTo(from.x, from.y)
    context.lineTo(to.x, to.y)
    for (const spread of [Math.PI / 7, -Math.PI / 7]) {
      context.moveTo(to.x, to.y)
      context.lineTo(
        to.x - head * Math.cos(angle - spread),
        to.y - head * Math.sin(angle - spread),
      )
    }
    context.stroke()
    return
  }
  context.beginPath()
  mark.points.forEach((point, index) => {
    if (index === 0) context.moveTo(point.x, point.y)
    else context.lineTo(point.x, point.y)
  })
  context.stroke()
}

// Every mark, every time, over a fresh copy of the image. The alternative —
// drawing each stroke once onto a canvas that keeps them — makes undo
// impossible without a second buffer, and undo is the one editing affordance
// simple marks genuinely need (a mis-aimed arrow is otherwise a re-attach).
function redraw() {
  const element = canvas.value
  if (!element || !image) return
  const context = element.getContext('2d')
  context.clearRect(0, 0, element.width, element.height)
  context.drawImage(image, 0, 0)
  for (const mark of marks.value) drawMark(context, mark)
  if (drawing) drawMark(context, drawing)
}

function onDown(event) {
  // One pointer owns the stroke. `drawing` is a single slot, so on a touchscreen
  // a second contact — a resting thumb, a palm on a phone held one-handed —
  // used to overwrite it from that finger's position, and the first finger's
  // moves then extended the new stroke until whichever came up first committed
  // the mixture. Pointer capture does not help: it is per pointer, and the
  // second one was never captured (!273 review).
  if (!image || drawing) return
  // The pointer is captured so a drag that leaves the canvas still finishes
  // here: without it the `up` lands on whatever is underneath and the mark is
  // left half-drawn and un-undoable.
  canvas.value.setPointerCapture?.(event.pointerId)
  const point = pointFrom(event)
  drawing = {
    pointerId: event.pointerId,
    tool: tool.value,
    width: strokeWidth(),
    points: [point, point],
  }
  redraw()
}

function onMove(event) {
  if (!owns(event)) return
  const point = pointFrom(event)
  if (drawing.tool === 'free') drawing.points.push(point)
  else drawing.points[1] = point
  redraw()
}

// Whether this event belongs to the stroke in progress. Every handler asks,
// because a second finger's move or release is not this stroke's.
function owns(event) {
  return !!drawing && event.pointerId === drawing.pointerId
}

function onUp(event) {
  if (!owns(event)) return
  const mark = drawing
  drawing = null
  // A tap is not a mark: without this every stray touch on the image leaves a
  // dot, and the operator cannot tell an accidental one from a deliberate full
  // stop. Two things decide it, and each was wrong once:
  //
  // * *What* is measured differs by tool. First-to-last distance is the right
  //   question for an arrow or a box and the wrong one for freehand — a stroke
  //   that returns to where it started measures zero however long it is, so
  //   circling something, the operator's own example, could never be kept.
  // * *Against what* is a question of units. The threshold was `mark.width`,
  //   which lives in image pixels while the finger works in screen pixels: on a
  //   3000px screenshot shown 342px wide it came to 1.14 CSS px, so a tap that
  //   wobbled cleared it and a still one did not (!273 review iteration 2). The
  //   bound is now a distance in CSS pixels, converted through the same scale the
  //   pointer mapping already computes.
  if (extent(mark) >= TAP_SLOP_CSS_PX * imagePerCssPixel()) {
    marks.value = [...marks.value, mark]
  }
  redraw()
}

//: How far a contact has to travel before it is a mark rather than a tap, in the
//: unit the finger works in. Around a platform touch slop: below this, a contact
//: is a tap however much it wobbles, and the operator has to be able to rest a
//: thumb on a phone screen without leaving a dot.
const TAP_SLOP_CSS_PX = 8

// Image pixels per CSS pixel — `toNatural`'s scale, in the direction a threshold
// needs it. A bound expressed in what the finger did has to be converted into the
// space the marks are recorded in, or it means something different on every
// image. Falls back to 1 for a canvas with no layout, which is the unreachable
// case `toNatural` documents.
function imagePerCssPixel() {
  const element = canvas.value
  const rect = element?.getBoundingClientRect()
  return rect?.width ? element.width / rect.width : 1
}

// How far a mark *reaches*: the greatest distance any of its points gets from
// where it started. One measure for every tool, which is the third answer here
// and the first that holds for all four cases at once:
//
// * first-to-last distance (the original) discards a freehand stroke that
//   returns to where it began, so circling something could never be kept;
// * accumulated path length fixes that and brings its own hole — length is
//   cumulative, so a tap that wobbles enough times clears any threshold a still
//   tap cannot. Measured at the scale this feature is aimed at, a 2 CSS px
//   wobble over four moves reaches exactly the bound;
// * reach has neither property. A closed circle reaches its own diameter, a
//   jittery tap reaches a couple of pixels however long it jitters, and for an
//   arrow or a box — two points — it *is* first-to-last distance, so the tools
//   need no branch between them.
function extent(mark) {
  const [origin] = mark.points
  return mark.points.reduce(
    (furthest, point) => Math.max(
      furthest, Math.hypot(point.x - origin.x, point.y - origin.y),
    ),
    0,
  )
}

// A gesture the OS took away — a system edge swipe, a call arriving — is
// abandoned rather than committed. It used to share `onUp`, which kept whatever
// had been drawn up to the moment the gesture was cancelled (!273 review).
function onCancel(event) {
  if (!owns(event)) return
  drawing = null
  redraw()
}

function undo() {
  marks.value = marks.value.slice(0, -1)
  redraw()
}

// The image is decoded from the file the composer already holds, so nothing is
// uploaded to be marked up and nothing is fetched.
function load() {
  problem.value = null
  marks.value = []
  drawing = null
  if (!props.file) return
  const url = URL.createObjectURL(props.file)
  const bitmap = new Image()
  bitmap.onload = () => {
    URL.revokeObjectURL(url)
    image = bitmap
    const element = canvas.value
    if (!element) return
    element.width = bitmap.naturalWidth
    element.height = bitmap.naturalHeight
    redraw()
  }
  bitmap.onerror = () => {
    URL.revokeObjectURL(url)
    // Said rather than silently offering an empty canvas: the file is still
    // attached and still sends, it just cannot be drawn on here.
    problem.value = 'that image could not be opened for marking up'
  }
  bitmap.src = url
}

function close() {
  emit('update:modelValue', false)
}

// The type the flattened copy comes back as: **what went in, when what went in
// was lossy**. PNG is right for a screenshot with lines drawn on it and wrong for
// a photograph — a 4032x3024 camera frame measured 3.2 MiB as JPEG and 22.7 MiB
// as PNG, so marking up a photo of a whiteboard could push a file that was
// sendable over the platform's 8 MiB default and leave the operator with no way
// forward but to unstage it and lose the marks (!273 review). The camera roll is
// one of the three input paths this feature was asked for, so that is the
// ordinary case rather than an exotic one.
//
// Deliberately *not* a size check against the daemon's cap: this client holds no
// copy of that policy (see Chat.vue's `ACCEPT`), and matching the source's format
// removes the cause rather than reporting the symptom. A marked PNG still grows a
// little — marks are entropy — but stays in the same range as the file it came
// from.
const OUTPUT_TYPE = computed(
  () => (props.file?.type === 'image/jpeg' ? 'image/jpeg' : 'image/png'),
)

//: Quality for a re-encoded JPEG. High enough that a second generation is not
//: visible next to the first at a glance, low enough that the re-encode does not
//: grow the file it came from.
const JPEG_QUALITY = 0.92

function accept() {
  const element = canvas.value
  if (!element) return
  const type = OUTPUT_TYPE.value
  element.toBlob((blob) => {
    if (!blob) {
      problem.value = 'the marked-up image could not be saved'
      return
    }
    emit('marked', new File([blob], markedName(props.file?.name, type), { type }))
    close()
  }, type, type === 'image/jpeg' ? JPEG_QUALITY : undefined)
}

// The original name with a suffix, so an operator who sends both can tell them
// apart in the chat and the agent can too. The extension follows the *output*
// type rather than the input's, since those can differ (a `.jpeg` input keeps
// JPEG and gets `.jpg`; anything else is flattened to PNG) — a name that lied
// about the bytes would be the one thing this end must not produce, given the
// daemon names the stored file from it.
function markedName(name, type = 'image/png') {
  const text = String(name || 'image')
  const dot = text.lastIndexOf('.')
  const extension = type === 'image/jpeg' ? '.jpg' : '.png'
  return `${dot > 0 ? text.slice(0, dot) : text}-marked${extension}`
}

// Loaded when the dialog opens rather than when the file changes: the composer
// keeps the tray's files alive for as long as they are staged, and decoding one
// nobody is looking at costs memory on a phone for nothing.
//
// `immediate`, and that word is the whole feature working. The composer mounts
// this component under `v-if="marking !== null"` with `:model-value="true"`, so
// `modelValue` is already true at setup and a plain watcher never fires — it
// runs on *changes*, and there is no change to observe. Without it `image`
// stayed null, `redraw()` returned at its guard, no drag could record a mark and
// *done* was disabled forever: the dialog opened on an undrawn 300x150 canvas
// (!273 review). It stays a watcher rather than becoming `onMounted(load)` so a
// caller that keeps the component mounted and toggles `v-model` still gets a
// fresh image on each open; `if (open)` is what makes the immediate call a no-op
// for one mounted closed.
watch(() => props.modelValue, (open) => {
  if (open) load()
}, { immediate: true })
</script>

<template>
  <v-dialog
    :model-value="modelValue"
    max-width="720"
    scrollable
    @update:model-value="emit('update:modelValue', $event)"
  >
    <v-card>
      <v-toolbar density="compact">
        <v-toolbar-title class="text-body-medium">mark up the image</v-toolbar-title>
        <v-btn
          :icon="mdiClose"
          variant="text"
          aria-label="cancel marking up"
          @click="close"
        />
      </v-toolbar>

      <v-card-text>
        <v-alert v-if="problem" type="warning" density="compact" class="mb-3">
          {{ problem }}
        </v-alert>

        <p class="text-body-small text-medium-emphasis mb-2">
          Drag on the image to draw. The marks go on a copy — what you attached is
          untouched until you tap done.
        </p>

        <!-- The canvas is the image at its own size, shown scaled to fit. See the
             header on why marks are recorded in image coordinates, and on
             `touch-action`. -->
        <div class="sketch">
          <canvas
            v-if="ready"
            ref="canvas"
            class="sketch-canvas"
            @pointerdown="onDown"
            @pointermove="onMove"
            @pointerup="onUp"
            @pointercancel="onCancel"
          />
        </div>
      </v-card-text>

      <!-- One row, thumb-sized, on the bottom edge — the app's rule for anything
           an operator taps one-handed (style.css, `.send-row`). The tool buttons
           carry no label because three icons in a toggle read faster than three
           words, and each keeps an aria-label for the same reason the send
           control does. -->
      <v-card-actions class="sketch-tools">
        <v-btn-toggle v-model="tool" density="comfortable" mandatory>
          <v-btn
            v-for="entry in TOOLS"
            :key="entry.id"
            :value="entry.id"
            :icon="entry.icon"
            :aria-label="`draw a ${entry.label}`"
            size="large"
          />
        </v-btn-toggle>
        <v-spacer />
        <v-btn
          :prepend-icon="mdiUndoVariant"
          :disabled="!marks.length"
          variant="tonal"
          size="large"
          aria-label="undo the last mark"
          @click="undo"
        >undo</v-btn>
        <!-- Disabled until there is a mark: an unmarked image is the one already
             in the tray, and re-encoding it would only make it bigger. Cancel is
             the way out, and it is the toolbar's close. -->
        <v-btn
          :prepend-icon="mdiCheck"
          :disabled="!canAccept"
          color="primary"
          variant="tonal"
          size="large"
          aria-label="use the marked-up image"
          @click="accept"
        >done</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<style scoped>
/* The image sits in the middle of whatever room the dialog has, and never wider
   than it: a screenshot is routinely wider than a phone, and a canvas has an
   intrinsic size, so without the bound it would push the dialog sideways. */
.sketch {
  display: flex;
  justify-content: center;
}

/* `touch-action: none` is the feature working on a touchscreen — see the header.
   `height: auto` keeps the aspect ratio while `max-width` scales it down, and
   the cursor says the surface is for drawing rather than for dragging the
   image out of the page.

   The viewport bound is what leaves the page visible around a centered dialog:
   the card is this plus a toolbar, a line of help and the tool row, and the
   dialog itself is capped below the viewport. `dvh` rather than `vh` because a
   phone's browser chrome moves; both are written because `dvh` is the newer of
   the two. Overflow past it is the card's to scroll (`scrollable`), so the
   number decides how much page shows rather than whether the controls are
   reachable. */
.sketch-canvas {
  max-width: 100%;
  max-height: 60vh;
  max-height: 60dvh;
  height: auto;
  touch-action: none;
  cursor: crosshair;
}

/* Wraps rather than crushing the buttons: three tools, undo and done do not fit
   one phone row, and a squeezed row is what the one-handed rule exists to
   prevent. */
.sketch-tools {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
</style>
