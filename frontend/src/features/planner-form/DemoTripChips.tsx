import Box from '@mui/material/Box';
import Chip from '@mui/material/Chip';
import Typography from '@mui/material/Typography';

import { DEMO_TRIPS, type DemoTrip } from '../../constants/presets';

interface DemoTripChipsProps {
  isLoading: boolean;
  onSelect: (trip: DemoTrip) => void;
}

// One-click long-haul demo trip chips. Consumes the
// shared DEMO_TRIPS constant from constants/presets.ts -- the single
// source of truth for these routes -- rather than redefining a second
// list; a repeat click always hits the same fixed "lat,lng" cache key.
function DemoTripChips({ isLoading, onSelect }: DemoTripChipsProps) {
  return (
    <Box sx={{ mt: 2 }}>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        Or try a real long-haul route
      </Typography>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
        {DEMO_TRIPS.map((trip) => (
          <Chip
            key={trip.label}
            label={trip.label}
            title={trip.description}
            clickable
            disabled={isLoading}
            onClick={() => onSelect(trip)}
          />
        ))}
      </Box>
    </Box>
  );
}

export default DemoTripChips;
