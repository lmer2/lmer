import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests/setup.js'],
    include: ['tests/**/*.test.js'],
    maxWorkers: 1,
    testTimeout: 10000,
    server: { deps: { inline: ['vuetify'] } },
  },
})
