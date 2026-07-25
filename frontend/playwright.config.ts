import { defineConfig, devices } from '@playwright/test';

// Drives the launch-asset capture pipeline (README screenshots, the OG
// card's route backdrop, and this plan's throwaway smoke proof). This is
// NOT a test-runner config in the vitest sense -- every spec it collects
// writes committed launch assets (or, for the smoke spec, a throwaway
// reproducibility proof), never a pass/fail regression check on
// application behavior. `npm test` (vitest) and `npm run capture` (this
// file) are two separate runners over two separate file sets -- see the
// test-file-matching option below for how that separation is enforced.
//
// The default base URL points at the docker-compose stack (port 80, the
// single gunicorn+WhiteNoise service), not the Vite dev server -- that's
// the topology production actually runs (SPA + API on one origin), and
// it's what a reviewer's browser sees. An env override re-points every
// spec at the live Render deployment for a later plan's browser smoke
// test, with zero config-file changes.
export default defineConfig({
  testDir: './scripts',
  // Load-bearing: vitest's default include already owns `*.test.*` and
  // `*.spec.*` (frontend/vite.config.ts), so the two runners must be
  // unable to collect each other's files -- `npm test` must never try to
  // drive a browser, and `npm run capture` must never try to run the
  // unit suite.
  testMatch: '**/*.capture.ts',
  // Capture specs write PNGs to fixed paths -- parallel workers or a
  // retried run would race the same file or silently double-write it.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: 'list',
  // A real route solve against a cold cache makes a live Mapbox
  // Directions call plus a corridor scan; the default 30s is too tight.
  timeout: 120_000,
  use: {
    baseURL: process.env.CAPTURE_BASE_URL ?? 'http://localhost',
    viewport: { width: 1280, height: 800 },
    deviceScaleFactor: 2,
    colorScheme: 'light',
    // A Playwright trace records every network request, and this app's
    // own GET /api/config response plus the Mapbox tile requests carry
    // the public pk. token -- a trace file is therefore a
    // credential-bearing artifact that must never be produced by default.
    trace: 'off',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});

// Every capture spec in this phase resolves its output directory through
// this constant rather than a hardcoded literal, so a later plan can
// re-point the same specs at a throwaway directory when it re-runs them
// against the live host as a smoke test, without ever overwriting an
// already-committed screenshot.
const resolvedOutDir =
  process.env.CAPTURE_OUT_DIR ?? '../docs/screenshots';
export const CAPTURE_OUT_DIR = resolvedOutDir;
