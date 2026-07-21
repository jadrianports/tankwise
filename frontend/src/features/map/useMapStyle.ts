import { useCallback, useEffect, useRef, useState } from 'react';
import type { Map as MapboxMap } from 'mapbox-gl';

export const STREETS_STYLE = 'mapbox://styles/mapbox/standard';
export const SATELLITE_STYLE = 'mapbox://styles/mapbox/standard-satellite';

export interface UseMapStyleResult {
  styleUrl: string;
  isSatellite: boolean;
  toggleSatellite: () => void;
}

// Two deliberately SEPARATE axes (09-RESEARCH.md Pitfall 2 -- do not merge
// into one "theme" hook):
//
// - Theme axis (isDark, UX-09): `setConfigProperty` reconfigures the
//   CURRENT style in place -- no reload, no style.load refire (D-31,
//   confirmed against current Mapbox Standard behavior).
// - Base-style axis (streets<->satellite, MAP-02): a genuine style
//   reload, driven by the `mapStyle` prop this hook exposes as `styleUrl`.
//   `mapbox://styles/mapbox/standard` and `...standard-satellite` are two
//   separate style documents -- switching between them discards every
//   source/layer added after initial load. `onStyleLoad` is owned by the
//   caller (MapView, since it knows about the route line and, later, the
//   candidate layer) and is invoked on every style.load, re-adding
//   everything from scratch.
export function useMapStyle(
  map: MapboxMap | null,
  isDark: boolean,
  onStyleLoad?: (map: MapboxMap) => void
): UseMapStyleResult {
  const [isSatellite, setIsSatellite] = useState(false);
  const onStyleLoadRef = useRef(onStyleLoad);
  onStyleLoadRef.current = onStyleLoad;

  const styleUrl = isSatellite ? SATELLITE_STYLE : STREETS_STYLE;

  // Theme axis: fires whenever isDark changes, independent of any style
  // reload -- this is the case a satellite/streets swap does NOT cover,
  // since setConfigProperty never refires style.load.
  useEffect(() => {
    if (!map) return;
    map.setConfigProperty('basemap', 'lightPreset', isDark ? 'night' : 'day');
  }, [map, isDark]);

  // Base-style axis: register the re-add BEFORE the swap so it fires the
  // moment the new style finishes loading (09-RESEARCH.md Pattern
  // 2/Pitfall 1). Also covers the very first load, since style.load fires
  // then too -- so this is the one place the route line (and later the
  // candidate layer) gets added at all.
  useEffect(() => {
    if (!map) return;

    const handleStyleLoad = () => {
      onStyleLoadRef.current?.(map);
      // A freshly loaded style starts at its own default lightPreset --
      // reapply the current theme choice immediately after every reload.
      map.setConfigProperty('basemap', 'lightPreset', isDark ? 'night' : 'day');
    };

    map.on('style.load', handleStyleLoad);
    if (map.isStyleLoaded()) {
      handleStyleLoad();
    }

    return () => {
      map.off('style.load', handleStyleLoad);
    };
  }, [map, isDark]);

  const toggleSatellite = useCallback(() => {
    setIsSatellite((prev) => !prev);
  }, []);

  return { styleUrl, isSatellite, toggleSatellite };
}
