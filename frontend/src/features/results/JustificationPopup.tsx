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

// The purchase_reason enum translated into a human sentence (the backend
// emits no prose, structured fields only -- this is the one place that
// prose gets written, frontend-side). Looked up via an OPTIONAL call
// (see justificationText below) so a future, still-unmapped reason value
// degrades to a neutral sentence instead of throwing.
const REASON_COPY: Partial<Record<PurchaseReason, (stop: FuelStop) => string>> = {
  reach_cheaper_stop: (stop) =>
    `Bought just enough fuel here to reach ${
      stop.rationale.reason_target_name ?? 'a cheaper station up ahead'
    } without running low.`,
  fill_to_continue: () =>
    'No cheaper station was in range, so the tank was filled here to keep the trip moving.',
  reach_finish: () => 'Bought just enough fuel here to reach the finish.',
  top_up_at_cheapest: () => 'This was the cheapest station in range, so the tank was topped up here.',
  // Placeholder sentence only -- the real copy is written in plan 18-05,
  // last, against observed DP output (D-06), so it can describe what the
  // solver actually does rather than being authored blind here.
  bypass_cheaper_not_worth_stop: () =>
    'A cheaper station up ahead was skipped here because stopping for it would have cost more in fees than it saved.',
};

function justificationText(stop: FuelStop): string {
  const { purchase_reason } = stop.rationale;
  if (!purchase_reason) {
    return 'The starting tank already covered this leg -- no fuel was purchased here.';
  }
  // Optional call with a fallback: REASON_COPY was previously an exhaustive
  // Record over a four-value union, so a fifth (or future sixth) value
  // called `undefined` and threw at runtime -- a crash, not a copy gap.
  // The fallback stays even after every known reason has real copy, so a
  // future new value degrades instead of breaking.
  return (
    REASON_COPY[purchase_reason]?.(stop) ??
    'This stop was part of the cheapest overall fueling plan for the trip.'
  );
}

// Reworded per D-04: skipped_count/skipped_avg_price no longer mean
// "everything positionally passed between the previous stop and this
// one" -- they now count genuinely-rejected candidates: strictly-cheaper
// stations the solver evaluated as successors from this stop and chose
// not to take.
function skippedText(stop: FuelStop): string | null {
  const { skipped_count, skipped_avg_price } = stop.rationale;
  if (skipped_count <= 0) return null;
  const avg = skipped_avg_price ? `, averaging $${skipped_avg_price}/gal` : '';
  return `Rejected ${skipped_count} cheaper station${skipped_count === 1 ? '' : 's'} in range from here${avg}.`;
}

// New (Phase 18): names the count -- and, when priced, the forgone
// fuel-dollar saving -- of strictly-cheaper stations the solver bypassed
// here because the flat per-stop penalty outweighed the saving
// (`bypass_cheaper_not_worth_stop`). Distinct from skippedText above:
// this always reflects the actual bypass decision behind THIS purchase,
// while skippedText is the broader "what was evaluated and rejected"
// context.
function bypassedCheaperText(stop: FuelStop): string | null {
  const { bypassed_cheaper_count, bypassed_saving_forgone } = stop.rationale;
  if (bypassed_cheaper_count <= 0) return null;
  const saving = bypassed_saving_forgone ? ` (about $${bypassed_saving_forgone} in fuel savings)` : '';
  return `Passed up ${bypassed_cheaper_count} cheaper station${
    bypassed_cheaper_count === 1 ? '' : 's'
  } here rather than pay for another stop${saving}.`;
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
  const bypassedCheaper = bypassedCheaperText(stop);
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
          <Typography variant="body2" color="text.secondary" sx={{ mb: bypassedCheaper || percentile ? 0.5 : 0 }}>
            {skipped}
          </Typography>
        )}
        {bypassedCheaper && (
          <Typography variant="body2" color="text.secondary" sx={{ mb: percentile ? 0.5 : 0 }}>
            {bypassedCheaper}
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
