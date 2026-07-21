import { Marker } from 'react-map-gl/mapbox';
import { useColorScheme } from '@mui/material/styles';
import Box from '@mui/material/Box';

import type { FuelStop } from '../../types/routeContract';

// Carried over pixel-for-pixel from the retired Leaflet
// FuelStopMarker.jsx's numbered divIcon (09-UI-SPEC.md spacing-scale
// exception: 32px diameter, 2px outline).
const COLORS = {
  light: { fill: '#F59E0B', text: '#FFFFFF', outline: '#B45309' },
  dark: { fill: '#FBBF24', text: '#10151B', outline: '#D97706' },
};

export interface ChosenStopMarkerProps {
  stop: FuelStop;
  number: number;
  longitude: number;
  latitude: number;
  isOpen: boolean;
  onActivate: () => void;
}

// react-map-gl <Marker> wrapping a focusable DOM <button> (D-34, UX-10) --
// in tab order, keyboard-activatable (Enter/Space), unlike the decorative
// candidate circle layer. Opens its UX-13 justification popup on
// activation; the caller (MapView) owns which stop's popup is open and
// keys every chosen-stop marker off `station_id ?? index`, the same
// null-safe convention used throughout this codebase.
function ChosenStopMarker({ stop, number, longitude, latitude, isOpen, onActivate }: ChosenStopMarkerProps) {
  const { mode } = useColorScheme();
  const colors = mode === 'dark' ? COLORS.dark : COLORS.light;

  return (
    <Marker longitude={longitude} latitude={latitude} anchor="center">
      <Box
        component="button"
        type="button"
        onClick={onActivate}
        aria-haspopup="dialog"
        aria-expanded={isOpen}
        aria-label={`Fuel stop ${number}: ${stop.name}, $${stop.price_per_gallon} per gallon`}
        sx={{
          width: 32,
          height: 32,
          borderRadius: '50%',
          bgcolor: colors.fill,
          color: colors.text,
          border: `2px solid ${colors.outline}`,
          boxSizing: 'border-box',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontWeight: 600,
          fontFamily: 'Inter, sans-serif',
          fontSize: '0.875rem',
          cursor: 'pointer',
          p: 0,
          '&:focus-visible': {
            outline: `2px solid ${colors.text}`,
            outlineOffset: 2,
          },
        }}
      >
        {number}
      </Box>
    </Marker>
  );
}

export default ChosenStopMarker;
