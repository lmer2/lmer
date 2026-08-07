<script setup>
import { computed } from 'vue'
import {
  mdiAccountMultipleOutline, mdiRobotOutline, mdiChevronRight,
  mdiClipboardTextOutline, mdiEyeOffOutline, mdiLinkVariant, mdiOpenInNew,
  mdiProgressClock, mdiReplyOutline, mdiRobot, mdiSourceMerge, mdiSourcePull,
  mdiSourceRepository, mdiTicketOutline, mdiTuneVariant,
} from '@mdi/js'
import {
  ago, attentionLabel, checkinLabel, driverLabel, duration, ledgerSummary,
  portUrl, shortTarget, stateMeta, targetLink, targetRef, toneColor,
} from '../format.js'

// Which kind of thing the run is against. The word in the chip says it too, and
// that is the point: main.js names colour-carrying-a-signal-alone as this UI's
// weak spot, and the same argument applies to a word — an MR run is not an issue
// run, and the difference should survive a glance that does not read.
const RESOURCE_ICONS = {
  mr: mdiSourceMerge,
  pr: mdiSourcePull,
  issue: mdiTicketOutline,
}

//: Run states a row may offer "forget" on: the ends of a run, where nothing more
//: will happen and there is nothing left to wait for. Everything else is a pause
//: rather than an ending — `dormant` is a run between sessions and `parked`,
//: `held`, `waiting_on_you` and `yielded` are all runs that are waiting for
//: something, most often for the operator. `crashed` is deliberately absent too:
//: the leftover session entry *is* the record of the crash, and clearing those is
//: the fleet's own verb (the prune card below the list).
//: Keep in step with lmer_platform.inventory.RUN_STATES.
const ENDED_STATES = ['complete', 'failed']

const props = defineProps({
  run: { type: Object, required: true },
  now: { type: Number, default: () => Date.now() },
})

defineEmits(['open', 'forget', 'open-chat'])

const meta = computed(() => stateMeta(props.run.state))
const ledger = computed(() => ledgerSummary(props.run.ledger))

// What the run says about itself, which is not what the chip says: the state in
// `meta` is *derived*, and liveness outranks the committed record by design (spec
// D24) — so a live session reads "running" while its own state.yaml can say it is
// paused, or say nothing yet. A row carrying only the derived word makes that
// disagreement invisible, and the disagreement is the thing an operator has to be
// able to see from the fleet.
//
// The status and not the phase, since the operator asked for the phase to be shown
// plainly (2026-07-29): a phase is where the work has got to, which is what the row
// is *for*, so it went up to the identity line. What stays here is the one value
// that competes with the state chip and therefore has to stay quieter than it.
//
// Labelled, because "in-progress" under a chip reading "running" is two
// state-shaped words with nothing saying which is whose. Null when the run has
// recorded nothing — every run in its first seconds — so the element is absent
// rather than an empty slot held open on every row.
const committed = computed(
  () => (props.run.status ? `status: ${props.run.status}` : null),
)

// How long the live session's harness has been quiet, which is the one thing the
// state chip cannot say: the chip reads `running` from the moment a session starts
// until the moment it exits (spec D24 — run state moves at the *end*), so a run
// that finished its work and is sitting at its prompt looks exactly like one that
// is working. "idle 22m" is what tells them apart, and it is the difference
// between winding a run down and interrupting it.
//
// The daemon's own seconds rather than an age computed here from a timestamp: the
// number was measured by one monotonic clock in the process that saw the output
// (lmer_cli.supervisor), so nothing about *this* device's clock can turn a busy
// session into an idle-looking one — the exposure `ago()` accepts for a commit
// time it has no other form of. The cost is that it is as fresh as the last poll,
// which is invisible at the resolutions `duration()` renders.
//
// Absent renders nothing, and absent is ordinary: a run with no live session, a
// session the platform cannot reach, and one whose image predates the fact all
// report nothing rather than an idle of zero — which would read as "just did
// something", the opposite of what is known.
const idle = computed(() => {
  const label = duration(props.run.session?.activity?.idle_seconds)
  return label ? `idle ${label}` : null
})

// When the orchestrator last looked at this run (issue 244). Beside the other time
// facts rather than up by the chip: it says nothing about the run — which may be
// working perfectly — only about the *orchestrator's* attention, so that "is anyone
// driving this?" need not be a question put to uber lmer itself.
//
// Unpainted, like every other addition here: colour is urgency, and a quiet run is
// not more urgent than a crashed one. The *word* carries the signal.
const checkin = computed(() => checkinLabel(props.run.checkin, props.now))

