<script setup>
// Two icons — Slack and matrix — that connect a running session to a chat thread
// and disconnect it again (issue #412), and open a new thread in a channel for
// it (issue #429).
//
// One component, three homes: the fleet row, the run view header and the uber
// lmer pane header. They differ only in what they hand it, which is why the
// component owns no polling: it renders `run.threads` off the fleet payload (or
// the assistant status), and asks its parent to refresh after a write. A copy
// that polled would poll once per row.
//
// Colourless by default like the rest of a run row, with one exception: a
// transport with a live connection wears the brand accent, because "this run is
// mirrored into a thread" is a go-and-see-this fact and the whole point of
// drawing the icons at all is that it is readable at arm's length.
import { computed, ref, watch } from 'vue'
import {
  mdiAlertCircleOutline, mdiBookmarkOutline, mdiBookmarkPlusOutline, mdiClose, mdiLinkOff, mdiMatrix,
  mdiOpenInNew, mdiPlus, mdiSlack,
} from '@mdi/js'
import {
  addSessionReference, connectSessionThread, disconnectSessionThread,
  openSessionThread, removeSessionReference,
} from '../api.js'

const TRANSPORTS = [
  { channel: 'slack', icon: mdiSlack, label: 'Slack' },
  { channel: 'matrix', icon: mdiMatrix, label: 'matrix' },
]

const props = defineProps({
  sessionId: { type: String, default: null },
  threads: { type: Array, default: () => [] },
  live: { type: Boolean, default: false },
  // Threads the run has been *pointed at* rather than connected to (#414).
  // `null` — not an empty array — means this surface has no run behind it, and
  // the reference controls are left out rather than offered and then refused:
  // a reference is attached to a run, and the uber lmer pane is a session.
  references: { type: Array, default: null },
})

const emit = defineEmits(['changed'])

const dialog = ref(false)
const pending = ref(null)
// Which of the two things the one dialog is asking for: a thread to connect, or
// a thread to remember. They differ by one field and by what saving does.
const intent = ref('connect')
const permalink = ref('')
const referenceTitle = ref('')
// The root message's first line, when what is typed above is a channel rather
// than a link.
const rootTitle = ref('')
const busy = ref(false)
const problem = ref(null)
const failure = ref(null)
const operations = ref([])
let generation = 0

watch(() => props.sessionId, () => {
  generation += 1
  operations.value = []
  busy.value = false
  dialog.value = false
  problem.value = null
  failure.value = null
})

function settleOperations() {
  operations.value = operations.value.filter((operation) => {
    if (!operation.accepted) return true
    const present = props.threads.some((thread) => thread.id === operation.thread.id)
    return operation.action === 'disconnect' ? present : !present
  })
}

watch(() => props.threads, settleOperations, { deep: true })

function operationFor(thread) {
  return operations.value.find((operation) => operation.thread.id === thread.id)
}

function threadStatus(thread) {
  const operation = operationFor(thread)
  if (operation) return operation.action === 'disconnect' ? 'disconnecting…' : 'connecting…'
  return thread.running ? 'connected' : thread.detail || 'not connected'
}

const connecting = computed(() => operations.value.some((operation) => operation.action === 'connect'))

const byChannel = computed(() => {
  const groups = { slack: [], matrix: [] }
  for (const thread of props.threads || []) {
    if (groups[thread.channel]) groups[thread.channel].push(thread)
  }
  for (const operation of operations.value) {
    const thread = operation.thread
    if (!props.threads.some((item) => item.id === thread.id) && groups[thread.channel]) {
      groups[thread.channel].push(thread)
    }
  }
  return groups
})

const referencesByChannel = computed(() => {
  const groups = { slack: [], matrix: [] }
  for (const reference of props.references || []) {
    if (groups[reference.channel]) groups[reference.channel].push(reference)
  }
  return groups
})

// References belong to the run, so a run row offers them whether or not its
// session is live: pointing a finished run at the thread it should have read is
// pointless, but pointing one that is between sessions at it is not.
const referencable = computed(() => !!props.sessionId && Array.isArray(props.references))

function hasMenu(transport) {
  return (
    byChannel.value[transport.channel].length > 0
    || referencesByChannel.value[transport.channel].length > 0
    || referencable.value
  )
}

