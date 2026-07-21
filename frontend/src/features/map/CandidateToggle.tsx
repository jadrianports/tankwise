import IconButton from '@mui/material/IconButton';
import Tooltip from '@mui/material/Tooltip';
import VisibilityIcon from '@mui/icons-material/Visibility';
import VisibilityOffIcon from '@mui/icons-material/VisibilityOff';

export interface CandidateToggleProps {
  visible: boolean;
  onToggle: () => void;
}

// On-by-default control (D-12) that shows/hides the candidate price layer
// + its legend together. The price landscape is the most impressive thing
// on the map, so it defaults to visible; this stays a one-click strip-back
// to route + chosen stops for a viewer who wants a cleaner view. >=44px
// touch target per 09-UI-SPEC.md's icon-only-control spacing exception.
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