// The platform's own session, read once so that everything the treatment touches —
// the card's border and the badge — is the same fact, and no other row can pay for
// it. It is a *kind* of row rather than a state of one, which is why the marking
// stays off the tone ramp: it says what this row IS, and every colour in the ramp
// says how urgent a run is.
//
// The marking is an edge and no longer a ground. A tinted row was the previous pass
// and the operator rejected it — "i don't think solid background for the uber lmer
// row is the way ... give it an orange border in the main list view" — so the border
// carries the identity and the row's own words are drawn on the same surface as
// every other row's (see the scoped rule below).
const orchestrator = computed(() => !!props.run.orchestrator)

// A crash and a question both need a human, but they are not the same news, so
// the edge stripe and the note keep them apart at a glance.
const tone = computed(() => {
  if (!props.run.attention) return null
  return props.run.attention.reason === 'crashed' ? 'bad' : 'attention'
})

// The issue/PR/MR the run is against, when the target names one. Null is
// ordinary rather than exceptional (a bare repo, a compare range, a taskdef
// whose target is prose), and the link then falls back to the shortened target
// so the row never loses its way to what it is working on.
const resource = computed(() => targetRef(props.run.target))
const resourceIcon = computed(
  () => (resource.value ? RESOURCE_ICONS[resource.value.kind] : mdiLinkVariant),
)

// Where the target chip goes, and null when there is nowhere to go: the gate is
// format.js's, so the row and the detail header cannot disagree about which
// targets are links. Unlinked is ordinary — a branch name, prose, the
// orchestrator's own `fleet` — and the chip then says the same words without
// being a relative link that 404s against the page it is on.
const targetHref = computed(() => targetLink(props.run.target))

// How the session was launched, which the platform knows only from the spawn
// entry: a preset, a fan-out selection, and what is driving the harness. Each is
// absent on an ordinary run and each is rendered only when it is not — a chip
// that reads "no preset" is noise on every row, and a blank one where a model
// belongs is worse than saying nothing (see driverLabel).
const driver = computed(() => driverLabel(props.run))
const launched = computed(
  () => !!(props.run.preset || props.run.agents || driver.value),
)

// A question is the one attention reason with something to *do* from this row, so
// it gets its own way through to the answer box (T19) rather than only "enter".
// It emits `open` like the enter button: the parent owns navigation, and the
// detail view leads with the answer box — so one tap lands on it either way. What
// the extra button buys is that the row says answering is possible at all.
//
// Two flavours, and the label is the only place a row can say which: answering a
// stopped run starts a container, while replying to a live one drops a file into
// a session that is already waiting. Same button, different promise (T23).
const answerable = computed(
  () => props.run.attention?.reason === 'question'
    || props.run.attention?.reason === 'live_question',
)
const answerLabel = computed(
  () => (props.run.attention?.reason === 'live_question' ? 'reply' : 'answer'),
)

// Whether this row may offer to stop tracking the run (T101). Two conditions, and
// the second is the row's own reading rather than a second definition of it: the
// state is derived with liveness outranking the committed record (spec D24), so a
// live session reads `running` however finished its state.yaml says it is — which
// already keeps a running container out of ENDED_STATES. `live` is tested beside
// it anyway, because the row that must never offer this is the one with a session
// behind it: winding a session down is what that case has, and a one-tap forget
// there would drop the platform's own handle on a container still doing work.
const forgettable = computed(
  () => !props.run.live && ENDED_STATES.includes(props.run.state),
)
</script>

