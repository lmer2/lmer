// Builds terminalBundleEntry.js with the project's own vite config (so the minifier
// is whatever the production bundle uses) and prints the entry chunk. A separate
// process because esbuild refuses to start inside the test runner's jsdom globals.
import { fileURLToPath } from 'node:url'
import { build } from 'vite'

const output = await build({
  root: fileURLToPath(new URL('../..', import.meta.url)),
  logLevel: 'silent',
  build: {
    write: false,
    emptyOutDir: false,
    rollupOptions: {
      input: fileURLToPath(new URL('./terminalBundleEntry.js', import.meta.url)),
      preserveEntrySignatures: 'exports-only',
    },
  },
})
const result = Array.isArray(output) ? output[0] : output
process.stdout.write(result.output.find((item) => item.type === 'chunk' && item.isEntry).code)
