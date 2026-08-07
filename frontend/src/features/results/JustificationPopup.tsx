import Dialog from '@mui/material/Dialog';
import DialogTitle from '@mui/material/DialogTitle';
import DialogContent from '@mui/material/DialogContent';
import IconButton from '@mui/material/IconButton';
import CloseIcon from '@mui/icons-material/Close';
import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';

import type { FuelStop, PurchaseReason } from '../../types/routeContract';
import { formatGallons } from '../../utils/format';

export interface JustificationPopupProps {
  stop: FuelStop;
  number: number;
  open: boolean;
  onClose: () => void;
}

// The purchase_reason enum translated into a human sentence (the backend
// emits no prose, structured fields only -- this is the one place that
// prose gets written, frontend-side). Looked up via an OPTIONAL call
// (see justificationText below) so a future, still-unmapped reason value
// degrades to a neutral sentence instead of throwing.
const REASON_COPY: Partial<Record<PurchaseReason, (stop: FuelStop) => string>> = {
  reach_cheaper_stop: (stop) =>
    `Bought just enough fuel here to reach ${
      stop.rationale.reason_target_name ?? 'a cheaper station up ahead'
    } without running low.`,
  fill_to_continue: () =>
    'No cheaper station was in range, so the tank was filled here to keep the trip moving.',
  reach_finish: () => 'Bought just enough fuel here to reach the finish.',
  top_up_at_cheapest: () => 'This was the cheapest station in range, so the tank was topped up here.',
  // Authored against real observed DP/heuristic output (D-06): on
  // el_paso_tx-portland_me at the UI-default vehicle, QUIKTRIP #667
  // fills up and drives past CIRCLE K #4707605 rather than pay for a
  // second stop (see 18-05-SUMMARY.md). Phase 19's HON-01 verifies this
  // wording against the shipped build rather than authoring it blind.
  bypass_cheaper_not_worth_stop: (stop) =>
    `Filled up here and drove past ${
      stop.rationale.reason_target_name ?? 'a cheaper station up ahead'
    } on the way, because stopping there would have cost more in fees than the fuel it would have saved.`,
};

// Qualifier appended to the per-gallon line, keyed on the raw `price_source`
// wire value. Both branches carry real copy deliberately (D-06): today's
// dataset is entirely `opis_indexed`, so an estimate-only branch would ship
// this phase with no observable UI change and leave the copy unverifiable
// against the shipped build. Each string carries its own leading
// space-middot-space separator so it concatenates onto the existing
// `/gal` text with no markup change. `Partial<Record<...>>` looked up
// optionally -- never a branching dispatch statement, never an exhaustive
// `Record` -- because an exhaustive Record over a four-value union
// (REASON_COPY, above) already crashed the SPA once when a fifth value
// arrived; a `null` or unrecognised `price_source` must degrade to no
// qualifier at all, not a guess (D-08).
const PRICE_SOURCE_COPY: Partial<Record<string, string>> = {
  opis_indexed: ' · recorded price',
  eia_regional_estimate: ' · regional estimate',
};

function priceSourceQualifier(stop: FuelStop): string {
  if (!stop.price_source) return '';
  return PRICE_SOURCE_COPY[stop.price_source] ?? '';
}

function justificationText(stop: FuelStop): string {
  const { purchase_reason } = stop.rationale;
  if (!purchase_reason) {
    return 'The starting tank already covered this leg -- no fuel was purchased here.';
  }
  // Optional call with a fallback: REASON_COPY was previously an exhaustive
  // Record over a four-value union, so a fifth (or future sixth) value
  // called `undefined` and threw at runtime -- a crash, not a copy gap.
  // The fallback stays even after every known reason has real copy, so a
  // future new value degrades instead of breaking.
  return (
    REASON_COPY[purchase_reason]?.(stop) ??
    'This stop was part of the cheapest overall fueling plan for the trip.'
  );
}