// Connecting a run that has no live session is out of scope: there is no control
// plane for a connector to talk to. The icons still render — what a run *was*
// connected to stays true — but nothing new can be attached.
const connectable = computed(() => !!props.sessionId && props.live)

function ariaLabel(transport) {
  if (needsAttention(transport)) return `${transport.label} threads: recovery required`
  return byChannel.value[transport.channel].length || referencesByChannel.value[transport.channel].length
    ? `${transport.label} threads`
    : `connect a ${transport.label} thread`
}

function needsAttention(transport) {
  return byChannel.value[transport.channel].some((thread) => thread.attention === 'recovery-required')
}

// The menu already opens itself when there is one; this is the other case.
function onIconClick(transport) {
  if (!hasMenu(transport)) openDialog(transport.channel)
}

// What the icon says on hover. Every connected thread by name, and — when one is
// recorded but its connector is not running — why, because a thread that has
// gone quiet otherwise looks exactly like one that is working.
function hoverText(transport) {
  const threads = byChannel.value[transport.channel]
  if (!threads.length) {
    if (connectable.value) return `Connect a ${transport.label} thread`
    // A run between sessions can still be pointed at a thread, so the tooltip
    // has to say what is unavailable rather than that nothing is.
    return referencable.value
      ? `${transport.label}: no live session to connect — a thread can still be referenced`
      : `${transport.label}: this run has no live session to connect`
  }
  return threads
    .map((thread) => {
      const name = thread.label || thread.permalink
      if (thread.running) return name
      return `${name} — not connected${thread.detail ? `: ${thread.detail}` : ''}`
    })
    .join('\n')
}

// One field takes either, and this is which one it took: a permalink is a URL
// and a channel is not (`#general`, `C0123ABCDEF`, `!room:example.org`). Decided
// here because the two are different bodies on the one route — the daemon reads
// the transport out of a link and cannot read it out of a field that is absent.
const opening = computed(
  () => permalink.value.trim().length > 0 && !/^https?:\/\//i.test(permalink.value.trim()),
)

