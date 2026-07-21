import Dialog from '@mui/material/Dialog';
import DialogTitle from '@mui/material/DialogTitle';
import DialogContent from '@mui/material/DialogContent';
import DialogActions from '@mui/material/DialogActions';
import Button from '@mui/material/Button';
import Typography from '@mui/material/Typography';
import Box from '@mui/material/Box';

import type { RouteResponse } from '../../types/routeContract';
import { formatCurrency } from '../../utils/format';

export interface SavingsModalProps {
  data: RouteResponse;
  onClose: () => void;
}

// The playback finale: the SAME savings figures the always-visible
// SummaryCard.tsx renders, in the same fuel-amber-cost /
// primary-green-savings color split -- this modal never
// computes a second savings figure, it reads `data.savings` directly.
// MUI's Dialog renders its own full-viewport backdrop, which is what
// keeps the rest of the page (including the sidebar) visually dimmed
// while this finale is shown -- no extra dimming mechanism needed here.
function SavingsModal({ data, onClose }: SavingsModalProps) {
  const savings = data.savings;

  return (
    <Dialog open onClose={onClose} aria-labelledby="savings-modal-title" maxWidth="xs" fullWidth>
      <DialogTitle id="savings-modal-title">Trip complete</DialogTitle>
      <DialogContent>
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
          <Typography variant="body2" color="text.secondary">
            Total fuel cost
          </Typography>
          <Typography variant="h4" component="p" sx={{ color: 'fuel.main' }}>
            {formatCurrency(data.total_cost)}
          </Typography>

          {savings ? (
            <Typography variant="h5" component="p" sx={{ color: 'primary.main' }}>
              Saved {formatCurrency(savings.amount)}
              {savings.percent !== null ? ` (${savings.percent.toFixed(1)}%)` : ''}
            </Typography>
          ) : (
            <Typography variant="body1" color="text.secondary">
              {data.savings_note ?? 'Savings could not be estimated for this trip.'}
            </Typography>
          )}
        </Box>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="contained" sx={{ minWidth: 44, minHeight: 44 }}>
          Done
        </Button>
      </DialogActions>
    </Dialog>
  );
}

export default SavingsModal;
