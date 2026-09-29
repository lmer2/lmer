export const projectColors = {
  Red: '#c62828', Orange: '#b84b00', Amber: '#936100', Yellow: '#827400', Lime: '#527d00',
  Green: '#26732b', Emerald: '#007553', Teal: '#00796b', Cyan: '#00788c', 'Sky blue': '#0078a8',
  Blue: '#225ec9', Indigo: '#4548b4', Violet: '#7346bb', Purple: '#853a9d', Magenta: '#aa227b',
  Pink: '#b12869', Rose: '#b3334a', Brown: '#795548', Slate: '#53677a', Gray: '#666666',
}

export function projectColor(name) {
  return projectColors[name] || projectColors.Gray
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
