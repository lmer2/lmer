import { expect, it } from 'vitest'
import { asNumber, numberReading } from '../src/numberField.js'

// A type="number" input sanitises `+300` to '' before a component sees it, so the
// helper's own contract is pinned here rather than through a field.
it('reads every spelling Save sends as the same number as that number', () => {
  for (const spelling of ['300', '300.0', '3e2', '+300', ' 0300 ', 300]) {
    expect(numberReading(spelling), String(spelling)).toBe('300')
    expect(asNumber(spelling), String(spelling)).toBe(300)
  }
  expect(numberReading('301')).not.toBe(numberReading('300'))
})

it('compares a blank or unreadable field as typed, never as zero', () => {
  expect(numberReading('')).toBe('')
  expect(numberReading('   ')).toBe('')
  expect(numberReading(null)).toBe('')
  expect(numberReading('0')).toBe('0')
  expect(numberReading('abc')).toBe('abc')
  expect(numberReading('Infinity')).toBe('Infinity')
})
