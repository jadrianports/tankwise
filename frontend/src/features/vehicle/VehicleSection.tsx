import { useState } from 'react';
import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';

import PresetChips from './PresetChips';
import WhatIfSliders from './WhatIfSliders';
import { HERO_VEHICLE_PRESET_ID, VEHICLE_PRESETS, type VehiclePreset } from '../../constants/presets';
import type { VehicleProfileRequest } from '../../types/routeContract';

const HERO_PRESET = VEHICLE_PRESETS.find((preset) => preset.id === HERO_VEHICLE_PRESET_ID)!;

// The what-if engine's chip/slider surface (UX-12): four diesel preset
// chips at the locked D-36 figures (Semi loaded selected by default,
// D-38) plus bounded MPG/tank/starting-fuel sliders. This plan's Task 1
// wires the local visual state only -- selecting a chip or dragging a
// slider produces the correct `{mpg, tank_range_mi, starting_fuel}`
// object, but nothing re-solves yet. Task 2 wires that same state into
// useDebouncedResolve.ts's debounced re-solve.
function VehicleSection() {
  const [selectedPresetId, setSelectedPresetId] = useState<string | null>(HERO_VEHICLE_PRESET_ID);
  const [vehicle, setVehicle] = useState<VehicleProfileRequest>(HERO_PRESET.vehicle);

  const handlePresetSelect = (preset: VehiclePreset) => {
    setVehicle(preset.vehicle);
    setSelectedPresetId(preset.id);
  };

  // A hand-tuned slider value no longer matches any preset exactly, so no
  // chip stays highlighted once the user starts dragging.
  const handleSliderChange = (next: VehicleProfileRequest) => {
    setVehicle(next);
    setSelectedPresetId(null);
  };

  return (
    <Box>
      <Typography variant="h6" component="h2" gutterBottom>
        Vehicle
      </Typography>

      <PresetChips presets={VEHICLE_PRESETS} selectedId={selectedPresetId} onSelect={handlePresetSelect} />

      <WhatIfSliders vehicle={vehicle} onChange={handleSliderChange} />
    </Box>
  );
}

export default VehicleSection;
