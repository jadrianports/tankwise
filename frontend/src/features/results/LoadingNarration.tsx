import Box from '@mui/material/Box';
import CircularProgress from '@mui/material/CircularProgress';
import Typography from '@mui/material/Typography';

import { useColdStart, type ColdStartStage } from './useColdStart';

// Progressive loading copy (UX-07), verbatim from 09-UI-SPEC.md's
// Copywriting Contract -- escalates to a named cold-start explanation
// rather than leaving a slow free-tier solve looking like a hung request
// (D-40).
const STAGE_COPY: Record<ColdStartStage, string> = {
  solving: 'Solving your route…',
  checking: 'Still working — checking fuel prices along the corridor…',
  waking: 'Waking up the server — free tier, this can take up to a minute…',
};

// Only ever mounted while a solve is in flight (see ResultsSection), so the
// hook's timer starts the moment this component appears and is cleaned up
// automatically on unmount when the solve resolves.
function LoadingNarration() {
  const stage = useColdStart(true);

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2, py: 4 }}>
      <CircularProgress size={32} />
      <Typography variant="body1" color="text.secondary" align="center">
        {STAGE_COPY[stage]}
      </Typography>
    </Box>
  );
}

export default LoadingNarration;
