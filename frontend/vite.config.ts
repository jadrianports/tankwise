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
    // WhiteNoise (no nginx sidecar), so the SPA code never changes
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
      // lcov is the format most coverage-consuming tools (e.g. Codecov) expect.
      reporter: ['text', 'lcov'],
      // An explicit include glob is what makes files no test ever imports
      // report at 0% instead of silently vanishing from the denominator.
      // (The vitest 3.x boolean toggle that used to do this was removed in
      // the 4.0 migration -- there is no `all: true` key on this version's
      // CoverageOptions type, so `include` is the only mechanism.)
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/test/**',
        'src/types/**',
        'src/main.tsx',
        // Imperative map-SDK integration code that drives camera and layer
        // side effects on a live map instance. A jsdom unit test here would
        // mostly assert that the module mocks in src/test/setup.ts behave,
        // not that the application does, so these two files are excluded
        // as a stated engineering judgment and the remaining surface is
        // measured honestly.
        'src/features/map/MapView.tsx',
        'src/features/playback/useChaseCam.ts',
      ],
    },
  },
})
