import IconButton from '@mui/material/IconButton';
import Tooltip from '@mui/material/Tooltip';
import VisibilityIcon from '@mui/icons-material/Visibility';
import VisibilityOffIcon from '@mui/icons-material/VisibilityOff';

export interface CandidateToggleProps {
  visible: boolean;
  onToggle: () => void;
}

// Off-by-default control that shows/hides the candidate price layer
// + its legend together. The layer starts hidden so a solved route reads
// as only the stops the solver chose; this is the one-click way to reveal
// the price landscape for a viewer who wants to see it. >=44px touch
// target, matching the spacing used for other icon-only controls.
function CandidateToggle({ visible, onToggle }: CandidateToggleProps) {
  const label = visible ? 'Hide candidate station prices' : 'Show candidate station prices';

  return (
    <Tooltip title={label}>
      <IconButton
        onClick={onToggle}
        aria-label={label}
        aria-pressed={visible}
        sx={{
          position: 'absolute',
          top: 16,
          left: 16,
          minWidth: 44,
          minHeight: 44,
          bgcolor: 'background.paper',
          boxShadow: 2,
          '&:hover': { bgcolor: 'background.paper' },
        }}
      >
        {visible ? <VisibilityIcon /> : <VisibilityOffIcon />}
      </IconButton>
    </Tooltip>
  );
}

export default CandidateToggle;
