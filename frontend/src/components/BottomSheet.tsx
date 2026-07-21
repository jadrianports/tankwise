import { useCallback, useEffect, useState } from 'react';
import Box from '@mui/material/Box';
import Paper from '@mui/material/Paper';
import Typography from '@mui/material/Typography';
import KeyboardArrowUpIcon from '@mui/icons-material/KeyboardArrowUp';

import Sidebar from './Sidebar';
import SummaryCard from '../features/results/SummaryCard';
import StopList from '../features/results/StopList';
import LoadingNarration from '../features/results/LoadingNarration';
import { useRoutePlanContext } from '../context/RoutePlanContext';

export type SnapPoint = 'peek' | 'half' | 'full';

// D-42: peek shows total cost + savings with zero interaction; half adds
// the stop list; full shows everything -- fixed viewport-relative heights,
// not content-derived, so the sheet's own height (not a scroll position)
// is what reveals more content per snap point.
const SNAP_HEIGHT: Record<SnapPoint, string> = {
  peek: '148px',
  half: '48vh',
  full: '86vh',
};

const NEXT_SNAP: Record<SnapPoint, SnapPoint> = { peek: 'half', half: 'full', full: 'peek' };

const HANDLE_LABEL: Record<SnapPoint, string> = {
  peek: 'Expand plan panel to show the stop list',
  half: 'Expand plan panel to show everything',
  full: 'Collapse plan panel',
};

// Baseline `prefers-reduced-motion` support (D-26 is stated as belonging to
// the later playback plan, but the layout/transition-level respect for it
// starts here, per this task's own action text): the sheet's own
// snap-point height transition is skipped entirely under reduced motion,
// snapping instead of animating.
function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );

  useEffect(() => {
    if (typeof window === 'undefined') return;
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    const handleChange = () => setReduced(query.matches);
    query.addEventListener('change', handleChange);
    return () => query.removeEventListener('change', handleChange);
  }, []);

  return reduced;
}

// Mobile plan panel (UX-08, D-42, xs/sm only -- App.tsx never mounts this
// at md+). A plain fixed-position Paper, deliberately NOT MUI's
// Drawer/SwipeableDrawer: both render a full-viewport modal backdrop that
// would trap every pointer/touch event outside the drawer's own bounds,
// which breaks D-42's explicit "the map stays interactive behind it at
// peek and half" requirement outright. This Paper only ever covers its own
// bottom slice of the viewport -- the map above it receives pointer events
// completely normally, with no backdrop element in the way at any snap
// point.
//
// Tap-to-cycle (not drag-to-resize) is the sheet's primary interaction:
// a real `<button>` (44px target, D-42/UX-10) advances peek -> half ->
// full -> peek on click/Enter/Space, which is reliably keyboard- and
// screen-reader-operable in a way a raw touch-drag gesture is not.
//
// D-18: `full` renders the literal `<Sidebar />` -- the exact same
// component tree and section order as the desktop aside, so "same content
// order" holds by construction rather than by a second hand-maintained
// copy. `peek`/`half` compose the SAME already-exported SummaryCard/
// StopList components (not a duplicated summary), just a smaller subset of
// them, ahead of the point where dragging to `full` reveals the rest.
function BottomSheet() {
  const { status, data, error } = useRoutePlanContext();
  const [snap, setSnap] = useState<SnapPoint>('peek');
  const reducedMotion = usePrefersReducedMotion();

  const cycleSnap = useCallback(() => setSnap((prev) => NEXT_SNAP[prev]), []);

  return (
    <Paper
      elevation={8}
      sx={{
        position: 'fixed',
        left: 0,
        right: 0,
        bottom: 0,
        zIndex: (theme) => theme.zIndex.appBar - 1,
        height: SNAP_HEIGHT[snap],
        maxHeight: '86vh',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        borderTopLeftRadius: 16,
        borderTopRightRadius: 16,
        transition: reducedMotion ? 'none' : 'height 250ms ease',
      }}
    >
      <Box
        component="button"
        type="button"
        onClick={cycleSnap}
        aria-label={HANDLE_LABEL[snap]}
        sx={{
          minWidth: 44,
          minHeight: 44,
          width: '100%',
          flexShrink: 0,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          border: 'none',
          bgcolor: 'transparent',
          color: 'inherit',
          cursor: 'pointer',
          p: 0,
          '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main', outlineOffset: -2 },
        }}
      >
        <Box sx={{ width: 36, height: 4, borderRadius: 2, bgcolor: 'divider', mb: 0.5 }} />
        <KeyboardArrowUpIcon
          fontSize="small"
          sx={{
            color: 'text.secondary',
            transition: reducedMotion ? 'none' : 'transform 250ms ease',
            transform: snap === 'full' ? 'rotate(180deg)' : 'none',
          }}
        />
      </Box>

      <Box
        sx={{ flexGrow: 1, overflowY: 'auto', px: 2, pb: 2 }}
        {...(snap !== 'full' ? { 'aria-live': 'polite' as const, 'aria-atomic': false } : {})}
      >
        {snap === 'full' ? (
          <Sidebar />
        ) : (
          <>
            {status === 'loading' && !data && <LoadingNarration />}
            {status === 'error' && error && !data && (
              <Typography role="alert" variant="body2" color="error">
                {error.message}
              </Typography>
            )}
            {data && <SummaryCard data={data} />}
            {data && snap === 'half' && <StopList stops={data.fuel_stops} />}
            {!data && status === 'idle' && (
              <Typography variant="body2" color="text.secondary">
                Enter a start and finish, or try a demo route, to see the cheapest fueling plan.
              </Typography>
            )}
          </>
        )}
      </Box>
    </Paper>
  );
}

export default BottomSheet;
