import Box from '@mui/material/Box';
import IconButton from '@mui/material/IconButton';
import CloseIcon from '@mui/icons-material/Close';

import AddressAutocomplete, { type ResolvedAddress } from './AddressAutocomplete';

export interface MiddleStop {
  id: string;
  value: string; // resolved value sent to POST /api/route (coords or address string)
  label: string; // human-readable, client-side only
}

interface StopRowProps {
  stop: MiddleStop;
  letter: string; // this row's A/B/C-style cross-reference letter
  token: string | null;
  disabled?: boolean;
  onChange: (id: string, result: ResolvedAddress) => void;
  onRemove: (id: string) => void;
}

// One intermediate stop between the anchored Start and Finish rows (D-01).
// Reuses AddressAutocomplete verbatim, exactly like the Start/Finish rows
// in PlannerFormSection. Middle stops are never anchored, always
// removable, and (Task 2) reorderable among themselves only.
function StopRow({ stop, letter, token, disabled, onChange, onRemove }: StopRowProps) {
  return (
    <Box sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}>
      <Box sx={{ flexGrow: 1, minWidth: 0 }}>
        <AddressAutocomplete
          label={`Stop ${letter}`}
          token={token}
          displayValue={stop.label}
          disabled={disabled}
          onResolve={(result: ResolvedAddress) => onChange(stop.id, result)}
        />
      </Box>
      <IconButton
        aria-label={`Remove stop ${letter}`}
        onClick={() => onRemove(stop.id)}
        disabled={disabled}
        sx={{ minWidth: 44, minHeight: 44, mt: 0.5 }}
      >
        <CloseIcon />
      </IconButton>
    </Box>
  );
}

export default StopRow;
