import { useState } from 'react';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Typography from '@mui/material/Typography';
import Box from '@mui/material/Box';
import TextField from '@mui/material/TextField';
import Divider from '@mui/material/Divider';
import Chip from '@mui/material/Chip';
import Tooltip from '@mui/material/Tooltip';

import type { RouteResponse } from '../../types/routeContract';
import { formatCurrency, formatMiles } from '../../utils/format';

const TREND_TOOLTIP_TITLE =
  'Week-over-week change in the U.S. EIA on-highway diesel average for this region.';

interface TrendChipContent {
  label: string;
  color: string;
}

// Chip content derived from the route-dominant region's week-over-week
// delta (D-08/D-09). A flat week (< 1 cent of movement) is "steady" --
// neutral color, no arrow. Direction is always encoded with BOTH the
// glyph and the color, never color alone.
function trendChipContent(region: string, deltaCents: number): TrendChipContent {
  const magnitude = Math.abs(deltaCents);
  if (magnitude < 1) {
    return { label: 'diesel steady this week', color: 'text.secondary' };
  }
  if (deltaCents > 0) {
    return { label: `${region} diesel ▲ ${magnitude}¢ this week`, color: 'error.main' };
  }
  return { label: `${region} diesel ▼ ${magnitude}¢ this week`, color: 'primary.main' };
}

const DEFAULT_HAULS_PER_WEEK = 5;
const WEEKS_PER_YEAR = 52;

export interface SummaryCardProps {
  data: RouteResponse;
}

// A one-line trust badge only, never the full alternatives comparison
// table -- the backend response includes `alternatives[]`, but rendering
// the full comparison table is out of scope here.
function alternativesBadgeText(count: number): string {
  if (count <= 1) {
    return 'This was the only feasible route option found.';
  }
  return `Compared ${count} route options — this one's cheapest.`;
}

// Sidebar summary card: total fuel cost is the hero (Display type, fuel
// amber -- theme.js reserves amber for fuel cost/price only), the
// savings figure is pinned directly beneath it and always visible
// (Display type, PRIMARY GREEN -- savings is never amber), and a third
// line annualizes savings across a fleet at an adjustable hauls/week
// count. A $0 short trip (the backend's free starting-tank assumption)
// is presented as an honest result via the echoed vehicle profile, not
// left to look broken. Ends with the alternatives trust badge and the
// price disclaimer.
function SummaryCard({ data }: SummaryCardProps) {
  const [haulsPerWeek, setHaulsPerWeek] = useState(DEFAULT_HAULS_PER_WEEK);

  const totalCostIsZero = Number(data.total_cost) === 0;
  const savings = data.savings;
  const savingsAmount = savings ? Number(savings.amount) : NaN;
  const fleetAnnual =
    savings && Number.isFinite(savingsAmount) ? savingsAmount * haulsPerWeek * WEEKS_PER_YEAR : null;

  // Undefined/missing status (a pre-phase fixture or legacy cached
  // payload) falls to the frozen branch, same as `'frozen'` itself --
  // this keeps the original "Prices as of ..." sentence rendering
  // unchanged for any payload that predates this phase (D-16).
  const isIndexed = data.price_index_status === 'current' || data.price_index_status === 'stale';
  const disclaimerText = isIndexed
    ? data.price_data_note
    : `Prices as of ${data.price_as_of}. ${data.price_data_note}`;

  // The chip is a currency claim -- it only renders on a genuinely
  // current week with a resolved region and delta (D-10); stale/frozen
  // fallback hides it entirely.
  const showTrendChip =
    data.price_index_status === 'current' &&
    data.trend_region != null &&
    data.trend_delta_cents != null;
  const trendChip = showTrendChip
    ? trendChipContent(data.trend_region as string, data.trend_delta_cents as number)
    : null;

  // Rendered unconditionally with respect to price_index_status (D-04):
  // the composition fact is independent of price freshness, so it is not
  // folded into disclaimerText's ternary above. A falsy value -- the
  // empty-string zero-count case, or a legacy payload with no such key at
  // all -- renders nothing rather than an empty paragraph.
  const stationDataNote = data.station_data_note;

  return (
    <Card variant="outlined">
      <CardContent>
        <Typography variant="body2" color="text.secondary">
          Total fuel cost
        </Typography>
        <Typography variant="h4" component="p" sx={{ color: 'fuel.main', mb: 0.5 }}>
          {formatCurrency(data.total_cost)}
        </Typography>

        {totalCostIsZero && data.vehicle && (
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
            The full starting tank ({formatMiles(data.vehicle.starting_fuel_mi)} of range) covers
            this trip — no fuel needed along the way.
          </Typography>
        )}

        {savings ? (
          <Typography variant="h5" component="p" sx={{ color: 'primary.main', mb: 0.5 }}>
            Save {formatCurrency(savings.amount)}
            {savings.percent !== null ? ` (${savings.percent.toFixed(1)}%)` : ''}
          </Typography>
        ) : (
          <Typography variant="body1" color="text.secondary" sx={{ mb: 0.5 }}>
            {data.savings_note ?? 'Savings could not be estimated for this trip.'}
          </Typography>
        )}

        {fleetAnnual !== null && (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap', mb: 1.5 }}>
            <Typography variant="body1" sx={{ color: 'primary.main' }}>
              ~{formatCurrency(fleetAnnual)}/year at
            </Typography>
            <TextField
              type="number"
              size="small"
              value={haulsPerWeek}
              onChange={(event) => {
                const next = Number(event.target.value);
                setHaulsPerWeek(Number.isFinite(next) && next > 0 ? next : 1);
              }}
              slotProps={{ htmlInput: { min: 1, 'aria-label': 'Hauls per week' } }}
              sx={{ width: 72 }}
            />
            <Typography variant="body1" sx={{ color: 'primary.main' }}>
              hauls/week
            </Typography>
          </Box>
        )}

        <Divider sx={{ my: 1.5 }} />

        <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
          {alternativesBadgeText(data.alternatives_considered)}
        </Typography>

        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
          <Typography variant="body2" color="text.secondary">
            {disclaimerText}
          </Typography>
          {trendChip && (
            <Tooltip title={TREND_TOOLTIP_TITLE}>
              <Chip
                size="small"
                variant="outlined"
                label={trendChip.label}
                tabIndex={0}
                sx={{ color: trendChip.color, borderColor: trendChip.color, fontWeight: 600 }}
              />
            </Tooltip>
          )}
          {stationDataNote && (
            <Typography variant="body2" color="text.secondary" sx={{ width: '100%' }}>
              {stationDataNote}
            </Typography>
          )}
        </Box>
      </CardContent>
    </Card>
  );
}

export default SummaryCard;
