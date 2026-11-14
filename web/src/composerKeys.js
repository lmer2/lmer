import { computed, ref } from 'vue'
import { chatEnterSends, chatSettingsLoaded } from './operatorChat.js'

// What Enter does in the three message boxes (issue 663): Chat's composer, the
// ask-channel reply (AskBox) and a stopped run's answer (AnswerBox). One rule, so
// an operator who learns it in one box has learned all three.
//
// Enter sends unless the platform's `chat_enter_sends` is off, or the primary
// pointer is coarse. A phone keyboard has no Shift+Enter, so on a touch-first
// device Enter is the only way to type a newline. Binding it to send there would
// make multi-line messages impossible to write, so touch keeps Enter as a newline
// and the send button does the sending. Ctrl/Cmd+Enter sends in every mode, so
// the old chord keeps working for anyone used to it.
//
// Until the platform's answer has loaded, Enter is a newline: failing towards the
// harmless key is what keeps a slow or failed config fetch from sending a draft
// for an operator who switched Enter-sends off.

function coarseQuery() {
  try {
    return window.matchMedia?.('(pointer: coarse)') ?? null
  } catch {
    return null
  }
}

const query = coarseQuery()
// A ref rather than a one-off read: a tablet that gains or loses a keyboard
// cover changes its primary pointer without a reload. `addListener` is the only
// form Safari before 14 offers.
export const coarsePointer = ref(!!query?.matches)
const followPointer = (event) => { coarsePointer.value = event.matches }
if (query?.addEventListener) query.addEventListener('change', followPointer)
else query?.addListener?.(followPointer)

export const enterSends = computed(
  () => chatSettingsLoaded.value && chatEnterSends.value && !coarsePointer.value,
)

// The half-sentence each box's hint carries, phrased for the rule in force. A
// touch keyboard has no Ctrl or Cmd, so its phrasing does not name the chord.
export const enterHint = computed(() => {
  if (enterSends.value) return 'Enter sends, Shift+Enter is a new line'
  if (coarsePointer.value) return 'Enter is a new line'
  return 'Enter is a new line, Ctrl+Enter (Cmd on a Mac) sends'
})

// Bound as `@keydown.enter`. An Enter that commits an IME composition (CJK input,
// some autocorrect) belongs to the IME, not to us. `isComposing` reports it, and
// keyCode 229 covers the browsers that clear `isComposing` on the committing
// keydown. `preventDefault` whenever we send: a newline left behind by the
// keystroke would stay in a draft that a failed send keeps. A held key's repeats
// are swallowed rather than sent, so a refused send is announced once.
export function composerKeydown(event, send) {
  if (event.key !== 'Enter' || event.isComposing || event.keyCode === 229) return
  const sends = event.ctrlKey || event.metaKey
    || (!event.shiftKey && !event.altKey && enterSends.value)
  if (!sends) return
  event.preventDefault()
  if (!event.repeat) send()
}
