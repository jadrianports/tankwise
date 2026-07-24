import List from '@mui/material/List';
import ListItem from '@mui/material/ListItem';
import ListItemButton from '@mui/material/ListItemButton';
import ListItemAvatar from '@mui/material/ListItemAvatar';
import ListItemText from '@mui/material/ListItemText';
import Avatar from '@mui/material/Avatar';
import Typography from '@mui/material/Typography';

import type { FuelStop, WaypointMarker } from '../../types/routeContract';
import { useRoutePlanContext } from '../../context/RoutePlanContext';
import { formatCurrency, formatGallons } from '../../utils/format';

export interface StopListProps {
  stops: FuelStop[];
  // Additive (WAY-06). Only the INTERMEDIATE waypoints (not the start/A or
  // finish/last-letter marker -- neither reads as a "stop" the driver adds
  // fuel at) are interleaved into the itinerary, letter-badged instead of
  // numbered, so they visually read as user stops rather than fuel stops.
  waypoints?: WaypointMarker[];
}

interface FuelRow {
  type: 'fuel';
  key: string | number;
  number: number;
  stop: FuelStop;
}

interface WaypointRow {
  type: 'waypoint';
  key: string;
  waypoint: WaypointMarker;
}

type ItineraryRow = FuelRow | WaypointRow;

function middleWaypoints(waypoints: WaypointMarker[]): WaypointMarker[] {
  return waypoints.length > 2 ? waypoints.slice(1, -1) : [];
}

function rowDistance(row: ItineraryRow): number {
  const raw = row.type === 'fuel' ? row.stop.distance_from_start_mi : row.waypoint.distance_from_start_mi;
  const n = Number(raw);
  return Number.isFinite(n) ? n : 0;
}

// Merges fuel stops and intermediate user waypoints into ONE ordered
// itinerary by cumulative `distance_from_start_mi` -- same flattened axis
// both arrays already share -- rather than two separate lists (D-05: one
// continuous, annotated plan).
function buildItinerary(stops: FuelStop[], waypoints: WaypointMarker[]): ItineraryRow[] {
  const fuelRows: FuelRow[] = stops.map((stop, index) => ({
    type: 'fuel',
    key: stop.station_id ?? index,
    number: index + 1,
    stop,
  }));
  const waypointRows: WaypointRow[] = middleWaypoints(waypoints).map((waypoint) => ({
    type: 'waypoint',
    key: `waypoint-${waypoint.label}`,
    waypoint,
  }));
  return [...fuelRows, ...waypointRows].sort((a, b) => rowDistance(a) - rowDistance(b));
}

// Ordered itinerary: one row per fuel stop, numbered avatar matching the
// map marker's number, plus (additive, Phase 13) one letter-badged row per
// intermediate user waypoint, matching its A/B/C map pin -- visually
// distinct from the numbered fuel-icon rows. `station_id` can be null
// (FuelStopSerializer), so fuel rows key off `station_id ?? index` -- the
// same null-safe convention used throughout this codebase for station
// lists. Clicking a fuel-stop row calls the shared `focusStop(key)`
// handler from RoutePlanContext, which App.tsx bridges to MapView's own
// camera fly-to/popup-open logic; waypoint rows are not interactive (their
// map pins are static, unlike the clickable fuel-stop markers).
function StopList({ stops, waypoints = [] }: StopListProps) {
  const { focusStop } = useRoutePlanContext();
  const itinerary = buildItinerary(stops, waypoints);

  if (itinerary.length === 0) {
    return null;
  }

  return (
    <List disablePadding aria-label="Fuel stops">
      {itinerary.map((row) => {
        if (row.type === 'fuel') {
          const { stop, number, key } = row;
          return (
            <ListItemButton key={key} onClick={() => focusStop(key)} sx={{ borderRadius: 1 }}>
              <ListItemAvatar>
                <Avatar sx={{ bgcolor: 'fuel.main', color: 'fuel.contrastText' }}>{number}</Avatar>
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
        }

        const { waypoint, key } = row;
        return (
          <ListItem key={key} sx={{ borderRadius: 1 }}>
            <ListItemAvatar>
              <Avatar sx={{ bgcolor: 'primary.main', color: 'primary.contrastText' }}>{waypoint.label}</Avatar>
            </ListItemAvatar>
            <ListItemText primary={waypoint.name} secondary="Your stop" />
          </ListItem>
        );
      })}
    </List>
  );
}

export default StopList;
