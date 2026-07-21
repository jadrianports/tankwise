import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';

import type { CandidateStation } from '../../types/routeContract';
import { computeQuantileBins } from '../../utils/quantile';
import { CANDIDATE_RAMP, candidatePrices } from './layers/candidateLayer';

export interface PriceLegendProps {
  candidates: CandidateStation[];
}

// Bottom-left horizontal 5-swatch strip (09-UI-SPEC.md Data Visualization)
// showing each bin's ACTUAL dollar threshold, not a percentile label --
// computed by calling the SAME shared computeQuantileBins the candidate
// circle layer uses, independently, against the same candidates array
// (Don't-Hand-Roll: one threshold function, never duplicated math, so the
// legend and the map layer can never drift onto different numbers, D-33).
function PriceLegend({ candidates }: PriceLegendProps) {
  const prices = candidatePrices(candidates);
  if (prices.length === 0) return null;

  const thresholds = computeQuantileBins(prices, 5);
  const max = Math.max(...prices);
  // 4 breakpoints + the top bin's own upper bound (the corridor's most
  // expensive candidate) = 5 labels for 5 swatches.
  const labels = [...thresholds, max];

  return (
    <Box
      role="group"
      aria-label="Candidate station price legend"
      sx={{
        position: 'absolute',
        bottom: 16,
        left: 16,
        display: 'flex',
        alignItems: 'flex-end',
        gap: 1,
        bgcolor: 'background.paper',
        boxShadow: 2,
        borderRadius: 1,
        px: 1.5,
        py: 1,
      }}
    >
      {labels.map((label, i) => (
        <Box
          key={i}
          sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 0.5 }}
        >
          <Box
            aria-hidden
            sx={{
              width: 14,
              height: 14,
              borderRadius: '50%',
              bgcolor: CANDIDATE_RAMP[i],
            }}
          />
          <Typography variant="body2" sx={{ fontSize: '0.75rem' }}>
            ${label.toFixed(2)}
          </Typography>
        </Box>
      ))}
    </Box>
  );
}

export default PriceLegend;
