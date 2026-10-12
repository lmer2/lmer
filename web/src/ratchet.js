// The assistant's identity in the UI (issue 581). One module, so no component
// hardcodes the name again: the daemon serves the configured value in
// `GET /api/state` → `config.assistant_name`, and App.vue hands it here after
// every fleet poll. Before the first poll the default stands.
import { ref } from 'vue'

export const DEFAULT_ASSISTANT_NAME = 'Ratchet'

export const assistantName = ref(DEFAULT_ASSISTANT_NAME)

export function applyAssistantName(config) {
  const raw = config && typeof config.assistant_name === 'string' ? config.assistant_name.trim() : ''
  assistantName.value = raw || DEFAULT_ASSISTANT_NAME
}
