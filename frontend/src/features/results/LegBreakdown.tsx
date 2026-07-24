import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';
import Table from '@mui/material/Table';
import TableBody from '@mui/material/TableBody';
import TableCell from '@mui/material/TableCell';
import TableHead from '@mui/material/TableHead';
import TableRow from '@mui/material/TableRow';

import type { Leg, WaypointMarker } from '../../types/routeContract';
import { formatCurrency, formatDuration, formatGallons, formatMiles } from '../../utils/format';

export interface LegBreakdownProps {
  legs: Leg[];
  totalDurationS: number | null;
  fuelStopCount: number;
  // Additive (WAY-06/WAY-08, Phase 13). The full A..last-letter marker
  // array; only the INTERMEDIATE entries (not the first/start letter or
  // the last/finish letter -- both already named by the first/last leg's
  // own `from`/`to` text) become their own boundary row. A 2-point trip's
  // `waypoints` is exactly [A, B] with no middle entries, so it renders
  // zero boundary rows -- byte-identical to the pre-multi-stop table.
  waypoints?: WaypointMarker[];
}

interface LegTableRow {
  type: 'leg';
  key: string;
  leg: Leg;
}

interface WaypointTableRow {
  type: 'waypoint';
  key: string;
  waypoint: WaypointMarker;
}

type BreakdownRow = LegTableRow | WaypointTableRow;

function middleWaypoints(waypoints: WaypointMarker[]): WaypointMarker[] {
  return waypoints.length > 2 ? waypoints.slice(1, -1) : [];
}

// Interleaves middle-waypoint boundary rows into the leg row sequence by
// comparing each waypoint's cumulative `distance_from_start_mi` against
// the legs' own running distance total (Pitfall B) -- `legs[]` itself is
// never spliced/mutated to carry an `is_waypoint` flag; it stays exactly
// what the backend returned, fuel-stop-indexed, and a waypoint row is a
// purely additive overlay row inserted between the leg rows it falls
// between on the same flattened distance axis.
function buildRows(legs: Leg[], waypoints: WaypointMarker[]): BreakdownRow[] {
  const pending = middleWaypoints(waypoints);
  const rows: BreakdownRow[] = [];
  let cumulativeMi = 0;
  let pendingIndex = 0;

  legs.forEach((leg, legIndex) => {
    rows.push({ type: 'leg', key: `leg-${legIndex}`, leg });
    const legDistance = Number(leg.distance_mi);
    cumulativeMi += Number.isFinite(legDistance) ? legDistance : 0;
    while (pendingIndex < pending.length && Number(pending[pendingIndex].distance_from_start_mi) <= cumulativeMi) {
      const waypoint = pending[pendingIndex];
      rows.push({ type: 'waypoint', key: `waypoint-${waypoint.label}`, waypoint });
      pendingIndex += 1;
    }
  });

  // Any waypoint whose distance never fell within a leg's running total
  // (shouldn't happen on a real flattened-axis response) still gets its
  // own row rather than silently vanishing from the table.
  while (pendingIndex < pending.length) {
    const waypoint = pending[pendingIndex];
    rows.push({ type: 'waypoint', key: `waypoint-${waypoint.label}`, waypoint });
    pendingIndex += 1;
  }

  return rows;
}

// Renders every entry in `legs[]` -- N+1 legs for N stops, never
// `fuel_stops[]` -- so a single-stop trip's start->stop and stop->finish
// legs both show up as their own rows, and a zero-stop trip still shows
// its one start->finish leg. ETA is driving time only, paired with the
// stop count rather than a wall-clock arrival estimate.
function LegBreakdown({ legs, totalDurationS, fuelStopCount, waypoints = [] }: LegBreakdownProps) {
  const rows = buildRows(legs, waypoints);

  return (
    <Box>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        {formatDuration(totalDurationS)} driving · {fuelStopCount} fuel{' '}
        {fuelStopCount === 1 ? 'stop' : 'stops'}
      </Typography>
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Leg</TableCell>
            <TableCell align="right">Distance</TableCell>
            <TableCell align="right">Duration</TableCell>
            <TableCell align="right">Gallons</TableCell>
            <TableCell align="right">Cost</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.map((row) =>
            row.type === 'leg' ? (
              <TableRow key={row.key}>
                <TableCell>
                  {row.leg.from} → {row.leg.to}
                </TableCell>
                <TableCell align="right">{formatMiles(row.leg.distance_mi)}</TableCell>
                <TableCell align="right">{formatDuration(row.leg.duration_s)}</TableCell>
                <TableCell align="right">{formatGallons(row.leg.gallons)}</TableCell>
                <TableCell align="right">{formatCurrency(row.leg.cost)}</TableCell>
              </TableRow>
            ) : (
              <TableRow key={row.key} sx={{ bgcolor: 'action.hover' }}>
                <TableCell colSpan={5}>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Box
                      aria-hidden
                      sx={{
                        width: 22,
                        height: 22,
                        flexShrink: 0,
                        borderRadius: '50%',
                        bgcolor: 'primary.main',
                        color: 'primary.contrastText',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        fontWeight: 600,
                        fontSize: '0.7rem',
                      }}
                    >
                      {row.waypoint.label}
                    </Box>
                    <Typography variant="body2">
                      {row.waypoint.label} · ~{formatDuration(row.waypoint.duration_s)} from start
                    </Typography>
                  </Box>
                </TableCell>
              </TableRow>
            )
          )}
        </TableBody>
      </Table>
    </Box>
  );
}

export default LegBreakdown;