// Reworded per D-04: skipped_count/skipped_avg_price no longer mean
// "everything positionally passed between the previous stop and this
// one" -- they now count genuinely-rejected candidates: strictly-cheaper
// stations the solver evaluated as successors from this stop and chose
// not to take.
function skippedText(stop: FuelStop): string | null {
  const { skipped_count, skipped_avg_price } = stop.rationale;
  if (skipped_count <= 0) return null;
  const avg = skipped_avg_price ? `, averaging $${skipped_avg_price}/gal` : '';
  return `Rejected ${skipped_count} cheaper station${skipped_count === 1 ? '' : 's'} in range from here${avg}.`;
}

// New (Phase 18): names the count -- and, when priced, the forgone
// fuel-dollar saving -- of strictly-cheaper stations the solver bypassed
// here because the flat per-stop penalty outweighed the saving
// (`bypass_cheaper_not_worth_stop`). Distinct from skippedText above:
// this always reflects the actual bypass decision behind THIS purchase,
// while skippedText is the broader "what was evaluated and rejected"
// context.
function bypassedCheaperText(stop: FuelStop): string | null {
  const { bypassed_cheaper_count, bypassed_saving_forgone } = stop.rationale;
  if (bypassed_cheaper_count <= 0) return null;
  const saving = bypassed_saving_forgone ? ` (about $${bypassed_saving_forgone} in fuel savings)` : '';
  return `Passed up ${bypassed_cheaper_count} cheaper station${
    bypassed_cheaper_count === 1 ? '' : 's'
  } here rather than pay for another stop${saving}.`;
}

// New (Phase 21, PROV-03, D-19/D-20/D-21): the bypass-direction sibling of
// bypassedCheaperText above, narrowed to only the bypassed candidates that
// were `eia_regional_estimate` priced. Fires only on a real-priced stop
// that was kept while strictly cheaper regional-estimate-priced stations
// were passed over -- the chosen-despite direction already has an answer
// on screen (Phase 20's `· regional estimate` per-gallon qualifier), so
// this is deliberately one-directional, never a mirror of it.
//
// Authored against a real observed instance, per the same discipline
// bypass_cheaper_not_worth_stop's QUIKTRIP #667/CIRCLE K #4707605 comment
// above uses -- but this one is a documented FALLBACK witness, not a
// production corridor. Plan 21-08's realism sweep (the trust margin's own
// two-sweep measurement, twelve pinned corridors, 288 measured cells)
// found zero qualifying instances: no stop anywhere in the measured
// corpus kept a real-priced station while passing over a cheaper
// estimate-priced one. Recorded as a finding, not tuned toward finding
// one. This plan's own fallback action first tried the trust margin's
// two named anchor witnesses (`TrustMarginAnchorTests`,
// test_solver_fixed_charge_optimality.py, plan 21-04) at every
// MARGIN_LADDER rung, including the shipped `TRUST_MARGIN_USD` default --
// both genuinely swap which station gets bought, but neither ever populates
// this disclosure
// pair, because the pair is only computed inside a full-tank fill that
// flies PAST a reachable-but-not-worth-a-stop cheaper station, a
// different decision shape than either witness models (a same-position
// swap, and an optional-stop drop). Falling back one step further: the
// wording below is authored against
// `routing.tests.test_dp.BypassedEstimateDisclosureTests`'s own
// three-station witness (plan 21-07's regression fixture for this exact
// pair), run through the real `solver.solve()` -- a real-priced station
// (opis_id=1, $3.50/gal, 250mi) is kept, buying 50.0 gallons and flying
// past a cheaper regional-estimate station (opis_id=2, $3.00/gal, 500mi)
// on its way to the next stop, giving up $12.50 in fuel savings; this
// held identically at trust_margin=0 and at the shipped `TRUST_MARGIN_USD`
// default, since this disclosure is gated on the flat per-stop penalty,
// not on the trust margin's own value.
//
// The trust margin itself never appears in this sentence and never will:
// it is a planning device inside the DP's selection objective, never
// money the driver pays, so the only dollar figure this sentence can ever
// render is bypassed_estimate_saving_forgone -- forgone FUEL savings,
// computed from raw prices only (plan 21-07). Do not "fix" this by adding
// the margin figure back in; its absence here is deliberate, not an
// oversight.
function bypassedEstimateText(stop: FuelStop): string | null {
  const { bypassed_estimate_count, bypassed_estimate_saving_forgone } = stop.rationale;
  if (!bypassed_estimate_count || bypassed_estimate_count <= 0) return null;
  const saving = bypassed_estimate_saving_forgone
    ? `, giving up $${bypassed_estimate_saving_forgone} in fuel savings`
    : '';
  return `Passed over ${bypassed_estimate_count} cheaper station${
    bypassed_estimate_count === 1 ? '' : 's'
  } whose price is a regional estimate rather than a recorded one${saving}.`;
}

