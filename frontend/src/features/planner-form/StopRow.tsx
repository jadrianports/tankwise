import Box from '@mui/material/Box';
import IconButton from '@mui/material/IconButton';
import CloseIcon from '@mui/icons-material/Close';
import DragIndicatorIcon from '@mui/icons-material/DragIndicator';
import KeyboardArrowUpIcon from '@mui/icons-material/KeyboardArrowUp';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import { useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';

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
  // Explicit keyboard-accessible reorder equivalent (D-02) alongside
  // @dnd-kit's own pointer-drag + KeyboardSensor pick-up-then-arrow flow
  // on the drag handle below -- undefined at a list boundary (first row
  // has no "up", last has no "down").
  onMoveUp?: () => void;
  onMoveDown?: () => void;
}

// One intermediate stop between the anchored Start and Finish rows (D-01).
// Reuses AddressAutocomplete verbatim, exactly like the Start/Finish rows
// in PlannerFormSection. Middle stops are never anchored, always
// removable, and reorderable among themselves only (D-02): the drag
// handle is a useSortable draggable/droppable target (pointer drag +
// KeyboardSensor pick-up-then-arrow), and the up/down IconButtons are a
// single-click keyboard equivalent that reorders via the same
// PlannerFormSection.reorderMiddleStops path -- both ship regardless of
// whether the user ever drags anything.
function StopRow({ stop, letter, token, disabled, onChange, onRemove, onMoveUp, onMoveDown }: StopRowProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: stop.id });
  const style = {
    transform: CSS.Transform.toString(transform),
    transition: transition ?? undefined,
    opacity: isDragging ? 0.6 : 1,
  };

  return (
    <Box ref={setNodeRef} style={style} sx={{ display: 'flex', gap: 0.5, alignItems: 'flex-start' }}>
      <Box
        {...attributes}
        {...listeners}
        aria-label={`Reorder stop ${letter}`}
        sx={{
          display: 'flex',
          alignItems: 'center',
          minHeight: 44,
          px: 0.5,
          touchAction: 'none',
          cursor: disabled ? 'default' : 'grab',
        }}
      >
        <DragIndicatorIcon fontSize="small" color={disabled ? 'disabled' : 'action'} />
      </Box>
      <Box sx={{ flexGrow: 1, minWidth: 0 }}>
        <AddressAutocomplete
          label={`Stop ${letter}`}
          token={token}
          displayValue={stop.label}
          disabled={disabled}
          onResolve={(result: ResolvedAddress) => onChange(stop.id, result)}
        />
      </Box>
      <Box sx={{ display: 'flex', flexDirection: 'column' }}>
        <IconButton
          aria-label={`Move stop ${letter} up`}
          onClick={onMoveUp}
          disabled={disabled || !onMoveUp}
          size="small"
          sx={{ minWidth: 32, minHeight: 22 }}
        >
          <KeyboardArrowUpIcon fontSize="small" />
        </IconButton>
        <IconButton
          aria-label={`Move stop ${letter} down`}
          onClick={onMoveDown}
          disabled={disabled || !onMoveDown}
          size="small"
          sx={{ minWidth: 32, minHeight: 22 }}
        >
          <KeyboardArrowDownIcon fontSize="small" />
        </IconButton>
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
