import { withoutOutboundCopies } from './outbound.js'

export function mergeHistory(messages, page) {
  const arriving = page.messages || []
  const ids = new Set(arriving.map((row) => row.id))
  const kept = messages.filter((row) => !ids.has(row.id) && (
    row.session !== page.session || row.seq < page.from || (
      page.cursor < page.total && row.seq >= page.cursor
    )
  ))
  return [...kept, ...arriving].sort((a, b) => a.generation - b.generation || a.seq - b.seq)
}

export function olderPage(messages, generations) {
  return historyGaps(messages, generations).at(-1)?.request || null
}

export function historyGaps(messages, generations) {
  const gaps = []
  const loaded = new Set(messages.map((message) => message.session))
  for (let index = 0; index < messages.length; index += 1) {
    const row = messages[index]
    const prior = messages[index - 1]
    const first = prior?.session !== row.session
    let request
    if ((first && row.seq > 0) || (!first && row.seq > prior.seq + 1)) {
      request = { session: row.session, before: row.id }
    } else if (first) {
      const previous = generations.get(row.session)?.previous_session
      if (previous && !loaded.has(previous)) request = { session: previous, since: -40 }
    }
    if (request) gaps.push({ internal: index > 0, request })
  }
  return gaps
}

export function historyWithoutOutboundCopies(messages) {
  const generations = new Map()
  for (const message of messages) {
    if (!generations.has(message.session)) generations.set(message.session, [])
    generations.get(message.session).push(message)
  }
  return [...generations.values()].flatMap(withoutOutboundCopies)
}
