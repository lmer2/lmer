import { describe, expect, it } from 'vitest'
import { DEFAULT_ASSISTANT_NAME, applyAssistantName, assistantName } from '../src/ratchet.js'

describe('assistantName', () => {
  it('defaults to Ratchet', () => {
    expect(DEFAULT_ASSISTANT_NAME).toBe('Ratchet')
    expect(assistantName.value).toBe('Ratchet')
  })

  it.each([
    [{ assistant_name: 'Sprocket' }, 'Sprocket'],
    [{ assistant_name: '  Cog  ' }, 'Cog'],
    [{ assistant_name: '' }, 'Ratchet'],
    [{ assistant_name: '   ' }, 'Ratchet'],
    [{}, 'Ratchet'],
    [undefined, 'Ratchet'],
    [null, 'Ratchet'],
  ])('applyAssistantName(%j) → %s', (config, expected) => {
    applyAssistantName(config)
    expect(assistantName.value).toBe(expected)
    applyAssistantName({})
  })
})
