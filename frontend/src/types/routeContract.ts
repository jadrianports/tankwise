// Hand-written types for the POST /api/route response contract, mirrored
// directly from `routing/serializers.py::RouteResponseSerializer` (and its
// candidate_stations[] addition landed earlier this phase -- see
// 09-01-SUMMARY.md). Money, gallon and mile fields are full-precision
// Decimal STRINGS from the backend, quantized only for display via
// `utils/format.ts` -- never typed as `number` here. Seconds/percent/count
// fields are already plain JSON numbers server-side (see
// `_duration_repr`/`_percent_repr`), so they stay `number` (or `number |
// null`, matching the backend's own None-safety).

export type PurchaseReason =
  | 'reach_cheaper_stop'
  | 'fill_to_continue'
  | 'reach_finish'
  | 'top_up_at_cheapest';

// `_location_repr` shape: a resolved coordinate rendered as string lat/lng,
// or `null` when no coords were supplied via serializer context.
export interface LatLngString {
  latitude: string | null;
  longitude: string | null;
}

// `_rationale_repr` output. `purchase_reason` is `null` for a stop that
// needed no rationale (e.g. the free starting tank covers the whole trip --
// see `routing/tests/test_solver.py`'s `assertIsNone(stop.purchase_reason)`).
export interface Rationale {
  purchase_reason: PurchaseReason | null;
  reason_target_station_id: string | null;
  reason_target_name: string | null;
  skipped_count: number;
  skipped_avg_price: string | null;
  corridor_avg_price: string | null;
  price_percentile: number | null;
}

// `FuelStopSerializer.to_representation` output.
export interface FuelStop {
  name: string;
  station_id: string | null; // can be null -- key lists off `station_id ?? index`
  location: LatLngString | null;
  distance_from_start_mi: string;
  price_per_gallon: string;
  gallons: string;
  cost: string;
  rationale: Rationale;
}

// `_candidate_stations_repr` output (D-09/D-10, landed in 09-01).
export interface CandidateStation {
  // Never null in practice: a candidate with no opis_id, or no resolvable
  // row in candidate_coords, is filtered out server-side before this array
  // is built (routing/serializers.py::_candidate_stations_repr).
  station_id: string;
  lat: number;
  lng: number;
  price_per_gallon: string;
  distance_from_start_mi: string;
}

// `_legs_repr` output. N+1 legs for N stops (Phase 7 D-22).
export interface Leg {
  from: string;
  to: string;
  distance_mi: string;
  duration_s: number | null;
  gallons: string;
  cost: string;
}

// `_savings_repr` output. The whole object is `null` when the naive
// baseline never solved (see the sibling top-level `savings_note`).
export interface Savings {
  amount: string;
  percent: number | null;
  naive_total_cost: string;
  naive_total_gallons: string;
  naive_stop_count: number;
}

// `_vehicle_repr` output -- the resolved vehicle profile echoed back,
// including the derived `starting_fuel_mi` that makes the free-tank
// assumption visible (Phase 7 D-04).
export interface VehicleEcho {
  mpg: string;
  tank_range_mi: string;
  starting_fuel: string;
  starting_fuel_mi: string;
}

// `_alternatives_repr` entry. `total_cost` is `null` for an infeasible
// alternative rather than the entry being omitted.
export interface Alternative {
  total_route_mi: string;
  duration_s: number | null;
  total_cost: string | null;
  chosen: boolean;
  feasible: boolean;
}

// The full `RouteResponseSerializer.to_representation` return shape.
export interface RouteResponse {
  start: LatLngString | null;
  finish: LatLngString | null;
  route_geometry: [number, number][]; // [lng, lat] GeoJSON order -- do NOT flip
  total_route_mi: string;
  fuel_stops: FuelStop[];
  total_cost: string;
  total_gallons: string;
  map_url: string | null;
  vehicle: VehicleEcho | null;
  legs: Leg[];
  total_duration_s: number | null;
  fuel_stop_count: number;
  savings: Savings | null;
  savings_note: string | null;
  alternatives_considered: number;
  alternatives: Alternative[];
  candidate_stations: CandidateStation[];
  price_as_of: string;
  price_data_note: string;
}

// The request-side nested vehicle profile POSTed to /api/route --
// matches `routing/serializers.py::VehicleSerializer` exactly (Phase 7
// D-01). All three keys are optional server-side (defaulted to 10mpg /
// 500mi / a full tank), but preset chips always send all three explicitly
// so the hero preset wins in the UI without changing the API default
// (D-38).
export interface VehicleProfileRequest {
  mpg: number;
  tank_range_mi: number;
  starting_fuel: number;
}

// `GET /api/config`'s response shape (landed in 09-01).
export interface ConfigResponse {
  mapbox_public_token: string;
  price_as_of: string;
  price_data_note: string;
}
