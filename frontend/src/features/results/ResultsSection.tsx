import { useCallback } from 'react';
import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';
import Accordion from '@mui/material/Accordion';
import AccordionSummary from '@mui/material/AccordionSummary';
import AccordionDetails from '@mui/material/AccordionDetails';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import Button from '@mui/material/Button';
import LinearProgress from '@mui/material/LinearProgress';

import { useRoutePlanContext } from '../../context/RoutePlanContext';
import SummaryCard from './SummaryCard';
import StopList from './StopList';
import LegBreakdown from './LegBreakdown';
import TankChart from './TankChart';
import LoadingNarration from './LoadingNarration';

// Composes the full static results story from already-returned response
// fields: hero cost + savings + fleet math + alternatives badge + price
// disclaimer (SummaryCard), the stop list, and two collapsible sections
// for the N+1 per-leg breakdown and the running tank chart. None of this
// depends on playback ever having run.
//
// Loading, error, and result all live inside one `aria-live` region so a
// screen reader announces each state change. A re-solve (once vehicle
// sliders exist) keeps the last good plan fully rendered with only a
// thin progress bar rather than blanking to a spinner -- the
// full-takeover LoadingNarration is reserved for the very first solve,
// when there is no prior plan to keep showing.
function ResultsSection() {
  const { status, data, error, retry } = useRoutePlanContext();

  const handleRetry = useCallback(() => {
    retry();
  }, [retry]);

  return (
    <Box aria-live="polite" aria-atomic="false">
      {status === 'loading' && !data && <LoadingNarration />}

      {status === 'error' && error && (
        <Box role="alert" sx={{ py: 2 }}>
          <Typography variant="body1" color="error" sx={{ mb: 1 }}>
            {error.message}
          </Typography>
          {error.code === 'upstream_error' && (
            <Button variant="outlined" size="small" onClick={handleRetry}>
              Retry
            </Button>
          )}
        </Box>
      )}

      {data && (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          {status === 'loading' && <LinearProgress sx={{ borderRadius: 1 }} />}

          <SummaryCard data={data} />

          <Accordion disableGutters>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Typography variant="body1">Per-leg breakdown</Typography>
            </AccordionSummary>
            <AccordionDetails>
              <LegBreakdown
                legs={data.legs}
                totalDurationS={data.total_duration_s}
                fuelStopCount={data.fuel_stop_count}
              />
            </AccordionDetails>
          </Accordion>

          <Accordion disableGutters>
            <AccordionSummary expandIcon={<ExpandMoreIcon />}>
              <Typography variant="body1">Tank level along the route</Typography>
            </AccordionSummary>
            <AccordionDetails>
              <TankChart legs={data.legs} stops={data.fuel_stops} vehicle={data.vehicle} />
            </AccordionDetails>
          </Accordion>

          <StopList stops={data.fuel_stops} />
        </Box>
      )}

      {status === 'idle' && !data && (
        <Typography variant="body2" color="text.secondary">
          Enter a start and finish, or try a demo route, to see the cheapest fueling plan.
        </Typography>
      )}
    </Box>
  );
}

export default ResultsSection;
