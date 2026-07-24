import { useEffect, useRef, useState, useSyncExternalStore, type FormEvent } from 'react';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import IconButton from '@mui/material/IconButton';
import LinearProgress from '@mui/material/LinearProgress';
import Typography from '@mui/material/Typography';
import AddIcon from '@mui/icons-material/Add';
import SwapVertIcon from '@mui/icons-material/SwapVert';
import MyLocationIcon from '@mui/icons-material/MyLocation';
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core';
import { SortableContext, verticalListSortingStrategy, arrayMove, sortableKeyboardCoordinates } from '@dnd-kit/sortable';

import AddressAutocomplete, { type ResolvedAddress } from './AddressAutocomplete';
import DemoTripChips from './DemoTripChips';
import StopRow, { type MiddleStop } from './StopRow';
import InfeasibleRouteCallout, { type OrderedStop } from './InfeasibleRouteCallout';
import { useRoutePlanContext } from '../../context/RoutePlanContext';
import { useRecentTrips } from '../recent-trips/useRecentTrips';
import { getLoadTripRequestSnapshot, subscribeLoadTripRequest } from '../share-export/tripState';
import { HERO_VEHICLE_PRESET_ID, type DemoTrip } from '../../constants/presets';
import { fetchConfig } from '../../api/configClient';
import type { InfeasibleRouteDetail } from '../../types/routeContract';

interface FieldState {
  value: string; // resolved value sent to POST /api/route (coords or address string)
  label: string; // human-readable, client-side only
}

const EMPTY_FIELD: FieldState = { value: '', label: '' };

// Start (always "A") + every middle stop + Finish (always the last letter)
// = 10 total stops max (D-12) -- Mapbox's own 25-coordinate cap is never
// approached. The server-side backstop (`waypoints` ListField
// max_length=8, 13-02) sits independently below this UI affordance.
const MAX_STOPS = 10;

let middleStopIdCounter = 0;
function createMiddleStopId(): string {
  middleStopIdCounter += 1;
  return `stop-${middleStopIdCounter}`;
}

function createEmptyMiddleStop(): MiddleStop {
  return { id: createMiddleStopId(), value: '', label: '' };
}

// Middle stops sit between anchored Start (letter A) and Finish (the last
// letter) -- e.g. 2 middle stops -> Start=A, stops=B,C, Finish=D.
function middleStopLetter(index: number): string {
  return String.fromCharCode(65 + 1 + index);
}

// Demo trip labels are "A → B" for a plain trip or "A → B → C" for a
// multi-stop one (constants/presets.ts) -- split on every arrow rather
// than just the first: the FIRST segment is always the start label, the
// LAST is always the finish label, and everything between is a middle
// stop's display label (positionally matched against trip.waypoints,
// same order). A 2-segment label degrades to middleLabels: [], the exact
// pre-multi-stop behavior.
function splitDemoLabel(label: string): { startLabel: string; finishLabel: string; middleLabels: string[] } {
  const parts = label
    .split('→')
    .map((part) => part.trim())
    .filter(Boolean);
  if (parts.length === 0) return { startLabel: label, finishLabel: label, middleLabels: [] };
  return {
    startLabel: parts[0],
    finishLabel: parts[parts.length - 1],
    middleLabels: parts.slice(1, -1),
  };
}

