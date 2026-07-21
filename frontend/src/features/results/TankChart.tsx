import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';
import { LineChart } from '@mui/x-charts/LineChart';

import type { FuelStop, Leg, VehicleEcho } from '../../types/routeContract';

export interface TankChartProps {
  legs: Leg[];
  stops: FuelStop[];
  vehicle: VehicleEcho | null;
}

interface TankSeries {
  distances: number[];
  levels: number[];
  capacityGal: number;
}

// Derives a running tank-level series (gallons) purely from already-returned
// fields -- no new backend field needed (UX-03). Each leg's own `gallons` is
// the exact consumption the solver already computed for that leg; each
// stop's own `gallons` is the exact amount purchased there. The result is a
// sawtooth: the level dips across a leg, then jumps back up at every stop
// where fuel was bought.
function buildTankSeries(legs: Leg[], stops: FuelStop[], vehicle: VehicleEcho): TankSeries | null {
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
    const legGallons = Number(leg.gallons);
    const legDistance = Number(leg.distance_mi);
    level -= Number.isFinite(legGallons) ? legGallons : 0;
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

// Running tank-level chart across the N+1 legs (D-22 asymmetry), drawn with
// @mui/x-charts (D-21) -- already theme-matched to theme.js, including dark
// mode, so no extra styling is needed here.
function TankChart({ legs, stops, vehicle }: TankChartProps) {
  const series = vehicle && legs.length > 0 ? buildTankSeries(legs, stops, vehicle) : null;

  if (!series) {
    return (
      <Typography variant="body2" color="text.secondary">
        Not enough trip data to draw a tank chart.
      </Typography>
    );
  }

  return (
    <Box>
      <LineChart
        height={220}
        series={[{ data: series.levels, area: true, label: 'Tank level', showMark: false }]}
        xAxis={[{ data: series.distances, label: 'Miles from start' }]}
        yAxis={[{ label: 'Gallons', min: 0, max: series.capacityGal }]}
      />
    </Box>
  );
}

export default TankChart;
