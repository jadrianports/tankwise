import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';

import type { RoutePlanError } from '../../hooks/useRoutePlan';
import type { InfeasibleRouteDetail } from '../../types/routeContract';

// One ordered stop's cross-reference letter + display label -- the same
// A/B/C convention as the map pins and the leg-breakdown boundary rows,
// supplied by PlannerFormSection (the owner of start/middleStops/finish
// state), not re-derived here.
export interface OrderedStop {
  letter: string;
  label: string;
}

export interface InfeasibleRouteCalloutProps {
  error: RoutePlanError | null;
  orderedStops: OrderedStop[];
}

// Additive, named-leg guidance for a multi-stop infeasible trip (D-08).
// ResultsSection's own generic alert already shows the gap-based message
// for EVERY error code, unchanged, regardless of whether this renders --
// this callout adds NOTHING beyond that unless the response carries an
// additive `leg_index` (13-03/13-04), in which case it names the exact
// failing segment by its bounding stop letters/labels and suggests a
// concrete fix, right next to the stop list the user needs to edit.
function InfeasibleRouteCallout({ error, orderedStops }: InfeasibleRouteCalloutProps) {
  if (!error || error.code !== 'infeasible_route') return null;

  const detail = error.detail as InfeasibleRouteDetail | undefined;
  const legIndex = detail?.leg_index;
  if (legIndex === null || legIndex === undefined) return null;

  const from = orderedStops[legIndex];
  const to = orderedStops[legIndex + 1];
  if (!from || !to) return null;

  return (
    <Box
      role="alert"
      sx={{
        mt: 1,
        p: 1.5,
        borderRadius: 1,
        border: '1px solid',
        borderColor: 'error.main',
        bgcolor: 'transparent',
      }}
    >
      <Typography variant="body2" color="error" sx={{ fontWeight: 600 }}>
        Leg {from.letter}→{to.letter} ({from.label} → {to.label}) is too far to reach on a full tank.
      </Typography>
      <Typography variant="body2" color="text.secondary">
        Add a stop between them, or move {to.letter} ({to.label}) closer.
      </Typography>
    </Box>
  );
}

export default InfeasibleRouteCallout;
