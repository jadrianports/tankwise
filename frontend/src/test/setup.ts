// Vitest setup file (referenced by vite.config.ts's test.setupFiles).
import '@testing-library/jest-dom/vitest';
import { afterEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

// This project does not enable vitest's `test.globals` option, so
// Testing Library's own auto-registered afterEach(cleanup) (which only
// self-installs when it detects global test hooks) never fires --
// verified empirically: a probe spec that rendered in one test without
// calling cleanup() left that render's DOM node present in document.body
// for the very next test in the same file. Any suite that renders without
// its own explicit cleanup() leaves that leftover markup around for
// whatever test runs next in the file, which is exactly the shape of a
// Testing Library strict-mode "found multiple elements" failure. Do not
// delete this as redundant with a suite's own local cleanup() calls --
// cleanup() is idempotent (a no-op on an already-unmounted root), and this
// is the backstop for suites that don't call it themselves.
afterEach(() => {
  cleanup();
});

// jsdom has no WebGL, so any component tree that transitively imports the
// map (MapView -> mapbox-gl / react-map-gl) needs both mocked at the module
// boundary before it ever renders. Mocked globally here so individual test
// files never need to repeat this boilerplate. Each factory is fully
// self-contained (no outer-scope references) since vi.mock is hoisted above
// the rest of this file.
vi.mock('mapbox-gl', () => ({
  default: {
    Map: vi.fn(() => ({
      on: vi.fn(),
      off: vi.fn(),
      remove: vi.fn(),
      addSource: vi.fn(),
      addLayer: vi.fn(),
      getSource: vi.fn(),
      getLayer: vi.fn(),
      flyTo: vi.fn(),
    })),
    NavigationControl: vi.fn(),
    Marker: vi.fn(() => ({
      setLngLat: vi.fn().mockReturnThis(),
      addTo: vi.fn().mockReturnThis(),
      remove: vi.fn(),
    })),
  },
}));

vi.mock('react-map-gl/mapbox', () => ({
  default: () => null,
  Map: () => null,
  Source: () => null,
  Layer: () => null,
  Marker: () => null,
  NavigationControl: () => null,
}));
