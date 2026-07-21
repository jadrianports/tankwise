import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';

import { useRoutePlanContext } from '../../context/RoutePlanContext';

// Placeholder slot -- the results plan replaces this with the savings
// card, per-leg breakdown, tank chart, stop list and price disclaimer
// (UX-03/11/13/14). This plan proves the shared solve-state wiring
// (status/data/error) reaches here without App.tsx or Sidebar.tsx needing
// any further edits (09-03-PLAN.md Task 1) -- it deliberately does not
// build the real results UI.
function ResultsSection() {
  const { status, data, error } = useRoutePlanContext();

  if (status === 'success' && data) {
    return (
      <Box>
        <Typography variant="body2" color="text.secondary">
          Total fuel cost
        </Typography>
        <Typography variant="h5" component="p" sx={{ color: 'fuel.main' }}>
          ${data.total_cost}
        </Typography>
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
      Savings card, leg breakdown, tank chart and stop list land here in a
      later plan.
    </Typography>
  );
}

export default ResultsSection;
