import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';
import Table from '@mui/material/Table';
import TableBody from '@mui/material/TableBody';
import TableCell from '@mui/material/TableCell';
import TableHead from '@mui/material/TableHead';
import TableRow from '@mui/material/TableRow';

import type { Leg } from '../../types/routeContract';
import { formatCurrency, formatDuration, formatGallons, formatMiles } from '../../utils/format';

export interface LegBreakdownProps {
  legs: Leg[];
  totalDurationS: number | null;
  fuelStopCount: number;
}

// Renders every entry in `legs[]` -- N+1 legs for N stops (Phase 7 D-22),
// never `fuel_stops[]` -- so a single-stop trip's start->stop and
// stop->finish legs both show up as their own rows, and a zero-stop trip
// still shows its one start->finish leg. ETA is driving time only (D-23),
// paired with the stop count rather than a wall-clock arrival estimate.
function LegBreakdown({ legs, totalDurationS, fuelStopCount }: LegBreakdownProps) {
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
          {legs.map((leg, index) => (
            <TableRow key={index}>
              <TableCell>
                {leg.from} → {leg.to}
              </TableCell>
              <TableCell align="right">{formatMiles(leg.distance_mi)}</TableCell>
              <TableCell align="right">{formatDuration(leg.duration_s)}</TableCell>
              <TableCell align="right">{formatGallons(leg.gallons)}</TableCell>
              <TableCell align="right">{formatCurrency(leg.cost)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Box>
  );
}

export default LegBreakdown;
