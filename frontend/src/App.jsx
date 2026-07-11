import { useCallback, useRef, useState } from 'react';
import Box from '@mui/material/Box';

import AppShell from './components/AppShell';
import LocationForm from './components/LocationForm';
import PresetRoutes from './components/PresetRoutes';
import SummaryCard from './components/SummaryCard';
import StopList from './components/StopList';
import EmptyState from './components/EmptyState';
import ErrorAlert from './components/ErrorAlert';
import RouteMap from './components/RouteMap';
import { useRoutePlan } from './hooks/useRoutePlan';

const SIDEBAR_WIDTH = 380;

// Full route-planner layout: AppBar on top; below it a permanent
// non-overlay sidebar (form + presets + results) fixed at 380px on md+
// (stacked column below 900px); the map fills the remaining width.
function App() {
  const { status, data, error, submit } = useRoutePlan();
  const [start, setStart] = useState('');
  const [finish, setFinish] = useState('');

  // Shared marker map (station_id/index -> Leaflet marker instance) that
  // RouteMap populates and StopList row clicks read from.
  const markerRefs = useRef({});
  const mapInstanceRef = useRef(null);

  const handleMapReady = useCallback((map) => {
    mapInstanceRef.current = map;
  }, []);

  const handlePresetSelect = useCallback(
    (preset) => {
      setStart(preset.start);
      setFinish(preset.finish);
      submit(preset.start, preset.finish);
    },
    [submit]
  );

  const focusStop = useCallback((key) => {
    const marker = markerRefs.current[key];
    const map = mapInstanceRef.current;
    if (!marker || !map) return;
    map.flyTo(marker.getLatLng(), 10);
    marker.openPopup();
  }, []);

  const showResults = status === 'success' && data;
  const showError = status === 'error' && error;
  const showEmptyState = !showResults && !showError;

  return (
    <Box sx={{ minHeight: '100vh', bgcolor: 'background.default' }}>
      <AppShell />

      <Box
        sx={{
          display: 'flex',
          flexDirection: { xs: 'column', md: 'row' },
          height: { md: 'calc(100vh - 64px)' },
        }}
      >
        <Box
          component="aside"
          sx={{
            width: { md: SIDEBAR_WIDTH },
            flexShrink: 0,
            p: 3,
            display: 'flex',
            flexDirection: 'column',
            gap: 3,
            bgcolor: 'background.paper',
            borderRight: { md: '1px solid' },
            borderColor: 'divider',
          }}
        >
          {/* Non-scrolling top block: form, presets, and the total-fuel-cost
              summary must always stay visible on md+, regardless of how long
              the itinerary below is. */}
          <Box sx={{ flexShrink: 0, display: 'flex', flexDirection: 'column', gap: 3 }}>
            <LocationForm
              start={start}
              finish={finish}
              onStartChange={setStart}
              onFinishChange={setFinish}
              status={status}
              onSubmit={submit}
            />

            <PresetRoutes status={status} onSelect={handlePresetSelect} />

            {showError && <ErrorAlert error={error} />}

            {showResults && <SummaryCard data={data} />}

            {showEmptyState && <EmptyState />}
          </Box>

          {/* Only the itinerary scrolls on md+; on xs the whole page scrolls
              and this stays part of the natural document flow. */}
          {showResults && (
            <Box sx={{ flex: { md: 1 }, minHeight: { md: 0 }, overflowY: { md: 'auto' } }}>
              <StopList stops={data.fuel_stops} onSelectStop={focusStop} />
            </Box>
          )}
        </Box>

        <Box component="main" sx={{ flexGrow: 1, height: { xs: 400, md: 'auto' }, minHeight: { xs: 400, md: 'auto' } }}>
          <RouteMap data={showResults ? data : null} markerRefs={markerRefs} onMapReady={handleMapReady} />
        </Box>
      </Box>
    </Box>
  );
}

export default App;
