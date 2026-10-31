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

function hsl(hex) {
  const [r, g, b] = [1, 3, 5].map((start) => parseInt(hex.slice(start, start + 2), 16) / 255)
  const max = Math.max(r, g, b)
  const min = Math.min(r, g, b)
  const l = (max + min) / 2
  const d = max - min
  if (!d) return [0, 0, l]
  const s = d / (1 - Math.abs(2 * l - 1))
  const h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4
  return [(h * 60 + 360) % 360, s, l]
}

function hexOf(h, s, l) {
  const c = (1 - Math.abs(2 * l - 1)) * s
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1))
  const m = l - c / 2
  const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x]
    : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x]
  return `#${[r, g, b].map((v) => Math.round((v + m) * 255).toString(16).padStart(2, '0')).join('')}`
}

// Vuetify spells its derived inks short (`#FFF`); everything here reads six digits.
function longHex(hex) {
  return /^#[0-9a-f]{3}$/i.test(hex) ? `#${[...hex.slice(1)].map((digit) => digit + digit).join('')}` : hex
}

// What `ink` at `alpha` actually paints on `ground`: emphasis classes are opacity, not a colour.
export function blend(ink, alpha, ground) {
  const channel = (hex, start) => parseInt(longHex(hex).slice(start, start + 2), 16)
  return `#${[1, 3, 5].map((start) => Math.round(alpha * channel(ink, start) + (1 - alpha) * channel(ground, start))
    .toString(16).padStart(2, '0')).join('')}`
}

const AA = 4.5
const GROUND_SATURATION_CAP = 0.5
// Where the search starts: close to the theme's own chat-operator lightness.
const GROUND_START = { dark: 0.24, light: 0.88 }

// The operator bubble's ground in the project's hue (issue 646). Lightness walks away
// from the ink — down in a dark theme, up in a light one — until the ink clears AA at
// every emphasis it is drawn at; the walk ends at black or white, which always does.
export function operatorGround(value, { dark, ink, emphasis }) {
  const [h, s] = hsl(projectColor(value))
  const saturation = Math.min(s, GROUND_SATURATION_CAP)
  const step = dark ? -0.01 : 0.01
  // An empty list would pass `every` unchecked; full-strength ink is the floor.
  const alphas = emphasis.length ? emphasis : [1]
  for (let l = GROUND_START[dark ? 'dark' : 'light']; dark ? l > 0 : l < 1; l += step) {
    const ground = hexOf(h, saturation, l)
    if (alphas.every((alpha) => contrastRatio(blend(ink, alpha, ground), ground) >= AA)) return ground
  }
  return dark ? '#000000' : '#ffffff'
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
