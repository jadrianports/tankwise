import { useCallback, useEffect, useRef, useState } from 'react';
import Map, { Marker } from 'react-map-gl/mapbox';
import type { MapRef, ViewStateChangeEvent } from 'react-map-gl/mapbox';
import type { Map as MapboxMap, GeoJSONSource } from 'mapbox-gl';
import type { Feature, LineString } from 'geojson';
import 'mapbox-gl/dist/mapbox-gl.css';
import { useColorScheme } from '@mui/material/styles';
import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';

import type { CandidateStation, FuelStop, RouteResponse } from '../../types/routeContract';
import type { FocusStopRequest } from '../../context/RoutePlanContext';
import { useMapStyle } from './useMapStyle';
import { useTerrain, getConditionalPitch } from './useTerrain';
import StyleSwitcher from './StyleSwitcher';
import CandidateToggle from './CandidateToggle';
import PriceLegend from './PriceLegend';
import ChosenStopMarker from './ChosenStopMarker';
import { applyCandidateLayer } from './layers/candidateLayer';
import JustificationPopup from '../results/JustificationPopup';

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
  // A sidebar StopList row's click (features/results), bridged through
  // App.tsx's shared RoutePlanContext -- see focusStop below.
  focusStopRequest?: FocusStopRequest | null;
}

// react-map-gl's <Map> owns the mapboxgl.Map create/destroy lifecycle
// itself (StrictMode-safe mount/cleanup, MAP-01) -- no hand-rolled
// useEffect(() => new mapboxgl.Map(...)) anywhere in this file.
function MapView({ data, token, tokenStatus, focusStopRequest }: MapViewProps) {
  const { mode } = useColorScheme();
  const isDark = mode === 'dark';

  const mapRef = useRef<MapRef | null>(null);
  const [mapInstance, setMapInstance] = useState<MapboxMap | null>(null);
  const [viewState, setViewState] = useState<CameraViewState>(DEFAULT_VIEW_STATE);

  const routeGeometryRef = useRef<[number, number][]>([]);
  routeGeometryRef.current = data?.route_geometry ?? [];

  const routeColorRef = useRef(ROUTE_COLOR.light);
  routeColorRef.current = isDark ? ROUTE_COLOR.dark : ROUTE_COLOR.light;

  // Candidate price layer (MAP-03, D-11/D-12) -- on by default (D-12).
  const [candidatesVisible, setCandidatesVisible] = useState(true);
  const candidatesVisibleRef = useRef(candidatesVisible);
  candidatesVisibleRef.current = candidatesVisible;

  const candidatesRef = useRef<CandidateStation[]>([]);
  candidatesRef.current = data?.candidate_stations ?? [];

  // Chosen-stop justification popup (UX-13, D-34) -- keyed off
  // `station_id ?? index`, same null-safe convention used throughout this
  // codebase for station lists.
  const [openStopKey, setOpenStopKey] = useState<string | number | null>(null);

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

  // Adds/updates the candidate GeoJSON source + circle layer, using the
  // current visibility ref so a toggle flip doesn't require resubscribing
  // any style.load listener.
  const applyCandidates = useCallback((map: MapboxMap) => {
    applyCandidateLayer(map, candidatesRef.current, candidatesVisibleRef.current);
  }, []);

  // Composed re-add callback: both the route line and the candidate layer
  // must survive a genuine style reload (streets<->satellite), so both are
  // re-registered from the same style.load handler (09-RESEARCH.md
  // Pitfall 1).
  const applyMapLayers = useCallback(
    (map: MapboxMap) => {
      applyRouteLine(map);
      applyCandidates(map);
    },
    [applyRouteLine, applyCandidates]
  );

  // Theme axis (no reload, UX-09) + base-style axis (streets<->satellite,
  // genuine reload, MAP-02) -- deliberately two separate mechanisms
  // (09-RESEARCH.md Pitfall 2). applyMapLayers is registered as the
  // re-add callback so the route line AND the candidate layer survive the
  // satellite/streets reload's style.load, and both run on the first load.
  const { styleUrl, isSatellite, toggleSatellite } = useMapStyle(mapInstance, isDark, applyMapLayers);
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

  // Re-draws the candidate layer whenever a new plan is solved OR the
  // toggle flips -- recomputing thresholds per response (never cached
  // across trips, since percentiles are corridor-relative, D-33).
  useEffect(() => {
    if (!mapInstance) return;
    applyCandidates(mapInstance);
  }, [mapInstance, data, candidatesVisible, applyCandidates]);

  // A new plan invalidates any open justification popup from the previous
  // solve -- never leave a popup open referencing a stale stop.
  useEffect(() => {
    setOpenStopKey(null);
  }, [data]);

  const toggleCandidates = useCallback(() => {
    setCandidatesVisible((prev) => !prev);
  }, []);

  // GL-native rewrite of the retired Leaflet App.jsx's `focusStop`, which
  // used the Leaflet marker instance's own coordinate lookup plus an
  // imperative Leaflet-popup-open call: this version flies the map's
  // camera to the stop's coordinates and opens its justification popup via
  // React state instead.
  const focusStop = useCallback((key: string | number, lng: number, lat: number) => {
    mapRef.current?.flyTo({ center: [lng, lat], zoom: 10 });
    setOpenStopKey(key);
  }, []);

  const fuelStops: FuelStop[] = data?.fuel_stops ?? [];
  const openStopEntry = fuelStops
    .map((stop, index) => ({ stop, index, key: stop.station_id ?? index }))
    .find((entry) => entry.key === openStopKey);

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

  // A StopList row (features/results, inside the sidebar) requests focus
  // via App.tsx's shared context -- resolve the request's key against
  // THIS solve's fuel_stops (station_id ?? index, the same null-safe
  // convention used everywhere else) and reuse the exact same
  // flyTo/popup-open path a marker's own click already uses. Depends on
  // `data`, not the `fuelStops` array literal below, so an unrelated
  // re-render (e.g. a style toggle) never re-fires this.
  useEffect(() => {
    if (!focusStopRequest) return;
    const stops = data?.fuel_stops ?? [];
    const entry = stops
      .map((stop, index) => ({ stop, index, key: stop.station_id ?? index }))
      .find((candidate) => candidate.key === focusStopRequest.key);
    if (!entry) return;
    const lat = toNumber(entry.stop.location?.latitude);
    const lng = toNumber(entry.stop.location?.longitude);
    if (lat === null || lng === null) return;
    focusStop(entry.key, lng, lat);
  }, [focusStopRequest, data, focusStop]);

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
        {fuelStops.map((stop, index) => {
          const key = stop.station_id ?? index;
          const lat = toNumber(stop.location?.latitude);
          const lng = toNumber(stop.location?.longitude);
          if (lat === null || lng === null) return null;
          return (
            <ChosenStopMarker
              key={key}
              stop={stop}
              number={index + 1}
              longitude={lng}
              latitude={lat}
              isOpen={openStopKey === key}
              onActivate={() => focusStop(key, lng, lat)}
            />
          );
        })}
      </Map>
      <StyleSwitcher isSatellite={isSatellite} onToggle={toggleSatellite} />
      <CandidateToggle visible={candidatesVisible} onToggle={toggleCandidates} />
      {candidatesVisible && data && data.candidate_stations.length > 0 && (
        <PriceLegend candidates={data.candidate_stations} />
      )}
      {openStopEntry && (
        <JustificationPopup
          stop={openStopEntry.stop}
          number={openStopEntry.index + 1}
          open
          onClose={() => setOpenStopKey(null)}
        />
      )}
    </Box>
  );
}

export default MapView;
