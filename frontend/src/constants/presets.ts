import type { VehicleProfileRequest } from '../types/routeContract';

// Vehicle presets. Every preset is diesel -- the dataset is truck-stop
// diesel prices, so pricing a gasoline sedan against it would be
// dishonest. Figures are a cited, fleet-average-shaded synthesis:
//   - Semi (loaded): 6.5 mpg, ~1,050 mi -- ATRI's fleet-average 6.65 mpg,
//     ~160 gal (a 15-25% real-world derate below 200-300 gal nameplate).
//     This is the hero/default preset: the app loads with this selected
//     and sends it explicitly in the request's nested `vehicle` object;
//     the backend's own default (10 mpg / 500 mi) is unchanged.
//   - Semi (empty): 8.5 mpg, ~1,350 mi -- same 160 gal tanks as loaded
//     (same physical vehicle), corrected upward from the candidate set's
//     internally-inconsistent ~1,050 mi to hold gallons constant.
//   - RV: 8 mpg, ~700 mi -- low-mid of diesel Class A "pusher" 7-12 mpg,
//     90 gal (low end of 90-150 gal diesel RV tanks).
//   - Sedan: 32 mpg, ~450 mi -- shaded down from VW TDI-class 34 mpg
//     combined EPA, ~14 gal compact-diesel-sedan-class tank.
// Do NOT substitute the optimistic candidate set or invent alternates --
// these are the cited, user-facing figures.
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

// The hero preset wins in the UI; the API default (10mpg/500mi) is
// unchanged for any request that omits `vehicle`.
export const HERO_VEHICLE_PRESET_ID = 'semi-loaded';

// Demo trip chips. Long-haul routes chosen to suit the
// realistic semi range above -- a real driver reads a 500-mi range on a
// Class 8 and knows it's wrong, so demo chips are coast-to-coast /
// Dallas-Seattle class routes, not the v1.0-era short happy-path routes
// sized for the old unrealistic 500-mi default. Fixed "lat,lng"
// coordinate strings, never addresses, so a repeat click always hits the
// same normalized cache key.
//
// Resolved (not an open question): stop counts were re-measured against
// the final v3 build -- EIA-indexed prices, multi-stop routing, and
// elevation all live -- via the showcase capture script
// (frontend/scripts/showcase.capture.ts), which reads the count straight
// out of the rendered itinerary rather than assuming it. At the hero
// preset (LA->NYC, Semi-loaded) that measured count is 10, well above
// both the ~6 pre-EIA-indexing figure this comment previously carried
// and the 2-3 assumed while planning; the multi-stop chip (LA->Denver->
// Chicago) measures 6. Both are accepted as correct solver behaviour
// rather than something to fix. The solver minimizes total dollars, not
// stop count, so it takes opportunistic `top_up_at_cheapest` purchases
// wherever fuel is cheap. LA->NYC is ~2,790 mi on a ~1,050 mi range, so 2
// stops is only the physical floor -- every stop beyond that is the
// optimizer buying cheap. The user-facing copy below stays count-agnostic
// for exactly this reason, and the product now explains the behaviour
// in-app through the WhyMultipleStopsPopup dialog mounted above the
// itinerary in ResultsSection, so a reviewer reads the count as
// intelligence rather than a bug.
export interface DemoTrip {
  label: string;
  description: string;
  start: string;
  finish: string;
  // Additive (D-09/D-10, WAY-09): order-preserving intermediate stops,
  // same fixed "lat,lng" string shape as start/finish -- never addresses,
  // so a repeat click always hits the same normalized cache key. Absent
  // for the three original A->B chips, which stay byte-unchanged.
  waypoints?: string[];
  // Additive, cache-warming-only field. Nothing in the UI reads this --
  // DemoTripChips renders every entry exactly as before. Marks a trip as
  // permanently excluded from the keep-warm workflow's warm POSTs. The
  // reason is mechanical, not cosmetic: views.py only calls cache.set on
  // the success path, so a route that 422s (no drivable route) is never
  // cached -- warming it would spend a real Mapbox Directions call on
  // every warm cycle forever while never producing a cache hit.
  excludeFromWarming?: boolean;
}

// Exported as `PRESET_ROUTES`; `DEMO_TRIPS` below is the alias current
// call sites import.
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
    excludeFromWarming: true,
  },
  {
    label: 'Los Angeles → Denver → Chicago',
    description: 'Multi-stop · Rockies crossing · multiple fuel stops',
    start: '34.0522,-118.2437',
    finish: '41.8781,-87.6298',
    waypoints: ['39.7392,-104.9903'],
  },
];

// Alias matching the "demo trip chips" vocabulary used elsewhere --
// same array, same reference.
export const DEMO_TRIPS = PRESET_ROUTES;
