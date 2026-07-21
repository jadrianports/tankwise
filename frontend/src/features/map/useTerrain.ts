import { useEffect } from 'react';
import type { Map as MapboxMap } from 'mapbox-gl';

const DEM_SOURCE_ID = 'mapbox-dem';

// Regional zoom threshold above which the camera tilts -- terrain
// relief is essentially invisible at a coast-to-coast overview zoom, and a
// pitched national view makes reading the route harder.
const REGIONAL_ZOOM_THRESHOLD = 7;
const REGIONAL_PITCH = 55;

// Terrain is always loaded -- exaggeration 1.5, added fresh on
// every style.load, since a genuine style reload (the streets<->satellite
// swap in useMapStyle.ts) discards the DEM source and the terrain setting
// along with everything else. This hook owns its own style.load
// subscription independently of useMapStyle.ts's re-add callback --
// unlike the route line (and, later, the candidate layer), terrain
// needs no external data from MapView, so it is fully self-contained.
export function useTerrain(map: MapboxMap | null): void {
  useEffect(() => {
    if (!map) return;

    const applyTerrain = () => {
      if (!map.getSource(DEM_SOURCE_ID)) {
        map.addSource(DEM_SOURCE_ID, {
          type: 'raster-dem',
          url: 'mapbox://mapbox.mapbox-terrain-dem-v1',
          tileSize: 512,
          maxzoom: 14,
        });
      }
      map.setTerrain({ source: DEM_SOURCE_ID, exaggeration: 1.5 });
    };

    map.on('style.load', applyTerrain);
    if (map.isStyleLoaded()) {
      applyTerrain();
    }

    return () => {
      map.off('style.load', applyTerrain);
    };
  }, [map]);
}

// Pitch is a controlled-viewState value, not a terrain toggle: terrain
// relief stays loaded at every zoom, but the camera only tilts at
// regional zoom or during trip playback.
export function getConditionalPitch(zoom: number, isPlayback = false): number {
  return isPlayback || zoom > REGIONAL_ZOOM_THRESHOLD ? REGIONAL_PITCH : 0;
}
