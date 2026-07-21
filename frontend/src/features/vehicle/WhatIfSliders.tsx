import Box from '@mui/material/Box';
import Slider from '@mui/material/Slider';
import Typography from '@mui/material/Typography';

import type { VehicleProfileRequest } from '../../types/routeContract';

interface WhatIfSlidersProps {
  vehicle: VehicleProfileRequest;
  disabled?: boolean;
  onChange: (vehicle: VehicleProfileRequest) => void;
}

// Bounds mirror the backend's own validation exactly (Phase 7 D-03:
// routing/serializers.py::VehicleSerializer) so the UI can never construct
// an out-of-bounds request -- the DRF serializer stays the authoritative
// validator, this is a UX guardrail only (T-09-13).
const MPG_MIN = 1;
const MPG_MAX = 100;
const TANK_RANGE_MIN = 20;
const TANK_RANGE_MAX = 2000;
const STARTING_FUEL_MIN = 0;
const STARTING_FUEL_MAX = 1;

// MPG, tank-range, and starting-fuel what-if sliders (UX-02). Every drag
// tick calls `onChange` immediately for instant local visual feedback; the
// parent (VehicleSection) feeds the same value into the debounced re-solve
// (useDebouncedResolve.ts) so a whole drag gesture still costs one network
// call (D-14) even though this component re-renders on every tick.
function WhatIfSliders({ vehicle, disabled, onChange }: WhatIfSlidersProps) {
  const handleMpgChange = (_event: Event, value: number | number[]) => {
    const mpg = Array.isArray(value) ? value[0] : value;
    onChange({ ...vehicle, mpg });
  };

  const handleTankRangeChange = (_event: Event, value: number | number[]) => {
    const tank_range_mi = Array.isArray(value) ? value[0] : value;
    onChange({ ...vehicle, tank_range_mi });
  };

  const handleStartingFuelChange = (_event: Event, value: number | number[]) => {
    const starting_fuel = Array.isArray(value) ? value[0] : value;
    onChange({ ...vehicle, starting_fuel });
  };

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, mt: 2 }}>
      <Box>
        <Typography variant="body2" gutterBottom>
          MPG: {vehicle.mpg}
        </Typography>
        <Slider
          aria-label="Miles per gallon"
          value={vehicle.mpg}
          min={MPG_MIN}
          max={MPG_MAX}
          step={0.5}
          disabled={disabled}
          onChange={handleMpgChange}
          valueLabelDisplay="auto"
        />
      </Box>

      <Box>
        <Typography variant="body2" gutterBottom>
          Tank range: {vehicle.tank_range_mi} mi
        </Typography>
        <Slider
          aria-label="Tank range in miles"
          value={vehicle.tank_range_mi}
          min={TANK_RANGE_MIN}
          max={TANK_RANGE_MAX}
          step={10}
          disabled={disabled}
          onChange={handleTankRangeChange}
          valueLabelDisplay="auto"
        />
      </Box>

      <Box>
        <Typography variant="body2" gutterBottom>
          Starting fuel: {Math.round(vehicle.starting_fuel * 100)}%
        </Typography>
        <Slider
          aria-label="Starting fuel fraction"
          value={vehicle.starting_fuel}
          min={STARTING_FUEL_MIN}
          max={STARTING_FUEL_MAX}
          step={0.05}
          disabled={disabled}
          onChange={handleStartingFuelChange}
          valueLabelDisplay="auto"
          valueLabelFormat={(v) => `${Math.round(v * 100)}%`}
        />
      </Box>
    </Box>
  );
}

export default WhatIfSliders;
