import { useEffect, useMemo } from 'react';
import { MapContainer, TileLayer, Polyline, Marker, Popup, useMap } from 'react-leaflet';
import { useColorScheme } from '@mui/material/styles';
import L from 'leaflet';

import FuelStopMarker from './FuelStopMarker';

const OSM_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const OSM_TILE_URL = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png';

// Continental-US default center/zoom shown before the first plan (D-07 --
// the map is always visible, it just starts empty).
const DEFAULT_CENTER = [39.8283, -98.5795];
const DEFAULT_ZOOM = 4;

// Route polyline + halo hex per the Map & Marker Contract (light/dark).
const ROUTE_STYLES = {
  light: {
    route: '#0F6D4F',
    routeWeight: 5,
    routeOpacity: 0.85,
    halo: '#FFFFFF',
    haloWeight: 8,
    haloOpacity: 0.35,
  },
  dark: {
    route: '#34C796',
    routeWeight: 5,
    routeOpacity: 0.9,
    halo: '#10151B',
    haloWeight: 8,
    haloOpacity: 0.5,
  },
};

function startIcon(mode) {
  const fill = mode === 'dark' ? '#1F8F68' : '#0A4F39';
  const text = mode === 'dark' ? '#10151B' : '#FFFFFF';
  return L.divIcon({
    className: '',
    html:
      `<div style="background:${fill};color:${text};width:32px;height:32px;` +
      'border-radius:50%;display:flex;align-items:center;justify-content:center;' +
      'font-weight:600;font-family:Inter,sans-serif;box-sizing:border-box;">S</div>',
    iconSize: [32, 32],
  });
}

function finishIcon(mode) {
  const fill = mode === 'dark' ? '#3A4550' : '#1A2027';
  const text = mode === 'dark' ? '#EDEFF2' : '#FFFFFF';
  return L.divIcon({
    className: '',
    html:
      `<div style="background:${fill};color:${text};width:32px;height:32px;` +
      'border-radius:50%;display:flex;align-items:center;justify-content:center;' +
      'font-weight:600;font-family:Inter,sans-serif;box-sizing:border-box;">F</div>',
    iconSize: [32, 32],
  });
}

function toNumber(value) {
  return typeof value === 'string' ? Number(value) : value;
}

// Fits the map to every rendered position whenever the route/stop set
// changes. Must live inside <MapContainer> to reach the Leaflet instance via
// useMap() (Leaflet flyTo/fitBounds, not a full re-render).
function FitBounds({ positions }) {
  const map = useMap();

  useEffect(() => {
    if (positions.length > 0) {
      map.fitBounds(positions, { padding: [32, 32] });
    }
  }, [map, positions]);

  return null;
}

// MapContainer + OSM TileLayer + haloed route polyline + start/finish/numbered
// fuel-stop markers. `markerRefs` is the shared ref (owned by App) that
// StopList row clicks use to focus a marker; `onMapReady` hands the Leaflet
// map instance back up to App for flyTo.
function RouteMap({ data, markerRefs, onMapReady }) {
  const { mode } = useColorScheme();
  const resolvedMode = mode === 'dark' ? 'dark' : 'light';
  const styles = ROUTE_STYLES[resolvedMode];

  // The API's route_geometry is GeoJSON [lng, lat] pairs; Leaflet wants
  // [lat, lng]. This is the single coordinate flip performed at this one data
  // boundary -- never scattered per-component (CLAUDE.md note, Pitfall 3). No
  // polyline-decode package is used or needed: route_geometry is already a
  // plain coordinate array, not an encoded polyline string.
  const routePositions = useMemo(() => {
    if (!data?.route_geometry) return [];
    return data.route_geometry.map(([lng, lat]) => [lat, lng]);
  }, [data]);

  const stops = data?.fuel_stops ?? [];

  const startPosition = useMemo(
    () => (data?.start ? [toNumber(data.start.latitude), toNumber(data.start.longitude)] : null),
    [data]
  );
  const finishPosition = useMemo(
    () => (data?.finish ? [toNumber(data.finish.latitude), toNumber(data.finish.longitude)] : null),
    [data]
  );

  const allPositions = useMemo(() => {
    const positions = [...routePositions];
    if (startPosition) positions.push(startPosition);
    if (finishPosition) positions.push(finishPosition);
    return positions;
  }, [routePositions, startPosition, finishPosition]);

  return (
    <MapContainer center={DEFAULT_CENTER} zoom={DEFAULT_ZOOM} style={{ height: '100%', width: '100%' }} ref={onMapReady}>
      <TileLayer attribution={OSM_ATTRIBUTION} url={OSM_TILE_URL} />

      {routePositions.length > 0 && (
        <>
          {/* Halo (wider, underneath) then the saturated route line on top. */}
          <Polyline
            positions={routePositions}
            pathOptions={{ color: styles.halo, weight: styles.haloWeight, opacity: styles.haloOpacity }}
          />
          <Polyline
            positions={routePositions}
            pathOptions={{ color: styles.route, weight: styles.routeWeight, opacity: styles.routeOpacity }}
          />
        </>
      )}

      {startPosition && (
        <Marker position={startPosition} icon={startIcon(resolvedMode)}>
          <Popup>Start</Popup>
        </Marker>
      )}

      {finishPosition && (
        <Marker position={finishPosition} icon={finishIcon(resolvedMode)}>
          <Popup>Finish</Popup>
        </Marker>
      )}

      {stops.map((stop, index) => {
        const key = stop.station_id ?? index;
        return (
          <FuelStopMarker
            key={key}
            stop={stop}
            number={index + 1}
            mode={resolvedMode}
            markerRef={(marker) => {
              markerRefs.current[key] = marker;
            }}
          />
        );
      })}

      {allPositions.length > 0 && <FitBounds positions={allPositions} />}
    </MapContainer>
  );
}

export default RouteMap;