// The planner input surface: two address-autocomplete fields,
// swap/geolocate controls, the "Find Cheapest Route" CTA, and the
// long-haul demo trip chips. Fills the first Sidebar slot; reads/writes
// shared solve state via useRoutePlanContext() rather than prop-drilling
// through Sidebar.tsx or App.tsx.
function PlannerFormSection() {
  const { status, solve, error } = useRoutePlanContext();
  const { add: addRecentTrip } = useRecentTrips();
  const isLoading = status === 'loading';

  const [start, setStart] = useState<FieldState>(EMPTY_FIELD);
  const [finish, setFinish] = useState<FieldState>(EMPTY_FIELD);
  // Planner opens as just anchored Start/Finish (D-03 empty state) --
  // intermediate stops are added on demand via "+ Add stop" and are the
  // only entries that can be added, removed, or (Task 2) reordered.
  const [middleStops, setMiddleStops] = useState<MiddleStop[]>([]);
  // Reorder announcement (D-02, WAY-02): updated by every reorder path --
  // pointer drag, @dnd-kit's KeyboardSensor pick-up-then-arrow flow, and
  // the explicit up/down IconButtons -- so an aria-live region announces
  // regardless of HOW the reorder happened, not just drag.
  const [reorderAnnouncement, setReorderAnnouncement] = useState('');
  const [geoLoading, setGeoLoading] = useState(false);
  const [geoError, setGeoError] = useState<string | null>(null);

  const stopSensors = useSensors(
    useSensor(PointerSensor),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  );

  // pk. token for the Search Box calls, fetched independently of App.tsx's
  // own GET /api/config call (MapView's copy) -- the endpoint is lean and
  // unthrottled by design, and keeping this self-contained avoids growing
  // App.tsx/RoutePlanContext.ts for a single feature's dependency.
  const [tokenState, setTokenState] = useState<{ status: 'loading' | 'ready' | 'error'; token: string | null }>({
    status: 'loading',
    token: null,
  });

  useEffect(() => {
    let cancelled = false;
    fetchConfig().then((result) => {
      if (cancelled) return;
      setTokenState(
        result.ok ? { status: 'ready', token: result.data.mapbox_public_token } : { status: 'error', token: null }
      );
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // A recent-trip click (RecentTripsSection, a sibling Sidebar section)
  // arrives through tripState.ts's tiny cross-section store rather than
  // App.tsx/RoutePlanContext.ts -- see tripState.ts's own comment for why.
  const loadTripRequest = useSyncExternalStore(
    subscribeLoadTripRequest,
    getLoadTripRequestSnapshot,
    getLoadTripRequestSnapshot
  );
  const lastHandledNonceRef = useRef(0);

  useEffect(() => {
    if (!loadTripRequest || loadTripRequest.nonce === lastHandledNonceRef.current) return;
    lastHandledNonceRef.current = loadTripRequest.nonce;
    const { trip } = loadTripRequest;
    const waypoints = trip.waypoints ?? [];
    setStart({ value: trip.start, label: trip.startLabel });
    setFinish({ value: trip.finish, label: trip.finishLabel });
    // TripState's waypoints are plain "lat,lng"/address strings with no
    // separate per-waypoint label field -- the row's label falls back to
    // its own value, same as a hand-typed address before autocomplete
    // resolves it.
    setMiddleStops(waypoints.map((value) => ({ id: createMiddleStopId(), value, label: value })));
    void solve(trip.start, trip.finish, waypoints.length > 0 ? waypoints : undefined);
  }, [loadTripRequest, solve]);

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    const startValue = start.value.trim();
    const finishValue = finish.value.trim();
    if (!startValue || !finishValue || isLoading) return;
    const waypointValues = middleStops.map((stop) => stop.value.trim()).filter(Boolean);
    void solve(startValue, finishValue, waypointValues.length > 0 ? waypointValues : undefined);
    addRecentTrip({
      start: startValue,
      finish: finishValue,
      startLabel: start.label || startValue,
      finishLabel: finish.label || finishValue,
      vehicle: HERO_VEHICLE_PRESET_ID,
      waypoints: waypointValues.length > 0 ? waypointValues : undefined,
    });
  };

  const handleSwap = () => {
    setStart(finish);
    setFinish(start);
  };

  const totalStops = 2 + middleStops.length;
  const isAtStopCap = totalStops >= MAX_STOPS;

  const handleAddStop = () => {
    if (isAtStopCap) return;
    setMiddleStops((stops) => [...stops, createEmptyMiddleStop()]);
  };

  const handleStopChange = (id: string, result: ResolvedAddress) => {
    setMiddleStops((stops) => stops.map((stop) => (stop.id === id ? { ...stop, ...result } : stop)));
  };

  const handleRemoveStop = (id: string) => {
    setMiddleStops((stops) => stops.filter((stop) => stop.id !== id));
  };

  // Single reorder path shared by pointer drag, the KeyboardSensor's
  // pick-up-then-arrow flow (both via handleDragEnd below), and the
  // explicit up/down IconButtons (D-01: middle stops only, never cross
  // Start/Finish since fromIndex/toIndex are always within middleStops).
  const reorderMiddleStops = (fromIndex: number, toIndex: number) => {
    setMiddleStops((stops) => {
      if (fromIndex < 0 || fromIndex >= stops.length || toIndex < 0 || toIndex >= stops.length) return stops;
      if (fromIndex === toIndex) return stops;
      const movedLetter = middleStopLetter(fromIndex);
      setReorderAnnouncement(`Stop ${movedLetter} moved to position ${toIndex + 1} of ${stops.length}.`);
      return arrayMove(stops, fromIndex, toIndex);
    });
  };

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const fromIndex = middleStops.findIndex((stop) => stop.id === active.id);
    const toIndex = middleStops.findIndex((stop) => stop.id === over.id);
    if (fromIndex === -1 || toIndex === -1) return;
    reorderMiddleStops(fromIndex, toIndex);
  };

  const handleGeolocate = () => {
    if (!('geolocation' in navigator)) {
      setGeoError('Geolocation is not supported by this browser.');
      return;
    }
    setGeoError(null);
    setGeoLoading(true);
    navigator.geolocation.getCurrentPosition(
      (position) => {
        const { latitude, longitude } = position.coords;
        setStart({ value: `${latitude},${longitude}`, label: 'Current location' });
        setGeoLoading(false);
      },
      () => {
        setGeoError('Could not determine your location.');
        setGeoLoading(false);
      },
      { enableHighAccuracy: false, timeout: 10_000 }
    );
  };

  const handleDemoTripSelect = (trip: DemoTrip) => {
    const { startLabel, finishLabel, middleLabels } = splitDemoLabel(trip.label);
    const waypoints = trip.waypoints ?? [];
    setStart({ value: trip.start, label: startLabel });
    setFinish({ value: trip.finish, label: finishLabel });
    setMiddleStops(
      waypoints.map((value, index) => ({
        id: createMiddleStopId(),
        value,
        label: middleLabels[index] ?? value,
      }))
    );
    void solve(trip.start, trip.finish, waypoints.length > 0 ? waypoints : undefined);
    addRecentTrip({
      start: trip.start,
      finish: trip.finish,
      startLabel,
      finishLabel,
      vehicle: HERO_VEHICLE_PRESET_ID,
      waypoints: waypoints.length > 0 ? waypoints : undefined,
    });
  };

  const isEmptyState = !start.value && !finish.value;

  // A/B/C-lettered ordered stop list (D-04/D-08), the same scheme the map
  // pins and leg-breakdown boundary rows use -- built here since this
  // component already owns start/middleStops/finish state, rather than
  // re-derived inside InfeasibleRouteCallout.
  const orderedStops: OrderedStop[] = [
    { letter: 'A', label: start.label || start.value || 'Start' },
    ...middleStops.map((stop, index) => ({
      letter: middleStopLetter(index),
      label: stop.label || stop.value || `Stop ${middleStopLetter(index)}`,
    })),
    { letter: middleStopLetter(middleStops.length), label: finish.label || finish.value || 'Finish' },
  ];

  // The two bounding stop letters for a named-leg infeasible error
  // (D-08) -- empty for a legacy/2-point infeasible error (`leg_index`
  // null) or any other error code, in which case no row is highlighted.
  const infeasibleDetail =
    error?.code === 'infeasible_route' ? (error.detail as InfeasibleRouteDetail | undefined) : undefined;
  const infeasibleLegIndex = infeasibleDetail?.leg_index;
  const highlightedLetters = new Set<string>();
  if (infeasibleLegIndex !== null && infeasibleLegIndex !== undefined) {
    const fromLetter = orderedStops[infeasibleLegIndex]?.letter;
    const toLetter = orderedStops[infeasibleLegIndex + 1]?.letter;
    if (fromLetter) highlightedLetters.add(fromLetter);
    if (toLetter) highlightedLetters.add(toLetter);
  }
  const highlightSx = { outline: '2px solid', outlineColor: 'error.main', borderRadius: 1, p: 0.5, m: -0.5 };

  return (
    <Box>
      <Typography variant="h6" component="h2" gutterBottom>
        Plan your route
      </Typography>
      {isEmptyState && (
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Enter a start and finish, or try a real long-haul route below to see the cheapest fueling plan.
        </Typography>
      )}

      {/* Reorder announcements (D-02, WAY-02): visually hidden, always
          present in the DOM so screen readers pick up text updates from
          any reorder path (drag, KeyboardSensor, or the up/down buttons). */}
      <Box
        role="status"
        aria-live="polite"
        data-testid="reorder-announcements"
        sx={{
          position: 'absolute',
          width: 1,
          height: 1,
          overflow: 'hidden',
          clip: 'rect(0 0 0 0)',
          whiteSpace: 'nowrap',
        }}
      >
        {reorderAnnouncement}
      </Box>

      <Box component="form" onSubmit={handleSubmit} sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        <Box sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}>
          <Box sx={{ flexGrow: 1, display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
            <Box
              data-testid="stop-row-A"
              data-highlighted={highlightedLetters.has('A') || undefined}
              sx={highlightedLetters.has('A') ? highlightSx : undefined}
            >
              <AddressAutocomplete
                label="Start"
                token={tokenState.token}
                displayValue={start.label}
                disabled={isLoading}
                onResolve={(result: ResolvedAddress) => setStart({ value: result.value, label: result.label })}
              />
            </Box>

            {middleStops.length > 0 && (
              <DndContext sensors={stopSensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
                <SortableContext
                  items={middleStops.map((stop) => stop.id)}
                  strategy={verticalListSortingStrategy}
                >
                  <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                    {middleStops.map((stop, index) => {
                      const letter = middleStopLetter(index);
                      return (
                        <Box
                          key={stop.id}
                          data-testid={`stop-row-${letter}`}
                          data-highlighted={highlightedLetters.has(letter) || undefined}
                          sx={highlightedLetters.has(letter) ? highlightSx : undefined}
                        >
                          <StopRow
                            stop={stop}
                            letter={letter}
                            token={tokenState.token}
                            disabled={isLoading}
                            onChange={handleStopChange}
                            onRemove={handleRemoveStop}
                            onMoveUp={index > 0 ? () => reorderMiddleStops(index, index - 1) : undefined}
                            onMoveDown={
                              index < middleStops.length - 1 ? () => reorderMiddleStops(index, index + 1) : undefined
                            }
                          />
                        </Box>
                      );
                    })}
                  </Box>
                </SortableContext>
              </DndContext>
            )}

            <Box>
              <Button
                type="button"
                variant="outlined"
                size="small"
                startIcon={<AddIcon />}
                onClick={handleAddStop}
                disabled={isLoading || isAtStopCap}
              >
                Add stop
              </Button>
              {isAtStopCap && (
                <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                  Maximum 10 stops.
                </Typography>
              )}
            </Box>

            <Box
              data-testid={`stop-row-${middleStopLetter(middleStops.length)}`}
              data-highlighted={highlightedLetters.has(middleStopLetter(middleStops.length)) || undefined}
              sx={highlightedLetters.has(middleStopLetter(middleStops.length)) ? highlightSx : undefined}
            >
              <AddressAutocomplete
                label="Finish"
                token={tokenState.token}
                displayValue={finish.label}
                disabled={isLoading}
                onResolve={(result: ResolvedAddress) => setFinish({ value: result.value, label: result.label })}
              />
            </Box>

            <InfeasibleRouteCallout error={error} orderedStops={orderedStops} />
          </Box>
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1, pt: 1 }}>
            <IconButton
              aria-label="Swap start and finish"
              onClick={handleSwap}
              disabled={isLoading}
              sx={{ minWidth: 44, minHeight: 44 }}
            >
              <SwapVertIcon />
            </IconButton>
            <IconButton
              aria-label="Use my current location as the start"
              onClick={handleGeolocate}
              disabled={isLoading || geoLoading}
              sx={{ minWidth: 44, minHeight: 44 }}
            >
              <MyLocationIcon />
            </IconButton>
          </Box>
        </Box>

        {geoError && (
          <Typography variant="body2" color="error">
            {geoError}
          </Typography>
        )}
        {tokenState.status === 'error' && (
          <Typography variant="body2" color="text.secondary">
            Address autocomplete is unavailable right now — you can still type a full address or lat,lng directly.
          </Typography>
        )}

        <Box>
          <Button type="submit" variant="contained" color="primary" loading={isLoading} fullWidth>
            Find Cheapest Route
          </Button>
          {isLoading && <LinearProgress sx={{ mt: 1 }} />}
        </Box>
      </Box>

      <DemoTripChips isLoading={isLoading} onSelect={handleDemoTripSelect} />
    </Box>
  );
}

export default PlannerFormSection;
