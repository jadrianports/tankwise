import { useEffect, useState } from 'react';
import type { Map as MapboxMap } from 'mapbox-gl';

import { assembleElevationProfile, buildEvenSamples } from './elevationMath';
import type { ElevationProfile } from '../../types/elevationProfile';

// Idle-gated, re-runnable terrain sampler (ELEV-01) -- sibling hook to
// useTerrain.ts, mirroring its style.load subscription discipline
// exactly. queryTerrainElevation() is a synchronous read against
// whatever DEM tiles are already decoded in GPU/tile memory: it does
// NOT wait or trigger a fetch, so the first sample pass must wait for
// the map's own `idle` event (tiles settled) or the series comes back
// mostly/entirely null. A satellite/streets style swap discards and
// re-adds the DEM source (see useTerrain.ts's own header comment), so
// this hook independently resubscribes to `style.load` and re-samples,
// idle-gated again, every time.
export function useElevationProfile(map: MapboxMap | null, routeGeometry: [number, number][]): ElevationProfile | null {
  const [profile, setProfile] = useState<ElevationProfile | null>(null);

  useEffect(() => {
    if (!map || routeGeometry.length < 2) {
      setProfile(null);
      return;
    }

    let cancelled = false;

    const sample = () => {
      if (cancelled) return;

      // Guarded defensively: a malformed/adversarial route_geometry
      // (e.g. a crafted share-URL-decoded trip) must degrade to an
      // all-null -> 'unavailable' profile rather than throw and take
      // down the results panel (T-14-01).
      let result: ElevationProfile;
      try {
        const samples = buildEvenSamples(routeGeometry);
        const rawMeters = samples.map((point) => {
          if (!Number.isFinite(point.lng) || !Number.isFinite(point.lat)) return null;
          // { exaggerated: false } is mandatory at this call site -- the
          // ONLY elevation read in this hook -- so values are real-world
          // meters, not useTerrain.ts's 1.5x visual exaggeration.
          const elevation = map.queryTerrainElevation([point.lng, point.lat], { exaggerated: false });
          return typeof elevation === 'number' && Number.isFinite(elevation) ? elevation : null;
        });
        result = assembleElevationProfile(samples, rawMeters);
      } catch {
        result = assembleElevationProfile([], []);
      }

      if (!cancelled) setProfile(result);
    };

    // First sample: wait for whatever idle state is already pending
    // (mirrors Mapbox's own official examples, which await map.once('idle')
    // before the first chart/marker draw -- Pitfall 1).
    if (map.isMoving() || !map.areTilesLoaded()) {
      map.once('idle', sample);
    } else {
      sample();
    }

    // Re-sample whenever a style reload re-adds terrain (satellite/streets
    // swap tears down and rebuilds the DEM source, per useTerrain.ts) --
    // gate this re-sample on idle too, since the reload itself triggers a
    // burst of tile loads (Pitfall 2).
    const handleStyleLoad = () => {
      map.once('idle', sample);
    };
    map.on('style.load', handleStyleLoad);

    return () => {
      cancelled = true;
      map.off('style.load', handleStyleLoad);
    };
    // routeGeometry is a new array reference on every solve (new `data`),
    // which is exactly the intended re-sample trigger -- re-sampling is
    // idempotent and cheap (a few hundred queryTerrainElevation calls).
  }, [map, routeGeometry]);

  return profile;
}
