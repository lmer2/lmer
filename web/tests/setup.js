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
// jsdom returns null here too, but logs "not implemented" on every call; the
// color picker's canvas paints from a frame callback that can outlive its test.
HTMLCanvasElement.prototype.getContext = () => null
window.matchMedia = (media) => ({
  media, matches: false, addEventListener() {}, removeEventListener() {},
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})
