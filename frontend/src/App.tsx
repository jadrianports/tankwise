import { useCallback, useEffect, useRef, useState } from 'react';
import Box from '@mui/material/Box';

import AppShell from './components/AppShell';
import Sidebar from './components/Sidebar';
import BottomSheet from './components/BottomSheet';
import MapView from './features/map/MapView';
import ShareExportBar from './features/share-export/ShareExportBar';
import { useShareUrl } from './features/share-export/useShareUrl';
import './features/share-export/print.css';
import { fetchConfig } from './api/configClient';
import { useRoutePlan } from './hooks/useRoutePlan';
import { RoutePlanContext } from './context/RoutePlanContext';
import type { FocusStopRequest } from './context/RoutePlanContext';

// ~440px scrolling sidebar (widened from the old 380px) + map filling
// the remaining viewport.
const SIDEBAR_WIDTH = 440;

type ConfigState =
  | { status: 'loading' }
  | { status: 'ready'; token: string }
  | { status: 'error' };

// Full route-planner layout: AppBar on top; below it a permanent
// non-overlay 440px sidebar (Sidebar's section slots) fixed on md+
// (stacked column below 900px); the map fills the remaining width.
function App() {
  const { status, data, error, submit, retry, resolveVehicle } = useRoutePlan();
  const [config, setConfig] = useState<ConfigState>({ status: 'loading' });

  // Shareable trip URLs: decodes window.location.search on mount and
  // auto-solves with the encoded vehicle profile if present; shareUrl is
  // the current plan's own shareable link, null until a plan has been
  // solved.
  const { shareUrl } = useShareUrl(submit, data);

  // Bridges a StopList row click (features/results, inside Sidebar) to
  // MapView's own camera fly-to/popup-open logic (features/map, focusStop)
  // without either module importing the other -- App.tsx is their shared
  // ancestor, so it owns the request state and hands the setter down
  // through context while passing the resulting request to MapView as a
  // plain prop.
  const [focusStopRequest, setFocusStopRequest] = useState<FocusStopRequest | null>(null);
  const focusNonceRef = useRef(0);
  const focusStop = useCallback((key: string | number) => {
    focusNonceRef.current += 1;
    setFocusStopRequest({ key, nonce: focusNonceRef.current });
  }, []);

  // Fetch the pk. token once at boot. A missing/misconfigured token
  // degrades to the map-pane-only config-error state inside MapView; it
  // never blocks the planner sidebar.
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

      <RoutePlanContext.Provider value={{ status, data, error, solve: submit, retry, focusStop, resolveVehicle }}>
        <Box className="print-hide">
          <ShareExportBar data={data} shareUrl={shareUrl} />
        </Box>

        <Box
          sx={{
            display: 'flex',
            flexDirection: { xs: 'column', md: 'row' },
            height: { md: 'calc(100vh - 64px)' },
          }}
        >
          {/* Permanent desktop sidebar (md+, the ~440px scrolling panel).
              Below md, PlannerFormSection/VehicleSection/ResultsSection/
              RecentTripsSection instead render inside the mobile
              BottomSheet -- this Box is never mounted at xs, so its
              content never duplicates the sheet's own composition. */}
          <Box
            component="aside"
            sx={{
              display: { xs: 'none', md: 'flex' },
              width: { md: SIDEBAR_WIDTH },
              flexShrink: 0,
              overflowY: { md: 'auto' },
              p: 3,
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
            className="print-hide"
            sx={{
              flexGrow: 1,
              height: { xs: 'calc(100vh - 64px)', md: 'auto' },
              minHeight: { xs: 'calc(100vh - 64px)', md: 'auto' },
            }}
          >
            <MapView
              data={data}
              token={config.status === 'ready' ? config.token : null}
              tokenStatus={config.status}
              focusStopRequest={focusStopRequest}
            />
          </Box>
        </Box>

        {/* Mobile bottom sheet: three snap points over the still-live
            map above. Never mounted at md+ -- `display: none` on this
            wrapper removes the sheet's own `position: fixed` Paper from
            the render entirely, so it can never overlap the desktop
            sidebar. */}
        <Box className="print-hide" sx={{ display: { xs: 'block', md: 'none' } }}>
          <BottomSheet />
        </Box>
      </RoutePlanContext.Provider>
    </Box>
  );
}

export default App;
