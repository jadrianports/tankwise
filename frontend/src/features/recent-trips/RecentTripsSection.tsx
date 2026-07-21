import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';
import List from '@mui/material/List';
import ListItem from '@mui/material/ListItem';
import ListItemButton from '@mui/material/ListItemButton';
import ListItemText from '@mui/material/ListItemText';
import IconButton from '@mui/material/IconButton';
import CloseIcon from '@mui/icons-material/Close';

import { useRecentTrips } from './useRecentTrips';
import { requestLoadTrip } from '../share-export/tripState';
import { useRoutePlanContext } from '../../context/RoutePlanContext';

// Last 5 trip inputs, deduped, newest first. Clicking a row
// hands the trip to PlannerFormSection (a sibling Sidebar section) via
// tripState.ts's requestLoadTrip bridge, which repopulates the form and
// re-solves; a single click removes one entry with no confirm dialog
// (low-stakes, instantly reconstructable).
function RecentTripsSection() {
  const { trips, remove } = useRecentTrips();
  const { status } = useRoutePlanContext();
  const isLoading = status === 'loading';

  if (trips.length === 0) {
    // Nothing to show yet -- an empty, half-explained section adds no
    // value before the first trip is ever solved.
    return null;
  }

  return (
    <Box>
      <Typography variant="h6" component="h2" gutterBottom>
        Recent trips
      </Typography>
      <List dense disablePadding>
        {trips.map((trip, index) => (
          <ListItem
            key={`${trip.start}|${trip.finish}|${trip.vehicle}|${trip.savedAt}`}
            disablePadding
            secondaryAction={
              <IconButton
                edge="end"
                aria-label={`Remove ${trip.startLabel} to ${trip.finishLabel} from recent trips`}
                onClick={() => remove(index)}
                sx={{ minWidth: 44, minHeight: 44 }}
              >
                <CloseIcon fontSize="small" />
              </IconButton>
            }
          >
            <ListItemButton disabled={isLoading} onClick={() => requestLoadTrip(trip)} sx={{ pr: 6 }}>
              <ListItemText primary={`${trip.startLabel} → ${trip.finishLabel}`} />
            </ListItemButton>
          </ListItem>
        ))}
      </List>
    </Box>
  );
}

export default RecentTripsSection;
