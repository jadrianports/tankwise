import { useState } from 'react';
import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';

import PresetChips from './PresetChips';
import WhatIfSliders from './WhatIfSliders';
import { useDebouncedResolve } from './useDebouncedResolve';
import { HERO_VEHICLE_PRESET_ID, VEHICLE_PRESETS, type VehiclePreset } from '../../constants/presets';
import type { VehicleProfileRequest } from '../../types/routeContract';

const HERO_PRESET = VEHICLE_PRESETS.find((preset) => preset.id === HERO_VEHICLE_PRESET_ID)!;

// The what-if engine (UX-12/UX-02): four diesel preset chips at the
// locked D-36 figures (Semi loaded selected by default, D-38) plus bounded
// MPG/tank/starting-fuel sliders that live-re-solve on a debounce. Owns
// only the locally-visible chip/slider values -- the actual re-solve
// (debounce, coordinate reuse, 429 pause/countdown) lives in
// useDebouncedResolve.ts / useRoutePlan.ts, reached through
// RoutePlanContext, never directly.
function VehicleSection() {
  const [selectedPresetId, setSelectedPresetId] = useState<string | null>(HERO_VEHICLE_PRESET_ID);
  const [vehicle, setVehicle] = useState<VehicleProfileRequest>(HERO_PRESET.vehicle);
  const { onVehicleChange, isPaused, retryCountdown } = useDebouncedResolve();

  const applyVehicle = (next: VehicleProfileRequest, presetId: string | null) => {
    setVehicle(next);
    setSelectedPresetId(presetId);
    onVehicleChange(next);
  };

  const handlePresetSelect = (preset: VehiclePreset) => applyVehicle(preset.vehicle, preset.id);
  // A hand-tuned slider value no longer matches any preset exactly, so no
  // chip stays highlighted once the user starts dragging.
  const handleSliderChange = (next: VehicleProfileRequest) => applyVehicle(next, null);

  return (
    <Box>
      <Typography variant="h6" component="h2" gutterBottom>
        Vehicle
      </Typography>

      <PresetChips
        presets={VEHICLE_PRESETS}
        selectedId={selectedPresetId}
        disabled={isPaused}
        onSelect={handlePresetSelect}
      />

      <WhatIfSliders vehicle={vehicle} disabled={isPaused} onChange={handleSliderChange} />

      {isPaused && (
        <Typography variant="body2" color="text.secondary" role="status" sx={{ mt: 1 }}>
          Catching up — retrying in {retryCountdown}s
        </Typography>
      )}
    </Box>
  );
}

export default VehicleSection;