// A channel carries no transport, so the dialog's own icon is the only thing
// that says which one the operator picked — and the daemon's reading of a bare
// `#name` is Slack. So a matrix alias typed without its `:server` half, into the
// matrix dialog, would open a *Slack* thread in a like-named channel: a root
// message that cannot be unposted, on the transport nobody chose. Checked here
// rather than by adding a control that names the transport, which for a
// permalink could disagree with the link. Not the authority — `parse_channel`
// is, and it refuses what neither transport claims; this only holds the value
// to the icon it was typed under.
const channelTransport = computed(
  () => (/^[!#][^\s/]+:[^\s/]+$/.test(permalink.value.trim()) ? 'matrix' : 'slack'),
)
const wrongTransport = computed(
  () => opening.value && intent.value !== 'reference' && channelTransport.value !== pending.value,
)

function openDialog(channel, mode = 'connect') {
  pending.value = channel
  intent.value = mode
  permalink.value = ''
  referenceTitle.value = ''
  rootTitle.value = ''
  problem.value = null
  dialog.value = true
}

async function save() {
  if (busy.value) return
  if (intent.value === 'reference') return remember()
  if (connecting.value) return
  return connect()
}

async function remember() {
  if (!permalink.value.trim()) return
  busy.value = true
  problem.value = null
  try {
    await addSessionReference(props.sessionId, permalink.value.trim(), referenceTitle.value.trim())
    dialog.value = false
    emit('changed')
  } catch (error) {
    problem.value = error.message
  } finally {
    busy.value = false
  }
}

async function forget(reference) {
  busy.value = true
  failure.value = null
  try {
    await removeSessionReference(props.sessionId, reference.id)
    emit('changed')
  } catch (error) {
    failure.value = error.message
  } finally {
    busy.value = false
  }
}

async function connect() {
  if (!permalink.value.trim()) return
  // The alert below the field says why, and the button is disabled; Enter
  // reaches this path too, so the check is here rather than only on the button.
  if (wrongTransport.value) return
  busy.value = true
  problem.value = null
  const mine = generation
  const operation = {
    action: 'connect', accepted: false,
    thread: { id: `pending:${pending.value}`, channel: pending.value, label: permalink.value.trim() },
  }
  operations.value = [...operations.value, operation]
  try {
    let result
    if (opening.value) {
      result = await openSessionThread(props.sessionId, permalink.value.trim(), rootTitle.value.trim())
    } else {
      result = await connectSessionThread(props.sessionId, permalink.value.trim())
    }
    if (mine !== generation) return
    operations.value = operations.value.map((item) => item.thread.id === operation.thread.id
      ? { ...item, accepted: true, thread: result.thread } : item)
    settleOperations()
    dialog.value = false
    emit('changed')
  } catch (error) {
    if (mine !== generation) return
    operations.value = operations.value.filter((item) => item.thread.id !== operation.thread.id)
    // Verbatim: these refusals name the environment variable that is missing or
    // the run already holding the thread, and a rewording here would drop the
    // one thing the operator needs to act on.
    problem.value = error.message
  } finally {
    if (mine === generation) busy.value = false
  }
}

async function disconnect(thread) {
  if (busy.value || operationFor(thread)) return
  busy.value = true
  failure.value = null
  const mine = generation
  operations.value = [...operations.value, { action: 'disconnect', thread, accepted: false }]
  try {
    await disconnectSessionThread(props.sessionId, thread.id)
    if (mine !== generation) return
    operations.value = operations.value.map((item) => item.thread.id === thread.id
      ? { ...item, accepted: true } : item)
    settleOperations()
    emit('changed')
  } catch (error) {
    if (mine !== generation) return
    operations.value = operations.value.filter((item) => item.thread.id !== thread.id)
    // Beside the icons, never in the connect dialog: that form is titled
    // "Connect a …" and its primary action is the one the operator did not ask
    // for, so surfacing a *disconnect* failure there offers to do the opposite.
    failure.value = error.message
  } finally {
    if (mine === generation) busy.value = false
  }
}
</script>

<template>
  <div class="d-flex align-center ga-1">
    <template v-for="transport in TRANSPORTS" :key="transport.channel">
      <v-badge
        :model-value="needsAttention(transport) || byChannel[transport.channel].length > 1"
        :content="needsAttention(transport) ? '!' : byChannel[transport.channel].length"
        :color="needsAttention(transport) ? 'warning' : 'primary'"
        offset-x="4"
        offset-y="4"
      >
        <!-- The menu hangs off the button rather than the button off the menu's
             activator slot: the same thing in a browser, but only this way is
             the button real markup that a test without one can read. -->
        <v-btn
          icon
          variant="text"
          size="small"
          density="comfortable"
          :color="needsAttention(transport) ? 'warning' : byChannel[transport.channel].length ? 'primary' : undefined"
          :title="hoverText(transport)"
          :aria-label="ariaLabel(transport)"
          :disabled="!hasMenu(transport) && !connectable"
          @click="onIconClick(transport)"
        >
          <v-icon :icon="transport.icon" />
          <v-menu v-if="hasMenu(transport)" activator="parent" :close-on-content-click="false">
            <v-list density="compact">
              <v-list-item
                v-for="thread in byChannel[transport.channel]"
                :key="thread.id"
                :href="thread.permalink"
                target="_blank"
                rel="noopener"
                :title="thread.label || thread.permalink"
                :subtitle="threadStatus(thread)"
                :prepend-icon="thread.attention ? mdiAlertCircleOutline : undefined"
                :append-icon="mdiOpenInNew"
              />
              <v-divider v-if="byChannel[transport.channel].length" />
              <v-list-item
                v-for="thread in byChannel[transport.channel]"
                :key="`off-${thread.id}`"
                :prepend-icon="mdiLinkOff"
                :data-thread-action="thread.id"
                :disabled="busy || !!operationFor(thread)"
                :aria-disabled="busy || !!operationFor(thread)"
                :title="operationFor(thread) ? threadStatus(thread) : `Disconnect ${thread.label || thread.permalink}`"
                @click="disconnect(thread)"
              />
              <v-list-item
                v-if="connectable"
                :disabled="busy || connecting"
                :prepend-icon="mdiPlus"
                :title="`Connect another ${transport.label} thread`"
                @click="openDialog(transport.channel)"
              />

              <!-- Referenced, not connected: the run was told about these once
                   and reads or posts them on demand. Under their own heading
                   because the difference is the whole feature — nothing here is
                   listening, and none of these threads is held by this run. -->
              <template v-if="referencesByChannel[transport.channel].length">
                <v-divider />
                <v-list-subheader>referenced</v-list-subheader>
                <v-list-item
                  v-for="reference in referencesByChannel[transport.channel]"
                  :key="reference.id"
                  :href="reference.permalink"
                  target="_blank"
                  rel="noopener"
                  :prepend-icon="mdiBookmarkOutline"
                  :title="reference.title || reference.permalink"
                  :subtitle="reference.announced ? 'the run was told' : 'the run has not been told yet'"
                  :append-icon="mdiOpenInNew"
                />
                <v-list-item
                  v-for="reference in referencesByChannel[transport.channel]"
                  :key="`drop-${reference.id}`"
                  :prepend-icon="mdiClose"
                  :disabled="busy"
                  :title="`Remove ${reference.title || reference.permalink}`"
                  @click="forget(reference)"
                />
              </template>
              <v-list-item
                v-if="referencable"
                :prepend-icon="mdiBookmarkPlusOutline"
                title="Add a thread as reference"
                @click="openDialog(transport.channel, 'reference')"
              />
            </v-list>
          </v-menu>
        </v-btn>
      </v-badge>
    </template>

    <span
      v-for="operation in operations"
      :key="operation.thread.id"
      role="status"
      class="text-body-small text-medium-emphasis"
    >{{ operation.thread.label || operation.thread.permalink }}: {{ threadStatus(operation.thread) }}</span>

    <v-alert
      v-if="failure"
      type="error"
      variant="tonal"
      density="compact"
      closable
      class="text-body-small"
      @click:close="failure = null"
    >{{ failure }}</v-alert>

    <!-- One field for the thread, because the transport comes out of what is
         typed: a permalink names one that exists, a channel a place to open a
         new one in (#429). A second control naming the transport could disagree
         with either. -->
    <v-dialog v-model="dialog" max-width="560">
      <v-card>
        <v-card-title class="text-title-medium">
          {{ intent === 'reference' ? 'Add a thread as reference' : `Connect a ${pending === 'matrix' ? 'matrix' : 'Slack'} thread` }}
        </v-card-title>
        <v-card-text>
          <v-text-field
            v-model="permalink"
            :label="intent === 'reference'
              ? 'Thread permalink'
              : 'Thread permalink, or a channel to open a new thread in'"
            :placeholder="pending === 'matrix'
              ? 'https://matrix.to/#/!room:example.org/$event  —  or  !room:example.org'
              : 'https://example.slack.com/archives/C0123ABCDEF/p1712345678000900  —  or  #a-channel'"
            autofocus
            :disabled="busy || (intent !== 'reference' && connecting)"
            @keyup.enter="save"
          />
          <v-text-field
            v-if="intent === 'reference'"
            v-model="referenceTitle"
            label="What this thread is"
            placeholder="the design discussion behind #414"
            :disabled="busy"
            @keyup.enter="save"
          />
          <v-text-field
            v-if="intent !== 'reference' && opening"
            v-model="rootTitle"
            label="First line of the root message (optional)"
            placeholder="the run's own title"
            :disabled="busy"
            @keyup.enter="save"
          />
          <p v-if="intent !== 'reference' && opening" class="text-body-small text-medium-emphasis">
            A new thread is opened there and this run is connected to it. The
            root message names the run, its key and this control UI. The bot has
            to be in the channel already.
          </p>
          <v-alert v-if="wrongTransport" type="warning" variant="tonal" density="compact">
            {{ pending === 'matrix'
              ? 'A matrix room is an id or alias carrying its server — !room:example.org, or #room:example.org.'
              : 'A Slack channel is an id or #name; that looks like a matrix room.' }}
          </v-alert>
          <p v-if="intent === 'reference'" class="text-body-small text-medium-emphasis">
            Nothing is connected: the run is told about this thread once, and
            reads or posts it with <code>lmer-signal read</code> /
            <code>post --thread</code>.
          </p>
          <v-alert v-if="problem" type="error" variant="tonal" density="compact">
            {{ problem }}
          </v-alert>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn :disabled="busy" variant="text" @click="dialog = false">cancel</v-btn>
          <v-btn
            :loading="busy || (intent !== 'reference' && connecting)"
            :disabled="wrongTransport"
            variant="tonal"
            color="primary"
            @click="save"
          >
            {{ intent === 'reference' ? 'save' : (connecting ? 'connecting…' : (opening ? 'open' : 'connect')) }}
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>
