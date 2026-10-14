export const projectColors = {
  Red: '#c62828', Orange: '#b84b00', Amber: '#936100', Yellow: '#827400', Lime: '#527d00',
  Green: '#26732b', Emerald: '#007553', Teal: '#00796b', Cyan: '#00788c', 'Sky blue': '#0078a8',
  Blue: '#225ec9', Indigo: '#4548b4', Violet: '#7346bb', Purple: '#853a9d', Magenta: '#aa227b',
  Pink: '#b12869', Rose: '#b3334a', Brown: '#795548', Slate: '#53677a', Gray: '#666666',
}

// The backend's other accepted spelling besides a preset name (projects.py `_HEX_COLOR`).
const HEX_COLOR = /^#[0-9a-f]{6}$/i

export function projectColor(value) {
  if (Object.hasOwn(projectColors, value)) return projectColors[value]
  return HEX_COLOR.test(value || '') ? value.toLowerCase() : projectColors.Gray
}

// A picked color equal to a preset is stored as the preset's name, so a project
// whose color was only re-confirmed in the picker keeps the value it had.
// The picker's text field also takes #rrggbbaa; a project color is opaque, so the
// alpha pair is dropped rather than sending a value the backend refuses.
export function storedProjectColor(hex) {
  const value = (hex || '').toLowerCase().replace(/^(#[0-9a-f]{6})[0-9a-f]{2}$/, '$1')
  return Object.keys(projectColors).find(name => projectColors[name] === value) || value
}

function luminance(hex) {
  const channels = [1, 3, 5].map((start) => parseInt(hex.slice(start, start + 2), 16) / 255)
  const [r, g, b] = channels.map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4))
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

export function contrastRatio(first, second) {
  const [light, dark] = [luminance(first), luminance(second)].sort((a, b) => b - a)
  return (light + 0.05) / (dark + 0.05)
}

// Text on a surface painted in the project's color. Decided by the color alone:
// the surface is the same in both themes, so the theme has no say.
export function projectTextColor(value) {
  const background = projectColor(value)
  return contrastRatio(background, '#000000') > contrastRatio(background, '#ffffff') ? '#000000' : '#ffffff'
}

export function eligibleSlots(slots, workspaceId) {
  return workspaceId ? slots.filter((slot) => slot.applicable_projects?.includes(workspaceId)) : []
}

const drafts = new Map()
export function readDraft(key) { return drafts.get(key) || { text: '', attachments: [] } }
export function storeDraft(key, text, attachments) {
  if (text || attachments.length) drafts.set(key, { text, attachments: [...attachments] })
  else drafts.delete(key)
}
