// Imported from 'vitest/config' (not 'vite') so the `test` key below is
// type-checked too -- both work at runtime, but this keeps the Vitest
// options honest against typos.
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // In `npm run dev`, forward the SPA's relative /api calls to the Django dev
    // server. In Docker, this same gunicorn service serves /api directly via
    // WhiteNoise (no nginx sidecar, D-07), so the SPA code never changes
    // between dev and production. It always fetches a relative /api/route.
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    coverage: {
      provider: 'v8',
      // lcov feeds the Codecov frontend flag wired up in a later plan.
      reporter: ['text', 'lcov'],
    },
  },
})