<template>
  <!-- Two independent markings, and they are independent on purpose: the stripe
       down the inline-start edge is why a human is needed, the border around the
       card is what kind of row this is. The orchestrator needs a human like
       anything else does, so the two have to be able to be true at once without
       overwriting each other — which is a constraint the scoped rule below has to
       respect edge by edge. -->
  <v-card
    class="mb-3"
    :class="[
      tone ? ['tone-edge', `tone-edge-${tone}`] : null,
      orchestrator ? 'orchestrator-row' : null,
    ]"
  >
    <v-card-text>
      <!-- The signal, and it stays on a line of its own: the state word is the
           long one in this row ("detached — not being recorded") and sharing a
           flex line with a project path is how it ends up wrapped away from the
           name it belongs to.
           Tonal while every information chip below it is outlined, and that gap is
           the point: the tint is now the one filled thing in the row, so the
           verdict is what the eye lands on and the facts are read after it. An
           outlined state chip would be another coloured word in a border among
           four of them — the ramp survives the glance by being a block of colour,
           which is also why the contrast complaint that moved the others (see the
           target chip) does not reach this one: the state word is the row's
           loudest text either way. -->
      <div class="d-flex flex-wrap align-center ga-2">
        <v-chip :color="toneColor(meta.tone)" variant="tonal">
          {{ meta.label }}
        </v-chip>
        <!-- The platform's own session, marked beside the state and before the
             name: this row is the orchestrator, not one of the orchestrated, and
             a fleet list where it reads as an ordinary run is a list an operator
             can stop or forget the wrong thing from. The word is what says it —
             colour on its own is not a signal in this app.
             Unpainted, and that is the second half of the operator's call on the
             tint: the card's border already marks this row in the accent, and a
             coloured badge inside a coloured border is two accent systems arguing
             on one row. So the chip is an ordinary chip and the edge carries the
             identity. -->
        <v-chip
          v-if="orchestrator"
          :prepend-icon="mdiRobot"
          size="small"
          title="the platform's own orchestrating session, not one of the runs"
          variant="tonal"
        >uber lmer</v-chip>
        <!-- The title this orchestrator gave the run, where the run is named, and
             the label only when there is none: a title nobody sees outside its own
             tab is not something you can identify a run by, which is the entire
             point of having one. Interpolated and not rendered — the daemon has
             already collapsed it to one line, and a row cannot host block markup
             anyway (RunMeta.vue makes the same call about the same string).
             The name a title replaces stays one hover away rather than taking a
             second line here; the detail header is where both are always visible.
             `scroll-x` because this is agent-written text bounded at 120
             characters: it wraps if it can, and an unbreakable one scrolls inside
             its own box instead of taking a phone's whole width sideways. It is
             also last on the flex line, so a long one wraps under the state chip
             and never pushes it off. -->
        <span
          class="text-body-large font-weight-medium scroll-x"
          :title="run.label"
        >{{ run.title || run.label }}</span>
      </div>

      <!-- What the run IS: the task, the repo, the ticket. Prominent by position
           and by full emphasis, and pointedly not by colour — the tone ramp in
           main.js is this view's signalling system, and identity painted in the
           brand accent would compete with the one thing the row exists to say.
           So this line carries icons and words, and no colour at all. -->
      <div class="d-flex flex-wrap align-center ga-3 mt-2">
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
        <!-- Where the work has got to (the operator: "id also like the listings to
             show the run phase"). Up here rather than in the dimmed line it used to
             share with the committed status: it is a fact about the run itself, like
             the taskdef and the repo, and buried among five dimmed facts it was
             there without being readable. Taskdef-defined free text, so it gets the
             icon and the tooltip this line names everything with instead of a chip
             of its own — and no colour, because the tone ramp is urgency and a phase
             is not. Absent renders nothing, which is most runs. -->
        <span v-if="run.phase" class="d-inline-flex align-center">
          <v-icon
            :icon="mdiProgressClock"
            size="small"
            class="me-1"
            title="phase"
          />{{ run.phase }}
        </span>
        <!-- The number, not the URL: a run gets talked about as "the MR" plus
             its number, and that fits a row at arm's length where the old
             host/…/path chip did not. Still the link it always was *when there is
             somewhere to go* (targetHref) — a target that is not an absolute URL
             would otherwise be a relative link resolved against this page — and
             the append icon is what says the tap leaves the app, so it goes with
             the link rather than sitting on a chip that is only text.
             Outlined, which for chips overrides the variant swept out of the rest
             of the app: a tonal chip is a low-contrast wash behind mid-emphasis
             text, worst on a dark card, and these are read at arm's length — "the
             chips (questions answers, port links) are quite hard to read, lets try
             variant outlined" (the operator, live). It holds for every information
             chip in this row; the state chip above and the uber lmer badge are
             argued separately, and every v-btn here stays tonal. -->
        <v-chip
          v-if="run.target"
          :href="targetHref"
          target="_blank"
          rel="noopener"
          :prepend-icon="resourceIcon"
          :append-icon="targetHref ? mdiOpenInNew : undefined"
          :title="run.target"
          variant="outlined"
        >{{ resource ? resource.label : shortTarget(run.target) }}</v-chip>
      </div>

      <!-- The run's own words about itself, and deliberately the quietest thing in
           the row: small, dimmed, and below both the state chip and the identity.
           Subordinate is the whole design — the chip is the platform's verdict and
           has to keep winning the glance, while this is the record that verdict was
           derived from, there to be read once the chip has made you look. Painting
           it, sizing it up, or giving it a chip of its own would put a second
           state-shaped thing in the row for the eye to arbitrate between. -->
      <div class="d-flex flex-wrap ga-3 text-body-small text-medium-emphasis mt-2">
        <span
          v-if="committed"
          title="what the run records about itself; the chip above is what the platform derived"
        >{{ committed }}</span>
        <span v-if="ledger">{{ ledger }}</span>
        <!-- Beside the run's age rather than up by the chip: it is a fact the
             chip's verdict does not contain, not a competing verdict, and the two
             time facts belong to each other — one is when the run last committed
             anything, the other is when its session last drew anything. The
             tooltip is the wall-clock moment the container dated it to, the same
             way the age's tooltip is the timestamp behind it. -->
        <span
          v-if="idle"
          :title="run.session?.activity?.last_output_at || ''"
        >{{ idle }}</span>
        <!-- Lifted by emphasis rather than colour when the run has gone
             unchecked: findable while scanning, without competing with the
             attention stripe. -->
        <span
          v-if="checkin"
          :class="run.checkin?.stale ? 'text-high-emphasis' : null"
          :title="run.checkin?.checked_at
            ? `the orchestrator last read this run at ${run.checkin.checked_at}`
            : 'nothing has read this run yet'"
        >{{ checkin }}</span>
        <span :title="run.updated || ''">{{ ago(run.updated, now) }}</span>
      </div>

      <!-- Why a row is at the top of the fleet, and therefore never cut away: the
           label below says what the run needs and is bounded by nothing (the note
           under it is — see the clamp). It stays above everything that was added
           later: what a run needs is read before how it was launched.
           It is also the one piece of agent prose in this row, and it is
           deliberately NOT rendered (T46). The renderer is a chunk of its own so
           that the landing screen does not pay for it (T42), and the landing
           screen is this list: a fleet of thirty rows would fetch markdown-it and
           DOMPurify to italicise a clause, on the first paint, on a phone. What
           the row would gain is small — the note is one dimmed sentence the
           daemon wrote around what the run said, and a row cannot host a block
           element anyway — and it is not lost: the detail header renders the same
           note the moment you enter the run, which is one tap away and where the
           chunk is already worth fetching. Vue interpolation escapes, so the
           asterisks are shown rather than obeyed. -->
      <v-alert
        v-if="run.attention"
        :type="tone === 'bad' ? 'error' : 'warning'"
        density="compact"
        class="mt-3"
      >
        <strong>{{ attentionLabel(run.attention.reason) }}</strong>
        <!-- Bounded to a couple of lines, at the operator's reading of a live
             fleet: a session waiting on a reply puts its whole question here, and
             a row that is nine lines of one agent's prose pushes every other run
             off the screen — "given that i cannot act on it from there anyway,
             lets truncate that to something sensible". A CSS clamp rather than a
             cut string, so the text in the DOM is still the whole note (the
             search over this list reads it) and no character count has to be
             invented; the full note is the hover title here and is rendered a tap
             later in the detail header, which is where it can be acted on.
             The label above is outside the clamp and stays whole: what the run
             needs is the one thing in this row that must never be cut. -->
        <div
          v-if="run.attention.note"
          class="attention-note"
          :title="run.attention.note"
        >{{ run.attention.note }}</div>
      </v-alert>

      <!-- How it was launched. Colourless on purpose and last but for the
           buttons: this is what you want once you have decided to look at a run,
           never what makes you look. The whole line is absent on a run that
           recorded none of it, which is most of them. -->
      <div v-if="launched" class="d-flex flex-wrap ga-2 mt-3">
        <v-chip
          v-if="run.preset"
          :prepend-icon="mdiTuneVariant"
          title="preset"
          variant="outlined"
        >{{ run.preset }}</v-chip>
        <v-chip
          v-if="run.agents"
          :prepend-icon="mdiAccountMultipleOutline"
          title="agents this session may fan work out to"
          variant="outlined"
        >{{ run.agents }}</v-chip>
        <v-chip
          v-if="driver"
          :prepend-icon="mdiRobotOutline"
          title="harness · model driving this session"
          variant="outlined"
        >{{ driver }}</v-chip>
      </div>

      <div class="d-flex flex-wrap align-center ga-2 mt-3">
        <!-- Ports first: for a run that built something to look at, opening it is
             the point of the row. They keep the accent because they are the one
             thing here that is genuinely "go and see this". -->
        <v-chip
          v-for="port in run.ports"
          :key="port.host"
          :href="portUrl(port)"
          target="_blank"
          rel="noopener"
          :append-icon="mdiOpenInNew"
          color="primary"
          variant="outlined"
        >:{{ port.host }}</v-chip>
        <!-- The way into the drawer, from the row that IS the drawer's other end.
             The app bar's robot toggle exists on every view, but nothing on the
             fleet said the conversation existed (the operator: "i just want it to
             be more obvious that it exists") — and the one row an operator studies
             when wondering about uber lmer is this one. The parent owns the
             drawer, so this only emits, like `open`. -->
        <v-btn
          v-if="orchestrator"
          variant="tonal"
          size="small"
          color="primary"
          :prepend-icon="mdiRobot"
          aria-label="talk to uber lmer"
          @click="$emit('open-chat')"
        >chat</v-btn>
        <v-btn
          v-if="answerable"
          variant="tonal"
          size="small"
          color="warning"
          :prepend-icon="mdiReplyOutline"
          @click="$emit('open', run)"
        >{{ answerLabel }}</v-btn>
        <v-btn
          variant="tonal"
          size="small"
          :append-icon="mdiChevronRight"
          @click="$emit('open', run)"
        >enter</v-btn>
        <!-- Only on a run that has ended, and last in the row: it is the way to
             get a finished run out of the list, never the way into one. Colourless
             like the rest of this line, and pointedly quieter than the same verb in
             the exit tab — one tap here is undoable for a few seconds (the shell
             owns that window), and nothing about the run is destroyed either way.
             The shell does the forgetting: a row that called the API itself would
             have to remove itself from a list it does not own. -->
        <v-btn
          v-if="forgettable"
          variant="tonal"
          size="small"
          :prepend-icon="mdiEyeOffOutline"
          title="stop tracking this run here; its run dir and work-repo state stay, and adopting it brings it back"
          @click="$emit('forget', run)"
        >forget</v-btn>
      </div>
    </v-card-text>
  </v-card>
</template>

<style scoped>
/* The one row in the list that is not a run, marked by a border in the brand accent.
   It was a tinted row for one pass, and the operator's next one ended that: "i don't
   think solid background for the uber lmer row is the way -- lets instead do the
   following - give it an orange border in the main list view". A ground repaints
   everything the row says in order to say one thing about the row, and with a
   coloured badge on top of it the eye had two accent systems to arbitrate between.
   An edge says the same thing and leaves the contents alone — which is also why the
   cyan the tint needed no longer exists as a theme key at all (main.js).
   The colour is the theme's `primary`, so the scheme switcher repaints it, and this
   is the one place in the row where the accent does not mean "go and see this": a
   border is not a control, and the word in the badge says which kind of row it is.
   2px because a v-card draws no border at all by default (border-width: 0), so a
   hairline reads as a rendering artifact where this has to read as a decision.
   Edge by edge rather than the `border` shorthand, and *that* is the constraint that
   shapes this rule: the inline-start edge belongs to the attention stripe
   (style.css), a scoped selector outranks it, and a shorthand here would repaint a
   crashed orchestrator's red stripe in the accent — losing the one marking the row
   shares with every other row in the list. */
.orchestrator-row {
  border-block-width: 2px;
  border-inline-end-width: 2px;
  border-style: solid;
  border-block-color: rgb(var(--v-theme-primary));
  border-inline-end-color: rgb(var(--v-theme-primary));
}

/* And when no stripe claims that edge, the border has all four: three orange sides
   around an open one reads as a card that failed to render, not as a marking. */
.orchestrator-row:not(.tone-edge) {
  border-inline-start: 2px solid rgb(var(--v-theme-primary));
}

/* Two lines of the note, and the whole note still in the DOM: this is agent prose
   of any length (a waiting session's question arrives here in full), and a row is
   a place you read *that a run needs you*, not the question itself. Clamping is
   the reason the row can carry it at all — the alternative sizes the list by
   whichever agent wrote the most.
   `-webkit-` prefixed properties and not only the standard ones: `line-clamp`
   alone is honoured by nothing an operator is likely to be holding, and the
   unprefixed `box-orient` by nothing at all. */
.attention-note {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  overflow: hidden;
}
</style>
