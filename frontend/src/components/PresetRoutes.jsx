import Box from '@mui/material/Box';
import Chip from '@mui/material/Chip';
import Typography from '@mui/material/Typography';

import { PRESET_ROUTES } from '../constants/presets';

// One-click example routes. Clicking a chip fills both fields AND
// auto-submits -- no extra click -- using the byte-identical shared
// PRESET_ROUTES constant so a repeat click reuses the exact cache key.
function PresetRoutes({ status, onSelect }) {
  const isLoading = status === 'loading';

  return (
    <Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        Or try a preset route
      </Typography>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
        {PRESET_ROUTES.map((preset) => (
          <Chip
            key={preset.label}
            label={preset.label}
            title={preset.description}
            clickable
            disabled={isLoading}
            onClick={() => onSelect(preset)}
          />
        ))}
      </Box>
    </Box>
  );
}

export default PresetRoutes;
