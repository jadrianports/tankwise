import { useCallback, useEffect, useRef, useState } from 'react';
import Map, { Marker } from 'react-map-gl/mapbox';
import type { MapRef, ViewStateChangeEvent } from 'react-map-gl/mapbox';
import type { Map as MapboxMap, GeoJSONSource } from 'mapbox-gl';
import type { Feature, LineString } from 'geojson';
import 'mapbox-gl/dist/mapbox-gl.css';
import { useColorScheme } from '@mui/material/styles';
import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';

import type { RouteResponse } from '../../types/routeContract';
import { useMapStyle } from './useMapStyle';
import { useTerrain, getConditionalPitch } from './useTerrain';
import StyleSwitcher from './StyleSwitcher';

const ROUTE_SOURCE_ID = 'route-line';

// Route polyline hex (light/dark) -- primary green, never fuel amber
// (09-UI-SPEC.md: "route line color = primary/neutral not fuel amber").
const ROUTE_COLOR = { light: '#0F6D4F', dark: '#34C796' };

// Continental-US default center/zoom shown before the first solve (D-39) --
// carried over from the retired Leaflet RouteMap.jsx.
const DEFAULT_VIEW_STATE: CameraViewState = {
  longitude: -98.5795,
  latitude: 39.8283,
  zoom: 4,
  bearing: 0,
  pitch: 0,
};

interface CameraViewState {
  longitude: number;
  latitude: number;
  zoom: number;
  bearing: number;
  pitch: number;
}

