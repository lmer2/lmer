const collapse = (text) => String(text || '').replace(/\s+/g, ' ').trim()
const SHELL_TOOLS = ['bash', 'shell', 'shell_command', 'exec_command', 'run_shell_command']
const VERBS = ['post', 'note', 'ask']
const VALUE_FLAGS = ['--option', '--thread', '--timeout', '--interval']
const CHANNELS = ['agent', 'uber-lmer']
const KINDS = ['message', 'note', 'question']

// Only complete literal commands prove that hiding a tool row loses no other work.
function literalWords(command) {
  const words = []
  let word = ''
  let quote = null
  let started = false
  for (let index = 0; index < command.length; index += 1) {
    const char = command[index]
    if (quote === "'") {
      if (char === quote) quote = null
      else word += char
    } else if (char === '\\') {
      const next = command[++index]
      if (next === undefined || next === '\n') return null
      if (quote === '"' && !['$', '`', '"', '\\'].includes(next)) word += '\\'
      word += next
      started = true
    } else if (char === '$' || char === '`') {
      return null
    } else if (quote) {
      if (char === quote) quote = null
      else word += char
    } else if (char === '"' || char === "'") {
      quote = char
      started = true
    } else if (/[;&|<>()\n\r]/.test(char)) {
      return null
    } else if (/\s/.test(char)) {
      if (started) words.push(word)
      word = ''
      started = false
    } else {
      if (/[~*?\[\]{}#]/.test(char)) return null
      word += char
      started = true
    }
  }
  if (quote) return null
  if (started) words.push(word)
  return words
}

function carriedText(tool) {
  if (tool.status === 'failed' || typeof tool.command !== 'string') return null
  if (!SHELL_TOOLS.includes(
    String(tool.name || '').split('.').at(-1).toLowerCase(),
  )) return null
  const words = literalWords(tool.command)
  if (!words || !/(^|\/)lmer-(signal|slack)$/.test(words[0] || '')) return null
  if (!VERBS.includes(words[1])) return null
  if (words[0].endsWith('lmer-slack') && words[1] !== 'post') return null
  const body = []
  let positional = false
  for (let index = 2; index < words.length; index += 1) {
    const word = words[index]
    if (!positional && word === '--') positional = true
    else if (!positional && word === '--no-wait' && words[1] === 'ask') continue
    else if (!positional && VALUE_FLAGS.some((flag) => word.startsWith(`${flag}=`))) continue
    else if (!positional && VALUE_FLAGS.includes(word)) {
      if (words[++index] === undefined) return null
    } else if (!positional && word.startsWith('-')) return null
    else body.push(word)
  }
  return collapse(body.join(' ')) || null
}

export function withoutOutboundCopies(messages) {
  const posted = new Map()
  // Spend each record on its nearest preceding copy, without erasing repeat sends.
  return [...messages].reverse().flatMap((message) => {
    if (message.role === 'assistant' && message.via === 'comms'
      && CHANNELS.includes(message.source?.channel)
      && KINDS.includes(message.comms_kind ?? 'message') && !message.truncated) {
      const text = collapse(message.text)
      if (text) posted.set(text, (posted.get(text) || 0) + 1)
    }
    if (message.role !== 'assistant' || message.via === 'comms' || !message.tools?.length) return [message]
    const tools = [...message.tools].reverse().filter((tool) => {
      const text = carriedText(tool)
      const remaining = posted.get(text) || 0
      if (!remaining) return true
      posted.set(text, remaining - 1)
      return false
    }).reverse()
    if (tools.length === message.tools.length) return [message]
    return tools.length || message.text ? [{ ...message, tools }] : []
  }).reverse()
}
