import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';
import { LineChart } from '@mui/x-charts/LineChart';
import { ChartsReferenceLine } from '@mui/x-charts/ChartsReferenceLine';

import type { FuelStop, Leg, VehicleEcho, WaypointMarker } from '../../types/routeContract';
import { buildTankSeries } from './tankSeries';

export interface TankChartProps {
  legs: Leg[];
  stops: FuelStop[];
  vehicle: VehicleEcho | null;
  // Additive (WAY-06/WAY-08, Phase 13). Only the INTERMEDIATE waypoints
  // (not the start/A or finish/last-letter marker, both already at the
  // chart's own left/right edge) get their own vertical reference line --
  // mirrors LegBreakdown.tsx's own middle-waypoint-only convention, and
  // keeps a 2-point trip's chart byte-identical (zero reference lines).
  waypoints?: WaypointMarker[];
}

function middleWaypoints(waypoints: WaypointMarker[]): WaypointMarker[] {
  return waypoints.length > 2 ? waypoints.slice(1, -1) : [];
}

// Running tank-level chart across the N+1 legs, drawn with @mui/x-charts
// -- already theme-matched to theme.js, including dark mode, so no extra
// styling is needed here.
function TankChart({ legs, stops, vehicle, waypoints = [] }: TankChartProps) {
  const series = vehicle && legs.length > 0 ? buildTankSeries(legs, stops, vehicle) : null;

  if (!series) {
    return (
      <Typography variant="body2" color="text.secondary">
        Not enough trip data to draw a tank chart.
      </Typography>
    );
  }

  const markers = middleWaypoints(waypoints);

  return (
    <Box>
      <LineChart
        height={220}
        series={[{ data: series.levels, area: true, label: 'Tank level', showMark: false }]}
        xAxis={[{ data: series.distances, label: 'Miles from start' }]}
        yAxis={[{ label: 'Gallons', min: 0, max: series.capacityGal }]}
      >
        {markers.map((waypoint) => (
          <ChartsReferenceLine
            key={waypoint.label}
            x={Number(waypoint.distance_from_start_mi)}
            label={waypoint.label}
            labelAlign="start"
            lineStyle={{ strokeDasharray: '4 4' }}
          />
        ))}
      </LineChart>
    </Box>
  );
}

export default TankChart;
