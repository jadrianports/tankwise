import GlobalStyles from '@mui/material/GlobalStyles';
import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';
import Button from '@mui/material/Button';

import type { ChaseCamBeat } from './useChaseCam';
import { formatCurrency, formatGallons, formatMiles } from '../../utils/format';
import TankGauge from './TankGauge';

export interface PlaybackHUDProps {
  currentBeat: ChaseCamBeat | null;
  tankFraction: number;
  onSkip: () => void;
}

// "passed N stations, avg $X.XX" -- the backend's own aggregate fields,
// composed into prose frontend-side (the backend emits no prose itself).
function aggregateSkippedText(beat: ChaseCamBeat): string | null {
  if (beat.skippedCount <= 0) return null;
  const avg = beat.skippedAvgPrice ? `, avg $${beat.skippedAvgPrice}/gal` : '';
  return `Passed ${beat.skippedCount} station${beat.skippedCount === 1 ? '' : 's'}${avg}.`;
}

// Map-overlay HUD: renders over the map surface, never driving the
// sidebar's own components. The <GlobalStyles> rule below is what dims
// the sidebar -- it targets the desktop <aside> App.tsx already renders,
// scoped to exactly as long as this component stays mounted, so removing
// the playback feature (unmounting this component) removes the dimming
// automatically, with no sidebar-side coupling to unpick -- no
// Sidebar.tsx/App.tsx edits were needed to wire this up.
function PlaybackHUD({ currentBeat, tankFraction, onSkip }: PlaybackHUDProps) {
  const skipped = currentBeat ? aggregateSkippedText(currentBeat) : null;

  return (
    <>
      <GlobalStyles
        styles={{
          aside: { opacity: 0.35, pointerEvents: 'none', transition: 'opacity 300ms ease' },
        }}
      />
      <Box
        role="status"
        aria-live="polite"
        sx={{
          position: 'absolute',
          left: 16,
          right: 88,
          bottom: 16,
          zIndex: 2,
          bgcolor: 'rgba(16, 21, 27, 0.85)',
          color: 'common.white',
          borderRadius: 2,
          p: 2,
          display: 'flex',
          flexDirection: 'column',
          gap: 1,
        }}
      >
        <TankGauge fraction={tankFraction} />

        {currentBeat ? (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.25 }}>
            <Typography variant="subtitle1" component="p" sx={{ color: 'common.white' }}>
              Stop {currentBeat.index + 1}: {currentBeat.stop.name}
            </Typography>
            <Typography variant="body2" sx={{ color: 'common.white' }}>
              {formatMiles(currentBeat.fuelRemainingMi)} of range left ·{' '}
              {formatGallons(currentBeat.gallonsToppedUp)} topped up · {formatCurrency(currentBeat.pricePaid)} paid
            </Typography>
            {skipped && (
              <Typography variant="body2" sx={{ color: 'common.white', opacity: 0.85 }}>
                {skipped}
              </Typography>
            )}
          </Box>
        ) : (
          <Typography variant="body2" sx={{ color: 'common.white' }}>
            On the road…
          </Typography>
        )}

        <Button
          onClick={onSkip}
          variant="outlined"
          size="small"
          sx={{
            alignSelf: 'flex-start',
            minWidth: 44,
            minHeight: 44,
            color: 'common.white',
            borderColor: 'common.white',
            '&:hover': { borderColor: 'common.white', bgcolor: 'rgba(255,255,255,0.1)' },
          }}
        >
          Skip
        </Button>
      </Box>
    </>
  );
}

export default PlaybackHUD;
