import List from '@mui/material/List';
import ListItemButton from '@mui/material/ListItemButton';
import ListItemAvatar from '@mui/material/ListItemAvatar';
import ListItemText from '@mui/material/ListItemText';
import Avatar from '@mui/material/Avatar';
import Typography from '@mui/material/Typography';

import type { FuelStop } from '../../types/routeContract';
import { useRoutePlanContext } from '../../context/RoutePlanContext';
import { formatCurrency, formatGallons } from '../../utils/format';

export interface StopListProps {
  stops: FuelStop[];
}

// Ordered itinerary: one row per fuel stop, numbered avatar matching the
// map marker's number. `station_id` can be null (FuelStopSerializer), so
// rows key off `station_id ?? index` -- the same null-safe convention used
// throughout this codebase for station lists. Clicking a row calls the
// shared `focusStop(key)` handler from RoutePlanContext, which App.tsx
// bridges to MapView's own camera fly-to/popup-open logic (built map-side
// in Plan 04).
function StopList({ stops }: StopListProps) {
  const { focusStop } = useRoutePlanContext();

  if (!stops || stops.length === 0) {
    return null;
  }

  return (
    <List disablePadding aria-label="Fuel stops">
      {stops.map((stop, index) => {
        const key = stop.station_id ?? index;
        return (
          <ListItemButton key={key} onClick={() => focusStop(key)} sx={{ borderRadius: 1 }}>
            <ListItemAvatar>
              <Avatar sx={{ bgcolor: 'fuel.main', color: 'fuel.contrastText' }}>{index + 1}</Avatar>
            </ListItemAvatar>
            <ListItemText
              primary={stop.name}
              secondary={`${formatGallons(stop.gallons)} @ $${stop.price_per_gallon}/gal`}
            />
            <Typography variant="body1" sx={{ color: 'fuel.main', fontWeight: 600 }}>
              {formatCurrency(stop.cost)}
            </Typography>
          </ListItemButton>
        );
      })}
    </List>
  );
}

export default StopList;
