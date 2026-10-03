import { afterEach, vi } from 'vitest'
import { enableAutoUnmount } from '@vue/test-utils'

enableAutoUnmount(afterEach)

// jsdom has no layout engine; these components only need observer lifecycle.
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.visualViewport = undefined
window.matchMedia = (media) => ({
  media, matches: false, addEventListener() {}, removeEventListener() {},
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})
