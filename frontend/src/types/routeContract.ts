// Hand-written types for the POST /api/route response contract, mirrored
// directly from `routing/serializers.py::RouteResponseSerializer`
// (including its candidate_stations[] addition). Money, gallon and mile
// fields are full-precision Decimal STRINGS from the backend, quantized
// only for display via `utils/format.ts` -- never typed as `number`
// here. Seconds/percent/count
// fields are already plain JSON numbers server-side (see
// `_duration_repr`/`_percent_repr`), so they stay `number` (or `number |
// null`, matching the backend's own None-safety).

export type PurchaseReason =
  | 'reach_cheaper_stop'
  | 'fill_to_continue'
  | 'reach_finish'
  | 'top_up_at_cheapest'
  | 'bypass_cheaper_not_worth_stop';

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
  // Additive (Phase 18): how many strictly-cheaper reachable stations the
  // fixed-charge recurrence evaluated as successors from this stop and did
  // not take because the flat per-stop penalty outweighed the fuel-dollar
  // saving, and that forgone saving. Money stays a full-precision string,
  // never `number`, matching this file's own header comment.
  bypassed_cheaper_count: number;
  bypassed_saving_forgone: string | null;
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
  // Additive (Phase 20/PROV-01). Wire values are `'opis_indexed'` (a real
  // OPIS-sourced price) or `'eia_regional_estimate'` (no OPIS row for this
  // station -- price is a regional EIA estimate). `null` covers a legacy
  // or pre-phase cached payload with no provenance recorded. Typed as
  // plain `string | null` rather than a two-value union -- same rationale
  // as `solver_strategy` below: an unrecognized future value should still
  // type-check, and the render path (never a `switch`, never an exhaustive
  // `Record`) must degrade rather than throw on one (D-08).
  price_source: string | null;
  rationale: Rationale;
}

// `_candidate_stations_repr` output.
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

// `_legs_repr` output. N+1 legs for N stops.
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
// assumption visible.
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

// `_waypoints_repr` entry (WAY-06/WAY-08). One letter-labeled marker
// (A, B, C, ...) per USER stop -- start, each intermediate waypoint, and
// finish -- with cumulative driving distance/duration from start. `lat`/
// `lng` render as floats for the map GL layer (mirrors
// `candidate_stations[]`); `name` defaults to "START"/"FINISH"/"Stop {letter}"
// server-side when no better label exists -- prefer `label` for the
// map-pin/leg-breakdown cross-reference and treat `name` as a fallback
// display string only.
export interface WaypointMarker {
  label: string;
  name: string;
  lat: number;
  lng: number;
  distance_from_start_mi: string;
  duration_s: number | null;
}

// `custom_exception_handler`'s `infeasible_route` 422 envelope detail
// (routing/exceptions.py). `leg_index`/`leg_coords` are additive
// (D-07/WAY-05): the offending leg on a multi-stop infeasible trip.
// Both are `null`/absent for a single-leg (2-point) infeasible route or
// a pre-multi-stop caller -- never assume they're populated. `leg_coords`
// is a `[lat, lng]`-order pair per bounding stop (mirrors
// `routing/views.py::_enrich_infeasible_leg`'s `ordered_stop_coords`
// tuples -- NOT `route_geometry`'s GeoJSON `[lng, lat]` convention).
export interface InfeasibleRouteDetail {
  from_station: string;
  to_station: string;
  gap_mi: string;
  max_range_mi: string;
  leg_index?: number | null;
  leg_coords?: [number, number][] | null;
}

// `price_index_status` values: "current" (this week's EIA factors),
// "stale" (last-known factors, EIA temporarily unreachable), or "frozen"
// (no factors ever fetched -- the original 2024 snapshot, unindexed).
export type PriceIndexStatus = 'current' | 'stale' | 'frozen';

// `solver_strategy` values (Phase 18-04c; wire value updated 18-04d):
// which algorithm actually produced the returned plan. `'exact_dp'` is
// the fixed-charge dynamic program (an exact optimum under fuel dollars
// plus a per-stop penalty); `'penalty_aware_heuristic'` is a fast,
// single-pass heuristic that approximates the same fixed-charge
// objective (not exactly, and not guaranteed stop-count-minimal -- see
// `routing.services.heuristic`'s own module docstring) used only when a
// deterministic pre-flight estimate finds the exact DP would exceed the
// request's latency budget. Treat any string other than these two the
// same way `JustificationPopup`'s reason lookup already treats an
// unmapped `PurchaseReason` -- render a neutral fallback rather than
// throwing, since this is server-reported and additive.
export type SolverStrategy = 'exact_dp' | 'penalty_aware_heuristic';

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
  // Additive (WAY-06/WAY-08, Phase 13): every live request (2-point or
  // multi-stop) returns at least the START/FINISH A/B pair -- an
  // instance shaped without a `waypoints` key at all (pre-Phase-13
  // backward compat) serializes `[]` instead.
  waypoints: WaypointMarker[];
  price_as_of: string;
  price_data_note: string;
  // Additive (Phase 20/PROV-04). A derived, server-composed sentence
  // describing the station dataset's price-provenance mix (e.g. "6,290
  // stations — all with recorded prices."). Always a string, empty when
  // nothing is known -- never `null`, matching `station_data_note()`'s own
  // return contract. Render verbatim; the client formats nothing.
  station_data_note: string;
  // Additive EIA indexing fields (Phase 12). `trend_delta_cents` is a
  // signed integer -- positive means the region's diesel average rose
  // week-over-week, negative means it fell. All four default safely
  // (status "frozen", the rest `null`) for a v1/v2-shaped or legacy
  // cached payload that predates this phase.
  price_index_status: PriceIndexStatus;
  eia_week: string | null;
  trend_region: string | null;
  trend_delta_cents: number | null;
  // Additive (Phase 18-04c). `null` only for a legacy/predates-this-field
  // cached payload -- a live solve() call always sets one of the two
  // `SolverStrategy` values. Typed as plain `string | null` rather than
  // `SolverStrategy | null` so an unrecognized future value (a third
  // strategy) still type-checks instead of forcing every consumer to
  // widen its own type -- consumers that care about the two known values
  // should narrow with `SolverStrategy` themselves.
  solver_strategy: string | null;
}

// The request-side nested vehicle profile POSTed to /api/route --
// matches `routing/serializers.py::VehicleSerializer` exactly. All three
// keys are optional server-side (defaulted to 10mpg / 500mi / a full
// tank), but preset chips always send all three explicitly so the hero
// preset wins in the UI without changing the API default.
export interface VehicleProfileRequest {
  mpg: number;
  tank_range_mi: number;
  starting_fuel: number;
}

// `GET /api/config`'s response shape.
export interface ConfigResponse {
  mapbox_public_token: string;
  price_as_of: string;
  price_data_note: string;
}
