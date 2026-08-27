import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vuetify from 'vite-plugin-vuetify'

// The daemon serves the built assets itself (no CDN, no external hosts), so
// everything is bundled and referenced with relative paths.
export default defineConfig({
  // vite-plugin-vuetify resolves the components each template actually uses, so
  // the bundle carries those and not the whole framework. Styles stay Vuetify's
  // precompiled CSS — the sass path would buy a smaller stylesheet at the price
  // of a compiler in the operator's `npm ci`.
  plugins: [vue(), vuetify()],
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // Stable, named output: one entry, plus one chunk per deliberate split. There
    // are three, and all of them exist because the fleet view — the screen a phone
    // loads on every glance — must not pay for something only a run view uses. The
    // terminal: xterm is more than half the JS and only the run-detail view opens
    // one. The markdown renderer: markdown-it plus DOMPurify are ~59 kB gzipped and
    // the fleet view renders no markdown at all. And the sketch pad (issue 246): a
    // canvas editor that only opens when an operator taps "mark up" on an image
    // they have already attached, which most sends never do. Each is reached with
    // defineAsyncComponent by every consumer — one static import anywhere pulls the
    // whole thing back into the entry chunk, which is why the guard in
    // tests/test_platform_web_bundle.py asserts against the built output rather
    // than the source. Nothing extra is needed to serve them: the daemon's
    // /assets/{path} route serves anything inside dist/assets, and the chunk and
    // its stylesheet land there under the names below.
    rollupOptions: {
      output: {
        entryFileNames: 'assets/app.js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/[name][extname]',
      },
    },
  },
  server: {
    // `npm run dev` proxies the API to a locally running daemon so the app can
    // be developed with hot reload against real data.
    proxy: {
      '/api': {
        target: process.env.LMER_PLATFORM_URL || 'http://127.0.0.1:8600',
        changeOrigin: true,
      },
    },
  },
})
