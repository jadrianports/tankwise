import { useCallback, useState } from 'react';
import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';
import Stack from '@mui/material/Stack';
import Chip from '@mui/material/Chip';
import { useTheme } from '@mui/material/styles';
import ArrowUpwardIcon from '@mui/icons-material/ArrowUpward';
import ArrowDownwardIcon from '@mui/icons-material/ArrowDownward';
import { LineChart } from '@mui/x-charts/LineChart';
import { ChartsReferenceLine } from '@mui/x-charts/ChartsReferenceLine';
import type { AxisItemIdentifier } from '@mui/x-charts/models';

import type { ElevationProfile } from '../../types/elevationProfile';
import type { WaypointMarker } from '../../types/routeContract';
import { formatMiles } from '../../utils/format';

export interface ElevationChartProps {
  profile: ElevationProfile | null;
  waypoints?: WaypointMarker[];
  onHoverDistanceChange: (distanceMi: number | null) => void;
}

// Same intermediate-waypoint-only reference-line convention as
// TankChart.tsx (D-07), copied verbatim so a 2-point trip's elevation
// chart stays reference-line-free, matching TankChart's own guarantee.
function middleWaypoints(waypoints: WaypointMarker[]): WaypointMarker[] {
  return waypoints.length > 2 ? waypoints.slice(1, -1) : [];
}

const EMPTY_DISTANCES: number[] = [];
const EMPTY_HIGHLIGHT: AxisItemIdentifier[] = [];

// Neutral -- these are magnitude readouts, not judgments, so no
// error.main/primary.main direction-coding (the ArrowUpward/ArrowDownward
// icons carry the up/down meaning instead).
const CHIP_SX = { color: 'text.secondary', borderColor: 'text.secondary' } as const;

// T-14-05: every chip value is Math.round + Number.isFinite guarded
// before it reaches a label, so a degenerate all-null terrain response
// can never render "NaN ft"/"Infinity ft".
function feetLabel(value: number): string {
  return Number.isFinite(value) ? Math.round(value).toLocaleString('en-US') : '0';
}

// Elevation-vs-distance chart mirroring TankChart.tsx's LineChart shape
// exactly (D-07) so the two stacked charts read column-for-column, plus a
// neutral stat-chip row and the three non-chart fallback states (loading /
// mostly-flat / unavailable). Stays a pure prop-driven component like
// TankChart -- no useRoutePlanContext import; ResultsSection reads the
// context and passes profile/waypoints/onHoverDistanceChange down.
function ElevationChart({ profile, waypoints = [], onHoverDistanceChange }: ElevationChartProps) {
  const theme = useTheme();
  const [highlightedAxis, setHighlightedAxis] = useState<AxisItemIdentifier[]>(EMPTY_HIGHLIGHT);

  const distances = profile?.distances ?? EMPTY_DISTANCES;

  // Controlled axis-highlight seam (D-03): held in state, updated only
  // from this memoized callback -- never an inline object literal on the
  // LineChart (Pitfall 5 / T-14-06, prevents a per-pointer-frame render
  // storm).
  const handleHighlightedAxisChange = useCallback(
    (axisItems: AxisItemIdentifier[]) => {
      if (!axisItems || axisItems.length === 0) {
        setHighlightedAxis(EMPTY_HIGHLIGHT);
        onHoverDistanceChange(null);
        return;
      }
      setHighlightedAxis(axisItems);
      const distance = distances[axisItems[0].dataIndex];
      onHoverDistanceChange(Number.isFinite(distance) ? distance : null);
    },
    [distances, onHoverDistanceChange],
  );

  if (profile == null) {
    return (
      <Typography variant="body2" color="text.secondary">
        Loading elevation profile…
      </Typography>
    );
  }

  if (profile.status === 'unavailable') {
    return (
      <Typography variant="body2" color="text.secondary">
        Elevation profile unavailable for this route — terrain data didn't finish loading. The rest
        of your fuel plan is unaffected.
      </Typography>
    );
  }

  if (profile.status === 'mostly-flat') {
    const totalMiLabel = formatMiles(profile.totalMi).replace(/\s*mi\s*$/, '');
    return (
      <Typography variant="body2" color="text.secondary">
        Mostly flat — negligible elevation change over {totalMiLabel} mi.
      </Typography>
    );
  }

  const markers = middleWaypoints(waypoints);
  const { stats } = profile;

  return (
    <Box>
      <Stack direction="row" spacing={1} flexWrap="wrap" sx={{ mb: 1 }}>
        <Chip size="small" variant="outlined" label={`Min ${feetLabel(stats.minFt)} ft`} sx={CHIP_SX} />
        <Chip size="small" variant="outlined" label={`Max ${feetLabel(stats.maxFt)} ft`} sx={CHIP_SX} />
        <Chip
          size="small"
          variant="outlined"
          icon={<ArrowUpwardIcon fontSize="small" />}
          label={`▲ ${feetLabel(stats.ascentFt)} ft climbed`}
          sx={CHIP_SX}
        />
        <Chip
          size="small"
          variant="outlined"
          icon={<ArrowDownwardIcon fontSize="small" />}
          label={`▼ ${feetLabel(stats.descentFt)} ft descended`}
          sx={CHIP_SX}
        />
      </Stack>

      <LineChart
        height={220}
        xAxis={[{ id: 'x', data: profile.distances, label: 'Miles from start' }]}
        yAxis={[{ label: 'Elevation (ft)' }]}
        series={[
          {
            data: profile.elevationsFt,
            area: false,
            showMark: false,
            label: 'Elevation',
            color: theme.palette.primary.main,
          },
        ]}
        highlightedAxis={highlightedAxis}
        onHighlightedAxisChange={handleHighlightedAxisChange}
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

export default ElevationChart;
