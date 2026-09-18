// Provenance defines the conversation; working transcript content never does.
const CONVERSATION_VIAS = new Set(['comms', 'ask'])

export function layerOf(message) {
  return CONVERSATION_VIAS.has(message?.via) ? 'conversation' : 'transcript'
}

export function inView(view, message) {
  return view !== 'conversation' || layerOf(message) === 'conversation'
}
