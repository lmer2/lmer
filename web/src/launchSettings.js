// The launch settings a project and the assistant both override, worded once so the
// two settings pages cannot drift apart.
export const LAUNCH_SETTING_KEYS = ['model', 'harness', 'preset', 'agents', 'effort']

export const LAUNCH_SETTING_LABELS = {
  model: 'Model',
  harness: 'Harness',
  preset: 'Preset',
  agents: 'Fan-out presets',
  effort: 'Reasoning effort',
}

export const LAUNCH_SETTING_HINTS = {
  model: 'The model the session runs, handed to the harness verbatim.',
  harness: 'Agent harness, for example claude, codex or pi.',
  preset: 'A named startup preset from the host’s presets file.',
  agents: 'Comma-separated preset names the session may fan out to.',
  effort: 'low, medium, high, xhigh, max or auto. Empty: the harness default.',
}
