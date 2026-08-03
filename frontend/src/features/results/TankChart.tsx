import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';
import { LineChart } from '@mui/x-charts/LineChart';
import { ChartsReferenceLine } from '@mui/x-charts/ChartsReferenceLine';

import type { FuelStop, Leg, VehicleEcho, WaypointMarker } from '../../types/routeContract';

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

interface TankSeries {
  distances: number[];
  levels: number[];
  capacityGal: number;
}

// Derives a running tank-level series (gallons) purely from already-returned
// fields -- no new backend field needed. Consumption across a leg is derived
// from that leg's own `distance_mi` at the vehicle's mpg; each stop's
// `gallons` is the exact amount purchased there. The result is a sawtooth:
// the level dips across a leg, then jumps back up at every stop where fuel
// was bought.
//
// `leg.gallons` is deliberately NOT consumption and must not be used here.
// `build_legs` (routing/services/legs.py) attributes each purchase to the leg
// DEPARTING the node where it was made, so leg 0 carries 0.00 gal however long
// it is, and leg k carries stop k-1's purchase. Subtracting that as if it were
// burn cancels exactly against the purchases added below, which rendered a flat
// full tank across the entire route.
export function buildTankSeries(legs: Leg[], stops: FuelStop[], vehicle: VehicleEcho): TankSeries | null {
  const mpg = Number(vehicle.mpg);
  const tankRangeMi = Number(vehicle.tank_range_mi);
  const startingFuel = Number(vehicle.starting_fuel);
  if (!Number.isFinite(mpg) || mpg <= 0 || !Number.isFinite(tankRangeMi) || !Number.isFinite(startingFuel)) {
    return null;
  }

  const capacityGal = tankRangeMi / mpg;
  let level = startingFuel * capacityGal;
  let cumulativeMi = 0;

  const distances: number[] = [0];
  const levels: number[] = [level];

  legs.forEach((leg, index) => {
    const legDistance = Number(leg.distance_mi);
    const burned = Number.isFinite(legDistance) ? legDistance / mpg : 0;
    level -= burned;
    cumulativeMi += Number.isFinite(legDistance) ? legDistance : 0;

    distances.push(cumulativeMi);
    levels.push(Math.max(level, 0));

    // A stop follows every leg except the final one (to the finish) --
    // `stops[index]` is the fuel stop this leg arrives at, if any.
    const stop = stops[index];
    if (stop) {
      const purchased = Number(stop.gallons);
      level += Number.isFinite(purchased) ? purchased : 0;
      distances.push(cumulativeMi);
      levels.push(Math.min(level, capacityGal));
    }
  });

  return { distances, levels, capacityGal };
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
