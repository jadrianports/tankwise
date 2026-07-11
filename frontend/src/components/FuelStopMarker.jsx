import L from 'leaflet';
import { Marker, Popup } from 'react-leaflet';
import Typography from '@mui/material/Typography';
import Box from '@mui/material/Box';

// Fuel-stop marker colors per the Map & Marker Contract (light/dark).
const COLORS = {
  light: { fill: '#F59E0B', text: '#FFFFFF', outline: '#B45309' },
  dark: { fill: '#FBBF24', text: '#10151B', outline: '#D97706' },
};

function numberedDivIcon(number, mode) {
  const { fill, text, outline } = COLORS[mode] ?? COLORS.light;
  return L.divIcon({
    className: '',
    html:
      `<div style="background:${fill};color:${text};width:32px;height:32px;` +
      'border-radius:50%;display:flex;align-items:center;justify-content:center;' +
      `font-weight:600;font-family:Inter,sans-serif;border:2px solid ${outline};box-sizing:border-box;">` +
      `${number}</div>`,
    iconSize: [32, 32],
  });
}

// A numbered L.divIcon (not the default Leaflet blue pin) -- number = route
// order matching the StopList. Popup content: station name (Heading role),
// price/gal, gallons purchased, cost (Label role, accent color) -- D-08.
function FuelStopMarker({ stop, number, mode, markerRef }) {
  const position = [Number(stop.location?.latitude), Number(stop.location?.longitude)];

  return (
    <Marker position={position} icon={numberedDivIcon(number, mode)} ref={markerRef}>
      <Popup>
        <Typography variant="subtitle1" component="p" sx={{ mb: 0.5 }}>
          {stop.name}
        </Typography>
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.25 }}>
          <Typography variant="body2" sx={{ color: 'fuel.dark' }}>
            ${stop.price_per_gallon}/gal
          </Typography>
          <Typography variant="body2">{stop.gallons} gal</Typography>
          <Typography variant="body2" sx={{ color: 'fuel.dark' }}>
            ${stop.cost} total
          </Typography>
        </Box>
      </Popup>
    </Marker>
  );
}

export default FuelStopMarker;
