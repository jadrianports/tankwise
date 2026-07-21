import IconButton from '@mui/material/IconButton';
import Tooltip from '@mui/material/Tooltip';
import SatelliteIcon from '@mui/icons-material/Satellite';
import MapIcon from '@mui/icons-material/Map';

export interface StyleSwitcherProps {
  isSatellite: boolean;
  onToggle: () => void;
}

// Streets<->satellite toggle (the base-style axis, independent of the
// dark/light theme toggle in AppShell). >=44px touch target, matching
// the spacing used for other icon-only controls.
function StyleSwitcher({ isSatellite, onToggle }: StyleSwitcherProps) {
  const label = isSatellite ? 'Switch to streets view' : 'Switch to satellite view';

  return (
    <Tooltip title={label}>
      <IconButton
        onClick={onToggle}
        aria-label={label}
        sx={{
          position: 'absolute',
          top: 16,
          right: 16,
          minWidth: 44,
          minHeight: 44,
          bgcolor: 'background.paper',
          boxShadow: 2,
          '&:hover': { bgcolor: 'background.paper' },
        }}
      >
        {isSatellite ? <MapIcon /> : <SatelliteIcon />}
      </IconButton>
    </Tooltip>
  );
}

export default StyleSwitcher;
