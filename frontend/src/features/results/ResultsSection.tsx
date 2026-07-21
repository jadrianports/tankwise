import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';

import { useRoutePlanContext } from '../../context/RoutePlanContext';
import SummaryCard from './SummaryCard';
import StopList from './StopList';

// Composes the results panel's static story (UX-03/11/13/14) from
// already-returned response fields: hero cost + savings + fleet math +
// alternatives badge + price disclaimer (SummaryCard), and the stop list.
// The collapsible per-leg breakdown, tank chart, and loading/error/cold
// start narration (D-20/UX-07/UX-10) land in this same file in the next
// task of this plan.
function ResultsSection() {
  const { status, data, error } = useRoutePlanContext();

  if (status === 'success' && data) {
    return (
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        <SummaryCard data={data} />
        <StopList stops={data.fuel_stops} />
      </Box>
    );
  }

  if (status === 'error' && error) {
    return (
      <Typography variant="body2" color="error">
        {error.message}
      </Typography>
    );
  }

  return (
    <Typography variant="body2" color="text.secondary">
      Enter a start and finish, or try a demo route, to see the cheapest fueling plan.
    </Typography>
  );
}

export default ResultsSection;
