import type { VehicleProfileRequest } from '../types/routeContract';

// Vehicle presets (UX-12, D-36/D-37/D-38). Every preset is diesel (D-37) --
// the dataset is truck-stop diesel prices, so pricing a gasoline sedan
// against it would be dishonest. Figures are the researcher's cited,
// fleet-average-shaded synthesis (09-RESEARCH.md Priority Mandate #1):
//   - Semi (loaded): 6.5 mpg, ~1,050 mi -- ATRI's fleet-average 6.65 mpg,
//     ~160 gal (a 15-25% real-world derate below 200-300 gal nameplate).
//     This is the hero/default preset (D-38): the app loads with this
//     selected and sends it explicitly in the request's nested `vehicle`
//     object; the backend's own default (10 mpg / 500 mi) is unchanged.
//   - Semi (empty): 8.5 mpg, ~1,350 mi -- same 160 gal tanks as loaded
//     (same physical vehicle), corrected upward from the candidate set's
//     internally-inconsistent ~1,050 mi to hold gallons constant.
//   - RV: 8 mpg, ~700 mi -- low-mid of diesel Class A "pusher" 7-12 mpg,
//     90 gal (low end of 90-150 gal diesel RV tanks).
//   - Sedan: 32 mpg, ~450 mi -- shaded down from VW TDI-class 34 mpg
//     combined EPA, ~14 gal compact-diesel-sedan-class tank.
// Do NOT substitute the optimistic candidate set or invent alternates --
// these are the researcher-cited, user-facing figures.
export interface VehiclePreset {
  id: string;
  label: string;
  secondaryText: string;
  vehicle: VehicleProfileRequest;
}

export const VEHICLE_PRESETS: VehiclePreset[] = [
  {
    id: 'semi-loaded',
    label: 'Semi (loaded)',
    secondaryText: '6.5 mpg · ~1,050 mi',
    vehicle: { mpg: 6.5, tank_range_mi: 1050, starting_fuel: 1 },
  },
  {
    id: 'semi-empty',
    label: 'Semi (empty)',
    secondaryText: '8.5 mpg · ~1,350 mi',
    vehicle: { mpg: 8.5, tank_range_mi: 1350, starting_fuel: 1 },
  },
  {
    id: 'rv',
    label: 'RV',
    secondaryText: '8 mpg · ~700 mi',
    vehicle: { mpg: 8, tank_range_mi: 700, starting_fuel: 1 },
  },
  {
    id: 'sedan',
    label: 'Sedan',
    secondaryText: '32 mpg · ~450 mi',
    vehicle: { mpg: 32, tank_range_mi: 450, starting_fuel: 1 },
  },
];

// D-38: the hero preset wins in the UI; the API default (10mpg/500mi) is
// unchanged for any request that omits `vehicle`.
export const HERO_VEHICLE_PRESET_ID = 'semi-loaded';

// Demo trip chips (UX-05, D-35). Long-haul routes chosen to suit the
// realistic semi range above -- a real driver reads a 500-mi range on a
// Class 8 and knows it's wrong, so demo chips are coast-to-coast /
// Dallas-Seattle class routes, not the v1.0-era short happy-path routes
// sized for the old unrealistic 500-mi default. Fixed "lat,lng"
// coordinate strings, never addresses, so a repeat click always hits the
// same normalized cache key.
//
// Both demo trips return ~6 stops at the hero preset, not the 2-3 assumed
// while planning. That is the solver working as designed: it minimizes
// dollars, not stop count, so it takes opportunistic `top_up_at_cheapest`
// purchases wherever fuel is cheap. LA->NYC is ~2,790 mi on a ~1,050 mi
// range, so 2 stops is only the physical floor -- every stop beyond that
// is the optimizer buying cheap. Copy here and in the UI says "multiple
// fuel stops" rather than a fixed count for exactly this reason.
export interface DemoTrip {
  label: string;
  description: string;
  start: string;
  finish: string;
}

// Kept as `PRESET_ROUTES` (not renamed) so the existing `PresetRoutes.jsx`/
// `App.jsx` (untouched this plan -- replaced wholesale in a later Phase 9
// plan per 09-PATTERNS.md) keep working against the same import unchanged.
export const PRESET_ROUTES: DemoTrip[] = [
  {
    label: 'Los Angeles → New York City',
    description: 'Coast-to-coast · ~2,790 mi driving · multiple fuel stops',
    start: '34.0522,-118.2437',
    finish: '40.7128,-74.0060',
  },
  {
    label: 'Dallas → Seattle',
    description: 'Long haul · ~2,108 mi driving · multiple fuel stops',
    start: '32.7767,-96.7970',
    finish: '47.6062,-122.3321',
  },
  {
    label: 'Catalina Island → Los Angeles',
    description: 'No drivable route',
    start: '33.3879,-118.4163',
    finish: '34.0522,-118.2437',
  },
];

// Alias matching the D-35/UX-05 vocabulary used elsewhere in CONTEXT.md/
// RESEARCH.md ("demo trip chips") -- same array, same reference.
export const DEMO_TRIPS = PRESET_ROUTES;
