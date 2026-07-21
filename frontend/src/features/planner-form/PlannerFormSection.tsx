import { useEffect, useRef, useState, useSyncExternalStore, type FormEvent } from 'react';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import IconButton from '@mui/material/IconButton';
import LinearProgress from '@mui/material/LinearProgress';
import Typography from '@mui/material/Typography';
import SwapVertIcon from '@mui/icons-material/SwapVert';
import MyLocationIcon from '@mui/icons-material/MyLocation';

import AddressAutocomplete, { type ResolvedAddress } from './AddressAutocomplete';
import DemoTripChips from './DemoTripChips';
import { useRoutePlanContext } from '../../context/RoutePlanContext';
import { useRecentTrips } from '../recent-trips/useRecentTrips';
import { getLoadTripRequestSnapshot, subscribeLoadTripRequest } from '../share-export/tripState';
import { HERO_VEHICLE_PRESET_ID, type DemoTrip } from '../../constants/presets';
import { fetchConfig } from '../../api/configClient';

interface FieldState {
  value: string; // resolved value sent to POST /api/route (coords or address string, D-07)
  label: string; // human-readable, client-side only
}

const EMPTY_FIELD: FieldState = { value: '', label: '' };

// Demo trip labels are always "A → B" (constants/presets.ts) -- split on
// the arrow to get per-endpoint display labels without inventing a second
// per-trip label field.
function splitDemoLabel(label: string): [string, string] {
  const [startLabel, finishLabel] = label.split('→').map((part) => part.trim());
  return [startLabel || label, finishLabel || label];
}

// The planner input surface (UX-01/UX-05/UX-06): two address-autocomplete
// fields, swap/geolocate controls, the "Find Cheapest Route" CTA, and the
// long-haul demo trip chips. Fills the Sidebar slot 09-03 established;
// reads/writes shared solve state via useRoutePlanContext() rather than
// prop-drilling through Sidebar.tsx or App.tsx.
function PlannerFormSection() {
  const { status, solve } = useRoutePlanContext();
  const { add: addRecentTrip } = useRecentTrips();
  const isLoading = status === 'loading';

  const [start, setStart] = useState<FieldState>(EMPTY_FIELD);
  const [finish, setFinish] = useState<FieldState>(EMPTY_FIELD);
  const [geoLoading, setGeoLoading] = useState(false);
  const [geoError, setGeoError] = useState<string | null>(null);

  // pk. token for the Search Box calls, fetched independently of App.tsx's
  // own GET /api/config call (MapView's copy) -- the endpoint is lean and
  // unthrottled by design (D-05), and keeping this self-contained avoids
  // growing App.tsx/RoutePlanContext.ts for a single feature's dependency.
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
    setStart({ value: trip.start, label: trip.startLabel });
    setFinish({ value: trip.finish, label: trip.finishLabel });
    void solve(trip.start, trip.finish);
  }, [loadTripRequest, solve]);

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    const startValue = start.value.trim();
    const finishValue = finish.value.trim();
    if (!startValue || !finishValue || isLoading) return;
    void solve(startValue, finishValue);
    addRecentTrip({
      start: startValue,
      finish: finishValue,
      startLabel: start.label || startValue,
      finishLabel: finish.label || finishValue,
      vehicle: HERO_VEHICLE_PRESET_ID,
    });
  };

  const handleSwap = () => {
    setStart(finish);
    setFinish(start);
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
    const [startLabel, finishLabel] = splitDemoLabel(trip.label);
    setStart({ value: trip.start, label: startLabel });
    setFinish({ value: trip.finish, label: finishLabel });
    void solve(trip.start, trip.finish);
    addRecentTrip({
      start: trip.start,
      finish: trip.finish,
      startLabel,
      finishLabel,
      vehicle: HERO_VEHICLE_PRESET_ID,
    });
  };

  const isEmptyState = !start.value && !finish.value;

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

      <Box component="form" onSubmit={handleSubmit} sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        <Box sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}>
          <Box sx={{ flexGrow: 1, display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
            <AddressAutocomplete
              label="Start"
              token={tokenState.token}
              displayValue={start.label}
              disabled={isLoading}
              onResolve={(result: ResolvedAddress) => setStart({ value: result.value, label: result.label })}
            />
            <AddressAutocomplete
              label="Finish"
              token={tokenState.token}
              displayValue={finish.label}
              disabled={isLoading}
              onResolve={(result: ResolvedAddress) => setFinish({ value: result.value, label: result.label })}
            />
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
