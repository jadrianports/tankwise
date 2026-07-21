import IconButton from '@mui/material/IconButton';
import Tooltip from '@mui/material/Tooltip';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';

export interface PlayButtonProps {
  onClick: () => void;
  disabled?: boolean;
}

// The chase-cam playback trigger (MAP-04) lives in the map's own controls
// (bottom-right, alongside StyleSwitcher/CandidateToggle), not the
// sidebar (D-25) -- this is what keeps playback cleanly cuttable: pulling
// this one control (and its MapView mounts) out never touches the
// results panel. >=44px touch target per 09-UI-SPEC.md's icon-only-
// control spacing exception (UX-10). Wrapped in a <span> so the Tooltip
// still fires while the button is disabled (MUI's disabled buttons
// otherwise swallow pointer events entirely).
function PlayButton({ onClick, disabled }: PlayButtonProps) {
  const label = 'Play the fuel stop fly-through';

  return (
    <Tooltip title={disabled ? 'Solve a route to play its fly-through' : label}>
      <span style={{ position: 'absolute', bottom: 16, right: 16 }}>
        <IconButton
          onClick={onClick}
          disabled={disabled}
          aria-label={label}
          sx={{
            minWidth: 44,
            minHeight: 44,
            bgcolor: 'background.paper',
            boxShadow: 2,
            '&:hover': { bgcolor: 'background.paper' },
          }}
        >
          <PlayArrowIcon />
        </IconButton>
      </span>
    </Tooltip>
  );
}

export default PlayButton;
