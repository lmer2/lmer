# Mounted pane tests

From `web/`, run `npm ci`, then `npm test`. `npm run build` checks the production
bundle. The existing Python web tests remain part of the repository's full
`gate-check` suite; run both suites before submitting a pane change.
The mounted suite is currently manual: neither CI nor `gate-check` invokes
`npm test`. Changes to those root configurations are outside this pane's scope.

Vitest uses the Vue SFC compiler and mounts the real components, Vuetify menus,
dialogs and Markdown renderer with Vue Test Utils. API responses are mocked;
clicks, reactive updates and `onMounted` run in jsdom. The thread tests model the
parent's next fleet poll by changing the `threads` prop independently of the
write response. This exercises the gap between request acceptance and confirmed
state, including refusals.

We use [jsdom](https://github.com/jsdom/jsdom) for its DOM and event support;
these tests need mounted lifecycle and interactive controls, not layout or a
browser binary. [Vitest's jsdom environment](https://v3.vitest.dev/guide/environment)
works with the existing Vite 6 toolchain. The setup supplies non-layout
`ResizeObserver`, `matchMedia` and `visualViewport` globals, and each test unmounts
its components and restores timers. These tests do not verify geometry, scrolling
or visual appearance. One worker bounds memory use on development hosts.

The pane's outbound-copy and thread-pending rules, including complete-command and file/stdin
correlation limit, are documented in [COMMS.md §4](../../docs/COMMS.md#what-the-chat-pane-draws).