function toNumber(value: string | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export interface MapViewProps {
  data: RouteResponse | null;
  token: string | null;
  tokenStatus: 'loading' | 'ready' | 'error';
}

// react-map-gl's <Map> owns the mapboxgl.Map create/destroy lifecycle
// itself (StrictMode-safe mount/cleanup, MAP-01) -- no hand-rolled
// useEffect(() => new mapboxgl.Map(...)) anywhere in this file.
function MapView({ data, token, tokenStatus }: MapViewProps) {
  const { mode } = useColorScheme();
  const isDark = mode === 'dark';

  const mapRef = useRef<MapRef | null>(null);
  const [mapInstance, setMapInstance] = useState<MapboxMap | null>(null);
  const [viewState, setViewState] = useState<CameraViewState>(DEFAULT_VIEW_STATE);

  const routeGeometryRef = useRef<[number, number][]>([]);
  routeGeometryRef.current = data?.route_geometry ?? [];

  const routeColorRef = useRef(ROUTE_COLOR.light);
  routeColorRef.current = isDark ? ROUTE_COLOR.dark : ROUTE_COLOR.light;

  // Adds the route line the first time it's needed and updates it in
  // place on every later call -- the API's route_geometry is already
  // [lng, lat] GeoJSON order, exactly what a Mapbox GL source wants (no
  // [lng,lat]->[lat,lng] flip like the retired Leaflet RouteMap.jsx did).
  const applyRouteLine = useCallback((map: MapboxMap) => {
    const coordinates = routeGeometryRef.current;
    if (coordinates.length === 0) return;

    const geojson: Feature<LineString> = {
      type: 'Feature',
      properties: {},
      geometry: { type: 'LineString', coordinates },
    };

    const source = map.getSource(ROUTE_SOURCE_ID) as GeoJSONSource | undefined;
    if (source) {
      source.setData(geojson);
      return;
    }

    map.addSource(ROUTE_SOURCE_ID, { type: 'geojson', data: geojson });
    map.addLayer({
      id: ROUTE_SOURCE_ID,
      type: 'line',
      source: ROUTE_SOURCE_ID,
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: {
        'line-color': routeColorRef.current,
        'line-width': 5,
        'line-opacity': 0.9,
      },
    });
  }, []);

  // Theme axis (no reload, UX-09) + base-style axis (streets<->satellite,
  // genuine reload, MAP-02) -- deliberately two separate mechanisms
  // (09-RESEARCH.md Pitfall 2). applyRouteLine is registered as the
  // re-add callback so the route line survives the satellite/streets
  // reload's style.load, and it also runs on the very first load.
  const { styleUrl, isSatellite, toggleSatellite } = useMapStyle(mapInstance, isDark, applyRouteLine);
  useTerrain(mapInstance);

  const handleLoad = useCallback(() => {
    setMapInstance(mapRef.current?.getMap() ?? null);
  }, []);

  const handleMove = useCallback((evt: ViewStateChangeEvent) => {
    const { longitude, latitude, zoom, bearing, pitch } = evt.viewState;
    setViewState({ longitude, latitude, zoom, bearing, pitch });
  }, []);

  // Re-draws the route line whenever a new plan is solved, including on
  // an unchanged style/camera -- a later slider re-solve must update the
  // line without moving the camera (see the fitBounds effect below).
  useEffect(() => {
    if (!mapInstance) return;
    applyRouteLine(mapInstance);
  }, [mapInstance, data, applyRouteLine]);

  const startLng = toNumber(data?.start?.longitude);
  const startLat = toNumber(data?.start?.latitude);
  const finishLng = toNumber(data?.finish?.longitude);
  const finishLat = toNumber(data?.finish?.latitude);

  // Camera holds position on every re-solve (D-16): fitBounds runs ONLY
  // from an effect scoped to the resolved start/finish coordinates, never
  // to every new plan response -- a later slider re-solve keeps the same
  // start/finish and must never move the camera.
  useEffect(() => {
    if (
      !mapRef.current ||
      startLng === null ||
      startLat === null ||
      finishLng === null ||
      finishLat === null
    ) {
      return;
    }
    mapRef.current.fitBounds(
      [
        [Math.min(startLng, finishLng), Math.min(startLat, finishLat)],
        [Math.max(startLng, finishLng), Math.max(startLat, finishLat)],
      ],
      { padding: 64, duration: 800 }
    );
  }, [startLat, startLng, finishLat, finishLng]);

  // D-08: a missing/misconfigured pk. token shows a config-error only in
  // this map pane -- the sidebar planner keeps working regardless.
  if (tokenStatus !== 'ready' || !token) {
    return (
      <Box
        sx={{
          height: '100%',
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          textAlign: 'center',
          p: 4,
          bgcolor: 'background.paper',
        }}
      >
        <Typography variant="body1" color="text.secondary">
          {tokenStatus === 'loading'
            ? 'Loading map…'
            : 'Map unavailable — the interactive map needs a valid Mapbox token. The route planner below still works.'}
        </Typography>
      </Box>
    );
  }

  return (
    <Box sx={{ position: 'relative', height: '100%', width: '100%' }}>
      <Map
        {...viewState}
        pitch={getConditionalPitch(viewState.zoom)}
        ref={mapRef}
        mapboxAccessToken={token}
        mapStyle={styleUrl}
        onMove={handleMove}
        onLoad={handleLoad}
        style={{ width: '100%', height: '100%' }}
      >
        {startLng !== null && startLat !== null && (
          <Marker longitude={startLng} latitude={startLat} anchor="center">
            <Box
              sx={{
                width: 32,
                height: 32,
                borderRadius: '50%',
                bgcolor: isDark ? '#1F8F68' : '#0A4F39',
                color: isDark ? '#10151B' : '#FFFFFF',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontWeight: 600,
                boxSizing: 'border-box',
              }}
            >
              S
            </Box>
          </Marker>
        )}
        {finishLng !== null && finishLat !== null && (
          <Marker longitude={finishLng} latitude={finishLat} anchor="center">
            <Box
              sx={{
                width: 32,
                height: 32,
                borderRadius: '50%',
                bgcolor: isDark ? '#3A4550' : '#1A2027',
                color: isDark ? '#EDEFF2' : '#FFFFFF',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontWeight: 600,
                boxSizing: 'border-box',
              }}
            >
              F
            </Box>
          </Marker>
        )}
      </Map>
      <StyleSwitcher isSatellite={isSatellite} onToggle={toggleSatellite} />
    </Box>
  );
}

export default MapView;
