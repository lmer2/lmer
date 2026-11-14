// AI usage formatting and the "most expensive now" ranking, shared by every usage
// surface so they cannot disagree (issue 315, docs/USAGE-TRACKING.md). Two rules:
// - There is no single "total tokens": six buckets are six facts, and a sum that
//   includes cache reads (cheap and enormous) says nothing about spend.
// - `thinking` is a subset of `output`; no function here adds it to anything.

import { ago } from './format.js'

//: The buckets, in reading order, with the word each is shown under. Cache reads
//: come last among the token buckets because they are the one left out of every
//: weight below; requests are a count of calls, not tokens.
export const BUCKETS = [
  { key: 'input', label: 'input' },
  { key: 'cache_create', label: 'cache write' },
  { key: 'output', label: 'output' },
  { key: 'thinking', label: 'thinking' },
  { key: 'cache_read', label: 'cache read' },
  { key: 'requests', label: 'requests' },
]

function count(value) {
  const number = Number(value)
  return Number.isFinite(number) ? number : 0
}

// A token count at arm's length: 950, 12.3k, 4.1M. The exact number is the
// title wherever one of these is shown.
export function tokens(value) {
  const number = count(value)
  const size = Math.abs(number)
  if (size < 1000) return String(Math.round(number))
  if (size < 1e6) return `${trim(number / 1e3)}k`
  if (size < 1e9) return `${trim(number / 1e6)}M`
  return `${trim(number / 1e9)}G`
}

function trim(value) {
  return (Math.abs(value) < 10 ? value.toFixed(1) : value.toFixed(0)).replace(/\.0$/, '')
}

export function exact(value) {
  return count(value).toLocaleString('en-US')
}

// Every bucket as a {key, label, value, text, title} part, in BUCKETS order.
// Null buckets render no parts at all, so a caller's `v-for` draws nothing.
export function bucketParts(buckets) {
  if (!buckets) return []
  return BUCKETS.map(({ key, label }) => ({
    key,
    label: key === 'thinking' ? 'of which thinking' : label,
    value: count(buckets[key]),
    text: tokens(buckets[key]),
    title: `${exact(buckets[key])} ${key === 'requests' ? 'requests' : `${label} tokens`}`,
  }))
}

// One line of the same facts, for a tooltip or a dimmed line.
export function bucketLine(buckets) {
  if (!buckets) return ''
  const value = (key) => tokens(buckets[key])
  return [
    `input ${value('input')}`,
    `cache write ${value('cache_create')}`,
    `output ${value('output')} (thinking ${value('thinking')})`,
    `cache read ${value('cache_read')}`,
    `${exact(buckets.requests)} requests`,
  ].join(' · ')
}

// The weight the fleet ranks unpriced runs by: output + cache writes + input.
// Cache reads are left out because they dominate and would misrank runs, and
// thinking is already inside output.
export function unpricedWeight(buckets) {
  if (!buckets) return 0
  return count(buckets.output) + count(buckets.cache_create) + count(buckets.input)
}

// --- cost ---------------------------------------------------------------------
//
// A cost object is the daemon's read-time estimate from the operator's price
// table, or null when there is no table. Null renders nothing — never a zero.

export function costLabel(cost) {
  if (!cost || cost.total == null || !Number.isFinite(Number(cost.total))) return null
  const amount = `${cost.currency || ''} ${Number(cost.total).toFixed(2)}`.trim()
  return cost.partial ? `${amount} partial` : amount
}

// The title of a cost: which table produced it, and what it could not price.
export function costTitle(cost) {
  if (!cost) return ''
  const parts = [`estimated from price table ${cost.table_hash || 'unknown'}`]
  if (cost.partial) {
    const missing = (cost.unpriced_models || []).join(', ')
    parts.push(missing ? `partial: no price for ${missing}` : 'partial: some usage has no price')
  }
  return parts.join(' — ')
}

// What a harness says it cost (pi's `cost.total`), never lmer's estimate. The
// record carries a list of {currency, total}, the live view a {currency: total}
// map; both come out as one list, and nothing here ever adds one to a cost.
export function harnessReported(value) {
  if (!value) return []
  const entries = Array.isArray(value)
    ? value.map((item) => [item?.currency, item?.total])
    : Object.entries(value)
  return entries
    .filter(([, total]) => Number.isFinite(Number(total)))
    .map(([currency, total]) => `${currency || ''} ${Number(total).toFixed(2)}`.trim())
}

// --- time ---------------------------------------------------------------------

// A span as "2h10m": a reset or a fill is read against another one, so whole
// hours alone (format.js's duration) would round both to the same number.
export function span(seconds) {
  const value = Number(seconds)
  if (seconds === null || seconds === undefined || !Number.isFinite(value)) return null
  const minutes = Math.max(0, Math.round(value / 60))
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return minutes % 60 ? `${hours}h${minutes % 60}m` : `${hours}h`
  const days = Math.floor(hours / 24)
  return hours % 24 ? `${days}d${hours % 24}h` : `${days}d`
}

