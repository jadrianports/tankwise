import Dialog from '@mui/material/Dialog';
import DialogTitle from '@mui/material/DialogTitle';
import DialogContent from '@mui/material/DialogContent';
import IconButton from '@mui/material/IconButton';
import CloseIcon from '@mui/icons-material/Close';
import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';

import type { FuelStop, PurchaseReason } from '../../types/routeContract';
import { formatGallons } from '../../utils/format';

export interface JustificationPopupProps {
  stop: FuelStop;
  number: number;
  open: boolean;
  onClose: () => void;
}

// The 4-value purchase_reason enum translated into a human sentence
// (the backend emits no prose, structured fields only -- this is the one
// place that prose gets written, frontend-side).
const REASON_COPY: Record<PurchaseReason, (stop: FuelStop) => string> = {
  reach_cheaper_stop: (stop) =>
    `Bought just enough fuel here to reach ${
      stop.rationale.reason_target_name ?? 'a cheaper station up ahead'
    } without running low.`,
  fill_to_continue: () =>
    'No cheaper station was in range, so the tank was filled here to keep the trip moving.',
  reach_finish: () => 'Bought just enough fuel here to reach the finish.',
  top_up_at_cheapest: () => 'This was the cheapest station in range, so the tank was topped up here.',
};

function justificationText(stop: FuelStop): string {
  const { purchase_reason } = stop.rationale;
  if (!purchase_reason) {
    return 'The starting tank already covered this leg -- no fuel was purchased here.';
  }
  return REASON_COPY[purchase_reason](stop);
}

// Matches the backend's own skipped rule, re-derived frontend-side:
// "passed N stations since the last stop" plus their average price.
function skippedText(stop: FuelStop): string | null {
  const { skipped_count, skipped_avg_price } = stop.rationale;
  if (skipped_count <= 0) return null;
  const avg = skipped_avg_price ? `, averaging $${skipped_avg_price}/gal` : '';
  return `Passed ${skipped_count} station${skipped_count === 1 ? '' : 's'} since the last stop${avg}.`;
}

function percentileText(stop: FuelStop): string | null {
  const { price_percentile, corridor_avg_price } = stop.rationale;
  if (price_percentile === null) return null;
  const pct = Math.round(price_percentile * 100);
  const avg = corridor_avg_price ? ` (corridor average: $${corridor_avg_price}/gal)` : '';
  return `This price beats ${pct}% of the corridor's candidate stations${avg}.`;
}

// Accessible dialog (not a Leaflet popup) opened on a chosen-stop
// marker's activation. Every sentence here is composed frontend-side from
// the structured `rationale` fields -- no backend prose field is consumed.
function JustificationPopup({ stop, number, open, onClose }: JustificationPopupProps) {
  const skipped = skippedText(stop);
  const percentile = percentileText(stop);

  return (
    <Dialog open={open} onClose={onClose} aria-labelledby="justification-popup-title" maxWidth="xs" fullWidth>
      <DialogTitle
        id="justification-popup-title"
        sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 2 }}
      >
        <Typography variant="h6" component="span">
          Stop {number}: {stop.name}
        </Typography>
        <IconButton aria-label="Close" onClick={onClose} size="small">
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent>
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.25, mb: 2 }}>
          <Typography variant="body2" sx={{ color: 'fuel.dark' }}>
            ${stop.price_per_gallon}/gal
          </Typography>
          <Typography variant="body2">{formatGallons(stop.gallons)}</Typography>
          <Typography variant="body2" sx={{ color: 'fuel.dark' }}>
            ${stop.cost} total
          </Typography>
        </Box>
        <Typography variant="body1" sx={{ mb: 1 }}>
          {justificationText(stop)}
        </Typography>
        {skipped && (
          <Typography variant="body2" color="text.secondary" sx={{ mb: skipped && percentile ? 0.5 : 0 }}>
            {skipped}
          </Typography>
        )}
        {percentile && (
          <Typography variant="body2" color="text.secondary">
            {percentile}
          </Typography>
        )}
      </DialogContent>
    </Dialog>
  );
}

export default JustificationPopup;
