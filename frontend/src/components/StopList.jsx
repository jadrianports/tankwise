import List from '@mui/material/List';
import ListItemButton from '@mui/material/ListItemButton';
import ListItemAvatar from '@mui/material/ListItemAvatar';
import ListItemText from '@mui/material/ListItemText';
import Avatar from '@mui/material/Avatar';
import Typography from '@mui/material/Typography';

import { formatGallons } from '../utils/format';

// Ordered itinerary: one row per fuel stop, numbered avatar matching the
// map marker's number, station name (Body), stop cost (Body, accent).
// Clicking a row focuses the corresponding map marker. station_id can
// be null (FuelStopSerializer), so rows key/focus by station_id ?? index.
function StopList({ stops, onSelectStop }) {
  if (!stops || stops.length === 0) {
    return null;
  }

  return (
    <List disablePadding>
      {stops.map((stop, index) => {
        const key = stop.station_id ?? index;
        return (
          <ListItemButton key={key} onClick={() => onSelectStop(key)} sx={{ borderRadius: 1 }}>
            <ListItemAvatar>
              <Avatar sx={{ bgcolor: 'fuel.main', color: 'fuel.contrastText' }}>
                {index + 1}
              </Avatar>
            </ListItemAvatar>
            <ListItemText
              primary={stop.name}
              secondary={`${formatGallons(stop.gallons)} @ $${stop.price_per_gallon}/gal`}
            />
            <Typography variant="body1" sx={{ color: 'fuel.main', fontWeight: 600 }}>
              ${stop.cost}
            </Typography>
          </ListItemButton>
        );
      })}
    </List>
  );
}

export default StopList;