// How long until *iso*, or null when it is unknown or already past.
export function until(iso, now = Date.now()) {
  if (!iso) return null
  const when = Date.parse(iso)
  if (Number.isNaN(when) || when <= now) return null
  return span((when - now) / 1000)
}

export function wallClock(source, now = Date.now()) {
  if (!source) return []
  const parts = []
  const active = span(source.active_seconds)
  if (active) parts.push({ key: 'active', text: `active ${active}`, title: `${exact(source.active_seconds)} s of wall clock` })
  if (source.started_at) parts.push({ key: 'started', text: `started ${ago(source.started_at, now)}`, title: source.started_at })
  if (source.ended_at) parts.push({ key: 'ended', text: `ended ${ago(source.ended_at, now)}`, title: source.ended_at })
  return parts
}

// --- headroom -----------------------------------------------------------------

//: The harness's own window keys, shortened the way the status line says them.
//: Anything else is shown as the key itself rather than guessed at.
const WINDOW_LABELS = { five_hour: '5h', seven_day: '7d' }

export function windowLabel(key) {
  return WINDOW_LABELS[key] || key || '?'
}

//: Percent at which a window warns on its own, whatever the projection says.
export const HEADROOM_WARN_PERCENT = 80

// One headroom entry as the strip draws it. `text` is the chip's words,
// `warning` the projection sentence when the window fills before it resets,
// `title` the reading behind it. Always labelled an approximation: readings are
// grouped by launching operator, and one operator can run several accounts.
export function headroomView(entry, now = Date.now()) {
  const percent = Number.isFinite(Number(entry?.percent)) ? Math.round(Number(entry.percent)) : null
  const reset = until(entry?.resets_at, now)
  const text = [
    entry?.harness, entry?.operator,
    `${windowLabel(entry?.window)} ${percent === null ? '?' : percent}%`,
    reset && `resets in ${reset}`,
  ].filter(Boolean).join(' · ')
  const fills = entry?.fills_before_reset
    ? `at current burn fills in ~${until(entry.projected_full_at, now) || '?'}, before reset in ${reset || '?'}`
    : null
  const rate = entry?.rate_pct_per_hour
  const title = [
    rate === null || rate === undefined
      ? `no rate: ${entry?.reason || 'unknown'}`
      : `rising ${Number(rate).toFixed(1)}%/h over ${entry?.readings ?? 0} readings`,
    entry?.stale ? `stale: last reading ${span(entry?.age_seconds) || '?'} old` : null,
    'an approximation of the account: one operator can run several accounts',
  ].filter(Boolean).join(' — ')
  return {
    text,
    fills,
    title,
    warn: (percent !== null && percent >= HEADROOM_WARN_PERCENT) || !!entry?.fills_before_reset,
    stale: !!entry?.stale,
  }
}

// --- one run's burn, on the fleet card ----------------------------------------

// "burn 12k out/15m · 340k out total", or null when the run has no usage.
export function burnLabel(usage) {
  if (!usage) return null
  return `burn ${tokens(usage.burn?.output)} out/15m · ${tokens(usage.totals?.output)} out total`
}

export function burnTitle(usage) {
  if (!usage) return ''
  return [
    `last 15 minutes: ${bucketLine(usage.burn) || 'nothing'}`,
    `run total: ${bucketLine(usage.totals) || 'nothing'}`,
    usage.status && usage.status !== 'collected' ? `usage ${usage.status.replace('_', ' ')}` : null,
    costLabel(usage.cost) && `cost ${costLabel(usage.cost)}`,
  ].filter(Boolean).join(' — ')
}

// --- "most expensive now" -----------------------------------------------------
//
// By cost when every row that has usage is fully priced (a partial cost is a
// floor, and comparing floors misranks), else by the unpriced token weight of
// the last 15 minutes. Rows with no usage at all sort last and do not decide the
// ranking: a run the collector has not read is not evidence about prices.

// Priced for the ranking: the burn's own cost, the last 15 minutes, which is
// what "now" means (the run's total cost is the run detail's to show).
export function isPriced(usage) {
  const cost = usage?.burn_cost
  return !!cost && !cost.partial && Number.isFinite(Number(cost.total))
}

export function usageRanking(rows) {
  const measured = (rows || []).filter((row) => row?.usage)
  return measured.length && measured.every((row) => isPriced(row.usage))
    ? 'cost'
    : 'unpriced_token_weight'
}

export function usageComparator(rows) {
  const byCost = usageRanking(rows) === 'cost'
  const score = (row) => {
    if (!row?.usage) return -1
    return byCost ? Number(row.usage.burn_cost.total) : unpricedWeight(row.usage.burn)
  }
  return (a, b) => score(b) - score(a)
}

export function mostExpensiveFirst(rows) {
  return [...(rows || [])].sort(usageComparator(rows))
}
