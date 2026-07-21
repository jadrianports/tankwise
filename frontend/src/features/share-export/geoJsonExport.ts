// Client-side GeoJSON export: the route LineString plus one Point per
// CHOSEN stop, carrying its trip facts as properties.
// `candidate_stations[]` is DELIBERATELY never read here -- candidates are
// map texture, not trip data, and would swamp the file.
import type { Feature, FeatureCollection, LineString, Point } from 'geojson';

import type { RouteResponse } from '../../types/routeContract';

export function buildTripGeoJson(data: RouteResponse): FeatureCollection {
  const routeFeature: Feature<LineString> = {
    type: 'Feature',
    properties: {},
    geometry: { type: 'LineString', coordinates: data.route_geometry },
  };

  const stopFeatures: Feature<Point>[] = data.fuel_stops.reduce<Feature<Point>[]>((acc, stop, index) => {
    const lat = Number(stop.location?.latitude);
    const lng = Number(stop.location?.longitude);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return acc;
    acc.push({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [lng, lat] },
      properties: {
        stop_number: index + 1,
        name: stop.name,
        station_id: stop.station_id,
        distance_from_start_mi: stop.distance_from_start_mi,
        price_per_gallon: stop.price_per_gallon,
        gallons: stop.gallons,
        cost: stop.cost,
      },
    });
    return acc;
  }, []);

  return { type: 'FeatureCollection', features: [routeFeature, ...stopFeatures] };
}

function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

export function downloadTripGeoJson(data: RouteResponse, filename = 'trip-route.geojson'): void {
  const blob = new Blob([JSON.stringify(buildTripGeoJson(data), null, 2)], {
    type: 'application/geo+json',
  });
  triggerDownload(blob, filename);
}