// `price_percentile` arrives from the API as a percentage number already
// (the serializer's `_percent_repr` renders the solver's 0-to-1 fraction as
// `0.125 -> 12.5`), so it must NOT be scaled again here. It counts the
// candidates priced strictly BELOW this stop, which is the complement of
// what "beats" claims -- the cheapest station in range scores 0 and beats
// everything. Both conversions are applied in the one place that renders
// the sentence.
function percentileText(stop: FuelStop): string | null {
  const { price_percentile, corridor_avg_price } = stop.rationale;
  if (price_percentile === null) return null;
  const pct = Math.round(100 - price_percentile);
  const avg = corridor_avg_price ? ` (corridor average: $${corridor_avg_price}/gal)` : '';
  return `This price beats ${pct}% of the corridor's candidate stations${avg}.`;
}

// Accessible dialog (not a Leaflet popup) opened on a chosen-stop
// marker's activation. Every sentence here is composed frontend-side from
// the structured `rationale` fields -- no backend prose field is consumed.
function JustificationPopup({ stop, number, open, onClose }: JustificationPopupProps) {
  const skipped = skippedText(stop);
  const bypassedCheaper = bypassedCheaperText(stop);
  const bypassedEstimate = bypassedEstimateText(stop);
  const percentile = percentileText(stop);

  return (
    <Dialog open={open} onClose={onClose} aria-labelledby="justification-popup-title" maxWidth="xs" fullWidth>
      <DialogTitle
        id="justification-popup-title"
        sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 2 }}
      >
        <Typography variant="h6" component="span">
          Stop {number}: {stop.name}
        </Typography>
        <IconButton aria-label="Close" onClick={onClose} size="small">
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent>
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.25, mb: 2 }}>
          <Typography variant="body2" sx={{ color: 'fuel.dark' }}>
            ${stop.price_per_gallon}/gal{priceSourceQualifier(stop)}
          </Typography>
          <Typography variant="body2">{formatGallons(stop.gallons)}</Typography>
          <Typography variant="body2" sx={{ color: 'fuel.dark' }}>
            ${stop.cost} total
          </Typography>
        </Box>
        <Typography variant="body1" sx={{ mb: 1 }}>
          {justificationText(stop)}
        </Typography>
        {skipped && (
          <Typography
            variant="body2"
            color="text.secondary"
            sx={{ mb: bypassedCheaper || bypassedEstimate || percentile ? 0.5 : 0 }}
          >
            {skipped}
          </Typography>
        )}
        {bypassedCheaper && (
          <Typography variant="body2" color="text.secondary" sx={{ mb: bypassedEstimate || percentile ? 0.5 : 0 }}>
            {bypassedCheaper}
          </Typography>
        )}
        {bypassedEstimate && (
          <Typography variant="body2" color="text.secondary" sx={{ mb: percentile ? 0.5 : 0 }}>
            {bypassedEstimate}
          </Typography>
        )}
        {percentile && (
          <Typography variant="body2" color="text.secondary">
            {percentile}
          </Typography>
        )}
      </DialogContent>
    </Dialog>
  );
}

export default JustificationPopup;
