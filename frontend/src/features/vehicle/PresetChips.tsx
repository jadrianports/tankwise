import Box from '@mui/material/Box';
import Chip from '@mui/material/Chip';
import Typography from '@mui/material/Typography';

import type { VehiclePreset } from '../../constants/presets';

interface PresetChipsProps {
  presets: VehiclePreset[];
  selectedId: string | null;
  disabled?: boolean;
  onSelect: (preset: VehiclePreset) => void;
}

// Four diesel vehicle preset chips. Sourced directly from
// constants/presets.ts's locked VEHICLE_PRESETS -- no figures are
// redefined or "improved" here. Each chip's secondary text
// (e.g. "6.5 mpg · ~1,050 mi") makes the preset self-explanatory without a
// details panel.
function PresetChips({ presets, selectedId, disabled, onSelect }: PresetChipsProps) {
  return (
    <Box>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
        {presets.map((preset) => {
          const selected = preset.id === selectedId;
          return (
            <Chip
              key={preset.id}
              clickable
              disabled={disabled}
              color={selected ? 'primary' : 'default'}
              variant={selected ? 'filled' : 'outlined'}
              onClick={() => onSelect(preset)}
              sx={{ height: 'auto', '& .MuiChip-label': { display: 'block', px: 1.5, py: 0.75 } }}
              label={
                <Box sx={{ textAlign: 'left' }}>
                  <Typography variant="body2" component="span" sx={{ display: 'block', fontWeight: 600 }}>
                    {preset.label}
                  </Typography>
                  <Typography
                    variant="caption"
                    component="span"
                    color={selected ? 'inherit' : 'text.secondary'}
                    sx={{ display: 'block' }}
                  >
                    {preset.secondaryText}
                  </Typography>
                </Box>
              }
            />
          );
        })}
      </Box>
      {/* Every preset is diesel; the UI must say so once, near the chips. */}
      <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
        All presets price against truck-stop diesel — Sedan and RV are diesel too.
      </Typography>
    </Box>
  );
}

export default PresetChips;
