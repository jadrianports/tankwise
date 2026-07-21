import { useCallback, useEffect, useRef, useState } from 'react';
import Box from '@mui/material/Box';

import AppShell from './components/AppShell';
import Sidebar from './components/Sidebar';
import MapView from './features/map/MapView';
import { fetchConfig } from './api/configClient';
import { useRoutePlan } from './hooks/useRoutePlan';
import { RoutePlanContext } from './context/RoutePlanContext';
import type { FocusStopRequest } from './context/RoutePlanContext';

// ~440px scrolling sidebar (D-18, widened from the old 380px) + map
// filling the remaining viewport.
const SIDEBAR_WIDTH = 440;

type ConfigState =
  | { status: 'loading' }
  | { status: 'ready'; token: string }
  | { status: 'error' };

// Full route-planner layout: AppBar on top; below it a permanent
// non-overlay 440px sidebar (Sidebar's section slots) fixed on md+
// (stacked column below 900px); the map fills the remaining width.
function App() {
  const { status, data, error, submit, retry } = useRoutePlan();
  const [config, setConfig] = useState<ConfigState>({ status: 'loading' });

  // Bridges a StopList row click (features/results, inside Sidebar) to
  // MapView's own camera fly-to/popup-open logic (features/map, 09-04's
  // focusStop) without either module importing the other -- App.tsx is
  // their shared ancestor, so it owns the request state and hands the
  // setter down through context while passing the resulting request to
  // MapView as a plain prop.
  const [focusStopRequest, setFocusStopRequest] = useState<FocusStopRequest | null>(null);
  const focusNonceRef = useRef(0);
  const focusStop = useCallback((key: string | number) => {
    focusNonceRef.current += 1;
    setFocusStopRequest({ key, nonce: focusNonceRef.current });
  }, []);

  // Fetch the pk. token once at boot (D-05). A missing/misconfigured
  // token degrades to the map-pane-only config-error state (D-08) inside
  // MapView; it never blocks the planner sidebar.
  useEffect(() => {
    let cancelled = false;
    fetchConfig().then((result) => {
      if (cancelled) return;
      setConfig(
        result.ok ? { status: 'ready', token: result.data.mapbox_public_token } : { status: 'error' }
      );
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <Box sx={{ minHeight: '100vh', bgcolor: 'background.default' }}>
      <AppShell />

      <RoutePlanContext.Provider value={{ status, data, error, solve: submit, retry, focusStop }}>
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
              overflowY: { md: 'auto' },
              p: 3,
              display: 'flex',
              flexDirection: 'column',
              gap: 3,
              bgcolor: 'background.paper',
              borderRight: { md: '1px solid' },
              borderColor: 'divider',
            }}
          >
            <Sidebar />
          </Box>

          <Box
            component="main"
            sx={{ flexGrow: 1, height: { xs: 400, md: 'auto' }, minHeight: { xs: 400, md: 'auto' } }}
          >
            <MapView
              data={data}
              token={config.status === 'ready' ? config.token : null}
              tokenStatus={config.status}
              focusStopRequest={focusStopRequest}
            />
          </Box>
        </Box>
      </RoutePlanContext.Provider>
    </Box>
  );
}

export default App;
