// Vitest setup file (referenced by vite.config.ts's test.setupFiles).
import '@testing-library/jest-dom/vitest';
import { vi } from 'vitest';

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
